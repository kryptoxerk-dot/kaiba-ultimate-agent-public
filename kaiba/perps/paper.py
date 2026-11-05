"""Forward PAPER book for the frozen D1 breakout on Binance USD-M perps (PERPS-D1-PAPER-CLAUDE-20261103).

Pre-registered in docs/research/claude-perps-d1-forward-prereg-20261103.md before any forward outcome.
Every run re-derives the whole forward ledger from closed daily bars and real funding (no state that
can drift): signals count only on bars that CLOSE at or after :data:`REGISTERED_MS`, every symbol
starts flat there, fills are the next day's open, exits follow the frozen rules. Paper only: public
market data, no key, no order, no account, no funds.

    python -m kaiba.perps.paper run      # fetch, re-derive, write data/perps/report.json
"""
from __future__ import annotations

import json
import os
import random
import statistics as st
import sys
import time
from pathlib import Path
from typing import Any

from kaiba.perps import binance_public as bp
from kaiba.perps.d1_breakout import COST_BPS, DAY_MS, PARAMS, Trade, funding_paid, replay

#: 2026-11-03 12:00 UTC, fixed in the pre-registration: the first signal that counts is on the bar of
#: 2026-11-03, which closes at 2026-11-04 00:00 UTC. Never moved.
REGISTERED_MS = 1_793_707_200_000
ARMS = {
    "primary": ("BTCUSDT", "ETHUSDT"),  # the pair the parameters were chosen on
    "secondary": ("SOLUSDT", "BNBUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "XRPUSDT", "DOGEUSDT", "LTCUSDT"),
}
WARMUP_DAYS = 260          # 200-day SMA + 20-day channel + ATR, with margin
MIN_N, HORIZON_DAYS, BOOT, SEED = 40, 365, 2000, 20261103
VERSION = "perps-d1-paper/1"


def data_dir() -> Path:
    from kaiba.core.config import get_settings

    return Path(get_settings().kaiba_data_dir) / "perps"


def unrealized_r(pos, last_close: float, fund_t, fund_r, now_ms: float) -> float:
    """Mark-to-market in R at the latest close, net of both sides' fees and the funding so far."""
    dist = abs(pos.entry - pos.stop)
    gross = (last_close - pos.entry) * pos.side
    cost = 2 * COST_BPS / 1e4 * pos.entry + funding_paid(fund_t, fund_r, pos.entry_t, now_ms, pos.side) * pos.entry
    return (gross - cost) / dist


def summarize(trades: list[Trade]) -> dict[str, Any]:
    rs = [t.r for t in trades]
    if not rs:
        return {"n": 0}
    return {"n": len(rs), "mean_r": round(st.fmean(rs), 4), "median_r": round(st.median(rs), 4),
            "sum_r": round(sum(rs), 4), "win": round(sum(r > 0 for r in rs) / len(rs), 3)}


def verdict(trades: list[Trade], now_ms: float) -> dict[str, Any]:
    """The pre-registered pass line, on the POOLED forward record (all ten symbols)."""
    n = len(trades)
    out: dict[str, Any] = {"n": n, "min_n": MIN_N}
    if n < MIN_N:
        days = (now_ms - REGISTERED_MS) / DAY_MS
        out["verdict"] = "INCONCLUSIVE" if days >= HORIZON_DAYS else "PENDING"
        return out
    ordered = sorted(trades, key=lambda t: t.entry_t)
    half = n // 2
    h1, h2 = st.fmean(t.r for t in ordered[:half]), st.fmean(t.r for t in ordered[half:])
    by_sym: dict[str, list[float]] = {}
    for t in trades:
        by_sym.setdefault(t.symbol, []).append(t.r)
    syms = sorted(by_sym)
    rng = random.Random(SEED)
    means = []
    for _ in range(BOOT):
        xs = [x for s in (rng.choice(syms) for _ in syms) for x in by_sym[s]]
        means.append(st.fmean(xs))
    lb = sorted(means)[int(0.10 * BOOT)]
    mean = st.fmean(t.r for t in trades)
    passed = mean > 0 and h1 > 0 and h2 > 0 and lb > 0
    out.update(mean_r=round(mean, 4), first_half_r=round(h1, 4), second_half_r=round(h2, 4),
               symbol_clustered_lb90_r=round(lb, 4), verdict="PASS" if passed else "FAIL")
    return out


def _trade_row(t: Trade) -> dict[str, Any]:
    return {"symbol": t.symbol, "side": "long" if t.side > 0 else "short", "entry_at": _iso(t.entry_t),
            "exit_at": _iso(t.exit_t), "entry": t.entry, "exit": t.exit, "stop": t.stop, "r": round(t.r, 4),
            "cost_r": round(t.cost_r, 4), "reason": t.reason}


def _iso(ms: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ms / 1000))


def run(*, now_ms: int | None = None, http: Any = None, out_dir: Path | None = None) -> dict[str, Any]:
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    out_dir = data_dir() if out_dir is None else Path(out_dir)
    days_since = max(0, int((now_ms - REGISTERED_MS) // DAY_MS))
    limit = min(1000, WARMUP_DAYS + days_since + 2)
    own = http is None
    if own:
        import httpx

        http = httpx.Client(headers={"User-Agent": "kaiba-perps-paper/1 (public market data)"})
    trades: list[Trade] = []
    opened, symbols = [], {}
    try:
        for arm, syms in ARMS.items():
            for sym in syms:
                try:
                    bars, next_open = bp.daily(sym, limit, http, now_ms)
                    ft, fr = bp.funding(sym, REGISTERED_MS - 2 * DAY_MS, http, now_ms)
                    done, pos = replay(sym, bars, ft, fr, first_signal_close_ms=REGISTERED_MS, next_open=next_open)
                except Exception as exc:  # noqa: BLE001 - one symbol's data must not hide the others
                    symbols[sym] = {"arm": arm, "status": "unavailable", "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
                    continue
                trades += done
                symbols[sym] = {"arm": arm, "status": "ok", "last_closed_bar": _iso(bars[-1].t) if bars else None,
                                "bars": len(bars), "funding_prints": len(ft)}
                if pos is not None:
                    opened.append({"symbol": sym, "arm": arm, "side": "long" if pos.side > 0 else "short",
                                   "entry_at": _iso(pos.entry_t), "entry": pos.entry, "stop": pos.stop,
                                   "target": pos.target, "bars_held": pos.bars_held,
                                   "last_close": bars[-1].c if bars else None,
                                   "unrealized_r": round(unrealized_r(pos, bars[-1].c, ft, fr, now_ms), 4)
                                   if bars and pos.bars_held else None})
    finally:
        if own:
            http.close()
    by_arm = {arm: summarize([t for t in trades if t.symbol in syms]) for arm, syms in ARMS.items()}
    report = {
        "version": VERSION, "as_of_ms": now_ms, "as_of": _iso(now_ms), "registered_at": _iso(REGISTERED_MS),
        "strategy": "d1_breakout_target (frozen, AEGIS OMNI research/prop_perps_2026_10)", "params": PARAMS,
        "mode": "paper", "venue": "Binance USD-M perpetuals, public market data only",
        "arms": by_arm, "pooled": summarize(trades), "verdict": verdict(trades, now_ms),
        "open_positions": opened, "closed_trades": [_trade_row(t) for t in sorted(trades, key=lambda t: -t.exit_t)][:100],
        "symbols": symbols,
        "notes": ["Paper only: no API key, no order, no account. Fills are the next daily open; stops and targets are "
                  "checked on daily highs and lows, stop first when both are touched.",
                  "Results are in R (the stop distance), net of 7 bps a side and the real funding. They are not "
                  "a position size or a forecast."],
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / ".report.json.tmp"
    tmp.write_text(json.dumps(report, sort_keys=True, allow_nan=False), encoding="utf-8")
    os.replace(tmp, out_dir / "report.json")
    return report


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] != ["run"]:
        print("usage: python -m kaiba.perps.paper run")
        return 2
    rep = run()
    unavailable = [s for s, v in rep["symbols"].items() if v.get("status") != "ok"]
    print(json.dumps({"as_of": rep["as_of"], "pooled": rep["pooled"], "verdict": rep["verdict"]["verdict"],
                      "open": len(rep["open_positions"]), "unavailable": unavailable}, sort_keys=True))
    return 0 if not unavailable else 1


if __name__ == "__main__":
    raise SystemExit(main())
