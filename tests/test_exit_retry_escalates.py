"""An exit that has failed the same way thirty times must stop retrying every minute.

THE BUG, MEASURED on the live box 2026-09-23. ``0xc777f87...`` rugged; every sell since
11:20 has been refused by the venue. The backoff is
``min(RETRY_MAX_MS, RETRY_BASE_MS * 2 ** (attempts - 1))``, so it reaches the 60 s ceiling
on the fifth attempt and then retries once a minute forever:

    33 failed sells in 33 minutes -- and 33 of the 50 orders placed in that hour, 66% of
    the ENTIRE order pipeline, were that one position.

The rate limiter those orders drain is the same bucket the price reads come from, so one
unsellable token was a direct cause of `pons_factory_read_failed`, blind positions and
`no_viable_band` entry refusals on every chain. A stuck exit does not just fail to sell;
it takes protection down with it.

THE FIX. The 60 s cap stays for the first :data:`RETRY_ESCALATE_AFTER` attempts, because
a venue blip, a 429 or a dropped connection all deserve a fast retry. After that the cap
lifts toward :data:`RETRY_CEILING_MS`: thirty consecutive refusals is a standing condition,
and attempt 31 at the same cadence has no better prospect than attempt 30 had.

WHAT THIS MUST NOT DO is slow down a real stop on a healthy token. Eight attempts spans
roughly five minutes of fast retries before anything escalates, and ``exit_attempts`` is
reset to 0 by any accepted submit, so only CONSECUTIVE failures on one position count.
"""

from __future__ import annotations

import pytest

from kaiba.execution import watchdog as wd


def test_a_blip_still_retries_fast():
    """First attempts keep the old behaviour: seconds, not minutes."""
    assert wd._exit_backoff(1) == wd.RETRY_BASE_MS
    assert wd._exit_backoff(2) == wd.RETRY_BASE_MS * 2
    assert wd._exit_backoff(3) == wd.RETRY_BASE_MS * 4


def test_it_still_caps_at_a_minute_early_on():
    for attempts in range(4, wd.RETRY_ESCALATE_AFTER + 1):
        assert wd._exit_backoff(attempts) <= wd.RETRY_MAX_MS


def test_a_standing_refusal_escalates_past_the_minute():
    """THE REGRESSION: 33 attempts at 60 s each, 66% of the order pipeline."""
    assert wd._exit_backoff(wd.RETRY_ESCALATE_AFTER + 1) > wd.RETRY_MAX_MS


def test_the_escalation_is_bounded():
    for attempts in (20, 50, 500):
        assert wd._exit_backoff(attempts) <= wd.RETRY_CEILING_MS


def test_the_stuck_position_would_now_be_rare():
    """At 33 consecutive failures the retry is at the ceiling, not once a minute."""
    assert wd._exit_backoff(33) == wd.RETRY_CEILING_MS
    per_hour = 3_600_000 / wd._exit_backoff(33)
    assert per_hour <= 4, f"{per_hour:.0f} retries/hour is still a storm"


def test_backoff_never_decreases():
    prev = 0
    for attempts in range(1, 40):
        got = wd._exit_backoff(attempts)
        assert got >= prev, f"backoff went down at attempt {attempts}"
        prev = got


@pytest.mark.parametrize("attempts", [0, -1, -99])
def test_a_nonsense_attempt_count_is_safe(attempts):
    got = wd._exit_backoff(attempts)
    assert wd.RETRY_BASE_MS <= got <= wd.RETRY_CEILING_MS


def test_the_ceiling_is_sane():
    assert wd.RETRY_MAX_MS < wd.RETRY_CEILING_MS <= 3_600_000, (
        "a ceiling over an hour stops being a backoff and becomes an abandonment"
    )


def test_the_watchdog_uses_the_helper():
    import inspect

    source = inspect.getsource(wd)
    assert "_exit_backoff(" in source
    assert "min(RETRY_MAX_MS, RETRY_BASE_MS" not in source, (
        "the flat 60 s cap is back; one stuck position will take the pipeline again"
    )
