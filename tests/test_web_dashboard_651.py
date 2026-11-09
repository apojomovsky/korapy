"""Ticket 651: data panels over new snapshot fields.

Source-discipline tests over the packaged `korapy/web/` files: every
number on the new panels traces to a snapshot field, and every panel
names what it shows when its source is missing. No JS runner, no
network beyond loopback, no real state dirs.
"""

import re
import unittest
from importlib import resources

from korapy import snapshot


def web_text(name):
    return resources.files("korapy").joinpath(f"web/{name}").read_text(encoding="utf-8")


def function_body(source, name):
    match = re.search(
        r"(?:export )?function " + re.escape(name) + r"\(.*?^\}",
        source,
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None, "no function " + name
    return match.group(0)


class DerivedAgentState(unittest.TestCase):
    """One derived state, idle time and doing intent per row, plus a sort menu."""

    def test_row_names_the_derived_fact(self):
        body = function_body(web_text("views.js"), "agentRow")
        for bit in ("row.derived", "row.idle_s", "row.doing", "formatIdle("):
            self.assertIn(bit, body)

    def test_missing_facts_read_unknown(self):
        body = function_body(web_text("views.js"), "agentRow")
        self.assertGreaterEqual(body.count("unknown"), 3)

    def test_doing_replaces_the_note_header(self):
        body = function_body(web_text("views.js"), "agentTable")
        self.assertIn("doing", body)

    def test_sort_menu_is_exported_and_pinned(self):
        views = web_text("views.js")
        for name in (
            "export const AGENT_SORT_MODES",
            "export function setAgentSort(",
            "export function getAgentSort(",
            "export function sortAgentRows(",
        ):
            self.assertIn(name, views)
        body = function_body(views, "agentsSection")
        self.assertIn("agentSortMenu()", body)
        self.assertIn("sortAgentRows(", body)
        self.assertIn('"agent-sort"', views)

    def test_shell_applies_the_sort_choice(self):
        app = web_text("app.js")
        self.assertIn("setAgentSort(", app)
        self.assertIn("agent-sort", app)

    def test_project_mode_reuses_the_server_order(self):
        body = function_body(web_text("views.js"), "sortAgentRows")
        self.assertNotIn("repo", body)


class AttentionBand(unittest.TestCase):
    """Needs-decision with the question, ready merges, coordinator fold-up."""

    def test_attention_items_cover_the_panel(self):
        body = function_body(web_text("views.js"), "attentionItems")
        for bit in (
            "needs decision",
            "ready to merge",
            "research findings",
            "unknown",
            "could not be read",
        ):
            self.assertIn(bit, body)

    def test_coordinator_share_folds_up_dim(self):
        self.assertIn("export function coordinatorHandled(", web_text("views.js"))
        body = function_body(web_text("views.js"), "attentionCard")
        self.assertIn("handled by the coordinator (", body)

    def test_empty_state_still_says_nothing_needs_you(self):
        self.assertIn("nothing needs you", web_text("views.js"))

    def test_no_doctor_row_without_a_reader(self):
        views = web_text("views.js")
        self.assertNotIn("doctor_failing", views)
        body = function_body(views, "attentionCard")
        self.assertNotIn("doctor checks", body)


class PlanBand(unittest.TestCase):
    """The next starts with their waits, and the empty-fleet next pass."""

    def test_plan_items_are_exported(self):
        self.assertIn("export function planItems(", web_text("views.js"))

    def test_plan_names_waits_and_the_next_pass(self):
        views = web_text("views.js")
        body = function_body(views, "planCard")
        for bit in ("planItems(doc)", "next pass in ", "unknown"):
            self.assertIn(bit, body)
        self.assertIn("starts next pass", function_body(views, "planItems"))

    def test_empty_fleet_states_are_named(self):
        body = function_body(web_text("views.js"), "planCard")
        self.assertIn("no starts left today", body)
        self.assertIn("nothing waiting to start", body)

    def test_overview_leads_with_the_plan(self):
        body = function_body(web_text("views.js"), "renderOverview")
        self.assertIn("planCard(doc", body)


class HonestQuota(unittest.TestCase):
    """Source on every provider, estimates labelled, trimmed failures."""

    def test_provider_names_its_source(self):
        body = function_body(web_text("views.js"), "quotaProvider")
        for bit in ("source: ", "(estimate)"):
            self.assertIn(bit, body)

    def test_windows_label_estimates(self):
        body = function_body(web_text("views.js"), "quotaWindow")
        self.assertIn("(estimate)", body)

    def test_failures_read_trimmed(self):
        body = function_body(web_text("views.js"), "quotaProvider")
        for bit in ("error_trimmed", "read_failed"):
            self.assertIn(bit, body)

    def test_missing_source_reads_unknown(self):
        body = function_body(web_text("views.js"), "quotaProvider")
        self.assertIn("unknown", body)


class PeekAndClock(unittest.TestCase):
    """Doing and idle on the detail, and stamps sliced with the local offset."""

    def test_detail_names_doing_and_idle_without_a_peek_caption(self):
        body = function_body(web_text("views.js"), "renderDetail")
        for bit in ('"doing: "', '"idle: "'):
            self.assertIn(bit, body)
        self.assertNotIn("peek: read-only", body)

    def test_shell_slices_the_offset_stamp(self):
        body = function_body(web_text("views.js"), "renderShell")
        for bit in ("server clock ", "formatLocalTime(", "nowIso"):
            self.assertIn(bit, body)

    def test_poll_passes_the_stamp_through(self):
        self.assertIn("nowIso", web_text("app.js"))

    def test_formatters_are_exported(self):
        fmt = web_text("fmt.js")
        self.assertIn("export function formatIdle(", fmt)
        self.assertIn("export function formatLocalTime(", fmt)

    def test_local_time_slices_the_stamped_offset(self):
        body = function_body(web_text("fmt.js"), "formatLocalTime")
        self.assertIn(".exec(value)", body)
        self.assertNotIn("parseIso(value)", body)


class SchemaGate(unittest.TestCase):
    """The shell renders document schema 4."""

    def test_shell_schema_matches_the_server(self):
        app = web_text("app.js")
        found = re.search(r"const EXPECTED_SCHEMA_VERSION = (\d+);", app)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(int(found.group(1)), snapshot.SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
