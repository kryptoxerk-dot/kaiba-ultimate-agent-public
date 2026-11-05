"""The lane CLI carries owner authority through the existing server gate."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from kaiba.cli.main import app
from kaiba.core.config import EnvelopeBounds, LaneConfig, RiskConfig, get_risk, save_risk
from kaiba.core.schemas import Lane, LaneMode

runner = CliRunner()


@pytest.fixture
def isolated_lane_risk(tmp_db, tmp_path, monkeypatch):
    """Use only an unfunded fixture config and the fixture journal database."""
    risk_path = tmp_path / "risk.yaml"
    monkeypatch.setenv("KAIBA_RISK_PATH", str(risk_path))
    save_risk(
        RiskConfig(
            global_mode=LaneMode.LIVE,
            bounds=EnvelopeBounds(max_lane_mode=LaneMode.LIVE),
            lanes={Lane.MANUAL: LaneConfig(mode=LaneMode.SHADOW)},
        ),
        risk_path,
    )
    return tmp_db


@pytest.mark.parametrize("mode", ["live", "canary"])
@pytest.mark.parametrize("owner_request", [None, "", " \t "])
def test_missing_or_blank_owner_request_remains_refused(isolated_lane_risk, mode, owner_request):
    args = ["risk", "lane", "manual", "--mode", mode]
    if owner_request is not None:
        args.extend(["--owner-request", owner_request])
    before = get_risk().model_dump()

    result = runner.invoke(app, args)

    assert result.exit_code != 0, result.stdout
    assert "owner_request_required" in result.stdout
    assert get_risk().model_dump() == before
    rows = isolated_lane_risk.execute("SELECT body FROM journal").fetchall()
    assert any("with no owner request" in row["body"] for row in rows)


@pytest.mark.parametrize("mode", ["live", "canary"])
def test_exact_owner_quote_reaches_real_gate(isolated_lane_risk, monkeypatch, mode):
    from kaiba.mcp import server

    real_set_lane_mode = server.kaiba_set_lane_mode
    forwarded = []

    def record_and_apply(lane, target_mode, reason="", owner_request=""):
        forwarded.append(owner_request)
        return real_set_lane_mode(lane, target_mode, reason, owner_request=owner_request)

    monkeypatch.setattr(server, "kaiba_set_lane_mode", record_and_apply)
    quote = "  Get trading to start asap\nWilli  "
    before_bounds = get_risk().bounds.model_dump()

    result = runner.invoke(
        app,
        ["risk", "lane", "manual", "--mode", mode, "--owner-request", quote,
         "--reason", "authorized operator request"],
    )

    assert result.exit_code == 0, result.stdout
    assert forwarded == [quote]
    assert get_risk().lane(Lane.MANUAL).mode.value == mode
    assert get_risk().effective_mode(Lane.MANUAL).value == mode
    assert get_risk().bounds.model_dump() == before_bounds
    rows = isolated_lane_risk.execute("SELECT body FROM journal").fetchall()
    assert any("authorized operator request" in row["body"] for row in rows)


@pytest.mark.parametrize("mode", ["shadow", "off"])
def test_unfunded_modes_need_no_owner_request(isolated_lane_risk, mode):
    result = runner.invoke(app, ["risk", "lane", "manual", "--mode", mode])

    assert result.exit_code == 0, result.stdout
    assert get_risk().lane(Lane.MANUAL).mode.value == mode


@pytest.mark.parametrize("mode", ["live", "canary"])
def test_owner_quote_does_not_raise_existing_envelope(isolated_lane_risk, tmp_path, mode):
    # Bootstrap a separate fixture file: save_risk deliberately preserves an existing
    # file's bounds, so set the ceiling before that file exists.
    risk_path = tmp_path / "risk.yaml"
    risk = get_risk()
    risk_path.unlink()
    risk.bounds.max_lane_mode = LaneMode.SHADOW
    save_risk(risk, risk_path)

    result = runner.invoke(
        app,
        ["risk", "lane", "manual", "--mode", mode,
         "--owner-request", "Get trading to start asap"],
    )

    assert result.exit_code != 0, result.stdout
    assert "envelope ceiling is shadow" in result.stdout
    assert get_risk().bounds.max_lane_mode is LaneMode.SHADOW
    assert get_risk().lane(Lane.MANUAL).mode is LaneMode.SHADOW
