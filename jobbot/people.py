"""LinkedIn recruiter lookup using YOUR OWN logged-in browser session (Playwright).

Deliberately conservative: only today's shortlisted companies, one search each, long random
pauses, and it stops for the day at the first login wall / checkpoint / verification page.
LinkedIn discourages automation - keep `max_per_day` low. Not guaranteed to match LinkedIn's
page layout forever; it reads links and visible text rather than fragile CSS classes.
"""
from __future__ import annotations

import logging
import random
import re
import time
from pathlib import Path
from urllib.parse import quote

from .models import norm_company

log = logging.getLogger("jobbot.people")

SEARCH = "https://www.linkedin.com/search/results/people/?keywords="
_BLOCK_URL = ("/login", "/authwall", "/checkpoint", "/uas/", "/challenge")
_BLOCK_TEXT = ("unusual activity", "security verification", "quick security check",
               "verify you're a human")
RECRUITER_RX = re.compile(r"recruit|talent|hiring|\bhr\b|human resources|"
                          r"people (ops|operations|partner)|acquisition|sourcer", re.I)
_JS = """() => {
  const out = [], seen = new Set();
  for (const a of document.querySelectorAll('a[href*="/in/"]')) {
    const href = a.href.split('?')[0].replace(/\\/$/, '')
      .replace(/^https?:\\/\\/[a-z]{2,3}\\.linkedin\\.com/, 'https://www.linkedin.com');
    if (!/linkedin\\.com\\/in\\/[^/]+$/.test(href) || seen.has(href)) continue;
    seen.add(href);
    const box = a.closest('li') || a.closest('div[role="listitem"]') || a.parentElement;
    out.push({url: href, text: ((box || a).innerText || '').replace(/\\s+/g, ' ').trim()});
  }
  return out;
}"""


class LinkedInBlocked(Exception):
    pass


def is_blocked_url(url: str) -> bool:
    return any(m in (url or "") for m in _BLOCK_URL)


def extract_cards(page) -> list[dict]:
    return page.evaluate(_JS)


def pick_contacts(cards: list[dict], company: str, limit: int = 1) -> list[tuple[str, str]]:
    """Recruiter-looking people whose snippet mentions the company -> [(snippet, url)]."""
    toks = [t for t in norm_company(company).split() if len(t) >= 3]
    if not toks:
        return []
    out = []
    for c in cards:
        text = c.get("text", "")
        if RECRUITER_RX.search(text) and toks[0] in text.lower():
            out.append((text[:140], c["url"]))
            if len(out) >= limit:
                break
    return out


class LinkedInFinder:
    def __init__(self, profile_dir, headless: bool = True, delay=(10, 25), sleep=time.sleep):
        self.profile_dir, self.headless, self.delay, self.sleep = Path(profile_dir), headless, delay, sleep
        self._pw = self._ctx = self.page = None
        self._first = True

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._ctx = self._pw.chromium.launch_persistent_context(
            str(self.profile_dir), headless=self.headless, locale="en-US",
            viewport={"width": 1280, "height": 900})
        self.page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        return self

    def __exit__(self, *exc):
        for obj, meth in ((self._ctx, "close"), (self._pw, "stop")):
            try:
                if obj:
                    getattr(obj, meth)()
            except Exception:  # noqa: BLE001
                pass

    def logged_in(self) -> bool:
        self.page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=45000)
        self.page.wait_for_timeout(1500)
        return not is_blocked_url(self.page.url)

    def find(self, company: str, limit: int = 1) -> list[tuple[str, str]]:
        if not self._first:
            self.sleep(random.uniform(*self.delay))
        self._first = False
        self.page.goto(SEARCH + quote(f"{company} recruiter"), wait_until="domcontentloaded",
                       timeout=45000)
        self.page.wait_for_timeout(2500)
        if is_blocked_url(self.page.url):
            raise LinkedInBlocked(self.page.url)
        body = self.page.inner_text("body")[:3000].lower()
        if any(t in body for t in _BLOCK_TEXT):
            raise LinkedInBlocked("verification page")
        try:
            self.page.wait_for_selector('a[href*="/in/"]', timeout=10000)
        except Exception:  # noqa: BLE001 - no people results
            return []
        return pick_contacts(extract_cards(self.page), company, limit)


def linkedin_login(profile_dir, wait_seconds: int = 300) -> bool:
    """Open a visible browser so you can sign in once; the session is kept in profile_dir."""
    from playwright.sync_api import sync_playwright
    Path(profile_dir).mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(profile_dir), headless=False, viewport={"width": 1200, "height": 850})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.linkedin.com/login")
        try:
            page.wait_for_url(re.compile(r"linkedin\.com/(feed|in/|mynetwork|jobs|messaging)"),
                              timeout=wait_seconds * 1000)
            ok = True
        except Exception:  # noqa: BLE001
            ok = False
        ctx.close()
    return ok
