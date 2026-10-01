"""Static contract tests for the Hermes Codex OAuth fallback preparation.

These tests deliberately do not authenticate, call a model, contact a VPS, or start a
gateway.  The live activation sequence belongs in the deployment runbook and must happen
after Claude's Hermes installation is complete.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PROFILES = ("kaiba-operator", "kaiba-research", "kaiba-reflect")


def _profile(name: str) -> tuple[Path, dict]:
    path = ROOT / "hermes" / "profiles" / name / "config.yaml"
    return path, yaml.safe_load(path.read_text(encoding="utf-8"))


def test_every_profile_declares_astra_fallback_at_high_effort() -> None:
    for name in PROFILES:
        path, config = _profile(name)
        fallback = config["fallback_providers"]
        assert fallback == [{"provider": "openai-codex", "model": "gpt-6-astra"}], path
        assert config["agent"]["reasoning_overrides"]["gpt-6-astra"] == "high", path


def test_astra_override_does_not_replace_profile_primary_effort() -> None:
    expected_primary_effort = {
        "kaiba-operator": "max",
        "kaiba-research": "medium",
        "kaiba-reflect": "max",
    }
    for name, effort in expected_primary_effort.items():
        _, config = _profile(name)
        assert config["model"]["provider"] == "anthropic"
        assert config["agent"]["reasoning_effort"] == effort


def test_deployment_runbook_installs_and_authenticates_in_service_home() -> None:
    runbook = (ROOT / "docs" / "runbooks" / "deploy.md").read_text(encoding="utf-8")
    marker = "HOME=/var/lib/kaiba/hermes HERMES_HOME=/var/lib/kaiba/hermes/.hermes"
    assert runbook.count(marker) >= 4
    assert "install-hermes.sh --root /opt/kaiba/current" in runbook
    assert "--hermes /opt/kaiba/hermes-venv/bin/hermes" in runbook
    assert "auth add openai-codex" in runbook
    assert "auth status openai-codex" in runbook
    assert "--provider openai-codex --model gpt-6-astra --reasoning high" in runbook
    assert "Call kaiba_status and report the lane modes in one sentence." in runbook
    assert "sudo /opt/kaiba/current/deploy/install-hermes.sh" not in runbook


def test_installer_warns_against_sharing_codex_cli_credentials() -> None:
    installer = (ROOT / "deploy" / "install-hermes.sh").read_text(encoding="utf-8")
    assert "auth add openai-codex" in installer
    assert "Do not copy or share ~/.codex/auth.json" in installer
    assert "same service user and" in installer
    assert 'if [[ "$(id -u)" -eq 0 ]]' in installer
    assert "Refuse that class of deployment error" in installer
    assert '$(id -un)' in installer and '"kaiba-agent"' in installer
    assert "HERMES_HOME must be explicit" in installer


def test_codex_oauth_material_is_not_in_repo_templates() -> None:
    # Only names and commands belong in the repository.  These checks catch accidental
    # token-shaped values in the files that are copied to a VPS.
    candidates = [
        *(ROOT / "hermes" / "profiles" / name / "config.yaml" for name in PROFILES),
        ROOT / "deploy" / "install-hermes.sh",
        ROOT / "docs" / "runbooks" / "deploy.md",
    ]
    forbidden = ("access_token=", "refresh_token=", "client_secret=", "sk-proj-")
    for path in candidates:
        text = path.read_text(encoding="utf-8").lower()
        assert not any(value in text for value in forbidden), path


