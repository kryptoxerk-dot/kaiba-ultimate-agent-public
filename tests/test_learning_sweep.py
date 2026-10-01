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
    """A module nobody calls is the bug this exists to fix."""
    assert set(S.STUDIES) >= {"entry_study", "deployer_stats", "validation",
                              "exit_study", "hold_study"}


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
