"""Offline receipts for prospective PaperBroker candidate books (Codex-owned)."""

from __future__ import annotations

import copy
import json
import sqlite3
from decimal import Decimal

import pytest

from kaiba.core.schemas import Chain, LaneMode
from kaiba.execution.paper import PaperBroker
from kaiba.learning.candidate_paper import CandidatePaperRunner

T0 = 1_000_000


def manifest(*, arms=1, hold_ms=60_000, chain="sol"):
    return {
        "campaign_id": "campaign", "selected_ms": T0,
        "hypotheses": [{
            "hypothesis_id": f"arm-{i}", "spec_hash": f"frozen-{i}", "status": "PAPER_ONLY",
            "kind": "exploratory", "spec": {
                "lane": "sm-trenches", "chain": chain, "filter": [],
                "exit": {"name": "test", "stop_frac": .30, "tps": [[2, .5], [5, .25]],
                         "trailing": [[2, .3]], "breakeven_after_tp1": True, "hold_ms": hold_ms},
            },
        } for i in range(arms)],
    }


def opportunity(token="token", *, chain="sol", decision_id="decision", ts=T0 + 1000):
    return {
        "decision_id": decision_id, "ts_ms": ts, "lane": "sm-trenches", "mode": "live",
        "chain": chain, "token": token, "size_base_units": 100_000_000 if chain == "sol" else 10**16,
        "features": {"count": 3}, "quote_ts_ms": ts, "price_usd": "1", "liquidity_usd": "1000000",
        "native_usd": "100" if chain == "sol" else "2000", "decimals": 6 if chain == "sol" else 18,
        "source": "fixture-observed-pool", "tax_known_zero": True,
        "venue_fee_bps": 100 if chain == "bsc" else 60 if chain == "robinhood" else 125,
        "venue_fee_source": "synthetic-fixture-venue",
    }


def quote(now, *, price="1", liquidity="1000000", chain="sol", **extra):
    return {
        "quote_ts_ms": now, "price_usd": price, "liquidity_usd": liquidity,
        "native_usd": "100" if chain == "sol" else "2000", "decimals": 6 if chain == "sol" else 18,
        "source": "fixture-observed-pool", "tax_known_zero": True,
        "venue_fee_bps": 100 if chain == "bsc" else 60 if chain == "robinhood" else 125,
        "venue_fee_source": "synthetic-fixture-venue", **extra,
    }


@pytest.fixture
def runner(tmp_path):
    return CandidatePaperRunner(tmp_path / "learning" / "paper", risk={"mode": "live"})


def run(runner, m, opps=(), *, now=T0 + 31_000, token="token", q=None):
    quotes = {} if q is False else {(m["hypotheses"][0]["spec"]["chain"], token): q or quote(now)}
    return runner.run_candidates(m, opps, quotes, now_ms=now)


def read_book(runner, m, sql):
    path = runner.book_path(m["campaign_id"], m["hypotheses"][0]["hypothesis_id"])
    with sqlite3.connect(path) as conn:
        return conn.execute(sql).fetchall()


def test_hard_shadow_uses_existing_broker_despite_live_runtime(runner, monkeypatch):
    seen = []
    original = PaperBroker.buy

    def spy(self, decision, **kwargs):
        seen.append(decision.mode)
        return original(self, decision, **kwargs)

    monkeypatch.setattr(PaperBroker, "buy", spy)
    m = manifest()
    result = run(runner, m, [opportunity()])
    assert not result["errors"]
    assert seen == [LaneMode.SHADOW]
    assert read_book(runner, m, "SELECT mode,provider,state FROM orders") == [("shadow", "paper", "filled")]


def test_empty_stream_creates_no_orders(runner):
    result = run(runner, manifest(), q=False)
    assert result["outcomes"] == []
    assert result["arms"][0]["order_count"] == 0


def test_warm_twelve_arm_poll_does_not_repeat_migrations_schema_ddl_or_idle_expiry_writes(runner, monkeypatch):
    from kaiba.learning import candidate_paper as cp

    m = manifest(arms=12)
    run(runner, m, q=False)
    migrations, statements, configured_writers = [], [], []
    original_migrate, original_connect = cp.migrate, sqlite3.connect
    original_core_connect = cp.connect

    def observe_migrate(conn):
        migrations.append(conn)
        return original_migrate(conn)

    def observe_connect(*args, **kwargs):
        conn = original_connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    def observe_writer(path):
        configured_writers.append(path)
        return original_core_connect(path)

    monkeypatch.setattr(cp, "migrate", observe_migrate)
    monkeypatch.setattr(cp, "connect", observe_writer)
    monkeypatch.setattr(sqlite3, "connect", observe_connect)
    observer = original_connect(runner.book_path("campaign", "arm-0"))
    try:
        version = observer.execute("PRAGMA data_version").fetchone()[0]
        expiry = runner.expire_pending(m, now_ms=T0 + 32_000)
        assert not expiry["errors"]
        assert runner.watched_tokens(m, now_ms=T0 + 32_000) == []
        idle_changes = observer.execute("PRAGMA data_version").fetchone()[0] - version
    finally:
        observer.close()
    result = run(runner, m, now=T0 + 32_000, q=False)
    assert not result["errors"] and len(result["arms"]) == 12
    ddl = [s for s in statements if s.lstrip().upper().startswith(("CREATE ", "ALTER "))]
    expiry_writes = [s for s in statements if "INSERT" in s.upper() and "last_expiry_ms" in s]
    counts = {"configured_writer_connections": len(configured_writers), "migration_calls": len(migrations), "schema_ddl": len(ddl),
              "idle_expiry_marker_writes": len(expiry_writes), "observed_idle_commits": idle_changes}
    assert counts == {"configured_writer_connections": 12, "migration_calls": 0, "schema_ddl": 0,
                      "idle_expiry_marker_writes": 0, "observed_idle_commits": 0}, counts


def test_actual_processing_clock_refuses_stale_first_arm_and_expired_second_arm_after_slow_book_io(runner, monkeypatch):
    m, clock = manifest(arms=2), {"ms": T0 + 31_000}
    runner.clock = lambda: clock["ms"] / 1000
    original = runner._open

    def delayed(identity):
        conn = original(identity)
        clock["ms"] += 70_000
        return conn

    monkeypatch.setattr(runner, "_open", delayed)
    result = run(runner, m, [opportunity()])
    assert not result["errors"] and [a["order_count"] for a in result["arms"]] == [0, 0]
    assert result["arms"][0]["incomplete"][0]["reason"] == "quote_stale_or_future"
    assert result["refusals"][0]["known_unfilled"] and result["refusals"][0]["no_position_created"]
    assert result["refusals"][0]["expired_ms"] == T0 + 171_000
    assert result["refusals"][0]["exposure_check"] == {"attempt_orders": 0, "attempt_positions": 0}
    assert result["refusals"][0]["expiry_diagnostics"]["cause"] == "book_processing_crossed_deadline"
    assert result["refusals"][0]["candidate_received_ms"] == T0 + 31_000
    assert result["now_ms"] == T0 + 31_000 and result["processing_frontier_ms"] == T0 + 171_000


def test_clocked_fresh_positive_control_preserves_market_clock_and_records_processing_frontier(runner, monkeypatch):
    m, clock = manifest(arms=2), {"ms": T0 + 31_000}
    runner.clock = lambda: clock["ms"] / 1000
    original = runner._open

    def delayed(identity):
        conn = original(identity)
        clock["ms"] += 5_000
        return conn

    monkeypatch.setattr(runner, "_open", delayed)
    result = run(runner, m, [opportunity()])
    assert [a["order_count"] for a in result["arms"]] == [1, 1]
    assert result["now_ms"] == T0 + 31_000 and result["processing_frontier_ms"] == T0 + 41_000
    for index, arm in enumerate(result["arms"]):
        with sqlite3.connect(arm["book_path"]) as conn:
            opened, processing = conn.execute("SELECT p.opened_ms,e.processing_ms FROM positions p "
                                               "JOIN candidate_entries e ON e.position_id=p.position_id").fetchone()
            evidence = json.loads(conn.execute("SELECT value FROM candidate_meta WHERE key LIKE 'fill_evidence:%'").fetchone()[0])
        assert opened == T0 + 31_000
        assert processing == T0 + 36_000 + index * 5_000
        assert evidence["quote_ms"] == evidence["price_ts_ms"] == T0 + 31_000
        assert evidence["processing_ms"] == processing


def test_processing_clock_is_rechecked_between_entries_and_does_not_expire_owned_inventory(runner, monkeypatch):
    m, clock = manifest(), {"ms": T0 + 31_000}
    runner.clock = lambda: clock["ms"] / 1000
    original = PaperBroker.buy

    def slow_buy(self, *args, **kwargs):
        order = original(self, *args, **kwargs)
        clock["ms"] += 100_000
        return order

    monkeypatch.setattr(PaperBroker, "buy", slow_buy)
    raws = [opportunity("one", decision_id="a"), opportunity("two", decision_id="b")]
    result = runner.run_candidates(m, raws, {("sol", t): quote(T0 + 31_000) for t in ("one", "two")}, now_ms=T0 + 31_000)
    assert not result["errors"] and result["arms"][0]["order_count"] == result["arms"][0]["open"] == 1
    assert result["refusals"][0]["signal_id"] == "b" and result["refusals"][0]["known_unfilled"]
    assert read_book(runner, m, "SELECT token,closed_ms FROM positions") == [("one", None)]


@pytest.mark.parametrize("stall_ms,reason", [(70_000, "quote_stale_or_future"), (100_000, "latency_unreachable")])
def test_slow_entry_preparation_rechecks_clock_before_broker_and_rolls_back_unsubmitted_rows(runner, monkeypatch, stall_ms, reason):
    clock = {"ms": T0 + 31_000}
    runner.clock = lambda: clock["ms"] / 1000
    original, stalled = runner._open, []

    def traced(identity):
        conn = original(identity)

        def slow_prepare(sql):
            if "INSERT INTO tokens" in sql and not stalled:
                stalled.append(True)
                clock["ms"] += stall_ms

        conn.set_trace_callback(slow_prepare)
        return conn

    monkeypatch.setattr(runner, "_open", traced)
    m = manifest()
    result = run(runner, m, [opportunity()])
    assert not result["errors"] and result["arms"][0]["order_count"] == 0
    assert result["arms"][0]["incomplete"][0]["reason"] == reason
    assert read_book(runner, m, "SELECT COUNT(*) FROM decisions") == [(0,)]
    assert read_book(runner, m, "SELECT COUNT(*) FROM tokens") == [(0,)]
    assert read_book(runner, m, "SELECT COUNT(*) FROM positions") == [(0,)]
    if stall_ms == 100_000:
        diagnostics = result["refusals"][0]["expiry_diagnostics"]
        assert diagnostics["cause"] == "preparation_crossed_deadline"
        assert diagnostics["last_admission_ms"] == diagnostics["preparation_started_ms"] == T0 + 31_000
        assert diagnostics["last_usable_quote"]["price_ts_ms"] == T0 + 31_000


def test_exit_processing_clock_is_rechecked_after_slow_inventory_read(runner, monkeypatch):
    from kaiba.learning import candidate_paper as cp

    clock, m = {"ms": T0 + 31_000}, manifest()
    runner.clock = lambda: clock["ms"] / 1000
    run(runner, m, [opportunity()])
    original = cp.load_position

    def slow_read(*args):
        position = original(*args)
        clock["ms"] += 70_000
        return position

    clock["ms"] = T0 + 104_000
    monkeypatch.setattr(cp, "load_position", slow_read)
    stale = run(runner, m, now=T0 + 104_000)
    assert stale["outcomes"] == [] and stale["arms"][0]["order_count"] == 1
    assert stale["arms"][0]["incomplete"][0]["reason"] == "quote_stale_or_future"
    monkeypatch.setattr(cp, "load_position", original)
    clock["ms"] = T0 + 175_000
    fresh = run(runner, m, now=T0 + 175_000)
    assert len(fresh["outcomes"]) == 1 and fresh["arms"][0]["order_count"] == 2
    assert fresh["outcomes"][0]["closed_ms"] == T0 + 175_000
    assert fresh["outcomes"][0]["fill_evidence"][1]["processing_ms"] == T0 + 175_000


def test_processing_clock_also_rechecks_older_depth_component(runner, monkeypatch):
    clock, m = {"ms": T0 + 31_000}, manifest()
    runner.clock = lambda: clock["ms"] / 1000
    original = runner._open

    def delayed(identity):
        conn = original(identity)
        clock["ms"] += 40_000
        return conn

    monkeypatch.setattr(runner, "_open", delayed)
    result = run(runner, m, [opportunity()], q=quote(T0 + 31_000, depth_ts_ms=T0 + 1000))
    assert result["arms"][0]["order_count"] == 0
    assert result["arms"][0]["incomplete"][0]["reason"] == "depth_stale_or_future"


@pytest.mark.parametrize("native_age,expected_orders", [(299_000, 0), (290_000, 1)])
def test_native_price_five_minute_age_is_rechecked_after_book_io(runner, monkeypatch, native_age, expected_orders):
    clock, m = {"ms": T0 + 31_000}, manifest()
    runner.clock = lambda: clock["ms"] / 1000
    original = runner._open

    def delayed(identity):
        conn = original(identity)
        clock["ms"] += 10_000
        return conn

    monkeypatch.setattr(runner, "_open", delayed)
    native_at = T0 + 31_000 - native_age
    result = run(runner, m, [opportunity()], q=quote(T0 + 31_000, native_price_ts_ms=native_at))
    assert not result["errors"] and result["arms"][0]["order_count"] == expected_orders
    if not expected_orders:
        assert result["arms"][0]["incomplete"][0]["reason"] == "native_price_stale_or_future"
        assert read_book(runner, m, "SELECT COUNT(*) FROM decisions") == [(0,)]
    else:
        evidence = json.loads(read_book(runner, m, "SELECT value FROM candidate_meta WHERE key LIKE 'fill_evidence:%'")[0][0])
        assert evidence["native_price_ts_ms"] == native_at
        assert evidence["native_usd"] == "100"
        assert evidence["quote_ms"] == T0 + 31_000 and evidence["processing_ms"] == T0 + 41_000


@pytest.mark.parametrize("native_clock", [T0 + 31_001, T0 + 31_000.5, True, "NaN", "Infinity", None, "bad"])
def test_invalid_native_clock_cannot_be_relabelled_as_a_fresh_fx_basis(runner, native_clock):
    m = manifest()
    result = run(runner, m, [opportunity()], q=quote(T0 + 31_000, native_price_ts_ms=native_clock))
    assert result["arms"][0]["order_count"] == 0
    assert result["arms"][0]["incomplete"][0]["reason"] in {"native_price_timestamp_invalid", "quote_malformed"}
    assert read_book(runner, m, "SELECT COUNT(*) FROM positions") == [(0,)]


def test_processing_clock_policy_switch_requires_a_new_frozen_book(runner):
    m = manifest()
    run(runner, m, [opportunity()])
    runner.clock = lambda: (T0 + 104_000) / 1000
    result = run(runner, m, now=T0 + 104_000)
    assert "frozen candidate identity" in result["errors"][0]["error"]
    assert read_book(runner, m, "SELECT COUNT(*) FROM orders") == [(1,)]


def test_processing_clock_guard_disabled_mutation_is_caught(runner, monkeypatch):
    monkeypatch.setattr(runner, "_processing_ms", lambda frontier: int(frontier))
    with pytest.raises(AssertionError):
        test_actual_processing_clock_refuses_stale_first_arm_and_expired_second_arm_after_slow_book_io(runner, monkeypatch)


def test_missing_real_core_migration_is_applied_once_and_prior_identity_is_retained(runner, monkeypatch):
    from kaiba.learning import candidate_paper as cp

    m = manifest()
    run(runner, m, q=False)
    prior = read_book(runner, m, "SELECT value FROM candidate_meta WHERE key='identity'")
    with sqlite3.connect(runner.book_path("campaign", "arm-0")) as conn:
        conn.execute("DELETE FROM schema_migrations WHERE name='047_strategy_search.sql'")
    original, calls = cp.migrate, []

    def observe(conn):
        calls.append(original(conn))

    monkeypatch.setattr(cp, "migrate", observe)
    assert not run(runner, m, now=T0 + 32_000, q=False)["errors"]
    assert calls == [["047_strategy_search.sql"]]
    assert not run(runner, m, now=T0 + 33_000, q=False)["errors"]
    assert len(calls) == 1 and read_book(runner, m, "SELECT value FROM candidate_meta WHERE key='identity'") == prior


def test_identity_conflict_is_rejected_before_any_schema_upgrade(runner, monkeypatch):
    from kaiba.learning import candidate_paper as cp

    m = manifest()
    run(runner, m, [opportunity()], now=T0 + 1000)
    with sqlite3.connect(runner.book_path("campaign", "arm-0")) as conn:
        conn.execute("ALTER TABLE candidate_entries DROP COLUMN expired_ms")
    statements, original = [], cp.connect

    def observe(path):
        conn = original(path)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(cp, "connect", observe)
    changed = copy.deepcopy(m)
    changed["hypotheses"][0]["spec_hash"] = "different-policy"
    assert "frozen candidate identity" in run(runner, changed)["errors"][0]["error"]
    assert not any(s.lstrip().upper().startswith(("CREATE ", "ALTER ")) for s in statements)
    assert "expired_ms" not in {r[1] for r in read_book(runner, m, "PRAGMA table_info(candidate_entries)")}
    assert read_book(runner, m, "SELECT COUNT(*) FROM orders") == [(0,)]


def test_repeated_expiry_reads_new_attempts_and_never_caches_zero_exposure(runner):
    m = manifest()
    run(runner, m, q=False)
    late = T0 + 180_000
    assert runner.expire_pending(m, now_ms=late)["expired"] == 0
    raw = opportunity(decision_id="externally-registered")
    raw["signal_ms"], raw["signal_id"] = raw["ts_ms"], raw["decision_id"]
    with sqlite3.connect(runner.book_path("campaign", "arm-0")) as conn:
        conn.execute("INSERT INTO candidate_entries(signal_id,payload,status) VALUES (?,?,'pending')",
                     (raw["signal_id"], json.dumps(raw)))
    result = runner.expire_pending(m, now_ms=late)
    assert result["expired"] == 1 and not result["errors"]
    assert read_book(runner, m, "SELECT status,reason FROM candidate_entries") == [("rejected", "latency_unreachable")]


def test_expiry_rechecks_identity_after_readonly_probe_and_writer_acquisition(runner, monkeypatch):
    m = manifest()
    run(runner, m, [opportunity()], now=T0 + 1000)
    original = runner._open

    def changed_after_open(identity):
        conn = original(identity)
        conn.execute("UPDATE candidate_meta SET value='{}' WHERE key='identity'")
        return conn

    monkeypatch.setattr(runner, "_open", changed_after_open)
    result = runner.expire_pending(m, now_ms=T0 + 180_000)
    assert result["expired"] == 0 and "frozen candidate identity" in result["errors"][0]["error"]
    assert read_book(runner, m, "SELECT status,expired_ms FROM candidate_entries") == [("pending", None)]
    assert read_book(runner, m, "SELECT COUNT(*) FROM orders") == [(0,)]


def test_duplicate_decision_does_not_add_inventory(runner):
    m, opp = manifest(), opportunity()
    first = run(runner, m, [opp])
    second = run(runner, m, [opp], now=T0 + 32_000)
    assert first["arms"][0]["order_count"] == second["arms"][0]["order_count"] == 1
    assert len(read_book(runner, m, "SELECT position_id FROM positions")) == 1
    assert second["arms"][0]["entries"] == {"opened": 1}
    assert second["refusals"] == []
    assert second["assessment_rows"][0]["status"] == "open"


def test_frozen_identity_rejects_policy_hash_and_execution_changes(runner):
    m = manifest()
    run(runner, m, [opportunity()])
    changed = copy.deepcopy(m)
    changed["hypotheses"][0]["spec"]["exit"]["hold_ms"] = 300_000
    result = run(runner, changed, [opportunity("other", decision_id="new")], token="other")
    assert "frozen candidate identity" in result["errors"][0]["error"]
    assert read_book(runner, m, "SELECT COUNT(*) FROM orders") == [(1,)]


def test_only_prospective_features_are_eligible(runner):
    m = manifest()
    old, future = opportunity(decision_id="old", ts=T0), opportunity(decision_id="future")
    future["feature_ts_ms"] = T0 + 9000
    result = run(runner, m, [old, future])
    assert result["arms"][0]["order_count"] == 0
    assert {r["reason"] for r in result["arms"][0]["incomplete"]} == {"outside_prospective_window", "future_features"}


def test_pending_feature_snapshot_survives_until_post_delay_quote(runner):
    m, opp = manifest(), opportunity()
    first = run(runner, m, [opp], now=T0 + 1000)
    assert first["arms"][0]["order_count"] == 0
    second = run(runner, m, now=T0 + 61_000)
    assert second["arms"][0]["open"] == 1
    assert read_book(runner, m, "SELECT latency_ms FROM candidate_entries") == [(60_000,)]


@pytest.mark.parametrize("new_component", ["depth_ts_ms", "metadata_ts_ms"])
def test_late_depth_or_precision_cannot_open_with_a_predelay_market_price(runner, new_component):
    m = manifest()
    available = T0 + 31_000
    old_price = T0 + 500  # before the decision, yet still inside quote freshness
    mixed = quote(available, price_ts_ms=old_price, **{new_component: available})
    result = run(runner, m, [opportunity()], now=available, q=mixed)
    assert result["arms"][0]["order_count"] == 0
    assert result["assessment_rows"][0]["last_quote_reason"] == "entry_price_before_delay"
    assert read_book(runner, m, "SELECT COUNT(*) FROM decisions") == [(0,)]
    assert read_book(runner, m, "SELECT COUNT(*) FROM positions") == [(0,)]
    # Even a later packet must wait for an actual market print at the cutoff.
    result = run(runner, m, now=T0 + 41_000,
                 q=quote(T0 + 41_000, price_ts_ms=T0 + 30_000))
    assert result["arms"][0]["order_count"] == 0
    opened = run(runner, m, now=T0 + 45_000,
                 q=quote(T0 + 45_000, price_ts_ms=T0 + 31_000))
    assert opened["arms"][0]["open"] == 1 and not opened["errors"]
    # PaperBroker receives availability time, never the earlier component clock.
    assert read_book(runner, m, "SELECT opened_ms FROM positions") == [(T0 + 45_000,)]
    assert read_book(runner, m, "SELECT latency_ms FROM candidate_entries") == [(44_000,)]
    evidence = json.loads(read_book(runner, m, "SELECT value FROM candidate_meta WHERE key LIKE 'fill_evidence:%'")[0][0])
    assert evidence["quote_ms"] == T0 + 45_000 and evidence["price_ts_ms"] == T0 + 31_000


@pytest.mark.parametrize("price_clock,reason", [
    (T0 - 40_000, "price_stale_or_future"),
    (T0 + 32_000, "price_timestamp_invalid"),
    (T0 + 31_000.5, "price_timestamp_invalid"),
    (True, "price_timestamp_invalid"),
])
def test_price_clock_itself_must_be_fresh_integral_and_not_after_availability(runner, price_clock, reason):
    result = run(runner, manifest(), [opportunity()],
                 q=quote(T0 + 31_000, price_ts_ms=price_clock))
    assert result["arms"][0]["order_count"] == 0
    assert result["assessment_rows"][0]["last_quote_reason"] == reason


def test_late_depth_cannot_fill_time_exit_with_a_market_price_before_exit_due(runner):
    m = manifest()
    run(runner, m, [opportunity()])
    # Entry31s + hold60s + poll12s: actual sell print must be >=103s.
    run(runner, m, now=T0 + 100_000,
        q=quote(T0 + 100_000, price_ts_ms=T0 + 90_000))
    too_early = run(runner, m, now=T0 + 110_000,
                    q=quote(T0 + 110_000, price_ts_ms=T0 + 102_000, depth_ts_ms=T0 + 110_000))
    assert too_early["outcomes"] == [] and too_early["arms"][0]["order_count"] == 1
    # Changing metadata without another market observation cannot unlock it.
    repeated = run(runner, m, now=T0 + 111_000,
                   q=quote(T0 + 111_000, price_ts_ms=T0 + 102_000, metadata_ts_ms=T0 + 111_000))
    assert repeated["arms"][0]["order_count"] == 1
    closed = run(runner, m, now=T0 + 115_000,
                 q=quote(T0 + 115_000, price_ts_ms=T0 + 103_000))
    assert len(closed["outcomes"]) == 1 and closed["arms"][0]["order_count"] == 2
    assert closed["outcomes"][0]["closed_ms"] == T0 + 115_000
    assert closed["outcomes"][0]["fill_evidence"][1]["price_ts_ms"] == T0 + 103_000


def test_tp_exit_deduplicates_market_observations_when_only_packet_metadata_advances(runner):
    m = manifest(hold_ms=None)
    run(runner, m, [opportunity()])
    run(runner, m, now=T0 + 40_000, q=quote(T0 + 40_000, price="2.5"))
    # The trigger was available at40s; delay ends52s. Price50s cannot fill.
    premature = run(runner, m, now=T0 + 55_000,
                     q=quote(T0 + 55_000, price="2.5", price_ts_ms=T0 + 50_000))
    assert premature["arms"][0]["order_count"] == 1
    run(runner, m, now=T0 + 57_000,
        q=quote(T0 + 57_000, price="2.5", price_ts_ms=T0 + 52_000))
    qty_before = read_book(runner, m, "SELECT qty FROM positions")
    duplicate = run(runner, m, now=T0 + 60_000,
                    q=quote(T0 + 60_000, price="6", price_ts_ms=T0 + 52_000))
    assert duplicate["arms"][0]["order_count"] == 2
    assert read_book(runner, m, "SELECT qty FROM positions") == qty_before
    assert read_book(runner, m, "SELECT last_price_ms,last_quote_ms,pending FROM candidate_protection") == [
        (T0 + 52_000, T0 + 57_000, None)]


def test_protection_clock_migration_preserves_legacy_receipt_evidence(runner):
    m = manifest()
    run(runner, m, [opportunity()])
    with sqlite3.connect(runner.book_path("campaign", "arm-0")) as conn:
        conn.execute("ALTER TABLE candidate_protection DROP COLUMN last_price_ms")
    # Newly arrived metadata for the same old print cannot create a sell.
    result = run(runner, m, now=T0 + 104_000,
                 q=quote(T0 + 104_000, price_ts_ms=T0 + 90_000))
    assert result["outcomes"] == [] and result["arms"][0]["order_count"] == 1
    assert read_book(runner, m, "SELECT last_price_ms,last_quote_ms FROM candidate_protection") == [
        (T0 + 90_000, T0 + 104_000)]


def test_collapsing_price_to_availability_clock_mutation_is_caught(runner, monkeypatch):
    original = runner._snapshot

    def collapsed(raw, now):
        snapshot, reason = original(raw, now)
        if snapshot is not None:
            snapshot["price_ms"] = snapshot["quote_ms"]
        return snapshot, reason

    monkeypatch.setattr(runner, "_snapshot", collapsed)
    with pytest.raises(AssertionError):
        test_late_depth_or_precision_cannot_open_with_a_predelay_market_price(runner, "depth_ts_ms")


def test_price_clock_policy_change_cannot_silently_relabel_an_older_book(runner):
    m = manifest()
    run(runner, m, [opportunity()])
    with sqlite3.connect(runner.book_path("campaign", "arm-0")) as conn:
        prior = json.loads(conn.execute("SELECT value FROM candidate_meta WHERE key='identity'").fetchone()[0])
        del prior["runner"]["price_clock_policy"]
        prior_json = json.dumps(prior, sort_keys=True, separators=(",", ":"))
        conn.execute("UPDATE candidate_meta SET value=? WHERE key='identity'", (prior_json,))
    result = run(runner, m, now=T0 + 104_000)
    assert "frozen candidate identity" in result["errors"][0]["error"]
    assert read_book(runner, m, "SELECT value FROM candidate_meta WHERE key='identity'") == [(prior_json,)]
    assert read_book(runner, m, "SELECT COUNT(*) FROM orders") == [(1,)]
    assert read_book(runner, m, "SELECT closed_ms FROM positions") == [(None,)]


def test_late_quote_is_refused_not_retroactively_filled(runner):
    m = manifest()
    run(runner, m, [opportunity()], now=T0 + 1000)
    result = run(runner, m, now=T0 + 122_000)
    assert result["arms"][0]["order_count"] == 0
    assert result["arms"][0]["incomplete"][0]["reason"] == "latency_unreachable"


def test_unfillable_entry_and_missing_exit_never_invent_win(runner):
    m = manifest()
    first = run(runner, m, [opportunity()], q=quote(T0 + 31_000, liquidity="0.01"))
    assert first["arms"][0]["entries"] == {"failed": 1}
    assert first["arms"][0]["open"] == 0
    assert first["outcomes"] == []
    m["campaign_id"] = "fresh-campaign"
    run(runner, m, [opportunity()])
    missing = run(runner, m, now=T0 + 110_000, q=quote(T0 + 110_000, liquidity=None))
    assert missing["arms"][0]["open"] == 1
    assert missing["outcomes"] == []
    assert missing["arms"][0]["order_count"] == 1


def test_flat_cap_includes_tip_and_does_not_scale_on_confidence(runner):
    m, opp = manifest(), opportunity()
    opp["size_base_units"] = 10**11
    opp["confidence"] = .99
    run(runner, m, [opp])
    debit = int(read_book(runner, m, "SELECT cost_native FROM positions")[0][0])
    assert Decimal(debit) / 10**9 * 100 <= 120
    assert debit == 1_200_000_000


def test_closed_outcomes_are_broker_fee_inclusive_with_stable_evidence(runner):
    m = manifest()
    run(runner, m, [opportunity()])
    result = run(runner, m, now=T0 + 104_000)
    assert not result["errors"]
    row = result["outcomes"][0]
    assert row["status"] == "closed" and row["source"] == "PaperBroker"
    assert row["fees_native"] > 0 and row["fees_included"]
    assert row["pnl_native"] == row["exit_credit_native"] - row["entry_debit_native"]
    assert row["net_return"] == pytest.approx(row["pnl_native"] / row["cost_native"])
    assert row["net_return"] < 0  # identical mid prices still pay both fees, impact and tips
    assert row["evidence_complete"] and row["cost_complete"] and row["inventory_complete"]
    assert row["cost_model_assumed"] and not row["live_cost_verified"]
    assert row["hypothesis_id"] == "arm-0" and row["signal_id"] == "decision"
    assert len(row["basis"]) == 2 and all(b["basis"] == "dex" for b in row["basis"])
    repeated = run(runner, m, now=T0 + 120_000)["outcomes"][0]
    assert repeated["outcome_id"] == row["outcome_id"]
    assert read_book(runner, m, "SELECT COUNT(*) FROM orders") == [(2,)]


def fee_only_broker(monkeypatch):
    """Isolate percentages using the real broker, excluding separately tested flat/impact costs."""
    original = CandidatePaperRunner._broker

    def configured(conn, chain, snapshot, *, side):
        broker = original(conn, chain, snapshot, side=side)
        broker.tip_native[chain] = 0
        broker.slippage_floor_bps = 0
        return broker

    monkeypatch.setattr(CandidatePaperRunner, "_broker", staticmethod(configured))


@pytest.mark.parametrize("chain,fee_bps", [("sol", 225), ("robinhood", 160), ("bsc", 200)])
def test_assumed_commission_is_charged_once_in_actual_pool_cashflows(runner, monkeypatch, chain, fee_bps):
    fee_only_broker(monkeypatch)
    m, raw = manifest(chain=chain), opportunity(chain=chain)
    run(runner, m, [raw], q=quote(T0 + 31_000, chain=chain, liquidity="1e40"))
    result = run(runner, m, now=T0 + 104_000, q=quote(T0 + 104_000, chain=chain, liquidity="1e40"))
    row = result["outcomes"][0]
    amount = raw["size_base_units"]
    gross_exit = amount - amount * fee_bps // 10_000
    expected_credit = gross_exit - gross_exit * fee_bps // 10_000
    assert row["entry_debit_native"] == amount and row["exit_credit_native"] == expected_credit
    assert row["net_return"] == pytest.approx((1 - fee_bps / 10_000) ** 2 - 1)
    assert [b["fee_bps"] for b in row["basis"]] == [fee_bps, fee_bps]
    assert row["fees_native"] == amount - gross_exit + gross_exit - expected_credit
    assert row["cost_complete"] and row["cost_model_assumed"] and not row["live_cost_verified"]
    assert row["fee_policy"]["commission_bps"][chain] == 100
    if chain == "bsc":
        assert fee_bps == 200 and row["fee_policy"]["commission_in_broker_default_bps"][chain] == 100


def test_actual_curve_cashflows_use_derived_conservative_commission_bound(runner, monkeypatch):
    from kaiba.execution.curve_price import DriftMode, quote_buy, quote_sell

    fee_only_broker(monkeypatch)
    m = manifest()

    def captured(at):
        return quote(at, liquidity=None, curve={
            "virtual_sol": 31_000_000_000, "virtual_token": 1_000_000_000_000_000,
            "real_sol": 1_000_000_000, "real_token": 720_100_000_000_000,
            "observed_ms": at, "source": "synthetic-curve", "complete": False})

    entry_quote = captured(T0 + 31_000)
    snap, _ = runner._snapshot(entry_quote, T0 + 31_000)
    expected_buy = quote_buy(snap["curve"], 100_000_000, sol_usd=Decimal("100"), decimals=6,
                             fee_bps=228, creator_fee_bps=30, latency_ms=0, drift_mode=DriftMode.NONE)
    run(runner, m, [opportunity()], q=entry_quote)
    assert read_book(runner, m, "SELECT qty FROM positions") == [(str(expected_buy.amount_out),)]
    exit_quote = captured(T0 + 104_000)
    snap, _ = runner._snapshot(exit_quote, T0 + 104_000)
    expected_sell = quote_sell(snap["curve"], expected_buy.amount_out, sol_usd=Decimal("100"), decimals=6,
                              fee_bps=228, creator_fee_bps=30, latency_ms=0, drift_mode=DriftMode.NONE)
    row = run(runner, m, now=T0 + 104_000, q=exit_quote)["outcomes"][0]
    assert row["cost_native"] == expected_buy.amount_in and row["proceeds_native"] == expected_sell.amount_out
    assert [b["fee_bps"] for b in row["basis"]] == [228, 228]
    assert row["fees_native"] == expected_buy.fee_native + expected_sell.fee_native
    # A 1% input-budget deduction before the 125bps curve venue fee yields this bound.
    assert 228 * 9900 >= (125 + 100) * 10_000 > 227 * 9900
    assert row["fee_policy"]["curve_protocol_split_is_observed"] is False
    assert row["cost_complete"] and row["cost_model_assumed"] and not row["live_cost_verified"]


def test_actual_pumpswap_path_uses_its_constructor_rate_with_commission(runner, monkeypatch):
    fee_only_broker(monkeypatch)
    m = manifest()
    run(runner, m, now=T0 + 1000, q=False)
    with sqlite3.connect(runner.book_path("campaign", "arm-0")) as conn:
        conn.execute("INSERT INTO tokens(chain,address,decimals,first_seen_ms,migrated_ms) VALUES ('sol','token',6,?,?)",
                     (T0, T0 + 500))
    run(runner, m, [opportunity()], q=quote(T0 + 31_000, liquidity="1e40", venue_fee_bps=120))
    row = run(runner, m, now=T0 + 104_000,
              q=quote(T0 + 104_000, liquidity="1e40", venue_fee_bps=120))["outcomes"][0]
    assert [b["venue"] for b in row["basis"]] == ["pumpswap-tier1", "pumpswap-tier1"]
    assert [b["fee_bps"] for b in row["basis"]] == [220, 220]
    assert row["net_return"] == pytest.approx(.978 ** 2 - 1)


@pytest.mark.parametrize("missing", ["venue", "tax"])
def test_unknown_cost_component_keeps_profitable_paper_cashflows_incomplete(runner, missing):
    m = manifest()

    def incomplete(at, price):
        q = quote(at, price=price)
        if missing == "venue":
            q.pop("venue_fee_bps")
            q.pop("venue_fee_source")
        else:
            q.pop("tax_known_zero")
        return q

    opened = run(runner, m, [opportunity()], q=incomplete(T0 + 31_000, "1"))
    assert opened["arms"][0]["open"] == 1
    row = run(runner, m, now=T0 + 104_000, q=incomplete(T0 + 104_000, "1.5"))["outcomes"][0]
    assert row["net_return"] > 0 and not row["cost_complete"] and not row["evidence_complete"]
    assert row[missing + "_complete"] is False
    assert row["cost_model_assumed"] and not row["live_cost_verified"]


@pytest.mark.parametrize("venue_changes", [
    {"venue_fee_bps": 126}, {"venue_fee_bps": "NaN"}, {"venue_fee_bps": True},
    {"venue_fee_source": None}, {"venue_fee_ts_ms": T0 + 110_000}, {"venue_fee_ts_ms": T0 - 60_000}])
def test_unproved_or_above_allowance_venue_fee_never_certifies_cost_coverage(runner, venue_changes):
    m = manifest()
    run(runner, m, [opportunity()], q=quote(T0 + 31_000, **venue_changes))
    row = run(runner, m, now=T0 + 104_000, q=quote(T0 + 104_000, **venue_changes))["outcomes"][0]
    assert not row["venue_complete"] and not row["cost_complete"]


def test_changed_commission_policy_refuses_populated_book_without_relabeling_ledger(runner, monkeypatch):
    from kaiba.learning import candidate_paper as cp

    m = manifest()
    run(runner, m, [opportunity()])
    identity = read_book(runner, m, "SELECT value FROM candidate_meta WHERE key='identity'")
    monkeypatch.setattr(cp, "ASSUMED_GMGN_COMMISSION_BPS", {**cp.ASSUMED_GMGN_COMMISSION_BPS, Chain.SOL: 101})
    changed = run(runner, m, now=T0 + 104_000)
    assert "frozen candidate identity" in changed["errors"][0]["error"]
    assert read_book(runner, m, "SELECT value FROM candidate_meta WHERE key='identity'") == identity
    assert read_book(runner, m, "SELECT COUNT(*) FROM orders") == [(1,)]
    assert read_book(runner, m, "SELECT closed_ms FROM positions") == [(None,)]


def test_pool_commission_removed_mutation_is_caught_by_actual_cashflows(runner, monkeypatch):
    from kaiba.learning import candidate_paper as cp

    monkeypatch.setattr(cp, "_pool_fee_bps", lambda chain: cp.DEFAULT_FEE_BPS[chain])
    with pytest.raises(AssertionError):
        test_assumed_commission_is_charged_once_in_actual_pool_cashflows(runner, monkeypatch, "sol", 225)


def test_curve_commission_removed_mutation_is_caught_by_actual_curve_quantity(runner, monkeypatch):
    from kaiba.learning import candidate_paper as cp

    monkeypatch.setattr(cp, "_curve_fee_bps", lambda: cp.CURVE_TOTAL_FEE_BPS)
    with pytest.raises(AssertionError):
        test_actual_curve_cashflows_use_derived_conservative_commission_bound(runner, monkeypatch)


def test_price_trigger_requires_later_quote_and_partial_rungs_are_frozen(runner):
    m = manifest(hold_ms=None)
    run(runner, m, [opportunity()])
    triggered = run(runner, m, now=T0 + 40_000, q=quote(T0 + 40_000, price="2.5"))
    assert triggered["arms"][0]["order_count"] == 1
    early = run(runner, m, now=T0 + 45_000, q=quote(T0 + 45_000, price="2.5"))
    assert early["arms"][0]["order_count"] == 1
    trimmed = run(runner, m, now=T0 + 100_000, q=quote(T0 + 100_000, price="2.5"))
    assert trimmed["arms"][0]["order_count"] == 2
    qty, total = map(int, read_book(runner, m, "SELECT qty,qty_total FROM positions")[0])
    assert qty == total - total // 2
    run(runner, m, now=T0 + 110_000, q=quote(T0 + 110_000, price="6"))
    run(runner, m, now=T0 + 130_000, q=quote(T0 + 130_000, price="6"))
    qty = int(read_book(runner, m, "SELECT qty FROM positions")[0][0])
    assert abs(qty - total / 4) <= 1  # TP2 sells 25% ORIGINAL, not 25% remaining


def test_same_capacity_for_every_arm_and_new_policy_is_new_inventory(runner):
    runner.max_open_per_arm = 1
    m = manifest(arms=2)
    opps = [opportunity("one", decision_id="a"), opportunity("two", decision_id="b")]
    quotes = {("sol", t): quote(T0 + 31_000) for t in ("one", "two")}
    result = runner.run_candidates(m, opps, quotes, now_ms=T0 + 31_000)
    assert [a["open"] for a in result["arms"]] == [1, 1]
    assert [a["entries"] for a in result["arms"]] == [{"opened": 1, "rejected": 1}] * 2
    assert len({a["book_path"] for a in result["arms"]}) == 2
    assert runner.book_path("campaign", "arm-0") != runner.book_path("other-campaign", "arm-0")


def test_main_connection_cannot_be_reused(tmp_db, tmp_path):
    runner = CandidatePaperRunner(tmp_path / "learning" / "paper")
    run(runner, manifest(), [opportunity()])
    assert tmp_db.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0
    assert tmp_db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_rejects_unsafe_paths_and_nonpaper_status(tmp_path, runner):
    with pytest.raises(ValueError, match="owned learning root"):
        CandidatePaperRunner(tmp_path / "unowned")
    with pytest.raises(ValueError, match="canonical database"):
        CandidatePaperRunner(tmp_path / "learning" / "kaiba.db")
    with pytest.raises(ValueError, match="at most"):
        CandidatePaperRunner(tmp_path / "learning", max_stake_usd="121")
    m = manifest()
    m["hypotheses"][0]["status"] = "LIVE"
    result = run(runner, m, [opportunity()])
    assert result["arms"] == [] and result["errors"][0]["error"] == "candidate must be PAPER_ONLY"


def test_unknown_evm_taxes_are_incomplete_even_with_profitable_model_fill(runner):
    m = manifest(chain="bsc")
    opp = opportunity(chain="bsc")
    opp.pop("tax_known_zero")
    q1 = quote(T0 + 31_000, chain="bsc")
    q1.pop("tax_known_zero")
    run(runner, m, [opp], q=q1)
    q2 = quote(T0 + 104_000, chain="bsc", price="2")
    q2.pop("tax_known_zero")
    result = run(runner, m, now=T0 + 104_000, q=q2)
    row = result["outcomes"][0]
    assert row["net_return"] > 0 and not row["cost_complete"] and not row["evidence_complete"]


def test_actual_sol_curve_reserves_fill_without_inventing_pool_depth(runner):
    m = manifest()
    snapshot = quote(T0 + 31_000, liquidity=None, curve={
        "virtual_sol": 31_000_000_000, "virtual_token": 1_000_000_000_000_000,
        "real_sol": 1_000_000_000, "real_token": 720_100_000_000_000,
        "observed_ms": T0 + 31_000, "source": "fixture-chain-curve", "complete": False,
    })
    result = run(runner, m, [opportunity()], q=snapshot)
    assert result["arms"][0]["open"] == 1 and not result["errors"]
    assert read_book(runner, m, "SELECT filled_out FROM orders")[0][0] is not None


def test_missing_owned_buy_cannot_sell_unmatched_inventory(runner):
    m = manifest()
    run(runner, m, [opportunity()])
    with sqlite3.connect(runner.book_path("campaign", "arm-0")) as conn:
        conn.execute("DELETE FROM position_orders")
    result = run(runner, m, now=T0 + 104_000)
    assert not result["outcomes"] and result["arms"][0]["order_count"] == 1
    assert result["arms"][0]["incomplete"][0]["reason"] == "unmatched_inventory"


def test_watch_pending_and_open_without_creating_unused_books(runner):
    m = manifest(arms=2)
    assert runner.watched_tokens(m) == []
    assert not runner.state_dir.exists()
    run(runner, m, [opportunity()], now=T0 + 1000)
    assert runner.watched_tokens(m) == [("sol", "token")]
    run(runner, m)
    assert runner.watched_tokens(m) == [("sol", "token")]
    run(runner, m, now=T0 + 104_000)
    assert runner.watched_tokens(m) == []
    with pytest.raises(ValueError):
        runner.watched_tokens(m, limit=129)


def test_assessment_keeps_open_and_unfillable_denominators(runner):
    m = manifest()
    result = run(runner, m, [opportunity()])
    opened = result["assessment_rows"][0]
    assert opened["status"] == "open" and not opened["no_position_created"]
    assert opened["net_return"] is None and not opened["inventory_complete"]
    assert not opened["cost_complete"] and not opened["evidence_complete"]
    missing = run(runner, m, now=T0 + 104_000, q=False)
    assert missing["assessment_rows"][0]["status"] == "open"
    assert missing["outcomes"] == []
    m["campaign_id"] = "unfillable"
    failed = run(runner, m, [opportunity()], q=quote(T0 + 31_000, liquidity="0.01"))
    refusal = failed["refusals"][0]
    assert refusal["no_position_created"] and refusal["status"] == "failed"
    assert refusal["net_return"] is None and refusal["inventory_remaining"] == 0


def test_changed_decimals_and_wrong_token_quote_are_incomplete(runner):
    m = manifest()
    run(runner, m, [opportunity()])
    mismatch = run(runner, m, now=T0 + 104_000, q=quote(T0 + 104_000, decimals=18))
    assert not mismatch["outcomes"]
    assert mismatch["assessment_rows"][0]["reason"] == "token_decimals_changed"
    wrong = run(runner, m, now=T0 + 110_000, q=quote(T0 + 110_000, token="another"))
    assert not wrong["outcomes"]
    assert wrong["arms"][0]["order_count"] == 1


def test_backward_ticks_are_rejected_without_rewriting_book(runner):
    m = manifest()
    run(runner, m, [opportunity()])
    result = run(runner, m, now=T0 + 30_000)
    assert "must not run backward" in result["errors"][0]["error"]
    assert read_book(runner, m, "SELECT value FROM candidate_meta WHERE key='last_tick_ms'") == [(str(T0 + 31_000),)]


def test_broker_source_upgrade_requires_a_new_frozen_book(runner, tmp_path, monkeypatch):
    import kaiba.execution.paper as paper

    m = manifest()
    run(runner, m, [opportunity()])
    changed_source = tmp_path / "changed-paper-model.py"
    changed_source.write_text("# a different broker model", encoding="utf-8")
    monkeypatch.setattr(paper, "__file__", str(changed_source))
    changed = run(runner, m, now=T0 + 104_000)
    assert "frozen candidate identity" in changed["errors"][0]["error"]
    assert read_book(runner, m, "SELECT COUNT(*) FROM orders") == [(1,)]


def test_quote_watch_cap_rotates_between_distinct_arm_universes(runner):
    m = manifest(arms=2)
    m["hypotheses"][0]["spec"]["filter"] = [{"f": "arm", "eq": "first"}]
    m["hypotheses"][1]["spec"]["filter"] = [{"f": "arm", "eq": "second"}]
    opportunities = []
    for i in range(5):
        raw = opportunity(f"first-{i}", decision_id=f"a-{i}")
        raw["features"] = {"arm": "first"}
        opportunities.append(raw)
    raw = opportunity("second", decision_id="last")
    raw["features"] = {"arm": "second"}
    opportunities.append(raw)
    run(runner, m, opportunities, now=T0 + 1000, q=False)
    assert runner.watched_tokens(m, limit=2) == [("sol", "first-0"), ("sol", "second")]


@pytest.mark.parametrize("incumbent_size", [1_000_000, 100_000_000, 1_000_000_000])
def test_pending_usd_policy_converts_only_at_actual_fill_quote(runner, incumbent_size):
    m = manifest(arms=2)
    raw = opportunity()
    raw["size_base_units"] = incumbent_size
    raw["paper_stake_usd"] = "30"
    pending = run(runner, m, [raw], now=T0 + 1000, q=False)
    assert all(a["order_count"] == 0 for a in pending["arms"])
    assert all(r["status"] == "pending" for r in pending["assessment_rows"])
    quote_now = quote(T0 + 61_000, native_usd="200")
    filled = run(runner, m, now=T0 + 61_000, q=quote_now)
    assert all(a["open"] == 1 for a in filled["arms"])
    assert {r["entry_debit_native"] for r in filled["assessment_rows"]} == {150_000_000}
    assert Decimal(filled["assessment_rows"][0]["entry_debit_native"]) / 10**9 * 200 == 30


@pytest.mark.parametrize("stake", ["0", "121", "NaN", "-1"])
def test_usd_policy_cannot_override_paper_stake_envelope(runner, stake):
    raw = opportunity()
    raw["paper_stake_usd"] = stake
    result = run(runner, manifest(), [raw])
    assert result["arms"][0]["order_count"] == 0
    assert result["refusals"][0]["reason"] == "invalid_flat_paper_stake"


def test_closed_features_are_frozen_for_trained_filter_validation(runner):
    m = manifest()
    m["hypotheses"][0]["kind"] = "trained"
    m["hypotheses"][0]["spec"]["filter"] = [{"f": "count", "lo": 2, "hi": 4}]
    raw = opportunity()
    raw["feature_ts_ms"] = raw["ts_ms"]
    run(runner, m, [raw])
    future_duplicate = {**raw, "features": {"count": 99}}
    result = run(runner, m, [future_duplicate], now=T0 + 104_000)
    closed = result["outcomes"][0]
    assert closed["features"] == {"count": 3}
    assert closed["feature_ts_ms"] == raw["ts_ms"]


def test_filter_exclusion_is_separate_from_assessment_and_refusals(runner):
    m = manifest()
    m["hypotheses"][0]["spec"]["filter"] = [{"f": "count", "lo": 4}]
    result = run(runner, m, [opportunity()])
    assert result["assessment_rows"] == [] and result["refusals"] == []
    assert result["excluded_by_filter"][0]["status"] == "excludedbyfilter"
    assert result["arms"][0]["order_count"] == 0


def test_only_ledger_proved_prebroker_refusal_is_known_unfilled(runner):
    m = manifest()
    runner.max_open_per_arm = 1
    raws = [opportunity("one", decision_id="a"), opportunity("two", decision_id="b")]
    result = runner.run_candidates(m, raws, {("sol", t): quote(T0 + 31_000) for t in ("one", "two")}, now_ms=T0 + 31_000)
    refused = result["refusals"][0]
    assert refused["status"] == "refused" and refused["known_unfilled"]
    assert refused["source"] == "PaperBroker" and refused["provider"] == "paper"
    assert all(refused[k] for k in ("evidence_complete", "cost_complete", "inventory_complete", "no_position_created"))
    assert refused["entry_debit_native"] == refused["exit_credit_native"] == 0
    assert refused["exposure_check"] == {"attempt_orders": 0, "attempt_positions": 0}
    m["campaign_id"] = "failed-order"
    failed = run(runner, m, [opportunity()], q=quote(T0 + 31_000, liquidity="0.01"))
    failed_row = failed["refusals"][0]
    assert not failed_row["known_unfilled"] and not failed_row["evidence_complete"]
    assert not failed_row["cost_complete"] and not failed_row["inventory_complete"]
    assert failed_row["exposure_check"]["attempt_orders"] == 1


def test_new_campaign_does_not_count_preselection_backlog_as_uncertain_trades(runner):
    m = manifest()
    m["selected_ms"] = T0 + 5000
    result = run(runner, m, [opportunity()])
    assert result["assessment_rows"] == [] and result["refusals"] == []
    assert result["excluded_by_scope"][0]["status"] == "excludedbyscope"
    assert result["excluded_by_scope"][0]["reason"] == "outside_prospective_window"
    assert result["arms"][0]["order_count"] == 0


def test_expired_unsent_entries_settle_before_source_guard_can_defer(runner):
    from kaiba.learning.autolearn_io import SourceDeferred

    m = manifest()
    run(runner, m, [opportunity()], now=T0 + 1000, q=False)
    original = read_book(runner, m, "SELECT payload FROM candidate_entries")[0][0]
    identity = read_book(runner, m, "SELECT value FROM candidate_meta WHERE key='identity'")[0][0]
    # The old clockless API remains compatible, but its stale watch requires source
    # work unless a research-only expiry is admitted first.
    assert runner.watched_tokens(m) == [("sol", "token")]
    late = T0 + 4 * 3600_000
    preflight = runner.expire_pending(m, now_ms=late)
    assert preflight["expired"] == 1 and not preflight["errors"]

    def source_guard():
        raise SourceDeferred("wal_over_limit:offline-fixture")

    with pytest.raises(SourceDeferred, match="wal_over_limit"):
        source_guard()
    assert runner.watched_tokens(m, now_ms=late) == []
    row = read_book(runner, m, "SELECT status,reason,expired_ms,payload FROM candidate_entries")[0]
    assert row == ("rejected", "latency_unreachable", late, original)
    assert read_book(runner, m, "SELECT value FROM candidate_meta WHERE key='identity'")[0][0] == identity
    assert read_book(runner, m, "SELECT COUNT(*) FROM orders") == [(0,)]
    assert read_book(runner, m, "SELECT COUNT(*) FROM positions") == [(0,)]
    assert read_book(runner, m, "SELECT COUNT(*) FROM trades") == [(0,)]


def test_now_aware_watch_does_not_create_empty_books_and_retains_actual_inventory(runner):
    m = manifest()
    assert runner.expire_pending(m, now_ms=T0)["expired"] == 0
    assert runner.watched_tokens(m, now_ms=T0) == []
    assert not runner.state_dir.exists()
    run(runner, m, [opportunity()])
    late = T0 + 4 * 3600_000
    assert runner.expire_pending(m, now_ms=late)["expired"] == 0
    assert runner.watched_tokens(m, now_ms=late) == [("sol", "token")]
    assert read_book(runner, m, "SELECT closed_ms FROM positions") == [(None,)]
    assert read_book(runner, m, "SELECT COUNT(*) FROM orders") == [(1,)]


def test_expiry_is_idempotent_and_cannot_book_old_quote_or_release_future_signal(runner):
    m = manifest()
    run(runner, m, [opportunity()], now=T0 + 1000, q=False)
    deadline = T0 + 1000 + runner.entry_max_age_ms
    assert runner.expire_pending(m, now_ms=deadline)["expired"] == 0
    assert runner.expire_pending(m, now_ms=deadline + 1)["expired"] == 1
    assert runner.expire_pending(m, now_ms=deadline + 30_000)["expired"] == 0
    assert read_book(runner, m, "SELECT expired_ms FROM candidate_entries") == [(deadline + 1,)]
    late_quote = quote(deadline + 31_000, price="100")
    result = run(runner, m, now=deadline + 31_000, q=late_quote)
    assert result["arms"][0]["order_count"] == 0 and result["outcomes"] == []
    refused = result["refusals"][0]
    assert refused["known_unfilled"] and refused["net_return"] is None


def test_pending_with_an_order_cannot_expire_into_proved_zero_exposure(runner):
    m = manifest()
    run(runner, m, [opportunity()], now=T0 + 1000, q=False)
    with sqlite3.connect(runner.book_path("campaign", "arm-0")) as conn:
        conn.execute("UPDATE candidate_entries SET decision_id='attempt-with-order'")
        conn.execute(
            "INSERT INTO orders(order_id,decision_id,chain,token,side,lane,mode,input_token,output_token,amount_in,"
            "min_out,slippage_bps,state,provider,created_ms,updated_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("uncertain-order", "attempt-with-order", "sol", "token", "buy", "sm-trenches", "shadow", "SOL", "token",
             "100", "1", 1500, "unknown", "paper", T0 + 1000, T0 + 1000),
        )
    late = T0 + 4 * 3600_000
    result = runner.expire_pending(m, now_ms=late)
    assert result["expired"] == 0 and result["blocked_by_exposure"] == 1
    row = read_book(runner, m, "SELECT status,reason,expired_ms FROM candidate_entries")[0]
    assert row == ("pending", "entry_expiry_has_ledger_exposure", None)
    checked = run(runner, m, now=late, q=False)
    assert checked["assessment_rows"][0]["evidence_complete"] is False
    assert checked["assessment_rows"][0]["known_unfilled"] is False
    assert checked["arms"][0]["order_count"] == 1


def test_pre_expiry_upgrade_adds_columns_without_rewriting_existing_evidence(runner):
    m = manifest()
    run(runner, m, [opportunity()], now=T0 + 1000, q=False)
    payload = read_book(runner, m, "SELECT payload FROM candidate_entries")[0][0]
    identity = read_book(runner, m, "SELECT value FROM candidate_meta WHERE key='identity'")[0][0]
    additions = ("expired_ms", "registered_ms", "last_checked_ms", "last_quote_reason",
                 "last_quote_diagnostics_json", "expiry_diagnostics_json", "source_observed_ms",
                 "candidate_received_ms", "last_admission_ms", "last_admission_stage",
                 "preparation_started_ms", "last_usable_quote_json")
    with sqlite3.connect(runner.book_path("campaign", "arm-0")) as conn:
        for column in additions:
            conn.execute(f"ALTER TABLE candidate_entries DROP COLUMN {column}")
    late = T0 + 4 * 3600_000
    result = runner.expire_pending(m, now_ms=late)
    assert result["expired"] == 1 and not result["errors"]
    row = read_book(runner, m, "SELECT payload,registered_ms,last_quote_reason,expiry_diagnostics_json FROM candidate_entries")[0]
    assert row[:3] == (payload, None, None)
    diagnostic = json.loads(row[3])
    assert diagnostic["cause"] == "arrival_or_processing_cause_unknown"
    assert diagnostic["first_arrival_age_ms"] is None
    assert diagnostic["source_observed_ms"] is diagnostic["candidate_received_ms"] is None
    assert read_book(runner, m, "SELECT value FROM candidate_meta WHERE key='identity'")[0][0] == identity


def test_expiry_preserves_source_missing_quote_cause_and_immutable_arrival_age(runner):
    m = manifest()
    raw = {k: v for k, v in opportunity().items()
           if k not in ("quote_ts_ms", "price_usd", "liquidity_usd", "native_usd", "decimals", "source")}
    raw["quote_missing_reason"] = "native_price_missing"
    raw["quote_diagnostics"] = {"native_price": "no_asof_sample"}
    first_seen = T0 + 50_000
    raw["source_observed_ms"] = first_seen
    first = run(runner, m, [raw], now=first_seen, q=False)
    assert first["assessment_rows"][0]["last_quote_reason"] == "native_price_missing"
    later = T0 + 90_000
    run(runner, m, now=later, q={"unavailable_reason": "pool_depth_missing", "diagnostics": {"depth": "missing"}})
    expired = run(runner, m, now=T0 + 180_000, q=False)
    row = expired["refusals"][0]
    diagnostics = row["expiry_diagnostics"]
    assert row["registered_ms"] == first_seen and row["last_checked_ms"] == later
    assert diagnostics["first_arrival_age_ms"] == first_seen - raw["ts_ms"]
    assert diagnostics["cause"] == "entry_quote_not_usable_before_deadline"
    assert diagnostics["last_quote_reason"] == "pool_depth_missing"
    assert diagnostics["last_quote_diagnostics"]["capture"] == {"depth": "missing"}
    assert diagnostics["registration_quote_diagnostics"] == raw["quote_diagnostics"]


def test_arrived_old_is_distinguished_from_missing_quote_and_no_cause_is_backfilled(runner):
    m = manifest()
    raw = {**opportunity(), "source_observed_ms": T0 + 180_000}
    result = run(runner, m, [raw], now=T0 + 180_000, q=False)
    diagnostic = result["refusals"][0]["expiry_diagnostics"]
    assert diagnostic["cause"] == "source_read_after_entry_deadline"
    assert diagnostic["last_checked_ms"] is None and diagnostic["last_quote_reason"] is None
    assert diagnostic["first_arrival_age_ms"] == 179_000


@pytest.mark.parametrize("source_ms,received_ms,cause", [
    (150_000, 151_000, "source_read_after_entry_deadline"),
    (31_000, 151_000, "predispatch_processing_crossed_deadline"),
    (31_000, 31_000, "book_processing_crossed_deadline"),
])
def test_identical_expiry_times_distinguish_proved_source_dispatch_and_book_delays(runner, source_ms, received_ms, cause):
    runner.clock = lambda: (T0 + 171_000) / 1000
    raw = {**opportunity(), "source_observed_ms": T0 + source_ms}
    row = run(runner, manifest(), [raw], now=T0 + received_ms, q=False)["refusals"][0]
    diagnostics = row["expiry_diagnostics"]
    assert diagnostics["cause"] == cause and diagnostics["expired_ms"] == T0 + 171_000
    assert row["source_observed_ms"] == T0 + source_ms
    assert row["candidate_received_ms"] == T0 + received_ms and row["registered_ms"] == T0 + 171_000
    assert diagnostics["source_observation_age_ms"] == source_ms - 1000
    assert diagnostics["candidate_receipt_age_ms"] == received_ms - 1000
    assert row["known_unfilled"] and row["exposure_check"] == {"attempt_orders": 0, "attempt_positions": 0}


def test_first_source_and_candidate_receipts_survive_duplicates_and_are_exported_on_closed_outcomes(runner):
    raw = {**opportunity(), "source_observed_ms": T0 + 1000}
    m = manifest()
    run(runner, m, [raw], now=T0 + 1000, q=False)
    original = read_book(runner, m, "SELECT payload,source_observed_ms,candidate_received_ms,registered_ms FROM candidate_entries")
    duplicate = {**raw, "source_observed_ms": T0 + 31_000, "features": {"count": 99}}
    opened = run(runner, m, [duplicate])
    assert read_book(runner, m, "SELECT payload,source_observed_ms,candidate_received_ms,registered_ms FROM candidate_entries") == original
    assert opened["assessment_rows"][0]["last_admission_ms"] == T0 + 31_000
    closed = run(runner, m, now=T0 + 104_000)["outcomes"][0]
    assert closed["source_observed_ms"] == closed["candidate_received_ms"] == closed["registered_ms"] == T0 + 1000
    assert closed["last_admission_stage"] == "broker_filled"
    assert closed["last_usable_quote"]["quote_ts_ms"] == closed["last_usable_quote"]["price_ts_ms"] == T0 + 31_000
    assert closed["features"] == {"count": 3}


@pytest.mark.parametrize("source_ms", [None, True, "NaN", T0, T0 + 200_000, T0 + 31_000.5])
def test_missing_or_invalid_source_clock_is_unknown_and_does_not_change_admission(runner, source_ms):
    m = manifest()
    raw = {**opportunity(), "source_observed_ms": source_ms}
    opened = run(runner, m, [raw])
    assert opened["arms"][0]["order_count"] == 1
    assert opened["assessment_rows"][0]["source_observed_ms"] is None
    assert opened["assessment_rows"][0]["candidate_received_ms"] == T0 + 31_000


def test_old_candidate_receipt_without_source_clock_does_not_assert_late_source_arrival(runner):
    row = run(runner, manifest(), [opportunity()], now=T0 + 180_000, q=False)["refusals"][0]
    diagnostics = row["expiry_diagnostics"]
    assert diagnostics["cause"] == "arrival_or_processing_cause_unknown"
    assert diagnostics["first_arrival_age_ms"] is None and diagnostics["source_observed_ms"] is None
    assert diagnostics["candidate_receipt_age_ms"] == diagnostics["registration_age_ms"] == 179_000


def test_quote_check_io_cannot_claim_a_valid_quote_at_a_stale_logical_clock(runner, monkeypatch):
    clock, m = {"ms": T0 + 31_000}, manifest()
    runner.clock = lambda: clock["ms"] / 1000
    original, stalled = runner._open, []

    def traced(identity):
        conn = original(identity)

        def wait_for_diagnostic_write(sql):
            if "SET last_checked_ms=" in sql and not stalled:
                stalled.append(True)
                clock["ms"] += 100_000

        conn.set_trace_callback(wait_for_diagnostic_write)
        return conn

    monkeypatch.setattr(runner, "_open", traced)
    result = run(runner, m, [opportunity()])
    diagnostics = result["refusals"][0]["expiry_diagnostics"]
    assert diagnostics["cause"] == "quote_check_processing_crossed_deadline"
    assert diagnostics["last_usable_quote"] is None and diagnostics["last_admission_ms"] is None
    assert result["arms"][0]["order_count"] == 0


def test_preparation_quote_failure_before_deadline_is_not_later_mislabelled_as_processing_overrun(runner, monkeypatch):
    test_slow_entry_preparation_rechecks_clock_before_broker_and_rolls_back_unsubmitted_rows(
        runner, monkeypatch, 70_000, "quote_stale_or_future")
    row = run(runner, manifest(), now=T0 + 180_000, q=False)["refusals"][0]
    diagnostics = row["expiry_diagnostics"]
    assert diagnostics["cause"] == "entry_quote_not_usable_before_deadline"
    assert diagnostics["last_admission_stage"] == "preparation_quote_unusable"
    assert diagnostics["last_usable_quote"]["price_ts_ms"] == T0 + 31_000
    assert diagnostics["last_quote_diagnostics"]["stage"] == "entry_preparation"


def test_removed_receipt_clocks_mutation_is_caught_by_phase_cause_regression(runner, monkeypatch):
    from kaiba.learning import candidate_paper as cp

    monkeypatch.setattr(cp, "_captured_clock", lambda *a, **k: None)
    with pytest.raises(AssertionError):
        test_identical_expiry_times_distinguish_proved_source_dispatch_and_book_delays(
            runner, 31_000, 31_000, "book_processing_crossed_deadline")


def test_old_arrival_cause_relabel_mutation_is_caught(runner, monkeypatch):
    original = runner._expire_pending_rows

    def mislabeled(conn, identity, now):
        counts = original(conn, identity, now)
        for sid, raw in conn.execute("SELECT signal_id,expiry_diagnostics_json FROM candidate_entries WHERE expired_ms IS NOT NULL").fetchall():
            diagnostics = {**json.loads(raw), "cause": "arrived_after_entry_deadline"}
            conn.execute("UPDATE candidate_entries SET expiry_diagnostics_json=? WHERE signal_id=?", (json.dumps(diagnostics), sid))
        return counts

    monkeypatch.setattr(runner, "_expire_pending_rows", mislabeled)
    with pytest.raises(AssertionError):
        test_identical_expiry_times_distinguish_proved_source_dispatch_and_book_delays(
            runner, 31_000, 31_000, "book_processing_crossed_deadline")


def test_expiry_disabled_mutation_is_caught_by_the_source_deferral_regression(runner, monkeypatch):
    monkeypatch.setattr(CandidatePaperRunner, "_expire_pending_rows",
                        lambda *a, **k: {"expired": 0, "blocked_by_exposure": 0})
    with pytest.raises(AssertionError):
        test_expired_unsent_entries_settle_before_source_guard_can_defer(runner)


def test_expiry_exposure_proof_removed_mutation_is_caught(runner, monkeypatch):
    monkeypatch.setattr(CandidatePaperRunner, "_attempt_exposure",
                        staticmethod(lambda *a, **k: {"attempt_orders": 0, "attempt_positions": 0}))
    with pytest.raises(AssertionError):
        test_pending_with_an_order_cannot_expire_into_proved_zero_exposure(runner)


def test_expiry_refuses_backward_clock_without_erasing_pending_evidence(runner):
    m = manifest()
    run(runner, m, [opportunity()], now=T0 + 1000, q=False)
    before = read_book(runner, m, "SELECT payload,status,reason,expired_ms FROM candidate_entries")
    result = runner.expire_pending(m, now_ms=T0)
    assert result["expired"] == 0 and "backward in time" in result["errors"][0]["error"]
    assert read_book(runner, m, "SELECT payload,status,reason,expired_ms FROM candidate_entries") == before
