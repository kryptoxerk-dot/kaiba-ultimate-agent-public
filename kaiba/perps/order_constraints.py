"""Offline USD-M MARKET quantity diagnostics, never an order or admission gate.

Inputs are caller-supplied public snapshots. Current filters cannot qualify old
backtests. A quantity that fits these filters says nothing about account margin,
order limits, depth, fill price, slippage, fees or a strategy's profitability.
"""
from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal, localcontext
from math import gcd
from typing import Any

_QUANTITY = {"LOT_SIZE", "MARKET_LOT_SIZE", "MIN_NOTIONAL"}
_OTHER = {"PRICE_FILTER", "PERCENT_PRICE", "MAX_NUM_ORDERS", "POSITION_RISK_CONTROL"}


def _decimal(value: Any, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("decimal_string_required")
    try:
        result = Decimal(value)
    except Exception as exc:
        raise ValueError("invalid_decimal") from exc
    if not result.is_finite() or abs(result) > Decimal("1e24") or result.as_tuple().exponent < -24:
        raise ValueError("decimal_out_of_range")
    if positive and result <= 0:
        raise ValueError("nonpositive_decimal")
    return result


def _lattice(lots: list[dict]) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """Intersect both minQty-anchored step grids using exact integer CRT."""
    rows = [tuple(_decimal(row[k], positive=True) for k in ("minQty", "maxQty", "stepSize"))
            for row in lots]
    if any(lo > hi for lo, hi, _ in rows):
        raise ValueError("invalid_quantity_bounds")
    digits = max(0, max(-x.as_tuple().exponent for row in rows for x in row))
    scale = Decimal(10) ** digits
    ints = [tuple(int(x * scale) for x in row) for row in rows]
    a, _, step_a = ints[0]
    b, _, step_b = ints[1]
    g = gcd(step_a, step_b)
    if (b - a) % g:
        raise ValueError("incompatible_quantity_grids")
    reduced = step_b // g
    k = 0 if reduced == 1 else ((b-a)//g * pow(step_a//g, -1, reduced)) % reduced
    period = step_a * reduced
    origin = (a + step_a * k) % period
    return (Decimal(origin) / scale, Decimal(period) / scale,
            max(r[0] for r in rows), min(r[1] for r in rows))


def assess_market_order(info: dict, mark: dict, signed_notional: Any, now_ms: int,
                        *, max_mark_age_ms: int = 60_000) -> dict:
    """Round toward zero; report infeasible clips without increasing or reallocating.

    ``signed_notional`` is USDT exposure, not margin. Mark prices are used only
    for the documented market minimum-notional rule and indicative rounding.
    Even ``FITS_SNAPSHOT_FILTERS`` is explicitly not execution permission.
    """
    out = {"symbol": info.get("symbol"), "status": "UNKNOWN", "reason": None,
           "execution_authorized": False, "margin_feasibility": "UNKNOWN"}
    with localcontext() as ctx:
        ctx.prec = 100
        try:
            target = _decimal(signed_notional)
            if not target:
                raise ValueError("zero_target")
            out["target_signed_notional"] = str(target)
            if (info.get("status") != "TRADING" or info.get("contractType") != "PERPETUAL"
                    or info.get("quoteAsset") != "USDT" or info.get("marginAsset") != "USDT"
                    or "MARKET" not in info.get("orderTypes", [])):
                raise ValueError("unsupported_or_inactive_market")
            if mark.get("symbol") != info.get("symbol"):
                raise ValueError("symbol_mismatch")
            ts = mark.get("time")
            if (type(ts) is not int or type(now_ms) is not int or max_mark_age_ms <= 0
                    or not 0 <= now_ms - ts <= max_mark_age_ms):
                raise ValueError("mark_clock_missing_stale_or_future")
            price = _decimal(mark["markPrice"], positive=True)
            filters: dict[str, dict] = {}
            for row in info["filters"]:
                name = row["filterType"]
                if name in filters:
                    raise ValueError("duplicate_filter")
                if name not in _QUANTITY | _OTHER:
                    raise ValueError("unsupported_filter:" + str(name))
                filters[name] = row
            if not _QUANTITY <= filters.keys():
                raise ValueError("missing_quantity_filter")
            control = filters.get("POSITION_RISK_CONTROL", {}).get("positionControlSide", "NONE")
            if control != "NONE":
                raise ValueError("position_control_unassessed")
            minimum = filters["MIN_NOTIONAL"]
            if set(minimum) != {"filterType", "notional"}:
                raise ValueError("unsupported_notional_semantics")
            min_notional = _decimal(minimum["notional"], positive=True)
            origin, step, lo, hi = _lattice([filters["LOT_SIZE"], filters["MARKET_LOT_SIZE"]])
            desired = abs(target) / price
            quantity = origin + ((desired - origin) / step).to_integral_value(rounding=ROUND_FLOOR) * step
            quantity = max(Decimal(0), quantity)
            minimum_quantity = origin + ((max(lo, min_notional / price)-origin) / step).to_integral_value(
                rounding="ROUND_CEILING") * step
            candidate = quantity * price
            reason = ("below_min_quantity" if quantity < lo else "above_max_quantity" if quantity > hi
                      else "below_min_notional" if candidate < min_notional else None)
            sign = Decimal(1) if target > 0 else Decimal(-1)
            feasible = Decimal(0) if reason else sign * candidate
            out.update(status="INFEASIBLE" if reason else "FITS_SNAPSHOT_FILTERS", reason=reason,
                       mark_price=str(price), mark_time_ms=ts, rounded_quantity=str(quantity),
                       rounded_signed_notional=str(sign * candidate),
                       indicative_admissible_signed_notional=str(feasible),
                       unallocated_signed_notional=str(target-feasible),
                       minimum_filter_fit_notional=(str(minimum_quantity * price)
                                                    if minimum_quantity <= hi else None),
                       unassessed_filters=sorted(filters.keys() - _QUANTITY),
                       metadata_freshness="CALLER_MUST_ESTABLISH")
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            out["reason"] = str(exc)
    return out


def portfolio_snapshot(exchange: dict, marks: list[dict], targets: dict[str, Any], now_ms: int) -> dict:
    """Expose missing legs and net/gross drift; never redistribute rejected capital."""
    if not targets:
        raise ValueError("empty_portfolio")
    def unique(rows: list[dict]) -> tuple[dict, set]:
        found, duplicate = {}, set()
        for row in rows:
            symbol = row.get("symbol")
            if symbol in found:
                duplicate.add(symbol)
            found[symbol] = row
        return found, duplicate

    symbols, duplicate_symbols = unique(exchange.get("symbols", []))
    prices, duplicate_marks = unique(marks)
    rows = []
    for symbol, target in sorted(targets.items()):
        if symbol not in symbols or symbol not in prices or symbol in duplicate_symbols | duplicate_marks:
            rows.append({"symbol": symbol, "status": "UNKNOWN", "reason": "missing_or_duplicate_input",
                         "execution_authorized": False})
        else:
            rows.append(assess_market_order(symbols[symbol], prices[symbol], target, now_ms))
    with localcontext() as ctx:
        ctx.prec = 100
        intended = [_decimal(t) for t in targets.values()]
        indicative = [Decimal(row["indicative_admissible_signed_notional"]) for row in rows
                      if row["status"] != "UNKNOWN"]
        unknown = sum(r["status"] == "UNKNOWN" for r in rows)
        failed = sum(r["status"] == "INFEASIBLE" for r in rows)
        return {"status": "UNKNOWN" if unknown else "FILTER_MISMATCH" if failed else "SIZING_ONLY",
                "execution_authorized": False, "rows": rows, "unknown_legs": unknown,
                "infeasible_legs": failed, "intended_gross_notional": str(sum(map(abs, intended))),
                "intended_net_notional": str(sum(intended)),
                "known_indicative_gross_notional": str(sum(map(abs, indicative))),
                "known_indicative_net_notional": str(sum(indicative)),
                "net_drift_if_unknown_legs_omitted": str(sum(indicative)-sum(intended)),
                "warning": "Partial snapshot sizing only; no reweighting, margin, depth or fill verification."}


def portfolio_from_companion(companion: dict, marks: list[dict], targets: dict[str, Any], now_ms: int,
                             *, max_metadata_age_ms: int = 300_000) -> dict:
    """Consume the lead's lossless F3 snapshot; never infer fields from its summary.

    The receive-age check cannot establish the upstream generation freshness.
    Caller-supplied marks must independently pass their own clock checks.
    Old schema-1 summaries deliberately remain unusable instead of being
    upgraded with invented market metadata or collapsed duplicate filters.
    """
    issue = None
    received = companion.get("received_wall_ms")
    if companion.get("schema") != "kaiba/perps-xs-order-filters/2":
        issue = "unsupported_companion_schema"
    elif (type(received) is not int or type(now_ms) is not int or max_metadata_age_ms <= 0
          or not 0 <= now_ms - received <= max_metadata_age_ms):
        issue = "metadata_receive_clock_missing_stale_or_future"
    symbols = []
    if not issue:
        rows = companion.get("symbols")
        if not isinstance(rows, dict):
            issue = "malformed_companion_symbols"
        else:
            for symbol in targets:
                row = rows.get(symbol, {})
                raw = row.get("raw_symbols") if isinstance(row, dict) else None
                if not isinstance(raw, list) or any(not isinstance(s, dict) or s.get("symbol") != symbol for s in raw):
                    issue = "missing_or_mismatched_raw_symbols"
                    break
                symbols.extend(raw)  # duplicate objects/filters remain visible to the assessor
    result = portfolio_snapshot({"symbols": [] if issue else symbols}, marks, targets, now_ms)
    result.update(companion_issue=issue, metadata_received_ms=received,
                  metadata_server_time_ms=companion.get("server_time_ms"),
                  metadata_generation_freshness="UNKNOWN")
    return result
