"""Flap (bsc) curves quoted in something other than BNB — the same mechanism as Pons.

WHY THIS FILE EXISTS. MEASURED 2026-09-22 over the 20 most recent bsc dossiers, with the
Flap depth reader live on the box: bsc sized **1 of 20**, and the refusals were

    12  flap_probe_unavailable:quote_not_native      <- 60% of every bsc candidate
     5  flap_probe_unavailable:flap_portal_read_failed
     3  dex:fee_upper100bps

So the single biggest reason this chain traded nothing was the same one that stopped 17%
of robinhood: a curve denominated in an ERC-20 while our size is in wei. The fix is the
same object, ``viability.QuoteAssetRate`` — one rate, one decimals rule, one cache, one set
of refusals — and what differs between the two venues is only *where* it is applied. Pons'
curve arithmetic lives in ``viability`` so its reserves are converted; Flap's lives in
``evm_price.FlapCurve`` so the size is converted into the curve's own currency on the way
in and the answer back out. ``tests/test_viability_quote_asset.py`` covers the rate itself
and the Pons arm; this file covers the Flap arm and the fact that it is the same rate.

Every number below was read on 2026-09-22:

* BNB 785.13 USD, from ``native_price.fetch(Chain.BSC)`` — PancakeSwap WBNB/USDT at
  0x16b9a828..., $80,093,043 of liquidity;
* the quote asset is BSC-USD (``0x55d398326f99059ff775485246999027b3197955``), which
  ``gmgn-cli token info --chain bsc`` reports as symbol USDT, **decimals 18**, price
  1.000032122081, liquidity $80,177,031. The 18 matters: ``evm_price.FlapCurve`` refuses
  any pairing that is not WAD on both sides, so on this venue — unlike robinhood, where
  USDG at 6 decimals is the second-most-used quote asset — a non-18 quote asset is
  refused upstream and stays refused;
* the sizes are the operator's own from ``config/risk.yaml``: 0.003 to 0.02 BNB.

Nothing here touches the network: ``evm_price.read_flap``, ``gmgn_cli.token_info`` and the
gas oracle are all replaced, and the BNB price is a row this file writes.
"""

from __future__ import annotations

import copy
import json
from decimal import Decimal

import pytest
import yaml

from kaiba.core.schemas import Chain, EvidenceBasis, Receipt, now_ms
from kaiba.execution import evm_price, viability
from kaiba.execution.evm_price import FLAP_CURVE_DECIMALS, FLAP_WAD, FlapCurve, VenuePrice
from kaiba.execution.viability import (
    FlapCurveDepth,
    NoDepth,
    check_size,
    read_venue,
    reset_quote_asset_cache,
    reset_venue_cache,
    resolve_depth,
    sizing_band,
)
from kaiba.providers import gmgn_cli

BSC = Chain.BSC
TOKEN = "0x7f98ea3d039f2cce3a244c89567694b63f127777"
OTHER_TOKEN = "0x1234ea3d039f2cce3a244c89567694b63f127777"

#: BSC-USD. Symbol USDT, 18 decimals, 1.000032122081 USD, $80,177,031 liquidity — read
#: from ``gmgn-cli token info --chain bsc`` on 2026-09-22. Also already in
#: ``schemas.QUOTE_ASSETS[Chain.BSC]``, which is where its address comes from.
USDT = "0x55d398326f99059ff775485246999027b3197955"
USDT_PRICE_USD = "1.000032122081"

#: BNB, same day, from the deepest pool ``native_price`` would itself have chosen.
BNB_USD = "785.13"

#: The operator's configured bsc sizes, ``config/risk.yaml``: 0.003 and 0.02 BNB.
MIN_POSITION = 3_000_000_000_000_000
MAX_POSITION = 20_000_000_000_000_000

#: Gas price pinned the way ``tests/test_dex_venue.py`` pins it, so these tests measure the
#: quote leg rather than a gas oracle.
GAS_PRICE_WEI = 120_000_000

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
        "bsc": {
            "enabled": True,
            "bankroll_base_units": 1_000_000_000_000_000_000,
            "max_position_base_units": MAX_POSITION,
            "min_position_base_units": MIN_POSITION,
            "gas_reserve_base_units": 30_000_000_000_000_000,
            "daily_loss_stop_base_units": 100_000_000_000_000_000,
            "max_exposure_pct": 10.0,
            "wallet": None,
        },
    },
    "lanes": {},
    "protection": {},
}


# --------------------------------------------------------------------------- the curve
#
# A Flap curve is `(x + h)(y + r) = K` with x tokens remaining and y quote raised, so a
# fixture is only self-consistent when `K = (x + h)(y + r)` holds at the state it claims
# to be in. `tests/test_flap_curve_refusals.py`'s fixture is built to exercise the
# PRECONDITIONS and does not satisfy the identity, so it prices no fill at all. This one
# derives `tokens_sold_atoms` from the reserve so the curve really is on its own curve.

#: Whole tokens of supply and of the `h` shift, and whole quote units of `r`. Chosen so a
#: 0.02 BNB order (about 15.7 quote units at the measured prices) is a small fraction of
#: the curve rather than a fill that walks it — a $25-pool fixture would make every
#: assertion below a statement about the pool instead of about the conversion.
SUPPLY_UNITS = 10**9
H_UNITS = 10**9
R_UNITS = 30_000


def flap_curve(*, reserve_base: int, quote_decimals: int = FLAP_CURVE_DECIMALS) -> FlapCurve:
    """A self-consistent Flap curve holding ``reserve_base`` of its quote asset."""
    supply_atoms = SUPPLY_UNITS * 10**18
    h_scaled = H_UNITS * FLAP_WAD
    r_scaled = R_UNITS * FLAP_WAD
    k_scaled = R_UNITS * (H_UNITS + SUPPLY_UNITS) * FLAP_WAD
    # out(0) == 0 is the identity: x + h == ceil(K * WAD / (y + r)).
    quote = r_scaled + reserve_base
    sold = supply_atoms + h_scaled - -(-(k_scaled * FLAP_WAD) // quote)
    return FlapCurve(
        r_scaled=r_scaled,
        h_scaled=h_scaled,
        k_scaled=k_scaled,
        total_supply_atoms=supply_atoms,
        tokens_sold_atoms=sold,
        graduation_tokens_atoms=8 * 10**26,
        quote_reserve_base=reserve_base,
        token_decimals=FLAP_CURVE_DECIMALS,
        quote_decimals=quote_decimals,
    )


#: 10,000 quote units raised. At the measured prices that is about 12.7 BNB of depth
#: against a 0.02 BNB order.
DEFAULT_RESERVE = 10_000 * 10**18


# --------------------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def _protectable(monkeypatch):
    """Let the protection gate pass, so these tests measure the RATE and not the gate.

    Added 2026-09-22 with `viability.quote_asset_is_protectable`, which refuses a quote
    asset the WATCHDOG cannot price even when the sizing path can price it perfectly. That
    gate exists because USDG -- the quote leg of 719 robinhood tokens -- is priced fine by
    `gmgn_cli` and not at all by the protection path, and a position opened against it
    would go blind with no working stop.

    The gate has its own file, `tests/test_quote_asset_protectable.py`. Here it is stood
    down deliberately: every test below is about the conversion arithmetic, and leaving
    the gate live would make each of them refuse for a reason they are not testing.
    """
    from kaiba.execution import viability as _V
    from kaiba.execution import watchdog as _wd

    class _Q:
        price_usd = __import__("decimal").Decimal("1")
        usable = True
        note = None

    class _Src:
        def quote(self, chain, token):
            return _Q()

    _V.reset_protectable_cache()
    monkeypatch.setattr(_wd, "configured_price_source_name", lambda *a, **k: "venue")
    monkeypatch.setattr(_wd, "resolve_price_source", lambda name: _Src())
    yield
    _V.reset_protectable_cache()


@pytest.fixture(autouse=True)
def _clean_caches():
    reset_venue_cache()
    reset_quote_asset_cache()
    yield
    reset_venue_cache()
    reset_quote_asset_cache()


@pytest.fixture(autouse=True)
def _pin_gas(monkeypatch):
    monkeypatch.setattr(
        viability, "_evm_gas_price_wei", lambda chain, conn: (GAS_PRICE_WEI, "gas:test")
    )


@pytest.fixture
def risk_yaml(tmp_path, monkeypatch):
    path = tmp_path / "risk.yaml"
    monkeypatch.setenv("KAIBA_RISK_PATH", str(path))
    path.write_text(yaml.safe_dump(copy.deepcopy(BASE_RISK)), encoding="utf-8")
    return path


@pytest.fixture
def flap(monkeypatch):
    """Replace the portal read with a canned curve and count the probes it costs."""
    state: dict = {
        "quote_token": USDT,
        "reserve_base": DEFAULT_RESERVE,
        "quote_decimals": FLAP_CURVE_DECIMALS,
        "refusal": None,          # a read_flap-level refusal instead of a curve
        "calls": [],
    }

    def fake(token, rpc):
        state["calls"].append(token)
        if state["refusal"] is not None:
            return None, state["refusal"]
        curve = flap_curve(
            reserve_base=state["reserve_base"], quote_decimals=state["quote_decimals"]
        )
        return (
            VenuePrice(
                price_quote_per_token=Decimal("0.00002"),
                quote_token=state["quote_token"],
                quote_reserve_base=state["reserve_base"],
                quote_decimals=state["quote_decimals"],
                venue="flap",
                observed_ms=now_ms(),
                note="flap:0x7777",
                curve=curve,
            ),
            "",
        )

    monkeypatch.setattr(evm_price, "read_flap", fake)
    monkeypatch.setattr(evm_price, "json_rpc_batch", lambda *a, **kw: (lambda calls: []))
    return state


def info_payload(
    *, symbol: str = "USDT", decimals: int | None = 18, price: str | None = USDT_PRICE_USD
) -> dict:
    body: dict = {"address": USDT, "symbol": symbol, "price": {"price": price} if price else {}}
    if decimals is not None:
        body["decimals"] = decimals
    return body


@pytest.fixture
def gmgn(monkeypatch):
    state: dict = {"payload": info_payload(), "basis": EvidenceBasis.PROVIDER_REPORTED,
                   "observed_at_ms": None, "calls": []}

    def fake(address, chain=Chain.SOL, **kwargs):
        state["calls"].append((address, chain))
        observed = state["observed_at_ms"]
        return gmgn_cli.GmgnResult(
            state["payload"],
            Receipt(provider="gmgn", endpoint="token.info", basis=state["basis"],
                    observed_at_ms=observed if observed is not None else now_ms()),
        )

    monkeypatch.setattr(gmgn_cli, "token_info", fake)
    return state


def put_native(conn, price: str = BNB_USD, *, age_ms: int = 0) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO native_prices (chain, ts_ms, price_usd, source, receipt_json) "
        "VALUES (?,?,?,?,?)",
        (BSC.value, now_ms() - int(age_ms), price, "dexscreener", "{}"),
    )
    conn.commit()


def put_dossier(conn, token: str = TOKEN, *, tax_bps: str = "0") -> None:
    """A bsc dossier carrying both tax legs, which is what ``read_dex_venue`` needs."""
    def measure(value: str) -> dict:
        return {"value": value, "basis": "provider_reported",
                "receipt": {"provider": "gmgn", "endpoint": "token.security",
                            "observed_at_ms": now_ms(), "basis": "provider_reported"},
                "freshness_budget_s": 900}

    body = {"address": token, "chain": BSC.value, "built_at_ms": now_ms(), "grade": "B",
            "buy_tax_bps": measure(tax_bps), "sell_tax_bps": measure(tax_bps)}
    conn.execute(
        "INSERT OR REPLACE INTO token_dossiers (chain, address, built_at_ms, score, grade, "
        "blockers_json, warnings_json, unknowns_json, dossier_json) VALUES (?,?,?,?,?,?,?,?,?)",
        (BSC.value, token, now_ms(), None, "B", "[]", "[]", "[]", json.dumps(body)),
    )
    conn.commit()


def ready(conn, *, tax_bps: str = "0", token: str = TOKEN) -> None:
    put_native(conn)
    put_dossier(conn, token, tax_bps=tax_bps)


# ------------------------------------------------------------------- the fixture is real


def test_the_fixture_curve_actually_prices_a_fill():
    """Without this the whole file could pass by refusing everything.

    ``tests/test_flap_curve_refusals.py``'s fixture satisfies the preconditions but not the
    curve identity, so it prices nothing; this one has to do the opposite.
    """
    curve = flap_curve(reserve_base=DEFAULT_RESERVE)
    assert curve.refusal is None
    assert curve.max_size_base_units is not None and curve.max_size_base_units > 0
    out = curve.tokens_out(10 * 10**18)
    assert out is not None and out > 0


# ------------------------------------------------------------------ the refusals first


def test_no_price_for_the_quote_asset_refuses_and_stops_the_ladder(
    tmp_db, flap, gmgn, risk_yaml
):
    """60% of bsc used to die here, and it must still die here when the rate is missing.

    The ``flap_probe_unavailable:`` prefix is load-bearing: this token IS on a curve, so a
    pool's depth is not the depth its fill would meet, and ``resolve_depth`` has to stop
    rather than walk on to the dossier and the pair.
    """
    ready(tmp_db)
    gmgn["payload"] = info_payload(price=None)

    venue = read_venue(BSC, TOKEN, tmp_db)
    assert not venue.priced
    assert venue.note.startswith(viability.FLAP_PROBE_UNAVAILABLE), venue.note
    assert "quote_not_native" in venue.note and "quote_price_unavailable" in venue.note
    depth = resolve_depth(BSC, TOKEN, tmp_db)
    assert isinstance(depth, NoDepth)
    assert depth.source.startswith(viability.FLAP_PROBE_UNAVAILABLE), depth.source
    assert not check_size(BSC, MAX_POSITION, tmp_db, token=TOKEN).ok


@pytest.mark.parametrize(
    ("payload_kw", "expected"),
    [
        ({"price": "0"}, "quote_price_not_positive"),
        ({"price": "-1"}, "quote_price_not_positive"),
        ({"price": "NaN"}, "quote_price_unavailable"),
        ({"decimals": None}, "quote_decimals_unreadable"),
        ({"decimals": 99}, "quote_decimals_unreadable"),
    ],
)
def test_an_untrustworthy_quote_price_refuses_by_name(
    tmp_db, flap, gmgn, risk_yaml, payload_kw, expected
):
    """The same refusal set as the Pons arm, because it is the same function."""
    ready(tmp_db)
    gmgn["payload"] = info_payload(**payload_kw)

    venue = read_venue(BSC, TOKEN, tmp_db)
    assert not venue.priced
    assert expected in venue.note, venue.note


def test_a_stale_quote_price_refuses(tmp_db, flap, gmgn, risk_yaml):
    ready(tmp_db)
    gmgn["observed_at_ms"] = now_ms() - int(viability.QUOTE_ASSET_PRICE_MAX_AGE_S * 1000) - 60_000

    venue = read_venue(BSC, TOKEN, tmp_db)
    assert not venue.priced and "quote_price_stale" in venue.note, venue.note


def test_no_native_price_refuses(tmp_db, flap, gmgn, risk_yaml):
    """bsc prices itself — it has its own ``WRAPPED_NATIVE`` entry — but a chain with no
    stored sample still has half a ratio."""
    put_dossier(tmp_db)  # no put_native

    venue = read_venue(BSC, TOKEN, tmp_db)
    assert not venue.priced and "native_usd_unavailable" in venue.note, venue.note


def test_a_non_wad_quote_asset_is_still_refused_by_the_curve(tmp_db, flap, gmgn, risk_yaml):
    """The decimals gate belongs to ``evm_price``, and converting the size does not lift it.

    LibCurve adds a 1e18-scaled constant to a base-unit count, so a 6-decimal side is not
    a small number, it is a number in different units. ``FlapCurve`` refuses it and this
    change deliberately leaves that refusal exactly where it was — unlike robinhood, where
    USDG at 6 decimals is the second-most-used quote asset and IS converted, because that
    curve's arithmetic carries no such assumption.
    """
    ready(tmp_db)
    flap["quote_decimals"] = 6
    gmgn["payload"] = info_payload(decimals=6)

    venue = read_venue(BSC, TOKEN, tmp_db)
    assert not venue.priced
    assert "flap_decimals_not_wad" in venue.note, venue.note


def test_a_portal_that_did_not_answer_still_refuses(tmp_db, flap, gmgn, risk_yaml):
    """5 of the 20 measured refusals were this, and it is not what this change is about."""
    ready(tmp_db)
    flap["refusal"] = "flap_portal_read_failed"

    venue = read_venue(BSC, TOKEN, tmp_db)
    assert not venue.priced
    assert venue.note == f"{viability.FLAP_PROBE_UNAVAILABLE}flap_portal_read_failed", venue.note
    assert gmgn["calls"] == [], "no rate is needed for a curve we could not read"


# ------------------------------------------------------------------------- the happy path


def test_a_usdt_quoted_flap_curve_is_priced_and_sized(tmp_db, flap, gmgn, risk_yaml):
    """The whole point: 12 of 20 bsc candidates were refused outright and now have a band."""
    ready(tmp_db)

    venue = read_venue(BSC, TOKEN, tmp_db)
    assert venue.priced, venue.note
    assert isinstance(venue.depth, FlapCurveDepth)
    assert venue.depth.rate is not None
    band = sizing_band(BSC, tmp_db, token=TOKEN)
    assert band.viable, band.reason
    assert band.contains(MAX_POSITION), band.reason
    assert check_size(BSC, MAX_POSITION, tmp_db, token=TOKEN).ok


def test_the_receipt_names_the_quote_asset_and_the_rate(tmp_db, flap, gmgn, risk_yaml):
    ready(tmp_db)

    venue = read_venue(BSC, TOKEN, tmp_db)
    assert venue.priced
    assert "USDT" in venue.depth.source, venue.depth.source
    assert USDT[:10] in venue.depth.source, venue.depth.source
    assert "18dp" in venue.depth.source, venue.depth.source
    assert USDT_PRICE_USD in venue.depth.source and BNB_USD in venue.depth.source


def test_a_converted_depth_is_not_claimed_as_verified_onchain(tmp_db, flap, gmgn, risk_yaml):
    """Half of it is a provider's opinion about what the quote asset is worth."""
    ready(tmp_db)
    assert resolve_depth(BSC, TOKEN, tmp_db).basis is EvidenceBasis.DERIVED


def test_a_bnb_quoted_curve_is_untouched(tmp_db, flap, gmgn, risk_yaml):
    """The curves that already worked must not acquire a provider dependency."""
    ready(tmp_db)
    flap["quote_token"] = None

    venue = read_venue(BSC, TOKEN, tmp_db)
    assert venue.priced
    assert venue.depth.rate is None
    assert venue.depth.basis is EvidenceBasis.VERIFIED_ONCHAIN
    assert venue.routing_bps_per_leg == Decimal(0)
    assert gmgn["calls"] == [], "a BNB-quoted curve needs no FX leg"


# ------------------------------------------------------------------------- the arithmetic


def test_the_size_is_converted_into_the_curves_own_currency(tmp_db, flap, gmgn, risk_yaml):
    """A conversion that did nothing would price 0.02 BNB as 0.02 USDT — 785x too small.

    Read as quote units our size is a rounding error against a 10,000-unit curve and the
    impact would be ~0. Converted it is about 15.7 units, which is a real order.
    """
    ready(tmp_db)
    converted = read_venue(BSC, TOKEN, tmp_db).depth
    raw = FlapCurveDepth(curve=flap_curve(reserve_base=DEFAULT_RESERVE))

    a = converted.entry_impact(MAX_POSITION)
    b = raw.entry_impact(MAX_POSITION)
    assert a is not None and b is not None
    assert a > b, (a, b)


def test_a_dearer_quote_asset_buys_more_depth(tmp_db, flap, gmgn, risk_yaml):
    """Monotonicity: getting the ratio upside down still produces numbers.

    A reserve of N units of a dearer asset is a deeper pool, so the same order costs LESS
    impact when the quote asset is worth more.
    """
    ready(tmp_db)
    cheap = read_venue(BSC, TOKEN, tmp_db).depth.entry_impact(MAX_POSITION)

    reset_venue_cache()
    reset_quote_asset_cache()
    gmgn["payload"] = info_payload(price=str(Decimal(USDT_PRICE_USD) * 10))
    dear = read_venue(BSC, TOKEN, tmp_db).depth.entry_impact(MAX_POSITION)

    assert cheap is not None and dear is not None
    assert dear < cheap, (dear, cheap)


def test_the_largest_order_the_curve_can_take_is_converted_too(tmp_db, flap, gmgn, risk_yaml):
    """``max_size_base_units`` caps the band. In quote units it is 785x the wei figure."""
    ready(tmp_db)
    converted = read_venue(BSC, TOKEN, tmp_db).depth
    raw = FlapCurveDepth(curve=flap_curve(reserve_base=DEFAULT_RESERVE))

    assert converted.max_size_base_units is not None
    assert raw.max_size_base_units is not None
    assert converted.max_size_base_units < raw.max_size_base_units


def test_the_two_conversion_directions_are_one_rational():
    """``to_quote_base_units`` is the inverse of ``to_native_base_units``, not a second rate.

    It rounds the other way on purpose — up, because this direction asks a venue what OUR
    order would do, and a slightly larger order is the cautious question. So a round trip
    through both never loses value, which is what "conservative in both directions" means
    when the two are composed.
    """
    rate = viability.QuoteAssetRate(
        chain=BSC, token=USDT, symbol="USDT", decimals=18,
        quote_usd=Decimal(USDT_PRICE_USD), native_usd=Decimal(BNB_USD),
        observed_ms=now_ms(), source="test",
    )
    for wei in (MIN_POSITION, MAX_POSITION, 10**18, 1):
        quote = rate.to_quote_base_units(wei)
        assert quote > 0
        assert rate.to_native_base_units(quote) >= wei - 1, wei
    assert rate.to_quote_base_units(0) == 0
    assert rate.to_quote_base_units(-1) == 0
    # One whole BNB is worth about 785 whole USDT at the measured prices.
    whole = rate.to_quote_base_units(10**18)
    assert 784 * 10**18 < whole < 786 * 10**18, whole

    # The rounding itself, on a value that is not exact: one wei of BNB is 785.10... quote
    # base units, and the venue must be asked about the 786 rather than the 785. Floor
    # here would mean asking about a smaller order than we intend to place, which
    # under-reports the impact -- the one direction this module never rounds.
    qn, qd = Decimal(USDT_PRICE_USD).as_integer_ratio()
    nn, nd = Decimal(BNB_USD).as_integer_ratio()
    num, den = nn * qd * 10**18, nd * qn * 10**18
    assert num % den != 0, "the fixture must exercise a fractional case"
    assert rate.to_quote_base_units(1) == num // den + 1


# ---------------------------------------------------------------------------- the hop


def test_the_crossing_into_the_quote_asset_is_charged_on_bsc_too(tmp_db, flap, gmgn, risk_yaml):
    """Our order is in BNB and the curve cannot take BNB, exactly as on robinhood."""
    ready(tmp_db)

    venue = read_venue(BSC, TOKEN, tmp_db)
    assert venue.routing_bps_per_leg == Decimal(
        viability.DEX_FEE_BPS_UPPER[BSC] * viability.ROUTING_HOPS_NON_NATIVE_QUOTE
    )
    model = viability.evm_cost_model(BSC, venue)
    # pool-fee bound 100 + tax 0 + router 100 + hop 100.
    assert model.proportional_bps_per_leg == Decimal(300), model.proportional_bps_per_leg
    assert f"+router{viability.ROUTER_BPS_PER_LEG}bps+hop100bps" in model.source, model.source
    assert "hop100bps" in venue.note, venue.note


def test_the_crossing_is_what_closes_the_window_on_a_taxed_curve(tmp_db, flap, gmgn, risk_yaml):
    """100 bps of token tax clears a 7% ceiling without the crossing and not with it."""
    ready(tmp_db, tax_bps="100")
    with_hop = sizing_band(BSC, tmp_db, token=TOKEN)

    reset_venue_cache()
    reset_quote_asset_cache()
    flap["quote_token"] = None
    without = sizing_band(BSC, tmp_db, token=TOKEN)

    assert without.viable, without.reason
    assert not with_hop.viable
    assert with_hop.reason.startswith("no_viable_size:cheapest_is_"), with_hop.reason


# ------------------------------------------------------------------- one shared mechanism


def test_the_rate_is_cached_per_quote_asset_across_tokens(tmp_db, flap, gmgn, risk_yaml):
    """Two Flap tokens quoted in USDT cost one provider call between them."""
    ready(tmp_db)
    put_dossier(tmp_db, OTHER_TOKEN)

    read_venue(BSC, TOKEN, tmp_db)
    reset_venue_cache()
    read_venue(BSC, OTHER_TOKEN, tmp_db)

    assert len(gmgn["calls"]) == 1, gmgn["calls"]
    assert gmgn["calls"][0] == (USDT, BSC)


def test_the_cache_is_per_chain_as_well_as_per_asset(tmp_db, flap, gmgn, risk_yaml):
    """The same address means different things on different chains, and the rate is a
    ratio against a *chain's* native asset — BNB here, ETH there."""
    put_native(tmp_db)
    tmp_db.execute(
        "INSERT OR REPLACE INTO native_prices (chain, ts_ms, price_usd, source, receipt_json) "
        "VALUES (?,?,?,?,?)",
        (Chain.ETH.value, now_ms(), "2741.02", "dexscreener", "{}"),
    )
    tmp_db.commit()

    bsc_rate, _ = viability.quote_asset_rate(BSC, USDT, tmp_db)
    rh_rate, _ = viability.quote_asset_rate(Chain.ROBINHOOD, USDT, tmp_db)
    assert bsc_rate is not None and rh_rate is not None
    assert bsc_rate.native_usd == Decimal(BNB_USD)
    assert rh_rate.native_usd == Decimal("2741.02")
    assert len(gmgn["calls"]) == 2, gmgn["calls"]


def test_both_arms_use_the_same_rate_object(tmp_db, flap, gmgn, risk_yaml):
    """One mechanism, not two: the Flap depth carries the very object the resolver made."""
    ready(tmp_db)
    depth = read_venue(BSC, TOKEN, tmp_db).depth
    cached, _ = viability.quote_asset_rate(BSC, USDT, tmp_db)
    assert depth.rate is cached
