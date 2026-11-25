# 20: a ratatui TUI dashboard as a sub-project

Status: design, awaiting maintainer approval. No code ships with
this doc. It designs a Rust ratatui terminal dashboard that lives
in this repository as a sub-project (`tui/`, its own `Cargo.toml`)
and runs alongside the web dashboard (`12-web-dashboard.md`) as
another read-only client of the dashboard API
(`05-dashboard-api.md`). Implementation waits for approval and
follows the tickets in section 6.

Decided by the maintainer (not reopened here): option A, a Rust
ratatui TUI, not Python `curses`; the sub-project lives inside
this repository, not a separate repository; the TUI only formats
what a snapshot says and derives nothing; running it next to the
web UI needs nothing extra.

Conventions: endpoint paths, field names, constants and behaviors
quoted from `serve.py`, `snapshot.py`, `dashboard.py` and
`docs/assets/dashboard-fixture.json` were read from the tree at
design time. Rust crate versions, terminal behavior and Docker
build facts that could not be verified from the tree are marked
UNVERIFIED with a safe way to learn them. Nothing in this doc
names a person, a host, an account or a private repository; the
generic stand-ins (`host-1`, `repo-a`, `provider-a`) come from the
committed fixture.

## 1. Layout and version 1 scope

The TUI is a single foreground program attached to a terminal. It
opens on one screen, polls the local dashboard API, and quits on
`q`. It is watch-only in version 1: it sends only GET requests and
holds no write path at all (section 2). Actions such as pause or
cap changes are a later step with their own design, auth story and
approval; version 1 does not reserve keys, menus or endpoints for
them beyond leaving the footer free.

### 1.1 Screen layout

One screen, four bands, top to bottom (widths adapt to the
terminal; below 80 columns the peek pane moves under the agent
list instead of beside it, PROPOSED and pinned by a layout test at
80 and 120 columns):

```
+------------------------------------------------------------------+
| header: host, now, live/stale/finished, quota, capacity, load    |
+------------------------------------------------------------------+
| attention: needs-decision, ready-to-merge, research (or clear)   |
+-------------------------------+----------------------------------+
| agent list (snapshot order)   | peek pane (/v1/tail for the      |
| id ticket role state doing    | selected ticket, plain text)     |
|                               |                                  |
+-------------------------------+----------------------------------+
| board: total, drift | doctor rows: degraded and stale sections   |
+------------------------------------------------------------------+
| footer: keys, as-of time, API state (ok, down, schema warning)   |
+------------------------------------------------------------------+
```

Panels, and the exact fields each one formats (no panel computes
a value the API did not send):

- **Quota and resource header.** One line per fact, all read from
  the snapshot document: `fleet_header` (`host`, `now` (epoch),
  `tz_offset_s`, `now_iso`, `agents_live`, `agents_stale`,
  `agents_finished`); `quota`
  (per provider `state`, each window `used_pct` and `resets_at`,
  plus `stale`, `read_at`, `source` and `read_failed` shown in
  words when set, never as a computed zero); `capacity`
  (`live` of `total_cap`, `repo_cap` with 0 reading as no per-repo
  limit, `pending`, `by_project`); `resources.pressure` (`ncpu`,
  `load_1min`, `mem_total_mb`, `mem_available_mb`, shown as the
  values sent; a gauge bar divides by the denominator the
  snapshot itself carries, it invents none); `disk` (every path
  as one line, in the order sent; ranking paths by free space is
  a local computation and stays a later, labelled local view);
  and `drive` (`paused` with `pause_reason`, `last_pass_at`,
  `last_exit`, `starts_today` of `max_starts_per_day`). A quota
  window or pressure field that is null renders as no reading
  recorded yet, naming the fact, never as 0.
- **Attention band.** A single band from the snapshot `attention`
  section: `needs_decision`, `ready_to_merge` and `research`
  rows as sent, with the `coordinator_handled` count, or one
  line reading nothing needs attention when the lists are empty.
  The `plan` section's `next` rows and `next_pass_in_s` sit at
  the right of the same band. When `attention.state` or
  `plan.state` is `unknown` or `error`, the band says so in words
  (the panel sections carry their own state, `snapshot.py`
  `PANEL_SECTIONS`); it never renders an empty band as all clear.
- **Agent list.** The `agents` list in snapshot order. The
  snapshot builder already sorts by urgency through
  `dashboard.URGENCY` (`snapshot._sort_key`), so the TUI keeps
  the order it receives and does no sorting of its own in
  version 1. Columns: `id`, ticket as `repo#number` (blank when
  `repo` or `number` is null), `project`, `role`, `runtime`,
  `profile`, `model`, `state` with `derived`, `idle_s` shown as
  sent (the string `unknown` stays `unknown`), and `doing` (the
  row's `note` fallback is the server's business, already applied
  in `derived`/`doing`; the TUI prints `doing`). Finished rows
  stay in place in version 1; there is no collapse toggle.
- **Peek pane.** `GET /v1/tail?repo=&number=&last=` for the
  selected row's ticket, plain text as returned (section 2),
  scrolled with the movement keys while the pane holds focus. A
  row with no ticket shows no ticket, no tail in the pane. A
  `404` from the tail endpoint shows tail unavailable for this
  ticket; it never retries in a loop (one fetch per selection
  change or explicit refresh).
- **Board counts.** From `GET /v1/board?body=0&limit=1` (section
  2): the reply `total` and `drift` beside the label board. One
  slim call buys both counts; the TUI shows no board rows in
  version 1. A board failure shows board unavailable with the
  error text sent, never a zero count presented as truth.
- **Doctor rows.** There is no doctor endpoint in the dashboard
  API, and version 1 adds none. The doctor rows panel is a
  formatting of degraded facts the snapshot and health reply
  already carry: every snapshot section whose body is exactly
  `{"error": ...}` (`snapshot.is_error_section`, the sections in
  `snapshot.ERROR_SECTIONS`) listed by name with its error text,
  every quota provider with `stale` true or `read_failed` true,
  `drive.paused` with its reason, all from the last good
  snapshot. When the snapshot call itself fails the TUI also
  tries `GET /v1/health` and uses its `degraded` list only on a
  `200` health reply; health and snapshot share one cached build
  on the server (`serve._cached_doc`), so a build failure
  answers `500` from both and a down server answers neither, and
  in both cases the doctor rows keep showing the last good
  snapshot's sections under the API down state (section 2.4),
  never an empty panel read as healthy. The
  panel is labelled doctor rows and each row names what the
  operator can run (for example `korapy doctor`); the TUI passes
  no verdict of its own and computes no check. A clean snapshot
  reads no degraded sections, not all checks pass: the TUI does
  not run the doctor, and the wording says so.

### 1.2 Keys in version 1, and what is later

Version 1 keys (the whole set; a footer line lists them):

| Key | What it does |
| --- | --- |
| `q`, `Ctrl-C` | quit (restores the terminal first) |
| `j`, `k`, Down, Up | move the agent selection by one row |
| `Enter` | move focus to the peek pane (fetches the tail once) |
| `Esc` | return focus to the agent list |
| `r` | refetch the snapshot and the board counts now |

Later, each with its own ticket when a user asks for it (none is
in version 1, and version 1 code must not half-build them):
full vim-style motions (`g`/`G`, `Ctrl-D`/`Ctrl-U`, counts),
sort by a chosen column, a text filter over the agent list,
mouse support (click to select, wheel over the peek pane), and
OSC 52 copy over ssh (copy the selected ticket or tail lines
through the terminal escape, which needs a capability probe and
a fallback message where the terminal refuses it; UNVERIFIED
until tested over a real ssh session with a terminal that claims
OSC 52 support). Sort and filter are display operations over
rows already received; when they land they must be labelled as
a local view and must restore snapshot order on reset, so the
default view never disagrees with the snapshot about urgency.

Features of the reference TUI that come later, not in version 1:
fullscreen live render pushed by the SSE stream as the primary
mode (version 1 polls; section 2 keeps the stream as an optional
upgrade inside the same client), a command palette, multiple
saved views or layouts, and any action keys. Each arrives only
with its own design note or ticket; the version 1 layout leaves
no placeholder UI for them.

## 2. Data contract

### 2.1 Endpoints the TUI calls

The TUI is a client of `korapy serve` on loopback (default
`http://127.0.0.1:8420`, `serve.port` in `serve.py`). It calls
exactly these routes, all GET, all read-only:

| Route | When | What the TUI does with it |
| --- | --- | --- |
| `GET /v1/version` | once at startup, once after any schema warning | reads `{api, schema_version}` (`serve.API_VERSION` is 1 and `snapshot.SCHEMA_VERSION` is 4 in the tree) and compares with the versions it was built for |
| `GET /v1/snapshot` | every poll (default 2 s, section 2.2) | the one document the header, attention band and agent list format |
| `GET /v1/tail?repo=&number=&last=` | on peek focus or `r` while peeked | plain text into the peek pane; `last` at most the server cap (`serve.tail_max`, default 200, larger values clamp server side) |
| `GET /v1/board?body=0&limit=1` | every poll | reads only `total` and `drift` from the reply envelope |
| `GET /v1/health` | only when `/v1/snapshot` fails | uses `degraded` for the doctor rows panel only on a `200` reply (a build failure `500`s both routes and a down server answers neither, section 1.1); otherwise the footer reads API down |
| `GET /v1/stream` | never in the polling default; optional upgrade, section 2.2 | SSE wake-up hint only; the TUI still refetches `/v1/snapshot` to learn what changed |

The filtered views (`/v1/agents`, `/v1/quota`, `/v1/capacity`,
`/v1/disks`, `/v1/projects`, `/v1/events`, `/v1/journal`) are not
called in version 1: the snapshot already carries every section
the panels show, and one document per poll keeps every panel
timed against the same `now`. That is one fact, one fetch. If a
later version shows a per-project log it will add `/v1/events`
or `/v1/journal` then, with its own ticket.

Auth: on loopback with no token file the server serves openly
(`05-dashboard-api.md` section 3) and the TUI sends no header.
When a token file exists, the server requires it on every
request; PROPOSED: the TUI reads the same token file the server
reads (`paths.api_token_path`, read at call time, never stored,
never logged, never shown on screen) and sends
`Authorization: Bearer <token>`. The token path is resolved in
exactly one place in the Rust code (section 2.3). A `401` answer
reads as token rejected in the footer; the presented value never
appears in any message or log line.

### 2.2 Polling is the contract; the stream is an upgrade

Polling is the base contract, exactly as `05-dashboard-api.md`
section 2 decides for every dashboard: the TUI polls
`/v1/snapshot` and `/v1/board` on a fixed interval (PROPOSED
default 2 s, matching `serve.snapshot_ttl_s` so a poll can share
the server's cached build and cost a `304` when an `ETag` is
sent; the TUI sends `If-None-Match` with the last `ETag` and
keeps its current screen on a `304`). The interval is a named
constant in the TUI with a `--poll-s` flag override; it is not a
korapy setting, because the server already owns its cache TTL
and a second resolver for the same fact would be a bug.

The SSE stream (`GET /v1/stream`, topics `snapshot`, `events`,
`journal`, with `Last-Event-ID` resume) is an optional upgrade
inside the same client, off by default: with `--stream` the TUI
opens the stream and treats a `snapshot` event only as a hint to
refetch `/v1/snapshot` at once instead of waiting for the next
poll. The stream never carries panel data the TUI renders
directly, so a dropped stream loses nothing: the TUI falls back
to the poll timer, reconnects with its last event id, and says
stream disconnected, polling in the footer while it does. A
consumer that never opens the stream is fully correct, which is
the same rule the API design sets for every dashboard.

HTTP transport: PROPOSED, a small HTTP/1.1 client over the Rust
standard library `TcpStream` for loopback, plus `serde` and
`serde_json` for the documents (section 3). No async runtime and
no HTTP crate in version 1: the requests are four fixed GET
shapes, the replies are JSON or plain text, and the stream is
line based. UNVERIFIED: whether `ratatui` and its terminal
backend fit that dependency set at current versions; learn it in
the first build ticket by pinning the crates and building in
Docker (section 3), and record the resolved versions there.

### 2.3 Where the snapshot types live

All snapshot types live in one Rust module, PROPOSED
`tui/src/model.rs`: one struct per document section
(`Snapshot`, `FleetHeader`, `AgentRow`, `Quota`, `Capacity`,
`Resources`, `Disk`, `Drive`, `Attention`, `Plan`, `BoardCounts`,
`Version`, `Health`), deserialized with `serde`. That module is
the single place the wire shape is named, the Rust mirror of the
one fact, one place rule: every panel takes the typed model,
never raw JSON, and no other module names a snapshot field.
The wire shape is not one happy-path struct per section: the
API's degraded shapes are part of the contract, so `model.rs`
names them explicitly. Every section that can fail carries an
untagged error variant: any section, including `fleet_header`
(which sits outside `snapshot.ERROR_SECTIONS`, as do `attention`
and `plan`, while `agents` is in it alongside `quota`,
`capacity`, `resources`, `disk` and `drive`, the exact set the
health roll-up checks, so doctor-row detection in section 1.1
tests each section body for the `{"error": ...}` shape itself,
through one helper in `model.rs`), may arrive as exactly
`{"error": ...}` and must deserialize as that variant, never as
a parse failure. The panel sections carry
their unknown shapes too: `attention` with `state` unknown sends
the string `unknown` for `needs_decision`, `ready_to_merge`,
`research`, `coordinator_handled` and `items` (`dashboard.attention`),
and `plan` does the same for `next`, `waits`, `next_pass_in_s`
and `starts_remaining` when its state is unknown, so those
fields are untagged value-or-`unknown`
types. `AgentRow.idle_s` is an untagged integer-or-`unknown` type
(`dashboard.agent_state` sends both). A model that only parses
the ready shapes fails on exactly the degraded snapshots
sections 1.1 and 2.4 promise to render, so ticket T1 pins these
variants with mutated fixtures (section 6).

The never-derive rule, enforced there:

- Every field the server derives arrives ready made and is only
  formatted: `derived`, `idle_s` and `doing` per agent
  (`dashboard.agent_state` on the server), `stale` and confidence
  facts, `attention` and `plan` verdicts, board `drift`. The TUI
  contains no urgency table, no idle computation, no staleness
  math beyond showing an age the snapshot's own `now` anchors
  (an age is `now` minus a sent timestamp, formatted; the verdict
  words always come from the document).
- Unknown fields are ignored (the additive-only rule in
  `05-dashboard-api.md` section 4), missing fields deserialize
  as null or absent and render as the no-reading wording in
  section 1.1, and no field is renamed on the way to the screen.
- The fixture is the contract test: `docs/assets/dashboard-fixture.json`
  (schema version 4 in the tree) must deserialize into the model
  with no error and no field silently dropped that a panel shows
  (section 3.2).

### 2.4 When the API is down, and when the schema changes

API down (connection refused, timeout, `5xx`, or a body that is
not JSON): the TUI keeps its last good screen, writes API down,
last good as of T with the snapshot's own `now` in the footer,
and retries on the poll timer with a capped backoff (PROPOSED:
the poll interval doubling to at most 30 s, reset on the first
success). It never blanks the panels, never shows a zero fleet
as truth, and never exits on a failed poll: `korapy serve` being
restarted is a normal event. Before the first success the screen
reads no snapshot yet and names the address it is trying. A
failed `/v1/tail` or `/v1/board` degrades only its own panel
(section 1.1); the snapshot panels keep their as-of time. The
footer always distinguishes three states in words: ok (with the
as-of age), down (with the last-good as-of time), and schema
warning (below). Degraded state is shown, never hidden behind a
spinner, mirroring the `/v1/health` rule.

Schema change, detected two ways and handled before rendering:

1. At startup, and again whenever a snapshot's `schema_version`
   differs from the last one seen, the TUI reads
   `GET /v1/version` and compares `api` and `schema_version`
   against the pair it was built for (today `api` 1, schema 4,
   named as constants in `model.rs` beside the types).
2. `api` different from the built-for major: the TUI refuses to
   render fleet data, shows this TUI was built for API 1, the
   server reports API N in the footer, and keeps polling the
   version route only. A new API major is a cutover (AGENTS.md:
   one version of each runtime, no side by side dialects), so
   the TUI is rebuilt against it, never taught two majors.
3. `schema_version` newer than built for: the TUI renders what
   it can (additive fields are ignored by contract) under a
   schema newer than this TUI footer warning, and names the
   upgrade route (`make tui`, section 4). Older: the footer
   reads server schema older than this TUI, and any panel whose
   expected section is absent shows the no-reading wording
   instead of guessing. A missing expected section is a warning,
   never a fabricated panel.

Exit behavior of the TUI process itself: `q` and `Ctrl-C` exit
0 with the terminal restored (raw mode and the alternate screen
are torn down on every exit path, including a panic hook, so a
crash never leaves a broken terminal). A usage error (an unknown
flag or a non-numeric `--poll-s`) exits 4 at startup before the
UI loop starts, matching the korapy exit code convention that 4
is configuration or usage. An unreachable server, including at
startup, is not an exit at all: section 2.4 above keeps the
program up and retrying. The TUI has no other exit codes in
version 1 because it decides nothing a script branches on.

## 3. Build and gating

### 3.1 The build: Docker, opt-in, outside `make check`

The host has no Rust toolchain as a korapy dependency:
`system-deps.toml` lists no cargo or rustc, `make bootstrap`
needs only uv, and the package rule in AGENTS.md (Python 3.11+,
standard library only, at runtime and in tests) is a statement
about the Python package. **The Rust sub-project does not change
that rule.** Nothing in this section adds a Python dependency, a
Python toolchain step, or a Rust entry to `system-deps.toml`; a
machine that never builds the TUI still passes `make bootstrap`,
`make deps` and `make check` exactly as today. Whether the
design machine happens to have cargo installed is beside the
point: the supported route must not assume it.

PROPOSED build route: Docker, following the reference fleet's
habit of building its TUI in Docker. `tui/Dockerfile` pins a Rust
image by digest (the digest is recorded in the Dockerfile and
bumped deliberately, never `latest`), copies `tui/` and
`docs/assets/dashboard-fixture.json` (tests only, section 3.2),
and runs `cargo build --release --locked` with a committed
`Cargo.lock`. `make tui` runs that build and copies the release
binary out of the image to `tui/target/korapy-tui` (PROPOSED
output path beside the sub-project, ignored by git); `make
tui-check` runs the Rust tests and `cargo clippy` inside the same
image. Both targets are opt-in. Host cargo (`cargo build` inside
`tui/`) is a documented convenience for a machine that already
has a toolchain, not a gated route: no test, hook or CI step may
require it.

Why not the alternatives. Adding cargo to `system-deps.toml`
would make every korapy host install a Rust toolchain for an
optional read-only screen, and would put a second ecosystem's
version policy inside `make deps`; rejected. Building on the
host in `make check` would slow the Python matrix (3.11 and
3.14) with a Rust compile and fail the gate on machines without
cargo for a component they do not run; rejected. Shipping no
build at all (source only) leaves every operator to improvise;
rejected. Docker is already a host program korapy uses
(`system-deps.toml`), so the TUI build reuses a checked
dependency instead of adding one. UNVERIFIED: the Docker daemon
is reachable by the building user on the target host and the
pinned image pulls on first build (network on first pull only);
learn both with `docker version` and one `make tui` on a clean
machine in the build ticket, and record the image digest and
crate versions that build resolves.

Gating, stated plainly so a reviewer can hold the line:

- `make check` (format check, type check, suite on 3.11 and
  3.14) does not build or test the TUI, and the pre-push hook's
  green-matrix proof is unaffected. The TUI is not part of the
  Python package: the implementing tickets must prove the wheel
  korapy builds contains no `tui/` files (the `uv_build` package
  already ships only `src/korapy`; a test or a build listing in
  the build ticket pins it, section 6, ticket T6).
- `make tui-check` is the Rust gate (tests plus clippy, `-D
  warnings` PROPOSED), run by whoever touches `tui/`, named in
  the sub-project README section and in each TUI ticket's
  acceptance. CI may add it as a separate opt-in job; it must
  not join the default matrix until a maintainer decides the
  TUI is load-bearing.
- `docs/commands.md` and `docs/configuration.md` do not change:
  the TUI adds no korapy verb, flag, setting or exit code. Its
  own flags (`--addr`, `--poll-s`, `--stream`) are documented in
  section 6's tickets and in `tui/README.md` when it lands, not
  in the korapy reference. `README.md` (the front door) changes
  only if the pitch or quickstart gains the TUI; the design
  expects no README change, and the implementing PR that first
  makes the binary reachable says why in its Review section.

### 3.2 How the tests run, with no outside-world call

Rust tests live in `tui/` (`cargo test`, run through `make
tui-check` in Docker) and obey the same rule as the Python suite:
no test touches the outside world (no network beyond loopback, no
real state dir, no `gh`, no systemd). Three layers:

- **Model tests over the shared fixture.** The fixture is the
  one committed example every dashboard shares. Tests read
  `docs/assets/dashboard-fixture.json` by a path relative to the
  crate (the Docker build copies it in; a host build reads the
  repository file directly), deserialize it into `model.rs`, and
  assert the panels' facts survive: `schema_version` 4, four
  agents with their `derived` and `idle_s` values as sent, both
  quota providers including the stale one, `attention` and
  `plan` states, and the `drive` counts. A fixture that stops
  parsing fails the gate: that is the schema drift alarm, and it
  fires in `make tui-check` before any host sees a broken screen.
- **Render tests over the model.** Ratatui's test buffer renders
  each panel from the fixture model at fixed sizes (80 and 120
  columns) and asserts the words on screen (the stale provider
  reads stale, the error-section wording appears when a section
  is replaced with an `{"error": ...}` body, the empty attention
  band reads nothing needs attention). Rendering is pure over
  the model, so these tests need no server at all.
- **Client tests over a loopback fake.** The HTTP client is
  tested against a fake server the test itself binds on
  `127.0.0.1` port 0 (the `tests/suiteguard.py` line, applied to
  the Rust suite by construction): canned snapshot, version and
  tail replies, a `401`, a dropped connection, and an SSE stream
  that closes, each asserting the footer state and the
  last-good rule in section 2.4. No test reaches a non-loopback
  address, a real `korapy serve`, or the real token file; the
  token test uses a temp file.

Call budget of the test suite: zero external calls. Every call a
test makes is a loopback call to its own fake, or no call.

## 4. Install route

Building and installing are separate steps, and installing is an
approval gate. The flow, end to end:

1. `make tui` builds the binary in Docker and leaves it at
   `tui/target/korapy-tui` (git-ignored build output, like every
   other build artifact).
2. A maintainer approves the install on that host, explicitly,
   in that maintainer's own session. Approval relayed by another
   agent, written in a ticket, or implied by an approved plan
   does not count (AGENTS.md, approval gates: installing or
   replacing anything on a host needs a maintainer's explicit
   go-ahead). One approval covers one install of one built
   binary on one host; a rebuilt binary is a new install.
3. The approved install copies the binary to a directory already
   on the operator's PATH, PROPOSED `~/.local/bin/korapy-tui`
   (the directory the runtime installers already use), with the
   version it was built for shown by `korapy-tui --version`
   (PROPOSED: the crate version plus the API and schema pair it
   was built against, so a stale install names itself). The
   install is a file copy and nothing else: no systemd unit, no
   change to any home config (`~/.omp/`, session dirs and shell
   startup files are never edited; the TUI reads the API token
   file only when it exists, section 2.1), no `system-deps.toml`
   entry.
4. Running it needs a running `korapy serve` on the same host
   (or `--addr` naming another loopback serve, PROPOSED flag);
   with none, the TUI shows its no snapshot yet state and
   retries (section 2.4), and the operator starts the server
   with the existing verb. The web dashboard and the TUI run
   side by side against the same server with nothing extra: two
   pollers share the server's cached snapshot build
   (`serve.snapshot_ttl_s`), and neither knows the other exists.

Removal is the reverse file operation (delete the copied
binary), operator-run, with no state to clean: the TUI writes no
files of its own in version 1 (no config, no cache, no history).

## 5. Repository rules

This repository is public, and the TUI sub-project follows every
repository rule as written (AGENTS.md, Nothing personal, ever):

- No names, usernames, emails, hostnames, IP or tailnet
  addresses, home paths, private repositories, their tickets, or
  quota numbers tied to a real deployment, in code, docs, tests,
  fixtures, commit messages or PR bodies. Generic stand-ins only
  (`host-1`, `repo-a`, `provider-a`, `~/.local/bin/`,
  `127.0.0.1`). The committed fixture already obeys this and the
  TUI tests reuse it as-is.
- The reference TUI the maintainer uses for another fleet is a
  private reference. Anything adapted from it (layout ideas,
  key habits, the Docker build habit) is rewritten from this
  design, never copied: no source file, comment, string or asset
  is carried over, and no commit or comment names that
  repository. This doc stays generic for the same reason: it
  describes the panels this API needs, not that fleet's screens.
  A reviewer who finds reference-specific wording or code shape
  in a TUI PR treats it as a finding (section 6, review gate).
- Secrets: the API token is read at call time and never stored
  or logged (section 2.1); quota payloads beyond usage numbers
  never reach the screen because the API never sends them
  (`05-dashboard-api.md` section 1).
- Commits, hooks and the review gate apply to `tui/` exactly as
  to `src/korapy/`: Conventional Commits, no trailers, never skip
  hooks, tests first, and the separate-reviewer step before any
  TUI PR opens. The Python-only rule is scoped to the Python
  package (section 3.1); it was never a ban on a second language
  in an opt-in sub-project, and this doc is the record that the
  maintainer chose one deliberately.

## 6. Ordered implementation tickets

Each ticket is one agent of work with its own tests, built on the
previous ticket's tests, in this order. Call counts count HTTP
calls the running TUI makes; test call counts are zero external
calls (section 3.2) unless a ticket says otherwise. Every ticket
ends with `make check` green (the Python gate, untouched) plus
`make tui-check` green once ticket T1 creates it, and no ticket
changes a public korapy interface (no verb, setting, schema or
exit code) unless it says so.

- **T1: the snapshot reader and types.** Create `tui/` (its own
  `Cargo.toml` with `Cargo.lock`, ratatui and its terminal
  backend, `serde`, `serde_json`), `tui/src/model.rs` with the
  section 2.3 types and the built-for pair (API 1, schema 4),
  and the loopback HTTP client (section 2.2) with `GET
  /v1/version`, `GET /v1/snapshot` (ETag aware) and `GET
  /v1/health`. No screen beyond a one-line as-of footer proving
  the read. Tests: the fixture deserializes with every panel
  fact intact; an unknown field is ignored; a missing section
  reads as the no-reading value; mutated fixtures pin the
  degraded shapes (a section replaced with `{"error": ...}`
  including `fleet_header` and `agents`, `attention` and `plan`
  in their `unknown` string shapes, an `idle_s` of `unknown`);
  fake-server tests pin the
  down, `401`, `304` and schema-warning footer states. Call
  count: 1 version call at startup, then 1 snapshot call per
  poll (plus 1 health call per failed snapshot); the tests make
  0 external calls.
- **T2: the layout and the agent list.** The four-band layout
  (section 1.1) with the header and attention band formatting
  the typed snapshot, the agent list in snapshot order, the
  selection keys (`j`/`k` and arrows), `q` with terminal restore
  on every exit path, and the footer API states. Board counts
  and doctor rows render as unavailable placeholders that T4
  replaces (labelled as such, never as zeros). Tests: render
  tests over the fixture model at 80 and 120 columns pin the
  header counts, the stale quota wording, the empty and
  non-empty attention bands, and snapshot order in the list.
  Call count: T1's count plus 0 (board and tail land later).
- **T3: the peek pane.** `GET /v1/tail` on `Enter` for the
  selected ticket, plain text in the peek pane, pane scrolling,
  `Esc` back to the list, the no-ticket and `404` wordings, and
  one refetch on `r` while peeked. Tests: a fake tail endpoint
  pins the text passthrough, the clamp (a `last` above the cap
  is the server's clamp, asserted on the request the client
  sends), the no-ticket wording and the `404` wording; render
  tests pin the narrow-terminal stacking. Call count: 1 tail
  call per selection or explicit refresh, 0 per poll.
- **T4: the header and board panels, complete.** Wire the
  remaining header facts (capacity, resources pressure, disk,
  drive) to their final wording, the board counts poll
  (`/v1/board?body=0&limit=1`, `total` and `drift`), the doctor
  rows panel (section 1.1: error sections, stale or failed
  quota, paused drive, health `degraded`), and the quota
  no-reading wording for null windows. Tests: fixture renders
  pin every header line; a mutated fixture with an
  `{"error": ...}` section and a paused drive pins the doctor
  rows; a fake board reply pins total and drift and the board
  unavailable wording. Call count: 1 board call per poll beside
  the snapshot call (2 calls per poll in total), plus health
  only on snapshot failure.
- **T5: keys, complete, and the stream upgrade.** The version 1
  key set is finished and documented in the footer and in
  `tui/README.md` (created here: what it is, `make tui`, the
  keys, the install pointer to section 4), the `r` refetch, and
  the optional `--stream` mode (a `snapshot` event triggers an
  immediate refetch; a dropped stream falls back to polling and
  reconnects with `Last-Event-ID`). Sort, filter, mouse and OSC
  52 stay later (section 1.2) and are named as such in the
  README. Tests: key-handling tests drive the app state without
  a terminal (movement, focus, quit, refetch); a fake SSE
  server pins the wake-up refetch, the resume id and the
  polling fallback. Call count: polling unchanged; in stream
  mode, 1 long-lived stream call plus 1 snapshot refetch per
  event, and the poll timer still runs as the fallback.
- **T6: the build and install targets.** `tui/Dockerfile`
  (pinned digest), `make tui` (Docker build, binary copied to
  `tui/target/korapy-tui`), `make tui-check` (tests plus clippy
  in the same image), `--version` naming the built-for API and
  schema pair, proof the korapy wheel contains no `tui/` files,
  and the section 4 install documented in `tui/README.md` as a
  maintainer-approved file copy (the ticket documents it; it
  does not install anything on any host). Tests: `make tui` and
  `make tui-check` pass on a clean checkout in Docker; the
  wheel-contents check runs in the Python suite or the build
  ticket's script, whichever the tree's conventions favor, with
  the choice recorded in the PR. Call count: 0 (the build calls
  no korapy API; the first image pull is the only network the
  build ever needs, UNVERIFIED until T6 runs it).

Review gate for every TUI ticket: a separate reviewer reads the
diff against AGENTS.md and this doc, asked one question (does
the TUI format only what the snapshot sends, derive nothing,
and stay inside the opt-in gate), with the reference-copy rule
in section 5 as an explicit check. Findings follow the fix in
place or file rule in AGENTS.md.

## Options:

1. Poll by default, Docker build, opt-in gate (recommended)
2. Stream first, Docker build, opt-in gate
3. Poll by default, host cargo in system-deps.toml
4. Python curses instead of ratatui
5. Separate repository for the TUI

Options 4 and 5 reopen maintainer decisions recorded at the top
of this doc; they are listed so the approval records that they
were set aside, not as live candidates. Option 3 puts a Rust
toolchain on every korapy host for an optional screen (section
3.1). Option 2 makes the primary mode depend on a long-lived
connection the API design treats as an upgrade (section 2.2).
