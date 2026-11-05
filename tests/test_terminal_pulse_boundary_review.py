"""Independent boundaries for the lead's background producer. Offline only."""

import json

import httpx

from dashboard.ultimate_pulse import build_pulse
from kaiba.market_intel import terminal_pulse as producer


def test_one_provider_refusal_stops_other_chain_requests(tmp_path):
    calls = []

    def refuse(url):
        calls.append(url)
        request = httpx.Request("GET", url)
        response = httpx.Response(429, request=request, headers={"Retry-After": "21600"})
        raise httpx.HTTPStatusError("quota refusal", request=request, response=response)

    state = tmp_path / "dex.json"
    producer.read_chains(1_793_710_000_000, state, refuse)
    assert len(calls) == 1, "a known provider-wide 429 must stop remaining requests"
    producer.read_chains(1_793_714_000_000, state, refuse)
    assert len(calls) == 1, "the shared Retry-After survives a process restart"


def test_producer_and_dashboard_share_the_real_envelope(tmp_path, monkeypatch):
    now = 1_793_710_000_000
    address = "1" * 32
    wallet = {"chain": "sol", "address": address, "first_seen_ms": now - 1000,
              "scored_at_ms": now - 2000, "closed_trades": 12, "win_rate": 0.5,
              "realized_pnl_usd": "-20", "pnl_window": "unknown", "grade": "C"}
    coverage = {"unique_wallets_in_sample": 40, "history_rows_read": 50, "exported": 1}
    monkeypatch.setattr(producer, "read_wallets", lambda *_: (
        [wallet], coverage, {"status": "ok", "observed_ms": now}))

    def cached(_key):
        return [{"address": address, "symbol": "Fixture", "rank": 1, "volume": 500}], now - 10000

    calls = []

    def dex(url):
        calls.append(url)
        return {"total24h": 5000, "change_1d": -2, "protocols": [{"total24h": 999999999}]}

    document = producer.build(now_ms=now, db=tmp_path / "unused.db", out_dir=tmp_path,
                              http_get=dex, cache=cached)
    assert json.loads((tmp_path / "latest.json").read_text())["schema"] == 1
    result = build_pulse(path=tmp_path / "latest.json", now_ms=now)
    assert result["status"] == "available"
    assert result["coverage"]["wallet_scope"] == "40 wallets in the latest 50 grading revisions"
    assert result["wallets"][0]["realized_pnl_usd"] == -20
    assert result["wallets"][0]["found_today"]
    assert result["wallets"][0]["copy_profitability"] == "unproven"
    token = next(row for row in result["tokens"] if row["chain"] == "sol")
    assert token["reported_volume_usd"] == 500 and token["volume_window"] == "unknown"
    assert token["volume_1h_usd"] is None and token["volume_24h_usd"] is None
    source = next(row for row in result["source_health"] if row["key"] == "gmgn_trending:sol")
    assert source["fetched_at"] == now - 10000
    assert len(document["chains"]) == len(calls) == 5
    assert all(row["volume_24h_usd"] == 5000 for row in result["chains"])
