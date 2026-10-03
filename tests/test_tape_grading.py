"""Grading wallets from our own tape: coverage honesty, sample caps and the free tag source.

The tape (``swaps``) is the trades we happened to observe on tokens we watched. It is never
a wallet's history. Every test here protects one of the ways that partial view could be
mistaken for knowledge: a sell-only settlement address graded as a trader, three winning
trades bought a follow, an A claimed for a wallet we have never fully seen, a provider's
"smart money" label counted as evidence, a failure rate reported as zero because nothing
failed *on the tape*, money held as a float, twenty buys with no exit widening a one-token
wallet into a B.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from kaiba.core.db import fetch_all
from kaiba.core.schemas import Archetype, Chain, EventKind, EvidenceBasis, Grade, WalletTag
from kaiba.execution import lanes
from kaiba.intelligence import grade, tracker

SOL_WALLET = "Bnd5oBSWpPpXoaWTckgXTVx9TCKkX5GyVqxcrwBzUYsB"
SOL_WALLET_2 = "3JUdoRHLtKkNqHG1ceL9NvGA4ketUcQJTJDf7keaJmaH"
SOL_WALLET_3 = "kv9h5rzPPL6bS2MXbnTzA1imRohMKF8pqFd1UP1aQ2U"
BSC_WALLET = "0x3bc82d3d920208f5f0590319494012599768b1fe"

LAMPORT = 1
SOL = 1_000_000_000
T0 = 1_789_000_000_000


# ============================================================ fixtures


def row(
    ts_ms: int,
    token: str,
    side: str,
    qty,
    native,
    usd=None,
    *,
    source: str = "pumpfun:trades",
    tx: str | None = None,
    chain: str = "sol",
) -> dict:
    """A ``swaps`` row the way SQLite hands it back: numbers as text."""
    return {
        "ts_ms": ts_ms,
        "token": token,
        "side": side,
        "amount_token": None if qty is None else str(qty),
        "amount_native": None if native is None else str(native),
        "usd_value": None if usd is None else str(usd),
        "source": source,
        "tx": tx or f"tx-{token}-{side}-{ts_ms}",
        "chain": chain,
    }


def round_trips(
    n: int,
    *,
    multiple: Decimal = Decimal("2"),
    tokens: int | None = None,
    cost_native: int = 1 * SOL,
    usd_per_sol: Decimal = Decimal("100"),
    hold_s: int = 600,
    start: int = 0,
) -> list[dict]:
    """``n`` closed episodes, each a buy of ``cost_native`` sold at ``multiple`` x.

    ``start`` offsets token names and timestamps so two batches never share either.
    """
    out: list[dict] = []
    distinct = tokens or n
    for j in range(n):
        i = start + j
        token = f"TOKEN{start + (j % distinct):03d}"
        t = T0 + i * 10_000_000
        proceeds = int(Decimal(cost_native) * multiple)
        out.append(row(t, token, "buy", 1_000_000, cost_native, Decimal(cost_native) / SOL * usd_per_sol))
        out.append(
            row(t + hold_s * 1000, token, "sell", 1_000_000, proceeds, Decimal(proceeds) / SOL * usd_per_sol)
        )
    return out


def dangling_buys(n: int, *, start: int = 500, cost_native: int = 1 * SOL) -> list[dict]:
    """``n`` buys on ``n`` fresh tokens that are never sold: open episodes, exits unseen."""
    return [
        row(T0 + (start + j) * 10_000_000, f"OPEN{start + j:03d}", "buy", 1_000_000, cost_native, Decimal(cost_native) / SOL * 100)
        for j in range(n)
    ]


def strong_early() -> grade.EarlyMetrics:
    return grade.EarlyMetrics(validated_early_tokens=60, insider_tokens=15, sniper_tokens=50, best_entry_rank=1)


def evidence(rows: list[dict], **kw) -> grade.WalletEvidence:
    kw.setdefault("as_of_ms", T0 + 10**10)
    return grade.tape_evidence_from_rows(SOL_WALLET, Chain.SOL, rows, **kw)


def seed_swaps(conn, wallet: str, rows: list[dict], chain: str = "sol") -> None:
    for r in rows:
        conn.execute(
            "INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, amount_token, amount_native, "
            "usd_value, source) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                chain, r["tx"], r["ts_ms"], wallet, r["token"], r["side"], r["amount_token"],
                r["amount_native"], r["usd_value"], r["source"],
            ),
        )


def seed_trade_event(conn, wallet: str, tags: list[str], chain: str = "sol", ident: str = "e1") -> None:
    payload = {"wallet": wallet, "feed": "smartmoney", "source": "gmgn:smartmoney", "tags": tags}
    conn.execute(
        "INSERT INTO events (ts_ms, kind, level, chain, subject, payload, dedupe_key) VALUES (?,?,?,?,?,?,?)",
        (T0, EventKind.WALLET_TRADE.value, "info", chain, wallet, json.dumps(payload), f"{wallet}:{ident}"),
    )


# ============================================================ provenance


_NOT_THRESHOLDS = {"TAPE_SCALED_COMPONENTS"}


def test_every_tape_threshold_declares_its_provenance():
    """A silently added knob fails the build, the way bundles.py's table does it."""
    numeric = {
        name
        for name, value in vars(grade).items()
        if name.startswith("TAPE_")
        and isinstance(value, int | Decimal)
        and not isinstance(value, bool)
        and name not in _NOT_THRESHOLDS
    }
    undeclared = numeric - set(grade.TAPE_THRESHOLD_PROVENANCE)
    assert not undeclared, f"tape thresholds with no provenance entry: {sorted(undeclared)}"
    stale = set(grade.TAPE_THRESHOLD_PROVENANCE) - numeric
    assert not stale, f"provenance entries for constants that no longer exist: {sorted(stale)}"


def test_unmeasured_tape_thresholds_say_invented():
    words = ("INVENTED", "MEASURED", "DERIVED", "DEFINITIONAL", "STRUCTURAL", "OPERATIONAL")
    for name, text in grade.TAPE_THRESHOLD_PROVENANCE.items():
        assert any(w in text for w in words), f"{name} does not classify its own provenance"
    # The ones nobody measured must use the word, not hide behind "derived".
    for name in ("TAPE_MIN_CLOSED_FOR_B", "TAPE_MIN_TOKENS_FOR_B", "TAPE_CANDIDATE_MIN_WIN_RATE"):
        assert "INVENTED" in grade.TAPE_THRESHOLD_PROVENANCE[name]


# ============================================================ money


def test_tape_pnl_is_exact_integer_base_units():
    ev = evidence(round_trips(3))
    assert ev.tape is not None and ev.tape.money_axis == "native"
    assert ev.tape.money_basis is EvidenceBasis.VERIFIED_ONCHAIN
    pnl = ev.pnl
    assert pnl is not None
    assert isinstance(pnl.realized_pnl_native, int) and not isinstance(pnl.realized_pnl_native, bool)
    assert pnl.realized_pnl_native == 3 * (2 * SOL - 1 * SOL)
    assert pnl.cost_native == 3 * SOL and pnl.proceeds_native == 6 * SOL
    assert pnl.realized_pnl_usd == Decimal("300")
    assert pnl.closed_episodes == 3 and pnl.win_rate == 1.0
    assert pnl.median_hold_s == 600


def test_gmgn_rows_are_reconstructed_in_integer_micro_dollars():
    """GMGN's ``amount_native`` is a quote amount in an unknown token; only ``usd_value`` is
    consistent, so the whole wallet moves onto integer micro-dollars and says so."""
    rows = [
        row(T0, "TOK", "buy", "2126352.0091361953", "0.147380625", "115.719582234375", source="gmgn:smartmoney"),
        row(T0 + 60_000, "TOK", "sell", "2126352.0091361953", "26932171.5006", "231.4", source="gmgn:smartmoney"),
    ]
    ev = evidence(rows)
    assert ev.tape.money_axis == "usd_micro"
    assert ev.tape.money_basis is EvidenceBasis.PROVIDER_REPORTED
    assert ev.tape.feeds == ["smartmoney"]
    pnl = ev.pnl
    assert pnl.closed_episodes == 1 and pnl.contaminated_episodes == 0
    assert isinstance(pnl.cost_native, int)
    assert pnl.cost_native == 115_719_582  # 115.719582234375 USD, truncated to the micro-dollar
    assert pnl.proceeds_native == 231_400_000
    assert pnl.realized_pnl_native == 231_400_000 - 115_719_582
    assert pnl.realized_pnl_usd == Decimal("231.4") - Decimal("115.719582234375")


def test_gmgn_row_without_usd_loses_its_money_and_contaminates():
    rows = [
        row(T0, "TOK", "buy", "1000.5", "0.5", None, source="gmgn:kol"),
        row(T0 + 60_000, "TOK", "sell", "1000.5", "1.0", "120", source="gmgn:kol"),
    ]
    ev = evidence(rows)
    assert ev.tape.rows_without_money == 1
    assert ev.pnl.contaminated_episodes == 1 and ev.pnl.closed_episodes == 0


def test_gmgn_duplicate_of_an_onchain_row_is_dropped():
    onchain = round_trips(1)
    dup = row(onchain[0]["ts_ms"], "TOKEN000", "buy", "1.0", "1.0", "100", source="gmgn:smartmoney", tx=onchain[0]["tx"])
    ev = evidence([*onchain, dup])
    assert ev.tape.rows_raw == 3 and ev.tape.rows == 2
    assert ev.tape.rows_dropped_duplicate == 1 and ev.tape.rows_dropped_mixed_units == 0
    assert ev.tape.money_axis == "native"  # nothing human-unit survived
    assert ev.pnl.buys == 1 and ev.pnl.sells == 1


def test_gmgn_row_on_a_token_we_hold_in_base_units_is_dropped():
    onchain = round_trips(1)
    mixed = row(T0 + 5_000, "TOKEN000", "buy", "0.5", "0.2", "20", source="gmgn:smartmoney", tx="other-tx")
    ev = evidence([*onchain, mixed])
    assert ev.tape.rows_dropped_mixed_units == 1
    assert ev.tape.rows == 2 and ev.tape.money_axis == "native"


def test_observed_sides_are_counted_before_any_row_is_dropped():
    onchain = round_trips(1)
    dup = row(onchain[1]["ts_ms"], "TOKEN000", "sell", "1.0", "2.0", "200", source="gmgn:kol", tx=onchain[1]["tx"])
    ev = evidence([*onchain, dup])
    assert (ev.observed_buys, ev.observed_sells) == (1, 2)


# ============================================================ refusals and caps


def test_sell_only_tape_wallet_is_refused():
    rows = [row(T0 + i * 1000, f"TOK{i}", "sell", 1_000, 5 * SOL, "500") for i in range(12)]
    ev = evidence(rows)
    assert (ev.observed_buys, ev.observed_sells) == (0, 12)
    score = grade.score_wallet(ev)
    assert score.grade is Grade.QUARANTINED
    assert score.penalties and score.penalties[0].startswith("sell_only:")
    assert score.evidence_weight == 0.0
    assert ev.tape is not None and ev.tape.rows == 12  # the coverage still rides along


def test_failure_rate_is_unavailable_not_zero():
    ev = evidence(round_trips(12))
    assert ev.tape.tx_failure_rate is None
    assert ev.tape.tx_failure_basis is EvidenceBasis.UNAVAILABLE


def test_coverage_basis_is_carried_on_evidence_and_score():
    ev = evidence(round_trips(12, tokens=6))
    cov = ev.tape
    assert cov.partial is True
    assert cov.sources == {"pumpfun:trades": 24}
    assert cov.rows == 24 and cov.distinct_tokens == 6
    assert cov.first_ms == T0 and cov.last_ms == T0 + 11 * 10_000_000 + 600_000
    assert ev.receipts[0].provider == "kaiba" and ev.receipts[0].endpoint == "swaps"
    assert ev.receipts[0].basis is EvidenceBasis.VERIFIED_ONCHAIN
    score = grade.score_wallet(ev)
    assert score.model_version == grade.MODEL_ID_TAPE != grade.MODEL_ID
    assert any(b.startswith("provisional: graded from partial tape") for b in score.blockers)
    assert any("unseen history" in b for b in score.blockers)
    assert score.receipts == ev.receipts


def test_tape_credit_shape():
    assert grade.tape_credit(None) == 0
    assert grade.tape_credit(grade.TAPE_MIN_CLOSED_EPISODES - 1) == 0
    assert grade.tape_credit(6) == Decimal("0.5")
    assert grade.tape_credit(grade.TAPE_FULL_CREDIT_CLOSED_EPISODES) == 1
    assert grade.tape_credit(10_000) == 1


def test_two_round_trips_are_unscored_not_graded():
    score = grade.score_wallet(evidence(round_trips(2, multiple=Decimal("10"))))
    assert score.grade is Grade.UNSCORED
    assert score.evidence_weight == 0.0
    assert any("tape too thin: 2 closed episodes" in b for b in score.blockers)
    assert any("realized_profit" in b for b in score.blockers)


def test_evidence_weight_grows_with_the_sample():
    thin = grade.score_wallet(evidence(round_trips(4)))
    mid = grade.score_wallet(evidence(round_trips(6)))
    full = grade.score_wallet(evidence(round_trips(12)))
    assert thin.evidence_weight < mid.evidence_weight < full.evidence_weight
    assert full.evidence_weight == 66.0  # five tape components at full credit
    assert mid.evidence_weight == 33.0
    assert thin.grade is Grade.UNSCORED  # 22 < MIN_EVIDENCE_WEIGHT
    assert mid.grade is not Grade.UNSCORED
    # The normalised score is unchanged by the credit: points and cap scale together.
    for factor in mid.factors:
        assert "tape_credit=0.50" in (factor.detail or "")


def test_three_perfect_trades_cannot_reach_b():
    """Three 10x wins plus a strong early-buyer record: enough evidence to be scored, not
    enough to be followed."""
    ev = evidence(round_trips(3, multiple=Decimal("10"), tokens=3), early_metrics=strong_early())
    score = grade.score_wallet(ev)
    assert score.grade is not Grade.UNSCORED
    assert score.score >= grade.B_MIN_SCORE  # it *looks* like a B
    assert score.grade is Grade.C
    assert any(b.startswith("capped to C: tape too thin for a B") for b in score.blockers)
    assert any("3 closed episodes < 8" in b for b in score.blockers)
    assert any("3 tokens with a closed round trip < 4" in b for b in score.blockers)


def test_seven_wins_over_seven_tokens_is_still_capped_to_c():
    ev = evidence(round_trips(7, multiple=Decimal("10")), early_metrics=strong_early())
    score = grade.score_wallet(ev)
    assert score.score >= grade.B_MIN_SCORE
    assert score.grade is Grade.C
    assert any("7 closed episodes < 8" in b for b in score.blockers)


def test_eight_wins_over_four_tokens_earns_a_provisional_b():
    ev = evidence(round_trips(8, multiple=Decimal("10"), tokens=4), early_metrics=strong_early())
    assert ev.closed_distinct_tokens == 4 and ev.sample_tokens == (4, "closed_episodes")
    score = grade.score_wallet(ev)
    assert score.grade is Grade.B
    assert not any(b.startswith("capped to C") for b in score.blockers)
    assert any("provisional" in b for b in score.blockers)


def test_eight_closed_on_one_token_plus_twenty_dangling_buys_is_not_a_b():
    """The closed-only pin. The shape the live tape produces — MEASURED 2026-09-22, the one
    sol tape B carries 37 open buys against 9 closed round trips, and the open-inclusive
    count showed it 39 tokens where 9 had closed — pushed to the extreme: every closed
    round trip on ONE token. Counting open episodes in either the B gate or the breadth
    term (the mutation) turns this C back into a B."""
    ev = evidence(
        [*round_trips(8, multiple=Decimal("10"), tokens=1), *dangling_buys(20)],
        early_metrics=strong_early(),
    )
    assert ev.pnl.closed_episodes == 8 and ev.pnl.open_episodes == 20
    assert ev.pnl.distinct_tokens == 21, "the aggregate still counts the open ones ..."
    assert ev.closed_distinct_tokens == 1, "... and the grader does not"
    assert ev.sample_tokens == (1, "closed_episodes")
    assert ev.distinct_tokens == 21, "the stored touched-token count is unchanged"

    score = grade.score_wallet(ev)
    assert score.score >= grade.B_MIN_SCORE, "it is the gate, not the score, that stops it"
    assert score.grade is Grade.C
    cap = next(b for b in score.blockers if b.startswith("capped to C: tape too thin for a B"))
    assert "1 tokens with a closed round trip < 4" in cap
    assert "21" not in cap, "the open-inclusive count is not the gate's number"
    assert "over 21 tokens" in next(b for b in score.blockers if b.startswith("provisional")), "coverage still says what was touched"


def _breadth_of(rows: list[dict]):
    factors, _ = grade.components_for(evidence(rows))
    return next(f for f in factors if f.name == "breadth")


def test_breadth_is_a_function_of_closed_round_trips_only():
    """Same wallet, the component itself. Twenty buys with no exit change nothing: not the
    token term (1 token, not 21) and not the sell-to-buy band, which on the tape would
    otherwise *rise* from 0.667 at 8/8 to 1.0 at 8/28 — the mutation."""
    closed_only = round_trips(8, multiple=Decimal("10"), tokens=1)
    with_dangling = _breadth_of([*closed_only, *dangling_buys(20)])
    control = _breadth_of(closed_only)
    assert "distinct_tokens=1 (basis=closed_episodes)" in (with_dangling.detail or "")
    assert "sell_to_buy=1.000 (basis=closed_episodes)" in (with_dangling.detail or "")
    assert with_dangling.points == control.points
    # Token term is 0 below ten tokens; the band decays 1.0 -> 0.667 at a 1:1 ratio, so the
    # component is exactly 0.3 * 0.667 of its 9 points.
    assert with_dangling.points == pytest.approx(0.3 * (2 / 3) * grade.COMPONENT_MAX["breadth"])
    # The open buys are still counted where "never sells" belongs.
    ev = evidence([*closed_only, *dangling_buys(20)])
    assert ev.trade_counts == (28, 8) and (ev.closed_buys, ev.closed_sells) == (8, 8)
    assert ev.breadth_sell_to_buy == (1.0, "closed_episodes") and ev.sell_to_buy_ratio == pytest.approx(8 / 28)


def test_tape_b_gate_refuses_a_token_count_that_is_not_closed_only():
    ev = evidence(round_trips(8, multiple=Decimal("10"), tokens=4), early_metrics=strong_early())
    ev.closed_distinct_tokens = None  # a hand-built PnL: the aggregate's open-inclusive count
    assert ev.sample_tokens == (4, "episodes_incl_open")
    score = grade.score_wallet(ev)
    assert score.grade is Grade.C
    assert any("tokens with a closed round trip unknown (token count basis=episodes_incl_open)" in b for b in score.blockers)


def test_closed_token_count_uses_the_aggregates_own_closed_rule():
    from kaiba.intelligence import pnl

    rows = [*round_trips(2, tokens=2), *dangling_buys(3)]
    # A closed episode contaminated by a transfer is not scorable and not breadth either.
    rows.append(row(T0 + 10**9, "DIRTY", "transfer_in", 5, None, None))
    rows.append(row(T0 + 10**9 + 1, "DIRTY", "sell", 5, SOL, "100"))
    episodes = pnl.reconstruct(rows, as_of_ms=T0 + 10**10)
    assert grade.closed_token_count(episodes) == 2
    assert grade.closed_token_count([]) == 0
    assert grade.closed_side_counts(episodes) == (2, 2) and grade.closed_side_counts([]) == (0, 0)
    summary = pnl.summarize(episodes)
    assert summary.distinct_tokens == 5, "clean tokens incl. open; DIRTY excluded"
    assert (summary.buys, summary.sells) == (5, 2), "the aggregate keeps the open buys"


def test_unseen_history_cannot_be_graded_a():
    """A wallet whose tape is flawless: 30 closed 10x wins over 20 tokens, six figures of
    realised USD, a top early-buyer record. Enough weight to clear the A evidence gate on
    its own — and still a B, because the tape is the tokens we watched, not the wallet."""
    ev = evidence(
        round_trips(30, multiple=Decimal("10"), tokens=20, cost_native=100 * SOL, usd_per_sol=Decimal("2500")),
        early_metrics=strong_early(),
    )
    score = grade.score_wallet(ev)
    assert score.score >= grade.A_MIN_SCORE
    assert score.evidence_weight >= grade.A_MIN_EVIDENCE_WEIGHT
    assert score.closed_trades == 30 and score.distinct_tokens == 20
    assert score.grade is Grade.B
    tape_caps = [b for b in score.blockers if b.startswith("capped to B: partial tape")]
    assert len(tape_caps) == 1 and "unseen history" in tape_caps[0]
    # ... and it was the only thing standing in the way.
    assert not any(b.startswith("capped to B: evidence_weight") for b in score.blockers)
    assert not any("closed episodes <" in b for b in score.blockers)


def test_backfilled_evidence_without_a_tape_block_is_unchanged():
    """The caps key off ``tape``; evidence assembled the old way keeps the old model id."""
    ev = evidence(round_trips(30, multiple=Decimal("10"), tokens=20, cost_native=100 * SOL, usd_per_sol=Decimal("2500")), early_metrics=strong_early())
    ev.tape = None
    ev.receipts = []
    score = grade.score_wallet(ev)
    assert score.grade is Grade.A
    assert score.model_version == grade.MODEL_ID
    assert not any("tape" in b for b in score.blockers)


def test_a_thin_losing_tape_may_still_be_a_d():
    """Asymmetric by design: a wrong D costs nothing, a wrong B costs money."""
    ev = evidence(round_trips(4, multiple=Decimal("0.2")), early_metrics=strong_early())
    score = grade.score_wallet(ev)
    assert score.grade is Grade.D
    assert any(p.startswith("realized_pnl_negative") for p in score.penalties)


# ============================================================ provider tags


def test_map_provider_tags_admits_only_labels_that_can_lower_a_wallet():
    tags = grade.map_provider_tags(
        ["smart_degen", "axiom", "kol", "top_followed", "wash_trader", "gmgn_go", "kol", "bundler", "renowned"]
    )
    assert tags == [WalletTag.WASH_TRADER, WalletTag.BUNDLER]
    assert grade.admit_vendor_label("smart_money") is None, "even spelled exactly like the tag"
    assert grade.admit_vendor_label(" sandwich_bot ") is WalletTag.SANDWICH_BOT
    assert not hasattr(grade, "PROVIDER_TAG_ALIASES"), "the positive-label aliases must stay gone"


def test_vendor_admitted_tags_are_disjoint_from_every_positive_vocabulary():
    admitted = grade.VENDOR_ADMITTED_TAGS
    assert not (admitted & set(grade.POSITIVE_REPUTATION_TAGS))
    assert not (admitted & lanes.SMART_TAGS)
    assert not ({t.value for t in admitted} & tracker.LANE_SMART_TAGS)
    assert WalletTag.KOL not in admitted and WalletTag.SMART_MONEY not in admitted
    # Each admitted label, on its own, quarantines or penalises; none can add a point.
    for tag in sorted(admitted):
        ev = grade.WalletEvidence(address=SOL_WALLET, chain=Chain.SOL, tags=[tag])
        score = grade.score_wallet(ev)
        assert score.grade is Grade.QUARANTINED or grade.penalties_for(ev), tag
        assert not any(f.name == "reputation" for f in grade.components_for(ev)[0]), tag


def test_tags_json_vendor_namespace_is_read_back_negative_only():
    read = grade._tags_from(
        ["gmgn:smart_degen", "gmgn:kol", "gmgn:renowned", "gmgn:wash_trader", "gmgn:bundler", "smart_money", "gmgn:nonsense", "junk"]
    )
    assert read == [WalletTag.WASH_TRADER, WalletTag.BUNDLER, WalletTag.SMART_MONEY]
    # A bare smart_money (written by a screened path, or the operator) still passes.


def test_provider_tags_are_recorded_and_never_a_tag_an_archetype_or_a_point(tmp_db):
    rows = [
        row(T0 + i * 100_000, f"TOK{i}", s, "1000.5", "0.5", "100" if s == "buy" else "200", source="gmgn:smartmoney")
        for i in range(8)
        for s in ("buy", "sell")
    ]
    seed_swaps(tmp_db, SOL_WALLET, rows)
    seed_trade_event(tmp_db, SOL_WALLET, ["smart_degen", "axiom"], ident="a")
    seed_trade_event(tmp_db, SOL_WALLET, ["smart_degen", "arbitrager", "kol", "top_followed"], ident="b")
    tmp_db.execute(
        "INSERT INTO wallets (chain, address, name, source, tags_json, first_seen_ms, last_seen_ms, meta_json) "
        "VALUES ('sol', ?, 'x', 'gmgn:smartmoney', ?, 1, 1, '{}')",
        (SOL_WALLET, json.dumps(["gmgn:smart_degen", "gmgn:kol"])),
    )
    ev = grade.build_tape_evidence(SOL_WALLET, Chain.SOL, tmp_db)
    assert ev.tape.provider_tags == ["smart_degen", "axiom", "arbitrager", "kol", "top_followed"]
    assert ev.tape.provider_tag_basis is EvidenceBasis.PROVIDER_REPORTED
    assert ev.tags == [], "neither the event bus nor tags_json turned a positive label into a tag"
    gmgn_receipts = [r for r in ev.receipts if r.provider == "gmgn"]
    assert len(gmgn_receipts) == 1 and gmgn_receipts[0].basis is EvidenceBasis.PROVIDER_REPORTED
    assert "not a quality signal" in (gmgn_receipts[0].note or "")

    score = grade.score_wallet(ev)
    assert score.archetype is not Archetype.SMART_MONEY and score.archetype is not Archetype.KOL
    assert not any(f.name == "reputation" for f in score.factors)
    assert any(b == "missing component: reputation" for b in score.blockers)

    # Stored, the grade cannot feed the lane's archetype route either.
    grade.store_score(score, tmp_db)
    assert tracker.lane_smart_wallets(Chain.SOL, tmp_db) == set()


def test_provider_tags_still_score_reputation_on_the_backfill_path():
    """Guard for the suppression rule: it applies to tape evidence only."""
    ev = grade.WalletEvidence(address=SOL_WALLET, chain=Chain.SOL, tags=[WalletTag.SMART_MONEY])
    factors, _ = grade.components_for(ev)
    assert any(f.name == "reputation" for f in factors)
    ev.tape = grade.TapeCoverage()
    factors, _ = grade.components_for(ev)
    assert not any(f.name == "reputation" for f in factors)


def test_wash_trader_provider_tag_quarantines(tmp_db):
    seed_swaps(tmp_db, SOL_WALLET, round_trips(12))
    seed_trade_event(tmp_db, SOL_WALLET, ["smart_degen", "wash_trader"])
    score = grade.grade_from_tape(SOL_WALLET, Chain.SOL, tmp_db)
    assert score.grade is Grade.QUARANTINED
    assert any("wash_trader" in p for p in score.penalties)


def test_no_trade_events_means_tags_unavailable_not_empty(tmp_db):
    seed_swaps(tmp_db, SOL_WALLET, round_trips(3))
    ev = grade.build_tape_evidence(SOL_WALLET, Chain.SOL, tmp_db)
    assert ev.tape.provider_tags == []
    assert ev.tape.provider_tag_basis is EvidenceBasis.UNAVAILABLE
    assert [r.provider for r in ev.receipts] == ["kaiba"]


def test_events_for_another_wallet_or_chain_do_not_leak(tmp_db):
    seed_swaps(tmp_db, SOL_WALLET, round_trips(3))
    seed_trade_event(tmp_db, SOL_WALLET_2, ["kol"], ident="other-wallet")
    seed_trade_event(tmp_db, SOL_WALLET, ["kol"], chain="bsc", ident="other-chain")
    ev = grade.build_tape_evidence(SOL_WALLET, Chain.SOL, tmp_db)
    assert ev.tape.provider_tags == []
    index = grade.provider_tag_index(tmp_db, Chain.SOL)
    assert index == {SOL_WALLET_2: ["kol"]}


# ============================================================ the batch runner


def _seed_three_wallets(conn) -> None:
    seed_swaps(conn, SOL_WALLET, round_trips(12, multiple=Decimal("10"), tokens=6))  # a real sample
    seed_swaps(conn, SOL_WALLET_2, [row(T0 + i * 1000, f"S{i}", "sell", 1000, SOL, "100") for i in range(12)])
    seed_swaps(conn, SOL_WALLET_3, [row(T0, "ONE", "buy", 1000, SOL, "100")])  # one row: not a trade


def test_batch_run_reports_measured_counts_and_writes_nothing_by_default(tmp_db):
    _seed_three_wallets(tmp_db)
    report = grade.grade_tape(tmp_db, Chain.SOL)
    d = report.as_dict()
    assert d["wallets_seen"] == 3 and d["wallets_scored"] == 2 and d["wallets_too_thin"] == 1
    assert d["rows_read"] == 24 + 12 + 1
    assert d["by_grade"] == {"B": 1, "QUARANTINED": 1}  # 12 closed over 6 tokens clears the B gate
    assert d["sell_only_refused"] == 1 and d["quarantined_by_tag"] == 0
    assert d["b_or_better"] == 1 and d["capped_small_n"] == 0
    assert d["capped_partial_tape"] == 0  # nothing scored >= 70, so the A gate never ran
    assert d["by_money_axis"] == {"native": 2}
    assert d["stored"] == 0 and d["elapsed_s"] >= 0
    assert fetch_all(tmp_db, "SELECT * FROM wallet_scores") == []


def test_batch_store_writes_tape_grades_and_keeps_full_history_grades(tmp_db):
    _seed_three_wallets(tmp_db)
    tmp_db.execute(
        "INSERT INTO wallet_scores (chain, address, score, grade, evidence_weight, archetype, "
        "model_version, scored_at_ms) VALUES ('sol', ?, 80.0, 'A', 100.0, 'trader', ?, 1)",
        (SOL_WALLET, grade.MODEL_ID),
    )
    report = grade.grade_tape(tmp_db, Chain.SOL, store=True)
    assert report.stored == 1 and report.kept_full_grade == 1
    stored = {r["address"]: r for r in fetch_all(tmp_db, "SELECT * FROM wallet_scores")}
    assert stored[SOL_WALLET]["grade"] == "A" and stored[SOL_WALLET]["model_version"] == grade.MODEL_ID
    assert stored[SOL_WALLET_2]["grade"] == "QUARANTINED"
    assert stored[SOL_WALLET_2]["model_version"] == grade.MODEL_ID_TAPE

    report = grade.grade_tape(tmp_db, Chain.SOL, store=True, overwrite_full=True)
    assert report.stored == 2 and report.kept_full_grade == 0
    stored = {r["address"]: r for r in fetch_all(tmp_db, "SELECT * FROM wallet_scores")}
    assert stored[SOL_WALLET]["model_version"] == grade.MODEL_ID_TAPE
    assert stored[SOL_WALLET]["grade"] == "B"


def test_batch_matches_the_single_wallet_path(tmp_db):
    _seed_three_wallets(tmp_db)
    seed_trade_event(tmp_db, SOL_WALLET, ["smart_degen"])
    seen: dict[str, object] = {}
    grade.grade_tape(tmp_db, Chain.SOL, on_score=lambda ev, s: seen.__setitem__(ev.address, s))
    single = grade.grade_from_tape(SOL_WALLET, Chain.SOL, tmp_db)
    batch = seen[SOL_WALLET]
    assert (batch.grade, batch.score, batch.evidence_weight, batch.archetype) == (
        single.grade, single.score, single.evidence_weight, single.archetype,
    )
    assert batch.archetype is not Archetype.SMART_MONEY, "a GMGN label does not name the archetype"
    listed = grade.grade_tape(tmp_db, Chain.SOL, wallets=[SOL_WALLET])
    assert listed.wallets_seen == 1 and listed.by_grade == {single.grade.value: 1}


def test_candidates_are_ranked_by_sample_and_exclude_full_graded(tmp_db):
    seed_swaps(tmp_db, SOL_WALLET, round_trips(5, multiple=Decimal("3"), tokens=5))
    seed_swaps(tmp_db, SOL_WALLET_2, round_trips(9, multiple=Decimal("3"), tokens=9))
    seed_swaps(tmp_db, SOL_WALLET_3, [row(T0 + i * 1000, f"S{i}", "sell", 1000, SOL, "100") for i in range(12)])
    report = grade.grade_tape(tmp_db, Chain.SOL)
    assert [c["address"] for c in report.candidates] == [SOL_WALLET_2, SOL_WALLET]
    top = report.candidates[0]
    assert top["closed_episodes"] == 9 and top["realized_pnl"] == 9 * 2 * SOL
    assert top["closed_distinct_tokens"] == 9 == top["distinct_tokens"]
    assert "partial tape" in top["reason"]
    assert report.candidates[1]["grade"] == "UNSCORED"  # a candidate precisely because thin

    tmp_db.execute(
        "INSERT INTO wallet_scores (chain, address, score, grade, evidence_weight, archetype, "
        "model_version, scored_at_ms) VALUES ('sol', ?, 50.0, 'B', 100.0, 'trader', ?, 1)",
        (SOL_WALLET_2, grade.MODEL_ID),
    )
    report = grade.grade_tape(tmp_db, Chain.SOL, candidate_limit=5)
    assert [c["address"] for c in report.candidates] == [SOL_WALLET]


def test_candidate_reason_refuses_losers_and_sell_only():
    losing = evidence(round_trips(6, multiple=Decimal("0.5")))
    assert grade.tape_candidate_reason(losing, grade.score_wallet(losing)) is None
    coin_flip = evidence(
        [*round_trips(2, multiple=Decimal("3")), *round_trips(3, multiple=Decimal("0.5"), start=2)]
    )
    assert coin_flip.pnl.closed_episodes == 5 and coin_flip.pnl.wins == 2
    assert grade.tape_candidate_reason(coin_flip, grade.score_wallet(coin_flip)) is None
    sell_only = evidence([row(T0 + i, f"S{i}", "sell", 1000, SOL, "100") for i in range(12)])
    assert grade.tape_candidate_reason(sell_only, grade.score_wallet(sell_only)) is None
    good = evidence(round_trips(2, multiple=Decimal("3")))
    reason = grade.tape_candidate_reason(good, grade.score_wallet(good))
    assert reason is not None and "2 closed episodes" in reason and "base units" in reason


def test_candidate_needs_net_profit_not_just_a_win_rate():
    """Two +10% wins and one -90% loss: a 0.67 win rate that lost money. Not a candidate —
    credits go to wallets whose tape made money, not to ones that merely won often."""
    ev = evidence([*round_trips(2, multiple=Decimal("1.1")), *round_trips(1, multiple=Decimal("0.1"), start=2)])
    assert ev.pnl.closed_episodes == 3 and ev.pnl.wins == 2
    assert ev.pnl.realized_pnl_native < 0
    assert grade.tape_candidate_reason(ev, grade.score_wallet(ev)) is None


def test_gmgn_only_wallet_reports_the_usd_axis_in_its_candidate_row(tmp_db):
    rows = [
        row(T0 + i * 100_000, f"TOK{i}", s, "1000.5", "0.5", "100" if s == "buy" else "300", source="gmgn:smartmoney", chain="bsc")
        for i in range(4)
        for s in ("buy", "sell")
    ]
    seed_swaps(tmp_db, BSC_WALLET, rows, chain="bsc")
    report = grade.grade_tape(tmp_db, Chain.BSC)
    assert report.by_money_axis == {"usd_micro": 1}
    assert report.candidates[0]["money_axis"] == "usd_micro"
    assert report.candidates[0]["realized_pnl"] == 4 * 200 * grade.TAPE_USD_MICRO
    assert "micro-USD" in report.candidates[0]["reason"]


@pytest.mark.parametrize("min_rows", [1, 2, 3])
def test_min_rows_is_a_tally_not_a_grade(tmp_db, min_rows):
    _seed_three_wallets(tmp_db)
    report = grade.grade_tape(tmp_db, Chain.SOL, min_rows=min_rows)
    assert report.wallets_seen == 3
    assert report.wallets_scored + report.wallets_too_thin == 3
    assert sum(report.by_grade.values()) == report.wallets_scored
