"""Tests for the replay engine, written so that removing a guard fails a NAMED test.

The module under test exists to produce numbers nobody can check against a market, so
every claim it makes has to be checkable here instead. Three groups matter most:

* **Isolation.** ``test_removing_the_time_bound_is_caught_by_the_row_guard`` deletes the
  prevention and asserts the detection fires;
  ``test_a_swap_injected_after_the_exit_cannot_change_the_result`` runs a whole episode
  twice with a fabricated future row in between and asserts the two results are byte for
  byte identical. A comment saying "point in time" is worth nothing next to either.
* **One fill model.** ``test_the_recovered_costs_reproduce_the_brokers_own_pnl`` asserts
  the cost terms this module recovers reconstruct ``PaperBroker``'s booked PnL to five
  decimal places. If they ever stop doing that, the short mirror is being charged a cost
  the long arm did not pay.
* **Trial counting.** ``test_two_configurations_differing_by_one_key_register_as_two``
  and ``test_a_registry_write_that_cannot_be_verified_stops_the_run`` are what stand
  between this harness and five hundred quiet variants with one reported winner.
"""

from __future__ import annotations

import ast
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from kaiba.core import db
from kaiba.core.db import jdump
from kaiba.core.schemas import Chain, EventKind, EvidenceBasis, Lane
from kaiba.execution import paper
from kaiba.learning import registry, replay
from kaiba.learning.replay import (
    NO_OBSERVATION_TIME,
    PROVENANCE,
    TIME_COLUMNS,
    Arm,
    CostTerms,
    Episode,
    LookaheadError,
    PointInTime,
    ReplayBroker,
    ReplayConfig,
    UnregisteredTrialError,
    Verdict,
)

TOKEN = "ReplayTokenPumpFunMintAddress000000000001"
T0 = 1_789_000_000_000


# --------------------------------------------------------------------------- fixtures


def _seed_migration(conn: sqlite3.Connection, *, token: str = TOKEN, t0: int = T0) -> None:
    conn.execute(
        "INSERT INTO events (ts_ms, kind, level, chain, subject, payload) VALUES (?,?,?,?,?,?)",
        (t0, EventKind.TOKEN_MIGRATED.value, "info", Chain.SOL.value, token,
         jdump({"mint": token, "migrated_ms": t0})),
    )


def _seed_sol_usd(conn: sqlite3.Connection, *, t0: int = T0, span_s: int = 1200) -> None:
    for off in range(-120, span_s, 60):
        conn.execute(
            "INSERT OR REPLACE INTO native_prices (chain, ts_ms, price_usd, source) "
            "VALUES (?,?,?,?)",
            (Chain.SOL.value, t0 + off * 1000, "100", "test"),
        )


def _cpmm_tape(
    conn: sqlite3.Connection,
    *,
    token: str = TOKEN,
    t0: int = T0,
    pool_sol: Decimal = Decimal(100),
    n: int = 90,
    step_s: int = 4,
    drift_lamports: int = 20_000_000,
    start_off_s: int = 4,
) -> list[tuple[int, Decimal]]:
    """A tape that is exactly constant-product, so the pool solve recovers it.

    ``p = (X / 1e9) ** 2 * 1e-9`` with ``X`` the SOL reserve in lamports. Every row is a
    buy of ``drift_lamports``, so the reserve and therefore the price walk upward in a way
    the solve reproduces to the last digit - which is what makes a depth failure in these
    tests a real failure rather than fixture noise.
    """
    x = pool_sol * Decimal(10**9)
    written: list[tuple[int, Decimal]] = []
    for i in range(n):
        ts = t0 + (start_off_s + i * step_s) * 1000
        price = (x / Decimal(10**9)) ** 2 * Decimal("1e-9")
        conn.execute(
            "INSERT INTO swaps (chain, tx, slot, block_index, ts_ms, wallet, token, side, "
            "amount_token, amount_native, price_usd, usd_value, program, source) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (Chain.SOL.value, f"tx{token[:6]}{i}", 1000 + i, 0, ts, f"w{i}", token, "buy",
             "1000000", str(drift_lamports), str(price), "1", "pump_amm", "pumpfun:trades"),
        )
        written.append((ts, price))
        x += Decimal(drift_lamports)
    return written


@pytest.fixture
def seeded(tmp_db):
    _seed_migration(tmp_db)
    _seed_sol_usd(tmp_db)
    _cpmm_tape(tmp_db)
    return tmp_db


def _episode(horizon_s: int = 300) -> Episode:
    return Episode(
        chain=Chain.SOL, token=TOKEN, t0_ms=T0, t0_basis=replay.T0_BASES[0],
        horizon_s=horizon_s, swaps_in_window=90, priced_in_window=90,
        eligible=True, reason="eligible",
    )


# ------------------------------------------------------------------------- provenance


def _module_constants() -> set[str]:
    tree = ast.parse(Path(replay.__file__).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        targets: list[ast.Name] = []
        if isinstance(node, ast.Assign):
            targets = [t for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target]
        for target in targets:
            if target.id.isupper():
                names.add(target.id)
    return names


def test_every_constant_has_provenance():
    """Fails on a silently-added knob.

    This is the test the task asks for by name. A threshold that appears in the replay
    engine without a row saying where it came from is indistinguishable from one chosen
    because it made the answer look better, and the whole value of a backtest is that
    every number in it can be traced.
    """
    declared = set(PROVENANCE) | {"PROVENANCE", "UNKNOWN_DEPTH"}
    assert _module_constants() == declared


def test_provenance_rows_are_filled_in():
    for name, row in PROVENANCE.items():
        assert row.value == getattr(replay, name), f"{name}: provenance value is stale"
        assert row.unit and row.source and row.note


def test_every_provenance_row_classifies_itself():
    words = ("MEASURED", "DERIVED", "INVENTED", "DEFINITIONAL")
    for name, row in PROVENANCE.items():
        assert any(w in row.note for w in words), f"{name} does not classify its provenance"


def test_the_invented_constants_say_so_and_report_their_sensitivity():
    """The three numbers chosen by eye must admit it, and must be swept, not asserted."""
    for name in ("DEPTH_INLIER_TOL", "DEPTH_MIN_INLIER_FRAC", "MIN_EPISODE_SWAPS"):
        assert "INVENTED" in PROVENANCE[name].note, name
    assert "sensitivity" in PROVENANCE["MIN_EPISODE_SWAPS"].note.lower()


def test_every_replay_config_default_is_a_provenanced_constant_or_an_argument():
    """A default buried in the dataclass is still a knob, and still needs a source."""
    from dataclasses import fields

    free = {
        f.name for f in fields(ReplayConfig)
        if f.name not in {"lane", "arm", "chain", "lane_overrides", "slippage_bps",
                          "entry_delay_s", "horizon_s", "size_base_units"}
    }
    backed = {
        "entry_window_s", "poll_s", "price_max_age_s", "exit_price_max_age_s",
        "depth_window_s", "depth_min_inlier_frac", "depth_program",
    }
    assert free == backed, "a ReplayConfig default has no provenanced constant behind it"


# -------------------------------------------------------------------- point-in-time


def test_a_future_row_is_not_returned(seeded):
    cursor = T0 + 60_000
    seeded.execute(
        "INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, amount_native, "
        "price_usd, program, source) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (Chain.SOL.value, "tx_future", cursor + 1, "w", TOKEN, "buy", "1", "999999",
         "pump_amm", "pumpfun:trades"),
    )
    view = PointInTime(seeded, cursor)
    rows = view.rows("swaps", where="token=?", params=(TOKEN,))
    assert rows, "fixture produced no visible rows"
    assert all(int(r["ts_ms"]) <= cursor for r in rows)
    assert not any(r["tx"] == "tx_future" for r in rows)


def test_removing_the_time_bound_is_caught_by_the_row_guard(seeded):
    """MUTATION CHECK. Delete the prevention; the detection must fail loudly.

    ``_omit_time_bound`` literally removes the ``WHERE ts_ms <= ?`` clause that
    :meth:`PointInTime.rows` appends. If the unconditional per-row check in ``_guard``
    were dropped - or weakened to a log line - this test would stop failing and the
    engine would silently read the future. That is the whole reason the guard does not
    trust the SQL.
    """
    cursor = T0 + 60_000
    seeded.execute(
        "INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, amount_native, "
        "price_usd, program, source) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (Chain.SOL.value, "tx_future", cursor + 7_000, "w", TOKEN, "buy", "1", "999999",
         "pump_amm", "pumpfun:trades"),
    )
    view = PointInTime(seeded, cursor)
    view._omit_time_bound = True
    with pytest.raises(LookaheadError) as err:
        view.rows("swaps", where="token=?", params=(TOKEN,))
    message = str(err.value)
    assert "past the replay cursor" in message
    assert "The time bound on this read is missing or wrong" in message


def test_a_table_with_no_observation_time_is_refused_by_name(seeded):
    """`tokens` is the one that matters: migrated_ms is our wall clock, written in place."""
    view = PointInTime(seeded, T0)
    with pytest.raises(LookaheadError) as err:
        view.rows("tokens", where="address=?", params=(TOKEN,))
    assert "mutated in place" in str(err.value) or "updated in place" in str(err.value)
    with pytest.raises(LookaheadError):
        view.rows("entity_members")


def test_an_unregistered_table_is_refused_with_instructions(seeded):
    view = PointInTime(seeded, T0)
    with pytest.raises(LookaheadError) as err:
        view.rows("gate_results")
    assert "TIME_COLUMNS" in str(err.value) and "NO_OBSERVATION_TIME" in str(err.value)


def test_every_registered_time_column_is_not_null_in_the_schema(tmp_db):
    """A nullable observation time is a row that cannot be placed in time.

    Registering one would let the guard below be the only thing standing between the
    replay and a row of unknown age, and a guard is a worse defence than a constraint.
    All fourteen registered columns are NOT NULL today; this fails if that changes or if
    someone registers a table where it is not true.
    """
    for table, column in TIME_COLUMNS.items():
        info = {r["name"]: r for r in tmp_db.execute(f"PRAGMA table_info({table})")}
        assert column in info, f"{table} has no column {column}"
        assert info[column]["notnull"] == 1, f"{table}.{column} is nullable"


def test_a_null_observation_time_is_refused_by_the_guard(seeded):
    """Belt and braces for a future table: an undateable row is refused, not assumed."""
    view = PointInTime(seeded, T0 + 60_000)
    with pytest.raises(LookaheadError) as err:
        view._guard("token_dossiers", "built_at_ms", [{"built_at_ms": None}])
    assert "NULL built_at_ms" in str(err.value)


def test_every_registered_table_and_refusal_is_disjoint_and_real():
    assert not (set(TIME_COLUMNS) & set(NO_OBSERVATION_TIME))
    for table, reason in NO_OBSERVATION_TIME.items():
        assert len(reason) > 20, f"{table} refuses without explaining itself"


def test_the_lane_gets_no_database_handle(seeded):
    """The lane cannot run its own unbounded queries, because it has nothing to run them on.

    ``lanes._event_payload`` reads the newest matching event with no time filter at all.
    Handing the lane a live connection would reintroduce lookahead one helper at a time,
    so it is handed None and everything it may see is assembled from the view.
    """
    view = PointInTime(seeded, T0 + 60_000)
    ctx = replay.lane_context(view, _episode(), ReplayConfig())
    assert ctx.conn is None
    assert ctx.dossier is None
    assert ctx.recent_buys == []
    assert ctx.extras["migration_ms"] == T0


def test_a_dossier_built_after_the_cursor_is_invisible(seeded):
    cursor = T0 + 60_000
    seeded.execute(
        "INSERT INTO token_dossiers (chain, address, built_at_ms, score, grade, dossier_json) "
        "VALUES (?,?,?,?,?,?)",
        (Chain.SOL.value, TOKEN, cursor + 1, 50, "b", "{}"),
    )
    ctx = replay.lane_context(PointInTime(seeded, cursor), _episode(), ReplayConfig())
    assert ctx.extras["dossier_available_at_cursor"] is False

    seeded.execute(
        "UPDATE token_dossiers SET built_at_ms=? WHERE address=?", (cursor - 1, TOKEN)
    )
    ctx = replay.lane_context(PointInTime(seeded, cursor), _episode(), ReplayConfig())
    assert ctx.extras["dossier_available_at_cursor"] is True


def test_a_swap_injected_after_the_exit_cannot_change_the_result(seeded):
    """End to end: fabricate a spectacular future print and show the replay never sees it.

    This is the test worth more than the module docstring. The injected row is a 100x
    price 10 seconds after the exit deadline; if any read in the whole pipeline - the
    lane's own evaluation, the entry price, the pool solve, the exit mark - were
    unbounded, the replayed return would move.
    """
    cfg = ReplayConfig(horizon_s=120, entry_delay_s=30)
    broker = ReplayBroker()
    try:
        before = replay.replay_episode(seeded, _episode(120), cfg, broker)
        assert before.filled, f"fixture did not fill: {before.refused_reason}"
        exit_ms = before.exit_ms
        assert exit_ms is not None
        seeded.execute(
            "INSERT INTO swaps (chain, tx, slot, block_index, ts_ms, wallet, token, side, "
            "amount_native, price_usd, program, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (Chain.SOL.value, "tx_moon", 99_999, 0, exit_ms + 10_000, "w", TOKEN, "buy",
             "1000000000", "9999", "pump_amm", "pumpfun:trades"),
        )
        broker.reset()
        after = replay.replay_episode(seeded, _episode(120), cfg, broker)
    finally:
        broker.close()

    assert after.filled
    assert after.entry_ms == before.entry_ms
    assert after.exit_ms == before.exit_ms
    assert after.entry_price_usd == before.entry_price_usd
    assert after.exit_price_usd == before.exit_price_usd
    assert after.net_return == before.net_return
    assert after.depth.lamports == before.depth.lamports


# ----------------------------------------------------------------------- pool depth


def test_the_pool_solve_recovers_the_reserve_it_was_built_from(seeded):
    """The fixture tape is exactly constant-product at 100 SOL; the solve must find it."""
    view = PointInTime(seeded, T0 + 120_000)
    depth = replay.implied_pool(view, Chain.SOL, TOKEN, not_before_ms=T0, program="pump_amm")
    assert depth.known
    assert depth.basis is EvidenceBasis.ESTIMATED, "a tape solve is never better than ESTIMATED"
    sol = Decimal(depth.lamports) / Decimal(10**9)
    assert Decimal("99") < sol < Decimal("104"), sol


def test_a_holed_tape_fails_the_contiguity_gate_and_returns_unavailable(tmp_db):
    """Deleting rows collapses the implied pool, and the gate must say so, not guess."""
    _seed_migration(tmp_db)
    _seed_sol_usd(tmp_db)
    _cpmm_tape(tmp_db, n=60)
    tmp_db.execute("DELETE FROM swaps WHERE token=? AND (rowid % 3) = 0", (TOKEN,))
    view = PointInTime(tmp_db, T0 + 120_000)
    depth = replay.implied_pool(view, Chain.SOL, TOKEN, not_before_ms=T0, program="pump_amm")
    assert not depth.known
    assert depth.lamports is None, "missing depth must be None, never 0"
    assert depth.basis is EvidenceBasis.UNAVAILABLE
    assert "tape_not_contiguous" in depth.source or "too_few_solves" in depth.source


def test_missing_depth_is_unavailable_and_never_zero(seeded):
    view = PointInTime(seeded, T0 - 1_000_000)
    depth = replay.implied_pool(view, Chain.SOL, TOKEN)
    assert depth.lamports is None
    assert depth.basis is EvidenceBasis.UNAVAILABLE
    assert depth.usd(Decimal(100)) is None


def test_the_depth_solve_will_not_mix_curve_swaps_into_a_pool(tmp_db):
    """A trailing window reaching back past migration must not price a pool that did not exist."""
    _seed_migration(tmp_db)
    _seed_sol_usd(tmp_db)
    for i in range(30):
        tmp_db.execute(
            "INSERT INTO swaps (chain, tx, slot, block_index, ts_ms, wallet, token, side, "
            "amount_native, price_usd, program, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (Chain.SOL.value, f"curve{i}", i, 0, T0 - (60 - i * 2) * 1000, "w", TOKEN,
             "buy", "1000000", str(Decimal("1e-6") * (1 + Decimal(i) / 100)), "pump",
             "pumpfun:trades"),
        )
    view = PointInTime(tmp_db, T0)
    mixed = replay.implied_pool(view, Chain.SOL, TOKEN, program=None, not_before_ms=None)
    scoped = replay.implied_pool(view, Chain.SOL, TOKEN, program="pump_amm", not_before_ms=T0)
    assert mixed.solves > 0, "the fixture must actually reach the curve rows"
    assert scoped.solves == 0
    assert scoped.lamports is None


# ----------------------------------------------------------------- one fill model


def test_the_replay_broker_is_the_paper_broker(seeded):
    broker = ReplayBroker()
    try:
        assert isinstance(broker.broker, paper.PaperBroker)
    finally:
        broker.close()


def test_the_recovered_costs_reproduce_the_brokers_own_pnl(seeded):
    """The identity the short mirror rests on, checked against the broker that booked it.

    ``net = (1 - c_in)(1 + gross)(fx)(1 - c_out) - 1 - c_tip``. If this drifts, the
    mirror is charging a cost the long arm did not pay and the two arms stop being
    comparable - which is the exact failure a second fill model would cause.
    """
    cfg = ReplayConfig(horizon_s=120, entry_delay_s=30)
    broker = ReplayBroker()
    try:
        out = replay.replay_episode(seeded, _episode(120), cfg, broker)
    finally:
        broker.close()
    assert out.filled and out.costs is not None and out.fx is not None
    c = out.costs
    modelled = (1 - c.c_in) * (1 + out.gross_return) * out.fx * (1 - c.c_out) - 1 - c.c_tip
    assert abs(modelled - out.net_return) < Decimal("0.00001")


def test_a_thin_pool_is_refused_rather_than_filled(seeded):
    """A broker that always fills is a lie, and the replay must inherit the refusal."""
    broker = ReplayBroker()
    try:
        got = broker.round_trip(
            chain=Chain.SOL, token=TOKEN, lane=Lane.MIGRATION_FADE, t0_ms=T0,
            entry_ms=T0 + 60_000, exit_ms=T0 + 180_000, size_base_units=60_000_000,
            entry_price_usd=Decimal("0.00001"), exit_price_usd=Decimal("0.00001"),
            entry_liquidity_usd=Decimal("20"), exit_liquidity_usd=Decimal("20"),
            entry_sol_usd=Decimal(100), exit_sol_usd=Decimal(100),
            exit_reason="test", decision_seed="thin",
        )
    finally:
        broker.close()
    assert got["filled"] is False
    assert "slippage_exceeded" in got["refused_reason"]


def test_the_cost_cross_check_never_sums_the_two_accountings(seeded):
    """viability's fitted model and the broker's fee schedule measure the same cost once."""
    cfg = ReplayConfig(horizon_s=120, entry_delay_s=30)
    eps = [_episode(120)]
    census = replay.EpisodeCensus({120: 1}, {120: 1}, {120: {"eligible": 1}})
    res = replay.run_config(seeded, cfg, eps, census, registry_conn=seeded)
    note = next((n for n in res.notes if "round-trip cost" in n), None)
    assert note is not None
    assert "never summed" in note


# --------------------------------------------------------------------- the two arms


def test_the_short_arm_is_marked_not_executable():
    assert Arm.LONG.executable is True
    assert Arm.SHORT_MIRROR.executable is False
    assert "borrow" in Arm.executable.__doc__
    assert "no perp" in Arm.executable.__doc__
    assert "UPPER BOUND" in Arm.executable.__doc__


def test_a_flat_price_path_loses_the_round_trip_on_both_arms():
    """Neither side is free. With no move, both arms pay the whole round trip."""
    costs = CostTerms(c_in=Decimal("0.03"), c_out=Decimal("0.015"), c_tip=Decimal("0.016"))
    short = replay.short_mirror_return(Decimal(0), costs)
    long_net = (1 - costs.c_in) * (1 - costs.c_out) - 1 - costs.c_tip
    assert short < 0 and long_net < 0
    assert abs(short - long_net) < Decimal("0.005")


def test_the_short_mirror_is_charged_the_same_round_trip_as_the_long():
    costs = CostTerms(c_in=Decimal("0.03"), c_out=Decimal("0.015"), c_tip=Decimal("0.016"))
    up = Decimal("0.50")
    long_net = (1 - costs.c_in) * (1 + up) * (1 - costs.c_out) - 1 - costs.c_tip
    short_net = replay.short_mirror_return(up, costs)
    assert long_net > 0 > short_net
    # The two are near-mirror images: their sum is roughly minus the round trip, twice.
    assert short_net + long_net < 0


def test_the_short_mirror_refuses_rather_than_assuming_a_cost():
    assert replay.short_mirror_return(Decimal("0.1"), None) is None


def test_the_mirror_honours_the_sol_reprice():
    costs = CostTerms(c_in=Decimal(0), c_out=Decimal(0), c_tip=Decimal(0))
    flat = replay.short_mirror_return(Decimal(0), costs, Decimal(1))
    sol_up = replay.short_mirror_return(Decimal(0), costs, Decimal("0.9"))
    assert flat == Decimal(0)
    assert sol_up > 0, "a token flat in USD while SOL fell is a gain to a lamport short"


# ------------------------------------------------------------- registry and deflation


def test_every_configuration_is_registered_before_its_result_is_read(seeded):
    registry.reset_memo()
    census = replay.EpisodeCensus({120: 1}, {120: 1}, {120: {"eligible": 1}})
    before = registry.trial_count(Lane.MIGRATION_FADE, seeded)
    replay.run_config(
        seeded, ReplayConfig(horizon_s=120, entry_delay_s=30), [_episode(120)], census,
        registry_conn=seeded,
    )
    assert registry.trial_count(Lane.MIGRATION_FADE, seeded) == before + 1


def test_two_configurations_differing_by_one_key_register_as_two(seeded):
    """The defence against five hundred quiet variants and one reported winner."""
    registry.reset_memo()
    census = replay.EpisodeCensus({120: 1}, {120: 1}, {120: {"eligible": 1}})
    before = registry.trial_count(Lane.MIGRATION_FADE, seeded)
    for delay in (30, 45, 60):
        replay.run_config(
            seeded, ReplayConfig(horizon_s=120, entry_delay_s=delay), [_episode(120)],
            census, registry_conn=seeded,
        )
    assert registry.trial_count(Lane.MIGRATION_FADE, seeded) == before + 3


def test_the_registered_params_are_the_effective_lane_params_not_the_overrides(seeded):
    """A sweep that passes fewer keys must not register as a smaller configuration."""
    registry.reset_memo()
    census = replay.EpisodeCensus({120: 1}, {120: 1}, {120: {"eligible": 1}})
    res = replay.run_config(
        seeded, ReplayConfig(horizon_s=120, entry_delay_s=30), [_episode(120)], census,
        registry_conn=seeded,
    )
    assert "sell_within_s" in res.effective_params
    assert "max_hold_s" in res.effective_params


def test_a_registry_write_that_cannot_be_verified_stops_the_run(tmp_path):
    """Registration is the one measurement allowed to abort a replay.

    ``registry.register`` never raises, by design, because on the live decision path a
    lost measurement is better than a lost trade. Here the trade-off inverts: an
    unregistered configuration does not deflate, and a result that does not deflate is
    worse than no result.
    """
    empty = sqlite3.connect(":memory:")
    empty.row_factory = sqlite3.Row
    with pytest.raises(UnregisteredTrialError) as err:
        replay.register_or_refuse(Lane.MIGRATION_FADE, {"a": 1}, empty)
    assert "does not deflate" in str(err.value)


def test_the_dsr_is_flagged_not_deflated_when_there_is_only_one_trial(seeded):
    """One trial means no deflation term. The gate must FAIL, not pass with a footnote."""
    census = replay.EpisodeCensus({120: 1}, {120: 1}, {120: {"eligible": 1}})
    res = replay.run_config(
        seeded, ReplayConfig(horizon_s=120, entry_delay_s=30), [_episode(120)], census,
        registry_conn=seeded,
    )
    replay.deflate(seeded, [res], lane=Lane.MIGRATION_FADE)
    assert res.deflation_ok is False
    # One filled episode cannot even carry a Sharpe, so the gate fails one step earlier
    # than the deflation. Either way it is a FAILED gate, never a missing one.
    assert any("NOT DEFLATED" in n or "NOT COMPUTABLE" in n for n in res.notes)


def test_pbo_reports_unevaluable_rather_than_a_number_it_cannot_support(seeded):
    census = replay.EpisodeCensus({120: 1}, {120: 1}, {120: {"eligible": 1}})
    res = replay.run_config(
        seeded, ReplayConfig(horizon_s=120, entry_delay_s=30), [_episode(120)], census,
        registry_conn=seeded,
    )
    replay.deflate(seeded, [res], lane=Lane.MIGRATION_FADE)
    assert res.pbo is None
    assert any("PBO unevaluable" in n for n in res.notes)


def test_the_block_matrix_is_periods_by_configurations():
    from kaiba.learning.replay import UNKNOWN_DEPTH, EpisodeOutcome

    def outcome(entry_ms: int, ret: str) -> EpisodeOutcome:
        return EpisodeOutcome(
            token="t", t0_ms=0, entry_ms=entry_ms, exit_ms=entry_ms + 1,
            entry_price_usd=Decimal(1), exit_price_usd=Decimal(1), depth=UNKNOWN_DEPTH,
            gross_return=Decimal(0), net_return=Decimal(ret), costs=None,
            fill_basis="dex", signal_strength=None, refused_reason=None,
        )

    census = replay.EpisodeCensus({}, {}, {})
    a = replay.ArmResult("a", ReplayConfig(), "t1", {}, [outcome(0, "0.1"), outcome(600_000, "0.2")],
                         census, 2)
    b = replay.ArmResult("b", ReplayConfig(), "t2", [], [outcome(0, "-0.1")], census, 1)
    matrix = replay.block_matrix([a, b])
    assert len(matrix) == 2 and all(len(r) == 2 for r in matrix)
    assert matrix[0] == [0.1, -0.1]
    assert matrix[1] == [0.2, 0.0]


# ------------------------------------------------------------------------- the census


def test_the_census_counts_every_episode_including_the_excluded_ones(seeded):
    """The denominator is the point. A result without one is not a result."""
    quiet = "QuietTokenNoTapeAtAll00000000000000000001"
    _seed_migration(seeded, token=quiet, t0=T0 + 1000)
    eps, census = replay.discover_episodes(seeded, horizons_s=(300,))
    assert census.counts[300] == 2
    assert census.eligible[300] == 1
    assert census.by_reason[300]["no_tape_in_window"] == 1
    assert len(eps) == 2
    assert sum(1 for e in eps if not e.eligible) == 1


def test_the_census_prints_the_selection_bias_it_cannot_remove(seeded):
    """Eligibility is conditioned on holding tape, and the report must say so out loud."""
    _, census = replay.discover_episodes(seeded, horizons_s=(300,))
    text = chr(10).join(census.lines())
    assert "SELECTION" in text
    assert "correlates with activity" in text


def test_the_census_reports_its_own_sensitivity_to_the_invented_floor(seeded):
    _, census = replay.discover_episodes(seeded, horizons_s=(300,))
    assert set(census.by_min_swaps[300]) == {4, replay.MIN_EPISODE_SWAPS,
                                             replay.MIN_EPISODE_SWAPS * 2}


def test_the_report_prints_the_denominator_before_the_result(seeded):
    report = replay.direction_report(
        seeded, entry_delay_s=30, horizon_s=120, limit=5
    )
    lines = report.lines()
    census_at = next(i for i, ln in enumerate(lines) if "episode census" in ln)
    result_at = next(i for i, ln in enumerate(lines) if "mean net return" in ln or
                     "nothing filled" in ln)
    assert census_at < result_at


def test_the_clock_is_the_event_never_the_mutable_token_row(seeded):
    """`tokens.migrated_ms` is our wall clock. The episode clock must not come from it."""
    seeded.execute(
        "INSERT OR REPLACE INTO tokens (chain, address, migrated_ms, first_seen_ms) "
        "VALUES (?,?,?,?)",
        (Chain.SOL.value, TOKEN, T0 + 999_999, T0),
    )
    eps, _ = replay.discover_episodes(seeded, horizons_s=(300,))
    assert eps[0].t0_ms == T0
    assert eps[0].t0_basis == "event:token.migrated"


def test_record_census_writes_the_excluded_rows_too(seeded):
    quiet = "QuietTokenNoTapeAtAll00000000000000000001"
    _seed_migration(seeded, token=quiet, t0=T0 + 1000)
    eps, _ = replay.discover_episodes(seeded, horizons_s=(300,))
    assert replay.record_census(seeded, eps) == 2
    rows = seeded.execute("SELECT eligible, reason FROM replay_episodes").fetchall()
    assert {r["eligible"] for r in rows} == {0, 1}


# ------------------------------------------------------------------------- reporting


def test_a_report_with_too_little_data_says_unevaluable_not_a_number(seeded):
    report = replay.direction_report(seeded, entry_delay_s=30, horizon_s=120, limit=1)
    assert report.verdict in (Verdict.UNEVALUABLE, Verdict.CANNOT_SEPARATE)
    assert any("no venue" in r for r in report.reasons)


def test_the_verdict_always_leads_with_the_missing_venue(seeded):
    report = replay.direction_report(seeded, entry_delay_s=30, horizon_s=120, limit=2)
    assert "no venue" in report.reasons[0]
    assert "borrow" in report.reasons[0]


def test_bootstrap_ci_refuses_below_three_observations():
    assert replay.bootstrap_ci([Decimal(1), Decimal(2)]) is None
    got = replay.bootstrap_ci([Decimal("0.1"), Decimal("0.2"), Decimal("0.3")])
    assert got is not None and got[0] < got[1]


def test_run_rows_are_appended_with_their_verdict(seeded):
    census = replay.EpisodeCensus({120: 1}, {120: 1}, {120: {"eligible": 1}})
    res = replay.run_config(
        seeded, ReplayConfig(horizon_s=120, entry_delay_s=30), [_episode(120)], census,
        registry_conn=seeded,
    )
    replay.deflate(seeded, [res], lane=Lane.MIGRATION_FADE)
    replay.record_run(seeded, res, verdict=Verdict.CANNOT_SEPARATE)
    row = seeded.execute("SELECT * FROM replay_runs WHERE run_id=?", (res.run_id,)).fetchone()
    assert row["executable"] == 1
    assert row["dsr_deflated"] == 0
    assert row["verdict"] == "cannot_separate"
    assert "PointInTime" in row["cursor_basis"]


# ------------------------------------------------------------------------- migration


def test_migration_029_creates_the_replay_tables(tmp_path):
    conn = sqlite3.connect(tmp_path / "m.db")
    conn.row_factory = sqlite3.Row
    db.migrate(conn)
    names = {
        r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"replay_runs", "replay_episodes", "replay_outcomes"} <= names
    conn.close()


def test_the_replay_never_writes_to_the_connection_it_reads(seeded):
    """A read-only handle must survive a whole run; the registry goes somewhere else."""
    ro = replay.open_readonly(Path(seeded.execute("PRAGMA database_list").fetchone()[2]))
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO events (ts_ms, kind, payload) VALUES (1,'x','{}')")
    eps, census = replay.discover_episodes(ro, horizons_s=(120,))
    assert census.counts[120] >= 1
    ro.close()


def test_a_sweep_deflates_every_configuration_together(seeded):
    """The supported way to try more than one configuration, and the reason it is supported.

    Three variants swept together must register as three trials, and each result must
    carry a note saying the deflation it reports covers all of them. Reporting one of
    them alone is the failure mode; the note is what makes that visible to whoever reads
    a single row later.
    """
    registry.reset_memo()
    census = replay.EpisodeCensus({120: 1}, {120: 1}, {120: {"eligible": 1}})
    before = registry.trial_count(Lane.MIGRATION_FADE, seeded)
    configs = [
        ReplayConfig(horizon_s=120, entry_delay_s=d) for d in (20, 30, 40)
    ]
    results = replay.sweep(
        seeded, configs, [_episode(120)], census, registry_conn=seeded
    )
    assert len(results) == 3
    assert registry.trial_count(Lane.MIGRATION_FADE, seeded) == before + 3
    for res in results:
        assert any("swept together with 2 other" in n for n in res.notes)
        assert res.honest_trials is not None and res.honest_trials >= 3


def test_the_block_matrix_makes_pbo_computable_which_it_has_never_been_here(seeded):
    """`gates.pbo_cscv` has had no matrix in this repository until now. Prove it takes one.

    ``validation``'s own docstring concedes that gate 2 reports PBO as unevaluable
    because nobody builds a T-by-N trial matrix. A replay produces one naturally, and
    this asserts the published procedure accepts it and returns a probability rather
    than the ``None`` it returns on a shape it cannot support.
    """
    from kaiba.learning import gates
    from kaiba.learning.replay import UNKNOWN_DEPTH, EpisodeOutcome

    def arm(name: str, rets: list[str]) -> replay.ArmResult:
        outs = [
            EpisodeOutcome(
                token=f"t{i}", t0_ms=0, entry_ms=i * replay.BLOCK_S * 1000,
                exit_ms=i * replay.BLOCK_S * 1000 + 1, entry_price_usd=Decimal(1),
                exit_price_usd=Decimal(1), depth=UNKNOWN_DEPTH, gross_return=Decimal(0),
                net_return=Decimal(r), costs=None, fill_basis="dex",
                signal_strength=None, refused_reason=None,
            )
            for i, r in enumerate(rets)
        ]
        return replay.ArmResult(name, ReplayConfig(), name, {}, outs,
                                replay.EpisodeCensus({}, {}, {}), len(outs))

    a = arm("a", ["0.05", "-0.02", "0.04", "-0.01", "0.06", "-0.03", "0.02", "0.01",
                  "0.03", "-0.02", "0.05", "0.00"])
    b = arm("b", ["-0.04", "0.03", "-0.05", "0.02", "-0.06", "0.04", "-0.01", "0.00",
                  "-0.03", "0.01", "-0.02", "0.03"])
    matrix = replay.block_matrix([a, b])
    assert len(matrix) == 12 and len(matrix[0]) == 2
    pbo = gates.pbo_cscv(matrix)
    assert pbo is not None and 0.0 <= pbo <= 1.0


def test_a_caller_supplied_where_clause_cannot_disarm_the_time_bound(seeded):
    """The caller's filter is parenthesised, so an OR in it cannot widen the bound.

    Without the brackets, ``ts_ms <= ? AND token=? OR 1=1`` binds as
    ``(ts_ms <= ? AND token=?) OR 1=1`` and returns the whole table. The row guard would
    still catch it, which is the point of having two mechanisms, but the bracket is what
    stops it happening at all.
    """
    cursor = T0 + 60_000
    seeded.execute(
        "INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, amount_native, "
        "price_usd, program, source) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (Chain.SOL.value, "tx_future", cursor + 5_000, "w", TOKEN, "buy", "1", "1",
         "pump_amm", "pumpfun:trades"),
    )
    view = PointInTime(seeded, cursor)
    rows = view.rows("swaps", where="token=? OR 1=1", params=(TOKEN,))
    assert all(int(r["ts_ms"]) <= cursor for r in rows)


def test_pbo_is_withheld_for_a_long_and_its_own_mirror(seeded):
    """One price path with the sign flipped is not two strategies to select between.

    PBO on that pair is 1.0 by construction - the in-sample winner is necessarily the
    out-of-sample loser - and reporting it as an overfitting measure would be a number
    that means nothing wearing the name of one that means a lot.
    """
    census = replay.EpisodeCensus({120: 1}, {120: 1}, {120: {"eligible": 1}})
    eps = [_episode(120)]
    long_res = replay.run_config(
        seeded, ReplayConfig(horizon_s=120, entry_delay_s=30, arm=Arm.LONG), eps,
        census, registry_conn=seeded,
    )
    short_res = replay.run_config(
        seeded, ReplayConfig(horizon_s=120, entry_delay_s=30, arm=Arm.SHORT_MIRROR), eps,
        census, registry_conn=seeded,
    )
    replay.deflate(seeded, [long_res, short_res], lane=Lane.MIGRATION_FADE)
    assert long_res.pbo is None and short_res.pbo is None
    assert any("PBO withheld" in n for n in long_res.notes)


def test_a_total_loss_is_counted_not_allowed_to_fail_the_reconciliation():
    """The broker clamps proceeds at zero, so a wipeout is a floor, not a cost error."""
    from kaiba.learning.replay import UNKNOWN_DEPTH, ArmResult, EpisodeOutcome

    wiped = EpisodeOutcome(
        token="t", t0_ms=0, entry_ms=0, exit_ms=1, entry_price_usd=Decimal(1),
        exit_price_usd=Decimal("0.01"), depth=UNKNOWN_DEPTH,
        gross_return=Decimal("-0.99"), net_return=Decimal("-1"),
        costs=CostTerms(Decimal("0.03"), Decimal("0.015"), Decimal("0.016")),
        fill_basis="dex", signal_strength=None, refused_reason=None, fx=Decimal(1),
    )
    res = ArmResult("r", ReplayConfig(), "t1", {}, [wiped],
                    replay.EpisodeCensus({}, {}, {}), 1)
    replay._reconcile_costs(res)
    joined = " ".join(res.notes)
    assert "FAILED" not in joined
    assert "1 position(s) booked a total loss" in joined
