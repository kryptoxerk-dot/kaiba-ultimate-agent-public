"""Tests for the live wallet tracker.

Offline by default. Three properties get the most coverage because they are the ones a
future edit is most likely to quietly break:

1. **The tracker cannot mint trust.** Both the enum and the database must refuse it.
2. **Unknown blocks admission.** An unmeasurable failure rate or an unscreened shape is a
   refusal, not a pass, and never a zero.
3. **Windows count entities, not addresses.** Five addresses behind one funder must
   collapse to one opinion, because otherwise ``confluence-5`` is spoofable for 0.1 SOL.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from decimal import Decimal

import pytest

from kaiba.core.schemas import Chain, EvidenceBasis, Grade, WalletTag, now_ms
from kaiba.intelligence import grade as grade_mod
from kaiba.intelligence import tracker

SOL = Chain.SOL
A = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
B = "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
C = "CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"
D = "DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD"
E = "EEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEE"
F = "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF"
MINT = "MintMintMintMintMintMintMintMintMintMintMin"


# --------------------------------------------------------------------------------------
# fixtures and builders
# --------------------------------------------------------------------------------------


def _add_swaps(conn: sqlite3.Connection, wallet: str, *, buys: int, sells: int, token: str = MINT) -> None:
    base = now_ms() - 3_600_000
    n = 0
    for side, count in (("buy", buys), ("sell", sells)):
        for i in range(count):
            n += 1
            conn.execute(
                "INSERT INTO swaps (chain, tx, slot, block_index, ts_ms, wallet, token, side, "
                " amount_token, amount_native, price_usd, usd_value, program, source, is_create_tx, "
                " fee_payer) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,NULL)",
                (
                    SOL.value, f"tx-{wallet[:4]}-{side}-{i}", 1000 + n, i, base + n * 1000,
                    wallet, token, side, "1000000", "1000000000", "0.001", "100",
                    "pump", "test",
                ),
            )


def _score(conn: sqlite3.Connection, address: str, grade: Grade = Grade.C, score: float = 30.0) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO wallet_scores (chain, address, score, grade, evidence_weight, "
        " archetype, model_version, scored_at_ms) VALUES (?,?,?,?,?,?,?,?)",
        (SOL.value, address, score, grade.value, 50.0, "trader", grade_mod.MODEL_ID, now_ms()),
    )


def _clean_wallet(conn: sqlite3.Connection, address: str, grade: Grade = Grade.C) -> None:
    """A wallet that passes every rule except the one a test is about to break."""
    _add_swaps(conn, address, buys=12, sells=10)
    _score(conn, address, grade)


@pytest.fixture
def no_helius(monkeypatch):
    """Force the failure-rate measurement to a clean, low value without a network call."""

    def _fake(chain, address, conn=None, *, config=tracker.DEFAULT_CONFIG):
        from kaiba.core.schemas import Receipt

        return (
            0.02,
            {"signatures_seen": 500, "signatures_failed": 10, "last_activity_ms": now_ms()},
            Receipt(provider="helius", endpoint="tx.getTransactionsForAddress"),
        )

    monkeypatch.setattr(tracker, "measure_failure_rate", _fake)
    return _fake


@pytest.fixture
def helius_unavailable(monkeypatch):
    def _fake(chain, address, conn=None, *, config=tracker.DEFAULT_CONFIG):
        from kaiba.core.schemas import Receipt

        return (
            None,
            {"reason": "helius down", "signatures_seen": None, "signatures_failed": None},
            Receipt(provider="helius", endpoint="tx.getTransactionsForAddress",
                    basis=EvidenceBasis.UNAVAILABLE, note="helius down"),
        )

    monkeypatch.setattr(tracker, "measure_failure_rate", _fake)
    return _fake


# --------------------------------------------------------------------------------------
# 1. the tracker cannot mint trust
# --------------------------------------------------------------------------------------


def test_tier_enum_has_no_trusted_member():
    assert {t.value for t in tracker.Tier} == {"observe", "candidate"}
    assert not hasattr(tracker.Tier, "TRUSTED")


def test_database_refuses_a_trusted_tier(tmp_db):
    """Even a caller that bypasses the enum entirely cannot write trust."""
    with pytest.raises(sqlite3.IntegrityError):
        tmp_db.execute(
            "INSERT INTO tracker_watchlist (chain, address, tier, reason, source, added_by, added_ms) "
            "VALUES (?,?,?,?,?,?,?)",
            (SOL.value, A, "trusted", "r", "s", "who", now_ms()),
        )


def test_set_tier_cannot_reach_trusted(tmp_db, no_helius):
    _clean_wallet(tmp_db, A)
    assert tracker.admit(SOL, A, reason="graded C", source="grade:test", added_by="tester")
    with pytest.raises(ValueError):
        tracker.set_tier(SOL, A, tracker.Tier("trusted"), actor="t", reason="no", conn=tmp_db)


def test_admission_event_disclaims_trust(tmp_db, no_helius):
    from kaiba.core.events import recent

    _clean_wallet(tmp_db, A)
    tracker.admit(SOL, A, reason="graded C", source="grade:test", added_by="tester")
    payloads = [e.payload for e in recent(20, conn=tmp_db) if e.payload.get("tracker")]
    assert any("never promotes" in str(p.get("note", "")) for p in payloads)


# --------------------------------------------------------------------------------------
# 2. the admission screen
# --------------------------------------------------------------------------------------


def test_admit_requires_a_reason_a_source_and_an_author(tmp_db, no_helius):
    _clean_wallet(tmp_db, A)
    for kwargs in (
        {"reason": "  ", "source": "s", "added_by": "w"},
        {"reason": "r", "source": "", "added_by": "w"},
        {"reason": "r", "source": "s", "added_by": " "},
    ):
        with pytest.raises(ValueError):
            tracker.admit(SOL, A, conn=tmp_db, **kwargs)


def test_sell_only_wallet_is_refused_by_grade_rule(tmp_db, no_helius):
    """1,793 trades and zero buys is the shape; grade.py owns the rule and we reuse it."""
    _add_swaps(tmp_db, B, buys=0, sells=40)
    _score(tmp_db, B, Grade.B, 60.0)
    screen = tracker.screen_wallet(SOL, B, tmp_db)
    assert not screen.admissible
    assert any("sell-only" in b for b in screen.blockers)
    assert tracker.admit(SOL, B, reason="r", source="s", added_by="w", conn=tmp_db) is None
    assert tracker.watched_addresses(SOL, tmp_db) == set()


def test_buy_starved_wallet_is_refused(tmp_db, no_helius):
    _add_swaps(tmp_db, C, buys=2, sells=40)  # 4.8% buy share: above 2%, below 10%
    _score(tmp_db, C, Grade.B, 60.0)
    screen = tracker.screen_wallet(SOL, C, tmp_db)
    assert any("buy-starved" in b for b in screen.blockers)


def test_high_failure_rate_is_refused(tmp_db, monkeypatch):
    """23.2%, 23.3% and 35.8% were measured on our own C-or-better wallets."""
    from kaiba.core.schemas import Receipt

    monkeypatch.setattr(
        tracker,
        "measure_failure_rate",
        lambda *a, **k: (
            0.358,
            {"signatures_seen": 1000, "signatures_failed": 358, "last_activity_ms": now_ms()},
            Receipt(provider="helius", endpoint="tx.getTransactionsForAddress"),
        ),
    )
    _clean_wallet(tmp_db, D, Grade.B)
    screen = tracker.screen_wallet(SOL, D, tmp_db)
    assert screen.tx_failure_rate == pytest.approx(0.358)
    assert screen.tx_failure_basis is EvidenceBasis.VERIFIED_ONCHAIN
    assert any("failure rate" in b for b in screen.blockers)
    assert tracker.admit(SOL, D, reason="r", source="s", added_by="w", conn=tmp_db) is None


def test_unmeasurable_failure_rate_blocks_and_is_never_zero(tmp_db, helius_unavailable):
    _clean_wallet(tmp_db, A)
    screen = tracker.screen_wallet(SOL, A, tmp_db)
    assert screen.tx_failure_rate is None           # not 0.0
    assert screen.tx_failure_basis is EvidenceBasis.UNAVAILABLE
    assert "tx_failure_rate" in screen.unknowns
    assert not screen.admissible
    assert tracker.admit(SOL, A, reason="r", source="s", added_by="w", conn=tmp_db) is None


def test_ungraded_wallet_is_unassessed_not_average(tmp_db, no_helius):
    _add_swaps(tmp_db, A, buys=12, sells=10)  # no wallet_scores row at all
    screen = tracker.screen_wallet(SOL, A, tmp_db)
    assert "grade" in screen.unknowns
    assert any("never graded" in b for b in screen.blockers)


def test_quarantined_and_low_grades_are_refused(tmp_db, no_helius):
    for addr, grade in ((A, Grade.QUARANTINED), (B, Grade.D), (C, Grade.UNSCORED)):
        _clean_wallet(tmp_db, addr, grade)
        screen = tracker.screen_wallet(SOL, addr, tmp_db)
        assert not screen.admissible, grade


def test_hard_quarantine_tag_is_refused(tmp_db, no_helius):
    _clean_wallet(tmp_db, A, Grade.B)
    tmp_db.execute(
        "INSERT INTO wallets (chain, address, source, tags_json, first_seen_ms, last_seen_ms) "
        "VALUES (?,?,?,?,?,?)",
        (SOL.value, A, "test", f'["{WalletTag.MEV_BOT.value}"]', now_ms(), now_ms()),
    )
    screen = tracker.screen_wallet(SOL, A, tmp_db)
    assert any("hard quarantine" in b for b in screen.blockers)


def test_thin_sample_cannot_be_shape_checked(tmp_db, no_helius):
    _add_swaps(tmp_db, A, buys=2, sells=1)  # under grade.SELL_ONLY_MIN_TRADES
    _score(tmp_db, A, Grade.B)
    screen = tracker.screen_wallet(SOL, A, tmp_db)
    assert screen.buy_share is None
    assert screen.buy_share_basis is EvidenceBasis.UNAVAILABLE
    assert "buy_share" in screen.unknowns


# --------------------------------------------------------------------------------------
# 3. the watchlist is auditable
# --------------------------------------------------------------------------------------


def test_admission_is_auditable(tmp_db, no_helius):
    _clean_wallet(tmp_db, A, Grade.B)
    entry = tracker.admit(
        SOL, A, reason="graded B on 11 closed trades", source="grade:kaiba-wallet-v1",
        added_by="operator:alice", tier=tracker.Tier.OBSERVE, conn=tmp_db,
    )
    assert entry is not None
    assert entry.reason.startswith("graded B")
    assert entry.source == "grade:kaiba-wallet-v1"
    assert entry.added_by == "operator:alice"
    assert entry.grade_at_add is Grade.B
    assert entry.screen["tx_failure_rate"] == pytest.approx(0.02)

    trail = tracker.audit_trail(SOL, A, tmp_db)
    assert [t["action"] for t in trail] == ["admitted"]
    assert trail[0]["actor"] == "operator:alice"
    assert trail[0]["payload"]["screen"]["buy_share_basis"] == EvidenceBasis.VERIFIED_ONCHAIN.value


def test_a_second_nomination_does_not_rewrite_who_admitted_it(tmp_db, no_helius):
    """Corroboration, not a rewrite: the row must keep the decision that admitted it."""
    _clean_wallet(tmp_db, A, Grade.B)
    first = tracker.admit(
        SOL, A, reason="graded B", source="grade:kaiba-wallet-v1",
        added_by="operator:alice", conn=tmp_db,
    )
    again = tracker.admit(
        SOL, A, reason="surfaced by discovery", source="discovery:run-7",
        added_by="seed_from_discovery", conn=tmp_db,
    )
    assert again is not None
    assert again.source == "grade:kaiba-wallet-v1"
    assert again.added_by == "operator:alice"
    assert again.added_ms == first.added_ms
    trail = tracker.audit_trail(SOL, A, tmp_db)
    assert [t["action"] for t in trail] == ["admitted", "rescreened"]
    assert "discovery:run-7" in trail[1]["detail"]
    assert trail[1]["payload"]["additional_source"] == "discovery:run-7"


def test_readmission_after_removal_records_the_new_decision(tmp_db, no_helius):
    _clean_wallet(tmp_db, A, Grade.B)
    tracker.admit(SOL, A, reason="first", source="grade:x", added_by="one", conn=tmp_db)
    tracker.remove(SOL, A, reason="went quiet", actor="one", conn=tmp_db)
    back = tracker.admit(SOL, A, reason="active again", source="grade:y", added_by="two",
                         conn=tmp_db)
    assert back is not None and back.source == "grade:y" and back.added_by == "two"
    assert back.status == "active" and back.removed_reason is None
    assert [t["action"] for t in tracker.audit_trail(SOL, A, tmp_db)] == [
        "admitted", "removed", "admitted",
    ]


def test_refusals_are_logged_too(tmp_db, no_helius):
    _add_swaps(tmp_db, B, buys=0, sells=40)
    _score(tmp_db, B, Grade.B)
    assert tracker.admit(SOL, B, reason="r", source="s", added_by="w", conn=tmp_db) is None
    trail = tracker.audit_trail(SOL, B, tmp_db)
    assert [t["action"] for t in trail] == ["refused"]
    assert "sell-only" in trail[0]["detail"]


def test_removal_keeps_the_history(tmp_db, no_helius):
    _clean_wallet(tmp_db, A)
    tracker.admit(SOL, A, reason="r", source="s", added_by="w", conn=tmp_db)
    assert tracker.remove(SOL, A, reason="went quiet", actor="w", conn=tmp_db) is True
    assert tracker.watched_addresses(SOL, tmp_db) == set()
    entry = tracker.get_entry(SOL, A, tmp_db)
    assert entry is not None and entry.status == "removed"
    assert entry.removed_reason == "went quiet"
    assert [t["action"] for t in tracker.audit_trail(SOL, A, tmp_db)] == ["admitted", "removed"]
    assert tracker.remove(SOL, A, reason="again", actor="w", conn=tmp_db) is False


def test_retier_between_the_two_legal_tiers(tmp_db, no_helius):
    _clean_wallet(tmp_db, A)
    tracker.admit(SOL, A, reason="r", source="s", added_by="w", conn=tmp_db)
    assert tracker.set_tier(SOL, A, tracker.Tier.CANDIDATE, actor="w", reason="forward test",
                            conn=tmp_db)
    assert tracker.get_entry(SOL, A, tmp_db).tier is tracker.Tier.CANDIDATE
    assert tracker.watchlist(SOL, tmp_db, tier=tracker.Tier.OBSERVE) == []


def test_rescreen_evicts_a_wallet_that_went_bot(tmp_db, monkeypatch):
    """A watchlist that only ever grows is the leaderboard problem with a slower clock."""
    from kaiba.core.schemas import Receipt

    clean = (
        0.02,
        {"signatures_seen": 500, "signatures_failed": 10, "last_activity_ms": now_ms()},
        Receipt(provider="helius", endpoint="tx.getTransactionsForAddress"),
    )
    monkeypatch.setattr(tracker, "measure_failure_rate", lambda *a, **k: clean)
    _clean_wallet(tmp_db, A, Grade.B)
    assert tracker.admit(SOL, A, reason="r", source="s", added_by="w", conn=tmp_db)

    gone_bot = (
        0.41,
        {"signatures_seen": 1000, "signatures_failed": 410, "last_activity_ms": now_ms()},
        Receipt(provider="helius", endpoint="tx.getTransactionsForAddress"),
    )
    monkeypatch.setattr(tracker, "measure_failure_rate", lambda *a, **k: gone_bot)
    report = tracker.rescreen(SOL, tmp_db, actor="nightly")
    assert report["screened"] == 1
    assert report["kept"] == []
    assert "failure rate" in report["evicted"][0]["why"]
    assert report["credits"] == 10
    assert tracker.watched_addresses(SOL, tmp_db) == set()
    assert [t["action"] for t in tracker.audit_trail(SOL, A, tmp_db)] == ["admitted", "removed"]


def test_rescreen_keeps_a_wallet_that_still_passes(tmp_db, no_helius):
    _clean_wallet(tmp_db, A, Grade.B)
    tracker.admit(SOL, A, reason="r", source="s", added_by="w", conn=tmp_db)
    report = tracker.rescreen(SOL, tmp_db)
    assert report["kept"] == [A] and report["evicted"] == []
    assert [t["action"] for t in tracker.audit_trail(SOL, A, tmp_db)] == ["admitted", "rescreened"]


def test_seed_from_grades_admits_only_what_survives(tmp_db, no_helius):
    _clean_wallet(tmp_db, A, Grade.B)          # clean
    _add_swaps(tmp_db, B, buys=0, sells=40)    # sell-only
    _score(tmp_db, B, Grade.B)
    _clean_wallet(tmp_db, C, Grade.D)          # below the floor
    admitted, refused = tracker.seed_from_grades(SOL, tmp_db)
    assert [e.address for e in admitted] == [A]
    assert {s.address for s in refused} == {B}   # D is not offered at all


def test_seed_from_discovery_tolerates_a_missing_module(tmp_db, no_helius):
    admitted, refused = tracker.seed_from_discovery(SOL, tmp_db)
    assert admitted == [] and refused == []


# --------------------------------------------------------------------------------------
# 4. detection and latency
# --------------------------------------------------------------------------------------


def _detection(wallet: str, *, block_ms: int, detected_ms: int, route: str = tracker.ROUTE_PUMPFUN,
               tx: str | None = None) -> tracker.Detection:
    return tracker.Detection(
        chain=SOL, wallet=wallet, token=MINT, side="buy", tx=tx or f"sig-{wallet[:4]}-{block_ms}",
        slot=1, block_ms=block_ms, detected_ms=detected_ms, usd_value=Decimal("120"),
        amount_native=1_000_000_000, route=route,
    )


def test_lag_is_none_without_a_block_time_never_zero():
    det = tracker.Detection(
        chain=SOL, wallet=A, token=MINT, side="buy", tx="x", slot=None, block_ms=None,
        detected_ms=now_ms(), usd_value=None, amount_native=None, route=tracker.ROUTE_HELIUS,
    )
    assert det.lag_ms is None


def test_detections_are_deduped_and_emit_observation_events(tmp_db):
    from kaiba.core.events import recent

    now = now_ms()
    det = _detection(A, block_ms=now - 3_000, detected_ms=now)
    assert len(tracker.record_detections(tmp_db, [det])) == 1
    assert len(tracker.record_detections(tmp_db, [det])) == 0  # idempotent
    payload = next(e.payload for e in recent(10, conn=tmp_db) if e.payload.get("tracker"))
    assert payload["observation"] is True
    assert "not a buy signal" in payload["note"]
    assert payload["lag_ms"] == 3_000


def test_latency_report_states_the_verdict_on_p95_not_the_median(tmp_db):
    now = now_ms()
    # Ninety fast detections and ten slow ones. The median clears the 20 s budget easily
    # and the p95 does not, which is exactly the shape that makes a median-only verdict
    # dangerous: the lane fails in the tail it was built for.
    dets = [_detection(A, block_ms=now - 2_000, detected_ms=now, tx=f"fast-{i}") for i in range(90)]
    dets += [_detection(A, block_ms=now - 300_000, detected_ms=now, tx=f"slow-{i}") for i in range(10)]
    tracker.record_detections(tmp_db, dets)
    report = tracker.latency_report(tmp_db)
    route = report["routes"][tracker.ROUTE_PUMPFUN]
    assert route["p50_s"] == pytest.approx(2.0)
    assert route["max_s"] == pytest.approx(300.0)
    assert route["viable_for_trusted_copy"] is False
    assert report["copy_delay_budget_s"] == 20


def test_latency_report_is_honest_about_an_empty_sample(tmp_db):
    assert "no detection carried a block time" in tracker.latency_report(tmp_db)["note"]


def test_swap_parsing_from_a_raw_helius_transaction():
    owner = A
    tx = {
        "transaction": {"signatures": ["sig1"], "message": {"accountKeys": [owner, "other"]}},
        "blockTime": 1_700_000_000,
        "slot": 42,
        "meta": {
            "err": None,
            "preBalances": [1_000_000_000, 0],
            "postBalances": [900_000_000, 0],
            "preTokenBalances": [],
            "postTokenBalances": [
                {"owner": owner, "mint": MINT, "uiTokenAmount": {"amount": "5000"}}
            ],
        },
    }
    assert tracker._swap_from_transaction(tx, owner) == (MINT, "buy", -100_000_000)

    tx["meta"]["preTokenBalances"] = [
        {"owner": owner, "mint": MINT, "uiTokenAmount": {"amount": "5000"}}
    ]
    tx["meta"]["postTokenBalances"] = []
    tx["meta"]["postBalances"] = [1_100_000_000, 0]
    assert tracker._swap_from_transaction(tx, owner) == (MINT, "sell", 100_000_000)


def test_failed_transactions_are_not_trades():
    tx = {
        "transaction": {"signatures": ["s"], "message": {"accountKeys": [A]}},
        "meta": {"err": {"InstructionError": [0, "Custom"]},
                 "postTokenBalances": [{"owner": A, "mint": MINT, "uiTokenAmount": {"amount": "1"}}]},
    }
    assert tracker._swap_from_transaction(tx, A) is None


def test_poll_token_handles_a_mint_we_have_never_seen(tmp_db, monkeypatch):
    """Regression: a sweep runs on mints with no ``tokens`` row, so decimals are unknown.

    ``token_flow.token_decimals`` returns ``None`` there and ``parse_trade`` raises on it.
    The live run died on its second minute for exactly this.
    """
    from kaiba.core.schemas import Receipt

    now = now_ms()
    page = {
        "trades": [
            {
                "txId": "sigX", "blockId": 1, "txIndex": 0, "blockTimeMs": now - 2_000,
                "side": "buy", "kind": "swap", "venue": "pump",
                "trader": {"address": A},
                "baseAmount": {"raw": "1000000", "decimals": 6},
                "quoteAmount": {"raw": "1000000000", "decimals": 9},
                "quote": {"id": "So11111111111111111111111111111111111111112"},
                "priceUsd": "0.001", "valueUsd": "120", "ordinalKey": "1-1-1-1",
            }
        ]
    }
    monkeypatch.setattr(
        tracker.token_flow, "fetch_trades_page",
        lambda *a, **k: (page, Receipt(provider="pumpfun", endpoint="coins.trades")),
    )
    assert tmp_db.execute("SELECT COUNT(*) FROM tokens").fetchone()[0] == 0
    result = tracker.poll_token(SOL, MINT, tmp_db, watched={A})
    assert result.ok and result.rows_seen == 1
    assert [d.side for d in result.detections] == ["buy"]
    assert result.detections[0].lag_ms is not None


def test_a_sol_only_transaction_is_not_a_trade():
    tx = {
        "transaction": {"signatures": ["s"], "message": {"accountKeys": [A]}},
        "meta": {"err": None, "preTokenBalances": [], "postTokenBalances": [],
                 "preBalances": [1], "postBalances": [2]},
    }
    assert tracker._swap_from_transaction(tx, A) is None


# --------------------------------------------------------------------------------------
# 5. entity-counted windows — the anti-spoofing property
# --------------------------------------------------------------------------------------


def _buy(conn: sqlite3.Connection, wallet: str, ts_ms: int, usd: str = "100", tx: str = "") -> None:
    conn.execute(
        "INSERT INTO swaps (chain, tx, slot, block_index, ts_ms, wallet, token, side, amount_token, "
        " amount_native, price_usd, usd_value, program, source, is_create_tx, fee_payer) "
        "VALUES (?,?,?,?,?,?,?,'buy',?,?,?,?,?,?,0,NULL)",
        (SOL.value, tx or f"buy-{wallet[:4]}-{ts_ms}", 1, 0, ts_ms, wallet, MINT,
         "1000", "1000000000", "0.001", usd, "pump", "test"),
    )


def _one_entity(conn: sqlite3.Connection, members: list[str]) -> str:
    entity_id = "sol:ent:testentity0001"
    conn.execute(
        "INSERT INTO entities (entity_id, chain, label, archetype, confidence, size, "
        " edge_types_json, created_ms, updated_ms, version) VALUES (?,?,NULL,'trader',0.9,?,'[]',?,?,1)",
        (entity_id, SOL.value, len(members), now_ms(), now_ms()),
    )
    for address in members:
        conn.execute(
            "INSERT INTO entity_members (entity_id, chain, address) VALUES (?,?,?)",
            (entity_id, SOL.value, address),
        )
    return entity_id


def test_five_addresses_in_one_window_is_five_entities_when_unclustered(tmp_db):
    now = now_ms()
    for i, addr in enumerate([A, B, C, D, E]):
        _buy(tmp_db, addr, now - 60_000 + i * 1_000)
    windows = tracker.scan_windows(SOL, MINT, tmp_db)
    best = max(windows, key=lambda w: (w.entity_count, w.buyer_count))
    assert best.buyer_count == 5
    assert best.entity_count == 5
    assert best.qualifying is True


def test_five_addresses_behind_one_funder_are_one_opinion(tmp_db):
    """The spoofing case: without this collapse, confluence-5 costs 0.1 SOL to fake."""
    now = now_ms()
    for i, addr in enumerate([A, B, C, D, E]):
        _buy(tmp_db, addr, now - 60_000 + i * 1_000)
    _one_entity(tmp_db, [A, B, C, D, E])
    windows = tracker.scan_windows(SOL, MINT, tmp_db)
    best = max(windows, key=lambda w: (w.entity_count, w.buyer_count))
    assert best.buyer_count == 5
    assert best.entity_count == 1
    assert best.qualifying is False


def test_a_mixed_window_counts_the_operator_once(tmp_db):
    now = now_ms()
    for i, addr in enumerate([A, B, C, D, E, F]):
        _buy(tmp_db, addr, now - 60_000 + i * 1_000)
    _one_entity(tmp_db, [A, B, C])
    windows = tracker.scan_windows(SOL, MINT, tmp_db)
    best = max(windows, key=lambda w: (w.entity_count, w.buyer_count))
    assert best.buyer_count == 6
    assert best.entity_count == 4   # one entity plus D, E, F
    assert best.qualifying is False


def test_buys_outside_the_window_do_not_count(tmp_db):
    now = now_ms()
    for i, addr in enumerate([A, B, C]):
        _buy(tmp_db, addr, now - 600_000 + i * 1_000)   # ten minutes ago
    for i, addr in enumerate([D, E]):
        _buy(tmp_db, addr, now - 10_000 + i * 1_000)
    windows = tracker.scan_windows(SOL, MINT, tmp_db)
    assert max(w.entity_count for w in windows) == 3


def test_a_buy_with_no_usd_value_is_not_counted(tmp_db):
    """Unknown is not average: an unpriced buy cannot be shown to clear the $50 floor."""
    now = now_ms()
    for i, addr in enumerate([A, B, C, D]):
        _buy(tmp_db, addr, now - 60_000 + i * 1_000)
    tmp_db.execute(
        "INSERT INTO swaps (chain, tx, slot, block_index, ts_ms, wallet, token, side, amount_token, "
        " amount_native, price_usd, usd_value, program, source, is_create_tx, fee_payer) "
        "VALUES (?,?,1,0,?,?,?,'buy','1000','1000000000',NULL,NULL,'pump','test',0,NULL)",
        (SOL.value, "unpriced", now - 55_000, E, MINT),
    )
    windows = tracker.scan_windows(SOL, MINT, tmp_db)
    assert max(w.entity_count for w in windows) == 4


def test_small_buys_are_below_the_floor(tmp_db):
    now = now_ms()
    for i, addr in enumerate([A, B, C, D, E]):
        _buy(tmp_db, addr, now - 60_000 + i * 1_000, usd="10")
    assert tracker.scan_windows(SOL, MINT, tmp_db) == []


def test_windows_are_persisted_including_the_misses(tmp_db):
    now = now_ms()
    for i, addr in enumerate([A, B, C]):
        _buy(tmp_db, addr, now - 60_000 + i * 1_000)
    tracker.scan_windows(SOL, MINT, tmp_db)
    rows = tmp_db.execute("SELECT entities, qualifying FROM tracker_windows").fetchall()
    assert rows, "a non-qualifying window must still be recorded"
    assert all(r["qualifying"] == 0 for r in rows)


def test_scan_recent_tokens_summarises_the_negative_result(tmp_db):
    now = now_ms()
    for i, addr in enumerate([A, B, C]):
        _buy(tmp_db, addr, now - 60_000 + i * 1_000)
    report = tracker.scan_recent_tokens(SOL, tmp_db)
    assert report["tokens_scanned"] == 1
    assert report["max_entities_in_any_window"] == 3
    assert report["qualifying_windows"] == 0
    assert report["required_entities"] == 5
    assert report["entity_table_populated"] is False
    assert report["collapse_effective"] is False
    assert report["lane_reachable"]["confluence_5_reachable"] is False


def test_a_populated_entity_table_does_not_prove_the_collapse_fired(tmp_db):
    """Measured on the live run: 174 entities existed and the collapse fired zero times."""
    now = now_ms()
    for i, addr in enumerate([A, B, C]):
        _buy(tmp_db, addr, now - 60_000 + i * 1_000)
    _one_entity(tmp_db, [D, E])   # an entity that shares no member with the buyers
    report = tracker.scan_recent_tokens(SOL, tmp_db)
    assert report["entity_table_populated"] is True
    assert report["windows_where_collapse_fired"] == 0
    assert report["collapse_effective"] is False

    # Now put two of the actual buyers in one entity: the collapse has something to do.
    for address in (A, B):
        tmp_db.execute(
            "INSERT INTO entity_members (entity_id, chain, address) VALUES (?,?,?)",
            ("sol:ent:testentity0001", SOL.value, address),
        )
    report = tracker.scan_recent_tokens(SOL, tmp_db)
    assert report["windows_where_collapse_fired"] > 0
    assert report["collapse_effective"] is True


def test_lane_reachability_is_a_ceiling_on_graded_wallets(tmp_db):
    """lanes.py grades every buyer before counting entities, so coverage caps the lane."""
    reach = tracker.lane_reachability(SOL, tmp_db)
    assert reach["dossier_grade_floor"] == "B"
    assert reach["wallets_at_or_above_floor"] == 0
    assert reach["confluence_5_reachable"] is False

    for i in range(5):
        _score(tmp_db, f"{chr(ord('G') + i)}" * 43, Grade.B, 60.0)
    reach = tracker.lane_reachability(SOL, tmp_db)
    assert reach["wallets_at_or_above_floor"] == 5
    assert reach["confluence_5_reachable"] is True
    assert reach["sm_trenches_reachable"] is True


# --------------------------------------------------------------------------------------
# 6. cost arithmetic and the viability verdict
# --------------------------------------------------------------------------------------


def test_twenty_second_polling_does_not_fit_the_free_tier():
    assert tracker.max_free_tier_wallets(20) == 0
    projection = tracker.projected_wallet_cost(10, 20)
    assert projection["fits_free_tier"] is False
    assert projection["multiple_of_allowance"] > 12


def test_the_free_tier_ceiling_is_a_five_minute_interval():
    assert tracker.max_free_tier_wallets(300) >= 10
    projection = tracker.projected_wallet_cost(10, 300)
    assert projection["fits_free_tier"] is True
    assert projection["expected_lag_s"] > 20   # affordable and far outside the lane budget


def test_projection_degenerates_safely():
    assert tracker.projected_wallet_cost(0, 10)["credits"] == 0
    assert tracker.max_free_tier_wallets(0) == 0


def test_config_reads_the_lane_thresholds_from_risk_yaml(tmp_path, monkeypatch):
    # The thresholds are the operator's (confluence-5 went 120 s / 5 entities / $50 ->
    # 1800 s / 2 / $20 on purpose), so this pins the READ, not a value: the shipped file
    # with its lane params replaced by values no default shares must come back verbatim.
    import yaml

    from kaiba.core.config import DEFAULT_RISK_PATH

    raw = yaml.safe_load(Path(DEFAULT_RISK_PATH).read_text(encoding="utf-8"))
    raw["lanes"]["confluence-5"]["params"].update(window_s=777, min_entities=7, min_buy_usd=33)
    raw["lanes"]["trusted-copy"]["params"]["max_copy_delay_s"] = 13
    path = tmp_path / "risk.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    monkeypatch.setenv("KAIBA_RISK_PATH", str(path))

    config = tracker.config_from_risk()
    default = tracker.DEFAULT_CONFIG
    assert (default.confluence_window_s, default.confluence_min_entities,
            default.confluence_min_buy_usd, default.copy_delay_budget_s) != (777, 7, Decimal("33"), 13)
    assert config.confluence_window_s == 777
    assert config.confluence_min_entities == 7
    assert config.confluence_min_buy_usd == Decimal("33")
    assert config.copy_delay_budget_s == 13


def test_webhook_readiness_names_the_blocker():
    readiness = tracker.webhook_readiness()
    assert readiness["ready"] is False
    assert readiness["receiver_implemented"] is True
    assert readiness["credits_per_push"] == 1
    assert "publicly reachable" in readiness["blocker"]


def test_status_reports_without_a_network(tmp_db, no_helius):
    _clean_wallet(tmp_db, A, Grade.B)
    tracker.admit(SOL, A, reason="r", source="s", added_by="w", conn=tmp_db)
    report = tracker.status(SOL, tmp_db)
    assert report["watchlist_active"] == 1
    assert report["watchlist_by_tier"] == {"observe": 1, "candidate": 0}
    assert report["detections"] == 0
    assert "only measured forward performance may promote" in report["never_promotes"]


def test_cost_report_counts_free_and_paid_routes_separately(tmp_db):
    tracker._record_poll(
        tmp_db, SOL,
        tracker.PollResult(tracker.ROUTE_PUMPFUN, MINT, True, 1000, 0, 50), now_ms(),
    )
    tracker._record_poll(
        tmp_db, SOL,
        tracker.PollResult(tracker.ROUTE_HELIUS, A, True, 1900, 10, 25), now_ms(),
    )
    tracker._record_poll(
        tmp_db, SOL,
        tracker.PollResult(tracker.ROUTE_PUMPFUN, MINT, False, 900, 0, 0, note="503"), now_ms(),
    )
    routes = tracker.cost_report(tmp_db)["routes"]
    assert routes[tracker.ROUTE_PUMPFUN]["credits"] == 0
    assert routes[tracker.ROUTE_PUMPFUN]["availability_pct"] == 50.0
    assert routes[tracker.ROUTE_HELIUS]["credits"] == 10


# --------------------------------------------------------------------------------------
# 7. live — skipped unless KAIBA_LIVE_TESTS=1
# --------------------------------------------------------------------------------------


@pytest.mark.live
def test_live_pumpfun_trades_route_is_fresh(tmp_db):
    mints, receipt = tracker.hot_mints(5, tmp_db)
    assert mints, receipt.note
    result = tracker.poll_token(SOL, mints[0], tmp_db)
    assert result.route == tracker.ROUTE_PUMPFUN
    assert result.credits == 0
