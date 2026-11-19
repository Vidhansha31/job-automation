import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from test_daily import DAY, add, rowcol_to_a1
from test_phase1 import mk
from test_sheets import FakeSpreadsheet, WorksheetNotFound
from jobbot.apply_run import run_apply
from jobbot.applier import Result
from jobbot.daily import COL, sync_daily_tab
from jobbot.qa import Ctx
from jobbot.scoring import Scored, select
from jobbot.sheets import SheetsClient
from jobbot.store import Store


class FakeApplier:
    def __init__(self, results):
        self.results, self.calls = results, []

    def apply(self, job, ctx, resume, dry_run=False):
        self.calls.append(job["company"])
        r = self.results.get(job["company"], Result("dryrun" if dry_run else "applied", "ok"))
        if isinstance(r, Exception):
            raise r
        return r


def sc(tier, score):
    return Scored(score=score, tier=tier) if "tier" in Scored.__dataclass_fields__ else None


class TestSelect(unittest.TestCase):
    def test_balanced(self):
        class S:
            def __init__(s, t, sc_): s.tier, s.score = t, sc_
        items = [(("linkedin", i), S(1, 90 - i)) for i in range(8)] + [(("naukri", 0), S(2, 50)),
                                                                        (("career", 0), S(3, 40))]
        out = select(items, 5, group=lambda x: x[0], min_each=1)
        self.assertEqual({x[0][0] for x in out}, {"linkedin", "naukri", "career"})
        self.assertEqual(len(out), 5)
        self.assertEqual(len(select(items, 5)), 5)


class TestApply(unittest.TestCase):
    def setUp(self):
        gs = types.ModuleType("gspread")
        gs.exceptions = types.SimpleNamespace(WorksheetNotFound=WorksheetNotFound)
        gu = types.ModuleType("gspread.utils")
        gu.rowcol_to_a1 = rowcol_to_a1
        gs.utils = gu
        p = mock.patch.dict(sys.modules, {"gspread": gs, "gspread.utils": gu})
        p.start()
        self.addCleanup(p.stop)
        self.tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "j.db")
        self.addCleanup(self.store.close)
        self.sheets = SheetsClient("x")
        self.sheets.sh = FakeSpreadsheet()
        self.cv = Path(self.tmp.name) / "cv.pdf"
        self.cv.write_bytes(b"x")
        for i in (1, 2, 3):
            rid = add(self.store, i, apply_url=f"https://jobs.lever.co/co{i}/abc{i}")
            self.store.set_resume(rid, path=str(self.cv), engine="offline", jd_summary="s")
        sync_daily_tab(self.store, self.sheets, DAY, auto_enabled=True)
        self.ctx = Ctx({}, 2.5)

    def run_it(self, ap, **kw):
        kw.setdefault("dry_run", False)
        return run_apply(store=self.store, sheets=self.sheets, applier=ap, ctx=self.ctx, today=DAY,
                         since_day=DAY, sleep=lambda s: None, **kw)

    def status(self):
        ws = self.sheets.sh.worksheet(DAY)
        return [r[COL["Status"] - 1] for r in ws.rows[1:]]

    def test_outcomes_and_sheet(self):
        ap = FakeApplier({"Co2": Result("failed", "No answer for required question: Why us"),
                          "Co3": RuntimeError("boom")})
        rep = self.run_it(ap)
        self.assertEqual(rep.counts, {"applied": 1, "failed": 2})
        self.assertEqual(self.status(), ["Applied (Auto)", "Auto-Apply Failed - Apply Manually",
                                         "Auto-Apply Failed - Apply Manually"])
        self.assertEqual(self.run_it(FakeApplier({})).pending, 0)      # never retried / re-applied

    def test_user_edit_protected(self):
        ws = self.sheets.sh.worksheet(DAY)
        ws.rows[1][COL["Status"] - 1] = "Applied (Manual)"
        self.run_it(FakeApplier({}))
        self.assertEqual(self.status()[0], "Applied (Manual)")

    def test_dry_run_keeps_status_and_is_repeatable(self):
        rep = self.run_it(FakeApplier({"Co1": Result("dryrun", "DRY RUN ok")}), dry_run=True)
        self.assertEqual(rep.counts.get("dryrun"), 3)
        self.assertEqual(set(self.status()), {"New"})
        self.assertEqual(self.run_it(FakeApplier({}), dry_run=True).pending, 3)

    def test_cap_and_missing_resume(self):
        rep = self.run_it(FakeApplier({}), max_per_day=2)
        self.assertEqual((rep.counts["applied"], rep.skipped_cap), (2, 1))
        self.cv.unlink()
        rep = self.run_it(FakeApplier({}), max_per_day=5)
        self.assertEqual(rep.counts, {"failed": 1})


class TestDiscover(unittest.TestCase):
    def test_discover(self):
        from jobbot import discover as D
        from jobbot.collectors import ats as A

        def fake(session, slug, name):
            if slug != "acme":
                raise A.BoardNotFound(slug)
            return [mk(title="PA", company="Acme", location="Pune, India")]
        with mock.patch.dict(A.FETCHERS, {"lever": fake, "greenhouse": fake}, clear=True), \
                mock.patch.object(A, "make_session", lambda: None):
            out = D.discover(["Acme", "Nope Co"], workers=2)
        self.assertEqual(sorted(out), ["greenhouse", "lever"])
        self.assertEqual(out["lever"][0]["slug"], "acme")


if __name__ == "__main__":
    unittest.main()


class TestRepair(TestApply):
    def test_existing_tab_gets_formatting_again(self):
        before = len(self.sheets.sh.batches)
        self.sheets.ensure_daily_tab(DAY)
        reqs = self.sheets.sh.batches[-1]["requests"] if isinstance(self.sheets.sh.batches[-1], dict) else []
        self.assertGreater(len(self.sheets.sh.batches), before)
        kinds = [list(r)[0] for r in reqs]
        self.assertEqual(kinds.count("deleteConditionalFormatRule"), 2)
        self.assertIn("setDataValidation", kinds)
