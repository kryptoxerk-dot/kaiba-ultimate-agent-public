"""Tests for :mod:`kaiba.learning.outcomes`.

The one that matters is :func:`test_no_feature_reads_past_entry`. It does not check that
the SQL has a ``WHERE`` clause; it builds a row, then writes a contradictory future into
every table the feature builder touches, rebuilds, and asserts that not one reading moved.
Deleting the time bound makes it fail, which is the only kind of leakage test worth having.

The rest pin the conventions, because on this dataset the conventions are worth more than
the variables: which price an entry is booked at moved the mean net return of "buy
everything" by 11 to 16 percentage points.
"""

from __future__ import annotations

import json

import pytest

from kaiba.intelligence import confluence
from kaiba.learning import gates, outcomes, replay
from kaiba.learning.outcomes import EntryFill, SourceKind

CHAIN = "sol"
TOKEN = "TokenAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAApump"
OTHER = "TokenBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBpump"
CREATOR = "CreatorAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
T0 = 1_790_000_000_000
ENTRY = T0 + 300_000


# --------------------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------------------


def _swap(conn, *, ts_ms, wallet, side, price, native=1_000_000_000, tx=None, token=TOKEN):
    conn.execute(
        "INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, amount_token, amount_native, "
        "price_usd, source) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            CHAIN,
            tx or f"tx{ts_ms}{wallet}{side}",
            ts_ms,
            wallet,
            token,
            side,
            "1000",
            str(native),
            str(price),
            "test",
        ),
    )


def _event(conn, *, ts_ms, kind, subject, payload):
    conn.execute(
        "INSERT INTO events (ts_ms, kind, level, chain, subject, payload) VALUES (?,?,?,?,?,?)",
        (ts_ms, kind, "info", CHAIN, subject, json.dumps(payload)),
    )


def _dossier(conn, *, built_at_ms, top10, dev, sniper, bundler=1.0, liquidity=50_000.0, token=TOKEN):
    def measure(value):
        return {"value": str(value), "basis": "provider_reported"}

    payload = {
        "top10_pct": measure(top10),
        "dev_pct": measure(dev),
        "sniper_pct": measure(sniper),
        "bundler_pct": measure(bundler),
        "liquidity_usd": measure(liquidity),
        "freeze_authority_revoked": True,
        "graded_wallets": [],
    }
    conn.execute(
        "INSERT INTO token_dossiers (chain, address, built_at_ms, grade, dossier_json) "
        "VALUES (?,?,?,?,?) ON CONFLICT(chain, address) DO UPDATE SET "
        "built_at_ms=excluded.built_at_ms, dossier_json=excluded.dossier_json",
        (CHAIN, token, built_at_ms, "B", json.dumps(payload)),
    )


def _seed_before_entry(conn) -> None:
    """A token with a creator, an earlier sibling launch, a pre-entry tape and a dossier."""
    _event(
        conn,
        ts_ms=T0 - 600_000,
        kind="token.created",
        subject=OTHER,
        payload={"creator": CREATOR, "uri": "ipfs://other", "name": "Other", "symbol": "OTH",
                 "created_ms": T0 - 600_000},
    )
    _event(
        conn,
        ts_ms=T0 - 300_000,
        kind="token.migrated",
        subject=OTHER,
        payload={"mint": OTHER},
    )
    _event(
        conn,
        ts_ms=T0,
        kind="token.created",
        subject=TOKEN,
        payload={"creator": CREATOR, "uri": "ipfs://shared", "name": "Thing", "symbol": "THG",
                 "created_ms": T0},
    )
    for i in range(12):
        _swap(
            conn,
            ts_ms=T0 + 10_000 * i,
            wallet=f"wallet{i % 5}",
            side="buy" if i % 3 else "sell",
            price=0.001 * (1.0 + 0.01 * i),
            native=2_000_000_000 + i,
        )
    _swap(conn, ts_ms=ENTRY - 20_000, wallet="wallet9", side="buy", price=0.00112, native=3_000_000_000)
    _dossier(conn, built_at_ms=ENTRY - 60_000, top10=40.0, dev=3.0, sniper=5.0)


def _seed_after_entry(conn) -> None:
    """A tape that exists only after the entry, so forward returns have something to read."""
    for i in range(1, 25):
        _swap(
            conn,
            ts_ms=ENTRY + 30_000 * i,
            wallet=f"late{i}",
            side="buy",
            price=0.002,
            native=5_000_000_000,
        )


def _observe(conn):
    index = outcomes.EventIndex(conn)
    return outcomes.observe_before_entry(conn, CHAIN, TOKEN, ENTRY, index=index)


# --------------------------------------------------------------------------------------
# the leakage proof
# --------------------------------------------------------------------------------------


def test_no_feature_reads_past_entry(tmp_db):
    """Write a loud, contradictory future into every source and prove nothing moves.

    If a reader loses its time bound, one of these assertions fails and names the variable.
    """
    _seed_before_entry(tmp_db)
    _seed_after_entry(tmp_db)
    before_obs, before_raw, _ = _observe(tmp_db)
    baseline_values = {k: (v.value, v.support, v.basis) for k, v in before_obs.items()}

    # A future in which everything about this token is different.
    for i in range(200):
        _swap(
            tmp_db,
            ts_ms=ENTRY + 1_000 + i,
            wallet="washer",
            side="buy" if i % 2 else "sell",
            price=99.0,
            native=999_000_000_000,
            tx=f"wash{i}",
        )
    _dossier(tmp_db, built_at_ms=ENTRY + 1, top10=99.0, dev=99.0, sniper=99.0, bundler=99.0,
             liquidity=1.0, token=OTHER)
    _event(
        tmp_db,
        ts_ms=ENTRY + 1,
        kind="token.created",
        subject="TokenCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCpump",
        payload={"creator": CREATOR, "uri": "ipfs://shared", "name": "Thing", "symbol": "THG",
                 "created_ms": ENTRY + 1},
    )
    _event(tmp_db, ts_ms=ENTRY + 2, kind="token.migrated", subject=TOKEN, payload={"mint": TOKEN})
    tmp_db.execute(
        "INSERT INTO curve_snapshots (chain, token, observed_ms, real_sol_lamports, "
        "virtual_sol_lamports, real_token_atoms, virtual_token_atoms, sol_in_curve, trades_seen, "
        "coverage_from_ms, created_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (CHAIN, TOKEN, ENTRY + 5_000, 99_000_000_000, 30_000_000_000, "1", "1", "99.0", 9_999,
         T0, T0),
    )
    tmp_db.execute(
        "INSERT INTO token_bundles (chain, token, computed_ms, model, coverage, reason, "
        "bundled_pct, sniped_pct) VALUES (?,?,?,?,?,?,?,?)",
        (CHAIN, TOKEN, ENTRY + 5_000, "test", "measured", "test", "99.0", "99.0"),
    )

    after_obs, after_raw, _ = _observe(tmp_db)
    for name, expected in baseline_values.items():
        got = after_obs[name]
        assert (got.value, got.support, got.basis) == expected, (
            f"{name} moved when post-entry rows were written: a feature read past the entry"
        )
    for name, expected_raw in before_raw.items():
        assert after_raw[name] == expected_raw, f"candidate {name} read past the entry"


def test_removing_the_time_bound_is_a_loud_failure(tmp_db, monkeypatch):
    """The mutation check. Delete the prevention and the detection must still fire.

    ``PointInTime`` guards every row on the way out, and this module routes every feature
    read through it. If someone rewrote a reader to query ``swaps`` directly, this test
    would pass while :func:`test_no_feature_reads_past_entry` failed, so the two together
    are what pin the rule.
    """
    _seed_before_entry(tmp_db)
    _seed_after_entry(tmp_db)
    monkeypatch.setattr(replay.PointInTime, "_omit_time_bound", True)
    with pytest.raises(replay.LookaheadError):
        _observe(tmp_db)


def test_the_view_excludes_the_entry_millisecond_itself(tmp_db):
    """Our own buy lands in ``swaps`` at the entry instant and must not be a feature of it."""
    _seed_before_entry(tmp_db)
    _seed_after_entry(tmp_db)
    _, raw_before, _ = _observe(tmp_db)
    _swap(tmp_db, ts_ms=ENTRY, wallet="us", side="buy", price=0.005, tx="our-own-buy")
    _, raw_after, _ = _observe(tmp_db)
    assert raw_after["pre_entry_swaps"] == raw_before["pre_entry_swaps"]


def test_event_index_counts_only_launches_seen_before_the_instant(tmp_db):
    _seed_before_entry(tmp_db)
    index = outcomes.EventIndex(tmp_db)
    assert len(index.prior_launches(CHAIN, CREATOR, ENTRY, TOKEN)) == 1
    assert len(index.prior_launches(CHAIN, CREATOR, T0 - 700_000, TOKEN)) == 0
    assert index.graduated_before(CHAIN, [OTHER], ENTRY) == 1
    assert index.graduated_before(CHAIN, [OTHER], T0 - 400_000) == 0


def test_creator_history_is_derived_from_events_not_the_creators_table(tmp_db):
    """``creators`` is recomputed in place, so replay must not read it. Prove it is not read."""
    _seed_before_entry(tmp_db)
    _seed_after_entry(tmp_db)
    obs, _, _ = _observe(tmp_db)
    assert obs["creator_graduation_history"].known
    assert obs["creator_graduation_history"].value == pytest.approx(1.0)
    tmp_db.execute(
        "INSERT INTO creators (chain, address, launches, graduated, updated_ms) VALUES (?,?,?,?,?)",
        (CHAIN, CREATOR, 1_000, 0, ENTRY - 1),
    )
    obs_again, _, _ = _observe(tmp_db)
    assert obs_again["creator_graduation_history"].value == pytest.approx(1.0)


def test_variables_that_cannot_be_replayed_say_so_rather_than_guess(tmp_db):
    _seed_before_entry(tmp_db)
    _seed_after_entry(tmp_db)
    obs, _, _ = _observe(tmp_db)
    for name in ("bundle_adjusted_concentration", "independent_entity_count"):
        assert not obs[name].known, name
        assert obs[name].detail, f"{name} must say why it is unavailable"
    assert "holder list" in (obs["bundle_adjusted_concentration"].detail or "")
    assert "timestamp" in (obs["independent_entity_count"].detail or "")


def test_copycat_is_flagged_as_a_proxy_for_the_published_variable(tmp_db):
    _seed_before_entry(tmp_db)
    _seed_after_entry(tmp_db)
    obs, _, notes = _observe(tmp_db)
    assert obs["copycat_reuse"].proxy is True
    assert any("proxy" in note for note in notes)


# --------------------------------------------------------------------------------------
# labels, cost and censoring
# --------------------------------------------------------------------------------------


def test_net_of_cost_charges_both_legs():
    assert outcomes.net_pct(1.0, 1.0, 200) == pytest.approx(-1.99, abs=1e-6)
    # A +2% gross move is still a loss: the fee is charged on the larger exit notional too.
    assert outcomes.net_pct(1.0, 1.02, 200) == pytest.approx(-0.0298, abs=1e-3)
    # A +1.5% move is a loss once both legs are paid.
    assert outcomes.net_pct(1.0, 1.015, 200) < 0.0
    # Break-even needs a little over +2%.
    assert outcomes.net_pct(1.0, 1.0204, 200) > 0.0


def test_a_censored_horizon_is_none_and_not_a_zero():
    """Nothing printed after the entry at all: every horizon is unobserved, not a zero."""
    series = [(T0, 1.0), (T0 + 30_000, 2.0)]
    forwards = outcomes.forward_returns(series, T0 + 60_000, 2.0, (5, 20, 60), 200)
    for horizon in (5, 20, 60):
        assert forwards[horizon].censored is True
        assert forwards[horizon].net_carry_pct is None
        assert forwards[horizon].net_strict_pct is None
    # One print inside the 20-minute window is a carry observation, not a censored one.
    partial = outcomes.forward_returns([(T0, 1.0), (T0 + 60_000, 2.0)], T0, 1.0, (5, 20), 200)
    assert partial[20].censored is False
    assert partial[20].tape_ended is True
    assert partial[20].net_carry_pct is not None
    assert partial[20].net_strict_pct is None


def test_strict_and_carry_are_different_questions():
    series = [(T0, 1.0), (T0 + 60_000, 2.0), (T0 + 3_600_000, 0.5)]
    forwards = outcomes.forward_returns(series, T0, 1.0, (20,), 0)
    # Same entry, same tape, opposite answers: carry marks the last print inside the window,
    # strict waits for a print at or after the horizon and finds one an hour later.
    assert forwards[20].gross_carry_pct == pytest.approx(100.0)
    assert forwards[20].gross_strict_pct == pytest.approx(-50.0)
    assert forwards[20].tape_ended is False


def test_the_exit_may_not_be_the_fill(tmp_db):
    """Under NEXT_PRINT the fill print is consumed and cannot also be the exit."""
    series = [(T0 - 1, 1.0), (T0 + 1_000, 2.0), (T0 + 120_000, 4.0)]
    forwards = outcomes.forward_returns(series, T0, 2.0, (5,), 0, after_ms=T0 + 1_000)
    assert forwards[5].gross_carry_pct == pytest.approx(100.0)


# --------------------------------------------------------------------------------------
# the fill convention, which is worth more than any variable here
# --------------------------------------------------------------------------------------


def _seed_dataset(conn) -> None:
    _seed_before_entry(conn)
    _seed_after_entry(conn)
    conn.execute(
        "INSERT INTO decisions (decision_id, ts_ms, lane, mode, chain, token, action) "
        "VALUES (?,?,?,?,?,?,?)",
        ("dec1", ENTRY, "test-lane", "shadow", CHAIN, TOKEN, "enter"),
    )


def test_next_print_fill_books_the_trade_that_actually_happened(tmp_db):
    _seed_dataset(tmp_db)
    last_before = outcomes.build_dataset(
        tmp_db, sources=(SourceKind.DECISION,), entry_fill=EntryFill.LAST_BEFORE
    )
    next_print = outcomes.build_dataset(
        tmp_db, sources=(SourceKind.DECISION,), entry_fill=EntryFill.NEXT_PRINT
    )
    assert len(last_before) == len(next_print) == 1
    stale, real = last_before.rows[0], next_print.rows[0]
    assert stale.fill_price_usd == stale.entry_price_usd
    assert real.fill_price_usd > real.entry_price_usd
    assert real.next_print_move_pct == pytest.approx(stale.next_print_move_pct)
    # The head start LAST_BEFORE hands the backtest is exactly that jump, and it is real
    # money: the stale convention books a better return on the same tape.
    assert stale.net(20) > real.net(20)


def test_a_fill_that_never_arrives_is_dropped_not_invented(tmp_db):
    """A token whose next trade is ten minutes away is not a token we could have bought."""
    sparse = "TokenDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDpump"
    for i in range(6):
        _swap(tmp_db, ts_ms=T0 + i, wallet=f"w{i}", side="buy", price=0.001, token=sparse)
    _swap(tmp_db, ts_ms=T0 + 15 * 60_000, wallet="much-later", side="buy", price=0.004,
          token=sparse)
    data = outcomes.build_dataset(
        tmp_db, sources=(SourceKind.TAPE,), entry_fill=EntryFill.NEXT_PRINT
    )
    assert len(data) == 0
    assert data.census.dropped.get("next_print_too_late_to_be_a_fill", 0) >= 1
    # The same entry survives under the stale convention, which is the whole problem with it.
    stale = outcomes.build_dataset(
        tmp_db, sources=(SourceKind.TAPE,), entry_fill=EntryFill.LAST_BEFORE
    )
    assert len(stale) == 1


def test_entry_price_bias_reports_the_head_start_per_source(tmp_db):
    _seed_dataset(tmp_db)
    data = outcomes.build_dataset(tmp_db, sources=(SourceKind.DECISION,))
    bias = outcomes.entry_price_bias(data)
    assert "decision" in bias
    n, mean, _median = bias["decision"]
    assert n == 1
    assert mean > 0.0


# --------------------------------------------------------------------------------------
# splitting and de-duplication
# --------------------------------------------------------------------------------------


def _row(
    token: str,
    entry_ms: int,
    source: SourceKind = SourceKind.TAPE,
    ret: float | None = 1.0,
    x: float | None = None,
):
    return outcomes.OutcomeRow(
        chain=CHAIN,
        token=token,
        entry_ms=entry_ms,
        source=source,
        ref=f"{source.value}:{token}:{entry_ms}",
        lane=None,
        entry_price_usd=1.0,
        entry_price_age_ms=0,
        fill_price_usd=1.0,
        fill_ms=entry_ms,
        fill=EntryFill.NEXT_PRINT,
        next_print_move_pct=0.0,
        observations={},
        mapped={},
        raw={"x": ret if x is None else x},
        forwards={
            20: outcomes.Forward(
                horizon_min=20,
                gross_strict_pct=ret,
                net_strict_pct=ret,
                gross_carry_pct=ret,
                net_carry_pct=ret,
                prints_in_window=1,
                tape_ended=False,
                censored=ret is None,
                last_print_ms=entry_ms + 1,
            )
        },
        realised_pnl_pct=None,
        realised_exit_reason=None,
        action=None,
        dossier_grade=None,
    )


def _dataset(rows):
    return outcomes.Dataset(
        rows=tuple(rows),
        census=outcomes.Census(considered=len(rows), kept=len(rows)),
        cost_bps=200,
        horizons_min=(20,),
        built_from="test",
    )


def test_time_split_never_puts_a_token_on_both_sides():
    rows = [_row("A", 1), _row("B", 2), _row("C", 3), _row("A", 4), _row("D", 5), _row("E", 6)]
    fit, oos = _dataset(rows).time_split()
    assert {r.token for r in fit.rows} & {r.token for r in oos.rows} == set()
    assert len(fit) + len(oos) == len(rows)
    leaky_fit, leaky_oos = _dataset(rows).time_split(by_token=False)
    assert {r.token for r in leaky_fit.rows} & {r.token for r in leaky_oos.rows} == {"A"}


def test_one_per_token_keeps_the_most_trustworthy_source():
    rows = [
        _row("A", 5, SourceKind.TAPE),
        _row("A", 6, SourceKind.LIVE),
        _row("A", 7, SourceKind.DECISION),
        _row("B", 8, SourceKind.SHADOW),
    ]
    kept = _dataset(rows).one_per_token()
    assert len(kept) == 2
    by_token = {r.token: r.source for r in kept.rows}
    assert by_token["A"] is SourceKind.LIVE
    assert by_token["B"] is SourceKind.SHADOW


# --------------------------------------------------------------------------------------
# separation: what is fitted, and where
# --------------------------------------------------------------------------------------


def test_a_fitted_threshold_comes_from_the_earlier_half_only():
    fit = _dataset([_row(f"F{i}", i, SourceKind.TAPE, ret=float(i) - 10.5) for i in range(1, 21)])
    oos_good = _dataset(
        [_row(f"O{i}", 100 + i, SourceKind.TAPE, ret=float(i) - 10.5) for i in range(1, 21)]
    )
    # Same variable values, opposite outcomes. If the rule were re-fitted on the held-out
    # half it would simply flip with them and report the same separation twice.
    oos_flipped = _dataset(
        [
            _row(f"O{i}", 100 + i, SourceKind.TAPE, ret=10.5 - float(i), x=float(i) - 10.5)
            for i in range(1, 21)
        ]
    )
    first = outcomes.measure_variable(fit, oos_good, "x", horizon_min=20)
    second = outcomes.measure_variable(fit, oos_flipped, "x", horizon_min=20)
    assert first is not None and second is not None
    assert first.rule == second.rule, "the rule must not be re-fitted on the held-out half"
    assert first.split == "fitted_median"
    assert (first.measured_half_nats or 0) > 0 > (second.measured_half_nats or 0)


def test_a_published_mapping_is_never_fitted():
    rows = [_row(f"T{i}", i, SourceKind.TAPE, ret=1.0) for i in range(1, 9)]
    data = _dataset(rows)
    sep = outcomes.measure_variable(data, data, "copycat_reuse", horizon_min=20)
    assert sep is None or sep.split == "published_mapping"


def test_claimed_half_separation_is_the_confluence_weight():
    fit = _dataset([_row(f"F{i}", i, SourceKind.TAPE, ret=float(i)) for i in range(1, 21)])
    sep = outcomes.measure_variable(fit, fit, "wash_trading", horizon_min=20)
    spec = confluence.LITERATURE_V1.by_name()["wash_trading"]
    if sep is not None:
        assert sep.claimed_half_nats == pytest.approx(spec.weight_nats)


def test_an_all_or_nothing_arm_is_finite_not_infinite():
    pair = outcomes.ArmPair(
        good=outcomes.Arm(n=9, wins=9, mean_net_pct=1.0, median_net_pct=1.0),
        bad=outcomes.Arm(n=9, wins=0, mean_net_pct=-1.0, median_net_pct=-1.0),
    )
    value = pair.half_separation_nats
    assert value is not None
    assert value == pytest.approx(2.944, abs=0.01)


def test_bootstrap_intervals_are_reproducible():
    rows = [_row(f"R{i}", i, SourceKind.TAPE, ret=float(i % 5) - 2.0) for i in range(1, 61)]
    fit, oos = _dataset(rows).time_split()
    first = outcomes.measure_variable(fit, oos, "x", horizon_min=20)
    second = outcomes.measure_variable(fit, oos, "x", horizon_min=20)
    assert first is not None and second is not None
    assert first.oos_half_nats_ci == second.oos_half_nats_ci


def test_stability_calls_a_sign_flip_a_label_artefact():
    def sep(name, value, arm_n=100):
        strong, weak = (arm_n // 2, arm_n // 4) if value > 0 else (arm_n // 4, arm_n // 2)
        pair = outcomes.ArmPair(
            good=outcomes.Arm(n=arm_n, wins=strong, mean_net_pct=1.0, median_net_pct=1.0),
            bad=outcomes.Arm(n=arm_n, wins=weak, mean_net_pct=0.0, median_net_pct=0.0),
        )
        return outcomes.Separation(
            variable=name,
            horizon_min=20,
            grade="candidate",
            split="fitted_median",
            claimed_half_nats=None,
            rule="test",
            in_sample=pair,
            out_of_sample=pair,
            oos_half_nats_ci=(value - 0.05, value + 0.05),
            oos_mean_gap_ci=(0.1, 0.5),
            coverage_oos=1.0,
        )

    views = {
        "a": [sep("steady", 0.4), sep("flipper", 0.4), sep("thin", 0.4, arm_n=5)],
        "b": [sep("steady", 0.3), sep("flipper", -0.3), sep("thin", 0.4, arm_n=5)],
    }
    verdicts = {item.variable: item.verdict for item in outcomes.stability(views)}
    assert verdicts["steady"] == "SIZE"
    assert verdicts["flipper"] == "label-artefact"
    assert verdicts["thin"] == "n-too-small"


def test_stability_refuses_a_variable_that_is_missing_from_a_view():
    def sep(name):
        pair = outcomes.ArmPair(
            good=outcomes.Arm(n=100, wins=50, mean_net_pct=1.0, median_net_pct=1.0),
            bad=outcomes.Arm(n=100, wins=20, mean_net_pct=0.0, median_net_pct=0.0),
        )
        return outcomes.Separation(
            variable=name,
            horizon_min=20,
            grade="candidate",
            split="fitted_median",
            claimed_half_nats=None,
            rule="test",
            in_sample=pair,
            out_of_sample=pair,
            oos_half_nats_ci=(0.2, 0.6),
            oos_mean_gap_ci=(0.1, 0.5),
            coverage_oos=1.0,
        )

    views = {"a": [sep("here"), sep("sometimes")], "b": [sep("here")]}
    verdicts = {item.variable: item.verdict for item in outcomes.stability(views)}
    assert verdicts["sometimes"] == "incomplete"


# --------------------------------------------------------------------------------------
# the combination must go through the gates, not around them
# --------------------------------------------------------------------------------------


def test_combined_gate_uses_the_repository_gates(monkeypatch):
    calls: list[str] = []
    real_dsr, real_pbo = gates.deflated_sharpe, gates.pbo_cscv

    def spy_dsr(*args, **kwargs):
        calls.append("deflated_sharpe")
        return real_dsr(*args, **kwargs)

    def spy_pbo(*args, **kwargs):
        calls.append("pbo_cscv")
        return real_pbo(*args, **kwargs)

    monkeypatch.setattr(gates, "deflated_sharpe", spy_dsr)
    monkeypatch.setattr(gates, "pbo_cscv", spy_pbo)
    rows = [_row(f"S{i}", i, SourceKind.TAPE, ret=float(i % 7) - 3.0) for i in range(1, 121)]
    fit, oos = _dataset(rows).time_split()
    result = outcomes.combined_gate(fit, oos, horizon_min=20)
    assert "deflated_sharpe" in calls
    assert result.trials >= len(confluence.LITERATURE_V1.variables)


def test_deflation_counts_every_variable_we_looked_at():
    rows = [_row(f"S{i}", i, SourceKind.TAPE, ret=1.0) for i in range(1, 41)]
    fit, oos = _dataset(rows).time_split()
    result = outcomes.combined_gate(fit, oos, horizon_min=20)
    expected = len(confluence.LITERATURE_V1.variables) + len(outcomes.CANDIDATE_VARIABLES)
    assert result.trials == expected


# --------------------------------------------------------------------------------------
# honesty contract
# --------------------------------------------------------------------------------------


def test_every_candidate_variable_says_what_it_is_worth():
    for name in outcomes.CANDIDATE_VARIABLES:
        text = outcomes.CANDIDATE_PROVENANCE[name]
        assert "INVENTED" in text or "MEASURED" in text, name


def test_every_tunable_constant_names_its_provenance():
    vocabulary = ("MEASURED", "INVENTED", "DEFINITIONAL", "OPERATIONAL", "DERIVED")
    for name, text in outcomes.PROVENANCE.items():
        assert any(word in text for word in vocabulary), name
        if "INVENTED" in text:
            assert "Settled by" in text or "settle" in text, f"{name} is INVENTED and names no way out"


def test_the_module_states_its_own_biases():
    text = outcomes.__doc__ or ""
    for word in ("Selection", "Survivorship", "Net of cost", "17.3 hours"):
        assert word in text, word


def test_source_kinds_all_carry_a_stated_bias():
    for kind in SourceKind:
        assert kind in outcomes.SOURCE_BIAS
        assert outcomes.SOURCE_BIAS[kind]
    assert outcomes.TRUST_ORDER[0] is SourceKind.LIVE


def test_census_is_reported_with_the_rows(tmp_db):
    _seed_dataset(tmp_db)
    data = outcomes.build_dataset(tmp_db, sources=(SourceKind.DECISION, SourceKind.TAPE))
    line = outcomes.describe(data)
    assert "census" in line
    assert "considered" in data.census.line()
    assert data.census.considered >= len(data)


def test_describe_names_the_censoring_rate(tmp_db):
    _seed_dataset(tmp_db)
    data = outcomes.build_dataset(tmp_db, sources=(SourceKind.DECISION,))
    assert "censored" in outcomes.describe(data)


def test_report_only_variables_are_measured_but_never_scored():
    """Weight-zero variables still get measured here; they just may not move a score."""
    for name in ("raw_top10_concentration", "sniper_exposure", "wallet_pnl_grade"):
        spec = confluence.LITERATURE_V1.by_name()[name]
        assert spec.weight_nats == 0.0
        assert spec.mapping == "report_only"
    fit = _dataset([_row(f"F{i}", i, SourceKind.TAPE, ret=float(i)) for i in range(1, 21)])
    sep = outcomes.measure_variable(fit, fit, "raw_top10_concentration", horizon_min=20)
    assert sep is None or sep.split == "fitted_median"


def test_wallet_pnl_grade_is_carried_but_never_weighted():
    """docs/TRADING-METHOD.md rules wallet PnL rank out. Measuring it must not re-admit it."""
    spec = confluence.LITERATURE_V1.by_name()["wallet_pnl_grade"]
    assert spec.weight_nats == 0.0
    assert "wallet_pnl_grade" not in outcomes.CANDIDATE_VARIABLES


def test_artefact_share_measures_the_entry_price_head_start():
    rows = []
    for i in range(1, 41):
        row = _row(f"A{i}", i, SourceKind.TAPE, ret=float(i % 4) - 1.5)
        rows.append(row)
    fit, oos = _dataset(rows).time_split()
    got = outcomes.artefact_share(fit, oos, "x", horizon_min=20)
    assert got is None or got.variable == "x"
