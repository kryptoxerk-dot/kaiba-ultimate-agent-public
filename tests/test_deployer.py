"""The deployer's record is about EARLIER launches, and "we do not know" is not "clean".

The measurement behind this module is in its docstring. These tests pin the three ways a
deployer-reputation feature lies to you:

* it counts the token it is judging in that token's own record, which is the leakage that
  made a first pass of the study report every "all dud" cell at exactly 0.0% reaching 2x
  -- a number that is true by construction and means nothing;
* it reads a missing or stale record as a clean one, so an unmeasured deployer gets the
  benefit of the doubt on live money;
* it runs its seven-day scan on the entry path.
"""

from __future__ import annotations

import time
from decimal import Decimal

import pytest

from kaiba.core.schemas import Chain, EvidenceBasis
from kaiba.intelligence import deployer as D

NOW = int(time.time() * 1000)
DEV = "NMgfqcn6BoS1k6yZoYfureFGPCdev1111111111111111"


def put_token(conn, address: str, creator: str = DEV, *, first_seen_ms: int | None = None,
              chain: Chain = Chain.SOL) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO tokens (chain, address, symbol, decimals, creator, first_seen_ms) "
        "VALUES (?,?,?,?,?,?)",
        (chain.value, address, "T", 9, creator,
         first_seen_ms if first_seen_ms is not None else NOW - 3_600_000),
    )
    conn.commit()


def put_prints(conn, token: str, prices: list[str], *, start_ms: int | None = None,
               chain: Chain = Chain.SOL) -> None:
    base = start_ms if start_ms is not None else NOW - 3_600_000
    for i, price in enumerate(prices):
        conn.execute(
            "INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, amount_token, "
            "price_usd, usd_value, source) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (chain.value, f"tx{token}{i}", base + i * 1000, "w", token, "buy", "1",
             price, "10", "test"),
        )
    conn.commit()


def a_launch(conn, token: str, peak_multiple: str, *, creator: str = DEV,
             first_seen_ms: int | None = None, prints: int = D.MIN_PRINTS) -> None:
    """A launch that opens at 1.0 and peaks at ``peak_multiple``."""
    put_token(conn, token, creator, first_seen_ms=first_seen_ms)
    series = ["1.0"] * (prints - 1) + [peak_multiple]
    put_prints(conn, token, series, start_ms=first_seen_ms)


# ------------------------------------------------------------------ the leakage rule


def test_the_token_being_judged_is_not_in_its_own_record(tmp_db):
    """THE BUG THIS MODULE EXISTS TO AVOID.

    One deployer, one launch, and that launch ran 9x. Its own record must read "no prior",
    never "a proven deployer". Counting it is how a study reports that dud deployers never
    produce a 2x -- of course they do not, that is the definition of the bucket.
    """
    a_launch(tmp_db, "TokA", "9.0")
    D.refresh(tmp_db, Chain.SOL, now_ms=NOW)
    rec = D.lookup(tmp_db, Chain.SOL, "TokA", now_ms=NOW)
    assert rec.known
    assert rec.prior_scored == 0, "its own outcome leaked into its record"
    assert rec.prior_runners == 0
    assert rec.record == "no_prior"


def test_an_earlier_runner_is_counted_for_a_later_launch(tmp_db):
    a_launch(tmp_db, "TokA", "9.0", first_seen_ms=NOW - 7_200_000)
    a_launch(tmp_db, "TokB", "1.1", first_seen_ms=NOW - 3_600_000)
    D.refresh(tmp_db, Chain.SOL, now_ms=NOW)
    rec = D.lookup(tmp_db, Chain.SOL, "TokB", now_ms=NOW)
    assert rec.prior_scored == 1 and rec.prior_runners == 1
    assert rec.record == "runner"


def test_earlier_duds_read_as_all_dud(tmp_db):
    a_launch(tmp_db, "TokA", "1.1", first_seen_ms=NOW - 7_200_000)
    a_launch(tmp_db, "TokB", "1.2", first_seen_ms=NOW - 3_600_000)
    D.refresh(tmp_db, Chain.SOL, now_ms=NOW)
    rec = D.lookup(tmp_db, Chain.SOL, "TokB", now_ms=NOW)
    assert rec.prior_scored == 1 and rec.prior_runners == 0
    assert rec.record == "all_dud"


# ------------------------------------------------------------------ unknown is not clean


def test_a_token_with_no_creator_is_unknown(tmp_db):
    put_token(tmp_db, "TokA", creator="")
    D.refresh(tmp_db, Chain.SOL, now_ms=NOW)
    rec = D.lookup(tmp_db, Chain.SOL, "TokA", now_ms=NOW)
    assert not rec.known and rec.label == "unknown"
    assert rec.basis is EvidenceBasis.UNAVAILABLE


def test_a_deployer_absent_from_the_stats_is_unknown(tmp_db):
    put_token(tmp_db, "TokA")
    D.ensure_table(tmp_db)
    rec = D.lookup(tmp_db, Chain.SOL, "TokA", now_ms=NOW)
    assert not rec.known and "not in stats" in rec.note


def test_a_stale_record_is_unknown_not_reused(tmp_db):
    """An hour-old launch count is fine; a day-old one is a different deployer."""
    a_launch(tmp_db, "TokA", "1.1")
    D.refresh(tmp_db, Chain.SOL, now_ms=NOW)
    rec = D.lookup(tmp_db, Chain.SOL, "TokA", now_ms=NOW + (D.STALE_AFTER_S + 60) * 1000)
    assert not rec.known and "stale" in rec.note


def test_an_unreadable_table_is_unknown_not_a_crash(tmp_db, monkeypatch):
    put_token(tmp_db, "TokA")

    def boom(*a, **k):
        import sqlite3 as s

        raise s.OperationalError("disk I/O error")

    monkeypatch.setattr(D, "fetch_one", boom)
    rec = D.lookup(tmp_db, Chain.SOL, "TokA", now_ms=NOW)
    assert not rec.known


# ------------------------------------------------------------------ the bands


@pytest.mark.parametrize(
    ("launches", "band"),
    [(1, "low"), (10, "low"), (11, "mid"), (50, "mid"), (51, "spam"), (500, "spam")],
)
def test_volume_bands_match_the_measurement(launches, band):
    rec = D.DeployerRecord(Chain.SOL, DEV, launches, 0, 0, None, EvidenceBasis.DERIVED)
    assert rec.volume_band == band


def test_the_label_composes_volume_and_record():
    rec = D.DeployerRecord(Chain.SOL, DEV, 80, 4, 0, Decimal("1.1"), EvidenceBasis.DERIVED)
    assert rec.label == "spam/all_dud", "the worst measured cell: 5.3% reach 2x vs 13.2%"


def test_the_runner_threshold_is_the_one_that_discriminates():
    """The >=5x cells in the study run on 47-143 tokens; >=2x is the usable statistic."""
    assert D.RUNNER_MULTIPLE == Decimal(2)


def test_a_token_below_the_print_floor_does_not_count_as_an_outcome(tmp_db):
    """Too little tape is not an outcome; it must not become a dud on the record."""
    a_launch(tmp_db, "TokA", "9.0", first_seen_ms=NOW - 7_200_000, prints=D.MIN_PRINTS - 1)
    a_launch(tmp_db, "TokB", "1.0", first_seen_ms=NOW - 3_600_000)
    D.refresh(tmp_db, Chain.SOL, now_ms=NOW)
    rec = D.lookup(tmp_db, Chain.SOL, "TokB", now_ms=NOW)
    assert rec.prior_scored == 0, "a thinly-traded launch is not evidence either way"


def test_launch_volume_counts_every_token_not_just_priced_ones(tmp_db):
    """A spam factory's tokens mostly never trade. Counting only priced ones would hide it."""
    for i in range(60):
        put_token(tmp_db, f"Spam{i}")
    a_launch(tmp_db, "TokReal", "1.1")
    D.refresh(tmp_db, Chain.SOL, now_ms=NOW)
    rec = D.lookup(tmp_db, Chain.SOL, "TokReal", now_ms=NOW)
    assert rec.launches >= 61 and rec.volume_band == "spam"


def test_refresh_is_idempotent(tmp_db):
    a_launch(tmp_db, "TokA", "3.0")
    assert D.refresh(tmp_db, Chain.SOL, now_ms=NOW) == 1
    assert D.refresh(tmp_db, Chain.SOL, now_ms=NOW) == 1
    rows = tmp_db.execute("SELECT COUNT(*) FROM deployer_stats").fetchone()[0]
    assert rows == 1


def test_lookup_does_not_scan_the_tape(tmp_db, monkeypatch):
    """The entry path must not pay for a seven-day aggregate."""
    a_launch(tmp_db, "TokA", "1.1")
    D.refresh(tmp_db, Chain.SOL, now_ms=NOW)
    monkeypatch.setattr(
        D, "fetch_all",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("lookup scanned the tape")),
    )
    D.lookup(tmp_db, Chain.SOL, "TokA", now_ms=NOW)


def test_chains_do_not_share_a_deployer_record(tmp_db):
    put_token(tmp_db, "0xabc", creator=DEV, chain=Chain.BSC)
    a_launch(tmp_db, "TokA", "9.0", first_seen_ms=NOW - 7_200_000)
    D.refresh(tmp_db, Chain.SOL, now_ms=NOW)
    rec = D.lookup(tmp_db, Chain.BSC, "0xabc", now_ms=NOW)
    assert not rec.known, "a sol record must not vouch for a bsc deployer"


# ------------------------------------------------------------------ the sizing arm


def test_the_worst_measured_bucket_is_charged(tmp_db):
    """spam(51+) + every prior launch a dud: 5.3% reach 2x against a 13.2% baseline."""
    from kaiba.execution.risk import DEPLOYER_LADDER

    assert DEPLOYER_LADDER["spam/all_dud"] < DEPLOYER_LADDER["mid/all_dud"] < Decimal(1)


def test_nothing_in_the_ladder_ever_grows_a_size():
    """This mechanism shrinks only; `scaled <= banded` is pinned elsewhere."""
    from kaiba.execution.risk import DEPLOYER_LADDER, DEPLOYER_UNKNOWN

    assert all(m <= Decimal(1) for m in DEPLOYER_LADDER.values())
    assert DEPLOYER_UNKNOWN == Decimal(1)


def test_a_proven_deployer_is_not_charged_but_is_not_rewarded_either(tmp_db):
    """17-19% vs 13.2% is a real lift on 47-125 tokens. Not enough to size UP on."""
    from kaiba.execution.risk import _deployer_multiplier

    a_launch(tmp_db, "TokA", "9.0", first_seen_ms=NOW - 7_200_000)
    a_launch(tmp_db, "TokB", "1.0", first_seen_ms=NOW - 3_600_000)
    D.refresh(tmp_db, Chain.SOL, now_ms=NOW)
    multiplier, label = _deployer_multiplier(Chain.SOL, "TokB", tmp_db)
    assert multiplier == Decimal(1), "no boost"
    assert "runner" in label


def test_an_unknown_deployer_is_charged_nothing(tmp_db):
    """Same rule as an unmeasured concentration: we do not guess on live money."""
    from kaiba.execution.risk import _deployer_multiplier

    put_token(tmp_db, "TokA", creator="")
    multiplier, label = _deployer_multiplier(Chain.SOL, "TokA", tmp_db)
    assert multiplier == Decimal(1) and "unknown" in label


def test_a_spam_factory_with_only_duds_is_cut(tmp_db):
    from kaiba.execution.risk import DEPLOYER_LADDER, _deployer_multiplier

    for i in range(60):
        put_token(tmp_db, f"Spam{i}", first_seen_ms=NOW - 7_200_000)
    a_launch(tmp_db, "TokDud", "1.1", first_seen_ms=NOW - 7_200_000)
    a_launch(tmp_db, "TokNew", "1.0", first_seen_ms=NOW - 3_600_000)
    D.refresh(tmp_db, Chain.SOL, now_ms=NOW)
    multiplier, label = _deployer_multiplier(Chain.SOL, "TokNew", tmp_db)
    assert label == "deployer:spam/all_dud", label
    assert multiplier == DEPLOYER_LADDER["spam/all_dud"]


def test_the_arm_never_raises_into_the_sizer(tmp_db, monkeypatch):
    """A broken record must cost one haircut, never an entry."""
    from kaiba.execution import risk as R

    monkeypatch.setattr(
        D, "lookup", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    multiplier, label = R._deployer_multiplier(Chain.SOL, "TokA", tmp_db)
    assert multiplier == Decimal(1) and "unavailable" in label
