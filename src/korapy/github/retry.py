"""Retry that waits for the reset GitHub reports (docs/19, section 1).

One helper beside the `gh` seam: on a rate-limit answer it reads
`GET rate_limit` once, sleeps until the reported reset for the tripped
resource plus a small margin, and retries the same call. The wait is
bounded by `github.retry_max_wait_s`; past the bound it gives up with
`GhError("unavailable")` (exit 3) naming the reset time, never with a
bare exit 1. Only rate-limit answers retry: a rate-limited call was
rejected and never landed, so repeating it converges (a comment POST
included). Any other failure, a timeout included, raises at once: the
first call may have landed, and a re-run after one converges only
through a caller-side guard (the `--expect` list-before-post of
docs/19 section 2), never through a bare retry here.

The pure part (which reset, until when, the give-up text) is separate
from the injected `now` and `sleep`, so tests pin it with a fake clock
and no process ever sleeps in a test.
"""

import datetime
import json
import math
import os
import sys

from korapy import settings
from korapy.github.gh import (
    GhError,
    _PACING_NAP_S,
    is_rate_limited,
    rate_limit_kind,
    stop_requested,
)

#: Seconds past the reported reset before the retried call runs: the
#: provider's clock and ours may disagree by a little.
RESET_MARGIN_S = 5.0

#: The one setting for the bound, and its built-in default.
RETRY_MAX_WAIT_SETTING = "github.retry_max_wait_s"
DEFAULT_RETRY_MAX_WAIT_S = 1800

#: The `rate_limit` resources each tripped kind reads its reset from.
#: A secondary answer carries no usable reset in that payload, so it
#: maps to nothing and the helper fails closed instead of guessing.
_RESOURCE_BY_KIND = {"rest": "core", "core": "core", "graphql": "graphql"}


def _check_max_wait(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("must be an integer of 0 or more seconds")
    return value


settings.register(
    RETRY_MAX_WAIT_SETTING,
    int,
    DEFAULT_RETRY_MAX_WAIT_S,
    "seconds a GitHub call may wait out a rate limit for its reset before giving up (exit 3)",
    check=_check_max_wait,
)


def retry_max_wait_s(resolved=None) -> int:
    """The effective bound: the one reader of `github.retry_max_wait_s`.

    `resolved` is a settings `Resolved` when the caller already has
    one. Without one the value is resolved live through the settings
    layers (host config, the ops root above the working directory,
    the environment), like every other setting, so a configured bound
    reaches every paced caller and not only the ones that happen to
    carry a `Resolved`. A settings file that cannot be read falls
    back to the built-in default: the seam must never break a `gh`
    call over a settings read, and the caller's own settings load
    reports the same file's error at its own gate.
    """
    if resolved is None:
        try:
            resolved = settings.load(root=settings.find_root(os.getcwd()))
        except settings.SettingsError:
            return DEFAULT_RETRY_MAX_WAIT_S
    return int(resolved[RETRY_MAX_WAIT_SETTING])


def reset_epoch(payload, kind) -> float | None:
    """The reset epoch for the tripped `kind` in a `rate_limit` payload.

    `kind` is `rate_limit_kind`'s word ("rest", "graphql", "secondary").
    None when the payload names no usable reset for it (a secondary
    limit, a missing resource, a malformed value): never a guess.
    """
    resource = _RESOURCE_BY_KIND.get(kind or "")
    if resource is None or not isinstance(payload, dict):
        return None
    resources = payload.get("resources")
    entry = resources.get(resource) if isinstance(resources, dict) else None
    reset = entry.get("reset") if isinstance(entry, dict) else None
    if isinstance(reset, bool) or not isinstance(reset, (int, float)):
        return None
    try:
        value = float(reset)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return value


def reset_text(epoch: float) -> str:
    """One reset epoch as the give-up message spells it (UTC, second).

    Total: an epoch outside the clock range reads as an unread reset
    instead of raising out of the give-up path.
    """
    try:
        moment = datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return "an unread reset"
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _give_up(budget_s: float, reset: float | None, detail: str = "") -> GhError:
    when = reset_text(reset) if reset is not None else "an unread reset"
    suffix = f" ({detail})" if detail else ""
    return GhError(
        "unavailable",
        f"rate limited until {when}; giving up after {budget_s:g}s{suffix}",
    )


def run_with_reset_wait(run, args, *, budget_s, now, sleep, stop=None) -> str:
    """Run `run(args)`, waiting out rate limits until `budget_s` is spent.

    `run` returns stdout and raises on failure, like the `gh` seam.
    `budget_s` is a wall-clock bound around every call and sleep,
    measured through the injected `now`, so pacer naps inside `run`
    count against it too. The reset wait runs in `_PACING_NAP_S`-sized
    naps through the injected `sleep`, and stops at the stop flag
    (`stop`, defaulting to the `gh` seam's flag) the way pacer waits
    do, so a stop during a long reset wait ends within one nap. A
    success after waiting reports the waited seconds on stderr; the
    return is the successful call's stdout.
    """
    stopped = stop if stop is not None else stop_requested
    start = now()
    waited = 0.0
    past_reset: float | None = None
    while True:
        try:
            out = run(args)
        except Exception as err:
            text = str(err)
            if not is_rate_limited(text):
                raise
            kind = rate_limit_kind(text)
            try:
                payload = json.loads(run(["api", "rate_limit"]))
            except Exception as read_err:
                raise _give_up(budget_s, None, f"reset read failed: {read_err}") from err
            reset = reset_epoch(payload, kind)
            if reset is None:
                raise _give_up(budget_s, None, f"no usable reset for a {kind} limit") from err
            current = now()
            wake = reset + RESET_MARGIN_S
            if wake <= current:
                # The reset already passed: retry at once, unless the
                # calls themselves already spent the bound. The same
                # past reset twice means our clock disagrees with the
                # provider's (it is still limiting): give up instead
                # of tight-looping until the bound expires.
                if current - start >= budget_s or reset == past_reset:
                    raise _give_up(budget_s, reset) from err
                past_reset = reset
                continue
            if wake > start + budget_s:
                raise _give_up(budget_s, reset) from err
            while True:
                delay = wake - now()
                if delay <= 0:
                    break
                if stopped():
                    raise GhError(
                        "unavailable",
                        "stop requested while waiting for the GitHub rate limit reset; retry later",
                    ) from err
                nap = min(delay, _PACING_NAP_S)
                sleep(nap)
                waited += nap
            continue
        if waited:
            print(f"waited {waited:g}s for the GitHub rate limit reset", file=sys.stderr)
        return out
