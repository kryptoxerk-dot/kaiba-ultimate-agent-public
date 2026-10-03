"""The scheduled experiment loop: proposed -> replay -> shadow -> promoted / rejected -> monitored.

Every row here uses the REAL sm-trenches payload shape (``kaiba/execution/lanes.py``): the
feature behind ``min_smart_degen`` is recorded as ``smart_wallets``, and the lane echoes its
own ``max_rug_ratio`` threshold into the payload. A fixture that used the threshold's own
name as the feature would pass with the replay reading the wrong field -- which is exactly
what the old fixtures did.

Hand-computable: history = 64 hourly entries alternating smart_wallets 5 (+200 on 1000) and
3 (-250 on 1000). Candidate ``min_smart_degen`` 3 -> 4 keeps exactly the winners.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from kaiba.core import journal
from kaiba.core.config import load_risk
from kaiba.core.schemas import Lane
from kaiba.learning import experiment_loop as loop
from kaiba.learning import gates

BASE_MS = 1_700_000_000_000
HOUR = 3_600_000
DAY = 86_400_000
HISTORY = 240
HISTORY_SPACING = 10 * HOUR
#: The shadow starts an hour after the last history entry (history spans ~100 days: the
#: relative criterion needs >= 20 distinct days out of sample, and the last 30% = 30 days).
NOW = BASE_MS + (HISTORY - 1) * HISTORY_SPACING + HOUR
#: Forward streams span 25 days (>= 20 distinct days for the day-cluster bound).
FORWARD_DAYS = 25
PROMOTE_AT = NOW + (FORWARD_DAYS + 1) * DAY
LANE = "sm-trenches"


# ---------------------------------------------------------------------------- fixtures


@pytest.fixture
def risk_file(tmp_path, monkeypatch) -> Path:
    return write_risk_file(tmp_path / "risk.yaml", monkeypatch)


def write_risk_file(path: Path, monkeypatch) -> Path:
    """An isolated, live sm-trenches envelope; points KAIBA_RISK_PATH at it."""
    path.write_text(yaml.safe_dump({
        "version": "v1",
        "global_mode": "live",
        "bounds": {
            "max_size_pct_bankroll": 30.0, "max_daily_loss_pct": 10.0, "max_slippage_bps": 2500,
            "max_lane_mode": "live", "allow_self_promotion": True,
        },
        "lanes": {LANE: {
            "mode": "live", "chains": ["robinhood"], "size_pct_min": 26.0, "size_pct_max": 29.0,
            "params": {
                "min_smart_degen": 3, "min_independent_entities": 2, "max_rug_ratio": 0.3,
                # A sizing-flavoured threshold the replay CAN judge (decisions.size_pct_bankroll
                # is a recorded column), so the policy -- not the gates -- must stop it. 0.25
                # takes every recorded entry (0.5 and 2.0), like min_smart_degen 3 does.
                "min_size_pct_bankroll": 0.25,
                "live_launchpads_by_chain": {"robinhood": ["pons", "pons_v2"]},
            },
        }},
    }), encoding="utf-8")
    monkeypatch.setenv("KAIBA_RISK_PATH", str(path))
    return path


def set_param(risk_file: Path, key: str, value) -> None:
    raw = yaml.safe_load(risk_file.read_text(encoding="utf-8"))
    raw["lanes"][LANE]["params"][key] = value
    risk_file.write_text(yaml.safe_dump(raw), encoding="utf-8")


def set_bound(risk_file: Path, key: str, value) -> None:
    raw = yaml.safe_load(risk_file.read_text(encoding="utf-8"))
    raw["bounds"][key] = value
    risk_file.write_text(yaml.safe_dump(raw), encoding="utf-8")


def param(risk_file: Path, key: str):
    return load_risk(risk_file).lane(Lane.SM_TRENCHES).params.get(key)


def add_entry(
    conn, tag: str, *, ts: int, smart: int | None, pnl: int, chain: str = "robinhood",
    mode: str = "live", action: str = "enter", with_trade: bool = True,
) -> str:
    """One signal -> decision -> closed trade, in the real sm-trenches payload shape."""
    sid = f"sig_{tag}"
    signals: list[str] = []
    if smart is not None:
        payload = {"smart_wallets": smart, "entity_count": 2, "rug_ratio": None,
                   "max_rug_ratio": "0.3", "rug_ratio_basis": "unavailable"}
        conn.execute(
            "INSERT INTO signals (signal_id, lane, chain, token, strength, created_ms, payload_json) "
            "VALUES (?,?,?,?,?,?,?)",
            (sid, LANE, chain, f"tok_{tag}", 0.6, ts - 1000, json.dumps(payload)),
        )
        signals = [sid]
    did = f"dec_{tag}"
    size_pct = None if smart is None else (2.0 if smart >= 4 else 0.5)
    conn.execute(
        "INSERT INTO decisions (decision_id, ts_ms, lane, mode, chain, token, action, thesis, "
        "confidence, signals_json, size_pct_bankroll, blockers_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (did, ts, LANE, mode, chain, f"tok_{tag}", action, "t", 0.6, json.dumps(signals), size_pct, "[]"),
    )
    if with_trade and action == "enter":
        conn.execute(
            "INSERT INTO trades (trade_id, position_id, decision_id, lane, mode, chain, token, "
            "opened_ms, closed_ms, hold_s, cost_native, proceeds_native, pnl_native, pnl_pct) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"trd_{tag}", f"pos_{tag}", did, LANE, mode, chain, f"tok_{tag}", ts, ts + 60_000, 60,
             "1000", str(1000 + pnl), str(pnl), pnl / 10.0),
        )
    return did


def build_history(conn, *, pattern: str = "clean", n: int = HISTORY) -> None:
    for i in range(n):
        high = i % 2 == 0
        if pattern == "clean":
            pnl = 200 if high else -250
        else:  # overfit: the winning side flips every block of 8
            pnl = 300 if (high == ((i // (n // 8)) % 2 == 0)) else -300  # flips every CSCV block
        add_entry(conn, f"h{i}", ts=BASE_MS + i * HISTORY_SPACING, smart=5 if high else 3, pnl=pnl)


def build_forward(conn, *, start: int, n_cand: int, n_inc: int, days: float,
                  cand_pnl: int = 200, inc_pnl: int = -250, prefix: str = "f") -> None:
    """Every third forward entry is one the candidate refuses (so n_inc must be n_cand/2 or 0)."""
    assert n_inc in (0, n_cand // 2)
    total = n_cand + n_inc
    step = int(days * DAY) // max(total - 1, 1)
    for i in range(total):
        is_cand = n_inc == 0 or i % 3 != 2
        add_entry(conn, f"{prefix}{i}", ts=start + HOUR + i * step,
                  smart=5 if is_cand else 3, pnl=cand_pnl if is_cand else inc_pnl)


def propose(conn, exp_id: str, diff: dict, *, lane: str | None = LANE) -> str:
    conn.execute(
        "INSERT INTO experiments (experiment_id, created_ms, lane, hypothesis, diff_json, status) "
        "VALUES (?,?,?,?,?,'proposed')",
        (exp_id, BASE_MS, lane, "test", json.dumps(diff)),
    )
    return exp_id


def status(conn, exp_id: str) -> str:
    return conn.execute("SELECT status FROM experiments WHERE experiment_id=?", (exp_id,)).fetchone()["status"]


def gate_rows(conn, exp_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT gate, passed, reasons_json FROM gate_results WHERE experiment_id=? ORDER BY id", (exp_id,)
    )]


def arms(conn, exp_id: str) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {"candidate": set(), "incumbent": set()}
    for r in conn.execute("SELECT trade_id, arm FROM experiment_trades WHERE experiment_id=?", (exp_id,)):
        out[r["arm"]].add(r["trade_id"])
    return out


def to_shadow(conn, exp_id: str = "exp_tight", **diff) -> str:
    build_history(conn)
    propose(conn, exp_id, diff or {"key": "min_smart_degen", "new": 4})
    loop.run(conn, now=NOW)
    assert status(conn, exp_id) == loop.SHADOW, gate_rows(conn, exp_id)
    return exp_id


def to_promoted(conn, exp_id: str = "exp_tight") -> str:
    to_shadow(conn, exp_id)
    build_forward(conn, start=NOW, n_cand=60, n_inc=30, days=FORWARD_DAYS)
    out = loop.run(conn, now=PROMOTE_AT)
    assert out["promoted"] == [exp_id], out
    return exp_id


# ======================================================================= features (gates)


def test_replay_reads_the_lane_measurement_not_the_echoed_threshold(tmp_db):
    """sm-trenches records smart_wallets/entity_count and ECHOES max_rug_ratio; confluence
    echoes min_entities beside entity_count. The threshold is never the feature."""
    tmp_db.execute(
        "INSERT INTO signals (signal_id, lane, chain, token, created_ms, payload_json) VALUES (?,?,?,?,?,?)",
        ("s1", LANE, "robinhood", "t", BASE_MS - 1, json.dumps(
            {"smart_wallets": 5, "entity_count": 2, "rug_ratio": None, "max_rug_ratio": "0.3",
             "min_entities": 9})),
    )
    d = {"ts_ms": BASE_MS, "signals_json": json.dumps(["s1"])}
    assert gates._feature_value(tmp_db, d, "min_smart_degen") == Decimal(5)
    assert gates._feature_value(tmp_db, d, "min_independent_entities") == Decimal(2)
    assert gates._feature_value(tmp_db, d, "min_entities") == Decimal(2)  # not the echoed 9
    assert gates._feature_value(tmp_db, d, "max_rug_ratio") is None       # not the echoed 0.3


# ======================================================================= proposed -> ?


def test_a_proposal_that_fails_replay_is_rejected_with_its_reasons(tmp_db, risk_file):
    build_history(tmp_db, pattern="overfit")
    propose(tmp_db, "exp_overfit", {"key": "min_smart_degen", "new": 4})

    out = loop.run(tmp_db, now=NOW)

    assert out["rejected"] == ["exp_overfit"]
    assert status(tmp_db, "exp_overfit") == loop.REJECTED
    rows = gate_rows(tmp_db, "exp_overfit")
    assert [(r["gate"], r["passed"]) for r in rows] == [("replay", 0)]
    assert any("PBO" in r for r in json.loads(rows[0]["reasons_json"]))
    notes = tmp_db.execute("SELECT notes, decided_by FROM experiments WHERE experiment_id='exp_overfit'").fetchone()
    assert notes["decided_by"] == "experiment_gates" and "PBO" in notes["notes"]
    assert param(risk_file, "min_smart_degen") == 3
    assert any("rejected exp_overfit" in e["body"]
               for e in journal.read(10, kind="experiment", subject="exp_overfit", conn=tmp_db))


def test_the_box_proposals_are_each_rejected_with_a_reason_that_says_what_to_fix(tmp_db, risk_file):
    """The four diffs on the box on 2026-10-02, verbatim in shape."""
    build_history(tmp_db)
    propose(tmp_db, "exp_window", {"window_s": 60})
    propose(tmp_db, "exp_job", {"jobs.execute_planned.max_plan_age_s": 30})
    propose(tmp_db, "exp_nested", {"dead_money_exit": {"quiet_window_s": 1800}}, lane=None)
    propose(tmp_db, "exp_text", {"key": "graduation_predictor.protocol_version", "new": "v1"},
            lane="graduation-predictor")

    loop.run(tmp_db, now=NOW)

    def reasons(exp_id):
        return " ".join(json.loads(r["reasons_json"])[0] for r in gate_rows(tmp_db, exp_id))

    for exp_id in ("exp_window", "exp_job", "exp_nested", "exp_text"):
        assert status(tmp_db, exp_id) == loop.REJECTED
    assert "no replay rule for key 'window_s'" in reasons("exp_window")  # flat diff normalised
    assert "no replay rule for key 'jobs.execute_planned.max_plan_age_s'" in reasons("exp_job")
    assert "diff does not name a lane and a key" in reasons("exp_nested")
    assert "is not numeric" in reasons("exp_text")


def test_a_proposal_that_passes_replay_starts_a_selection_shadow(tmp_db, risk_file):
    exp = to_shadow(tmp_db, "exp_tight")

    assert [(r["gate"], r["passed"]) for r in gate_rows(tmp_db, exp)] == [("replay", 1)]
    plan = loop.load_plan(tmp_db, exp)
    assert plan["chain"] == "robinhood" and plan["modes"] == ["live", "canary"]
    assert (plan["incumbent"], plan["candidate"], plan["candidate_value"]) == ("3", "4", 4)
    assert plan["started_ms"] == NOW
    assert param(risk_file, "min_smart_degen") == 3  # a shadow changes nothing


def test_the_replay_judges_against_the_running_incumbent_not_the_claimed_one(tmp_db, risk_file):
    build_history(tmp_db)
    propose(tmp_db, "exp_claim", {"key": "min_smart_degen", "old": 99, "new": 4})
    loop.run(tmp_db, now=NOW)
    replay = json.loads(tmp_db.execute(
        "SELECT replay_json FROM experiments WHERE experiment_id='exp_claim'").fetchone()["replay_json"])
    # Incumbent 3 takes all 64; the misremembered 99 would have taken none.
    assert replay["metrics"]["incumbent_trades"] == HISTORY


@pytest.mark.parametrize(
    ("new", "why"),
    [
        (2, "LOOSENS"),         # min_ lowered: admits signals the lane never emitted
        (3, "equals the incumbent"),
        (3.5, "truncated"),     # the lane reads int(p["min_smart_degen"])
    ],
)
def test_shadow_setup_refuses_what_selection_cannot_measure(tmp_db, risk_file, new, why):
    build_history(tmp_db)
    plan, reasons = loop.setup_shadow(
        tmp_db, {"experiment_id": "e", "lane": LANE}, {"lane": LANE, "key": "min_smart_degen", "new": new},
        now=NOW + 3 * DAY,
    )
    assert plan is None and why in reasons[0]


def test_shadow_setup_refuses_a_key_the_lane_never_reads(tmp_db, risk_file):
    build_history(tmp_db)
    plan, reasons = loop.setup_shadow(
        tmp_db, {"experiment_id": "e", "lane": LANE}, {"lane": LANE, "key": "min_smart_degn", "new": 4},
        now=NOW,
    )
    assert plan is None and "reads no parameter" in reasons[0]


# ======================================================================= shadow: the arms


def test_forward_trades_are_labelled_by_the_candidate_threshold_and_nothing_else(tmp_db, risk_file):
    exp = to_shadow(tmp_db)
    add_entry(tmp_db, "c1", ts=NOW + HOUR, smart=5, pnl=200)            # candidate
    add_entry(tmp_db, "c2", ts=NOW + 2 * HOUR, smart=4, pnl=-10)        # candidate (>= 4)
    add_entry(tmp_db, "i1", ts=NOW + 3 * HOUR, smart=3, pnl=-250)       # incumbent arm
    add_entry(tmp_db, "sol", ts=NOW + HOUR, smart=5, pnl=999, chain="sol")       # other chain
    add_entry(tmp_db, "twin", ts=NOW + HOUR, smart=5, pnl=999, mode="shadow")    # paper twin
    add_entry(tmp_db, "nofeat", ts=NOW + HOUR, smart=None, pnl=999)     # no point-in-time feature
    add_entry(tmp_db, "skip", ts=NOW + HOUR, smart=5, pnl=0, action="skip")
    # decided BEFORE the shadow began, closed after: in-sample, never an arm
    add_entry(tmp_db, "early", ts=NOW - HOUR, smart=5, pnl=999)

    out = loop.run(tmp_db, now=NOW + DAY)

    assert out["waiting"][exp]["candidate"] == 2 and out["waiting"][exp]["incumbent"] == 1
    assert arms(tmp_db, exp) == {"candidate": {"trd_c1", "trd_c2"}, "incumbent": {"trd_i1"}}
    assert loop.load_plan(tmp_db, exp)["missing_feature"] == 1
    assert status(tmp_db, exp) == loop.SHADOW
    assert [r["gate"] for r in gate_rows(tmp_db, exp)] == ["replay"]  # minimums not met: no gate

    before = tmp_db.execute("SELECT COUNT(*) FROM experiment_trades").fetchone()[0]
    loop.run(tmp_db, now=NOW + DAY + 1)
    loop.run(tmp_db, now=NOW + DAY + 2)
    assert tmp_db.execute("SELECT COUNT(*) FROM experiment_trades").fetchone()[0] == before
    assert [r["gate"] for r in gate_rows(tmp_db, exp)] == ["replay"]


def test_the_shadow_gate_waits_for_its_own_minimums(tmp_db, risk_file):
    exp = to_shadow(tmp_db)
    build_forward(tmp_db, start=NOW, n_cand=60, n_inc=30, days=10)  # enough trades, too few days
    loop.run(tmp_db, now=NOW + 11 * DAY)
    assert status(tmp_db, exp) == loop.SHADOW
    assert "shadow" not in [r["gate"] for r in gate_rows(tmp_db, exp)]


def test_the_shadow_gate_waits_for_enough_distinct_days_for_its_bound(tmp_db, risk_file):
    """>= 50 trades over >= 14 days is not enough: the relative bound needs >= 20 distinct
    days, and calling the gate at 16 would record a FAIL for an unfinished shadow."""
    exp = to_shadow(tmp_db)
    build_forward(tmp_db, start=NOW, n_cand=60, n_inc=30, days=15)
    out = loop.run(tmp_db, now=NOW + 17 * DAY)
    assert status(tmp_db, exp) == loop.SHADOW
    assert out["waiting"][exp]["days"] < loop.SHADOW_MIN_CLUSTER_DAYS
    assert "shadow" not in [r["gate"] for r in gate_rows(tmp_db, exp)]


def test_the_candidate_arm_must_span_the_gates_minimum_days_on_its_own(tmp_db, risk_file):
    """20+ distinct days across BOTH arms, but the candidate's own trades cover 12: the
    gate's min_days is about the candidate arm, so the shadow keeps waiting."""
    exp = to_shadow(tmp_db)
    for i in range(55):
        add_entry(tmp_db, f"c{i}", ts=NOW + HOUR + i * (12 * DAY // 54), smart=5, pnl=200)
    for i in range(30):
        add_entry(tmp_db, f"r{i}", ts=NOW + HOUR + i * (25 * DAY // 29), smart=3, pnl=-250)
    out = loop.run(tmp_db, now=PROMOTE_AT)
    assert out["waiting"][exp]["days"] >= loop.SHADOW_MIN_CLUSTER_DAYS
    assert out["waiting"][exp]["span_days"] < loop.SHADOW_MIN_DAYS
    assert status(tmp_db, exp) == loop.SHADOW
    assert "shadow" not in [r["gate"] for r in gate_rows(tmp_db, exp)]


def test_the_jobs_removal_ceiling_reaches_the_replay_gate(tmp_db, risk_file):
    build_history(tmp_db)  # the candidate removes exactly half of the decided signals
    propose(tmp_db, "exp_cut", {"key": "min_smart_degen", "new": 4})
    loop.run(tmp_db, now=NOW, params={"max_auto_refused_share": 0.4})
    assert status(tmp_db, "exp_cut") == loop.REJECTED
    assert "above the 40% ceiling" in gate_rows(tmp_db, "exp_cut")[0]["reasons_json"]


def test_a_one_armed_shadow_is_rejected_without_handing_the_gate_unlabelled_trades(tmp_db, risk_file):
    exp = to_shadow(tmp_db)
    build_forward(tmp_db, start=NOW, n_cand=55, n_inc=0, days=22)   # candidate refuses nothing
    add_entry(tmp_db, "solloser", ts=NOW + DAY, smart=3, pnl=-900, chain="sol")  # gate fallback bait

    out = loop.run(tmp_db, now=NOW + 23 * DAY)

    assert out["rejected"] == [exp]
    rows = gate_rows(tmp_db, exp)
    assert [(r["gate"], r["passed"]) for r in rows] == [("replay", 1), ("shadow_setup", 0)]
    assert "refused none" in rows[-1]["reasons_json"]


def test_a_changed_incumbent_ends_the_shadow(tmp_db, risk_file):
    exp = to_shadow(tmp_db)
    set_param(risk_file, "min_smart_degen", 5)  # the operator moved it mid-shadow
    loop.run(tmp_db, now=NOW + DAY)
    assert status(tmp_db, exp) == loop.REJECTED
    assert "incumbent changed" in gate_rows(tmp_db, exp)[-1]["reasons_json"]
    assert param(risk_file, "min_smart_degen") == 5


def test_a_shadow_that_fails_the_gate_is_rejected(tmp_db, risk_file):
    exp = to_shadow(tmp_db)
    # forward: the candidate's trades lose, the ones it refuses win
    build_forward(tmp_db, start=NOW, n_cand=60, n_inc=30, days=FORWARD_DAYS, cand_pnl=-300, inc_pnl=200)
    out = loop.run(tmp_db, now=PROMOTE_AT)
    assert out["rejected"] == [exp]
    assert gate_rows(tmp_db, exp)[-1]["gate"] == "shadow" and gate_rows(tmp_db, exp)[-1]["passed"] == 0
    assert param(risk_file, "min_smart_degen") == 3


# ======================================================================= promotion policy


def test_a_whitelisted_tightening_that_passes_both_gates_is_promoted(tmp_db, risk_file):
    exp = to_promoted(tmp_db)

    assert status(tmp_db, exp) == loop.PROMOTED
    assert param(risk_file, "min_smart_degen") == 4
    # Nothing else moved: not the bounds, not the band, not the allowlist.
    risk = load_risk(risk_file)
    assert risk.bounds.max_size_pct_bankroll == 30.0
    assert (risk.lane(Lane.SM_TRENCHES).size_pct_min, risk.lane(Lane.SM_TRENCHES).size_pct_max) == (26.0, 29.0)
    assert risk.lane(Lane.SM_TRENCHES).params["live_launchpads_by_chain"] == {"robinhood": ["pons", "pons_v2"]}
    plan = loop.load_plan(tmp_db, exp)
    assert plan["promoted_ms"] == PROMOTE_AT and plan["baseline"]["n"] == 90
    gates_seen = [(r["gate"], r["passed"]) for r in gate_rows(tmp_db, exp)]
    assert gates_seen == [("replay", 1), ("shadow", 1), ("replay", 1), ("shadow", 1)]  # promote re-ran both
    change = journal.read(10, kind="change", subject=exp, conn=tmp_db)
    assert any(e["body"].startswith(f"promoted {exp}:") for e in change)
    assert any("pre-registered rollback rule" in e["body"]
               for e in journal.read(10, kind="experiment", subject=exp, conn=tmp_db))


@pytest.mark.parametrize("switch", ["allow_self_promotion", "auto_promote"])
def test_a_pass_waits_for_the_owner_when_promotion_is_switched_off(tmp_db, risk_file, switch):
    exp = to_shadow(tmp_db)
    build_forward(tmp_db, start=NOW, n_cand=60, n_inc=30, days=FORWARD_DAYS)
    params = {}
    if switch == "allow_self_promotion":
        set_bound(risk_file, "allow_self_promotion", False)
    else:
        params = {"auto_promote": False}

    out = loop.run(tmp_db, now=PROMOTE_AT, params=params)

    assert out["awaiting_owner"] == [exp]
    assert status(tmp_db, exp) == loop.AWAITING_OWNER
    assert param(risk_file, "min_smart_degen") == 3
    assert any(e["body"].startswith("promotion withheld for the owner")
               for e in journal.read(10, kind="change", subject=exp, conn=tmp_db))
    loop.run(tmp_db, now=PROMOTE_AT + DAY, params=params)  # terminal: not re-judged
    assert status(tmp_db, exp) == loop.AWAITING_OWNER


def test_a_sizing_key_that_passes_both_gates_is_never_auto_promoted(tmp_db, risk_file):
    """size_pct_bankroll is a recorded decision column, so the gates CAN pass it; the
    promotion policy must still refuse. Owner directive: entry size is flat."""
    exp = to_shadow(tmp_db, "exp_size", key="min_size_pct_bankroll", new=1.5)
    build_forward(tmp_db, start=NOW, n_cand=60, n_inc=30, days=FORWARD_DAYS)

    out = loop.run(tmp_db, now=PROMOTE_AT)

    assert out["awaiting_owner"] == [exp], out
    assert [(r["gate"], r["passed"]) for r in gate_rows(tmp_db, exp)] == [("replay", 1), ("shadow", 1)]
    assert param(risk_file, "min_size_pct_bankroll") == 0.25
    notes = tmp_db.execute("SELECT notes FROM experiments WHERE experiment_id=?", (exp,)).fetchone()["notes"]
    assert "never promoted automatically" in notes


@pytest.mark.parametrize("key", [
    "size_pct_max", "size_pct_min", "max_size_pct_bankroll", "max_total_exposure_pct",
    "bankroll_base_units", "max_daily_loss_pct", "max_slippage_bps", "mode", "chains",
    "live_launchpads_by_chain", "max_position_base_units", "allow_self_promotion",
    "min_wallet_grade", "max_concurrent_positions",
])
def test_sizing_bounds_mode_and_chain_keys_are_refused(key):
    assert loop.key_refusal(key) is not None


@pytest.mark.parametrize("key", ["min_smart_degen", "min_independent_entities", "max_rug_ratio",
                                 "min_holder_count", "min_liquidity_usd"])
def test_entry_thresholds_are_promotable(key):
    assert loop.key_refusal(key) is None


def test_a_candidate_that_cuts_most_of_the_scanned_signals_waits_for_the_owner(tmp_db, risk_file):
    exp = to_shadow(tmp_db)
    build_forward(tmp_db, start=NOW, n_cand=60, n_inc=30, days=FORWARD_DAYS)
    # 200 forward signals the lane decided on but did not enter (skips), all below the
    # candidate: on fills the cut is 30/90, on the lane's signal population it is 230/290.
    for i in range(200):
        add_entry(tmp_db, f"sk{i}", ts=NOW + HOUR + i * 60_000, smart=3, pnl=0, action="skip")
    out = loop.run(tmp_db, now=PROMOTE_AT)
    assert out["awaiting_owner"] == [exp]
    assert param(risk_file, "min_smart_degen") == 3
    share = loop.load_plan(tmp_db, exp)["promotion_evidence"]["refused_share"]
    assert (share["refused"], share["measured"]) == (230, 290)


# ======================================================================= promoted: the rule


def test_rollback_fires_on_the_pre_registered_rule(tmp_db, risk_file):
    exp = to_promoted(tmp_db)
    t0 = PROMOTE_AT
    for i in range(20):
        add_entry(tmp_db, f"p{i}", ts=t0 + HOUR + i * HOUR, smart=5, pnl=-500)

    out = loop.run(tmp_db, now=t0 + 2 * DAY)

    assert out["rolled_back"] == [exp]
    assert status(tmp_db, exp) == loop.REJECTED
    assert param(risk_file, "min_smart_degen") == 3
    last = gate_rows(tmp_db, exp)[-1]
    assert (last["gate"], last["passed"]) == ("live_monitor", 0)
    assert any("rolled back" in e["body"] for e in journal.read(10, kind="correction", conn=tmp_db))
    assert journal.verify(tmp_db)[0] is True


def test_rollback_waits_for_its_minimum_and_holds_when_the_rule_holds(tmp_db, risk_file):
    exp = to_promoted(tmp_db)
    t0 = PROMOTE_AT
    for i in range(19):
        add_entry(tmp_db, f"p{i}", ts=t0 + HOUR + i * HOUR, smart=5, pnl=-500)
    out = loop.run(tmp_db, now=t0 + DAY)
    assert status(tmp_db, exp) == loop.PROMOTED and out["monitoring"][exp]["live_n"] == 19

    # 81 winners take the post-promotion sample to 100 with a mean well above the bound.
    for i in range(81):
        add_entry(tmp_db, f"w{i}", ts=t0 + 30 * HOUR + i * HOUR, smart=5, pnl=300)
    loop.run(tmp_db, now=t0 + 10 * DAY)
    assert status(tmp_db, exp) == loop.PROMOTED
    assert param(risk_file, "min_smart_degen") == 4
    assert loop.load_plan(tmp_db, exp)["monitor"]["closed"] == "confirmed"
    assert (gate_rows(tmp_db, exp)[-1]["gate"], gate_rows(tmp_db, exp)[-1]["passed"]) == ("live_monitor", 1)


def test_a_superseded_promotion_is_never_rolled_back(tmp_db, risk_file):
    exp = to_promoted(tmp_db)
    set_param(risk_file, "min_smart_degen", 6)  # a newer decision
    t0 = PROMOTE_AT
    for i in range(20):
        add_entry(tmp_db, f"p{i}", ts=t0 + HOUR + i * HOUR, smart=5, pnl=-500)
    loop.run(tmp_db, now=t0 + 2 * DAY)
    assert param(risk_file, "min_smart_degen") == 6
    assert status(tmp_db, exp) == loop.PROMOTED
    assert loop.load_plan(tmp_db, exp)["monitor"]["closed"] == "superseded"


def test_rollback_restores_only_the_promoted_key(tmp_db, risk_file):
    exp = to_promoted(tmp_db)
    # The lead widens the live allowlist after the promotion. A rollback must not undo it.
    set_param(risk_file, "live_launchpads_by_chain", {"robinhood": ["pons", "pons_v2", "newpad"]})

    assert gates.rollback(exp, tmp_db) is True

    assert param(risk_file, "min_smart_degen") == 3
    assert param(risk_file, "live_launchpads_by_chain") == {"robinhood": ["pons", "pons_v2", "newpad"]}


def test_rollback_refuses_once_the_promoted_key_has_moved_on(tmp_db, risk_file):
    exp = to_promoted(tmp_db)
    set_param(risk_file, "min_smart_degen", 6)
    assert gates.rollback(exp, tmp_db) is False
    assert param(risk_file, "min_smart_degen") == 6


def test_an_interrupted_promotion_is_finished_not_rejected(tmp_db, risk_file):
    exp = to_promoted(tmp_db)
    # Simulate a crash after promote() wrote risk.yaml + journal, before the status update.
    tmp_db.execute("UPDATE experiments SET status='shadow' WHERE experiment_id=?", (exp,))
    plan = loop.load_plan(tmp_db, exp)
    plan.pop("promoted_ms")
    loop.save_plan(tmp_db, plan)

    loop.run(tmp_db, now=PROMOTE_AT + DAY)

    assert status(tmp_db, exp) == loop.PROMOTED
    assert param(risk_file, "min_smart_degen") == 4
    assert "promoted_ms" in loop.load_plan(tmp_db, exp)


# ======================================================================= the run


def test_a_finished_run_repeats_nothing(tmp_db, risk_file):
    build_history(tmp_db, pattern="overfit")
    propose(tmp_db, "exp_a", {"key": "min_smart_degen", "new": 4})
    propose(tmp_db, "exp_b", {"window_s": 60})
    loop.run(tmp_db, now=NOW)
    gate_count = tmp_db.execute("SELECT COUNT(*) FROM gate_results").fetchone()[0]
    journal_count = tmp_db.execute("SELECT COUNT(*) FROM journal").fetchone()[0]

    out = loop.run(tmp_db, now=NOW + DAY)

    assert out["seen"] == 0
    assert tmp_db.execute("SELECT COUNT(*) FROM gate_results").fetchone()[0] == gate_count
    assert tmp_db.execute("SELECT COUNT(*) FROM journal").fetchone()[0] == journal_count


def test_a_misspelt_parameter_fails_instead_of_running_on_defaults():
    with pytest.raises(ValueError, match="auto_promot"):
        loop._params({"auto_promot": False})


def test_the_scheduled_job_runs_and_fails_loudly_on_a_bad_parameter(tmp_db, risk_file):
    from kaiba.ops import scheduler as S

    build_history(tmp_db)
    propose(tmp_db, "exp_job", {"key": "min_smart_degen", "new": 4})

    def ctx(params):
        return S.JobContext(name="experiment_gates", conn=tmp_db, params=params,
                            config=S.ScheduleConfig(), started_ms=NOW, deadline_ms=NOW + 300_000,
                            clock=lambda: NOW / 1000)

    result = S.job_experiment_gates(ctx({}))
    assert result["shadow_started"] == ["exp_job"]
    with pytest.raises(S.JobFailed):
        S.job_experiment_gates(ctx({"auto_promot": False}))


# ======================================================================= MCP read-out


def test_the_mcp_read_out_shows_status_latest_verdict_and_arms(tmp_db, risk_file, monkeypatch):
    from kaiba.mcp import server

    monkeypatch.setattr(server, "_conn", lambda: tmp_db)
    exp = to_shadow(tmp_db)
    add_entry(tmp_db, "c1", ts=NOW + HOUR, smart=5, pnl=200)
    add_entry(tmp_db, "i1", ts=NOW + 2 * HOUR, smart=3, pnl=-250)
    loop.run(tmp_db, now=NOW + DAY)

    by_id = {e["experiment_id"]: e for e in server.kaiba_experiments()["experiments"]}

    assert by_id[exp]["status"] == "shadow"
    assert by_id[exp]["latest_verdict"]["gate"] == "replay" and by_id[exp]["latest_verdict"]["passed"] is True
    assert by_id[exp]["loop"]["arms"] == {"candidate": 1, "incumbent": 1}
    assert by_id[exp]["loop"]["chain"] == "robinhood"


def test_a_proposal_is_told_at_once_whether_the_gates_can_judge_it(tmp_db, risk_file, monkeypatch):
    from kaiba.mcp import server

    monkeypatch.setattr(server, "_conn", lambda: tmp_db)
    ok = server.kaiba_propose_experiment("tighter confluence", LANE, {"min_smart_degen": 4})
    bad = server.kaiba_propose_experiment("shorter window", LANE, {"window_s": 60})
    assert ok["judgeable"] is True and ok["status"] == "proposed"
    assert bad["judgeable"] is False and "min_*/max_*" in bad["why"]
