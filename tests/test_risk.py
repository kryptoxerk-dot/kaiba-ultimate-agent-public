"""The tunable gate: sizing, the daily loss stop, and the exit ladder.

Everything here is a number the agent may move. The tests pin the behaviour that must not
change when it does: entries stop when a brake is on, exits never do, the ladder cannot
fire a rung twice, and a trailing stop only ever rises.
"""

from __future__ import annotations

import copy
from decimal import Decimal

import pytest
import yaml

from kaiba.core.schemas import Chain, Lane, now_ms
from kaiba.execution.protection import (
    ProtectionAction,
    ProtectionConfig,
    ProtectionKind,
    ProtectionState,
    evaluate,
    initial_state,
    next_stop,
    to_gmgn_condition_orders,
)
from kaiba.execution.risk import RiskGate, day_key, score_fraction

SOL = Chain.SOL
LANE = Lane.CONFLUENCE_5
SMALL_LANE = Lane.KOL_FADE
TOKEN = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
OTHER_TOKEN = "CJF7MNqb9xv1XrTs5St1Du5JuQXLsfFBB137vmYRKpnb"

BANKROLL = 10_000_000_000  # 10 SOL in lamports

BASE_RISK: dict = {
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
    },
    "chains": {
        "sol": {
            "enabled": True,
            "bankroll_base_units": BANKROLL,
            "max_position_base_units": 500_000_000,
            "min_position_base_units": 5_000_000,
            "gas_reserve_base_units": 50_000_000,
            "daily_loss_stop_base_units": 500_000_000,
            "max_exposure_pct": 10.0,
            "wallet": None,
        },
        "bsc": {
            "enabled": True,
            "bankroll_base_units": 10**18,
            "max_position_base_units": 10**16,
            "min_position_base_units": 10**14,
            "gas_reserve_base_units": 10**15,
            "daily_loss_stop_base_units": 10**16,
            "max_exposure_pct": 10.0,
            "wallet": None,
        },
    },
    "lanes": {
        "confluence-5": {
            "mode": "live", "size_pct_min": 1.0, "size_pct_max": 5.0, "chains": ["sol"], "params": {}
        },
        "kol-fade": {
            "mode": "live", "size_pct_min": 0.5, "size_pct_max": 1.0, "chains": ["sol"], "params": {}
        },
        "migration-fade": {
            "mode": "off", "size_pct_min": 0.25, "size_pct_max": 1.0, "chains": ["sol"], "params": {}
        },
    },
    "protection": {
        "poll_interval_s": 5,
        "use_provider_orders": True,
        "stop_loss_bps": 3000,
        "tp_ladder": [[2.0, 50], [5.0, 25], [10.0, 15]],
        "trailing": [[2.0, 3000], [5.0, 2500], [10.0, 2000], [25.0, 1500], [100.0, 1000]],
        "breakeven_after_tp1": True,
        "anti_wick_min_ratio": 0.7,
        "rug_liquidity_drop_pct": 40,
        "emergency_loss_bps": 5000,
    },
}


def _merge(base: dict, overrides: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


@pytest.fixture
def write_risk(tmp_path, monkeypatch):
    """Write a risk envelope to disk and point the config loader at it."""
    path = tmp_path / "risk.yaml"
    monkeypatch.setenv("KAIBA_RISK_PATH", str(path))

    def _write(**overrides):
        path.write_text(yaml.safe_dump(_merge(BASE_RISK, overrides)), encoding="utf-8")
        return path

    _write()
    return _write


@pytest.fixture
def gate(write_risk) -> RiskGate:
    return RiskGate()


def seed_depth(conn, token: str = TOKEN) -> None:
    """Give the token a curve so the sizing gate can price its depth.

    Depth is per token, not per chain, and `viability` refuses a size it cannot price --
    unknown depth is not free depth. Without this the gate correctly answers
    `round_trip_cost_unknown`, so any test asserting an entry is *allowed* has to say
    what pool it is entering.
    """
    conn.execute(
        "INSERT INTO curve_snapshots (chain, token, observed_ms, real_sol_lamports, "
        "virtual_sol_lamports, real_token_atoms, virtual_token_atoms, created_ms, source) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (SOL.value, token, now_ms(), "55780466117", "85780466117",
         "95360268161085", "375260268161085", now_ms(), "pumpfun"),
    )


def open_position(conn, token: str, cost: int, proceeds: int = 0, chain: Chain = SOL) -> None:
    conn.execute(
        "INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms, qty, qty_total, "
        "cost_native, proceeds_native) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (f"p-{token}-{cost}", chain.value, token, LANE.value, "live", now_ms(), "1", "1",
         str(cost), str(proceeds)),
    )


def close_position(conn, token: str, realized_native: int, *, mode: str = "live", chain: str = "sol") -> None:
    """A closed round trip with a realised PnL, closed NOW, on the positions table.

    ``realized_today`` reads this table (2026-09-21); it no longer reads the
    ``risk_state`` ledger that ``record_fill`` writes, because ``record_fill`` has zero
    production callers and that ledger was empty after 57 real closed trades.
    """
    import time as _t
    import uuid as _uuid
    now = int(_t.time() * 1000)
    # uuid, not a timestamp: two closes in the same millisecond with the same token
    # prefix collided on the PRIMARY KEY, and the collision only showed under a mutation
    # run -- the normal run passed by timing luck. A fixture that can flake is not a fixture.
    conn.execute(
        "INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms, closed_ms, "
        "qty, qty_total, cost_native, proceeds_native, realized_native) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"pos_{_uuid.uuid4().hex[:12]}", chain, token, LANE.value, mode,
         now - 60_000, now, 0, 1, 100_000_000, 100_000_000 + realized_native, realized_native),
    )
    conn.commit()


# ------------------------------------------------------------------ the score ladder


@pytest.mark.parametrize(
    "score,fraction",
    [(0, "0"), (69.9, "0"), (70, "0.25"), (79.9, "0.25"), (80, "0.50"), (89.9, "0.50"),
     (90, "0.75"), (94.9, "0.75"), (95, "1.00"), (100, "1.00")],
)
def test_score_ladder_bands(score, fraction):
    """A 60-70 rung was tried on 2026-09-23 and reverted; see SCORE_LADDER for why.

    Conviction IS anti-calibrated in the range we trade (70-80 returned -10.0% at a 40%
    win rate against 80-90's -20.9%/14%), which argues for admitting lower bands. What
    killed it is that there is no cheap way to try: `min_position_base_units` clamps a
    0.10x rung back up, and on robinhood 0.10x and 0.25x produce the IDENTICAL size.
    """
    assert score_fraction(score) == Decimal(fraction)


def test_the_bottom_of_the_ladder_is_still_a_refusal():
    """Below 70 there is no size. Widening it needs a size that can actually be small."""
    assert score_fraction(69.99) == Decimal(0)
    assert score_fraction(0) == Decimal(0)


def test_any_new_bottom_rung_must_be_cheaper_than_the_floor_it_would_hit():
    """The trap that reverted the 60-70 rung, kept as a check rather than a memory.

    A rung whose size is clamped up to `min_position_base_units` is not a small position,
    it is a normal one wearing a small number. On robinhood 0.10x and 0.25x both resolve
    to 0.004350 -- identical -- so a "cheap probe" there does not exist.
    """
    from kaiba.execution.risk import SCORE_LADDER

    assert min(float(t) for t, _ in SCORE_LADDER) >= 70.0, (
        "a rung below 70 was added; confirm its size survives min_position_base_units "
        "on EVERY chain before trusting it to be a cheap measurement"
    )


@pytest.mark.parametrize(
    "score,expected",
    [(95.0, 500_000_000), (90.0, 375_000_000), (80.0, 250_000_000), (70.0, 125_000_000), (69.0, 0)],
)
def test_position_size_at_each_band(gate, tmp_db, score, expected):
    assert gate.position_size(SOL, LANE, score, tmp_db) == expected


def test_position_size_respects_the_lane_minimum(gate, tmp_db):
    """25% of a 1% lane is 0.25%, below the lane's own 0.5% floor — the floor wins."""
    assert gate.position_size(SOL, SMALL_LANE, 70.0, tmp_db) == 50_000_000


def test_position_size_is_clamped_by_the_operator_envelope(write_risk, tmp_db):
    write_risk(bounds={"max_size_pct_bankroll": 2.0})
    assert RiskGate().position_size(SOL, LANE, 95.0, tmp_db) == 200_000_000


def test_position_size_is_capped_by_max_position(write_risk, tmp_db):
    write_risk(chains={"sol": {"max_position_base_units": 100_000_000}})
    assert RiskGate().position_size(SOL, LANE, 95.0, tmp_db) == 100_000_000


def test_position_size_leaves_the_gas_reserve_alone(write_risk, tmp_db):
    write_risk(chains={"sol": {"gas_reserve_base_units": 9_900_000_000}})
    # 10 SOL bankroll, 9.9 reserved, so at most 0.1 SOL is spendable.
    assert RiskGate().position_size(SOL, LANE, 95.0, tmp_db) == 100_000_000


def test_position_size_refuses_when_the_basket_is_at_the_aggregate_cap(gate, tmp_db):
    """This test used to assert a 0.25 SOL entry with 9.7 of 10 SOL already deployed.

    That assertion WAS the ruin path: max_exposure_pct is per token and
    max_concurrent_positions is enforced nowhere, so a simulated high-volume run admitted
    22 simultaneous positions = 97.8% of the bankroll (MEASURED 2026-09-21). Positions on
    one chain in one hour are one factor (pairwise rho +0.22), not N bets. The basket is
    now capped at max_total_exposure_pct (default 25%) of the bankroll. At 97% deployed
    there is no room, and the right answer is zero.
    """
    open_position(tmp_db, TOKEN, 9_700_000_000)
    assert gate.position_size(SOL, LANE, 95.0, tmp_db) == 0


def test_position_size_subtracts_open_exposure_under_the_cap(gate, tmp_db):
    """The original intent, kept: open exposure is netted out of what a new entry may use.

    2.0 SOL open on a 10 SOL bankroll is under the 25% cap (2.5 SOL), so 0.5 SOL of room
    remains and the size is bounded by that room, not by the lane maximum.
    """
    open_position(tmp_db, TOKEN, 2_000_000_000)
    size = gate.position_size(SOL, LANE, 95.0, tmp_db)
    assert 0 < size <= 500_000_000, f"room under the 25% cap is 0.5 SOL, got {size}"


def test_position_size_is_zero_on_an_unfunded_chain(write_risk, tmp_db):
    write_risk(chains={"sol": {"bankroll_base_units": 0}})
    assert RiskGate().position_size(SOL, LANE, 99.0, tmp_db) == 0


def test_position_size_is_zero_for_a_lane_that_is_off(gate, tmp_db):
    assert gate.position_size(SOL, Lane.MIGRATION_FADE, 99.0, tmp_db) == 0


def test_position_size_is_zero_under_the_kill_switch(write_risk, tmp_db):
    write_risk(kill_switch=True)
    assert RiskGate().position_size(SOL, LANE, 99.0, tmp_db) == 0


def test_position_size_is_zero_when_the_remainder_is_below_the_minimum(write_risk, tmp_db):
    write_risk(chains={"sol": {"min_position_base_units": 600_000_000}})
    assert RiskGate().position_size(SOL, LANE, 95.0, tmp_db) == 0


# ------------------------------------------------------------------ entry admission


def test_entry_inside_the_envelope_is_allowed(gate, tmp_db):
    seed_depth(tmp_db)
    decision = gate.check_entry(SOL, LANE, 100_000_000, tmp_db, token=TOKEN)
    assert decision.allowed, decision.reason
    assert "mode:live" in decision.findings


def test_kill_switch_blocks_entries(write_risk, tmp_db):
    write_risk(kill_switch=True)
    decision = RiskGate().check_entry(SOL, LANE, 100_000_000, tmp_db)
    assert not decision.allowed and decision.reason == "kill_switch"


def test_entries_paused_blocks_entries(write_risk, tmp_db):
    write_risk(entries_paused=True)
    decision = RiskGate().check_entry(SOL, LANE, 100_000_000, tmp_db)
    assert not decision.allowed and decision.reason == "entries_paused"


def test_reduce_only_blocks_entries(write_risk, tmp_db):
    write_risk(reduce_only=True)
    decision = RiskGate().check_entry(SOL, LANE, 100_000_000, tmp_db)
    assert not decision.allowed and decision.reason == "reduce_only"


def test_reduce_only_still_allows_exits(write_risk, tmp_db):
    write_risk(reduce_only=True, kill_switch=True)
    decision = RiskGate().check_exit(SOL, LANE, tmp_db)
    assert decision.allowed
    assert "reduce_only_active" in decision.findings and "kill_switch_active" in decision.findings


def test_lane_that_is_off_blocks_entries(gate, tmp_db):
    decision = gate.check_entry(SOL, Lane.MIGRATION_FADE, 100_000_000, tmp_db)
    assert not decision.allowed and decision.reason == "lane_off"


def test_global_mode_ceiling_blocks_entries(write_risk, tmp_db):
    write_risk(global_mode="off")
    decision = RiskGate().check_entry(SOL, LANE, 100_000_000, tmp_db)
    assert not decision.allowed and decision.reason == "lane_off"


def test_disabled_chain_blocks_entries(write_risk, tmp_db):
    write_risk(chains={"sol": {"enabled": False}})
    decision = RiskGate().check_entry(SOL, LANE, 100_000_000, tmp_db)
    assert not decision.allowed and decision.reason == "chain_disabled"


def test_lane_not_enabled_for_the_chain_blocks_entries(gate, tmp_db):
    decision = gate.check_entry(Chain.BSC, LANE, 10**15, tmp_db)
    assert not decision.allowed and decision.reason == "lane_chain_not_enabled"


def test_size_below_the_minimum_is_refused(gate, tmp_db):
    decision = gate.check_entry(SOL, LANE, 1_000_000, tmp_db)
    assert not decision.allowed and decision.reason.startswith("size_below_min")


def test_size_above_the_maximum_is_refused(gate, tmp_db):
    decision = gate.check_entry(SOL, LANE, 900_000_000, tmp_db)
    assert not decision.allowed and decision.reason.startswith("size_above_max")


def test_size_above_the_bankroll_clamp_is_refused(write_risk, tmp_db):
    write_risk(chains={"sol": {"max_position_base_units": 5_000_000_000}})
    decision = RiskGate().check_entry(SOL, LANE, 900_000_000, tmp_db)  # 9% of a 5% envelope
    assert not decision.allowed and decision.reason.startswith("size_above_clamp")


def test_zero_size_is_refused(gate, tmp_db):
    assert RiskGate().check_entry(SOL, LANE, 0, tmp_db).reason == "size_not_positive"


def test_unfunded_bankroll_is_not_infinite(write_risk, tmp_db):
    write_risk(chains={"sol": {"bankroll_base_units": 0}})
    decision = RiskGate().check_entry(SOL, LANE, 10_000_000, tmp_db)
    assert not decision.allowed and decision.reason == "bankroll_unfunded"


def test_gas_reserve_is_preserved(write_risk, tmp_db):
    write_risk(chains={"sol": {"gas_reserve_base_units": 9_900_000_000}})
    decision = RiskGate().check_entry(SOL, LANE, 200_000_000, tmp_db)
    assert not decision.allowed and decision.reason.startswith("gas_reserve")


def test_per_token_exposure_cap(gate, tmp_db):
    seed_depth(tmp_db)
    open_position(tmp_db, TOKEN, 900_000_000)  # 9% of bankroll already in this token
    decision = gate.check_entry(SOL, LANE, 200_000_000, tmp_db, token=TOKEN)
    assert not decision.allowed and decision.reason.startswith("max_exposure_pct")


def test_exposure_cap_is_per_token_not_global(gate, tmp_db):
    seed_depth(tmp_db)
    open_position(tmp_db, OTHER_TOKEN, 900_000_000)
    decision = gate.check_entry(SOL, LANE, 200_000_000, tmp_db, token=TOKEN)
    assert decision.allowed, decision.reason


def test_exposure_already_realised_does_not_double_count(gate, tmp_db):
    seed_depth(tmp_db)
    open_position(tmp_db, TOKEN, 900_000_000, proceeds=850_000_000)
    decision = gate.check_entry(SOL, LANE, 200_000_000, tmp_db, token=TOKEN)
    assert decision.allowed, decision.reason


def test_entry_without_a_token_says_so_rather_than_pretending(gate, tmp_db):
    decision = gate.check_entry(SOL, LANE, 100_000_000, tmp_db)
    assert decision.allowed
    assert "exposure_check_skipped_no_token" in decision.findings


# ------------------------------------------------------------------ the daily loss stop


def test_daily_loss_stop_halts_entries_but_still_allows_exits(gate, tmp_db):
    """The daily brake reads realised PnL from CLOSED POSITIONS, the table with the data.

    Until 2026-09-21 it read risk_state via record_fill, which nothing in production
    calls -- so this brake had never once fired on a real loss. Same assertions as
    before, fed by the data the box actually holds.
    """
    seed_depth(tmp_db)  # so the ONLY reason this can refuse is the daily stop
    close_position(tmp_db, TOKEN, -500_000_000)
    entry = gate.check_entry(SOL, LANE, 100_000_000, tmp_db, token=TOKEN)
    assert not entry.allowed and entry.reason == "daily_loss_stop", entry.reason
    exit_ = gate.check_exit(SOL, LANE, tmp_db)
    assert exit_.allowed and "daily_loss_stop_active" in exit_.findings


def test_loss_below_the_stop_still_trades(gate, tmp_db):
    seed_depth(tmp_db)
    close_position(tmp_db, TOKEN, -100_000_000)
    assert gate.check_entry(SOL, LANE, 100_000_000, tmp_db, token=TOKEN).allowed


def test_realized_today_accumulates_closed_positions_within_the_day(gate, tmp_db):
    close_position(tmp_db, TOKEN, -200_000_000)
    close_position(tmp_db, TOKEN + "b", -200_000_000)
    close_position(tmp_db, TOKEN + "c", 50_000_000)
    assert gate.realized_today(SOL, tmp_db) == -350_000_000


def test_realized_today_ignores_paper_positions(gate, tmp_db):
    """A shadow loss must not halt live entries; a shadow win must not license them."""
    close_position(tmp_db, TOKEN, -900_000_000, mode="shadow")
    assert gate.realized_today(SOL, tmp_db) == 0


def test_realized_today_ignores_still_open_positions(gate, tmp_db):
    """Only REALISED PnL counts here; open drawdown is the aggregate cap's job."""
    open_position(tmp_db, TOKEN, 1_000_000_000)
    assert gate.realized_today(SOL, tmp_db) == 0


def test_record_fill_is_keyed_by_utc_day(gate, tmp_db):
    gate.record_fill(SOL, -400_000_000, tmp_db, is_entry=True)
    row = tmp_db.execute("SELECT day_key, entries FROM risk_state").fetchone()
    assert row["day_key"] == day_key() and row["entries"] == 1


def test_record_fill_keeps_chains_separate(gate, tmp_db):
    gate.record_fill(SOL, -400_000_000, tmp_db)
    assert gate.realized_today(Chain.BSC, tmp_db) == 0


def test_halt_blocks_entries_and_resume_releases_them(gate, tmp_db):
    seed_depth(tmp_db)
    gate.halt("provider_outage", tmp_db)
    decision = gate.check_entry(SOL, LANE, 100_000_000, tmp_db, token=TOKEN)
    assert not decision.allowed and decision.reason.startswith("halted:provider_outage")
    assert gate.check_exit(SOL, LANE, tmp_db).allowed
    gate.resume(tmp_db)
    assert gate.check_entry(SOL, LANE, 100_000_000, tmp_db, token=TOKEN).allowed


def test_daily_summary_reports_the_loss_budget(gate, tmp_db):
    """The budget is reported from the number the GATE enforces with.

    Changed 2026-09-23. This used to drive `record_fill` and read the figure straight back
    out of the summary, which passed only because both ends touched the same stored blob.
    `check_entry` has refused entries on `realized_today` -- the sum of closed positions --
    since 2026-09-21, and on the live box the two had drifted to -0.213516 against
    -0.554208 on the same chain at the same second, so the summary said "not stopped" over
    41 refusals in an hour. The per-fill ledger is still reported, under its own name.
    """
    gate.record_fill(SOL, -200_000_000, tmp_db, is_entry=True)
    summary = gate.daily_summary(tmp_db)
    assert summary["day"] == day_key()
    assert summary["entries"] == 1 and summary["halted"] is False
    sol = summary["chains"]["sol"]
    assert sol["realized_native_stored"] == -200_000_000, "the ledger is still recorded"
    assert sol["realized_native"] == gate.realized_today(SOL, tmp_db)
    assert sol["stopped"] is False


def test_the_gate_rereads_the_config_on_every_check(write_risk, tmp_db):
    """The file is edited while the agent runs; a cached config would keep trading."""
    seed_depth(tmp_db)
    gate = RiskGate()
    assert gate.check_entry(SOL, LANE, 100_000_000, tmp_db, token=TOKEN).allowed
    write_risk(kill_switch=True)
    assert gate.check_entry(SOL, LANE, 100_000_000, tmp_db, token=TOKEN).reason == "kill_switch"


def test_a_brake_emits_a_risk_halt_event(write_risk, tmp_db):
    from kaiba.core import events
    from kaiba.core.schemas import EventKind

    write_risk(kill_switch=True)
    before = events.latest_id(conn=tmp_db)
    RiskGate().check_entry(SOL, LANE, 100_000_000, tmp_db)
    rows = events.tail(after_id=before, conn=tmp_db)
    assert rows and rows[-1].kind is EventKind.RISK_HALT and rows[-1].level == "warn"


# ------------------------------------------------------------------ protection


@pytest.fixture
def pcfg() -> ProtectionConfig:
    return ProtectionConfig.model_validate(BASE_RISK["protection"])


def test_protection_config_comes_from_the_risk_file(write_risk):
    from kaiba.execution.protection import protection_config

    write_risk(protection={"stop_loss_bps": 1234, "anti_wick_min_ratio": 0.9})
    cfg = protection_config()
    assert cfg.stop_loss_bps == 1234 and cfg.anti_wick_min_ratio == Decimal("0.9")


def test_initial_state_arrives_already_stopped(pcfg):
    state = initial_state("pos1", "1.0", pcfg)
    assert state.stop_price == Decimal("0.70")
    assert state.peak_price == Decimal("1.0")


def test_rug_monitor_exits_on_a_single_interval_lp_collapse(pcfg):
    state = initial_state("pos1", 1, pcfg)
    action = evaluate(state, price_usd=3, liquidity_usd=50_000, prev_liquidity_usd=100_000, cfg=pcfg)
    assert action.kind is ProtectionKind.EXIT_ALL and action.reason.startswith("rug")


def test_rug_monitor_ignores_a_shallow_lp_dip(pcfg):
    state = initial_state("pos1", 1, pcfg)
    action = evaluate(state, price_usd=1.1, liquidity_usd=90_000, prev_liquidity_usd=100_000, cfg=pcfg)
    assert action.kind is ProtectionKind.HOLD


def test_emergency_loss_beats_everything(pcfg):
    """Even with a take-profit live and liquidity healthy, -50% exits."""
    state = initial_state("pos1", 1, pcfg)
    action = evaluate(
        state, price_usd="0.5", liquidity_usd=100_000, prev_liquidity_usd=100_000, cfg=pcfg
    )
    assert action.kind is ProtectionKind.EXIT_ALL and action.reason == "emergency_loss"


def test_rug_beats_emergency_loss(pcfg):
    state = initial_state("pos1", 1, pcfg)
    action = evaluate(state, price_usd="0.4", liquidity_usd=10_000, prev_liquidity_usd=100_000, cfg=pcfg)
    assert action.reason.startswith("rug")


def test_hard_stop_fires_before_tp1(pcfg):
    state = initial_state("pos1", 1, pcfg)
    action = evaluate(state, price_usd="0.69", cfg=pcfg)
    assert action.kind is ProtectionKind.EXIT_ALL and action.reason == "stop_loss"


def test_tp_ladder_fires_once_per_rung(pcfg):
    state = initial_state("pos1", 1, pcfg)
    first = evaluate(state, price_usd=2, cfg=pcfg)
    assert first.kind is ProtectionKind.TRIM and first.pct == Decimal(50) and first.tp_tag == "tp1"
    again = evaluate(state, price_usd=2, cfg=pcfg)
    assert again.kind is ProtectionKind.HOLD
    second = evaluate(state, price_usd=5, cfg=pcfg)
    assert second.kind is ProtectionKind.TRIM and second.pct == Decimal(25) and second.tp_tag == "tp2"
    assert state.tp_done == ["tp1", "tp2"]


def test_tp_ladder_fires_lowest_rung_first_on_a_gap(pcfg):
    state = initial_state("pos1", 1, pcfg)
    assert evaluate(state, price_usd=20, cfg=pcfg).tp_tag == "tp1"
    assert evaluate(state, price_usd=20, cfg=pcfg).tp_tag == "tp2"
    assert evaluate(state, price_usd=20, cfg=pcfg).tp_tag == "tp3"
    assert evaluate(state, price_usd=20, cfg=pcfg).kind is ProtectionKind.HOLD


def test_breakeven_locks_after_tp1(pcfg):
    state = initial_state("pos1", 1, pcfg)
    evaluate(state, price_usd=2, cfg=pcfg)
    assert state.stop_price >= state.entry_price


def test_anti_wick_holds_a_take_profit(pcfg):
    """The chart says 3x; a real sell quote says 1.5x. The chart wick is not a fill."""
    state = initial_state("pos1", 1, pcfg)
    action = evaluate(state, price_usd=3, executable_quote_usd="1.5", cfg=pcfg)
    assert action.kind is ProtectionKind.HOLD and action.reason.startswith("anti_wick")
    assert state.tp_done == [], "a held rung must stay unfired"


def test_anti_wick_lets_a_genuine_move_through(pcfg):
    state = initial_state("pos1", 1, pcfg)
    action = evaluate(state, price_usd=3, executable_quote_usd="2.94", cfg=pcfg)
    assert action.kind is ProtectionKind.TRIM and action.tp_tag == "tp1"


def test_trailing_stop_ratchets_up(pcfg):
    state = initial_state("pos1", 1, pcfg)
    evaluate(state, price_usd=2, cfg=pcfg)          # tp1, trail tier 3000 bps
    evaluate(state, price_usd=4, cfg=pcfg)
    assert state.stop_price == Decimal("4") * Decimal("0.7")
    evaluate(state, price_usd=6, cfg=pcfg)          # tp2 at 5x tightens the tier to 2500 bps
    assert state.stop_price == Decimal("6") * Decimal("0.75")


def test_trailing_stop_never_loosens(pcfg):
    """Monotonic, with exactly ONE deliberate exception: the moon-bag handover.

    The bag is taken AT the trailing stop, so if the stop stayed where it fired the next
    tick would sell the bag -- MEASURED 2026-09-24, all 12 live bags died that way, a
    median of one minute after being taken. `evaluate` therefore releases the stop once,
    latched by MOONBAG_TAG. This test still pins the invariant everywhere else, and pins
    that the one relaxation cannot take the bag into a loss.
    See tests/test_moonbag_survives_the_trim.py.
    """
    from kaiba.execution.protection import MOONBAG_TAG

    state = initial_state("pos1", 1, pcfg)
    evaluate(state, price_usd=2, cfg=pcfg)
    evaluate(state, price_usd=8, cfg=pcfg)
    high_water = state.stop_price
    relaxations = 0
    for price in ("7.5", "6.2", "5.9", "6.4"):
        had_bag = MOONBAG_TAG in state.tp_done
        evaluate(state, price_usd=price, cfg=pcfg)
        if state.stop_price < high_water:
            relaxations += 1
            assert not had_bag, "the stop loosened AFTER the bag was already taken"
            assert MOONBAG_TAG in state.tp_done, "the stop loosened without a moon bag"
            # Wider leash, not no leash: the bag still cannot close red.
            assert state.stop_price >= state.entry_price
        high_water = state.stop_price
    assert relaxations <= 1, f"the stop loosened {relaxations} times"


def test_trailing_stop_exits_when_price_falls_through_it(pcfg):
    state = initial_state("pos1", 1, pcfg)
    evaluate(state, price_usd=2, cfg=pcfg)
    evaluate(state, price_usd=8, cfg=pcfg)
    action = evaluate(state, price_usd="5.0", cfg=pcfg)
    # TRIM, not EXIT_ALL: since 2026-09-23 a profitable trailing stop keeps a moon bag
    # (`ProtectionConfig.moonbag_retain_pct`, default 20). Entry is 1 and the price is 5,
    # so this is a winner being trimmed. The assertion this test exists for is that the
    # trailing stop FIRES when the price falls through it, which it does.
    assert action.kind in (ProtectionKind.EXIT_ALL, ProtectionKind.TRIM), action.reason
    assert "trailing_stop" in action.reason


def test_next_stop_is_pure_and_monotonic(pcfg):
    state = initial_state("pos1", 1, pcfg)
    state.tp_done.append("tp1")
    state.peak_price = Decimal(4)
    state.stop_price = Decimal("2.8")
    assert next_stop(state, Decimal("3.0"), pcfg) == Decimal("2.8")
    assert next_stop(state, Decimal("10"), pcfg) == Decimal(10) * Decimal("0.8")
    assert state.stop_price == Decimal("2.8"), "next_stop must not mutate the state"


def test_missing_price_is_not_a_sell_signal(pcfg):
    state = initial_state("pos1", 1, pcfg)
    assert evaluate(state, price_usd=None, cfg=pcfg).kind is ProtectionKind.HOLD
    assert evaluate(state, price_usd=0, cfg=pcfg).reason == "price_unavailable"


def test_protection_action_helpers():
    assert ProtectionAction.exit_all("x").sells
    assert not ProtectionAction.hold("x").sells
    assert ProtectionAction.trim(25, "r", "tp2").pct == Decimal(25)


def test_gmgn_condition_orders_match_the_cli_contract(pcfg):
    """Shape pinned against gmgn-cli 1.6.1 `skills/gmgn-swap/SKILL.md` + `dist/commands/swap.js`.

    The CLI does not validate this array — it JSON.parses and forwards it — so a wrong key
    or unit yields a position with no server-side protection and no error.
    """
    state = initial_state("pos1", 1, pcfg)
    orders = to_gmgn_condition_orders(state, pcfg)
    for order in orders:
        assert order["side"] == "sell"
        assert "order_type" in order and "type" not in order
        assert all(isinstance(v, str) for v in order.values())

    kinds = [o["order_type"] for o in orders]
    assert kinds.count("profit_stop") == 3
    assert "loss_stop" in kinds and "profit_stop_trace" in kinds

    # +100% = 2x, selling 50% of the remaining position.
    assert orders[0] == {
        "order_type": "profit_stop", "side": "sell", "price_scale": "100", "sell_ratio": "50"
    }
    # A 30% stop is the positive drop magnitude "30", never "-0.3".
    loss = next(o for o in orders if o["order_type"] == "loss_stop")
    assert loss["price_scale"] == "30" and loss["sell_ratio"] == "100"


def test_gmgn_condition_orders_drop_rungs_that_already_fired(pcfg):
    state = initial_state("pos1", 1, pcfg)
    evaluate(state, price_usd=2, cfg=pcfg)
    orders = to_gmgn_condition_orders(state, pcfg)
    assert [o["order_type"] for o in orders].count("profit_stop") == 2
    trace = next(o for o in orders if o["order_type"] == "profit_stop_trace")
    assert trace["drawdown_rate"] == "30"  # 3000 bps as a percentage, not a fraction


def test_gmgn_loss_stop_cannot_express_a_stop_above_entry(pcfg):
    """`loss_stop` only encodes a drop from entry, so a ratcheted stop floors at breakeven."""
    state = initial_state("pos1", 1, pcfg)
    evaluate(state, price_usd=2, cfg=pcfg)
    evaluate(state, price_usd=8, cfg=pcfg)
    assert state.stop_price > state.entry_price
    loss = next(o for o in to_gmgn_condition_orders(state, pcfg) if o["order_type"] == "loss_stop")
    assert loss["price_scale"] == "0", "a negative drop would be silently misread by GMGN"


def test_gmgn_condition_orders_are_refused_on_unsupported_chains(pcfg):
    state = initial_state("pos1", 1, pcfg)
    assert to_gmgn_condition_orders(state, pcfg, chain="arc") == []
    assert to_gmgn_condition_orders(state, pcfg, chain="stable") == []
    assert to_gmgn_condition_orders(state, pcfg, chain="sol")


def test_sell_ratio_type_matches_the_ladder_semantics():
    """risk.yaml says "pct of remaining"; GMGN defaults to buy_amount, which is not that."""
    from kaiba.execution.protection import GMGN_SELL_RATIO_TYPE

    assert GMGN_SELL_RATIO_TYPE == "hold_amount"


def test_protection_state_survives_a_round_trip(pcfg):
    state = initial_state("pos1", "0.000123", pcfg)
    evaluate(state, price_usd="0.000246", cfg=pcfg)
    revived = ProtectionState.model_validate(state.model_dump())
    assert revived.tp_done == ["tp1"]
    assert evaluate(revived, price_usd="0.000246", cfg=pcfg).kind is ProtectionKind.HOLD


def test_signal_strength_is_converted_before_it_reaches_the_ladder():
    """0-1 conviction and a 0-100 ladder are different scales, and nothing said so.

    `Signal.strength` is 0-1; `SCORE_LADDER`'s lowest rung is 70. Feeding the raw value in
    put every signal below every rung, so `score_fraction` returned 0 and the engine sized
    to nothing: 986 signals, 0 that ever reached 80, 361 `size_not_positive` refusals, and
    11.6 hours with no order at all before anyone noticed. It never raised, never logged,
    and every individual component was behaving exactly as written.
    """
    from kaiba.execution.risk import SCORE_LADDER, score_fraction, score_from_strength

    lowest_rung = min(threshold for threshold, _ in SCORE_LADDER)

    # The whole observed range of real signals, raw, is below the lowest rung.
    assert score_fraction(0.7299) == Decimal(0)
    assert score_fraction(1.0) == Decimal(0)

    # Converted, conviction reaches the ladder and discriminates.
    assert score_from_strength(0.73) == pytest.approx(73.0)
    assert score_from_strength(0.73) >= lowest_rung
    assert score_fraction(score_from_strength(0.73)) == Decimal("0.25")
    assert score_fraction(score_from_strength(0.95)) == Decimal("1.00")

    # Monotone: more conviction is never less size.
    fractions = [score_fraction(score_from_strength(s)) for s in (0.70, 0.80, 0.90, 0.95)]
    assert fractions == sorted(fractions)


def test_the_engine_converts_strength_rather_than_passing_it_raw(monkeypatch):
    """Pin the conversion at the call site, not just the helper's existence."""
    from kaiba.execution import engine

    seen: list[float] = []

    class _Gate:
        def position_size(self, chain, lane, score, conn=None, *, token=None):  # noqa: ANN001
            seen.append(score)
            return 0

    from kaiba.core.schemas import Signal

    monkeypatch.setattr(engine, "RiskGate", _Gate)
    signal = Signal(
        signal_id="s", chain=SOL, lane=Lane.MIGRATION_FADE, token=TOKEN, strength=0.73, ts_ms=0
    )
    engine._size_for(signal, None)

    assert seen, "the gate was never consulted"
    assert seen[0] == pytest.approx(73.0), (
        f"engine passed {seen[0]} — a 0-1 strength reaching a 0-100 ladder is the bug"
    )


# ------------------------------------------------------------------ the float-ULP clamp


def test_a_legal_size_is_never_refused_as_above_the_clamp(gate, write_risk, tmp_db):
    """MEASURED 2026-09-21: 44.4% of all 55,001 legal sizes on the shipped sol budget were
    refused with ``size_above_clamp`` although every one was under the 5% bound.

    Cause: the ceiling was built as ``Decimal(str(clamp_size_pct(float(pct))))``.
    ``float(Decimal)`` rounds DOWN by an ULP, so an exact ``pct`` came back a hair below
    itself and ``pct > ceiling`` fired on a rounding error -- the live box's own message
    was ``size_above_clamp:1.7478>1.7478155555555555``. A float in a money comparison.

    This sweeps sizes whose percentage is NOT representable in binary (the ones that
    round) and asserts none is refused for being above a bound it is under.
    """
    from decimal import Decimal

    seed_depth(tmp_db)
    bankroll = 4_500_000_000
    write_risk(chains={"sol": {"bankroll_base_units": bankroll, "min_position_base_units": 1,
                                "max_position_base_units": bankroll}},
               lanes={LANE.value: {"size_pct_max": 5.0, "size_pct_min": 0.01}})
    g = RiskGate()
    cap_pct = Decimal("5.0")
    refused_wrongly = []
    # 78,651,700 is the exact lamport count that produced the live message; the rest are
    # sizes chosen so bankroll*pct/100 is a repeating binary fraction.
    for size in (78_651_700, 45_000_001, 123_456_789, 224_999_999, 224_999_990, 199_999_999,
                 33_333_333, 66_666_667, 100_000_001, 149_999_999):
        pct = Decimal(size) * 100 / Decimal(bankroll)
        assert pct <= cap_pct, "test bug: size is not legal"
        d = g.check_entry(SOL, LANE, size, tmp_db, token=TOKEN)
        if (d.reason or "").startswith("size_above_clamp"):
            refused_wrongly.append((size, d.reason))
    assert refused_wrongly == [], f"legal sizes refused as above the clamp: {refused_wrongly}"


def test_a_size_genuinely_above_the_clamp_is_still_refused(gate, write_risk, tmp_db):
    """The fix must not have removed the bound. One lamport over 5% is refused."""
    seed_depth(tmp_db)
    bankroll = 4_500_000_000
    write_risk(chains={"sol": {"bankroll_base_units": bankroll, "min_position_base_units": 1,
                                "max_position_base_units": bankroll}},
               lanes={LANE.value: {"size_pct_max": 5.0, "size_pct_min": 0.01}})
    over = bankroll * 5 // 100 + 1
    d = RiskGate().check_entry(SOL, LANE, over, tmp_db, token=TOKEN)
    assert not d.allowed and d.reason.startswith("size_above_clamp"), d.reason
