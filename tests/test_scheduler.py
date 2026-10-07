"""The maintenance scheduler (kaiba/ops/scheduler.py).

The tests that carry weight are the ones for the failure modes the scheduler exists to
make impossible, and each is written as the failure it prevents:

* ``test_one_failing_job_does_not_stop_the_others`` and ``test_a_timed_out_job_...`` —
  a job cannot take the service down, and a hung one cannot be started twice.
* ``test_a_helius_spending_job_is_skipped_below_the_floor`` — a schedule cannot burn the
  month's credits; the skip is recorded with its reason, never silent.
* ``test_the_wallet_buyers_job_respects_its_daily_quota`` — the quota lives in the database,
  so a restart inside the day cannot reset it.
* ``test_a_restart_does_not_rerun_what_is_not_due`` — boot reads ``ops_jobs`` rather than
  firing everything, and overdue jobs are staggered rather than fired together.
* ``test_status_derives_the_states_an_operator_needs`` — "never ran", "late" and "missing"
  are first-class, because a job that silently stopped looks like nothing otherwise.

Nothing here touches the network. Jobs under test are fakes; the real jobs are exercised
against monkeypatched entry points so their targeting and accounting are checked without a
provider.
"""

from __future__ import annotations

import importlib.util
import json
import os
import random
import socket
import sys
import threading
import time
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from kaiba.core import db as core_db
from kaiba.core.db import fetch_all, fetch_one
from kaiba.core.schemas import Chain, now_ms
from kaiba.ops import scheduler as S

ROOT = Path(__file__).resolve().parents[1]
SOL = Chain.SOL.value


# --------------------------------------------------------------------------- helpers


def cfg(*, stagger_s: float = 0.0, max_concurrent: int = 2, backoff: dict[str, Any] | None = None,
        floor: int = 300_000, **jobs: dict[str, Any]) -> S.ScheduleConfig:
    """Jitter 0 and no stagger by default so timings in assertions are exact."""
    return S.ScheduleConfig(
        tick_s=0.01, jitter_pct=0, stagger_s=stagger_s, max_concurrent=max_concurrent, lock_stale_s=5,
        helius=S.HeliusGate(floor_credits=floor), backoff=S.BackoffConfig(**(backoff or {})),
        jobs={name: S.JobConfig(**spec) for name, spec in jobs.items()},
    )


def job(interval_s: int = 60, timeout_s: int = 30, **params: Any) -> dict[str, Any]:
    return {"interval_s": interval_s, "timeout_s": timeout_s, **params}


def spec(name: str, fn: Any, *, spends: bool = False) -> S.JobSpec:
    return S.JobSpec(name, fn, "test job", spends_helius=spends)


def make(tmp_db: Any, tmp_path: Path, config: S.ScheduleConfig, jobs: dict[str, S.JobSpec],
         **kw: Any) -> S.Scheduler:
    path = tmp_path / "kaiba.db"
    kw.setdefault("budget_status", lambda conn: {"remaining": 1_000_000, "used": 0, "allowance": 1_000_000})
    return S.Scheduler(
        config, jobs=jobs, conn=tmp_db, conn_factory=lambda: core_db.connect(path),
        lock_dir=tmp_path / "ops", rng=random.Random(1), **kw,
    )


def job_row(conn: Any, name: str) -> dict[str, Any]:
    row = fetch_one(conn, "SELECT * FROM ops_jobs WHERE name=?", (name,))
    assert row is not None, f"no ops_jobs row for {name}"
    return row


def ctx_for(conn: Any, name: str, params: dict[str, Any], config: S.ScheduleConfig | None = None,
            timeout_s: int = 60) -> S.JobContext:
    now = now_ms()
    return S.JobContext(name, conn, params, config or S.ScheduleConfig(), now, now + timeout_s * 1000)


def seed_swap(conn: Any, wallet: str, token: str, *, ts_ms: int, source: str = "pumpfun:trades",
              side: str = "buy") -> None:
    conn.execute(
        "INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, amount_token, source) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (SOL, f"tx-{wallet}-{token}-{ts_ms}", ts_ms, wallet, token, side, "1", source),
    )


def seed_score(conn: Any, address: str, *, grade: str = "C", scored_at_ms: int | None = None,
               model_version: str = "test", chain: str = SOL) -> None:
    conn.execute(
        "INSERT INTO wallet_scores (chain, address, score, grade, evidence_weight, archetype, "
        "model_version, scored_at_ms) VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(chain, address) DO UPDATE SET grade=excluded.grade, scored_at_ms=excluded.scored_at_ms, "
        "model_version=excluded.model_version",
        (chain, address, 50.0, grade, 10.0, "unknown", model_version, scored_at_ms or now_ms()),
    )


def seed_wallet(conn: Any, address: str, cohort: str) -> None:
    ts = now_ms()
    conn.execute(
        "INSERT INTO wallets (chain, address, first_seen_ms, last_seen_ms, cohort) VALUES (?,?,?,?,?)",
        (SOL, address, ts, ts, cohort),
    )


def seed_dossier(conn: Any, token: str, *, built_ms: int, created_ms: int | None) -> None:
    conn.execute(
        "INSERT INTO tokens (chain, address, created_ms, first_seen_ms) VALUES (?,?,?,?)",
        (SOL, token, created_ms, built_ms),
    )
    conn.execute(
        "INSERT INTO token_dossiers (chain, address, built_at_ms, dossier_json) VALUES (?,?,?,'{}')",
        (SOL, token, built_ms),
    )


# --------------------------------------------------------------------------- the shipped config


class _NoDuplicateKeys(yaml.SafeLoader):
    """``yaml.safe_load`` keeps the LAST of two identical keys without a word, so a job
    block pasted twice would silently ship the second copy's numbers."""

    def construct_mapping(self, node: Any, deep: bool = False) -> dict[Any, Any]:
        seen: set[Any] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            assert key not in seen, f"duplicate key {key!r} at line {key_node.start_mark.line + 1}"
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def test_shipped_schedule_covers_every_registered_job_and_nothing_else():
    """The reconciliation: every JobSpec has a YAML entry, every YAML entry a JobSpec, each
    entry once, and each spec runs the function of its own name (a copy-paste that
    registered ``job_wallet_tape`` under ``wallet_naming`` would pass the set check)."""
    config = S.load_config(ROOT / "config" / "schedule.yaml")
    assert set(config.jobs) == set(S.JOBS)
    raw = yaml.load((ROOT / "config" / "schedule.yaml").read_text(encoding="utf-8"), Loader=_NoDuplicateKeys)
    assert set(raw["jobs"]) == set(S.JOBS)
    for name, sp in S.JOBS.items():
        assert sp.name == name and sp.run.__name__ == f"job_{name}", name
        assert sp.why, f"{name}: says why"
    for name, jc in config.jobs.items():
        assert jc.interval_s > 0 and jc.timeout_s > 0, name
        assert jc.timeout_s < config.lock_stale_s, f"{name}: a live job would look stale"
    # The dispatcher must poll the signals hunter at least as often as its fastest source.
    from kaiba.hunters.signals import DEFAULT_INTERVALS

    assert config.jobs["signals"].interval_s <= min(DEFAULT_INTERVALS.values())


def monthly_ceiling(params: dict[str, Any]) -> int:
    """The month's worst case for one wallet job under the shipped parameters.

    The binding daily limit is the smaller of the count cap (``wallets_per_day`` x the
    planning estimate) and the credit cap (``credits_per_day``, when the schedule names
    one); ``gather_credit_room`` takes the tighter of the two on every run, so the
    month's ceiling is that figure x 31.
    """
    by_count = int(params["wallets_per_day"]) * S.CREDITS_PER_WALLET_EST
    by_credits = int(params["credits_per_day"]) if "credits_per_day" in params else by_count
    return min(by_count, by_credits) * 31


def test_shipped_wallet_budgets_stay_well_under_ten_percent_of_the_free_month():
    """The bound is now 46.5% of the free month, not 10% and no longer 31%. The name is
    kept as the anchor other worklogs cite; the reasoning for each raise is here.

    RAISED AGAIN 2026-09-24 by the lead, who asked for their own GMGN wallet list to be
    graded. What did NOT change is the property this test actually protects: scheduled
    paid work must still fit above ``helius.floor_credits``, which reserves 30% of the
    month for the on-demand scanner. A first attempt at 20,000/day and 40 tracked wallets
    came to 852,500 against a 700,000 ceiling and this test caught it. What ships:

        wallet_buyers      15,000/day x 31  =  465,000   46.5%
        wallet_tracked     20/day x 125x31  =   77,500    7.8%
        clustering_funding  2,500/day x 31  =   77,500    7.8%
                                              ---------
                                               620,000   of 700,000, 80,000 spare

    MEASURED, ``provider_budget`` for 2026-09: 32,263 credits across all Helius routes in
    the four days the ledger has existed, ~8,000/day, so the tier was ~24% committed and
    the raise spends headroom that was sitting idle.

    MEASURED on the live box, 2026-09-21, under the previous schedule: ``wallet_buyers``
    graded 15 wallets a day for 1,650 credits (110 each) against a month of 1,000,000 and
    a 300,000 floor, i.e. 0.5% of the discretionary budget, while ``swaps`` held 61,334
    distinct ungraded pump.fun buyers and ``wallet_scores`` had 27 rows ever. Coverage of
    graded buyers is the input three lanes lack, so the schedule now spends
    ``credits_per_day: 10000`` on that job: 310,000 a month, 31% of the free tier, 44% of
    the 700,000 above the floor. The count cap (120 x 125 = 15,000 a day) is deliberately
    looser than the credit cap so credits, not a count, are the binding limit, and
    ``GATHER_DAILY_CREDIT_HARD_MAX`` (5% of the month a day) clamps a typo above it.

    ``wallet_tracked`` keeps its 1.9%. Together with ``clustering_funding`` (7.8%) the
    scheduled paid work is under 41% of the month, which leaves the on-demand scanner
    more than the 30% the floor protects before the floor ever binds.
    """
    config = S.load_config(ROOT / "config" / "schedule.yaml")
    buyers = config.jobs["wallet_buyers"].params
    tracked = config.jobs["wallet_tracked"].params
    # The credit cap must exist and must be the one that binds.
    assert "credits_per_day" in buyers
    assert int(buyers["credits_per_day"]) < int(buyers["wallets_per_day"]) * S.CREDITS_PER_WALLET_EST
    assert int(buyers["credits_per_day"]) <= S.GATHER_DAILY_CREDIT_HARD_MAX
    buyers_month = monthly_ceiling(buyers)
    assert buyers_month <= 0.47 * S.FREE_MONTHLY_CREDITS, f"wallet_buyers would cost {buyers_month}/month"
    tracked_month = monthly_ceiling(tracked)
    assert tracked_month <= 0.08 * S.FREE_MONTHLY_CREDITS, f"wallet_tracked would cost {tracked_month}/month"
    monthly = buyers_month + tracked_month
    assert monthly <= 0.55 * S.FREE_MONTHLY_CREDITS, f"scheduled wallet work would cost {monthly}/month"
    funding = int(config.jobs["clustering_funding"].params["credits_per_day"]) * 31
    # wallet_deepen (2026-10-02): 1,000/day x 31 = 31,000, 3.1%. Total 651,000 of 700,000.
    deepen = config.jobs["wallet_deepen"].params
    assert int(deepen["credits_per_day"]) <= S.GATHER_DAILY_CREDIT_HARD_MAX
    deepen_month = int(deepen["credits_per_day"]) * 31
    assert deepen_month <= 0.04 * S.FREE_MONTHLY_CREDITS, f"wallet_deepen would cost {deepen_month}/month"
    floor = config.helius.floor_credits
    assert monthly + funding + deepen_month < S.FREE_MONTHLY_CREDITS - floor, (
        "scheduled paid work must fit above the floor")
    assert 0.10 * S.FREE_MONTHLY_CREDITS <= floor <= 0.50 * S.FREE_MONTHLY_CREDITS


def test_shipped_gather_throughput_is_credit_bound_and_grades_nothing_for_free():
    """The gather-throughput YAML, with its one dangerous line left out.

    ``free_grade_chains`` is NOT set: the verifier showed a 7-row feed wallet graded to B
    through the uncapped full-history model, and a B is what ``sm-trenches`` sizes on.
    """
    config = S.load_config(ROOT / "config" / "schedule.yaml")
    buyers = config.jobs["wallet_buyers"]
    assert buyers.enabled and buyers.interval_s == 900
    p = buyers.params
    assert p["wallets_per_run"] == 6 and p["wallets_per_day"] == 240
    assert p["credits_per_day"] == 15_000 and p["max_credits_per_run"] == 1_500
    # One run's ceiling is 2x the estimate for its wallets (the rule this file has always
    # used) and a run can never spend more than the day.
    assert p["max_credits_per_run"] == 2 * p["wallets_per_run"] * S.CREDITS_PER_WALLET_EST
    assert p["max_credits_per_run"] <= p["credits_per_day"]
    assert "free_grade_chains" not in p
    # The imported-list top-up is pinned to the operator's cohorts, because `wallets` is
    # about to be populated by naming and cohort seeding with rows whose cohort is NULL and
    # `backfill.tracked_wallets` with no cohort filter would top those up alphabetically.
    cohorts = str(config.jobs["wallet_tracked"].params["cohorts"])
    assert {c.strip() for c in cohorts.split(",")} == {"research", "tracked", "trusted_copy"}


def test_a_missing_or_invalid_schedule_is_an_error_not_a_guess(tmp_path):
    with pytest.raises(S.ScheduleConfigError):
        S.load_config(tmp_path / "nope.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("jobs:\n  x:\n    interval_s: -5\n    timeout_s: 1\n", encoding="utf-8")
    with pytest.raises(S.ScheduleConfigError):
        S.load_config(bad)
    bad.write_text("- not a mapping\n", encoding="utf-8")
    with pytest.raises(S.ScheduleConfigError):
        S.load_config(bad)


def test_migration_creates_the_scheduler_tables(tmp_db):
    names = {r["name"] for r in fetch_all(tmp_db, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"ops_jobs", "ops_runs", "ops_quota"} <= names


# --------------------------------------------------------------------------- lock


def test_a_hard_killed_scheduler_can_restart_before_systemd_gives_up(tmp_db, tmp_path):
    """MEASURED 2026-09-20: a SIGKILLed scheduler leaves its lock behind with a fresh
    heartbeat, and the restart was refused for the full lock_stale_s (900 s). The unit
    restarts every 5 s with StartLimitBurst=10 in 300 s, so systemd would exhaust its
    start limit and leave the service dead — a crash turned into a permanent outage."""
    # The shipped numbers are the ones that decide whether a crashed unit comes back.
    shipped = S.load_config(ROOT / "config" / "schedule.yaml")
    assert shipped.service_lock_stale_s <= 120, "must clear systemd's StartLimitIntervalSec=300"
    # A job lock from the same crash keeps the long window: reclaiming one means running a
    # job that might still be in flight.
    assert shipped.lock_stale_s > shipped.service_lock_stale_s

    config = cfg(a=job())
    lock_dir = tmp_path / "ops"
    # The lock a killed process leaves behind: heartbeat stops, nothing releases it.
    lock_dir.mkdir(parents=True)
    killed = now_ms() - int(config.service_lock_stale_s * 1000) - 1_000
    (lock_dir / "scheduler.lock").write_text(
        json.dumps({"pid": 999_999, "host": socket.gethostname(), "heartbeat_ms": killed})
    )
    sched = make(tmp_db, tmp_path, config, {"a": spec("a", lambda c: {})})
    assert sched.service_lock.acquire().held
    sched.service_lock.release()
    assert S.service_holder(lock_dir, stale_after_s=config.service_lock_stale_s) is None


def test_service_holder_reports_heartbeat_age(tmp_path):
    lock_dir = tmp_path / "ops"
    lock = S.JobLock(lock_dir / "scheduler.lock", stale_after_s=600).acquire()
    holder = S.service_holder(lock_dir, stale_after_s=600, now=now_ms() + 45_000)
    # Windows cannot probe the pid, so the age is how an operator tells a dead holder from
    # a live one before it ages out.
    assert holder is not None and 44 <= holder["heartbeat_age_s"] <= 46
    lock.release()


def test_lock_refuses_a_live_holder_and_reclaims_a_stale_one(tmp_path):
    path = tmp_path / "ops" / "x.lock"
    first = S.JobLock(path, stale_after_s=60).acquire()
    with pytest.raises(S.AlreadyRunning) as exc:
        S.JobLock(path, stale_after_s=60).acquire()
    assert exc.value.holder["pid"] == os.getpid()
    first.release()
    assert not path.exists()

    # A holder whose heartbeat is older than the stale window is debris from a crash.
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"pid": 999_999, "host": "elsewhere", "heartbeat_ms": now_ms() - 120_000}))
    reclaimed = S.JobLock(path, stale_after_s=60).acquire()
    assert reclaimed.held
    reclaimed.release()

    # Unreadable debris is stale too.
    path.write_text("not json")
    assert S.JobLock(path).acquire().held


# --------------------------------------------------------------------------- the loop


def test_a_due_job_runs_records_its_result_and_reschedules(tmp_db, tmp_path):
    seen: list[str] = []

    def work(ctx: S.JobContext) -> dict[str, Any]:
        seen.append(ctx.name)
        assert ctx.params["tag"] == "x"
        assert ctx.time_left_s() > 0
        return {"n": 1}

    sched = make(tmp_db, tmp_path, cfg(alpha=job(60, 30, tag="x")), {"alpha": spec("alpha", work)})
    outcomes = sched.run_once(force=True)
    assert seen == ["alpha"]
    assert [o.status for o in outcomes] == ["ok"]
    row = job_row(tmp_db, "alpha")
    assert row["runs"] == 1 and row["last_status"] == "ok" and row["consecutive_failures"] == 0
    assert json.loads(row["last_result_json"]) == {"n": 1}
    assert row["last_success_ms"] is not None
    # Jitter is 0 here, so the next run is exactly one interval after the reschedule.
    assert 59_000 <= row["next_due_ms"] - row["last_success_ms"] <= 61_000
    run = fetch_one(tmp_db, "SELECT * FROM ops_runs WHERE job='alpha'")
    assert run["status"] == "ok" and run["finished_ms"] is not None and run["pid"] == os.getpid()
    assert not (tmp_path / "ops" / "alpha.lock").exists()
    # Not due again until then.
    assert sched.due_jobs() == []


def test_one_failing_job_does_not_stop_the_others_and_the_error_is_redacted(tmp_db, tmp_path):
    def boom(ctx: S.JobContext) -> dict[str, Any]:
        raise RuntimeError("provider said no for https://x.test/?api-key=SECRETVALUE123")

    ran: list[str] = []

    def fine(ctx: S.JobContext) -> dict[str, Any]:
        ran.append("beta")
        return {}

    sched = make(tmp_db, tmp_path, cfg(alpha=job(), beta=job()),
                 {"alpha": spec("alpha", boom), "beta": spec("beta", fine)})
    outcomes = {o.job: o for o in sched.run_once(force=True)}
    assert ran == ["beta"]
    assert outcomes["alpha"].status == "error" and outcomes["beta"].status == "ok"
    row = job_row(tmp_db, "alpha")
    assert row["consecutive_failures"] == 1 and row["failures"] == 1
    assert "RuntimeError" in row["last_error"]
    assert "SECRETVALUE123" not in row["last_error"]
    assert row["last_success_ms"] is None
    # The failure is on the bus, redacted, with the job named.
    from kaiba.core.events import recent

    ev = [e for e in recent(limit=20, kinds=["system"]) if e.payload.get("action") == "ops.job"]
    assert ev and ev[0].payload["job"] == "alpha" and ev[0].level == "warn"
    assert "SECRETVALUE123" not in json.dumps(ev[0].payload)


def test_consecutive_failures_back_off_and_recovery_resets(tmp_db, tmp_path):
    state = {"fail": True}

    def flaky(ctx: S.JobContext) -> dict[str, Any]:
        if state["fail"]:
            raise S.JobFailed("still broken", {"partial": 1})
        return {"fixed": True}

    config = cfg(backoff={"after_failures": 2, "multiplier": 2.0, "max_s": 10_000}, alpha=job(100))
    sched = make(tmp_db, tmp_path, config, {"alpha": spec("alpha", flaky)})
    for _ in range(4):
        sched.run_once(force=True)
    row = job_row(tmp_db, "alpha")
    assert row["consecutive_failures"] == 4 and row["failures"] == 4
    assert json.loads(row["last_result_json"]) == {"partial": 1}
    # `after_failures: N` means the Nth consecutive failure is itself the first backoff —
    # the job has by then failed N times in a row, which is the condition the operator
    # configured ("a job that fails N times in a row backs off"). Only the failures
    # *below* N retry at the plain interval. An earlier draft of this test read it as
    # "N plain retries, then back off", which gives a job one more hammer than asked for.
    assert sched.backoff_interval("alpha", 1) == 100    # below N: plain interval
    assert sched.backoff_interval("alpha", 2) == 200    # N reached: first doubling
    assert sched.backoff_interval("alpha", 3) == 400
    assert sched.backoff_interval("alpha", 4) == 800
    assert sched.backoff_interval("alpha", 20) == 10_000  # capped, not 100 * 2**19
    assert 799_000 <= row["next_due_ms"] - row["last_error_ms"] <= 801_000
    state["fail"] = False
    (outcome,) = sched.run_once(force=True)
    assert outcome.status == "ok"
    row = job_row(tmp_db, "alpha")
    assert row["consecutive_failures"] == 0 and row["failures"] == 4
    assert 99_000 <= row["next_due_ms"] - row["last_success_ms"] <= 101_000


def test_a_timed_out_job_is_recorded_and_not_started_again_until_its_thread_returns(tmp_db, tmp_path):
    release = threading.Event()
    started = threading.Event()

    def hang(ctx: S.JobContext) -> dict[str, Any]:
        started.set()
        release.wait(10)
        return {"late": True}

    sched = make(tmp_db, tmp_path, cfg(alpha=job(60, 1)), {"alpha": spec("alpha", hang)})
    t0 = time.time()
    (outcome,) = sched.run_once(force=True)
    assert outcome.status == "timeout" and "timed out" in (outcome.error or "")
    assert time.time() - t0 < 5
    row = job_row(tmp_db, "alpha")
    assert row["last_status"] == "timeout" and row["consecutive_failures"] == 1
    # Still busy: the lock is held and the job is not dispatchable, even by force.
    assert "alpha" in sched.orphans and (tmp_path / "ops" / "alpha.lock").exists()
    assert sched.due_jobs(force=True) == []
    assert sched.run_once(force=True) == []
    # When the thread finally returns the lock is released and the job is dispatchable again.
    release.set()
    sched.orphans["alpha"].done.wait(5)
    sched.tick()
    assert "alpha" not in sched.orphans
    assert not (tmp_path / "ops" / "alpha.lock").exists()
    assert sched.due_jobs(force=True) == ["alpha"]
    run = fetch_one(tmp_db, "SELECT result_json FROM ops_runs WHERE job='alpha'")
    assert json.loads(run["result_json"]).get("late") is True


def test_a_job_raising_system_exit_does_not_end_the_scheduler(tmp_db, tmp_path):
    def quit_(ctx: S.JobContext) -> dict[str, Any]:
        raise SystemExit(7)

    sched = make(tmp_db, tmp_path, cfg(alpha=job()), {"alpha": spec("alpha", quit_)})
    (outcome,) = sched.run_once(force=True)
    assert outcome.status == "error" and "SystemExit" in (outcome.error or "")


def test_a_helius_spending_job_is_skipped_below_the_floor_and_credits_are_attributed(tmp_db, tmp_path):
    ledger = {"used": 0}
    calls: list[str] = []

    def budget(conn: Any) -> dict[str, Any]:
        return {"remaining": 1_000_000 - ledger["used"], "used": ledger["used"], "allowance": 1_000_000}

    def spend(ctx: S.JobContext) -> dict[str, Any]:
        calls.append("spend")
        ledger["used"] += 250
        return {"wallets": 2}

    config = cfg(floor=999_000, alpha=job())
    sched = make(tmp_db, tmp_path, config, {"alpha": spec("alpha", spend, spends=True)}, budget_status=budget)
    ledger["used"] = 5_000  # remaining 995,000 < floor 999,000
    (outcome,) = sched.run_once(force=True)
    assert outcome.status == "skipped" and outcome.result["reason"] == "helius_budget_floor"
    assert outcome.result["remaining"] == 995_000 and outcome.result["floor"] == 999_000
    assert calls == []
    row = job_row(tmp_db, "alpha")
    assert row["last_status"] == "skipped" and row["last_error"] == "helius_budget_floor"
    assert row["consecutive_failures"] == 0  # a budget skip is not a failure
    assert row["next_due_ms"] > now_ms() - 1000
    run = fetch_one(tmp_db, "SELECT status, error FROM ops_runs WHERE job='alpha'")
    assert run["status"] == "skipped" and run["error"] == "helius_budget_floor"

    ledger["used"] = 0
    (outcome,) = sched.run_once(force=True)
    assert outcome.status == "ok" and calls == ["spend"]
    assert outcome.credits == 250
    assert job_row(tmp_db, "alpha")["credits_total"] == 250


def test_an_unreadable_ledger_blocks_spending(tmp_db, tmp_path):
    def broken(conn: Any) -> dict[str, Any]:
        raise RuntimeError("ledger gone")

    sched = make(tmp_db, tmp_path, cfg(alpha=job()),
                 {"alpha": spec("alpha", lambda ctx: {"ran": True}, spends=True)}, budget_status=broken)
    (outcome,) = sched.run_once(force=True)
    assert outcome.status == "skipped" and outcome.result["reason"] == "helius_budget_floor"
    assert "ledger gone" in outcome.result["ledger_error"]


def test_a_job_locked_by_another_process_is_skipped_not_run(tmp_db, tmp_path):
    lock_path = tmp_path / "ops" / "alpha.lock"
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text(json.dumps({"pid": os.getpid(), "host": socket.gethostname(),
                                     "heartbeat_ms": now_ms()}))
    ran: list[str] = []
    sched = make(tmp_db, tmp_path, cfg(alpha=job()), {"alpha": spec("alpha", lambda c: ran.append("x") or {})})
    (outcome,) = sched.run_once(force=True)
    assert outcome.status == "skipped" and outcome.result["reason"] == "locked_elsewhere"
    assert outcome.result["holder"]["pid"] == os.getpid()
    assert ran == []


def test_a_restart_does_not_rerun_what_is_not_due_and_staggers_what_is(tmp_db, tmp_path):
    ran: list[str] = []
    jobs = {n: spec(n, lambda c, n=n: ran.append(n) or {}) for n in ("a", "b", "c")}
    config = cfg(stagger_s=20, a=job(600), b=job(600), c=job(600))
    first = make(tmp_db, tmp_path, config, jobs)
    first.sync_jobs()
    # A cold boot spreads first runs: nothing is due this instant.
    assert first.due_jobs() == []
    dues = sorted(job_row(tmp_db, n)["next_due_ms"] for n in ("a", "b", "c"))
    assert dues[1] - dues[0] >= 10_000 and dues[2] - dues[1] >= 10_000
    first.run_once(force=True)
    assert sorted(ran) == ["a", "b", "c"]

    # A second process on the same database sees the future next_due and runs nothing.
    second = make(tmp_db, tmp_path, config, jobs)
    assert second.run_once() == []
    assert all(job_row(tmp_db, n)["runs"] == 1 for n in ("a", "b", "c"))

    # After downtime long enough for everything to be overdue, boot staggers rather than
    # firing all three on the same second.
    tmp_db.execute("UPDATE ops_jobs SET next_due_ms = ?", (now_ms() - 3_600_000,))
    third = make(tmp_db, tmp_path, config, jobs)
    third.sync_jobs()
    dues = sorted(job_row(tmp_db, n)["next_due_ms"] for n in ("a", "b", "c"))
    assert dues[1] - dues[0] >= 10_000 and dues[2] - dues[1] >= 10_000
    assert third.due_jobs() == []


def test_a_run_interrupted_by_a_crash_is_recorded_on_the_next_boot(tmp_db, tmp_path):
    ts = now_ms() - 60_000
    tmp_db.execute(
        "INSERT INTO ops_jobs (name, enabled, interval_s, timeout_s, next_due_ms, last_run_ms, last_status, "
        "runs, updated_ms) VALUES ('a', 1, 60, 30, ?, ?, 'running', 1, ?)", (ts + 60_000, ts, ts),
    )
    tmp_db.execute("INSERT INTO ops_runs (job, started_ms, status) VALUES ('a', ?, 'running')", (ts,))
    sched = make(tmp_db, tmp_path, cfg(a=job()), {"a": spec("a", lambda c: {})})
    sched.sync_jobs()
    row = job_row(tmp_db, "a")
    assert row["last_status"] == "error" and "restarted mid-run" in row["last_error"]
    assert row["failures"] == 1 and row["consecutive_failures"] == 1
    assert fetch_one(tmp_db, "SELECT status FROM ops_runs WHERE job='a'")["status"] == "error"


def test_retired_and_disabled_jobs_do_not_run(tmp_db, tmp_path):
    ran: list[str] = []
    jobs = {n: spec(n, lambda c, n=n: ran.append(n) or {}) for n in ("a", "b")}
    sched = make(tmp_db, tmp_path, cfg(a=job(), b=job(enabled=False)), jobs)
    # --force overrides the clock, not the operator's per-job kill switch. A flag that
    # silently defeats `enabled: false` would fire a job that was switched off precisely
    # because it was misbehaving — and `kaiba ops run --once --force` is a plausible thing
    # to type while testing the scheduler.
    sched.run_once(force=True)
    assert ran == ["a"]
    assert sched.due_jobs(force=True) == ["a"]
    # `b` disappears from the config: its row is kept for history, disabled.
    sched.config = cfg(a=job())
    sched.sync_jobs()
    assert job_row(tmp_db, "b")["enabled"] == 0
    assert sched.due_jobs(force=True) == ["a"]


def test_max_concurrent_bounds_parallelism(tmp_db, tmp_path):
    peak = {"now": 0, "max": 0}
    lock = threading.Lock()

    def slow(ctx: S.JobContext) -> dict[str, Any]:
        with lock:
            peak["now"] += 1
            peak["max"] = max(peak["max"], peak["now"])
        time.sleep(0.15)
        with lock:
            peak["now"] -= 1
        return {}

    jobs = {n: spec(n, slow) for n in ("a", "b", "c", "d")}
    sched = make(tmp_db, tmp_path, cfg(max_concurrent=2, a=job(), b=job(), c=job(), d=job()), jobs)
    outcomes = sched.run_once(force=True)
    assert len(outcomes) == 4 and all(o.status == "ok" for o in outcomes)
    assert peak["max"] == 2


def test_critical_jobs_get_a_slot_while_long_jobs_hold_the_rest(tmp_db, tmp_path):
    # MEASURED 2026-09-29: eighteen minutes after a restart execute_planned and native_price
    # had not run once. Three study jobs with 10-25 minute timeouts held all three slots, so
    # nothing could be sized and no planned order could be sent.
    release = threading.Event()

    def hang(ctx: S.JobContext) -> dict[str, Any]:
        release.wait(10)
        return {}

    names = ("study_a", "study_b", "study_c", "execute_planned", "native_price")
    config = cfg(
        max_concurrent=2,
        study_a=job(3600, 900), study_b=job(3600, 900), study_c=job(3600, 1200),
        execute_planned=job(5, 120), native_price=job(60, 60),
    )
    sched = make(tmp_db, tmp_path, config, {n: spec(n, hang) for n in names})
    sched.sync_jobs()
    try:
        started = sched.tick(force=True)
        assert "execute_planned" in started and "native_price" in started
        assert sum(1 for n in sched.running if n.startswith("study_")) == 1
    finally:
        release.set()
        sched.wait_idle()


def test_long_jobs_cannot_take_every_general_slot(tmp_db, tmp_path):
    release = threading.Event()

    def hang(ctx: S.JobContext) -> dict[str, Any]:
        release.wait(10)
        return {}

    config = cfg(max_concurrent=3, long_a=job(3600, 900), long_b=job(3600, 600), quick=job(60, 30))
    sched = make(tmp_db, tmp_path, config, {n: spec(n, hang) for n in ("long_a", "long_b", "quick")})
    sched.sync_jobs()
    try:
        sched.tick(force=True)
        assert sorted(sched.running) in (["long_a", "quick"], ["long_b", "quick"])
    finally:
        release.set()
        sched.wait_idle()


def test_a_runaway_job_is_let_finish_late_then_stopped_at_its_hard_deadline(tmp_db, tmp_path):
    # An abandoned thread used to keep its read snapshot for as long as its query ran. On
    # 2026-09-29 that stopped SQLite resetting the WAL until it reached 19.8 GB and the disk
    # filled; every service then wrote nothing for fourteen hours. But most jobs that time
    # out finish late and that late result is their only output, so the cut comes at
    # timeout * hard_timeout_factor, not at the timeout.
    def endless(ctx: S.JobContext) -> dict[str, Any]:
        ctx.conn.execute(
            "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT count(*) FROM c"
        ).fetchone()
        return {}

    config = cfg(spin=job(60, 1)).model_copy(update={"hard_timeout_factor": 2.5})
    sched = make(tmp_db, tmp_path, config, {"spin": spec("spin", endless)})
    (outcome,) = sched.run_once(force=True)
    assert outcome.status == "timeout"
    run = sched.orphans["spin"]
    assert not run.done.wait(0.5), "stopped before its hard deadline: a late finisher would lose its result"
    deadline = time.time() + 6
    while not run.done.is_set() and time.time() < deadline:
        sched.tick()
        time.sleep(0.05)
    assert run.done.is_set(), "the runaway job's query kept running past its hard deadline"
    assert run.outcome is not None and "interrupted" in (run.outcome.error or "")
    sched.tick()
    assert "spin" not in sched.orphans


def test_a_timed_out_long_job_keeps_its_slot_until_it_really_stops(tmp_db, tmp_path):
    # Every timeout used to free a slot while its work carried on, so timed-out studies
    # piled up three and four deep on a two-CPU box.
    release = threading.Event()

    def hang(ctx: S.JobContext) -> dict[str, Any]:
        release.wait(10)
        return {}

    config = cfg(max_concurrent=3, long_a=job(3600, 600), long_b=job(3600, 600))
    config = config.model_copy(update={"long_timeout_s": 1.0})
    config.jobs["long_a"] = S.JobConfig(interval_s=3600, timeout_s=1)
    sched = make(tmp_db, tmp_path, config, {n: spec(n, hang) for n in ("long_a", "long_b")})
    sched.sync_jobs()
    try:
        assert sched.tick(force=True) == ["long_a"]
        time.sleep(1.2)
        sched.tick(force=True)
        assert "long_a" in sched.orphans and "long_b" not in sched.running
        release.set()
        sched.orphans["long_a"].done.wait(5)
        assert "long_b" in sched.tick(force=True)
    finally:
        release.set()
        sched.wait_idle()


def test_a_timed_out_medium_job_still_holds_a_general_slot(tmp_db, tmp_path):
    # The 2026-09-29 OOM: wallet_naming (timeout 300 s) and wallet_regrade kept running as
    # orphans while fresh jobs took their slots; six heavy jobs ran in one 6.4 GB process.
    release = threading.Event()

    def hang(ctx: S.JobContext) -> dict[str, Any]:
        release.wait(10)
        return {}

    config = cfg(max_concurrent=1, naming=job(3600, 1), regrade=job(3600, 30))
    sched = make(tmp_db, tmp_path, config, {n: spec(n, hang) for n in ("naming", "regrade")})
    sched.sync_jobs()
    try:
        assert sched.tick(force=True) == ["naming"]
        time.sleep(1.2)
        sched.tick(force=True)
        assert "naming" in sched.orphans and "regrade" not in sched.running
        release.set()
        sched.orphans["naming"].done.wait(5)
        assert "regrade" in sched.tick(force=True)
    finally:
        release.set()
        sched.wait_idle()


def test_a_restart_does_not_stagger_the_trading_path(tmp_db, tmp_path):
    names = ("study_a", "study_b", "execute_planned", "native_price")
    config = cfg(stagger_s=20, study_a=job(), study_b=job(), execute_planned=job(5, 60),
                 native_price=job(60, 60))
    sched = make(tmp_db, tmp_path, config, {n: spec(n, lambda ctx: {}) for n in names})
    for boot in ("first boot", "restart with everything overdue"):
        now = sched._now()
        if boot != "first boot":
            tmp_db.execute("UPDATE ops_jobs SET next_due_ms=?", (now - 60_000,))
        sched.sync_jobs(now)
        due = {n: job_row(tmp_db, n)["next_due_ms"] - now for n in names}
        assert due["execute_planned"] <= 0 and due["native_price"] <= 0, boot
    due = {n: job_row(tmp_db, n)["next_due_ms"] - now for n in names}
    assert due["execute_planned"] <= 0 and due["native_price"] <= 0
    assert sorted(due[n] for n in ("study_a", "study_b"))[1] >= 20_000  # the rest still stagger


def test_jitter_stays_inside_the_configured_band(tmp_db, tmp_path):
    config = S.ScheduleConfig(jitter_pct=10, jobs={"a": S.JobConfig(**job(100))})
    sched = make(tmp_db, tmp_path, config, {"a": spec("a", lambda c: {})})
    samples = [sched.jittered(100) for _ in range(200)]
    assert all(90 <= s <= 110 for s in samples)
    assert max(samples) - min(samples) > 5  # it does actually vary


def test_a_bad_config_edit_keeps_the_last_good_one(tmp_db, tmp_path):
    path = tmp_path / "schedule.yaml"
    path.write_text("jitter_pct: 0\njobs:\n  a:\n    interval_s: 60\n    timeout_s: 30\n", encoding="utf-8")
    sched = make(tmp_db, tmp_path, S.load_config(path), {"a": spec("a", lambda c: {})}, config_path=path)
    sched.sync_jobs()
    path.write_text("jobs: [broken", encoding="utf-8")
    sched._maybe_reload(now_ms() + 10 ** 9)
    assert sched.config.jobs["a"].interval_s == 60
    path.write_text("jitter_pct: 0\njobs:\n  a:\n    interval_s: 999\n    timeout_s: 30\n", encoding="utf-8")
    sched._maybe_reload(now_ms() + 2 * 10 ** 9)
    assert sched.config.jobs["a"].interval_s == 999
    assert job_row(tmp_db, "a")["interval_s"] == 999


def test_an_entry_point_that_does_not_exist_is_recorded_not_failed(tmp_db, tmp_path):
    def missing(ctx: S.JobContext) -> dict[str, Any]:
        raise S.EntryPointMissing("kaiba.providers.native_price not importable")

    sched = make(tmp_db, tmp_path, cfg(a=job()), {"a": spec("a", missing)})
    (outcome,) = sched.run_once(force=True)
    assert outcome.status == "skipped" and outcome.result["reason"] == "entry_point_missing"
    row = job_row(tmp_db, "a")
    assert row["consecutive_failures"] == 0
    assert S.status(tmp_db, sched.config)[0]["state"] == "missing"


def test_status_derives_the_states_an_operator_needs(tmp_db, tmp_path):
    jobs = {
        "ok": spec("ok", lambda c: {"fine": 1}),
        "failing": spec("failing", lambda c: (_ for _ in ()).throw(RuntimeError("x"))),
        "never": spec("never", lambda c: {}),
        "off": spec("off", lambda c: {}),
    }
    config = cfg(ok=job(60), failing=job(60), never=job(60), off=job(60, enabled=False))
    sched = make(tmp_db, tmp_path, config, jobs)
    sched.sync_jobs()
    sched.run_once(only=["ok", "failing"], force=True)
    states = {r["name"]: r["state"] for r in S.status(tmp_db, config)}
    assert states == {"ok": "ok", "failing": "failing", "never": "never_ran", "off": "disabled"}
    # Three more failures: backoff.
    for _ in range(3):
        sched.run_once(only=["failing"], force=True)
    assert {r["name"]: r["state"] for r in S.status(tmp_db, config)}["failing"] == "backoff"
    # Time passes with no scheduler picking the job up: LATE, the "silently stopped" alarm.
    later = now_ms() + 10 * 60_000
    by_name = {r["name"]: r for r in S.status(tmp_db, config, now=later)}
    assert by_name["ok"]["state"] == "late"
    assert by_name["ok"]["since_success_s"] >= 590 and by_name["ok"]["next_in_s"] < 0
    # A job that keeps running but has not succeeded lately is STALE, not OK.
    tmp_db.execute("UPDATE ops_jobs SET next_due_ms=?, last_success_ms=? WHERE name='ok'",
                   (later + 30_000, later - 10 * 60_000))
    assert {r["name"]: r["state"] for r in S.status(tmp_db, config, now=later)}["ok"] == "stale"
    assert all(r["why"] == "" for r in S.status(tmp_db, config))  # test jobs are not in JOBS


def test_quota_ledger_is_per_job_and_per_utc_day(tmp_db):
    day = S.utc_day(now_ms())
    assert S.quota_used(tmp_db, "w", day) == (0, 0)
    S.quota_add(tmp_db, "w", day, units=3, credits=375)
    S.quota_add(tmp_db, "w", day, units=1, credits=125)
    assert S.quota_used(tmp_db, "w", day) == (4, 500)
    assert S.quota_used(tmp_db, "w", "1999-01-01") == (0, 0)
    assert S.quota_used(tmp_db, "other", day) == (0, 0)
    assert S.utc_day(0) == "1970-01-01"


# --------------------------------------------------------------------------- targeting


def test_ungraded_buyers_targets_buyers_seen_on_scanned_tokens_most_confluent_first(tmp_db):
    ts = now_ms()
    seed_swap(tmp_db, "A", "T1", ts_ms=ts - 3000)
    seed_swap(tmp_db, "A", "T2", ts_ms=ts - 2000)
    seed_swap(tmp_db, "B", "T1", ts_ms=ts - 1000)
    seed_swap(tmp_db, "C", "T1", ts_ms=ts)  # graded already
    seed_score(tmp_db, "C")
    seed_swap(tmp_db, "D", "T1", ts_ms=ts)  # blacklisted
    seed_wallet(tmp_db, "D", "blacklist")
    seed_swap(tmp_db, "E", "T1", ts_ms=ts, source="helius:backfill")  # not a scanned-token buyer
    seed_swap(tmp_db, "F", "T1", ts_ms=ts, side="sell")  # a seller is not a buyer
    seed_swap(tmp_db, "G", "T1", ts_ms=ts)  # attempted yesterday, failed
    S._mark_attempted(tmp_db, Chain.SOL, "G", "boom")
    rows = S.ungraded_buyers(tmp_db, Chain.SOL, limit=10)
    assert [r["wallet"] for r in rows] == ["A", "B"]
    assert rows[0]["tokens"] == 2
    # The attempt marker expires: eight days on, G is worth paying for again. Asserted a
    # clear week past the window rather than one millisecond past it — the marker carries
    # its own now_ms(), a few ms after `ts`, so a boundary-exact assertion here would be
    # testing the test's clock rather than the scheduler's rule.
    # Order is confluence first, then recency: A saw two tokens, and G's buy is newer
    # than B's, so G outranks B once it is eligible again.
    rows = S.ungraded_buyers(tmp_db, Chain.SOL, limit=10, now=ts + 8 * 86_400_000)
    assert [r["wallet"] for r in rows] == ["A", "G", "B"]
    assert [r["wallet"] for r in S.ungraded_buyers(tmp_db, Chain.SOL, limit=1)] == ["A"]


def test_the_wallet_buyers_job_backfills_grades_and_respects_its_daily_quota(tmp_db, monkeypatch):
    from kaiba.ingest import backfill as BF
    from kaiba.intelligence import grade as GR

    ts = now_ms()
    for w in ("A", "B", "C", "D", "E"):
        seed_swap(tmp_db, w, "T1", ts_ms=ts)
    backfilled: list[list[str]] = []

    def fake_backfill(conn: Any, chain: Chain, *, wallets: list[str], **kw: Any) -> BF.BackfillReport:
        backfilled.append(list(wallets))
        # The per-run ceiling tracks the wallets this run may actually touch, so it falls
        # with the remaining quota (2 wallets -> 500, the last 1 -> 250). Asserting a
        # constant here was wrong: it only held on the first of the three runs below.
        assert kw["max_credits"] == len(wallets) * 2 * S.CREDITS_PER_WALLET_EST
        report = BF.BackfillReport(chain=chain, credits_spent=125 * len(wallets))
        for w in wallets:
            report.results.append(BF.WalletResult(wallet=w, chain=chain, error="rpc" if w == "B" else None))
        return report

    graded: list[str] = []

    def fake_grade(address: str, chain: Any, conn: Any) -> SimpleNamespace:
        graded.append(address)
        seed_score(conn, address, grade="C")
        return SimpleNamespace(grade="C")

    monkeypatch.setattr(BF, "backfill_wallets", fake_backfill)
    monkeypatch.setattr(GR, "grade_address", fake_grade)
    params = {"wallets_per_run": 2, "wallets_per_day": 3}

    out = S.job_wallet_buyers(ctx_for(tmp_db, "wallet_buyers", params))
    assert backfilled == [["A", "B"]]
    assert graded == ["A"]  # B's backfill failed, so it is not graded on thin evidence
    assert out["wallets"] == 2 and out["graded"] == 1 and out["by_grade"] == {"C": 1}
    assert out["backfill_credits"] == 250 and out["used_today"] == 2
    assert S.quota_used(tmp_db, "wallet_buyers", S.utc_day(now_ms())) == (2, 250)

    # Second run the same day: one wallet left in the quota, and B is not retried.
    out = S.job_wallet_buyers(ctx_for(tmp_db, "wallet_buyers", params))
    assert backfilled[-1] == ["C"] and out["wallets"] == 1

    # Third run: quota reached, nothing spent, nothing called.
    out = S.job_wallet_buyers(ctx_for(tmp_db, "wallet_buyers", params))
    assert out["reason"] == "daily_quota_reached" and out["wallets"] == 0
    assert len(backfilled) == 2


def test_the_wallet_buyers_job_lets_the_credit_room_bind_the_run(tmp_db, monkeypatch):
    """MUTATION FINDING 2026-09-22 (round 2): with the count quota loose, dropping
    ``allow = min(allow, room.credits // CREDITS_PER_WALLET_EST)`` and handing the backfill
    ``room.run_max`` instead of ``min(run_max, room.credits)`` both survived every test in
    this file, because no test had credits binding tighter than the count. The shipped
    numbers on the live ledger, with 9,800 of the day's 10,000 already booked."""
    from kaiba.ingest import backfill as BF
    from kaiba.intelligence import grade as GR
    from kaiba.providers import helius

    ts = now_ms()
    for w in ("A", "B", "C", "D", "E"):
        seed_swap(tmp_db, w, "T1", ts_ms=ts)
    monkeypatch.setattr(helius, "budget_status",
                        lambda conn=None, period=None: {"remaining": 995_849, "resets_in_s": 10 * 86_400})
    calls: list[dict[str, Any]] = []

    def fake_backfill(conn: Any, chain: Chain, *, wallets: list[str], **kw: Any) -> BF.BackfillReport:
        calls.append({"wallets": list(wallets), "max_credits": kw["max_credits"]})
        report = BF.BackfillReport(chain=chain, credits_spent=110 * len(wallets))
        for w in wallets:
            report.results.append(BF.WalletResult(wallet=w, chain=chain))
        return report

    monkeypatch.setattr(BF, "backfill_wallets", fake_backfill)
    monkeypatch.setattr(GR, "grade_address", lambda a, c, conn: SimpleNamespace(grade="C"))
    params = {"wallets_per_run": 4, "wallets_per_day": 120, "credits_per_day": 10_000,
              "max_credits_per_run": 1_000}
    today = S.utc_day(now_ms())
    S.quota_add(tmp_db, "wallet_buyers", today, units=80, credits=9_800)
    out = S.job_wallet_buyers(ctx_for(tmp_db, "wallet_buyers", params))
    # 200 credits left in the day is room for one wallet at the planning 125, not four,
    # and the backfill is told the 200, not the 1,000 run cap.
    assert calls == [{"wallets": ["A"], "max_credits": 200}]
    assert out["wallets"] == 1 and out["credit_room"]["binding"] == "daily_ceiling"
    assert out["credits_today"] == 9_910 and S.quota_used(tmp_db, "wallet_buyers", today) == (81, 9_910)
    # 90 left is under one wallet: nothing is called, and the reason names the limit.
    out = S.job_wallet_buyers(ctx_for(tmp_db, "wallet_buyers", params))
    assert out["reason"] == S.REASON_QUOTA and out["binding"] == "daily_ceiling" and out["wallets"] == 0
    assert len(calls) == 1


def test_gather_credit_room_binds_on_the_tightest_of_four_limits(tmp_db, monkeypatch):
    """The credit ceiling itself, limit by limit. MUTATION FINDING 2026-09-22: before this
    test, ``binding = max(...)``, a removed run cap, a daily ceiling or pace that ignored
    today's spend, and a hard max of 50% of the month all passed the module's tests --
    the shipped-config tests only checked the YAML numbers, never the arithmetic that
    applies them. The ledger figures are the live box's on 2026-09-22 (MEASURED: 995,849
    remaining, 10 days to reset, 1,650 spent by wallet_buyers that day).

    Which constant is right, and why this test failed on 2026-09-22 with the module at
    ``FREE_MONTHLY_CREDITS // 2``: a clamp guards only if it sits between the shipped
    ceiling (10,000) and that ceiling's one-zero typo (100,000). 5% (50,000) does; 50%
    (500,000) lets the typo through and would let one job spend 71% of the credits above
    the floor in a day. The module comment and the YAML both said 5%, so the code was the
    odd one out and is what changed."""
    from kaiba.providers import helius

    ledger = {"remaining": 995_849, "resets_in_s": 10 * 86_400}
    monkeypatch.setattr(helius, "budget_status", lambda conn=None, period=None: dict(ledger))
    config = S.ScheduleConfig(helius=S.HeliusGate(floor_credits=300_000))
    today = S.utc_day(now_ms())

    def room(name: str, params: dict[str, Any], run_max: int) -> S.CreditRoom:
        return S.gather_credit_room(ctx_for(tmp_db, name, params, config), daily_default=3_750, run_max=run_max)

    S.quota_add(tmp_db, "wallet_buyers", today, units=15, credits=1_650)
    # 1. The shipped YAML on the live ledger: the run cap is the tightest. 1,000 credits is
    #    room for 8 wallets at the planning estimate; the YAML's wallets_per_run 4 is what
    #    makes the run 4 (max_credits_per_run is 2x the estimate for 4, the file's rule).
    r = room("wallet_buyers", {"credits_per_day": 10_000}, 1_000)
    assert r.binding == "run_size" and r.credits == 1_000
    assert r.days_left == 10 and r.pace_per_day == (995_849 - 300_000) // 10
    assert r.remaining == 995_849 and r.floor == 300_000 and r.used_today == 1_650 and not r.clamped
    assert r.credits // S.CREDITS_PER_WALLET_EST == 8
    assert min(4, r.credits // S.CREDITS_PER_WALLET_EST) == 4
    # 2. A looser run cap is bound by what is left of the day: 10,000 - 1,650.
    r = room("wallet_buyers", {"credits_per_day": 10_000}, 20_000)
    assert r.binding == "daily_ceiling" and r.credits == 8_350
    # 3. Today's spend is read from the database, so booking the rest closes the day.
    S.quota_add(tmp_db, "wallet_buyers", today, units=0, credits=8_350)
    r = room("wallet_buyers", {"credits_per_day": 10_000}, 20_000)
    assert r.binding == "daily_ceiling" and r.credits == 0 and r.used_today == 10_000
    # 4. A typo that adds a zero is clamped to the hard max (5% of the month) and says so.
    assert S.GATHER_DAILY_CREDIT_HARD_MAX * 20 == S.FREE_MONTHLY_CREDITS
    r = room("wallet_buyers", {"credits_per_day": 100_000}, 10**9)
    assert r.clamped and r.daily_ceiling == S.GATHER_DAILY_CREDIT_HARD_MAX == 50_000
    assert r.binding == "daily_ceiling" and r.credits == 50_000 - 10_000
    assert r.as_dict()["ceiling_clamped"] is True
    #    A ceiling exactly AT the hard max is not a typo and must not read as clamped.
    r = room("wallet_buyers", {"credits_per_day": S.GATHER_DAILY_CREDIT_HARD_MAX}, 10**9)
    assert not r.clamped and r.daily_ceiling == S.GATHER_DAILY_CREDIT_HARD_MAX
    # 5. The last day of a month with 5,000 above the floor: the pace binds, net of today.
    ledger.update(remaining=305_000, resets_in_s=3_600)
    S.quota_add(tmp_db, "paced", today, units=0, credits=2_000)
    r = room("paced", {"credits_per_day": 50_000}, 10**9)
    assert r.days_left == 1 and r.pace_per_day == 5_000
    assert r.binding == "monthly_pace" and r.credits == 3_000
    #    A partial day counts as a day, or the pace over-spends the last one: 36 h to the
    #    reset is two days of pace, not one.
    ledger.update(remaining=305_000, resets_in_s=36 * 3_600)
    r = room("paced", {"credits_per_day": 50_000}, 10**9)
    assert r.days_left == 2 and r.pace_per_day == 2_500 and r.credits == 500
    # 6. At the floor: nothing, and the reason is the floor's (a tie goes to the earlier key).
    ledger.update(remaining=300_000)
    r = room("paced", {"credits_per_day": 50_000}, 10**9)
    assert r.binding == S.REASON_BUDGET_FLOOR and r.credits == 0
    # 7. An unreadable ledger is zero credits with its own reason, never the run cap.
    monkeypatch.setattr(helius, "budget_status", lambda conn=None, period=None: (_ for _ in ()).throw(OSError("x")))
    r = room("paced", {"credits_per_day": 50_000}, 1_000)
    assert r.binding == S.REASON_BUDGET_UNREADABLE and r.credits == 0 and r.run_max == 1_000
    assert r.as_dict()["pace_per_day"] == "UNAVAILABLE" and r.as_dict()["remaining"] == "UNAVAILABLE"


def test_the_tape_model_version_is_the_graders_own():
    """The exclusion below keys on this string; a drift between the two modules would
    silently make tape grades count as real ones again."""
    from kaiba.intelligence import grade as GR

    assert S.TAPE_MODEL_VERSION == GR.MODEL_ID_TAPE


def test_a_tape_grade_does_not_make_a_wallet_graded_for_the_paid_queue(tmp_db):
    """Prerequisite for ``wallet_tape.store`` ever being true: a stored tape grade must not
    silently drop the wallet out of the queue that would buy it real evidence."""
    from kaiba.intelligence import grade as GR

    ts = now_ms()
    seed_swap(tmp_db, "TAPE", "T1", ts_ms=ts)
    seed_swap(tmp_db, "TAPE", "T2", ts_ms=ts - 1)
    seed_score(tmp_db, "TAPE", grade="B", model_version=GR.MODEL_ID_TAPE)
    seed_swap(tmp_db, "FULL", "T1", ts_ms=ts)
    seed_score(tmp_db, "FULL", grade="B", model_version=GR.MODEL_ID)
    seed_swap(tmp_db, "NONE", "T1", ts_ms=ts - 2)
    # The original targeting query, the signal-ordered queue and the free BSC path.
    assert [r["wallet"] for r in S.ungraded_buyers(tmp_db, Chain.SOL, limit=10)] == ["TAPE", "NONE"]
    assert [r["wallet"] for r in S.gather_queue(tmp_db, Chain.SOL, limit=10, now=ts).rows] == ["TAPE", "NONE"]
    bsc = Chain.BSC.value
    tmp_db.execute(
        "INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, amount_token, source) "
        "VALUES (?,?,?,?,?,?,?,?)", (bsc, "tx-b1", ts, "BTAPE", "B1", "buy", "1", "gmgn:smartmoney"),
    )
    tmp_db.execute(
        "INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, amount_token, source) "
        "VALUES (?,?,?,?,?,?,?,?)", (bsc, "tx-b2", ts, "BFULL", "B1", "buy", "1", "gmgn:smartmoney"),
    )
    seed_score(tmp_db, "BTAPE", grade="B", model_version=GR.MODEL_ID_TAPE, chain=bsc)
    seed_score(tmp_db, "BFULL", grade="B", model_version=GR.MODEL_ID, chain=bsc)
    assert S.free_grade_targets(tmp_db, Chain.BSC, limit=10) == ["BTAPE"]


def test_the_regrade_job_never_hands_a_tape_grade_to_the_full_history_model(tmp_db, monkeypatch):
    """The other prerequisite: ``grade_address`` over the same thin tape would replace the
    provisional grade with one that then looks measured forever."""
    from kaiba.intelligence import grade as GR

    ts = now_ms()
    seed_score(tmp_db, "TAPE-OLD", grade="B", scored_at_ms=ts - 30 * 86_400_000, model_version=GR.MODEL_ID_TAPE)
    seed_swap(tmp_db, "TAPE-OLD", "T1", ts_ms=ts)  # new evidence too, so both stale rules fire
    seed_score(tmp_db, "FULL-OLD", grade="D", scored_at_ms=ts - 30 * 86_400_000, model_version=GR.MODEL_ID)
    assert S.stale_scores(tmp_db, Chain.SOL, max_age_ms=7 * 86_400_000, limit=10, now=ts) == ["FULL-OLD"]
    seen: list[str] = []

    def fake_grade(address: str, chain: Any, conn: Any) -> SimpleNamespace:
        seen.append(address)
        seed_score(conn, address, grade="D", model_version=GR.MODEL_ID)
        return SimpleNamespace(grade="D")

    monkeypatch.setattr(GR, "grade_address", fake_grade)
    out = S.job_wallet_regrade(ctx_for(tmp_db, "wallet_regrade", {"max_age_days": 7}))
    assert seen == ["FULL-OLD"] and out["candidates"] == 1
    row = fetch_one(tmp_db, "SELECT model_version, grade FROM wallet_scores WHERE address='TAPE-OLD'")
    assert row["model_version"] == GR.MODEL_ID_TAPE and row["grade"] == "B"


def test_stale_scores_finds_moved_evidence_and_old_grades(tmp_db):
    ts = now_ms()
    seed_score(tmp_db, "OLD", scored_at_ms=ts - 30 * 86_400_000)
    seed_score(tmp_db, "BACKFILLED", scored_at_ms=ts - 60_000)
    tmp_db.execute("INSERT INTO kv (key, value, updated_ms) VALUES (?, '{}', ?)",
                   (f"backfill:swaps:{SOL}:BACKFILLED", ts))
    seed_score(tmp_db, "TRADED", scored_at_ms=ts - 60_000)
    seed_swap(tmp_db, "TRADED", "T1", ts_ms=ts)
    seed_score(tmp_db, "FRESH", scored_at_ms=ts - 60_000)
    seed_swap(tmp_db, "FRESH", "T1", ts_ms=ts - 120_000)
    found = S.stale_scores(tmp_db, Chain.SOL, max_age_ms=7 * 86_400_000, limit=10, now=ts)
    assert found == ["OLD", "BACKFILLED", "TRADED"]


def test_stale_scores_never_hands_a_provider_grade_to_the_full_history_grader(tmp_db):
    # MEASURED 2026-09-29: regrade had turned 22 EVM GMGN B grades into ungraded rows, and all
    # 11,099 GMGN provider rows reach the 7-day age on 10-01.
    ts = now_ms()
    seed_score(tmp_db, "OLD", scored_at_ms=ts - 30 * 86_400_000)
    seed_score(tmp_db, "GMGN", scored_at_ms=ts - 30 * 86_400_000,
               model_version=S.GMGN_PROVIDER_MODEL_VERSION)
    found = S.stale_scores(tmp_db, Chain.SOL, max_age_ms=7 * 86_400_000, limit=10, now=ts)
    assert found == ["OLD"]


def test_the_scheduler_spells_the_provider_model_version_like_the_grader():
    from kaiba.intelligence import gmgn_grade
    assert S.GMGN_PROVIDER_MODEL_VERSION == gmgn_grade.MODEL_ID_PROVIDER


def test_the_regrade_job_counts_grade_changes(tmp_db, monkeypatch):
    from kaiba.intelligence import grade as GR

    ts = now_ms()
    seed_score(tmp_db, "OLD", grade="D", scored_at_ms=ts - 30 * 86_400_000)
    seed_score(tmp_db, "OLD2", grade="D", scored_at_ms=ts - 30 * 86_400_000)

    def fake_grade(address: str, chain: Any, conn: Any) -> SimpleNamespace:
        new = "A" if address == "OLD" else "D"
        seed_score(conn, address, grade=new)
        return SimpleNamespace(grade=new)

    monkeypatch.setattr(GR, "grade_address", fake_grade)
    out = S.job_wallet_regrade(ctx_for(tmp_db, "wallet_regrade", {"max_age_days": 7}))
    assert out["candidates"] == 2 and out["graded"] == 2 and out["changed"] == 1
    assert out["by_grade"] == {"A": 1, "D": 1}
    assert S.job_wallet_regrade(ctx_for(tmp_db, "wallet_regrade", {"max_age_days": 7}))["candidates"] == 0


def test_the_trade_flow_job_collects_only_tokens_whose_flow_is_missing_or_old(tmp_db, monkeypatch):
    from kaiba.ingest import token_flow as TF

    ts = now_ms()
    seed_dossier(tmp_db, "NEW", built_ms=ts - 60_000, created_ms=ts - 90_000)  # never collected
    seed_dossier(tmp_db, "FRESH", built_ms=ts - 60_000, created_ms=ts - 90_000)
    seed_swap(tmp_db, "W", "FRESH", ts_ms=ts - 30_000, source=TF.SOURCE)  # collected 30 s ago
    seed_dossier(tmp_db, "OLDFLOW", built_ms=ts - 3_600_000, created_ms=None)
    seed_swap(tmp_db, "W", "OLDFLOW", ts_ms=ts - 1_800_000, source=TF.SOURCE)  # 30 min ago
    seed_dossier(tmp_db, "ANCIENT", built_ms=ts - 3 * 3_600_000, created_ms=None)  # outside lookback
    calls: list[tuple[str, int | None, int | None]] = []

    def fake_collect(chain: Chain, token: str, conn: Any, *, since_ms: int | None, created_ms: int | None,
                     max_pages: int) -> TF.FlowResult:
        calls.append((token, since_ms, created_ms))
        assert max_pages == 2
        return TF.FlowResult(chain=chain, token=token, pages=1, rows_written=3, reason="end_of_history")

    monkeypatch.setattr(TF, "collect_trades", fake_collect)
    out = S.job_trade_flow(ctx_for(tmp_db, "trade_flow", {"lookback_s": 7200, "refresh_s": 600}))
    assert [c[0] for c in calls] == ["NEW", "OLDFLOW"]
    assert calls[0][1] is None and calls[0][2] == ts - 90_000
    assert calls[1][1] == ts - 1_800_000
    assert out == {"candidates": 2, "tokens": 2, "ok_tokens": 2, "pages": 2, "rows_written": 6,
                   "unavailable": 0, "stopped": None}


def test_the_trade_flow_job_stops_when_the_route_is_down_from_the_start(tmp_db, monkeypatch):
    from kaiba.ingest import token_flow as TF

    ts = now_ms()
    for i in range(8):
        seed_dossier(tmp_db, f"T{i}", built_ms=ts - i * 1000, created_ms=None)
    calls: list[str] = []

    def down(chain: Chain, token: str, conn: Any, **kw: Any) -> TF.FlowResult:
        calls.append(token)
        return TF.FlowResult(chain=chain, token=token, reason="unavailable")

    monkeypatch.setattr(TF, "collect_trades", down)
    out = S.job_trade_flow(ctx_for(tmp_db, "trade_flow", {}))
    # Nothing worked at all, so three in a row is enough to call it an outage.
    assert len(calls) == 3 and out["stopped"] == "provider_unavailable" and out["ok_tokens"] == 0


def test_mints_with_no_trade_history_do_not_look_like_an_outage(tmp_db, monkeypatch):
    """MEASURED 2026-09-20: 6 of 10 scanned tokens answered 'unavailable' on a pass that
    still wrote 601 rows from the rest. A 3-in-a-row breaker stopped later runs doing the
    work that would have succeeded."""
    from kaiba.ingest import token_flow as TF

    ts = now_ms()
    for i in range(10):
        seed_dossier(tmp_db, f"T{i}", built_ms=ts - i * 1000, created_ms=None)
    # One good token, then four dead mints, then more: the streak must not trip at 3.
    good = {"T0", "T5"}
    calls: list[str] = []

    def mixed(chain: Chain, token: str, conn: Any, **kw: Any) -> TF.FlowResult:
        calls.append(token)
        if token in good:
            return TF.FlowResult(chain=chain, token=token, pages=1, rows_written=7, reason="end_of_history")
        return TF.FlowResult(chain=chain, token=token, reason="unavailable")

    monkeypatch.setattr(TF, "collect_trades", mixed)
    out = S.job_trade_flow(ctx_for(tmp_db, "trade_flow", {}))
    assert out["stopped"] is None and len(calls) == 10
    assert out["ok_tokens"] == 2 and out["unavailable"] == 8 and out["rows_written"] == 14

    # But a long streak after a success is still an outage.
    good = {"T0"}
    calls.clear()
    out = S.job_trade_flow(ctx_for(tmp_db, "trade_flow", {"unavailable_streak": 5}))
    assert out["stopped"] == "provider_unavailable" and out["ok_tokens"] == 1 and len(calls) == 6


def test_the_journal_job_flags_a_broken_chain(tmp_db):
    from kaiba.core import journal

    journal.append("observation", "first", conn=tmp_db)
    journal.append("observation", "second", conn=tmp_db)
    assert S.job_journal_verify(ctx_for(tmp_db, "journal_verify", {})) == {"entries": 2, "chain_ok": True}
    tmp_db.execute("UPDATE journal SET body='tampered' WHERE seq=1")
    with pytest.raises(S.JobFailed) as exc:
        S.job_journal_verify(ctx_for(tmp_db, "journal_verify", {}))
    assert "chain broken" in str(exc.value) and exc.value.result == {"entries": 2, "chain_ok": False}


def test_the_budget_job_reports_and_warns_below_the_floor(tmp_db, monkeypatch):
    from kaiba.core.events import recent
    from kaiba.providers import helius

    monkeypatch.setattr(helius, "budget_status", lambda conn=None, period=None: {
        "period": "2026-09", "allowance": 1_000_000, "used": 800_000, "remaining": 200_000,
        "pct_used": 80.0, "calls": 10, "denied_calls": 0, "estimated_credits": 0, "resets_in_s": 100,
    })
    config = S.ScheduleConfig(helius=S.HeliusGate(floor_credits=300_000))
    out = S.job_budget_check(ctx_for(tmp_db, "budget_check", {}, config))
    assert out["below_floor"] is True and out["remaining"] == 200_000 and out["floor"] == 300_000
    ev = recent(limit=5, kinds=["provider.budget"])
    assert ev and ev[0].payload["state"] == "below_ops_floor" and ev[0].level == "warn"
    # Same day, same period: one warning, not one per run.
    S.job_budget_check(ctx_for(tmp_db, "budget_check", {}, config))
    assert len(recent(limit=5, kinds=["provider.budget"])) == 1


def test_the_native_price_job_samples_one_pass_per_configured_chain(tmp_db, monkeypatch):
    from kaiba.providers import native_price as NP

    calls: list[dict[str, Any]] = []

    def fake_sampler(chains: Any, *, conn: Any, max_iterations: int, wait_for_slot_s: float):
        calls.append({"chains": list(chains), "conn": conn, "max_iterations": max_iterations,
                      "wait": wait_for_slot_s})
        report = NP.SamplerReport()
        for ch in chains:
            report.bump(ch, "recorded")
        report.iterations = 1
        return report

    monkeypatch.setattr(NP, "run_sampler", fake_sampler)
    out = S.job_native_price(ctx_for(tmp_db, "native_price", {"chains": ["sol"]}))
    # One pass only: the scheduler is the clock, not a second loop inside the job.
    assert calls[0]["max_iterations"] == 1 and calls[0]["conn"] is tmp_db
    assert calls[0]["chains"] == [Chain.SOL]
    assert out["recorded"] == 1 and out["failures"] == 0
    assert out["per_chain"] == {"sol": {"recorded": 1, "duplicates": 0, "failures": 0}}

    # A chain with no reference pool, or no such chain at all, is reported not skipped.
    out = S.job_native_price(ctx_for(tmp_db, "native_price", {"chains": "sol,robinhood,nosuchchain"}))
    assert calls[-1]["chains"] == [Chain.SOL]
    # robinhood is a real chain with no reference pool; nosuchchain is not a chain at all.
    assert sorted(out["unsupported"]) == ["nosuchchain", "robinhood"]
    with pytest.raises(S.JobFailed):
        S.job_native_price(ctx_for(tmp_db, "native_price", {"chains": ["robinhood"]}))

    # A pass where every chain failed is a failure, not a quiet success.
    def all_fail(chains: Any, **kw: Any):
        report = NP.SamplerReport()
        for ch in chains:
            report.bump(ch, "failures")
        return report

    monkeypatch.setattr(NP, "run_sampler", all_fail)
    with pytest.raises(S.JobFailed) as exc:
        S.job_native_price(ctx_for(tmp_db, "native_price", {"chains": ["sol"]}))
    assert exc.value.result["failures"] == 1


def test_the_native_price_job_reports_a_missing_module_rather_than_guessing(tmp_db, monkeypatch):
    """The state this job shipped in: the module was another agent's and had not landed."""
    import kaiba.providers as providers_pkg

    name = "kaiba.providers.native_price"
    monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.delattr(providers_pkg, "native_price", raising=False)
    with pytest.raises(S.EntryPointMissing):
        S.job_native_price(ctx_for(tmp_db, "native_price", {}))

    empty = types.ModuleType(name)
    monkeypatch.setitem(sys.modules, name, empty)
    monkeypatch.setattr(providers_pkg, "native_price", empty, raising=False)
    with pytest.raises(S.EntryPointMissing) as exc:
        S.job_native_price(ctx_for(tmp_db, "native_price", {}))
    assert "run_sampler" in str(exc.value)


def test_the_signals_job_polls_one_source_at_a_time_and_summarises_health(tmp_db, monkeypatch):
    from kaiba.hunters import signals

    calls: list[list[str]] = []
    monkeypatch.setattr(signals, "due_sources", lambda conn, cfg, **kw: ["crtsh", "okx", "aevo"])
    monkeypatch.setattr(signals, "refresh",
                        lambda conn=None, only=None, **kw: calls.append(list(only)) or 2)
    monkeypatch.setattr(signals, "source_health", lambda conn=None, cfg=None: [
        {"source": "okx", "state": "ok"}, {"source": "crtsh", "state": "dead"},
        {"source": "aevo", "state": "never_run"},
    ])
    out = S.job_signals(ctx_for(tmp_db, "signals", {}))
    # One call per source, never one call for all of them.
    assert calls == [["crtsh"], ["okx"], ["aevo"]]
    assert out["new"] == 6 and out["polled"] == ["crtsh", "okx", "aevo"] and out["deferred"] == []
    assert out["states"] == {"ok": 1, "dead": 1, "never_run": 1} and out["dead"] == ["crtsh"]


def test_a_source_that_ate_a_whole_run_goes_last_on_the_next_one(tmp_db, monkeypatch):
    """Without this, a source that consumes every run (crt.sh, measured at ~250 s) leads
    every run, and the sources behind it are never reached."""
    from kaiba.hunters import signals

    monkeypatch.setattr(signals, "due_sources", lambda conn, cfg, **kw: ["crtsh", "okx", "aevo"])
    monkeypatch.setattr(signals, "source_health", lambda conn=None, cfg=None: [])

    # Run 1: crtsh consumes the whole budget, so the others are deferred, not lost.
    def hog(conn=None, only=None, **kw):
        if only == ["crtsh"]:
            ctx.deadline_ms = ctx.now()  # crtsh ate the run
        return 0

    monkeypatch.setattr(signals, "refresh", hog)
    ctx = ctx_for(tmp_db, "signals", {"per_source_reserve_s": 0.5})
    out = S.job_signals(ctx)
    assert out["polled"] == ["crtsh"] and out["deferred"] == ["okx", "aevo"]

    # Run 2: crtsh is the most recently tried, so the starved sources go first.
    calls: list[list[str]] = []
    monkeypatch.setattr(signals, "refresh", lambda conn=None, only=None, **kw: calls.append(list(only)) or 0)
    S.job_signals(ctx_for(tmp_db, "signals", {}))
    assert calls == [["okx"], ["aevo"], ["crtsh"]]


def test_the_prune_jobs_bound_their_tables(tmp_db):
    old = now_ms() - 30 * 86_400_000
    tmp_db.execute("INSERT INTO ops_runs (job, started_ms, status) VALUES ('a', ?, 'ok')", (old,))
    tmp_db.execute("INSERT INTO ops_runs (job, started_ms, status) VALUES ('a', ?, 'ok')", (now_ms(),))
    S.quota_add(tmp_db, "a", "2020-01-01", units=1, credits=1)
    S.quota_add(tmp_db, "a", S.utc_day(now_ms()), units=1, credits=1)
    out = S.job_ops_prune(ctx_for(tmp_db, "ops_prune", {}))
    assert out["runs_deleted"] == 1 and out["quota_rows_deleted"] == 1
    assert fetch_one(tmp_db, "SELECT COUNT(*) AS n FROM ops_runs")["n"] == 1

    from kaiba.ingest.token_flow import DEFAULT_CONFIG

    stale = now_ms() - (DEFAULT_CONFIG.snapshot_max_age_s + 60) * 1000
    tmp_db.execute(
        "INSERT INTO curve_snapshots (chain, token, observed_ms, real_sol_lamports, virtual_sol_lamports, "
        "real_token_atoms, virtual_token_atoms) VALUES ('sol', 'T', ?, 1, 1, '1', '1')", (stale,),
    )
    out = S.job_snapshot_prune(ctx_for(tmp_db, "snapshot_prune", {}))
    assert out == {"deleted": 1, "remaining": 0}


# --------------------------------------------------------------------------- CLI and deploy


runner = CliRunner()


def test_cli_ops_commands_are_registered_and_status_reads_an_empty_table(tmp_db):
    from kaiba.cli.main import app

    result = runner.invoke(app, ["ops", "run", "--help"])
    assert result.exit_code == 0, result.stdout
    for option in ("--once", "--only", "--force", "--config"):
        assert option in result.stdout
    result = runner.invoke(app, ["ops", "status"])
    assert result.exit_code == 0, result.stdout
    assert "no jobs recorded yet" in result.stdout
    result = runner.invoke(app, ["ops", "status", "--json"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["jobs"] == [] and payload["service"] is None


def test_cli_ops_run_once_executes_a_real_job_against_a_custom_schedule(tmp_db, tmp_path):
    from kaiba.cli.main import app

    path = tmp_path / "schedule.yaml"
    path.write_text(
        "jitter_pct: 0\njobs:\n  journal_verify:\n    interval_s: 60\n    timeout_s: 30\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["ops", "run", "--once", "--force", "--config", str(path), "--json"])
    assert result.exit_code == 0, result.stdout
    (outcome,) = json.loads(result.stdout)
    assert outcome["job"] == "journal_verify" and outcome["status"] == "ok"
    assert outcome["result"] == {"entries": 0, "chain_ok": True}
    result = runner.invoke(app, ["ops", "status", "--runs", "5"])
    assert result.exit_code == 0, result.stdout
    assert "journal_verify" in result.stdout and "OK" in result.stdout
    # Not due again: a second --once without --force runs nothing.
    result = runner.invoke(app, ["ops", "run", "--once", "--config", str(path)])
    assert result.exit_code == 0 and "nothing due" in result.stdout
    result = runner.invoke(app, ["ops", "run", "--once", "--config", str(path), "--only", "nope"])
    assert result.exit_code == 1 and "unknown job" in result.stdout


def test_cli_ops_run_configures_logging_so_the_service_is_visible_in_journalctl(tmp_db, tmp_path,
                                                                                monkeypatch):
    """A working scheduler that logs nothing is indistinguishable from a stopped one."""
    from kaiba.cli.main import app
    from kaiba.ops import scheduler as sched_mod

    path = tmp_path / "schedule.yaml"
    path.write_text("jobs:\n  ops_prune:\n    interval_s: 60\n    timeout_s: 30\n", encoding="utf-8")
    seen: dict[str, Any] = {}
    monkeypatch.setattr("logging.basicConfig", lambda **kw: seen.update(kw))
    monkeypatch.setattr(sched_mod.Scheduler, "run_forever", lambda self, only=None: 0)
    result = runner.invoke(app, ["ops", "run", "--config", str(path)])
    assert result.exit_code == 0, result.stdout
    assert seen.get("level") and "%(levelname)" in seen.get("format", "")


def test_cli_status_stops_showing_an_error_once_the_job_recovers(tmp_db, tmp_path, monkeypatch):
    from kaiba.cli.main import app

    monkeypatch.setenv("COLUMNS", "300")  # else rich truncates the detail column to "explo…"
    state = {"fail": True}

    def flaky(ctx: S.JobContext) -> dict[str, Any]:
        if state["fail"]:
            raise RuntimeError("upstream exploded")
        return {"all": "well"}

    sched = make(tmp_db, tmp_path, cfg(journal_verify=job()), {"journal_verify": spec("j", flaky)})
    sched.run_once(force=True)
    assert "upstream exploded" in runner.invoke(app, ["ops", "status"]).stdout
    state["fail"] = False
    sched.run_once(force=True)
    out = runner.invoke(app, ["ops", "status"]).stdout
    # The row is green again, so it must not still be captioned with the old failure.
    assert "upstream exploded" not in out and "all" in out
    # ...but the database keeps it, because "what went wrong last time" is worth having.
    assert "upstream exploded" in (job_row(tmp_db, "journal_verify")["last_error"] or "")


def test_cli_ops_run_refuses_a_bad_schedule_loudly(tmp_db, tmp_path):
    from kaiba.cli.main import app

    result = runner.invoke(app, ["ops", "run", "--once", "--config", str(tmp_path / "missing.yaml")])
    assert result.exit_code == 1 and "does not exist" in result.stdout


def test_systemd_unit_and_local_runner_start_the_scheduler():
    text = (ROOT / "deploy" / "systemd" / "kaiba-ops.service").read_text(encoding="utf-8")
    exec_lines = [line for line in text.splitlines() if line.startswith("ExecStart=")]
    assert exec_lines == ["ExecStart=/opt/kaiba/venv/bin/python -I -B -m kaiba.cli.main ops run"]
    assert "PartOf=kaiba.target" in text and "Restart=always" in text
    # The lock directory must be created before the sandbox: a ReadWritePaths entry that
    # does not exist fails the unit, and the scheduler cannot mkdir under a read-only parent.
    assert "StateDirectory=kaiba/ops" in text
    assert "ReadWritePaths=/var/lib/kaiba/ops" not in text
    for hardening in ("NoNewPrivileges=yes", "ProtectSystem=strict", "UnsetEnvironment=KAIBA_SIGNER_PASSPHRASE"):
        assert hardening in text
    # The unit's stop timeout must exceed the scheduler's own drain window.
    stop = int(next(line for line in text.splitlines() if line.startswith("TimeoutStopSec=")).split("=")[1])
    assert stop > S.load_config(ROOT / "config" / "schedule.yaml").stop_grace_s

    # run-local.py is a script, not an importable module name (the hyphen), so it is
    # loaded by path. It must be in sys.modules *before* it executes: it uses
    # `from __future__ import annotations`, so @dataclass resolves Service's annotations
    # by looking its own module up by name, and finds None if we skip this.
    spec_ = importlib.util.spec_from_file_location("kaiba_run_local", ROOT / "deploy" / "run-local.py")
    assert spec_ is not None and spec_.loader is not None
    module = importlib.util.module_from_spec(spec_)
    sys.modules[spec_.name] = module
    try:
        spec_.loader.exec_module(module)
    finally:
        sys.modules.pop(spec_.name, None)
    ops = next(s for s in module.services() if s.name == "ops")
    assert ops.args == ["-m", "kaiba", "ops", "run"] and not ops.optional


# ------------------------------------------- tracking and clustering (wired 2026-09-20)
#
# Five jobs whose entry points existed, were tested, and ran on nobody's clock. The tests
# that carry weight here are the two numbers their authors were emphatic about — the
# Helius free-tier floor under ``tracker_poll`` and the reason ``clustering`` is daily —
# because both are the kind of figure that gets "tuned" later by somebody reading only
# the interval.


def poll_result(tracker: Any, route: str, subject: str, *, ok: bool = True, rows: int = 0,
                credits: int = 0, note: str = "") -> Any:
    return tracker.PollResult(
        route=route, subject=subject, ok=ok, rtt_ms=5, credits=credits, rows_seen=rows, note=note
    )


def test_tracker_poll_is_never_scheduled_below_the_helius_free_tier_floor():
    """260 s for ten wallets at 10 credits a poll IS the free tier, exactly.

    That is the whole finding of the tracking work, so it is asserted against the module's
    own arithmetic rather than left as a comment somebody can tune past.
    """
    from kaiba.intelligence import tracker

    config = S.load_config(ROOT / "config" / "schedule.yaml")
    assert config.jobs["tracker_poll"].interval_s >= 260
    assert tracker.max_free_tier_wallets(260) >= 10
    assert tracker.max_free_tier_wallets(130) < 10
    assert S.JOBS["tracker_poll"].spends_helius is True
    assert S.JOBS["tracker_sweep"].spends_helius is False


def test_clustering_is_daily_because_persist_edges_accumulates():
    """``cluster.persist_edges`` increments ``cluster_edges.observations`` on every run.

    An hourly rebuild would therefore inflate that column 24x a day over evidence that
    never changed, and ``observations`` is read as confidence. The reason has to travel
    with the interval, so the config is asserted to still carry it.
    """
    config = S.load_config(ROOT / "config" / "schedule.yaml")
    for name in ("clustering", "clustering_funding"):
        assert config.jobs[name].interval_s >= 86_400, name
    text = (ROOT / "config" / "schedule.yaml").read_text(encoding="utf-8")
    assert "observations" in text and "ACCUMULATES" in text


def test_the_two_clustering_jobs_are_split_so_the_free_rebuild_is_never_credit_gated():
    """A month where credits ran low must not be a month where confluence counts five
    addresses from one hand as five operators."""
    config = S.load_config(ROOT / "config" / "schedule.yaml")
    assert S.JOBS["clustering"].spends_helius is False
    assert S.JOBS["clustering_funding"].spends_helius is True
    assert "max_credits" not in config.jobs["clustering"].params


def test_the_shipped_funding_budget_is_the_share_of_the_month_it_claims_to_be():
    config = S.load_config(ROOT / "config" / "schedule.yaml")
    params = config.jobs["clustering_funding"].params
    per_day = int(params["credits_per_day"])
    assert per_day == int(params["max_credits"])
    assert (per_day * 31) / S.FREE_MONTHLY_CREDITS == pytest.approx(0.078, abs=0.002)


def test_the_sweep_job_treats_a_minority_of_503s_as_the_routes_normal_availability(
    tmp_db, monkeypatch
):
    """MEASURED 87.5% availability over 915 polls. A partial pass is the route working."""
    from kaiba.intelligence import tracker as TR

    sizes: list[int] = []

    def fake_sweep(chain: Any, conn: Any = None, *, mints: Any = None,
                   config: Any = TR.DEFAULT_CONFIG) -> list[Any]:
        sizes.append(config.token_sweep_size)
        return [
            poll_result(TR, TR.ROUTE_PUMPFUN, "M1", rows=100),
            poll_result(TR, TR.ROUTE_PUMPFUN, "M2", ok=False, note="503"),
        ]

    monkeypatch.setattr(TR, "sweep_tokens", fake_sweep)
    out = S.job_tracker_sweep(ctx_for(tmp_db, "tracker_sweep", {}))
    assert out["mints"] == 2 and out["ok"] == 1 and out["failed"] == 1
    assert out["rows_seen"] == 100 and out["credits"] == 0
    # No `mints` param means the module's own sweep size, not one this file invented.
    assert sizes == [TR.DEFAULT_CONFIG.token_sweep_size]
    S.job_tracker_sweep(ctx_for(tmp_db, "tracker_sweep", {"mints": 7}))
    assert sizes[-1] == 7


def test_the_sweep_job_fails_when_the_whole_route_is_down(tmp_db, monkeypatch):
    from kaiba.intelligence import tracker as TR

    monkeypatch.setattr(
        TR, "sweep_tokens",
        lambda chain, conn=None, **kw: [poll_result(TR, TR.ROUTE_PUMPFUN, "M1", ok=False)],
    )
    with pytest.raises(S.JobFailed):
        S.job_tracker_sweep(ctx_for(tmp_db, "tracker_sweep", {}))


def test_the_poll_job_reports_what_the_watchlist_it_polled_costs_for_a_month(
    tmp_db, monkeypatch
):
    """The projection is over ``pollable_addresses`` -- what Route W pays for -- not over
    every active row. Cohort wallets carry ``helius_poll: false`` because the GMGN feed
    that nominated them already delivers their trades; counting them over-stated the bill
    by the whole cohort (a 700-wallet cohort reads as ~2,000,000 credits a day)."""
    from kaiba.intelligence import tracker as TR

    monkeypatch.setattr(TR, "watched_addresses", lambda chain, conn=None: {"A", "B", "COHORT1", "COHORT2"})
    monkeypatch.setattr(TR, "pollable_addresses", lambda chain, conn=None: {"A", "B"})
    monkeypatch.setattr(
        TR, "poll_wallets",
        lambda chain, conn=None, **kw: [
            poll_result(TR, TR.ROUTE_HELIUS, "A", rows=5, credits=10),
            poll_result(TR, TR.ROUTE_HELIUS, "B", rows=0, credits=10),
        ],
    )
    out = S.job_tracker_poll(ctx_for(tmp_db, "tracker_poll", {"interval_s_for_projection": 300}))
    assert out["wallets"] == 2 and out["ok"] == 2 and out["credits"] == 20
    assert out["watched"] == 4 and out["cohort_only"] == 2
    # The operator must be able to see the monthly bill of the size they chose, in the
    # job result, without reaching for a calculator -- and it is the bill for the two
    # wallets the route polls, not the four on the list.
    assert out["monthly_credits_at_this_size"] == TR.projected_wallet_cost(2, 300)["credits"]
    assert out["monthly_credits_at_this_size"] < TR.projected_wallet_cost(4, 300)["credits"]


def test_the_poll_job_spends_nothing_on_an_empty_watchlist(tmp_db, monkeypatch):
    from kaiba.intelligence import tracker as TR

    monkeypatch.setattr(TR, "watched_addresses", lambda chain, conn=None: set())
    monkeypatch.setattr(TR, "pollable_addresses", lambda chain, conn=None: set())

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("an empty watchlist must not be polled")

    monkeypatch.setattr(TR, "poll_wallets", forbidden)
    out = S.job_tracker_poll(ctx_for(tmp_db, "tracker_poll", {}))
    assert out == {"route": TR.ROUTE_HELIUS, "wallets": 0, "reason": "watchlist_empty",
                   "watched": 0, "cohort_only": 0}
    # A watchlist that is ALL cohort wallets is empty for this route too: nothing is polled.
    monkeypatch.setattr(TR, "watched_addresses", lambda chain, conn=None: {"C1", "C2", "C3"})
    out = S.job_tracker_poll(ctx_for(tmp_db, "tracker_poll", {}))
    assert out["wallets"] == 0 and out["reason"] == "watchlist_empty" and out["cohort_only"] == 3


def test_the_poll_job_reads_the_real_pollable_set(tmp_db):
    """No monkeypatch: two active rows, one flagged ``helius_poll: false`` by the cohort
    seeder, and the projection counts one."""
    from kaiba.intelligence import tracker as TR

    ts = now_ms()
    for addr, meta in (("POLLED", "{}"), ("COHORT", '{"helius_poll": false}')):
        tmp_db.execute(
            "INSERT INTO tracker_watchlist (chain, address, tier, status, reason, source, added_by, "
            "added_ms, meta_json) VALUES (?,?,?,?,?,?,?,?,?)",
            (SOL, addr, "observe", "active", "test", "test", "test", ts, meta),
        )
    assert TR.watched_addresses(Chain.SOL, tmp_db) == {"POLLED", "COHORT"}
    assert TR.pollable_addresses(Chain.SOL, tmp_db) == {"POLLED"}


# ------------------------------------------- wallet intelligence (wired 2026-09-21)
#
# Three entry points built by the wallet-intelligence agents and verified, none of them
# on a clock: tracker.seed_from_cohorts (writes the `wallets` rows sm-trenches reads,
# 0 on the live box), naming.name_wallets (off until its vendor-tag leak is closed) and
# grade.grade_tape (read-only, candidates to kv). The tests that carry weight are the
# ones about money: the cohort job cannot spend as shipped, and the tape job stores
# nothing as shipped.


def cohort_report(chain: Chain, **counts: Any) -> Any:
    from kaiba.intelligence import tracker as TR

    return TR.CohortSeedReport(chain=chain, sources=["gmgn:smartmoney", "gmgn:kol"], dry_run=False, **counts)


def test_the_cohorts_job_seeds_every_configured_chain_for_free_as_shipped(tmp_db, monkeypatch):
    from kaiba.intelligence import tracker as TR

    calls: list[dict[str, Any]] = []

    def fake_seed(chain: Chain, conn: Any = None, **kw: Any) -> Any:
        calls.append({"chain": chain, **kw})
        return cohort_report(
            chain, considered=405, smart_cohort=300, admitted=["ADMITTED-ADDR"] * 250,
            refused=[{"address": "REFUSED-ADDR"}] * 155,
            refusal_counts={"shape_unassessable": 155}, admitted_by_tier={"observe": 250},
            wallet_rows_written=405, smart_tags_written=250, failure_rate_unmeasurable=405,
            notes=["failure rate has no signature source on bsc"],
        )

    monkeypatch.setattr(TR, "seed_from_cohorts", fake_seed)
    # The shipped timeout (600 s): the default per-chain reserve is 120 s, and a 60 s
    # test deadline would defer every chain before the first call.
    ctx = ctx_for(tmp_db, "tracker_cohorts", {"chains": ["bsc", "sol", "robinhood", "nope"],
                                               "check_failure_rate": False, "lookback_s": 3600,
                                               "max_credits_per_run": 0}, timeout_s=600)
    out = S.job_tracker_cohorts(ctx)
    assert [c["chain"] for c in calls] == [Chain.BSC, Chain.SOL, Chain.ROBINHOOD]
    for c in calls:
        # Exactly the call the piece specified: no paid screen, a day's lookback, no cap.
        assert c["check_failure_rate"] is False and c["max_credits"] is None
        assert ctx.now() - 3_600_000 - 2_000 <= c["since_ms"] <= ctx.now() - 3_600_000
    assert out["per_chain"]["nope"] == {"reason": "unknown_chain"}
    assert out["per_chain"]["bsc"]["admitted"] == 250 and out["per_chain"]["bsc"]["refused"] == 155
    assert out["admitted"] == 750 and out["wallet_rows_written"] == 1215 and out["credits"] == 0
    assert out["paid_check"] == {"requested": False, "taken": False} and out["deferred"] == []
    # Counts, never the address lists: three chains of hundreds of wallets each must
    # still fit the table cell `_small` keeps.
    assert "ADMITTED-ADDR" not in json.dumps(out) and "REFUSED-ADDR" not in json.dumps(out)
    assert len(json.dumps(out)) < 2000
    assert S.quota_used(tmp_db, "tracker_cohorts", S.utc_day(now_ms())) == (0, 0)


def test_the_cohorts_job_defers_chains_it_has_no_time_left_for(tmp_db, monkeypatch):
    """MEASURED on a live snapshot: sol takes ~150 s of a pass, robinhood ~60 s. A run that
    would be killed mid-chain records nothing; one that defers records what it did and
    names what it skipped."""
    from kaiba.intelligence import tracker as TR

    calls: list[Chain] = []

    def slow_seed(chain: Chain, conn: Any = None, **kw: Any) -> Any:
        calls.append(chain)
        if chain is Chain.SOL:
            ctx.deadline_ms = ctx.now() + 10_000  # sol ate the run: 10 s left
        return cohort_report(chain, considered=1)

    monkeypatch.setattr(TR, "seed_from_cohorts", slow_seed)
    ctx = ctx_for(tmp_db, "tracker_cohorts", {"chains": ["bsc", "sol", "robinhood"], "per_chain_reserve_s": 30},
                  timeout_s=600)
    out = S.job_tracker_cohorts(ctx)
    assert calls == [Chain.BSC, Chain.SOL]
    assert out["deferred"] == ["robinhood"] and out["per_chain"]["robinhood"]["reason"] == "deferred"
    assert out["considered"] == 2


def test_the_cohorts_job_takes_the_paid_path_only_capped_and_above_the_floor(tmp_db, monkeypatch):
    """VERIFIER FINDING: seed_from_cohorts pays a 10-credit Helius screen per sol cohort
    wallet BEFORE it checks get_entry(), so an uncapped paid pass re-screens every wallet
    it already admitted, every run. The job refuses that shape outright."""
    from kaiba.intelligence import tracker as TR
    from kaiba.providers import helius

    calls: list[dict[str, Any]] = []

    def fake_seed(chain: Chain, conn: Any = None, **kw: Any) -> Any:
        calls.append({"chain": chain, **kw})
        spent = 30 if kw["check_failure_rate"] and chain is Chain.SOL else 0
        return cohort_report(chain, considered=3, credits_spent=spent, failure_rate_measured=3 if spent else 0)

    monkeypatch.setattr(TR, "seed_from_cohorts", fake_seed)
    ledger = {"remaining": 900_000}
    monkeypatch.setattr(helius, "budget_status", lambda conn=None, period=None: dict(ledger))
    params = {"chains": "sol", "check_failure_rate": True, "max_credits_per_run": 0}

    def run() -> dict[str, Any]:
        return S.job_tracker_cohorts(ctx_for(tmp_db, "tracker_cohorts", params, timeout_s=600))

    # 1. requested but unbounded: refused, the free pass runs instead.
    out = run()
    assert calls[-1]["check_failure_rate"] is False and calls[-1]["max_credits"] is None
    assert out["paid_check"] == {"requested": True, "taken": False, "reason": S.REASON_PAID_CHECK_UNBOUNDED}
    assert out["credits"] == 0

    # 2. capped but the month is at the floor: refused with the floor's own reason.
    ledger["remaining"] = 200_000
    params["max_credits_per_run"] = 100
    out = run()
    assert calls[-1]["check_failure_rate"] is False
    assert out["paid_check"]["reason"] == S.REASON_BUDGET_FLOOR and out["paid_check"]["remaining"] == 200_000

    # 3. an unreadable ledger blocks spending.
    monkeypatch.setattr(helius, "budget_status", lambda conn=None, period=None: (_ for _ in ()).throw(RuntimeError("x")))
    out = run()
    assert calls[-1]["check_failure_rate"] is False and out["paid_check"]["reason"] == S.REASON_BUDGET_UNREADABLE

    # 4. capped, readable, above the floor: taken, bounded by the cap, and booked.
    ledger["remaining"] = 900_000
    monkeypatch.setattr(helius, "budget_status", lambda conn=None, period=None: dict(ledger))
    out = run()
    assert calls[-1]["check_failure_rate"] is True and calls[-1]["max_credits"] == 100
    assert out["paid_check"]["taken"] is True and out["credits"] == 30
    assert S.quota_used(tmp_db, "tracker_cohorts", S.utc_day(now_ms())) == (3, 30)


def test_the_shipped_cohorts_job_cannot_spend_and_is_not_credit_gated():
    """As shipped it is a free pass on every chain, and the dispatcher's floor must not
    stop the bsc seeding in a low-credit month: bsc has no paid path to gate."""
    config = S.load_config(ROOT / "config" / "schedule.yaml")
    jc = config.jobs["tracker_cohorts"]
    assert jc.enabled and jc.interval_s == 1800 and jc.timeout_s == 600
    # The per-chain reserve (job default 120 s) must leave room for at least the cheap
    # chain plus the slow one: MEASURED ~85 s for all three live, so 600 is 7x.
    assert jc.timeout_s >= 4 * 120
    assert jc.params["check_failure_rate"] is False
    assert int(jc.params["max_credits_per_run"]) == 0
    assert int(jc.params["lookback_s"]) == 86_400
    assert [c.lower() for c in jc.params["chains"]] == ["bsc", "sol", "robinhood"]
    assert S.JOBS["tracker_cohorts"].spends_helius is False
    text = (ROOT / "config" / "schedule.yaml").read_text(encoding="utf-8")
    assert "get_entry" in text and "check_failure_rate" in text


def test_the_naming_job_is_wired_and_on_because_the_tag_leak_is_closed(tmp_db, monkeypatch):
    """Flipped on 2026-09-22. The flip is safe only while two things hold, so both are
    checked on the real modules rather than on the YAML's word: a GMGN label reaches
    ``tags_json`` as ``gmgn:<label>`` and never as a word the lanes or the grader read, and
    the paid top-up is pinned to the operator's cohorts before ~88k cohort-NULL rows land."""
    from kaiba.execution import lanes as LN
    from kaiba.intelligence import grade as GR
    from kaiba.intelligence import naming as NM
    from kaiba.intelligence import tracker as TR

    config = S.load_config(ROOT / "config" / "schedule.yaml")
    jc = config.jobs["wallet_naming"]
    # 2026-10-02: incremental, every 15 min, and the run's own budget ends it well inside
    # the timeout (see test_the_naming_job_runs_incrementally_inside_its_budget).
    # 2026-10-04 (6e99c80): timeout 300 -> 600 s after 8 false timeouts in 24 h. The value
    # is an operator choice; the invariant is that a run is reaped before the next one is
    # due and before its lock is considered stale.
    assert jc.enabled is True and jc.interval_s == 900
    assert jc.timeout_s <= jc.interval_s and jc.timeout_s < config.lock_stale_s
    assert float(jc.params["budget_s"]) + 30 < jc.timeout_s
    # The leak, on the real namer: the three vendor labels that used to become lane words.
    facts = NM.WalletFacts(chain=Chain.SOL, address="LEAK", gmgn_tags={
        "smart_degen": 3, "launchpad_smart": 1, "app_smart_money": 2, "wash_trader": 1, "kol": 1,
    })
    tags = set(NM.registry_tags(facts))
    assert tags == {"gmgn:smart_degen", "gmgn:launchpad_smart", "gmgn:app_smart_money",
                    "gmgn:wash_trader", "gmgn:kol"}
    money_words = ({t.value for t in LN.SMART_TAGS} | {t.value for t in GR.POSITIVE_REPUTATION_TAGS}
                   | set(TR.LANE_SMART_TAGS))
    assert {"smart_money", "pump_smart"} <= money_words and not (tags & money_words)
    assert all(t.startswith(NM.GMGN_TAG_PREFIX) for t in tags)
    # The ordering guard lives in the same file, so the two cannot ship apart.
    cohorts = {c.strip() for c in str(config.jobs["wallet_tracked"].params["cohorts"]).split(",")}
    assert cohorts == {"research", "tracked", "trusted_copy"}
    text = (ROOT / "config" / "schedule.yaml").read_text(encoding="utf-8")
    assert "gmgn:<label>" in text and "naming-tape-fixes" not in text
    assert "DISABLED" not in text.split("wallet_naming:", 1)[1].split("wallet_tape:", 1)[0]
    seen: list[Any] = []

    def fake_name(conn: Any, chain: Any = None, **kw: Any) -> Any:
        seen.append((conn, chain, kw))
        report = NM.NamingReport(dry_run=False, wallets_before=0, wallets_after=12, considered=12, inserted=12)
        report.samples = [f"name-{i}" for i in range(9)]
        return report

    monkeypatch.setattr(NM, "name_wallets", fake_name)
    out = S.job_wallet_naming(ctx_for(tmp_db, "wallet_naming", {"mode": "full"}))
    assert seen == [(tmp_db, None, {})]  # every chain, not dry
    assert out["inserted"] == 12 and out["wallets_after"] == 12 and len(out["samples"]) == 5

    # It really is a Python entry point on the real module, on an empty database -- and the
    # default is the incremental pass, which never calls the full one.
    out = S.job_wallet_naming(ctx_for(tmp_db, "wallet_naming", {}))
    assert seen == [(tmp_db, None, {})] and out["mode"] == "incremental"
    monkeypatch.undo()
    out = S.job_wallet_naming(ctx_for(tmp_db, "wallet_naming", {"mode": "full"}))
    assert out["considered"] == 0 and out["dry_run"] is False


def test_the_tape_job_grades_each_chain_read_only_and_publishes_candidates(tmp_db, monkeypatch):
    from kaiba.intelligence import grade as GR

    calls: list[dict[str, Any]] = []

    def fake_tape(conn: Any, chain: Any, **kw: Any) -> Any:
        calls.append({"chain": chain, **kw})
        report = GR.TapeRunReport(chain=chain.value, wallets_seen=400, wallets_scored=120, wallets_too_thin=280,
                                  by_grade={"C": 100, "UNSCORED": 20}, b_or_better=0)
        report.candidates = [{"address": f"{chain.value}-{i}", "grade": "C", "closed_episodes": 5 - i}
                             for i in range(3)]
        return report

    monkeypatch.setattr(GR, "grade_tape", fake_tape)
    out = S.job_wallet_tape(ctx_for(tmp_db, "wallet_tape", {"chains": ["sol", "bsc", "robinhood", "nope"],
                                                            "store": False, "candidate_limit": 200}))
    assert [c["chain"] for c in calls] == [Chain.SOL, Chain.BSC, Chain.ROBINHOOD]
    assert all(c["store"] is False and c["candidate_limit"] == 200 for c in calls)
    assert out["store"] is False and out["stored"] == 0 and out["wallets_seen"] == 1200
    assert out["per_chain"]["nope"] == {"reason": "unknown_chain"}
    assert out["per_chain"]["bsc"]["by_grade"] == {"C": 100, "UNSCORED": 20}
    assert out["per_chain"]["bsc"]["candidates"] == 3 and out["candidates"] == 9
    for name in ("sol", "bsc", "robinhood"):
        row = fetch_one(tmp_db, "SELECT value, updated_ms FROM kv WHERE key=?", (f"ops:wallet_tape:candidates:{name}",))
        assert row is not None
        assert [c["address"] for c in json.loads(row["value"])] == [f"{name}-0", f"{name}-1", f"{name}-2"]
    # Three chains of counts fit the table cell; the candidate lists live in kv, not here.
    assert len(json.dumps(out)) < 2000 and "sol-0" not in json.dumps(out)

    # A second run overwrites, never appends.
    S.job_wallet_tape(ctx_for(tmp_db, "wallet_tape", {"chains": "sol"}))
    rows = fetch_all(tmp_db, "SELECT key FROM kv WHERE key LIKE 'ops:wallet_tape:candidates:%'")
    assert len(rows) == 3

    # The real grader on an empty tape: the wiring holds without a fake.
    monkeypatch.undo()
    out = S.job_wallet_tape(ctx_for(tmp_db, "wallet_tape", {"chains": ["bsc"]}))
    assert out["per_chain"]["bsc"]["wallets_seen"] == 0 and out["candidate_limit"] == GR.TAPE_CANDIDATE_LIMIT
    row = fetch_one(tmp_db, "SELECT value FROM kv WHERE key='ops:wallet_tape:candidates:bsc'")
    assert json.loads(row["value"]) == []
    assert fetch_all(tmp_db, "SELECT * FROM wallet_scores") == []


def test_the_shipped_tape_job_stores_its_grades():
    """``store`` was flipped to true by the LEAD on 2026-09-24, which is what the previous
    version of this test said was the only thing outstanding.

    The prerequisites it listed are unchanged and still asserted: tape rows stay invisible
    to the paid queue and to the regrade (above), and the grader's B gate is checked here
    on the real function -- a token count whose basis is not closed episodes still caps a
    B to C, so the artefact that produced a bogus tape B cannot come back.

    WHY THE FLIP. On bsc and robinhood there is no Helius history route, so this is the
    only grading path that exists, and that is where the operator's own imported GMGN
    wallets live -- 11,938 rows on each against 19 and 66 scored. With ``store: false``
    those wallets could never be graded at all, at any budget.

    WHAT IT DOES NOT LICENSE. A stored tape grade is capped and is overwhelmingly C. It
    is not an argument for lowering ``require_wallet_grade``: MEASURED 2026-09-24 over 173
    live fills, more confluence is WORSE -- 3 wallets +0.6%, 4 -23.7%, 5 -26.2%, 6+
    -12.6%."""
    from kaiba.intelligence import grade as GR

    config = S.load_config(ROOT / "config" / "schedule.yaml")
    jc = config.jobs["wallet_tape"]
    assert jc.enabled and jc.interval_s == 21_600
    assert jc.params["store"] is True and int(jc.params["candidate_limit"]) == 200
    ev = GR.WalletEvidence(chain=Chain.SOL, address="OPEN", tape=GR.TapeCoverage(rows=60, distinct_tokens=39),
                           pnl=GR.WalletPnl(closed_episodes=9, distinct_tokens=39))
    assert ev.sample_tokens == (39, "episodes_incl_open")
    blockers: list[str] = []
    assert GR._tape_cap(ev, GR.Grade.B, blockers) is GR.Grade.C and any("capped to C" in b for b in blockers)
    ev.closed_distinct_tokens = 9
    assert ev.sample_tokens == (9, "closed_episodes") and GR._tape_cap(ev, GR.Grade.B, []) is GR.Grade.B
    block = (ROOT / "config" / "schedule.yaml").read_text(encoding="utf-8").split("wallet_tape:", 1)[1].split("signals:", 1)[0]
    assert "CLOSED episodes only" in block and "count OPEN episodes" not in block
    # The flip must stay a recorded decision with a date, not a number someone nudged.
    assert "FLIPPED TO TRUE" in block and "lead" in block
    # bsc has no Helius history route, so this read-only pass is its only grading path
    # and the chain the whole piece exists for; sol and robinhood ride along.
    assert {c.lower() for c in jc.params["chains"]} == {"sol", "bsc", "robinhood"}
    assert S.JOBS["wallet_tape"].spends_helius is False
    text = (ROOT / "config" / "schedule.yaml").read_text(encoding="utf-8")
    assert "TAPE_MODEL_VERSION" in text or "kaiba-wallet-tape-v1" in text


def test_the_rescreen_job_summarises_evictions_instead_of_dumping_them(tmp_db, monkeypatch):
    from kaiba.intelligence import tracker as TR

    monkeypatch.setattr(
        TR, "rescreen",
        lambda chain, conn=None, **kw: {
            "screened": 3, "kept": ["A"], "credits": 30,
            "evicted": [{"address": "B" * 44, "why": "x" * 400},
                        {"address": "C" * 44, "why": "y" * 400}],
        },
    )
    out = S.job_tracker_rescreen(ctx_for(tmp_db, "tracker_rescreen", {}))
    assert out["screened"] == 3 and out["kept"] == 1 and out["evicted"] == 2
    assert out["credits"] == 30
    assert out["evicted_addresses"] == ["B" * 12, "C" * 12]
    # The result lands in a table cell and in ops_jobs.last_result_json; 800 characters of
    # refusal text does not belong in either, and the audit trail holds both in full.
    assert len(json.dumps(out)) < 500


def test_the_clustering_job_summarises_instead_of_dumping_the_entity_list(tmp_db, monkeypatch):
    from kaiba.intelligence import clustering as CL

    run = CL.ClusteringRun(
        run_id=7, chain=Chain.SOL, model=CL.MODEL_ID, started_ms=0, finished_ms=1, status="ok",
        edges_by_rule={"same_funder": 3517, "co_signed": 1829}, addresses_seen=50_417,
        addresses_checked=2_335, largest_component=270,
    )
    run.quarantined.append(
        CL.QuarantinedComponent(chain=Chain.SOL, component_key="sol:comp:1",
                                members=["a"] * 106, reason="too big")
    )
    seen: list[dict[str, Any]] = []

    def fake_run(conn: Any, chain: Any, **kw: Any) -> Any:
        seen.append(kw)
        return run

    monkeypatch.setattr(CL, "run", fake_run)
    out = S.job_clustering(ctx_for(tmp_db, "clustering", {}))
    assert seen == [{"fund_credits": 0, "max_entity_size": CL.MAX_ENTITY_SIZE}]
    assert out["edges_total"] == 5346 and out["addresses_seen"] == 50_417
    assert out["quarantined"] == 1 and out["quarantined_members"] == 106
    # Counts, never the membership: a 106-address component would blow past the 2,000
    # characters ``_small`` will keep and the whole result would be replaced by a stub.
    assert out["entities"] == 0
    assert len(json.dumps(out)) < 600 and "sol:comp:1" not in json.dumps(out)


def test_the_clustering_job_fails_loudly_on_a_failed_run(tmp_db, monkeypatch):
    from kaiba.intelligence import clustering as CL

    monkeypatch.setattr(
        CL, "run",
        lambda conn, chain, **kw: CL.ClusteringRun(
            run_id=1, chain=Chain.SOL, model=CL.MODEL_ID, started_ms=0, finished_ms=1,
            status="error", reason="edge derivation blew up",
        ),
    )
    with pytest.raises(S.JobFailed):
        S.job_clustering(ctx_for(tmp_db, "clustering", {}))


def test_the_funding_job_holds_its_daily_credit_budget_in_the_database(tmp_db, monkeypatch):
    """A restart inside the UTC day must not hand the job a second day's budget."""
    from kaiba.intelligence import clustering as CL

    budgets: list[int] = []

    def fake_enrich(conn: Any, chain: Any, max_credits: int = 0, addresses: Any = None) -> Any:
        """Spend the budget exactly, the way ``enrich_funding`` does: one address per
        ``FUNDING_CREDITS_PER_WALLET``, two of which cannot be resolved."""
        budgets.append(max_credits)
        n = max_credits // CL.FUNDING_CREDITS_PER_WALLET
        return n, max(0, n - 2), n * CL.FUNDING_CREDITS_PER_WALLET

    monkeypatch.setattr(CL, "enrich_funding", fake_enrich)
    params = {"max_credits": 100, "credits_per_day": 150}

    out = S.job_clustering_funding(ctx_for(tmp_db, "clustering_funding", params))
    assert budgets == [100]
    assert out["looked_up"] == 10 and out["resolved"] == 8 and out["unresolved"] == 2
    assert out["credits"] == 100 and out["credits_today"] == 100
    assert S.quota_used(tmp_db, "clustering_funding", S.utc_day(now_ms())) == (10, 100)

    # Second run the same day: only the remainder of the day's budget is offered, so the
    # per-run ceiling is not what bounds it.
    out = S.job_clustering_funding(ctx_for(tmp_db, "clustering_funding", params))
    assert budgets[-1] == 50 and out["credits_today"] == 150

    # Third: not one address's worth of the day's budget left, so nothing is bought.
    out = S.job_clustering_funding(ctx_for(tmp_db, "clustering_funding", params))
    assert out["reason"] == S.REASON_QUOTA and out["looked_up"] == 0
    assert len(budgets) == 2


def test_the_new_jobs_are_dispatched_and_the_paid_ones_are_gated_by_the_floor(tmp_db, tmp_path):
    """The gate is what stops a schedule burning a month of credits unattended."""
    paid = ("tracker_poll", "tracker_rescreen", "clustering_funding")
    free = ("tracker_sweep", "clustering")
    config = cfg(floor=999_999, **{name: job() for name in (*paid, *free)})
    jobs = {name: spec(name, lambda c: {"ran": True}, spends=name in paid) for name in (*paid, *free)}
    sched = make(tmp_db, tmp_path, config, jobs,
                 budget_status=lambda conn: {"remaining": 1_000, "used": 999_000,
                                             "allowance": 1_000_000})
    outcomes = {o.job: o for o in sched.run_once(force=True)}
    for name in paid:
        assert outcomes[name].status == S.STATUS_SKIPPED
        assert outcomes[name].error == S.REASON_BUDGET_FLOOR
    for name in free:
        assert outcomes[name].status == S.STATUS_OK, outcomes[name].error
