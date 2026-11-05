"""Operator-only risk controls exposed by the production CLI."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from kaiba.cli.main import app
from kaiba.core.config import get_risk, load_risk, save_risk
from kaiba.core.schemas import Lane

runner = CliRunner()


@pytest.fixture
def risk_cli(tmp_db, tmp_path, monkeypatch):
    risk_path = tmp_path / "risk.yaml"
    save_risk(load_risk(), risk_path)
    monkeypatch.setenv("KAIBA_RISK_PATH", str(risk_path))
    return tmp_db


def test_reduce_only_requires_one_explicit_direction(risk_cli):
    assert runner.invoke(app, ["risk", "reduce-only"]).exit_code != 0
    assert runner.invoke(app, ["risk", "reduce-only", "--on", "--off"]).exit_code != 0


def test_reduce_only_toggles_and_records_the_reason(risk_cli):
    result = runner.invoke(
        app, ["risk", "reduce-only", "--on", "--reason", "provider degraded"]
    )
    assert result.exit_code == 0, result.stdout
    assert get_risk().reduce_only is True

    result = runner.invoke(app, ["risk", "reduce-only", "--off"])
    assert result.exit_code == 0, result.stdout
    assert get_risk().reduce_only is False

    rows = risk_cli.execute("SELECT body FROM journal ORDER BY seq").fetchall()
    assert any("provider degraded" in row["body"] for row in rows)
    assert any("reduce_only=False" in row["body"] for row in rows)


def test_lane_mode_uses_the_existing_ceiling_and_journals(risk_cli):
    result = runner.invoke(
        app,
        ["risk", "lane", "manual", "--mode", "shadow", "--reason", "reviewed"],
    )
    assert result.exit_code == 0, result.stdout
    assert get_risk().lane(Lane.MANUAL).mode.value == "shadow"
    assert '"lane": "manual"' in result.stdout

    result = runner.invoke(app, ["risk", "lane", "manual", "--mode", "not-a-mode"])
    assert result.exit_code != 0
    assert "valid lanes and modes" in result.stdout


def test_global_mode_changes_do_not_widen_bounds(risk_cli):
    """Raising the global mode must leave the envelope exactly where it was.

    This used to assert ``max_lane_mode == "shadow"`` literally, which made the test a
    reading of whatever ``config/risk.yaml`` happened to contain rather than of the
    invariant. It broke the moment the operator armed the shipped file -- not because
    the envelope widened, but because it was never shadow to begin with. Capture the
    bounds before and compare, so this holds at any starting point.
    """
    before = get_risk().bounds.model_dump()

    result = runner.invoke(
        app, ["risk", "global", "--mode", "live", "--reason", "operator test"]
    )
    assert result.exit_code == 0, result.stdout
    risk = get_risk()
    assert risk.global_mode.value == "live"
    assert risk.bounds.model_dump() == before, (
        "changing global_mode must not touch the bounds block at all"
    )
    assert '"global_mode": "live"' in result.stdout


def test_global_mode_cannot_exceed_the_envelope(risk_cli):
    """The envelope is a ceiling, not a suggestion: a lane still cannot run hotter.

    The test above proves the bounds are not *edited*. This proves they still *bind*
    after the edit, which is the property an operator actually relies on -- otherwise
    "bounds unchanged" would be satisfied by an envelope nothing consults.
    """
    from kaiba.core.schemas import Lane, LaneMode

    risk = get_risk()
    bounds = risk.bounds.model_copy(update={"max_lane_mode": LaneMode.SHADOW})
    lanes = dict(risk.lanes)
    a_lane = next(iter(lanes))
    lanes[a_lane] = lanes[a_lane].model_copy(update={"mode": LaneMode.LIVE})
    clamped = risk.model_copy(update={"bounds": bounds, "lanes": lanes,
                                      "global_mode": LaneMode.LIVE, "kill_switch": False})

    effective = clamped.effective_mode(a_lane if isinstance(a_lane, Lane) else Lane(a_lane))
    assert effective is LaneMode.SHADOW, (
        f"a live lane under a shadow envelope resolved to {effective}, not shadow"
    )


def test_resume_clear_kill_is_explicit_and_clears_both_flags(risk_cli):
    risk = get_risk()
    risk.kill_switch = True
    risk.entries_paused = True
    save_risk(risk)

    result = runner.invoke(
        app, ["risk", "resume", "--clear-kill", "--reason", "incident resolved"]
    )
    assert result.exit_code == 0, result.stdout
    after = get_risk()
    assert after.kill_switch is False
    assert after.entries_paused is False
    assert '"kill_switch": false' in result.stdout

    rows = risk_cli.execute("SELECT body FROM journal ORDER BY seq").fetchall()
    assert any("kill switch cleared" in row["body"] for row in rows)
