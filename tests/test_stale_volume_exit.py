"""A token nobody is trading is not a position, it is a write-off waiting to be admitted.

THE GAP, raised by the operator 2026-09-23 and confirmed on the live box: there is no
stale-volume exit for a LIVE position. MEASURED at the time, 10 of 12 open positions had
ZERO swaps in the preceding hour, one had not traded for 22.5 hours, and one sat at +15%
unrealised that could not be realised because nothing was trading.

MEASURED on 135 live-traded tokens, what the NEXT print does after a silence, and how often
there is never a next print at all:

    silence   next print up   median move   token never trades again
      15m          44%           -0.4%               93%
      30m          44%           -0.5%               93%
      60m          42%           -3.1%               88%
     120m          38%           -6.0%               80%
     240m          35%          -11.0%               70%

Both columns move the same way: the longer the silence, the worse the resumption, and the
less likely there is one. At an hour the median resumption is -3.1% and 88% of tokens are
already dead. That is the measurement behind the 3600 s default.

THE TAIL IS REAL AND IS NOT IGNORED. At 60 minutes the MEAN resumption is +1.7% against a
-3.1% median, so a small number of silent tokens do rip. This rule gives that up on
purpose: 88% of the sample never prints again, and a position we can neither price nor
sell is not a lottery ticket, it is an unrecorded loss. ``stale_no_volume_exit_s: 0``
turns the rule off for anyone who wants to take the other side of that trade.

PRECEDENCE. This is the LAST rule. Every price-based exit -- rug, emergency, hard stop,
trailing stop -- and every take-profit rung is offered the tick first, because each of them
is a better-evidenced reason to sell than "nothing has happened lately". Silence only
decides the case where we would otherwise have done nothing at all.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from kaiba.execution.protection import ProtectionConfig, ProtectionState, evaluate

NOW = 1_790_000_000_000
HOUR_MS = 3_600_000


def cfg(**kw) -> ProtectionConfig:
    base = {
        "stop_loss_bps": 4000,
        "trailing": [(Decimal("1.2"), 2000), (Decimal("2.0"), 3000)],
        "stale_no_volume_exit_s": 3600,
    }
    base.update(kw)
    return ProtectionConfig(**base)


def state(entry="100", peak=None) -> ProtectionState:
    return ProtectionState(
        position_id="pos_quiet",
        entry_price=Decimal(entry),
        peak_price=Decimal(peak if peak is not None else entry),
    )


def tick(**kw):
    args = {"price_usd": Decimal("105"), "now_ms": NOW, "cfg": cfg()}
    args.update(kw)
    return args


# ------------------------------------------------------------------ the rule


def test_a_silent_token_is_sold():
    """THE FIX: 88% of tokens silent for an hour never trade again."""
    action = evaluate(state(), last_trade_ms=NOW - HOUR_MS - 1000, **tick())
    assert action.kind.value == "exit_all"
    assert action.reason.startswith("stale_no_volume")


def test_a_trading_token_is_held():
    action = evaluate(state(), last_trade_ms=NOW - 60_000, **tick())
    assert action.kind.value != "exit_all"


@pytest.mark.parametrize("age_s,sold", [(3599, False), (3600, True), (7200, True)])
def test_the_threshold(age_s, sold):
    action = evaluate(state(), last_trade_ms=NOW - age_s * 1000, **tick())
    assert (action.kind.value == "exit_all") is sold


def test_an_unknown_last_trade_does_not_sell():
    """We have not OBSERVED silence; we have failed to look. Those are different, and the
    second one must not liquidate a position (CONTRACT rule 2)."""
    action = evaluate(state(), last_trade_ms=None, **tick())
    assert action.kind.value != "exit_all"


def test_a_future_last_trade_does_not_sell():
    """Clock skew must not read as silence."""
    action = evaluate(state(), last_trade_ms=NOW + HOUR_MS, **tick())
    assert action.kind.value != "exit_all"


def test_the_rule_can_be_switched_off():
    action = evaluate(
        state(), last_trade_ms=NOW - 10 * HOUR_MS, **tick(cfg=cfg(stale_no_volume_exit_s=0)))
    assert action.kind.value != "exit_all"


# ------------------------------------------------------------------ precedence


def test_a_price_stop_still_wins_over_silence():
    """A stop is better evidence than an absence of prints, and names the real reason."""
    action = evaluate(
        state(), last_trade_ms=NOW - 10 * HOUR_MS, **tick(price_usd=Decimal("20")))
    assert action.kind.value == "exit_all"
    assert action.reason in ("emergency_loss", "stop_loss")


def test_a_take_profit_rung_still_fires_on_a_quiet_token():
    """If the last print was a 2x, take the rung rather than dumping the whole position."""
    action = evaluate(
        state(), last_trade_ms=NOW - 10 * HOUR_MS, **tick(price_usd=Decimal("250")))
    assert action.kind.value == "trim"


def test_a_rug_still_wins_over_silence():
    action = evaluate(
        state(), last_trade_ms=NOW - 10 * HOUR_MS,
        **tick(liquidity_usd=Decimal(0), prev_liquidity_usd=Decimal("50000")))
    assert action.kind.value == "exit_all"
    assert action.reason.startswith("rug:lp_-")


def test_the_reason_says_how_long_it_was_silent():
    """A post-mortem needs the number, not just the word."""
    action = evaluate(state(), last_trade_ms=NOW - 2 * HOUR_MS, **tick())
    assert "7200" in action.reason or "120" in action.reason


# ------------------------------------------------------------------ config


def test_the_default_is_the_measured_hour():
    assert ProtectionConfig().stale_no_volume_exit_s == 3600
