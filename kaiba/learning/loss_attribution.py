"""Where did the money go: the ENTRY, or the EXIT? And what would have told the two apart?

Deterministic. Reads recorded rows only -- no model, no provider call, no quote, no order.
It PROPOSES NOTHING: the daily LLM learning loop reads the ranked output (``kv`` key
:data:`KV_KEY`, table :data:`TABLE`) and proposes at most one experiment; the gates in
:mod:`kaiba.learning.gates` judge it. A module that both diagnosed and changed thresholds
would be grading its own homework.

WHY (2026-10-03, box). 30-day live sm-trenches: robinhood 91 trades mean -9.1%, sol 94 mean
-14.9%, bsc 51. Last 7 days by exit: stop_loss 13 trades mean -36.5% against a -30% stop,
emergency_loss 2 at -64%, trailing_stop 12 at +30.7%. ~70% of trades end at the stop, so the
first question is whether that is the ENTRY (the token falls after we buy) or the EXIT (we
sell well below the level the policy set). The two have different fixes, and neither can be
argued from a mean.

PART A -- per closed live position, the loss split into components that SUM to it:

    ret = entry_pp + exit_pp
    entry_pp   = min(0, level)           the loss the exit policy ACCEPTED when the price
                                         path reached its level: the stop (stop_loss /
                                         emergency_loss / trailing_stop use
                                         positions.stop_price_usd), else the mark the
                                         watchdog acted on (rug, stale, write-off)
    exit_pp    = cushion + gap + fill + cost
      cushion  = max(0, level)           a trailing stop above entry given back
      gap      = trigger - level         how far past the level the price already was on
                                         the mark the exit acted on (detection / gap)
      fill     = realised px - trigger   the sell fills against that mark
      cost     = ret - realised px       fees, rent, native drift, dust left behind

  all in percentage points of cost, prices in USD from ``positions.entry_price_usd``,
  ``position_marks`` and the sell legs of ``fill_prices``. Where ``onchain_price_samples``
  exist (Robinhood, log-only), the realised exit is ALSO compared with the pool's own price
  at the trigger, and the time the pool crossed the stop with the time our mark did.
  A loss is labelled ``entry`` when entry_pp is the larger part, ``exit`` otherwise.

PART B -- which entry features separate winners from losers on the SCANNED population:
every decision the lane made (skips included), first one per token, with a FORWARD outcome
priced from the swap tape: the first touch of +/-``barrier_pct`` within ``horizon_s`` of the
decision (a bracket at the live stop), else the last price at the horizon. Owner directive
2026-09-24: a filter measured on the fills we took removed 98% and 87.5% of the population
we scan, so the scanned population is the denominator, and a recorded-unknown feature counts
as REFUSED by a threshold (the lane refuses it live; ``lanes.feature_threshold_refusal``).

  * Features come from the decision's own signal payload (``recorded``). For signals that
    predate a feature, the ones that are point-in-time reconstructable from the tokens row
    and the swap tape (token age, migration, window flow, window price path) are rebuilt
    with the lane's own code (``reconstructed``; a swap ingested late with an older
    timestamp can leak in, so they are labelled). Dossier features cannot be rebuilt --
    ``token_dossiers`` keeps only the newest dossier -- and are ranked only once recorded.
  * Each feature's threshold is CHOSEN on the older half (by time) among its deciles,
    keeping >= ``min_keep_share`` of the population (the gates' removal ceiling), and then
    SCORED on the newer half it never saw. Only a feature that improves the newer half is
    called separating. This is a hint for a proposer, not a verdict: the gates' replay and
    shadow judge whatever is proposed.
  * ``gate_ready`` says whether the replay gate could judge it yet: the gate reads RECORDED
    payloads of closed trades only, and its relative criterion needs the newest 30% of them
    to span >= 20 UTC days.

    python -m kaiba.learning.loss_attribution [--db PATH] [--json] [--write]
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from kaiba.core.db import fetch_all, fetch_one, jdump, jload, tx
from kaiba.core.schemas import NATIVE_DECIMALS, Chain, now_ms

log = logging.getLogger(__name__)

VERSION = "loss-attribution-v1"
KV_KEY = "learning:loss_attribution"
TABLE = "loss_attribution"
DAY_MS = 86_400_000

DEFAULTS: dict[str, Any] = {
    "trade_days": 30,          # Part A: closed live positions in this window
    "summary_days": 7,         # the headline window the daily report prints
    "scan_days": 30,           # Part B: decisions in this window
    "lanes": "sm-trenches",    # comma list
    "horizon_s": 7200,         # forward outcome horizon after the decision
    "barrier_pct": 30.0,       # +/- bracket; 30 = the live hard stop (stop_loss_bps 3000)
    "pre_window_s": 300,       # the decision price is the median of the last prints in this
    "max_decisions": 4000,     # decisions read for Part B (newest first)
    "max_swaps": 5000,         # swap rows per query (forward and backward each)
    "min_feature_n": 40,       # rows with the feature before it is ranked at all
    "min_keep_share": 0.5,     # = gates.MAX_REMOVAL_SHARE: a threshold may refuse <= half
    "never_green_pct": 2.0,    # MFE below this: the position never went meaningfully green
    "top_features": 12,
}

#: Exit reasons whose level is the stop on the position row.
LEVEL_EXITS = frozenset({"stop_loss", "emergency_loss", "trailing_stop"})

#: Exits that are not a market exit at all: a ledger repair. Attributed, never ranked.
NON_MARKET_EXITS = frozenset({"bookkeeping_correction"})

#: Reconstructable from the swap tape / tokens row with the lane's own code.
RECONSTRUCTABLE = frozenset({
    "token_age_s", "migrated", "since_migration_s", "launchpad",
    "window_swaps", "window_buy_usd", "window_sell_usd", "window_buy_share",
    "window_buyers", "window_sellers", "price_change_window_pct", "from_window_high_pct",
})


# ---------------------------------------------------------------------------- helpers


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _int(value: Any) -> int | None:
    d = _dec(value)
    if d is None:
        return None
    try:
        return int(d)
    except (ValueError, OverflowError):
        return None


def _pct(px: Decimal | None, base: Decimal | None) -> float | None:
    if px is None or base is None or base <= 0:
        return None
    return float((px / base - 1) * 100)


def _r(value: float | None, places: int = 2) -> float | None:
    return None if value is None else round(float(value), places)


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _params(overrides: Mapping[str, Any] | None) -> dict[str, Any]:
    out = dict(DEFAULTS)
    for key, value in (overrides or {}).items():
        if key in DEFAULTS and value is not None:
            out[key] = value
    return out


def ensure_table(conn: sqlite3.Connection) -> None:
    """One row per closed live position. Lives here, like ``deployer_stats``: a learning
    output table, rebuilt by this module, read by nothing that trades."""
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {TABLE} ("
        "  position_id TEXT PRIMARY KEY,"
        "  trade_id TEXT, decision_id TEXT, lane TEXT, chain TEXT NOT NULL, token TEXT,"
        "  opened_ms INTEGER, closed_ms INTEGER NOT NULL, exit_reason TEXT, exit_family TEXT,"
        "  ret_pct REAL, level_pct REAL, entry_pp REAL, exit_pp REAL, cushion_pp REAL,"
        "  gap_pp REAL, fill_pp REAL, cost_pp REAL, after_trigger_pp REAL,"
        "  mfe_pct REAL, mae_pct REAL, excursion_basis TEXT, marks INTEGER,"
        "  time_to_level_s REAL, exit_latency_s REAL, hold_s REAL,"
        "  pool_ret_at_trigger_pct REAL, fill_vs_pool_pp REAL, mark_vs_pool_pp REAL,"
        "  pool_lead_s REAL, cost_usd REAL, pnl_usd REAL, label TEXT, never_green INTEGER,"
        "  notes_json TEXT NOT NULL DEFAULT '[]', computed_ms INTEGER NOT NULL, version TEXT)"
    )
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_closed ON {TABLE}(closed_ms)")


# ============================================================================ PART A


def _sell_legs(conn: sqlite3.Connection, position_id: str) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """``(sell legs, buy leg)`` with their ``fill_prices`` rows (None where unpriced)."""
    rows = fetch_all(
        conn,
        "SELECT po.order_id, po.side, po.ts_ms, f.price_usd, f.token_atoms, f.native_usd, "
        "f.fill_ts_ms FROM position_orders po LEFT JOIN fill_prices f ON f.order_id = po.order_id "
        "WHERE po.position_id = ? ORDER BY po.ts_ms",
        (position_id,),
    )
    sells = [r for r in rows if str(r["side"]).lower() == "sell"]
    buys = [r for r in rows if str(r["side"]).lower() == "buy"]
    return sells, (buys[0] if buys else None)


def _marks(conn: sqlite3.Connection, position_id: str, limit: int = 50_000) -> list[tuple[int, Decimal]]:
    out: list[tuple[int, Decimal]] = []
    for r in fetch_all(
        conn,
        "SELECT ts_ms, price_usd FROM position_marks WHERE position_id = ? ORDER BY ts_ms LIMIT ?",
        (position_id, int(limit)),
    ):
        px = _dec(r["price_usd"])
        if px is not None and px > 0:
            out.append((int(r["ts_ms"]), px))
    return out


def _pool_samples(conn: sqlite3.Connection, position_id: str) -> list[tuple[int, Decimal]]:
    try:
        rows = fetch_all(
            conn,
            "SELECT ts_ms, price_usd FROM onchain_price_samples WHERE position_id = ? ORDER BY ts_ms",
            (position_id,),
        )
    except sqlite3.Error:  # the table is log-only and may not exist (not migrated)
        return []
    out = []
    for r in rows:
        px = _dec(r["price_usd"])
        if px is not None and px > 0:
            out.append((int(r["ts_ms"]), px))
    return out


def attribute_position(conn: sqlite3.Connection, p: Mapping[str, Any], *,
                       never_green_pct: float = DEFAULTS["never_green_pct"]) -> dict[str, Any]:
    """Split one closed position's return into entry and exit components. Pure arithmetic
    over its rows; every component it cannot price is ``None`` and says why in ``notes``."""
    notes: list[str] = []
    pid = str(p["position_id"])
    reason = str(p.get("exit_reason") or "unknown")
    family = reason.split(":", 1)[0]
    cost = _int(p.get("cost_native"))
    realized = _int(p.get("realized_native"))  # TEXT on the table: never compare as text
    ret = (realized / cost * 100.0) if cost and cost > 0 and realized is not None else None
    if ret is None:
        notes.append("return unavailable: cost or realized missing")
    entry = _dec(p.get("entry_price_usd"))
    stop = _dec(p.get("stop_price_usd"))
    opened, closed = int(p.get("opened_ms") or 0), int(p.get("closed_ms") or 0)

    sells, buy = _sell_legs(conn, pid)
    marks = _marks(conn, pid)
    sell_ts = min((int(s["ts_ms"]) for s in sells), default=closed) or closed

    # excursions: the in-lifetime marks first, the stored row second
    in_life = [(t, px) for t, px in marks if opened <= t <= max(closed, sell_ts)]
    if in_life and entry:
        mfe = _pct(max(px for _, px in in_life), entry)
        mae = _pct(min(px for _, px in in_life), entry)
        basis = "marks"
    else:
        mfe, mae = p.get("mfe_pct"), p.get("mae_pct")
        basis = "position_row" if (mfe is not None or mae is not None) else "none"
        if basis == "none":
            notes.append("no in-lifetime marks and no stored excursion")

    level = _pct(stop, entry) if family in LEVEL_EXITS else None
    if family in LEVEL_EXITS and level is None:
        notes.append("level exit without a readable stop/entry price")
    trigger_px = None
    for t, px in in_life or marks:
        if t <= sell_ts + 1_000:
            trigger_px = px
    trigger = _pct(trigger_px, entry)
    if trigger is None:
        notes.append("no mark at or before the first sell")

    # realised exit price: token-weighted USD over the priced sell legs
    num = den = Decimal(0)
    unpriced = 0
    for s in sells:
        px, atoms = _dec(s.get("price_usd")), _dec(s.get("token_atoms"))
        if px is None or atoms is None or atoms <= 0:
            unpriced += 1
            continue
        num += px * atoms
        den += atoms
    exit_px = (num / den) if den > 0 and not unpriced else None
    realised = _pct(exit_px, entry)
    if sells and exit_px is None:
        notes.append(f"{unpriced}/{len(sells)} sell legs unpriced in fill_prices")

    split_level = level if level is not None else trigger
    out: dict[str, Any] = {
        "position_id": pid, "trade_id": p.get("trade_id"), "decision_id": p.get("decision_id"),
        "lane": p.get("lane"), "chain": p.get("chain"), "token": p.get("token"),
        "opened_ms": opened, "closed_ms": closed, "exit_reason": reason, "exit_family": family,
        "ret_pct": _r(ret, 4), "level_pct": _r(level, 4),
        "entry_pp": None, "exit_pp": None, "cushion_pp": None, "gap_pp": None,
        "fill_pp": None, "cost_pp": None, "after_trigger_pp": None,
        "mfe_pct": _r(mfe, 4), "mae_pct": _r(mae, 4), "excursion_basis": basis,
        "marks": len(in_life), "hold_s": _r((closed - opened) / 1000.0, 1) if closed and opened else None,
        "time_to_level_s": None, "exit_latency_s": None,
        "pool_ret_at_trigger_pct": None, "fill_vs_pool_pp": None, "mark_vs_pool_pp": None,
        "pool_lead_s": None, "cost_usd": None, "pnl_usd": None,
    }
    if ret is not None and split_level is not None:
        entry_pp = min(0.0, split_level)
        out["entry_pp"] = _r(entry_pp, 4)
        out["exit_pp"] = _r(ret - entry_pp, 4)
        out["cushion_pp"] = _r(max(0.0, split_level), 4)
        if level is not None and trigger is not None:
            out["gap_pp"] = _r(trigger - level, 4)
        elif level is None and trigger is not None:
            out["gap_pp"] = 0.0  # the level IS the mark acted on
        if trigger is not None:
            out["after_trigger_pp"] = _r(ret - trigger, 4)
            if realised is not None:
                out["fill_pp"] = _r(realised - trigger, 4)
                out["cost_pp"] = _r(ret - realised, 4)
    elif ret is not None:
        notes.append("unattributable: no level and no trigger mark")

    if level is not None and stop is not None:
        breach = next((t for t, px in in_life if px <= stop), None)
        if breach is not None:
            out["time_to_level_s"] = _r((breach - opened) / 1000.0, 1)
            out["exit_latency_s"] = _r((sell_ts - breach) / 1000.0, 1)

    pool = _pool_samples(conn, pid)
    if pool and entry:
        at = [px for t, px in pool if t <= sell_ts + 1_000]
        if at:
            pool_ret = _pct(at[-1], entry)
            out["pool_ret_at_trigger_pct"] = _r(pool_ret, 4)
            if realised is not None and pool_ret is not None:
                out["fill_vs_pool_pp"] = _r(realised - pool_ret, 4)
            if trigger is not None and pool_ret is not None:
                out["mark_vs_pool_pp"] = _r(trigger - pool_ret, 4)
        if stop is not None and level is not None:
            pool_breach = next((t for t, px in pool if px <= stop), None)
            mark_breach = next((t for t, px in in_life if px <= stop), None)
            if pool_breach is not None and mark_breach is not None:
                out["pool_lead_s"] = _r((mark_breach - pool_breach) / 1000.0, 1)

    # USD at the entry fill's native price: flat $-sizing makes pp ~ dollars, this makes it exact
    native_usd = _dec((buy or {}).get("native_usd"))
    try:
        dec = NATIVE_DECIMALS[Chain(str(p.get("chain")))]
    except ValueError:
        dec = None
    if native_usd is not None and dec is not None and cost:
        scale = Decimal(10) ** dec
        out["cost_usd"] = _r(float(Decimal(cost) / scale * native_usd), 2)
        if realized is not None:
            out["pnl_usd"] = _r(float(Decimal(realized) / scale * native_usd), 2)

    if family in NON_MARKET_EXITS:
        label = "non_market"
    elif ret is None or out["entry_pp"] is None:
        label = "unattributed"
    elif ret >= 0:
        label = "win"
    else:
        label = "entry" if out["entry_pp"] <= out["exit_pp"] else "exit"
    out["label"] = label
    out["never_green"] = (None if out["mfe_pct"] is None
                          else int(float(out["mfe_pct"]) < float(never_green_pct)))
    out["notes"] = notes
    return out


def closed_positions(conn: sqlite3.Connection, *, since_ms: int, until_ms: int,
                     lanes: Sequence[str] | None = None) -> list[dict[str, Any]]:
    """Closed LIVE/CANARY positions in the window, oldest first, with their trade row."""
    rows = fetch_all(
        conn,
        "SELECT p.*, t.trade_id, t.decision_id FROM positions p "
        "LEFT JOIN trades t ON t.position_id = p.position_id "
        "WHERE p.closed_ms >= ? AND p.closed_ms < ? AND p.mode IN ('live', 'canary') "
        "ORDER BY p.closed_ms ASC, p.position_id ASC",
        (int(since_ms), int(until_ms)),
    )
    if lanes:
        keep = set(lanes)
        rows = [r for r in rows if r.get("lane") in keep]
    seen: set[str] = set()
    out = []
    for r in rows:  # a position with two trade rows is attributed once
        if r["position_id"] not in seen:
            seen.add(r["position_id"])
            out.append(r)
    return out


def summarise_trades(rows: Sequence[Mapping[str, Any]], *, since_ms: int) -> dict[str, Any]:
    """Losses split ENTRY vs EXIT, per exit family, and the ranked leaks. Over ``since_ms``."""
    window = [r for r in rows if int(r["closed_ms"]) >= since_ms and r["label"] != "non_market"]
    losses = [r for r in window if r["ret_pct"] is not None and r["ret_pct"] < 0]
    wins = [r for r in window if r["ret_pct"] is not None and r["ret_pct"] >= 0]
    attributed = [r for r in losses if r["entry_pp"] is not None]

    def s(rows_: Sequence[Mapping[str, Any]], key: str, only_neg: bool = False) -> float:
        vals = [float(r[key]) for r in rows_ if r.get(key) is not None]
        return sum(min(0.0, v) for v in vals) if only_neg else sum(vals)

    def usd(rows_: Sequence[Mapping[str, Any]], key: str, only_neg: bool = False) -> float | None:
        total, seen = 0.0, 0
        for r in rows_:
            v, c = r.get(key), r.get("cost_usd")
            if v is None or c is None:
                continue
            v = min(0.0, float(v)) if only_neg else float(v)
            total += v / 100.0 * float(c)
            seen += 1
        return round(total, 2) if seen else None

    entry_pp = s(attributed, "entry_pp")
    exit_pp = s(attributed, "exit_pp")
    by_family: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for r in window:
        by_family[r["exit_family"]].append(r)
    families = []
    for fam, items in by_family.items():
        rets = [float(r["ret_pct"]) for r in items if r["ret_pct"] is not None]

        def m(key: str, items_: Sequence[Mapping[str, Any]] = items) -> float | None:
            return _r(_mean([float(r[key]) for r in items_ if r.get(key) is not None]))

        families.append({
            "exit": fam, "n": len(items), "sum_pp": _r(sum(rets)), "mean_ret_pct": _r(_mean(rets)),
            "mean_level_pct": m("level_pct"), "mean_exit_pp": m("exit_pp"), "mean_gap_pp": m("gap_pp"),
            "mean_fill_pp": m("fill_pp"), "mean_cost_pp": m("cost_pp"),
            "mean_mfe_pct": m("mfe_pct"), "pnl_usd": _r(sum(float(r["pnl_usd"]) for r in items
                                                          if r.get("pnl_usd") is not None)),
        })
    families.sort(key=lambda f: f["sum_pp"] or 0.0)

    level_losses = [r for r in attributed if r["exit_family"] in LEVEL_EXITS]
    other_losses = [r for r in attributed if r["exit_family"] not in LEVEL_EXITS]
    leaks = [
        {"leak": "entry: price path fell to the stop (the signal picked a token that dumped)",
         "side": "entry", "n": len(level_losses), "sum_pp": _r(s(level_losses, "entry_pp")),
         "usd": usd(level_losses, "entry_pp")},
        {"leak": "entry: rug/stale/other exit at the price the watchdog acted on",
         "side": "entry", "n": len(other_losses), "sum_pp": _r(s(other_losses, "entry_pp")),
         "usd": usd(other_losses, "entry_pp")},
        {"leak": "exit: price already past the stop on the mark the exit acted on (gap/detection)",
         "side": "exit", "n": sum(1 for r in level_losses if (r.get("gap_pp") or 0) < 0),
         "sum_pp": _r(s(level_losses, "gap_pp", True)), "usd": usd(level_losses, "gap_pp", True)},
        {"leak": "exit: sell filled below the mark it acted on (slippage)",
         "side": "exit", "n": sum(1 for r in attributed if (r.get("fill_pp") or 0) < 0),
         "sum_pp": _r(s(attributed, "fill_pp", True)), "usd": usd(attributed, "fill_pp", True)},
        {"leak": "exit: fees, rent, native drift and dust after the fill",
         "side": "exit", "n": sum(1 for r in attributed if (r.get("cost_pp") or 0) < 0),
         "sum_pp": _r(s(attributed, "cost_pp", True)), "usd": usd(attributed, "cost_pp", True)},
        {"leak": "exit: after the trigger where the sell legs are unpriced (fill+cost unsplit)",
         "side": "exit",
         "n": sum(1 for r in attributed if r.get("fill_pp") is None and r.get("after_trigger_pp") is not None
                  and r["after_trigger_pp"] < 0),
         "sum_pp": _r(sum(min(0.0, float(r["after_trigger_pp"])) for r in attributed
                          if r.get("fill_pp") is None and r.get("after_trigger_pp") is not None)),
         "usd": None},
    ]
    leaks = [lk for lk in leaks if lk["n"] and (lk["sum_pp"] or 0) < 0]
    leaks.sort(key=lambda lk: lk["sum_pp"] or 0.0)

    green = [r for r in losses if r.get("never_green") is not None]
    pool = [r for r in attributed if r.get("fill_vs_pool_pp") is not None]
    return {
        "since_ms": since_ms,
        "closed": len(window), "wins": len(wins), "losses": len(losses),
        "unattributed_losses": len(losses) - len(attributed),
        "mean_ret_pct": _r(_mean([float(r["ret_pct"]) for r in window if r["ret_pct"] is not None])),
        "loss_sum_pp": _r(sum(float(r["ret_pct"]) for r in losses)),
        "loss_usd": usd(losses, "ret_pct"),
        "entry_pp": _r(entry_pp), "exit_pp": _r(exit_pp),
        "entry_share": _r(entry_pp / (entry_pp + min(0.0, exit_pp)), 3) if (entry_pp + min(0.0, exit_pp)) < 0 else None,
        "labels": {k: sum(1 for r in losses if r["label"] == k) for k in ("entry", "exit", "unattributed")},
        "never_green_losses": (f"{sum(1 for r in green if r['never_green'])}/{len(green)}" if green else None),
        "by_exit": families,
        "leaks": leaks,
        "onchain": {
            "positions": len(pool),
            "mean_fill_vs_pool_pp": _r(_mean([float(r["fill_vs_pool_pp"]) for r in pool])),
            "mean_mark_vs_pool_pp": _r(_mean([float(r["mark_vs_pool_pp"]) for r in pool
                                              if r.get("mark_vs_pool_pp") is not None])),
            "mean_pool_lead_s": _r(_mean([float(r["pool_lead_s"]) for r in attributed
                                          if r.get("pool_lead_s") is not None]), 1),
        },
    }


# ============================================================================ PART B


def _robust(rows: Sequence[Mapping[str, Any]]) -> list[tuple[int, float]]:
    from kaiba.execution.lanes import _robust_prices

    return _robust_prices(list(rows))


def forward_outcome(
    before: Sequence[Mapping[str, Any]], after: Sequence[Mapping[str, Any]], *, ts_ms: int,
    horizon_s: int, barrier_pct: float, pre_window_s: int,
) -> dict[str, Any]:
    """The forward path after a decision, as a bracket at +/-``barrier_pct``.

    ``fwd_ret_pct`` is +barrier / -barrier at the first touch (``basis`` up/down), else the
    last price inside the horizon (``horizon``). ``None`` with a reason when there is no
    decision price or no print after it -- never a zero.
    """
    pre = [(t, p) for t, p in _robust(before) if ts_ms - pre_window_s * 1000 <= t <= ts_ms]
    if not pre:
        return {"fwd_ret_pct": None, "why": "no priced print in the pre-window"}
    tail = [p for _, p in pre[-5:]]
    p0 = sorted(tail)[len(tail) // 2]
    path = [(t, p) for t, p in _robust(after) if ts_ms < t <= ts_ms + horizon_s * 1000]
    if not path:
        return {"fwd_ret_pct": None, "why": "no priced print after the decision"}
    up, down = 1.0 + barrier_pct / 100.0, 1.0 - barrier_pct / 100.0
    hi = lo = 0.0
    for t, p in path:
        r = p / p0
        hi, lo = max(hi, (r - 1) * 100), min(lo, (r - 1) * 100)
        if r >= up:
            return {"fwd_ret_pct": barrier_pct, "fwd_basis": "up", "touch_s": round((t - ts_ms) / 1000.0, 1),
                    "fwd_max_pct": round(hi, 2), "fwd_min_pct": round(lo, 2)}
        if r <= down:
            return {"fwd_ret_pct": -barrier_pct, "fwd_basis": "down", "touch_s": round((t - ts_ms) / 1000.0, 1),
                    "fwd_max_pct": round(hi, 2), "fwd_min_pct": round(lo, 2)}
    last = path[-1][1]
    return {"fwd_ret_pct": round((last / p0 - 1) * 100, 4), "fwd_basis": "horizon",
            "fwd_max_pct": round(hi, 2), "fwd_min_pct": round(lo, 2)}


def _signal_payload(conn: sqlite3.Connection, decision: Mapping[str, Any],
                    cache: dict[str, Any]) -> tuple[dict[str, Any], int | None]:
    """The first point-in-time signal's ``(payload, window_s)``; ``({}, None)`` if none."""
    for sid in jload(decision.get("signals_json"), []):
        sid = str(sid)
        if sid not in cache:
            cache[sid] = fetch_one(
                conn, "SELECT payload_json, created_ms, window_s FROM signals WHERE signal_id = ?", (sid,)
            )
        row = cache[sid]
        if row is None or int(row["created_ms"]) > int(decision["ts_ms"]):
            continue
        payload = jload(row["payload_json"], {})
        return (payload if isinstance(payload, dict) else {}), _int(row.get("window_s"))
    return {}, None


def _reconstruct(conn: sqlite3.Connection, decision: Mapping[str, Any],
                 before: Sequence[Mapping[str, Any]], window_s: int) -> dict[str, Any]:
    """Point-in-time token and window-flow features, computed by the lane's own code."""
    from kaiba.core.schemas import Token
    from kaiba.execution import lanes

    chain = Chain(str(decision["chain"]))
    meta = None
    try:
        row = fetch_one(conn, "SELECT created_ms, migrated_ms, launchpad FROM tokens "
                              "WHERE chain = ? AND address = ?", (chain.value, str(decision["token"])))
        if row:
            meta = Token(address=str(decision["token"]), chain=chain, created_ms=row["created_ms"],
                         migrated_ms=row["migrated_ms"], launchpad=row["launchpad"])
    except Exception:  # noqa: BLE001 - reconstruction is best effort; missing stays None
        meta = None
    ctx = lanes.LaneContext(chain=chain, token=str(decision["token"]), now_ms=int(decision["ts_ms"]),
                            conn=None, token_meta=meta, recent_buys=list(before))
    out: dict[str, Any] = dict(lanes._window_flow(ctx, window_s))
    created = meta.created_ms if meta else None
    age = (int(decision["ts_ms"]) - int(created)) / 1000.0 if created else None
    out["token_age_s"] = round(age, 1) if age is not None and age >= 0 else None
    mig = meta.migrated_ms if meta else None
    if meta is None:
        out["migrated"] = out["since_migration_s"] = None
    elif mig is not None and int(mig) <= int(decision["ts_ms"]):
        out["migrated"], out["since_migration_s"] = 1, round((int(decision["ts_ms"]) - int(mig)) / 1000.0, 1)
    else:
        out["migrated"], out["since_migration_s"] = 0, None
    lp = str((meta.launchpad if meta else None) or "").strip().lower()
    out["launchpad"] = lp or None  # categorical, for the by-launchpad table only
    return out


def scanned_population(
    conn: sqlite3.Connection, *, lanes_: Sequence[str], since_ms: int, until_ms: int,
    params: Mapping[str, Any], deadline: float | None = None,
) -> dict[str, Any]:
    """First decision per token (skips included), its features and its forward outcome."""
    rows: list[dict[str, Any]] = []
    for lane in lanes_:
        rows.extend(fetch_all(
            conn,
            "SELECT decision_id, ts_ms, lane, mode, chain, token, action, signals_json, blockers_json "
            "FROM decisions WHERE lane = ? AND ts_ms >= ? AND ts_ms < ? ORDER BY ts_ms DESC LIMIT ?",
            (lane, int(since_ms), int(until_ms), int(params["max_decisions"])),
        ))
    rows.sort(key=lambda r: (int(r["ts_ms"]), str(r["decision_id"])))
    firsts: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        if "token_is_quote_asset" in str(r.get("blockers_json") or ""):
            continue
        firsts.setdefault((str(r["chain"]), str(r["token"])), r)
    cache: dict[str, Any] = {}
    out: list[dict[str, Any]] = []
    why: dict[str, int] = defaultdict(int)
    truncated = False
    horizon, pre_s = int(params["horizon_s"]), int(params["pre_window_s"])
    for d in sorted(firsts.values(), key=lambda r: int(r["ts_ms"])):
        if deadline is not None and time.monotonic() > deadline:
            truncated = True
            break
        ts = int(d["ts_ms"])
        if ts + horizon * 1000 > until_ms:
            why["horizon not yet elapsed"] += 1
            continue
        payload, window_s = _signal_payload(conn, d, cache)
        window_s = int(window_s or 300)
        # Read the lane's whole window only when something must be rebuilt from it; once the
        # payload records the flow features (lanes.entry_features), the decision price needs
        # the pre-window alone. MEASURED 2026-10-03 on the box: 20-340 s per run, nearly all
        # of it these swap reads under ionice idle.
        rebuild = any(k not in payload for k in RECONSTRUCTABLE)
        back_s = max(window_s, pre_s) if rebuild else pre_s
        before = list(reversed(fetch_all(
            conn,
            "SELECT ts_ms, side, wallet, usd_value, price_usd FROM swaps "
            "WHERE chain = ? AND token = ? AND ts_ms >= ? AND ts_ms <= ? ORDER BY ts_ms DESC LIMIT ?",
            (d["chain"], d["token"], ts - back_s * 1000, ts, int(params["max_swaps"])),
        )))
        after = fetch_all(
            conn,
            "SELECT ts_ms, price_usd FROM swaps WHERE chain = ? AND token = ? AND ts_ms > ? "
            "AND ts_ms <= ? ORDER BY ts_ms ASC LIMIT ?",
            (d["chain"], d["token"], ts, ts + horizon * 1000, int(params["max_swaps"])),
        )
        fwd = forward_outcome(before, after, ts_ms=ts, horizon_s=horizon,
                              barrier_pct=float(params["barrier_pct"]), pre_window_s=pre_s)
        if fwd["fwd_ret_pct"] is None:
            why[fwd["why"]] += 1
            continue
        feats: dict[str, Any] = {}
        basis: dict[str, str] = {}
        for k, v in payload.items():
            if isinstance(v, (int, float, str)) or v is None:
                feats[k], basis[k] = v, "recorded"
        missing = [k for k in RECONSTRUCTABLE if k not in feats]
        if missing:
            rebuilt = _reconstruct(conn, d, before, window_s)
            for k in missing:
                if k in rebuilt:
                    feats[k], basis[k] = rebuilt[k], "reconstructed"
        out.append({"decision_id": d["decision_id"], "ts_ms": ts, "chain": d["chain"], "lane": d["lane"],
                    "action": d["action"], "mode": d["mode"], "features": feats, "feature_basis": basis, **fwd})
    return {"rows": out, "decisions": len(rows), "tokens": len(firsts),
            "unmeasurable": dict(why), "truncated": truncated}


def _auc(pairs: Sequence[tuple[float, bool]]) -> float | None:
    """P(feature of a random winner > a random loser); ties count half. ``None`` if one class."""
    pos = [x for x, y in pairs if y]
    neg = [x for x, y in pairs if not y]
    if not pos or not neg:
        return None
    ranked = sorted(pairs, key=lambda t: t[0])
    ranks: dict[int, float] = {}
    i = 0
    while i < len(ranked):
        j = i
        while j + 1 < len(ranked) and ranked[j + 1][0] == ranked[i][0]:
            j += 1
        for k in range(i, j + 1):
            ranks[k] = (i + j) / 2.0 + 1
        i = j + 1
    rank_sum = sum(ranks[k] for k, (_, y) in enumerate(ranked) if y)
    return round((rank_sum - len(pos) * (len(pos) + 1) / 2.0) / (len(pos) * len(neg)), 4)


def _keep(value: Any, threshold: float, direction: str) -> bool:
    """The lane's rule: a configured threshold refuses an unknown."""
    v = _dec(value)
    if v is None:
        return False
    return float(v) >= threshold if direction == "ge" else float(v) <= threshold


def _split_eval(rows: Sequence[Mapping[str, Any]], feat: str, threshold: float, direction: str) -> dict[str, Any]:
    kept = [float(r["fwd_ret_pct"]) for r in rows if _keep(r["features"].get(feat), threshold, direction)]
    allv = [float(r["fwd_ret_pct"]) for r in rows]
    if not allv:
        return {"n": 0, "kept": 0, "keep_share": None, "delta_pp": None}
    base = sum(allv) / len(allv)
    return {
        "n": len(allv), "kept": len(kept), "keep_share": round(len(kept) / len(allv), 3),
        "mean_all": round(base, 3), "mean_kept": round(sum(kept) / len(kept), 3) if kept else None,
        "delta_pp": round(sum(kept) / len(kept) - base, 3) if kept else None,
    }


def rank_features(rows: Sequence[Mapping[str, Any]], features: Sequence[str], *,
                  min_n: int, min_keep_share: float) -> list[dict[str, Any]]:
    """Choose a threshold per feature on the OLDER half, score it on the NEWER half."""
    rows = sorted(rows, key=lambda r: int(r["ts_ms"]))
    half = len(rows) // 2
    older, newer = rows[:half], rows[half:]
    out: list[dict[str, Any]] = []
    for feat in features:
        vals = [(float(_dec(r["features"].get(feat))), float(r["fwd_ret_pct"]) > 0)
                for r in rows if _dec(r["features"].get(feat)) is not None]
        n_feat = len(vals)
        recorded = sum(1 for r in rows if r.get("feature_basis", {}).get(feat) == "recorded")
        entry: dict[str, Any] = {
            "feature": feat, "n": len(rows), "with_feature": n_feat,
            "coverage": round(n_feat / len(rows), 3) if rows else None,
            "recorded": recorded, "basis": ("recorded" if recorded and recorded >= n_feat / 2
                                            else "reconstructed" if n_feat else "none"),
            "auc": _auc(vals) if n_feat >= min_n else None,
        }
        if n_feat < min_n or not older or not newer:
            entry["verdict"] = f"too few: {n_feat} rows carry it (need {min_n})"
            out.append(entry)
            continue
        older_vals = sorted(float(_dec(r["features"][feat])) for r in older
                            if _dec(r["features"].get(feat)) is not None)
        if len(older_vals) < max(10, min_n // 2):
            entry["verdict"] = f"too few in the older half: {len(older_vals)}"
            out.append(entry)
            continue
        cands = sorted({older_vals[min(len(older_vals) - 1, int(q / 10 * len(older_vals)))]
                        for q in range(1, 10)})
        best = None
        for direction in ("ge", "le"):
            for t in cands:
                ev = _split_eval(older, feat, t, direction)
                if (ev["keep_share"] is None or ev["keep_share"] < min_keep_share
                        or ev["kept"] < 10 or ev["delta_pp"] is None):
                    continue
                if best is None or ev["delta_pp"] > best[2]["delta_pp"]:
                    best = (direction, t, ev)
        if best is None:
            entry["verdict"] = f"no threshold keeps >= {min_keep_share:.0%} of the older half"
            out.append(entry)
            continue
        direction, t, ev_old = best
        ev_new = _split_eval(newer, feat, t, direction)
        improves = (ev_old["delta_pp"] or 0) > 0 and (ev_new["delta_pp"] or 0) > 0
        # The keep share must hold on the NEWER half too: a threshold chosen to keep half of
        # last month that keeps a fifth of this week is a kill switch on today's flow (the
        # gates' removal ceiling would refuse it, and the owner directive is why).
        keeps = (ev_new["keep_share"] or 0) >= min_keep_share
        holds = improves and keeps
        if holds:
            verdict = "separates: holds on the newer half"
        elif improves:
            verdict = (f"improves the newer half but keeps only {ev_new['keep_share']:.0%} of it "
                       f"(< {min_keep_share:.0%}): the feature's distribution moved")
        else:
            verdict = "does not hold out: the newer half does not improve"
        entry.update({
            "key": ("min_" if direction == "ge" else "max_") + feat,
            "threshold": round(t, 6), "direction": direction,
            "older": ev_old, "newer": ev_new, "verdict": verdict, "holds_out": holds,
        })
        out.append(entry)
    out.sort(key=lambda e: (not e.get("holds_out", False),
                            -((e.get("newer") or {}).get("delta_pp") or -1e9)))
    return out


def gate_readiness(conn: sqlite3.Connection, lane: str, features: Sequence[str], *,
                   limit: int = 5000) -> dict[str, dict[str, Any]]:
    """Per feature: closed-trade entry decisions whose payload RECORDED it, and whether the
    newest 30% of those span the >= 20 UTC days the replay's relative bound needs."""
    from kaiba.learning.gates import DEFAULT_OOS_FRACTION, MIN_CLUSTER_DAYS

    rows = fetch_all(
        conn,
        "SELECT d.ts_ms, d.signals_json FROM decisions d JOIN trades t ON t.decision_id = d.decision_id "
        "WHERE d.lane = ? AND d.action IN ('enter', 'scale_in') ORDER BY d.ts_ms DESC LIMIT ?",
        (lane, int(limit)),
    )
    cache: dict[str, Any] = {}
    stamps: dict[str, list[int]] = defaultdict(list)
    for r in rows:
        payload, _ = _signal_payload(conn, r, cache)
        for f in features:
            if f in payload:
                stamps[f].append(int(r["ts_ms"]))
    out: dict[str, dict[str, Any]] = {}
    for f in features:
        ts = sorted(stamps.get(f, []))
        cut = max(1, int(len(ts) * (1 - DEFAULT_OOS_FRACTION))) if ts else 0
        oos_days = len({t // DAY_MS for t in ts[cut:]}) if ts else 0
        out[f] = {"recorded_trades": len(ts), "days": len({t // DAY_MS for t in ts}),
                  "oos_days": oos_days,
                  "ready": len(ts) >= 30 and oos_days >= MIN_CLUSTER_DAYS}
    return out


def _numeric_features(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """Every numeric feature the lanes record or the tape rebuilds -- not their thresholds."""
    from kaiba.execution.lanes import ENTRY_FEATURES

    extra = ("smart_wallets", "entity_count", "address_count", "proven_wallets", "newest_buy_age_s")
    names = list(dict.fromkeys(ENTRY_FEATURES + extra))
    present = set()
    for r in rows:
        present.update(k for k, v in r["features"].items() if _dec(v) is not None)
    return [n for n in names if n in present or n in ENTRY_FEATURES]


def _launchpad_table(rows: Sequence[Mapping[str, Any]], top: int = 8) -> list[dict[str, Any]]:
    groups: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        lp = r["features"].get("launchpad")
        if lp:
            groups[str(lp)].append(float(r["fwd_ret_pct"]))
    table = [{"launchpad": k, "n": len(v), "mean_fwd_pct": round(sum(v) / len(v), 2)}
             for k, v in groups.items() if len(v) >= 5]
    table.sort(key=lambda e: -e["n"])
    return table[:top]


# ============================================================================ the run


def build(conn: sqlite3.Connection, *, now: int | None = None,
          params: Mapping[str, Any] | None = None, deadline: float | None = None) -> dict[str, Any]:
    """Both parts as data. Reads only; :func:`persist` writes."""
    p = _params(params)
    until = int(now if now is not None else now_ms())
    started = time.monotonic()
    lanes_ = [x.strip() for x in str(p["lanes"]).split(",") if x.strip()]

    positions = closed_positions(conn, since_ms=until - int(p["trade_days"]) * DAY_MS, until_ms=until,
                                 lanes=lanes_)
    attributed = [attribute_position(conn, pos, never_green_pct=float(p["never_green_pct"]))
                  for pos in positions]
    summary = {
        f"{int(p['summary_days'])}d": summarise_trades(attributed, since_ms=until - int(p["summary_days"]) * DAY_MS),
        f"{int(p['trade_days'])}d": summarise_trades(attributed, since_ms=until - int(p["trade_days"]) * DAY_MS),
    }
    by_chain = {}
    for ch in sorted({str(r["chain"]) for r in attributed}):
        s = summarise_trades([r for r in attributed if r["chain"] == ch],
                             since_ms=until - int(p["trade_days"]) * DAY_MS)
        by_chain[ch] = {k: s[k] for k in ("closed", "losses", "mean_ret_pct", "loss_sum_pp", "loss_usd",
                                          "entry_pp", "exit_pp", "entry_share", "labels")}
        by_chain[ch]["top_leak"] = s["leaks"][0] if s["leaks"] else None

    scan = scanned_population(conn, lanes_=lanes_, since_ms=until - int(p["scan_days"]) * DAY_MS,
                              until_ms=until, params=p, deadline=deadline)
    feats = _numeric_features(scan["rows"])
    ranked = rank_features(scan["rows"], feats, min_n=int(p["min_feature_n"]),
                           min_keep_share=float(p["min_keep_share"]))
    ready = gate_readiness(conn, lanes_[0], feats) if lanes_ else {}
    for e in ranked:
        e["gate"] = ready.get(e["feature"])

    fwd = [float(r["fwd_ret_pct"]) for r in scan["rows"]]
    traded = [r for r in scan["rows"] if r["action"] in ("enter", "scale_in")]
    out = {
        "version": VERSION,
        "generated_ms": until,
        "params": {k: p[k] for k in ("trade_days", "summary_days", "scan_days", "lanes", "horizon_s",
                                     "barrier_pct", "min_keep_share")},
        "trades": summary,
        "trades_by_chain": by_chain,
        "scanned": {
            "decisions": scan["decisions"], "tokens": scan["tokens"], "measured": len(scan["rows"]),
            "unmeasurable": scan["unmeasurable"], "truncated": scan["truncated"],
            "mean_fwd_pct": _r(_mean(fwd)), "share_down_first": _r(
                sum(1 for r in scan["rows"] if r.get("fwd_basis") == "down") / len(fwd), 3) if fwd else None,
            "entered": len(traded), "mean_fwd_entered_pct": _r(_mean([float(r["fwd_ret_pct"]) for r in traded])),
            "outcome": (f"bracket +/-{p['barrier_pct']}% within {int(p['horizon_s']) // 60} min of the decision "
                        "on the swap tape, else the last print in the horizon; first decision per token"),
        },
        "features": [e for e in ranked if e.get("holds_out")][: int(p["top_features"])],
        "features_not_holding": [
            {k: e.get(k) for k in ("feature", "with_feature", "coverage", "basis", "verdict", "key",
                                   "threshold")}
            for e in ranked if not e.get("holds_out")
        ],
        "launchpads": _launchpad_table(scan["rows"]),
        "elapsed_s": round(time.monotonic() - started, 1),
        "notes": [
            "proposes nothing: the daily learning loop proposes, kaiba.learning.gates judges",
            "dossier features (holders, liquidity, concentration...) are ranked only from RECORDED "
            "payloads: token_dossiers keeps the newest dossier, so history cannot be rebuilt",
            "a recorded-unknown feature counts as REFUSED by a threshold, as the lane refuses it live",
        ],
    }
    out["_rows"] = attributed  # for persist(); stripped before the kv write
    return out


def persist(conn: sqlite3.Connection, report: dict[str, Any]) -> dict[str, Any]:
    """Upsert the per-position rows and the kv summary. The only writes this module makes."""
    rows = report.get("_rows") or []
    stamp = int(report["generated_ms"])
    with tx(conn) as c:
        ensure_table(c)
        cols = ["position_id", "trade_id", "decision_id", "lane", "chain", "token", "opened_ms",
                "closed_ms", "exit_reason", "exit_family", "ret_pct", "level_pct", "entry_pp",
                "exit_pp", "cushion_pp", "gap_pp", "fill_pp", "cost_pp", "after_trigger_pp",
                "mfe_pct", "mae_pct", "excursion_basis", "marks", "time_to_level_s",
                "exit_latency_s", "hold_s", "pool_ret_at_trigger_pct", "fill_vs_pool_pp",
                "mark_vs_pool_pp", "pool_lead_s", "cost_usd", "pnl_usd", "label", "never_green"]
        c.executemany(
            f"INSERT OR REPLACE INTO {TABLE} ({', '.join(cols)}, notes_json, computed_ms, version) "
            f"VALUES ({', '.join('?' * (len(cols) + 3))})",
            [tuple(r.get(k) for k in cols) + (jdump(r.get("notes") or []), stamp, VERSION) for r in rows],
        )
        summary = {k: v for k, v in report.items() if not k.startswith("_")}
        c.execute(
            "INSERT INTO kv (key, value, updated_ms) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_ms = excluded.updated_ms",
            (KV_KEY, jdump(summary), stamp),
        )
    return {"positions": len(rows), "kv": KV_KEY}


def run(conn: sqlite3.Connection, *, now: int | None = None, params: Mapping[str, Any] | None = None,
        deadline_monotonic: float | None = None) -> dict[str, Any]:
    """The scheduled job: build, persist, return a small result for ops_runs."""
    report = build(conn, now=now, params=params, deadline=deadline_monotonic)
    written = persist(conn, report)
    window = f"{int(_params(params)['summary_days'])}d"
    head = report["trades"].get(window, {})
    top = report["features"][0] if report["features"] else None
    return {
        "written": written, "elapsed_s": report["elapsed_s"], "window": window,
        "losses": head.get("losses"), "entry_share": head.get("entry_share"),
        "top_leak": (head.get("leaks") or [{}])[0].get("leak") if head.get("leaks") else None,
        "top_feature": (f"{top['key']} {top['threshold']}" if top else None),
        "scanned_measured": report["scanned"]["measured"], "truncated": report["scanned"]["truncated"],
    }


# ============================================================================ read side


def summary(conn: sqlite3.Connection, *, now: int | None = None) -> dict[str, Any]:
    """The newest stored report (SELECT only: safe on the daily report's read-only handle)."""
    try:
        row = fetch_one(conn, "SELECT value, updated_ms FROM kv WHERE key = ?", (KV_KEY,))
    except sqlite3.Error as exc:
        return {"error": f"kv unreadable: {exc}"}
    if not row:
        return {"error": "loss_attribution has not run yet (no kv row)"}
    data = jload(row["value"], {})
    if not isinstance(data, dict) or not data:
        return {"error": "kv row unreadable"}
    data["age_s"] = round(((now if now is not None else now_ms()) - int(row["updated_ms"])) / 1000.0, 1)
    return data


def _money(pp: Any, usd: Any) -> str:
    if pp is None:
        return "n/a"
    return f"{float(pp):+.0f}pp" + (f" (${float(usd):+,.0f})" if usd is not None else "")


def render_lines(data: Mapping[str, Any], *, window: str | None = None) -> list[str]:
    """The daily report's LOSSES section: the split, the top leak, the top separating feature."""
    if "error" in data:
        return [f"LOSSES cannot measure: {data['error']}"]
    trades = data.get("trades") or {}
    window = window or next(iter(trades), None)
    head = trades.get(window) if window else None
    age = data.get("age_s")
    age_txt = f", {age / 3600:.1f}h old" if age is not None else ""
    if not head:
        return [f"LOSSES cannot measure: no trade window in the report{age_txt}"]
    lines = [
        f"LOSSES {window} live (loss_attribution{age_txt}): {head['losses']} of {head['closed']} closed lost "
        f"{_money(head['loss_sum_pp'], head.get('loss_usd'))} | ENTRY {_money(head['entry_pp'], None)} "
        f"| EXIT {_money(head['exit_pp'], None)}"
        + (f" | entry share {head['entry_share']:.0%}" if head.get("entry_share") is not None else "")
        + (f" | never green {head['never_green_losses']}" if head.get("never_green_losses") else "")
    ]
    leaks = head.get("leaks") or []
    if leaks:
        lk = leaks[0]
        lines.append(f"  top leak: {lk['leak']}: {lk['n']}x {_money(lk['sum_pp'], lk.get('usd'))}")
    for fam in (head.get("by_exit") or [])[:2]:
        parts = [f"{fam['exit']} {fam['n']}x mean {fam['mean_ret_pct']:+.1f}%"
                 if fam.get("mean_ret_pct") is not None else f"{fam['exit']} {fam['n']}x"]
        if fam.get("mean_level_pct") is not None:
            parts.append(f"level {fam['mean_level_pct']:+.1f}%")
        for k, lbl in (("mean_gap_pp", "gap"), ("mean_fill_pp", "fill"), ("mean_cost_pp", "cost")):
            if fam.get(k) is not None:
                parts.append(f"{lbl} {fam[k]:+.1f}pp")
        lines.append("  " + " | ".join(parts))
    feats = data.get("features") or []
    scanned = data.get("scanned") or {}
    if feats:
        f = feats[0]
        new = f.get("newer") or {}
        old = f.get("older") or {}
        gate = f.get("gate") or {}
        lines.append(
            f"  top separating feature (scanned, chosen on older half, scored on newer): "
            f"{f['key']} {f['threshold']:g} keeps {new.get('keep_share', 0):.0%} of n={new.get('n')} | "
            f"fwd {new.get('delta_pp', 0):+.1f}pp vs all (older {old.get('delta_pp', 0):+.1f}pp) | "
            f"{f['basis']} | gate {'READY' if gate.get('ready') else 'not ready'} "
            f"({gate.get('recorded_trades', 0)} recorded trades, {gate.get('oos_days', 0)} OOS days)"
        )
    else:
        lines.append(f"  no entry feature held out on the scanned population "
                     f"(measured {scanned.get('measured', 0)} tokens)")
    return lines


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI
    from pathlib import Path

    from kaiba.core.config import get_settings
    from kaiba.core.db import connect

    parser = argparse.ArgumentParser(prog="python -m kaiba.learning.loss_attribution")
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--write", action="store_true", help="persist (default: read-only)")
    args = parser.parse_args(argv)
    path = args.db or get_settings().db_path
    if args.write:
        conn = connect(path)
    else:
        conn = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=1")
    report = build(conn)
    if args.write:
        persist(conn, report)
    report.pop("_rows", None)
    if args.json:
        print(json.dumps(report, indent=1, default=str))
    else:
        stamp = datetime.fromtimestamp(report["generated_ms"] / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
        print(f"loss attribution {stamp}")
        for line in render_lines(report):
            print(line)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
