"""Ticket 669: the doctor row in the attention band and the peek pane.

Source-discipline tests over the packaged `korapy/web/` files plus
the committed fixture: the doctor rows trace to `doctor_failing`,
the peek traces to the `/v1/peek` document, and each pane names
what it shows when its source is missing. No JS runner, no network.
"""

import json
import re
import unittest

from tests.test_web_dashboard_651 import function_body, web_text


class DoctorRowBand(unittest.TestCase):
    """Failing doctor rows with their fix text, unknown with no record."""

    def test_attention_items_name_failing_doctor_rows(self):
        body = function_body(web_text("views.js"), "attentionItems")
        for bit in ("doctor_failing", "doctor ", "failing: ", "fix: "):
            self.assertIn(bit, body)

    def test_missing_doctor_record_reads_unknown(self):
        body = function_body(web_text("views.js"), "attentionItems")
        self.assertIn("doctor state is unknown", body)
        self.assertIn("unknown", body)


class PeekPane(unittest.TestCase):
    """A bounded peek of the selected agent, never an empty pane."""

    def test_api_loads_the_peek_document(self):
        api = web_text("api.js")
        self.assertIn("export const PEEK_POLL_MS = 3000;", api)
        self.assertIn("export function loadPeek(", api)
        self.assertIn('"/v1/peek?repo="', api)

    def test_peek_lines_carry_sliced_local_times(self):
        body = function_body(web_text("views.js"), "renderPeek")
        self.assertIn("formatLocalTime(", body)
        self.assertIn('getElementById("tail")', body)
        self.assertIn(".textContent =", body)

    def test_missing_peek_says_not_available(self):
        views = web_text("views.js")
        self.assertIn('"not available"', function_body(views, "renderPeekMissing"))
        self.assertIn('"not available"', function_body(views, "renderPeek"))

    def test_detail_captions_the_bounded_peek(self):
        body = function_body(web_text("views.js"), "renderDetail")
        self.assertIn("peek: read-only, bounded by the server", body)

    def test_shell_polls_the_peek_not_the_tail_text(self):
        app = web_text("app.js")
        self.assertIn("loadPeek(", app)
        self.assertIn("PEEK_POLL_MS", app)
        self.assertNotIn("loadTail(", app)

    def test_fixture_shows_the_doctor_row_and_the_peek_offline(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        with open(root / "docs" / "assets" / "dashboard-fixture.json", encoding="utf-8") as handle:
            doc = json.load(handle)
        failing = doc["attention"]["doctor_failing"]
        self.assertIsInstance(failing, list)
        self.assertTrue(failing)
        self.assertIn("fix", failing[0])
        peek = doc["peek"]["repo-a#41"]
        self.assertEqual(peek["state"], "ready")
        self.assertTrue(peek["lines"])
        self.assertIn("at", peek["lines"][0])
        self.assertIn("text", peek["lines"][0])


if __name__ == "__main__":
    unittest.main()
