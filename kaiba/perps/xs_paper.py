"""Forward PAPER books for weekly cross-sectional momentum and funding carry (PERPS-XS-FORWARD-CLAUDE-20261103).

Pre-registered in docs/research/claude-xs-forward-prereg-20261103.md before any forward outcome; the
rules are the archive tests' (claude-xsmom-prereg / claude-xscarry-prereg), unchanged:

* every Monday 00:00 UTC, the top 60 USDT perpetuals by median daily quote volume over the 30 prior
  days, among those with 60 gap-free prior days and a bar opening that Monday;
* momentum: 14-day return to Saturday's close (Sunday unused); long the top fifth, short the bottom;
* carry: funding printed in (m - 8 d, m - 1 d]; long the lowest third, short the highest;
* equal weights, each leg 1.0 of capital; filled at Monday's open, closed at the next Monday's open
  (or the last recorded close if a symbol stops trading); 5 bps a side on target-weight changes plus
  the real funding paid or received.

Public market data only: no key, no account, no order. Unlike the D1 ledger, a cross-section cannot
be re-derived later (delisted symbols vanish from the API), so formations are appended to a ledger
when they are made and are never rewritten. A formation that was not made before Monday + 1 day is
recorded as missed, never backfilled.

    python -m kaiba.perps.xs_paper run
"""
from __future__ import annotations

import json
import math
import os
import statistics as st
import sys
import time
from pathlib import Path
from typing import Any

DAY_MS = 86_400_000
WEEK_MS = 7 * DAY_MS
#: Monday 2026-11-09 00:00 UTC, fixed in the pre-registration: the first forward formation.
FIRST_MONDAY_MS = 1_794_182_400_000
TOP_N, HIST_DAYS, VOL_DAYS, LOOK_FROM, LOOK_TO = 60, 60, 30, 16, 2
BPS = 5.0
FORM_DEADLINE_MS = DAY_MS
KLINE_LIMIT = 70
FAPI = "https://fapi.binance.com"
VERSION = "perps-xs-paper/1"
STABLE = ("USDC", "BUSD", "TUSD", "FDUSD", "USDP", "DAI", "EUR", "GBP", "AEUR", "USDE", "USD1", "RLUSD")
INDEX = {"BTCDOMUSDT", "DEFIUSDT", "FOOTBALLUSDT", "BLUEBIRDUSDT"}
BOOKS = ("momentum", "carry")


def in_universe(sym: str) -> bool:
    """The archive tests' universe rule (P2 majors included)."""
    if not sym.endswith("USDT") or "_" in sym or sym in INDEX:
        return False
    return sym not in {s + "USDT" for s in STABLE}


def monday_of(ms: int) -> int:
    """The most recent Monday 00:00 UTC at or before ``ms`` (1970-01-05 was a Monday)."""
    base = 4 * DAY_MS
    return base + ((ms - base) // WEEK_MS) * WEEK_MS


# ------------------------------------------------------------------ pure rules (tested)

def eligible(bars: dict[int, tuple[float, float, float]], m: int) -> tuple[float, float] | None:
    """``(liquidity, momentum signal)`` for Monday ``m`` or None. ``bars``: open ms -> (open, close, quote vol)."""
    if m not in bars or any((m - d * DAY_MS) not in bars for d in range(1, HIST_DAYS + 1)):
        return None
    vol = st.median(bars[m - d * DAY_MS][2] for d in range(1, VOL_DAYS + 1))
    sig = bars[m - LOOK_TO * DAY_MS][1] / bars[m - LOOK_FROM * DAY_MS][1] - 1
    return vol, sig


def funding_sum(prints: list[tuple], t0: int, t1: int) -> float:
    """Sum of rates printed in (t0, t1]; positive = longs pay. Used for the carry SIGNAL only."""
    return math.fsum(p[1] for p in prints if t0 < p[0] <= t1)


def mark_proxy(bars: dict[int, tuple[float, float, float]], t: int) -> float | None:
    """Daily-bar proxy for the mark at funding time ``t``: the day's open at a day boundary, else the
    mean of its open and close; the last earlier close when the day is missing or still open."""
    day = t - t % DAY_MS
    if day in bars:
        o, c, _ = bars[day]
        if t == day:
            return o
        if math.isfinite(c):
            return (o + c) / 2
    prev = [x for x in bars if x < day and math.isfinite(bars[x][1])]
    return bars[max(prev)][1] if prev else None


def funding_owed(prints: list[tuple], t0: int, t1: int, entry: float,
                 bars: dict[int, tuple[float, float, float]] | None) -> float:
    """Funding a fixed-quantity long owes over (t0, t1] as a fraction of ENTRY notional: each print
    is charged on quantity x mark at that time (rate x mark / entry), not on the entry notional.

    The mark is the print's own ``markPrice`` when Binance returned one (amendment F1, before the
    first formation); the daily-bar proxy otherwise."""
    out = []
    for p in prints:
        t, r = p[0], p[1]
        if t0 < t <= t1:
            mark = p[2] if len(p) > 2 and p[2] else None
            px = mark or (mark_proxy(bars, t) if bars else None)
            out.append(r * (px / entry if px else 1.0))
    return math.fsum(out)


def drifted(w: dict[str, float], rets: dict[str, float], net: float) -> dict[str, float]:
    """Weights after a week: each position moves with its return, the NAV with the book's net."""
    return {s: w[s] * (1 + rets[s]) / (1 + net) for s in w if s in rets}


def form(m: int, bars: dict[str, dict], funding: dict[str, list[tuple[int, float]]]) -> dict[str, Any]:
    """Both books' legs for Monday ``m`` from the same top-60 liquid set."""
    cand = []
    for sym, b in bars.items():
        e = eligible(b, m)
        if e:
            cand.append((sym, e[0], e[1]))
    cand.sort(key=lambda x: (-x[1], x[0]))
    cand = cand[:TOP_N]
    out: dict[str, Any] = {"m": m, "n": len(cand), "universe": sorted(c[0] for c in cand)}
    mom = sorted(cand, key=lambda x: (x[2], x[0]))
    k = len(mom) // 5
    out["momentum"] = {"longs": [c[0] for c in mom[-k:]] if k else [], "shorts": [c[0] for c in mom[:k]]}
    car = sorted(((c[0], funding_sum(funding.get(c[0], []), m - 8 * DAY_MS, m - DAY_MS)) for c in cand
                  if c[0] in funding), key=lambda x: (x[1], x[0]))
    k3 = len(car) // 3
    out["carry"] = {"longs": [c[0] for c in car[:k3]], "shorts": [c[0] for c in car[-k3:]] if k3 else []}
    for book in BOOKS:
        legs = out[book]
        legs["entry"] = {s: bars[s][m][0] for s in legs["longs"] + legs["shorts"]}
    return out


def weights(legs: dict[str, Any]) -> dict[str, float]:
    if not legs["longs"] or not legs["shorts"]:
        return {}
    return {s: 1 / len(legs["longs"]) for s in legs["longs"]} | {s: -1 / len(legs["shorts"]) for s in legs["shorts"]}


def turnover(new: dict[str, float], old: dict[str, float]) -> float:
    return math.fsum(abs(new.get(s, 0.0) - old.get(s, 0.0)) for s in set(new) | set(old))


def settle(legs: dict[str, Any], m: int, exit_open: dict[str, float], last_close: dict[str, tuple[int, float]],
           funding: dict[str, list[tuple[int, float]]], turn: float,
           bars: dict[str, dict] | None = None) -> dict[str, Any]:
    """One book's week from Monday ``m`` to ``m + 7 d``: exit at the next open, or at the last recorded
    close (``last_close`` holds only the symbols allowed to use one) if the symbol stopped trading in
    the week. ``unsettled`` when neither exists yet."""
    rows, rets = {}, {}
    for side, syms in (("long", legs["longs"]), ("short", legs["shorts"])):
        rs, fs = [], []
        for s in syms:
            entry = legs["entry"][s]
            if s in exit_open:
                px, t1, how = exit_open[s], m + WEEK_MS, "open"
            elif s in last_close and last_close[s][0] >= m:
                (t_close, px), how = last_close[s], "last_close"
                t1 = t_close + DAY_MS
            else:
                return {"m": m, "status": "unsettled", "missing": s}
            rs.append(px / entry - 1)
            rets[s] = px / entry - 1
            fs.append(funding_owed(funding.get(s, []), m, t1, entry, (bars or {}).get(s)))
            charged = [p for p in funding.get(s, []) if m < p[0] <= t1]
            rows[s] = {"exit": px, "how": how, "funding_prints": len(charged),
                       "funding_on_print_mark": sum(1 for p in charged if len(p) > 2 and p[2])}
        if side == "long":
            r_long, f_long = st.fmean(rs), st.fmean(fs)
        else:
            r_short, f_short = st.fmean(rs), st.fmean(fs)
    gross = r_long - r_short
    fund = -f_long + f_short
    cost = BPS / 1e4 * turn
    return {"m": m, "status": "settled", "gross": gross, "funding": fund, "turnover": turn, "cost": cost,
            "net": gross + fund - cost, "exits": rows, "rets": rets}


# ------------------------------------------------------------------ public data

def _get(http: Any, path: str, params: dict) -> Any:
    r = http.get(FAPI + path, params=params, timeout=15.0)
    r.raise_for_status()
    return r.json()


def trading_symbols(http: Any) -> list[str]:
    return symbols_from(_get(http, "/fapi/v1/exchangeInfo", {}))


def symbols_from(info: dict) -> list[str]:
    return sorted(s["symbol"] for s in info.get("symbols", [])
                  if s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT"
                  and s.get("status") == "TRADING" and in_universe(s["symbol"]))


ORDER_FILTERS = ("LOT_SIZE", "MARKET_LOT_SIZE", "MIN_NOTIONAL")


def order_filters_snapshot(info: dict, symbols: list[str], *, m: int, run_now_ms: int,
                           received_wall_ms: int) -> dict:
    """The order filters of ``symbols`` from the exchangeInfo this run already fetched, for the
    read-only order-feasibility companion (Codex's ``order_constraints``). Raw strings, no
    interpretation; a symbol or filter the response lacks is recorded as missing, never invented.
    It is NOT part of the ledger and changes no formation rule."""
    by: dict[Any, list[dict]] = {}
    for s in info.get("symbols", []):
        by.setdefault(s.get("symbol"), []).append(s)
    rows = {}
    for sym in sorted(set(symbols)):
        matches = by.get(sym, [])
        if not matches:
            rows[sym] = {"missing": True, "raw_symbols": []}
            continue
        # F3: every raw object the response carried for this symbol, untouched (filters stay a LIST).
        # The summary below reads the first only, and says so when there was more than one.
        row = matches[0]
        raw_filters = row.get("filters", []) if isinstance(row.get("filters"), list) else []
        types = [f.get("filterType") for f in raw_filters]
        filters = {f.get("filterType"): {k: v for k, v in f.items() if k != "filterType"}
                   for f in raw_filters if f.get("filterType") in ORDER_FILTERS}
        rows[sym] = {"status": row.get("status"), "filters": filters,
                     "missing_filters": [k for k in ORDER_FILTERS if k not in filters],
                     "duplicate_symbol_objects": len(matches) - 1,
                     "duplicate_filter_types": sorted({t for t in types if types.count(t) > 1}, key=str),
                     "raw_symbols": matches}
    return {"schema": "kaiba/perps-xs-order-filters/2", "m": m, "run_now_ms": run_now_ms,
            "received_wall_ms": received_wall_ms, "server_time_ms": info.get("serverTime"),
            "source": "GET /fapi/v1/exchangeInfo (the formation run's own call; no extra request)",
            "symbols": rows}


def daily_bars(sym: str, http: Any, now_ms: int) -> dict[int, tuple[float, float, float]]:
    """Closed daily bars plus today's bar (its open only is used) as open ms -> (open, close, quote vol)."""
    out = {}
    for row in _get(http, "/fapi/v1/klines", {"symbol": sym, "interval": "1d", "limit": KLINE_LIMIT}):
        t = int(row[0])
        o, c, qv = float(row[1]), float(row[4]), float(row[7])
        if t % DAY_MS or not all(math.isfinite(x) and x > 0 for x in (o, c)) or t > now_ms:
            continue
        out[t] = (o, c if int(row[6]) < now_ms else float("nan"), qv)
    return out


def _mark(raw: Any) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def funding_prints(sym: str, start_ms: int, http: Any) -> list[tuple[int, float, float | None]]:
    """``(fundingTime, fundingRate, markPrice or None)``: the mark Binance associates with the charge
    rides on the same request (Codex, 2026-11-03); a missing or unusable one stays None."""
    rows = _get(http, "/fapi/v1/fundingRate", {"symbol": sym, "startTime": start_ms, "limit": 1000})
    out = sorted((int(r["fundingTime"]), float(r["fundingRate"]), _mark(r.get("markPrice"))) for r in rows)
    if any(not math.isfinite(p[1]) or abs(p[1]) > 0.05 for p in out):
        raise ValueError(f"{sym}: funding rate out of range")
    return out


# ------------------------------------------------------------------ ledger

def data_dir() -> Path:
    from kaiba.core.config import get_settings

    return Path(get_settings().kaiba_data_dir) / "perps" / "xs"


def _load(path: Path) -> dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"version": VERSION, "formations": [], "weeks": [], "marks": {}, "missed": []}


def _save(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, sort_keys=True, allow_nan=False), encoding="utf-8")
    os.replace(tmp, path)


def summary(rows: list[float]) -> dict[str, Any]:
    if not rows:
        return {"weeks": 0}
    return {"weeks": len(rows), "mean": round(st.fmean(rows), 5), "sum": round(math.fsum(rows), 5),
            "worst": round(min(rows), 5), "best": round(max(rows), 5)}


def report(ledger: dict[str, Any], now_ms: int) -> dict[str, Any]:
    by: dict[str, list[float]] = {b: [] for b in BOOKS}
    mix: dict[int, list[float]] = {}
    for w in ledger["weeks"]:
        if w.get("status") == "settled":
            by[w["book"]].append(w["net"])
            mix.setdefault(w["m"], []).append(w["net"])
    mixed = [st.fmean(v) for _, v in sorted(mix.items()) if len(v) == 2]
    n = len(mixed)
    if n < 26:
        verdict = "PENDING"
    elif st.fmean(mixed) <= -0.005:
        verdict = "RETIRE"
    elif st.fmean(mixed) > 0 and st.fmean(by["carry"]) > 0:
        verdict = "CONTINUE_TO_OWNER" if n >= 52 else "NOT_FALSIFIED"
    else:
        verdict = "CONTINUE" if n < 52 else "FAIL"
    return {"version": VERSION, "as_of_ms": now_ms, "first_monday_ms": FIRST_MONDAY_MS, "mode": "paper",
            "books": {b: summary(v) for b, v in by.items()}, "mix_50_50": summary(mixed), "verdict": verdict,
            "missed_formations": ledger.get("missed", []),
            "open": [{"book": b, "m": f["m"], "longs": f[b]["longs"], "shorts": f[b]["shorts"]}
                     for f in ledger["formations"][-1:] for b in BOOKS],
            "notes": ["Paper only: public Binance USD-M data, no key, no order.",
                      "Weekly net return as a fraction of capital per book (each leg 1.0 of capital); "
                      "5 bps a side on target-weight changes plus real funding.",
                      "Not tradeable as is: tax (PMK 50/2025), shorting and access are open owner questions."]}


def _turnover(ledger: dict[str, Any], f: dict[str, Any], book: str) -> float:
    """Trading at formation ``f``: its targets against the previous week's holdings as they DRIFTED
    (each position by its return, the NAV by the book's net), or against nothing after a gap. Known
    only once the previous week has settled, so it is computed at settlement."""
    prev_f = next((x for x in ledger["formations"] if x["m"] == f["m"] - WEEK_MS), None)
    prev_w = next((w for w in ledger["weeks"] if w["m"] == f["m"] - WEEK_MS and w["book"] == book), None)
    old = drifted(weights(prev_f[book]), prev_w["rets"], prev_w["net"]) if prev_f and prev_w else {}
    return turnover(weights(f[book]), old)


def run(*, now_ms: int | None = None, http: Any = None, out_dir: Path | None = None) -> dict[str, Any]:
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    out_dir = data_dir() if out_dir is None else Path(out_dir)
    path = out_dir / "ledger.json"
    ledger = _load(path)
    own = http is None
    if own:
        import httpx

        http = httpx.Client(headers={"User-Agent": "kaiba-perps-xs-paper/1 (public market data)"})
    try:
        m = monday_of(now_ms)
        last = ledger["formations"][-1] if ledger["formations"] else None
        held = sorted({s for f in ledger["formations"][-1:] for b in BOOKS
                       for s in f[b]["longs"] + f[b]["shorts"]})
        bars: dict[str, dict] = {}
        for s in held:  # daily marks for what is held, so a delisting mid-week has a last close
            try:
                bars[s] = daily_bars(s, http, now_ms)
            except Exception:  # noqa: BLE001 - a delisted symbol stops answering; its marks remain
                continue
            closes = [(t, v[1]) for t, v in bars[s].items() if math.isfinite(v[1])]
            if closes:
                ledger["marks"][s] = list(max(closes))
        trading: list[str] | None = None
        info: dict | None = None
        info_wall_ms = 0
        settled = {w["m"] for w in ledger["weeks"]}
        if last is not None and last["m"] + WEEK_MS <= now_ms and last["m"] not in settled:
            info = _get(http, "/fapi/v1/exchangeInfo", {})
            info_wall_ms = int(time.time() * 1000)
            trading = symbols_from(info)
            end = last["m"] + WEEK_MS
            exit_open = {s: b[end][0] for s, b in bars.items() if end in b}
            # The last recorded close stands in only for a symbol that has stopped trading, or once
            # three days have passed (a symbol whose data never returns cannot block the ledger).
            late = now_ms > end + 3 * DAY_MS
            fallback = {s: tuple(v) for s, v in ledger["marks"].items() if late or s not in trading}
            fund_held = {s: funding_prints(s, last["m"] - DAY_MS, http) for s in held}
            rows = [{"book": b, **settle(last[b], last["m"], exit_open, fallback, fund_held,
                                         _turnover(ledger, last, b), bars)} for b in BOOKS]
            if all(r["status"] == "settled" for r in rows):
                ledger["weeks"] += rows
        due = m >= FIRST_MONDAY_MS and (last is None or last["m"] < m) and m not in ledger["missed"]
        if due and now_ms - m > FORM_DEADLINE_MS:
            ledger["missed"].append(m)
            due = False
        if due:
            if info is None:
                info = _get(http, "/fapi/v1/exchangeInfo", {})
                info_wall_ms = int(time.time() * 1000)
                trading = symbols_from(info)
            for s in trading:
                if s not in bars:
                    try:
                        bars[s] = daily_bars(s, http, now_ms)
                    except Exception:  # noqa: BLE001 - one symbol must not hide the others
                        continue
                    time.sleep(0.02)
            liquid = sorted(((s, e[0]) for s, b in bars.items() if (e := eligible(b, m))),
                            key=lambda x: (-x[1], x[0]))[:TOP_N]
            funding = {s: funding_prints(s, m - 9 * DAY_MS, http) for s, _ in liquid}
            new = form(m, {s: bars[s] for s, _ in liquid}, funding)
            new["formed_ms"] = now_ms
            ledger["formations"].append(new)
            # Companion, not ledger: the filters this formation would have traded under, from the
            # exchangeInfo already fetched above. Written once per Monday, never overwritten.
            snap = out_dir / f"order-filters-{m}.json"
            if not snap.exists():
                _save(snap, order_filters_snapshot(info, [s for s, _ in liquid] + held, m=m,
                                                   run_now_ms=now_ms, received_wall_ms=info_wall_ms))
    finally:
        if own:
            http.close()
    _save(path, ledger)
    rep = report(ledger, now_ms)
    _save(out_dir / "report.json", rep)
    return rep


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] != ["run"]:
        print("usage: python -m kaiba.perps.xs_paper run")
        return 2
    rep = run()
    print(json.dumps({"verdict": rep["verdict"], "mix": rep["mix_50_50"], "books": rep["books"],
                      "missed": len(rep["missed_formations"])}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
