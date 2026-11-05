"""Research-only daily trade-price proxies and adverse OHLC bounds.

These calculations cannot certify exchange solvency or actual liquidation.
Missing prices/funding stay unknown. No provider, account or order interface.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

VERSION = "xs-risk-audit/1"
MAINTENANCE_SCENARIOS = (0.005, 0.01, 0.025)
AUTHORITY = {"financial_execution": False, "solvency_certified": False, "profitability_certified": False}


def _number(value: Any, *, positive: bool = False) -> float:
    if type(value) not in (float, int) or not math.isfinite(value) or (positive and value <= 0):
        raise ValueError("finite_number_required")
    return float(value)


def checkpoint(legs: Sequence[Mapping[str, Any]], *, formation_fee: float) -> dict:
    """Fixed quantity weights are signed entry notional / initial weekly NAV.

    funding_fraction is cumulative funding on one unit of ENTRY notional, already
    adjusted by funding-time price / entry. None is not a zero funding payment.
    An exited leg retains its price/funding PnL but has no remaining notional.
    """
    fee = _number(formation_fee)
    if fee < 0 or not legs:
        raise ValueError("nonempty_book_and_nonnegative_fee_required")
    seen, missing, terminal = set(), [], 0
    price_pnl, adverse_pnl, funding, gross, adverse_gross = [], [], [], [], []
    for leg in legs:
        symbol = leg.get("symbol")
        if not isinstance(symbol, str) or not symbol or symbol in seen:
            raise ValueError("unique_symbol_required")
        seen.add(symbol)
        weight = _number(leg.get("weight"))
        entry = _number(leg.get("entry"), positive=True)
        if weight == 0 or type(leg.get("exited")) is not bool:
            raise ValueError("nonzero_weight_and_explicit_exit_required")
        terminal += int(leg["exited"])
        absent = [name for name in ("close", "low", "high", "funding_fraction") if leg.get(name) is None]
        if absent:
            missing.append({"symbol": symbol, "fields": absent})
            continue
        close, low, high = (_number(leg[k], positive=True) for k in ("close", "low", "high"))
        if not low <= close <= high:
            raise ValueError("ohlc_order_invalid")
        cost = _number(leg["funding_fraction"])
        worst = low if weight > 0 else high
        price_pnl.append(weight * (close / entry - 1))
        adverse_pnl.append(weight * (worst / entry - 1))
        funding.append(weight * cost)
        if not leg["exited"]:
            gross.append(abs(weight) * close / entry)
            adverse_gross.append(abs(weight) * worst / entry)
    complete = not missing
    equity = 1 - fee + math.fsum(price_pnl) - math.fsum(funding) if complete else None
    adverse = 1 - fee + math.fsum(adverse_pnl) - math.fsum(funding) if complete else None
    total_gross = math.fsum(gross) if complete else None
    bound_gross = math.fsum(adverse_gross) if complete else None
    return {
        "version": VERSION,
        "legs": len(legs),
        "exited_legs": terminal,
        "complete_supplied_inputs": complete,
        "missing": missing,
        "daily_close_equity_proxy": equity,
        "adverse_ohlc_equity_bound": adverse,
        "gross_notional_proxy": total_gross,
        "adverse_gross_notional": bound_gross,
        "gross_over_equity_proxy": total_gross / equity if complete and equity > 0 else None,
        "nonpositive_equity_proxy": equity <= 0 if complete else None,
        "maintenance_scenarios": [
            {
                "rate": rate,
                "daily_close_proxy_breach": equity <= rate * total_gross if complete else None,
                "adverse_ohlc_bound_breach": adverse <= rate * bound_gross if complete else None,
            }
            for rate in MAINTENANCE_SCENARIOS
        ],
        "basis": "trade OHLC and supplied funding-price proxy; not exchange mark equity",
        "funding_settlement_coverage_verified": False,
        "adverse_extremes_simultaneous": False,
        "actual_liquidation_measured": False,
        **AUTHORITY,
    }


def summarize(rows: Sequence[Mapping[str, Any]]) -> dict:
    """Do not count missing checkpoints as non-breaches or duplicate a date."""
    identities = [(row["week_ms"], row["checkpoint_ms"]) for row in rows]
    if len(set(identities)) != len(identities):
        raise ValueError("duplicate_checkpoint")
    complete = [row for row in rows if row["complete_supplied_inputs"]]
    ratios = [
        row["gross_over_equity_proxy"] for row in complete if row["gross_over_equity_proxy"] is not None
    ]
    return {
        "checkpoints": len(rows),
        "complete_supplied_inputs": len(complete),
        "incomplete": len(rows) - len(complete),
        "minimum_daily_close_equity_proxy": min(
            (r["daily_close_equity_proxy"] for r in complete), default=None
        ),
        "minimum_adverse_ohlc_equity_bound": min(
            (r["adverse_ohlc_equity_bound"] for r in complete), default=None
        ),
        "maximum_gross_over_equity_proxy": max(ratios, default=None),
        "nonpositive_daily_close_proxy_checkpoints": sum(r["nonpositive_equity_proxy"] for r in complete),
        "maintenance_scenarios": [
            {
                "rate": rate,
                **{
                    kind + "_checkpoints": sum(r["maintenance_scenarios"][index][kind] for r in complete)
                    for kind in ("daily_close_proxy_breach", "adverse_ohlc_bound_breach")
                },
                **{
                    kind + "_weeks": len(
                        {r["week_ms"] for r in complete if r["maintenance_scenarios"][index][kind]}
                    )
                    for kind in ("daily_close_proxy_breach", "adverse_ohlc_bound_breach")
                },
            }
            for index, rate in enumerate(MAINTENANCE_SCENARIOS)
        ],
        "actual_liquidation_measured": False,
        "funding_settlement_coverage_verified": False,
        **AUTHORITY,
    }
