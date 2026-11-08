"""`korapy decision record`: the maintainer's answer as a durable record.

A decision the maintainer takes leaves one record a later reader (or
agent) finds without reading chat: a markdown file under the ops root's
``decisions/`` dir, one comment on the ticket carrying the decision
line, the ``needs-decision`` label removed, and a ``decision`` entry in
the coordinator logbook.

Ordering and retry: the file lands first, and only then do the tracker
calls and the logbook entry follow. A local failure before the file
lands runs no tracker call, so the retry starts fresh. A retry that
finds its file replays only what is missing: it lists the ticket
comments and posts only when the decision line is absent, removing an
absent label succeeds, and a missing logbook entry is appended. The
file is written once and the comment posts once, whatever date the
retry runs on: the retry reuses the first filename, never a recomputed
one.
"""

import datetime
import os
import re
import tempfile
from typing import NamedTuple

from korapy import coord, tracker as iface

#: Where decision records live under the ops root.
DECISIONS_DIRNAME = "decisions"

REVERSIBLE_CHOICES = ("yes", "no")
CONFIDENCE_CHOICES = ("high", "medium", "low")


class UsageError(ValueError):
    """A decision flag or slug is unusable (maps to exit 4 at the CLI)."""


class RecordResult(NamedTuple):
    """One recorded (or already-recorded) decision: its file path."""

    path: str
    already: bool


def slugify(text):
    """The filename slug for a decision: lowercase words joined by hyphens.

    Pure: every retry of the same decision derives the same slug, so the
    verb is idempotent without storing one. Anything without a word
    reads as no slug, and the caller refuses it.
    """
    words = re.findall(r"[a-z0-9]+", str(text or "").lower())
    slug = "-".join(words)
    return slug[:60].rstrip("-")


def decisions_dir(root):
    """The decision records dir under one ops root."""
    return os.path.join(root, DECISIONS_DIRNAME)


def decision_ref(repo, number):
    """The ticket ref a decision names: ``REPO#N``."""
    return f"{repo}#{number}"


def decision_line(repo, number, decision):
    """The one decision line the comment and the logbook carry."""
    return f"Decision on {decision_ref(repo, number)}: {decision}"


def record_filename(date, slug):
    """The decision file name for one date and slug."""
    return f"{date}-{slug}.md"


#: One record file name: a date plus the decision slug.
_RECORD_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-(.+)\.md$")


def find_recorded(root, slug):
    """The recorded file for ``slug`` under the ops root, else None.

    The slug compares exactly after the leading date, so ``claim``
    never matches ``hub-claim``. A retry past midnight still finds the
    first write, whatever date the retry runs on. The oldest name wins;
    anything outside the decisions dir never counts.
    """
    base = decisions_dir(root)
    try:
        names = os.listdir(base)
    except OSError:
        return None
    found = []
    for name in names:
        match = _RECORD_RE.match(name)
        if match is None:
            continue
        try:
            datetime.date.fromisoformat(match.group(1))
        except ValueError:
            continue
        if match.group(2) == slug:
            found.append(name)
    if not found:
        return None
    return os.path.join(base, sorted(found)[0])


def render_markdown(
    *,
    repo,
    number,
    date,
    decision,
    why,
    reversible=None,
    confidence=None,
    follow_ups=(),
    settles=None,
    still_open=None,
    reopen_when=None,
):
    """The decision file body. Pure: the same inputs render byte-identical."""
    lines = [
        f"# Decision: {decision}",
        "",
        f"Ticket: {decision_ref(repo, number)}",
        f"Date: {date}",
        f"Decision: {decision}",
        f"Why: {why}",
        f"Reversible: {reversible if reversible else 'unknown'}",
        f"Confidence: {confidence if confidence else 'unknown'}",
    ]
    if follow_ups:
        lines.append(f"Follow-ups: {', '.join(decision_ref(repo, n) for n in follow_ups)}")
    else:
        lines.append("Follow-ups: none")
    if settles:
        lines.append(f"Settles for now: {settles}")
    if still_open:
        lines.append(f"Still open: {still_open}")
    if reopen_when:
        lines.append(f"Reopen when: {reopen_when}")
    return "\n".join(lines) + "\n"


def comment_body(
    *,
    repo,
    number,
    decision,
    why,
    reversible=None,
    confidence=None,
    filename,
    settles=None,
    still_open=None,
    reopen_when=None,
    follow_ups=(),
):
    """The one ticket comment for a decision.

    Carries the decision line and a relative ``decisions/`` path. The
    verb never adds an absolute path or a private name: only the flags
    above reach the text, so the comment is safe to post as-is.
    """
    lines = [
        decision_line(repo, number, decision),
        "",
        why,
        "",
        f"Reversible: {reversible if reversible else 'unknown'}; "
        f"Confidence: {confidence if confidence else 'unknown'}.",
    ]
    if settles:
        lines.append(f"Settles for now: {settles}")
    if still_open:
        lines.append(f"Still open: {still_open}")
    if reopen_when:
        lines.append(f"Reopen when: {reopen_when}")
    if follow_ups:
        lines.append(f"Follow-ups: {', '.join(decision_ref(repo, n) for n in follow_ups)}")
    lines.append(f"Recorded in {DECISIONS_DIRNAME}/{filename}.")
    return "\n".join(lines) + "\n"


def _opt(value):
    """Flag text stripped, or None when absent or blank."""
    if value is None:
        return None
    return str(value).strip() or None


def _write_file(path, text):
    """Write ``text`` to ``path`` atomically, creating parents as needed."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".decision-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _check_args(number, decision, why, reversible, confidence):
    text = str(decision or "").strip()
    if not text:
        raise UsageError("korapy decision record needs --decision TEXT")
    reason = str(why or "").strip()
    if not reason:
        raise UsageError("korapy decision record needs --why TEXT")
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        raise UsageError(f"korapy decision record needs a ticket number, got {number!r}")
    if reversible is not None and reversible not in REVERSIBLE_CHOICES:
        raise UsageError(f"korapy decision record --reversible wants yes|no, got {reversible!r}")
    if confidence is not None and confidence not in CONFIDENCE_CHOICES:
        raise UsageError(
            f"korapy decision record --confidence wants high|medium|low, got {confidence!r}"
        )
    slug = slugify(text)
    if not slug:
        raise UsageError("korapy decision record --decision names no words for a file slug")
    return text, reason, slug


def _recorded_lines(path):
    """The recorded file as lines, for the exact idempotency check."""
    with open(path, encoding="utf-8") as handle:
        return handle.read().splitlines()


def _comment_present(tracker, tracker_repo, number, line):
    """Whether the ticket already carries ``line`` in one comment."""
    return any(line in body for body in tracker.comments(tracker_repo, number))


def _logbook_present(coord_root, line):
    """Whether the logbook already holds a decision entry for ``line``."""
    return any(
        entry.get("kind") == "decision" and line in (entry.get("text") or "")
        for entry in coord.read_entries(root=coord_root)
    )


def _publish(
    *,
    tracker,
    tracker_repo,
    repo,
    number,
    text,
    reason,
    reversible,
    confidence,
    settles,
    still_open,
    reopen_when,
    follow_ups,
    filename,
    coord_root,
    holder,
    generation,
):
    """The tracker call set plus the logbook entry, each idempotent.

    ``filename`` is the basename of the record file, first write or
    retry alike, so the comment always names the file on disk.
    ``item`` proves the ticket is reachable, ``comments`` finds the
    decision line a retry already posted, ``remove_label`` clears
    ``needs-decision`` (removing an absent label succeeds), and the
    logbook entry is appended only when missing.
    """
    line = decision_line(repo, number, text)
    tracker.item(tracker_repo, number)
    tracker.remove_label(tracker_repo, number, iface.NEEDS_DECISION)
    if not _comment_present(tracker, tracker_repo, number, line):
        tracker.comment(
            tracker_repo,
            number,
            comment_body(
                repo=repo,
                number=number,
                decision=text,
                why=reason,
                reversible=reversible,
                confidence=confidence,
                filename=filename,
                settles=settles,
                still_open=still_open,
                reopen_when=reopen_when,
                follow_ups=tuple(follow_ups),
            ),
        )
    entry_text = f"{line} ({DECISIONS_DIRNAME}/{filename})"
    if not _logbook_present(coord_root, line):
        coord.append_entry(
            "decision",
            entry_text,
            refs=[decision_ref(repo, number)],
            holder=holder,
            generation=generation,
            root=coord_root,
        )


def record(
    *,
    repo,
    tracker_repo,
    number,
    decision,
    why,
    reversible=None,
    confidence=None,
    settles=None,
    still_open=None,
    reopen_when=None,
    follow_ups=(),
    tracker,
    ops_root,
    coord_root,
    holder="manual",
    generation=0,
    now=None,
):
    """Record one decision: file, ticket comment, label removal, logbook.

    The file lands first; the one tracker call set per record follows
    (``item``, ``remove_label``, ``comments``, and ``comment`` only
    when the decision line is absent), and the logbook entry last. A
    local failure before the file lands runs no tracker call, so the
    retry starts fresh; a retry that finds its file replays only what
    is missing and returns it with ``already`` set. A ``TrackerError``
    leaves the file without its comment or log entry, and the retry
    completes it. A file with the same slug for another ticket is a
    ``UsageError``: the operator rewords the decision instead of
    overwriting a record.
    """
    text, reason, slug = _check_args(number, decision, why, reversible, confidence)
    for follow in follow_ups:
        if not isinstance(follow, int) or isinstance(follow, bool) or follow < 1:
            raise UsageError(
                f"korapy decision record --follow-up wants ticket numbers, got {follow!r}"
            )
    opt_settles, opt_open, opt_reopen = _opt(settles), _opt(still_open), _opt(reopen_when)
    existing = find_recorded(ops_root, slug)
    if existing is not None:
        lines = _recorded_lines(existing)
        if f"Ticket: {decision_ref(repo, number)}" not in lines or f"Decision: {text}" not in lines:
            raise UsageError(
                f"decision slug {slug!r} already records another ticket in {existing}: "
                "reword --decision"
            )
        _publish(
            tracker=tracker,
            tracker_repo=tracker_repo,
            repo=repo,
            number=number,
            text=text,
            reason=reason,
            reversible=reversible,
            confidence=confidence,
            settles=opt_settles,
            still_open=opt_open,
            reopen_when=opt_reopen,
            follow_ups=tuple(follow_ups),
            filename=os.path.basename(existing),
            coord_root=coord_root,
            holder=holder,
            generation=generation,
        )
        return RecordResult(existing, True)
    moment = now if now is not None else coord.utcnow()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    date = moment.date().isoformat()
    filename = record_filename(date, slug)
    path = os.path.join(decisions_dir(ops_root), filename)
    _write_file(
        path,
        render_markdown(
            repo=repo,
            number=number,
            date=date,
            decision=text,
            why=reason,
            reversible=reversible,
            confidence=confidence,
            follow_ups=tuple(follow_ups),
            settles=opt_settles,
            still_open=opt_open,
            reopen_when=opt_reopen,
        ),
    )
    _publish(
        tracker=tracker,
        tracker_repo=tracker_repo,
        repo=repo,
        number=number,
        text=text,
        reason=reason,
        reversible=reversible,
        confidence=confidence,
        settles=opt_settles,
        still_open=opt_open,
        reopen_when=opt_reopen,
        follow_ups=tuple(follow_ups),
        filename=filename,
        coord_root=coord_root,
        holder=holder,
        generation=generation,
    )
    return RecordResult(path, False)
