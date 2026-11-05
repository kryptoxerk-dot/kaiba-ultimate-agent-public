"""How the console starts a child process: the agent chat and the commands page.

On the box every child runs as its OWN transient systemd user unit (``systemd-run --user
--pipe --wait``). Three things follow from that, and they are the reason for this module:

* the child's memory and runtime caps are its own, not the console's 384 MB;
* it gets the user manager's environment, not the console's stripped one (the console unit
  unsets every provider key so the web process itself can never leak one);
* it has a unit name, so a stuck child can be stopped by name even if its pipe is gone.

There is no shell anywhere: every argv is a list built from fixed parts plus values that
were validated against an allowlist. Off the box (Windows, tests) the same argv runs
directly, so behaviour can be exercised without systemd.
"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence

from dashboard import broker


def isolated() -> bool:
    """True where children should run as transient systemd units."""
    if os.environ.get("KAIBA_DASHBOARD_ISOLATE", "1") == "0":
        return False
    return sys.platform.startswith("linux") and shutil.which("systemd-run") is not None


def unit_name(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(4)}"


def wrap(
    argv: Sequence[str],
    *,
    unit: str,
    memory_max: str = "1G",
    runtime_max_s: int = 1200,
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    """``argv`` as it should be executed: inside a transient unit on the box, as-is elsewhere."""
    if not isolated():
        return list(argv)
    pre = [
        "systemd-run", "--user", "--pipe", "--wait", "--collect", "--quiet",
        f"--unit={unit}",
        "-p", f"MemoryMax={memory_max}",
        "-p", f"RuntimeMaxSec={int(runtime_max_s)}",
    ]
    if cwd:
        pre.append(f"--working-directory={cwd}")
    for key, value in (env or {}).items():
        pre.append(f"--setenv={key}={value}")
    return [*pre, "--", *argv]


def spawn(
    argv: Sequence[str],
    *,
    unit: str,
    stdin: bool = False,
    merge_stderr: bool = True,
    memory_max: str = "1G",
    runtime_max_s: int = 1200,
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.Popen:
    """Start ``argv`` (wrapped as above) with line-buffered text pipes."""
    full = wrap(argv, unit=unit, memory_max=memory_max, runtime_max_s=runtime_max_s, cwd=cwd, env=env)
    child_env = None
    if not isolated() and env:
        child_env = {**os.environ, **env}
    return subprocess.Popen(
        full,
        stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        cwd=None if isolated() else cwd,
        env=child_env,
    )


def stop(proc: subprocess.Popen | None, unit: str | None) -> None:
    """Stop a child by unit name (the unit outlives its systemd-run client) and by process."""
    if unit and isolated():
        if broker.active():
            broker.stop_unit(unit)
        else:
            try:
                subprocess.run(["systemctl", "--user", "stop", f"{unit}.service"], capture_output=True,
                               timeout=20, check=False)
            except (OSError, subprocess.SubprocessError):
                pass
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
