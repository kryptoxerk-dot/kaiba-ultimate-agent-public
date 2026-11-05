"""The console's only way out of its wall.

:mod:`dashboard.wall` shuts the web process off from the keys, from other processes and from the
network. The console still has a few jobs that need the outside: reading unit state and journals,
starting units, the Hermes chat, the commands page, the GMGN account check and saving the risk
file. This module runs those for it.

``python -m dashboard.broker`` is the unit's main process. It forks once. The child builds the wall
and serves HTTP. The parent keeps no port and answers only the named operations below, over a
socket the two share from the fork. It never takes a program to run: every argv it starts is either
built here from fixed parts or matched exactly against a shape the console really uses, so a
compromised web process can do what the console password can do, and no more. A refusal means a
bug or an attacker, so each one is logged.

With no ``KAIBA_BROKER_FD`` in the environment (tests, a dev machine) :func:`active` is false and
the console runs its commands directly, as it always has.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import select
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from dashboard import wall

ENV_FD = "KAIBA_BROKER_FD"
#: Hermes writes session exports here for the web process to read and delete.
EXPORT_DIR = "/dev/shm/kaiba-dash"
MAX_REQUEST = 1 << 20
MAX_TIMEOUT_S = 1_300.0
MAX_CALLS = 32
INODE_CHECK_S = 20.0

_CURSOR = re.compile(r"^[A-Za-z0-9=;]{1,200}$")
_SESSION = re.compile(r"^[0-9]{8}_[0-9]{6}_[0-9a-f]{6,64}$")
_SINCE_EPOCH = re.compile(r"^@[0-9]{1,12}$")
_NEWER_THAN = re.compile(r"^[0-9]{1,4}h$")
_CHILD_UNIT = re.compile(r"^kaiba-dash-(chat|run)-[0-9a-f]{8}$")
_EXPORT_FILE = re.compile(r"^" + re.escape(EXPORT_DIR) + r"/kaiba-hx-[a-z0-9_]{8}/sessions\.jsonl$")


class BrokerError(RuntimeError):
    """The broker refused a request or could not be reached."""


class Refused(Exception):
    """A request that is not one of the shapes the console uses."""


# --------------------------------------------------------------------------------------
# the web process's side
# --------------------------------------------------------------------------------------

_control: socket.socket | None = None
_control_lock = threading.Lock()


def active() -> bool:
    """True inside the walled web process."""
    return ENV_FD in os.environ


def _channel() -> socket.socket:
    """A private socket to the broker for one operation."""
    global _control
    mine, theirs = socket.socketpair()
    try:
        with _control_lock:
            if _control is None:
                _control = socket.socket(fileno=int(os.environ[ENV_FD]))
            socket.send_fds(_control, [b"c"], [theirs.fileno()])
    except BaseException:
        mine.close()
        raise
    finally:
        theirs.close()
    return mine


def _encode(request: dict[str, Any]) -> bytes:
    return json.dumps(request).encode("utf-8") + b"\n"


def _call(request: dict[str, Any]) -> dict[str, Any]:
    """One operation, one reply."""
    try:
        with _channel() as channel:
            channel.sendall(_encode(request))
            chunks = []
            while chunk := channel.recv(1 << 16):
                chunks.append(chunk)
    except OSError as exc:
        raise BrokerError(f"the broker cannot be reached: {exc}") from exc
    if not chunks:
        raise BrokerError("the broker closed the channel")
    reply = json.loads(b"".join(chunks))
    if "error" in reply:
        raise BrokerError(reply["error"])
    return reply


class RemoteProcess:
    """The part of ``subprocess.Popen`` the console uses, for a process the broker started."""

    def __init__(self, channel: socket.socket, pipes: dict[str, int]) -> None:
        self._channel = channel
        self._lock = threading.Lock()
        self.returncode: int | None = None
        self.stdin = self._text(pipes, "stdin", "w")
        self.stdout = self._text(pipes, "stdout", "r")
        self.stderr = self._text(pipes, "stderr", "r")

    @staticmethod
    def _text(pipes: dict[str, int], name: str, mode: str) -> Any:
        if name not in pipes:
            return None
        return os.fdopen(pipes[name], mode, buffering=1, encoding="utf-8", errors="replace")

    def _collect(self) -> None:
        with self._lock:
            if self.returncode is not None:
                return
            chunks = []
            while chunk := self._channel.recv(4096):
                chunks.append(chunk)
            self._channel.close()
            self.returncode = int(json.loads(b"".join(chunks))["returncode"]) if chunks else -1

    def poll(self) -> int | None:
        if self.returncode is None and select.select([self._channel], [], [], 0)[0]:
            self._collect()
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            if not select.select([self._channel], [], [], timeout)[0]:
                raise subprocess.TimeoutExpired("broker process", timeout or 0.0)
            self._collect()
        assert self.returncode is not None
        return self.returncode

    def _tell(self, word: bytes) -> None:
        if self.returncode is None:
            try:
                self._channel.sendall(word)
            except OSError:
                pass

    def terminate(self) -> None:
        self._tell(b"terminate\n")

    def kill(self) -> None:
        self._tell(b"kill\n")


def _open(request: dict[str, Any]) -> RemoteProcess:
    """One operation that starts a process and hands its pipes back."""
    channel = _channel()
    try:
        channel.sendall(_encode(request))
        head, fds, _flags, _addr = socket.recv_fds(channel, 4096, 3)
        if not head:
            raise BrokerError("the broker closed the channel")
        reply = json.loads(head)
        if "error" in reply:
            raise BrokerError(reply["error"])
    except BaseException:
        channel.close()
        raise
    return RemoteProcess(channel, dict(zip(reply["pipes"], fds, strict=True)))


def capture(argv: list[str], timeout: float) -> tuple[int | None, str, str]:
    """(exit code, stdout, stderr) of a read or a named unit action. Never raises."""
    try:
        reply = _call({"op": "capture", "argv": argv, "timeout": timeout})
    except BrokerError as exc:
        return None, "", str(exc)
    return reply["code"], reply["stdout"], reply["stderr"]


def hermes(args: list[str], *, stdin: str | None, timeout: float) -> tuple[int | None, str]:
    """One Hermes CLI call on the operator profile. Never raises."""
    try:
        reply = _call({"op": "hermes", "args": args, "stdin": stdin, "timeout": timeout})
    except BrokerError as exc:
        return None, str(exc)
    return reply["code"], reply["text"]


def stop_unit(unit: str) -> None:
    try:
        _call({"op": "stop_unit", "unit": unit})
    except BrokerError:
        pass


def start_unit(unit: str) -> RemoteProcess:
    return _open({"op": "start_unit", "unit": unit})


def chat(thread: str, unit: str) -> RemoteProcess:
    return _open({"op": "chat", "thread": thread, "unit": unit})


def script(name: str, unit: str) -> RemoteProcess:
    return _open({"op": "script", "name": name, "unit": unit})


def gmgn_account() -> Any:
    """``account_info`` run outside the wall, shaped like the ``GmgnResult`` it returns."""
    from kaiba.core.schemas import Receipt

    try:
        reply = _call({"op": "gmgn_account"})
    except BrokerError as exc:
        if str(exc) == "timeout":
            raise TimeoutError from exc
        raise
    return SimpleNamespace(data=reply["data"], receipt=Receipt.model_validate(reply["receipt"]))


def save_risk(config: dict[str, Any]) -> None:
    """Save the risk file. The broker keeps the bounds on disk, whatever arrives here."""
    _call({"op": "save_risk", "config": config})


# --------------------------------------------------------------------------------------
# what may be asked for
# --------------------------------------------------------------------------------------


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise Refused("expected a list of strings")
    return value


def _log_unit(unit: str) -> None:
    from dashboard import commands

    if not commands.LOG_UNIT.match(unit):
        raise Refused(f"unit {unit!r}")


def _start_targets() -> set[str]:
    from dashboard import commands

    return {cmd.target for cmd in commands.builtin_commands() if cmd.kind == "unit"}


def _check_start(unit: str) -> None:
    if unit not in _start_targets():
        raise Refused(f"start of {unit!r}")


def _check_systemctl(args: list[str]) -> None:
    from dashboard import commands, trading

    if args == ["list-units", "--all", "--plain", "--no-legend", "--no-pager", *trading.UNIT_PATTERNS]:
        return
    if len(args) == 2 and args[0] == "is-active":
        return _log_unit(args[1])
    if args[:-1] in (["show", "-p", "ActiveState", "--value"],
                     ["show", "-p", "ActiveState", "-p", "SubState", "-p", "ActiveEnterTimestamp"]):
        return _log_unit(args[-1])
    if args[:-1] == ["start", "--no-block"]:
        return _check_start(args[-1])
    if len(args) == 2 and args[0] == "restart" and args[1].removesuffix(".service") in commands.RESTARTABLE \
            and args[1].endswith(".service"):
        return None
    raise Refused(f"systemctl {args[:2]}")


def _check_journal(args: list[str]) -> None:
    if args == ["-p", "err", "--since", "-2h", "--no-pager", "-o", "short-iso", "-n", "300"]:
        return None
    if len(args) == 7 and args[0] == "-u" and args[2:] == ["-n", "200", "--no-pager", "-o", "short-iso"]:
        return _log_unit(args[1])
    if len(args) == 8 and args[0] == "-u" and args[2:6] == ["--no-pager", "-o", "short-iso", "--show-cursor"]:
        _log_unit(args[1])
        if (args[6] == "--after-cursor" and _CURSOR.match(args[7])) or \
                (args[6] == "--since" and _SINCE_EPOCH.match(args[7])):
            return None
    raise Refused("journalctl shape")


def check_capture(argv: list[str]) -> None:
    """Raise :class:`Refused` unless ``argv`` is a read or action the console really runs."""
    if argv[:2] == ["systemctl", "--user"]:
        return _check_systemctl(argv[2:])
    if argv[:2] == ["journalctl", "--user"]:
        return _check_journal(argv[2:])
    raise Refused(f"program {argv[:1]}")


def check_hermes(args: list[str]) -> None:
    """Raise :class:`Refused` unless ``args`` is a Hermes call the console really makes."""
    if args in (["usage", "--json"], ["cron", "list"], ["pause"], ["resume"],
                ["sessions", "list", "--source", "telegram", "--limit", "1"],
                ["send", "-t", "telegram", "-f", "-", "--json"]):
        return
    if args[:4] == ["sessions", "export", "--format", "jsonl"] and _EXPORT_FILE.match(args[-1]):
        middle = args[4:-1]
        if len(middle) == 2 and middle[0] == "--session-id" and _SESSION.match(middle[1]):
            return
        if len(middle) == 4 and middle[:3] == ["--source", "cron", "--newer-than"] and _NEWER_THAN.match(middle[3]):
            return
    raise Refused(f"hermes {args[:2]}")


# --------------------------------------------------------------------------------------
# the broker's side
# --------------------------------------------------------------------------------------


def _timeout(request: dict[str, Any]) -> float:
    value = request.get("timeout", 30)
    if type(value) not in (int, float) or not 0 < value <= MAX_TIMEOUT_S:
        raise Refused("timeout")
    return float(value)


def _child_unit(request: dict[str, Any]) -> str:
    unit = request.get("unit")
    if not isinstance(unit, str) or not _CHILD_UNIT.match(unit):
        raise Refused("child unit name")
    return unit


def _send(channel: socket.socket, reply: dict[str, Any]) -> None:
    channel.sendall(json.dumps(reply, default=str).encode("utf-8"))


def _watch(channel: socket.socket, proc: subprocess.Popen) -> None:
    """Hand the pipes over, obey stop requests, report the exit code."""
    names = [name for name in ("stdin", "stdout", "stderr") if getattr(proc, name) is not None]
    socket.send_fds(channel, [json.dumps({"pipes": names}).encode("utf-8")],
                    [getattr(proc, name).fileno() for name in names])
    for name in names:
        getattr(proc, name).close()
    while proc.poll() is None:
        if not select.select([channel], [], [], 0.25)[0]:
            continue
        word = channel.recv(32)
        if word.startswith(b"kill"):
            proc.kill()
        else:
            proc.terminate()
        if not word:  # the web process is gone: nobody is left to tell
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            return
    _send(channel, {"returncode": proc.returncode})


def _op_capture(channel: socket.socket, request: dict[str, Any]) -> None:
    argv = _strings(request.get("argv"))
    check_capture(argv)
    timeout = _timeout(request)
    try:
        done = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, check=False)
    except FileNotFoundError:
        return _send(channel, {"code": None, "stdout": "", "stderr": f"{argv[0]} is not available on this machine"})
    except subprocess.TimeoutExpired:
        return _send(channel, {"code": None, "stdout": "", "stderr": f"{argv[0]} timed out after {timeout:.0f} s"})
    _send(channel, {"code": done.returncode, "stdout": done.stdout or "", "stderr": done.stderr or ""})


def _op_hermes(channel: socket.socket, request: dict[str, Any]) -> None:
    from dashboard import agent_view

    args = _strings(request.get("args"))
    check_hermes(args)
    stdin = request.get("stdin")
    if stdin is not None and not isinstance(stdin, str):
        raise Refused("stdin")
    code, text = agent_view.run_hermes(args, stdin=stdin, timeout=_timeout(request))
    _send(channel, {"code": code, "text": text})


def _op_stop_unit(channel: socket.socket, request: dict[str, Any]) -> None:
    unit = _child_unit(request)
    subprocess.run(["systemctl", "--user", "stop", f"{unit}.service"], capture_output=True, timeout=20, check=False)
    _send(channel, {"ok": True})


def _op_start_unit(channel: socket.socket, request: dict[str, Any]) -> None:
    unit = request.get("unit")
    if not isinstance(unit, str):
        raise Refused("unit")
    _check_start(unit)
    proc = subprocess.Popen(["systemctl", "--user", "start", unit], stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, text=True)
    _watch(channel, proc)


def _op_chat(channel: socket.socket, request: dict[str, Any]) -> None:
    from dashboard import agent_chat, procs

    thread, unit = request.get("thread"), _child_unit(request)
    if not isinstance(thread, str) or not agent_chat.THREAD.match(thread):
        raise Refused("chat thread")
    argv, env, cwd = agent_chat.command_for(thread)
    _watch(channel, procs.spawn(argv, unit=unit, stdin=True, merge_stderr=False, memory_max="2G",
                                runtime_max_s=agent_chat.TURN_TIMEOUT_S, cwd=cwd, env=env))


def _op_script(channel: socket.socket, request: dict[str, Any]) -> None:
    from dashboard import commands, procs

    unit, name = _child_unit(request), request.get("name")
    # Only a script that opted in, with the timeout from its own header.
    cmd = next((c for c in commands.script_commands() if c.target == name), None)
    if cmd is None:
        raise Refused("script")
    argv, env = commands.script_argv(cmd.target, cmd.timeout_s)
    _watch(channel, procs.spawn(argv, unit=unit, memory_max="1G", runtime_max_s=cmd.timeout_s + 60,
                                cwd=str(commands.REPO_ROOT), env=env))


def _op_gmgn_account(channel: socket.socket, _request: dict[str, Any]) -> None:
    from kaiba.providers.gmgn_cli import account_info

    try:
        result = account_info(timeout_s=8.0, wait_for_slot_s=0.0)
    except TimeoutError:
        return _send(channel, {"error": "timeout"})
    _send(channel, {"data": result.data, "receipt": result.receipt.model_dump(mode="json")})


def _op_save_risk(channel: socket.socket, request: dict[str, Any]) -> None:
    from kaiba.core.config import RiskConfig
    from kaiba.core.config import save_risk as write_risk

    write_risk(RiskConfig.model_validate(request.get("config")))
    _send(channel, {"ok": True})


_OPS = {
    "capture": _op_capture, "hermes": _op_hermes, "stop_unit": _op_stop_unit, "start_unit": _op_start_unit,
    "chat": _op_chat, "script": _op_script, "gmgn_account": _op_gmgn_account, "save_risk": _op_save_risk,
}


def _say(text: str) -> None:
    print(f"broker: {text}", file=sys.stderr, flush=True)


def _read_request(channel: socket.socket) -> dict[str, Any]:
    channel.settimeout(10)
    data = b""
    while b"\n" not in data:
        chunk = channel.recv(1 << 16)
        if not chunk:
            break
        data += chunk
        if len(data) > MAX_REQUEST:
            raise Refused("request too large")
    channel.settimeout(None)
    request = json.loads(data)
    if not isinstance(request, dict):
        raise Refused("request is not an object")
    return request


def _answer(channel: socket.socket, slots: threading.Semaphore) -> None:
    op = "?"
    try:
        request = _read_request(channel)
        op = str(request.get("op"))[:40]
        handler = _OPS.get(op)
        if handler is None:
            raise Refused(f"operation {op!r}")
        handler(channel, request)
    except Refused as exc:
        _say(f"REFUSED {op}: {exc}")
        _try_send(channel, {"error": f"refused: {exc}"})
    except Exception as exc:
        _say(f"{op} failed: {type(exc).__name__}: {exc}")
        _try_send(channel, {"error": f"{op} failed: {type(exc).__name__}"})
    finally:
        channel.close()
        slots.release()


def _try_send(channel: socket.socket, reply: dict[str, Any]) -> None:
    try:
        _send(channel, reply)
    except OSError:
        pass


def serve(control: socket.socket) -> None:
    """Answer the web process until it goes away."""
    slots = threading.Semaphore(MAX_CALLS)
    while True:
        message, fds, _flags, _addr = socket.recv_fds(control, 16, 4)
        if not message:
            return
        for fd in fds:
            if not slots.acquire(blocking=False):
                _say("REFUSED a call: too many at once")
                os.close(fd)
                continue
            try:
                channel = socket.socket(fileno=fd)
            except OSError:
                os.close(fd)
                slots.release()
                continue
            threading.Thread(target=_answer, args=(channel, slots), daemon=True).start()


def console_policy(root: Path, port: int) -> wall.Policy:
    """What the web process may touch. ``root`` is the deployed tree."""
    home = Path.home()
    data = root / "data"
    db = data / "kaiba.db"
    package = Path(__file__).resolve().parent
    etc = ("ld.so.cache", "localtime", "mime.types", "nsswitch.conf", "passwd", "group", "hosts", "ssl")
    forbidden = tuple(str(path) for path in (
        home / ".config" / "gmgn" / ".env", home / ".config" / "kaiba" / ".env",
        home / ".config" / "kaiba" / "dashboard.env", home / ".hermes" / "auth.json",
        home / ".ssh" / "authorized_keys") if path.is_file())
    if not forbidden:
        raise wall.WallError("none of the key files exists, so the wall cannot be proved")
    return wall.Policy(
        read=("/usr", *(f"/etc/{name}" for name in etc if Path("/etc", name).exists()),
              str(package), str(root / ".venv"), str(root / "kaiba"), str(root / "config"),
              str(root / "scripts" / "dashboard"), str(data)),
        # By inode: nothing else under data/ is writable, because timers run code from there.
        write=("/dev/null", str(db), f"{db}-wal", f"{db}-shm"),
        own=(str(data / "dashboard"), EXPORT_DIR),
        bind_ports=(port,),
        forbidden=forbidden,
    )


def _serve_http(control: socket.socket, policy: wall.Policy, root: Path, host: str, port: int) -> None:
    """The forked child: build the wall, prove it, then start the web app inside it."""
    wall.die_with_parent(signal.SIGTERM)
    tmp = str(root / "data" / "dashboard" / "tmp")
    os.environ.update({
        ENV_FD: str(control.fileno()), "TMPDIR": tmp, "SQLITE_TMPDIR": tmp, "PYTHONDONTWRITEBYTECODE": "1",
        # Nothing to read there, and Settings would otherwise fail opening the key file.
        "KAIBA_CONFIG_DIR": "/nonexistent",
    })
    for name in ("DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR", "SSH_AUTH_SOCK"):
        os.environ.pop(name, None)
    sys.dont_write_bytecode = True
    wall.apply(policy)
    wall.prove(policy)
    _say(f"wall up and proved against {len(policy.forbidden)} key files, Landlock ABI {wall.landlock_abi()}")

    import uvicorn

    uvicorn.run("dashboard.app:app", host=host, port=port, proxy_headers=True, forwarded_allow_ips="127.0.0.1",
                server_header=False, timeout_graceful_shutdown=10)


def _restart_if_replaced(paths: tuple[str, ...], child: int) -> None:
    """The wall's write rules follow inodes. If SQLite makes new files, rebuild it around them."""
    seen = {path: os.stat(path).st_ino for path in paths}
    while True:
        time.sleep(INODE_CHECK_S)
        for path, inode in seen.items():
            try:
                if os.stat(path).st_ino != inode:
                    _say(f"{path} was replaced; restarting so the wall covers the new file")
                    os.kill(child, signal.SIGTERM)
                    return
            except FileNotFoundError:
                continue


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the console inside its wall")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8794)
    args = parser.parse_args(argv)

    root = Path(os.environ.get("KAIBA_WALL_ROOT") or Path(__file__).resolve().parents[1])
    os.makedirs(EXPORT_DIR, mode=0o700, exist_ok=True)
    os.makedirs(root / "data" / "dashboard" / "tmp", exist_ok=True)
    policy = console_policy(root, args.port)

    inner, outer = socket.socketpair()
    child = os.fork()
    if child == 0:
        outer.close()
        try:
            _serve_http(inner, policy, root, args.host, args.port)
        except BaseException:
            traceback.print_exc()
            os._exit(70)
        os._exit(0)

    inner.close()
    # systemd signals the whole unit. The web process shuts down on its own; stay until it has.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    db_files = tuple(path for path in policy.write if path.startswith(str(root)))
    threading.Thread(target=_restart_if_replaced, args=(db_files, child), daemon=True).start()
    serve(outer)
    _pid, status = os.waitpid(child, 0)
    _say(f"web process ended with status {status}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
