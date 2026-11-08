# 18: area locks for the dispatch loop

Status: design, awaiting maintainer approval. No code ships with
this doc. It decides how the loop refuses to run two agents in
overlapping areas of one project at once. Implementation waits
for approval and follows the tickets in section 6.

Conventions: module, function and file names quoted below were
read from the tree at design time (`drive.py`, `eligibility.py`,
`policies.py`, `admission.py`, `hold.py`, `settings.py`). Behavior
not yet implemented is marked PROPOSED; everything else describes
the tree as it stands.

## 0. Scope and non-goals

The design adds: what an area is, two PROPOSED settings, one
PROPOSED pure helper, one PROPOSED gate check with its `--plan`
and journal wording, the release rule, the cost statement, the
test plan and the migration. Non-goals: changing the eligibility
interface (`eligible(rows, *, now)` keeps its shape), changing
what `basic` selects (the gate stays authoritative so projects
get the lock with no policy code), cross-project locks (areas
are per project), board-field areas (labels only in version one;
`settings.py` already owns an unrelated board `area_column`
mapping, which this design does not read), merging and closing
(a maintainer merges; the loop never merges).

## 1. What an area is, and where the setting lives

PROPOSED: `drive.area_prefix`, a string registered in the
settings registry and resolved per project like `agents.cap`,
default `""` meaning off. A project opts in from its ops root
`korapy.toml`, for example `drive.area_prefix = "area:"`. The
`drive.*` namespace matches every other drive-pass tunable in
the `drive` row of `commands.md`; no new top-level namespace.

An area is a label with that prefix. The suffix past the prefix
is a path split on the fixed separator `":"`, so with prefix
`"area:"` the label `area:ci:suite` names the path
(`ci`, `suite`), a child of (`ci`,). The separator is fixed in
version one to match this shape; no second setting. Matching is
exact and case sensitive, like labels everywhere. A label whose
suffix is empty or carries an empty segment (for example the
bare prefix, or `area::x`) names no area and is ignored. A ticket
carrying several area labels holds several areas (the union); a
ticket with no area label holds none. The project owns its
labels; korapy never invents an area. Label areas are
authoritative and the board `area` field is ignored by the
lock: when a project maps `area_column` and also uses `area:`
labels with different values, the lock reads only the labels.

## 2. The lock rule: a pure function over plain rows

PROPOSED: one pure helper beside `drive.gate`, over rows the
pass already read:

```python
def area_hold(candidate: TicketRow, rows: Sequence[TicketRow], *,
              prefix: str, hold_until_merged: bool) -> str | None: ...
```

It returns the blocking area path, or None when the candidate
is free. Inputs only: the candidate row, every row in the same
project's driven set, the prefix, and the release option. No
tracker, forge, clock or settings access inside.

Holders. A row holds its areas while it proves live work:

- a non-empty `agent` claim holds, managed or foreign: the fact
  is that another agent works there, not who minted the id;
- a row with an open linked pull (`pulls` entries with state
  `"open"`) holds, only when `hold_until_merged` is true
  (section 3).

The candidate's own row never blocks itself: same
`(repo, number)` is excluded, so a ticket's own claim or pull
cannot refuse it.

Overlap. Two areas overlap when their paths are equal or one is
a strict path prefix of the other: an area overlaps its parents
and its children, never its siblings. `ci` overlaps `ci:suite`
and `ci:suite` overlaps `ci`; `ci:suite` does not overlap
`ci:lint`. Candidates overlap when any of the candidate's areas
overlaps any of a holder's areas.

Gate placement. The check refuses kind `start` only (a `fix` or
`resume` reclassified as a fresh start on an unclaimed row
counts as `start`), placed in `drive.gate` directly after the
linked-pull fresh-start check and before `hold`: it needs only
rows already in hand, so it runs before any check that reads
labels live. `resume`, `fix` and `review` on a claimed row
bypass it: same-ticket fix rounds and reviewer takeovers always
proceed. The gate alone is not enough: `_gate_all` gates every
candidate over the same pre-pass rows snapshot, and
`store.claim` conflicts only on the same ticket, so two
unclaimed same-area tickets in one pass would both gate
"proceed" and both launch. PROPOSED: the claiming loop re-runs
`area_hold` for each proceeding `start` immediately before
`store.claim`, with the areas of tickets this pass already
claimed added to the holder set (synthetic same-project holder
rows), mirroring the existing "claim changed under the pass,
left held" re-read pattern. The second same-area start refuses
with code `area` and its claim is never minted. The reason shown in
`drive --plan` names the ticket, the area and the holder:

```text
owner/repo#12: area `ci` held by owner/repo#34 (agent `drive-abc`), start refused
owner/repo#12: area `ci` held by owner/repo#34 pull #56, start refused
```

`--plan` builds rows with `pulls` empty and never contacts the
tracker, exactly like the linked-pull check it follows: the plan
shows claim-held refusals and never PR-held ones. Holder pulls
must be fetched to be seen: `pulls_check_needed` returns False
for held rows and `basic` never selects them, so without more
wiring a held PR-holder reads `pulls` None and its PR-held areas
vanish. Failing case: ticket 34 holds `area:ci`, its claim
released at PR open, open pull 56, and a new `hold` label keeps
it unfetched; ticket 12 in `area:ci` would then gate "proceed"
and start inside pull 56's merge window, which the default
`True` handoff must prevent. PROPOSED: with a non-empty prefix
the pass adds same-project rows whose in-hand labels carry the
prefix to the pulls fetch set, reusing the one `pulls_index`
listing per repository the pass already reads; repos with no
area-labeled row cost no new call. `None` then means truly
unreadable only: other rows with `None` pulls contribute their
claim-held areas, while the candidate's own `None` already
refuses it earlier. When `drive.pulls_max_calls` caps the pass,
capped holders read None and lose PR-held areas but keep
claim-held ones; the existing "starts refused" notice counts
them, so the default handoff stays fail-open only under a cap
or a listing outage.

## 3. Release, no-area tickets, ceiling and hold

Release. A claim-held area releases when the claim releases
(worker done, orphan release, give-up). A PR-held area releases
when no open linked pull on another ticket holds it anymore
(merged or closed). PROPOSED default
`drive.area_hold_until_merged = True`: the claim hands
the lock to the pull request at PR open, so the area stays
locked from the first start to the merge. Set it false and
pulls stop holding other tickets: the area releases at PR open
and review latency no longer serializes it, at the price that
two agents may then work parent and child areas at once across
the merge window.

A ticket with no area label always passes this check; a mixed
queue serializes area tickets per area while no-area tickets
flow freely. A missing area label silently loses the
protection, which is why labels stay people-owned and the
doctor never guesses a ticket's area: korapy cannot tell a
ticket that needs no area from one that was mislabeled.

The project ceiling and the repo cap are untouched. The area
check runs before admission, so a refused candidate never
reaches it and consumes no `agents.cap` slot; scanning
continues to other tickets, projects and repos, exactly like a
`PROJECT_CEILING` skip. The lock only ever refuses; it never
raises a ceiling and never reserves a slot. Hold labels are
orthogonal and keep their place after the area check: a ticket
that is both area-blocked and held reports the area reason, and
`basic` still pre-filters held rows so the gate mostly sees
unheld ones.

## 4. Cost in parallelism, and how to measure it

The honest cost: one area is one lane. Two tickets in one area
wait even with idle slots and quota to spare, and nesting
amplifies it: a parent-area ticket blocks every child area, and
a busy child blocks the parent. Broad areas (one label across
half the board) serialize the fleet; narrow ones cost label
discipline. The coordinator's old rule had the same price; this
design only moves it into the shared gate where every project
can see it.

Measurement needs no new verb. Every refusal appends a journal
skip entry with code `area`, so the existing
`drive_status`/`snapshot` drive section counts it under
`skipped_by_reason` beside the other codes. A coordinator
compares `area` skips per pass and per day against starts, and
reads one ticket's wait as the gap between its first `area`
skip and its start. A rising `area` share with idle capacity
means the labels are too broad; near zero means the lock is
idle overhead only.

## 5. Test plan (fakes at the existing seams) and migration

No test touches the outside world. Pure-helper tests cover the
overlap table (equal, parent/child both directions, siblings,
multi-area union, case sensitivity, malformed labels ignored,
empty prefix disables everything) with plain rows. Gate tests
use hand-built rows through the existing `gate` seams (fake
`read_labels`, admission, quota, ledger): claim-held refused,
PR-held refused, own row excluded, `resume`/`fix`/`review` on
the claimed row bypass, no-area passes, `hold_until_merged`
false ignores pulls, and `None` pulls on other rows contribute
claims only. A held PR-holder plus a conflicting candidate
proves PR-held refusal through the extended fetch set, with a
fake listing seam asserting the holder row was fetched. An
in-pass test starts two same-area tickets in
one pass through the claiming loop with a fake store and
proves the second refuses with code `area` and mints no claim.
A `--plan` test proves claim-held refusals print
with the section 2 wording while PR-held ones never fire on
empty pulls. The journal test proves one refusal lands one
`area` count in `skipped_by_reason`. No new GitHub call exists
to fake: holder pulls arrive on rows from the listing the pass
already reads, and plans read none.

Migration is default-off. An empty prefix skips the helper, so
existing projects see byte-identical gating; a test pins that a
pass with the default resolves no areas. Enabling is one line
in the ops root `korapy.toml` plus labels on tickets: no store
migration, no resync, no exit-code change (a new skip code, the
same exit meanings). Docs freshness on implementation: the
settings table in `configuration.md` regenerates from the
registry, and the `drive` row in `commands.md` gains the `area`
reason wording; this doc stays the record.

## 6. Implementation tickets, in order

1. The pure helper plus the two settings (registry entries,
   per-project resolution, empty-prefix off, overlap table
   tests, label-over-board precedence test).
2. The gate check with `--plan` wording, journal code and
   snapshot counts (gate tests, extended holder fetch test,
   in-pass double-start test, plan test, journal test; the
   doctor warns when a project maps `area_column` and uses
   `area:` labels at once).
3. The reference-doc updates above plus a worked example with
   nested areas in the guide.

## Options:

1. Gate-held areas, release at merge (recommended)
2. Policy-side filtering, gate unchanged
3. Release at PR open, no hold option
4. Lock whole subtree on any child use
5. Do nothing: overlapping areas stay racy
