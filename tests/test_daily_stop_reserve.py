"""The daily loss stop reserves what is already committed before it admits another ticket.

MEASURED on the live box, SOL 2026-10-02 (journal #5048): stop 0.8 SOL, ticket 0.83 SOL.
The gate read realised PnL only (``realized_today <= -stop``), so with 0.0855 SOL of budget
left it still admitted a full 0.83 SOL ticket, and it never counted the positions already
open. CATGPT opened 19:06:52 and ZETA 19:10:27; ZETA lost 0.678 SOL in a minute, CATGPT
0.254, and the day closed at -1.647 SOL -- 206% of the stop.

The rule now: admit only if ``realized - (open_exposure + ticket) * reserve > -stop``, with
``reserve`` from ``protection.daily_stop_reserve_pct`` (default 0.40; stop exits fill at
-36% to -38% on this book). Exits are never gated.

The fixture below is the real 10-02 SOL sequence, lamports as booked on the box.
"""

from __future__ import annotations

import copy
import uuid
from types import SimpleNamespace

import pytest
import yaml

from kaiba.core.schemas import Chain, EventKind, Lane, now_ms
from kaiba.execution import risk as risk_mod
from kaiba.execution.risk import (
    DAILY_STOP_RESERVE_DEFAULT,
    RiskGate,
    daily_stop_reserve,
    daily_stop_reserved,
)

SOL = Chain.SOL
LANE = Lane.SM_TRENCHES
STOP = 800_000_000          # 0.8 SOL
TICKET = 830_000_000        # 0.83 SOL, the flat $120 ticket that day

#: (token, realized lamports) of the four round trips closed before 19:06 on 2026-10-02.
CLOSED_BEFORE_CATGPT = (
    ("8iivCARE", 72_447_064),      # 15:50 -> 15:56  trailing_stop
    ("TEX8ns7H", -345_918_101),    # 16:06 -> 16:08  stop_loss
    ("ETXqxfVo", -110_794_473),    # 17:19 -> 17:28  trailing_stop
    ("9jUKtKNS", -330_156_916),    # 17:46 -> 17:48  trailing_stop
)
REALIZED_AT_19_06 = sum(r for _t, r in CLOSED_BEFORE_CATGPT)  # -714,422,426: 0.0855 SOL left
CATGPT, CATGPT_PNL = "CNohWHNTcatgpt1111111111111111111111111111", -254_114_640
ZETA, ZETA_PNL = "FyRYWqg6zeta11111111111111111111111111111111", -678_410_891

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
        "sm-trenches": {"mode": "live", "size_pct_min": 10.375, "size_pct_max": 10.375,
                        "chains": ["sol"], "params": {}},
    },
    "protection": {"poll_interval_s": 12, "stop_loss_bps": 3000},
}


@pytest.fixture
def write_risk(tmp_path, monkeypatch):
    path = tmp_path / "risk.yaml"
    monkeypatch.setenv("KAIBA_RISK_PATH", str(path))

    def _write(protection: dict | None = None):
        cfg = copy.deepcopy(RISK)
        if protection is not None:
            cfg["protection"] = {**cfg["protection"], **protection}
        path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
        return path

    _write()
    return _write


@pytest.fixture(autouse=True)
def priced_pool(monkeypatch):
    """The pool arm is ``viability``'s business and has its own tests; here it always fits."""
    monkeypatch.setattr(
        risk_mod.viability, "check_size",
        lambda *a, **k: SimpleNamespace(ok=True, reason="fixture_pool", findings=["fixture_pool"]),
    )


def closed(conn, token: str, realized: int, *, mode: str = "live") -> None:
    ts = now_ms()
    conn.execute(
        "INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms, closed_ms, qty, "
        "qty_total, cost_native, proceeds_native, realized_native) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"pos_{uuid.uuid4().hex[:12]}", SOL.value, token, LANE.value, mode, ts - 60_000, ts,
         "0", "1", str(TICKET), str(TICKET + realized), str(realized)),
    )


def opened(conn, token: str, cost: int = TICKET, proceeds: int = 0, *, mode: str = "live") -> None:
    conn.execute(
        "INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms, qty, qty_total, "
        "cost_native, proceeds_native) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (f"pos_{uuid.uuid4().hex[:12]}", SOL.value, token, LANE.value, mode, now_ms(), "1", "1",
         str(cost), str(proceeds)),
    )


def the_day_so_far(conn) -> None:
    for token, realized in CLOSED_BEFORE_CATGPT:
        closed(conn, token, realized)


def enter(conn, token: str):
    return RiskGate().check_entry(SOL, LANE, TICKET, conn, token=token)


# ------------------------------------------------------------- the 10-02 sequence


def test_the_fixture_is_the_measured_day(tmp_db, write_risk):
    the_day_so_far(tmp_db)
    assert RiskGate().realized_today(SOL, tmp_db) == REALIZED_AT_19_06 == -714_422_426
    assert STOP + REALIZED_AT_19_06 == 85_577_574  # 0.0855 SOL of budget left


def test_zeta_is_refused_with_catgpt_open(tmp_db, write_risk):
    """THE REGRESSION: the entry that took the day to 206% of its stop."""
    the_day_so_far(tmp_db)
    opened(tmp_db, CATGPT)

    verdict = enter(tmp_db, ZETA)

    assert verdict.allowed is False
    reserved = daily_stop_reserved(TICKET, TICKET, DAILY_STOP_RESERVE_DEFAULT)
    assert reserved == 664_000_000
    assert verdict.reason == f"daily_stop_reserve:{REALIZED_AT_19_06}/{reserved}/{STOP}"


def test_catgpt_itself_is_refused_with_nothing_open(tmp_db, write_risk):
    """0.0855 SOL left cannot carry a 0.83 SOL ticket even with an empty book."""
    the_day_so_far(tmp_db)

    verdict = enter(tmp_db, CATGPT)

    assert verdict.allowed is False
    assert verdict.reason == f"daily_stop_reserve:{REALIZED_AT_19_06}/332000000/{STOP}"


def test_the_realised_only_rule_admitted_zeta(tmp_db, write_risk):
    """Positive control: with the reservation off, the old gate lets ZETA through.

    Without this, the refusal above could be coming from anything else in the gate.
    """
    write_risk({"daily_stop_reserve_pct": 0})
    the_day_so_far(tmp_db)
    opened(tmp_db, CATGPT)

    verdict = enter(tmp_db, ZETA)

    assert verdict.allowed is True, verdict.reason


def test_replaying_the_day_holds_it_inside_the_stop(tmp_db, write_risk):
    """Every 10-02 SOL entry in order. Old rule: -1.647 SOL. Reserved rule: -0.714."""
    day = [  # (token, realized) in opening order; each closed before the next opened
        *CLOSED_BEFORE_CATGPT, (CATGPT, CATGPT_PNL), (ZETA, ZETA_PNL),
    ]
    taken: list[str] = []
    for i, (token, realized) in enumerate(day):
        verdict = enter(tmp_db, token)
        if not verdict.allowed:
            assert verdict.reason.startswith("daily_stop_reserve:"), verdict.reason
            continue
        taken.append(token)
        if token == CATGPT:  # CATGPT was still open when ZETA came in
            opened(tmp_db, CATGPT)
        else:
            closed(tmp_db, token, realized)
        assert i < len(CLOSED_BEFORE_CATGPT), f"{token} should have been refused"

    assert taken == [t for t, _r in CLOSED_BEFORE_CATGPT]
    day_pnl = RiskGate().realized_today(SOL, tmp_db)
    assert day_pnl == REALIZED_AT_19_06 > -STOP
    assert REALIZED_AT_19_06 + CATGPT_PNL + ZETA_PNL == -1_646_947_957  # what happened


def test_a_fresh_day_carries_two_tickets_not_three(tmp_db, write_risk):
    """0.83 x 0.4 = 0.332 per ticket against 0.8: the third concurrent ticket is refused."""
    assert enter(tmp_db, "A" * 44).allowed
    opened(tmp_db, "A" * 44)
    assert enter(tmp_db, "B" * 44).allowed
    opened(tmp_db, "B" * 44)

    third = enter(tmp_db, "C" * 44)

    assert third.allowed is False
    assert third.reason == f"daily_stop_reserve:0/996000000/{STOP}"


def test_the_boundary_is_exactly_the_stop(tmp_db, write_risk):
    """``realized - reserved`` landing ON the stop is refused, a lamport inside it is not."""
    reserved = daily_stop_reserved(0, TICKET, DAILY_STOP_RESERVE_DEFAULT)  # 332,000,000
    closed(tmp_db, "A" * 44, -(STOP - reserved) + 1)
    assert enter(tmp_db, "B" * 44).allowed is True
    closed(tmp_db, "C" * 44, -1)
    assert enter(tmp_db, "B" * 44).reason == f"daily_stop_reserve:{-(STOP - reserved)}/{reserved}/{STOP}"


def test_a_position_that_took_profit_reserves_nothing(tmp_db, write_risk):
    """At-risk cost is cost less proceeds: a TP that returned the cost is house money."""
    opened(tmp_db, "A" * 44, proceeds=TICKET)
    opened(tmp_db, "B" * 44)
    assert enter(tmp_db, "C" * 44).allowed


def test_paper_positions_reserve_nothing(tmp_db, write_risk):
    for t in ("A", "B", "C"):
        opened(tmp_db, t * 44, mode="shadow")
    closed(tmp_db, "D" * 44, -700_000_000, mode="shadow")
    assert enter(tmp_db, "E" * 44).allowed


def test_exits_are_never_blocked(tmp_db, write_risk):
    the_day_so_far(tmp_db)
    opened(tmp_db, CATGPT)
    assert enter(tmp_db, ZETA).allowed is False
    assert RiskGate().check_exit(SOL, LANE, tmp_db).allowed is True


def test_the_refusal_is_a_named_brake_event(tmp_db, write_risk):
    the_day_so_far(tmp_db)
    enter(tmp_db, CATGPT)
    rows = tmp_db.execute(
        "SELECT payload FROM events WHERE kind = ?", (EventKind.RISK_HALT.value,)
    ).fetchall()
    assert any("daily_stop_reserve:" in r["payload"] for r in rows)


def test_the_finding_shows_the_reservation_on_an_admitted_entry(tmp_db, write_risk):
    verdict = enter(tmp_db, "A" * 44)
    assert verdict.allowed
    assert f"daily_stop_reserved:332000000/{STOP}" in verdict.findings


# ------------------------------------------------------------- the config key


@pytest.mark.parametrize("raw", [None, "abc", True, -0.1, float("nan"), [0.4]])
def test_missing_or_unreadable_reserve_never_loosens(raw):
    from kaiba.core.config import RiskConfig

    protection = {} if raw is None else {"daily_stop_reserve_pct": raw}
    assert daily_stop_reserve(RiskConfig(protection=protection)) == DAILY_STOP_RESERVE_DEFAULT


def test_a_reserve_above_one_is_clamped_to_the_whole_ticket():
    from kaiba.core.config import RiskConfig

    assert daily_stop_reserve(RiskConfig(protection={"daily_stop_reserve_pct": 40})) == 1


def test_an_explicit_reserve_is_used_as_written():
    from kaiba.core.config import RiskConfig

    assert str(daily_stop_reserve(RiskConfig(protection={"daily_stop_reserve_pct": 0.25}))) == "0.25"


def test_the_reservation_rounds_up():
    assert daily_stop_reserved(1, 0, DAILY_STOP_RESERVE_DEFAULT) == 1
    assert daily_stop_reserved(10, 1, DAILY_STOP_RESERVE_DEFAULT) == 5  # 4.4 -> 5


def test_an_unreadable_key_on_disk_still_refuses_zeta(tmp_db, write_risk):
    write_risk({"daily_stop_reserve_pct": "forty"})
    the_day_so_far(tmp_db)
    opened(tmp_db, CATGPT)
    assert enter(tmp_db, ZETA).reason.startswith("daily_stop_reserve:")
