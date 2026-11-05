"""The arithmetic behind the DLMM paper position. Real money will be judged on these figures."""

from __future__ import annotations

import pytest

from kaiba.execution import lp_model as m

STEP = 100
SOL = "So11111111111111111111111111111111111111112"
ARM = m.Arm("t", 4, 60, 6.0, 2.0, 20)
ARM_R = m.Arm("r", 4, 60, 6.0, 2.0, 20, reopen_width=3, reopen_hold_s=120)


def row(b: int, x: int = 0, y: int = 1_000, supply: int | None = None, growth: tuple[int, int] = (0, 0)) -> dict:
    q = int(m.bin_price(b, STEP) * 2**64)
    return {"id": b, "amounts": [x, y], "price_q64": q, "supply": (y << 64) if supply is None else supply,
            "fee_growth": list(growth)}


def state(active: int = 0, bins: dict | None = None, at_ms: int = 0, missing: frozenset[int] = frozenset()) -> m.PoolState:
    bins = {b: row(b) for b in range(-80, 8)} if bins is None else bins
    return m.PoolState(slot=1, at_ms=at_ms, active=active, bin_step=STEP, bins=bins, missing_arrays=missing)


def opened(arm: m.Arm = ARM, ticket: int = 400) -> m.Plan:
    s = state()
    plan = m.plan_bins(arm, s, ticket)
    assert m.land(plan, s)
    return plan


def test_stakes_sit_strictly_below_the_active_bin() -> None:
    plan = opened()
    assert (plan.lo, plan.hi) == (-4, -1)
    assert sum(k.y for k in plan.stakes.values()) == 400


def test_above_the_range_the_position_is_all_sol() -> None:
    x, y = m.holdings(opened(), state(active=5))
    assert (x, y) == (0.0, 400.0)


def test_below_the_range_the_position_is_all_token_at_its_own_bin_prices() -> None:
    x, y = m.holdings(opened(), state(active=-9))
    assert y == 0.0
    assert x == pytest.approx(sum(100 / m.bin_price(b, STEP) for b in range(-4, 0)))


def test_the_active_bin_splits_as_the_bin_itself_does() -> None:
    p = m.bin_price(-2, STEP)
    bins = {b: row(b) for b in range(-80, 8)}
    bins[-2] = row(-2, x=int(750 / p), y=250)  # 75% of the bin's value is token
    x, y = m.holdings(opened(), state(active=-2, bins=bins))
    assert y == pytest.approx(100 + 100 + 25, abs=0.5)  # bins -4 and -3 untouched, 25% of -2
    assert x == pytest.approx(100 / m.bin_price(-1, STEP) + 75 / p, abs=0.5)


def test_fees_follow_growth_and_are_diluted_by_our_own_shares() -> None:
    plan = opened()
    bins = {b: row(b) for b in range(-80, 8)}
    bins[-1] = row(-1, growth=(0, 5 << 64))
    _, fee_y = m.fees(plan, state(bins=bins))
    assert fee_y == 500 * 1000 // 1100  # 100 shares of a 1,000-share bin that held 1,100 with us in it


def test_a_bin_missing_from_a_later_read_keeps_its_fees() -> None:
    plan = opened()
    bins = {b: row(b) for b in range(-80, 8)}
    bins[-1] = row(-1, growth=(0, 5 << 64))
    first = m.fees(plan, state(bins=bins))
    assert m.fees(plan, state(bins={b: r for b, r in bins.items() if b != -1})) == first


def test_price_falling_into_the_range_before_landing_is_a_miss() -> None:
    s = state()
    plan = m.plan_bins(ARM, s, 400)
    assert m.land(plan, state(active=plan.hi)) is False
    assert not plan.stakes


def test_an_uninitialised_array_is_charged_as_non_refundable_rent() -> None:
    s = state(missing=frozenset({m.array_index(-1)}))
    plan = m.plan_bins(ARM, s, m.LAMPORTS)
    assert plan.rent_arrays == 1
    m.land(plan, state())
    res = m.settle(plan, state(active=5), 0.02)
    assert res.rent_cost == m.BIN_ARRAY_RENT_LAMPORTS
    assert res.net_pct == pytest.approx(-7.14, abs=0.2)


def test_selling_walks_down_the_bins_pays_the_fee_and_reports_what_cannot_fill() -> None:
    bins = {0: row(0, y=100), -1: row(-1, y=50)}
    out, left = m.quote_sell(1_000.0, state(bins=bins), 0.02)
    assert out == pytest.approx(150, abs=1)
    assert left > 800
    out2, left2 = m.quote_sell(50.0, state(bins=bins), 0.02)
    assert out2 == pytest.approx(49.0, abs=0.1) and left2 == pytest.approx(0, abs=1e-6)


def test_settle_adds_up_and_a_token_exit_pays_for_its_swap() -> None:
    plan = opened(ticket=m.LAMPORTS)
    res = m.settle(plan, state(active=5), 0.02)
    assert res.tx_cost == 2 * m.TX_FEE_LAMPORTS  # nothing to sell
    deep = {b: row(b, y=10**12) for b in range(-80, 8)}
    plan2 = opened(ticket=m.LAMPORTS)
    res2 = m.settle(plan2, state(active=-9, bins=deep), 0.02)
    assert res2.tx_cost == 3 * m.TX_FEE_LAMPORTS
    assert res2.net == pytest.approx(res2.proceeds - plan2.ticket - res2.tx_cost - res2.rent_cost)
    assert res2.exit_cost > 0


@pytest.mark.parametrize("active, at_ms, want", [(-9, 1000, "below_range"), (5, 1000, None), (0, 61_000, "max_hold")])
def test_exit_reasons(active: int, at_ms: int, want: str | None) -> None:
    assert m.exit_reason(opened(), state(active=active, at_ms=at_ms)) == want


def test_the_stop_and_the_take_read_the_mark_including_fees() -> None:
    bins = {b: row(b) for b in range(-80, 8)}
    bins[-1] = row(-1, growth=(0, 1 << 64))
    plan = opened(ticket=400)
    assert m.exit_reason(plan, state(active=0, bins=bins)) == "take"
    wide = m.Arm("w", 40, 60, 6.0, 2.0, 20)
    assert m.exit_reason(opened(wide, 40_000), state(active=-30)) == "stop"


def test_idle_above_the_range_needs_a_continuous_stretch() -> None:
    plan = opened()
    assert m.exit_reason(plan, state(active=5, at_ms=1000)) is None
    assert m.exit_reason(plan, state(active=-1, at_ms=2000)) is None  # back in range
    assert plan.above_since_ms is None
    assert m.exit_reason(plan, state(active=5, at_ms=3000)) is None
    assert m.exit_reason(plan, state(active=5, at_ms=24_000)) == "idle_above"


def test_reopen_keeps_the_value_it_had_and_counts_its_transactions() -> None:
    plan = opened(ARM_R, 400_000)
    s = state(active=-2)
    before = m.value(plan, s).mark
    m.reopen(plan, s)
    assert (plan.lo, plan.hi) == (-5, 1)
    assert -2 not in plan.stakes  # no composition fee
    assert m.value(plan, s).mark == pytest.approx(before, rel=1e-4)
    assert plan.reopened and plan.extra_txs == 2


def test_reopen_only_once_only_when_holding_mostly_token_and_never_for_a_clean_exit() -> None:
    plan = opened(ARM_R)
    low = state(active=-9)
    assert m.wants_reopen(plan, "below_range", low)
    assert not m.wants_reopen(plan, "max_hold", low)
    assert not m.wants_reopen(opened(ARM_R), "stop", state(active=5))
    assert not m.wants_reopen(opened(ARM), "below_range", low)
    m.reopen(plan, low)
    assert not m.wants_reopen(plan, "below_range", state(active=-30))


def test_a_reopened_position_uses_its_own_hold_limit() -> None:
    plan = opened(ARM_R)
    m.reopen(plan, state(active=-2, at_ms=1000))
    assert m.exit_reason(plan, state(active=-2, at_ms=70_000)) is None
    assert m.exit_reason(plan, state(active=-2, at_ms=122_000)) == "max_hold"


POOL = {"token_y": {"address": SOL}, "token_x": {"freeze_authority_disabled": True}, "tvl": 9_000,
        "pool_config": {"bin_step": 100, "base_fee_pct": 2.0}, "volume": {"30m": 20_000}, "fee_tvl_ratio": {"30m": 1.0}}


@pytest.mark.parametrize("patch, why", [
    ({}, None), ({"is_blacklisted": True}, "blacklisted"), ({"token_y": {"address": "x"}}, "quote_not_sol_y"),
    ({"token_x": {}}, "freeze_authority"), ({"tvl": 100}, "tvl"), ({"volume": {"30m": 5}}, "volume"),
    ({"fee_tvl_ratio": {"30m": 0.1}}, "fee_tvl"), ({"pool_config": {"bin_step": 200, "base_fee_pct": 2.0}}, "bin_step"),
    ({"pool_config": {"bin_step": 100, "base_fee_pct": 5.0}}, "base_fee"),
])
def test_the_selector_names_why_a_pool_is_out(patch: dict, why: str | None) -> None:
    assert m.eligible({**POOL, **patch}, m.SelectorConfig(), SOL) == why


def test_a_two_sided_open_holds_both_sides_and_loses_only_the_swap_fee() -> None:
    arm = m.Arm("w", 3, 60, 15.0, 6.0, 600, two_sided=3)
    s = state()
    plan = m.plan_bins(arm, s, 1_000_000)
    assert (plan.lo, plan.hi) == (-3, 3)
    assert m.land(plan, s, 0.02)
    x, y = m.holdings(plan, s)
    assert y == pytest.approx(500_000, abs=5)
    assert x == pytest.approx(490_000, rel=1e-4)  # half the ticket, less 2%, at price 1
    assert plan.extra_txs == m.SWAP_TXS
    assert m.value(plan, s).mark == pytest.approx(990_000, rel=1e-4)


def test_a_two_sided_position_ends_all_sol_above_and_all_token_below() -> None:
    arm = m.Arm("w", 3, 60, 15.0, 6.0, 600, two_sided=3)
    s = state()
    plan = m.plan_bins(arm, s, 1_000_000)
    m.land(plan, s, 0.0)
    assert m.holdings(plan, state(active=9))[0] == 0.0
    assert m.holdings(plan, state(active=-9))[1] == 0.0


def candle(high: float, low: float, close: float, volume: float = 100.0) -> dict:
    return {"high": high, "low": low, "close": close, "volume": volume}


RUN = [candle(1.2, 1.0, 1.1), candle(4.0, 1.1, 3.5, 900.0), candle(10.0, 3.5, 9.0, 1000.0), candle(9.0, 5.0, 5.5, 800.0),
       candle(5.5, 3.5, 3.8, 300.0), candle(3.9, 3.2, 3.4, 200.0), candle(3.6, 3.1, 3.3, 150.0)]


def test_a_run_a_fall_and_faded_selling_is_the_setup() -> None:
    why, measures = m.dump_setup(RUN)
    assert why is None
    assert measures["run_x"] == 10.0 and measures["drop"] == 0.67 and measures["hours_since_top"] == 4


@pytest.mark.parametrize("candles, why", [
    (RUN[:3], "setup_short_history"),
    ([candle(1.2, 1.0, 1.1)] * 7, "setup_no_run"),
    (RUN[:3] + [candle(9.0, 2.9, 3.0), candle(3.5, 2.9, 3.2, 90.0), candle(10.5, 3.0, 3.2, 90.0)], "setup_top_too_recent"),
    (RUN[:-1] + [candle(3.6, 1.5, 1.6, 150.0)], "setup_drop_out_of_band"),
    (RUN[:-1] + [candle(6.0, 3.1, 5.5, 150.0)], "setup_drop_out_of_band"),
    (RUN[:-2] + [candle(3.9, 3.2, 3.4, 900.0), candle(3.6, 3.1, 3.3, 900.0)], "setup_selling_not_faded"),
])
def test_the_dump_setup_names_why_it_is_not_one(candles: list, why: str) -> None:
    assert m.dump_setup(candles)[0] == why


@pytest.mark.parametrize("step, bins", [(100, 70), (200, 36), (50, 139), (250, 29)])
def test_a_price_sized_range_doubles_whatever_the_bin_step(step: int, bins: int) -> None:
    arm = m.Arm("d", 0, 60, 40.0, 15.0, 600, range_x=2.0)
    assert m.half_width(arm, step) == bins
    assert (1 + step / 10_000) ** bins >= 2.0 > (1 + step / 10_000) ** (bins - 1)


def test_a_price_sized_arm_opens_two_sided_on_the_pools_own_step() -> None:
    arm = m.Arm("d", 0, 60, 40.0, 15.0, 600, range_x=2.0)
    s = m.PoolState(slot=1, at_ms=0, active=0, bin_step=STEP, bins={b: row(b) for b in range(-90, 90)})
    plan = m.plan_bins(arm, s, 1_000_000)
    assert (plan.lo, plan.hi) == (-70, 70)
    assert m.land(plan, s, 0.02) and len(plan.stakes) == 140 and 0 not in plan.stakes



FILTERED_CFG = m.SelectorConfig(max_bin_step=250, skip_bin_steps=(81, 125), min_age_h=1.0)
NOW_MS = 10 * 3_600_000


@pytest.mark.parametrize(("step", "why"), [(80, None), (81, "bin_step_skipped"), (100, "bin_step_skipped"), (125, "bin_step_skipped"), (126, None), (200, None), (251, "bin_step")])
def test_the_skipped_steps_include_both_ends(step: int, why: str | None) -> None:
    pool = {**POOL, "pool_config": {"bin_step": step, "base_fee_pct": 2.0}, "created_at": 1}
    assert m.eligible(pool, FILTERED_CFG, SOL, NOW_MS) == why


def test_a_pool_too_new_or_of_unknown_age_is_refused() -> None:
    pool = {**POOL, "pool_config": {"bin_step": 200, "base_fee_pct": 2.0}}
    assert m.eligible({**pool, "created_at": NOW_MS - 3_599_000}, FILTERED_CFG, SOL, NOW_MS) == "too_new"
    assert m.eligible({**pool, "created_at": NOW_MS - 3_600_000}, FILTERED_CFG, SOL, NOW_MS) is None
    assert m.eligible({**pool, "created_at": 0}, FILTERED_CFG, SOL, NOW_MS) == "age_unknown"
    assert m.eligible({**pool, "created_at": 1}, FILTERED_CFG, SOL) == "age_unknown"


def test_with_the_new_settings_off_the_selector_reads_neither_age_nor_clock() -> None:
    assert m.eligible({**POOL, "created_at": 0}, m.SelectorConfig(), SOL) is None


@pytest.mark.parametrize(("closes", "why"), [([1.0, 0.81], None), ([1.0, 0.80], "dumped_last_hour"), ([1.0, 0.5], "dumped_last_hour"), ([1.0, 1.5], None), ([1.0], "setup_short_history"), ([], "setup_short_history")])
def test_a_recent_dump_is_read_from_the_last_two_closes(closes: list[float], why: str | None) -> None:
    assert m.recent_dump([{"close": c} for c in closes])[0] == why


def test_only_the_last_hour_counts_as_recent() -> None:
    assert m.recent_dump([{"close": 3.0}, {"close": 1.0}, {"close": 1.1}]) == (None, {"prior_1h": 0.1})

