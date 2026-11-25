"""The agent peek: a bounded, read-only tail of one agent's session.

One reader over the session files: the newest session of the ticket
and role, through the runtime adapter's one activity-file resolver,
read from the end under a hard cap on bytes and on lines. Every line
carries its entry's timestamp in local time (the machine offset the
snapshot header already stamps), tool calls and results shortened to
one capped line by the tail renderer, and a session that is missing
or unreadable reads `not-available`, never an empty pane and never
an error. No writes, no quota ledger, no network here.
"""

import json
import os

from korapy import dashboard, paths, runtimes
from korapy.runtimes import tail as runtime_tail

#: Lines one peek returns at most. The one owner of the bound; the
#: serve layer clamps any larger request to it.
MAX_LINES = 50

#: Bytes one peek reads at most, from the end of the session file.
#: A session file can grow without bound while its agent works, so
#: the peek never reads more than this however long it runs.
MAX_BYTES = 32768

#: Characters one peek line keeps at most: the tail renderer's own
#: width, so a peek line and a tail line shorten the same way.
LINE_WIDTH = runtime_tail.WIDTH

#: Characters one peek timestamp keeps at most: an ISO stamp with an
#: offset is far shorter, so anything longer is a corrupt stamp.
STAMP_WIDTH = 64

#: Entry keys that carry the entry's own timestamp, in lookup order.
_STAMP_KEYS = ("timestamp", "ts", "time", "at", "created_at")

NOT_AVAILABLE = {"state": "not-available", "lines": [], "truncated": False}


def _not_available():
    """A fresh not-available document: callers may hold or mutate it."""
    return {"state": "not-available", "lines": [], "truncated": False}


def _stamp_of(entry):
    """One entry's timestamp as a local-offset ISO stamp, or `"unknown"`.

    Numeric stamps (epoch seconds) convert through
    `dashboard.now_iso`, the one local-time stamp; string stamps pass
    through as recorded (they already carry their offset). Anything
    else reads `unknown`: a peek never invents a time.
    """
    if not isinstance(entry, dict):
        return "unknown"
    value = None
    for key in _STAMP_KEYS:
        if entry.get(key) is not None:
            value = entry.get(key)
            break
    if value is None and isinstance(entry.get("payload"), dict):
        for key in _STAMP_KEYS:
            if entry["payload"].get(key) is not None:
                value = entry["payload"].get(key)
                break
    if isinstance(value, bool):
        return "unknown"
    if isinstance(value, (int, float)):
        try:
            return dashboard.now_iso(float(value))
        except (TypeError, ValueError, OverflowError, OSError):
            return "unknown"
    if isinstance(value, str) and value.strip():
        return value.strip()[:STAMP_WIDTH]
    return "unknown"


def _entries(data, max_lines):
    """Peek lines from complete session bytes (pure).

    Only complete lines render (a line the agent is still writing
    waits for its newline, the tail's own rule), each through the one
    tail renderer so tool calls and results shorten identically, and
    only the newest `max_lines` survive.
    """
    end = data.rfind(b"\n") + 1
    lines = []
    for raw in data[:end].splitlines():
        text_line = raw.decode("utf-8", "replace")
        rendered = runtime_tail.render(text_line)
        if not rendered:
            continue
        try:
            entry = json.loads(text_line)
        except ValueError:
            entry = None
        stamp = _stamp_of(entry)
        for text in rendered:
            lines.append({"at": stamp, "text": text.strip()[:LINE_WIDTH]})
    truncated = len(lines) > max_lines
    if truncated:
        lines = lines[len(lines) - max_lines :]
    return lines, truncated


def read_peek_file(path, max_lines=MAX_LINES, max_bytes=MAX_BYTES):
    """The peek document for one session file (read-only, bounded).

    At most `max_bytes` are read, from the end of the file; past the
    byte cap the first partial line drops out, so a shown line is
    always a whole entry. A missing or unreadable file reads
    `not-available`. `truncated` names whether either cap cut the
    view, so the page can say the peek is a tail, not the whole run.
    """
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            if size > max_bytes:
                handle.seek(size - max_bytes)
            data = handle.read(max_bytes)
    except OSError:
        return _not_available()
    truncated = size > max_bytes
    if truncated:
        newline = data.find(b"\n")
        data = data[newline + 1 :] if newline >= 0 else b""
    lines, cut = _entries(data, max_lines)
    return {"state": "ready", "lines": lines, "truncated": truncated or cut}


def peek_for_ticket(repo, number, role=None, last=None, root=None, env=None):
    """The peek document for one ticket's newest session of `role`.

    The session resolves through `paths.newest_session`, the one
    session-layout resolver, and its activity file through the
    recorded runtime's adapter, the one activity resolver. Any miss
    along that chain (a bad ticket name, no session, no activity yet,
    an unreadable file) reads `not-available`, never an exception:
    the pane states the miss instead of guessing.
    """
    try:
        session = paths.newest_session(repo, number, role, env=env, root=root)
    except Exception:  # noqa: BLE001 - a bad name or role record is a miss, not a crash
        return _not_available()
    if session is None:
        return _not_available()
    try:
        adapter = runtimes.get(runtimes.recorded(session))
        target = adapter.activity_file(session)
    except Exception:  # noqa: BLE001 - an unknown runtime is a miss, not a crash
        return _not_available()
    if target is None:
        return _not_available()
    limit = MAX_LINES if last is None else max(0, min(int(last), MAX_LINES))
    return read_peek_file(target, max_lines=limit)
