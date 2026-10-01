"""The live lane needs a book it can get out of, and that floor is the best edge we have.

MEASURED 2026-09-23, on the two kinds of evidence that matter, which agree:

**Our own money.** 113 closed LIVE fills, by liquidity at entry:

    > $15.5k     n=44   mean  +2.1%   win 32%     <- the only profitable band we have
    $5-15.5k     n=53   mean -31.0%   win  9%
    <= $5k       n=16   mean -33.9%   win  6%

69 of those 113 fills were taken below $15.5k, because ``sm_trenches`` -- the only live
lane -- had NO liquidity gate at all. The ``min_liquidity_usd: 5000`` in risk.yaml belongs
to ``pons_robinhood``, a shadow lane.

**The tape.** 7,837 tokens under an hour old, reaching 5x after the feature window:

    no floor     56% kept    9.6%    1.00x
    $5,000       21% kept   17.4%    1.82x
    $15,575      11% kept   22.7%    2.37x   <- peak
    $25,000       6% kept   19.0%    1.98x
    $50,000       4% kept   11.8%    1.23x

The PEAK is the part that makes this an effect rather than a proxy for size. If liquidity
were simply standing in for "big token", lift would rise monotonically. It does not: above
$15.5k it falls away, because a token already carrying $50k of book has made the move we
are trying to be early for. That band is early-with-traction, which is the thing we are
actually hunting.

An UNKNOWN liquidity refuses exactly like a low one. This is the number that decides
whether we can get back out, and "we could not read it" has never been evidence that it is
fine (CONTRACT rule 2).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from kaiba.core.schemas import (
    Chain,
    EvidenceBasis,
    Grade,
    Lane,
    Measure,
    Receipt,
    TokenDossier,
    now_ms,
)
from kaiba.execution import lanes


def dossier(liquidity, *, at_ms: int | None = None) -> TokenDossier:
    at = at_ms or now_ms()
    measure = Measure.unknown()
    if liquidity is not None:
        measure = Measure(
            value=Decimal(str(liquidity)),
            basis=EvidenceBasis.PROVIDER_REPORTED,
            receipt=Receipt(provider="fixture", endpoint="dossier", observed_at_ms=at),
            freshness_budget_s=86_400,
        )
    return TokenDossier(
        address="tok", chain=Chain.SOL, grade=Grade.B, built_at_ms=at,
        rug_ratio=Measure.unknown(), liquidity_usd=measure,
    )


def context(tmp_db, liquidity, **params):
    merged = {
        "min_smart_degen": 3,
        "min_independent_entities": 2,
        "max_rug_ratio": 0.3,
        "window_s": 300,
        "min_buy_usd": 0,
        **params,
    }
    return lanes.LaneContext(
        chain=Chain.SOL, token="tok", conn=tmp_db, recent_buys=[],
        dossier=dossier(liquidity), params=merged,
    )


# ------------------------------------------------------------------ the floor exists


@pytest.mark.parametrize("chain", [Chain.SOL, Chain.BSC, Chain.ROBINHOOD])
def test_every_live_chain_has_a_liquidity_floor(chain):
    """THE REGRESSION: it had none, and 69 of 113 live fills were taken below the band."""
    floor = float(lanes._min_liquidity_for(lanes.DEFAULT_PARAMS[Lane.SM_TRENCHES], chain))
    assert floor > 0, f"{chain.value} has no liquidity floor again"
    assert floor >= 5_000, f"{chain.value} floor {floor} is below every measured band"


@pytest.mark.parametrize("chain", [Chain.SOL, Chain.BSC, Chain.ROBINHOOD])
def test_no_floor_is_set_past_its_own_chains_peak(chain):
    """Each chain has its OWN book size; a flat number cost robinhood 88% of its signals.

    Medians for a token under an hour old: sol $3,576, bsc $3,630, robinhood $1,475.
    """
    floor = float(lanes._min_liquidity_for(lanes.DEFAULT_PARAMS[Lane.SM_TRENCHES], chain))
    assert floor <= 15_000, (
        f"{chain.value} floor {floor}: a flat $15,000 took robinhood from 15.3 signals/h "
        "to 1.9/h, which is how this became per-chain"
    )


def test_a_scalar_floor_still_applies_everywhere():
    """Every other lane still configures a plain number; that must keep working."""
    for chain in (Chain.SOL, Chain.BSC, Chain.ROBINHOOD):
        assert lanes._min_liquidity_for({"min_liquidity_usd": 5_000}, chain) == 5_000


def test_an_unlisted_chain_takes_the_default_not_a_refusal():
    """An unconfigured floor is an ABSENT one, never a silent refusal of a whole chain."""
    mapping = {"min_liquidity_usd": {"sol": 10_000, "default": 4_000}}
    assert lanes._min_liquidity_for(mapping, Chain.SOL) == 10_000
    assert lanes._min_liquidity_for(mapping, Chain.BSC) == 4_000
    assert lanes._min_liquidity_for({}, Chain.BSC) == 0


# ------------------------------------------------------------------ it actually refuses
#
# These reuse a context that GENUINELY FIRES -- three seeded smart buyers on real tape --
# so liquidity is the only thing that differs between passing and refusing. The first
# version of this file used `recent_buys=[]`, which made `sm_trenches` return None for
# want of buyers, and every refusal test below passed without the gate existing at all.
# A mutation that let an unknown liquidity through survived the whole file.

from tests.test_bsc_lane_inputs import (  # noqa: E402
    BSC,
    _ctx,
    _three_smart_buyers,
    _tok,
)
from kaiba.intelligence import tracker  # noqa: E402


def firing_ctx(conn, liquidity, *, token_n: int = 91):
    """A context that produces a signal, with `liquidity` as the only variable."""
    token = _tok(token_n)
    _three_smart_buyers(conn, BSC, token)
    tracker.seed_from_cohorts(BSC, conn)
    ctx = _ctx(conn, BSC, token, rug_ratio=None)
    measure = Measure.unknown()
    if liquidity is not None:
        measure = Measure(
            value=Decimal(str(liquidity)),
            basis=EvidenceBasis.PROVIDER_REPORTED,
            receipt=Receipt(provider="fixture", endpoint="dossier", observed_at_ms=now_ms()),
            freshness_budget_s=86_400,
        )
    return ctx.model_copy(
        update={"dossier": ctx.dossier.model_copy(update={"liquidity_usd": measure})}
    )


def test_the_fixture_really_does_fire_without_the_floor(tmp_db):
    """The control. Without this, every refusal below could be an absent signal."""
    ctx = firing_ctx(tmp_db, 50_000, token_n=90)
    ctx = ctx.model_copy(update={"params": {**ctx.params, "min_liquidity_usd": 0}})
    assert lanes.sm_trenches(ctx) is not None, "the fixture does not fire; the tests prove nothing"


def test_a_deep_book_still_fires(tmp_db):
    assert lanes.sm_trenches(firing_ctx(tmp_db, 50_000, token_n=92)) is not None


@pytest.mark.parametrize("liquidity", [0, 1_000, 5_000, 9_999])
def test_a_thin_book_is_refused(tmp_db, liquidity):
    """THE REGRESSION: 69 of 113 live fills were taken in exactly this range."""
    assert lanes.sm_trenches(firing_ctx(tmp_db, liquidity)) is None


def test_an_unknown_liquidity_is_refused(tmp_db):
    """"We could not read it" is not evidence that the book is fine.

    This is the case a mutation survived in the first version of this file.
    """
    assert lanes.sm_trenches(firing_ctx(tmp_db, None)) is None


def test_the_floor_can_be_turned_off_for_a_study(tmp_db):
    """Zero disables it, so a shadow sweep can measure the band it would have refused."""
    ctx = firing_ctx(tmp_db, 100, token_n=93)
    assert lanes.sm_trenches(ctx) is None
    off = ctx.model_copy(update={"params": {**ctx.params, "min_liquidity_usd": 0}})
    assert lanes.sm_trenches(off) is not None, "zero did not disable the floor"


def test_the_gate_reads_the_dossier_not_the_quote(tmp_db):
    """It must be the entry-time reading, not whatever a price source says later."""
    import inspect

    source = inspect.getsource(lanes.sm_trenches)
    assert "_min_liquidity_for(p, ctx.chain)" in source, "the floor is no longer per-chain"
    assert '_measure(ctx.dossier, "liquidity_usd")' in source
