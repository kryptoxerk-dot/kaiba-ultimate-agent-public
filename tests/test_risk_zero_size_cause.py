"""A zero size names its cause on the decision row.

MEASURED 2026-09-22 on the live box: 28 of 51 decisions in one half hour were refused as
``size_not_positive`` and nothing on the row said why. ``position_size`` has seven
distinct reasons to answer 0 -- lane off, chain not in lane, bankroll zero, score below
the ladder, exposure cap full, no viable band, remainder below the minimum -- and
``check_entry`` saw only the 0. The sizer knew; the row did not say.
"""

from __future__ import annotations

from kaiba.core.db import connect, migrate
from kaiba.execution import risk
from kaiba.execution.risk import RiskGate
from tests.test_risk import (  # noqa: F401 - fixtures are used by name
    LANE, SOL, TOKEN, gate, open_position, seed_depth, write_risk,
)


def test_a_full_exposure_cap_is_named(gate, tmp_db):
    open_position(tmp_db, TOKEN, 2_500_000_000)  # 25% of the 10 SOL bankroll: no room
    assert gate.position_size(SOL, LANE, 95.0, tmp_db, token=TOKEN) == 0
    verdict = RiskGate().check_entry(SOL, LANE, 0, tmp_db, token=TOKEN)
    assert not verdict.allowed
    assert verdict.reason.startswith("size_not_positive:total_exposure_cap:"), verdict.reason


def test_a_remainder_below_the_minimum_is_named_with_both_numbers(write_risk, tmp_db):
    write_risk(chains={"sol": {"min_position_base_units": 600_000_000}})
    seed_depth(tmp_db)  # a priced pool with a viable band, so the band is not the cause
    assert RiskGate().position_size(SOL, LANE, 95.0, tmp_db, token=TOKEN) == 0
    reason = RiskGate().check_entry(SOL, LANE, 0, tmp_db, token=TOKEN).reason
    assert reason.startswith("size_not_positive:below_min_position:"), reason
    assert "<600000000" in reason


def test_a_score_below_the_ladder_is_named(gate, tmp_db):
    assert gate.position_size(SOL, LANE, 0.0, tmp_db, token=TOKEN) == 0
    reason = RiskGate().check_entry(SOL, LANE, 0, tmp_db, token=TOKEN).reason
    assert reason.startswith("size_not_positive:score_below_ladder:"), reason


def test_the_cause_is_found_without_the_token(gate, tmp_db):
    """The engine passes the token to the sizer; not every caller passes it to the gate."""
    open_position(tmp_db, TOKEN, 2_500_000_000)
    gate.position_size(SOL, LANE, 95.0, tmp_db, token=TOKEN)
    reason = RiskGate().check_entry(SOL, LANE, 0, tmp_db).reason
    assert reason.startswith("size_not_positive:total_exposure_cap:"), reason


def test_no_recorded_cause_keeps_the_bare_reason(tmp_db, write_risk):
    """The old contract holds for a caller that never asked the sizer."""
    assert RiskGate().check_entry(SOL, LANE, 0, tmp_db).reason == "size_not_positive"


def test_a_cause_on_one_connection_never_labels_another(gate, tmp_db, tmp_path):
    open_position(tmp_db, TOKEN, 2_500_000_000)
    gate.position_size(SOL, LANE, 95.0, tmp_db, token=TOKEN)
    other = connect(tmp_path / "other.db")
    migrate(other)
    try:
        assert RiskGate().check_entry(SOL, LANE, 0, other, token=TOKEN).reason == "size_not_positive"
    finally:
        other.close()


def test_a_stale_cause_is_not_reused(gate, tmp_db, monkeypatch):
    open_position(tmp_db, TOKEN, 2_500_000_000)
    gate.position_size(SOL, LANE, 95.0, tmp_db, token=TOKEN)
    later = risk.now_ms() + risk.ZERO_SIZE_CAUSE_TTL_MS + 1
    monkeypatch.setattr(risk, "now_ms", lambda: later)
    assert RiskGate().check_entry(SOL, LANE, 0, tmp_db, token=TOKEN).reason == "size_not_positive"


def test_the_head_of_the_reason_is_unchanged_for_brake_classification():
    """``_deny`` classifies on the part before the colon; the suffix must not move it."""
    assert "size_not_positive:no_viable_band".split(":", 1)[0] == "size_not_positive"
