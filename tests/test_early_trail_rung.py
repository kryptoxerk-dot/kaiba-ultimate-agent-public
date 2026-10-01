"""Nothing protected a position between entry and 2.0x. Now something does.

THE HOLE, MEASURED on the live box 2026-09-23 over 91 stopped-out live fills:

    never moved up (<5%)   38   42%
    up 5-25%               28   31%
    up 25-50%              13   14%
    up 50-100%             11   12%
    up 100%+                1    1%

58% of the positions we stopped out of had been WINNING. The median stopped-out fill ran
to +10.3% (p90 +47%) and still closed at -35.6%, because the first trailing rung sat at
2.0x and the take-profit ladder started there too: below +100% the hard stop off entry was
the only rule in play, so a position could run to +47%, round-trip through breakeven and
stop out for a full loss without a single rung ever arming.

WHAT WAS CHOSEN, and by whom. Replaying all 109 closed live fills against their real swap
paths, choosing on the OLDER half and scoring on the NEWER half that was never used to
pick (mean return per fill):

    policy                              older     NEWER
    shipped 30% / nothing below 2x     -24.2%    -12.2%
    40% + trail20 from +20%            -16.5%    -11.1%   <- shipped
    50% + trail20 from +20%            -18.0%    -13.1%
    40% + trail20 from +25%            -18.3%    -12.4%
    50% + trail20 from +25%            -20.5%    -15.6%
    60% + trail20 from +25%            -21.7%    -17.9%

The early rung is the part that is clearly positive and it holds on the holdout at every
stop width. The hard stop is the OWNER'S CALL, moved 30 -> 40 -> 30 on 2026-09-23 after being shown
that the measurement points the other way: a 20% stop scored -6.9% on the same holdout
against -11.1% here. It is recorded as their decision, not as a measured optimum, and 40%
is the widest value that still beats the shipped ladder out of sample -- 50% and 60% are
both WORSE than changing nothing, which is why neither was taken.

Note the interaction with ``emergency_loss_bps`` (5000). The hard stop must stay tighter
than the emergency exit or the emergency path becomes the de-facto stop, and emergency
exits realised -56.7% mean over 20 live fills against -34.4% for an ordinary stop. At 4000
against 5000 the ordinary stop still fires first, but the margin is now 10pp, not 20pp.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from kaiba.execution import protection
from kaiba.execution.protection import ProtectionConfig, ProtectionState, next_stop


def cfg(**kw) -> ProtectionConfig:
    base = {
        "stop_loss_bps": 4000,
        "trailing": [(Decimal("1.2"), 2000), (Decimal("2.0"), 3000), (Decimal("5.0"), 2500)],
    }
    base.update(kw)
    return ProtectionConfig(**base)


def state(entry="100", peak=None, stop=None) -> ProtectionState:
    return ProtectionState(
        position_id="pos_test",
        entry_price=Decimal(entry),
        peak_price=Decimal(peak if peak is not None else entry),
        stop_price=Decimal(stop) if stop is not None else None,
    )


# ------------------------------------------------------------------ the rung exists


def test_the_shipped_ladder_protects_below_2x():
    """THE REGRESSION: the first rung was at 2.0x and nothing armed under it."""
    shipped = protection.protection_config()
    lowest = min(mult for mult, _bps in shipped.trailing)
    assert lowest < Decimal("2.0"), (
        f"the trailing ladder starts at {lowest}: a position can again run to +47%, give "
        "it all back and stop out for a full loss with nothing ever armed"
    )


def test_the_shipped_hard_stop_is_the_owners_number():
    """The owner moved this twice on 2026-09-23: 30% -> 40% -> 30%.

    It is pinned because it is a CHOICE, not a measurement, and a silent drift back would
    be indistinguishable from someone tuning it. For the record, the holdout measurement
    preferred 20% (-6.9% per fill against -11.1% at 30%); the owner chose 30% and that is
    what ships.
    """
    shipped = protection.protection_config()
    assert shipped.stop_loss_bps == 3000, (
        f"the hard stop is {shipped.stop_loss_bps} bps; the owner set 30% on 2026-09-23"
    )


def test_the_hard_stop_stays_tighter_than_the_emergency_exit():
    """Otherwise the emergency path becomes the real stop, at -56.7% instead of -34.4%."""
    shipped = protection.protection_config()
    assert shipped.stop_loss_bps < shipped.emergency_loss_bps, (
        f"hard stop {shipped.stop_loss_bps} bps is at or past the emergency exit "
        f"{shipped.emergency_loss_bps} bps"
    )


# ------------------------------------------------------------------ it actually arms


def test_a_position_up_20_percent_gets_a_trailing_stop():
    """THE FIX. At +20% the rung arms and the stop leaves the entry-based hard stop."""
    hard = Decimal("100") * (1 - Decimal("4000") / 10000)       # 60
    armed = next_stop(state(peak="120"), Decimal("120"), cfg())
    assert armed > hard, f"nothing armed at +20%: stop {armed} is still the hard stop"
    assert armed == Decimal("120") * Decimal("0.8")             # 96


def test_below_the_rung_the_hard_stop_still_governs():
    """A position that never runs is not protected by a rung that never armed."""
    assert next_stop(state(peak="115"), Decimal("115"), cfg()) == Decimal("60")


def test_the_rung_does_not_fire_a_stop_above_the_current_price():
    """At exactly +20% the stop sits at 0.96x entry -- a small loss, not a profit lock.

    Worth pinning because "breakeven at +20%" is the obvious way to describe this rung and
    it is NOT what it does: a 20% trail off a 1.2x peak lands below the entry price.
    """
    armed = next_stop(state(peak="120"), Decimal("120"), cfg())
    assert armed < Decimal("100"), "the rung claims to lock in profit; it does not"


def test_the_stop_ratchets_up_and_never_down():
    c = cfg()
    at_130 = next_stop(state(peak="130", stop="96"), Decimal("130"), c)
    assert at_130 == Decimal("104")
    pulled_back = next_stop(state(peak="130", stop="104"), Decimal("105"), c)
    assert pulled_back == Decimal("104"), "the stop came down on a pullback"


def test_a_higher_rung_still_wins_when_unlocked():
    """The 2.0x rung must still take over; the new rung only fills the gap below it."""
    armed = next_stop(state(peak="250"), Decimal("250"), cfg())
    assert armed == Decimal("250") * Decimal("0.7")   # 2.0x tier, 3000 bps


@pytest.mark.parametrize("peak,expected", [("119", "60"), ("120", "96"), ("150", "120")])
def test_the_rung_boundary(peak, expected):
    assert next_stop(state(peak=peak), Decimal(peak), cfg()) == Decimal(expected)


def test_next_stop_is_still_pure():
    """`evaluate` commits; `next_stop` must not move the state it was handed."""
    s = state(peak="120")
    before = (s.peak_price, s.stop_price)
    next_stop(s, Decimal("500"), cfg())
    assert (s.peak_price, s.stop_price) == before
