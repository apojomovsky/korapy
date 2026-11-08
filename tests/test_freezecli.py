"""The `korapy freeze` verb: hold the loop while agents drain. Fakes only.

The drain listing, the clock and the sleep arrive through injected
seams; the freeze and pause files live in temporary directories, never
the real state dir. No units, no tracker, no subprocess.
"""

import argparse
import io
import json
import os
import tempfile
import unittest

from korapy import freezecli


def start_args(until=(), reason="sweep"):
    return argparse.Namespace(freeze_verb="start", until=list(until), reason=reason)


def end_args():
    return argparse.Namespace(freeze_verb="end")


def files(tmp):
    return {
        "freeze_file": os.path.join(tmp, "drive.freeze"),
        "pause_file": os.path.join(tmp, "drive.pause"),
    }


def scripted(counts, sleeps):
    live = iter(counts)

    def live_total():
        return next(live)

    def sleep(seconds):
        sleeps.append(seconds)

    return live_total, sleep


def read_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


class FreezeStartTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="korapy-freeze-cli-")
        self.addCleanup(self.tmp.cleanup)
        self.paths = files(self.tmp.name)

    def run_start(self, args, counts, now=2000.0):
        out, err, sleeps = io.StringIO(), io.StringIO(), []
        live_total, sleep = scripted(counts, sleeps)
        code = freezecli.cmd_freeze(
            args,
            live_total=live_total,
            sleep=sleep,
            now=now,
            out=out,
            err=err,
            **self.paths,
        )
        return code, out.getvalue(), err.getvalue(), sleeps

    def test_start_writes_both_files_and_reports_frozen(self):
        code, out, _, sleeps = self.run_start(start_args(), [0])
        self.assertEqual(code, 0)
        self.assertIn("frozen", out)
        self.assertEqual(sleeps, [])
        record = read_json(self.paths["freeze_file"])
        self.assertEqual(record["reason"], "sweep")
        self.assertEqual(record["until"], [])
        self.assertTrue(record["owned_pause"])
        with open(self.paths["pause_file"], encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "frozen: sweep\n")

    def test_start_records_its_until_tickets(self):
        code, _, _, _ = self.run_start(start_args(until=["app#7", "app#8"]), [0])
        self.assertEqual(code, 0)
        record = read_json(self.paths["freeze_file"])
        self.assertEqual(record["until"], ["app#7", "app#8"])

    def test_start_waits_one_pass_per_live_report(self):
        code, out, _, sleeps = self.run_start(start_args(), [2, 1, 0])
        self.assertEqual(code, 0)
        self.assertEqual(len(sleeps), 2)
        self.assertIn("2", out)
        self.assertIn("1", out)

    def test_start_refuses_when_a_freeze_is_already_active(self):
        with open(self.paths["freeze_file"], "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"reason": "x"}) + "\n")
        code, out, _, sleeps = self.run_start(start_args(), [0])
        self.assertEqual(code, 2)
        self.assertEqual(sleeps, [])
        self.assertFalse(os.path.exists(self.paths["pause_file"]))

    def test_start_leaves_an_operator_pause_file_alone(self):
        with open(self.paths["pause_file"], "w", encoding="utf-8") as handle:
            handle.write("operator break\n")
        code, _, _, _ = self.run_start(start_args(), [0])
        self.assertEqual(code, 0)
        with open(self.paths["pause_file"], encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "operator break\n")
        self.assertFalse(read_json(self.paths["freeze_file"])["owned_pause"])

    def test_start_refuses_a_bad_until_ref(self):
        code, _, err, sleeps = self.run_start(start_args(until=["bogus"]), [0])
        self.assertEqual(code, 4)
        self.assertIn("bogus", err)
        self.assertEqual(sleeps, [])
        self.assertFalse(os.path.exists(self.paths["freeze_file"]))
        self.assertFalse(os.path.exists(self.paths["pause_file"]))

    def test_drain_failure_rolls_the_start_back(self):
        from korapy import units

        def failing():
            raise units.Unreachable("cannot list units: boom")

        out, err = io.StringIO(), io.StringIO()
        code = freezecli.cmd_freeze(
            start_args(),
            live_total=failing,
            sleep=lambda seconds: None,
            now=2000.0,
            out=out,
            err=err,
            **self.paths,
        )
        self.assertEqual(code, 3)
        self.assertIn("rolled back", err.getvalue())
        self.assertFalse(os.path.exists(self.paths["freeze_file"]))
        self.assertFalse(os.path.exists(self.paths["pause_file"]))

    def test_drain_failure_keeps_an_operator_pause(self):
        from korapy import units

        def failing():
            raise units.Unreachable("cannot list units: boom")

        with open(self.paths["pause_file"], "w", encoding="utf-8") as handle:
            handle.write("operator break\n")
        out, err = io.StringIO(), io.StringIO()
        code = freezecli.cmd_freeze(
            start_args(),
            live_total=failing,
            sleep=lambda seconds: None,
            now=2000.0,
            out=out,
            err=err,
            **self.paths,
        )
        self.assertEqual(code, 3)
        self.assertFalse(os.path.exists(self.paths["freeze_file"]))
        with open(self.paths["pause_file"], encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "operator break\n")


class FreezeEndTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="korapy-freeze-cli-")
        self.addCleanup(self.tmp.cleanup)
        self.paths = files(self.tmp.name)

    def run_end(self):
        out, err = io.StringIO(), io.StringIO()
        code = freezecli.cmd_freeze(end_args(), out=out, err=err, **self.paths)
        return code, out.getvalue(), err.getvalue()

    def start_owned(self):
        out, err = io.StringIO(), io.StringIO()
        code = freezecli.cmd_freeze(
            start_args(),
            live_total=lambda: 0,
            sleep=lambda s: None,
            now=2000.0,
            out=out,
            err=err,
            **self.paths,
        )
        self.assertEqual(code, 0)

    def test_end_without_a_freeze_is_nothing_to_do(self):
        code, out, _ = self.run_end()
        self.assertEqual(code, 1)
        self.assertIn("no freeze", out)

    def test_end_removes_the_freeze_and_its_pause(self):
        self.start_owned()
        code, out, _ = self.run_end()
        self.assertEqual(code, 0)
        self.assertIn("resumed", out)
        self.assertFalse(os.path.exists(self.paths["freeze_file"]))
        self.assertFalse(os.path.exists(self.paths["pause_file"]))

    def test_end_without_a_pause_file_still_resumes(self):
        self.start_owned()
        os.remove(self.paths["pause_file"])
        code, out, _ = self.run_end()
        self.assertEqual(code, 0)
        self.assertIn("resumed", out)
        self.assertFalse(os.path.exists(self.paths["freeze_file"]))

    def test_end_leaves_an_operator_pause_file_in_place(self):
        self.start_owned()
        with open(self.paths["pause_file"], "w", encoding="utf-8") as handle:
            handle.write("operator break\n")
        with open(self.paths["freeze_file"], encoding="utf-8") as handle:
            record = json.load(handle)
        record["owned_pause"] = False
        with open(self.paths["freeze_file"], "w", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
        code, _, err = self.run_end()
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(self.paths["freeze_file"]))
        with open(self.paths["pause_file"], encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "operator break\n")
        self.assertIn("left in place", err)

    def test_end_with_a_corrupt_freeze_is_a_usage_error(self):
        self.start_owned()
        with open(self.paths["freeze_file"], "w", encoding="utf-8") as handle:
            handle.write("{not json")
        code, _, err = self.run_end()
        self.assertEqual(code, 4)
        self.assertIn("freeze", err)
        self.assertTrue(os.path.exists(self.paths["freeze_file"]))


if __name__ == "__main__":
    unittest.main()
