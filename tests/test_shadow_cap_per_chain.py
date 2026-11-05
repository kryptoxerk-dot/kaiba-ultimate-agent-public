"""The paper cap counts per (lane, chain), with a total over every lane and chain (design
2026-10-21 step 2).

Counted per lane across chains, five sol launch-snipe positions refused every robinhood and
bsc snipe, and in the capped periods sol held 44% of sm-trenches' slot time. Both gates count:
the paper entry gate (``RiskGate._paper_entry``) and the paper twin (``engine._twin_refusal``),
through one refusal builder (``risk.shadow_open_refusal``). ``config/risk.yaml`` is untouched:
the defaults are 5 per (lane, chain) and 25 in total.
"""

from __future__ import annotations

import copy
import sqlite3
import uuid
from types import SimpleNamespace

import pytest
import yaml

from kaiba.core.config import RiskConfig
from kaiba.core.schemas import Chain, Lane, Signal, now_ms
from kaiba.execution import engine, lanes
from kaiba.execution import risk as risk_mod
from kaiba.execution.risk import (
    SHADOW_MAX_OPEN_PER_LANE_CHAIN_DEFAULT,
    SHADOW_MAX_OPEN_TOTAL_DEFAULT,
    RiskGate,
    shadow_max_open_per_lane_chain,
    shadow_max_open_total,
)

SOL, RH, BSC = Chain.SOL, Chain.ROBINHOOD, Chain.BSC
SNIPE = Lane.LAUNCH_SNIPE
TICKETS = {SOL: 370_000_000, RH: 16_700_000_000_000_000, BSC: 129_420_000_000_000_000}
PAIR, TOTAL = SHADOW_MAX_OPEN_PER_LANE_CHAIN_DEFAULT, SHADOW_MAX_OPEN_TOTAL_DEFAULT

RISK: dict = {
    "version": "v1",
    "global_mode": "live",
    "kill_switch": False,
    "entries_paused": False,
    "reduce_only": False,
    "bounds": {"max_size_pct_bankroll": 30.0, "max_total_exposure_pct": 70.0, "max_lane_mode": "live"},
    "chains": {
        SOL.value: {"enabled": True, "bankroll_base_units": 8_000_000_000, "min_position_base_units": TICKETS[SOL],
                    "max_position_base_units": TICKETS[SOL], "max_exposure_pct": 15.0},
        RH.value: {"enabled": True, "bankroll_base_units": 400_000_000_000_000_000,
                   "min_position_base_units": TICKETS[RH], "max_position_base_units": TICKETS[RH],
                   "max_exposure_pct": 15.0},
        BSC.value: {"enabled": False, "bankroll_base_units": 1_000_000_000_000_000_000,
                    "min_position_base_units": TICKETS[BSC], "max_position_base_units": TICKETS[BSC],
                    "max_exposure_pct": 15.0},
    },
    "lanes": {
        SNIPE.value: {"mode": "shadow", "size_pct_min": 1.0, "size_pct_max": 30.0,
                      "chains": [SOL.value, RH.value, BSC.value], "params": {}},
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
    """Pool economics are ``viability``'s; here every pool fits the ticket."""
    monkeypatch.setattr(risk_mod.viability, "check_size",
                        lambda *a, **k: SimpleNamespace(ok=True, reason="fixture_pool", findings=["fixture_pool"]))
    monkeypatch.setattr(RiskGate, "_clamp_to_band", lambda self, chain, token, size, conn: size)


def hold(conn, n: int, *, chain: Chain = SOL, lane: Lane = SNIPE, mode: str = "shadow", closed: bool = False) -> None:
    ts = now_ms()
    for _ in range(n):
        conn.execute(
            "INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms, closed_ms, qty, qty_total, "
            "cost_native) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"pos_{uuid.uuid4().hex[:12]}", chain.value, uuid.uuid4().hex, lane.value, mode, ts - 60_000,
             ts if closed else None, "1", "1", str(TICKETS.get(chain, 1))))


def entry(conn, chain: Chain, lane: Lane = SNIPE):
    return RiskGate().check_entry(chain, lane, TICKETS[chain], conn, token="0xfeedface" + uuid.uuid4().hex[:8])


def twin(conn, chain: Chain, lane: Lane = SNIPE) -> str | None:
    ts = now_ms() - 1_000
    token = "FakeTwinMint" + uuid.uuid4().hex[:8]
    sig = Signal(signal_id=lanes.signal_id_for(lane, chain, token, ts, 300), lane=lane, chain=chain, token=token,
                 strength=0.9, reasons=["fixture"], window_s=300, created_ms=ts, payload={})
    return engine._twin_refusal(sig, conn)


# ------------------------------------------------------------- (1) per (lane, chain)

def test_five_open_sol_snipes_do_not_block_a_robinhood_snipe(tmp_db, write_risk):
    hold(tmp_db, PAIR, chain=SOL)
    rh = entry(tmp_db, RH)
    assert rh.allowed is True, rh.reason
    assert f"shadow_open:0/{PAIR}:robinhood" in rh.findings and f"shadow_open_total:{PAIR}/{TOTAL}" in rh.findings
    sixth = entry(tmp_db, SOL)
    assert sixth.allowed is False and sixth.reason == f"shadow_open_cap:{PAIR}/{PAIR}:sol"
    assert sixth.reason.split(":", 1)[0] == "shadow_open_cap"  # the head brake classification reads


def test_the_pair_cap_counts_only_this_lanes_open_paper_on_this_chain(tmp_db, write_risk):
    hold(tmp_db, PAIR, chain=SOL, lane=Lane.SM_TRENCHES)  # another lane on the chain
    hold(tmp_db, PAIR, chain=SOL, closed=True)            # this pair, closed
    hold(tmp_db, PAIR, chain=SOL, mode="live")            # this pair, not paper
    assert entry(tmp_db, SOL).allowed is True


def test_a_chain_disabled_for_live_is_capped_per_chain_too(tmp_db, write_risk):
    hold(tmp_db, PAIR, chain=BSC)
    assert entry(tmp_db, BSC).reason == f"shadow_open_cap:{PAIR}/{PAIR}:bsc"
    assert entry(tmp_db, RH).allowed is True


# ------------------------------------------------------------- (2) the total

def test_the_total_cap_refuses_across_chains_and_lanes(tmp_db, write_risk):
    hold(tmp_db, 4, chain=SOL)
    hold(tmp_db, 4, chain=SOL, lane=Lane.SM_TRENCHES)
    hold(tmp_db, 4, chain=SOL, lane=Lane.MIGRATION_FADE)
    hold(tmp_db, 4, chain=SOL, lane=Lane.CONFLUENCE_5)
    hold(tmp_db, 4, chain=BSC, lane=Lane.SM_TRENCHES)
    hold(tmp_db, 4, chain=RH, lane=Lane.SM_TRENCHES)
    hold(tmp_db, 1, chain=RH)  # 25 open; the robinhood snipe pair holds 1 of 5
    verdict = entry(tmp_db, RH)
    assert verdict.allowed is False and verdict.reason == f"shadow_open_total_cap:{TOTAL}/{TOTAL}"
    tmp_db.execute("UPDATE positions SET closed_ms=? WHERE rowid=(SELECT MIN(rowid) FROM positions)", (now_ms(),))
    assert entry(tmp_db, RH).allowed is True  # positive control: one slot back, admitted


def test_the_total_cap_is_configurable(tmp_db, write_risk):
    write_risk(protection={"shadow_max_open_total": 2})
    hold(tmp_db, 1, chain=SOL)
    hold(tmp_db, 1, chain=BSC, lane=Lane.SM_TRENCHES)
    assert entry(tmp_db, RH).reason == "shadow_open_total_cap:2/2"


def test_the_pair_cap_is_configurable(tmp_db, write_risk):
    write_risk(protection={"shadow_max_open_per_lane_chain": 1})
    hold(tmp_db, 1, chain=SOL)
    assert entry(tmp_db, SOL).reason == "shadow_open_cap:1/1:sol"
    assert entry(tmp_db, RH).allowed is True


def test_the_caps_never_touch_live(tmp_db, write_risk):
    write_risk(protection={"shadow_max_open_per_lane_chain": 0, "shadow_max_open_total": 0},
               lanes={SNIPE.value: {"mode": "live"}})
    assert entry(tmp_db, SOL).allowed is True


# ------------------------------------------------------------- (3) the twin path

def test_twins_obey_the_pair_cap(tmp_db, write_risk):
    hold(tmp_db, PAIR, chain=SOL)
    assert twin(tmp_db, RH) is None
    assert twin(tmp_db, SOL) == f"shadow_open_cap:{PAIR}/{PAIR}:sol"


def test_twins_obey_the_total_cap(tmp_db, write_risk):
    write_risk(protection={"shadow_max_open_total": 3})
    hold(tmp_db, 2, chain=SOL, lane=Lane.MIGRATION_FADE)
    assert twin(tmp_db, RH) is None
    hold(tmp_db, 1, chain=BSC, lane=Lane.SM_TRENCHES)
    assert twin(tmp_db, RH) == "shadow_open_total_cap:3/3"


def test_a_twin_count_that_cannot_be_read_refuses(tmp_db, write_risk, monkeypatch):
    real = engine.fetch_one

    def broken(conn, sql, params=()):
        if "COUNT(*)" in sql:
            raise sqlite3.OperationalError("database is locked")
        return real(conn, sql, params)

    monkeypatch.setattr(engine, "fetch_one", broken)
    assert twin(tmp_db, RH).startswith("shadow cap unreadable")


def test_an_entry_count_that_cannot_be_read_refuses(tmp_db, write_risk, monkeypatch):
    def broken(self, *a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(RiskGate, "open_shadow_positions_total", broken)
    verdict = entry(tmp_db, RH)
    assert verdict.allowed is False and verdict.reason == "shadow_open_cap:unreadable:OperationalError"


# ------------------------------------------------------------- the config reads fail closed

@pytest.mark.parametrize("raw", ["ten", True, -1, 2.5, [5]])
def test_an_unreadable_value_is_the_default_never_unlimited(raw):
    assert shadow_max_open_per_lane_chain(RiskConfig(protection={"shadow_max_open_per_lane_chain": raw})) == PAIR
    assert shadow_max_open_total(RiskConfig(protection={"shadow_max_open_total": raw})) == TOTAL


def test_absent_keys_are_the_defaults():
    assert shadow_max_open_per_lane_chain(RiskConfig(protection={})) == 5
    assert shadow_max_open_total(RiskConfig(protection={})) == 25


def test_the_per_lane_key_this_replaces_is_the_pair_caps_fallback():
    """The box ships ``shadow_max_open_per_lane: 5``; an operator's 0 there must still stop paper."""
    assert shadow_max_open_per_lane_chain(RiskConfig(protection={"shadow_max_open_per_lane": 0})) == 0
    assert shadow_max_open_per_lane_chain(RiskConfig(protection={"shadow_max_open_per_lane": 5})) == 5
    assert shadow_max_open_per_lane_chain(RiskConfig(protection={"shadow_max_open_per_lane": "x"})) == PAIR
    both = {"shadow_max_open_per_lane": 0, "shadow_max_open_per_lane_chain": 3}
    assert shadow_max_open_per_lane_chain(RiskConfig(protection=both)) == 3
