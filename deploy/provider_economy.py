"""Inspect the provider-economy rollout and persist owner-requested Hermes memory.

Run on the VPS as its existing service user. No provider, credential, financial,
service, or canonical database writes. Each read-only SQL connection is closed
within five seconds, after checking WAL size. Source deployment uses the existing
push_committed.py. --write-memory only changes the three Kaiba profile documents.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path

MARKER = "PROVIDER-ECONOMY-20261102"
MEMORY = (
    "\n<!-- " + MARKER + " -->\n"
    "Owner spending order, 2 Nov 2026: no new API spend/top-ups/overages. "
    "No Alchemy payment after Nov2026 (Asia/Jakarta). Use fresh cache/free APIs "
    "and prepaid annual GMGN; fall back to GMGN where supported, respecting "
    "limits and exit/reconciliation priority. Kaiba Alchemy HTTP/probe/WS "
    "network guard denies new calls. Billing cancellation/account-wide usage "
    "still unverified. See ~/kaiba/docs/MEMORY.md. Preserve trading authority.\n"
    "<!-- /" + MARKER + " -->\n\n"
)
SOURCES = (
    "kaiba/providers/provider_policy.py", "kaiba/providers/_http.py",
    "kaiba/ingest/feed_endpoints.py", "kaiba/ingest/alchemy_ws.py",
)


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def read(path: Path, limit: int = 128 * 1024) -> bytes:
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("symlink_refused")
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("file_byte_limit")
    return raw


def preserve_addition(path: Path, addition: bytes, backup_root: Path) -> dict:
    original = read(path) if path.exists() else b""
    if MARKER.encode() in original:
        if not original.startswith(addition):
            raise ValueError("existing_policy_block_differs")
        return {"status": "already_present", "sha256": sha(original)}
    # Preserve privately outside git; never print existing memory or personal data.
    backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = backup_root / (sha(str(path).encode())[:16] + ".original")
    if backup.exists():
        if read(backup) != original:
            raise ValueError("backup_conflict")
    else:
        with backup.open("xb") as stream:
            stream.write(original)
        backup.chmod(0o600)
    path.parent.mkdir(parents=True, exist_ok=True)
    final = addition + original
    temporary = path.with_name(path.name + ".provider-economy-new")
    with temporary.open("xb") as stream:
        stream.write(final)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(path.stat().st_mode & 0o777 if path.exists() else 0o600)
    if (read(path) if path.exists() else b"") != original:
        raise ValueError("document_changed")
    temporary.replace(path)
    if read(path) != final:
        raise ValueError("write_verification_failed")
    return {"status": "added", "before_sha256": sha(original), "sha256": sha(final),
            "original_preserved": final[len(addition):] == original}


def memory(root: Path, home: Path) -> dict:
    template = read(root / "hermes/profiles/kaiba-operator/SOUL.md").decode()
    start = template.index("<!-- Owner-requested addition by Codex, " + MARKER)
    end = template.index("<!-- /" + MARKER + " -->", start) + len("<!-- /" + MARKER + " -->")
    section = (template[start:end] + "\n\n").encode()
    backup = root / "data/codex-provider-economy-20261102/memory-backups"
    out = {}
    for profile in ("kaiba-operator", "kaiba-research", "kaiba-reflect"):
        base = home / ".hermes/profiles" / profile
        if not (base / "SOUL.md").is_file():
            raise ValueError("expected_profile_missing")
        for name, body in (("SOUL.md", section), ("memories/MEMORY.md", MEMORY.encode())):
            target = base / name
            out[f"{profile}/{name}"] = preserve_addition(target, body, backup)
    return out


def inspect(root: Path) -> dict:
    out = {"at_ms": int(time.time() * 1000), "sources": {}, "configs": {}, "units": {}}
    for name in SOURCES:
        p = root / name
        out["sources"][name] = sha(read(p, 1024 * 1024)) if p.exists() else None
    for name in ("config/risk.yaml", "config/signer-policy.yaml", "config/schedule.yaml"):
        out["configs"][name] = sha(read(root / name, 1024 * 1024))
    for unit in ("kaiba-ingest.service", "kaiba-ops.service", "kaiba-protection.service",
                 "kaiba-scan.service", "kaiba-engine.service", "kaiba-tweet-sources.service",
                 "hermes-gateway-kaiba-operator.service", "hermes-gateway-kaiba-research.service",
                 "hermes-gateway-kaiba-reflect.service"):
        p = subprocess.run(["systemctl", "--user", "show", unit, "-p", "MainPID", "-p", "ActiveState"],
                           capture_output=True, text=True, timeout=3, check=True)
        out["units"][unit] = dict(line.split("=", 1) for line in p.stdout.splitlines() if "=" in line)
    db = root / "data/kaiba.db"
    queries = {
        "orders_in_flight": "SELECT COUNT(*) FROM orders WHERE state IN ('reserved','submitting','submitted','partial','unknown')",
        "live_open_positions": "SELECT COUNT(*) FROM positions WHERE closed_ms IS NULL AND mode IN ('live','canary')",
    }
    for label, sql in queries.items():
        wal = Path(str(db) + "-wal")
        if wal.exists() and wal.stat().st_size > 1_500_000_000:
            out[label] = "deferred_wal"  # caller waits >=60s before another query
            break
        start = time.monotonic()
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1)
        try:
            conn.execute("PRAGMA query_only=ON")
            conn.set_progress_handler(lambda deadline=start + 4: int(time.monotonic() > deadline), 1000)
            out[label] = conn.execute(sql).fetchone()[0]
        finally:
            conn.close()
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.home() / "kaiba")
    parser.add_argument("--write-memory", action="store_true")
    args = parser.parse_args()
    out = inspect(args.root)
    if args.write_memory:
        out["memory"] = memory(args.root, Path.home())
    print(json.dumps(out, sort_keys=True))


if __name__ == "__main__":
    main()
