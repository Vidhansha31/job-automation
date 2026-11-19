"""Job scoring, role families and relaxation tiers (deterministic, no LLM).

Tier 1 Core         title is a direct match, not senior, experience ask within ~1.5 yrs of yours
Tier 2 Related      related title, experience ask within ~2.5 yrs of yours
Tier 3 Stretch      related title (senior allowed), experience ask within ~3.5 yrs of yours
Tier 4 Skills match no title match but strong skills/domain overlap, experience within ~2.5 yrs
The shortlist fills tier 1 first and relaxes down only when it is short of the daily minimum.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .keywords import terms_in
from .models import Job

TIER_NAMES = {1: "1-Core", 2: "2-Related", 3: "3-Stretch", 4: "4-Skills match"}

# (family, regex, weight)
_TITLE_RULES = [
    ("product", r"associate product manager|\bapm\b", 40),
    ("product", r"product (associate|analyst|owner|operations|ops|specialist|support)", 40),
    ("product", r"product manager", 28),
    ("support", r"(technical|application|production|product|customer|client) support", 40),
    ("support", r"support (engineer|analyst|specialist|associate|executive|consultant)", 38),
    ("support", r"site reliability|\bsre\b|reliability engineer", 30),
    ("implementation", r"implementation (specialist|consultant|engineer|analyst|manager|associate|lead|executive)", 40),
    ("implementation", r"(solutions?|technical|functional) (consultant|engineer|analyst)|onboarding (specialist|manager|consultant)", 30),
    ("csm", r"customer success|client success", 40),
    ("performance", r"performance (test|testing|engineer)|load test|jmeter", 38),
    ("ba", r"business analyst|techno[- ]functional", 25),
]
_RULES = [(f, re.compile(rx, re.I), w) for f, rx, w in _TITLE_RULES]

_HARD_EXCLUDE = re.compile(
    r"(?<![a-z])(director|vp|vice president|head of|chief|president|principal|distinguished|fellow)(?![a-z])", re.I)
_SENIOR = re.compile(r"(?<![a-z])(senior|sr\.?|lead|staff)(?![a-z])|\b(ii|iii|iv)\b", re.I)
_JUNIOR = re.compile(r"(?<![a-z])(intern|interns|internship|trainee|apprentice)(?![a-z])", re.I)
_DOMAIN = re.compile(
    r"order management|\boms\b|fulfil+ment|e-?commerce|retail|omnichannel|supply chain|saas|shopify|netsuite", re.I)


def years_experience(start_month: str, now: datetime) -> float:
    y, m = (int(x) for x in start_month.split("-"))
    start = datetime(y, m, 1, tzinfo=timezone.utc)
    return max(0.0, (now - start).days / 365.25)


def parse_exp(text: str) -> Optional[tuple[float, Optional[float]]]:
    """'2-4 years' -> (2, 4); '5+ years' -> (5, None); '' -> None."""
    if not text:
        return None
    m = re.match(r"\s*(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)", text)
    if m:
        return float(m.group(1)), float(m.group(2))
    m = re.match(r"\s*(\d+(?:\.\d+)?)", text)
    if m:
        return float(m.group(1)), None
    return None


@dataclass
class Scored:
    score: float
    tier: Optional[int]
    family: str
    title_weight: int
    matched: list = field(default_factory=list)
    missing: list = field(default_factory=list)
    exp_min: Optional[float] = None
    flags: list = field(default_factory=list)

    @property
    def tier_name(self) -> str:
        return TIER_NAMES.get(self.tier, "")


def title_match(title: str) -> tuple[str, int]:
    best = ("", 0)
    for fam, rx, w in _RULES:
        if w > best[1] and rx.search(title or ""):
            best = (fam, w)
    return best


def score_job(job: Job, *, years: float, profile_terms: set[str],
              now: Optional[datetime] = None, excluded_companies=()) -> Scored:
    now = now or datetime.now(timezone.utc)
    family, tw = title_match(job.title)
    flags: list[str] = []
    senior = bool(_SENIOR.search(job.title))
    junior = bool(_JUNIOR.search(job.title))
    excluded = bool(_HARD_EXCLUDE.search(job.title)) or \
        job.company.strip().lower() in {c.strip().lower() for c in excluded_companies}
    if senior:
        flags.append("senior")
    if junior:
        flags.append("junior")

    text = f"{job.title}\n{job.description}"
    jd_terms = terms_in(text)
    matched, missing = sorted(jd_terms & profile_terms), sorted(jd_terms - profile_terms)
    skills = min(25.0, 2.5 * len(matched))
    domain = min(10.0, 2.0 * len({m.lower() for m in _DOMAIN.findall(text)}))

    exp = parse_exp(job.experience)
    gap = None if exp is None else exp[0] - years
    if gap is None:
        exp_pts = 8.0
    elif gap <= 0.6:
        exp_pts = 25.0
    elif gap <= 1.5:
        exp_pts = 18.0
    elif gap <= 2.5:
        exp_pts = 8.0
    elif gap <= 3.5:
        exp_pts = -5.0
    else:
        exp_pts = -30.0
    if exp and exp[1] is not None and exp[1] < years - 1.5:
        exp_pts -= 8.0
        flags.append("too junior range")

    if job.posted_at:
        age = (now - job.posted_at).days
        fresh = 5.0 if age <= 1 else 3.0 if age <= 3 else 0.0
    else:
        fresh = 1.0

    total = tw + exp_pts + skills + domain + fresh - (15 if senior else 0) - (25 if junior else 0)
    total = round(max(0.0, min(100.0, total)), 1)

    tier: Optional[int] = None
    if not excluded and not junior:
        ok1 = gap is None or gap <= 1.5
        ok2 = gap is None or gap <= 2.5
        ok3 = gap is None or gap <= 3.5
        if tw >= 30 and not senior and ok1:
            tier = 1
        elif tw >= 15 and ok2 and (not senior or (gap is not None and ok1)):
            tier = 2
        elif tw >= 15 and ok3:
            tier = 3
        elif tw == 0 and skills >= 15 and domain > 0 and ok2:
            tier = 4
    return Scored(total, tier, family, tw, matched, missing, exp[0] if exp else None, flags)


def select(scored: list[tuple], target: int, group=None, min_each: int = 0) -> list[tuple]:
    """scored = [(item, Scored)]; best tier first, then highest score.
    With group(item)->name and min_each>0, each source gets up to min_each picks first
    (its best ones), then the rest are filled by overall rank, so one site cannot crowd out others."""
    eligible = [x for x in scored if x[1].tier]
    eligible.sort(key=lambda x: (x[1].tier, -x[1].score))
    target = max(0, target)
    if not group or min_each <= 0:
        return eligible[:target]
    chosen, counts = [], {}
    for x in eligible:
        g = group(x[0])
        if counts.get(g, 0) < min_each and len(chosen) < target:
            chosen.append(x)
            counts[g] = counts.get(g, 0) + 1
    ids = {id(x) for x in chosen}
    chosen += [x for x in eligible if id(x) not in ids][:max(0, target - len(chosen))]
    chosen.sort(key=lambda x: (x[1].tier, -x[1].score))
    return chosen
