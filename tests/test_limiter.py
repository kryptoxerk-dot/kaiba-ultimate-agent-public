"""The rate limiter's budget arithmetic.

These cover one class of bug: a budget that is wrong in a way that looks like it is
working. A weight that resolves to the default charges a swap as if it were a token
lookup; a capacity below the cost of the call reports a transient limit that never clears.
Both were live, both are pinned here.
"""

from __future__ import annotations

import pytest

# ------------------------------------------- a bucket smaller than the call is a deadlock


def test_a_call_costing_more_than_the_whole_budget_is_named_not_deferred(tmp_db):
    """"Retry in 25s" is a lie when the bucket can never hold the call."""
    from kaiba.core import limiter

    limiter.DEFAULTS["testprov"] = limiter.Limits(
        min_interval_ms=0, capacity=5, refill_per_s=1.0, max_inflight=4,
        weights={"swap": 10, "default": 1},
    )
    try:
        with pytest.raises(limiter.RateLimited) as exc:
            limiter.reserve("testprov", "trade.swap", conn=tmp_db)
        msg = str(exc.value)
        assert "costs 10" in msg and "caps at 5" in msg
        assert "capacity" in msg  # tells the operator what to change
        assert "bucket exhausted" not in msg
    finally:
        limiter.DEFAULTS.pop("testprov", None)


def test_the_shipped_gmgn_budget_can_actually_hold_a_swap(tmp_db):
    """Setting capacity to the Free allowance made every live entry and exit impossible."""
    from kaiba.core.limiter import limits_for

    lim = limits_for("gmgn")
    assert lim.weight_for("trade.swap") <= lim.capacity
    assert lim.weight_for("quote") <= lim.capacity


def test_the_executors_endpoint_string_charges_the_real_swap_weight(tmp_db):
    """The weight table is keyed `swap`; the executor reserves `trade.swap`."""
    from kaiba.core.limiter import limits_for

    lim = limits_for("gmgn")
    assert lim.weight_for("trade.swap") == 10
    assert lim.weight_for("quote") == 10
    assert lim.weight_for("trade.quote") == 10
    assert lim.weight_for("token.info") == 1
