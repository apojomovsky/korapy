# 21: integration and end-to-end scenario tests

Status: design, awaiting maintainer approval. No code ships with
this doc. It decides a fast scenario tier that chains the real
pieces (CLI verb, settings, store, tracker, drive pass, launcher,
dashboard API) through whole user journeys with only the external
seams faked, so CI catches cross-module failures the unit suite
cannot see. Implementation waits for approval and follows the
tickets in section 6.

Conventions: module, function and file names quoted below were
read from the tree at design time (`cli.py`, `drive.py` with
`run_pass`, `board.py` with `open_store` and `Store`,
`launchcli.py` with `production_seams` and `Seams`, `serve.py`,
`tracker.py`, `forge.py`, `github/gh.py`, `paths.py`,
`tests/test_tracker.py`, `tests/suiteguard.py`). Behaviour not
yet implemented is marked PROPOSED; everything else describes
the tree as it stands. No names, hosts or accounts appear: every
example uses `example.com`, `suite@korapy.invalid`, `host-1`,
`acme/app` as in the existing tests, or `a project`.

## 0. Scope and non-goals

The problem: korapy has 144 `tests/test_*.py` modules and about 6,925
`def test` methods (counted with `grep -c` at design time),
stdlib `unittest` only, run by `make check` (lint, type check,
then the suite on 3.11 and 3.14 in parallel via `test-matrix`).
CI (`.github/workflows/ci.yml`) runs exactly `make check` on
`ubuntu-latest`. A full run takes on the order of 15 to 20
minutes on a loaded host (maintainer measurement; see section 1
for the per-group breakdown measured for this design). Every
external seam (`gh`, `git`, `systemd-run`, `systemctl`, `docker`,
the agent runtimes, the network) already has an injectable fake,
and `tests/suiteguard.py` fails any test that reaches the outside
world. There is no dedicated integration or end-to-end tier:
nothing chains a CLI verb through settings, the board store, a
drive pass, the launcher and the dashboard API in one journey.

This design adds that tier as PROPOSED `tests/test_scenarios*.py`
modules plus one shared harness, a `make scenarios` target and a
separate CI job. Non-goals: real GitHub, real systemd, real
agents (the maintainer runs a manual smoke checklist for those;
section 5.3 gives it as an appendix), load and soak tests, a
second test framework, and any change to exit-code meanings,
the snapshot schema, or the tracker and forge interfaces.

## 1. Survey: what the suite already covers, and what it does not cross

### 1.1 What crosses a boundary today

The suite is not pure unit tests. Several modules already call
production code in-process across a boundary, with fakes only at
the outer seam:

| Area (modules) | Boundary crossed | Seam faked | What it proves |
| --- | --- | --- | --- |
| CLI in-process (`test_takeoff.py` 107 tests, `test_heavycli.py`, `test_freezecli.py`, `test_quotacli.py`, `test_syncli.py`, `test_profilecli.py`, `test_reviewcli.py`, `test_githubcli.py`) | argv parsing in `cli.py` and the `*cli.py` modules, settings resolution, exit-code mapping | `gh` runner, `git` runner, unit listing, clock | one verb end to end, in isolation |
| Drive (`test_drive*.py`, 17 modules, the largest `test_drive_gates.py` 168 and `test_drive_stuck.py` 150 tests) | `drive.run_pass` over a real `board.Store` on a temp file, eligibility, claims, mirror outbox | `FakeTracker` from `test_tracker.py`, fake launcher, fake live-unit listing, injected `now` | one pass: sync, claim, launch decision, mirror drain |
| Board (`test_board*.py`, 18 modules, `test_board_status.py` 1,070 lines) | store transactions, feed and board steps, sweep, settlement | `FakeTracker`, `FakeBoardTracker`, scripted board reader | store stays in step with a scripted tracker |
| Launch (`test_launch.py` 170 tests, 3,560 lines) | `launchcli.Seams`, admission, quota gate, worktree creation on a local git repo, registry | unit `run` and `list_fn`, `host_ledger_fn`, `labels_fn`, disk `usage`, `pressure_fn` | launch, resume, stop, tail with no systemd |
| Dashboard (`test_serve.py` 192 tests, 3,011 lines; `test_dashboard.py`, `test_snapshot.py`, `test_webui.py` and the `test_web_*.py` polish modules) | `serve.py` `ThreadingHTTPServer` on loopback read back with `http.client`, `snapshot.build` document | snapshot builders, tail and journal readers | the API contract and the page, over real loopback HTTP |
| Forge and tracker (`test_forge.py` 138, `test_tracker.py` 92, `test_gh.py` 112 tests) | `github/tracker.py` and `github/forge.py` payload parsing, `GraphqlPacer` | `FakeRun` (path to JSON text or exception, calls recorded) | the adapters match scripted `gh` shapes |
| Heavy lane (`test_heavy*.py`, 6 modules) | queue, estimator, rollup, scope seam | scope runner, clock, PSI reader | admission and learned costs; golden JSON in `tests/golden/heavy-*.json` |
| Init and new (`test_init.py` 110, `test_new.py` 101 tests) | scaffold writers, registration, hooks on a real local git repo | `gh` runner for the remote step | a project onboards; golden plan text |
| Hub (`test_hub.py` 143 tests) | hub claim service over loopback HTTP, epochs, host add and remove | clock, token store on temp dirs | multi-host claims without a network |

The reusable fakes already exist and a scenario harness should
compose them, not replace them: `FakeTracker` and `FakeRun` in
`tests/test_tracker.py` (scripted items, per-call error maps,
`calls` recorder), `FakeBoardTracker` in
`tests/test_board_wiring.py` and `tests/test_board_sweep.py`,
`FakeLauncher` in `tests/test_area_locks.py`, and the
`launchcli.Seams` NamedTuple whose production value only
`production_seams()` builds.

### 1.2 What no test crosses

No test today does any of these in one journey:

1. **CLI verb to store to second verb.** `korapy sync` (or a
   drive pass) writes the store, then a different verb
   (`snapshot`, `serve`, `drive --plan`, `stop`) reads the state
   the first one left. Each verb is tested with a store the test
   itself seeded, so a writer and reader that disagree on a
   field pass separately and fail together only in production.
2. **Drive pass to real launch path to registry to dashboard.**
   Drive tests stop at a fake launcher; launch tests start from
   a hand-built request. The chain pass, claim, `launch` argv
   and overlay, registry entry, `snapshot` row, `serve` JSON is
   never asserted as one flow.
3. **Settings file to behaviour.** Tests pass settings as Python
   values. A journey that writes `korapy.toml` and
   `config.toml` to a temp config dir, resolves them through
   `settings` and `paths` (the `KORAPY_STATE`, `KORAPY_CONFIG`,
   `KORAPY_CACHE`, `KORAPY_SESSIONS` overrides), and observes
   the resolved value change a launch or a pass is missing.
4. **Freeze, hold and pause across verbs.** `freeze start`,
   `drive`, `resume` and `doctor` each read the pause and freeze
   files; no test holds a freeze with one verb and watches the
   next pass and a resume refuse because of it.
5. **Tracker outage mid-journey.** Fakes fail one call. No
   journey syncs, loses the tracker, claims from the store,
   fails to mirror, recovers, and drains the outbox.
6. **The exit code as the contract.** Exit codes are a public
   interface (0, 1, 2, 3, 4, 75; `docs/commands.md`), asserted
   per verb, never asserted as the branching a script would do
   across two verbs (for example retry on 2, stop on 4).
7. **Time passing.** Cooldowns, staleness bounds, claim expiry
   and the day cap are tested with injected `now` values inside
   one module. No journey advances a fake clock across verbs
   and watches a stale reading, a lapsed claim and a reset day
   counter agree.

### 1.3 Wall time: what dominates the 15 to 20 minutes

Measured for this design on the design host with
`python -m unittest <module>` per module (single Python, warm
uv cache; absolute numbers vary by host, the ranking is the
finding). The full-suite figure of 15 to 20 minutes on a loaded
host is the maintainer's; `make check` runs the two interpreter
legs in parallel, so CI wall time tracks the slower leg plus
lint and type check.

Measured spot runs for this design (one module per process,
`python -m unittest <module>`, this host, warm cache) give the
ranking; absolute seconds are host-specific and include one
interpreter start each:

| Module | Tests | Measured |
| --- | --- | --- |
| `test_launch.py` | 170 | 149.9s |
| `test_hub.py` | 143 | 93.7s |
| `test_drive.py` (one of 17 drive modules) | 63 | 34.5s |
| `test_takeoff.py` | 107 | 25.1s |
| `test_serve.py` | 192 | 23.3s |
| `test_init.py` | 110 | 11.7s |
| `test_merge.py` | 128 | 5.0s |
| `test_board_writes.py` (one of 18 board modules) | 43 | 4.6s |
| `test_forge.py`, `test_tracker.py` | 138, 92 | under 1s each |

So the cost is not spread evenly: launch and hub alone account
for roughly 240s of the spot total, ahead of any single drive,
board or serve module. The pattern behind the ranking is
per-test real work the fake cannot remove: local git worktrees
and unit argv construction in launch, loopback hub servers and
claim rounds in hub, SQLite stores in drive and board, and
loopback HTTP in serve. The roughly 95 remaining modules (quota,
admission, hygiene, gc, settings, prose, guards; about 3,400
tests) are mostly pure functions and are slow only in aggregate.
Extrapolating the spot runs is not a substitute for a full-suite
profile; ticket S0 in section 6 adds a per-module timing report
so the ranking is measured continuously, not once here.

Three structural costs dominate, and the scenario tier must not
repeat them: (a) process and interpreter start per module is
already paid once by discovery, but per-test temp git repos and
SQLite files are the real cost in the top groups; (b) loopback
HTTP servers are started per test in `test_serve.py`, which is
correct there and would be wasteful per scenario; (c) nothing
sleeps on a real clock by design (`suiteguard` and the injected
`now` seams see to that), so the tier can hold the same rule at
no cost. The conclusion for section 4: a scenario tier that
reuses one temp host per module, shares one loopback server per
module, and never sleeps can hold tens of journeys inside a
minute, two orders of magnitude under the unit suite.

## 2. Scenario catalog, derived not guessed

Derivation, in order: every verb and exit code in
`docs/commands.md`; the workflows in `docs/guide.md` (install,
init, register a project, doctor, a first drive pass, merge,
freeze and resume, hygiene, the dashboard); and the closed bug
issues and merged fixes whose failure crossed a module boundary.
Each scenario names its source: a verb row, a guide section, or
an issue number. Issues are named by number only, as the board
names them; the failure shape is restated generically.

Reading the table: **Journey** is the user-visible flow.
**Chains** lists the real pieces composed. **Faked seams** is
the complete list of what is not real; anything not listed runs
for real (settings resolution, the SQLite store, argv building,
exit-code mapping, loopback HTTP). **Asserts** names the state,
exit codes and call counts that matter. **Est.** is the runtime
budget on the CI runner; every scenario is designed for the
fake-clock, no-sleep rule, so estimates are dominated by temp
file and git work, not waiting.

Tiers: **A** is the smoke core (every PR, the 60 second budget
in section 4). **B** is the incident tier (same job, same
budget; it exists so a fixed cross-module bug cannot return).
**C** is the contract tier (golden and transcript checks; same
job, still fast, but allowed its own module so a fixture churn
never blocks tier A debugging).

### Tier A: documented journeys

| # | Scenario (source) | Journey | Chains | Faked seams | Asserts | Est. |
| --- | --- | --- | --- | --- | --- | --- |
| A1 | First run (guide: first run; `deps`, `doctor`, `paths`) | empty host, `deps`, `doctor`, `paths --mkdir` | CLI, `paths`, settings, deps probe | program probes, user-manager listing | exit 0 or 1 per row; state, config, cache dirs created owner-only | 0.5s |
| A2 | Register a project (guide: registering; `init --plan`, `init`) | `init --plan` writes nothing, `init` registers, `config show --origin` agrees | CLI, init writer, registration store, settings layers | `gh` runner, git remote read on a local repo | plan exit 0 and no files; init exit 0; resolved default profile named with its origin | 1.0s |
| A3 | Scaffold and onboard (`new --plan`, `new`, `github setup --plan`) | scaffold a project, onboard it, plan the board setup | `new` templates, init, tracker factory selection | `gh` runner, forge remote creation off | no outward call in plan mode (recorder empty); labels step idempotent on rerun | 1.5s |
| A4 | First drive pass (guide: the loop; `drive --plan`, `drive`) | plan, then a real pass claims and launches one ready ticket | CLI, store, `run_pass`, eligibility, launcher argv, registry | tracker, unit run and list, clock | plan exit 0 and store unchanged; pass exit 0, one claim with epoch, one launch call, mirror drained | 1.5s |
| A5 | Watch and stop (guide: the loop; `tail`, `agent list`, `stop`) | after A4, tail the session, list agents, stop the ticket | registry, session files, unit stop seam | unit listing and stop | tail exit 0 names the role session; stop exit 0; a second stop exits 1 | 0.5s |
| A6 | Resume and hold (guide: the loop; `resume`, hold label) | resume a finished ticket; a held ticket refuses | session store, launch path, tracker label read | tracker, unit run | resume exit 0 with a new unit; held resume exit 1 and no unit started | 1.0s |
| A7 | Quota routes a launch (`quota`, `quota mark`, `launch`) | exhaust a provider, watch the chain drop it, then refuse at 75 | accounts file, quota ledger, gate, launch overlay | quota readers, unit run, clock | `quota` shows the mark; launch picks the live provider; all exhausted exits 75 | 1.0s |
| A8 | Freeze and resume the loop (guide: freeze; `freeze start`, `drive`, `freeze end`) | freeze, pass refuses, end, pass runs | freeze and pause files, `run_pass` gate, doctor row reader | unit listing, tracker, clock | frozen pass exit 1 naming the reason and starts nothing; after end, pass exit 0 | 1.0s |
| A9 | Dashboard over a driven host (`snapshot --json`, `serve`) | drive one ticket live, read the snapshot, serve it and fetch it | snapshot builder, `serve` loopback server, HTTP client | unit listing, clock | served JSON equals the snapshot document; agent row present; exit 2 on a held port | 1.5s |
| A10 | Merge a ready PR (`review record`, `review show`, `merge --plan`, `merge`) | record a review, plan the merge, merge | review record store, forge, merge gate rows | forge `gh` runner, tracker | self-review refused at record; plan exit 1 lists the blocking row; ready merge exit 0, one forge merge call | 1.0s |
| A11 | Hygiene plan (`gc`, `disk`) | seed a merged worktree and a stale session, plan both keepers | local git repo, proof-before-removal checks | forge PR index, disk usage | plan exit 0 lists the rows; nothing deleted without `--apply`; unproven rows never listed as safe | 1.0s |
| A12 | Takeoff gate (`takeoff --plan`, `takeoff`) | gate a clean ticket worktree, then break one check | worktree, commit scan, leak guard, review record | `git ls-remote` on a local remote, check commands | plan exit mirrors rows without running commands; a bad commit exits 1 naming its fix | 1.5s |

### Tier B: incidents that crossed a boundary

| # | Scenario (source) | Journey | Chains | Faked seams | Asserts | Est. |
| --- | --- | --- | --- | --- | --- | --- |
| B1 | Null ids to board writes (#735, #742) | `github setup` provisions a board whose create step answers without ids | tracker factory, board provisioner, mapping writer | `gh` runner returning id-less payloads | setup exits 1 (or 3 per repo failure), never sends a null id in a later mutation; recorded calls contain no `null` argument | 0.5s |
| B2 | Rate limit ends a ship (#732; guide: merge retry) | merge a PR whose forge call is rate limited, then succeeds after reset | forge seam, retry-after-reset helper when built, merge gate | `gh` runner scripted limit then success, fake clock and sleep | first attempt carries the limit to the caller as exit 3 shape, never a bare 1; after the clock passes reset, merge exit 0; sleep called with the reset delta, never a real sleep | 1.0s |
| B3 | Area holder survives a failed launch (#711, #746) | two tickets share an area; the holder's launch fails; the second ticket still refuses, and a released claim frees the lane | `run_pass`, area gate, claim store, launcher | tracker, launcher that raises, clock | failed launch leaves no synthetic holder; released claim lets the next pass start the waiter; refusal names area, holder and cause | 1.0s |
| B4 | Install during a pass (`drive install-timer`; #185 shape) | `install-timer --plan` is a usage error; installing while a pass holds the lock changes no cadence mid-pass | CLI parser routing, unit writer, drive lock | unit writer, manager reload | `--plan` after the verb exits 4; the pass that follows uses the cadence baked at install, and a reinstall is the only way it changes | 0.5s |
| B5 | Policy ignores dependencies (#685, #699 shape: merged work re-selected) | a ticket whose linked pull merged after its claim is never re-selected, even when the policy would pick it | eligibility policy loader from the ops root, pulls listing, `run_pass` | tracker, forge pulls, clock | pass exit 0 starts nothing for the merged ticket; a bad policy module in one project skips only that project (the #580 shape) | 1.0s |
| B6 | Foreign rows in a shared store (#553, #561) | a store holding another project's rows drives only its own | project registration, `run_pass` driven-repo guard, claim transaction | tracker factory per repo | foreign rows never claimed; a foreign row reaching the launch path refuses the pass with exit 4 naming the repo | 0.5s |
| B7 | Unreadable pulls fail closed (#529, #559) | the pulls listing fails; every start kind refuses and names the cause, with the listing capped per pass | `run_pass` pulls check, settings cap | forge pulls raising, tracker | no launch call; exit carries the cause; listing call count stays under the configured cap | 0.5s |
| B8 | Malformed stored status (#582, #619) | a store row with a malformed status meets a sync and a pass | store, board step, `run_pass` | tracker | neither crashes; the row is reported, the pass completes for healthy rows | 0.5s |
| B9 | Reviewer leaves no record (#639, #701) | a reviewer finishes with prose and no record; the loop resumes it once to leave one, then the merge gate can read it | finished-entry scan, resume path, review record, merge gate | tracker, unit run, session files | exactly one resume for the record; a second recordless finish is not resumed again; `review show` exit 0 after the record | 1.0s |
| B10 | Ceiling-0 project reserves nothing (#594, #600) | a paused project beside an active one launches nothing and takes no budget share | registration, admission, host ledger, slice seam | unit run, ledger on temp dirs | active project's split is computed as if the paused one were absent; no slice created for the paused project | 0.5s |
| B11 | Quota refresh goes stale (#641, #644) | readings age past the refresh bound during a long pass sequence | quota ledger, `run_pass` refresh step, dashboard snapshot | readers, clock advanced between passes | the pass refreshes at half the stale bound; the snapshot never shows a reading the gate would call stale without saying why | 1.0s |
| B12 | Claim released on a refused start (#354 shape) | admission refuses a launch the pass already claimed; the claim does not leak | `run_pass`, admission, claim store | launcher refusing with exit 75 shape | claim released with epoch kept; the next pass can claim the same ticket; journal names the refusal | 0.5s |
| B13 | Board variant flip with queued writes (commands: `doctor`, `sync`) | queue a mirror write, flip the tracker variant, then sync and mirror | store outbox, tracker factory, doctor board row | two tracker fakes, labels and board | doctor fails the repo row naming the flip; no write is sent through the wrong variant; the outbox row survives for the correct one | 1.0s |

### Tier C: contracts (golden and transcript)

| # | Scenario (source) | Journey | Chains | Faked seams | Asserts | Est. |
| --- | --- | --- | --- | --- | --- | --- |
| C1 | `gh` transcript contract (docs/19; section 3.4) | replay recorded `gh` answers through the real tracker and forge adapters | `github/tracker.py`, `github/forge.py` payload parsing | the recording itself (no runner) | parsed items, pulls and board rows equal the recorded meaning; a shape drift fails naming the field | 0.5s |
| C2 | Snapshot golden (existing `tests/golden/`) | build a snapshot over a driven fake host and compare the document shape | snapshot builder over harness state | clock, unit listing | key set and schema version match the golden file; volatile fields (times, ports) normalised before compare | 0.5s |
| C3 | Exit-code sweep (commands: exit codes) | run each verb's nothing-to-do and usage shapes in the harness | CLI parser and exit mapping for the verb set | per-verb outer seams | the code is the documented one (1 nothing to do, 4 usage, 2 held port or lock, 75 full) for every verb in the sweep table | 1.5s |
| C4 | Dashboard API golden (docs/05) | fetch every read route from the harness server | `serve` routes, snapshot, board and journal readers | clock | each route's key set matches its golden; an unknown route is 404 JSON, never HTML | 1.0s |

That is 12 + 13 + 4 = 29 scenarios. Every scenario is one
`unittest` method (or one table row, section 3.3) whose name
carries its id, for example `test_B2_rate_limit_ends_a_ship`, so
a CI failure names the scenario in the test id, the module and
the table above with no extra reporting machinery.

## 3. Harness: one shared fake host

PROPOSED: one harness module, `tests/scenario.py`, imported by
`tests/test_scenarios_a.py`, `test_scenarios_b.py` and
`test_scenarios_c.py` (the tier letters). It composes the fakes
the suite already trusts and adds only the host around them.
No production code changes: every seam the harness injects
already exists (`launchcli.Seams`, `drive.run_pass` keyword
seams, tracker and forge factories, `paths` env overrides).

### 3.1 The pieces

| Piece (PROPOSED name) | What it is | Reuses |
| --- | --- | --- |
| `FakeHost` | one `tempfile.TemporaryDirectory` per test class (not per test) holding state, config, cache, sessions and an ops root; sets `KORAPY_STATE`, `KORAPY_CONFIG`, `KORAPY_CACHE`, `KORAPY_SESSIONS` (resolved through `paths`) and a fixed `KORAPY_AGENT_ID` env var directly (no `paths` resolver owns it); writes `accounts.toml`, `config.toml` and `projects.d/` registrations as text, so settings resolve from files, never from hand-built values | `paths` resolvers, the temp-dir pattern in `test_launch.py` |
| `ScriptedTracker` / `ScriptedForge` | the existing `FakeTracker` and `FakeRun` wrapped with a script queue: each call pops the next scripted answer or raises the scripted error, and appends `(verb, repo, number)` to one shared recorder | `FakeTracker`, `FakeBoardTracker`, `FakeRun` in `test_tracker.py` |
| `FakeUnits` | records `systemd-run` argv, answers `list_units` from a table the scenario mutates, and implements stop; destructive seams stay required arguments with no default, per AGENTS.md | `launchcli.Seams` (`list_fn`, `run`, `stop_fn`) |
| `FakeLauncher` | records launch requests and can be scripted to succeed, refuse (75 shape) or raise; on success it writes the session file and registry entry a real launch would, so `tail`, `stop` and `snapshot` read real files | `FakeLauncher` in `test_area_locks.py` |
| `FakeClock` | one mutable `now` plus a `sleep` that records durations and advances `now`; injected everywhere a seam takes `now`, `now_fn` or a sleeper; no scenario ever calls real `time.sleep` | the injected `now` parameters in `drive.run_pass` and `launch` |
| `DashboardClient` | starts one `serve` `ThreadingHTTPServer` on port 0 per test class against the `FakeHost` state, reads it with `http.client`, and shuts it down in `tearDownClass` | the server and client pattern in `test_serve.py` |
| `run_cli` | calls `cli.main(argv)` in-process with captured stdout and stderr and returns `(exit_code, out, err)`; scenarios assert on all three | the in-process verb calls in `test_takeoff.py` and friends |

Two rules keep the harness honest. First, isolation: the host
ledger beside the XDG state base (which deliberately ignores
`KORAPY_STATE`) is redirected by pointing `XDG_STATE_HOME` at
the fake host too, and `KORAPY_AGENT_ID` is set to a fixed
scenario value and restored, because ambient values have
already leaked into tests once (#698, #710). Second, proof:
`suiteguard` installs at `tests/__init__.py` import for every
runner, so a scenario that reaches a real `gh`, `systemctl`, a
runtime binary or a non-loopback address fails the run and
names the scenario, with no new guard code.

### 3.2 Framework choice

Hard rule from AGENTS.md: Python standard library only, at
runtime and in tests, and `unittest` only. That excludes
pytest (fixtures, parametrize, plugins), Hypothesis, VCR-style
recording libraries, testcontainers, and any HTTP test client
beyond `http.client`; each would be a new dependency or a
second runner, and `make test` must stay
`python -m unittest discover -t . -s tests`. The recommendation
fits the rule exactly: plain `unittest.TestCase` classes over
the harness above, `setUpClass` for the shared host and server,
`subTest` for table rows, and `unittest` discovery so the tier
runs under the existing command with no new tooling. Ad-hoc
per-scenario setup is rejected: it is what the suite already
has, and it is why no journey crosses more than one boundary
today.

### 3.3 Table-driven scenarios

Where a scenario is one journey with many inputs (C3 exit
codes, B7 refusal kinds, A7 provider states), PROPOSED: one
method driven by a tuple table with `subTest`, each row named
by its scenario id. Where the journey itself differs (A4, B3,
B9), a named method each: a table that encodes control flow
becomes unreadable and hides which assertion failed. The line
is: same verbs in the same order, table; different order or
different verbs, method.

### 3.4 Recorded `gh` transcripts as contract fixtures

PROPOSED: `tests/fixtures/gh/` holds a small number of recorded
`gh api` answers as JSON files, captured once by hand from the
real shapes the adapters parse (issues, pulls, board project
and items, rate_limit), scrubbed to `example.com` and generic
logins. Tier C replays them through `FakeRun` into the real
`GithubTracker` and forge adapter. This is a contract test that
the fakes still match the real shape: `FakeTracker` answers are
written by hand, so only a transcript can catch a renamed field
breaking the adapter and every fake at once. Transcripts are
fixtures, never recordings made in CI (CI has no `gh` login and
`suiteguard` would refuse the call), and each file names the
adapter function whose payload it pins. Refreshing one is a
manual maintainer step, listed in section 5.3.

### 3.5 Golden output

Golden checks already exist (`tests/golden/heavy-*.json`,
`snapshot-resources.json`, the init plan goldens) and the tier
extends the pattern only where the output is a stable document:
snapshot and dashboard JSON key sets (C2, C4). PROPOSED
normalisation before compare: timestamps, ports and temp paths
are replaced with fixed tokens, so a golden never flakes on
clock or host. Human prose (drive summaries, doctor rows) is
asserted by substring and exit code, never golden, because
wording fixes should not churn fixtures.

## 4. Speed and cost budget

PROPOSED numbers, all measurable in CI output:

| Budget | Value | How it is enforced |
| --- | --- | --- |
| Per scenario | under 2 seconds | harness uses fake clock, no sleeps; a scenario over budget is visible in `unittest -v` duration reporting per class |
| Whole tier (tiers A+B+C, 29 scenarios) | under 60 seconds on the CI runner | one `make scenarios` job; the job fails its own budget check when the tier exceeds 60s, reported as a job warning first, a failure after one release of grace |
| External calls | zero | `suiteguard` (already installed for every runner) |
| Docker, network, real systemd, real `gh` | none | fakes at the existing seams only |
| Parallel safety | each scenario owns its `FakeHost`; no shared files, no fixed ports (port 0), no order dependence | `python -m unittest` can shard the three tier modules across runners unchanged |

How the tier runs, PROPOSED: a `make scenarios` target
(`uv run --locked python -m unittest discover -t . -s tests -p "test_scenarios_*.py"`)
and a separate `scenarios` job in `.github/workflows/ci.yml`
beside the existing `check` job, on the same `ubuntu-latest`
runner, so feedback arrives in about a minute instead of after
the full matrix. The tier is NOT part of `make check` and NOT
part of the pre-push hook stamp at first: the hook already
reuses a green-matrix proof for an identical tree, and folding
a new tier into that proof before it has a flake history would
slow every push. After one release with no flakes, ticket S6
re-evaluates folding tier A only into `make check`. A failure
names the scenario three ways: the test id (`test_B2_...`),
the scenario table row in this doc, and the recorded seam calls
the harness prints on failure (tracker, forge and unit calls in
order), so the first debugging step is reading the call log,
not reproducing locally.

The 15 to 20 minute unit suite is out of scope to fix here.
PROPOSED follow-ups, filed separately, not built by this
design: split the CI `check` job per Python version (3.11 and
3.14 as separate jobs instead of one matrix step), cache the uv
environment between runs, and add the per-module timing report
(ticket S0) so the launch and hub costs in section 1.3 are
tracked. Each is its own ticket with its own review.

## 5. Not in scope

### 5.1 Excluded by design

Real GitHub (no token, no network in CI), real systemd (no user
manager on the runner), real agent runtimes (`omp`, `muse`
binaries never start), load and soak tests, and browser-driven
dashboard tests (the page is covered by `test_webui.py` and
friends over loopback; scenarios assert the JSON the page
consumes).

### 5.2 What the tier will not catch

A `gh` version whose output shape changed since the transcript
was recorded, a systemd behaviour no fake models, and timing
races that need real processes. Those belong to the manual
smoke checklist below, run by the maintainer on a real host.

### 5.3 Appendix: manual smoke checklist (maintainer, real host)

Run after a release, on a host with a real fleet. Each line is
pass or fail by eye; failures become issues, never scenario
tests.

1. `korapy deps` reports every required program ok.
2. `korapy doctor` shows no fail row; the timer row is ok.
3. `korapy drive --plan` names the candidates the next pass starts.
4. One real pass launches one agent; `korapy tail REPO N` shows its session.
5. `korapy serve` opens the dashboard; the agent from step 4 is listed live.
6. `korapy quota` ages match the providers' own dashboards within one refresh.
7. A rate-limited `gh` call by an agent paces instead of failing the ticket.
8. `korapy freeze start`, one refused pass, `korapy freeze end`, one live pass.
9. `korapy hub` claim and release against the real hub, if multi-host.
10. Refresh one transcript in `tests/fixtures/gh/` from real `gh` output and rerun tier C.

## 6. Ordered implementation tickets

Each ticket is small, carries its own tests, and lands behind
the one before it. No ticket changes an exit code, a public
interface or a production seam shape; if one turns out to need
that, it stops for a decision instead.

| Ticket | Scope | Tests included |
| --- | --- | --- |
| S0 | Per-module timing report for the unit suite (script plus CI artifact), no behaviour change | the report lists every module; a fixture run reproduces the ranking shape |
| S1 | The harness: `tests/scenario.py` (`FakeHost`, recorder, `FakeClock`, `FakeUnits`, `FakeLauncher`, `DashboardClient`, `run_cli`) | harness self-tests: isolation (ambient `KORAPY_AGENT_ID` ignored), no external call under `suiteguard`, clock never sleeps |
| S2 | Tier A scenarios A1 to A6 (first run, register, scaffold, first pass, watch and stop, resume and hold) | the six scenario methods |
| S3 | Tier A scenarios A7 to A12 (quota, freeze, dashboard, merge, hygiene, takeoff) | the six scenario methods |
| S4 | Tier B incident scenarios B1 to B13 | the thirteen scenario methods, each linked to its issue |
| S5 | Tier C contracts: transcripts, goldens, exit-code sweep; `make scenarios` target | the four scenario methods; the target runs only the tier |
| S6 | CI: the `scenarios` job beside `check`; after one flake-free release, evaluate folding tier A into `make check` | CI change verified by a deliberately failing scenario on a branch, then reverted |

Options:
1. Approve as designed (recommended)
2. Approve harness and tier A only
3. Fold into make check from day one
4. Table-driven scenarios only
5. Skip transcripts (C1)
6. Do not build the tier

