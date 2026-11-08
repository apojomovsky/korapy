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
from typing import NamedTuple

from korapy import heavy, paths, settings, worktrees

#: The fixed vocabulary of directory bases. Each resolves through its
#: existing helper; anything else is exit 4, never a guessed dir.
BASES = ("cache", "state", "sessions", "worktree")

DIR_SELECTORS = ("age", "count", "size")


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
