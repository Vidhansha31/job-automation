"""Phase 3: today's shortlist -> a dated tab in the Job Tracker sheet (+ recruiter contacts)."""
from __future__ import annotations

import logging

from .models import norm_company
from .people import LinkedInBlocked

log = logging.getLogger("jobbot.daily")

STATUSES = ["New", "Manual Apply Needed", "Applied (Auto)", "Auto-Apply Failed - Apply Manually",
            "Applied (Manual)", "Interview", "Rejected", "Skipped", "Job Expired"]
STATUS_COLORS = {
    "New": (0.85, 0.92, 1.0), "Manual Apply Needed": (1.0, 0.92, 0.72),
    "Applied (Auto)": (0.80, 0.94, 0.80), "Applied (Manual)": (0.80, 0.94, 0.80),
    "Auto-Apply Failed - Apply Manually": (1.0, 0.78, 0.78), "Interview": (0.86, 0.80, 1.0),
    "Rejected": (0.88, 0.88, 0.88), "Skipped": (0.88, 0.88, 0.88), "Job Expired": (0.88, 0.88, 0.88),
}
GROUPS = [   # (group, headers, header colour)
    ("Action", ["#", "Status", "Apply Type", "Apply Notes", "Applied At"], (0.13, 0.25, 0.45)),
    ("Job", ["Company", "Job Title", "Location", "Work Mode", "Experience", "Salary", "Posted On",
             "Source", "Job ID", "Job Link", "Apply Link"], (0.12, 0.38, 0.30)),
    ("Match", ["Match Score", "Match Tier", "Matched Keywords", "Missing Keywords"], (0.55, 0.33, 0.10)),
    ("Documents", ["Resume File", "JD Summary", "Full JD"], (0.38, 0.22, 0.48)),
    ("People", ["Contact", "Contact LinkedIn"], (0.55, 0.20, 0.25)),
    ("Yours", ["Follow-up Date", "My Notes"], (0.30, 0.30, 0.30)),
]
KEY_HEADER = "Key"
DAILY_HEADERS = [h for _, hs, _ in GROUPS for h in hs] + [KEY_HEADER]
COL = {h: i + 1 for i, h in enumerate(DAILY_HEADERS)}          # 1-based column numbers
_WIDTH = {"#": 40, "Status": 190, "Apply Type": 85, "Apply Notes": 220, "Applied At": 100,
          "Company": 160, "Job Title": 240, "Location": 150, "Work Mode": 85, "Experience": 100,
          "Salary": 120, "Posted On": 95, "Source": 80, "Job ID": 100, "Job Link": 90,
          "Apply Link": 110, "Match Score": 80, "Match Tier": 100, "Matched Keywords": 220,
          "Missing Keywords": 220, "Resume File": 300, "JD Summary": 320, "Full JD": 220,
          "Contact": 260, "Contact LinkedIn": 220, "Follow-up Date": 110, "My Notes": 220, KEY_HEADER: 60}


def cell(v):
    """Make a value safe for USER_ENTERED writes (no accidental formulas / numbers / dates)."""
    if v is None:
        return ""
    if isinstance(v, (int, float)):
        return v
    s = str(v)
    return "'" + s if s and (s[0] in "=+-@" or s.isdigit()) else s


def link(url: str, label: str = "") -> str:
    url = (url or "").strip().replace('"', "%22")
    return f'=HYPERLINK("{url}","{(label or url).replace(chr(34), "")}")' if url else ""


def daily_row(r, idx: int, auto_enabled: bool, max_jd: int = 20000) -> list:
    auto = r["apply_type"] == "Auto" and auto_enabled
    if auto:
        status, note = "New", ""
    elif r["apply_type"] == "Auto":
        status, note = "Manual Apply Needed", "Auto-apply not switched on yet - apply manually"
    else:
        status, note = "Manual Apply Needed", f"Apply manually ({r['ats'] or r['source']})"
    values = {
        "#": idx, "Status": status, "Apply Type": r["apply_type"] or "", "Apply Notes": note,
        "Applied At": "", "Company": r["company"], "Job Title": r["title"],
        "Location": r["location"], "Work Mode": r["work_mode"], "Experience": r["experience"],
        "Salary": r["salary"], "Posted On": (r["posted_at"] or "")[:10], "Source": r["source"],
        "Job ID": r["job_id"], "Match Score": r["score"], "Match Tier": r["tier"],
        "Matched Keywords": r["matched_kw"], "Missing Keywords": r["missing_kw"],
        "Resume File": r["resume_path"] or "", "JD Summary": r["jd_summary"],
        "Full JD": (r["description"] or "")[:max_jd], "Contact": r["contact_name"] or "",
        "Follow-up Date": "", "My Notes": "", KEY_HEADER: r["primary_key"],
    }
    row = [cell(values.get(h, "")) for h in DAILY_HEADERS]
    row[COL["Job Link"] - 1] = link(r["url"], "Open job")
    apply_url = r["apply_url"] if r["apply_url"] != r["url"] else ""
    row[COL["Apply Link"] - 1] = link(apply_url, "Open apply page")
    row[COL["Contact LinkedIn"] - 1] = link(r["contact_url"] or "")
    return row


def _rgb(c):
    return {"red": c[0], "green": c[1], "blue": c[2]}


def daily_format_requests(sid: int) -> list:
    """Sheets API requests: frozen header, coloured groups, widths, dropdown, status colours."""
    n, reqs, col = len(DAILY_HEADERS), [], 0
    reqs.append({"updateSheetProperties": {
        "properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": 1, "frozenColumnCount": 2}},
        "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount"}})
    groups = GROUPS + [("Key", [KEY_HEADER], (0.5, 0.5, 0.5))]
    for _, hs, color in groups:
        reqs.append({"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1,
                      "startColumnIndex": col, "endColumnIndex": col + len(hs)},
            "cell": {"userEnteredFormat": {
                "backgroundColor": _rgb(color), "verticalAlignment": "MIDDLE",
                "textFormat": {"bold": True, "foregroundColor": _rgb((1, 1, 1))}}},
            "fields": "userEnteredFormat(backgroundColor,verticalAlignment,textFormat)"}})
        col += len(hs)
    reqs.append({"repeatCell": {
        "range": {"sheetId": sid, "startRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": n},
        "cell": {"userEnteredFormat": {"wrapStrategy": "CLIP", "verticalAlignment": "TOP"}},
        "fields": "userEnteredFormat(wrapStrategy,verticalAlignment)"}})
    for i, h in enumerate(DAILY_HEADERS):
        reqs.append({"updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": i, "endIndex": i + 1},
            "properties": {"pixelSize": _WIDTH.get(h, 130)}, "fields": "pixelSize"}})
    k = COL[KEY_HEADER] - 1
    reqs.append({"updateDimensionProperties": {
        "range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": k, "endIndex": k + 1},
        "properties": {"hiddenByUser": True}, "fields": "hiddenByUser"}})
    s = COL["Status"] - 1
    status_range = {"sheetId": sid, "startRowIndex": 1, "endRowIndex": 1000,
                    "startColumnIndex": s, "endColumnIndex": s + 1}
    reqs.append({"setDataValidation": {"range": status_range, "rule": {
        "condition": {"type": "ONE_OF_LIST",
                      "values": [{"userEnteredValue": v} for v in STATUSES]},
        "showCustomUi": True, "strict": True}}})
    for status, color in STATUS_COLORS.items():
        reqs.append({"addConditionalFormatRule": {"index": 0, "rule": {
            "ranges": [status_range],
            "booleanRule": {"condition": {"type": "TEXT_EQ", "values": [{"userEnteredValue": status}]},
                            "format": {"backgroundColor": _rgb(color)}}}}})
    return reqs


def sync_daily_tab(store, sheets, day: str, auto_enabled: bool = False) -> int:
    """Append today's not-yet-written shortlisted jobs to the dated tab. Never touches rows
    already there, so your status changes and notes are safe."""
    rows = store.unsynced_daily(day)
    if not rows:
        return 0
    start = store.count_daily_synced(day)
    sheets.ensure_daily_tab(day)
    sheets.append_daily(day, [daily_row(r, start + i + 1, auto_enabled) for i, r in enumerate(rows)])
    store.mark_daily_synced([r["id"] for r in rows])
    return len(rows)


def run_people(store, sheets, finder, day: str, *, max_per_day: int = 20) -> dict:
    """Find one recruiter per company for today's jobs and fill the Contact columns."""
    stats = {"searched": 0, "found": 0, "blocked": False, "pushed": 0}
    cache: dict = {}
    for r in store.needs_contact(day)[:max_per_day]:
        key = norm_company(r["company"])
        if key not in cache:
            try:
                cache[key] = finder.find(r["company"])
                stats["searched"] += 1
            except LinkedInBlocked as exc:
                log.warning("LinkedIn asked for verification (%s); stopping contact lookup", exc)
                stats["blocked"] = True
                break
            except Exception as exc:  # noqa: BLE001 - retried on a later run
                log.warning("Contact lookup failed for %s: %s", r["company"], str(exc)[:100])
                cache[key] = None
        found = cache[key]
        if found is None:
            continue
        name, url = (found[0] if found else ("", ""))
        stats["found"] += 1 if found else 0
        store.set_contact(r["id"], name=name, url=url, day=day)
    pending = store.contacts_to_push(day)
    if pending:
        sheets.update_contacts(day, {r["primary_key"]: (r["contact_name"], r["contact_url"])
                                     for r in pending})
        store.mark_contacts_pushed([r["id"] for r in pending])
        stats["pushed"] = len(pending)
    return stats
