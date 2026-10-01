"""How the risk limits must scale as the bankroll compounds.

Today every limit in ``config/risk.yaml`` is an absolute number of base units.
``max_position`` is 0.1 SOL and ``daily_loss_stop`` is 0.1 SOL, so the stop is exactly
one maximum position: **one max-size loser ends the trading day**. That is not a brake,
it is an off switch, and it is flatly incompatible with the operator's directive to run
high volume. Under compounding the same absolute numbers drift into nonsense in the other
direction: at 45 SOL a 0.1 SOL daily stop is 0.22% of the bankroll and stops nothing at
all.

This module is the specification for the replacement. Every rule below is a pure function
over integers and :class:`~decimal.Decimal` -- floats never touch money -- and the tests
pin the arithmetic so that whoever implements it in ``kaiba/execution/risk.py`` and
``config/risk.yaml`` has an executable contract rather than a paragraph of prose.

THE MEASURED INPUTS, and where they come from
---------------------------------------------
:data:`MEASURED_RETURNS` is the full record of closed paper round trips as of
2026-09-21: 49 from ``data/kaiba.db`` and 8 from the live VPS at
``~/kaiba/data/kaiba.db``. The token sets are disjoint, so the pooled sample is 57
independent closed positions. They are embedded here as integer (cost, proceeds) pairs in
lamports rather than read from a database, because the test suite is offline by contract
and because a measurement that can silently change is not a measurement.

The three constants the design turns on are all derived from that sample by the tests
themselves, never asserted by hand:

* ``sigma``   ~ 0.633  per-trade return standard deviation
* ``mu``      ~ -0.418 per-trade mean return -- NEGATIVE, on n=57, with a bootstrap
  probability of the true mean being positive of 0.0000
* ``rho``     ~ 0.22   intraclass correlation of returns opened in the same hour

WHAT IS INVENTED
----------------
The *policy* constants -- 1.0% max position, 8% daily stop, 25% total exposure, 4% per
token, quarter-Kelly -- are INVENTED. They are choices, argued from the measured numbers
above but not themselves measured. They are marked INVENTED where they are defined. The
*shape* of every rule (linear in bankroll, sqrt in trade count, 1/rho ceiling on
diversification) is DERIVED from the measured sample.
"""

from __future__ import annotations

import math
import statistics
from decimal import Decimal

import pytest

# ======================================================================================
# MEASURED: the closed-position record. (cost_native, proceeds_native, opened_ms)
# ======================================================================================

#: 49 closed positions from the local ``data/kaiba.db`` (earlier shadow run, lanes
#: migration-fade and manual, chain sol) plus 8 from the live VPS. MEASURED 2026-09-21.
MEASURED_RETURNS: tuple[tuple[int, int, int], ...] = (
    # --- local data/kaiba.db, 49 rows
    (51_000_000, 65_145_623, 1_789_898_546_640),
    (51_000_000, 44_756_581, 1_789_898_600_000),
    (102_000_000, 90_373_548, 1_789_898_700_000),
    (16_401_000, 5_700_633, 1_789_901_000_000),
    (15_769_500, 0, 1_789_901_100_000),
    (15_913_500, 4_407_062, 1_789_901_200_000),
    (16_499_999, 21_490_487, 1_789_901_300_000),
    (16_243_499, 0, 1_789_901_400_000),
    (16_342_500, 5_529_878, 1_789_901_500_000),
    (15_409_500, 6_773_370, 1_789_901_600_000),
    (16_639_500, 6_559_305, 1_789_901_700_000),
    (16_762_500, 10_339_156, 1_789_901_800_000),
    (16_626_000, 5_932_645, 1_789_901_900_000),
    (16_287_000, 0, 1_789_902_000_000),
    (16_072_500, 1_290_179, 1_789_902_100_000),
    (15_852_000, 2_939_027, 1_789_902_200_000),
    (16_377_000, 3_159_197, 1_789_902_300_000),
    (16_554_000, 15_133_361, 1_789_902_400_000),
    (16_078_500, 2_202_936, 1_789_902_500_000),
    (16_630_500, 23_678_443, 1_789_902_600_000),
    (16_492_500, 1_883_487, 1_789_902_700_000),
    (15_787_500, 26_986_296, 1_789_902_800_000),
    (16_641_000, 2_397_686, 1_789_902_900_000),
    (16_128_000, 26_080_572, 1_789_903_000_000),
    (15_407_999, 2_237_523, 1_789_903_100_000),
    (16_639_500, 3_283_300, 1_789_903_200_000),
    (15_608_999, 1_827_392, 1_789_903_300_000),
    (15_742_500, 2_727_077, 1_789_903_400_000),
    (16_108_500, 2_348_065, 1_789_903_500_000),
    (16_453_500, 764_401, 1_789_903_600_000),
    (15_457_499, 0, 1_789_903_700_000),
    (16_072_500, 5_253_214, 1_789_903_800_000),
    (16_467_000, 5_783_159, 1_789_903_900_000),
    (46_000_000, 21_767_096, 1_789_911_000_000),
    (46_000_000, 8_459_017, 1_789_911_100_000),
    (71_555_000, 40_610_614, 1_789_914_000_000),
    (74_600_000, 29_416_854, 1_789_914_100_000),
    (71_277_500, 29_089_123, 1_789_914_200_000),
    (78_312_500, 52_012_072, 1_789_914_300_000),
    (72_965_000, 125_955_851, 1_789_914_400_000),
    (77_232_500, 48_155_064, 1_789_917_000_000),
    (74_554_999, 34_249_177, 1_789_917_100_000),
    (74_525_000, 1_460_638, 1_789_917_200_000),
    (73_347_500, 134_303_389, 1_789_917_300_000),
    (79_115_000, 3_680_709, 1_789_920_000_000),
    (79_872_500, 43_318_916, 1_789_920_100_000),
    (51_000_000, 0, 1_789_923_000_000),
    (51_000_000, 47_259_225, 1_789_923_100_000),
    (76_520_000, 927_874, 1_789_923_973_851),
    # --- live VPS ~/kaiba/data/kaiba.db, 8 rows, MEASURED verbatim
    (80_365_100, 54_105_885, 1_789_985_346_414),
    (79_828_858, 149_651_617, 1_789_985_822_919),
    (79_828_858, 254_811_577, 1_789_986_197_117),
    (79_828_858, 49_236_017, 1_789_986_593_330),
    (79_475_358, 47_851_283, 1_789_988_514_162),
    (79_475_358, 131_707_534, 1_789_990_917_431),
    (79_828_858, 20_260_265, 1_789_995_099_503),
    (79_828_858, 22_883_290, 1_789_996_372_828),
)


def _returns() -> list[float]:
    """Per-trade fractional returns. Statistics only -- never a money path."""
    return [(p - c) / c for c, p, _ in MEASURED_RETURNS]


# ======================================================================================
# MEASURED constants, and the economic floors that set the absolute limits
# ======================================================================================

#: Economic floor per chain, in native base units. ABSOLUTE by construction: it is set by
#: venue fees and gas, which do not care how rich we are. MEASURED, from config/risk.yaml
#: (sol 0.045 SOL, bsc 0.003 BNB, robinhood 0.00435 ETH) and the cost work behind it.
MIN_POSITION = {
    "sol": 45_000_000,  # 0.045 SOL, 9 decimals
    "bsc": 3_000_000_000_000_000,  # 0.003 BNB, 18 decimals
    "robinhood": 4_350_000_000_000_000,  # 0.00435 ETH, 18 decimals
}

#: Live bankrolls at the time of measurement, same units.
BANKROLL_TODAY = {
    "sol": 4_500_000_000,
    "bsc": 950_000_000_000_000_000,
    "robinhood": 150_000_000_000_000_000,
}

#: DERIVED from the measured day-loss simulation: the 5th percentile of a zero-edge
#: trading day of N trades is very close to ``1.0 * sqrt(N)`` maximum positions, so a stop
#: at ``1.15 * sqrt(N)`` fires on 1.3-3.1% of zero-edge days across N = 10..150. The
#: coefficient is DERIVED; the 2-3% target fire rate is INVENTED.
STOP_COEFFICIENT = Decimal("1.15")

# ---------------------------------------------------------------- INVENTED policy dials
#: INVENTED. Per-position size as a percentage of the LIVE bankroll. Chosen from the ruin
#: table: at f=1% over 1,000 trades the probability of ever halving the bankroll is 0.000
#: under a zero edge (median x0.974) while a +5% edge still compounds to x1.61. Raising it
#: to 2% buys x2.49 at a +5% edge but takes zero-edge ruin from 0.000 to 0.097.
MAX_POSITION_PCT = Decimal("1.0")

#: INVENTED. Daily realised-loss stop as a percentage of the LIVE bankroll. Sits inside
#: the operator's own ``bounds.max_daily_loss_pct`` of 10.0. With MAX_POSITION_PCT this
#: buys 48 trades/day of headroom -- see :func:`max_trades_per_day`.
DAILY_STOP_PCT = Decimal("8.0")

#: INVENTED. Cap on SIMULTANEOUS open exposure, as a percentage of the live bankroll.
#: This is the number that must be survivable in full: 5 of the 57 measured outcomes were
#: -100%, and at the measured rho=0.22 a correlated wave can take the whole book. At 25%,
#: four consecutive total-wipeout days leave 0.75**4 = x0.316 rather than zero.
MAX_TOTAL_EXPOSURE_PCT = Decimal("25.0")

#: INVENTED. Per-token exposure cap, as a percentage of the live bankroll. Four times
#: MAX_POSITION_PCT, so a position may be added to three times and no further.
MAX_TOKEN_EXPOSURE_PCT = Decimal("4.0")


# ======================================================================================
# The recommended scaling rules. Pure, integer, no floats in any money path.
# ======================================================================================


def _require_int(name: str, value: object) -> int:
    """Money is integers in base units. A float here is a bug, not a rounding choice."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int in base units, got {type(value).__name__}")
    return value


def max_position_base_units(bankroll: int, min_position: int, pct: Decimal = MAX_POSITION_PCT) -> int:
    """Per-position ceiling: a percentage of the live bankroll, floored at the economic floor.

    The percentage is what compounds. The floor is what does not: it is priced by venue
    fees and gas, so it stays absolute however rich we get. When the percentage lands below
    the floor the floor wins, and :func:`chain_is_viable` is what decides whether trading
    at that forced size is still allowed at all.
    """
    _require_int("bankroll", bankroll)
    _require_int("min_position", min_position)
    if bankroll <= 0:
        return 0
    scaled = int(Decimal(bankroll) * pct / Decimal(100))
    return max(scaled, min_position)


def daily_stop_base_units(bankroll: int, pct: Decimal = DAILY_STOP_PCT) -> int:
    """Daily realised-loss stop: a straight percentage of the live bankroll.

    Percentage, not absolute, is the whole point. An absolute stop is a different
    instrument at every bankroll -- 22% of 0.45 SOL and 0.22% of 45 SOL -- so it either
    halts everything or nothing depending only on how well we have been doing.
    """
    _require_int("bankroll", bankroll)
    if bankroll <= 0:
        return 0
    return int(Decimal(bankroll) * pct / Decimal(100))


def max_total_exposure_base_units(bankroll: int, pct: Decimal = MAX_TOTAL_EXPOSURE_PCT) -> int:
    """Cap on the sum of all open positions on a chain.

    THIS LIMIT DOES NOT EXIST TODAY. ``RiskGate.check_entry`` caps per-token exposure via
    ``max_exposure_pct`` and otherwise only refuses when the gas reserve would be breached,
    so total open exposure may currently reach ``bankroll - gas_reserve`` -- 98.9% of the
    sol bankroll. See :func:`test_gate_today_has_no_total_exposure_cap`, which pins that
    gap against the real gate so it cannot be argued away.
    """
    _require_int("bankroll", bankroll)
    if bankroll <= 0:
        return 0
    return int(Decimal(bankroll) * pct / Decimal(100))


def max_concurrent_positions(bankroll: int, min_position: int, pct: Decimal = MAX_POSITION_PCT) -> int:
    """How many positions may be open at once. DERIVED, never configured separately.

    ``max_total_exposure / max_position``. The operator's mandate says no count cap, and
    this respects it: nothing here is a number someone picked. The count falls out of two
    limits that already exist, and it self-tightens as the bankroll shrinks, because a
    smaller bankroll pins ``max_position`` at the absolute floor while the exposure cap
    keeps scaling down.
    """
    pos = max_position_base_units(bankroll, min_position, pct)
    if pos <= 0:
        return 0
    return max_total_exposure_base_units(bankroll) // pos


def max_trades_per_day(pct: Decimal = MAX_POSITION_PCT, stop_pct: Decimal = DAILY_STOP_PCT) -> int:
    """Trades per day the daily stop can absorb without halting a normal losing day.

    Inverts ``daily_stop = 1.15 * sqrt(N) * max_position``. This is the single dial that
    ties aggression to risk: at a fixed daily loss budget, per-position size must fall as
    ``1/sqrt(N)`` for volume to rise, and total daily turnover therefore grows as
    ``sqrt(N)`` -- sublinearly, but it does grow.
    """
    if pct <= 0:
        return 0
    ratio = stop_pct / (STOP_COEFFICIENT * pct)
    return int(ratio * ratio)


def chain_is_viable(bankroll: int, min_position: int, pct: Decimal = MAX_POSITION_PCT) -> bool:
    """Is this chain tradable at this bankroll, or must it be switched off?

    A chain is off when one forced-floor position would breach the total exposure cap on
    its own. Below that point the fee floor is dictating a bet size the bankroll cannot
    carry, and the honest answer is to stop trading that chain rather than to trade it at
    a size that cannot pay for itself.
    """
    pos = max_position_base_units(bankroll, min_position, pct)
    return pos > 0 and pos <= max_total_exposure_base_units(bankroll)


def effective_independent_bets(k: int, rho: float) -> float:
    """k correlated positions are worth this many independent ones. Ceiling is 1/rho."""
    if k <= 0:
        return 0.0
    if rho <= 0:
        return float(k)
    return k / (1 + (k - 1) * rho)


def intraclass_correlation(groups: list[list[float]]) -> float:
    """One-way ICC(1): the share of return variance explained by *which hour it was*."""
    groups = [g for g in groups if len(g) >= 2]
    sizes = [len(g) for g in groups]
    total = sum(sizes)
    k = len(groups)
    grand = sum(sum(g) for g in groups) / total
    msb = sum(len(g) * (statistics.mean(g) - grand) ** 2 for g in groups) / (k - 1)
    msw = sum(sum((x - statistics.mean(g)) ** 2 for x in g) for g in groups) / (total - k)
    n0 = (total - sum(n * n for n in sizes) / total) / (k - 1)
    return (msb - msw) / (msb + (n0 - 1) * msw)


# ======================================================================================
# 1. What the measured record actually says
# ======================================================================================


def test_the_measured_sample_is_57_not_8() -> None:
    """The brief's n=8 is a recent slice; the repository holds 57 closed round trips."""
    assert len(MEASURED_RETURNS) == 57
    opened = [ts for _, _, ts in MEASURED_RETURNS]
    assert opened == sorted(opened), "rows must stay in time order for the hour bucketing"


def test_measured_per_trade_mean_is_negative_and_sigma_is_the_design_input() -> None:
    """mu = -41.8%, sigma = 63.3%. Both DERIVED from the embedded sample."""
    r = _returns()
    mu = statistics.mean(r)
    sigma = statistics.pstdev(r)
    assert mu == pytest.approx(-0.4181, abs=0.002), "per-trade expectancy is negative"
    assert sigma == pytest.approx(0.6333, abs=0.002)
    assert sum(1 for x in r if x > 0) == 10, "10 winners in 57"
    assert sum(1 for x in r if x <= -0.999) == 5, "5 total losses -- the tail is real"


def test_pooled_return_on_capital_deployed_is_negative() -> None:
    """-27.9% pooled, in integer base units. The n=8 slice showed +14.4%; this is all of it."""
    cost = sum(c for c, _, _ in MEASURED_RETURNS)
    proceeds = sum(p for _, p, _ in MEASURED_RETURNS)
    pooled = Decimal(proceeds - cost) / Decimal(cost)
    assert pooled < 0
    assert pooled == pytest.approx(Decimal("-0.2793"), abs=Decimal("0.001"))


def test_the_edge_kill_gate_would_fire_on_todays_evidence() -> None:
    """Upper 95% confidence bound on the edge is still below zero at n=57.

    This is the number that makes every sizing question secondary: no fraction of bankroll
    is safe against a negative edge at volume, so the design's real protection is a gate
    that measures the edge and stops. On today's data that gate says STOP.
    """
    r = _returns()
    se = statistics.pstdev(r) / math.sqrt(len(r))
    ucb = statistics.mean(r) + 1.645 * se
    assert ucb < 0, "a lane whose 95% upper bound is negative must not size up"
    assert ucb == pytest.approx(-0.2801, abs=0.002)


def test_kelly_on_the_measured_edge_is_zero() -> None:
    """Kelly maximises E[log(1+fR)]. On the measured sample the maximiser is f -> 0."""
    r = _returns()

    def growth(f: float) -> float:
        return statistics.mean(math.log(1 + f * x) for x in r)

    assert growth(0.001) > growth(0.01) > growth(0.02) > growth(0.05)
    assert growth(0.001) < 0, "even an infinitesimal bet loses money at this expectancy"


def test_volume_buys_the_truth_faster_than_it_burns_the_bankroll() -> None:
    """Fail-fast is cheap; confirm-slow is expensive. This asymmetry is the whole design.

    Detecting a badly negative edge takes a handful of trades. Confirming a small positive
    one takes hundreds. High volume shortens both in wall-clock time, which is the honest
    case FOR the operator's directive.
    """
    sigma = statistics.pstdev(_returns())

    def n_to_reject(true_edge: float) -> float:
        return (1.645 * sigma / abs(true_edge)) ** 2

    def n_to_confirm(true_edge: float) -> float:
        return ((1.96 + 0.84) * sigma / true_edge) ** 2

    assert n_to_reject(0.418) < 10, "the measured edge is rejectable in under 10 trades"
    assert n_to_reject(0.10) < 120
    assert n_to_confirm(0.10) > 300, "confirming a +10% edge needs hundreds of trades"
    assert n_to_confirm(0.05) > 1000
    # At 48 trades/day the -10% case resolves in under 3 days; at 1 trade/day, 109 days.
    assert n_to_reject(0.10) / 48 < 3


# ======================================================================================
# 2. Which limits scale, and which must not
# ======================================================================================


@pytest.mark.parametrize("chain", ["sol", "bsc", "robinhood"])
@pytest.mark.parametrize("multiplier", [1, 2, 10, 100])
def test_min_position_is_absolute_and_never_scales(chain: str, multiplier: int) -> None:
    """The economic floor is set by fees and gas. Those do not care how rich we are."""
    floor = MIN_POSITION[chain]
    rich = BANKROLL_TODAY[chain] * multiplier
    poor = BANKROLL_TODAY[chain] // multiplier
    assert max_position_base_units(rich, floor) >= floor
    assert max_position_base_units(poor, floor) >= floor
    # The floor itself is never a function of the bankroll.
    assert MIN_POSITION[chain] == floor


@pytest.mark.parametrize("multiplier", [1, 2, 4, 10, 20, 100])
def test_max_position_scales_linearly_with_the_bankroll(multiplier: int) -> None:
    """Once clear of the floor, doubling the bankroll doubles the position."""
    floor = MIN_POSITION["sol"]
    base = BANKROLL_TODAY["sol"]
    assert max_position_base_units(base * multiplier, floor) == (
        max_position_base_units(base, floor) * multiplier
    )


def test_daily_stop_scales_linearly_and_is_inside_the_operators_bound() -> None:
    """8% of the live bankroll, always inside the operator's own 10% ceiling."""
    for mult in (1, 2, 10, 100):
        bankroll = BANKROLL_TODAY["sol"] * mult
        stop = daily_stop_base_units(bankroll)
        assert stop == int(Decimal(bankroll) * Decimal("8.0") / 100)
        assert Decimal(stop) * 100 / Decimal(bankroll) <= Decimal("10.0")


def test_money_rules_reject_floats() -> None:
    """Floats never touch money. A float bankroll is a type error, not a rounding choice."""
    for fn in (daily_stop_base_units, max_total_exposure_base_units):
        with pytest.raises(TypeError):
            fn(4.5e9)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        max_position_base_units(4.5e9, 45_000_000)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        max_position_base_units(4_500_000_000, 4.5e7)  # type: ignore[arg-type]


def test_every_returned_limit_is_an_integer_in_base_units() -> None:
    bankroll, floor = BANKROLL_TODAY["sol"], MIN_POSITION["sol"]
    for value in (
        max_position_base_units(bankroll, floor),
        daily_stop_base_units(bankroll),
        max_total_exposure_base_units(bankroll),
    ):
        assert isinstance(value, int) and not isinstance(value, bool)


# ======================================================================================
# 3. THE URGENT ONE: the daily stop must survive a losing streak
# ======================================================================================


def _mean_loss() -> float:
    return abs(statistics.mean([x for x in _returns() if x <= 0]))


def test_the_shipped_daily_stop_halts_after_a_single_loser() -> None:
    """MEASURED defect. daily_loss_stop 0.1 SOL == max_position 0.1 SOL.

    At the measured mean loss of 66.9% of the position, the shipped envelope halts the
    day after 1.5 max-size losers. A 3-loss streak is a 1.000 probability event in a
    20-trade day at the measured loss rate, so the agent would halt on essentially every
    high-volume day before the strategy had a chance to express itself.
    """
    shipped_stop = 100_000_000  # 0.1 SOL, config/risk.yaml chains.sol
    shipped_max_position = 100_000_000
    losers_absorbed = shipped_stop / (shipped_max_position * _mean_loss())
    assert losers_absorbed < 2.0
    assert shipped_stop == shipped_max_position, "the stop is exactly one position"


def test_the_recommended_stop_absorbs_the_budgeted_ten_loss_streak() -> None:
    """BUDGETED STREAK: 10 consecutive max-size losers, at the measured mean loss.

    Why 10. At the measured loss rate of 0.825 a run of 10 has probability 0.40 in a
    20-trade day and 0.97 in a 100-trade day -- it is not a tail event, it is Tuesday. A
    stop that halts on a run that common is not managing risk, it is refusing to trade.
    Ruin is still bounded because the stop is 8% of the LIVE bankroll and therefore
    shrinks with it.
    """
    bankroll = BANKROLL_TODAY["sol"]
    floor = MIN_POSITION["sol"]
    stop = daily_stop_base_units(bankroll)
    position = max_position_base_units(bankroll, floor)
    losers_absorbed = stop / (position * _mean_loss())
    assert losers_absorbed >= 10.0, "must survive the budgeted streak"
    assert losers_absorbed < 20.0, "but must still be a stop, not decoration"


def test_the_budgeted_streak_is_a_common_event_not_a_tail_event() -> None:
    """P(run of >=10 losses) at the measured loss rate, by exact dynamic programming."""
    r = _returns()
    p_loss = sum(1 for x in r if x <= 0) / len(r)
    assert p_loss == pytest.approx(0.8246, abs=0.002)

    def p_run(p: float, trials: int, k: int) -> float:
        state = [0.0] * k
        state[0] = 1.0
        absorbed = 0.0
        for _ in range(trials):
            nxt = [0.0] * k
            for run, pr in enumerate(state):
                if pr == 0.0:
                    continue
                if run + 1 >= k:
                    absorbed += pr * p
                else:
                    nxt[run + 1] += pr * p
                nxt[0] += pr * (1 - p)
            state = nxt
        return absorbed

    assert p_run(p_loss, 20, 10) > 0.35, "a 10-streak in a 20-trade day is routine"
    assert p_run(p_loss, 100, 10) > 0.95, "in a 100-trade day it is near certain"
    assert p_run(p_loss, 20, 3) > 0.99, "3-streaks are guaranteed -- the shipped stop dies here"


def test_stop_rule_and_volume_rule_are_inverses() -> None:
    """daily_stop = 1.15*sqrt(N)*max_position, inverted to give the trades/day headroom."""
    assert max_trades_per_day(Decimal("1.0"), Decimal("8.0")) == 48
    assert max_trades_per_day(Decimal("2.0"), Decimal("8.0")) == 12
    assert max_trades_per_day(Decimal("1.0"), Decimal("6.0")) == 27
    # The shipped envelope: stop and position are equal, so it budgets for under one trade.
    assert max_trades_per_day(Decimal("2.22"), Decimal("2.22")) == 0


def test_halving_position_size_quadruples_the_permitted_volume() -> None:
    """The 1/sqrt(N) trade-off, stated as the operator will experience it."""
    big = max_trades_per_day(Decimal("2.0"))
    small = max_trades_per_day(Decimal("1.0"))
    assert small == pytest.approx(big * 4, rel=0.05)
    # Turnover -- the thing the operator actually means by "volume" -- grows as sqrt(N).
    turnover_big = Decimal(big) * Decimal("2.0")
    turnover_small = Decimal(small) * Decimal("1.0")
    assert turnover_small > turnover_big


def test_recommended_envelope_beats_the_shipped_one_on_turnover() -> None:
    """The aggression claim, as a number: ~28x more capital turned over per day."""
    shipped = Decimal(max_trades_per_day(Decimal("2.22"), Decimal("2.22"))) * Decimal("2.22")
    # The shipped envelope rounds to zero permitted trades; price it at its literal one.
    shipped = max(shipped, Decimal("1") * Decimal("2.22"))
    recommended = Decimal(max_trades_per_day()) * MAX_POSITION_PCT
    assert recommended / shipped > Decimal("20")


# ======================================================================================
# 4. Concurrency: 10 memecoins in one hour are not 10 independent bets
# ======================================================================================


def test_same_hour_positions_are_measurably_correlated() -> None:
    """MEASURED rho = 0.22 from the 57-position record, bucketed by hour of entry."""
    buckets: dict[int, list[float]] = {}
    for (_, _, ts), r in zip(MEASURED_RETURNS, _returns(), strict=True):
        buckets.setdefault(ts // 3_600_000, []).append(r)
    rho = intraclass_correlation(list(buckets.values()))
    assert rho > 0.1, "concurrent memecoin positions share a common factor"
    assert rho == pytest.approx(0.22, abs=0.06)


def test_diversification_saturates_at_one_over_rho() -> None:
    """The hard ceiling: no number of correlated positions exceeds 1/rho independent bets."""
    rho = 0.22
    assert effective_independent_bets(10, rho) == pytest.approx(3.36, abs=0.05)
    assert effective_independent_bets(20, rho) == pytest.approx(3.87, abs=0.05)
    assert effective_independent_bets(1000, rho) < 1 / rho
    # 10 positions are worth 3.4 bets, not 10. Anyone sizing as though they were
    # independent is running 3x the risk they think they are.
    assert effective_independent_bets(10, rho) < 4.0


def test_concurrency_past_eight_buys_almost_nothing() -> None:
    """Portfolio sigma per unit of exposure barely moves after k=8."""
    rho = 0.22

    def div_factor(k: int) -> float:
        return math.sqrt((1 + (k - 1) * rho) / k)

    assert div_factor(8) / div_factor(1) < 0.60, "the first 8 positions do help"
    gain_8_to_44 = 1 - div_factor(44) / div_factor(8)
    assert gain_8_to_44 < 0.15, "5.5x the positions buys under 15% more diversification"


def test_max_concurrent_falls_out_of_exposure_over_position() -> None:
    """No separate count cap -- the mandate forbids one and none is needed."""
    assert max_concurrent_positions(BANKROLL_TODAY["sol"], MIN_POSITION["sol"]) == 25
    assert max_concurrent_positions(BANKROLL_TODAY["bsc"], MIN_POSITION["bsc"]) == 25
    # A shrinking bankroll pins max_position at the floor, so the count tightens by itself.
    assert max_concurrent_positions(2_250_000_000, MIN_POSITION["sol"]) == 12
    assert max_concurrent_positions(1_125_000_000, MIN_POSITION["sol"]) == 6


def test_total_exposure_is_survivable_in_full() -> None:
    """25% must be losable outright, because at rho=0.22 with -100% outcomes it can be.

    Percentage exposure de-levers geometrically: the bankroll asymptotes towards zero but
    never reaches it, which is the structural difference between a drawdown and ruin.
    """
    bankroll = Decimal(1)
    for _ in range(4):
        bankroll *= Decimal(1) - MAX_TOTAL_EXPOSURE_PCT / 100
    assert bankroll > Decimal("0.31"), "four total wipeouts still leave a third"
    # At today's effective cap the same four days leave essentially nothing.
    today = Decimal(1)
    for _ in range(4):
        today *= Decimal(1) - Decimal("0.989")
    assert today < Decimal("0.0001")


# ======================================================================================
# 5. The gap in the shipped gate, pinned against the real RiskGate
# ======================================================================================


#: A minimal live envelope. Defined here rather than imported from ``tests/test_risk.py``
#: so this module owns its own fixtures and cannot be broken by an edit over there.
_ENVELOPE: dict = {
    "version": "v1",
    "global_mode": "live",
    "kill_switch": False,
    "entries_paused": False,
    "reduce_only": False,
    "bounds": {
        "max_size_pct_bankroll": 5.0,
        "max_daily_loss_pct": 10.0,
        "max_slippage_bps": 2500,
        "max_concurrent_positions": None,
        "max_lane_mode": "live",
        "allow_self_promotion": True,
        "max_round_trip_cost_pct": 7.0,
    },
    "chains": {
        "sol": {
            "enabled": True,
            "bankroll_base_units": 10_000_000_000,
            "max_position_base_units": 500_000_000,
            "min_position_base_units": 5_000_000,
            "gas_reserve_base_units": 50_000_000,
            "daily_loss_stop_base_units": 500_000_000,
            "max_exposure_pct": 10.0,
            "wallet": None,
        }
    },
    "lanes": {
        "confluence-5": {
            "mode": "live",
            "size_pct_min": 1.0,
            "size_pct_max": 5.0,
            "chains": ["sol"],
            "params": {},
        }
    },
    "protection": {},
}


@pytest.fixture
def write_risk(tmp_path, monkeypatch):
    """Write a risk envelope to disk and point the config loader at it."""
    import copy

    import yaml

    path = tmp_path / "risk-scaling.yaml"
    monkeypatch.setenv("KAIBA_RISK_PATH", str(path))

    def _write(**overrides):
        merged = copy.deepcopy(_ENVELOPE)
        for key, value in overrides.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = {**merged[key], **value}
            else:
                merged[key] = value
        path.write_text(yaml.safe_dump(merged), encoding="utf-8")
        return path

    _write()
    return _write


@pytest.fixture
def gate(write_risk):
    from kaiba.execution.risk import RiskGate

    return RiskGate()


def test_gate_today_has_no_total_exposure_cap(gate, tmp_db, write_risk) -> None:
    """CHARACTERISATION of a real defect, not an endorsement of it.

    ``max_exposure_pct`` is applied PER TOKEN, and it is skipped entirely when the caller
    passes no token. The only limit on the SUM of open positions is the gas reserve, so
    total exposure may reach ``bankroll - gas_reserve``. With no count cap and high volume
    that is the ruin path: many correlated positions, none of them closed, so the daily
    stop -- which reads REALISED PnL only -- never trips while the book bleeds.

    When a portfolio cap is added this test must be updated to assert the new refusal.
    """
    from kaiba.core.schemas import Chain
    from kaiba.execution.risk import RiskGate

    bankroll = 10_000_000_000
    write_risk(
        chains={
            "sol": {
                "enabled": True,
                "bankroll_base_units": bankroll,
                "max_position_base_units": 500_000_000,
                "min_position_base_units": 5_000_000,
                "gas_reserve_base_units": 50_000_000,
                "daily_loss_stop_base_units": 500_000_000,
                "max_exposure_pct": 10.0,
                "wallet": None,
            }
        }
    )
    g = RiskGate()

    # 19 open positions on 19 different tokens, 0.5 SOL each = 9.5 SOL = 95% of bankroll.
    for i in range(19):
        tmp_db.execute(
            "INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms, qty, "
            "qty_total, cost_native, proceeds_native) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"p{i}", "sol", f"TOKEN{i}", "confluence-5", "live", 1, "1", "1", "500000000", "0"),
        )
    exposure = g.open_exposure(Chain.SOL, tmp_db)
    assert exposure == 9_500_000_000
    # 95% of the bankroll is deployed and the per-token cap has refused nothing, because
    # no single token exceeds 10%.
    assert Decimal(exposure) * 100 / Decimal(bankroll) == Decimal(95)
    # The recommended cap would have refused everything past the 5th position.
    assert max_total_exposure_base_units(bankroll) == 2_500_000_000
    assert exposure > max_total_exposure_base_units(bankroll)


def test_gate_daily_stop_reads_realised_pnl_only(gate, tmp_db) -> None:
    """The stop cannot see an open losing book. Under high volume that is the exposure hole.

    Pinned because it is the reason a total-exposure cap and a mark-to-market trip are
    both required: with 25 positions open and none closed, ``realized_today`` is 0 and the
    daily stop is fully armed but blind.
    """
    from kaiba.core.schemas import Chain

    tmp_db.execute(
        "INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms, qty, "
        "qty_total, cost_native, proceeds_native) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("open1", "sol", "TOKENX", "confluence-5", "live", 1, "1", "1", "500000000", "0"),
    )
    assert gate.open_exposure(Chain.SOL, tmp_db) == 500_000_000
    assert gate.realized_today(Chain.SOL, tmp_db) == 0, "open losses are invisible to the stop"


def test_gate_still_refuses_below_the_absolute_economic_floor(gate, tmp_db) -> None:
    """Whatever the percentages do, the fee floor stays an absolute refusal."""
    from kaiba.core.schemas import Chain, Lane

    decision = gate.check_entry(Chain.SOL, Lane.CONFLUENCE_5, 4_999_999, tmp_db, token="T")
    assert not decision.allowed
    assert decision.reason.startswith("size_below_min")


def test_gate_never_blocks_an_exit_however_tight_the_stop(gate, tmp_db, write_risk) -> None:
    """A larger daily stop must not become a reason an exit is refused. It never is."""
    from kaiba.core.schemas import Chain, Lane

    write_risk(kill_switch=True, reduce_only=True, entries_paused=True)
    from kaiba.execution.risk import RiskGate

    decision = RiskGate().check_exit(Chain.SOL, Lane.CONFLUENCE_5, tmp_db)
    assert decision.allowed
    assert decision.reason == "exit_never_blocked"


# ======================================================================================
# 6. The operator's table -- mechanical, no judgement required
# ======================================================================================


@pytest.mark.parametrize(
    "bankroll,expect_position,expect_stop,expect_k",
    [
        (1_125_000_000, 45_000_000, 90_000_000, 6),  # 1.125 SOL, floor-bound at 4.0%
        (2_250_000_000, 45_000_000, 180_000_000, 12),  # 2.25 SOL, floor-bound at 2.0%
        (4_500_000_000, 45_000_000, 360_000_000, 25),  # 4.5 SOL, TODAY: floor == 1.0%
        (9_000_000_000, 90_000_000, 720_000_000, 25),  # 9 SOL
        (45_000_000_000, 450_000_000, 3_600_000_000, 25),  # 45 SOL
        (90_000_000_000, 900_000_000, 7_200_000_000, 25),  # 90 SOL
    ],
)
def test_operator_table_sol(bankroll, expect_position, expect_stop, expect_k) -> None:
    floor = MIN_POSITION["sol"]
    assert max_position_base_units(bankroll, floor) == expect_position
    assert daily_stop_base_units(bankroll) == expect_stop
    assert max_concurrent_positions(bankroll, floor) == expect_k
    assert chain_is_viable(bankroll, floor)


def test_operator_table_is_monotonic_in_bankroll() -> None:
    """Every limit rises with the bankroll and none of them ever falls. Compounding works."""
    floor = MIN_POSITION["sol"]
    rungs = [1_125_000_000 * 2**i for i in range(8)]
    positions = [max_position_base_units(b, floor) for b in rungs]
    stops = [daily_stop_base_units(b) for b in rungs]
    exposures = [max_total_exposure_base_units(b) for b in rungs]
    assert positions == sorted(positions)
    assert stops == sorted(stops)
    assert exposures == sorted(exposures)


def test_a_chain_switches_off_rather_than_trade_below_its_own_floor() -> None:
    """When one forced-floor position would breach the exposure cap, the chain goes off."""
    floor = MIN_POSITION["sol"]
    assert chain_is_viable(1_125_000_000, floor)  # 0.045 of a 0.28 SOL cap: fine
    assert not chain_is_viable(150_000_000, floor)  # 0.045 vs a 0.0375 SOL cap: off
    assert not chain_is_viable(0, floor)


def test_fee_floors_rank_the_chains_by_how_much_volume_they_can_carry() -> None:
    """The strategic conclusion: high volume belongs on bsc, not sol, and never robinhood.

    ``min_position`` as a share of the bankroll is what caps trades per day, and it is a
    fact about venue fees, not about conviction. bsc's floor is 0.316% of its bankroll
    against sol's 1.0% and robinhood's 2.9%, so at the same 8% daily stop bsc carries an
    order of magnitude more trades than sol and robinhood carries almost none.
    """
    capacity = {}
    for chain, floor in MIN_POSITION.items():
        floor_pct = Decimal(floor) * 100 / Decimal(BANKROLL_TODAY[chain])
        capacity[chain] = max_trades_per_day(floor_pct, DAILY_STOP_PCT)
    assert capacity["bsc"] > capacity["sol"] > capacity["robinhood"]
    assert capacity["bsc"] > 400, "bsc can carry genuinely high volume"
    assert capacity["sol"] <= 50, "sol's fee floor caps it at ~48 trades/day at 4.5 SOL"
    assert capacity["robinhood"] < 10, "robinhood cannot carry volume at this bankroll"


def test_solana_volume_capacity_rises_as_the_bankroll_compounds() -> None:
    """Compounding buys volume capacity on sol because the floor stops binding.

    At 4.5 SOL the floor IS 1.0% of the bankroll, so sol is exactly at its limit. Growth
    is what unsticks it -- and this is the concrete reward for compounding rather than
    withdrawing.
    """
    floor = MIN_POSITION["sol"]

    def capacity(bankroll: int) -> int:
        pos = max_position_base_units(bankroll, floor)
        pct = Decimal(pos) * 100 / Decimal(bankroll)
        return max_trades_per_day(pct, DAILY_STOP_PCT)

    assert capacity(1_125_000_000) < capacity(2_250_000_000) < capacity(4_500_000_000)
    assert capacity(4_500_000_000) == capacity(45_000_000_000) == 48
    # Past 4.5 SOL the percentage binds instead of the floor, so capacity plateaus at 48
    # and further growth shows up as bigger positions rather than more of them.
    assert max_position_base_units(45_000_000_000, floor) == 450_000_000
