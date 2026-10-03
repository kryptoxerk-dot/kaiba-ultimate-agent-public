"""A proven confluence signal becomes a PAPER position, and its close reaches the size study.

End to end through the ordinary machinery: lane -> ``signals`` -> ``engine.run_once`` in
SHADOW -> paper broker -> position -> paper close -> ``trades`` -> ``confluence_size``. The
point is the plumbing the measurement depends on: if the confluence level does not survive
the trip from the signal payload to the closed trade, EV cannot be measured BY level and
the sizing rule has nothing to read.
"""

from __future__ import annotations

from decimal import Decimal

from kaiba.core.config import load_risk
from kaiba.core.schemas import Action, Chain, Lane, LaneMode
from kaiba.execution import engine, lanes
from kaiba.execution.paper import PaperBroker
from kaiba.learning import confluence_size as CS
from kaiba.learning import proven as P
from tests.test_engine_launchpad_allowlist import SIZE, TOKEN, _Gate, _ready, protectable  # noqa: F401
from tests.test_lanes import NOW, build_ctx, load_fixture

RH = Chain.ROBINHOOD
A = "wa11etAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1"
B = "wa11etBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB2"
F = "wa11etFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF6"


def _arm_shadow(monkeypatch):
    cfg = load_risk()
    lane_cfg = cfg.lane(Lane.CONFLUENCE_5).model_copy(update={
        "mode": LaneMode.SHADOW, "chains": [RH],
        "params": {**dict(cfg.lane(Lane.CONFLUENCE_5).params or {}), "wallet_source": "proven"},
    })
    cfg = cfg.model_copy(update={
        "kill_switch": False, "entries_paused": False, "reduce_only": False,
        "lanes": {**cfg.lanes, Lane.CONFLUENCE_5: lane_cfg},
        "chains": {**cfg.chains, RH: cfg.chain_budget(RH).model_copy(update={"enabled": True})},
    })
    monkeypatch.setattr(engine, "get_risk", lambda: cfg)
    monkeypatch.setattr(engine, "RiskGate", _Gate)
    assert cfg.effective_mode(Lane.CONFLUENCE_5) is LaneMode.SHADOW


def test_a_proven_signal_is_paper_traded_and_its_level_reaches_the_size_study(
    tmp_db, monkeypatch, protectable  # noqa: F811
):
    _arm_shadow(monkeypatch)
    P.clear_cache()
    report = P.ProvenReport(chain=RH, as_of_ms=NOW - 3_600_000, config=P.ProvenConfig(),
                            split_ms=NOW - 90_000_000)
    report.proven = [P.WalletEvidence(wallet=w, source="test") for w in (A, B, F)]
    P.freeze(tmp_db, report)
    P.clear_cache()

    fx = load_fixture("confluence_5")
    fx["chain"], fx["token"] = "robinhood", TOKEN
    ctx = build_ctx(tmp_db, fx, params={"wallet_source": "proven", "min_entities": 3,
                                        "max_signal_age_s": 60, "min_buy_usd": 50})
    signal = lanes.confluence_5(ctx)
    assert signal is not None and signal.payload["confluence_level"] == 3
    assert lanes.record(signal, tmp_db)

    _ready(tmp_db, "pons")
    [decision] = engine.run_once(tmp_db)
    assert (decision.action, decision.mode, decision.lane) == (Action.ENTER, LaneMode.SHADOW, Lane.CONFLUENCE_5)
    assert decision.size_base_units == SIZE
    pos_row = tmp_db.execute("SELECT * FROM positions").fetchone()
    assert (pos_row["mode"], pos_row["lane"]) == ("shadow", "confluence-5")
    assert tmp_db.execute("SELECT COUNT(*) FROM orders WHERE mode != 'shadow'").fetchone()[0] == 0

    broker = PaperBroker(tmp_db)
    position = broker._load_position(pos_row["position_id"])
    broker.sell(position, 100, price_usd=Decimal("0.00015"), liquidity_usd=Decimal("50000"),
                exit_reason="tp_test", decimals=18)
    trades, skipped = CS.collect_paper_trades(tmp_db, since_ms=NOW - 86_400_000)
    assert skipped == {}
    assert len(trades) == 1
    assert trades[0].level == 3 and trades[0].chain == "robinhood"
    assert trades[0].ret > 0  # bought ~0.0001, sold 0.00015, net of the paper fees
