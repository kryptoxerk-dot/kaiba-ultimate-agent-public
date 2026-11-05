"""The commands page: named actions the operator can run from the browser.

This is deliberately NOT a shell. The browser sends a command *name* (and, for a few, one
argument chosen from a list the server produced); the server maps it to a fixed argv. That
is the whole security model, so it is worth stating why it is not more permissive: the
console is reachable from the internet behind one password, on a box that holds live
trading keys. A free-form shell there is one leaked password away from an emptied wallet.
Everything an operator needs day to day is below, and new actions are added as reviewed
code -- either an entry in :func:`builtin_commands` or a script in ``scripts/dashboard/``.

Kinds of command:

* ``internal`` -- read-only, answered in-process (status, orders in flight, reports, logs);
* ``unit``     -- start one of the box's own systemd units now (the same thing its timer does);
* ``restart``  -- restart one allowlisted service, typed confirmation, and REFUSED while any
  order is in flight (the lead's deploy protocol, enforced instead of remembered);
* ``script``   -- run a reviewed script from ``scripts/dashboard/`` under nice/ionice and a
  hard timeout (DATABASE READ RULE: at most 20 minutes).

Every run is recorded (``data/dashboard/runs/<id>.json``) and announced on the event bus,
so the agent stream shows who started what. There is no withdraw, transfer or lane-arming
command, and no Hermes auth or gateway action (AGENTS.md: never touch those).
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dashboard import broker, procs, trading

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts" / "dashboard"
MAX_CONCURRENT = 2
MAX_LINES = 5_000
MAX_CHARS = 400_000
MAX_SCRIPT_TIMEOUT_S = 1_200
KEEP_RUN_FILES = 200
POLL_S = 3.0

#: Services the page may restart. Not the console itself (it would cut its own request), not
#: kaiba-mcp (the Hermes tool bridge), not tweet-launch (owner-stopped), never hermes-*.
RESTARTABLE = (
    "kaiba-scan", "kaiba-snipe", "kaiba-engine", "kaiba-ops", "kaiba-protection",
    "kaiba-ingest", "kaiba-shadow-tape", "kaiba-notify", "kaiba-social-follow",
    "kaiba-social-hunt", "kaiba-tweet-sources",
)
#: Units whose log may be read. A pattern, checked against the live unit list as well.
LOG_UNIT = re.compile(r"^(kaiba-[a-z0-9-]+|hermes-gateway-kaiba-[a-z]+|claude-runner-[a-z]+)\.service$")
SCRIPT_FILE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,48}\.(py|sh)$")
SCRIPT_HEADER = re.compile(r"^#\s*dashboard:\s*(label|about|confirm|timeout)\s*=\s*(.+?)\s*$", re.M)
SECRET_NAME = re.compile(r"PASS|KEY|SECRET|TOKEN|PRIVATE|SEED|MNEMONIC", re.I)


class CommandError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class Command:
    name: str
    label: str
    group: str
    about: str
    kind: str
    target: str = ""
    confirm: bool = False
    timeout_s: int = 120
    wait: bool = True
    arg_label: str = ""
    choices: tuple[str, ...] = ()

    def public(self) -> dict[str, Any]:
        return {"name": self.name, "label": self.label, "group": self.group, "about": self.about,
                "kind": self.kind, "confirm": self.confirm, "arg_label": self.arg_label,
                "choices": list(self.choices), "timeout_s": self.timeout_s}


def _log_units() -> tuple[str, ...]:
    live = [u["unit"] for u in trading.units() if LOG_UNIT.match(u["unit"])]
    if live:
        return tuple(sorted(set(live)))
    return tuple(f"{u}.service" for u in RESTARTABLE)


def builtin_commands() -> list[Command]:
    return [
        Command("status", "Service status", "Status",
                "Every kaiba / hermes / wallet-hunt unit, disk, WAL, backups and orders in flight.",
                "internal"),
        Command("orders", "Orders in flight", "Status",
                "Orders not yet settled. A restart is refused while this is non-zero.", "internal"),
        Command("positions", "Open positions", "Status", "Every open live and paper position, oldest first.",
                "internal"),
        Command("errors", "Recent errors (2 h)", "Status",
                "Error-level journal lines from every user service in the last two hours.", "internal"),
        Command("logs", "Service log", "Status", "The last 200 lines of one service's journal.",
                "internal", arg_label="service", choices=_log_units()),
        Command("paper_report", "Latest paper report", "Reports",
                "The daily paper report exactly as it was last sent to Telegram.", "internal"),
        Command("twin_report", "Exit twins", "Reports",
                "The paired early-exit study: which exit beats the current one, with cost stress.", "internal"),
        Command("hunt_status", "Wallet hunt status", "Reports",
                "What each chain's last wallet hunt found and exported.", "internal"),
        Command("twin_run", "Run exit twins now", "Research",
                "Re-scores every closed paper position against the early exits (normally every 2 h).",
                "unit", target="kaiba-twin-exits.service", timeout_s=900),
        Command("hunt_run", "Run wallet hunt now", "Research",
                "Finds early buyers and snipers of recent runner tokens and refreshes the GMGN import "
                "lists (normally hourly).", "unit", target="claude-runner-hunt.service", timeout_s=1_500),
        Command("snowball_run", "Run wallet snowball now", "Research",
                "Widens the wallet hunt from the current winners (normally 03:20). Runs in the background.",
                "unit", target="claude-runner-snowball.service", wait=False),
        Command("hightier_run", "Run high-tier wallet screen now", "Research",
                "Re-screens Solana wallets for the top-tier list (normally 04:30). Runs in the background.",
                "unit", target="claude-runner-hightier.service", wait=False),
        Command("backup_run", "Start a core backup", "Maintenance",
                "Snapshots the irreplaceable tables (normally 06:50). Takes up to a few hours; runs in "
                "the background at idle priority.", "unit", target="kaiba-core-backup.service", wait=False),
        Command("restart", "Restart a service", "Maintenance",
                "Restarts one service. Refused while any order is in flight. Type the service name "
                "to confirm. Restarting kaiba-protection leaves open positions unwatched for ~10 s.",
                "restart", confirm=True, arg_label="service", choices=RESTARTABLE, timeout_s=90),
    ]


def script_commands() -> list[Command]:
    """Scripts that opted in with a ``# dashboard: label = ...`` header."""
    if not SCRIPTS_DIR.is_dir():
        return []
    out = []
    for path in sorted(SCRIPTS_DIR.iterdir()):
        if not path.is_file() or not SCRIPT_FILE.match(path.name):
            continue
        try:
            head = path.read_text(encoding="utf-8", errors="replace")[:4_000]
        except OSError:
            continue
        meta = {k: v for k, v in SCRIPT_HEADER.findall(head)}
        if "label" not in meta:
            continue
        try:
            timeout = int(meta.get("timeout", "600"))
        except ValueError:
            timeout = 600
        out.append(Command(
            name=f"script-{path.stem}", label=meta["label"], group="Scripts",
            about=meta.get("about", ""), kind="script", target=path.name,
            confirm=meta.get("confirm", "no").strip().lower() in ("yes", "true", "1"),
            timeout_s=max(10, min(timeout, MAX_SCRIPT_TIMEOUT_S)),
        ))
    return out


def all_commands() -> list[Command]:
    return builtin_commands() + script_commands()


# --------------------------------------------------------------------------------------
# runs
# --------------------------------------------------------------------------------------


@dataclass
class Run:
    run_id: str
    name: str
    label: str
    arg: str | None
    started_ms: int
    status: str = "running"
    exit_code: int | None = None
    finished_ms: int | None = None
    lines: list[str] = field(default_factory=list)
    truncated: bool = False
    chars: int = 0

    def add(self, text: str) -> None:
        for line in str(text).splitlines() or [""]:
            if self.truncated:
                return
            if len(self.lines) >= MAX_LINES or self.chars + len(line) > MAX_CHARS:
                self.truncated = True
                self.lines.append("… output truncated (the full log is in the service journal)")
                return
            self.lines.append(line.rstrip("\r"))
            self.chars += len(line)

    def to_dict(self, since_line: int = 0) -> dict[str, Any]:
        return {"run_id": self.run_id, "name": self.name, "label": self.label, "arg": self.arg,
                "started_ms": self.started_ms, "finished_ms": self.finished_ms, "status": self.status,
                "exit_code": self.exit_code, "truncated": self.truncated, "line_count": len(self.lines),
                "lines": self.lines[since_line:]}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _default_emit(kind: str, payload: dict[str, Any], level: str = "info") -> None:
    """Announce on the event bus from a worker thread (its own short connection)."""
    try:
        from kaiba.core import db, events

        with db.session() as conn:
            events.emit(kind, payload, level=level, subject="dashboard", conn=conn)
    except Exception as exc:
        log.warning("dashboard: could not emit %s: %s", kind, exc)


def _orders_in_flight() -> int | None:
    """A fresh read, not the page cache: this number gates a restart."""
    try:
        from kaiba.core import db

        with db.session() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM orders WHERE state IN "
                "('planned','reserved','submitting','submitted','partial','unknown')"
            ).fetchone()
        return int(row[0])
    except Exception as exc:
        log.warning("dashboard: orders in flight unreadable: %s", exc)
        return None


def _run_capture(argv: list[str], timeout: float = 30.0) -> tuple[int | None, str]:
    if broker.active():
        code, out, err = broker.capture(argv, timeout)
        return code, out + err
    try:
        done = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, check=False)
    except FileNotFoundError:
        return None, f"{argv[0]} is not available on this machine"
    except subprocess.TimeoutExpired:
        return None, f"{argv[0]} timed out after {timeout:.0f} s"
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def script_argv(name: str, timeout_s: int) -> tuple[list[str], dict[str, str]]:
    """(argv, env) of a reviewed script: idle priority, a hard timeout, no secret in the env."""
    path = SCRIPTS_DIR / name
    venv = REPO_ROOT / ".venv" / "bin" / "python"
    interpreter = [str(venv) if venv.exists() else sys.executable] if path.suffix == ".py" else ["bash"]
    argv = [*interpreter, str(path)]
    if sys.platform.startswith("linux"):
        argv = ["nice", "-n", "19", "ionice", "-c3", "timeout", str(timeout_s), *argv]
    env = {k: v for k, v in os.environ.items() if k.startswith("KAIBA_") and not SECRET_NAME.search(k)}
    env["PYTHONUNBUFFERED"] = "1"
    return argv, env


def _journal_since(unit: str, since_s: float, cursor: str | None) -> tuple[list[str], str | None]:
    argv = ["journalctl", "--user", "-u", unit, "--no-pager", "-o", "short-iso", "--show-cursor"]
    argv += ["--after-cursor", cursor] if cursor else ["--since", f"@{int(since_s)}"]
    _code, text = _run_capture(argv, timeout=20)
    lines, new_cursor = [], cursor
    for line in text.splitlines():
        if line.startswith("-- cursor: "):
            new_cursor = line[len("-- cursor: "):].strip()
        elif line and not line.startswith("-- No entries --"):
            lines.append(line)
    return lines, new_cursor


def _unit_state(unit: str) -> str:
    _code, text = _run_capture(["systemctl", "--user", "show", "-p", "ActiveState", "--value", unit], 10)
    return text.strip() or "unknown"


def _fmt_pct(x: Any) -> str:
    return "—" if x is None else f"{100 * float(x):+.1f}%"


class Runner:
    """Starts, follows and remembers command runs. One instance per console process."""

    def __init__(self, *, emit: Callable[..., None] | None = None, runs_dir: Path | None = None):
        self._emit = emit or _default_emit
        self._runs_dir = runs_dir
        self._lock = threading.Lock()
        self._runs: dict[str, Run] = {}

    # ------------------------------------------------------------------ registry

    def commands(self) -> list[Command]:
        return all_commands()

    def find(self, name: str) -> Command | None:
        return next((c for c in self.commands() if c.name == name), None)

    # ------------------------------------------------------------------ lifecycle

    def runs_dir(self) -> Path:
        if self._runs_dir is None:
            self._runs_dir = trading._data_dir() / "dashboard" / "runs"
        return self._runs_dir

    def start(self, name: str, arg: str | None = None, confirm: str | None = None) -> Run:
        cmd = self.find(str(name or ""))
        if cmd is None:
            raise CommandError(400, f"unknown command {name!r}")
        arg = (arg or "").strip() or None
        if cmd.choices:
            if arg not in cmd.choices:
                raise CommandError(400, f"{cmd.label}: choose one of the listed {cmd.arg_label or 'options'}")
        elif arg is not None:
            raise CommandError(400, f"{cmd.label} takes no argument")
        if cmd.confirm:
            expected = arg or cmd.name
            if (confirm or "").strip() != expected:
                raise CommandError(400, f"type {expected!r} to confirm")
        with self._lock:
            running = [r for r in self._runs.values() if r.status == "running"]
            if any(r.name == cmd.name for r in running):
                raise CommandError(409, f"{cmd.label} is already running")
            if len(running) >= MAX_CONCURRENT:
                raise CommandError(409, f"{len(running)} commands are already running; wait for one to finish")
            run = Run(run_id=f"{time.strftime('%m%d-%H%M%S')}-{secrets.token_hex(3)}", name=cmd.name,
                      label=cmd.label, arg=arg, started_ms=_now_ms())
            self._runs[run.run_id] = run
            while len(self._runs) > 50:
                oldest = next(iter(self._runs))
                if self._runs[oldest].status == "running":
                    break
                self._runs.pop(oldest)
        self._emit("system", {"event": "dashboard_command", "action": "start", "command": cmd.name,
                              "arg": arg, "run_id": run.run_id, "by": "operator"})
        threading.Thread(target=self._execute, args=(run, cmd), name=f"cmd-{run.run_id}", daemon=True).start()
        return run

    def get(self, run_id: str) -> Run | None:
        with self._lock:
            run = self._runs.get(run_id)
        if run is not None:
            return run
        if not re.fullmatch(r"[0-9]{4}-[0-9]{6}-[0-9a-f]{6}", run_id or ""):
            return None
        try:
            raw = json.loads((self.runs_dir() / f"{run_id}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return Run(**{k: raw.get(k) for k in ("run_id", "name", "label", "arg", "started_ms", "status",
                                               "exit_code", "finished_ms", "truncated")},
                   lines=list(raw.get("lines") or []))

    def recent(self, limit: int = 30) -> list[dict[str, Any]]:
        seen: dict[str, dict[str, Any]] = {}
        with self._lock:
            for run in self._runs.values():
                seen[run.run_id] = {k: v for k, v in run.to_dict().items() if k != "lines"}
        try:
            files = sorted(self.runs_dir().glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            files = []
        for path in files[: limit * 2]:
            if path.stem in seen:
                continue
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            raw.pop("lines", None)
            seen[path.stem] = raw
        rows = sorted(seen.values(), key=lambda r: r.get("started_ms") or 0, reverse=True)
        return rows[:limit]

    # ------------------------------------------------------------------ execution

    def _execute(self, run: Run, cmd: Command) -> None:
        try:
            if cmd.kind == "internal":
                self._internal(run, cmd)
            elif cmd.kind == "unit":
                self._unit(run, cmd)
            elif cmd.kind == "restart":
                self._restart(run, cmd)
            elif cmd.kind == "script":
                self._script(run, cmd)
            else:  # pragma: no cover - the registry only builds the kinds above
                run.add(f"unknown kind {cmd.kind}")
                run.status = "failed"
            if run.status == "running":
                run.status = "ok"
        except Exception as exc:  # a broken command must end as a failed run, never hang the page
            log.exception("dashboard command %s failed", cmd.name)
            run.add(f"error: {type(exc).__name__}: {exc}")
            run.status = "failed"
        finally:
            run.finished_ms = _now_ms()
            self._persist(run)
            self._emit("system", {"event": "dashboard_command", "action": "finish", "command": cmd.name,
                                  "arg": run.arg, "run_id": run.run_id, "status": run.status},
                       level="info" if run.status in ("ok", "started") else "warn")

    def _persist(self, run: Run) -> None:
        try:
            folder = self.runs_dir()
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"{run.run_id}.json").write_text(json.dumps(run.to_dict(), default=str), encoding="utf-8")
            files = sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime)
            for old in files[:-KEEP_RUN_FILES]:
                old.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("dashboard: could not record run %s: %s", run.run_id, exc)

    # internal ---------------------------------------------------------------------------

    def _internal(self, run: Run, cmd: Command) -> None:
        name = cmd.name
        if name == "status":
            trading._cache.pop("health", None)
            h = trading.system_health()
            disk = h.get("disk") or {}
            run.add(f"disk: {disk.get('used_pct', '?')}% used, {disk.get('free_gb', '?')} GB free"
                    f" | WAL {h.get('wal_gb', '?')} GB | orders in flight: {h.get('orders_in_flight', '?')}")
            newest = h.get("backup_newest") or {}
            last = h.get("backup_last") or {}
            run.add(f"backup: newest {newest.get('name', 'none')} | last run {last.get('status', '?')} "
                    f"{last.get('ts_utc', '')} {last.get('error', '')}".rstrip())
            if not h.get("units"):
                run.add("unit list unavailable on this machine")
            for u in h.get("units") or []:
                mark = "!!" if u["active"] == "failed" else "  "
                run.add(f"{mark} {u['unit']:<46} {u['active']:<10} {u['sub']}")
        elif name == "orders":
            n = _orders_in_flight()
            run.add(f"orders in flight: {n if n is not None else 'unreadable'}")
            from dashboard import data

            for r in data._rows(
                "SELECT order_id, chain, lane, side, state, mode, created_ms FROM orders WHERE state IN "
                "('planned','reserved','submitting','submitted','partial','unknown') ORDER BY created_ms DESC LIMIT 50"
            ):
                run.add(f"{r.get('order_id')} {r.get('chain')} {r.get('lane')} {r.get('side')} "
                        f"{r.get('state')} {r.get('mode')}")
        elif name == "positions":
            from dashboard import data

            rows = sorted(data.open_positions(limit=500), key=lambda p: p["opened_ms"] or 0)
            run.add(f"{len(rows)} open positions")
            now = _now_ms()
            for p in rows:
                age_h = (now - (p["opened_ms"] or now)) / 3_600_000
                run.add(f"{p['mode']:<6} {p['chain']:<9} {p['lane']:<15} {p['token']}  age {age_h:6.1f} h  "
                        f"cost {p['cost_native']} {p['symbol_native']}")
        elif name == "errors":
            _code, text = _run_capture(["journalctl", "--user", "-p", "err", "--since", "-2h", "--no-pager",
                                        "-o", "short-iso", "-n", "300"], timeout=30)
            run.add(text or "no error-level lines in the last two hours")
        elif name == "logs":
            _code, text = _run_capture(["journalctl", "--user", "-u", str(run.arg), "-n", "200", "--no-pager",
                                        "-o", "short-iso"], timeout=30)
            run.add(text or "no journal lines")
        elif name == "paper_report":
            rep = trading.latest_paper_report()
            run.add(rep.get("text") or rep.get("reason") or "no report")
        elif name == "twin_report":
            twin = trading.twin_exits()
            if not twin.get("available"):
                run.add(twin.get("reason") or "no twin report")
            for r in twin.get("rows") or []:
                run.add(f"{r.get('verdict', '?'):<7} {r.get('lane')}/{r.get('chain')} {r.get('challenger')}: "
                        f"n={r.get('pairs')} challenger {_fmt_pct(r.get('challenger_mean'))} vs now "
                        f"{_fmt_pct(r.get('incumbent_model_mean'))}, paired {_fmt_pct(r.get('paired_diff_mean'))} "
                        f"(low90 {_fmt_pct(r.get('paired_diff_lower90'))})"
                        + (f", +{r.get('stress_bps')}bps/leg {'holds' if r.get('survives_stress') else 'FAILS'}"
                           if r.get("stress_bps") else ""))
        elif name == "hunt_status":
            for h in trading.hunt_status() or [{"note": "no wallet hunt has run on this machine"}]:
                run.add(json.dumps(h, default=str))

    # units ------------------------------------------------------------------------------

    def _unit(self, run: Run, cmd: Command) -> None:
        unit = cmd.target
        since = time.time()
        state = _unit_state(unit)
        proc = None
        if state in ("active", "activating", "reloading"):
            run.add(f"{unit} is already running; following its log")
        elif cmd.wait:
            run.add(f"starting {unit} …")
            if broker.active():
                proc = broker.start_unit(unit)
            else:
                proc = subprocess.Popen(["systemctl", "--user", "start", unit], stdout=subprocess.DEVNULL,
                                        stderr=subprocess.PIPE, text=True)
        else:
            code, text = _run_capture(["systemctl", "--user", "start", "--no-block", unit], timeout=30)
            run.add(f"started {unit} in the background" if code == 0 else f"could not start {unit}: {text.strip()}")
            if code != 0:
                run.status, run.exit_code = "failed", code
                return
        cursor = None
        follow_until = since + (cmd.timeout_s if cmd.wait else 60)
        while True:
            lines, cursor = _journal_since(unit, since, cursor)
            for line in lines:
                run.add(line)
            if proc is not None and proc.poll() is not None:
                time.sleep(1)
                for line in _journal_since(unit, since, cursor)[0]:
                    run.add(line)
                run.exit_code = proc.returncode
                run.status = "ok" if proc.returncode == 0 else "failed"
                if proc.returncode != 0:
                    run.add((proc.stderr.read() if proc.stderr else "").strip() or f"{unit} failed")
                return
            if proc is None and state not in ("active", "activating", "reloading") and not cmd.wait \
                    and time.time() > follow_until:
                run.add(f"{unit} keeps running in the background; check 'Service status' or its log later")
                run.status = "started"
                return
            if proc is None and _unit_state(unit) in ("inactive", "failed") and time.time() > since + 5:
                final = _unit_state(unit)
                run.add(f"{unit} finished: {final}")
                run.status = "ok" if final == "inactive" else "failed"
                return
            if time.time() > follow_until:
                run.add(f"stopped following after {cmd.timeout_s} s; {unit} keeps running")
                run.status = "started"
                return
            time.sleep(POLL_S)

    # restart ----------------------------------------------------------------------------

    def _restart(self, run: Run, cmd: Command) -> None:
        unit = f"{run.arg}.service"
        n = _orders_in_flight()
        if n is None:
            run.add("refused: the orders table could not be read, so it is unknown whether an order is in flight")
            run.status = "refused"
            return
        if n > 0:
            run.add(f"refused: {n} order(s) in flight. A restart mid-order can strand it; try again shortly.")
            run.status = "refused"
            return
        since = time.time()
        run.add(f"0 orders in flight; restarting {unit} …")
        code, text = _run_capture(["systemctl", "--user", "restart", unit], timeout=cmd.timeout_s)
        if code != 0:
            run.add(text.strip() or f"restart failed (exit {code})")
            run.status, run.exit_code = "failed", code
            return
        time.sleep(8)
        _c, show = _run_capture(["systemctl", "--user", "show", "-p", "ActiveState", "-p", "SubState",
                                 "-p", "ActiveEnterTimestamp", unit], timeout=10)
        run.add(show.strip())
        for line in _journal_since(unit, since, None)[0][-40:]:
            run.add(line)
        run.status = "ok" if "ActiveState=active" in show else "failed"

    # scripts ----------------------------------------------------------------------------

    def _script(self, run: Run, cmd: Command) -> None:
        path = SCRIPTS_DIR / cmd.target
        if not SCRIPT_FILE.match(cmd.target) or not path.is_file():
            run.add("script not found")
            run.status = "failed"
            return
        unit = procs.unit_name("kaiba-dash-run")
        if broker.active():
            proc = broker.script(cmd.target, unit)
        else:
            argv, env = script_argv(cmd.target, cmd.timeout_s)
            proc = procs.spawn(argv, unit=unit, memory_max="1G", runtime_max_s=cmd.timeout_s + 60,
                               cwd=str(REPO_ROOT), env=env)
        deadline = time.time() + cmd.timeout_s + 30

        def watchdog() -> None:
            while proc.poll() is None:
                if time.time() > deadline:
                    run.add(f"timed out after {cmd.timeout_s} s; stopping")
                    run.status = "timeout"
                    procs.stop(proc, unit)
                    return
                time.sleep(1)

        threading.Thread(target=watchdog, daemon=True).start()
        assert proc.stdout is not None
        for line in proc.stdout:
            run.add(line)
        proc.wait()
        run.exit_code = proc.returncode
        if run.status == "running":
            run.status = "ok" if proc.returncode == 0 else "failed"
