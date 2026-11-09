"""Area locks (docs/18, option 1): ticket 1, pure helper plus settings.

Tests use plain TicketRows. No tracker, no network, no subprocess,
no real state or config dir.
"""

import unittest
from typing import Any

from korapy import drive, eligibility, settings


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


if __name__ == "__main__":
    unittest.main()
