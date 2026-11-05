"""Trade launchpad tokens; a manual deploy needs more smart money behind it.

OWNER POLICY 2026-09-23: "only trade on pump.fun, stonkfun and launchpads" on every chain,
and "only trade manual deploy IF really good wallets are in and volume is good".

MEASURED on the tape, reach-5x for a launchpad token against a manual one:

    sol         launchpad  8.0%   manual  4.1%     (2.0x worse)
    bsc         launchpad 14.7%   manual  5.5%     (2.7x worse)
    robinhood   launchpad  6.1%   manual  8.4%     (manual is BETTER)

Robinhood contradicts the policy and it is written down rather than smoothed over: 60% of
its "launchpad" volume is pons v1, which the mooner study put at 0.81x lift -- under its
own chain's base rate. The policy is applied there anyway because the owner asked for it on
all three chains, and because a genuinely good manual deploy still gets in through the bar
below.

Manual deploys by smart wallets present early, which is what sets that bar:

    sol        0 wallets 1.8%   1-2 10.6%   3-4 8.4%   >=5 8.2%
    bsc        0 wallets 4.0%   1-2  5.4%   3-4 6.2%   >=5 5.0%
    robinhood  0 wallets 5.7%   1-2 19.5%   3-4 5.0%   >=5 5.4%

On sol the bar earns its keep: a manual deploy with smart money matches the launchpad
baseline (8.4% vs 8.0%) while one with none collapses to 1.8%.

The volume half was implemented 2026-10-05, once ``dyor`` began populating
``volume_24h_usd``: a manual deploy also needs a KNOWN, fresh 24h volume at or above
``manual_min_volume_24h_usd`` (0 -- no threshold has been measured). Unknown volume refuses;
``tests/test_audit_volume_policy.py`` is the reproduction that found it missing.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from kaiba.core.schemas import (
    EvidenceBasis,
    Lane,
    Measure,
    Receipt,
    Token,
)
from kaiba.execution import lanes
from kaiba.intelligence import tracker
from tests.test_bsc_lane_inputs import BSC, _ctx, _three_smart_buyers, _tok


def ctx_with(conn, *, launchpad: str | None, token_n: int, smart: int = 3, **params):
    """A context that fires on `smart` seeded buyers, with the given deploy provenance."""
    token = _tok(token_n)
    _three_smart_buyers(conn, BSC, token)
    tracker.seed_from_cohorts(BSC, conn)
    base = _ctx(conn, BSC, token, rug_ratio=None)
    meta = Token(address=token, chain=BSC, launchpad=launchpad) if launchpad else None
    merged = {**(base.params or {}), "min_liquidity_usd": 0, **params}
    return base.model_copy(update={"token_meta": meta, "params": merged})


# ------------------------------------------------------------------ the policy is on


def test_the_policy_is_enabled_on_the_live_lane():
    params = lanes.DEFAULT_PARAMS[Lane.SM_TRENCHES]
    assert params.get("require_launchpad") is True, "the launchpad policy is off"
    assert int(params["manual_min_smart_degen"]) > int(
        lanes.DEFAULT_PARAMS[Lane.SM_TRENCHES].get("min_smart_degen", 3)
        if "min_smart_degen" in lanes.DEFAULT_PARAMS[Lane.SM_TRENCHES]
        else 3
    ), "a manual deploy does not need MORE confluence than a launchpad token"


# ------------------------------------------------------------------ launchpad vs manual


@pytest.mark.parametrize("launchpad", ["pump.fun", "stonkfun", "flap", "pons_v2", "ray_launchpad"])
def test_a_launchpad_token_passes_on_the_normal_bar(tmp_db, launchpad):
    """Three smart buyers is enough when we know where the token came from."""
    signal = lanes.sm_trenches(ctx_with(tmp_db, launchpad=launchpad, token_n=60))
    assert signal is not None, f"{launchpad} was refused on the normal bar"


def test_a_manual_deploy_is_refused_on_the_normal_bar(tmp_db):
    """THE POLICY: no launchpad means the bar rises from 3 smart wallets to 5.

    MEASURED: a manual sol deploy with no smart money reaches 5x 1.8% of the time against
    an 8.0% launchpad baseline.
    """
    assert lanes.sm_trenches(ctx_with(tmp_db, launchpad=None, token_n=61)) is None


def with_volume(ctx, usd: str | None):
    """The same context with a known (or, for None, unknown) fresh 24h volume."""
    if usd is None:
        measure = Measure.unknown()
    else:
        measure = Measure(
            value=Decimal(usd), basis=EvidenceBasis.PROVIDER_REPORTED,
            receipt=Receipt(provider="fixture", endpoint="dossier", observed_at_ms=ctx.now_ms),
            freshness_budget_s=86_400,
        )
    return ctx.model_copy(update={"dossier": ctx.dossier.model_copy(update={"volume_24h_usd": measure})})


def test_a_manual_deploy_passes_when_the_bar_is_met(tmp_db):
    """The owner's escape hatch: really good wallets AND known volume get a manual deploy in."""
    ctx = ctx_with(tmp_db, launchpad=None, token_n=62, manual_min_smart_degen=3)
    assert lanes.sm_trenches(with_volume(ctx, "25000")) is not None, "the escape hatch does not open"
    assert lanes.sm_trenches(with_volume(ctx, None)) is None, "wallets alone opened it"


def test_the_manual_volume_floor_is_a_parameter(tmp_db):
    """A measured floor, when one exists, is configuration -- and it binds only manual deploys."""
    ctx = ctx_with(tmp_db, launchpad=None, token_n=66, manual_min_smart_degen=3,
                   manual_min_volume_24h_usd=10_000)
    assert lanes.sm_trenches(with_volume(ctx, "9999")) is None
    assert lanes.sm_trenches(with_volume(ctx, "10000")) is not None
    launchpad = ctx_with(tmp_db, launchpad="pump.fun", token_n=67, manual_min_volume_24h_usd=10_000)
    assert lanes.sm_trenches(with_volume(launchpad, None)) is not None, "the floor leaked onto launchpads"


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_a_blank_launchpad_counts_as_manual(tmp_db, blank):
    """An empty string is not provenance. Whitespace is not either."""
    token = _tok(63)
    _three_smart_buyers(tmp_db, BSC, token)
    tracker.seed_from_cohorts(BSC, tmp_db)
    base = _ctx(tmp_db, BSC, token, rug_ratio=None)
    meta = Token(address=token, chain=BSC, launchpad=blank)
    ctx = base.model_copy(update={
        "token_meta": meta, "params": {**(base.params or {}), "min_liquidity_usd": 0}})
    assert lanes.sm_trenches(ctx) is None


def test_missing_token_meta_counts_as_manual(tmp_db):
    """A token we know nothing about is not a launchpad token.

    `token_meta` is a `Token` MODEL, not a dict. The first version of this gate used
    `.get("launchpad")`, which returns nothing for every token and would have made the
    entire book look manual -- silently raising the bar on everything.
    """
    ctx = ctx_with(tmp_db, launchpad=None, token_n=64)
    assert ctx.token_meta is None
    assert lanes.sm_trenches(ctx) is None


# ------------------------------------------------------------------ it can be turned off


def test_the_policy_can_be_disabled_for_a_study(tmp_db):
    """A shadow sweep must be able to measure the band the policy refuses."""
    ctx = ctx_with(tmp_db, launchpad=None, token_n=65, require_launchpad=False)
    assert lanes.sm_trenches(ctx) is not None


def test_the_gate_reads_the_model_attribute_not_a_dict_key():
    import inspect

    source = inspect.getsource(lanes.sm_trenches)
    assert "ctx.token_meta.launchpad" in source, (
        "the launchpad is being read some other way; a dict .get() here reads None for "
        "every token and makes the whole book look manual"
    )
