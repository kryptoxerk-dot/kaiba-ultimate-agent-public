"""Local backpressure is not a sent trade. All state and requests here are fixtures."""
from __future__ import annotations

import time

import pytest
import yaml

from kaiba.core import limiter
from kaiba.core.config import load_risk
from kaiba.core.schemas import Chain, Lane, LaneMode, OrderState, Side
from kaiba.execution import executor
from kaiba.execution.policy import load_policy

WALLET = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
TOKEN = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


@pytest.fixture
def live_context(tmp_path, monkeypatch):
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
    sent = []

    def cli(args, timeout_s=45, *, mutating=False):
        sent.append(args)
        return {"data": {"order_id": "fixture-provider-order", "tx_hash": "fixture-tx"}}

    monkeypatch.setattr(executor, "_run_gmgn", cli)
    monkeypatch.setattr(executor, "reconcile_all", lambda c=None: {})
    return risk, sent


def order(side=Side.BUY):
    return executor.build_order(
        decision_id="fixture-entry", chain=Chain.SOL, token=TOKEN, side=side,
        lane=Lane.SM_TRENCHES, mode=LaneMode.LIVE, amount_in=50_000_000,
        min_out=1000, slippage_bps=300,
    )


def saturate(conn):
    limiter.reserve("gmgn", "token.info", limiter.Priority.RESEARCH, conn=conn)
    conn.execute(
        "UPDATE provider_state SET inflight=?,last_call_ms=? WHERE provider='gmgn'",
        (limiter.limits_for("gmgn").max_inflight, int(time.time()*1000)-1000),
    )


def test_unsent_buy_survives_local_slot_contention(tmp_db, live_context):
    _, sent = live_context
    plan = order()
    saturate(tmp_db)
    with pytest.raises(limiter.RateLimited, match="max inflight"):
        executor.submit_gmgn(plan, tmp_db)
    row = tmp_db.execute("SELECT * FROM orders WHERE order_id=?", (plan.order_id,)).fetchone()
    assert sent == []
    assert row["state"] == OrderState.PLANNED.value
    assert row["created_ms"] == plan.created_ms
    assert row["provider_order_id"] is None and row["tx_hash"] is None
    assert not tmp_db.execute(
        "SELECT 1 FROM order_events WHERE order_id=? AND state='submitting'", (plan.order_id,)
    ).fetchone(), "reservation failure must not claim the request was submitting"


def consumer(conn, monkeypatch):
    from kaiba.ops import scheduler
    cfg = scheduler.load_config(scheduler.config_path())
    now = int(time.time()*1000)
    return scheduler.job_execute_planned(scheduler.JobContext(
        name="execute_planned", conn=conn,
        params={"max_plan_age_s": 300, "max_submits_per_run": 3},
        config=cfg, started_ms=now, deadline_ms=now+120_000,
    ))


def release_local_pressure(conn):
    conn.execute("UPDATE provider_state SET inflight=0,last_call_ms=0 WHERE provider='gmgn'")


def deferred_plan(conn):
    plan = order()
    saturate(conn)
    with pytest.raises(limiter.RateLimited, match="max inflight"):
        executor.submit_gmgn(plan, conn)
    return plan


def test_existing_consumer_retries_same_fresh_plan_once(tmp_db, live_context, monkeypatch):
    _, sent = live_context
    plan = deferred_plan(tmp_db)
    assert consumer(tmp_db, monkeypatch)["submitted"] == []
    assert sent == []
    release_local_pressure(tmp_db)
    result = consumer(tmp_db, monkeypatch)
    assert result["submitted"] == [f"{plan.order_id}:submitted"]
    assert len(sent) == 1
    consumer(tmp_db, monkeypatch)
    assert len(sent) == 1, "submitted orders must not re-enter the planned consumer"
    row = tmp_db.execute("SELECT * FROM orders WHERE order_id=?", (plan.order_id,)).fetchone()
    assert row["created_ms"] == plan.created_ms
    assert row["amount_in"] == str(plan.amount_in)
    assert row["min_out"] == str(plan.min_out)
    assert row["slippage_bps"] == plan.slippage_bps


def test_deferred_plan_expires_from_original_creation_time(tmp_db, live_context, monkeypatch):
    _, sent = live_context
    plan = deferred_plan(tmp_db)
    tmp_db.execute("UPDATE orders SET created_ms=? WHERE order_id=?",
                   (plan.created_ms-301_000, plan.order_id))
    release_local_pressure(tmp_db)
    result = consumer(tmp_db, monkeypatch)
    assert result["abandoned_stale"] == [plan.order_id]
    assert sent == []


@pytest.mark.parametrize("flag", ["entries_paused", "reduce_only", "kill_switch"])
def test_deferred_plan_rechecks_entry_controls(tmp_db, live_context, monkeypatch, flag):
    risk, sent = live_context
    deferred_plan(tmp_db)
    release_local_pressure(tmp_db)
    setattr(risk, flag, True)
    result = consumer(tmp_db, monkeypatch)
    assert result["submitted"] == [] and result["refused"]
    assert sent == []


def test_deferred_plan_rechecks_lane_mode(tmp_db, live_context, monkeypatch):
    risk, sent = live_context
    deferred_plan(tmp_db)
    release_local_pressure(tmp_db)
    risk.lanes[Lane.SM_TRENCHES].mode = LaneMode.SHADOW
    assert consumer(tmp_db, monkeypatch)["submitted"] == []
    assert sent == []


def test_deferred_plan_revalidates_swap_body(tmp_db, live_context, monkeypatch):
    _, sent = live_context
    plan = deferred_plan(tmp_db)
    release_local_pressure(tmp_db)
    # GMGN validates its exact custodial swap body; direct-signer owned_addresses
    # are a different gate. Corrupt the output floor to exercise the actual policy.
    tmp_db.execute("UPDATE orders SET min_out='0' WHERE order_id=?", (plan.order_id,))
    result = consumer(tmp_db, monkeypatch)
    assert result["submitted"] == [] and result["refused"]
    assert "gmgn_body_amount_invalid:min_output_amount" in result["refused"][0]["reason"]
    assert sent == []


@pytest.mark.parametrize("reason", ["minimum interval", "bucket exhausted"])
def test_other_local_transient_limits_defer_only_before_send(
    tmp_db, live_context, monkeypatch, reason
):
    _, sent = live_context
    plan = order()
    limiter.reserve("gmgn", "token.info", limiter.Priority.RESEARCH, conn=tmp_db)
    fixed = int(time.time()*1000)
    monkeypatch.setattr(limiter, "now_ms", lambda: fixed)
    if reason == "minimum interval":
        tmp_db.execute("UPDATE provider_state SET last_call_ms=? WHERE provider='gmgn'", (fixed,))
    else:
        tmp_db.execute("UPDATE provider_state SET last_call_ms=0,credit_milli=0,last_refill_ms=? "
                       "WHERE provider='gmgn'", (fixed,))
    with pytest.raises(limiter.RateLimited, match=reason):
        executor.submit_gmgn(plan, tmp_db)
    assert sent == []
    row = tmp_db.execute("SELECT state FROM orders WHERE order_id=?", (plan.order_id,)).fetchone()
    assert row["state"] == "planned"


@pytest.mark.parametrize("reason", ["provider returned 429", "max inflight"])
def test_a_failure_from_the_cli_is_never_requeued(tmp_db, live_context, monkeypatch, reason):
    _, sent = live_context
    def refused(args, **kwargs):
        sent.append(args)
        raise limiter.RateLimited("gmgn", reason, 300)
    monkeypatch.setattr(executor, "_run_gmgn", refused)
    plan = order()
    with pytest.raises(limiter.RateLimited):
        executor.submit_gmgn(plan, tmp_db)
    assert tmp_db.execute("SELECT state FROM orders WHERE order_id=?",
                          (plan.order_id,)).fetchone()["state"] == "failed"
    consumer(tmp_db, monkeypatch)
    assert len(sent) == 1


def test_ambiguous_send_stays_unknown_and_is_never_requeued(tmp_db, live_context, monkeypatch):
    _, sent = live_context
    def ambiguous(args, **kwargs):
        sent.append(args)
        raise executor.ExecutionAmbiguous("fixture timeout after possible send")
    monkeypatch.setattr(executor, "_run_gmgn", ambiguous)
    plan = order()
    with pytest.raises(executor.ExecutionAmbiguous):
        executor.submit_gmgn(plan, tmp_db)
    assert tmp_db.execute("SELECT state FROM orders WHERE order_id=?",
                          (plan.order_id,)).fetchone()["state"] == "unknown"
    consumer(tmp_db, monkeypatch)
    assert len(sent) == 1


def test_sell_backpressure_keeps_existing_watchdog_retry_semantics(tmp_db, live_context, monkeypatch, kaiba_bought):
    _, sent = live_context
    plan = order(Side.SELL)
    kaiba_bought(tmp_db, plan.chain, plan.token)  # a live sell sells what Kaiba bought
    fixed = int(time.time()*1000)
    monkeypatch.setattr(limiter, "now_ms", lambda: fixed)
    limiter.reserve("gmgn", "token.info", limiter.Priority.RESEARCH, conn=tmp_db)
    with pytest.raises(limiter.RateLimited, match="minimum interval"):
        executor.submit_gmgn(plan, tmp_db)
    assert sent == []
    assert tmp_db.execute("SELECT state FROM orders WHERE order_id=?",
                          (plan.order_id,)).fetchone()["state"] == "failed"


@pytest.mark.parametrize("changes", [
    {"state": OrderState.UNKNOWN},
    {"state": OrderState.SUBMITTED},
    {"provider_order_id": "existing-venue-order"},
    {"tx_hash": "existing-transaction"},
])
def test_preexisting_send_identity_or_state_is_not_requeued(tmp_db, live_context, changes):
    _, sent = live_context
    plan = order().model_copy(update=changes)
    saturate(tmp_db)
    with pytest.raises(limiter.RateLimited, match="max inflight"):
        executor.submit_gmgn(plan, tmp_db)
    row = tmp_db.execute("SELECT state,provider_order_id,tx_hash FROM orders WHERE order_id=?",
                         (plan.order_id,)).fetchone()
    assert row["state"] != "planned"
    assert row["provider_order_id"] == plan.provider_order_id
    assert row["tx_hash"] == plan.tx_hash
    assert sent == []


@pytest.mark.parametrize("reason", ["provider cooldown", "trade cooldown", "daily credit cap"])
def test_nonallowlisted_reservation_refusal_is_not_requeued(
    tmp_db, live_context, monkeypatch, reason
):
    from contextlib import contextmanager
    _, sent = live_context
    @contextmanager
    def refuse(*args, **kwargs):
        raise limiter.RateLimited("gmgn", reason, 300)
        yield  # pragma: no cover - reservation is denied before the body
    monkeypatch.setattr(executor, "guarded", refuse)
    plan = order()
    with pytest.raises(limiter.RateLimited, match=reason):
        executor.submit_gmgn(plan, tmp_db)
    assert tmp_db.execute("SELECT state FROM orders WHERE order_id=?",
                          (plan.order_id,)).fetchone()["state"] == "failed"
    assert sent == []


def test_historical_failed_order_is_not_reactivated(tmp_db, live_context, monkeypatch):
    _, sent = live_context
    plan = order().model_copy(update={"state": OrderState.FAILED, "error": "old local refusal"})
    executor._persist(plan, tmp_db)
    assert consumer(tmp_db, monkeypatch)["planned_seen"] == 0
    assert sent == []
