import sys
import types
import unittest
from unittest import mock

from test_phase1 import mk
from test_sheets import FakeSpreadsheet, WorksheetNotFound
from jobbot.daily import (COL, DAILY_HEADERS, STATUSES, cell, daily_format_requests,
                          run_people, sync_daily_tab)
from jobbot.people import LinkedInBlocked
from jobbot.sheets import SheetsClient
from jobbot.store import Store

DAY = "2026-11-12"


def rowcol_to_a1(row, col):
    letters = ""
    while col:
        col, rem = divmod(col - 1, 26)
        letters = chr(65 + rem) + letters
    return f"{letters}{row}"


def add(store, i, **kw):
    base = dict(title=f"Product Analyst {i}", company=f"Co{i}", job_id=f"{4000 + i}",
                url=f"https://www.linkedin.com/jobs/view/{4000 + i}")
    base.update(kw)
    j = mk(**base)
    rid = store.add_job(j, DAY, j.raw_row(DAY), [])
    store.mark_shortlisted(rid, day=DAY, score=80.0 - i, tier="1-Core", family="product",
                           matched="Jira", missing="Zendesk",
                           apply_type="Auto" if j.ats in ("lever", "greenhouse") else "Manual")
    store.set_resume(rid, path=f"C:\\out\\r{i}.pdf", engine="offline", jd_summary="Short summary")
    return rid


class FakeFinder:
    def __init__(self, result=None, blocked_on=None):
        self.result, self.blocked_on, self.calls = result, blocked_on, []

    def find(self, company):
        self.calls.append(company)
        if company == self.blocked_on:
            raise LinkedInBlocked("checkpoint")
        return self.result if self.result is not None else []


class TestDailyTab(unittest.TestCase):
    def setUp(self):
        gs = types.ModuleType("gspread")
        gs.exceptions = types.SimpleNamespace(WorksheetNotFound=WorksheetNotFound)
        gu = types.ModuleType("gspread.utils")
        gu.rowcol_to_a1 = rowcol_to_a1
        gs.utils = gu
        p = mock.patch.dict(sys.modules, {"gspread": gs, "gspread.utils": gu})
        p.start()
        self.addCleanup(p.stop)
        self.sheets = SheetsClient("x")
        self.sheets.sh = FakeSpreadsheet()
        self.store = Store(":memory:")

    def tab(self):
        return self.sheets.sh.sheets[DAY]

    def test_tab_rows_dropdown_and_links(self):
        for i in range(3):
            add(self.store, i)
        self.assertEqual(sync_daily_tab(self.store, self.sheets, DAY), 3)
        rows = self.tab().rows
        self.assertEqual(rows[0], DAILY_HEADERS)
        self.assertEqual(len(rows), 4)
        self.assertEqual([r[COL["#"] - 1] for r in rows[1:]], [1, 2, 3])
        self.assertEqual(rows[1][COL["Status"] - 1], "Manual Apply Needed")
        self.assertTrue(rows[1][COL["Job Link"] - 1].startswith('=HYPERLINK("https://www.linkedin.com'))
        self.assertEqual(rows[1][COL["Job ID"] - 1], "'4000")          # kept as text
        self.assertEqual(rows[1][COL["Resume File"] - 1], "C:\\out\\r0.pdf")
        body = self.sheets.sh.batches[-1]["requests"]
        validation = [r for r in body if "setDataValidation" in r][0]["setDataValidation"]
        self.assertEqual([v["userEnteredValue"] for v in validation["rule"]["condition"]["values"]],
                         STATUSES)

    def test_idempotent_numbering_and_user_edits_survive(self):
        add(self.store, 0)
        sync_daily_tab(self.store, self.sheets, DAY)
        self.tab().rows[1][COL["Status"] - 1] = "Applied (Manual)"      # user edits the sheet
        self.assertEqual(sync_daily_tab(self.store, self.sheets, DAY), 0)
        add(self.store, 1)
        self.assertEqual(sync_daily_tab(self.store, self.sheets, DAY), 1)
        rows = self.tab().rows
        self.assertEqual((len(rows), rows[1][COL["Status"] - 1], rows[2][COL["#"] - 1]),
                         (3, "Applied (Manual)", 2))

    def test_status_follows_auto_switch(self):
        add(self.store, 0, source="lever", url="https://jobs.lever.co/x/1", job_id="l1")
        sync_daily_tab(self.store, self.sheets, DAY, auto_enabled=True)
        self.assertEqual(self.tab().rows[1][COL["Status"] - 1], "New")

    def test_cell_escaping(self):
        self.assertEqual(cell("=SUM(A1)"), "'=SUM(A1)")
        self.assertEqual(cell("- bullet"), "'- bullet")
        self.assertEqual((cell(""), cell(None), cell(7), cell("Pune")), ("", "", 7, "Pune"))

    def test_format_requests_cover_all_status_colours(self):
        reqs = daily_format_requests(5)
        self.assertEqual(sum("addConditionalFormatRule" in r for r in reqs), len(STATUSES))
        self.assertTrue(any("hiddenByUser" in str(r) for r in reqs))

    def test_people_fill_contacts_once_and_find_rows_by_key(self):
        for i in range(2):
            add(self.store, i)
        sync_daily_tab(self.store, self.sheets, DAY)
        self.tab().rows[1:] = self.tab().rows[1:][::-1]                  # user sorts the sheet
        finder = FakeFinder([("Jane Doe Technical Recruiter at Co0", "https://www.linkedin.com/in/jane")])
        st = run_people(self.store, self.sheets, finder, DAY)
        self.assertEqual((st["searched"], st["found"], st["pushed"]), (2, 2, 2))
        for r in self.tab().rows[1:]:
            self.assertIn("Jane Doe", r[COL["Contact"] - 1])
            self.assertIn("linkedin.com/in/jane", r[COL["Contact LinkedIn"] - 1])
        self.assertEqual(run_people(self.store, self.sheets, finder, DAY)["searched"], 0)

    def test_people_not_found_is_blank_and_not_retried(self):
        add(self.store, 0)
        sync_daily_tab(self.store, self.sheets, DAY)
        finder = FakeFinder([])
        run_people(self.store, self.sheets, finder, DAY)
        run_people(self.store, self.sheets, finder, DAY)
        self.assertEqual(len(finder.calls), 1)
        self.assertEqual(self.tab().rows[1][COL["Contact"] - 1], "")

    def test_linkedin_block_stops_and_retries_later(self):
        for i in range(3):
            add(self.store, i)
        sync_daily_tab(self.store, self.sheets, DAY)
        st = run_people(self.store, self.sheets, FakeFinder(blocked_on="Co1"), DAY)
        self.assertTrue(st["blocked"])
        self.assertEqual(len(self.store.needs_contact(DAY)), 2)         # Co1 and Co2 remain


if __name__ == "__main__":
    unittest.main()
