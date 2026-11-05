"""Do not sell the whole thing when the trailing stop fires in profit. Keep a bag.

OWNER 2026-09-23: *"please do not exit fully if we are profitable like leave moon bag
especially up trend"*.

MEASURED on the live box, our own closed positions, what the token's BEST price did after
we exited on a profitable trailing stop (n=51-53):

    best after  1h   median 1.29x   above our exit 69% of the time   2x+ 18%
    best after  6h   median 1.32x   above our exit 71% of the time   2x+ 19%
    best after 24h   median 1.29x   above our exit 74% of the time   2x+ 19%

So seven times in ten the thing we just sold went on to trade at least 29% higher. The
take-profit ladder already retains ~32% (50% at 2x, 25% of the rest at 5x, 15% at 10x);
the TRAILING STOP was the full exit, and it is what closes most winners.

WHAT THIS IS NOT. It is not "hold and hope". The retained bag stays under every rule it
had: the ratcheted trailing stop still governs it, the take-profit rungs above still fire,
`stale_no_volume` still cleans it up if the token stops trading, and the emergency exit
still applies. The only change is that ONE trailing-stop hit, while in profit, trims
instead of closing.

IT HAPPENS ONCE. Without that, a falling price trims 80% of 20% of 20%... and the position
decays into dust that cannot be sold economically. The bag is taken once, recorded in
``tp_done``, and the next trailing stop closes the position.

IN PROFIT ONLY. A trailing stop can fire BELOW entry -- the ratchet sets a stop from a peak
that was above entry, the price falls through entry, and the stop catches it there. That is
a loss being cut, not a winner being trimmed, and it exits in full.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from kaiba.execution.protection import ProtectionConfig, ProtectionState, evaluate

NOW = 1_790_000_000_000


def cfg(**kw) -> ProtectionConfig:
    base = {
        "stop_loss_bps": 3000,
        "trailing": [(Decimal("1.2"), 2000), (Decimal("2.0"), 3000)],
        "tp_ladder": [(Decimal("2.0"), Decimal(50))],
        "moonbag_retain_pct": 20,
    }
    base.update(kw)
    return ProtectionConfig(**base)


def state(entry="100", peak=None, stop=None, tp_done=None) -> ProtectionState:
    return ProtectionState(
        position_id="pos_bag",
        entry_price=Decimal(entry),
        peak_price=Decimal(peak if peak is not None else entry),
        stop_price=Decimal(stop) if stop is not None else None,
        tp_done=list(tp_done or []),
        activated_trail_bps=2000,
    )


def tick(**kw):
    args = {"now_ms": NOW, "cfg": cfg()}
    args.update(kw)
    return args


# ------------------------------------------------------------------ the bag


def test_a_profitable_trailing_stop_trims_instead_of_closing():
    """THE OWNER'S RULE: 7 of 10 tokens trade higher after we sell them."""
    s = state(peak="200", stop="160")
    action = evaluate(s, price_usd=Decimal("155"), **tick())
    assert action.kind.value == "trim", action.reason
    assert action.pct == Decimal(80), f"sold {action.pct}%, expected 80"
    assert "moonbag" in action.reason


def test_the_bag_is_the_configured_size():
    s = state(peak="200", stop="160")
    action = evaluate(s, price_usd=Decimal("155"), **tick(cfg=cfg(moonbag_retain_pct=35)))
    assert action.pct == Decimal(65)


def test_it_only_happens_once():
    """Otherwise 80% of 20% of 20%... decays into unsellable dust.

    Since 2026-09-24 the bag is NOT closed on the tick after the trim -- that defect
    killed all 12 live bags a median of one minute after they were taken, one of them
    after it had moved +51.5%. What this test guards is the latch: the bag is trimmed
    once and never again, whatever the price does afterwards.
    """
    s = state(peak="200", stop="160")
    first = evaluate(s, price_usd=Decimal("155"), **tick())
    assert first.kind.value == "trim"
    trims = 0
    for price in ("150", "140", "120", "101", "99", "60"):
        action = evaluate(s, price_usd=Decimal(price), **tick())
        if action.kind.value == "trim":
            trims += 1
        if action.kind.value == "exit_all":
            break
    assert trims == 0, "the bag was trimmed twice"
    assert s.tp_done.count("moonbag") == 1


def test_the_bag_is_recorded_on_the_state():
    s = state(peak="200", stop="160")
    evaluate(s, price_usd=Decimal("155"), **tick())
    assert any("moonbag" in t for t in s.tp_done), s.tp_done


# ------------------------------------------------------------------ in profit ONLY


def test_a_trailing_stop_below_entry_still_exits_fully():
    """The ratchet can set a stop from a peak above entry and catch the price BELOW it.
    That is a loss being cut, not a winner being trimmed."""
    s = state(peak="130", stop="95")
    action = evaluate(s, price_usd=Decimal("94"), **tick())
    assert action.kind.value == "exit_all"
    assert "moonbag" not in action.reason


def test_a_price_exactly_at_entry_is_not_profit():
    s = state(peak="130", stop="100")
    action = evaluate(s, price_usd=Decimal("100"), **tick())
    assert action.kind.value == "exit_all"


def test_the_hard_stop_never_leaves_a_bag():
    """Before any TP, a hard-stop loss closes in full."""
    s = state(peak="100")
    s.activated_trail_bps = None
    action = evaluate(s, price_usd=Decimal("60"), **tick())
    assert action.kind.value == "exit_all"
    assert action.reason == "stop_loss"


def test_a_non_trailing_stop_in_profit_still_exits_fully():
    """The `reason == "trailing_stop"` guard, pinned directly.

    A stop above entry with NO trail armed is not reachable through the shipped ladder
    (an unarmed stop sits at or below entry), so this builds the state by hand. The guard
    is defensive, and a mutation removing it survived the whole file without this.
    """
    s = ProtectionState(
        position_id="pos_bag",
        entry_price=Decimal("100"),
        peak_price=Decimal("200"),
        stop_price=Decimal("160"),
        tp_done=["tp1"],
        activated_trail_bps=None,      # NOT a trailing stop
    )
    action = evaluate(s, price_usd=Decimal("155"), **tick())
    assert action.kind.value == "exit_all", action.reason
    assert action.reason == "stop_loss"
    assert "moonbag" not in action.reason


def test_the_emergency_exit_never_leaves_a_bag():
    s = state(peak="200", stop="160")
    action = evaluate(s, price_usd=Decimal("40"), **tick())
    assert action.kind.value == "exit_all"
    assert action.reason == "emergency_loss"


def test_a_rug_never_leaves_a_bag():
    s = state(peak="200", stop="160")
    action = evaluate(
        s, price_usd=Decimal("155"),
        liquidity_usd=Decimal(0), prev_liquidity_usd=Decimal("50000"), **tick())
    assert action.kind.value == "exit_all"
    assert action.reason.startswith("rug:lp_-")


# ------------------------------------------------------------------ switches


def test_zero_retain_restores_the_old_behaviour():
    s = state(peak="200", stop="160")
    action = evaluate(s, price_usd=Decimal("155"), **tick(cfg=cfg(moonbag_retain_pct=0)))
    assert action.kind.value == "exit_all"


@pytest.mark.parametrize("pct", [-1, 100, 101, 500])
def test_a_nonsense_retain_is_refused_not_obeyed(pct):
    """A 100% bag is 'never sell', which is not a protection policy."""
    s = state(peak="200", stop="160")
    action = evaluate(s, price_usd=Decimal("155"), **tick(cfg=cfg(moonbag_retain_pct=pct)))
    assert action.kind.value == "exit_all"


def test_the_shipped_default_keeps_a_bag():
    assert 0 < ProtectionConfig().moonbag_retain_pct < 100
