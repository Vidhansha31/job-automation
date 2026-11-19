import unittest
from datetime import timedelta

from test_phase1 import NOW, mk
from jobbot.keywords import terms_in
from jobbot.scoring import parse_exp, score_job, select, years_experience

PROFILE_TERMS = {"jira", "sql", "api", "order management", "customer support"}
YEARS = 2.6


def sc(title, exp="", desc="", **kw):
    return score_job(mk(title=title, experience=exp, description=desc, **kw),
                     years=YEARS, profile_terms=PROFILE_TERMS, now=NOW)


class TestScoring(unittest.TestCase):
    def test_years_and_parse(self):
        self.assertAlmostEqual(years_experience("2024-04", NOW), 2.6, delta=0.1)
        self.assertEqual(parse_exp("2-4 years"), (2.0, 4.0))
        self.assertEqual(parse_exp("5+ years"), (5.0, None))
        self.assertIsNone(parse_exp(""))

    def test_core_titles_are_tier1(self):
        for title, exp in [("Product Analyst", "2-4 years"), ("Associate Product Manager", ""),
                           ("Customer Success Manager", "3+ years"),
                           ("Technical Support Engineer", "1-3 years"),
                           ("Implementation Consultant", "2 years"),
                           ("Performance Test Engineer", "3 years")]:
            self.assertEqual(sc(title, exp).tier, 1, title)

    def test_relaxation_by_experience_and_seniority(self):
        self.assertEqual(sc("Product Analyst", "5+ years").tier, 2)
        self.assertEqual(sc("Senior Customer Support Engineer", "3-5 years").tier, 2)
        self.assertEqual(sc("Product Analyst", "6 years").tier, 3)
        self.assertIsNone(sc("Product Analyst", "9+ years").tier)
        self.assertEqual(sc("Senior Product Manager").tier, 3)   # senior + unknown exp is only a stretch

    def test_exclusions(self):
        self.assertIsNone(sc("Director of Product").tier)
        self.assertIsNone(sc("Product Intern").tier)
        self.assertIsNone(sc("Senior Backend Engineer", "5 years").tier)
        self.assertIsNone(score_job(mk(title="Product Analyst", company="Bad Co"), years=YEARS,
                                    profile_terms=PROFILE_TERMS, now=NOW,
                                    excluded_companies=["bad co"]).tier)

    def test_skills_only_tier4_needs_strong_overlap(self):
        desc = "We use Jira, SQL, APIs, order management, Shopify, NetSuite, SLA and escalation."
        rich = PROFILE_TERMS | {"shopify", "netsuite", "sla", "escalation"}   # 8 of 8 overlap
        s = score_job(mk(title="Operations Specialist", experience="2-3 years", description=desc),
                      years=YEARS, profile_terms=rich, now=NOW)
        self.assertEqual(s.tier, 4)
        self.assertIsNone(sc("Operations Specialist", "2-3 years", desc).tier)   # weak overlap
        self.assertIsNone(sc("Operations Specialist", "2-3 years", "Great team").tier)

    def test_matched_and_missing_keywords(self):
        s = sc("Product Analyst", "", "Needs Jira, Zendesk and SQL")
        self.assertEqual(s.matched, ["jira", "sql"])
        self.assertIn("zendesk", s.missing)

    def test_select_orders_by_tier_then_score(self):
        items = [("a", sc("Product Analyst", "5+ years")), ("b", sc("Product Analyst", "2-4 years")),
                 ("c", sc("Chef")), ("d", sc("Customer Success Manager", "3 years"))]
        picked = [x[0] for x in select(items, 3)]
        self.assertEqual(picked[-1], "a")           # tier-2 comes after the tier-1 jobs
        self.assertNotIn("c", picked)
        self.assertEqual(len(select(items, 1)), 1)

    def test_fresh_job_scores_higher_than_stale(self):
        fresh = sc("Product Analyst", "2-4 years", posted_at=NOW - timedelta(hours=5))
        stale = sc("Product Analyst", "2-4 years", posted_at=NOW - timedelta(days=30))
        self.assertGreater(fresh.score, stale.score)

    def test_terms_plural_and_boundaries(self):
        self.assertEqual(terms_in("REST APIs and JavaScript"), {"api", "rest api", "javascript"})
        self.assertNotIn("git", terms_in("GitHub and digital"))


if __name__ == "__main__":
    unittest.main()
