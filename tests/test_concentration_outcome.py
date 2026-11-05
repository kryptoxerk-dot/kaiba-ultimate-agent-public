"""Tests for :mod:`kaiba.learning.concentration_outcome`.

Two properties carry most of these tests.

**A refusal is not a zero.** The module exists to feed a sizing decision, and the failure
that would hurt is a mint we could not measure arriving as "0% concentration, 0% return" —
which reads as "clean launch, size up". Every refusal path therefore asserts ``is None``
rather than falsiness, exactly as ``tests/test_bundles.py`` does for the bundle share.

**A finding must be able to fail.** The statistics here are the evidence for a
recommendation about real money, so the verdict function is tested on data built to break
it: a sign flip out of sample, a held-out half too small to speak, and a feature that is a
pure function of its own control.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest

from kaiba.core.schemas import Chain, EvidenceBasis
from kaiba.intelligence import bundles
from kaiba.learning import concentration_outcome as co

SOL = Chain.SOL
CREATOR = "Creator1111111111111111111111111111111111111"
CREATED_MS = 1_789_891_020_283
TOKEN = "Mint1111111111111111111111111111111111111111"
REAL_ATOMS = bundles.PUMPFUN_LAUNCH_CURVE_ATOMS
VIRTUAL_ATOMS = REAL_ATOMS + bundles.PUMPFUN_CURVE_INVARIANT_ATOMS
SUPPLY = bundles.PUMPFUN_TOTAL_SUPPLY_ATOMS
ONE_PCT = SUPPLY // 100


# --------------------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------------------


def _token(conn: sqlite3.Connection, address: str = TOKEN, *, created_ms: int | None = CREATED_MS):
    conn.execute(
        "INSERT INTO tokens (chain, address, creator, created_ms, launchpad, first_seen_ms) "
        "VALUES (?,?,?,?,?,?)",
        (SOL.value, address, CREATOR, created_ms, "pump.fun", created_ms or 0),
    )


def _snapshot(
    conn: sqlite3.Connection,
    address: str = TOKEN,
    *,
    coverage_from_ms: int | None = CREATED_MS - 1_000,
    real: int = REAL_ATOMS,
    virtual: int | None = VIRTUAL_ATOMS,
):
    conn.execute(
        "INSERT INTO curve_snapshots (chain, token, observed_ms, real_sol_lamports, "
        " virtual_sol_lamports, real_token_atoms, virtual_token_atoms, coverage_from_ms, "
        " created_ms, source) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            SOL.value,
            address,
            CREATED_MS,
            0,
            30_000_000_000,
            str(real),
            str(virtual if virtual is not None else real),
            coverage_from_ms,
            CREATED_MS,
            "pumpfun",
        ),
    )


def _tape(
    conn: sqlite3.Connection,
    address: str = TOKEN,
    *,
    covered_to_ms: int | None = None,
    coverage: str = "complete",
):
    conn.execute(
        "INSERT INTO token_tape (chain, token, model, coverage, route, proof, reason, "
        " covered_from_ms, covered_to_ms, created_ms, first_seen_ms, updated_ms) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            SOL.value,
            address,
            "test",
            coverage,
            "pumpfun:trades",
            "test-proof",
            "test fixture",
            CREATED_MS - 1_000,
            covered_to_ms,
            CREATED_MS,
            CREATED_MS,
            CREATED_MS,
        ),
    )


_tx = 0


def _swap(
    conn: sqlite3.Connection,
    wallet: str,
    *,
    address: str = TOKEN,
    ts_ms: int = CREATED_MS,
    side: str = "buy",
    atoms: int | None = ONE_PCT,
    lamports: int | None = 1_000_000,
    source: str = "pumpfun:trades",
):
    global _tx
    _tx += 1
    conn.execute(
        "INSERT INTO swaps (chain, tx, slot, block_index, ts_ms, wallet, token, side, "
        " amount_token, amount_native, source) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            SOL.value,
            f"tx{_tx:040d}",
            1_000 + _tx,
            _tx,
            ts_ms,
            wallet,
            address,
            side,
            None if atoms is None else str(atoms),
            None if lamports is None else str(lamports),
            source,
        ),
    )


@pytest.fixture
def clean_launch(tmp_db):
    """Two buyers, 10% and 20% of supply, inside the window. Nothing pathological."""
    _token(tmp_db)
    _snapshot(tmp_db)
    _tape(tmp_db)
    _swap(tmp_db, "walletA", atoms=10 * ONE_PCT, lamports=10_000_000, ts_ms=CREATED_MS + 1_000)
    _swap(tmp_db, "walletB", atoms=20 * ONE_PCT, lamports=24_000_000, ts_ms=CREATED_MS + 2_000)
    tmp_db.commit()
    return tmp_db


# --------------------------------------------------------------------------------------
# provenance: every threshold says where it came from, and unmeasured ones say INVENTED
# --------------------------------------------------------------------------------------


def _declared_thresholds() -> set[str]:
    skip = {
        # Imported from kaiba.intelligence.bundles, which declares their provenance.
        "LAUNCH_ANCHOR_TOLERANCE_MS",
        "PER_TOKEN_TAPE_SOURCES",
        "MODEL_ID",
        "FEATURE_SOURCE",
        "ZERO",
        "HUNDRED",
        "MEASURED",
        "UNAVAILABLE",
        "BUCKET_LABELS",
        "FEATURES",
        "THRESHOLD_PROVENANCE",
    }
    out = set()
    for name, value in vars(co).items():
        if not name.isupper() or name.startswith("_") or name in skip:
            continue
        if isinstance(value, int | Decimal | tuple):
            out.add(name)
    return out


def test_every_threshold_declares_its_provenance():
    assert _declared_thresholds() == set(co.THRESHOLD_PROVENANCE), (
        "a knob was added or removed without a provenance entry; the dict is the only "
        "place a magic number is allowed to exist"
    )


def test_unmeasured_thresholds_use_the_word_invented():
    vocabulary = ("MEASURED", "INVENTED", "DEFINITIONAL", "STRUCTURAL", "CONVENTION", "DETERMINISM")
    for name, text in co.THRESHOLD_PROVENANCE.items():
        assert text.split(".")[0].split(",")[0].split(" ")[0] in vocabulary, name
    invented = [n for n, t in co.THRESHOLD_PROVENANCE.items() if "INVENTED" in t]
    assert "WAVE_WINDOW_S" in invented
    assert "BUCKET_EDGES" in invented
    assert len(invented) >= 5


# --------------------------------------------------------------------------------------
# the feature: refusals are None, never 0
# --------------------------------------------------------------------------------------


def test_measures_a_clean_launch(clean_launch):
    wave = co.launch_wave(SOL, TOKEN, clean_launch)
    assert wave.coverage == co.MEASURED
    assert wave.gross_pct == Decimal(30)
    assert wave.net_pct == Decimal(30)
    assert wave.top1_pct == Decimal(20)
    assert wave.wallets == 2
    assert wave.buys == 2
    assert wave.supply_basis == bundles.SupplyBasis.PUMPFUN_STANDARD_VERIFIED.value
    assert wave.measure().basis is EvidenceBasis.DERIVED


def test_no_created_ms_is_unavailable_not_zero(tmp_db):
    _token(tmp_db, created_ms=None)
    _snapshot(tmp_db)
    _swap(tmp_db, "walletA")
    tmp_db.commit()
    wave = co.launch_wave(SOL, TOKEN, tmp_db)
    assert wave.coverage == co.UNAVAILABLE
    assert wave.gross_pct is None
    assert wave.measure().value is None
    assert wave.measure().basis is EvidenceBasis.UNAVAILABLE
    assert "created_ms" in wave.reason


def test_a_mint_we_never_ingested_says_so(tmp_db):
    """GIVE's own answer on both databases: not measured, and not zero either."""
    wave = co.launch_wave(SOL, "12qeY9vz1uZHZjWtQPuRfmJtMidPXg9mU1CY4Mpkygiv", tmp_db)
    assert wave.coverage == co.UNAVAILABLE
    assert wave.gross_pct is None
    assert "not in tokens" in wave.reason


def test_tape_that_starts_after_the_launch_is_unavailable(tmp_db):
    _token(tmp_db)
    _snapshot(tmp_db, coverage_from_ms=CREATED_MS + 3_600_000)
    _swap(tmp_db, "walletA", ts_ms=CREATED_MS + 3_600_000)
    tmp_db.commit()
    wave = co.launch_wave(SOL, TOKEN, tmp_db)
    assert wave.coverage == co.UNAVAILABLE
    assert wave.gross_pct is None
    assert "tape starts" in wave.reason


def test_wallet_walk_source_cannot_anchor_a_launch(tmp_db):
    """A ``helius:backfill`` row at the create slot proves nothing about the rest of it."""
    _token(tmp_db)
    _snapshot(tmp_db, coverage_from_ms=CREATED_MS + 3_600_000)
    _swap(tmp_db, "walletA", source="helius:backfill")
    tmp_db.commit()
    wave = co.launch_wave(SOL, TOKEN, tmp_db)
    assert wave.coverage == co.UNAVAILABLE
    assert wave.gross_pct is None


def test_no_supply_denominator_is_unavailable(tmp_db):
    _token(tmp_db)
    _swap(tmp_db, "walletA")
    tmp_db.commit()
    wave = co.launch_wave(SOL, TOKEN, tmp_db)
    assert wave.coverage == co.UNAVAILABLE
    assert wave.gross_pct is None
    assert wave.supply_atoms is None


def test_mostly_unpriced_launch_buys_are_refused(tmp_db):
    _token(tmp_db)
    _snapshot(tmp_db)
    _swap(tmp_db, "walletA", atoms=None)
    _swap(tmp_db, "walletB", atoms=None)
    _swap(tmp_db, "walletC", atoms=None)
    _swap(tmp_db, "walletD", atoms=ONE_PCT)
    tmp_db.commit()
    wave = co.launch_wave(SOL, TOKEN, tmp_db)
    assert wave.coverage == co.UNAVAILABLE
    assert wave.gross_pct is None
    assert "amount_token" in wave.reason


def test_the_shipped_window_is_thirty_seconds(clean_launch):
    """Pinned because widening it strengthens the headline number for free.

    A 60 s window would have swept the +45 s buy below into the launch wave and reported
    a higher concentration on the same tape. The width is a claim about what "at launch"
    means and it is stated in THRESHOLD_PROVENANCE; changing it has to be a decision, not
    a drift.
    """
    assert co.WAVE_WINDOW_S == 30
    _swap(clean_launch, "walletLate", atoms=40 * ONE_PCT, ts_ms=CREATED_MS + 45_000)
    clean_launch.commit()
    assert co.launch_wave(SOL, TOKEN, clean_launch).gross_pct == Decimal(30)
    assert co.launch_wave(SOL, TOKEN, clean_launch, window_s=60).gross_pct == Decimal(70)


def test_a_wallet_that_only_sells_cannot_push_net_below_what_others_hold(tmp_db):
    """The creator's allocation is sold without ever appearing as a buy.

    Netting that sell against the rest of the wave would subtract supply nobody in the
    wave bought, and report a wave holding less than it does.
    """
    _token(tmp_db)
    _snapshot(tmp_db)
    _tape(tmp_db)
    _swap(tmp_db, "walletA", atoms=30 * ONE_PCT, ts_ms=CREATED_MS + 1_000)
    _swap(tmp_db, "walletB", atoms=10 * ONE_PCT, ts_ms=CREATED_MS + 2_000)
    _swap(tmp_db, CREATOR, side="sell", atoms=10 * ONE_PCT, ts_ms=CREATED_MS + 3_000)
    tmp_db.commit()
    wave = co.launch_wave(SOL, TOKEN, tmp_db)
    assert wave.gross_pct == Decimal(40)
    assert wave.net_pct == Decimal(40)


def test_a_single_print_window_has_a_wave_but_no_return(tmp_db):
    _token(tmp_db)
    _snapshot(tmp_db)
    _tape(tmp_db, covered_to_ms=CREATED_MS + 10 * 60_000)
    _swap(tmp_db, "walletA", atoms=25 * ONE_PCT, lamports=5_000_000, ts_ms=CREATED_MS + 1_000)
    tmp_db.commit()
    wave = co.launch_wave(SOL, TOKEN, tmp_db)
    assert wave.coverage == co.MEASURED
    assert wave.gross_pct == Decimal(25)
    assert wave.window_vwap is None
    ret = co.forward_return(wave, tmp_db, k_min=5)
    assert ret.coverage == co.UNAVAILABLE
    assert ret.ret_vwap_pct is None
    assert "MIN_WINDOW_TRADES" in ret.reason


def test_buys_after_the_window_are_not_in_the_wave(clean_launch):
    _swap(
        clean_launch,
        "walletC",
        atoms=40 * ONE_PCT,
        ts_ms=CREATED_MS + (co.WAVE_WINDOW_S + 5) * 1000,
    )
    clean_launch.commit()
    wave = co.launch_wave(SOL, TOKEN, clean_launch)
    assert wave.gross_pct == Decimal(30)


def test_the_relay_shows_up_as_gross_above_net(tmp_db):
    """The GIVE evasion, in miniature: buy, sell, re-buy the same supply.

    A holder snapshot at the end of the window sees 30% in two hands. ``gross`` sees the
    60% that actually changed hands, which is the quantity the operator was asking for.
    """
    _token(tmp_db)
    _snapshot(tmp_db)
    _tape(tmp_db)
    _swap(tmp_db, "walletA", atoms=30 * ONE_PCT, ts_ms=CREATED_MS + 1_000)
    _swap(tmp_db, "walletA", atoms=30 * ONE_PCT, ts_ms=CREATED_MS + 2_000, side="sell")
    _swap(tmp_db, "walletB", atoms=30 * ONE_PCT, ts_ms=CREATED_MS + 3_000)
    tmp_db.commit()
    wave = co.launch_wave(SOL, TOKEN, tmp_db)
    assert wave.gross_pct == Decimal(60)
    assert wave.net_pct == Decimal(30)
    assert wave.gross_pct > wave.net_pct


def test_hhi_separates_one_hand_from_a_crowd(tmp_db):
    _token(tmp_db)
    _snapshot(tmp_db)
    _tape(tmp_db)
    for i in range(10):
        _swap(tmp_db, f"wallet{i}", atoms=3 * ONE_PCT, ts_ms=CREATED_MS + i * 100)
    tmp_db.commit()
    crowd = co.launch_wave(SOL, TOKEN, tmp_db)
    assert crowd.gross_pct == Decimal(30)
    assert crowd.hhi == pytest.approx(Decimal(10))  # 10 equal hands -> 1/10, on a 0..100 scale
    assert crowd.top1_pct == Decimal(3)


def test_atoms_are_integers_not_floats():
    big = "793100000000000123"
    assert co._atoms(big) == 793_100_000_000_000_123
    assert co._atoms(None) is None
    assert co._atoms("") is None
    assert co._atoms("-5") is None


# --------------------------------------------------------------------------------------
# the outcome
# --------------------------------------------------------------------------------------


def test_short_tape_has_no_return_rather_than_a_flat_one(clean_launch):
    wave = co.launch_wave(SOL, TOKEN, clean_launch)
    ret = co.forward_return(wave, clean_launch, k_min=5)
    assert ret.coverage == co.UNAVAILABLE
    assert ret.ret_vwap_pct is None
    assert "does not reach" in ret.reason


def test_a_dead_mint_keeps_its_last_price(tmp_db):
    """Silence under a coverage watermark is a price, and it is usually a bad one."""
    _token(tmp_db)
    _snapshot(tmp_db)
    _tape(tmp_db, covered_to_ms=CREATED_MS + 10 * 60_000)
    _swap(tmp_db, "walletA", atoms=10 * ONE_PCT, lamports=10_000_000, ts_ms=CREATED_MS + 1_000)
    _swap(tmp_db, "walletB", atoms=20 * ONE_PCT, lamports=20_000_000, ts_ms=CREATED_MS + 2_000)
    # the dump, still inside the first minute, and then nothing at all
    _swap(
        tmp_db,
        "walletA",
        side="sell",
        atoms=10 * ONE_PCT,
        lamports=2_000_000,
        ts_ms=CREATED_MS + 20_000,
    )
    tmp_db.commit()
    wave = co.launch_wave(SOL, TOKEN, tmp_db)
    ret = co.forward_return(wave, tmp_db, k_min=5)
    assert ret.coverage == co.MEASURED
    assert ret.mark_basis == "last_print"
    assert ret.ret_vwap_pct is not None and ret.ret_vwap_pct < 0


def test_bracket_vwap_is_preferred_when_the_mint_still_trades(tmp_db):
    _token(tmp_db)
    _snapshot(tmp_db)
    _tape(tmp_db)
    _swap(tmp_db, "walletA", atoms=10 * ONE_PCT, lamports=10_000_000, ts_ms=CREATED_MS + 1_000)
    _swap(tmp_db, "walletB", atoms=10 * ONE_PCT, lamports=10_000_000, ts_ms=CREATED_MS + 2_000)
    _swap(tmp_db, "walletC", atoms=ONE_PCT, lamports=3_000_000, ts_ms=CREATED_MS + 5 * 60_000)
    tmp_db.commit()
    wave = co.launch_wave(SOL, TOKEN, tmp_db)
    ret = co.forward_return(wave, tmp_db, k_min=5)
    assert ret.coverage == co.MEASURED
    assert ret.mark_basis.startswith("bracket_vwap")
    # 3x the launch price per atom, so +200%
    assert ret.ret_vwap_pct == pytest.approx(Decimal(200))


def test_post_wave_baseline_rebases_on_the_end_of_the_window(tmp_db):
    _token(tmp_db)
    _snapshot(tmp_db)
    _tape(tmp_db)
    _swap(tmp_db, "walletA", atoms=10 * ONE_PCT, lamports=10_000_000, ts_ms=CREATED_MS + 1_000)
    _swap(tmp_db, "walletB", atoms=10 * ONE_PCT, lamports=30_000_000, ts_ms=CREATED_MS + 2_000)
    _swap(tmp_db, "walletC", atoms=ONE_PCT, lamports=3_000_000, ts_ms=CREATED_MS + 5 * 60_000)
    tmp_db.commit()
    wave = co.launch_wave(SOL, TOKEN, tmp_db)
    ret = co.forward_return(wave, tmp_db, k_min=5)
    # window VWAP is 2 lamports/atom, the last window print is 3, the mark is 3
    assert ret.ret_vwap_pct == pytest.approx(Decimal(50))
    assert ret.ret_post_pct == pytest.approx(Decimal(0))


def test_a_curve_reserve_denominator_is_not_pooled(tmp_db):
    """79.31% of supply is a different denominator and must not share a bucket table."""
    _token(tmp_db)
    _snapshot(tmp_db, real=500_000_000_000_000, virtual=600_000_000_000_000)
    _tape(tmp_db, covered_to_ms=CREATED_MS + 10 * 60_000)
    _swap(tmp_db, "walletA", atoms=10 * ONE_PCT, lamports=10_000_000, ts_ms=CREATED_MS)
    _swap(tmp_db, "walletB", atoms=10 * ONE_PCT, lamports=10_000_000, ts_ms=CREATED_MS + 1_000)
    tmp_db.commit()
    wave = co.launch_wave(SOL, TOKEN, tmp_db)
    assert wave.coverage == co.MEASURED
    assert wave.share_of_total_supply is False
    sample = co.Sample(wave=wave, returns={5: co.forward_return(wave, tmp_db, k_min=5)})
    assert sample.usable_at(5) is False


# --------------------------------------------------------------------------------------
# statistics
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pct", "label"),
    [
        (Decimal("0"), "<20"),
        (Decimal("19.99"), "<20"),
        (Decimal("20"), "20-35"),
        (Decimal("34.99"), "20-35"),
        (Decimal("35"), "35-50"),
        (Decimal("49.99"), "35-50"),
        (Decimal("50"), ">50"),
        (Decimal("59.418"), ">50"),
        (Decimal("180"), ">50"),
    ],
)
def test_bucket_edges(pct, label):
    assert co.bucket_of(pct) == label


def test_thin_buckets_refuse_rather_than_report():
    points = [(Decimal(5), Decimal(1)), (Decimal(60), Decimal(-90))]
    stats = {b.label: b for b in co.bucket_stats(points)}
    assert stats["<20"].n == 1
    assert stats["<20"].mean is None
    assert stats["<20"].median is None
    assert "MIN_BUCKET_N" in stats["<20"].reason


def test_bucket_stats_report_when_the_bucket_is_big_enough():
    points = [(Decimal(5), Decimal(v)) for v in (-10, -5, 0, 5, 20)]
    stats = {b.label: b for b in co.bucket_stats(points)}
    assert stats["<20"].n == 5
    assert stats["<20"].median == Decimal(0)
    assert stats["<20"].mean == Decimal(2)
    assert stats["<20"].hit_rate == Decimal(40)


def test_median_of_an_even_bucket_averages_the_middle_two():
    points = [(Decimal(5), Decimal(v)) for v in (-10, -4, 2, 8, 20, 40)]
    stats = {b.label: b for b in co.bucket_stats(points)}
    assert stats["<20"].n == 6
    assert stats["<20"].median == Decimal(5)


def test_spearman_signs_and_refusals():
    xs = [Decimal(i) for i in range(10)]
    assert co.spearman(xs, xs) == pytest.approx(1.0)
    assert co.spearman(xs, list(reversed(xs))) == pytest.approx(-1.0)
    assert co.spearman(xs, [Decimal(1)] * 10) is None
    assert co.spearman([Decimal(1), Decimal(2)], [Decimal(1), Decimal(2)]) is None
    assert co.spearman(xs, xs[:3]) is None


def test_partial_spearman_removes_a_feature_that_is_only_its_control():
    control = [Decimal(i) for i in range(20)]
    feature = [c * 2 for c in control]  # a pure function of the control
    outcome = [-c for c in control]
    assert co.spearman(feature, outcome) == pytest.approx(-1.0)
    assert co.partial_spearman(feature, outcome, control) is None


def test_partial_spearman_keeps_an_independent_feature():
    control = [Decimal(i) for i in range(20)]
    feature = [Decimal((i * 7) % 20) for i in range(20)]
    outcome = [f + Decimal("0.001") * c for f, c in zip(feature, control, strict=True)]
    partial = co.partial_spearman(feature, outcome, control)
    assert partial is not None and partial > 0.9


def test_permutation_p_counts_the_observed_arrangement():
    """Three perfectly ordered points are not evidence, and the p-value must say so.

    One shuffle in six reproduces a perfect rank correlation, so the honest p here is
    around 0.17. Counting only *stronger* shuffles would report about 0.005 and turn
    three points into a finding.
    """
    xs = [Decimal(1), Decimal(2), Decimal(3)]
    ys = [Decimal(10), Decimal(20), Decimal(30)]
    p = co.permutation_p(xs, ys, iters=600, seed=7)
    assert p is not None and 0.05 < p < 0.4


def test_permutation_p_is_deterministic_and_bounded():
    xs = [Decimal(i) for i in range(30)]
    ys = [Decimal(i % 7) for i in range(30)]
    first = co.permutation_p(xs, ys, iters=200, seed=1)
    second = co.permutation_p(xs, ys, iters=200, seed=1)
    assert first == second
    assert 0 < first <= 1
    assert co.permutation_p([Decimal(1)] * 5, [Decimal(2)] * 5, iters=10) is None


def test_time_split_does_not_leak_the_future():
    samples = [_synthetic(t0=1_000 * i, conc=Decimal(i), ret=Decimal(-i)) for i in range(11)]
    train, test = co.time_split(samples)
    assert len(train) == 5 and len(test) == 6
    assert max(s.t0_ms for s in train) < min(s.t0_ms for s in test)


def test_stability_blocks_cut_on_the_clock():
    hour = 3_600_000
    samples = [
        _synthetic(t0=t, conc=Decimal(c), ret=Decimal(r))
        for t, c, r in [
            (0, 10, 5),
            (hour, 60, -5),
            (2 * hour, 30, 0),
            (7 * hour, 10, -5),
            (8 * hour, 60, 5),
            (9 * hour, 30, 0),
        ]
    ]
    blocks = co.stability_blocks(samples, 5, block_hours=6)
    assert [b.n for b in blocks] == [3, 3]
    assert blocks[0].rho is not None and blocks[0].rho < 0
    assert blocks[1].rho is not None and blocks[1].rho > 0


# --------------------------------------------------------------------------------------
# verdicts: the finding has to be able to fail
# --------------------------------------------------------------------------------------


def _synthetic(*, t0: int, conc: Decimal, ret: Decimal, wallets: int = 10) -> co.Sample:
    wave = co.LaunchWave(
        chain=SOL,
        token=f"tok{t0}",
        coverage=co.MEASURED,
        reason="synthetic",
        t0_ms=t0,
        created_ms=t0,
        gross_pct=conc,
        net_pct=conc,
        top1_pct=conc / 2,
        hhi=Decimal(50),
        wallets=wallets,
        buys=wallets,
        supply_atoms=SUPPLY,
        supply_basis=bundles.SupplyBasis.PUMPFUN_STANDARD_VERIFIED.value,
        window_trades=3,
        window_vwap=Decimal(1),
        window_last_price=Decimal(1),
        horizon_ms=t0 + 3_600_000,
    )
    return co.Sample(
        wave=wave,
        returns={
            5: co.ForwardReturn(
                k_min=5,
                coverage=co.MEASURED,
                reason="synthetic",
                mark=Decimal(1),
                mark_basis="bracket_vwap_60s",
                ret_vwap_pct=ret,
                ret_post_pct=ret,
            )
        },
    )


def test_a_sign_flip_out_of_sample_does_not_replicate():
    early = [_synthetic(t0=i, conc=Decimal(i), ret=Decimal(-i)) for i in range(1, 41)]
    late = [_synthetic(t0=100 + i, conc=Decimal(i), ret=Decimal(i)) for i in range(1, 41)]
    result = co.analyse_horizon(early + late, 5)
    assert result.replicates is False
    assert "sign flips" in result.verdict


def test_a_held_out_half_that_is_too_small_says_so():
    samples = [_synthetic(t0=i, conc=Decimal(i), ret=Decimal(-i)) for i in range(1, 9)]
    result = co.analyse_horizon(samples, 5)
    assert result.replicates is False
    assert "NOT TESTABLE" in result.verdict


def test_a_stable_relationship_replicates():
    samples = [
        _synthetic(t0=i, conc=Decimal(i % 50), ret=Decimal(-(i % 50)) + Decimal(i % 3))
        for i in range(1, 121)
    ]
    result = co.analyse_horizon(samples, 5)
    assert result.replicates is True
    assert result.rho is not None and result.rho < 0
    assert "REPLICATES" in result.verdict


def test_a_flat_relationship_does_not_replicate():
    samples = [_synthetic(t0=i, conc=Decimal(i % 60), ret=Decimal((i * 13) % 7)) for i in range(120)]
    result = co.analyse_horizon(samples, 5)
    assert result.replicates is False


def test_selection_check_counts_who_was_left_out():
    inside = [_synthetic(t0=i, conc=Decimal(80), ret=Decimal(1)) for i in range(5)]
    outside = []
    for i in range(5):
        sample = _synthetic(t0=100 + i, conc=Decimal(2), ret=Decimal(1))
        outside.append(
            co.Sample(
                wave=sample.wave,
                returns={
                    5: co.ForwardReturn(k_min=5, coverage=co.UNAVAILABLE, reason="tape too short")
                },
            )
        )
    check = co.selection_check(inside + outside, 5)
    assert check.n_included == 5
    assert check.n_excluded == 5
    assert check.median_included == Decimal(80)
    assert check.median_excluded == Decimal(2)
    assert check.share_over_50_included == Decimal(100)
    assert check.share_over_50_excluded == Decimal(0)


def test_tail_contrast_refuses_on_thin_arms():
    high = [
        _synthetic(t0=i, conc=Decimal(90), ret=Decimal(-50 + i), wallets=30) for i in range(3)
    ]
    low = [_synthetic(t0=10 + i, conc=Decimal(5), ret=Decimal(i), wallets=30) for i in range(3)]
    tail = co.tail_contrast(high + low, 5, min_wallets=24)
    assert tail.n_high == 3 and tail.n_low == 3
    assert tail.diff_pp is None
    assert tail.p_value is None


def test_tail_contrast_measures_a_real_gap():
    high = [
        _synthetic(t0=i, conc=Decimal(80), ret=Decimal(-40) + Decimal(i) / 4, wallets=30)
        for i in range(20)
    ]
    low = [
        _synthetic(t0=50 + i, conc=Decimal(10), ret=Decimal(i) / 4, wallets=30) for i in range(20)
    ]
    tail = co.tail_contrast(high + low, 5, min_wallets=24)
    assert tail.n_high == 20 and tail.n_low == 20
    assert tail.diff_pp == Decimal(-40)
    assert tail.p_value is not None and tail.p_value < 0.05


def test_a_two_valued_sample_cannot_support_a_median_test():
    """A real limitation, asserted so nobody quotes a p-value this test would refuse.

    With every high at one value and every low at another, a shuffled 50/50 split lands
    on the same two medians unless it is exactly balanced, so the permutation p sits near
    0.75 no matter how large the gap is. The module is not broken when this happens; the
    sample is, and on a real tape ties this total do not occur.
    """
    high = [_synthetic(t0=i, conc=Decimal(80), ret=Decimal(-40), wallets=30) for i in range(20)]
    low = [_synthetic(t0=50 + i, conc=Decimal(10), ret=Decimal(0), wallets=30) for i in range(20)]
    tail = co.tail_contrast(high + low, 5, min_wallets=24)
    assert tail.diff_pp == Decimal(-40)
    assert tail.p_value is not None and tail.p_value > 0.5


# --------------------------------------------------------------------------------------
# corpus plumbing
# --------------------------------------------------------------------------------------


def test_collect_counts_refusals_as_well_as_measurements(clean_launch):
    _token(clean_launch, "Mint222222222222222222222222222222222222222", created_ms=CREATED_MS)
    _tape(clean_launch, "Mint222222222222222222222222222222222222222", coverage="partial")
    clean_launch.commit()
    samples, census = co.collect(SOL, clean_launch, horizons=(5,))
    assert census["candidates"] == 2
    assert census["wave_measured"] == 1
    assert census["wave_refused"] == 1
    assert census["ret_refused_k5:tape_too_short"] == 1
    assert len(samples) == 1


def test_realized_arm_reports_how_few_trades_are_measurable(clean_launch):
    clean_launch.execute(
        "INSERT INTO trades (trade_id, position_id, lane, mode, chain, token, opened_ms, "
        " closed_ms, hold_s, cost_native, proceeds_native, pnl_native, pnl_pct) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "trd_1",
            "pos_1",
            "sm-trenches",
            "live",
            SOL.value,
            TOKEN,
            CREATED_MS,
            CREATED_MS + 60_000,
            60,
            "1000",
            "500",
            "-500",
            -50.0,
        ),
    )
    clean_launch.execute(
        "INSERT INTO trades (trade_id, position_id, lane, mode, chain, token, opened_ms, "
        " closed_ms, hold_s, cost_native, proceeds_native, pnl_native, pnl_pct) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "trd_2",
            "pos_2",
            "sm-trenches",
            "live",
            SOL.value,
            "UnknownMint111111111111111111111111111111111",
            CREATED_MS,
            CREATED_MS + 60_000,
            60,
            "1000",
            "500",
            "-500",
            -50.0,
        ),
    )
    clean_launch.commit()
    rows, census = co.realized_arm(SOL, clean_launch)
    assert census["closed_trades"] == 2
    assert census["measurable"] == 1
    assert rows[0].trade_id == "trd_1"
    assert rows[0].wave.gross_pct == Decimal(30)


def test_an_external_refusal_drops_the_mint_instead_of_scoring_it_zero():
    """The owning module refusing a mint must not read as "0% concentration"."""
    scored = [
        co.Sample(
            wave=_synthetic(t0=i, conc=Decimal(10), ret=Decimal(-i)).wave,
            returns=_synthetic(t0=i, conc=Decimal(10), ret=Decimal(-i)).returns,
            external={"headline": Decimal(60)},
        )
        for i in range(1, 41)
    ]
    refused = [
        co.Sample(
            wave=_synthetic(t0=100 + i, conc=Decimal(10), ret=Decimal(500)).wave,
            returns=_synthetic(t0=100 + i, conc=Decimal(10), ret=Decimal(500)).returns,
            external={},
        )
        for i in range(1, 41)
    ]
    result = co.analyse_horizon(scored + refused, 5, feature="ext:headline")
    assert result.n == 40
    assert all(b.n == 0 for b in result.buckets if b.label != ">50")


def test_external_features_pass_through_a_refusal(monkeypatch):
    from kaiba.intelligence import launch_concentration as lc

    refusal = lc.LaunchConcentrationReport(
        chain=SOL,
        token=TOKEN,
        model_id=lc.MODEL_ID,
        basis=lc.Basis.UNAVAILABLE,
        reason="thin tape",
        gate=lc.Gate.THIN,
    )
    monkeypatch.setattr(lc, "measure", lambda *a, **k: refusal)
    assert co.external_features(SOL, TOKEN, None) == {}


def test_external_features_carry_the_owning_modules_numbers(monkeypatch):
    from kaiba.intelligence import launch_concentration as lc

    def _m(value):
        return lc.Measure(value=value, basis=EvidenceBasis.DERIVED)

    report = lc.LaunchConcentrationReport(
        chain=SOL,
        token=TOKEN,
        model_id=lc.MODEL_ID,
        basis=lc.Basis.MEASURED,
        reason="ok",
        headline_pct=_m(Decimal("59.418")),
        cotimed_pct=_m(Decimal("7.372")),
        relay_pct=_m(Decimal("21.799")),
        wave=(
            lc.WavePoint(
                window_s=30,
                wallets=("a",),
                bought_atoms=1,
                in_window_atoms=1,
                pct=_m(Decimal("50.0")),
                in_window_pct=_m(Decimal("40.0")),
            ),
            lc.WavePoint(
                window_s=60,
                wallets=("a", "b"),
                bought_atoms=2,
                in_window_atoms=2,
                pct=_m(Decimal("59.418")),
                in_window_pct=_m(Decimal("45.0")),
            ),
        ),
    )
    monkeypatch.setattr(lc, "measure", lambda *a, **k: report)
    out = co.external_features(SOL, TOKEN, None)
    assert out["headline"] == Decimal("59.418")
    assert out["wave30"] == Decimal("50.0")
    assert out["wave60"] == Decimal("59.418")
    # the leak-free pair: what an observer at t0+W could have computed
    assert out["wave30_iw"] == Decimal("40.0")
    assert out["wave60_iw"] == Decimal("45.0")
    assert out["cotimed"] == Decimal("7.372")
    assert out["relay"] == Decimal("21.799")


def test_open_readonly_cannot_write(tmp_path):
    path = tmp_path / "ro.db"
    seed = sqlite3.connect(path)
    seed.execute("CREATE TABLE t (a INTEGER)")
    seed.commit()
    seed.close()
    conn = co.open_readonly(str(path))
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO t (a) VALUES (1)")


def test_render_never_prints_a_refusal_as_a_number():
    samples = [_synthetic(t0=i, conc=Decimal(i), ret=Decimal(-i)) for i in range(1, 9)]
    result = co.analyse_horizon(samples, 5)
    text = co.render([result], {"candidates": 8}, [], {"closed_trades": 0}, label="unit")
    assert "refused" in text
    assert "NOT TESTABLE" in text
