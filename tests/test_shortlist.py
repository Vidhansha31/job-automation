import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

import pandas as pd

from test_phase1 import IST, NOW, mk
from jobbot.collectors.jobspy_collector import JobSpyCollector
from jobbot.render import ResumeRenderer, count_pages
from jobbot.shortlist import run_shortlist
from jobbot.store import Store
from jobbot.tailor import load_profile
from unittest import mock

PROFILE = load_profile(Path(__file__).resolve().parent / "fixtures" / "sample_profile.yaml")
DAY = "2026-11-07"


def seed(store, n_good=6, n_bad=4):
    for i in range(n_good):
        j = mk(title=f"Product Analyst {i}" if i else "Product Analyst", company=f"Co{i}",
               job_id=f"g{i}", url=f"https://x.com/g{i}", experience="2-4 years",
               description="Jira SQL order management", location=f"City{i}, India")
        store.add_job(j, DAY, j.raw_row(DAY), [])
    for i in range(n_bad):
        j = mk(title="Senior Backend Engineer", company=f"Bad{i}", job_id=f"b{i}",
               url=f"https://x.com/b{i}", experience="6 years", location=f"Town{i}, India")
        store.add_job(j, DAY, j.raw_row(DAY), [])


class FakeRenderer:
    def __init__(self, fail_for=None):
        self.n, self.fail_for = 0, fail_for

    def render(self, profile, tailored, path):
        if self.fail_for and self.n == self.fail_for:
            self.n += 1
            raise RuntimeError("chromium crashed")
        self.n += 1
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(b"%PDF-fake")
        return 2


def run(store, out, **kw):
    return run_shortlist(store=store, profile=PROFILE, day=DAY, now=NOW, llm=None,
                         out_dir=out, scfg={"daily_target": 5, "minimum": 4}, **kw)


class TestShortlist(unittest.TestCase):
    def test_selects_only_relevant_builds_resumes_and_csv(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(":memory:")
            seed(store)
            rep = run(store, d, renderer=FakeRenderer())
            self.assertEqual((rep.pool, rep.selected, rep.total_today, rep.resumes_ok), (10, 5, 5, 5))
            self.assertTrue(all(r["title"].startswith("Product") for r in store.shortlisted(DAY)))
            csv_text = Path(rep.csv_path).read_text(encoding="utf-8-sig")
            self.assertIn("Resume File", csv_text.splitlines()[0])
            self.assertEqual(len(csv_text.strip().splitlines()), 6)
            self.assertEqual(len(list((Path(d) / DAY).glob("*.pdf"))), 5)

    def test_rerun_same_day_adds_nothing_and_no_job_repeats(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(":memory:")
            seed(store)
            run(store, d, renderer=FakeRenderer())
            rep2 = run(store, d, renderer=FakeRenderer())
            self.assertEqual((rep2.selected, rep2.total_today, rep2.resumes_ok), (0, 5, 0))
            # next day the already-shortlisted jobs are not offered again; the 1 leftover is
            rep3 = run_shortlist(store=store, profile=PROFILE, day="2026-11-08",
                                 now=NOW + timedelta(days=1), llm=None, out_dir=d,
                                 scfg={"daily_target": 5, "minimum": 4}, renderer=FakeRenderer())
            self.assertEqual(rep3.selected, 1)
            ids_day1 = {r["id"] for r in store.shortlisted(DAY)}
            ids_day2 = {r["id"] for r in store.shortlisted("2026-11-08")}
            self.assertFalse(ids_day1 & ids_day2)
            self.assertIn("WARNING", rep3.summary())          # 1 < minimum 4

    def test_one_failed_resume_is_isolated_and_retried_next_run(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(":memory:")
            seed(store)
            rep = run(store, d, renderer=FakeRenderer(fail_for=2))
            self.assertEqual((rep.resumes_ok, rep.resumes_failed), (4, 1))
            self.assertEqual(sum(1 for r in store.shortlisted(DAY) if r["resume_error"]), 1)
            rep2 = run(store, d, renderer=FakeRenderer())
            self.assertEqual(rep2.resumes_ok, 1)

    def test_no_renderer_still_shortlists(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(":memory:")
            seed(store)
            rep = run(store, d, renderer=None)
            self.assertEqual((rep.total_today, rep.resumes_ok), (5, 0))

    def test_apply_type_routing(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(":memory:")
            j = mk(source="lever", job_id="l", url="https://jobs.lever.co/x/l", title="Product Analyst",
                   experience="2 years")
            store.add_job(j, DAY, j.raw_row(DAY), [])
            run(store, d)
            self.assertEqual(store.shortlisted(DAY)[0]["apply_type"], "Auto")


class TestRobustness(unittest.TestCase):
    def test_naive_dates_and_null_fields_do_not_break_the_run(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(":memory:")
            seed(store)
            with store.db:
                store.db.execute("UPDATE jobs SET posted_at='2026-11-06T10:00:00' WHERE id=1")
                store.db.execute("UPDATE jobs SET description=NULL, location=NULL WHERE id=2")
            self.assertEqual(run(store, d, renderer=None).total_today, 5)

    def test_empty_database_is_handled(self):
        with tempfile.TemporaryDirectory() as d:
            rep = run(Store(":memory:"), d, renderer=None)
            self.assertEqual((rep.pool, rep.total_today), (0, 0))
            self.assertIn("WARNING", rep.summary())


class TestStoreMigration(unittest.TestCase):
    def test_phase1_database_upgrades_in_place(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            path = Path(d) / "jobs.db"
            db = sqlite3.connect(path)       # the exact Phase 1 schema, with a row in it
            db.executescript("""CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT,
              found_on TEXT NOT NULL, source TEXT, job_id TEXT, company TEXT, title TEXT, location TEXT,
              work_mode TEXT, job_type TEXT, experience TEXT, salary TEXT, posted_at TEXT, url TEXT,
              apply_url TEXT, ats TEXT, search_term TEXT, description TEXT, primary_key TEXT UNIQUE,
              status TEXT NOT NULL DEFAULT 'raw', sheet_synced INTEGER NOT NULL DEFAULT 0,
              raw_row_json TEXT, dedupe_rows_json TEXT);
              CREATE TABLE seen_keys (key TEXT PRIMARY KEY, job_rowid INTEGER);
              INSERT INTO jobs (found_on, source, company, title, primary_key) VALUES
                ('2026-11-06','lever','Old Co','Product Analyst','id:lever:1');""")
            db.commit()
            db.close()
            store = Store(path)
            store.close()
            store = Store(path)                                  # second open must be a no-op
            self.assertEqual(len(store.pool("2026-11-01")), 1)
            self.assertEqual(store.pool("2026-11-01")[0]["company"], "Old Co")
            store.close()                                        # Windows cannot delete an open db


class TestLinkedinEmptyRetry(unittest.TestCase):
    def test_empty_linkedin_result_is_retried_once(self):
        row = {"site": "linkedin", "id": "1", "title": "PA", "company": "Z", "location": "Pune, India",
               "job_url": "https://www.linkedin.com/jobs/view/123456", "description": "x"}
        results = [pd.DataFrame(), pd.DataFrame([row])]

        class C(JobSpyCollector):
            calls = 0

            def _scrape(self, site, term, location):
                C.calls += 1
                return results.pop(0)

        with mock.patch("jobbot.collectors.jobspy_collector.time.sleep"):
            c = C(["linkedin"], ["Product Analyst"], ["India"], 72, 10)
            jobs = c.collect()
        self.assertEqual((len(jobs), C.calls), (1, 2))

    def test_empty_indeed_result_is_not_retried(self):
        class C(JobSpyCollector):
            calls = 0

            def _scrape(self, site, term, location):
                C.calls += 1
                return pd.DataFrame()

        with mock.patch("jobbot.collectors.jobspy_collector.time.sleep"):
            C(["indeed"], ["x"], ["India"], 72, 10).collect()
        self.assertEqual(C.calls, 1)


class TestRealRender(unittest.TestCase):
    def test_pdf_is_valid_and_fits_two_pages(self):
        from jobbot.tailor import tailor_resume
        job = mk(title="Product Analyst", description="Jira SQL JMeter performance testing Grafana RCA "
                 "Linux Jenkins Java order management")
        with tempfile.TemporaryDirectory() as d:
            try:
                r = ResumeRenderer(max_pages=2).__enter__()
            except Exception as exc:  # noqa: BLE001
                self.skipTest(f"Chromium not available: {exc}")
            try:
                out = Path(d) / "r.pdf"
                pages = r.render(PROFILE, tailor_resume(PROFILE, job, "performance", None), out)
            finally:
                r.close()
            data = out.read_bytes()
            self.assertTrue(data.startswith(b"%PDF"))
            self.assertLessEqual(pages, 2)
            self.assertEqual(count_pages(data), pages)


if __name__ == "__main__":
    unittest.main()
