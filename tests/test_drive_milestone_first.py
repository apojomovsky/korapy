"""Optional milestone-first ordering for the dispatch pass (ticket 681).

When `eligibility.milestone_first` is on for a project, the drive pass
post-sorts that project's candidates by milestone (earliest due date,
then version title, unmilestoned and closed-milestone last), by
priority within a milestone, and by (repo, number) after that. The
milestone rides the tracker row from the existing issue listing, so
the pass spends no new GitHub calls; `drive --plan` names it in each
row's reason.
"""

import io
import os
import tempfile
import unittest
from typing import Any

from korapy import admission, board, drive, settings
from korapy import projects as projects_mod
from korapy import tracker as iface
from korapy.eligibility import StartRequest, TicketRow
from korapy.github import tracker as ghtracker
from tests.test_area_locks import board_item
from tests.test_drive import policy_source
from tests.test_drive_claims import drive_pass
from tests.test_tracker import FakeRun, item_payload

DUE_EARLY = "2026-01-02T00:00:00Z"
DUE_LATE = "2026-01-10T00:00:00Z"


def row(number, repo="app", priority="normal", extra=None, **fields):
    values: dict[str, Any] = {
        "repo": repo,
        "number": number,
        "priority": priority,
        "state": "open",
        "extra": dict(extra or {}),
    }
    values.update(fields)
    return TicketRow(**values)


def milestone_extra(title, due=None, closed=False):
    extra = {"milestone": title, "milestone_closed": closed}
    if due is not None:
        extra["milestone_due"] = due
    return extra


def candidate(project, kind, row_, wait=1000.0):
    return drive.GateInput(
        project=project,
        request=StartRequest(repo=row_.repo, number=row_.number, kind=kind),
        row=row_,
        wait=wait,
    )


def requests_of(items):
    """The requests of ordered candidates, asserting none is withdrawn."""
    answered = []
    for item in items:
        assert item.request is not None
        answered.append(item.request)
    return answered


def ordered(entries, **fields):
    return [
        (request.repo, request.number) for request in requests_of(drive.order(entries, **fields))
    ]


class MilestoneOrderTest(unittest.TestCase):
    def test_earlier_due_date_orders_first(self):
        entries = [
            candidate(
                "blue", "start", row(1, priority="urgent", extra=milestone_extra("M2", DUE_LATE))
            ),
            candidate(
                "blue", "start", row(2, priority="backlog", extra=milestone_extra("M1", DUE_EARLY))
            ),
        ]
        self.assertEqual(ordered(entries, milestone_first={"blue"}), [("app", 2), ("app", 1)])

    def test_unmilestoned_sorts_last_despite_higher_priority(self):
        entries = [
            candidate("blue", "start", row(1, priority="urgent")),
            candidate(
                "blue", "start", row(2, priority="backlog", extra=milestone_extra("M1", DUE_EARLY))
            ),
        ]
        self.assertEqual(ordered(entries, milestone_first={"blue"}), [("app", 2), ("app", 1)])

    def test_priority_orders_within_a_milestone(self):
        extra = milestone_extra("M1", DUE_EARLY)
        entries = [
            candidate("blue", "start", row(1, priority="normal", extra=dict(extra))),
            candidate("blue", "start", row(2, priority="urgent", extra=dict(extra))),
        ]
        self.assertEqual(ordered(entries, milestone_first={"blue"}), [("app", 2), ("app", 1)])

    def test_dated_milestone_beats_dateless_despite_lower_priority(self):
        entries = [
            candidate("blue", "start", row(1, priority="urgent", extra=milestone_extra("v9"))),
            candidate(
                "blue", "start", row(2, priority="backlog", extra=milestone_extra("M1", DUE_LATE))
            ),
        ]
        self.assertEqual(ordered(entries, milestone_first={"blue"}), [("app", 2), ("app", 1)])

    def test_dateless_milestones_order_by_version_title(self):
        entries = [
            candidate("blue", "start", row(1, extra=milestone_extra("v10"))),
            candidate("blue", "start", row(2, extra=milestone_extra("v2"))),
        ]
        self.assertEqual(ordered(entries, milestone_first={"blue"}), [("app", 2), ("app", 1)])

    def test_closed_milestone_sorts_with_unmilestoned_by_priority(self):
        entries = [
            candidate(
                "blue",
                "start",
                row(1, priority="urgent", extra=milestone_extra("M0", DUE_EARLY, closed=True)),
            ),
            candidate("blue", "start", row(2, priority="normal")),
            candidate(
                "blue", "start", row(3, priority="backlog", extra=milestone_extra("M1", DUE_LATE))
            ),
        ]
        self.assertEqual(
            ordered(entries, milestone_first={"blue"}),
            [("app", 3), ("app", 1), ("app", 2)],
        )

    def test_equal_milestone_and_priority_breaks_by_repo_number(self):
        extra = milestone_extra("M1", DUE_EARLY)
        entries = [
            candidate("blue", "start", row(9, extra=dict(extra))),
            candidate("blue", "start", row(7, extra=dict(extra))),
        ]
        self.assertEqual(ordered(entries, milestone_first={"blue"}), [("app", 7), ("app", 9)])

    def test_odd_milestone_data_never_raises(self):
        # A non-string title reads as no milestone; a non-string due
        # date degrades to dateless (the title is the milestone, the
        # due only refines its order), so ordering never raises.
        entries = [
            candidate("blue", "start", row(1, priority="urgent", extra={"milestone": 7})),
            candidate("blue", "start", row(2, priority="normal")),
            candidate(
                "blue", "start", row(3, priority="backlog", extra=milestone_extra("M1", DUE_EARLY))
            ),
            candidate(
                "blue",
                "start",
                row(4, priority="high", extra={"milestone": "M9", "milestone_due": 5}),
            ),
        ]
        self.assertEqual(
            ordered(entries, milestone_first={"blue"}),
            [("app", 3), ("app", 4), ("app", 1), ("app", 2)],
        )

    def test_required_kinds_still_group_first(self):
        entries = [
            candidate(
                "blue", "start", row(1, priority="urgent", extra=milestone_extra("M1", DUE_EARLY))
            ),
            candidate("blue", "start", row(2, priority="backlog"), wait=1000.0),
            candidate("blue", "resume", row(2, priority="backlog"), wait=1000.0),
        ]
        [first] = requests_of(drive.order(entries, milestone_first={"blue"})[:1])
        self.assertEqual((first.repo, first.number), ("app", 2))
        self.assertEqual(first.kind, "resume")

    def test_other_projects_keep_todays_order(self):
        entries = [
            candidate(
                "blue", "start", row(1, priority="urgent", extra=milestone_extra("M1", DUE_EARLY))
            ),
            candidate(
                "green", "start", row(2, priority="urgent", extra=milestone_extra("M0", DUE_LATE))
            ),
            candidate("green", "start", row(3, priority="backlog")),
        ]
        self.assertEqual(
            ordered(entries, milestone_first={"blue"}),
            [("app", 1), ("app", 2), ("app", 3)],
        )

    def test_opted_in_backlog_does_not_jump_other_projects_urgent(self):
        # Blue opts in with a backlog ticket on a dated milestone; green
        # stays out with an urgent unmilestoned ticket. Milestone-first
        # reorders only within blue, so green's urgent still leads: the
        # full order matches today's (setting-off) order exactly.
        entries = [
            candidate(
                "blue",
                "start",
                row(1, priority="backlog", extra=milestone_extra("M1", DUE_EARLY)),
            ),
            candidate("green", "start", row(2, priority="urgent")),
        ]
        self.assertEqual(
            ordered(entries, milestone_first={"blue"}),
            [("app", 2), ("app", 1)],
        )
        self.assertEqual(
            ordered(entries, milestone_first={"blue"}),
            ordered(entries, milestone_first=set()),
        )

    def test_milestone_reorders_only_within_the_opted_in_project(self):
        # Blue's own tickets swap by milestone while green's urgent keeps
        # the slot today's order gives it: green never moves.
        entries = [
            candidate("blue", "start", row(1, priority="urgent")),
            candidate(
                "blue",
                "start",
                row(2, priority="backlog", extra=milestone_extra("M1", DUE_EARLY)),
            ),
            candidate("green", "start", row(3, priority="urgent")),
        ]
        self.assertEqual(
            ordered(entries, milestone_first={"blue"}),
            [("app", 2), ("app", 3), ("app", 1)],
        )

    def test_invalid_due_date_sorts_as_dateless(self):
        # "2026-1-2" string-compares before "2026-12-01...", so without
        # validation it would wrongly lead. Fail closed: it reads as no
        # due date and sorts behind every dated milestone.
        entries = [
            candidate(
                "blue",
                "start",
                row(1, priority="normal", extra=milestone_extra("M9", "2026-1-2")),
            ),
            candidate(
                "blue",
                "start",
                row(2, priority="normal", extra=milestone_extra("M1", "2026-12-01T00:00:00Z")),
            ),
        ]
        self.assertEqual(ordered(entries, milestone_first={"blue"}), [("app", 2), ("app", 1)])

    def test_impossible_due_date_sorts_as_dateless(self):
        # Month 13 matches the ISO shape but is not a real date, so it
        # reads as no due date and sorts behind every dated milestone.
        entries = [
            candidate(
                "blue",
                "start",
                row(1, priority="normal", extra=milestone_extra("M9", "2026-13-01T00:00:00Z")),
            ),
            candidate(
                "blue",
                "start",
                row(2, priority="normal", extra=milestone_extra("M1", "2026-12-01T00:00:00Z")),
            ),
        ]
        self.assertEqual(ordered(entries, milestone_first={"blue"}), [("app", 2), ("app", 1)])

    def test_two_opted_in_projects_reorder_within_each_project(self):
        # Both projects opt in, so each re-sorts its own candidates by
        # milestone into the slots today's order gives it: blue's dated
        # ticket and green's dated ticket each move ahead of their own
        # project's unmilestoned urgent, while neither project moves
        # into the other's slots.
        entries = [
            candidate("blue", "start", row(1, priority="urgent")),
            candidate(
                "blue",
                "start",
                row(2, priority="backlog", extra=milestone_extra("M1", DUE_EARLY)),
            ),
            candidate("green", "start", row(3, priority="urgent")),
            candidate(
                "green",
                "start",
                row(4, priority="backlog", extra=milestone_extra("M0", DUE_LATE)),
            ),
        ]
        self.assertEqual(
            ordered(entries, milestone_first={"blue", "green"}),
            [("app", 2), ("app", 4), ("app", 1), ("app", 3)],
        )

    def test_setting_off_ignores_milestones(self):
        entries = [
            candidate("blue", "start", row(1, priority="urgent")),
            candidate(
                "blue", "start", row(2, priority="backlog", extra=milestone_extra("M1", DUE_EARLY))
            ),
        ]
        self.assertEqual(ordered(entries, milestone_first=set()), [("app", 1), ("app", 2)])
        self.assertEqual(
            [(request.repo, request.number) for request in requests_of(drive.order(entries))],
            [("app", 1), ("app", 2)],
        )


class MilestoneSettingTest(unittest.TestCase):
    def test_default_is_false(self):
        self.assertIs(settings.REGISTRY["eligibility.milestone_first"].type, bool)
        self.assertIs(settings.REGISTRY["eligibility.milestone_first"].default, False)

    def test_true_in_the_project_file_opts_in(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory(prefix="korapy-ms-"))
        root = os.path.join(tmp, "ops", "blue")
        os.makedirs(root)
        with open(os.path.join(root, "korapy.toml"), "w", encoding="utf-8") as handle:
            handle.write("[eligibility]\nmilestone_first = true\n")
        project = projects_mod.Project(name="blue", unit_prefix="blue-agent-", root=root)
        self.assertEqual(drive.milestone_first_for([project]), {"blue": True})

    def test_absent_setting_opts_out(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory(prefix="korapy-ms-"))
        root = os.path.join(tmp, "ops", "blue")
        os.makedirs(root)
        with open(os.path.join(root, "korapy.toml"), "w", encoding="utf-8") as handle:
            handle.write("[drive]\nperiod = '*:0/5'\n")
        project = projects_mod.Project(name="blue", unit_prefix="blue-agent-", root=root)
        self.assertEqual(drive.milestone_first_for([project]), {"blue": False})

    def test_broken_project_file_opts_out(self):
        # Like the area configs: a project whose settings fail to load
        # is left out of the map, and the pass reads it as opted out.
        tmp = self.enterContext(tempfile.TemporaryDirectory(prefix="korapy-ms-"))
        root = os.path.join(tmp, "ops", "blue")
        os.makedirs(root)
        with open(os.path.join(root, "korapy.toml"), "w", encoding="utf-8") as handle:
            handle.write("not = [valid toml\n")
        project = projects_mod.Project(name="blue", unit_prefix="blue-agent-", root=root)
        self.assertEqual(drive.milestone_first_for([project]), {})
        self.assertNotIn("blue", drive.milestone_first_for([project]))


class MilestoneInfoTest(unittest.TestCase):
    def test_title_due_and_state_ride_along(self):
        # The contract takes the issue payload, like milestone_of.
        self.assertEqual(
            ghtracker.milestone_info_of(
                {"milestone": {"title": "M1", "due_on": DUE_EARLY, "state": "open"}}
            ),
            ("M1", DUE_EARLY, False),
        )
        self.assertEqual(
            ghtracker.milestone_info_of(
                {"milestone": {"title": "M0", "due_on": None, "state": "closed"}}
            ),
            ("M0", None, True),
        )

    def test_missing_milestone_reads_as_none(self):
        self.assertEqual(ghtracker.milestone_info_of({}), (None, None, False))
        self.assertEqual(ghtracker.milestone_info_of({"milestone": None}), (None, None, False))
        self.assertEqual(
            ghtracker.milestone_info_of({"milestone": {"title": "M1"}}),
            ("M1", None, False),
        )

    def test_malformed_milestone_fails(self):
        for milestone in ({"title": ""}, {"title": 3}, {"title": "M1", "due_on": 5}):
            with self.subTest(milestone=milestone):
                with self.assertRaises(iface.TrackerError):
                    ghtracker.milestone_info_of({"milestone": milestone})

    def test_unknown_state_fails(self):
        # Only a missing, open or closed state reads; anything else is
        # a TrackerError, never a silent guess at open.
        for state in ("bogus", "", "OPEN", "Closed", 3):
            with self.subTest(state=state):
                with self.assertRaises(iface.TrackerError):
                    ghtracker.milestone_info_of({"milestone": {"title": "M1", "state": state}})
        self.assertEqual(
            ghtracker.milestone_info_of({"milestone": {"title": "M1", "state": "open"}}),
            ("M1", None, False),
        )
        self.assertEqual(
            ghtracker.milestone_info_of({"milestone": {"title": "M1", "state": "closed"}}),
            ("M1", None, True),
        )

    def test_blank_title_fails_like_a_missing_title(self):
        # A whitespace-only title syncs no milestone the pass can read
        # (the row reader strips it), so the sync rejects it the same
        # way it rejects every other malformed milestone.
        for milestone in ({"title": " "}, {"title": "   "}, {"title": "\t"}):
            with self.subTest(milestone=milestone):
                with self.assertRaises(iface.TrackerError):
                    ghtracker.milestone_info_of({"milestone": milestone})


class MilestoneSyncTest(unittest.TestCase):
    def test_labels_feed_carries_due_and_state(self):
        run = FakeRun()
        path = (
            "repos/acme/app/issues?state=all&sort=updated"
            f"&direction=asc&per_page={ghtracker.PER_PAGE}"
        )
        run.add(
            path,
            [
                item_payload(
                    1,
                    milestone={"title": "M1", "due_on": DUE_EARLY, "state": "open"},
                ),
                item_payload(2),
            ],
        )
        found = list(ghtracker.GithubTracker(run).items("acme/app"))
        self.assertEqual([item.extra["milestone"] for item in found], ["M1", None])
        self.assertEqual(found[0].extra["milestone_due"], DUE_EARLY)
        self.assertIs(found[0].extra["milestone_closed"], False)
        self.assertEqual([item.extra["milestone_due"] for item in found], [DUE_EARLY, None])
        # An unmilestoned row carries an explicit None flag: the flag
        # rides only with a title, and the None lets the store merge
        # drop a stale stored flag instead of keeping it.
        self.assertIsNone(found[1].extra["milestone_closed"])

    def test_removed_milestone_clears_the_stored_closed_flag(self):
        # A row that loses its milestone must not keep a stale
        # milestone_closed: True: the reading carries None, and the
        # store merge drops the key.
        run = FakeRun()
        path = (
            "repos/acme/app/issues?state=all&sort=updated"
            f"&direction=asc&per_page={ghtracker.PER_PAGE}"
        )
        run.add(
            path,
            [
                item_payload(
                    1,
                    milestone={"title": "M1", "due_on": DUE_EARLY, "state": "closed"},
                ),
                item_payload(2),
            ],
        )
        adapter = ghtracker.GithubTracker(run)
        found = {item.number: item for item in adapter.items("acme/app")}
        self.assertIs(found[1].extra["milestone_closed"], True)
        stored = dict(found[1].extra)
        merged = board.merge_extra(stored, found[2].extra)
        self.assertNotIn("milestone_closed", merged)
        self.assertNotIn("milestone", merged)
        self.assertNotIn("milestone_due", merged)


def milestone_project(tmp, name="blue", milestone_first=True):
    root = os.path.join(tmp, "ops", name)
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "korapy.toml"), "w", encoding="utf-8") as handle:
        handle.write(
            "[eligibility]\nmilestone_first = " + ("true" if milestone_first else "false") + "\n"
        )
    policy_path = os.path.join(tmp, name + ".py")
    with open(policy_path, "w", encoding="utf-8") as handle:
        handle.write(policy_source([("app", 1, "start"), ("app", 2, "start"), ("app", 3, "start")]))
    return projects_mod.Project(
        name=name,
        unit_prefix=name + "-agent-",
        root=root,
        eligibility=policy_path + ":eligible",
    )


class MilestoneGateTest(unittest.TestCase):
    def test_milestone_first_reorders_and_reasons(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory(prefix="korapy-ms-gate-"))
        project = milestone_project(tmp)
        items = [
            board_item("app", 1, priority="urgent"),
            board_item(
                "app",
                2,
                priority="backlog",
                extra=milestone_extra("M1", DUE_EARLY),
            ),
            board_item("app", 3, priority="normal", extra=milestone_extra("M2")),
        ]
        out = io.StringIO()
        gated, problems, _, _, _, _, _ = drive._gate_all(
            items,
            [project],
            out,
            now=2000.0,
            live_set=frozenset(),
            limits=admission.Limits(total=4),
            quota_for={"blue": drive.QuotaSource(chains={}, stale_after=10**12)},
            pool=None,
            ledger_data={},
            read_labels=lambda kind, repo, number: [],
            hold_label="hold",
            driven={"blue": frozenset({"app"})},
        )
        self.assertEqual(problems, [])
        self.assertEqual(
            [(row.repo, row.number) for row in gated],
            [("app", 2), ("app", 3), ("app", 1)],
        )
        self.assertIn("; milestone M1", gated[0].reason)
        self.assertIn("; milestone M2", gated[1].reason)
        self.assertIn("; milestone none", gated[2].reason)

    def test_closed_milestone_names_the_closure_in_the_reason(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory(prefix="korapy-ms-gate-"))
        project = milestone_project(tmp)
        items = [
            board_item(
                "app",
                1,
                priority="normal",
                extra=milestone_extra("M0", DUE_EARLY, closed=True),
            ),
            board_item("app", 2, priority="normal"),
            board_item("app", 3, priority="backlog"),
        ]
        out = io.StringIO()
        gated, problems, _, _, _, _, _ = drive._gate_all(
            items,
            [project],
            out,
            now=2000.0,
            live_set=frozenset(),
            limits=admission.Limits(total=4),
            quota_for={"blue": drive.QuotaSource(chains={}, stale_after=10**12)},
            pool=None,
            ledger_data={},
            read_labels=lambda kind, repo, number: [],
            hold_label="hold",
            driven={"blue": frozenset({"app"})},
        )
        self.assertEqual(problems, [])
        self.assertEqual(
            [(row.repo, row.number) for row in gated],
            [("app", 1), ("app", 2), ("app", 3)],
        )
        self.assertIn("; milestone M0 (closed)", gated[0].reason)
        self.assertIn("; milestone none", gated[1].reason)
        self.assertIn("; milestone none", gated[2].reason)

    def test_host_full_skip_still_names_the_milestone(self):
        # The host-full shortcut skips without gating, but the reason
        # still names the row's milestone for an opted-in project.
        tmp = self.enterContext(tempfile.TemporaryDirectory(prefix="korapy-ms-gate-"))
        project = milestone_project(tmp)
        items = [
            board_item(
                "app",
                1,
                priority="urgent",
                extra=milestone_extra("M1", DUE_EARLY),
            ),
            board_item(
                "app",
                2,
                priority="normal",
                extra=milestone_extra("M2", DUE_LATE),
            ),
            board_item("app", 3, priority="normal"),
        ]
        out = io.StringIO()
        gated, problems, _, _, _, _, _ = drive._gate_all(
            items,
            [project],
            out,
            now=2000.0,
            live_set=frozenset({("blue", "app", 9)}),
            limits=admission.Limits(total=1),
            quota_for={"blue": drive.QuotaSource(chains={}, stale_after=10**12)},
            pool=None,
            ledger_data={},
            read_labels=lambda kind, repo, number: [],
            hold_label="hold",
            driven={"blue": frozenset({"app"})},
        )
        self.assertEqual(problems, [])
        self.assertEqual(
            [(row.repo, row.number) for row in gated],
            [("app", 1), ("app", 2), ("app", 3)],
        )
        self.assertIn("host full", gated[0].reason)
        self.assertIn("new starts stop here", gated[1].reason)
        self.assertIn("new starts stop here", gated[2].reason)
        self.assertIn("; milestone M1", gated[0].reason)
        self.assertIn("; milestone M2", gated[1].reason)
        self.assertIn("; milestone none", gated[2].reason)

    def test_setting_off_is_todays_order_with_plain_reasons(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory(prefix="korapy-ms-gate-"))
        project = milestone_project(tmp, milestone_first=False)
        items = [
            board_item("app", 1, priority="urgent"),
            board_item(
                "app",
                2,
                priority="backlog",
                extra=milestone_extra("M1", DUE_EARLY),
            ),
            board_item("app", 3, priority="normal", extra=milestone_extra("M2")),
        ]
        out = io.StringIO()
        gated, problems, _, _, _, _, _ = drive._gate_all(
            items,
            [project],
            out,
            now=2000.0,
            live_set=frozenset(),
            limits=admission.Limits(total=4),
            quota_for={"blue": drive.QuotaSource(chains={}, stale_after=10**12)},
            pool=None,
            ledger_data={},
            read_labels=lambda kind, repo, number: [],
            hold_label="hold",
            driven={"blue": frozenset({"app"})},
        )
        self.assertEqual(problems, [])
        self.assertEqual(
            [(row.repo, row.number) for row in gated],
            [("app", 1), ("app", 3), ("app", 2)],
        )
        for row in gated:
            self.assertNotIn("milestone", row.reason)


class MilestonePlanTest(unittest.TestCase):
    def test_plan_names_milestones_in_row_reasons(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory(prefix="korapy-ms-plan-"))
        store = board.open_store(os.path.join(tmp, "board.sqlite"))
        self.addCleanup(store.close)
        for number, priority, extra in (
            (1, "urgent", {}),
            (2, "backlog", milestone_extra("M1", DUE_EARLY)),
            (3, "normal", milestone_extra("M2")),
        ):
            store.upsert_item(
                board_item("app", number, priority=priority, extra=extra),
                operators=["operator"],
                now=1000.0,
            )
        project = milestone_project(tmp)
        out, err = io.StringIO(), io.StringIO()
        code = drive_pass(
            store,
            os.path.join(tmp, "drive.lock"),
            out,
            err,
            plan=True,
            found=[project],
            driven={"blue": frozenset({"app"})},
        )
        text = out.getvalue()
        self.assertEqual(code, 0, text + err.getvalue())
        lines = [line for line in text.splitlines() if line.startswith("blue app#")]
        self.assertEqual(len(lines), 3)
        self.assertIn("app#2", lines[0])
        self.assertIn("app#3", lines[1])
        self.assertIn("app#1", lines[2])
        self.assertIn("; milestone M1", lines[0])
        self.assertIn("; milestone M2", lines[1])
        self.assertIn("; milestone none", lines[2])

    def test_invalid_due_date_is_named_in_the_reason(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory(prefix="korapy-ms-plan-"))
        store = board.open_store(os.path.join(tmp, "board.sqlite"))
        self.addCleanup(store.close)
        for number, priority, extra in (
            (1, "normal", milestone_extra("M9", "2026-1-2")),
            (2, "normal", milestone_extra("M1", "2026-12-01T00:00:00Z")),
            (3, "backlog", {}),
        ):
            store.upsert_item(
                board_item("app", number, priority=priority, extra=extra),
                operators=["operator"],
                now=1000.0,
            )
        project = milestone_project(tmp)
        out, err = io.StringIO(), io.StringIO()
        code = drive_pass(
            store,
            os.path.join(tmp, "drive.lock"),
            out,
            err,
            plan=True,
            found=[project],
            driven={"blue": frozenset({"app"})},
        )
        text = out.getvalue()
        self.assertEqual(code, 0, text + err.getvalue())
        lines = [line for line in text.splitlines() if line.startswith("blue app#")]
        self.assertEqual(len(lines), 3)
        self.assertIn("app#2", lines[0])
        self.assertIn("app#1", lines[1])
        self.assertIn("; milestone M9 (bad due 2026-1-2)", lines[1])
        self.assertIn("; milestone none", lines[2])

    def test_plan_without_the_setting_hides_milestones(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory(prefix="korapy-ms-plan-"))
        store = board.open_store(os.path.join(tmp, "board.sqlite"))
        self.addCleanup(store.close)
        for number, priority, extra in (
            (1, "urgent", {}),
            (2, "backlog", milestone_extra("M1", DUE_EARLY)),
            (3, "normal", milestone_extra("M2")),
        ):
            store.upsert_item(
                board_item("app", number, priority=priority, extra=extra),
                operators=["operator"],
                now=1000.0,
            )
        project = milestone_project(tmp, milestone_first=False)
        out, err = io.StringIO(), io.StringIO()
        code = drive_pass(
            store,
            os.path.join(tmp, "drive.lock"),
            out,
            err,
            plan=True,
            found=[project],
            driven={"blue": frozenset({"app"})},
        )
        text = out.getvalue()
        self.assertEqual(code, 0, text + err.getvalue())
        lines = [line for line in text.splitlines() if line.startswith("blue app#")]
        self.assertEqual(len(lines), 3)
        self.assertIn("app#1", lines[0])
        self.assertIn("app#3", lines[1])
        self.assertIn("app#2", lines[2])
        self.assertNotIn("milestone", text)


if __name__ == "__main__":
    unittest.main()
