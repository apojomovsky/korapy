"""Ticket 673: dashboard polish 2 (card heights, plan wording, placeholders).

Source-discipline tests over the packaged `korapy/web/` files: no JS
runner, no network beyond loopback, no real state dirs. Each class maps
to one ticket point; the plan-band wording tests land with the
maintainer-approved design (point 2), not here.
"""

import re
import unittest
from importlib import resources


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


class CardHeights(unittest.TestCase):
    """Point 1: cards align to their content, never stretch to the tallest."""

    def test_cards_align_to_start(self):
        match = re.search(r"#cards\s*\{(.*?)\}", web_text("app.css"), re.DOTALL)
        assert match is not None, "no #cards rule"
        self.assertIn("align-items: start", match.group(1))

    def test_cards_still_fit_three_across(self):
        self.assertIn("min(20rem, 100%)", web_text("app.css"))


class PlanBandWording(unittest.TestCase):
    """Point 2: wait rows name the ticket and the reason, collapsed with a count."""

    def test_wait_rows_name_ticket_and_reason(self):
        body = function_body(web_text("views.js"), "planItems")
        self.assertIn("waits: ", body)
        self.assertIn("item.ticket", body)

    def test_identical_rows_collapse_with_a_count(self):
        body = function_body(web_text("views.js"), "planItems")
        self.assertIn("tickets)", body)

    def test_free_slots_start_next_pass(self):
        body = function_body(web_text("views.js"), "planItems")
        self.assertIn("starts next pass", body)

    def test_countdown_is_kept(self):
        body = function_body(web_text("views.js"), "planCard")
        self.assertIn("next pass in ", body)

    def test_no_repeated_slot_free_rows(self):
        self.assertNotIn("next start waits: ", web_text("views.js"))


class PlaceholdersRemoved(unittest.TestCase):
    """Point 3: the Later panel is gone; placeholders belong in the schema."""

    def test_no_planned_panel_names(self):
        views = web_text("views.js")
        for name in (
            "external contributions",
            "in-flight review",
            "board sync lag",
            "quota gate and pacing share",
        ):
            self.assertNotIn(name, views)
        self.assertNotIn(": planned", views)

    def test_overview_renders_no_placeholders(self):
        views = web_text("views.js")
        self.assertNotIn("placeholdersCard", views)
        self.assertNotIn("PLANNED_PANELS", views)
        body = function_body(views, "renderOverview")
        self.assertNotIn("Later", body)


if __name__ == "__main__":
    unittest.main()
