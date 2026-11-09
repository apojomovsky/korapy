"""Area locks (docs/18, option 1): ticket 1, pure helper plus settings.

Tests use plain TicketRows. No tracker, no network, no subprocess,
no real state or config dir.
"""

import io
import os
import tempfile
import unittest
from typing import Any
from unittest import mock

from korapy import admission, board, drive, drivehub, eligibility, settings
from korapy.eligibility import LinkedPull, StartRequest, TicketRow
from tests.test_drive import policy_source, stored
from tests.test_drive_hub import FakeHub
from tests.test_tracker import FakeTracker, reading


def row(repo="app", number=1, labels=(), agent="", pulls=(), **fields):
    values: dict[str, Any] = {
        "repo": repo,
        "number": number,
        "title": f"Title {number}",
        "labels": list(labels),
        "priority": "high",
        "state": "open",
        "agent": agent,
        "status": "",
        "external": False,
        "pulls": pulls,
        "extra": {},
    }
    values.update(fields)
    return eligibility.TicketRow(**values)


def pull(number=56, state="open"):
    return eligibility.LinkedPull(number=number, state=state)


class AreaSettingsTest(unittest.TestCase):
    def test_prefix_defaults_to_empty_off(self):
        self.assertEqual(settings.REGISTRY["drive.area_prefix"].default, "")

    def test_hold_until_merged_defaults_true(self):
        self.assertEqual(settings.REGISTRY["drive.area_hold_until_merged"].default, True)

    def test_fail_open_defaults_false(self):
        self.assertEqual(settings.REGISTRY["drive.area_fail_open"].default, False)


class AreaPathsTest(unittest.TestCase):
    def test_empty_prefix_disables_everything(self):
        candidate = row(number=12, labels=["area:ci"])
        holder = row(number=34, labels=["area:ci"], agent="drive-abc")
        self.assertIsNone(eligibility.area_hold(candidate, [holder], prefix=""))

    def test_equal_areas_overlap(self):
        candidate = row(number=12, labels=["area:ci"])
        holder = row(number=34, labels=["area:ci"], agent="drive-abc")
        self.assertEqual(
            eligibility.area_hold(candidate, [holder], prefix="area:", hold_until_merged=True),
            "ci",
        )

    def test_parent_holder_blocks_child_candidate(self):
        candidate = row(number=12, labels=["area:ci:suite"])
        holder = row(number=34, labels=["area:ci"], agent="drive-abc")
        self.assertEqual(
            eligibility.area_hold(candidate, [holder], prefix="area:", hold_until_merged=True),
            "ci:suite",
        )

    def test_child_holder_blocks_parent_candidate(self):
        candidate = row(number=12, labels=["area:ci"])
        holder = row(number=34, labels=["area:ci:suite"], agent="drive-abc")
        self.assertEqual(
            eligibility.area_hold(candidate, [holder], prefix="area:", hold_until_merged=True),
            "ci",
        )

    def test_siblings_do_not_overlap(self):
        candidate = row(number=12, labels=["area:ci:suite"])
        holder = row(number=34, labels=["area:ci:lint"], agent="drive-abc")
        self.assertIsNone(
            eligibility.area_hold(candidate, [holder], prefix="area:", hold_until_merged=True)
        )

    def test_multi_area_union(self):
        candidate = row(number=12, labels=["area:docs", "area:ci:suite"])
        holder = row(number=34, labels=["area:ci"], agent="drive-abc")
        self.assertEqual(
            eligibility.area_hold(candidate, [holder], prefix="area:", hold_until_merged=True),
            "ci:suite",
        )

    def test_matching_is_case_sensitive(self):
        candidate = row(number=12, labels=["area:CI"])
        holder = row(number=34, labels=["area:ci"], agent="drive-abc")
        self.assertIsNone(
            eligibility.area_hold(candidate, [holder], prefix="area:", hold_until_merged=True)
        )

    def test_malformed_labels_name_no_area(self):
        candidate = row(number=12, labels=["area:", "area::x", "area:ci"])
        holder = row(number=34, labels=["area:"], agent="drive-abc")
        self.assertIsNone(
            eligibility.area_hold(candidate, [holder], prefix="area:", hold_until_merged=True)
        )
        bare = row(number=34, labels=["area:"], agent="drive-abc")
        self.assertIsNone(
            eligibility.area_hold(candidate, [bare], prefix="area:", hold_until_merged=True)
        )

    def test_board_area_field_is_ignored(self):
        candidate = row(number=12, labels=["area:ci"], extra={"area": "other"})
        holder = row(number=34, labels=["area:ci"], agent="drive-abc", extra={"area": "other"})
        self.assertEqual(
            eligibility.area_hold(candidate, [holder], prefix="area:", hold_until_merged=True),
            "ci",
        )
        other_holder = row(number=34, labels=[], agent="drive-abc", extra={"area": "ci"})
        self.assertIsNone(
            eligibility.area_hold(candidate, [other_holder], prefix="area:", hold_until_merged=True)
        )


class AreaHoldersTest(unittest.TestCase):
    def test_foreign_claim_holds(self):
        candidate = row(number=12, labels=["area:ci"])
        holder = row(number=34, labels=["area:ci"], agent="human-op")
        self.assertEqual(
            eligibility.area_hold(candidate, [holder], prefix="area:", hold_until_merged=True),
            "ci",
        )

    def test_open_pull_holds_when_release_at_merge(self):
        candidate = row(number=12, labels=["area:ci"])
        holder = row(number=34, labels=["area:ci"], pulls=(pull(56, "open"),))
        self.assertEqual(
            eligibility.area_hold(candidate, [holder], prefix="area:", hold_until_merged=True),
            "ci",
        )

    def test_open_pull_releases_when_not_held_to_merge(self):
        candidate = row(number=12, labels=["area:ci"])
        holder = row(number=34, labels=["area:ci"], pulls=(pull(56, "open"),))
        self.assertIsNone(
            eligibility.area_hold(candidate, [holder], prefix="area:", hold_until_merged=False)
        )

    def test_closed_pull_holds_nothing(self):
        candidate = row(number=12, labels=["area:ci"])
        holder = row(number=34, labels=["area:ci"], pulls=(pull(56, "closed"),))
        self.assertIsNone(
            eligibility.area_hold(candidate, [holder], prefix="area:", hold_until_merged=True)
        )

    def test_own_row_never_blocks_itself(self):
        candidate = row(number=12, labels=["area:ci"], agent="drive-abc", pulls=(pull(56, "open"),))
        self.assertIsNone(
            eligibility.area_hold(candidate, [candidate], prefix="area:", hold_until_merged=True)
        )

    def test_no_area_candidate_always_passes(self):
        candidate = row(number=12, labels=[])
        holder = row(number=34, labels=["area:ci"], agent="drive-abc")
        self.assertIsNone(
            eligibility.area_hold(candidate, [holder], prefix="area:", hold_until_merged=True)
        )

    def test_unheld_holder_passes(self):
        candidate = row(number=12, labels=["area:ci"])
        holder = row(number=34, labels=["area:ci"])
        self.assertIsNone(
            eligibility.area_hold(candidate, [holder], prefix="area:", hold_until_merged=True)
        )


def gate_candidate(number=12, kind="start", row_=None):
    return drive.GateInput(
        project="blue",
        request=StartRequest(repo="app", number=number, kind=kind),
        row=row_ if row_ is not None else row(number=number, labels=["area:ci"]),
        wait=1000.0,
    )


def gate(candidate, holders=(), **fields):
    values = {
        "read_labels": lambda kind, repo, number: [],
        "hold_label": "hold",
        "live": frozenset(),
        "limits": admission.Limits(total=4),
        "quota_source": drive.QuotaSource(chains={}, stale_after=10**12),
        "ledger": {},
        "now": 2000.0,
        "area_rows": list(holders),
        "area_prefix": "area:",
        "area_hold_until_merged": True,
        "area_fail_open": False,
    }
    values.update(fields)
    return drive.gate(candidate, **values)


class AreaGateTest(unittest.TestCase):
    def test_claim_held_area_refuses_with_the_section_two_wording(self):
        candidate = gate_candidate()
        holder = row(number=34, labels=["area:ci"], agent="drive-abc")
        gated = gate(candidate, [holder])
        self.assertEqual((gated.verdict, gated.code), ("skip", "area"))
        self.assertEqual(
            gated.reason,
            "app#12: area `ci` held by app#34 (agent `drive-abc`), start refused",
        )

    def test_pull_held_area_refuses_naming_the_pull(self):
        candidate = gate_candidate()
        holder = row(
            number=34,
            labels=["area:ci"],
            pulls=(LinkedPull(number=56, state="open"),),
        )
        gated = gate(candidate, [holder])
        self.assertEqual((gated.verdict, gated.code), ("skip", "area"))
        self.assertEqual(gated.reason, "app#12: area `ci` held by app#34 pull #56, start refused")

    def test_unreadable_pulls_fail_closed_naming_the_outage(self):
        candidate = gate_candidate()
        holder = row(number=34, labels=["area:ci"], pulls=None)
        gated = gate(candidate, [holder])
        self.assertEqual((gated.verdict, gated.code), ("skip", "area"))
        self.assertEqual(
            gated.reason,
            "app#12: area ci possibly held by app#34 (pulls unreadable), start refused",
        )

    def test_fail_open_reads_claim_held_areas_only(self):
        candidate = gate_candidate()
        holder = row(number=34, labels=["area:ci"], pulls=None)
        gated = gate(candidate, [holder], area_fail_open=True)
        self.assertEqual((gated.verdict, gated.code), ("start", ""))

    def test_fail_open_still_refuses_a_live_claim(self):
        candidate = gate_candidate()
        holder = row(number=34, labels=["area:ci"], agent="drive-abc", pulls=None)
        gated = gate(candidate, [holder], area_fail_open=True)
        self.assertEqual((gated.verdict, gated.code), ("skip", "area"))

    def test_open_pull_releases_when_not_held_to_merge(self):
        candidate = gate_candidate()
        holder = row(
            number=34,
            labels=["area:ci"],
            pulls=(LinkedPull(number=56, state="open"),),
        )
        gated = gate(candidate, [holder], area_hold_until_merged=False)
        self.assertEqual(gated.verdict, "start")

    def test_empty_prefix_disables_the_gate_check(self):
        candidate = gate_candidate()
        holder = row(number=34, labels=["area:ci"], agent="drive-abc")
        gated = gate(candidate, [holder], area_prefix="")
        self.assertEqual(gated.verdict, "start")

    def test_no_area_candidate_passes(self):
        candidate = gate_candidate(row_=row(number=12, labels=[]))
        holder = row(number=34, labels=["area:ci"], agent="drive-abc")
        gated = gate(candidate, [holder])
        self.assertEqual(gated.verdict, "start")

    def test_own_row_never_blocks_in_the_gate(self):
        stale_self = row(number=12, labels=["area:ci"], agent="drive-abc")
        gated = gate(gate_candidate(), [stale_self])
        self.assertEqual(gated.verdict, "start")

    def test_empty_pulls_never_fire_a_pull_held_refusal(self):
        candidate = gate_candidate()
        holder = row(number=34, labels=["area:ci"], pulls=())
        gated = gate(candidate, [holder])
        self.assertEqual(gated.verdict, "start")

    def test_claimed_fix_rounds_bypass_the_area_check(self):
        candidate = gate_candidate(
            kind="fix", row_=row(number=12, labels=["area:ci"], agent="drive-abc")
        )
        holder = row(number=34, labels=["area:ci"], agent="drive-xyz")
        gated = gate(candidate, [holder])
        self.assertEqual(gated.verdict, "fix")

    def test_claimed_reviews_bypass_the_area_check(self):
        candidate = gate_candidate(
            kind="review",
            row_=row(number=12, labels=["area:ci"], agent="drive-abc"),
        )
        holder = row(number=34, labels=["area:ci"], agent="drive-xyz")
        gated = gate(candidate, [holder])
        self.assertEqual((gated.verdict, gated.code), ("skip", "claim"))

    def test_claimed_resumes_bypass_the_area_check(self):
        candidate = gate_candidate(
            kind="resume",
            row_=row(number=12, labels=["area:ci"], agent="drive-abc"),
        )
        holder = row(number=34, labels=["area:ci"], agent="drive-xyz")
        gated = gate(candidate, [holder])
        self.assertEqual(gated.verdict, "resume")

    def test_a_reclassified_fresh_start_counts_as_a_start(self):
        candidate = gate_candidate(kind="resume", row_=row(number=12, labels=["area:ci"]))
        holder = row(number=34, labels=["area:ci"], agent="drive-xyz")
        gated = gate(candidate, [holder])
        self.assertEqual((gated.verdict, gated.code, gated.kind), ("skip", "area", "start"))


def board_item(repo="app", number=7, **fields):
    values: dict[str, Any] = {
        "repo": repo,
        "number": number,
        "kind": "issue",
        "title": f"Title {number}",
        "body": "Body",
        "state": "open",
        "author": "operator",
        "labels": ["ready"],
        "priority": "high",
        "assignees": [],
        "url": f"https://example.com/{repo}/{number}",
        "tracker_updated_at": 1000.0,
        "extra": {},
    }
    values.update(fields)
    return board.Item(**values)


def area_project(tmp, name="blue", prefix="area:"):
    from korapy import projects as projects_mod

    root = os.path.join(tmp, "ops", name)
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "korapy.toml"), "w", encoding="utf-8") as handle:
        handle.write(f'[drive]\narea_prefix = "{prefix}"\n')
    policy_path = os.path.join(tmp, name + ".py")
    with open(policy_path, "w", encoding="utf-8") as handle:
        handle.write(policy_source([("app", 12, "start")]))
    return projects_mod.Project(
        name=name,
        unit_prefix=name + "-agent-",
        root=root,
        eligibility=policy_path + ":eligible",
    )


class AreaFetchTest(unittest.TestCase):
    def test_a_held_pull_holder_is_fetched_and_refuses(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory(prefix="korapy-area-fetch-"))
        project = area_project(tmp)
        items = [
            board_item("app", 12, labels=["area:ci"]),
            board_item("app", 34, labels=["area:ci", "hold"]),
            board_item("api", 5, labels=["ready"]),
        ]
        fake = FakeTracker()
        fake.index["app"] = {34: [reading("app", 56, kind="pr", state="open")]}
        out = io.StringIO()
        gated, problems, _, pulls_calls, pulls_limited, pulls_by_key, pull_errors = drive._gate_all(
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
            index_for=drive.pulls_index_for(lambda repo: fake),
            pulls_max_calls=20,
            claims_by_key={},
            driven={"blue": frozenset({"app", "api"})},
        )
        self.assertEqual(problems, [])
        self.assertEqual(len(gated), 1)
        self.assertEqual(gated[0].code, "area")
        self.assertEqual(
            gated[0].reason, "app#12: area `ci` held by app#34 pull #56, start refused"
        )
        listed = sorted(call[1] for call in fake.calls if call[0] == "pulls_index")
        self.assertEqual(listed, ["app"])
        self.assertEqual(pulls_calls, 1)
        self.assertFalse(pulls_limited)
        held = pulls_by_key[("app", 34)]
        self.assertEqual([pull.number for pull in held], [56])
        self.assertEqual(pull_errors, {})

    def test_an_unreadable_holder_listing_fails_closed(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory(prefix="korapy-area-fetch-"))
        project = area_project(tmp)
        items = [
            board_item("app", 12, labels=["area:ci"]),
            board_item("api", 34, labels=["area:ci", "hold"]),
        ]
        fake = FakeTracker()
        fake.index_errors["api"] = "tracker down"
        out = io.StringIO()
        gated, problems, _, _, _, pulls_by_key, pull_errors = drive._gate_all(
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
            index_for=drive.pulls_index_for(lambda repo: fake),
            pulls_max_calls=20,
            claims_by_key={},
            driven={"blue": frozenset({"app", "api"})},
        )
        self.assertEqual(problems, [])
        self.assertEqual(len(gated), 1)
        self.assertEqual(gated[0].code, "area")
        self.assertIn("(pulls unreadable)", gated[0].reason)
        self.assertIsNone(pulls_by_key[("api", 34)])
        self.assertIn("tracker down", pull_errors[("api", 34)])


class FakeLauncher:
    def __init__(self):
        self.calls = []

    def __call__(self, project, repo, number, kind, agent, title, *, now, author=None):
        self.calls.append((project, repo, number, kind, agent, title, author))
        return f"{agent}-unit"


def gated_start(project="blue", repo="app", number=12):
    return drive.Gated(
        project=project,
        repo=repo,
        number=number,
        kind="start",
        verdict="start",
        reason=f"{repo}#{number}: unclaimed, fresh start",
        code="",
    )


def gated_research(project="blue", repo="app", number=12):
    return drive.Gated(
        project=project,
        repo=repo,
        number=number,
        kind="research",
        verdict="research",
        reason=f"{repo}#{number}: unclaimed, fresh research start",
        code="",
    )


def claim_loop(store, gated_rows, area_rows, **fields):
    values = {
        "tracker_factory": None,
        "operators": [],
        "launcher": FakeLauncher(),
        "host": "host-1",
        "now": 2000.0,
        "stale_after": 3600.0,
        "area_rows": area_rows,
        "area_configs": {"blue": ("area:", True, False)},
        "driven": {"blue": frozenset({"app"})},
    }
    values.update(fields)
    launcher = values["launcher"]
    rows, started, blocked, launched = drive._claim_and_launch(store, gated_rows, **values)
    return rows, started, launcher


class InPassAreaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="korapy-area-inpass-")
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "board.sqlite")

    def test_the_second_same_area_start_refuses_and_mints_no_claim(self):
        store = stored(self.db, [("app", 12, "open"), ("app", 34, "open")])
        self.addCleanup(store.close)
        labels = ("area:ci",)
        area_rows = {
            ("blue", "app", 12): row(number=12, labels=labels),
            ("blue", "app", 34): row(number=34, labels=labels),
        }
        launcher = FakeLauncher()
        rows, started, _ = claim_loop(
            store,
            [gated_start(number=12), gated_start(number=34)],
            area_rows,
            launcher=launcher,
        )
        self.assertEqual(started, 1)
        self.assertEqual(rows[0].verdict, "start")
        self.assertEqual((rows[1].verdict, rows[1].code, rows[1].kind), ("skip", "area", "start"))
        self.assertRegex(
            rows[1].reason,
            r"^app#34: area `ci` held by app#12 \(agent `drive-[0-9a-f]+-12`\), "
            r"start refused$",
        )
        self.assertEqual([call[2] for call in launcher.calls], [12])
        self.assertIsNotNone(store.claim_of("app", 12))
        self.assertIsNone(store.claim_of("app", 34))

    def test_different_areas_start_side_by_side(self):
        store = stored(self.db, [("app", 12, "open"), ("app", 34, "open")])
        self.addCleanup(store.close)
        area_rows = {
            ("blue", "app", 12): row(number=12, labels=("area:ci",)),
            ("blue", "app", 34): row(number=34, labels=("area:docs",)),
        }
        launcher = FakeLauncher()
        rows, started, _ = claim_loop(
            store,
            [gated_start(number=12), gated_start(number=34)],
            area_rows,
            launcher=launcher,
        )
        self.assertEqual(started, 2)
        self.assertEqual([item.verdict for item in rows], ["start", "start"])

    def test_the_refused_start_grants_no_hub_claim(self):
        store = stored(self.db, [("app", 12, "open"), ("app", 34, "open")])
        self.addCleanup(store.close)
        labels = ("area:ci",)
        area_rows = {
            ("blue", "app", 12): row(number=12, labels=labels),
            ("blue", "app", 34): row(number=34, labels=labels),
        }
        fake_hub = FakeHub()
        mode = drivehub.HubMode(fake_hub, "host-1")
        rows, started, _ = claim_loop(
            store, [gated_start(number=12), gated_start(number=34)], area_rows, hub=mode
        )
        self.assertEqual(started, 1)
        self.assertEqual((rows[1].verdict, rows[1].code), ("skip", "area"))
        proposes = [call for call in fake_hub.calls if call[0] == "propose"]
        self.assertEqual(len(proposes), 1)
        self.assertEqual(proposes[0][1:3], ("app", 12))

    def test_a_research_start_bypasses_the_in_pass_area_lock(self):
        store = stored(self.db, [("app", 12, "open"), ("app", 34, "open")])
        self.addCleanup(store.close)
        labels = ("area:ci",)
        area_rows = {
            ("blue", "app", 12): row(number=12, labels=labels),
            ("blue", "app", 34): row(number=34, labels=labels, agent="drive-holder"),
        }
        launcher = FakeLauncher()
        rows, started, _ = claim_loop(
            store, [gated_research(number=12)], area_rows, launcher=launcher
        )
        self.assertEqual(started, 1)
        self.assertEqual((rows[0].verdict, rows[0].code), ("research", ""))
        self.assertEqual(rows[0].kind, "research")
        self.assertEqual([call[2:4] for call in launcher.calls], [(12, "research")])
        self.assertIsNotNone(store.claim_of("app", 12))

    def test_a_pull_held_holder_refuses_the_second_start(self):
        store = stored(self.db, [("app", 12, "open"), ("app", 34, "open")])
        self.addCleanup(store.close)
        labels = ("area:ci",)
        area_rows = {
            ("blue", "app", 12): row(number=12, labels=labels, pulls=(pull(56, "open"),)),
            ("blue", "app", 34): row(number=34, labels=labels),
        }
        launcher = FakeLauncher()
        rows, started, _ = claim_loop(
            store,
            [gated_start(number=12), gated_start(number=34)],
            area_rows,
            launcher=launcher,
        )
        self.assertEqual(started, 1)
        self.assertEqual(rows[0].verdict, "start")
        self.assertEqual((rows[1].verdict, rows[1].code, rows[1].kind), ("skip", "area", "start"))
        self.assertEqual(rows[1].reason, "app#34: area `ci` held by app#12 pull #56, start refused")
        self.assertEqual([call[2] for call in launcher.calls], [12])
        self.assertIsNone(store.claim_of("app", 34))

    def test_a_missing_in_pass_row_refuses_fail_closed(self):
        store = stored(self.db, [("app", 12, "open")])
        self.addCleanup(store.close)
        launcher = FakeLauncher()
        rows, started, _ = claim_loop(store, [gated_start(number=12)], {})
        self.assertEqual(started, 0)
        self.assertEqual((rows[0].verdict, rows[0].code), ("skip", "area"))
        self.assertEqual(rows[0].kind, "start")
        self.assertEqual([call for call in launcher.calls], [])
        self.assertIsNone(store.claim_of("app", 12))


class AreaPlanTest(unittest.TestCase):
    def test_the_plan_names_claim_held_refusals(self):
        from tests.test_drive_claims import drive_pass

        tmp = self.enterContext(tempfile.TemporaryDirectory(prefix="korapy-area-plan-"))
        store = stored(os.path.join(tmp, "board.sqlite"), [("app", 12, "open")])
        self.addCleanup(store.close)
        for number, labels in ((12, ["area:ci"]), (34, ["area:ci"])):
            store.upsert_item(
                board_item("app", number, labels=labels),
                operators=["operator"],
                now=1000.0,
            )
        store.claim("app", 34, "drive-holder", "host-1", now=1000.0)
        project = area_project(tmp)
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
        self.assertIn(
            "app#12: area `ci` held by app#34 (agent `drive-holder`), start refused", text
        )


class AreaJournalTest(unittest.TestCase):
    def test_one_refusal_lands_one_area_count(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory(prefix="korapy-area-journal-"))
        journal = os.path.join(tmp, "journal.jsonl")
        day = os.path.join(tmp, "drive.starts.json")
        pause = os.path.join(tmp, "drive.pause")
        lock = os.path.join(tmp, "drive.lock")
        import json

        with open(journal, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"event": "pass_start", "at": 2000.0, "host": "host-1"}) + "\n")
            handle.write(
                json.dumps(
                    {
                        "event": "skip",
                        "at": 2000.0,
                        "project": "blue",
                        "ticket": "app#12",
                        "code": "area",
                        "reason": "app#12: area `ci` held by app#34, start refused",
                    }
                )
                + "\n"
            )
        view = drive.drive_status(
            now=2000.0,
            max_starts=50,
            journal_path=journal,
            pause_path=pause,
            day_path=day,
            lock_path=lock,
        )
        self.assertEqual(view["skipped_by_reason"], {"area": 1})
        self.assertEqual(view["waits"][0]["code"], "area")


class AreaDoctorTest(unittest.TestCase):
    def test_no_reading_means_no_rows(self):
        from korapy import doctor

        self.assertEqual(doctor._check_area_sites(None), [])

    def test_an_unmapped_project_with_no_prefix_is_quiet(self):
        from korapy import doctor

        sites = [doctor.AreaSite(project="blue", prefix="", area_column=False, unreadable=0)]
        self.assertEqual(doctor._check_area_sites(sites), [])

    def test_a_mapped_column_with_labels_warns(self):
        from korapy import doctor

        sites = [doctor.AreaSite(project="blue", prefix="area:", area_column=True, unreadable=0)]
        found = doctor._check_area_sites(sites)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].status, "warn")
        self.assertIn("blue", found[0].detail)

    def test_repeated_unreadable_refusals_warn(self):
        from korapy import doctor

        sites = [doctor.AreaSite(project="blue", prefix="area:", area_column=False, unreadable=3)]
        found = doctor._check_area_sites(sites)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].status, "warn")
        self.assertIn("pulls unreadable", found[0].detail)

    def test_a_single_unreadable_refusal_is_quiet(self):
        from korapy import doctor

        sites = [doctor.AreaSite(project="blue", prefix="area:", area_column=False, unreadable=1)]
        self.assertEqual(doctor._check_area_sites(sites), [])

    def test_a_corrupt_journal_line_keeps_the_other_counts(self):
        import json

        from korapy import doctor

        tmp = self.enterContext(tempfile.TemporaryDirectory(prefix="korapy-area-report-"))
        journal = os.path.join(tmp, "journal.jsonl")
        with open(journal, "w", encoding="utf-8") as handle:
            handle.write("{not json}\n")
            handle.write(
                json.dumps(
                    {
                        "event": "skip",
                        "code": "area",
                        "project": "blue",
                        "reason": (
                            "app#12: area ci possibly held by app#34 "
                            "(pulls unreadable), start refused"
                        ),
                    }
                )
                + "\n"
            )
        project = mock.Mock()
        project.name = "blue"
        project.root = tmp
        self.enterContext(mock.patch.object(doctor.projects, "load_all", return_value=[project]))
        self.enterContext(mock.patch.object(doctor.drive, "journal_path", return_value=journal))
        self.enterContext(
            mock.patch.object(doctor.settings, "load", return_value={"drive.area_prefix": "area:"})
        )
        self.enterContext(mock.patch.object(doctor.settings, "repo_names", return_value=[]))
        sites = doctor.load_area_report()
        assert sites is not None
        self.assertEqual(len(sites), 1)
        self.assertEqual(sites[0].unreadable, 1)


if __name__ == "__main__":
    unittest.main()
