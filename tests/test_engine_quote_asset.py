"""The agent never "buys" the asset it pays with.

FOUND 2026-09-22, the minute the smart-money cohort was seeded and ``sm_trenches`` could
see candidates for the first time. ``tracker.trenches_input_report(Chain.SOL)`` ranked
``So11111111111111111111111111111111111111112`` -- WSOL, the quote asset -- FIRST, with 31
smart net buyers, because every SOL-denominated buy in ``swaps`` carries the wrapped mint
on one side. The lane was live. Entering it pays SOL to receive SOL and pays the GMGN
round trip twice (~2%): a certain loss, sized by the same ladder as a real candidate.

Refused in every mode, including shadow: a shadow record of buying the quote asset is not
evidence about anything, it is noise in the expectancy the operator reads.
"""

from __future__ import annotations

import pytest

from kaiba.core.config import load_risk, save_risk
from kaiba.core.schemas import (
    EVM_ZERO,
    SOL_NATIVE_MINT,
    Action,
    Chain,
    Lane,
    LaneMode,
    Signal,
    is_quote_asset,
)
from kaiba.execution import engine, lanes

BLOCKER = "token_is_quote_asset"
REAL_SOL_TOKEN = "GMFCWQv8CfnjGR2xCJebyxqSsCmyMo31pXcCEnfDpump"
REAL_BSC_TOKEN = "0x18f91c1d14b0d4c528fb746836948944e6486666"
WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"


@pytest.fixture
def armed(tmp_path, monkeypatch):
    """sm-trenches live on sol and bsc, as the box runs it."""
    cfg = load_risk()
    cfg.global_mode = LaneMode.LIVE
    cfg.kill_switch = False
    cfg.lanes[Lane.SM_TRENCHES].mode = LaneMode.LIVE
    path = tmp_path / "risk.yaml"
    save_risk(cfg, path)
    monkeypatch.setenv("KAIBA_RISK_PATH", str(path))
    return path


def _signal(chain: Chain, token: str, lane: Lane = Lane.SM_TRENCHES) -> Signal:
    ts = engine.now_ms()
    return Signal(
        signal_id=lanes.signal_id_for(lane, chain, token, ts, 300), lane=lane, chain=chain,
        token=token, strength=0.9, reasons=["31 smart wallets in the trenches preset (min 3)"],
        window_s=300, created_ms=ts, payload={},
    )


# ------------------------------------------------------------------ the measured case


def test_the_real_wsol_candidate_is_refused(tmp_db, armed):
    """The exact token the live report ranked first on 2026-09-22."""
    d = engine.decide(_signal(Chain.SOL, SOL_NATIVE_MINT), tmp_db)
    assert d.action is Action.SKIP
    assert BLOCKER in d.blockers, d.blockers
    assert d.size_base_units is None


@pytest.mark.parametrize("mode", [LaneMode.LIVE, LaneMode.CANARY, LaneMode.SHADOW])
def test_it_is_refused_in_every_mode(tmp_db, tmp_path, monkeypatch, mode):
    cfg = load_risk()
    cfg.global_mode = LaneMode.LIVE
    cfg.kill_switch = False
    cfg.lanes[Lane.SM_TRENCHES].mode = mode
    path = tmp_path / "risk.yaml"
    save_risk(cfg, path)
    monkeypatch.setenv("KAIBA_RISK_PATH", str(path))
    assert BLOCKER in engine.decide(_signal(Chain.SOL, SOL_NATIVE_MINT), tmp_db).blockers


def test_the_evm_wrapped_native_is_refused_though_the_input_leg_differs(tmp_db, armed):
    """On EVM the order's input leg is EVM_ZERO, so only the address table can catch WBNB."""
    assert WBNB != EVM_ZERO
    assert BLOCKER in engine.decide(_signal(Chain.BSC, WBNB), tmp_db).blockers


def test_a_checksummed_evm_address_is_still_caught(tmp_db, armed):
    """Addresses reach us in both cases; a case-sensitive check would let one through."""
    assert BLOCKER in engine.decide(_signal(Chain.BSC, WBNB.upper().replace("0X", "0x")), tmp_db).blockers


def test_stables_are_refused_too(tmp_db, armed):
    """A dollar for a dollar, minus the round trip."""
    for chain, token in ((Chain.SOL, "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"),
                         (Chain.BSC, "0x55d398326f99059ff775485246999027b3197955")):
        assert BLOCKER in engine.decide(_signal(chain, token), tmp_db).blockers, (chain, token)


# ------------------------------------------------------------------ it refuses nothing else


def test_a_real_candidate_is_not_touched_by_the_rule(tmp_db, armed):
    """The guard must not become a filter. These fail later, for their own reasons."""
    for chain, token in ((Chain.SOL, REAL_SOL_TOKEN), (Chain.BSC, REAL_BSC_TOKEN)):
        d = engine.decide(_signal(chain, token), tmp_db)
        assert BLOCKER not in d.blockers, (chain, d.blockers)


def test_the_predicate_itself(tmp_db):
    assert is_quote_asset(Chain.SOL, SOL_NATIVE_MINT)
    assert is_quote_asset(Chain.BSC, WBNB)
    assert is_quote_asset(Chain.BSC, EVM_ZERO)
    assert not is_quote_asset(Chain.SOL, REAL_SOL_TOKEN)
    assert not is_quote_asset(Chain.BSC, REAL_BSC_TOKEN)


def test_the_refusal_is_readable_off_the_decisions_table(tmp_db, armed):
    """Skips are the training data; this one must name itself."""
    d = engine.decide(_signal(Chain.SOL, SOL_NATIVE_MINT), tmp_db)
    row = tmp_db.execute(
        "SELECT action, blockers_json, thesis FROM decisions WHERE decision_id=?", (d.decision_id,)
    ).fetchone()
    assert row["action"] == "skip" and BLOCKER in row["blockers_json"]
    assert "denominated" in row["thesis"]
