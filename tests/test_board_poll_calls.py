"""The board poll REST budget: no per-item call for an unchanged issue.

A board-mode pass used to spend one REST call per board item (a fresh
``read_issue`` for every converted node), so a board with a few hundred
items drained the shared hourly REST budget on every pass. The poll now
reuses the stored issue facts for an item whose issue has not changed
since the last pass, keyed by the issue's ``updated_at`` carried on the
board node itself, and reads live exactly the items the snapshot cannot
cover: first sightings, changed issues, and rows the snapshot cannot
parse. Fakes only: canned GraphQL pages and REST payloads at the
injected ``run`` seam, a temporary store, no network, no real state
dir.
"""

import json
import os
import re
import tempfile
import unittest

from korapy import board as store
from korapy import sync
from korapy import tracker as iface
from korapy.github import board as boardreader
from korapy.trackers import BoardBackedTracker
from tests.test_board_fields import FakePacer
from tests.test_board_reader import (
    BoardFakeRun,
    config,
    full_schema_fields,
    item_node,
    items_payload,
    rest_issue,
    schema_payload,
    text_value,
)
from tests.test_board_sync import FakeBoardReader
from tests.test_board_sync import REPO as SYNC_REPO
from tests.test_board_sync import schema as sync_schema
from tests.test_tracker import item_payload, reading

REPO = "example/demo"
ISSUE_T = "2026-10-28T12:00:00Z"
NEW_T = "2026-10-28T13:00:00Z"
T0 = iface.parse_cursor(ISSUE_T)
T1 = iface.parse_cursor(NEW_T)

#: The per-pass REST bound for a quiet 400-item board: the feed page,
#: the adopt listing pages, and zero per-item reads. A regression that
#: reintroduces one call per item fails at 400.
QUIET_PASS_REST_BOUND = 20

_SINGLE_RE = re.compile(r"repos/[^/]+/[^/]+/issues/\d+")


def rest_calls(run):
    """Every non-GraphQL call the fake transport saw, in order."""
    return [args for args, _ in run.calls if args[1] != "graphql"]


def single_issue_numbers(run):
    """The issue numbers read through the per-item REST route, in order."""
    return [args[1].rsplit("/", 1)[-1] for args in rest_calls(run) if _SINGLE_RE.fullmatch(args[1])]


def stored_row(number, updated_at=T0, extra=None, kind="issue"):
    """One stored issue row as the snapshot sees it: full issue facts."""
    if extra is None:
        extra = {"milestone": None, "milestone_due": None, "milestone_closed": None}
    return store.Item(
        repo=REPO,
        number=number,
        kind=kind,
        title=f"Title {number}",
        body=f"Body {number}",
        state="open",
        author="alice",
        labels=["ready", "priority:high"],
        priority="high",
        assignees=["bob"],
        url=f"https://example.com/{REPO}/{number}",
        tracker_updated_at=updated_at,
        extra=extra,
    )


def node(number, board_time=ISSUE_T, issue_time=ISSUE_T, values=()):
    return item_node(number, repo=REPO, updated=board_time, values=values, issue_updated=issue_time)


def poll_with(run, nodes, stored, max_pages=10, page_size=100):
    """One real poll over scripted pages, through the stored snapshot."""
    run.script("BoardSchemaByNumber", [schema_payload(*full_schema_fields())])
    pages = [nodes[i : i + page_size] for i in range(0, len(nodes), page_size)] or [[]]
    run.script(
        "BoardItems",
        [items_payload(page, end=f"C{i}", more=i < len(pages) - 1) for i, page in enumerate(pages)],
    )
    reader = boardreader.BoardReader(config(), run, pacer=FakePacer())
    return reader.poll(reader.schema(), max_pages=max_pages, stored=stored)


class UnchangedSkipTest(unittest.TestCase):
    def test_unchanged_issues_cost_no_rest_call(self):
        run = BoardFakeRun()
        stored = {(REPO, number): stored_row(number) for number in (1, 2, 3)}
        found, _ = poll_with(
            run,
            [node(1, values=[text_value("F_agent", "Agent", "amy")]), node(2), node(3)],
            stored,
        )
        self.assertEqual([item.number for item in found], [1, 2, 3])
        # The issue facts come from the snapshot, the board cell overlays live.
        self.assertEqual(found[0].title, "Title 1")
        self.assertEqual(found[0].agent, "amy")
        self.assertEqual(found[0].labels, ("ready", "priority:high"))
        self.assertEqual(found[0].updated_at, T0)
        self.assertEqual(rest_calls(run), [])

    def test_changed_issue_is_reread_exactly_once(self):
        run = BoardFakeRun()
        run.add_rest(
            f"repos/{REPO}/issues/2",
            item_payload(
                2,
                title="New title",
                body="Work.\nDepends on #3",
                updated_at=NEW_T,
                milestone={"title": "M1", "due_on": "2026-12-05T00:00:00Z", "state": "open"},
            ),
        )
        stored = {(REPO, number): stored_row(number) for number in (1, 2, 3)}
        found, _ = poll_with(run, [node(1), node(2, issue_time=NEW_T), node(3)], stored)
        self.assertEqual(single_issue_numbers(run), ["2"])
        changed = [item for item in found if item.number == 2][0]
        self.assertEqual(changed.title, "New title")
        self.assertEqual(changed.extra["milestone"], "M1")
        self.assertEqual(changed.extra["blocked_by"], [f"{REPO}#3"])
        self.assertEqual(changed.updated_at, T1)
        # The untouched items still come from the snapshot.
        self.assertEqual([item.title for item in found], ["Title 1", "New title", "Title 3"])

    def test_first_sync_with_empty_snapshot_reads_each_item_once(self):
        run = BoardFakeRun()
        run.rest_fallback = lambda path: json.dumps(
            item_payload(int(path.rsplit("/", 1)[-1]), updated_at=ISSUE_T)
        )
        found, _ = poll_with(run, [node(1), node(2), node(3)], {})
        self.assertEqual([item.number for item in found], [1, 2, 3])
        self.assertEqual(sorted(single_issue_numbers(run)), ["1", "2", "3"])

    def test_board_only_edit_skips_rest_and_moves_row_forward(self):
        run = BoardFakeRun()
        stored = {(REPO, 1): stored_row(1)}
        found, _ = poll_with(
            run,
            [node(1, board_time=NEW_T, values=[text_value("F_agent", "Agent", "amy")])],
            stored,
        )
        self.assertEqual(rest_calls(run), [])
        self.assertEqual(found[0].updated_at, T1)
        self.assertEqual(found[0].agent, "amy")
        self.assertEqual(found[0].title, "Title 1")

    def test_stale_snapshot_rereads_live(self):
        run = BoardFakeRun()
        rest_issue(run, 1, title="Fresh title", updated_at=NEW_T)
        stored = {(REPO, 1): stored_row(1, updated_at=T0)}
        found, _ = poll_with(run, [node(1, issue_time=NEW_T)], stored)
        self.assertEqual(single_issue_numbers(run), ["1"])
        self.assertEqual(found[0].title, "Fresh title")

    def test_stored_board_concepts_survive_the_reuse(self):
        run = BoardFakeRun()
        stored = {
            (REPO, 1): stored_row(1, extra={"board_status": "In progress"}),
        }
        found, _ = poll_with(run, [node(1)], stored)
        self.assertEqual(single_issue_numbers(run), [])
        self.assertEqual(found[0].title, "Title 1")

    def test_duplicate_nodes_for_one_issue_share_the_snapshot_row(self):
        run = BoardFakeRun()
        stored = {(REPO, 5): stored_row(5)}
        found, _ = poll_with(
            run,
            [
                node(5, values=[text_value("F_agent", "Agent", "amy")]),
                node(5, board_time=NEW_T, values=[text_value("F_agent", "Agent", "bob")]),
            ],
            stored,
        )
        self.assertEqual(single_issue_numbers(run), [])
        self.assertEqual([item.agent for item in found], ["amy", "bob"])
        self.assertEqual(found[1].updated_at, T1)

    def test_snapshot_partial_milestone_rereads_live(self):
        run = BoardFakeRun()
        rest_issue(run, 1, updated_at=ISSUE_T)
        stored = {(REPO, 1): stored_row(1, extra={"milestone": "M1"})}
        found, _ = poll_with(run, [node(1)], stored)
        self.assertEqual(single_issue_numbers(run), ["1"])
        self.assertEqual(found[0].title, "Title 1")

    def test_non_issue_snapshot_rereads_live(self):
        run = BoardFakeRun()
        rest_issue(run, 1, updated_at=ISSUE_T)
        stored = {(REPO, 1): stored_row(1, kind="pr")}
        found, _ = poll_with(run, [node(1)], stored)
        self.assertEqual(single_issue_numbers(run), ["1"])
        self.assertEqual(found[0].title, "Title 1")

    def test_missing_issue_time_with_snapshot_is_a_tracker_error(self):
        run = BoardFakeRun()
        stored = {(REPO, 1): stored_row(1)}
        with self.assertRaises(iface.TrackerError):
            poll_with(run, [item_node(1, repo=REPO, updated=ISSUE_T)], stored)


class QuietPassBudgetTest(unittest.TestCase):
    """A quiet 400-item board pass costs a constant number of REST calls.

    Two full passes (feed plus board poll plus adopt) through the real
    reader and the real sync step over one fake transport: the first
    pass stocks the store through the bulk listings, the second pass
    changes nothing, and the bound counts every REST call the second
    pass spends. One raced edit the feed missed is re-read exactly
    once.
    """

    COUNT = 400

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="korapy-board-budget-")
        self.addCleanup(self.tmp.cleanup)
        self.store = store.open_store(os.path.join(self.tmp.name, "board.sqlite"))
        self.addCleanup(self.store.close)
        self.fake = BoardFakeRun()
        self.feed_changed: list = []
        self.singles: dict = {}
        self.fake.rest_fallback = self.route

    def route(self, path):
        prefix = f"repos/{REPO}/issues"
        if path.startswith(prefix + "?"):
            page = 1
            for part in path.split("&"):
                if part.startswith("page="):
                    page = int(part[len("page=") :])
            if "since=" in path:
                return json.dumps(self.feed_changed)
            entries = self.feed_entries if "sort=updated" in path else self.adopt_entries
            return json.dumps(entries[(page - 1) * 100 : page * 100])
        number = path.rsplit("/", 1)[-1]
        return json.dumps(self.singles[number])

    def script_pass(self, nodes):
        self.fake.script("BoardSchemaByNumber", [schema_payload(*full_schema_fields())])
        pages = [nodes[i : i + 100] for i in range(0, len(nodes), 100)]
        self.fake.script(
            "BoardItems",
            [
                items_payload(page, end=f"C{i}", more=i < len(pages) - 1)
                for i, page in enumerate(pages)
            ],
        )

    def tracker(self):
        return BoardBackedTracker(
            config(), self.fake, pacer=FakePacer(), max_pages=5, adopt_limit=20
        )

    def sync_pass(self, nodes, now):
        tracker = self.tracker()
        feed = sync.sync_repo(
            self.store,
            tracker,
            REPO,
            operators=["alice"],
            now=now,
            cursor=self.store.get_cursor(REPO, sync.CURSOR_NAME),
        )
        schema = tracker.board_schema()
        board_result = sync.sync_board_repo(
            self.store, tracker, schema, REPO, operators=["alice"], now=now, max_pages=5
        )
        return feed, board_result

    def test_quiet_second_pass_costs_a_constant(self):
        numbers = list(range(1, self.COUNT + 1))
        self.feed_entries = [
            item_payload(n, updated_at=ISSUE_T, node_id=f"NODE_{n}") for n in numbers
        ]
        self.adopt_entries = [
            {"number": n, "node_id": f"NODE_{n}", "state": "open"} for n in numbers
        ]
        self.singles = {str(n): self.feed_entries[n - 1] for n in numbers}
        nodes = [node(n) for n in numbers]

        self.script_pass(nodes)
        feed, board_result = self.sync_pass(nodes, T0)
        self.assertEqual(feed.errors, ())
        self.assertEqual(board_result.errors, ())
        self.assertEqual(board_result.items_seen, self.COUNT)
        # The first pass stocks the store through the bulk listings: the
        # feed already carried every issue, so the board poll reads none
        # of them a second time.
        self.assertEqual(single_issue_numbers(self.fake), [])

        calls_before = len(self.fake.calls)
        self.feed_changed = []
        self.script_pass(nodes)
        feed, board_result = self.sync_pass(nodes, T0 + 300)
        second = self.fake.calls[calls_before:]
        rest = [args for args, _ in second if args[1] != "graphql"]
        singles = [args[1] for args in rest if _SINGLE_RE.fullmatch(args[1])]
        self.assertEqual(board_result.errors, ())
        self.assertEqual(board_result.items_seen, self.COUNT)
        self.assertEqual(singles, [])
        self.assertLess(len(rest), QUIET_PASS_REST_BOUND)

    def test_raced_change_the_feed_missed_is_reread_exactly_once(self):
        numbers = list(range(1, 11))
        self.feed_entries = [
            item_payload(n, updated_at=ISSUE_T, node_id=f"NODE_{n}") for n in numbers
        ]
        self.adopt_entries = [
            {"number": n, "node_id": f"NODE_{n}", "state": "open"} for n in numbers
        ]
        self.singles = {str(n): self.feed_entries[n - 1] for n in numbers}
        self.script_pass([node(n) for n in numbers])
        self.sync_pass([node(n) for n in numbers], T0)

        changed_payload = item_payload(
            7,
            title="Raced title",
            updated_at=NEW_T,
            node_id="NODE_7",
            milestone={"title": "M1", "due_on": "2026-12-05T00:00:00Z", "state": "open"},
        )
        self.singles["7"] = changed_payload
        nodes = [node(7, board_time=NEW_T, issue_time=NEW_T)] + [node(n) for n in numbers if n != 7]
        self.feed_changed = []
        calls_before = len(self.fake.calls)
        self.script_pass(nodes)
        _, board_result = self.sync_pass(nodes, T0 + 300)
        fresh = self.fake.calls[calls_before:]
        singles = [args[1] for args, _ in fresh if _SINGLE_RE.fullmatch(args[1])]
        self.assertEqual(singles, [f"repos/{REPO}/issues/7"])
        self.assertEqual(board_result.errors, ())
        row, _ = self.store.item(REPO, 7, now=T0 + 300)
        self.assertEqual(row.title, "Raced title")
        self.assertEqual(row.extra["milestone"], "M1")


class SnapshotWiringTest(unittest.TestCase):
    """The sync step hands the poll its stored snapshot, or nothing usable."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="korapy-board-snapshot-")
        self.addCleanup(self.tmp.cleanup)
        self.store = store.open_store(os.path.join(self.tmp.name, "board.sqlite"))
        self.addCleanup(self.store.close)

    def seed(self, number, updated_at):
        self.store.upsert_item(
            store.Item(
                repo=SYNC_REPO,
                number=number,
                title="Title",
                body="Body",
                tracker_updated_at=updated_at,
            ),
            operators=["alice"],
            now=updated_at,
        )

    def test_poll_receives_the_stored_snapshot(self):
        self.seed(1, T0)
        self.seed(2, T0)
        reader = FakeBoardReader([reading(repo=SYNC_REPO, number=1, updated_at=T0)])
        result = sync.sync_board_repo(
            self.store, reader, sync_schema(), SYNC_REPO, operators=["alice"], now=T0, max_pages=5
        )
        self.assertEqual(result.errors, ())
        snapshot = reader.calls[0]["stored"]
        self.assertEqual(set(snapshot), {(SYNC_REPO, 1), (SYNC_REPO, 2)})
        self.assertEqual(snapshot[(SYNC_REPO, 1)].tracker_updated_at, T0)

    def test_corrupt_store_falls_back_to_live_reads(self):
        self.seed(1, T0)
        self.store._conn.execute(
            "UPDATE items SET extra='{' WHERE repo=? AND number=?", (SYNC_REPO, 1)
        )
        self.store._conn.commit()
        reader = FakeBoardReader([reading(repo=SYNC_REPO, number=1, updated_at=T0)])
        result = sync.sync_board_repo(
            self.store, reader, sync_schema(), SYNC_REPO, operators=["alice"], now=T0, max_pages=5
        )
        self.assertEqual(reader.calls[0]["stored"], None)
        self.assertEqual(result.errors, ())
        self.assertEqual(len(result.store_errors), 1)
