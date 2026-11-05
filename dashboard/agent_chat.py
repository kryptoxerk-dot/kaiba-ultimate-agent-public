"""Talk to the Kaiba operator agent from the console, as well as from Telegram.

Each message is one ``hermes chat`` turn on the SAME profile the Telegram gateway serves
(``kaiba-operator``: same SOUL, same rules, same Kaiba tools through the MCP socket bridge),
on a named thread of its own (``--continue dashboard-<thread> --create-if-missing``), so the
agent keeps the conversation's context between turns.

What this deliberately does NOT do (AGENTS.md, Hermes section):

* it never runs ``hermes auth``, never edits ``~/.hermes``, never restarts a gateway -- it is
  a separate short-lived process, the same way Hermes's own cron workers run;
* it never passes ``--yolo``: a command the agent wants to run that needs approval is refused
  in a non-interactive turn, never auto-approved;
* the message travels on stdin (``--query-file -``), so nothing in it is ever shell-parsed.

Output is ``--format stream-json``: one JSON object per line -- ``system/init`` (session id,
model), ``text`` chunks, tool events, then ``result`` (final text, tokens, duration). Turns
are kept in ``data/dashboard/chat/<thread>.jsonl``. Replies are rendered as Markdown with
raw HTML disabled, because a reply can quote anything the agent read on the web.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from dashboard import broker, procs, trading

log = logging.getLogger(__name__)

THREAD = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
MAX_MESSAGE_CHARS = 8_000
RUN_BUDGET_S = 600
TURN_TIMEOUT_S = 900
HISTORY_TURNS = 60
MAX_EVENT_TEXT = 600


class ChatError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Turn:
    turn_id: str
    thread: str
    user: str
    started_ms: int
    status: str = "running"
    reply: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)
    session_id: str | None = None
    model: str | None = None
    tokens: dict[str, Any] | None = None
    duration_ms: int | None = None
    exit_code: int | None = None
    finished_ms: int | None = None
    error: str | None = None

    def public(self, since_event: int = 0) -> dict[str, Any]:
        out = asdict(self)
        out["events"] = self.events[since_event:]
        out["event_count"] = len(self.events)
        out["reply_html"] = render_markdown(self.reply) if self.status != "running" else None
        return out


def render_markdown(text: str) -> str:
    """Markdown to HTML with raw HTML disabled; plain escaped text if markdown-it is missing."""
    try:
        from markdown_it import MarkdownIt

        md = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False})
        md.enable("table")
        return md.render(text or "")
    except Exception:
        import html

        return "<pre>" + html.escape(text or "") + "</pre>"


def _hermes_root() -> Path:
    return Path(os.environ.get("KAIBA_HERMES_ROOT", "~/.hermes")).expanduser()


def _profile() -> str:
    return os.environ.get("KAIBA_CHAT_PROFILE", "kaiba-operator")


def command_for(thread: str) -> tuple[list[str], dict[str, str], str | None]:
    """(argv, env, cwd) for one turn on ``thread``. ``KAIBA_CHAT_ARGV`` (a JSON list) replaces
    the Hermes command outright -- used by the tests and for a local preview with no agent."""
    override = os.environ.get("KAIBA_CHAT_ARGV")
    if override:
        return list(json.loads(override)), {}, None
    root = _hermes_root()
    profile = _profile()
    python = root / "hermes-agent" / "venv" / "bin" / "python"
    profile_dir = root / "profiles" / profile
    argv = [
        str(python), "-m", "hermes_cli.main", "--profile", profile, "chat",
        "--query-file", "-",
        "--continue", f"dashboard-{thread}", "--create-if-missing",
        "--source", "dashboard",
        "--format", "stream-json",
        "--run-budget", str(RUN_BUDGET_S),
        "--max-turns", "60",
    ]
    home = str(Path.home())
    env = {
        "HERMES_HOME": str(profile_dir),
        "VIRTUAL_ENV": str(root / "hermes-agent" / "venv"),
        "PATH": ":".join([str(root / "hermes-agent" / "venv" / "bin"),
                          str(root / "hermes-agent" / "node_modules" / ".bin"),
                          f"{home}/.local/bin", "/usr/local/bin", "/usr/bin", "/bin"]),
    }
    return argv, env, str(profile_dir)


def available() -> tuple[bool, str]:
    if os.environ.get("KAIBA_CHAT_ARGV"):
        return True, "test command"
    argv, _env, cwd = command_for("main")
    if not Path(argv[0]).exists():
        return False, "the Hermes agent is not installed on this machine"
    if cwd and not Path(cwd).is_dir():
        return False, f"Hermes profile {_profile()!r} not found"
    return True, f"Hermes profile {_profile()}"


def _short(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= MAX_EVENT_TEXT else text[: MAX_EVENT_TEXT - 1] + "…"


class ChatService:
    """One turn at a time: the operator agent answers in order, and one quota is not spent twice."""

    def __init__(self, *, chat_dir: Path | None = None, emit=None):
        self._chat_dir = chat_dir
        self._emit = emit
        self._lock = threading.Lock()
        self._current: Turn | None = None
        self._proc = None
        self._unit: str | None = None

    def chat_dir(self) -> Path:
        if self._chat_dir is None:
            self._chat_dir = trading._data_dir() / "dashboard" / "chat"
        return self._chat_dir

    # ------------------------------------------------------------------ reading

    def threads(self) -> list[str]:
        try:
            names = sorted(p.stem for p in self.chat_dir().glob("*.jsonl") if THREAD.match(p.stem))
        except OSError:
            names = []
        return names or ["main"]

    def history(self, thread: str, limit: int = HISTORY_TURNS) -> list[dict[str, Any]]:
        if not THREAD.match(thread or ""):
            return []
        path = self.chat_dir() / f"{thread}.jsonl"
        turns: list[dict[str, Any]] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
        except OSError:
            lines = []
        for line in lines:
            try:
                turn = json.loads(line)
            except ValueError:
                continue
            turn["reply_html"] = render_markdown(turn.get("reply") or "")
            turns.append(turn)
        current = self._current
        if current is not None and current.thread == thread and current.status == "running":
            turns.append(current.public())
        return turns

    def current(self) -> Turn | None:
        return self._current

    def get(self, turn_id: str) -> Turn | None:
        current = self._current
        return current if current is not None and current.turn_id == turn_id else None

    # ------------------------------------------------------------------ writing

    def send(self, thread: str, message: str) -> Turn:
        thread = (thread or "main").strip().lower()
        if not THREAD.match(thread):
            raise ChatError(400, "thread names are lowercase letters, digits and dashes (max 32)")
        message = (message or "").strip()
        if not message:
            raise ChatError(400, "empty message")
        if len(message) > MAX_MESSAGE_CHARS:
            raise ChatError(400, f"message is over {MAX_MESSAGE_CHARS} characters")
        ok, why = available()
        if not ok:
            raise ChatError(503, why)
        with self._lock:
            if self._current is not None and self._current.status == "running":
                raise ChatError(409, "the agent is still answering the previous message")
            turn = Turn(turn_id=f"{time.strftime('%m%d-%H%M%S')}-{secrets.token_hex(3)}", thread=thread,
                        user=message, started_ms=int(time.time() * 1000))
            self._current = turn
        if self._emit:
            self._emit("system", {"event": "dashboard_chat", "action": "send", "thread": thread,
                                  "turn_id": turn.turn_id, "chars": len(message), "by": "operator"})
        threading.Thread(target=self._run, args=(turn,), name=f"chat-{turn.turn_id}", daemon=True).start()
        return turn

    def stop(self) -> bool:
        turn = self._current
        if turn is None or turn.status != "running":
            return False
        turn.error = "stopped by the operator"
        procs.stop(self._proc, self._unit)
        return True

    # ------------------------------------------------------------------ the turn

    def _run(self, turn: Turn) -> None:
        argv, env, cwd = command_for(turn.thread)
        unit = procs.unit_name("kaiba-dash-chat")
        self._unit = unit
        prompt = f"[Message from the owner via the Kaiba web dashboard, thread '{turn.thread}']\n\n{turn.user}"
        stderr_tail: list[str] = []
        try:
            if broker.active():
                proc = broker.chat(turn.thread, unit)
            else:
                proc = procs.spawn(argv, unit=unit, stdin=True, merge_stderr=False, memory_max="2G",
                                   runtime_max_s=TURN_TIMEOUT_S, cwd=cwd, env=env)
            self._proc = proc
            assert proc.stdin is not None and proc.stdout is not None
            proc.stdin.write(prompt)
            proc.stdin.close()

            def drain_stderr() -> None:
                for line in proc.stderr or []:
                    stderr_tail.append(line.rstrip())
                    del stderr_tail[:-30]

            threading.Thread(target=drain_stderr, daemon=True).start()
            deadline = time.time() + TURN_TIMEOUT_S

            def watchdog() -> None:
                while proc.poll() is None:
                    if time.time() > deadline:
                        turn.error = f"no answer within {TURN_TIMEOUT_S} s"
                        procs.stop(proc, unit)
                        return
                    time.sleep(1)

            threading.Thread(target=watchdog, daemon=True).start()
            for line in proc.stdout:
                self._consume(turn, line)
            proc.wait()
            turn.exit_code = proc.returncode if turn.exit_code is None else turn.exit_code
        except Exception as exc:
            log.exception("dashboard chat turn failed")
            turn.error = f"{type(exc).__name__}: {exc}"
        finally:
            if turn.error is None and (turn.exit_code not in (0, None) or not turn.reply):
                tail = " | ".join(line for line in stderr_tail[-5:] if line)
                turn.error = f"the agent exited with code {turn.exit_code}" + (f": {tail}" if tail else "")
            turn.status = "done" if turn.error is None else "failed"
            turn.finished_ms = int(time.time() * 1000)
            self._save(turn)
            if self._emit:
                self._emit("system", {"event": "dashboard_chat", "action": "reply", "thread": turn.thread,
                                      "turn_id": turn.turn_id, "status": turn.status,
                                      "duration_ms": turn.duration_ms})
            self._proc, self._unit = None, None

    def _consume(self, turn: Turn, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            ev = json.loads(line)
        except ValueError:
            turn.events.append({"kind": "log", "text": _short(line)})
            return
        if not isinstance(ev, dict):
            return
        kind = str(ev.get("type") or "event")
        if kind == "system":
            turn.session_id = ev.get("session_id") or turn.session_id
            turn.model = ev.get("model") or turn.model
            turn.events.append({"kind": "start", "text": f"{turn.model or 'agent'} is thinking"})
        elif kind == "text":
            chunk = str(ev.get("text") or "")
            turn.reply += chunk
            turn.events.append({"kind": "text", "text": chunk})
        elif kind == "result":
            final = ev.get("text")
            if isinstance(final, str) and final.strip():
                turn.reply = final
            turn.session_id = ev.get("session_id") or turn.session_id
            turn.tokens = ev.get("tokens") if isinstance(ev.get("tokens"), dict) else None
            turn.duration_ms = ev.get("duration_ms")
            turn.exit_code = ev.get("exit_code") if isinstance(ev.get("exit_code"), int) else turn.exit_code
            turn.events.append({"kind": "result", "text": "done"})
        elif "tool" in kind:
            name = ev.get("name") or ev.get("tool") or ev.get("tool_name") or ""
            detail = ev.get("input") or ev.get("args") or ev.get("arguments") or ev.get("output") or ev.get("result") or ""
            turn.events.append({"kind": "tool", "text": f"{kind}: {name} {_short(detail)}".strip()})
        elif kind == "error":
            turn.events.append({"kind": "error", "text": _short(ev.get("message") or ev.get("text") or ev)})
        else:
            turn.events.append({"kind": kind, "text": _short({k: v for k, v in ev.items() if k != "type"})})
        del turn.events[:-400]

    def _save(self, turn: Turn) -> None:
        try:
            folder = self.chat_dir()
            folder.mkdir(parents=True, exist_ok=True)
            record = asdict(turn)
            record["events"] = [e for e in turn.events if e.get("kind") not in ("text",)][-80:]
            with (folder / f"{turn.thread}.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str) + "\n")
        except OSError as exc:
            log.warning("dashboard: could not save chat turn %s: %s", turn.turn_id, exc)
