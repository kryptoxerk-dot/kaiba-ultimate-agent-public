"""What the hold study must not be allowed to do.

The findings this module produces are all negative, and a negative finding is only worth
anything if the machinery that produced it would have produced a positive one had there
been one there. So these tests are mostly traps, in the order in which they would have
fooled us:

* a forward window that reaches back to the entry print and books 0.00% for a mint that
  never traded again (``test_forward_price_never_reuses_the_entry_print``);
* a horizon that drops the mints that stopped printing, so the survivors carry the mean
  (``test_observed_only_is_the_optimistic_end_of_the_bracket``);
* a coverage boundary read as a death (``test_censored_anchor_is_excluded_not_booked``);
* a gross mean that never meets the fee (``test_cost_floor_is_charged_on_both_legs``);
* the realised hold/PnL table read as a hold rule
  (``test_realised_table_ships_its_confound``);
* a hold rule justified on one half of the tape
  (``test_split_is_by_time_and_disjoint``).

Every fixture is synthetic and hand-computable. The point is not to reproduce the live
numbers - those move - but to fix the arithmetic and the refusals.
"""

from __future__ import annotations

import pytest

from kaiba.learning import hold_study as hs

SOL = "So11111111111111111111111111111111111111112"
T0 = 1_700_000_000_000


# --------------------------------------------------------------------------- helpers


def _swap(conn, *, token, ts_ms, price, usd=100.0, side="buy", source="pumpfun:trades",
          chain="sol", wallet="w1", tx=None):
    conn.execute(
        "INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, amount_token, "
        "price_usd, usd_value, source) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (chain, tx or f"tx-{token[:6]}-{ts_ms}-{side}-{price}", ts_ms, wallet, token,
         side, "1000", str(price), str(usd), source),
    )


def _tape(conn, *, token, created_ms, covered_to_ms, chain="sol", swaps_total=5):
    conn.execute(
        "INSERT INTO token_tape (chain, token, model, coverage, route, proof, reason, "
        "covered_from_ms, covered_to_ms, created_ms, swaps_total, first_seen_ms, updated_ms) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (chain, token, "m", "complete", "pumpfun:trades", "proof", "ok",
         created_ms - 1, covered_to_ms, created_ms, swaps_total, created_ms, covered_to_ms),
    )


def _series(points, *, token="T", chain="sol", usd=100.0):
    s = hs.Series(chain=chain, token=token)
    for ts, px in points:
        s.prints.append(hs.Print(ts, px, usd, "buy", "pumpfun:trades"))
    return s.seal()


# ------------------------------------------------------------------ the cost floor


def test_cost_floor_is_charged_on_both_legs_and_flat_costs_are_not_bps():
    c = hs.COST_RENT_RECOVERED
    # 1% off the way in, 1% off the way out, plus 2 x 0.00005 SOL on a 0.056 SOL ticket.
    expected = 0.99 * 1.0 * 0.99 - 1.0 - (100_000 / 56_000_000)
    assert c.net(0.0) == pytest.approx(expected)
    assert c.net(0.0) < -0.021, "a flat round trip must be a loss, not a wash"
    assert c.breakeven_gross > 0.022


def test_unresolved_ata_rent_moves_the_floor_and_is_reported_both_ways():
    lost = hs.COST_RENT_LOST.breakeven_gross
    kept = hs.COST_RENT_RECOVERED.breakeven_gross
    assert lost > kept
    # 0.00204 SOL on a 0.056 SOL ticket is 3.6 percentage points of breakeven.
    # the rent divides by (1-fee)^2 like every other flat cost, so it is slightly more
    # than its raw share of the ticket
    assert lost - kept == pytest.approx((2_040_000 / 56_000_000) / 0.99 ** 2, rel=1e-6)
    assert lost - kept > 0.036
    assert "UNRESOLVED" in hs.PROVENANCE["ata_rent_lamports"]


def test_every_constant_declares_measured_or_invented():
    vocabulary = ("MEASURED", "INVENTED", "DERIVED", "DEFINITIONAL", "STRUCTURAL")
    assert hs.PROVENANCE, "the provenance table may not be empty"
    for name, text in hs.PROVENANCE.items():
        assert any(w in text for w in vocabulary), name
    invented = [n for n, t in hs.PROVENANCE.items() if "INVENTED" in t]
    assert invented, "a study with no invented constants has hidden one"
    for name in invented:
        assert "would settle it" in hs.PROVENANCE[name], (
            f"{name} is INVENTED and must say what would settle it"
        )


# ------------------------------------------------------- the entry-print collision


def test_forward_price_never_reuses_the_entry_print():
    """A mint that traded once and never again has NO forward price, not a 0% return.

    This is the artefact that made the first pass of this study report a median gross of
    exactly 0.00% at every horizon at or below the window width.
    """
    s = _series([(T0, 1.0)])
    assert s.price_at(T0 + 30_000, 60_000, after_ms=T0) is None
    assert hs._returns_at(s, T0, 30, window_ms=60_000) is None
    # and the unguarded lookup is exactly what would have gone wrong
    assert s.price_at(T0 + 30_000, 60_000) == 1.0


def test_forward_window_is_capped_at_half_the_horizon():
    assert hs._forward_window_ms(15, 60_000) == 7_500
    assert hs._forward_window_ms(3600, 60_000) == 60_000
    s = _series([(T0, 1.0), (T0 + 12_000, 2.0)])
    # a print 2 s from the 10 s horizon is inside the 5 s cap
    assert hs._returns_at(s, T0, 10, window_ms=60_000) == pytest.approx(1.0)
    # the same print is 18 s from the 30 s horizon, outside the 15 s cap
    assert hs._returns_at(s, T0, 30, window_ms=60_000) is None


def test_price_at_takes_the_nearest_print_not_the_last_one():
    s = _series([(T0, 1.0), (T0 + 50_000, 2.0), (T0 + 70_000, 4.0)])
    assert s.price_at(T0 + 60_000, 30_000, after_ms=T0) in (2.0, 4.0)
    # 70_000 is 10 s away, 50_000 is 10 s away: a tie, but 55_000 is unambiguous
    assert s.price_at(T0 + 55_000, 30_000, after_ms=T0) == 2.0
    assert s.price_at(T0 + 68_000, 30_000, after_ms=T0) == 4.0


# ------------------------------------------------------------- the imputation bracket


def test_observed_only_is_the_optimistic_end_of_the_bracket():
    """Two mints: one doubles and keeps printing, one stops dead. The bracket must span."""
    alive = _series([(T0, 1.0), (T0 + 300_000, 2.0)], token="ALIVE")
    dead = _series([(T0, 1.0)], token="DEAD")
    tape = {"ALIVE": alive, "DEAD": dead}
    anchors = [
        hs.Anchor("sol", "ALIVE", T0, "P", censor_ms=T0 + 10_000_000),
        hs.Anchor("sol", "DEAD", T0, "P", censor_ms=T0 + 10_000_000),
    ]
    got = {
        imp: hs.hold_curve(anchors, tape, grid=(300,), imputation=imp).points[0]
        for imp in hs.IMPUTATIONS
    }
    assert got["observed_only"].n_priced == 1
    assert got["observed_only"].mean_gross_pct == pytest.approx(100.0)
    assert got["last_price"].n_priced == 2
    assert got["last_price"].mean_gross_pct == pytest.approx(50.0)   # +100% and 0%
    assert got["dead_is_zero"].n_priced == 2
    assert got["dead_is_zero"].mean_gross_pct == pytest.approx(0.0)  # +100% and -100%
    assert (
        got["dead_is_zero"].mean_gross_pct
        < got["last_price"].mean_gross_pct
        < got["observed_only"].mean_gross_pct
    ), "the three imputations must bracket, or the bracket is decoration"


def test_coverage_column_exposes_how_much_of_the_sample_was_dropped():
    alive = _series([(T0, 1.0), (T0 + 300_000, 2.0)], token="ALIVE")
    tape = {"ALIVE": alive, "DEAD": _series([(T0, 1.0)], token="DEAD")}
    anchors = [
        hs.Anchor("sol", "ALIVE", T0, "P", censor_ms=T0 + 10_000_000),
        hs.Anchor("sol", "DEAD", T0, "P", censor_ms=T0 + 10_000_000),
    ]
    pt = hs.hold_curve(anchors, tape, grid=(300,), imputation="observed_only").points[0]
    assert pt.n_eligible == 2 and pt.n_priced == 1 and pt.coverage == 0.5


def test_unknown_imputation_is_refused_rather_than_guessed():
    s = _series([(T0, 1.0)])
    with pytest.raises(ValueError):
        hs._returns_at(s, T0, 300, window_ms=60_000, imputation="optimistic")


# --------------------------------------------------------------------- censoring


def test_censored_anchor_is_excluded_not_booked():
    """Past the coverage boundary we know nothing, and nothing is not zero."""
    s = _series([(T0, 1.0), (T0 + 30_000, 2.0)])
    anchors = [hs.Anchor("sol", "T", T0, "P", censor_ms=T0 + 60_000)]
    tape = {"T": s}
    inside = hs.hold_curve(anchors, tape, grid=(30,), imputation="dead_is_zero").points[0]
    beyond = hs.hold_curve(anchors, tape, grid=(300,), imputation="dead_is_zero").points[0]
    assert inside.n_eligible == 1
    assert beyond.n_eligible == 0, "a horizon past the coverage end is not measurable"
    assert beyond.mean_gross_pct is None, "and it must not be booked as a loss either"


def test_anchors_curve_launch_carries_the_coverage_boundary(tmp_db):
    _tape(tmp_db, token="A", created_ms=T0, covered_to_ms=T0 + 45_000)
    _swap(tmp_db, token="A", ts_ms=T0, price=1.0)
    _swap(tmp_db, token="A", ts_ms=T0 + 20_000, price=1.5)
    got = hs.anchors_curve_launch(tmp_db)
    assert len(got) == 1
    assert got[0].t0_ms == T0
    assert got[0].censor_ms == T0 + 45_000
    assert got[0].population == "POP_CURVE_LAUNCH"


def test_smart_money_anchor_is_the_first_buy_and_ignores_the_wrapped_sol_leg(tmp_db):
    _swap(tmp_db, token=SOL, ts_ms=T0 - 1_000, price=118.0, side="sell",
          source="gmgn:smartmoney")
    _swap(tmp_db, token="M", ts_ms=T0, price=0.001, side="buy", source="gmgn:smartmoney")
    _swap(tmp_db, token="M", ts_ms=T0 + 5_000, price=0.002, side="buy",
          source="gmgn:smartmoney")
    got = hs.anchors_smart_money(tmp_db)
    assert [a.token for a in got] == ["M"], "the SOL leg is not a token anchor"
    assert got[0].t0_ms == T0
    assert got[0].lane == "sm-trenches"


def test_load_series_drops_the_wrapped_sol_leg_and_unpriced_rows(tmp_db):
    _swap(tmp_db, token=SOL, ts_ms=T0, price=118.0, source="gmgn:smartmoney")
    _swap(tmp_db, token="M", ts_ms=T0, price=0.001, source="gmgn:smartmoney")
    tmp_db.execute(
        "INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, amount_token, source) "
        "VALUES ('sol','tx-null',?,'w','M','buy','1','gmgn:smartmoney')", (T0 + 1_000,)
    )
    series = hs.load_series(tmp_db, chain="sol")
    assert set(series) == {"M"}
    assert len(series["M"].prints) == 1, "a row with no price is not a price"


# --------------------------------------------------------------------- survival


def test_survival_separates_having_a_price_from_being_able_to_leave():
    """A single dust print is a price and is not an exit."""
    thin = _series([(T0, 1.0), (T0 + 300_000, 1.0)], token="THIN", usd=0.5)
    deep = _series([(T0, 1.0), (T0 + 300_000, 1.0)], token="DEEP", usd=10_000.0)
    anchors = [
        hs.Anchor("sol", "THIN", T0, "P", censor_ms=T0 + 10_000_000),
        hs.Anchor("sol", "DEEP", T0, "P", censor_ms=T0 + 10_000_000),
    ]
    pt = hs.survival_curve(anchors, {"THIN": thin, "DEEP": deep}, grid=(300,))[0]
    assert pt.n_eligible == 2
    assert pt.n_with_price == 2
    assert pt.n_exitable == 1, "a 0.50 USD print cannot absorb our ticket"


def test_survival_window_width_is_reported_at_three_settings():
    s = _series([(T0, 1.0), (T0 + 300_000 + 90_000, 1.0)], token="T", usd=10_000.0)
    anchors = [hs.Anchor("sol", "T", T0, "P", censor_ms=T0 + 10_000_000)]
    got = hs.survival_sensitivity(anchors, {"T": s}, grid=(300,),
                                  cost=hs.COST_RENT_RECOVERED)
    assert set(got) == {"window_30s", "window_60s", "window_120s"}
    assert got["window_30s"][0]["n_with_price"] == 0
    assert got["window_120s"][0]["n_with_price"] == 1


# ------------------------------------------------------- sign versus duration


def test_feature_study_reports_sign_and_duration_as_separate_rows():
    """A variable that picks winners and a variable that predicts a long hold are not
    the same variable, and merging the two columns is how a hold rule gets invented."""
    tape: dict[str, hs.Series] = {}
    anchors = []
    for i in range(120):
        tok = f"T{i}"
        # entry-time activity: half the mints are busy, half are quiet. The sign at the
        # horizon is set by a SECOND, independent coin, so prints_per_min carries
        # duration information and no direction information at all - which is the
        # distinction the two columns exist to keep apart.
        busy = i % 2 == 0
        good_end = (i // 2) % 2 == 0
        t0 = T0 + i * 1_000
        pts = [(t0 - 20_000, 1.0)]
        pts += [(t0 - 10_000 + k * 500, 1.0) for k in range(8 if busy else 1)]
        pts.append((t0, 1.0))
        up_through = 60 if busy else 240
        for d in (30, 60, 120, 180, 240, 300):
            if d == 300:
                px = 1.6 if good_end else 0.9
            else:
                px = 1.6 if d <= up_through else 0.9
            pts.append((t0 + d * 1_000, px))
        tape[tok] = _series(pts, token=tok)
        anchors.append(hs.Anchor("sol", tok, t0, "P", censor_ms=t0 + 10_000_000))
    out = hs.entry_feature_study(anchors, tape, horizon_s=300, min_n=10)
    targets = {r.target for r in out}
    assert any("net_return_positive" in t for t in targets)
    assert any("profitable_hold_s" in t for t in targets)
    by = {(r.feature, r.statistic): r for r in out}
    assert ("prints_per_min", "spearman_rho") in by
    assert ("prints_per_min", "auc") in by
    dur = by[("prints_per_min", "spearman_rho")]
    assert dur.stat_fit is not None and dur.stat_test is not None
    assert dur.stat_fit < 0 and dur.stat_test < 0, (
        "the planted effect is busy-at-entry -> shorter profitable window"
    )
    assert "same direction in both halves" == dur.reading
    sign = by[("prints_per_min", "auc")]
    assert sign.stat_test == pytest.approx(0.5, abs=0.05), (
        "the same variable carries no information about the sign, and the sign column "
        "must be allowed to say so instead of borrowing the duration result"
    )


def test_feature_study_refuses_rather_than_splitting_a_tiny_sample():
    s = _series([(T0, 1.0), (T0 + 300_000, 2.0)], token="T")
    anchors = [hs.Anchor("sol", "T", T0, "P", censor_ms=T0 + 10_000_000)]
    out = hs.entry_feature_study(anchors, {"T": s}, horizon_s=300, min_n=30)
    assert len(out) == 1 and out[0].statistic == "none"
    assert "no split is worth reporting" in out[0].reading


def test_auc_and_rho_are_zero_information_on_noise():
    assert hs._auc([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == pytest.approx(0.5)
    assert hs._auc([3.0, 4.0], [1.0, 2.0]) == pytest.approx(1.0)
    assert hs._spearman([1.0, 2.0, 3.0, 4.0], [1.0, 2.0, 3.0, 4.0]) == pytest.approx(1.0)
    assert hs._spearman([1.0, 2.0, 3.0, 4.0], [4.0, 3.0, 2.0, 1.0]) == pytest.approx(-1.0)


# ------------------------------------------------------------- adding to a winner


def test_add_to_winner_compares_against_the_unconditional_mint_not_against_zero():
    """A positive forward return is not an edge. Beating the average mint is."""
    tape: dict[str, hs.Series] = {}
    anchors = []
    for i in range(40):
        tok = f"T{i}"
        up = i % 2 == 0
        p_check = 2.0 if up else 0.5
        # everything drifts the same way after the check point: no winner edge at all
        tape[tok] = _series(
            [(T0, 1.0), (T0 + 60_000, p_check), (T0 + 300_000, p_check * 1.1)], token=tok
        )
        anchors.append(hs.Anchor("sol", tok, T0, "P", censor_ms=T0 + 10_000_000))
    rows = hs.add_to_winner(anchors, tape, pairs=((60, 300),), thresholds=(0.2,))
    assert len(rows) == 1
    row = rows[0]
    assert row.n_winners == 20
    assert row.mean_fwd_winner_pct == pytest.approx(10.0)
    assert row.edge_pp == pytest.approx(0.0, abs=1e-6), (
        "identical drift must show zero edge even though the forward return is +10%"
    )
    assert row.net_fwd_winner_pct is not None and row.net_fwd_winner_pct < 10.0


def test_add_to_winner_charges_the_new_money_a_fresh_round_trip():
    tape = {"T": _series([(T0, 1.0), (T0 + 60_000, 2.0), (T0 + 300_000, 2.0)])}
    anchors = [hs.Anchor("sol", "T", T0, "P", censor_ms=T0 + 10_000_000)]
    row = hs.add_to_winner(anchors, tape, pairs=((60, 300),), thresholds=(0.2,))[0]
    assert row.mean_fwd_winner_pct == pytest.approx(0.0)
    assert row.net_fwd_winner_pct < 0.0, "a flat add still pays two legs"


# ----------------------------------------------------------------- held-out split


def test_split_is_by_time_and_disjoint():
    anchors = [hs.Anchor("sol", f"T{i}", T0 + i * 1_000, "P") for i in range(10)]
    early, late, cut = hs.split_by_time(anchors)
    assert cut is not None
    assert len(early) + len(late) == 10
    assert not ({a.token for a in early} & {a.token for a in late})
    assert max(a.t0_ms for a in early) < min(a.t0_ms for a in late)
    assert "STRUCTURAL" in hs.PROVENANCE["split_rule"]
    assert "regime" in hs.PROVENANCE["split_rule"], (
        "the split must warn that one 28-hour window is not two regimes"
    )


def test_split_refuses_on_a_sample_too_small_to_halve():
    anchors = [hs.Anchor("sol", "T", T0, "P")]
    early, late, cut = hs.split_by_time(anchors)
    assert late == [] and cut is None


# --------------------------------------------------------- the realised-book trap


def test_realised_table_ships_its_confound(tmp_db):
    rows = [("a", 30, -40.0, "stop_loss"), ("b", 40, -35.0, "stop_loss"),
            ("c", 400, 90.0, "trailing_stop"), ("d", 500, 120.0, "trailing_stop")]
    for i, (tid, hold, pnl, reason) in enumerate(rows):
        tmp_db.execute(
            "INSERT INTO trades (trade_id, position_id, lane, mode, chain, token, "
            "opened_ms, closed_ms, hold_s, cost_native, proceeds_native, pnl_native, "
            "pnl_pct, exit_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, f"pos{i}", "sm-trenches", "live", "sol", f"T{i}", T0 + i * 1_000,
             T0 + i * 1_000 + hold * 1_000, hold, "1", "1", "0", pnl, reason),
        )
    table = hs.realised_hold_vs_pnl(tmp_db)
    assert table.confounded is True
    assert table.n == 4
    # the tempting correlation is there, and it is the stop
    assert table.spearman_hold_vs_pnl == pytest.approx(1.0)
    for word in ("OUTPUT of the exit rule", "cannot be inverted", "fixed time exit"):
        assert word in table.confound
    assert len(table.live_rows) == 4


def test_realised_confound_text_is_not_droppable():
    """The warning is a field on the dataclass, so ``asdict`` carries it into the JSON."""
    from dataclasses import fields

    names = {f.name for f in fields(hs.RealisedHoldTable)}
    assert {"confounded", "confound"} <= names


# ------------------------------------------------------------------ significance


def test_time_stop_grid_uses_the_repository_gates_not_a_hand_rolled_test():
    tape: dict[str, hs.Series] = {}
    anchors = []
    for i in range(200):
        tok = f"T{i}"
        drift = 1.0 + (0.2 if i % 3 == 0 else -0.05)
        tape[tok] = _series([(T0 + i * 1_000, 1.0), (T0 + i * 1_000 + 60_000, drift)],
                            token=tok)
        anchors.append(hs.Anchor("sol", tok, T0 + i * 1_000, "P",
                                 censor_ms=T0 + i * 1_000 + 10_000_000))
    out = hs.time_stop_grid(anchors, tape, grid=(60, 120), trials=4)
    assert out["imputation"] == "observed_only"
    row = next(r for r in out["rows"] if r["delta_s"] == 60)
    assert row["n"] == 200
    assert row["deflated_sharpe"] is not None
    assert any("deflated" in note or "trials" in note for note in row["deflation_notes"])


def test_pbo_is_computed_over_horizons_with_their_own_timestamps():
    stamped = {
        60: [(T0 + i * 1_000, 0.01 if i % 2 else -0.01) for i in range(120)],
        300: [(T0 + i * 1_000, -0.02 if i % 2 else 0.03) for i in range(120)],
    }
    pbo = hs._pbo_over_horizons(stamped, (60, 300), blocks=12)
    assert pbo is None or 0.0 <= pbo <= 1.0


def test_pbo_refuses_with_a_single_configuration():
    stamped = {60: [(T0 + i * 1_000, 0.01) for i in range(120)]}
    assert hs._pbo_over_horizons(stamped, (60,), blocks=12) is None


def test_sample_needed_refuses_a_size_for_a_negative_edge_and_says_what_would_be_needed():
    losing = [-0.3, -0.5, 0.2, -0.1, -0.4, 0.6, -0.2]
    out = hs.sample_needed(losing, per_day=4.0)
    assert out["needed_for_observed_mean"] is None
    assert "no edge to size a sample for" in out["needed_note"]
    rows = out["to_prove_a_hypothetical_edge"]
    assert rows and all(r["trades_needed"] is not None for r in rows)
    # a smaller target edge needs a bigger sample
    by_target = {r["target_net_pct_per_trade"]: r["trades_needed"] for r in rows}
    assert by_target[1.0] > by_target[10.0]
    assert all(r["days_at_current_rate"] is not None for r in rows)


def test_trade_rate_turns_a_sample_size_into_a_date(tmp_db):
    for i in range(10):
        tmp_db.execute(
            "INSERT INTO trades (trade_id, position_id, lane, mode, chain, token, "
            "opened_ms, closed_ms, hold_s, cost_native, proceeds_native, pnl_native, "
            "pnl_pct) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"t{i}", f"p{i}", "sm-trenches", "live" if i < 4 else "shadow", "sol",
             f"T{i}", T0 + i * 3_600_000, T0 + i * 3_600_000 + 60_000, 60,
             "1", "1", "0", -10.0),
        )
    rate = hs.trade_rate(tmp_db)
    assert rate["closed_trades"] == 10 and rate["live_trades"] == 4
    assert rate["all_per_day"] > rate["live_per_day"] > 0


# ------------------------------------------------------------------- end to end


def test_run_is_honest_on_an_empty_database(tmp_db):
    rep = hs.run(tmp_db, chains=("sol",))
    assert rep.curves == [] or all(c["n_anchors"] == 0 for c in rep.curves)
    assert rep.verdicts, "an empty database must say why it produced nothing"
    assert any("no curve reported" in v for v in rep.verdicts)


def test_run_reports_coverage_before_any_curve(tmp_db):
    _tape(tmp_db, token="A", created_ms=T0, covered_to_ms=T0 + 45_000)
    _swap(tmp_db, token="A", ts_ms=T0, price=1.0)
    _swap(tmp_db, token="A", ts_ms=T0 + 20_000, price=2.0)
    rep = hs.run(tmp_db, chains=("sol",), grid=(20,))
    facts = rep.coverage_facts
    assert facts["complete_tape_rows"] == 1
    assert facts["complete_tape_window_s"]["max"] == pytest.approx(45.001, abs=0.01)
    assert "live_traded_tokens_with_complete_tape" in facts
    launch = [c for c in rep.curves if c["population"] == "POP_CURVE_LAUNCH"]
    assert launch, "the gap-free population must always be reported"
    assert all(c["bias"] for c in launch), "every curve ships its bias statement"


def test_bias_statement_names_both_directions_of_the_feed_selection():
    assert "UPPER BOUND" in hs.BIAS_SMART_MONEY
    assert "LOWER BOUND" in hs.BIAS_SMART_MONEY
    assert "NOT MEASURABLE" in hs.BIAS_LAUNCH


def test_cross_check_reports_the_quote_asset_contamination(tmp_db):
    tmp_db.execute(
        "INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, amount_token, "
        "amount_native, price_usd, usd_value, source) "
        "VALUES ('sol','x1',?,'w','A','buy','1000000','120000','0.118','100','pumpfun:trades')",
        (T0,),
    )
    tmp_db.execute(
        "INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, amount_token, "
        "amount_native, price_usd, usd_value, source) "
        "VALUES ('sol','x2',?,'w','A','buy','1000000','240000','0.236','100','pumpfun:trades')",
        (T0 + 30_000,),
    )
    out = hs.cross_check_sol_price(tmp_db, ["A"])
    assert out["n_pairs"] == 1
    # both definitions double: the contamination is zero on this pair
    assert out["mean_usd_minus_sol_return_pct"] == pytest.approx(0.0, abs=1e-6)


def test_module_states_that_the_curve_mark_is_exact_and_the_feed_mark_is_not():
    doc = hs.__doc__ or ""
    assert "POP_CURVE_LAUNCH" in doc and "POP_SMART_MONEY" in doc
    assert "upper bound" in doc.lower()
    src_note = hs.PROVENANCE["imputation_bracket"]
    assert "observed_only" in src_note and "dead_is_zero" in src_note
