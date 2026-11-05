"""Loss attribution: every loss split into ENTRY vs EXIT components that sum to it, entry
features ranked on the SCANNED population with a held-out half, and nothing proposed.

Hand-computable fixture (prices in USD, entry 1.0, stop 0.70 = -30%):

    A  stop_loss       marks 1.05 0.80 0.69 0.65 | sell fill 0.62 | ret -40
       entry -30 | exit -10 = gap -5 (mark 0.65 vs stop) + fill -3 + cost -2  -> ENTRY
    B  emergency_loss  marks 0.98 0.35          | sell fill 0.33 | ret -68
       entry -30 | exit -38 = gap -35 + fill -2 + cost -1                     -> EXIT
    C  bookkeeping_correction                                                 -> non_market
    D  trailing_stop   stop 1.20, marks 1.60 1.18 | sell fill 1.15 | ret +14
       entry 0 | exit +14 = cushion +20 + gap -2 + fill -3 + cost -1          -> win
"""

from __future__ import annotations

import json

import pytest

from kaiba.learning import loss_attribution as LA

ETH = 10**18
T0 = 1_790_000_000_000


def _position(conn, pid, *, chain="robinhood", reason, entry="1.0", stop="0.70", cost=ETH, ret_pct,
              opened=T0, closed=T0 + 600_000, marks=(), sells=(), native_usd="2000", pool=()):
    realized = int(cost * ret_pct / 100)
    conn.execute(
        "INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms, closed_ms, cost_native, "
        "realized_native, entry_price_usd, stop_price_usd, exit_reason, mfe_pct, mae_pct) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (pid, chain, f"tok_{pid}", "sm-trenches", "live", opened, closed, str(cost), str(realized),
         entry, stop, reason, None, None),
    )
    conn.execute(
        "INSERT INTO trades (trade_id, position_id, decision_id, lane, mode, chain, token, opened_ms, "
        "closed_ms, hold_s, cost_native, proceeds_native, pnl_native, pnl_pct, exit_reason) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"trd_{pid}", pid, f"dec_{pid}", "sm-trenches", "live", chain, f"tok_{pid}", opened, closed,
         (closed - opened) // 1000, str(cost), str(cost + realized), str(realized), ret_pct, reason),
    )
    _fill(conn, pid, f"buy_{pid}", "buy", opened, entry, native_usd)
    for i, (ts, px) in enumerate(sells):
        _fill(conn, pid, f"sell_{pid}_{i}", "sell", ts, px, native_usd)
    for ts, px in marks:
        conn.execute("INSERT INTO position_marks (position_id, ts_ms, price_usd) VALUES (?,?,?)", (pid, ts, px))
    for ts, px in pool:
        conn.execute(
            "INSERT INTO onchain_price_samples (ts_ms, position_id, token, pool, pool_kind, price_quote, "
            "price_usd, source, stop_price_usd) VALUES (?,?,?,?,?,?,?,?,?)",
            (ts, pid, f"tok_{pid}", "0xpool", "v2", "1", px, "onchain:v2", stop),
        )


def _fill(conn, pid, oid, side, ts, px, native_usd):
    conn.execute("INSERT INTO position_orders (position_id, order_id, side, ts_ms) VALUES (?,?,?,?)",
                 (pid, oid, side, ts))
    conn.execute(
        "INSERT INTO fill_prices (order_id, chain, token, side, fill_ts_ms, fill_ts_basis, native_atoms, "
        "token_atoms, decimals_basis, price_usd, native_usd, basis, computed_ms) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (oid, "robinhood", f"tok_{pid}", side, ts, "caller", "1", "100", "caller", px, native_usd,
         "fill_ratio", ts),
    )


@pytest.fixture
def book(tmp_db):
    _position(tmp_db, "A", reason="stop_loss", ret_pct=-40,
              marks=[(T0 + 100_000, "1.05"), (T0 + 200_000, "0.80"), (T0 + 400_000, "0.69"),
                     (T0 + 500_000, "0.65")],
              sells=[(T0 + 550_000, "0.62")],
              pool=[(T0 + 300_000, "0.70"), (T0 + 540_000, "0.66")])
    _position(tmp_db, "B", chain="sol", reason="emergency_loss", ret_pct=-68, cost=10**9,
              marks=[(T0 + 100_000, "0.98"), (T0 + 500_000, "0.35")], sells=[(T0 + 550_000, "0.33")])
    _position(tmp_db, "C", reason="bookkeeping_correction:unbooked_wallet_signed_settlement", ret_pct=-14,
              marks=[(T0 + 100_000, "0.9")], sells=[(T0 + 550_000, "0.86")])
    _position(tmp_db, "D", reason="trailing_stop", stop="1.20", ret_pct=14,
              marks=[(T0 + 100_000, "1.60"), (T0 + 500_000, "1.18")], sells=[(T0 + 550_000, "1.15")])
    rows = LA.closed_positions(tmp_db, since_ms=T0 - 1, until_ms=T0 + 10**7)
    return {r["position_id"]: LA.attribute_position(tmp_db, r) for r in rows}


# ===================================================================== PART A: the split


def test_a_stop_loss_splits_into_the_stop_and_what_execution_added(book):
    a = book["A"]
    assert (a["ret_pct"], a["level_pct"]) == (-40.0, -30.0)
    assert (a["entry_pp"], a["exit_pp"]) == (-30.0, -10.0)
    assert (a["gap_pp"], a["fill_pp"], a["cost_pp"], a["cushion_pp"]) == (-5.0, -3.0, -2.0, 0.0)
    assert a["label"] == "entry"
    assert (a["mfe_pct"], a["mae_pct"], a["excursion_basis"], a["marks"]) == (5.0, -35.0, "marks", 4)
    assert a["never_green"] == 0
    # first mark at or under the stop is +400 s; the sell went out 150 s later
    assert (a["time_to_level_s"], a["exit_latency_s"]) == (400.0, 150.0)
    # the pool: at the trigger it read 0.66 (-34%); we sold 4 pp under it, our mark was 1 pp
    # under it, and the pool crossed the stop 100 s before our mark did
    assert (a["pool_ret_at_trigger_pct"], a["fill_vs_pool_pp"], a["mark_vs_pool_pp"]) == (-34.0, -4.0, -1.0)
    assert a["pool_lead_s"] == 100.0
    assert (a["cost_usd"], a["pnl_usd"]) == (2000.0, -800.0)


def test_a_gap_through_the_stop_is_an_exit_loss(book):
    b = book["B"]
    assert (b["entry_pp"], b["exit_pp"]) == (-30.0, -38.0)
    assert (b["gap_pp"], b["fill_pp"], b["cost_pp"]) == (-35.0, -2.0, -1.0)
    assert b["label"] == "exit" and b["never_green"] == 1


def test_components_always_sum_to_the_return(book):
    for pid, r in book.items():
        if r["entry_pp"] is None:
            continue
        assert r["entry_pp"] + r["exit_pp"] == pytest.approx(r["ret_pct"], abs=1e-6), pid
        parts = [r[k] for k in ("cushion_pp", "gap_pp", "fill_pp", "cost_pp")]
        if None not in parts:
            assert sum(parts) == pytest.approx(r["exit_pp"], abs=1e-6), pid


def test_a_trailing_winner_reports_the_cushion_it_gave_back(book):
    d = book["D"]
    assert d["label"] == "win" and d["entry_pp"] == 0.0 and d["exit_pp"] == 14.0
    assert (d["cushion_pp"], d["gap_pp"], d["fill_pp"], d["cost_pp"]) == (20.0, -2.0, -3.0, -1.0)


def test_a_ledger_repair_is_not_a_market_loss(book):
    assert book["C"]["label"] == "non_market"
    s = LA.summarise_trades(list(book.values()), since_ms=T0 - 1)
    assert s["closed"] == 3 and s["losses"] == 2  # C left out


def test_missing_evidence_is_none_with_a_reason(tmp_db):
    _position(tmp_db, "E", reason="stop_loss", ret_pct=-33, stop=None, marks=[], sells=[(T0 + 1, None)])
    r = LA.attribute_position(tmp_db, LA.closed_positions(tmp_db, since_ms=T0 - 1, until_ms=T0 + 10**7)[0])
    assert r["entry_pp"] is None and r["label"] == "unattributed"
    assert any("stop" in n for n in r["notes"]) and any("unpriced" in n for n in r["notes"])


def test_the_summary_ranks_the_leaks_by_money(book):
    s = LA.summarise_trades(list(book.values()), since_ms=T0 - 1)
    assert (s["entry_pp"], s["exit_pp"]) == (-60.0, -48.0)
    assert s["entry_share"] == round(60 / 108, 3)
    assert s["labels"] == {"entry": 1, "exit": 1, "unattributed": 0}
    leaks = [(lk["side"], lk["n"], lk["sum_pp"]) for lk in s["leaks"]]
    assert leaks == [("entry", 2, -60.0), ("exit", 2, -40.0), ("exit", 2, -5.0), ("exit", 2, -3.0)]
    assert s["leaks"][0]["leak"].startswith("entry: price path fell to the stop")
    assert s["never_green_losses"] == "1/2"
    assert s["onchain"]["positions"] == 1 and s["onchain"]["mean_pool_lead_s"] == 100.0


# ===================================================================== PART B: outcomes


def _rows(prices, start, step=10_000):
    return [{"ts_ms": start + i * step, "price_usd": str(p), "side": "buy", "wallet": f"w{i}",
             "usd_value": "10"} for i, p in enumerate(prices)]


@pytest.mark.parametrize("path,expected,basis", [
    ([1.1, 1.35, 1.4], 30.0, "up"),
    ([0.9, 0.69, 0.6], -30.0, "down"),
    ([1.05, 1.1, 1.1], 10.0, "horizon"),
    ([1.0, 2.0, 1.0, 1.0], 0.0, "horizon"),   # one silly print is smoothed away, not a +100%
])
def test_forward_outcome_is_a_bracket_at_the_stop(path, expected, basis):
    ts = T0
    out = LA.forward_outcome(_rows([1.0, 1.0, 1.0], ts - 60_000), _rows(path, ts + 1_000),
                             ts_ms=ts, horizon_s=3600, barrier_pct=30, pre_window_s=300)
    assert (out["fwd_ret_pct"], out["fwd_basis"]) == (pytest.approx(expected, abs=1e-9), basis)


def test_no_price_is_no_outcome_never_zero():
    out = LA.forward_outcome([], _rows([1.0], T0 + 1), ts_ms=T0, horizon_s=60, barrier_pct=30, pre_window_s=300)
    assert out["fwd_ret_pct"] is None and "pre-window" in out["why"]
    out = LA.forward_outcome(_rows([1.0], T0 - 1), [], ts_ms=T0, horizon_s=60, barrier_pct=30, pre_window_s=300)
    assert out["fwd_ret_pct"] is None and "after" in out["why"]


# ===================================================================== PART B: the ranking


def _scan(n=240, *, none_share=0.0):
    """holder_count separates (high wins +30, low loses -30); `noise` does not."""
    rows = []
    for i in range(n):
        win = i % 2 == 0
        holders = (400 + i) if win else (50 + i % 100)
        if none_share and (i % 100) < none_share * 100:
            holders = None
        rows.append({"ts_ms": T0 + i * 60_000, "fwd_ret_pct": 30.0 if win else -30.0, "action": "skip",
                     "features": {"holder_count": holders, "noise": float(i % 7)},
                     "feature_basis": {"holder_count": "recorded", "noise": "recorded"}})
    return rows


def test_a_feature_that_separates_is_chosen_on_the_older_half_and_scored_on_the_newer():
    ranked = LA.rank_features(_scan(), ["noise", "holder_count"], min_n=40, min_keep_share=0.5)
    top = ranked[0]
    assert top["feature"] == "holder_count" and top["holds_out"] is True
    assert top["key"] == "min_holder_count" and top["direction"] == "ge"
    assert top["newer"]["keep_share"] >= 0.5 and top["newer"]["delta_pp"] > 0
    noise = next(e for e in ranked if e["feature"] == "noise")
    assert noise.get("holds_out") is not True


def test_a_recorded_unknown_counts_as_refused_so_a_kill_switch_cannot_rank():
    """60% of rows carry no holder count: any floor refuses them live, so no floor can keep
    half the scanned book -- the holder-floor trap of 2026-09-24, caught by construction."""
    ranked = LA.rank_features(_scan(none_share=0.6), ["holder_count"], min_n=40, min_keep_share=0.5)
    assert ranked[0].get("holds_out") is not True
    assert "keeps" in ranked[0]["verdict"]


def test_a_threshold_that_keeps_too_little_of_the_newer_half_does_not_rank():
    """Chosen to keep half of the older book; the feature's level then FELL (the lane's
    window went 300 s -> 1800 s on 2026-09-24 and every flow number moved), so the same
    threshold keeps a tenth of today's flow -- still all winners, so it "improves", but a
    filter that refuses 90% of today is a kill switch, not an edge."""
    rows = []
    for i in range(240):
        win = i % 2 == 0
        if i < 120:
            x = (150 + i % 50) if win else (10 + i % 50)
        else:
            x = (300 if i % 10 == 0 else 80) if win else 10
        rows.append({"ts_ms": T0 + i * 60_000, "fwd_ret_pct": 30.0 if win else -30.0,
                     "features": {"x": x}, "feature_basis": {"x": "recorded"}})
    top = LA.rank_features(rows, ["x"], min_n=40, min_keep_share=0.5)[0]
    assert top["key"] == "min_x" and top["older"]["keep_share"] >= 0.5
    assert top["newer"]["delta_pp"] > 0 and top["newer"]["keep_share"] < 0.5
    assert top["holds_out"] is False and "keeps only" in top["verdict"]


def test_too_few_rows_is_said_not_ranked():
    ranked = LA.rank_features(_scan(n=20), ["holder_count"], min_n=40, min_keep_share=0.5)
    assert ranked[0]["verdict"].startswith("too few")


# ===================================================================== the read side


def test_persist_then_summary_then_render(tmp_db, book):
    report = {"generated_ms": T0 + 10**6, "version": LA.VERSION, "_rows": list(book.values()),
              "trades": {"7d": LA.summarise_trades(list(book.values()), since_ms=T0 - 1)},
              "features": [], "scanned": {"measured": 0}}
    LA.persist(tmp_db, report)
    stored = {r["position_id"]: dict(r) for r in tmp_db.execute(f"SELECT * FROM {LA.TABLE}")}
    assert set(stored) == {"A", "B", "C", "D"} and stored["A"]["gap_pp"] == -5.0
    assert json.loads(stored["A"]["notes_json"]) == []
    data = LA.summary(tmp_db, now=T0 + 10**6 + 3_600_000)
    assert data["age_s"] == 3600.0 and "_rows" not in data
    lines = LA.render_lines(data)
    assert lines[0].startswith("LOSSES 7d live (loss_attribution, 1.0h old): 2 of 3 closed lost")
    assert "ENTRY -60pp | EXIT -48pp" in lines[0]
    assert lines[1].startswith("  top leak: entry: price path fell to the stop")
    # a second run replaces, never duplicates
    LA.persist(tmp_db, report)
    assert tmp_db.execute(f"SELECT COUNT(*) FROM {LA.TABLE}").fetchone()[0] == 4


def test_summary_before_the_first_run_says_so(tmp_db):
    assert LA.render_lines(LA.summary(tmp_db)) == [
        "LOSSES cannot measure: loss_attribution has not run yet (no kv row)"]


def test_build_reads_only(tmp_db, book):
    before = tmp_db.total_changes
    report = LA.build(tmp_db, now=T0 + 10**7, params={"trade_days": 1, "summary_days": 1, "scan_days": 1})
    assert tmp_db.total_changes == before
    assert report["trades"]["1d"]["losses"] == 2 and report["scanned"]["measured"] == 0


def test_the_job_is_registered_and_scheduled():
    from pathlib import Path

    from kaiba.ops import scheduler as S

    assert S.JOBS["loss_attribution"].run is S.job_loss_attribution
    cfg = S.load_config(Path(__file__).resolve().parents[1] / "config" / "schedule.yaml")
    job = cfg.jobs["loss_attribution"]
    assert job.enabled and job.timeout_s < cfg.lock_stale_s
    assert float(job.params["min_keep_share"]) == 0.5  # = gates.MAX_REMOVAL_SHARE
    from kaiba.learning.gates import MAX_REMOVAL_SHARE

    assert LA.DEFAULTS["min_keep_share"] == MAX_REMOVAL_SHARE


def test_the_job_writes_its_table_and_kv(tmp_db, book):
    out = LA.run(tmp_db, now=T0 + 10**7, params={"trade_days": 1, "summary_days": 1, "scan_days": 1})
    assert out["written"] == {"positions": 4, "kv": LA.KV_KEY}
    assert (out["window"], out["losses"]) == ("1d", 2)
    assert tmp_db.execute("SELECT COUNT(*) FROM kv WHERE key=?", (LA.KV_KEY,)).fetchone()[0] == 1


def test_the_daily_report_prints_the_losses_section(tmp_db, book, tmp_path, monkeypatch, capsys):
    from kaiba.core.config import RiskConfig, save_risk
    from kaiba.ops import daily_report

    save_risk(RiskConfig(), tmp_path / "risk.yaml")
    monkeypatch.setenv("KAIBA_RISK_PATH", str(tmp_path / "risk.yaml"))
    LA.run(tmp_db, now=T0 + 10**7, params={"trade_days": 1, "summary_days": 1, "scan_days": 1})
    tmp_db.commit()
    assert daily_report.main(["--db", str(tmp_path / "kaiba.db")]) == 0
    text = capsys.readouterr().out
    assert "LOSSES 1d live (loss_attribution" in text
    assert "top leak: entry: price path fell to the stop" in text
