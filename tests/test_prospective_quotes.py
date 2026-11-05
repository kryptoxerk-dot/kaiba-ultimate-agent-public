"""Real research books plus offline provider/cache boundaries for observations.

Codex AUTOLEARN-BUDGET 2026-10-18: the second observation in the real-wrapper
cache test follows its actual durable admission, independent of metadata latency.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from kaiba.core import limiter
from kaiba.core.limiter import Limits, Priority
from kaiba.core.schemas import Chain, EvidenceBasis, Receipt, digest
from kaiba.learning import prospective_quotes as pq
from kaiba.learning.autolearn_io import GuardedReader, quote_evidence
from kaiba.learning.candidate_paper import CandidatePaperRunner
from kaiba.learning.signal_audit import ExitLadder
from kaiba.learning.strategy_campaign import generate_campaign
from kaiba.providers import _http, gmgn_cli

T0 = 1_800_000_000_000
TOKEN, SECOND = "A" * 32, "B" * 32


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def opportunity(token=TOKEN, *, chain="sol", at=T0 + 1000):
    return {"decision_id": token, "ts_ms": at, "chain": chain, "token": token,
            "lane": "sm-trenches", "size_base_units": 100_000_000,
            "paper_stake_usd": "30", "features": {}, "feature_ts_ms": at}


def snapshot(at):
    return {"quote_ts_ms": at, "price_usd": "1", "liquidity_usd": "1000000", "native_usd": "100",
            "decimals": 6, "basis": "fixture-pool", "tax_known_zero": True,
            "venue_fee_bps": 125, "venue_fee_source": "synthetic-fixture"}


class Provider:
    def __init__(self, clock):
        self.clock, self.calls = clock, []
        self.write_cache, self.available = True, True
        self.change_data, self.receipt_changes = {}, {}
        self.after = lambda: None

    def token_info(self, token, chain, **kwargs):
        self.calls.append((token, chain, kwargs))
        data = {"address": token, "chain": chain.value, "decimals": 6, "liquidity": "1000000",
                "price": {"address": token, "chain": chain.value, "price": "1"}}
        data.update(self.change_data)
        key = f"gmgn-cli token info --address {token} --chain {chain.value} --raw"
        if self.write_cache and self.available:
            _http.cache_write("gmgn", key, data)
        receipt = {"provider": "gmgn", "endpoint": "token.info", "observed_at_ms": self.clock["ms"],
                   "basis": EvidenceBasis.PROVIDER_REPORTED if self.available else EvidenceBasis.UNAVAILABLE,
                   "request_digest": digest(key), "response_digest": digest(data), **self.receipt_changes}
        self.after()
        return SimpleNamespace(data=data, receipt=Receipt(**receipt), ok=self.available)


@pytest.fixture
def setup(tmp_db, tmp_path, monkeypatch):
    clock = {"ms": T0 + 1000}
    monkeypatch.setattr(_http, "now_ms", lambda: clock["ms"])
    root, research = tmp_path / "learning", tmp_path / "learning/autolearn"
    manifest = generate_campaign([], ExitLadder(), now_ms=T0,
        groups=[("sm-trenches", "sol")], max_candidates=2)
    name = manifest["campaign_id"] + ".json"
    write(research / name, manifest)
    write(research / "state.json", {"campaign_files": [name]})
    runner = CandidatePaperRunner(research / "paper", learning_root=root, max_stake_usd="30",
                                  clock=lambda: clock["ms"] / 1000)
    result = runner.run_candidates(manifest, [opportunity()], {}, now_ms=clock["ms"])
    assert not result["errors"]
    provider = Provider(clock)
    collector = pq.ProspectiveQuoteCollector(research, root / "prospective-quotes", learning_root=root,
                                            clock=lambda: clock["ms"] / 1000, provider=provider)
    return SimpleNamespace(clock=clock, root=root, research=research, manifest=manifest,
        runner=runner, provider=provider, collector=collector, db=tmp_db)


def at(s, delta=31_000):
    s.clock["ms"] = T0 + delta
    return s.collector.once()


def book(s, arm=0):
    return s.runner.book_path(s.manifest["campaign_id"], s.manifest["hypotheses"][arm]["hypothesis_id"])


def mutate_books(s, sql, args=()):
    for arm in s.manifest["hypotheses"]:
        with sqlite3.connect(s.runner.book_path(s.manifest["campaign_id"], arm["hypothesis_id"])) as conn:
            conn.execute(sql, args)


def cached_market(s, *, fetched=None, data=None):
    key = f"gmgn-cli token info --address {TOKEN} --chain sol --raw"
    path = _http.cache_path("gmgn", key)
    write(path, {"fetched_ms": s.clock["ms"] if fetched is None else fetched,
        "data": data or {"address": TOKEN, "decimals": 6, "liquidity": "1000000",
                         "price": {"address": TOKEN, "price": "1"}}})
    return path


def limiter_state(s, monkeypatch, **changes):
    limits = Limits(min_interval_ms=1000, capacity=10, refill_per_s=.2, max_inflight=1)
    monkeypatch.setattr(limiter, "limits_for", lambda provider: limits)
    now = s.clock["ms"]
    fields = {"provider": "gmgn", "credit_milli": 10000, "last_refill_ms": now,
              "last_call_ms": 0, "inflight": 0, "banned_until_ms": 0,
              "penalty_level": 0, "spent_today": 0,
              "day_key": datetime.datetime.fromtimestamp(now / 1000, datetime.UTC).date().isoformat(),
              "inflight_over_since_ms": None, **changes}
    s.db.execute("INSERT OR REPLACE INTO provider_state (" + ",".join(fields) + ") VALUES (" +
                 ",".join("?" for _ in fields) + ")", tuple(fields.values()))
    return limits


def actual_epoch(s):
    actual = int(time.time() * 1000)
    s.clock["ms"] = actual
    manifest = generate_campaign([], ExitLadder(), now_ms=actual - 40_000,
        groups=[("sm-trenches", "sol")], max_candidates=2)
    name = manifest["campaign_id"] + ".json"
    write(s.research / name, manifest)
    write(s.research / "state.json", {"campaign_files": [name]})
    runner = CandidatePaperRunner(s.research / "paper", learning_root=s.root, max_stake_usd="30",
                                  clock=lambda: s.clock["ms"] / 1000)
    assert not runner.run_candidates(manifest, [opportunity(at=actual - 31_000)], {}, now_ms=actual)["errors"]
    return actual


@pytest.mark.parametrize("price_address", [True, False])
def test_existing_fresh_cache_skips_local_admission_and_preserves_original_clock(setup, price_address):
    s = setup
    s.clock["ms"] = T0 + 31_000
    data = {"address": TOKEN, "decimals": 6, "liquidity": "1000",
            "price": {"price": "1", "updated_at": (T0 - 120_000) // 1000},
            "pool": {"address": "D" * 32, "base_address": SECOND, "quote_address": TOKEN}}
    if price_address:
        data["price"]["address"] = TOKEN
    path = cached_market(s, fetched=T0 + 30_000, data=data)
    original = path.read_bytes()
    report = s.collector.once()
    assert report["reason"] == "fresh_canonical_cache_already_present"
    assert report["existing_cache"]["fetched_ms"] == T0 + 30_000
    assert report["existing_cache"]["age_ms"] == 1000
    assert report["existing_cache"]["new_receipt_created"] is False and "attempt" not in report
    assert report["provider_attempts"] == report["daily_admitted_attempts"] == 0
    state = read(s.collector.state_path)
    assert state["attempts"] == [] and state["days"] == {} and state["tokens"] == {}
    assert not s.provider.calls and path.read_bytes() == original


@pytest.mark.parametrize("mutation", ["stale", "future", "wrong_address", "wrong_price", "wrong_pool", "no_depth", "no_decimals", "malformed"])
def test_unknown_or_conflicting_cache_never_suppresses_authoritative_path(setup, mutation):
    s = setup
    s.clock["ms"] = T0 + 31_000
    path = cached_market(s)
    value = read(path)
    if mutation == "stale":
        value["fetched_ms"] -= 15_001
    elif mutation == "future":
        value["fetched_ms"] += 1
    elif mutation == "wrong_address":
        value["data"]["address"] = SECOND
    elif mutation == "wrong_price":
        value["data"]["price"]["address"] = SECOND
    elif mutation == "wrong_pool":
        value["data"]["pool"] = {"base_address": SECOND, "quote_address": "C" * 32}
    elif mutation == "no_depth":
        value["data"]["liquidity"] = None
    elif mutation == "no_decimals":
        value["data"]["decimals"] = None
    else:
        value = []
    write(path, value)
    assert s.collector.once()["status"] == "captured" and len(s.provider.calls) == 1


def test_fresh_cache_rotation_does_not_starve_another_candidate(setup):
    s = setup
    s.runner.run_candidates(s.manifest, [opportunity(SECOND)], {}, now_ms=T0 + 1000)
    s.clock["ms"] = T0 + 31_000
    cached_market(s)
    assert s.collector.once()["reason"] == "fresh_canonical_cache_already_present"
    second = at(s, 32_000)
    assert second["attempt"]["target"]["token"] == SECOND and len(s.provider.calls) == 1


@pytest.mark.parametrize("category", ["provider_cooldown", "token_cooldown", "daily_credit_cap", "minimum_interval", "max_inflight", "bucket_exhausted"])
def test_known_shared_refusals_are_readonly_and_do_not_consume_local_slots(setup, monkeypatch, category):
    s = setup
    s.clock["ms"] = T0 + 31_000
    changes = {"provider_cooldown": {"banned_until_ms": s.clock["ms"] + 1000},
               "minimum_interval": {"last_call_ms": s.clock["ms"] - 500},
               "max_inflight": {"inflight": 1, "inflight_over_since_ms": s.clock["ms"] - 1000},
               "bucket_exhausted": {"credit_milli": 999},
               "daily_credit_cap": {"spent_today": 10}}
    limits = limiter_state(s, monkeypatch, **changes.get(category, {}))
    if category == "daily_credit_cap":
        monkeypatch.setattr(limiter, "limits_for", lambda provider: Limits(**{**limits.__dict__, "daily_credit_cap": 10}))
    if category == "token_cooldown":
        s.db.execute("INSERT INTO provider_family_bans (provider,family,banned_until_ms) VALUES ('gmgn','token',?)",
                     (s.clock["ms"] + 1000,))
    before = tuple(s.db.execute("SELECT * FROM provider_state WHERE provider='gmgn'").fetchone())
    report = s.collector.once()
    assert report["reason"] == "known_shared_limiter_refusal" and report["limiter_preflight"]["category"] == category
    assert len(report["limiter_preflight"]["query_receipts"]) == 2
    assert report["daily_admitted_attempts"] == report["provider_attempts"] == 0 and not s.provider.calls
    assert tuple(s.db.execute("SELECT * FROM provider_state WHERE provider='gmgn'").fetchone()) == before
    assert s.db.execute("SELECT COUNT(*) FROM provider_calls").fetchone()[0] == 0


@pytest.mark.parametrize("lease,last_call", [(None, -2000), (-120_000, -2000), (-5000, -120_001)])
def test_potential_inflight_reclamation_is_left_to_atomic_guard(setup, monkeypatch, lease, last_call):
    s = setup
    s.clock["ms"] = T0 + 31_000
    limiter_state(s, monkeypatch, inflight=4, last_call_ms=s.clock["ms"] + last_call,
                  inflight_over_since_ms=None if lease is None else s.clock["ms"] + lease)
    assert s.collector.once()["status"] == "captured" and len(s.provider.calls) == 1
    # This fake provider does not mutate limiter state; preflight must not reclaim.
    assert s.db.execute("SELECT inflight FROM provider_state WHERE provider='gmgn'").fetchone()[0] == 4


@pytest.mark.parametrize("changes", [{"inflight": "broken"}, {"credit_milli": "broken"}, {"last_call_ms": T0 + 31_001}, {"inflight_over_since_ms": T0 + 31_001}])
def test_malformed_or_future_limiter_hints_defer_to_authoritative_guard(setup, monkeypatch, changes):
    s = setup
    s.clock["ms"] = T0 + 31_000
    limiter_state(s, monkeypatch, **changes)
    report = s.collector.once()
    assert report["status"] == "captured" and report["limiter_preflight"]["category"] == "unknown_allow_authoritative_guard"
    assert len(s.provider.calls) == 1


def test_bucket_boundary_matches_atomic_reserve_and_keeps_three_second_query_guards(setup, monkeypatch):
    s = setup
    s.clock["ms"] = T0 + 31_000
    # In binary float, 5.499 seconds * .2 *1000 is1099.8. The exact
    # authoritative operation order is used, including its integer conversion.
    limiter_state(s, monkeypatch, credit_milli=-99, last_refill_ms=s.clock["ms"] - 5499)
    observed = []
    original = pq.GuardedReader
    def reader(*args, **kwargs):
        observed.append(kwargs)
        return original(*args, **kwargs)
    monkeypatch.setattr(pq, "GuardedReader", reader)
    report = s.collector.once()
    assert report["status"] == "captured" and observed == [{"query_seconds": 3, "max_rows": 2}]
    monkeypatch.setattr(limiter, "now_ms", lambda: s.clock["ms"])
    assert limiter._reserve_once(s.db, "gmgn", "token.info", Priority.RESEARCH) == 1


def test_preflight_WAL_growth_before_second_query_defers_sixty_seconds_without_charge(setup, monkeypatch):
    s = setup
    original, calls = pq.GuardedReader.rows, []
    def rows(reader, sql, args=()):
        calls.append(sql)
        if "FROM provider_state" in sql:
            result = original(reader, sql, args)
            def overflow(state):
                raise pq.SourceDeferred("wal_over_limit:between_preflight_queries")
            monkeypatch.setattr(s.collector, "_wal_guard", overflow)
            return result
        return original(reader, sql, args)
    monkeypatch.setattr(pq.GuardedReader, "rows", rows)
    report = at(s)
    assert report["reason"].startswith("wal_over_limit:") and report["daily_admitted_attempts"] == 0
    assert len(calls) == 1 and not s.provider.calls
    assert read(s.collector.state_path)["retry_after_ms"] == T0 + 91_000


def test_real_SQLite_preflight_read_lock_defers_without_provider_or_admission(setup):
    s = setup
    s.db.execute("PRAGMA journal_mode=DELETE")
    s.db.execute("BEGIN EXCLUSIVE")
    try:
        report = at(s)
        assert report["reason"].startswith("source_query_unavailable:")
        assert report["provider_attempts"] == report["daily_admitted_attempts"] == 0
        assert read(s.collector.state_path)["retry_after_ms"] == T0 + 91_000
        assert not s.provider.calls
    finally:
        s.db.rollback()


@pytest.mark.parametrize("finished,expected", [(120_999, "captured"), (121_001, "deferred")])
def test_preflight_rechecks_actual_entry_deadline_before_admission(setup, monkeypatch, finished, expected):
    s = setup
    original = s.collector._limiter_preflight
    def slow(state):
        result = original(state)
        s.clock["ms"] = T0 + finished
        return result
    monkeypatch.setattr(s.collector, "_limiter_preflight", slow)
    report = at(s)
    assert report["status"] == expected
    assert report["daily_admitted_attempts"] == int(expected == "captured")
    if expected == "deferred":
        assert report["reason"] == "cadence_or_entry_window_elapsed_before_admission" and not s.provider.calls
    else:
        assert report["attempt"]["admitted_ms"] == T0 + finished


def test_preflight_midnight_charges_actual_new_day_and_preserves_old_exhaustion(setup, monkeypatch):
    s = setup
    s.clock["ms"] = T0 + 31_000
    assert all(a["open"] for a in s.runner.run_candidates(s.manifest, [],
        {("sol", TOKEN): snapshot(s.clock["ms"])}, now_ms=s.clock["ms"])["arms"])
    midnight = ((s.clock["ms"] // pq.DAY_MS) + 1) * pq.DAY_MS
    s.clock["ms"] = midnight - 100
    old = str(s.clock["ms"] // pq.DAY_MS)
    write(s.collector.state_path, {"version": pq.VERSION, "policy": pq.POLICY, "days": {old: 99},
        "tokens": {}, "attempts": [], "retry_after_ms": 0, "updated_ms": 0})
    original = s.collector._limiter_preflight
    def midnight_read(state):
        result = original(state)
        s.clock["ms"] = midnight + 100
        return result
    monkeypatch.setattr(s.collector, "_limiter_preflight", midnight_read)
    report = s.collector.once()
    state = read(s.collector.state_path)
    assert report["status"] == "captured" and report["daily_admitted_attempts"] == 1
    assert state["days"] == {old: 99, str(midnight // pq.DAY_MS): 1}
    assert report["attempt"]["admitted_ms"] == midnight + 100


def test_actual_final_pre_admission_guard_delay_cannot_charge_an_expired_pending_target(setup, monkeypatch):
    s = setup
    original = s.collector._wal_guard
    guards = []
    def slow(state):
        guards.append(1)
        if len(guards) == 4:  # source guard, two guarded queries, then final admission guard
            s.clock["ms"] = T0 + 121_001
        return original(state)
    monkeypatch.setattr(s.collector, "_wal_guard", slow)
    report = at(s)
    assert report["reason"] == "cadence_or_entry_window_elapsed_before_admission"
    assert report["daily_admitted_attempts"] == report["provider_attempts"] == 0 and not s.provider.calls


def test_actual_typed_wrapper_limiter_race_consumes_one_admission_without_spawn(setup, monkeypatch):
    s = setup
    actual_epoch(s)
    limiter_state(s, monkeypatch)
    s.collector.provider, s.collector.clock = gmgn_cli, time.time
    calls = []
    monkeypatch.setattr(gmgn_cli, "cli_argv", lambda: ["offline-test-boundary"])
    monkeypatch.setattr(gmgn_cli, "_spawn", lambda *args: calls.append(args))
    original = s.collector._limiter_preflight
    def raced(state):
        result = original(state)
        assert not result["known_refusal"]
        s.db.execute("UPDATE provider_state SET banned_until_ms=? WHERE provider='gmgn'", (actual_epoch_ms() + 60_000,))
        return result
    monkeypatch.setattr(s.collector, "_limiter_preflight", raced)
    report = s.collector.once()
    assert report["status"] == "unavailable" and report["daily_admitted_attempts"] == 1
    assert report["provider_attempts"] == 1 and calls == []
    assert report["attempt"]["cache"]["failure_category"] == "local_limiter_provider_cooldown"
    assert s.db.execute("SELECT spent_today FROM provider_state WHERE provider='gmgn'").fetchone()[0] == 0
    assert "note" not in report["attempt"]["cache"]["receipt"]


def actual_epoch_ms():
    return int(time.time() * 1000)


@pytest.mark.parametrize("note,category", [("limiter refused: gmgn: max inflight (retry in 0.5s)", "local_limiter_max_inflight"),
    ("secret-api-key=never-write-this", "unavailable_other_redacted"), (None, "unavailable_detail_not_retained")])
def test_unavailable_receipt_persists_only_fixed_safe_failure_categories(setup, note, category):
    s = setup
    s.provider.available = False
    s.provider.receipt_changes["note"] = note
    report = at(s)
    assert report["attempt"]["cache"]["failure_category"] == category
    assert "never-write-this" not in s.collector.state_path.read_text()
    assert "note" not in report["attempt"]["cache"]["receipt"]


def test_one_unique_capture_from_real_pending_books_preserves_cache_clock_and_main_ledger(setup):
    s = setup
    report = at(s)
    assert report["status"] == "captured" and report["eligible_targets"] == 1
    assert report["provider_attempts"] == report["daily_admitted_attempts"] == 1
    assert len(s.provider.calls) == 1
    _, chain, kwargs = s.provider.calls[0]
    assert chain is Chain.SOL
    assert kwargs == {"priority": Priority.RESEARCH,
        "ttl_s": 15, "stale_grace_s": 0, "timeout_s": 6, "wait_for_slot_s": 0}
    cache = report["attempt"]["cache"]
    assert cache["verified"] and cache["market_fields_usable"]
    assert cache["fetched_ms"] == cache["receipt"]["observed_at_ms"] == T0 + 31_000
    assert cache["cache_sha256"] == hashlib.sha256(Path(cache["cache_path"]).read_bytes()).hexdigest()
    assert s.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    assert s.db.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0
    assert not (s.research / "paper/kaiba.db").exists()


@pytest.mark.parametrize("delta", [1000, 30_999, 121_001, 500_000])
def test_pre_delay_and_expired_attempts_do_not_acquire(setup, delta):
    s = setup
    result = at(s, delta)
    assert result["provider_attempts"] == 0 and not s.provider.calls
    assert read(s.collector.state_path)["days"] == {}


def test_real_open_paper_inventory_remains_eligible_after_entry_deadline_then_closed_is_not(setup):
    s = setup
    s.clock["ms"] = T0 + 31_000
    opened = s.runner.run_candidates(s.manifest, [], {("sol", TOKEN): snapshot(s.clock["ms"])}, now_ms=s.clock["ms"])
    assert all(r["open"] == 1 for r in opened["arms"])
    report = at(s, 200_000)
    assert report["status"] == "captured" and report["attempt"]["target"]["kind"] == "open"
    mutate_books(s, "UPDATE positions SET qty='0',closed_ms=?", (T0 + 200_000,))
    assert at(s, 216_000)["provider_attempts"] == 0
    assert len(s.provider.calls) == 1


@pytest.mark.parametrize("status", ["closed", "rejected", "failed"])
def test_nonpending_without_inventory_is_not_a_market_target(setup, status):
    s = setup
    mutate_books(s, "UPDATE candidate_entries SET status=?", (status,))
    assert at(s)["provider_attempts"] == 0


def test_canonical_capture_feeds_existing_adapter_and_real_broker_after_delay(setup):
    s = setup
    assert at(s)["status"] == "captured"
    s.db.execute("INSERT INTO tokens(chain,address,decimals,first_seen_ms,migrated_ms) VALUES ('sol',?,6,?,?)",
                 (TOKEN, T0, T0))
    s.db.execute("INSERT INTO native_prices(chain,ts_ms,price_usd,source) VALUES ('sol',?,'100','fixture')", (T0,))
    path = Path(s.db.execute("PRAGMA database_list").fetchone()[2])
    q = quote_evidence(GuardedReader(path), "sol", TOKEN, now_ms=s.clock["ms"], max_age_ms=60_000)
    assert q["basis"] == "captured_gmgn_pool_depth" and q["price_ts_ms"] == T0 + 31_000
    filled = s.runner.run_candidates(s.manifest, [], {("sol", TOKEN): q}, now_ms=s.clock["ms"])
    assert all(r["open"] == 1 and r["order_count"] == 1 for r in filled["arms"])
    assert all(r["entry_latency_ms"] == 30_000 for r in filled["assessment_rows"])


def test_fair_rotation_and_per_token_cadence_survive_restart(setup):
    s = setup
    s.runner.run_candidates(s.manifest, [opportunity(SECOND)], {}, now_ms=T0 + 1000)
    assert at(s)["attempt"]["target"]["token"] == TOKEN
    # Another token can use a subsequent cycle; a hot first token cannot monopolize it.
    assert at(s, 32_000)["attempt"]["target"]["token"] == SECOND
    assert at(s, 33_000)["provider_attempts"] == 0
    newer = pq.ProspectiveQuoteCollector(s.research, s.root / "prospective-quotes", learning_root=s.root,
        clock=lambda: s.clock["ms"] / 1000, provider=s.provider)
    s.clock["ms"] = T0 + 46_001  # The wrapper's positive TTL includes its exact 15s boundary.
    assert newer.once()["attempt"]["target"]["token"] == TOKEN
    assert len(s.provider.calls) == 3


def test_daily_attempt_cap_is_admitted_before_provider_and_next_utc_day_has_own_count(setup):
    s = setup
    at(s)
    state = read(s.collector.state_path)
    state["days"][str((T0 + 31_000) // pq.DAY_MS)] = 100
    write(s.collector.state_path, state)
    result = at(s, 46_000)
    assert result["reason"] == "daily_research_attempt_cap" and result["provider_attempts"] == 0
    assert len(s.provider.calls) == 1
    s.runner.run_candidates(s.manifest, [], {("sol", TOKEN): snapshot(s.clock["ms"])}, now_ms=s.clock["ms"])
    next_day = at(s, pq.DAY_MS + 46_000)
    assert next_day["status"] == "captured" and next_day["daily_admitted_attempts"] == 1
    assert 100 in read(s.collector.state_path)["days"].values()


def test_exhausted_day_skips_all_target_and_limiter_queries_then_next_day_checks_real_books(setup, monkeypatch):
    s = setup
    s.clock["ms"] = T0 + 31_000
    filled = s.runner.run_candidates(s.manifest, [], {("sol", TOKEN): snapshot(s.clock["ms"])}, now_ms=s.clock["ms"])
    assert all(arm["open"] == 1 for arm in filled["arms"])
    today = str(s.clock["ms"] // pq.DAY_MS)
    write(s.collector.state_path, {"version": pq.VERSION, "policy": pq.POLICY, "days": {today: 100},
        "tokens": {}, "attempts": [], "retry_after_ms": 0, "updated_ms": s.clock["ms"]})
    original_targets, original_rows = s.collector._targets, pq.GuardedReader.rows
    calls = []
    def forbidden(*args, **kwargs):
        raise AssertionError("exhausted quota must not read books or canonical limiter")
    monkeypatch.setattr(s.collector, "_targets", forbidden)
    monkeypatch.setattr(pq.GuardedReader, "rows", forbidden)
    report = s.collector.once()
    assert report["reason"] == "daily_research_attempt_cap" and report["status"] == "deferred"
    assert report["eligible_targets"] is None and report["eligibility_evaluated"] is False
    assert report["daily_admitted_attempts"] == 100 and report["provider_attempts"] == 0
    assert not s.provider.calls and read(s.collector.state_path)["days"] == {today: 100}
    def observed(now):
        calls.append(now)
        return original_targets(now)
    monkeypatch.setattr(s.collector, "_targets", observed)
    monkeypatch.setattr(pq.GuardedReader, "rows", original_rows)
    next_day = at(s, pq.DAY_MS + 31_000)
    assert next_day["status"] == "captured" and next_day["daily_admitted_attempts"] == 1
    assert next_day["eligibility_evaluated"] and next_day["eligible_targets"] == 1
    assert len(calls) == 2 and len(s.provider.calls) == 1
    assert read(s.collector.state_path)["days"][today] == 100


@pytest.mark.parametrize("mutation", ["policy", "future_day", "invalid_count"])
def test_exhausted_quota_does_not_mask_invalid_state(setup, monkeypatch, mutation):
    s = setup
    s.clock["ms"] = T0 + 31_000
    today = str(s.clock["ms"] // pq.DAY_MS)
    state = {"version": pq.VERSION, "policy": dict(pq.POLICY), "days": {today: 100},
        "tokens": {}, "attempts": [], "retry_after_ms": 0, "updated_ms": s.clock["ms"]}
    if mutation == "policy":
        state["policy"]["daily_attempts"] = 200
    elif mutation == "future_day":
        state["days"][str(int(today) + 1)] = 100
    else:
        state["days"][today] = 101
    write(s.collector.state_path, state)
    def forbidden(*args, **kwargs):
        raise AssertionError("malformed state must not reach target inspection")
    monkeypatch.setattr(s.collector, "_targets", forbidden)
    report = s.collector.once()
    assert report["status"] == "partial" and report["reason"].startswith("ValueError:")
    assert report["provider_attempts"] == 0 and not s.provider.calls
    retained = read(s.collector.state_path)
    assert retained["policy"] == state["policy"] and retained["days"] == state["days"]


def test_interruption_consumes_admission_and_never_replays_same_attempt(setup):
    s = setup
    def interrupted():
        raise SystemExit("interrupted_after_dispatch")
    s.provider.after = interrupted
    with pytest.raises(SystemExit):
        at(s)
    first = read(s.collector.state_path)
    assert sum(first["days"].values()) == 1 and first["attempts"][-1]["status"] == "admitted"
    assert not (s.collector.state_dir / "worker.lock").exists()
    s.provider.after = lambda: None
    assert at(s, 32_000)["provider_attempts"] == 0
    # The interrupted wrapper wrote a real cache; a subsequent cycle reuses it
    # without charging a new local admission, then acquires after its positive TTL.
    assert at(s, 46_000)["reason"] == "fresh_canonical_cache_already_present"
    second = at(s, 46_001)
    assert second["daily_admitted_attempts"] == 2
    assert second["attempt"]["attempt_id"] != first["attempts"][-1]["attempt_id"]
    assert len(s.provider.calls) == 2


def test_reservation_is_already_durable_at_provider_boundary(setup):
    s = setup
    original = s.provider.token_info
    def inspect_reservation(*args, **kwargs):
        state = read(s.collector.state_path)
        assert sum(state["days"].values()) == 1
        assert state["attempts"][-1]["status"] == "admitted"
        assert state["tokens"]["sol:" + TOKEN] == T0 + 31_000
        return original(*args, **kwargs)
    s.provider.token_info = inspect_reservation
    assert at(s)["status"] == "captured"


def test_slow_revalidation_cannot_dispatch_past_entry_deadline(setup, monkeypatch):
    s = setup
    original = s.collector._targets
    calls = []
    def slow_validation(now):
        result = original(now)
        calls.append(now)
        if len(calls) == 2:
            s.clock["ms"] = T0 + 122_000
        return result
    monkeypatch.setattr(s.collector, "_targets", slow_validation)
    report = at(s)
    assert report["provider_attempts"] == 0 and not s.provider.calls
    assert report["attempt"]["status"] == "entry_window_elapsed_during_validation"
    assert report["daily_admitted_attempts"] == 1


def test_future_day_counter_and_clock_rollback_cannot_restore_quota(setup):
    s = setup
    at(s)
    state = read(s.collector.state_path)
    future = str((T0 + 31_000) // pq.DAY_MS + 1)
    state["days"][future] = 100
    write(s.collector.state_path, state)
    report = at(s, 46_000)
    assert report["status"] == "partial" and report["provider_attempts"] == 0
    assert read(s.collector.state_path)["days"][future] == 100
    s.clock["ms"] = T0 + 32_000
    restarted = pq.ProspectiveQuoteCollector(s.research, s.root / "prospective-quotes", learning_root=s.root,
        clock=lambda: s.clock["ms"] / 1000, provider=s.provider)
    report = restarted.once()
    assert report["status"] == "partial" and "clock_moved_backward" in report["reason"]
    assert read(s.collector.state_path)["updated_ms"] == T0 + 46_000
    assert len(s.provider.calls) == 1


def test_WAL_pressure_and_its_retry_block_all_provider_accounting_for_sixty_seconds(setup):
    s = setup
    s.clock["ms"] = T0 + 31_000
    original = s.collector._wal_guard
    def overflow(state):
        state["retry_after_ms"] = s.collector._now() + 60_000
        raise pq.SourceDeferred("wal_over_limit:fixture")
    s.collector._wal_guard = overflow
    blocked = s.collector.once()
    assert blocked["provider_attempts"] == 0 and blocked["reason"].startswith("wal_over_limit:")
    s.collector._wal_guard = original
    assert at(s, 90_999)["reason"] == "source_retry_backoff"
    assert at(s, 91_000)["status"] == "captured"
    assert len(s.provider.calls) == 1


def test_actual_oversized_WAL_stat_has_no_provider_or_admission(tmp_path, monkeypatch):
    root = tmp_path / "learning"
    collector = pq.ProspectiveQuoteCollector(root / "research", root / "observer", learning_root=root)
    wal = Path(str(collector.db_path) + "-wal")
    wal.parent.mkdir(parents=True, exist_ok=True)
    with wal.open("wb") as stream:
        stream.truncate(pq.WAL_LIMIT + 1)  # sparse fixture, no canonical connection exists
    state = {}
    with pytest.raises(pq.SourceDeferred, match="wal_over_limit:"):
        collector._wal_guard(state)
    assert state["retry_after_ms"] >= collector._now() + 59_900


def test_WAL_growth_after_durable_admission_withholds_transport_and_keeps_slot(setup, monkeypatch):
    s = setup
    original, calls = s.collector._wal_guard, []
    def grow(state):
        calls.append(1)
        if s.collector.state_path.exists() and read(s.collector.state_path).get("attempts"):
            state["retry_after_ms"] = s.collector._now() + 60_000
            raise pq.SourceDeferred("wal_over_limit:after_admission")
        return original(state)
    monkeypatch.setattr(s.collector, "_wal_guard", grow)
    report = at(s)
    state = read(s.collector.state_path)
    assert report["provider_attempts"] == 0 and report["daily_admitted_attempts"] == 1
    assert state["retry_after_ms"] == T0 + 91_000 and not s.provider.calls


@pytest.mark.parametrize("mutation", ["manifest_hash", "source_hash", "paper_status", "book_identity", "future_tick", "future_entry", "foreign_order"])
def test_conflicting_frozen_identity_or_future_evidence_cannot_authorize_read(setup, mutation):
    s = setup
    manifest_path = s.research / (s.manifest["campaign_id"] + ".json")
    if mutation in ("manifest_hash", "source_hash", "paper_status"):
        value = read(manifest_path)
        if mutation == "manifest_hash":
            value["campaign_id"] = "changed"
        elif mutation == "source_hash":
            value["source_versions"]["execution/paper.py"] = "changed"
        else:
            value["hypotheses"][0]["status"] = "LIVE"
        write(manifest_path, value)
    elif mutation == "book_identity":
        mutate_books(s, "UPDATE candidate_meta SET value='{}' WHERE key='identity'")
    elif mutation == "future_tick":
        mutate_books(s, "UPDATE candidate_meta SET value=? WHERE key='last_tick_ms'", (str(T0 + 90_000),))
    elif mutation == "future_entry":
        mutate_books(s, "UPDATE candidate_entries SET registered_ms=?", (T0 + 90_000,))
    else:
        s.clock["ms"] = T0 + 31_000
        s.runner.run_candidates(s.manifest, [], {("sol", TOKEN): snapshot(s.clock["ms"])}, now_ms=s.clock["ms"])
        mutate_books(s, "UPDATE orders SET mode='live'")
    report = at(s)
    assert report["provider_attempts"] == 0 and not s.provider.calls
    assert report["status"] == "partial"


@pytest.mark.parametrize("mutation", ["missing_cache", "wrong_token", "wrong_chain", "future_receipt", "unavailable", "missing_depth", "wrong_digest", "wrong_pool"])
def test_provider_success_never_invents_verified_market_evidence(setup, mutation):
    s = setup
    if mutation == "missing_cache":
        s.provider.write_cache = False
    elif mutation == "wrong_token":
        s.provider.change_data["address"] = SECOND
    elif mutation == "wrong_chain":
        s.provider.change_data["chain"] = "bsc"
    elif mutation == "future_receipt":
        s.provider.receipt_changes["observed_at_ms"] = T0 + 32_000
    elif mutation == "unavailable":
        s.provider.available = False
    elif mutation == "missing_depth":
        s.provider.change_data["liquidity"] = None
    elif mutation == "wrong_pool":
        s.provider.change_data["pool"] = {"base_address": SECOND, "quote_address": "C" * 32}
    else:
        s.provider.receipt_changes["response_digest"] = "wrong"
    report = at(s)
    cache = report["attempt"]["cache"]
    assert report["daily_admitted_attempts"] == report["provider_attempts"] == 1
    if mutation == "missing_depth":
        assert cache["verified"] and not cache["market_fields_usable"]
    else:
        assert not cache["verified"]


def test_pool_address_and_opposite_base_constituent_are_not_token_conflicts(setup):
    s = setup
    s.provider.change_data["pool"] = {"address": "D" * 32, "base_address": SECOND, "quote_address": TOKEN}
    assert at(s)["attempt"]["cache"]["verified"]


def test_cached_receipt_retains_its_observation_clock_and_cannot_relabel_old_data(setup, monkeypatch):
    s = setup
    original = s.provider.token_info
    def captured_then_cached(*args, **kwargs):
        result = original(*args, **kwargs)
        result.receipt = result.receipt.model_copy(update={"basis": EvidenceBasis.CACHED})
        s.clock["ms"] += 9000
        return result
    monkeypatch.setattr(s.provider, "token_info", captured_then_cached)
    report = at(s)
    cache = report["attempt"]["cache"]
    assert cache["verified"] and cache["fetched_ms"] == T0 + 31_000
    assert cache["receipt"]["observed_at_ms"] == T0 + 31_000
    assert report["completed_ms"] == T0 + 40_000


def test_active_payload_cap_and_retired_book_exclusion(setup):
    s = setup
    # A valid old book not named by active state must not become a provider target.
    retired = s.research / "paper/candidate-retired.db"
    retired.write_bytes(b"not sqlite; opening this would fail")
    assert at(s)["status"] == "captured"
    assert not read(s.collector.state_dir / "latest-report.json")["book_errors"]
    mutate_books(s, "UPDATE candidate_entries SET payload=?", ("x" * 33_000,))
    report = at(s, 46_000)
    assert report["provider_attempts"] == 0 and report["status"] == "partial"
    assert len(s.provider.calls) == 1


def test_global_book_and_unique_target_caps_refuse_oversized_active_population(setup):
    s = setup
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    opportunities = [opportunity(letter + "A" * 31) for letter in alphabet[:33]]
    s.runner.run_candidates(s.manifest, opportunities, {}, now_ms=T0 + 1000)
    report = at(s)
    assert report["provider_attempts"] == 0 and "unique_target_cap" in report["reason"]
    excessive = generate_campaign([], ExitLadder(), now_ms=T0,
        groups=[(lane, chain) for lane in ("sm-trenches", "trusted-copy")
                for chain in ("sol", "bsc", "robinhood")], max_candidates=19)
    assert len(excessive["hypotheses"]) == 19
    name = excessive["campaign_id"] + ".json"
    write(s.research / name, excessive)
    write(s.research / "state.json", {"campaign_files": [name]})
    report = at(s, 32_000)
    assert report["provider_attempts"] == 0 and "active_book_cap" in report["reason"]
    assert not s.provider.calls


def test_symlinked_manifest_and_state_cannot_authorize_observation(setup, tmp_path):
    s = setup
    path = s.research / (s.manifest["campaign_id"] + ".json")
    outside = tmp_path / "foreign-manifest.json"
    outside.write_bytes(path.read_bytes())
    path.unlink()
    try:
        path.symlink_to(outside)
    except OSError:
        pytest.skip("host does not permit symlink fixtures")
    report = at(s)
    assert report["provider_attempts"] == 0 and "symlink" in report["reason"]
    assert not s.provider.calls


def test_real_typed_wrapper_one_spawn_writes_the_cache_without_any_network(setup, monkeypatch):
    s = setup
    actual = int(time.time() * 1000)
    s.clock["ms"] = actual
    # Recreate real books at a contemporaneous epoch, so Receipt's real clock is honest.
    manifest = generate_campaign([], ExitLadder(), now_ms=actual - 40_000,
        groups=[("sm-trenches", "sol")], max_candidates=2)
    name = manifest["campaign_id"] + ".json"
    write(s.research / name, manifest)
    write(s.research / "state.json", {"campaign_files": [name]})
    runner = CandidatePaperRunner(s.research / "paper", learning_root=s.root, max_stake_usd="30",
                                  clock=lambda: s.clock["ms"] / 1000)
    registered = runner.run_candidates(manifest, [opportunity(at=actual - 31_000)], {}, now_ms=actual)
    assert not registered["errors"]
    calls = []
    data = {"address": TOKEN, "decimals": 6, "liquidity": "1000000", "price": {"address": TOKEN, "price": "1"}}
    def spawn(argv, timeout_s):
        calls.append((argv, timeout_s))
        return gmgn_cli._Raw(0, json.dumps(data), "")
    monkeypatch.setattr(gmgn_cli, "cli_argv", lambda: ["offline-test-boundary"])
    monkeypatch.setattr(gmgn_cli, "_spawn", spawn)
    s.collector.provider, s.collector.clock = gmgn_cli, time.time
    # Cache time and actual provider receipt both use the current wall clock.
    monkeypatch.setattr(_http, "now_ms", lambda: int(time.time() * 1000))
    report = s.collector.once()
    assert report["status"] == "captured" and len(calls) == 1
    assert calls[0][1] == 6
    assert read(Path(report["attempt"]["cache"]["cache_path"]))["data"] == data
    assert s.db.execute("SELECT COUNT(*) FROM provider_calls WHERE provider='gmgn'").fetchone()[0] == 1
    assert s.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    # A later external positive cache is already available to the paper worker.
    # Even when the observer's own token cadence permits another call, it neither
    # invokes the typed wrapper nor charges another local admission for that cache.
    s.clock["ms"] = report["attempt"]["admitted_ms"] + 16_000
    s.collector.clock = lambda: s.clock["ms"] / 1000
    cached_market(s, fetched=s.clock["ms"] - 100, data=data)
    second = s.collector.once()
    assert second["reason"] == "fresh_canonical_cache_already_present" and second["provider_attempts"] == 0
    assert second["daily_admitted_attempts"] == 1 and len(calls) == 1


def test_live_pid_and_malformed_lock_never_capture_and_provably_dead_lock_can_recover(setup, monkeypatch):
    s = setup
    lock = s.collector.state_dir / "worker.lock"
    write(lock, {"pid": os.getpid()})
    assert at(s)["status"] == "locked"
    lock.write_text("malformed", encoding="utf-8")
    assert at(s)["status"] == "locked"
    write(lock, {"pid": 987654})
    monkeypatch.setattr(pq, "_pid_alive", lambda pid: False)
    assert at(s)["status"] == "captured"
    assert len(s.provider.calls) == 1 and not lock.exists()


def test_manifest_path_escape_and_book_hardlink_are_refused(setup, tmp_path):
    s = setup
    write(s.research / "state.json", {"campaign_files": ["../foreign.json"]})
    assert at(s)["provider_attempts"] == 0
    write(s.research / "state.json", {"campaign_files": [s.manifest["campaign_id"] + ".json"]})
    # Real book paths are deterministic; an arbitrary main DB hardlink cannot substitute.
    path = book(s)
    path.unlink()
    main = Path(s.db.execute("PRAGMA database_list").fetchone()[2])
    os.link(main, path)
    # The other valid arm still owns a genuine pending attempt, so refuse it too.
    with sqlite3.connect(book(s, 1)) as conn:
        conn.execute("UPDATE candidate_entries SET status='rejected'")
    report = at(s, 32_000)
    assert report["provider_attempts"] == 0 and report["book_errors"]


def test_empty_research_state_does_not_create_books_or_provider_calls(tmp_path):
    root = tmp_path / "learning"
    provider = Provider({"ms": T0})
    collector = pq.ProspectiveQuoteCollector(root / "research", root / "observer", learning_root=root,
                                           clock=lambda: T0 / 1000, provider=provider)
    report = collector.once()
    assert report["status"] == "idle" and not provider.calls
    assert not (root / "research").exists()
    assert read(collector.state_path)["days"] == {}


@pytest.mark.parametrize("opened", [False, True])
def test_observer_reads_exact_admission_clock_books_without_changing_legacy_identity(setup, opened):
    from kaiba.learning.admission_paper import AdmissionPaperRunner, generate_admission_campaign
    s = setup
    def identity(index):
        conn = sqlite3.connect(book(s, index))
        try:
            return conn.execute("SELECT value FROM candidate_meta WHERE key='identity'").fetchone()[0]
        finally:
            conn.close()
    legacy = [read(s.research / (s.manifest["campaign_id"] + ".json")),
              [identity(i) for i in range(2)]]
    manifest = generate_admission_campaign([], ExitLadder(), now_ms=T0,
        groups=[("sm-trenches", "sol")], max_candidates=2)
    name = manifest["campaign_id"] + ".json"
    write(s.research / name, manifest)
    write(s.research / "state.json", {"campaign_files": [name]})
    runner = AdmissionPaperRunner(s.research / "paper", learning_root=s.root,
        clock=lambda: s.clock["ms"] / 1000)
    assert not runner.run_candidates(manifest, [opportunity()], {}, now_ms=s.clock["ms"])["errors"]
    if opened:
        s.clock["ms"] = T0 + 81_000
        result = runner.run_candidates(manifest, [], {("sol", TOKEN): snapshot(T0 + 31_000)}, now_ms=s.clock["ms"])
        assert sum(arm["order_count"] for arm in result["arms"]) == 2
    report = s.collector.once() if opened else at(s)
    assert report["status"] == "captured" and len(s.provider.calls) == 1
    assert report["attempt"]["target"]["campaign_id"] == manifest["campaign_id"]
    assert report["attempt"]["target"]["kind"] == ("open" if opened else "pending")
    assert not report["book_errors"]
    assert read(s.research / (s.manifest["campaign_id"] + ".json")) == legacy[0]
    for i, expected in enumerate(legacy[1]):
        assert identity(i) == expected


def test_observer_refuses_admission_policy_tampering_even_with_rehashed_campaign(setup):
    from kaiba.learning.admission_paper import AdmissionPaperRunner, generate_admission_campaign
    from kaiba.learning.strategy_campaign import _campaign_id
    s = setup
    manifest = generate_admission_campaign([], ExitLadder(), now_ms=T0,
        groups=[("sm-trenches", "sol")], max_candidates=2)
    runner = AdmissionPaperRunner(s.research / "paper", learning_root=s.root,
        clock=lambda: s.clock["ms"] / 1000)
    runner.run_candidates(manifest, [opportunity()], {}, now_ms=s.clock["ms"])
    manifest["assessment_policy"]["execution_policy"]["hold_clock"] = "quote_clock"
    manifest["campaign_id"] = _campaign_id(manifest)
    name = manifest["campaign_id"] + ".json"
    write(s.research / name, manifest)
    write(s.research / "state.json", {"campaign_files": [name]})
    report = at(s)
    assert report["provider_attempts"] == 0 and report["book_errors"]
    assert not s.provider.calls
