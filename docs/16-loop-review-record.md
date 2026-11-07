# 16: reviewers that end with prose and no record

Status: design, awaiting maintainer approval. No code ships with
this doc. It decides how the loop recovers when a reviewer
finishes with a prose verdict and never runs `korapy review
record`. Implementation waits for approval.

Conventions: module, function and file names quoted below were
read from the tree at design time (`drive.py`,
`reviewrecord.py`, `reviewcli.py`, `merge.py`, `takeoff.py`,
`launch.py`, `prompts/reviewer.md`, `runtimes/__init__.py`).
Behavior not yet implemented is marked PROPOSED; everything else
describes the tree as it stands.

## 1. The failure

A reviewer runs as its own process and proves its verdict by
writing a record (`reviewrecord.py`: JSON, one file per reviewed
commit, written by the reviewer process itself). Takeoff and the
merge path read the same record, so one proof serves both gates.
A prose verdict in the reviewer's final message proves nothing:
the loop cannot tell it from a missing record.

Observed: a reviewer session ended with `Verdict: approve, no
findings` but never ran `korapy review record` (the session's
`out.log` shows the command only inside the prompt text). The
next loop pass logged `reviewer finished without a review
record, review missing, skipped` (`drive.py`
`log_missing_reviews`), and the merge path reported `no
approving review record` for the head (`merge.py`
`_check_review`). The same shape recurred on two further
tickets. Result: the loop's own review step never produces a
mergeable ticket, so a coordinator has to re-review by hand.

Seen in the same run but out of scope here: `Failed to spawn:
ruff` from the sandbox, so the reviewer's format check could
not run. That is a runtime/sandbox matter, not a record
matter; it should be filed separately, not fixed by this
design.

After the skip the ticket is stuck, not merely delayed: the
gate keeps skipping it as `review-missing` until the worker
produces a new commit, which discards a review that already
happened and spends a full new review to replace it.

## 2. Options

(a) Prompt first. Move the record step to the top of
`prompts/reviewer.md` as a checklist item (record first, then
findings, then summary) instead of last. Nearly free, and it
lowers the incidence: a reviewer that stops early has already
seen the command. But it is still a request to the model, and
the observed failure is exactly a model ignoring the prompt.
It also recovers nothing: already-finished sessions stay
recordless, and the next pass spends a full new review.

(b) Resume once. When the unit ends without a record, the loop
resumes the reviewer session once with the exact record
command and waits; if there is still no record, today's skip
stands. Deterministic recovery at the cost of one cheap
resumed turn instead of one full review. Needs a durable
"already reprompted" mark per reviewer session (loop passes
are stateless across processes), and a pure decision helper
so the gate stays testable.

(c) Parse the prose. Require the reviewer to print a
structured verdict line and have the loop record it. Rejected:
it breaks the one load-bearing property of the record
(`reviewrecord.py`: a paragraph of free text proves nothing).
Prose is forgeable by anything that can write the session log,
parsing is brittle across runtimes (one runtime version at a
time, but prompt dialects still drift), and a loop-written
record destroys the reviewer/author separation the
self-review refusal exists to protect.

(d) PROPOSED, recommended: (a) plus (b). The prompt reorder
is prevention at negligible cost; the single resume is the
backstop that makes the observed failure class self-healing.
Neither changes what a record means or who writes it, so (c)
stays rejected and its reasons stay standing.

## 3. Recommended change (d): what moves

Prompt (`prompts.py`, `prompts/reviewer.md`): the record
command becomes checklist item one, with the same exact
command template, before the findings and summary steps. One
template, one place: the resume message in `drive.py` reuses
the prompt builder's command string, never a second copy
(AGENTS.md "One fact, one place"; a test pins the two
identical). The same builder also serves the fix hint in
`takeoff.py` `_check_review`, whose hand-written
`--verdict <approve|block>` has drifted from `VERDICTS`
(`reviewrecord.py`) and `prompts/reviewer.md`; the agreement
test pins all three spellings, so the hint cannot drift
again.

Loop (`drive.py`): PROPOSED pure helper
`needs_reprompt(finished, reviews, reprompted)` beside
`_review_missing`, sharing its inputs plus the reprompt mark;
PROPOSED planning step that turns a `review-missing` skip
with no mark into one resume request carrying the exact
command (commit pinned to the newest head read at plan time,
the same head `merge --plan` checks; author pinned per the
review launch rule below; verdict left to the reviewer). All
three head reads, the plan-time pin, the pre-resume
revalidation and the post-resume revalidation, go through
`mergeup.resolve_commit("HEAD", cwd)` in the ticket
checkout, the same resolver `reviewcli.py` uses for the
record's `--commit`; `drive.py` resolves no head today, so
the resume path adds no second resolver for the head fact.
The head is re-validated newest == pinned immediately
before the resume request and again after the resume
returns: when the head moved, no resume is spent, the skip
stands, and the ticket waits for a fresh review of the new
head, so the one resume never burns on a record that cannot
unblock the gate.
The resume goes through the existing `launch.resume` seam
with role reviewer (read-only preserved), so runtimes need no
new interface (`resume_handle` already exists per runtime).

Author: the exact source is the review launch rule
(`drive.py` claim path): `(released or {}).get(key)` first,
else `_finished_agent` of the `resolve_finished` newest
entry for the ticket, read through `_finished_agent`,
`_finished_role` and `_finished_outcome` only, no second
resolver. The resume-pass capture (`_capture_finished_holders`)
is usually empty, since no takeover precedes the resume, so
the fallback carries the case. Empty or mismatch means no
resume and the skip stands: no id at all, an unknown role or
outcome, or a fallback that still names the finished
reviewer itself (the two id flavors in `resolve_finished`:
cross-pass unit name versus capture drive id) would record a
self-review, which `write_record` refuses.

Mark: PROPOSED one empty marker file per reprompted reviewer
session inside that session's own leaf,
`os.path.join(paths.role_session_dir(repo, number,
"reviewer", gen), ".reprompted")` with `gen` the reprompted
reviewer session's generation, resolved only through `paths`
like every other session path. A file directly under the
ticket root is refused: `paths._legacy_session` reads any
direct child file as a legacy generation-0 session, so every
reprompted ticket would gain a phantom
`Session(ticket-root, worker-default, 0)` from
`ticket_sessions`, and `launch._leaves` would union that
phantom worker leaf into stop and close-previous. Inside the
leaf the mark changes neither half of `ticket_sessions`:
`_legacy_session` lists only the ticket root's direct
children, and `_role_leaves` still sees the same
`<role>-<gen>` dir with its prompt; `latest_record` scans
only `reviews/`, so it never sees the mark. The mark leaves
with the leaf under the existing session gc proofs, and a
resume preserves it (the prompt and config are rewritten in
place), so the post-resume pass still reads "already
reprompted" and never spends a second resume. The mark is
written before the resume is requested; a crash between mark
and spawn reads as "already reprompted" and fails closed to
today's skip. A mark that cannot be written also fails
closed: the skip stands, no resume is requested, the pass
never crashes and never reprompts without a mark.

## 4. Exit codes, settings, failing closed

Exit codes: none change. The resumed reviewer exits through
the existing unit outcome (`ok` or not); the gate's skip
verdicts and `review show`/`merge --plan` codes (`0`
approving, `1` missing or block, `3` unreachable, `4` usage)
are untouched. A reprompt that still yields no record is
indistinguishable from today: `review-missing` skip,
once-per-pass log line, merge gate fails its review check.

Settings: none added in version one. Exactly one resume per
reviewer session is fixed loop behavior, like the existing
takeover rule, not operator tuning. If operators later need a
kill switch, that is a separate ticket with its own default
(`drive.fix_round_threshold` agreement pattern applies).

Failing closed, in order: resume refused (no session, held
ticket, runtime refuses the handle) leaves the mark set and
the skip standing, never a forged record; a record appearing
mid-resume from the original session still wins by
create-exclusive write (`write_record`: first record wins, a
retry with the same reviewer and verdict is a no-op); a
self-reviewing resume is refused by `write_record` as today;
the mark lives inside the declared session root, so gc's
proofs still cover it.

## 5. GitHub calls per pass

None added. The record check reads local session dirs, the
resume spawns a local unit through the existing launch seam,
and the prompt reorder is text. No tracker or forge calls:
`review-missing` detection, the reprompt decision and the
resume all stay behind the current seams.

## 6. Test plan (fakes at the existing seams)

Pure core first: `needs_reprompt` over plain data (finished
entry, review map, mark set) covering reviewer-ok with no
record and no mark (true), mark present (false), non-`ok`
outcome (false, never a protocol failure), record present
(false), worker role (false). Agreement test: the resume
message's command string, the prompt template's, and the
`_check_review` fix hint all equal the one builder's output
for `VERDICTS`, so the three cannot drift.

Drive plan tests with a fake `launch.resume` at the existing
seam: missing-record pass requests exactly one resume with
the exact command (commit and author pinned); second pass
with the mark requests none; head moved between plan and
resume requests none and keeps the skip for a fresh review;
resume success plus a record written by the fake reviewer
clears the skip; resume refusal keeps the skip and the log
line; an unwritable mark keeps the skip with no resume and
no failed pass. Author cases request no resume and keep the
skip: no finished id at all, an unknown role or outcome, and
a fallback that still names the finished reviewer itself.
The mid-resume race lands the original session's record
during the resume and resolves by create-exclusive
first-wins (`write_record`: a retry with the same reviewer
and verdict is a no-op, anything else is refused), so the
gate reads the winner and never a duplicate. Mark placement
tests use a fake sessions root: with `.reprompted` inside
the reviewer leaf, `paths._legacy_session` stays false and
`paths.ticket_sessions` returns the same leaves as without
the mark. Runtime side: fake `resume_handle` per
runtime, reviewer stays read-only after resume (existing
launch guarantee). No test touches the network, a real
session dir, or a live unit; the suite guard keeps it
honest.

## 7. Tickets to file on approval

One to implement this design (prompt reorder, mark,
`needs_reprompt`, plan wiring, tests); one for the sandbox
`ruff` spawn failure seen in the same run, against the
runtime, not the loop.

## Options:

1. Checklist-first prompt plus one resume (recommended)
2. Prompt-first checklist only, no loop change
3. One loop resume only, prompt unchanged
4. Loop parses a structured verdict line
5. Leave the skip; coordinator re-reviews by hand
