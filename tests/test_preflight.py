"""The pre-arm gate is callable from both its script and operator CLI surfaces."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from deploy import preflight
from kaiba.cli.main import app

runner = CliRunner()


def test_run_preflight_is_reentrant_and_supports_json(monkeypatch, capsys):
    monkeypatch.setattr(preflight, "_import_kaiba", lambda: (True, ""))
    monkeypatch.setattr(
        preflight,
        "CHECKS",
        {"synthetic": lambda: preflight.record("synthetic", preflight.PASS, "ok")},
    )

    assert preflight.run_preflight(json_out=True) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["ok"] is True
    assert first["checks"] == [{"name": "synthetic", "status": "PASS", "detail": "ok"}]

    # A second invocation must not inherit the first run's rows.
    assert preflight.run_preflight(json_out=True) == 0
    second = json.loads(capsys.readouterr().out)
    assert len(second["checks"]) == 1


def test_run_preflight_reports_skips_and_rejects_unknown_names(monkeypatch, capsys):
    monkeypatch.setattr(preflight, "_import_kaiba", lambda: (True, ""))
    monkeypatch.setattr(
        preflight,
        "CHECKS",
        {"synthetic": lambda: preflight.record("synthetic", preflight.PASS, "ok")},
    )

    assert preflight.run_preflight(json_out=True, skip="synthetic") == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["checks"][0]["status"] == "SKIP"

    assert preflight.run_preflight(skip="missing") == 2
    assert "unknown check(s)" in capsys.readouterr().err


def test_risk_preflight_cli_delegates_to_the_shared_gate(monkeypatch):
    calls: list[bool] = []
    monkeypatch.setattr("deploy.preflight.run_preflight", lambda: calls.append(True) or 0)
    result = runner.invoke(app, ["risk", "preflight"])
    assert result.exit_code == 0, result.stdout
    assert calls == [True]


def test_risk_preflight_cli_propagates_a_failed_gate(monkeypatch):
    monkeypatch.setattr("deploy.preflight.run_preflight", lambda: 1)
    result = runner.invoke(app, ["risk", "preflight"])
    assert result.exit_code == 1
