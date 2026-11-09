"""Read-only health report: `korapy doctor`.

One report over the checks that already exist, plus the host checks that do
not. It changes nothing and installs nothing; a failing check prints the
command a human would run, in the style of `korapy deps`.

Every external call comes through `Env`: `run` reads `loginctl` and the
slice dumps, `list_fn` lists the systemd user units, and the rest arrives
as plain data assembled by `production_env`. Only the CLI builds that;
tests inject fakes.
"""

import argparse
import functools
import getpass
import json
import os
import re
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from typing import NamedTuple
from korapy import (
    accounts,
    admission,
    boarddoctor,
    deps,
    detect,
    drive,
    freeze,
    heavy,
    host_ledger,
    hub,
    launch,
    paths,
    pressure,
    profiles,
    projects,
    prompts,
    pushenv,
    quota,
    quota_ledger,
    quotacli,
    registry,
    repodoctor,
    resourcedoctor,
    resources,
    settings,
    units,
)
from korapy import board as store

OK, UNREACHABLE, USAGE = 0, 3, 4

SCHEMA_VERSION = 1


class Check(NamedTuple):
    """One row of the report: what was looked at, and how it reads."""

    name: str
    status: str  # "ok" | "warn" | "fail"
    detail: str
    fix: str


class HubProbe(NamedTuple):
    """The hub version probe, gathered once by production, carried as data.

    `status` is one of "ok", "unreachable", "unauthorized" or
    "version-mismatch"; `detail` already names the hub and the failure.
    `hub_api` and `hub_schema` are the hub's reported versions, 0 when
    the hub never answered. Tests inject values; only production calls
    the network, exactly once per report.
    """

    status: str
    detail: str
    hub_api: int = 0
    hub_schema: int = 0


class HubAuth(NamedTuple):
    """This host's hub token file, gathered once by production, as data.

    `status` is one of "ok", "missing" or "bad" (unreadable, empty, or
    readable beyond its owner); `detail` names the file and the
    problem. Tests inject values.
    """

    status: str
    detail: str


class ProfileState(NamedTuple):
    """The profile readings for the report, gathered once by production.

    `parsed` are the validated declarations (empty when none is
    declared); `error` is why they are unusable (bad shape, unknown
    provider or runtime, unreadable pool), or None. `sources` maps each
    name to its winning source path and `shadowed` maps a shadowed name
    to the committed source the local table hides. `selections` are the
    local selections (None when the file is unreadable, with the reason
    in `selections_error`); `committed` are the committed defaults as
    `{"default", "repos", "roles"}`; `now` is the clock the override
    expiry is read against. Tests inject values.
    """

    parsed: Mapping[str, profiles.Profile]
    error: str | None
    sources: Mapping[str, str]
    shadowed: Mapping[str, str]
    selections: profiles.Selections | None
    selections_error: str | None
    committed: Mapping[str, object]
    now: float = 0.0


class Env(NamedTuple):
    """Everything `run_checks` reads. Only the first rows are required arguments."""

    dep_results: Sequence[deps.Result]
    account_problems: Sequence[str]
    settings_error: str | None
    state_error: str | None
    user: str
    unit_prefix: str
    run: Callable[[Sequence[str]], object]
    list_fn: Callable[[str], list]
    reader_errors: Sequence[tuple[str, str, str]]
    registry_rows: Sequence[tuple[Mapping, str]]
    registry_error: str | None = None
    #: The resolved `agents.max_time` in seconds for the max-time row.
    #: None means the caller has no reading, and then the report carries
    #: no max-time row.
    max_time_s: float | None = None
    #: The resolved `agents.reviewer_max_time` in seconds for the reviewer
    #: max-time row. None means it follows `agents.max_time`, and then
    #: the report carries no reviewer row.
    reviewer_max_time_s: float | None = None
    deps_error: str | None = None
    #: Roles with invalid prompt overrides, as "role: problem".
    prompts_invalid: Sequence[str] = ()
    #: Roles whose ejected prompt is older than the built-in.
    prompts_stale: Sequence[str] = ()
    #: The prompts check itself failed (usage or configuration error).
    prompts_error: str | None = None
    #: The disk pressure (`ok`, `low`, `critical`, `unknown`), or None when
    #: the caller has no reading: then the report carries no disk row.
    disk: str | None = None
    #: The host-budget readings for the `resources*` rows. None means the
    #: caller has no reading, and then the report carries no resources row.
    resource_readings: resourcedoctor.ResourceReadings | None = None
    #: Repos in board mode to check, with the seams that check them:
    #: `board_fetch` resolves one board config to its live snapshot
    #: (raw fields, title, owner boards), `board_open_store` opens the
    #: store parity rows come from, and `board_items` lists the board's
    #: `(repo, number)` pairs for the missing-from-board row. Empty
    #: means no board repos, and then the report carries no board row.
    board_sites: Sequence[boarddoctor.BoardSite] = ()
    board_fetch: Callable[[settings.BoardConfig], object] | None = None
    board_open_store: Callable[[], store.Store] | None = None
    board_items: Callable[[settings.BoardConfig], object] | None = None
    #: The board assembly itself failed: one failing `board` row.
    board_error: str | None = None
    #: Projects whose remote-dependent rows are checked. Empty means the
    #: caller has no reading, and then the report carries no remote row.
    repo_sites: Sequence[repodoctor.RepoSite] = ()
    #: Problems with the project registry (bad registration files and
    #: policy entries that do not resolve): None means the caller has
    #: no reading, and then the report carries no `drive-projects` row.
    project_problems: Sequence[str] | None = None
    #: The drive timer state ("enabled", "disabled", "masked",
    #: "missing", or "unreachable: ..."): None means the caller has no
    #: reading, and then the report carries no `drive-timer` row.
    timer_state: str | None = None
    #: The resolved `hub.url`: None or empty means single-host, and then
    #: the report carries no hub rows. Every hub call site branches on
    #: this once, so single-host behavior is the absence of the step.
    hub_url: str | None = None
    #: This host's hub token file. None means the caller has no reading,
    #: and then the report carries no `hub-token` row.
    hub_auth: HubAuth | None = None
    #: The hub version probe. None means the caller has no reading, and
    #: then the report carries no `hub` row.
    hub_probe: HubProbe | None = None
    #: The profile readings. None means the caller has no reading, and
    #: then the report carries no profile row.
    profile_state: ProfileState | None = None
    #: The quota freshness view: the dict `quotacli.snapshot` returns,
    #: or the exception reading it raised. None means the caller has no
    #: reading, and then the report carries no quota row.
    quota_view: Mapping[str, object] | Exception | None = None
    #: Every resolving eligibility entry as (project, entry, source
    #: file), gathered once by production. None means the caller has no
    #: reading, and then the ok row stays generic; a bad entry is
    #: already a failing row through `project_problems`.
    project_policies: Sequence[tuple[str, str, str | None]] | None = None
    #: The freeze reading: the dict `freeze.freeze_view` returns, the
    #: exception reading it raised, or None. None means the caller has
    #: no reading, and then the report carries no freeze row.
    freeze_view: Mapping[str, object] | Exception | None = None
    #: The `freeze.max_age_s` setting the staleness warning reads
    #: against, resolved once by production.
    freeze_max_age_s: int = freeze.DEFAULT_MAX_AGE_S


def _check_deps(results: Sequence[deps.Result], error: str | None = None) -> list[Check]:
    if error is not None:
        return [
            Check(
                "deps",
                "fail",
                error,
                "reinstall korapy so system_deps.toml parses, then run `korapy deps`",
            )
        ]
    failed = [r for r in results if r.problem and not r.dep.optional]
    if not failed:
        total = len([r for r in results if not r.dep.optional])
        return [Check("deps", "ok", f"{total} required programs present", "")]
    checks = []
    for result in failed:
        fix = " ".join(deps.fixes([result]))
        checks.append(Check(f"deps:{result.dep.name}", "fail", result.problem or "", fix))
    return checks


def _check_accounts(problems: Sequence[str]) -> Check:
    if not problems:
        return Check("accounts", "ok", "the accounts file is usable", "")
    return Check(
        "accounts",
        "fail",
        "; ".join(problems),
        "correct the named [[provider]] entry in accounts.toml (see `korapy deps`)",
    )


def _check_settings(error: str | None) -> Check:
    if error is None:
        return Check("settings", "ok", "the host config and korapy.toml resolve", "")
    return Check(
        "settings",
        "fail",
        error,
        "fix the named file, then `korapy config show` to confirm it resolves",
    )


def _check_state_dirs(error: str | None) -> Check:
    if error is None:
        return Check("state-dir", "ok", "the state and config dirs are writable", "")
    return Check("state-dir", "fail", error, "create the directory or fix its permissions")


def _check_linger(user: str, run: Callable[[Sequence[str]], object]) -> Check:
    argv = ["loginctl", "show-user", user, "--property=Linger"]
    try:
        proc = run(argv)
    except (OSError, subprocess.SubprocessError) as err:
        return Check("linger", "fail", f"loginctl cannot be reached: {err}", _linger_fix())
    if getattr(proc, "returncode", 1) != 0:
        err = (getattr(proc, "stderr", "") or "").strip()
        detail = f"loginctl cannot be reached{(': ' + err) if err else ''}"
        return Check("linger", "fail", detail, _linger_fix())
    out = getattr(proc, "stdout", "") or ""
    if "Linger=yes" in out:
        return Check("linger", "ok", f"linger is on for {user}", "")
    return Check(
        "linger",
        "warn",
        f"linger is off for {user}; agents die at logout",
        'run `loginctl enable-linger "$USER"`',
    )


def _linger_fix() -> str:
    return "log in on the host console so the user manager starts"


_MANAGER_BUS_FIX = "log in again so the user manager starts, then list the user units"


def _check_manager(prefix: str, list_fn: Callable[[str], list]) -> Check:
    try:
        found = list_fn(prefix)
    except units.Unreachable as err:
        return Check("user-manager", "fail", str(err), _MANAGER_BUS_FIX)
    except units.UnitsError as err:
        return Check(
            "user-manager",
            "fail",
            str(err),
            "fix `agents.unit_prefix` in the host config, then run `korapy doctor` again",
        )
    return Check("user-manager", "ok", f"the user manager answers ({len(found)} units)", "")


def _check_drive_timer(state: str | None) -> list[Check]:
    """The drive timer row: no row without a reading, warn when the pass
    would never wake on its own, fail when the manager cannot be asked."""
    if state is None:
        return []
    if state == "enabled":
        return [
            Check(
                "drive-timer",
                "ok",
                f"the user timer {drive.TIMER_NAME} is enabled",
                "",
            )
        ]
    if state == "disabled":
        return [
            Check(
                "drive-timer",
                "warn",
                f"the user timer {drive.TIMER_NAME} is installed but not enabled",
                "run `korapy drive install-timer`",
            )
        ]
    if state == "masked":
        return [
            Check(
                "drive-timer",
                "warn",
                f"the user timer {drive.TIMER_NAME} is masked and cannot start",
                f"run `systemctl --user unmask {drive.TIMER_NAME}`, "
                "then `korapy drive install-timer`",
            )
        ]
    if state.startswith("unreachable:"):
        return [Check("drive-timer", "fail", state, _MANAGER_BUS_FIX)]
    return [
        Check(
            "drive-timer",
            "warn",
            f"the user timer {drive.TIMER_NAME} is not installed ({state})",
            "run `korapy drive install-timer`",
        )
    ]


def _check_drive_projects(
    problems: Sequence[str] | None,
    policies: Sequence[tuple[str, str, str | None]] | None = None,
) -> list[Check]:
    """The registry row: no row without a reading, one fail row joining
    every bad registration and every unreadable policy entry, else one
    ok row naming the file each resolving entry loaded from."""
    if problems is None:
        return []
    if problems:
        return [
            Check(
                "drive-projects",
                "fail",
                "; ".join(problems),
                "fix the named registration file or policy entry, then run `korapy doctor` again",
            )
        ]
    if policies:
        joined = "; ".join(
            f"project {name}: {entry!r} loaded from {source}"
            if source
            else f"project {name}: {entry!r} loaded"
            for name, entry, source in policies
        )
        return [
            Check(
                "drive-projects",
                "ok",
                f"every registration loads and every policy entry resolves: {joined}",
                "",
            )
        ]
    return [
        Check(
            "drive-projects",
            "ok",
            "every registration loads and every policy entry resolves",
            "",
        )
    ]


def _check_readers(errors: Sequence[tuple[str, str, str]]) -> list[Check]:
    if not errors:
        return [Check("quota-readers", "ok", "every configured reader loads", "")]
    named = []
    for provider, entry, error in errors:
        name = provider or entry
        named.append(
            Check(
                f"quota-readers:{name}" if name else "quota-readers",
                "fail",
                error,
                "correct the reader entry in accounts.toml (see `korapy deps`)",
            )
        )
    return named


def check_quota(view: Mapping[str, object] | Exception | None) -> list[Check]:
    """Per-provider quota freshness rows, pure over a snapshot view.

    None means the caller has no reading: no rows. An exception is
    one warn row: the pool or ledger could not be read. A fresh
    provider reads ok with its age; a stale one warns with the reason
    ("never ran", "reader failed", "too old"), so a dead reader never
    fails the report: admission already treats it as unknown, and the
    fix names the refresh that would heal it.
    """
    if view is None:
        return []
    if isinstance(view, Exception):
        return [
            Check(
                "quota",
                "warn",
                f"quota ledger unreadable: {view}",
                "make the state dir usable, then run `korapy doctor` again",
            )
        ]
    providers = view.get("providers", {}) if isinstance(view, dict) else {}
    if not isinstance(providers, dict):
        providers = {}
    now = view.get("now") if isinstance(view, dict) else None
    rows = []
    for name in sorted(providers):
        info = providers[name] if isinstance(providers[name], dict) else {}
        state = info.get("state", "unknown")
        read_at = info.get("read_at")
        try:
            age = (
                quotacli.format_age(now - read_at)
                if read_at is not None and now is not None
                else quotacli.format_age(None)
            )
        except TypeError:
            age = quotacli.format_age(None)
        reason = quotacli.stale_reason(info)
        if reason is None:
            rows.append(Check(f"quota:{name}", "ok", f"{state}, read {age}", ""))
            continue
        detail = f"{state}, stale: {reason}, read {age}"
        if reason == quotacli.READER_FAILED and info.get("error") is not None:
            detail += f": {info['error']}"
        rows.append(
            Check(
                f"quota:{name}",
                "warn",
                detail,
                f"run `korapy quota refresh {name}`, then run `korapy doctor` again",
            )
        )
    return rows


def _check_registry(rows: Sequence[tuple[Mapping, str]], error: str | None = None) -> Check:
    if error is not None:
        return Check(
            "registry",
            "fail",
            error,
            "make the state dir usable, then run `korapy doctor` again",
        )
    counts = {"live": 0, "stale": 0, "finished": 0}
    unticketed = 0
    for entry, state in rows:
        counts[state] = counts.get(state, 0) + 1
        if state == registry.LIVE and registry.ticket_of(entry) is None:
            unticketed += 1
    total = len(rows)
    detail = (
        f"{total} entries (live={counts.get('live', 0)}, "
        f"stale={counts.get('stale', 0)}, finished={counts.get('finished', 0)})"
    )
    if not unticketed:
        return Check("registry", "ok", detail, "")
    return Check(
        "registry",
        "warn",
        f"{detail}; {unticketed} live agents hold no ticket",
        "the agent should register its ticket (`korapy agent register --repo REPO --number N`)",
    )


def _format_span(seconds) -> str:
    """`seconds` as a short span: "45s", "30m", "4h", "2d". Pure."""
    try:
        secs = max(0, int(seconds))
    except (TypeError, ValueError):
        return "unknown"
    if secs < 60:
        return f"{secs}s"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m"
    hours = mins // 60
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d"


def _longest_run(rows) -> float | None:
    """The longest finished span in `rows`, or None when none reads.

    Pure: only finished rows with a readable non-negative span count.
    """
    longest = None
    for entry, state in rows or ():
        if state != registry.FINISHED:
            continue
        if not isinstance(entry, Mapping):
            continue
        start, end = entry.get("started_at"), entry.get("ended_at")
        if (
            isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, (int, float))
            or not isinstance(end, (int, float))
        ):
            continue
        try:
            run = float(end) - float(start)
        except (TypeError, ValueError, OverflowError):
            continue
        if run < 0:
            continue
        longest = run if longest is None else max(longest, run)
    return longest


def _max_time_rows(name, limit_s, rows, run_noun) -> list[Check]:
    """The one wall-clock row for `run_noun` runs: ok when the limit covers
    the longest, warn when a recent run outran it.

    Pure over the resolved limit and the registry rows. None means the
    caller has no reading: no row. No finished run with a readable span
    means nothing to compare: no row either.
    """
    if limit_s is None:
        return []
    longest = _longest_run(rows)
    if longest is None:
        return []
    if longest <= limit_s:
        return [
            Check(
                name,
                "ok",
                f"the {run_noun}wall-clock limit ({_format_span(limit_s)}) covers "
                f"the longest recent finished {run_noun}run ({_format_span(longest)})",
                "",
            )
        ]
    return [
        Check(
            name,
            "warn",
            f"the {run_noun}wall-clock limit ({_format_span(limit_s)}) is shorter than "
            f"the longest recent finished {run_noun}run ({_format_span(longest)})",
            f"raise `{name}` in korapy.toml, then run `korapy doctor` again",
        )
    ]


def check_max_time(limit_s, rows, reviewer_limit_s=None) -> list[Check]:
    """The wall-clock rows: warn when a limit is shorter than the longest
    recent finished run from the registry.

    Pure over the resolved limits and the registry rows. Without a
    reviewer override every finished run reads against `agents.max_time`,
    one row. With one, worker runs read against `agents.max_time` and
    reviewer runs (entries with the reviewer role) against
    `agents.reviewer_max_time`, one row per role that has a finished
    run, so a reviewer limit that would now kill reviewer runs warns
    even when the worker limit covers them. Either row warns so the
    operator raises the limit before the unit starts killing agents
    that were nearly done.
    """
    if reviewer_limit_s is None:
        return _max_time_rows("agents.max_time", limit_s, rows, "")
    worker = [
        (entry, state)
        for entry, state in (rows or ())
        if not (isinstance(entry, Mapping) and entry.get("role") == "reviewer")
    ]
    reviewing = [
        (entry, state)
        for entry, state in (rows or ())
        if isinstance(entry, Mapping) and entry.get("role") == "reviewer"
    ]
    return [
        *_max_time_rows("agents.max_time", limit_s, worker, ""),
        *_max_time_rows("agents.reviewer_max_time", reviewer_limit_s, reviewing, "reviewer "),
    ]


def _check_prompts(invalid: Sequence[str], stale_roles: Sequence[str], error: str | None) -> Check:
    if error is not None:
        return Check(
            "prompts",
            "fail",
            error,
            "fix the prompts configuration, then run `korapy prompts check`",
        )
    if invalid:
        return Check(
            "prompts",
            "fail",
            f"invalid prompt overrides: {'; '.join(invalid)}",
            "fix the placeholders or re-eject the template",
        )
    if stale_roles:
        return Check(
            "prompts",
            "warn",
            f"built-in changed since eject: {', '.join(stale_roles)}",
            "review the diff and re-eject with --force",
        )
    return Check("prompts", "ok", "the role prompts are built-in or valid overrides", "")


def _check_disk(pressure: str | None) -> list[Check]:
    """The disk row, reusing the report's row shape. No reading, no row."""
    if pressure is None:
        return []
    if pressure == "ok":
        return [Check("disk", "ok", "disk pressure ok", "")]
    if pressure == "low":
        return [
            Check(
                "disk",
                "warn",
                "disk pressure low: free space is under a threshold",
                "reclaim space: `korapy disk` plans, `korapy disk --apply` removes",
            )
        ]
    if pressure == "critical":
        return [
            Check(
                "disk",
                "fail",
                "disk pressure critical: free space is under half a threshold",
                "reclaim space now: `korapy disk` plans, `korapy disk --apply` removes",
            )
        ]
    return [
        Check(
            "disk",
            "warn",
            "disk pressure unknown: a filesystem could not be read",
            "check the state, cache and sessions dirs, then run `korapy doctor` again",
        )
    ]


def _check_resources(env: Env) -> list[Check]:
    """The host-budget rows: no row without a reading, else one per aspect."""
    if env.resource_readings is None:
        return []
    return [
        Check(row.name, row.status, row.detail, row.fix)
        for row in resourcedoctor.check(env.resource_readings)
    ]


def _check_boards(env: Env) -> list[Check]:
    """One row per repo in board mode, mapped from the board slice."""
    if env.board_error is not None:
        return [
            Check(
                "board",
                "fail",
                env.board_error,
                "fix the ops root's korapy.toml, then run `korapy doctor` again",
            )
        ]
    return [
        Check(row.name, row.status, row.detail, row.fix)
        for row in boarddoctor.check_sites(
            env.board_sites,
            fetch=env.board_fetch,
            open_store=env.board_open_store,
            board_items=env.board_items,
        )
    ]


def check_freeze(view: Mapping[str, object] | Exception | None, max_age_s: int) -> list[Check]:
    """The `drive-freeze` row: no row without a reading.

    A standing freeze reads ok with its reason and gate; past
    `max_age_s` it warns, so a crash-orphaned freeze never blocks
    starts silently. An unreadable freeze file fails: the loop holds
    on broken state and the operator must repair it.
    """
    if view is None:
        return []
    if isinstance(view, Exception):
        return [
            Check(
                "drive-freeze",
                "fail",
                f"freeze state unreadable: {view}",
                "repair or remove the freeze file, then run `korapy doctor` again",
            )
        ]
    reason = view.get("reason") or "no reason recorded"
    raw_until = view.get("until")
    until = [str(ref) for ref in raw_until] if isinstance(raw_until, list) else []
    age = view.get("age_s")
    detail = f"frozen: {reason}"
    if until:
        detail += f" (until {', '.join(until)} merge)"
    if isinstance(age, bool) or not isinstance(age, (int, float)):
        age_text = "age unknown"
        stale = False
    else:
        age_text = f"age {quotacli.format_age(age)}"
        stale = age > max_age_s
    detail += f", {age_text}"
    if not stale:
        return [Check("drive-freeze", "ok", detail, "")]
    return [
        Check(
            "drive-freeze",
            "warn",
            f"{detail}: older than the {max_age_s}s freeze window",
            "run `korapy freeze end` to resume dispatch",
        )
    ]


def _check_repos(sites: Sequence[repodoctor.RepoSite]) -> list[Check]:
    """The remote-dependent rows: warn without a remote, strict with one.

    No site, no row. `repodoctor` decides each row; this only renders.
    """
    return [
        Check(row.name, row.status, row.detail, row.fix) for row in repodoctor.check_sites(sites)
    ]


def _check_hub_auth(url: str | None, auth: HubAuth | None) -> list[Check]:
    """The hub-token row: no row without a configured hub or a reading."""
    if not url or auth is None:
        return []
    if auth.status == "ok":
        return [
            Check(
                "hub-token",
                "ok",
                "this host's hub token is present and owner-only",
                "",
            )
        ]
    return [
        Check(
            "hub-token",
            "fail",
            auth.detail,
            "install the token the operator minted for this host, then run `korapy doctor` again",
        )
    ]


def _check_hub(url: str | None, probe: HubProbe | None) -> list[Check]:
    """The hub row: no row without a configured hub or a reading.

    An unreachable hub warns: the outage is operational, hosts keep
    their held work, and the drive already exits 3 where it matters. A
    refused token or a version mismatch fails: both are configuration
    errors the operator must fix.
    """
    if not url or probe is None:
        return []
    if probe.status == "ok":
        return [
            Check(
                "hub",
                "ok",
                f"the hub answers at {url} (api {probe.hub_api}, schema {probe.hub_schema})",
                "",
            )
        ]
    if probe.status == "unreachable":
        return [
            Check(
                "hub",
                "warn",
                probe.detail,
                "check the hub is running and hub.url names it, then run `korapy doctor` again",
            )
        ]
    if probe.status == "unauthorized":
        return [
            Check(
                "hub",
                "fail",
                probe.detail,
                "install the token the operator minted for this host,"
                " then run `korapy doctor` again",
            )
        ]
    return [
        Check(
            "hub",
            "fail",
            probe.detail,
            "upgrade the hub and the hosts in lockstep, then run `korapy doctor` again",
        )
    ]


def _profile_references(state: ProfileState) -> list[tuple[str, str]]:
    """Every `(where, name)` a selection or default points at, in stable order."""
    refs: list[tuple[str, str]] = []
    selections = state.selections
    if selections is not None:
        if selections.default is not None:
            refs.append(("the local default", selections.default))
        for repo in sorted(selections.projects):
            refs.append((f"the local selection for project {repo!r}", selections.projects[repo]))
        for role in sorted(selections.roles):
            refs.append((f"the local selection for role {role!r}", selections.roles[role]))
        for key in sorted(selections.pins):
            refs.append((f"the pin for ticket {key}", selections.pins[key]))
        if selections.override is not None:
            refs.append(("the temporary override", selections.override.profile))
    committed = state.committed or {}
    default = committed.get("default")
    if isinstance(default, str) and default:
        refs.append(("the committed profile.default", default))
    repos = committed.get("repos")
    if isinstance(repos, dict):
        for repo in sorted(repos):
            refs.append((f"the committed [[repo]] {repo!r} profile", repos[repo]))
    roles = committed.get("roles")
    if isinstance(roles, dict):
        for role in sorted(roles):
            refs.append((f"the committed [role.{role}] profile", roles[role]))
    return refs


def _check_profile_declarations(state: ProfileState) -> list[Check]:
    """The `profiles` row: declarations validate and every reference dangles nowhere."""
    if state.error is not None:
        return [
            Check(
                "profiles",
                "fail",
                state.error,
                "fix the named declaration, then run `korapy doctor` again",
            )
        ]
    checks = []
    if state.selections_error is not None:
        checks.append(
            Check(
                "profiles",
                "fail",
                state.selections_error,
                "fix the selections file, then run `korapy doctor` again",
            )
        )
    dangling = [
        (where, name) for where, name in _profile_references(state) if name not in state.parsed
    ]
    if dangling:
        detail = "; ".join(f"{where} names undeclared profile {name!r}" for where, name in dangling)
        checks.append(
            Check(
                "profiles",
                "fail",
                detail,
                "declare [profiles.NAME] or remove the reference, then run `korapy doctor` again",
            )
        )
    if not checks:
        count = len(state.parsed)
        detail = f"{count} declared profile(s) validate" if count else "no profiles declared"
        checks.append(Check("profiles", "ok", detail, ""))
    return checks


def _check_profile_runtimes(state: ProfileState, dep_results) -> list[Check]:
    """The `profile-runtimes` rows: every referenced runtime's binary resolves.

    A missing binary fails with the install command the doctor prints but
    never runs (installing a runtime needs the maintainer's go-ahead). A
    runtime no profile references is not this check's business: the `deps`
    rows already cover it.
    """
    referenced = sorted({profile.runtime for profile in state.parsed.values()})
    if not referenced:
        return [Check("profile-runtimes", "ok", "no profile references a runtime", "")]
    by_name = {result.dep.name: result for result in dep_results}
    missing = []
    for runtime in referenced:
        result = by_name.get(runtime)
        if result is None:
            missing.append((runtime, f"no system_deps.toml probe for runtime {runtime!r}"))
        elif not result.ok:
            missing.append((runtime, result.problem or f"runtime {runtime!r} is missing"))
    if not missing:
        return [
            Check(
                "profile-runtimes",
                "ok",
                f"every referenced runtime resolves ({', '.join(referenced)})",
                "",
            )
        ]
    checks = []
    for runtime, problem in missing:
        names = sorted(name for name, profile in state.parsed.items() if profile.runtime == runtime)
        if runtime in by_name:
            fix = " ".join(deps.fixes([by_name[runtime]]))
        else:
            fix = f"add a system_deps.toml probe for runtime {runtime!r}"
        checks.append(
            Check(
                f"profile-runtimes:{runtime}",
                "fail",
                f"profile(s) {', '.join(names)} need runtime {runtime!r}: {problem}",
                fix,
            )
        )
    return checks


def _check_profile_override(state: ProfileState) -> list[Check]:
    """The `profile-override` row, only while an override stands.

    An expired override fails until `korapy profile reset --temp`
    clears it; the resolver already ignores it, so the row is the only
    thing that moves the operator to reset.
    """
    selections = state.selections
    override = selections.override if selections is not None else None
    if override is None:
        return []
    if profiles.is_expired(override, state.now):
        return [
            Check(
                "profile-override",
                "fail",
                f"the temporary override for scope {override.scope}"
                f" (profile {override.profile!r}) expired at {override.until!r}",
                "run `korapy profile reset --temp` to clear it",
            )
        ]
    return [
        Check(
            "profile-override",
            "ok",
            f"the temporary override for scope {override.scope}"
            f" (profile {override.profile!r}) runs until {override.until!r}",
            "",
        )
    ]


def _check_profile_shadows(state: ProfileState) -> list[Check]:
    """One `profiles:<name>` note per shadowed declaration, with both sources."""
    checks = []
    for name in sorted(state.shadowed):
        checks.append(
            Check(
                f"profiles:{name}",
                "warn",
                f"{name!r} is declared in both {state.shadowed[name]}"
                f" and {state.sources.get(name, 'the host config')}; the local table wins",
                "keep both (local wins) or remove one to silence this note",
            )
        )
    return checks


def _check_profile_default(state: ProfileState) -> list[Check]:
    """The `profile-default` row: a launch with no selection would fail.

    There is no shipped default, so the launch fails fast (exit 4) naming
    the fix, and the doctor fails the same way: the operator sees the
    refusal here instead of at launch time. Fails when declarations
    validate yet no layer-6 value stands: no `[profiles.default]` table,
    no local default and no committed default. A fail, never a warn: the
    launch path resolves the same way through `profiles.resolve`. With no
    profiles declared at all every launch fails, so that fails too.
    """
    if state.error is not None:
        return []
    if profiles.declared_default(state.parsed) is not None:
        return []
    selections = state.selections
    if selections is not None and selections.default is not None:
        return []
    committed = state.committed or {}
    if isinstance(committed.get("default"), str) and committed["default"]:
        return []
    if selections is None:
        return []
    if state.parsed:
        detail = (
            f"{len(state.parsed)} declared profile(s) but no default selected: "
            "a launch with no profile selection fails (exit 4)"
        )
    else:
        detail = "no profiles declared: a launch with no profile selection fails (exit 4)"
    return [Check("profile-default", "fail", detail, profiles.MISSING_DEFAULT_FIX)]


def check_profiles(state: ProfileState, dep_results) -> list[Check]:
    """The profile rows: declarations, runtimes, override, default and shadows.

    Pure over the readings in `state`: declarations validate, every
    selection, pin, override and committed default names a declared
    profile, every referenced runtime resolves, an expired override
    fails until reset, and a missing default over declared profiles (or
    over none at all) fails naming the fix. Unknown runtime, missing
    binary, expiry and a missing default fail; a shadowed name is a note.
    """
    if state.error is not None:
        return [
            *_check_profile_declarations(state),
            *_check_profile_override(state),
        ]
    return [
        *_check_profile_declarations(state),
        *_check_profile_runtimes(state, dep_results),
        *_check_profile_override(state),
        *_check_profile_default(state),
        *_check_profile_shadows(state),
    ]


def run_checks(env: Env) -> list[Check]:
    """Every check, in report order. Pure over the seams in `env`."""
    return [
        *_check_deps(env.dep_results, env.deps_error),
        _check_accounts(env.account_problems),
        _check_settings(env.settings_error),
        _check_state_dirs(env.state_error),
        _check_linger(env.user, env.run),
        _check_manager(env.unit_prefix, env.list_fn),
        *_check_drive_timer(env.timer_state),
        *_check_drive_projects(env.project_problems, env.project_policies),
        *check_freeze(env.freeze_view, env.freeze_max_age_s),
        *_check_readers(env.reader_errors),
        *check_quota(env.quota_view),
        _check_registry(env.registry_rows, env.registry_error),
        *check_max_time(env.max_time_s, env.registry_rows, env.reviewer_max_time_s),
        _check_prompts(env.prompts_invalid, env.prompts_stale, env.prompts_error),
        *_check_boards(env),
        *_check_repos(env.repo_sites),
        *_check_disk(env.disk),
        *_check_resources(env),
        *_check_hub_auth(env.hub_url, env.hub_auth),
        *_check_hub(env.hub_url, env.hub_probe),
        *(
            check_profiles(env.profile_state, env.dep_results)
            if env.profile_state is not None
            else []
        ),
    ]


def exit_code(checks: Sequence[Check]) -> int:
    """0 ok or warn only, 1 at least one fail, 3 the manager bus is gone.

    Only an unreachable bus selects 3: a linger failure, or a
    user-manager or drive-timer failure with the bus fix. A bad
    `agents.unit_prefix` is an ordinary fail with exit 1.
    """
    for check in checks:
        if check.status != "fail":
            continue
        if check.name == "linger":
            return UNREACHABLE
        if check.name == "user-manager" and check.fix == _MANAGER_BUS_FIX:
            return UNREACHABLE
        if check.name == "drive-timer" and check.fix == _MANAGER_BUS_FIX:
            return UNREACHABLE
    if any(c.status == "fail" for c in checks):
        return 1
    return OK


_SYMBOLS = {"ok": "✓", "warn": "!", "fail": "✗"}


def render(checks: Sequence[Check], json_mode: bool = False) -> str:
    """The report: one line per check, or one JSON document."""
    if json_mode:
        document = {
            "schema_version": SCHEMA_VERSION,
            "checks": [
                {"name": c.name, "status": c.status, "detail": c.detail, "fix": c.fix}
                for c in checks
            ],
        }
        return json.dumps(document, indent=2) + "\n"
    lines = []
    for check in checks:
        lines.append(f"{_SYMBOLS[check.status]} {check.name}: {check.detail}")
        if check.status != "ok":
            lines.append(f"  fix: {check.fix}")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------ production


def check_state_dirs(env: Mapping[str, str] | None = None) -> str | None:
    """None when the state and config dirs exist and take a probe write.

    The probe goes through the one atomic-write helper and is removed
    after, so a passing check leaves nothing behind. The probe name carries
    the pid so two concurrent doctors do not share it.
    """
    try:
        targets = (paths.state_dir(env), paths.config_dir(env))
    except paths.PathError as err:
        return str(err)
    for target in targets:
        try:
            os.makedirs(target, exist_ok=True)
            probe = os.path.join(target, f".doctor-probe-{os.getpid()}")
            paths.atomic_write_text(probe, "ok")
            try:
                os.unlink(probe)
            except FileNotFoundError:
                pass
        except OSError as err:
            return f"{target} is not writable: {err}"
    return None


def _resolved_settings(
    start: str, env: Mapping[str, str] | None = None
) -> tuple[settings.Resolved | None, str | None]:
    """(resolved, error): the settings and their failure, resolved once."""
    try:
        return settings.load(root=settings.find_root(start), env=env), None
    except (settings.SettingsError, paths.PathError) as err:
        return None, str(err)


def _production_settings(
    env: Mapping[str, str] | None = None,
) -> tuple[settings.Resolved | None, str | None]:
    """The live settings without ever raising on a deleted working directory."""
    try:
        start = os.getcwd()
    except OSError as err:
        return None, f"cannot determine the working directory: {err}"
    return _resolved_settings(start, env)


def check_settings(start: str, env: Mapping[str, str] | None = None) -> str | None:
    """None when the host config and the ops root's file resolve."""
    return _resolved_settings(start, env)[1]


def load_freeze_view(resolved: settings.Resolved | None):
    """The freeze reading, or the exception reading it raised.

    Only production builds it; tests inject the view into `Env`
    directly. A missing freeze file is None (no row); an unreadable
    one is the exception the `drive-freeze` row fails on. Reading
    never lists units or reaches the tracker.
    """
    if resolved is None:
        max_age_s = freeze.DEFAULT_MAX_AGE_S
    else:
        try:
            max_age_s = resolved["freeze.max_age_s"]
        except KeyError:
            max_age_s = freeze.DEFAULT_MAX_AGE_S
    try:
        path = freeze.freeze_path()
    except paths.PathError as err:
        return err
    try:
        return freeze.freeze_view(path, now=time.time(), max_age_s=max_age_s)
    except freeze.FreezeError as err:
        return err
    except OSError as err:
        return err


def load_dep_results() -> tuple[list[deps.Result], str | None]:
    """(results, error): the dep probes and their load failure, gathered once."""
    try:
        return deps.check_all(deps.load()), None
    except (
        tomllib.TOMLDecodeError,
        UnicodeDecodeError,
        OSError,
        TypeError,
        KeyError,
        re.error,
    ) as err:
        return [], str(err)


def load_quota_view(resolved: settings.Resolved | None):
    """The quota freshness view, or the exception reading it raised.

    Only production builds it; tests inject the view into `Env`
    directly. A missing accounts file is an empty pool, never an
    error; an unreadable pool or ledger is the exception the one
    `quota` row warns about. Reading never runs a reader.
    """
    if resolved is None:
        low_pct = quota.DEFAULT_LOW_PCT
    else:
        try:
            low_pct = resolved["quota.low_pct"]
        except KeyError:
            low_pct = quota.DEFAULT_LOW_PCT
    try:
        pool = accounts.providers()
    except (paths.PathError, accounts.AccountsError) as err:
        return err
    try:
        ledger = quota_ledger.load()
    except (paths.PathError, quota_ledger.LedgerError) as err:
        return err
    try:
        return quotacli.snapshot(pool, ledger, time.time(), low_pct=low_pct)
    except Exception as err:  # noqa: BLE001 - a broken view is report data, never a traceback
        return err


def load_readers(config_dir: str | None = None) -> list[tuple[str, str, str]]:
    """(provider, entry, error) for every reader that does not load."""
    try:
        found, dirs = accounts.pool(config_dir)
    except (accounts.AccountsError, paths.PathError) as err:
        return [("", "", str(err))]
    errors = []
    for provider in found:
        try:
            accounts.load_reader(provider.reader, reader_dirs=dirs)
        except accounts.AccountsError as err:
            errors.append((provider.name, provider.reader, str(err)))
    return errors


def production_run(argv: Sequence[str]) -> object:
    """The real loginctl seam. Only the CLI builds it; tests never call this."""
    return subprocess.run(list(argv), capture_output=True, text=True, timeout=10)


def production_list_fn(prefix: str) -> list:
    """The real unit listing. Only the CLI builds it; tests never call this."""
    return units.list_units(prefix)


def production_git(args: Sequence[str], cwd: str):
    """The git seam for the origin and default-branch lookups. Only the CLI builds it.

    `remote get-url` stays local; `ls-remote --symref` reaches the
    remote the same way the labels read does. Anything git cannot run
    reads as None so the caller degrades to a warn row, never a
    traceback. Tests inject a fake.
    """
    from korapy.gitenv import clean_env

    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            env=clean_env(),
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def production_tracker_factory(tracker_repo: str):
    """One labels tracker per tracker repo, through the one factory.

    Labels live on the repository whatever the tracker variant, so
    every repo reads through the labels construction; the runners stay
    the factory's production ones. Only the CLI calls this; tests pass
    a fake. `max_pages` is unused by the labels construction.
    """
    from korapy import trackers

    return trackers.build_tracker(variant="labels", board=None, max_pages=1)


def production_board_fetch(board_config: settings.BoardConfig) -> object:
    """The live board snapshot fetch. Only the CLI builds it; tests never call this.

    Routed through the one tracker factory, so the doctor reads the
    board through the same construction as sync, mirror and drive.
    """
    from korapy import trackers
    from korapy.github import gh

    return trackers.board_snapshot_for(board_config, run=gh.run)


def production_board_issue_refs(board_config: settings.BoardConfig) -> object:
    """The live board membership fetch. Only the CLI builds it; tests never call this.

    Membership only, through the same construction: no field values
    and no REST reads. The doctor diffs it against the store's open
    rows for the missing-from-board row.
    """
    from korapy import trackers
    from korapy.github import gh

    return trackers.board_issue_refs_for(board_config, run=gh.run)


def load_board_sites(settings_error: str | None) -> tuple[tuple, str | None]:
    """Board-mode sites for the live report, or ((), error) that names it.

    Read-only: plain file reads plus the resolved per-repo tables. When
    the settings themselves failed, there is nothing to assemble, and
    the settings row already carries the failure.
    """
    if settings_error is not None:
        return (), None
    try:
        root = settings.find_root(os.getcwd())
    except OSError:
        return (), None
    if root is None:
        return (), None
    sites, error = boarddoctor.sites_from_settings(root)
    return tuple(sites), error


def _default_branch(origin: str, path: str, git) -> tuple[str, str | None]:
    """The remote's advertised default branch state through the git seam.

    One `git ls-remote --symref` against the remote: a `ref:` line for
    `HEAD` names the default branch ("ok"); a clean read that
    advertises none means the remote has no default branch ("missing",
    so the row fails instead of reading as an ok note). A failed or
    unrunnable read maps to a cause, so the row warns naming it, never
    a traceback. The labels read already reaches the network, so this
    read adds no new kind of access; it asks the remote itself rather
    than trusting the clone's cached `origin/HEAD`.
    """
    try:
        proc = git(["ls-remote", "--symref", origin, "HEAD"], path)
    except Exception as err:  # noqa: BLE001 - a failed git read is report data
        return "unknown", f"cannot read the default branch on {origin}: {err}"
    if proc is None:
        return "unknown", f"cannot read the default branch on {origin}: cannot run git in {path}"
    if getattr(proc, "returncode", 1) != 0:
        detail = (getattr(proc, "stderr", "") or "").strip() or (
            f"git exited {getattr(proc, 'returncode', 1)}"
        )
        return "unknown", f"cannot read the default branch on {origin}: {detail}"
    for line in (getattr(proc, "stdout", "") or "").splitlines():
        head, _, target = line.partition("\t")
        if target == "HEAD" and head.startswith("ref: refs/heads/"):
            branch = head[len("ref: refs/heads/") :]
            if branch:
                return "ok", None
    return "missing", None


def _push_reading(origin: str, push_probe) -> str | None:
    """The site's push mode from the gathered probe readings, or None.

    `push_probe` is the `(ssh_ok, gh_ok)` pair gathered once per
    report, or None when the caller has no reading: then the report
    carries no push row. The one resolver (`pushenv.resolve`) maps the
    pair and the origin to the row's reading, so the report agrees with
    every path that pushes; the pair arrives as injected probes, so the
    seams never run twice.
    """
    if push_probe is None or not origin:
        return None
    ssh_ok, gh_ok = push_probe
    return pushenv.resolve(origin, ssh_probe=lambda: ssh_ok, gh_login=lambda: gh_ok).mode


def _repo_site(
    root: str, name: str, *, git, tracker_factory, push_probe=None
) -> repodoctor.RepoSite:
    """One repo's remote readings: the origin URL plus label presence.

    Read-only: one local `git remote get-url` per clone (no network),
    one label listing per tracker repo, and one `git ls-remote
    --symref` for the remote's default branch, all only when a remote
    is set. Every gather failure degrades to an "unknown" reading
    naming the repo and the cause, never a raise. `push_probe` is the
    `(ssh_ok, gh_ok)` pair gathered once per report (None for no push
    row); the same pair feeds every site, so the probe runs once, not
    per repo.
    """
    try:
        resolved = settings.load(root, repo=name)
    except (settings.SettingsError, paths.PathError, OSError) as err:
        return repodoctor.RepoSite(name, None, cause=str(err))
    clone = resolved["clone.path"]
    if not clone:
        return repodoctor.RepoSite(name, None)
    path = clone if os.path.isabs(clone) else os.path.join(root, clone)
    try:
        proc = git(["remote", "get-url", "origin"], path)
    except Exception as err:  # noqa: BLE001 - a failed git read is report data
        return repodoctor.RepoSite(
            name, None, cause=f"cannot read the origin remote of {path}: {err}"
        )
    if proc is None:
        return repodoctor.RepoSite(name, None, cause=f"cannot run git in {path}")
    if getattr(proc, "returncode", 1) != 0:
        stderr = (getattr(proc, "stderr", "") or "").strip()
        if "No such remote" in stderr:
            return repodoctor.RepoSite(name, None)
        cause = stderr or f"git exited {getattr(proc, 'returncode', 1)}"
        return repodoctor.RepoSite(
            name,
            None,
            cause=f"cannot read the origin remote of {path}: {cause}:"
            " the clone needs a re-init here, not a new remote on the forge",
        )
    origin = (getattr(proc, "stdout", "") or "").strip()
    if not origin:
        return repodoctor.RepoSite(name, None)
    push = _push_reading(origin, push_probe)
    tracker_repo = resolved["tracker.repo"] or detect.remote_slug(origin)
    if not tracker_repo:
        return repodoctor.RepoSite(
            name, origin, push=push, cause=f"no owner/name in the origin remote of {path}"
        )
    try:
        repo_tracker = tracker_factory(tracker_repo)
    except KeyError:
        return repodoctor.RepoSite(name, origin, push=push, cause=f"no tracker for {tracker_repo}")
    except Exception as err:  # noqa: BLE001 - a failed factory is report data
        return repodoctor.RepoSite(name, origin, push=push, cause=str(err))
    try:
        missing = repo_tracker.missing_labels(tracker_repo)
    except Exception as err:  # noqa: BLE001 - a failed read is report data
        return repodoctor.RepoSite(name, origin, push=push, cause=str(err))
    try:
        canonical = repo_tracker.canonical_repo(tracker_repo)
    except Exception:  # noqa: BLE001 - an unreadable name is no row, never a traceback
        canonical = None
    branch, branch_cause = _default_branch(origin, path, git)
    if branch_cause is not None:
        return repodoctor.RepoSite(
            name,
            origin,
            push=push,
            labels="missing" if missing else "ok",
            cause=branch_cause,
            tracker_repo=tracker_repo,
            canonical=canonical,
        )
    return repodoctor.RepoSite(
        name,
        origin,
        push=push,
        branch=branch,
        labels="missing" if missing else "ok",
        tracker_repo=tracker_repo,
        canonical=canonical,
    )


def load_repo_sites(
    settings_error: str | None, *, git, tracker_factory, push_probe=None
) -> tuple[repodoctor.RepoSite, ...]:
    """Remote-dependent sites for the live report. Never raises.

    Read-only like `load_board_sites`: plain file reads, one local git
    read per clone, one remote default-branch read, one label listing
    per tracker repo. Without settings there are no sites (the settings
    row already carries the failure); a repo whose own gathering fails
    still gets warn rows naming it and the cause. `push_probe` is the
    `(ssh_ok, gh_ok)` pair gathered once per report (None for no push
    row); the same pair feeds every site.
    """
    if settings_error is not None:
        return ()
    try:
        root = settings.find_root(os.getcwd())
    except OSError:
        return ()
    if root is None:
        return ()
    try:
        names = settings.repo_names(root)
    except (settings.SettingsError, OSError):
        return ()
    return tuple(
        _repo_site(root, name, git=git, tracker_factory=tracker_factory, push_probe=push_probe)
        for name in names
    )


def load_prompts_state() -> tuple[tuple[str, ...], tuple[str, ...], str | None]:
    """(invalid, stale, error) for the prompt overrides, gathered once.

    Every failure to look is data for a failing row, never a traceback:
    a broken settings file, a relative config path or an unreadable cwd.
    """
    try:
        found = prompts.check(settings.find_root(os.getcwd()))
    except (prompts.PromptsError, settings.SettingsError, paths.PathError, OSError) as err:
        return (), (), str(err)
    invalid = tuple(f"{role}: {found.invalid[role]}" for role in sorted(found.invalid))
    return invalid, tuple(found.stale), None


def load_disk_pressure(resolved) -> str:
    """The live disk pressure, best effort: `unknown` when unreadable."""
    from korapy import disk as _disk

    try:
        readings = _disk.read(
            [paths.state_dir(), paths.cache_dir(), paths.sessions_dir()],
            usage=_disk.production_usage,
        )
        if resolved is None:
            return _disk.pressure(
                readings,
                min_free_pct=settings.REGISTRY["disk.min_free_pct"].default,
                min_free_gb=settings.REGISTRY["disk.min_free_gb"].default,
            )
        return _disk.pressure(
            readings,
            min_free_pct=resolved["disk.min_free_pct"],
            min_free_gb=resolved["disk.min_free_gb"],
        )
    except Exception:  # noqa: BLE001 - a broken dir reads as unknown, never a traceback
        return _disk.UNKNOWN


def _read_text(path: str) -> str:
    """One file read for the resource sensors; tests inject a fake."""
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _read_pressure(reader) -> tuple[float | None, float | None]:
    """The measured host pressure through the injected file reader.

    Unreadable or unparsable files read as unknown (None), never raising:
    a half-blind host still decides on the half it can see, and unknown
    never blocks admission. `reader` is the same seam the sensor check
    uses, so tests inject one fake for both.
    """
    try:
        loadavg = reader(pressure.LOADAVG_PATH)
    except OSError:
        loadavg = None
    try:
        meminfo = reader(pressure.MEMINFO_PATH)
    except OSError:
        meminfo = None
    try:
        ncpu = os.cpu_count()
    except Exception:  # noqa: BLE001 - any counter failure reads as unknown
        ncpu = None
    reading = pressure.read(loadavg, meminfo, ncpu)
    return (reading.load_per_cpu, reading.mem_available_pct)


def _load_heavy_lane():
    """(lane, error, path, size, mtime) for the heavy rows, never raising.

    Best effort per aspect like every other reading here: a missing
    file reads as no lane (the row renders an empty queue), a busy lock
    reads as no lane (transient, gone by the next run), and only a
    present-but-corrupt file lands in error with the documented
    move-aside repair.
    """
    try:
        path = heavy.lane_path()
    except (paths.PathError, OSError):
        return None, None, "", None, None
    size = mtime = None
    try:
        probe = os.stat(path)
        size, mtime = probe.st_size, probe.st_mtime
    except OSError:
        pass
    try:
        return heavy.read_lane(), None, path, size, mtime
    except heavy.LaneLocked:
        return None, None, path, size, mtime
    except heavy.HeavyError as err:
        return None, str(err), path, size, mtime
    except (OSError, paths.PathError, ValueError) as err:
        return None, f"cannot read the lane file at {path}: {err}", path, size, mtime


def _load_heavy_estimates():
    """The per-key ledger estimates for the stuck-holder row, never raising."""
    try:
        rows, _sources, _skipped = heavy.collect_ledger_rows()
    except (heavy.HeavyError, OSError, paths.PathError):
        return {}
    try:
        return heavy.estimates_for(rows)
    except heavy.HeavyError:
        return {}


def _load_heavy_wait_factor(resolved):
    """The configured wait multiplier for the stuck-holder row, never raising."""
    try:
        raw = resolved.heavy if resolved is not None else None
    except AttributeError:
        return heavy.DEFAULT_WAIT_FACTOR
    try:
        cfg = heavy.check_table(raw, "<settings>")
    except (settings.SettingsError, heavy.HeavyError):
        return heavy.DEFAULT_WAIT_FACTOR
    if cfg is None:
        return heavy.DEFAULT_WAIT_FACTOR
    return cfg.wait_factor


def load_resource_readings(
    resolved: settings.Resolved | None,
    *,
    list_fn: Callable[[str], list],
    run: Callable[[Sequence[str]], object],
    read=None,
    now=None,
) -> resourcedoctor.ResourceReadings | None:
    """The host-budget readings for the `resources*` rows, or None without settings.

    Best effort per aspect: an unreadable ledger, marker, delegation file,
    sensor or slice listing is report data, never a traceback. Only the CLI
    calls this; tests inject `ResourceReadings` data instead.
    """
    if resolved is None:
        return None
    reader = read or _read_text
    moment = now if now is not None else time.time()
    try:
        registered = projects.load_all()
    except (projects.ProjectsError, paths.PathError, settings.SettingsError, OSError):
        registered = []
    ledger_path = host_ledger.ledger_path()
    try:
        ledger = host_ledger.load(ledger_path)
    except host_ledger.LedgerError as err:
        ledger, ledger_error = None, str(err)
    else:
        ledger_error = None
    try:
        foreign_ignored = tuple(host_ledger.ignored_drop_ins(host_ledger.claims_dir(ledger_path)))
    except (host_ledger.LedgerError, OSError):
        foreign_ignored = ()
    try:
        state = paths.state_dir()
    except paths.PathError as err:
        state, marker, marker_error = "", None, str(err)
    else:
        marker_path = host_ledger.marker_path(ledger_path)
        try:
            marker, marker_error = reader(marker_path).strip(), None
        except FileNotFoundError:
            marker, marker_error = None, None
        except (OSError, ValueError) as err:
            marker, marker_error = None, f"cannot read the ledger marker at {marker_path}: {err}"
    try:
        available = units.read_controllers(read=reader)
    except units.UnitsError as err:
        available, controllers_error = None, str(err)
    else:
        controllers_error = None
    sensor_errors = resourcedoctor.load_sensor_errors(read=reader)
    pressure_values = _read_pressure(reader)
    names = [units.HOST_SLICE]
    for entry in registered:
        try:
            names.append(units.project_slice(entry.name))
        except units.UnitsError:
            continue
    slices = resourcedoctor.load_slice_actual(names, run=run)
    prefixes = {entry.unit_prefix for entry in registered}
    try:
        prefixes.add(str(resolved["agents.unit_prefix"]))
    except KeyError:
        pass
    try:
        registry_cfg = registry.config(resolved)
    except (settings.SettingsError, paths.PathError):
        registry_cfg = None
    entries: dict = {}
    if registry_cfg is not None:
        try:
            listed = registry.visible(moment, registry_cfg)
        except (paths.PathError, settings.SettingsError, registry.RegistryError):
            listed = []
        for entry, _row_state in listed:
            entries.setdefault(entry.get("id"), entry)
    live_roles = resourcedoctor.load_live_roles(prefixes, list_fn=list_fn, entries=entries)
    tables = resolved.resources
    priced = frozenset(tables.roles) if tables is not None else frozenset()
    try:
        where = os.path.join(paths.config_dir(), settings.HOST_CONFIG_NAME)
    except paths.PathError:
        where = "the host config"
    try:
        base = resources.config(resolved)
        try:
            total_cap = resolved["admission.total_cap"]
        except KeyError:
            total_cap = admission.DEFAULT_TOTAL_CAP
        shares = projects.budget_shares(registered, total_cap=total_cap)
        foreign = (ledger or {}).get("foreign", [])
        if not isinstance(foreign, list):
            foreign = []
        limits = resources.join(base, shares, foreign, where=where)
    except (settings.SettingsError, resources.LimitsError) as err:
        limits, limits_error = None, str(err)
    else:
        limits_error = None
    if limits is not None and limits.pressure is not None:
        pressure_caps = (
            limits.pressure.max_load_per_cpu,
            limits.pressure.min_mem_available_pct,
        )
    else:
        pressure_caps = (
            pressure.DEFAULT_MAX_LOAD_PER_CPU,
            pressure.DEFAULT_MIN_MEM_AVAILABLE_PCT,
        )
    heavy_lane, heavy_error, heavy_path, heavy_bytes, heavy_mtime = _load_heavy_lane()
    return resourcedoctor.ResourceReadings(
        host=limits.host if limits is not None else None,
        shares=dict(limits.projects) if limits is not None else {},
        foreign=list(limits.foreign) if limits is not None else [],
        gone_s=limits.gone_s if limits is not None else resources.DEFAULT_GONE_S,
        now=moment,
        limits_error=limits_error,
        priced_roles=priced,
        live_roles=live_roles,
        controllers=available,
        controllers_error=controllers_error,
        ledger=ledger,
        ledger_error=ledger_error,
        ledger_path=ledger_path,
        marker=marker,
        marker_error=marker_error,
        state_dir=state,
        marker_path=host_ledger.marker_path(ledger_path),
        sensor_errors=sensor_errors,
        slices=slices,
        config_path=where,
        pressure=pressure_values,
        pressure_caps=pressure_caps,
        foreign_ignored=foreign_ignored,
        heavy_lane=heavy_lane,
        heavy_error=heavy_error,
        heavy_path=heavy_path,
        heavy_bytes=heavy_bytes,
        heavy_mtime=heavy_mtime,
        heavy_now=moment,
        heavy_estimates=_load_heavy_estimates(),
        heavy_wait_factor=_load_heavy_wait_factor(resolved),
    )


def load_project_report() -> tuple[list[str], list[tuple[str, str, str | None]] | None]:
    """Registry problems plus every resolving policy entry, in one pass.

    Returns (problems, sources): `sources` holds one (project, entry,
    source file) per resolving entry, or None when the registry cannot
    even be listed. Each policy module imports once here, so a policy's
    top-level code runs once per report, not once per answer. A
    gathering failure is one problem and no sources, never a
    traceback. The one loader for the live report; the two readers
    below are thin views over it.
    """
    try:
        problems, sources = projects.check_and_sources()
    except (projects.ProjectsError, paths.PathError, settings.SettingsError, OSError) as err:
        return [str(err)], None
    return problems, list(sources)


def load_project_problems() -> list[str]:
    """The registry problems for the live report: bad files and policy
    entries that do not resolve. A gathering failure is one problem
    naming it, never a traceback. One view over `load_project_report`,
    so reading problems never re-imports a policy module."""
    problems, _ = load_project_report()
    return problems


def load_project_policies() -> list[tuple[str, str, str | None]] | None:
    """Every resolving eligibility entry for the live report, as
    (project, entry, source file). Empty when nothing resolves: then
    the ok row stays generic and any failure already reads through
    `load_project_problems`. Entries that do not resolve are skipped
    here and reported there. One view over `load_project_report`, so
    reading sources never re-imports a policy module."""
    _, sources = load_project_report()
    return sources


def load_profile_state(resolved, settings_error: str | None) -> ProfileState:
    """The profile readings for the live report, gathered once.

    Validates the declared profiles against the account pool and reads
    the local selections file; every fallible gathering degrades to
    report data, never a traceback. Only the CLI calls this; tests
    inject ProfileState data instead.
    """
    now = time.time()
    if resolved is None:
        return ProfileState(
            parsed={},
            error=settings_error or "settings did not resolve",
            sources={},
            shadowed={},
            selections=None,
            selections_error=None,
            committed={},
            now=now,
        )
    try:
        pool = accounts.providers()
    except (accounts.AccountsError, paths.PathError) as err:
        return ProfileState(
            parsed={},
            error=str(err),
            sources={},
            shadowed={},
            selections=None,
            selections_error=None,
            committed={},
            now=now,
        )
    try:
        parsed = profiles.validate(resolved.profiles, pool)
    except settings.SettingsError as err:
        return ProfileState(
            parsed={},
            error=str(err),
            sources=dict(resolved.profile_sources),
            shadowed=dict(resolved.profile_shadowed),
            selections=None,
            selections_error=None,
            committed=dict(resolved.profile_committed),
            now=now,
        )
    try:
        selections: profiles.Selections | None = profiles.load_selections()
        selections_error = None
    except (profiles.ProfileError, paths.PathError) as err:
        selections, selections_error = None, str(err)
    return ProfileState(
        parsed=parsed,
        error=None,
        sources=dict(resolved.profile_sources),
        shadowed=dict(resolved.profile_shadowed),
        selections=selections,
        selections_error=selections_error,
        committed=dict(resolved.profile_committed),
        now=now,
    )


def load_timer_state() -> str:
    """The live drive timer state for the report: the vocabulary the
    timer row reads, or `unreachable: ...` when the manager cannot
    be asked. Only the CLI calls this; tests never call this."""
    try:
        return drive.timer_state()
    except units.Unreachable as err:
        return f"unreachable: {err}"


def read_hub_token() -> str | None:
    """This host's hub token, read at call time. None when it cannot be read.

    The value is never logged or stored: callers pass it to the probe
    and drop it. Only the CLI calls this; tests never call this.
    """
    try:
        path = paths.hub_token_path()
    except paths.PathError:
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            token = handle.read().strip()
    except OSError:
        return None
    return token or None


def load_hub_auth(url: str | None) -> HubAuth | None:
    """The hub-token reading for the live report, or None without a hub.

    Only the CLI calls this; tests inject HubAuth data. The detail
    names the file and the problem, never the token value.
    """
    if not url:
        return None
    try:
        path = paths.hub_token_path()
    except paths.PathError as err:
        return HubAuth("bad", str(err))
    try:
        mode = os.stat(path).st_mode
    except FileNotFoundError:
        return HubAuth(
            "missing",
            f"no hub token at {path}: install the token the operator minted for this host",
        )
    except OSError as err:
        return HubAuth("bad", f"hub token at {path} cannot be read: {err}")
    if mode & 0o077:
        return HubAuth(
            "bad",
            f"hub token at {path} is readable beyond its owner: make it owner-only",
        )
    try:
        with open(path, encoding="utf-8") as handle:
            token = handle.read().strip()
    except OSError as err:
        return HubAuth("bad", f"hub token at {path} cannot be read: {err}")
    if not token:
        return HubAuth(
            "bad",
            f"hub token at {path} is empty: install the token the operator minted",
        )
    return HubAuth("ok", "this host's hub token is present and owner-only")


def _close_quietly(err) -> None:
    """Close an error response, swallowing a failure to close it.

    The probe reports a failed hub as data; a close that raises must not
    turn that report into a traceback.
    """
    try:
        err.close()
    except Exception:  # noqa: BLE001 - closing report data is best effort
        pass


def _typed_version_refusal(err: urllib.error.HTTPError) -> HubProbe | None:
    """A typed version refusal carried on an HTTP error, or None.

    An older hub refuses with a typed body carrying its versions; that
    refusal runs through `hub.check_version` so a real mismatch fails
    instead of warning as unreachable. An absent or unreadable body is
    None, and the caller keeps the unreachable warning.
    """
    try:
        raw = err.read()
    except Exception:  # noqa: BLE001 - an unreadable error body is report data
        return None
    try:
        body = json.loads(raw.decode("utf-8"))
        api, schema = body["api"], body["schema_version"]
        if (
            isinstance(api, bool)
            or not isinstance(api, int)
            or isinstance(schema, bool)
            or not isinstance(schema, int)
        ):
            return None
    except (ValueError, UnicodeDecodeError, KeyError, TypeError, AttributeError):
        return None
    try:
        hub.check_version(api, schema)
    except hub.HubVersionError as version_err:
        return HubProbe("version-mismatch", str(version_err), hub_api=api, hub_schema=schema)
    return None


def load_hub_probe(url: str | None, token: str | None) -> HubProbe | None:
    """Probe the hub's version route once, or None without a hub.

    One GET of `/v1/version` with the host token: unreachable, refused
    (401), and version-mismatched hubs each map to their row. A failed
    probe is report data, never a traceback. Only the CLI calls this;
    tests inject HubProbe data instead.
    """
    if not url:
        return None
    target = url.rstrip("/") + "/v1/version"
    request = urllib.request.Request(target, headers={"Accept": "application/json"})
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            status = response.status
            raw = response.read(65536)
    except urllib.error.HTTPError as err:
        # HTTPError subclasses tempfile._TemporaryFileWrapper: an unclosed
        # one warns at collection and leaks the response's file. The typed
        # refusal reads the body first, then the handle closes either way;
        # a close that fails is report data too, never a traceback.
        try:
            if err.code == 401:
                return HubProbe(
                    "unauthorized", f"the hub at {url} refused this host's token (HTTP 401)"
                )
            refusal = _typed_version_refusal(err)
            if refusal is not None:
                return refusal
            return HubProbe("unreachable", f"the hub at {url} answered HTTP {err.code}")
        finally:
            _close_quietly(err)
    except Exception as err:  # noqa: BLE001 - a failed probe is report data, never a traceback
        return HubProbe("unreachable", f"the hub at {url} cannot be reached: {err}")
    if status != 200:
        return HubProbe("unreachable", f"the hub at {url} answered HTTP {status}")
    try:
        body = json.loads(raw.decode("utf-8"))
        api, schema = body["api"], body["schema_version"]
        if (
            isinstance(api, bool)
            or not isinstance(api, int)
            or isinstance(schema, bool)
            or not isinstance(schema, int)
        ):
            raise ValueError("hub version reply holds non-integer versions")
    except (ValueError, UnicodeDecodeError, KeyError, TypeError, AttributeError) as err:
        return HubProbe(
            "unreachable", f"the hub at {url} answered an unreadable version reply: {err}"
        )
    try:
        hub.check_version(api, schema)
    except hub.HubVersionError as err:
        return HubProbe("version-mismatch", str(err), hub_api=api, hub_schema=schema)
    return HubProbe("ok", f"the hub answers at {url}", hub_api=api, hub_schema=schema)


def _production_hub_url(resolved: settings.Resolved | None) -> str:
    """The resolved `hub.url`, or "" when unset or unreadable (single-host)."""
    if resolved is None:
        return ""
    try:
        return str(resolved["hub.url"])
    except KeyError:
        return ""


def production_env() -> Env:
    """The live environment. Only the CLI calls this; tests never call this.

    Every fallible gathering degrades to report data: a broken host config
    or an unusable state dir is a failing row, never a traceback.
    """
    resolved, settings_error = _production_settings()
    if resolved is not None:
        prefix = str(resolved["agents.unit_prefix"])
        try:
            registry_cfg = registry.config(resolved)
        except settings.SettingsError as err:
            settings_error = str(err)
            registry_cfg = registry.default_config()
        try:
            _, max_time_s = launch.max_time_for(resolved)
        except launch.LaunchError:
            max_time_s = None
        try:
            _, reviewer_max_time_s = launch.max_time_for(resolved, reviewer=True)
        except launch.LaunchError:
            reviewer_max_time_s = None
        if reviewer_max_time_s == max_time_s:
            reviewer_max_time_s = None
    else:
        prefix = str(settings.REGISTRY["agents.unit_prefix"].default)
        registry_cfg = registry.default_config()
        max_time_s = None
        reviewer_max_time_s = None
    try:
        account_problems = accounts.check()
    except (paths.PathError, accounts.AccountsError) as err:
        account_problems = [str(err)]
    try:
        user = getpass.getuser()
    except (OSError, KeyError):
        user = "unknown"
    try:
        registry_rows = registry.visible(time.time(), cfg=registry_cfg)
        registry_error = None
    except (paths.PathError, settings.SettingsError, registry.RegistryError) as err:
        registry_rows = ()
        registry_error = str(err)
    dep_results, deps_error = load_dep_results()
    prompts_invalid, prompts_stale, prompts_error = load_prompts_state()
    disk_pressure = load_disk_pressure(resolved)
    resource_readings = load_resource_readings(
        resolved, list_fn=production_list_fn, run=production_run
    )
    board_sites, board_error = load_board_sites(settings_error)
    # The push readings, gathered once per report: one SSH probe and one
    # `gh` login check feed every site, so neither runs per repo. Both
    # degrade to False on failure (never a traceback); a wrong-False
    # only pessimizes the row, never blocks the report.
    push_probe = (pushenv.default_ssh_probe(), pushenv.default_gh_login())
    repo_sites = load_repo_sites(
        settings_error,
        git=production_git,
        tracker_factory=production_tracker_factory,
        push_probe=push_probe,
    )
    project_problems, project_policies = load_project_report()
    timer_state = load_timer_state()
    try:
        freeze_max_age_s = (
            resolved["freeze.max_age_s"] if resolved is not None else freeze.DEFAULT_MAX_AGE_S
        )
    except KeyError:
        freeze_max_age_s = freeze.DEFAULT_MAX_AGE_S
    hub_url = _production_hub_url(resolved)
    hub_token = read_hub_token() if hub_url else None
    hub_auth = load_hub_auth(hub_url)
    hub_probe = load_hub_probe(hub_url, hub_token)
    profile_state = load_profile_state(resolved, settings_error)
    return Env(
        dep_results=dep_results,
        deps_error=deps_error,
        account_problems=account_problems,
        settings_error=settings_error,
        state_error=check_state_dirs(),
        user=user,
        unit_prefix=prefix,
        run=production_run,
        list_fn=production_list_fn,
        reader_errors=load_readers(),
        registry_rows=registry_rows,
        registry_error=registry_error,
        max_time_s=max_time_s,
        reviewer_max_time_s=reviewer_max_time_s,
        prompts_invalid=prompts_invalid,
        prompts_stale=prompts_stale,
        prompts_error=prompts_error,
        disk=disk_pressure,
        resource_readings=resource_readings,
        board_sites=board_sites,
        board_fetch=production_board_fetch,
        board_open_store=store.open_store,
        board_items=production_board_issue_refs,
        board_error=board_error,
        repo_sites=repo_sites,
        project_problems=project_problems,
        project_policies=project_policies,
        timer_state=timer_state,
        freeze_view=load_freeze_view(resolved),
        freeze_max_age_s=freeze_max_age_s,
        hub_url=hub_url or None,
        hub_auth=hub_auth,
        hub_probe=hub_probe,
        profile_state=profile_state,
        quota_view=load_quota_view(resolved),
    )


# ------------------------------------------------------------------- CLI


def cmd_doctor(args: argparse.Namespace, make_env: Callable[[], Env]) -> int:
    checks = run_checks(make_env())
    sys.stdout.write(render(checks, json_mode=args.json))
    return exit_code(checks)


def add_parser(sub, make_env: Callable[[], Env]) -> None:
    """Register `korapy doctor` on the top-level subparsers."""
    parser = sub.add_parser(
        "doctor", help="read-only health report for the core's own prerequisites"
    )
    parser.add_argument("--json", action="store_true", help="one JSON document on stdout")
    parser.set_defaults(func=functools.partial(cmd_doctor, make_env=make_env))


def run_verb(argv: Sequence[str], make_env: Callable[[], Env]) -> int:
    """Parse and run `doctor` with the given environment. Returns the exit code."""
    parser = argparse.ArgumentParser(prog="korapy")
    sub = parser.add_subparsers(dest="command", required=True)
    add_parser(sub, make_env)
    try:
        args = parser.parse_args(list(argv))
    except SystemExit as err:
        return OK if err.code == 0 else USAGE
    return args.func(args)
