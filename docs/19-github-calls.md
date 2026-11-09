# 19: one place for every GitHub call

Status: design, awaiting maintainer approval. No code ships with
this doc. It decides how every `gh` call on the host goes through
one paced, retrying, cached path, so coordinator scripts and ship
pipelines stop bypassing the bucket the agents already share.
Implementation waits for approval and follows the tickets in
section 6.

Conventions: module, function and file names quoted below were
read from the tree at design time (`github/gh.py`,
`github/ghshim.py`, `github/gh_shim.py`, `github/forge.py`,
`github/tracker.py`, `forge.py`, `tracker.py`, `paths.py`,
`githubcli.py`, `github/ghcli.py`, `mergecli.py`). Behavior not yet implemented is
marked PROPOSED; everything else describes the tree as it stands.

## 0. Scope and non-goals

The problem: the paced `gh` shim and the token bucket (section 8
of `00-design.md`, `github/gh_shim.py`) cover dispatched agents
only. Anything else that runs `gh` directly (coordinator scripts,
ship pipelines, ad hoc calls) bypasses the bucket and the shared
cooldown. The token is also shared with other tools and fleets on
the host, which the bucket cannot see. When the GraphQL budget
runs out, a ship that already pushed and opened its PR fails at
the merge step and needs a human to notice and re-run it.

This design adds, in order: a retry that sleeps until the reset
time GitHub reports (section 1), forge and tracker verbs for what
the scripts do today through raw `gh` (section 2), a small
time-to-live read cache (section 3), and a batching rule that
waits for measurement (section 4). It also states how the bucket
learns about spending it did not make, and how many GitHub calls
each verb adds per pass (section 5).

Non-goals: a second bucket (the verbs reuse the one
`GraphqlPacer` file and its cooldown), a second path or settings
resolver (every path goes through `paths`, one fact one place),
GraphQL for reads (REST, except the review-threads read that only
the merge gate needs and that stays GraphQL-only), a general
comment or label editor (the verbs expose exactly the
tracker and forge interfaces, no more), and any change to what
the merge gate decides on (section 3).

## 1. Retry that waits

Today a rate-limit answer is met with a guess:
`retry_after_s` parses seconds out of the message text and falls
back to a 60 second cooldown, and `TrackerRateLimited` carries
that guess to the caller, which stops the pass. The reset time
GitHub reports is never read.

PROPOSED: one retry helper beside the `gh` seam, for example
`github/retry.py` with a function shaped like
`run_with_reset_wait(run, args, *, budget_s, now, sleep) -> str`.
On a rate-limit answer (the existing `is_rate_limited`
classifier, unchanged) it reads `GET rate_limit` once, takes the
reset epoch for the tripped resource (`resources.core.reset`,
`resources.graphql.reset`) as the authority, sleeps until reset
plus a small margin, and retries the same call. The pure part
(should this answer retry, and until when) stays separate from
the sleep seam, so tests pin it with a fake clock and no
process ever sleeps in a test.

The wait is bounded. PROPOSED: one setting
`github.retry_max_wait_s`, default 1800 seconds, resolved like
every other setting and read in exactly one place. Sleep plus
calls count against it. Past the bound the verb gives up with
exit 3 (GitHub unreachable), and the message names the reset
time, for example `rate limited until 2026-01-02T03:04:05Z;
giving up after 1800s`. A give-up is never a bare exit 1: exit 1
keeps meaning a gate said no, and scripts branch on the
difference. Success after a wait is exit 0, with the waited
seconds on stderr, so a ship that rode out a limit reads as a
slow success, not a failure a human must re-run.

Safe to retry, because repeating the call converges:

- every idempotent read (all GETs: PR state, issue labels,
  comments listing, label listing, canonical name, rate limit);
- merge-if-not-merged (below): the pre-read pin makes a repeated
  PUT converge instead of doubling the merge;
- label add of a label already present, and label remove of a
  label already absent (the adapter already reads the 404 remove
  as done; the add path treats an already-exists answer as kept
  once the label reads back, as `ensure_labels` does today).

Not safe to retry blindly:

- comment post: a repeated POST writes twice. A rate-limit
  answer means the POST was rejected and never landed, so the
  comment verb retries those answers bare; re-execution after
  a timeout (the first POST may have landed) still requires
  the `--expect` list-before-post guard (section 2), which
  makes that retry converge;
- repo create: a repeated create loses the same race twice.
  `ForgeExists` stays the caller's signal, not a retry;
- the merge PUT without the pre-read pin: an unverified merge
  (the PUT answered but the verify read failed) must re-verify,
  never re-PUT.

PROPOSED merge-if-not-merged: `GithubForge.merge` keeps its
pre-read, and when the pre-read reports the PR merged with the
pinned head still in place (head SHA unchanged since the pin),
it returns the recorded merge SHA with exit 0 instead of
raising `ForgeRefused`. For a squash that tie is the merged
state plus the recorded SHA on the unchanged head; for a merge
commit it additionally checks the recorded commit's parents
include the pinned head, reusing `_check_merge_parents`. A
merged PR whose head moved past the pin stays `ForgeRefused`:
someone else landed other work, and the caller must look, not
retry. A merged state with no recorded merge SHA (`merge_sha
None`) fails closed: the verb refuses (or re-verifies live)
instead of returning a SHA it cannot name.

The helper serves both new verbs and the existing adapters: the
verbs call it around their one `run`, and the adapters call it
around theirs, so an in-process caller (the drive pass, `korapy
merge`) waits the same way a script does. The shim keeps pacing
agent calls as today and never waits until reset: it still naps
through `acquire`, but its best-effort contract
(a broken bucket must never break `gh`) is unchanged.

## 2. Verbs for what the scripts do

Only the GitHub adapters call `gh` (AGENTS.md). Coordinator
scripts and ship pipelines therefore get verbs instead of raw
`gh`, each a thin CLI over the existing interface methods, each
paced through `paced_run`/`graphql` on the shared bucket with
the shared cooldown. Reads prefer REST; the only GraphQL read
stays the review-threads one inside the merge gate, which gets
no verb. `docs/commands.md` gains the rows in the implementing
PRs, not in this doc.

PROPOSED: the verbs live in `githubcli.py`, in the existing
`korapy github` group beside `setup`. The top-level
`korapy gh-calls` summary (`github/ghcli.py`, wired in `cli.py`)
stays where it is; the new verbs do not join it, so
`docs/commands.md` rows land in the `github` section:

| Verb | Interface method | Exit codes | Errors |
| --- | --- | --- | --- |
| `korapy github pr-state REPO N [--json]` | `Forge.pull_request` | 0 with the state; 3 unreachable (a give-up names the reset, section 1); 4 usage | `ForgeError` to 3; a missing PR to 1, never 3 (the provider answered) |
| `korapy github pr-merge REPO N --head SHA [--method squash\|merge]` | `Forge.merge` | 0 merged, printing the SHA (`already merged <sha>` when merge-if-not-merged fires); 1 refused; 2 another merger holds the repo lock; 3 unreachable | `ForgeRefused` to 1; `ForgeError` to 3 |
| `korapy github issue-labels REPO N [--add L \| --remove L]` | read: `Tracker.item` labels; write: `Tracker.add_label` / `Tracker.remove_label` | 0; 1 rejected or refused; 3 unreachable; 4 usage (a name outside the loop-owned set, a `ValueError` today) | `ValueError` to 4; `TrackerRejected` to 1; `TrackerError` to 3 |
| `korapy github comment REPO N (--body T \| --body-file F) [--expect LINE]` | `Tracker.comments` then `Tracker.comment` | 0 posted, or `already posted` when `--expect` names a line the listing already holds; 3 unreachable; 4 usage | `TrackerError` to 3; an `--expect` hit is 0, never 1 |
| `korapy github rate-limit [--json]` | `GET rate_limit` | 0 with remaining and reset per resource; 3 unreachable; never a refusal | any adapter failure to 3; never 1 |

Notes on the table. The read half of `issue-labels` is the
cached read of section 3; the write half invalidates it. The
comment verb always lists live (comments are not cached): a
retry past a rate-limit answer needs no guard (the POST never
landed), while a re-run after a timeout converges through
`--expect`, since ship pipelines pass the decision line they
post and find their own line instead of posting twice.
`pr-merge` takes the same one-merger-per-repo lock `korapy
merge` takes, so two ships racing one PR resolve to 0 plus 2,
never to a double merge. Every verb retries rate-limit answers
through section 1 within the same bound; the bound, not the
verb, decides when waiting stops.

## 3. A small read cache with a time-to-live

Some reads are polled far more often than the underlying facts
change: PR state while a ship waits for checks, issue labels
while a coordinator scans a queue, the rate-limit reading on
every retry. Each such read costs a call today.

PROPOSED: a host-shared read cache for exactly three reads: PR
state (`Forge.pull_request`), issue labels (`Tracker.item`
label names), and the rate-limit reading. One helper beside the
adapters, for example `github/readcache.py`, over plain files;
one path resolver in `paths.py`, for example
`github_cache_dir()` under `cache_dir()`, so the location is one
fact in one place. A cache entry is one JSON file,
`{"fetched_at": <epoch>, "ttl_s": <n>, "payload": {...}}`.
An unreadable or corrupt entry reads as a miss, and a cache
write never fails the verb: the cache fails open, the verbs fail
closed.

Keys keep tenants apart. The file path is
`<github_cache>/<login>/<owner>/<repo>/<verb>-<args>.json`,
where `login` is the forge's `current_user` (so two accounts on
one host never share entries), `owner/repo` is the
`Tracker.canonical_repo` spelling (`src/korapy/tracker.py`; so
two repositories never share entries), and `args` is the
canonical verb arguments (number, label set requested).
`current_user` itself costs one call per process: it is memoized
in-process and backed by a `self.json` entry with its own short
TTL, so the common case costs nothing. The `self.json` file is
keyed by a hash of the token, hash only, so two accounts never
share it. No token material ever enters a key or a file; keys
and entries carry account names and hashes, never secrets.

PROPOSED TTLs, as constants beside the helper (settings only if
a fleet proves a kind needs tuning): PR state 60 seconds, issue
labels 300 seconds, rate-limit 60 seconds. The merge gate and
every other deciding path keep reading live: the cache serves
polling scripts and queue scans, never a merge precondition, a
claim, or a takeoff row. A design that cached a decision read
would trade a stale merge for a saved call; this one does not.

Invalidation is by the verbs that write, deleting the keys they
stale: `pr-merge` drops that PR's state entry after a merge;
`issue-labels --add/--remove` drops that item's labels entry;
`ensure_labels` (setup path) drops the repo's label entries.
Comment posts invalidate nothing, because comments are not
cached. A delete that finds no file succeeds: invalidation is
idempotent like the writes. A delete racing a read-through
repopulation only costs freshness within the TTL: the stale
entry expires on its own, so the bound on a wrong read stays
one TTL.

## 4. Batching, later

No batching ships with this design. The call log already records
every paced call (`gh-calls.jsonl`: caller, shape, throttled
flag), and `korapy gh-calls --by-shape` summarizes it, so the
measurement is cheap today: run the pass that feels chatty,
group `--by-shape` output by purpose, and count how many small
reads serve one purpose. Batching becomes a follow-up only when
such a measured pass shows many small reads for one purpose with
no existing single call that answers it (the pulls index already
batches the per-ticket pull question, for example). The follow-up
then proposes one batched read behind the same interfaces, with
its own call count, and this doc gains a section. Until that
measurement exists, N reads for N tickets stay N calls.

## 5. The bucket learns, and what each verb costs

Today the bucket only sees what it meters: it starts full, it
drains on paced calls, and anything else on the host (another
fleet's raw `gh`, a second account's tooling, an operator's ad
hoc call) spends invisibly until a rate-limit answer lands a
cooldown. The bucket's own tally then reads fuller than the
account is.

PROPOSED: the reported remaining count is the authority, and it
reconciles downward only. Paced REST calls run through
`gh api -i`, and the seam parses `x-ratelimit-remaining` and
`x-ratelimit-reset` out of the headers before splitting the
body; the retry path's `rate_limit` read carries both
resources. The pacer (same state file, same lock) folds them in
with a function shaped like `note_reported(resource, remaining,
reset_epoch)`: tokens drop to the reported remaining mapped
onto bucket units when that is lower than the tally, never rise
to it when higher (a stale high read must not grant a burst),
and a zero remaining pushes the cooldown to the reported reset.
One resource's report never touches the other resource's
balance: core and graphql keep separate sub-balances in the
same file, because a REST burst must not drain GraphQL
headroom. Mapping detail (units per reported point) stays in
the implementing ticket; the rule here is only the direction:
down on report, up on refill, never up on report.

Per-pass call counts, so a reviewer can price a script. A cache
hit costs zero; misses cost what the adapter costs today:

- `pr-state`: 1 REST read.
- `pr-merge`: 3 calls for squash (pre-read, PUT, verify read),
  4 for a merge commit (plus the `GET commits/{sha}` parents
  read in `_check_merge_parents`); 1 for squash and 2 for a
  merge commit when merge-if-not-merged short-circuits on the
  pre-read (the merge-commit tie re-reads the parents).
- `issue-labels` read: 1 REST read; `--add` / `--remove`: 1
  write each, plus invalidation with no call.
- `comment`: the listing (usually 1 page) plus 1 post; 1 page
  and no post when `--expect` already holds.
- `rate-limit`: 1 REST read.
- each rate-limit event on the retry path: 1 `rate_limit` read
  plus the retried call.

No existing pass pays more until its scripts switch to the
verbs: the drive pass, the mirror pass and `korapy merge` keep
their current call patterns, and each script migration in the
implementing PRs states its own before/after count from the
`gh-calls` log.

## 6. Implementation tickets, in order

Retry first (it fixes the ship failure alone), then verbs, then
cache. `docs/commands.md` updates ride with the verb PRs.

1. Retry that waits: the `run_with_reset_wait` helper with
   injected clock and sleep, the `github.retry_max_wait_s`
   setting, merge-if-not-merged in `GithubForge.merge` with its
   head-moved refusal and its merged-at-pin-but-`merge_sha
   None` fail-closed refusal, exit 3 with the reset time in the
   message, the `gh api -i` header capture in the seam, and the
   downward-only `note_reported` reconciliation in the pacer.
   New per-resource keys are read with `.get` defaults matching
   a full bucket, so old `graphql-pacing.json` files (which
   lack them) never trip the corrupt-state backoff. Tests with
   fake `run`, clock and sleep: reset read from `rate_limit`
   not guessed, bound honored across sleep plus calls, give-up
   exits 3 naming the reset, safe verbs retried, bare comment
   retry on rate-limit answers but `--expect` required past a
   timeout, merged-at-pinned-head returns the SHA, `merge_sha
   None` at the pin refuses, moved head refuses, and an old
   state file without the new keys paces without backing off.
2. The five verbs in `githubcli.py` with the exit codes in the
   section 2 table, the repo merger lock on `pr-merge`, the
   `--expect` guard on `comment`, and the `commands.md` rows.
   Tests with fake forge/tracker seams: each exit code, the
   lock race resolving to 2, the already-posted short circuit,
   and a proof that no verb imports `gh` or builds a provider
   URL (only the adapters do).
3. The read cache: the `paths` resolver, the entry format and
   key layout, the three TTLs, invalidation in the writing
   verbs, and the live-read rule for deciding paths (a test
   proves the merge gate never consults the cache). Tests with
   a temp dir seam: hit within TTL costs no call, expiry
   re-reads, corrupt entry reads as miss, two repos and two
   logins never share a file, writes invalidate exactly their
   keys.
4. Follow-up only: batching, if a `gh-calls --by-shape`
   measurement shows many small reads for one purpose with no
   single call answering it.

## Options:

1. Retry in the adapters, verbs share it (recommended)
2. Verbs under korapy github, REST first (recommended)
3. Cache in cache dir, deciding paths live (recommended)
4. Batch now on assumed shapes
5. Do nothing: scripts keep raw gh
