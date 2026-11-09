"""The dispatch freeze: drain agents, hold the loop, resume on merge.

A sweeping change (a mass reformat, a rename across the tree) conflicts
with every agent working at the time. `korapy freeze start` stops new
starts immediately by holding the drive pause file, waits until no
agent is live, and records the freeze beside the pause file with its
reason and the tickets that end it. The next dispatch pass ends the
freeze by itself once those tickets read merged; `korapy freeze end`
resumes by hand.

Every host read arrives through an injected seam: `live_total` counts
live agents, `item_state(repo, number)` reports a ticket's tracker
state, `sleep` waits between drain polls. This module never lists
units or reaches the tracker itself.
"""

import json
import os

from korapy import paths, settings


def _positive_seconds(value):
    """One configured freeze age: anything but a positive integer raises.

    Takes the parsed value; zero would warn on a fresh freeze, so it is
    a configuration error, not a quiet always-stale.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"must be a positive integer, not {value!r}")
    return value


#: Default age in seconds after which a standing freeze warns in
#: doctor instead of silently blocking starts.
DEFAULT_MAX_AGE_S = 86400

settings.register(
    "freeze.max_age_s",
    int,
    DEFAULT_MAX_AGE_S,
    "age in seconds after which a standing freeze warns in doctor "
    "instead of silently blocking starts",
    check=_positive_seconds,
)


class FreezeError(Exception):
    """A freeze ref or state file is unusable (exit 4)."""


#: The freeze state file, beside the drive pause file in the state dir.
FREEZE_NAME = "drive.freeze"

#: Seconds between live-agent polls while a freeze drains.
POLL_S = 30


def freeze_path():
    """The freeze state file: inside the state dir, beside the pause file."""
    return os.path.join(paths.state_dir(), FREEZE_NAME)


def parse_ref(text):
    """`(repo, number)` for a `REPO#N` ticket ref, else `FreezeError`."""
    if not isinstance(text, str):
        raise FreezeError(f"ticket ref {text!r} must look like 'REPO#N'")
    repo, mark, number = text.rpartition("#")
    if not mark or not paths.plain_repo_name(repo) or not number.isdigit():
        raise FreezeError(f"ticket ref {text!r} must look like 'REPO#N'")
    found = int(number)
    if found < 1:
        raise FreezeError(f"ticket ref {text!r} must look like 'REPO#N'")
    return repo, found


def format_ref(repo, number):
    """The `REPO#N` spelling of one parsed ref."""
    return f"{repo}#{number}"


def parse_until_refs(items):
    """Deduplicated `[(repo, number)]` for a list of `REPO#N` refs.

    The whole list fails on one bad entry, so a typo never gates on
    half the tickets. Anything but a list of strings is a `FreezeError`.
    """
    if isinstance(items, str) or not isinstance(items, (list, tuple)):
        raise FreezeError(f"until tickets {items!r} must be a list of 'REPO#N' refs")
    seen = set()
    refs = []
    for item in items:
        ref = parse_ref(item)
        if ref not in seen:
            seen.add(ref)
            refs.append(ref)
    return refs


def pause_text(reason):
    """The pause file line a freeze holds: the reason, frozen-marked."""
    clean = (reason or "").strip()
    return f"frozen: {clean}" if clean else "frozen"


def write_freeze(path, *, reason, until, now, owned_pause):
    """Record a freeze atomically: reason, until refs, clock and pause debt.

    `until` holds `REPO#N` strings, normalized here, so a typo fails
    the write instead of gating on half the tickets. `owned_pause`
    says the start wrote the pause file itself, so `end` removes only
    a pause the freeze still owns.
    """
    if not isinstance(reason, str):
        raise FreezeError(f"freeze reason {reason!r} must be a string")
    refs = parse_until_refs(until)
    payload = {
        "created_at": now,
        "owned_pause": bool(owned_pause),
        "pause_text": pause_text(reason),
        "reason": reason,
        "until": [format_ref(repo, number) for repo, number in refs],
    }
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    paths.atomic_write_text(path, json.dumps(payload, sort_keys=True) + "\n")


def read_freeze(path):
    """The freeze record, or None when no freeze stands.

    Missing keys read as defaults; a wrong-typed value is a
    `FreezeError`, so a hand-edited file fails loudly instead of
    gating on half a record.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as err:
        raise FreezeError(f"freeze file {path} is unreadable: {err}") from None
    if not isinstance(data, dict):
        raise FreezeError(f"freeze file {path} is corrupt: expected an object")
    reason = data.get("reason", "")
    if not isinstance(reason, str):
        raise FreezeError(f"freeze file {path} is corrupt: 'reason' must be a string")
    until = data.get("until", [])
    if not isinstance(until, list) or any(not isinstance(item, str) for item in until):
        raise FreezeError(f"freeze file {path} is corrupt: 'until' must be a list of refs")
    created = data.get("created_at", 0)
    if isinstance(created, bool) or not isinstance(created, (int, float)):
        raise FreezeError(f"freeze file {path} is corrupt: 'created_at' must be a number")
    owned = data.get("owned_pause", False)
    if not isinstance(owned, bool):
        raise FreezeError(f"freeze file {path} is corrupt: 'owned_pause' must be a boolean")
    text = data.get("pause_text", pause_text(reason))
    if not isinstance(text, str):
        raise FreezeError(f"freeze file {path} is corrupt: 'pause_text' must be a string")
    return {
        "created_at": created,
        "owned_pause": owned,
        "pause_text": text,
        "reason": reason,
        "until": [format_ref(repo, number) for repo, number in parse_until_refs(until)],
    }


def active_reason(path):
    """The frozen-marked reason a freeze holds, or None when none stands."""
    record = read_freeze(path)
    if record is None:
        return None
    return record["pause_text"]


def freeze_view(path, *, now, max_age_s):
    """The doctor's freeze reading: reason, until, age and staleness.

    None when no freeze stands. A freeze past `max_age_s` reads stale,
    so a crash-orphaned freeze warns instead of blocking starts
    silently.
    """
    record = read_freeze(path)
    if record is None:
        return None
    age = int(now - record["created_at"])
    return {
        "age_s": age,
        "created_at": record["created_at"],
        "reason": record["reason"],
        "stale": age > max_age_s,
        "until": record["until"],
    }


def all_merged(refs, item_state):
    """Whether every ref reads merged through the `item_state` seam.

    Returns `(merged, pending)`: `item_state(repo, number)` reports the
    tracker's state (`"closed"` reads merged). An unreadable ticket
    blocks, so a tracker outage keeps the freeze instead of ending it
    on a guess; a merged check never breaks the pass.
    """
    pending = []
    for repo, number in refs:
        try:
            state = item_state(repo, number)
        except Exception:  # noqa: BLE001 - a merged check never breaks the pass
            pending.append(format_ref(repo, number))
            continue
        if state != "closed":
            pending.append(format_ref(repo, number))
    return (not pending), pending


def drain(live_total, *, sleep, out):
    """Wait until no agent is live, reporting each polling pass.

    `live_total()` counts live agents; `sleep` waits between polls.
    Returns the number of passes polled.
    """
    passes = 0
    while True:
        live = live_total()
        passes += 1
        if live <= 0:
            print("freeze: no live agents, the loop holds", file=out)
            return passes
        print(f"freeze: waiting for {live} live agent(s) to finish...", file=out)
        sleep(POLL_S)


def try_release(freeze_file, pause_file, item_state):
    """End a freeze whose until tickets all read merged. True when released.

    No gate means no auto-release: a freeze without `--until` waits for
    `freeze end`. A missing freeze is not a release. The owned pause
    goes first, so a crash between the removes leaves the freeze
    standing (visible, retried by the next pass) instead of an orphan
    pause no freeze verb can clear. An `OSError` removing a file keeps
    the freeze, so the next pass retries instead of starting on
    half-removed state.
    """
    record = read_freeze(freeze_file)
    if record is None:
        return False
    refs = parse_until_refs(record["until"])
    if not refs:
        return False
    merged, _ = all_merged(refs, item_state)
    if not merged:
        return False
    try:
        if record["owned_pause"]:
            try:
                with open(pause_file, encoding="utf-8") as handle:
                    current = handle.read()
            except FileNotFoundError:
                current = None
            if current in (record["pause_text"] + "\n", record["pause_text"]):
                os.remove(pause_file)
        os.remove(freeze_file)
    except OSError:
        return False
    return True
