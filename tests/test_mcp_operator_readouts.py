"""Operator read-outs on the MCP surface (2026-10-01) and the two repairs that came with them.

* ``kaiba_rebuild_clusters`` no longer clusters in-process (the likely trigger of 13 OOM
  kills of the MCP server that froze the box, stop-loss included).
* ``kaiba_experiments`` reads ``gate_results.reasons_json``; it used to select a column
  that does not exist and swallow the error into ``gates: []``.
* ``kaiba_copy_manager``, ``kaiba_health``, ``kaiba_wallet_grade_counts`` and
  ``kaiba_live_ev``: what the owner kept asking for, without terminal + SQL.

Temp databases only; nothing here calls a provider.
"""

from __future__ import annotations

import json
import sys

import pytest

from kaiba.core import events as ev
from kaiba.core.config import ChainBudget, LaneConfig, RiskConfig, save_risk
from kaiba.core.schemas import Chain, EventKind, Lane, LaneMode, now_ms
from kaiba.mcp import server

NOW = 1_790_870_000_000  # 2026-10-01 15:53:20 UTC
H = 3_600_000
D = 86_400_000
ETH = 10**18
STOP = 39 * 10**15  # 0.039 ETH

VPORT = "0x" + "a1" * 20
FOO = "0x" + "b2" * 20
IDLE = "0x" + "c3" * 20
STALE = "0x" + "d4" * 20
OLD = "0x" + "e5" * 20
STRANGER = "0x" + "f6" * 20


def _risk(**overrides) -> RiskConfig:
    cfg = RiskConfig(
        global_mode=LaneMode.LIVE,
        chains={
            Chain.ROBINHOOD: ChainBudget(enabled=True, bankroll_base_units=ETH,
                                         daily_loss_stop_base_units=STOP),
            Chain.SOL: ChainBudget(enabled=False, daily_loss_stop_base_units=450_000_000),
        },
        lanes={Lane.SM_TRENCHES: LaneConfig(mode=LaneMode.LIVE, chains=[Chain.ROBINHOOD]),
               Lane.MIGRATION_FADE: LaneConfig(mode=LaneMode.SHADOW)},
    )
    return cfg.model_copy(update=overrides)


@pytest.fixture
def ro_db(tmp_db, tmp_path, monkeypatch):
    """The MCP module on a temp database, a Robinhood-only risk file and a schedule file."""
    monkeypatch.setattr(server, "_conn", lambda: tmp_db)
    risk_path = tmp_path / "risk.yaml"
    save_risk(_risk(), risk_path)
    monkeypatch.setenv("KAIBA_RISK_PATH", str(risk_path))
    schedule = tmp_path / "schedule.yaml"
    schedule.write_text("jobs:\n  execute_planned:\n    enabled: true\n", encoding="utf-8")
    monkeypatch.setenv("KAIBA_SCHEDULE_CONFIG", str(schedule))
    return tmp_db


# ---------------------------------------------------------------- registration


def test_readouts_are_registered_as_reads_and_pass_the_signer_policy(ro_db):
    expected = {
        "kaiba_copy_manager": "positions",
        "kaiba_health": "status",
        "kaiba_wallet_grade_counts": "wallets_read",
        "kaiba_live_ev": "performance_read",
    }
    for name, operation in expected.items():
        assert name in server.TOOLS
        assert server.TOOL_OPERATIONS[name] == operation
        out = server.guard_tool(name, server.TOOLS[name])()
        assert isinstance(out, dict) and "refused" not in str(out.get("reason", ""))
    server.check_tool_surface()


def test_unknown_chain_is_refused_as_data(ro_db):
    assert server.kaiba_live_ev(chain="nope")["ok"] is False
    assert server.kaiba_copy_manager(chain="nope")["ok"] is False


# ---------------------------------------------------------------- rebuild_clusters


def test_rebuild_clusters_refuses_and_never_loads_clustering(ro_db, monkeypatch):
    for module in ("kaiba.intelligence.cluster", "kaiba.intelligence.entity"):
        monkeypatch.delitem(sys.modules, module, raising=False)
    journal_before = ro_db.execute("SELECT COUNT(*) FROM journal").fetchone()[0]

    out = server.TOOLS["kaiba_rebuild_clusters"]("sol")

    assert out == {
        "ok": False,
        "reason": "clustering runs in ops and is disabled since 2026-09-29 (OOM); "
                  "this tool no longer runs it in-process",
    }
    assert "kaiba.intelligence.cluster" not in sys.modules
    assert "kaiba.intelligence.entity" not in sys.modules
    assert ro_db.execute("SELECT COUNT(*) FROM journal").fetchone()[0] == journal_before


# ---------------------------------------------------------------- experiments


def _gate(conn, experiment_id: str, passed: int, reasons: list[str], created: int) -> None:
    conn.execute(
        "INSERT INTO gate_results (experiment_id, gate, passed, reasons_json, metrics_json, created_ms) "
        "VALUES (?,?,?,?,?,?)",
        (experiment_id, "replay", passed, json.dumps(reasons), "{}", created),
    )


def test_experiments_return_the_recorded_gate_verdicts(ro_db):
    judged = server.kaiba_propose_experiment("tighter stop", "sm-trenches", {"stop": 20})["experiment_id"]
    server.kaiba_propose_experiment("never judged", "sm-trenches", {"stop": 25})
    _gate(ro_db, judged, 0, ["mean_below_baseline", "n_below_minimum"], 111)

    out = server.kaiba_experiments()

    by_id = {e["experiment_id"]: e for e in out["experiments"]}
    assert by_id[judged]["gates"] == [{
        "gate": "replay", "passed": False, "created_ms": 111,
        "reasons": ["mean_below_baseline", "n_below_minimum"],
    }]
    assert "gates_error" not in out
    assert out["status_counts"] == {"proposed": 2}
    assert out["proposed_without_gate_run"] == 1


def test_an_unreadable_gate_table_is_reported_not_returned_as_empty(ro_db):
    server.kaiba_propose_experiment("h", "sm-trenches", {"k": 1})
    ro_db.execute("DROP TABLE gate_results")

    out = server.kaiba_experiments()

    assert out["experiments"][0]["gates"] is None  # unknown, never "no verdicts"
    assert out["gates_error"].startswith("gate_results unreadable: OperationalError")


# ---------------------------------------------------------------- live EV


def _position(conn, pid: str, *, closed: int | None, cost: int, realized: int,
              reason: str = "stop_loss", chain: str = "robinhood", mode: str = "live") -> None:
    conn.execute(
        "INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms, closed_ms, "
        "cost_native, realized_native, exit_reason) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (pid, chain, f"tok_{pid}", "sm-trenches", mode, (closed or NOW) - H, closed,
         str(cost), str(realized), reason),
    )


@pytest.fixture
def ev_book(ro_db):
    huge_cost = 4 * 10**22  # beyond SQLite's int64: summing it through REAL would round
    _position(ro_db, "a", closed=NOW - 1 * H, cost=ETH, realized=ETH // 2, reason="trailing_stop")
    _position(ro_db, "b", closed=NOW - 2 * H, cost=ETH, realized=-3 * ETH // 10)
    _position(ro_db, "c", closed=NOW - 3 * D, cost=2 * ETH, realized=-2 * ETH // 10,
              reason="stale_no_volume:3609s")
    _position(ro_db, "d", closed=NOW - 3 * D, cost=ETH, realized=-3 * ETH // 10)
    _position(ro_db, "big", closed=NOW - 4 * D, cost=huge_cost, realized=-huge_cost // 4,
              reason="rug:lp_-49.0pct")
    # Excluded: paper, another chain, outside the window, still open, in the future.
    _position(ro_db, "paper", closed=NOW - H, cost=ETH, realized=9 * ETH, mode="shadow")
    _position(ro_db, "solana", closed=NOW - H, cost=ETH, realized=9 * ETH, chain="sol")
    _position(ro_db, "ancient", closed=NOW - 8 * D, cost=ETH, realized=9 * ETH)
    _position(ro_db, "open", closed=None, cost=ETH, realized=0)
    _position(ro_db, "future", closed=NOW + H, cost=ETH, realized=9 * ETH)
    ro_db.execute(
        "INSERT INTO native_prices (chain, ts_ms, price_usd, source) VALUES ('eth', ?, '2500', 'test')",
        (NOW - 60_000,),
    )
    return ro_db


def test_live_ev_seven_days_is_exact_and_live_only(ev_book):
    out = server.live_ev(ev_book, days=7, chain="robinhood", now=NOW)

    assert out["n"] == 5 and out["wins"] == 1
    assert out["mean_pct"] == -9.0          # (50 - 30 - 10 - 30 - 25) / 5
    assert out["median_pct"] == -25.0
    assert out["net_native_units"] == ETH // 2 - 3 * ETH // 10 - 2 * ETH // 10 - 3 * ETH // 10 - 10**22
    assert out["best_pct"] == 50.0 and out["worst_pct"] == -30.0
    families = [row["exit_reason"] for row in out["by_exit_reason"]]
    assert families == ["rug", "stop_loss", "stale_no_volume", "trailing_stop"]  # costliest first
    stop = out["by_exit_reason"][1]
    assert (stop["n"], stop["wins"], stop["mean_pct"], stop["net_native"]) == (2, 0, -30.0, "-0.600000")


def test_live_ev_one_day_with_usd(ev_book):
    out = server.live_ev(ev_book, days=1, chain="robinhood", now=NOW)

    assert (out["n"], out["wins"], out["mean_pct"], out["median_pct"]) == (2, 1, 10.0, 10.0)
    assert out["net_native"] == "0.200000"
    assert out["net_usd"] == 500.0
    assert out["native_usd"]["source_chain"] == "eth"  # robinhood's native asset is ETH


def test_live_ev_counts_a_zero_cost_close_without_inventing_a_percent(ro_db):
    _position(ro_db, "free", closed=NOW - H, cost=0, realized=10**15)
    out = server.live_ev(ro_db, days=1, chain="robinhood", now=NOW)
    assert out["n"] == 1 and out["wins"] == 1
    assert out["mean_pct"] is None and out["pct_unavailable"] == 1
    assert out["net_usd"] is None  # no native price stored: unknown, not zero


# ---------------------------------------------------------------- wallet grades


def _score(conn, chain: str, n: int, grade: str, scored: int) -> None:
    for i in range(n):
        conn.execute(
            "INSERT INTO wallet_scores (chain, address, score, grade, evidence_weight, archetype, "
            "model_version, scored_at_ms) VALUES (?,?,?,?,?,?,?,?)",
            (chain, f"{chain}-{grade}-{scored}-{i}", 50.0, grade, 1.0, "unknown", "t", scored),
        )


@pytest.fixture
def graded(ro_db):
    _score(ro_db, "sol", 1, "B", NOW - H)
    _score(ro_db, "sol", 1, "B", NOW - 3 * D)
    _score(ro_db, "sol", 3, "C", NOW - H)
    _score(ro_db, "sol", 5, "UNSCORED", NOW - H)
    _score(ro_db, "robinhood", 1, "B", NOW - 2 * H)
    _score(ro_db, "robinhood", 1, "A", NOW - H // 2)
    _score(ro_db, "robinhood", 2, "D", NOW - H)
    _score(ro_db, "bsc", 1, "QUARANTINED", NOW - H)
    return ro_db


def test_wallet_grade_counts_by_chain_and_new_a_b(graded):
    out = server.wallet_grade_counts(graded, hours=24, now=NOW)

    assert out["grades_total"] == {"A": 1, "B": 3, "C": 3, "D": 2, "QUARANTINED": 1, "UNSCORED": 5}
    assert out["by_chain"]["sol"] == {"A": 0, "B": 2, "C": 3, "UNSCORED": 5}
    assert out["by_chain"]["robinhood"] == {"A": 1, "B": 1, "D": 2}
    assert out["a_b_scored_in_window"] == {"sol": {"A": 0, "B": 1}, "robinhood": {"A": 1, "B": 1}}
    assert out["totals_only"] == {}


def test_a_grade_too_large_to_split_is_a_total_only(graded, monkeypatch):
    monkeypatch.setattr(server, "GRADE_SPLIT_MAX_ROWS", 4)
    out = server.wallet_grade_counts(graded, hours=24, now=NOW)
    assert out["totals_only"] == {"UNSCORED": 5}
    assert "UNSCORED" not in out["by_chain"]["sol"]
    assert out["by_chain"]["sol"]["C"] == 3  # still under the cap, still split


# ---------------------------------------------------------------- copy_manager


def _run(conn, at: int, status: str = "ok", result: dict | None = None, error: str | None = None) -> None:
    conn.execute(
        "INSERT INTO ops_runs (job, started_ms, finished_ms, status, result_json, error) "
        "VALUES ('copy_manager', ?, ?, ?, ?, ?)",
        (at, at + 500, status, json.dumps(result or {}), error),
    )


def _decision(token: str, symbol: str, kind: str, pnl: str) -> dict:
    return {"token": token, "symbol": symbol, "kind": kind, "fraction": "1", "pnl": pnl,
            "reason": f"{symbol} {kind}"}


def _state(conn, chain: str, token: str, peak: str, updated: int) -> None:
    conn.execute(
        "INSERT INTO kv (key, value, updated_ms) VALUES (?,?,?)",
        (f"copy_mgr:{chain}:{token}",
         json.dumps({"started_s": 1, "rungs_done": [], "peak_pnl": peak, "prices": [], "last_action_ms": 0}),
         updated),
    )


def _order(conn, oid: str, token: str, side: str, created: int) -> None:
    conn.execute(
        "INSERT INTO orders (order_id, chain, token, side, lane, mode, input_token, output_token, "
        "amount_in, min_out, slippage_bps, state, provider, created_ms, updated_ms) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (oid, "robinhood", token, side, "manual", "live", token, "eth", "100", "1", 100,
         "filled", "gmgn", created, created),
    )


@pytest.fixture
def copy_book(ro_db):
    _run(ro_db, NOW - 30 * H, result={"live": False, "decisions": [_decision(OLD, "OLD", "trim", "0.3")]})
    _run(ro_db, NOW - 50 * 60_000, result={
        "live": False, "holdings": 4, "managed": 1, "skipped": {"thin_pool": 2},
        "decisions": [_decision(VPORT, "VPORT", "giveback", "-0.05")], "sells": [], "errors": []})
    _run(ro_db, NOW - 30 * 60_000, "error",
         error="gmgn holdings unavailable: limiter refused: gmgn: minimum interval (retry in 0.1s)")
    _run(ro_db, NOW - 20 * 60_000, "error",
         error="gmgn holdings unavailable: limiter refused: gmgn: minimum interval (retry in 0.0s)")
    # The later report shape: an `action` per decision, and held/attempts/sells_today per run.
    _run(ro_db, NOW - 10 * 60_000, result={
        "live": False, "holdings": 5, "managed": 2, "skipped": {"thin_pool": 2, "dust": 1},
        "held": {"run_cap": 1}, "attempts": 0, "sells_today": 0,
        "decisions": [{**_decision(VPORT, "VPORT", "giveback", "-0.10"), "action": "dry_run"},
                      {**_decision(FOO, "FOO", "trim", "0.30"), "action": "held:run_cap"}],
        "sells": [], "errors": ["FOO: wallet holds none"]})
    _run(ro_db, NOW - 5 * 60_000, "timeout", error="timed out after 90s; thread abandoned until it returns")

    _state(ro_db, "robinhood", VPORT, "1.8198", NOW - 10 * 60_000)
    _state(ro_db, "robinhood", FOO, "0.3", NOW - 10 * 60_000)
    _state(ro_db, "robinhood", IDLE, "0.05", NOW - 2 * H)
    _state(ro_db, "robinhood", STALE, "0.9", NOW - 3 * D)
    _state(ro_db, "sol", "So1anaToken", "2.0", NOW - H)

    _order(ro_db, "ord_tracked", FOO, "sell", NOW - 15 * 60_000)
    _order(ro_db, "ord_untracked", STRANGER, "sell", NOW - 15 * 60_000)
    _order(ro_db, "ord_buy", FOO, "buy", NOW - 15 * 60_000)
    def system(subject: str, payload: dict) -> None:
        ro_db.execute(
            "INSERT INTO events (ts_ms, kind, chain, subject, payload) VALUES (?,?,?,?,?)",
            (NOW - 15 * 60_000, "system", "robinhood", subject, json.dumps(payload)),
        )

    sell = {"service": "copy_manager", "action": "copy_manager_sell", "outcome": "refused",
            "symbol": "FOO", "kind": "trim", "pnl": "0.30", "qty": "100", "order_id": "ord_x",
            "error": "venue refused"}
    system(FOO, sell)                                    # decided token: reported
    system(IDLE, {**sell, "symbol": "IDLE"})             # never decided in the window: not probed
    system(FOO, {"service": "copy_manager", "action": "something_else"})  # not a sell
    return ro_db


def test_copy_manager_per_token_decisions_peaks_and_runs(copy_book):
    out = server.copy_manager_report(copy_book, hours=24, chain="robinhood", now=NOW)

    assert out["live"] is False
    assert out["runs"] == {"total": 5, "by_status": {"ok": 2, "error": 2, "timeout": 1},
                           "with_decisions": 2, "truncated": False}
    assert out["latest_run"]["skipped"] == {"thin_pool": 2, "dust": 1}
    assert out["latest_run"]["held"] == {"run_cap": 1} and out["latest_run"]["sells_today"] == 0
    assert out["decisions_by_kind"] == {"giveback": 2, "trim": 1}
    assert out["decisions_by_action"] == {"dry_run": 1, "held:run_cap": 1}  # old-shape rows carry none
    assert out["tokens_with_decisions"] == 2

    tokens = {t["token"]: t for t in out["tokens"]}
    assert set(tokens) == {VPORT, FOO, IDLE}  # STALE unseen in window; OLD decided outside it
    vport = tokens[VPORT]
    assert vport["symbol"] == "VPORT"
    assert vport["last_decision"]["kind"] == "giveback" and vport["last_decision"]["pnl_pct"] == -10.0
    assert vport["last_decision"]["action"] == "dry_run"
    assert vport["first_decision_in_window"]["pnl_pct"] == -5.0
    assert vport["decision_runs"] == 2 and vport["decision_kinds"] == {"giveback": 2}
    assert vport["peak_pct"] == 182.0
    assert tokens[FOO]["last_decision"]["pnl_pct"] == 30.0 and tokens[FOO]["peak_pct"] == 30.0
    assert tokens[IDLE]["decision_runs"] == 0 and tokens[IDLE]["last_decision"] is None
    assert tokens[IDLE]["peak_pct"] == 5.0
    assert out["tokens"][-1]["token"] == IDLE  # undecided tokens sort last

    assert out["run_failures"] == [
        {"error": "gmgn holdings unavailable: limiter refused: gmgn: minimum interval (retry in #s)", "n": 2},
        {"error": "timed out after #s; thread abandoned until it returns", "n": 1},
    ]
    assert out["sell_errors"] == [{"error": "FOO: wallet holds none", "n": 1}]


def test_copy_manager_live_sells_come_only_from_tracked_sells(copy_book):
    out = server.copy_manager_report(copy_book, hours=24, chain="robinhood", now=NOW)
    sources = sorted((s["source"], s["order_id"]) for s in out["live_sells"])
    assert sources == [("events", "ord_x"), ("orders", "ord_tracked")]
    event = next(s for s in out["live_sells"] if s["source"] == "events")
    assert (event["token"], event["outcome"], event["pnl_pct"], event["error"]) == (
        FOO, "refused", 30.0, "venue refused")


def test_copy_manager_names_match_the_module_that_writes_them():
    from kaiba.execution import copy_manager

    assert server.COPY_STATE_PREFIX == copy_manager.STATE_PREFIX
    # Present in the version that records live sells as system events; absent in 09-30's.
    assert server.COPY_SERVICE == getattr(copy_manager, "SERVICE", server.COPY_SERVICE)
    assert server.COPY_SELL_ACTION == getattr(copy_manager, "SELL_ACTION", server.COPY_SELL_ACTION)


# ---------------------------------------------------------------- health


def _ops(conn, job: str, at: int, status: str, error: str | None = None) -> None:
    conn.execute(
        "INSERT INTO ops_runs (job, started_ms, status, error) VALUES (?,?,?,?)", (job, at, status, error)
    )


def _heartbeat(conn, **fields) -> None:
    ev.emit(EventKind.SYSTEM, {"service": "watchdog", "event": "heartbeat", **fields}, conn=conn)


@pytest.fixture
def box(ro_db):
    t = now_ms()
    for i in range(3):
        _ops(ro_db, "copy_manager", t - (i + 1) * 60_000, "ok")
    _ops(ro_db, "copy_manager", t - 5 * 60_000, "error", "limiter refused (retry in 0.1s)")
    _ops(ro_db, "copy_manager", t - 6 * 60_000, "error", "limiter refused (retry in 0.0s)")
    _ops(ro_db, "deployer_stats", t - 7 * 60_000, "timeout", "timed out after 300s")
    _ops(ro_db, "deployer_stats", t - 8 * 60_000, "timeout", "timed out after 300s")
    _ops(ro_db, "wallet_tape", t - 2 * D, "error", "outside the window")
    _heartbeat(ro_db, checked=3, blind=2, stranded_deferred=5, standing_unprotected=3,
               exit_failures=0, longest_blind_s=428331, price_source="venue+gmgn")
    ev.emit(EventKind.DECISION, {"lane": "sm-trenches"}, conn=ro_db)
    day = server.datetime.fromtimestamp(t / 1000, tz=server.UTC).strftime("%Y-%m-%d")
    ro_db.execute(
        "INSERT INTO risk_state (day_key, realized_native_json, entries, halted, halt_reason, updated_ms) "
        "VALUES (?,?,?,?,?,?)", (day, '{"robinhood": -1}', 6, 0, None, t),
    )
    # The gate's realized_today: live closes today, paper excluded.
    _position(ro_db, "today_loss", closed=t, cost=ETH, realized=-2 * ETH // 100)
    _position(ro_db, "today_paper", closed=t, cost=ETH, realized=-9 * ETH, mode="shadow")
    # Two open live positions: one exiting normally, one stuck (the box had 204 attempts).
    _position(ro_db, "held_ok", closed=None, cost=ETH // 50, realized=0)
    _position(ro_db, "held_stuck", closed=None, cost=ETH // 50, realized=0)
    _position(ro_db, "held_paper", closed=None, cost=ETH, realized=0, mode="shadow")
    for pid, attempts, blind in (("held_ok", 3, None), ("held_stuck", 204, t - 60_000)):
        ro_db.execute(
            "INSERT INTO watchdog_state (position_id, exit_state, exit_attempts, blind_since_ms, updated_ms) "
            "VALUES (?,?,?,?,?)", (pid, "failed" if attempts > 10 else None, attempts, blind, t),
        )
    return ro_db, t


def test_health_reports_jobs_watchdog_risk_and_red_flags(box):
    conn, t = box
    out = server.health_report(conn, hours=24, now=t + 4_000)

    jobs = {j["job"]: j for j in out["jobs"]}
    assert set(jobs) == {"copy_manager", "deployer_stats"}  # the 2-day-old run is outside
    assert (jobs["copy_manager"]["ok"], jobs["copy_manager"]["error"]) == (3, 2)
    assert jobs["copy_manager"]["top_error"] == "limiter refused (retry in #s)"
    assert jobs["copy_manager"]["top_error_n"] == 2
    assert out["jobs"][0]["job"] == "copy_manager"  # most failures first

    wd = out["watchdog"]
    assert wd["found"] is True and wd["stale"] is False
    assert (wd["blind"], wd["stranded_deferred"], wd["standing_unprotected"]) == (2, 5, 3)

    assert out["risk_today"]["entries"] == 6 and out["risk_today"]["halted"] is False
    (rh,) = out["daily_loss"]  # enabled chains only
    assert rh["chain"] == "robinhood" and rh["realized_today"] == "-0.020000"
    assert rh["stop_used_pct"] == 51.3 and rh["stopped"] is False

    assert out["controls"] == {
        "global_mode": "live", "kill_switch": False, "entries_paused": False, "reduce_only": False,
        "enabled_chains": ["robinhood"], "live_lanes": ["sm-trenches"], "execute_planned_enabled": True,
    }
    assert out["pipeline_last_seen_s"]["decision"] is not None
    assert out["pipeline_last_seen_s"]["order.filled"] is None
    assert out["storage"]["wal_reset_size_bytes"] == 512 * 1024 * 1024
    assert 0 <= out["storage"]["disk_used_pct"] <= 100
    assert out["ops_last_run_age_s"] is not None

    assert "watchdog blind 2" in out["red"]
    assert "watchdog stranded_deferred 5" in out["red"]
    assert "watchdog standing_unprotected 3" in out["red"]
    assert "job deployer_stats failed every run (2)" in out["red"]
    assert "1 open position(s) with exit_attempts > 10 (max 204)" in out["red"]
    assert not any("entries_paused" in r for r in out["red"])

    held = {p["position_id"]: p for p in out["open_positions"]}
    assert set(held) == {"held_ok", "held_stuck"}  # paper is not protected inventory
    assert held["held_stuck"]["exit_attempts"] == 204 and held["held_stuck"]["exit_state"] == "failed"
    assert held["held_stuck"]["cost_native"] == "0.020000"
    assert 60 <= held["held_stuck"]["blind_s"] <= 70
    assert held["held_ok"]["blind_s"] is None


def test_a_pause_is_red(box, tmp_path, monkeypatch):
    conn, t = box
    save_risk(_risk(entries_paused=True), tmp_path / "paused.yaml")
    monkeypatch.setenv("KAIBA_RISK_PATH", str(tmp_path / "paused.yaml"))
    assert "entries_paused is ON" in server.kaiba_health()["red"]


def test_heartbeat_search_is_bounded(ro_db):
    _heartbeat(ro_db, checked=1, blind=0)
    for i in range(10):
        ev.emit(EventKind.SYSTEM, {"service": "ops", "event": "tick", "i": i}, conn=ro_db)

    assert server.heartbeat(ro_db, scan_rows=5)["found"] is False
    assert server.heartbeat(ro_db, scan_rows=50)["found"] is True


def test_no_heartbeat_at_all_is_red(ro_db):
    out = server.health_report(ro_db, hours=24)
    assert out["watchdog"]["found"] is False
    assert "no watchdog heartbeat found" in out["red"]
