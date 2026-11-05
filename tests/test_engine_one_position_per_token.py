"""One live holding per token.

MEASURED on the box 2026-11-04: with ``min_smart_degen`` lowered to 3 the same token fires
again within minutes. Two tokens took 0.3 + 0.3 SOL, 6-7 minutes apart, on an owner ticket
of 0.3. The paper path has always refused a second twin on a token; live had no such rule.
These tests pin the rule on real rows, and a retry and a later re-entry stay allowed.
"""

from __future__ import annotations

import sqlite3

import pytest

from kaiba.core.config import load_risk, save_risk
from kaiba.core.schemas import Action, Chain, Lane, LaneMode, Signal
from kaiba.execution import engine, lanes

TOKEN = "6d27YXn6Lct1dqRw4oYvVTRtikHSh2Qjp7BYjz2FozRm"
OTHER = "DDmGnuGs6M8auzbHmyW3tki8WbZh2rnbb3bS4eiyUHed"
BLOCKER = engine.ALREADY_HOLDING


@pytest.fixture
def arm(tmp_path, monkeypatch):
    def _arm(lane: Lane, mode: LaneMode):
        cfg = load_risk()
        cfg.global_mode = LaneMode.LIVE
        cfg.kill_switch = False
        cfg.lanes[lane].mode = mode
        path = tmp_path / "risk.yaml"
        save_risk(cfg, path)
        monkeypatch.setenv("KAIBA_RISK_PATH", str(path))
        assert engine.get_risk().effective_mode(lane, Chain.SOL) is mode, "fixture could not arm the lane"

    return _arm


def _signal(lane: Lane = Lane.SM_TRENCHES, token: str = TOKEN) -> Signal:
    ts = engine.now_ms()
    return Signal(signal_id=lanes.signal_id_for(lane, Chain.SOL, token, ts, 1800), lane=lane, chain=Chain.SOL,
                  token=token, strength=0.9, reasons=["5 smart wallets"], window_s=1800, created_ms=ts, payload={})


def _position(conn, *, token=TOKEN, lane="sm-trenches", mode="live", closed=False):
    now = engine.now_ms()
    conn.execute(
        "INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms, closed_ms, qty, qty_total, cost_native, proceeds_native) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (f"pos_{token[:4]}_{mode}_{lane}", "sol", token, lane, mode, now - 60_000, now if closed else None,
         "0" if closed else "1", "1", "300000000", "0"),
    )


def _order(conn, state, *, token=TOKEN, side="buy", mode="live"):
    conn.execute(
        "INSERT INTO orders (order_id, chain, token, side, lane, mode, input_token, output_token, amount_in, min_out, slippage_bps, "
        "state, provider, created_ms, updated_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"ord_{state}_{side}_{mode}", "sol", token, side, "sm-trenches", mode, "SOL", token, "300000000", "1", 2500, state, "gmgn",
         engine.now_ms(), engine.now_ms()),
    )


def test_a_second_buy_on_an_open_live_position_is_refused(tmp_db, arm):
    arm(Lane.SM_TRENCHES, LaneMode.LIVE)
    _position(tmp_db)
    d = engine.decide(_signal(), tmp_db)
    assert d.action is Action.SKIP
    assert d.blockers == [BLOCKER]


def test_the_refusal_is_on_the_decision_row(tmp_db, arm):
    arm(Lane.SM_TRENCHES, LaneMode.LIVE)
    _position(tmp_db)
    d = engine.decide(_signal(), tmp_db)
    row = tmp_db.execute("SELECT action, blockers_json FROM decisions WHERE decision_id=?", (d.decision_id,)).fetchone()
    assert row["action"] == "skip"
    assert BLOCKER in row["blockers_json"]


def test_a_moonbag_still_counts_as_holding(tmp_db, arm):
    """A position that took profit is still open, and buying it again would be the same add."""
    arm(Lane.SM_TRENCHES, LaneMode.LIVE)
    _position(tmp_db)
    tmp_db.execute("UPDATE positions SET proceeds_native='262167718', tp_done_json='[\"moonbag\"]'")
    assert BLOCKER in engine.decide(_signal(), tmp_db).blockers


def test_another_lanes_live_position_counts(tmp_db, arm):
    """The wallet holds one balance of the token, whichever lane bought it."""
    arm(Lane.SM_TRENCHES, LaneMode.LIVE)
    _position(tmp_db, lane="trusted-copy")
    assert BLOCKER in engine.decide(_signal(), tmp_db).blockers


@pytest.mark.parametrize("state", ["planned", "reserved", "submitting", "submitted", "partial", "unknown"])
def test_an_unresolved_buy_counts(tmp_db, arm, state):
    """Two signals seconds apart: the first has no position yet, and an UNKNOWN may have filled."""
    arm(Lane.SM_TRENCHES, LaneMode.LIVE)
    _order(tmp_db, state)
    assert BLOCKER in engine.decide(_signal(), tmp_db).blockers


@pytest.mark.parametrize("state", ["failed", "expired", "cancelled", "filled"])
def test_a_finished_buy_with_no_position_does_not_block(tmp_db, arm, state):
    """MEASURED: a venue route error on one token was followed by a good fill 10 minutes later."""
    arm(Lane.SM_TRENCHES, LaneMode.LIVE)
    _order(tmp_db, state)
    assert BLOCKER not in engine.decide(_signal(), tmp_db).blockers


def test_a_closed_position_does_not_block_a_later_entry(tmp_db, arm):
    arm(Lane.SM_TRENCHES, LaneMode.LIVE)
    _position(tmp_db, closed=True)
    assert BLOCKER not in engine.decide(_signal(), tmp_db).blockers


def test_an_unresolved_sell_does_not_block(tmp_db, arm):
    arm(Lane.SM_TRENCHES, LaneMode.LIVE)
    _order(tmp_db, "submitted", side="sell")
    assert BLOCKER not in engine.decide(_signal(), tmp_db).blockers


def test_a_paper_position_does_not_block_a_live_entry(tmp_db, arm):
    arm(Lane.SM_TRENCHES, LaneMode.LIVE)
    _position(tmp_db, mode="shadow")
    _order(tmp_db, "planned", mode="shadow")
    assert BLOCKER not in engine.decide(_signal(), tmp_db).blockers


def test_a_different_token_is_not_blocked(tmp_db, arm):
    arm(Lane.SM_TRENCHES, LaneMode.LIVE)
    _position(tmp_db, token=OTHER)
    _order(tmp_db, "planned", token=OTHER)
    assert BLOCKER not in engine.decide(_signal(), tmp_db).blockers


def test_paper_is_untouched(tmp_db, arm):
    """Shadow keeps its own twin rule. This gate is about the owner's real tickets."""
    arm(Lane.SM_TRENCHES, LaneMode.SHADOW)
    _position(tmp_db)
    assert BLOCKER not in engine.decide(_signal(), tmp_db).blockers


def test_an_explicit_manual_intent_is_left_alone(tmp_db):
    assert engine._already_held_refusal(_signal(Lane.MANUAL), tmp_db) is None
    _position(tmp_db)
    assert engine._already_held_refusal(_signal(Lane.MANUAL), tmp_db) is None
    assert engine._already_held_refusal(_signal(), tmp_db) is not None


def test_an_unreadable_table_refuses():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    assert "unreadable" in engine._already_held_refusal(_signal(), conn)
