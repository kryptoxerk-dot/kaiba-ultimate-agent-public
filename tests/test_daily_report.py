"""The deterministic daily report: read-only, every section from the MCP read-outs, never silent."""

from __future__ import annotations

import json
import sqlite3

import pytest

from kaiba.core import events as ev
from kaiba.core.config import ChainBudget, LaneConfig, RiskConfig, save_risk
from kaiba.core.schemas import Chain, EventKind, Lane, LaneMode, now_ms
from kaiba.ops import daily_report

H = 3_600_000
ETH = 10**18
TOKEN = "0x" + "ab" * 20


@pytest.fixture
def seeded(tmp_db, tmp_path, monkeypatch):
    """A small but complete box: risk, schedule, positions, copy_manager runs, grades, health."""
    save_risk(RiskConfig(
        global_mode=LaneMode.LIVE,
        chains={Chain.ROBINHOOD: ChainBudget(enabled=True, bankroll_base_units=ETH,
                                             daily_loss_stop_base_units=39 * 10**15)},
        lanes={Lane.SM_TRENCHES: LaneConfig(mode=LaneMode.LIVE, chains=[Chain.ROBINHOOD])},
    ), tmp_path / "risk.yaml")
    monkeypatch.setenv("KAIBA_RISK_PATH", str(tmp_path / "risk.yaml"))
    (tmp_path / "schedule.yaml").write_text("jobs:\n  execute_planned:\n    enabled: true\n", encoding="utf-8")
    monkeypatch.setenv("KAIBA_SCHEDULE_CONFIG", str(tmp_path / "schedule.yaml"))

    t = now_ms()
    c = tmp_db
    for pid, closed, cost, realized, reason, mode in (
        ("win", t - H, ETH, ETH // 2, "trailing_stop", "live"),
        ("loss", t - 2 * H, ETH, -3 * ETH // 10, "stop_loss", "live"),
        ("older", t - 3 * 24 * H, ETH, -ETH // 10, "stale_no_volume:3609s", "live"),
        ("paper", t - H, ETH, 5 * ETH, "trailing_stop", "shadow"),
    ):
        c.execute(
            "INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms, closed_ms, cost_native, "
            "realized_native, exit_reason) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (pid, "robinhood", f"tok_{pid}", "sm-trenches", mode, closed - H, closed, str(cost),
             str(realized), reason),
        )
    c.execute("INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms, cost_native) "
              "VALUES ('pos_heldstuck1', 'robinhood', 'tok_held', 'sm-trenches', 'live', ?, ?)",
              (t - 5 * H, str(ETH // 50)))
    c.execute("INSERT INTO watchdog_state (position_id, exit_state, exit_attempts, updated_ms) "
              "VALUES ('pos_heldstuck1', 'failed', 204, ?)", (t,))
    c.execute("INSERT INTO native_prices (chain, ts_ms, price_usd, source) VALUES ('eth', ?, '2500', 'test')",
              (t - 60_000,))
    c.execute(
        "INSERT INTO ops_runs (job, started_ms, status, result_json) VALUES ('copy_manager', ?, 'ok', ?)",
        (t - 10 * 60_000, json.dumps({
            "live": False, "holdings": 3, "managed": 1, "skipped": {"thin_pool": 2},
            "decisions": [{"token": TOKEN, "symbol": "VPORT", "kind": "giveback", "fraction": "1",
                           "pnl": "-0.05", "reason": "gave back", "action": "held:daily_cap"}],
            "sells": [], "errors": []})),
    )
    c.execute("INSERT INTO ops_runs (job, started_ms, status, error) VALUES ('copy_manager', ?, 'error', ?)",
              (t - 20 * 60_000, "limiter refused (retry in 0.1s)"))
    c.execute("INSERT INTO kv (key, value, updated_ms) VALUES (?,?,?)",
              (f"copy_mgr:robinhood:{TOKEN}", json.dumps({"peak_pnl": "1.82", "started_s": 1}), t - 10 * 60_000))
    for i, (chain, grade, scored) in enumerate((("sol", "B", t - H), ("sol", "C", t - H),
                                                ("robinhood", "B", t - 50 * H))):
        c.execute("INSERT INTO wallet_scores (chain, address, score, grade, evidence_weight, archetype, "
                  "model_version, scored_at_ms) VALUES (?,?,?,?,?,?,?,?)",
                  (chain, f"w{i}", 60.0, grade, 1.0, "unknown", "t", scored))
    ev.emit(EventKind.SYSTEM, {"service": "watchdog", "event": "heartbeat", "checked": 1, "blind": 0,
                               "stranded_deferred": 0, "standing_unprotected": 0}, conn=c)
    return tmp_db, tmp_path / "kaiba.db"


def test_report_prints_every_section_from_a_read_only_connection(seeded, capsys):
    conn, path = seeded
    rows_before = conn.execute("SELECT (SELECT COUNT(*) FROM events) + (SELECT COUNT(*) FROM journal)").fetchone()[0]

    assert daily_report.main(["--db", str(path)]) == 0

    text = capsys.readouterr().out
    assert text.startswith("Kaiba daily report, ")
    assert "CONTROLS kill switch off | entries open | reduce-only off | mode live" in text
    assert "chains on: robinhood | live lanes: sm-trenches | execute_planned ON" in text
    red = next(line for line in text.splitlines() if line.startswith("RED "))
    assert "1 open position(s) with exit_attempts > 10 (max 204)" in red
    assert "24h: n 2 | wins 1 | mean +10.0% | median +10.0% | net +0.2000 ETH (+$500)" in text
    assert "7d : n 3 | wins 1" in text
    assert "7d by exit (costliest first): stop_loss 1x -0.3000" in text
    assert "TODAY robinhood:" in text
    assert "OPEN 1 live: id, age, cost, exit tries, state" in text
    assert "ldstuck1 robinhood 5.0h 0.020000 204 failed" in text  # last 8 chars of the id
    assert "WATCHDOG beat" in text
    assert "COPY MGR (dry-run) runs ok 1 | error 1 | timeout 0 | decisions on 1 token(s)" in text
    assert 'outcomes: {"held:daily_cap":1}' in text
    assert "VPORT giveback (held:daily_cap) | pnl -5.0% | peak +182.0% | 1 runs" in text
    assert "WALLETS A/B scored 24h: sol A0 B1" in text
    assert "A/B held: robinhood A0 B1, sol A0 B1" in text
    assert "DISK " in text and "JOBS with errors 24h: copy_manager 1/2" in text
    assert "(read-only, " in text
    assert len(text.splitlines()) <= 30
    # Nothing was written by the report.
    after = conn.execute("SELECT (SELECT COUNT(*) FROM events) + (SELECT COUNT(*) FROM journal)").fetchone()[0]
    assert after == rows_before


def test_the_report_connection_cannot_write(seeded):
    _, path = seeded
    conn = daily_report.open_readonly(path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO kv (key, value, updated_ms) VALUES ('x', 'y', 1)")
    finally:
        conn.close()


def test_an_unreadable_section_says_so_and_the_rest_still_prints(seeded, capsys):
    conn, path = seeded
    conn.execute("DROP TABLE wallet_scores")

    assert daily_report.main(["--db", str(path)]) == 0

    text = capsys.readouterr().out
    assert "WALLETS cannot measure: OperationalError: no such table: wallet_scores" in text
    assert "COPY MGR (dry-run)" in text  # the other sections are untouched


def test_json_mode_carries_the_same_sections(seeded, capsys):
    _, path = seeded
    assert daily_report.main(["--db", str(path), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert {"health", "ev_24h", "ev_7d", "copy", "wallets", "elapsed_s"} <= set(data)
    assert data["ev_24h"]["n"] == 2


def test_a_missing_database_is_one_line_and_exit_1(tmp_path, capsys):
    assert daily_report.main(["--db", str(tmp_path / "absent.db")]) == 1
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1 and out[0].startswith("Kaiba daily report: cannot open the database read-only")


# ---------------------------------------------------------------- HUNTERS (2026-10-02)


def test_hunters_section_reaches_the_report_without_writing_the_cursor(seeded, capsys):
    """The digest rides the report to Telegram; the report still writes nothing at all."""
    from kaiba.hunters import digest
    from tests.test_hunter_digest import seed_airdrops, seed_alpha_and_listings

    conn, path = seeded
    seed_airdrops(conn)
    seed_alpha_and_listings(conn)
    conn.commit()
    rows_before = conn.execute("SELECT (SELECT COUNT(*) FROM events) + (SELECT COUNT(*) FROM kv)").fetchone()[0]

    assert daily_report.main(["--db", str(path)]) == 0
    text = capsys.readouterr().out
    lines = text.splitlines()
    head = lines.index("HUNTERS new in 24h (leads to check yourself; Kaiba mints/claims/signs nothing)")
    section = lines[head + 1:-1]
    assert "  AIRDROPS/POINTS (5 new, top 2):" in section
    # This fixture funds robinhood only, so the robinhood radar cross-match leads; the
    # referral warning is written first so no cut can drop it, and the link has its own line.
    first = section.index("  AIRDROPS/POINTS (5 new, top 2):") + 1
    assert section[first].startswith("   1. Arcus Points [robinhood]: REFERRAL link, use the project's own "
                                     "site; radar: Arcus Perps on robinhood, $70,327/day fees")
    assert section[first + 1] == "      https://app.arcus.xyz/ref/AIRDROPSIO"
    assert "  EARLY ALPHA (2 new):" in section and "  LISTINGS (3 new, top 2):" in section
    assert all(len(line) <= daily_report.HUNTER_LINE_WIDTH for line in section)
    assert len(section) <= 1 + 4 * (1 + 2 * daily_report.HUNTER_PER_KIND)
    # Read-only: no cursor, no event, nothing.
    after = conn.execute("SELECT (SELECT COUNT(*) FROM events) + (SELECT COUNT(*) FROM kv)").fetchone()[0]
    assert after == rows_before and digest.read_cursor(conn) == {}


def test_hunters_section_skips_what_a_digest_push_already_sent(seeded, capsys):
    from kaiba.hunters import digest
    from tests.test_hunter_digest import seed_airdrops

    conn, path = seeded
    seed_airdrops(conn)
    digest.mark_sent(conn, digest.build(conn, per_kind=50))
    conn.commit()
    assert daily_report.main(["--db", str(path)]) == 0
    text = capsys.readouterr().out
    assert "AIRDROPS/POINTS" not in text and "  nothing new: nft 0/0, airdrop 0/8" in text


def test_an_unreadable_hunters_section_says_so(seeded, capsys):
    conn, path = seeded
    conn.execute("DROP TABLE opportunities")
    conn.commit()
    assert daily_report.main(["--db", str(path)]) == 0
    text = capsys.readouterr().out
    assert "  NFT MINTS: cannot measure: OperationalError: no such table: opportunities" in text
    assert "COPY MGR (dry-run)" in text  # the rest of the report is untouched


# ---------------------------------------------------------------- WALLET FUNNEL (2026-10-03)


def test_wallet_funnel_shows_each_chain_and_its_growth(seeded, capsys):
    """The owner asked for more wallets; this line is how he watches whether it grows.
    Every number comes from the proven freezes (kaiba.learning.proven.funnel_history)."""
    from kaiba.learning import proven as P
    from tests.test_proven_wallets import DAY, _cfg, _world

    conn, path = seeded
    t = now_ms()
    _world(conn, t)
    P.freeze(conn, P.build(conn, Chain.ROBINHOOD, config=_cfg(max_candidates=6), as_of_ms=t - 7 * DAY + 1))
    P.freeze(conn, P.build(conn, Chain.ROBINHOOD, config=_cfg(), as_of_ms=t))
    conn.commit()
    P.clear_cache()

    assert daily_report.main(["--db", str(path)]) == 0
    lines = capsys.readouterr().out.splitlines()
    head = lines.index("WALLET FUNNEL (proven_wallets: tape -> eligible -> tested -> copy-proven)")
    rh = lines[head + 1]
    assert rh.startswith("  robinhood ") and "| active 18 | drawn 18 | eligible 18 |" in rh
    assert "| proven 6" in rh and "(was " in rh and ": eligible " in rh
    assert lines[head + 2] == "  sol: never frozen"
    assert len(lines) <= 33


def test_an_unreadable_wallet_funnel_says_so(seeded, capsys):
    conn, path = seeded
    conn.execute("DROP TABLE wallet_cohort_freezes")
    conn.commit()
    assert daily_report.main(["--db", str(path)]) == 0
    text = capsys.readouterr().out
    assert "WALLET FUNNEL cannot measure: OperationalError: no such table: wallet_cohort_freezes" in text
    assert "COPY MGR (dry-run)" in text
