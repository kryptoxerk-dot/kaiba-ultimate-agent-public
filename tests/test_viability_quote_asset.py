"""Pons curves quoted in a tokenised stock: the conversion, and every way it refuses.

The operator authorised these pairs. Before this file existed the sizing gate refused
every one of them outright (``pons:quote_not_native``), and the refusal was correct as
written: our size is in wei, the curve's reserves are in some other ERC-20's base units,
and adding the two is a unit error dressed as a number. The fix is to **get the missing
rate**, not to delete the check -- so the property this file defends is that the rate is
either present and named in the receipt, or absent and the entry still refuses.

Why it is worth the trouble (MEASURED on the live box, 2026-09-22):

* over 60 recent robinhood dossiers: 41 native-quoted, **10 non-native**, 6 with no curve
  and 3 graduated -- so this is about **17%** of robinhood;
* across all robinhood tokens: **59 distinct non-native quote assets covering 2,538
  tokens**, the top one used by 708 of them. That ratio is why the rate is cached **per
  quote asset** and not per token: a per-token lookup would be 2,538 provider calls to
  learn 59 answers. The local ``tokens`` table read the same shape the same day: **1,890
  of 8,963** tokens (21%) over **57** assets, top one 566 and next 468.

The quote asset used throughout is the one that was read live on 2026-09-22:
``0x1b0e319c6a659f002271b69db8a7df2f911c153e`` -- ``GME``, 18 decimals, ``price.price``
23.63952947 USD, liquidity $2,021,746.46 from ``gmgn-cli token info --chain robinhood``.
The native leg is ETH at 2741.02 USD, read the same day (Robinhood Chain's native asset
IS ETH; ``native_price.NATIVE_PRICE_ALIAS`` is the identity that says so).

The second asset here is ``USDG`` and it is not decoration: at **6 decimals** and 468
tokens it is the case that makes "assume 18" a silent failure rather than a style
complaint.

Nothing here touches the network: ``gmgn_cli.token_info`` and ``robinhood.rpc_batch`` are
both replaced, and the native price is a row this file writes into ``native_prices``.
"""

from __future__ import annotations

import copy
import json
from decimal import Decimal

import pytest
import yaml

from kaiba.core.schemas import Chain, EvidenceBasis, Receipt, now_ms
from kaiba.execution import viability
from kaiba.execution.viability import (
    NoDepth,
    QuoteAssetRate,
    check_size,
    quote_asset_rate,
    read_venue,
    reset_quote_asset_cache,
    reset_venue_cache,
    resolve_depth,
    sizing_band,
)
from kaiba.providers import gmgn_cli

# The measured curve, the operator's configured sizes and the canned batch all come from
# the EVM sizing-gate suite rather than being retyped here: these tests are about the
# quote leg, and a second copy of a curve read off the chain on 2026-09-21 would be a
# second thing to keep in step.
from tests.test_viability_evm import (
    BASE_RISK,
    CURVE,
    CURVE_ADDRESS,
    MAX_POSITION,
    MIN_POSITION,
    TOKEN,
    batch_results,
)

RH = Chain.ROBINHOOD

#: The quote asset read live on 2026-09-22. Every field below is from that one response.
GME = "0x1b0e319c6a659f002271b69db8a7df2f911c153e"
GME_SYMBOL = "GME"
GME_DECIMALS = 18
GME_PRICE_USD = "23.63952947"
GME_LIQUIDITY_USD = 2_021_746.464244

#: ETH/USD on 2026-09-22, the same reading ``native_price`` was verified against.
ETH_USD = "2741.02"

#: The second-most-used non-native quote asset on this venue, and the reason "assume 18"
#: is not an option. MEASURED 2026-09-22 against the local ``tokens`` table and the two
#: providers: ``0x5fc5360d...`` is **USDG, 6 decimals**, price 1.00064189754,
#: liquidity $19,468,001, and it is the quote leg of **468 of the 1,890** non-native
#: robinhood tokens -- a quarter of them. Reading its reserve as though it had 18 decimals
#: would understate the curve's depth by a factor of 1e12.
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
USDG_DECIMALS = 6
USDG_PRICE_USD = "1.00064189754"

ZERO_ADDRESS = "0x" + "0" * 40


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
    """Both memos are process-global; a test must not inherit another test's rate."""
    reset_venue_cache()
    reset_quote_asset_cache()
    yield
    reset_venue_cache()
    reset_quote_asset_cache()


@pytest.fixture
def risk_yaml(tmp_path, monkeypatch):
    path = tmp_path / "risk.yaml"
    monkeypatch.setenv("KAIBA_RISK_PATH", str(path))
    path.write_text(yaml.safe_dump(copy.deepcopy(BASE_RISK)), encoding="utf-8")
    return path


@pytest.fixture
def rpc(monkeypatch):
    """Replace the chain with a canned curve batch and count the round trips it costs."""
    from kaiba.ingest import robinhood as pons

    state: dict = {"results": batch_results(), "ok": True, "note": None, "calls": []}

    def fake(calls, **kwargs):
        state["calls"].append(list(calls))
        return pons.RpcResult(list(state["results"]), state["ok"], state["note"], 1)

    monkeypatch.setattr(pons, "rpc_batch", fake)
    return state


def info_payload(
    *,
    address: str = GME,
    symbol: str = GME_SYMBOL,
    decimals: int | None = GME_DECIMALS,
    price: str | None = GME_PRICE_USD,
    liquidity: float | None = GME_LIQUIDITY_USD,
) -> dict:
    """``gmgn-cli token info --raw`` as it answered for GME on 2026-09-22.

    The price sits inside the nested ``price`` block exactly as GMGN sends it, so the
    parser has to go through ``gmgn_cli.flatten_payload`` rather than a top-level lookup.
    """
    body: dict = {
        "address": address,
        "symbol": symbol,
        "name": f"{symbol} - Robinhood Token",
        "price": {"price": price} if price is not None else {},
    }
    if decimals is not None:
        body["decimals"] = decimals
    if liquidity is not None:
        body["liquidity"] = liquidity
    return body


@pytest.fixture
def gmgn(monkeypatch):
    """Replace ``token_info`` with a canned answer and count the calls it costs."""
    state: dict = {
        "payload": info_payload(),
        "basis": EvidenceBasis.PROVIDER_REPORTED,
        "observed_at_ms": None,  # None -> now
        "note": None,
        "calls": [],
    }

    def fake(address, chain=Chain.SOL, **kwargs):
        state["calls"].append((address, chain))
        observed = state["observed_at_ms"]
        receipt = Receipt(
            provider="gmgn",
            endpoint="token.info",
            basis=state["basis"],
            observed_at_ms=observed if observed is not None else now_ms(),
            note=state["note"],
        )
        payload = state["payload"]
        return gmgn_cli.GmgnResult(payload, receipt)

    monkeypatch.setattr(gmgn_cli, "token_info", fake)
    return state


#: Whole GME per whole ETH at the two measured prices: 2741.02 / 23.63952947 = 115.95,
#: rounded to 116. Used to build a curve of the SAME economic size as the measured
#: native-quoted one but denominated in the stock — which is what a GME-quoted Pons curve
#: is. Without it the fixture would describe a curve holding 1.7 GME (about $40), and a
#: band that refused it would be right for reasons that have nothing to do with the
#: conversion.
QUOTE_SCALE = 116


def stock_curve(**overrides) -> list:
    """The measured curve batch with its quote leg denominated in GME instead of ETH."""
    scaled = {
        name: CURVE[name] * QUOTE_SCALE
        for name in ("quote_reserve", "real_quote_reserve", "graduation_threshold")
    }
    return batch_results(**{**scaled, **overrides})


def put_native(conn, price: str = ETH_USD, *, age_ms: int = 0) -> None:
    """One ETH sample. Robinhood Chain's native price is read from ETH's rows."""
    conn.execute(
        "INSERT OR REPLACE INTO native_prices (chain, ts_ms, price_usd, source, receipt_json) "
        "VALUES (?,?,?,?,?)",
        (Chain.ETH.value, now_ms() - int(age_ms), price, "dexscreener", "{}"),
    )
    conn.commit()


def register(
    conn,
    *,
    token: str = TOKEN,
    curve: str = CURVE_ADDRESS,
    pair_token: str = GME,
    quote_is_native: bool | None = False,
    migrated_ms: int | None = None,
) -> None:
    """A ``tokens`` row shaped as ``ingest.robinhood`` writes one, with a real quote asset.

    ``quote_is_native=None`` omits the key entirely, which is what a row written before
    that field existed looks like.
    """
    meta: dict = {"curve": curve, "pair_token": pair_token}
    if quote_is_native is not None:
        meta["quote_is_native"] = quote_is_native
    conn.execute(
        "INSERT OR REPLACE INTO tokens (chain, address, launchpad, pool, created_ms, "
        "migrated_ms, first_seen_ms, meta_json) VALUES (?,?,?,?,?,?,?,?)",
        (RH.value, token, "pons", curve, now_ms() - 3_600_000, migrated_ms, now_ms(),
         json.dumps(meta)),
    )
    conn.commit()


# ------------------------------------------------------------------ the refusals first
#
# Each of these is a state in which the rate is missing or untrustworthy. The property is
# always the same and it is the one the module is built on: a missing number never becomes
# a favourable one. Not 1.0 (which would read a $23 stock as if it were an ETH), not 0
# (which would read the curve as bottomless), but a refusal that names what was missing.


def test_no_price_for_the_quote_asset_refuses_and_names_it(tmp_db, rpc, gmgn, risk_yaml):
    """The provider answered with no price. That is blindness, not a free curve."""
    put_native(tmp_db)
    register(tmp_db)
    gmgn["payload"] = info_payload(price=None)

    venue = read_venue(RH, TOKEN, tmp_db)
    assert not venue.priced
    assert "quote_not_native" in venue.note
    assert "quote_price_unavailable" in venue.note, venue.note
    # Every rate term is absent rather than defaulted. A 1.0 here would price a $23.64
    # stock as one $2,741 ETH and read the curve as 116x deeper than it is.
    assert venue.fee_bps_per_leg is None and venue.tax_bps_per_leg is None
    assert not check_size(RH, MAX_POSITION, tmp_db, token=TOKEN).ok


def test_a_provider_refusal_refuses(tmp_db, rpc, gmgn, risk_yaml):
    """An UNAVAILABLE receipt is the limiter, a 429 or a dead CLI. All of them refuse."""
    put_native(tmp_db)
    register(tmp_db)
    gmgn["basis"] = EvidenceBasis.UNAVAILABLE
    gmgn["note"] = "limiter refused"

    venue = read_venue(RH, TOKEN, tmp_db)
    assert not venue.priced
    assert "quote_not_native" in venue.note and "quote_price_unavailable" in venue.note


def test_a_stale_quote_price_refuses_with_the_age_in_the_note(tmp_db, rpc, gmgn, risk_yaml):
    """Past the freshness budget the rate is a number about a different market."""
    put_native(tmp_db)
    register(tmp_db)
    budget_ms = int(viability.QUOTE_ASSET_PRICE_MAX_AGE_S * 1000)
    gmgn["observed_at_ms"] = now_ms() - budget_ms - 60_000
    gmgn["basis"] = EvidenceBasis.STALE

    venue = read_venue(RH, TOKEN, tmp_db)
    assert not venue.priced
    assert "quote_price_stale" in venue.note, venue.note
    assert not check_size(RH, MAX_POSITION, tmp_db, token=TOKEN).ok


def test_a_quote_price_inside_the_budget_is_accepted(tmp_db, rpc, gmgn, risk_yaml):
    """The boundary matters in both directions: a tight budget that refuses everything is
    the same silent failure as a loose one that accepts anything."""
    put_native(tmp_db)
    register(tmp_db)
    gmgn["observed_at_ms"] = now_ms() - int(viability.QUOTE_ASSET_PRICE_MAX_AGE_S * 1000) + 5_000

    rate, why = quote_asset_rate(RH, GME, tmp_db)
    assert rate is not None, why


def test_a_zero_quote_price_refuses(tmp_db, rpc, gmgn, risk_yaml):
    """Zero is the provider's way of saying nothing, and it divides a curve into infinity."""
    put_native(tmp_db)
    register(tmp_db)
    gmgn["payload"] = info_payload(price="0")

    venue = read_venue(RH, TOKEN, tmp_db)
    assert not venue.priced
    assert "quote_price_not_positive" in venue.note, venue.note


def test_a_negative_quote_price_refuses(tmp_db, rpc, gmgn, risk_yaml):
    put_native(tmp_db)
    register(tmp_db)
    gmgn["payload"] = info_payload(price="-1.5")

    venue = read_venue(RH, TOKEN, tmp_db)
    assert not venue.priced
    assert "quote_price_not_positive" in venue.note, venue.note


def test_unreadable_decimals_refuse_rather_than_defaulting_to_eighteen(
    tmp_db, rpc, gmgn, risk_yaml
):
    """18 is the common answer and assuming it is how a depth comes out by 1e10.

    The Solana twin of this venue records tokenised equities at 8 decimals
    (``ingest/stonkfun.py``), so "most ERC-20s are 18" is not an argument about this
    class of asset at all.
    """
    put_native(tmp_db)
    register(tmp_db)
    gmgn["payload"] = info_payload(decimals=None)

    venue = read_venue(RH, TOKEN, tmp_db)
    assert not venue.priced
    assert "quote_decimals_unreadable" in venue.note, venue.note


@pytest.mark.parametrize("bad", [-1, 37, "eighteen", 18.5])
def test_implausible_decimals_refuse(tmp_db, rpc, gmgn, risk_yaml, bad):
    put_native(tmp_db)
    register(tmp_db)
    gmgn["payload"] = info_payload(decimals=bad)

    venue = read_venue(RH, TOKEN, tmp_db)
    assert not venue.priced
    assert "quote_decimals_unreadable" in venue.note, venue.note


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity", "sNaN"])
def test_a_non_finite_quote_price_refuses_without_raising(tmp_db, rpc, gmgn, risk_yaml, bad):
    """``Decimal("NaN") <= 0`` RAISES InvalidOperation, and this function never raises.

    An infinity is the other half: it compares as positive, survives the sign check and
    then makes the curve infinitely deep. Both are the provider sending something that is
    not a price, and both refuse before either can happen.
    """
    put_native(tmp_db)
    register(tmp_db)
    gmgn["payload"] = info_payload(price=bad)

    rate, why = quote_asset_rate(RH, GME, tmp_db)
    assert rate is None and "quote_price_unavailable" in why, why
    venue = read_venue(RH, TOKEN, tmp_db)
    assert not venue.priced
    assert "quote_not_native" in venue.note, venue.note


def test_the_curve_state_does_not_claim_a_native_quote(
    tmp_db, rpc, gmgn, risk_yaml, monkeypatch
):
    """The ``CurveState`` handed to the shared reader has to be honest about its own quote.

    ``ingest/robinhood.curve_from_state`` gates its market-cap derivation on
    ``state.quote_is_native`` -- with the flag forced True it would multiply a
    GME-denominated reserve ratio by the ETH price and call the answer an FDV. Nothing on
    the sizing path reads it today, which is exactly why it would rot unnoticed.
    """
    from kaiba.ingest import robinhood as pons

    seen: dict = {}
    real = pons.parse_curve_state

    def spy(values, **kwargs):
        seen.update(kwargs)
        return real(values, **kwargs)

    monkeypatch.setattr(pons, "parse_curve_state", spy)

    put_native(tmp_db)
    register(tmp_db)
    assert read_venue(RH, TOKEN, tmp_db).priced
    assert seen["quote_is_native"] is False, seen

    reset_venue_cache()
    register(tmp_db, pair_token=ZERO_ADDRESS, quote_is_native=True)
    assert read_venue(RH, TOKEN, tmp_db).priced
    assert seen["quote_is_native"] is True, seen


def test_no_native_price_refuses(tmp_db, rpc, gmgn, risk_yaml):
    """The rate is quote_usd / native_usd. Half of it is not a rate.

    MEASURED 2026-09-22: ``native_prices`` held zero robinhood rows, which is exactly the
    state that made every robinhood entry unpriceable before the ETH alias existed.
    """
    register(tmp_db)  # no put_native

    venue = read_venue(RH, TOKEN, tmp_db)
    assert not venue.priced
    assert "native_usd_unavailable" in venue.note, venue.note


def test_a_stale_native_price_refuses(tmp_db, rpc, gmgn, risk_yaml):
    """``native_price.at`` owns that budget; this reader does not get a second opinion."""
    put_native(tmp_db, age_ms=3_600_000)

    rate, why = quote_asset_rate(RH, GME, tmp_db)
    assert rate is None and "native_usd_unavailable" in why, why


def test_a_contradictory_row_refuses_without_spending_a_round_trip(
    tmp_db, rpc, gmgn, risk_yaml
):
    """``quote_is_native=false`` with a zero ``pair_token`` names no asset to convert.

    This is the shape ``tests/test_viability_evm`` registers, and it must go on refusing:
    a curve whose quote asset we cannot even name is not one to spend an RPC batch or a
    provider call on.
    """
    put_native(tmp_db)
    register(tmp_db, pair_token=ZERO_ADDRESS, quote_is_native=False)

    venue = read_venue(RH, TOKEN, tmp_db)
    assert not venue.priced
    assert "quote_not_native" in venue.note and "quote_token_unknown" in venue.note
    assert rpc["calls"] == [], "a curve we cannot name the quote asset of is not worth a read"
    assert gmgn["calls"] == [], "nor a provider call"


def test_a_row_that_does_not_say_still_refuses_when_it_names_no_asset(
    tmp_db, rpc, gmgn, risk_yaml
):
    """``quote_is_native`` absent and no ``pair_token``: unknown, so refused."""
    put_native(tmp_db)
    register(tmp_db, pair_token="", quote_is_native=None)

    venue = read_venue(RH, TOKEN, tmp_db)
    assert not venue.priced
    assert "quote_token_unknown" in venue.note, venue.note
    assert rpc["calls"] == []


def test_a_quote_asset_worth_dust_refuses_rather_than_reporting_no_depth(
    tmp_db, rpc, gmgn, risk_yaml
):
    """A rate so small the curve's whole reserve floors to zero wei is not zero depth.

    Left to itself the converted reserve would be 0, ``PonsCurveDepth`` would decline to
    price any size, and the refusal would read as a depth problem rather than a rate one.
    """
    put_native(tmp_db)
    register(tmp_db)
    gmgn["payload"] = info_payload(price="0.000000000000000001")

    venue = read_venue(RH, TOKEN, tmp_db)
    assert not venue.priced
    assert "quote_conversion_underflow" in venue.note, venue.note


# ------------------------------------------------------------------------- the happy path


def test_a_stock_quoted_curve_is_tradable_and_the_note_says_how(tmp_db, rpc, gmgn, risk_yaml):
    """The whole point. 17% of robinhood was refused outright and now has a size band.

    The band has to contain the sizes the operator actually configured (0.00435-0.0075
    ETH), because a band that exists but excludes both numbers means the agent still
    never trades -- the same property ``test_viability_evm`` pins for the native case.
    """
    put_native(tmp_db)
    register(tmp_db)
    rpc["results"] = stock_curve(creator_tax_bps=0)

    band = sizing_band(RH, tmp_db, token=TOKEN)
    assert band.reason == "band", band.reason
    assert band.viable
    assert band.contains(MIN_POSITION) and band.contains(MAX_POSITION)
    assert check_size(RH, MIN_POSITION, tmp_db, token=TOKEN).ok
    assert check_size(RH, MAX_POSITION, tmp_db, token=TOKEN).ok


def test_the_crossing_into_the_quote_asset_is_charged(tmp_db, rpc, gmgn, risk_yaml):
    """Our order is in ETH and the curve cannot take ETH. Something has to cross a pool.

    ``engine`` denominates every EVM order in the chain's native asset
    (``input_token = EVM_ZERO``), so reaching a GME-quoted curve means buying GME on the
    way in and selling it on the way out. Leaving that out would be the same hole
    ``ROUTER_BPS_PER_LEG`` exists to have closed: a real cost missing from the round trip
    authorises trades that lose money.
    """
    put_native(tmp_db)
    register(tmp_db)
    rpc["results"] = stock_curve(creator_tax_bps=0)

    venue = read_venue(RH, TOKEN, tmp_db)
    assert venue.routing_bps_per_leg == Decimal(
        viability.DEX_FEE_BPS_UPPER[RH] * viability.ROUTING_HOPS_NON_NATIVE_QUOTE
    )
    model = viability.evm_cost_model(RH, venue)
    # fee 100 + tax 0 + router 100 + hop 100.
    assert model.proportional_bps_per_leg == Decimal(300), model.proportional_bps_per_leg
    # And it is spelled out in the rate breakdown -- not merely inherited from the venue
    # note the source also quotes -- so an operator can see which term to remove if a live
    # quote ever shows the crossing is free.
    assert f"+router{viability.ROUTER_BPS_PER_LEG}bps+hop100bps" in model.source, model.source
    assert "hop100bps" in venue.note, venue.note


def test_a_native_quoted_curve_is_charged_no_crossing(tmp_db, rpc, gmgn, risk_yaml):
    """There is nothing to cross: the order is already in the currency the curve takes."""
    put_native(tmp_db)
    register(tmp_db, pair_token=ZERO_ADDRESS, quote_is_native=True)
    rpc["results"] = batch_results(creator_tax_bps=0)

    venue = read_venue(RH, TOKEN, tmp_db)
    assert venue.routing_bps_per_leg == Decimal(0)
    model = viability.evm_cost_model(RH, venue)
    assert model.proportional_bps_per_leg == Decimal(200), model.proportional_bps_per_leg
    assert "hop" not in model.source, model.source


def test_the_crossing_is_what_closes_the_window_on_a_taxed_stock_curve(
    tmp_db, rpc, gmgn, risk_yaml
):
    """One term decides it, the same way the creator tax does on a native curve.

    fee 100 + tax 100 + router 100 = 300 bps a leg is 6% of a round trip, inside a 7%
    ceiling. The crossing takes it to 8% and no size works. A cost model that dropped the
    term would price these two identically.
    """
    put_native(tmp_db)
    register(tmp_db)
    rpc["results"] = stock_curve(creator_tax_bps=100)
    with_hop = sizing_band(RH, tmp_db, token=TOKEN)

    reset_venue_cache()
    register(tmp_db, pair_token=ZERO_ADDRESS, quote_is_native=True)
    rpc["results"] = batch_results(creator_tax_bps=100)
    without = sizing_band(RH, tmp_db, token=TOKEN)

    assert without.viable, without.reason
    assert not with_hop.viable
    assert with_hop.reason.startswith("no_viable_size:cheapest_is_"), with_hop.reason


def test_the_receipt_names_the_quote_asset_and_the_rate(tmp_db, rpc, gmgn, risk_yaml):
    """An operator reading a fill has to be able to tell the price was converted.

    A conversion nobody can see in the record is indistinguishable from a unit bug.
    """
    put_native(tmp_db)
    register(tmp_db)

    venue = read_venue(RH, TOKEN, tmp_db)
    assert venue.priced
    assert GME_SYMBOL in venue.note, venue.note
    assert GME[:10] in venue.note, venue.note
    assert f"{GME_DECIMALS}dp" in venue.note, venue.note
    # The rate itself, not just the fact that there was one.
    assert GME_PRICE_USD in venue.note and ETH_USD in venue.note, venue.note
    # And it survives into the depth's own provenance, which is what a decision record
    # prints as `depth_basis:...`.
    assert GME_SYMBOL in venue.depth.source, venue.depth.source


def test_a_converted_depth_is_not_claimed_as_verified_onchain(tmp_db, rpc, gmgn, risk_yaml):
    """Half of it is a provider's opinion about a stock price. DERIVED says so."""
    put_native(tmp_db)
    register(tmp_db)

    depth = resolve_depth(RH, TOKEN, tmp_db)
    assert not isinstance(depth, NoDepth)
    assert depth.basis is EvidenceBasis.DERIVED, depth.basis


def test_a_native_quoted_curve_is_untouched(tmp_db, rpc, gmgn, risk_yaml):
    """The 41 of 60 that were already tradable must not acquire a provider dependency."""
    put_native(tmp_db)
    register(tmp_db, pair_token=ZERO_ADDRESS, quote_is_native=True)

    venue = read_venue(RH, TOKEN, tmp_db)
    assert venue.priced
    assert venue.depth.basis is EvidenceBasis.VERIFIED_ONCHAIN
    assert gmgn["calls"] == [], "a native-quoted curve needs no FX leg"


def test_a_row_that_does_not_say_is_answered_by_its_pair_token(tmp_db, rpc, gmgn, risk_yaml):
    """``quote_is_native`` IS ``pair_token == ZERO`` (ingest/robinhood.py), so the address
    answers the question on a row written before the flag existed."""
    put_native(tmp_db)
    register(tmp_db, quote_is_native=None)

    venue = read_venue(RH, TOKEN, tmp_db)
    assert venue.priced
    assert GME_SYMBOL in venue.note, venue.note


# --------------------------------------------------------------------- the arithmetic


def test_the_conversion_is_exact_integer_arithmetic():
    """Money stays integers in base units, and the floor is the conservative direction.

    Floor on both reserves: a smaller quote reserve prices our order into MORE impact and
    a smaller headroom caps the size LOWER, so the rounding error can only ever refuse a
    trade the exact number would have allowed.
    """
    rate = QuoteAssetRate(
        chain=RH, token=GME, symbol=GME_SYMBOL, decimals=18,
        quote_usd=Decimal(GME_PRICE_USD), native_usd=Decimal(ETH_USD),
        observed_ms=now_ms(), source="test",
    )
    # 1 whole GME = 23.63952947 / 2741.02 ETH = 0.00862432... ETH.
    one_gme = 10**18
    got = rate.to_native_base_units(one_gme)
    exact = 10**18 * 2_363_952_947 * 100 // (100_000_000 * 274_102)
    assert got == exact
    assert isinstance(got, int)
    # Floor, never round-half-up: one atom in gives strictly less than one atom out here.
    assert rate.to_native_base_units(1) == 0
    assert rate.to_native_base_units(0) == 0
    assert rate.to_native_base_units(-5) == 0


def test_the_quote_assets_own_decimals_are_honoured():
    """USDG has 6 decimals and is the quote leg of 468 robinhood tokens.

    This is the failure mode assuming 18 produces, and it is silent: the same balance read
    at the wrong scale is a curve 1e12 times thinner, which refuses every size on a quarter
    of this venue without anything saying why.
    """
    common = dict(
        chain=RH, token=USDG, symbol="USDG",
        quote_usd=Decimal(USDG_PRICE_USD), native_usd=Decimal(ETH_USD),
        observed_ms=now_ms(), source="test",
    )
    six = QuoteAssetRate(decimals=USDG_DECIMALS, **common)
    eighteen = QuoteAssetRate(decimals=18, **common)
    # One whole unit is one whole unit, whatever the asset's scale.
    assert six.to_native_base_units(10**6) == eighteen.to_native_base_units(10**18)
    # And reading a 6-decimal balance as though it were an 18-decimal one is the silent
    # twelve-orders-of-magnitude error this guards against.
    assert six.to_native_base_units(10**18) // eighteen.to_native_base_units(10**18) == 10**12


def test_the_reverse_conversion_reads_the_assets_decimals_too():
    """``to_quote_base_units`` is the direction the Flap arm uses, and USDG is 6 decimals.

    Both directions read ``decimals``; only one of them is exercised by a curve whose
    conversion happens on the reserves, so the other is pinned here. With 18 assumed, a
    6-decimal answer comes out 1e12 too large and the venue is asked about an order a
    trillion times bigger than the one we would place.
    """
    common = dict(
        chain=RH, token=USDG, symbol="USDG",
        quote_usd=Decimal(USDG_PRICE_USD), native_usd=Decimal(ETH_USD),
        observed_ms=now_ms(), source="test",
    )
    six = QuoteAssetRate(decimals=USDG_DECIMALS, **common)
    eighteen = QuoteAssetRate(decimals=18, **common)

    # One whole ETH is 2741.02 USD and a USDG is 1.00064..., so about 2,739.2 whole USDG.
    assert 2_738 * 10**USDG_DECIMALS < six.to_quote_base_units(10**18) < 2_740 * 10**USDG_DECIMALS
    assert 2_738 * 10**18 < eighteen.to_quote_base_units(10**18) < 2_740 * 10**18
    # And one whole unit is one whole unit, the same identity the forward direction keeps.
    assert six.to_native_base_units(10**USDG_DECIMALS) == eighteen.to_native_base_units(10**18)


def test_a_stablecoin_quote_asset_converts_at_its_own_scale(tmp_db, rpc, gmgn, risk_yaml):
    """End to end on the real 6-decimal case, not just the arithmetic.

    A USDG-quoted curve holding N dollars must come out the same depth in wei as an
    ETH-quoted curve holding the same value -- which is the whole claim the conversion
    makes, and the one a hardcoded 18 would break on 468 tokens.
    """
    put_native(tmp_db)
    register(tmp_db, pair_token=USDG)
    gmgn["payload"] = info_payload(
        address=USDG, symbol="USDG", decimals=USDG_DECIMALS, price=USDG_PRICE_USD,
    )
    # One whole ETH of value, denominated in USDG base units at $2741.02/ETH.
    usdg_per_eth = int(Decimal(ETH_USD) / Decimal(USDG_PRICE_USD) * 10**USDG_DECIMALS)
    rpc["results"] = batch_results(
        quote_reserve=usdg_per_eth * 2,
        real_quote_reserve=usdg_per_eth // 10,
        graduation_threshold=usdg_per_eth * 4,
    )

    venue = read_venue(RH, TOKEN, tmp_db)
    assert venue.priced, venue.note
    assert "USDG" in venue.note and f"{USDG_DECIMALS}dp" in venue.note, venue.note
    # Two whole ETH of USDG reads back as ~2e18 wei. The tolerance is the rounding in the
    # fixture's own integer division, not in the conversion.
    assert abs(venue.depth.quote_reserve - 2 * 10**18) < 10**12, venue.depth.quote_reserve


def test_the_reserves_are_actually_converted_and_not_passed_through(
    tmp_db, rpc, gmgn, risk_yaml
):
    """A conversion that did nothing would leave the depth identical to the native case.

    GME is worth 0.0086 ETH, so the curve's reserve is worth ~116x less in wei than its
    raw integer suggests -- a shallower pool, a smaller band. If these two agreed, the
    quote leg would be decorative.
    """
    put_native(tmp_db)
    register(tmp_db)
    rpc["results"] = stock_curve()
    converted = sizing_band(RH, tmp_db, token=TOKEN)

    reset_venue_cache()
    reset_quote_asset_cache()
    register(tmp_db, pair_token=ZERO_ADDRESS, quote_is_native=True)
    native = sizing_band(RH, tmp_db, token=TOKEN)

    assert converted.optimal_base_units != native.optimal_base_units
    assert converted.optimal_base_units is not None and native.optimal_base_units is not None
    assert converted.optimal_base_units < native.optimal_base_units


def test_the_remaining_headroom_is_converted_too(tmp_db, rpc, gmgn, risk_yaml):
    """The reserve is not the only quote-denominated number on the curve.

    ``graduationThreshold - realQuoteReserve`` is how much quote the curve can still take,
    and it is what caps the largest order we may place: past it the buy is truncated or
    graduates the pool mid-fill, which is not a fill at the size we asked for. Left in GME
    units it reads 116x too large, and the gate would authorise an order the venue cannot
    complete. (On the 6-decimal USDG it would read 1e12 too SMALL and refuse everything --
    the same bug, the other way round, which is why this is about the conversion and not
    about a direction.)
    """
    put_native(tmp_db)
    register(tmp_db)
    scaled_real = CURVE["real_quote_reserve"] * QUOTE_SCALE
    # 0.1 GME-scaled units of headroom: more than the operator's smallest position when
    # read as wei, far less than it once converted.
    headroom_quote = 10**17
    rpc["results"] = stock_curve(
        real_quote_reserve=scaled_real,
        graduation_threshold=scaled_real + headroom_quote,
    )

    venue = read_venue(RH, TOKEN, tmp_db)
    assert venue.priced, venue.note
    assert venue.depth.headroom_base_units < headroom_quote // 100, (
        venue.depth.headroom_base_units
    )
    # And the cap it sets is below the smallest size the operator configured, so the band
    # cannot contain it.
    assert venue.depth.max_size_base_units is not None
    assert venue.depth.max_size_base_units < MIN_POSITION
    assert not sizing_band(RH, tmp_db, token=TOKEN).contains(MIN_POSITION)


def test_a_refusal_carries_no_rates_so_the_ladder_cannot_walk_past_it(
    tmp_db, rpc, gmgn, risk_yaml
):
    """A quote refusal must leave the venue UNPRICED, not merely depthless.

    ``resolve_depth``'s ladder walks from the curve to the dossier to the pair, and a
    ``VenueRead`` that carried rates with no depth would let a token that IS on a curve be
    priced off somebody's pool -- the mistake ``FLAP_PROBE_UNAVAILABLE`` exists to prevent
    one venue over. Refusing on the rates is what stops it here.
    """
    put_native(tmp_db)
    register(tmp_db)
    gmgn["payload"] = info_payload(price=None)

    venue = read_venue(RH, TOKEN, tmp_db)
    assert venue.fee_bps_per_leg is None
    assert venue.tax_bps_per_leg is None
    assert not venue.priced
    # Belt and braces: the note must not read as "this token left the curve", which is the
    # one class of note `read_venue` is allowed to fall back to a DEX reader on.
    assert not viability._left_the_curve(venue.note), venue.note
    assert isinstance(resolve_depth(RH, TOKEN, tmp_db), NoDepth)


def test_a_dearer_quote_asset_buys_more_depth(tmp_db, rpc, gmgn, risk_yaml):
    """Monotonicity: the rate has to point the right way round.

    A reserve of N units of a dearer asset is a deeper pool, so the band's optimal size
    goes UP with the quote asset's price. Getting the ratio upside down still produces
    numbers, and this is the test that notices.
    """
    put_native(tmp_db)
    register(tmp_db)
    rpc["results"] = stock_curve()
    cheap = sizing_band(RH, tmp_db, token=TOKEN)

    reset_venue_cache()
    reset_quote_asset_cache()
    gmgn["payload"] = info_payload(price=str(Decimal(GME_PRICE_USD) * 10))
    dear = sizing_band(RH, tmp_db, token=TOKEN)

    assert cheap.optimal_base_units is not None and dear.optimal_base_units is not None
    assert dear.optimal_base_units > cheap.optimal_base_units


# ------------------------------------------------------------------------------- caching


def test_the_rate_is_cached_per_quote_asset_not_per_token(tmp_db, rpc, gmgn, risk_yaml):
    """59 quote assets cover 2,538 tokens. Per token that is 2,538 calls for 59 answers."""
    put_native(tmp_db)
    other = "0x" + "11" * 20
    register(tmp_db, token=TOKEN)
    register(tmp_db, token=other)

    read_venue(RH, TOKEN, tmp_db)
    reset_venue_cache()  # the venue memo is per token; the rate memo must not be
    read_venue(RH, other, tmp_db)

    assert len(gmgn["calls"]) == 1, gmgn["calls"]
    assert gmgn["calls"][0] == (GME, RH)


def test_two_quote_assets_are_two_entries(tmp_db, rpc, gmgn, risk_yaml):
    """Caching per asset must not collapse into caching one rate for the whole chain."""
    put_native(tmp_db)
    register(tmp_db)
    read_venue(RH, TOKEN, tmp_db)

    gmgn["payload"] = info_payload(
        address=USDG, symbol="USDG", decimals=USDG_DECIMALS, price=USDG_PRICE_USD,
    )
    rate, why = quote_asset_rate(RH, USDG, tmp_db)
    assert rate is not None, why
    assert rate.decimals == USDG_DECIMALS
    assert len(gmgn["calls"]) == 2


def test_the_cache_expires(tmp_db, rpc, gmgn, risk_yaml, monkeypatch):
    """A rate reused for ever is the stale-price failure with extra steps."""
    put_native(tmp_db)
    first, _ = quote_asset_rate(RH, GME, tmp_db)
    assert first is not None

    later = now_ms() + int(viability.QUOTE_ASSET_RATE_TTL_S * 1000) + 1
    again, _ = quote_asset_rate(RH, GME, tmp_db, at_ms=later)
    assert again is not None
    assert len(gmgn["calls"]) == 2


def test_a_failed_lookup_is_not_cached_as_an_answer(tmp_db, rpc, gmgn, risk_yaml):
    """A refusal must not become a sticky one; the provider may be back next tick."""
    put_native(tmp_db)
    gmgn["payload"] = info_payload(price=None)
    assert quote_asset_rate(RH, GME, tmp_db)[0] is None

    gmgn["payload"] = info_payload()
    rate, why = quote_asset_rate(RH, GME, tmp_db)
    assert rate is not None, why


# ------------------------------------------------------------------------- housekeeping


def test_the_new_constants_declare_their_provenance():
    """The module's own rule, enforced by tests/test_viability.py -- checked here too so a
    failure points at the constant that was added rather than at a set difference."""
    for name in ("QUOTE_ASSET_RATE_TTL_S", "QUOTE_ASSET_PRICE_MAX_AGE_S",
                 "QUOTE_ASSET_MAX_DECIMALS"):
        assert name in viability.PROVENANCE, name
        row = viability.PROVENANCE[name]
        assert row.value == getattr(viability, name)
        assert row.unit and row.source and row.note


def test_the_reader_asks_at_entry_priority_and_waits_for_capacity(
    tmp_db, rpc, risk_yaml, monkeypatch
):
    """``docs/CONTRACT.md``: a call that blocks an entry must wait rather than no-op.

    The gmgn bucket is shared with the scanner, and a rate lost to 250 ms of spacing is a
    live entry lost to it.
    """
    from kaiba.core.limiter import Priority

    seen: dict = {}

    def fake(address, chain=Chain.SOL, **kwargs):
        seen.update(kwargs)
        return gmgn_cli.GmgnResult(
            info_payload(), Receipt(provider="gmgn", endpoint="token.info")
        )

    monkeypatch.setattr(gmgn_cli, "token_info", fake)
    put_native(tmp_db)
    assert quote_asset_rate(RH, GME, tmp_db)[0] is not None
    assert seen.get("priority") is Priority.ENTRY
    assert seen.get("wait_for_slot_s") == viability.VENUE_PROBE_WAIT_S
    assert seen.get("conn") is tmp_db
