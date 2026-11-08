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
"""

import os
from dataclasses import dataclass
from typing import NamedTuple

from korapy import heavy, paths, settings, worktrees

#: The fixed vocabulary of directory bases. Each resolves through its
#: existing helper; anything else is exit 4, never a guessed dir.
BASES = ("cache", "state", "sessions", "worktree")

DIR_SELECTORS = ("age", "count", "size")

#: The candidate kind directory rows join the `disk` plan as.
HYGIENE_DIR = "hygiene-dir"

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
    clone = resolved["clone.path"]
    if not os.path.isabs(clone):
        clone = os.path.normpath(os.path.join(root, clone))
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
    the ops root: without it the clone and worktree-root entries are
    skipped, so production callers always pass it.
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
        clone = resolved["clone.path"]
        if not os.path.isabs(clone):
            clone = os.path.normpath(os.path.join(root, clone))
        found.append((f"the clone of repo {repo!r}", clone))
        try:
            grounds = worktrees.root(os.path.realpath(clone), setting)
        except worktrees.WorktreeError as err:
            raise settings.SettingsError(f"[hygiene.{rule.name}] {err}") from None
        found.append((f"the worktree root of repo {repo!r}", grounds))
    return found


def declared_roots(rules, *, root=None, env=None, worktrees_setting=None, home=None):
    """`{name: realpath}` for every rule with a directory root.

    The one function that joins a rule's `base` and `path`. The root
    must sit strictly below its base (never equal to it, never
    escaping it, symlinks resolved), must not name a `reviews`
    record dir, and must neither be nor contain protected ground
    (home, clones, worktree roots, state, sessions, cache, ledger).
    Any refusal is exit 4, naming the rule. Image rules carry no
    directory root and are skipped.
    """
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


def _eligible(rule, entries, roots, notes):
    """`(kept_unreadable, entries)` after the symlink and escape drops.

    Symlinks are never candidates and never followed; anything resolving
    outside every declared root is dropped with a note. Both drops are
    notes, never rows: a dropped path must not read as kept or safe.
    """
    kept = []
    live = []
    for found in entries:
        if found.is_symlink:
            notes.append(f"dropped {found.path}: is a symlink")
            continue
        if not _below_any(_resolved(found), roots):
            notes.append(f"dropped {found.path}: resolves outside a declared root")
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
    safe = {id(found) for found in ordered[: len(ordered) - len(survivors)]}
    rows = []
    for found in live:
        if id(found) in safe:
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


def plan(rules, roots, *, now, scan):
    """`(candidates, notes)` planning every `dir` rule over injected scans.

    `rules` is `{name: Rule}`, `roots` is `{name: realpath}` from
    `declared_roots` (a missing root for a dir rule refuses, naming the
    rule), and `scan(root)` lists that root's `DirEntry` rows. Image and
    session rules are skipped: tickets 3 and 4 plan them. Nothing is
    deleted and nothing joins a base and a path here; `declared_roots`
    stays the one join.
    """
    candidates: list[Candidate] = []
    notes: list[str] = []
    grounds = tuple(roots.values())
    for name, rule in rules.items():
        if rule.kind != "dir":
            continue
        root = roots.get(name)
        if root is None:
            raise settings.SettingsError(f"[hygiene.{name}] has no declared root")
        kept, live = _eligible(rule, scan(root), grounds, notes)
        candidates.extend(kept)
        if rule.selector == "age":
            candidates.extend(_plan_age(rule, live, now=now))
        elif rule.selector == "count":
            candidates.extend(_plan_count(rule, live, notes=notes))
        elif rule.selector == "size":
            candidates.extend(_plan_size(rule, live, sum(found.bytes for found in kept)))
        else:  # pragma: no cover - check_table refuses any other selector
            raise settings.SettingsError(
                f"[hygiene.{name}] selector {rule.selector!r} is not planned"
            )
    return tuple(candidates), tuple(notes)
