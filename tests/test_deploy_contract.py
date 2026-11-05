"""Keep production systemd entrypoints and the Python CLI in lockstep."""

import tomllib
from pathlib import Path

from typer.testing import CliRunner

from kaiba.cli.main import app
from kaiba.cli.reports import REPORT_NAMES

ROOT = Path(__file__).resolve().parents[1]
runner = CliRunner()


def test_systemd_service_commands_have_cli_surfaces():
    commands = {
        "kaiba-ingest.service": (["ingest", "run", "--help"], "--listeners"),
        "kaiba-ops.service": (["ops", "run", "--help"], "--once"),
        "kaiba-scan.service": (["scan", "run", "--help"], "--workers"),
        "kaiba-engine.service": (["engine", "run", "--help"], "--once"),
        "kaiba-protection.service": (["protection", "run", "--help"], "Run the exit watchdog"),
        "kaiba-reconcile.service": (["reconcile", "once", "--help"], "--write-reports"),
        "kaiba-signer.service": (["signer", "serve", "--help"], "--socket"),
    }
    for unit, (argv, expected_help) in commands.items():
        text = (ROOT / "deploy" / "systemd" / unit).read_text(encoding="utf-8")
        assert "ExecStart=" in text
        result = runner.invoke(app, argv)
        assert result.exit_code == 0, f"{unit}: {result.stdout}"
        assert expected_help in result.stdout


def test_reconcile_unit_requests_all_hermes_reports():
    text = (ROOT / "deploy" / "systemd" / "kaiba-reconcile.service").read_text(encoding="utf-8")
    assert "reconcile once --write-reports" in text
    assert tuple(REPORT_NAMES) == ("status", "positions", "signals", "providers", "risk", "journal")


def test_legacy_run_aliases_remain_documented_in_cli_help():
    result = runner.invoke(app, ["run", "--help"])
    assert result.exit_code == 0
    for command in ("ingest", "engine", "protection"):
        assert command in result.stdout


def test_production_extra_covers_enabled_service_boundaries():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    production = set(project["project"]["optional-dependencies"]["production"])
    assert any(dep.startswith("mcp>=") for dep in production)
    assert any(dep.startswith("telethon>=") for dep in production)
    assert any(dep.startswith("solders>=") for dep in production)
    assert any(dep.startswith("eth-account>=") for dep in production)


def test_installer_defaults_to_production_extra():
    text = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")
    assert 'KAIBA_EXTRAS="${KAIBA_INSTALL_EXTRAS:-production}"' in text
    assert '"$RELEASE[$KAIBA_EXTRAS]"' in text


def test_mcp_unit_uses_the_core_socket_bridge():
    text = (ROOT / "deploy" / "systemd" / "kaiba-mcp.service").read_text(encoding="utf-8")
    exec_lines = [line for line in text.splitlines() if line.startswith("ExecStart=")]
    assert len(exec_lines) == 1
    assert "-m kaiba.mcp.socket_bridge serve" in exec_lines[0]
    assert "--socket /run/kaiba/mcp/kaiba.sock" in text
    assert "--socket-group kaiba-mcp-ipc" in text


def test_hermes_profiles_use_the_socket_client_shim():
    for profile in ("kaiba-operator", "kaiba-research", "kaiba-reflect"):
        text = (ROOT / "hermes" / "profiles" / profile / "config.yaml").read_text(
            encoding="utf-8"
        )
        assert '"kaiba.mcp.socket_bridge", "client"' in text
        assert 'command: "{{PYTHON}}"' in text
        assert 'KAIBA_MCP_SOCKET: "/run/kaiba/mcp/kaiba.sock"' in text


def test_hermes_installer_checks_bridge_and_finds_production_venv():
    text = (ROOT / "deploy" / "install-hermes.sh").read_text(encoding="utf-8")
    assert 'kaiba/mcp/socket_bridge.py' in text
    assert 'KAIBA_PARENT/venv/bin/python' in text
    assert 'KAIBA_ROOT}/.venv/bin/python' in text
    # The default is resolved after --root is parsed, so a release symlink can select
    # /opt/kaiba/venv rather than accidentally using the source checkout's path.
    assert text.index('while [[ $# -gt 0 ]]') < text.index('if [[ -z "$PYTHON_BIN" ]]')


def test_arming_runbook_risk_controls_have_cli_surfaces():
    controls = {
        "reduce-only": "--on",
        "lane": "--mode",
        "global": "--mode",
        "resume": "--clear-kill",
    }
    for command, option in controls.items():
        argv = ["risk", command, "--help"]
        result = runner.invoke(app, argv)
        assert result.exit_code == 0, result.stdout
        assert option in result.stdout


def test_preflight_gate_is_packaged_for_the_operator_cli():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    includes = set(project["tool"]["setuptools"]["packages"]["find"]["include"])
    assert "deploy*" in includes
    assert (ROOT / "deploy" / "__init__.py").exists()
    assert "def run_preflight(" in (ROOT / "deploy" / "preflight.py").read_text(encoding="utf-8")


def test_scanner_unit_drains_the_durable_queue_after_ingestion():
    text = (ROOT / "deploy" / "systemd" / "kaiba-scan.service").read_text(encoding="utf-8")
    assert "ExecStart=/opt/kaiba/venv/bin/python -I -B -m kaiba.cli.main scan run" in text
    assert "After=network-online.target kaiba-ingest.service" in text
    assert "PartOf=kaiba.target" in text
    assert "User=kaiba-core" in text and "Group=kaiba-core" in text
    assert "ReadWritePaths=/var/lib/kaiba/db /var/lib/kaiba/cache" in text
    assert "UnsetEnvironment=GMGN_PRIVATE_KEY KAIBA_SIGNER_PASSPHRASE" in text
    assert "/var/lib/kaiba/reports" not in next(
        line for line in text.splitlines() if line.startswith("ReadWritePaths=")
    )
    assert "HELIUS_WEBHOOK_SECRET" in text and "LANGFUSE_SECRET_KEY" in text


def test_ops_unit_runs_the_configured_scheduler_without_signer_access():
    text = (ROOT / "deploy" / "systemd" / "kaiba-ops.service").read_text(encoding="utf-8")
    assert "ExecStart=/opt/kaiba/venv/bin/python -I -B -m kaiba.cli.main ops run" in text
    assert "After=network-online.target kaiba-ingest.service" in text
    assert "PartOf=kaiba.target" in text
    assert "Environment=KAIBA_SCHEDULE_CONFIG=/etc/kaiba/config/schedule.yaml" in text
    assert "StateDirectory=kaiba/ops" in text
    assert "UnsetEnvironment=KAIBA_SIGNER_PASSPHRASE KAIBA_SIGNER_KEYSTORE" in text
    assert "UnsetEnvironment=GMGN_PRIVATE_KEY" in text
    assert "HELIUS_WEBHOOK_SECRET" in text and "ALCHEMY_WEBHOOK_SIGNING_KEY" in text
    assert "TG_API_ID" in text and "LANGFUSE_SECRET_KEY" in text
    assert "/run/kaiba/signer" in text and "InaccessiblePaths=" in text


def test_scanner_unit_is_installed_started_and_reported():
    install = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")
    target = (ROOT / "deploy" / "systemd" / "kaiba.target").read_text(encoding="utf-8")
    status = (ROOT / "deploy" / "status.sh").read_text(encoding="utf-8")
    runbook = (ROOT / "docs" / "runbooks" / "deploy.md").read_text(encoding="utf-8")

    assert "UNITS=(kaiba-ingest.service kaiba-ops.service kaiba-scan.service" in install
    assert "kaiba-ingest.service kaiba-ops.service kaiba-scan.service kaiba-engine.service" in install
    assert "kaiba-ingest.service kaiba-ops.service kaiba-scan.service kaiba-engine.service" in target
    assert "kaiba-mcp kaiba-ingest kaiba-ops kaiba-scan kaiba-engine" in status
    assert "systemctl start kaiba-ingest kaiba-ops kaiba-scan" in runbook
    assert "kaiba scan status --json" in runbook
    assert "kaiba ops status" in runbook


def test_scanner_is_in_restore_upgrade_and_provider_key_lifecycles():
    restore = (ROOT / "deploy" / "restore.sh").read_text(encoding="utf-8")
    rotate = (ROOT / "deploy" / "rotate-keys.sh").read_text(encoding="utf-8")
    upgrade = (ROOT / "docs" / "runbooks" / "upgrade.md").read_text(encoding="utf-8")
    keys = (ROOT / "docs" / "runbooks" / "keys.md").read_text(encoding="utf-8")
    deploy_runbook = (ROOT / "docs" / "runbooks" / "deploy.md").read_text(encoding="utf-8")
    env_example = (ROOT / "deploy" / "env" / "deploy.env.example").read_text(encoding="utf-8")

    assert "SERVICES=(kaiba-ingest kaiba-ops kaiba-scan kaiba-engine" in restore
    assert "systemctl restart kaiba-ingest kaiba-ops kaiba-scan kaiba-engine" in rotate
    assert "systemctl stop kaiba-engine kaiba-scan kaiba-ops kaiba-ingest" in upgrade
    assert "systemctl start kaiba-ingest kaiba-ops kaiba-scan" in upgrade
    assert "systemctl restart kaiba-ingest kaiba-ops kaiba-scan kaiba-engine" in keys
    assert "KAIBA_ALLOW_NODE_JIT" in deploy_runbook and "kaiba-scan.service" in deploy_runbook
    assert "kaiba-scan.service" in env_example


def test_scheduler_config_is_seeded_once_and_operator_edits_survive_upgrades():
    install = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")
    assert 'seed_release_file "$RELEASE/config/schedule.yaml"' in install
    assert '"$KAIBA_CONF/config/schedule.yaml" root kaiba-core 0640' in install
    assert 'if [[ -s "$dst" ]]; then' in install
    assert 'if [[ -L "$dst" ]]; then' in install
    assert "signals.yaml remains optional" in install


def test_backup_restore_preserve_config_and_policy_boundaries():
    backup = (ROOT / "deploy" / "backup.sh").read_text(encoding="utf-8")
    restore = (ROOT / "deploy" / "restore.sh").read_text(encoding="utf-8")
    assert 'install -d -m 0700 "$STAGE/policy"' in backup
    assert 'for f in "$KAIBA_CONF/config"/*.yaml' in backup
    assert 'for f in "$KAIBA_CONF/policy"/*.yaml' in backup
    assert 'cp -p "$KAIBA_CONF/fingerprints/current.tsv" "$STAGE/fingerprints/"' in backup
    assert 'if [[ -d "$RESTORE_DIR/policy" ]]; then' in restore
    assert 'restore_yaml_dir "$RESTORE_DIR/policy" "$KAIBA_CONF/policy"' in restore
    assert 'risk.yaml|schedule.yaml|signals.yaml' in restore
    assert 'RESTORE_DIR/fingerprints/current.tsv' in restore
