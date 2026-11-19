"""Local SQLite store: source of truth for what has been seen.

Writes happen here FIRST (crash-safe), then are mirrored to Google Sheets.
Rows not yet mirrored stay `sheet_synced = 0` and are retried on the next run.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable, Optional

from .models import Job

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    found_on      TEXT NOT NULL,
    source        TEXT, job_id TEXT, company TEXT, title TEXT, location TEXT,
    work_mode     TEXT, job_type TEXT, experience TEXT, salary TEXT,
    posted_at     TEXT, url TEXT, apply_url TEXT, ats TEXT, search_term TEXT,
    description   TEXT,
    primary_key   TEXT UNIQUE,
    status        TEXT NOT NULL DEFAULT 'raw',   -- raw | shortlisted (Phase 2)
    sheet_synced  INTEGER NOT NULL DEFAULT 0,
    raw_row_json  TEXT, dedupe_rows_json TEXT
);
CREATE TABLE IF NOT EXISTS seen_keys (
    key TEXT PRIMARY KEY,
    job_rowid INTEGER
);
CREATE INDEX IF NOT EXISTS idx_jobs_found_on ON jobs(found_on);
"""


# Phase 2 columns, added in place so an existing jobs.db keeps all its data.
NEW_COLUMNS = {
    "score": "REAL", "tier": "TEXT", "family": "TEXT", "shortlisted_on": "TEXT",
    "matched_kw": "TEXT", "missing_kw": "TEXT", "jd_summary": "TEXT", "apply_type": "TEXT",
    "resume_path": "TEXT", "resume_engine": "TEXT", "resume_error": "TEXT",
    # Phase 3
    "daily_synced": "INTEGER DEFAULT 0", "contact_name": "TEXT", "contact_url": "TEXT",
    "contact_checked_on": "TEXT", "contact_pushed": "INTEGER DEFAULT 0",
    "apply_status": "TEXT", "apply_note": "TEXT", "applied_at": "TEXT", "apply_attempts": "INTEGER DEFAULT 0",
    "apply_synced": "INTEGER DEFAULT 0", "apply_ran_on": "TEXT",
}


class Store:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        have = {r["name"] for r in self.db.execute("PRAGMA table_info(jobs)")}
        with self.db:
            for col, typ in NEW_COLUMNS.items():
                if col not in have:
                    self.db.execute(f"ALTER TABLE jobs ADD COLUMN {col} {typ}")

    # ------------------------------------------------------------ shortlist (Phase 2)
    def pool(self, since_day: str) -> list[sqlite3.Row]:
        """Jobs found since `since_day` that have not been shortlisted yet."""
        return self.db.execute(
            "SELECT * FROM jobs WHERE status = 'raw' AND found_on >= ? ORDER BY id",
            (since_day,)).fetchall()

    def mark_shortlisted(self, rowid: int, *, day: str, score: float, tier: str, family: str,
                         matched: str, missing: str, apply_type: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE jobs SET status='shortlisted', shortlisted_on=?, score=?, tier=?, "
                "family=?, matched_kw=?, missing_kw=?, apply_type=? WHERE id=?",
                (day, score, tier, family, matched, missing, apply_type, rowid))

    def shortlisted(self, day: str) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM jobs WHERE shortlisted_on = ? ORDER BY "
            "tier, score DESC, id", (day,)).fetchall()

    def count_shortlisted(self, day: str) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM jobs WHERE shortlisted_on = ?", (day,)).fetchone()[0]

    def set_resume(self, rowid: int, *, path: str = "", engine: str = "",
                   jd_summary: str = "", error: str = "") -> None:
        with self.db:
            self.db.execute(
                "UPDATE jobs SET resume_path=?, resume_engine=?, jd_summary=?, resume_error=? "
                "WHERE id=?", (path, engine, jd_summary, error, rowid))

    def clear_resumes(self, day: str) -> None:
        with self.db:
            self.db.execute("UPDATE jobs SET resume_path='', resume_error='' "
                            "WHERE shortlisted_on=?", (day,))

    # ------------------------------------------------------------ daily tab + contacts (Phase 3)
    _ORDER = " ORDER BY tier, score DESC, id"

    def unsynced_daily(self, day: str) -> list:
        return self.db.execute("SELECT * FROM jobs WHERE shortlisted_on=? AND "
                               "COALESCE(daily_synced,0)=0" + self._ORDER, (day,)).fetchall()

    def count_daily_synced(self, day: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM jobs WHERE shortlisted_on=? AND "
                               "daily_synced=1", (day,)).fetchone()[0]

    def mark_daily_synced(self, ids) -> None:
        with self.db:
            self.db.executemany("UPDATE jobs SET daily_synced=1 WHERE id=?", [(i,) for i in ids])

    def needs_contact(self, day: str) -> list:
        return self.db.execute("SELECT * FROM jobs WHERE shortlisted_on=? AND daily_synced=1 AND "
                               "COALESCE(contact_checked_on,'')=''" + self._ORDER, (day,)).fetchall()

    def set_contact(self, rowid: int, *, name: str, url: str, day: str) -> None:
        with self.db:
            self.db.execute("UPDATE jobs SET contact_name=?, contact_url=?, contact_checked_on=?, "
                            "contact_pushed=0 WHERE id=?", (name, url, day, rowid))

    def contacts_to_push(self, day: str) -> list:
        return self.db.execute("SELECT * FROM jobs WHERE shortlisted_on=? AND daily_synced=1 AND "
                               "COALESCE(contact_url,'')!='' AND COALESCE(contact_pushed,0)=0",
                               (day,)).fetchall()

    def mark_contacts_pushed(self, ids) -> None:
        with self.db:
            self.db.executemany("UPDATE jobs SET contact_pushed=1 WHERE id=?", [(i,) for i in ids])

    # ---- auto-apply (Phase 4)
    def apply_pending(self, since_day: str) -> list:
        return self.db.execute(
            "SELECT * FROM jobs WHERE shortlisted_on>=? AND apply_type='Auto' AND daily_synced=1 AND "
            "COALESCE(apply_status,'') IN ('','DRYRUN') AND COALESCE(resume_path,'')!=''" + self._ORDER,
            (since_day,)).fetchall()

    def apply_overview(self, since_day: str) -> str:
        rows = self.db.execute("SELECT source, apply_type, daily_synced, COALESCE(apply_status,'') st, "
                               "COALESCE(resume_path,'')!='' hasres, COUNT(*) n FROM jobs WHERE shortlisted_on>=? "
                               "GROUP BY 1,2,3,4,5", (since_day,)).fetchall()
        return "; ".join(f"{r['source']}/{r['apply_type']}/synced={r['daily_synced']}/status='{r['st']}'/"
                         f"resume={'y' if r['hasres'] else 'n'}: {r['n']}" for r in rows) or "no shortlisted jobs"

    def reset_failed(self, since_day: str) -> int:
        with self.db:
            return self.db.execute("UPDATE jobs SET apply_status='', apply_synced=1 WHERE shortlisted_on>=? AND "
                                   "apply_status='Auto-Apply Failed - Apply Manually'", (since_day,)).rowcount

    def count_apply_done(self, day: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM jobs WHERE apply_ran_on=? AND "
                               "apply_status IN ('Applied (Auto)','Auto-Apply Failed - Apply Manually')",
                               (day,)).fetchone()[0]

    def set_apply(self, rowid: int, *, status: str, note: str, applied_at: str, attempts: int, ran_on: str) -> None:
        with self.db:
            self.db.execute("UPDATE jobs SET apply_status=?, apply_note=?, applied_at=?, apply_attempts=?, "
                            "apply_ran_on=?, apply_synced=0 WHERE id=?",
                            (status, note, applied_at, attempts, ran_on, rowid))

    def apply_unsynced(self, since_day: str) -> list:
        return self.db.execute("SELECT * FROM jobs WHERE shortlisted_on>=? AND COALESCE(apply_status,'')!='' "
                               "AND COALESCE(apply_synced,0)=0", (since_day,)).fetchall()

    def mark_apply_synced(self, ids) -> None:
        with self.db:
            self.db.executemany("UPDATE jobs SET apply_synced=1 WHERE id=?", [(i,) for i in ids])

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------ dedupe
    def any_seen(self, keys: Iterable[str]) -> bool:
        keys = list(keys)
        if not keys:
            return False
        marks = ",".join("?" * len(keys))
        row = self.db.execute(
            f"SELECT 1 FROM seen_keys WHERE key IN ({marks}) LIMIT 1", keys).fetchone()
        return row is not None

    def count_keys(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM seen_keys").fetchone()[0]

    def import_keys(self, keys: Iterable[str]) -> int:
        """Recovery: re-seed dedupe memory from the DEDUPE_INDEX sheet tab."""
        before = self.count_keys()
        with self.db:
            self.db.executemany(
                "INSERT OR IGNORE INTO seen_keys(key, job_rowid) VALUES (?, NULL)",
                [(k,) for k in keys if k])
        return self.count_keys() - before

    # ------------------------------------------------------------ writes
    def add_job(self, job: Job, found_on: str, raw_row: list[str],
                dedupe_rows: list[list[str]]) -> Optional[int]:
        """Insert a job if none of its keys was seen. Returns rowid, or None if duplicate."""
        keys = job.keys()
        if self.any_seen(keys):
            return None
        with self.db:
            cur = self.db.execute(
                """INSERT INTO jobs (found_on, source, job_id, company, title, location,
                   work_mode, job_type, experience, salary, posted_at, url, apply_url,
                   ats, search_term, description, primary_key, raw_row_json,
                   dedupe_rows_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (found_on, job.source, job.job_id, job.company, job.title, job.location,
                 job.work_mode, job.job_type, job.experience, job.salary,
                 job.posted_at.isoformat() if job.posted_at else "", job.url,
                 job.apply_url, job.ats, job.search_term, job.description,
                 job.primary_key, json.dumps(raw_row), json.dumps(dedupe_rows)))
            rowid = cur.lastrowid
            self.db.executemany(
                "INSERT OR IGNORE INTO seen_keys(key, job_rowid) VALUES (?, ?)",
                [(k, rowid) for k in keys])
        return rowid

    # ------------------------------------------------------------ sheet sync
    def unsynced(self) -> list[tuple[int, list[str], list[list[str]]]]:
        rows = self.db.execute(
            "SELECT id, raw_row_json, dedupe_rows_json FROM jobs "
            "WHERE sheet_synced = 0 ORDER BY id").fetchall()
        return [(r["id"], json.loads(r["raw_row_json"]), json.loads(r["dedupe_rows_json"]))
                for r in rows]

    def mark_synced(self, ids: Iterable[int]) -> None:
        with self.db:
            self.db.executemany("UPDATE jobs SET sheet_synced = 1 WHERE id = ?",
                                [(i,) for i in ids])

    # ------------------------------------------------------------ reporting
    def stats(self) -> dict:
        total = self.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        by_source = {r["source"]: r["n"] for r in self.db.execute(
            "SELECT source, COUNT(*) AS n FROM jobs GROUP BY source")}
        by_day = {r["found_on"]: r["n"] for r in self.db.execute(
            "SELECT found_on, COUNT(*) AS n FROM jobs GROUP BY found_on "
            "ORDER BY found_on DESC LIMIT 7")}
        unsynced = self.db.execute(
            "SELECT COUNT(*) FROM jobs WHERE sheet_synced = 0").fetchone()[0]
        return {"total_jobs": total, "by_source": by_source,
                "last_7_days": by_day, "unsynced_to_sheet": unsynced,
                "dedupe_keys": self.count_keys()}
