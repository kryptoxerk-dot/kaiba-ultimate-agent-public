"""Every decision carries the dossier's measured values AS READ AT THE DECISION.

Found 2026-10-27 by the refutation of the launch-snipe entry-veto test: the forward test for
2026-11-03 needs each decision's top10_pct / liquidity / holder count at decision time, and
nothing kept them. ``token_dossiers`` is one row per token, rebuilt later; no thesis or
blocker carried the value; recovering it meant dropping every decision whose dossier was
rebuilt > 60 s afterwards -- the winners (33 launch-snipe rows at +37.4% capped against a
lane at -20.8%). These tests pin: a known measure is copied, an unknown one is null (never a
default), the row survives the database round trip, and a decision taken through
``engine.decide`` writes the column.
"""
from __future__ import annotations

from decimal import Decimal

from kaiba.core.config import load_risk, save_risk
from kaiba.core.db import fetch_one, jload
from kaiba.core.schemas import (
    Action,
    Chain,
    Decision,
    EvidenceBasis,
    Grade,
    Lane,
    LaneMode,
    Measure,
    Signal,
    TokenDossier,
)
from kaiba.execution import engine, lanes

TOKEN = "6d27YXn6Lct1dqRw4oYvVTRtikHSh2Qjp7BYjz2FozRm"


def _m(value: str) -> Measure:
    return Measure(value=Decimal(value), basis=EvidenceBasis.PROVIDER_REPORTED)


def _dossier(**over) -> TokenDossier:
    base = dict(address=TOKEN, chain=Chain.SOL, top10_pct=_m("23.4"), liquidity_usd=_m("18250"),
                score=41.0, grade=Grade.C, built_at_ms=1_000_000, unknowns=["holder_count", "rug_ratio"])
    base.update(over)
    return TokenDossier(**base)


def test_known_measures_are_copied_and_unknown_ones_are_null_not_zero():
    f = engine.decision_features(_dossier(), ts_ms=1_061_500)
    assert f["top10_pct"] == 23.4 and f["liquidity_usd"] == 18_250.0
    assert "holder_count" in f and f["holder_count"] is None, "an unmeasured count must read as unmeasured"
    assert f["rug_ratio"] is None and f["dossier_score"] == 41.0
    assert f["dossier_built_ms"] == 1_000_000 and f["dossier_age_s"] == 61.5 and f["unknowns"] == 2
    assert engine.decision_features(None, ts_ms=5) == {}


def test_features_survive_the_database_round_trip(tmp_db):
    d = Decision(decision_id="dec_features_rt", lane=Lane.LAUNCH_SNIPE, mode=LaneMode.SHADOW,
                 chain=Chain.SOL, token=TOKEN, action=Action.SKIP, thesis="t",
                 features=engine.decision_features(_dossier(), ts_ms=1_061_500))
    engine._persist(d, tmp_db)
    row = fetch_one(tmp_db, "SELECT features_json FROM decisions WHERE decision_id=?", (d.decision_id,))
    assert jload(row["features_json"], {})["top10_pct"] == 23.4
    back = engine.load_decision("dec_features_rt", tmp_db)
    assert back is not None and back.features == d.features


def test_a_decision_taken_through_decide_writes_the_column(tmp_db, tmp_path, monkeypatch):
    """The path the box takes: a lane signal, a dossier, decide(); the row must carry what the
    dossier said at that instant, whatever decide() concluded after it."""
    cfg = load_risk()
    cfg.global_mode = LaneMode.LIVE
    cfg.kill_switch = False
    cfg.lanes[Lane.LAUNCH_SNIPE].mode = LaneMode.SHADOW
    path = tmp_path / "risk.yaml"
    save_risk(cfg, path)
    monkeypatch.setenv("KAIBA_RISK_PATH", str(path))
    assert engine.get_risk().effective_mode(Lane.LAUNCH_SNIPE) is LaneMode.SHADOW
    ts = engine.now_ms()
    monkeypatch.setattr(engine, "load_dossier", lambda *a, **k: _dossier(built_at_ms=ts - 2_000))
    sig = Signal(signal_id=lanes.signal_id_for(Lane.LAUNCH_SNIPE, Chain.SOL, TOKEN, ts, 120),
                 lane=Lane.LAUNCH_SNIPE, chain=Chain.SOL, token=TOKEN, strength=0.6,
                 reasons=["test"], window_s=120, created_ms=ts, payload={})
    d = engine.decide(sig, tmp_db)
    row = fetch_one(tmp_db, "SELECT features_json FROM decisions WHERE decision_id=?", (d.decision_id,))
    feats = jload(row["features_json"], {})
    assert feats.get("top10_pct") == 23.4 and feats.get("liquidity_usd") == 18250.0, (d.action, d.blockers, feats)
    assert feats.get("holder_count") is None and feats.get("dossier_built_ms") == ts - 2_000
    assert d.features == feats
