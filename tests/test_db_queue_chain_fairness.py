"""The tier-0 queue must not let one chain's burst starve another's scanning.

THE LIVE BUG, MEASURED on the box 2026-09-23. ``token.scanned`` per 30-minute bucket:

    bucket        sol   robinhood
    1.5h ago      149      40
    1.0h ago      147      73
    0.5h ago       55     213
    now            30     319

A burst of robinhood launches filled ``triage_decisions``; ``_db_queue_work`` ranks every
chain in ONE pool by ``(verdict, score, id)`` and keeps the global top n, so robinhood took
the whole budget and sol fell 80%. Sol was producing most of the signals, and signal output
collapsed with it -- 19-32 per 30 minutes for eight hours, then **1**. Nothing was wrong
with the lanes; they were never offered a sol token to evaluate.

THIS IS THE SAME BUG, IN A SECOND FEEDER. ``_smart_flow_work`` had it, was fixed on
2026-09-22 (``tests/test_smart_flow_chain_fairness.py``: robinhood won 0 of 528 qualifying
rows), and ``_db_queue_work`` was left ranking one pool. The fix here is deliberately the
same shape -- bucket by chain, take one from each in turn, strongest chain first -- so the
two feeders cannot drift apart again.

It costs nothing: same slice size, same passes, same providers. A chain with no queued work
contributes no bucket, so one busy chain still fills the whole slice.
"""

from __future__ import annotations

from kaiba.core.schemas import Chain, now_ms
from kaiba.execution import scanner

CFG = scanner.DEFAULT_CONFIG


def seed(conn, rows):
    """rows: (chain, token, score). Writes `triage_decisions` as the tier-0 pass does."""
    ts = now_ms()
    for i, (chain, token, score) in enumerate(rows):
        conn.execute(
            "INSERT INTO triage_decisions (chain, token, verdict, score, reasons_json, ts_ms) "
            "VALUES (?,?,?,?,?,?)",
            (chain, token, "promote", float(score), "[]", ts - i),
        )
    conn.commit()


def test_a_bursting_chain_does_not_take_every_slot(tmp_db):
    """THE LIVE BUG: robinhood 40 -> 319 scans while sol fell 149 -> 30."""
    seed(tmp_db, [("robinhood", f"0xrh{i:02d}" + "a" * 34, 90 - i) for i in range(10)]
         + [("sol", "solA" + "1" * 30, 50), ("sol", "solB" + "1" * 30, 49)])
    got = scanner._db_queue_work(tmp_db, 4, config=CFG)
    chains = {w.chain for w in got}
    assert Chain.SOL in chains, (
        "a chain with queued work got no slot: %s" % [w.chain.value for w in got]
    )


def test_every_queued_chain_appears_before_any_chain_repeats(tmp_db):
    seed(tmp_db, [
        ("sol", "solA" + "1" * 30, 99),
        ("sol", "solB" + "1" * 30, 98),
        ("sol", "solC" + "1" * 30, 97),
        ("bsc", "0xbsc" + "a" * 35, 50),
        ("robinhood", "0xrh" + "a" * 36, 10),
    ])
    got = scanner._db_queue_work(tmp_db, 3, config=CFG)
    seen = [w.chain.value for w in got]
    assert len(set(seen)) == 3, f"first three slots must cover three chains, got {seen}"


def test_ordering_within_a_chain_is_still_by_score(tmp_db):
    """Fairness decides WHICH chains are scanned; score still decides which token."""
    seed(tmp_db, [("sol", "solWEAK" + "1" * 27, 3), ("sol", "solSTRONG" + "1" * 25, 99)])
    got = scanner._db_queue_work(tmp_db, 1, config=CFG)
    assert got and "STRONG" in got[0].token, [w.token for w in got]


def test_the_strongest_chain_still_leads(tmp_db):
    seed(tmp_db, [("robinhood", "0xrh" + "a" * 36, 10), ("sol", "solA" + "1" * 30, 99)])
    got = scanner._db_queue_work(tmp_db, 2, config=CFG)
    assert got[0].chain is Chain.SOL, [w.chain.value for w in got]


def test_one_chain_alone_still_fills_the_whole_slice(tmp_db):
    """Fairness must never idle capacity when only one chain has queued work."""
    seed(tmp_db, [("sol", f"sol{i:02d}" + "1" * 28, 90 - i) for i in range(6)])
    got = scanner._db_queue_work(tmp_db, 4, config=CFG)
    assert len(got) == 4, [w.token for w in got]


def test_the_slice_size_is_still_respected(tmp_db):
    seed(tmp_db, [("sol", "solA" + "1" * 30, 9), ("bsc", "0xbsc" + "a" * 35, 8),
                  ("robinhood", "0xrh" + "a" * 36, 7)])
    assert len(scanner._db_queue_work(tmp_db, 2, config=CFG)) == 2


def test_nothing_queued_yields_nothing(tmp_db):
    assert scanner._db_queue_work(tmp_db, 4, config=CFG) == []


def test_a_short_bucket_is_skipped_in_later_rounds(tmp_db):
    """One chain with a single candidate must not stall the rounds for the others."""
    seed(tmp_db, [("robinhood", "0xrh" + "a" * 36, 99)]
         + [("sol", f"sol{i:02d}" + "1" * 28, 50 - i) for i in range(4)])
    got = scanner._db_queue_work(tmp_db, 5, config=CFG)
    assert len(got) == 5, [w.token for w in got]
    assert sum(1 for w in got if w.chain is Chain.SOL) == 4


def test_both_feeders_use_the_same_shape():
    """The two feeders drifted once already; keep them recognisably the same."""
    import inspect

    db_src = inspect.getsource(scanner._db_queue_work)
    flow_src = inspect.getsource(scanner._smart_flow_work)
    for marker in ("by_chain", "rank"):
        assert marker in db_src, f"_db_queue_work lost the interleave ({marker})"
        assert marker in flow_src, f"_smart_flow_work lost the interleave ({marker})"
