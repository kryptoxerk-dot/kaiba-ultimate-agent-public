"""Independent actual-boundary controls for the lead's new read-only kline route.

Owner: Codex, KLINE-READONLY-REVIEW-20261103. No real CLI or provider calls.
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from kaiba.core import limiter
from kaiba.core.limiter import Priority
from kaiba.core.schemas import EvidenceBasis
from kaiba.providers import gmgn_cli as g

START = 1_793_577_600


def args(seconds=100 * 60):
    return ["market", "kline", "--chain", "sol", "--address", "1" * 32,
            "--resolution", "1m", "--from", str(START), "--to", str(START + seconds)]


@pytest.fixture
def boundary(tmp_db, monkeypatch):
    calls = []
    monkeypatch.setattr(limiter, "get_risk", lambda: SimpleNamespace(provider_budgets={"gmgn": {
        "capacity": 60, "refill_per_s": 3, "min_interval_ms": 0, "max_inflight": 4,
        "weights": {"quote": 10, "swap": 10, "default": 1}}}))
    monkeypatch.setattr(g, "cli_argv", lambda: ["synthetic-cli-only"])

    def spawn(argv, timeout_s):
        calls.append((argv, timeout_s))
        return g._Raw(0, json.dumps({"list": []}), "")

    monkeypatch.setattr(g, "_spawn", spawn)
    return tmp_db, calls


def test_real_reservation_uses_two_despite_config_default_one_and_cannot_borrow_exit(boundary):
    conn, calls = boundary
    result = g.run_read("market.kline", args(), ttl_s=0, conn=conn)
    assert result.ok and result.data == [] and len(calls) == 1
    assert conn.execute("SELECT weight,status FROM provider_calls WHERE endpoint='market.kline'").fetchall()[0][:] == (2, "ok")
    refused = g.run_read("market.kline", args(), priority=Priority.EXIT, ttl_s=0, conn=conn)
    assert not refused.ok and len(calls) == 1


@pytest.mark.parametrize("seconds", [200 * 60 + 1, 200 * 60 + 59, 201 * 60])
def test_hard_range_bound_includes_a_partial_extra_bar(boundary, seconds):
    conn, calls = boundary
    result = g.run_read("market.kline", args(seconds), ttl_s=0, conn=conn)
    assert not result.ok and result.data is None and calls == []


@pytest.mark.parametrize("options", [{"ttl_s": float("inf")}, {"ttl_s": float("nan")},
                                    {"ttl_s": 3601}, {"timeout_s": float("inf")},
                                    {"timeout_s": float("nan")}, {"timeout_s": 0}])
def test_nonfinite_or_unbounded_cache_and_timeout_do_not_spend_a_request(boundary, options):
    conn, calls = boundary
    result = g.run_read("market.kline", args(), conn=conn, **options)
    assert not result.ok and calls == []


def test_corrupt_cached_shape_never_becomes_successful_candles(boundary, monkeypatch):
    conn, calls = boundary
    fetched = int(time.time() * 1000) - 1000
    monkeypatch.setattr(g, "cache_read", lambda *a, **kw: ({"not_candles": "bad"}, False, fetched))
    result = g.run_read("market.kline", args(), conn=conn)
    assert not result.ok or (isinstance(result.data, list) and all(isinstance(c, dict) for c in result.data))
    assert len(calls) <= 1  # a cache miss/refusal or ONE guarded fresh read is acceptable


def test_valid_cached_candles_keep_original_clock_and_make_no_request(boundary, monkeypatch):
    conn, calls = boundary
    fetched = int(time.time() * 1000) - 1000
    monkeypatch.setattr(g, "cache_read", lambda *a, **kw: ([], False, fetched))
    result = g.run_read("market.kline", args(), conn=conn)
    assert result.ok and result.data == [] and result.receipt.observed_at_ms == fetched
    assert result.receipt.basis is EvidenceBasis.CACHED and calls == []
