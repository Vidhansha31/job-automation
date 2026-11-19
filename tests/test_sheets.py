"""Tests for the Google Sheets layer using a fake spreadsheet (no network, no gspread).

Run:  python -m unittest discover -s tests -v
"""
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobbot import sheets as sheets_mod
from jobbot.models import DEDUPE_HEADERS, RAW_HEADERS
from jobbot.sheets import CONFIG_TAB, DEDUPE_TAB, RAW_TAB, SheetsClient


class WorksheetNotFound(Exception):
    pass


class FakeWorksheet:
    _next_id = 1

    def __init__(self, title):
        self.title = title
        self.id = FakeWorksheet._next_id
        FakeWorksheet._next_id += 1
        self.rows: list[list[str]] = []

    def row_values(self, n):
        return list(self.rows[n - 1]) if len(self.rows) >= n else []

    def update(self, range_name, values):
        self.rows = [list(r) for r in values] + self.rows[len(values):]

    def get_all_values(self):
        return [list(r) for r in self.rows] or [[]]

    def col_values(self, col):
        return [r[col - 1] for r in self.rows if len(r) >= col]

    def append_rows(self, rows, **kwargs):
        self.rows += [list(r) for r in rows]

    def batch_update(self, data, **kwargs):
        import re as _re
        for item in data:
            m = _re.match(r"([A-Z]+)(\d+):([A-Z]+)(\d+)", item["range"])
            col = 0
            for ch in m.group(1):
                col = col * 26 + ord(ch) - 64
            row = int(m.group(2))
            for j, v in enumerate(item["values"][0]):
                while len(self.rows[row - 1]) < col + j:
                    self.rows[row - 1].append("")
                self.rows[row - 1][col - 1 + j] = v


class FakeSpreadsheet:
    title = "Job Tracker"

    def __init__(self):
        self.sheets = {"Sheet1": FakeWorksheet("Sheet1")}   # Google's default tab
        self.batches = []

    def worksheet(self, title):
        if title not in self.sheets:
            raise WorksheetNotFound(title)
        return self.sheets[title]

    def add_worksheet(self, title, rows, cols, index=None):
        self.sheets[title] = FakeWorksheet(title)
        return self.sheets[title]

    def del_worksheet(self, ws):
        del self.sheets[ws.title]

    def fetch_sheet_metadata(self):
        return {"sheets": [{"properties": {"sheetId": w.id}, "conditionalFormats": [{}, {}]} for w in self.sheets.values()]}

    def batch_update(self, body):
        self.batches.append(body)


ANSWERS = {"Notice Period": "7 days (maximum)", "Expected CTC": "10 LPA"}


class TestSheetsLayer(unittest.TestCase):
    def setUp(self):
        fake = types.ModuleType("gspread")
        fake.exceptions = types.SimpleNamespace(WorksheetNotFound=WorksheetNotFound)
        patcher = mock.patch.dict(sys.modules, {"gspread": fake})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = SheetsClient("unused.json")
        self.client.sh = FakeSpreadsheet()

    def test_ensure_layout_creates_tabs_headers_and_config(self):
        self.client.ensure_layout(ANSWERS)   # raised "'list' object is not callable" before the fix
        sh = self.client.sh
        self.assertEqual(sh.sheets[RAW_TAB].rows[0], RAW_HEADERS)
        self.assertEqual(sh.sheets[DEDUPE_TAB].rows[0], DEDUPE_HEADERS)
        self.assertEqual(sh.sheets[CONFIG_TAB].rows,
                         [["Setting", "Value"], ["Notice Period", "7 days (maximum)"],
                          ["Expected CTC", "10 LPA"]])
        self.assertNotIn("Sheet1", sh.sheets)          # empty default tab removed

    def test_ensure_layout_is_idempotent_and_keeps_user_edits(self):
        self.client.ensure_layout(ANSWERS)
        cfg = self.client.sh.sheets[CONFIG_TAB]
        cfg.rows[1][1] = "15 days"                      # user edits a value in the sheet
        self.client.ensure_layout(ANSWERS)
        self.assertEqual(len(cfg.rows), 3)              # no duplicate rows
        self.assertEqual(cfg.rows[1][1], "15 days")     # never overwritten

    def test_non_empty_default_tab_is_kept(self):
        self.client.sh.sheets["Sheet1"].rows = [["my notes"]]
        self.client.ensure_layout(ANSWERS)
        self.assertIn("Sheet1", self.client.sh.sheets)

    def test_dedupe_keys_roundtrip_and_chunked_append(self):
        self.client.ensure_layout()
        rows = [[f"id:x:{i}", "2026-11-10", "x", "c", "t"] for i in range(650)]
        with mock.patch.object(sheets_mod.time, "sleep"):
            self.client.append_dedupe(rows)
        self.assertEqual(len(self.client.load_dedupe_keys()), 650)

    def test_retry_recovers_from_rate_limit_but_not_other_errors(self):
        class RateLimited(Exception):
            response = types.SimpleNamespace(status_code=429)

        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise RateLimited()
            return "ok"

        with mock.patch.object(sheets_mod.time, "sleep"):
            self.assertEqual(sheets_mod._retry(flaky), "ok")
            self.assertEqual(calls["n"], 3)
            with self.assertRaises(ValueError):
                sheets_mod._retry(lambda: (_ for _ in ()).throw(ValueError("bug")))


if __name__ == "__main__":
    unittest.main()
