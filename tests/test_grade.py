"""Wallet grading: PnL reconstruction, the unified rubric, archetypes and exports.

The invariants worth protecting here are the ones that make a grade trustworthy: exact
integer arithmetic, contaminated positions excluded rather than valued at zero, thin
evidence producing UNSCORED rather than a confident-looking C, and A reserved for wallets
with a real sample behind them.
"""

from __future__ import annotations

import json
from collections import Counter
from decimal import Decimal
from pathlib import Path

import pytest

from kaiba.core.db import fetch_all
from kaiba.core.schemas import (
    HARD_QUARANTINE_TAGS,
    Archetype,
    Chain,
    EventKind,
    Grade,
    WalletScore,
    WalletTag,
)
from kaiba.intelligence import naming, pnl
from kaiba.intelligence.grade import (
    MODEL_ID,
    EarlyMetrics,
    ProviderStats,
    Reputation,
    WalletEvidence,
    build_evidence,
    components_for,
    creator_discount,
    grade_address,
    load_score,
    penalties_for,
    score_history,
    score_wallet,
    store_score,
)

FIXTURES = Path(__file__).parent / "fixtures" / "grade"

SOL_WALLET = "CJF7MNqb9xv1XrTs5St1Du5JuQXLsfFBB137vmYRKpnb"
EVM_WALLET = "0x68EEE5c2FE8883A63CD9E5F0e71a3116FB728B3a"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def swap(
    ts_ms: int,
    token: str,
    side: str,
    amount_token: int | None,
    amount_native: int | None = None,
    usd_value: str | None = None,
) -> dict:
    """A swaps-table row the way SQLite hands it back: big integers as text."""
    return {
        "chain": "sol",
        "ts_ms": ts_ms,
        "token": token,
        "side": side,
        "amount_token": None if amount_token is None else str(amount_token),
        "amount_native": None if amount_native is None else str(amount_native),
        "usd_value": usd_value,
    }


def strong_evidence(**overrides) -> WalletEvidence:
    data = load_fixture("evidence_strong")
    data.update(overrides)
    return WalletEvidence.model_validate(data)


# ============================================================ PnL reconstruction


def test_simple_round_trip_is_exact_integers():
    rows = [
        swap(1_000, "AAA", "buy", 1_000_000_000, 2_000_000_000, "400.00"),
        swap(61_000, "AAA", "sell", 1_000_000_000, 3_000_000_001, "600.25"),
    ]
    (episode,) = pnl.reconstruct(rows, as_of_ms=100_000)
    assert episode.closed_ms == 61_000
    assert episode.cost_native == 2_000_000_000
    assert episode.proceeds_native == 3_000_000_001
    assert episode.realized_pnl_native == 1_000_000_001  # exact, not 1.000000001e9
    assert isinstance(episode.realized_pnl_native, int)
    assert episode.cost_usd == Decimal("400.00")
    assert episode.proceeds_usd == Decimal("600.25")
    assert episode.hold_s == 60
    assert episode.is_win and not episode.is_big_win
    assert not episode.contaminated


def test_partial_exit_then_rebuy_stays_one_episode():
    fixture = load_fixture("swaps_partial_rebuy")
    episodes = pnl.reconstruct(fixture["swaps"], as_of_ms=3_000_000)
    by_token = {e.token: e for e in episodes}
    aaa = by_token["AAA"]
    assert aaa.buys == 2 and aaa.sells == 2
    assert aaa.cost_native == 3_500_000_000
    assert aaa.proceeds_native == 8_400_000_000
    assert aaa.realized_pnl_native == 4_900_000_000
    assert aaa.opened_ms == 1_000_000 and aaa.closed_ms == 1_300_000
    assert aaa.hold_s == 300
    assert aaa.cost_usd == Decimal("700.00") and aaa.proceeds_usd == Decimal("1680.00")


def test_rebuy_after_a_full_exit_opens_a_second_episode():
    rows = [
        swap(1_000, "AAA", "buy", 1_000, 100, "10"),
        swap(2_000, "AAA", "sell", 1_000, 150, "15"),
        swap(3_000, "AAA", "buy", 500, 90, "9"),
        swap(4_000, "AAA", "sell", 500, 40, "4"),
    ]
    first, second = pnl.reconstruct(rows, as_of_ms=9_000)
    assert first.buys == 1 and first.sells == 1 and first.is_win
    assert second.opened_ms == 3_000 and second.closed_ms == 4_000
    assert second.realized_pnl_native == -50 and not second.is_win


def test_dust_remainder_closes_the_episode():
    # 0.05% of the position left behind is rounding, not a held bag.
    rows = [
        swap(1_000, "AAA", "buy", 1_000_000, 500, "50"),
        swap(2_000, "AAA", "sell", 999_500, 900, "90"),
    ]
    (episode,) = pnl.reconstruct(rows, as_of_ms=9_000)
    assert episode.closed_ms == 2_000
    assert episode.leftover_qty == 500


def test_meaningful_remainder_leaves_the_episode_open():
    rows = [
        swap(1_000, "AAA", "buy", 1_000_000, 500, "50"),
        swap(2_000, "AAA", "sell", 400_000, 900, "90"),
    ]
    (episode,) = pnl.reconstruct(rows, as_of_ms=3_000)
    assert episode.closed_ms is None
    assert not episode.is_win and episode.realized_pnl_native == 0
    assert episode.hold_s == 2  # dated against as_of_ms, not wall clock


def test_big_win_needs_six_times_cost_exactly():
    at_six = pnl.reconstruct(
        [swap(0, "AAA", "buy", 100, 1_000, "1"), swap(1_000, "AAA", "sell", 100, 6_000, "6")]
    )[0]
    under = pnl.reconstruct(
        [swap(0, "BBB", "buy", 100, 1_000, "1"), swap(1_000, "BBB", "sell", 100, 5_999, "6")]
    )[0]
    assert at_six.is_big_win
    assert not under.is_big_win


def test_roi_is_decimal_and_exact_to_many_places():
    rows = [
        swap(0, "AAA", "buy", 300, 3, "3"),
        swap(1_000, "AAA", "sell", 300, 4, "4"),
    ]
    (episode,) = pnl.reconstruct(rows)
    assert isinstance(episode.roi, Decimal)
    assert episode.roi == Decimal(1) / Decimal(3) or str(episode.roi).startswith("0.3333333333")


def test_transfer_in_contaminates_and_is_excluded_from_the_summary():
    fixture = load_fixture("swaps_contaminated")
    episodes = pnl.reconstruct(fixture["swaps"], as_of_ms=2_000_000)
    ccc = next(e for e in episodes if e.token == "CCC")
    ddd = next(e for e in episodes if e.token == "DDD")
    assert ccc.contaminated and "transfer_in" in ccc.contamination
    assert not ddd.contaminated

    summary = pnl.summarize(episodes)
    assert summary.closed_episodes == 1
    assert summary.contaminated_episodes == 1
    assert summary.distinct_tokens == 1
    assert summary.realized_pnl_native == 500_000_000  # DDD only; CCC's 2 SOL is ignored
    assert summary.realized_pnl_usd == Decimal("100.00")


def test_selling_inventory_we_never_saw_bought_is_contaminated():
    rows = [swap(1_000, "AAA", "sell", 5_000, 900, "90")]
    (episode,) = pnl.reconstruct(rows, as_of_ms=2_000)
    assert episode.contaminated
    assert "sell_without_buy" in episode.contamination
    assert pnl.summarize([episode]).closed_episodes == 0


def test_missing_usd_leaves_none_rather_than_zero():
    rows = [
        swap(0, "AAA", "buy", 1_000, 100, None),
        swap(1_000, "AAA", "sell", 1_000, 150, "15"),
    ]
    (episode,) = pnl.reconstruct(rows)
    assert episode.cost_usd is None
    assert episode.proceeds_usd == Decimal("15")
    assert episode.realized_pnl_usd is None
    assert pnl.summarize([episode]).realized_pnl_usd is None


def test_summary_aggregates_holds_wins_and_sell_to_buy():
    fixture = load_fixture("swaps_partial_rebuy")
    episodes, summary = pnl.reconstruct_wallet(fixture["swaps"], as_of_ms=3_000_000)
    assert len(episodes) == 2
    assert summary.closed_episodes == 2
    assert summary.distinct_tokens == 2
    assert summary.wins == 2 and summary.win_rate == 1.0
    assert summary.big_wins == 1
    assert summary.median_hold_s == 400 and summary.avg_hold_s == 400
    assert summary.buys == 3 and summary.sells == 3
    assert summary.sell_to_buy_ratio == 1.0
    assert summary.realized_pnl_native == 4_900_000_000 + 900_000_000
    assert summary.realized_pnl_usd == Decimal("1160.00")
    assert summary.first_trade_ms == 1_000_000 and summary.last_trade_ms == 2_500_000


def test_summary_of_nothing_is_unknown_not_zero():
    summary = pnl.summarize([])
    assert summary.closed_episodes == 0
    assert summary.win_rate is None
    assert summary.roi is None
    assert summary.median_hold_s is None
    assert summary.realized_pnl_usd is None


# ============================================================ evidence normalisation


def test_available_components_set_the_evidence_weight():
    ev = strong_evidence()
    factors, missing = components_for(ev)
    assert not missing
    assert sum(f.max_points for f in factors) == 100.0
    score = score_wallet(ev)
    assert score.evidence_weight == 100.0
    assert score.model_version == MODEL_ID == "kaiba-wallet-v1"


def test_three_components_cannot_reach_grade_a():
    ev = WalletEvidence(
        address=SOL_WALLET,
        chain=Chain.SOL,
        provider_stats=ProviderStats(realized_profit_usd=Decimal("2000000"), win_rate=0.95),
        seed_confluence=5,
    )
    factors, missing = components_for(ev)
    names = {f.name for f in factors}
    assert names == {"realized_profit", "win_rate", "seed_confluence"}
    assert "roi" in missing and "early_edge" in missing
    score = score_wallet(ev)
    assert score.evidence_weight == 44.0
    assert score.grade is Grade.B  # capped: normalisation cannot manufacture evidence
    assert any("evidence_weight" in b for b in score.blockers)


def test_unscored_when_the_evidence_is_too_thin():
    ev = WalletEvidence.model_validate(load_fixture("evidence_thin"))
    score = score_wallet(ev)
    assert score.grade is Grade.UNSCORED
    assert score.evidence_weight == 18.0
    assert any("evidence_weight 18.0" in b for b in score.blockers)
    assert any("realized_profit" in b for b in score.blockers)
    assert any("roi" in b for b in score.blockers)


def test_provider_reported_profit_only_earns_sixty_percent_credit():
    provider_only = WalletEvidence(
        address=SOL_WALLET,
        chain=Chain.SOL,
        provider_stats=ProviderStats(realized_profit_usd=Decimal("2000000")),
    )
    reconstructed = WalletEvidence(
        address=SOL_WALLET,
        chain=Chain.SOL,
        pnl=pnl.WalletPnl(closed_episodes=10, realized_pnl_usd=Decimal("2000000")),
    )
    provider_factor = next(f for f in components_for(provider_only)[0] if f.name == "realized_profit")
    our_factor = next(f for f in components_for(reconstructed)[0] if f.name == "realized_profit")
    assert our_factor.points == pytest.approx(20.0)
    assert provider_factor.points == pytest.approx(12.0)
    assert "provider_reported" in (provider_factor.detail or "")
    assert "reconstructed" in (our_factor.detail or "")


def test_grade_bands_follow_the_score():
    grades = []
    for profit, roi, win in (("2000000", "1.6", 0.6), ("30000", "0.5", 0.35), ("2000", "0.2", 0.24)):
        ev = strong_evidence(
            pnl={
                "closed_episodes": 24,
                "distinct_tokens": 40,
                "win_rate": win,
                "realized_pnl_usd": profit,
                "roi": roi,
                "big_wins": 0,
                "median_hold_s": 5400,
                "sell_to_buy_ratio": 0.5,
                "buys": 80,
                "sells": 40,
            },
            early_metrics={"validated_early_tokens": 0, "sniper_tokens": 0, "insider_tokens": 0},
            seed_confluence=0,
            reputation=None,
            tags=[],
        )
        grades.append(score_wallet(ev).grade)
    assert grades[0] in {Grade.A, Grade.B}
    assert grades[1] is Grade.C
    assert grades[2] is Grade.D


# ============================================================ penalties


def test_every_penalty_fires_exactly_once():
    ev = WalletEvidence(
        address=SOL_WALLET,
        chain=Chain.SOL,
        tags=[
            WalletTag.WASH_TRADER,
            WalletTag.DEX_BOT,
            WalletTag.RAT_TRADER,
            WalletTag.BUNDLER,
            WalletTag.TRANSFER_IN,
        ],
        pnl=pnl.WalletPnl(
            closed_episodes=8,
            distinct_tokens=60,
            realized_pnl_usd=Decimal("-5000"),
            median_hold_s=60,
            buys=10,
            sells=0,
        ),
        trade_count_lifetime=150_000,
        tokens_30d=1_200,
    )
    counts = Counter(p.name for p in penalties_for(ev))
    assert counts == {
        "realized_pnl_negative": 1,
        "wash_or_mev": 1,
        "dex_bot": 1,
        "rat_trader": 1,
        "bundler": 1,
        "transfer_in": 1,
        "no_sells": 1,
        "ultra_short_holds": 1,
        "trade_cadence": 1,
        "token_churn_30d": 1,
    }


def test_cadence_penalty_picks_a_single_tier():
    def cadence(n: int) -> float | None:
        ev = WalletEvidence(address=SOL_WALLET, chain=Chain.SOL, trade_count_lifetime=n)
        hits = [p for p in penalties_for(ev) if p.name == "trade_cadence"]
        assert len(hits) <= 1
        return hits[0].points if hits else None

    assert cadence(150_000) == 30.0
    assert cadence(30_000) == 18.0
    assert cadence(12_000) == 8.0
    assert cadence(500) is None


def test_negative_pnl_penalty_cannot_push_the_score_below_zero():
    ev = strong_evidence(
        pnl={
            "closed_episodes": 12,
            "distinct_tokens": 20,
            "win_rate": 0.1,
            "realized_pnl_usd": "-90000",
            "roi": "-0.4",
            "big_wins": 0,
            "median_hold_s": 30,
            "sell_to_buy_ratio": 0.5,
            "buys": 40,
            "sells": 20,
        },
        early_metrics={"validated_early_tokens": 0, "sniper_tokens": 0, "insider_tokens": 0},
        seed_confluence=0,
        reputation=None,
        tags=[],
    )
    score = score_wallet(ev)
    assert score.score >= 0.0
    assert any(p.startswith("realized_pnl_negative") for p in score.penalties)
    assert score.grade is Grade.D


def test_hard_quarantine_tag_short_circuits_before_scoring():
    ev = strong_evidence(tags=["smart_money", "sandwich_bot"])
    score = score_wallet(ev)
    assert score.grade is Grade.QUARANTINED
    assert score.score == 0.0
    assert score.evidence_weight == 0.0
    assert score.factors == []
    assert any("sandwich_bot" in p for p in score.penalties)
    assert score.archetype is Archetype.BOT


@pytest.mark.parametrize("tag", sorted(t.value for t in HARD_QUARANTINE_TAGS))
def test_each_hard_quarantine_tag_quarantines(tag):
    ev = strong_evidence(tags=[tag])
    assert score_wallet(ev).grade is Grade.QUARANTINED


def test_creator_wallet_takes_the_self_dealing_discount():
    clean = strong_evidence()
    creator = strong_evidence(created_token_count=25)  # 25 created vs 40 traded
    assert creator_discount(clean) is None
    assert creator_discount(creator) == Decimal("0.45")
    clean_score = score_wallet(clean)
    creator_score = score_wallet(creator)
    # The stored score is rounded to four places, so compare within that rounding.
    assert creator_score.score == pytest.approx(clean_score.score * 0.45, abs=1e-3)
    assert any("creator_self_dealing" in p for p in creator_score.penalties)
    assert creator_score.archetype is Archetype.DEV


# ============================================================ grade gates


def test_strong_wallet_with_a_real_sample_earns_an_a():
    score = score_wallet(strong_evidence())
    assert score.score >= 70.0
    assert score.grade is Grade.A
    assert score.blockers == []
    assert score.closed_trades == 24 and score.distinct_tokens == 40


def test_high_score_with_four_closed_episodes_is_capped_to_b():
    ev = strong_evidence()
    ev.pnl.closed_episodes = 4
    score = score_wallet(ev)
    assert score.score >= 70.0
    assert score.grade is Grade.B
    assert any("4 closed episodes < 10" in b for b in score.blockers)


def test_high_score_with_a_capped_sample_is_capped_to_b():
    score = score_wallet(strong_evidence(sample_capped=True))
    assert score.score >= 70.0
    assert score.grade is Grade.B
    assert any("sample was truncated" in b for b in score.blockers)


def test_high_score_with_too_few_tokens_is_capped_to_b():
    ev = strong_evidence()
    ev.pnl.distinct_tokens = 3
    ev.provider_stats.token_num = 3
    score = score_wallet(ev)
    assert score.grade is Grade.B
    assert any("distinct tokens < 5" in b for b in score.blockers)


# ============================================================ storage


def test_store_and_load_round_trip(tmp_db):
    score = score_wallet(strong_evidence())
    store_score(score, tmp_db)
    loaded = load_score(Chain.SOL, SOL_WALLET, tmp_db)
    assert loaded is not None
    assert loaded.address == score.address
    assert loaded.grade is score.grade
    assert loaded.score == pytest.approx(score.score)
    assert loaded.evidence_weight == score.evidence_weight
    assert [f.name for f in loaded.factors] == [f.name for f in score.factors]
    assert loaded.realized_pnl_usd == score.realized_pnl_usd
    assert loaded.archetype is score.archetype
    assert load_score(Chain.SOL, "11111111111111111111111111111112", tmp_db) is None


def test_store_appends_history_and_keeps_one_latest_row(tmp_db):
    score = score_wallet(strong_evidence())
    store_score(score, tmp_db)
    downgraded = score.model_copy(update={"score": 41.0, "grade": Grade.B, "scored_at_ms": score.scored_at_ms + 1})
    store_score(downgraded, tmp_db)

    latest = fetch_all(tmp_db, "SELECT * FROM wallet_scores WHERE address = ?", (SOL_WALLET,))
    assert len(latest) == 1 and latest[0]["grade"] == "B"
    history = score_history(Chain.SOL, SOL_WALLET, conn=tmp_db)
    assert [h["grade"] for h in history] == ["B", "A"]


def seed_wallet_rows(conn, swaps: list[dict]) -> None:
    conn.execute(
        "INSERT INTO wallets (chain, address, source, tags_json, first_seen_ms, last_seen_ms, "
        "twitter, meta_json) VALUES (?,?,?,?,?,?,?,?)",
        ("sol", SOL_WALLET, "test", json.dumps(["smart_money", "not_a_real_tag"]), 0, 0, None, "{}"),
    )
    for i, row in enumerate(swaps):
        conn.execute(
            "INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, amount_token, amount_native, "
            "usd_value, source) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "sol", f"tx{i}", row["ts_ms"], SOL_WALLET, row["token"], row["side"],
                row["amount_token"], row["amount_native"], row["usd_value"], "test",
            ),
        )


def test_build_evidence_reconstructs_from_the_swaps_table(tmp_db):
    seed_wallet_rows(tmp_db, load_fixture("swaps_partial_rebuy")["swaps"])
    ev = build_evidence(SOL_WALLET, Chain.SOL, tmp_db)
    assert ev.tags == [WalletTag.SMART_MONEY]  # the unknown provider label is dropped
    assert ev.pnl is not None
    assert ev.pnl.closed_episodes == 2 and ev.pnl.distinct_tokens == 2
    assert ev.pnl.realized_pnl_usd == Decimal("1160.00")
    assert ev.early_metrics is None  # no first_buyers rows: not measured, not zero
    assert ev.seed_confluence is None


def test_grade_address_scores_and_stores(tmp_db):
    seed_wallet_rows(tmp_db, load_fixture("swaps_partial_rebuy")["swaps"])
    score = grade_address(SOL_WALLET, "sol", tmp_db)
    assert score.model_version == MODEL_ID
    assert score.closed_trades == 2
    assert score.grade in {Grade.B, Grade.C, Grade.D}  # two round trips is not an A
    assert load_score(Chain.SOL, SOL_WALLET, tmp_db) is not None


def test_build_evidence_reads_early_metrics_from_first_buyers(tmp_db):
    seed_wallet_rows(tmp_db, load_fixture("swaps_partial_rebuy")["swaps"])
    for token, rank, secs in (("AAA", 2, 30.0), ("BBB", 1, 12.0), ("CCC", 7, 900.0)):
        tmp_db.execute(
            "INSERT INTO first_buyers (chain, token, wallet, rank, ts_ms, seconds_after_open, source) "
            "VALUES (?,?,?,?,?,?,?)",
            ("sol", token, SOL_WALLET, rank, 0, secs, "test"),
        )
    for i in range(10):  # AAA drew ten buyers, so a top-3 entry there is validated
        tmp_db.execute(
            "INSERT INTO first_buyers (chain, token, wallet, rank, ts_ms, source) VALUES (?,?,?,?,?,?)",
            ("sol", "AAA", f"other{i}", 10 + i, 0, "test"),
        )
    em = build_evidence(SOL_WALLET, Chain.SOL, tmp_db).early_metrics
    assert em is not None
    assert em.best_entry_rank == 1
    assert em.insider_tokens == 2  # AAA rank2@30s and BBB rank1@12s
    assert em.sniper_tokens == 2  # CCC at 900s is outside the five-minute window
    assert em.validated_early_tokens == 1  # only AAA had ten or more buyers


def test_store_emits_wallet_graded(tmp_db):
    score = score_wallet(strong_evidence())
    store_score(score, tmp_db)
    rows = fetch_all(
        tmp_db, "SELECT * FROM events WHERE kind = ?", (EventKind.WALLET_GRADED.value,)
    )
    assert len(rows) == 1
    assert rows[0]["subject"] == SOL_WALLET
    payload = json.loads(rows[0]["payload"])
    assert payload == {"grade": "A", "score": score.score, "archetype": score.archetype.value}


# ============================================================ archetypes


def base_evidence(**kw) -> WalletEvidence:
    kw.setdefault("address", SOL_WALLET)
    kw.setdefault("chain", Chain.SOL)
    return WalletEvidence(**kw)


def test_bot_tag_beats_every_other_signal():
    ev = base_evidence(
        tags=[WalletTag.MEV_BOT, WalletTag.KOL, WalletTag.BUNDLER],
        created_token_count=5,
        early_metrics=EarlyMetrics(insider_tokens=9, sniper_tokens=9, best_entry_rank=1),
        reputation=Reputation(followers=100_000, verified=True),
    )
    assert naming.infer_archetype(ev) is Archetype.BOT


def test_archetype_precedence_walks_down_the_ladder():
    cases = [
        (Archetype.BUNDLER, {"tags": [WalletTag.BUNDLER], "created_token_count": 3}),
        (Archetype.DEV, {"created_token_count": 3, "reputation": Reputation(followers=99_000, verified=True)}),
        (Archetype.KOL, {"reputation": Reputation(followers=9_000, verified=True),
                         "early_metrics": EarlyMetrics(insider_tokens=9)}),
        (Archetype.INSIDER, {"early_metrics": EarlyMetrics(insider_tokens=3, sniper_tokens=9)}),
        (Archetype.SNIPER, {"early_metrics": EarlyMetrics(sniper_tokens=5, best_entry_rank=1)}),
        (Archetype.EARLY_BUYER, {"early_metrics": EarlyMetrics(best_entry_rank=4)}),
        (Archetype.SIDE_WALLET, {"lead_lag_of": EVM_WALLET.lower()}),
        (Archetype.COPYBOT, {"tags": [WalletTag.COPYBOT]}),
        (Archetype.TOP_TRADER, {"tags": [WalletTag.TOP_TRADER]}),
        (Archetype.TOP_HOLDER, {"tags": [WalletTag.TOP_HOLDER]}),
        (Archetype.SMART_MONEY, {"tags": [WalletTag.SMART_MONEY]}),
        (Archetype.TRADER, {}),
    ]
    for expected, kwargs in cases:
        assert naming.infer_archetype(base_evidence(**kwargs)) is expected, expected


def test_insider_needs_three_tokens_not_two():
    two = base_evidence(early_metrics=EarlyMetrics(insider_tokens=2))
    three = base_evidence(early_metrics=EarlyMetrics(insider_tokens=3))
    assert naming.infer_archetype(two) is Archetype.TRADER
    assert naming.infer_archetype(three) is Archetype.INSIDER


def test_sniper_can_be_inferred_from_hold_time_and_breadth():
    ev = base_evidence(pnl=pnl.WalletPnl(closed_episodes=50, distinct_tokens=41, median_hold_s=120, buys=90, sells=80))
    assert naming.infer_archetype(ev) is Archetype.SNIPER


def test_diamond_and_fomo_sit_below_the_label_tags():
    diamond = base_evidence(
        pnl=pnl.WalletPnl(closed_episodes=20, distinct_tokens=20, median_hold_s=200_000, win_rate=0.7, buys=40, sells=30)
    )
    fomo = base_evidence(fomo_flag=True)
    assert naming.infer_archetype(diamond) is Archetype.DIAMOND
    assert naming.infer_archetype(fomo) is Archetype.FOMO


def test_lead_lag_argument_overrides_the_evidence_field():
    ev = base_evidence()
    assert naming.infer_archetype(ev) is Archetype.TRADER
    assert naming.infer_archetype(ev, lead_lag_of=SOL_WALLET) is Archetype.SIDE_WALLET


# ============================================================ naming


def test_build_name_is_role_seed_grade():
    ev = strong_evidence()
    score = score_wallet(ev)
    assert score.archetype is Archetype.INSIDER
    assert naming.build_name(ev, score, "mcat") == "Insider MCAT A"


def test_build_name_without_a_seed_drops_the_middle_cleanly():
    ev = base_evidence(early_metrics=EarlyMetrics(best_entry_rank=2))
    score = WalletScore(
        address=SOL_WALLET, chain=Chain.SOL, score=55.0, grade=Grade.B,
        evidence_weight=80.0, archetype=Archetype.EARLY_BUYER,
    )
    assert naming.build_name(ev, score) == "Early Buyer B"
    assert "  " not in naming.build_name(ev, score)


def test_verified_kol_is_named_by_handle():
    ev = base_evidence(reputation=Reputation(twitter="@Kaiba_Whale", followers=90_000, verified=True))
    score = score_wallet(ev.model_copy(update={"seed_confluence": 2}))
    named = naming.build_name(ev, score.model_copy(update={"archetype": Archetype.KOL, "grade": Grade.A}), "mcat")
    assert named == "KOL @Kaiba_Whale A"


def test_naming_is_deterministic_across_rebuilt_evidence():
    first = naming.build_name(strong_evidence(), score_wallet(strong_evidence()), "MCAT")
    second = naming.build_name(strong_evidence(), score_wallet(strong_evidence()), "MCAT")
    assert first == second == "Insider MCAT A"


# ============================================================ GMGN export


def test_gmgn_row_shape_is_exactly_the_round_trip_format():
    rows = naming.gmgn_import_rows([(SOL_WALLET, "Insider MCAT A")])
    assert len(rows) == 1
    row = rows[0]
    assert tuple(row) == naming.GMGN_ROW_KEYS
    assert row == {
        "address": SOL_WALLET,
        "name": "Insider MCAT A",
        "emoji": "",
        "alertsOnToast": True,
        "alertsOnFeed": True,
        "alertsOnBubble": True,
        "sound": "default",
    }


def test_gmgn_rows_accept_scores_and_dedupe_by_address():
    score = score_wallet(strong_evidence())
    rows = naming.gmgn_import_rows(
        [score, (SOL_WALLET, "duplicate"), {"address": EVM_WALLET, "name": "Sniper B"}],
        group="trusted-copy",
    )
    assert [r["address"] for r in rows] == [SOL_WALLET, EVM_WALLET]
    assert rows[0]["name"] == "Insider A"
    assert rows[0]["group"] == "trusted-copy"


def test_gmgn_import_splits_at_two_thousand_rows(tmp_path):
    rows = naming.gmgn_import_rows((f"wallet{i:05d}", f"Trader {i}") for i in range(4001))
    written = naming.write_gmgn_import(rows, tmp_path)
    names = sorted(p.name for p in written)
    assert names == [
        "gmgn-import-part1of3.json",
        "gmgn-import-part1of3.txt",
        "gmgn-import-part2of3.json",
        "gmgn-import-part2of3.txt",
        "gmgn-import-part3of3.json",
        "gmgn-import-part3of3.txt",
    ]
    part1 = json.loads((tmp_path / "gmgn-import-part1of3.json").read_text(encoding="utf-8"))
    part3 = json.loads((tmp_path / "gmgn-import-part3of3.json").read_text(encoding="utf-8"))
    assert len(part1) == 2000 and len(part3) == 1


def test_gmgn_import_writes_the_address_name_text_variant(tmp_path):
    rows = naming.gmgn_import_rows([(SOL_WALLET, "Insider MCAT A"), (EVM_WALLET, "Sniper B")])
    naming.write_gmgn_import(rows, tmp_path)
    lines = (tmp_path / "gmgn-import.txt").read_text(encoding="utf-8").splitlines()
    assert lines == [f"{SOL_WALLET}:Insider MCAT A", f"{EVM_WALLET}:Sniper B"]
    assert (tmp_path / "gmgn-import.json").exists()
