"""Ticket 659: dashboard polish (short gauge figures, pills, durations).

Source-discipline tests over the packaged `korapy/web/` files plus pure
pressure tests: no JS runner, no network beyond loopback, no real state
dirs. Each class maps to one ticket point.
"""

import re
import unittest
from importlib import resources

from korapy import pressure as P
from korapy import resources as R


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


class ShortGaugeFigures(unittest.TestCase):
    """Point 1: the giant figure is short; the rest rides a small line."""

    def test_load_figure_is_short_with_no_total_prefix(self):
        fmt = web_text("fmt.js")
        self.assertIn("export function formatLoadFigure(", fmt)
        body = function_body(fmt, "formatLoadFigure")
        self.assertIn("per core", body)
        self.assertNotIn('" on "', body)

    def test_load_detail_carries_the_total(self):
        fmt = web_text("fmt.js")
        self.assertIn("export function formatLoadDetail(", fmt)
        body = function_body(fmt, "formatLoadDetail")
        self.assertIn("load on", body)

    def test_memory_figure_is_a_bare_size(self):
        fmt = web_text("fmt.js")
        self.assertIn("export function formatMemFigure(", fmt)
        body = function_body(fmt, "formatMemFigure")
        self.assertIn("GB", body)
        self.assertNotIn('"used "', body)
        self.assertNotIn('"of "', body)

    def test_memory_detail_carries_the_total(self):
        fmt = web_text("fmt.js")
        self.assertIn("export function formatMemDetail(", fmt)
        body = function_body(fmt, "formatMemDetail")
        self.assertIn('"of "', body)

    def test_cpu_gauge_renders_figure_plus_detail(self):
        body = function_body(web_text("views.js"), "cpuGauge")
        self.assertIn("formatLoadFigure(", body)
        self.assertIn("formatLoadDetail(", body)

    def test_memory_gauge_renders_figure_plus_detail(self):
        body = function_body(web_text("views.js"), "memoryGauge")
        self.assertIn("formatMemFigure(", body)
        self.assertIn("formatMemDetail(", body)

    def test_gauge_detail_rides_a_small_line(self):
        views = web_text("views.js")
        self.assertIn('"sub"', function_body(views, "hostGauge"))
        self.assertIn(".card.gauge .sub", web_text("app.css"))

    def test_disk_giant_is_short_with_total_in_the_detail(self):
        body = function_body(web_text("views.js"), "diskCard")
        # The per-path small lines keep their totals inline; only the
        # giant figure splits off its detail.
        giant = body.split('"disks"')[0]
        self.assertNotIn('free of "', giant)
        self.assertIn('"of "', giant)


class NoRepeatedPillLabels(unittest.TestCase):
    """Point 2: the pill label names the fact; the value does not repeat it."""

    def test_loop_value_does_not_repeat_loop(self):
        body = function_body(web_text("views.js"), "stripSegments")
        self.assertNotIn('"loop running', body)
        self.assertNotIn('"loop paused', body)
        self.assertNotIn('"loop unknown', body)

    def test_quota_value_does_not_repeat_quota(self):
        body = function_body(web_text("views.js"), "stripSegments")
        self.assertNotIn('"quota stale', body)
        self.assertNotIn('"quota fresh', body)
        self.assertNotIn('"quota unknown', body)
        self.assertNotIn('"quota: ', body)

    def test_load_value_does_not_repeat_load(self):
        body = function_body(web_text("views.js"), "stripSegments")
        self.assertNotIn('"load "', body)
        self.assertNotIn('"/cpu"', body)

    def test_disk_value_does_not_repeat_disk(self):
        body = function_body(web_text("views.js"), "stripSegments")
        self.assertNotIn('"disk "', body)
        self.assertNotIn('"disk unknown', body)


class NoRepeatedPillLabelsContinued(unittest.TestCase):
    """Point 2, continued: the fleet pill and the pressure wording."""

    def test_fleet_value_does_not_repeat_fleet(self):
        body = function_body(web_text("views.js"), "stripSegments")
        self.assertNotIn('"fleet unknown', body)

    def test_pressure_text_spells_per_core(self):
        body = function_body(web_text("views.js"), "pressureText")
        self.assertIn("per core", body)
        self.assertNotIn('"/cpu"', body)

    def test_stale_reading_line_names_stale_once(self):
        # The stale badge and pill state already say stale, so the
        # reading sentence must not suffix it a second time.
        body = function_body(web_text("fmt.js"), "formatQuotaRead")
        self.assertNotIn('", stale"', body)


class RunningForDurations(unittest.TestCase):
    """Point 3: `running for` takes a duration, not an age."""

    def test_duration_formatter_exists_without_ago(self):
        fmt = web_text("fmt.js")
        self.assertIn("export function formatDuration(", fmt)
        body = function_body(fmt, "formatDuration")
        self.assertNotIn("ago", body)

    def test_running_cell_uses_a_duration(self):
        body = function_body(web_text("views.js"), "agentRow")
        self.assertIn("formatDuration(", body)

    def test_seen_cell_keeps_the_age(self):
        body = function_body(web_text("views.js"), "agentRow")
        self.assertIn("age(row.last_seen", body)

    def test_running_header_stays(self):
        body = function_body(web_text("views.js"), "agentTable")
        self.assertIn("running for", body)


class NegativeMemAvailable(unittest.TestCase):
    """Point 5: a negative MemAvailable reads as unknown and never blocks."""

    MEMINFO_NEGATIVE = "MemTotal:        8000000 kB\nMemAvailable:    -8 kB\n"

    def test_negative_available_reads_as_unknown(self):
        found = P.read("0.80 0.60 0.40 1/200 12345\n", self.MEMINFO_NEGATIVE, 4)
        self.assertIsNone(found.mem_available_pct)

    def test_unknown_memory_never_saturates_on_its_own(self):
        saturated, _detail = P.saturated(
            P.Reading(load_per_cpu=0.2, mem_available_pct=None),
            max_load_per_cpu=2.0,
            min_mem_available_pct=10.0,
        )
        self.assertFalse(saturated)

    def test_admission_pressure_with_negative_available_is_not_saturated(self):
        found = R.parse_pressure("0.80 0.60 0.40 1/200 12345\n", self.MEMINFO_NEGATIVE, 4)
        self.assertIsNone(found.mem_available_pct)
        self.assertFalse(found.saturated)


if __name__ == "__main__":
    unittest.main()
