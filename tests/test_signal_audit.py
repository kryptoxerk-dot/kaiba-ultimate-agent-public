"""The daily self-audit: replay fidelity, no lookahead, honest verdicts, database discipline."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from kaiba.learning import signal_audit as sa

MIG = Path(__file__).resolve().parents[1] / "kaiba" / "core" / "migrations" / "037_signal_audit.sql"
DAY = 86_400_000
T0 = 20 * DAY + 12 * 3600_000  # noon UTC on day 20


def series(*points: tuple[int, float]) -> list[tuple[int, float]]:
    return [(T0 + s * 1000, p) for s, p in points]


# --------------------------------------------------------------------------------------
# the exit replay
# --------------------------------------------------------------------------------------


def test_stop_fills_at_the_next_tick_not_at_the_trigger():
    # Entry at 1.0; -31% trips the -30% stop; 12 s later the print is 0.55. A stop that
    # filled at its trigger would report about -33%. The real tick fills the gap.
    s = series((0, 1.0), (5, 0.95), (10, 0.69), (22, 0.55), (40, 0.50))
    r = sa.simulate_exit(s, 0, 1.0, sa.ExitLadder())
    assert r == pytest.approx(0.55 * (1 - sa.COST_PER_LEG) - 1)


def test_tp1_sells_half_then_breakeven_protects_the_rest():
    s = series((0, 1.0), (10, 2.05), (22, 2.10), (40, 1.6), (60, 0.99), (72, 0.98))
    r = sa.simulate_exit(s, 0, 1.0, sa.ExitLadder())
    # Half fills at 2.10 (one tick after the 2x trigger). The trailing tier for peak 2.1x
    # (30%) trips at 1.6 < 2.1*0.7=1.47? no -- 1.6 > 1.47, so the remainder rides to the
    # breakeven stop at 0.99 and fills at 0.98.
    want = (0.5 * 2.10 + 0.5 * 0.98) * (1 - sa.COST_PER_LEG) - 1
    assert r == pytest.approx(want)
    assert r > 0


def test_ladder_reads_the_risk_config_shape():
    prot = {"stop_loss_bps": 2500, "tp_ladder": [[3.0, 40]], "trailing": [[1.5, 1000]],
            "breakeven_after_tp1": False}
    lad = sa.ExitLadder.from_protection(prot)
    assert lad.stop_frac == pytest.approx(0.25)
    assert lad.tps == ((3.0, 0.40),)
    assert lad.trailing == ((1.5, 0.10),)
    assert lad.breakeven_after_tp1 is False
    assert sa.ExitLadder.from_protection({"tp_ladder": "garbage"}) == sa.ExitLadder()


# --------------------------------------------------------------------------------------
# one signal: single source, no lookahead
# --------------------------------------------------------------------------------------


def _rows(points, *, source="a", wallet="w", side="buy"):
    return [(T0 + s * 1000, p, source, wallet, side) for s, p in points]


def _case(rows, *, wallets=("s1",), kol=frozenset(), grades_now=None, grades_pit=None):
    return sa.build_case(
        lane="sm-trenches", chain="sol", token="T", t0=T0, rows=rows, wallets=list(wallets),
        payload={"holder_count": 250, "launchpad": "pump.fun"}, ladder=sa.ExitLadder(),
        kol=set(kol), trusted=set(), grades_now=grades_now or {}, grades_pit=grades_pit,
    )


def test_a_second_feeds_misprint_cannot_invent_a_take_profit():
    flat = [(-60, 1.0)] + [(s, 1.0) for s in range(0, 600, 20)]
    good = _rows(flat, source="main")
    # The other feed has fewer prints and one 5x misprint inside the hold.
    bad = _rows([(100, 5.0), (113, 5.0)], source="other")
    case = _case(good + bad)
    assert case is not None
    # Flat price, no TP: the outcome is just two legs of cost (marked at horizon/last print).
    assert case.outcome == pytest.approx((1 - sa.COST_PER_LEG) / (1 + sa.COST_PER_LEG) - 1)


def test_features_never_read_after_the_signal():
    pre = _rows([(-600, 1.0), (-300, 1.2), (-10, 1.5)], wallet="s1")
    post = _rows([(s, 1.5) for s in range(31, 900, 30)], wallet="kolwallet")
    case = _case(pre + post, kol={("sol", "kolwallet")})
    assert case is not None
    # The KOL bought only AFTER t0, so it must not count as a pre-signal KOL buyer.
    assert case.features["kol_pre"] == 0
    assert case.features["run_30m"] == pytest.approx(0.5)
    assert case.features["late_s"] == pytest.approx(600)
    # Guard check: had the KOL bought before t0 it would count.
    pre_kol = _rows([(-20, 1.5)], wallet="kolwallet")
    assert _case(pre + pre_kol + post, kol={("sol", "kolwallet")}).features["kol_pre"] == 1


def test_point_in_time_grades_are_separate_from_todays():
    rows = _rows([(s, 1.0) for s in range(-60, 900, 30)])
    case = _case(rows, wallets=("s1", "s2"), grades_now={("sol", "s1"): "A", ("sol", "s2"): "B"},
                 grades_pit={("sol", "s1"): "A"})
    assert case.features["gradeAB_now"] == 2
    assert case.features["gradeAB_pit"] == 1
    assert _case(rows, grades_pit=None).features["gradeAB_pit"] is None


def test_too_little_tape_is_not_a_loss():
    assert _case(_rows([(0, 1.0), (40, 0.5)])) is None


# --------------------------------------------------------------------------------------
# verdicts
# --------------------------------------------------------------------------------------


def _stat(n, mean):
    return sa.Stat(n, mean, mean, 0.5)


def test_edge_needs_both_halves_positive():
    base = _stat(100, -0.15)
    assert sa.verdict(_stat(20, 0.10), _stat(20, 0.02), base, base) == "edge"
    assert sa.verdict(_stat(20, 0.30), _stat(20, -0.20), base, base) == "noise"
    assert sa.verdict(_stat(20, -0.05), _stat(20, -0.04), base, base) == "lift"
    assert sa.verdict(_stat(20, -0.30), _stat(20, -0.25), base, base) == "drag"
    assert sa.verdict(_stat(5, 0.50), _stat(20, 0.50), base, base) == "thin"


def test_todays_grades_are_labelled_lookahead():
    cases = []
    for i in range(60):
        good = i % 2 == 0
        cases.append(sa.SignalCase("sm-trenches", "sol", f"t{i}", T0 + i * 60_000,
                                   0.2 if good else -0.4,
                                   {"gradeAB_now": 2 if good else 0, "gradeAB_pit": None}, ()))
    card = sa.score(cases)
    now_cells = [c for c in card.cells if c.feature == "gradeAB_now" and c.band == "2+"]
    assert now_cells and now_cells[0].verdict == "lookahead:edge"
    assert not [c for c in card.cells if c.feature == "gradeAB_pit"]


def test_one_mooner_cannot_carry_a_cell():
    s = sa.Stat.of([50.0] + [-0.3] * 9)
    assert s.mean == pytest.approx((sa.OUTCOME_CAP - 0.3 * 9) / 10)


# --------------------------------------------------------------------------------------
# the database: short read-only connections, WAL guard, end to end
# --------------------------------------------------------------------------------------


def _db(tmp_path: Path) -> Path:
    p = tmp_path / "k.db"
    c = sqlite3.connect(p)
    c.executescript("""
        CREATE TABLE signals (signal_id TEXT, lane TEXT, chain TEXT, token TEXT, strength REAL,
            reasons_json TEXT, wallets_json TEXT, entities_json TEXT, window_s INT,
            created_ms INT, payload_json TEXT);
        CREATE TABLE swaps (chain TEXT, token TEXT, ts_ms INT, wallet TEXT, side TEXT,
            price_usd TEXT, source TEXT);
        CREATE TABLE wallet_feed_tags (chain TEXT, address TEXT, tag TEXT);
        CREATE TABLE wallets (chain TEXT, address TEXT, cohort TEXT);
        CREATE TABLE wallet_scores (chain TEXT, address TEXT, grade TEXT, score REAL);
    """)
    c.executescript(MIG.read_text(encoding="utf-8"))
    c.executescript((MIG.parent / "039_audit_wallets.sql").read_text(encoding="utf-8"))
    c.commit()
    c.close()
    return p


def test_reader_never_holds_a_connection_and_waits_on_a_big_wal(tmp_path):
    p = _db(tmp_path)
    Path(f"{p}-wal").write_bytes(b"x" * 64)
    sleeps: list[float] = []
    r = sa.Reader(p, wal_limit=10, sleep=sleeps.append, pause_s=0)
    for _ in range(sa.WAL_CHECK_EVERY + 1):
        r.q("SELECT 1")
    # One WAL check fired (at query 25) and waited its bounded maximum.
    assert sleeps.count(60) == 10 and r.wal_waits == 10
    # Read-only: a write through the reader must fail.
    with pytest.raises(sqlite3.OperationalError):
        r.q("INSERT INTO wallets VALUES ('sol','x','y')")


def test_end_to_end_audit_records_and_reads_back(tmp_path):
    p = _db(tmp_path)
    c = sqlite3.connect(p)
    for i in range(40):
        t0 = T0 + i * 3600_000
        c.execute("INSERT INTO signals (lane, chain, token, created_ms, wallets_json, payload_json) "
                  "VALUES ('sm-trenches','sol',?,?,?,?)",
                  (f"tok{i}", t0, json.dumps(["s1", "s2", "s3"]), json.dumps({"holder_count": 300})))
        up = i % 3 == 0
        for k in range(30):
            px = 1.0 + (0.05 * k if up else -0.02 * k)
            c.execute("INSERT INTO swaps VALUES ('sol',?,?,?,?,?,'feed')",
                      (f"tok{i}", t0 - 120_000 + k * 40_000, "s1", "buy", str(px)))
    c.execute("INSERT INTO wallet_scores VALUES ('sol','s1','A',80)")
    c.commit()
    c.close()
    reader = sa.Reader(p, pause_s=0)
    run = sa.run_audit(reader, ladder=sa.ExitLadder(), lanes=["sm-trenches"], chains=["sol"],
                       days=30, now_ms=T0 + 50 * 3600_000)
    assert run.considered == 40 and len(run.cases) == 40 and run.no_tape == 0
    assert ("sm-trenches", "sol") in run.card.baselines
    w = sqlite3.connect(p, isolation_level=None)
    run_id = sa.record(w, run, now_ms=T0 + 51 * 3600_000, days=30)
    got = sa.latest(w, include_noise=True)
    assert got["run_id"] == run_id and got["summary"]["replayed"] == 40
    assert got["cells"]
    snap = sa.snapshot_grades(w, now_ms=T0)
    again = sa.snapshot_grades(w, now_ms=T0)
    assert snap["inserted"] == 1 and again["inserted"] == 0
    w.close()


def test_snapshot_from_the_signal_day_is_not_point_in_time(tmp_path):
    p = _db(tmp_path)
    c = sqlite3.connect(p)
    day = sa._utc_day(T0)  # noqa: SLF001
    prev = sa._utc_day(T0 - DAY)  # noqa: SLF001
    c.execute("INSERT INTO signals (lane, chain, token, created_ms, wallets_json, payload_json) "
              "VALUES ('sm-trenches','sol','tok',?,?,'{}')", (T0, json.dumps(["s1", "s2"])))
    for k in range(30):
        c.execute("INSERT INTO swaps VALUES ('sol','tok',?,?,?,?,'feed')",
                  (T0 - 60_000 + k * 30_000, "s1", "buy", "1.0"))
    c.execute("INSERT INTO wallet_grade_snapshots VALUES (?,?,?,?,?)", (day, "sol", "s1", "A", 90))
    c.execute("INSERT INTO wallet_grade_snapshots VALUES (?,?,?,?,?)", (day, "sol", "s2", "A", 90))
    c.execute("INSERT INTO wallet_grade_snapshots VALUES (?,?,?,?,?)", (prev, "sol", "s1", "B", 70))
    c.commit()
    c.close()
    run = sa.run_audit(sa.Reader(p, pause_s=0), ladder=sa.ExitLadder(), lanes=["sm-trenches"],
                       chains=["sol"], days=30, now_ms=T0 + DAY)
    # Only the PREVIOUS day's snapshot counts (s1 only); the same-day one would say 2.
    assert run.cases[0].features["gradeAB_pit"] == 1


# --------------------------------------------------------------------------------------
# wallet track records: point in time, per chain
# --------------------------------------------------------------------------------------


def _sc(i, t0, outcome, wallets, chain="robinhood"):
    return sa.SignalCase("sm-trenches", chain, f"t{i}", t0, outcome, {}, tuple(wallets))


def test_a_wallet_record_only_counts_signals_already_settled():
    h = sa.HORIZON_MS
    cases = [_sc(i, T0 + i * 60_000, 0.5, ["w"]) for i in range(3)]  # three wins, close together
    late = _sc(9, T0 + 2 * 60_000 + h, -0.1, ["w"])                   # after all three settled
    early = _sc(8, T0 + 2 * 60_000 + h - 1, -0.1, ["w"])              # one ms before the third settles
    sa.add_wallet_track(cases + [late, early])
    assert late.features["wallet_track_best"] == pytest.approx(0.5)
    assert late.features["wallet_track_good"] == 1
    assert early.features["wallet_track_best"] is None   # only 2 settled: below TRACK_MIN
    assert early.features["wallet_track_hist"] == 0


def test_wallet_records_do_not_cross_chains():
    cases = [_sc(i, T0 + i * 60_000, 0.5, ["w"], chain="sol") for i in range(5)]
    rh = _sc(9, T0 + sa.HORIZON_MS * 2, 0.0, ["w"], chain="robinhood")
    sa.add_wallet_track(cases + [rh])
    assert rh.features["wallet_track_hist"] == 0


def test_top_wallets_rank_per_chain_with_a_minimum_sample():
    cases = [_sc(i, T0 + i, 0.2 if i % 2 else -0.1, ["a", "b"] if i < 6 else ["b"]) for i in range(10)]
    cases += [_sc(20 + i, T0, 1.0, ["lucky"]) for i in range(2)]   # 2 signals: below the minimum
    top = sa.top_wallets(cases)
    names = [w["address"] for w in top]
    assert "lucky" not in names and names[0] in ("a", "b")
