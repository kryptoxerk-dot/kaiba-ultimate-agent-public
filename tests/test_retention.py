"""Row retention (kaiba/ops/retention.py) and its scheduler wiring.

The tests that carry weight, each written as the failure it prevents:

* disabled, or a dry run, deletes NOTHING -- and the dry run works on a read-only
  connection, so it provably writes nothing either;
* no delete transaction holds more than ``batch_rows`` rows, a slow one halves the batch,
  and a lock that stays busy ends the run instead of queuing behind protection;
* the history prune keeps every grade transition and each wallet's latest row, so the
  as-of lookup the readers use returns the same grade for EVERY instant before and after;
* the shipped config ships it off twice (job and switch), as a long job.
"""

from __future__ import annotations

import json
import math
import random
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from kaiba.core import db as core_db
from kaiba.core.db import jdump
from kaiba.core.schemas import now_ms
from kaiba.ops import retention as RT
from kaiba.ops import scheduler as S

ROOT = Path(__file__).resolve().parents[1]
DAY = RT.DAY_MS
NOW = 1_790_000_000_000


def _cfg(**kw: Any) -> RT.RetentionConfig:
    base = {"enabled": True, "sleep_s": 0, "budget_s": 600}
    base.update(kw)
    return RT.RetentionConfig(**base)


def _calls(conn, ages_days: list[float]) -> None:
    for i, age in enumerate(ages_days):
        conn.execute(
            "INSERT INTO provider_calls (provider, endpoint, ts_ms, status) VALUES (?,?,?,?)",
            (f"p{i % 3}", "x", int(NOW - age * DAY), "ok"),
        )


def _triage(conn, ages_days: list[float]) -> None:
    for i, age in enumerate(ages_days):
        conn.execute(
            "INSERT INTO triage_decisions (ts_ms, chain, token, verdict) VALUES (?,?,?,?)",
            (int(NOW - age * DAY), "sol", f"T{i}", "reject"),
        )


def _hist(conn, address: str, rows: list[tuple[float, str, float, str]], chain: str = "sol") -> None:
    """rows: (age_days, grade, score, model)."""
    for age, grade, score, model in rows:
        conn.execute(
            "INSERT INTO wallet_score_history (chain, address, score, grade, scored_at_ms, model_version) "
            "VALUES (?,?,?,?,?,?)",
            (chain, address, score, grade, int(NOW - age * DAY), model),
        )


def _count(conn, table: str) -> int:
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def _seed_all(conn) -> dict[str, int]:
    _calls(conn, [20, 19, 18, 17, 16, 15, 3, 2, 1, 0])
    _triage(conn, [30, 21, 15, 1, 0])
    _hist(conn, "A" * 44, [(20, "C", 30.0, "m"), (19, "C", 30.1, "m"), (18, "C", 30.2, "m"), (1, "C", 30.3, "m")])
    return {t: _count(conn, t) for t in ("provider_calls", "triage_decisions", "wallet_score_history")}


def _as_of(conn, chain: str, address: str, t: int) -> tuple[str, float, str] | None:
    row = conn.execute(
        "SELECT grade, score, model_version FROM wallet_score_history WHERE chain=? AND address=? "
        "AND scored_at_ms <= ? ORDER BY scored_at_ms DESC, id DESC LIMIT 1",
        (chain, address, t),
    ).fetchone()
    return (row[0], row[1], row[2]) if row else None


# --------------------------------------------------------------------------- the gate


def test_disabled_deletes_nothing(tmp_db):
    before = _seed_all(tmp_db)
    changes = tmp_db.total_changes
    report = RT.run(tmp_db, _cfg(enabled=False), dry_run=False, now_ms=NOW)
    after = {t: _count(tmp_db, t) for t in before}
    assert after == before
    assert tmp_db.total_changes == changes, "a disabled run wrote something (a cursor, a row)"
    assert report["mode"] == "dry_run" and report["deleted"] == 0
    assert report["reason"] == "retention.enabled is false"
    # ...and it still says what it WOULD delete, which is the point of running it disabled
    assert report["tables"]["provider_calls"]["rows_older_est"] == 6
    assert report["tables"]["triage_decisions"]["rows_older_est"] == 3


def test_a_dry_run_deletes_nothing_even_when_enabled(tmp_db):
    before = _seed_all(tmp_db)
    changes = tmp_db.total_changes
    report = RT.run(tmp_db, _cfg(), dry_run=True, now_ms=NOW)
    assert {t: _count(tmp_db, t) for t in before} == before
    assert tmp_db.total_changes == changes, "a dry run wrote something"
    assert report["mode"] == "dry_run" and report["reason"] == "dry_run requested"


def test_the_dry_run_needs_nothing_but_a_read_only_connection(tmp_db, tmp_path):
    _seed_all(tmp_db)
    tmp_db.execute(
        "INSERT INTO kv (key, value, updated_ms) VALUES (?,?,?)",
        (RT.CURSOR_KEY, json.dumps({"chain": "sol", "address": "A"}), NOW),
    )
    ro = RT._ro_connect(tmp_path / "kaiba.db")
    try:
        report = RT.run(ro, _cfg(sample_rows=50), dry_run=True, now_ms=NOW, rng=random.Random(1))
    finally:
        ro.close()
    assert report["mode"] == "dry_run"
    assert report["tables"]["wallet_score_history"]["sampled"] > 0


def test_enabled_deletes_only_rows_older_than_the_window(tmp_db):
    _seed_all(tmp_db)
    # an out-of-order row: low id, new timestamp -- must survive
    tmp_db.execute("UPDATE provider_calls SET ts_ms = ? WHERE id = 2", (NOW,))
    report = RT.run(tmp_db, _cfg(), dry_run=False, now_ms=NOW)
    assert report["mode"] == "delete"
    ages = [round((NOW - r[0]) / DAY) for r in tmp_db.execute("SELECT ts_ms FROM provider_calls ORDER BY id")]
    assert ages == [0, 3, 2, 1, 0], ages
    assert sorted(r[0] for r in tmp_db.execute("SELECT token FROM triage_decisions")) == ["T3", "T4"]
    assert report["tables"]["provider_calls"]["deleted"] == 5
    assert report["tables"]["triage_decisions"]["deleted"] == 3
    assert report["deleted"] == 5 + 3 + report["tables"]["wallet_score_history"]["deleted"]


# --------------------------------------------------------------------------- pacing


def test_no_delete_transaction_holds_more_than_batch_rows(tmp_db):
    _calls(tmp_db, [30.0] * 10 + [0.0] * 2)
    begins: list[str] = []
    tmp_db.set_trace_callback(lambda s: begins.append(s) if s.startswith("BEGIN") else None)
    try:
        report = RT.run(tmp_db, _cfg(batch_rows=3, max_tx_ms=60_000), dry_run=False, now_ms=NOW)
    finally:
        tmp_db.set_trace_callback(None)
    assert report["tables"]["provider_calls"]["deleted"] == 10
    assert 1 <= report["max_rows_per_tx"] <= 3
    assert report["transactions"] >= math.ceil(10 / 3)
    assert len(begins) >= math.ceil(10 / 3)
    assert _count(tmp_db, "provider_calls") == 2


def test_the_config_cannot_raise_a_batch_past_the_hard_cap():
    with pytest.raises(ValueError):
        RT.RetentionConfig(batch_rows=RT.MAX_BATCH_ROWS + 1)


def test_a_slow_transaction_halves_the_batch_and_a_fast_one_grows_it_back(tmp_db):
    ticks = iter([0.0, 5.0, 10.0, 10.0001, 20.0, 20.0001])  # 5 s, then two instant ones
    pacer = RT._Pacer(_cfg(batch_rows=1000, max_tx_ms=1000), NOW + 10**9, lambda: NOW / 1000,
                      lambda s: None, timer=lambda: next(ticks))
    assert pacer.delete(tmp_db, lambda c: 0) == 0
    assert pacer.batch == 500 and pacer.slow_tx == 1
    pacer.delete(tmp_db, lambda c: 0)
    assert pacer.batch == 750
    pacer.delete(tmp_db, lambda c: 0)
    assert pacer.batch == 1000, "never past batch_rows"


def test_a_busy_lock_backs_off_and_three_in_a_row_end_the_run(tmp_db, tmp_path):
    _calls(tmp_db, [30.0] * 5)
    holder = core_db.connect(tmp_path / "kaiba.db")
    holder.execute("BEGIN IMMEDIATE")  # another writer holds the lock throughout
    tmp_db.execute("PRAGMA busy_timeout=20")
    slept: list[float] = []
    try:
        report = RT.run(tmp_db, _cfg(sleep_s=0.1), dry_run=False, now_ms=NOW, sleep=slept.append,
                        deadline_ms=int(time.time() * 1000) + 10_000)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
        tmp_db.execute("PRAGMA busy_timeout=10000")
    assert report["stopped"] == "locked"
    assert report["lock_refusals"] == RT.LOCK_FAILS_TO_STOP
    assert report["deleted"] == 0 and _count(tmp_db, "provider_calls") == 5
    assert len(slept) == RT.LOCK_FAILS_TO_STOP - 1 and all(s >= 1.0 for s in slept)


def test_a_spent_budget_deletes_nothing(tmp_db):
    before = _seed_all(tmp_db)
    report = RT.run(tmp_db, _cfg(), dry_run=False, now_ms=NOW, deadline_ms=NOW - 1, clock=lambda: NOW / 1000)
    assert report["stopped"] == "budget" and report["deleted"] == 0
    assert {t: _count(tmp_db, t) for t in before} == before


def test_it_refuses_to_run_inside_a_callers_transaction(tmp_db):
    tmp_db.execute("BEGIN")
    try:
        with pytest.raises(RuntimeError):
            RT.run(tmp_db, _cfg(), dry_run=False, now_ms=NOW)
    finally:
        tmp_db.execute("ROLLBACK")


# --------------------------------------------------------------------------- the history rule


def test_the_history_prune_keeps_transitions_the_latest_row_and_the_window(tmp_db):
    a, b = "A" * 44, "B" * 44
    _hist(tmp_db, a, [
        (30, "C", 30.0, "m1"),  # first: kept
        (29, "C", 30.4, "m1"),  # drop
        (28, "C", 30.9, "m1"),  # drop (0.9 from the KEPT 30.0)
        (27, "C", 31.2, "m1"),  # kept: 1.2 from 30.0
        (26, "B", 31.2, "m1"),  # kept: letter
        (25, "B", 31.3, "m1"),  # drop
        (24, "B", 31.3, "m2"),  # kept: model
        (23, "B", 31.4, "m2"),  # drop
        (5, "B", 31.4, "m2"),   # inside the 14-day window: kept
        (4, "B", 31.4, "m2"),   # inside the window: kept
    ])
    _hist(tmp_db, b, [(30, "D", 10.0, "m1"), (29, "D", 10.1, "m1"), (28, "D", 10.2, "m1")])  # last kept
    RT.run(tmp_db, _cfg(), dry_run=False, now_ms=NOW)

    def left(addr: str) -> list[tuple[int, str, float, str]]:
        return [
            (round((NOW - r[0]) / DAY), r[1], r[2], r[3])
            for r in tmp_db.execute(
                "SELECT scored_at_ms, grade, score, model_version FROM wallet_score_history "
                "WHERE address=? ORDER BY scored_at_ms", (addr,))
        ]

    assert left(a) == [
        (30, "C", 30.0, "m1"), (27, "C", 31.2, "m1"), (26, "B", 31.2, "m1"),
        (24, "B", 31.3, "m2"), (5, "B", 31.4, "m2"), (4, "B", 31.4, "m2"),
    ]
    assert left(b) == [(30, "D", 10.0, "m1"), (28, "D", 10.2, "m1")]


def _random_history(conn, rng: random.Random, wallets: int, rows: int) -> list[tuple[str, str]]:
    """Random walks for ``wallets`` wallets, inserted in global time order as production
    writes them (so ids grow with time across wallets, which id_boundary assumes)."""
    keys = []
    pending: list[tuple[float, str, str, str, float, str]] = []
    for w in range(wallets):
        chain = rng.choice(["sol", "bsc", "robinhood"])
        address = f"{w:04d}" + "q" * 40
        keys.append((chain, address))
        score, grade, model = rng.uniform(10, 60), rng.choice("ABCD"), "m1"
        for age in sorted((rng.uniform(0, 40) for _ in range(rows)), reverse=True):
            score += rng.uniform(-0.6, 0.6)  # slow drift: the case a "vs previous row" rule breaks
            if rng.random() < 0.08:
                grade = rng.choice("ABCD")
            if rng.random() < 0.03:
                model = "m2" if model == "m1" else "m1"
            pending.append((age, chain, address, grade, round(score, 3), model))
    for age, chain, address, grade, score, model in sorted(pending, key=lambda r: -r[0]):
        _hist(conn, address, [(age, grade, score, model)], chain=chain)
    return keys


def test_the_as_of_lookup_answers_the_same_grade_at_every_instant(tmp_db):
    rng = random.Random(11)
    keys = _random_history(tmp_db, rng, wallets=40, rows=30)
    instants: dict[tuple[str, str], list[int]] = {}
    for k in keys:
        times = [int(r[0]) for r in tmp_db.execute(
            "SELECT scored_at_ms FROM wallet_score_history WHERE chain=? AND address=?", k)]
        instants[k] = sorted({x + d for x in times for d in (-1, 0, 1)})
    before = {(k, t): _as_of(tmp_db, *k, t) for k in keys for t in instants[k]}
    n0 = _count(tmp_db, "wallet_score_history")

    report = RT.run(tmp_db, _cfg(history_scan_rows=50), dry_run=False, now_ms=NOW)

    n1 = _count(tmp_db, "wallet_score_history")
    assert report["tables"]["wallet_score_history"]["deleted"] == n0 - n1 > n0 // 5, "vacuous"
    for (k, t), was in before.items():
        now = _as_of(tmp_db, *k, t)
        if was is None:
            assert now is None
            continue
        assert now is not None and now[0] == was[0] and now[2] == was[2], (k, t, was, now)
        assert abs(now[1] - was[1]) < RT.HISTORY_MIN_SCORE_DELTA, (k, t, was, now)
    for k in keys:  # the latest row of every wallet survives, exactly
        assert _as_of(tmp_db, *k, NOW + 1) == before[(k, instants[k][-1])]


def test_the_walk_resumes_across_runs_and_converges_to_the_one_shot_answer(tmp_db, tmp_path):
    rng = random.Random(5)
    _random_history(tmp_db, rng, wallets=12, rows=8)
    _hist(tmp_db, "Z" * 44, [(40 - i * 0.5, "C", 30.0 + i * 0.01, "m1") for i in range(25)])  # > a scan step
    expected: set[int] = set()
    for chain, address in {(r[0], r[1]) for r in tmp_db.execute(
            "SELECT DISTINCT chain, address FROM wallet_score_history")}:
        expected |= set(RT.deletable_history_ids(RT._wallet_rows(tmp_db, chain, address), NOW - 14 * DAY))
    survivors = {r[0] for r in tmp_db.execute("SELECT id FROM wallet_score_history")} - expected

    clock = {"t": NOW / 1000}

    def tick() -> float:
        clock["t"] += 1.0
        return clock["t"]

    complete = False
    for _ in range(200):
        # Each run gets ~15 ticks of the fake clock: a step or three. The cutoff stays NOW.
        report = RT.run(tmp_db, _cfg(history_scan_rows=10, history_pass_interval_s=0),
                        dry_run=False, now_ms=NOW, deadline_ms=int(clock["t"] * 1000) + 15_000,
                        clock=tick)
        if report["tables"]["wallet_score_history"].get("pass_complete"):
            complete = True
            break
    assert complete, "the walk never finished"
    assert {r[0] for r in tmp_db.execute("SELECT id FROM wallet_score_history")} == survivors
    state = json.loads(tmp_db.execute("SELECT value FROM kv WHERE key=?", (RT.CURSOR_KEY,)).fetchone()[0])
    assert state["chain"] is None and state["last_pass_done_ms"]


def test_a_finished_pass_waits_out_its_interval(tmp_db):
    _hist(tmp_db, "A" * 44, [(30, "C", 30.0, "m"), (29, "C", 30.0, "m"), (28, "C", 30.0, "m")])
    first = RT.run(tmp_db, _cfg(), dry_run=False, now_ms=NOW, clock=lambda: NOW / 1000)
    assert first["tables"]["wallet_score_history"]["pass_complete"] is True
    second = RT.run(tmp_db, _cfg(), dry_run=False, now_ms=NOW, clock=lambda: NOW / 1000 + 60)
    assert second["tables"]["wallet_score_history"]["skipped"] == "pass_interval"


# --------------------------------------------------------------------------- estimates


def test_id_boundary_is_a_handful_of_seeks_and_survives_gaps(tmp_db):
    _calls(tmp_db, [40 - i * 0.01 for i in range(3000)])  # ages 40 .. ~10 days
    tmp_db.execute("DELETE FROM provider_calls WHERE id BETWEEN 1200 AND 1900")
    cutoff = NOW - 14 * DAY
    truth = tmp_db.execute("SELECT max(id) FROM provider_calls WHERE ts_ms < ?", (cutoff,)).fetchone()[0]
    seen: list[str] = []
    tmp_db.set_trace_callback(seen.append)
    try:
        got = RT.id_boundary(tmp_db, "provider_calls", "ts_ms", cutoff)
    finally:
        tmp_db.set_trace_callback(None)
    assert got == truth
    assert len(seen) <= 2 * math.ceil(math.log2(3000)) + 4, len(seen)
    assert RT.id_boundary(tmp_db, "provider_calls", "ts_ms", NOW - 100 * DAY) is None


def test_the_history_estimate_tracks_the_true_deletable_count(tmp_db):
    rng = random.Random(3)
    _random_history(tmp_db, rng, wallets=120, rows=20)
    cutoff = NOW - 14 * DAY
    truth = 0
    for chain, address in {(r[0], r[1]) for r in tmp_db.execute(
            "SELECT DISTINCT chain, address FROM wallet_score_history")}:
        truth += len(RT.deletable_history_ids(RT._wallet_rows(tmp_db, chain, address), cutoff))
    est = RT.estimate(tmp_db, _cfg(sample_rows=1500), now_ms=NOW, rng=random.Random(9))["wallet_score_history"]
    older_true = tmp_db.execute("SELECT count(*) FROM wallet_score_history WHERE scored_at_ms < ?",
                                (cutoff,)).fetchone()[0]
    assert truth > 100
    assert est["rows_older_est"] == older_true, "ids are time-ordered, so the id span is exact"
    lo, hi = est["deletable_est_ci95"]
    assert lo - 0.02 * older_true <= truth <= hi + 0.02 * older_true, (est, truth)


# --------------------------------------------------------------------------- scheduler wiring


def test_the_shipped_config_ships_retention_off_twice_as_a_long_job():
    config = S.load_config(ROOT / "config" / "schedule.yaml")
    assert config.retention.enabled is False, "the delete switch ships off"
    job = config.jobs["retention"]
    assert job.enabled is False, "the job ships off"
    assert job.timeout_s >= config.long_timeout_s, "it must queue for the long slot, not crowd it"
    assert job.timeout_s < config.lock_stale_s
    assert config.retention.budget_s * 1000 < job.timeout_s * 1000 - 30_000
    assert config.retention.batch_rows <= RT.MAX_BATCH_ROWS
    assert "retention" in S.JOBS and "retention" not in config.critical_jobs
    assert S.ScheduleConfig().retention.enabled is False, "absent block = off"


def _ctx(conn, config: S.ScheduleConfig, params: dict[str, Any] | None = None) -> S.JobContext:
    now = now_ms()
    return S.JobContext("retention", conn, params or {}, config, now, now + 900_000)


def test_the_job_under_the_shipped_switch_deletes_nothing(tmp_db):
    t = now_ms()
    for age in (30, 20, 1):
        tmp_db.execute("INSERT INTO provider_calls (provider, endpoint, ts_ms, status) VALUES ('p','x',?,'ok')",
                       (t - age * DAY,))
    shipped = S.load_config(ROOT / "config" / "schedule.yaml")
    out = S.job_retention(_ctx(tmp_db, shipped))
    assert out["mode"] == "dry_run" and _count(tmp_db, "provider_calls") == 3
    assert len(jdump(out)) <= 2000, "the scheduler truncates results over 2,000 chars to their keys"


def test_the_job_deletes_when_the_switch_is_on_and_honours_a_dry_run_param(tmp_db):
    t = now_ms()
    for age in (30, 20, 1):
        tmp_db.execute("INSERT INTO provider_calls (provider, endpoint, ts_ms, status) VALUES ('p','x',?,'ok')",
                       (t - age * DAY,))
    on = S.ScheduleConfig(retention=RT.RetentionConfig(enabled=True, sleep_s=0))
    forced = S.job_retention(_ctx(tmp_db, on, {"dry_run": True}))
    assert forced["mode"] == "dry_run" and _count(tmp_db, "provider_calls") == 3
    out = S.job_retention(_ctx(tmp_db, on))
    assert out["mode"] == "delete" and _count(tmp_db, "provider_calls") == 1
    assert len(jdump(out)) <= 2000


def test_the_cli_refuses_execute_while_disabled(tmp_db, tmp_path, capsys):
    _seed_all(tmp_db)
    cfg_path = tmp_path / "schedule.yaml"
    cfg_path.write_text("version: v1\nretention:\n  enabled: false\njobs: {}\n", encoding="utf-8")
    code = RT.main(["--db", str(tmp_path / "kaiba.db"), "--config", str(cfg_path), "--execute"])
    assert code == 2
    assert json.loads(capsys.readouterr().out)["mode"] == "dry_run"
    assert _count(tmp_db, "provider_calls") == 10
    assert RT.main(["--db", str(tmp_path / "kaiba.db"), "--config", str(cfg_path)]) == 0


def test_a_lock_error_that_is_not_busy_is_not_swallowed(tmp_db):
    pacer = RT._Pacer(_cfg(), NOW + 10**9, lambda: NOW / 1000, lambda s: None)

    def boom(c: sqlite3.Connection) -> int:
        raise sqlite3.OperationalError("no such table: nope")

    with pytest.raises(sqlite3.OperationalError):
        pacer.delete(tmp_db, boom)


def test_no_statement_scans_a_big_table(tmp_db):
    """Every read and delete is a rowid or index SEARCH. MEASURED 2026-10-01: an earlier
    ``SELECT min(id), max(id)`` planned as a full SCAN and took 462 s on the box's
    wallet_score_history and provider_calls -- a dry run that was meant to be cheap."""
    _seed_all(tmp_db)
    _random_history(tmp_db, random.Random(2), wallets=10, rows=10)
    seen: list[str] = []
    tmp_db.set_trace_callback(seen.append)
    try:
        RT.run(tmp_db, _cfg(sample_rows=30), dry_run=True, now_ms=NOW, rng=random.Random(1))
        RT.run(tmp_db, _cfg(history_scan_rows=10), dry_run=False, now_ms=NOW)
    finally:
        tmp_db.set_trace_callback(None)
    big = ("provider_calls", "triage_decisions", "wallet_score_history")
    checked = 0
    for sql in {s for s in seen if s.lstrip().upper().startswith(("SELECT", "DELETE"))}:
        if not any(t in sql for t in big):
            continue
        plan = [str(r[3]) for r in tmp_db.execute(f"EXPLAIN QUERY PLAN {sql}")]
        assert not any(p.startswith("SCAN") and any(t in p for t in big) for p in plan), (sql, plan)
        assert not any("TEMP B-TREE" in p for p in plan), (sql, plan)
        checked += 1
    assert checked >= 8, checked
