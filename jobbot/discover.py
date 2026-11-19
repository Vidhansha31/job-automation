"""Find company career boards (Greenhouse / Lever / Ashby / SmartRecruiters) from company names.

For each name, tries a couple of likely slugs on each platform and keeps boards that exist AND
list at least one India job. Review the saved list: a slug can occasionally belong to a
different company with the same name.
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor

from .collectors import ats as A
from .models import is_india_location


def slug_variants(name: str) -> list[str]:
    words = re.sub(r"[^a-z0-9 ]", "", (name or "").lower()).split()
    if not words:
        return []
    out = ["".join(words)]
    if len(words) > 1:
        out.append("-".join(words))
    return out


def _probe(args):
    platform, slug, name = args
    try:
        jobs = A.FETCHERS[platform](A.make_session(), slug, name)
    except Exception:  # noqa: BLE001 - not found / network / blocked: simply not a hit
        return None
    india = sum(1 for j in jobs if is_india_location(j.location))
    return {"platform": platform, "slug": slug, "name": name, "india_jobs": india} if india else None


def discover(names, workers: int = 8) -> dict:
    tasks = [(p, s, n) for n in names for s in slug_variants(n) for p in A.FETCHERS]
    found: dict = {}
    seen = set()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for hit in pool.map(_probe, tasks):
            if not hit or (hit["platform"], hit["slug"]) in seen:
                continue
            seen.add((hit["platform"], hit["slug"]))
            found.setdefault(hit["platform"], []).append(
                {"slug": hit["slug"], "name": hit["name"], "india_jobs": hit["india_jobs"]})
    return found
