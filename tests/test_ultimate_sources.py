"""Offline ledger contract tests for the unified dashboard read side."""

import json
import sqlite3
import time

import pytest

from dashboard import ultimate_sources as sources


def _position(conn, pid, mode, *, closed=False, pnl="0", cost="1000000000", protected=0):
    now = int(time.time() * 1000)
    conn.execute(
        "INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms, closed_ms, "
        "cost_native, realized_native, protected) VALUES (?, 'sol', 'token', 'test', ?, ?, ?, ?, ?, ?)",
        (pid, mode, now - 60_000, now if closed else None, cost, pnl, protected),
    )
    conn.commit()


def test_real_fixture_reader_preserves_paper_live_and_missing_cost(tmp_db, monkeypatch):
    monkeypatch.setattr(sources, "_mode", lambda: "shadow")
    _position(tmp_db, "open-live", "live", protected=1)
    _position(tmp_db, "open-paper", "shadow", cost="unknown")
    _position(tmp_db, "closed-live", "live", closed=True, pnl="-100000000")
    _position(tmp_db, "closed-paper", "shadow", closed=True, pnl="200000000")
    before = tmp_db.total_changes
    result = sources.build_kaiba_snapshot()
    assert result["status"] == "available"
    assert result["mode"] == "shadow"
    assert result["metrics"]["open_positions"] == 2
    assert result["metrics"]["live_positions"] == 1
    assert result["metrics"]["paper_positions"] == 1
    assert result["metrics"]["protected_positions"] == 1
    by_mode = {row["mode"]: row for row in result["metrics"]["realized_today_by_chain"]}
    assert by_mode["live"]["pnl_native"] == -0.1
    assert by_mode["shadow"]["pnl_native"] == 0.2
    assert next(r for r in result["positions"] if r["id"] == "open-paper")["cost_native"] is None
    assert all(row["mark_price_usd"] is None for row in result["positions"])
    assert result["performance"]["profitability"] == "unproven"
    assert tmp_db.total_changes == before


def test_existing_confluence_and_registry_rows_are_observations_not_advice(tmp_db, monkeypatch):
    monkeypatch.setattr(sources, "_mode", lambda: "shadow")
    now = int(time.time() * 1000)
    payload = {"level": 3, "symbol": "TEST", "usd": "1250", "tiers": {"A": 1, "B": 2},
               "wallets": [{"wallet": "never-return-private-wallet-list"}]}
    tmp_db.execute(
        "INSERT INTO alpha_signals (signal_key, source, kind, subject, title, url, chain, first_seen_ms, "
        "payload_json) VALUES ('a1', 'kaiba:wallet_track', 'wallet_confluence', 'token', '3 tracked wallets', "
        "'javascript:bad', 'sol', ?, ?)", (now - 10_000, json.dumps(payload)),
    )
    tmp_db.execute(
        "INSERT INTO opportunities (opportunity_id, kind, name, chain, status, url, created_ms, updated_ms, "
        "deadline_ms) VALUES ('o1', 'nft_mint', 'NFT lead', 'sol', 'open', 'https://example.com/mint', ?, ?, ?)",
        (now - 60_000, now - 30_000, now - 1),
    )
    tmp_db.commit()
    result = sources.build_kaiba_snapshot()
    alert = result["wallet_alerts"][0]
    assert alert["amount_usd"] == 1250
    assert alert["wallet_count"] == 3
    assert alert["url"] is None
    assert alert["stale"] is False
    assert "unproven" in alert["detail"]
    assert "wallets" not in alert
    opportunity = result["opportunities"][0]
    assert opportunity["kind"] == "nft_mint"
    assert opportunity["url"] == "https://example.com/mint"
    assert opportunity["stale"] is True
    assert "verify" in opportunity["verification"].lower()


def test_missing_ledger_is_unavailable_and_money_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "_db_path", lambda: tmp_path / "missing.db")
    monkeypatch.setattr(sources, "_mode", lambda: "unknown")
    result = sources.build_kaiba_snapshot()
    assert result["status"] == "unavailable"
    assert result["metrics"]["open_positions"] is None
    assert result["metrics"]["live_closed_today"] is None
    assert result["wallet_alerts_available"] is False
    assert not (tmp_path / "missing.db").exists()


def test_empty_real_ledger_measures_zero_book_without_inventing_pnl(tmp_db, monkeypatch):
    monkeypatch.setattr(sources, "_mode", lambda: "shadow")
    result = sources.build_kaiba_snapshot()
    assert result["status"] == "available"
    assert result["metrics"]["open_positions"] == 0
    assert result["metrics"]["live_closed_today"] == 0
    assert result["metrics"]["realized_today_by_chain"] == []


def test_row_caps_refuse_partial_totals(tmp_db, monkeypatch):
    monkeypatch.setattr(sources, "_mode", lambda: "shadow")
    monkeypatch.setattr(sources, "POSITION_LIMIT", 1)
    monkeypatch.setattr(sources, "DAY_LIMIT", 1)
    _position(tmp_db, "o1", "live")
    _position(tmp_db, "o2", "live")
    _position(tmp_db, "c1", "live", closed=True, pnl="10")
    _position(tmp_db, "c2", "live", closed=True, pnl="20")
    result = sources.build_kaiba_snapshot()
    assert len(result["positions"]) == 1
    assert result["metrics"]["open_positions"] is None
    assert result["metrics"]["positions_complete"] is False
    assert result["metrics"]["day_complete"] is False
    assert result["metrics"]["realized_today_by_chain"] == []
    assert result["metrics"]["live_closed_today"] is None


def test_high_wal_defers_before_connect(tmp_path, monkeypatch):
    path = tmp_path / "ledger.db"
    path.write_bytes(b"not opened")
    wal = tmp_path / "ledger.db-wal"
    wal.write_bytes(b"exceeds tiny fixture budget")
    monkeypatch.setattr(sources, "WAL_LIMIT_BYTES", 1)
    monkeypatch.setattr(sources, "_db_path", lambda: path)
    monkeypatch.setattr(sources, "_mode", lambda: "unknown")
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("opened")))
    result = sources.build_kaiba_snapshot()
    assert result["status"] == "deferred"
    assert result["metrics"]["open_positions"] is None


def test_reader_is_read_only_and_closes_connections(tmp_path):
    path = tmp_path / "fixture.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE witness(value INTEGER)")
    conn.execute("INSERT INTO witness VALUES (7)")
    conn.commit()
    conn.close()
    assert sources._query(path, "SELECT * FROM witness") == ([{"value": 7}], None)
    rows, error = sources._query(path, "DELETE FROM witness")
    assert rows == [] and error
    assert sources._query(path, "SELECT * FROM witness") == ([{"value": 7}], None)
    path.rename(tmp_path / "closed.db")  # Windows rename fails if a reader leaks its handle.


def test_progress_deadline_interrupts_real_expensive_query(tmp_path, monkeypatch):
    path = tmp_path / "fixture.db"
    sqlite3.connect(path).close()
    monkeypatch.setattr(sources, "QUERY_SECONDS", -1)
    rows, error = sources._query(path, "WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL "
                                      "SELECT x+1 FROM n WHERE x<10000000) SELECT sum(x) FROM n")
    assert rows == [] and error


def test_invalid_nonfinite_values_and_credential_urls_stay_unknown():
    assert sources._number("nan") is None
    assert sources._native("unknown", "sol") is None
    assert sources._native("100", "unsupported") is None
    assert sources._url("https://user:secret@example.com") is None
    assert sources._url("data:text/html,bad") is None


def test_canary_routes_to_paper_counts_and_keeps_separate_native_bucket(tmp_db, monkeypatch):
    monkeypatch.setattr(sources, "_mode", lambda: "shadow")
    _position(tmp_db, "live-open", "live")
    _position(tmp_db, "canary-open", "canary")
    _position(tmp_db, "canary-closed", "canary", closed=True, pnl="500000000")
    result = sources.build_kaiba_snapshot()
    metrics = result["metrics"]
    assert metrics["live_positions"] == 1
    assert metrics["paper_positions"] == 1
    assert metrics["live_closed_today"] == 0
    assert metrics["paper_closed_today"] == 1
    assert metrics["realized_today_by_chain"] == [
        {"chain": "sol", "mode": "canary", "trades": 1, "symbol": "SOL", "pnl_native": 0.5},
    ]


def test_existing_token_and_promoted_launch_feeds_are_bounded_research_only(tmp_db, monkeypatch):
    monkeypatch.setattr(sources, "_mode", lambda: "shadow")
    now = int(time.time() * 1000)
    for feed, token, age in (("gmgn:trending", "token1", 1000), ("gmgn:smart", "token1", 2000),
                             ("gmgn:trending", "old-token", 2 * 86_400_000)):
        tmp_db.execute(
            "INSERT INTO alpha_feed_first_seen (feed, chain, token_address, first_seen_ms, "
            "first_source_ts_ms, symbol) VALUES (?, 'sol', ?, ?, ?, 'TOK')",
            (feed, token, now - age, now - age - 7000),
        )
    for verdict, token in (("promote", "new-token"), ("reject", "rejected-token")):
        tmp_db.execute(
            "INSERT INTO triage_decisions (ts_ms, chain, token, verdict, launchpad, source, event_ms) "
            "VALUES (?, 'sol', ?, ?, 'pump.fun', 'pumpportal', ?)", (now - 500, token, verdict, now - 1000),
        )
    tmp_db.commit()
    result = sources.build_kaiba_snapshot()
    tokens = [row for row in result["opportunities"] if row["kind"] == "token"]
    launches = [row for row in result["opportunities"] if row["kind"] == "new_launch"]
    assert len(tokens) == 1 and tokens[0]["token"] == "token1"
    assert tokens[0]["observed_ms"] == now - 1000
    assert tokens[0]["source_event_ms"] == now - 8000
    assert tokens[0]["status"] == "research_only"
    assert "unverified" in tokens[0]["verification"]
    assert tokens[0]["url"] == "/terminal?t=sol%3Atoken1"
    assert len(launches) == 1 and launches[0]["token"] == "new-token"
    assert launches[0]["launchpad"] == "pump.fun"
    assert launches[0]["status"] == "research_only"
    assert "not approval" in launches[0]["verification"]
    assert {row["kind"] for row in result["category_coverage"]} >= {"token", "new_launch"}


@pytest.mark.parametrize("clock", ["yesterday", "NaN", "179999999999999999999999999999999999", "future"])
def test_bad_wallet_and_registry_observation_clocks_stay_unknown(tmp_db, monkeypatch, clock):
    monkeypatch.setattr(sources, "_mode", lambda: "shadow")
    now = int(time.time() * 1000)
    raw_clock = now + 3_600_000 if clock == "future" else clock
    tmp_db.execute(
        "INSERT INTO alpha_signals (signal_key, source, kind, subject, chain, first_seen_ms, payload_json, url) "
        "VALUES ('bad-clock', 'kaiba:wallet_track', 'wallet_confluence', 'token', 'sol', ?, ?, ?)",
        (raw_clock, json.dumps({"level": 2, "usd": True}), "https://example.com/source?api_key=secret#secret"),
    )
    tmp_db.execute(
        "INSERT INTO opportunities (opportunity_id, kind, name, status, created_ms, updated_ms, last_seen_ms, "
        "url, deadline_ms) VALUES ('clock-lead', 'airdrop', 'Clock lead', 'open', ?, ?, ?, ?, ?)",
        (now - 60_000, now - 30_000, raw_clock, "https://example.com/claim?token=secret#secret", "soon"),
    )
    tmp_db.commit()
    result = sources.build_kaiba_snapshot()
    alert = result["wallet_alerts"][0]
    assert alert["observed_ms"] is None
    assert alert["observation_status"] == "unknown" and alert["stale"] is True
    assert alert["amount_usd"] is None
    assert alert["url"] == "https://example.com/source"
    lead = next(r for r in result["opportunities"] if r["id"] == "clock-lead")
    assert lead["observed_ms"] is None  # the valid older updated_ms cannot freshen a bad last_seen_ms
    assert lead["observation_status"] == "unknown" and lead["stale"] is True
    assert lead["deadline_ms"] is None
    assert lead["url"] == "https://example.com/claim"
    assert "secret" not in json.dumps(result)


def test_future_feed_launch_and_position_times_never_become_current(tmp_db, monkeypatch):
    monkeypatch.setattr(sources, "_mode", lambda: "shadow")
    now = int(time.time() * 1000)
    future = now + 3_600_000
    tmp_db.execute(
        "INSERT INTO alpha_feed_first_seen (feed, chain, token_address, first_seen_ms, first_source_ts_ms) "
        "VALUES ('feed', 'sol', 'future-token', ?, ?)", (future, future),
    )
    tmp_db.execute(
        "INSERT INTO triage_decisions (ts_ms, chain, token, verdict, event_ms) "
        "VALUES (?, 'sol', 'future-launch', 'promote', ?)", (future, future),
    )
    tmp_db.commit()
    _position(tmp_db, "future-closed", "live", closed=True, pnl="1000000000")
    tmp_db.execute("UPDATE positions SET closed_ms=? WHERE position_id='future-closed'", (future,))
    tmp_db.commit()
    result = sources.build_kaiba_snapshot()
    assert result["metrics"]["day_complete"] is False
    assert result["metrics"]["live_closed_today"] is None
    assert result["metrics"]["realized_today_by_chain"] == []
    for row in result["opportunities"]:
        if row["kind"] in ("token", "new_launch"):
            assert row["observed_ms"] is None
            assert row["source_event_ms"] is None
            assert row["observation_status"] == "unknown" and row["stale"] is True


@pytest.mark.parametrize("url", ["https://example.com/a\n", "https://example.com/\tpath", "\x00https://example.com", "https://example.com/\x7f"])
def test_source_urls_reject_control_characters(url):
    assert sources._url(url) is None


def test_valid_future_deadline_remains_allowed_but_future_observation_does_not():
    now = int(time.time() * 1000)
    future = now + 3_600_000
    assert sources._timestamp(future, now) is None
    assert sources._timestamp(future, now, allow_future=True) == future
