import unittest

from jobbot.people import extract_cards, is_blocked_url, pick_contacts

HTML = """<ul>
<li><a href="https://www.linkedin.com/in/jane-doe?miniProfileUrn=x">Jane Doe</a> 2nd Senior Technical Recruiter at Acme Corp Pune</li>
<li><a href="https://www.linkedin.com/in/bob-x/">Bob X</a> Software Engineer at Acme Corp</li>
<li><a href="https://in.linkedin.com/in/jane-doe">Jane Doe again</a></li>
<li><a href="https://www.linkedin.com/company/acme">Acme</a></li>
<li><a href="https://www.linkedin.com/in/raj-hr">Raj</a> Talent Acquisition Partner at Other Inc</li>
</ul>"""


class TestPeople(unittest.TestCase):
    def test_blocked_urls(self):
        for u in ("https://www.linkedin.com/login?x", "https://www.linkedin.com/checkpoint/challenge/1",
                  "https://www.linkedin.com/authwall?trk=x"):
            self.assertTrue(is_blocked_url(u))
        self.assertFalse(is_blocked_url("https://www.linkedin.com/feed/"))

    def test_pick_contacts_needs_recruiter_title_and_company(self):
        cards = [{"url": "u1", "text": "Jane Senior Technical Recruiter at Acme Corp"},
                 {"url": "u2", "text": "Bob Software Engineer at Acme Corp"},
                 {"url": "u3", "text": "Raj Talent Acquisition at Other Inc"}]
        self.assertEqual(pick_contacts(cards, "Acme Corp Pvt Ltd"),
                         [("Jane Senior Technical Recruiter at Acme Corp", "u1")])
        self.assertEqual(pick_contacts(cards, "Zzz"), [])
        self.assertEqual(len(pick_contacts(cards, "Acme", limit=5)), 1)

    def test_extract_cards_from_results_page(self):
        try:
            from playwright.sync_api import sync_playwright
            pw = sync_playwright().start()
            browser = pw.chromium.launch()
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"Chromium not available: {exc}")
        try:
            page = browser.new_page()
            page.set_content(HTML)
            cards = extract_cards(page)
        finally:
            browser.close()
            pw.stop()
        self.assertEqual([c["url"] for c in cards],
                         ["https://www.linkedin.com/in/jane-doe", "https://www.linkedin.com/in/bob-x",
                          "https://www.linkedin.com/in/raj-hr"])
        self.assertEqual(pick_contacts(cards, "Acme Corp")[0][1], "https://www.linkedin.com/in/jane-doe")


if __name__ == "__main__":
    unittest.main()
