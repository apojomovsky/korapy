"""Dashboard data panels (ticket 651): pure builders over plain data.

One reader per fact: every number the new panels show traces to one of
these helpers, and every helper reads its source as `unknown` when the
source is missing. No subprocess, no network, no file reads here; the
snapshot layer owns collecting, this module owns deriving.
"""

import re as _re
import time as _time

#: Derived agent states, most urgent first. The order is the panel's
#: sort: blocked work beats questions, questions beat stalls, and
#: finished history sorts last.
DERIVED = (
    "blocked",
    "asking",
    "stalled",
    "testing",
    "building",
    "working",
    "idle",
    "review",
    "finished",
    "failed",
)

URGENCY = {name: rank for rank, name in enumerate(DERIVED)}

#: Quota error text shown on screen, trimmed to this many characters.
ERROR_TRIM = 120

#: Note-signal words, matched as whole words: substrings misclassify
#: notes like "task sync running" (which holds "ask") or "latest
#: deploy" (which holds "test"). `\w*` keeps inflections ("tests",
#: "asking", "building") matching while their carriers do not.
_SIGNAL = {
    name: _re.compile(r"\b" + name + r"\w*\b")
    for name in ("blocked", "waiting", "ask", "question", "test", "build", "review")
}


def _has(signal, text):
    """Whether the whole-word `signal` appears in `text`."""
    return _SIGNAL[signal].search(text) is not None


#: Seconds between drive passes (`korapy-drive.timer` `*:0/5`): the one
#: source for the plan band's "next pass in N".
PLAN_PASS_S = 300


def _idle_s(entry, now):
    """Seconds since `last_seen` (else `started_at`), or `"unknown"`.

    A present but unreadable `last_seen` reads `unknown`: falling back
    to `started_at` would hide the broken stamp behind an older truth.
    """
    if not isinstance(entry, dict):
        return "unknown"
    last = entry.get("last_seen")
    if last is not None:
        if isinstance(last, bool) or not isinstance(last, (int, float)):
            return "unknown"
        try:
            return max(0, int(now - float(last)))
        except (TypeError, ValueError, OverflowError):
            return "unknown"
    start = entry.get("started_at")
    if isinstance(start, bool) or not isinstance(start, (int, float)):
        return "unknown"
    try:
        return max(0, int(now - float(start)))
    except (TypeError, ValueError, OverflowError):
        return "unknown"


def agent_state(entry, state, now):
    """One agent's derived panel fact: `{derived, idle_s, doing}` (pure).

    `state` is the registry status (`live`, `stale`, `finished`).
    Finished rows read their outcome (`failed` stays `failed`, anything
    else reads `finished`). Stale rows read `stalled`. Live rows read
    their note's first signal word (blocked, asking, testing, building,
    review), else `working` while recently seen and `idle` when the
    note is empty and the row is old. `doing` is the note's first line,
    trimmed, or `"unknown"` when there is none. `idle_s` is seconds
    since `last_seen`, or `"unknown"` when unreadable.
    """
    note = entry.get("note") if isinstance(entry, dict) else None
    text = note if isinstance(note, str) else ""
    lowered = text.lower()
    idle = _idle_s(entry if isinstance(entry, dict) else {}, now)
    if state == "finished":
        outcome = entry.get("outcome") if isinstance(entry, dict) else None
        derived = "failed" if outcome == "failed" else "finished"
    elif state == "stale":
        derived = "stalled"
    elif _has("blocked", lowered) or _has("waiting", lowered):
        derived = "blocked"
    elif "?" in text or _has("ask", lowered) or _has("question", lowered):
        derived = "asking"
    elif _has("test", lowered):
        derived = "testing"
    elif _has("build", lowered):
        derived = "building"
    elif _has("review", lowered):
        derived = "review"
    elif isinstance(entry, dict) and entry.get("role") == "reviewer":
        derived = "review"
    elif not text.strip() and (idle == "unknown" or idle >= 300):
        derived = "idle"
    else:
        derived = "working"
    first = text.strip().splitlines()[0].strip() if text.strip() else ""
    doing = first[:160] if first else "unknown"
    return {"derived": derived, "idle_s": idle, "doing": doing}


def sort_key(row):
    """Urgency first, then project, ticket and id (pure)."""
    derived = row.get("derived") if isinstance(row, dict) else None
    rank = URGENCY.get(derived, len(URGENCY))
    project = row.get("project") or "" if isinstance(row, dict) else ""
    repo = row.get("repo") or "" if isinstance(row, dict) else ""
    number = row.get("number") if isinstance(row, dict) else None
    ident = row.get("id") or "" if isinstance(row, dict) else ""
    return (rank, project, repo, number is None, number or 0, ident)


def quota_honesty(provider):
    """One provider's honest panel fact (pure).

    `source` reads `measured` for fresh windows, `estimate` for stale
    ones (old numbers, labelled as such) and when the panel would
    otherwise show a guess, and `unknown` when there is no reading at
    all: a bare `ok` state with no windows and no `read_at` reads
    `unknown`, never a measured zero. `read_failed` names a recorded
    read failure; `error_trimmed` carries its text trimmed to
    `ERROR_TRIM`, or `"unknown"` when nothing was recorded.
    """
    if not isinstance(provider, dict):
        return {"source": "unknown", "read_failed": False, "error_trimmed": "unknown"}
    windows = provider.get("windows")
    error = provider.get("error")
    read_failed = isinstance(error, str) and bool(error)
    if isinstance(windows, dict) and windows:
        source = "estimate" if provider.get("stale") else "measured"
    elif provider.get("read_at") is not None and provider.get("state") in (
        "ok",
        "low",
        "exhausted",
    ):
        source = "measured"
    elif read_failed or provider.get("read_at") is not None:
        source = "estimate"
    else:
        source = "unknown"
    if read_failed:
        error_trimmed = error[:ERROR_TRIM]
    elif error is None:
        error_trimmed = "unknown" if source == "unknown" else "none"
    else:
        error_trimmed = "none"
    return {"source": source, "read_failed": read_failed, "error_trimmed": error_trimmed}


def with_honest_quota(quota_view):
    """`quota_view` with honesty fields merged per provider (pure).

    Each present window carries its provider's `source`, so the panel
    is honest per window; a null window stays null (no reading, never
    a stamped guess).
    """
    if not isinstance(quota_view, dict) or not isinstance(quota_view.get("providers"), dict):
        return quota_view
    providers = {}
    for name, info in quota_view["providers"].items():
        merged = dict(info) if isinstance(info, dict) else {}
        merged.update(quota_honesty(info if isinstance(info, dict) else None))
        windows = merged.get("windows")
        if isinstance(windows, dict):
            stamped = {}
            for window_id, entry in windows.items():
                if isinstance(entry, dict):
                    marked = dict(entry)
                    marked["source"] = merged["source"]
                    stamped[window_id] = marked
                else:
                    stamped[window_id] = entry
            merged["windows"] = stamped
        providers[name] = merged
    out = dict(quota_view)
    out["providers"] = providers
    return out


def attention(events):
    """The attention band from unresolved event rows (pure).

    `events` is `coordevents.unresolved` rows, None when uncollected,
    or the exception reading them raised. `needs_decision` carries the
    question rows, `ready_to_merge` the held-merge rows, each with its
    ref and one line of evidence. `coordinator_handled` counts the rows
    the coordinator owns (`coordinator-review`). Failing doctor rows
    are not part of this band: they need their own reader, which a
    later change adds.
    """
    if events is None:
        return {
            "state": "unknown",
            "needs_decision": "unknown",
            "ready_to_merge": "unknown",
            "coordinator_handled": "unknown",
            "items": "unknown",
        }
    if isinstance(events, Exception):
        return {
            "state": "error",
            "needs_decision": [],
            "ready_to_merge": [],
            "coordinator_handled": 0,
            "items": [],
            "error": str(events) or repr(events),
        }
    needs = []
    merges = []
    handled = 0
    for row in events or ():
        if not isinstance(row, dict):
            continue
        kind = row.get("kind")
        ref = row.get("ref")
        if kind == "question":
            needs.append(
                {"ref": ref, "question": row.get("question") or row.get("evidence") or "unknown"}
            )
        elif kind == "held-merge":
            merges.append(
                {"ref": ref, "reason": row.get("reason") or row.get("evidence") or "unknown"}
            )
        elif kind == "coordinator-review":
            handled += 1
    return {
        "state": "ready",
        "needs_decision": needs,
        "ready_to_merge": merges,
        "coordinator_handled": handled,
        "items": list(needs) + list(merges),
    }


def plan_projection(drive_view, now):
    """The plan band from the drive section (pure).

    `next` names the starts the loop would make next: one row per free
    slot while the daily budget holds, each carrying the reason it
    waits (the top skip codes from the last pass, else `slot free`).
    With no starts left the list is empty, so the panel reads the
    empty state instead of naming starts that can never start.
    `next_pass_in_s` counts down from the last pass on the 5-minute
    timer cadence. A missing drive section reads `unknown`; a broken
    one reads `error`. Neither invents candidates: with no pass data
    the panel says it does not know.
    """
    if drive_view is None:
        return {
            "state": "unknown",
            "next": "unknown",
            "waits": "unknown",
            "next_pass_in_s": "unknown",
            "starts_remaining": "unknown",
        }
    if isinstance(drive_view, Exception) or (
        isinstance(drive_view, dict) and "error" in drive_view
    ):
        detail = drive_view if isinstance(drive_view, Exception) else drive_view.get("error")
        return {
            "state": "error",
            "next": [],
            "waits": [],
            "next_pass_in_s": None,
            "starts_remaining": 0,
            "error": str(detail),
        }
    try:
        remaining = int(drive_view.get("starts_remaining", 0))
    except (TypeError, ValueError):
        remaining = 0
    remaining = max(0, remaining)
    skipped = drive_view.get("skipped_by_reason") or {}
    waits = sorted(skipped) if isinstance(skipped, dict) else []
    if not remaining:
        nxt = []
    elif not waits:
        nxt = [{"what": "start", "waits": "slot free"} for _ in range(min(remaining, 3))]
    else:
        nxt = [{"what": "start", "waits": wait} for wait in waits[:3]]
        if len(nxt) < min(remaining, 3):
            nxt += [
                {"what": "start", "waits": "slot free"} for _ in range(min(remaining, 3) - len(nxt))
            ]
    last = drive_view.get("last_pass_at")
    if isinstance(last, bool) or not isinstance(last, (int, float)):
        countdown = None
    else:
        try:
            countdown = max(0, int(last + PLAN_PASS_S - float(now)))
        except (TypeError, ValueError, OverflowError):
            countdown = None
    return {
        "state": "ready",
        "next": nxt,
        "waits": waits,
        "next_pass_in_s": countdown,
        "starts_remaining": remaining,
    }


def local_offset_s(now=None):
    """The machine's local UTC offset in seconds (one reader)."""
    moment = _time.time() if now is None else float(now)
    return int(_time.localtime(moment).tm_gmtoff)


def now_iso(now=None):
    """`now` as an ISO timestamp carrying the local offset (one reader)."""
    from datetime import datetime, timezone

    moment = _time.time() if now is None else float(now)
    local = datetime.fromtimestamp(moment).astimezone()
    try:
        reference = datetime.fromtimestamp(moment, tz=timezone.utc).astimezone(local.tzinfo)
        return reference.isoformat()
    except (OverflowError, OSError, ValueError):
        return local.isoformat()
