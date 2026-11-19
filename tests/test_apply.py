import pytest

pw = pytest.importorskip("playwright.sync_api")
from jobbot.applier import Applier, adjust_url
from jobbot.qa import Ctx

CTX = Ctx({"Full Name": "Asha Rao", "Email": "v@x.com", "Phone": "+91 1", "LinkedIn": "https://l/in/v",
           "Notice Period": "7 days", "Current CTC": "6 LPA", "Expected CTC": "10 LPA"}, 2.6,
          [("pronoun", "He/Him")])
FORM = """<html><body><form id=f onsubmit="document.body.innerHTML='<h1>Thank you for applying</h1>';return false">
<label for=n>Full name *</label><input id=n name=name required>
<label for=e>Email *</label><input id=e name=email type=email required>
<label for=p>Phone</label><input id=p name=phone>
<label for=r>Resume *</label><input id=r type=file name=resume required>
<label for=c>Expected CTC *</label><input id=c name=ctc required>
{extra}
<input type=checkbox id=k required><label for=k>I agree to the privacy policy</label>
<button type=submit>Submit application</button></form></body></html>"""


@pytest.fixture(scope="module")
def ap(tmp_path_factory):
    a = Applier(shot_dir=tmp_path_factory.mktemp("s"), timeout=40, confirm_wait=5, upload_wait=0.2,
                retry_pause=0.1)
    try:
        a.__enter__()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"no browser: {e}")
    yield a
    a.close()


def route(html, status=200):
    def on(ctx):
        ctx.route("**/*", lambda r: r.fulfill(status=status, content_type="text/html", body=html))
    return on


@pytest.fixture
def resume(tmp_path):
    p = tmp_path / "cv.pdf"
    p.write_bytes(b"%PDF-1.4 test")
    return str(p)


def run(ap, html, resume, dry=False, status=200):
    ap.on_context = route(html, status)
    return ap.apply({"id": 1, "company": "Acme", "apply_url": "https://boards.example.com/acme/1",
                     "ats": "lever"}, CTX, resume, dry)


def test_happy(ap, resume):
    r = run(ap, FORM.format(extra=""), resume)
    assert r.status == "applied", r.note


def test_dry_run(ap, resume):
    r = run(ap, FORM.format(extra=""), resume, dry=True)
    assert r.status == "dryrun" and r.shot


def test_unknown_required(ap, resume):
    r = run(ap, FORM.format(extra="<label for=q>Why us? *</label><input id=q name=q required>"), resume)
    assert r.status == "failed" and "Why us" in r.note


def test_rule_select_radio(ap, resume):
    extra = ("<label for=s>Notice period *</label><select id=s required><option value=''>Select</option>"
             "<option>Immediate</option><option>60 days</option></select>"
             "<fieldset><legend>Preferred pronouns *</legend><label><input type=radio name=pr value=a required>"
             " He/Him</label><label><input type=radio name=pr value=b> She/Her</label></fieldset>")
    r = run(ap, FORM.format(extra=extra), resume)
    assert r.status == "applied", r.note


def test_expired(ap, resume):
    assert run(ap, "<h1>Not found</h1>", resume, status=404).status == "expired"


def test_captcha_or_unconfirmed(ap, resume):
    html = FORM.replace("document.body.innerHTML='<h1>Thank you for applying</h1>'", "void 0")
    r = run(ap, html.format(extra=""), resume)
    assert r.status in ("unconfirmed", "failed")


def test_adjust_url():
    assert adjust_url("https://jobs.lever.co/cred/abc") == "https://jobs.lever.co/cred/abc/apply"
    assert adjust_url("https://jobs.ashbyhq.com/x/abc") == "https://jobs.ashbyhq.com/x/abc/application"
    assert adjust_url("https://boards.greenhouse.io/x/jobs/1") == "https://boards.greenhouse.io/x/jobs/1"


def test_combobox_in_iframe_with_class_options(ap, resume):
    inner = ("<html><body><label for=cb>Country *</label><input id=cb role=combobox>"
             "<div id=l style='display:none'><div class='select__option'>India</div>"
             "<div class='select__option'>Ireland</div></div>"
             "<script>cb.onfocus=cb.onclick=()=>l.style.display='block';"
             "document.querySelectorAll('.select__option').forEach(o=>o.onclick=()=>{cb.value=o.textContent;l.style.display='none'})"
             "</script></body></html>")
    outer = "<html><body><iframe srcdoc=\"" + inner.replace('"', "&quot;") + "\" width=600 height=300></iframe></body></html>"
    from jobbot.applier import fill_combobox
    page = ap._browser.new_page()
    try:
        page.set_content(outer)
        fr = page.frames[1]
        fr.wait_for_selector("#cb")
        fill_combobox(fr, page, fr.locator("#cb"), "India")
        assert fr.locator("#cb").input_value() == "India"
    finally:
        page.close()
