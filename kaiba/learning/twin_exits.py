"""Twin exits: each closed paper position against itself with ONE extra early exit.

Why (2026-10-16). Codex's autolearn study declared three exit hypotheses -- hold 60 s, hold
5 min, sell everything at +25% -- against the incumbent exits, and counted 0 comparisons in
its first 14.5 h (docs/research/claude-autolearn-review-20261016.md). The production paper
lanes close ~215 positions a day under the incumbent exits, and protection prices each one
about every 12 s; :mod:`kaiba.learning.shadow_tape` now keeps that price path.

A challenger here is the incumbent's own exits plus ONE extra early exit. Adding an exit can
only make an exit happen earlier, so every challenger exit falls while the incumbent position
is still open and still being priced: the price it needs is always on the tape or the pair is
dropped, never guessed. Until its extra exit fires the challenger IS the incumbent (same entry,
same partial sells), so the comparison is paired.

Declared before any result (2026-10-16):

* population: closed SHADOW positions of every lane, settled (closed >= 10 min ago), outcome
  observed (not :func:`kaiba.learning.fabricated.is_fabricated_outcome`), with tape prints;
* entry reference = the first tape print at or after the position opened; a print more than
  50% away from the recorded ``entry_price_usd`` is a different price scale -> dropped;
* challengers: ``hold60s`` (exit 60 s after entry), ``hold5m`` (300 s), ``tp1.25all`` (sell
  everything at the first print >= 1.25 x the entry reference);
* fill: the first print at least 12 s after the trigger (one protection tick) and at most 60 s
  after that (a staler print is not a fill) -> otherwise the pair is dropped as ``no_fill_print``;
  a trigger the incumbent never reached before closing leaves the challenger = incumbent;
* cost model, the SAME for both arms (the paper broker's own constants): every sell leg pays the
  venue fee (pumpswap 120 bps, pump curve 125, else ``paper.DEFAULT_FEE_BPS`` of the chain),
  ``paper.DEFAULT_SLIPPAGE_FLOOR_BPS``, constant-product impact against the print's liquidity,
  and ``paper.DEFAULT_TIP_NATIVE``. A leg without liquidity is an unknown cost -> pair dropped.
  The incumbent is re-costed with this model (its legs at the print protection used); its real
  ledger return is reported beside it as the model's calibration, never mixed in;
* verdict per (lane, chain, challenger): PASS only when the challenger's own returns clear the
  owner's pass line (:func:`kaiba.learning.sprint.score`: n >= 40, >= 15 per half, both halves
  > 0, 90% bootstrap lower bound > 0) AND the paired improvement's 90% lower bound is > 0.
  (2026-10-18: the halves split the pairs' own time span, not the 7-day window; the tape began
  10-16, so a window split put every pair in one half.)
* cost stress (2026-10-18, Codex review: a percentage fee does not cancel when the two arms'
  proceeds differ): every pair is also scored with :data:`STRESS_BPS` more cost on EVERY leg --
  the entry and each sell leg of both arms. Live sol fills measured ~2.4% all-in per side
  against the model's ~1.5%, so 100 bps covers that gap. ``survives_stress`` = the stressed
  challenger also clears the pass line AND the stressed paired lower bound stays > 0.

Research only: no provider call, no order, no database write. Database reads are short,
read-only, under the DATABASE READ RULE. Output: ``data/learning/twin_exits/latest.json``.
Run: ``python -m kaiba.learning.twin_exits once [--days 7]``.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sqlite3
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kaiba.core.schemas import Chain
from kaiba.execution import paper
from kaiba.execution.curve_price import CURVE_TOTAL_FEE_BPS, PUMPSWAP_TIER1_TOTAL_FEE_BPS
from kaiba.learning import fabricated, position_tape, shadow_tape, sprint, time_exit_trial

log = logging.getLogger(__name__)

VERSION = "twin_exits/1"
SETTLE_MS = 600_000
FILL_DELAY_MS = 12_000
FILL_MAX_AGE_MS = 60_000
LEG_PRINT_MAX_AGE_MS = 60_000
#: Extra cost per leg in the stressed scoring (entry and every sell leg, both arms).
STRESS_BPS = 100
SCALE_TOLERANCE = 0.5
NATIVE_DECIMALS = {"sol": 9, "bsc": 18, "robinhood": 18, "eth": 18, "base": 18}
NATIVE_PRICE_ALIAS = {"robinhood": "eth", "base": "eth"}
CHALLENGERS: tuple[tuple[str, str, float], ...] = (
    ("hold60s", "time", 60_000),
    ("hold5m", "time", 300_000),
    ("tp1.25all", "take_profit", 1.25),
)


@dataclass
class Leg:
    ts: int
    qty: int


@dataclass
class Pos:
    pid: str
    lane: str
    chain: str
    opened: int
    closed: int
    cost: int
    qty_total: int
    proceeds: int
    entry_price_usd: float | None
    exit_reason: str | None
    legs: list[Leg] = field(default_factory=list)


@dataclass
class Pair:
    closed: int
    challenger: float
    incumbent_model: float
    incumbent_actual: float
    triggered: bool
    challenger_stress: float | None = None
    incumbent_stress: float | None = None


# --------------------------------------------------------------------------- cost model


def _fee_bps(chain: str, venue: str | None) -> int:
    v = (venue or "").lower()
    if v == "pumpswap":
        return PUMPSWAP_TIER1_TOTAL_FEE_BPS
    if v in ("pumpfun", "pump.fun", "pump"):
        return CURVE_TOTAL_FEE_BPS
    try:
        return int(paper.DEFAULT_FEE_BPS.get(Chain(chain), 125))
    except ValueError:
        return 125


def sell_net(gross_native: float, print_row: Mapping[str, Any], chain: str, native_usd: float,
             extra_bps: float = 0.0) -> float | None:
    """Model net native for selling ``gross_native`` worth at ``print_row``; ``None`` = unknown.
    ``extra_bps`` adds cost on top of the model (the stressed scoring)."""
    try:
        liq = float(print_row.get("liq")) if print_row.get("liq") not in (None, "") else None
    except (TypeError, ValueError):
        liq = None
    if not liq or liq <= 0 or native_usd <= 0:
        return None
    size_usd = gross_native / 10 ** NATIVE_DECIMALS.get(chain, 18) * native_usd
    impact = size_usd / (liq + size_usd) * 10_000
    bps = _fee_bps(chain, print_row.get("venue")) + paper.DEFAULT_SLIPPAGE_FLOOR_BPS + impact + extra_bps
    try:
        tip = int(paper.DEFAULT_TIP_NATIVE.get(Chain(chain), 0))
    except ValueError:
        tip = 0
    return max(0.0, gross_native * (1 - bps / 10_000) - tip)


# --------------------------------------------------------------------------- one position


def _px(row: Mapping[str, Any]) -> float:
    return float(row["px"])


def _print_at_or_before(prints: Sequence[Mapping[str, Any]], ts: int) -> Mapping[str, Any] | None:
    best = None
    for r in prints:
        if r["obs"] <= ts:
            best = r
        else:
            break
    return best


def evaluate(pos: Pos, prints: Sequence[Mapping[str, Any]], native_usd: float) -> dict[str, Pair | str]:
    """``{challenger: Pair | drop_reason}`` for one closed position."""
    out: dict[str, Pair | str] = {}
    if pos.cost <= 0 or pos.qty_total <= 0 or not pos.legs:
        return {name: "no_ledger" for name, _k, _v in CHALLENGERS}
    entry = next((r for r in prints if r["obs"] >= pos.opened), None)
    if entry is None:
        return {name: "no_tape" for name, _k, _v in CHALLENGERS}
    e = _px(entry)
    if pos.entry_price_usd and abs(e / pos.entry_price_usd - 1) > SCALE_TOLERANCE:
        return {name: "entry_scale_mismatch" for name, _k, _v in CHALLENGERS}
    per_atom = pos.cost / pos.qty_total

    def value(qty: int, row: Mapping[str, Any], extra: float = 0.0) -> float | None:
        return sell_net(qty * per_atom * _px(row) / e, row, pos.chain, native_usd, extra)

    entry_stress = 1 - STRESS_BPS / 10_000  # a dearer entry buys proportionally fewer tokens
    legs = sorted(pos.legs, key=lambda leg: leg.ts)
    leg_nets: list[float | None] = []
    leg_nets_s: list[float | None] = []
    for leg in legs:
        row = _print_at_or_before(prints, leg.ts)
        ok = row is not None and leg.ts - row["obs"] <= LEG_PRINT_MAX_AGE_MS
        leg_nets.append(value(leg.qty, row) if ok else None)
        leg_nets_s.append(value(leg.qty, row, STRESS_BPS) if ok else None)
    actual = pos.proceeds / pos.cost - 1
    if any(n is None for n in leg_nets):
        return {name: "incumbent_leg_unpriced" for name, _k, _v in CHALLENGERS}
    incumbent = sum(leg_nets) / pos.cost - 1  # type: ignore[arg-type]
    incumbent_s = sum(leg_nets_s) * entry_stress / pos.cost - 1  # type: ignore[arg-type]
    last_exit = legs[-1].ts
    for name, kind, param in CHALLENGERS:
        if kind == "time":
            trigger = pos.opened + int(param)
        else:
            hit = next((r for r in prints if r["obs"] > entry["obs"] and _px(r) >= param * e), None)
            trigger = hit["obs"] if hit else None
        if trigger is None or trigger + FILL_DELAY_MS >= last_exit:
            out[name] = Pair(pos.closed, incumbent, incumbent, actual, False, incumbent_s, incumbent_s)  # never diverged
            continue
        fill = next((r for r in prints if r["obs"] >= trigger + FILL_DELAY_MS), None)
        if fill is None or fill["obs"] > trigger + FILL_DELAY_MS + FILL_MAX_AGE_MS or fill["obs"] >= last_exit:
            out[name] = "no_fill_print"
            continue
        before = [(leg, n, ns) for leg, n, ns in zip(legs, leg_nets, leg_nets_s, strict=True) if leg.ts <= fill["obs"]]
        remaining = pos.qty_total - sum(leg.qty for leg, _n, _ns in before)
        rest = value(remaining, fill) if remaining > 0 else 0.0
        rest_s = value(remaining, fill, STRESS_BPS) if remaining > 0 else 0.0
        if rest is None or rest_s is None:
            out[name] = "fill_cost_unknown"
            continue
        chal = (sum(n for _leg, n, _ns in before) + rest) / pos.cost - 1  # type: ignore[misc]
        chal_s = (sum(ns for _leg, _n, ns in before) + rest_s) * entry_stress / pos.cost - 1  # type: ignore[misc]
        out[name] = Pair(pos.closed, chal, incumbent, actual, True, chal_s, incumbent_s)
    return out


# --------------------------------------------------------------------------- reads


def _q(db: Path, sql: str, args: Sequence[Any] = ()) -> list[tuple]:
    wal = Path(str(db) + "-wal")
    waited = 0
    while wal.exists() and wal.stat().st_size > 1_500_000_000:
        if waited >= 600:
            raise RuntimeError("WAL over 1.5 GB for 10 min; not reading")
        time.sleep(60)
        waited += 60
    for attempt in range(2):  # one retry: a busy disk (e.g. the nightly backup) can stall a read
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
        deadline = time.monotonic() + 30
        conn.set_progress_handler(lambda d=deadline: 1 if time.monotonic() > d else 0, 10_000)
        try:
            return conn.execute(sql, tuple(args)).fetchall()
        except sqlite3.OperationalError as exc:
            if "interrupted" not in str(exc) or attempt:
                raise
            time.sleep(15)
        finally:
            conn.close()
    return []


def load_positions(db: Path, start_ms: int, end_ms: int) -> list[Pos]:
    rows: list[tuple] = []
    for lo in range(start_ms, end_ms, 86_400_000):  # one day per read: each stays short
        rows += _q(db, "SELECT position_id, lane, chain, opened_ms, closed_ms, cost_native, qty_total, proceeds_native, "
                       "entry_price_usd, exit_reason, mode FROM positions WHERE mode = 'shadow' AND closed_ms >= ? AND closed_ms < ?",
                   (lo, min(end_ms, lo + 86_400_000)))
    out: dict[str, Pos] = {}
    for pid, lane, chain, op, cl, cost, qty, proceeds, entry, reason, mode in rows:
        if fabricated.is_fabricated_outcome(reason, mode):
            continue
        try:
            out[pid] = Pos(pid, lane, chain, int(op), int(cl), int(cost), int(qty), int(proceeds),
                           float(entry) if entry not in (None, "") else None, reason)
        except (TypeError, ValueError):
            continue
    ids = list(out)
    for i in range(0, len(ids), 300):
        chunk = ids[i:i + 300]
        for pid, ts, amount_in in _q(
                db, "SELECT po.position_id, po.ts_ms, o.amount_in FROM position_orders po JOIN orders o "
                    f"ON o.order_id = po.order_id WHERE po.side = 'sell' AND po.position_id IN ({','.join('?' * len(chunk))})",
                chunk):
            try:
                out[pid].legs.append(Leg(int(ts), int(amount_in)))
            except (TypeError, ValueError):
                continue
    return list(out.values())


#: The newest native price an as-of read may use, relative to the time it prices.
FX_MAX_AGE_MS = 600_000


class NativeUsd:
    """The native token's USD price AS OF a time, never after it (``idx_native_prices_chain_ts``).

    Fixed 2026-10-18 (Codex twin review, codex-twin-review-20261018): the old key was the
    chain-HOUR and the read took the newest price before the hour's END, so a position priced
    at 12:01 could use a 12:59 observation -- up to 3,599 s of look-ahead -- and a stale, zero
    or NaN row was accepted. Now: one read per chain-MINUTE, taken at the minute's START (so a
    cached value can never come from after the time it prices), and 0.0 -- which ``sell_net``
    treats as unknown -- when the newest observation is invalid or older than
    :data:`FX_MAX_AGE_MS`. Here the native price only sizes the impact term, so the old error
    moved results negligibly; it is fixed because a look-ahead must never be tolerated."""

    def __init__(self, db: Path) -> None:
        self.db = db
        self.cache: dict[tuple[str, int], float] = {}

    def at(self, chain: str, ts: int) -> float:
        minute = int(ts) // 60_000 * 60_000
        key = (NATIVE_PRICE_ALIAS.get(chain, chain), minute)
        if key not in self.cache:
            rows = _q(self.db, "SELECT price_usd, ts_ms FROM native_prices WHERE chain = ? AND ts_ms <= ? "
                               "ORDER BY ts_ms DESC LIMIT 1", key)
            value = 0.0
            if rows:
                try:
                    price, observed = float(rows[0][0]), int(rows[0][1])
                    if math.isfinite(price) and price > 0 and 0 <= minute - observed <= FX_MAX_AGE_MS:
                        value = price
                except (TypeError, ValueError, OverflowError):
                    pass
            self.cache[key] = value
        return self.cache[key]


# --------------------------------------------------------------------------- verdicts


def summarize(results: Mapping[tuple[str, str, str], list[Pair | str]], start_ms: int, end_ms: int) -> list[dict[str, Any]]:
    cap = lambda x: max(-1.0, min(sprint.OUTCOME_CAP, x))  # noqa: E731
    rows = []
    for (lane, chain, name), items in sorted(results.items()):
        pairs = [p for p in items if isinstance(p, Pair)]
        drops: dict[str, int] = {}
        for p in items:
            if isinstance(p, str):
                drops[p] = drops.get(p, 0) + 1
        # Halves split the EVIDENCE's own time span, not the window: the tape began 2026-10-16,
        # so a 7-day window put every pair in its newer half and left every row PENDING.
        span_start = min((p.closed for p in pairs), default=start_ms)
        score = sprint.score(lane, chain, [(p.closed, cap(p.challenger)) for p in pairs], span_start, end_ms)
        diffs = [cap(p.challenger) - cap(p.incumbent_model) for p in pairs]
        lo = sprint.lower_bound(diffs)
        mean = lambda xs: (sum(xs) / len(xs)) if xs else None  # noqa: E731
        passed = score.verdict == "PASS" and lo is not None and lo > 0
        stressed = [p for p in pairs if p.challenger_stress is not None and p.incumbent_stress is not None]
        score_s = sprint.score(lane, chain, [(p.closed, cap(p.challenger_stress)) for p in stressed], span_start, end_ms)
        diffs_s = [cap(p.challenger_stress) - cap(p.incumbent_stress) for p in stressed]
        lo_s = sprint.lower_bound(diffs_s)
        rows.append({
            "lane": lane, "chain": chain, "challenger": name, "pairs": len(pairs),
            "triggered": sum(p.triggered for p in pairs), "dropped": drops,
            "challenger_mean": mean([cap(p.challenger) for p in pairs]),
            "incumbent_model_mean": mean([cap(p.incumbent_model) for p in pairs]),
            "incumbent_actual_mean": mean([cap(p.incumbent_actual) for p in pairs]),
            "model_abs_error_mean": mean([abs(p.incumbent_model - p.incumbent_actual) for p in pairs]),
            "paired_diff_mean": mean(diffs), "paired_diff_lower90": lo,
            "pass_line": {"verdict": score.verdict, "n": score.n, "mean_old": score.mean_old,
                          "mean_new": score.mean_new, "lower90": score.lower, "why": score.why},
            "verdict": "PASS" if passed else ("PENDING" if score.verdict == "PENDING" else "FAIL"),
            "stress_bps": STRESS_BPS,
            "challenger_stress_mean": mean([cap(p.challenger_stress) for p in stressed]),
            "paired_diff_stress_mean": mean(diffs_s), "paired_diff_stress_lower90": lo_s,
            "stress_pass_line": {"verdict": score_s.verdict, "mean_old": score_s.mean_old,
                                 "mean_new": score_s.mean_new, "lower90": score_s.lower},
            "survives_stress": bool(score_s.verdict == "PASS" and lo_s is not None and lo_s > 0),
        })
    return rows


#: The spool sits in /dev/shm, which counts against the unit's memory, so this ceiling stays well
#: inside its 400 MB cap. A week selected 87 MB on 2026-11-04 and 162 MB on 2026-10-27, when the
#: peak with Python was 230 MB (docs/research/codex-profit-current-20261027/position-tape-review.md).
TAPE_LIMITS = position_tape.Limits(max_spool_bytes=192 * 1024 * 1024)


def _spool_root() -> Path:
    shm = Path("/dev/shm")
    return shm if shm.is_dir() else Path(tempfile.gettempdir())


def run_once(db: Path, tape_dir: Path, out_dir: Path, *, days: int = 7, now_ms: int | None = None) -> dict[str, Any]:
    now = now_ms or int(time.time() * 1000)
    start, end = now - days * 86_400_000, now - SETTLE_MS
    positions = load_positions(db, start, end)
    # A time-exit trial's treated positions exited at the trial's time, not by the incumbent
    # policy the twins pair against: leave them out (kaiba/learning/time_exit_trial.py).
    trials = time_exit_trial.load_trials()
    in_trial = sum(1 for p in positions if time_exit_trial.treated(trials, p.lane, p.pid, p.opened))
    positions = [p for p in positions if not time_exit_trial.treated(trials, p.lane, p.pid, p.opened)]
    natives = NativeUsd(db)
    results: dict[tuple[str, str, str], list[Pair | str]] = {}
    # One position's rows in memory at a time. Loading the whole window held about 317k row dicts
    # and hit the unit's 400 MB cap on 8 of 12 runs on 2026-11-04, so no report was written.
    spanned = [p for p in positions if 0 <= p.opened <= p.closed]
    windows = [position_tape.Window(p.pid, p.opened, p.closed) for p in spanned]
    no_tape = len(positions) - len(spanned)
    with position_tape.PositionTape(tape_dir, start, end, windows, tail_ms=FILL_MAX_AGE_MS,
                                    spool_root=_spool_root(), limits=TAPE_LIMITS) as tape:
        for pos, (pid, prints) in zip(spanned, tape, strict=True):
            if pid != pos.pid:
                raise ValueError("position tape order changed")
            if not prints:
                no_tape += 1
                continue
            for name, got in evaluate(pos, prints, natives.at(pos.chain, pos.opened)).items():
                results.setdefault((pos.lane, pos.chain, name), []).append(got)
        tape_provenance = tape.receipt
    rows = summarize(results, start, end)
    report = {"version": VERSION, "at_ms": now, "window_days": days, "positions": len(positions),
              "positions_without_tape": no_tape, "positions_in_trial": in_trial, "rows": rows,
              "passes": [f"{r['lane']}/{r['chain']}/{r['challenger']}" for r in rows if r["verdict"] == "PASS"],
              "tape_provenance": tape_provenance}
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / "latest.json.tmp"
    tmp.write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, out_dir / "latest.json")
    return report


def render(report: Mapping[str, Any]) -> str:
    lines = [f"Twin exits ({report['window_days']} d, {report['positions']} closed paper positions, "
             f"{report['positions_without_tape']} without tape):"]
    for r in report["rows"]:
        if not r["pairs"]:
            continue
        pct = lambda x: "n/a" if x is None else f"{100 * x:+.1f}%"  # noqa: E731
        lines.append(f"  {r['verdict']:7} {r['lane']}/{r['chain']} {r['challenger']}: n={r['pairs']} "
                     f"chal {pct(r['challenger_mean'])} vs inc {pct(r['incumbent_model_mean'])} "
                     f"(diff {pct(r['paired_diff_mean'])}, low90 {pct(r['paired_diff_lower90'])})")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="paired exit challengers on closed paper positions")
    ap.add_argument("command", choices=["once"])
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    data = Path(os.environ.get("KAIBA_DATA_DIR") or Path.home() / "kaiba" / "data")
    report = run_once(shadow_tape.default_db(), shadow_tape.default_out(), data / "learning" / "twin_exits",
                      days=args.days)
    print(render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["CHALLENGERS", "Leg", "Pair", "Pos", "evaluate", "run_once", "render", "sell_net", "summarize"]
