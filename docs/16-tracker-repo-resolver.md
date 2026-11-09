# 16: one tracker-repo resolver for `[[repo]]` entries

Status: design, awaiting maintainer approval. No code ships with
this doc. It names the one helper that answers "which tracker repo
does this `[[repo]]` entry feed", collapses the two out-of-tree
resolvers onto it, and leaves the drive-side map exactly as the
drive ticket left it. Implementation waits for approval and
follows the tickets in section 7.

Conventions: module, function and file names quoted below were
read from the tree at design time (`syncli.py`,
`boarddoctor.py`, `doctor.py`, `drive.py`, `detect.py`,
`github/forge.py`, `trackers.py`, `reviewrecord.py`,
`launchcli.py`, `mergecli.py`, `reviewcli.py`, `gccli.py`).
Behavior not yet implemented is marked PROPOSED; everything else
describes the tree as it stands.

## 0. Scope and non-goals

The design adds: one public helper for the entry-to-tracker-repo
fact, delegation of the two divergent readers onto it, and an
agreement test with a resolver guard. Non-goals: changing the
drive-side short-to-tracker map (settled by the drive ticket:
`repo_operators`, `root_short_map`, `production_repo_map`,
`driven_repos`); changing short-name collision semantics across
roots (section 4, a separate ticket, #456); any new CLI verb or flag;
any new exit code.

## 1. The problem

Three code paths answer "which tracker repo does this `[[repo]]`
entry feed", and they agree only when `tracker.repo` is set.
When it is unset they diverge:

| Caller | Unset `tracker.repo` reads as |
| --- | --- |
| `syncli._tracker_repo` (drive, sync, mirror, launch, operators) | origin via `ghforge.origin_repo`, else a problem |
| `boarddoctor.sites_from_settings` | the entry short name itself |
| `doctor._repo_site` | `detect.remote_slug` of its own `get-url` read |

So a board site can report under a short name the drive pass
would key as `owner/name`, and `doctor` can disagree with both
on a clone whose origin differs from `clone.path`. AGENTS.md
"one fact, one place" calls the second resolver a bug even when
the two agree today. No test asserts the three agree, and none
of the three is covered against an entry that omits
`tracker.repo` in a second caller's fixture.

## 2. The one helper

PROPOSED: promote `syncli._tracker_repo` to the public
`syncli.tracker_repo(root, name, resolved, git)` with its
current contract unchanged: `(owner/name, None)` for one entry,
or `(None, problem)`. Explicit `tracker.repo` is validated with
`ghforge.is_repo_name`; otherwise `clone.path` (absent: a
problem naming the entry) resolves against the root and the
origin reads through `ghforge.origin_repo` (unparsable: a
problem naming the path). No guessing, no fallback spelling.
A second entry point takes the already-read origin URL (an
`origin=` parameter) for callers that already ran `get-url`;
both spellings share the parse-and-validate core (section 3).

It lives in `syncli`, not `settings`: `syncli` already owns the
`git(args, cwd)` seam shape and the forge-name validation, and
`drive`, `launchcli` and `trackers` already import `syncli` for
it. Teaching `settings` a subprocess seam would break its
layering (file reads only). `detect` keeps `remote_slug` as the
URL parser; it stops being a second answer to this fact.

An unresolvable origin fails closed at entry-to-tracker
resolution. Strict callers (the drive pass via `root_short_map`
raising `SettingsError`, sync, mirror, launch,
`trackers.production_trackers`) refuse with exit 4,
configuration or usage error, as today. Report callers render
the same problem instead of raising: `doctor` emits its warn
row (section 3), `boarddoctor` returns its existing
`([], error)` channel (section 3). No new exit code, no new
row kind. Outside that resolution `short_to_tracker` stays
fail-soft (it catches `SettingsError` and returns `{}`) and
`reviewrecord.local_repo` keeps passing unknown plain names
through; neither answers which tracker repo an entry feeds.

## 3. Every current resolver and its delegation

- `syncli._tracker_repo`: renamed to `syncli.tracker_repo`,
  logic unchanged. `repo_operators`, `root_short_map` and the
  fail-soft `short_to_tracker` (which catches `SettingsError`
  to keep on-disk short spellings resolving) keep calling it.
  Deliberate and unchanged.
- `drive.production_repo_map`, `driven_repos`: already built
  on `root_short_map`. No change.
- `drive._tracker_key` (`mapping.get(repo, repo)`): kept. It
  keys registry short spellings against the pass map; an
  unknown short keeps itself by design (removed projects),
  which is join behavior, not a second answer for an entry.
- `launchcli` (direct `_tracker_repo` call) and
  `trackers.setup_entry` / `production_trackers` (via
  `repo_operators`): already on the helper. Rename only.
- PROPOSED `boarddoctor.sites_from_settings`: replaces
  `loaded["tracker.repo"] or name` with the helper, taking a
  required keyword-only `git` seam (no default; tests pass a
  fake). The production value arrives through the
  `load_board_sites` threading below (`production_git`), so
  one runner reaches the helper, not two. `syncli._git` and
  `doctor.production_git` share that shape today; the
  implementation either collapses them or justifies keeping
  both. A problem returns `([], error)` naming the entry,
  the same channel as its existing settings failures. No
  `BoardSite` shape change (its `repo` comment admitting "or
  the entry name" goes away with the fallback).
- PROPOSED `doctor.load_board_sites` (the only production
  caller of `sites_from_settings`): takes the same required
  keyword-only `git` seam (no default) and threads it into
  `sites_from_settings`. The report-path call site
  `production_env` (`load_board_sites(settings_error)`) passes
  `git=production_git`, the same runner the `load_repo_sites`
  call below receives, so there is no second default.
  Today `load_board_sites` calls `sites_from_settings` with
  no seam.
- PROPOSED `doctor._repo_site`: replaces
  `resolved["tracker.repo"] or detect.remote_slug(origin)`
  with the helper over its `git` seam. `_repo_site` already
  reads the origin URL once for its own row, so the helper
  takes that already-read origin (an `origin=` parameter)
  instead of running `get-url` a second time: one spawn per
  repo and no window where two reads disagree. A problem becomes
  `RepoSite(name, origin_or_None, cause=problem)`, a warn row
  under the existing never-raise contract. Explicit values
  gain the `is_repo_name` check, so a malformed explicit
  `tracker.repo` now warns here instead of reaching the
  tracker adapter. The three earlier silent branches route
  through the helper too, each becoming a `cause=` warn row:
  no `clone.path`, the `No such remote` failure, and the
  empty-origin read (today each returns a silent
  `RepoSite(name, None)` with no cause and never reaches any
  resolver). With the helper the no-`clone.path` entry warns
  naming the entry, and the two unreadable-origin entries
  warn naming the path and the reason.
- Reviewed, no change: `mergecli`, `reviewcli` and `gccli`
  call `ghforge.origin_repo` for the forge repo of a working
  clone (`mergecli` and `reviewcli` pass `os.getcwd()`;
  `gccli` passes its repo dir): where to open a PR, which
  repo this clone is. That is a different fact from what an
  entry feeds.
  `reviewrecord.local_repo` translates spellings downstream
  of the map. `takeoff` consumes the map.

## 4. Two entries sharing one short name

Across registered roots `production_repo_map` keeps the first
project naming a short name (`setdefault`), while
`driven_repos` keeps each root's own set, so two projects
sharing a short name each still drive their own tracker repos.
That asymmetry is deliberate and a separate ticket (#456) owns it.
This design is out of that scope: it must not change collision
semantics, and the agreement test uses distinct short names
(or pins first-winner explicitly where multi-root). After the
collapse the three callers resolve each entry identically, so
a collision surfaces the same spelling in all three reports
instead of three different guesses.

## 5. Test plan

PROPOSED `tests/test_tracker_repo_agreement.py`, fakes at the
git seam, no network, no real clones:

1. One `[[repo]]` entry omitting `tracker.repo`, fake git
   answering a fixed origin URL: `syncli.tracker_repo`,
   `boarddoctor.sites_from_settings` and
   `doctor._repo_site` all report the same `owner/name`.
2. Unresolvable variants (no `clone.path`, git failure,
   non-GitHub origin URL, malformed explicit value): the
   helper returns a problem; doctor yields a warn row with
   that cause; boarddoctor returns `([], error)`; the drive
   map raises `SettingsError`.
3. Explicit `tracker.repo` set: all callers agree and the
   helper makes no git call (the fake asserts none;
   `_repo_site` still reads the origin for its own row).
4. A resolver guard in the style of
   `test_drive_resolver_guard.py`: parsing `src/korapy`
   fails on any `["tracker.repo"] or` fallback outside the
   helper, so a fourth resolver cannot slip back in.

## 6. Migration risk

Low. Entries with a well-formed `tracker.repo` set resolve
byte-identical; only entries omitting it with an unresolvable
or divergent origin change behavior, plus one explicit-value
case: a malformed explicit `tracker.repo` now warns in doctor
instead of reaching the tracker adapter. Boarddoctor stops
reporting a guessed short-name site (now an error naming the
entry) and doctor stops reporting a guessed origin-slug site
(now a warn row with the cause). Both new readings name the
entry and the reason, and both travel existing error channels,
so no production caller needs a new branch. The required
`git` seam does touch test call sites: the five
`sites_from_settings(root, env=...)` calls in
`tests/test_board_doctor.py` pass a fake `git`. No exit code
changes: 4 is already the configuration-error code on the
strict paths, and doctor rows already warn.

## 7. Implementation tickets

One ticket moves the helper (rename, public, same contract,
plus the `origin=` single-read entry point), one per doctor
caller: `sites_from_settings` plus its sole caller
`load_board_sites` threading the required `git` seam, plus
the report-path call site `production_env` passing
`git=production_git` (the same runner the `load_repo_sites`
call below receives, so there is no second default), and
`_repo_site` (helper delegation plus the three silent
branches), plus the agreement test and guard above, plus
updating the five `sites_from_settings(root, env=...)` test
call sites in `tests/test_board_doctor.py` to pass a fake
`git`. Docs freshness: no user-visible surface changes, so
`commands.md`, `configuration.md`, the guide and the README
stay as they are; this doc is the record.

Options:
1. syncli helper, fail closed to exit 4 (recommended)
2. Helper lives in settings, git seam injected
3. Doctors keep fallbacks, agreement test pins them
4. boarddoctor skips bad entries instead of error
5. Do nothing: document the three spellings as-is
