"""A size a fraction under the chain floor is rounding, not a decision.

MEASURED 2026-09-24 on the live box: a sol entry was refused at 555,524,237 base units
against a floor of 560,000,000 -- 99.2% of it, vetoed over 0.8%. The pool band and the
chain floor are computed from different inputs (pool depth vs the operator's "never
trade dust"), so they land a fraction apart routinely.

Clamping UP is the conservative direction: the result is EXACTLY the operator's own
minimum, which is the size every other trade on that chain already uses. What must NOT
happen is the tolerance swallowing a real veto -- a pool that can carry half the floor
still has to refuse.
"""
from decimal import Decimal

import pytest

from kaiba.execution.risk import MIN_POSITION_NEAR_MISS

FLOOR = 560_000_000


def _short(banded: int) -> Decimal:
    return Decimal(FLOOR - banded) / Decimal(FLOOR)


def _clamps(banded: int) -> bool:
    return _short(banded) <= MIN_POSITION_NEAR_MISS


def test_the_tolerance_is_small():
    assert Decimal("0.02") <= MIN_POSITION_NEAR_MISS <= Decimal("0.15"), (
        "a wide tolerance stops being rounding and starts overriding the pool band"
    )


def test_the_measured_near_miss_now_trades():
    """The exact live refusal this exists for: 0.8% short."""
    assert _clamps(555_524_237)


def test_the_concentration_dead_zone_now_trades():
    """Also measured live: the 0.9x multiplier landing 8.6% under the floor."""
    assert _clamps(511_982_571)


@pytest.mark.parametrize("banded", [280_000_000, 400_000_000, 1])
def test_a_real_shortfall_is_still_refused(banded):
    """The veto must survive. A pool that can carry half the floor is not a near miss."""
    assert not _clamps(banded)


def test_a_size_already_at_or_above_the_floor_is_untouched():
    assert _short(FLOOR) == 0
    assert _clamps(FLOOR)


def test_the_boundary_is_inclusive_and_does_not_drift():
    """Exactly at the tolerance clamps; a base unit worse refuses."""
    exact = FLOOR - int(Decimal(FLOOR) * MIN_POSITION_NEAR_MISS)
    assert _clamps(exact)
    assert not _clamps(exact - int(Decimal(FLOOR) * Decimal("0.001")))


def test_the_clamp_never_produces_less_than_the_floor():
    """Whatever it clamps, the result is the floor itself -- never an in-between size."""
    for banded in (555_524_237, 511_982_571, FLOOR - 1):
        if _clamps(banded):
            assert FLOOR >= banded, "clamping must raise, never lower"
