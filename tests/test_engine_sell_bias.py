"""Real money never opens a long on a sell-bias signal.

MEASURED 2026-09-21. ``lanes.migration_fade`` emits ``payload["bias"] == "sell"`` -- its
thesis is that 73% of migrations trade below 40% of the migration price within 20 min --
and the engine's live order builder hardcodes ``side=Side.BUY``. Armed live, the lane's
first two real fills were longs at migration+1s: -91.5% (0.05625 -> 0.004777 SOL) and
-66.0% (0.055607 -> 0.018751 SOL). ``kaiba/learning/replay.py`` had already written down
that the short arm has no venue; nothing had written down that the long arm must
therefore not be sent. Shadow keeps recording it, because that record is the replay's
input for the alternatives a spot venue can express.
"""

from __future__ import annotations

import pytest

from kaiba.core.config import load_risk, save_risk
from kaiba.core.schemas import Action, Chain, Lane, LaneMode, Signal
from kaiba.execution import engine, lanes

TOKEN = "EfcmyGs6M8auzbHmyW3tki8WbZh2rnbb3bS4eiyUHedq"
BLOCKER = "bias_sell_no_short_venue"


@pytest.fixture
def arm(tmp_path, monkeypatch):
    """Put one lane in the requested mode in an isolated risk.yaml."""

    def _arm(lane: Lane, mode: LaneMode):
        cfg = load_risk()
        cfg.global_mode = LaneMode.LIVE
        cfg.kill_switch = False
        cfg.lanes[lane].mode = mode
        path = tmp_path / "risk.yaml"
        save_risk(cfg, path)
        monkeypatch.setenv("KAIBA_RISK_PATH", str(path))
        assert engine.get_risk().effective_mode(lane) is mode, "fixture could not arm the lane"

    return _arm


def _signal(bias: str, lane: Lane = Lane.MIGRATION_FADE) -> Signal:
    ts = engine.now_ms()
    return Signal(
        signal_id=lanes.signal_id_for(lane, Chain.SOL, TOKEN, ts, 180), lane=lane, chain=Chain.SOL,
        token=TOKEN, strength=0.73, reasons=["migration 1.0s ago"], window_s=180, created_ms=ts,
        payload={"bias": bias, "sell_within_s": 180, "never_hold_through_migration": True},
    )


@pytest.mark.parametrize("mode", [LaneMode.LIVE, LaneMode.CANARY])
def test_a_sell_bias_signal_is_skipped_with_real_money(tmp_db, arm, mode):
    arm(Lane.MIGRATION_FADE, mode)
    d = engine.decide(_signal("sell"), tmp_db)
    assert d.action is Action.SKIP
    assert BLOCKER in d.blockers, d.blockers
    assert "no short arm" in d.thesis


def test_shadow_keeps_recording_the_long_arm(tmp_db, arm):
    """The replay module prices the alternatives from this record. Do not blind it."""
    arm(Lane.MIGRATION_FADE, LaneMode.SHADOW)
    d = engine.decide(_signal("sell"), tmp_db)
    assert BLOCKER not in d.blockers, d.blockers


def test_a_buy_bias_signal_is_not_touched_by_the_rule(tmp_db, arm):
    arm(Lane.MIGRATION_FADE, LaneMode.LIVE)
    d = engine.decide(_signal("buy"), tmp_db)
    assert BLOCKER not in d.blockers, d.blockers


def test_the_decision_row_carries_the_blocker(tmp_db, arm):
    """Skips are training data (test_paper.py). This one must be readable off the table."""
    arm(Lane.MIGRATION_FADE, LaneMode.LIVE)
    d = engine.decide(_signal("sell"), tmp_db)
    row = tmp_db.execute("SELECT action, blockers_json FROM decisions WHERE decision_id=?", (d.decision_id,)).fetchone()
    assert row["action"] == "skip" and BLOCKER in row["blockers_json"]
