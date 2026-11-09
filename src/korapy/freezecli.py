"""`korapy freeze`: drain agents and hold the dispatch loop for a sweep.

`start` holds the drive pause file at once, waits until no agent is
live, and records the freeze beside the pause file; `--until TICKET`
names `REPO#N` refs whose merge ends the freeze by itself on the next
dispatch pass. `end` resumes by hand.

Exit codes: 0 frozen or resumed, 1 nothing to do, 2 a freeze is already
active, 3 the unit listing cannot be read, 4 usage or configuration error.
"""

import argparse
import os
import sys
import time

OK, NO, RACE, UNREACHABLE, USAGE = 0, 1, 2, 3, 4


def _production_live_total():
    """Count live agents across every registered project, from the host."""
    from korapy import launch, projects, registry, units

    registered = projects.load_all()
    cfg = registry.config()
    return lambda: sum(launch.project_live_counts(registered, units.list_units, cfg).values())


def cmd_start(args, *, live_total, sleep, now, out, err, freeze_file, pause_file):
    """Hold the pause, drain the agents, record the freeze."""
    from korapy import freeze, units

    try:
        freeze.parse_until_refs(list(args.until or ()))
    except freeze.FreezeError as problem:
        print(f"korapy freeze: {problem}", file=err)
        return USAGE
    reason = args.reason or ""
    try:
        directory = os.path.dirname(freeze_file)
        if directory:
            os.makedirs(directory, exist_ok=True)
        created = os.open(freeze_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(created)
    except FileExistsError:
        print("korapy freeze: a freeze is already active", file=err)
        return RACE
    except OSError as problem:
        print(f"korapy freeze: cannot write the freeze state: {problem}", file=err)
        return USAGE
    owned = False
    try:
        owned = not os.path.exists(pause_file)
        freeze.write_freeze(
            freeze_file,
            reason=reason,
            until=list(args.until or ()),
            now=now,
            owned_pause=owned,
        )
        if owned:
            with open(pause_file, "w", encoding="utf-8") as handle:
                handle.write(freeze.pause_text(reason) + "\n")
    except OSError as problem:
        _rollback(freeze_file, pause_file, freeze.pause_text(reason), owned, err)
        print(f"korapy freeze: cannot write the freeze state: {problem}", file=err)
        return USAGE
    try:
        freeze.drain(live_total, sleep=sleep, out=out)
    except units.Unreachable as problem:
        _rollback(freeze_file, pause_file, freeze.pause_text(reason), owned, err)
        print(f"korapy freeze: {problem} (freeze rolled back)", file=err)
        return UNREACHABLE
    except Exception as problem:
        _rollback(freeze_file, pause_file, freeze.pause_text(reason), owned, err)
        print(f"korapy freeze: cannot count live agents: {problem} (freeze rolled back)", file=err)
        return UNREACHABLE
    print(freeze.pause_text(reason), file=out)
    return OK


def _rollback(freeze_file, pause_file, pause_text, owned, err):
    """Remove what a failed `start` wrote, so the failure changes nothing.

    Best effort: a removal that fails names its file instead of
    raising, since the drain failure is already being reported.
    """
    try:
        os.remove(freeze_file)
    except FileNotFoundError:
        pass
    except OSError as problem:
        print(f"korapy freeze: cannot roll back {freeze_file}: {problem}", file=err)
        return
    if not owned:
        return
    try:
        with open(pause_file, encoding="utf-8") as handle:
            current = handle.read()
    except FileNotFoundError:
        return
    except OSError as problem:
        print(f"korapy freeze: cannot roll back {pause_file}: {problem}", file=err)
        return
    if current in (pause_text + "\n", pause_text):
        try:
            os.remove(pause_file)
        except FileNotFoundError:
            pass
        except OSError as problem:
            print(f"korapy freeze: cannot roll back {pause_file}: {problem}", file=err)


def cmd_end(*, out, err, freeze_file, pause_file):
    """Drop the freeze; drop the pause file only while the freeze owns it.

    The owned pause goes first, so a crash between the removes leaves
    the freeze standing (visible, resumable by hand) instead of an
    orphan pause no freeze verb can clear.
    """
    from korapy import freeze

    if not os.path.exists(freeze_file):
        print("korapy freeze: no freeze in place", file=out)
        return NO
    try:
        record = freeze.read_freeze(freeze_file)
    except freeze.FreezeError as problem:
        print(f"korapy freeze: {problem}", file=err)
        return USAGE
    if record is None:
        print("korapy freeze: no freeze in place", file=out)
        return NO
    removed_pause = False
    if record["owned_pause"]:
        try:
            with open(pause_file, encoding="utf-8") as handle:
                current = handle.read()
        except FileNotFoundError:
            current = None
        if current is None or current in (record["pause_text"] + "\n", record["pause_text"]):
            try:
                os.remove(pause_file)
            except FileNotFoundError:
                pass
            except OSError as problem:
                print(
                    f"korapy freeze: cannot remove the pause file: {problem}",
                    file=err,
                )
                return USAGE
            removed_pause = True
    try:
        os.remove(freeze_file)
    except OSError as problem:
        print(f"korapy freeze: cannot remove the freeze state: {problem}", file=err)
        return USAGE
    if removed_pause or not os.path.exists(pause_file):
        print("korapy freeze: resumed", file=out)
        return OK
    print("korapy freeze: freeze ended", file=out)
    print(
        "korapy freeze: pause file left in place (it changed since the freeze "
        "started); remove it by hand to resume",
        file=err,
    )
    return OK


def cmd_freeze(
    args,
    *,
    live_total=None,
    sleep=None,
    now=None,
    out=None,
    err=None,
    freeze_file=None,
    pause_file=None,
):
    """Run the verb. Seams default to the live host; tests pass fakes."""
    from korapy import drive, freeze
    from korapy import paths as paths_mod
    from korapy import projects

    if out is None:
        out = sys.stdout
    if err is None:
        err = sys.stderr
    try:
        if freeze_file is None:
            freeze_file = freeze.freeze_path()
        if pause_file is None:
            pause_file = drive.pause_path()
    except paths_mod.PathError as problem:
        print(f"korapy freeze: {problem}", file=err)
        return USAGE
    if args.freeze_verb == "end":
        return cmd_end(out=out, err=err, freeze_file=freeze_file, pause_file=pause_file)
    if live_total is None:
        try:
            live_total = _production_live_total()
        except (paths_mod.PathError, projects.ProjectsError) as problem:
            print(f"korapy freeze: {problem}", file=err)
            return USAGE
    if sleep is None:
        sleep = time.sleep
    if now is None:
        now = time.time()
    return cmd_start(
        args,
        live_total=live_total,
        sleep=sleep,
        now=now,
        out=out,
        err=err,
        freeze_file=freeze_file,
        pause_file=pause_file,
    )


def add_parser(sub) -> None:
    """Register `freeze` on the top-level subparsers."""
    p = sub.add_parser(
        "freeze",
        help="drain agents and hold the dispatch loop for a sweeping change",
    )
    verbs = p.add_subparsers(dest="freeze_verb", required=True)
    start = verbs.add_parser(
        "start",
        help="hold the loop now, wait until no agent is live",
    )
    start.add_argument(
        "--until",
        nargs="*",
        default=[],
        metavar="TICKET",
        help="REPO#N refs whose merge ends the freeze by itself",
    )
    start.add_argument(
        "--reason",
        default="operator freeze",
        help="why the loop holds (shown on the dashboard and in doctor)",
    )
    verbs.add_parser("end", help="drop the freeze and resume dispatch")
    p.set_defaults(func=cmd_freeze)
