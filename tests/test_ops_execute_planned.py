"""The entry path: ops.execute_planned drains `planned` orders to the executor.

These tests exist because the gap they cover was invisible for as long as the box was
armed. ``engine.handoff`` wrote a ``planned`` row for every live decision and stopped;
nothing read those rows back, so entries could never reach a venue while exits could.
A job that submits orders deserves tests that assert what it *refuses* to do, so the
cases below are weighted that way: one happy path, three refusals.

STUB DEPTH MATTERS HERE. The first version of this file stubbed ``executor.submit_gmgn``,
which sits *above* the ``_check_mode`` call at executor.py:382 -- so the shadow-lane test
"failed" by sending an order that production would have refused. The stub had removed the
gate it was meant to be testing. Everything below stubs ``_run_gmgn``, the subprocess
boundary, so ``_check_mode`` and ``_authorize`` run for real in every test. When you mock
a safety property, mock the network, never the check.
"""

from __future__ import annotations

import time

from kaiba.core.schemas import LaneMode, OrderState

FAKE_GMGN_OK = {"data": {"order_id": "prov_test_1", "tx_hash": "0xdeadbeef"}}

SOL_MINT = "So11111111111111111111111111111111111111112"
# A distinct output mint. The first draft used wSOL on both sides and the signer policy
# refused it with `gmgn_body_input_equals_output` -- the policy catching a malformed test,
# which is the check working. A swap fixture needs two different tokens.
TARGET_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def _plan(conn, *, order_id="ord_test1", lane="migration-fade", mode="live",
          created_ms=None, amount="77777786", min_out="1000000",
          state=OrderState.PLANNED.value):
    now = created_ms if created_ms is not None else int(time.time() * 1000)
    conn.execute(
        "INSERT INTO orders (order_id, decision_id, chain, token, side, lane, mode, "
        "input_token, output_token, amount_in, min_out, slippage_bps, state, provider, "
        "created_ms, updated_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (order_id, "dec_" + order_id, "sol", TARGET_MINT,
         "buy", lane, mode, SOL_MINT,
         TARGET_MINT, amount, min_out, 2500,
         state, "gmgn", now, now),
    )
    conn.commit()
    return order_id


def _ctx(conn, monkeypatch, **params):
    from kaiba.ops import scheduler as S

    cfg = S.load_config(S.config_path())
    now = int(time.time() * 1000)
    base = {"max_plan_age_s": 300, "max_submits_per_run": 3}
    base.update(params)
    # reconcile_all reaches a provider; these tests are about the submit decision, so the
    # network half is stubbed. The submit *gates* are never stubbed.
    monkeypatch.setattr("kaiba.execution.executor.reconcile_all", lambda c=None: {})
    return S.JobContext(name="execute_planned", conn=conn, params=base, config=cfg,
                        started_ms=now, deadline_ms=now + 120_000)


def _stub_network(monkeypatch, sent: list[str]) -> None:
    """Replace only the subprocess call, so every gate above it still executes."""
    def _fake_run(args, timeout_s=45, *, mutating=False):
        sent.append(" ".join(args))
        return FAKE_GMGN_OK

    monkeypatch.setattr("kaiba.execution.executor._run_gmgn", _fake_run)
    monkeypatch.setattr("kaiba.execution.executor.guarded",
                        lambda *a, **k: __import__("contextlib").nullcontext())


def _live_risk(monkeypatch):
    """A risk config with migration-fade live, patched where the executor reads it."""
    from kaiba.core.config import load_risk

    risk = load_risk()
    lanes = dict(risk.lanes)
    lanes["migration-fade"] = lanes["migration-fade"].model_copy(update={"mode": LaneMode.LIVE})
    bounds = risk.bounds.model_copy(update={"max_lane_mode": LaneMode.LIVE})
    patched = risk.model_copy(update={"lanes": lanes, "bounds": bounds,
                                      "global_mode": LaneMode.LIVE, "kill_switch": False,
                                      "reduce_only": False, "entries_paused": False})
    monkeypatch.setattr("kaiba.execution.executor.get_risk", lambda: patched)
    return patched


def test_shadow_lane_order_is_refused_not_sent(tmp_db, monkeypatch):
    """The whole safety story in one test.

    ``executor.submit`` re-reads the risk config and refuses a buy on a shadow lane. If
    this ever passes by *sending*, the job has become a way to bypass the lane mode,
    which is the one thing it must never be.
    """
    from kaiba.core.config import load_risk
    from kaiba.ops import scheduler as S

    # Build the shadow lane explicitly instead of trusting the repo's risk.yaml. The
    # first version used `load_risk` bare, so the test asserted "shadow" against whatever
    # config happened to be checked in -- it passed only while migration-fade was shadow
    # there, and failed the moment the operator armed it. A safety test must not depend
    # on ambient configuration for the condition it is testing.
    base = load_risk()
    lanes = dict(base.lanes)
    lanes["migration-fade"] = lanes["migration-fade"].model_copy(
        update={"mode": LaneMode.SHADOW}
    )
    shadowed = base.model_copy(update={
        "lanes": lanes, "global_mode": LaneMode.LIVE, "kill_switch": False,
        "bounds": base.bounds.model_copy(update={"max_lane_mode": LaneMode.LIVE}),
    })
    monkeypatch.setattr("kaiba.execution.executor.get_risk", lambda: shadowed)
    sent: list[str] = []
    _stub_network(monkeypatch, sent)

    _plan(tmp_db, order_id="ord_shadow", lane="migration-fade", mode="live")
    out = S.job_execute_planned(_ctx(tmp_db, monkeypatch))

    assert sent == [], "a shadow lane must never reach the venue"
    assert out["submitted"] == []
    assert len(out["refused"]) == 1
    assert "shadow" in out["refused"][0]["reason"].lower()


def test_kill_switch_refuses_entries(tmp_db, monkeypatch):
    """A kill switch that does not stop entries is decoration.

    Forcing the lane live does NOT isolate the switch, and the earlier version of this
    test assumed it did. ``Risk.effective_mode`` short-circuits to ``OFF`` whenever
    ``kill_switch`` is set, so the lane check at executor.py:121 raises first and the
    reason reads "lane ... is off" -- never "kill switch engaged". The switch is doing
    its job *through* the lane clamp, which is stronger than a single explicit check,
    but it means asserting on the word "kill" tests a branch that cannot be reached.

    Consequence worth knowing: ``if risk.kill_switch: raise ... ("kill switch engaged")``
    at executor.py:123 is UNREACHABLE for a buy. Same outcome, so nothing is unsafe, but
    it reads as the control and is not the control. Left in place deliberately as
    defence in depth if ``effective_mode`` ever stops clamping; this test pins the
    behaviour that actually protects us.
    """
    from kaiba.ops import scheduler as S

    risk = _live_risk(monkeypatch)
    killed = risk.model_copy(update={"kill_switch": True})
    monkeypatch.setattr("kaiba.execution.executor.get_risk", lambda: killed)
    sent: list[str] = []
    _stub_network(monkeypatch, sent)

    _plan(tmp_db, order_id="ord_killed", lane="migration-fade", mode="live")
    out = S.job_execute_planned(_ctx(tmp_db, monkeypatch))

    assert sent == [], "kill switch must stop an entry"
    assert len(out["refused"]) == 1

    # What the switch actually does, verified rather than assumed.
    from kaiba.core.schemas import Lane

    assert killed.effective_mode(Lane.MIGRATION_FADE) is LaneMode.OFF, (
        "the kill switch must clamp every lane to off; that is the mechanism"
    )
    assert "no live submission" in out["refused"][0]["reason"]

    # And prove it was the SWITCH, not the lane's own mode: the same config with the
    # switch released must let the identical order through.
    released = killed.model_copy(update={"kill_switch": False})
    assert released.effective_mode(Lane.MIGRATION_FADE) is LaneMode.LIVE
    monkeypatch.setattr("kaiba.execution.executor.get_risk", lambda: released)
    _plan(tmp_db, order_id="ord_released", lane="migration-fade", mode="live")
    out2 = S.job_execute_planned(_ctx(tmp_db, monkeypatch))
    assert out2["submitted"], f"released switch must allow the entry: {out2}"
    assert sent, "the released order should have reached the (stubbed) venue"


def test_engine_planned_min_out_of_zero_is_refused_by_policy(tmp_db, monkeypatch):
    """REGRESSION PIN for the signer half of a live blocker found 2026-09-21.

    History, because the name of this test only makes sense with it. When it was written,
    ``engine._plan_live_order`` hardcoded ``min_out=0`` (engine.py:635); that reached the
    signer policy as ``min_output_amount="0"``, ``policy._positive_amount`` rejected zero,
    and so **every** live order the engine planned was refused with
    ``gmgn_body_amount_invalid:min_output_amount`` -- the third independent blocker in the
    entry path, after the score-ladder scale mismatch and the missing planned-order
    consumer. 110 decisions, 0 live orders.

    The engine side was fixed on 2026-09-21: ``_plan_live_order`` now derives the floor
    from the dossier price, a contemporaneous native/USD sample and chain-verified token
    decimals, and *refuses to plan the order at all* when any of those is unavailable.
    ``tests/test_engine_min_out.py`` pins that, including the end-to-end path where the
    body handed to this same policy is built by ``executor.gmgn_swap_body`` from an order
    the engine really planned.

    This test keeps its original intent unchanged and is deliberately still here: it pins
    the **policy**, which is the last line of defence and the half that was never wrong.
    ``min_out=0`` means "accept any output", i.e. an unbounded-slippage market order,
    precisely the shape a sandwich bot wants. The fix belonged in the engine, never in the
    policy -- so this test fails the day someone "fixes" a future blockage by loosening
    the check instead.
    """
    from kaiba.execution.policy import check_gmgn_swap_body
    from kaiba.core.schemas import Chain

    wallet = "62EbtFbmJATT94Y6Uguf6a1kdaJuZWAu8ycqbg3xKfQg"
    body = {
        "chain": "sol", "from_address": wallet,
        "input_token": SOL_MINT, "output_token": TARGET_MINT,
        "input_amount": "77777786", "min_output_amount": "0",
        "swap_mode": "ExactIn", "slippage": "25.00", "auto_slippage": False,
    }
    verdict = check_gmgn_swap_body(body, wallet=wallet, chain=Chain.SOL)
    assert verdict.allowed is False, "min_out=0 is unbounded slippage and must be refused"
    assert verdict.reason == "gmgn_body_amount_invalid:min_output_amount"

    body["min_output_amount"] = "1000000"
    assert check_gmgn_swap_body(body, wallet=wallet, chain=Chain.SOL).allowed is True


def test_stale_plan_is_abandoned_rather_than_submitted(tmp_db, monkeypatch):
    """An old plan was sized against a book that no longer exists."""
    from kaiba.ops import scheduler as S

    _live_risk(monkeypatch)
    sent: list[str] = []
    _stub_network(monkeypatch, sent)

    old = int(time.time() * 1000) - 3_600_000  # an hour old
    _plan(tmp_db, order_id="ord_stale", created_ms=old)
    out = S.job_execute_planned(_ctx(tmp_db, monkeypatch, max_plan_age_s=300))

    assert sent == []
    assert out["abandoned_stale"] == ["ord_stale"]
    row = tmp_db.execute("SELECT state, error FROM orders WHERE order_id=?", ("ord_stale",)).fetchone()
    assert row["state"] == OrderState.EXPIRED.value
    assert "stale" in (row["error"] or "")


def test_live_lane_order_is_submitted(tmp_db, monkeypatch):
    """The happy path: a live lane, a fresh plan, and the order reaches the venue."""
    from kaiba.ops import scheduler as S

    _live_risk(monkeypatch)
    sent: list[str] = []
    _stub_network(monkeypatch, sent)

    _plan(tmp_db, order_id="ord_live", lane="migration-fade", mode="live")
    out = S.job_execute_planned(_ctx(tmp_db, monkeypatch))

    assert len(sent) == 1, f"live lane should submit; refused={out['refused']}"
    assert "swap" in sent[0]
    assert out["submitted"] == ["ord_live:submitted"]
    row = tmp_db.execute("SELECT state, tx_hash FROM orders WHERE order_id=?", ("ord_live",)).fetchone()
    assert row["state"] == OrderState.SUBMITTED.value
    assert row["tx_hash"] == "0xdeadbeef"


def test_burst_bound_caps_submissions_per_run(tmp_db, monkeypatch):
    """If a bug ever plans a hundred orders, this decides it is not a hundred trades."""
    from kaiba.ops import scheduler as S

    _live_risk(monkeypatch)
    sent: list[str] = []
    _stub_network(monkeypatch, sent)

    for i in range(10):
        _plan(tmp_db, order_id=f"ord_burst{i}", lane="migration-fade", mode="live")
    out = S.job_execute_planned(_ctx(tmp_db, monkeypatch, max_submits_per_run=3))

    assert len(sent) == 3, f"burst bound not enforced: sent {len(sent)}"
    assert len(out["submitted"]) == 3


def test_job_is_registered_and_governed_by_the_risk_gate():
    """Scheduling it must not be what arms trading; the risk gate must be.

    This test used to assert ``enabled is False``. That assertion was right for as long as
    nobody had made the call, and it fired on 2026-09-22 when the owner did: shown the
    measured live expectancy (54 closed fills, -19.3% mean, 19% win, confidence
    anti-calibrated) they chose to trade live anyway while entry quality is rebuilt. The
    decision is recorded in the ``playbook`` table as ``owner_directive_sm_trenches_live``.

    So the flag's value is no longer the invariant. What still has to be true -- and what
    made enabling it safe rather than reckless -- is that this job cannot widen risk on its
    own: ``executor.submit`` re-reads the risk config at submit time and refuses when the
    lane is off or shadow, the kill switch is set, reduce-only is on, or entries are
    paused. A flag in ``schedule.yaml`` is a throttle on a path the gate still owns.
    """
    import inspect

    from kaiba.execution import executor as E
    from kaiba.ops import scheduler as S

    assert "execute_planned" in S.JOBS
    cfg = S.load_config(S.config_path())
    assert "execute_planned" in cfg.jobs

    source = inspect.getsource(E._check_mode)
    for guard in ("kill_switch", "entries_paused", "reduce_only"):
        assert guard in source, (
            f"_check_mode no longer consults {guard}; enabling execute_planned would then "
            f"genuinely arm trading by itself"
        )
