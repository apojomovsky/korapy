"""Ticket 651: data panels behind new snapshot fields.

Pure-builder tests for `korapy.dashboard`: one reader per fact, and
every panel's missing-source shape. No state dirs, no network.
"""

import unittest

from korapy import dashboard

T0 = 1_800_000_000.0


def entry(note="", last_seen=None, outcome=None, ended_at=None, role=None):
    return {
        "id": "a1",
        "role": role,
        "note": note,
        "last_seen": T0 - 10 if last_seen is None else last_seen,
        "started_at": T0 - 100,
        "ended_at": ended_at,
        "outcome": outcome,
    }


class AgentState(unittest.TestCase):
    def test_finished_failed_outcomes(self):
        ok = dashboard.agent_state(entry(ended_at=T0 - 5, outcome="ok"), "finished", T0)
        failed = dashboard.agent_state(entry(ended_at=T0 - 5, outcome="failed"), "finished", T0)
        self.assertEqual(ok["derived"], "finished")
        self.assertEqual(failed["derived"], "failed")

    def test_stale_reads_stalled(self):
        self.assertEqual(dashboard.agent_state(entry(), "stale", T0)["derived"], "stalled")

    def test_note_signals_win_in_order(self):
        self.assertEqual(
            dashboard.agent_state(entry(note="blocked on review"), "live", T0)["derived"],
            "blocked",
        )
        self.assertEqual(
            dashboard.agent_state(entry(note="quick question?"), "live", T0)["derived"],
            "asking",
        )
        self.assertEqual(
            dashboard.agent_state(entry(note="running tests"), "live", T0)["derived"],
            "testing",
        )
        self.assertEqual(
            dashboard.agent_state(entry(note="building wheels"), "live", T0)["derived"],
            "building",
        )
        self.assertEqual(
            dashboard.agent_state(entry(note="needs review"), "live", T0)["derived"],
            "review",
        )
        self.assertEqual(
            dashboard.agent_state(entry(note="doing things"), "live", T0)["derived"],
            "working",
        )

    def test_substrings_are_not_signals(self):
        for note in (
            "task sync running",
            "latest deploy",
            "rebuild image",
            "contest results",
        ):
            got = dashboard.agent_state(entry(note=note), "live", T0)
            self.assertEqual(got["derived"], "working", note)

    def test_idle_when_note_empty_and_old(self):
        got = dashboard.agent_state(entry(note="", last_seen=T0 - 400), "live", T0)
        self.assertEqual(got["derived"], "idle")

    def test_idle_s_and_doing(self):
        got = dashboard.agent_state(entry(note="  hello world  ", last_seen=T0 - 42), "live", T0)
        self.assertEqual(got["idle_s"], 42)
        self.assertEqual(got["doing"], "hello world")

    def test_missing_note_reads_unknown(self):
        got = dashboard.agent_state(entry(note=None), "live", T0)
        self.assertEqual(got["doing"], "unknown")

    def test_unreadable_time_reads_unknown(self):
        got = dashboard.agent_state(entry(last_seen="soon"), "live", T0)
        self.assertEqual(got["idle_s"], "unknown")

    def test_urgency_orders_blocked_before_finished(self):
        rows = [
            {"derived": "finished", "project": "", "repo": "", "number": None, "id": "b"},
            {"derived": "blocked", "project": "", "repo": "", "number": None, "id": "a"},
        ]
        self.assertEqual([row["id"] for row in sorted(rows, key=dashboard.sort_key)], ["a", "b"])


class HonestQuota(unittest.TestCase):
    def test_measured_source_when_windows(self):
        got = dashboard.quota_honesty(
            {"state": "ok", "windows": {"w": {"used_pct": 10.0}}, "error": None}
        )
        self.assertEqual(got["source"], "measured")
        self.assertFalse(got["read_failed"])

    def test_stale_windows_read_estimate(self):
        got = dashboard.quota_honesty(
            {
                "state": "ok",
                "stale": True,
                "windows": {"w": {"used_pct": 10.0}},
                "read_at": T0 - 10,
                "error": None,
            }
        )
        self.assertEqual(got["source"], "estimate")

    def test_windows_carry_the_provider_source(self):
        view = dashboard.with_honest_quota(
            {
                "now": T0,
                "providers": {
                    "p": {
                        "state": "ok",
                        "stale": False,
                        "windows": {"w": {"used_pct": 10.0, "resets_at": None}},
                        "read_at": T0,
                        "error": None,
                    }
                },
            }
        )
        self.assertEqual(view["providers"]["p"]["windows"]["w"]["source"], "measured")

    def test_two_providers_keep_their_own_source(self):
        view = dashboard.with_honest_quota(
            {
                "now": T0,
                "providers": {
                    "fresh": {
                        "state": "ok",
                        "stale": False,
                        "windows": {"w": {"used_pct": 10.0, "resets_at": None}},
                        "read_at": T0,
                        "error": None,
                    },
                    "old": {
                        "state": "ok",
                        "stale": True,
                        "windows": {"w": {"used_pct": 10.0, "resets_at": None}},
                        "read_at": T0 - 9999,
                        "error": None,
                    },
                },
            }
        )
        self.assertEqual(view["providers"]["fresh"]["source"], "measured")
        self.assertEqual(view["providers"]["old"]["source"], "estimate")

    def test_estimate_when_no_windows(self):
        got = dashboard.quota_honesty(
            {"state": "unknown", "windows": None, "error": None, "read_at": T0 - 10}
        )
        self.assertEqual(got["source"], "estimate")

    def test_bare_ok_state_reads_unknown(self):
        got = dashboard.quota_honesty({"state": "ok"})
        self.assertEqual(got["source"], "unknown")
        self.assertEqual(got["error_trimmed"], "unknown")

    def test_ok_with_a_reading_reads_measured(self):
        got = dashboard.quota_honesty(
            {"state": "ok", "windows": None, "error": None, "read_at": T0 - 10}
        )
        self.assertEqual(got["source"], "measured")

    def test_unknown_when_no_provider(self):
        got = dashboard.quota_honesty(None)
        self.assertEqual(got["source"], "unknown")
        self.assertEqual(got["error_trimmed"], "unknown")

    def test_error_trimmed_and_flagged(self):
        got = dashboard.quota_honesty({"state": "unknown", "windows": None, "error": "x" * 500})
        self.assertTrue(got["read_failed"])
        self.assertEqual(len(got["error_trimmed"]), dashboard.ERROR_TRIM)


class Attention(unittest.TestCase):
    def test_partitions_questions_and_merges(self):
        events = [
            {"kind": "question", "ref": "r#1", "question": "q?"},
            {"kind": "held-merge", "ref": "r#2", "reason": "checks"},
            {"kind": "coordinator-review", "ref": "r#3"},
        ]
        got = dashboard.attention(events)
        self.assertEqual(got["state"], "ready")
        self.assertEqual(len(got["needs_decision"]), 1)
        self.assertEqual(len(got["ready_to_merge"]), 1)
        self.assertEqual(got["coordinator_handled"], 1)

    def test_two_questions_both_list_in_order(self):
        events = [
            {"kind": "question", "ref": "r#1", "question": "first?"},
            {"kind": "question", "ref": "r#2", "question": "second?"},
        ]
        got = dashboard.attention(events)
        self.assertEqual([item["ref"] for item in got["needs_decision"]], ["r#1", "r#2"])
        self.assertEqual(len(got["items"]), 2)

    def test_missing_source_reads_unknown(self):
        got = dashboard.attention(None)
        self.assertEqual(got["state"], "unknown")
        self.assertEqual(got["needs_decision"], "unknown")

    def test_broken_source_reads_error(self):
        got = dashboard.attention(ValueError("boom"))
        self.assertEqual(got["state"], "error")

    def test_wrong_shaped_source_reads_error_not_ready(self):
        for bad in ("oops", {"needs_decision": []}, 42, ["oops"]):
            got = dashboard.attention(bad)
            self.assertEqual(got["state"], "error", repr(bad))


class PlanBand(unittest.TestCase):
    def test_unknown_when_drive_missing(self):
        got = dashboard.plan_projection(None, T0)
        self.assertEqual(got["state"], "unknown")
        self.assertEqual(got["next"], "unknown")

    def test_names_next_pass_from_last_pass(self):
        drive = {"last_pass_at": T0 - 100, "starts_remaining": 2, "skipped_by_reason": {}}
        got = dashboard.plan_projection(drive, T0)
        self.assertEqual(got["state"], "ready")
        self.assertTrue(got["next_pass_in_s"] is not None and got["next_pass_in_s"] >= 0)
        self.assertTrue(len(got["next"]) >= 1)

    def test_waits_name_skip_reasons(self):
        drive = {
            "last_pass_at": T0 - 100,
            "starts_remaining": 2,
            "skipped_by_reason": {"quota": 3},
        }
        got = dashboard.plan_projection(drive, T0)
        self.assertIn("quota", got["waits"])

    def test_several_skips_sort_and_cap_the_next_list(self):
        drive = {
            "last_pass_at": T0 - 100,
            "starts_remaining": 5,
            "skipped_by_reason": {"quota": 3, "cap": 1, "hold": 2, "lock": 1},
        }
        got = dashboard.plan_projection(drive, T0)
        self.assertEqual(got["waits"], ["cap", "hold", "lock", "quota"])
        self.assertEqual(len(got["next"]), 3)
        self.assertEqual([item["waits"] for item in got["next"]], ["cap", "hold", "lock"])

    def test_no_starts_left_means_empty_next(self):
        drive = {
            "last_pass_at": T0 - 100,
            "starts_remaining": 0,
            "skipped_by_reason": {"quota": 3},
        }
        got = dashboard.plan_projection(drive, T0)
        self.assertEqual(got["state"], "ready")
        self.assertEqual(got["next"], [])

    def test_wrong_shaped_source_reads_error_not_ready(self):
        for bad in ("oops", ["last_pass_at"], 42):
            got = dashboard.plan_projection(bad, T0)
            self.assertEqual(got["state"], "error", repr(bad))

    def test_waits_name_ticket_and_reason(self):
        drive = {
            "last_pass_at": T0 - 100,
            "starts_remaining": 2,
            "skipped_by_reason": {"cap": 1},
            "waits": [
                {
                    "ticket": "app#7",
                    "code": "cap",
                    "reason": "app#7: day cap reached (1 of 1 today), not started",
                },
            ],
        }
        got = dashboard.plan_projection(drive, T0)
        self.assertEqual(got["state"], "ready")
        self.assertEqual(
            got["next"][0],
            {
                "what": "start",
                "ticket": "app#7",
                "waits": "day cap reached (1 of 1 today), not started",
                "count": 1,
            },
        )

    def test_identical_waits_collapse_with_a_count(self):
        drive = {
            "last_pass_at": T0 - 100,
            "starts_remaining": 5,
            "skipped_by_reason": {"cap": 2},
            "waits": [
                {
                    "ticket": "app#7",
                    "code": "cap",
                    "reason": "app#7: day cap reached (1 of 1 today), not started",
                },
                {
                    "ticket": "app#9",
                    "code": "cap",
                    "reason": "app#9: day cap reached (1 of 1 today), not started",
                },
            ],
        }
        got = dashboard.plan_projection(drive, T0)
        rows = [row for row in got["next"] if row["ticket"] is not None]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ticket"], "app#7")
        self.assertEqual(rows[0]["count"], 2)

    def test_free_slots_start_next_pass(self):
        drive = {
            "last_pass_at": T0 - 100,
            "starts_remaining": 3,
            "skipped_by_reason": {"cap": 1},
            "waits": [
                {
                    "ticket": "app#7",
                    "code": "cap",
                    "reason": "app#7: day cap reached (1 of 1 today), not started",
                },
            ],
        }
        got = dashboard.plan_projection(drive, T0)
        fillers = [row for row in got["next"] if row["ticket"] is None]
        self.assertEqual(len(fillers), 1)
        self.assertEqual(fillers[0]["waits"], "slot free")
        self.assertEqual(fillers[0]["count"], 2)

    def test_wrong_shaped_waits_read_error(self):
        for bad in ("oops", ["app#7"]):
            drive = {
                "last_pass_at": T0 - 100,
                "starts_remaining": 2,
                "skipped_by_reason": {},
                "waits": bad,
            }
            got = dashboard.plan_projection(drive, T0)
            self.assertEqual(got["state"], "error", repr(bad))

    def test_wrong_shaped_counts_read_error_not_empty(self):
        drive = {
            "last_pass_at": T0 - 100,
            "starts_remaining": "lots",
            "skipped_by_reason": {"quota": 3},
        }
        self.assertEqual(dashboard.plan_projection(drive, T0)["state"], "error")
        drive = {
            "last_pass_at": T0 - 100,
            "starts_remaining": 2,
            "skipped_by_reason": ["quota"],
        }
        self.assertEqual(dashboard.plan_projection(drive, T0)["state"], "error")


class LocalTime(unittest.TestCase):
    def test_offset_is_an_int_and_iso_carries_it(self):
        self.assertIsInstance(dashboard.local_offset_s(T0), int)
        iso = dashboard.now_iso(T0)
        self.assertIsInstance(iso, str)
        self.assertRegex(iso, r"[+-]\d{2}:?\d{2}")


class CadenceAgreement(unittest.TestCase):
    """The plan countdown repeats the drive timer's cadence: one fact, one place.

    `drive.TIMER_ON_CALENDAR` owns the installed cadence; `PLAN_PASS_S`
    restates it in seconds, so this test pins the copies to agree.
    """

    def test_plan_pass_matches_the_drive_timer(self):
        from korapy import drive

        self.assertEqual(drive.TIMER_ON_CALENDAR, "*:0/5")
        self.assertEqual(dashboard.PLAN_PASS_S, 300)


class BuildPanels(unittest.TestCase):
    """The new fields inside a built document: one reader, one test."""

    def build(self, **overrides):
        from korapy import snapshot
        from tests.test_snapshot import (
            HOST,
            config,
            doc_limits,
            doc_pressure,
            quota_view,
            usage,
            view,
        )

        params = {
            "now": T0,
            "host": HOST,
            "registry_view": view(),
            "quota_view": quota_view(),
            "settings": config(),
            "usage_fn": usage,
            "drive_view": None,
            "resources_view": snapshot.resources_section(
                view(),
                HOST,
                doc_limits(),
                doc_pressure(),
                [],
                lambda *args: None,
                live_by_owner={},
                project_names=set(),
            ),
            "events_view": None,
        }
        params.update(overrides)
        return snapshot.build(**params)

    def test_attention_ready_lists_questions_and_merges(self):
        doc = self.build(
            events_view=[
                {"kind": "question", "ref": "r#1", "question": "ship it?"},
                {"kind": "held-merge", "ref": "r#2", "reason": "checks"},
                {"kind": "coordinator-review", "ref": "r#3"},
            ]
        )
        attention = doc["attention"]
        self.assertEqual(attention["state"], "ready")
        self.assertEqual(len(attention["needs_decision"]), 1)
        self.assertIn("ship it?", attention["needs_decision"][0]["question"])
        self.assertEqual(len(attention["ready_to_merge"]), 1)
        self.assertEqual(attention["coordinator_handled"], 1)

    def test_attention_unknown_when_no_queue(self):
        self.assertEqual(self.build()["attention"]["state"], "unknown")

    def test_attention_error_when_queue_broken(self):
        doc = self.build(events_view=ValueError("queue gone"))
        self.assertEqual(doc["attention"]["state"], "error")

    def test_plan_unknown_when_no_drive(self):
        plan = self.build()["plan"]
        self.assertEqual(plan["state"], "unknown")
        self.assertEqual(plan["next"], "unknown")

    def test_plan_ready_names_waits_and_next_pass(self):
        doc = self.build(
            drive_view={
                "last_pass_at": T0 - 100,
                "starts_remaining": 2,
                "skipped_by_reason": {"quota": 1},
            }
        )
        plan = doc["plan"]
        self.assertEqual(plan["state"], "ready")
        self.assertIn("quota", plan["waits"])
        self.assertTrue(isinstance(plan["next_pass_in_s"], int) and plan["next_pass_in_s"] >= 0)

    def test_quota_honesty_merged_per_provider(self):
        provider = self.build()["quota"]["providers"]["provider-a"]
        self.assertEqual(provider["source"], "measured")
        self.assertFalse(provider["read_failed"])

    def test_header_carries_the_local_offset(self):
        header = self.build()["fleet_header"]
        self.assertIsInstance(header["tz_offset_s"], int)
        self.assertIsInstance(header["now_iso"], str)


if __name__ == "__main__":
    unittest.main()
