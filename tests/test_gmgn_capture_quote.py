"""Exact original GMGN capsule tax view; real quote adapter, no provider or SQL."""
# Root completion: genuine codec + existing memory-only paper journal seam.
from __future__ import annotations

import copy
import hashlib
import inspect
import json
import socket
import sqlite3
import subprocess
import sys
import types
from decimal import Decimal

import pytest

from kaiba.core.schemas import digest
from kaiba.learning import autolearn_io as io
from kaiba.learning import gmgn_capture_quote as capture_quote
from kaiba.learning import strategy_campaign as sc
from kaiba.learning import tax_quote

_MEMORY_CONNECT = sqlite3.connect


@pytest.fixture(autouse=True)
def offline_and_unchanged_model(monkeypatch):
    before = sc.source_versions()
    def deny(*args, **kwargs):
        pytest.fail("SQL/provider/process forbidden in capture quote tests")
    monkeypatch.setattr(sqlite3, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    monkeypatch.setattr(subprocess, "Popen", deny)
    yield
    assert sc.source_versions() == before


class MarketRows:
    def __init__(self, stamp, dossier=None):
        self.stamp, self.dossier, self.calls = stamp, dossier or {}, []

    def rows(self, sql, args):
        self.calls.append((sql, args))
        if sql.startswith("SELECT decimals,migrated_ms FROM tokens"):
            return [{"decimals": 18, "migrated_ms": 1}]
        if sql == tax_quote.DOSSIER_SQL:
            return [{"built_at_ms": self.stamp, "dossier_json": json.dumps(self.dossier)}]
        if sql.startswith("SELECT price_usd,ts_ms,source FROM native_prices"):
            return [{"price_usd": "2000", "ts_ms": self.stamp - 1000, "source": "fixture_existing_native"}]
        raise AssertionError("unexpected source query")


def market(tmp_path, *, tax=None):
    chain, token, stamp = "robinhood", "0x" + "a" * 40, 1_000_000
    data = {"chain": chain, "address": token, "decimals": 18,
            "price": {"address": token, "price": "1"}, "liquidity": "1000000"}
    if tax is not None:
        data.update(tax)
    raw = json.dumps({"fetched_ms": stamp, "data": data}, sort_keys=True).encode()
    path = tmp_path / (digest(f"gmgn-cli token info --address {token} --chain {chain} --raw")[:32] + ".json")
    path.write_bytes(raw)
    reader = MarketRows(stamp)
    quote = tax_quote.quote_evidence(reader, chain, token, now_ms=stamp + 100, max_age_ms=60_000, cache_root=tmp_path)
    assert quote["basis"] == "captured_gmgn_pool_depth"
    assert quote["diagnostics"]["cached_info"]["sha256"] == hashlib.sha256(raw).hexdigest()
    return {"quote": quote, "data": data, "raw": raw, "reader": reader, "chain": chain,
            "token": token, "stamp": stamp, "now": stamp + 100, "cache_root": tmp_path}


@pytest.mark.parametrize("value,expected", [("0", 0), (0, 0), ("0.01", 100), ("0.0001", 1),
    ("0.00015", None), (None, None), (False, None), ("NaN", None), ("-0.01", None),
    ("1", None), ("bad", None), ("", None)])
def test_existing_pure_normalizer_explicit_tax_only_no_rounding_or_missing_zero(value, expected):
    assert capture_quote._tax({"buy_tax": value}, "robinhood", "buy") == expected
    assert capture_quote._tax({"buy_tax": value}, "robinhood", "sell") is None


def test_wrapper_without_loader_is_actual_original_quote_path(tmp_path):
    value = market(tmp_path)
    reader = MarketRows(value["stamp"])
    result = capture_quote.quote_evidence(reader, value["chain"], value["token"], now_ms=value["now"],
        max_age_ms=60_000, cache_root=value["cache_root"])
    assert result == value["quote"]
    assert reader.calls == value["reader"].calls


def test_unavailable_market_never_loads_capsule(tmp_path):
    calls = []
    value = market(tmp_path)
    result = capture_quote.quote_evidence(MarketRows(value["stamp"]), value["chain"], value["token"],
        now_ms=value["stamp"] + 60_001, max_age_ms=60_000, cache_root=tmp_path,
        capsule_reference={"sha256": "1" * 64, "bytes": 1}, capsule_loader=lambda ref: calls.append(ref))
    assert result["unavailable_reason"] == "pool_price_or_depth_missing_or_stale" and not calls


def test_market_reader_failure_propagates_once_without_loading_or_retry(tmp_path):
    value, calls = market(tmp_path), []
    error = io.SourceDeferred("wal_over_limit:fixture")
    class Reader:
        def rows(self, sql, args):
            calls.append((sql, args))
            raise error
    with pytest.raises(io.SourceDeferred) as observed:
        capture_quote.quote_evidence(Reader(), value["chain"], value["token"], now_ms=value["now"],
            max_age_ms=60_000, cache_root=tmp_path, capsule_reference=object(),
            capsule_loader=lambda ref: pytest.fail("capsule load after source failure"))
    assert observed.value is error and len(calls) == 1


def test_loader_failure_propagates_once_after_unchanged_original_reads(tmp_path):
    value, calls = market(tmp_path), []
    reader, error = MarketRows(value["stamp"]), io.SourceDeferred("source_retry_backoff")
    reference = {"sha256": "1" * 64, "bytes": 1}
    def fail(ref):
        calls.append(ref)
        raise error
    with pytest.raises(io.SourceDeferred) as observed:
        capture_quote.quote_evidence(reader, value["chain"], value["token"], now_ms=value["now"],
            max_age_ms=60_000, cache_root=tmp_path, capsule_reference=reference, capsule_loader=fail)
    assert observed.value is error and calls == [reference] and reader.calls == value["reader"].calls


def unchanged_market(quote):
    return {key: value for key, value in quote.items()
            if key not in {"buy_tax_bps", "sell_tax_bps", "diagnostics", "quote_ts_ms", "source"}}


def assert_no_model_or_fee_change(before, after):
    assert unchanged_market(after) == unchanged_market(before)
    assert before["quote_ts_ms"] <= after["quote_ts_ms"] <= max(
        before["quote_ts_ms"], after["diagnostics"]["gmgn_original_capture"]["receipt_observed_ms"])
    assert not any(key in after for key in ("venue_fee_bps", "venue_fee_source", "venue_fee_ts_ms", "cost_complete"))
    assert after["diagnostics"]["gmgn_original_capture"]["wire_verified"] is False
    assert after["diagnostics"]["gmgn_original_capture"]["venue_fee_inferred"] is False


def test_unvalidated_capsule_noop_preserves_input_and_unknown_tax(tmp_path):
    value = market(tmp_path)
    before = copy.deepcopy(value["quote"])
    result = capture_quote.augment(value["quote"], b"not JSON", value["chain"], value["token"], value["now"])
    assert result is value["quote"] and result == before
    assert result["buy_tax_bps"] is result["sell_tax_bps"] is None


def test_equal_existing_tax_number_comparison_is_exact():
    assert capture_quote._number(Decimal("100.00")) == 100
    assert capture_quote._number(False) is None


@pytest.fixture
def codec_boundary(monkeypatch):
    """Explicit detached-decoder double; real capsule seal/callsite tests belong to codec owner."""
    module = types.ModuleType("kaiba.learning.gmgn_observations")
    module.validate_capsule = lambda raw: json.loads(raw)
    monkeypatch.setitem(sys.modules, module.__name__, module)


def capsule_for(value, *, basis="provider_reported"):
    receipt_clock = value["stamp"] + (5 if basis == "provider_reported" else 0)
    data_sha = digest(value["data"])
    return {"target": {"chain": value["chain"], "token": value["token"]}, "data": value["data"],
        "receipt": {"provider": "gmgn", "endpoint": "token.info", "basis": basis,
            "observed_at_ms": receipt_clock,
            "request_digest": digest(f"gmgn-cli token info --address {value['token']} --chain {value['chain']} --raw"),
            "response_digest": data_sha},
        "cache": {"fetched_ms": value["stamp"], "cache_sha256": hashlib.sha256(value["raw"]).hexdigest(),
                  "data_sha256": data_sha}}


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


@pytest.mark.parametrize("basis", ["provider_reported", "cached"])
def test_matching_capsule_adds_explicit_tax_and_preserves_original_market_and_receipt_clock(tmp_path, codec_boundary, basis):
    value = market(tmp_path, tax={"buy_tax": "0.01", "sell_tax": "0"})
    capsule, before = capsule_for(value, basis=basis), copy.deepcopy(value["quote"])
    raw = encoded(capsule)
    result = capture_quote.augment(value["quote"], raw, value["chain"], value["token"], value["now"])
    assert (result["buy_tax_bps"], result["sell_tax_bps"]) == (100, 0)
    assert value["quote"] == before and result is not value["quote"]
    assert_no_model_or_fee_change(before, result)
    evidence = result["diagnostics"]["gmgn_original_capture"]
    assert evidence["basis"] == basis and evidence["receipt_observed_ms"] == capsule["receipt"]["observed_at_ms"]
    assert evidence["cache_fetched_ms"] == result["price_ts_ms"] == value["stamp"]
    assert evidence["capsule_sha256"] == hashlib.sha256(raw).hexdigest()
    assert evidence["origin"] == "trusted_local_original_capture"


def test_later_original_availability_never_backdates_price_depth_or_quote(tmp_path, codec_boundary):
    value = market(tmp_path, tax={"buy_tax": "0.01"})
    value["quote"]["quote_ts_ms"] += 10
    result = capture_quote.augment(value["quote"], encoded(capsule_for(value)), value["chain"], value["token"], value["now"])
    assert result["buy_tax_bps"] == 100 and result["sell_tax_bps"] is None
    assert result["quote_ts_ms"] == value["stamp"] + 10
    assert result["price_ts_ms"] == result["depth_ts_ms"] == value["stamp"]


@pytest.mark.parametrize("change", ["target", "cache_hash", "data_hash", "receipt_digest", "request_digest",
    "selected_clock", "price_clock", "depth_clock", "receipt_future", "receipt_earlier", "market_price",
    "market_depth", "metadata_decimals", "stale", "basis", "non_gmgn_market"])
def test_invalid_binding_is_exact_noop_not_a_new_tax_source(tmp_path, codec_boundary, change):
    value = market(tmp_path, tax={"buy_tax": "0.01", "sell_tax": "0"})
    capsule = capsule_for(value)
    if change == "target":
        capsule["target"]["token"] = "0x" + "b" * 40
    elif change == "cache_hash":
        value["quote"]["diagnostics"]["cached_info"]["sha256"] = "f" * 64
    elif change == "data_hash":
        capsule["cache"]["data_sha256"] = "f" * 64
    elif change in {"receipt_digest", "request_digest"}:
        capsule["receipt"]["response_digest" if change == "receipt_digest" else "request_digest"] = "f" * 64
    elif change == "selected_clock":
        value["quote"]["diagnostics"]["cached_info"]["fetched_ms"] += 1
    elif change in {"price_clock", "depth_clock"}:
        value["quote"]["price_ts_ms" if change == "price_clock" else "depth_ts_ms"] += 1
    elif change in {"receipt_future", "receipt_earlier"}:
        capsule["receipt"]["observed_at_ms"] = value["now"] + 1 if change == "receipt_future" else value["stamp"] - 1
    elif change in {"market_price", "market_depth"}:
        value["quote"]["price_usd" if change == "market_price" else "liquidity_usd"] = "999"
    elif change == "metadata_decimals":
        value["quote"]["decimals"] = 9
    elif change == "stale":
        value["now"] = value["stamp"] + 60_001
    elif change == "basis":
        capsule["receipt"]["basis"] = "estimated"
    else:
        value["quote"]["basis"] = "captured_pool_depth"
    before = copy.deepcopy(value["quote"])
    result = capture_quote.augment(value["quote"], encoded(capsule), value["chain"], value["token"], value["now"])
    assert result is value["quote"] and result == before


def test_conflicting_valid_dossier_tax_is_unknown_but_matching_side_preserved(tmp_path, codec_boundary):
    value = market(tmp_path, tax={"buy_tax": "0.01", "sell_tax": "0"})
    value["quote"].update(buy_tax_bps=50, sell_tax_bps=0)
    before = copy.deepcopy(value["quote"])
    result = capture_quote.augment(value["quote"], encoded(capsule_for(value)), value["chain"], value["token"], value["now"])
    assert result["buy_tax_bps"] is None and result["sell_tax_bps"] == 0
    assert result["diagnostics"]["gmgn_original_capture"]["tax_components"] == {
        "buy": "conflict_tax_unknown", "sell": "agrees_with_existing_tax"}
    assert value["quote"] == before
    assert_no_model_or_fee_change(before, result)


@pytest.mark.parametrize("tax", [{}, {"buy_tax": "0.00015"}, {"buy_tax": False}, {"buy_tax": "1"}])
def test_missing_nonintegral_boolean_or_full_tax_does_not_replace_valid_existing_tax(tmp_path, codec_boundary, tax):
    value = market(tmp_path, tax=tax)
    value["quote"]["buy_tax_bps"] = 50
    result = capture_quote.augment(value["quote"], encoded(capsule_for(value)), value["chain"], value["token"], value["now"])
    assert result["buy_tax_bps"] == 50 and result["sell_tax_bps"] is None
    assert_no_model_or_fee_change(value["quote"], result)


def test_wrapper_exact_loader_once_after_same_original_queries(tmp_path, codec_boundary):
    value = market(tmp_path, tax={"buy_tax": "0.01"})
    raw, calls = encoded(capsule_for(value)), []
    reference = {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
    def loader(ref):
        calls.append(ref)
        return raw
    reader = MarketRows(value["stamp"])
    result = capture_quote.quote_evidence(reader, value["chain"], value["token"], now_ms=value["now"],
        max_age_ms=60_000, cache_root=tmp_path, capsule_loader=loader, capsule_reference=reference)
    assert result["buy_tax_bps"] == 100 and calls == [reference] and reader.calls == value["reader"].calls
    assert_no_model_or_fee_change(value["quote"], result)


def test_same_cache_hash_guard_removed_is_detected_by_genuine_selected_quote_fixture(tmp_path, codec_boundary, monkeypatch):
    value = market(tmp_path, tax={"buy_tax": "0.01"})
    raw = encoded(capsule_for(value))
    value["quote"]["diagnostics"]["cached_info"]["sha256"] = "f" * 64
    assert capture_quote.augment(value["quote"], raw, value["chain"], value["token"], value["now"]) is value["quote"]
    source = inspect.getsource(capture_quote._matched)
    changed = source.replace('selected.get("sha256") == _hash(cache.get("cache_sha256"))', "True", 1)
    assert changed != source
    namespace = dict(capture_quote.__dict__)
    exec(compile(changed, "<removed-selected-cache-hash>", "exec"), namespace)
    monkeypatch.setattr(capture_quote, "_matched", namespace["_matched"])
    wrongly_accepted = capture_quote.augment(value["quote"], raw, value["chain"], value["token"], value["now"])
    assert wrongly_accepted["buy_tax_bps"] == 100


def genuine_capsule(value):
    from kaiba.learning import gmgn_observations as observations

    payload = capsule_for(value)
    target = {**payload["target"], "campaign_id": "pc_" + "1" * 24,
              "spec_hash": "2" * 64, "hypothesis_id": "ph_" + "2" * 24,
              "book": "fixture.db", "kind": "pending", "signal_ms": value["stamp"] - 31_000}
    admitted = value["stamp"] - 10
    day = str(admitted // io.DAY_MS)
    attempt = {"target": target, "admitted_ms": admitted, "status": "admitted",
               "attempt_id": digest([day, 1, value["chain"] + ":" + value["token"]])}
    return observations.build_capsule(target=target, data=payload["data"],
        receipt={**payload["receipt"], "note": None}, cache=payload["cache"],
        admission={"day": day, "ordinal": 1, "attempt": attempt},
        captured_ms=value["now"], sources=observations.source_bindings())


def test_genuine_codec_to_existing_memory_paper_fill_preserves_reference_and_clock(tmp_path):
    from kaiba.core.schemas import Chain
    from kaiba.learning.candidate_paper import CandidatePaperRunner

    value = market(tmp_path, tax={"buy_tax": "0.01", "sell_tax": "0"})
    raw = genuine_capsule(value)
    result = capture_quote.augment(value["quote"], raw, value["chain"], value["token"], value["now"])
    assert result["buy_tax_bps"] == 100 and result["sell_tax_bps"] == 0
    assert result["quote_ts_ms"] == value["stamp"] + 5
    assert result["price_ts_ms"] == result["depth_ts_ms"] == value["stamp"]
    reference = capture_quote.parse_capture_source(result["source"])
    assert reference == {"reference": {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)},
                         "receipt_observed_ms": value["stamp"] + 5, "cache_fetched_ms": value["stamp"]}
    runner = CandidatePaperRunner(tmp_path / "learning/paper", learning_root=tmp_path / "learning", max_stake_usd="30")
    snap, reason = runner._snapshot(result, value["now"])
    assert reason == "ok" and snap["price_ms"] == value["stamp"] and snap["quote_ms"] == value["stamp"] + 5
    # An explicit memory-only factory preserves the suite's denial of all ordinary
    # sqlite3.connect calls. No canonical schema, book, provider or order is needed.
    conn = _MEMORY_CONNECT(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("CREATE TABLE order_events(id INTEGER PRIMARY KEY,order_id TEXT,detail TEXT)")
        conn.execute("CREATE TABLE candidate_meta(key TEXT PRIMARY KEY,value TEXT)")
        snap["processing_ms"] = value["now"]
        runner._fill_evidence(conn, "fixture-order", snap, Chain.ROBINHOOD, "buy")
        evidence = json.loads(conn.execute("SELECT value FROM candidate_meta WHERE key='fill_evidence:fixture-order'").fetchone()[0])
        assert evidence["source"] == result["source"] and evidence["tax_bps"] == 100
        assert capture_quote.parse_capture_source(evidence["source"]) == reference
        assert evidence["price_ts_ms"] == value["stamp"] and evidence["quote_ms"] == value["stamp"] + 5
        assert evidence["venue_complete"] is False and evidence["cost_model_assumed"] is True
    finally:
        conn.close()


@pytest.mark.parametrize("component", ["address", "price", "dev", "link", "pool", "chain"])
def test_genuine_capsule_cannot_supply_tax_for_foreign_component(tmp_path, component):
    value = market(tmp_path, tax={"buy_tax": "0.01"})
    foreign = "0x" + "b" * 40
    if component == "address":
        value["data"]["address"] = foreign
    elif component == "chain":
        value["data"]["chain"] = "bsc"
    elif component == "pool":
        value["data"]["pool"] = {"base_address": foreign, "quote_address": "0x" + "c" * 40}
    elif component == "price":
        value["data"]["price"]["address"] = foreign
    else:
        value["data"][component] = {"address": foreign}
    raw = genuine_capsule(value)
    assert capture_quote.augment(value["quote"], raw, value["chain"], value["token"], value["now"]) is value["quote"]


@pytest.mark.parametrize("source", [None, "", "gmgn_original_capture/1:" + "a" * 64 + ":0:10:10",
    "gmgn_original_capture/1:" + "a" * 64 + ":10:9:10", "gmgn_original_capture/1:" + "a" * 64 + ":131073:10:10"])
def test_fill_source_parser_refuses_unbounded_or_ambiguous_reference(source):
    with pytest.raises(ValueError):
        capture_quote.parse_capture_source(source)
