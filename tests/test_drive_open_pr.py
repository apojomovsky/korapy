"""A finished worker with an open pull request: review, never restart.

The loop's whole point is worker, reviewer, record, then merge. A
worker that finishes with its pull request open must hand over to a
reviewer on the next pass: the pass requests the row, the gate turns
the request into a review takeover, and no fresh start ever launches
on it. When the claim is already gone, the row still gates as
refused (an open pull, never a fresh start), and an approving review
record with green checks reaches the merge step.

Fakes only: a temporary board store, the ready-made basic policy, an
in-memory tracker double for the pulls listing, a recording launcher,
an in-memory forge, and review records through the review-reader
seam. No `gh`, no systemd, no network, no real state dir.
"""

import io
import os
import tempfile
import unittest
from unittest import mock

from korapy import drive, drivehub, reviewrecord
from tests.test_drive import project_named, stored
from tests.test_drive_claims import FakeLauncher, drive_pass
from tests.test_forge import FakeForge, make_pr
from tests.test_tracker import FakeTracker, reading

NOW = 2000.0
REPO = "acme/app"
WORKER = "drive-testworker-7"
REVIEWER = "drive-testreviewer-7"
HEAD = "b" * 40


def basic_project():
    """The registered project on the ready-made basic policy."""
    return project_named("blue", "korapy.policies:basic")


def open_pr_tracker(number=7, pr=9):
    """A tracker double with one open issue and one open pull on it."""
    fake = FakeTracker([reading(repo=REPO, number=number, updated_at=1900.0)])
    fake.index[REPO] = {number: [reading(repo=REPO, number=pr, kind="pr", state="open")]}
    return fake


def approving_record(number=7, head=HEAD):
    """An approving review record naming the finished worker as author."""
    return {
        "repo": "app",
        "number": number,
        "commit": head,
        "reviewer": REVIEWER,
        "author": WORKER,
        "verdict": "approve",
        "findings": [],
    }


class FinishedWorkerOpenPrTest(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        self.tmp = tempfile.TemporaryDirectory(prefix="korapy-open-pr-")
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "board.sqlite")
        self.lock = os.path.join(self.tmp.name, "drive.lock")
        self.sessions = os.path.join(self.tmp.name, "sessions")
        os.environ["KORAPY_SESSIONS"] = self.sessions
        os.environ["KORAPY_STATE"] = os.path.join(self.tmp.name, "state")
        os.environ["KORAPY_CONFIG"] = os.path.join(self.tmp.name, "config")

    def run_open_pr(self, store, fake, launcher, **fields):
        out, err = io.StringIO(), io.StringIO()
        values = {
            "plan": False,
            "found": [basic_project()],
            "fake_tracker": fake,
            "index_for": drive.pulls_index_for(lambda repo: fake),
            "launcher": launcher,
            "stale_after": 10**12,
        }
        values.update(fields)
        code = drive_pass(store, self.lock, out, err, **values)
        return code, out.getvalue(), err.getvalue()

    def test_finished_worker_with_open_pr_gets_a_review_row_and_no_start_row(self):
        store = stored(self.db, [(REPO, 7, "open")])
        self.addCleanup(store.close)
        store.claim(REPO, 7, WORKER, "host-1", now=NOW - 10.0)
        launcher = FakeLauncher()
        code, out, _ = self.run_open_pr(
            store,
            open_pr_tracker(),
            launcher,
            finished_for={(REPO, 7): (WORKER, "worker", "ok")},
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(launcher.calls), 1)
        project, repo, number, kind, _agent, _title, author = launcher.calls[0]
        self.assertEqual((project, repo, number, kind, author), ("blue", REPO, 7, "review", WORKER))
        self.assertIn("reviewer takes over", out)
        self.assertNotIn("fresh start", out)
        self.assertIn("pulls calls 1", out)

    def test_released_claim_with_open_pr_is_refused_never_a_fresh_start(self):
        store = stored(self.db, [(REPO, 7, "open")])
        self.addCleanup(store.close)
        launcher = FakeLauncher()
        code, out, _ = self.run_open_pr(
            store,
            open_pr_tracker(),
            launcher,
            finished_for={(REPO, 7): (WORKER, "worker", "ok")},
        )
        self.assertEqual(code, 0)
        self.assertEqual(launcher.calls, [])
        self.assertIn("pull #9 is open, not a fresh start", out)
        self.assertNotIn("unclaimed, fresh start", out)
        self.assertIn("pulls calls 1", out)

    def test_approving_record_with_green_checks_reaches_the_merge(self):
        store = stored(self.db, [(REPO, 7, "open")])
        self.addCleanup(store.close)
        reviewrecord.write_record(
            "app",
            7,
            HEAD,
            reviewer=REVIEWER,
            author=WORKER,
            verdict="approve",
            findings=[],
            sessions_root=self.sessions,
        )
        record = approving_record()
        fake = open_pr_tracker()
        forge = FakeForge(
            pulls=[
                make_pr(
                    repo=REPO,
                    number=9,
                    base_sha="a" * 40,
                    head_sha=HEAD,
                    body="Closes #7",
                )
            ],
            files={(REPO, 9): ("tests/test_x.py",)},
        )
        launcher = FakeLauncher()
        code, out, _ = self.run_open_pr(
            store,
            fake,
            launcher,
            finished_for={(REPO, 7): (REVIEWER, "reviewer", "ok")},
            review_reader=lambda repo, number: dict(record),
            merge_run=lambda candidate: drivehub.merge_candidate_run(candidate, forge),
            merge_auto=True,
        )
        self.assertEqual(code, 0)
        self.assertEqual(launcher.calls, [])
        self.assertEqual(forge.calls, [("merge", REPO, 9, HEAD, "squash")])
        self.assertIn("pulls calls 1", out)


if __name__ == "__main__":
    unittest.main()
