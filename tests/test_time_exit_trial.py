"""Randomized time-exit trial on paper positions (kaiba/learning/time_exit_trial.py).

What matters: the arm is a pure function of the position id (the job, the report and the
twins must agree without storing anything); only TREATED, PAPER, in-window positions past
their hold get one exit request; the request really closes the paper position through
protection; and the treated arm is reported apart from the lane's own row.
"""

from __future__ import annotations

import collections
from decimal import Decimal

import pytest

from kaiba.core import events as ev
from kaiba.core.db import fetch_all
from kaiba.core.schemas import SOL_NATIVE_MINT, EventKind, now_ms
from kaiba.learning import paper_report
from kaiba.learning import time_exit_trial as tet
from tests.test_watchdog import FakeSource, risk_file  # noqa: F401 - fixture, used by name

TOKEN = "EGLpQAhQVB3gN9ZMsgjCBjabf42sPQ3K7T6DRAvy8pr"
LANE = "migration-fade"


def _ids_by_arm(n: int = 40) -> tuple[list[str], list[str]]:
    treat, control = [], []
    for i in range(n):
        pid = f"pos_{i}"
        (treat if tet.arm(pid) == tet.TREAT else control).append(pid)
    return treat, control


def _open(conn, pid, *, opened_ms, mode="shadow", lane=LANE, token=TOKEN):
    conn.execute(
        "INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms, qty, qty_total, cost_native, "
        "proceeds_native, realized_native, entry_price_usd, peak_price_usd) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (pid, "sol", token, lane, mode, opened_ms, "1000000", "1000000", "5000000", "0", "0", "1.0", "1.0"),
    )


def _closed(conn, pid, *, opened_ms, ret, lane=LANE):
    cost = 1_000_000_000
    conn.execute(
        "INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms, closed_ms, qty, qty_total, "
        "cost_native, proceeds_native, realized_native, exit_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (pid, "sol", TOKEN, lane, "shadow", opened_ms, opened_ms + 60_000, "0", "100", str(cost),
         str(int(cost * (1 + ret))), str(int(cost * ret)), "test"),
    )


def _ctx(conn, trials):
    from kaiba.ops import scheduler as S

    now = now_ms()
    return S.JobContext("lane_time_exit", conn, {"trials": trials}, S.ScheduleConfig(), now, now + 20_000)


def _requests(conn) -> list[dict]:
    return [e.payload for e in ev.tail(after_id=0, limit=500, kinds=[EventKind.PROTECTION_TRIGGERED.value], conn=conn)]


# ---------------------------------------------------------------- the arm


def test_the_arm_is_deterministic_and_close_to_half():
    assert tet.arm("pos_x") == tet.arm("pos_x")
    counts = collections.Counter(tet.arm(f"p{i}") for i in range(20_000))
    assert abs(counts[tet.TREAT] - 10_000) < 400


def test_trial_definitions_are_validated_not_guessed():
    trials = tet.trials_from({
        "migration-fade": {"hold_s": 60, "since_ms": 5, "split": "half"},
        "too-short": {"hold_s": 1, "since_ms": 5},
        "bad-split": {"hold_s": 60, "split": "most"},
        "no-hold": {"since_ms": 5},
        "not-a-map": 7,
    })
    assert list(trials) == ["migration-fade"]
    t = trials["migration-fade"]
    assert t.covers(5) and t.covers(10**13) and not t.covers(4)
    assert not tet.Trial(LANE, 60, 0).covers(10**13), "since_ms 0 means not started"
    assert not tet.Trial(LANE, 60, 5, until_ms=100).covers(100)


def test_only_the_treated_arm_inside_the_window_is_treated():
    treat, control = _ids_by_arm()
    trials = {LANE: tet.Trial(LANE, 60, 1_000, until_ms=5_000)}
    assert tet.treated(trials, LANE, treat[0], 2_000) is not None
    assert tet.treated(trials, LANE, control[0], 2_000) is None
    assert tet.treated(trials, LANE, treat[0], 999) is None
    assert tet.treated(trials, LANE, treat[0], 5_000) is None
    assert tet.treated(trials, "sm-trenches", treat[0], 2_000) is None
    everyone = {LANE: tet.Trial(LANE, 60, 1_000, split="all")}
    assert tet.treated(everyone, LANE, control[0], 2_000) is not None


def test_an_unstarted_trial_relabels_and_hides_nothing():
    """since_ms 0 is the shipped state: no position may move to the trial row or leave the twins."""
    treat, _ = _ids_by_arm()
    trials = {LANE: tet.Trial(LANE, 60, 0)}
    assert tet.treated(trials, LANE, treat[0], 10**13) is None
    assert tet.label_for(trials, LANE, treat[0], 10**13) == LANE


# ---------------------------------------------------------------- the job


def test_the_job_requests_one_exit_per_treated_paper_position_past_its_hold(tmp_db):
    from kaiba.ops import scheduler as S

    treat, control = _ids_by_arm()
    now = now_ms()
    since = now - 3_600_000
    _open(tmp_db, treat[0], opened_ms=now - 90_000)                 # treated, past 60 s: exit
    _open(tmp_db, treat[1], opened_ms=now - 10_000)                 # treated, too young: not yet
    _open(tmp_db, control[0], opened_ms=now - 90_000)               # control arm: never
    _open(tmp_db, treat[2], opened_ms=since - 60_000)               # opened before the trial: never
    _open(tmp_db, treat[3], opened_ms=now - 90_000, mode="live")    # live: never, by construction
    _open(tmp_db, treat[4], opened_ms=now - 90_000, lane="sm-trenches")  # not a trial lane
    tmp_db.commit()
    trials = {LANE: {"hold_s": 60, "since_ms": since, "split": "half"}}

    out = S.job_lane_time_exit(_ctx(tmp_db, trials))
    assert out["requested"] == 1
    reqs = _requests(tmp_db)
    assert [r["position_id"] for r in reqs] == [treat[0]]
    assert reqs[0]["reason"] == "time_exit:migration-fade:60s" and reqs[0]["pct"] == 100
    assert S.job_lane_time_exit(_ctx(tmp_db, trials))["requested"] == 0, "one request per position"


def test_a_trial_that_has_not_started_does_nothing(tmp_db):
    from kaiba.ops import scheduler as S

    treat, _ = _ids_by_arm()
    _open(tmp_db, treat[0], opened_ms=now_ms() - 90_000)
    tmp_db.commit()
    out = S.job_lane_time_exit(_ctx(tmp_db, {LANE: {"hold_s": 60, "since_ms": 0}}))
    assert out["requested"] == 0 and _requests(tmp_db) == []


@pytest.mark.usefixtures("risk_file")
def test_the_request_closes_the_paper_position_through_protection(tmp_db):
    """End to end: the job's request is drained by protection and sold on the paper broker."""
    from kaiba.execution import watchdog as wd
    from kaiba.ops import scheduler as S

    treat, _ = _ids_by_arm()
    now = now_ms()
    _open(tmp_db, treat[0], opened_ms=now - 90_000)
    tmp_db.commit()
    S.job_lane_time_exit(_ctx(tmp_db, {LANE: {"hold_s": 60, "since_ms": now - 3_600_000}}))

    source = FakeSource({TOKEN: "1.02", SOL_NATIVE_MINT: "200"})
    dog = wd.Watchdog(tmp_db, price_source=source, submitter=wd.DefaultExitSubmitter(tmp_db, source))
    assert dog.tick().exits == 1
    row = fetch_all(tmp_db, "SELECT closed_ms, exit_reason FROM positions WHERE position_id = ?", (treat[0],))[0]
    assert row["closed_ms"] is not None and "time_exit:migration-fade:60s" in (row["exit_reason"] or "")


# ---------------------------------------------------------------- the report


def test_treated_positions_get_their_own_scoreboard_row(tmp_db):
    treat, control = _ids_by_arm()
    now = now_ms()
    since = now - 86_400_000
    _closed(tmp_db, treat[0], opened_ms=since + 1_000, ret=0.10)
    _closed(tmp_db, control[0], opened_ms=since + 1_000, ret=-0.05)
    _closed(tmp_db, treat[1], opened_ms=since - 1_000, ret=0.20)    # before the trial: the lane's row
    tmp_db.commit()
    trials = {LANE: tet.Trial(LANE, 60, since)}
    lanes = collections.Counter(r[0] for r in paper_report.paper_rows(tmp_db, now, trials=trials))
    assert lanes == {"migration-fade [exit 60s]": 1, "migration-fade": 2}


def test_trial_stats_compare_the_arms_and_pass_only_when_both_bars_clear(tmp_db):
    treat, control = _ids_by_arm(400)
    now = now_ms()
    since = now - 86_400_000
    for i, pid in enumerate(treat[:60]):
        _closed(tmp_db, pid, opened_ms=since + 1_000 + i, ret=0.08 + (i % 5) * 0.01)
    for i, pid in enumerate(control[:60]):
        _closed(tmp_db, pid, opened_ms=since + 1_000 + i, ret=-0.03 + (i % 5) * 0.01)
    tmp_db.commit()
    (row,) = paper_report.trial_stats(tmp_db, now, {LANE: tet.Trial(LANE, 60, since)})
    assert row["n_treat"] == 60 and row["n_control"] == 60
    assert row["mean_treat"] == pytest.approx(0.10) and row["mean_control"] == pytest.approx(-0.01)
    assert row["verdict"] == "PASS" and row["diff_low90"] > 0
    lines = paper_report.trial_lines([row])
    assert any("exit 60s" in line and "PASS" in line for line in lines)


def test_a_timed_arm_that_only_loses_less_does_not_pass(tmp_db):
    treat, control = _ids_by_arm(400)
    now = now_ms()
    since = now - 86_400_000
    for i, pid in enumerate(treat[:60]):
        _closed(tmp_db, pid, opened_ms=since + 1_000 + i, ret=-0.02 - (i % 3) * 0.01)
    for i, pid in enumerate(control[:60]):
        _closed(tmp_db, pid, opened_ms=since + 1_000 + i, ret=-0.20 - (i % 3) * 0.01)
    tmp_db.commit()
    (row,) = paper_report.trial_stats(tmp_db, now, {LANE: tet.Trial(LANE, 60, since)})
    assert row["diff_low90"] > 0 and row["verdict"] == "FAIL", "beating a loser is not making money"


def test_the_shipped_trial_is_consistent():
    """Started 2026-10-18 with the pre-declared 60 s. An enabled trial without a start time
    would request nothing (and look like it was running); the hold must stay the declared one."""
    from kaiba.ops import scheduler as S

    job = S.load_config().jobs["lane_time_exit"]
    trial = tet.load_trials()[LANE]
    assert Decimal(trial.hold_s) == Decimal(60) and trial.split == "half"
    if job.enabled:
        assert trial.since_ms > 0, "an enabled trial needs since_ms"
