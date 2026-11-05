"""Real short source reads and replay paths; partial discovery never reaches search."""

from __future__ import annotations

import dataclasses
import json
from collections import Counter
from pathlib import Path

import pytest

from kaiba.learning import autolearn_discovery as ad
from kaiba.learning import signal_audit as sa
from kaiba.learning import strategy_campaign as sc
from kaiba.learning import strategy_search as ss
from kaiba.learning.autolearn_io import GuardedReader, SourceDeferred

NOW = 1_800_000_000_000
GROUPS = [("sm-trenches", "sol")]


def _path(conn):
    return Path(conn.execute("PRAGMA database_list").fetchone()[2])


def _signal(conn, token, at, *, chain="sol", sid=None, payload=None, tape=True):
    sid = sid or f"s:{chain}:{token}:{at}"
    payload = payload or {"holder_count": {"value": 150, "basis": "provider_reported",
                                           "receipt": {"observed_at_ms": at - 1}}, "launchpad": "pump"}
    conn.execute("INSERT INTO signals(signal_id,lane,chain,token,created_ms,wallets_json,payload_json) "
                 "VALUES (?,?,?,?,?,?,?)", (sid, "sm-trenches", chain, token, at, '["wallet"]', json.dumps(payload)))
    if tape:
        for i in range(14):
            conn.execute("INSERT INTO swaps(chain,tx,ts_ms,wallet,token,side,price_usd,source) "
                         "VALUES (?,?,?,?,?,?,?,?)", (chain, sid + str(i), at - 30_000 + i * 60_000,
                                                      "wallet", token, "buy", str(1 + i * .05), "fixture-tape"))
    return sid


def _collect(reader, path, **kwargs):
    return ad.collect(reader, path, groups=GROUPS, ladder=sa.ExitLadder(), now_ms=NOW, **kwargs)


class ClockReader(GuardedReader):
    """SQL still runs against a real fixture DB; only the collector wall clock is accelerated."""
    def __init__(self, path, query_cost=.01):
        super().__init__(path)
        self.elapsed, self.query_cost, self.tape_tokens = 0., query_cost, []
        self.defer_token = None

    def _read(self, sql, args, *, dictionaries):
        self.elapsed += self.query_cost
        if "FROM swaps" in sql and "WHERE" in sql:
            if args[0] == self.defer_token:
                raise SourceDeferred("fixture_guard_deferred")
            self.tape_tokens.append(args[0])
        assert not any(table in sql for table in ("wallet_scores", "wallet_feed_tags", "x_token_obs"))
        assert "FROM wallets" not in sql
        return super()._read(sql, args, dictionaries=dictionaries)

    def progress_clock(self):
        return self.elapsed


def test_real_replay_completes_and_keeps_only_prospective_fields_not_raw_tape(tmp_db, tmp_path):
    at = NOW - ss.SETTLE_MS - 60_000
    _signal(tmp_db, "token", at)
    path = tmp_path / "work.json"
    result = _collect(ClockReader(_path(tmp_db)), path)
    assert result["status"] == "complete" and len(result["observations"]) == 1
    assert result["counts"] == {"considered": 1, "processed": 1, "priced": 1, "no_tape": 0,
                                 "invalid_prices": 0, "frozen_groups": 1, "groups": 1}
    obs = result["observations"][0]
    assert obs.features == {"holder_count": 150, "launchpad": "pump"}
    assert len(obs.raw) >= 9 and all(isinstance(value, float) for value in obs.raw.values())
    work = json.loads(path.read_text())
    assert not any(key in path.read_text() for key in ("price_usd", "wallet_scores", "receipt", "payload_json"))
    assert work["source_rowid_frontier"] == 1 and path.stat().st_size < ad.MAX_WORK_BYTES
    again = _collect(ClockReader(_path(tmp_db)), path)
    assert dataclasses.asdict(again["observations"][0]) == dataclasses.asdict(obs)


def test_tiny_budgets_resume_original_cursor_and_equal_one_shot_replay(tmp_db, tmp_path):
    at = NOW - ss.SETTLE_MS - 10_000_000
    for i in range(12):
        _signal(tmp_db, f"token{i}", at + i * 60_000)
    reader = ClockReader(_path(tmp_db))
    path = tmp_path / "resumed.json"
    seen_cursors, result = [], None
    for _ in range(30):
        result = _collect(reader, path, budget_seconds=.06, clock=reader.progress_clock)
        work = json.loads(path.read_text())
        seen_cursors.append(work["cursor"])
        if result["status"] == "complete":
            break
        assert result["observations"] == [] and result["funding_allowed"] is False
    assert result["status"] == "complete" and len(result["observations"]) == 12
    assert seen_cursors == sorted(seen_cursors) and len(seen_cursors) > 2
    assert Counter(reader.tape_tokens) == Counter({f"token{i}": 1 for i in range(12)})
    full = _collect(ClockReader(_path(tmp_db)), tmp_path / "full.json")
    assert [dataclasses.asdict(o) for o in result["observations"]] == [dataclasses.asdict(o) for o in full["observations"]]
    assert result["roster_hash"] == full["roster_hash"]


def test_source_deferral_does_not_advance_or_release_a_partial_sample(tmp_db, tmp_path):
    for i in range(3):
        _signal(tmp_db, f"token{i}", NOW - ss.SETTLE_MS - 10_000 + i * 100)
    reader = ClockReader(_path(tmp_db))
    reader.defer_token = "token1"
    path = tmp_path / "work.json"
    result = _collect(reader, path)
    assert result["status"] == "collecting" and result["observations"] == []
    assert result["reason"].startswith("source_deferred:")
    assert json.loads(path.read_text())["cursor"] == 1
    reader.defer_token = None
    result = _collect(reader, path)
    assert result["status"] == "complete" and result["counts"]["processed"] == 3
    assert Counter(reader.tape_tokens) == Counter({f"token{i}": 1 for i in range(3)})


def test_equal_group_caps_oldest_rowid_dedupe_and_original_anchor_ignore_late_backfills(tmp_db, tmp_path):
    at = NOW - ss.SETTLE_MS - 10_000_000
    groups = [("sm-trenches", chain) for chain in ("sol", "bsc", "robinhood")]
    for chain in ("sol", "bsc", "robinhood"):
        for i in range(5):
            _signal(tmp_db, f"{chain}{i}", at + i * 1000, chain=chain)
    first = _signal(tmp_db, "dedup", at + 20_000, chain="sol")
    _signal(tmp_db, "dedup", at + 19_000, chain="sol", sid="later-row-earlier-time", tape=False)
    reader = ClockReader(_path(tmp_db))
    path = tmp_path / "work.json"
    result = ad.collect(reader, path, groups=groups, ladder=sa.ExitLadder(), now_ms=NOW,
                        signal_cap=6, budget_seconds=.025, clock=reader.progress_clock)
    assert result["status"] == "collecting"
    _signal(tmp_db, "latebackfill", at + 30_000, chain="sol")
    _signal(tmp_db, "future", NOW, chain="sol")
    result = ad.collect(reader, path, groups=groups, ladder=sa.ExitLadder(), now_ms=NOW + 30_000,
                        signal_cap=6)
    assert result["status"] == "complete"
    assert Counter(o.chain for o in result["observations"]) == Counter(sol=2, bsc=2, robinhood=2)
    roster = json.loads(path.read_text())["roster"]
    assert any(r["signal_id"] == first for r in roster)
    assert not any(r["signal_id"] == "later-row-earlier-time" or r["token"] in ("latebackfill", "future") for r in roster)
    assert result["anchor_ms"] == NOW


def test_measure_unwrapping_rejects_future_unavailable_estimated_and_non_scalar_payloads(tmp_db, tmp_path):
    at = NOW - ss.SETTLE_MS - 10_000
    payload = {"holder_count": {"value": 70, "receipt": {"observed_at_ms": at - 1}},
               "liquidity_usd": {"value": 999, "receipt": {"observed_at_ms": at + 1}},
               "top10_pct": {"value": 99, "basis": "unavailable"},
               "bundler_pct": {"value": 99, "receipt": {"observed_at_ms": at, "basis": "estimated"}},
               "market_cap_usd": {"value": 100, "receipt": {}}, "token_age_s": 123,
               "smart_wallets_sold": {"value": [3]}, "gradeAB_now": 10, "x_posts": 500}
    _signal(tmp_db, "token", at, payload=payload)
    result = _collect(ClockReader(_path(tmp_db)), tmp_path / "work.json")
    assert result["observations"][0].features == {"holder_count": 70, "token_age_s": 123}


@pytest.mark.parametrize("mutation", ["delete", "payload"])
def test_deleted_or_changed_already_priced_signal_rejects_entire_frozen_sample(tmp_db, tmp_path, mutation):
    for i in range(3):
        _signal(tmp_db, f"token{i}", NOW - ss.SETTLE_MS - 100_000 + i * 100)
    reader = ClockReader(_path(tmp_db))
    reader.defer_token = "token1"
    path = tmp_path / "work.json"
    _collect(reader, path)
    before = path.read_bytes()
    work = json.loads(before)
    rid = work["roster"][0]["rowid"]
    if mutation == "delete":
        tmp_db.execute("DELETE FROM signals WHERE rowid=?", (rid,))
    else:
        tmp_db.execute("UPDATE signals SET payload_json='{}' WHERE rowid=?", (rid,))
    reader.defer_token = None
    result = _collect(reader, path)
    assert result["terminal"] and result["status"] == "collecting" and result["observations"] == []
    assert result["error"] == "frozen_discovery_signal_deleted_or_changed"
    assert path.read_bytes() == before


def test_advanced_retention_rejects_work_without_erasing_partial_evidence(tmp_db, tmp_path):
    for i in range(3):
        _signal(tmp_db, f"token{i}", NOW - ss.SETTLE_MS - 100_000 + i * 100)
    reader = ClockReader(_path(tmp_db))
    reader.defer_token = "token1"
    path = tmp_path / "work.json"
    _collect(reader, path)
    before = path.read_bytes()
    oldest = min(r["t0"] for r in json.loads(before)["roster"])
    tmp_db.execute("INSERT INTO kv(key,value,updated_ms) VALUES (?,?,?)",
                   ("retention:swaps:horizon_ms", str(oldest + 1), NOW))
    result = _collect(reader, path)
    assert result["error"] == "frozen_discovery_roster_expired_by_retention"
    assert result["observations"] == [] and path.read_bytes() == before


def test_changed_code_ladder_or_group_policy_never_restarts_partial_work(tmp_db, tmp_path, monkeypatch):
    _signal(tmp_db, "token", NOW - ss.SETTLE_MS - 10_000)
    path = tmp_path / "work.json"
    reader = ClockReader(_path(tmp_db))
    _collect(reader, path)
    before = path.read_bytes()
    changed = ad.collect(reader, path, groups=GROUPS, ladder=dataclasses.replace(sa.ExitLadder(), stop_frac=.2), now_ms=NOW)
    assert changed["terminal"] and changed["observations"] == [] and path.read_bytes() == before
    monkeypatch.setattr(ad, "_versions", lambda: {"code": "changed"})
    changed = _collect(reader, path)
    assert changed["error"] == "frozen_discovery_code_or_policy_changed" and path.read_bytes() == before


def test_work_progress_corruption_is_rejected_and_complete_empty_or_missing_tape_is_honest(tmp_db, tmp_path):
    empty = _collect(ClockReader(_path(tmp_db)), tmp_path / "empty.json")
    assert empty["status"] == "complete" and empty["counts"]["considered"] == 0
    _signal(tmp_db, "token", NOW - ss.SETTLE_MS - 10_000, tape=False)
    path = tmp_path / "missing.json"
    result = _collect(ClockReader(_path(tmp_db)), path)
    assert result["status"] == "complete" and result["observations"] == [] and result["counts"]["no_tape"] == 1
    work = json.loads(path.read_text())
    work["cursor"] = 0
    path.write_text(json.dumps(work))
    corrupt = path.read_bytes()
    result = _collect(ClockReader(_path(tmp_db)), path)
    assert result["error"] == "frozen_discovery_progress_corrupt" and path.read_bytes() == corrupt


def test_retention_and_settlement_boundaries_exclude_out_of_window_signals(tmp_db, tmp_path):
    horizon = NOW - 2 * ad.DAY_MS
    tmp_db.execute("INSERT INTO kv(key,value,updated_ms) VALUES (?,?,?)",
                   ("retention:swaps:horizon_ms", str(horizon), NOW))
    _signal(tmp_db, "before", horizon - 1)
    _signal(tmp_db, "boundary", horizon)
    _signal(tmp_db, "settled", NOW - ss.SETTLE_MS)
    _signal(tmp_db, "unsettled", NOW - ss.SETTLE_MS + 1)
    result = _collect(ClockReader(_path(tmp_db)), tmp_path / "work.json")
    assert {o.token for o in result["observations"]} == {"boundary", "settled"}
    assert result["window_start_ms"] == horizon


def test_complete_sample_is_the_only_input_eligible_for_existing_factory(tmp_db, tmp_path):
    _signal(tmp_db, "token", NOW - ss.SETTLE_MS - 10_000)
    path = tmp_path / "work.json"
    reader = ClockReader(_path(tmp_db))
    pending = _collect(reader, path, budget_seconds=.025, clock=reader.progress_clock)
    assert pending["observations"] == []
    completed = _collect(reader, path)
    campaign = sc.generate_campaign(completed["observations"], sa.ExitLadder(), now_ms=NOW, groups=GROUPS,
                                    max_candidates=2, trained_only=True)
    assert completed["status"] == "complete" and campaign["observation_count"] == 1
    assert campaign["hypotheses"] == []  # a completed collection is not evidence of profit


@pytest.mark.parametrize("kwargs", [{"signal_cap": 0}, {"signal_cap": 751}, {"signal_cap": True},
                                    {"budget_seconds": 0}, {"budget_seconds": 31},
                                    {"budget_seconds": float("nan")}])
def test_invalid_capacity_or_deadline_cannot_create_work(tmp_db, tmp_path, kwargs):
    path = tmp_path / "invalid.json"
    with pytest.raises(ValueError):
        _collect(ClockReader(_path(tmp_db)), path, **kwargs)
    assert not path.exists()


def test_wal_deferral_preserves_anchor_before_any_roster_is_admitted(tmp_db, tmp_path, monkeypatch):
    from kaiba.learning.autolearn_io import WAL_LIMIT
    reader = ClockReader(_path(tmp_db))
    monkeypatch.setattr(reader, "_wal_bytes", lambda: WAL_LIMIT + 1)
    path = tmp_path / "work.json"
    result = _collect(reader, path)
    assert result["status"] == "collecting" and result["reason"].startswith("source_deferred:wal_over_limit")
    assert result["counts"]["processed"] == 0 and reader.queries == 0
    assert json.loads(path.read_text())["anchor_ms"] == NOW
    monkeypatch.setattr(reader, "_wal_bytes", lambda: 0)
    completed = ad.collect(reader, path, groups=GROUPS, ladder=sa.ExitLadder(), now_ms=NOW + 60_000)
    assert completed["status"] == "complete" and completed["anchor_ms"] == NOW


def test_frozen_roster_ages_beyond_13_day_swap_reader_ceiling_before_retention_expires(tmp_db, tmp_path):
    at = NOW - 12 * ad.DAY_MS
    tmp_db.execute("INSERT INTO kv(key,value,updated_ms) VALUES (?,?,?)",
                   ("retention:swaps:horizon_ms", str(NOW - 14 * ad.DAY_MS), NOW))
    _signal(tmp_db, "oldest", at)
    reader = ClockReader(_path(tmp_db))
    reader.defer_token = "oldest"
    path = tmp_path / "work.json"
    pending = _collect(reader, path)
    assert pending["status"] == "collecting" and pending["counts"]["considered"] == 1
    assert reader.tape_tokens == []
    before = path.read_bytes()
    prior_queries = reader.queries
    reader.defer_token = None
    expired = ad.collect(reader, path, groups=GROUPS, ladder=sa.ExitLadder(),
                         now_ms=NOW + ad.DAY_MS + sa.PRE_WINDOW_MS)
    assert expired["terminal"] and expired["status"] == "collecting"
    assert expired["error"] == "frozen_discovery_history_exceeds_13_days"
    assert expired["observations"] == [] and expired["funding_allowed"] is False
    assert reader.tape_tokens == [] and reader.queries == prior_queries
    assert path.read_bytes() == before


def test_exact_paging_finishes_dense_signal_that_previous_full_query_cannot_read(tmp_db, tmp_path):
    for i in range(3):
        _signal(tmp_db, f"token{i}", NOW - ss.SETTLE_MS - 10_000 + i * 100)
    # Each real tape has14rows; a query-wide12-row guard previously pinned the
    # first signal forever. Paging keeps the guard and includes all14originalrows.
    reader = ClockReader(_path(tmp_db))
    reader.max_rows = 12
    with pytest.raises(SourceDeferred, match="source_row_cap"):
        GuardedReader(_path(tmp_db), max_rows=12).q(
            "SELECT ts_ms,price_usd,source,wallet,side FROM swaps WHERE token=? AND chain=? "
            "AND ts_ms BETWEEN ? AND ?", ("token2", "sol", NOW - ad.DAY_MS, NOW))
    paged = _collect(reader, tmp_path / "paged.json")
    full = _collect(ClockReader(_path(tmp_db)), tmp_path / "full.json")
    assert paged["status"] == "complete" and paged["counts"]["processed"] == 3
    assert [dataclasses.asdict(o) for o in paged["observations"]] == [dataclasses.asdict(o) for o in full["observations"]]
    assert Counter(reader.tape_tokens) == Counter(token0=2, token1=2, token2=2)


def test_paging_preserves_tied_timestamps_and_uses_the_existing_token_index(tmp_db, tmp_path):
    at = NOW - ss.SETTLE_MS - 10_000
    _signal(tmp_db, "token", at)
    tmp_db.execute("UPDATE swaps SET ts_ms=? WHERE token='token'", (at + 30_000,))

    class RecordingReader(ClockReader):
        statements = []

        def q(self, sql, args=()):
            if "FROM swaps" in sql:
                self.statements.append((sql, args))
            return super().q(sql, args)

    reader = RecordingReader(_path(tmp_db))
    reader.max_rows = 3
    result = _collect(reader, tmp_path / "work.json")
    full = _collect(ClockReader(_path(tmp_db)), tmp_path / "full.json")
    assert result["status"] == "complete" and result["observations"][0].raw == full["observations"][0].raw
    assert len(reader.statements) == 5
    sql, args = reader.statements[-1]
    plan = " ".join(row[3] for row in tmp_db.execute("EXPLAIN QUERY PLAN " + sql, args))
    assert "idx_swaps_token" in plan and "TEMP B-TREE" not in plan
    # Explicitly prove every tied row was retained, rather than repeatedly reading
    # one page or skipping all remaining rows with the same timestamp.
    rows = ad._tape(reader, {"token": "token", "chain": "sol", "t0": at}, 10_000,
                    deadline=reader.progress_clock() + 1, clock=reader.progress_clock, progress={})
    assert len(rows) == 14 and [row[1] for row in rows] == [str(1 + i * .05) for i in range(14)]


def test_frozen_swap_rowid_frontier_excludes_late_historical_backfills(tmp_db, tmp_path):
    at = NOW - ss.SETTLE_MS - 10_000
    _signal(tmp_db, "token", at)
    baseline = _collect(ClockReader(_path(tmp_db)), tmp_path / "baseline.json")
    reader = ClockReader(_path(tmp_db))
    reader.defer_token = "token"
    path = tmp_path / "frozen.json"
    deferred = _collect(reader, path)
    assert deferred["status"] == "collecting"
    for i in range(20):
        tmp_db.execute("INSERT INTO swaps(chain,tx,ts_ms,wallet,token,side,price_usd,source) "
                       "VALUES (?,?,?,?,?,?,?,?)", ("sol", f"late{i}", at - 10_000 + i * 10_000,
                                                    "wallet", "token", "buy", str(10000 + i), "late-source"))
    reader.defer_token = None
    resumed = _collect(reader, path)
    assert resumed["status"] == "complete"
    assert resumed["observations"][0].raw == baseline["observations"][0].raw
    fresh = _collect(ClockReader(_path(tmp_db)), tmp_path / "fresh.json")
    assert fresh["observations"][0].raw != resumed["observations"][0].raw


def test_three_nonprogressing_tape_attempts_are_actionable_unproven_not_infinite_deferral(tmp_db, tmp_path):
    _signal(tmp_db, "token", NOW - ss.SETTLE_MS - 10_000)
    reader = ClockReader(_path(tmp_db))
    reader.defer_token = "token"
    path = tmp_path / "work.json"
    for attempt in range(3):
        result = _collect(reader, path)
        assert result["observations"] == [] and result["counts"]["processed"] == 0
        assert result["current_signal"]["attempt"]["stalled_attempts"] == attempt + 1
        assert result["terminal"] == (attempt == 2)
    assert result["error"] == "frozen_discovery_exact_tape_stalled_after_3_attempts"
    assert json.loads(path.read_text())["observations"] == []


@pytest.mark.parametrize("bound,value", [("MAX_TAPE_ROWS", 12), ("MAX_TAPE_PAYLOAD_BYTES", 10)])
def test_exact_tape_resource_cap_rejects_whole_sample_without_omitting_dense_token(tmp_db, tmp_path, monkeypatch, bound, value):
    _signal(tmp_db, "token", NOW - ss.SETTLE_MS - 10_000)
    monkeypatch.setattr(ad, bound, value)
    result = _collect(ClockReader(_path(tmp_db)), tmp_path / "work.json")
    assert result["terminal"] and result["error"] == "frozen_discovery_exact_tape_resource_limit"
    assert result["observations"] == [] and result["counts"]["processed"] == 0
    assert result["counts"]["no_tape"] == 0 and result["current_signal"]["attempt"]["resource_limit"]
