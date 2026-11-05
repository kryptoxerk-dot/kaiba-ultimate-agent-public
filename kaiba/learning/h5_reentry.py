"""H5 slower-wallet re-entry: the forward PAPER evaluator (H5-REENTRY-IMPL-CLAUDE-20261103).

Pre-registered in docs/research/claude-h5-reentry-prereg-20261103.md (9d3e97f, T0 2026-11-03
15:51:08 UTC) and amended before any outcome in claude-h5-reentry-amendment-20261103.md (A1-A11).
Every rule below is that text; a change is a new registration, never an edit.

What it does, every few seconds:

* reads the ``swaps`` tape (read-only, indexed by wallet) for the 329 frozen members: the 59 slow
  members (H5, B1) and the 270 fast ones (B2);
* stamps ``seen_ms`` on every new buy it sees and decides, from the member's own rows on that
  token in the 13 days before the buy, whether it is an observed buy-sell-rebuy (H5, B2) or a first
  buy (B1). A sell may be partial: this is an observed cycle, not a proven exit;
* prices the entry 30 s or more after ``seen_ms`` and the exit path with the readers the engine's
  paper entry uses on sol (PumpSwap pool, then pump.fun curve), at ``Priority.RESEARCH`` so the
  shared limiter refuses this job before protection. A token neither reader can price is counted
  ``unpriced_venue``; nothing is ever filled from DexScreener, a GMGN mark or a guess;
* appends one fsync'd JSON line per event to its own ledger and rebuilds from it on restart.

It never writes positions, orders or decisions, never touches risk.yaml, and has no order path.
Paper only: the result says whether delayed following of slower re-entries has room, nothing else.

    python -m kaiba.learning.h5_reentry run        # the service loop
    python -m kaiba.learning.h5_reentry report     # rebuild the report from the ledger
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import random
import signal
import sqlite3
import statistics as st
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

VERSION = "h5-reentry/1"
T0_MS = 1_793_721_068_000                       # 2026-11-03T15:51:08Z, the prereg commit
COHORT_ID = "runners:sol:1793713163992"
SLOW_FILE, SLOW_SHA = "slow-members-20261103.json", "c806b29dda54c5517493e2e2a5437c3163fbe20da0f65b0a9edf414b66eff0ae"
FAST_FILE, FAST_SHA = "fast-members-20261103.json", "eb890dce59310edd86cfe3256a95e72227f711ab476d0b0b4be7fea57b396ba8"
MIN = 60_000
DAY = 86_400_000
GAP_MS = 10 * MIN                 # re-buy at least this long after the member's last observed sell
LOOKBACK_MS = 13 * DAY            # A3: the decision reads only the 13 days before the buy
ENTRY_DELAY_MS = 30_000           # entry >= 30 s after seen_ms
ENTRY_WINDOW_MS = 10 * MIN        # A5: no price by seen + 10 min -> entry_unpriced
OBS_INTERVAL_MS = 30_000          # A7
GAP_REPORT_MS = 60_000
MAX_HOLD_MS = 60 * MIN
UNRESOLVED_MS = 70 * MIN
TP, SL = 0.50, -0.30
SIZE_SOL, ALT_SIZE_SOL = 0.1, 0.3
LIQ_MIN_SOL = 100.0
POLL_WINDOW_MS = 30 * MIN         # rows can land late; re-read this window, dedup by row id
WAL_LIMIT = 1_500_000_000
VENUE_FEE_BPS = {"pumpswap": 120, "pumpfun-curve": 125}
ROUTER_BPS = 100
TIP_SOL = 0.001
FLAT_BPS = 325
MIN_N, MIN_HALF, BOOT, SEED = 40, 15, 2000, 20261103
INCONCLUSIVE_DAYS = 42
ARMS = ("H5", "B1", "B2")
#: A12 (before install): kaiba.core.schemas.QUOTE_ASSETS[sol], copied so this module has no core import.
SOL_QUOTE_ASSETS = frozenset({"So11111111111111111111111111111111111111112",
                              "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                              "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"})


class ReadDeferred(RuntimeError):
    """A tape read that did not happen (WAL over the limit, or a database error). Never 'no rows'."""


class FrozenInputMismatch(RuntimeError):
    """A frozen selection file or registration constant differs from the pre-registration."""


# --------------------------------------------------------------------------------------
# frozen inputs
# --------------------------------------------------------------------------------------

def load_members(directory: Path) -> tuple[frozenset[str], frozenset[str]]:
    """(slow, fast) from the two frozen files, refusing any byte difference."""
    out = []
    for name, sha in ((SLOW_FILE, SLOW_SHA), (FAST_FILE, FAST_SHA)):
        raw = (directory / name).read_bytes()
        if hashlib.sha256(raw).hexdigest() != sha:
            raise FrozenInputMismatch(f"{name}: sha256 differs from the pre-registration")
        doc = json.loads(raw)
        if doc.get("cohort_id") != COHORT_ID:
            raise FrozenInputMismatch(f"{name}: cohort_id differs")
        out.append(frozenset(doc["members"]))
    slow, fast = out
    if slow & fast:
        raise FrozenInputMismatch("slow and fast sets overlap")
    return slow, fast


# --------------------------------------------------------------------------------------
# the trigger rule (pure)
# --------------------------------------------------------------------------------------

def classify_buy(history: Iterable[tuple[str, int]], buy_ts: int) -> str | None:
    """``"first"``, ``"reentry"`` or None for a buy at ``buy_ts``, from the same member's rows on
    the same token, ``(side, ts_ms)``, strictly before it and within :data:`LOOKBACK_MS`.

    Walks the window in order: a buy after an observed sell starts a new holding and consumes the
    sell, whatever the gap, so the next trigger needs a new sell first."""
    seen_buy = False
    last_sell: int | None = None
    window = [(s, t) for s, t in history if buy_ts - LOOKBACK_MS <= t < buy_ts]
    for side, ts in sorted(window, key=lambda x: (x[1], x[0] != "buy")):
        if side == "buy":
            seen_buy = True
            last_sell = None
        elif side == "sell" and seen_buy:
            last_sell = ts
    if not seen_buy:
        return "first"
    if last_sell is not None and buy_ts - last_sell >= GAP_MS:
        return "reentry"
    return None


def arm_for(kind: str | None, wallet: str, slow: frozenset[str], fast: frozenset[str]) -> str | None:
    if kind == "reentry":
        return "H5" if wallet in slow else ("B2" if wallet in fast else None)
    if kind == "first" and wallet in slow:
        return "B1"
    return None


def trigger_id(arm: str, wallet: str, token: str, tx: str) -> str:
    return hashlib.sha256(f"{VERSION}|{arm}|{wallet}|{token}|{tx}".encode()).hexdigest()[:32]


# --------------------------------------------------------------------------------------
# costs and verdicts (pure)
# --------------------------------------------------------------------------------------

def leg_cost(model: str, venue: str, liquidity_usd: float, size_usd: float, size_sol: float) -> float:
    """Fraction of notional one leg costs under A8."""
    impact = size_usd / (liquidity_usd / 2) if liquidity_usd > 0 else 1.0
    if model == "flat":
        return FLAT_BPS / 1e4 + impact
    return (VENUE_FEE_BPS.get(venue, max(VENUE_FEE_BPS.values())) + ROUTER_BPS) / 1e4 + TIP_SOL / size_sol + impact


def net_return(entry: dict, exit_: dict, model: str, size_sol: float = SIZE_SOL) -> float:
    gross = exit_["price_usd"] / entry["price_usd"] - 1
    c_in = leg_cost(model, entry["venue"], entry["liquidity_usd"], size_sol * entry["sol_usd"], size_sol)
    c_out = leg_cost(model, exit_["venue"], exit_["liquidity_usd"], size_sol * exit_["sol_usd"], size_sol)
    return (1 - c_in) * (1 + gross) * (1 - c_out) - 1


def exit_decision(entry_price: float, entry_ms: int, obs_price: float, obs_ms: int) -> str | None:
    r = obs_price / entry_price - 1
    if r >= TP:
        return "take_profit"
    if r <= SL:
        return "stop"
    if obs_ms - entry_ms >= MAX_HOLD_MS:
        return "time"
    return None


def token_lb90(rows: list[tuple[str, float]]) -> float | None:
    """10th percentile of the bootstrap mean, resampling TOKENS."""
    by: dict[str, list[float]] = {}
    for token, r in rows:
        by.setdefault(token, []).append(r)
    if not by:
        return None
    names = sorted(by)
    rng = random.Random(SEED)
    means = []
    for _ in range(BOOT):
        xs = [x for n in (rng.choice(names) for _ in names) for x in by[n]]
        means.append(math.fsum(xs) / len(xs))
    means.sort()
    return means[int(0.10 * BOOT)]


def verdict(settled: list[dict], *, first_heartbeat_ms: int | None, now_ms: int, unknown: int = 0) -> dict:
    """The batch pass line. ``unknown`` counts exposures whose outcome a ledger recovery lost
    (``recovery_unknown``): they count toward n, sit in the worst case at -100%, and can never
    be silently dropped -- a result that passes only on the known exposures is INCONCLUSIVE."""
    n = len(settled) + unknown
    out: dict[str, Any] = {"n": n, "n_known": len(settled), "n_recovery_unknown": unknown, "min_n": MIN_N}
    if n < MIN_N:
        late = first_heartbeat_ms is not None and now_ms - first_heartbeat_ms > INCONCLUSIVE_DAYS * DAY
        out["verdict"] = "INCONCLUSIVE" if late else "PENDING"
        return out
    out["worst_case"] = math.fsum([s["net_worst"] for s in settled] + [-1.0] * unknown) / n
    if not settled:
        out["verdict"] = "INCONCLUSIVE"
        out["reason"] = "every exposure's outcome is recovery_unknown"
        return out
    ordered = sorted(settled, key=lambda s: s["trigger_ts"])
    half = len(ordered) // 2
    h1, h2 = ordered[:half], ordered[half:]
    out.update(mean_base=st.fmean(s["net_base"] for s in ordered), mean_flat=st.fmean(s["net_flat"] for s in ordered),
               first_half=st.fmean(s["net_base"] for s in h1) if h1 else None,
               second_half=st.fmean(s["net_base"] for s in h2),
               worst_case_known=st.fmean(s["net_worst"] for s in ordered),
               lb90=token_lb90([(s["token"], s["net_base"]) for s in ordered]))
    known_passes = (len(ordered) >= MIN_N and len(h1) >= MIN_HALF and len(h2) >= MIN_HALF
                    and (out["first_half"] or 0) > 0 and out["second_half"] > 0
                    and (out["lb90"] or 0) > 0 and out["worst_case_known"] > 0)
    if not known_passes:
        out["verdict"] = "FAIL" if len(ordered) >= MIN_N else "INCONCLUSIVE"
        if out["verdict"] == "INCONCLUSIVE":
            out["reason"] = "too few known outcomes once recovery_unknown exposures are set aside"
    elif unknown:
        out["verdict"] = "INCONCLUSIVE"
        out["reason"] = "passes only on known exposures; recovery_unknown exposures cannot be resolved"
    else:
        out["verdict"] = "PASS" if out["worst_case"] > 0 else "FAIL"
    if out["verdict"] == "PASS" and out["mean_flat"] <= 0:
        out["label"] = "cost-sensitive"
    return out


# --------------------------------------------------------------------------------------
# the ledger
# --------------------------------------------------------------------------------------

class LedgerCorrupt(RuntimeError):
    """A complete (newline-terminated) line does not parse: not a torn write, so refuse to run."""


def _fsync_write(path: Path, data: bytes) -> None:
    with open(path, "xb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    if hasattr(os, "O_DIRECTORY"):  # make the new name itself durable (POSIX)
        fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class Ledger:
    """Append-only JSON lines, fsync'd; the whole state is a fold over it.

    A hard kill can tear only the LAST line (each record is one write ending in a newline). In
    writer mode such a tail is recovered in a crash-safe order:

    1. the torn bytes go, byte for byte, to a ``<ledger>.torn-<ms>`` sidecar (fsync'd);
    2. a ``<ledger>.recovery-<sha16>-<offset>.json`` INTENT with their provenance is written
       (fsync'd) -- reused if an identical intent already exists;
    3. only then is the ledger truncated back to its valid prefix.

    Every load lists the intents whose ``recovery`` record is not in the ledger yet in
    :attr:`recovered`, so a death between any two steps (or before the evaluator publishes) leaves
    an intent the next start reconciles. ``readonly=True`` never writes anything: a torn tail is
    only reported, so a reader can never truncate under an active writer. A bad line anywhere else
    refuses."""

    def __init__(self, path: Path, *, now_ms: Callable[[], int] = lambda: int(time.time() * 1000),
                 readonly: bool = False):
        self.path = path
        self.ids: set[tuple[str, str]] = set()
        self.lines: list[dict] = []
        self.recovered: list[dict] = []
        self.torn_tail_present = False
        if not path.exists():
            return
        raw = path.read_bytes()
        cut = raw.rfind(b"\n") + 1  # end of the last complete line
        body, tail = raw[:cut], raw[cut:]
        for n, line in enumerate(body.split(b"\n")[:-1], 1):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError as exc:
                raise LedgerCorrupt(f"line {n} of {path.name} does not parse (not a torn tail)") from exc
            self.lines.append(rec)
            self.ids.add((rec["type"], rec["id"]))
        if tail.strip():
            self.torn_tail_present = True
            if not readonly:
                self._recover_tail(tail, cut, now_ms())
        for intent_path in sorted(path.parent.glob(f"{path.name}.recovery-*.json")):
            intent = json.loads(intent_path.read_bytes())
            if not self.has("recovery", intent["recovery_id"]):
                self.recovered.append(intent)

    def _recover_tail(self, tail: bytes, cut: int, when: int) -> None:
        import re

        sha = hashlib.sha256(tail).hexdigest()
        intent_path = self.path.with_name(f"{self.path.name}.recovery-{sha[:16]}-{cut}.json")
        if not intent_path.exists():
            sidecar = self.path.with_name(f"{self.path.name}.torn-{when}")
            _fsync_write(sidecar, tail)
            m = re.search(rb'"entry_id": "([^"]+)"', tail) or re.search(rb'"id": "([^"]+)"', tail)
            intent = {"recovery_id": f"recovery:{sha[:16]}:{cut}", "torn_bytes": len(tail), "torn_sha256": sha,
                      "torn_offset": cut, "sidecar": sidecar.name, "valid_lines": len(self.lines),
                      "last_valid_ts": self.lines[-1].get("ts") if self.lines else None,
                      "torn_id": m.group(1).decode() if m else None, "found_ms": when}
            _fsync_write(intent_path, json.dumps(intent, sort_keys=True).encode())
        with self.path.open("r+b") as f:
            f.truncate(cut)
            f.flush()
            os.fsync(f.fileno())

    def has(self, kind: str, ident: str) -> bool:
        return (kind, ident) in self.ids

    def append(self, rec: dict) -> bool:
        key = (rec["type"], rec["id"])
        if key in self.ids:
            return False
        line = json.dumps(rec, sort_keys=True, allow_nan=False) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
        self.ids.add(key)
        self.lines.append(rec)
        return True


# --------------------------------------------------------------------------------------
# price reading (the engine's sol paper-entry readers, at RESEARCH priority)
# --------------------------------------------------------------------------------------

@dataclass
class Read:
    ok: bool
    price_usd: float = 0.0
    liquidity_usd: float = 0.0
    sol_usd: float = 0.0
    venue: str = ""
    source: str = ""
    no_venue: bool = False
    note: str = ""


NO_VENUE_NOTES = ("no pumpswap pool", "no live pump.fun curve")


def default_reader(conn: Any) -> Callable[[str], Read]:
    from kaiba.core.limiter import Priority
    from kaiba.core.schemas import Chain
    from kaiba.execution.pumpswap_price import PumpCurveSource, PumpSwapPoolSource
    from kaiba.providers import native_price

    pool = PumpSwapPoolSource(conn, priority=Priority.RESEARCH)
    curve = PumpCurveSource(conn, priority=Priority.RESEARCH)

    def read(token: str) -> Read:
        notes = []
        for source in (pool, curve):
            q = source.quote(Chain.SOL, token)
            if getattr(q, "price_usd", None) and q.price_usd > 0 and q.liquidity_usd:
                sample = native_price.latest(Chain.SOL, conn)
                sol = float(sample.price_usd) if sample else 0.0
                if sol <= 0:
                    return Read(False, note="no SOL/USD")
                return Read(True, float(q.price_usd), float(q.liquidity_usd), sol, str(q.venue or ""),
                            str(q.source or ""))
            notes.append(str(getattr(q, "note", "") or ""))
        no_venue = len(notes) == 2 and all(any(n.startswith(p) for p in NO_VENUE_NOTES) for n in notes)
        return Read(False, no_venue=no_venue, note="; ".join(notes)[:200])

    return read


# --------------------------------------------------------------------------------------
# the evaluator
# --------------------------------------------------------------------------------------

class Evaluator:
    def __init__(self, ledger: Ledger, slow: frozenset[str], fast: frozenset[str], *,
                 fetch_new: Callable[[list[str], int], list[dict]],
                 fetch_history: Callable[[str, str, int, int], list[tuple[str, int]]],
                 read_price: Callable[[str], Read], now: Callable[[], int]):
        self.ledger, self.slow, self.fast = ledger, slow, fast
        self.members = sorted(slow | fast)
        self.fetch_new, self.fetch_history, self.read_price, self.now = fetch_new, fetch_history, read_price, now
        self.seen_rows: dict[str, int] = {}
        self.first_heartbeat: int | None = None
        self.last_poll = 0
        self.pending: dict[str, dict] = {}
        self.opened: dict[str, dict] = {}
        self.last_obs: dict[str, dict] = {}
        self.no_venue: dict[str, bool] = {}
        self.deferred_since: int | None = None
        for rec in ledger.lines:
            self._fold(rec)

    def _fold(self, r: dict) -> None:
        """The state is a fold over the ledger: the same after a restart as in the running process."""
        kind = r["type"]
        if kind == "heartbeat_first":
            self.first_heartbeat = r["ts"]
        elif kind == "trigger" and r["status"] == "pending":
            self.pending[r["id"]] = r
        elif kind in ("entry_unpriced", "unpriced_venue", "excluded_universe"):
            self.pending.pop(r["id"], None)
        elif kind == "entry":
            self.pending.pop(r["id"], None)
            self.opened[r["id"]] = r
        elif kind == "obs":
            self.last_obs[r["entry_id"]] = r
        elif kind in ("exit", "unresolved", "recovery_unknown"):
            self.pending.pop(r["id"], None)
            self.opened.pop(r["id"], None)
            self.last_obs.pop(r["id"], None)

    def _write(self, rec: dict) -> None:
        if self.ledger.append(rec):
            self._fold(rec)

    def start(self) -> None:
        now = self.now()
        for rec in self.ledger.recovered:
            # A torn last line: its event is unknown. Publish that BEFORE anything else. The affected
            # exposure leaves scoring as recovery_unknown (it stays in the worst case at -100%): the
            # trigger the line names, or every pending trigger and open entry when none can be read.
            # A readable id that names no tracked exposure (a heartbeat, a restart) affects none.
            tracked = set(self.pending) | set(self.opened)
            torn = rec.get("torn_id")
            if torn:
                base = torn.split(":", 1)[0]
                ids = [base] if base in tracked else []
            else:
                ids = sorted(tracked)
            for tid in ids:
                self._write({"type": "recovery_unknown", "id": tid, "ts": now, "torn_sha256": rec["torn_sha256"],
                             "was": "entered" if tid in self.opened else "pending"})
            self._write({"type": "recovery", "id": rec["recovery_id"], "ts": now, "state": "unknown_interval",
                         "affected": ids, **{k: v for k, v in rec.items() if k != "recovery_id"}})
        if self.first_heartbeat is None:
            self._write({"type": "heartbeat_first", "id": "first", "ts": now, "version": VERSION, "t0_ms": T0_MS,
                         "cohort_id": COHORT_ID, "slow_sha": SLOW_SHA, "fast_sha": FAST_SHA})
        else:
            last = self.ledger.lines[-1].get("ts") if self.ledger.lines else None
            self._write({"type": "restart", "id": f"restart:{now}", "ts": now, "previous_ledger_ts": last})

    def _deferred(self, why: str) -> None:
        """Edge-triggered: one ledger line when reads start being refused, one when they resume."""
        if self.deferred_since is None:
            self.deferred_since = self.now()
            self._write({"type": "read_deferred_begin", "id": f"defer:{self.deferred_since}",
                         "ts": self.deferred_since, "why": why})

    def _admitted(self) -> None:
        if self.deferred_since is not None:
            now = self.now()
            self._write({"type": "read_deferred_end", "id": f"defer:{self.deferred_since}", "ts": now,
                         "since": self.deferred_since})
            self.deferred_since = None

    # ---- one pass
    def poll(self) -> int:
        start = self.now()
        try:
            rows = self.fetch_new(self.members, max(T0_MS, start - POLL_WINDOW_MS))
        except ReadDeferred as exc:
            self._deferred(str(exc))
            return 0
        self._admitted()
        held = {(r["arm"], r["token"]) for r in list(self.pending.values()) + list(self.opened.values())}
        made = 0
        for row in sorted(rows, key=lambda r: (r["ts_ms"], r["tx"])):
            rid = f"{row['tx']}|{row['wallet']}|{row['token']}|{row['side']}"
            if rid in self.seen_rows:
                continue
            if row["side"] != "buy" or row["ts_ms"] < T0_MS or row["token"] in SOL_QUOTE_ASSETS:
                self.seen_rows[rid] = row["ts_ms"]
                continue  # A12: the quote asset (WSOL/USDC/USDT) is never a token to follow
            try:
                hist = self.fetch_history(row["wallet"], row["token"], row["ts_ms"] - LOOKBACK_MS, row["ts_ms"])
            except ReadDeferred as exc:
                # Unknown history is never "no history": leave the row unseen and stop this pass;
                # the next pass reads it again (its seen_ms will be honestly later).
                self._deferred(str(exc))
                break
            self.seen_rows[rid] = row["ts_ms"]
            # The decision exists only once the history read has COMPLETED: that is the observation.
            seen = self.now()
            arm = arm_for(classify_buy(hist, row["ts_ms"]), row["wallet"], self.slow, self.fast)
            if arm is None:
                continue
            tid = trigger_id(arm, row["wallet"], row["token"], row["tx"])
            if self.ledger.has("trigger", tid):
                continue
            rec = {"type": "trigger", "id": tid, "arm": arm, "wallet": row["wallet"], "token": row["token"],
                   "tx": row["tx"], "trigger_ts": row["ts_ms"], "seen_ms": seen, "source": row.get("source"),
                   "cycle": "observed_buy_sell_rebuy" if arm != "B1" else "observed_first_buy"}
            if self.first_heartbeat is None or row["ts_ms"] < self.first_heartbeat:
                rec["status"] = "pre_evaluator"
            elif (arm, row["token"]) in held:
                rec["status"] = "overlap_skipped"
            else:
                rec["status"] = "pending"
                held.add((arm, row["token"]))
            self._write(rec)
            made += 1
        cutoff = self.now() - 2 * POLL_WINDOW_MS
        self.seen_rows = {k: v for k, v in self.seen_rows.items() if v >= cutoff}
        self.last_poll = self.now()
        return made

    def step(self) -> None:
        """Entries due, then observations of open entries. Every recorded time is the clock read
        AFTER the price read completed: a slow RPC can only make an entry later, never earlier."""
        for tid, trig in sorted(self.pending.items(), key=lambda kv: kv[1]["seen_ms"]):
            if self.now() < trig["seen_ms"] + ENTRY_DELAY_MS:
                continue
            if self.now() > trig["seen_ms"] + ENTRY_WINDOW_MS:
                kind = "unpriced_venue" if self.no_venue.pop(tid, False) else "entry_unpriced"
                self._write({"type": kind, "id": tid, "arm": trig["arm"], "token": trig["token"], "ts": self.now()})
                continue
            read = self.read_price(trig["token"])
            done = self.now()
            if not read.ok:
                self.no_venue[tid] = read.no_venue
                continue
            if done > trig["seen_ms"] + ENTRY_WINDOW_MS:
                continue  # completed too late to count; the next pass records entry_unpriced
            self.no_venue.pop(tid, None)
            liq_sol = read.liquidity_usd / read.sol_usd
            if liq_sol < LIQ_MIN_SOL:
                self._write({"type": "excluded_universe", "id": tid, "arm": trig["arm"], "token": trig["token"],
                             "ts": done, "liquidity_sol": liq_sol})
                continue
            self._write({"type": "entry", "id": tid, "arm": trig["arm"], "token": trig["token"],
                         "trigger_ts": trig["trigger_ts"], "seen_ms": trig["seen_ms"], "ts": done,
                         "delay_ms": done - trig["seen_ms"], "price_usd": read.price_usd,
                         "liquidity_usd": read.liquidity_usd, "sol_usd": read.sol_usd, "venue": read.venue,
                         "source": read.source})
        for tid, ent in sorted(self.opened.items()):
            last = self.last_obs.get(tid) or ent
            if self.now() - last["ts"] < OBS_INTERVAL_MS:
                continue
            read = self.read_price(ent["token"])
            done = self.now()
            if not read.ok:
                if done - ent["ts"] >= UNRESOLVED_MS:
                    self._write({"type": "unresolved", "id": tid, "arm": ent["arm"], "token": ent["token"],
                                 "ts": done, "last": {k: last[k] for k in ("price_usd", "liquidity_usd",
                                                                           "sol_usd", "venue")}})
                continue
            self._write({"type": "obs", "id": f"{tid}:{done}", "entry_id": tid, "ts": done,
                         "price_usd": read.price_usd, "liquidity_usd": read.liquidity_usd,
                         "sol_usd": read.sol_usd, "venue": read.venue, "gap_ms": done - last["ts"]})
            why = exit_decision(ent["price_usd"], ent["ts"], read.price_usd, done)
            if why:
                self._write({"type": "exit", "id": tid, "arm": ent["arm"], "token": ent["token"], "ts": done,
                             "reason": why, "price_usd": read.price_usd, "liquidity_usd": read.liquidity_usd,
                             "sol_usd": read.sol_usd, "venue": read.venue})


# --------------------------------------------------------------------------------------
# the report (a pure fold over the ledger)
# --------------------------------------------------------------------------------------

def build_report(lines: list[dict], now_ms: int) -> dict:
    first = next((r["ts"] for r in lines if r["type"] == "heartbeat_first"), None)
    triggers = {r["id"]: r for r in lines if r["type"] == "trigger"}
    entries = {r["id"]: r for r in lines if r["type"] == "entry"}
    exits = {r["id"]: r for r in lines if r["type"] == "exit"}
    unresolved = {r["id"]: r for r in lines if r["type"] == "unresolved"}
    lost = {r["id"] for r in lines if r["type"] == "recovery_unknown"}
    gaps = [r["gap_ms"] for r in lines if r["type"] == "obs"]
    arms = {}
    for arm in ARMS:
        trig = [t for t in triggers.values() if t["arm"] == arm]
        # Exposures a recovery could not resolve: an entry with no exit, or a pending trigger whose
        # entry line may be the one that was torn. Both stay in n and the worst case.
        unknown_entered = sum(1 for tid, e in entries.items() if e["arm"] == arm and tid in lost
                              and tid not in exits and tid not in unresolved)
        unknown_pending = sum(1 for tid in lost if tid in triggers and triggers[tid]["arm"] == arm
                              and tid not in entries)
        settled = []
        for tid, ent in entries.items():
            if ent["arm"] != arm:
                continue
            if tid in exits:
                ex = exits[tid]
                worst_exit = ex
            elif tid in unresolved:
                ex = {**unresolved[tid]["last"]}
                worst_exit = {**ex, "price_usd": 1e-18}
            else:
                continue
            settled.append({"token": ent["token"], "trigger_ts": ent["trigger_ts"],
                            "net_base": net_return(ent, ex, "base"), "net_flat": net_return(ent, ex, "flat"),
                            "net_base_0_3": net_return(ent, ex, "base", ALT_SIZE_SOL),
                            "net_worst": net_return(ent, worst_exit, "base"),
                            "reason": exits[tid]["reason"] if tid in exits else "unresolved"})
        counts = {s: sum(1 for t in trig if t["status"] == s) for s in ("pending", "pre_evaluator", "overlap_skipped")}
        for kind in ("entry_unpriced", "unpriced_venue", "excluded_universe"):
            counts[kind] = sum(1 for r in lines if r["type"] == kind and r.get("arm") == arm)
        delays = [e["delay_ms"] for e in entries.values() if e["arm"] == arm]
        arms[arm] = {"triggers": len(trig), "counts": counts,
                     "entered": sum(1 for e in entries.values() if e["arm"] == arm),
                     "settled": len(settled), "unresolved": sum(1 for s in settled if s["reason"] == "unresolved"),
                     "entry_delay_ms_median": st.median(delays) if delays else None,
                     "mean_base_0_3_sol": st.fmean(s["net_base_0_3"] for s in settled) if settled else None,
                     "recovery_unknown": {"entered": unknown_entered, "pending": unknown_pending},
                     "verdict": verdict(settled, first_heartbeat_ms=first, now_ms=now_ms,
                                        unknown=unknown_entered + unknown_pending)}
    return {"version": VERSION, "as_of_ms": now_ms, "t0_ms": T0_MS, "first_heartbeat_ms": first,
            "mode": "paper", "arms": arms,
            "observation_gaps": {"n": len(gaps), "over_60s": sum(1 for g in gaps if g > GAP_REPORT_MS),
                                 "max_ms": max(gaps) if gaps else None},
            "unknown_intervals": {
                "recoveries": [{k: r[k] for k in ("ts", "torn_sha256", "torn_bytes", "sidecar", "last_valid_ts")}
                               for r in lines if r["type"] == "recovery"],
                "recovery_unknown_ids": sum(1 for r in lines if r["type"] == "recovery_unknown"),
                "read_deferrals": sum(1 for r in lines if r["type"] == "read_deferred_begin"),
                "restarts": sum(1 for r in lines if r["type"] == "restart")},
            "scope": ["pump.fun-venue re-entries only (PumpSwap pool or pump.fun curve); other venues are "
                      "counted unpriced_venue, never filled",
                      "observed buy-sell-rebuy cycles on partial tape coverage: a sell may be partial and "
                      "unobserved trades may exist; no claim that a member fully exited",
                      "pass line judges H5 only; B1 and B2 are baselines, reported"],
            "authority": {"financial_execution": False, "promotion": False}}


# --------------------------------------------------------------------------------------
# box wiring
# --------------------------------------------------------------------------------------

def _ro(db: Path, secs: float = 20.0) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    c.execute("PRAGMA query_only=1")
    deadline = time.time() + secs
    c.set_progress_handler(lambda: 1 if time.time() > deadline else 0, 10_000)
    return c


def admit(db: Path) -> None:
    """AGENTS.md read rule 2, before EVERY connection and every chunk: a WAL over the limit refuses."""
    wal = Path(f"{db}-wal")
    if wal.exists() and wal.stat().st_size > WAL_LIMIT:
        raise ReadDeferred("wal_over_limit")


def box_fetchers(db: Path, *, connect: Callable[[Path], sqlite3.Connection] = _ro) -> tuple[Callable, Callable]:
    """Read-only tape readers. Any refusal or database error is :class:`ReadDeferred`, never an
    empty answer: an empty history would read as a first buy."""

    def fetch_new(members: list[str], since_ms: int) -> list[dict]:
        out = []
        for i in range(0, len(members), 200):
            admit(db)
            part = members[i:i + 200]
            marks = ",".join("?" * len(part))
            c = connect(db)
            try:
                for w, tok, side, ts, tx, src in c.execute(
                        f"SELECT wallet, token, side, ts_ms, tx, source FROM swaps WHERE chain='sol' "
                        f"AND wallet IN ({marks}) AND ts_ms >= ?", (*part, since_ms)):
                    out.append({"wallet": w, "token": tok, "side": side, "ts_ms": int(ts), "tx": tx, "source": src})
            except sqlite3.Error as exc:
                raise ReadDeferred(f"tape_read_failed:{type(exc).__name__}") from exc
            finally:
                c.close()
        return out

    def fetch_history(wallet: str, token: str, start_ms: int, end_ms: int) -> list[tuple[str, int]]:
        admit(db)
        c = connect(db)
        try:
            return [(s, int(t)) for s, t in c.execute(
                "SELECT side, ts_ms FROM swaps WHERE chain='sol' AND wallet=? AND ts_ms >= ? AND ts_ms < ? "
                "AND token=?", (wallet, start_ms, end_ms, token))]
        except sqlite3.Error as exc:
            raise ReadDeferred(f"history_read_failed:{type(exc).__name__}") from exc
        finally:
            c.close()

    return fetch_new, fetch_history


def _atomic_json(path: Path, obj: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, sort_keys=True, allow_nan=False), encoding="utf-8")
    os.replace(tmp, path)


def run_service(*, poll_s: float = 30.0, tick_s: float = 5.0) -> int:
    import fcntl

    from kaiba.core import db as core_db
    from kaiba.core.config import get_settings

    settings = get_settings()
    directory = Path(settings.kaiba_data_dir) / "learning" / "h5"
    slow, fast = load_members(directory)
    lock = open(directory / "evaluator.lock", "w")  # noqa: SIM115 - held for the process lifetime
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    ledger = Ledger(directory / "ledger.jsonl")
    fetch_new, fetch_history = box_fetchers(Path(settings.db_path))
    conn = core_db.connect(settings.db_path)
    ev = Evaluator(ledger, slow, fast, fetch_new=fetch_new, fetch_history=fetch_history,
                   read_price=default_reader(conn), now=lambda: int(time.time() * 1000))
    ev.start()
    stop = []
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
        except sqlite3.Error as exc:  # a busy or slow read is a skipped pass, never a crash loop
            log.warning("h5_reentry: database read skipped: %s", exc)
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

        directory = Path(get_settings().kaiba_data_dir) / "learning" / "h5"
        # Read-only: a report never recovers, truncates or writes, so it cannot race the writer.
        ledger = Ledger(directory / "ledger.jsonl", readonly=True)
        rep = build_report(ledger.lines, int(time.time() * 1000))
        print(json.dumps({"arms": {a: {"triggers": v["triggers"], "entered": v["entered"], "settled": v["settled"],
                                       "verdict": v["verdict"]["verdict"]} for a, v in rep["arms"].items()},
                          "torn_tail_present": ledger.torn_tail_present,
                          "unapplied_recoveries": len(ledger.recovered)}, sort_keys=True))
        return 0
    print("usage: python -m kaiba.learning.h5_reentry run|report")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
