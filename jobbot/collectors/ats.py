"""Company career pages through the public JSON APIs of common ATS platforms.

Greenhouse, Lever, Ashby and SmartRecruiters expose job boards without login.
These are the most reliable sources, and the same platforms are the ones Phase 4
can auto-apply on. Workday is intentionally not collected here (needs an account
per company).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ..models import Job, infer_work_mode, is_india_location, strip_html
from . import Collector

log = logging.getLogger("jobbot.ats")


class BoardNotFound(Exception):
    pass


def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=4, backoff_factor=1.5, status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=("GET",))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers["User-Agent"] = "Mozilla/5.0 (compatible; JobTrackerBot/1.0)"
    return s


def _get_json(session: requests.Session, url: str, params: Optional[dict] = None):
    resp = session.get(url, params=params, timeout=30)
    if resp.status_code == 404:
        raise BoardNotFound(url)
    resp.raise_for_status()
    return resp.json()


def _parse_dt(v) -> Optional[datetime]:
    if v in (None, ""):
        return None
    try:
        if isinstance(v, (int, float)):
            return datetime.fromtimestamp(v / 1000, tz=timezone.utc)
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None


# ------------------------------------------------------------------ parsers
def parse_greenhouse(data: dict, slug: str, name: str) -> list[Job]:
    jobs = []
    for j in data.get("jobs", []):
        desc = strip_html(j.get("content", ""))
        loc = (j.get("location") or {}).get("name", "")
        jobs.append(Job(
            source="greenhouse", job_id=str(j.get("id", "")), title=j.get("title", ""),
            company=name, location=loc, url=j.get("absolute_url", ""),
            posted_at=_parse_dt(j.get("first_published") or j.get("updated_at")),
            description=desc, work_mode=infer_work_mode(None, loc, j.get("title", "")),
            ats="greenhouse"))
    return jobs


def parse_lever(data: list, slug: str, name: str) -> list[Job]:
    jobs = []
    for j in data or []:
        cats = j.get("categories") or {}
        loc = cats.get("location") or ", ".join(cats.get("allLocations") or [])
        wm = (j.get("workplaceType") or "").replace("-", " ").title()
        jobs.append(Job(
            source="lever", job_id=str(j.get("id", "")), title=j.get("text", ""),
            company=name, location=loc, url=j.get("hostedUrl", ""),
            apply_url=j.get("applyUrl", "") or j.get("hostedUrl", ""),
            posted_at=_parse_dt(j.get("createdAt")),
            description=j.get("descriptionPlain") or strip_html(j.get("description", "")),
            job_type=cats.get("commitment", ""),
            work_mode=wm if wm in ("Remote", "Hybrid") else infer_work_mode(None, loc),
            ats="lever"))
    return jobs


def parse_ashby(data: dict, slug: str, name: str) -> list[Job]:
    jobs = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        locs = [j.get("location", "")] + [
            s.get("location", "") if isinstance(s, dict) else str(s)
            for s in (j.get("secondaryLocations") or [])]
        loc = " | ".join(x for x in locs if x)
        wm = "Remote" if j.get("isRemote") else infer_work_mode(None, loc, j.get("title", ""))
        jobs.append(Job(
            source="ashby", job_id=str(j.get("id", "")), title=j.get("title", ""),
            company=name, location=loc, url=j.get("jobUrl", ""),
            apply_url=j.get("applyUrl", "") or j.get("jobUrl", ""),
            posted_at=_parse_dt(j.get("publishedAt")),
            description=j.get("descriptionPlain") or strip_html(j.get("descriptionHtml", "")),
            job_type=(j.get("employmentType") or ""), work_mode=wm, ats="ashby"))
    return jobs


def parse_smartrecruiters(data: dict, slug: str, name: str) -> list[Job]:
    jobs = []
    for j in data.get("content", []):
        loc = j.get("location") or {}
        country = (loc.get("country") or "").lower()
        place = ", ".join(p for p in (loc.get("city"), loc.get("region")) if p)
        if country == "in":
            place = f"{place}, India" if place else "India"
        jid = str(j.get("id", ""))
        jobs.append(Job(
            source="smartrecruiters", job_id=jid, title=j.get("name", ""),
            company=(j.get("company") or {}).get("name") or name, location=place,
            url=f"https://jobs.smartrecruiters.com/{slug}/{jid}",
            posted_at=_parse_dt(j.get("releasedDate")),
            work_mode="Remote" if loc.get("remote") else "",
            ats="smartrecruiters"))  # description is fetched later (Phase 2)
    return jobs


# ------------------------------------------------------------------ fetchers
def fetch_greenhouse(session, slug, name):
    data = _get_json(session, f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
                     {"content": "true"})
    return parse_greenhouse(data, slug, name)


def fetch_lever(session, slug, name):
    data = _get_json(session, f"https://api.lever.co/v0/postings/{slug}", {"mode": "json"})
    return parse_lever(data, slug, name)


def fetch_ashby(session, slug, name):
    data = _get_json(session, f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
                     {"includeCompensation": "true"})
    return parse_ashby(data, slug, name)


def fetch_smartrecruiters(session, slug, name):
    out: list[Job] = []
    for page in range(5):  # up to 500 postings
        data = _get_json(session, f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",
                         {"limit": 100, "offset": page * 100, "country": "in"})
        out += parse_smartrecruiters(data, slug, name)
        if (page + 1) * 100 >= data.get("totalFound", 0):
            break
    return out


FETCHERS: dict[str, Callable] = {
    "greenhouse": fetch_greenhouse, "lever": fetch_lever,
    "ashby": fetch_ashby, "smartrecruiters": fetch_smartrecruiters,
}


def iter_boards(cfg_ats: dict):
    """Yield (platform, slug, display_name) from config (strings or {slug, name})."""
    for platform, entries in (cfg_ats or {}).items():
        for e in entries or []:
            slug = e if isinstance(e, str) else e.get("slug", "")
            name = slug.replace("-", " ").title() if isinstance(e, str) else \
                e.get("name") or slug.replace("-", " ").title()
            if slug and platform in FETCHERS:
                yield platform, slug, name


class AtsCollector(Collector):
    name = "ats"

    def __init__(self, cfg_ats: dict, session: Optional[requests.Session] = None):
        super().__init__()
        self.cfg_ats = cfg_ats
        self.session = session or make_session()

    def collect(self) -> list[Job]:
        jobs: list[Job] = []
        for platform, slug, name in iter_boards(self.cfg_ats):
            label = f"{platform}/{slug}"
            try:
                found = FETCHERS[platform](self.session, slug, name)
            except BoardNotFound:
                log.warning("%s: board not found (check the slug) - skipped", label)
                self.errors.append(f"{label}: board not found")
                continue
            except Exception as exc:  # noqa: BLE001
                log.warning("%s failed: %s", label, exc)
                self.errors.append(f"{label}: {exc}")
                continue
            india = [j for j in found if is_india_location(j.location)]
            dates = [j.posted_at for j in india if j.posted_at]
            newest = max(dates).strftime("%Y-%m-%d") if dates else "unknown"
            log.info("%s -> %d jobs (%d in India, newest posted %s)",
                     label, len(found), len(india), newest)
            jobs += india
        return jobs


def check_boards(cfg_ats: dict) -> list[str]:
    """Human-readable health report for every configured board."""
    session, lines = make_session(), []
    for platform, slug, name in iter_boards(cfg_ats):
        try:
            found = FETCHERS[platform](session, slug, name)
            india = sum(1 for j in found if is_india_location(j.location))
            lines.append(f"OK         {platform}/{slug}: {len(found)} jobs ({india} in India)")
        except BoardNotFound:
            lines.append(f"NOT FOUND  {platform}/{slug}: wrong slug or not on this platform")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"ERROR      {platform}/{slug}: {exc}")
    return lines
