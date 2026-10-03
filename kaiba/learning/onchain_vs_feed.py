"""Would a pool-price stop have fired earlier and better than the DexScreener/GMGN mark?

Reads what :mod:`kaiba.execution.onchain_pool` logged (``onchain_price_samples``) beside the
watchdog's own accepted marks (``position_marks``) for CLOSED live Robinhood positions, and
answers per position:

* **when** the on-chain pool price first crossed the stop the watchdog was carrying at that
  moment, and when the incumbent mark did (``lead_s`` > 0 means the pool saw it first);
* **at what price**: the pool price where the on-chain series crossed (after an execution
  latency, default 2 s; the audit measured sells landing a median 1.8 s after the trigger)
  against the pool price at the moment the incumbent actually triggered. Both are pool
  prices, so fees and impact cancel to first order: ``improvement_pp`` is the counterfactual
  gain in percentage points of entry from deciding on the pool rather than the feed;
* **false alarms**: positions where the pool crossed and the incumbent never did. Those are
  the cost side -- audit-20261001 §1c recorded a -30.0% DexScreener trigger that sold at
  +8.5%; faster stops also fire on wicks that recover. ``onchain_only_delta_pp`` is what
  selling at the pool's cross would have done relative to what the position actually made.

This is the evidence the lead asked for before switching the RH decision source
(audit-20261001-strategy #3: "Ship only if the shadow stop realizes better than the
incumbent on >=30 stops"). It changes nothing; it opens the database read-only.

Sampling caveat, stated rather than hidden: both series are SAMPLED (the pool once per
protection tick, ~12 s on the box; marks once per accepted quote), so a crossing time is
known to within one tick. That resolution is fine against a median 36 s feed freeze.

Run on the box (read-only, bounded, indexed reads)::

    nice -n 19 .venv/bin/python -m kaiba.learning.onchain_vs_feed \\
        --db /home/ubuntu/kaiba/data/kaiba.db --since-days 14
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

#: Exit reasons that are a price stop firing. ``trailing_stop`` counts: it is the same
#: comparison against a ratcheted stop, and the pool would move it just the same.
STOP_REASONS = ("stop_loss", "emergency_loss", "trailing_stop")

#: Shipped ``protection.stop_loss_bps``; used only before the first sample carries a stop.
DEFAULT_STOP_BPS = 3000

#: Seconds between the decision and the sell landing on chain (audit §1c median 1.8 s).
DEFAULT_LATENCY_S = 2.0

#: The audit's bar for switching the decision source.
EVIDENCE_TARGET_STOPS = 30

#: Feed marks earlier than the first pool sample by more than this are outside the window
#: both series cover, and are not compared. A position opened before the logger was turned
#: on, or before its pool resolved, would otherwise "cross" on the feed with no pool to
#: compare against -- and against a stop the samples never recorded.
COVERAGE_SLACK_MS = 15_000


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        out = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return out if out.is_finite() else None


def _pct(price: Decimal | None, entry: Decimal) -> float | None:
    if price is None or entry <= 0:
        return None
    return float((price / entry - 1) * 100)


@dataclass
class PositionResult:
    position_id: str
    token: str
    exit_reason: str | None
    opened_ms: int
    closed_ms: int | None
    samples: int
    priced_samples: int
    marks: int
    realized_pct: float | None
    onchain_cross_ms: int | None = None
    incumbent_cross_ms: int | None = None
    lead_s: float | None = None
    onchain_exit_pct: float | None = None  # pool price at its own cross + latency
    incumbent_trigger_pct: float | None = None  # the feed's mark that triggered
    pool_at_incumbent_trigger_pct: float | None = None  # what the pool said at that moment
    improvement_pp: float | None = None  # onchain_exit_pct - pool_at_incumbent_trigger_pct
    onchain_only: bool = False
    onchain_only_delta_pp: float | None = None  # onchain_exit_pct - realized_pct
    recovered_after_onchain_cross: bool = False
    incumbent_age_p50_s: float | None = None
    incumbent_repeat_share: float | None = None  # consecutive samples with an unchanged mark
    onchain_repeat_share: float | None = None
    divergence_p50_pct: float | None = None  # |pool / mark - 1| at the same tick
    note: str | None = None


@dataclass
class Report:
    generated_ms: int
    since_ms: int
    stop_bps: int
    latency_s: float
    positions_closed: int = 0
    positions_with_samples: int = 0
    positions_unpriced: int = 0
    stop_exits: int = 0
    both_crossed: int = 0
    onchain_first: int = 0
    lead_s_p50: float | None = None
    improvement_pp_mean: float | None = None
    improvement_pp_p50: float | None = None
    onchain_only: int = 0
    onchain_only_delta_pp_mean: float | None = None
    incumbent_repeat_share: float | None = None
    onchain_repeat_share: float | None = None
    evidence_progress: str = ""
    verdict: str = ""
    positions: list[PositionResult] = field(default_factory=list)


def _stop_at(ts: int, steps: list[tuple[int, Decimal]], fallback: Decimal) -> Decimal:
    """The stop the watchdog carried at ``ts``: the last sampled stop at or before it."""
    current = steps[0][1] if steps else fallback
    for at, stop in steps:
        if at > ts:
            break
        current = stop
    return current


def _repeat_share(values: Sequence[Any]) -> float | None:
    pairs = [(a, b) for a, b in zip(values, values[1:], strict=False) if a is not None and b is not None]
    if not pairs:
        return None
    return sum(1 for a, b in pairs if a == b) / len(pairs)


def analyse_position(
    position: dict[str, Any],
    samples: list[dict[str, Any]],
    marks: list[dict[str, Any]],
    *,
    stop_bps: int = DEFAULT_STOP_BPS,
    latency_s: float = DEFAULT_LATENCY_S,
) -> PositionResult:
    """One closed position. Pure: no database, no clock."""
    entry = _dec(position.get("entry_price_usd")) or Decimal(0)
    cost = _dec(position.get("cost_native"))
    realized = _dec(position.get("realized_native"))
    realized_pct = float(realized / cost * 100) if cost and realized is not None and cost > 0 else None
    result = PositionResult(
        position_id=str(position["position_id"]),
        token=str(position.get("token") or ""),
        exit_reason=position.get("exit_reason"),
        opened_ms=int(position.get("opened_ms") or 0),
        closed_ms=position.get("closed_ms"),
        samples=len(samples),
        priced_samples=0,
        marks=len(marks),
        realized_pct=realized_pct,
    )
    if entry <= 0:
        result.note = "entry_price_unknown"
        return result
    hard_stop = entry * (Decimal(1) - Decimal(stop_bps) / Decimal(10_000))
    steps = [(int(s["ts_ms"]), stop) for s in samples if (stop := _dec(s.get("stop_price_usd"))) is not None]
    pool = [(int(s["ts_ms"]), p) for s in samples if (p := _dec(s.get("price_usd"))) is not None]
    result.priced_samples = len(pool)
    if not pool:
        result.note = "no_usd_priced_samples"
        return result

    # Feed staleness and divergence, measured at the same ticks.
    ages = [int(s["incumbent_age_ms"]) for s in samples if s.get("incumbent_age_ms") is not None]
    result.incumbent_age_p50_s = statistics.median(ages) / 1000 if ages else None
    result.incumbent_repeat_share = _repeat_share([s.get("incumbent_price_usd") for s in samples])
    result.onchain_repeat_share = _repeat_share([s.get("price_quote") for s in samples])
    gaps = []
    for s in samples:
        p, inc = _dec(s.get("price_usd")), _dec(s.get("incumbent_price_usd"))
        if p is not None and inc is not None and inc > 0:
            gaps.append(abs(float(p / inc - 1)) * 100)
    result.divergence_p50_pct = statistics.median(gaps) if gaps else None

    # The pool's first crossing of the stop it was being judged against at that tick.
    for ts, price in pool:
        if price <= _stop_at(ts, steps, hard_stop):
            result.onchain_cross_ms = ts
            break

    # The feed's first crossing: the watchdog's own accepted marks, else the per-sample copy,
    # inside the window the pool series covers.
    window_start = pool[0][0] - COVERAGE_SLACK_MS
    feed = [
        (ts, p) for m in marks
        if (p := _dec(m.get("price_usd"))) is not None and (ts := int(m["ts_ms"])) >= window_start
    ]
    if not feed:
        feed = [
            (ts, p) for s in samples
            if (p := _dec(s.get("incumbent_price_usd"))) is not None
            and (ts := int(s.get("incumbent_observed_ms") or s["ts_ms"])) >= window_start
        ]
    feed.sort()
    for ts, price in feed:
        if price <= _stop_at(ts, steps, hard_stop):
            result.incumbent_cross_ms = ts
            result.incumbent_trigger_pct = _pct(price, entry)
            break

    def pool_at(ts: int) -> Decimal | None:
        """The first pool price at or after ``ts`` (the next block we saw), else the last."""
        for at, price in pool:
            if at >= ts:
                return price
        return pool[-1][1] if pool else None

    if result.onchain_cross_ms is not None:
        exit_price = pool_at(result.onchain_cross_ms + int(latency_s * 1000))
        result.onchain_exit_pct = _pct(exit_price, entry)
        stop_then = _stop_at(result.onchain_cross_ms, steps, hard_stop)
        horizon = result.incumbent_cross_ms or (int(result.closed_ms) if result.closed_ms else None)
        result.recovered_after_onchain_cross = any(
            price > stop_then
            for at, price in pool
            if at > result.onchain_cross_ms and (horizon is None or at <= horizon)
        )
    if result.incumbent_cross_ms is not None:
        result.pool_at_incumbent_trigger_pct = _pct(pool_at(result.incumbent_cross_ms), entry)

    if result.onchain_cross_ms is not None and result.incumbent_cross_ms is not None:
        result.lead_s = (result.incumbent_cross_ms - result.onchain_cross_ms) / 1000
        if result.onchain_exit_pct is not None and result.pool_at_incumbent_trigger_pct is not None:
            result.improvement_pp = result.onchain_exit_pct - result.pool_at_incumbent_trigger_pct
    elif result.onchain_cross_ms is not None:
        result.onchain_only = True
        if result.onchain_exit_pct is not None and realized_pct is not None:
            result.onchain_only_delta_pp = result.onchain_exit_pct - realized_pct
    return result


def summarize(results: list[PositionResult], report: Report) -> Report:
    report.positions = results
    report.positions_with_samples = sum(1 for r in results if r.samples)
    report.positions_unpriced = sum(1 for r in results if r.samples and not r.priced_samples)
    report.stop_exits = sum(1 for r in results if r.priced_samples and str(r.exit_reason or "").startswith(STOP_REASONS))
    both = [r for r in results if r.lead_s is not None]
    report.both_crossed = len(both)
    report.onchain_first = sum(1 for r in both if (r.lead_s or 0) > 0)
    if both:
        report.lead_s_p50 = statistics.median(r.lead_s for r in both if r.lead_s is not None)
    gains = [r.improvement_pp for r in both if r.improvement_pp is not None]
    if gains:
        report.improvement_pp_mean = statistics.fmean(gains)
        report.improvement_pp_p50 = statistics.median(gains)
    only = [r for r in results if r.onchain_only]
    report.onchain_only = len(only)
    deltas = [r.onchain_only_delta_pp for r in only if r.onchain_only_delta_pp is not None]
    if deltas:
        report.onchain_only_delta_pp_mean = statistics.fmean(deltas)
    reps = [r.incumbent_repeat_share for r in results if r.incumbent_repeat_share is not None]
    report.incumbent_repeat_share = statistics.fmean(reps) if reps else None
    reps = [r.onchain_repeat_share for r in results if r.onchain_repeat_share is not None]
    report.onchain_repeat_share = statistics.fmean(reps) if reps else None
    report.evidence_progress = f"{report.both_crossed}/{EVIDENCE_TARGET_STOPS} stops crossed by both series"
    if report.both_crossed < EVIDENCE_TARGET_STOPS:
        report.verdict = "INSUFFICIENT: keep logging; do not switch the decision source"
    else:
        net = (report.improvement_pp_mean or 0.0) * report.both_crossed + sum(deltas)
        report.verdict = (
            f"MEASURED: net {net:+.1f}pp over {report.both_crossed} stops and {len(only)} pool-only "
            "crossings; the lead decides"
        )
    return report


def run(
    conn: sqlite3.Connection,
    *,
    since_ms: int,
    stop_bps: int = DEFAULT_STOP_BPS,
    latency_s: float = DEFAULT_LATENCY_S,
    limit: int = 500,
) -> Report:
    """Every closed live RH position since ``since_ms``. Indexed, bounded reads only."""
    conn.row_factory = sqlite3.Row
    report = Report(generated_ms=int(time.time() * 1000), since_ms=since_ms, stop_bps=stop_bps, latency_s=latency_s)
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='onchain_price_samples'"
    ).fetchone()
    if not has_table:
        report.verdict = "NO DATA: onchain_price_samples is absent (migration 033 not applied yet)"
        return report
    positions = [
        dict(r)
        for r in conn.execute(
            "SELECT position_id, token, opened_ms, closed_ms, exit_reason, entry_price_usd, cost_native, "
            "realized_native FROM positions WHERE closed_ms >= ? AND chain='robinhood' AND mode != 'shadow' "
            "ORDER BY closed_ms DESC LIMIT ?",
            (since_ms, int(limit)),
        )
    ]
    report.positions_closed = len(positions)
    results: list[PositionResult] = []
    for position in positions:
        samples = [
            dict(r)
            for r in conn.execute(
                "SELECT ts_ms, price_usd, price_quote, stop_price_usd, incumbent_price_usd, "
                "incumbent_observed_ms, incumbent_age_ms FROM onchain_price_samples "
                "WHERE position_id=? ORDER BY ts_ms LIMIT 20000",
                (position["position_id"],),
            )
        ]
        if not samples:
            continue
        marks = [
            dict(r)
            for r in conn.execute(
                "SELECT ts_ms, price_usd FROM position_marks WHERE position_id=? ORDER BY ts_ms LIMIT 20000",
                (position["position_id"],),
            )
        ]
        results.append(
            analyse_position(position, samples, marks, stop_bps=stop_bps, latency_s=latency_s)
        )
    return summarize(results, report)


def _table(report: Report) -> str:
    def f(value: Any, spec: str = "+.1f") -> str:
        return "-" if value is None else format(value, spec)

    lines = [
        f"closed live RH positions since {report.since_ms}: {report.positions_closed}; with samples "
        f"{report.positions_with_samples} (no USD rate: {report.positions_unpriced}); stop exits {report.stop_exits}",
        f"both crossed {report.both_crossed}, pool first {report.onchain_first}, lead p50 {f(report.lead_s_p50, '.0f')} s",
        f"improvement pp mean {f(report.improvement_pp_mean)} p50 {f(report.improvement_pp_p50)}; "
        f"pool-only crossings {report.onchain_only} (delta vs realized mean {f(report.onchain_only_delta_pp_mean)})",
        f"unchanged consecutive marks: feed {f(report.incumbent_repeat_share, '.0%')}, "
        f"pool {f(report.onchain_repeat_share, '.0%')}",
        f"{report.evidence_progress} -> {report.verdict}",
        "",
        "position     exit             realized  feed_trig  pool@trig  pool_exit  gain_pp  lead_s  only",
    ]
    for r in report.positions:
        lines.append(
            f"{r.position_id[-12:]:<12} {str(r.exit_reason or '')[:16]:<16} {f(r.realized_pct):>8} "
            f"{f(r.incumbent_trigger_pct):>10} {f(r.pool_at_incumbent_trigger_pct):>10} "
            f"{f(r.onchain_exit_pct):>10} {f(r.improvement_pp):>8} {f(r.lead_s, '.0f'):>7} "
            f"{'yes' if r.onchain_only else '':>5}"
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - operator entry point
    ap = argparse.ArgumentParser(description=__doc__ and __doc__.splitlines()[0])
    ap.add_argument("--db", required=True, help="path to kaiba.db (opened read-only)")
    ap.add_argument("--since-days", type=float, default=14.0)
    ap.add_argument("--stop-bps", type=int, default=DEFAULT_STOP_BPS)
    ap.add_argument("--latency-s", type=float, default=DEFAULT_LATENCY_S)
    ap.add_argument("--json", action="store_true")
    ns = ap.parse_args(list(argv) if argv is not None else sys.argv[1:])
    conn = sqlite3.connect(f"file:{Path(ns.db).as_posix()}?mode=ro", uri=True, timeout=10)
    try:
        since = int(time.time() * 1000 - ns.since_days * 86_400_000)
        report = run(conn, since_ms=since, stop_bps=ns.stop_bps, latency_s=ns.latency_s)
    finally:
        conn.close()
    print(json.dumps(asdict(report), indent=1, default=str) if ns.json else _table(report))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
