"""The signer unit's environment must be the environment the code reads (defect 2).

Until 2026-10-02 `kaiba-signer.service` set KAIBA_SIGNER_KEYSTORE and KAIBA_SIGNER_POLICY,
while signer.py reads KAIBA_KEYSTORE_DIR and policy.py reads KAIBA_SIGNER_POLICY_PATH. The
unit's paths were silently ignored, and the signer would have judged against the repo's
writable config/signer-policy.yaml instead of the root-owned /etc/kaiba/policy copy.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from kaiba.execution import signer
from tests.signer_fixtures import new_pubkey

ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "deploy" / "systemd" / "kaiba-signer.service"

_READ = re.compile(
    r"""(?:os\.environ\.get|os\.getenv|os\.environ\.setdefault)\(\s*["']([A-Z0-9_]+)["']"""
    r"""|os\.environ\[\s*["']([A-Z0-9_]+)["']\s*\]"""
)


def unit_environment() -> dict[str, str]:
    env: dict[str, str] = {}
    for line in UNIT.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("Environment="):
            name, _, value = line[len("Environment="):].partition("=")
            env[name] = value
    return env


def names_read_by_code() -> set[str]:
    names: set[str] = set()
    for path in (ROOT / "kaiba").rglob("*.py"):
        for m in _READ.finditer(path.read_text(encoding="utf-8", errors="ignore")):
            names.add(m.group(1) or m.group(2))
    return names


def test_every_kaiba_variable_the_unit_sets_is_read_by_the_code():
    kaiba_vars = {k for k in unit_environment() if k.startswith("KAIBA_")}
    assert len(kaiba_vars) >= 4, f"parser found {kaiba_vars}; it is not reading the unit"
    unread = kaiba_vars - names_read_by_code()
    assert not unread, f"kaiba-signer.service sets names no code reads: {sorted(unread)}"


def test_the_unit_names_the_root_owned_policy_and_the_signer_keystore():
    env = unit_environment()
    assert env[signer.ENV_KEYSTORE] == "/etc/kaiba/signer/keys"
    assert env["KAIBA_SIGNER_POLICY_PATH"] == "/etc/kaiba/policy/signer-policy.yaml"
    assert env[signer.ENV_STATE] == "/var/lib/kaiba/signer"
    assert env[signer.ENV_SOCKET] == "/run/kaiba/signer/signer.sock"
    assert f"--socket {env[signer.ENV_SOCKET]}" in UNIT.read_text(encoding="utf-8")


def test_under_the_units_environment_the_code_reads_the_units_paths(tmp_path):
    """Behavioural, and independent of what the names are: take the unit's variables,
    remap each path value into tmp, start a fresh interpreter with them, and ask the code
    where it looks. A name the code does not read leaves the code on its default."""
    unit = unit_environment()
    remapped = {
        name: str(tmp_path / value.strip("/").replace("/", "_"))
        for name, value in unit.items()
        if name.startswith("KAIBA_") and value.startswith("/")
    }

    def by_value(suffix: str) -> str:
        [name] = [n for n, v in unit.items() if n in remapped and v.endswith(suffix)]
        return remapped[name]

    keystore = by_value("/signer/keys")
    policy_file = by_value("signer-policy.yaml")
    state = by_value("/var/lib/kaiba/signer")
    sock = by_value("signer.sock")
    marker = new_pubkey()
    Path(policy_file).write_text(json.dumps({"owned_addresses": {"sol": [marker]}}), encoding="utf-8")

    probe = (
        "import json\n"
        "from kaiba.execution import policy, signer\n"
        "led = signer._ledger()\n"
        "print(json.dumps({'keystore': str(signer.KEYSTORE_DIR), 'socket': str(signer.DEFAULT_SOCKET),\n"
        "  'state': str(led.path.parent) if led.path else None,\n"
        "  'owned': policy.load_policy().owned_addresses.get('sol', [])}))\n"
    )
    env = {k: v for k, v in os.environ.items()
           if k not in ("KAIBA_KEYSTORE_DIR", "KAIBA_SIGNER_POLICY_PATH", "KAIBA_SIGNER_STATE", "KAIBA_SIGNER_SOCKET")}
    env.update(remapped)
    out = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, env=env,
                         capture_output=True, text=True, timeout=120, check=True)
    seen = json.loads(out.stdout.strip().splitlines()[-1])
    assert seen["keystore"] == keystore
    assert seen["owned"] == [marker], "the signer would judge against a policy file the unit did not name"
    assert seen["state"] == state
    assert seen["socket"] == sock
