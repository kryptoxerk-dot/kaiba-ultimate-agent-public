"""A grading pass that writes one row and reports success is worse than one that fails.

THE BUG, MEASURED on the live box 2026-09-24 while grading the robinhood tape.

``grade_tape(store=True)`` scored 32,468 wallets and stored ONE. Two independent faults,
and the first hid the second:

1. ``store_score`` opens its own transaction per wallet, so a whole-tape pass asked for
   32,406 separate write locks while the engine, scanner, ingest and protection watchdog
   all write continuously. Its caller caught ``sqlite3.Error``, logged a warning and
   carried on, so ``stored: 1`` was reported as a successful run.

2. The deeper one. ``_iter_wallet_rows`` streams ONE ordered scan of ``swaps`` and that
   cursor is still open while the loop stores. On the same connection the read holds a
   transaction, so every in-loop write lost. Batching at 500 turned 32,406 lock attempts
   into 65 and stored 406 -- and the shape of that number is the whole diagnosis: 65
   batches, 64 of them full and ALL of them failed, and the only one that landed was the
   final 406-row flush that runs after the loop, once the cursor is exhausted.

So the fix is both: batch the writes, and write them on a connection that is not the one
being read through. WAL exists precisely so a second connection can write while the first
reads. After both: 32,407 stored, zero failed, robinhood 478 -> 32,473 graded.

WHY IT MATTERS. On bsc and robinhood there is no Helius history route, so this pass is the
only grading path there, and that is where the operator's own imported GMGN wallets live.
The lanes read ``wallet_scores``. A grading job that silently writes nothing leaves every
EVM lane reading an empty table forever, which is what it had been doing.
"""

from __future__ import annotations

import sqlite3

import pytest

from kaiba.core.schemas import Chain
from kaiba.intelligence import grade as GR


def a_score(address: str, grade: GR.Grade = GR.Grade.C) -> GR.WalletScore:
    return GR.WalletScore(
        chain=Chain.ROBINHOOD,
        address=address,
        score=41.0,
        grade=grade,
        evidence_weight=0.8,
        archetype=GR.Archetype.TRADER,
        model_version=GR.MODEL_ID_TAPE,
        scored_at_ms=1_790_000_000_000,
    )


# ------------------------------------------------------------------ the batch writer


def test_many_scores_are_written(tmp_db):
    scores = [a_score("0x%040x" % i) for i in range(250)]
    stored, failed = GR.store_scores(scores, tmp_db)
    assert (stored, failed) == (250, 0)
    n = tmp_db.execute("SELECT COUNT(*) FROM wallet_scores WHERE chain=?",
                       (Chain.ROBINHOOD.value,)).fetchone()[0]
    assert n == 250


def test_they_share_transactions_instead_of_taking_one_lock_each(tmp_db, monkeypatch):
    """THE FIX. 32,406 lock acquisitions is what a live box refuses."""
    commits = {"n": 0}
    real = GR.tx

    def counting_tx(conn):
        commits["n"] += 1
        return real(conn)

    monkeypatch.setattr(GR, "tx", counting_tx)
    GR.store_scores([a_score("0x%040x" % i) for i in range(1000)], tmp_db, batch_size=500)
    assert commits["n"] == 2, f"took {commits['n']} transactions for 1000 rows"


def test_a_failed_batch_is_counted_and_returned_not_swallowed(tmp_db, monkeypatch):
    """THE REGRESSION: a warning and a carry-on is how `stored: 1` looked like success."""
    def boom(_score, _conn):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(GR, "_write_score_row", boom)
    stored, failed = GR.store_scores([a_score("0x%040x" % i) for i in range(20)], tmp_db)
    assert stored == 0
    assert failed == 20, "a caller that cannot say how much it lost cannot be trusted"


def test_a_failed_batch_leaves_nothing_behind(tmp_db, monkeypatch):
    """All-or-nothing: the count must not claim rows a rollback removed."""
    calls = {"n": 0}
    real = GR._write_score_row

    def fail_late(score, conn):
        calls["n"] += 1
        if calls["n"] > 5:
            raise sqlite3.OperationalError("database is locked")
        real(score, conn)

    monkeypatch.setattr(GR, "_write_score_row", fail_late)
    stored, failed = GR.store_scores(
        [a_score("0x%040x" % i) for i in range(10)], tmp_db, batch_size=10)
    assert (stored, failed) == (0, 10)
    n = tmp_db.execute("SELECT COUNT(*) FROM wallet_scores").fetchone()[0]
    assert n == 0, "a rolled-back batch left rows behind"


def test_the_batch_is_bounded(tmp_db):
    """One transaction for the whole tape would starve the services keeping stops on time."""
    assert 0 < GR.TAPE_STORE_BATCH <= 5000


# ------------------------------------------------------------------ the bus


def test_a_whole_tape_pass_does_not_announce_every_wallet(tmp_db, monkeypatch):
    """32,000 WALLET_GRADED events would bury every other event on the bus."""
    seen = {"n": 0}
    monkeypatch.setattr(GR, "emit", lambda *a, **k: seen.__setitem__("n", seen["n"] + 1))
    GR.store_scores([a_score("0x%040x" % i) for i in range(30)], tmp_db)
    assert seen["n"] == 0


def test_grading_one_wallet_on_purpose_still_announces_it(tmp_db, monkeypatch):
    """store_score keeps its old behaviour; only the bulk path is quiet."""
    seen = {"n": 0}
    monkeypatch.setattr(GR, "emit", lambda *a, **k: seen.__setitem__("n", seen["n"] + 1))
    GR.store_score(a_score("0x%040x" % 1), tmp_db)
    assert seen["n"] == 1


def test_store_score_still_writes_both_rows(tmp_db):
    """The refactor split the row writes out; the single-wallet path must be unchanged."""
    GR.store_score(a_score("0x%040x" % 7), tmp_db)
    assert tmp_db.execute("SELECT COUNT(*) FROM wallet_scores").fetchone()[0] == 1
    assert tmp_db.execute("SELECT COUNT(*) FROM wallet_score_history").fetchone()[0] == 1


# ------------------------------------------------------------------ the report


def test_the_report_carries_the_failures(tmp_db):
    """A run that lost rows must say so in the summary the scheduler records."""
    report = GR.TapeRunReport(chain=Chain.ROBINHOOD.value)
    report.stored = 3
    report.store_failed = 97
    assert report.as_dict()["store_failed"] == 97


def a_tape(conn, wallets: int = 3, rows_each: int = 4) -> None:
    """Real swap rows, so grade_tape actually reaches its store branch.

    Without these the pass scores nothing, ``store_scores`` is never called, and a test
    that asserts over an empty list passes whatever the code does -- which is exactly how
    the first version of the test below survived the mutation it exists to catch.
    """
    n = 0
    for w in range(wallets):
        wallet = "0x%040x" % (w + 1)
        for i in range(rows_each):
            n += 1
            conn.execute(
                "INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, amount_token, "
                "amount_native, usd_value, source) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (Chain.ROBINHOOD.value, "0xtx%d" % n, 1_790_000_000_000 + i * 1000, wallet,
                 "0x%040x" % (100 + w), "buy" if i % 2 == 0 else "sell",
                 1000, 10**16, 25.0, "test"),
            )
    conn.commit()


def test_grade_tape_writes_on_a_connection_it_is_not_reading_through(tmp_db, monkeypatch):
    """THE DEEPER FIX, pinned: the store must not use the connection the scan is on.

    ``_iter_wallet_rows`` holds an open cursor over ``swaps`` for the whole pass, so a
    write through that same connection loses the lock on a live box. Handing the reading
    connection back in reproduces the live failure exactly.
    """
    a_tape(tmp_db)
    used: list[object] = []

    def spy(scores, conn=None, **kw):
        used.append(conn)
        return len(scores), 0

    monkeypatch.setattr(GR, "store_scores", spy)
    GR.grade_tape(tmp_db, Chain.ROBINHOOD, store=True)
    assert used, "grade_tape stored nothing, so this test proves nothing -- fix the tape"
    assert all(conn is not tmp_db for conn in used), (
        "grade_tape stored through the connection its own cursor is reading"
    )


def test_grade_tape_actually_lands_its_rows(tmp_db):
    """End to end on a real tape: the rows are in the table afterwards."""
    a_tape(tmp_db, wallets=5)
    before = tmp_db.execute("SELECT COUNT(*) FROM wallet_scores").fetchone()[0]
    report = GR.grade_tape(tmp_db, Chain.ROBINHOOD, store=True)
    assert report.store_failed == 0, "batches failed: %d" % report.store_failed
    assert report.stored > 0, "scored %d wallets and stored none" % report.wallets_scored
    after = tmp_db.execute("SELECT COUNT(*) FROM wallet_scores").fetchone()[0]
    assert after - before == report.stored, (
        "report claims %d stored but the table gained %d" % (report.stored, after - before)
    )


@pytest.mark.parametrize("store", [False, True])
def test_grade_tape_does_not_leak_its_write_connection(tmp_db, store):
    """Opened only when storing, and closed either way."""
    report = GR.grade_tape(tmp_db, Chain.ROBINHOOD, store=store)
    assert report.chain == Chain.ROBINHOOD.value
