"""Start the whole agent on one machine, without systemd.

`deploy/systemd/` is the production answer and it is Linux-only. This is the development
and Windows answer: one command that brings up every long-running service, keeps them
alive, and prints one status line per service so you can see at a glance which part of the
agent is actually working.

    python deploy/run-local.py                 # everything
    python deploy/run-local.py --only ingest,dashboard
    python deploy/run-local.py --dry-run       # print what it would start

It deliberately does **not** daemonise, hide output, or restart forever in silence. A
service that keeps dying should be visible, because the failure mode this project has
already hit twice is a component that looks alive and is doing nothing.

Safety: this starts read, ingest, decision and paper services. It does not arm anything.
The global mode in `config/risk.yaml` governs whether any order can reach a venue, and
`deploy/preflight.py` is the gate you run before changing that.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PY = sys.executable

#: Restart backoff, seconds. Grows so a service that is failing fast stops spamming.
BACKOFF = (1, 2, 5, 10, 30, 60)


@dataclass
class Service:
    name: str
    args: list[str]
    why: str
    #: Services that are useful but not required for the agent to be "running".
    optional: bool = False
    proc: subprocess.Popen[bytes] | None = None
    restarts: int = 0
    started_ms: float = 0.0
    last_error: str = ""
    history: list[float] = field(default_factory=list)


def services() -> list[Service]:
    return [
        Service(
            "ingest",
            ["-m", "kaiba", "ingest", "run"],
            "real-time listeners; without this every intelligence table stays empty",
        ),
        Service(
            "scan",
            ["-m", "kaiba", "scan", "run"],
            "tier 1: dossier, curve and lanes on whatever triage promoted",
        ),
        Service(
            "engine",
            ["-m", "kaiba", "engine", "run"],
            "turns signals into decisions and paper fills",
        ),
        Service(
            "protection",
            ["-m", "kaiba", "protection", "run"],
            "the exit watchdog; a position with no watchdog has no stop",
        ),
        Service(
            "ops",
            ["-m", "kaiba", "ops", "run"],
            "maintenance scheduler; without it creators, grades, signals and flow go stale",
        ),
        Service(
            "dashboard",
            ["-m", "kaiba", "dashboard"],
            "operator console on http://127.0.0.1:8788",
            optional=True,
        ),
    ]


def spawn(svc: Service, env: dict[str, str]) -> None:
    svc.proc = subprocess.Popen(  # noqa: S603 - fixed argv from a literal table, no shell
        [PY, *svc.args],
        cwd=str(REPO),
        env=env,
        stdout=None,  # inherit: the operator should see what each service says
        stderr=None,
        shell=False,
    )
    svc.started_ms = time.time()
    svc.history.append(svc.started_ms)


def flapping(svc: Service, window_s: float = 60.0, limit: int = 4) -> bool:
    """More than `limit` starts in `window_s` means restarting is not helping."""
    cutoff = time.time() - window_s
    recent = [t for t in svc.history if t >= cutoff]
    return len(recent) > limit


def status_line(svc: Service) -> str:
    if svc.proc is None:
        return f"  {svc.name:<11} not started"
    code = svc.proc.poll()
    if code is None:
        up = int(time.time() - svc.started_ms)
        return f"  {svc.name:<11} running   up {up:>5}s   restarts {svc.restarts}"
    tag = "stopped" if code == 0 else f"exit {code}"
    return f"  {svc.name:<11} {tag:<9} restarts {svc.restarts}  {svc.last_error[:40]}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", default="", help="Comma-separated subset, e.g. ingest,engine.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-restart", action="store_true", help="Exit when a service dies.")
    ap.add_argument("--status-every", type=float, default=30.0)
    args = ap.parse_args()

    chosen = [s.strip() for s in args.only.split(",") if s.strip()]
    svcs = [s for s in services() if not chosen or s.name in chosen]
    if not svcs:
        print(f"no such service; known: {', '.join(s.name for s in services())}")
        return 2

    if args.dry_run:
        for s in svcs:
            print(f"  {s.name:<11} {PY} {' '.join(s.args)}\n              {s.why}")
        return 0

    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("PYTHONPATH", str(REPO))

    stopping = False

    def on_signal(_sig: int, _frm: object) -> None:
        nonlocal stopping
        stopping = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, on_signal)
        except (ValueError, OSError):  # pragma: no cover - not all platforms/threads
            pass

    print(f"kaiba local runner — {len(svcs)} service(s), repo {REPO}")
    for s in svcs:
        spawn(s, env)
        print(f"  started {s.name} (pid {s.proc.pid if s.proc else '?'})")
    print("  ctrl-c to stop everything\n")

    next_status = time.time() + args.status_every
    try:
        while not stopping:
            time.sleep(0.5)
            for s in svcs:
                if s.proc is None or s.proc.poll() is None:
                    continue
                code = s.proc.returncode
                s.last_error = f"exited {code}"
                if args.no_restart:
                    print(f"  {s.name} exited {code}; --no-restart, stopping")
                    stopping = True
                    break
                if flapping(s):
                    print(f"  {s.name} is flapping (>4 starts in 60s); leaving it down")
                    s.proc = None
                    continue
                delay = BACKOFF[min(s.restarts, len(BACKOFF) - 1)]
                print(f"  {s.name} exited {code}; restarting in {delay}s")
                time.sleep(delay)
                s.restarts += 1
                spawn(s, env)

            if time.time() >= next_status:
                print(f"\n[{time.strftime('%H:%M:%S')}]")
                for s in svcs:
                    print(status_line(s))
                print()
                next_status = time.time() + args.status_every
    finally:
        print("\nstopping services...")
        for s in svcs:
            if s.proc and s.proc.poll() is None:
                s.proc.terminate()
        deadline = time.time() + 10
        for s in svcs:
            if not s.proc:
                continue
            remaining = max(0.0, deadline - time.time())
            try:
                s.proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                print(f"  {s.name} did not stop; killing")
                s.proc.kill()
        print("stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
