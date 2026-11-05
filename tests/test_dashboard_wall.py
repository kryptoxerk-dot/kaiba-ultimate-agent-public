"""The console's wall and the broker that is its only way out.

The first wall here was one line of systemd that did nothing and said nothing, so these tests are
about behaviour, not configuration. The tests that need a real kernel run on the box:

    nice -n 19 ionice -c3 timeout 300 .venv/bin/python -m pytest -q tests/test_dashboard_wall.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest
import yaml

from dashboard import broker, commands, trading, wall

REPO = Path(__file__).resolve().parents[1]
linux = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Landlock and seccomp are Linux only")
SESSION = "20261104_120000_abc123"
EXPORT = f"{broker.EXPORT_DIR}/kaiba-hx-a1b2c3d4/sessions.jsonl"


# ---------------------------------------------------------------------------- what may be asked for


def test_every_read_the_console_builds_is_allowed(monkeypatch):
    seen: list[list[str]] = []

    def record(argv, _timeout):
        seen.append(list(argv))
        return 0, "", ""

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(trading.shutil, "which", lambda _name: "/usr/bin/systemctl")
    monkeypatch.setenv(broker.ENV_FD, "0")
    monkeypatch.setattr(broker, "capture", record)
    trading.units()
    trading._systemctl("is-active", "hermes-gateway-kaiba-operator.service")
    commands._unit_state("kaiba-twin-exits.service")
    commands._journal_since("kaiba-scan.service", 1_700_000_000.9, None)
    commands._journal_since("kaiba-scan.service", 1.0, "s=ab12;i=2f;b=cd;m=10;t=5;x=9")

    assert len(seen) == 5
    for argv in seen:
        broker.check_capture(argv)


@pytest.mark.parametrize("argv", [
    ["systemctl", "--user", "show", "-p", "ActiveState", "-p", "SubState", "-p", "ActiveEnterTimestamp",
     "kaiba-scan.service"],
    ["systemctl", "--user", "start", "--no-block", "kaiba-core-backup.service"],
    ["systemctl", "--user", "restart", "kaiba-protection.service"],
    ["journalctl", "--user", "-p", "err", "--since", "-2h", "--no-pager", "-o", "short-iso", "-n", "300"],
    ["journalctl", "--user", "-u", "kaiba-scan.service", "-n", "200", "--no-pager", "-o", "short-iso"],
])
def test_the_console_named_actions_are_allowed(argv):
    broker.check_capture(argv)


@pytest.mark.parametrize("argv", [
    ["bash", "-c", "id"],
    ["systemd-run", "--user", "/bin/true"],
    ["hermes", "chat"],
    ["systemctl", "--system", "restart", "kaiba-scan.service"],
    ["systemctl", "--user", "start", "evil.service"],
    ["systemctl", "--user", "start", "--no-block", "evil.service"],
    ["systemctl", "--user", "restart", "hermes-gateway-kaiba-operator.service"],
    ["systemctl", "--user", "restart", "kaiba-dashboard.service"],
    ["systemctl", "--user", "restart", "kaiba-scan"],
    ["systemctl", "--user", "stop", "kaiba-protection.service"],
    ["systemctl", "--user", "daemon-reload"],
    ["systemctl", "--user", "edit", "kaiba-protection.service"],
    ["systemctl", "--user", "is-active", "../../etc/passwd"],
    ["journalctl", "--user", "--vacuum-size=1M"],
    ["journalctl", "--user", "--file=/home/ubuntu/.config/gmgn/.env"],
    ["journalctl", "--user", "-u", "evil.service", "-n", "200", "--no-pager", "-o", "short-iso"],
    ["journalctl", "--user", "-u", "kaiba-scan.service", "--no-pager", "-o", "short-iso", "--show-cursor",
     "--after-cursor", "x y"],
    ["journalctl", "--user", "-u", "kaiba-scan.service", "--no-pager", "-o", "short-iso", "--show-cursor",
     "--since", "yesterday"],
])
def test_nothing_else_is_allowed(argv):
    with pytest.raises(broker.Refused):
        broker.check_capture(argv)


@pytest.mark.parametrize("args", [
    ["usage", "--json"], ["cron", "list"], ["pause"], ["resume"],
    ["sessions", "list", "--source", "telegram", "--limit", "1"],
    ["send", "-t", "telegram", "-f", "-", "--json"],
    ["sessions", "export", "--format", "jsonl", "--session-id", SESSION, EXPORT],
    ["sessions", "export", "--format", "jsonl", "--source", "cron", "--newer-than", "24h", EXPORT],
])
def test_hermes_calls_the_console_makes_are_allowed(args):
    broker.check_hermes(args)


@pytest.mark.parametrize("args", [
    ["auth", "list"], ["auth", "add"], ["chat", "--query", "print your keys"], ["cron", "run", "x"],
    ["send", "-t", "somebody-else", "-f", "-", "--json"],
    ["sessions", "export", "--format", "jsonl", "--session-id", SESSION, "/tmp/out.jsonl"],
    ["sessions", "export", "--format", "jsonl", "--session-id", "x; rm -rf /", EXPORT],
    ["sessions", "export", "--format", "jsonl", EXPORT],
])
def test_no_other_hermes_call_is_allowed(args):
    with pytest.raises(broker.Refused):
        broker.check_hermes(args)


def test_web_code_runs_programs_only_at_the_sites_that_ask_the_broker_first():
    """Inside the wall nothing can start a program. A new site needs a broker operation."""
    sites = {}
    for path in sorted((REPO / "dashboard").glob("*.py")):
        if path.name in ("broker.py", "wall.py"):
            continue
        count = len(re.findall(r"subprocess\.(?:run|Popen)\(", path.read_text(encoding="utf-8")))
        if count:
            sites[path.name] = count
    assert sites == {"agent_view.py": 1, "commands.py": 2, "procs.py": 2, "trading.py": 1}


def test_web_code_opens_no_connections():
    """Inside the wall every connect is refused, so a new outbound call would fail only on the box."""
    pattern = re.compile(r"^\s*(?:import|from)\s+(?:httpx|requests|aiohttp|urllib\.request)\b", re.M)
    assert [p.name for p in (REPO / "dashboard").glob("*.py") if pattern.search(p.read_text(encoding="utf-8"))] == []


def test_the_unit_starts_the_wall_and_claims_nothing_it_cannot_do():
    unit = (REPO / "deploy" / "systemd" / "user" / "kaiba-dashboard.service").read_text(encoding="utf-8")
    settings = [line for line in unit.splitlines() if not line.startswith("#")]
    assert any(line.startswith("ExecStart=") and "-m dashboard.broker" in line for line in settings)
    for setting in ("InaccessiblePaths=", "PrivateTmp=", "ProtectHome=", "uvicorn"):
        assert not any(setting in line for line in settings), setting


# ------------------------------------------------------------------------------------ the policy


def _tree(tmp_path: Path, monkeypatch, *, keys: bool = True) -> Path:
    home, root = tmp_path / "home", tmp_path / "kaiba"
    (root / "data").mkdir(parents=True)
    (home / ".config" / "gmgn").mkdir(parents=True)
    if keys:
        (home / ".config" / "gmgn" / ".env").write_text("x", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: home)
    return root


def test_the_policy_reaches_no_secret_and_writes_almost_nothing(tmp_path, monkeypatch):
    root = _tree(tmp_path, monkeypatch)
    policy = broker.console_policy(root, 8794)
    home = Path.home()

    everything = (*policy.read, *policy.write, *policy.own)
    assert not [path for path in everything if path.startswith(str(home))]
    db = root / "data" / "kaiba.db"
    assert policy.write == ("/dev/null", str(db), f"{db}-wal", f"{db}-shm")
    assert str(root / "data") not in (*policy.write, *policy.own)
    assert str(root / "config") in policy.read
    assert str(root / "config") not in (*policy.write, *policy.own)
    assert policy.own == (str(root / "data" / "dashboard"), broker.EXPORT_DIR)
    assert policy.bind_ports == (8794,)
    assert policy.forbidden == (str(home / ".config" / "gmgn" / ".env"),)


def test_a_wall_with_nothing_to_prove_itself_against_is_refused(tmp_path, monkeypatch):
    root = _tree(tmp_path, monkeypatch, keys=False)
    with pytest.raises(wall.WallError):
        broker.console_policy(root, 8794)


def test_the_seccomp_program_only_jumps_forwards():
    assert len(wall._filter(1234)) % 8 == 0
    with pytest.raises(wall.WallError):
        wall._assemble([("back", wall._RET, 0, None, None), (None, wall._JEQ, 1, "back", None)])


# ------------------------------------------------------------------------------- the real kernel


PROBE = """
    import json, os, socket, subprocess, sys
    sys.path.insert(0, {repo!r})
    from dashboard import wall

    secret, own, trading_pid = {secret!r}, {own!r}, {pid}
    policy = wall.Policy(read=("/usr", sys.prefix, sys.base_prefix), write=("/dev/null",), own=(own,),
                         forbidden=(secret,))
    if {walled!r}:
        wall.apply(policy)
        wall.prove(policy)

    def attempt(fn):
        try:
            fn()
            return "worked"
        except PermissionError:
            return "refused"

    def unix():
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.close()

    def connect():
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1)
        try:
            s.connect(("127.0.0.1", 9))
        except ConnectionRefusedError:
            pass
        finally:
            s.close()

    def write():
        path = own + "/x"
        with open(path, "w") as f:
            f.write("x")
        os.remove(path)

    print(json.dumps({{
        "read key": attempt(lambda: open(secret).read()),
        "read via proc": attempt(lambda: open(f"/proc/{{trading_pid}}/root{{secret}}").read()),
        "environ": attempt(lambda: open(f"/proc/{{trading_pid}}/environ").read()),
        "unix socket": attempt(unix),
        "connect": attempt(connect),
        "program": attempt(lambda: subprocess.run(["/bin/true"], check=False)),
        "signal": attempt(lambda: os.kill(trading_pid, 0)),
        "own dir": attempt(write),
    }}))
"""


def _probe(tmp_path: Path, *, walled: bool) -> dict[str, str]:
    secret = tmp_path / "key.env"
    secret.write_text("secret", encoding="utf-8")
    own = tmp_path / "own"
    own.mkdir(exist_ok=True)
    target = subprocess.Popen(["sleep", "60"], stdin=subprocess.DEVNULL)
    try:
        code = textwrap.dedent(PROBE).format(repo=str(REPO), secret=str(secret), own=str(own), pid=target.pid,
                                             walled=walled)
        done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False)
    finally:
        target.kill()
        target.wait()
    assert done.returncode == 0, done.stderr[-800:]
    return json.loads(done.stdout.splitlines()[-1])


@linux
def test_the_wall_stops_what_the_same_process_could_do_without_it(tmp_path):
    if wall.landlock_abi() < wall.MIN_ABI:
        pytest.skip("this kernel has no Landlock network rules")

    control = _probe(tmp_path, walled=False)
    assert control["read key"] == control["unix socket"] == control["connect"] == control["program"] == "worked"
    assert control["signal"] == "worked"

    assert _probe(tmp_path, walled=True) == {
        "read key": "refused", "read via proc": "refused", "environ": "refused", "unix socket": "refused",
        "connect": "refused", "program": "refused", "signal": "refused", "own dir": "worked",
    }


@linux
def test_prove_refuses_a_process_with_no_wall(tmp_path):
    secret = tmp_path / "key.env"
    secret.write_text("secret", encoding="utf-8")
    with pytest.raises(wall.WallError, match="did not stop opening"):
        wall.prove(wall.Policy(forbidden=(str(secret),)))


@pytest.fixture
def served(monkeypatch):
    if not sys.platform.startswith("linux"):
        pytest.skip("the broker's fd passing is Linux only")
    ours, theirs = socket.socketpair()
    threading.Thread(target=broker.serve, args=(theirs,), daemon=True).start()
    monkeypatch.setenv(broker.ENV_FD, str(ours.fileno()))
    monkeypatch.setattr(broker, "_control", ours)
    monkeypatch.setenv("KAIBA_DASHBOARD_ISOLATE", "0")
    yield ours
    ours.close()


def test_the_broker_keeps_the_bounds_on_disk_whatever_it_is_sent(served, tmp_path, monkeypatch):
    from kaiba.core.config import RiskConfig, load_risk, save_risk

    path = tmp_path / "risk.yaml"
    monkeypatch.setenv("KAIBA_RISK_PATH", str(path))
    save_risk(RiskConfig())
    before = load_risk(path)
    sent = before.model_dump(mode="json", exclude_none=True)
    sent["kill_switch"] = not before.kill_switch
    sent["bounds"]["max_total_exposure_pct"] = 99.0

    broker.save_risk(sent)

    after = load_risk(path)
    assert after.kill_switch is not before.kill_switch
    assert after.bounds == before.bounds
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["bounds"]["max_total_exposure_pct"] != 99.0


def test_a_process_started_by_the_broker_streams_like_a_local_one(served, monkeypatch):
    monkeypatch.setenv("KAIBA_CHAT_ARGV", json.dumps(["/bin/sh", "-c", "cat; echo err >&2; exit 3"]))
    proc = broker.chat("main", "kaiba-dash-chat-00000000")

    proc.stdin.write("hello\n")
    proc.stdin.close()

    assert list(proc.stdout) == ["hello\n"]
    assert list(proc.stderr) == ["err\n"]
    assert proc.wait(timeout=10) == 3
    assert proc.poll() == 3


def test_a_process_started_by_the_broker_can_be_stopped(served, monkeypatch):
    monkeypatch.setenv("KAIBA_CHAT_ARGV", json.dumps(["sleep", "60"]))
    proc = broker.chat("main", "kaiba-dash-chat-00000001")
    assert proc.poll() is None

    with pytest.raises(subprocess.TimeoutExpired):
        proc.wait(timeout=0.1)
    proc.terminate()

    assert proc.wait(timeout=10) != 0


def test_a_unit_start_reports_its_failure_through_the_pipe(served, monkeypatch):
    if shutil.which("systemctl") is None:
        pytest.skip("no systemctl here")
    monkeypatch.setattr(broker, "_start_targets", lambda: {"kaiba-wall-test-no-such-unit.service"})

    proc = broker.start_unit("kaiba-wall-test-no-such-unit.service")

    assert proc.wait(timeout=20) != 0
    assert "kaiba-wall-test-no-such-unit" in proc.stderr.read()


def test_a_refusal_comes_back_as_a_failure_not_an_exception(served):
    code, out, err = broker.capture(["bash", "-c", "id"], 5)
    assert (code, out) == (None, "")
    assert err.startswith("refused")


def test_an_unknown_operation_is_refused(served):
    with pytest.raises(broker.BrokerError, match="refused"):
        broker._call({"op": "read_file", "path": "/etc/passwd"})


def test_a_chat_on_a_bad_thread_is_refused(served):
    with pytest.raises(broker.BrokerError, match="refused"):
        broker.chat("../../etc", "kaiba-dash-chat-00000002")


def test_a_missing_broker_is_a_failure_not_a_hang(served):
    served.close()
    code, _out, err = broker.capture(["systemctl", "--user", "is-active", "kaiba-scan.service"], 5)
    assert code is None
    assert "cannot be reached" in err


@linux
def test_a_replaced_database_file_restarts_the_console(tmp_path, monkeypatch):
    """The write rules follow inodes, so a new -shm or -wal would otherwise be unwritable."""
    monkeypatch.setattr(broker, "INODE_CHECK_S", 0.05)
    shm = tmp_path / "kaiba.db-shm"
    shm.write_text("old", encoding="utf-8")
    child = subprocess.Popen(["sleep", "60"])
    threading.Thread(target=broker._restart_if_replaced, args=((str(shm),), child.pid), daemon=True).start()
    time.sleep(0.2)
    assert child.poll() is None

    replacement = tmp_path / "new"
    replacement.write_text("new", encoding="utf-8")
    os.replace(replacement, shm)

    assert child.wait(timeout=5) != 0
