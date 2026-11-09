"""The dispatch freeze: drain agents, hold the loop, resume on merge. Fakes only.

The freeze writes a state file beside the drive pause file and holds the
same pause, so the loop stops starting while live agents drain. Every
host read (live units, tracker item states, the clock) arrives through
an injected seam; files live in temporary directories, never the real
state dir. No tracker, no network, no subprocess.
"""

import io
import json
import os
import tempfile
import unittest

from korapy import freeze


def item_state(states, calls=None):
    """An item-state fake serving states from a {(repo, number): state} map."""

    def read(repo, number):
        if calls is not None:
            calls.append((repo, number))
        value = states[(repo, number)]
        if isinstance(value, Exception):
            raise value
        return value

    return read


class ParseRefTest(unittest.TestCase):
    def test_plain_ref_parses(self):
        self.assertEqual(freeze.parse_ref("app#7"), ("app", 7))

    def test_repos_with_hyphens_parse(self):
        self.assertEqual(freeze.parse_ref("my-app#12"), ("my-app", 12))

    def test_missing_number_is_a_freeze_error(self):
        with self.assertRaises(freeze.FreezeError):
            freeze.parse_ref("app")

    def test_missing_repo_is_a_freeze_error(self):
        with self.assertRaises(freeze.FreezeError):
            freeze.parse_ref("#7")

    def test_zero_is_a_freeze_error(self):
        with self.assertRaises(freeze.FreezeError):
            freeze.parse_ref("app#0")

    def test_non_string_is_a_freeze_error(self):
        with self.assertRaises(freeze.FreezeError):
            freeze.parse_ref(("app", 7))

    def test_format_round_trips(self):
        self.assertEqual(freeze.parse_ref(freeze.format_ref("app", 7)), ("app", 7))


class ParseUntilTest(unittest.TestCase):
    def test_empty_means_no_gate(self):
        self.assertEqual(freeze.parse_until_refs([]), [])

    def test_duplicates_keep_first_order(self):
        self.assertEqual(
            freeze.parse_until_refs(["app#7", "app#7", "app#8"]),
            [("app", 7), ("app", 8)],
        )

    def test_bad_entry_fails_the_whole_list(self):
        with self.assertRaises(freeze.FreezeError):
            freeze.parse_until_refs(["app#7", "bogus"])

    def test_non_list_is_a_freeze_error(self):
        with self.assertRaises(freeze.FreezeError):
            freeze.parse_until_refs("app#7")


class FreezeFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="korapy-freeze-")
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "drive.freeze")

    def test_missing_file_reads_as_no_freeze(self):
        self.assertIsNone(freeze.read_freeze(self.path))
        self.assertIsNone(freeze.active_reason(self.path))

    def test_write_then_read_round_trips(self):
        freeze.write_freeze(
            self.path, reason="sweep", until=["app#7"], now=2000.0, owned_pause=True
        )
        record = freeze.read_freeze(self.path)
        self.assertEqual(record["reason"], "sweep")
        self.assertEqual(record["until"], ["app#7"])
        self.assertEqual(record["created_at"], 2000.0)
        self.assertTrue(record["owned_pause"])

    def test_reason_names_the_freeze(self):
        freeze.write_freeze(self.path, reason="sweep", until=[], now=2000.0, owned_pause=True)
        self.assertEqual(freeze.active_reason(self.path), "frozen: sweep")

    def test_empty_reason_reads_generic(self):
        freeze.write_freeze(self.path, reason="", until=[], now=2000.0, owned_pause=True)
        self.assertEqual(freeze.active_reason(self.path), "frozen")

    def test_corrupt_file_raises_freeze_error(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        with self.assertRaises(freeze.FreezeError):
            freeze.read_freeze(self.path)
        with self.assertRaises(freeze.FreezeError):
            freeze.active_reason(self.path)

    def test_wrong_shape_raises_freeze_error(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"reason": 7}) + "\n")
        with self.assertRaises(freeze.FreezeError):
            freeze.read_freeze(self.path)

    def test_view_reports_age_and_until(self):
        freeze.write_freeze(
            self.path,
            reason="sweep",
            until=["app#7"],
            now=1000.0,
            owned_pause=True,
        )
        view = freeze.freeze_view(self.path, now=4600.0, max_age_s=86400)
        self.assertEqual(view["reason"], "sweep")
        self.assertEqual(view["until"], ["app#7"])
        self.assertEqual(view["age_s"], 3600)
        self.assertFalse(view["stale"])

    def test_view_marks_a_freeze_past_its_max_age_stale(self):
        freeze.write_freeze(self.path, reason="sweep", until=[], now=1000.0, owned_pause=True)
        view = freeze.freeze_view(self.path, now=1000.0 + 86401, max_age_s=86400)
        self.assertTrue(view["stale"])

    def test_view_of_a_missing_file_is_none(self):
        self.assertIsNone(freeze.freeze_view(self.path, now=0.0, max_age_s=60))


class AllMergedTest(unittest.TestCase):
    def test_no_gate_is_merged(self):
        merged, pending = freeze.all_merged([], item_state({}))
        self.assertTrue(merged)
        self.assertEqual(pending, [])

    def test_closed_tickets_are_merged(self):
        read = item_state({("app", 7): "closed", ("app", 8): "closed"})
        merged, pending = freeze.all_merged([("app", 7), ("app", 8)], read)
        self.assertTrue(merged)
        self.assertEqual(pending, [])

    def test_an_open_ticket_blocks(self):
        read = item_state({("app", 7): "closed", ("app", 8): "open"})
        merged, pending = freeze.all_merged([("app", 7), ("app", 8)], read)
        self.assertFalse(merged)
        self.assertEqual(pending, ["app#8"])

    def test_an_unreadable_ticket_blocks(self):
        read = item_state({("app", 7): RuntimeError("tracker down")})
        merged, pending = freeze.all_merged([("app", 7)], read)
        self.assertFalse(merged)
        self.assertEqual(pending, ["app#7"])


class DrainTest(unittest.TestCase):
    def test_no_live_agents_needs_no_sleep(self):
        out, sleeps = io.StringIO(), []
        passes = freeze.drain(lambda: 0, sleep=sleeps.append, out=out)
        self.assertEqual(passes, 1)
        self.assertEqual(sleeps, [])
        self.assertIn("no live agents", out.getvalue())

    def test_each_pass_reports_the_live_count(self):
        live = iter([2, 1, 0])
        out, sleeps = io.StringIO(), []
        passes = freeze.drain(lambda: next(live), sleep=sleeps.append, out=out)
        self.assertEqual(passes, 3)
        self.assertEqual(sleeps, [freeze.POLL_S, freeze.POLL_S])
        text = out.getvalue()
        self.assertIn("2", text)
        self.assertIn("1", text)


class TryReleaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="korapy-freeze-release-")
        self.addCleanup(self.tmp.cleanup)
        self.frost = os.path.join(self.tmp.name, "drive.freeze")
        self.pause = os.path.join(self.tmp.name, "drive.pause")

    def test_missing_freeze_is_not_a_release(self):
        self.assertFalse(freeze.try_release(self.frost, self.pause, item_state({})))

    def test_freeze_without_until_never_releases_itself(self):
        freeze.write_freeze(self.frost, reason="sweep", until=[], now=1000.0, owned_pause=True)
        self.assertFalse(freeze.try_release(self.frost, self.pause, item_state({})))
        self.assertTrue(os.path.exists(self.frost))

    def test_merged_until_removes_the_freeze_and_its_pause(self):
        freeze.write_freeze(
            self.frost, reason="sweep", until=["app#7"], now=1000.0, owned_pause=True
        )
        with open(self.pause, "w", encoding="utf-8") as handle:
            handle.write("frozen: sweep\n")
        released = freeze.try_release(self.frost, self.pause, item_state({("app", 7): "closed"}))
        self.assertTrue(released)
        self.assertFalse(os.path.exists(self.frost))
        self.assertFalse(os.path.exists(self.pause))

    def test_an_open_ticket_keeps_the_freeze(self):
        freeze.write_freeze(
            self.frost, reason="sweep", until=["app#7"], now=1000.0, owned_pause=True
        )
        released = freeze.try_release(self.frost, self.pause, item_state({("app", 7): "open"}))
        self.assertFalse(released)
        self.assertTrue(os.path.exists(self.frost))

    def test_release_leaves_a_pause_the_freeze_does_not_own(self):
        freeze.write_freeze(
            self.frost, reason="sweep", until=["app#7"], now=1000.0, owned_pause=False
        )
        with open(self.pause, "w", encoding="utf-8") as handle:
            handle.write("operator break\n")
        released = freeze.try_release(self.frost, self.pause, item_state({("app", 7): "closed"}))
        self.assertTrue(released)
        self.assertFalse(os.path.exists(self.frost))
        with open(self.pause, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "operator break\n")

    def test_release_without_a_pause_file_still_releases(self):
        freeze.write_freeze(
            self.frost, reason="sweep", until=["app#7"], now=1000.0, owned_pause=True
        )
        released = freeze.try_release(self.frost, self.pause, item_state({("app", 7): "closed"}))
        self.assertTrue(released)
        self.assertFalse(os.path.exists(self.frost))

    def test_pause_removal_failure_keeps_the_freeze_standing(self):
        import types
        from unittest import mock

        freeze.write_freeze(
            self.frost, reason="sweep", until=["app#7"], now=1000.0, owned_pause=True
        )
        with open(self.pause, "w", encoding="utf-8") as handle:
            handle.write("frozen: sweep\n")
        real_os = freeze.os

        def remove(path):
            if path == self.pause:
                raise OSError("disk full")
            return real_os.remove(path)

        fake = types.SimpleNamespace(remove=remove)
        with mock.patch.object(freeze, "os", fake):
            released = freeze.try_release(
                self.frost, self.pause, item_state({("app", 7): "closed"})
            )
        self.assertFalse(released)
        self.assertTrue(os.path.exists(self.frost))
        self.assertTrue(os.path.exists(self.pause))


def closed_tracker():
    """A tracker fake whose app#7 reads merged."""
    from tests.test_tracker import FakeTracker, reading

    return FakeTracker(items=[reading(repo="app", number=7, state="closed")])


def open_tracker():
    """A tracker fake whose app#7 still reads open."""
    from tests.test_tracker import FakeTracker, reading

    return FakeTracker(items=[reading(repo="app", number=7, state="open")])


class DriveFreezeTest(unittest.TestCase):
    """The dispatch pass holds on a freeze through the one pause resolver."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="korapy-freeze-drive-")
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "board.sqlite")
        self.lock = os.path.join(self.tmp.name, "drive.lock")
        self.policies = os.path.join(self.tmp.name, "policies")
        os.makedirs(self.policies)
        self.pause = os.path.join(self.tmp.name, "drive.pause")
        self.frost = os.path.join(self.tmp.name, "drive.freeze")

    def passing(self, store, plan=True, **fields):
        from tests.test_drive_claims import FakeLauncher, drive_pass

        out, err = io.StringIO(), io.StringIO()
        launcher = FakeLauncher()
        params = {"pause_path": self.pause, "freeze_path": self.frost}
        params.update(fields)
        code = drive_pass(
            store,
            self.lock,
            out,
            err,
            plan=plan,
            policy_dir=self.policies,
            launcher=launcher,
            **params,
        )
        return code, out.getvalue(), err.getvalue(), launcher

    def test_frozen_plan_exits_one_with_the_frozen_reason(self):
        from tests.test_drive import stored

        freeze.write_freeze(self.frost, reason="sweep", until=[], now=1000.0, owned_pause=True)
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        code, _, err, launcher = self.passing(store, found=[("blue", [("app", 7, "start")])])
        self.assertEqual(code, 1)
        self.assertIn("frozen: sweep", err)
        self.assertEqual(launcher.calls, [])

    def test_operator_pause_still_wins_over_a_freeze(self):
        from tests.test_drive import stored

        with open(self.pause, "w", encoding="utf-8") as handle:
            handle.write("operator break\n")
        freeze.write_freeze(self.frost, reason="sweep", until=[], now=1000.0, owned_pause=False)
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        code, _, err, _ = self.passing(store, found=[("blue", [("app", 7, "start")])])
        self.assertEqual(code, 1)
        self.assertIn("operator break", err)

    def test_merged_until_releases_the_pass_and_the_files(self):
        from tests.test_drive import stored

        freeze.write_freeze(
            self.frost, reason="sweep", until=["app#7"], now=1000.0, owned_pause=True
        )
        with open(self.pause, "w", encoding="utf-8") as handle:
            handle.write("frozen: sweep\n")
        store = stored(self.db, [("app", 7, "open"), ("app", 8, "open")])
        self.addCleanup(store.close)
        code, out, _, launcher = self.passing(
            store,
            plan=False,
            found=[("blue", [("app", 8, "start")])],
            tracker_factory=lambda repo: closed_tracker(),
        )
        self.assertEqual(code, 0)
        self.assertIn("freeze ended", out)
        self.assertEqual(len(launcher.calls), 1)
        self.assertFalse(os.path.exists(self.frost))
        self.assertFalse(os.path.exists(self.pause))

    def test_released_freeze_honors_a_standing_operator_pause(self):
        from tests.test_drive import stored

        freeze.write_freeze(
            self.frost, reason="sweep", until=["app#7"], now=1000.0, owned_pause=False
        )
        with open(self.pause, "w", encoding="utf-8") as handle:
            handle.write("operator break\n")
        store = stored(self.db, [("app", 7, "open"), ("app", 8, "open")])
        self.addCleanup(store.close)
        code, out, err, launcher = self.passing(
            store,
            plan=False,
            found=[("blue", [("app", 8, "start")])],
            tracker_factory=lambda repo: closed_tracker(),
        )
        self.assertEqual(code, 1)
        self.assertIn("operator break", err)
        self.assertEqual(launcher.calls, [])
        self.assertFalse(os.path.exists(self.frost))
        with open(self.pause, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "operator break\n")

    def test_open_until_keeps_the_pass_paused(self):
        from tests.test_drive import stored

        freeze.write_freeze(
            self.frost, reason="sweep", until=["app#7"], now=1000.0, owned_pause=True
        )
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        code, _, err, launcher = self.passing(
            store,
            plan=False,
            found=[("blue", [("app", 7, "start")])],
            tracker_factory=lambda repo: open_tracker(),
        )
        self.assertEqual(code, 1)
        self.assertIn("frozen: sweep", err)
        self.assertEqual(launcher.calls, [])
        self.assertTrue(os.path.exists(self.frost))

    def test_plan_never_releases_a_merged_freeze(self):
        from tests.test_drive import stored

        freeze.write_freeze(
            self.frost, reason="sweep", until=["app#7"], now=1000.0, owned_pause=True
        )
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        code, _, _, _ = self.passing(
            store,
            plan=True,
            found=[("blue", [("app", 7, "start")])],
            tracker_factory=lambda repo: closed_tracker(),
        )
        self.assertEqual(code, 1)
        self.assertTrue(os.path.exists(self.frost))


class DriveStatusFreezeTest(unittest.TestCase):
    def test_status_names_the_freeze_and_its_until(self):
        import json as _json

        from korapy import drive

        with tempfile.TemporaryDirectory(prefix="korapy-freeze-status-") as tmp:
            journal = os.path.join(tmp, "journal.jsonl")
            with open(journal, "w", encoding="utf-8") as handle:
                handle.write(_json.dumps({"event": "pass_start", "at": 1700000000}) + "\n")
                handle.write(_json.dumps({"event": "pass_end", "at": 1700000000, "exit": 0}) + "\n")
            frost = os.path.join(tmp, "drive.freeze")
            freeze.write_freeze(
                frost, reason="sweep", until=["app#7"], now=1000.0, owned_pause=True
            )
            status = drive.drive_status(
                now=4600.0,
                max_starts=50,
                journal_path=journal,
                pause_path=os.path.join(tmp, "drive.pause"),
                freeze_path=frost,
                day_path=os.path.join(tmp, "starts.json"),
                lock_path=os.path.join(tmp, "drive.lock"),
            )
        self.assertTrue(status["paused"])
        self.assertTrue(status["frozen"])
        self.assertEqual(status["pause_reason"], "frozen: sweep")
        self.assertEqual(status["freeze_until"], ["app#7"])
        self.assertEqual(status["freeze_file"], freeze.FREEZE_NAME)

    def test_corrupt_freeze_holds_the_pause_and_names_itself(self):
        import json as _json

        from korapy import drive

        with tempfile.TemporaryDirectory(prefix="korapy-freeze-status-") as tmp:
            journal = os.path.join(tmp, "journal.jsonl")
            with open(journal, "w", encoding="utf-8") as handle:
                handle.write(_json.dumps({"event": "pass_start", "at": 1700000000}) + "\n")
                handle.write(_json.dumps({"event": "pass_end", "at": 1700000000, "exit": 0}) + "\n")
            frost = os.path.join(tmp, "drive.freeze")
            with open(frost, "w", encoding="utf-8") as handle:
                handle.write("{not json")
            status = drive.drive_status(
                now=4600.0,
                max_starts=50,
                journal_path=journal,
                pause_path=os.path.join(tmp, "drive.pause"),
                freeze_path=frost,
                day_path=os.path.join(tmp, "starts.json"),
                lock_path=os.path.join(tmp, "drive.lock"),
            )
        self.assertTrue(status["paused"])
        self.assertTrue(status["frozen"])
        self.assertIn("freeze unreadable", status["pause_reason"])
        self.assertIsNotNone(status["freeze_error"])

    def test_operator_pause_wins_over_a_corrupt_freeze_in_status(self):
        import json as _json

        from korapy import drive

        with tempfile.TemporaryDirectory(prefix="korapy-freeze-status-") as tmp:
            journal = os.path.join(tmp, "journal.jsonl")
            with open(journal, "w", encoding="utf-8") as handle:
                handle.write(_json.dumps({"event": "pass_start", "at": 1700000000}) + "\n")
                handle.write(_json.dumps({"event": "pass_end", "at": 1700000000, "exit": 0}) + "\n")
            pause = os.path.join(tmp, "drive.pause")
            with open(pause, "w", encoding="utf-8") as handle:
                handle.write("operator break\n")
            frost = os.path.join(tmp, "drive.freeze")
            with open(frost, "w", encoding="utf-8") as handle:
                handle.write("{not json")
            status = drive.drive_status(
                now=4600.0,
                max_starts=50,
                journal_path=journal,
                pause_path=pause,
                freeze_path=frost,
                day_path=os.path.join(tmp, "starts.json"),
                lock_path=os.path.join(tmp, "drive.lock"),
            )
        self.assertTrue(status["paused"])
        self.assertTrue(status["frozen"])
        self.assertEqual(status["pause_reason"], "operator break")
        self.assertIsNotNone(status["freeze_error"])

    def test_status_without_a_freeze_is_not_frozen(self):
        import json as _json

        from korapy import drive

        with tempfile.TemporaryDirectory(prefix="korapy-freeze-status-") as tmp:
            journal = os.path.join(tmp, "journal.jsonl")
            with open(journal, "w", encoding="utf-8") as handle:
                handle.write(_json.dumps({"event": "pass_start", "at": 1700000000}) + "\n")
                handle.write(_json.dumps({"event": "pass_end", "at": 1700000000, "exit": 0}) + "\n")
            status = drive.drive_status(
                now=4600.0,
                max_starts=50,
                journal_path=journal,
                pause_path=os.path.join(tmp, "drive.pause"),
                freeze_path=os.path.join(tmp, "drive.freeze"),
                day_path=os.path.join(tmp, "starts.json"),
                lock_path=os.path.join(tmp, "drive.lock"),
            )
        self.assertFalse(status["paused"])
        self.assertFalse(status["frozen"])
        self.assertEqual(status["freeze_until"], [])


class DoctorFreezeRowTest(unittest.TestCase):
    def test_no_reading_is_no_row(self):
        from korapy import doctor

        self.assertEqual(doctor.check_freeze(None, 86400), [])

    def test_fresh_freeze_is_ok(self):
        from korapy import doctor

        rows = doctor.check_freeze({"reason": "sweep", "until": ["app#7"], "age_s": 60}, 86400)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].name, "drive-freeze")
        self.assertEqual(rows[0].status, "ok")
        self.assertIn("sweep", rows[0].detail)

    def test_stale_freeze_warns_with_the_resume_fix(self):
        from korapy import doctor

        rows = doctor.check_freeze({"reason": "sweep", "until": [], "age_s": 90000}, 86400)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].status, "warn")
        self.assertIn("freeze end", rows[0].fix)

    def test_unreadable_freeze_fails(self):
        from korapy import doctor

        rows = doctor.check_freeze(RuntimeError("no permission"), 86400)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].status, "fail")


class WebFreezeCardTest(unittest.TestCase):
    def test_drive_card_names_the_snapshot_freeze_file(self):
        import re
        from importlib import resources

        views = resources.files("korapy").joinpath("web/views.js").read_text(encoding="utf-8")
        match = re.search(r"function driveCard\(.*?\n\}", views, re.DOTALL)
        assert match is not None, "no function driveCard"
        self.assertIn("freeze_file", match.group(0))

    def test_no_hardcoded_freeze_file_name_in_the_dashboard(self):
        from importlib import resources

        views = resources.files("korapy").joinpath("web/views.js").read_text(encoding="utf-8")
        self.assertNotIn("drive.freeze", views)


if __name__ == "__main__":
    unittest.main()
