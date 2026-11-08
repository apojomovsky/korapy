"""Tests for `korapy decision record`: file, ticket comment, label, logbook.

Offline: every tracker effect goes through the in-memory ``FakeTracker``,
the ops root and the coordinator root are temp dirs, and the clock is
fixed. No subprocess, no network, no state dir.
"""

import argparse
import datetime
import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from korapy import coord, decision, decisioncli, tracker
from tests.test_tracker import FakeTracker, reading

T0 = datetime.datetime(2026, 11, 8, 12, 0, 0, tzinfo=datetime.timezone.utc)
REPO = "demo"
TRACKER_REPO = "acme/demo"
NUMBER = 7
DECISION = "Adopt the hub claim service"
WHY = "Peer-to-peer acks lost claims under partition"


def fake_tracker():
    item = reading(
        repo=TRACKER_REPO,
        number=NUMBER,
        labels=("needs-decision",),
        updated_at=T0.timestamp(),
    )
    fake = FakeTracker([item], labels=[(TRACKER_REPO, ("needs-decision",))])
    return fake


class SlugifyTest(unittest.TestCase):
    def test_words_join_with_hyphens(self):
        self.assertEqual(
            decision.slugify("Adopt the hub claim service"), "adopt-the-hub-claim-service"
        )

    def test_punctuation_and_case_fold(self):
        self.assertEqual(decision.slugify("Hub: claims, take two!"), "hub-claims-take-two")

    def test_empty_text_has_no_slug(self):
        self.assertEqual(decision.slugify("..."), "")


class RenderTest(unittest.TestCase):
    def test_required_fields_always_present(self):
        text = decision.render_markdown(
            repo=REPO,
            number=NUMBER,
            date="2026-11-08",
            decision=DECISION,
            why=WHY,
            reversible=None,
            confidence=None,
            follow_ups=(),
            settles=None,
            still_open=None,
            reopen_when=None,
        )
        for field in (
            f"Decision: {DECISION}",
            f"Why: {WHY}",
            "Reversible: unknown",
            "Confidence: unknown",
            "Follow-ups: none",
            f"Ticket: {REPO}#{NUMBER}",
        ):
            self.assertIn(field, text)

    def test_optional_sections_appear_only_when_given(self):
        text = decision.render_markdown(
            repo=REPO,
            number=NUMBER,
            date="2026-11-08",
            decision=DECISION,
            why=WHY,
            reversible="yes",
            confidence="high",
            follow_ups=(8,),
            settles="The claim path",
            still_open="The retry budget",
            reopen_when="A partition loses a claim",
        )
        for field in (
            "Reversible: yes",
            "Confidence: high",
            f"Follow-ups: {REPO}#8",
            "Settles for now: The claim path",
            "Still open: The retry budget",
            "Reopen when: A partition loses a claim",
        ):
            self.assertIn(field, text)


class RecordTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ops = os.path.join(tmp.name, "ops")
        self.coords = os.path.join(tmp.name, "coords")
        os.makedirs(self.ops)
        self.fake = fake_tracker()

    def record(self, fake=None, **over):
        params = {
            "repo": REPO,
            "tracker_repo": TRACKER_REPO,
            "number": NUMBER,
            "decision": DECISION,
            "why": WHY,
            "tracker": fake if fake is not None else self.fake,
            "ops_root": self.ops,
            "coord_root": self.coords,
            "now": T0,
        }
        params.update(over)
        return decision.record(**params)

    def test_record_writes_file_comments_removes_label_and_logs(self):
        result = self.record()
        self.assertFalse(result.already)
        self.assertEqual(
            result.path,
            os.path.join(self.ops, "decisions", "2026-11-08-adopt-the-hub-claim-service.md"),
        )
        with open(result.path, encoding="utf-8") as handle:
            body = handle.read()
        self.assertIn(f"Decision: {DECISION}", body)
        kinds = [call[0] for call in self.fake.calls]
        self.assertEqual(kinds, ["item", "remove_label", "comment"])
        self.assertEqual(
            self.fake.calls[1], ("remove_label", TRACKER_REPO, NUMBER, "needs-decision")
        )
        comment = self.fake.calls[2][3]
        self.assertIn(f"Decision on {REPO}#{NUMBER}: {DECISION}", comment)
        self.assertIn("decisions/2026-11-08-adopt-the-hub-claim-service.md", comment)
        self.assertNotIn(self.ops, comment)
        entries = coord.read_entries(root=self.coords)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["kind"], "decision")
        self.assertIn(f"{REPO}#{NUMBER}", entries[0]["refs"])
        self.assertIn(DECISION, entries[0]["text"])

    def test_retry_after_success_writes_nothing(self):
        first = self.record()
        calls = list(self.fake.calls)
        second = self.record()
        self.assertTrue(second.already)
        self.assertEqual(second.path, first.path)
        self.assertEqual(self.fake.calls, calls)
        self.assertEqual(len(coord.read_entries(root=self.coords)), 1)

    def test_conflicting_slug_is_a_usage_error(self):
        self.record()
        with self.assertRaises(decision.UsageError):
            self.record(number=9)

    def test_halfway_tracker_failure_writes_no_file_or_log(self):
        self.fake.comment_errors[(TRACKER_REPO, NUMBER)] = tracker.TrackerError("down")
        with self.assertRaises(tracker.TrackerError):
            self.record()
        self.assertEqual(
            os.listdir(os.path.join(self.ops, "decisions"))
            if os.path.isdir(os.path.join(self.ops, "decisions"))
            else [],
            [],
        )
        self.assertEqual(coord.read_entries(root=self.coords), [])

    def test_retry_after_halfway_failure_comments_once(self):
        self.fake.comment_errors[(TRACKER_REPO, NUMBER)] = tracker.TrackerError("down")
        with self.assertRaises(tracker.TrackerError):
            self.record()
        del self.fake.comment_errors[(TRACKER_REPO, NUMBER)]
        self.fake.calls.clear()
        result = self.record()
        self.assertFalse(result.already)
        self.assertEqual([call[0] for call in self.fake.calls], ["item", "remove_label", "comment"])

    def test_missing_decision_is_a_usage_error(self):
        with self.assertRaises(decision.UsageError):
            self.record(decision="  ")


class RemoveLabelTest(unittest.TestCase):
    def setUp(self):
        from korapy.github import tracker as ghtracker

        self.calls = []
        self.error = None

        def run(args):
            self.calls.append(list(args))
            if self.error is not None:
                raise self.error
            return ""

        self.adapter = ghtracker.GithubTracker(run)

    def test_remove_posts_delete(self):
        self.adapter.remove_label(TRACKER_REPO, NUMBER, "needs-decision")
        self.assertEqual(len(self.calls), 1)
        posted = self.calls[0]
        self.assertIn("DELETE", posted)
        self.assertIn(f"repos/{TRACKER_REPO}/issues/{NUMBER}/labels/needs-decision", posted)

    def test_remove_of_absent_label_succeeds(self):
        self.error = RuntimeError("gh: Not Found (HTTP 404)")
        self.adapter.remove_label(TRACKER_REPO, NUMBER, "needs-decision")
        self.assertEqual(len(self.calls), 1)

    def test_remove_of_other_labels_is_refused(self):
        with self.assertRaises(ValueError):
            self.adapter.remove_label(TRACKER_REPO, NUMBER, "hold")
        self.assertEqual(self.calls, [])

    def test_remove_failure_is_a_tracker_error(self):
        self.error = RuntimeError("connection refused")
        with self.assertRaises(tracker.TrackerError):
            self.adapter.remove_label(TRACKER_REPO, NUMBER, "needs-decision")


class CliTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        with open(os.path.join(self.root, "korapy.toml"), "w", encoding="utf-8") as handle:
            handle.write('[[repo]]\nname = "demo"\ntracker.repo = "acme/demo"\n')
        self.coords = os.path.join(tmp.name, "coords")
        self.fake = fake_tracker()
        here = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, here)

    def args(self, **over):
        base = {
            "repo": REPO,
            "number": NUMBER,
            "decision": DECISION,
            "why": WHY,
            "reversible": None,
            "confidence": None,
            "settles": None,
            "still_open": None,
            "reopen_when": None,
            "follow_up": [],
            "project": None,
        }
        base.update(over)
        return argparse.Namespace(**base)

    def run_record(self, args):
        from korapy import trackers

        entry = trackers.RepoTracker(
            name=REPO,
            tracker_repo=TRACKER_REPO,
            operators=[],
            variant="labels",
            tracker=self.fake,
        )
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = decisioncli.cmd_record(
                args,
                tracker_factory=lambda root, names, git: ({REPO: entry}, None),
                coord_root=self.coords,
                now=T0,
            )
        return code, out.getvalue(), err.getvalue()

    def test_record_exits_zero_and_names_file(self):
        code, out, _ = self.run_record(self.args())
        self.assertEqual(code, 0)
        self.assertIn("2026-11-08-adopt-the-hub-claim-service.md", out)

    def test_missing_why_is_usage(self):
        code, _, err = self.run_record(self.args(why=""))
        self.assertEqual(code, 4)
        self.assertIn("why", err)

    def test_tracker_outage_is_unreachable(self):
        self.fake.item_errors[(TRACKER_REPO, NUMBER)] = tracker.TrackerError("down")
        code, _, _ = self.run_record(self.args())
        self.assertEqual(code, 3)

    def test_unknown_repo_is_usage(self):
        code, _, err = self.run_record(self.args(repo="nope"))
        self.assertEqual(code, 4)
        self.assertIn("nope", err)


if __name__ == "__main__":
    unittest.main()
