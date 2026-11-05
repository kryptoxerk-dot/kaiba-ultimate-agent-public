"""Reload existing Kaiba workers after the spend-guard deployment, never Hermes.

Requires all configured lanes shadow/off, zero live inventory and unresolved
orders, exact installed source hashes, and unchanged risk/signer/schedule bytes.
Only already-active units in the fixed allowlist are reloaded, one at a time.
Run under nice19/ionice3/timeout on the existing service user's account.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import yaml

from deploy.provider_economy import inspect, read

UNITS = (
    "kaiba-ingest.service", "kaiba-scan.service", "kaiba-ops.service",
    "kaiba-engine.service", "kaiba-protection.service", "kaiba-snipe.service",
    "kaiba-tweet-launch.service", "kaiba-tweet-sources.service",
    "kaiba-notify.service", "kaiba-social-hunt.service", "kaiba-social-follow.service",
    "kaiba-mcp.service", "kaiba-dashboard.service",
)


def validate_packet(packet: dict) -> dict:
    if set(packet) not in ({"configs", "sources"}, {"configs", "sources", "already_reloaded"}):
        raise ValueError("expected_snapshot_required")
    already = packet.get("already_reloaded", {})
    if not isinstance(already, dict) or any(
        unit not in UNITS or not isinstance(pid, str) or not pid.isdecimal() or int(pid) <= 0
        for unit, pid in already.items()
    ):
        raise ValueError("verified_reload_inventory_invalid")
    return already


def require_quiet(snapshot: dict, risk: dict, expected: dict) -> None:
    if snapshot.get("orders_in_flight") != 0 or snapshot.get("live_open_positions") != 0:
        raise ValueError("live_or_unresolved_inventory_reload_refused")
    if snapshot.get("configs") != expected.get("configs") or snapshot.get("sources") != expected.get("sources"):
        raise ValueError("deployment_or_configuration_drift")
    lanes = risk.get("lanes")
    if not isinstance(lanes, dict) or not lanes:
        raise ValueError("lane_modes_unavailable")
    if any(not isinstance(v, dict) or v.get("mode") not in ("shadow", "off") for v in lanes.values()):
        raise ValueError("nonpaper_lane_reload_refused")


def state(unit: str) -> dict:
    if unit not in UNITS:
        raise ValueError("unit_not_owned")
    p = subprocess.run(["systemctl", "--user", "show", unit, "-p", "MainPID", "-p", "ActiveState"],
                       capture_output=True, text=True, timeout=3, check=True)
    return dict(row.split("=", 1) for row in p.stdout.splitlines() if "=" in row)


def main():
    root = Path.home() / "kaiba"
    packet = json.loads(input())
    already = validate_packet(packet)
    for name, want in packet["sources"].items():
        if not isinstance(want, str) or hashlib.sha256(read(root / name, 1024 * 1024)).hexdigest() != want:
            raise ValueError("installed_source_mismatch")
    results = []
    for unit in UNITS:
        # Each inspect opens/closes short indexed queries independently and checks
        # WAL before each. No connection survives a systemctl action.
        snapshot = inspect(root)
        risk = yaml.safe_load(read(root / "config/risk.yaml"))
        require_quiet(snapshot, risk, packet)
        before = state(unit)
        if unit in already:
            if before.get("ActiveState") != "active" or before.get("MainPID") != already[unit]:
                raise ValueError("previous_reload_not_verified")
            results.append({"unit": unit, "status": "previous_reload_verified", "pid": already[unit]})
            continue
        if before.get("ActiveState") != "active":
            results.append({"unit": unit, "status": "skipped_inactive"})
            continue
        # Existing ops units allow 90s for a graceful stop. A client timeout does
        # not cancel systemd's job; never replay it without observing completion.
        subprocess.run(["systemctl", "--user", "restart", unit], timeout=125, check=True)
        after = state(unit)
        if after.get("ActiveState") != "active" or int(after.get("MainPID", "0")) <= 0 or after["MainPID"] == before["MainPID"]:
            raise ValueError("worker_reload_not_verified")
        row = {"unit": unit, "before_pid": before["MainPID"], "after_pid": after["MainPID"], "status": "active"}
        results.append(row)
        print(json.dumps(row), flush=True)
    final = inspect(root)
    require_quiet(final, yaml.safe_load(read(root / "config/risk.yaml")), packet)
    print(json.dumps({"status": "complete", "workers": results, "final": final}), flush=True)


if __name__ == "__main__":
    main()
