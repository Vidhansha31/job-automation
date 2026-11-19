import tempfile
import unittest
from datetime import timedelta

from test_phase1 import IST, NOW, FakeCollector, FakeSheets, mk
from jobbot.pipeline import run_pipeline
from jobbot.store import Store


def run(jobs, store, **kw):
    with tempfile.TemporaryDirectory() as d:
        return run_pipeline(store=store, collectors=[FakeCollector(jobs)], sheets=FakeSheets(),
                            hours_old=72, tz=IST, data_dir=d, now=NOW, **kw)


class TestAtsBacklog(unittest.TestCase):
    def old_lever(self):
        return mk(source="lever", job_id="l1", url="https://jobs.lever.co/x/l1",
                  posted_at=NOW - timedelta(days=40))

    def test_old_career_page_job_listed_once(self):
        store = Store(":memory:")
        self.assertEqual(run([self.old_lever()], store).new, 1)
        again = run([self.old_lever()], store)
        self.assertEqual((again.new, again.duplicates), (0, 1))

    def test_old_linkedin_job_still_filtered(self):
        old = mk(posted_at=NOW - timedelta(days=40))
        self.assertEqual(run([old], Store(":memory:")).too_old, 1)

    def test_backlog_can_be_disabled(self):
        self.assertEqual(run([self.old_lever()], Store(":memory:"), ats_backlog=False).too_old, 1)


if __name__ == "__main__":
    unittest.main()
