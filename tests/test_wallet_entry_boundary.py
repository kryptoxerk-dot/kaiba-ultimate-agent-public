"""Copy eligibility is a decoded buy + owner trust + fresh DYOR, never inventory inflow.

All database writes use tmp_db. Provider refreshes are stubbed at scan_token; lane and
engine checks execute for real and no order is submitted.
"""
from decimal import Decimal

import pytest

from kaiba.core.config import LaneConfig, RiskConfig
from kaiba.core.schemas import (
    Action,
    Chain,
    EvidenceBasis,
    Grade,
    Lane,
    LaneMode,
    Measure,
    Receipt,
    TokenDossier,
    now_ms,
)
from kaiba.execution import engine, lanes

TOKEN = "Bnd5oBSWpPpXoaWTckgXTVx9TCKkX5GyVqxcrwBzUYsB"
WALLET = "srcWa11etTrustedAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


@pytest.fixture
def copy_ctx(tmp_db, monkeypatch):
    ts = now_ms()
    cfg = RiskConfig()
    monkeypatch.setattr(lanes, "get_risk", lambda: cfg)
    monkeypatch.setattr(engine, "get_risk", lambda: cfg)
    monkeypatch.setattr(engine, "now_ms", lambda: ts)
    tmp_db.execute(
        "INSERT INTO wallets (chain,address,source,first_seen_ms,last_seen_ms,cohort) "
        "VALUES (?,?,?,?,?,?)", ("sol", WALLET, "operator", ts, ts, "trusted_copy"),
    )
    tmp_db.execute(
        "INSERT INTO wallet_scores (chain,address,score,grade,evidence_weight,archetype,"
        "model_version,scored_at_ms) VALUES (?,?,?,?,?,?,?,?)",
        ("sol", WALLET, 91.0, "A", 100.0, "smart_money", "fixture", ts),
    )
    row = dict(chain="sol", token=TOKEN, wallet=WALLET, side="buy", tx="source-buy",
               ts_ms=ts - 1_000, price_usd="0.01", usd_value="1800", source="gmgn:smartmoney")
    dossier = TokenDossier(
        address=TOKEN, chain=Chain.SOL, built_at_ms=ts, grade=Grade.B,
        mint_authority_revoked=True, freeze_authority_revoked=True, can_sell=True,
        price_usd=Measure(value=Decimal("0.0106"), basis=EvidenceBasis.PROVIDER_REPORTED,
                          receipt=Receipt(provider="fixture", endpoint="dyor", observed_at_ms=ts)),
    )
    return lanes.LaneContext(chain=Chain.SOL, token=TOKEN, now_ms=ts, conn=tmp_db,
                             dossier=dossier, recent_buys=[row])


@pytest.mark.parametrize("side", [None, "", "transfer_in", "receive", "airdrop", "in", "sell"])
def test_only_an_explicit_decoded_buy_is_a_copy_source(copy_ctx, side):
    assert lanes.trusted_copy(copy_ctx) is not None  # prove every other gate passes
    copy_ctx.recent_buys[0]["side"] = side
    assert lanes.trusted_copy(copy_ctx) is None


@pytest.mark.parametrize(
    ("field", "value"),
    [("tags", ["transfer_in"]), ("tags", '["transfer_in"]'), ("event_type", "transfer_in"), ("type", "receive")],
)
def test_transfer_in_markers_do_not_turn_a_row_into_a_copy_buy(copy_ctx, field, value):
    copy_ctx.recent_buys[0][field] = value
    assert lanes.trusted_copy(copy_ctx) is None


def _store_dossier(conn, dossier: TokenDossier) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO token_dossiers "
        "(chain,address,built_at_ms,score,grade,blockers_json,warnings_json,unknowns_json,dossier_json) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            dossier.chain.value,
            dossier.address,
            dossier.built_at_ms,
            dossier.score,
            dossier.grade.value,
            "[]",
            "[]",
            "[]",
            dossier.model_dump_json(),
        ),
    )


def _arm_copy_entry(monkeypatch):
    cfg = RiskConfig(
        global_mode=LaneMode.SHADOW,
        lanes={Lane.TRUSTED_COPY: LaneConfig(mode=LaneMode.SHADOW)},
    )
    monkeypatch.setattr(engine, "get_risk", lambda: cfg)
    monkeypatch.setattr(engine, "_size_for", lambda *_args: 1)
    monkeypatch.setattr(engine, "_risk_refusal", lambda *_args: None)


def test_genuine_trusted_copy_buy_can_enter_with_a_fresh_passable_dossier(
    copy_ctx, monkeypatch, tmp_db
):
    _arm_copy_entry(monkeypatch)
    signal = lanes.trusted_copy(copy_ctx)
    assert signal is not None
    _store_dossier(tmp_db, copy_ctx.dossier)

    decision = engine.decide(signal, tmp_db)

    assert decision.action is Action.ENTER
    assert decision.blockers == []


def test_missing_dossier_blocks_before_sizing(copy_ctx, monkeypatch, tmp_db):
    _arm_copy_entry(monkeypatch)
    signal = lanes.trusted_copy(copy_ctx)
    assert signal is not None
    monkeypatch.setattr(engine, "_size_for", lambda *_args: pytest.fail("sizer reached"))

    decision = engine.decide(signal, tmp_db)

    assert decision.action is Action.SKIP
    assert decision.blockers == ["no_dossier"]


def test_stale_dossier_blocks_before_sizing(copy_ctx, monkeypatch, tmp_db):
    _arm_copy_entry(monkeypatch)
    signal = lanes.trusted_copy(copy_ctx)
    assert signal is not None
    stale = copy_ctx.dossier.model_copy(
        update={"built_at_ms": copy_ctx.now_ms - (engine.DOSSIER_MAX_AGE_S + 1) * 1000}
    )
    _store_dossier(tmp_db, stale)
    monkeypatch.setattr(engine, "_size_for", lambda *_args: pytest.fail("sizer reached"))

    decision = engine.decide(signal, tmp_db)

    assert decision.action is Action.SKIP
    assert decision.blockers == ["no_dossier"]
    assert "old" in decision.thesis


def test_quarantined_dossier_blocks_before_sizing(copy_ctx, monkeypatch, tmp_db):
    _arm_copy_entry(monkeypatch)
    signal = lanes.trusted_copy(copy_ctx)
    assert signal is not None
    quarantined = copy_ctx.dossier.model_copy(update={"grade": Grade.QUARANTINED})
    _store_dossier(tmp_db, quarantined)
    monkeypatch.setattr(engine, "_size_for", lambda *_args: pytest.fail("sizer reached"))

    decision = engine.decide(signal, tmp_db)

    assert decision.action is Action.SKIP
    assert decision.blockers == ["dossier_quarantined"]


def test_engine_rejects_a_transfer_in_copy_signal_even_with_a_clean_dossier(
    copy_ctx, monkeypatch, tmp_db
):
    _arm_copy_entry(monkeypatch)
    signal = lanes.trusted_copy(copy_ctx)
    assert signal is not None
    transfer_signal = signal.model_copy(
        update={
            "payload": {
                **signal.payload,
                "source_side": "buy",
                "source_event": "transfer_in",
            }
        }
    )
    _store_dossier(tmp_db, copy_ctx.dossier)

    decision = engine.decide(transfer_signal, tmp_db)

    assert decision.action is Action.SKIP
    assert decision.blockers == ["copy_source_transfer_in"]


def test_engine_rejects_a_copy_signal_without_a_decoded_buy_marker(copy_ctx, monkeypatch, tmp_db):
    _arm_copy_entry(monkeypatch)
    signal = lanes.trusted_copy(copy_ctx)
    assert signal is not None
    signal = signal.model_copy(
        update={
            "payload": {
                key: value
                for key, value in signal.payload.items()
                if key not in {"source_side", "source_event"}
            }
        }
    )
    _store_dossier(tmp_db, copy_ctx.dossier)

    decision = engine.decide(signal, tmp_db)

    assert decision.action is Action.SKIP
    assert decision.blockers == ["copy_source_not_decoded_buy"]


def test_engine_rechecks_copy_source_cohort_before_entry(copy_ctx, monkeypatch, tmp_db):
    _arm_copy_entry(monkeypatch)
    signal = lanes.trusted_copy(copy_ctx)
    assert signal is not None
    tmp_db.execute("UPDATE wallets SET cohort='tracked' WHERE address=?", (WALLET,))
    _store_dossier(tmp_db, copy_ctx.dossier)

    decision = engine.decide(signal, tmp_db)

    assert decision.action is Action.SKIP
    assert decision.blockers == ["copy_source_not_trusted"]


@pytest.mark.parametrize("cohort", ["tracked", "research", "blacklist", None])
def test_feed_claim_cannot_promote_a_wallet_into_trusted_copy(copy_ctx, tmp_db, cohort):
    tmp_db.execute("UPDATE wallets SET cohort=? WHERE address=?", (cohort, WALLET))
    copy_ctx.recent_buys[0]["cohort"] = "trusted_copy"
    assert lanes.trusted_copy(copy_ctx) is None
    assert tmp_db.execute("SELECT cohort FROM wallets WHERE address=?", (WALLET,)).fetchone()[0] == cohort


@pytest.mark.parametrize("unknown", ["can_sell", "mint_authority_revoked", "bundler_pct"])
def test_unknown_dyor_blocks_copy_signal_and_decision(copy_ctx, tmp_db, monkeypatch, unknown):
    _arm_copy_entry(monkeypatch)
    signal = lanes.trusted_copy(copy_ctx)
    copy_ctx.dossier = copy_ctx.dossier.model_copy(update={"unknowns": [unknown]})
    _store_dossier(tmp_db, copy_ctx.dossier)
    decision = engine.decide(signal, tmp_db)
    assert decision.action is Action.SKIP
    assert decision.blockers == ["unknown_safety"]
    assert unknown in decision.thesis
    assert lanes.trusted_copy(copy_ctx) is None
