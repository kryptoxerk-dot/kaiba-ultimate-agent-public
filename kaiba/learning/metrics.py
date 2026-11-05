"""Performance and calibration maths over the recorded decision/trade stream.

Why this module exists in this shape:

* **It is the only place the agent's edge is quantified, so it must be boring.** Every
  function here takes data and returns numbers. Nothing calls a model, nothing reads
  config, nothing writes. The promotion gates in :mod:`kaiba.learning.gates` are built on
  top of it, and a gate the agent can influence is not a gate.
* **Undefined is ``None``, not zero.** Expectancy over an empty trade list is not "no
  edge", it is "no data". A profit factor with no losing trades is not 10, it is capped at
  10 and flagged. Per the repository contract, a reassuring default is a bug.
* **Money stays in ``int`` base units and ``Decimal``.** Returns are computed as
  ``pnl_native / cost_native`` in ``Decimal`` — exact — rather than from the ``pnl_pct``
  REAL column, which is a float round-trip. ``pnl_pct`` is only a fallback for rows that
  never recorded a cost. Ratios (Sharpe, Brier, beta) are floats because they are not
  money.
* **Attribution matters more than PnL.** A lane that bought anything at all while SOL
  doubled looks brilliant on expectancy alone. :func:`attribution` regresses realised
  returns on the chain's native token so the beta component can be subtracted out. What is
  left is the only number that says the lane knows something.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from statistics import NormalDist
from typing import Any

from pydantic import BaseModel, Field

from kaiba.core.db import fetch_all, get_conn, jload
from kaiba.core.schemas import Chain, Lane, LaneMode

ZERO = Decimal(0)
ONE = Decimal(1)
HUNDRED = Decimal(100)

#: A profit factor with no losses is infinite, which is never a real measurement.
PROFIT_FACTOR_CAP = Decimal(10)

#: Default annualisation factor for a per-trade return series. Memecoin lanes turn over
#: daily at best, so "periods" are trades, not calendar days; callers that know their real
#: trade frequency should pass it.
DEFAULT_PERIODS_PER_YEAR = 365.0


# --------------------------------------------------------------------------------------
# row access helpers — trades arrive either as sqlite dict rows or as TradeOutcome models
# --------------------------------------------------------------------------------------


def _field(row: Any, name: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(name, default)
    return getattr(row, name, default)


def _dec(value: Any, default: Decimal | None = ZERO) -> Decimal | None:
    """Coerce a DB cell (TEXT big integer, REAL, int, Decimal) into ``Decimal``."""
    if value is None:
        return default
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return Decimal(int(value))
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    text = str(value).strip()
    if not text:
        return default
    try:
        return Decimal(text)
    except InvalidOperation:
        return default


def pnl_native(trade: Any) -> Decimal:
    """Realised PnL of one trade in the chain's native base units."""
    return _dec(_field(trade, "pnl_native"), ZERO) or ZERO


def cost_native(trade: Any) -> Decimal:
    return _dec(_field(trade, "cost_native"), ZERO) or ZERO


def trade_return(trade: Any) -> Decimal | None:
    """Return of one trade as a fraction (0.25 == +25%).

    Derived from base units when a cost was recorded, because that is exact. The
    ``pnl_pct`` column is a float and is only used when there is no cost to divide by.
    """
    cost = cost_native(trade)
    if cost > 0:
        return pnl_native(trade) / cost
    pct = _dec(_field(trade, "pnl_pct"), None)
    if pct is None:
        return None
    return pct / HUNDRED


def returns_of(trades: Iterable[Any]) -> list[Decimal]:
    out: list[Decimal] = []
    for t in trades:
        r = trade_return(t)
        if r is not None:
            out.append(r)
    return out


# --------------------------------------------------------------------------------------
# core performance statistics
# --------------------------------------------------------------------------------------


def expectancy(trades: Sequence[Any]) -> Decimal | None:
    """Mean realised PnL per trade, in native base units.

    ``None`` when there are no trades: the agent has not shown us anything yet, and that
    is different from having shown us zero.
    """
    rows = list(trades)
    if not rows:
        return None
    total = sum((pnl_native(t) for t in rows), ZERO)
    return total / Decimal(len(rows))


def expectancy_r(trades: Sequence[Any]) -> Decimal | None:
    """Mean return per trade as a fraction — size-independent, so lanes compare."""
    rets = returns_of(trades)
    if not rets:
        return None
    return sum(rets, ZERO) / Decimal(len(rets))


def win_rate(trades: Sequence[Any]) -> float | None:
    rows = list(trades)
    if not rows:
        return None
    wins = sum(1 for t in rows if pnl_native(t) > 0)
    return wins / len(rows)


def profit_factor(trades: Sequence[Any]) -> Decimal | None:
    """Gross profit / gross loss, capped at :data:`PROFIT_FACTOR_CAP`.

    ``None`` with no trades. Capped (not infinite) when there are no losses, because a
    lane that has not lost yet has not been measured, it has been lucky.
    """
    rows = list(trades)
    if not rows:
        return None
    gross_win = sum((pnl_native(t) for t in rows if pnl_native(t) > 0), ZERO)
    gross_loss = sum((-pnl_native(t) for t in rows if pnl_native(t) < 0), ZERO)
    if gross_loss == 0:
        return PROFIT_FACTOR_CAP if gross_win > 0 else None
    return min(gross_win / gross_loss, PROFIT_FACTOR_CAP)


def mean_std(values: Sequence[Decimal]) -> tuple[Decimal, Decimal] | None:
    """Sample mean and sample standard deviation (n-1), exact in ``Decimal``."""
    n = len(values)
    if n < 2:
        return None
    mean = sum(values, ZERO) / Decimal(n)
    var = sum(((v - mean) ** 2 for v in values), ZERO) / Decimal(n - 1)
    if var < 0:
        var = ZERO
    return mean, var.sqrt()


def sharpe_of_returns(
    returns: Sequence[Decimal | float], periods_per_year: float = DEFAULT_PERIODS_PER_YEAR
) -> float | None:
    """Annualised Sharpe of a return series. ``None`` if fewer than two points or flat."""
    vals = [v if isinstance(v, Decimal) else Decimal(str(v)) for v in returns]
    stats = mean_std(vals)
    if stats is None:
        return None
    mean, std = stats
    if std == 0:
        return None
    return float(mean / std) * math.sqrt(max(periods_per_year, 0.0))


def sharpe(
    trades: Sequence[Any], periods_per_year: float = DEFAULT_PERIODS_PER_YEAR
) -> float | None:
    return sharpe_of_returns(returns_of(trades), periods_per_year)


def sortino_of_returns(
    returns: Sequence[Decimal | float],
    periods_per_year: float = DEFAULT_PERIODS_PER_YEAR,
    target: Decimal = ZERO,
) -> float | None:
    """Downside-deviation Sharpe. ``None`` when nothing went below target (undefined)."""
    vals = [v if isinstance(v, Decimal) else Decimal(str(v)) for v in returns]
    if len(vals) < 2:
        return None
    mean = sum(vals, ZERO) / Decimal(len(vals))
    downside = [(v - target) for v in vals if v < target]
    if not downside:
        return None
    dd_var = sum((d**2 for d in downside), ZERO) / Decimal(len(vals))
    if dd_var <= 0:
        return None
    return float((mean - target) / dd_var.sqrt()) * math.sqrt(max(periods_per_year, 0.0))


def sortino(
    trades: Sequence[Any], periods_per_year: float = DEFAULT_PERIODS_PER_YEAR
) -> float | None:
    return sortino_of_returns(returns_of(trades), periods_per_year)


def max_drawdown(equity_curve: Sequence[tuple[int, int]] | Sequence[int]) -> Decimal | None:
    """Largest peak-to-trough decline of an equity curve, in the curve's own units.

    Accepts ``[(ts_ms, equity)]`` or a bare list of equity values. Returns a non-negative
    ``Decimal``; ``None`` for an empty curve. Absolute rather than fractional so it stays
    comparable when equity crosses zero, which a small bankroll does.
    """
    points = list(equity_curve)
    if not points:
        return None
    values: list[Decimal] = []
    for p in points:
        if isinstance(p, (tuple, list)):
            values.append(_dec(p[1], ZERO) or ZERO)
        else:
            values.append(_dec(p, ZERO) or ZERO)
    peak = values[0]
    worst = ZERO
    for v in values:
        if v > peak:
            peak = v
        drop = peak - v
        if drop > worst:
            worst = drop
    return worst


def max_drawdown_pct(equity_curve: Sequence[tuple[int, int]] | Sequence[int]) -> float | None:
    """Drawdown as a fraction of the running peak. ``None`` when the peak is never > 0."""
    points = list(equity_curve)
    if not points:
        return None
    values = [
        (_dec(p[1], ZERO) or ZERO) if isinstance(p, (tuple, list)) else (_dec(p, ZERO) or ZERO)
        for p in points
    ]
    peak = values[0]
    worst: Decimal | None = None
    for v in values:
        if v > peak:
            peak = v
        if peak > 0:
            frac = (peak - v) / peak
            if worst is None or frac > worst:
                worst = frac
    return float(worst) if worst is not None else None


def avg_hold_s(trades: Sequence[Any]) -> float | None:
    rows = [t for t in trades if _field(t, "hold_s") is not None]
    if not rows:
        return None
    return sum(int(_field(t, "hold_s")) for t in rows) / len(rows)


def turnover(trades: Sequence[Any], bankroll_base_units: int | None = None) -> Decimal | None:
    """Notional deployed, in base units — or as a multiple of bankroll when one is given."""
    rows = list(trades)
    if not rows:
        return None
    total = sum((cost_native(t) for t in rows), ZERO)
    if bankroll_base_units:
        return total / Decimal(bankroll_base_units)
    return total


# --------------------------------------------------------------------------------------
# calibration — does `Decision.confidence` mean anything?
# --------------------------------------------------------------------------------------


def _calibration_pairs(items: Iterable[Any]) -> list[tuple[float, int]]:
    """Normalise to ``(probability, outcome 0/1)``.

    Accepts ``(p, win)`` tuples, mappings with ``confidence``/``win`` (or ``outcome``,
    ``pnl_native``), or objects with those attributes. Rows without a usable probability
    are dropped rather than assumed to be 0.5.
    """
    pairs: list[tuple[float, int]] = []
    for item in items:
        if isinstance(item, (tuple, list)) and len(item) == 2:
            p_raw, o_raw = item
        else:
            p_raw = _field(item, "confidence", _field(item, "probability"))
            o_raw = _field(item, "win", _field(item, "outcome"))
            if o_raw is None:
                pnl = _field(item, "pnl_native")
                o_raw = None if pnl is None else ((_dec(pnl, ZERO) or ZERO) > 0)
        if p_raw is None or o_raw is None:
            continue
        try:
            p = float(p_raw)
        except (TypeError, ValueError):
            continue
        if not (0.0 <= p <= 1.0) or math.isnan(p):
            continue
        outcome = 1 if (o_raw is True or o_raw == 1 or o_raw == "win") else 0
        pairs.append((p, outcome))
    return pairs


def brier_score(decisions_with_outcomes: Iterable[Any]) -> float | None:
    """Mean squared error of stated confidence against the realised win/loss.

    0 is perfect, 0.25 is a coin flip stated at 50%, 1 is confidently wrong every time.
    """
    pairs = _calibration_pairs(decisions_with_outcomes)
    if not pairs:
        return None
    return sum((p - o) ** 2 for p, o in pairs) / len(pairs)


def base_rate(decisions_with_outcomes: Iterable[Any]) -> float | None:
    pairs = _calibration_pairs(decisions_with_outcomes)
    if not pairs:
        return None
    return sum(o for _, o in pairs) / len(pairs)


def brier_skill_score(decisions_with_outcomes: Iterable[Any]) -> float | None:
    """Brier score against the "always predict the base rate" reference forecast.

    Positive means the confidence number carries information beyond knowing how often the
    lane wins. Zero or negative means it does not, whatever the model says in prose.
    """
    pairs = _calibration_pairs(decisions_with_outcomes)
    if len(pairs) < 2:
        return None
    rate = sum(o for _, o in pairs) / len(pairs)
    ref = sum((rate - o) ** 2 for _, o in pairs) / len(pairs)
    if ref == 0:
        return None  # every outcome identical: no skill is measurable
    bs = sum((p - o) ** 2 for p, o in pairs) / len(pairs)
    return 1.0 - bs / ref


def calibration_buckets(decisions_with_outcomes: Iterable[Any], bins: int = 5) -> list[dict[str, Any]]:
    """Reliability table: stated confidence versus realised frequency, per bucket."""
    pairs = _calibration_pairs(decisions_with_outcomes)
    bins = max(1, bins)
    buckets: list[dict[str, Any]] = []
    for i in range(bins):
        lo, hi = i / bins, (i + 1) / bins
        inside = [(p, o) for p, o in pairs if (p >= lo and (p < hi or (i == bins - 1 and p <= hi)))]
        buckets.append(
            {
                "lo": round(lo, 4),
                "hi": round(hi, 4),
                "n": len(inside),
                "stated": round(sum(p for p, _ in inside) / len(inside), 4) if inside else None,
                "realised": round(sum(o for _, o in inside) / len(inside), 4) if inside else None,
            }
        )
    return buckets


# --------------------------------------------------------------------------------------
# attribution: how much of the return was just holding the chain's native token?
# --------------------------------------------------------------------------------------


def attribution(
    trades: Sequence[Any],
    benchmark_returns: Mapping[str, Any] | Sequence[Any],
) -> dict[str, Any]:
    """Split realised return into a beta component and a residual.

    ``benchmark_returns`` is the native token's return over each trade's own holding
    window: either ``{trade_id: return_fraction}`` or a sequence aligned with ``trades``.

    Ordinary least squares of trade return on benchmark return:
    ``beta = cov(r, b) / var(b)``, ``alpha = mean(r) - beta * mean(b)``. The beta
    component of the total is ``beta * sum(b)``; the residual is what is left. A lane whose
    residual is ~0 has no edge — it rode the chain.

    Returns ``basis: "unavailable"`` rather than a fabricated beta when the benchmark does
    not vary or there are too few paired observations.
    """
    rows = list(trades)
    paired: list[tuple[Decimal, Decimal]] = []
    for i, t in enumerate(rows):
        r = trade_return(t)
        if r is None:
            continue
        if isinstance(benchmark_returns, Mapping):
            b_raw = benchmark_returns.get(str(_field(t, "trade_id")))
        else:
            b_raw = benchmark_returns[i] if i < len(benchmark_returns) else None
        b = _dec(b_raw, None)
        if b is None:
            continue
        paired.append((r, b))

    out: dict[str, Any] = {"n": len(paired), "basis": "derived"}
    if len(paired) < 2:
        out["basis"] = "unavailable"
        out["reason"] = "fewer than two paired observations"
        return out

    n = Decimal(len(paired))
    mean_r = sum((r for r, _ in paired), ZERO) / n
    mean_b = sum((b for _, b in paired), ZERO) / n
    var_b = sum(((b - mean_b) ** 2 for _, b in paired), ZERO)
    total_r = sum((r for r, _ in paired), ZERO)
    total_b = sum((b for _, b in paired), ZERO)
    if var_b == 0:
        out["basis"] = "unavailable"
        out["reason"] = "benchmark does not vary; beta is not identifiable"
        out["total_return"] = total_r
        out["mean_return"] = mean_r
        return out

    cov = sum(((r - mean_r) * (b - mean_b) for r, b in paired), ZERO)
    beta = cov / var_b
    alpha = mean_r - beta * mean_b
    beta_component = beta * total_b
    residual_component = total_r - beta_component

    var_r = sum(((r - mean_r) ** 2 for r, _ in paired), ZERO)
    r_squared = float((cov * cov) / (var_r * var_b)) if var_r > 0 else 0.0

    out.update(
        {
            "beta": beta,
            "alpha_per_trade": alpha,
            "total_return": total_r,
            "benchmark_total": total_b,
            "beta_component": beta_component,
            "residual_component": residual_component,
            "r_squared": r_squared,
            "mean_return": mean_r,
            "mean_benchmark": mean_b,
        }
    )
    return out


# --------------------------------------------------------------------------------------
# database rollups
# --------------------------------------------------------------------------------------


class LaneStats(BaseModel):
    """One rollup block. Every ratio is optional because "not measurable" is a real state."""

    key: str
    trades: int = 0
    wins: int = 0
    losses: int = 0
    pnl_native: Decimal = ZERO
    expectancy: Decimal | None = None
    expectancy_r: Decimal | None = None
    win_rate: float | None = None
    profit_factor: Decimal | None = None
    sharpe: float | None = None
    sortino: float | None = None
    max_drawdown: Decimal | None = None
    avg_hold_s: float | None = None
    turnover: Decimal | None = None
    mistakes: dict[str, int] = Field(default_factory=dict)


class MistakeStats(BaseModel):
    tag: str
    count: int = 0
    pnl_native: Decimal = ZERO
    expectancy: Decimal | None = None


def _mode_value(mode: LaneMode | str | None) -> str | None:
    if mode is None:
        return None
    return mode.value if isinstance(mode, LaneMode) else str(mode)


def load_trades(
    conn: sqlite3.Connection | None = None,
    *,
    since_ms: int = 0,
    until_ms: int | None = None,
    mode: LaneMode | str | None = None,
    lane: Lane | str | None = None,
    chain: Chain | str | None = None,
) -> list[dict[str, Any]]:
    """Closed trades in a window, oldest first. The one query everything else builds on."""
    c = conn or get_conn()
    sql = "SELECT * FROM trades WHERE closed_ms >= ?"
    params: list[Any] = [since_ms]
    if until_ms is not None:
        sql += " AND closed_ms <= ?"
        params.append(until_ms)
    m = _mode_value(mode)
    if m:
        sql += " AND mode = ?"
        params.append(m)
    if lane is not None:
        sql += " AND lane = ?"
        params.append(lane.value if isinstance(lane, Lane) else str(lane))
    if chain is not None:
        sql += " AND chain = ?"
        params.append(chain.value if isinstance(chain, Chain) else str(chain))
    sql += " ORDER BY closed_ms ASC, trade_id ASC"
    rows = fetch_all(c, sql, params)
    for r in rows:
        r["mistakes"] = jload(r.get("mistakes_json"), [])
    return rows


def _stats_for(key: str, rows: Sequence[dict[str, Any]]) -> LaneStats:
    curve = [(int(r["closed_ms"]), pnl) for r, pnl in zip(rows, cumulative_pnl(rows), strict=True)]
    mistakes: dict[str, int] = {}
    for r in rows:
        for tag in r.get("mistakes") or jload(r.get("mistakes_json"), []):
            mistakes[tag] = mistakes.get(tag, 0) + 1
    return LaneStats(
        key=key,
        trades=len(rows),
        wins=sum(1 for r in rows if pnl_native(r) > 0),
        losses=sum(1 for r in rows if pnl_native(r) < 0),
        pnl_native=sum((pnl_native(r) for r in rows), ZERO),
        expectancy=expectancy(rows),
        expectancy_r=expectancy_r(rows),
        win_rate=win_rate(rows),
        profit_factor=profit_factor(rows),
        sharpe=sharpe(rows),
        sortino=sortino(rows),
        max_drawdown=max_drawdown(curve),
        avg_hold_s=avg_hold_s(rows),
        turnover=turnover(rows),
        mistakes=mistakes,
    )


def cumulative_pnl(rows: Sequence[Any]) -> list[Decimal]:
    running = ZERO
    out: list[Decimal] = []
    for r in rows:
        running += pnl_native(r)
        out.append(running)
    return out


def by_lane(
    conn: sqlite3.Connection | None = None,
    since_ms: int = 0,
    mode: LaneMode | str | None = None,
    *,
    until_ms: int | None = None,
) -> dict[Lane, LaneStats]:
    rows = load_trades(conn, since_ms=since_ms, until_ms=until_ms, mode=mode)
    grouped: dict[Lane, list[dict[str, Any]]] = {}
    for r in rows:
        try:
            lane = Lane(r["lane"])
        except ValueError:
            continue  # an unknown lane string is bad data, not a new lane
        grouped.setdefault(lane, []).append(r)
    return {lane: _stats_for(lane.value, rs) for lane, rs in grouped.items()}


def by_regime(
    conn: sqlite3.Connection | None = None,
    since_ms: int = 0,
    mode: LaneMode | str | None = None,
    *,
    until_ms: int | None = None,
) -> dict[str, LaneStats]:
    """Regime comes from the decision that opened the trade, not from hindsight."""
    c = conn or get_conn()
    sql = (
        "SELECT t.*, d.regime AS regime FROM trades t "
        "LEFT JOIN decisions d ON d.decision_id = t.decision_id WHERE t.closed_ms >= ?"
    )
    params: list[Any] = [since_ms]
    if until_ms is not None:
        sql += " AND t.closed_ms <= ?"
        params.append(until_ms)
    m = _mode_value(mode)
    if m:
        sql += " AND t.mode = ?"
        params.append(m)
    sql += " ORDER BY t.closed_ms ASC, t.trade_id ASC"
    rows = fetch_all(c, sql, params)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        r["mistakes"] = jload(r.get("mistakes_json"), [])
        grouped.setdefault(r.get("regime") or "unknown", []).append(r)
    return {k: _stats_for(k, rs) for k, rs in grouped.items()}


def by_mistake_tag(
    conn: sqlite3.Connection | None = None,
    since_ms: int = 0,
    mode: LaneMode | str | None = None,
    *,
    until_ms: int | None = None,
) -> dict[str, MistakeStats]:
    rows = load_trades(conn, since_ms=since_ms, until_ms=until_ms, mode=mode)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        for tag in r["mistakes"]:
            grouped.setdefault(str(tag), []).append(r)
    return {
        tag: MistakeStats(
            tag=tag,
            count=len(rs),
            pnl_native=sum((pnl_native(r) for r in rs), ZERO),
            expectancy=expectancy(rs),
        )
        for tag, rs in grouped.items()
    }


def equity_curve(
    conn: sqlite3.Connection | None = None,
    mode: LaneMode | str | None = None,
    chain: Chain | str | None = None,
    *,
    since_ms: int = 0,
    until_ms: int | None = None,
    lane: Lane | str | None = None,
) -> list[tuple[int, int]]:
    """Cumulative realised PnL in native base units, one point per closed trade."""
    rows = load_trades(
        conn, since_ms=since_ms, until_ms=until_ms, mode=mode, chain=chain, lane=lane
    )
    out: list[tuple[int, int]] = []
    running = 0
    for r in rows:
        running += int(pnl_native(r))
        out.append((int(r["closed_ms"]), running))
    return out


def calibration_points(
    conn: sqlite3.Connection | None = None,
    *,
    since_ms: int = 0,
    until_ms: int | None = None,
    mode: LaneMode | str | None = None,
    lane: Lane | str | None = None,
) -> list[tuple[float, int]]:
    """``(stated confidence, realised win)`` for every decision that produced a trade."""
    c = conn or get_conn()
    sql = (
        "SELECT d.confidence AS confidence, t.pnl_native AS pnl_native "
        "FROM decisions d JOIN trades t ON t.decision_id = d.decision_id "
        "WHERE t.closed_ms >= ? AND d.confidence IS NOT NULL"
    )
    params: list[Any] = [since_ms]
    if until_ms is not None:
        sql += " AND t.closed_ms <= ?"
        params.append(until_ms)
    m = _mode_value(mode)
    if m:
        sql += " AND t.mode = ?"
        params.append(m)
    if lane is not None:
        sql += " AND t.lane = ?"
        params.append(lane.value if isinstance(lane, Lane) else str(lane))
    sql += " ORDER BY t.closed_ms ASC"
    return _calibration_pairs(fetch_all(c, sql, params))


def normal_cdf(z: float) -> float:
    return NormalDist().cdf(z)


def normal_inv_cdf(p: float) -> float:
    return NormalDist().inv_cdf(min(max(p, 1e-12), 1 - 1e-12))


def skewness(values: Sequence[Decimal | float]) -> float:
    """Sample skewness (biased, /n) — used by the deflated Sharpe ratio."""
    vals = [float(v) for v in values]
    n = len(vals)
    if n < 3:
        return 0.0
    mean = sum(vals) / n
    m2 = sum((v - mean) ** 2 for v in vals) / n
    m3 = sum((v - mean) ** 3 for v in vals) / n
    if m2 <= 0:
        return 0.0
    return m3 / (m2**1.5)


def kurtosis(values: Sequence[Decimal | float]) -> float:
    """Non-excess kurtosis (3.0 for a normal distribution)."""
    vals = [float(v) for v in values]
    n = len(vals)
    if n < 4:
        return 3.0
    mean = sum(vals) / n
    m2 = sum((v - mean) ** 2 for v in vals) / n
    m4 = sum((v - mean) ** 4 for v in vals) / n
    if m2 <= 0:
        return 3.0
    return m4 / (m2**2)


__all__ = [
    "LaneStats",
    "MistakeStats",
    "PROFIT_FACTOR_CAP",
    "attribution",
    "avg_hold_s",
    "base_rate",
    "brier_score",
    "brier_skill_score",
    "by_lane",
    "by_mistake_tag",
    "by_regime",
    "calibration_buckets",
    "calibration_points",
    "cumulative_pnl",
    "cost_native",
    "equity_curve",
    "expectancy",
    "expectancy_r",
    "kurtosis",
    "load_trades",
    "max_drawdown",
    "max_drawdown_pct",
    "mean_std",
    "normal_cdf",
    "normal_inv_cdf",
    "pnl_native",
    "profit_factor",
    "returns_of",
    "sharpe",
    "sharpe_of_returns",
    "skewness",
    "sortino",
    "sortino_of_returns",
    "trade_return",
    "turnover",
    "win_rate",
]
