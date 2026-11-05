"""Tests for :mod:`kaiba.learning.exit_study`.

The point of this module is that somebody will act on its numbers, so the tests are
mostly about the ways a study like this lies:

* a policy that quietly books the last available print as an exit when the tape ran out;
* an in-sample number reported without its out-of-sample twin;
* a cost model applied to one leg, or to none;
* a "tighter slippage band would have helped" claim that never prices the refusal;
* a constant with no provenance.

Each of those has a test below that fails if the lie is reintroduced. The synthetic
fixtures are built from the real schema so a column rename breaks the test rather than
silently changing what is measured.
"""

from __future__ import annotations

import sqlite3

import pytest

from kaiba.learning import exit_study as es

# --------------------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE positions (
  position_id TEXT PRIMARY KEY, chain TEXT NOT NULL, token TEXT NOT NULL, lane TEXT NOT NULL,
  mode TEXT NOT NULL, opened_ms INTEGER NOT NULL, closed_ms INTEGER,
  qty TEXT NOT NULL DEFAULT '0', qty_total TEXT NOT NULL DEFAULT '0',
  cost_native TEXT NOT NULL DEFAULT '0', proceeds_native TEXT NOT NULL DEFAULT '0',
  realized_native TEXT NOT NULL DEFAULT '0', entry_price_usd TEXT, peak_price_usd TEXT,
  stop_price_usd TEXT, tp_done_json TEXT NOT NULL DEFAULT '[]', protected INTEGER NOT NULL DEFAULT 0,
  protection_ids_json TEXT NOT NULL DEFAULT '[]', mae_pct REAL, mfe_pct REAL, exit_reason TEXT
);
CREATE TABLE position_marks (
  id INTEGER PRIMARY KEY AUTOINCREMENT, position_id TEXT NOT NULL, ts_ms INTEGER NOT NULL,
  price_usd TEXT NOT NULL, return_pct REAL, mae_pct REAL, mfe_pct REAL
);
CREATE TABLE trades (
  trade_id TEXT PRIMARY KEY, position_id TEXT NOT NULL, decision_id TEXT, lane TEXT NOT NULL,
  mode TEXT NOT NULL, chain TEXT NOT NULL, token TEXT NOT NULL, opened_ms INTEGER NOT NULL,
  closed_ms INTEGER NOT NULL, hold_s INTEGER NOT NULL, cost_native TEXT NOT NULL,
  proceeds_native TEXT NOT NULL, pnl_native TEXT NOT NULL, pnl_pct REAL NOT NULL,
  fees_native TEXT NOT NULL DEFAULT '0', slippage_bps INTEGER, mae_pct REAL, mfe_pct REAL,
  exit_reason TEXT, mistakes_json TEXT NOT NULL DEFAULT '[]', lesson TEXT,
  params_version TEXT NOT NULL DEFAULT 'v1'
);
CREATE TABLE orders (
  order_id TEXT PRIMARY KEY, decision_id TEXT, chain TEXT NOT NULL, token TEXT NOT NULL,
  side TEXT NOT NULL, lane TEXT NOT NULL, mode TEXT NOT NULL, input_token TEXT NOT NULL,
  output_token TEXT NOT NULL, amount_in TEXT NOT NULL, min_out TEXT NOT NULL,
  slippage_bps INTEGER NOT NULL, state TEXT NOT NULL, provider TEXT NOT NULL,
  provider_order_id TEXT, tx_hash TEXT, filled_out TEXT, fee_native TEXT,
  created_ms INTEGER NOT NULL, updated_ms INTEGER NOT NULL, error TEXT
);
CREATE TABLE decisions (
  decision_id TEXT PRIMARY KEY, ts_ms INTEGER NOT NULL, lane TEXT NOT NULL, mode TEXT NOT NULL,
  chain TEXT NOT NULL, token TEXT NOT NULL, action TEXT NOT NULL, thesis TEXT, confidence REAL,
  signals_json TEXT NOT NULL DEFAULT '[]', dossier_grade TEXT, size_base_units INTEGER,
  size_pct_bankroll REAL, expected_return_pct REAL, invalidation TEXT, regime TEXT,
  blockers_json TEXT NOT NULL DEFAULT '[]', params_version TEXT NOT NULL DEFAULT 'v1',
  model TEXT, trace_id TEXT
);
CREATE TABLE swaps (
  id INTEGER PRIMARY KEY AUTOINCREMENT, chain TEXT NOT NULL, tx TEXT NOT NULL, slot INTEGER,
  block_index INTEGER, ts_ms INTEGER NOT NULL, wallet TEXT NOT NULL, token TEXT NOT NULL,
  side TEXT NOT NULL, amount_token TEXT, amount_native TEXT, price_usd TEXT, usd_value TEXT,
  program TEXT, source TEXT NOT NULL, is_create_tx INTEGER NOT NULL DEFAULT 0,
  fee_payer TEXT, amount_quote TEXT, quote_mint TEXT
);
"""

T0 = 1_790_000_000_000


@pytest.fixture()
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    return c


def add_position(
    c: sqlite3.Connection,
    pid: str,
    *,
    token: str = "TOK",
    lane: str = "sm-trenches",
    mode: str = "live",
    opened_ms: int = T0,
    hold_ms: int = 60_000,
    entry: str = "0.001",
    peak: str | None = None,
    cost: int = 100_000_000,
    proceeds: int = 70_000_000,
) -> None:
    c.execute(
        "INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms, closed_ms, "
        "cost_native, proceeds_native, entry_price_usd, peak_price_usd) "
        "VALUES (?,'sol',?,?,?,?,?,?,?,?,?)",
        (pid, token, lane, mode, opened_ms, opened_ms + hold_ms, str(cost), str(proceeds), entry, peak),
    )


def add_prints(
    c: sqlite3.Connection, token: str, base_ms: int, series: list[tuple[int, float]]
) -> None:
    """``series`` is ``(offset_s, price_usd)``."""
    for i, (offset_s, px) in enumerate(series):
        c.execute(
            "INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, amount_token, price_usd, source) "
            "VALUES ('sol',?,?,'w',?,'buy','1',?,'test')",
            (f"{token}-{base_ms}-{i}", base_ms + offset_s * 1000, token, str(px)),
        )


def add_sell(
    c: sqlite3.Connection,
    oid: str,
    *,
    token: str = "TOK",
    mode: str = "live",
    created_ms: int,
    updated_ms: int,
    min_out: int,
    filled_out: int | None,
    slippage_bps: int = 2500,
    state: str = "filled",
    error: str | None = None,
) -> None:
    c.execute(
        "INSERT INTO orders (order_id, chain, token, side, lane, mode, input_token, output_token, "
        "amount_in, min_out, slippage_bps, state, provider, filled_out, created_ms, updated_ms, error) "
        "VALUES (?,'sol',?,'sell','sm-trenches',?,?,'SOL','1',?,?,?,'gmgn',?,?,?,?)",
        (
            oid,
            token,
            mode,
            token,
            str(min_out),
            slippage_bps,
            state,
            None if filled_out is None else str(filled_out),
            created_ms,
            updated_ms,
            error,
        ),
    )


def path(points: list[tuple[int, float]], *, last: int | None = None, mode: str = "live") -> es.PricePath:
    pts = tuple((t * 1000, g) for t, g in points)
    return es.PricePath(
        position_id="p",
        token="TOK",
        lane="sm-trenches",
        mode=mode,
        opened_ms=T0,
        closed_ms=T0 + 60_000,
        points=pts,
        last_point_ms=pts[-1][0] if last is None else last,
        realised_gross=None,
    )


# --------------------------------------------------------------------------------------
# cost model: both legs, always
# --------------------------------------------------------------------------------------


def test_round_trip_cost_is_two_legs_and_is_charged_on_a_flat_exit():
    assert es.ROUND_TRIP_COST_PCT == pytest.approx(2 * es.LEG_COST_PCT, abs=1e-9)
    flat = es.fixed_stop(path([(0, 1.0), (10, 1.0), (20, 1.0)]), -30.0)
    # Exiting exactly where we entered is a loss of the whole round trip, not break-even.
    assert flat.net_return_pct == pytest.approx(-es.ROUND_TRIP_COST_PCT, abs=1e-9)
    assert flat.net_return_pct < 0


def test_a_policy_needs_more_than_the_cost_to_show_a_profit():
    # +2.0% gross is a LOSS once both legs are paid. The repo rule, as arithmetic.
    r = es.time_stop(path([(0, 1.0), (40, 1.02)]), 30)
    assert r.net_return_pct < 0


# --------------------------------------------------------------------------------------
# policy semantics
# --------------------------------------------------------------------------------------


def test_fixed_stop_fires_on_the_first_breaching_print_not_the_worst():
    p = path([(0, 1.0), (10, 0.85), (20, 0.65), (30, 0.30)])
    r = es.fixed_stop(p, -30.0)
    assert r.exit_offset_ms == 20_000
    assert r.net_return_pct == pytest.approx(es._net(0.65))
    assert not r.censored


def test_fixed_stop_that_never_breaches_is_censored_not_a_free_win():
    p = path([(0, 1.0), (10, 1.5), (20, 2.0)])
    r = es.fixed_stop(p, -30.0)
    assert r.censored is True
    assert r.exit_offset_ms == 20_000


def test_trailing_stop_measures_from_the_running_peak():
    p = path([(0, 1.0), (10, 2.0), (20, 1.85), (30, 1.7)])
    r = es.trailing_stop(p, -10.0)
    # 10% off a peak of 2.0 is 1.80: 1.85 survives, 1.7 does not.
    assert r.exit_offset_ms == 30_000
    assert r.net_return_pct == pytest.approx(es._net(1.7))


def test_trailing_stop_peak_is_seeded_at_entry_so_it_can_fire_before_any_gain():
    p = path([(0, 1.0), (10, 0.8), (20, 0.5)])
    r = es.trailing_stop(p, -10.0)
    assert r.exit_offset_ms == 10_000
    assert not r.censored


def test_trailing_stop_peak_is_entry_not_the_first_print_when_the_tape_opens_below_us():
    # Our fill is the reference. The tape's first print is a stranger's trade a second
    # later; seeding the peak from it would let a position that only ever fell look as
    # though it had never given anything back.
    p = path([(0, 0.95), (10, 0.88)])
    r = es.trailing_stop(p, -10.0)
    assert r.exit_offset_ms == 10_000  # 10% off entry is 0.90, and 0.88 is through it
    assert not r.censored


def test_take_profit_ladder_scales_out_and_leaves_the_rest_on_the_stop():
    p = path([(0, 1.0), (10, 1.30), (20, 0.60)])
    r = es.take_profit_ladder(p, [(25.0, 0.5)], stop_pct=-30.0)
    # Half at 1.30, half at 0.60.
    assert r.net_return_pct == pytest.approx(es._net(0.5 * 1.30 + 0.5 * 0.60))
    assert r.reason == "ladder:stop"


def test_take_profit_ladder_completes_when_every_rung_fires():
    p = path([(0, 1.0), (10, 1.30), (20, 1.60)])
    r = es.take_profit_ladder(p, [(25.0, 0.5), (50.0, 0.5)], stop_pct=-90.0)
    assert r.reason == "ladder:complete"
    assert not r.censored
    assert r.net_return_pct == pytest.approx(es._net(0.5 * 1.30 + 0.5 * 1.60))


def test_time_stop_refuses_a_stale_print_rather_than_booking_a_late_exit_as_punctual():
    # The only print after +30 s is four minutes late. Booking it as a 30-second trade is
    # the cheapest way to manufacture an edge, so the policy must censor instead.
    p = path([(0, 1.0), (10, 1.0), (300, 8.0)])
    r = es.time_stop(p, 30)
    assert r.censored is True
    assert "stale" in r.reason
    assert r.net_return_pct == pytest.approx(es._net(8.0))  # reported, but flagged


def test_hold_to_is_censored_whenever_the_tape_ends_first():
    p = path([(0, 1.0), (60, 0.5)])
    r = es.hold_to(p, 3600)
    assert r.censored is True


def test_every_shipped_policy_marks_censoring_when_the_tape_is_short():
    p = path([(0, 1.0), (1, 1.0), (2, 1.0)])  # three prints, two seconds, nothing happens
    for name, fn in es.POLICIES.items():
        r = fn(p)
        if name.startswith("stop") or name.startswith("trail"):
            assert r.censored, name
        assert isinstance(r.net_return_pct, float), name


# --------------------------------------------------------------------------------------
# paths and the censoring census
# --------------------------------------------------------------------------------------


def test_load_paths_counts_what_it_dropped(conn):
    add_position(conn, "p_ok", token="A")
    add_prints(conn, "A", T0, [(0, 0.001), (10, 0.0008), (20, 0.0005)])
    add_position(conn, "p_thin", token="B")
    add_prints(conn, "B", T0, [(0, 0.001)])
    conn.execute(
        "INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms, closed_ms, "
        "cost_native, proceeds_native, entry_price_usd) VALUES "
        "('p_noentry','sol','C','sm-trenches','live',?,?,'1','1',NULL)",
        (T0, T0 + 1000),
    )
    paths, census = load = es.load_paths(conn)
    assert census == {"closed": 3, "no_entry_price": 1, "too_few_prints": 1, "replayable": 1}
    assert [p.position_id for p in paths] == ["p_ok"]
    assert load[0][0].points[0] == (0, pytest.approx(1.0))


def add_decision(
    c: sqlite3.Connection, did: str, *, token: str, ts_ms: int, action: str = "enter"
) -> None:
    c.execute(
        "INSERT INTO decisions (decision_id, ts_ms, lane, mode, chain, token, action) "
        "VALUES (?,?,'sm-trenches','live','sol',?,?)",
        (did, ts_ms, token, action),
    )


def test_decision_paths_anchor_on_the_first_print_after_the_decision_not_before(conn):
    add_decision(conn, "d1", token="A", ts_ms=T0)
    # The 0.002 print is BEFORE the decision and must not become the anchor.
    add_prints(conn, "A", T0, [(-30, 0.002), (5, 0.001), (20, 0.0012), (40, 0.0005)])
    paths, census = es.load_decision_paths(conn)
    assert census["replayable"] == 1
    (p,) = paths
    assert p.points[0] == (5_000, pytest.approx(1.0))
    assert p.points[1][1] == pytest.approx(1.2)


def test_decision_paths_refuse_an_anchor_that_arrives_far_too_late(conn):
    add_decision(conn, "d1", token="A", ts_ms=T0)
    add_prints(conn, "A", T0, [(600, 0.001), (610, 0.0012), (620, 0.0014)])
    paths, census = es.load_decision_paths(conn)
    assert paths == []
    assert census["no_anchor"] == 1


def test_decision_paths_keep_the_skip_cohort_separate_from_the_enter_cohort(conn):
    add_decision(conn, "d_in", token="A", ts_ms=T0, action="enter")
    add_decision(conn, "d_out", token="B", ts_ms=T0, action="skip")
    for tok in ("A", "B"):
        add_prints(conn, tok, T0, [(1, 0.001), (10, 0.0009), (20, 0.0006)])
    entered, _ = es.load_decision_paths(conn, actions=("enter",))
    skipped, _ = es.load_decision_paths(conn, actions=("skip",))
    assert [p.position_id for p in entered] == ["d_in"]
    assert [p.position_id for p in skipped] == ["d_out"]


def test_decision_paths_docstring_says_the_cohort_is_an_upper_bound():
    doc = " ".join((es.load_decision_paths.__doc__ or "").split())
    assert "upper bound" in doc
    assert "No look-ahead in the anchor" in doc


def test_path_at_refuses_a_stale_print(conn):
    p = path([(0, 1.0), (10, 0.9)])
    assert p.at(20_000) == (10_000, pytest.approx(0.9))
    assert p.at(10_000 + es.MAX_PRINT_STALENESS_MS + 1) is None


# --------------------------------------------------------------------------------------
# the exit gap decomposition
# --------------------------------------------------------------------------------------


def test_exit_gap_components_sum_to_the_whole_miss(conn):
    add_position(conn, "p1", cost=100_000_000, proceeds=60_000_000, hold_ms=40_000)
    # mark at send = min_out / 0.75 = 80_000_000 -> -20%; realised -40%.
    add_sell(
        conn,
        "o1",
        created_ms=T0 + 30_000,
        updated_ms=T0 + 35_000,
        min_out=60_000_000,
        filled_out=60_000_000,
    )
    conn.execute("UPDATE positions SET exit_reason='stop_loss' WHERE position_id='p1'")
    (g,) = es.exit_gaps(conn)
    assert g.threshold_applies is True
    assert g.declared_stop_pct == es.DECLARED_STOP_PCT
    assert g.mark_at_send_pct == pytest.approx(-20.0)
    assert g.realised_pct == pytest.approx(-40.0)
    assert g.evaluation_gap_pp + g.fill_gap_pp == pytest.approx(g.realised_pct - g.declared_stop_pct)
    assert g.fill_over_mark == pytest.approx(0.75)
    assert g.submit_lag_s == pytest.approx(5.0)


def test_exit_gap_scores_an_emergency_exit_against_the_emergency_threshold(conn):
    # Charging an emergency_loss exit against the -30% stop invents 20pp of evaluation lag.
    add_position(conn, "p1", cost=100_000_000, proceeds=45_000_000, hold_ms=40_000)
    conn.execute("UPDATE positions SET exit_reason='emergency_loss' WHERE position_id='p1'")
    add_sell(
        conn,
        "o1",
        created_ms=T0 + 30_000,
        updated_ms=T0 + 35_000,
        min_out=41_250_000,
        filled_out=45_000_000,
    )
    (g,) = es.exit_gaps(conn)
    assert g.declared_stop_pct == es.DECLARED_EMERGENCY_PCT == -50.0
    assert g.mark_at_send_pct == pytest.approx(-45.0)
    assert g.evaluation_gap_pp == pytest.approx(5.0)


def test_exit_gap_gives_a_rug_exit_no_evaluation_gap_at_all(conn):
    add_position(conn, "p1", cost=100_000_000, proceeds=20_000_000, hold_ms=40_000)
    conn.execute("UPDATE positions SET exit_reason='rug:lp_-57.0pct' WHERE position_id='p1'")
    add_sell(
        conn,
        "o1",
        created_ms=T0 + 30_000,
        updated_ms=T0 + 35_000,
        min_out=15_000_000,
        filled_out=20_000_000,
    )
    (g,) = es.exit_gaps(conn)
    assert g.threshold_applies is False
    assert g.evaluation_gap_pp == 0.0


def test_exit_gap_inverts_whatever_band_the_order_actually_carried(conn):
    # A future config change must not silently reinterpret old orders: the band comes from
    # the row, not from the module constant.
    add_position(conn, "p1", cost=100_000_000, proceeds=90_000_000, hold_ms=40_000)
    add_sell(
        conn,
        "o1",
        created_ms=T0 + 30_000,
        updated_ms=T0 + 31_000,
        min_out=90_000_000,
        filled_out=90_000_000,
        slippage_bps=1000,
    )
    (gap,) = es.exit_gaps(conn)
    assert gap.mark_at_send_pct == pytest.approx(0.0)  # 90/0.9 = 100 = cost
    assert gap.fill_over_mark == pytest.approx(0.9)


def test_exit_gap_records_a_sell_that_never_went_out(conn):
    add_position(conn, "p1", cost=100_000_000, proceeds=10_000_000, hold_ms=900_000)
    add_sell(
        conn,
        "refused",
        created_ms=T0 + 100_000,
        updated_ms=T0 + 200_000,
        min_out=45_000_000,
        filled_out=None,
        state="failed",
        error="confirmation required",
    )
    add_sell(
        conn,
        "later",
        created_ms=T0 + 800_000,
        updated_ms=T0 + 890_000,
        min_out=7_500_000,
        filled_out=10_000_000,
    )
    (gap,) = es.exit_gaps(conn)
    assert gap.order_id == "later"  # the fill that actually happened
    assert gap.failed_attempts and gap.failed_attempts[0][0] == "refused"
    assert "confirmation" in gap.failed_attempts[0][1]


def gap(ratio: float, *, realised: float = -50.0, threshold_applies: bool = True) -> es.ExitGap:
    return es.ExitGap(
        position_id="p",
        lane="sm-trenches",
        mode="live",
        order_id="o",
        declared_stop_pct=-30.0,
        exit_reason="stop_loss",
        threshold_applies=threshold_applies,
        mark_at_send_pct=-35.0,
        realised_pct=realised,
        evaluation_gap_pp=-5.0,
        fill_gap_pp=realised + 35.0,
        fill_over_mark=ratio,
        send_lag_s=1.0,
        submit_lag_s=1.0,
        failed_attempts=(),
    )


def test_gap_verdict_will_not_call_a_fill_effect_separable_when_it_straddles_one(conn):
    gaps = [
        gap(ratio, realised=r_pct)
        for r_pct, ratio in ((-2.0, 1.50), (-48.7, 0.78), (-31.1, 1.02), (-52.8, 0.79))
    ]
    v = es.gap_verdict(gaps)
    assert v.n == 4
    assert v.fill_over_mark_ci95 is not None
    lo, hi = v.fill_over_mark_ci95
    assert lo < 1.0 < hi
    assert v.fill_effect_separable is False
    assert "not separable" in v.dominant


def test_gap_verdict_does_call_it_separable_when_every_fill_is_genuinely_bad():
    gaps = [gap(r) for r in (0.70, 0.72, 0.68, 0.71, 0.69, 0.73)]
    v = es.gap_verdict(gaps)
    assert v.fill_effect_separable is True
    assert v.fill_over_mark_ci95 is not None and v.fill_over_mark_ci95[1] < 1.0


def test_gap_verdict_bootstrap_is_deterministic():
    gaps = [gap(r) for r in (0.8, 1.1, 0.95, 1.4)]
    assert es.gap_verdict(gaps).fill_over_mark_ci95 == es.gap_verdict(gaps).fill_over_mark_ci95


def test_gap_verdict_excludes_exits_that_had_no_price_threshold_from_the_evaluation_mean():
    # A rug exit is triggered by a liquidity drop, not a price level. Averaging it into
    # the evaluation gap would charge it for missing a stop nobody set.
    gaps = [gap(1.0), gap(1.0, threshold_applies=False)]
    gaps = [
        es.ExitGap(**{**g.__dict__, "evaluation_gap_pp": ev})
        for g, ev in zip(gaps, (-4.0, -999.0), strict=True)
    ]
    assert es.gap_verdict(gaps).mean_evaluation_gap_pp == pytest.approx(-4.0)


# --------------------------------------------------------------------------------------
# in-sample never travels alone
# --------------------------------------------------------------------------------------


def test_policy_report_cannot_be_built_without_its_out_of_sample_twin():
    empty = es.PolicySummary(0, 0, None, None, None, None, None)
    with pytest.raises(TypeError):
        es.PolicyReport(  # type: ignore[call-arg]
            name="x", overall=empty, in_sample=empty, dsr_overall=None, dsr_notes=()
        )


def test_replicated_needs_both_a_positive_oos_mean_and_a_deflated_sharpe():
    good = es.PolicySummary(20, 0, 30.0, 5.0, 60.0, -10.0, 100.0)
    empty = es.PolicySummary(0, 0, None, None, None, None, None)
    assert not es.PolicyReport("x", good, good, good, empty, 0.99, 0.10, ()).replicated
    assert not es.PolicyReport(
        "x", good, good, es.PolicySummary(20, 0, -1.0, 0.0, 10.0, -9.0, 1.0), empty, 0.99, 0.99, ()
    ).replicated
    assert es.PolicyReport("x", good, good, good, empty, 0.99, 0.99, ()).replicated


def test_study_splits_by_time_into_disjoint_covering_halves(conn):
    for i in range(6):
        tok = f"T{i}"
        add_position(conn, f"p{i}", token=tok, opened_ms=T0 + i * 600_000)
        add_prints(conn, tok, T0 + i * 600_000, [(0, 0.001), (10, 0.0009), (20, 0.0006)])
    s = es.study(conn)
    assert s.n_in_sample + s.n_out_of_sample == s.eligibility["replayable"] == 6
    assert s.n_in_sample > 0 and s.n_out_of_sample > 0
    for report in s.policies:
        assert report.in_sample.n + report.out_of_sample.n == report.overall.n


def test_study_on_an_empty_database_says_so_instead_of_returning_zeroes(conn):
    s = es.study(conn)
    assert s.eligibility["replayable"] == 0
    assert s.caveats == ("no replayable positions",)
    assert all(r.overall.mean_pct is None for r in s.policies)
    assert s.any_policy_replicated is False


def test_study_flags_the_censored_control_and_the_short_corpus(conn):
    for i in range(4):
        tok = f"T{i}"
        add_position(conn, f"p{i}", token=tok, opened_ms=T0 + i * 60_000)
        add_prints(conn, tok, T0 + i * 60_000, [(0, 0.001), (10, 0.0009), (20, 0.0006)])
    s = es.study(conn)
    text = " ".join(s.caveats)
    assert "held-out regime" in text
    # The span must be printed, not merely alluded to: "one session" is an opinion,
    # "0.1 hours" is the fact a reader can argue with.
    assert f"corpus spans {s.span_hours:.1f} hours" in text
    assert "hold-to-+60min control is censored" in text
    control = next(r for r in s.policies if r.name == "hold-60min(control)")
    assert control.overall.censored == control.overall.n


# --------------------------------------------------------------------------------------
# excursion evidence
# --------------------------------------------------------------------------------------


def test_excursions_reports_position_marks_emptiness_rather_than_hiding_it(conn):
    add_position(conn, "p1", entry="0.001", peak="0.0012")
    add_prints(conn, "TOK", T0, [(0, 0.001), (10, 0.0011), (20, 0.0007)])
    e = es.excursions(conn, mode="live")
    assert e.marks_rows == 0
    assert e.marks_for_live == 0
    assert e.peak_mfe_median_pct == pytest.approx(20.0)
    assert e.tape_mfe_median_pct == pytest.approx(10.0)
    assert e.tape_mae_median_pct == pytest.approx(-30.0)


def test_excursions_counts_against_the_shipped_first_take_profit_rung(conn):
    add_position(conn, "a", token="A", entry="1", peak="1.30")
    add_position(conn, "b", token="B", entry="1", peak="2.50")
    e = es.excursions(conn, mode="live")
    assert e.first_tp_rung_pct == es.FIRST_TP_RUNG_PCT == 100.0
    assert e.reached_25pct == 2
    assert e.reached_100pct == 1
    assert e.rung_reachable == 1


# --------------------------------------------------------------------------------------
# the slippage band, with the refusal priced
# --------------------------------------------------------------------------------------


def _band_fixture(conn: sqlite3.Connection, *, retry_price: float) -> None:
    add_position(conn, "p1", cost=100_000_000, proceeds=60_000_000, hold_ms=40_000, entry="1.0")
    add_sell(
        conn,
        "o1",
        created_ms=T0 + 30_000,
        updated_ms=T0 + 35_000,
        min_out=60_000_000,
        filled_out=60_000_000,
    )
    add_prints(conn, "TOK", T0, [(0, 1.0), (35, 0.60), (45, retry_price), (50, retry_price)])


def test_a_tighter_band_that_refuses_a_fill_is_charged_the_price_it_gets_later(conn):
    _band_fixture(conn, retry_price=0.40)  # kept falling while we waited
    gaps = es.exit_gaps(conn)
    paths, _ = es.load_paths(conn)
    scen = {b.band_bps: b for b in es.slippage_band_scenarios(conn, gaps, paths, bands_bps=(2500, 2000))}
    assert scen[2500].exits_rejected == 0
    assert scen[2500].delta_pp == pytest.approx(0.0)
    assert scen[2000].exits_rejected == 1
    # Drift 0.40/0.60 applied to a realised -40% -> -60%. The tighter band is worse.
    assert scen[2000].mean_modelled_pct == pytest.approx(-60.0, abs=1e-6)
    assert scen[2000].delta_pp < 0


def test_a_tighter_band_gets_credit_when_the_price_actually_recovered(conn):
    _band_fixture(conn, retry_price=0.90)
    gaps = es.exit_gaps(conn)
    paths, _ = es.load_paths(conn)
    (scen,) = es.slippage_band_scenarios(conn, gaps, paths, bands_bps=(2000,))
    assert scen.exits_rejected == 1
    assert scen.delta_pp > 0
    assert scen.rejected_detail[0][0] == "p1"


def test_band_scenarios_never_claim_to_price_the_never_fills_tail(conn):
    _band_fixture(conn, retry_price=0.40)
    gaps = es.exit_gaps(conn)
    paths, _ = es.load_paths(conn)
    for scen in es.slippage_band_scenarios(conn, gaps, paths):
        assert scen.retry_optimistic is True
        assert scen.never_filled_risk_pp == pytest.approx(48.7)


def test_band_scenario_calls_a_retry_unpriceable_when_the_tape_cannot_resolve_it(conn):
    add_position(conn, "p1", cost=100_000_000, proceeds=60_000_000, hold_ms=40_000, entry="1.0")
    add_sell(
        conn,
        "o1",
        created_ms=T0 + 30_000,
        updated_ms=T0 + 35_000,
        min_out=60_000_000,
        filled_out=60_000_000,
    )
    # One print covers both the fill and the retry moment: drift would be exactly 1.0,
    # which is "we cannot see", not "nothing happened".
    add_prints(conn, "TOK", T0, [(0, 1.0), (5, 0.9), (30, 0.60)])
    gaps = es.exit_gaps(conn)
    paths, _ = es.load_paths(conn)
    (scen,) = es.slippage_band_scenarios(conn, gaps, paths, bands_bps=(2000,))
    assert scen.exits_rejected == 1
    assert scen.unpriceable_retries == 1
    assert scen.delta_pp == pytest.approx(0.0)


# --------------------------------------------------------------------------------------
# provenance and shape
# --------------------------------------------------------------------------------------


def test_every_public_constant_has_a_provenance_entry():
    numeric = {
        name
        for name, value in vars(es).items()
        if name.isupper() and isinstance(value, (int, float)) and not isinstance(value, bool)
    }
    missing = numeric - set(es.CONSTANT_PROVENANCE)
    assert not missing, f"constants with no provenance: {sorted(missing)}"


def test_provenance_uses_the_repository_vocabulary_and_says_invented_where_it_is():
    allowed = ("MEASURED", "INVENTED", "DERIVED", "DEFINITIONAL", "STRUCTURAL")
    for name, text in es.CONSTANT_PROVENANCE.items():
        assert any(word in text for word in allowed), f"{name}: {text}"
    invented = [n for n, t in es.CONSTANT_PROVENANCE.items() if "INVENTED" in t]
    assert invented, "a study with no invented constants is a study that is hiding some"
    assert "MIN_PATH_POINTS" in invented


def test_module_states_that_position_marks_cannot_carry_the_replay():
    doc = es.__doc__ or ""
    assert "position_marks" in doc
    flat = " ".join(doc.split())
    assert "88 rows for 72 positions, zero of them for any of the six live positions" in flat


def test_render_prints_every_section_and_the_caveats(conn):
    for i in range(4):
        tok = f"T{i}"
        add_position(conn, f"p{i}", token=tok, opened_ms=T0 + i * 60_000, peak="0.0012")
        add_prints(conn, tok, T0 + i * 60_000, [(0, 0.001), (10, 0.0011), (20, 0.0006)])
        add_sell(
            conn,
            f"o{i}",
            token=tok,
            created_ms=T0 + i * 60_000 + 30_000,
            updated_ms=T0 + i * 60_000 + 35_000,
            min_out=60_000_000,
            filled_out=70_000_000,
        )
    text = es.render(es.study(conn))
    for heading in ("THE EXIT GAP", "COUNTERFACTUAL EXIT POLICIES", "FAVOURABLE EXCURSION", "CAVEATS"):
        assert heading in text
    assert "position_marks:" in text
    assert "round-trip cost charged: 2.18%" in text


def test_study_is_read_only(conn):
    add_position(conn, "p1")
    add_prints(conn, "TOK", T0, [(0, 0.001), (10, 0.0009), (20, 0.0006)])
    before = conn.total_changes
    es.study(conn)
    assert conn.total_changes == before
