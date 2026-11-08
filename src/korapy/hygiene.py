"""Project-declared host hygiene rules for `korapy gc` and `korapy disk`.

Design: docs/15-project-hygiene.md. Rules live in the ops root's
`korapy.toml` as top-level `[hygiene.<name>]` tables, one table per
rule. Each rule names the repo it belongs to and exactly one kind
(`dir`, `image`, `session`). `check_table` validates the shape with
strict keys (an unknown key is exit 4, never a silent default);
`rules` reads and validates the ops root's tables, refusing the whole
load when any rule is bad; `declared_roots` is the one function that
joins a rule's `base` and `path`, and refuses roots that escape their
base or touch protected ground. Validation errors are
`settings.SettingsError` (exit 4, configuration error), keeping the
public meaning.

Shape validation (`check_table`) is pure over the parsed table.
Placement validation (`declared_roots`) resolves real paths: callers
must call both before planning anything, and the disk wiring refuses
the command when either raises. Live worktree leaves under a
worktree-base root are not enumerated here (listing them needs the
git seam); the worktree root itself is protected ground, and the
per-ticket planning tickets exclude live leaves at plan time.

Planning (`plan`) is pure over injected scans: `dir` rules plan age,
count and size selectors, and `session` rules plan the `disk.py`
session proof unchanged (done registry entry, no live unit, older
than `retention_days`). Image rules are skipped: ticket 3 plans them.
"""

import os
from dataclasses import dataclass
from typing import NamedTuple

from korapy import disk, heavy, paths, registry, settings, worktrees

#: The fixed vocabulary of directory bases. Each resolves through its
#: existing helper; anything else is exit 4, never a guessed dir.
BASES = ("cache", "state", "sessions", "worktree")

DIR_SELECTORS = ("age", "count", "size")

#: The candidate kind directory rows join the `disk` plan as.
HYGIENE_DIR = "hygiene-dir"

#: The candidate kind proven-shape session rows join the `disk` plan
#: as. Leaves with no registry entry read as `disk.UNPROVEN` keep
#: rows instead, the `disk.py` precedent.
HYGIENE_SESSION = "hygiene-session"

#: Seconds in a day, for the `age` cutoff.
DAY_S = 24 * 60 * 60


@dataclass(frozen=True)
class DirEntry:
    """One immediate entry under a rule root, as the `scan` seam reports it.

    `mtime` is the newest modification time under the entry, None when
    unreadable; `size` is the bytes under it. `realpath` is the entry
    resolved past symlinks (the scanner resolves it); it defaults to
    `path` when the scanner already hands back resolved paths.
    """

    path: str
    mtime: float | None
    size: int
    is_symlink: bool = False
    realpath: str | None = None


@dataclass(frozen=True)
class Candidate:
    """One directory row: what, how big, why, and whether proven safe."""

    kind: str
    path: str
    bytes: int
    proof: str
    safe: bool


class Rule(NamedTuple):
    """One validated `[hygiene.<name>]` rule, exactly as declared."""

    name: str
    repo: str
    kind: str
    base: str | None
    path: str | None
    selector: str | None
    days: int | None = None
    keep: int | None = None
    max_bytes: int | None = None
    prefix: str | None = None
    older_than_days: int | None = None
    retention_days: int | None = None


def _fail(where, name, text):
    """A shape error naming the file and the rule (exit 4)."""
    return settings.SettingsError(f"{where}: [hygiene.{name}] {text}")


def _whole(value, where, name, what, minimum):
    """`value` as an int of at least `minimum`, else a refusal.

    Integral floats read as ints (the `heavy` caps precedent); bools,
    strings and fractions are refused, never truncated.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _fail(where, name, f"{what} must be a number, not {value!r}")
    if isinstance(value, float):
        if not value.is_integer():
            raise _fail(where, name, f"{what} must be a whole number, not {value!r}")
        value = int(value)
    if value < minimum:
        raise _fail(where, name, f"{what} {value!r} must be at least {minimum}")
    return value


def _check_repo(raw, where, name):
    repo = raw.get("repo")
    if not isinstance(repo, str) or not paths.plain_repo_name(repo):
        raise _fail(where, name, f"repo {repo!r} is not a plain repository name")
    return repo


def _selector_key(selector):
    """The numeric key a directory `selector` takes."""
    return {"age": "days", "count": "keep", "size": "max_bytes"}[selector]


def _check_dir(raw, where, name):
    """`(base, path, selector, days, keep, max_bytes)` for a dir rule."""
    selector = raw.get("selector")
    if selector not in DIR_SELECTORS:
        raise _fail(where, name, f"selector {selector!r} must be one of {', '.join(DIR_SELECTORS)}")
    allowed = {"repo", "kind", "base", "path", "selector", _selector_key(selector)}
    for key in raw:
        # None reads as absent (dict-built rules; TOML has no nulls),
        # so a count rule carrying `"days": None` is not an extra key.
        if key not in allowed and raw[key] is not None:
            raise _fail(where, name, f"has no key {key!r}")
    base = raw.get("base")
    if base not in BASES:
        raise _fail(where, name, f"base {base!r} must be one of {', '.join(BASES)}")
    path = raw.get("path")
    if not isinstance(path, str) or not path:
        raise _fail(where, name, f"path {path!r} must be a non-empty string")
    want = _selector_key(selector)
    if raw.get(want) is None:
        raise _fail(where, name, f"{selector} needs {want}")
    value = _whole(raw.get(want), where, name, want, 1)
    return (
        base,
        path,
        selector,
        value if selector == "age" else None,
        (value if selector == "count" else None),
        (value if selector == "size" else None),
    )


def _check_image(raw, where, name, repo):
    """`(prefix, older_than_days)` for an image rule, repo-scoped."""
    allowed = {"repo", "kind", "prefix", "selector", "older_than_days"}
    for key in raw:
        if key not in allowed and raw[key] is not None:
            raise _fail(where, name, f"has no key {key!r}")
    if raw.get("selector") != "unreferenced":
        raise _fail(where, name, f"selector {raw.get('selector')!r} must be 'unreferenced'")
    prefix = raw.get("prefix")
    if (
        not isinstance(prefix, str)
        or not prefix
        or not (prefix.startswith(repo + "-") or prefix.startswith(repo + "/"))
        or len(prefix) <= len(repo) + 1
    ):
        raise _fail(
            where,
            name,
            f"prefix {prefix!r} must start with the rule's repo {repo!r} plus '-' or '/'",
        )
    floor = raw.get("older_than_days")
    if floor is None:
        return prefix, None
    return prefix, _whole(floor, where, name, "older_than_days", 0)


def _check_session(raw, where, name):
    """`(base, path, retention_days)` for a session rule."""
    allowed = {"repo", "kind", "base", "path", "retention_days"}
    for key in raw:
        if key not in allowed and raw[key] is not None:
            raise _fail(where, name, f"has no key {key!r}")
    base = raw.get("base")
    if base not in BASES:
        raise _fail(where, name, f"base {base!r} must be one of {', '.join(BASES)}")
    path = raw.get("path")
    if not isinstance(path, str) or not path:
        raise _fail(where, name, f"path {path!r} must be a non-empty string")
    if raw.get("retention_days") is None:
        raise _fail(where, name, "session needs retention_days")
    return base, path, _whole(raw.get("retention_days"), where, name, "retention_days", 1)


def _rule(name, raw, where):
    """The validated `Rule` for one `[hygiene.<name>]` table."""
    if not isinstance(name, str) or not name:
        raise settings.SettingsError(f"{where}: [hygiene] rule names must be non-empty strings")
    if not isinstance(raw, dict):
        raise _fail(where, name, "must be a table")
    kind = raw.get("kind")
    if kind not in ("dir", "image", "session"):
        raise _fail(where, name, f"kind {kind!r} must be one of dir, image, session")
    repo = _check_repo(raw, where, name)
    if kind == "dir":
        base, path, selector, days, keep, max_bytes = _check_dir(raw, where, name)
        return Rule(
            name=name,
            repo=repo,
            kind=kind,
            base=base,
            path=path,
            selector=selector,
            days=days,
            keep=keep,
            max_bytes=max_bytes,
        )
    if kind == "image":
        prefix, floor = _check_image(raw, where, name, repo)
        return Rule(
            name=name,
            repo=repo,
            kind=kind,
            base=None,
            path=None,
            selector="unreferenced",
            prefix=prefix,
            older_than_days=floor,
        )
    base, path, retention = _check_session(raw, where, name)
    return Rule(
        name=name,
        repo=repo,
        kind=kind,
        base=base,
        path=path,
        selector=None,
        retention_days=retention,
    )


def _check_prefix_overlap(rules, where):
    """Refuse image prefixes where one starts with the other.

    Two rules racing `docker rmi` over the same image must fail here,
    naming both rules, never at apply time.
    """
    images = sorted(
        (rule for rule in rules.values() if rule.kind == "image"),
        key=lambda rule: rule.name,
    )
    for pos, first in enumerate(images):
        for second in images[pos + 1 :]:
            assert first.prefix is not None and second.prefix is not None
            if first.prefix.startswith(second.prefix) or second.prefix.startswith(first.prefix):
                raise settings.SettingsError(
                    f"{where}: [hygiene.{first.name}] prefix {first.prefix!r} "
                    f"overlaps [hygiene.{second.name}] prefix {second.prefix!r}"
                )


def check_table(raw, where):
    """The validated `[hygiene]` table as `{name: Rule}`, or {} when absent.

    `raw` is the carried table (the `[heavy]` precedent): strict keys,
    one kind per rule with exactly its own keys, repo-scoped image
    prefixes with no overlaps. Any bad rule refuses the whole load.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise settings.SettingsError(f"{where}: [hygiene] must be a table of rules")
    rules = {name: _rule(name, entry, where) for name, entry in raw.items()}
    _check_prefix_overlap(rules, where)
    return rules


def rules(root):
    """The validated hygiene rules in the ops root's `korapy.toml`.

    `{}` when there is no root, no file, or no `[hygiene]` table. A
    bad rule refuses the whole load with exit 4, naming the rule.
    """
    if not root:
        return {}
    table = settings.read_project(root)
    return check_table(table.get("hygiene", {}), os.path.join(root, settings.PROJECT_FILE))


def _abs_clone(root, clone):
    """A clone path as an absolute path, resolved against the ops root.

    The one place in `hygiene.py` a relative `clone.path` joins the ops
    root: both the base resolver and the protected-grounds listing read
    through here, so the worktree base and the recorded clone ground
    never disagree.
    """
    if os.path.isabs(clone):
        return clone
    return os.path.normpath(os.path.join(root, clone))


def _base_dir(rule, root, env, worktrees_setting):
    """The base directory a directory rule's `path` sits under.

    Each base resolves through its existing helper, never a second
    copy. The worktree base resolves through the rule's repo clone
    and the `git.worktrees` setting, defaulting to the registered
    default when the caller passes none.
    """
    if rule.base == "cache":
        return paths.cache_dir(env)
    if rule.base == "state":
        return paths.state_dir(env)
    if rule.base == "sessions":
        return paths.sessions_dir(env)
    if root is None:
        raise settings.SettingsError(f"[hygiene.{rule.name}] base 'worktree' needs the ops root")
    try:
        resolved = settings.load(root=root, repo=rule.repo, env=env)
    except settings.SettingsError as err:
        raise settings.SettingsError(f"[hygiene.{rule.name}] {err}") from None
    clone = _abs_clone(root, resolved["clone.path"])
    setting = worktrees_setting
    if setting is None:
        setting = settings.REGISTRY["git.worktrees"].default
    try:
        return worktrees.root(os.path.realpath(clone), setting)
    except worktrees.WorktreeError as err:
        raise settings.SettingsError(f"[hygiene.{rule.name}] {err}") from None


def _protected_dirs(rule, root, env, home, worktrees_setting):
    """`(label, path)` pairs no rule root may be or contain.

    Bases resolve through `env`, so tests redirect them at temp dirs
    and never touch the real state, sessions or cache dirs. `root` is
    the ops root: `declared_roots` refuses before reaching here
    without it, so the clone and worktree-root entries below are
    only skipped by direct callers passing none.
    """
    state = paths.state_dir(env)
    found = [
        ("the home directory", home if home is not None else os.path.expanduser("~")),
        ("the state directory", state),
        ("the cache directory", paths.cache_dir(env)),
        ("the sessions directory", paths.sessions_dir(env)),
        ("the heavy ledger dir", heavy.runs_dir(state_dir=state)),
        ("the host ledger file", paths.host_ledger_path(env)),
    ]
    if root is None:
        return found
    setting = worktrees_setting
    if setting is None:
        setting = settings.REGISTRY["git.worktrees"].default
    for repo in settings.repo_names(root):
        try:
            resolved = settings.load(root=root, repo=repo, env=env)
        except settings.SettingsError as err:
            raise settings.SettingsError(f"[hygiene.{rule.name}] {err}") from None
        clone = _abs_clone(root, resolved["clone.path"])
        found.append((f"the clone of repo {repo!r}", clone))
        try:
            grounds = worktrees.root(os.path.realpath(clone), setting)
        except worktrees.WorktreeError as err:
            raise settings.SettingsError(f"[hygiene.{rule.name}] {err}") from None
        found.append((f"the worktree root of repo {repo!r}", grounds))
    return found


def declared_roots(rules, *, root, env=None, worktrees_setting=None, home=None):
    """`{name: realpath}` for every rule with a directory root.

    The one function that joins a rule's `base` and `path`. `root` is
    the ops root and is required: without it the clone and
    worktree-root entries of protected ground would be silently
    skipped, so any rule with a directory root refuses naming the
    rule instead. The root must sit strictly below its base (never
    equal to it, never escaping it, symlinks resolved), must not name
    a `reviews` record dir, and must neither be nor contain protected
    ground (home, clones, worktree roots, state, sessions, cache,
    ledger). Any refusal is exit 4, naming the rule. Image rules
    carry no directory root and are skipped.
    """
    if root is None:
        offenders = sorted(name for name, rule in rules.items() if rule.kind != "image")
        if offenders:
            raise settings.SettingsError(f"[hygiene.{offenders[0]}] needs the ops root")
        return {}
    found = {}
    for name, rule in rules.items():
        if rule.kind == "image":
            continue
        assert rule.base is not None and rule.path is not None
        base_dir = _base_dir(rule, root, env, worktrees_setting)
        real_base = os.path.realpath(base_dir)
        real = os.path.realpath(os.path.join(base_dir, rule.path))
        if real == real_base:
            raise settings.SettingsError(
                f"[hygiene.{name}] root must sit strictly below its base, never equal to it: {real}"
            )
        if not worktrees.inside(real, real_base):
            raise settings.SettingsError(f"[hygiene.{name}] root resolves outside its base: {real}")
        if "reviews" in os.path.normpath(rule.path).split(os.sep):
            raise settings.SettingsError(
                f"[hygiene.{name}] root must never name a reviews record dir"
            )
        for label, grounds in _protected_dirs(rule, root, env, home, worktrees_setting):
            real_grounds = os.path.realpath(grounds)
            if real == real_grounds:
                raise settings.SettingsError(f"[hygiene.{name}] root {real} is {label}")
            if worktrees.inside(real_grounds, real):
                raise settings.SettingsError(
                    f"[hygiene.{name}] root {real} contains {label} {real_grounds}"
                )
        found[name] = real
    return found


def _resolved(entry):
    """The entry resolved past symlinks, as the scanner reported it."""
    return entry.realpath if entry.realpath is not None else entry.path


def _below_any(resolved, roots):
    """Whether `resolved` sits strictly below one of `roots` (pure strings)."""
    for root in roots:
        try:
            if worktrees.inside(resolved, root):
                return True
        except ValueError:
            continue
    return False


def _drop_note(found, roots):
    """The drop note for an unscannable entry, else None.

    Symlinks are never candidates and never followed; anything resolving
    outside every declared root is dropped. Both drops are notes, never
    rows: a dropped path must not read as kept or safe.
    """
    if found.is_symlink:
        return f"dropped {found.path}: is a symlink"
    if not _below_any(_resolved(found), roots):
        return f"dropped {found.path}: resolves outside a declared root"
    return None


def _eligible(rule, entries, roots, notes):
    """`(kept, live)` after the symlink and escape drops."""
    kept = []
    live = []
    for found in entries:
        dropped = _drop_note(found, roots)
        if dropped is not None:
            notes.append(dropped)
            continue
        if found.mtime is None:
            kept.append(
                Candidate(
                    HYGIENE_DIR,
                    found.path,
                    found.size,
                    f"kept: modification time is unreadable (rule {rule.name})",
                    False,
                )
            )
            continue
        live.append(found)
    return kept, live


def _session_eligible(rule, entries, roots, notes):
    """`(kept, live)` for a session rule after the session drops.

    Symlink and escape drops read as notes, the directory precedent. A
    `reviews` record dir is never a candidate (review evidence is
    append-only proof), and a leaf parsed for another repo is skipped
    with a note, never a candidate under this rule. An unreadable
    mtime is a `hygiene-session` keep row, the `disk.py` session
    precedent, never a `hygiene-dir` row.
    """
    kept = []
    live = []
    for found in entries:
        dropped = _drop_note(found, roots)
        if dropped is not None:
            notes.append(dropped)
            continue
        if os.path.basename(found.path) == "reviews":
            notes.append(
                f"dropped {found.path}: reviews record dirs are never candidates (rule {rule.name})"
            )
            continue
        parsed = paths.parse_ticket_dirname(os.path.basename(found.path))
        if parsed is not None and parsed[0] != rule.repo:
            notes.append(
                f"dropped {found.path}: belongs to repo {parsed[0]!r}, rule is for {rule.repo!r}"
            )
            continue
        if found.mtime is None:
            kept.append(
                Candidate(
                    HYGIENE_SESSION,
                    found.path,
                    found.size,
                    f"kept: its modification time is unreadable (rule {rule.name})",
                    False,
                )
            )
            continue
        live.append(found)
    return kept, live


def _plan_session(rule, idle, *, now, entries, live, notes):
    """Safe rows for done, idle leaves past `retention_days`, keeps else.

    The `disk.py` session proof unchanged: the registry entry for the
    ticket is done, no live unit holds it, and the newest mtime under
    the leaf is older than `retention_days`. Leaf names that do not
    parse as ticket dirs, or parse but hold no registry entry, are
    `disk.UNPROVEN` keep rows. A `None` live set (the listing itself
    failed) keeps every leaf with its reason, once noted.
    """
    assert rule.retention_days is not None
    cutoff = now - rule.retention_days * DAY_S
    done = disk.done_tickets(entries, now)
    rows = []
    saw_ticket = False
    for found in idle:
        assert found.mtime is not None
        name = os.path.basename(found.path)
        parsed = paths.parse_ticket_dirname(name)
        ticket = None
        if parsed is not None:
            if parsed in done:
                ticket = parsed
            else:
                for record in entries:
                    if registry.ticket_of(record) == parsed:
                        ticket = parsed
                        break
        if ticket is None:
            rows.append(
                Candidate(
                    disk.UNPROVEN,
                    found.path,
                    found.size,
                    f"rule {rule.name}; session dir {name} has no registry entry: "
                    "cannot prove nothing is lost",
                    False,
                )
            )
            continue
        saw_ticket = True
        state = "done" if ticket in done else "open"
        is_live = live is not None and ticket in live
        aged = int((now - found.mtime) // DAY_S)
        old = found.mtime < cutoff
        if state == "done" and not is_live and live is not None and old:
            rows.append(
                Candidate(
                    HYGIENE_SESSION,
                    found.path,
                    found.size,
                    f"rule {rule.name}; registry done; no live unit; "
                    f"untouched for {aged}d, rule keeps {rule.retention_days}d",
                    True,
                )
            )
            continue
        reasons = []
        if state != "done":
            reasons.append("the registry entry is still open")
        if live is None:
            reasons.append("the live listing failed")
        elif is_live:
            reasons.append("a live unit holds it")
        if not old:
            reasons.append("it was modified within the retention")
        rows.append(
            Candidate(
                HYGIENE_SESSION,
                found.path,
                found.size,
                f"kept: {'; '.join(reasons)} (rule {rule.name})",
                False,
            )
        )
    if (
        live is None
        and saw_ticket
        and not any(
            note.startswith(f"rule {rule.name}:") and "live units unreadable" in note
            for note in notes
        )
    ):
        notes.append(f"rule {rule.name}: live units unreadable: session candidates are kept")
    return rows


def _plan_age(rule, live, *, now):
    """Safe rows for entries older than `rule.days`, keep rows for the rest."""
    assert rule.days is not None
    cutoff = now - rule.days * DAY_S
    rows = []
    for found in live:
        assert found.mtime is not None
        aged = int((now - found.mtime) // DAY_S)
        if found.mtime < cutoff:
            rows.append(
                Candidate(
                    HYGIENE_DIR,
                    found.path,
                    found.size,
                    f"rule {rule.name}; untouched for {aged}d, rule keeps {rule.days}d",
                    True,
                )
            )
        else:
            rows.append(
                Candidate(
                    HYGIENE_DIR,
                    found.path,
                    found.size,
                    f"kept: touched {aged}d ago, rule keeps {rule.days}d",
                    False,
                )
            )
    return rows


def _by_mtime_name(found):
    """Oldest first; ties break by ascending basename (the count/size order)."""
    assert found.mtime is not None
    return (found.mtime, os.path.basename(found.path))


def _plan_count(rule, live, *, notes):
    """All but the newest `rule.keep` are safe; the survivors are named."""
    assert rule.keep is not None
    ordered = sorted(live, key=_by_mtime_name)
    survivors = ordered[-rule.keep :] if rule.keep else []
    safe = {found.path for found in ordered[: len(ordered) - len(survivors)]}
    rows = []
    for found in live:
        if found.path in safe:
            rows.append(
                Candidate(
                    HYGIENE_DIR,
                    found.path,
                    found.size,
                    f"rule {rule.name}; outside newest {rule.keep} by mtime",
                    True,
                )
            )
        else:
            rows.append(
                Candidate(
                    HYGIENE_DIR,
                    found.path,
                    found.size,
                    f"kept: within newest {rule.keep} by mtime (rule {rule.name})",
                    False,
                )
            )
    if survivors:
        notes.append(f"rule {rule.name}: keeps {', '.join(found.path for found in survivors)}")
    return rows


def _plan_size(rule, live, unreadable_bytes):
    """Oldest first, safe until the root would measure under `max_bytes`."""
    assert rule.max_bytes is not None
    ordered = sorted(live, key=_by_mtime_name)
    remaining = unreadable_bytes + sum(found.size for found in ordered)
    rows = []
    for found in ordered:
        if remaining <= rule.max_bytes:
            rows.append(
                Candidate(
                    HYGIENE_DIR,
                    found.path,
                    found.size,
                    f"kept: under budget now (rule {rule.name})",
                    False,
                )
            )
            continue
        rows.append(
            Candidate(
                HYGIENE_DIR,
                found.path,
                found.size,
                f"rule {rule.name}; oldest-first until under {rule.max_bytes} bytes",
                True,
            )
        )
        remaining -= found.size
    return rows


def plan(rules, roots, *, now, scan, entries=(), live=None):
    """`(candidates, notes)` planning `dir` and `session` rules over scans.

    `rules` is `{name: Rule}`, `roots` is `{name: realpath}` from
    `declared_roots` (a missing root for a planned rule refuses, naming
    the rule), and `scan(root)` lists that root's `DirEntry` rows.
    `entries` is `registry.load_all()` output and `live` is the
    `(repo, number)` ticket set, None when the listing itself failed
    (every session leaf is then kept). Image rules are skipped: ticket
    3 plans them. Nothing is deleted and nothing joins a base and a
    path here; `declared_roots` stays the one join.
    """
    candidates: list[Candidate] = []
    notes: list[str] = []
    grounds = tuple(roots.values())
    for name, rule in rules.items():
        if rule.kind == "image":
            continue
        root = roots.get(name)
        if root is None:
            raise settings.SettingsError(f"[hygiene.{name}] has no declared root")
        if rule.kind == "session":
            kept, idle = _session_eligible(rule, scan(root), grounds, notes)
            candidates.extend(kept)
            candidates.extend(
                _plan_session(rule, idle, now=now, entries=entries, live=live, notes=notes)
            )
            continue
        kept, idle = _eligible(rule, scan(root), grounds, notes)
        candidates.extend(kept)
        if rule.selector == "age":
            candidates.extend(_plan_age(rule, idle, now=now))
        elif rule.selector == "count":
            candidates.extend(_plan_count(rule, idle, notes=notes))
        elif rule.selector == "size":
            candidates.extend(_plan_size(rule, idle, sum(found.bytes for found in kept)))
        else:  # pragma: no cover - check_table refuses any other selector
            raise settings.SettingsError(
                f"[hygiene.{name}] selector {rule.selector!r} is not planned"
            )
    return tuple(candidates), tuple(notes)
