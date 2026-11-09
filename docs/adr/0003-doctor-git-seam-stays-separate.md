# 0003: the doctor keeps its own git runner

Status: accepted (kept separate in ticket 712, recorded for ticket 709).

## Context

`syncli.tracker_repo` (ticket 702) is the one resolver for which
tracker repo a `[[repo]]` entry feeds. Sync and the doctor both
resolve through it, so both need a git seam for the origin lookup.
The sync side already had one: `syncli._git` feeds sync, mirror,
launch and drive. The report side already had its own:
`doctor.production_git` feeds every git read in the live report
(board sites, repo sites, default-branch lookups).

Two runners for one `git` binary look like a second resolver for one
fact. But the two consumers honor different failure contracts: the
report never raises (a failed read degrades to a warn row naming the
site), while sync returns a problem naming the entry. One shared
runner would mean one fake shape and one failure contract across two
test suites that assert different degradation.

## Decision

The doctor keeps `production_git` separate from `syncli._git`. Each
consumer owns its seam and its fake. The sameness of the two bodies
today (both `subprocess.run(["git", ...])` with `clean_env` and a
30 second timeout, None when git cannot run) is coincidence, not a
contract: either side may change timeouts or degradation without
auditing the other consumer's tests.

## Consequences

- `doctor.py` wires `production_git` into `load_board_sites` and
  `load_repo_sites` only; every other doctor git read takes the
  injected seam. `syncli._git` stays the default for sync, mirror,
  launch and drive.
- A change to one runner's behavior needs no audit of the other
  consumer's tests.
- A new consumer that needs git gets its own seam with its own fake,
  not a share of one of these two.
