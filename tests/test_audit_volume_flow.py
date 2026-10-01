"""Offline volume regressions; recorded GMGN token-info fixture, temporary DB/cache only."""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from kaiba.core.schemas import Chain, EvidenceBasis, Receipt
from kaiba.intelligence import dyor
from kaiba.providers._http import cache_path, cache_write
from tests.test_dyor import SOL_MINT, http, wire_solana  # noqa: F401 - shared offline fixture
from tests.test_gmgn_cli import fast_limiter, replay_many, wsol_pair  # noqa: F401 - offline replay


FIXTURES = Path(__file__).parent / "fixtures" / "gmgn"


def info_payload():
    """The existing recorded response, not a hand-built success response."""
    recorded = json.loads((FIXTURES / "token_info.json").read_text(encoding="utf-8"))
    return json.loads(recorded["stdout"])


def info_key(address=SOL_MINT, chain=Chain.SOL):
    return f"gmgn-cli token info --address {address} --chain {chain.value} --raw"


def seed_info(body, *, address=SOL_MINT, chain=Chain.SOL, observed_ms=None):
    key = info_key(address, chain)
    cache_write("gmgn", key, body)
    path = cache_path("gmgn", key)
    record = json.loads(path.read_text(encoding="utf-8"))
    if observed_ms is not None:
        record["fetched_ms"] = observed_ms
        path.write_text(json.dumps(record), encoding="utf-8")
    return record["fetched_ms"]


def test_cached_token_info_volume_reaches_stored_dossier(tmp_db, http):
    body = info_payload()
    fetched_ms = seed_info(body)
    wire_solana(http)

    dossier = dyor.scan_token(SOL_MINT, Chain.SOL, conn=tmp_db)

    measure = dossier.volume_24h_usd
    assert measure.value == Decimal(body["price"]["volume_24h"])
    assert measure.basis is EvidenceBasis.CACHED
    assert measure.receipt.observed_at_ms == fetched_ms
    assert measure.receipt.provider == "gmgn"
    assert measure.receipt.endpoint == "token.info"
    assert "volume_24h_usd" not in dossier.unknowns
    stored = json.loads(tmp_db.execute(
        "SELECT dossier_json FROM token_dossiers WHERE chain=? AND address=?",
        ("sol", SOL_MINT),
    ).fetchone()[0])
    assert Decimal(stored["volume_24h_usd"]["value"]) == measure.value
    assert stored["volume_24h_usd"]["receipt"]["observed_at_ms"] == fetched_ms


@pytest.mark.parametrize("bad", [None, "", "unknown", True, False, [], {}, "-1", -1,
                                "NaN", "sNaN", "Infinity", "-Infinity", "1_000"])
def test_bad_cached_volume_is_unknown(bad):
    body = info_payload()
    body["price"]["volume_24h"] = bad
    seed_info(body)
    claims, _, _ = dyor.collect_cached_gmgn_volume(SOL_MINT, Chain.SOL)
    assert claims == []


@pytest.mark.parametrize("bad", [True, "-1", "NaN", "Infinity", "-Infinity", "1_000"])
def test_bad_normalized_volume_claim_is_unknown(bad):
    receipt = Receipt(provider="gmgn", endpoint="token.info")
    resolution = dyor.resolve([dyor.Claim("volume_24h_usd", "gmgn", bad, receipt)], chain=Chain.SOL)
    dossier = dyor.build_dossier(SOL_MINT, Chain.SOL, resolution)
    assert dossier.volume_24h_usd.value is None
    assert dossier.volume_24h_usd.basis is EvidenceBasis.UNAVAILABLE
    assert "volume_24h_usd" in dossier.unknowns


@pytest.mark.parametrize("zero", ["0", 0, 0.0])
def test_measured_zero_volume_remains_zero(zero):
    body = info_payload()
    body["price"]["volume_24h"] = zero
    seed_info(body)
    claims, _, _ = dyor.collect_cached_gmgn_volume(SOL_MINT, Chain.SOL)
    dossier = dyor.build_dossier(SOL_MINT, Chain.SOL, dyor.resolve(claims, chain=Chain.SOL))
    assert dossier.volume_24h_usd.known
    assert dossier.volume_24h_usd.value == Decimal(0)


@pytest.mark.parametrize("where,field,value", [
    ("body", "address", None),
    ("price", "address", None),
    ("body", "address", "OtherToken1111111111111111111111111111111111"),
    ("price", "address", "OtherToken1111111111111111111111111111111111"),
    ("body", "address", SOL_MINT.lower()),
    ("price", "address", SOL_MINT.lower()),
    ("body", "chain", "bsc"),
    ("price", "chain", "bsc"),
])
def test_wrong_or_missing_token_provenance_is_unknown(where, field, value):
    body = info_payload()
    target = body if where == "body" else body["price"]
    target[field] = value
    seed_info(body)
    claims, _, _ = dyor.collect_cached_gmgn_volume(SOL_MINT, Chain.SOL)
    assert claims == []


@pytest.mark.parametrize("offset_ms", [60_000, -16_000])
def test_future_or_expired_cache_is_unknown(offset_ms):
    seed_info(info_payload(), observed_ms=dyor.now_ms() + offset_ms)
    claims, _, _ = dyor.collect_cached_gmgn_volume(SOL_MINT, Chain.SOL)
    assert claims == []


@pytest.mark.parametrize("basis,offset_ms", [
    (EvidenceBasis.UNAVAILABLE, 0),
    (EvidenceBasis.STALE, 0),
    (EvidenceBasis.ESTIMATED, 0),
    (EvidenceBasis.DERIVED, 0),
    (EvidenceBasis.CACHED, 60_000),
    (EvidenceBasis.CACHED, -121_000),
])
def test_unusable_claim_receipt_does_not_populate_volume(basis, offset_ms):
    receipt = Receipt(provider="gmgn", endpoint="token.info", basis=basis,
                      observed_at_ms=dyor.now_ms() + offset_ms)
    claims = [dyor.Claim("volume_24h_usd", "gmgn", Decimal("12.50"), receipt)]
    dossier = dyor.build_dossier(SOL_MINT, Chain.SOL, dyor.resolve(claims, chain=Chain.SOL))
    assert dossier.volume_24h_usd.value is None
    assert "volume_24h_usd" in dossier.unknowns


def test_volume_receipt_belongs_to_the_adopted_value():
    first = Receipt(provider="a", endpoint="token.info")
    adopted = Receipt(provider="b", endpoint="token.info", observed_at_ms=dyor.now_ms() - 1000)
    claims = [dyor.Claim("volume_24h_usd", "a", Decimal(100), first),
              dyor.Claim("volume_24h_usd", "b", Decimal(90), adopted)]
    dossier = dyor.build_dossier(SOL_MINT, Chain.SOL, dyor.resolve(claims, chain=Chain.SOL))
    assert dossier.volume_24h_usd.value == Decimal(90)
    assert dossier.volume_24h_usd.receipt == adopted


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), [], {}, "not-a-time"])
def test_corrupt_cache_timestamp_is_unknown_not_a_scan_crash(bad):
    seed_info(info_payload(), observed_ms=bad)
    claims, _, _ = dyor.collect_cached_gmgn_volume(SOL_MINT, Chain.SOL)
    assert claims == []


def test_provider_error_payload_cannot_supply_volume():
    body = info_payload()
    body["code"] = 400
    seed_info(body)
    claims, _, _ = dyor.collect_cached_gmgn_volume(SOL_MINT, Chain.SOL)
    assert claims == []


def test_existing_security_info_reads_fill_volume_without_extra_fetch(tmp_db, monkeypatch, wsol_pair):
    for name in ("collect_gmgn_feed", "collect_goplus", "collect_rugcheck", "collect_dedup", "collect_bundles"):
        monkeypatch.setattr(dyor, name, lambda *args: ([], [], "n/a"))
    dossier = dyor.scan_token(SOL_MINT, Chain.SOL, conn=tmp_db)
    assert dossier.volume_24h_usd.value == Decimal(info_payload()["price"]["volume_24h"])
    assert len(wsol_pair) == 2, "volume must not issue a third paid read"
    assert "security" in wsol_pair[0]
    assert "info" in wsol_pair[1]
    from kaiba.providers import gmgn_cli
    assert dyor.GMGN_VOLUME_CACHE_TTL_S == gmgn_cli._TTL["token.info"][0]


def test_cache_identity_separates_evm_chains_and_accepts_checksummed_address():
    address = "0x" + "Ab" * 20
    body = info_payload()
    body["address"] = body["price"]["address"] = address
    seed_info(body, address=address.lower(), chain=Chain.BSC)
    correct, _, _ = dyor.collect_cached_gmgn_volume(address, Chain.BSC)
    wrong_chain, _, _ = dyor.collect_cached_gmgn_volume(address, Chain.ROBINHOOD)
    assert len(correct) == 1
    assert wrong_chain == []


@pytest.mark.parametrize("key", ["volume", "volume_usd", "volume_1h", "cumulative_volume", "liquidity", "price"])
def test_ambiguous_or_unrelated_numbers_never_fill_24h_volume(key):
    body = info_payload()
    body["price"].pop("volume_24h")
    body["price"][key] = "1000000"
    seed_info(body)
    claims, _, _ = dyor.collect_cached_gmgn_volume(SOL_MINT, Chain.SOL)
    assert claims == []


def test_cache_miss_is_unknown_without_provider_calls(tmp_db, http):
    wire_solana(http)
    dossier = dyor.scan_token(SOL_MINT, Chain.SOL, conn=tmp_db)
    assert dossier.volume_24h_usd.value is None
    assert "volume_24h_usd" in dossier.unknowns
