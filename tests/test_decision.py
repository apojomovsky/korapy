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
from unittest import mock

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
        self.assertEqual(kinds, ["item", "comments", "comment", "remove_label"])
        self.assertEqual(
            self.fake.calls[3], ("remove_label", TRACKER_REPO, NUMBER, "needs-decision")
        )
        comment = [call[3] for call in self.fake.calls if call[0] == "comment"][0]
        self.assertIn(f"Decision on {REPO}#{NUMBER}: {DECISION}", comment)
        self.assertIn("decisions/2026-11-08-adopt-the-hub-claim-service.md", comment)
        self.assertNotIn(self.ops, comment)
        entries = coord.read_entries(root=self.coords)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["kind"], "decision")
        self.assertIn(f"{REPO}#{NUMBER}", entries[0]["refs"])
        self.assertIn(DECISION, entries[0]["text"])

    def test_retry_after_success_replays_reads_but_writes_nothing(self):
        first = self.record()
        calls = list(self.fake.calls)
        second = self.record()
        self.assertTrue(second.already)
        self.assertEqual(second.path, first.path)
        replayed = [call[0] for call in self.fake.calls[len(calls) :]]
        self.assertEqual(replayed, ["item", "comments", "remove_label"])
        posted = [body for bodies in self.fake.posted_comments.values() for body in bodies]
        self.assertEqual(len(posted), 1)
        self.assertEqual(len(coord.read_entries(root=self.coords)), 1)

    def test_conflicting_slug_is_a_usage_error(self):
        self.record()
        self.fake.add(reading(repo=TRACKER_REPO, number=9, updated_at=T0.timestamp()))
        with self.assertRaises(decision.UsageError):
            self.record(number=9)

    def test_tracker_failure_keeps_file_but_writes_no_comment_or_log(self):
        self.fake.comment_errors[(TRACKER_REPO, NUMBER)] = tracker.TrackerError("down")
        with self.assertRaises(tracker.TrackerError):
            self.record()
        names = (
            os.listdir(os.path.join(self.ops, "decisions"))
            if os.path.isdir(os.path.join(self.ops, "decisions"))
            else []
        )
        self.assertEqual(names, ["2026-11-08-adopt-the-hub-claim-service.md"])
        with open(os.path.join(self.ops, "decisions", names[0]), encoding="utf-8") as handle:
            self.assertIn(f"Decision: {DECISION}", handle.read())
        posted = [body for bodies in self.fake.posted_comments.values() for body in bodies]
        self.assertEqual(posted, [])
        self.assertEqual(coord.read_entries(root=self.coords), [])

    def test_retry_after_halfway_failure_comments_once(self):
        self.fake.comment_errors[(TRACKER_REPO, NUMBER)] = tracker.TrackerError("down")
        with self.assertRaises(tracker.TrackerError):
            self.record()
        del self.fake.comment_errors[(TRACKER_REPO, NUMBER)]
        result = self.record()
        self.assertTrue(result.already)
        self.assertEqual(
            result.path,
            os.path.join(self.ops, "decisions", "2026-11-08-adopt-the-hub-claim-service.md"),
        )
        attempts = [call for call in self.fake.calls if call[0] == "comment"]
        self.assertEqual(len(attempts), 2)
        posted = [body for bodies in self.fake.posted_comments.values() for body in bodies]
        self.assertEqual(len(posted), 1)
        self.assertIn(f"Decision on {REPO}#{NUMBER}: {DECISION}", posted[0])
        self.assertEqual(len(coord.read_entries(root=self.coords)), 1)

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


class ExactSlugTest(unittest.TestCase):
    """`find_recorded` matches the slug exactly, not by suffix."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ops = os.path.join(tmp.name, "ops")
        os.makedirs(os.path.join(self.ops, "decisions"))
        with open(
            os.path.join(self.ops, "decisions", "2026-11-08-hub-claim-service.md"),
            "w",
            encoding="utf-8",
        ) as handle:
            handle.write("recorded\n")

    def test_longer_slug_with_same_suffix_does_not_match(self):
        self.assertIsNone(decision.find_recorded(self.ops, "claim-service"))

    def test_exact_slug_still_matches(self):
        self.assertEqual(
            decision.find_recorded(self.ops, "hub-claim-service"),
            os.path.join(self.ops, "decisions", "2026-11-08-hub-claim-service.md"),
        )

    def test_retry_past_midnight_finds_the_first_write(self):
        with open(
            os.path.join(self.ops, "decisions", "2026-11-07-hub-claim-service.md"),
            "w",
            encoding="utf-8",
        ) as handle:
            handle.write("older\n")
        self.assertEqual(
            decision.find_recorded(self.ops, "hub-claim-service"),
            os.path.join(self.ops, "decisions", "2026-11-07-hub-claim-service.md"),
        )


class ExactTicketLineTest(unittest.TestCase):
    """The idempotency check compares full lines, not substrings."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ops = os.path.join(tmp.name, "ops")
        self.coords = os.path.join(tmp.name, "coords")
        os.makedirs(self.ops)
        self.fake = fake_tracker()

    def test_prefix_ticket_number_is_a_conflict_not_already(self):
        slug = decision.slugify(DECISION)
        os.makedirs(os.path.join(self.ops, "decisions"))
        with open(
            os.path.join(self.ops, "decisions", f"2026-11-08-{slug}.md"),
            "w",
            encoding="utf-8",
        ) as handle:
            handle.write(
                decision.render_markdown(
                    repo=REPO,
                    number=70,
                    date="2026-11-08",
                    decision=DECISION,
                    why=WHY,
                )
            )
        with self.assertRaises(decision.UsageError):
            decision.record(
                repo=REPO,
                tracker_repo=TRACKER_REPO,
                number=NUMBER,
                decision=DECISION,
                why=WHY,
                tracker=self.fake,
                ops_root=self.ops,
                coord_root=self.coords,
                now=T0,
            )


class FileFirstOrderingTest(unittest.TestCase):
    """The file lands before any tracker write, so a local failure posts nothing."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ops = os.path.join(tmp.name, "ops")
        self.coords = os.path.join(tmp.name, "coords")
        os.makedirs(self.ops)
        self.fake = fake_tracker()

    def test_write_failure_posts_no_comment(self):
        with mock.patch.object(decision, "_write_file", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                decision.record(
                    repo=REPO,
                    tracker_repo=TRACKER_REPO,
                    number=NUMBER,
                    decision=DECISION,
                    why=WHY,
                    tracker=self.fake,
                    ops_root=self.ops,
                    coord_root=self.coords,
                    now=T0,
                )
        kinds = [call[0] for call in self.fake.calls]
        self.assertEqual(kinds, ["item"])

    def test_retry_after_write_failure_comments_once(self):
        real_write = decision._write_file
        state = {"fail": True}

        def flaky(path, text):
            if state["fail"]:
                state["fail"] = False
                raise OSError("disk full")
            return real_write(path, text)

        with mock.patch.object(decision, "_write_file", side_effect=flaky):
            with self.assertRaises(OSError):
                decision.record(
                    repo=REPO,
                    tracker_repo=TRACKER_REPO,
                    number=NUMBER,
                    decision=DECISION,
                    why=WHY,
                    tracker=self.fake,
                    ops_root=self.ops,
                    coord_root=self.coords,
                    now=T0,
                )
            result = decision.record(
                repo=REPO,
                tracker_repo=TRACKER_REPO,
                number=NUMBER,
                decision=DECISION,
                why=WHY,
                tracker=self.fake,
                ops_root=self.ops,
                coord_root=self.coords,
                now=T0,
            )
        bodies = [body for bodies in self.fake.posted_comments.values() for body in bodies]
        self.assertEqual(len(bodies), 1)
        self.assertIn(f"Decision on {REPO}#{NUMBER}: {DECISION}", bodies[0])
        self.assertFalse(result.already)
        self.assertEqual(len(coord.read_entries(root=self.coords)), 1)


class RetryConvergenceTest(unittest.TestCase):
    """A retry replays only what is missing: never a second comment or file."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ops = os.path.join(tmp.name, "ops")
        self.coords = os.path.join(tmp.name, "coords")
        os.makedirs(self.ops)
        self.fake = fake_tracker()

    def record(self, fake=None, coords=None, now=T0, **over):
        params = {
            "repo": REPO,
            "tracker_repo": TRACKER_REPO,
            "number": NUMBER,
            "decision": DECISION,
            "why": WHY,
            "tracker": fake if fake is not None else self.fake,
            "ops_root": self.ops,
            "coord_root": coords if coords is not None else self.coords,
            "now": now,
        }
        params.update(over)
        return decision.record(**params)

    def test_retry_past_midnight_reuses_first_file_and_comments_once(self):
        first = self.record()
        fresh = fake_tracker()
        coords2 = os.path.join(os.path.dirname(self.coords), "coords2")
        second = self.record(
            fake=fresh,
            coords=coords2,
            now=T0 + datetime.timedelta(days=1),
        )
        self.assertTrue(second.already)
        self.assertEqual(second.path, first.path)
        bodies = [body for bodies in fresh.posted_comments.values() for body in bodies]
        self.assertEqual(len(bodies), 1)
        self.assertIn(os.path.basename(first.path), bodies[0])
        self.assertNotIn("2026-11-09", bodies[0])

    def test_retry_repairs_missing_comment_without_second_file(self):
        first = self.record()
        fresh = fake_tracker()
        second = self.record(fake=fresh)
        self.assertTrue(second.already)
        self.assertEqual(second.path, first.path)
        bodies = [body for bodies in fresh.posted_comments.values() for body in bodies]
        self.assertEqual(len(bodies), 1)
        kinds = [call[0] for call in fresh.calls]
        self.assertIn("remove_label", kinds)
        self.assertEqual(len(coord.read_entries(root=self.coords)), 1)

    def test_retry_repairs_missing_logbook_entry_without_second_comment(self):
        first = self.record()
        self.assertFalse(first.already)
        os.remove(coord.journal_path(self.coords))
        second = self.record()
        self.assertTrue(second.already)
        self.assertEqual(second.path, first.path)
        bodies = [body for bodies in self.fake.posted_comments.values() for body in bodies]
        self.assertEqual(len(bodies), 1)
        entries = coord.read_entries(root=self.coords)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["kind"], "decision")


class DirnameConstantTest(unittest.TestCase):
    """The relative display path comes from `DECISIONS_DIRNAME`, not a literal."""

    def test_comment_and_logbook_use_dirname_constant(self):
        with mock.patch.object(decision, "DECISIONS_DIRNAME", "verdicts"):
            body = decision.comment_body(
                repo=REPO,
                number=NUMBER,
                decision=DECISION,
                why=WHY,
                filename="2026-11-08-adopt-the-hub-claim-service.md",
            )
        self.assertIn("verdicts/2026-11-08-adopt-the-hub-claim-service.md", body)
        self.assertNotIn("decisions/", body)

    def test_record_logbook_text_uses_dirname_constant(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ops = os.path.join(tmp.name, "ops")
        coords = os.path.join(tmp.name, "coords")
        os.makedirs(ops)
        fake = fake_tracker()
        with mock.patch.object(decision, "DECISIONS_DIRNAME", "verdicts"):
            result = decision.record(
                repo=REPO,
                tracker_repo=TRACKER_REPO,
                number=NUMBER,
                decision=DECISION,
                why=WHY,
                tracker=fake,
                ops_root=ops,
                coord_root=coords,
                now=T0,
            )
        self.assertTrue(os.path.dirname(result.path).endswith("verdicts"))
        entries = coord.read_entries(root=coords)
        self.assertEqual(len(entries), 1)
        self.assertIn("verdicts/", entries[0]["text"])
        self.assertNotIn("decisions/", entries[0]["text"])


class HolderFallbackTest(unittest.TestCase):
    """Only a missing lease falls back to manual; real errors surface."""

    def test_os_error_falls_back_to_manual(self):
        class Bad:
            @staticmethod
            def describe(root=None):
                raise OSError("no lease")

        with mock.patch.dict(os.environ):
            os.environ.pop(decisioncli.AGENT_ID_ENV, None)
            self.assertEqual(decisioncli._holder_and_generation(Bad(), "/tmp/x"), ("manual", 0))

    def test_value_error_falls_back_to_manual(self):
        class Bad:
            @staticmethod
            def describe(root=None):
                raise ValueError("bad lease")

        with mock.patch.dict(os.environ):
            os.environ.pop(decisioncli.AGENT_ID_ENV, None)
            self.assertEqual(decisioncli._holder_and_generation(Bad(), "/tmp/x"), ("manual", 0))

    def test_unexpected_error_propagates(self):
        class Boom:
            @staticmethod
            def describe(root=None):
                raise RuntimeError("lease lock poisoned")

        with self.assertRaises(RuntimeError):
            decisioncli._holder_and_generation(Boom(), "/tmp/x")


class PrefixDecisionTest(unittest.TestCase):
    """A shorter decision is never "already present" inside a longer one."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ops = os.path.join(tmp.name, "ops")
        self.coords = os.path.join(tmp.name, "coords")
        os.makedirs(self.ops)
        self.fake = fake_tracker()

    def record(self, **over):
        params = {
            "repo": REPO,
            "tracker_repo": TRACKER_REPO,
            "number": NUMBER,
            "why": WHY,
            "tracker": self.fake,
            "ops_root": self.ops,
            "coord_root": self.coords,
            "now": T0,
        }
        params.update(over)
        return decision.record(**params)

    def test_prefix_decision_posts_its_own_comment_and_log_entry(self):
        first = self.record(decision="Ship it")
        second = self.record(decision="Ship")
        self.assertFalse(first.already)
        self.assertFalse(second.already)
        self.assertNotEqual(first.path, second.path)
        bodies = [body for bodies in self.fake.posted_comments.values() for body in bodies]
        self.assertEqual(len(bodies), 2)
        entries = coord.read_entries(root=self.coords)
        self.assertEqual(len(entries), 2)


class PublishOrderTest(unittest.TestCase):
    """The comment posts before the label leaves, so a failed post keeps it."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ops = os.path.join(tmp.name, "ops")
        self.coords = os.path.join(tmp.name, "coords")
        os.makedirs(self.ops)
        self.fake = fake_tracker()

    def test_comment_posts_before_label_removal(self):
        decision.record(
            repo=REPO,
            tracker_repo=TRACKER_REPO,
            number=NUMBER,
            decision=DECISION,
            why=WHY,
            tracker=self.fake,
            ops_root=self.ops,
            coord_root=self.coords,
            now=T0,
        )
        kinds = [call[0] for call in self.fake.calls]
        self.assertEqual(kinds, ["item", "comments", "comment", "remove_label"])


class UnknownTicketTest(unittest.TestCase):
    """A read-only probe runs before the file write: no orphan records."""

    def test_unknown_ticket_writes_no_file(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ops = os.path.join(tmp.name, "ops")
        coords = os.path.join(tmp.name, "coords")
        os.makedirs(ops)
        fake = FakeTracker([], labels=[(TRACKER_REPO, ("needs-decision",))])
        with self.assertRaises(tracker.TrackerError):
            decision.record(
                repo=REPO,
                tracker_repo=TRACKER_REPO,
                number=NUMBER,
                decision=DECISION,
                why=WHY,
                tracker=fake,
                ops_root=ops,
                coord_root=coords,
                now=T0,
            )
        base = os.path.join(ops, "decisions")
        names = os.listdir(base) if os.path.isdir(base) else []
        self.assertEqual(names, [])


class NewlineRejectionTest(unittest.TestCase):
    """Free-text flags stay one line so a retry can match whole lines."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ops = os.path.join(tmp.name, "ops")
        self.coords = os.path.join(tmp.name, "coords")
        os.makedirs(self.ops)
        self.fake = fake_tracker()

    def record(self, **over):
        params = {
            "repo": REPO,
            "tracker_repo": TRACKER_REPO,
            "number": NUMBER,
            "decision": DECISION,
            "why": WHY,
            "tracker": self.fake,
            "ops_root": self.ops,
            "coord_root": self.coords,
            "now": T0,
        }
        params.update(over)
        return decision.record(**params)

    def test_newline_decision_is_a_usage_error_naming_the_flag(self):
        with self.assertRaises(decision.UsageError) as ctx:
            self.record(decision="Ship\nit")
        self.assertIn("--decision", str(ctx.exception))

    def test_carriage_return_decision_is_a_usage_error(self):
        with self.assertRaises(decision.UsageError) as ctx:
            self.record(decision="Ship\rit")
        self.assertIn("--decision", str(ctx.exception))

    def test_newline_why_is_a_usage_error_naming_the_flag(self):
        with self.assertRaises(decision.UsageError) as ctx:
            self.record(why="Because\nreasons")
        self.assertIn("--why", str(ctx.exception))

    def test_newline_settles_is_a_usage_error(self):
        with self.assertRaises(decision.UsageError) as ctx:
            self.record(settles="The path\nfor now")
        self.assertIn("--settles", str(ctx.exception))

    def test_newline_still_open_is_a_usage_error(self):
        with self.assertRaises(decision.UsageError) as ctx:
            self.record(still_open="The budget\nremains")
        self.assertIn("--open", str(ctx.exception))

    def test_newline_reopen_when_is_a_usage_error(self):
        with self.assertRaises(decision.UsageError) as ctx:
            self.record(reopen_when="A claim\nis lost")
        self.assertIn("--reopen-when", str(ctx.exception))

    def test_newline_decision_writes_no_file_and_posts_nothing(self):
        with self.assertRaises(decision.UsageError):
            self.record(decision="Ship\nit")
        base = os.path.join(self.ops, "decisions")
        names = os.listdir(base) if os.path.isdir(base) else []
        self.assertEqual(names, [])
        posted = [body for bodies in self.fake.posted_comments.values() for body in bodies]
        self.assertEqual(posted, [])

    def test_cli_maps_newline_decision_to_exit_4(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = tmp.name
        with open(os.path.join(root, "korapy.toml"), "w", encoding="utf-8") as handle:
            handle.write('[[repo]]\nname = "demo"\ntracker.repo = "acme/demo"\n')
        coords = os.path.join(root, "coords")
        here = os.getcwd()
        os.chdir(root)
        self.addCleanup(os.chdir, here)
        from korapy import trackers

        entry = trackers.RepoTracker(
            name=REPO,
            tracker_repo=TRACKER_REPO,
            operators=[],
            variant="labels",
            tracker=self.fake,
        )
        args = argparse.Namespace(
            repo=REPO,
            number=NUMBER,
            decision="Ship\nit",
            why=WHY,
            reversible=None,
            confidence=None,
            settles=None,
            still_open=None,
            reopen_when=None,
            follow_up=[],
            project=None,
        )
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = decisioncli.cmd_record(
                args,
                tracker_factory=lambda root, names, git: ({REPO: entry}, None),
                coord_root=coords,
                now=T0,
            )
        self.assertEqual(code, 4)
        self.assertIn("--decision", err.getvalue())


class RetryNoopTest(unittest.TestCase):
    """A retry of a valid record changes nothing: same path, once each."""

    def test_retry_of_valid_record_is_a_noop(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ops = os.path.join(tmp.name, "ops")
        coords = os.path.join(tmp.name, "coords")
        os.makedirs(ops)
        fake = fake_tracker()
        first = decision.record(
            repo=REPO,
            tracker_repo=TRACKER_REPO,
            number=NUMBER,
            decision=DECISION,
            why=WHY,
            tracker=fake,
            ops_root=ops,
            coord_root=coords,
            now=T0,
        )
        self.assertFalse(first.already)
        second = decision.record(
            repo=REPO,
            tracker_repo=TRACKER_REPO,
            number=NUMBER,
            decision=DECISION,
            why=WHY,
            tracker=fake,
            ops_root=ops,
            coord_root=coords,
            now=T0,
        )
        self.assertTrue(second.already)
        self.assertEqual(second.path, first.path)
        bodies = [body for bodies in fake.posted_comments.values() for body in bodies]
        self.assertEqual(len(bodies), 1)
        self.assertIn(f"Decision on {REPO}#{NUMBER}: {DECISION}", bodies[0].splitlines())
        entries = coord.read_entries(root=coords)
        self.assertEqual(len(entries), 1)


if __name__ == "__main__":
    unittest.main()
