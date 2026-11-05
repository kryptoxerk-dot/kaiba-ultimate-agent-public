"""Independent zero-network cache/receipt boundary witnesses, owned by Codex.

The actual shared request_json/cache/Measure paths run; only transport, limiter
reservation and disposable storage/clock are fixtures. No canonical DB/provider.
"""
from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from decimal import Decimal

import httpx
import pytest

from kaiba.core import schemas
from kaiba.core.schemas import EvidenceBasis, Measure, Receipt
from kaiba.providers import _http

AT = 1_793_727_000_000


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    calls, slots, errors = [], [], []
    monkeypatch.setattr(_http, "now_ms", lambda: AT)
    monkeypatch.setattr(schemas, "now_ms", lambda: AT)
    monkeypatch.setattr(_http, "cache_path", lambda provider, key:
                        tmp_path / (hashlib.sha256((provider+"\0"+key).encode()).hexdigest()+".json"))

    @contextmanager
    def guarded(*args, **kwargs):
        slots.append(args)
        yield

    monkeypatch.setattr(_http, "guarded", guarded)
    monkeypatch.setattr(_http, "_report_error", lambda *args, **kwargs: errors.append(args))

    def transport(method, url, **kwargs):
        calls.append((method, kwargs))
        if kwargs["headers"] == {"Fixture-Fail": "yes"}:
            raise httpx.ReadTimeout("fixture outage")
        body = kwargs["json"]
        payload = {"body": body, "result": body.get("params", [None])[0] if isinstance(body, dict) else None}
        return httpx.Response(200, json=payload, request=httpx.Request(method, url))

    monkeypatch.setattr(httpx, "request", transport)
    return {"calls": calls, "slots": slots, "errors": errors}


@pytest.mark.parametrize("ttl,grace", [(10,0), (0,60), (10,60)])
def test_future_cache_is_not_fresh_or_grace(isolated, ttl, grace):
    path = _http.cache_path("fixture", "clock")
    path.write_text(json.dumps({"fetched_ms": AT+1, "data": {"price": "9"}}))
    assert _http.cache_read("fixture", "clock", ttl, grace) is None
    # A real same-time/past fetch is still usable; rejecting every cache is not a fix.
    path.write_text(json.dumps({"fetched_ms": AT-1000, "data": {"price": "9"}}))
    valid = _http.cache_read("fixture", "clock", ttl, grace)
    assert valid == ({"price": "9"}, ttl == 0, AT-1000)
    assert not isolated["calls"] and not isolated["slots"]


def test_distinct_post_rpc_params_do_not_share_default_cache_or_receipt_identity(isolated):
    a = _http.post_json("fixture", "rpc.read", "https://unit.invalid/rpc",
                        json_body={"method": "getAccountInfo", "params": ["alpha"]}, ttl_s=60)
    b = _http.post_json("fixture", "rpc.read", "https://unit.invalid/rpc",
                        json_body={"method": "getAccountInfo", "params": ["beta"]}, ttl_s=60)
    a2 = _http.post_json("fixture", "rpc.read", "https://unit.invalid/rpc",
                         json_body={"params": ["alpha"], "method": "getAccountInfo"}, ttl_s=60)
    assert a.data["result"] == "alpha" and b.data["result"] == "beta"
    assert len(isolated["calls"]) == len(isolated["slots"]) == 2
    assert a.receipt.request_digest != b.receipt.request_digest
    assert a2.receipt.request_digest == a.receipt.request_digest
    assert a2.data == a.data and a2.receipt.basis is EvidenceBasis.CACHED
    assert a2.receipt.observed_at_ms == AT


def test_none_empty_object_and_empty_array_post_bodies_are_distinct(isolated):
    bodies = [None, {}, []]
    got = [_http.post_json("fixture", "rpc.read", "https://unit.invalid/rpc", json_body=b, ttl_s=60)
           for b in bodies]
    assert [g.data["body"] for g in got] == bodies
    assert len(isolated["calls"]) == 3 and len({g.receipt.request_digest for g in got}) == 3


def test_default_post_preserves_case_and_ordered_rpc_arrays(isolated):
    bodies = [{"params": ["Alpha", "beta"]}, {"params": ["alpha", "beta"]},
              {"params": ["beta", "Alpha"]}]
    got = [_http.post_json("fixture", "rpc.read", "https://unit.invalid/rpc",
                           json_body=b, ttl_s=60) for b in bodies]
    assert [g.data["body"] for g in got] == bodies
    assert len(isolated["calls"]) == 3 and len({g.receipt.request_digest for g in got}) == 3
    repeat = _http.post_json("fixture", "rpc.read", "https://unit.invalid/rpc",
                             json_body=bodies[0], ttl_s=60)
    assert repeat.data == got[0].data and repeat.receipt.basis is EvidenceBasis.CACHED
    assert len(isolated["calls"]) == 3


def test_default_post_binds_endpoint_and_rpc_read_options(isolated):
    got, expected = [], []
    for url in ("https://unit.invalid/mainnet", "https://unit.invalid/testnet"):
        for commitment in ("processed", "finalized"):
            body = {"method": "getAccountInfo", "params": ["alpha", {
                "commitment": commitment, "minContextSlot": 123,
                "encoding": "base64", "dataSlice": {"offset": 0, "length": 8}}]}
            expected.append(body)
            got.append(_http.post_json("fixture", "rpc.read", url, json_body=body, ttl_s=60))
    assert [g.data["body"] for g in got] == expected
    assert len(isolated["calls"]) == len({g.receipt.request_digest for g in got}) == 4


def test_future_cache_not_resurrected_by_provider_outage_stale_fallback(isolated):
    key = "GET:https://unit.invalid/data:{}"
    path = _http.cache_path("fixture", key)
    path.write_text(json.dumps({"fetched_ms": AT+1, "data": {"price": "9"}}))
    got = _http.get_json("fixture", "rpc.read", "https://unit.invalid/data", ttl_s=0,
                         stale_grace_s=60, headers={"Fixture-Fail": "yes"})
    assert got.data is None and got.receipt.basis is EvidenceBasis.UNAVAILABLE
    path.write_text(json.dumps({"fetched_ms": AT-1000, "data": {"price": "9"}}))
    past = _http.get_json("fixture", "rpc.read", "https://unit.invalid/data", ttl_s=0,
                          stale_grace_s=60, headers={"Fixture-Fail": "yes"})
    assert past.data == {"price": "9"} and past.receipt.basis is EvidenceBasis.STALE
    assert past.receipt.observed_at_ms == AT-1000 and len(isolated["calls"]) == 2


def test_future_alchemy_cache_cannot_hide_network_denial(isolated):
    url = "https://solana-mainnet.g.alchemy.com/v2/fixture-only"
    path = _http.cache_path("fixture", "GET:"+url+":{}")
    path.write_text(json.dumps({"fetched_ms": AT+1000, "data": {"price": "9"}}))
    got = _http.get_json("fixture", "rpc.read", url, ttl_s=60)
    assert got.data is None and got.receipt.basis is EvidenceBasis.UNAVAILABLE
    assert not isolated["calls"] and not isolated["slots"]
    # Genuine fresh existing cache remains permitted under current spending policy.
    path.write_text(json.dumps({"fetched_ms": AT-1000, "data": {"price": "9"}}))
    valid = _http.get_json("fixture", "rpc.read", url, ttl_s=60)
    assert valid.data == {"price": "9"} and valid.receipt.basis is EvidenceBasis.CACHED
    assert not isolated["calls"] and not isolated["slots"]


def test_future_measure_is_stale_without_clamping_away_original_clock(isolated):
    receipt = Receipt(provider="fixture", endpoint="read", observed_at_ms=AT+1)
    measure = Measure(value=Decimal("1"), basis=EvidenceBasis.PROVIDER_REPORTED,
                      receipt=receipt, freshness_budget_s=10)
    assert measure.stale and measure.receipt.age_seconds < 0
    current = measure.model_copy(update={"receipt": receipt.model_copy(update={"observed_at_ms": AT})})
    assert current.known and not current.stale


def test_existing_past_get_cache_remains_fresh_with_original_clock(isolated):
    path = _http.cache_path("fixture", "GET:https://unit.invalid/data:{}")
    path.write_text(json.dumps({"fetched_ms": AT-1000, "data": {"price": "9"}}))
    got = _http.get_json("fixture", "rpc.read", "https://unit.invalid/data", ttl_s=60)
    assert got.data == {"price": "9"} and got.receipt.observed_at_ms == AT-1000
    assert got.receipt.basis is EvidenceBasis.CACHED and not isolated["calls"]


def test_explicit_distinct_post_keys_and_repeated_key_keep_their_current_contract(isolated):
    a = _http.post_json("fixture", "rpc.read", "https://unit.invalid/rpc", json_body={"params":["alpha"]},
                        ttl_s=60, cache_key="resource-alpha")
    b = _http.post_json("fixture", "rpc.read", "https://unit.invalid/rpc", json_body={"params":["beta"]},
                        ttl_s=60, cache_key="resource-beta")
    a2 = _http.post_json("fixture", "rpc.read", "https://unit.invalid/rpc", json_body={"params":["alpha"]},
                         ttl_s=60, cache_key="resource-alpha")
    assert a.data["result"] == "alpha" and b.data["result"] == "beta"
    assert a2.data == a.data and a2.receipt.basis is EvidenceBasis.CACHED
    assert len(isolated["calls"]) == 2


def test_zero_ttl_different_post_calls_still_dispatch(isolated):
    a = _http.post_json("fixture", "rpc.read", "https://unit.invalid/rpc", json_body={"params":["alpha"]})
    b = _http.post_json("fixture", "rpc.read", "https://unit.invalid/rpc", json_body={"params":["beta"]})
    assert a.data["result"] == "alpha" and b.data["result"] == "beta"
    assert len(isolated["calls"]) == 2
