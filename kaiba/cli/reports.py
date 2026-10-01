"""Publish the small, read-only report surface used by Hermes in production.

The deployment layout deliberately gives the ``kaiba-agent`` user access to these six
JSON files rather than to the SQLite database.  Reconciliation is the single writer so a
restart cannot leave a half-written document behind: each file is rendered to a sibling
temporary file and replaced atomically.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from kaiba.core.config import get_risk, get_settings
from kaiba.core.schemas import now_ms

REPORT_NAMES = ("status", "positions", "signals", "providers", "risk", "journal")


def reports_dir(path: Path | None = None) -> Path:
    """Return the configured report directory without creating it."""
    if path is not None:
        return Path(path)
    settings = get_settings()
    return Path(os.environ.get("KAIBA_REPORTS_DIR", settings.kaiba_data_dir / "reports"))


def snapshot() -> dict[str, dict[str, Any]]:
    """Build all six reports from the same current database/config view.

    The MCP read functions already scrub provider text and bound result sizes. Reusing
    them keeps the dashboard, Hermes and CLI views consistent and avoids accidentally
    exposing a credential while adding a new report field.
    """
    from kaiba.mcp.server import (
        kaiba_journal_read,
        kaiba_positions,
        kaiba_signals,
        kaiba_status,
    )

    status = kaiba_status()
    return {
        "status": status,
        "positions": kaiba_positions(),
        "signals": kaiba_signals(),
        "providers": {
            "generated_ms": now_ms(),
            "providers": status.get("providers", []),
        },
        "risk": {
            "generated_ms": now_ms(),
            "config": get_risk().model_dump(mode="json"),
            "mode": status.get("global_mode"),
            "entries_paused": status.get("entries_paused"),
            "reduce_only": status.get("reduce_only"),
            "kill_switch": status.get("kill_switch"),
        },
        "journal": kaiba_journal_read(limit=50),
    }


def _write_atomic(path: Path, payload: Any) -> None:
    """Write one JSON document and replace the destination atomically."""
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    if os.name == "posix":
        os.chmod(temp, 0o640)
    os.replace(temp, path)


def write_reports(path: Path | None = None) -> list[Path]:
    """Atomically publish the six Hermes reports and return their paths."""
    directory = reports_dir(path)
    directory.mkdir(parents=True, exist_ok=True)
    reports = snapshot()
    written: list[Path] = []
    for name in REPORT_NAMES:
        destination = directory / f"{name}.json"
        _write_atomic(destination, reports[name])
        written.append(destination)
    return written


__all__ = ["REPORT_NAMES", "reports_dir", "snapshot", "write_reports"]
