"""Core contracts: schemas, migrations, the event bus, the limiter and the journal."""

from __future__ import annotations

import time
from decimal import Decimal

import pytest

from kaiba.core import db, events, journal, limiter
from kaiba.core.config import RiskConfig, load_risk, save_risk
from kaiba.core.schemas import (
    Chain,
    EventKind,
    EvidenceBasis,
    Grade,
    Lane,
    LaneMode,
    Measure,
    Receipt,
    Wallet,
    digest,
    infer_chain,
    looks_evm,
    looks_solana,
    normalize_address,
)

EVM = "0x68EEE5c2FE8883A63CD9E5F0e71a3116FB728B3a"
SOL = "CJF7MNqb9xv1XrTs5St1Du5JuQXLsfFBB137vmYRKpnb"


# ---------------------------------------------------------------- addresses & schemas


def test_address_shapes():
    assert looks_evm(EVM) and not looks_solana(EVM)
    assert looks_solana(SOL) and not looks_evm(SOL)
    assert infer_chain(EVM) is Chain.ETH
    assert infer_chain(SOL) is Chain.SOL
    assert infer_chain("nonsense") is None


def test_evm_is_lowercased_solana_is_not():
    assert normalize_address(EVM, Chain.ROBINHOOD) == EVM.lower()
    assert normalize_address(SOL, Chain.SOL) == SOL
    with pytest.raises(ValueError):
        normalize_address(SOL, Chain.ETH)


def test_missing_evidence_is_unknown_not_zero():
    m = Measure.unknown()
    assert m.value is None
    assert not m.known
    assert m.basis is EvidenceBasis.UNAVAILABLE
    known = Measure(value=Decimal("1.5"), basis=EvidenceBasis.VERIFIED_ONCHAIN, receipt=Receipt(provider="p", endpoint="e"))
    assert known.known and not known.stale


def test_digest_is_stable_across_key_order():
    assert digest({"a": 1, "b": 2}) == digest({"b": 2, "a": 1})


def test_wallet_key():
    w = Wallet(address=SOL, chain=Chain.SOL)
    assert w.key == f"sol:{SOL}"


# ---------------------------------------------------------------- db & events


def test_migrations_create_expected_tables(tmp_db):
    names = {
        r["name"] for r in tmp_db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    for expected in {
        "events", "wallets", "wallet_scores", "tokens", "swaps", "transfers",
        "cluster_edges", "entities", "token_dossiers", "first_buyers", "callers",
        "signals", "decisions", "orders", "positions", "trades", "journal",
        "playbook", "experiments", "opportunities", "provider_state",
    }:
        assert expected in names, f"missing table {expected}"


def test_migrations_are_idempotent(tmp_db):
    assert db.migrate(tmp_db) == []  # already applied by the fixture


def test_event_roundtrip_and_tail(tmp_db):
    first = events.emit(EventKind.SYSTEM, {"hello": "world"}, conn=tmp_db)
    second = events.emit(
        EventKind.TOKEN_CREATED, {"mint": SOL}, chain=Chain.SOL, subject=SOL, conn=tmp_db
    )
    assert first and second and second > first

    after_first = events.tail(after_id=first, conn=tmp_db)
    assert [e.id for e in after_first] == [second]
    assert after_first[0].payload["mint"] == SOL
    assert after_first[0].chain is Chain.SOL

    only_created = events.tail(0, kinds=[EventKind.TOKEN_CREATED.value], conn=tmp_db)
    assert [e.id for e in only_created] == [second]
    assert events.latest_id(conn=tmp_db) == second


def test_event_dedupe_key_blocks_the_second_write(tmp_db):
    assert events.emit(EventKind.SYSTEM, {"a": 1}, dedupe_key="same", conn=tmp_db) is not None
    assert events.emit(EventKind.SYSTEM, {"a": 1}, dedupe_key="same", conn=tmp_db) is None
    assert len(events.tail(0, conn=tmp_db)) == 1


def test_emit_once_derives_its_own_key(tmp_db):
    payload = {"mint": SOL, "slot": 1}
    assert events.emit_once(EventKind.TOKEN_CREATED, payload, conn=tmp_db) is not None
    assert events.emit_once(EventKind.TOKEN_CREATED, payload, conn=tmp_db) is None


# ---------------------------------------------------------------- limiter


def test_limiter_charges_capacity_and_enforces_interval(tmp_db):
    # generous bucket, 1s floor
    lim = limiter.Limits(min_interval_ms=1000, capacity=10, refill_per_s=1.0, max_inflight=4)
    limiter.DEFAULTS["unittest"] = lim
    try:
        weight = limiter.reserve("unittest", "read", conn=tmp_db)
        assert weight == 1
        limiter.release("unittest", "read", status="ok", weight=weight, conn=tmp_db)

        with pytest.raises(limiter.RateLimited) as exc:
            limiter.reserve("unittest", "read", conn=tmp_db)
        assert "minimum interval" in str(exc.value)
    finally:
        limiter.DEFAULTS.pop("unittest", None)


def test_high_priority_bypasses_most_of_the_interval(tmp_db):
    limiter.DEFAULTS["prio"] = limiter.Limits(
        min_interval_ms=400, capacity=10, refill_per_s=10.0, max_inflight=4
    )
    try:
        limiter.reserve("prio", "read", conn=tmp_db)
        limiter.release("prio", "read", weight=1, conn=tmp_db)
        time.sleep(0.12)  # >400/4 ms, well under the 400 ms research floor
        limiter.reserve("prio", "read", priority=limiter.Priority.EXIT, conn=tmp_db)
    finally:
        limiter.DEFAULTS.pop("prio", None)


def test_rate_limit_opens_a_family_cooldown(tmp_db):
    limiter.DEFAULTS["banme"] = limiter.Limits(
        min_interval_ms=0, capacity=100, refill_per_s=100.0, max_inflight=4, ban_floor_s=30
    )
    try:
        w = limiter.reserve("banme", "trade.swap", conn=tmp_db)
        limiter.release("banme", "trade.swap", status="rate_limited", weight=w, conn=tmp_db)

        with pytest.raises(limiter.RateLimited) as exc:
            limiter.reserve("banme", "trade.quote", conn=tmp_db)  # same "trade" family
        assert "cooldown" in str(exc.value)

        # a different family is unaffected
        limiter.reserve("banme", "token.info", conn=tmp_db)
    finally:
        limiter.DEFAULTS.pop("banme", None)


def test_weights_come_from_the_endpoint_table(tmp_db):
    assert limiter.limits_for("gmgn").weight_for("quote") == 10
    assert limiter.limits_for("gmgn").weight_for("token.info") == 1


def test_guarded_records_a_call_row(tmp_db):
    limiter.DEFAULTS["guard"] = limiter.Limits(min_interval_ms=0, capacity=50, refill_per_s=50.0)
    try:
        with limiter.guarded("guard", "x", conn=tmp_db):
            pass
        rows = list(tmp_db.execute("SELECT * FROM provider_calls WHERE provider='guard'"))
        assert rows and rows[0]["status"] == "ok"
    finally:
        limiter.DEFAULTS.pop("guard", None)


# ---------------------------------------------------------------- journal


def test_journal_chain_verifies(tmp_db):
    journal.append("observation", "first thing", conn=tmp_db)
    journal.append("lesson", "second thing", subject="sol:ABC", conn=tmp_db)
    ok, err = journal.verify(tmp_db)
    assert ok and err is None
    assert journal.stats(tmp_db)["total"] == 2


def test_tampering_breaks_the_chain(tmp_db):
    journal.append("observation", "original", conn=tmp_db)
    journal.append("observation", "second", conn=tmp_db)
    tmp_db.execute("UPDATE journal SET body='rewritten' WHERE seq=1")
    ok, err = journal.verify(tmp_db)
    assert not ok and "seq 1" in err


def test_journal_rejects_an_empty_body(tmp_db):
    with pytest.raises(ValueError):
        journal.append("lesson", "   ", conn=tmp_db)


# ---------------------------------------------------------------- risk envelope


def test_kill_switch_forces_every_lane_off():
    cfg = RiskConfig(global_mode=LaneMode.LIVE, kill_switch=True)
    assert cfg.effective_mode(Lane.CONFLUENCE_5) is LaneMode.OFF


def test_lane_never_runs_hotter_than_the_envelope():
    cfg = load_risk()
    cfg.global_mode = LaneMode.LIVE
    cfg.bounds.max_lane_mode = LaneMode.CANARY
    cfg.lanes[Lane.CONFLUENCE_5].mode = LaneMode.LIVE
    assert cfg.effective_mode(Lane.CONFLUENCE_5) is LaneMode.CANARY


def test_saving_cannot_widen_operator_bounds(tmp_path):
    path = tmp_path / "risk.yaml"
    save_risk(RiskConfig(bounds={"max_size_pct_bankroll": 3.0}), path)
    tampered = load_risk(path)
    tampered.bounds.max_size_pct_bankroll = 99.0
    tampered.global_mode = LaneMode.LIVE
    save_risk(tampered, path)

    reloaded = load_risk(path)
    assert reloaded.bounds.max_size_pct_bankroll == 3.0  # bounds survived
    assert reloaded.global_mode is LaneMode.LIVE          # tunables were written


def test_shipped_risk_file_is_valid_and_conservative():
    from kaiba.core.config import DEFAULT_RISK_PATH

    cfg = load_risk(DEFAULT_RISK_PATH)  # the file a fresh install gets, not the test fixture
    assert cfg.version
    assert cfg.bounds.max_concurrent_positions is None  # owner mandate: no count cap
    assert cfg.global_mode is not LaneMode.LIVE, "shipped default must not be live"
    for lane in (Lane.CONFLUENCE_5, Lane.TRUSTED_COPY, Lane.CURVE_VELOCITY):
        assert cfg.effective_mode(lane) in {LaneMode.OFF, LaneMode.SHADOW}


def test_grade_enum_has_quarantine():
    assert Grade.QUARANTINED.value == "QUARANTINED"
