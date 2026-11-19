"""Resume tailoring that cannot invent facts.

Structure (competencies, bullets, education, ...) is assembled deterministically from
the master profile and ordered by relevance to the JD. An LLM (optional) may only
rewrite the summary and the experience bullets; every rewrite passes a guard that
rejects unknown tools/keywords, unknown numbers and years-of-experience claims, and
falls back to the original profile text for that field if it fails.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from .keywords import terms_in
from .models import Job

log = logging.getLogger("jobbot.tailor")
_WS = re.compile(r"\s+")


@dataclass
class Tailored:
    headline: str
    summary: str
    competencies: list
    bullets: list
    jd_summary: str = ""
    engine: str = "offline"
    notes: list = field(default_factory=list)


# ------------------------------------------------------------------ profile
def load_profile(path) -> dict:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Master profile not found: {p}")
    prof = yaml.safe_load(p.read_text(encoding="utf-8"))
    for key in ("name", "contact", "summary", "competencies", "experience", "education"):
        if key not in prof:
            raise ValueError(f"master_profile.yaml is missing '{key}'")
    return prof


def _strings(obj, skip=("triggers",)):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if k not in skip:
                yield from _strings(v, skip)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _strings(v, skip)


def profile_text(profile: dict) -> str:
    """Every fact in the profile (extras included, trigger keywords excluded)."""
    return "\n".join(_strings(profile))


# ------------------------------------------------------------------ assembly
def assemble(profile: dict, job_text: str) -> dict:
    jd_l = _WS.sub(" ", job_text.lower())
    jd_terms = terms_in(job_text)

    def hit(s: str) -> bool:
        return s.lower() in jd_l or bool(terms_in(s) & jd_terms)

    def triggered(triggers) -> bool:
        return any(t.lower() in jd_terms or t.lower() in jd_l for t in triggers or [])

    cats = []
    for i, c in enumerate(profile["competencies"]):
        prim, sec = list(c["primary"]), list(c.get("secondary", []))
        for ex in profile.get("extra_skills", []):
            if ex["category"] == c["category"] and triggered(ex.get("triggers")) \
                    and ex["skill"] not in prim + sec:
                prim.append(ex["skill"])
        prim.sort(key=lambda s: not hit(s))
        sec.sort(key=lambda s: not hit(s))
        cats.append((-sum(hit(x) for x in prim + sec), i,
                     {"category": c["category"], "primary": prim, "secondary": sec}))
    cats.sort(key=lambda x: (x[0], x[1]))

    exp = profile["experience"]
    bullets = [dict(b) for b in exp["bullets"]]
    bullets += [dict(b) for b in exp.get("extra_bullets", []) if triggered(b.get("triggers"))]
    bullets.sort(key=lambda b: -len(terms_in(b["text"]) & jd_terms))   # stable
    return {"competencies": [c for _, _, c in cats], "bullets": bullets, "jd_terms": jd_terms}


def provided_text(profile: dict, facts: dict) -> str:
    parts = [profile["summary"], profile.get("project", {}).get("text", "")]
    parts += [b["text"] for b in facts["bullets"]]
    for c in facts["competencies"]:
        parts += c["primary"] + c["secondary"]
    parts += profile.get("achievements", []) + profile.get("certifications", [])
    return "\n".join(parts)


# ------------------------------------------------------------------ guard
def guard(text: str, allowed_terms: set[str], allowed_numbers: set[str]) -> Optional[str]:
    """Return the reason a generated text is rejected, or None if it is acceptable."""
    if re.search(r"\b\d+\s*\+?\s*(?:years?|yrs?)\b", text, re.I):
        return "claims years of experience"
    bad = terms_in(text) - allowed_terms
    if bad:
        return f"unknown terms {sorted(bad)}"
    if set(re.findall(r"\d+", text)) - allowed_numbers:
        return "unknown numbers"
    if text.count("**") % 2:
        return "unbalanced bold markers"
    return None


def _words(s: str) -> int:
    return len(s.split())


# ------------------------------------------------------------------ prompt
SYSTEM = (
    "You tailor a resume for a specific job WITHOUT inventing anything. Use only the facts "
    "in PROFILE. Never add tools, technologies, numbers, employers, job titles, certifications "
    "or years of experience that are not in PROFILE. You may reorder, shorten and rephrase, and "
    "you should mirror the job description's vocabulary wherever it is truthful. Reply with one "
    "JSON object only."
)


def build_prompt(profile: dict, facts: dict, job: Job) -> str:
    payload = {
        "PROFILE": {
            "summary": profile["summary"],
            "bullets": {b["id"]: b["text"] for b in facts["bullets"]},
            "skills": [x for c in facts["competencies"] for x in c["primary"] + c["secondary"]],
            "project": profile.get("project", {}).get("text", ""),
        },
        "JOB": {"title": job.title, "company": job.company,
                "description": (job.description or "")[:6000]},
        "OUTPUT_RULES": {
            "summary": "2-3 sentences, 30-70 words, no bold markers, no years of experience",
            "bullets": "object mapping EVERY bullet id in PROFILE.bullets to a rewritten bullet "
                       "(10-45 words). Wrap 2-5 key phrases in **double asterisks**. Keep all "
                       "numbers exactly as in the original. Do not merge or drop bullets.",
            "jd_summary": "2 plain sentences: what the role is and its top requirements",
        },
        "OUTPUT_SCHEMA": {"summary": "string", "bullets": {"<id>": "string"},
                          "jd_summary": "string"},
    }
    return json.dumps(payload, ensure_ascii=False)


def _offline_jd_summary(description: str, limit: int = 380) -> str:
    text = _WS.sub(" ", (description or "").strip())
    if not text:
        return ""
    out = ""
    for sent in re.split(r"(?<=[.!?])\s+", text):
        if len(out) + len(sent) > limit and out:
            break
        out += (" " if out else "") + sent
    return out[:limit + 80]


# ------------------------------------------------------------------ main entry
def tailor_resume(profile: dict, job: Job, family: str = "", llm=None) -> Tailored:
    facts = assemble(profile, f"{job.title}\n{job.description}")
    ptext = provided_text(profile, facts)
    allowed_terms, allowed_nums = terms_in(ptext), set(re.findall(r"\d+", ptext))

    summary = profile["summary"]
    bullets = [(b["id"], b["text"]) for b in facts["bullets"]]
    jd_summary = _offline_jd_summary(job.description)
    engine, notes = "offline", []

    if llm is not None and getattr(llm, "available", False):
        try:
            data, engine = llm.complete_json(SYSTEM, build_prompt(profile, facts, job))
        except Exception as exc:  # noqa: BLE001 - every LLM failure degrades to offline
            notes.append(f"LLM unavailable ({str(exc)[:100]}); used original wording")
            engine, data = "offline", {}
        s = str(data.get("summary", "")).replace("**", "").strip()
        if s:
            why = guard(s, allowed_terms, allowed_nums) or \
                (None if 20 <= _words(s) <= 90 else "summary length")
            if why:
                notes.append(f"summary rejected: {why}")
            else:
                summary = s
        new = data.get("bullets") if isinstance(data.get("bullets"), dict) else {}
        rewritten = []
        for bid, original in bullets:
            t = str(new.get(bid, "")).strip()
            if t:
                why = guard(t, allowed_terms, allowed_nums) or \
                    (None if 8 <= _words(t) <= 60 else "bullet length")
                if why:
                    notes.append(f"bullet {bid} rejected: {why}")
                    t = ""
            rewritten.append((bid, t or original))
        bullets = rewritten
        js = _WS.sub(" ", str(data.get("jd_summary", ""))).strip()
        if js:
            jd_summary = js[:500]

    headlines = profile.get("headlines", {})
    headline = headlines.get(family) or headlines.get("default", "")
    return Tailored(headline=headline, summary=summary, competencies=facts["competencies"],
                    bullets=[t for _, t in bullets], jd_summary=jd_summary,
                    engine=engine, notes=notes)
