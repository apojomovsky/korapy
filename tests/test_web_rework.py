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
from pathlib import Path

from korapy import serve, snapshot
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
        body = function_body(web_text("views.js"), "stripSegments")
        for bit in ("running, last pass ", "starts ", "fresh", "per core", "free"):
            self.assertIn(bit, body)

    def test_strip_never_reports_a_missing_source_as_zero(self):
        body = function_body(web_text("views.js"), "stripSegments")
        # Every pill reads as a bare unknown: the pill label names
        # the fact, so the value never repeats it.
        self.assertGreaterEqual(body.count('text: "unknown"'), 5)
        self.assertNotIn("fleet unknown", body)

    def test_overview_renders_the_strip(self):
        body = function_body(web_text("views.js"), "renderOverview")
        self.assertIn("renderStrip(doc", body)

    def test_stopped_gate_degrades_the_strip_to_unknown(self):
        body = function_body(web_text("views.js"), "renderStopped")
        self.assertIn("renderStrip(null", body)

    def test_overview_has_no_separate_fleet_card(self):
        # The strip already says "2 live, 1 stale, 1 finished", so the
        # overview renders no second fleet card beside it.
        body = function_body(web_text("views.js"), "renderOverview")
        self.assertNotIn("headerLine(", body)
        self.assertNotIn("fleet_header", body)

    def test_strip_names_the_finished_count(self):
        body = function_body(web_text("views.js"), "stripSegments")
        self.assertIn("agents_finished", body)
        self.assertIn("finished", body)

    def test_strip_renders_segments_with_dots(self):
        body = function_body(web_text("views.js"), "renderStrip")
        self.assertIn("stripSegments(doc", body)
        self.assertIn('"seg seg-"', body)
        self.assertIn('"dot"', body)
        css = web_text("app.css")
        for bit in (".seg", ".seg-ok", ".seg-low", ".seg-unknown", "#strip"):
            self.assertIn(bit, css)


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
        for name in (
            "bandFor",
            "clamp01",
            "formatLoadFigure",
            "formatLoadDetail",
            "formatMemFigure",
            "formatMemDetail",
            "formatDuration",
            "formatRetentionWindow",
        ):
            self.assertIn("export function " + name + "(", fmt)

    def test_gauge_bands_match_the_charged_bar_cuts(self):
        fmt = web_text("fmt.js")
        body = function_body(fmt, "bandFor")
        self.assertIn("0.7", body)
        self.assertIn("0.9", body)

    def test_missing_gauge_source_reads_unknown(self):
        fmt = web_text("fmt.js")
        self.assertIn('"not reported"', function_body(fmt, "formatLoadFigure"))
        self.assertIn('"not reported"', function_body(fmt, "formatMemFigure"))
        self.assertIn('"unknown"', function_body(fmt, "bandFor"))

    def test_cpu_and_memory_gauges_carry_a_reserved_tick(self):
        views = web_text("views.js")
        body = function_body(views, "hostGauge")
        self.assertIn("meter(", body)
        self.assertIn("marker", body)
        for name in ("cpuGauge", "memoryGauge"):
            self.assertIn("hostGauge(", function_body(views, name))
        self.assertIn("of the host", views)

    def test_gauge_cards_label_the_reservation(self):
        views = web_text("views.js")
        for name, gauge in (("cpuCard", "cpuGauge("), ("memoryCard", "memoryGauge(")):
            body = function_body(views, name)
            self.assertIn(gauge, body)
            self.assertIn("tick inside is the share the loop reserved", body)
        self.assertIn("reserved for agents", views)

    def test_overview_renders_cpu_memory_and_disk_gauges(self):
        body = function_body(web_text("views.js"), "renderOverview")
        for name in ("cpuCard(", "memoryCard(", "diskCard("):
            self.assertIn(name, body)
        self.assertNotIn("resourcesCard(", body)

    def test_disk_card_says_no_reservation_is_tracked(self):
        body = function_body(web_text("views.js"), "diskCard")
        self.assertIn("reservation is tracked for disk, so this bar carries no tick", body)

    def test_disk_lists_paths_without_bars(self):
        body = function_body(web_text("views.js"), "diskCard")
        self.assertIn('"disks"', body)
        self.assertNotIn("row.appendChild(meter(", body)


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

    def test_operator_marks_name_their_lift_time(self):
        body = function_body(web_text("views.js"), "quotaProvider")
        self.assertIn("mark until ", body)
        self.assertIn("marked: no lift time recorded", body)

    def test_operator_mark_passes_the_raw_epoch_to_the_reset_line(self):
        # formatResetLine parses the epoch itself; handing it an
        # already-parsed stamp multiplies by 1000 twice and reads
        # years in the future.
        body = function_body(web_text("views.js"), "quotaProvider")
        self.assertIn("formatResetLine(mark.until, nowMs)", body)
        self.assertNotIn("formatResetLine(until,", body)
        self.assertNotIn("formatResetLine(liftAt,", body)


class PlainLabels(unittest.TestCase):
    """Point 5: plain labels with one-line captions, tooltips for help."""

    def test_cpu_and_memory_cards_replace_the_host_card(self):
        views = web_text("views.js")
        self.assertIn('card("CPU"', views)
        self.assertIn('card("Memory"', views)
        self.assertNotIn('card("host"', views)

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


class DriveLock(unittest.TestCase):
    """A missing lock reading is not reported, never a healthy idle."""

    def test_absent_lock_does_not_default_to_running(self):
        body = function_body(web_text("views.js"), "driveCard")
        self.assertIn("info.lock_held === true", body)
        self.assertIn("info.lock_held === false", body)


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


class FixtureContract(unittest.TestCase):
    """The committed fixture stays a usable gauge source of truth."""

    @staticmethod
    def fixture():
        root = Path(__file__).resolve().parent.parent
        with open(root / "docs" / "assets" / "dashboard-fixture.json", encoding="utf-8") as handle:
            return json.load(handle)

    def test_fixture_matches_the_snapshot_schema(self):
        doc = self.fixture()
        self.assertEqual(doc["schema_version"], snapshot.SCHEMA_VERSION)

    def test_fixture_carries_every_gauge_input(self):
        doc = self.fixture()
        pressure = doc["resources"]["pressure"]
        for key in ("ncpu", "load_1min", "mem_total_mb", "mem_available_mb", "load_per_cpu"):
            self.assertIsInstance(pressure[key], (int, float), key)
        self.assertTrue(doc["agents"])
        self.assertTrue(doc["quota"]["providers"])
        self.assertTrue(doc["disk"]["paths"])


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


class SelectedRowSurvivesPolls(unittest.TestCase):
    """The keyboard selection is re-applied after every overview render."""

    def test_overview_renders_restore_the_selection(self):
        app = web_text("app.js")
        self.assertIn("function restoreSelection(", app)
        # One restore per renderOverview site: the compare-and-skip
        # path in renderCurrent, the snapshot poll, and the boot cache.
        self.assertEqual(app.count("restoreSelection();"), 3)

    def test_restore_does_not_scroll_the_page(self):
        body = function_body(web_text("app.js"), "restoreSelection")
        self.assertIn('add("selected")', body)
        self.assertNotIn("scrollIntoView", body)


class TopBarAndFooter(unittest.TestCase):
    """One navigation row in the top bar; server details in a footer."""

    def test_tabs_live_inside_the_top_bar(self):
        shell = web_text("index.html")
        self.assertLess(shell.index("<header"), shell.index('id="tabs"'))
        self.assertLess(shell.index('id="tabs"'), shell.index("</header>"))

    def test_no_dashboard_heading_card(self):
        self.assertNotIn('id="status-card"', web_text("index.html"))

    def test_server_details_live_in_a_small_footer(self):
        shell = web_text("index.html")
        self.assertIn('id="meta"', shell)
        self.assertLess(shell.index('id="meta"'), shell.index('id="toasts"'))
        footer = shell[shell.index('id="meta"') :]
        for anchor in ('id="mode"', 'id="api"', 'id="status-line"'):
            self.assertIn(anchor, footer)


class AgentsVisualCentre(unittest.TestCase):
    """The agents table leads: project select, state chips, ticket links."""

    def test_agents_section_carries_a_project_select(self):
        views = web_text("views.js")
        self.assertIn("projectSelect(", function_body(views, "agentsSection"))
        select = function_body(views, "projectSelect")
        self.assertIn("project-select", select)
        self.assertIn('"select"', select)

    def test_select_navigates_by_hash(self):
        self.assertIn("project-select", web_text("app.js"))

    def test_overview_hides_the_pills_when_the_select_is_shown(self):
        body = function_body(web_text("views.js"), "renderOverview")
        self.assertIn("hidden = true", body)

    def test_ticket_is_the_row_link(self):
        body = function_body(web_text("views.js"), "agentRow")
        self.assertIn("detailHash(", body)

    def test_state_renders_as_a_dot_plus_text_chip(self):
        body = function_body(web_text("views.js"), "agentRow")
        self.assertIn("dot", body)
        self.assertIn("state-", body)

    def test_runtime_renders_as_a_badge(self):
        body = function_body(web_text("views.js"), "agentRow")
        self.assertIn("badge", body)

    def test_running_for_replaces_started(self):
        body = function_body(web_text("views.js"), "agentTable")
        self.assertIn("running for", body)
        self.assertNotIn('"started"', body)

    def test_live_rows_carry_an_accent_not_a_fill(self):
        css = web_text("app.css")
        self.assertIn('tr[data-state="live"]', css)
        self.assertIn("inset", css)


class AttentionBanner(unittest.TestCase):
    """Needs-you is a full-width banner under the strip, not a card."""

    def test_banner_node_sits_under_the_strip(self):
        shell = web_text("index.html")
        self.assertIn('id="attention"', shell)
        self.assertLess(shell.index('id="strip"'), shell.index('id="attention"'))
        self.assertLess(shell.index('id="attention"'), shell.index('id="agents"'))

    def test_overview_fills_the_banner(self):
        body = function_body(web_text("views.js"), "renderOverview")
        self.assertIn('getElementById("attention")', body)
        self.assertIn("attentionCard(doc", body)


class ShellProvidesEveryPollNode(unittest.TestCase):
    """Every id the scripts look up must exist in the shell or be
    created by the scripts. A missing node throws inside the poll
    loop: once this dropped the connection banner and the strip
    read failure forever with a stale page behind it."""

    def test_every_lookup_resolves_to_shell_or_created_node(self):
        import re

        shell = web_text("index.html")
        views = web_text("views.js")
        app = web_text("app.js")
        wanted = set(re.findall(r'getElementById\("([^"]+)"', views + app))
        self.assertIn("banner", wanted)
        static = set(re.findall(r'id="([^"]+)"', shell))
        created = set(re.findall(r'setAttribute\("id", "([^"]+)"', views + app))
        self.assertEqual(set(), wanted - static - created)

    def test_shell_has_the_connection_banner(self):
        self.assertIn('id="banner"', web_text("index.html"))


class ImpossibleMemoryReadsUnknown(unittest.TestCase):
    """Round 3: available above total is unknown, never a healthy gauge."""

    def test_format_mem_used_rejects_available_above_total(self):
        figure = function_body(web_text("fmt.js"), "formatMemFigure")
        self.assertIn("not reported", figure)
        self.assertIn("availMb > totalMb", figure)
        detail = function_body(web_text("fmt.js"), "formatMemDetail")
        self.assertIn('""', detail)
        self.assertIn("availMb > totalMb", detail)

    def test_memory_gauge_treats_impossible_reading_as_unknown(self):
        body = function_body(web_text("views.js"), "memoryGauge")
        self.assertIn("availMb <= totalMb", body)
        self.assertIn("let frac = null", body)


class UniformHostCards(unittest.TestCase):
    """Round 3: the three host cards share one shape, one bar style."""

    def test_gauges_render_a_giant_number_not_a_labelled_field(self):
        body = function_body(web_text("views.js"), "hostGauge")
        self.assertIn('"giant"', body)
        self.assertIn("meter(", body)
        self.assertIn("marker", body)
        self.assertNotIn('createElement("dt"', body)
        self.assertNotIn('createElement("dl"', body)

    def test_gauge_help_stays_a_tooltip(self):
        self.assertIn('setAttribute("title"', function_body(web_text("views.js"), "hostGauge"))

    def test_one_bar_style_for_gauges_and_quota(self):
        css = web_text("app.css")
        self.assertNotIn("height: 6px", css)
        meter = re.search(r"\.meter svg \{[^}]*\}", css)
        assert meter is not None, "no .meter svg rule"
        self.assertIn("height: 10px", meter.group(0))
        self.assertIn("border-radius", meter.group(0))
        self.assertIn('setAttribute("rx"', web_text("views.js"))

    def test_host_titles_read_as_sentences(self):
        views = web_text("views.js")
        for title in (
            'card("Memory"',
            'card("Disk"',
            'card("Quota"',
            'card("Capacity"',
            'card("Dispatch loop"',
            'card("Agents"',
        ):
            self.assertIn(title, views)


class ShortCaptions(unittest.TestCase):
    """Round 3: one short caption per panel, the rest in a tooltip."""

    def test_cards_carry_an_info_marker_with_the_long_text(self):
        body = function_body(web_text("views.js"), "card")
        self.assertIn('"info"', body)
        self.assertIn('setAttribute("title"', body)
        self.assertIn(".info", web_text("app.css"))

    def test_panel_captions_are_single_short_lines(self):
        views = web_text("views.js")
        for caption in (
            "Host load from the kernel, per core.",
            "Host memory used from MemTotal and MemAvailable.",
            "Free space on the state and cache disks.",
            "What the providers last reported.",
            "How many agents this host may run.",
            "The drive pass: sync boards, claim tickets, start agents.",
            "Agents on this host right now.",
            "Only items that need a human.",
        ):
            self.assertIn(caption, views)
            self.assertLessEqual(len(caption), 70, caption)


class StartsBudgetHalfMissing(unittest.TestCase):
    """Round 3: one half of the starts budget reads not reported."""

    def test_strip_spells_starts_today_in_words(self):
        body = function_body(web_text("views.js"), "stripSegments")
        self.assertIn("starts today", body)
        self.assertIn(" of ", body)

    def test_half_missing_budget_reads_not_reported(self):
        body = function_body(web_text("views.js"), "stripSegments")
        self.assertIn("starts not reported", body)


class StripWrapsAndScale(unittest.TestCase):
    """The strip wraps on narrow screens; one spacing scale and type scale."""

    def test_strip_never_clips(self):
        import re

        css = web_text("app.css")
        strip = re.search(r"#strip \{[^}]*\}", css)
        self.assertIsNotNone(strip, "no #strip rule")
        assert strip is not None
        self.assertNotIn("nowrap", strip.group(0))
        self.assertIn("white-space: normal", strip.group(0))

    def test_base_type_is_14px_with_ten_px_radius(self):
        css = web_text("app.css")
        self.assertIn("font-size: 0.875rem", css)
        self.assertIn("10px", css)

    def test_content_centres_at_twelve_hundred_px(self):
        self.assertIn("75rem", web_text("app.css"))


if __name__ == "__main__":
    unittest.main()
