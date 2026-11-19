"""Daily shortlist: score the unshortlisted pool, pick the best 15-20, tailor a resume PDF
for each, and write a local CSV. Safe to run several times a day: it tops up to the
target and only builds resumes that are still missing."""
from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .keywords import display_terms, terms_in
from .models import Job
from .scoring import Scored, score_job, select, years_experience
from .tailor import load_profile, profile_text, tailor_resume  # noqa: F401  (re-export)

log = logging.getLogger("jobbot.shortlist")

from .pipeline import ATS_SOURCES  # noqa: E402

AUTO_APPLY_ATS = {"greenhouse", "lever", "ashby", "smartrecruiters", "workable"}
CSV_HEADERS = ["#", "Company", "Job Title", "Location", "Experience", "Match Score", "Tier",
               "Source", "Apply Type", "Posted On", "Job Link", "Apply Link", "Resume File",
               "Resume Engine", "Matched Keywords", "Missing Keywords", "JD Summary",
               "Resume Error"]


@dataclass
class ShortlistReport:
    existing: int = 0
    pool: int = 0
    eligible: int = 0
    selected: int = 0
    total_today: int = 0
    minimum: int = 15
    by_tier: dict = field(default_factory=dict)
    by_source: dict = field(default_factory=dict)
    resumes_ok: int = 0
    resumes_failed: int = 0
    engines: dict = field(default_factory=dict)
    csv_path: str = ""
    errors: list = field(default_factory=list)

    def summary(self) -> str:
        tiers = ", ".join(f"{k}: {v}" for k, v in sorted(self.by_tier.items())) or "none"
        eng = ", ".join(f"{k}: {v}" for k, v in sorted(self.engines.items())) or "none"
        lines = [
            f"Pool (unshortlisted) : {self.pool}   eligible: {self.eligible}",
            f"New picks by source  : {', '.join(f'{k}: {v}' for k, v in sorted(self.by_source.items())) or 'none'}",
            f"Shortlisted today    : {self.total_today}  (+{self.selected} new)   tiers: {tiers}",
            f"Resumes              : {self.resumes_ok} ok, {self.resumes_failed} failed   engines: {eng}",
        ]
        if self.total_today < self.minimum:
            lines.append(f"WARNING: only {self.total_today} jobs qualify today (minimum "
                         f"{self.minimum}). Add more search terms/cities in config.yaml.")
        if self.csv_path:
            lines.append(f"CSV                  : {self.csv_path}")
        lines += [f"  ! {e}" for e in self.errors[:10]]
        return "\n".join(lines)


def job_from_row(r) -> Job:
    posted = None
    if r["posted_at"]:
        try:
            posted = datetime.fromisoformat(r["posted_at"])
        except ValueError:
            posted = None
    if posted is not None and posted.tzinfo is None:
        posted = posted.replace(tzinfo=timezone.utc)
    return Job(source=r["source"], title=r["title"], company=r["company"], url=r["url"] or "",
               job_id=r["job_id"] or "", location=r["location"] or "", posted_at=posted,
               description=r["description"] or "", job_type=r["job_type"] or "",
               work_mode=r["work_mode"] or "", salary=r["salary"] or "",
               experience=r["experience"] or "", apply_url=r["apply_url"] or "",
               ats=r["ats"] or "", search_term=r["search_term"] or "")


def _safe(s: str, n: int = 40) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s or "").strip("_")[:n] or "x"


def resume_filename(profile: dict, row) -> str:
    return f"{_safe(profile['name'], 30)}_{_safe(row['company'])}_{_safe(row['title'])}.pdf"


def run_shortlist(*, store, profile: dict, day: str, now: datetime, scfg: Optional[dict] = None,
                  llm=None, renderer=None, out_dir="output",
                  limit: Optional[int] = None, regen: bool = False) -> ShortlistReport:
    scfg = scfg or {}
    target = int(scfg.get("daily_target", 20))
    rep = ShortlistReport(minimum=int(scfg.get("minimum", 15)))
    rep.existing = store.count_shortlisted(day)

    # ---- 1. score the pool and select
    years = years_experience(profile["start_date"], now)
    p_terms = terms_in(profile_text(profile))
    since = (now - timedelta(days=int(scfg.get("pool_days", 7)))).strftime("%Y-%m-%d")
    scored = []
    for row in store.pool(since):
        try:
            job = job_from_row(row)
            scored.append((row, job, score_job(
                job, years=years, profile_terms=p_terms, now=now,
                excluded_companies=scfg.get("excluded_companies", []))))
        except Exception:  # noqa: BLE001 - one malformed row must not stop the batch
            log.exception("Could not score job id=%s; skipped", row["id"])
    rep.pool = len(scored)
    rep.eligible = sum(1 for _, _, s in scored if s.tier)
    need = max(0, target - rep.existing)
    if limit is not None:
        need = min(need, limit)
    grp = lambda rj: "career pages" if rj[1].source in ATS_SOURCES else rj[1].source  # noqa: E731
    for (r, j), s in select([((r, j), s) for r, j, s in scored], need, group=grp,
                            min_each=int(scfg.get("min_per_source", 3))):
        store.mark_shortlisted(
            r["id"], day=day, score=s.score, tier=s.tier_name, family=s.family,
            matched=display_terms(s.matched), missing=display_terms(s.missing),
            apply_type="Auto" if j.ats in AUTO_APPLY_ATS else "Manual")
        rep.selected += 1
        rep.by_tier[s.tier_name] = rep.by_tier.get(s.tier_name, 0) + 1
        rep.by_source[grp((r, j))] = rep.by_source.get(grp((r, j)), 0) + 1

    # ---- 2. resumes for every shortlisted job that still lacks one
    out = Path(out_dir) / day
    if regen:                                   # rebuild today's resumes (e.g. after adding AI keys)
        for row in store.shortlisted(day):
            if row["resume_path"]:
                try:
                    Path(row["resume_path"]).unlink(missing_ok=True)
                except OSError:
                    log.warning("Could not delete %s (open in a PDF viewer?) - it will be overwritten",
                                row["resume_path"])
        store.clear_resumes(day)
    for row in store.shortlisted(day):
        if row["resume_path"] and Path(row["resume_path"]).exists():
            continue
        job = job_from_row(row)
        try:
            t = tailor_resume(profile, job, row["family"] or "", llm)
            store.set_resume(row["id"], engine=t.engine, jd_summary=t.jd_summary)
            rep.engines[t.engine] = rep.engines.get(t.engine, 0) + 1
            if renderer is None:
                continue
            path = out / resume_filename(profile, row)
            if path.exists():                      # same company+title already used today
                path = path.with_name(f"{path.stem}_{row['id']}.pdf")
            renderer.render(profile, t, path)
            store.set_resume(row["id"], path=str(path), engine=t.engine, jd_summary=t.jd_summary)
            rep.resumes_ok += 1
        except Exception as exc:  # noqa: BLE001 - one bad resume never stops the batch
            log.exception("Resume failed for %s / %s", row["company"], row["title"])
            store.set_resume(row["id"], error=str(exc)[:200])
            rep.resumes_failed += 1
            rep.errors.append(f"{row['company']} | {row['title']}: {str(exc)[:100]}")

    # ---- 3. local CSV of today's shortlist
    rows = store.shortlisted(day)
    rep.total_today = len(rows)
    if rows:
        out.mkdir(parents=True, exist_ok=True)
        csv_path = out / f"shortlist_{day}.csv"
        with csv_path.open("w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(CSV_HEADERS)
            for i, r in enumerate(rows, 1):
                posted = (r["posted_at"] or "")[:10]
                w.writerow([i, r["company"], r["title"], r["location"], r["experience"], r["score"],
                            r["tier"], r["source"], r["apply_type"], posted, r["url"],
                            r["apply_url"], r["resume_path"] or "", r["resume_engine"] or "",
                            r["matched_kw"], r["missing_kw"], r["jd_summary"] or "",
                            r["resume_error"] or ""])
        rep.csv_path = str(csv_path)
    return rep
