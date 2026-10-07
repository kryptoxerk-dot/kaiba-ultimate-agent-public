"""A price series is read from ONE swap source, and a peak is a NUMERIC maximum.

``swaps`` mixes feeds -- ``pumpfun:trades``, ``gmgn:smartmoney``, ``gmgn:kol``,
``helius:backfill``, ``alchemy:ws``, the Robinhood poller -- and they disagree print by
print. MEASURED 2026-10-03 on sol: ``gmgn:smartmoney`` printed 5.93 where
``pumpfun:trades`` printed 0.0000064 for the same token seconds apart. A maximum over the
mixed tape takes the other feed's high misprint as the peak, and a first price taken from
it takes the other feed's low one; either way a flat token "runs". MEASURED 2026-10-04 on
the box's newest 1M swaps: 27.6% of sol tokens with eight prints reach 2x on the mixed
series against 20.0% on one source.

Every fixture below mixes two feeds whose scales disagree, and every assertion fails if the
reader goes back to reading them together. Three readers share the rule
(``deployer.series_source``): the deployer record that picks LIVE snipe triggers, the
mooner autopsy, and the outcomes test bench.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

import pytest

from kaiba.core.schemas import Chain
from kaiba.intelligence import deployer as D
from kaiba.intelligence import price_extent as PE
from kaiba.learning import mooner, outcomes
from kaiba.learning.outcomes import EntryFill, SourceKind

NOW = int(time.time() * 1000)
DEV = "NMgfqcn6BoS1k6yZoYfureFGPCdev1111111111111111"
TAPE = "pumpfun:trades"
FEED = "gmgn:smartmoney"
_seq = iter(range(10**9))


def swap(conn, token: str, price: Any, ts_ms: int, *, source: str = TAPE, chain: str = "sol",
         wallet: str = "w", side: str = "buy") -> None:
    conn.execute(
        "INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, amount_token, amount_native, "
        "price_usd, usd_value, source) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (chain, f"tx{next(_seq)}", ts_ms, wallet, token, side, "1", "1000000", str(price), "10",
         source),
    )
    conn.commit()


def token(conn, address: str, *, first_seen_ms: int, creator: str = DEV, chain: str = "sol") -> None:
    conn.execute(
        "INSERT OR REPLACE INTO tokens (chain, address, symbol, decimals, creator, first_seen_ms) "
        "VALUES (?,?,?,?,?,?)",
        (chain, address, "T", 6, creator, first_seen_ms),
    )
    conn.commit()


def tape(conn, address: str, prices: list[Any], start_ms: int, *, source: str = TAPE,
         step_ms: int = 1000) -> None:
    for i, px in enumerate(prices):
        swap(conn, address, px, start_ms + i * step_ms, source=source)


# ------------------------------------------------------------------ the shared rule


def test_the_series_source_is_the_one_with_the_most_prints():
    assert D.series_source({FEED: 3, TAPE: 9}, "sol") == TAPE
    assert D.series_source({FEED: 9, TAPE: 3}, "sol") == FEED


def test_a_tie_goes_to_the_per_token_tape_then_to_the_name():
    assert D.series_source({FEED: 5, TAPE: 5}, "sol") == TAPE
    assert D.series_source({"gmgn:smartmoney": 5, "robinhood": 5}, "robinhood") == "robinhood"
    assert D.series_source({"zeta": 2, "alpha": 2}, "sol") == "alpha"
    assert D.series_source({}, "sol") is None
    assert D.series_source({FEED: 0}, "sol") is None


def test_single_source_keeps_one_feed_in_order():
    rows = [(1, 1e-5, TAPE), (2, 5.93, FEED), (3, 1.1e-5, TAPE)]
    assert D.single_source(rows, "sol") == [(1, 1e-5), (3, 1.1e-5)]


# ------------------------------------------------------------------ the deployer record


def test_a_high_misprint_on_another_feed_does_not_make_a_runner(tmp_db):
    """THE BUG. A flat launch plus one GMGN print at the box's 5.93-vs-0.0000064 slip.

    Mixed, its peak is 5.93 over a first price of 0.0000064: a 926,000x "runner", and the
    next launch by the same deployer reads ``low/runner`` -- a LIVE snipe trigger.
    """
    born = NOW - 7_200_000
    token(tmp_db, "Flat", first_seen_ms=born)
    tape(tmp_db, "Flat", ["0.0000064"] * 10, born)
    swap(tmp_db, "Flat", "5.93", born + 3_000, source=FEED)
    token(tmp_db, "Next", first_seen_ms=NOW - 3_600_000)
    D.refresh(tmp_db, Chain.SOL, now_ms=NOW)
    rec = D.lookup(tmp_db, Chain.SOL, "Next", now_ms=NOW)
    assert (rec.prior_scored, rec.prior_runners) == (1, 0), rec
    assert rec.label == "low/all_dud"
    assert rec.prior_best_multiple == Decimal(1)


def test_a_low_misprint_on_another_feed_is_not_the_first_price(tmp_db):
    """The mirror image: the other feed's EARLIEST print is a tiny misprint."""
    born = NOW - 7_200_000
    token(tmp_db, "Flat", first_seen_ms=born)
    swap(tmp_db, "Flat", "6.4e-12", born, source=FEED)
    tape(tmp_db, "Flat", ["0.0000064"] * 10, born + 1_000)
    token(tmp_db, "Next", first_seen_ms=NOW - 3_600_000)
    D.refresh(tmp_db, Chain.SOL, now_ms=NOW)
    rec = D.lookup(tmp_db, Chain.SOL, "Next", now_ms=NOW)
    assert (rec.prior_scored, rec.prior_runners) == (1, 0), rec


def test_a_real_runner_on_its_own_feed_still_counts(tmp_db):
    """The fix must not throw away real runs: 1x -> 3x on the tape, a stray feed print."""
    born = NOW - 7_200_000
    token(tmp_db, "Run", first_seen_ms=born)
    tape(tmp_db, "Run", ["0.00001"] * 9 + ["0.00003"], born)
    swap(tmp_db, "Run", "0.000005", born + 2_000, source=FEED)
    token(tmp_db, "Next", first_seen_ms=NOW - 3_600_000)
    D.refresh(tmp_db, Chain.SOL, now_ms=NOW)
    rec = D.lookup(tmp_db, Chain.SOL, "Next", now_ms=NOW)
    assert (rec.prior_scored, rec.prior_runners) == (1, 1), rec
    assert rec.prior_best_multiple == Decimal(3)


def test_the_peak_is_numeric_and_best_multiple_is_its_numeric_best(tmp_db):
    """'9e-06' is the STRING maximum of these prices; the launch actually ran 2.5x."""
    born = NOW - 7_200_000
    token(tmp_db, "Sci", first_seen_ms=born)
    tape(tmp_db, "Sci", ["0.00001"] * 6 + ["9e-06", "0.000025"], born)
    token(tmp_db, "Next", first_seen_ms=NOW - 3_600_000)
    D.refresh(tmp_db, Chain.SOL, now_ms=NOW)
    rec = D.lookup(tmp_db, Chain.SOL, "Next", now_ms=NOW)
    assert rec.record == "runner", rec
    assert rec.prior_best_multiple == Decimal("2.5")
    stored = tmp_db.execute("SELECT best_multiple FROM deployer_stats").fetchone()[0]
    assert Decimal(stored) == Decimal("2.5")


def test_a_feed_that_started_covering_an_old_token_does_not_readmit_it(tmp_db):
    """Born 8 days ago on GMGN; the tape route started inside the window and shows a run.

    The window is about LAUNCHES. A token whose first print on any feed is outside it is
    not an outcome of it, whichever feed its series is later read from.
    """
    old = NOW - 8 * 86_400_000
    token(tmp_db, "Old", first_seen_ms=old)
    swap(tmp_db, "Old", "0.00001", old, source=FEED)
    tape(tmp_db, "Old", ["0.00001"] * 9 + ["0.00005"], NOW - 7_200_000)
    token(tmp_db, "Next", first_seen_ms=NOW - 3_600_000)
    D.refresh(tmp_db, Chain.SOL, now_ms=NOW)
    rec = D.lookup(tmp_db, Chain.SOL, "Next", now_ms=NOW)
    assert rec.prior_scored == 0, "a launch from before the window was scored in it"


def test_the_print_floor_applies_to_the_series_source(tmp_db):
    """Five tape prints plus five feed prints is not eight prints of one series."""
    born = NOW - 7_200_000
    token(tmp_db, "Thin", first_seen_ms=born)
    tape(tmp_db, "Thin", ["0.00001"] * 5, born)
    tape(tmp_db, "Thin", ["0.00001"] * 4 + ["0.00009"], born + 500, source=FEED)
    token(tmp_db, "Next", first_seen_ms=NOW - 3_600_000)
    D.refresh(tmp_db, Chain.SOL, now_ms=NOW)
    rec = D.lookup(tmp_db, Chain.SOL, "Next", now_ms=NOW)
    assert rec.prior_scored == 0, rec


def test_lookup_takes_back_exactly_the_single_source_outcome(tmp_db):
    """The judged token is a runner on its tape and a dud on the mixed series.

    refresh counts it as (1 scored, 1 runner). lookup must subtract the same runner, or the
    token's own run leaks into its own record -- the leakage the module exists to avoid.
    """
    born = NOW - 3_600_000
    token(tmp_db, "Self", first_seen_ms=born)
    swap(tmp_db, "Self", "1.0", born - 500, source=FEED)            # a high, EARLIEST misprint
    tape(tmp_db, "Self", ["0.00001"] * 9 + ["0.00003"], born)
    D.refresh(tmp_db, Chain.SOL, now_ms=NOW)
    row = tmp_db.execute("SELECT scored, runners FROM deployer_stats").fetchone()
    assert tuple(row) == (1, 1)
    rec = D.lookup(tmp_db, Chain.SOL, "Self", now_ms=NOW)
    assert (rec.prior_scored, rec.prior_runners) == (0, 0), "its own run leaked back in"


def test_the_source_extent_folds_in_pieces_like_at_once(tmp_db):
    """Same cursor contract as price_extent: a late backfill lands whenever it arrives."""
    born = NOW - 7_200_000

    def rows() -> dict[str, tuple[Any, ...]]:
        return {
            r[0]: tuple(r[1:])
            for r in tmp_db.execute(
                f"SELECT source, first_ts_ms, first_px, peak_px, prints FROM {D.SOURCE_TABLE}"
            )
        }

    tape(tmp_db, "Tok", ["0.00002", "0.00003"], born + 10_000)
    swap(tmp_db, "Tok", "0.000015", born + 5_000)                   # older, same batch
    D.advance_sources(tmp_db)
    assert rows()[TAPE] == (born + 5_000, 0.000015, 0.00003, 3)
    swap(tmp_db, "Tok", "0.00001", born)                            # older still, next run
    swap(tmp_db, "Tok", "5.93", born + 20_000, source=FEED)
    rep = D.advance_sources(tmp_db)
    assert rep.rows_read == 2 and rep.caught_up
    assert rows()[TAPE] == (born, 0.00001, 0.00003, 4)
    assert rows()[FEED] == (born + 20_000, 5.93, 5.93, 1)


def test_refresh_refuses_a_half_folded_source_extent(tmp_db):
    born = NOW - 3_600_000
    token(tmp_db, "Tok", first_seen_ms=born)
    tape(tmp_db, "Tok", ["0.00001"] * 8, born)
    with pytest.raises(D.ExtentNotReady):
        D.refresh(tmp_db, Chain.SOL, now_ms=NOW, deadline=0.0)
    assert tmp_db.execute("SELECT COUNT(*) FROM deployer_stats").fetchone()[0] == 0


def test_the_shipped_deployer_peak_rule_is_numeric():
    assert D.PEAK_RULE == PE.PEAK_NUMERIC


# ------------------------------------------------------------------ the mooner autopsy


def test_a_misprint_on_another_feed_is_not_a_mooner(tmp_db):
    """Flat on the tape; one GMGN print after the window at the 1e6 slip. Not a mooner."""
    tape(tmp_db, "FLAT", ["0.0000064"] * 16, 1_000_000)
    swap(tmp_db, "FLAT", "5.93", 1_000_000 + 12_500, source=FEED)
    cases = mooner.build_cases(tmp_db)
    assert len(cases) == 1
    case = cases[0]
    assert case.price_source == TAPE
    assert case.multiple == pytest.approx(1.0)
    assert not case.mooned


def test_a_low_misprint_closing_the_window_is_not_the_base(tmp_db):
    """The window's LAST print is the other feed's tiny misprint: mixed, everything after is 1e6x."""
    tape(tmp_db, "BASE", ["0.0000064"] * 9, 1_000_000)
    swap(tmp_db, "BASE", "6.4e-12", 1_000_000 + 9_000, source=FEED)
    tape(tmp_db, "BASE", ["0.0000064"] * 6, 1_000_000 + 10_000)
    case = mooner.build_cases(tmp_db)[0]
    assert case.window_end_price == pytest.approx(0.0000064)
    assert case.multiple == pytest.approx(1.0)


def test_a_real_run_after_the_window_still_moons(tmp_db):
    tape(tmp_db, "RUN", ["0.00001"] * 10 + ["0.00002", "0.00009", "0.00003", "0.00001"], 1_000_000)
    swap(tmp_db, "RUN", "0.0000001", 1_000_000 + 11_500, source=FEED)
    case = mooner.build_cases(tmp_db)[0]
    assert case.multiple == pytest.approx(9.0)
    assert case.mooned


def test_features_are_still_read_from_every_source(tmp_db):
    """Wallets are not prices: a GMGN-only early buyer is still an early buyer."""
    tape(tmp_db, "FEAT", ["0.00001"] * 16, 1_000_000)
    swap(tmp_db, "FEAT", "0.00001", 1_000_000 + 500, source=FEED, wallet="smart")
    case = mooner.build_cases(tmp_db)[0]
    assert "smart" in case.early_wallets


# ------------------------------------------------------------------ the outcomes bench


def test_the_priced_series_is_one_source(tmp_db):
    tape(tmp_db, "SER", ["0.00001"] * 5, 1_000_000)
    swap(tmp_db, "SER", "5.93", 1_000_000 + 2_500, source=FEED)
    series = outcomes._priced_series(tmp_db, "sol", "SER")
    assert [px for _ts, px in series] == [0.00001] * 5


def test_a_forward_return_is_not_made_of_two_scales(tmp_db):
    """A tape pseudo-entry on a flat token; a GMGN misprint is the last print before +20 min.

    Mixed, the 20-minute carry return is +92,656,150% (5.93 / 0.0000064). On one source it
    is flat.
    """
    t0 = 1_790_000_000_000
    for i in range(21):
        swap(tmp_db, "FWD", "0.0000064", t0 + i * 30_000)
    swap(tmp_db, "FWD", "5.93", t0 + 700_000, source=FEED)
    data = outcomes.build_dataset(tmp_db, sources=(SourceKind.TAPE,),
                                  entry_fill=EntryFill.NEXT_PRINT)
    assert len(data) == 1
    fwd = data.rows[0].forwards[20]
    assert fwd.gross_carry_pct == pytest.approx(0.0)
    assert max(f.gross_carry_pct or 0.0 for f in data.rows[0].forwards.values()) < 1.0


def test_pre_entry_price_features_read_one_source(tmp_db):
    """A GMGN misprint before the entry must not read as a 99.99% drawdown from the peak."""
    t0 = 1_790_000_000_000
    for i in range(40):
        swap(tmp_db, "PRE", "0.0000064", t0 + i * 30_000)
    swap(tmp_db, "PRE", "5.93", t0 + 15_000, source=FEED)
    data = outcomes.build_dataset(tmp_db, sources=(SourceKind.TAPE,),
                                  entry_fill=EntryFill.NEXT_PRINT)
    raw = data.rows[0].raw
    assert raw["pre_entry_drawdown_from_peak_pct"] == pytest.approx(0.0)
    assert raw["pre_entry_run_from_first_pct"] == pytest.approx(0.0)


def test_a_tape_pseudo_entry_starts_on_the_series_it_is_priced_from(tmp_db):
    """One early GMGN print, then the tape five minutes later.

    Keyed on the mixed first print, the pseudo-entry lands where its own series has no
    price yet and the row is dropped as ``no_price_before_entry``.
    """
    t0 = 1_790_000_000_000
    swap(tmp_db, "LATE", "0.0000064", t0, source=FEED)
    for i in range(30):
        swap(tmp_db, "LATE", "0.0000064", t0 + 300_000 + i * 30_000)
    data = outcomes.build_dataset(tmp_db, sources=(SourceKind.TAPE,),
                                  entry_fill=EntryFill.NEXT_PRINT)
    assert len(data) == 1, data.census.dropped
    assert data.rows[0].entry_ms == t0 + 300_000 + outcomes.PSEUDO_ENTRY_LAG_MS


def test_two_callers_cannot_fold_the_same_rows_twice(tmp_db, monkeypatch):
    """The source cursor is re-read inside the write transaction; the loser writes nothing."""
    for i in range(4):
        swap(tmp_db, "Tok", "0.00001", NOW - 100_000 + i)
    real_fold = D.fold_sources
    raced = {"done": False}

    def fold_while_another_caller_finishes_first(rows):
        if not raced["done"]:
            raced["done"] = True
            D.advance_sources(tmp_db)  # the other job folds the same range first
        return real_fold(rows)

    monkeypatch.setattr(D, "fold_sources", fold_while_another_caller_finishes_first)
    rep = D.advance_sources(tmp_db)
    assert rep.lost_races == 1
    prints = tmp_db.execute(f"SELECT prints FROM {D.SOURCE_TABLE}").fetchone()[0]
    assert prints == 4, "the same rows were counted twice"


def test_the_deadline_stops_between_batches_and_the_cursor_resumes(tmp_db):
    for i in range(6):
        swap(tmp_db, "Tok", "0.00001", NOW - 100_000 + i)
    ticks = iter([0.0, 0.0, 5.0, 5.0, 5.0])
    rep = D.advance_sources(tmp_db, batch_rows=2, deadline=1.0, clock=lambda: next(ticks))
    assert rep.batches == 1 and rep.rows_read == 2 and not rep.caught_up
    rest = D.advance_sources(tmp_db, batch_rows=2)
    assert rest.caught_up and rest.rows_read == 4
    prints = tmp_db.execute(f"SELECT prints FROM {D.SOURCE_TABLE}").fetchone()[0]
    assert prints == 6, "a batch was folded twice or skipped"


# ------------------------------------------------------------------ the bootstrap CLI


def test_the_bootstrap_waits_while_the_wal_is_large_and_does_not_refresh(tmp_db):
    """The box's DB-guard rule: no fold while the WAL is over the limit, and no stats
    written from a fold that never caught up."""
    born = NOW - 3_600_000
    token(tmp_db, "Tok", first_seen_ms=born)
    tape(tmp_db, "Tok", ["0.00001"] * 8, born)
    now = [1000.0]
    slept: list[float] = []

    def sleep(s: float) -> None:
        slept.append(s)
        now[0] += s

    code = D.main(["--budget-s", "150", "--max-wal-mb", "-1", "--refresh", "sol"],
                  clock=lambda: now[0], sleep=sleep)
    assert code == 2
    assert slept == [60, 60, 60]
    D.ensure_source_table(tmp_db)
    assert tmp_db.execute(f"SELECT COUNT(*) FROM {D.SOURCE_TABLE}").fetchone()[0] == 0
    D.ensure_table(tmp_db)
    assert tmp_db.execute("SELECT COUNT(*) FROM deployer_stats").fetchone()[0] == 0


def test_the_bootstrap_folds_then_refreshes(tmp_db):
    born = NOW - 3_600_000
    token(tmp_db, "Tok", first_seen_ms=born)
    tape(tmp_db, "Tok", ["0.00001"] * 7 + ["0.00003"], born)
    assert D.main(["--budget-s", "60", "--max-wal-mb", "1e9", "--refresh", "sol"]) == 0
    row = tmp_db.execute("SELECT scored, runners FROM deployer_stats").fetchone()
    assert tuple(row) == (1, 1)
