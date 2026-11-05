"""B-tier first buy: the forward PAPER evaluator (TIER-FORWARD-CLAUDE-20261105).

Pre-registered in docs/research/claude-tier-forward-prereg-20261105.md. Every rule below is that
text; a change is a new registration, never an edit.

It reads the ``swaps`` tape for the frozen wallets, stamps ``seen_ms`` when a first buy of a token
reaches the tape, prices the entry at least 60 s later with the engine's sol paper readers at
``Priority.RESEARCH``, and walks the live exit ladder on real quotes. It reuses the H5 ledger, tape
readers, price reader and pass line, so a token nothing can price is counted, never filled.

It never writes positions, orders or decisions, never touches risk.yaml, and has no order path.

    python -m kaiba.learning.tier_forward run        # the service loop
    python -m kaiba.learning.tier_forward report     # rebuild the report from the ledger
"""
from __future__ import annotations

import hashlib
import json
import logging
import signal
import sqlite3
import statistics as st
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from kaiba.learning.h5_reentry import (
    Ledger,
    Read,
    ReadDeferred,
    _atomic_json,
    box_fetchers,
    default_reader,
    verdict,
)

log = logging.getLogger(__name__)

VERSION = "tier-forward/1"
T0_MS = 1_793_858_400_000                # 2026-11-05T06:00:00Z
MEMBERS_FILE = "frozen-20261028.json"
MEMBERS_SHA = "45302ddffc9af7e707c55c3dae4279c8619b7a47240e9f8ee00e186a4920b7ef"
MIN = 60_000
DAY = 86_400_000
LOOKBACK_MS = 13 * DAY                   # a token a frozen wallet bought in this span is not a first buy
POLL_WINDOW_MS = 90 * MIN                # rows land late; re-read this window, dedup by row id
MAX_LAG_MS = 60 * MIN                    # older than this when seen: too_late, never entered
ENTRY_DELAY_MS = 60_000
ENTRY_WINDOW_MS = 10 * MIN
OBS_INTERVAL_MS = 30_000
MAX_HOLD_MS = 200 * MIN
STALE_MS = 60 * MIN
STALE_BAND = 0.01                        # within 1%: quotes carry the SOL price, so never exactly equal
NO_QUOTE_MS = 30 * MIN
SUPPLY = 1_000_000_000
MIN_CAP, MAX_CAP = 10_000.0, 3_000_000.0
MIN_LIQUIDITY_USD = 10_000.0
COST = 5.5
STOP_FLOOR = 0.70
STOP_NET = -40.0
TPS = ((2.0, 0.50), (5.0, 0.25), (10.0, 0.15))
TRAIL = ((100.0, 0.10), (25.0, 0.15), (10.0, 0.20), (5.0, 0.25), (2.0, 0.30), (1.2, 0.20))
FAST_LAG_MS = 15 * MIN
BANDS = ((10e3, 50e3), (50e3, 100e3), (100e3, 300e3), (300e3, 1e6), (1e6, 3e6))


class FrozenListMismatch(RuntimeError):
    """The frozen wallet file differs from the pre-registration."""


def load_members(directory: Path) -> list[str]:
    raw = (directory / MEMBERS_FILE).read_bytes()
    if hashlib.sha256(raw).hexdigest() != MEMBERS_SHA:
        raise FrozenListMismatch(f"{MEMBERS_FILE}: sha256 differs from the pre-registration")
    return sorted(json.loads(raw)["members"])


def eligible(token: str) -> bool:
    """A pump.fun mint. WSOL, USDC and USDT never end in ``pump``, so quote assets are out by construction."""
    return token.endswith("pump")


def trigger_id(token: str) -> str:
    return hashlib.sha256(f"{VERSION}|{token}".encode()).hexdigest()[:32]


class Ladder:
    """The live ladder over one quote at a time, in the order ``first_wallet.simulate`` applies it.

    A fold: the same quotes give the same state, so a restart rebuilds it from the ledger. Fills are
    at the quote read, which is never better than the level that was crossed."""

    def __init__(self, entry: float, entry_ms: int):
        self.entry, self.entry_ms = entry, entry_ms
        self.qty, self.realized, self.peak = 1.0, 0.0, entry
        self.floor, self.taken = STOP_FLOOR, 0
        self.ref, self.ref_ms = entry, entry_ms
        self.last = entry
        self.net: float | None = None
        self.reason: str | None = None

    def _close(self, reason: str, price: float) -> None:
        self.net = self.realized + self.qty * (price / self.entry - 1) * 100 - COST
        self.reason = reason

    def step(self, price: float, ms: int) -> None:
        if self.net is not None:
            return
        self.last = price
        if price <= self.entry * self.floor:
            self._close("stop" if self.taken == 0 else "breakeven", price)
            if self.taken == 0:
                self.net = min(self.net, STOP_NET)
            return
        trail = next((d for m, d in TRAIL if self.peak >= m * self.entry), None)
        if trail is not None and price <= self.peak * (1 - trail):
            self._close("trail", price)
            return
        while self.taken < len(TPS) and price >= TPS[self.taken][0] * self.entry:
            mult, share = TPS[self.taken]
            self.realized += share * (mult - 1) * 100
            self.qty -= share
            self.taken += 1
            self.floor = 1.0
        self.peak = max(self.peak, price)
        if abs(price / self.ref - 1) > STALE_BAND:
            self.ref, self.ref_ms = price, ms
        if ms - self.ref_ms >= STALE_MS:
            self._close("stale", price)
        elif ms - self.entry_ms >= MAX_HOLD_MS:
            self._close("max_hold", price)


class Evaluator:
    def __init__(self, ledger: Ledger, members: list[str], *,
                 fetch_new: Callable[[list[str], int], list[dict]],
                 read_price: Callable[[str], Read], now: Callable[[], int]):
        self.ledger, self.members = ledger, members
        self.fetch_new, self.read_price, self.now = fetch_new, read_price, now
        self.first_heartbeat: int | None = None
        self.known: set[str] = set()
        self.seen_rows: dict[str, int] = {}
        self.pending: dict[str, dict] = {}
        self.opened: dict[str, dict] = {}
        self.ladders: dict[str, Ladder] = {}
        self.last_read: dict[str, int] = {}
        self.no_venue: dict[str, bool] = {}
        self.last_poll = 0
        for rec in ledger.lines:
            self._fold(rec)

    def _fold(self, r: dict) -> None:
        """The state is a fold over the ledger: the same after a restart as in the running process."""
        kind = r["type"]
        if kind == "heartbeat_first":
            self.first_heartbeat = r["ts"]
        elif kind == "trigger":
            self.known.add(r["token"])
            if r["status"] == "pending":
                self.pending[r["id"]] = r
        elif kind in ("entry_unpriced", "unpriced_venue", "excluded_universe"):
            self.pending.pop(r["id"], None)
        elif kind == "entry":
            self.pending.pop(r["id"], None)
            self.opened[r["id"]] = r
            self.ladders[r["id"]] = Ladder(r["price_usd"], r["ts"])
            self.last_read[r["id"]] = r["ts"]
        elif kind == "obs":
            self.ladders[r["entry_id"]].step(r["price_usd"], r["ts"])
            self.last_read[r["entry_id"]] = r["ts"]
        elif kind in ("exit", "unresolved", "recovery_unknown"):
            self.pending.pop(r["id"], None)
            self.opened.pop(r["id"], None)
            self.ladders.pop(r["id"], None)
            self.last_read.pop(r["id"], None)

    def _write(self, rec: dict) -> None:
        if self.ledger.append(rec):
            self._fold(rec)

    def start(self) -> None:
        """Publish any torn line before anything else, then seed the 13-day memory from the tape."""
        now = self.now()
        for rec in self.ledger.recovered:
            tracked = set(self.pending) | set(self.opened)
            torn = rec.get("torn_id")
            ids = ([torn.split(":", 1)[0]] if torn.split(":", 1)[0] in tracked else []) if torn else sorted(tracked)
            for tid in ids:
                self._write({"type": "recovery_unknown", "id": tid, "ts": now, "torn_sha256": rec["torn_sha256"]})
            self._write({"type": "recovery", "id": rec["recovery_id"], "ts": now, "state": "unknown_interval",
                         "affected": ids, **{k: v for k, v in rec.items() if k != "recovery_id"}})
        for row in self.fetch_new(self.members, max(T0_MS, now) - LOOKBACK_MS):
            if row["side"] == "buy" and row["ts_ms"] < T0_MS:
                self.known.add(row["token"])
        if self.first_heartbeat is None:
            self._write({"type": "heartbeat_first", "id": "first", "ts": now, "version": VERSION, "t0_ms": T0_MS,
                         "members_sha": MEMBERS_SHA})
        else:
            self._write({"type": "restart", "id": f"restart:{now}", "ts": now})

    def poll(self) -> int:
        start = self.now()
        rows = self.fetch_new(self.members, max(T0_MS, start - POLL_WINDOW_MS))
        made = 0
        for row in sorted(rows, key=lambda r: (r["ts_ms"], r["tx"])):
            rid = f"{row['tx']}|{row['wallet']}|{row['token']}|{row['side']}"
            if rid in self.seen_rows:
                continue
            self.seen_rows[rid] = row["ts_ms"]
            token = row["token"]
            if row["side"] != "buy" or row["ts_ms"] < T0_MS or token in self.known or not eligible(token):
                continue
            seen = self.now()
            status = "too_late" if seen - row["ts_ms"] > MAX_LAG_MS else "pending"
            self._write({"type": "trigger", "id": trigger_id(token), "token": token, "wallet": row["wallet"],
                         "tx": row["tx"], "trigger_ts": row["ts_ms"], "seen_ms": seen, "lag_ms": seen - row["ts_ms"],
                         "source": row.get("source"), "status": status})
            made += 1
        cutoff = self.now() - 2 * POLL_WINDOW_MS
        self.seen_rows = {k: v for k, v in self.seen_rows.items() if v >= cutoff}
        self.last_poll = self.now()
        return made

    def _enter(self, tid: str, trig: dict) -> None:
        now = self.now()
        if now < trig["seen_ms"] + ENTRY_DELAY_MS:
            return
        if now > trig["seen_ms"] + ENTRY_WINDOW_MS:
            kind = "unpriced_venue" if self.no_venue.pop(tid, False) else "entry_unpriced"
            self._write({"type": kind, "id": tid, "token": trig["token"], "ts": now})
            return
        read = self.read_price(trig["token"])
        done = self.now()  # the clock AFTER the read: a slow RPC makes an entry later, never earlier
        if not read.ok:
            self.no_venue[tid] = read.no_venue
            return
        if done > trig["seen_ms"] + ENTRY_WINDOW_MS:
            return  # completed too late to count; the next pass records entry_unpriced
        self.no_venue.pop(tid, None)
        cap = read.price_usd * SUPPLY
        if not MIN_CAP <= cap <= MAX_CAP or read.liquidity_usd < MIN_LIQUIDITY_USD:
            self._write({"type": "excluded_universe", "id": tid, "token": trig["token"], "ts": done, "cap_usd": cap,
                         "liquidity_usd": read.liquidity_usd})
            return
        self._write({"type": "entry", "id": tid, "token": trig["token"], "trigger_ts": trig["trigger_ts"],
                     "seen_ms": trig["seen_ms"], "lag_ms": trig["lag_ms"], "ts": done, "cap_usd": cap,
                     "price_usd": read.price_usd, "liquidity_usd": read.liquidity_usd, "venue": read.venue,
                     "source": read.source})

    def _exit_if_done(self, tid: str) -> bool:
        lad = self.ladders[tid]
        if lad.net is None:
            return False
        self._write({"type": "exit", "id": tid, "token": self.opened[tid]["token"], "ts": self.now(),
                     "reason": lad.reason, "net": lad.net, "peak": lad.peak / lad.entry})
        return True

    def step(self) -> None:
        for tid, trig in sorted(self.pending.items(), key=lambda kv: kv[1]["seen_ms"]):
            self._enter(tid, trig)
        for tid, ent in sorted(self.opened.items()):
            if self._exit_if_done(tid) or self.now() - self.last_read[tid] < OBS_INTERVAL_MS:
                continue
            read = self.read_price(ent["token"])
            done = self.now()
            if not read.ok:
                if done - self.last_read[tid] >= NO_QUOTE_MS:
                    lad = self.ladders[tid]
                    self._write({"type": "unresolved", "id": tid, "token": ent["token"], "ts": done,
                                 "net": lad.realized + lad.qty * (lad.last / lad.entry - 1) * 100 - COST,
                                 "net_worst": lad.realized - lad.qty * 100 - COST})
                continue
            self._write({"type": "obs", "id": f"{tid}:{done}", "entry_id": tid, "ts": done,
                         "price_usd": read.price_usd, "liquidity_usd": read.liquidity_usd, "venue": read.venue})
            self._exit_if_done(tid)


def _settled(lines: list[dict]) -> list[dict]:
    entries = {r["id"]: r for r in lines if r["type"] == "entry"}
    out = []
    for r in lines:
        if r["type"] not in ("exit", "unresolved") or r["id"] not in entries:
            continue
        e = entries[r["id"]]
        out.append({"token": e["token"], "trigger_ts": e["trigger_ts"], "lag_ms": e["lag_ms"], "cap_usd": e["cap_usd"],
                    "net_base": r["net"] / 100, "net_flat": r["net"] / 100,
                    "net_worst": r.get("net_worst", r["net"]) / 100,
                    "reason": r.get("reason", "unresolved")})
    return out


def _mean(rows: list[dict]) -> dict:
    return {"n": len(rows), "mean_pct": round(st.fmean(s["net_base"] for s in rows) * 100, 2) if rows else None}


def build_report(lines: list[dict], now_ms: int) -> dict:
    first = next((r["ts"] for r in lines if r["type"] == "heartbeat_first"), None)
    trig = [r for r in lines if r["type"] == "trigger"]
    settled = _settled(lines)
    lost = sum(1 for r in lines if r["type"] == "recovery_unknown")
    counts = {s: sum(1 for t in trig if t["status"] == s) for s in ("pending", "too_late")}
    for kind in ("entry_unpriced", "unpriced_venue", "excluded_universe", "entry", "exit", "unresolved"):
        counts[kind] = sum(1 for r in lines if r["type"] == kind)
    lags = [t["lag_ms"] for t in trig]
    fast = [s for s in settled if s["lag_ms"] <= FAST_LAG_MS]
    slow = [s for s in settled if s["lag_ms"] > FAST_LAG_MS]
    return {"version": VERSION, "as_of_ms": now_ms, "t0_ms": T0_MS, "first_heartbeat_ms": first, "mode": "paper",
            "counts": counts, "lag_ms_median": st.median(lags) if lags else None,
            "F1": verdict(settled, first_heartbeat_ms=first, now_ms=now_ms, unknown=lost),
            "context": {"lag_15m_or_less": _mean(fast), "lag_over_15m": _mean(slow),
                        "bands": {f"${lo / 1000:.0f}k-{hi / 1000:.0f}k": _mean([s for s in settled if lo <= s["cap_usd"] < hi])
                                  for lo, hi in BANDS},
                        "reasons": {k: sum(1 for s in settled if s["reason"] == k)
                                    for k in sorted({s["reason"] for s in settled})}},
            "authority": {"financial_execution": False, "promotion": False}}


def run_service(*, poll_s: float = 30.0, tick_s: float = 5.0) -> int:
    import fcntl

    from kaiba.core import db as core_db
    from kaiba.core.config import get_settings

    settings = get_settings()
    directory = Path(settings.kaiba_data_dir) / "learning" / "tier"
    members = load_members(directory)
    lock = open(directory / "evaluator.lock", "w")  # noqa: SIM115 - held for the process lifetime
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    ledger = Ledger(directory / "ledger.jsonl")
    fetch_new, _ = box_fetchers(Path(settings.db_path))
    conn = core_db.connect(settings.db_path)
    ev = Evaluator(ledger, members, fetch_new=fetch_new, read_price=default_reader(conn),
                   now=lambda: int(time.time() * 1000))
    ev.start()
    stop: list[int] = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    last_report = 0.0
    while not stop:
        try:
            if time.time() * 1000 - ev.last_poll >= poll_s * 1000:
                ev.poll()
            ev.step()
            if time.time() - last_report >= 60:
                _atomic_json(directory / "report.json", build_report(ledger.lines, int(time.time() * 1000)))
                last_report = time.time()
        except (sqlite3.Error, ReadDeferred) as exc:  # a refused read is a skipped pass, never a crash loop
            log.warning("tier_forward: pass skipped: %s", exc)
        time.sleep(tick_s)
    _atomic_json(directory / "report.json", build_report(ledger.lines, int(time.time() * 1000)))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if argv[:1] == ["run"]:
        return run_service()
    if argv[:1] == ["report"]:
        from kaiba.core.config import get_settings

        directory = Path(get_settings().kaiba_data_dir) / "learning" / "tier"
        ledger = Ledger(directory / "ledger.jsonl", readonly=True)  # never recovers, truncates or writes
        rep: Any = build_report(ledger.lines, int(time.time() * 1000))
        print(json.dumps({k: rep[k] for k in ("counts", "lag_ms_median", "F1", "context")}, sort_keys=True))
        return 0
    print("usage: python -m kaiba.learning.tier_forward run|report")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
