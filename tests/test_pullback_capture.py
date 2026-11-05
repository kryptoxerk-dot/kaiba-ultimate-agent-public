"""Offline tests for the prospective pullback capture (PULLBACK-CAPTURE-CLAUDE-20261102).

No provider is called: the GMGN reader, the cache, the clock, the disk and the load average are
injected. The source database is a small SQLite file read through the production GuardedReader.
"""
from __future__ import annotations

import json
import sqlite3
from collections import namedtuple
from decimal import Decimal

import pytest

from kaiba.core.schemas import EvidenceBasis, Receipt
from kaiba.learning import pullback_capture as pc
from kaiba.learning import pullback_study
from kaiba.providers.gmgn_cli import GmgnResult

DAY, MIN = pc.DAY_MS, pc.MINUTE_MS
T0 = 1_800_000_000_000 - (1_800_000_000_000 % DAY) + 10 * MIN  # 00:10 UTC on some day
Disk = namedtuple("Disk", "total used free")
GIB = 1024**3


def body(price, *, circ="1000000000", total="1000000000", liquidity="20000", **extra):
    data = {"price": {"price": str(price), "price_1m": "0.0000001", "price_24h": "0.0000001"},
            "circulating_supply": circ, "total_supply": total, "max_supply": total, "decimals": 6,
            "liquidity": liquidity, "launchpad": "pump", "launchpad_status": 1}
    data.update(extra)
    return data


def source_db(path, *, now, decisions=(), unresolved=0, credit=60_000, banned=0, family_ban=0,
              penalty=0, halt=None):
    conn = sqlite3.connect(path)
    conn.executescript("""
    CREATE TABLE decisions(decision_id TEXT, ts_ms INTEGER, chain TEXT, token TEXT, features_json TEXT);
    CREATE TABLE orders(order_id TEXT, state TEXT);
    CREATE TABLE risk_state(day_key TEXT, halted INTEGER, halt_reason TEXT);
    CREATE TABLE provider_state(provider TEXT, credit_milli INTEGER, last_refill_ms INTEGER,
      banned_until_ms INTEGER, penalty_level INTEGER);
    CREATE TABLE provider_family_bans(provider TEXT, family TEXT, banned_until_ms INTEGER);
    CREATE TABLE native_prices(chain TEXT, ts_ms INTEGER, price_usd TEXT);
    """)
    conn.execute("INSERT INTO decisions VALUES ('seed', 0, 'sol', 'seed', '{}')")  # cursor starts after it
    for d in decisions:
        conn.execute("INSERT INTO decisions VALUES (?,?,?,?,?)", d)
    for i in range(unresolved):
        conn.execute("INSERT INTO orders VALUES (?, 'submitted')", (f"o{i}",))
    conn.execute("INSERT INTO orders VALUES ('done', 'filled')")
    if halt:
        conn.execute("INSERT INTO risk_state VALUES (?, 1, ?)", (pc.utc_day(now), halt))
    conn.execute("INSERT INTO provider_state VALUES ('gmgn', ?, ?, ?, ?)", (credit, now, banned, penalty))
    if family_ban:
        conn.execute("INSERT INTO provider_family_bans VALUES ('gmgn', 'token', ?)", (family_ban,))
    conn.execute("INSERT INTO native_prices VALUES ('sol', ?, '100')", (now - 30_000,))
    conn.commit()
    conn.close()


def decision(ts, token, cap=200_000, age=5, chain="sol"):
    return (f"d-{token}-{ts}", ts, chain, token, json.dumps({"market_cap_usd": cap, "dossier_age_s": age}))


class FakeProvider:
    def __init__(self, clock, price=Decimal("0.0002"), fail=None):
        self.clock, self.price, self.fail, self.calls = clock, price, fail, []

    def token_info(self, token, chain, **kw):
        self.calls.append((token, kw))
        assert kw["priority"].name == "RESEARCH" and kw["stale_grace_s"] == 0 and kw["wait_for_slot_s"] == 0
        if self.fail:
            return GmgnResult(None, Receipt(provider="gmgn", endpoint="token.info",
                                            basis=EvidenceBasis.UNAVAILABLE, note=self.fail))
        return GmgnResult(body(self.price), Receipt(provider="gmgn", endpoint="token.info",
                                                    observed_at_ms=self.clock(),
                                                    basis=EvidenceBasis.PROVIDER_REPORTED))


@pytest.fixture
def env(tmp_path):
    class Env:
        now = T0
        store = tmp_path / "pullback-capture" / "capture.db"
        db = tmp_path / "kaiba.db"
        used_pct = 50.0
        free = 40 * GIB
        load = 0.5
        cache = {}

        def capture(self, provider=None, **kw):
            def disk_usage(path):  # df's Use% is used / (used + free)
                used = int(self.free * self.used_pct / (100 - self.used_pct))
                return Disk(used + self.free, used, self.free)
            return pc.Capture(self.store, self.db, provider=provider or FakeProvider(lambda: self.now),
                              clock=lambda: self.now, disk_usage=disk_usage,
                              loadavg=lambda: (self.load, self.load, self.load), cpu_count=2,
                              cache_reader=lambda token: self.cache.get(token), **kw)
    return Env()


def ledger(store):
    conn = sqlite3.connect(store)
    try:
        return {(d, k): n for d, k, n in conn.execute("SELECT day, key, n FROM ledger")}
    finally:
        conn.close()


def rows(store, sql, args=()):
    conn = sqlite3.connect(store)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------- field semantics


def test_circulating_supply_times_price_is_market_cap_and_vendor_lookbacks_are_ignored():
    p = pc.parse_token_info(body("0.0002"))
    assert (p["cap_detail"], p["cap_basis"], p["market_cap_usd"]) == ("derived_circulating", "market_cap", Decimal("200000.0000"))
    assert p["price_usd"] == Decimal("0.0002")  # price.price, never price_1m / price_24h


def test_total_supply_alone_is_fdv_and_never_market_cap():
    p = pc.parse_token_info(body("0.0002", circ=None))
    assert (p["cap_detail"], p["cap_basis"]) == ("derived_total", "fdv")


@pytest.mark.parametrize("kw, detail", [
    ({"circ": "2000000000", "total": "1000000000"}, "supply_inconsistent"),
    ({"market_cap": "999999"}, "reported_unverified"),  # vendor cap the response cannot reproduce
    ({"circ": None, "total": None}, "missing"),
])
def test_ambiguous_or_missing_cap_evidence_stays_unknown(kw, detail):
    p = pc.parse_token_info(body("0.0002", **kw))
    assert (p["cap_detail"], p["cap_basis"], p["market_cap_usd"]) == (detail, "unknown", None)


def test_a_reported_cap_counts_only_when_the_same_response_reproduces_it():
    p = pc.parse_token_info(body("0.0002", market_cap="200100"))
    assert (p["cap_detail"], p["cap_basis"]) == ("reported_matches_circulating", "market_cap")


@pytest.mark.parametrize("data", [None, {"price": "0.0002"}, body("0"), body("-1"), body("nan"), body(True)])
def test_no_positive_price_is_no_observation(data):
    assert pc.parse_token_info(data) is None


# ---------------------------------------------------------------- collector


def test_admission_is_deterministic_and_every_exclusion_is_censused(env):
    ts = env.now - 30_000
    ds = [decision(ts, f"t{i}") for i in range(9, -1, -1)]  # 10 qualifying, inserted out of order
    ds += [decision(ts, "low", cap=50_000), decision(ts, "nocap", cap=None), decision(ts, "old", age=900),
           decision(ts, "evm", chain="bsc")]
    source_db(env.db, now=env.now, decisions=ds)
    report = env.capture().cycle()
    assert report["status"] == "ok"
    conn = sqlite3.connect(env.db)  # a decision that reaches the table after the processing lag
    conn.execute("INSERT INTO decisions VALUES (?,?,?,?,?)", decision(env.now - 10 * MIN, "late"))
    conn.commit()
    conn.close()
    env.capture().cycle()
    admitted = [r[0] for r in rows(env.store, "SELECT token FROM watch ORDER BY token")]
    assert admitted == [f"t{i}" for i in range(8)]  # max_tokens 8, (ts, token) order
    census = dict(rows(env.store, "SELECT token, outcome FROM census"))
    assert census["t8"] == census["t9"] == "list_full"
    assert (census["low"], census["nocap"], census["old"], census["late"]) == (
        "below_cap", "cap_missing", "stale_dossier", "processed_late")
    assert "evm" not in census


def test_a_fetched_observation_keeps_the_receipt_clock_and_a_cached_one_its_original_clock(env):
    source_db(env.db, now=env.now, decisions=[decision(env.now - 1000, "a"), decision(env.now - 1000, "b")])
    env.cache = {"a": (body("0.0002"), env.now - 10_000)}  # fresh (<15 s), fetched by someone else
    provider = FakeProvider(lambda: env.now + 700)
    env.capture(provider).cycle()
    got = dict(rows(env.store, "SELECT token, observed_ms || ':' || available_ms || ':' || origin FROM observations"))
    assert got == {"a": f"{env.now - 10_000}:{env.now - 10_000}:cache", "b": f"{env.now + 700}:{env.now + 700}:fetched"}
    assert [c[0] for c in provider.calls] == ["b"]  # the cache hit cost nothing


def test_a_stale_cache_file_is_not_served_and_a_fresh_one_keeps_its_clock(tmp_path, monkeypatch):
    from kaiba.core.schemas import now_ms
    from kaiba.providers import _http

    monkeypatch.setattr(_http, "_cache_root", lambda: tmp_path)
    key = "gmgn-cli token info --address tok --chain sol --raw"
    path = _http.cache_path("gmgn", key)
    path.parent.mkdir(parents=True)
    old = now_ms() - 40_000
    path.write_text(json.dumps({"fetched_ms": old, "data": body("0.0002")}))
    assert pc._fresh_cache("tok") is None  # inside run_read's 120 s grace, but not fresh: never used
    fresh = now_ms() - 5_000
    path.write_text(json.dumps({"fetched_ms": fresh, "data": body("0.0002")}))
    assert pc._fresh_cache("tok")[1] == fresh


def test_future_clock_is_rejected_at_capture(env):
    conn = pc.open_store(env.store)
    try:
        assert env.capture().record(conn, "a", body("0.0002"), env.now + 60_000, "cache", env.now) == "future_clock"
        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
    finally:
        conn.close()


class SlowProvider(FakeProvider):
    """The call takes ``delay`` ms of wall clock; its receipt is stamped when it returns (+ ``skew``)."""

    def __init__(self, env, delay, skew=0):
        super().__init__(lambda: env.now + skew)
        self.env, self.delay = env, delay

    def token_info(self, token, chain, **kw):
        self.env.now += self.delay
        return super().token_info(token, chain, **kw)


def test_a_receipt_that_returns_after_the_cycle_began_is_recorded_not_future(env):
    """Codex review 2026-11-03: _observe compared a receipt with the CYCLE-START clock, so an honest
    call that returned 6.001 s later was labelled future_clock and lost."""
    source_db(env.db, now=env.now, decisions=[decision(env.now - 1000, "a")])
    start = env.now
    env.capture(SlowProvider(env, delay=6_001)).cycle()
    got = rows(env.store, "SELECT observed_ms, recorded_ms, origin FROM observations")
    assert got == [(start + 6_001, start + 6_001, "fetched")]
    assert rows(env.store, "SELECT COUNT(*) FROM gaps WHERE reason='future_clock'") == [(0,)]


def test_a_truly_future_receipt_is_still_rejected(env):
    source_db(env.db, now=env.now, decisions=[decision(env.now - 1000, "a")])
    env.capture(SlowProvider(env, delay=0, skew=6_001)).cycle()
    assert rows(env.store, "SELECT COUNT(*) FROM observations") == [(0,)]
    assert rows(env.store, "SELECT COUNT(*) FROM gaps WHERE reason='future_clock'") == [(1,)]


def test_a_cache_file_written_after_the_cycle_began_keeps_its_clock_and_is_recorded(env):
    source_db(env.db, now=env.now, decisions=[decision(env.now - 1000, "a")])
    start = env.now

    class WrittenMidCycle(dict):  # another job fetched it 6.001 s into our cycle
        def get(self, token, default=None):
            env.now = start + 6_001
            return body("0.0002"), start + 6_001

    env.cache = WrittenMidCycle()
    provider = FakeProvider(lambda: env.now)
    env.capture(provider).cycle()
    assert rows(env.store, "SELECT observed_ms, origin FROM observations") == [(start + 6_001, "cache")]
    assert provider.calls == []


def test_the_implementation_revision_is_recorded_once_and_shown(env):
    source_db(env.db, now=env.now, decisions=[decision(env.now - 1000, "a")])
    conn = pc.open_store(env.store)  # a store that began under the original code, 2 days earlier
    with conn:
        pc._set_meta(conn, "contract_sha256", pc.CONTRACT_SHA256)
        pc._set_meta(conn, "capture_start_ms", env.now - 2 * DAY)
    conn.close()
    env.capture().cycle()
    env.now += MIN
    env.capture().cycle()
    hist = pc.status(env.store, now_ms=env.now)["implementation_history"]
    assert hist == [{"implementation": pc.IMPLEMENTATION_ORIGINAL, "from_ms": T0 - 2 * DAY},
                    {"implementation": pc.IMPLEMENTATION, "from_ms": T0}]
    assert pc.status(env.store, now_ms=env.now)["implementation"] == pc.IMPLEMENTATION


def test_a_new_store_records_only_the_current_implementation(env):
    source_db(env.db, now=env.now)
    env.capture().cycle()
    assert pc.status(env.store, now_ms=env.now)["implementation_history"] == [
        {"implementation": pc.IMPLEMENTATION, "from_ms": T0}]


@pytest.mark.parametrize("setup, reason", [
    ({"unresolved": 1}, "orders_unresolved"),
    ({"halt": "protection_overrun:3x"}, "entries_halted"),
    ({"banned": T0 + 60_000}, "gmgn_cooldown"),
    ({"family_ban": T0 + 60_000}, "gmgn_cooldown"),
    ({"penalty": 1}, "gmgn_penalty"),
    ({"credit": 10_000}, "gmgn_credit_low"),
])
def test_research_yields_to_reconciliation_protection_and_gmgn_without_a_provider_call(env, setup, reason):
    source_db(env.db, now=env.now, decisions=[decision(env.now - 1000, "a")], **setup)
    provider = FakeProvider(lambda: env.now)
    report = env.capture(provider).cycle()
    assert report["yield"] == reason and provider.calls == []
    assert rows(env.store, "SELECT reason FROM gaps") == [(f"deferred:{reason}",)]


def test_an_operator_or_daily_loss_halt_does_not_stop_research(env):
    source_db(env.db, now=env.now, decisions=[decision(env.now - 1000, "a")], halt="daily_loss_stop")
    provider = FakeProvider(lambda: env.now)
    assert env.capture(provider).cycle()["yield"] is None and len(provider.calls) == 1


def test_cpu_saturation_defers_reads(env):
    source_db(env.db, now=env.now, decisions=[decision(env.now - 1000, "a")])
    env.load = 7.0  # > 3.0 per cpu x 2
    provider = FakeProvider(lambda: env.now)
    assert env.capture(provider).cycle()["yield"] == "cpu_saturated" and provider.calls == []


def test_the_daily_budget_stops_reads_but_not_free_cache_reuse(env):
    source_db(env.db, now=env.now, decisions=[decision(env.now - 1000, "a"), decision(env.now - 1000, "b")])
    conn = pc.open_store(env.store)
    with conn:
        pc._bump(conn, pc.utc_day(env.now), "provider_weight", pc.CONTRACT["daily_reads"])
    conn.close()
    env.cache = {"a": (body("0.0002"), env.now - 3_000)}
    provider = FakeProvider(lambda: env.now)
    report = env.capture(provider).cycle()
    assert provider.calls == [] and report["observe"] == {"cache_reused": 1, "deferred": 1}
    assert rows(env.store, "SELECT token, reason FROM gaps") == [("b", "deferred:daily_budget")]


def test_a_limiter_refusal_is_not_retried_and_the_cycle_yields(env):
    source_db(env.db, now=env.now, decisions=[decision(env.now - 1000, t) for t in "abc"])
    provider = FakeProvider(lambda: env.now, fail="limiter refused: gmgn: bucket exhausted (retry in 1.0s)")
    report = env.capture(provider).cycle()
    assert len(provider.calls) == 1
    assert report["observe"] == {"limiter_refused": 1, "deferred": 2}


@pytest.mark.parametrize("used_pct, free, reason", [(92.5, 40 * GIB, "disk_halt"), (60.0, 3 * GIB, "disk_free_floor")])
def test_disk_guard_halt_or_free_floor_defers_without_writing_anything(env, used_pct, free, reason):
    source_db(env.db, now=env.now, decisions=[decision(env.now - 1000, "a")])
    env.used_pct, env.free = used_pct, free
    report = env.capture().cycle()
    assert (report["status"], report["reason"]) == ("deferred", reason)
    assert not env.store.exists()


def test_the_disk_guards_act_band_still_captures_into_the_bounded_store(env):
    source_db(env.db, now=env.now, decisions=[decision(env.now - 1000, "a")])
    env.used_pct = 86.5  # today's box: the guard prunes kaiba.db; this store is separate and capped
    assert env.capture().cycle()["status"] == "ok"


def test_a_restart_neither_recounts_decisions_nor_duplicates_observations(env):
    source_db(env.db, now=env.now, decisions=[decision(env.now - 1000, "a")])
    env.capture().cycle()
    start = rows(env.store, "SELECT value FROM meta WHERE key='capture_start_ms'")
    env.now += 1_000  # a restart: a new process, the same store
    env.capture().cycle()
    day = pc.utc_day(env.now)
    assert ledger(env.store)[(day, "sol_decisions")] == 1
    assert rows(env.store, "SELECT value FROM meta WHERE key='capture_start_ms'") == start
    conn = pc.open_store(env.store)
    try:
        cap = env.capture()
        first = rows(env.store, "SELECT observed_ms FROM observations")[0][0]
        assert cap.record(conn, "a", body("0.0002"), first, "cache", env.now) == "duplicate"
    finally:
        conn.close()
    assert len(rows(env.store, "SELECT * FROM observations")) == 1


def test_a_changed_contract_refuses_to_mix_designs(env, monkeypatch):
    source_db(env.db, now=env.now, decisions=[decision(env.now - 1000, "a")])
    env.capture().cycle()
    monkeypatch.setattr(pc, "CONTRACT_SHA256", "0" * 64)
    assert env.capture().cycle()["reason"] == "contract_changed"


def test_a_wal_over_the_limit_defers_the_cycle(env, monkeypatch):
    from kaiba.learning import autolearn_io

    source_db(env.db, now=env.now, decisions=[decision(env.now - 1000, "a")])
    monkeypatch.setattr(autolearn_io, "WAL_LIMIT", -1)
    report = env.capture().cycle()
    assert (report["status"], report["reason"]) == ("deferred", "source:SourceDeferred")


# ---------------------------------------------------------------- execution model


def obs(t, price, liquidity="20000", basis="market_cap"):
    p = Decimal(str(price))
    return {"observed_ms": t, "available_ms": t, "price_usd": p, "cap_basis": basis,
            "market_cap_usd": p * 1_000_000_000 if basis == "market_cap" else None,
            "liquidity_usd": Decimal(liquidity) if liquidity is not None else None}


NATIVE = [(0, Decimal("100"))]
BASE = {"name": "base", "entry_bps": 313, "exit_bps": 225, "tip_lamports": 1_000_000, "impact": True}
FLAT = {"name": "flat325", "entry_bps": 325, "exit_bps": 325, "tip_lamports": 0, "impact": False}


def path(prices, start=0, step=MIN, liquidity="20000"):
    return [obs(start + i * step, p, liquidity) for i, p in enumerate(prices)]


def test_no_observation_at_the_signal_is_unfilled_not_backfilled():
    case = pc.simulate(path([0.0002] * 3, start=5 * MIN), 0, size=50_000_000, costs=BASE, native=NATIVE)
    assert case["status"] == "unfilled_no_signal_observation" and case["entry_ms"] is None


def test_no_observation_in_the_entry_window_is_unfilled():
    o = [obs(1_000, 0.0002), obs(1_000 + 200_000, 0.0002)]
    case = pc.simulate(o, 0, size=50_000_000, costs=BASE, native=NATIVE)
    assert case["status"] == "unfilled_no_entry_observation" and case["cost_lamports"] is None


def test_entry_waits_the_declared_delay_and_a_stop_settles_at_the_observed_price():
    o = path([0.0002, 0.0002, 0.00019, 0.00013])  # -35% at minute 3
    case = pc.simulate(o, 0, size=50_000_000, costs=FLAT, native=NATIVE)
    assert case["entry_ms"] == MIN and case["signal_ms"] == 0  # not the signal's own observation
    assert case["status"] == "settled_stop" and case["settled_ms"] == 3 * MIN
    assert case["proceeds_lamports"] == int(Decimal(50_000_000) * Decimal("0.65") * Decimal("0.9675") / Decimal("1.0325"))


def test_base_costs_charge_tips_and_liquidity_impact_flat_does_not():
    o = path([0.0002, 0.0002] + [0.0002] * 59 + [0.0002])  # flat price, time exit
    base = pc.simulate(o, 0, size=500_000_000, costs=BASE, native=NATIVE)
    flat = pc.simulate(o, 0, size=500_000_000, costs=FLAT, native=NATIVE)
    assert base["status"] == flat["status"] == "settled_time"
    assert base["cost_lamports"] == 501_000_000 and flat["cost_lamports"] == 500_000_000
    assert Decimal(base["entry_impact_bps"]) == Decimal(10_000) * 50 / 10_000  # $50 into half of $20k
    assert flat["entry_impact_bps"] is None
    assert base["proceeds_lamports"] < flat["proceeds_lamports"] < 500_000_000


def test_a_token_that_stops_reporting_after_entry_locks_its_cost():
    case = pc.simulate(path([0.0002, 0.0002]), 0, size=50_000_000, costs=BASE, native=NATIVE)
    assert case["status"] == "vanished_after_entry"
    assert case["entry_ms"] is not None and case["settled_ms"] is None and case["proceeds_lamports"] is None


def test_a_triggered_exit_with_no_liquidity_is_a_failed_exit_not_a_price():
    o = path([0.0002, 0.0002]) + [obs(2 * MIN + i * MIN, 0.0001, liquidity="100") for i in range(5)]
    case = pc.simulate(o, 0, size=50_000_000, costs=BASE, native=NATIVE)
    assert case["status"] == "exit_unsellable" and case["settled_ms"] is None


def test_entry_below_the_liquidity_floor_is_unfilled():
    case = pc.simulate(path([0.0002] * 4, liquidity="100"), 0, size=50_000_000, costs=BASE, native=NATIVE)
    assert case["status"] == "unfilled_entry_liquidity"


# ---------------------------------------------------------------- export and the frozen evaluator


def seeded_store(env, prices_for, *, tokens=2, census_extra=30):
    """A store whose capture started on day D 00:10; tokens admitted on D+1 (development)."""
    source_db(env.db, now=env.now)
    env.capture().cycle()  # writes the contract and capture_start_ms = T0
    dev_start, dev_end = pc.windows(T0)["development"]
    conn = pc.open_store(env.store)
    cap = env.capture()
    with conn:
        for i in range(tokens):
            token, admitted = f"tok{i}", dev_start + HOUR + i * 4 * HOUR
            conn.execute("INSERT INTO watch VALUES (?, ?, 'd', '200000', 5)", (token, admitted))
            for m, price in enumerate(prices_for(i)):
                t = admitted + m * MIN
                assert cap.record(conn, token, body(price), t, "fetched", t) == "recorded"
            conn.execute("INSERT INTO census VALUES (?, ?, ?, 'admitted')", (pc.utc_day(admitted), token, admitted))
        for j in range(census_extra):
            conn.execute("INSERT INTO census VALUES (?, ?, ?, 'list_full')", (pc.utc_day(dev_start), f"x{j}", dev_start))
        conn.execute("INSERT INTO native VALUES (?, '100')", (dev_start,))
    conn.close()
    return dev_start, dev_end


HOUR = 60 * MIN


def test_export_is_valid_pullback_input_with_selected_population_and_modeled_cashflows(env):
    _dev_start, dev_end = seeded_store(env, lambda i: [0.0002] * 190)
    data, side = pc.export(env.store, "development", size=50_000_000, costs="base", now_ms=dev_end + HOUR)
    pullback_study.validate(data)
    assert data["population"] == "selected" and data["population"] != "all_scanned"
    assert data["scanned_count"] == 32 and len(data["opportunities"]) == 20
    assert {c["evidence"] for c in data["opportunities"]} == {"modeled"}
    assert data["capture"]["measured_cashflows"] == 0
    assert all(o["cap_basis"] == "market_cap" for o in data["observations"])
    assert side["status_counts"] == {"settled_time": 20}


def test_a_dip_seen_only_after_the_signal_cannot_qualify_it(env):
    # flat at $200k through the first signal (minute 30), then a 30% dip afterwards
    _s, dev_end = seeded_store(env, lambda i: [0.0002] * 31 + [0.00014] * 159, tokens=1)
    data, _side = pc.export(env.store, "development", size=50_000_000, costs="flat325", now_ms=dev_end + HOUR)
    flags, reason = pullback_study.classify(
        [o for o in data["observations"]], data["opportunities"][0]["signal_ms"])
    assert reason == "outside_dip_band" and not flags["cap_dip"]


def test_the_study_runs_unchanged_and_counts_locked_inventory(env):
    # token 0 vanishes after its first entry; token 1 trades flat
    _s, dev_end = seeded_store(env, lambda i: [0.0002] * (32 if i == 0 else 190))  # entry at minute 31, then nothing
    data, side = pc.export(env.store, "development", size=50_000_000, costs="base", now_ms=dev_end + HOUR)
    assert side["status_counts"]["vanished_after_entry"] == 1
    result = pullback_study.study(data, capitals=(500_000_000,), reserve=20_000_000)
    base = next(r for r in result["results"] if r["arm"] == "baseline")
    assert base["unsettled_cost_lamports"] == 51_000_000 and base["liquidation_equity_lamports"] is None
    assert result["profit_proven"] is False


def test_windows_open_and_holdout_order_are_enforced(env, tmp_path):
    _s, dev_end = seeded_store(env, lambda i: [0.0002] * 190)
    with pytest.raises(pc.Refused, match="window_open"):
        pc.export(env.store, "development", size=50_000_000, now_ms=dev_end)
    holdout_end = pc.windows(T0)["holdout"][1]
    with pytest.raises(pc.Refused, match="development_not_reported"):
        pc.export(env.store, "holdout", size=50_000_000, now_ms=holdout_end + HOUR)
    with pytest.raises(pc.Refused, match="size_not_in_contract"):
        pc.export(env.store, "development", size=123, now_ms=dev_end + HOUR)
    out = pc.evaluate(env.store, "development", tmp_path / "reports", now_ms=dev_end + HOUR)
    assert len(out["reports"]) == 4 and all(r["study"]["profit_proven"] is False for r in out["reports"])
    with pytest.raises(pc.Refused, match="window_already_reported"):
        pc.evaluate(env.store, "development", tmp_path / "reports", now_ms=dev_end + HOUR)
    pc.export(env.store, "holdout", size=50_000_000, now_ms=holdout_end + HOUR)  # now allowed


def test_coverage_view_carries_no_prices_after_the_signal(env):
    dev_start, _e = seeded_store(env, lambda i: [0.0002] * 190)
    data, side = pc.export(env.store, "development", size=50_000_000, completeness_only=True,
                           now_ms=dev_start + 2 * HOUR)
    assert all(c["entry_ms"] is None and c["proceeds_lamports"] is None for c in data["opportunities"])
    assert set(side["status_counts"]) <= {"entry_observation_available", "no_entry_observation",
                                          "no_signal_observation"}
    assert all("exit_price_usd" not in e for e in side["execution"])


def test_a_census_smaller_than_the_opportunities_refuses_instead_of_padding(env):
    _s, dev_end = seeded_store(env, lambda i: [0.0002] * 190, census_extra=0)
    with pytest.raises(pc.Refused, match="census_smaller_than_opportunities"):
        pc.export(env.store, "development", size=50_000_000, now_ms=dev_end + HOUR)
