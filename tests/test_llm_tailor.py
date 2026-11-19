import os
import unittest
from pathlib import Path
from unittest import mock

from test_phase1 import mk
from jobbot.llm import LLMChain, LLMError, extract_json
from jobbot.tailor import guard, load_profile, tailor_resume
from jobbot.keywords import terms_in

PROFILE = load_profile(Path(__file__).resolve().parent / "fixtures" / "sample_profile.yaml")


class FakeResp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body, self.text = status, body or {}, str(body)

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, handler):
        self.handler, self.calls = handler, []

    def post(self, url, **kw):
        self.calls.append(url)
        return self.handler(url, **kw)


def gemini_ok(text):
    return FakeResp(200, {"candidates": [{"content": {"parts": [{"text": text}]}}]})


def openai_ok(text):
    return FakeResp(200, {"choices": [{"message": {"content": text}}]})


ENV = {"GEMINI_API_KEY": "g", "GROQ_API_KEY": "q", "OPENROUTER_API_KEY": "o"}


class TestLLM(unittest.TestCase):
    def test_extract_json_variants(self):
        self.assertEqual(extract_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(extract_json('Sure! {"a": 1} hope it helps'), {"a": 1})
        with self.assertRaises(ValueError):
            extract_json("no json here")

    def test_no_keys_means_unavailable(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(LLMChain().available)

    def test_rotation_across_providers(self):
        def handler(url, **kw):
            return gemini_ok('{"n": 1}') if "googleapis" in url else openai_ok('{"n": 2}')
        with mock.patch.dict(os.environ, ENV):
            chain = LLMChain(session=FakeSession(handler), min_interval=0, sleep=lambda s: None)
            names = [chain.complete_json("s", "u")[1] for _ in range(3)]
        self.assertEqual(names, ["gemini", "groq", "openrouter"])

    def test_failover_and_cooldown_on_429(self):
        def handler(url, **kw):
            return FakeResp(429, {}) if "googleapis" in url else openai_ok('{"ok": true}')
        with mock.patch.dict(os.environ, ENV):
            sess = FakeSession(handler)
            chain = LLMChain(session=sess, min_interval=0, sleep=lambda s: None)
            self.assertEqual(chain.complete_json("s", "u")[1], "groq")
            self.assertEqual(chain.complete_json("s", "u")[1], "openrouter")
            gem_calls = sum("googleapis" in c for c in sess.calls)
            chain.complete_json("s", "u")
            self.assertEqual(sum("googleapis" in c for c in sess.calls), gem_calls)  # cooling down

    def test_all_providers_failing_raises(self):
        with mock.patch.dict(os.environ, ENV):
            chain = LLMChain(session=FakeSession(lambda u, **k: FakeResp(500, {})),
                             min_interval=0, sleep=lambda s: None)
            with self.assertRaises(LLMError):
                chain.complete_json("s", "u")


class FakeLLM:
    available = True

    def __init__(self, data=None, boom=False):
        self.data, self.boom = data or {}, boom

    def complete_json(self, system, user):
        if self.boom:
            raise LLMError("down")
        return self.data, "fake"


JOB = mk(title="Product Analyst", company="Acme",
         description="Jira, SQL, user stories, order management. JMeter performance testing a plus.")
GOOD_B1 = ("**Resolved queries from 8+ enterprise clients** by translating business requirements "
           "into functional solutions for order management customers.")


class TestGuardAndTailor(unittest.TestCase):
    def test_guard_rules(self):
        t, n = terms_in("jira sql"), {"8", "100"}
        self.assertIsNone(guard("Used Jira and SQL for 8 clients", t, n))
        self.assertIn("unknown terms", guard("Used Zendesk daily", t, n))
        self.assertIn("years", guard("Brings 5+ years of Jira", t, n))
        self.assertIn("unknown numbers", guard("Handled 250 clients with Jira", t, n))
        self.assertIn("unbalanced", guard("**Jira", t, n))

    def test_offline_keeps_original_text_and_gates_extras(self):
        t = tailor_resume(PROFILE, JOB, "product", None)
        self.assertEqual(t.engine, "offline")
        self.assertEqual(t.summary, PROFILE["summary"])
        self.assertEqual(t.headline, PROFILE["headlines"]["product"])
        self.assertEqual(len(t.bullets), 5)                       # JD mentions JMeter -> x1 added
        plain = tailor_resume(PROFILE, mk(description="Jira and SQL"), "product", None)
        self.assertEqual(len(plain.bullets), 4)                   # no extras without a trigger
        skills = " ".join(x for c in plain.competencies for x in c["primary"] + c["secondary"])
        self.assertNotIn("JMeter", skills)

    def test_llm_rewrite_accepted_when_truthful(self):
        data = {"summary": "Customer support and order management professional with enterprise SaaS "
                           "experience, coordinating functional solutions for customer issues and "
                           "supporting production releases for enterprise clients.",
                "bullets": {"b1": GOOD_B1}, "jd_summary": "Product analyst role."}
        t = tailor_resume(PROFILE, JOB, "product", FakeLLM(data))
        self.assertEqual(t.engine, "fake")
        self.assertIn("Resolved queries", " ".join(t.bullets))
        self.assertTrue(t.summary.startswith("Customer support and order management"))
        self.assertEqual(t.jd_summary, "Product analyst role.")

    def test_llm_inventions_are_rejected_per_field(self):
        data = {"summary": "Seasoned analyst with 6+ years of Zendesk and Salesforce experience "
                           "across many enterprise customer support teams worldwide today.",
                "bullets": {"b1": "**Managed Zendesk for 300 clients** with Kubernetes dashboards today ok."}}
        t = tailor_resume(PROFILE, JOB, "product", FakeLLM(data))
        self.assertEqual(t.summary, PROFILE["summary"])
        self.assertTrue(any("Handled queries from 8+" in b for b in t.bullets))
        self.assertTrue(any("rejected" in n for n in t.notes))

    def test_llm_failure_falls_back_offline(self):
        t = tailor_resume(PROFILE, JOB, "product", FakeLLM(boom=True))
        self.assertEqual((t.engine, t.summary), ("offline", PROFILE["summary"]))
        self.assertTrue(t.notes)


if __name__ == "__main__":
    unittest.main()
