"""Atomic gas reservations for the owner's gas-only NFT envelope.

This is a private worker ledger, never the canonical Kaiba database. Costs use
integer micro-USD valued by the worker's verified quote. Unknown submissions
retain their reservation across day boundaries until reconciled.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

# BAW-FUNDED-MINTS-20261102: one unchanged global gas envelope across all five.
CHAINS = frozenset({"bsc", "robinhood", "sol", "base", "eth"})
WIB = dt.timezone(dt.timedelta(hours=7))


class MintRefused(RuntimeError):
    pass


def require(ok, reason):
    if not ok:
        raise MintRefused(reason)


def day_of(ms):
    require(type(ms) is int and ms >= 0, "invalid_clock")
    return dt.datetime.fromtimestamp(ms / 1000, WIB).date().isoformat()


@dataclass(frozen=True)
class GasEnvelope:
    per_mint_micro_usd: int = 100_000
    daily_micro_usd: int = 10_000_000

    def __post_init__(self):
        require(type(self.per_mint_micro_usd) is int and type(self.daily_micro_usd) is int and
                0 < self.per_mint_micro_usd <= self.daily_micro_usd, "invalid_gas_envelope")


class GasLedger:
    def __init__(self, path, envelope=GasEnvelope()):
        self.path, self.envelope = Path(path), envelope
        require(self.path.name != "kaiba.db" and not self.path.is_symlink(), "private_ledger_required")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.executescript("""
              CREATE TABLE IF NOT EXISTS envelope(singleton INTEGER PRIMARY KEY CHECK(singleton=1), body TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS gas(
                key TEXT PRIMARY KEY, chain TEXT NOT NULL, payload_sha TEXT NOT NULL,
                reserved_ms INTEGER NOT NULL, upper_usd INTEGER NOT NULL, upper_native TEXT NOT NULL,
                state TEXT NOT NULL, provider_id TEXT, settled_ms INTEGER,
                actual_usd INTEGER, actual_native TEXT, receipt_sha TEXT);
              CREATE TABLE IF NOT EXISTS control(singleton INTEGER PRIMARY KEY CHECK(singleton=1), halted INTEGER NOT NULL);
              CREATE INDEX IF NOT EXISTS gas_settled ON gas(state,settled_ms);
            """)
            body = json.dumps(envelope.__dict__, sort_keys=True)
            conn.execute("INSERT OR IGNORE INTO envelope VALUES(1,?)", (body,))
            require(conn.execute("SELECT body FROM envelope WHERE singleton=1").fetchone()[0] == body,
                    "envelope_changed_requires_operator_migration")
            conn.execute("INSERT OR IGNORE INTO control VALUES(1,0)")

    @contextlib.contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=1, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            yield conn
        finally:
            conn.close()

    def _usage(self, conn, at_ms):
        day = day_of(at_ms)
        pending = conn.execute("SELECT COALESCE(SUM(upper_usd),0) FROM gas WHERE state IN ('reserved','submitted','unknown')").fetchone()[0]
        # Every actual fee (including reverted transactions) counts on settlement
        # day. Unresolved previous-day submits continue to consume today's room.
        start = dt.datetime.combine(dt.date.fromisoformat(day), dt.time(), WIB)
        lo = int(start.timestamp() * 1000)
        spent = conn.execute("SELECT COALESCE(SUM(actual_usd),0) FROM gas "
                             "WHERE state='settled' AND settled_ms>=? AND settled_ms<?",
                             (lo, lo + 86_400_000)).fetchone()[0]
        return int(pending), int(spent)

    def reserve(self, *, key, chain, payload_sha, upper_usd, upper_native, at_ms):
        require(chain in CHAINS, "chain_refused")
        require(all(isinstance(v, str) and re.fullmatch(r"[0-9a-f]{64}", v) for v in (key, payload_sha)), "intent_identity")
        require(type(upper_usd) is int and 0 < upper_usd <= self.envelope.per_mint_micro_usd,
                "per_mint_gas_cap")
        require(type(upper_native) is int and upper_native > 0, "native_gas_bound_missing")
        day_of(at_ms)
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                old = conn.execute("SELECT * FROM gas WHERE key=?", (key,)).fetchone()
                if old:
                    require((old["chain"], old["payload_sha"], old["upper_usd"], old["upper_native"]) ==
                            (chain, payload_sha, upper_usd, str(upper_native)), "idempotency_conflict")
                    conn.commit()
                    return {"new": False, "state": old["state"]}
                require(not conn.execute("SELECT halted FROM control WHERE singleton=1").fetchone()[0], "gas_ledger_halted")
                pending, spent = self._usage(conn, at_ms)
                require(pending + spent + upper_usd <= self.envelope.daily_micro_usd, "daily_gas_cap")
                conn.execute("INSERT INTO gas(key,chain,payload_sha,reserved_ms,upper_usd,upper_native,state) VALUES(?,?,?,?,?,?,'reserved')",
                             (key, chain, payload_sha, at_ms, upper_usd, str(upper_native)))
                conn.commit()
                return {"new": True, "state": "reserved"}
            except BaseException:
                conn.rollback()
                raise

    def submitted(self, key, provider_id, *, ambiguous=False):
        require(isinstance(provider_id, str) and 0 < len(provider_id) <= 160, "provider_id_missing")
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT state,provider_id FROM gas WHERE key=?", (key,)).fetchone()
                require(row is not None, "reservation_missing")
                require(row["state"] == "reserved" or
                        (row["state"] in ("submitted", "unknown") and row["provider_id"] == provider_id), "submit_state_conflict")
                first_claim = row["state"] == "reserved"
                conn.execute("UPDATE gas SET state=?,provider_id=? WHERE key=?",
                             ("unknown" if ambiguous else "submitted", provider_id, key))
                conn.commit()
                return first_claim
            except BaseException:
                conn.rollback()
                raise

    def settle(self, key, *, actual_usd, actual_native, settled_ms, receipt_sha):
        require(type(actual_usd) is int and actual_usd >= 0 and type(actual_native) is int and actual_native >= 0,
                "actual_fee_unavailable")
        require(isinstance(receipt_sha, str) and re.fullmatch(r"[0-9a-f]{64}", receipt_sha), "receipt_unbound")
        day_of(settled_ms)
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT * FROM gas WHERE key=?", (key,)).fetchone()
                require(row is not None and row["state"] in ("submitted", "unknown", "settled"), "settlement_state")
                require(settled_ms >= row["reserved_ms"], "settlement_clock")
                if row["state"] == "settled":
                    require((row["actual_usd"], row["actual_native"], row["settled_ms"], row["receipt_sha"]) ==
                            (actual_usd, str(actual_native), settled_ms, receipt_sha), "settlement_conflict")
                    conn.commit()
                    return
                conn.execute("UPDATE gas SET state='settled',settled_ms=?,actual_usd=?,actual_native=?,receipt_sha=? WHERE key=?",
                             (settled_ms, actual_usd, str(actual_native), receipt_sha, key))
                if actual_usd > row["upper_usd"] or actual_native > int(row["upper_native"]):
                    conn.execute("UPDATE control SET halted=1 WHERE singleton=1")
                pending, spent = self._usage(conn, settled_ms)
                if pending + spent > self.envelope.daily_micro_usd:
                    conn.execute("UPDATE control SET halted=1 WHERE singleton=1")
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def status(self, at_ms):
        with self.connection() as conn:
            pending, spent = self._usage(conn, at_ms)
            halted = bool(conn.execute("SELECT halted FROM control WHERE singleton=1").fetchone()[0])
        return {"day_wib": day_of(at_ms), "pending_micro_usd": pending, "spent_micro_usd": spent,
                "remaining_micro_usd": max(0, self.envelope.daily_micro_usd - pending - spent),
                "halted": halted, **self.envelope.__dict__}
