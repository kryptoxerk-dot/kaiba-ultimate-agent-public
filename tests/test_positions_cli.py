"""Read-only position inspection used by the unprotected-position runbook."""

import json
from pathlib import Path

from typer.testing import CliRunner

from kaiba.cli.main import app

runner = CliRunner()
ROOT = Path(__file__).resolve().parents[1]


def _insert_position(conn, position_id: str, *, closed_ms: int | None = None, opened_ms: int = 1_000):
    conn.execute(
        "INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms, closed_ms, "
        "qty, qty_total, cost_native, entry_price_usd, stop_price_usd, protected, "
        "protection_ids_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            position_id,
            "sol",
            "So11111111111111111111111111111111111111112",
            "confluence-5",
            "paper",
            opened_ms,
            closed_ms,
            "12.5",
            "12.5",
            "0.25",
            "1.50",
            "1.10",
            1,
            '["provider-order-secret"]',
        ),
    )
    conn.commit()


def test_positions_show_json_is_open_only_and_scrubbed(tmp_db):
    _insert_position(tmp_db, "open-1", opened_ms=2_000)
    _insert_position(tmp_db, "closed-1", closed_ms=3_000, opened_ms=1_000)

    result = runner.invoke(app, ["positions", "show", "--json"])

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert [row["position_id"] for row in payload["positions"]] == ["open-1"]
    assert "protection_ids_json" not in payload["positions"][0]
    assert payload["quote"] == {
        "status": "unavailable",
        "reason": "persisted position snapshot; no live mark was requested",
    }


def test_positions_show_all_includes_closed_and_filters_id(tmp_db):
    _insert_position(tmp_db, "open-1", opened_ms=2_000)
    _insert_position(tmp_db, "closed-1", closed_ms=3_000, opened_ms=1_000)

    result = runner.invoke(app, ["positions", "show", "closed-1", "--all", "--json"])

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert [row["position_id"] for row in payload["positions"]] == ["closed-1"]
    assert payload["positions"][0]["closed_ms"] == 3_000


def test_positions_show_unknown_id_is_a_read_only_error(tmp_db):
    _insert_position(tmp_db, "open-1")

    result = runner.invoke(app, ["positions", "show", "missing"])

    assert result.exit_code == 1
    assert "position not found: missing" in result.stdout
    assert "--all" in result.stdout


def test_positions_show_table_marks_current_quote_unavailable_and_hides_ids(tmp_db):
    _insert_position(tmp_db, "open-1")

    result = runner.invoke(app, ["positions", "show"])

    assert result.exit_code == 0, result.stdout
    assert "current quote" in result.stdout.lower()
    assert "unavailable" in result.stdout.lower()
    assert "provider-order-secret" not in result.stdout
    assert "protection_ids_json" not in result.stdout


def test_positions_show_help_and_incident_runbook_stay_in_sync():
    result = runner.invoke(app, ["positions", "show", "--help"])

    assert result.exit_code == 0, result.stdout
    assert "--all" in result.stdout
    assert "--limit" in result.stdout
    assert "--json" in result.stdout
    runbook = (ROOT / "docs" / "runbooks" / "incident-position-unprotected.md").read_text(
        encoding="utf-8"
    )
    assert "kaiba.cli.main positions show <position_id>" in runbook
