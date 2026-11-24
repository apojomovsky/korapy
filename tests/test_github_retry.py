"""The reset-wait retry: fake run, fake clock, fake sleep only.

No test sleeps, calls `gh`, or touches the network: the run seam is a
script, `now` reads a counter the script and the fake sleep advance,
and every wait is recorded instead of taken.
"""

import contextlib
import io
import json
import unittest
from unittest import mock

from korapy import settings
from korapy.github import gh
from korapy.github import retry


class Clock:
    def __init__(self, start=1000.0):
        self.t = start
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


class ScriptedRun:
    """One outcome per call, in order; advances the clock per call."""

    def __init__(self, clock, outcomes, advance=0.0):
        self.clock = clock
        self.outcomes = list(outcomes)
        self.advance = advance
        self.calls = []

    def __call__(self, args):
        self.calls.append(list(args))
        self.clock.t += self.advance
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def rate_limit_payload(clock, *, core_in=300.0, graphql_in=900.0):
    return json.dumps(
        {
            "resources": {
                "core": {"limit": 5000, "remaining": 0, "reset": int(clock.t + core_in)},
                "graphql": {"limit": 5000, "remaining": 0, "reset": int(clock.t + graphql_in)},
            }
        }
    )


def limited(text="gh: API rate limit exceeded (HTTP 403), retry after 62"):
    return gh.GhError("unavailable", text)


class RunWithResetWaitTest(unittest.TestCase):
    def run_retry(self, run, clock, budget=1800.0, args=("api", "repos/acme/lib/pulls/7")):
        return retry.run_with_reset_wait(
            run, list(args), budget_s=budget, now=clock.now, sleep=clock.sleep
        )

    def test_success_needs_no_rate_limit_read_and_no_sleep(self):
        clock = Clock()
        run = ScriptedRun(clock, ["ok"])
        self.assertEqual(self.run_retry(run, clock), "ok")
        self.assertEqual(run.calls, [["api", "repos/acme/lib/pulls/7"]])
        self.assertEqual(clock.sleeps, [])

    def test_reset_comes_from_rate_limit_not_the_guessed_text(self):
        clock = Clock()
        run = ScriptedRun(clock, [limited(), rate_limit_payload(clock), "ok"])
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(self.run_retry(run, clock), "ok")
        # The message guessed 62s; the reported core reset is 300s out.
        self.assertEqual(clock.sleeps, [300.0 + retry.RESET_MARGIN_S])
        self.assertEqual(run.calls[1], ["api", "rate_limit"])
        self.assertEqual(len(run.calls), 3)
        self.assertIn(f"waited {300.0 + retry.RESET_MARGIN_S:g}s", err.getvalue())

    def test_graphql_answer_uses_the_graphql_reset(self):
        clock = Clock()
        run = ScriptedRun(
            clock,
            [limited("GraphQL: API rate limit exceeded"), rate_limit_payload(clock), "ok"],
        )
        self.run_retry(run, clock)
        self.assertEqual(clock.sleeps, [900.0 + retry.RESET_MARGIN_S])

    def test_each_rate_limit_event_reads_rate_limit_once(self):
        clock = Clock()
        run = ScriptedRun(
            clock,
            [limited(), rate_limit_payload(clock), limited(), rate_limit_payload(clock), "ok"],
        )
        self.run_retry(run, clock)
        self.assertEqual(
            [c for c in run.calls if c == ["api", "rate_limit"]],
            [["api", "rate_limit"], ["api", "rate_limit"]],
        )
        self.assertEqual(len(run.calls), 5)

    def test_give_up_past_the_budget_names_the_reset_and_never_sleeps(self):
        clock = Clock()
        run = ScriptedRun(clock, [limited(), rate_limit_payload(clock, core_in=3600.0)])
        with self.assertRaises(gh.GhError) as caught:
            self.run_retry(run, clock, budget=60.0)
        self.assertEqual(caught.exception.kind, "unavailable")
        reset_text = retry.reset_text(clock.t + 3600.0)
        self.assertIn(f"rate limited until {reset_text}", str(caught.exception))
        self.assertIn("giving up after 60s", str(caught.exception))
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(len(run.calls), 2)

    def test_the_bound_is_wall_clock_around_the_calls(self):
        # Each call (a pacer nap rides inside it) burns 80s of the 100s
        # budget, so a reset 300s out is already past the bound.
        clock = Clock()
        run = ScriptedRun(clock, [limited(), rate_limit_payload(clock)], advance=80.0)
        with self.assertRaises(gh.GhError) as caught:
            self.run_retry(run, clock, budget=100.0)
        self.assertEqual(caught.exception.kind, "unavailable")
        self.assertEqual(clock.sleeps, [])

    def test_a_wait_that_would_cross_the_deadline_gives_up(self):
        clock = Clock()
        run = ScriptedRun(clock, [limited(), rate_limit_payload(clock, core_in=90.0)])
        with self.assertRaises(gh.GhError):
            self.run_retry(run, clock, budget=50.0)
        self.assertEqual(clock.sleeps, [])

    def test_non_rate_limit_errors_are_not_retried(self):
        clock = Clock()
        boom = gh.GhError("other", "boom")
        run = ScriptedRun(clock, [boom])
        with self.assertRaises(gh.GhError) as caught:
            self.run_retry(run, clock)
        self.assertIs(caught.exception, boom)
        self.assertEqual(len(run.calls), 1)

    def test_a_timeout_is_not_retried_bare(self):
        # A timeout means the first call may have landed: the helper
        # retries rate-limit answers only, never a bare timeout.
        clock = Clock()
        run = ScriptedRun(clock, [gh.GhError("unavailable", "gh timed out after 30s")])
        with self.assertRaises(gh.GhError):
            self.run_retry(run, clock, args=("api", "--method", "POST", "x/comments"))
        self.assertEqual(len(run.calls), 1)
        self.assertEqual(clock.sleeps, [])

    def test_a_rate_limited_comment_post_retries_bare(self):
        # A rate-limit answer means the POST was rejected and never
        # landed, so the same POST retries without any guard.
        clock = Clock()
        run = ScriptedRun(clock, [limited(), rate_limit_payload(clock), "posted"])
        out = self.run_retry(
            run, clock, args=("api", "--method", "POST", "repos/acme/lib/issues/7/comments")
        )
        self.assertEqual(out, "posted")
        self.assertEqual(run.calls[0], run.calls[2])

    def test_an_unreadable_rate_limit_read_gives_up_without_guessing(self):
        clock = Clock()
        run = ScriptedRun(clock, [limited(), "not json"])
        with self.assertRaises(gh.GhError) as caught:
            self.run_retry(run, clock)
        self.assertEqual(caught.exception.kind, "unavailable")
        self.assertIn("giving up after 1800s", str(caught.exception))
        self.assertEqual(clock.sleeps, [])

    def test_a_secondary_limit_has_no_reset_to_wait_for(self):
        clock = Clock()
        run = ScriptedRun(
            clock, [limited("You have exceeded a secondary rate limit"), rate_limit_payload(clock)]
        )
        with self.assertRaises(gh.GhError) as caught:
            self.run_retry(run, clock)
        self.assertEqual(caught.exception.kind, "unavailable")
        self.assertEqual(clock.sleeps, [])

    def test_reset_in_the_past_retries_without_sleeping(self):
        clock = Clock()
        payload = json.dumps(
            {"resources": {"core": {"reset": int(clock.t - 5)}, "graphql": {"reset": 0}}}
        )
        run = ScriptedRun(clock, [limited(), payload, "ok"])
        self.assertEqual(self.run_retry(run, clock), "ok")
        self.assertEqual(clock.sleeps, [])

    def test_the_same_past_reset_twice_gives_up_instead_of_looping(self):
        clock = Clock()
        payload = json.dumps(
            {"resources": {"core": {"reset": int(clock.t - 5)}, "graphql": {"reset": 0}}}
        )
        run = ScriptedRun(clock, [limited(), payload, limited(), payload])
        with self.assertRaises(gh.GhError) as caught:
            self.run_retry(run, clock)
        self.assertEqual(caught.exception.kind, "unavailable")
        self.assertIn("giving up after 1800s", str(caught.exception))
        self.assertEqual(len(run.calls), 4)
        self.assertEqual(clock.sleeps, [])


class StubPacer:
    def __init__(self):
        self.acquired = []
        self.cooldowns = []

    def acquire(self, cost=gh.GRAPHQL_CALL_COST, resource=None):
        self.acquired.append(cost)

    def cooldown(self, seconds):
        self.cooldowns.append(seconds)


class PacedRunIntegrationTest(unittest.TestCase):
    def test_the_paced_seam_waits_and_retries_through_one_run(self):
        clock = Clock()
        run = ScriptedRun(clock, [limited(), rate_limit_payload(clock), "ok"])
        pacer = StubPacer()
        out = gh.paced_run_with_reset_wait(
            ["api", "repos/acme/lib/pulls/7"],
            run=lambda args, **kwargs: run(args),
            pacer=pacer,
            budget_s=1800.0,
            now=clock.now,
            sleep=clock.sleep,
        )
        self.assertEqual(out, "ok")
        # Every attempt, the rate_limit read included, ran paced.
        self.assertEqual(len(pacer.acquired), 3)
        self.assertEqual(clock.sleeps, [300.0 + retry.RESET_MARGIN_S])
        # The guessed 62s cooldown never lands: the reported reset is
        # the only wait a retry-managed call takes.
        self.assertEqual(pacer.cooldowns, [])

    def test_a_configured_budget_flows_through_the_paced_seam(self):
        clock = Clock()
        run = ScriptedRun(clock, [limited(), rate_limit_payload(clock, core_in=3600.0)])
        with mock.patch.object(retry, "retry_max_wait_s", return_value=50):
            with self.assertRaises(gh.GhError) as caught:
                gh.paced_run_with_reset_wait(
                    ["api", "repos/acme/lib/pulls/7"],
                    run=lambda args, **kwargs: run(args),
                    pacer=StubPacer(),
                    now=clock.now,
                    sleep=clock.sleep,
                )
        self.assertIn("giving up after 50s", str(caught.exception))
        self.assertEqual(clock.sleeps, [])


class PureResetTest(unittest.TestCase):
    def test_reset_epoch_maps_rest_to_core_and_graphql_to_graphql(self):
        payload = {"resources": {"core": {"reset": 111}, "graphql": {"reset": 222}}}
        self.assertEqual(retry.reset_epoch(payload, "rest"), 111.0)
        self.assertEqual(retry.reset_epoch(payload, "core"), 111.0)
        self.assertEqual(retry.reset_epoch(payload, "graphql"), 222.0)
        self.assertIsNone(retry.reset_epoch(payload, "secondary"))
        self.assertIsNone(retry.reset_epoch(payload, None))
        self.assertIsNone(retry.reset_epoch({"resources": {}}, "rest"))
        self.assertIsNone(retry.reset_epoch([], "rest"))

    def test_reset_text_is_the_give_up_spelling(self):
        self.assertEqual(retry.reset_text(1767225600.0), "2026-01-01T00:00:00Z")


class SettingTest(unittest.TestCase):
    def test_default_and_the_one_reader(self):
        self.assertEqual(settings.REGISTRY["github.retry_max_wait_s"].default, 1800)
        empty = settings.resolve(env={})
        with mock.patch.object(settings, "load", return_value=empty):
            self.assertEqual(retry.retry_max_wait_s(), 1800)
        resolved = settings.resolve(project={"github": {"retry_max_wait_s": 123}}, env={})
        self.assertEqual(retry.retry_max_wait_s(resolved), 123)
        with mock.patch.object(settings, "load", return_value=resolved):
            self.assertEqual(retry.retry_max_wait_s(), 123)

    def test_provider_is_registered(self):
        self.assertIn("korapy.github.retry", settings.PROVIDERS)

    def test_negative_wait_is_a_settings_error(self):
        with self.assertRaises(settings.SettingsError):
            settings.resolve(project={"github": {"retry_max_wait_s": -1}}, env={})


if __name__ == "__main__":
    unittest.main()
