"""Google Sheets layer (service account).

Tabs it manages:
  RAW_DATA       every distinct job found (before any filtering)
  DEDUPE_INDEX   hidden; one row per dedupe key ever seen (disaster recovery)
  CONFIG         reusable form answers (notice period, CTC, ...)

Values are written with value_input_option="RAW" so scraped text starting with
'=' can never be evaluated as a formula.
"""
from __future__ import annotations

import json
import logging
import random
import time
from pathlib import Path
from typing import Callable, Iterable

from .models import DEDUPE_HEADERS, RAW_HEADERS

log = logging.getLogger("jobbot.sheets")

RAW_TAB, DEDUPE_TAB, CONFIG_TAB = "RAW_DATA", "DEDUPE_INDEX", "CONFIG"

_COL_WIDTHS = {
    "Found On": 95, "Source": 85, "Company": 170, "Job Title": 250, "Location": 150,
    "Work Mode": 85, "Job Type": 95, "Experience (from JD)": 130, "Salary": 130,
    "Posted On": 95, "Job Link": 220, "Apply Link": 220, "ATS": 90, "Job ID": 130,
    "Search Term": 170, "Job Description": 400, "Dedupe Key": 260,
    "First Seen": 100,
}


def _retry(fn: Callable, *args, tries: int = 6, **kwargs):
    """Retry on rate limits (429), server errors and network hiccups."""
    for attempt in range(tries):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            code = getattr(getattr(exc, "response", None), "status_code", None)
            retriable = code in (429, 500, 502, 503, 504) or isinstance(
                exc, (OSError, TimeoutError))
            if not retriable or attempt == tries - 1:
                raise
            wait = min(90, 2 ** attempt * 3) + random.random()
            log.warning("Sheets call failed (%s); retry %d/%d in %.0fs",
                        code or type(exc).__name__, attempt + 1, tries - 1, wait)
            time.sleep(wait)


class SheetsClient:
    def __init__(self, credentials_file: str, spreadsheet_id: str = "",
                 spreadsheet_name: str = ""):
        self.credentials_file = credentials_file
        self.spreadsheet_id = spreadsheet_id
        self.spreadsheet_name = spreadsheet_name
        self.sh = None

    # ---------------------------------------------------------------- connect
    def connect(self) -> None:
        import gspread  # lazy: keeps the rest of the bot importable without it

        creds = Path(self.credentials_file)
        if not creds.exists():
            raise RuntimeError(
                f"Service-account key not found at '{creds}'. See README, Phase 0, step 3.")
        email = json.loads(creds.read_text()).get("client_email", "<service-account-email>")
        gc = gspread.service_account(filename=str(creds))
        try:
            if self.spreadsheet_id:
                self.sh = _retry(gc.open_by_key, self.spreadsheet_id)
            else:
                self.sh = _retry(gc.open, self.spreadsheet_name)
        except gspread.exceptions.SpreadsheetNotFound as exc:
            raise RuntimeError(
                "Spreadsheet not found. Create it in your Google Drive and share it "
                f"(Editor) with: {email}") from exc
        except gspread.exceptions.APIError as exc:
            if getattr(exc.response, "status_code", None) in (403, 404):
                raise RuntimeError(
                    "No access to the spreadsheet. Share it (Editor) with: "
                    f"{email}  - and check SHEET_ID in .env.") from exc
            raise
        log.info("Connected to spreadsheet '%s'", self.sh.title)

    # ---------------------------------------------------------------- layout
    def ensure_layout(self, answers: dict[str, str] | None = None) -> None:
        self._ensure_tab(RAW_TAB, RAW_HEADERS)
        dedupe = self._ensure_tab(DEDUPE_TAB, DEDUPE_HEADERS)
        self._ensure_tab(CONFIG_TAB, ["Setting", "Value"])
        self._hide(dedupe)
        if answers:
            self._write_config(answers)
        # Remove the default empty "Sheet1" if we created everything else
        try:
            default = self.sh.worksheet("Sheet1")
            if not any(any(row) for row in default.get_all_values()):
                _retry(self.sh.del_worksheet, default)
        except Exception:  # noqa: BLE001 - cosmetic only
            pass
        log.info("Sheet layout ready: %s, %s, %s", RAW_TAB, DEDUPE_TAB, CONFIG_TAB)

    def _ensure_tab(self, title: str, headers: list[str]):
        import gspread
        try:
            ws = _retry(self.sh.worksheet, title)
            created = False
        except gspread.exceptions.WorksheetNotFound:
            ws = _retry(self.sh.add_worksheet, title=title, rows=1000,
                        cols=max(len(headers), 10))
            created = True
        first = _retry(ws.row_values, 1)
        if not first:
            _retry(ws.update, range_name="A1", values=[headers])
            created = True
        elif first[:len(headers)] != headers:
            log.warning("Tab %s has a different header row; leaving it untouched.", title)
            return ws
        if created:
            self._format(ws, headers)
        return ws

    def _format(self, ws, headers: list[str]) -> None:
        sid, n = ws.id, len(headers)
        reqs = [
            {"updateSheetProperties": {
                "properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": 1}},
                "fields": "gridProperties.frozenRowCount"}},
            {"repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1,
                          "startColumnIndex": 0, "endColumnIndex": n},
                "cell": {"userEnteredFormat": {
                    "backgroundColor": {"red": 0.13, "green": 0.25, "blue": 0.45},
                    "textFormat": {"bold": True, "foregroundColor":
                                   {"red": 1, "green": 1, "blue": 1}},
                    "verticalAlignment": "MIDDLE", "wrapStrategy": "CLIP"}},
                "fields": "userEnteredFormat(backgroundColor,textFormat,"
                          "verticalAlignment,wrapStrategy)"}},
            {"repeatCell": {   # keep long JD text from exploding row heights
                "range": {"sheetId": sid, "startRowIndex": 1,
                          "startColumnIndex": 0, "endColumnIndex": n},
                "cell": {"userEnteredFormat": {"wrapStrategy": "CLIP",
                                               "verticalAlignment": "TOP"}},
                "fields": "userEnteredFormat(wrapStrategy,verticalAlignment)"}},
        ]
        for i, h in enumerate(headers):
            reqs.append({"updateDimensionProperties": {
                "range": {"sheetId": sid, "dimension": "COLUMNS",
                          "startIndex": i, "endIndex": i + 1},
                "properties": {"pixelSize": _COL_WIDTHS.get(h, 140)},
                "fields": "pixelSize"}})
        _retry(self.sh.batch_update, {"requests": reqs})

    def _hide(self, ws) -> None:
        _retry(self.sh.batch_update, {"requests": [{"updateSheetProperties": {
            "properties": {"sheetId": ws.id, "hidden": True}, "fields": "hidden"}}]})

    def _write_config(self, answers: dict[str, str]) -> None:
        ws = _retry(self.sh.worksheet, CONFIG_TAB)
        existing = {r[0] for r in _retry(ws.get_all_values)[1:] if r}
        new = [[k, v] for k, v in answers.items() if k not in existing]  # never overwrite edits
        if new:
            _retry(ws.append_rows, new, value_input_option="RAW")

    # ---------------------------------------------------------------- daily tab (Phase 3)
    def ensure_daily_tab(self, day: str):
        import gspread
        from .daily import DAILY_HEADERS, daily_format_requests
        try:
            ws = _retry(self.sh.worksheet, day)
            self.repair_daily_format(ws)          # self-heal: formatting/dropdown missing or half-applied
            return ws
        except gspread.exceptions.WorksheetNotFound:
            pass
        ws = _retry(self.sh.add_worksheet, title=day, rows=1000, cols=len(DAILY_HEADERS), index=0)
        _retry(ws.update, range_name="A1", values=[DAILY_HEADERS])
        _retry(self.sh.batch_update, {"requests": daily_format_requests(ws.id)})
        return ws

    def repair_daily_format(self, ws) -> None:
        """(Re)apply header colours, widths, status dropdown and status colours to a dated tab.
        Safe to repeat: old colour rules are removed first so they never stack."""
        from .daily import DAILY_HEADERS, daily_format_requests
        try:
            if not _retry(ws.row_values, 1):
                _retry(ws.update, range_name="A1", values=[DAILY_HEADERS])
            reqs = []
            meta = _retry(self.sh.fetch_sheet_metadata)
            for s in meta.get("sheets", []):
                if s.get("properties", {}).get("sheetId") == ws.id:
                    reqs += [{"deleteConditionalFormatRule": {"sheetId": ws.id, "index": 0}}
                             for _ in s.get("conditionalFormats", [])]
            _retry(self.sh.batch_update, {"requests": reqs + daily_format_requests(ws.id)})
        except Exception as exc:  # noqa: BLE001 - cosmetic; never block the data
            log.warning("Could not (re)apply sheet formatting on %s: %s", getattr(ws, "title", "?"), str(exc)[:120])

    def append_daily(self, day: str, rows: list, chunk: int = 100) -> None:
        ws = _retry(self.sh.worksheet, day)
        for i in range(0, len(rows), chunk):
            _retry(ws.append_rows, rows[i:i + chunk], value_input_option="USER_ENTERED",
                   insert_data_option="INSERT_ROWS", table_range="A1")
            if i + chunk < len(rows):
                time.sleep(1.2)

    def update_contacts(self, day: str, updates: dict) -> int:
        """updates = {job key: (contact text, linkedin url)}; rows are found by the hidden Key
        column, so sorting or filtering the sheet never misplaces a contact."""
        from gspread.utils import rowcol_to_a1
        from .daily import COL, KEY_HEADER, cell, link
        ws = _retry(self.sh.worksheet, day)
        pos = {k: i + 1 for i, k in enumerate(_retry(ws.col_values, COL[KEY_HEADER])) if k}
        c1, c2 = COL["Contact"], COL["Contact LinkedIn"]
        data = [{"range": f"{rowcol_to_a1(pos[k], c1)}:{rowcol_to_a1(pos[k], c2)}",
                 "values": [[cell(name), link(url)]]}
                for k, (name, url) in updates.items() if k in pos]
        if data:
            _retry(ws.batch_update, data, value_input_option="USER_ENTERED")
        return len(data)

    def update_apply(self, day: str, updates: dict) -> int:
        """updates = {key: (status or None, note, applied_at)}. Only rows still at 'New' or
        'Manual Apply Needed' with Apply Type 'Auto' get a new status, so your own edits win."""
        from .daily import COL, KEY_HEADER, cell
        ws = _retry(self.sh.worksheet, day)
        keys = _retry(ws.col_values, COL[KEY_HEADER])
        stat = _retry(ws.col_values, COL["Status"])
        atype = _retry(ws.col_values, COL["Apply Type"])
        g = lambda col, i: col[i] if i < len(col) else ""  # noqa: E731
        data, n = [], 0
        def L(h):  # column letter
            from gspread.utils import rowcol_to_a1
            return rowcol_to_a1(1, COL[h])[:-1]
        for i, k in enumerate(keys):
            if k not in updates or i == 0:
                continue
            status, note, at = updates[k]
            r = i + 1
            if g(stat, i) in ("", "New", "Manual Apply Needed", "Auto-Apply Failed - Apply Manually") and g(atype, i) == "Auto":
                if status:
                    data.append({"range": f"{L('Status')}{r}:{L('Status')}{r}", "values": [[status]]})
                    data.append({"range": f"{L('Applied At')}{r}:{L('Applied At')}{r}", "values": [[at]]})
                data.append({"range": f"{L('Apply Notes')}{r}:{L('Apply Notes')}{r}", "values": [[cell(note)]]})
                n += 1
        for j in range(0, len(data), 200):
            _retry(ws.batch_update, data[j:j + 200], value_input_option="USER_ENTERED")
        return n

    # ---------------------------------------------------------------- data
    def load_dedupe_keys(self) -> set[str]:
        ws = _retry(self.sh.worksheet, DEDUPE_TAB)
        return set(_retry(ws.col_values, 1)[1:])

    def append_raw(self, rows: list[list[str]]) -> None:
        self._append(RAW_TAB, rows)

    def append_dedupe(self, rows: list[list[str]]) -> None:
        self._append(DEDUPE_TAB, rows)

    def _append(self, tab: str, rows: Iterable[list[str]], chunk: int = 300) -> None:
        rows = list(rows)
        if not rows:
            return
        ws = _retry(self.sh.worksheet, tab)
        for i in range(0, len(rows), chunk):
            _retry(ws.append_rows, rows[i:i + chunk], value_input_option="RAW",
                   insert_data_option="INSERT_ROWS", table_range="A1")
            if i + chunk < len(rows):
                time.sleep(1.2)  # stay under per-minute write quota
