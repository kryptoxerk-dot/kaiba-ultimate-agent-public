"""Wallet-job queries that timed out every run, and the tape pass that overwrote paid grades.

MEASURED on the live box 2026-09-29, which has never been ANALYZEd:

* ``grade.build_evidence``'s ``SUM(side)`` count planned as the ``(chain)`` prefix of the
  swaps autoindex and walked ~5.5M sol entries per wallet -- about two minutes a wallet
  under load. ``wallet_regrade`` (300 s) and the grading half of ``wallet_buyers`` (600 s)
  timed out on every run for days. ``tracker.screen_wallet`` had the same query.
* ``discover._feed_tags`` planned on ``idx_events_kind`` and read every ``wallet.trade``
  event (~2M) per chunk, so ``tracker_cohorts`` spent its whole budget on bsc and deferred
  sol and robinhood on every run.
* The tape pass decided "is this wallet's grade a paid full-history one?" from a snapshot
  taken when the pass began. A sol pass runs for hours; every paid grade written in the
  meantime was overwritten by a tape grade (6 of 6 on 09-28, one B turned D).

Plans are checked against the SQL the code actually runs, captured with a trace callback,
on the real schema. Without statistics the planner's choice depends only on the schema,
so this is the plan production gets.
"""

from __future__ import annotations

import sqlite3

from kaiba.core.schemas import Chain
from kaiba.intelligence import discover
from kaiba.intelligence import grade as GR
from kaiba.learning import exit_study as ES

SOL_WALLET = "CJF7MNqb9xv1XrTs5St1Du5JuQXLsfFBB137vmYRKpnb"


def _plans(conn: sqlite3.Connection, statements: list[str], needle: str) -> list[str]:
    hits = [s for s in statements if needle in s and not s.lstrip().upper().startswith("EXPLAIN")]
    assert hits, f"no statement containing {needle!r} was run"
    return [
        " | ".join(str(r[3]) for r in conn.execute(f"EXPLAIN QUERY PLAN {s}"))
        for s in hits
    ]


def test_the_grader_counts_a_wallets_sides_by_seeking_its_rows(tmp_db):
    seen: list[str] = []
    tmp_db.set_trace_callback(seen.append)
    try:
        GR.build_evidence(SOL_WALLET, Chain.SOL, tmp_db)
    finally:
        tmp_db.set_trace_callback(None)
    for plan in _plans(tmp_db, seen, "SUM(side = 'buy')"):
        assert "idx_swaps_wallet" in plan and "wallet=?" in plan, plan


def test_feed_tags_seeks_subjects_instead_of_reading_every_trade_event(tmp_db):
    seen: list[str] = []
    tmp_db.set_trace_callback(seen.append)
    try:
        discover._feed_tags(tmp_db, Chain.SOL, ["a" * 44, "b" * 44])
    finally:
        tmp_db.set_trace_callback(None)
    for plan in _plans(tmp_db, seen, "SELECT subject, payload FROM events"):
        assert "idx_events_subj" in plan, plan
        assert "idx_events_kind" not in plan, plan


def _score(address: str, grade: GR.Grade, model: str, at: int) -> GR.WalletScore:
    return GR.WalletScore(
        chain=Chain.SOL, address=address, score=41.0, grade=grade, evidence_weight=0.8,
        archetype=GR.Archetype.TRADER, model_version=model, scored_at_ms=at,
    )


def test_a_paid_grade_written_during_a_tape_pass_is_not_overwritten(tmp_db):
    paid = _score("W1" * 20, GR.Grade.B, GR.MODEL_ID, 1_790_000_000_000)
    GR.store_scores([paid], tmp_db)
    history_before = tmp_db.execute("SELECT COUNT(*) FROM wallet_score_history").fetchone()[0]

    tape = [
        _score("W1" * 20, GR.Grade.D, GR.MODEL_ID_TAPE, 1_790_000_100_000),  # the paid one
        _score("W2" * 20, GR.Grade.C, GR.MODEL_ID_TAPE, 1_790_000_100_000),  # a new one
    ]
    stored, failed = GR.store_scores(tape, tmp_db, protect_full=True)

    row = tmp_db.execute(
        "SELECT grade, model_version FROM wallet_scores WHERE address=?", ("W1" * 20,)
    ).fetchone()
    assert (row[0], row[1]) == ("B", GR.MODEL_ID), "the paid grade was replaced by a tape grade"
    assert (stored, failed) == (1, 0)
    history_after = tmp_db.execute("SELECT COUNT(*) FROM wallet_score_history").fetchone()[0]
    assert history_after == history_before + 1, "a refused write still appended history"


def test_an_operator_can_still_force_the_overwrite(tmp_db):
    GR.store_scores([_score("W1" * 20, GR.Grade.B, GR.MODEL_ID, 1)], tmp_db)
    GR.store_scores([_score("W1" * 20, GR.Grade.D, GR.MODEL_ID_TAPE, 2)], tmp_db)
    row = tmp_db.execute("SELECT grade FROM wallet_scores WHERE address=?", ("W1" * 20,)).fetchone()
    assert row[0] == "D"


def test_exit_study_seeks_each_tokens_prints_instead_of_walking_every_swap(tmp_db):
    # MEASURED 2026-09-29: three price-path queries omitted ``chain``, so the (chain, token,
    # ts_ms) index could not be used and each of ~400 positions walked the whole table --
    # 744-894 s a run against a 900 s timeout.
    t0 = 1_790_000_000_000
    tmp_db.execute(
        "INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms, closed_ms, "
        "cost_native, proceeds_native, entry_price_usd, peak_price_usd) "
        "VALUES ('p1','sol','TOK','sm-trenches','live',?,?,'1','1','0.001','0.002')",
        (t0, t0 + 60_000),
    )
    tmp_db.execute(
        "INSERT INTO decisions (decision_id, ts_ms, lane, mode, chain, token, action) "
        "VALUES ('d1',?,'sm-trenches','live','sol','TOK','enter')",
        (t0,),
    )
    seen: list[str] = []
    tmp_db.set_trace_callback(seen.append)
    try:
        ES.load_paths(tmp_db)
        ES.load_decision_paths(tmp_db)
        ES.excursions(tmp_db)
    finally:
        tmp_db.set_trace_callback(None)
    plans = _plans(tmp_db, seen, "price_usd FROM swaps")
    assert len(plans) >= 3
    for plan in plans:
        assert "idx_swaps_token" in plan and "chain=? AND token=?" in plan, plan
