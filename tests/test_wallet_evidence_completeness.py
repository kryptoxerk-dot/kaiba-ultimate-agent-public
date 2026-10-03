"""Two evidence defects the grader had, written as the failures they prevent.

1. **The truncated-sample A gate could not fire.** ``WalletEvidence.sample_capped`` was read
   from a ``wallets.meta_json`` key nothing writes, while every paid sol grade rested on a
   one-page (100 transaction) Helius walk and every robinhood full-model grade on our tape
   alone. ``grade.history_truncated`` now derives it from the walk's own cursor.
2. **Realized USD was summed over the closed episodes that happened to carry USD**, and
   the sign of that partial sum decided the 35-point loss penalty. MEASURED 2026-10-02:
   on 17 of 150 paid sol grades it disagreed with the wallet's native result over all its
   round trips. ``grade.complete_realized_usd`` prices the rest at the wallet's own rate.

Both directions of (2) are pinned, because a fix that only ever adds the penalty (or only
ever removes it) would pass a one-sided test while manufacturing grades.
"""

from __future__ import annotations

import json
from decimal import Decimal

from kaiba.core.schemas import Chain
from kaiba.intelligence import grade as G
from kaiba.intelligence.pnl import reconstruct, summarize

WALLET = "Bnd5oBSWpPpXoaWTckgXTVx9TCKkX5GyVqxcrwBzUYsB"
SOL = 1_000_000_000


def row(i: int, token: str, side: str, native: int, usd: str | None, source: str) -> dict:
    return {
        "chain": "sol", "tx": f"tx{i}", "ts_ms": 1_000 + i, "token": token, "side": side,
        "amount_token": "1000", "amount_native": str(native), "usd_value": usd, "source": source,
    }


def losing_mix() -> list[dict]:
    """+$20 on the one round trip with USD, -5 SOL on the one without (rate $200/SOL)."""
    return [
        row(0, "AAA", "buy", 1 * SOL, "200", "pumpfun:trades"),
        row(1, "AAA", "sell", 11 * SOL // 10, "220", "pumpfun:trades"),
        row(2, "BBB", "buy", 10 * SOL, None, "helius:backfill"),
        row(3, "BBB", "sell", 5 * SOL, None, "helius:backfill"),
    ]


def winning_mix() -> list[dict]:
    """-$50 on the one round trip with USD, +5 SOL on the one without (rate $200/SOL)."""
    return [
        row(0, "AAA", "buy", 1 * SOL, "200", "pumpfun:trades"),
        row(1, "AAA", "sell", 3 * SOL // 4, "150", "pumpfun:trades"),
        row(2, "BBB", "buy", 1 * SOL, None, "helius:backfill"),
        row(3, "BBB", "sell", 6 * SOL, None, "helius:backfill"),
    ]


def seed(conn, rows: list[dict]) -> None:
    for r in rows:
        conn.execute(
            "INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, amount_token, amount_native, "
            "usd_value, source) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("sol", r["tx"], r["ts_ms"], WALLET, r["token"], r["side"], r["amount_token"],
             r["amount_native"], r["usd_value"], r["source"]),
        )


def penalty_names(ev: G.WalletEvidence) -> set[str]:
    return {p.name for p in G.penalties_for(ev)}


# ------------------------------------------------------------------ realized USD, completed


def test_the_partial_sum_is_what_summarize_reports():
    """The defect itself, so the fix below is shown to change something real."""
    eps = reconstruct(losing_mix())
    assert summarize(eps).realized_pnl_usd == Decimal("20")
    assert summarize(eps).realized_pnl_native == -49 * SOL // 10


def test_unpriced_round_trips_are_priced_at_the_wallets_own_rate():
    rows = losing_mix()
    eps = reconstruct(rows)
    pnl, imputed = G.complete_realized_usd(eps, rows, summarize(eps))
    # rate = (200 + 220) / 2.1 SOL = $200/SOL; BBB's -5 SOL is -$1,000.
    assert imputed == 1
    assert pnl.realized_pnl_usd == Decimal("-980")


def test_a_loss_hidden_by_the_partial_sum_is_now_penalised(tmp_db):
    """MUTATION: dropping ``complete_realized_usd`` from build_evidence fails here."""
    seed(tmp_db, losing_mix())
    ev = G.build_evidence(WALLET, Chain.SOL, tmp_db)
    assert ev.pnl.realized_pnl_usd == Decimal("-980") and ev.realized_usd_imputed == 1
    assert "realized_pnl_negative" in penalty_names(ev)
    factor = next(f for f in G.components_for(ev)[0] if f.name == "realized_profit")
    assert "1 priced at the wallet's own observed native/USD rate" in factor.detail


def test_a_profit_hidden_by_the_partial_sum_is_no_longer_penalised(tmp_db):
    """The other direction: a -$50 slice of a +5 SOL wallet used to cost it 35 points."""
    seed(tmp_db, winning_mix())
    ev = G.build_evidence(WALLET, Chain.SOL, tmp_db)
    assert ev.pnl.realized_pnl_usd == Decimal("950")
    assert "realized_pnl_negative" not in penalty_names(ev)


def test_the_tape_builder_completes_the_same_way():
    """MUTATION: dropping ``complete_realized_usd`` from tape_evidence_from_rows fails here."""
    ev = G.tape_evidence_from_rows(WALLET, Chain.SOL, losing_mix(), as_of_ms=10**13)
    assert ev.tape is not None and ev.tape.money_axis == "native"
    assert ev.pnl.realized_pnl_usd == Decimal("-980") and ev.realized_usd_imputed == 1


def test_complete_usd_is_left_alone():
    rows = losing_mix()[:2]
    eps = reconstruct(rows)
    before = summarize(eps)
    pnl, imputed = G.complete_realized_usd(eps, rows, before)
    assert imputed == 0 and pnl.realized_pnl_usd == Decimal("20") and pnl is before


def test_no_rate_means_unmeasured_and_the_penalty_falls_back_to_native(tmp_db):
    """No row carries both a native amount and USD: there is no honest rate, so the USD
    figure is unmeasured rather than a guess, and the loss penalty reads the native sign,
    which covers every closed round trip."""
    seed(tmp_db, losing_mix()[2:])
    ev = G.build_evidence(WALLET, Chain.SOL, tmp_db)
    assert ev.pnl.realized_pnl_usd is None
    assert "realized_profit" in G.components_for(ev)[1]
    assert "realized_pnl_negative" in penalty_names(ev)


def test_a_partial_sum_without_a_rate_is_unmeasured_not_kept():
    """USD on one round trip, but no row carries both a native amount and USD (here the
    priced legs report a zero native leg), so nothing can price the other round trip. The
    partial +$30 must not survive as if it covered both. MUTATION: returning ``pnl``
    unchanged on the no-rate branch fails here."""
    rows = [
        row(0, "AAA", "buy", 0, "100", "pumpfun:trades"),
        row(1, "AAA", "sell", 0, "130", "pumpfun:trades"),
        row(2, "BBB", "buy", 10 * SOL, None, "helius:backfill"),
        row(3, "BBB", "sell", 5 * SOL, None, "helius:backfill"),
    ]
    eps = reconstruct(rows)
    assert summarize(eps).realized_pnl_usd == Decimal("30")
    pnl, imputed = G.complete_realized_usd(eps, rows, summarize(eps))
    assert pnl.realized_pnl_usd is None and imputed == 1


def test_a_usd_micro_tape_rates_at_one_micro_dollar():
    """On the tape's usd_micro axis amount_native IS micro-dollars, so a completed figure
    must equal the micro-dollar result divided by a million, not something rescaled."""
    rows = [
        {**row(0, "AAA", "buy", 0, "100", "gmgn:smartmoney"), "amount_token": "5.0"},
        {**row(1, "AAA", "sell", 0, "130", "gmgn:smartmoney"), "amount_token": "5.0"},
    ]
    ev = G.tape_evidence_from_rows(WALLET, Chain.SOL, rows, as_of_ms=10**13)
    assert ev.tape.money_axis == "usd_micro"
    assert ev.pnl.realized_pnl_usd == Decimal("30") and ev.realized_usd_imputed == 0


# ------------------------------------------------------------------ truncated history


def cursor(conn, value) -> None:
    conn.execute(
        "INSERT INTO kv (key, value, updated_ms) VALUES (?,?,?)",
        (f"backfill:swaps:sol:{WALLET}", value if isinstance(value, str) else json.dumps(value), 1),
    )


def test_a_wallet_never_walked_is_truncated(tmp_db):
    """No cursor: the full-history label over our tape alone. MUTATION: returning False
    for a missing cursor fails here."""
    seed(tmp_db, losing_mix())
    assert G.history_truncated(tmp_db, Chain.SOL, WALLET) is True
    assert G.build_evidence(WALLET, Chain.SOL, tmp_db).sample_capped is True


def test_an_unfinished_walk_caps_the_sample(tmp_db):
    """MUTATION: dropping ``or history_truncated(...)`` from build_evidence fails here, and
    so does inverting the ``exhausted`` test inside history_truncated."""
    seed(tmp_db, losing_mix())
    cursor(tmp_db, {"oldest_sig": "x", "pages": 1, "exhausted": False})
    assert G.build_evidence(WALLET, Chain.SOL, tmp_db).sample_capped is True


def test_a_finished_walk_does_not(tmp_db):
    """The positive control: without it every assertion above passes on a function that
    always answers True, and no wallet could ever be graded A again."""
    seed(tmp_db, losing_mix())
    cursor(tmp_db, {"oldest_sig": "x", "pages": 3, "exhausted": True})
    assert G.history_truncated(tmp_db, Chain.SOL, WALLET) is False
    assert G.build_evidence(WALLET, Chain.SOL, tmp_db).sample_capped is False


def test_a_forward_hole_or_an_unreadable_cursor_caps_it(tmp_db):
    cursor(tmp_db, {"pages": 3, "exhausted": True, "forward_gap": True})
    assert G.history_truncated(tmp_db, Chain.SOL, WALLET) is True
    tmp_db.execute("UPDATE kv SET value = ? WHERE key LIKE 'backfill:swaps:%'", ("[1, 2]",))
    assert G.history_truncated(tmp_db, Chain.SOL, WALLET) is True


def test_a_truncated_sample_is_an_a_gate_and_nothing_else():
    """Only the A gate reads sample_capped: the same evidence scores the same and grades
    B instead of A, and a wallet under the A score is not touched at all."""
    strong = G.WalletEvidence.model_validate(
        json.loads((__import__("pathlib").Path(__file__).parent / "fixtures" / "grade" /
                    "evidence_strong.json").read_text(encoding="utf-8"))
    )
    whole = G.score_wallet(strong)
    capped = G.score_wallet(strong.model_copy(update={"sample_capped": True}))
    assert whole.grade.value == "A" and capped.grade.value == "B"
    assert whole.score == capped.score and whole.evidence_weight == capped.evidence_weight
    assert any("sample was truncated" in b for b in capped.blockers)
