"""Offline tests for Phase 1. Run:  python -m unittest discover -s tests -v"""
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobbot.collectors import Collector
from jobbot.collectors.ats import (AtsCollector, BoardNotFound, parse_ashby,
                                   parse_greenhouse, parse_lever,
                                   parse_smartrecruiters)
from jobbot.collectors.jobspy_collector import record_to_job
from jobbot.models import (Job, canonical_url, detect_ats, extract_experience,
                           is_india_location, norm_city, norm_company, strip_html)
from jobbot.pipeline import run_pipeline, within_window
from jobbot.store import Store

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 11, 7, 0, 30, tzinfo=timezone.utc)


def mk(**kw):
    base = dict(source="linkedin", title="Product Analyst", company="Acme Pvt Ltd",
                url="https://www.linkedin.com/jobs/view/4000000001", job_id="4000000001",
                location="Pune, Maharashtra, India", posted_at=NOW - timedelta(hours=10))
    base.update(kw)
    return Job(**base)


class FakeSheets:
    def __init__(self, keys=None, fail_raw=False):
        self.keys, self.raw, self.dedupe, self.fail_raw = set(keys or []), [], [], fail_raw

    def load_dedupe_keys(self):
        return set(self.keys)

    def append_dedupe(self, rows):
        self.dedupe += rows

    def append_raw(self, rows):
        if self.fail_raw:
            raise RuntimeError("quota")
        self.raw += rows


class FakeCollector(Collector):
    name = "fake"

    def __init__(self, jobs, errors=None, boom=False):
        super().__init__()
        self._jobs, self.errors, self.boom = jobs, errors or [], boom

    def collect(self):
        if self.boom:
            raise RuntimeError("blocked")
        return self._jobs


class TestNormalisation(unittest.TestCase):
    def test_canonical_linkedin_variants_match(self):
        a = canonical_url("https://in.linkedin.com/jobs/view/product-analyst-at-acme-4000000001?trk=x&refId=1")
        b = canonical_url("https://www.linkedin.com/jobs/view/4000000001/")
        self.assertEqual(a, b)

    def test_canonical_strips_tracking_but_keeps_gh_jid(self):
        u = canonical_url("https://acme.com/careers?utm_source=x&gh_jid=77&ref=abc")
        self.assertEqual(u, "acme.com/careers?gh_jid=77")

    def test_indeed_uses_jk(self):
        self.assertEqual(canonical_url("https://in.indeed.com/viewjob?jk=abc123&from=serp"),
                         "indeed.com/viewjob?jk=abc123")

    def test_company_and_city_aliases(self):
        self.assertEqual(norm_company("Acme Pvt. Ltd."), norm_company("ACME"))
        self.assertEqual(norm_city("Bangalore, Karnataka"), norm_city("Bengaluru"))
        self.assertEqual(norm_city("Gurgaon"), "gurugram")

    def test_india_location(self):
        self.assertTrue(is_india_location("Remote - India"))
        self.assertTrue(is_india_location("Pune"))
        self.assertFalse(is_india_location("New York, NY"))
        self.assertFalse(is_india_location("Indianapolis, IN"))
        self.assertFalse(is_india_location(""))

    def test_experience_extraction(self):
        self.assertEqual(extract_experience("Need 2-4 years of experience"), "2-4 years")
        self.assertEqual(extract_experience("5+ years in SaaS"), "5+ years")
        self.assertEqual(extract_experience("minimum 3 years"), "3+ years")
        self.assertEqual(extract_experience("great culture"), "")

    def test_detect_ats(self):
        self.assertEqual(detect_ats("https://boards.greenhouse.io/x/jobs/1"), "greenhouse")
        self.assertEqual(detect_ats("https://jobs.lever.co/x/1"), "lever")
        self.assertEqual(detect_ats("https://x.wd5.myworkdayjobs.com/y"), "workday")

    def test_strip_html(self):
        self.assertEqual(strip_html("&lt;p&gt;Hi &amp;amp; bye&lt;/p&gt;&lt;ul&gt;&lt;li&gt;A&lt;/li&gt;&lt;/ul&gt;"),
                         "Hi & bye\n- A")


class TestKeys(unittest.TestCase):
    def test_cross_platform_same_role_shares_signature(self):
        li = mk()
        ind = mk(source="indeed", job_id="ind-1", url="https://in.indeed.com/viewjob?jk=zz",
                 company="ACME", title="Product Analyst (Remote)", location="Pune")
        self.assertTrue(set(li.keys()) & set(ind.keys()))

    def test_different_city_is_different_job(self):
        self.assertFalse({k for k in mk().keys() if k.startswith("sig:")} &
                         {k for k in mk(location="Delhi").keys() if k.startswith("sig:")})


class TestStore(unittest.TestCase):
    def test_duplicate_rejected_and_keys_persist(self):
        s = Store(":memory:")
        j = mk()
        self.assertIsNotNone(s.add_job(j, "2026-11-07", j.raw_row("2026-11-07"), []))
        self.assertIsNone(s.add_job(mk(job_id="other"), "2026-11-07", [], []))  # same URL
        self.assertEqual(s.stats()["total_jobs"], 1)

    def test_import_keys_blocks_known(self):
        s = Store(":memory:")
        s.import_keys(mk().keys())
        self.assertIsNone(s.add_job(mk(), "2026-11-07", [], []))


class TestParsers(unittest.TestCase):
    def test_greenhouse(self):
        data = {"jobs": [{"id": 1, "title": "Support Engineer", "location": {"name": "Pune, India"},
                          "absolute_url": "https://boards.greenhouse.io/x/jobs/1",
                          "updated_at": "2026-11-06T10:00:00-05:00",
                          "content": "&lt;p&gt;3-5 years experience&lt;/p&gt;"}]}
        [j] = parse_greenhouse(data, "x", "X Corp")
        self.assertEqual((j.company, j.experience, j.ats), ("X Corp", "3-5 years", "greenhouse"))
        self.assertIsNotNone(j.posted_at)

    def test_lever(self):
        data = [{"id": "a1", "text": "CSM", "categories": {"location": "Bengaluru", "commitment": "Full-time"},
                 "hostedUrl": "https://jobs.lever.co/x/a1", "applyUrl": "https://jobs.lever.co/x/a1/apply",
                 "createdAt": 1793000000000, "descriptionPlain": "Own accounts", "workplaceType": "hybrid"}]
        [j] = parse_lever(data, "x", "X")
        self.assertEqual((j.work_mode, j.job_type, j.ats), ("Hybrid", "Full-time", "lever"))

    def test_ashby_skips_unlisted(self):
        data = {"jobs": [{"id": "1", "title": "A", "isListed": False},
                         {"id": "2", "title": "PM", "location": "Mumbai", "isRemote": True,
                          "jobUrl": "https://jobs.ashbyhq.com/x/2", "publishedAt": "2026-11-06T00:00:00Z"}]}
        jobs = parse_ashby(data, "x", "X")
        self.assertEqual([j.job_id for j in jobs], ["2"])
        self.assertEqual(jobs[0].work_mode, "Remote")

    def test_smartrecruiters_marks_india(self):
        data = {"content": [{"id": "9", "name": "Analyst", "releasedDate": "2026-11-06T08:00:00Z",
                             "location": {"city": "Pune", "country": "in"}}]}
        [j] = parse_smartrecruiters(data, "x", "X")
        self.assertTrue(is_india_location(j.location))

    def test_jobspy_record_mapping_handles_nan(self):
        nan = float("nan")
        j = record_to_job({"site": "indeed", "id": "in-1", "title": "PA", "company": "Z",
                           "job_url": "https://in.indeed.com/viewjob?jk=1", "location": "Pune",
                           "date_posted": datetime(2026, 11, 6).date(), "description": nan,
                           "min_amount": nan, "max_amount": nan, "is_remote": nan}, "Product Analyst")
        self.assertEqual((j.source, j.salary, j.description), ("indeed", "", ""))
        self.assertIsNone(record_to_job({"site": "indeed", "title": nan, "company": "Z"}, "t"))


class TestPipeline(unittest.TestCase):
    def run_it(self, collectors, sheets, store=None):
        with tempfile.TemporaryDirectory() as d:
            store = store or Store(":memory:")
            rep = run_pipeline(store=store, collectors=collectors, sheets=sheets,
                               hours_old=72, tz=IST, data_dir=d, now=NOW)
            return rep, store

    def test_window_by_calendar_day(self):
        self.assertTrue(within_window(mk(posted_at=NOW - timedelta(hours=70)), 72, NOW))
        self.assertFalse(within_window(mk(posted_at=NOW - timedelta(days=6)), 72, NOW))
        self.assertTrue(within_window(mk(posted_at=None), 72, NOW))

    def test_end_to_end_dedupe_within_run_and_across_days(self):
        a = mk()
        dup_same_run = mk(source="indeed", job_id="i1", url="https://in.indeed.com/viewjob?jk=1",
                          company="Acme")                       # cross-platform duplicate
        old = mk(title="Old", job_id="o", url="https://x.com/old", posted_at=NOW - timedelta(days=9))
        bad = Job(source="x", title="", company="", url="")
        other = mk(title="Customer Success Manager", job_id="2", url="https://x.com/2")
        sheets = FakeSheets()
        rep, store = self.run_it([FakeCollector([a, dup_same_run, old, bad, other])], sheets)
        self.assertEqual((rep.found, rep.new, rep.duplicates, rep.too_old, rep.invalid),
                         (5, 2, 1, 1, 1))
        self.assertEqual(len(sheets.raw), 2)
        self.assertTrue(all(len(r) == 17 for r in sheets.raw))

        # next day: same jobs reappear -> nothing new
        rep2, _ = self.run_it([FakeCollector([mk(), other])], FakeSheets(), store)
        self.assertEqual((rep2.new, rep2.duplicates), (0, 2))

    def test_collector_crash_is_isolated(self):
        rep, _ = self.run_it([FakeCollector([], boom=True), FakeCollector([mk()], errors=["x"])],
                             FakeSheets())
        self.assertEqual(rep.new, 1)
        self.assertEqual(len(rep.errors), 2)

    def test_sheet_failure_keeps_rows_and_retries_next_run(self):
        store = Store(":memory:")
        rep, _ = self.run_it([FakeCollector([mk()])], FakeSheets(fail_raw=True), store)
        self.assertEqual((rep.new, rep.synced_rows), (1, 0))
        self.assertEqual(store.stats()["unsynced_to_sheet"], 1)
        good = FakeSheets()
        rep2, _ = self.run_it([FakeCollector([])], good, store)   # sheet is back
        self.assertEqual((len(good.raw), store.stats()["unsynced_to_sheet"]), (1, 0))

    def test_recovery_from_sheet_when_local_db_lost(self):
        sheets = FakeSheets(keys=mk().keys())
        rep, _ = self.run_it([FakeCollector([mk()])], sheets, Store(":memory:"))
        self.assertEqual((rep.new, rep.duplicates), (0, 1))

    def test_runs_without_sheets(self):
        rep, store = self.run_it([FakeCollector([mk()])], None)
        self.assertEqual(rep.new, 1)
        self.assertEqual(store.stats()["unsynced_to_sheet"], 1)


class TestAtsCollector(unittest.TestCase):
    def test_missing_board_does_not_stop_others(self):
        import jobbot.collectors.ats as ats

        def nf(session, slug, name):
            raise BoardNotFound(slug)

        def ok(session, slug, name):
            return [mk(location="Pune, India"), mk(job_id="2", url="https://x/2", location="Austin, TX")]

        old = dict(ats.FETCHERS)
        ats.FETCHERS.update({"greenhouse": nf, "lever": ok})
        try:
            c = AtsCollector({"greenhouse": ["bad"], "lever": ["good"]}, session=object())
            jobs = c.collect()
        finally:
            ats.FETCHERS.clear()
            ats.FETCHERS.update(old)
        self.assertEqual(len(jobs), 1)            # only the India job
        self.assertEqual(len(c.errors), 1)


if __name__ == "__main__":
    unittest.main()
