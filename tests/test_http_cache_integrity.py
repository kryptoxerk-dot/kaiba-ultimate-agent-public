"""Lead-owned regressions for the shared HTTP cache identity and receipt clocks (2026-11-04).

Companion to Codex's independent witnesses (tests/test_http_cache_independent_review.py). These pin the
contracts the fix must keep, not only the defects it removes."""
from __future__ import annotations

import json
from decimal import Decimal

import pytest

from kaiba.core import schemas
from kaiba.core.schemas import EvidenceBasis, Measure, Receipt
from kaiba.providers import _http

AT = 1_793_727_000_000


def test_get_keeps_its_historical_key_exactly():
    # Existing GET cache files on the box must stay addressable after the fix.
    assert _http._default_key("GET", "https://x.invalid/a", {"b": 2, "a": 1}, None) == \
        'GET:https://x.invalid/a:{"a": 1, "b": 2}'
    assert _http._default_key("GET", "https://x.invalid/a", None, {"ignored": True}) == "GET:https://x.invalid/a:{}"


def test_post_binds_the_canonical_body():
    k = lambda body: _http._default_key("POST", "https://x.invalid/rpc", None, body)  # noqa: E731
    assert k({"method": "m", "params": ["alpha"]}) != k({"method": "m", "params": ["beta"]})
    assert k({"params": ["alpha"], "method": "m"}) == k({"method": "m", "params": ["alpha"]})  # order-free
    assert len({k(None), k({}), k([]), k(""), k(0)}) == 5
    assert k({"x": float("nan")}) is None  # no canonical form -> never cached
    assert k({"x": {1, 2}}) is None


def test_an_uncanonical_body_is_sent_but_never_cached(monkeypatch, tmp_path):
    writes, sent = [], []
    monkeypatch.setattr(_http, "cache_write", lambda *a: writes.append(a))
    monkeypatch.setattr(_http, "cache_path", lambda p, key: tmp_path / "never.json")

    class R:
        status_code, headers, text = 200, {}, json.dumps({"ok": True})

        def raise_for_status(self):
            return None

    import contextlib

    import httpx

    monkeypatch.setattr(_http, "guarded", lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(httpx, "request", lambda *a, **k: sent.append(k) or R())
    got = _http.post_json("fixture", "rpc.read", "https://x.invalid/rpc", json_body={"x": float("nan")}, ttl_s=60)
    assert got.data == {"ok": True} and len(sent) == 1 and writes == []


def test_cache_age_boundaries(monkeypatch, tmp_path):
    monkeypatch.setattr(_http, "now_ms", lambda: AT)
    path = tmp_path / "c.json"
    monkeypatch.setattr(_http, "cache_path", lambda p, key: path)
    for fetched, ttl, grace, want in [
        (AT, 10, 0, (False, AT)),          # written this millisecond: age 0, fresh
        (AT - 10_000, 10, 0, (False, AT - 10_000)),   # exactly at the ttl
        (AT - 10_001, 10, 60, (True, AT - 10_001)),   # into the grace: stale, original clock
        (AT + 1, 10, 60, None),            # one millisecond in the future: a miss, never stale
        (AT + 86_400_000, 0, 999_999, None),
    ]:
        path.write_text(json.dumps({"fetched_ms": fetched, "data": 1}))
        got = _http.cache_read("p", "k", ttl, grace)
        assert (None if got is None else (got[1], got[2])) == want


@pytest.mark.parametrize("offset_ms,budget,stale", [(-1, 10, False), (0, 10, False), (-10_001, 10, True),
                                                    (1, 10, True), (3_600_000, 999_999, True)])
def test_measure_stale_including_the_future(monkeypatch, offset_ms, budget, stale):
    monkeypatch.setattr(schemas, "now_ms", lambda: AT)
    r = Receipt(provider="p", endpoint="e", observed_at_ms=AT + offset_ms)
    m = Measure(value=Decimal(1), basis=EvidenceBasis.PROVIDER_REPORTED, receipt=r, freshness_budget_s=budget)
    assert m.stale is stale
    assert m.receipt.observed_at_ms == AT + offset_ms  # never clamped
