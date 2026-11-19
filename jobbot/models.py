"""Job model, text normalisation and de-duplication keys.

A job is a duplicate if ANY of its keys was seen before:
  id:<source>:<job_id>   same posting on the same platform
  url:<canonical url>    same posting URL (tracking params stripped)
  sig:<company>|<title>|<city>   same role cross-posted on several platforms
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlparse

# ---------------------------------------------------------------- sheet schema
RAW_HEADERS = [
    "Found On", "Source", "Company", "Job Title", "Location", "Work Mode",
    "Job Type", "Experience (from JD)", "Salary", "Posted On", "Job Link",
    "Apply Link", "ATS", "Job ID", "Search Term", "Job Description", "Dedupe Key",
]
DEDUPE_HEADERS = ["Dedupe Key", "First Seen", "Source", "Company", "Job Title"]

# ---------------------------------------------------------------- normalisation
_DROP_PARAMS = {
    "trackingid", "refid", "trk", "trkemail", "ref", "src", "source",
    "lever-source", "lever-origin", "currentjobid", "position", "pagenum",
    "origin", "from", "ccuid", "fccid", "referrer",
}
_COMPANY_SUFFIXES = {
    "pvt", "private", "ltd", "limited", "inc", "llp", "llc", "corp",
    "corporation", "india", "co",
}
_CITY_ALIASES = {
    "bangalore": "bengaluru", "bengaluru": "bengaluru", "bangaluru": "bengaluru",
    "gurgaon": "gurugram", "gurugram": "gurugram",
    "bombay": "mumbai", "navi mumbai": "mumbai", "new delhi": "delhi",
    "delhi ncr": "delhi", "noida": "noida", "greater noida": "noida",
}
INDIAN_PLACES = {
    "india", "bengaluru", "bangalore", "mumbai", "pune", "hyderabad", "chennai",
    "delhi", "new delhi", "gurgaon", "gurugram", "noida", "kolkata", "ahmedabad",
    "jaipur", "indore", "chandigarh", "kochi", "coimbatore", "lucknow", "nagpur",
    "bhopal", "thiruvananthapuram", "trivandrum", "vadodara", "surat", "mohali",
    "ghaziabad", "faridabad", "navi mumbai", "thane", "visakhapatnam",
    "bhubaneswar", "mysore", "mysuru", "nashik", "patna", "ranchi", "raipur",
}


def strip_html(text) -> str:
    """HTML (possibly entity-escaped) -> readable plain text."""
    if not text:
        return ""
    text = html.unescape(str(text))
    text = re.sub(r"(?i)<\s*br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</\s*(p|div|li|h[1-6]|ul|ol|tr)\s*>", "\n", text)
    text = re.sub(r"(?i)<\s*li[^>]*>", "- ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\r\f\v ]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _alnum(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower()).strip()


def norm_company(name: str) -> str:
    tokens = [t for t in _alnum(name).split() if t]
    while tokens and tokens[-1] in _COMPANY_SUFFIXES and len(tokens) > 1:
        tokens.pop()
    return " ".join(tokens)


def norm_title(title: str) -> str:
    t = re.sub(r"[\(\[].*?[\)\]]", " ", title or "")
    return " ".join(_alnum(t).split())


def norm_city(location: str) -> str:
    loc = (location or "").lower().strip()
    if not loc:
        return ""
    first = _alnum(loc.split(",")[0])
    if first in _CITY_ALIASES:
        return _CITY_ALIASES[first]
    if "remote" in loc and first in ("remote", "india", ""):
        return "remote"
    return first


def is_india_location(location: str) -> bool:
    text = _alnum(location or "")
    if not text:
        return False
    words = set(text.split())
    if "india" in words:
        return True
    return any((p in words) if " " not in p else (p in text) for p in INDIAN_PLACES)


def canonical_url(url: str) -> str:
    """Stable form of a job URL: no scheme/www/fragment/tracking params."""
    if not url:
        return ""
    url = url.strip()
    m = re.search(r"linkedin\.[a-z.]+/jobs/view/(?:[^/?#]*-)?(\d{5,})", url, re.I)
    if m:
        return f"linkedin.com/jobs/view/{m.group(1)}"
    parts = urlparse(url if "://" in url else "https://" + url)
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if host.endswith("indeed.com"):
        jk = dict(parse_qsl(parts.query)).get("jk")
        if jk:
            return f"indeed.com/viewjob?jk={jk}"
    query = sorted(
        (k, v) for k, v in parse_qsl(parts.query)
        if k.lower() not in _DROP_PARAMS and not k.lower().startswith("utm_")
    )
    path = parts.path.rstrip("/")
    out = f"{host}{path}"
    if query:
        out += "?" + urlencode(query)
    return out


def detect_ats(url: str) -> str:
    """Which application platform a link belongs to (used to route auto-apply)."""
    u = (url or "").lower()
    table = [
        ("greenhouse.io", "greenhouse"), ("lever.co", "lever"),
        ("ashbyhq.com", "ashby"), ("myworkdayjobs.com", "workday"),
        ("smartrecruiters.com", "smartrecruiters"), ("workable.com", "workable"),
        ("linkedin.com", "linkedin"), ("indeed.com", "indeed"),
        ("naukri.com", "naukri"), ("icims.com", "icims"),
        ("successfactors", "successfactors"), ("taleo", "taleo"),
    ]
    for needle, name in table:
        if needle in u:
            return name
    return ""


def extract_experience(text: str) -> str:
    """Best-effort 'N-M years' / 'N+ years' phrase from a job description."""
    if not text:
        return ""
    t = text.lower()
    m = re.search(r"(\d{1,2})\s*(?:-|–|—|to)\s*(\d{1,2})\s*\+?\s*(?:years|yrs|year)", t)
    if m and int(m.group(1)) <= int(m.group(2)) <= 40:
        return f"{m.group(1)}-{m.group(2)} years"
    m = re.search(r"(\d{1,2})\s*\+\s*(?:years|yrs|year)", t)
    if m:
        return f"{m.group(1)}+ years"
    m = re.search(
        r"(?:minimum|min\.?|at least|atleast)\s*(?:of\s*)?(\d{1,2})\s*(?:years|yrs|year)", t)
    if m:
        return f"{m.group(1)}+ years"
    m = re.search(r"(\d{1,2})\s*(?:years|yrs)\s*(?:of\s*)?(?:relevant\s*)?experience", t)
    if m:
        return f"{m.group(1)} years"
    return ""


def infer_work_mode(is_remote: Optional[bool], *texts: str) -> str:
    blob = " ".join(t for t in texts if t)[:3000].lower()
    if is_remote is True or re.search(r"\bremote\b|work from home|\bwfh\b", blob):
        return "Remote" if (is_remote is True or "hybrid" not in blob) else "Hybrid"
    if "hybrid" in blob:
        return "Hybrid"
    return ""


# ---------------------------------------------------------------- Job
@dataclass
class Job:
    source: str
    title: str
    company: str
    url: str
    job_id: str = ""
    location: str = ""
    posted_at: Optional[datetime] = None
    description: str = ""
    job_type: str = ""
    work_mode: str = ""
    salary: str = ""
    experience: str = ""
    apply_url: str = ""
    ats: str = ""
    search_term: str = ""

    def __post_init__(self) -> None:
        for name in ("source", "title", "company", "url", "job_id", "location",
                     "job_type", "work_mode", "salary", "experience",
                     "apply_url", "ats", "search_term"):
            setattr(self, name, (getattr(self, name) or "").strip())
        self.description = (self.description or "").strip()
        if not self.apply_url:
            self.apply_url = self.url
        if not self.ats:
            self.ats = detect_ats(self.apply_url) or detect_ats(self.url)
        if not self.experience:
            self.experience = extract_experience(self.description)

    @property
    def is_valid(self) -> bool:
        return bool(self.title and self.company and (self.url or self.job_id))

    def keys(self) -> list[str]:
        ks: list[str] = []
        if self.job_id:
            ks.append(f"id:{self.source}:{self.job_id}")
        cu = canonical_url(self.url)
        if cu:
            ks.append(f"url:{cu}")
        sig = f"{norm_company(self.company)}|{norm_title(self.title)}|{norm_city(self.location)}"
        ks.append(f"sig:{sig}")
        return ks

    @property
    def primary_key(self) -> str:
        return self.keys()[0]

    def raw_row(self, found_on: str, max_desc: int = 30000) -> list[str]:
        posted = self.posted_at.strftime("%Y-%m-%d") if self.posted_at else ""
        return [
            found_on, self.source, self.company, self.title, self.location,
            self.work_mode, self.job_type, self.experience, self.salary, posted,
            self.url, self.apply_url, self.ats, self.job_id, self.search_term,
            self.description[:max_desc], self.primary_key,
        ]
