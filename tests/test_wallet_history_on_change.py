"""wallet_score_history is written only when a grade moved.

MEASURED on the live box 2026-10-01: ~1.41M history rows a day, one per wallet per regrade
whether or not anything changed; of the last 2,000 rows only 52 differed from the wallet's
previous row. The rule (grade._history_moved): write when the letter changed, the
model_version changed, or the score moved at least HISTORY_MIN_SCORE_DELTA against the
wallet's LATEST HISTORY ROW. wallet_scores (the current grade) is upserted every time.
"""

from __future__ import annotations

import random

from kaiba.core.schemas import Chain
from kaiba.intelligence import grade as GR
from kaiba.ops import retention as RT

W = "Bnd5oBSWpPpXoaWTckgXTVx9TCKkX5GyVqxcrwBzUYsB"
T0 = 1_790_000_000_000


def _score(score: float, grade: GR.Grade = GR.Grade.C, *, at: int, model: str = GR.MODEL_ID,
           address: str = W) -> GR.WalletScore:
    return GR.WalletScore(
        chain=Chain.SOL, address=address, score=score, grade=grade, evidence_weight=0.8,
        archetype=GR.Archetype.TRADER, model_version=model, scored_at_ms=at,
    )


def _history(conn, address: str = W) -> list[tuple[str, float, str]]:
    return [
        (r[0], r[1], r[2])
        for r in conn.execute(
            "SELECT grade, score, model_version FROM wallet_score_history WHERE address=? "
            "ORDER BY scored_at_ms, id", (address,),
        )
    ]


def _current(conn, address: str = W) -> tuple[str, float, int]:
    row = conn.execute(
        "SELECT grade, score, scored_at_ms FROM wallet_scores WHERE address=?", (address,)
    ).fetchone()
    return (row[0], row[1], row[2])


def test_an_unchanged_regrade_updates_the_current_grade_and_writes_no_history(tmp_db):
    GR.store_score(_score(30.0, at=T0), tmp_db)
    GR.store_score(_score(30.4, at=T0 + 1), tmp_db)
    GR.store_score(_score(29.7, at=T0 + 2), tmp_db)
    assert _history(tmp_db) == [("C", 30.0, GR.MODEL_ID)]
    assert _current(tmp_db) == ("C", 29.7, T0 + 2), "wallet_scores must still be upserted"


def test_a_grade_change_a_model_change_and_a_one_point_move_each_write(tmp_db):
    GR.store_score(_score(30.0, at=T0), tmp_db)
    GR.store_score(_score(30.0, GR.Grade.B, at=T0 + 1), tmp_db)           # letter
    GR.store_score(_score(30.0, GR.Grade.B, at=T0 + 2, model=GR.MODEL_ID_TAPE), tmp_db)  # model
    GR.store_score(_score(31.0, GR.Grade.B, at=T0 + 3, model=GR.MODEL_ID_TAPE), tmp_db)  # exactly 1.0
    GR.store_score(_score(31.99, GR.Grade.B, at=T0 + 4, model=GR.MODEL_ID_TAPE), tmp_db)  # 0.99: no
    assert _history(tmp_db) == [
        ("C", 30.0, GR.MODEL_ID),
        ("B", 30.0, GR.MODEL_ID),
        ("B", 30.0, GR.MODEL_ID_TAPE),
        ("B", 31.0, GR.MODEL_ID_TAPE),
    ]


def test_a_slow_drift_lands_once_it_adds_up_to_a_point(tmp_db):
    # Compared with the latest WRITTEN row, not the previous regrade: 0.3 a step is never
    # a point against the step before, but it is against the row we kept.
    for i, s in enumerate((40.0, 40.3, 40.6, 40.9, 41.2, 41.5)):
        GR.store_score(_score(s, at=T0 + i), tmp_db)
    assert [h[1] for h in _history(tmp_db)] == [40.0, 41.2]


def test_one_batch_regrading_a_wallet_twice_sees_its_own_first_row(tmp_db):
    stored, failed = GR.store_scores(
        [_score(30.0, at=T0), _score(30.2, at=T0 + 1), _score(50.0, GR.Grade.B, at=T0 + 2)], tmp_db
    )
    assert (stored, failed) == (3, 0), "stored counts current-grade writes, history or not"
    assert _history(tmp_db) == [("C", 30.0, GR.MODEL_ID), ("B", 50.0, GR.MODEL_ID)]


def test_wallets_do_not_share_a_history(tmp_db):
    GR.store_score(_score(30.0, at=T0), tmp_db)
    GR.store_score(_score(30.0, at=T0 + 1, address="X" * 44), tmp_db)
    assert _history(tmp_db, "X" * 44) == [("C", 30.0, GR.MODEL_ID)]


def test_the_latest_row_lookup_is_one_index_seek_with_no_sort(tmp_db):
    seen: list[str] = []
    tmp_db.set_trace_callback(seen.append)
    try:
        GR.store_score(_score(30.0, at=T0), tmp_db)
    finally:
        tmp_db.set_trace_callback(None)
    lookups = [s for s in seen if s.lstrip().startswith("SELECT") and "wallet_score_history" in s]
    assert len(lookups) == 1, lookups
    plan = " | ".join(str(r[3]) for r in tmp_db.execute(f"EXPLAIN QUERY PLAN {lookups[0]}"))
    assert "idx_wsh" in plan and "chain=? AND address=?" in plan, plan
    assert "TEMP B-TREE" not in plan, plan


def test_the_write_rule_and_the_prune_rule_agree(tmp_db):
    """What the grader writes now is exactly what retention keeps of the old full history
    (plus each wallet's latest row, which retention also keeps)."""
    assert RT.HISTORY_MIN_SCORE_DELTA == GR.HISTORY_MIN_SCORE_DELTA
    rng = random.Random(7)
    grades = [GR.Grade.B, GR.Grade.C, GR.Grade.D]
    for w in range(30):
        address = f"W{w:03d}" + "x" * 40
        score, grade, model = 30.0, GR.Grade.C, GR.MODEL_ID
        full: list[tuple[int, str, str, int, str, float, str]] = []
        for i in range(25):
            score += rng.uniform(-0.7, 0.7)
            if rng.random() < 0.1:
                grade = rng.choice(grades)
            if rng.random() < 0.04:
                model = GR.MODEL_ID_TAPE if model == GR.MODEL_ID else GR.MODEL_ID
            GR.store_score(_score(score, grade, at=T0 + i, model=model, address=address), tmp_db)
            full.append((i, "sol", address, T0 + i, grade.value, score, model))
        dropped = set(RT.deletable_history_ids(full, cutoff_ms=T0 + 10_000))
        kept = [(r[4], r[5], r[6]) for r in full if r[0] not in dropped]
        written = _history(tmp_db, address)
        last = (full[-1][4], full[-1][5], full[-1][6])
        # Retention also keeps each wallet's latest row; the writer has no reason to.
        assert kept[: len(written)] == written, address
        assert kept[len(written):] in ([], [last]), address
    total = tmp_db.execute("SELECT count(*) FROM wallet_score_history").fetchone()[0]
    assert total < 30 * 25 / 2, f"the rule should drop most of a slow random walk, kept {total}"
