"""Truth boundaries for terminal rankings; no providers or live ledger."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from dashboard import ultimate_pulse as pulse

NOW = int(datetime(2026, 11, 2, 17, 30, tzinfo=UTC).timestamp() * 1000)  # 00:30 WIB, 3 Nov
ADDRESS = "1" * 32
EVM = "0x" + "a" * 40


def packet(**overrides):
    return {"schema_version": 1, "observed_ms": NOW, "wallets": [], "tokens": [], "chains": [], **overrides}


def read(tmp_path, value):
    path = tmp_path / "pulse.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return pulse.build_pulse(path=path, now_ms=NOW)


def wallet(**values):
    return {"chain": "sol", "address": ADDRESS, "scored_at_ms": NOW - 1000,
            "first_seen_ms": NOW - 10_000, "realized_pnl_usd": "125.5", "win_rate": 0.6,
            "closed_trades": 20, "grade": "B", "model_version": "history-model", **values}


def token(**values):
    return {"chain": "sol", "address": ADDRESS, "observed_ms": NOW - 1000, "symbol": "FIXTURE",
            "rank": 2, "market_cap_usd": "200000", "liquidity_usd": "30000", **values}


@pytest.mark.parametrize("value", [None, [], {}, {"schema_version": 2}, {"schema_version": 1, "tokens": "bad"}])
def test_bad_or_missing_clock_cannot_be_current(tmp_path, value):
    assert read(tmp_path, value)["status"] in ("unavailable", "unknown")


def test_missing_file_is_unknown_without_creating_directories(tmp_path):
    path = tmp_path / "absent" / "latest.json"
    assert pulse.build_pulse(path=path, now_ms=NOW)["status"] == "unavailable"
    assert not path.parent.exists()


def test_new_today_uses_discovery_in_jakarta_not_last_grade(tmp_path):
    old = wallet(address="2" * 32, first_seen_ms=NOW - 31 * 60_000)
    new = wallet(first_seen_ms=NOW - 10 * 60_000)
    rows = read(tmp_path, packet(wallets=[old, new]))["wallets"]
    assert {row["address"]: row["found_today"] for row in rows} == {old["address"]: False, ADDRESS: True}
    assert all(row["pnl_window"] == "Stored history · window unspecified" for row in rows)
    assert all(row["copy_profitability"] == "unproven" for row in rows)


def test_negative_source_pnl_and_measured_zero_are_retained(tmp_path):
    rows = read(tmp_path, packet(wallets=[wallet(realized_pnl_usd="-15.2"), wallet(address="2" * 32, realized_pnl_usd=0)]))["wallets"]
    assert [r["realized_pnl_usd"] for r in rows] == [-15.2, 0]
    assert rows[0]["win_rate_pct"] == 60
    assert rows[0]["url"] == f"/w/sol/{ADDRESS}"


def test_closed_zero_cannot_support_a_profitable_pnl_claim(tmp_path):
    row = read(tmp_path, packet(wallets=[wallet(closed_trades=0)]))["wallets"][0]
    assert row["realized_pnl_usd"] is None
    assert row["flags"] == ["PnL conflicts with zero recorded closes"]


def test_newer_assessment_wins_duplicate_not_larger_pnl(tmp_path):
    old = wallet(scored_at_ms=NOW - 2000, realized_pnl_usd=900000, grade="A")
    new = wallet(scored_at_ms=NOW - 1000, realized_pnl_usd=-100, grade="C")
    rows = read(tmp_path, packet(wallets=[old, new]))["wallets"]
    assert len(rows) == 1 and rows[0]["realized_pnl_usd"] == -100


def test_snapshot_collection_does_not_freshen_old_assessment(tmp_path):
    row = read(tmp_path, packet(wallets=[wallet(scored_at_ms=NOW - 2 * pulse.DAY_MS)]))["wallets"][0]
    assert row["status"] == "stale"


@pytest.mark.parametrize("bad", [NOW + 1, "not-a-time", True, None])
def test_future_or_bad_source_clock_stays_unknown(tmp_path, bad):
    row = read(tmp_path, packet(tokens=[token(observed_ms=bad)]))["tokens"][0]
    assert row["status"] == "unknown" and row["observed_ms"] is None


def test_old_snapshot_cannot_be_current_even_with_recent_rows(tmp_path):
    result = read(tmp_path, packet(observed_ms=NOW - pulse.SNAPSHOT_TTL_MS - 1, tokens=[token()]))
    assert result["status"] == result["tokens"][0]["status"] == "stale"


def test_volume_periods_never_blend_or_infer_from_trend_window(tmp_path):
    rows = read(tmp_path, packet(tokens=[
        token(volume_usd=7000, trend_window="1h"),
        token(address="2" * 32, volume_usd=5000, volume_window="1h"),
        token(address="3" * 32, volume_usd=8000, volume_window="24h"),
    ]))["tokens"]
    assert rows[0]["reported_volume_usd"] == 7000
    assert rows[0]["volume_1h_usd"] is None and rows[0]["volume_24h_usd"] is None
    assert rows[1]["volume_1h_usd"] == 5000 and rows[1]["volume_24h_usd"] is None
    assert rows[2]["volume_24h_usd"] == 8000 and rows[2]["volume_1h_usd"] is None


def test_chain_total_is_reported_field_not_sum_of_sampled_tokens(tmp_path):
    result = read(tmp_path, packet(chains=[{"chain": "Solana", "volume_24h_usd": 1234, "change_24h_pct": -3,
                                          "observed_ms": NOW}], tokens=[token(volume_24h_usd=999999)]))
    assert result["chains"][0]["volume_24h_usd"] == 1234
    assert result["chains"][0]["change_24h_pct"] == -3


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonstandard_json_numbers_refuse_document(tmp_path, value):
    result = read(tmp_path, packet(coverage={"count": value}))
    assert result["status"] == "unavailable"


def test_number_strings_booleans_negative_sizes_and_bad_addresses(tmp_path):
    result = read(tmp_path, packet(tokens=[token(liquidity_usd=-10, market_cap_usd="NaN", volume_24h_usd=True),
                                         token(address='javascript:alert(1)')], wallets=[wallet(win_rate=70)]))
    assert len(result["tokens"]) == 1
    assert result["tokens"][0]["liquidity_usd"] is None
    assert result["tokens"][0]["market_cap_usd"] is None
    assert result["tokens"][0]["volume_24h_usd"] is None
    assert result["wallets"][0]["win_rate_pct"] is None


def test_size_depth_and_row_limits(tmp_path):
    path = tmp_path / "pulse.json"
    path.write_bytes(b" " * (pulse.MAX_BYTES + 1))
    assert pulse.build_pulse(path=path, now_ms=NOW)["status"] == "unavailable"
    path.write_text("[" * 2000 + "]" * 2000)
    assert pulse.build_pulse(path=path, now_ms=NOW)["status"] == "unavailable"
    result = read(tmp_path, packet(chains=[{"chain": "sol", "observed_ms": NOW}] * 1000))
    assert len(result["chains"]) == 1


def test_reader_never_uses_database_or_network(tmp_path, monkeypatch):
    import socket
    import sqlite3

    def forbidden(*args, **kwargs):
        pytest.fail("ranking view attempted network or database work")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    assert read(tmp_path, packet(wallets=[wallet()], tokens=[token()]))["status"] == "available"


def test_partial_producer_and_missing_source_clock_cannot_be_current(tmp_path):
    result = read(tmp_path, packet(status="partial", tokens=[token()], source_health=[
        {"key": "wallets", "status": "fresh"},
        {"key": "tokens", "status": "fresh", "observed_ms": NOW - 1000},
    ]))
    assert result["status"] == "partial"
    assert result["source_health"][0]["status"] == "unknown"
    assert result["source_health"][1]["status"] == "fresh"


def test_actual_producer_envelope_preserves_original_source_clock_and_scope(tmp_path):
    document = packet(tokens=[token()], source_health={
        "gmgn_trending:sol": {"status": "ok", "fetched_ms": NOW - 10000, "observed_ms": NOW},
        "defillama:base": {"status": "missing", "fetched_ms": None, "observed_ms": NOW},
    }, coverage={"wallets": {"history_rows_read": 5000, "unique_wallets_in_sample": 700, "exported": 150}})
    del document["schema_version"]
    document["schema"] = 1
    result = read(tmp_path, document)
    assert result["status"] == "partial"
    assert result["source_health"][0]["fetched_at"] == NOW - 10000
    assert result["source_health"][1]["fetched_at"] is None
    assert result["coverage"]["wallet_scope"] == "700 wallets in the latest 5,000 grading revisions"
    assert result["coverage"]["wallets_exported"] == 150


def test_provider_cooldown_is_visible_and_keeps_old_clock(tmp_path):
    result = read(tmp_path, packet(source_health={"defillama:base": {
        "status": "cooldown", "fetched_ms": NOW - 10000, "observed_ms": NOW,
    }}))
    assert result["status"] == "partial"
    source = result["source_health"][0]
    assert source["status"] == "deferred" and source["fetched_at"] == NOW - 10000
    assert "cooldown" in source["reason"]


@pytest.mark.parametrize("malformed", [{"unexpected": "object"}, ["ok"], 1, None])
def test_malformed_source_status_stays_unknown(tmp_path, malformed):
    result = read(tmp_path, packet(source_health=[{
        "key": "unexpected", "status": malformed, "observed_ms": NOW,
    }]))
    assert result["status"] == "partial"
    assert result["source_health"][0]["status"] == "unknown"
