"""LinkedIn + Indeed via the `python-jobspy` library.

Each (site x term x location) query is isolated: one blocked query never stops
the others. LinkedIn is queried with fetch-description so Phase 2 has full JDs.
"""
from __future__ import annotations

import logging
import random
import time
from datetime import date, datetime, timezone
from typing import Optional

from ..models import Job, extract_experience, infer_work_mode, strip_html
from . import Collector

log = logging.getLogger("jobbot.jobspy")


def _clean(v) -> str:
    if v is None:
        return ""
    try:
        if v != v:  # NaN / NaT
            return ""
    except Exception:  # noqa: BLE001 - array-likes
        pass
    s = str(v).strip()
    return "" if s.lower() in ("nan", "none", "nat") else s


def _to_dt(v) -> Optional[datetime]:
    s = _clean(v)
    if not s:
        return None
    try:
        if isinstance(v, datetime):
            dt = v
        elif isinstance(v, date):
            dt = datetime(v.year, v.month, v.day)
        else:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _salary(rec: dict) -> str:
    lo, hi = _clean(rec.get("min_amount")), _clean(rec.get("max_amount"))
    if not lo and not hi:
        return ""
    cur, interval = _clean(rec.get("currency")), _clean(rec.get("interval"))
    def fmt(x: str) -> str:
        try:
            return f"{float(x):,.0f}"
        except ValueError:
            return x
    amount = f"{fmt(lo)} - {fmt(hi)}" if lo and hi else fmt(lo or hi)
    return " ".join(p for p in (cur, amount, f"/ {interval}" if interval else "") if p)


def record_to_job(rec: dict, search_term: str) -> Optional[Job]:
    """Map one JobSpy DataFrame record to a Job (None if unusable)."""
    site = _clean(rec.get("site")) or "jobspy"
    desc = strip_html(_clean(rec.get("description")))
    is_remote = rec.get("is_remote")
    is_remote = bool(is_remote) if _clean(is_remote) != "" else None
    job = Job(
        source=site,
        job_id=_clean(rec.get("id")),
        title=_clean(rec.get("title")),
        company=_clean(rec.get("company")),
        location=_clean(rec.get("location")),
        url=_clean(rec.get("job_url")),
        apply_url=_clean(rec.get("job_url_direct")),
        posted_at=_to_dt(rec.get("date_posted")),
        description=desc,
        job_type=_clean(rec.get("job_type")),
        salary=_salary(rec),
        work_mode=infer_work_mode(is_remote, _clean(rec.get("location")),
                                  _clean(rec.get("title")), desc),
        search_term=search_term,
        experience=extract_experience(_clean(rec.get("experience_range"))),
    )
    return job if job.is_valid else None


class JobSpyCollector(Collector):
    name = "jobspy"

    def __init__(self, sites: list[str], terms: list[str], locations: list[str],
                 hours_old: int, results_per_query: int,
                 delay: tuple[float, float] = (6, 14), max_retries: int = 3,
                 proxies: Optional[list[str]] = None,
                 empty_retry_wait: tuple[float, float] = (45, 75)):
        super().__init__()
        self.sites, self.terms, self.locations = sites, terms, locations
        self.hours_old, self.results = hours_old, results_per_query
        self.delay, self.max_retries = delay, max_retries
        self.empty_retry_wait = empty_retry_wait
        self.proxies = proxies or None

    def _scrape(self, site: str, term: str, location: str):
        from jobspy import scrape_jobs  # lazy import
        kwargs = dict(
            site_name=[site], search_term=term, location=location,
            results_wanted=self.results, hours_old=self.hours_old,
            country_indeed="India", verbose=0,
        )
        if site == "linkedin":
            kwargs["linkedin_fetch_description"] = True
        if self.proxies:
            kwargs["proxies"] = self.proxies
        return scrape_jobs(**kwargs)

    def collect(self) -> list[Job]:
        jobs: list[Job] = []
        for site in self.sites:
            for term in self.terms:
                for location in self.locations:
                    label = f"{site} | {term} | {location}"
                    df, empty_retried = None, False
                    for attempt in range(1, self.max_retries + 1):
                        try:
                            df = self._scrape(site, term, location)
                        except Exception as exc:  # noqa: BLE001
                            log.warning("%s failed (attempt %d/%d): %s",
                                        label, attempt, self.max_retries, exc)
                            df = None
                            time.sleep(min(60, 10 * attempt) + random.random() * 5)
                            continue
                        if len(df) == 0 and site == "linkedin" and not empty_retried:
                            # JobSpy reports LinkedIn's 429 rate limit as "0 jobs" without raising
                            empty_retried = True
                            log.warning("%s returned 0 results (possible rate limit); "
                                        "waiting, then retrying once", label)
                            time.sleep(random.uniform(*self.empty_retry_wait))
                            continue
                        break
                    if df is None:
                        self.errors.append(f"{label}: failed after {self.max_retries} attempts")
                        continue
                    records = df.to_dict("records") if len(df) else []
                    got = 0
                    for rec in records:
                        job = record_to_job(rec, term)
                        if job:
                            jobs.append(job)
                            got += 1
                    log.info("%s -> %d jobs", label, got)
                    time.sleep(random.uniform(*self.delay))
        return jobs
