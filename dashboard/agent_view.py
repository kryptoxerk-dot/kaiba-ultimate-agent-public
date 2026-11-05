"""The Hermes operator agent, seen from the console (2026-10-20).

The owner talks to ``kaiba-operator`` on Telegram (@your_kaiba_bot) and asked to watch and
prompt the same agent from the terminal. Prompting already exists (:mod:`dashboard.agent_chat`:
one ``hermes chat`` turn per message on the same profile). This module is the *watching* side:

* the Telegram conversation (the latest ``telegram`` session) and the agent's own background work
  (its ``cron`` sessions: the early-alpha hunt, paper health, ...), read with
  ``hermes sessions export`` -- Hermes's own exporter, which REDACTS secrets by default -- into a
  0700 temp dir that is deleted before the call returns; nothing is cached on disk;
* its scheduled jobs (``hermes cron list``), rate-limit windows (``hermes usage --json``) and the
  three gateway units' state;
* two owner actions: post a message to the Telegram home channel (``hermes send -t telegram``)
  and the agent's emergency stop (``hermes pause`` / ``hermes resume``).

Rules kept from :mod:`dashboard.agent_chat` / AGENTS.md: this never reads a file under
``~/.hermes`` itself, never runs ``hermes auth``/``logout``, never restarts a gateway, and passes
every message on stdin, never through a shell.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from dashboard import agent_chat, broker, trading

log = logging.getLogger(__name__)

ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
SESSION_ID = re.compile(r"\b\d{8}_\d{6}_[0-9a-f]{6,}\b")
GATEWAYS = ("hermes-gateway-kaiba-operator", "hermes-gateway-kaiba-reflect", "hermes-gateway-kaiba-research")
MAX_TEXT = 4000
MAX_TOOL_TEXT = 400
MAX_SEND_CHARS = 3500
CLI_TIMEOUT_S = 45

_cache: dict[str, tuple[float, Any]] = {}
_lock = threading.RLock()  # re-entrant: status() builds through gateways()


def clear_caches() -> None:
    _cache.clear()


def _cached(key: str, ttl_s: float, build):
    hit = _cache.get(key)
    now = time.monotonic()
    if hit is not None and now - hit[0] < ttl_s:
        return hit[1]
    with _lock:
        hit = _cache.get(key)
        if hit is not None and time.monotonic() - hit[0] < ttl_s:
            return hit[1]
        value = build()
        _cache[key] = (time.monotonic(), value)
        return value


# --------------------------------------------------------------------------------------
# running the Hermes CLI
# --------------------------------------------------------------------------------------


def available() -> tuple[bool, str]:
    return agent_chat.available()


def _hermes(args: list[str], *, stdin: str | None = None, timeout: float = CLI_TIMEOUT_S) -> tuple[int | None, str]:
    """One read-only (or owner-requested) Hermes CLI call on the operator profile. Never raises."""
    if broker.active():
        return broker.hermes(args, stdin=stdin, timeout=timeout)
    return run_hermes(args, stdin=stdin, timeout=timeout)


def run_hermes(args: list[str], *, stdin: str | None, timeout: float) -> tuple[int | None, str]:
    if os.environ.get("KAIBA_CHAT_ARGV"):
        return None, "no Hermes on this machine (test or preview)"
    argv, env, cwd = agent_chat.command_for("main")
    python, profile = argv[0], agent_chat._profile()
    full = [python, "-m", "hermes_cli.main", "--profile", profile, *args]
    try:
        done = subprocess.run(full, input=stdin, capture_output=True, text=True, timeout=timeout,
                              env={**env, "HOME": str(Path.home()), "NO_COLOR": "1"}, cwd=cwd, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"{type(exc).__name__}: {exc}"[:300]
    return done.returncode, ANSI.sub("", (done.stdout or "") + (done.stderr or ""))


def _export(args: list[str]) -> list[dict[str, Any]]:
    """``hermes sessions export --format jsonl`` (redacted) into a private temp dir, parsed, deleted."""
    base = "/dev/shm" if os.path.isdir("/dev/shm") else None
    tmp = tempfile.mkdtemp(prefix="kaiba-hx-", dir=broker.EXPORT_DIR if broker.active() else base)
    try:
        os.chmod(tmp, 0o700)
        out = Path(tmp) / "sessions.jsonl"
        rc, text = _hermes(["sessions", "export", "--format", "jsonl", *args, str(out)])
        if rc not in (0, None) or not out.exists():
            if rc not in (0, None):
                log.info("hermes export failed (%s): %s", rc, text[:200])
            return []
        rows = []
        with out.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
        return rows
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------------------
# condensing a session for the page
# --------------------------------------------------------------------------------------


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return ""


def _tool_names(calls: Any) -> list[str]:
    names = []
    for call in calls or []:
        if isinstance(call, dict):
            fn = call.get("function") if isinstance(call.get("function"), dict) else {}
            name = fn.get("name") or call.get("name")
            if name:
                names.append(str(name))
    return names


def condense(session: dict[str, Any], *, last: int = 80) -> dict[str, Any]:
    """The parts of an exported session the console shows. The system prompt is never kept."""
    msgs = []
    for m in (session.get("messages") or [])[-last:]:
        role = m.get("role")
        ts = m.get("timestamp")
        item: dict[str, Any] = {"role": role, "ts_ms": int(float(ts) * 1000) if ts else None}
        if role == "tool":
            item["tool"] = m.get("tool_name") or "tool"
            item["text"] = _text(m.get("content"))[:MAX_TOOL_TEXT]
        else:
            body = _text(m.get("content")).strip()
            item["text"] = body[:MAX_TEXT]
            item["truncated"] = len(body) > MAX_TEXT
            item["calls"] = _tool_names(m.get("tool_calls"))
            item["html"] = agent_chat.render_markdown(item["text"]) if item["text"] else ""
        if role in ("user", "assistant") and not item.get("text") and not item.get("calls"):
            continue
        msgs.append(item)
    started, active = session.get("started_at"), session.get("last_activity_at")
    final = next((m for m in reversed(msgs) if m["role"] == "assistant" and m.get("text")), None)
    return {
        "id": session.get("id"), "source": session.get("source"), "title": session.get("title"),
        "model": session.get("model"), "started_ms": int(float(started) * 1000) if started else None,
        "active_ms": int(float(active) * 1000) if active else None,
        "messages": session.get("message_count"), "tool_calls": session.get("tool_call_count"),
        "cost_usd": session.get("estimated_cost_usd"), "end_reason": session.get("end_reason"),
        "activity": session.get("last_activity_description"),
        "tools_used": sorted(set(session.get("tool_names") or []))[:30],
        "thread": msgs, "final": final,
    }


# --------------------------------------------------------------------------------------
# what the page reads
# --------------------------------------------------------------------------------------


def telegram() -> dict[str, Any]:
    """The latest Telegram conversation with the operator agent."""

    def build() -> dict[str, Any]:
        ok, why = available()
        if not ok:
            return {"available": False, "reason": why}
        rc, listing = _hermes(["sessions", "list", "--source", "telegram", "--limit", "1"])
        match = SESSION_ID.search(listing or "")
        if not match:
            return {"available": True, "session": None, "reason": "no Telegram session yet" if rc == 0 else listing[:200]}
        rows = _export(["--session-id", match.group(0)])
        return {"available": True, "session": condense(rows[0]) if rows else None,
                "reason": None if rows else "the session could not be exported"}

    return _cached("telegram", 30.0, build)


def background(hours: int = 24) -> dict[str, Any]:
    """The agent's own work while nobody is talking to it: its cron sessions, newest first."""

    def build() -> dict[str, Any]:
        ok, why = available()
        if not ok:
            return {"available": False, "reason": why, "runs": []}
        rows = _export(["--source", "cron", "--newer-than", f"{hours}h"])
        runs = sorted((condense(r, last=12) for r in rows), key=lambda r: r["active_ms"] or 0, reverse=True)
        return {"available": True, "runs": runs[:40], "total": len(runs),
                "cost_usd": sum(float(r["cost_usd"] or 0) for r in runs)}

    return _cached(f"background:{hours}", 90.0, build)


def gateways() -> list[dict[str, Any]]:
    """The three Hermes gateways' unit states, cheap enough for the status bar (``systemctl
    is-active`` only, cached 30 s). Empty where systemd is not there to ask (a dev machine)."""

    def build() -> list[dict[str, Any]]:
        out = []
        for unit in GATEWAYS:
            state = trading._systemctl("is-active", f"{unit}.service", timeout=5.0)
            out.append({"unit": unit, "name": unit.replace("hermes-gateway-kaiba-", ""),
                        "state": state.strip().splitlines()[0] if state and state.strip() else "unknown"})
        return out if any(g["state"] != "unknown" for g in out) else []

    return _cached("gateways", 30.0, build)


def status() -> dict[str, Any]:
    def build() -> dict[str, Any]:
        ok, why = available()
        gateways_now = gateways()
        usage = None
        if ok:
            rc, text = _hermes(["usage", "--json"], timeout=30)
            if rc == 0:
                try:
                    usage = json.loads(text[text.index("{"):])
                except (ValueError, json.JSONDecodeError):
                    usage = None
        return {"available": ok, "reason": why, "gateways": gateways_now, "usage": usage, "at_ms": int(time.time() * 1000)}

    return _cached("status", 60.0, build)


def cron_jobs() -> str:
    def build() -> str:
        ok, why = available()
        if not ok:
            return why
        rc, text = _hermes(["cron", "list"], timeout=30)
        return text.strip()[:20000] if text else "no output"

    return _cached("cron", 120.0, build)


# --------------------------------------------------------------------------------------
# the two owner actions
# --------------------------------------------------------------------------------------


class AgentError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def send_telegram(text: str) -> dict[str, Any]:
    """Post the owner's text to the Telegram home channel through the agent's own gateway config."""
    body = (text or "").strip()
    if not body:
        raise AgentError(400, "empty message")
    if len(body) > MAX_SEND_CHARS:
        raise AgentError(400, f"message is over {MAX_SEND_CHARS} characters")
    ok, why = available()
    if not ok:
        raise AgentError(503, why)
    rc, out = _hermes(["send", "-t", "telegram", "-f", "-", "--json"], stdin=body, timeout=40)
    if rc != 0:
        raise AgentError(502, f"hermes send failed: {out.strip()[:300]}")
    clear_caches()
    return {"ok": True, "chars": len(body)}


def set_paused(paused: bool) -> dict[str, Any]:
    """``hermes pause`` / ``hermes resume``: the agent's own emergency stop (cron dispatch and new
    gateway turns). Not a restart, not an auth change; trading protection is a separate service."""
    ok, why = available()
    if not ok:
        raise AgentError(503, why)
    rc, out = _hermes(["pause" if paused else "resume"], timeout=40)
    if rc != 0:
        raise AgentError(502, f"hermes {'pause' if paused else 'resume'} failed: {out.strip()[:300]}")
    clear_caches()
    return {"ok": True, "paused": paused, "detail": out.strip()[:300]}
