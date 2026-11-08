"""Research tickets: questions that finish as a Findings comment.

A ticket carrying the `research` label is started with the research
worker prompt (read-only on the repository, no commits, one comment
whose first line starts with `Findings`). The loop treats it as done
when that comment exists: it releases the claim, adds the
`researched` label, and never starts a reviewer round or waits for a
pull request. A finding addressed to the maintainer is reported as an
attention item on the dashboard until acknowledged.

Everything here is pure: findings detection over comment bodies, and
the settlement plan over store rows, finished entries and comments.
The drive pass owns every write (release, label, event).
"""

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from korapy import tracker as iface


@dataclass(frozen=True)
class Settlement:
    """One claimed research row ready to settle: release, maybe label.

    `has_findings` tells the pass whether the `researched` label and
    the attention event go with the release. A finished research
    worker with no Findings comment releases bare, so the ticket is
    startable again instead of waiting on a reviewer that never comes.
    `project` is the driving project the attention event goes to,
    resolved by the pass where the project context still exists.
    """

    repo: str
    number: int
    agent: str
    has_findings: bool
    findings: str = ""
    project: str = ""


def findings(comments: Sequence[Any]) -> str | None:
    """The first Findings comment body, or None when there is none. Pure.

    A comment counts when its first line starts with `Findings`. The
    match is case sensitive and unindented, so a note merely
    mentioning findings never reads as the verdict. Non-string
    entries are ignored, never an error. The first Findings comment
    on the ticket wins across re-runs: comment bodies carry no
    authorship or timestamp to scope the verdict to the current
    holder's run, so a re-run's corrected verdict never beats an
    earlier one already posted.
    """
    for comment in comments or ():
        if not isinstance(comment, str):
            continue
        lines = comment.splitlines()
        if lines and lines[0].startswith("Findings"):
            return comment
    return None


def first_line(body: str) -> str:
    """The findings verdict in one line: the body's first line, stripped."""
    return body.splitlines()[0].strip() if body.splitlines() else ""


def settlements(
    items: Sequence[Any],
    holders: Mapping[tuple[str, int], tuple[str | None, str | None] | None] | None,
    comments_by_key: Mapping[tuple[str, int], Sequence[str] | None] | None,
    projects: Mapping[tuple[str, int], str] | None = None,
) -> list[Settlement]:
    """The research rows whose claims the pass can settle now. Pure.

    One settlement per open claimed row carrying the research label
    (and not the researched one) whose `holders` entry names the
    research role behind the current holder, and whose comments the
    pass read. `holders` maps (repo, number) to the pre-resolved
    (agent, role) pair the pass read off the finished registry entry
    through its one finished-entry reader, so this function never
    indexes the entry shape itself. `projects` maps (repo, number)
    to the driving project, resolved by the pass where the project
    context still exists; a missing entry settles with no project,
    and the pass records that finding nowhere. `has_findings` is
    whether the comments hold a Findings comment. Unreadable
    comments (None, or a missing key) settle nothing: without the
    comments there is no proof either way, so the claim stays held
    for a later pass.
    """
    due: list[Settlement] = []
    for item in items or ():
        labels = tuple(getattr(item, "labels", None) or ())
        if getattr(item, "state", "open") != "open":
            continue
        if iface.RESEARCH not in labels or iface.RESEARCHED in labels:
            continue
        holder = getattr(item, "agent", "") or ""
        if not holder:
            continue
        seen = (holders or {}).get((item.repo, item.number))
        if seen is None:
            continue
        agent, role = seen
        if role != "research" or agent != holder:
            continue
        if comments_by_key is None or (item.repo, item.number) not in comments_by_key:
            continue
        comments = comments_by_key[(item.repo, item.number)]
        if comments is None:
            continue
        found = findings(comments)
        project = (projects or {}).get((item.repo, item.number)) or ""
        due.append(
            Settlement(
                repo=item.repo,
                number=item.number,
                agent=holder,
                has_findings=found is not None,
                findings=found or "",
                project=project if isinstance(project, str) else "",
            )
        )
    return due
