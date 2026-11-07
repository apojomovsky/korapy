"""Ticket 642: the dashboard layout rework with every gauge real.

Source-discipline tests over the packaged `korapy/web/` files plus
loopback tests for fixture mode: no JS runner, no network beyond
loopback, no real state dirs. One class per ticket point; the
maintainer's reviewer question (does every number trace to a real
reading, and what does each panel show when its source is missing)
is answered by the unknown-fallback pins below.
"""

import json
import os
import re
import tempfile
import unittest
from importlib import resources

from korapy import serve
from tests.test_serve import ServeCase


def web_text(name):
    return resources.files("korapy").joinpath(f"web/{name}").read_text(encoding="utf-8")


def function_body(source, name):
    """The text of `function name(...)` through its closing brace line."""
    match = re.search(
        r"(?:export )?function " + re.escape(name) + r"\(.*?^\}",
        source,
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None, "no function " + name
    return match.group(0)


class Strip(unittest.TestCase):
    """Point 1: a top status strip, always visible."""

    def test_strip_node_exists_in_the_shell(self):
        self.assertIn('id="strip"', web_text("index.html"))

    def test_strip_renderer_is_exported(self):
        self.assertIn("export function renderStrip(", web_text("views.js"))

    def test_strip_names_loop_quota_load_and_disk(self):
        body = function_body(web_text("views.js"), "stripText")
        for bit in ("loop running, last pass", "starts ", "quota fresh", "load ", "/cpu", "free"):
            self.assertIn(bit, body)

    def test_strip_never_reports_a_missing_source_as_zero(self):
        body = function_body(web_text("views.js"), "stripText")
        for bit in (
            "fleet unknown",
            "loop unknown",
            "quota unknown",
            "load unknown",
            "disk unknown",
        ):
            self.assertIn(bit, body)

    def test_overview_renders_the_strip(self):
        body = function_body(web_text("views.js"), "renderOverview")
        self.assertIn("renderStrip(doc", body)

    def test_stopped_gate_degrades_the_strip_to_unknown(self):
        body = function_body(web_text("views.js"), "renderStopped")
        self.assertIn("renderStrip(null", body)


class AgentsFirst(unittest.TestCase):
    """Point 2: agents first and large, with an honest empty state."""

    def test_agents_section_sits_above_the_cards(self):
        shell = web_text("index.html")
        self.assertLess(shell.index('id="agents"'), shell.index('id="cards"'))

    def test_live_rows_are_highlighted_by_state(self):
        views = web_text("views.js")
        self.assertIn('setAttribute("data-state"', views)
        css = web_text("app.css")
        self.assertIn('tr[data-state="live"]', css)
        self.assertIn("--live-bg", css)

    def test_empty_state_names_the_loop(self):
        body = function_body(web_text("views.js"), "emptyAgentsText")
        self.assertIn("Nothing running right now", body)
        self.assertIn("last passed", body)
        self.assertIn("paused", body)

    def test_empty_state_keeps_the_launch_hint(self):
        self.assertIn("korapy launch", web_text("views.js"))

    def test_finished_rows_dim_and_sort_last(self):
        self.assertIn("retainedFinished", function_body(web_text("views.js"), "agentGroup"))
        self.assertIn("finishedDetails", web_text("views.js"))


class RealGauges(unittest.TestCase):
    """Point 3: CPU, memory and disk show the actual host values."""

    def test_gauge_helpers_exist_in_fmt(self):
        fmt = web_text("fmt.js")
        for name in ("bandFor", "clamp01", "formatLoad", "formatMemUsed", "formatRetentionWindow"):
            self.assertIn("export function " + name + "(", fmt)

    def test_gauge_bands_match_the_charged_bar_cuts(self):
        fmt = web_text("fmt.js")
        body = function_body(fmt, "bandFor")
        self.assertIn("0.7", body)
        self.assertIn("0.9", body)

    def test_missing_gauge_source_reads_unknown(self):
        fmt = web_text("fmt.js")
        self.assertIn('"not reported"', function_body(fmt, "formatLoad"))
        self.assertIn('"not reported"', function_body(fmt, "formatMemUsed"))
        self.assertIn('"unknown"', function_body(fmt, "bandFor"))

    def test_cpu_and_memory_gauges_carry_a_reserved_tick(self):
        views = web_text("views.js")
        body = function_body(views, "hostGauge")
        self.assertIn("meter(", body)
        self.assertIn("marker", body)
        for name in ("cpuGauge", "memoryGauge"):
            self.assertIn("hostGauge(", function_body(views, name))
        self.assertIn("of the host", views)

    def test_host_card_labels_the_reservation(self):
        body = function_body(web_text("views.js"), "resourcesCard")
        self.assertIn("reserved for agents", body)
        self.assertIn("the tick inside is the share the loop reserved", body)

    def test_disk_card_says_no_reservation_is_tracked(self):
        body = function_body(web_text("views.js"), "diskCard")
        self.assertIn("no reservation is tracked for disk", body)


class HonestQuota(unittest.TestCase):
    """Point 4: quota bars with a reset countdown and a dated stale badge."""

    def test_stale_badge_carries_the_reading_age(self):
        body = function_body(web_text("views.js"), "quotaProvider")
        self.assertIn("badge", body)
        self.assertIn("stale, ", body)
        self.assertIn("never read", body)

    def test_windows_keep_the_reset_countdown(self):
        body = function_body(web_text("views.js"), "quotaWindow")
        self.assertIn("formatResetLine", body)
        self.assertIn("no reading", body)


class PlainLabels(unittest.TestCase):
    """Point 5: plain labels with one-line captions, tooltips for help."""

    def test_host_card_replaces_resources(self):
        self.assertIn('card("host"', web_text("views.js"))

    def test_abbreviations_keep_tooltips(self):
        views = web_text("views.js")
        self.assertIn('"abbr"', views)
        self.assertIn("central processing unit", views)


class AttentionBand(unittest.TestCase):
    """Attention: only items that need a human, no new GitHub calls."""

    def test_attention_items_are_exported_for_review(self):
        self.assertIn("export function attentionItems(", web_text("views.js"))

    def test_empty_state_says_nothing_needs_you(self):
        self.assertIn("nothing needs you", web_text("views.js"))

    def test_attention_covers_pause_stale_quota_and_failed_sections(self):
        body = function_body(web_text("views.js"), "attentionItems")
        for bit in ("paused", "stale", "saturated", "could not be read"):
            self.assertIn(bit, body)

    def test_attention_card_leads_the_overview(self):
        body = function_body(web_text("views.js"), "renderOverview")
        self.assertIn("attentionCard(doc", body)


class Retention(unittest.TestCase):
    """Recently handled: finished runs stay six hours, then drop out."""

    def test_retention_window_is_named_in_api(self):
        api = web_text("api.js")
        self.assertIn("export const FINISHED_RETENTION_MS = 6 * 3600 * 1000;", api)

    def test_undated_finished_rows_never_age_out(self):
        self.assertIn("never age out", web_text("api.js"))

    def test_toggle_names_the_window(self):
        self.assertIn("formatRetentionWindow(", web_text("views.js"))


class InstantRestart(unittest.TestCase):
    """Instant restart: the last good snapshot renders at once."""

    def test_cache_helpers_are_named_in_api(self):
        api = web_text("api.js")
        self.assertIn("SNAPSHOT_CACHE_KEY", api)
        self.assertIn("export function loadCachedSnapshot(", api)
        self.assertIn("export function saveCachedSnapshot(", api)

    def test_boot_renders_the_saved_copy_with_its_age(self):
        app = web_text("app.js")
        self.assertIn("loadCachedSnapshot()", app)
        self.assertIn("saved copy, ", app)

    def test_good_polls_replace_the_cache(self):
        self.assertIn("saveCachedSnapshot(", web_text("app.js"))


class KeyboardFirst(unittest.TestCase):
    """Keyboard first, mouse too: every key action works by click."""

    def test_keybindings_are_pinned(self):
        app = web_text("app.js")
        self.assertIn('"j"', app)
        self.assertIn('"k"', app)
        self.assertIn('"/"', app)
        self.assertIn('"?"', app)
        self.assertIn('"Enter"', app)

    def test_help_panel_exists_in_the_shell(self):
        shell = web_text("index.html")
        self.assertIn('id="help"', shell)
        self.assertIn('id="help-close"', shell)
        self.assertIn("Every key also works by click.", shell)

    def test_filter_box_is_focusable_by_key(self):
        self.assertIn("agent-search", web_text("views.js"))
        self.assertIn("agent-search", web_text("app.js"))

    def test_every_key_action_also_works_by_click(self):
        app = web_text("app.js")
        self.assertIn("help-close", app)
        self.assertIn("data-copy-link", app)
        self.assertIn("#agents table.agents tbody tr", app)


class Toasts(unittest.TestCase):
    """Toasts for copy and open outcomes, bottom right."""

    def test_toast_region_lives_outside_the_footer(self):
        shell = web_text("index.html")
        self.assertIn('id="toasts"', shell)

    def test_toasts_are_capped_and_auto_removed(self):
        body = function_body(web_text("views.js"), "toast")
        self.assertIn("4000", body)
        self.assertIn("never in the footer", web_text("views.js"))

    def test_copy_and_token_outcomes_toast(self):
        app = web_text("app.js")
        self.assertIn("link copied", app)
        self.assertIn("token saved", app)


class FixtureMode(ServeCase):
    """A saved snapshot file drives the snapshot routes."""

    def write_fixture(self, tmp, payload):
        path = os.path.join(tmp, "fixture.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(payload)
        return path

    def test_fixture_builder_serves_the_file(self):
        with tempfile.TemporaryDirectory(prefix="korapy-fixture-") as tmp:
            doc = {"schema_version": 3, "fleet_header": {"host": "fixture-host"}}
            path = self.write_fixture(tmp, json.dumps(doc))
            server, _ = self.start_server(serve.fixture_build(path))
            status, _, body = self.fetch(server, "GET", "/v1/snapshot")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), doc)

    def test_fixture_sections_ride_the_same_file(self):
        with tempfile.TemporaryDirectory(prefix="korapy-fixture-") as tmp:
            agents = [{"id": "a1", "state": "live"}]
            doc = {
                "schema_version": 3,
                "fleet_header": {"host": "fixture-host"},
                "agents": agents,
            }
            path = self.write_fixture(tmp, json.dumps(doc))
            server, _ = self.start_server(serve.fixture_build(path))
            status, _, body = self.fetch(server, "GET", "/v1/agents")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["agents"], agents)

    def test_missing_fixture_fails_closed(self):
        with self.assertRaises(serve.ServeError):
            serve.fixture_build("/nonexistent/fixture.json")

    def test_corrupt_fixture_fails_closed(self):
        with tempfile.TemporaryDirectory(prefix="korapy-fixture-") as tmp:
            path = self.write_fixture(tmp, "not json{")
            with self.assertRaises(serve.ServeError):
                serve.fixture_build(path)

    def test_non_object_fixture_fails_closed(self):
        with tempfile.TemporaryDirectory(prefix="korapy-fixture-") as tmp:
            path = self.write_fixture(tmp, "[1, 2]")
            with self.assertRaises(serve.ServeError):
                serve.fixture_build(path)


class FixtureFlag(unittest.TestCase):
    """`serve --snapshot-file` is a documented verb flag."""

    def test_parser_accepts_a_snapshot_file(self):
        import argparse

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        serve.add_parser(sub)
        args = parser.parse_args(["serve", "--snapshot-file", "docs/assets/dashboard-fixture.json"])
        self.assertEqual(args.snapshot_file, "docs/assets/dashboard-fixture.json")

    def test_flag_defaults_to_live(self):
        import argparse

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        serve.add_parser(sub)
        args = parser.parse_args(["serve"])
        self.assertIsNone(args.snapshot_file)


if __name__ == "__main__":
    unittest.main()
