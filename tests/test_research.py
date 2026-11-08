"""Ticket 683: a research ticket kind that finishes as a Findings comment.

A ticket carrying the `research` label starts with the research worker
prompt (read-only, no commits, one Findings comment). The loop treats
it as done when that comment exists: it releases the claim, adds the
`researched` label, and never starts a reviewer round or waits for a
PR. Findings surface as dashboard attention items until acknowledged.

Pure cores, fakes only: no network, no subprocess, no real dirs beyond
temporary ones.
"""

import io
import os
import tempfile
import unittest

from korapy import board, dashboard, drive, eligibility, policies, prompts, tracker
from korapy.eligibility import StartRequest, TicketRow
from korapy.github import tracker as ghtracker
from tests import test_drive_gates as gates
from tests.test_drive import hermetic_pass, policy_source, project_named
from tests.test_tracker import FakeRun, FakeTracker


def research_row(
    number=7,
    agent="",
    repo="app",
    state="open",
    priority="",
    labels=("research",),
    external=False,
    pulls=(),
):
    return TicketRow(
        repo=repo,
        number=number,
        state=state,
        agent=agent,
        priority=priority,
        labels=labels,
        external=external,
        pulls=pulls,
    )


def research_entry(number=7, kind="research", agent="", labels=("research",), pulls=()):
    return drive.GateInput(
        project="blue",
        request=StartRequest(repo="app", number=number, kind=kind),
        row=research_row(number=number, agent=agent, labels=labels, pulls=pulls),
        wait=1000.0,
    )


def finished_entry(agent="drive-host-1-x", role="research", outcome="ok"):
    return (agent, role, outcome)


class FindingsTest(unittest.TestCase):
    def test_no_comments_means_no_findings(self):
        from korapy import research

        self.assertIsNone(research.findings([]))

    def test_a_comment_starting_with_findings_is_the_finding(self):
        from korapy import research

        body = "Findings: go, because the API already exists\n\nDetails here."
        self.assertEqual(research.findings(["thanks", body]), body)

    def test_a_first_line_that_only_mentions_findings_is_not_enough(self):
        from korapy import research

        self.assertIsNone(research.findings(["My Findings so far"]))
        self.assertIsNone(research.findings(["  Findings with leading space"]))

    def test_the_first_findings_comment_wins(self):
        from korapy import research

        first = "Findings: no-go"
        second = "Findings: go after all"
        self.assertEqual(research.findings([first, second]), first)

    def test_non_string_comments_are_ignored(self):
        from korapy import research

        self.assertIsNone(research.findings([None, 42]))


def holder_of(agent="drive-host-1-x", role="research"):
    return {("app", 7): (agent, role)}


class SettlementPlanTest(unittest.TestCase):
    def test_finished_research_with_findings_is_due_with_findings(self):
        from korapy import research

        item = board.Item(
            repo="app",
            number=7,
            title="T",
            state="open",
            labels=["research"],
            agent="drive-host-1-x",
        )
        comments = {("app", 7): ["note", "Findings: go"]}
        due = research.settlements([item], holder_of(), comments)
        self.assertEqual(len(due), 1)
        self.assertTrue(due[0].has_findings)

    def test_finished_research_without_findings_is_due_without_them(self):
        from korapy import research

        item = board.Item(
            repo="app",
            number=7,
            title="T",
            state="open",
            labels=["research"],
            agent="drive-host-1-x",
        )
        comments = {("app", 7): ["still digging"]}
        due = research.settlements([item], holder_of(), comments)
        self.assertEqual(len(due), 1)
        self.assertFalse(due[0].has_findings)

    def test_unreadable_comments_settle_nothing(self):
        from korapy import research

        item = board.Item(
            repo="app",
            number=7,
            title="T",
            state="open",
            labels=["research"],
            agent="drive-host-1-x",
        )
        self.assertEqual(research.settlements([item], holder_of(), {("app", 7): None}), [])

    def test_a_later_findings_comment_never_beats_the_first(self):
        from korapy import research

        item = board.Item(
            repo="app",
            number=7,
            title="T",
            state="open",
            labels=["research"],
            agent="drive-host-1-x",
        )
        comments = {("app", 7): ["Findings: no-go on old facts", "Findings: go on new facts"]}
        due = research.settlements([item], holder_of(), comments)
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0].findings, "Findings: no-go on old facts")

    def test_unfinished_or_unclaimed_or_labeled_rows_settle_nothing(self):
        from korapy import research

        claimed = board.Item(
            repo="app",
            number=7,
            title="T",
            state="open",
            labels=["research"],
            agent="drive-host-1-x",
        )
        comments = {("app", 7): ["Findings: go"]}
        self.assertEqual(research.settlements([claimed], {}, comments), [])
        self.assertEqual(
            research.settlements([claimed], {("app", 7): ("drive-host-1-x", "worker")}, comments),
            [],
        )
        unclaimed = board.Item(repo="app", number=8, title="T", state="open", labels=["research"])
        self.assertEqual(
            research.settlements(
                [unclaimed],
                {("app", 8): ("drive-host-1-x", "research")},
                {("app", 8): ["Findings: go"]},
            ),
            [],
        )
        labeled = board.Item(
            repo="app",
            number=9,
            title="T",
            state="open",
            labels=["research", "researched"],
            agent="drive-host-1-x",
        )
        self.assertEqual(
            research.settlements(
                [labeled],
                {("app", 9): ("drive-host-1-x", "research")},
                {("app", 9): ["Findings: go"]},
            ),
            [],
        )

    def test_a_settlement_carries_its_driving_project(self):
        from korapy import research

        item = board.Item(
            repo="app",
            number=7,
            title="T",
            state="open",
            labels=["research"],
            agent="drive-host-1-x",
        )
        due = research.settlements(
            [item],
            holder_of(),
            {("app", 7): ["Findings: go"]},
            projects={("app", 7): "green"},
        )
        self.assertEqual([entry.project for entry in due], ["green"])


class PromptsResearchTest(unittest.TestCase):
    def test_research_is_a_known_role(self):
        self.assertIn("research", prompts.ROLES)

    def test_research_has_an_override_setting(self):
        from korapy import settings

        settings._load_providers()
        self.assertIn("prompts.research", settings.REGISTRY)

    def test_research_builtin_renders_with_the_shared_placeholders(self):
        with tempfile.TemporaryDirectory() as root:
            text = prompts.render(
                "research",
                {
                    "issue": "7",
                    "title": "T",
                    "branch": "b",
                    "worktree": "w",
                    "checks.test": "make test",
                    "overlay": "",
                    "project_agents_md": "AGENTS.md",
                },
                ops_root=root,
            )
        self.assertIn("7", text)

    def test_research_prompt_is_read_only_with_one_findings_comment(self):
        text = prompts._builtin_text("research")
        lowered = text.lower()
        self.assertIn("findings", lowered)
        self.assertIn("push nothing", lowered)
        self.assertIn("no pull request", lowered)


class EligibilityResearchTest(unittest.TestCase):
    def test_research_is_a_request_kind(self):
        self.assertIn("research", eligibility.KINDS)

    def test_basic_returns_a_research_request_for_research_rows(self):
        rows = [
            TicketRow(repo="app", number=7, labels=("research",)),
            TicketRow(repo="app", number=8, labels=()),
        ]
        found = policies.basic(rows, now=1000.0)
        by_number = {request.number: request.kind for request in found}
        self.assertEqual(by_number, {7: "research", 8: "start"})

    def test_basic_never_selects_a_researched_row(self):
        rows = [TicketRow(repo="app", number=7, labels=("research", "researched"))]
        self.assertEqual(policies.basic(rows, now=1000.0), [])

    def test_basic_selects_a_research_row_with_unreadable_pulls(self):
        rows = [research_row(number=7, pulls=None)]
        found = policies.basic(rows, now=1000.0)
        self.assertEqual([(request.number, request.kind) for request in found], [(7, "research")])

    def test_basic_selects_a_research_row_with_an_open_pull(self):
        from tests import test_drive_gates as gates_mod

        rows = [research_row(number=7, pulls=(gates_mod.open_pull(3),))]
        found = policies.basic(rows, now=1000.0)
        self.assertEqual([(request.number, request.kind) for request in found], [(7, "research")])


class GateResearchTest(unittest.TestCase):
    def test_finished_research_never_takes_over_as_reviewer(self):
        candidate = research_entry(agent="drive-host-1-x")
        gated = gates.gate(candidate, finished={("app", 7): finished_entry()})
        self.assertEqual((gated.verdict, gated.code), ("skip", "claim"))
        self.assertIn("already claimed", gated.reason)
        self.assertNotEqual(gated.kind, "review")

    def test_a_review_request_on_a_research_row_skips(self):
        candidate = research_entry(kind="review")
        gated = gates.gate(candidate)
        self.assertEqual(gated.verdict, "skip")
        self.assertIn("review", gated.reason)

    def test_research_ignores_an_open_linked_pull(self):
        candidate = research_entry()
        gated = gates.gate(
            drive.GateInput(
                project=candidate.project,
                request=candidate.request,
                row=research_row(pulls=(gates.open_pull(3),)),
                wait=candidate.wait,
            )
        )
        self.assertEqual(gated.verdict, "research")

    def test_research_ignores_unreadable_pulls(self):
        candidate = research_entry()
        gated = gates.gate(
            drive.GateInput(
                project=candidate.project,
                request=candidate.request,
                row=research_row(pulls=None),
                wait=candidate.wait,
            )
        )
        self.assertEqual(gated.verdict, "research")

    def test_research_sorts_with_new_starts(self):
        ordered = drive.order(
            [
                gates.entry(req=gates.request(number=1, kind="research")),
                gates.entry(req=gates.request(number=2, kind="start")),
            ]
        )
        self.assertEqual(
            [request.kind for request in gates.requests_of(ordered)],
            ["research", "start"],
        )


class TrackerCommentsTest(unittest.TestCase):
    def test_comments_read_bodies_oldest_first(self):
        run = FakeRun()
        run.add(
            "repos/acme/app/issues/7/comments?per_page=100",
            [
                {"body": "first"},
                {"body": "Findings: go"},
            ],
        )
        tracker = ghtracker.GithubTracker(run)
        self.assertEqual(tracker.comments("acme/app", 7), ["first", "Findings: go"])

    def test_comments_refuse_a_malformed_page(self):
        run = FakeRun()
        run.add("repos/acme/app/issues/7/comments?per_page=100", [{"nobody": 1}])
        adapter = ghtracker.GithubTracker(run)
        with self.assertRaises(tracker.TrackerError):
            adapter.comments("acme/app", 7)

    def test_add_label_accepts_the_researched_label(self):
        fake = FakeTracker()
        fake.add_label("acme/app", 7, tracker.RESEARCHED)
        self.assertIn(("add_label", "acme/app", 7, tracker.RESEARCHED), fake.calls)

    def test_add_label_still_refuses_other_labels(self):
        adapter = ghtracker.GithubTracker(FakeRun())
        with self.assertRaises(ValueError):
            adapter.add_label("acme/app", 7, "ready")


class SettleResearchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = board.open_store(os.path.join(self.tmp.name, "board.db"))
        self.store.upsert_item(
            board.Item(
                repo="app",
                number=7,
                kind="issue",
                title="Title 7",
                body="Body",
                state="open",
                author="operator",
                labels=["research"],
                priority="",
                assignees=[],
                url="https://example.com/app/7",
                tracker_updated_at=1000.0,
                extra={},
            ),
            operators=["operator"],
            now=1000.0,
        )
        self.claim = self.store.claim("app", 7, "drive-host-1-x", "host-1", now=1000.0)
        self.out = io.StringIO()
        self.events = []

    def _settle(self, fake, settlements, **over):
        values = {
            "live_units": frozenset(),
            "now": 2000.0,
            "out": self.out,
            "events": self.events,
            "record_event": lambda project, repo, number, body: self.events.append(
                ("research-event", project, repo, number)
            ),
        }
        values.update(over)
        return drive.settle_research(self.store, lambda repo: fake, settlements, **values)

    def test_findings_release_the_claim_add_the_label_and_record(self):
        from korapy import research

        fake = FakeTracker()
        released = self._settle(
            fake,
            [research.Settlement("app", 7, "drive-host-1-x", True, project="blue")],
        )
        self.assertEqual(released, {("app", 7): "drive-host-1-x"})
        held = self.store.claim_of("app", 7)
        self.assertTrue(held is None or not held.agent)
        self.assertIn(("add_label", "app", 7, tracker.RESEARCHED), fake.calls)
        self.assertIn(("research-event", "blue", "app", 7), self.events)

    def test_no_findings_release_without_label_or_event(self):
        from korapy import research

        fake = FakeTracker()
        released = self._settle(fake, [research.Settlement("app", 7, "drive-host-1-x", False)])
        self.assertEqual(released, {("app", 7): "drive-host-1-x"})
        self.assertNotIn(("add_label", "app", 7, tracker.RESEARCHED), fake.calls)
        self.assertEqual([entry for entry in self.events if isinstance(entry, tuple)], [])

    def test_a_failed_label_write_keeps_the_claim(self):
        from korapy import research

        fake = FakeTracker()
        fake.label_errors[("app", 7)] = tracker.TrackerError("down")
        released = self._settle(
            fake, [research.Settlement("app", 7, "drive-host-1-x", True, project="blue")]
        )
        self.assertEqual(released, {})
        self.assertEqual(self.store.claim_of("app", 7).agent, "drive-host-1-x")

    def test_a_live_unit_keeps_the_claim(self):
        from korapy import research

        fake = FakeTracker()
        released = self._settle(
            fake,
            [research.Settlement("app", 7, "drive-host-1-x", True, project="blue")],
            live_units=frozenset({("app", 7)}),
        )
        self.assertEqual(released, {})
        self.assertEqual(self.store.claim_of("app", 7).agent, "drive-host-1-x")

    def test_the_event_goes_to_the_settlement_project(self):
        from korapy import research

        fake = FakeTracker()
        released = self._settle(
            fake,
            [
                research.Settlement(
                    "app",
                    7,
                    "drive-host-1-x",
                    True,
                    findings="Findings: go",
                    project="green",
                )
            ],
        )
        self.assertEqual(released, {("app", 7): "drive-host-1-x"})
        self.assertIn(("research-event", "green", "app", 7), self.events)
        self.assertNotIn(("research-event", "blue", "app", 7), self.events)


class DashboardResearchTest(unittest.TestCase):
    def test_research_events_are_attention_items(self):
        view = dashboard.attention(
            [
                {
                    "id": "a1",
                    "kind": "research",
                    "ref": "app#7",
                    "evidence": "go: the API exists",
                    "state": "Findings: go",
                }
            ]
        )
        self.assertEqual(view["state"], "ready")
        self.assertEqual(view["research"], [{"ref": "app#7", "findings": "go: the API exists"}])
        self.assertIn({"ref": "app#7", "findings": "go: the API exists"}, view["items"])

    def test_other_kinds_keep_their_shape(self):
        view = dashboard.attention(
            [{"id": "a1", "kind": "question", "ref": "app#7", "question": "q?"}]
        )
        self.assertEqual(view["research"], [])
        self.assertEqual(len(view["items"]), 1)

    def test_research_without_evidence_reads_unknown(self):
        view = dashboard.attention([{"id": "a1", "kind": "research", "ref": "app#7"}])
        self.assertEqual(view["research"], [{"ref": "app#7", "findings": "unknown"}])


class ResearchPassTest(unittest.TestCase):
    def test_a_finished_research_row_settles_without_a_reviewer_or_pr(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = board.open_store(os.path.join(tmp.name, "board.db"))
        self.addCleanup(store.close)
        store.upsert_item(
            board.Item(
                repo="app",
                number=7,
                kind="issue",
                title="Title 7",
                body="Body",
                state="open",
                author="operator",
                labels=["research"],
                priority="",
                assignees=[],
                url="https://example.com/app/7",
                tracker_updated_at=1000.0,
                extra={},
            ),
            operators=["operator"],
            now=1000.0,
        )
        store.claim("app", 7, "drive-host-1-x", "host-1", now=1000.0)
        policies_dir = os.path.join(tmp.name, "policies")
        os.makedirs(policies_dir)
        with open(os.path.join(policies_dir, "blue.py"), "w", encoding="utf-8") as handle:
            handle.write(policy_source([]))
        found = [project_named("blue", os.path.join(policies_dir, "blue.py") + ":eligible")]
        fake = FakeTracker()
        fake.comment_bodies[("app", 7)] = ["digging", "Findings: go, the API exists"]
        recorded = []
        out, err = io.StringIO(), io.StringIO()
        code = hermetic_pass(
            store,
            os.path.join(tmp.name, "drive.lock"),
            out,
            err,
            plan=False,
            found=found,
            tracker_factory=lambda repo: fake,
            stale_after=10**12,
            launcher=None,
            agent_states={},
            finished_for={("app", 7): ("drive-host-1-x", "research", "ok")},
            comments_for=drive.research_comments_for(lambda repo: fake),
            record_event=lambda project, repo, number, body: recorded.append(
                (project, repo, number, body)
            ),
            driven={"blue": frozenset({"app"})},
        )
        self.assertEqual(code, 0, err.getvalue())
        held = store.claim_of("app", 7)
        self.assertTrue(held is None or not held.agent)
        self.assertIn(("add_label", "app", 7, tracker.RESEARCHED), fake.calls)
        self.assertIn(("comments", "app", 7), fake.calls)
        self.assertEqual([call for call in fake.calls if call[0] == "pulls_index"], [])
        self.assertEqual(recorded, [("blue", "app", 7, "Findings: go, the API exists")])
        self.assertIn("as researched", out.getvalue())

    def test_a_finished_research_row_without_findings_restarts_clean(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = board.open_store(os.path.join(tmp.name, "board.db"))
        self.addCleanup(store.close)
        store.upsert_item(
            board.Item(
                repo="app",
                number=7,
                kind="issue",
                title="Title 7",
                body="Body",
                state="open",
                author="operator",
                labels=["research"],
                priority="",
                assignees=[],
                url="https://example.com/app/7",
                tracker_updated_at=1000.0,
                extra={},
            ),
            operators=["operator"],
            now=1000.0,
        )
        store.claim("app", 7, "drive-host-1-x", "host-1", now=1000.0)
        policies_dir = os.path.join(tmp.name, "policies")
        os.makedirs(policies_dir)
        with open(os.path.join(policies_dir, "blue.py"), "w", encoding="utf-8") as handle:
            handle.write(policy_source([]))
        found = [project_named("blue", os.path.join(policies_dir, "blue.py") + ":eligible")]
        fake = FakeTracker()
        fake.comment_bodies[("app", 7)] = ["still digging"]
        recorded = []
        out, err = io.StringIO(), io.StringIO()
        code = hermetic_pass(
            store,
            os.path.join(tmp.name, "drive.lock"),
            out,
            err,
            plan=False,
            found=found,
            tracker_factory=lambda repo: fake,
            stale_after=10**12,
            launcher=None,
            agent_states={},
            finished_for={("app", 7): ("drive-host-1-x", "research", "ok")},
            comments_for=drive.research_comments_for(lambda repo: fake),
            record_event=lambda project, repo, number, body: recorded.append(
                (project, repo, number, body)
            ),
            driven={"blue": frozenset({"app"})},
        )
        self.assertEqual(code, 0, err.getvalue())
        held = store.claim_of("app", 7)
        self.assertTrue(held is None or not held.agent)
        self.assertNotIn(("add_label", "app", 7, tracker.RESEARCHED), fake.calls)
        self.assertEqual(recorded, [])
        self.assertIn("open for a fresh start", out.getvalue())

    def test_an_aged_finished_research_claim_settles_instead_of_orphaning(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = board.open_store(os.path.join(tmp.name, "board.db"))
        self.addCleanup(store.close)
        store.upsert_item(
            board.Item(
                repo="app",
                number=7,
                kind="issue",
                title="Title 7",
                body="Body",
                state="open",
                author="operator",
                labels=["research"],
                priority="",
                assignees=[],
                url="https://example.com/app/7",
                tracker_updated_at=1000.0,
                extra={},
            ),
            operators=["operator"],
            now=1000.0,
        )
        store.claim("app", 7, "drive-host-1-x", "host-1", now=1000.0)
        policies_dir = os.path.join(tmp.name, "policies")
        os.makedirs(policies_dir)
        with open(os.path.join(policies_dir, "blue.py"), "w", encoding="utf-8") as handle:
            handle.write(policy_source([]))
        found = [project_named("blue", os.path.join(policies_dir, "blue.py") + ":eligible")]
        fake = FakeTracker()
        fake.comment_bodies[("app", 7)] = ["digging", "Findings: go, the API exists"]
        recorded = []
        out, err = io.StringIO(), io.StringIO()
        code = hermetic_pass(
            store,
            os.path.join(tmp.name, "drive.lock"),
            out,
            err,
            plan=False,
            found=found,
            tracker_factory=lambda repo: fake,
            stale_after=10**12,
            launcher=None,
            agent_states={},
            gone_s=400.0,
            finished_for={("app", 7): ("drive-host-1-x", "research", "ok")},
            comments_for=drive.research_comments_for(lambda repo: fake),
            record_event=lambda project, repo, number, body: recorded.append(
                (project, repo, number, body)
            ),
            driven={"blue": frozenset({"app"})},
        )
        self.assertEqual(code, 0, err.getvalue())
        held = store.claim_of("app", 7)
        self.assertTrue(held is None or not held.agent)
        self.assertIn(("add_label", "app", 7, tracker.RESEARCHED), fake.calls)
        self.assertEqual(recorded, [("blue", "app", 7, "Findings: go, the API exists")])
        self.assertIn("as researched", out.getvalue())
        self.assertNotIn("released dead claim", out.getvalue())

    def test_a_rate_limited_comments_read_stops_the_pass(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = board.open_store(os.path.join(tmp.name, "board.db"))
        self.addCleanup(store.close)
        store.upsert_item(
            board.Item(
                repo="app",
                number=7,
                kind="issue",
                title="Title 7",
                body="Body",
                state="open",
                author="operator",
                labels=["research"],
                priority="",
                assignees=[],
                url="https://example.com/app/7",
                tracker_updated_at=1000.0,
                extra={},
            ),
            operators=["operator"],
            now=1000.0,
        )
        store.claim("app", 7, "drive-host-1-x", "host-1", now=1000.0)
        policies_dir = os.path.join(tmp.name, "policies")
        os.makedirs(policies_dir)
        with open(os.path.join(policies_dir, "blue.py"), "w", encoding="utf-8") as handle:
            handle.write(policy_source([]))
        found = [project_named("blue", os.path.join(policies_dir, "blue.py") + ":eligible")]
        fake = FakeTracker()
        fake.comments_errors[("app", 7)] = tracker.TrackerRateLimited("limited", 30.0)
        out, err = io.StringIO(), io.StringIO()
        code = hermetic_pass(
            store,
            os.path.join(tmp.name, "drive.lock"),
            out,
            err,
            plan=False,
            found=found,
            tracker_factory=lambda repo: fake,
            stale_after=10**12,
            launcher=None,
            agent_states={},
            finished_for={("app", 7): ("drive-host-1-x", "research", "ok")},
            comments_for=drive.research_comments_for(lambda repo: fake),
            driven={"blue": frozenset({"app"})},
        )
        self.assertEqual(code, 3, err.getvalue())
        self.assertIn("rate limited", err.getvalue())
        self.assertEqual(store.claim_of("app", 7).agent, "drive-host-1-x")


class CommentsMapTest(unittest.TestCase):
    def test_research_comments_map_reads_one_call_per_ticket(self):
        fake = FakeTracker()
        fake.comment_bodies[("app", 7)] = ["Findings: go"]
        found = drive.research_comments_map(
            {("app", 7), ("app", 8)}, drive.research_comments_for(lambda repo: fake)
        )
        self.assertEqual(found[("app", 7)], ["Findings: go"])
        self.assertEqual(
            sorted(call for call in fake.calls if call[0] == "comments"),
            [("comments", "app", 7), ("comments", "app", 8)],
        )

    def test_comment_failures_read_as_unreadable_with_the_cause(self):
        fake = FakeTracker()
        fake.comments_errors[("app", 7)] = tracker.TrackerError("down")
        errors = {}
        found = drive.research_comments_map(
            {("app", 7)},
            drive.research_comments_for(lambda repo: fake),
            errors=errors,
        )
        self.assertIsNone(found[("app", 7)])
        self.assertIn("down", errors[("app", 7)])

    def test_a_rate_limited_read_propagates_instead_of_reading_unreadable(self):
        fake = FakeTracker()
        fake.comments_errors[("app", 7)] = tracker.TrackerRateLimited("limited", 30.0)
        with self.assertRaises(tracker.TrackerRateLimited):
            drive.research_comments_map(
                {("app", 7)},
                drive.research_comments_for(lambda repo: fake),
                errors={},
            )


class GovernanceDocTest(unittest.TestCase):
    def test_the_governance_sample_prices_the_research_role(self):
        import re
        import tomllib
        from pathlib import Path

        text = (
            Path(__file__).resolve().parent.parent / "docs" / "09-host-resource-governance.md"
        ).read_text(encoding="utf-8")
        blocks = re.findall(r"```toml\n(.*?)```", text, re.DOTALL)
        samples = [tomllib.loads(block) for block in blocks]
        hosts = [doc for doc in samples if "resources" in doc and "host" in doc["resources"]]
        self.assertTrue(hosts, "no host budget sample in the governance doc")
        roles = hosts[0]["resources"].get("role", {})
        self.assertIn("research", roles)
        self.assertTrue(roles["research"].get("cpu_m", 0) > 0)
        self.assertTrue(roles["research"].get("memory_mb", 0) > 0)


if __name__ == "__main__":
    unittest.main()
