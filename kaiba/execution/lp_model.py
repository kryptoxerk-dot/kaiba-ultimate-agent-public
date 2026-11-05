"""DLMM fee scalping: what a one-sided SOL position would hold and earn, from bin state alone.

Owner 2026-11-04: "LP bot is doable too develop one". This is the pure half of that bot's paper
stage: no network, no database, no key, no transaction. `kaiba.execution.lp_paper` feeds it
on-chain bin state; it answers what the position is worth.

**Why a paper position here is not a guess.** A DLMM bin is constant-sum: `P*x + y` does not change
when a swap crosses it, and fees are stored apart from reserves. So:

* **Inventory is exact and needs no swap tape.** A stake of `y` lamports in bin `b` holds `y` SOL
  while the active bin is above `b`, holds `y / P(b)` tokens while it is below, and in the active
  bin holds that bin's own split, because a position is pro rata of its bin.
* **Fees are exact.** Each bin stores fee growth per liquidity share (`fee_growth`), so the fees on
  a hypothetical stake are `shares * delta_growth`, the same arithmetic `dlmm_state.snapshot` uses
  on a real position. The one correction is dilution: real growth would have been lower had our
  shares been in the bin, so fees are scaled by `S / (S + shares)`.

**What it deliberately does not flatter.** Bins are strictly below the active bin at planning, which
avoids the composition fee but also misses fees while price stays above the range. An entry whose
price fell into the planned range before landing is a miss that pays its transaction fee. Leftover
token is sold by walking this pool's own bins at the pool's fee, with our own SOL left out of the
reserves. Jupiter could do better. Anything the walk cannot fill is worth zero and is reported.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

BINS_PER_ARRAY = 70
LAMPORTS = 1_000_000_000
#: Rent for a 10,136-byte BinArray: (10,136 + 128) * 6,960 lamports. NOT refunded: the first LP to
#: touch an array pays it and leaves it behind. On a 1 SOL ticket one array is 7.1% of the stake.
BIN_ARRAY_RENT_LAMPORTS = 71_437_440
#: A position account's rent, returned on close. It is float, not cost, but the wallet must hold it.
POSITION_RENT_LAMPORTS = 57_406_080
#: One transaction at a contested moment: base fee plus priority fee. A floor, not an estimate.
TX_FEE_LAMPORTS = 300_000
#: Open is one transaction. Close is remove + close, plus a swap when any token is left.
OPEN_TXS = 1
CLOSE_TXS = 1
SWAP_TXS = 1


@dataclass(frozen=True)
class Arm:
    """One way of running a position. Every arm shares the same observation and the same reads."""

    name: str
    width: int
    max_hold_s: int
    stop_pct: float
    take_pct: float
    idle_above_s: int
    #: 0 sells whatever token is left. Above 0, a position that ends holding token re-opens two-sided
    #: around the price with this many bins each side, for ``reopen_hold_s``, before selling.
    reopen_width: int = 0
    reopen_hold_s: int = 0
    #: Above 0, the position opens two-sided around the price with this many bins each side: half the
    #: ticket is swapped to token first, so it pays a swap and holds token from the start.
    two_sided: int = 0
    #: Above 1, a two-sided range sized by price, whatever the pool's bin step: 2.0 is -50% to +100%.
    range_x: float = 0.0


@dataclass(frozen=True)
class SelectorConfig:
    """Which pools are hot enough to work. Read only from what the data API shows at that moment."""

    min_tvl_usd: float = 5_000.0
    min_volume_30m_usd: float = 10_000.0
    min_fee_tvl_30m_pct: float = 0.5
    max_bin_step: int = 125
    max_base_fee_pct: float = 3.0
    #: A bin-step range left out, ends included. Real positions lost in it: claude-lp-filtered-prereg-20261104.md.
    skip_bin_steps: tuple[int, int] | None = None
    #: Above 0, a pool must be this old, and an age that cannot be read refuses.
    min_age_h: float = 0.0


@dataclass(frozen=True)
class PoolState:
    """One getMultipleAccounts read: a single slot, so these fields agree with each other."""

    slot: int
    at_ms: int
    active: int
    bin_step: int
    bins: dict[int, dict[str, Any]]
    missing_arrays: frozenset[int] = frozenset()


@dataclass
class Stake:
    y: int
    shares: int
    growth0: tuple[int, int]
    supply0: int


@dataclass
class Plan:
    """A planned position: the bins are fixed when the transaction would be built."""

    arm: Arm
    ticket: int
    lo: int
    hi: int
    stakes: dict[int, Stake] = field(default_factory=dict)
    rent_arrays: int = 0
    opened_ms: int = 0
    above_since_ms: int | None = None
    in_range_ticks: int = 0
    ticks: int = 0
    last_fees: dict[int, tuple[int, int]] = field(default_factory=dict)
    reopened: bool = False
    extra_txs: int = 0
    carried_fee: float = 0.0


@dataclass(frozen=True)
class Value:
    y: float
    x: float
    fee_y: float
    fee_x: float
    price: float

    @property
    def mark(self) -> float:
        return self.y + self.fee_y + (self.x + self.fee_x) * self.price


@dataclass(frozen=True)
class Result:
    proceeds: float
    fee_value: float
    inventory_pnl: float
    exit_cost: float
    unfilled_x: float
    tx_cost: int
    rent_cost: int
    net: float
    net_pct: float


def bin_price(bin_id: int, bin_step: int) -> float:
    """Lamports per token atom at ``bin_id``."""
    return (1 + bin_step / 10_000) ** bin_id


def array_index(bin_id: int) -> int:
    return bin_id // BINS_PER_ARRAY


def arrays_for(lo: int, hi: int) -> list[int]:
    return list(range(array_index(lo), array_index(hi) + 1))


def eligible(pool: dict[str, Any], cfg: SelectorConfig, sol_mint: str, now_ms: int | None = None) -> str | None:
    """Why this pool is out, or None. Each reason is counted, so a dead selector is visible."""
    if pool.get("is_blacklisted"):
        return "blacklisted"
    if ((pool.get("token_y") or {}).get("address")) != sol_mint:
        return "quote_not_sol_y"
    if not (pool.get("token_x") or {}).get("freeze_authority_disabled"):
        return "freeze_authority"
    conf = pool.get("pool_config") or {}
    step, base = conf.get("bin_step"), conf.get("base_fee_pct")
    if not step or base is None:
        return "no_pool_config"
    if step > cfg.max_bin_step:
        return "bin_step"
    if cfg.skip_bin_steps and cfg.skip_bin_steps[0] <= step <= cfg.skip_bin_steps[1]:
        return "bin_step_skipped"
    if base > cfg.max_base_fee_pct:
        return "base_fee"
    if cfg.min_age_h:
        created = int(pool.get("created_at") or 0)
        if not created or now_ms is None:
            return "age_unknown"
        if now_ms - created < cfg.min_age_h * 3_600_000:
            return "too_new"
    if float(pool.get("tvl") or 0) < cfg.min_tvl_usd:
        return "tvl"
    if float((pool.get("volume") or {}).get("30m") or 0) < cfg.min_volume_30m_usd:
        return "volume"
    if float((pool.get("fee_tvl_ratio") or {}).get("30m") or 0) < cfg.min_fee_tvl_30m_pct:
        return "fee_tvl"
    return None


def recent_dump(candles: list[dict[str, Any]], *, limit: float = 0.20) -> tuple[str | None, dict[str, float]]:
    """Did the pool fall this much over the last hour? (reason, measures), from hourly candles.

    Real positions opened just after such a fall lost in both samples. Too little history refuses, because
    a pool that cannot be shown to be calm is not one to enter.
    """
    closes = [float(c["close"]) for c in candles if float(c.get("close") or 0) > 0]
    if len(closes) < 2:
        return "setup_short_history", {}
    # On prices, not the percentage: a fall of exactly 20% computes as -19.99...%.
    fell = closes[-1] <= closes[-2] * (1 - limit)
    return ("dumped_last_hour" if fell else None), {"prior_1h": round(closes[-1] / closes[-2] - 1, 4)}


def dump_setup(candles: list[dict[str, Any]], *, fall: tuple[float, float] = (0.60, 0.75), min_run_x: float = 3.0,
               min_hours_since_top: int = 2, fade: float = 0.40) -> tuple[str | None, dict[str, float]]:
    """Has this token run, dumped 60-75%, and has the selling faded? (reason, measures), from hourly candles.

    The owner's 2026-11-04 @zhaidf_ post: watch a new runner, wait for a 60-70% fall until sell volume
    weakens, then LP a wide two-sided range through the sideways stretch. Only candles already closed
    are read, so nothing here knows what came next.
    """
    if len(candles) < 6:
        return "setup_short_history", {}
    highs = [float(c["high"]) for c in candles]
    top = max(range(len(highs)), key=highs.__getitem__)
    low_before = min((float(c["low"]) for c in candles[:top + 1]), default=0.0)
    last = float(candles[-1]["close"])
    run_x = highs[top] / low_before if low_before > 0 else 0.0
    drop = 1 - last / highs[top]
    after = [float(c["volume"]) for c in candles[top:]]
    measures = {"run_x": round(run_x, 2), "drop": round(drop, 3), "hours_since_top": len(candles) - 1 - top}
    if run_x < min_run_x:
        return "setup_no_run", measures
    if len(candles) - 1 - top < min_hours_since_top:
        return "setup_top_too_recent", measures
    if not fall[0] <= drop <= fall[1]:
        return "setup_drop_out_of_band", measures
    recent = sum(after[-2:]) / 2
    measures["volume_ratio"] = round(recent / max(after), 3) if max(after) > 0 else 1.0
    return ("setup_selling_not_faded", measures) if measures["volume_ratio"] > fade else (None, measures)


def shares_for(y: int, row: dict[str, Any] | None) -> int:
    """Shares minted for ``y`` lamports into a bin that holds only SOL or is empty."""
    q = y << 64
    if not row or not row.get("supply"):
        return q
    x_r, y_r = row["amounts"]
    liquidity = row["price_q64"] * x_r + (y_r << 64)
    return q if liquidity <= 0 else q * row["supply"] // liquidity


def half_width(arm: Arm, bin_step: int) -> int:
    """Bins each side of the price for a two-sided arm, or 0 for a one-sided one."""
    if arm.range_x > 1:
        return math.ceil(math.log(arm.range_x) / math.log(1 + bin_step / 10_000))
    return arm.two_sided


def plan_bins(arm: Arm, state: PoolState, ticket: int) -> Plan:
    """Spot shape strictly below the active bin. The bins are fixed from here on."""
    width = half_width(arm, state.bin_step)
    if width:
        lo, hi = state.active - width, state.active + width
    else:
        hi = state.active - 1
        lo = hi - arm.width + 1
    return Plan(arm=arm, ticket=ticket, lo=lo, hi=hi,
                rent_arrays=sum(1 for i in arrays_for(lo, hi) if i in state.missing_arrays))


def land(plan: Plan, state: PoolState, fee_rate: float = 0.0) -> bool:
    """Open the position on the state it landed on, or False when price fell into the range first.

    A two-sided position cannot miss: it is centred on whatever price it lands on.
    """
    width = half_width(plan.arm, state.bin_step)
    if width:
        half = plan.ticket / 2
        spread(plan, state, half * (1 - fee_rate) / bin_price(state.active, state.bin_step), half, width)
        plan.extra_txs += SWAP_TXS
        plan.opened_ms = state.at_ms
        return True
    if state.active <= plan.hi:
        return False
    each = plan.ticket // plan.arm.width
    rest = plan.ticket - each * plan.arm.width
    for k, b in enumerate(range(plan.lo, plan.hi + 1)):
        y = each + (rest if k == plan.arm.width - 1 else 0)
        row = state.bins.get(b)
        growth = tuple(row["fee_growth"]) if row else (0, 0)
        plan.stakes[b] = Stake(y=y, shares=shares_for(y, row), growth0=growth,
                               supply0=int(row["supply"]) if row else 0)
    plan.opened_ms = state.at_ms
    return True


def fees(plan: Plan, state: PoolState) -> tuple[int, int]:
    """Fees earned so far, in token atoms and lamports, scaled for our own dilution.

    A bin outside this read keeps its last known figure: fees never go down.
    """
    total = [0, 0]
    for b, stake in plan.stakes.items():
        row = state.bins.get(b)
        if row is not None:
            earned = []
            for i in range(2):
                delta = row["fee_growth"][i] - stake.growth0[i]
                fee = ((stake.shares >> 64) * delta) >> 64 if delta > 0 else 0
                seen = [s for s in (stake.supply0, int(row["supply"])) if s > 0]
                if seen:
                    fee = fee * min(seen) // (min(seen) + stake.shares)
                earned.append(fee)
            plan.last_fees[b] = (earned[0], earned[1])
        got = plan.last_fees.get(b)
        if got:
            total[0] += got[0]
            total[1] += got[1]
    return total[0], total[1]


def holdings(plan: Plan, state: PoolState) -> tuple[float, float]:
    """(token atoms, lamports) the stakes hold now. Exact: a bin is constant-sum."""
    x = y = 0.0
    for b, stake in plan.stakes.items():
        p = bin_price(b, state.bin_step)
        if b < state.active:
            y += stake.y
        elif b > state.active:
            x += stake.y / p
        else:
            row = state.bins.get(b)
            share_y = 1.0
            if row:
                x_r, y_r = row["amounts"]
                liquidity = p * x_r + y_r
                if liquidity > 0:
                    share_y = y_r / liquidity
            y += stake.y * share_y
            x += stake.y * (1 - share_y) / p
    return x, y


def value(plan: Plan, state: PoolState) -> Value:
    x, y = holdings(plan, state)
    fee_x, fee_y = fees(plan, state)
    return Value(y=y, x=x, fee_y=float(fee_y), fee_x=float(fee_x), price=bin_price(state.active, state.bin_step))


def quote_sell(x: float, state: PoolState, fee_rate: float) -> tuple[float, float]:
    """(lamports out, token atoms unfilled) selling ``x`` down this pool's bins."""
    left, out = x, 0.0
    for b in sorted((b for b in state.bins if b <= state.active), reverse=True):
        reserve = float(state.bins[b]["amounts"][1])
        if reserve <= 0 or left <= 0:
            continue
        p = bin_price(b, state.bin_step)
        take = min(left * (1 - fee_rate), reserve / p)
        out += take * p
        left -= take / (1 - fee_rate)
    return out, max(left, 0.0)


def settle(plan: Plan, state: PoolState, fee_rate: float) -> Result:
    """Everything the round trip cost, on the state the close landed on."""
    v = value(plan, state)
    tokens = v.x + v.fee_x
    out, unfilled = quote_sell(tokens, state, fee_rate) if tokens > 0 else (0.0, 0.0)
    proceeds = v.y + v.fee_y + out
    tx_cost = (OPEN_TXS + CLOSE_TXS + (SWAP_TXS if tokens > 0 else 0) + plan.extra_txs) * TX_FEE_LAMPORTS
    rent = plan.rent_arrays * BIN_ARRAY_RENT_LAMPORTS
    fee_value = plan.carried_fee + v.fee_y + v.fee_x * v.price
    net = proceeds - plan.ticket - tx_cost - rent
    return Result(proceeds=proceeds, fee_value=fee_value, inventory_pnl=v.y + v.x * v.price - plan.ticket,
                  exit_cost=tokens * v.price - out, unfilled_x=unfilled, tx_cost=tx_cost, rent_cost=rent,
                  net=net, net_pct=100 * net / plan.ticket)


def spread(plan: Plan, state: PoolState, tokens: float, sol: float, width: int) -> None:
    """Stake token in bins above the price and SOL below, skipping the active bin and its composition
    fee. A token stake is held as its SOL value at its own bin, the constant-sum quantity `holdings`
    needs."""
    above = range(state.active + 1, state.active + 1 + width)
    below = range(state.active - width, state.active)
    stakes: dict[int, Stake] = {}
    for b in above:
        y = int(tokens / width * bin_price(b, state.bin_step))
        row = state.bins.get(b)
        stakes[b] = Stake(y=y, shares=y << 64, growth0=tuple(row["fee_growth"]) if row else (0, 0),
                          supply0=int(row["supply"]) if row else 0)
    for b in below:
        y = int(sol / width)
        row = state.bins.get(b)
        stakes[b] = Stake(y=y, shares=shares_for(y, row), growth0=tuple(row["fee_growth"]) if row else (0, 0),
                          supply0=int(row["supply"]) if row else 0)
    plan.stakes, plan.last_fees = stakes, {}
    plan.lo, plan.hi = below[0], above[-1]


def reopen(plan: Plan, state: PoolState) -> None:
    """Close and re-open two-sided around the price, with what the position holds now.

    MEASURED 2026-11-04 on the published challenge's first wallet: re-opens of this kind made 139 of
    its 213 SOL. The first leg's fees fold into the new stakes, so the ticket stays the base of every
    later figure.
    """
    v = value(plan, state)
    width = plan.arm.reopen_width
    plan.carried_fee += v.fee_y + v.fee_x * v.price
    plan.extra_txs += OPEN_TXS + CLOSE_TXS
    spread(plan, state, v.x + v.fee_x, v.y + v.fee_y, width)
    plan.rent_arrays += sum(1 for i in arrays_for(plan.lo, plan.hi) if i in state.missing_arrays)
    plan.opened_ms, plan.above_since_ms, plan.reopened = state.at_ms, None, True


def wants_reopen(plan: Plan, reason: str, state: PoolState) -> bool:
    """A first leg that ended mostly in token re-opens instead of selling into the fall."""
    if plan.reopened or not plan.arm.reopen_width or reason not in ("below_range", "stop"):
        return False
    v = value(plan, state)
    return (v.x + v.fee_x) * v.price >= 0.5 * v.mark


def exit_reason(plan: Plan, state: PoolState) -> str | None:
    """Why this arm leaves now, or None. Called once per tick, so it also keeps the tick counts."""
    arm = plan.arm
    plan.ticks += 1
    if plan.lo <= state.active <= plan.hi:
        plan.in_range_ticks += 1
    held_s = (state.at_ms - plan.opened_ms) / 1000
    if state.active < plan.lo:
        return "below_range"
    pnl_pct = 100 * (value(plan, state).mark - plan.ticket) / plan.ticket
    if pnl_pct <= -arm.stop_pct:
        return "stop"
    if pnl_pct >= arm.take_pct:
        return "take"
    if state.active > plan.hi:
        plan.above_since_ms = plan.above_since_ms or state.at_ms
        if (state.at_ms - plan.above_since_ms) / 1000 >= arm.idle_above_s:
            return "idle_above"
    else:
        plan.above_since_ms = None
    limit = arm.reopen_hold_s if plan.reopened else arm.max_hold_s
    return "max_hold" if held_s >= limit else None
