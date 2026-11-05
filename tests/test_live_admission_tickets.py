"""The owner's fixed live tickets on sm-trenches/sol (2026-11-03, SOL-TICKETS-LEAD-CLAUDE).

OWNER: "Entry should be .3 each entry no daily drawdown stop" and ".1 sol for entry under 100k
minimum 10k market cap". Every live entry is EXACTLY 0.1 SOL for a verified circulating market
cap in [$10k, $100k) and EXACTLY 0.3 SOL at $100k and above; below $10k, unknown or stale is
refused. A ticket the cash, exposure or pool cannot take whole is refused, never resized, and a
plan whose tier changed before send is refused. The daily loss stop at 0 disables both the
realised-loss stop and its reservation. Paper is untouched. Each refusal has a positive control.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from kaiba.core.schemas import Chain, Lane
from kaiba.execution import viability
from kaiba.execution.executor import ExecutionRefused, _check_mode
from kaiba.execution.risk import RiskGate, note_zero_size, zero_size_cause
from tests.test_live_admission import buy, seed_token_info
from tests.test_risk import (  # noqa: F401
    OTHER_TOKEN,
    TOKEN,
    close_position,
    open_position,
    seed_depth,
    seed_dossier,
    write_risk,
)

SOL, BSC = Chain.SOL, Chain.BSC
LANE = Lane.SM_TRENCHES
LOW, HIGH = 100_000_000, 300_000_000  # 0.1 SOL and 0.3 SOL
BANKROLL = 3_294_269_853
TICKETS = {
    "chains": ["sol"], "max_position_base_units": {"sol": HIGH}, "min_market_cap_usd": 10_000,
    "market_cap_max_age_s": 600,
    "tickets": {"sol": [{"min_market_cap_usd": 10_000, "ticket_base_units": LOW},
                        {"min_market_cap_usd": 100_000, "ticket_base_units": HIGH}]},
}
#: The box's legacy ladder: a $200k dossier cap picks the 0.25 SOL rung, below the 0.3 ticket.
LEGACY_RUNGS = [
    {"min_market_cap_usd": 0, "size_base_units": 100_000_000},
    {"min_market_cap_usd": 100_000, "size_base_units": 250_000_000},
    {"min_market_cap_usd": 250_000, "size_base_units": 500_000_000},
    {"min_market_cap_usd": 1_000_000, "size_base_units": 1_000_000_000},
]


def configure(write_risk, *, mode: str = "live", tickets: bool = True, stop: int = 0,  # noqa: F811
              bankroll: int = BANKROLL, gas: int = 50_000_000, bounds: dict | None = None) -> None:
    lane = {"mode": mode, "size_pct_min": 26.0, "size_pct_max": 29.0, "chains": ["sol", "bsc"]}
    if tickets:
        lane["params"] = {"live_admission": TICKETS}
    write_risk(
        bounds=bounds or {"max_size_pct_bankroll": 30.0, "max_total_exposure_pct": 70.0},
        chains={"sol": {"bankroll_base_units": bankroll, "max_position_base_units": 100_000_000,
                        "min_position_base_units": 50_000_000, "gas_reserve_base_units": gas,
                        "daily_loss_stop_base_units": stop, "max_exposure_pct": 90.0,
                        "drawdown_recovery": False, "size_by_market_cap": LEGACY_RUNGS}},
        lanes={LANE.value: lane},
    )


def market_cap(usd: float) -> None:
    """A fresh cached GMGN response: 1e9 circulating at usd/1e9 a token."""
    seed_token_info(circulating="1000000000", price=format(usd / 1e9, ".12f"))


@pytest.fixture
def pool(tmp_db):
    seed_dossier(tmp_db, 200_000)  # the legacy dossier cap that picks the 0.25 SOL rung
    return tmp_db


# --- exact tickets at the boundaries --------------------------------------------------------

@pytest.mark.parametrize("usd, ticket", [
    (10_000, LOW), (99_999, LOW), (100_000, HIGH), (2_500_000, HIGH),
])
def test_the_ticket_is_exact_at_the_boundaries(write_risk, pool, usd, ticket):  # noqa: F811
    configure(write_risk)
    market_cap(usd)
    gate = RiskGate()
    assert gate.position_size(SOL, LANE, 95.0, pool, token=TOKEN) == ticket
    assert gate.position_size(SOL, LANE, 10.0, pool, token=TOKEN) in (0, ticket)  # never scaled
    verdict = gate.check_entry(SOL, LANE, ticket, pool, token=TOKEN)
    assert verdict.allowed, verdict.reason  # the 0.25 SOL legacy rung does not block 0.3


@pytest.mark.parametrize("seed, cause, gate_reason", [
    ({"usd": 9_999}, "live_ticket_below_floor:9999<10000",
     "live_admission_market_cap_below_floor:9999<10000"),
    (None, "live_ticket_market_cap_unverified:no_token_info",
     "live_admission_market_cap_unverified:no_token_info"),
    ({"age_ms": 601_000}, "live_ticket_market_cap_unverified:stale:601s",
     "live_admission_market_cap_unverified:stale:601s"),
])
def test_below_10k_unknown_and_stale_are_refused(write_risk, pool, seed, cause, gate_reason):  # noqa: F811
    configure(write_risk)
    if seed and "usd" in seed:
        market_cap(seed["usd"])
    elif seed:
        seed_token_info(price="0.0002", **seed)
    gate = RiskGate()
    assert gate.position_size(SOL, LANE, 95.0, pool, token=TOKEN) == 0
    assert zero_size_cause(pool, SOL, LANE, TOKEN) == cause
    assert gate.check_entry(SOL, LANE, LOW, pool, token=TOKEN).reason == gate_reason


@pytest.mark.parametrize("usd, size, ok", [
    (50_000, LOW, True), (50_000, LOW - 1, False), (50_000, HIGH, False),
    (200_000, HIGH, True), (200_000, LOW, False), (200_000, 250_000_000, False),
])
def test_the_gate_admits_only_the_exact_ticket(write_risk, pool, usd, size, ok):  # noqa: F811
    configure(write_risk)
    market_cap(usd)
    verdict = RiskGate().check_entry(SOL, LANE, size, pool, token=TOKEN)
    if ok:
        assert verdict.allowed, verdict.reason
    else:
        assert not verdict.allowed and verdict.reason.startswith("live_admission_ticket_mismatch:"), verdict.reason


def test_a_zero_size_reports_the_sizers_cause_not_a_ticket_mismatch(write_risk, pool):  # noqa: F811
    configure(write_risk)
    market_cap(200_000)
    note_zero_size(pool, SOL, LANE, TOKEN, "no_viable_band")
    verdict = RiskGate().check_entry(SOL, LANE, 0, pool, token=TOKEN)
    assert not verdict.allowed
    assert verdict.reason == "size_not_positive:no_viable_band"


def test_a_zero_size_on_an_uncapped_or_unknown_token_is_still_refused(write_risk, pool):  # noqa: F811
    configure(write_risk)
    verdict = RiskGate().check_entry(SOL, LANE, 0, pool, token=TOKEN)
    assert not verdict.allowed and verdict.reason.startswith("size_not_positive")


# --- the tier is rechecked at send ----------------------------------------------------------

def test_both_tier_crossings_before_send_are_refused(write_risk, tmp_db):  # noqa: F811
    configure(write_risk)
    market_cap(200_000)
    _check_mode(buy(SOL, HIGH))  # positive control
    market_cap(50_000)  # fell under $100k: a planned 0.3 must never be spent in the low tier
    with pytest.raises(ExecutionRefused, match=r"ticket_mismatch:300000000!=100000000"):
        _check_mode(buy(SOL, HIGH))
    _check_mode(buy(SOL, LOW))
    market_cap(150_000)  # rose over $100k: the 0.1 plan is stale too
    with pytest.raises(ExecutionRefused, match=r"ticket_mismatch:100000000!=300000000"):
        _check_mode(buy(SOL, LOW))
    market_cap(9_000)
    with pytest.raises(ExecutionRefused, match=r"below_floor:9000<10000"):
        _check_mode(buy(SOL, LOW))


# --- what the money or the pool cannot take whole is refused, not resized --------------------

@pytest.mark.parametrize("usd, field, value, cause", [
    (50_000, "min_viable_base_units", 120_000_000, "live_ticket_band:120000000!=100000000"),
    (200_000, "max_viable_base_units", 200_000_000, "live_ticket_band:200000000!=300000000"),
])
def test_the_pool_band_moving_the_ticket_refuses(write_risk, pool, monkeypatch, usd, field, value, cause):  # noqa: F811
    configure(write_risk)
    market_cap(usd)
    real = viability.sizing_band(SOL, pool, token=TOKEN)
    assert real.min_viable_base_units <= LOW and real.max_viable_base_units >= HIGH  # real pool fits both
    monkeypatch.setattr(viability, "sizing_band", lambda *a, **k: replace(real, **{field: value}))
    assert RiskGate().position_size(SOL, LANE, 95.0, pool, token=TOKEN) == 0
    assert zero_size_cause(pool, SOL, LANE, TOKEN) == cause


def test_insufficient_free_cash_refuses(write_risk, pool):  # noqa: F811
    wide = {"max_size_pct_bankroll": 100.0, "max_total_exposure_pct": 100.0}
    configure(write_risk, bankroll=350_000_000, gas=100_000_000, bounds=wide)  # free 0.25 SOL
    market_cap(200_000)
    gate = RiskGate()
    assert gate.position_size(SOL, LANE, 95.0, pool, token=TOKEN) == 0
    assert zero_size_cause(pool, SOL, LANE, TOKEN).startswith("live_ticket_insufficient_free:")
    market_cap(50_000)  # positive control: the 0.1 ticket fits the same cash
    assert gate.position_size(SOL, LANE, 95.0, pool, token=TOKEN) == LOW


def test_aggregate_exposure_refuses_the_ticket_it_cannot_hold(write_risk, pool):  # noqa: F811
    configure(write_risk)
    open_position(pool, OTHER_TOKEN, 2_100_000_000)  # 2.1 of the 2.306 SOL total cap is open
    market_cap(200_000)
    gate = RiskGate()
    assert gate.position_size(SOL, LANE, 95.0, pool, token=TOKEN) == 0
    assert zero_size_cause(pool, SOL, LANE, TOKEN).startswith("total_exposure_cap:")
    market_cap(50_000)  # positive control: 0.1 still fits the 0.206 SOL of room
    assert gate.position_size(SOL, LANE, 95.0, pool, token=TOKEN) == LOW


# --- paper and other chains -----------------------------------------------------------------

def test_paper_sizing_ignores_the_tickets(write_risk, pool):  # noqa: F811
    market_cap(200_000)
    configure(write_risk, mode="shadow", tickets=False)
    want = RiskGate().position_size(SOL, LANE, 95.0, pool, token=TOKEN)
    configure(write_risk, mode="shadow")
    assert RiskGate().position_size(SOL, LANE, 95.0, pool, token=TOKEN) == want != HIGH


def test_an_unlisted_chain_never_uses_a_ticket(write_risk, tmp_db):  # noqa: F811
    configure(write_risk)
    with pytest.raises(ExecutionRefused, match="is shadow on bsc"):
        _check_mode(buy(BSC, LOW))


# --- daily loss stop at 0 -------------------------------------------------------------------

def test_a_zero_daily_stop_skips_the_realised_loss_stop(write_risk, pool):  # noqa: F811
    market_cap(50_000)
    close_position(pool, OTHER_TOKEN, -500_000_000)  # lost 0.5 SOL today
    configure(write_risk, stop=329_426_985)  # positive control: the stop as it was
    assert RiskGate().check_entry(SOL, LANE, LOW, pool, token=TOKEN).reason == "daily_loss_stop"
    configure(write_risk, stop=0)
    verdict = RiskGate().check_entry(SOL, LANE, LOW, pool, token=TOKEN)
    assert verdict.allowed, verdict.reason


def test_a_zero_daily_stop_skips_the_reservation(write_risk, pool):  # noqa: F811
    market_cap(50_000)
    open_position(pool, OTHER_TOKEN, 1_000_000_000)  # 1 SOL open, nothing realised
    configure(write_risk, stop=50_000_000)  # positive control: the reservation fires
    assert RiskGate().check_entry(SOL, LANE, LOW, pool, token=TOKEN).reason.startswith("daily_stop_reserve:")
    configure(write_risk, stop=0)
    verdict = RiskGate().check_entry(SOL, LANE, LOW, pool, token=TOKEN)
    assert verdict.allowed, verdict.reason
    assert not any(f.startswith("daily_stop_reserved") for f in verdict.findings)


def test_a_cap_under_the_lowest_tier_is_refused_by_name(write_risk, pool):  # noqa: F811
    """With no separate floor, the lowest tier is the floor; the refusal must say so."""
    configure(write_risk)
    import os

    import yaml

    path = os.environ["KAIBA_RISK_PATH"]
    raw = yaml.safe_load(open(path, encoding="utf-8"))
    raw["lanes"][LANE.value]["params"]["live_admission"]["min_market_cap_usd"] = 0
    open(path, "w", encoding="utf-8").write(yaml.safe_dump(raw))
    market_cap(9_000)
    verdict = RiskGate().check_entry(SOL, LANE, LOW, pool, token=TOKEN)
    assert verdict.reason == "live_admission_below_lowest_tier:9000"
    assert RiskGate().position_size(SOL, LANE, 95.0, pool, token=TOKEN) == 0
    assert zero_size_cause(pool, SOL, LANE, TOKEN) == "live_ticket_below_lowest_tier:9000"
