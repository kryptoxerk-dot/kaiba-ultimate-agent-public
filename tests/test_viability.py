"""Position sizing economics: the band a trade is worth doing in, or the refusal.

Four properties are load-bearing and each has a test that fails if it is removed:

1. **too small** is refused — the flat cost is not amortised;
2. **too large** is refused — our own impact, or the order simply not filling, and this
   is the arm whose absence let 0.06 SOL orders into $25 pools;
3. an **exit is never refused**, whatever either arm says;
4. the costs are *measured* — fees from our own trades, depth from the token's own curve
   or pool — and every constant that remains is in the provenance table.
"""

from __future__ import annotations

import ast
import copy
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from kaiba.core.schemas import Chain, EvidenceBasis, Lane, now_ms
from kaiba.execution import viability
from kaiba.execution.curve_price import CURVE_TOTAL_FEE_BPS, CurveState
from kaiba.execution.risk import RiskGate
from kaiba.execution.viability import (
    DECLARED_FLAT_PER_LEG,
    DECLARED_PROPORTIONAL_BPS_PER_LEG,
    DEFAULT_MAX_ROUND_TRIP_COST_PCT,
    MIN_SAMPLE_TRADES,
    PROVENANCE,
    CostModel,
    CurveDepth,
    NoDepth,
    PoolDepth,
    check_size,
    configured_ceiling_pct,
    cost_model,
    declared_cost_model,
    derive_cost_model,
    estimate_round_trip,
    max_executable_size,
    optimal_size,
    pool_depth_from_round_trip,
    resolve_depth,
    sizing_band,
)

SOL = Chain.SOL
LANE = Lane.CONFLUENCE_5
TOKEN = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

#: The paper broker's real fee model, which is what our own closed trades were generated
#: by: 0.001 SOL tip per leg (``paper.DEFAULT_TIP_NATIVE``) and 125 bps of notional per
#: leg (``curve_price.CURVE_TOTAL_FEE_BPS``). The derivation is judged against these.
TRUE_FLAT_PER_LEG = 1_000_000
TRUE_BPS_PER_LEG = Decimal(CURVE_TOTAL_FEE_BPS)

#: SOL/USD used wherever a pool's depth is quoted in USD. Only the ratio to the pool
#: matters, and it keeps the arithmetic in the tests readable.
NATIVE_USD = Decimal(200)

BANKROLL = 10_000_000_000  # 10 SOL

BASE_RISK: dict = {
    "version": "v1",
    "global_mode": "live",
    "kill_switch": False,
    "entries_paused": False,
    "reduce_only": False,
    "bounds": {
        "max_size_pct_bankroll": 5.0,
        "max_daily_loss_pct": 10.0,
        "max_slippage_bps": 2500,
        "max_concurrent_positions": None,
        "max_lane_mode": "live",
        "allow_self_promotion": True,
    },
    "chains": {
        "sol": {
            "enabled": True,
            "bankroll_base_units": BANKROLL,
            "max_position_base_units": 500_000_000,
            "min_position_base_units": 1_000_000,
            "gas_reserve_base_units": 50_000_000,
            "daily_loss_stop_base_units": 500_000_000,
            "max_exposure_pct": 100.0,
            "wallet": None,
        },
        "base": {
            "enabled": True,
            "bankroll_base_units": 10**18,
            "max_position_base_units": 10**17,
            "min_position_base_units": 10**14,
            "gas_reserve_base_units": 10**15,
            "daily_loss_stop_base_units": 10**16,
            "max_exposure_pct": 100.0,
            "wallet": None,
        },
    },
    "lanes": {
        "confluence-5": {
            "mode": "live", "size_pct_min": 1.0, "size_pct_max": 5.0,
            "chains": ["sol", "base"], "params": {},
        },
    },
    "protection": {},
}


def _merge(base: dict, overrides: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


@pytest.fixture
def write_risk(tmp_path, monkeypatch):
    path = tmp_path / "risk.yaml"
    monkeypatch.setenv("KAIBA_RISK_PATH", str(path))

    def _write(**overrides):
        path.write_text(yaml.safe_dump(_merge(BASE_RISK, overrides)), encoding="utf-8")
        return path

    _write()
    return _write


# ------------------------------------------------------------------ fixtures: depth


def pool(liquidity_usd: str | int) -> PoolDepth:
    """A pool of a stated USD depth, priced exactly as the paper broker prices one."""
    return PoolDepth(liquidity_usd=Decimal(liquidity_usd), native_usd=NATIVE_USD)


#: The thinnest pool in our own record: ``order_events.detail`` on a migration-fade buy
#: reports ``liquidity_usd = 25.42`` and a 1069 bps single-leg impact on 0.0154 SOL.
THIN_POOL_USD = Decimal("25.42")
#: A graduated pair from our own ``token_dossiers``, for contrast. Same code path.
DEEP_POOL_USD = Decimal("1527762.21")


def curve(*, virtual_sol: int = 85_780_466_117, real_sol: int = 55_780_466_117) -> CurveDepth:
    """A real curve from our own record: the state recorded on a live curve buy.

    ``virtual_sol`` is the depth — the constant-product reserve our order walks — so a
    thin curve is one with a small ``virtual_sol``, not one with a small ``real_sol``.
    """
    state, note = CurveState.build(
        virtual_sol=virtual_sol,
        virtual_token=375_260_268_161_085,
        real_sol=real_sol,
        real_token=95_360_268_161_085,
        observed_ms=now_ms(),
    )
    assert state is not None, note
    return CurveDepth(state=state, source="test_curve")


def thin_curve() -> CurveDepth:
    """A curve early on its bonding curve: 1 SOL of virtual reserve, 0.2 SOL raised."""
    return curve(virtual_sol=1_000_000_000, real_sol=200_000_000)


# ------------------------------------------------------------------ fixtures: trades


def insert_trade(
    conn, *, cost: int, proceeds: int, fees: int, chain: Chain = SOL, mode: str = "shadow",
    lane: str = "migration-fade", seq: int = 0,
) -> None:
    conn.execute(
        "INSERT INTO trades (trade_id, position_id, lane, mode, chain, token, opened_ms, closed_ms, "
        "hold_s, cost_native, proceeds_native, pnl_native, pnl_pct, fees_native) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            f"t-{chain.value}-{mode}-{seq}-{cost}", f"p-{seq}-{cost}", lane, mode, chain.value,
            TOKEN, now_ms() - 60_000, now_ms() + seq, 60, str(cost), str(proceeds),
            str(proceeds - cost), 0.0, str(fees),
        ),
    )


def seed_paper_history(conn, *, n: int = 14, chain: Chain = SOL, mode: str = "shadow") -> None:
    """Closed trades generated by the fee model the paper broker actually applies."""
    sizes = [15_500_000 + i * 120_000 for i in range(n - 3)] + [50_000_000, 51_000_000, 102_000_000]
    for seq, cost in enumerate(sizes[:n]):
        proceeds = cost * 3 // 4
        fees = 2 * TRUE_FLAT_PER_LEG + int(Decimal(cost + proceeds) * TRUE_BPS_PER_LEG / 10_000)
        insert_trade(conn, cost=cost, proceeds=proceeds, fees=fees, chain=chain, mode=mode, seq=seq)


def _model(flat: int = TRUE_FLAT_PER_LEG, bps: Decimal = TRUE_BPS_PER_LEG) -> CostModel:
    return CostModel(
        chain=SOL, flat_per_leg_base_units=flat, proportional_bps_per_leg=bps,
        basis=EvidenceBasis.DERIVED, source="test",
    )


# ------------------------------------------------------------------ deriving the fees


def test_flat_cost_is_derived_from_our_own_trades(tmp_db):
    """The flat cost is measured, not the constant in the source."""
    seed_paper_history(tmp_db)
    model = derive_cost_model(SOL, tmp_db)
    assert model.basis is EvidenceBasis.DERIVED
    assert abs(model.flat_per_leg_base_units - TRUE_FLAT_PER_LEG) < TRUE_FLAT_PER_LEG // 20
    assert model.flat_per_leg_base_units != DECLARED_FLAT_PER_LEG[SOL]
    assert abs(model.proportional_bps_per_leg - TRUE_BPS_PER_LEG) < 15


def test_derivation_survives_a_take_profit_ladder_paying_a_third_tip(tmp_db):
    """Three-leg exits are the reason this is a median fit and not least squares."""
    seed_paper_history(tmp_db, n=14)
    for seq, cost in enumerate((31_000_000, 38_000_000, 42_000_000), start=100):
        proceeds = cost * 3 // 4
        fees = 3 * TRUE_FLAT_PER_LEG + int(Decimal(cost + proceeds) * TRUE_BPS_PER_LEG / 10_000)
        insert_trade(tmp_db, cost=cost, proceeds=proceeds, fees=fees, seq=seq)
    model = derive_cost_model(SOL, tmp_db)
    assert abs(model.flat_per_leg_base_units - TRUE_FLAT_PER_LEG) < TRUE_FLAT_PER_LEG // 20
    assert model.proportional_bps_per_leg < 150, "ladder tips must not inflate the fee rate"


def test_too_little_history_falls_back_to_the_declared_constant(tmp_db):
    seed_paper_history(tmp_db, n=MIN_SAMPLE_TRADES - 1)
    assert derive_cost_model(SOL, tmp_db).basis is EvidenceBasis.UNAVAILABLE
    model = cost_model(SOL, tmp_db)
    assert model.basis is EvidenceBasis.ESTIMATED
    assert model.flat_per_leg_base_units == DECLARED_FLAT_PER_LEG[SOL]


def test_one_size_of_trade_cannot_separate_flat_from_proportional(tmp_db):
    """Twenty identical trades fit infinitely many (flat, rate) pairs. Refuse to guess."""
    for seq in range(20):
        insert_trade(tmp_db, cost=16_000_000, proceeds=12_000_000, fees=2_350_000, seq=seq)
    model = derive_cost_model(SOL, tmp_db)
    assert model.basis is EvidenceBasis.UNAVAILABLE and "spread" in model.source


def test_a_live_sample_is_never_mixed_with_paper_fills(tmp_db):
    """A paper fill pays a modelled tip; a live fill pays a router. Different models."""
    seed_paper_history(tmp_db, n=14, mode="shadow")
    for seq, cost in enumerate((15_000_000, 20_000_000, 40_000_000, 60_000_000,
                                80_000_000, 100_000_000, 120_000_000, 150_000_000), start=200):
        proceeds = cost * 3 // 4
        fees = 2 * 2_500_000 + int(Decimal(cost + proceeds) * TRUE_BPS_PER_LEG / 10_000)
        insert_trade(tmp_db, cost=cost, proceeds=proceeds, fees=fees, mode="live", seq=seq)
    model = derive_cost_model(SOL, tmp_db)
    assert model.sample_modes == ("live",) and model.from_live_trades
    assert abs(model.flat_per_leg_base_units - 2_500_000) < 200_000


def test_a_chain_with_no_history_and_no_declared_constant_is_unavailable(tmp_db):
    """EVM gas is not a constant, so we decline to invent one."""
    assert Chain.BASE not in DECLARED_FLAT_PER_LEG
    model = cost_model(Chain.BASE, tmp_db)
    assert model.basis is EvidenceBasis.UNAVAILABLE and not model.known
    assert model.flat_per_leg_base_units is None, "missing is None, never 0"


def test_an_implausible_fit_is_refused_rather_than_used(tmp_db):
    for seq, cost in enumerate((10_000_000, 20_000_000, 40_000_000, 80_000_000, 100_000_000,
                                120_000_000, 140_000_000, 160_000_000, 180_000_000), start=1):
        insert_trade(tmp_db, cost=cost, proceeds=cost, fees=cost, seq=seq)  # 100% "fees"
    assert derive_cost_model(SOL, tmp_db).basis is EvidenceBasis.UNAVAILABLE


# ------------------------------------------------------------------ the two arms


@pytest.mark.parametrize(
    "size,flat_pct",
    [(5_000_000, "40.0"), (10_000_000, "20.0"), (20_000_000, "10.0"),
     (50_000_000, "4.0"), (100_000_000, "2.0"), (250_000_000, "0.8")],
)
def test_the_flat_share_falls_as_the_position_grows(size, flat_pct):
    """§5a's table: nothing but the size changes between these rows."""
    est = estimate_round_trip(SOL, size, model=_model(), depth=pool(DEEP_POOL_USD))
    assert Decimal(est.flat_base_units) * 100 / size == Decimal(flat_pct)
    assert Decimal(est.proportional_base_units) * 100 / size == Decimal("2.5")


@pytest.mark.parametrize(
    "size", [5_000_000, 10_000_000, 20_000_000, 50_000_000, 100_000_000, 250_000_000]
)
def test_the_impact_share_rises_as_the_position_grows(size):
    """The term the first version of this module was missing. It points the other way."""
    thin = estimate_round_trip(SOL, size, model=_model(), depth=pool(THIN_POOL_USD))
    bigger = estimate_round_trip(SOL, size * 2, model=_model(), depth=pool(THIN_POOL_USD))
    assert bigger.impact_bps_one_leg > thin.impact_bps_one_leg


def test_total_cost_is_u_shaped_and_has_a_minimum():
    """Falling flat plus rising impact: there is a best size, not just a floor."""
    depth, model = pool(DEEP_POOL_USD), _model()
    best, best_cost = optimal_size(SOL, model, depth)
    assert best is not None and best_cost is not None
    for step in (Decimal("0.5"), Decimal("0.8"), Decimal("1.25"), Decimal(2), Decimal(10)):
        other = estimate_round_trip(SOL, int(best * step), model=model, depth=depth).pct
        assert other >= best_cost, f"{step}x should not beat the optimum"


def test_a_deeper_pool_wants_a_larger_position():
    """The optimum is a property of the token, which is why it cannot be a config constant."""
    model = _model()
    thin, _ = optimal_size(SOL, model, pool("5000"))
    mid, _ = optimal_size(SOL, model, pool("100000"))
    deep, _ = optimal_size(SOL, model, pool(DEEP_POOL_USD))
    assert thin < mid < deep
    assert deep > 20 * thin, "two orders of magnitude of depth must move the answer"


def test_the_curve_arithmetic_is_the_one_in_curve_price():
    """Depth on a bonding curve is exact, and it is curve_price's own integer maths."""
    depth = curve()
    size = 50_000_000
    from kaiba.execution.curve_price import curve_leg_for_budget

    leg = curve_leg_for_budget(size)
    atoms, _after, capped = depth.state.buy_exact_in(leg)
    assert not capped
    expected = leg - atoms * depth.state.virtual_sol // depth.state.virtual_token
    assert depth.entry_impact(size) == expected
    # 0.05 SOL against 85.8 SOL of virtual reserve is a ~6 bps displacement. The fill
    # recorded on this exact state reports ``impact_bps: 130`` because curve_price
    # measures the effective price *including* the 125 bps platform fee; charging that
    # here would count the fee twice, which is why this reads the pre-fee arithmetic.
    assert 4 <= depth.entry_impact(size) * 10_000 // size <= 9


def test_a_buy_that_would_consume_the_curve_is_not_priceable():
    """pump.fun truncates such a buy, so the size we asked for is not a size we can get."""
    depth = curve()
    assert depth.entry_impact(10_000_000_000_000) is None


def test_pool_depth_agrees_with_the_broker_that_will_price_the_fill():
    """The gate must refuse exactly what the broker refuses, not hold a second opinion."""
    from kaiba.execution.paper import PaperBroker

    depth, size = pool(THIN_POOL_USD), 15_400_000
    size_usd = Decimal(size) / Decimal(10**9) * NATIVE_USD
    assert depth.entry_impact(size) * 10_000 // size == PaperBroker().impact_bps(
        size_usd, THIN_POOL_USD
    )


def test_the_recorded_thin_pool_reproduces_its_recorded_impact():
    """order_events for a real migration-fade buy: $25.42 of depth, 1069 bps on 0.0154 SOL."""
    est = estimate_round_trip(SOL, 15_401_000, model=_model(), depth=pool(THIN_POOL_USD))
    assert 1050 <= est.impact_bps_one_leg <= 1090


# ------------------------------------------------------------------ the band


def test_the_band_has_both_ends(write_risk):
    band = sizing_band(SOL, model=_model(), depth=pool("50000"), token=TOKEN)
    assert band.viable and band.reason == "band"
    assert band.min_viable_base_units < band.optimal_base_units < band.max_viable_base_units
    assert check_size(SOL, band.min_viable_base_units, model=_model(), depth=pool("50000"),
                      token=TOKEN).ok
    assert check_size(SOL, band.optimal_base_units, model=_model(), depth=pool("50000"),
                      token=TOKEN).ok
    assert not check_size(SOL, band.min_viable_base_units - 1_000_000, model=_model(),
                          depth=pool("50000"), token=TOKEN).ok
    assert not check_size(SOL, band.upper + 10_000_000, model=_model(),
                          depth=pool("50000"), token=TOKEN).ok


def test_too_large_is_refused_for_being_too_large(write_risk):
    """The regression: 0.06 SOL passed the fee-only gate and the broker refused it after."""
    verdict = check_size(SOL, 60_000_000, model=_model(), depth=pool(THIN_POOL_USD), token=TOKEN)
    assert not verdict.ok
    assert "no_viable_size" in verdict.reason or "above_" in verdict.reason


def test_a_pool_too_thin_for_any_size_says_exactly_that(write_risk):
    """The complete answer: on this token there is no size at which the trade is economic."""
    band = sizing_band(SOL, model=_model(), depth=pool(THIN_POOL_USD), token=TOKEN)
    assert not band.viable
    assert band.reason.startswith("no_viable_size")
    # And the cheapest size available is still far above the ceiling, not marginally so.
    assert band.optimal_cost_pct > 3 * DEFAULT_MAX_ROUND_TRIP_COST_PCT


def test_the_executable_bound_is_the_brokers_own_refusal_criterion(write_risk):
    """Above it the order does not fill; the gate should know that before it sizes."""
    from kaiba.execution.paper import DEFAULT_SLIPPAGE_FLOOR_BPS

    depth = pool("20000")
    tolerance = 2500 - DEFAULT_SLIPPAGE_FLOOR_BPS
    largest = max_executable_size(SOL, depth, tolerance)
    assert depth.entry_impact(largest) * 10_000 // largest <= tolerance
    over = int(largest * 1.2)
    assert depth.entry_impact(over) * 10_000 // over > tolerance


def test_executability_can_bind_before_economics(write_risk):
    """A tight slippage tolerance shrinks the band from the top, not the bottom."""
    depth = pool("50000")
    wide = sizing_band(SOL, model=_model(), depth=depth, token=TOKEN, tolerance_bps=2470)
    tight = sizing_band(SOL, model=_model(), depth=depth, token=TOKEN, tolerance_bps=100)
    assert tight.upper < wide.upper
    assert tight.min_viable_base_units == wide.min_viable_base_units


def test_the_impact_convention_is_reported_not_hidden(write_risk):
    """Charging impact 0, 1 or 2 times moves the optimum; the band reports all three."""
    band = sizing_band(
        SOL, model=_model(), depth=pool("50000"), token=TOKEN, with_alternates=True
    )
    zero, one, two = band.optimal_by_impact_legs
    assert zero is not None and one is not None and two is not None
    assert zero > one > two, "charging impact harder must shrink the optimum"
    assert one == band.optimal_base_units


# ------------------------------------------------------------------ depth resolution


def test_depth_comes_from_the_tokens_own_curve_when_it_has_one(tmp_db):
    state = curve().state
    tmp_db.execute(
        "INSERT INTO curve_snapshots (chain, token, observed_ms, real_sol_lamports, "
        "virtual_sol_lamports, real_token_atoms, virtual_token_atoms, created_ms, source) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (SOL.value, TOKEN, now_ms(), str(state.real_sol), str(state.virtual_sol),
         str(state.real_token), str(state.virtual_token), now_ms(), "pumpfun"),
    )
    depth = resolve_depth(SOL, TOKEN, tmp_db)
    assert isinstance(depth, CurveDepth)
    assert depth.basis is EvidenceBasis.VERIFIED_ONCHAIN


def test_a_stale_curve_snapshot_is_not_this_tokens_depth(tmp_db):
    """The curves in our own record moved by whole SOL inside minutes."""
    state = curve().state
    tmp_db.execute(
        "INSERT INTO curve_snapshots (chain, token, observed_ms, real_sol_lamports, "
        "virtual_sol_lamports, real_token_atoms, virtual_token_atoms, created_ms, source) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (SOL.value, TOKEN, now_ms() - 3_600_000, str(state.real_sol), str(state.virtual_sol),
         str(state.real_token), str(state.virtual_token), now_ms(), "pumpfun"),
    )
    assert isinstance(resolve_depth(SOL, TOKEN, tmp_db), NoDepth)


def seed_native_price(conn, usd: str = "197.5") -> None:
    """A SOL/USD sample, without which a USD pool depth cannot be compared to our size."""
    conn.execute(
        "INSERT INTO native_prices (chain, ts_ms, price_usd, source) VALUES (?,?,?,?)",
        (SOL.value, now_ms(), usd, "test"),
    )


def seed_dossier(conn, token: str = TOKEN, *, liquidity_usd: str = "40000", age_s: int = 0) -> None:
    """A dossier carrying a pool depth, as the DYOR layer writes it."""
    from kaiba.core.schemas import EvidenceBasis as EB
    from kaiba.core.schemas import Measure, Receipt, TokenDossier

    dossier = TokenDossier(
        address=token, chain=SOL,
        liquidity_usd=Measure(
            value=Decimal(liquidity_usd), basis=EB.PROVIDER_REPORTED,
            receipt=Receipt(provider="dexscreener", endpoint="price.token_pairs",
                            observed_at_ms=now_ms() - age_s * 1000),
            freshness_budget_s=600,
        ),
    )
    conn.execute(
        "INSERT INTO token_dossiers (chain, address, dossier_json, built_at_ms) VALUES (?,?,?,?)",
        (SOL.value, token, dossier.model_dump_json(), now_ms()),
    )


def mark_graduated(conn, token: str = TOKEN) -> None:
    conn.execute(
        "INSERT INTO tokens (chain, address, first_seen_ms, migrated_ms) VALUES (?,?,?,?) "
        "ON CONFLICT(chain, address) DO UPDATE SET migrated_ms=excluded.migrated_ms",
        (SOL.value, token, now_ms(), now_ms()),
    )


class FakePair:
    """The two fields :func:`pair_depth` reads off a ``dexscreener.PairSnapshot``."""

    def __init__(self, liquidity_usd: str, base: str = TOKEN, quote: str = "So111", dex="raydium"):
        self.liquidity_usd = Decimal(liquidity_usd)
        self.base_address, self.quote_address, self.dex_id = base, quote, dex


@pytest.fixture
def fake_pairs(monkeypatch):
    """Stand in for the one network call, and record that it happened."""
    from kaiba.providers import dexscreener

    calls: list[str] = []

    def _install(pairs, *, raises: bool = False):
        def fake(chain, token, **kw):
            calls.append(token)
            if raises:
                raise RuntimeError("provider down")
            from kaiba.core.schemas import Receipt
            return list(pairs), Receipt(provider="dexscreener", endpoint="price.token_pairs")

        monkeypatch.setattr(dexscreener, "token_pairs", fake)
        return calls

    return _install


def test_a_graduated_token_is_priced_from_its_pair(tmp_db, fake_pairs):
    """The gap: migration-fade trades the migration, so every token it sees has no curve."""
    calls = fake_pairs([FakePair("42000")])
    seed_native_price(tmp_db)
    mark_graduated(tmp_db)
    depth = resolve_depth(SOL, TOKEN, tmp_db)
    assert isinstance(depth, PoolDepth) and depth.liquidity_usd == Decimal("42000")
    assert depth.source.startswith("pair:raydium")
    assert "graduated" in depth.source
    assert calls == [TOKEN], "exactly one provider call for one decision"


def test_a_graduated_token_is_never_asked_for_a_curve(tmp_db, fake_pairs):
    """``tokens.migrated_ms`` is set, so the curve is gone however fresh a snapshot looks."""
    fake_pairs([FakePair("42000")])
    seed_native_price(tmp_db)
    _snapshot(tmp_db)          # a fresh curve snapshot that must NOT be used
    mark_graduated(tmp_db)
    depth = resolve_depth(SOL, TOKEN, tmp_db)
    assert isinstance(depth, PoolDepth), "a migrated token has no bonding curve"


def test_the_deepest_single_pool_wins_not_their_sum(tmp_db, fake_pairs):
    """Our broker fills one route. Summing pools claims depth we cannot reach."""
    fake_pairs([FakePair("1000"), FakePair("9000"), FakePair("300")])
    seed_native_price(tmp_db)
    mark_graduated(tmp_db)
    depth = resolve_depth(SOL, TOKEN, tmp_db)
    assert depth.liquidity_usd == Decimal("9000")
    assert "3pools" in depth.source


def test_a_pool_holding_the_token_on_the_quote_side_still_counts(tmp_db, fake_pairs):
    fake_pairs([FakePair("5000", base="OTHER", quote=TOKEN)])
    seed_native_price(tmp_db)
    mark_graduated(tmp_db)
    assert isinstance(resolve_depth(SOL, TOKEN, tmp_db), PoolDepth)


def test_a_fresh_dossier_is_used_before_the_network(tmp_db, fake_pairs):
    """Free and local first: the probe is for when the dossier cannot answer."""
    calls = fake_pairs([FakePair("42000")])
    seed_native_price(tmp_db)
    seed_dossier(tmp_db, liquidity_usd="31000", age_s=30)
    mark_graduated(tmp_db)
    depth = resolve_depth(SOL, TOKEN, tmp_db)
    assert depth.liquidity_usd == Decimal("31000") and depth.source.startswith("dossier:")
    assert calls == [], "a fresh local answer must not cost a request"


def test_a_stale_dossier_falls_through_to_the_pair_rather_than_refusing(tmp_db, fake_pairs):
    """The dossier's own budget is 600 s. Expiring it is a reason to look, not to refuse."""
    calls = fake_pairs([FakePair("42000")])
    seed_native_price(tmp_db)
    seed_dossier(tmp_db, age_s=4_000)
    mark_graduated(tmp_db)
    depth = resolve_depth(SOL, TOKEN, tmp_db)
    assert isinstance(depth, PoolDepth) and depth.liquidity_usd == Decimal("42000")
    assert "dossier_liquidity_stale" in depth.source and calls == [TOKEN]


def test_freshness_is_measured_at_the_decision_not_at_the_wall_clock(tmp_db, fake_pairs):
    """Replaying an old decision must ask what we knew then.

    ``Measure.stale`` ages against ``now``, so a replay of yesterday's decisions calls
    every dossier stale — which is how the post-graduation gap was first measured, and
    it would also have hidden this test.
    """
    fake_pairs([])
    seed_native_price(tmp_db)
    seed_dossier(tmp_db, liquidity_usd="31000", age_s=4_000)
    mark_graduated(tmp_db)

    # Asked as of 50 s after the dossier was observed, it is inside its own 600 s budget.
    at_decision = now_ms() - 4_000_000 + 50_000
    then = resolve_depth(SOL, TOKEN, tmp_db, at_ms=at_decision)
    assert isinstance(then, PoolDepth) and then.liquidity_usd == Decimal("31000")

    # Asked as of now, the same row is an hour past its budget and there is no pair.
    assert isinstance(resolve_depth(SOL, TOKEN, tmp_db), NoDepth)


def test_no_native_price_means_no_probe_and_no_guess(tmp_db, fake_pairs):
    """A USD depth and a lamport size are not comparable without one, and guessing is
    how a $25 pool comes to look tradable. Checked before the call, so it costs nothing."""
    calls = fake_pairs([FakePair("42000")])
    mark_graduated(tmp_db)
    depth = resolve_depth(SOL, TOKEN, tmp_db)
    assert isinstance(depth, NoDepth) and "no_native_usd_price" in depth.source
    assert calls == [], "never spend a request we could not have used"


def test_the_probe_can_be_switched_off_for_an_offline_caller(tmp_db, fake_pairs):
    calls = fake_pairs([FakePair("42000")])
    seed_native_price(tmp_db)
    mark_graduated(tmp_db)
    depth = resolve_depth(SOL, TOKEN, tmp_db, allow_network=False)
    assert isinstance(depth, NoDepth) and depth.source.endswith("probe_disabled")
    assert calls == []


def test_a_dead_provider_refuses_rather_than_raising(tmp_db, fake_pairs):
    fake_pairs([FakePair("42000")], raises=True)
    seed_native_price(tmp_db)
    mark_graduated(tmp_db)
    depth = resolve_depth(SOL, TOKEN, tmp_db)
    assert isinstance(depth, NoDepth) and "pair_probe_failed" in depth.source


def test_a_limiter_refusal_is_waited_out_not_taken_as_an_answer(tmp_db, monkeypatch):
    """`_http` never blocks for capacity, so an UNAVAILABLE here is often just spacing.

    Measured: two probes in a row return "rate limited: dexscreener: minimum interval
    (retry in 0.2s)". Treating that as "no depth" would refuse a live entry over 200 ms.
    """
    from kaiba.core.schemas import Receipt
    from kaiba.providers import dexscreener

    calls: list[int] = []
    slept: list[bool] = []

    def fake(chain, token, **kw):
        calls.append(1)
        if len(calls) == 1:
            return [], Receipt(
                provider="dexscreener", endpoint="price.token_pairs",
                basis=EvidenceBasis.UNAVAILABLE,
                note="rate limited: dexscreener: minimum interval (retry in 0.2s)",
            )
        return [FakePair("42000")], Receipt(provider="dexscreener", endpoint="price.token_pairs")

    monkeypatch.setattr(dexscreener, "token_pairs", fake)
    monkeypatch.setattr(dexscreener, "pace", lambda *a, **k: slept.append(True))
    seed_native_price(tmp_db)
    mark_graduated(tmp_db)
    depth = resolve_depth(SOL, TOKEN, tmp_db)
    assert isinstance(depth, PoolDepth) and depth.liquidity_usd == Decimal("42000")
    assert len(calls) == 2 and slept == [True], "one paced retry, not a busy loop"


def test_a_probe_that_keeps_failing_refuses_rather_than_retrying_forever(tmp_db, monkeypatch):
    from kaiba.core.schemas import Receipt
    from kaiba.providers import dexscreener

    calls: list[int] = []

    def fake(chain, token, **kw):
        calls.append(1)
        return [], Receipt(
            provider="dexscreener", endpoint="price.token_pairs",
            basis=EvidenceBasis.UNAVAILABLE, note="rate limited: minimum interval",
        )

    monkeypatch.setattr(dexscreener, "token_pairs", fake)
    monkeypatch.setattr(dexscreener, "pace", lambda *a, **k: None)
    seed_native_price(tmp_db)
    mark_graduated(tmp_db)
    assert isinstance(resolve_depth(SOL, TOKEN, tmp_db), NoDepth)
    assert len(calls) == 1 + viability.PAIR_PROBE_RETRIES


def test_a_pair_with_no_liquidity_is_not_a_depth(tmp_db, fake_pairs):
    fake_pairs([])
    seed_native_price(tmp_db)
    mark_graduated(tmp_db)
    depth = resolve_depth(SOL, TOKEN, tmp_db)
    assert isinstance(depth, NoDepth) and "none_with_liquidity" in depth.source


def test_a_graduated_token_gets_a_band_end_to_end(write_risk, tmp_db, fake_pairs):
    """The whole point: an entry on a migrated token is now priced, sized and admitted."""
    fake_pairs([FakePair("42000")])
    seed_paper_history(tmp_db)
    seed_native_price(tmp_db)
    mark_graduated(tmp_db)
    band = sizing_band(SOL, tmp_db, token=TOKEN)
    assert band.viable, band.reason
    # On a $42k pool the cost-optimal size is larger than the chain budget allows, which
    # is the envelope binding before the economics do — the common case at our bankroll.
    assert band.optimal_base_units > 500_000_000
    size = 200_000_000
    assert band.contains(size)
    decision = RiskGate().check_entry(SOL, LANE, size, tmp_db, token=TOKEN)
    assert decision.allowed, decision.reason
    assert any("depth_basis:provider_reported:pair:" in f for f in decision.findings)


def test_an_unpriceable_token_refuses_the_entry(tmp_db, write_risk):
    """Fail closed. Unknown depth is not free depth — it is how 0.06 SOL met a $25 pool."""
    verdict = check_size(SOL, 100_000_000, tmp_db, token=TOKEN)
    assert not verdict.ok
    assert verdict.reason.startswith("round_trip_cost_unknown:sol")
    assert "depth_basis:unavailable" in " ".join(verdict.findings)


def test_fail_closed_is_a_declared_choice_not_an_accident():
    assert viability.UNKNOWN_COST_FAILS_CLOSED is True
    assert "UNKNOWN_COST_FAILS_CLOSED" in PROVENANCE


def test_an_executable_round_trip_can_supply_the_depth():
    """jupiter.round_trip is two quotes; its cost includes the fee, so the fee comes out.

    Never priceImpactPct: it returns exact 0 on a split route.
    """
    size = 20_000_000
    fee = 2 * CURVE_TOTAL_FEE_BPS
    depth = pool_depth_from_round_trip(size, cost_bps=fee + 500, native_usd=NATIVE_USD)
    assert isinstance(depth, PoolDepth)
    # 500 bps of residual impact at this size implies a pool about 19x the size.
    assert 450 <= depth.entry_impact(size) * 10_000 // size <= 550
    # A round trip that costs no more than the fee implies no measurable impact, which is
    # not the same as a measured zero: refuse rather than report a free pool.
    assert isinstance(pool_depth_from_round_trip(size, cost_bps=fee), NoDepth)


# ------------------------------------------------------------------ the ceiling


def test_the_ceiling_comes_from_the_operators_bounds_block(write_risk):
    write_risk(bounds={"max_round_trip_cost_pct": 3.25})
    assert configured_ceiling_pct() == Decimal("3.25")


def test_an_absent_ceiling_uses_the_declared_default_rather_than_switching_off(write_risk):
    path = write_risk()
    assert "max_round_trip_cost_pct" not in yaml.safe_load(
        Path(path).read_text(encoding="utf-8")
    )["bounds"]
    assert configured_ceiling_pct() == DEFAULT_MAX_ROUND_TRIP_COST_PCT


@pytest.mark.parametrize("bad", [0, -1])
def test_an_unusable_ceiling_refuses_rather_than_inventing_one(write_risk, bad):
    """A number the loader accepts but no trade can satisfy is a config error, not a dial."""
    write_risk(bounds={"max_round_trip_cost_pct": bad})
    assert configured_ceiling_pct() is None
    verdict = check_size(SOL, 100_000_000, model=_model(), depth=pool("50000"), token=TOKEN)
    assert not verdict.ok and verdict.reason.startswith("round_trip_ceiling_invalid")


@pytest.mark.parametrize("bad", ["wide", True])
def test_a_non_numeric_ceiling_is_rejected_by_the_config_loader(write_risk, bad):
    """Typed on ``EnvelopeBounds``, so the file will not load at all — which is louder
    than anything this module could do, and it happens before a trade is sized."""
    from pydantic import ValidationError

    from kaiba.core.config import get_risk

    write_risk(bounds={"max_round_trip_cost_pct": bad})
    with pytest.raises(ValidationError):
        get_risk()


def test_the_executability_arm_reuses_the_operators_existing_slippage_bound(write_risk):
    """No new knob: bounds.max_slippage_bps already exists and is already theirs."""
    from kaiba.execution.paper import DEFAULT_SLIPPAGE_FLOOR_BPS

    write_risk(bounds={"max_slippage_bps": 900})
    assert viability.slippage_tolerance_bps() == 900 - DEFAULT_SLIPPAGE_FLOOR_BPS


def test_the_ceiling_is_reread_on_every_check(write_risk):
    """Same reason risk.py re-reads: the file is edited while the agent runs."""
    depth = pool("50000")
    write_risk(bounds={"max_round_trip_cost_pct": 50.0})
    assert check_size(SOL, 20_000_000, model=_model(), depth=depth, token=TOKEN).ok
    write_risk(bounds={"max_round_trip_cost_pct": 0.5})
    assert not check_size(SOL, 20_000_000, model=_model(), depth=depth, token=TOKEN).ok


# ------------------------------------------------------------------ wired into the gate


def _snapshot(conn, token: str = TOKEN, *, thin: bool = False) -> None:
    state = (thin_curve() if thin else curve()).state
    conn.execute(
        "INSERT INTO curve_snapshots (chain, token, observed_ms, real_sol_lamports, "
        "virtual_sol_lamports, real_token_atoms, virtual_token_atoms, created_ms, source) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (SOL.value, token, now_ms(), str(state.real_sol), str(state.virtual_sol),
         str(state.real_token), str(state.virtual_token), now_ms(), "pumpfun"),
    )


def test_an_entry_below_the_economic_floor_is_refused(write_risk, tmp_db):
    """Remove the gate from ``RiskGate.check_entry`` and this test fails."""
    seed_paper_history(tmp_db)
    _snapshot(tmp_db)
    decision = RiskGate().check_entry(SOL, LANE, 5_000_000, tmp_db, token=TOKEN)
    assert not decision.allowed
    assert decision.reason.startswith("below_economic_floor:")


def test_an_entry_inside_the_band_is_allowed_and_reports_its_cost(write_risk, tmp_db):
    seed_paper_history(tmp_db)
    _snapshot(tmp_db)
    band = sizing_band(SOL, tmp_db, token=TOKEN)
    assert band.viable, band.reason
    decision = RiskGate().check_entry(SOL, LANE, band.optimal_base_units, tmp_db, token=TOKEN)
    assert decision.allowed, decision.reason
    assert any(f.startswith("round_trip_cost_pct:") for f in decision.findings)
    assert any(f.startswith("entry_impact_bps:") for f in decision.findings)
    assert any(f.startswith("optimal_base_units:") for f in decision.findings)


def test_an_entry_too_large_for_the_pool_is_refused_by_the_gate(write_risk, tmp_db):
    """The upper arm, end to end. Without it this is the 0.06 SOL order that got through."""
    seed_paper_history(tmp_db)
    _snapshot(tmp_db, thin=True)  # a curve with 1 SOL of virtual reserve
    decision = RiskGate().check_entry(SOL, LANE, 60_000_000, tmp_db, token=TOKEN)
    assert not decision.allowed
    assert "no_viable_size" in decision.reason or "above_" in decision.reason


def test_an_entry_on_an_unpriceable_token_is_refused_by_the_gate(write_risk, tmp_db):
    """No curve, no dossier: the size is unpriceable and unpriceable refuses.

    This is the contract change that ``tests/test_risk.py`` has not caught up with — six
    of its entry tests assert that 0.1 SOL is allowed on a token with no depth data at
    all, which a depth-aware gate cannot honour. Their fixture needs a curve snapshot or
    a dossier row; see the handover note.
    """
    seed_paper_history(tmp_db)
    decision = RiskGate().check_entry(SOL, LANE, 100_000_000, tmp_db, token=TOKEN)
    assert not decision.allowed and decision.reason.startswith("round_trip_cost_unknown")


def test_a_tokenless_check_enforces_the_fee_arm_and_says_the_other_was_skipped(write_risk, tmp_db):
    """Depth is per token. With no token there is no depth — and no pretence of one."""
    seed_paper_history(tmp_db)
    allowed = RiskGate().check_entry(SOL, LANE, 200_000_000, tmp_db)
    assert allowed.allowed
    assert "impact_arm_skipped:no_token" in allowed.findings
    refused = RiskGate().check_entry(SOL, LANE, 5_000_000, tmp_db)
    assert not refused.allowed and refused.reason.endswith(":fees_only")


def test_the_engine_always_names_the_token_it_is_sizing():
    """The tokenless path must stay unreachable from production, or it is a bypass."""
    import inspect

    from kaiba.execution import engine

    source = inspect.getsource(engine._risk_refusal)
    assert "token=signal.token" in source


def test_bankroll_unfunded_still_wins_over_the_sizing_band(write_risk, tmp_db):
    """Ordering: "no money on this chain" is a truer answer than "wrong size"."""
    write_risk(chains={"sol": {"bankroll_base_units": 0}})
    decision = RiskGate().check_entry(SOL, LANE, 5_000_000, tmp_db, token=TOKEN)
    assert decision.reason == "bankroll_unfunded"


# ------------------------------------------------------------------ exits are never gated


def test_an_exit_is_never_refused_however_bad_the_cost(write_risk, tmp_db):
    """**Mutation target.** Wire the sizing gate into ``check_exit`` and this fails.

    A refused sell strands the position permanently. The flat fee is paid once; a
    position you cannot close is paid in full. Asserted in the same conditions that
    refuse an entry.
    """
    seed_paper_history(tmp_db)
    _snapshot(tmp_db)
    gate = RiskGate()
    assert not gate.check_entry(SOL, LANE, 1_000_000, tmp_db, token=TOKEN).allowed
    decision = gate.check_exit(SOL, LANE, tmp_db)
    assert decision.allowed and decision.reason == "exit_never_blocked"


def test_an_exit_is_allowed_on_a_token_whose_depth_is_unknown(write_risk, tmp_db):
    """Fail-closed on entries must never become fail-closed on exits."""
    seed_paper_history(tmp_db)
    assert not RiskGate().check_entry(SOL, LANE, 100_000_000, tmp_db, token=TOKEN).allowed
    assert RiskGate().check_exit(SOL, LANE, tmp_db).allowed


def test_an_exit_is_allowed_when_the_position_is_too_large_for_the_pool(write_risk, tmp_db):
    """The dangerous case: the only way out of an oversized position is through it."""
    seed_paper_history(tmp_db)
    _snapshot(tmp_db, thin=True)
    assert not RiskGate().check_entry(SOL, LANE, 60_000_000, tmp_db, token=TOKEN).allowed
    assert RiskGate().check_exit(SOL, LANE, tmp_db).allowed


def test_the_viability_module_is_unreachable_from_check_exit():
    """Structural, not behavioural: ``check_exit``'s source must not mention the gate."""
    import inspect

    source = inspect.getsource(RiskGate.check_exit)
    assert "viability" not in source and "check_size" not in source


# ------------------------------------------------------------------ provenance


def _module_constants() -> set[str]:
    tree = ast.parse(Path(viability.__file__).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = [t for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target]
        for target in targets:
            if target.id.isupper():
                names.add(target.id)
    return names


def test_every_constant_has_provenance():
    """Fails on a silently-added knob.

    A number that appears in this module without a row saying where it came from is the
    exact failure the gate exists to prevent: an unmeasured constant nobody re-checks.
    """
    assert _module_constants() == set(PROVENANCE) | {"PROVENANCE"}


def test_provenance_rows_are_filled_in():
    for name, row in PROVENANCE.items():
        assert row.value == getattr(viability, name), f"{name}: provenance value is stale"
        assert row.unit and row.source and row.note


def test_the_declared_constants_are_solana_only():
    """One number per chain, and no number where we do not have one."""
    assert set(DECLARED_FLAT_PER_LEG) == {Chain.SOL}
    assert set(DECLARED_PROPORTIONAL_BPS_PER_LEG) == {Chain.SOL}
    assert declared_cost_model(Chain.ETH).basis is EvidenceBasis.UNAVAILABLE


def test_the_venue_fee_is_imported_not_retyped():
    """One number for pump.fun's fee in the tree, and it lives in curve_price."""
    assert DECLARED_PROPORTIONAL_BPS_PER_LEG[Chain.SOL] == CURVE_TOTAL_FEE_BPS
