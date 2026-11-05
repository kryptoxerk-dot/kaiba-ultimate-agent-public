"""Tests for wallet discovery.

The load-bearing tests here are the ones about *refusing*: that the sample-size arithmetic
reproduces the research's published table rather than a friendlier one, that the chance
expectation travels with every cohort, that the known-bad shapes are rejected before
anything is measured, and that nothing can write ``trusted_copy``. A test that only
asserted "discovery found some wallets" would pass on a leaderboard.
"""

from __future__ import annotations

import json
import math

import pytest

from kaiba.core.db import fetch_all, fetch_one
from kaiba.core.schemas import Chain
from kaiba.ingest import tape as tape_coverage  # `tape` is a fixture name in this module
from kaiba.intelligence import discover
from kaiba.intelligence.discover import (
    CandidateStatus,
    RejectReason,
    TokenCoverage,
    UnassessedReason,
)

SOL = Chain.SOL
DAY = 86_400_000
T0 = 1_780_000_000_000


# --------------------------------------------------------------------------------------
# fixtures: a tiny observed tape we control completely
# --------------------------------------------------------------------------------------


#: The two collection routes, spelled from the module that defines them. Which one a row
#: came from decides whether a token count is a count of the wallet's tokens or a count of
#: the tokens we pulled, so no fixture here may leave it implied.
WALK = tape_coverage.WALLET_SOURCE
TOKEN_ROUTE = tape_coverage.TRADE_SOURCE


def _swap(
    conn, *, wallet, token, side, ts, native, qty, slot=None, tx="", payer=None, source=WALK
):
    """One swap row. ``source`` defaults to the wallet walk: these fixtures build wallets.

    A fixture that left this at a token-route value would be describing a wallet we only
    glimpsed through one mint's tape, and every rule about *which* tokens a wallet traded
    would correctly refuse to fire on it. That is a real state — it is most of the live
    database — and :data:`TOKEN_ROUTE` is how a test asks for it deliberately.
    """
    conn.execute(
        "INSERT INTO swaps (chain, tx, slot, ts_ms, wallet, token, side, amount_token, "
        "amount_native, source, fee_payer) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            SOL.value,
            tx or f"tx{wallet[:4]}{token[:4]}{ts}{side}",
            slot,
            ts,
            wallet,
            token,
            side,
            str(qty),
            str(native),
            source,
            payer,
        ),
    )


#: Round trips in the fixtures hold for an hour, comfortably above
#: :data:`discover.MIN_MEDIAN_HOLD_S`, so the cadence reject does not fire on wallets that
#: are in these tests for another reason.
HOLD_MS = 3_600_000


def _round_trip(conn, wallet, token, *, ts, cost, proceeds, qty=1_000_000, slot=None, hold_ms=HOLD_MS):
    """One closed episode: buy then sell the whole position."""
    _swap(conn, wallet=wallet, token=token, side="buy", ts=ts, native=cost, qty=qty, slot=slot)
    _swap(
        conn,
        wallet=wallet,
        token=token,
        side="sell",
        ts=ts + hold_ms,
        native=proceeds,
        qty=qty,
        slot=None if slot is None else slot + 1,
    )


def _wallet_with_record(conn, wallet, *, wins, losses, start=T0):
    """A wallet with a known win/loss record across distinct tokens."""
    ts = start
    for i in range(wins):
        _round_trip(conn, wallet, f"{wallet[:3]}win{i}", ts=ts, cost=1_000_000, proceeds=3_000_000)
        ts += DAY
    for i in range(losses):
        _round_trip(conn, wallet, f"{wallet[:3]}loss{i}", ts=ts, cost=1_000_000, proceeds=400_000)
        ts += DAY


@pytest.fixture
def tape(tmp_db):
    """Ten wallets with real round trips, plus the known-bad shapes."""
    conn = tmp_db
    for n in range(10):
        _wallet_with_record(conn, f"W{n:02d}trader", wins=3, losses=7, start=T0 + n * 3600_000)
    conn.commit()
    return conn


# --------------------------------------------------------------------------------------
# 1. the multiple-comparisons arithmetic
# --------------------------------------------------------------------------------------


#: Research 13 §A2's published sample-size table: (p_alt, p_null) -> by screen size.
PUBLISHED_SAMPLE_TABLE: dict[tuple[float, float], dict[int, int]] = {
    (0.40, 0.30): {1: 136, 100: 369, 10_000: 594, 100_000: 704},
    (0.35, 0.30): {1: 534, 100: 1_458, 10_000: 2_353, 100_000: 2_794},
    (0.33, 0.30): {1: 1_468, 100: 4_027, 10_000: 6_507, 100_000: 7_729},
}


def test_min_closed_trades_reproduces_the_published_table():
    """All twelve cells of research 13 §A2's table, to the trade.

    If this drifts, the gate is no longer the one the research described and every cohort
    downstream is being judged against an invented bar.
    """
    for (p_alt, p_null), row in PUBLISHED_SAMPLE_TABLE.items():
        for screened, published in row.items():
            exact = discover.min_closed_trades_exact(p_alt, p_null, screened=screened)
            assert exact is not None
            assert round(exact) == published, (p_alt, p_null, screened, exact)


def test_the_gate_we_apply_is_never_below_the_published_figure():
    """We round up where the research rounds to nearest; never the other way."""
    for (p_alt, p_null), row in PUBLISHED_SAMPLE_TABLE.items():
        for screened, published in row.items():
            gate = discover.min_closed_trades(p_alt, p_null, screened=screened)
            assert gate is not None
            assert published <= gate <= published + 1


def test_min_closed_trades_rises_with_the_screen():
    """A bigger screen can only ever demand a bigger sample."""
    sizes = [1, 10, 100, 1_000, 10_000, 100_000]
    needed = [discover.min_closed_trades(0.40, 0.30, screened=n) for n in sizes]
    assert all(a is not None and b is not None and a <= b for a, b in zip(needed, needed[1:], strict=False))


def test_no_sample_size_for_an_absent_edge():
    """No edge means no sample size. A number here would read as progress."""
    assert discover.min_closed_trades(0.30, 0.30, screened=100) is None
    assert discover.min_closed_trades(0.20, 0.30, screened=100) is None


def test_binomial_tail_matches_the_research_noise_figures():
    """The per-wallet probabilities behind "98 perfect 10-for-10 records in 100,000"."""
    p10 = discover.binomial_tail_p(10, 10, 0.50)
    assert p10 == pytest.approx(9.7656e-4, rel=1e-3)
    assert discover.expected_peers(100_000, p10) == pytest.approx(97.7, rel=1e-2)
    assert discover.binomial_tail_p(15, 20, 0.50) == pytest.approx(2.069e-2, rel=1e-3)
    assert discover.binomial_tail_p(35, 50, 0.50) == pytest.approx(3.301e-3, rel=1e-3)
    assert discover.binomial_tail_p(25, 50, 0.35) == pytest.approx(2.07e-2, rel=2e-2)
    assert discover.binomial_tail_p(45, 100, 0.35) == pytest.approx(2.46e-2, rel=2e-2)


def test_chance_table_is_recomputed_for_our_screen():
    rows = discover.chance_table(1_000, 0.30)
    perfect = next(r for r in rows if r.label == "10 of 10 wins")
    assert perfect.expected_hits == pytest.approx(1_000 * 0.30**10)
    assert all(r.expected_hits == pytest.approx(1_000 * r.p_per_wallet) for r in rows)


def test_bonferroni_alpha_denominator_is_the_screen_not_the_shortlist():
    assert discover.bonferroni_alpha(0.05, 5_000) == pytest.approx(1e-5)
    assert discover.bonferroni_alpha(0.05, 0) == pytest.approx(0.05)


# --------------------------------------------------------------------------------------
# 2. the structural rejects
# --------------------------------------------------------------------------------------


def _features(**kw) -> discover.CandidateFeatures:
    base = {"swaps": 40, "buys": 20, "sells": 20, "distinct_tokens": 8}
    base.update(kw)
    return discover.CandidateFeatures(**base)


def _walked(tokens: int = 8, complete: int = 0) -> TokenCoverage:
    """Coverage for a wallet whose own history we walked: its token count is its own."""
    return TokenCoverage(
        tokens_observed=tokens, tokens_complete_tape=complete, walk_rows=tokens, sources=(WALK,)
    )


def _glimpsed(tokens: int = 1, complete: int = 0) -> TokenCoverage:
    """Coverage for a wallet seen only through the trade tapes of tokens we chose."""
    return TokenCoverage(
        tokens_observed=tokens,
        tokens_complete_tape=complete,
        walk_rows=0,
        sources=(TOKEN_ROUTE,),
    )


def test_sell_only_address_is_rejected_before_anything_is_scored():
    """arXiv:2602.14860's rank-1 wallet: 1,793 trades, zero buys, 9,373 SOL realised."""
    f = _features(swaps=1_793, buys=0, sells=1_793, buy_share=0.0)
    reasons = {r.reason for r in discover.rejections_for("A" * 32, SOL, f)}
    assert RejectReason.SELL_ONLY in reasons


def test_buy_starved_address_is_rejected():
    f = _features(swaps=100, buys=5, sells=95, buy_share=0.05)
    reasons = {r.reason for r in discover.rejections_for("B" * 32, SOL, f)}
    assert RejectReason.BUY_STARVED in reasons


def test_bot_failure_rate_is_rejected_and_the_threshold_is_below_jupiters():
    """The audit found leaderboard wallets at 36% and 49%; Jupiter's own line is 50%."""
    assert discover.TX_FAILURE_REJECT_RATE < discover.JUPITER_SYBIL_FAILURE_RATE
    f = _features(tx_observed=100, tx_failures=36, tx_failure_rate=0.36)
    reasons = {r.reason for r in discover.rejections_for("C" * 32, SOL, f)}
    assert RejectReason.HIGH_TX_FAILURE in reasons


def test_unknown_failure_rate_is_not_a_pass():
    """No meta rows means unknown, and unknown may not be rejected *or* cleared."""
    f = _features(tx_observed=3, tx_failures=0, tx_failure_rate=None)
    reasons = {r.reason for r in discover.rejections_for("D" * 32, SOL, f)}
    assert RejectReason.HIGH_TX_FAILURE not in reasons


def test_a_sub_minute_scalper_is_rejected_as_uncopyable():
    """The live run's own best wallet: 144 swaps in nine minutes, 29 wins from 38.

    It is buy/sell symmetric, lands every transaction and spreads its gains over ten
    tokens, so none of the other rejects touch it. The copier's penalty on a convex curve
    makes a round trip this short uncopyable regardless of skill.
    """
    f = _features(closed_trades=38, wins=29, median_hold_s=4, distinct_tokens=10)
    reasons = {r.reason for r in discover.rejections_for("I" * 32, SOL, f)}
    assert RejectReason.UNCOPYABLE_CADENCE in reasons


def test_cadence_needs_a_sample_before_it_rejects():
    """Two fast round trips is not a cadence, and an unmeasured hold is not a fast one."""
    thin = _features(closed_trades=2, wins=2, median_hold_s=4, distinct_tokens=4)
    assert RejectReason.UNCOPYABLE_CADENCE not in {
        r.reason for r in discover.rejections_for("J" * 32, SOL, thin)
    }
    unknown = _features(closed_trades=40, wins=20, median_hold_s=None, distinct_tokens=9)
    assert RejectReason.UNCOPYABLE_CADENCE not in {
        r.reason for r in discover.rejections_for("K" * 32, SOL, unknown)
    }


def test_scalper_is_rejected_end_to_end(tmp_db):
    conn = tmp_db
    _wallet_with_record(conn, "W00trader", wins=3, losses=7)
    for i in range(8):
        _round_trip(
            conn, "scalpBot", f"scalptok{i}", ts=T0 + i * 10_000, cost=1_000_000,
            proceeds=1_400_000, hold_ms=4_000,
        )
    conn.commit()
    report = discover.discover(conn, chain=SOL, as_of_ms=T0 + 400 * DAY)
    assert "scalpBot" not in {c.address for c in report.candidates}
    assert RejectReason.UNCOPYABLE_CADENCE in {
        r.reason for r in report.rejected if r.address == "scalpBot"
    }


def test_single_token_wallet_is_rejected():
    f = _features(distinct_tokens=1)
    reasons = {
        r.reason
        for r in discover.rejections_for("E" * 32, SOL, f, coverage=_walked(tokens=1))
    }
    assert RejectReason.SINGLE_TOKEN in reasons


def test_edge_concentrated_in_one_token_is_rejected():
    f = _features(distinct_tokens=9, best_token_pnl_share=0.93)
    reasons = {r.reason for r in discover.rejections_for("F" * 32, SOL, f, coverage=_walked(9))}
    assert RejectReason.SINGLE_TOKEN_PNL in reasons
    spread = _features(distinct_tokens=9, best_token_pnl_share=0.40)
    assert not discover.rejections_for("F" * 32, SOL, spread, coverage=_walked(9))


def test_creator_self_dealing_is_rejected():
    f = _features(distinct_tokens=4)
    reasons = {
        r.reason
        for r in discover.rejections_for(
            "G" * 32, SOL, f, coverage=_walked(4), created_tokens=3
        )
    }
    assert RejectReason.CREATOR_SELF_DEALING in reasons


def test_every_reason_is_reported_not_just_the_first():
    f = _features(swaps=50, buys=0, sells=50, buy_share=0.0, distinct_tokens=1)
    reasons = {
        r.reason
        for r in discover.rejections_for("H" * 32, SOL, f, coverage=_walked(tokens=1))
    }
    assert {RejectReason.SELL_ONLY, RejectReason.SINGLE_TOKEN} <= reasons


# --------------------------------------------------------------------------------------
# 2b. the three states: traded one token, traded many, and we did not look
# --------------------------------------------------------------------------------------


def test_one_observed_token_is_not_a_finding_when_we_only_pulled_one_token():
    """The bug this gate exists for: 587 of 588 live SINGLE_TOKEN rejects were this.

    A wallet seen only through the trade tape of tokens we chose has a token count that is
    a floor on our sampling. Rejecting it as "indistinguishable from having been in the
    bundle" asserts something nobody established.
    """
    f = _features(distinct_tokens=1)
    verdict = discover.assess_shape("E" * 32, SOL, f, coverage=_glimpsed(tokens=1))
    assert RejectReason.SINGLE_TOKEN not in {r.reason for r in verdict.rejections}
    assert [u.reason for u in verdict.unassessed] == [
        UnassessedReason.TOKEN_UNIVERSE_UNESTABLISHED
    ]
    assert verdict.unassessed[0].would_have_been is RejectReason.SINGLE_TOKEN
    assert "never walked" in verdict.unassessed[0].detail


def test_a_complete_tape_on_the_one_token_we_pulled_still_proves_nothing_about_the_wallet():
    """Per-token completeness is not per-wallet completeness, and the gate must not conflate
    them: holding every trade of one mint says nothing about the mints we never pulled."""
    f = _features(distinct_tokens=1)
    verdict = discover.assess_shape(
        "E" * 32, SOL, f, coverage=_glimpsed(tokens=1, complete=1)
    )
    assert not verdict.rejections
    assert verdict.unassessed[0].would_have_been is RejectReason.SINGLE_TOKEN


def test_pnl_concentration_is_withheld_when_the_denominator_is_our_sampling():
    f = _features(distinct_tokens=9, best_token_pnl_share=0.93)
    verdict = discover.assess_shape("F" * 32, SOL, f, coverage=_glimpsed(tokens=9))
    assert not verdict.rejections
    assert verdict.unassessed[0].would_have_been is RejectReason.SINGLE_TOKEN_PNL


def test_creator_self_dealing_is_withheld_when_the_denominator_is_our_sampling():
    f = _features(distinct_tokens=4)
    verdict = discover.assess_shape(
        "G" * 32, SOL, f, coverage=_glimpsed(tokens=4), created_tokens=3
    )
    assert not verdict.rejections
    assert verdict.unassessed[0].would_have_been is RejectReason.CREATOR_SELF_DEALING


def test_rules_that_do_not_depend_on_the_token_set_still_fire_without_coverage():
    """The gate must narrow exactly three rules. Sell-only, buy-starved, failure rate and
    cadence are facts about the rows we hold, and hold whatever else we missed."""
    f = _features(
        swaps=100, buys=5, sells=95, buy_share=0.05, tx_observed=100, tx_failures=36,
        tx_failure_rate=0.36, closed_trades=38, median_hold_s=4,
    )
    verdict = discover.assess_shape("Z" * 32, SOL, f, coverage=_glimpsed(tokens=9))
    assert {r.reason for r in verdict.rejections} == {
        RejectReason.BUY_STARVED,
        RejectReason.HIGH_TX_FAILURE,
        RejectReason.UNCOPYABLE_CADENCE,
    }


def test_a_caller_that_states_no_coverage_gets_the_honest_answer():
    """The default is unestablished. A caller that forgets to say how the tokens were
    observed must not be handed a rejection it cannot support."""
    f = _features(distinct_tokens=1)
    assert RejectReason.SINGLE_TOKEN not in {
        r.reason for r in discover.rejections_for("E" * 32, SOL, f)
    }
    assert discover.assess_shape("E" * 32, SOL, f).unassessed


# --------------------------------------------------------------------------------------
# 3. entities, not addresses
# --------------------------------------------------------------------------------------


def test_same_slot_co_buyers_across_tokens_collapse_to_one_operator(tmp_db):
    """Two addresses buying the same tokens in the same slots are one hand, not two votes."""
    conn = tmp_db
    for i, token in enumerate(("tokAAA", "tokBBB")):
        slot = 1_000 + i
        for wallet in ("bundleOne", "bundleTwo"):
            _swap(
                conn,
                wallet=wallet,
                token=token,
                side="buy",
                ts=T0 + i * 1000,
                native=1_000_000,
                qty=1_000,
                slot=slot,
                tx=f"tx{wallet}{token}",
            )
    conn.commit()
    flow = discover._load_flow(conn, SOL, T0 + DAY)
    keys = discover.entity_keys(conn, SOL, ["bundleOne", "bundleTwo"], flow)
    assert keys["bundleOne"] == keys["bundleTwo"]
    assert len(set(keys.values())) == 1


def test_one_shared_slot_is_not_enough_to_merge(tmp_db):
    """A single crowded block is not evidence of one operator."""
    conn = tmp_db
    for wallet in ("loneOne", "loneTwo"):
        _swap(
            conn,
            wallet=wallet,
            token="tokAAA",
            side="buy",
            ts=T0,
            native=1_000_000,
            qty=1_000,
            slot=1_000,
            tx=f"tx{wallet}",
        )
    conn.commit()
    flow = discover._load_flow(conn, SOL, T0 + DAY)
    keys = discover.entity_keys(conn, SOL, ["loneOne", "loneTwo"], flow)
    assert keys["loneOne"] != keys["loneTwo"]


def test_shared_third_party_fee_payer_collapses_addresses(tmp_db):
    conn = tmp_db
    for wallet in ("payeeOne", "payeeTwo"):
        _swap(
            conn,
            wallet=wallet,
            token="tokAAA",
            side="buy",
            ts=T0,
            native=1_000_000,
            qty=1_000,
            payer="thePayer",
            tx=f"tx{wallet}",
        )
    conn.commit()
    flow = discover._load_flow(conn, SOL, T0 + DAY)
    keys = discover.entity_keys(conn, SOL, ["payeeOne", "payeeTwo"], flow)
    assert keys["payeeOne"] == keys["payeeTwo"]


def test_duplicate_addresses_of_one_entity_are_rejected_from_the_cohort(tmp_db):
    conn = tmp_db
    for wallet in ("dupeOne", "dupeTwo"):
        for i in range(4):
            _round_trip(
                conn, wallet, f"dupetok{i}", ts=T0 + i * DAY, cost=1_000_000, proceeds=2_000_000, slot=500 + i
            )
    conn.commit()
    report = discover.discover(conn, chain=SOL, as_of_ms=T0 + 100 * DAY)
    same_entity = [r for r in report.rejected if r.reason is RejectReason.SAME_ENTITY]
    assert len(same_entity) == 1
    assert report.chance.screened_entities < len(report.screened)


# --------------------------------------------------------------------------------------
# 4. the report itself
# --------------------------------------------------------------------------------------


def test_report_carries_the_screen_size_and_the_chance_expectation(tape):
    report = discover.discover(tape, chain=SOL, as_of_ms=T0 + 400 * DAY)
    assert report.chance.screened_addresses == len(report.screened) == 10
    assert report.chance.expected_false_positives == pytest.approx(0.5)
    assert report.chance.expected_max_z > 0
    assert "screened" in report.chance.sentence.lower()
    assert str(report.chance.screened_addresses) in report.chance.sentence


def test_no_null_means_no_wallet_is_gated(tmp_db):
    """Under the pooled-trade floor there is no base rate, so nothing can be tested."""
    conn = tmp_db
    for n in range(4):
        _wallet_with_record(conn, f"T{n:02d}thin", wins=2, losses=2)
    conn.commit()
    report = discover.discover(conn, chain=SOL, as_of_ms=T0 + 400 * DAY)
    assert report.chance.null_win_rate is None
    assert report.chance.null_basis == "unavailable"
    assert report.chance.min_closed_trades is None
    assert report.gate_cleared == []
    assert all(c.status is not CandidateStatus.SAMPLE_GATE_CLEARED for c in report.candidates)
    assert any("base rate" in b for c in report.candidates for b in c.blockers)


def test_thin_wallets_are_unvalidated_and_say_why(tape):
    report = discover.discover(tape, chain=SOL, as_of_ms=T0 + 400 * DAY)
    assert report.candidates
    below = [c for c in report.candidates if c.status is CandidateStatus.UNVALIDATED]
    assert below, "ten-trade wallets must not clear a gate built for hundreds"
    assert all(any("closed trades against the" in b for b in c.blockers) for c in below)


def test_a_candidate_is_never_validated(tape):
    report = discover.discover(tape, chain=SOL, as_of_ms=T0 + 400 * DAY)
    assert all(c.validated is False for c in report.candidates)


def test_missing_features_are_none_with_an_unavailable_basis(tape):
    report = discover.discover(tape, chain=SOL, as_of_ms=T0 + 400 * DAY)
    cand = report.candidates[0]
    assert cand.features.tx_failure_rate is None
    assert cand.bases["tx_failure_rate"] == "unavailable"
    assert "tx_failure_rate" in cand.unknowns
    assert any("unmeasured" in b for b in cand.blockers)


def test_unassessably_thin_wallets_are_counted_not_dropped_silently(tmp_db):
    conn = tmp_db
    _wallet_with_record(conn, "W00trader", wins=3, losses=7)
    _swap(conn, wallet="glimpsed", token="tokAAA", side="buy", ts=T0, native=1, qty=1)
    _swap(conn, wallet="glimpsed", token="tokBBB", side="buy", ts=T0 + 1, native=1, qty=1)
    conn.commit()
    report = discover.discover(conn, chain=SOL, as_of_ms=T0 + 400 * DAY)
    assert "glimpsed" in report.screened
    assert report.unassessable == 1
    assert "glimpsed" not in {c.address for c in report.candidates}


def _token(conn, address, *, creator=None, created_ms=T0):
    conn.execute(
        "INSERT INTO tokens (chain, address, creator, created_ms, first_seen_ms) "
        "VALUES (?,?,?,?,?)",
        (SOL.value, address, creator, created_ms, created_ms),
    )


def _prove_tape(conn, token, *, created_ms=T0, complete=True):
    """Record coverage for a mint through ``kaiba.ingest.tape``'s own writer.

    Written through the real writer rather than an INSERT so the record has to satisfy
    migration 025's CHECK constraints; a fixture that could fake completeness would make
    every test below pass for the wrong reason.
    """
    record = tape_coverage.TapeRecord(
        chain=SOL,
        token=token,
        coverage=tape_coverage.COMPLETE if complete else tape_coverage.PARTIAL,
        route=tape_coverage.ROUTE_TRADES,
        reason="fixture",
        proof=tape_coverage.REASON_END_OF_HISTORY if complete else None,
        covered_from_ms=created_ms - 1 if complete else created_ms + 1,
        covered_to_ms=created_ms + DAY,
        created_ms=created_ms,
    )
    assert tape_coverage.store(record, conn) is True
    conn.commit()


def _one_token_wallet(conn, wallet, token, *, source):
    """Four swaps, one token: thick enough to assess, and only one mint to show for it."""
    _round_trip(conn, wallet, token, ts=T0, cost=1_000_000, proceeds=3_000_000)
    _round_trip(conn, wallet, token, ts=T0 + DAY, cost=1_000_000, proceeds=2_000_000)
    conn.execute(
        "UPDATE swaps SET source=? WHERE chain=? AND wallet=?", (source, SOL.value, wallet)
    )
    conn.commit()


def test_a_wallet_seen_through_one_tokens_tape_is_unassessed_not_rejected(tmp_db):
    """End to end: the live run's largest reject reason, on the evidence that produced it.

    588 of 681 wallets were rejected SINGLE_TOKEN and 587 of them had never been walked —
    they were seen through the trade tapes of tokens we chose to pull.
    """
    conn = tmp_db
    _wallet_with_record(conn, "W00trader", wins=3, losses=7)
    _one_token_wallet(conn, "glimpsedOnce", "onlytok", source=TOKEN_ROUTE)
    report = discover.discover(conn, chain=SOL, as_of_ms=T0 + 400 * DAY)

    assert "glimpsedOnce" in report.screened
    assert "glimpsedOnce" not in {r.address for r in report.rejected}
    assert "glimpsedOnce" not in {c.address for c in report.candidates}
    gap = next(u for u in report.unassessed if u.address == "glimpsedOnce")
    assert gap.reason is UnassessedReason.TOKEN_UNIVERSE_UNESTABLISHED
    assert gap.would_have_been is RejectReason.SINGLE_TOKEN
    assert report.reject_counts.get("single_token") is None
    assert report.withheld_rejects["single_token"] == 1
    assert any("floor" in n for n in report.notes)


def test_the_same_wallet_is_rejected_once_its_history_has_been_walked(tmp_db):
    """The paired half: the gate must narrow the claim, not delete the rule."""
    conn = tmp_db
    _wallet_with_record(conn, "W00trader", wins=3, losses=7)
    _one_token_wallet(conn, "walkedOnce", "onlytok", source=WALK)
    report = discover.discover(conn, chain=SOL, as_of_ms=T0 + 400 * DAY)

    assert RejectReason.SINGLE_TOKEN in {
        r.reason for r in report.rejected if r.address == "walkedOnce"
    }
    assert "walkedOnce" not in {u.address for u in report.unassessed}
    assert report.withheld_rejects == {}


def test_the_token_count_carries_its_basis(tmp_db):
    conn = tmp_db
    _one_token_wallet(conn, "glimpsedOnce", "onlytok", source=TOKEN_ROUTE)
    _wallet_with_record(conn, "W00trader", wins=3, losses=7)
    conn.commit()
    flow = discover._load_flow(conn, SOL, T0 + 400 * DAY)

    _f, bases, unknowns, cov = discover._features("glimpsedOnce", flow)
    assert bases["distinct_tokens"] == "unavailable"
    assert "distinct_tokens" in unknowns
    assert cov.universe_established is False

    _f2, bases2, unknowns2, cov2 = discover._features("W00trader", flow)
    assert bases2["distinct_tokens"] == "verified_onchain"
    assert "distinct_tokens" not in unknowns2
    assert cov2.universe_established is True


def test_tape_completeness_travels_with_the_count_through_the_shared_gate(tmp_db):
    """``tokens_complete_tape`` is whatever ``kaiba.ingest.tape`` proved, and nothing else.

    Proving the tape does not make the wallet assessable — per-token completeness cannot
    answer a per-wallet question — but it is reported, because it says how much of what we
    did pull is whole.
    """
    conn = tmp_db
    _token(conn, "proven")
    _token(conn, "unproven")
    _one_token_wallet(conn, "twoTokens", "proven", source=TOKEN_ROUTE)
    _round_trip(conn, "twoTokens", "unproven", ts=T0 + 2 * DAY, cost=1, proceeds=2)
    conn.execute("UPDATE swaps SET source=? WHERE chain=?", (TOKEN_ROUTE, SOL.value))
    conn.commit()

    flow = discover._load_flow(conn, SOL, T0 + 400 * DAY)
    assert discover.coverage_for("twoTokens", flow).tokens_complete_tape == 0

    _prove_tape(conn, "unproven", complete=False)
    flow = discover._load_flow(conn, SOL, T0 + 400 * DAY)
    assert discover.coverage_for("twoTokens", flow).tokens_complete_tape == 0, (
        "a partial record is not a complete one"
    )

    _prove_tape(conn, "proven", complete=True)
    flow = discover._load_flow(conn, SOL, T0 + 400 * DAY)
    cov = discover.coverage_for("twoTokens", flow)
    assert cov.tokens_complete_tape == 1
    assert cov.tokens_observed == 2
    assert cov.universe_established is False
    features, _b, _u, _c = discover._features("twoTokens", flow)
    assert features.tokens_complete_tape == 1
    assert set(flow.complete_tape) == set(tape_coverage.complete_tokens(SOL, conn))


def test_unassessed_addresses_are_never_written_as_rejections(tmp_db):
    """A row in ``discovery_rejects`` is a finding. These are the absence of one."""
    conn = tmp_db
    _wallet_with_record(conn, "W00trader", wins=3, losses=7)
    _one_token_wallet(conn, "glimpsedOnce", "onlytok", source=TOKEN_ROUTE)
    report = discover.discover(conn, chain=SOL, as_of_ms=T0 + 400 * DAY)
    discover.store_report(report, conn)

    rows = fetch_all(
        conn, "SELECT address, reason FROM discovery_rejects WHERE run_id=?", (report.run_id,)
    )
    assert "glimpsedOnce" not in {str(r["address"]) for r in rows}
    run = fetch_one(
        conn, "SELECT payload_json FROM discovery_runs WHERE run_id=?", (report.run_id,)
    )
    payload = json.loads(str(run["payload_json"]))
    assert payload["withheld_rejects"]["single_token"] == 1
    assert payload["unassessed_counts"]["token_universe_unestablished"] == 1
    assert any(u["address"] == "glimpsedOnce" for u in payload["unassessed"])


def test_thin_wallets_are_unassessed_with_their_own_reason(tmp_db):
    conn = tmp_db
    _wallet_with_record(conn, "W00trader", wins=3, losses=7)
    _swap(conn, wallet="glimpsed", token="tokAAA", side="buy", ts=T0, native=1, qty=1)
    _swap(conn, wallet="glimpsed", token="tokBBB", side="buy", ts=T0 + 1, native=1, qty=1)
    conn.commit()
    report = discover.discover(conn, chain=SOL, as_of_ms=T0 + 400 * DAY)
    thin = next(u for u in report.unassessed if u.address == "glimpsed")
    assert thin.reason is UnassessedReason.TOO_THIN
    assert thin.would_have_been is None
    assert report.unassessed_counts["too_thin"] == report.unassessable == 1


def test_as_of_cutoff_hides_the_future(tape):
    early = discover.discover(tape, chain=SOL, as_of_ms=T0 + 2 * DAY)
    late = discover.discover(tape, chain=SOL, as_of_ms=T0 + 400 * DAY)
    assert sum(c.features.closed_trades for c in early.candidates) < sum(
        c.features.closed_trades for c in late.candidates
    )


def test_token_peak_outcome_uses_the_wallets_own_entry_print(tmp_db):
    """"Bought a token that later did well" is measured against what this wallet paid."""
    conn = tmp_db
    _swap(conn, wallet="earlyBird", token="runner", side="buy", ts=T0, native=100, qty=1_000_000)
    for i in range(3):
        _swap(
            conn,
            wallet="laterGuy",
            token="runner",
            side="buy",
            ts=T0 + (i + 1) * 60_000,
            native=1_000,
            qty=1_000_000,
            tx=f"later{i}",
        )
    _swap(conn, wallet="earlyBird", token="other", side="buy", ts=T0, native=100, qty=1_000_000)
    conn.commit()
    flow = discover._load_flow(conn, SOL, T0 + DAY)
    features, _bases, _unknowns, _cov = discover._features("earlyBird", flow)
    assert features.peak_assessable_tokens == 1
    assert features.peak_hit_tokens == 1
    assert features.peak_hit_rate == pytest.approx(1.0)


def test_a_peak_resting_on_one_print_is_not_assessable(tmp_db):
    conn = tmp_db
    _swap(conn, wallet="earlyBird", token="thin", side="buy", ts=T0, native=100, qty=1_000_000)
    _swap(conn, wallet="other", token="thin", side="buy", ts=T0 + 1000, native=9_000, qty=1_000_000)
    conn.commit()
    flow = discover._load_flow(conn, SOL, T0 + DAY)
    features, _bases, _unknowns, _cov = discover._features("earlyBird", flow)
    assert features.peak_assessable_tokens == 0
    assert features.peak_hit_rate is None


# --------------------------------------------------------------------------------------
# 5. storage and cohort safety
# --------------------------------------------------------------------------------------


def test_store_report_persists_the_screen_size_alongside_the_cohort(tape):
    report = discover.discover(tape, chain=SOL, as_of_ms=T0 + 400 * DAY)
    run_id = discover.store_report(report, tape)
    row = fetch_one(tape, "SELECT * FROM discovery_runs WHERE run_id = ?", (run_id,))
    assert row is not None
    assert row["screened_addresses"] == len(report.screened)
    assert row["screened_entities"] == report.chance.screened_entities
    assert row["expected_false_positives"] == pytest.approx(len(report.screened) * 0.05)
    assert json.loads(row["payload_json"])["chance"]["screened_addresses"] == len(report.screened)
    stored = fetch_all(tape, "SELECT * FROM discovery_candidates WHERE run_id = ?", (run_id,))
    assert len(stored) == len(report.candidates)
    assert all(s["status"] != "validated" for s in stored)


def test_rejects_are_stored_with_their_reason(tmp_db):
    conn = tmp_db
    _wallet_with_record(conn, "W00trader", wins=3, losses=7)
    for i in range(12):
        _swap(
            conn,
            wallet="settleOnly",
            token=f"settletok{i}",
            side="sell",
            ts=T0 + i * 1000,
            native=5_000_000,
            qty=1_000,
        )
    conn.commit()
    report = discover.discover(conn, chain=SOL, as_of_ms=T0 + 400 * DAY)
    discover.store_report(report, conn)
    rows = fetch_all(conn, "SELECT * FROM discovery_rejects WHERE address = ?", ("settleOnly",))
    assert {r["reason"] for r in rows} >= {RejectReason.SELL_ONLY.value}
    assert all(r["detail"] for r in rows)


def test_discovery_never_writes_a_trusted_cohort(tape):
    report = discover.discover(tape, chain=SOL, as_of_ms=T0 + 400 * DAY)
    written, _skipped = discover.propose_research_cohort(report, tape)
    assert written == len(report.candidates)
    cohorts = {
        r["cohort"] for r in fetch_all(tape, "SELECT DISTINCT cohort FROM wallets", ())
    }
    assert cohorts == {discover.PROPOSED_COHORT}
    assert "trusted_copy" not in cohorts


def test_an_existing_trusted_or_blacklisted_row_is_left_alone(tape):
    report = discover.discover(tape, chain=SOL, as_of_ms=T0 + 400 * DAY)
    victim = report.candidates[0].address
    tape.execute(
        "INSERT INTO wallets (chain, address, source, first_seen_ms, last_seen_ms, cohort) "
        "VALUES (?,?,?,?,?,?)",
        (SOL.value, victim, "operator", T0, T0, "trusted_copy"),
    )
    tape.commit()
    written, skipped = discover.propose_research_cohort(report, tape)
    assert victim in skipped
    assert written == len(report.candidates) - 1
    row = fetch_one(tape, "SELECT cohort FROM wallets WHERE address = ?", (victim,))
    assert row is not None and row["cohort"] == "trusted_copy"


# --------------------------------------------------------------------------------------
# 6. forward tracking, delegated to validation.py
# --------------------------------------------------------------------------------------


def test_freeze_writes_into_the_shared_cohort_tables(tape):
    report = discover.discover(tape, chain=SOL, as_of_ms=T0 + 400 * DAY)
    cohort_id = discover.freeze_cohort(report, tape)
    freeze = fetch_one(tape, "SELECT * FROM wallet_cohort_freezes WHERE cohort_id = ?", (cohort_id,))
    assert freeze is not None
    assert freeze["frozen_ms"] == report.as_of_ms
    unmatched = json.loads(freeze["unmatched_json"])
    assert unmatched, "a matched study must declare what it could not match on"
    arms = {
        r["arm"] for r in fetch_all(tape, "SELECT arm FROM wallet_cohorts WHERE cohort_id = ?", (cohort_id,))
    }
    assert arms <= {"graded", "control"}


def test_forward_report_delegates_and_refuses_to_conclude_on_a_tiny_cohort(tape):
    from kaiba.learning.validation import Verdict

    report = discover.discover(tape, chain=SOL, as_of_ms=T0 + 400 * DAY)
    cohort_id = discover.freeze_cohort(report, tape)
    forward = discover.forward_report(cohort_id, tape)
    assert forward.cohort_id == cohort_id
    assert forward.verdict in (Verdict.UNDERPOWERED, Verdict.FAIL)
    assert forward.verdict is not Verdict.PASS


def test_freeze_of_an_empty_cohort_still_records_the_freeze(tmp_db):
    report = discover.discover(tmp_db, chain=SOL, as_of_ms=T0)
    cohort_id = discover.freeze_cohort(report, tmp_db)
    freeze = fetch_one(tmp_db, "SELECT * FROM wallet_cohort_freezes WHERE cohort_id = ?", (cohort_id,))
    assert freeze is not None
    assert freeze["graded_n"] == 0


# --------------------------------------------------------------------------------------
# 7. the population null
# --------------------------------------------------------------------------------------


def test_population_null_is_pooled_from_our_own_tape(tape):
    report = discover.discover(tape, chain=SOL, as_of_ms=T0 + 400 * DAY)
    assert report.chance.null_pooled_trades == 100
    assert report.chance.null_win_rate == pytest.approx(0.30)
    assert report.chance.null_basis == "derived"
    assert report.chance.min_interesting_rate == pytest.approx(0.40)
    assert report.chance.min_closed_trades == discover.min_closed_trades(
        0.40, 0.30, screened=len(report.screened)
    )


def test_population_null_refuses_below_the_pooled_floor():
    thin = [discover.CandidateFeatures(closed_trades=5, wins=3) for _ in range(4)]
    rate, pooled, basis = discover.population_null(thin)
    assert rate is None
    assert pooled == 20
    assert basis == "unavailable"


def test_the_gate_can_actually_be_cleared_by_a_large_enough_sample(tmp_db):
    """A gate that nothing can ever clear is not a gate, it is a refusal with extra steps.

    One wallet with 700 closed trades at 60% against a pooled base rate near 53%. It
    clears, and the report still reports how many of the screened wallets would match it
    by chance — which is the number that keeps the result in proportion.
    """
    conn = tmp_db
    for n in range(20):
        _wallet_with_record(conn, f"B{n:02d}base", wins=3, losses=7, start=T0 + n * 3600_000)
    _wallet_with_record(conn, "BIGsample", wins=420, losses=280, start=T0 + 500 * DAY)
    conn.commit()
    report = discover.discover(conn, chain=SOL, as_of_ms=T0 + 5_000 * DAY)
    cleared = report.gate_cleared
    assert [c.address for c in cleared] == ["BIGsample"]
    winner = cleared[0]
    assert winner.features.closed_trades == 700
    assert report.chance.min_closed_trades is not None
    assert winner.features.closed_trades >= report.chance.min_closed_trades
    assert winner.p_under_null is not None and winner.p_under_null < report.chance.bonferroni_alpha
    assert winner.expected_peers == pytest.approx(
        len(report.screened) * winner.p_under_null
    )
    assert "cleared the sample-size gate" in report.headline


def test_the_null_is_pooled_over_the_screen_including_the_candidate(tmp_db):
    """A dominant wallet drags the base rate towards its own, making it harder to stand out.

    That is a property, not a bug: the alternative is testing a wallet against a null it
    is not part of, which flatters whichever wallet supplied most of the data.
    """
    conn = tmp_db
    for n in range(20):
        _wallet_with_record(conn, f"B{n:02d}base", wins=3, losses=7, start=T0 + n * 3600_000)
    _wallet_with_record(conn, "BIGsample", wins=420, losses=280, start=T0 + 500 * DAY)
    conn.commit()
    report = discover.discover(conn, chain=SOL, as_of_ms=T0 + 5_000 * DAY)
    assert report.chance.null_pooled_trades == 900
    assert report.chance.null_win_rate == pytest.approx(480 / 900)


def test_freeze_records_its_cohort_id_on_the_report(tape):
    report = discover.discover(tape, chain=SOL, as_of_ms=T0 + 400 * DAY)
    cohort_id = discover.freeze_cohort(report, tape)
    assert report.cohort_id == cohort_id
    run_id = discover.store_report(report, tape)
    row = fetch_one(tape, "SELECT cohort_id FROM discovery_runs WHERE run_id = ?", (run_id,))
    assert row is not None and row["cohort_id"] == cohort_id


def test_headline_says_the_useful_thing_when_nothing_clears(tape):
    report = discover.discover(tape, chain=SOL, as_of_ms=T0 + 400 * DAY)
    assert not report.gate_cleared
    assert "distinguishable from luck" in report.headline
    assert "unvalidated" in report.headline


def test_expected_max_z_matches_the_research_table():
    """Sanity on the deflation we borrow from validation.py, at the published values."""
    from kaiba.learning.validation import expected_max_z

    assert expected_max_z(100) == pytest.approx(2.53, abs=0.02)
    assert expected_max_z(1_000) == pytest.approx(3.26, abs=0.02)
    assert expected_max_z(3_142_559) == pytest.approx(5.09, abs=0.02)
    assert not math.isnan(expected_max_z(1))
