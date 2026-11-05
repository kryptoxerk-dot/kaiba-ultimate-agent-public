"""Restricted BAW connection/candidate/preview service. Never broadcasts a transaction.

The owner requested automatic NFT mints and completed wallet pairing.
BAW-FUNDED-MINTS-20261102 adds verified Base/Ethereum preview support and accepts
the actual timezone-aware developer expiry. This staged service implements the
independent buildable components;
complete asset-effect/fee proofs and execution require authenticated previews.
Do not label this service a working or armed mint executor.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
import json
import math
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from kaiba.execution.baw_preview import inspect_preview
from kaiba.execution.nft_mint_policy import CHAINS, GasEnvelope, GasLedger, MintRefused, day_of
from kaiba.providers.binance_wallet_cli import BinanceWalletCLI, WalletUnavailable, connection_view

MAX_BODY = 16_384


def _expiry_ms(value):
    """Actual BAW settings return timezone-aware ISO expiry as well as epoch.

    BAW-FUNDED-MINTS-20261102: preserve the active/unexpired gate; unknown,
    naive, non-finite and malformed times must never admit a preview.
    """
    if type(value) in (int, float):
        if value <= 0 or value > 253_402_300_799_999 or not math.isfinite(value):
            return None
        return value * 1000 if value < 100_000_000_000 else value
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.utcoffset() is None:
            return None
        return stamp.timestamp() * 1000
    except (ValueError, OverflowError, OSError):
        return None


class MintPreflightWorker:
    def __init__(self, settings, *, client=None):
        self.settings = settings
        self.client = client or BinanceWalletCLI(node=settings["node"], script=settings["cli_script"],
                                                 state_dir=settings["session_dir"])
        state = Path(settings["state_dir"])
        state.mkdir(parents=True, exist_ok=True)
        self.db = state / "candidates.db"
        self.ledger = GasLedger(state / "gas.db", GasEnvelope(100_000, 10_000_000))
        self.connection = {"connected": False, "reason": "connection_not_checked"}
        self.checked_ms = 0
        self.lock = threading.Lock()
        conn = sqlite3.connect(self.db)
        try:
            with conn:
                conn.execute("CREATE TABLE IF NOT EXISTS candidates(key TEXT PRIMARY KEY, chain TEXT NOT NULL, "
                             "created_ms INTEGER NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL, "
                             "preview_day TEXT, preview_sha TEXT, blockers TEXT)")
                columns = {r[1] for r in conn.execute("PRAGMA table_info(candidates)")}
                for field, kind in (("preview_ms", "INTEGER"), ("evidence", "TEXT")):
                    if field not in columns:
                        conn.execute(f"ALTER TABLE candidates ADD COLUMN {field} {kind}")
        finally:
            conn.close()

    def _db(self):
        return sqlite3.connect(self.db, timeout=1)

    def status(self):
        conn = self._db()
        try:
            counts = dict(conn.execute("SELECT state,COUNT(*) FROM candidates GROUP BY state").fetchall())
            last = conn.execute("SELECT key,preview_ms,evidence FROM candidates WHERE preview_sha IS NOT NULL "
                                "ORDER BY preview_ms DESC,rowid DESC LIMIT 1").fetchone()
        finally:
            conn.close()
        evidence = None
        if last:
            try:
                evidence = json.loads(last[2]) if last[2] else {"blockers": ["legacy_preview_evidence_unavailable"]}
            except (ValueError, TypeError):
                evidence = {"blockers": ["preview_evidence_unreadable"]}
        if not self.checked_ms:
            blockers = ["wallet_connection_not_checked"]
        elif not self.connection.get("connected"):
            blockers = (["wallet_pairing_required"] if self.connection.get("reason") == "wallet_unconnected"
                        else ["wallet_connection_unavailable"])
        else:
            blockers = ["complete_mint_effect_and_locked_fee_bound_proof_required", "execution_adapter_not_armed"]
        return {"service": "kaiba-baw-preflight/1", "automatic_minting_requested": True,
                "execution_enabled": False, "connection": self.connection,
                "connection_checked_ms": self.checked_ms, "candidates": counts,
                "last_preview": {"candidate_id": last[0], "observed_ms": last[1], "evidence": evidence} if last else None,
                "gas": {**self.ledger.status(int(time.time()*1000)),
                        "scope": "this_worker_ledger", "account_spend_reconciled": False},
                "blockers": blockers}

    def refresh(self):
        with self.lock:
            now = int(time.time()*1000)
            if self.checked_ms and now - self.checked_ms < 60_000:
                return self.status()
            try:
                self.connection = connection_view(self.client)
            except WalletUnavailable as exc:
                self.connection = {"connected": False, "reason": str(exc)}
            self.checked_ms = now
            return self.status()

    def stage(self, payload):
        allowed = {"chain", "opportunity_id", "contract", "calldata", "unsigned_tx"}
        if not isinstance(payload, dict) or set(payload) - allowed:
            raise MintRefused("candidate_fields_refused")
        if payload.get("chain") not in CHAINS:
            raise MintRefused("chain_refused")
        ident = payload.get("opportunity_id")
        if not isinstance(ident, str) or not 0 < len(ident) <= 160:
            raise MintRefused("opportunity_identity_required")
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(raw.encode()) > MAX_BODY:
            raise MintRefused("candidate_size")
        key = hashlib.sha256(raw.encode()).hexdigest()
        conn = self._db()
        try:
            with conn:
                if conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] >= 500:
                    raise MintRefused("candidate_queue_capacity")
                conn.execute("INSERT OR IGNORE INTO candidates(key,chain,created_ms,payload,state) VALUES(?,?,?,?,?)",
                             (key, payload["chain"], int(time.time()*1000), raw, "staged"))
        finally:
            conn.close()
        return {"candidate_id": key, "state": "staged", "execution_enabled": False}

    def preview_one(self, key):
        # Serialize preview admissions. No financial transaction is submitted.
        if not isinstance(key, str) or re.fullmatch(r"[0-9a-f]{64}", key) is None:
            raise MintRefused("candidate_identity")
        with self.lock:
            if not self.connection.get("connected"):
                raise MintRefused("wallet_pairing_required")
            chain_view = self.connection.get("chains", {})
            mode = self.connection.get("settings", {}).get("devMode") or {}
            now = int(time.time()*1000)
            expiry = _expiry_ms(mode.get("expiresAt"))
            if mode.get("enabled") is not True or expiry is None or expiry <= now:
                raise MintRefused("developer_mode_required")
            conn = self._db()
            try:
                row = conn.execute("SELECT chain,payload,state FROM candidates WHERE key=?", (key,)).fetchone()
                if row is None:
                    raise MintRefused("candidate_missing")
                chain, raw, state = row
                view = chain_view.get(chain, {})
                if not view.get("supported") or not view.get("wallet"):
                    raise MintRefused("runtime_chain_or_wallet_unverified")
                if state != "staged":
                    return {"candidate_id": key, "state": state, "execution_enabled": False}
                today = day_of(now)
                if conn.execute("SELECT COUNT(*) FROM candidates WHERE preview_day=?", (today,)).fetchone()[0] >= 20:
                    raise MintRefused("preview_daily_budget")
                payload = json.loads(raw)
                if not payload.get("calldata") and not payload.get("unsigned_tx"):
                    raise MintRefused("verified_transaction_builder_required")
                with conn:
                    conn.execute("UPDATE candidates SET state='preview_requested',preview_day=? WHERE key=?", (today, key))
            finally:
                conn.close()
            try:
                file = Path(self.settings["state_dir"]) / ("preview-" + key + ".json")
                temporary = file.with_suffix(".json.partial")
                if file.exists() or file.is_symlink() or temporary.exists() or temporary.is_symlink():
                    raise WalletUnavailable("preview_artifact_exists")
                result = self.client.preview(chain=chain, wallet=view["wallet"],
                                             contract=payload.get("contract"), calldata=payload.get("calldata"),
                                             unsigned_tx=payload.get("unsigned_tx"))
                serialized = json.dumps(result, sort_keys=True, allow_nan=False).encode()
                if len(serialized) > 64 * 1024:
                    raise WalletUnavailable("preview_retention_size")
                digest = hashlib.sha256(serialized).hexdigest()
                # Hash and retain exactly the same bytes. A second serialization
                # can change key order and invalidate the recorded evidence hash.
                with temporary.open("xb") as output:
                    output.write(serialized)
                    output.flush()
                    os.fsync(output.fileno())
                temporary.chmod(0o600)
                # Atomic no-replace publication; never overwrite an older receipt.
                os.link(temporary, file)
                temporary.unlink()
                observed = int(time.time()*1000)
                evidence = inspect_preview(serialized, payload, wallet=view["wallet"],
                                           observed_ms=observed, at_ms=observed)
                blockers = evidence["blockers"]
                state = "preview_retained_blocked"
            except (WalletUnavailable, ValueError, OSError, MintRefused) as exc:
                digest, state = None, "preview_unavailable"
                evidence, observed = None, None
                blockers = [str(exc) if isinstance(exc, (WalletUnavailable, MintRefused)) else type(exc).__name__]
            conn = self._db()
            try:
                with conn:
                    conn.execute("UPDATE candidates SET state=?,preview_sha=?,blockers=?,preview_ms=?,evidence=? WHERE key=?",
                                 (state, digest, json.dumps(blockers), observed,
                                  json.dumps(evidence) if evidence else None, key))
            finally:
                conn.close()
            return {"candidate_id": key, "state": state, "preview_sha256": digest,
                    "blockers": blockers, "execution_enabled": False}


def handler(worker, token):
    if not isinstance(token, str) or re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token) is None:
        raise MintRefused("worker_token_invalid")

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *args):
            pass  # Never log authentication headers, addresses or candidate payloads.

        def do_POST(self):
            auth = self.headers.get("Authorization", "")
            if self.headers.get("Origin") or not hmac.compare_digest(auth.encode(), ("Bearer " + token).encode()):
                self.send_error(403)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= MAX_BODY:
                    raise MintRefused("request_size")
                body = json.loads(self.rfile.read(size))
                if self.path == "/status" and body == {}:
                    out = worker.status()
                elif self.path == "/refresh" and body == {}:
                    out = worker.refresh()
                elif self.path == "/stage":
                    out = worker.stage(body)
                elif self.path == "/preview" and set(body) == {"candidate_id"}:
                    out = worker.preview_one(body["candidate_id"])
                else:
                    raise MintRefused("operation_refused")
                code = 200
            except (ValueError, TypeError, KeyError, MintRefused) as exc:
                out = {"execution_enabled": False, "reason": str(exc) if isinstance(exc, MintRefused) else "request_invalid"}
                code = 400
            raw = json.dumps(out, allow_nan=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    return Handler


def call_worker(config, operation, payload=None):
    if operation not in ("status", "refresh", "stage", "preview"):
        raise MintRefused("operation_refused")
    port = config["port"]
    if type(port) is not int or not 1024 <= port <= 65535:
        raise MintRefused("worker_port")
    request = urllib.request.Request(f"http://127.0.0.1:{port}/{operation}",
                                     data=json.dumps(payload or {}).encode(), method="POST",
                                     headers={"Authorization": "Bearer " + config["token"],
                                              "Content-Type": "application/json"})
    # Bypass ambient HTTP proxies for this loopback-only service.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=40) as response:
            return json.loads(response.read(128 * 1024))
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read(128 * 1024))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("serve", "status", "refresh", "stage", "preview"))
    parser.add_argument("--settings", type=Path, default=Path.home()/".config/kaiba/baw-worker-client.json")
    parser.add_argument("--input", type=Path)
    args = parser.parse_args(argv)
    try:
        settings = json.loads(args.settings.read_text(encoding="utf-8"))
        if args.action == "serve":
            token = Path(settings["token_file"]).read_text(encoding="utf-8").strip()
            worker = MintPreflightWorker(settings)
            server = ThreadingHTTPServer(("127.0.0.1", settings["port"]), handler(worker, token))
            server.daemon_threads = True
            server.serve_forever()
        else:
            payload = json.loads(args.input.read_text(encoding="utf-8")) if args.input else {}
            print(json.dumps(call_worker(settings, args.action, payload), allow_nan=False))
        return 0
    except (OSError, ValueError, TypeError, KeyError, MintRefused) as exc:
        print(json.dumps({"execution_enabled": False,
                          "reason": str(exc) if isinstance(exc, MintRefused) else type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
