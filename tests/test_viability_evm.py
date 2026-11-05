"""The EVM arm of the sizing gate: a venue read per token, or a refusal that names the hole.

Before this existed, ``check_size`` refused every EVM entry at every size with
``round_trip_cost_unknown`` — there was no flat cost (gas is not a constant), no
proportional cost (the venue fee is in the token's own contract) and no depth (the only
depth source in the tree was a Solana ``curve_snapshots`` row). bsc and robinhood could
not trade no matter what else was fixed.

Four properties are load-bearing here and each has a test that fails if it is removed:

1. **the token's tax is charged** — it is the term that decides an EVM round trip, and a
   fixture that only differs in ``creatorTaxBps`` flips from a band to no viable size;
2. **an unreadable tax refuses** — never 0, which is what the provider says for 836 of
   836 robinhood tokens, including ones the chain says charge 150 and 180 bps;
3. **the depth is the curve's own arithmetic** — pinned against a real fill this
   arithmetic has to reproduce to the atom;
4. **a chain with no venue reader refuses** rather than being priced as free.

Every number in ``CURVE`` and ``GOLDEN`` was read off Robinhood Chain on 2026-09-21 and
is quoted with the block it came from. Nothing here touches the network: the RPC batch is
replaced, and a test that reached a provider would be a live test (``conftest`` skips
those unless ``KAIBA_LIVE_TESTS=1``).
"""

from __future__ import annotations

import copy
import json
from decimal import Decimal

import pytest
import yaml

from kaiba.core.schemas import Chain, EvidenceBasis, now_ms
from kaiba.execution import viability
from kaiba.execution import evm_price
from kaiba.execution.evm_price import FlapRecord, parse_flap_record
from kaiba.execution.viability import (
    DECLARED_FLAT_PER_LEG,
    EVM_SWAP_GAS_UNITS,
    ROUTER_BPS_PER_LEG,
    CostModel,
    NoDepth,
    PonsCurveDepth,
    VenueRead,
    check_size,
    cost_model,
    estimate_round_trip,
    evm_cost_model,
    read_venue,
    reset_venue_cache,
    resolve_depth,
    sizing_band,
)

RH = Chain.ROBINHOOD
BSC = Chain.BSC
TOKEN = "0x2c4e5fbc750cf76b9294b44f1a320698267d3ee7"
CURVE_ADDRESS = "0x3ccc7163beec0f8396b85847950e54de5507221f"
BSC_FLAP_TOKEN = "0x" + "ab" * 20

#: Gas price read from ``eth_gasPrice`` on 2026-09-21 alongside the receipts behind
#: :data:`~kaiba.execution.viability.EVM_SWAP_GAS_UNITS`: 0.0484 gwei. A day earlier the
#: same endpoint reported 0.174 gwei, which is why the module reads it and never declares it.
GAS_PRICE_WEI = 48_398_000

#: ``CURVE_READS`` off curve 0x3ccc7163… as it stood on 2026-09-21, in that tuple's order.
#: ``launched_at`` is the one value the fixtures compute instead of quoting, because the
#: anti-sniper tax is a function of *elapsed* time and a frozen timestamp would make this
#: file's meaning depend on the year it is run in. The live value was 1789991064.
CURVE: dict[str, int] = {
    "quote_reserve": 1_717_050_000_000_000_000,
    "real_quote_reserve": 37_050_000_000_000_000,
    "sellable_tokens": 692_708_008_336_557_301_351_570_594,
    "reserved_tokens": 285_714_285_714_285_714_285_714_285,
    "graduation_threshold": 4_200_000_000_000_000_000,
    "launch_supply": 1_000_000_000_000_000_000_000_000_000,
    "fee_bps": 100,
    "launched_at": 0,  # replaced per fixture, see above
    "snipe_tax_start_bps": 9_900,
    "snipe_tax_seconds": 3,
}

#: The operator's own robinhood sizes from ``config/risk.yaml`` (0.00435 and 0.0075 ETH).
#: The gate has to have an opinion at the sizes the agent will actually ask about.
MIN_POSITION = 4_350_000_000_000_000
MAX_POSITION = 7_500_000_000_000_000

BASE_RISK: dict = {
    "version": "v1",
    "global_mode": "shadow",
    "kill_switch": False,
    "entries_paused": False,
    "reduce_only": False,
    "bounds": {
        "max_size_pct_bankroll": 5.0,
        "max_daily_loss_pct": 10.0,
        "max_slippage_bps": 2500,
        "max_concurrent_positions": None,
        "max_lane_mode": "shadow",
        "allow_self_promotion": False,
        "max_round_trip_cost_pct": 7.0,
    },
    "chains": {
        "robinhood": {
            "enabled": True,
            "bankroll_base_units": 150_000_000_000_000_000,
            "max_position_base_units": MAX_POSITION,
            "min_position_base_units": MIN_POSITION,
            "gas_reserve_base_units": 10_000_000_000_000_000,
            "daily_loss_stop_base_units": 3_000_000_000_000_000,
            "max_exposure_pct": 10.0,
            "wallet": None,
        },
    },
    "lanes": {},
    "protection": {},
}


# --------------------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def _clean_venue_cache():
    """The memo is process-global; a test must not inherit another test's curve."""
    reset_venue_cache()
    yield
    reset_venue_cache()


@pytest.fixture
def risk_yaml(tmp_path, monkeypatch):
    path = tmp_path / "risk.yaml"
    monkeypatch.setenv("KAIBA_RISK_PATH", str(path))
    path.write_text(yaml.safe_dump(copy.deepcopy(BASE_RISK)), encoding="utf-8")
    return path


def register_token(conn, *, token: str = TOKEN, curve: str = CURVE_ADDRESS,
                   quote_is_native: bool = True, migrated_ms: int | None = None) -> None:
    """A ``tokens`` row shaped exactly as ``ingest.robinhood`` writes one at launch."""
    conn.execute(
        "INSERT OR REPLACE INTO tokens (chain, address, launchpad, pool, created_ms, "
        "migrated_ms, first_seen_ms, meta_json) VALUES (?,?,?,?,?,?,?,?)",
        (RH.value, token, "pons", curve, now_ms() - 3_600_000, migrated_ms, now_ms(),
         json.dumps({"curve": curve, "pair_token": "0x" + "0" * 40,
                     "quote_is_native": quote_is_native})),
    )
    conn.commit()


def _word(value: int) -> str:
    return "0x" + f"{int(value):064x}"


def block_header(timestamp_s: int, number: int = 69_108_111) -> dict:
    """The part of an ``eth_getBlockByNumber`` header the reader uses, shaped as the node
    sends it: hex quantities, not ints."""
    return {"number": hex(number), "timestamp": hex(int(timestamp_s)), "hash": "0x" + "ab" * 32}


def batch_results(
    *, creator_tax_bps: int = 0, gas_price_wei: int = GAS_PRICE_WEI,
    age_s: int = 3_600, launched_at_s: int | None = None, head_ts_s: int | None = None,
    **overrides: int,
) -> list:
    """The twelve ``eth_call`` results ``read_pons_venue`` asks for, in order — and, when
    ``head_ts_s`` is given, the head block header it asks for as the thirteenth item.

    Without a header the reader falls back to the wall clock less the lag margin, which is
    what a batch whose header failed looks like; ``launched_at_s`` pins the launch to a
    real second so a test can pin the clock against it, where ``age_s`` measures from
    whatever ``now_ms()`` says.
    """
    state = {**CURVE, **overrides}
    state["launched_at"] = (
        int(launched_at_s) if launched_at_s is not None else now_ms() // 1000 - int(age_s)
    )
    from kaiba.ingest.robinhood import CURVE_READS

    out: list = [_word(state[name]) for name, _ in CURVE_READS]
    out.append(_word(creator_tax_bps))
    out.append(_word(gas_price_wei))
    if head_ts_s is not None:
        out.append(block_header(head_ts_s))
    return out


@pytest.fixture
def rpc(monkeypatch):
    """Replace the chain with a canned batch and count the round trips it costs."""
    from kaiba.ingest import robinhood as pons

    state: dict = {"results": batch_results(), "ok": True, "note": None, "calls": []}

    def fake(calls, **kwargs):
        state["calls"].append(list(calls))
        return pons.RpcResult(list(state["results"]), state["ok"], state["note"], 1)

    monkeypatch.setattr(pons, "rpc_batch", fake)
    return state


def band_for(conn, **kw):
    return sizing_band(RH, conn, token=TOKEN, **kw)


# --------------------------------------------------------- the tax is the binding term


def test_a_pons_curve_prices_a_band_at_the_sizes_the_operator_configured(tmp_db, rpc, risk_yaml):
    """The whole point: an EVM entry that used to be refused now has a size window.

    0.00435-0.0075 ETH is what ``config/risk.yaml`` authorises on this chain, so a band
    that exists but excludes both numbers would still mean the agent never trades.
    """
    register_token(tmp_db)
    rpc["results"] = batch_results(creator_tax_bps=0)
    band = band_for(tmp_db)

    assert band.reason == "band", band.reason
    assert band.viable
    assert band.contains(MIN_POSITION) and band.contains(MAX_POSITION)
    assert check_size(RH, MIN_POSITION, tmp_db, token=TOKEN).ok
    assert check_size(RH, MAX_POSITION, tmp_db, token=TOKEN).ok
    assert band.depth.basis is EvidenceBasis.VERIFIED_ONCHAIN


def test_the_creator_tax_is_charged_and_it_is_what_closes_the_window(tmp_db, rpc, risk_yaml):
    """One number changes between these two runs and it decides whether we can trade.

    Measured on live curves: ``creatorTaxBps()`` reads 0, 150, 180, 200 and 300 across
    tokens minutes apart. At 300 bps a round trip is 2 x (100 venue + 300 tax + 100
    router) = 10% before gas or impact, against a 7% ceiling, so no size works. A cost
    model that dropped the tax term would price both of these the same.
    """
    register_token(tmp_db)

    rpc["results"] = batch_results(creator_tax_bps=0)
    free = band_for(tmp_db)
    reset_venue_cache()
    rpc["results"] = batch_results(creator_tax_bps=300)
    taxed = band_for(tmp_db)

    assert free.viable and free.optimal_cost_pct is not None
    assert not taxed.viable
    assert taxed.reason.startswith("no_viable_size:cheapest_is_"), taxed.reason
    assert taxed.optimal_cost_pct > Decimal(7)
    # 300 bps a leg on both legs is 6 points of cost, less a little: the tax is taken off
    # the top, so a taxed order puts 3% less into the pool and displaces it 3% less. The
    # gap being *just under* 6 is the coupling working; 0 would mean the tax is not
    # charged and exactly 6 would mean the depth never saw it.
    gap = taxed.optimal_cost_pct - free.optimal_cost_pct
    assert Decimal("5.9") < gap < Decimal(6), gap
    assert not check_size(RH, MIN_POSITION, tmp_db, token=TOKEN).ok


def test_the_proportional_term_is_venue_fee_plus_token_tax_plus_router(tmp_db, rpc):
    """Three parts, all of them, and the source string says which is which."""
    register_token(tmp_db)
    rpc["results"] = batch_results(creator_tax_bps=150)
    model = cost_model(RH, tmp_db, token=TOKEN)

    assert model.known
    assert model.proportional_bps_per_leg == Decimal(100 + 150 + ROUTER_BPS_PER_LEG)
    assert model.flat_per_leg_base_units == EVM_SWAP_GAS_UNITS * GAS_PRICE_WEI
    assert "fee100+tax150+router100bps" in model.source
    assert isinstance(model.flat_per_leg_base_units, int)
    assert isinstance(model.proportional_bps_per_leg, Decimal)


def test_an_unreadable_tax_refuses_instead_of_assuming_zero(tmp_db, rpc, risk_yaml):
    """A missing tax is the provider's zero wearing a different hat. Fail closed.

    ``0x`` is what an ``eth_call`` to a contract without the view returns, and it is
    exactly the case where assuming 0 would authorise a 5%-a-leg token.
    """
    register_token(tmp_db)
    results = batch_results(creator_tax_bps=0)
    results[-2] = "0x"
    rpc["results"] = results

    venue = read_venue(RH, TOKEN, tmp_db)
    assert venue.tax_bps_per_leg is None and not venue.priced
    assert isinstance(venue.depth, NoDepth)

    model = cost_model(RH, tmp_db, token=TOKEN)
    assert model.basis is EvidenceBasis.UNAVAILABLE and not model.known
    assert model.flat_per_leg_base_units is None, "missing is None, never 0"
    assert "token_tax" in model.source

    verdict = check_size(RH, MAX_POSITION, tmp_db, token=TOKEN)
    assert not verdict.ok
    assert "creator_tax_unreadable" in verdict.reason or "token_tax" in verdict.reason


def test_the_anti_sniper_window_refuses_every_size(tmp_db, rpc, risk_yaml, clock):
    """9,900 / 618 / 19 / 0 bps at 0 / 1 / 2 / 3+ s (MEASURED rung table,
    ``lanes.PONS_SNIPE_TAX_RUNGS_BPS``, from ``snipeTaxBps()``/``snipeTaxSeconds()`` eth_calls
    and 913k real ``CurveBuy`` events). The linear 6,600 this test once assumed was wrong
    by 10.7x. Never tradeable inside the window either way — and the window is measured
    on the chain's clock, so it is walked here with a head timestamp."""
    register_token(tmp_db)
    for chain_elapsed_s, rung_bps in ((0, 9_900), (1, 618), (2, 19)):
        rpc["results"] = batch_results(
            creator_tax_bps=0, launched_at_s=LAUNCHED_AT_S,
            head_ts_s=LAUNCHED_AT_S + chain_elapsed_s,
        )
        clock(60_000)  # the wall clock says a minute; the chain says otherwise
        venue = read_venue(RH, TOKEN, tmp_db)
        assert venue.tax_bps_per_leg == Decimal(rung_bps), "the rung is charged, and named"
        assert isinstance(venue.depth, NoDepth)
        assert f"rung{chain_elapsed_s}_{rung_bps}bps" in venue.note, venue.note
        verdict = check_size(RH, MAX_POSITION, tmp_db, token=TOKEN)
        assert not verdict.ok
        assert f"rung{chain_elapsed_s}_{rung_bps}bps" in verdict.reason, verdict.reason
        assert "chain_time" in verdict.reason


def test_a_tax_that_takes_the_whole_order_refuses_on_the_rates_not_the_depth(tmp_db, rpc):
    """The reason has to name the cause: this is a 100% fee, not an unpriceable pool."""
    register_token(tmp_db)
    rpc["results"] = batch_results(creator_tax_bps=9_900, age_s=3_600)
    venue = read_venue(RH, TOKEN, tmp_db)
    assert "take_10000bps_per_leg" in venue.note
    assert venue.tax_bps_per_leg is None


# ------------------------------------------------------------------- the depth itself


#: Two real ``CurveBuy`` fills, replayed. The reserves are an ``eth_call`` at
#: ``block - 1`` and ``quote_in``/``tokens_out`` are the event's own words; each fill was
#: alone in its block, so the pre-state priced here is the pre-state it filled against.
#: 40 of 40 such fills on 2026-09-21 reproduced exactly under this arithmetic, and 0 of 40
#: under the algebraically-equal ``token_reserve - k // (quote_reserve + leg)``, which was
#: one atom high every time. That is the whole reason this test exists: the two spellings
#: differ only in which way the division rounds, and only the chain can say which.
GOLDEN: list[dict[str, int]] = [
    {   # curve 0xe52c120ca069e6313ccf84a4efebc1f732e2317d, block 68843109
        "quote_reserve": 4_032_569_612_823_790_426,
        "sellable_tokens": 130_893_525_507_995_175_872_337_548,
        "reserved_tokens": 285_714_285_714_285_714_285_714_285,
        "real_quote_reserve": 2_352_569_612_823_790_426,
        "graduation_threshold": 4_200_000_000_000_000_000,
        "fee_bps": 100,
        "creator_tax_bps": 0,
        "quote_in": 11_000_000_000_000_000,
        "tokens_out": 1_122_024_082_996_164_274_907_938,
    },
    {   # curve 0x1da6d521ef172eef826d6a6adfb67e146e51a018, block 68843109
        "quote_reserve": 4_357_909_546_173_169_234,
        "sellable_tokens": 99_791_650_606_780_432_096_852_953,
        "reserved_tokens": 285_714_285_714_285_714_285_714_285,
        "real_quote_reserve": 2_677_909_546_173_169_234,
        "graduation_threshold": 4_200_000_000_000_000_000,
        "fee_bps": 100,
        "creator_tax_bps": 0,
        "quote_in": 1_022_477_078_176_580,
        "tokens_out": 89_524_265_769_444_226_671_129,
    },
]


def _golden_depth(fill: dict[str, int]) -> PonsCurveDepth:
    return PonsCurveDepth(
        quote_reserve=fill["quote_reserve"],
        token_reserve=fill["sellable_tokens"] + fill["reserved_tokens"],
        headroom_base_units=fill["graduation_threshold"] - fill["real_quote_reserve"],
        take_bps_per_leg=fill["fee_bps"] + fill["creator_tax_bps"],
    )


@pytest.mark.parametrize("fill", GOLDEN, ids=["68843109a", "68843109b"])
def test_the_curve_arithmetic_reproduces_a_real_fill(fill):
    """The depth model is the venue's own maths or it is a second opinion.

    ``entry_impact`` is ``leg - (what those atoms cost at the pre-trade marginal price)``,
    so the quantity this pins is the ``atoms`` in the middle of it: get that wrong and
    every impact number is wrong in the same direction, invisibly.
    """
    depth = _golden_depth(fill)
    leg = depth._leg(fill["quote_in"])
    atoms = depth.tokens_out(fill["quote_in"])

    assert atoms == fill["tokens_out"], atoms - fill["tokens_out"]
    # The spelling that was wrong, pinned so nobody "simplifies" back into it. Its error
    # is invisible in `entry_impact` — one atom is worth less than one wei here — so
    # without this line a rewrite could reintroduce it and every test would still pass.
    k = depth.quote_reserve * depth.token_reserve
    assert depth.token_reserve - k // (depth.quote_reserve + leg) == fill["tokens_out"] + 1

    impact = depth.entry_impact(fill["quote_in"])
    at_spot = atoms * depth.quote_reserve // depth.token_reserve
    assert impact == leg - at_spot > 0
    # Impact is the share of the reserve the leg represents, to first order.
    assert abs(impact * 10_000 // leg - leg * 10_000 // (depth.quote_reserve + leg)) <= 1


def test_impact_grows_with_size_and_the_flat_share_falls(tmp_db, rpc):
    """The U. Without the rising arm this gate has a floor and no ceiling."""
    register_token(tmp_db)
    depth = resolve_depth(RH, TOKEN, tmp_db)
    assert isinstance(depth, PonsCurveDepth)

    sizes = [10**14, 10**15, 10**16, 10**17]
    bps = [depth.entry_impact(s) * 10_000 // s for s in sizes]
    assert bps == sorted(bps) and bps[0] < bps[-1]

    model = evm_cost_model(RH, read_venue(RH, TOKEN, tmp_db))
    pcts = [estimate_round_trip(RH, s, model=model, depth=depth).pct for s in sizes]
    assert pcts[0] > pcts[1], "the flat term must dominate at the small end"
    assert pcts[-1] > pcts[1], "impact must dominate at the large end"


def test_a_size_past_the_graduation_threshold_is_not_priceable(tmp_db, rpc):
    """Past the threshold the curve graduates mid-fill; that is not a fill at our size."""
    register_token(tmp_db)
    depth = resolve_depth(RH, TOKEN, tmp_db)
    headroom = CURVE["graduation_threshold"] - CURVE["real_quote_reserve"]
    assert depth.headroom_base_units == headroom
    assert depth.entry_impact(headroom) is not None
    assert depth.entry_impact(depth.max_size_base_units + 10**15) is None


def test_the_gas_term_is_small_and_the_tax_term_is_large(tmp_db, rpc):
    """Measured, because a gas-only EVM cost model is wrong by two orders of magnitude."""
    register_token(tmp_db)
    rpc["results"] = batch_results(creator_tax_bps=300)
    venue = read_venue(RH, TOKEN, tmp_db)
    est = estimate_round_trip(RH, MAX_POSITION, model=evm_cost_model(RH, venue),
                              depth=venue.depth)

    flat_pct = Decimal(est.flat_base_units) * 100 / MAX_POSITION
    prop_pct = Decimal(est.proportional_base_units) * 100 / MAX_POSITION
    assert flat_pct < Decimal("0.5"), flat_pct          # 0.32% at 0.0484 gwei
    assert prop_pct > Decimal(9), prop_pct              # 2 x (100+300+100) bps
    assert prop_pct > flat_pct * 20


# ------------------------------------------------------------ what refuses, and why


def _flap_price_fixture():
    """A checked Flap curve read, built from the venue's published geometry."""
    scale = Decimal(10) ** 18
    supply_atoms = 10**27
    sold_atoms = 10**26
    supply = Decimal(supply_atoms) / scale
    remaining = supply - Decimal(sold_atoms) / scale
    r = Decimal("6.14")
    h = Decimal("107036752")
    k = Decimal("6797205657.28")
    price = k / ((remaining + h) ** 2)
    reserve = k * (Decimal(1) / (remaining + h) - Decimal(1) / (supply + h))
    record = FlapRecord(
        status=1,
        quote_raised_base=int(reserve * scale),
        tokens_sold_atoms=sold_atoms,
        price_word=int(price * scale),
        r_scaled=int(r * scale),
        h_scaled=int(h * scale),
        k_scaled=int(k * scale),
        graduation_tokens_atoms=8 * 10**26,
        quote_token=None,
    )
    priced, note = parse_flap_record(
        record,
        token_decimals=18,
        token_supply_atoms=supply_atoms,
        quote_decimals=18,
    )
    assert priced is not None, note
    return priced


def _flap_tokens_out(size_base_units: int) -> int:
    """The Flap LibCurve ``estimateSupply`` result for the fixture's buy."""
    wad = 10**18

    def div_wad_up(numerator: int, denominator: int) -> int:
        return (numerator * wad + denominator - 1) // denominator

    priced = _flap_price_fixture()
    reserve = priced.quote_reserve_base
    assert reserve is not None
    total_supply = 10**27
    sold = 10**26
    h = 107_036_752 * wad
    r = 6_140_000_000_000_000_000
    k = 6_797_205_657_280_000_000_000_000_000
    after_supply = total_supply + h - div_wad_up(k, r + reserve + size_base_units)
    return after_supply - sold


def test_a_verified_flap_curve_provides_exact_depth_for_bsc_sizing(
    tmp_db, risk_yaml, monkeypatch,
):
    """A checked native-quoted Flap read supplies exact depth to the EVM sizer."""
    priced = _flap_price_fixture()
    monkeypatch.setattr(evm_price, "read_flap", lambda token, rpc: (priced, "ok"))
    # The Buy Quota read (viability._flap_buy_quota_refusal, 2026-10-05) is the one call that
    # reaches the transport past the faked portal read: answer "no quota", never the network.
    monkeypatch.setattr(evm_price, "json_rpc_batch",
                        lambda *a, **kw: (lambda calls: ["0x" + "0" * 128 for _ in calls]))
    monkeypatch.setattr(
        viability,
        "read_dex_venue",
        lambda chain, token, conn=None: VenueRead(
            NoDepth("flap_rates_fixture"), Decimal(100), Decimal(0), GAS_PRICE_WEI,
            "flap_rates_fixture",
        ),
    )
    reset_venue_cache()

    venue = read_venue(BSC, BSC_FLAP_TOKEN, tmp_db)
    assert venue.priced, venue.note
    assert type(venue.depth).__name__ == "FlapCurveDepth", venue.note
    assert venue.depth.basis is EvidenceBasis.VERIFIED_ONCHAIN
    assert venue.depth.tokens_out(10**16) == _flap_tokens_out(10**16)
    assert venue.depth.entry_impact(10**16) is not None
    assert venue.depth.entry_impact(2 * 10**16) > venue.depth.entry_impact(10**16)
    assert venue.depth.entry_impact(venue.depth.max_size_base_units + 1) is None

    band = sizing_band(
        BSC,
        tmp_db,
        token=BSC_FLAP_TOKEN,
        ceiling_pct=Decimal("7"),
        tolerance_bps=2500,
    )
    assert band.reason == "band", band.reason
    assert band.viable
    verdict = check_size(
        BSC,
        10**16,
        tmp_db,
        token=BSC_FLAP_TOKEN,
        ceiling_pct=Decimal("7"),
    )
    assert verdict.ok, verdict.reason


def test_a_failed_flap_probe_cannot_fall_back_to_pool_depth(
    tmp_db, monkeypatch,
):
    """An unknown Flap read stays blind even when a generic pool answer is available."""
    monkeypatch.setattr(
        evm_price, "read_flap", lambda token, rpc: (None, "flap_portal_read_failed")
    )
    monkeypatch.setattr(
        viability,
        "read_dex_venue",
        lambda chain, token, conn=None: VenueRead(
            NoDepth("dex_rates_fixture"), Decimal(100), Decimal(0), GAS_PRICE_WEI,
            "dex_rates_fixture",
        ),
    )
    monkeypatch.setattr(viability, "_native_usd", lambda chain, conn, *, at_ms=None: Decimal("600"))
    monkeypatch.setattr(
        viability,
        "_dossier_liquidity_usd",
        lambda chain, token, conn, *, at_ms: (Decimal("100000"), "fixture"),
    )
    reset_venue_cache()

    venue = read_venue(BSC, BSC_FLAP_TOKEN, tmp_db)
    assert not venue.priced
    assert venue.note.startswith("flap_probe_unavailable:")
    depth = resolve_depth(BSC, BSC_FLAP_TOKEN, tmp_db)
    assert isinstance(depth, NoDepth)
    assert depth.source.startswith("flap_probe_unavailable:")


def test_an_unknown_bsc_token_still_refuses_rather_than_pricing_free(
    tmp_db, risk_yaml, monkeypatch,
):
    """A token with no exact Flap record and no dossier still refuses safely.

    It has neither a venue tax nor a generic depth proof, so the old no-reader refusal remains
    a refusal rather than pricing unknown data at zero.
    """
    monkeypatch.setattr(
        evm_price, "read_flap", lambda token, rpc: (None, "flap_no_portal_record")
    )
    venue = read_venue(Chain.BSC, "0x7f98ea3d039f2cce3a244c89567694b63f127777", tmp_db)
    assert not venue.priced and isinstance(venue.depth, NoDepth)
    # Since 2026-09-22 bsc HAS a rates reader (`read_dex_venue`: measured per-token tax +
    # a bounded pool fee + live gas). The property this test defends is unchanged and is
    # the important one -- with nothing to read, it REFUSES rather than pricing at zero.
    assert not venue.priced
    # The TAX is the unreadable part and it is the one that must be None. The pool-fee
    # bound is a published constant and reporting it is not "pricing free" -- `priced`
    # requires all three numbers, so an unknown tax still refuses the trade.
    assert venue.tax_bps_per_leg is None
    assert venue.note == "dex:no_dossier", venue.note

    model = cost_model(Chain.BSC, tmp_db, token="0x7f98ea3d039f2cce3a244c89567694b63f127777")
    assert model.basis is EvidenceBasis.UNAVAILABLE
    verdict = check_size(Chain.BSC, 10**16, tmp_db,
                         token="0x7f98ea3d039f2cce3a244c89567694b63f127777")
    assert not verdict.ok and "bsc" in verdict.reason


def test_a_curve_quoted_in_another_token_refuses(tmp_db, rpc, risk_yaml):
    """78 of the 400 most recent launches are. Our size is wei; the curve wants something else."""
    register_token(tmp_db, quote_is_native=False)
    venue = read_venue(RH, TOKEN, tmp_db)
    assert "quote_not_native" in venue.note
    assert not check_size(RH, MAX_POSITION, tmp_db, token=TOKEN).ok
    assert rpc["calls"] == [], "a curve we cannot spend on is not worth a round trip"


def test_a_graduated_token_gets_no_curve_depth(tmp_db, rpc, risk_yaml):
    """The curve is complete and the pool is Uniswap v4, which this module cannot quote."""
    register_token(tmp_db, migrated_ms=now_ms() - 1_000)
    venue = read_venue(RH, TOKEN, tmp_db)
    # A graduated token now falls through to the DEX rates reader, which still refuses
    # here because there is no dossier to read a tax from -- and the note carries both
    # halves so the reason is not lost.
    assert not venue.priced
    assert venue.note == "pons:graduated+dex:no_dossier", venue.note
    verdict = check_size(RH, MAX_POSITION, tmp_db, token=TOKEN)
    assert not verdict.ok
    assert "no_native_usd_price" in verdict.reason or "graduated" in verdict.reason


def test_a_dead_rpc_refuses_and_says_so(tmp_db, rpc, risk_yaml):
    register_token(tmp_db)
    rpc["ok"] = False
    rpc["results"] = []
    rpc["note"] = "connection refused"
    venue = read_venue(RH, TOKEN, tmp_db)
    assert not venue.priced and "unreadable" in venue.note
    assert not check_size(RH, MAX_POSITION, tmp_db, token=TOKEN).ok


def test_a_missing_gas_price_refuses(tmp_db, rpc, risk_yaml):
    """No declared EVM flat cost exists to fall back to, and inventing one is the bug."""
    assert RH not in DECLARED_FLAT_PER_LEG
    register_token(tmp_db)
    results = batch_results()
    results[-1] = "0x"
    rpc["results"] = results
    model = cost_model(RH, tmp_db, token=TOKEN)
    assert model.basis is EvidenceBasis.UNAVAILABLE and "gas_price" in model.source
    assert not check_size(RH, MAX_POSITION, tmp_db, token=TOKEN).ok


def test_a_token_with_no_row_and_no_factory_answer_refuses(tmp_db, rpc, risk_yaml):
    """The factory is asked before we give up; a zero record is still a refusal."""
    rpc["results"] = [_word(0)]
    venue = read_venue(RH, TOKEN, tmp_db)
    assert "factory_has_no_curve" in venue.note
    assert not check_size(RH, MAX_POSITION, tmp_db, token=TOKEN).ok


def test_an_evm_entry_without_a_token_refuses(tmp_db, risk_yaml):
    """The fee-only path is a Solana affordance: on EVM there is no fee without a token."""
    model = cost_model(RH, tmp_db)
    assert model.basis is EvidenceBasis.UNAVAILABLE
    assert not check_size(RH, MAX_POSITION, tmp_db).ok


# ----------------------------------------------------------- how the answer is reached


def test_one_decision_costs_one_round_trip(tmp_db, rpc, risk_yaml):
    """The sizer and the gate both ask; the chain is asked once. 1 call/s bucket."""
    register_token(tmp_db)
    sizing_band(RH, tmp_db, token=TOKEN)
    check_size(RH, MAX_POSITION, tmp_db, token=TOKEN)
    assert len(rpc["calls"]) == 1
    assert len(rpc["calls"][0]) == 13, "ten curve reads, the tax, the gas price, the head"
    assert rpc["calls"][0][-1] == ("eth_getBlockByNumber", ["latest", False])

    reset_venue_cache()
    sizing_band(RH, tmp_db, token=TOKEN)
    assert len(rpc["calls"]) == 2, "and the memo can be dropped"


def test_the_evm_arm_never_falls_back_to_a_chain_wide_fit(tmp_db, rpc, risk_yaml):
    """A fit over other tokens' fills would launder a 0% tax onto a 5% token."""
    for seq, cost in enumerate((10**15, 2 * 10**15, 3 * 10**15, 4 * 10**15, 5 * 10**15,
                                6 * 10**15, 8 * 10**15, 10**16, 12 * 10**15), start=1):
        tmp_db.execute(
            "INSERT INTO trades (trade_id, position_id, lane, mode, chain, token, opened_ms, "
            "closed_ms, hold_s, cost_native, proceeds_native, pnl_native, pnl_pct, fees_native) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"t{seq}", f"p{seq}", "pons-robinhood", "live", RH.value, TOKEN,
             now_ms() - 60_000, now_ms() + seq, 60, str(cost), str(cost * 3 // 4),
             str(-cost // 4), 0.0, str(10**13 + cost // 100)),
        )
    tmp_db.commit()
    assert viability.derive_cost_model(RH, tmp_db).known, "the fit itself is available"

    register_token(tmp_db)
    rpc["ok"] = False
    rpc["results"] = []
    model = cost_model(RH, tmp_db, token=TOKEN)
    assert model.basis is EvidenceBasis.UNAVAILABLE, "the fit must not stand in for a tax"
    assert not check_size(RH, MAX_POSITION, tmp_db, token=TOKEN).ok


def test_robinhood_is_priced_from_eth_samples_or_not_at_all(tmp_db):
    """Its native token *is* ETH, and there is no reference pool on a one-year-old L2."""
    assert viability._native_pricing_chain(RH) is Chain.ETH
    assert viability._native_pricing_chain(Chain.BSC) is Chain.BSC
    assert viability._native_usd(RH, tmp_db) is None, "no sample is None, not a guess"

    tmp_db.execute(
        "INSERT INTO native_prices (chain, ts_ms, price_usd, source, pair, liquidity_usd, "
        "receipt_json) VALUES (?,?,?,?,?,?,?)",
        (Chain.ETH.value, now_ms(), "2700.46", "dexscreener", "uniswap:WETH/USDT",
         "110000000", "{}"),
    )
    tmp_db.commit()
    assert viability._native_usd(RH, tmp_db) == Decimal("2700.46")


def test_the_venue_read_shape_refuses_on_any_missing_part():
    """``priced`` is an and, not an or: three inputs, and each one alone refuses."""
    whole = VenueRead(NoDepth("x"), Decimal(100), Decimal(0), GAS_PRICE_WEI, "n")
    assert whole.priced
    for field, value in (("fee_bps_per_leg", None), ("tax_bps_per_leg", None),
                         ("gas_price_wei", None), ("gas_price_wei", 0)):
        broken = VenueRead(**{**whole.__dict__, field: value})
        assert not broken.priced
        assert evm_cost_model(RH, broken).basis is EvidenceBasis.UNAVAILABLE


def test_an_evm_model_is_not_dressed_up_as_measured(tmp_db, rpc):
    """The rates are on-chain, the gas units and the router cut are declared. Say so."""
    register_token(tmp_db)
    model = cost_model(RH, tmp_db, token=TOKEN)
    assert model.basis is EvidenceBasis.ESTIMATED
    assert not model.from_live_trades
    assert isinstance(model, CostModel)


# ------------------------------------------------------- the clock the toll runs on
#
# The curve charges its anti-sniper rung off ``block.timestamp - launchedAt``, both chain
# time. Round 1 priced it off ``now_ms() - launchedAt``, and the chain head's timestamp
# trails a wall clock: at wall-clock t=2 s the reader charged rung 2 (19 bps), check_size
# approved 0.0075 ETH at 5.13%, and a chain 0.87-2.0 s behind was about to charge rung 1
# (618 bps a leg, ~17% round trip) or rung 0. So the reader now prices the rung off the
# head block's own timestamp, carried in the same batch (``chain_time``), and only when
# that is missing off the wall clock less the largest lag ever measured
# (``wall_clock_minus_lag``, :data:`~kaiba.execution.viability.PONS_CHAIN_LAG_MARGIN_S`).
# Inside the window it refuses on either basis, naming the rung and the basis.

#: A real ``launchedAt()`` (curve 0x3ccc7163…, 2026-09-21). "t = 2 s" below means exactly
#: 2,000 ms after it on a pinned ``viability.now_ms``.
LAUNCHED_AT_S = 1_789_991_064

#: MEASURED ``wall_clock - eth_getBlockByNumber('latest').timestamp`` in seconds, the
#: runs behind ``PONS_CHAIN_LAG_MARGIN_S``, as (where, min, max). The reader stamps its
#: clock *before* the batch goes out, so the send-time figure is the one the margin has to
#: cover; the receipt-time figure (send + one RTT of 265-373 ms) is listed because it is
#: the larger one and it brushed the margin once (2.047 s on the box, 2026-09-22).
LAG_SAMPLES: tuple[tuple[str, float, float], ...] = (
    ("workstation 2026-09-21, round-1 verifier", 1.3, 2.0),
    ("live box 2026-09-21, round-1 verifier", 0.87, 1.55),
    ("live box 2026-09-22, 5 samples at send time", 0.849, 1.782),
    ("live box 2026-09-22, the same 5 at receipt", 1.184, 2.047),
    ("workstation 2026-09-22, 5 samples at send time", 0.902, 1.579),
    ("workstation 2026-09-22, the same 5 at receipt", 1.222, 1.931),
)


@pytest.fixture
def clock(monkeypatch):
    """Pin ``viability.now_ms`` so a test can say "t = 2.0 s after launch" and mean it.

    ``batch_results(age_s=...)`` measures from the real clock in whole seconds, which is
    up to 999 ms loose; a test about a 1 s rung cannot afford that. The memo is dropped on
    every move because it is keyed on the token, not the clock.
    """
    def at(elapsed_ms: int) -> None:
        monkeypatch.setattr(viability, "now_ms", lambda: LAUNCHED_AT_S * 1000 + int(elapsed_ms))
        reset_venue_cache()

    return at


def _no_header(**kw) -> list:
    return batch_results(creator_tax_bps=0, launched_at_s=LAUNCHED_AT_S, **kw)


def test_at_wall_clock_two_seconds_the_chain_is_still_in_the_launch_second(tmp_db, rpc, risk_yaml, clock):
    """The round-1 finding, closed: t=2 s on our clock is elapsed 0 on the honest one.

    2.0 s less the 2.0 s margin is 0 s: rung 0, 9,900 bps, refused — and the refusal says
    which rung and which clock. Round 1 priced this instant at rung 2 and approved it.
    """
    register_token(tmp_db)
    rpc["results"] = _no_header()
    clock(2_000)
    venue = read_venue(RH, TOKEN, tmp_db)
    assert isinstance(venue.depth, NoDepth)
    assert "snipe_window" in venue.note and "rung0_9900bps" in venue.note, venue.note
    assert "elapsed0s" in venue.note and "wall_clock_minus_lag" in venue.note, venue.note
    assert venue.tax_bps_per_leg == Decimal(9_900), "the rung is charged, not hidden"
    assert venue.fee_bps_per_leg == Decimal(100) and venue.gas_price_wei == GAS_PRICE_WEI

    verdict = check_size(RH, MAX_POSITION, tmp_db, token=TOKEN)
    assert not verdict.ok
    assert "rung0_9900bps" in verdict.reason and "wall_clock_minus_lag" in verdict.reason, verdict.reason


def test_just_under_five_seconds_on_the_wall_clock_still_refuses(tmp_db, rpc, risk_yaml, clock):
    """t=4.9 s less the margin is 2.9 s: rung 2, 19 bps. The cost gate alone clears that
    (5.13% < 7%) — and it is exactly the rung a chain one second behind us prices at 618.
    The window refuses it whatever it costs; the strategy waits the toll out."""
    register_token(tmp_db)
    rpc["results"] = _no_header()
    clock(4_900)
    venue = read_venue(RH, TOKEN, tmp_db)
    assert isinstance(venue.depth, NoDepth)
    assert "rung2_19bps" in venue.note and "elapsed2s" in venue.note, venue.note
    assert "wall_clock_minus_lag" in venue.note
    assert venue.tax_bps_per_leg == Decimal(19)
    verdict = check_size(RH, MAX_POSITION, tmp_db, token=TOKEN)
    assert not verdict.ok and "rung2_19bps" in verdict.reason, verdict.reason


def test_just_over_five_seconds_on_the_wall_clock_approves_at_the_zero_rung(tmp_db, rpc, risk_yaml, clock):
    """t=5.1 s less the margin is 3.1 s: the window has elapsed on the honest clock too."""
    register_token(tmp_db)
    rpc["results"] = _no_header()
    clock(5_100)
    venue = read_venue(RH, TOKEN, tmp_db)
    assert isinstance(venue.depth, PonsCurveDepth), venue.note
    assert venue.tax_bps_per_leg == Decimal(0)
    assert "snipe0bps" in venue.note and "elapsed3s" in venue.note, venue.note
    assert "wall_clock_minus_lag" in venue.note
    assert venue.depth.source.endswith(":snipe0:wall_clock_minus_lag"), venue.depth.source
    verdict = check_size(RH, MAX_POSITION, tmp_db, token=TOKEN)
    assert verdict.ok, verdict.reason


def test_a_head_timestamp_in_the_batch_outranks_the_wall_clock(tmp_db, rpc, risk_yaml, clock):
    """With the header present the wall clock is not consulted at all, in either direction.

    Chain t=3 s approves at wall t=1 s, which the margin alone would call rung 0; chain
    t=2 s refuses at wall t=60 s, which the margin alone would have waved through.
    """
    register_token(tmp_db)
    rpc["results"] = _no_header(head_ts_s=LAUNCHED_AT_S + 3)
    clock(1_000)
    venue = read_venue(RH, TOKEN, tmp_db)
    assert isinstance(venue.depth, PonsCurveDepth), venue.note
    assert "chain_time" in venue.note and "elapsed3s" in venue.note, venue.note
    assert "wall_clock" not in venue.note
    assert venue.depth.source.endswith(":snipe0:chain_time")
    assert check_size(RH, MAX_POSITION, tmp_db, token=TOKEN).ok

    rpc["results"] = _no_header(head_ts_s=LAUNCHED_AT_S + 2)
    clock(60_000)
    venue = read_venue(RH, TOKEN, tmp_db)
    assert isinstance(venue.depth, NoDepth)
    assert "rung2_19bps" in venue.note and "chain_time" in venue.note, venue.note
    verdict = check_size(RH, MAX_POSITION, tmp_db, token=TOKEN)
    assert not verdict.ok and "chain_time" in verdict.reason, verdict.reason


def test_the_head_is_asked_for_in_the_batch_and_may_fail_without_refusing_the_read(tmp_db, rpc, risk_yaml, clock):
    """The header is the thirteenth item and optional: a node that will not serve it costs
    the margin, not the entry. The twelve before it are not optional."""
    register_token(tmp_db)
    rpc["results"] = _no_header() + [None]
    rpc["ok"] = False
    rpc["note"] = "eth_getBlockByNumber: {'code': -32601, 'message': 'method not found'}"
    clock(5_100)
    venue = read_venue(RH, TOKEN, tmp_db)
    assert rpc["calls"][-1][-1] == ("eth_getBlockByNumber", ["latest", False])
    assert isinstance(venue.depth, PonsCurveDepth), venue.note
    assert "wall_clock_minus_lag" in venue.note

    # ``0x0`` in the header is absent, not the epoch: the epoch would read every rung as
    # elapsed and price the launch second at 0 bps.
    rpc["ok"] = True
    rpc["note"] = None
    rpc["results"] = _no_header() + [block_header(0)]
    clock(2_000)
    venue = read_venue(RH, TOKEN, tmp_db)
    assert isinstance(venue.depth, NoDepth) and "wall_clock_minus_lag" in venue.note, venue.note
    assert "rung0_9900bps" in venue.note

    # A failed item *before* the header still refuses the whole read, as it always did.
    from kaiba.ingest.robinhood import CURVE_READS

    broken = _no_header(head_ts_s=LAUNCHED_AT_S + 60)
    broken[len(CURVE_READS)] = None  # the creator tax
    rpc["ok"] = False
    rpc["note"] = "eth_call: execution reverted"
    rpc["results"] = broken
    clock(60_000)
    venue = read_venue(RH, TOKEN, tmp_db)
    assert isinstance(venue.depth, NoDepth) and "unreadable" in venue.note, venue.note
    assert venue.tax_bps_per_leg is None


def test_elapsed_never_goes_negative_on_either_basis(tmp_db, rpc, risk_yaml, clock):
    """A head behind the launch, or a clock behind the chain, is the launch second."""
    register_token(tmp_db)
    rpc["results"] = _no_header(head_ts_s=LAUNCHED_AT_S - 5)
    clock(60_000)
    venue = read_venue(RH, TOKEN, tmp_db)
    assert "rung0_9900bps" in venue.note and "elapsed0s" in venue.note and "chain_time" in venue.note

    rpc["results"] = _no_header()
    clock(-1_000)
    venue = read_venue(RH, TOKEN, tmp_db)
    assert "rung0_9900bps" in venue.note and "elapsed0s" in venue.note, venue.note
    assert "wall_clock_minus_lag" in venue.note


def test_an_unmeasured_schedule_inside_its_window_is_labelled_invented_and_refused(tmp_db, rpc, risk_yaml, clock):
    """Only (9900, 3) was measured. Another configuration charges its full toll inside its
    window and the note says the shape is INVENTED; past it, the read is ordinary."""
    register_token(tmp_db)
    rpc["results"] = _no_header(
        head_ts_s=LAUNCHED_AT_S + 5, snipe_tax_start_bps=5_000, snipe_tax_seconds=10,
    )
    clock(60_000)
    venue = read_venue(RH, TOKEN, tmp_db)
    assert isinstance(venue.depth, NoDepth)
    assert "rung5_5000bps" in venue.note and "INVENTED" in venue.note, venue.note
    assert venue.tax_bps_per_leg == Decimal(5_000)

    rpc["results"] = _no_header(
        head_ts_s=LAUNCHED_AT_S + 10, snipe_tax_start_bps=5_000, snipe_tax_seconds=10,
    )
    clock(60_000)
    assert isinstance(read_venue(RH, TOKEN, tmp_db).depth, PonsCurveDepth)

    # No tax configured is no window: the launch second itself prices at the creator tax.
    rpc["results"] = _no_header(head_ts_s=LAUNCHED_AT_S, snipe_tax_start_bps=0, snipe_tax_seconds=0)
    clock(0)
    venue = read_venue(RH, TOKEN, tmp_db)
    assert isinstance(venue.depth, PonsCurveDepth) and venue.tax_bps_per_leg == Decimal(0)


def test_an_older_curve_reader_without_the_shape_label_still_refuses_and_never_raises(
    tmp_db, rpc, risk_yaml, clock, monkeypatch,
):
    """The live box runs an older ``ingest/robinhood.py`` — no ``snipe_tax_basis``, no
    rung table (MEASURED 2026-09-22: 0 hits for either on the box's copy). A
    ``viability.py`` deployed on its own must still refuse inside the window, not turn
    every read into an ``AttributeError`` that ``check_entry`` was never written to catch."""
    from kaiba.ingest import robinhood as pons

    monkeypatch.delattr(pons.CurveState, "snipe_tax_basis")
    register_token(tmp_db)
    rpc["results"] = _no_header(head_ts_s=LAUNCHED_AT_S + 1)
    clock(60_000)
    venue = read_venue(RH, TOKEN, tmp_db)
    assert isinstance(venue.depth, NoDepth)
    assert "rung1_618bps" in venue.note and "chain_time" in venue.note, venue.note
    assert "shape_basis_unavailable" in venue.note, venue.note
    assert not check_size(RH, MAX_POSITION, tmp_db, token=TOKEN).ok


def test_the_lag_margin_is_the_largest_lag_measured_and_says_so():
    """The margin is a number about the chain, so it is MEASURED or it is nothing — and it
    is the max of the send-time column, because a margin below the lag charges rung 2 for a
    fill that pays rung 1."""
    margin = viability.PONS_CHAIN_LAG_MARGIN_S
    row = viability.PROVENANCE["PONS_CHAIN_LAG_MARGIN_S"]
    assert row.source.startswith("MEASURED"), row.source
    assert "max" in row.source and "INVENTED" not in row.source
    assert isinstance(margin, Decimal), "seconds feed a rung, and a rung is a cost: no float"
    send_time = [hi for where, _, hi in LAG_SAMPLES if "receipt" not in where]
    assert margin == max(send_time) == Decimal("2.0")
    assert all(margin >= Decimal(str(hi)) for hi in send_time)
    # The wall-clock threshold the margin implies, stated once: elapsed 3 s at t = 5.0 s.
    assert viability._pons_elapsed_s(LAUNCHED_AT_S, LAUNCHED_AT_S * 1000 + 4_999, None) == (2, "wall_clock_minus_lag")
    assert viability._pons_elapsed_s(LAUNCHED_AT_S, LAUNCHED_AT_S * 1000 + 5_000, None) == (3, "wall_clock_minus_lag")
    assert viability._pons_elapsed_s(LAUNCHED_AT_S, 0, LAUNCHED_AT_S + 3) == (3, "chain_time")
