"""The loop's review-record reprompt: reviewers that end with prose get one resume.

When a reviewer's unit ends `ok` with no review record, the next pass
resumes that reviewer session once with the exact `korapy review record`
command (the head pinned through the remote tip reader and revalidated
before and after, the author parsed from the reviewer leaf's stored
prompt, the resume mark inside the reviewer leaf, fail closed when the
mark cannot be written). One builder owns the command text, and the
prompt template, the resume message and the takeoff hint are tested
against it. Fakes only: a scripted `run` behind `push.remote_head`, a
recording resume double, and a temporary sessions root. No network, no
real session dir, no live unit.
"""

import io
import os
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from korapy import drive, paths, prompts, reviewrecord
from tests.test_reviewrecord import git_in

REPO = "app"
NUMBER = 7
PREFIX = "blue-agent-"
REVIEWER = "blue-agent-app-7-r1"
RIVAL_REVIEWER = "blue-agent-app-7-r2"
WORKER = "drive-11111111-7"
SHA_A = "a" * 40
SHA_B = "b" * 40

FINISHED = (REVIEWER, "reviewer", "ok")


def reviewer_prompt(worker=WORKER):
    """A stored reviewer prompt naming `worker` as the change's author."""
    return "review the change\n" + drive.reviewer_author_block(worker) + "\n"


def make_leaf(root, repo=REPO, number=NUMBER, gen=1, prompt=None):
    """A reviewer session leaf under a fake sessions root; returns the leaf."""
    leaf = os.path.join(root, f"{repo}-{number}", f"reviewer-{gen}")
    os.makedirs(leaf, exist_ok=True)
    if prompt is not None:
        with open(os.path.join(leaf, "prompt.md"), "w", encoding="utf-8") as handle:
            handle.write(prompt)
    return leaf


def make_tgt(root, repo=REPO, prefix=PREFIX):
    """A launch-target double: clone, branch template and unit prefix."""
    return SimpleNamespace(
        root=root,
        repo=repo,
        clone=os.path.join(root, "clone"),
        branch_template="{type}/{issue}-{slug}",
        resolved={"agents.unit_prefix": prefix},
    )


class FakeRemote:
    """A `push.remote_head` run double: one scripted tip per read.

    Each entry is a full sha, "" for a missing ref, or None for an
    unreachable remote. Calls record the argv, so tests see the exact
    branch each read asked for.
    """

    def __init__(self, tips):
        self.tips = list(tips)
        self.calls = []

    def __call__(self, argv, env=None, timeout=None):
        self.calls.append(list(argv))
        tip = self.tips.pop(0) if self.tips else ""
        if tip is None:
            return subprocess.CompletedProcess(argv, 1, "", "fatal: unable to connect")
        ref = argv[-1]
        return subprocess.CompletedProcess(argv, 0, f"{tip}\t{ref}\n" if tip else "", "")

    def branches(self):
        """The `refs/heads/<branch>` each call asked for."""
        return [argv[-1] for argv in self.calls]


class FakeLaunchResume:
    """A `launch.resume` double honoring the hold gate like the real one."""

    def __init__(self):
        self.calls = []

    def __call__(self, tgt, number, message, *, role=None, hold_reason_fn=None, **kwargs):
        from korapy import launch

        self.calls.append({"tgt": tgt, "number": number, "message": message, "role": role})
        reason = hold_reason_fn(tgt, number) if hold_reason_fn is not None else None
        if reason is not None:
            raise launch.LaunchError(reason)
        return (f"resumed-{number}", None)


class FakeStore:
    """A store double with fixed issue labels."""

    def __init__(self, labels):
        self.labels = list(labels)

    def item(self, repo, number, now=None):
        return SimpleNamespace(labels=list(self.labels)), None


class FakeResume:
    """A resume double: records calls, optionally refuses or writes a record."""

    def __init__(self):
        self.calls = []
        self.error = None
        self.on_call = None

    def __call__(self, repo, number, tgt, message):
        self.calls.append((repo, number, tgt, message))
        if self.error is not None:
            raise self.error
        if self.on_call is not None:
            self.on_call(repo, number, tgt, message)
        return f"{repo}-resumed-unit"


def step(missing, resume, remote, root, tgt=None, **fields):
    """`reprompt_missing_reviews` with test defaults; returns (outcomes, out)."""
    out = io.StringIO()
    values = {
        "finished": {(REPO, NUMBER): FINISHED},
        "reviews": {},
        "titles": {(REPO, NUMBER): "fix: thing"},
        "resolve": lambda repo, number: make_tgt(root) if tgt is None else tgt,
        "sessions_root": root,
        "run": remote,
        "resume": resume,
        "out": out,
    }
    values.update(fields)
    return drive.reprompt_missing_reviews(missing, **values), out.getvalue()


class NeedsRepromptTest(unittest.TestCase):
    def test_a_reviewer_ok_with_no_record_and_no_mark_reprompts(self):
        self.assertTrue(drive.needs_reprompt("", FINISHED, {}, (REPO, NUMBER), set()))

    def test_a_present_mark_never_reprompts(self):
        self.assertFalse(drive.needs_reprompt("", FINISHED, {}, (REPO, NUMBER), {(REPO, NUMBER)}))

    def test_a_non_ok_outcome_never_reprompts(self):
        for outcome in ("error", "killed", None):
            with self.subTest(outcome=outcome):
                finished = (REVIEWER, "reviewer", outcome)
                self.assertFalse(drive.needs_reprompt("", finished, {}, (REPO, NUMBER), set()))

    def test_a_present_record_never_reprompts(self):
        record = {"verdict": "approve", "reviewer": REVIEWER}
        self.assertFalse(
            drive.needs_reprompt("", FINISHED, {(REPO, NUMBER): record}, (REPO, NUMBER), set())
        )

    def test_a_worker_entry_never_reprompts(self):
        finished = (WORKER, "worker", "ok")
        self.assertFalse(drive.needs_reprompt("", finished, {}, (REPO, NUMBER), set()))

    def test_a_short_entry_never_reprompts(self):
        for finished in (None, (), (WORKER,)):
            with self.subTest(finished=finished):
                self.assertFalse(drive.needs_reprompt("", finished, {}, (REPO, NUMBER), set()))

    def test_a_held_row_never_reprompts(self):
        self.assertFalse(drive.needs_reprompt("some-holder", FINISHED, {}, (REPO, NUMBER), set()))

    def test_a_missing_review_map_never_reprompts(self):
        self.assertFalse(drive.needs_reprompt("", FINISHED, None, (REPO, NUMBER), set()))


class ReviewRecordCommandTest(unittest.TestCase):
    def test_the_builder_spells_the_full_command(self):
        self.assertEqual(
            prompts.review_record_command(REPO, NUMBER, SHA_A, WORKER),
            f"korapy review record {REPO} {NUMBER} --commit {SHA_A} "
            "--verdict <approve|approve-with-findings|block> "
            f"--author {WORKER}",
        )

    def test_the_verdict_alternation_follows_verdicts(self):
        command = prompts.review_record_command(REPO, NUMBER, SHA_A, WORKER)
        self.assertIn("<" + "|".join(reviewrecord.VERDICTS) + ">", command)

    def test_the_prompt_template_carries_the_builder_command(self):
        template = prompts._builtin_text("reviewer")
        command = prompts.review_record_command("<repo>", "{issue}", "<full-sha>", "<worker-id>")
        self.assertIn(command, template)

    def test_the_reprompt_message_carries_the_exact_command(self):
        command = prompts.review_record_command(REPO, NUMBER, SHA_A, WORKER)
        message = drive.reprompt_message(REPO, NUMBER, SHA_A, WORKER)
        self.assertIn(command, message)
        self.assertIn(f"{REPO}#{NUMBER}", message)

    def test_the_takeoff_hint_appends_the_reviewer_suffix_to_the_builder_command(self):
        from korapy import takeoff

        with tempfile.TemporaryDirectory(prefix="korapy-reprompt-hint-") as tmp:
            repo = os.path.realpath(tmp)
            git_in(repo, "init", "-q", "-b", "master")
            git_in(repo, "commit", "-q", "--allow-empty", "-m", "init")
            head = git_in(repo, "rev-parse", "HEAD")
            with mock.patch.dict(os.environ, {"KORAPY_SESSIONS": os.path.join(repo, "sessions")}):
                check = takeoff._check_review(True, REPO, NUMBER, repo, head)
        self.assertEqual(check.status, takeoff.FAIL)
        command = prompts.review_record_command(REPO, NUMBER, head, "<change-author>")
        self.assertIn(f"{command} --reviewer <reviewer-id>", check.fix)


class ParseReviewAuthorTest(unittest.TestCase):
    def test_the_prompt_names_the_worker(self):
        self.assertEqual(drive.parse_review_author(reviewer_prompt()), WORKER)

    def test_a_prompt_without_the_block_names_no_author(self):
        self.assertEqual(drive.parse_review_author("review the change\n"), "")

    def test_a_blank_text_names_no_author(self):
        self.assertEqual(drive.parse_review_author(""), "")
        self.assertEqual(drive.parse_review_author(None), "")


class RepromptStepTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="korapy-reprompt-")
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        self.missing = [(REPO, NUMBER, REVIEWER)]

    def test_a_missing_record_requests_one_resume_with_the_exact_command(self):
        make_leaf(self.root, prompt=reviewer_prompt())
        resume = FakeResume()
        outcomes, out = step(self.missing, resume, FakeRemote([SHA_A, SHA_A, SHA_A]), self.root)
        self.assertEqual(outcomes, {(REPO, NUMBER): "resumed"})
        self.assertEqual(len(resume.calls), 1)
        repo, number, _tgt, message = resume.calls[0]
        self.assertEqual((repo, number), (REPO, NUMBER))
        self.assertIn(prompts.review_record_command(REPO, NUMBER, SHA_A, WORKER), message)
        self.assertTrue(
            os.path.isfile(os.path.join(self.root, f"{REPO}-{NUMBER}", "reviewer-1", ".reprompted"))
        )
        self.assertIn(f"{REPO}#{NUMBER}", out)
        self.assertIn("resumed", out)

    def test_the_resume_pins_the_ticket_branch_tip(self):
        make_leaf(self.root, prompt=reviewer_prompt())
        resume = FakeResume()
        remote = FakeRemote([SHA_A, SHA_A, SHA_A])
        step(self.missing, resume, remote, self.root)
        self.assertEqual(len(remote.calls), 3)
        for ref in remote.branches():
            self.assertEqual(ref, "refs/heads/fix/7-thing")

    def test_a_second_pass_with_the_mark_requests_no_resume(self):
        make_leaf(self.root, prompt=reviewer_prompt())
        resume = FakeResume()
        step(self.missing, resume, FakeRemote([SHA_A, SHA_A, SHA_A]), self.root)
        again = FakeResume()
        remote = FakeRemote([SHA_A, SHA_A, SHA_A])
        outcomes, _ = step(self.missing, again, remote, self.root)
        self.assertEqual(again.calls, [])
        self.assertEqual(remote.calls, [])
        self.assertEqual(outcomes, {(REPO, NUMBER): "refused: already reprompted"})

    def test_a_moved_head_between_plan_and_resume_requests_none_and_keeps_the_skip(self):
        make_leaf(self.root, prompt=reviewer_prompt())
        resume = FakeResume()
        outcomes, out = step(self.missing, resume, FakeRemote([SHA_A, SHA_B, SHA_A]), self.root)
        self.assertEqual(resume.calls, [])
        self.assertEqual(outcomes, {(REPO, NUMBER): "refused: the branch moved"})
        self.assertIn("moved", out)

    def test_an_unreadable_tip_requests_no_resume(self):
        make_leaf(self.root, prompt=reviewer_prompt())
        resume = FakeResume()
        outcomes, _ = step(self.missing, resume, FakeRemote(["", "", ""]), self.root)
        self.assertEqual(resume.calls, [])
        self.assertEqual(outcomes, {(REPO, NUMBER): "refused: the branch tip is unreadable"})

    def test_a_raising_tip_reader_requests_no_resume_and_fails_no_pass(self):
        def raising(argv, env=None, timeout=None):
            raise RuntimeError("git is gone")

        make_leaf(self.root, prompt=reviewer_prompt())
        resume = FakeResume()
        outcomes, _ = step(self.missing, resume, raising, self.root)
        self.assertEqual(resume.calls, [])
        self.assertEqual(outcomes, {(REPO, NUMBER): "refused: the branch tip is unreadable"})

    def test_a_refused_resume_keeps_the_skip_and_the_log_line(self):
        make_leaf(self.root, prompt=reviewer_prompt())
        resume = FakeResume()
        resume.error = ValueError("no session to resume")
        outcomes, out = step(self.missing, resume, FakeRemote([SHA_A, SHA_A, SHA_A]), self.root)
        self.assertEqual(
            outcomes, {(REPO, NUMBER): "refused: resume refused (no session to resume)"}
        )
        self.assertIn("refused", out)
        self.assertTrue(
            os.path.isfile(os.path.join(self.root, f"{REPO}-{NUMBER}", "reviewer-1", ".reprompted"))
        )

    def test_an_unwritable_mark_requests_no_resume_and_fails_no_pass(self):
        leaf = make_leaf(self.root, prompt=reviewer_prompt())
        os.chmod(leaf, 0o500)
        self.addCleanup(os.chmod, leaf, 0o700)
        resume = FakeResume()
        outcomes, _ = step(self.missing, resume, FakeRemote([SHA_A, SHA_A, SHA_A]), self.root)
        self.assertEqual(resume.calls, [])
        self.assertEqual(outcomes, {(REPO, NUMBER): "refused: the reprompt mark is unwritable"})

    def test_an_unreadable_leaf_prompt_requests_no_resume(self):
        make_leaf(self.root, prompt=None)
        resume = FakeResume()
        outcomes, _ = step(self.missing, resume, FakeRemote([SHA_A, SHA_A, SHA_A]), self.root)
        self.assertEqual(resume.calls, [])
        self.assertEqual(outcomes, {(REPO, NUMBER): "refused: the reviewer prompt is unreadable"})

    def test_a_non_utf8_leaf_prompt_requests_no_resume(self):
        leaf = make_leaf(self.root, prompt=None)
        with open(os.path.join(leaf, "prompt.md"), "wb") as handle:
            handle.write(b"review \xff\xfe the change\n")
        resume = FakeResume()
        outcomes, _ = step(self.missing, resume, FakeRemote([SHA_A, SHA_A, SHA_A]), self.root)
        self.assertEqual(resume.calls, [])
        self.assertEqual(outcomes, {(REPO, NUMBER): "refused: the reviewer prompt is unreadable"})

    def test_a_prompt_without_a_worker_id_requests_no_resume(self):
        make_leaf(self.root, prompt="review the change\n")
        resume = FakeResume()
        outcomes, _ = step(self.missing, resume, FakeRemote([SHA_A, SHA_A, SHA_A]), self.root)
        self.assertEqual(resume.calls, [])
        self.assertEqual(outcomes, {(REPO, NUMBER): "refused: the reviewer prompt names no author"})

    def test_a_self_reviewing_author_requests_no_resume(self):
        make_leaf(self.root, prompt=reviewer_prompt(worker=REVIEWER))
        resume = FakeResume()
        finished = {(REPO, NUMBER): (REVIEWER, "reviewer", "ok")}
        outcomes, _ = step(
            self.missing, resume, FakeRemote([SHA_A, SHA_A, SHA_A]), self.root, finished=finished
        )
        self.assertEqual(resume.calls, [])
        self.assertEqual(outcomes, {(REPO, NUMBER): "refused: the author is the reviewer itself"})

    def test_an_unparseable_generation_requests_no_resume(self):
        make_leaf(self.root, prompt=reviewer_prompt())
        resume = FakeResume()
        finished = {(REPO, NUMBER): ("drive-11111111-7", "reviewer", "ok")}
        outcomes, _ = step(
            [(REPO, NUMBER, "drive-11111111-7")],
            resume,
            FakeRemote([SHA_A, SHA_A, SHA_A]),
            self.root,
            finished=finished,
        )
        self.assertEqual(resume.calls, [])
        self.assertEqual(outcomes, {(REPO, NUMBER): "refused: the reviewer session is unknown"})

    def test_a_ticket_without_a_launch_target_requests_no_resume(self):
        make_leaf(self.root, prompt=reviewer_prompt())
        resume = FakeResume()
        outcomes, _ = step(
            self.missing,
            resume,
            FakeRemote([SHA_A, SHA_A, SHA_A]),
            self.root,
            resolve=lambda repo, number: None,
        )
        self.assertEqual(resume.calls, [])
        self.assertEqual(
            outcomes, {(REPO, NUMBER): f"refused: {REPO}#{NUMBER} has no launch target"}
        )

    def test_an_unusable_branch_template_requests_no_resume(self):
        make_leaf(self.root, prompt=reviewer_prompt())
        resume = FakeResume()
        bad = make_tgt(self.root)
        bad.branch_template = "no-placeholders"
        outcomes, _ = step(
            self.missing, resume, FakeRemote([SHA_A, SHA_A, SHA_A]), self.root, tgt=bad
        )
        self.assertEqual(resume.calls, [])
        outcome = outcomes[(REPO, NUMBER)]
        self.assertTrue(outcome.startswith("refused: "), outcome)

    def test_a_record_written_by_the_resume_clears_the_next_skip(self):
        def write_record(repo, number, tgt, message):
            reviewrecord.write_record(
                repo,
                number,
                SHA_A,
                reviewer=REVIEWER,
                verdict="approve",
                author=WORKER,
                sessions_root=self.root,
            )

        make_leaf(self.root, prompt=reviewer_prompt())
        resume = FakeResume()
        resume.on_call = write_record
        outcomes, _ = step(self.missing, resume, FakeRemote([SHA_A, SHA_A, SHA_A]), self.root)
        self.assertEqual(outcomes, {(REPO, NUMBER): "resumed"})
        reviews = {(REPO, NUMBER): reviewrecord.latest_record(REPO, NUMBER, self.root)}
        self.assertFalse(
            drive.missing_review_records(
                {(REPO, NUMBER): FINISHED}, reviews, {(REPO, NUMBER): _open_row()}
            )
        )
        self.assertFalse(
            drive.needs_reprompt("", FINISHED, reviews, (REPO, NUMBER), {(REPO, NUMBER)})
        )

    def test_a_tip_moved_after_the_resume_proceeds_with_the_mark_spent(self):
        make_leaf(self.root, prompt=reviewer_prompt())
        resume = FakeResume()
        outcomes, out = step(self.missing, resume, FakeRemote([SHA_A, SHA_A, SHA_B]), self.root)
        self.assertEqual(len(resume.calls), 1)
        self.assertEqual(outcomes, {(REPO, NUMBER): "resumed-tip-moved"})
        self.assertIn("fresh review", out)
        self.assertTrue(
            os.path.isfile(os.path.join(self.root, f"{REPO}-{NUMBER}", "reviewer-1", ".reprompted"))
        )

    def test_two_recordless_tickets_resume_independently(self):
        make_leaf(self.root, prompt=reviewer_prompt())
        make_leaf(self.root, number=8, prompt=reviewer_prompt())
        reviewer8 = "blue-agent-app-8-r1"
        missing = [(REPO, NUMBER, REVIEWER), (REPO, 8, reviewer8)]
        resume = FakeResume()
        remote = FakeRemote([SHA_A, SHA_A, SHA_A, SHA_B, SHA_B, SHA_B])
        out = io.StringIO()
        outcomes = drive.reprompt_missing_reviews(
            missing,
            finished={
                (REPO, NUMBER): FINISHED,
                (REPO, 8): (reviewer8, "reviewer", "ok"),
            },
            reviews={},
            titles={(REPO, NUMBER): "fix: thing", (REPO, 8): "fix: other"},
            resolve=lambda repo, number: make_tgt(self.root),
            sessions_root=self.root,
            run=remote,
            resume=resume,
            out=out,
        )
        self.assertEqual(outcomes, {(REPO, NUMBER): "resumed", (REPO, 8): "resumed"})
        self.assertEqual(len(resume.calls), 2)
        self.assertIn(
            prompts.review_record_command(REPO, NUMBER, SHA_A, WORKER), resume.calls[0][3]
        )
        self.assertIn(prompts.review_record_command(REPO, 8, SHA_B, WORKER), resume.calls[1][3])

    def test_one_unresolvable_ticket_leaves_the_other_to_resume(self):
        make_leaf(self.root, prompt=reviewer_prompt())
        make_leaf(self.root, number=8, prompt=reviewer_prompt())
        reviewer8 = "blue-agent-app-8-r1"
        missing = [(REPO, NUMBER, REVIEWER), (REPO, 8, reviewer8)]
        resume = FakeResume()
        remote = FakeRemote([SHA_A, SHA_A, SHA_A])
        out = io.StringIO()
        outcomes = drive.reprompt_missing_reviews(
            missing,
            finished={
                (REPO, NUMBER): FINISHED,
                (REPO, 8): (reviewer8, "reviewer", "ok"),
            },
            reviews={},
            titles={(REPO, NUMBER): "fix: thing", (REPO, 8): "fix: other"},
            resolve=lambda repo, number: make_tgt(self.root) if number == NUMBER else None,
            sessions_root=self.root,
            run=remote,
            resume=resume,
            out=out,
        )
        self.assertEqual(len(resume.calls), 1)
        self.assertEqual(
            outcomes,
            {
                (REPO, NUMBER): "resumed",
                (REPO, 8): f"refused: {REPO}#8 has no launch target",
            },
        )

    def test_an_unexpected_planning_error_refuses_one_ticket_and_resumes_the_other(self):
        make_leaf(self.root, prompt=reviewer_prompt())
        make_leaf(self.root, number=8, prompt=reviewer_prompt())
        reviewer8 = "blue-agent-app-8-r1"
        missing = [(REPO, NUMBER, REVIEWER), (REPO, 8, reviewer8)]
        resume = FakeResume()
        remote = FakeRemote([SHA_B, SHA_B, SHA_B])

        def resolve(repo, number):
            if number == NUMBER:
                bad = make_tgt(self.root)
                bad.branch_template = "{type.nonexistent}/{issue}-{slug}"
                return bad
            return make_tgt(self.root)

        out = io.StringIO()
        outcomes = drive.reprompt_missing_reviews(
            missing,
            finished={
                (REPO, NUMBER): FINISHED,
                (REPO, 8): (reviewer8, "reviewer", "ok"),
            },
            reviews={},
            titles={(REPO, NUMBER): "fix: thing", (REPO, 8): "fix: other"},
            resolve=resolve,
            sessions_root=self.root,
            run=remote,
            resume=resume,
            out=out,
        )
        self.assertEqual(len(resume.calls), 1)
        self.assertEqual(resume.calls[0][1], 8)
        self.assertTrue(outcomes[(REPO, NUMBER)].startswith("refused: "), outcomes)
        self.assertEqual(outcomes[(REPO, 8)], "resumed")

    def test_a_raising_resolve_refuses_one_ticket_and_resumes_the_other(self):
        make_leaf(self.root, prompt=reviewer_prompt())
        make_leaf(self.root, number=8, prompt=reviewer_prompt())
        reviewer8 = "blue-agent-app-8-r1"
        missing = [(REPO, NUMBER, REVIEWER), (REPO, 8, reviewer8)]
        resume = FakeResume()
        remote = FakeRemote([SHA_B, SHA_B, SHA_B])

        def resolve(repo, number):
            if number == NUMBER:
                raise AttributeError("boom in resolve")
            return make_tgt(self.root)

        out = io.StringIO()
        outcomes = drive.reprompt_missing_reviews(
            missing,
            finished={
                (REPO, NUMBER): FINISHED,
                (REPO, 8): (reviewer8, "reviewer", "ok"),
            },
            reviews={},
            titles={(REPO, NUMBER): "fix: thing", (REPO, 8): "fix: other"},
            resolve=resolve,
            sessions_root=self.root,
            run=remote,
            resume=resume,
            out=out,
        )
        self.assertEqual(len(resume.calls), 1)
        self.assertEqual(resume.calls[0][1], 8)
        self.assertTrue(outcomes[(REPO, NUMBER)].startswith("refused: "), outcomes)
        self.assertEqual(outcomes[(REPO, 8)], "resumed")

    def test_without_a_resume_seam_nothing_is_read_or_written(self):
        make_leaf(self.root, prompt=reviewer_prompt())
        remote = FakeRemote([SHA_A, SHA_A, SHA_A])
        outcomes, out = step(self.missing, None, remote, self.root)
        self.assertEqual(outcomes, {})
        self.assertEqual(remote.calls, [])
        self.assertEqual(out, "")
        self.assertFalse(
            os.path.exists(os.path.join(self.root, f"{REPO}-{NUMBER}", "reviewer-1", ".reprompted"))
        )


def _open_row():
    return SimpleNamespace(state="open", external=False, agent="")


class MarkPlacementTest(unittest.TestCase):
    def test_the_mark_inside_the_leaf_is_no_legacy_session(self):
        with tempfile.TemporaryDirectory(prefix="korapy-reprompt-mark-") as tmp:
            root = os.path.realpath(tmp)
            leaf = make_leaf(root, prompt=reviewer_prompt())
            ticket = os.path.join(root, f"{REPO}-{NUMBER}")
            before = paths.ticket_sessions(REPO, NUMBER, root=root)
            with open(os.path.join(leaf, ".reprompted"), "w", encoding="utf-8") as handle:
                handle.write("")
            self.assertFalse(paths._legacy_session(ticket))
            self.assertEqual(paths.ticket_sessions(REPO, NUMBER, root=root), before)
            self.assertIsNone(reviewrecord.latest_record(REPO, NUMBER, root))

    def test_a_direct_child_file_would_read_as_a_legacy_session(self):
        with tempfile.TemporaryDirectory(prefix="korapy-reprompt-mark-") as tmp:
            root = os.path.realpath(tmp)
            ticket = os.path.join(root, f"{REPO}-{NUMBER}")
            os.makedirs(ticket, exist_ok=True)
            with open(os.path.join(ticket, ".reprompted"), "w", encoding="utf-8") as handle:
                handle.write("")
            self.assertTrue(paths._legacy_session(ticket))


class WriteRaceTest(unittest.TestCase):
    def test_a_retry_with_the_same_reviewer_and_verdict_is_a_no_op(self):
        with tempfile.TemporaryDirectory(prefix="korapy-reprompt-race-") as tmp:
            root = os.path.realpath(tmp)
            first = reviewrecord.write_record(
                REPO,
                NUMBER,
                SHA_A,
                reviewer=REVIEWER,
                verdict="approve",
                author=WORKER,
                sessions_root=root,
            )
            second = reviewrecord.write_record(
                REPO,
                NUMBER,
                SHA_A,
                reviewer=REVIEWER,
                verdict="approve",
                author=WORKER,
                sessions_root=root,
            )
            self.assertEqual(first, second)
            record = reviewrecord.read_record(REPO, NUMBER, SHA_A, root)
            self.assertEqual((record["reviewer"], record["verdict"]), (REVIEWER, "approve"))

    def test_a_different_reviewer_or_verdict_is_refused(self):
        with tempfile.TemporaryDirectory(prefix="korapy-reprompt-race-") as tmp:
            root = os.path.realpath(tmp)
            reviewrecord.write_record(
                REPO,
                NUMBER,
                SHA_A,
                reviewer=REVIEWER,
                verdict="approve",
                author=WORKER,
                sessions_root=root,
            )
            with self.assertRaises(ValueError):
                reviewrecord.write_record(
                    REPO,
                    NUMBER,
                    SHA_A,
                    reviewer=RIVAL_REVIEWER,
                    verdict="approve",
                    author=WORKER,
                    sessions_root=root,
                )
            with self.assertRaises(ValueError):
                reviewrecord.write_record(
                    REPO,
                    NUMBER,
                    SHA_A,
                    reviewer=REVIEWER,
                    verdict="block",
                    author=WORKER,
                    sessions_root=root,
                )
            record = reviewrecord.read_record(REPO, NUMBER, SHA_A, root)
            self.assertEqual((record["reviewer"], record["verdict"]), (REVIEWER, "approve"))


class RepromptPassTest(unittest.TestCase):
    """The resume fires inside a real drive pass, through the injected seams."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="korapy-reprompt-pass-")
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        self.db = os.path.join(self.tmp, "board.sqlite")
        self.lock = os.path.join(self.tmp, "drive.lock")
        self.policies = os.path.join(self.tmp, "policies")
        os.makedirs(self.policies)
        self.sessions = os.path.join(self.tmp, "sessions")
        os.makedirs(self.sessions)
        make_leaf(self.sessions, prompt=reviewer_prompt())

    def run_drive(self, resume, remote, **fields):
        from tests.test_drive import stored
        from tests.test_drive_claims import drive_pass
        from tests.test_tracker import FakeTracker, reading

        store = stored(self.db, [(REPO, NUMBER, "open")])
        self.addCleanup(store.close)
        tgt = make_tgt(self.tmp)
        values = {
            "found": [("blue", [(REPO, NUMBER, "review")])],
            "policy_dir": self.policies,
            "fake_tracker": FakeTracker([reading(repo=REPO, number=NUMBER, updated_at=1900.0)]),
            "finished_for": {(REPO, NUMBER): (REVIEWER, "reviewer", "ok")},
            "review_reader": lambda repo, number: None,
            "reprompt_resolve": lambda repo, number: tgt,
            "reprompt_run": remote,
            "reprompt_resume": resume,
            "reprompt_sessions_root": self.sessions,
        }
        values.update(fields)
        out, err = io.StringIO(), io.StringIO()
        code = drive_pass(store, self.lock, out, err, **values)
        return code, out.getvalue(), err.getvalue()

    def test_a_missing_record_pass_resumes_the_reviewer_once(self):
        resume = FakeResume()
        code, out, _ = self.run_drive(resume, FakeRemote([SHA_A, SHA_A, SHA_A]))
        self.assertEqual(code, 0)
        self.assertEqual(len(resume.calls), 1)
        repo, number, _tgt, message = resume.calls[0]
        self.assertEqual((repo, number), (REPO, NUMBER))
        self.assertIn(prompts.review_record_command(REPO, NUMBER, SHA_A, WORKER), message)
        self.assertIn("review missing", out)
        self.assertIn("resumed", out)

    def test_plan_prints_the_skip_and_resumes_nothing(self):
        resume = FakeResume()
        remote = FakeRemote([SHA_A, SHA_A, SHA_A])
        code, out, _ = self.run_drive(resume, remote, plan=True)
        self.assertEqual(code, 0)
        self.assertEqual(resume.calls, [])
        self.assertEqual(remote.calls, [])
        self.assertIn("review missing", out)


class ProductionRepromptWiringTest(unittest.TestCase):
    """Direct tests for the production reprompt wiring.

    `_reprompt_target` (the stuck owner lookup, the driven fallback,
    every failure reading as no target) and
    `production_reprompt_resume` (a reviewer resume over the launch
    path with the store-backed hold check) previously ran only behind
    a faked `reprompt_resolve` pass test, so these pin them with fakes
    at their own seams: a fake registry, a recording `launch.target` /
    `launch.resume` double, and a tiny store double. No network, no
    real ops root, no live unit.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="korapy-reprompt-prod-")
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        self.registered = [SimpleNamespace(name="blue", root=self.root)]
        self.driven = {"blue": [REPO]}
        self.tgt = SimpleNamespace(root=self.root, repo=REPO, clone=self.root)

    def resolve_with(self, stuck_project, driven=None, short="app"):
        target = SimpleNamespace(root=self.root, repo=REPO, clone=self.root)
        with (
            mock.patch.object(drive, "_local_repo", return_value=short) as local_fn,
            mock.patch.object(drive.launch, "target", return_value=target) as target_fn,
        ):
            resolve = drive._reprompt_target(
                self.registered, self.driven if driven is None else driven, stuck_project
            )
            return resolve(REPO, NUMBER), local_fn, target_fn, target

    def test_the_stuck_owner_resolves_through_the_launch_path(self):
        found, local_fn, target_fn, target = self.resolve_with(lambda repo, number: "blue")
        self.assertIs(found, target)
        local_fn.assert_called_once_with(self.root, REPO)
        target_fn.assert_called_once_with(self.root, "app")

    def test_the_driven_fallback_resolves_when_stuck_names_no_owner(self):
        found, _, _, target = self.resolve_with(lambda repo, number: None)
        self.assertIs(found, target)

    def test_an_undriven_repo_reads_as_no_target(self):
        found, _, target_fn, _ = self.resolve_with(lambda repo, number: None, driven={})
        self.assertIsNone(found)
        target_fn.assert_not_called()

    def test_an_unknown_owner_reads_as_no_target(self):
        found, _, target_fn, _ = self.resolve_with(lambda repo, number: "red")
        self.assertIsNone(found)
        target_fn.assert_not_called()

    def test_a_settings_error_from_stuck_reads_as_no_target(self):
        from korapy import settings

        def stuck(repo, number):
            raise settings.SettingsError("bad registry")

        found, _, target_fn, _ = self.resolve_with(stuck)
        self.assertIsNone(found)
        target_fn.assert_not_called()

    def test_a_launch_failure_reads_as_no_target(self):
        from korapy import launch

        with (
            mock.patch.object(drive, "_local_repo", return_value="app"),
            mock.patch.object(drive.launch, "target", side_effect=launch.LaunchError("no clone")),
        ):
            resolve = drive._reprompt_target(self.registered, self.driven, lambda r, n: "blue")
            self.assertIsNone(resolve(REPO, NUMBER))

    def resume_one_with(self, labels, hold_label="paused"):
        fake = FakeLaunchResume()
        resume_one = drive.production_reprompt_resume(
            self.registered, store=FakeStore(labels), hold_label=hold_label, now=1234.0
        )
        return resume_one, fake

    def test_a_free_ticket_resumes_as_reviewer(self):
        resume_one, fake = self.resume_one_with([])
        with mock.patch.object(drive.launch, "resume", fake):
            unit = resume_one(REPO, NUMBER, self.tgt, "record this")
        self.assertEqual(unit, f"resumed-{NUMBER}")
        self.assertEqual(len(fake.calls), 1)
        call = fake.calls[0]
        self.assertIs(call["tgt"], self.tgt)
        self.assertEqual(call["number"], NUMBER)
        self.assertEqual(call["message"], "record this")
        self.assertEqual(call["role"], "reviewer")

    def test_a_held_ticket_raises_without_a_resumed_unit(self):
        from korapy import launch

        resume_one, _ = self.resume_one_with(["paused"])
        with mock.patch.object(drive.launch, "resume", FakeLaunchResume()):
            with self.assertRaises(launch.LaunchError):
                resume_one(REPO, NUMBER, self.tgt, "record this")

    def test_an_unknown_project_raises_without_calling_resume(self):
        from korapy import launch

        resume_one, fake = self.resume_one_with([])
        stray = SimpleNamespace(root="/ops/nowhere", repo=REPO, clone="/ops/nowhere")
        with mock.patch.object(drive.launch, "resume", fake):
            with self.assertRaises(launch.LaunchError):
                resume_one(REPO, NUMBER, stray, "record this")
        self.assertEqual(fake.calls, [])
