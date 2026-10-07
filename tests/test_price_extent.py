"""The price extent: first print, numeric peak and print count, folded from a swaps cursor.

It replaced two whole-chain ``GROUP BY token`` walks of ``swaps`` per chain every 30
minutes in ``deployer.refresh`` (197 s mean on the box, 5 of 28 runs past the 300 s
timeout) and one every 4 hours in ``seeds.seed_tokens``. The properties pinned here:

* the peak is a NUMERIC maximum -- ``MAX(price_usd)`` over the TEXT column was the string
  maximum, wrong for 35 of 178 tokens on the box's newest 20,000 swaps;
* a run reads only the swaps inserted since the last one, and folding in pieces gives the
  same table as folding at once, including a late backfill of an older print;
* two callers cannot fold the same rows twice;
* a half-folded table is never read as an answer.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

import pytest

from kaiba.core.schemas import Chain
from kaiba.intelligence import deployer as D
from kaiba.intelligence import price_extent as PE

NOW = int(time.time() * 1000)
DEV = "NMgfqcn6BoS1k6yZoYfureFGPCdev1111111111111111"
_seq = iter(range(10**9))


def swap(conn, token: str, price: Any, ts_ms: int, *, chain: str = "sol") -> None:
    conn.execute(
        "INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, amount_token, price_usd, "
        "usd_value, source) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (chain, f"tx{next(_seq)}", ts_ms, "w", token, "buy", "1", price, "10", "test"),
    )
    conn.commit()


def extent(conn, token: str, chain: str = "sol") -> PE.Extent:
    ext = PE.get(conn, chain, token)
    assert ext is not None, f"no extent row for {token}"
    return ext


# ------------------------------------------------------------------ the fold


def test_the_peak_is_the_numeric_maximum_not_the_string_maximum(tmp_db):
    """'9.1e-05' sorts above '0.0005' as text. The peak is 0.0005."""
    for px in ("0.0001", "9.1e-05", "0.0005", "0.0002"):
        swap(tmp_db, "TokA", px, NOW - 60_000)
    PE.advance(tmp_db)
    ext = extent(tmp_db, "TokA")
    assert ext.peak_px == pytest.approx(0.0005)
    assert ext.multiple == pytest.approx(5.0)
    # The legacy rule keeps the string maximum the old SQL produced, so it can be chosen.
    assert ext.peak_text == "9.1e-05"
    assert ext.multiple_for(PE.PEAK_LEGACY_TEXT) == pytest.approx(0.91)
    # The trap itself, so this test cannot pass against a column that sorts numerically.
    assert tmp_db.execute(
        "SELECT MAX(price_usd) FROM swaps WHERE token='TokA'").fetchone()[0] == "9.1e-05"


def test_first_is_the_earliest_priced_print_and_bad_prices_are_not_prints(tmp_db):
    swap(tmp_db, "TokA", "", NOW - 90_000)          # unparseable: not a print
    swap(tmp_db, "TokA", "0", NOW - 80_000)         # zero: not a print
    swap(tmp_db, "TokA", None, NOW - 70_000)        # null: not a print
    swap(tmp_db, "TokA", "nan", NOW - 65_000)       # not finite: not a print
    swap(tmp_db, "TokA", "2.0", NOW - 50_000)
    swap(tmp_db, "TokA", "1.0", NOW - 60_000)       # inserted later, but EARLIER in time
    swap(tmp_db, "TokA", "3.0", NOW - 40_000)
    PE.advance(tmp_db)
    ext = extent(tmp_db, "TokA")
    assert (ext.first_ts_ms, ext.first_px) == (NOW - 60_000, 1.0)
    assert ext.peak_px == 3.0 and ext.prints == 3 and ext.last_ts_ms == NOW - 40_000


def test_a_run_reads_only_what_was_inserted_since_the_last_one(tmp_db):
    for i in range(10):
        swap(tmp_db, "TokA", str(1 + i), NOW - 100_000 + i)
    first = PE.advance(tmp_db)
    assert first.rows_read == 10 and first.caught_up
    again = PE.advance(tmp_db)
    assert again.rows_read == 0 and again.batches == 0 and again.caught_up
    swap(tmp_db, "TokA", "50", NOW - 1_000)
    later = PE.advance(tmp_db)
    assert later.rows_read == 1
    assert extent(tmp_db, "TokA").prints == 11 and extent(tmp_db, "TokA").peak_px == 50.0


def test_folding_in_pieces_equals_folding_at_once_including_a_late_backfill(tmp_db):
    """min / max / sum are order-free, so a backfilled OLD print lands wherever it arrives."""
    import sqlite3

    rows = [("TokA", "1.0", NOW - 50_000), ("TokB", "4.0", NOW - 49_000),
            ("TokA", "7.0", NOW - 40_000), ("TokB", "2.0", NOW - 30_000),
            ("TokA", "0.5", NOW - 90_000),  # backfill: older than everything, lands last
            ("TokB", "9.0", NOW - 10_000)]
    for tok, px, ts in rows:
        swap(tmp_db, tok, px, ts)
    for _ in range(10):  # one row per batch
        PE.advance(tmp_db, batch_rows=1)
    pieces = {t: extent(tmp_db, t) for t in ("TokA", "TokB")}

    tmp_db.execute(f"DELETE FROM {PE.TABLE}")
    tmp_db.execute("DELETE FROM kv WHERE key = ?", (PE.KV_CURSOR,))
    tmp_db.commit()
    PE.advance(tmp_db)
    whole = {t: extent(tmp_db, t) for t in ("TokA", "TokB")}
    assert pieces == whole
    assert (whole["TokA"].first_ts_ms, whole["TokA"].first_px) == (NOW - 90_000, 0.5)
    assert whole["TokA"].peak_px == 7.0 and whole["TokB"].prints == 3
    assert isinstance(tmp_db, sqlite3.Connection)


def test_the_deadline_stops_between_batches_and_the_cursor_resumes(tmp_db):
    for i in range(6):
        swap(tmp_db, "TokA", str(1 + i), NOW - 100_000 + i)
    ticks = iter([0.0, 0.0, 5.0, 5.0, 5.0])
    rep = PE.advance(tmp_db, batch_rows=2, deadline=1.0, clock=lambda: next(ticks))
    assert rep.batches == 1 and rep.rows_read == 2 and not rep.caught_up
    assert PE.read_cursor(tmp_db) == rep.cursor_to
    rest = PE.advance(tmp_db, batch_rows=2)
    assert rest.caught_up and rest.rows_read == 4
    assert extent(tmp_db, "TokA").prints == 6, "a batch was folded twice or skipped"


def test_two_callers_cannot_fold_the_same_rows_twice(tmp_db, monkeypatch):
    """The cursor is re-read inside the write transaction; the loser writes nothing."""
    for i in range(4):
        swap(tmp_db, "TokA", str(1 + i), NOW - 100_000 + i)
    real_fold = PE.fold
    raced = {"done": False}

    def fold_while_another_caller_finishes_first(rows):
        if not raced["done"]:
            raced["done"] = True
            PE.advance(tmp_db)  # the other job folds the same range and moves the cursor
        return real_fold(rows)

    monkeypatch.setattr(PE, "fold", fold_while_another_caller_finishes_first)
    rep = PE.advance(tmp_db)
    assert rep.lost_races == 1
    assert extent(tmp_db, "TokA").prints == 4, "the same rows were counted twice"


def test_ran_is_peak_over_first_with_a_print_floor(tmp_db):
    for px in ["1.0"] * 7 + ["5.0"]:
        swap(tmp_db, "Ran", px, NOW - 60_000)
    for px in ["1.0"] * 6 + ["50.0"]:              # 7 prints: below the floor
        swap(tmp_db, "Thin", px, NOW - 60_000)
    for px in ["1.0"] * 7 + ["4.9"]:
        swap(tmp_db, "Short", px, NOW - 60_000)
    PE.advance(tmp_db)
    assert PE.ran(tmp_db, "sol", min_prints=8, multiple=5.0) == {"Ran"}


# ------------------------------------------------------------------ deployer on the extent


def launch(conn, token: str, prices: list[str], *, first_seen_ms: int, creator: str = DEV) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO tokens (chain, address, symbol, decimals, creator, first_seen_ms) "
        "VALUES ('sol',?,?,?,?,?)",
        (token, "T", 9, creator, first_seen_ms),
    )
    conn.commit()
    for i, px in enumerate(prices):
        swap(conn, token, px, first_seen_ms + i * 1000)


def test_the_shipped_peak_rules():
    """Correcting the peak moves sizing labels and grader points: the lead's call, measured.

    MEASURED 2026-10-02: numeric moves 177 of 1,740 sol mid/spam deployers out of a charged
    bucket and adds 197 seeds to 335. Flipping either constant must be a diff, not a drift.

    2026-10-04, the lead's call: deployer records go NUMERIC on one price source (the text max
    had made the live snipe's runner labels noise: O/E 0.94/0.96 this week, vs sol mid/runner
    1.26 corrected). Seed grading stays legacy until it is measured on its own.
    """
    from kaiba.intelligence import seeds

    assert D.PEAK_RULE == PE.PEAK_NUMERIC
    assert seeds.SEED_PEAK_RULE == PE.PEAK_LEGACY_TEXT


@pytest.mark.parametrize(("rule", "record", "best"), [
    (PE.PEAK_LEGACY_TEXT, "all_dud", Decimal("1")),     # the scan's answer (0.9x; TokB's 1.0 is best)
    (PE.PEAK_NUMERIC, "runner", Decimal("2.5")),        # what the launch actually did
])
def test_a_runner_written_in_scientific_notation(tmp_db, monkeypatch, rule, record, best):
    """THE BUG: the old string MAX read this 2.5x launch as a 0.9x dud.

    Prices 1e-05 ... then 9e-06 and 2.5e-05. As text '9e-06' is the maximum, so the
    scan scored peak/first = 0.9 and the deployer read ``all_dud`` -- the bucket the sizer
    cuts to 0.70x / 0.85x. Numerically the launch ran 2.5x.
    """
    monkeypatch.setattr(D, "PEAK_RULE", rule)
    launch(tmp_db, "TokA", ["0.00001"] * 6 + ["9e-06", "0.000025"],
           first_seen_ms=NOW - 7_200_000)
    launch(tmp_db, "TokB", ["1.0"] * 8, first_seen_ms=NOW - 3_600_000)
    D.refresh(tmp_db, Chain.SOL, now_ms=NOW)
    rec = D.lookup(tmp_db, Chain.SOL, "TokB", now_ms=NOW)
    assert rec.prior_scored == 1 and rec.record == record, rec
    assert rec.prior_best_multiple == best


def test_a_token_first_priced_before_the_window_is_not_an_outcome_of_it(tmp_db):
    """The scan scored it from a MID-LIFE price seven days in; its launch is outside."""
    old = NOW - 8 * 86_400_000
    launch(tmp_db, "Old", ["1.0"] * 8, first_seen_ms=old)
    for i, px in enumerate(["1.0"] * 7 + ["3.0"]):   # still trading inside the window
        swap(tmp_db, "Old", px, NOW - 3_600_000 + i * 1000)
    launch(tmp_db, "New", ["1.0"] * 8, first_seen_ms=NOW - 600_000)
    D.refresh(tmp_db, Chain.SOL, now_ms=NOW)
    rec = D.lookup(tmp_db, Chain.SOL, "New", now_ms=NOW)
    assert rec.prior_scored == 0, "a launch from before the window was scored in it"


def test_refresh_never_walks_the_whole_tape(tmp_db):
    """The plan on the box bounded the old GROUP BY by chain only: every swap ever."""
    launch(tmp_db, "TokA", ["1.0"] * 8, first_seen_ms=NOW - 3_600_000)
    statements: list[str] = []
    tmp_db.set_trace_callback(statements.append)
    try:
        D.refresh(tmp_db, Chain.SOL, now_ms=NOW)
        D.refresh(tmp_db, Chain.SOL, now_ms=NOW)
    finally:
        tmp_db.set_trace_callback(None)
    swap_reads = [s for s in statements if "FROM swaps" in s]
    assert swap_reads, "the fold never read the tape"
    for sql in swap_reads:
        assert "GROUP BY" not in sql, sql
        assert "id > " in sql or "MAX(id)" in sql, f"a swaps read not bounded by the cursor: {sql}"


def test_refresh_refuses_a_half_folded_extent(tmp_db):
    """Mid-bootstrap the NEWEST swaps land last: a record written now would be wrong."""
    launch(tmp_db, "TokA", ["1.0"] * 8, first_seen_ms=NOW - 3_600_000)
    with pytest.raises(D.ExtentNotReady):
        D.refresh(tmp_db, Chain.SOL, now_ms=NOW, deadline=0.0)
    assert tmp_db.execute("SELECT COUNT(*) FROM deployer_stats").fetchone()[0] == 0


@pytest.mark.parametrize(("rule", "runners"), [(PE.PEAK_LEGACY_TEXT, 0), (PE.PEAK_NUMERIC, 1)])
def test_lookup_subtracts_what_refresh_added_from_the_same_row(tmp_db, monkeypatch, rule, runners):
    """The judged token's own outcome comes back out of its record -- under the SAME rule."""
    monkeypatch.setattr(D, "PEAK_RULE", rule)
    launch(tmp_db, "TokA", ["0.00001"] * 6 + ["9e-06", "0.000025"],
           first_seen_ms=NOW - 3_600_000)
    D.refresh(tmp_db, Chain.SOL, now_ms=NOW)
    row = tmp_db.execute("SELECT scored, runners FROM deployer_stats").fetchone()
    assert tuple(row) == (1, runners)
    rec = D.lookup(tmp_db, Chain.SOL, "TokA", now_ms=NOW)
    assert rec.prior_scored == 0 and rec.prior_runners == 0, "its own runner leaked back in"


def test_a_token_from_before_the_window_is_not_taken_back_out_either(tmp_db):
    """It was never added, so judging it must not subtract it from the record."""
    old = NOW - 8 * 86_400_000
    launch(tmp_db, "Old", ["1.0"] * 7 + ["3.0"], first_seen_ms=old)          # a runner, outside
    launch(tmp_db, "Mid", ["1.0"] * 7 + ["3.0"], first_seen_ms=NOW - 7_200_000)  # a runner, inside
    D.refresh(tmp_db, Chain.SOL, now_ms=NOW)
    rec = D.lookup(tmp_db, Chain.SOL, "Old", now_ms=NOW)
    assert (rec.prior_scored, rec.prior_runners) == (1, 1), rec


def test_records_are_written_in_bounded_transactions(tmp_db, monkeypatch):
    monkeypatch.setattr(D, "WRITE_BATCH", 2)
    for i in range(5):
        launch(tmp_db, f"Tok{i}", ["1.0"] * 8, first_seen_ms=NOW - 3_600_000, creator=f"dev{i}")
    commits: list[str] = []
    tmp_db.set_trace_callback(lambda s: commits.append(s) if s.strip().upper() == "COMMIT" else None)
    try:
        assert D.refresh(tmp_db, Chain.SOL, now_ms=NOW) == 5
    finally:
        tmp_db.set_trace_callback(None)
    assert len(commits) >= 3, "five rows at two per transaction is three commits"
    assert tmp_db.execute("SELECT COUNT(*) FROM deployer_stats").fetchone()[0] == 5


# ------------------------------------------------------------------ the scheduled job


def _ctx(conn, params: dict[str, Any], *, timeout_s: int = 300):
    from kaiba.core.schemas import now_ms
    from kaiba.ops import scheduler as S

    now = now_ms()
    return S.JobContext("deployer_stats", conn, params, S.ScheduleConfig(), now,
                        now + timeout_s * 1000)


def test_the_job_fails_loudly_while_the_extent_is_bootstrapping(tmp_db):
    from kaiba.ops import scheduler as S

    launch(tmp_db, "TokA", ["1.0"] * 8, first_seen_ms=NOW - 3_600_000)
    # A deadline already inside the 30 s safety margin: the fold cannot start.
    with pytest.raises(S.JobFailed) as err:
        S.job_deployer_stats(_ctx(tmp_db, {"chains": "sol"}, timeout_s=10))
    assert "price extent" in str(err.value)
    assert err.value.result["price_extent"]["caught_up"] is False
    D.ensure_table(tmp_db)
    assert tmp_db.execute("SELECT COUNT(*) FROM deployer_stats").fetchone()[0] == 0

    out = S.job_deployer_stats(_ctx(tmp_db, {"chains": "sol,bsc,robinhood"}))
    assert out["deployers_written"] == {"sol": 1, "bsc": 0, "robinhood": 0}
    assert out["price_extent"]["caught_up"] is True and out["price_extent"]["rows_read"] == 8
