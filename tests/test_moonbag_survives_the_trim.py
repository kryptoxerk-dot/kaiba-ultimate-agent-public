"""The moon bag must survive the tick that created it.

MEASURED 2026-09-24 on all 12 live moon bags taken since the protection fix: median time
held after the trim was ONE MINUTE, four were sold on the very next tick, and one had
moved +51.5% by the time it was sold. Cause: the trim fires AT the trailing stop and the
ratchet is monotonic, so the bag inherited the stop that had just triggered -- sitting at
or above the current price -- and the next evaluation satisfied `price <= stop_price`.

The owner asked for this feature explicitly ("please do not exit fully if we are
profitable like leave moon bag especially up trend") and `ProtectionConfig.
moonbag_retain_pct` records why it pays: after a profitable trailing exit the token
traded at least 29% higher 69-74% of the time.
"""
from decimal import Decimal

import pytest

from kaiba.execution.protection import (
    MOONBAG_TAG,
    ProtectionConfig,
    ProtectionState,
    evaluate,
)

ENTRY = Decimal("0.001")


def _cfg(**over):
    base = dict(
        stop_loss_bps=3000,
        emergency_loss_bps=5000,
        tp_ladder=[(Decimal(2), 50)],
        trailing=[(Decimal("1.2"), 2000), (Decimal(2), 3000)],
        moonbag_retain_pct=20,
        moonbag_trail_bps=5000,
        breakeven_after_tp1=True,
        stale_no_volume_exit_s=0,
    )
    base.update(over)
    return ProtectionConfig(**base)


def _ride_to_trim(cfg):
    """Ride up, then fall until the trailing stop trims the bag. Returns (state, price)."""
    state = ProtectionState(position_id="p", entry_price=ENTRY, peak_price=ENTRY)
    for mult in ("1.10", "1.30", "1.60"):
        evaluate(state, price_usd=ENTRY * Decimal(mult), cfg=cfg)
    for mult in ("1.45", "1.35", "1.28", "1.20"):
        price = ENTRY * Decimal(mult)
        action = evaluate(state, price_usd=price, cfg=cfg)
        if action.sells and action.pct < 100:
            return state, price
    raise AssertionError("the trailing stop never trimmed a bag")


def test_the_bag_is_taken_as_a_trim_not_a_close():
    state, _ = _ride_to_trim(_cfg())
    assert MOONBAG_TAG in state.tp_done


def test_the_stop_is_left_below_the_trim_price():
    """The regression itself: a stop at or above the trim price sells on the next tick."""
    state, price = _ride_to_trim(_cfg())
    assert state.stop_price is not None
    assert state.stop_price < price, (
        f"bag inherited a stop of {state.stop_price} at a trim price of {price}; "
        "the next tick would liquidate it"
    )


def test_the_bag_is_not_sold_on_the_very_next_tick():
    state, price = _ride_to_trim(_cfg())
    action = evaluate(state, price_usd=price, cfg=_cfg())
    assert not action.sells, f"bag sold immediately: {action.reason}"


def test_the_bag_rides_a_continuation_instead_of_being_dumped():
    """The +51.5% case we measured: after the trim the price ran. Keep the bag."""
    cfg = _cfg()
    state, price = _ride_to_trim(cfg)
    for mult in ("1.3", "1.8", "2.4"):
        action = evaluate(state, price_usd=ENTRY * Decimal(mult), cfg=cfg)
        # A take-profit rung trimming on the way up is the ladder working; what must not
        # happen is the position being CLOSED while the price is still climbing.
        assert not (action.sells and action.pct >= 100), (
            f"bag closed at {mult}x on the way up: {action.reason}"
        )
    assert state.peak_price >= ENTRY * Decimal("2.4")


def test_the_bag_is_still_floored_at_breakeven():
    """Wider leash is not NO leash: breakeven_after_tp1 keeps the bag out of the red."""
    state, _ = _ride_to_trim(_cfg())
    assert state.stop_price >= ENTRY


def test_the_bag_is_taken_only_once():
    """Latched, or a falling price trims 80% of 20% of 20% into unsellable dust."""
    cfg = _cfg()
    state, _ = _ride_to_trim(cfg)
    trims = 0
    for mult in ("1.05", "1.02", "1.0", "0.95", "0.90"):
        action = evaluate(state, price_usd=ENTRY * Decimal(mult), cfg=cfg)
        if action.sells and action.pct < 100:
            trims += 1
        if action.sells and action.pct >= 100:
            break
    assert trims == 0, "the bag was trimmed a second time"
    assert state.tp_done.count(MOONBAG_TAG) == 1


def test_a_disabled_moonbag_still_closes_the_position():
    """moonbag_retain_pct=0 must keep the old full-exit behaviour."""
    cfg = _cfg(moonbag_retain_pct=0)
    state = ProtectionState(position_id="p", entry_price=ENTRY, peak_price=ENTRY)
    for mult in ("1.10", "1.30", "1.60"):
        evaluate(state, price_usd=ENTRY * Decimal(mult), cfg=cfg)
    sold = None
    for mult in ("1.45", "1.35", "1.28", "1.20"):
        action = evaluate(state, price_usd=ENTRY * Decimal(mult), cfg=cfg)
        if action.sells:
            sold = action
            break
    assert sold is not None and sold.pct >= 100
    assert MOONBAG_TAG not in state.tp_done


def test_a_deep_bag_keeps_its_tighter_tier_trail():
    """max(), not replace: a 25x bag must not be widened back out to 50%."""
    cfg = _cfg(trailing=[(Decimal("1.2"), 2000), (Decimal(25), 1500)], moonbag_trail_bps=5000)
    state = ProtectionState(
        position_id="p", entry_price=ENTRY, peak_price=ENTRY * 30, tp_done=[MOONBAG_TAG]
    )
    from kaiba.execution.protection import _trail_bps_for

    assert _trail_bps_for(state, cfg) == 5000
    cfg2 = _cfg(trailing=[(Decimal("1.2"), 2000), (Decimal(25), 6000)], moonbag_trail_bps=5000)
    assert _trail_bps_for(state, cfg2) == 6000
