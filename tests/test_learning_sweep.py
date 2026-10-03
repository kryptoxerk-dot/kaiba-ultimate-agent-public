"""The learning studies run, write down what they found, and change nothing.

MEASURED 2026-09-22: ten analysis modules in ``kaiba/learning/`` and two of them reachable
from the scheduler. ``reflect``, ``outcomes``, ``playbook``, ``validation``, ``exit_study``,
``hold_study``, ``copytrade`` and ``concentration_outcome`` were fully implemented and
never once executed -- the same "declared, not enforced" shape as ``creator_rug_count``,
which shipped as a rule with no data source and had never fired in its life.

The three properties that make a scheduled sweep safe, each pinned here:

* **one broken study cannot silence the rest** -- a sweep that dies on its first exception
  reports nothing, which is indistinguishable from a sweep that found nothing;
* **it writes down what it found** -- a finding that lives only in a log line the operator
  never opens has not been learned;
* **it changes nothing.** Every study returns evidence. A system that retunes its own
  thresholds from its own output is a feedback loop with no reader, and the first wrong
  number in it is permanent.
"""

from __future__ import annotations

import pytest

from kaiba.learning import sweep as S


def test_every_study_is_registered():
    """A module nobody calls is the bug this exists to fix -- or it is excluded ON PURPOSE.

    ``hold_study`` is the second kind since 2026-10-02 (see ``sweep.NOT_SWEPT``): it made
    the sweep time out on every run and the sweep discarded what it computed.
    """
    assert set(S.STUDIES) >= {"entry_study", "deployer_stats", "validation", "exit_study"}
    assert not set(S.STUDIES) & set(S.NOT_SWEPT), "a study is both swept and excluded"
    assert all(len(reason) > 40 for reason in S.NOT_SWEPT.values()), "an exclusion needs a reason"


# ------------------------------------------------------------------ the 2026-10-02 bound
#
# MEASURED on the box: 4 of 4 sweeps timed out (900 s budget, up to 1544 s). hold_study
# alone took 1279-3237 s and returned "0 verdict(s)"; entry_study and deployer_stats
# repeated the full scans their own scheduled jobs had just run.


def _record_run(conn, job, status, *, result=None, error=None, age_s=60):
    import json
    import time as _t

    now = int(_t.time() * 1000)
    conn.execute(
        "INSERT INTO ops_runs (job, started_ms, finished_ms, status, duration_ms, result_json, error) "
        "VALUES (?,?,?,?,?,?,?)",
        (job, now - age_s * 1000 - 5_000, now - age_s * 1000, status, 5_000,
         json.dumps(result or {}), error),
    )
    conn.commit()


def test_the_sweep_does_not_rerun_the_expensive_studies(tmp_db, monkeypatch):
    """THE TIMEOUT. None of the three scans may run inside the sweep.

    Each is patched to raise; the old table called all three, and every line would then
    read FAIL. With the bound in place none of them is reached at all.
    """
    from kaiba.intelligence import deployer
    from kaiba.learning import entry_study, hold_study

    def forbidden(*a, **k):
        raise AssertionError("the sweep reran an expensive study")

    monkeypatch.setattr(hold_study, "run", forbidden)
    monkeypatch.setattr(deployer, "refresh", forbidden)
    monkeypatch.setattr(entry_study, "run_study", forbidden)
    _record_run(tmp_db, "entry_study", "ok",
                result={"sol": {"sample": 8026, "baseline_rate": 21.6, "cells": 12}})
    _record_run(tmp_db, "deployer_stats", "ok",
                result={"deployers_written": {"sol": 49129, "bsc": 511}})
    report = S.run_sweep(tmp_db, journal_it=False)
    text = "\n".join(report.lines())
    assert "reran" not in text, text
    names = {r.name for r in report.results}
    assert "hold_study" not in names
    assert {"entry_study", "deployer_stats"} <= names


def test_a_scheduled_study_reports_its_jobs_latest_success(tmp_db):
    """The digest still carries the finding; it comes from the job that computed it."""
    _record_run(tmp_db, "entry_study", "ok", age_s=3_600,
                result={"sol": {"sample": 111, "baseline_rate": 1.0, "cells": 1}})
    _record_run(tmp_db, "entry_study", "ok", age_s=120,
                result={"sol": {"sample": 8026, "baseline_rate": 21.6, "cells": 12}})
    _record_run(tmp_db, "entry_study", "timeout", age_s=30)  # newer, but not a success
    report = S.run_sweep(tmp_db, only="entry_study", journal_it=False)
    assert report.failed == 0, report.lines()
    line = report.results[0].headline
    assert "n=8026" in line and "baseline=21.6%" in line, line
    assert "n=111" not in line, "reported an older success than the latest"

    _record_run(tmp_db, "deployer_stats", "ok",
                result={"deployers_written": {"sol": 49129, "robinhood": 33486}})
    report = S.run_sweep(tmp_db, only="deployer_stats", journal_it=False)
    assert "sol=49129" in report.results[0].headline


def test_a_stalled_scheduled_job_is_a_failed_line_not_an_old_number(tmp_db):
    """A job that stopped succeeding must not keep reporting yesterday's finding as today's."""
    _record_run(tmp_db, "deployer_stats", "ok", age_s=S.SCHEDULED_RESULT_MAX_AGE_S + 600,
                result={"deployers_written": {"sol": 1}})
    _record_run(tmp_db, "deployer_stats", "timeout", age_s=60,
                error="timed out after 300s; thread abandoned until it returns")
    report = S.run_sweep(tmp_db, only="deployer_stats", journal_it=False)
    assert report.failed == 1
    text = "\n".join(report.lines())
    assert "FAIL" in text and "timeout" in text, text


def test_a_job_that_never_succeeded_is_a_failed_line(tmp_db):
    _record_run(tmp_db, "entry_study", "error", error="OperationalError: interrupted")
    report = S.run_sweep(tmp_db, only="entry_study", journal_it=False)
    assert report.failed == 1
    assert "never" in report.results[0].error and "interrupted" in report.results[0].error


def test_one_broken_study_does_not_stop_the_others(tmp_db, monkeypatch):
    calls: list[str] = []

    def boom(conn):
        calls.append("boom")
        raise RuntimeError("study exploded")

    def fine(conn):
        calls.append("fine")
        return "all good"

    monkeypatch.setattr(S, "STUDIES", {"broken": boom, "working": fine})
    report = S.run_sweep(tmp_db, journal_it=False)
    assert calls == ["boom", "fine"], "the sweep stopped at the first failure"
    assert report.ran == 1 and report.failed == 1


def test_a_failure_is_reported_not_swallowed(tmp_db, monkeypatch):
    """Silence about a broken study reads exactly like a study that found nothing."""
    monkeypatch.setattr(
        S, "STUDIES",
        {"broken": lambda conn: (_ for _ in ()).throw(ValueError("bad data"))},
    )
    report = S.run_sweep(tmp_db, journal_it=False)
    text = "\n".join(report.lines())
    assert "FAIL" in text and "broken" in text
    assert "ValueError" in text and "bad data" in text


def test_the_sweep_never_raises(tmp_db, monkeypatch):
    """It runs on a timer; an exception here is a silent end to all learning."""
    monkeypatch.setattr(
        S, "STUDIES",
        {"a": lambda c: (_ for _ in ()).throw(RuntimeError("x")),
         "b": lambda c: (_ for _ in ()).throw(KeyError("y"))},
    )
    report = S.run_sweep(tmp_db, journal_it=False)
    assert report.failed == 2


def test_findings_reach_the_journal(tmp_db, monkeypatch):
    """Self-journalling: the finding has to outlive the process that produced it."""
    monkeypatch.setattr(S, "STUDIES", {"toy": lambda conn: "baseline=22.5% best=31.0%"})
    S.run_sweep(tmp_db, journal_it=True)
    rows = [
        str(r["body"]) for r in tmp_db.execute(
            "SELECT body FROM journal WHERE kind='observation' ORDER BY seq DESC LIMIT 3")
    ]
    assert any("LEARNING SWEEP" in r and "baseline=22.5%" in r for r in rows), rows


def test_a_journal_failure_does_not_lose_the_findings(tmp_db, monkeypatch):
    monkeypatch.setattr(S, "STUDIES", {"toy": lambda conn: "found something"})
    import kaiba.core.journal as J

    monkeypatch.setattr(J, "append", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no")))
    report = S.run_sweep(tmp_db, journal_it=True)
    assert report.ran == 1
    assert "found something" in "\n".join(report.lines())


def test_the_sweep_changes_no_configuration():
    """The hard rule. Evidence in, nothing out but a journal entry."""
    import inspect

    source = inspect.getsource(S)
    for forbidden in ("save_risk", "set_lane", "kaiba_pause", "kaiba_resume",
                      "entries_paused", "EARLY_BUY_FRACTION_FLOOR", "DEPLOYER_LADDER"):
        assert forbidden not in source, f"the sweep touches {forbidden}"


def test_only_runs_one_study_when_asked(tmp_db, monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(
        S, "STUDIES",
        {"a": lambda c: seen.append("a") or "a", "b": lambda c: seen.append("b") or "b"},
    )
    S.run_sweep(tmp_db, only="b", journal_it=False)
    assert seen == ["b"]


def test_the_report_names_every_study_it_ran(tmp_db, monkeypatch):
    monkeypatch.setattr(S, "STUDIES", {"alpha": lambda c: "one", "beta": lambda c: "two"})
    text = "\n".join(S.run_sweep(tmp_db, journal_it=False).lines())
    assert "alpha" in text and "beta" in text
    assert "2 ran, 0 failed" in text


def test_slow_studies_are_flagged(tmp_db, monkeypatch):
    """A study that quietly grows to dominate the sweep should be visible as that."""
    monkeypatch.setattr(S, "SLOW_STUDY_S", -1.0)   # every study is "slow" now
    monkeypatch.setattr(S, "STUDIES", {"slow": lambda c: "done"})
    assert "(slow)" in "\n".join(S.run_sweep(tmp_db, journal_it=False).lines())


@pytest.mark.parametrize("name", ["entry_study", "deployer_stats"])
def test_the_real_studies_run_against_an_empty_database(tmp_db, name):
    """They must degrade to 'nothing to say', not to an exception, on a cold box."""
    report = S.run_sweep(tmp_db, only=name, journal_it=False)
    assert report.failed == 0, "\n".join(report.lines())
