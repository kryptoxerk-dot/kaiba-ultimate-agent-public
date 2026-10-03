"""A paper entry is not refused by brakes that exist to protect real money.

MEASURED on the live box, 7 days to 2026-10-03: shadow lanes were refused
``daily_loss_stop`` 224 times (migration-fade 166, pons-robinhood 58), ``chain_disabled``
1,385 times and ``size_not_positive:total_exposure_cap`` 3 times. A losing LIVE day
stopped the PAPER record growing, so the shadow sample was selected on live PnL -- the
very record ``kaiba.learning`` and the owner's size-increase rule are judged from.

What a SHADOW entry skips: the daily loss stop (and its reservation), the per-token and
total exposure caps, the compounding bankroll and free balance, the drawdown cut, the
chain's live permission. What it keeps: kill switch, OFF, ``entries_paused``,
``reduce_only``, a halt, the lane's chains, the ladder/envelope/position cap, the pool
band, and a per-lane cap on open shadow positions so protection's quote budget is not
flooded. A paper size is raised to the chain minimum -- the live ticket -- because 1,171
of the 1,612 measured refusals would otherwise just have become ``below_min_position``.

And the property tested hardest: nothing about a LIVE (or CANARY) decision changes.
"""

from __future__ import annotations

import copy
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
import yaml

from kaiba.core.schemas import Action, Chain, EventKind, Lane, LaneMode, Signal, now_ms
from kaiba.execution import engine, lanes
from kaiba.execution import risk as risk_mod
from kaiba.execution.risk import SHADOW_MAX_OPEN_PER_LANE_DEFAULT, RiskGate, shadow_max_open_per_lane
from tests.test_paper import write_dossier

SOL = Chain.SOL
LIVE = Lane.SM_TRENCHES
PAPER = Lane.MIGRATION_FADE
CANARY = Lane.KOL_FADE
TICKET = 830_000_000
STOP = 800_000_000
TOKEN = "GMFCWQv8CfnjGR2xCJebyxqSsCmyMo31pXcCEnfDpump"

RISK: dict = {
    "version": "v1",
    "global_mode": "live",
    "kill_switch": False,
    "entries_paused": False,
    "reduce_only": False,
    "bounds": {"max_size_pct_bankroll": 30.0, "max_total_exposure_pct": 70.0,
               "max_lane_mode": "live"},
    "chains": {
        "sol": {"enabled": True, "bankroll_base_units": 8_000_000_000,
                "max_position_base_units": TICKET, "min_position_base_units": TICKET,
                "gas_reserve_base_units": 50_000_000, "daily_loss_stop_base_units": STOP,
                "max_exposure_pct": 15.0},
    },
    "lanes": {
        lane.value: {"mode": mode, "size_pct_min": 10.375, "size_pct_max": 10.375,
                     "chains": ["sol"], "params": {}}
        for lane, mode in ((LIVE, "live"), (PAPER, "shadow"), (CANARY, "canary"))
    },
    "protection": {"poll_interval_s": 12, "stop_loss_bps": 3000},
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in over.items():
        out[key] = _merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


@pytest.fixture
def write_risk(tmp_path, monkeypatch):
    path = tmp_path / "risk.yaml"
    monkeypatch.setenv("KAIBA_RISK_PATH", str(path))

    def _write(**over):
        path.write_text(yaml.safe_dump(_merge(RISK, over)), encoding="utf-8")
        return path

    _write()
    return _write


@pytest.fixture(autouse=True)
def priced_pool(monkeypatch):
    """Pool economics are ``viability``'s and tested there; here every pool fits the ticket."""
    monkeypatch.setattr(
        risk_mod.viability, "check_size",
        lambda *a, **k: SimpleNamespace(ok=True, reason="fixture_pool", findings=["fixture_pool"]),
    )
    monkeypatch.setattr(RiskGate, "_clamp_to_band", lambda self, chain, token, size, conn: size)


def position(conn, *, lane: Lane = LIVE, mode: str = "live", token: str | None = None,
             cost: int = TICKET, realized: int | None = None) -> None:
    ts = now_ms()
    conn.execute(
        "INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms, closed_ms, qty, "
        "qty_total, cost_native, proceeds_native, realized_native) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"pos_{uuid.uuid4().hex[:12]}", SOL.value, token or uuid.uuid4().hex, lane.value, mode,
         ts - 60_000, ts if realized is not None else None, "1", "1", str(cost),
         str(cost + realized) if realized is not None else "0",
         str(realized) if realized is not None else "0"),
    )


def entry(conn, lane: Lane, size: int = TICKET, token: str = TOKEN):
    return RiskGate().check_entry(SOL, lane, size, conn, token=token)


def size(conn, lane: Lane, token: str = TOKEN) -> int:
    return RiskGate().position_size(SOL, lane, 99.0, conn, token=token)


def halt_events(conn) -> list[str]:
    rows = conn.execute(
        "SELECT payload FROM events WHERE kind = ?", (EventKind.RISK_HALT.value,)
    ).fetchall()
    return [r["payload"] for r in rows]


# ------------------------------------------------------------- the money brakes


def test_a_losing_live_day_does_not_stop_paper(tmp_db, write_risk):
    """THE REGRESSION: 224 shadow refusals for daily_loss_stop in a week."""
    position(tmp_db, realized=-900_000_000)  # past the 0.8 SOL stop

    assert entry(tmp_db, LIVE).reason == "daily_loss_stop"
    assert entry(tmp_db, CANARY).reason == "daily_loss_stop"
    before = len(halt_events(tmp_db))
    paper = entry(tmp_db, PAPER)

    assert paper.allowed is True, paper.reason
    assert paper.reason == "paper_entry_within_envelope"
    assert len(halt_events(tmp_db)) == before, "a paper entry fired a risk.halt"


def test_the_daily_stop_reservation_is_live_only(tmp_db, write_risk):
    position(tmp_db, realized=-700_000_000)  # 0.1 SOL left: the reservation refuses live

    assert entry(tmp_db, LIVE).reason.startswith("daily_stop_reserve:")
    assert entry(tmp_db, PAPER).allowed is True


def test_a_chain_disabled_for_real_money_still_paper_trades(tmp_db, write_risk):
    """1,385 migration-fade refusals in a week, on a chain switched off for live money."""
    write_risk(chains={"sol": {"enabled": False}})

    assert entry(tmp_db, LIVE).reason == "chain_disabled"
    assert size(tmp_db, LIVE) == 0
    paper = entry(tmp_db, PAPER)
    assert paper.allowed is True, paper.reason
    assert "chain_not_enabled_for_live" in paper.findings
    assert size(tmp_db, PAPER) == TICKET


def test_the_total_exposure_cap_is_live_only(tmp_db, write_risk):
    for _ in range(7):  # 5.81 SOL of 8 open: past the 70% basket cap of 5.6
        position(tmp_db)

    assert size(tmp_db, LIVE) == 0
    assert size(tmp_db, PAPER) == TICKET


def test_the_per_token_cap_is_live_only(tmp_db, write_risk):
    write_risk(protection={"daily_stop_reserve_pct": 0})
    position(tmp_db, token=TOKEN)  # 0.83 of 8 already in this token; +0.83 = 20.75% > 15%

    assert entry(tmp_db, LIVE).reason.startswith("max_exposure_pct:")
    assert entry(tmp_db, PAPER).allowed is True


def test_the_free_balance_is_live_only(tmp_db, write_risk):
    write_risk(protection={"daily_stop_reserve_pct": 0}, chains={"sol": {"max_exposure_pct": 100.0}})
    for _ in range(9):  # 7.47 SOL open: 8 - 7.47 - 0.05 gas leaves less than a ticket
        position(tmp_db)

    assert entry(tmp_db, LIVE).reason.startswith("gas_reserve:")
    assert entry(tmp_db, PAPER).allowed is True


def test_paper_sizes_from_the_configured_baseline_not_the_live_equity(tmp_db, write_risk, monkeypatch):
    """Compounding and the wallet cap are live-money arithmetic; paper spends nothing."""
    drained = SimpleNamespace(equity_base_units=0, free_base_units=0,
                              open_exposure_base_units=0, findings=["fixture:drained"])
    monkeypatch.setattr(RiskGate, "bankroll_reading", lambda self, *a, **k: drained)

    assert size(tmp_db, LIVE) == 0
    assert entry(tmp_db, LIVE).reason == "bankroll_unfunded"
    assert size(tmp_db, PAPER) == TICKET
    assert entry(tmp_db, PAPER).allowed is True


def test_paper_skips_the_drawdown_cut(tmp_db, write_risk, monkeypatch):
    """The owner's "trade smaller after a losing day" is about money; paper keeps its size."""
    write_risk(chains={"sol": {"min_position_base_units": 1}})
    monkeypatch.setattr(risk_mod, "recovery_multiplier", lambda *a, **k: (Decimal("0.5"), "fixture"))

    assert size(tmp_db, LIVE) == TICKET // 2
    assert size(tmp_db, PAPER) == TICKET


# ------------------------------------------------------------- what still binds paper


@pytest.mark.parametrize("over, reason", [
    ({"kill_switch": True}, "kill_switch"),
    ({"global_mode": "off"}, "lane_off"),
    ({"entries_paused": True}, "entries_paused"),
    ({"reduce_only": True}, "reduce_only"),
    ({"lanes": {PAPER.value: {"chains": ["robinhood"]}}}, "lane_chain_not_enabled"),
])
def test_operator_brakes_still_stop_paper(tmp_db, write_risk, over, reason):
    write_risk(**over)
    assert entry(tmp_db, PAPER).reason == reason


def test_a_halt_still_stops_paper(tmp_db, write_risk):
    RiskGate().halt("protection_overrun:fixture", tmp_db)
    assert entry(tmp_db, PAPER).reason == "halted:protection_overrun:fixture"


@pytest.mark.parametrize("bad, reason", [
    (0, "size_not_positive"),
    (TICKET - 1, f"size_below_min:{TICKET - 1}"),
    (TICKET + 1, f"size_above_max:{TICKET + 1}"),
])
def test_the_size_checks_still_bind_paper(tmp_db, write_risk, bad, reason):
    assert entry(tmp_db, PAPER, size=bad).reason == reason


def test_paper_is_floored_to_the_live_ticket(tmp_db, write_risk):
    """MEASURED: 1,171 of 1,612 money-brake refusals then sized under the chain minimum.

    migration-fade sizes 1.25% of 8 SOL = 0.10 SOL against a 0.83 SOL floor. Without the
    floor, skipping the money brakes would only have swapped `daily_loss_stop` for
    `below_min_position`. A live/canary lane with the same band is still refused.
    """
    small = {"size_pct_min": 1.0, "size_pct_max": 1.25}
    write_risk(lanes={PAPER.value: small, CANARY.value: small})

    assert size(tmp_db, CANARY) == 0
    assert size(tmp_db, PAPER) == TICKET


def test_a_pool_too_thin_still_refuses_paper(tmp_db, write_risk, monkeypatch):
    monkeypatch.setattr(RiskGate, "_clamp_to_band", lambda self, chain, token, size, conn: 0)
    assert size(tmp_db, PAPER) == 0


def test_the_position_cap_still_binds_paper(tmp_db, write_risk):
    write_risk(lanes={PAPER.value: {"size_pct_min": 20.0, "size_pct_max": 20.0}})
    assert size(tmp_db, PAPER) == TICKET  # 1.6 SOL by the ladder, capped at the ticket


def test_an_unfunded_baseline_cannot_size_paper(tmp_db, write_risk):
    write_risk(chains={"sol": {"bankroll_base_units": 0}})
    assert size(tmp_db, PAPER) == 0
    assert entry(tmp_db, PAPER).reason == "bankroll_unfunded"


# ------------------------------------------------------------- the volume cap


def test_open_paper_positions_are_capped_per_lane(tmp_db, write_risk):
    for _ in range(SHADOW_MAX_OPEN_PER_LANE_DEFAULT):
        position(tmp_db, lane=PAPER, mode="shadow")

    verdict = entry(tmp_db, PAPER)

    assert verdict.allowed is False
    assert verdict.reason == f"shadow_open_cap:{SHADOW_MAX_OPEN_PER_LANE_DEFAULT}/{SHADOW_MAX_OPEN_PER_LANE_DEFAULT}"


def test_the_cap_counts_this_lanes_open_paper_only(tmp_db, write_risk):
    for _ in range(SHADOW_MAX_OPEN_PER_LANE_DEFAULT):
        position(tmp_db, lane=LIVE, mode="shadow")            # another lane's paper (twins)
        position(tmp_db, lane=PAPER, mode="shadow", realized=0)  # this lane's, closed
        position(tmp_db, lane=PAPER, mode="live")              # not paper
    verdict = entry(tmp_db, PAPER)
    assert verdict.allowed is True, verdict.reason
    assert f"shadow_open:0/{SHADOW_MAX_OPEN_PER_LANE_DEFAULT}" in verdict.findings


def test_the_cap_is_configurable(tmp_db, write_risk):
    write_risk(protection={"shadow_max_open_per_lane": 1})
    assert entry(tmp_db, PAPER).allowed is True
    position(tmp_db, lane=PAPER, mode="shadow")
    assert entry(tmp_db, PAPER).reason == "shadow_open_cap:1/1"


@pytest.mark.parametrize("raw", [None, "ten", True, -1, 2.5])
def test_an_unreadable_cap_is_the_default_never_unlimited(raw):
    from kaiba.core.config import RiskConfig

    protection = {} if raw is None else {"shadow_max_open_per_lane": raw}
    assert shadow_max_open_per_lane(RiskConfig(protection=protection)) == SHADOW_MAX_OPEN_PER_LANE_DEFAULT


def test_the_paper_cap_never_touches_live(tmp_db, write_risk):
    write_risk(protection={"shadow_max_open_per_lane": 0})
    assert entry(tmp_db, LIVE).allowed is True
    assert entry(tmp_db, PAPER).reason == "shadow_open_cap:0/0"


# ------------------------------------------------------------- the engine


def _signal(lane: Lane) -> Signal:
    ts = engine.now_ms() - 1_000
    return Signal(
        signal_id=lanes.signal_id_for(lane, SOL, TOKEN, ts, 300),
        lane=lane, chain=SOL, token=TOKEN, strength=0.99, reasons=["fixture"],
        window_s=300, created_ms=ts, payload={},
    )


def test_decide_enters_paper_and_skips_live_on_the_same_losing_day(tmp_db, write_risk):
    write_dossier(tmp_db, token=TOKEN, chain=SOL)
    position(tmp_db, realized=-900_000_000)

    live = engine.decide(_signal(LIVE), tmp_db)
    paper = engine.decide(_signal(PAPER), tmp_db)

    assert live.action is Action.SKIP and live.blockers == ["daily_loss_stop"]
    assert paper.action is Action.ENTER and paper.mode is LaneMode.SHADOW, paper.blockers
    assert paper.size_base_units == TICKET


def test_decide_on_a_clean_day_is_unchanged_for_live(tmp_db, write_risk):
    write_dossier(tmp_db, token=TOKEN, chain=SOL)
    live = engine.decide(_signal(LIVE), tmp_db)
    assert live.action is Action.ENTER and live.mode is LaneMode.LIVE, live.blockers
    assert live.size_base_units == TICKET
