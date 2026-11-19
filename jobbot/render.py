"""HTML -> PDF resume renderer (Playwright/Chromium), styled after the base CV.

Letter size, Arial body, dark-blue headings, shaded table headers, bullets with bold
lead phrases and a centred footer. If the content spills past `max_pages`, the font
is scaled down in small steps until it fits.
"""
from __future__ import annotations

import html
import re
from pathlib import Path

from .tailor import Tailored


class RenderError(Exception):
    pass


def md(text: str) -> str:
    s = html.escape(text or "", quote=False)
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)


def esc(text: str) -> str:
    return html.escape(text or "", quote=False)


def count_pages(pdf: bytes) -> int:
    return len(re.findall(rb"/Type\s*/Page(?![s\w])", pdf)) or 1


def build_html(profile: dict, t: Tailored, scale: float = 1.0) -> str:
    fs = 10.0 * scale
    c = profile["contact"]
    css = f"""
    * {{ box-sizing: border-box; }}
    body {{ font-family: Arial, 'Liberation Sans', Helvetica, sans-serif; font-size: {fs:.2f}pt;
           line-height: 1.32; color: #111; margin: 0; }}
    h1 {{ font-size: {fs*1.9:.1f}pt; font-weight: 400; color: #1F3A5A; margin: 0; }}
    .headline {{ font-weight: 700; color: #1F3A5A; margin: 2pt 0 3pt; }}
    .contact {{ font-size: {fs*.92:.1f}pt; }}
    .contact a {{ color: #0563C1; }}
    hr {{ border: 0; border-top: 1px solid #999; margin: 6pt 0 4pt; }}
    h2 {{ font-family: Calibri, Carlito, Arial, sans-serif; font-size: {fs*1.3:.1f}pt; font-weight: 700;
         color: #2B4A6F; text-transform: uppercase; margin: {fs*1.25:.1f}pt 0 {fs*.45:.1f}pt;
         break-after: avoid; }}
    p {{ margin: 0; }}
    .role {{ font-weight: 700; margin: 0 0 {fs*.3:.1f}pt; }}
    table {{ width: 100%; border-collapse: collapse; font-size: {fs*.95:.1f}pt; }}
    th, td {{ border: 1px solid #C5CFDB; padding: {fs*.4:.1f}pt {fs*.6:.1f}pt; text-align: left;
             vertical-align: top; }}
    th {{ background: #E8EDF3; font-weight: 700; }}
    td.cat {{ font-weight: 700; background: #F5F7FA; }}
    tr, .keep {{ break-inside: avoid; }}
    ul {{ list-style: none; margin: 0; padding: 0; }}
    ul.dot li {{ position: relative; padding-left: {fs*2:.1f}pt; margin: 0 0 {fs*.55:.1f}pt; }}
    ul.dot li::before {{ content: "\\25CF"; position: absolute; left: {fs*.6:.1f}pt;
                         font-size: {fs*.75:.1f}pt; top: {fs*.05:.1f}pt; }}
    ul.small li {{ padding-left: {fs*.9:.1f}pt; margin: 0 0 {fs*.2:.1f}pt; position: relative; }}
    ul.small li::before {{ content: "\\2022"; position: absolute; left: 0; }}
    """
    comp_rows = "".join(
        f"<tr><td class='cat' style='width:29%'>{esc(x['category'])}</td>"
        f"<td style='width:36%'>{esc(', '.join(x['primary']))}</td>"
        f"<td style='width:35%'>{esc(', '.join(x['secondary']))}</td></tr>"
        for x in t.competencies)
    edu_rows = "".join(
        f"<tr><td style='width:29%'>{esc(e['qualification'])}</td><td style='width:30%'>"
        f"{esc(e['institute'])}</td><td style='width:21%'>{esc(e['period'])}</td>"
        f"<td style='width:20%'>{esc(e['result'])}</td></tr>"
        for e in profile["education"])
    exp, proj = profile["experience"], profile.get("project")
    bullets = "".join(f"<li>{md(b)}</li>" for b in t.bullets)
    ach = "".join(f"<li>{md(a)}</li>" for a in profile.get("achievements", []))
    certs = "".join(f"<li>{md(x)}</li>" for x in profile.get("certifications", []))
    project = (f"<div class='keep'><h2>Project Profile</h2><div class='role'>{esc(proj['name'])} | "
               f"{esc(proj['role'])}</div><p>{esc(proj['text'])}</p></div>") if proj else ""
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>{css}</style></head><body>
    <h1>{esc(profile['name'])}</h1>
    <div class="headline">{esc(t.headline)}</div>
    <div class="contact"><b>{esc(c['phone'])}</b> &nbsp;|&nbsp;
      <a href="mailto:{esc(c['email'])}">{esc(c['email'])}</a> &nbsp;|&nbsp;
      <a href="https://{esc(c['linkedin'])}">{esc(c['linkedin'])}</a> &nbsp;|&nbsp; {esc(c['location'])}</div>
    <hr>
    <h2>Profile Summary</h2><p>{esc(t.summary)}</p>
    <h2>Competencies</h2>
    <table><tr><th>Category</th><th>Major Skills – Primary</th><th>Minor Skills – Secondary</th></tr>{comp_rows}</table>
    <h2>Experience</h2>
    <div class="role">{esc(exp['title'])} | {esc(exp['company'])}</div>
    <ul class="dot">{bullets}</ul>
    {project}
    <h2>Key Achievements</h2><ul class="small">{ach}</ul>
    <h2>Academic Qualifications</h2>
    <table><tr><th>Qualification</th><th>Institute</th><th>Period</th><th>Result</th></tr>{edu_rows}</table>
    <h2>Certifications</h2><ul class="dot">{certs}</ul>
    </body></html>"""


class ResumeRenderer:
    SCALES = (1.0, 0.96, 0.92, 0.88, 0.84)

    def __init__(self, max_pages: int = 2):
        self.max_pages = max_pages
        self._pw = self._browser = None

    def __enter__(self):
        try:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch()
        except Exception as exc:  # noqa: BLE001
            self.close()
            raise RenderError(
                "Could not start Chromium for PDF rendering. Run once:  pip install playwright  "
                f"and  playwright install chromium   ({str(exc)[:120]})") from exc
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self) -> None:
        for obj, meth in ((self._browser, "close"), (self._pw, "stop")):
            try:
                if obj:
                    getattr(obj, meth)()
            except Exception:  # noqa: BLE001
                pass
        self._pw = self._browser = None

    def render(self, profile: dict, tailored: Tailored, out_path) -> int:
        if self._browser is None:
            raise RenderError("renderer is not started")
        footer = (f"<div style=\"font-family:Arial,sans-serif;font-size:8px;color:#888;width:100%;"
                  f"text-align:center\">{esc(profile['name'])} | {esc(tailored.headline)}</div>")
        pdf, pages = b"", 0
        for scale in self.SCALES:
            page = self._browser.new_page()
            try:
                page.set_content(build_html(profile, tailored, scale), wait_until="load")
                pdf = page.pdf(
                    format="Letter", print_background=True, display_header_footer=True,
                    header_template="<span></span>", footer_template=footer,
                    margin={"top": "0.5in", "bottom": "0.7in", "left": "0.55in", "right": "0.55in"})
            except Exception as exc:  # noqa: BLE001
                raise RenderError(f"PDF render failed: {str(exc)[:150]}") from exc
            finally:
                page.close()
            pages = count_pages(pdf)
            if pages <= self.max_pages:
                break
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(pdf)
        return pages
