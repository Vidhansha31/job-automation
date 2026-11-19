"""Runs auto-apply for pending jobs, records every outcome locally, mirrors it to the sheet."""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .applier import Result

log = logging.getLogger("jobbot.apply")
IST = timezone(timedelta(hours=5, minutes=30))
STATUS = {"applied": "Applied (Auto)", "failed": "Auto-Apply Failed - Apply Manually",
          "unconfirmed": "Auto-Apply Failed - Apply Manually", "expired": "Job Expired",
          "dryrun": "DRYRUN"}


@dataclass
class ApplyReport:
    pending: int = 0
    counts: dict = field(default_factory=dict)
    pushed: int = 0
    skipped_cap: int = 0
    errors: list = field(default_factory=list)
    dry_run: bool = False

    def summary(self) -> str:
        c = ", ".join(f"{k}: {v}" for k, v in sorted(self.counts.items())) or "none"
        lines = [f"Auto-apply ({'DRY RUN' if self.dry_run else 'LIVE'}): {self.pending} pending, results: {c}",
                 f"Sheet rows updated: {self.pushed}"]
        if self.skipped_cap:
            lines.append(f"Daily cap reached - {self.skipped_cap} job(s) left for the next run")
        lines += [f"  ! {e}" for e in self.errors[:10]]
        return "\n".join(lines)


def push_results(store, sheets, since_day: str, report: ApplyReport) -> None:
    if sheets is None:
        return
    by_day: dict = {}
    for r in store.apply_unsynced(since_day):
        by_day.setdefault(r["shortlisted_on"], []).append(r)
    for day, rows in by_day.items():
        updates = {r["primary_key"]: (None if r["apply_status"] == "DRYRUN" else r["apply_status"],
                                      r["apply_note"] or "", r["applied_at"] or "") for r in rows}
        try:
            sheets.update_apply(day, updates)
        except Exception as exc:  # noqa: BLE001 - kept locally, retried next run
            report.errors.append(f"Sheet update for {day} failed: {str(exc)[:100]}")
            continue
        store.mark_apply_synced([r["id"] for r in rows])
        report.pushed += len(rows)


def run_apply(*, store, sheets, applier, ctx, today: str, since_day: str, dry_run: bool,
              limit=None, max_per_day: int = 25, delay=(15, 40), sleep=time.sleep) -> ApplyReport:
    rep = ApplyReport(dry_run=dry_run)
    rows = store.apply_pending(since_day)
    rep.pending = len(rows)
    if not dry_run:
        cap = max(0, max_per_day - store.count_apply_done(today))
        rep.skipped_cap = max(0, len(rows) - cap)
        rows = rows[:cap]
    if limit:
        rows = rows[:limit]
    try:
        for i, row in enumerate(rows):
            ctx.unknown = []
            resume = row["resume_path"] or ""
            if not resume or not Path(resume).exists():
                res = Result("failed", "Resume PDF missing - run `python run.py shortlist --regen`")
            else:
                try:
                    res = applier.apply(dict(row), ctx, resume, dry_run)
                except Exception as exc:  # noqa: BLE001 - never let one job stop the batch
                    log.exception("Apply crashed for %s", row["company"])
                    res = Result("failed", f"Unexpected error: {str(exc)[:120]}")
            try:
                ctx.flush_unknown()
            except Exception:  # noqa: BLE001
                log.warning("Could not update profile/my_answers.yaml")
            note = res.note + (f" | screenshot: {res.shot}" if res.shot else "")
            now = datetime.now(IST).strftime("%Y-%m-%d %H:%M")
            store.set_apply(row["id"], status=STATUS[res.status], note=note[:400],
                            applied_at=now if res.status == "applied" else "",
                            attempts=res.attempts, ran_on=today)
            rep.counts[res.status] = rep.counts.get(res.status, 0) + 1
            log.info("%s | %s -> %s %s", row["company"], row["title"], res.status, res.note)
            if i < len(rows) - 1:
                sleep(random.uniform(3, 6) if dry_run else random.uniform(*delay))
    finally:
        push_results(store, sheets, since_day, rep)       # results are never lost, even on a crash
    return rep
