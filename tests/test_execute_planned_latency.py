"""Decision -> fill latency on the live entry path (ops.execute_planned + reconcile).

MEASURED 2026-10-05 on the box (12 days, 227 live buys; 3 days, 124): the job configured at
``interval_s: 5`` actually ran every 10 or 15 s (the scheduler reaps on one tick and checks
due on the next), so decision -> first reservation was median 7.8 s / p90 14.9 s, every
limiter deferral cost another 10-15 s cycle, and a sol fill whose first ``order get`` had
no report waited a cycle to be booked. ``ord_746dd87e`` (EGTvz...pump): sent 8.4 s after the
decision, then six reconciles refused "gmgn: max inflight", booked at 98 s, closed -79%.

Each test below fails with its change reverted:
* drain window removed (single pass)   -> the mid-run plan / deferred plan / late report wait
* default max_plan_age_s back to 300   -> the 31 s plan is sent
* reconcile spacing removed            -> reconcile is called every poll
* query_gmgn_order back to plain guarded -> the reconcile query is refused at once

Network is stubbed at ``_run_gmgn`` (the subprocess boundary), so ``_check_mode`` and
``_authorize`` run for real.
"""

from __future__ import annotations

import time

import pytest
import yaml

from kaiba.core import limiter
from kaiba.core.config import load_risk
from kaiba.core.schemas import Chain, Lane, LaneMode, OrderState
from kaiba.execution import executor
from kaiba.execution.policy import load_policy

WALLET = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
SOL_MINT = "So11111111111111111111111111111111111111112"
TOKEN = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


class FakeTime:
    """One clock for the job (ctx.clock) and its sleeps. ``hooks[n]`` runs on the n-th sleep."""

    def __init__(self) -> None:
        self.t = time.time()
        self.sleeps = 0
        self.hooks: dict[int, object] = {}

    def clock(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps += 1
        self.t += s
        hook = self.hooks.get(self.sleeps)
        if hook:
            hook()


@pytest.fixture
def live(tmp_path, monkeypatch):
    risk = load_risk()
    risk.global_mode = LaneMode.LIVE
    risk.bounds.max_lane_mode = LaneMode.LIVE
    risk.kill_switch = False
    risk.entries_paused = False
    risk.reduce_only = False
    risk.lanes[Lane.SM_TRENCHES].mode = LaneMode.LIVE
    risk.chains[Chain.SOL].wallet = WALLET
    monkeypatch.setattr(executor, "get_risk", lambda: risk)
    policy = load_policy().model_copy(update={"owned_addresses": {"sol": [WALLET]}})
    path = tmp_path / "signer-policy.yaml"
    path.write_text(yaml.safe_dump(policy.model_dump(mode="json")))
    monkeypatch.setenv("KAIBA_SIGNER_POLICY_PATH", str(path))
    return risk


@pytest.fixture
def ft(monkeypatch):
    from kaiba.ops import scheduler as S

    fake = FakeTime()
    monkeypatch.setattr(S.time, "sleep", fake.sleep)
    return fake


def _plan(conn, order_id, created_ms):
    conn.execute(
        "INSERT INTO orders (order_id, decision_id, chain, token, side, lane, mode, "
        "input_token, output_token, amount_in, min_out, slippage_bps, state, provider, "
        "created_ms, updated_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (order_id, "dec_" + order_id, "sol", TOKEN, "buy", "sm-trenches", "live", SOL_MINT,
         TOKEN, "50000000", "1000", 300, "planned", "gmgn", created_ms, created_ms),
    )


def _ctx(conn, fake, timeout_s=120, **params):
    from kaiba.ops import scheduler as S

    cfg = S.load_config(S.config_path())
    now = int(fake.clock() * 1000)
    return S.JobContext(name="execute_planned", conn=conn, params=params, config=cfg,
                        started_ms=now, deadline_ms=now + timeout_s * 1000, clock=fake.clock)


def _cli(sent, order_get=()):
    """gmgn-cli stub: swaps succeed; ``order get`` answers from ``order_get`` in turn."""
    answers = list(order_get)

    def run(args, timeout_s=45, *, mutating=False):
        sent.append(args[0])
        if args[0] == "swap":
            return {"data": {"order_id": "prov_1", "tx_hash": "fixture-tx"}}
        return {"data": answers.pop(0) if answers else {"status": "pending"}}

    return run


def _state(conn, order_id):
    return conn.execute("SELECT state FROM orders WHERE order_id=?", (order_id,)).fetchone()["state"]


# ------------------------------------------------------------------ drain window


def test_a_plan_written_mid_run_is_sent_on_the_next_poll(tmp_db, live, ft, monkeypatch):
    from kaiba.ops import scheduler as S

    sent: list[str] = []
    monkeypatch.setattr(executor, "_run_gmgn", _cli(sent))
    # The engine commits the plan 3 s into the run.
    ft.hooks[3] = lambda: _plan(tmp_db, "ord_mid", int(ft.clock() * 1000))
    out = S.job_execute_planned(_ctx(tmp_db, ft, drain_window_s=10, poll_s=1))
    assert out["submitted"] == ["ord_mid:submitted"], out
    assert sent.count("swap") == 1
    assert out["submit_age_s"]["ord_mid"] <= 1.0, "sent on the next 1 s poll, not a cycle later"
    assert out["passes"] >= 4


def test_a_locally_deferred_plan_is_retried_inside_the_same_run(tmp_db, live, ft, monkeypatch):
    from kaiba.ops import scheduler as S

    sent: list[str] = []
    monkeypatch.setattr(executor, "_run_gmgn", _cli(sent))
    _plan(tmp_db, "ord_def", int(ft.clock() * 1000))
    limiter.reserve("gmgn", "token.info", limiter.Priority.RESEARCH, conn=tmp_db)
    tmp_db.execute("UPDATE provider_state SET inflight=?, last_call_ms=? WHERE provider='gmgn'",
                   (limiter.limits_for("gmgn").max_inflight, int(time.time() * 1000) - 1000))
    # The scanner's calls finish 1 s later.
    ft.hooks[1] = lambda: tmp_db.execute(
        "UPDATE provider_state SET inflight=0, last_call_ms=0 WHERE provider='gmgn'")
    out = S.job_execute_planned(_ctx(tmp_db, ft, drain_window_s=5, poll_s=1))
    assert out["submitted"] == ["ord_def:submitted"], out
    assert out["refused"] == [] or out["refused"][0]["order_id"] == "ord_def"
    assert sent.count("swap") == 1
    events = [r["state"] for r in tmp_db.execute(
        "SELECT state FROM order_events WHERE order_id='ord_def' ORDER BY id")]
    assert events[-1] == "submitted" and "planned" in events[1:], events


def test_a_fill_reported_late_is_booked_inside_the_same_run(tmp_db, live, ft, monkeypatch):
    """The sol shape: the first ``order get`` says processed with no report."""
    from kaiba.ops import scheduler as S

    sent: list[str] = []
    monkeypatch.setattr(executor, "_run_gmgn", _cli(sent, order_get=[
        {"status": "processed"},
        {"status": "confirmed", "report": {"input_amount": "50000000", "output_amount": "123456"}},
    ]))
    _plan(tmp_db, "ord_rep", int(ft.clock() * 1000))
    out = S.job_execute_planned(_ctx(tmp_db, ft, drain_window_s=6, poll_s=1,
                                     reconcile_every_s=2))
    assert _state(tmp_db, "ord_rep") == OrderState.FILLED.value, out
    assert sent.count("order") == 2
    assert out["reconciled"]["ord_rep"] == "filled"


def test_reconcile_is_spaced_and_skipped_when_nothing_is_in_flight(tmp_db, live, ft, monkeypatch):
    from kaiba.ops import scheduler as S

    calls: list[float] = []
    monkeypatch.setattr(executor, "reconcile_all", lambda c=None: calls.append(ft.clock()) or {})
    pending: list[dict] = []
    monkeypatch.setattr(executor, "unresolved_orders", lambda c=None: pending)
    S.job_execute_planned(_ctx(tmp_db, ft, drain_window_s=10, poll_s=1, reconcile_every_s=2))
    assert len(calls) == 1, "nothing in flight: only the first pass reconciles"

    calls.clear()
    pending.append({"order_id": "ord_stuck"})
    S.job_execute_planned(_ctx(tmp_db, ft, drain_window_s=10, poll_s=1, reconcile_every_s=2))
    assert 4 <= len(calls) <= 6, calls  # ~every 2 s over 10 s, not every 1 s poll
    assert all(b - a >= 2 for a, b in zip(calls, calls[1:], strict=False))


def test_the_window_never_runs_into_the_job_timeout(tmp_db, live, ft, monkeypatch):
    from kaiba.ops import scheduler as S

    monkeypatch.setattr(executor, "reconcile_all", lambda c=None: {})
    start = ft.clock()
    out = S.job_execute_planned(_ctx(tmp_db, ft, timeout_s=60, drain_window_s=120, poll_s=1,
                                     submit_headroom_s=50))
    assert ft.clock() - start <= 10.0, "window capped at timeout - headroom"
    assert out["passes"] <= 11


def test_disabling_the_job_stops_a_running_window(tmp_db, live, ft, monkeypatch):
    from kaiba.ops import scheduler as S

    sent: list[str] = []
    monkeypatch.setattr(executor, "_run_gmgn", _cli(sent))
    monkeypatch.setattr(executor, "reconcile_all", lambda c=None: {})
    tmp_db.execute("INSERT INTO ops_jobs (name, enabled, interval_s, timeout_s, updated_ms) "
                   "VALUES ('execute_planned', 1, 1, 120, 0)")

    def operator_disables_then_engine_plans():
        tmp_db.execute("UPDATE ops_jobs SET enabled=0 WHERE name='execute_planned'")
        _plan(tmp_db, "ord_after_disable", int(ft.clock() * 1000))

    ft.hooks[2] = operator_disables_then_engine_plans
    out = S.job_execute_planned(_ctx(tmp_db, ft, drain_window_s=30, poll_s=1))
    assert sent == [] and out["passes"] == 2
    assert _state(tmp_db, "ord_after_disable") == "planned"


def test_drain_window_zero_is_the_old_single_pass(tmp_db, live, ft, monkeypatch):
    from kaiba.ops import scheduler as S

    monkeypatch.setattr(executor, "reconcile_all", lambda c=None: {})
    out = S.job_execute_planned(_ctx(tmp_db, ft))
    assert ft.sleeps == 0 and "passes" not in out


# ------------------------------------------------------------------ staleness guard


def test_default_staleness_bound_is_thirty_seconds(tmp_db, live, ft, monkeypatch):
    from kaiba.ops import scheduler as S

    sent: list[str] = []
    monkeypatch.setattr(executor, "_run_gmgn", _cli(sent))
    monkeypatch.setattr(executor, "reconcile_all", lambda c=None: {})
    now = int(ft.clock() * 1000)
    _plan(tmp_db, "ord_old", now - 31_000)
    _plan(tmp_db, "ord_fresh", now - 29_000)
    out = S.job_execute_planned(_ctx(tmp_db, ft))  # no max_plan_age_s param: the default
    assert out["abandoned_stale"] == ["ord_old"]
    assert out["submitted"] == ["ord_fresh:submitted"]
    assert sent.count("swap") == 1
    assert _state(tmp_db, "ord_old") == OrderState.EXPIRED.value


def test_shipped_schedule_bounds_plan_age_and_drains(tmp_db):
    from kaiba.ops import scheduler as S

    job = S.load_config(S.config_path()).jobs["execute_planned"]
    p = job.params
    assert float(p["max_plan_age_s"]) <= 30
    assert float(p["drain_window_s"]) > 0
    # The window plus a full gmgn-cli timeout must fit inside the job timeout.
    assert job.timeout_s - float(p["submit_headroom_s"]) >= 45
    assert job.interval_s <= 5


def test_a_stale_plan_is_expired_even_when_the_submit_budget_is_spent(tmp_db, live, ft, monkeypatch):
    from kaiba.ops import scheduler as S

    sent: list[str] = []
    monkeypatch.setattr(executor, "_run_gmgn", _cli(sent))
    monkeypatch.setattr(executor, "reconcile_all", lambda c=None: {})
    # The limiter runs on the real clock and this run's sleeps are fake; take it out.
    monkeypatch.setattr(executor, "guarded",
                        lambda *a, **k: __import__("contextlib").nullcontext())
    now = int(ft.clock() * 1000)
    _plan(tmp_db, "ord_a", now - 1_000)
    _plan(tmp_db, "ord_b", now)
    ft.hooks[1] = lambda: _plan(tmp_db, "ord_late", int(ft.clock() * 1000))
    out = S.job_execute_planned(_ctx(tmp_db, ft, max_submits_per_run=2, drain_window_s=40,
                                     poll_s=1, max_plan_age_s=30, submit_headroom_s=0))
    assert sent.count("swap") == 2
    assert "ord_late" in out["abandoned_stale"], "never sent, and never left planned"


# ------------------------------------------------------------------ patient reconcile query


def _saturate(conn):
    limiter.reserve("gmgn", "token.info", limiter.Priority.RESEARCH, conn=conn)
    conn.execute("UPDATE provider_state SET inflight=?, last_call_ms=? WHERE provider='gmgn'",
                 (limiter.limits_for("gmgn").max_inflight, int(time.time() * 1000) - 1000))


def _sent_order():
    o = executor.build_order(decision_id="d", chain=Chain.SOL, token=TOKEN,
                             side=__import__("kaiba.core.schemas", fromlist=["Side"]).Side.BUY,
                             lane=Lane.SM_TRENCHES, mode=LaneMode.LIVE, amount_in=1, min_out=1,
                             slippage_bps=300)
    return o.model_copy(update={"provider_order_id": "prov_1", "state": OrderState.SUBMITTED})


def test_reconcile_query_waits_for_a_free_slot(tmp_db, monkeypatch):
    _saturate(tmp_db)
    clock = {"t": 0.0, "sleeps": 0}

    def sleep(s):
        clock["t"] += s
        clock["sleeps"] += 1
        if clock["sleeps"] == 3:  # another caller's request finishes 0.3 s later
            tmp_db.execute("UPDATE provider_state SET inflight=0 WHERE provider='gmgn'")

    monkeypatch.setattr(executor, "_sleep", sleep)
    monkeypatch.setattr(executor, "_monotonic", lambda: clock["t"])
    monkeypatch.setattr(executor, "_run_gmgn", lambda args, **k: {"data": {"status": "pending"}})
    assert executor.query_gmgn_order(_sent_order(), tmp_db) == {"data": {"status": "pending"}}
    assert clock["sleeps"] == 3


def test_reconcile_query_wait_is_bounded(tmp_db, monkeypatch):
    _saturate(tmp_db)
    clock = {"t": 0.0}
    monkeypatch.setattr(executor, "_sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    monkeypatch.setattr(executor, "_monotonic", lambda: clock["t"])
    with pytest.raises(limiter.RateLimited, match="max inflight"):
        executor.query_gmgn_order(_sent_order(), tmp_db)
    assert clock["t"] <= executor.RECONCILE_SLOT_WAIT_S + 1e-9


@pytest.mark.parametrize("reason", ["provider cooldown", "trade cooldown", "provider returned 429"])
def test_reconcile_query_never_waits_out_a_provider_refusal(tmp_db, monkeypatch, reason):
    from contextlib import contextmanager

    @contextmanager
    def refuse(*a, **k):
        raise limiter.RateLimited("gmgn", reason, 30)
        yield  # pragma: no cover

    slept: list[float] = []
    monkeypatch.setattr(executor, "guarded", refuse)
    monkeypatch.setattr(executor, "_sleep", slept.append)
    with pytest.raises(limiter.RateLimited, match=reason):
        executor.query_gmgn_order(_sent_order(), tmp_db)
    assert slept == []


def test_reconcile_query_never_sleeps_inside_a_caller_transaction(tmp_db, monkeypatch):
    _saturate(tmp_db)
    slept: list[float] = []
    monkeypatch.setattr(executor, "_sleep", slept.append)
    tmp_db.execute("BEGIN")
    try:
        with pytest.raises(limiter.RateLimited, match="max inflight"):
            executor.query_gmgn_order(_sent_order(), tmp_db)
    finally:
        tmp_db.execute("ROLLBACK")
    assert slept == []
