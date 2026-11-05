"""Prospective point-in-time capture for the frozen pullback study (``pullback-study/1``).

Lead (Claude), PULLBACK-CAPTURE-CLAUDE-20261102. Contract and its measurements:
docs/research/claude-pullback-capture-contract-20261102.md. The evaluator is Codex's unchanged
:mod:`kaiba.learning.pullback_study`; this module only feeds it.

* **Collector** (:class:`Capture`): a deterministic watch list of Solana tokens a Kaiba decision
  saw at a $100k+ cap, one GMGN ``token info`` read per watched token per :data:`CADENCE_MS`
  through the shared limiter at ``Priority.RESEARCH``. A fresh cached response (15 s TTL, no
  grace) is reused with its ORIGINAL clock. Research yields: no provider read while an order is
  unresolved, protection or the disk guard has halted entries, GMGN is cooling down or below half
  its bucket, the box is CPU-saturated or the day's read budget is spent; nothing is written at
  all at the disk guard's HALT line. The isolated store is ``data/pullback-capture/capture.db``;
  nothing is written to kaiba.db and no order or trade path is imported.
* **Exporter** (:func:`export`): ``pullback-input/1`` with ``population: selected`` and the
  decided-token census as ``scanned_count``, plus a SEPARATE execution sidecar. Every cashflow is
  modeled from observed prices and declared costs, so every filled case is ``evidence: modeled``;
  unfilled, unsellable and vanished cases stay in the input as unfilled or unsettled rows.

Market cap: GMGN ``token info`` reports none (inspected 2026-11-02). A cap is derived from the
SAME response only: ``circulating_supply x price`` is ``market_cap``; ``total_supply x price``
without a circulating figure is ``fdv`` and never qualifies; anything inconsistent or missing is
``unknown``. No supply is carried across responses or applied to an older price.

    python -m kaiba.learning.pullback_capture run --loop
    python -m kaiba.learning.pullback_capture status
    python -m kaiba.learning.pullback_capture coverage --window development
    python -m kaiba.learning.pullback_capture evaluate --window development
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import logging
import os
import shutil
import signal
import sqlite3
import time
from collections import Counter
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

VERSION = "pullback-capture/1"
#: Implementation revisions under the SAME frozen contract, recorded in the store's meta
#: (``implementation_history``) and shown in ``status`` and every export sidecar, so the study can
#: see which rows were captured under which code. 2026-11-03 (Codex review): ``_observe`` passed the
#: CYCLE-START clock to ``record``, so an honest receipt that arrived more than 5 s after the cycle
#: began (admission reads plus an up-to-8 s provider call on a CPU-saturated box) was rejected as
#: ``future_clock`` and never recorded. Records now compare the source's own clock with a FRESH wall
#: clock read at record time; a truly future receipt is still rejected.
IMPLEMENTATION = "pullback-capture/1+fresh-record-clock-20261103"
IMPLEMENTATION_ORIGINAL = "pullback-capture/1 (cycle-start record clock)"
SOURCE = "gmgn:token_info"
DAY_MS = 86_400_000
MINUTE_MS = 60_000
LAMPORTS = 1_000_000_000

#: Frozen before any outcome was observed. Its digest is stored on the first cycle; a cycle or an
#: export under a different contract refuses (``contract_changed``) instead of mixing two designs.
CONTRACT: dict[str, Any] = {
    "version": VERSION,
    "study": "pullback-study/1",
    "chain": "sol",
    "source": SOURCE,
    "selection": {"min_decision_cap_usd": 100_000, "max_dossier_age_s": 60,
                  "max_processing_lag_ms": 5 * MINUTE_MS, "order": ["decision_ts_ms", "token"],
                  "one_watch_per_token": True, "basis_of_decision_cap": "kaiba features (may be fdv)"},
    "max_tokens": 8,
    "cadence_ms": MINUTE_MS,
    "warmup_ms": 30 * MINUTE_MS,
    "signal_every_ms": 10 * MINUTE_MS,
    "signals_per_token": 10,
    "watch_ms": 190 * MINUTE_MS,
    "entry_delay_ms": 30_000,
    "entry_window_ms": 120_000,
    "exit": {"take_profit": 0.5, "stop": -0.3, "max_hold_ms": 60 * MINUTE_MS,
             "min_liquidity_usd": 500, "sell_at": "first sellable observation after the trigger"},
    "sizes": [{"size_lamports": 50_000_000, "bankroll_lamports": 500_000_000},
              {"size_lamports": 500_000_000, "bankroll_lamports": 5_000_000_000}],
    "reserve_lamports": 20_000_000,
    "costs": {"base": "paper_costs entry/exit bps + paper broker fee + broker tip per leg + "
                      "constant-product impact from GMGN liquidity at the leg",
              "flat325": "sensitivity only: 325 bps per leg, no tip, no impact"},
    "sol_usd": "native_prices sol row at or before the leg, recorded by the collector",
    "daily_reads": 12_000,
    "min_credit_fraction": 0.5,
    "max_load_per_cpu": 3.0,
    "cycle_budget_ms": 45_000,
    "store_max_bytes": 128 * 1024 * 1024,
    "min_free_bytes": 4 * 1024**3,
    "retention_days": 30,
    "windows": {"development_days": 7, "holdout_days": 7, "first_full_utc_day_after_start": True},
    "signal_tail_ms": 65 * MINUTE_MS,
}
CONTRACT_SHA256 = hashlib.sha256(json.dumps(CONTRACT, sort_keys=True).encode()).hexdigest()

#: Census precedence: a token's day row keeps the most advanced outcome it reached that day.
OUTCOMES = ("admitted", "list_full", "already_watched", "processed_late", "stale_dossier",
            "below_cap", "cap_missing")
UNRESOLVED_STATES = ("planned", "reserved", "submitting", "submitted", "partial", "unknown")
HALT_PREFIXES = ("protection_", "disk_guard:")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS watch(token TEXT PRIMARY KEY, admitted_ms INTEGER NOT NULL,
  decision_id TEXT NOT NULL, decision_cap_usd TEXT, dossier_age_s REAL);
CREATE TABLE IF NOT EXISTS observations(token TEXT NOT NULL, source TEXT NOT NULL,
  observed_ms INTEGER NOT NULL, available_ms INTEGER NOT NULL, origin TEXT NOT NULL,
  price_usd TEXT NOT NULL, circulating_supply TEXT, total_supply TEXT, max_supply TEXT,
  decimals INTEGER, reported_cap_usd TEXT, cap_detail TEXT NOT NULL, cap_basis TEXT NOT NULL,
  market_cap_usd TEXT, liquidity_usd TEXT, launchpad TEXT, launchpad_status TEXT,
  request_digest TEXT, response_digest TEXT, recorded_ms INTEGER NOT NULL,
  PRIMARY KEY(token, source, observed_ms));
CREATE TABLE IF NOT EXISTS gaps(token TEXT NOT NULL, ms INTEGER NOT NULL, reason TEXT NOT NULL,
  PRIMARY KEY(token, ms));
CREATE TABLE IF NOT EXISTS census(day TEXT NOT NULL, token TEXT NOT NULL, first_ms INTEGER NOT NULL,
  outcome TEXT NOT NULL, PRIMARY KEY(day, token));
CREATE TABLE IF NOT EXISTS ledger(day TEXT NOT NULL, key TEXT NOT NULL, n INTEGER NOT NULL,
  PRIMARY KEY(day, key));
CREATE TABLE IF NOT EXISTS native(ms INTEGER PRIMARY KEY, sol_usd TEXT NOT NULL);
"""


class Refused(RuntimeError):
    """An export or evaluation the contract does not allow (yet)."""


def utc_day(ms: int) -> str:
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.UTC).date().isoformat()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _dec(value: Any) -> Decimal | None:
    """A finite non-negative Decimal, or None. Booleans and junk are unknown, never zero."""
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d.is_finite() and d >= 0 else None


def _pos(value: Any) -> Decimal | None:
    d = _dec(value)
    return d if d is not None and d > 0 else None


# ---------------------------------------------------------------------- field semantics


def parse_token_info(data: Any) -> dict[str, Any] | None:
    """The fields the study may use from one raw ``token info`` body, with the cap's basis.

    ``price.price`` is USD per whole token; supplies are whole tokens. The vendor's
    ``price_1m``..``price_24h`` look-backs are never read (identical across windows in the
    inspected bodies, semantics unverified). Returns None without a positive price."""
    if not isinstance(data, dict):
        return None
    block = data.get("price")
    price = _pos(block.get("price")) if isinstance(block, dict) else None
    if price is None:
        return None
    circ, total, maxs = (_pos(data.get(k)) for k in ("circulating_supply", "total_supply", "max_supply"))
    reported = None
    for source in (data, block):
        for key in ("market_cap", "usd_market_cap"):
            reported = reported or _pos(source.get(key))
    derived = circ * price if circ is not None else None
    if circ is not None and total is not None and circ > total:
        detail, basis, cap = "supply_inconsistent", "unknown", None
    elif reported is not None:
        # A vendor cap of unstated basis counts only when the same response's circulating
        # supply reproduces it; otherwise it may be FDV, so it stays unknown.
        if derived is not None and abs(reported / derived - 1) <= Decimal("0.01"):
            detail, basis, cap = "reported_matches_circulating", "market_cap", reported
        else:
            detail, basis, cap = "reported_unverified", "unknown", None
    elif derived is not None:
        detail, basis, cap = "derived_circulating", "market_cap", derived
    elif total is not None:
        detail, basis, cap = "derived_total", "fdv", total * price
    else:
        detail, basis, cap = "missing", "unknown", None
    decimals = data.get("decimals")
    return {
        "price_usd": price, "circulating_supply": circ, "total_supply": total, "max_supply": maxs,
        "decimals": decimals if type(decimals) is int and 0 <= decimals <= 30 else None,
        "reported_cap_usd": reported, "cap_detail": detail, "cap_basis": basis, "market_cap_usd": cap,
        "liquidity_usd": _dec(data.get("liquidity")),
        "launchpad": str(data["launchpad"])[:40] if isinstance(data.get("launchpad"), str) else None,
        "launchpad_status": str(data.get("launchpad_status"))[:20] if data.get("launchpad_status") is not None else None,
    }


def failure_label(note: Any) -> str:
    """Fixed labels only: a provider note may carry arbitrary text."""
    text = str(note or "").lower()
    if text.startswith("limiter refused"):
        return "limiter_refused"
    if "429" in text or "rate limit" in text:
        return "rate_limited"
    return "provider_unavailable"


# ---------------------------------------------------------------------------- the store


def open_store(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def _meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _set_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, str(value)))


def _bump(conn: sqlite3.Connection, day: str, key: str, n: int = 1) -> None:
    if n:
        conn.execute("INSERT INTO ledger(day, key, n) VALUES (?, ?, ?) "
                     "ON CONFLICT(day, key) DO UPDATE SET n = n + excluded.n", (day, key, n))


def _ledger(conn: sqlite3.Connection, day: str) -> dict[str, int]:
    return {r[0]: r[1] for r in conn.execute("SELECT key, n FROM ledger WHERE day=?", (day,))}


def store_bytes(path: Path) -> int:
    return sum(os.path.getsize(p) for p in (path, Path(f"{path}-wal")) if os.path.exists(p))


def windows(capture_start_ms: int) -> dict[str, tuple[int, int]]:
    """Development: the first 7 FULL UTC days after the capture started. Holdout: the next 7."""
    dev_start = (capture_start_ms // DAY_MS + 1) * DAY_MS
    dev_end = dev_start + CONTRACT["windows"]["development_days"] * DAY_MS
    return {"development": (dev_start, dev_end),
            "holdout": (dev_end, dev_end + CONTRACT["windows"]["holdout_days"] * DAY_MS)}


# ------------------------------------------------------------------------- the collector


class Capture:
    """One collector cycle per :meth:`cycle`. Every dependency is injectable for offline tests."""

    def __init__(self, store_path: Path, db_path: Path, *, provider: Any = None,
                 clock: Callable[[], int] = _now_ms, monotonic: Callable[[], float] = time.monotonic,
                 disk_usage: Callable[[Path], Any] = shutil.disk_usage,
                 loadavg: Callable[[], tuple[float, float, float]] | None = getattr(os, "getloadavg", None),
                 cpu_count: int | None = None, reader_factory: Callable[[], Any] | None = None,
                 cache_reader: Callable[[str], tuple[Any, int] | None] | None = None) -> None:
        self.store_path, self.db_path = Path(store_path), Path(db_path)
        self.provider, self.clock, self.monotonic = provider, clock, monotonic
        self.disk_usage, self.loadavg = disk_usage, loadavg
        self.cpu_count = cpu_count or os.cpu_count() or 1
        self.reader_factory = reader_factory or self._guarded_reader
        self.cache_reader = cache_reader or _fresh_cache

    def _guarded_reader(self) -> Any:
        from kaiba.learning.autolearn_io import GuardedReader

        return GuardedReader(self.db_path, query_seconds=3, max_rows=600)

    # -- guards -------------------------------------------------------------------------

    def disk_state(self) -> tuple[str | None, dict[str, Any]]:
        """``(deferral, facts)``. Deferral at the disk guard's HALT line, under the free floor, or
        when this store reached its own cap. ACT is the guard pruning kaiba.db; this store is
        separate and bounded, so it keeps writing there."""
        from kaiba.ops import disk_guard as dg

        probe = self.store_path.parent
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        u = dg.usage_of(probe, self.disk_usage)
        level = dg.level_for(u.used_pct, warn_pct=dg.WARN_PCT, act_pct=dg.ACT_PCT, halt_pct=dg.HALT_PCT)
        size = store_bytes(self.store_path)
        facts = {"disk_used_pct": round(u.used_pct, 2), "disk_level": level, "free_bytes": u.free,
                 "store_bytes": size}
        if level == dg.HALT:
            return "disk_halt", facts
        if u.free < CONTRACT["min_free_bytes"]:
            return "disk_free_floor", facts
        if size >= CONTRACT["store_max_bytes"]:
            return "store_full", facts
        return None, facts

    def _gmgn_yield(self, reader: Any, now: int) -> str | None:
        """Why research must not reserve GMGN now, or None. The limiter stays the authority."""
        from kaiba.core.limiter import limits_for

        rows = reader.rows("SELECT credit_milli, last_refill_ms, banned_until_ms, penalty_level "
                           "FROM provider_state WHERE provider='gmgn'")
        bans = reader.rows("SELECT banned_until_ms FROM provider_family_bans "
                           "WHERE provider='gmgn' AND family='token'")
        if not rows:
            return "gmgn_state_unknown"
        st = rows[0]
        if int(st["banned_until_ms"] or 0) > now or any(int(b["banned_until_ms"] or 0) > now for b in bans):
            return "gmgn_cooldown"
        if int(st["penalty_level"] or 0) > 0:
            return "gmgn_penalty"
        lim = limits_for("gmgn")
        credit = min(lim.capacity * 1000, int(int(st["credit_milli"] or 0)
                     + max(0.0, (now - int(st["last_refill_ms"] or 0)) / 1000.0) * lim.refill_per_s * 1000))
        if credit < CONTRACT["min_credit_fraction"] * lim.capacity * 1000:
            return "gmgn_credit_low"
        return None

    def _trading_yield(self, reader: Any, now: int) -> str | None:
        marks = ",".join("?" * len(UNRESOLVED_STATES))
        unresolved = reader.q(f"SELECT COUNT(*) FROM orders WHERE state IN ({marks})", UNRESOLVED_STATES)
        if unresolved and int(unresolved[0][0] or 0) > 0:
            return "orders_unresolved"
        halt = reader.q("SELECT halted, halt_reason FROM risk_state WHERE day_key=?", (utc_day(now),))
        if halt and int(halt[0][0] or 0) and str(halt[0][1] or "").startswith(HALT_PREFIXES):
            return "entries_halted"
        return None

    def _cpu_yield(self) -> str | None:
        if self.loadavg is None:
            return None
        try:
            one = float(self.loadavg()[0])
        except OSError:
            return None
        return "cpu_saturated" if one > CONTRACT["max_load_per_cpu"] * self.cpu_count else None

    # -- admission ----------------------------------------------------------------------

    def _active(self, conn: sqlite3.Connection, at_ms: int) -> list[sqlite3.Row]:
        return conn.execute("SELECT token, admitted_ms FROM watch WHERE admitted_ms <= ? AND ? < admitted_ms + ? "
                            "ORDER BY admitted_ms, token", (at_ms, at_ms, CONTRACT["watch_ms"])).fetchall()

    def admit(self, conn: sqlite3.Connection, decisions: list[dict[str, Any]], now: int) -> Counter:
        """Census every Solana decision and admit in (decision time, token) order."""
        sel = CONTRACT["selection"]
        counts: Counter = Counter()
        for d in sorted(decisions, key=lambda d: (int(d["ts_ms"]), str(d["token"]))):
            if d.get("chain") != "sol":
                continue
            ts, token = int(d["ts_ms"]), str(d["token"])
            try:
                feats = json.loads(d.get("features_json") or "{}")
            except ValueError:
                feats = {}
            cap, age = _dec(feats.get("market_cap_usd")), _dec(feats.get("dossier_age_s"))
            if cap is None:
                outcome = "cap_missing"
            elif cap < sel["min_decision_cap_usd"]:
                outcome = "below_cap"
            elif age is None or age > sel["max_dossier_age_s"]:
                outcome = "stale_dossier"
            elif now - ts > sel["max_processing_lag_ms"]:
                outcome = "processed_late"
            elif conn.execute("SELECT 1 FROM watch WHERE token=?", (token,)).fetchone():
                outcome = "already_watched"
            elif len(self._active(conn, ts)) >= CONTRACT["max_tokens"]:
                outcome = "list_full"
            else:
                outcome = "admitted"
                conn.execute("INSERT INTO watch(token, admitted_ms, decision_id, decision_cap_usd, dossier_age_s) "
                             "VALUES (?, ?, ?, ?, ?)", (token, ts, str(d["decision_id"]), str(cap), float(age)))
            counts["sol_decisions"] += 1
            counts["decision_" + outcome] += 1
            day = utc_day(ts)
            row = conn.execute("SELECT outcome FROM census WHERE day=? AND token=?", (day, token)).fetchone()
            if row is None:
                conn.execute("INSERT INTO census(day, token, first_ms, outcome) VALUES (?, ?, ?, ?)",
                             (day, token, ts, outcome))
            elif OUTCOMES.index(outcome) < OUTCOMES.index(row[0]):
                conn.execute("UPDATE census SET outcome=? WHERE day=? AND token=?", (outcome, day, token))
        return counts

    # -- observations -------------------------------------------------------------------

    def record(self, conn: sqlite3.Connection, token: str, data: Any, observed_ms: int, origin: str,
               now: int, request_digest: str | None = None, response_digest: str | None = None) -> str:
        """Insert one observation at its ORIGINAL clock. Returns recorded / duplicate / a gap label."""
        if observed_ms > now + 5_000:
            return "future_clock"
        parsed = parse_token_info(data)
        if parsed is None:
            return "no_price"
        s = {k: (str(v) if isinstance(v, Decimal) else v) for k, v in parsed.items()}
        cur = conn.execute(
            "INSERT OR IGNORE INTO observations(token, source, observed_ms, available_ms, origin, price_usd, "
            "circulating_supply, total_supply, max_supply, decimals, reported_cap_usd, cap_detail, cap_basis, "
            "market_cap_usd, liquidity_usd, launchpad, launchpad_status, request_digest, response_digest, "
            "recorded_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (token, SOURCE, observed_ms, observed_ms, origin, s["price_usd"], s["circulating_supply"],
             s["total_supply"], s["max_supply"], s["decimals"], s["reported_cap_usd"], s["cap_detail"],
             s["cap_basis"], s["market_cap_usd"], s["liquidity_usd"], s["launchpad"], s["launchpad_status"],
             request_digest, response_digest, now))
        return "recorded" if cur.rowcount else "duplicate"

    def _provider(self) -> Any:
        if self.provider is None:
            from kaiba.providers import gmgn_cli

            self.provider = gmgn_cli
        return self.provider

    def cycle(self) -> dict[str, Any]:
        now = self.clock()
        started = self.monotonic()
        report: dict[str, Any] = {"version": VERSION, "now_ms": now, "status": "ok"}
        deferral, facts = self.disk_state()
        report.update(facts)
        if deferral:
            # Nothing is written at all: not even the ledger.
            report.update(status="deferred", reason=deferral)
            return report
        conn = open_store(self.store_path)
        try:
            with conn:
                stored = _meta(conn, "contract_sha256")
                if stored is None:
                    _set_meta(conn, "contract_sha256", CONTRACT_SHA256)
                    _set_meta(conn, "capture_start_ms", now)
                elif stored != CONTRACT_SHA256:
                    report.update(status="refused", reason="contract_changed")
                    return report
                if _meta(conn, "implementation") != IMPLEMENTATION:
                    history = json.loads(_meta(conn, "implementation_history") or "[]")
                    if not history and stored is not None:  # a store that began under the original code
                        history.append({"implementation": IMPLEMENTATION_ORIGINAL,
                                        "from_ms": int(_meta(conn, "capture_start_ms") or now)})
                    history.append({"implementation": IMPLEMENTATION, "from_ms": now})
                    _set_meta(conn, "implementation_history", json.dumps(history))
                    _set_meta(conn, "implementation", IMPLEMENTATION)
            day = utc_day(now)
            reader = self.reader_factory()
            try:
                yield_reason = self._trading_yield(reader, now) or self._gmgn_yield(reader, now)
                cursor = _meta(conn, "decision_cursor")
                if cursor is None:
                    # Prospective from the first cycle: start after the newest decision already
                    # older than the processing lag (idx_decisions_ts), never a backfill.
                    top = reader.q("SELECT rowid FROM decisions WHERE ts_ms < ? ORDER BY ts_ms DESC LIMIT 1",
                                   (now - CONTRACT["selection"]["max_processing_lag_ms"],))
                    cursor = str(int(top[0][0])) if top else "0"
                decisions = reader.rows(
                    "SELECT rowid AS rid, decision_id, ts_ms, chain, token, features_json FROM decisions "
                    "WHERE rowid > ? ORDER BY rowid LIMIT 500", (int(cursor),))
                native = reader.q("SELECT ts_ms, price_usd FROM native_prices WHERE chain='sol' "
                                  "ORDER BY ts_ms DESC LIMIT 1")
            except Exception as exc:  # noqa: BLE001 - SourceDeferred (WAL) or a busy source
                with conn:
                    _bump(conn, day, "cycles_source_deferred")
                report.update(status="deferred", reason="source:" + type(exc).__name__)
                return report
            yield_reason = yield_reason or self._cpu_yield()
            with conn:
                counts = self.admit(conn, decisions, now)
                if decisions:
                    _set_meta(conn, "decision_cursor", max(int(d["rid"]) for d in decisions))
                else:
                    _set_meta(conn, "decision_cursor", cursor)
                for key, n in counts.items():
                    _bump(conn, day, key, n)
                if native and _pos(native[0][1]) is not None:
                    conn.execute("INSERT OR IGNORE INTO native(ms, sol_usd) VALUES (?, ?)",
                                 (int(native[0][0]), str(native[0][1])))
            report["admission"] = dict(counts)
            report["yield"] = yield_reason
            report["observe"] = self._observe(conn, now, day, started, yield_reason)
            with conn:
                _bump(conn, day, "cycles")
                self._retain(conn, now)
                _set_meta(conn, "last_cycle", json.dumps(report, sort_keys=True, default=str))
            return report
        finally:
            conn.close()

    def _observe(self, conn: sqlite3.Connection, now: int, day: str, started: float,
                 yield_reason: str | None) -> dict[str, int]:
        from kaiba.core.limiter import Priority, limits_for
        from kaiba.core.schemas import Chain, EvidenceBasis

        weight = limits_for("gmgn").weight_for("token.info")
        out: Counter = Counter()
        due = []
        for row in self._active(conn, now):
            last = conn.execute("SELECT MAX(observed_ms) FROM observations WHERE token=?", (row["token"],)).fetchone()[0]
            if last is None or now - last >= CONTRACT["cadence_ms"] - 5_000:
                due.append((last or 0, row["admitted_ms"], row["token"]))
        stop_fetching = yield_reason
        for _last, _adm, token in sorted(due):
            hit = self.cache_reader(token)
            if hit is not None:
                data, fetched_ms = hit
                with conn:  # a fresh clock: the file may have been written after this cycle began
                    result = self.record(conn, token, data, fetched_ms, "cache", self.clock())
                if result == "recorded":
                    out["cache_reused"] += 1
                    continue
            if not stop_fetching and _ledger(conn, day).get("provider_weight", 0) + weight > CONTRACT["daily_reads"]:
                stop_fetching = "daily_budget"
            if not stop_fetching and (self.monotonic() - started) * 1000 > CONTRACT["cycle_budget_ms"]:
                stop_fetching = "cycle_budget"
            if stop_fetching:
                self._gap(conn, token, now, "deferred:" + stop_fetching)
                out["deferred"] += 1
                continue
            with conn:
                _bump(conn, day, "provider_attempts")
                _bump(conn, day, "provider_weight", weight)
            try:
                got = self._provider().token_info(token, Chain.SOL, priority=Priority.RESEARCH, ttl_s=15,
                                                  stale_grace_s=0, timeout_s=8, wait_for_slot_s=0)
            except Exception:  # noqa: BLE001 - never retried in-run
                got = None
            if got is None or not got.ok:
                label = failure_label(got.receipt.note if got is not None else "exception")
                self._gap(conn, token, now, label)
                out[label] += 1
                if label in ("limiter_refused", "rate_limited"):
                    stop_fetching = label  # yield the rest of the cycle; no retry
                continue
            origin = "fetched" if got.receipt.basis is EvidenceBasis.PROVIDER_REPORTED else "cache"
            with conn:  # a fresh clock: the receipt is stamped when the call returned, not at cycle start
                result = self.record(conn, token, got.data, int(got.receipt.observed_at_ms), origin, self.clock(),
                                     got.receipt.request_digest, got.receipt.response_digest)
                if result not in ("recorded", "duplicate"):
                    self._gap_in_tx(conn, token, now, result)
                if origin == "fetched":
                    _bump(conn, day, "provider_reads")
            out[origin if result == "recorded" else result] += 1
        with conn:
            for key, n in out.items():
                _bump(conn, day, "observe_" + key, n)
        return dict(out)

    def _gap(self, conn: sqlite3.Connection, token: str, now: int, reason: str) -> None:
        with conn:
            self._gap_in_tx(conn, token, now, reason)

    @staticmethod
    def _gap_in_tx(conn: sqlite3.Connection, token: str, now: int, reason: str) -> None:
        conn.execute("INSERT OR IGNORE INTO gaps(token, ms, reason) VALUES (?, ?, ?)", (token, now, reason[:60]))

    def _retain(self, conn: sqlite3.Connection, now: int) -> None:
        """Rows older than the retention horizon AND outside every frozen window are deleted."""
        start = _meta(conn, "capture_start_ms")
        if start is None:
            return
        w = windows(int(start))
        keep_from = w["development"][0] - CONTRACT["warmup_ms"] - CONTRACT["watch_ms"]
        keep_to = w["holdout"][1] + CONTRACT["watch_ms"]
        horizon = now - CONTRACT["retention_days"] * DAY_MS
        for table, col in (("observations", "observed_ms"), ("gaps", "ms")):
            conn.execute(f"DELETE FROM {table} WHERE {col} < ? AND ({col} < ? OR {col} > ?)",
                         (horizon, keep_from, keep_to))


def _fresh_cache(token: str) -> tuple[Any, int] | None:
    """A ``token info`` response the system already fetched, at most 15 s old, with its clock."""
    from kaiba.providers._http import cache_read

    hit = cache_read("gmgn", f"gmgn-cli token info --address {token} --chain sol --raw", 15.0, 0.0)
    if hit is None:
        return None
    data, stale, fetched_ms = hit
    return None if stale else (data, int(fetched_ms))


# ---------------------------------------------------------------------------- execution


def _impact_bps(size_usd: Decimal | None, liquidity: Decimal | None) -> Decimal | None:
    """Constant-product price impact of one leg against half the vendor-reported liquidity."""
    if size_usd is None or liquidity is None or liquidity <= 0:
        return None
    return Decimal(10_000) * size_usd / (liquidity / 2)


def cost_legs(name: str) -> dict[str, Any]:
    """Per-leg costs, read at export so the version used is recorded with the result."""
    if name == "flat325":
        return {"name": name, "entry_bps": 325, "exit_bps": 325, "tip_lamports": 0, "impact": False}
    if name != "base":
        raise Refused("unknown_cost_model")
    from kaiba.core.schemas import Chain
    from kaiba.execution import paper, paper_costs

    broker = paper_costs.broker_fee_bps(Chain.SOL)
    src = Path(paper_costs.__file__).read_bytes() + Path(paper.__file__).read_bytes()
    return {"name": name, "entry_bps": paper_costs.entry_bps(Chain.SOL) + broker,
            "exit_bps": paper_costs.exit_bps(Chain.SOL) + broker,
            "tip_lamports": int(paper.DEFAULT_TIP_NATIVE[Chain.SOL]), "impact": True,
            "source_sha256": hashlib.sha256(src).hexdigest()}


def _sol_usd(native: list[tuple[int, Decimal]], at_ms: int) -> Decimal | None:
    best = None
    for ms, usd in native:
        if ms <= at_ms:
            best = usd
        else:
            break
    return best


def simulate(obs: list[dict[str, Any]], signal_grid_ms: int, *, size: int, costs: dict[str, Any],
             native: list[tuple[int, Decimal]]) -> dict[str, Any]:
    """One opportunity's modeled execution from point-in-time observations. Never values a hold."""
    c, ex = CONTRACT, CONTRACT["exit"]
    case: dict[str, Any] = {"grid_ms": signal_grid_ms, "evidence": "unknown", "entry_ms": None,
                            "cost_lamports": None, "settled_ms": None, "proceeds_lamports": None}
    sig = next((o for o in obs if signal_grid_ms <= o["observed_ms"] < signal_grid_ms + c["cadence_ms"]), None)
    case["signal_ms"] = sig["observed_ms"] if sig else signal_grid_ms
    if sig is None:
        return {**case, "status": "unfilled_no_signal_observation"}
    lo = case["signal_ms"] + c["entry_delay_ms"]
    entry = next((o for o in obs if lo <= o["observed_ms"] <= lo + c["entry_window_ms"]), None)
    if entry is None:
        return {**case, "status": "unfilled_no_entry_observation"}
    if entry["liquidity_usd"] is None or entry["liquidity_usd"] < ex["min_liquidity_usd"]:
        return {**case, "status": "unfilled_entry_liquidity", "entry_liquidity_usd": entry["liquidity_usd"]}
    sol = _sol_usd(native, entry["observed_ms"])
    entry_impact = _impact_bps(Decimal(size) / LAMPORTS * sol if sol else None, entry["liquidity_usd"])
    if costs["impact"] and entry_impact is None:
        return {**case, "status": "unfilled_unpriced_impact"}
    entry_mult = 1 + (Decimal(costs["entry_bps"]) + (entry_impact if costs["impact"] else 0)) / 10_000
    p0 = entry["price_usd"]
    case.update(entry_ms=entry["observed_ms"], cost_lamports=size + costs["tip_lamports"], evidence="modeled",
                entry_price_usd=str(p0), entry_liquidity_usd=str(entry["liquidity_usd"]),
                entry_impact_bps=str(round(entry_impact, 2)) if costs["impact"] else None)
    trigger, seen_after = None, 0
    for o in obs:
        if o["observed_ms"] <= entry["observed_ms"]:
            continue
        seen_after += 1
        r = o["price_usd"] / p0 - 1
        if trigger is None:
            if r >= Decimal(str(ex["take_profit"])):
                trigger = "take_profit"
            elif r <= Decimal(str(ex["stop"])):
                trigger = "stop"
            elif o["observed_ms"] >= entry["observed_ms"] + ex["max_hold_ms"]:
                trigger = "time"
        if trigger is None or o["liquidity_usd"] is None or o["liquidity_usd"] < ex["min_liquidity_usd"]:
            continue
        gross = o["price_usd"] / p0
        exit_impact = _impact_bps(Decimal(size) / LAMPORTS * gross * sol if sol else None, o["liquidity_usd"])
        exit_mult = 1 - (Decimal(costs["exit_bps"]) + ((exit_impact or 0) if costs["impact"] else 0)) / 10_000
        proceeds = int(Decimal(size) * gross * max(exit_mult, Decimal(0)) / entry_mult) - costs["tip_lamports"]
        return {**case, "status": "settled_" + trigger, "settled_ms": o["observed_ms"],
                "proceeds_lamports": max(proceeds, 0), "exit_price_usd": str(o["price_usd"]),
                "exit_liquidity_usd": str(o["liquidity_usd"]), "gross_multiple": str(round(gross, 6))}
    status = ("vanished_after_entry" if seen_after == 0 else
              "exit_unsellable" if trigger else "unresolved_observations_ended")
    return {**case, "status": status}


# ------------------------------------------------------------------------------ export


def _completeness_case(obs: list[dict[str, Any]], grid: int) -> dict[str, Any]:
    """Data availability for one opportunity WITHOUT any price after the signal: the study's own
    classification of what was known at the signal, and whether an entry observation exists."""
    from kaiba.learning import pullback_study

    c = CONTRACT
    sig = next((o for o in obs if grid <= o["observed_ms"] < grid + c["cadence_ms"]), None)
    signal_ms = sig["observed_ms"] if sig else grid
    known = [{"source": SOURCE, "observed_ms": o["observed_ms"], "available_ms": o["available_ms"],
              "price_usd": float(o["price_usd"]), "cap_basis": o["cap_basis"],
              "market_cap_usd": float(o["market_cap_usd"]) if o["market_cap_usd"] else None}
             for o in obs if o["available_ms"] <= signal_ms]
    _flags, reason = pullback_study.classify(known, signal_ms)
    lo = signal_ms + c["entry_delay_ms"]
    has_entry = any(lo <= o["observed_ms"] <= lo + c["entry_window_ms"] for o in obs)
    status = ("no_signal_observation" if sig is None else
              "entry_observation_available" if has_entry else "no_entry_observation")
    return {"grid_ms": grid, "signal_ms": signal_ms, "status": status, "classification": reason,
            "evidence": "unknown", "entry_ms": None, "cost_lamports": None, "settled_ms": None,
            "proceeds_lamports": None}


def _load_obs(conn: sqlite3.Connection, token: str) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT observed_ms, available_ms, price_usd, cap_basis, market_cap_usd, liquidity_usd "
                        "FROM observations WHERE token=? AND source=? ORDER BY observed_ms", (token, SOURCE))
    return [{"observed_ms": r[0], "available_ms": r[1], "price_usd": Decimal(r[2]), "cap_basis": r[3],
             "market_cap_usd": _pos(r[4]) if r[3] == "market_cap" else None, "liquidity_usd": _dec(r[5])}
            for r in rows]


def export(store_path: Path, window: str, *, size: int, costs: str = "base", now_ms: int | None = None,
           completeness_only: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(pullback-input/1, execution sidecar)`` for a closed frozen window.

    ``completeness_only`` builds a window-to-date view WITHOUT cashflows (every case unfilled,
    nothing priced after a signal), so coverage can be watched without peeking at outcomes."""
    now = _now_ms() if now_ms is None else now_ms
    if window not in ("development", "holdout"):
        raise Refused("unknown_window")
    if not any(s["size_lamports"] == size for s in CONTRACT["sizes"]):
        raise Refused("size_not_in_contract")
    conn = open_store(store_path)
    try:
        if _meta(conn, "contract_sha256") != CONTRACT_SHA256:
            raise Refused("contract_changed")
        start_ms, end_ms = windows(int(_meta(conn, "capture_start_ms")))[window]
        if not completeness_only:
            if now < end_ms + 10 * MINUTE_MS:
                raise Refused("window_open")
            if window == "holdout" and _meta(conn, "reported:development") is None:
                raise Refused("development_not_reported")
        legs = cost_legs(costs) if not completeness_only else {"name": "none"}
        native = [(r[0], Decimal(r[1])) for r in conn.execute("SELECT ms, sol_usd FROM native ORDER BY ms")]
        census = Counter(r[0] for r in conn.execute(
            "SELECT outcome FROM census WHERE day >= ? AND day < ?", (utc_day(start_ms), utc_day(end_ms))))
        scanned = sum(census.values())
        tail = CONTRACT["signal_tail_ms"]
        observations, cases, execution = [], [], []
        watched = conn.execute("SELECT token, admitted_ms FROM watch WHERE admitted_ms < ? AND admitted_ms + ? > ? "
                               "ORDER BY admitted_ms, token", (end_ms, CONTRACT["watch_ms"], start_ms)).fetchall()
        for token, admitted in watched:
            # Nothing at or after the window's end is used: a later price is another window's data.
            obs = [o for o in _load_obs(conn, token) if o["observed_ms"] < end_ms]
            emitted = False
            for k in range(CONTRACT["signals_per_token"]):
                grid = admitted + CONTRACT["warmup_ms"] + k * CONTRACT["signal_every_ms"]
                if not start_ms <= grid < end_ms - tail:
                    continue
                case = (_completeness_case(obs, grid) if completeness_only
                        else simulate(obs, grid, size=size, costs=legs, native=native))
                case_id = f"{token}:{k}"
                cases.append({"id": case_id, "token": token, "signal_ms": case["signal_ms"],
                              "entry_ms": case["entry_ms"], "cost_lamports": case["cost_lamports"],
                              "settled_ms": case["settled_ms"], "proceeds_lamports": case["proceeds_lamports"],
                              "evidence": case["evidence"]})
                execution.append({"id": case_id, **{k2: v for k2, v in case.items() if k2 not in ("evidence",)},
                                  "evidence": case["evidence"]})
                emitted = True
            if emitted:
                observations += [{"token": token, "source": SOURCE, "observed_ms": o["observed_ms"],
                                  "available_ms": o["available_ms"], "price_usd": float(o["price_usd"]),
                                  "cap_basis": o["cap_basis"],
                                  "market_cap_usd": float(o["market_cap_usd"]) if o["market_cap_usd"] else None}
                                 for o in obs if start_ms - CONTRACT["warmup_ms"] <= o["observed_ms"] < end_ms]
        if len(cases) > scanned:
            # pullback_study.validate requires scanned_count >= opportunities; say why, do not pad.
            raise Refused(f"census_smaller_than_opportunities:{scanned}<{len(cases)}")
        gaps = Counter(r[0] for r in conn.execute("SELECT reason FROM gaps WHERE ms >= ? AND ms < ?",
                                                  (start_ms, end_ms)))
        data = {"version": "pullback-input/1", "start_ms": start_ms, "end_ms": end_ms, "chain": "sol",
                "population": "selected", "scanned_count": scanned,
                "observations": observations, "opportunities": cases,
                "capture": {"version": VERSION, "contract_sha256": CONTRACT_SHA256, "window": window,
                            "size_lamports": size, "costs": legs, "completeness_only": completeness_only,
                            "scanned_count_basis": "distinct sol tokens with a Kaiba decision per UTC day "
                                                   "(not every token the scanner touched)",
                            "census": dict(census), "measured_cashflows": 0}}
        sidecar = {"version": VERSION, "window": window, "size_lamports": size, "costs": legs,
                   "status_counts": dict(Counter(e["status"] for e in execution)),
                   "classification_counts": dict(Counter(e["classification"] for e in execution
                                                         if "classification" in e)),
                   "gap_counts": dict(gaps), "watched_tokens": len(watched), "execution": execution,
                   "implementation_history": json.loads(_meta(conn, "implementation_history") or "[]"),
                   "notes": ["every filled case is modeled from GMGN token info prices; no fill was measured",
                             "an unsettled case locks its cost until the window ends; it is never valued",
                             "SOL/USD is the collector's last native_prices sol row at or before each leg"]}
        return data, sidecar
    finally:
        conn.close()


def evaluate(store_path: Path, window: str, out_dir: Path, *, now_ms: int | None = None) -> dict[str, Any]:
    """Run the unchanged evaluator once per (size, its bankroll) under base costs, plus the flat
    sensitivity. One shot per window: a second evaluation of the same window refuses."""
    from kaiba.learning import pullback_study

    conn = open_store(store_path)
    try:
        if _meta(conn, f"reported:{window}") is not None:
            raise Refused("window_already_reported")
    finally:
        conn.close()
    reports = []
    for s in CONTRACT["sizes"]:
        for costs in ("base", "flat325"):
            data, sidecar = export(store_path, window, size=s["size_lamports"], costs=costs, now_ms=now_ms)
            result = pullback_study.study(data, capitals=(s["bankroll_lamports"],),
                                          reserve=CONTRACT["reserve_lamports"])
            reports.append({"size_lamports": s["size_lamports"], "bankroll_lamports": s["bankroll_lamports"],
                            "costs": costs, "study": result, "execution_status": sidecar["status_counts"],
                            "gaps": sidecar["gap_counts"], "census": data["capture"]["census"],
                            "scanned_count": data["scanned_count"], "watched_tokens": sidecar["watched_tokens"]})
    out = {"version": VERSION, "window": window, "contract_sha256": CONTRACT_SHA256,
           "profit_proven": False, "reports": reports}
    out_dir.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(out, sort_keys=True, default=str).encode()
    path = out_dir / f"{window}-{hashlib.sha256(blob).hexdigest()[:12]}.json"
    path.write_bytes(blob)
    conn = open_store(store_path)
    try:
        with conn:
            _set_meta(conn, f"reported:{window}", json.dumps({"path": str(path), "ms": _now_ms()}))
    finally:
        conn.close()
    return {"path": str(path), **out}


def status(store_path: Path, *, now_ms: int | None = None) -> dict[str, Any]:
    now = _now_ms() if now_ms is None else now_ms
    if not store_path.exists():
        return {"version": VERSION, "status": "not_started"}
    conn = open_store(store_path)
    try:
        start = _meta(conn, "capture_start_ms")
        day = utc_day(now)
        active = conn.execute("SELECT COUNT(*) FROM watch WHERE admitted_ms <= ? AND ? < admitted_ms + ?",
                              (now, now, CONTRACT["watch_ms"])).fetchone()[0]
        return {"version": VERSION, "contract_sha256": CONTRACT_SHA256,
                "contract_matches": _meta(conn, "contract_sha256") == CONTRACT_SHA256,
                "capture_start_ms": int(start) if start else None,
                "windows": windows(int(start)) if start else None,
                "watched_total": conn.execute("SELECT COUNT(*) FROM watch").fetchone()[0], "watched_now": active,
                "observations": conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0],
                "cap_detail": {r[0]: r[1] for r in conn.execute(
                    "SELECT cap_detail, COUNT(*) FROM observations GROUP BY cap_detail")},
                "today": _ledger(conn, day), "store_bytes": store_bytes(store_path),
                "last_cycle": json.loads(_meta(conn, "last_cycle") or "null"),
                "reported": {w: _meta(conn, f"reported:{w}") for w in ("development", "holdout")},
                "implementation": _meta(conn, "implementation"),
                "implementation_history": json.loads(_meta(conn, "implementation_history") or "[]")}
    finally:
        conn.close()


# --------------------------------------------------------------------------------- CLI


def default_store() -> Path:
    from kaiba.core.config import get_settings

    return Path(get_settings().kaiba_data_dir) / "pullback-capture" / "capture.db"


def run_loop(capture: Capture, *, sleep: Callable[[float], None] = time.sleep,
             should_stop: Callable[[], bool] = lambda: False, max_cycles: int | None = None) -> int:
    cycles = 0
    while not should_stop() and (max_cycles is None or cycles < max_cycles):
        started = time.monotonic()
        try:
            report = capture.cycle()
            log.info("pullback_capture %s", json.dumps({k: report.get(k) for k in
                     ("status", "reason", "yield", "admission", "observe")}, default=str))
        except Exception:  # noqa: BLE001 - one bad cycle must not end the capture
            log.exception("pullback_capture cycle failed")
        cycles += 1
        sleep(max(5.0, CONTRACT["cadence_ms"] / 1000 - (time.monotonic() - started)))
    return cycles


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m kaiba.learning.pullback_capture", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("--loop", action="store_true")
    sub.add_parser("status")
    for name in ("coverage", "evaluate"):
        p = sub.add_parser(name)
        p.add_argument("--window", choices=("development", "holdout"), required=True)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    store = default_store()
    if args.cmd == "run":
        from kaiba.core.config import get_settings

        cap = Capture(store, Path(get_settings().db_path))
        if not args.loop:
            print(json.dumps(cap.cycle(), sort_keys=True, default=str))
            return 0
        stop = {"flag": False}
        signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))
        run_loop(cap, should_stop=lambda: stop["flag"])
        return 0
    try:
        if args.cmd == "status":
            print(json.dumps(status(store), sort_keys=True, default=str))
        elif args.cmd == "coverage":
            data, side = export(store, args.window, size=CONTRACT["sizes"][0]["size_lamports"], completeness_only=True)
            print(json.dumps({"window": args.window, "scanned_count": data["scanned_count"],
                              "census": data["capture"]["census"], "watched_tokens": side["watched_tokens"],
                              "opportunities": len(data["opportunities"]),
                              "observations": len(data["observations"]), "status_counts": side["status_counts"],
                              "classification_counts": side["classification_counts"],
                              "gaps": side["gap_counts"]}, sort_keys=True))
        else:
            out = evaluate(store, args.window, store.parent / "reports")
            print(json.dumps({"path": out["path"], "window": out["window"]}, sort_keys=True))
    except Refused as exc:
        print(json.dumps({"version": VERSION, "status": "refused", "reason": str(exc)}))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
