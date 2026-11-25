"""Ticket 669: the bounded, read-only agent peek.

One reader over the session files with a hard cap on bytes and
lines, timestamps in local time, tool calls and results shortened,
and missing or unreadable sessions reading not available. Offline:
every session file is a temp file written by the test.
"""

import json
import os
import tempfile
import unittest

from korapy import peek
from tests.test_serve import FakeBuilder, ServeCase, healthy_doc


def omp_line(text, **extra):
    row = {
        "type": "message",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }
    row.update(extra)
    return json.dumps(row)


def omp_call(**extra):
    row = {
        "type": "message",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "toolCall",
                    "name": "bash",
                    "intent": "Running the suite",
                    "arguments": {"command": "make test " + "x" * 500},
                }
            ],
        },
    }
    row.update(extra)
    return json.dumps(row)


class PeekFile(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(self._clean)

    def _clean(self):
        import shutil

        shutil.rmtree(self.dir, ignore_errors=True)

    def path(self, *lines, tail_text=""):
        target = os.path.join(self.dir, "session.jsonl")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("".join(line + "\n" for line in lines) + tail_text)
        return target

    def test_ready_lines_carry_local_timestamps(self):
        target = self.path(omp_line("hello", timestamp=1_800_000_000.0))
        got = peek.read_peek_file(target)
        self.assertEqual(got["state"], "ready")
        self.assertEqual(len(got["lines"]), 1)
        self.assertEqual(got["lines"][0]["text"], "hello")
        self.assertIn("T", got["lines"][0]["at"])
        self.assertRegex(got["lines"][0]["at"], r"[+-]\d{2}:\d{2}$")

    def test_missing_stamp_reads_unknown_never_a_guess(self):
        target = self.path(omp_line("hello"))
        got = peek.read_peek_file(target)
        self.assertEqual(got["lines"][0]["at"], "unknown")

    def test_long_stamp_is_capped(self):
        target = self.path(omp_line("hello", timestamp="2026-11-07T19:42:16-03:00" + "x" * 200))
        got = peek.read_peek_file(target)
        self.assertLessEqual(len(got["lines"][0]["at"]), peek.STAMP_WIDTH)

    def test_iso_stamp_passes_through(self):
        target = self.path(omp_line("hello", timestamp="2026-11-07T19:42:16-03:00"))
        got = peek.read_peek_file(target)
        self.assertEqual(got["lines"][0]["at"], "2026-11-07T19:42:16-03:00")

    def test_tool_calls_are_shortened_to_one_capped_line(self):
        target = self.path(omp_call(timestamp=1_800_000_000.0))
        got = peek.read_peek_file(target)
        self.assertEqual(len(got["lines"]), 1)
        self.assertLessEqual(len(got["lines"][0]["text"]), peek.LINE_WIDTH)
        self.assertIn("Running the suite", got["lines"][0]["text"])

    def test_line_cap_keeps_the_newest_and_marks_truncation(self):
        target = self.path(*[omp_line(f"line {n}") for n in range(10)])
        got = peek.read_peek_file(target, max_lines=3)
        self.assertEqual([row["text"] for row in got["lines"]], ["line 7", "line 8", "line 9"])
        self.assertTrue(got["truncated"])

    def test_byte_cap_reads_only_the_tail_and_marks_truncation(self):
        target = self.path(*[omp_line(f"line {n} " + "y" * 200) for n in range(10)])
        got = peek.read_peek_file(target, max_bytes=400)
        self.assertTrue(got["truncated"])
        self.assertLessEqual(len(got["lines"]), 3)
        self.assertTrue(got["lines"][-1]["text"].startswith("line 9 "))
        self.assertLessEqual(len(got["lines"][-1]["text"]), peek.LINE_WIDTH)

    def test_partial_last_line_is_never_shown(self):
        target = self.path(omp_line("complete"), tail_text=omp_line("half"))
        got = peek.read_peek_file(target)
        self.assertEqual([row["text"] for row in got["lines"]], ["complete"])

    def test_missing_file_is_not_available(self):
        got = peek.read_peek_file(os.path.join(self.dir, "gone.jsonl"))
        self.assertEqual(got, {"state": "not-available", "lines": [], "truncated": False})

    def test_unreadable_entries_leave_no_lines_but_stay_ready(self):
        target = self.path("not json", json.dumps({"type": "custom"}))
        got = peek.read_peek_file(target)
        self.assertEqual(got["state"], "ready")
        self.assertEqual(got["lines"], [])

    def test_caps_are_hard_bounds(self):
        self.assertGreater(peek.MAX_LINES, 0)
        self.assertGreater(peek.MAX_BYTES, 0)
        self.assertLessEqual(peek.MAX_BYTES, 1024 * 1024)


class DoctorRecord(unittest.TestCase):
    """The cached doctor run: one writer, one reader, no checks run."""

    def test_record_round_trips_and_missing_reads_none(self):
        import tempfile
        from unittest import mock

        from korapy import doctorrecord

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict("os.environ", {"KORAPY_STATE": tmp}):
                self.assertIsNone(doctorrecord.load_last_checks())
                doctorrecord.record_last_result(
                    [{"name": "deps:x", "status": "fail", "detail": "gone", "fix": "install x"}],
                    at=1_800_000_000.0,
                )
                got = doctorrecord.load_last_checks()
        self.assertEqual(
            got, [{"name": "deps:x", "status": "fail", "detail": "gone", "fix": "install x"}]
        )

    def test_corrupt_record_raises_for_the_caller_to_read_unknown(self):
        import tempfile
        from unittest import mock

        from korapy import doctorrecord

        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "doctor-last.json")
            with open(target, "w", encoding="utf-8") as handle:
                handle.write("not json")
            with mock.patch.dict("os.environ", {"KORAPY_STATE": tmp}):
                with self.assertRaises(ValueError):
                    doctorrecord.load_last_checks()


class PeekTicket(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(self._clean)

    def _clean(self):
        import shutil

        shutil.rmtree(self.root, ignore_errors=True)

    def leaf(self, repo="app", number=7, role="worker", gen=0):
        ticket = os.path.join(self.root, f"{repo}-{number}")
        leaf = os.path.join(ticket, f"{role}-{gen}")
        os.makedirs(leaf, exist_ok=True)
        with open(os.path.join(leaf, "prompt.md"), "w", encoding="utf-8") as handle:
            handle.write("prompt")
        return leaf

    def test_ticket_peek_reads_the_newest_worker_session(self):
        leaf = self.leaf()
        with open(os.path.join(leaf, "chat.jsonl"), "w", encoding="utf-8") as handle:
            handle.write(omp_line("working", timestamp=1_800_000_000.0) + "\n")
        got = peek.peek_for_ticket("app", 7, root=self.root)
        self.assertEqual(got["state"], "ready")
        self.assertEqual(got["lines"][0]["text"], "working")

    def test_newest_generation_and_role_win(self):
        old = self.leaf(gen=0)
        with open(os.path.join(old, "chat.jsonl"), "w", encoding="utf-8") as handle:
            handle.write(omp_line("old run") + "\n")
        new = self.leaf(gen=2)
        with open(os.path.join(new, "chat.jsonl"), "w", encoding="utf-8") as handle:
            handle.write(omp_line("new run") + "\n")
        reviewer = self.leaf(role="reviewer", gen=1)
        with open(os.path.join(reviewer, "chat.jsonl"), "w", encoding="utf-8") as handle:
            handle.write(omp_line("review run") + "\n")
        self.assertEqual(
            peek.peek_for_ticket("app", 7, root=self.root)["lines"][0]["text"], "new run"
        )
        self.assertEqual(
            peek.peek_for_ticket("app", 7, "reviewer", root=self.root)["lines"][0]["text"],
            "review run",
        )

    def test_missing_ticket_is_not_available(self):
        got = peek.peek_for_ticket("app", 99, root=self.root)
        self.assertEqual(got["state"], "not-available")
        self.assertEqual(got["lines"], [])

    def test_session_without_an_activity_file_is_not_available(self):
        self.leaf()
        got = peek.peek_for_ticket("app", 7, root=self.root)
        self.assertEqual(got["state"], "not-available")

    def test_bad_ticket_names_are_not_available_never_an_error(self):
        for repo, number in (("../escape", 7), ("app", 0), ("app", -1)):
            got = peek.peek_for_ticket(repo, number, root=self.root)
            self.assertEqual(got["state"], "not-available", (repo, number))


class FakePeek:
    """An injected peek reader: a canned document, recorded calls."""

    def __init__(self, doc=None, error=None):
        self.doc = doc or {
            "state": "ready",
            "lines": [{"at": "2026-11-07T19:42:16-03:00", "text": "working"}],
            "truncated": False,
        }
        self.error = error
        self.calls = []

    def __call__(self, repo, number, last, role):
        self.calls.append((repo, number, last, role))
        if self.error is not None:
            raise self.error
        return self.doc


class PeekView(ServeCase):
    """The `/v1/peek` route over an injected reader (loopback only)."""

    def start(self, fake, **kwargs):
        kwargs.setdefault("peek_fn", fake)
        return self.start_server(FakeBuilder(healthy_doc()), **kwargs)

    def get_json(self, server, target):
        status, _, body = self.fetch(server, "GET", target)
        return status, json.loads(body)

    def test_peek_returns_the_reader_document(self):
        fake = FakePeek()
        server, _ = self.start(fake)
        status, doc = self.get_json(server, "/v1/peek?repo=app&number=7")
        self.assertEqual(status, 200)
        self.assertEqual(doc["peek"]["state"], "ready")
        self.assertEqual(doc["peek"]["lines"][0]["text"], "working")
        self.assertEqual(fake.calls, [("app", 7, peek.MAX_LINES, None)])

    def test_peek_last_clamps_and_role_passes_through(self):
        fake = FakePeek()
        server, _ = self.start(fake)
        status, _ = self.get_json(server, "/v1/peek?repo=app&number=7&last=999&role=reviewer")
        self.assertEqual(status, 200)
        self.assertEqual(fake.calls, [("app", 7, peek.MAX_LINES, "reviewer")])
        fake.calls.clear()
        status, _ = self.get_json(server, "/v1/peek?repo=app&number=7&last=2")
        self.assertEqual(status, 200)
        self.assertEqual(fake.calls, [("app", 7, 2, None)])

    def test_peek_handler_enforces_the_bounds_on_any_reader(self):
        fake = FakePeek(
            doc={
                "state": "ready",
                "lines": [
                    {"at": "2026-11-07T19:42:16-03:00", "text": "first"},
                    {"at": "2026-11-07T19:42:16-03:00" + "x" * 200, "text": "y" * 500},
                    {"at": "2026-11-07T19:42:16-03:00", "text": "third"},
                ],
                "truncated": False,
            }
        )
        server, _ = self.start(fake)
        status, doc = self.get_json(server, "/v1/peek?repo=app&number=7&last=2")
        self.assertEqual(status, 200)
        self.assertEqual(len(doc["peek"]["lines"]), 2)
        self.assertTrue(doc["peek"]["truncated"])
        self.assertLessEqual(len(doc["peek"]["lines"][0]["text"]), peek.LINE_WIDTH)
        self.assertLessEqual(len(doc["peek"]["lines"][0]["at"]), peek.STAMP_WIDTH)

    def test_peek_not_available_is_a_200_document(self):
        fake = FakePeek(doc={"state": "not-available", "lines": [], "truncated": False})
        server, _ = self.start(fake)
        status, doc = self.get_json(server, "/v1/peek?repo=app&number=99")
        self.assertEqual(status, 200)
        self.assertEqual(doc["peek"]["state"], "not-available")
        self.assertEqual(doc["peek"]["lines"], [])

    def test_peek_needs_repo_number_and_a_valid_last(self):
        fake = FakePeek()
        server, _ = self.start(fake)
        for target in (
            "/v1/peek?repo=app",
            "/v1/peek?number=7",
            "/v1/peek?repo=app&number=seven",
            "/v1/peek?repo=app&number=7&last=many",
            "/v1/peek?repo=app&number=7&last=0",
        ):
            with self.subTest(target=target):
                status, doc = self.get_json(server, target)
                self.assertEqual(status, 400, target)
                self.assertIn("error", doc)
        self.assertEqual(fake.calls, [])

    def test_fixture_peek_answers_the_committed_entry_offline(self):
        from korapy import serve

        reader = serve.fixture_peek("docs/assets/dashboard-fixture.json")
        got = reader("repo-a", 41, peek.MAX_LINES, None)
        self.assertEqual(got["state"], "ready")
        self.assertTrue(got["lines"])
        missing = reader("repo-a", 99, peek.MAX_LINES, None)
        self.assertEqual(missing["state"], "not-available")

    def test_peek_failure_is_a_generic_500(self):
        fake = FakePeek(error=OSError("gone"))
        server, _ = self.start(fake)
        status, doc = self.get_json(server, "/v1/peek?repo=app&number=7")
        self.assertEqual(status, 500)
        self.assertEqual(doc, {"error": "peek unavailable"})


if __name__ == "__main__":
    unittest.main()
