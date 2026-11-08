"""`korapy decision record`: the maintainer's answer as a durable record.

One verb: ``record`` writes the decision file, posts the ticket comment,
removes ``needs-decision`` and appends the logbook entry, through the
same seams the rest of korapy uses (the per-repo tracker from the one
factory, the ops root for files, the coordinator project root for the
logbook). Tests inject all three.

Exit codes: 0 recorded (or already recorded), 4 usage, 3 the tracker is
unreachable.
"""

import argparse
import os
import sys

from korapy import decision

OK, UNREACHABLE, USAGE = 0, 3, 4

AGENT_ID_ENV = "KORAPY_AGENT_ID"


def _err(text):
    print(f"korapy: {text}", file=sys.stderr)


def _holder_and_generation(coord, coord_root):
    """The logbook writer: the lease holder and generation, else manual/0.

    Read-only: a missing or unreadable lease still records, as manual.
    """
    try:
        info = coord.describe(root=coord_root)
    except Exception:
        return os.environ.get(AGENT_ID_ENV, "") or "manual", 0
    holder = info.get("holder") or os.environ.get(AGENT_ID_ENV, "") or "manual"
    generation = info.get("generation")
    return holder, generation if isinstance(generation, int) else 0


def cmd_record(args, *, tracker_factory=None, coord_root=None, now=None) -> int:
    from korapy import coord, reviewrecord, settings, syncli, trackers
    from korapy import tracker as iface

    root = settings.find_root(os.getcwd())
    if root is None:
        _err("korapy decision record needs an ops root (korapy.toml): no korapy.toml upward")
        return USAGE
    try:
        ticket_repo = reviewrecord.local_repo(args.repo, syncli.short_to_tracker(root, syncli._git))
    except ValueError as err:
        _err(f"korapy decision record: {err}")
        return USAGE
    factory = tracker_factory if tracker_factory is not None else trackers.production_trackers
    mapping, problem = factory(root, [ticket_repo], git=syncli._git)
    if problem is not None:
        _err(f"korapy decision record: {problem}")
        return USAGE
    entry = mapping.get(ticket_repo)
    if entry is None:
        _err(f"korapy decision record: unknown repo {args.repo!r} (not a [[repo]] entry)")
        return USAGE
    try:
        resolved_coord_root = (
            coord_root if coord_root is not None else coord.project_root(args.project)
        )
    except coord.UsageError as err:
        _err(f"korapy decision record: {err}")
        return USAGE
    holder, generation = _holder_and_generation(coord, resolved_coord_root)
    try:
        result = decision.record(
            repo=ticket_repo,
            tracker_repo=entry.tracker_repo,
            number=args.number,
            decision=args.decision,
            why=args.why,
            reversible=args.reversible,
            confidence=args.confidence,
            settles=args.settles,
            still_open=args.still_open,
            reopen_when=args.reopen_when,
            follow_ups=tuple(args.follow_up or ()),
            tracker=entry.tracker,
            ops_root=root,
            coord_root=resolved_coord_root,
            holder=holder,
            generation=generation,
            now=now,
        )
    except decision.UsageError as err:
        _err(str(err))
        return USAGE
    except iface.TrackerError as err:
        _err(f"korapy decision record: {err}")
        return UNREACHABLE
    except OSError as err:
        _err(f"korapy decision record: cannot write the record: {err}")
        return USAGE
    if result.already:
        print(f"already recorded in {result.path}")
    else:
        print(f"recorded in {result.path}")
    return OK


def build_decision_parser() -> argparse.ArgumentParser:
    """A standalone `korapy decision` parser (tests; `add_parser` below)."""
    parser = argparse.ArgumentParser(prog="korapy decision", description=__doc__)
    verbs = parser.add_subparsers(dest="decision_verb", required=True)
    p = verbs.add_parser("record", help="record a decision: file, comment, label, logbook")
    _add_record_flags(p)
    return parser


def _add_record_flags(p) -> None:
    p.add_argument("repo", help="the [[repo]] entry name (or its tracker spelling)")
    p.add_argument("number", type=int, help="the ticket number")
    p.add_argument("--decision", required=True, help="the decision line")
    p.add_argument("--why", required=True, help="why this decision")
    p.add_argument("--reversible", choices=decision.REVERSIBLE_CHOICES, default=None)
    p.add_argument("--confidence", choices=decision.CONFIDENCE_CHOICES, default=None)
    p.add_argument("--settles", default=None, help="what this settles for now")
    p.add_argument("--open", dest="still_open", default=None, help="what stays open")
    p.add_argument("--reopen-when", dest="reopen_when", default=None, help="reopen trigger")
    p.add_argument(
        "--follow-up",
        action="append",
        type=int,
        default=[],
        metavar="N",
        help="a follow-up ticket number (repeatable)",
    )
    p.add_argument(
        "--project",
        default=None,
        help="the coordinator project for the logbook entry (or KORAPY_PROJECT)",
    )
    p.set_defaults(func=cmd_record)


def add_parser(sub) -> None:
    """Register `decision` and its verbs on the top-level subparsers."""
    p = sub.add_parser("decision", help="record a maintainer decision durably")
    verbs = p.add_subparsers(dest="decision_verb", required=True)
    record = verbs.add_parser("record", help="record a decision: file, comment, label, logbook")
    _add_record_flags(record)
