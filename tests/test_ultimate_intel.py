"""Offline contracts for the additive Aegis cache bridge."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from dashboard import ultimate_intel as intel

NOW = int(datetime(2026, 11, 3, 12, tzinfo=UTC).timestamp() * 1000)


def write_cache(root, key, data, **overrides):
    doc = {
        "key": key, "ok": True, "stale": False, "fetched_ts": NOW / 1000 - 10,
        "sources": ["https://example.com/public"], "data": data,
    }
    doc.update(overrides)
    (root / f"{key}.json").write_text(json.dumps(doc), encoding="utf-8")


def test_missing_bridge_does_not_impute_zero_or_create_files(tmp_path):
    result = intel.build_intelligence(cache_root=tmp_path, now_ms=NOW)
    assert result["bridge"] == {
        "status": "unavailable", "mode": "cache_only", "fresh_count": 0,
        "stale_count": 0, "missing_count": 8,
    }
    assert result["institutional_flows"] == []
    assert result["whales"] == []
    assert list(tmp_path.iterdir()) == []


def test_refreshed_envelope_does_not_freshen_old_etf_observation(tmp_path):
    write_cache(tmp_path, "etf_flows", {
        "units": "US$m", "assets": {"BTC": {
            "ok": True, "source_url": "https://farside.co.uk/btc/", "latest": {"date": "2026-10-19"},
            "rows": [{"date": "2026-10-19", "flows": {"IBIT": 12.5, "FBTC": -3.5}}],
        }},
    })
    result = intel.build_intelligence(cache_root=tmp_path, now_ms=NOW)
    blackrock = result["institutional_flows"][0]
    assert blackrock["flow_usd"] == 12_500_000
    assert blackrock["direction"] == "inflow"
    assert blackrock["status"] == "stale"
    assert blackrock["kind"] == "etf_net_flow"
    assert "BlackRock" in blackrock["name"]
    assert result["institutional_flows"][1]["flow_usd"] == -3_500_000
    assert result["institutional_flows"][1]["direction"] == "outflow"


def test_partial_current_etf_missing_fund_remains_unknown(tmp_path):
    write_cache(tmp_path, "etf_flows", {
        "units": "US$m", "assets": {"ETH": {
            "ok": True, "latest": {"date": "2026-11-02", "partial": True},
            "rows": [{"date": "2026-11-02", "flows": {"ETHA": None, "FETH": 0.0}}],
        }},
    })
    rows = intel.build_intelligence(cache_root=tmp_path, now_ms=NOW)["institutional_flows"]
    etha = next(row for row in rows if row["ticker"] == "ETHA")
    feth = next(row for row in rows if row["ticker"] == "FETH")
    assert etha["flow_usd"] is None and etha["direction"] == "unknown"
    assert etha["status"] == "unknown" and etha["partial"] is True
    assert feth["flow_usd"] == 0 and feth["direction"] == "flat"
    assert feth["status"] == "partial"


def test_wrong_etf_units_and_mismatched_latest_date_refuse_conversion(tmp_path):
    write_cache(tmp_path, "etf_flows", {
        "units": "USD", "assets": {"BTC": {
            "ok": True, "latest": {"date": "2026-11-02"},
            "rows": [{"date": "2026-11-02", "flows": {"IBIT": 99}}],
        }},
    })
    row = intel.build_intelligence(cache_root=tmp_path, now_ms=NOW)["institutional_flows"][0]
    assert row["flow_usd"] is None and row["direction"] == "unknown"


def test_file_age_and_explicit_source_failure_preserve_stale_data(tmp_path):
    write_cache(tmp_path, "news", {"items": [{"id": "one", "title": "Fed statement", "ts": NOW / 1000 - 20}]},
                fetched_ts=NOW / 1000 - 900)
    write_cache(tmp_path, "markets", {"quotes": [{"symbol": "DXY", "price": 100, "time": "2026-11-03T11:59:00Z"}]},
                ok=False, error="https://example.com?api_key=secret")
    result = intel.build_intelligence(cache_root=tmp_path, now_ms=NOW)
    assert result["news"][0]["status"] == "stale"
    assert result["markets"][0]["status"] == "stale"
    assert "secret" not in json.dumps(result)
    assert next(c for c in result["collectors"] if c["key"] == "markets")["error"]


def test_malformed_oversized_mismatched_and_future_caches_are_visible(tmp_path, monkeypatch):
    (tmp_path / "news.json").write_text("broken", encoding="utf-8")
    (tmp_path / "calendar.json").write_text("x" * 1000, encoding="utf-8")
    write_cache(tmp_path, "markets", {}, key_override="unused")
    doc = {"key": "different", "data": {}}
    (tmp_path / "markets.json").write_text(json.dumps(doc), encoding="utf-8")
    write_cache(tmp_path, "tickers", {}, fetched_ts=NOW / 1000 + 1000)
    monkeypatch.setattr(intel, "MAX_CACHE_BYTES", 500)
    health = {c["key"]: c for c in intel.build_intelligence(cache_root=tmp_path, now_ms=NOW)["collectors"]}
    assert health["news"]["status"] == "error"
    assert health["calendar"]["status"] == "error"
    assert health["markets"]["status"] == "error"
    assert health["tickers"]["status"] == "unknown"


def test_refresh_interval_from_file_cannot_make_old_data_live(tmp_path):
    write_cache(tmp_path, "news", {"items": [{"title": "Old", "ts": NOW / 1000 - 30}]},
                fetched_ts=NOW / 1000 - 3600, interval_s=99999999999)
    result = intel.build_intelligence(cache_root=tmp_path, now_ms=NOW)
    assert result["news"][0]["status"] == "stale"


def test_calendar_relevant_events_sorted_and_unsafe_urls_filtered(tmp_path):
    write_cache(tmp_path, "calendar", {"events": [
        {"title": "Core CPI", "kind": "econ", "country": "USD", "impact": "High", "ts": NOW / 1000 + 3600,
         "source": "ForexFactory", "forecast": "2.5%", "previous": "2.6%"},
        {"title": "FOMC rate decision", "kind": "fomc", "country": "USD", "ts": NOW / 1000 + 7200,
         "source": "Federal Reserve"},
        {"title": "Core PCE", "kind": "econ", "country": "USD", "ts": NOW / 1000 - 2 * 86400},
        {"title": "Retail sales", "kind": "econ", "country": "GBP", "ts": NOW / 1000 + 600},
    ]}, sources=["javascript:alert(1)", "https://user:pass@example.com/", "https://fed.gov/feed?token=secret"])
    result = intel.build_intelligence(cache_root=tmp_path, now_ms=NOW)
    assert [row["title"] for row in result["macro_events"]] == ["Core CPI", "FOMC rate decision"]
    assert result["macro_events"][0]["actual"] is None
    assert result["macro_events"][1]["source_url"] == intel.FED_URL
    assert result["collectors"][0]["source_urls"] == ["https://fed.gov/feed"]


def test_macro_observation_clock_and_partial_feed_are_independent(tmp_path):
    write_cache(tmp_path, "macro_fred", {
        "feeds": [{"name": "DFF", "ok": True}, {"name": "TGA", "ok": False}],
        "series": [
            {"id": "DFF", "name": "Fed funds", "value": 4, "date": "2026-10-19", "freq": "daily"},
            {"id": "CPIAUCSL", "name": "CPI", "value": 2.4, "date": "2026-10-01", "freq": "monthly"},
        ],
    })
    result = intel.build_intelligence(cache_root=tmp_path, now_ms=NOW)
    assert result["macro_series"][0]["status"] == "stale"
    assert result["macro_series"][1]["status"] == "partial"
    assert next(c for c in result["collectors"] if c["key"] == "macro_fred")["partial"] is True


def test_whale_transfer_never_becomes_exchange_flow_or_position_trade(tmp_path):
    write_cache(tmp_path, "btc_whale_tx", {"txs": [{
        "hash": "public-hash", "btc": 150, "ts": NOW / 1000 - 10, "url": "https://mempool.space/tx/public-hash",
        "direction": "exchange_inflow", "exchange": "BlackRock",
    }]})
    write_cache(tmp_path, "smart_money", {
        "fetch": {"positions_as_of": "2026-11-03T11:59:00Z", "failed": 2},
        "cohorts": {"smart_money": {"coins": [{"coin": "BTC", "net_usd": -500000}]}}})
    result = intel.build_intelligence(cache_root=tmp_path, now_ms=NOW)
    transfer, position = result["whales"]
    assert transfer["direction"] == "unknown" and transfer["amount_usd"] is None
    assert transfer["kind"] == "unconfirmed_transfer"
    assert position["direction"] == "short" and position["kind"] == "cohort_position"
    assert position["status"] == "partial"
    assert "new trade" in position["note"]


def test_bad_numeric_shapes_and_huge_or_nan_flows_remain_unknown(tmp_path):
    write_cache(tmp_path, "etf_flows", {
        "units": "US$m", "assets": {"BTC": {
            "ok": True, "latest": {"date": "2026-11-02"},
            "rows": [{"date": "2026-11-02", "flows": {"IBIT": float("nan"), "FBTC": 1e308, "ARKB": True}}],
        }},
    })
    rows = intel.build_intelligence(cache_root=tmp_path, now_ms=NOW)["institutional_flows"]
    assert all(row["flow_usd"] is None and row["direction"] == "unknown" for row in rows)
    json.dumps(rows, allow_nan=False)


def test_environment_root_and_no_runtime_or_network_imports(tmp_path, monkeypatch):
    monkeypatch.setenv("KAIBA_AEGIS_INTEL_DIR", str(tmp_path))
    write_cache(tmp_path, "tickers", {"tickers": [{
        "symbol": "BTCUSDT", "base": "BTC", "price": 100, "time": "2026-11-03T11:59:00Z",
    }]})
    result = intel.build_intelligence(now_ms=NOW)
    assert result["markets"][0]["price"] == 100
    assert result["markets"][0]["status"] == "fresh"
    assert set(path.name for path in tmp_path.iterdir()) == {"tickers.json"}


@pytest.mark.parametrize("filename", ["unknown.json", "networth.json", "hl_cohorts.json"])
def test_unselected_files_are_never_read(tmp_path, filename):
    (tmp_path / filename).write_text("broken", encoding="utf-8")
    result = intel.build_intelligence(cache_root=tmp_path, now_ms=NOW)
    assert result["bridge"]["status"] == "unavailable"


def test_perps_is_explicit_snapshot_only_and_missing_positions_stay_unknown(tmp_path, monkeypatch):
    monkeypatch.delenv("KAIBA_AEGIS_ENGINE_STATE_DIR", raising=False)
    assert intel.build_intelligence(cache_root=tmp_path, now_ms=NOW)["perps"]["status"] == "unavailable"
    monkeypatch.setenv("KAIBA_AEGIS_ENGINE_STATE_DIR", str(tmp_path))
    path = tmp_path / "aegis_perps_runtime_snapshot.json"
    path.write_text(json.dumps({
        "ts": NOW - 1000, "service": {"venue": "binance_usdm", "deployment_profile": "paper"},
        "risk": {"open_positions": 3}, "credentials": "NEVER_EXPOSE_THIS",
    }), encoding="utf-8")
    result = intel.build_intelligence(cache_root=tmp_path, now_ms=NOW)
    assert result["perps"]["status"] == "fresh"
    assert result["perps"]["positions_count"] == 3
    assert result["perps"]["mode"] == "paper"
    assert "NEVER_EXPOSE_THIS" not in json.dumps(result)
    result = intel.build_intelligence(cache_root=tmp_path, now_ms=NOW + 120_000)
    assert result["perps"]["status"] == "stale"
    assert result["perps"]["positions_count"] is None


@pytest.mark.parametrize("mutate", [
    {"ts": NOW + 1}, {"ts": None}, {"service": {"venue": "another_venue"}},
])
def test_perps_unknown_time_or_venue_never_looks_fresh(tmp_path, monkeypatch, mutate):
    monkeypatch.setenv("KAIBA_AEGIS_ENGINE_STATE_DIR", str(tmp_path))
    snapshot = {"ts": NOW, "service": {"venue": "binance_usdm", "deployment_profile": "paper"}, "risk": {"open_positions": 0}}
    snapshot.update(mutate)
    (tmp_path / "aegis_perps_runtime_snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")
    result = intel.build_intelligence(cache_root=tmp_path, now_ms=NOW)["perps"]
    assert result["status"] == "unknown"
    assert result["positions_count"] is None


def test_full_bounded_news_set_reaches_macro_compiler_before_display_slice(tmp_path):
    items = [{"id": f"crypto:{index}", "title": "BTC market update", "ts": NOW / 1000 - index}
             for index in range(399)]
    items.append({"id": "fed-statement", "title": "FOMC rate decision", "ts": NOW / 1000 - 400})
    write_cache(tmp_path, "news", {"items": items})
    result = intel.build_intelligence(cache_root=tmp_path, now_ms=NOW)
    assert len(result["news"]) == 400
    assert result["news"][-1]["id"] == "fed-statement"


def test_dense_calendar_preserves_next_fomc_within_90_day_bound(tmp_path):
    events = [{"title": f"US release {index}", "kind": "econ", "country": "USD", "impact": "High",
               "ts": NOW / 1000 + index * 60} for index in range(330)]
    events.append({"title": "FOMC rate decision", "kind": "fomc", "country": "USD",
                   "ts": NOW / 1000 + 40 * 86400, "source": "Federal Reserve"})
    write_cache(tmp_path, "calendar", {"events": events})
    result = intel.build_intelligence(cache_root=tmp_path, now_ms=NOW)
    assert len(result["macro_events"]) == 300
    assert result["macro_events"][-1]["title"] == "FOMC rate decision"
    assert result["macro_events"][-1]["source_url"] == intel.FED_URL


def test_etf_total_uses_reported_total_not_sum_of_visible_funds(tmp_path):
    write_cache(tmp_path, "etf_flows", {
        "units": "US$m", "assets": {"BTC": {
            "ok": True, "latest": {"date": "2026-11-02", "total": 999.0, "partial": True},
            "rows": [{"date": "2026-11-02", "flows": {"IBIT": 12.0, "FBTC": -3.0}}],
        }},
    })
    rows = intel.build_intelligence(cache_root=tmp_path, now_ms=NOW)["institutional_flows"]
    total = next(row for row in rows if row["asset"] == "BTC" and row["ticker"] == "TOTAL")
    assert total["flow_usd"] == 999_000_000
    assert total["partial"] is True and total["status"] == "partial"
    assert total["name"] == "All BTC spot ETFs"


def test_missing_reported_etf_total_stays_unknown_despite_known_fund(tmp_path):
    write_cache(tmp_path, "etf_flows", {
        "units": "US$m", "assets": {"BTC": {
            "ok": True, "latest": {"date": "2026-11-02"},
            "rows": [{"date": "2026-11-02", "flows": {"IBIT": 12.0}}],
        }},
    })
    rows = intel.build_intelligence(cache_root=tmp_path, now_ms=NOW)["institutional_flows"]
    total = next(row for row in rows if row["asset"] == "BTC" and row["ticker"] == "TOTAL")
    assert total["flow_usd"] is None and total["direction"] == "unknown"
    assert total["status"] == "unknown"


def test_kaiba_local_collector_cache_precedes_sibling_fallback(tmp_path, monkeypatch):
    kaiba, aegis = tmp_path / "kaiba-intel", tmp_path / "aegis-intel"
    kaiba.mkdir()
    aegis.mkdir()
    monkeypatch.delenv("KAIBA_AEGIS_INTEL_DIR", raising=False)
    monkeypatch.setattr(intel, "KAIBA_CACHE_ROOT", kaiba)
    monkeypatch.setattr(intel, "AEGIS_CACHE_ROOT", aegis)
    write_cache(kaiba, "tickers", {"tickers": [{"symbol": "BTCUSDT", "price": 1}]})
    write_cache(aegis, "tickers", {"tickers": [{"symbol": "BTCUSDT", "price": 2}]})
    assert intel.build_intelligence(now_ms=NOW)["markets"][0]["price"] == 1
    monkeypatch.setenv("KAIBA_AEGIS_INTEL_DIR", str(aegis))
    assert intel.build_intelligence(now_ms=NOW)["markets"][0]["price"] == 2
    assert intel.build_intelligence(cache_root=kaiba, now_ms=NOW)["markets"][0]["price"] == 1


def test_sibling_cache_fallback_when_kaiba_collector_directory_absent(tmp_path, monkeypatch):
    kaiba, aegis = tmp_path / "kaiba-intel", tmp_path / "aegis-intel"
    aegis.mkdir()
    monkeypatch.delenv("KAIBA_AEGIS_INTEL_DIR", raising=False)
    monkeypatch.setattr(intel, "KAIBA_CACHE_ROOT", kaiba)
    monkeypatch.setattr(intel, "AEGIS_CACHE_ROOT", aegis)
    write_cache(aegis, "tickers", {"tickers": [{"symbol": "BTCUSDT", "price": 2}]})
    assert intel.build_intelligence(now_ms=NOW)["markets"][0]["price"] == 2
    assert not kaiba.exists()
