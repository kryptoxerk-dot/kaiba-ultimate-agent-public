"""kaiba.learning.labs: the research labs' read-outs for the dashboard and the agent."""

from __future__ import annotations

import gzip
import json
import time
from pathlib import Path

import pytest

from kaiba.learning import labs


def _split(low: float, mid: float, high: float, cuts: list[float]) -> dict:
    cell = lambda m: {h: {"n": 100, "mean": m} for h in ("all", "older", "newer")}  # noqa: E731
    return {"low": cell(low), "mid": cell(mid), "high": cell(high), "cuts": cuts}


def _lp_result(at: int, freq_yes: float = 0.0054, freq_no: float = 0.0029) -> dict:
    width = _split(0.005, 0.01, 0.007, [0.5, 1.2])
    width["low"]["newer"]["mean"] = 0.02  # older half: mid best; newer half: low best
    return {
        "at": at, "positions": 1000, "open_positions": 100, "pools": 2, "owners": 50,
        "Q1": {k: {h: {"n": 1000, "mean": 0.0078, "median": 0.0003, "pos_share": 0.7} for h in ("all", "older", "newer")}
               for k in ("lp_sol", "lp_usd", "lp_minus_hodl")},
        "Q2_fees_cover_share": 0.7, "Q2_fees": {"mean": 0.015}, "Q2_inventory": {"mean": -0.007},
        "Q3_in_range": _split(-0.0037, 0.0199, 0.0161, [0.27, 0.95]),
        "Q4": {"dur_s": _split(0.0014, 0.0045, 0.0176, [1644, 25843]), "width": width,
               "prior_move": _split(0.0087, 0.0071, 0.0056, [0.0, 0.17])},
        "capital_day": {"all": {"all": {"per_sol_day": 0.003}},
                        "frequent_lp": {"True": {"all": {"per_sol_day": freq_yes}},
                                        "False": {"all": {"per_sol_day": freq_no}}}},
        "per_lp": {"share_positive": 0.71}, "forward": {"since": at - 86_400, "lp_sol": {"n": 0}},
        "pools_table": [
            {"address": "GoodP222222222222222222222222222222222222", "name": "GOOD-SOL", "positions": 300,
             "lp_sol": {"mean": 0.03}, "lp_minus_hodl": 0.01},
            {"address": "BadP2222222222222222222222222222222222222", "name": "BAD-SOL", "positions": 129,
             "lp_sol": {"mean": -0.0366}, "lp_minus_hodl": -0.0247},
        ],
    }


def _write_lp(base: Path, result: dict) -> None:
    (base / "labs" / "lp").mkdir(parents=True, exist_ok=True)
    (base / "labs" / "lp" / "result.json").write_text(json.dumps(result), encoding="utf-8")


@pytest.fixture
def lp_dir(tmp_path: Path) -> Path:
    _write_lp(tmp_path, _lp_result(int(time.time())))
    return tmp_path


def test_missing_results_are_reported_not_guessed(tmp_path: Path):
    assert labs.lp_lab(tmp_path) == {"available": False, "reason": "the daily LP-lab job has not written yet"}
    assert labs.wallet_lab(tmp_path)["available"] is False
    assert labs.wallet_behavior("anything", tmp_path) is None
    assert labs.lp_decision(0.5, 3, data_dir=tmp_path)["decision"] is None


def test_lp_headline_and_staleness(lp_dir: Path, tmp_path_factory):
    lab = labs.lp_lab(lp_dir)
    assert lab["available"] and not lab["stale"]
    assert lab["vs_sol"]["mean"] == 0.0078 and lab["per_sol_day"] == 0.003
    old = tmp_path_factory.mktemp("old")
    _write_lp(old, _lp_result(int(time.time()) - 40 * 3600))
    assert labs.lp_lab(old)["stale"] is True


def test_in_range_position_is_kept(lp_dir: Path):
    out = labs.lp_decision(0.9, 10, out_of_range_now=False, data_dir=lp_dir)
    assert out["decision"] == "keep"
    assert any("mid tercile" in e and "+1.99%" in e for e in out["evidence"])
    assert any("high duration tercile" in e for e in out["evidence"])


def test_out_of_range_rebalances_only_when_recentering_measured_better(lp_dir: Path, tmp_path_factory):
    assert labs.lp_decision(0.1, 2, out_of_range_now=True, data_dir=lp_dir)["decision"] == "rebalance"
    flipped = tmp_path_factory.mktemp("flipped")
    _write_lp(flipped, _lp_result(int(time.time()), freq_yes=0.001, freq_no=0.003))
    out = labs.lp_decision(0.1, 2, out_of_range_now=True, data_dir=flipped)
    assert out["decision"] == "close"
    assert any("did not measure better" in e for e in out["evidence"])


def test_a_losing_pool_closes_even_in_range(lp_dir: Path):
    bad = labs.lp_decision(0.9, 10, pool="BadP2222222222222222222222222222222222222", data_dir=lp_dir)
    assert bad["decision"] == "close" and bad["pool"]["name"] == "BAD-SOL"
    good = labs.lp_decision(0.9, 10, pool="GoodP222222222222222222222222222222222222", data_dir=lp_dir)
    assert good["decision"] == "keep"


def test_narrowing_is_claimed_only_when_width_held_in_both_halves(lp_dir: Path, tmp_path_factory):
    mixed = labs.lp_decision(0.9, 10, data_dir=lp_dir)
    assert any("no measured case for narrowing" in e for e in mixed["evidence"])
    same = tmp_path_factory.mktemp("same")
    result = _lp_result(int(time.time()))
    result["Q4"]["width"] = _split(0.001, 0.02, 0.005, [0.5, 1.2])
    _write_lp(same, result)
    out = labs.lp_decision(0.9, 10, data_dir=same)
    assert any("the mid width tercile" in e and "both halves" in e for e in out["evidence"])


def test_wallet_profile_and_verdict(tmp_path: Path):
    base = tmp_path / "labs" / "wallet"
    base.mkdir(parents=True)
    now = int(time.time())
    with gzip.open(base / "profiles.json.gz", "wt", encoding="utf-8") as fh:
        json.dump({"at": now, "window_days": 14, "wallets": {
            "W1": {"tier": "A", "name": "a", "episodes": 12, "classes": ["sniper"], "F1_entry_age_s": 30},
            "W2": {"tier": "B", "name": "b", "episodes": 0, "classes": []}}}, fh)
    (base / "summary.json").write_text(json.dumps({"at": now, "tracked": 2, "profiled": 1}), encoding="utf-8")
    (base / "lab_result.json").write_text(json.dumps({"test_episodes": 10, "table": [
        {"group": "graded", "class": "sniper", "exit": "E2_30m", "n": 120, "mean": -0.05, "PASS": False},
        {"group": "control", "class": "sniper", "exit": "E2_30m", "n": 90, "mean": -0.02},
        {"group": "graded", "class": "dip_buyer", "exit": "E3_4h", "n": 101, "mean": 0.01, "PASS": True}]}),
        encoding="utf-8")
    prof = labs.wallet_behavior("W1", tmp_path)
    assert prof["classes"] == ["sniper"] and prof["class_notes"]["sniper"].startswith("median entry under 60 s")
    assert "not whether following it pays" in prof["caveat"]
    lab = labs.wallet_lab(tmp_path)
    assert [w["address"] for w in lab["wallets"]] == ["W1"]  # a wallet with no tokens is not listed
    verdict = lab["verdict"]
    assert [c["class"] for c in verdict["cells"]] == ["dip_buyer", "sniper"]  # graded only, best first
    assert [c["class"] for c in verdict["passing"]] == ["dip_buyer"]


# --------------------------------------------------------------------------------------
# dashboard: /labs and the behavior block on /w
# --------------------------------------------------------------------------------------

PASSWORD = "labs-test-password"
WALLET = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"


@pytest.fixture
def console(tmp_db, tmp_path, monkeypatch):
    import shutil

    from fastapi.testclient import TestClient

    from dashboard import intel, trading
    from dashboard.app import create_app
    from kaiba.core.config import DEFAULT_RISK_PATH, get_settings

    risk_path = tmp_path / "risk.yaml"
    shutil.copy(DEFAULT_RISK_PATH, risk_path)
    monkeypatch.setenv("KAIBA_RISK_PATH", str(risk_path))
    monkeypatch.setenv("KAIBA_DASHBOARD_PASSWORD", PASSWORD)
    monkeypatch.setenv("KAIBA_DASHBOARD_ISOLATE", "0")
    get_settings.cache_clear()
    trading.clear_caches()
    intel.clear_caches()
    client = TestClient(create_app())
    assert client.post("/login", data={"password": PASSWORD}, follow_redirects=False).status_code == 303
    try:
        yield client
    finally:
        client.close()
        get_settings.cache_clear()


def test_labs_page_without_results_says_so(console):
    page = console.get("/labs")
    assert page.status_code == 200
    assert "the daily LP-lab job has not written yet" in page.text
    assert "the daily wallet-lab job has not written yet" in page.text


def test_labs_page_renders_results_and_the_position_check(console, tmp_path: Path):
    _write_lp(tmp_path, _lp_result(int(time.time())))
    page = console.get("/labs", params={"ir": 10, "h": 2, "oor": "true", "pool": "BadP2222222222222222222222222222222222222"})
    assert page.status_code == 200
    assert "GOOD-SOL" in page.text and "BAD-SOL" in page.text
    assert 'class="lab-verdict d-close"' in page.text  # the losing pool wins over the re-center rule
    assert console.get("/labs", params={"ir": 101, "h": 2}).status_code == 422  # out of range input


def test_wallet_page_shows_the_behavior_profile(console, tmp_path: Path):
    base = tmp_path / "labs" / "wallet"
    base.mkdir(parents=True)
    with gzip.open(base / "profiles.json.gz", "wt", encoding="utf-8") as fh:
        json.dump({"at": int(time.time()), "window_days": 14, "wallets": {WALLET: {
            "tier": "B", "name": "b", "episodes": 9, "classes": ["fast_flipper"], "F1_entry_age_s": 75,
            "F6_first_sell_s": 50, "F6_first_sell_frac": 0.5, "closed": 3, "closed_won": 1, "closed_net_usd": -12.5}}}, fh)
    page = console.get(f"/w/sol/{WALLET}")
    assert page.status_code == 200
    assert "How it trades" in page.text and "fast flipper" in page.text
    other = console.get("/w/sol/EGLpQAhQVB3gN9ZMsgjCBjabf42sPQ3K7T6DRAvy8pr")
    assert "How it trades" not in other.text


def test_report_lines_are_one_per_lab(lp_dir: Path, tmp_path_factory):
    empty = tmp_path_factory.mktemp("empty")
    assert labs.render_lines(labs.overview(empty)) == ["LABS not run yet (LP lab, wallet lab)"]
    lines = labs.render_lines(labs.overview(lp_dir))
    assert len(lines) == 2
    assert lines[0].startswith("LABS LP (") and "vs SOL +0.78%" in lines[0]
    assert "best GOOD-SOL +3.00%" in lines[0] and "worst BAD-SOL -3.66%" in lines[0]
    assert lines[1] == "LABS wallets the daily wallet-lab job has not written yet"


def _lp_track(base: Path, nets: list[float], name: str = "fast", age_s: float = 5) -> None:
    from kaiba.execution import lp_paper as lp

    profile = lp.PROFILES[name]
    root = profile.root(base / "labs" / "lp_paper")
    bot = lp.Bot(None, root, profile=profile)
    now = int(time.time() * 1000)
    for i, net in enumerate(nets):
        bot.db.execute("INSERT INTO observations(id, pool, name, planned_ms, status, features_json, version) "
                       "VALUES (?, 'PoolA', 'AAA-SOL', 1, 'closed', ?, 'v')",
                       (i + 1, json.dumps({"age_h": 0.4, "fee_tvl_30m": 1.2})))
        bot.db.execute("INSERT INTO arm_results(obs_id, arm, status, exit_reason, at_ms, hold_s, net_pct, net_sol, fee_pct,"
                       " inventory_pct, exit_cost_pct, tx_sol, rent_sol) VALUES (?, ?, 'closed', 'max_hold', ?, 60, ?, ?, 1, 0, 0, 0, 0)",
                       (i + 1, profile.primary, now - (len(nets) - i) * 60_000, net, net / 100))
    bot.db.commit()
    bot.db.close()
    (root / "status.json").write_text(json.dumps({
        "at_ms": now - age_s * 1000, "open": 1, "counts": {},
        "last_selection": {"listed": 50, "picked": 1, "volume": 20, "fee_tvl": 4}}), encoding="utf-8")


def test_the_board_shows_each_track_its_pass_line_and_why_pools_were_passed_over(tmp_path: Path):
    _lp_track(tmp_path, [1.0, -0.5, 2.0])
    board = labs.lp_board(tmp_path)
    fast, slow, *_ = board["tracks"]
    assert fast["available"] and not fast["stale"] and not slow["available"]
    assert fast["curve"]["points"][-1][1] == 2.5
    assert fast["pass_rows"][0]["value"] == "3 of 300" and fast["pass_rows"][0]["ok"] is False
    assert fast["skips"][0] == ("too little volume", 20)
    assert board["recent"][0]["track"] == "Fast scalp" and board["recent"][0]["age_h"] == 0.4
    assert board["live"] is None and board["any_passed"] is False


def test_a_stopped_track_reads_stale_not_quiet(tmp_path: Path):
    _lp_track(tmp_path, [1.0], age_s=600)
    assert labs.lp_board(tmp_path)["tracks"][0]["stale"] is True


def test_a_long_curve_is_thinned_and_keeps_its_last_point(tmp_path: Path):
    from kaiba.execution import lp_paper as lp

    _lp_track(tmp_path, [1.0] * 1000)
    curve = lp.ledger(lp.FAST.root(tmp_path / "labs" / "lp_paper"), lp.FAST)["curve"]["points"]
    assert len(curve) <= 260 and curve[-1][1] == 1000.0


def test_the_board_is_cached_briefly(tmp_path: Path):
    _lp_track(tmp_path, [1.0])
    first = labs.lp_board(tmp_path)
    assert labs.lp_board(tmp_path) is first


def test_lp_page_says_paper_only_and_shows_the_track(console, tmp_path: Path):
    labs._board_cache.clear()
    _lp_track(tmp_path, [1.0, -0.5])
    page = console.get("/lp")
    assert page.status_code == 200
    assert "Live execution" in page.text and "Not connected" in page.text
    assert "Fast scalp" in page.text and "AAA-SOL" in page.text and "data-series" in page.text
    assert console.get("/lp/body").status_code == 200


def test_lp_page_with_no_data_says_so(console):
    labs._board_cache.clear()
    page = console.get("/lp")
    assert page.status_code == 200 and "has not reported yet" in page.text


def _write_best(base: Path, **over) -> None:
    (base / "labs" / "wallet").mkdir(parents=True, exist_ok=True)
    cell = {"n": 3, "mean": -0.04, "median": -0.1, "win": 0.33, "lower90": None, "leader_mean": 0.02}
    board = {"at": int(time.time()), "window_days": 7, "tracked": 191, "with_trades": 90, "eligible": 12, "lists": 1,
             "first_list_at": int(time.time()), "top": [{"rank": 1, "wallet": WALLET, "tier": "A", "name": "x", "n": 6, "mean": 0.31,
                                                         "median": 0.1, "win": 0.5, "leader_mean": 0.4, "last_trade": int(time.time()), "new": True}],
             "forward": {"trades": 3, "proof": {"arm": "top_5", "need_n": 100, "need_days": 21, "n": 3, "days": 0.4, "pass": False},
                         "arms": {k: cell for k in ("top_1", "top_2", "top_5", "top_10", "all_tracked")}}, **over}
    (base / "labs" / "wallet" / "best.json").write_text(json.dumps(board), encoding="utf-8")


def test_wallets_page_shows_the_best_list_and_says_it_is_not_a_signal(console, tmp_path: Path):
    _write_best(tmp_path)
    page = console.get("/intel/wallets")
    assert page.status_code == 200
    assert "Best wallets" in page.text and "Not proven" in page.text and "not a buy signal" in page.text
    assert f"/w/sol/{WALLET}" in page.text and "Does the list hold up?" in page.text


def test_wallets_page_before_the_first_list_says_so(console):
    page = console.get("/intel/wallets")
    assert page.status_code == 200 and "The first list is built by the daily wallet lab." in page.text


def test_the_daily_line_reports_the_list_without_calling_it_proven(tmp_path: Path):
    _write_best(tmp_path)
    view = {"best": labs.best_wallets(tmp_path), "lp": {"available": False}, "wallet": {"available": False, "reason": "x"}}
    view["wallet"] = {"available": True, "age_s": 1, "stale": False, "summary": {"class_counts": {}, "profiled": 0, "tracked": 0, "classed": 0},
                      "verdict": {"available": False, "reason": "x"}}
    line = next(x for x in labs.render_lines(view) if x.startswith("LABS best wallets"))
    assert "12 ranked" in line and "3 of 100 trades" in line and "not proven" in line


def test_wallets_page_survives_a_list_with_no_scored_trades_yet(console, tmp_path: Path):
    _write_best(tmp_path, forward={"trades": 0, "proof": {"arm": "top_5", "need_n": 100, "need_days": 21, "n": 0, "days": 0, "pass": False},
                                   "arms": {k: {"n": 0} for k in ("top_1", "top_2", "top_5", "top_10", "confluence", "all_tracked")}})
    page = console.get("/intel/wallets")
    assert page.status_code == 200 and "Does the list hold up?" in page.text


def test_lp_page_survives_a_setup_with_only_missed_entries(console, tmp_path: Path):
    from kaiba.execution import lp_paper as lp

    labs._board_cache.clear()
    _lp_track(tmp_path, [1.0])
    root = lp.FAST.root(tmp_path / "labs" / "lp_paper")
    import sqlite3

    db = sqlite3.connect(root / "lp_paper.db")
    db.execute("INSERT INTO arm_results(obs_id, arm, status, at_ms, net_pct, net_sol, tx_sol, rent_sol) "
               "VALUES (1, 's5_30', 'missed', 1, -0.03, -0.0003, 0.0003, 0)")
    db.commit()
    db.close()
    assert console.get("/lp").status_code == 200


def test_the_gmgn_import_holds_only_wallets_a_copier_made_money_on(monkeypatch):
    from dashboard import wallet_export

    top = [{"rank": 1, "wallet": "a", "tier": "A", "mean": 0.1}, {"rank": 2, "wallet": "b", "tier": "B", "mean": -0.1}]
    monkeypatch.setattr(labs, "best_wallets", lambda: {"available": True, "top": top})
    assert [r["address"] for r in wallet_export._best_rows()] == ["a"]
