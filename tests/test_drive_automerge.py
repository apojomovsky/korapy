"""The drive's auto-merge step: one ready PR per pass, or none.

A pass whose rows carry an open linked pull and an approving review
record attempts one serialized merge through the same path as
`korapy merge`, then logs the merge or the blocking criteria. The
step reads no forge data to select its candidate, so a pass with no
candidate costs zero GitHub calls; `merge.auto = false` (or no
forge seam) disables the step the same way. Fakes only: a temporary
board store, stub policies, an in-memory forge, review records on
disk under a temporary sessions root. No `gh`, no systemd, no
network, no real state dir.
"""

import io
import os
import tempfile
import unittest
from unittest import mock

from korapy import drive, drivehub, reviewrecord
from korapy.eligibility import LinkedPull, TicketRow
from tests.test_drive import stored
from tests.test_drive_claims import FakeLauncher, drive_pass
from tests.test_forge import MERGE_SHA, FakeForge, make_pr
from tests.test_tracker import FakeTracker, reading

NOW = 2000.0
HOST = "host-1"
REPO = "acme/app"
SHORT = "app"
HEAD = "b" * 40
OTHER = "c" * 40


def record(number=7, head=HEAD, verdict="approve", findings=()):
    return {
        "repo": SHORT,
        "number": number,
        "commit": head,
        "reviewer": "reviewer-1",
        "author": "worker-1",
        "verdict": verdict,
        "findings": list(findings),
    }


def row(number=7, pulls=(LinkedPull(number=9, state="open"),), state="open"):
    return TicketRow(repo=REPO, number=number, state=state, pulls=pulls)


class MergeCandidateTest(unittest.TestCase):
    def candidate(self, rows, reviews, project="blue"):
        projects_for = {key: project for key in rows}
        return drive.merge_candidate(rows, projects_for, reviews)

    def test_no_rows_is_no_candidate(self):
        self.assertIsNone(self.candidate({}, {(REPO, 7): record()}))

    def test_row_without_pulls_is_no_candidate(self):
        self.assertIsNone(self.candidate({(REPO, 7): row(pulls=())}, {(REPO, 7): record()}))

    def test_unreadable_pulls_are_no_candidate(self):
        self.assertIsNone(self.candidate({(REPO, 7): row(pulls=None)}, {(REPO, 7): record()}))

    def test_closed_pull_is_no_candidate(self):
        pulls = (LinkedPull(number=9, state="closed"),)
        self.assertIsNone(self.candidate({(REPO, 7): row(pulls=pulls)}, {(REPO, 7): record()}))

    def test_two_open_pulls_are_no_candidate(self):
        pulls = (LinkedPull(number=9, state="open"), LinkedPull(number=10, state="open"))
        self.assertIsNone(self.candidate({(REPO, 7): row(pulls=pulls)}, {(REPO, 7): record()}))

    def test_closed_row_is_no_candidate(self):
        rows = {(REPO, 7): row(state="closed")}
        self.assertIsNone(self.candidate(rows, {(REPO, 7): record()}))

    def test_missing_record_is_no_candidate(self):
        self.assertIsNone(self.candidate({(REPO, 7): row()}, {}))

    def test_block_record_is_no_candidate(self):
        reviews = {(REPO, 7): record(verdict="block")}
        self.assertIsNone(self.candidate({(REPO, 7): row()}, reviews))

    def test_empty_pin_is_no_candidate(self):
        reviews = {(REPO, 7): record(head="")}
        self.assertIsNone(self.candidate({(REPO, 7): row()}, reviews))

    def test_short_pin_passes_through_to_the_merge(self):
        reviews = {(REPO, 7): record(head="short")}
        candidate = self.candidate({(REPO, 7): row()}, reviews)
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate.pin, "short")

    def test_row_without_a_project_is_no_candidate(self):
        rows = {(REPO, 7): row()}
        reviews = {(REPO, 7): record()}
        self.assertIsNone(drive.merge_candidate(rows, {}, reviews))

    def test_ready_row_is_the_candidate(self):
        candidate = self.candidate({(REPO, 7): row()}, {(REPO, 7): record()})
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(
            (candidate.ticket_repo, candidate.number, candidate.forge_repo, candidate.pr_number),
            (SHORT, 7, REPO, 9),
        )
        self.assertEqual(candidate.pin, HEAD)

    def test_first_sorted_key_wins(self):
        rows = {(REPO, 8): row(number=8), (REPO, 7): row()}
        reviews = {(REPO, 8): record(number=8), (REPO, 7): record()}
        candidate = self.candidate(rows, reviews)
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual((candidate.number, candidate.pr_number), (7, 9))


class AutoMergePassTest(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        tmp = tempfile.TemporaryDirectory(prefix="korapy-automerge-")
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        self.sessions = os.path.join(self.tmp, "sessions")
        os.environ["KORAPY_SESSIONS"] = self.sessions
        os.environ["KORAPY_STATE"] = os.path.join(self.tmp, "state")
        os.environ["KORAPY_CONFIG"] = os.path.join(self.tmp, "config")
        self.db = os.path.join(self.tmp, "board.sqlite")
        self.lock = os.path.join(self.tmp, "drive.lock")
        self.policies = os.path.join(self.tmp, "policies")
        os.makedirs(self.policies)

    def write_record(self, number=7, head=HEAD, verdict="approve", findings=()):
        reviewrecord.write_record(
            SHORT,
            number,
            head,
            reviewer="reviewer-1",
            author="worker-1",
            verdict=verdict,
            findings=list(findings),
            sessions_root=self.sessions,
        )
        return record(number, head, verdict, findings)

    def run_auto(self, store, fake, records, **fields):
        out, err = io.StringIO(), io.StringIO()
        launcher = FakeLauncher()
        tickets = sorted({number for _, number in records} or [7])
        values = {
            "found": [("blue", [(REPO, number, "start") for number in tickets])],
            "policy_dir": self.policies,
            "fake_tracker": FakeTracker(
                [reading(repo=REPO, number=number, updated_at=1900.0) for number in tickets]
            ),
            "launcher": launcher,
            "review_reader": lambda repo, number: dict(records.get((repo, number)) or {}),
            "index_for": lambda repo, budget, *, merged_since=None: (
                {
                    number: (LinkedPull(number=9 if number == 7 else 10, state="open"),)
                    for number in tickets
                },
                len(tickets),
                True,
            ),
            "merge_run": lambda candidate: drivehub.merge_candidate_run(candidate, fake),
            "merge_auto": True,
            "now": NOW,
        }
        values.update(fields)
        code = drive_pass(store, self.lock, out, err, **values)
        return code, out.getvalue(), err.getvalue(), launcher

    def test_merges_a_ready_pr_once(self):
        store = stored(self.db, [(REPO, 7, "open")])
        self.addCleanup(store.close)
        canned = self.write_record()
        fake = FakeForge(
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
        code, out, err, launcher = self.run_auto(store, fake, {(REPO, 7): canned})
        self.assertEqual(code, 0)
        self.assertEqual(launcher.calls, [])
        self.assertEqual(fake.calls, [("merge", REPO, 9, HEAD, "squash")])
        pr = fake.pull_request(REPO, 9)
        self.assertEqual(pr.state, "merged")
        self.assertEqual(pr.merge_sha, MERGE_SHA)
        self.assertIn("korapy drive: merged acme/app#9", err)

    def test_merge_off_makes_no_forge_calls(self):
        store = stored(self.db, [(REPO, 7, "open")])
        self.addCleanup(store.close)
        canned = self.write_record()
        fake = FakeForge(
            pulls=[
                make_pr(repo=REPO, number=9, base_sha="a" * 40, head_sha=HEAD, body="Closes #7")
            ],
            files={(REPO, 9): ("tests/test_x.py",)},
        )
        code, _, err, launcher = self.run_auto(store, fake, {(REPO, 7): canned}, merge_auto=False)
        self.assertEqual(code, 0)
        self.assertEqual(launcher.calls, [])
        self.assertEqual(fake.requested, [])
        self.assertEqual(fake.threads_requested, [])
        self.assertEqual(fake.files_requested, [])
        self.assertEqual(fake.calls, [])
        self.assertNotIn("merged", err)

    def test_no_candidate_makes_no_forge_calls(self):
        store = stored(self.db, [(REPO, 7, "open")])
        self.addCleanup(store.close)
        fake = FakeForge(
            pulls=[
                make_pr(repo=REPO, number=9, base_sha="a" * 40, head_sha=HEAD, body="Closes #7")
            ],
            files={(REPO, 9): ("tests/test_x.py",)},
        )
        code, _, err, launcher = self.run_auto(store, fake, {})
        self.assertEqual(code, 0)
        self.assertEqual(launcher.calls, [])
        self.assertEqual(fake.requested, [])
        self.assertEqual(fake.threads_requested, [])
        self.assertEqual(fake.files_requested, [])
        self.assertNotIn("merged", err)

    def test_no_merge_run_makes_no_merge_lines(self):
        store = stored(self.db, [(REPO, 7, "open")])
        self.addCleanup(store.close)
        canned = self.write_record()
        code, _, err, launcher = self.run_auto(store, None, {(REPO, 7): canned}, merge_run=None)
        self.assertEqual(code, 0)
        self.assertEqual(launcher.calls, [])
        self.assertNotIn("merged", err)

    def test_blocked_merge_logs_the_criterion_and_changes_nothing(self):
        store = stored(self.db, [(REPO, 7, "open")])
        self.addCleanup(store.close)
        canned = self.write_record()
        fake = FakeForge(
            pulls=[
                make_pr(
                    repo=REPO,
                    number=9,
                    base_sha="a" * 40,
                    head_sha=HEAD,
                    body="Closes #7",
                    checks_state="failing",
                )
            ],
            files={(REPO, 9): ("tests/test_x.py",)},
        )
        code, _, err, launcher = self.run_auto(store, fake, {(REPO, 7): canned})
        self.assertEqual(code, 0)
        self.assertEqual(launcher.calls, [])
        self.assertEqual(fake.calls, [])
        self.assertEqual(fake.pull_request(REPO, 9).state, "open")
        self.assertIn("korapy drive: not merged acme/app#9", err)
        self.assertIn("checks", err)

    def test_unreadable_threads_block_without_crashing(self):
        store = stored(self.db, [(REPO, 7, "open")])
        self.addCleanup(store.close)
        canned = self.write_record()
        fake = FakeForge(
            pulls=[
                make_pr(repo=REPO, number=9, base_sha="a" * 40, head_sha=HEAD, body="Closes #7")
            ],
            files={(REPO, 9): ("tests/test_x.py",)},
            fail={"review_threads"},
        )
        code, _, err, launcher = self.run_auto(store, fake, {(REPO, 7): canned})
        self.assertEqual(code, 0)
        self.assertEqual(launcher.calls, [])
        self.assertEqual(fake.calls, [])
        self.assertIn("korapy drive: not merged acme/app#9", err)
        self.assertIn("unreadable", err)

    def test_moved_head_blocks(self):
        store = stored(self.db, [(REPO, 7, "open")])
        self.addCleanup(store.close)
        canned = self.write_record()
        fake = FakeForge(
            pulls=[
                make_pr(repo=REPO, number=9, base_sha="a" * 40, head_sha=OTHER, body="Closes #7")
            ],
            files={(REPO, 9): ("tests/test_x.py",)},
        )
        code, _, err, launcher = self.run_auto(store, fake, {(REPO, 7): canned})
        self.assertEqual(code, 0)
        self.assertEqual(fake.calls, [])
        self.assertIn("korapy drive: not merged acme/app#9", err)
        self.assertIn("no longer the tip", err)

    def test_two_ready_tickets_merge_only_once(self):
        store = stored(self.db, [(REPO, 7, "open"), (REPO, 8, "open")])
        self.addCleanup(store.close)
        first = self.write_record(number=7, head=HEAD)
        second = self.write_record(number=8, head=OTHER)
        fake = FakeForge(
            pulls=[
                make_pr(repo=REPO, number=9, base_sha="a" * 40, head_sha=HEAD, body="Closes #7"),
                make_pr(repo=REPO, number=10, base_sha="a" * 40, head_sha=OTHER, body="Closes #8"),
            ],
            files={(REPO, 9): ("tests/test_x.py",), (REPO, 10): ("tests/test_y.py",)},
        )
        code, _, err, launcher = self.run_auto(store, fake, {(REPO, 7): first, (REPO, 8): second})
        self.assertEqual(code, 0)
        self.assertEqual(launcher.calls, [])
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls, [("merge", REPO, 9, HEAD, "squash")])
        self.assertIn("korapy drive: merged acme/app#9", err)

    def test_project_file_disables_auto_merge(self):
        store = stored(self.db, [(REPO, 7, "open")])
        self.addCleanup(store.close)
        canned = self.write_record()
        root = os.path.join(self.policies, "ops", "blue")
        os.makedirs(root, exist_ok=True)
        with open(os.path.join(root, "korapy.toml"), "w", encoding="utf-8") as handle:
            handle.write("[merge]\nauto = false\n")
        fake = FakeForge(
            pulls=[
                make_pr(repo=REPO, number=9, base_sha="a" * 40, head_sha=HEAD, body="Closes #7")
            ],
            files={(REPO, 9): ("tests/test_x.py",)},
        )
        code, _, err, _ = self.run_auto(store, fake, {(REPO, 7): canned}, merge_auto=None)
        self.assertEqual(code, 0)
        self.assertEqual(fake.requested, [])
        self.assertEqual(fake.calls, [])
        self.assertNotIn("merged", err)

    def test_missing_project_file_merges_by_default(self):
        store = stored(self.db, [(REPO, 7, "open")])
        self.addCleanup(store.close)
        canned = self.write_record()
        fake = FakeForge(
            pulls=[
                make_pr(repo=REPO, number=9, base_sha="a" * 40, head_sha=HEAD, body="Closes #7")
            ],
            files={(REPO, 9): ("tests/test_x.py",)},
        )
        code, _, err, _ = self.run_auto(store, fake, {(REPO, 7): canned}, merge_auto=None)
        self.assertEqual(code, 0)
        self.assertEqual(fake.calls, [("merge", REPO, 9, HEAD, "squash")])
        self.assertIn("korapy drive: merged acme/app#9", err)

    def test_error_outcome_logs_cannot_merge(self):
        store = stored(self.db, [(REPO, 7, "open")])
        self.addCleanup(store.close)
        canned = self.write_record()
        code, _, err, launcher = self.run_auto(
            store,
            FakeForge(pulls=[]),
            {(REPO, 7): canned},
            merge_run=lambda candidate: drive.MergeOutcome(
                merged=False, merge_sha=None, error="forge unreachable: boom"
            ),
        )
        self.assertEqual(code, 0)
        self.assertEqual(launcher.calls, [])
        self.assertIn("korapy drive: cannot merge acme/app#9", err)
        self.assertIn("boom", err)

    def test_plan_makes_no_merge_or_forge_calls(self):
        store = stored(self.db, [(REPO, 7, "open")])
        self.addCleanup(store.close)
        canned = self.write_record()
        fake = FakeForge(
            pulls=[
                make_pr(repo=REPO, number=9, base_sha="a" * 40, head_sha=HEAD, body="Closes #7")
            ],
            files={(REPO, 9): ("tests/test_x.py",)},
        )
        out, err = io.StringIO(), io.StringIO()
        code = drive_pass(
            store,
            self.lock,
            out,
            err,
            plan=True,
            found=[("blue", [(REPO, 7, "start")])],
            policy_dir=self.policies,
            fake_tracker=FakeTracker([reading(repo=REPO, number=7, updated_at=1900.0)]),
            launcher=FakeLauncher(),
            review_reader=lambda repo, number: dict(canned),
            driven={},
            now=NOW,
            merge_run=lambda candidate: drivehub.merge_candidate_run(candidate, fake),
            merge_auto=True,
        )
        self.assertEqual(code, 0)
        self.assertEqual(fake.requested, [])
        self.assertEqual(fake.calls, [])
        self.assertNotIn("merged", err.getvalue())
