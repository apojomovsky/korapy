"""Ticket 660: label running-for and last-seen meta on narrow screens.

On phone widths the agents table collapses to cards and the header
row hides, so the running-for and idle (last-seen) cells render as
adjacent unlabeled meta. Source-discipline tests over the packaged
`korapy/web/` files: no JS runner, no network, no real state dirs.
"""

import re
import unittest

from tests.test_web_dashboard_651 import function_body, web_text


def narrow_block(css):
    """The `@media (max-width: 700px)` block, braces balanced."""
    start = css.index("@media (max-width: 700px)")
    depth = 0
    for i in range(css.index("{", start), len(css)):
        if css[i] == "{":
            depth += 1
        elif css[i] == "}":
            depth -= 1
            if depth == 0:
                return css[start : i + 1]
    raise AssertionError("unbalanced narrow media block")


class NarrowMetaLabels(unittest.TestCase):
    """Both timed cells name themselves when the header row hides."""

    def test_running_cell_is_labelled_on_narrow_screens(self):
        block = narrow_block(web_text("app.css"))
        self.assertIn("td.c-running::before", block)
        match = re.search(r"td\.c-running::before\s*\{(.*?)\}", block, re.DOTALL)
        assert match is not None
        self.assertIn("running for", match.group(1))
        self.assertIn("content:", match.group(1))

    def test_seen_cell_self_names_without_a_generated_label(self):
        # The c-seen value is the idle age ("idle 4h" from formatIdle,
        # last-seen age on its tooltip), so it already names itself
        # on narrow screens; a generated "last seen" label in front
        # of it would duplicate and misname the value.
        block = narrow_block(web_text("app.css"))
        self.assertNotIn("td.c-seen::before", block)
        body = function_body(web_text("views.js"), "agentRow")
        self.assertIn("formatIdle(", body)
        self.assertIn('"last seen "', body)
        fmt = function_body(web_text("fmt.js"), "formatIdle")
        self.assertIn('"idle "', fmt)

    def test_labels_stay_narrow_only(self):
        css = web_text("app.css")
        start = css.index("@media (max-width: 700px)")
        desktop = css[:start]
        self.assertNotIn("c-running::before", desktop)
        self.assertNotIn("c-seen::before", desktop)

    def test_no_data_th_labels(self):
        # The readable pass (ticket 525) bans attr(data-th) labels;
        # these labels are literal CSS content, not cell attributes.
        self.assertNotIn("attr(data-th)", web_text("app.css"))

    def test_desktop_headers_unchanged(self):
        body = function_body(web_text("views.js"), "agentTable")
        self.assertIn('"running for"', body)
        self.assertIn('"idle"', body)

    def test_header_still_hides_on_narrow_screens(self):
        block = narrow_block(web_text("app.css"))
        self.assertIn("table.agents thead", block)
        self.assertIn("display: none", block)


if __name__ == "__main__":
    unittest.main()
