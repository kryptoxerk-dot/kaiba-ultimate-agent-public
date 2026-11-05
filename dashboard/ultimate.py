"""Unified, read-only intelligence workspace (Codex, 2026-11-03).

The existing console owns authentication. These routes consume bounded local
observations; refreshing this workspace never refreshes an upstream provider.
Kaiba and Aegis retain their own execution and operator controls.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from dashboard.ultimate_intel import build_intelligence
from dashboard.ultimate_pulse import build_pulse
from dashboard.ultimate_sources import build_kaiba_snapshot

router = APIRouter()
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
CACHE_SECONDS = 30
_lock = threading.Lock()
_cached: dict | None = None
_cached_at = 0.0


def _terminal_url() -> str | None:
    value = os.environ.get("KAIBA_AEGIS_TERMINAL_URL", "").strip()
    try:
        p = urlsplit(value)
        if p.scheme in ("http", "https") and p.netloc and not p.username and not p.password:
            return urlunsplit((p.scheme, p.netloc, p.path, "", ""))
    except ValueError:
        pass
    return None


def _brief() -> dict:
    root = Path(os.environ.get("KAIBA_DAILY_BRIEF_DIR") or Path(__file__).parents[1] / "data" / "briefings")
    path = root / "latest.json"
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 524_288:
            return {"status": "unavailable"}
        with path.open("rb") as f:
            raw = f.read(524_289)
        if len(raw) > 524_288:
            return {"status": "unavailable"}
        value = json.loads(raw)
        return value if isinstance(value, dict) and value.get("schema_version") == 1 else {"status": "unavailable"}
    except (OSError, ValueError, RecursionError):
        return {"status": "unavailable"}


def snapshot() -> dict:
    """Deduplicate simultaneous browser refreshes and bound ledger read frequency."""
    global _cached, _cached_at
    with _lock:
        now = time.monotonic()
        if _cached is not None and now - _cached_at < CACHE_SECONDS:
            return _cached
        value = {
            "as_of_ms": int(time.time() * 1000),
            "kaiba": build_kaiba_snapshot(),
            "intelligence": build_intelligence(),
            "pulse": build_pulse(),
            "daily_brief": _brief(),
            "aegis_terminal_url": _terminal_url(),
            "refresh_interval_s": CACHE_SECONDS,
            "scope": "Read-only workspace. Execution controls remain in each terminal.",
        }
        _cached, _cached_at = value, time.monotonic()
        return value


@router.get("/ultimate", response_class=HTMLResponse)
def ultimate_page(request: Request):
    return TEMPLATES.TemplateResponse(request=request, name="ultimate.html", context={})


@router.get("/api/ultimate")
def ultimate_snapshot():
    return JSONResponse(snapshot())
