"""DOAI failure-class regression, not a claim about its unretained historical quote.

All persistence uses tmp_db. Transport replies are fixtures; no provider, service or
signer runs. Exercise the real quote adapters, watchdog, protection and order ledger.
"""
from __future__ import annotations

from decimal import Decimal

import json
import pytest

from kaiba.core.schemas import Chain, EvidenceBasis, Receipt, now_ms
from kaiba.execution import watchdog as wd
from kaiba.execution import evm_price as ep
from kaiba.providers import dexscreener as ds
from kaiba.providers._http import Fetched
from kaiba.providers._http import cache_path
from kaiba.providers import prices
from kaiba.core import events as ev
from kaiba.core.config import load_risk
from kaiba.core.db import fetch_one, fetch_all, jdump, jload
from kaiba.core.schemas import Lane, LaneMode, EventKind
from kaiba.execution.protection import ProtectionConfig
from tests.test_watchdog import make_position, events_named

TOKEN = "0x7218bd6c1c2038e613ce9203ffcdddde2623c12d"
POOL = "0x" + "1" * 40
CURVE = "0x2e5d9166c76e5aa5e4ee0682043868579bb3d20b"
ENTRY = Decimal("0.0000196898772")
PEAK = Decimal("0.0000373753")
BAD = Decimal("0.000004640")
HEALTHY = Decimal("0.000028100131309906810530342408826974932627272925009708")
DEPTH = Decimal("6885.18")


def dex_reply(monkeypatch, *, price=HEALTHY, liquidity=DEPTH, pool=POOL, receipt=None):
    receipt = receipt or Receipt(provider="dexscreener", endpoint="token.pairs")
    row = {
        "chainId": "robinhood", "dexId": "uniswap", "pairAddress": pool,
        "baseToken": {"address": TOKEN, "symbol": "DOAI"},
        "quoteToken": {"address": "0x" + "2" * 40, "symbol": "WETH"},
        "priceUsd": str(price),
    }
    if liquidity is not None:
        row["liquidity"] = {"usd": str(liquidity)}
    monkeypatch.setattr(ds, "get_json", lambda *a, **k: Fetched([row], receipt))
    return receipt


def test_dex_adapter_keeps_receipt_and_selected_measurement(tmp_db, monkeypatch):
    receipt = dex_reply(monkeypatch, receipt=Receipt(
        provider="dexscreener", endpoint="token.pairs", observed_at_ms=now_ms() - 4_000,
        basis=EvidenceBasis.CACHED, note="original response",
    ))
    quote = wd.ProviderPriceSource().quote(Chain.ROBINHOOD, TOKEN)
    assert quote.usable
    assert quote.observed_ms == receipt.observed_at_ms
    assert quote.basis is EvidenceBasis.CACHED
    assert "original response" in quote.note
    assert quote.chain is Chain.ROBINHOOD and quote.token == TOKEN
    assert quote.pool_id == POOL and quote.venue == "uniswap"
    assert quote.liquidity_kind == "dex_tvl_usd"


@pytest.fixture
def offline_book(tmp_db, tmp_path, monkeypatch):
    import socket
    import subprocess
    import yaml
    from kaiba.providers import gmgn_cli

    def forbidden(*a, **k):
        raise AssertionError("unmocked external transport")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(gmgn_cli, "_spawn", forbidden)
    cfg = load_risk()
    cfg.chains[Chain.ROBINHOOD].wallet = "0x" + "a" * 40
    cfg.protection["use_provider_orders"] = False
    # Risk and all flags are real, read from an isolated file; no policy/gate mocks.
    path = tmp_path / "risk.yaml"
    path.write_text(yaml.safe_dump(cfg.model_dump(mode="json")))
    monkeypatch.setenv("KAIBA_RISK_PATH", str(path))
    make_position(tmp_db, token=TOKEN, chain=Chain.ROBINHOOD, mode=LaneMode.LIVE,
                  lane=Lane.SM_TRENCHES, entry=str(ENTRY), qty=650963245966752481246972)
    tmp_db.execute("UPDATE positions SET peak_price_usd=?", (str(PEAK),))
    tmp_db.execute("INSERT INTO tokens (chain,address,decimals,first_seen_ms) VALUES (?,?,?,?)",
                   (Chain.ROBINHOOD.value, TOKEN, 18, now_ms()))
    tmp_db.execute("INSERT INTO native_prices (chain,ts_ms,price_usd,source,receipt_json) "
                   "VALUES (?,?,?,?,?)", ("eth", now_ms(), "2700", "fixture", "{}"))
    sent = []

    def send(args, *a, **k):
        sent.append(args)
        return {"data": {"order_id": "fixture-order"}}

    monkeypatch.setattr(wd.executor, "_run_gmgn", send)
    return sent


def dog(conn, source=None):
    if source is None:
        source = wd.FallbackPriceSource(wd.ProviderPriceSource(), wd.GmgnPriceSource())
    return wd.Watchdog(conn, price_source=source,
                       cfg_provider=lambda: ProtectionConfig(use_provider_orders=False))


@pytest.mark.parametrize("empty", [Decimal(0), Decimal("0.0001")])
def test_doai_new_empty_fallback_uses_credible_alternative(
    tmp_db, monkeypatch, offline_book, empty,
):
    dex_reply(monkeypatch)
    first = dog(tmp_db)
    assert first.tick().exits == 0
    baseline = fetch_one(tmp_db, "SELECT * FROM watchdog_state WHERE position_id='pos_1'")
    inventory = fetch_one(tmp_db, "SELECT qty,qty_total,cost_native FROM positions WHERE position_id='pos_1'")
    # This is the exact DOAI trigger/entry/peak/depth, with a synthetic new DEX pool.
    # Historical source/pair was not retained; this is a failure-class test only.
    dex_reply(monkeypatch, price=BAD, liquidity=empty, pool="0x" + "3" * 40)
    gmgn_cache()
    report = dog(tmp_db).tick()  # restart must retain the comparison identity
    assert report.exits == 0 and report.exit_failures == 0 and report.blind == 0
    assert offline_book == []
    state = fetch_one(tmp_db, "SELECT * FROM watchdog_state WHERE position_id='pos_1'")
    for key in ("prev_liquidity_usd", "peak_price_usd", "stop_price_usd", "tp_done_json"):
        assert state[key] == baseline[key]
    assert fetch_one(tmp_db, "SELECT qty,qty_total,cost_native FROM positions WHERE position_id='pos_1'") == inventory


@pytest.mark.parametrize("remaining,reason", [
    (Decimal(0), "rug:lp_-100.0pct"),
    (DEPTH * Decimal("0.6"), "rug:lp_-40.0pct"),
])
def test_established_pool_drain_sells_even_after_recent_migration(
    tmp_db, monkeypatch, offline_book, remaining, reason,
):
    dex_reply(monkeypatch)
    assert dog(tmp_db).tick().exits == 0
    tmp_db.execute("UPDATE tokens SET migrated_ms=?", (now_ms() - 1000,))
    dex_reply(monkeypatch, liquidity=remaining)
    restarted = dog(tmp_db)
    report = restarted.tick()
    assert report.exits == 1 and report.blind == 0 and report.errors == 0
    assert len(offline_book) == 1
    assert events_named(tmp_db, "exit_submitted")[0]["reason"] == reason
    orders = fetch_all(tmp_db, "SELECT * FROM orders WHERE side='sell'")
    assert len(orders) == 1 and orders[0]["state"] == "submitted"
    assert int(orders[0]["min_out"]) > 0
    exit_event = events_named(tmp_db, "exit_submitted")[0]
    evidence = exit_event["quote_provenance"]
    assert evidence["source"] == "prices:dexscreener" and evidence["pool_id"] == POOL
    assert evidence["liquidity_kind"] == "dex_tvl_usd"
    assert Decimal(evidence["liquidity_usd"]) == remaining
    assert evidence["observed_ms"] <= now_ms()
    assert Decimal(exit_event["previous_liquidity_usd"]) == DEPTH
    assert exit_event["previous_liquidity_provenance"]["pool_id"] == POOL
    assert exit_event["liquidity_comparable"] is True
    assert events_named(tmp_db, "quote_decision")[0]["quote_provenance"] == evidence
    restarted.tick()
    assert len(offline_book) == 1  # unresolved-order lock is still binding


def test_missing_alternative_is_monitored_blind_without_rebaselining(tmp_db, monkeypatch, offline_book):
    dex_reply(monkeypatch)
    assert dog(tmp_db).tick().exits == 0
    before = fetch_one(tmp_db, "SELECT value FROM kv WHERE key LIKE 'watchdog.quote:%'")
    dex_reply(monkeypatch, price=BAD, liquidity=0, pool="0x" + "3" * 40)
    gmgn_cache(age=20_000)  # STALE is not corroboration, even with a healthy price.
    restarted = dog(tmp_db)
    report = restarted.tick()
    assert report.blind == 1 and report.exits == report.exit_failures == report.errors == 0
    assert offline_book == []
    assert fetch_one(tmp_db, "SELECT value FROM kv WHERE key LIKE 'watchdog.quote:%'") == before
    blind = events_named(tmp_db, "protection_blind")[0]
    assert blind["stops_evaluated"] is False
    assert "uncorroborated" in blind["reason"]
    assert blind["quote_provenance"]["price_usd"] == str(BAD)
    assert blind["previous_liquidity_provenance"]["pool_id"] == POOL
    assert blind["rejected_quote"]["pool_id"] == "0x" + "3" * 40
    # Existing budget and durable request state remain operative, not a quiet HOLD.
    tmp_db.execute("UPDATE watchdog_state SET blind_since_ms=?", (now_ms() - 301_000,))
    assert restarted.tick().blind_over_budget == 1
    assert events_named(tmp_db, "protection_blind_timeout")


def test_different_pool_can_establish_baseline_without_cross_pool_rug(tmp_db, monkeypatch, offline_book):
    dex_reply(monkeypatch)
    assert dog(tmp_db).tick().exits == 0
    new_pool = "0x" + "4" * 40
    dex_reply(monkeypatch, liquidity=DEPTH / 2, pool=new_pool)
    assert dog(tmp_db).tick().exits == 0
    assert offline_book == []
    memory = jload(fetch_one(tmp_db, "SELECT value FROM kv WHERE key LIKE 'watchdog.quote:%'")["value"])
    assert memory["liquidity"]["pool_id"] == new_pool
    dex_reply(monkeypatch, liquidity=DEPTH / 4, pool=new_pool)
    assert dog(tmp_db).tick().exits == 1
    assert len(offline_book) == 1


def test_absent_liquidity_is_not_zero_and_does_not_erase_baseline(tmp_db, monkeypatch, offline_book):
    dex_reply(monkeypatch)
    assert dog(tmp_db).tick().exits == 0
    dex_reply(monkeypatch, liquidity=None)
    assert wd.ProviderPriceSource().quote(Chain.ROBINHOOD, TOKEN).liquidity_usd is None
    assert dog(tmp_db).tick().exits == 0
    state = fetch_one(tmp_db, "SELECT prev_liquidity_usd FROM watchdog_state WHERE position_id='pos_1'")
    assert Decimal(state["prev_liquidity_usd"]) == DEPTH
    dex_reply(monkeypatch, liquidity=0)
    assert wd.ProviderPriceSource().quote(Chain.ROBINHOOD, TOKEN).liquidity_usd == 0
    assert dog(tmp_db).tick().exits == 1


def test_migration_lookup_is_chain_scoped_and_uses_occurrence_time(tmp_db, offline_book):
    occurred = now_ms() - 7_200_000
    ev.emit(EventKind.TOKEN_MIGRATED, {"migrated_ms": occurred},
            chain=Chain.BSC, subject=TOKEN, conn=tmp_db)
    watch = dog(tmp_db)
    pos = wd.open_positions(tmp_db)[0]
    assert watch._migrated_ms(pos) is None
    ev.emit(EventKind.TOKEN_MIGRATED, {"migrated_ms": occurred},
            chain=Chain.ROBINHOOD, subject=TOKEN, conn=tmp_db)
    assert watch._migrated_ms(pos) == occurred  # not this event's ingestion time


@pytest.mark.parametrize("field,value", [
    ("source", "evm-venue:pons"),
    ("liquidity_kind", "curve_quote_reserve_usd"),
    ("chain", "bsc"),
    ("token", "0x" + "5" * 40),
])
def test_restart_never_compares_incompatible_baseline(tmp_db, monkeypatch, offline_book, field, value):
    dex_reply(monkeypatch)
    assert dog(tmp_db).tick().exits == 0
    row = fetch_one(tmp_db, "SELECT key,value FROM kv WHERE key LIKE 'watchdog.quote:%'")
    memory = jload(row["value"])
    memory["liquidity"][field] = value
    tmp_db.execute("UPDATE kv SET value=? WHERE key=?", (jdump(memory), row["key"]))
    dex_reply(monkeypatch, liquidity=DEPTH / 2)
    report = dog(tmp_db).tick()
    assert report.exits == 0 and report.blind == report.errors == 0
    assert offline_book == []
    updated = jload(fetch_one(tmp_db, "SELECT value FROM kv WHERE key=?", (row["key"],))["value"])
    assert updated["liquidity"]["source"] == "prices:dexscreener"
    assert updated["liquidity"]["liquidity_kind"] == "dex_tvl_usd"
    assert Decimal(updated["liquidity"]["liquidity_usd"]) == DEPTH / 2


def test_unidentified_legacy_baseline_is_unknown_not_same_pool(tmp_db, monkeypatch, offline_book):
    pos = wd.open_positions(tmp_db)[0]
    state = wd.load_state(tmp_db, pos)
    state.prev_liquidity_usd = DEPTH
    wd.save_state(tmp_db, state)
    dex_reply(monkeypatch, liquidity=DEPTH / 2)
    report = dog(tmp_db).tick()
    assert report.exits == 0 and report.blind == report.errors == 0
    assert offline_book == []
    transition = events_named(tmp_db, "quote_source_changed")[0]
    assert transition["previous_liquidity_provenance"] is None
    assert transition["liquidity_comparable"] is False


@pytest.mark.parametrize("age", [60_000, -60_000])
def test_invalid_observation_keeps_good_state_and_baseline(tmp_db, monkeypatch, offline_book, age):
    dex_reply(monkeypatch)
    source = wd.ProviderPriceSource()
    assert dog(tmp_db, source).tick().exits == 0
    previous = fetch_one(tmp_db, "SELECT value FROM kv WHERE key LIKE 'watchdog.quote:%'")
    receipt = dex_reply(monkeypatch, price=BAD, liquidity=0, receipt=Receipt(
        provider="dexscreener", endpoint="token.pairs", observed_at_ms=now_ms() - age,
    ))
    report = dog(tmp_db, source).tick()
    assert report.blind == 1 and report.exits == report.errors == 0
    assert fetch_one(tmp_db, "SELECT value FROM kv WHERE key LIKE 'watchdog.quote:%'") == previous
    assert events_named(tmp_db, "protection_blind")[0]["quote_provenance"]["observed_ms"] == receipt.observed_at_ms
    assert offline_book == []


@pytest.mark.parametrize("price,reason", [
    (BAD, "emergency_loss"), (ENTRY * Decimal("0.6"), "stop_loss"),
    (ENTRY * 2, "tp1_at_2.0x"),
])
def test_valid_established_prices_keep_emergency_hard_and_tp_rules(
    tmp_db, monkeypatch, offline_book, price, reason,
):
    dex_reply(monkeypatch)
    assert dog(tmp_db).tick().exits == 0
    dex_reply(monkeypatch, price=price)
    report = dog(tmp_db).tick()
    assert report.exits == 1 and report.errors == 0
    assert events_named(tmp_db, "exit_submitted")[0]["reason"] == reason
    assert len(offline_book) == 1


def test_new_pool_stop_with_normal_depth_still_requires_an_independent_quote(
    tmp_db, monkeypatch, offline_book,
):
    dex_reply(monkeypatch)
    assert dog(tmp_db).tick().exits == 0
    dex_reply(monkeypatch, price=BAD, pool="0x" + "3" * 40)
    gmgn_cache()
    assert dog(tmp_db).tick().exits == 0
    assert offline_book == []
    assert events_named(tmp_db, "quote_source_changed")[0]["rejected_quote"]["price_usd"] == str(BAD)


def test_curve_empty_dex_fallback_credible_price_preserves_curve_baseline(tmp_db, monkeypatch, offline_book):
    position = wd.open_positions(tmp_db)[0]
    prior = wd.PriceQuote(price_usd=HEALTHY, liquidity_usd=DEPTH,
                         basis=EvidenceBasis.DERIVED, chain=position.chain, token=TOKEN,
                         source="evm-venue:pons", pool_id=CURVE, venue="pons",
                         liquidity_kind="curve_quote_reserve_usd")
    seed = dog(tmp_db)
    state = wd.load_state(tmp_db, position)
    seed._remember_quote(position, state, prior, None)
    wd.save_state(tmp_db, state)
    ep._PONS_CURVE.clear()
    # Curve reader is temporarily unavailable, not known migrated. This is a
    # synthetic boundary response, not the unretained historical DOAI response.
    curve = ep.EvmVenuePriceSource(tmp_db, transports={Chain.ROBINHOOD: lambda calls: [None] * len(calls)},
                                   fallback=wd.ProviderPriceSource())
    routed = ep.ChainRoutedPriceSource({Chain.ROBINHOOD: curve})
    chain = wd.ChainFirstPriceSource(first={}, default=routed,
                                    rest=(routed, wd.GmgnPriceSource()))
    dex_reply(monkeypatch, price=BAD, liquidity=Decimal("0.0001"))
    gmgn_cache()
    report = dog(tmp_db, chain).tick()
    assert report.exits == report.blind == report.errors == 0
    assert offline_book == []
    saved = jload(fetch_one(tmp_db, "SELECT value FROM kv WHERE key LIKE 'watchdog.quote:%'")["value"])
    assert saved["price"]["source"] == "gmgn:token.info"
    assert saved["liquidity"] == prior.model_dump(mode="json")
    assert saved["liquidity"]["pool_id"] == CURVE


def test_confirmed_new_source_loss_exits_and_unknown_send_stays_locked(tmp_db, monkeypatch, offline_book):
    dex_reply(monkeypatch)
    assert dog(tmp_db).tick().exits == 0
    dex_reply(monkeypatch, price=BAD, pool="0x" + "3" * 40)
    gmgn_cache(price=BAD)

    def ambiguous(args, *a, **k):
        offline_book.append(args)
        raise wd.executor.ExecutionAmbiguous("fixture transport lost reply after POST")

    monkeypatch.setattr(wd.executor, "_run_gmgn", ambiguous)
    restarted = dog(tmp_db)
    report = restarted.tick()
    assert report.exits == 1 and report.errors == 0
    assert len(offline_book) == 1
    event = events_named(tmp_db, "exit_ambiguous")[0]
    assert event["quote_provenance"]["source"] == "gmgn:token.info"
    assert event["liquidity_comparable"] is False
    row = fetch_one(tmp_db, "SELECT state FROM orders WHERE side='sell'")
    assert row["state"] == "unknown"
    dog(tmp_db).tick()
    assert len(offline_book) == 1


def test_validated_fallback_does_not_drop_an_explicit_exit_request(tmp_db, monkeypatch, offline_book):
    dex_reply(monkeypatch)
    assert dog(tmp_db).tick().exits == 0
    dex_reply(monkeypatch, price=BAD, liquidity=0, pool="0x" + "3" * 40)
    gmgn_cache()
    ev.emit(EventKind.PROTECTION_TRIGGERED,
            {"source": "mcp", "position_id": "pos_1", "pct": "100", "reason": "fixture request"},
            chain=Chain.ROBINHOOD, subject=TOKEN, conn=tmp_db)
    report = dog(tmp_db).tick()
    assert report.exits == 1 and report.errors == 0
    event = events_named(tmp_db, "exit_submitted")[0]
    assert "request:" in event["reason"]
    assert event["quote_provenance"]["source"] == "gmgn:token.info"


def test_pons_zero_observation_is_not_replaced_with_now(tmp_db):
    from kaiba.ingest import robinhood as rh
    from tests.test_evm_price_source import PONS_LIVE, _pons_results

    state = rh.parse_curve_state(_pons_results(PONS_LIVE)[:-1], curve=CURVE, token=TOKEN,
                                 observed_ms=0, quote_is_native=True)
    priced, _ = ep.pons_price(state, token_decimals=18, quote_decimals=18, quote_token=None)
    assert priced is not None and priced.observed_ms == 0


@pytest.mark.parametrize("value", [None, "bad", "2026-09-23", 1.5, "Infinity", -1, "future"])
def test_migration_lookup_rejects_invalid_evidence(tmp_db, offline_book, value):
    if value == "future":
        value = now_ms() + 60_000
    ev.emit(EventKind.TOKEN_MIGRATED, {"migrated_ms": value},
            chain=Chain.ROBINHOOD, subject=TOKEN, conn=tmp_db)
    tmp_db.execute("UPDATE tokens SET migrated_ms=?", (value,))
    watch = dog(tmp_db)
    assert watch._migrated_ms(wd.open_positions(tmp_db)[0]) is None


def test_invalid_newest_migration_does_not_hide_valid_older_payload(tmp_db, offline_book):
    occurred = now_ms() - 300_000
    ev.emit(EventKind.TOKEN_MIGRATED, {"migrated_ms": occurred},
            chain=Chain.ROBINHOOD, subject=TOKEN, conn=tmp_db)
    ev.emit(EventKind.TOKEN_MIGRATED, {"migrated_ms": "bad"},
            chain=Chain.ROBINHOOD, subject=TOKEN, conn=tmp_db)
    assert dog(tmp_db)._migrated_ms(wd.open_positions(tmp_db)[0]) == occurred


@pytest.mark.parametrize("age,basis", [
    (int(prices.DEFAULT_MAX_AGE_S * 1000) + 1000, EvidenceBasis.CACHED),
    (-60_000, EvidenceBasis.PROVIDER_REPORTED),
    (0, EvidenceBasis.STALE),
])
def test_dex_invalid_time_or_basis_is_not_refreshed(tmp_db, monkeypatch, age, basis):
    receipt = dex_reply(monkeypatch, receipt=Receipt(
        provider="dexscreener", endpoint="token.pairs", observed_at_ms=now_ms() - age,
        basis=basis,
    ))
    quote = wd.ProviderPriceSource().quote(Chain.ROBINHOOD, TOKEN)
    assert not quote.usable
    assert quote.observed_ms == receipt.observed_at_ms
    assert quote.basis is basis


@pytest.mark.parametrize("field", ["price_usd", "liquidity_usd", "executable_quote_usd"])
@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_sample_never_reaches_evaluate(field, value):
    quote = wd.PriceQuote(price_usd=1, basis=EvidenceBasis.PROVIDER_REPORTED)
    # Pydantic assignment/model_copy do not revalidate; usable is the last boundary.
    setattr(quote, field, Decimal(value))
    assert not quote.usable


def gmgn_cache(*, price=HEALTHY, age=0):
    key = f"gmgn-cli token info --address {TOKEN} --chain robinhood --raw"
    path = cache_path("gmgn", key)
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = now_ms() - age
    path.write_text(json.dumps({
        "fetched_ms": stamp, "data": {"price": {"price": str(price)}},
    }))
    return stamp


@pytest.mark.parametrize("age,usable,basis", [
    (1000, True, EvidenceBasis.CACHED),
    (20_000, False, EvidenceBasis.STALE),
    (-60_000, False, EvidenceBasis.CACHED),
])
def test_gmgn_adapter_preserves_receipt_age_and_basis(tmp_db, age, usable, basis):
    stamp = gmgn_cache(age=age)
    quote = wd.GmgnPriceSource().quote(Chain.ROBINHOOD, TOKEN)
    assert quote.usable is usable
    assert quote.observed_ms == stamp
    assert quote.basis is basis


@pytest.mark.parametrize("age", [60_000, -60_000])
def test_evm_fx_adapter_cannot_refresh_invalid_receipt(tmp_db, monkeypatch, age):
    dex_reply(monkeypatch, receipt=Receipt(provider="dexscreener", endpoint="token.pairs",
                                         observed_at_ms=now_ms() - age))
    assert ep._provider_price_usd(Chain.ROBINHOOD, TOKEN) is None


def test_evm_curve_retains_pool_and_reserve_measurement(tmp_db, monkeypatch):
    from tests.test_evm_price_source import PONS_LIVE, _pons_results
    from kaiba.ingest import robinhood as rh

    ep._PONS_CURVE.clear()
    ep._DECIMALS.clear()
    # Only RPC/HTTP transports are stubbed. The reader, invariant, FX adapter and
    # quote source run unchanged against a migrated isolated database.
    def rpc(calls):
        if calls[0][1].startswith(rh.SELECTOR_GET_LAUNCHED_TOKEN):
            return ["0x" + "".join(format(v, "064x") for v in (0, int(CURVE, 16), 0, 0, 0))]
        return _pons_results(PONS_LIVE)

    ref_chain, ref_token = ep.NATIVE_USD_REFERENCE[Chain.ROBINHOOD]
    receipt = Receipt(provider="dexscreener", endpoint="token.pairs")
    monkeypatch.setattr(ds, "get_json", lambda *a, **k: Fetched([{
        "chainId": ds.slug_for_chain(ref_chain), "dexId": "uniswap", "pairAddress": POOL,
        "baseToken": {"address": ref_token}, "quoteToken": {"address": TOKEN},
        "priceUsd": "2700", "liquidity": {"usd": "1000000"},
    }], receipt))
    source = ep.EvmVenuePriceSource(tmp_db, transports={Chain.ROBINHOOD: rpc})
    before = now_ms()
    quote = source.quote(Chain.ROBINHOOD, TOKEN)
    assert quote.usable
    assert quote.pool_id == CURVE
    assert quote.chain is Chain.ROBINHOOD and quote.token == TOKEN
    assert quote.venue == "pons"
    assert quote.liquidity_kind == "curve_quote_reserve_usd"
    assert before <= quote.observed_ms <= now_ms()
    ep._PONS_CURVE.clear()
    ep._DECIMALS.clear()
