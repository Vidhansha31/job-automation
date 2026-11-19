"""Phase 1 pipeline: collect -> age filter -> dedupe -> store -> mirror to Sheets."""
from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Protocol

from .collectors import Collector
from .models import RAW_HEADERS, Job
from .store import Store

log = logging.getLogger("jobbot.pipeline")

# Career-page sources keep postings open for weeks, so their first sighting is
# included regardless of age (dedupe guarantees it is only ever listed once).
ATS_SOURCES = {"greenhouse", "lever", "ashby", "smartrecruiters"}


class SheetsLike(Protocol):  # what the pipeline needs from the Sheets layer
    def load_dedupe_keys(self) -> set[str]: ...
    def append_raw(self, rows: list[list[str]]) -> None: ...
    def append_dedupe(self, rows: list[list[str]]) -> None: ...


@dataclass
class RunReport:
    found: int = 0
    invalid: int = 0
    too_old: int = 0
    duplicates: int = 0
    new: int = 0
    synced_rows: int = 0
    new_by_source: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)

    def summary(self) -> str:
        src = ", ".join(f"{k}: {v}" for k, v in sorted(self.new_by_source.items())) or "none"
        lines = [
            f"Collected         : {self.found}",
            f"  invalid         : {self.invalid}",
            f"  older than window: {self.too_old}",
            f"  duplicates      : {self.duplicates}",
            f"New distinct jobs : {self.new}   ({src})",
            f"Rows sent to Sheet: {self.synced_rows}",
        ]
        if self.errors:
            lines.append(f"Problems ({len(self.errors)}):")
            lines += [f"  - {e}" for e in self.errors[:15]]
        return "\n".join(lines)


def within_window(job: Job, hours: int, now: datetime) -> bool:
    """Date-only sources (LinkedIn) have no clock time, so compare by calendar day.
    Jobs with an unknown posting date are kept."""
    if job.posted_at is None:
        return True
    return job.posted_at.date() >= (now - timedelta(hours=hours)).date()


def flush_unsynced(store: Store, sheets: SheetsLike, report: RunReport) -> None:
    """Mirror rows not yet in the sheet. DEDUPE_INDEX first: a duplicate row there is
    harmless, whereas a duplicate in RAW_DATA would be visible clutter."""
    pending = store.unsynced()
    if not pending:
        return
    try:
        sheets.append_dedupe([r for _, _, d in pending for r in d])
        sheets.append_raw([raw for _, raw, _ in pending])
    except Exception as exc:  # noqa: BLE001
        msg = f"Sheet sync failed ({exc}); {len(pending)} rows kept locally and will retry next run"
        log.error(msg)
        report.errors.append(msg)
        return
    store.mark_synced(i for i, _, _ in pending)
    report.synced_rows += len(pending)


def _csv_backup(path: Path, rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if is_new:
            w.writerow(RAW_HEADERS)
        w.writerows(rows)


def run_pipeline(*, store: Store, collectors: list[Collector], sheets: Optional[SheetsLike],
                 hours_old: int, tz: timezone, max_desc: int = 30000,
                 data_dir: str | Path = "data", now: Optional[datetime] = None,
                 ats_backlog: bool = True) -> RunReport:
    now = now or datetime.now(timezone.utc)
    found_on = now.astimezone(tz).strftime("%Y-%m-%d")
    report = RunReport()

    # 0. Recover dedupe memory / retry earlier failed sheet writes
    if sheets is not None:
        try:
            if store.count_keys() == 0:
                n = store.import_keys(sheets.load_dedupe_keys())
                if n:
                    log.info("Recovered %d dedupe keys from the sheet", n)
            flush_unsynced(store, sheets, report)
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"Sheet pre-sync failed: {exc}")

    # 1. Collect (each collector is isolated)
    collected: list[Job] = []
    for c in collectors:
        try:
            got = c.collect()
        except Exception as exc:  # noqa: BLE001 - collectors should not raise, but never die
            log.exception("Collector %s crashed", c.name)
            report.errors.append(f"{c.name} crashed: {exc}")
            got = []
        report.errors += [f"{c.name}: {e}" for e in c.errors]
        log.info("Collector %s returned %d jobs", c.name, len(got))
        collected += got
    report.found = len(collected)

    # 2-4. Validate, age-filter, dedupe, store
    new_rows: list[list[str]] = []
    for job in collected:
        if not job.is_valid:
            report.invalid += 1
            continue
        backlog_ok = ats_backlog and job.source in ATS_SOURCES
        if not backlog_ok and not within_window(job, hours_old, now):
            report.too_old += 1
            continue
        raw_row = job.raw_row(found_on, max_desc)
        dedupe_rows = [[k, found_on, job.source, job.company, job.title] for k in job.keys()]
        if store.add_job(job, found_on, raw_row, dedupe_rows) is None:
            report.duplicates += 1
            continue
        report.new += 1
        report.new_by_source[job.source] = report.new_by_source.get(job.source, 0) + 1
        new_rows.append(raw_row)

    # 5. Local CSV backup (always), then mirror to the sheet
    if new_rows:
        _csv_backup(Path(data_dir) / f"raw_{found_on}.csv", new_rows)
    if sheets is not None:
        flush_unsynced(store, sheets, report)
    return report
