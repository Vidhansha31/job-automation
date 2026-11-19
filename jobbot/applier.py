"""Auto-apply engine (Playwright) for public company application forms.

Works on Greenhouse, Lever, Ashby, Workable-style pages through ONE generic form engine
(label-based, not CSS-class-based). Design rules:
  * never guess: a required question without an answer rule stops the job -> manual apply
  * never solve or bypass a CAPTCHA -> manual apply
  * every attempt gets a fresh browser context; every exception is caught and classified
  * transient problems (timeouts, network, crashed page) are retried once
  * a screenshot is saved for every failure and dry run
  * "submitted but not confirmed" is reported separately so you never double-apply blindly
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .qa import Ctx, choose_option, norm, real_options, resolve

log = logging.getLogger("jobbot.apply")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0.0.0 Safari/537.36")
SUCCESS_RX = re.compile(
    r"thank(s| you)( for (applying|your (application|interest)))?|application (has been |was )?"
    r"(submitted|received|sent|complete)|we(?:'ve| have) received your application|"
    r"successfully (submitted|applied)", re.I)
EXPIRED_RX = re.compile(
    r"no longer (available|accepting|open)|(job|position|posting|role) (is )?(no longer|has been "
    r"(closed|filled|removed))|(job|position) (is )?(closed|expired)|page (you are looking for )?"
    r"(was )?not found|couldn'?t find (that|this) (job|page)|does not exist", re.I)
CONSENT_RX = re.compile(r"agree|consent|acknowledge|terms|privacy|policy|confirm|certify|declare|accept", re.I)
CAPTCHA_RX = re.compile(r"captcha|verify you are human|i'?m not a robot", re.I)
URL_OK_RX = re.compile(r"confirm|thank|success|submitted|complete", re.I)

FIELDS_JS = r"""() => {
  const clean = t => (t || '').replace(/\s+/g, ' ').trim();
  const vis = el => { const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const isOpt = l => !!l.querySelector('input[type=radio],input[type=checkbox]') ||
    ['radio', 'checkbox'].includes(((l.htmlFor && document.getElementById(l.htmlFor)) || {}).type);
  const keyCount = c => { const s = new Set();
    c.querySelectorAll('input:not([type=hidden]):not([type=submit]):not([type=button]),select,textarea')
      .forEach(e => s.add(e.type === 'radio' || e.type === 'checkbox' ? e.type + ':' + e.name : e));
    return s.size; };
  const ownLabel = el => {
    if (el.id) { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l) return l.innerText; }
    const w = el.closest('label'); return w ? w.innerText : '';
  };
  const ancestorLabel = el => {
    const fs = el.closest('fieldset');
    if (fs) { const lg = fs.querySelector('legend'); if (lg && clean(lg.innerText)) return lg.innerText; }
    let c = el.parentElement;
    for (let i = 0; i < 5 && c; i++, c = c.parentElement) {
      if (keyCount(c) > 1) break;
      const l = Array.from(c.querySelectorAll('label, legend, [class*="label" i], [class*="question" i]'))
        .find(x => !isOpt(x) && !x.contains(el) && clean(x.innerText));
      if (l) return l.innerText;
    }
    return '';
  };
  const labelledBy = el => (el.getAttribute('aria-labelledby') || '').split(' ')
    .map(i => (document.getElementById(i) || {}).innerText || '').join(' ');
  const out = [], groups = {};
  let idx = 0;
  for (const el of document.querySelectorAll('input, select, textarea')) {
    const type = (el.type || el.tagName).toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset'].includes(type) || el.disabled) continue;
    if (type !== 'file' && !vis(el)) continue;
    const isGroup = type === 'radio' || (type === 'checkbox' && el.name &&
      document.querySelectorAll('input[type=checkbox][name="' + CSS.escape(el.name) + '"]').length > 1);
    const gk = type + ':' + el.name;
    if (isGroup && groups[gk]) {
      groups[gk].options.push({idx: idx, label: clean(ownLabel(el)) || el.value, checked: el.checked});
      if (el.required) groups[gk].required = true;
      el.setAttribute('data-ja-idx', String(idx++));
      continue;
    }
    el.setAttribute('data-ja-idx', String(idx));
    let cands;
    if (isGroup) cands = [ancestorLabel(el)];
    else if (type === 'checkbox') cands = [ownLabel(el), ancestorLabel(el)];
    else cands = [ownLabel(el), el.getAttribute('aria-label'), labelledBy(el), ancestorLabel(el), el.placeholder, el.name];
    const raw = cands.map(clean).find(Boolean) || '';
    const required = el.required || el.getAttribute('aria-required') === 'true' || /[*✱]|\(required\)/i.test(raw);
    const entry = {idx: idx, type: type, name: el.name || '', id: el.id || '',
      label: raw.replace(/[*✱]/g, '').replace(/\(required\)/i, '').replace(/\s+/g, ' ').trim(),
      required: required, value: el.value || '', checked: !!el.checked, options: [], kind: 'text'};
    if (el.tagName === 'SELECT') { entry.kind = 'select'; entry.type = 'select';
      entry.options = Array.from(el.options).map(o => clean(o.text)); }
    else if (type === 'file') entry.kind = 'file';
    else if (isGroup) { entry.kind = type === 'radio' ? 'radio' : 'checkgroup';
      entry.options = [{idx: idx, label: clean(ownLabel(el)) || el.value, checked: el.checked}]; groups[gk] = entry; }
    else if (type === 'checkbox') entry.kind = 'checkbox';
    else if (el.getAttribute('role') === 'combobox' || /list|both/.test(el.getAttribute('aria-autocomplete') || '') ||
             el.getAttribute('aria-haspopup') === 'listbox') entry.kind = 'combobox';
    out.push(entry); idx++;
  }
  return out;
}"""

ERROR_JS = r"""() => {
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const a = Array.from(document.querySelectorAll('[role="alert"], .error, .field-error, .error-message'))
    .filter(vis).map(e => (e.innerText || '').trim()).filter(t => t && t.length < 200);
  if (a.length) return a[0];
  const i = Array.from(document.querySelectorAll('[aria-invalid="true"], input:invalid, select:invalid, textarea:invalid'))
    .filter(vis)[0];
  return i ? ('invalid field: ' + (i.name || i.id || i.type)) : '';
}"""


class ApplyFail(Exception):
    def __init__(self, msg: str, retryable: bool = False, kind: str = "failed"):
        super().__init__(msg)
        self.msg, self.retryable, self.kind, self.shot = msg, retryable, kind, ""


@dataclass
class Result:
    status: str                  # applied | dryrun | failed | expired | unconfirmed
    note: str = ""
    shot: str = ""
    attempts: int = 1


def _safe(s, n: int = 30) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", str(s or "")).strip("_")[:n] or "x"


def _describe(e: Exception) -> str:
    return (str(e).strip().splitlines() or [type(e).__name__])[0][:160]


def _transient(e: Exception) -> bool:
    try:
        from playwright.sync_api import TimeoutError as PWTimeout
        if isinstance(e, PWTimeout):
            return True
    except Exception:  # noqa: BLE001
        pass
    return bool(re.search(r"net::ERR|Target (page, context or browser has been )?closed|Execution context was "
                          r"destroyed|browser has been closed|Navigation failed|Timeout|crash", str(e), re.I))


def adjust_url(url: str) -> str:
    """Point at the application form itself on platforms that separate job page and form."""
    u = urlparse(url)
    host, path = u.netloc.lower(), u.path.rstrip("/")
    if host.endswith("lever.co") and path and not path.endswith("/apply"):
        return url.split("?")[0].rstrip("/") + "/apply"
    if host.endswith("ashbyhq.com") and path and not path.endswith("/application"):
        return url.split("?")[0].rstrip("/") + "/application"
    if host.endswith("workable.com") and "/j/" in path and not path.endswith("/apply"):
        return url.split("?")[0].rstrip("/") + "/apply/"
    return url


def _chk(deadline: float) -> None:
    if time.monotonic() > deadline:
        raise ApplyFail("Timed out", retryable=True)


def _fields(target) -> list:
    try:
        return target.evaluate(FIELDS_JS)
    except Exception:  # noqa: BLE001 - page navigating / frame gone
        return []


def looks_like_form(fields: list) -> bool:
    if any(f["type"] == "password" for f in fields):
        return False
    has_email = any(f["type"] == "email" or re.search(r"e-?mail", f"{f['label']} {f['name']} {f['id']}", re.I)
                    for f in fields)
    return has_email and len(fields) >= 3


def _loc(target, idx):
    return target.locator(f'[data-ja-idx="{idx}"]')


def dismiss_banners(page) -> None:
    try:
        page.get_by_role("button", name=re.compile(
            r"^(accept( all)?( cookies)?|allow all( cookies)?|got it|i agree|ok)$", re.I)).first.click(timeout=800)
    except Exception:  # noqa: BLE001
        pass


def click_apply(context, page):
    loc = page.locator("a, button").filter(has_text=re.compile(
        r"^\s*(apply( now| for this (job|position|role))?|apply to this job|i'?m interested)\s*$", re.I))
    for i in range(min(loc.count(), 5)):
        el = loc.nth(i)
        try:
            if not el.is_visible():
                continue
            before = len(context.pages)
            el.click()
            page.wait_for_timeout(1500)
            return context.pages[-1] if len(context.pages) > before else page
        except Exception:  # noqa: BLE001
            continue
    return None


def locate_form(context, page, deadline):
    for attempt in range(3):
        _chk(deadline)
        for target in [page] + [f for f in page.frames if f != page.main_frame]:
            fields = _fields(target)
            if looks_like_form(fields):
                return page, target, fields
        nxt = click_apply(context, page) if attempt < 2 else None
        if nxt is not None:
            page = nxt
        else:
            page.wait_for_timeout(1500)
    try:
        body = page.inner_text("body", timeout=3000)[:4000]
    except Exception:  # noqa: BLE001
        body = ""
    if EXPIRED_RX.search(body):
        raise ApplyFail("Job is no longer available", kind="expired")
    raise ApplyFail("No application form found (login required or unsupported page)")


OPTION_SEL = '[role="option"], [class*="option" i]:not(select):not(option), li[id*="option" i]'


def _find_option(target, page, answer: str, text: str, wait_ms: int = 3500):
    end, rx_full = time.monotonic() + wait_ms / 1000, re.compile(r"^\s*" + re.escape(answer.strip()) + r"\s*$", re.I)
    rx_part = re.compile(re.escape(text), re.I)
    while True:
        opts = target.locator(OPTION_SEL)
        for rx in (rx_full, re.compile(re.escape(answer.strip()), re.I), rx_part):
            pick = opts.filter(has_text=rx)
            for k in range(min(pick.count(), 6)):
                try:
                    if pick.nth(k).is_visible():
                        return pick.nth(k)
                except Exception:  # noqa: BLE001
                    pass
        if time.monotonic() >= end:
            return None
        page.wait_for_timeout(300)


def fill_combobox(target, page, loc, answer: str) -> None:
    """Options live in the same frame as the field (matters for embedded Greenhouse forms)."""
    text = answer.split(",")[0].strip() or answer
    loc.click()
    pick = None
    for attempt in range(2):
        if attempt == 0:
            loc.press_sequentially(text, delay=30)
        else:                                          # list may not filter: clear and read the full list
            loc.fill("")
            loc.click()
        pick = _find_option(target, page, answer, text, 3500 if attempt == 0 else 2500)
        if pick is not None:
            break
    if pick is None:
        raise ApplyFail(f"No dropdown option matched '{answer[:40]}'")
    pick.click()


def fill_form(target, page, fields: list, ctx: Ctx, resume: str, upload_wait: float, deadline: float) -> int:
    filled, uploaded, missing = 0, False, []
    files = [f for f in fields if f["kind"] == "file"]
    for f in files:                                    # phase A: resume first (some sites autofill from it)
        txt = f"{f['label']} {f['name']} {f['id']}".lower()
        is_resume = bool(re.search(r"resume|\bcv\b|curriculum", txt)) or (
            len(files) == 1 and not re.search(r"cover|letter|portfolio|transcript", txt))
        if is_resume and not uploaded:
            _loc(target, f["idx"]).set_input_files(str(resume))
            uploaded, filled = True, filled + 1
        elif f["required"]:
            raise ApplyFail(f"Required upload not supported: {f['label'][:60]}")
    if uploaded:
        page.wait_for_timeout(int(upload_wait * 1000))
        fields = _fields(target) or fields
    for f in fields:                                   # phase B: everything else
        _chk(deadline)
        kind, label, req = f["kind"], f["label"], f["required"]
        if kind == "file":
            continue
        loc = _loc(target, f["idx"])
        if kind in ("text", "combobox"):
            ans = resolve(label, f["type"], ctx)
            if ans is None:
                if req and not f["value"].strip():
                    ctx.note(label)
                    missing.append(label)
                continue
            if kind == "text" and f["value"].strip() == ans.strip():
                continue
            if kind == "combobox":
                fill_combobox(target, page, loc, ans)
            else:
                loc.fill(ans)
            filled += 1
        elif kind == "select":
            opts = real_options(f["options"])
            ans = resolve(label, "text", ctx)
            opt = choose_option(label, ans, opts)
            if opt:
                loc.select_option(label=opt)
                filled += 1
            elif req and not f["value"].strip():
                ctx.note(label, f["options"])
                missing.append(label)
        elif kind in ("radio", "checkgroup"):
            ans = resolve(label, "text", ctx)
            opt = choose_option(label, ans, [o["label"] for o in f["options"]]) if ans else None
            if opt:
                idx = next(o["idx"] for o in f["options"] if o["label"] == opt)
                _loc(target, idx).check(force=True)
                filled += 1
            elif req and not any(o.get("checked") for o in f["options"]):
                ctx.note(label, [o["label"] for o in f["options"]])
                missing.append(label)
        elif kind == "checkbox":
            if f["checked"] or not req:
                continue
            if CONSENT_RX.search(label):
                loc.check(force=True)
                filled += 1
            else:
                ctx.note(label, ["Yes", "No"])
                missing.append(label)
    if missing:
        raise ApplyFail("No answer for required question: " + " | ".join(m[:70] for m in missing[:5])
                        + (f" (+{len(missing) - 5} more)" if len(missing) > 5 else "")
                        + " -> add answers in profile/my_answers.yaml")
    return filled


def find_submit(target):
    loc = target.locator('button, input[type="submit"], [role="button"]')
    best = None
    for i in range(min(loc.count(), 60)):
        el = loc.nth(i)
        try:
            if not el.is_visible() or not el.is_enabled():
                continue
            t = (el.inner_text() or el.get_attribute("value") or "").strip()
        except Exception:  # noqa: BLE001
            continue
        if re.search(r"submit|send application|apply( now)?$|complete application", t, re.I) and \
                not re.search(r"cancel|back|linkedin|indeed|google|save|autofill", t, re.I):
            best = el
    return best


def _form_visible(target) -> bool:
    return looks_like_form(_fields(target))


def wait_outcome(page, target, url_before: str, wait_s: float):
    end, body = time.monotonic() + wait_s, ""
    while True:
        page.wait_for_timeout(1000)
        try:
            body = page.inner_text("body", timeout=3000)[:6000]
            gone = not _form_visible(target)
            if gone and (SUCCESS_RX.search(body) or (page.url != url_before and URL_OK_RX.search(page.url))):
                return "ok", ""
        except Exception:  # noqa: BLE001 - mid-navigation
            pass
        if time.monotonic() >= end:
            break
    try:
        challenge = any(page.locator(s).first.is_visible() for s in (
            'iframe[src*="hcaptcha.com/captcha"]', 'iframe[src*="recaptcha/api2/bframe"]',
            'iframe[title*="challenge" i]') if page.locator(s).count())
    except Exception:  # noqa: BLE001
        challenge = False
    if challenge or CAPTCHA_RX.search(body):
        return "captcha", ""
    try:
        err = target.evaluate(ERROR_JS)
    except Exception:  # noqa: BLE001
        err = ""
    if err:
        return "error", err[:120]
    return "unconfirmed", ""


class Applier:
    def __init__(self, *, headless: bool = True, shot_dir="output/apply_logs", timeout: int = 120,
                 confirm_wait: float = 20, upload_wait: float = 3.0, on_context=None,
                 max_attempts: int = 2, retry_pause: float = 3.0):
        self.headless, self.shot_dir, self.timeout = headless, Path(shot_dir), timeout
        self.confirm_wait, self.upload_wait, self.on_context = confirm_wait, upload_wait, on_context
        self.max_attempts, self.retry_pause = max_attempts, retry_pause
        self._pw = self._browser = None

    def __enter__(self):
        self._launch()
        return self

    def __exit__(self, *exc):
        self.close()

    def _launch(self) -> None:
        from playwright.sync_api import sync_playwright
        if self._pw is None:
            self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)

    def _ok(self) -> bool:
        return self._browser is not None and self._browser.is_connected()

    def close(self) -> None:
        for obj, meth in ((self._browser, "close"), (self._pw, "stop")):
            try:
                if obj:
                    getattr(obj, meth)()
            except Exception:  # noqa: BLE001
                pass
        self._pw = self._browser = None

    def _shot(self, page, job: dict, tag: str = "fail") -> str:
        if page is None:
            return ""
        try:
            self.shot_dir.mkdir(parents=True, exist_ok=True)
            p = self.shot_dir / f"{_safe(job.get('company'))}_{job.get('id')}_{tag}.png"
            page.screenshot(path=str(p), full_page=True, timeout=10000)
            return str(p)
        except Exception:  # noqa: BLE001
            return ""

    def apply(self, job: dict, ctx: Ctx, resume: str, dry_run: bool = False) -> Result:
        last, attempt = ApplyFail("Unknown error"), 0
        for attempt in range(1, self.max_attempts + 1):
            try:
                if not self._ok():
                    self.close()
                    self._launch()
                res = self._attempt(job, ctx, resume, dry_run)
                res.attempts = attempt
                return res
            except ApplyFail as e:
                last = e
            except Exception as e:  # noqa: BLE001 - e.g. browser failed to launch
                last = ApplyFail(_describe(e), retryable=_transient(e))
            if not last.retryable or attempt == self.max_attempts:
                break
            log.warning("Retrying %s after: %s", job.get("company"), last.msg)
            time.sleep(self.retry_pause)
        return Result(last.kind, last.msg, last.shot, attempts=attempt)

    def _attempt(self, job, ctx, resume, dry_run) -> Result:
        context = self._browser.new_context(viewport={"width": 1366, "height": 900}, locale="en-IN",
                                            timezone_id="Asia/Kolkata", user_agent=UA)
        page = None
        try:
            if self.on_context:
                self.on_context(context)
            page = context.new_page()
            page.set_default_timeout(15000)
            page.set_default_navigation_timeout(60000)
            page.on("dialog", lambda d: d.accept())
            return self._flow(context, page, job, ctx, resume, dry_run, time.monotonic() + self.timeout)
        except ApplyFail as e:
            e.shot = e.shot or self._shot(page, job)
            raise
        except Exception as e:  # noqa: BLE001
            fail = ApplyFail(_describe(e), retryable=_transient(e))
            fail.shot = self._shot(page, job)
            raise fail from e
        finally:
            try:
                context.close()
            except Exception:  # noqa: BLE001
                pass

    def _flow(self, context, page, job, ctx, resume, dry_run, deadline) -> Result:
        url = adjust_url(job.get("apply_url") or job.get("url") or "")
        if not url:
            raise ApplyFail("No apply link")
        resp = page.goto(url, wait_until="domcontentloaded")
        if resp is not None and resp.status in (404, 410):
            raise ApplyFail(f"Job page not found (HTTP {resp.status})", kind="expired")
        if resp is not None and resp.status >= 500:
            raise ApplyFail(f"Site error (HTTP {resp.status})", retryable=True)
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:  # noqa: BLE001
            pass
        dismiss_banners(page)
        page, target, fields = locate_form(context, page, deadline)
        n = fill_form(target, page, fields, ctx, resume, self.upload_wait, deadline)
        _chk(deadline)
        if dry_run:
            return Result("dryrun", f"DRY RUN ok - {n} field(s) filled, form ready, not submitted",
                          self._shot(page, job, "dryrun"))
        btn = find_submit(target)
        if btn is None:
            raise ApplyFail("Submit button not found")
        url_before = page.url
        btn.click()
        outcome, detail = wait_outcome(page, target, url_before, self.confirm_wait)
        if outcome == "ok":
            return Result("applied", f"Submitted via {job.get('ats') or 'company site'}",
                          self._shot(page, job, "done"))
        if outcome == "captcha":
            raise ApplyFail("CAPTCHA blocked the submission - apply manually")
        if outcome == "error":
            raise ApplyFail(f"Form errors: {detail}")
        raise ApplyFail("Clicked submit but could not confirm - check your email before applying again",
                        kind="unconfirmed")
