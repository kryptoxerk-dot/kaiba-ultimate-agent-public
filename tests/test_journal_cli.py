"""The operator journal command mirrors the bounded MCP append surface."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from kaiba.cli.main import app
from kaiba.core import journal

runner = CliRunner()


def test_journal_add_appends_without_echoing_the_body(tmp_db):
    result = runner.invoke(
        app,
        ["journal", "add", "lesson", "  use measured fills  ", "--subject", "sol:token"],
    )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["seq"] == 1
    assert payload["kind"] == "lesson"
    assert payload["subject"] == "sol:token"
    assert len(payload["hash"]) == 16
    assert "use measured fills" not in result.stdout

    row = tmp_db.execute("SELECT body FROM journal WHERE seq = 1").fetchone()
    assert row["body"] == "use measured fills"
    assert journal.verify(tmp_db) == (True, None)


def test_journal_add_rejects_unknown_kind_and_empty_body(tmp_db):
    bad_kind = runner.invoke(app, ["journal", "add", "diary", "text"])
    assert bad_kind.exit_code == 1
    assert "unknown journal kind" in bad_kind.stdout

    empty = runner.invoke(app, ["journal", "add", "lesson", "   "])
    assert empty.exit_code == 1
    assert "journal body is empty" in empty.stdout
    assert tmp_db.execute("SELECT COUNT(*) AS n FROM journal").fetchone()["n"] == 0


def test_journal_add_rejects_bodies_over_the_skill_cap(tmp_db):
    result = runner.invoke(app, ["journal", "add", "observation", "x" * 4001])

    assert result.exit_code == 1
    assert "exceeds 4000 characters" in result.stdout
    assert tmp_db.execute("SELECT COUNT(*) AS n FROM journal").fetchone()["n"] == 0


def test_journal_help_exposes_the_append_command():
    result = runner.invoke(app, ["journal", "--help"])
    assert result.exit_code == 0, result.stdout
    assert "add" in result.stdout

