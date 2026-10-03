"""Run the learning studies on a schedule, journal what they found, change nothing.

MEASURED 2026-09-22: ``kaiba/learning/`` contains ten analysis modules and **two** of them
were reachable from the scheduler. ``reflect``, ``outcomes``, ``playbook``, ``validation``,
``exit_study``, ``hold_study``, ``copytrade`` and ``concentration_outcome`` were all fully
implemented and nothing ever ran them. A learning system that never executes is not a
learning system; it is a directory.

This module is the driver that was missing. It is deliberately small and does three things:

1. runs each study inside its own guard, so one broken study cannot take the others down;
2. writes a compact result to the journal, which is what makes it *self*-journalling --
   the finding survives the process that produced it and reaches the operator's digest;
3. **changes nothing.** Every study here returns evidence. Moving a threshold in
   ``risk.py`` or a lane parameter stays a deliberate act with a diff and a test, because
   a system that retunes itself on its own output is a loop with no reader.

Cost: none. Every study here reads tables we already own and spends no provider credit.
That is why it can run hourly without touching the budget the live protection path needs --
a lesson learned the hard way the same day, when raising scanner concurrency drained the
``robinhood-rpc`` bucket and left two live positions with no evaluable stop.

WHAT THE SWEEP DOES NOT RECOMPUTE (2026-10-02). It timed out on every scheduled run on the
box (4 of 4 in 24 h, 900 s budget, 922-1544 s actual, one ``OperationalError:
interrupted``), and the journal lines it wrote say why -- per-study seconds, MEASURED::

    hold_study      1279.1 s / 1784.3 s / 3237.6 s (interrupted)   "0 verdict(s): "
    entry_study      79.5 s .. 395.2 s     also its own hourly job
    deployer_stats  110.8 s .. 358.8 s     also its own 30-minute job
    validation       14.2 s .. 19.4 s
    exit_study        8.6 s .. 9.5 s

* ``hold_study`` is out. :func:`kaiba.learning.hold_study.run` loads EVERY priced swap of
  each chain into Python objects (``load_series`` with no token filter in SQL) and
  full-scans ``swaps`` in ``coverage_facts``; on a 2-CPU box under a 4 GB MemoryMax that is
  the single largest allocation the ops service makes. And the sweep threw the whole
  report away: it kept ``len(report.verdicts)``, and ``verdicts`` is only appended when a
  chain has fewer than 20 smart-money anchors, so every run journalled ``0 verdict(s):``.
  Nothing else reads it. The full report is still one command away for a human, on a copy
  of the database rather than the live one: ``python -m kaiba.learning.hold_study --db``.
* ``entry_study`` and ``deployer_stats`` are REPORTED, not rerun. Both are scheduled jobs
  in their own right (hourly and every 30 minutes); the sweep used to repeat each scan,
  which for ``deployer_stats`` meant a second ~83,000-row rewrite of the table. The sweep
  now journals the headline of each job's latest successful run from ``ops_runs`` -- two
  indexed reads -- and FAILS that line when there is no success inside
  :data:`SCHEDULED_RESULT_MAX_AGE_S`, so a stalled job is still visible in the digest.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from kaiba.core.db import get_conn

log = logging.getLogger(__name__)

#: A study that runs longer than this is cut off in the report rather than allowed to
#: dominate the sweep. It still finished; only the reporting is bounded.
SLOW_STUDY_S = 60.0


@dataclass
class StudyResult:
    name: str
    ok: bool
    seconds: float
    headline: str = ""
    error: str = ""


@dataclass
class SweepReport:
    started_ms: int
    results: list[StudyResult] = field(default_factory=list)

    @property
    def ran(self) -> int:
        return sum(1 for r in self.results if r.ok)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if not r.ok)

    def lines(self) -> list[str]:
        out = [f"LEARNING SWEEP: {self.ran} ran, {self.failed} failed"]
        for r in sorted(self.results, key=lambda r: r.name):
            mark = "ok  " if r.ok else "FAIL"
            slow = " (slow)" if r.seconds > SLOW_STUDY_S else ""
            body = r.headline if r.ok else r.error
            out.append(f"  {mark} {r.name:22} {r.seconds:5.1f}s{slow}  {body[:150]}")
        return out


def _exit_study(conn: sqlite3.Connection) -> str:
    from kaiba.learning.exit_study import study

    result = study(conn)
    n = getattr(result, "n_in_sample", None)
    return f"n_in_sample={n}" if n is not None else "ran"


def _validation(conn: sqlite3.Connection) -> str:
    from kaiba.learning.validation import run_gates

    report = run_gates(conn, record=True)
    gates = getattr(report, "gates", None) or []
    passed = sum(1 for g in gates if str(getattr(g, "status", "")).lower() in {"pass", "passed"})
    return f"{passed}/{len(gates)} gates passed"


#: A scheduled job whose latest SUCCESS is older than this is a failed line in the sweep.
#: Six hours is three sweep intervals and covers every job reported this way (hourly and
#: 30-minute cadences) with room for a restart. INVENTED; what matters is that a job that
#: has stopped succeeding cannot keep reporting its last good number as current.
SCHEDULED_RESULT_MAX_AGE_S = 6 * 3600


def _latest_runs(conn: sqlite3.Connection, job: str) -> tuple[Any, Any]:
    """``(latest ok run, latest run of any status)`` for ``job``, or ``None`` for each.

    Both are seeks on ``idx_ops_runs_job (job, started_ms DESC)``; ``ops_prune`` bounds
    the table, so the walk to the first ``ok`` row is short even after a run of failures.
    """
    ok = conn.execute(
        "SELECT id, finished_ms, result_json FROM ops_runs WHERE job = ? AND status = 'ok' "
        "ORDER BY started_ms DESC LIMIT 1",
        (job,),
    ).fetchone()
    last = conn.execute(
        "SELECT status, started_ms, error FROM ops_runs WHERE job = ? "
        "ORDER BY started_ms DESC LIMIT 1",
        (job,),
    ).fetchone()
    return ok, last


def _scheduled(job: str, summarise: Callable[[dict[str, Any]], str]) -> Callable[
    [sqlite3.Connection], str
]:
    """A study line that REPORTS ``job``'s latest scheduled result instead of rerunning it.

    No run at all (a cold box) is "nothing to say", not a failure -- the same contract the
    rerun version had on an empty database. A last success older than
    :data:`SCHEDULED_RESULT_MAX_AGE_S` raises, so the sweep marks the line FAIL with the
    job's own latest status: the digest must not present a stalled job's old number as
    today's finding.
    """

    def study(conn: sqlite3.Connection) -> str:
        ok, last = _latest_runs(conn, job)
        if ok is None and last is None:
            return f"no scheduled {job} run recorded yet"
        now = int(time.time() * 1000)
        if ok is None or now - int(ok[1] or 0) > SCHEDULED_RESULT_MAX_AGE_S * 1000:
            age = "never" if ok is None else f"{(now - int(ok[1] or 0)) / 3_600_000:.1f}h ago"
            detail = f"last run {last[0]}" if last is not None else "no run"
            if last is not None and last[2]:
                detail += f": {str(last[2])[:60]}"
            raise RuntimeError(f"scheduled {job} last succeeded {age}; {detail}")
        try:
            result = json.loads(ok[2] or "{}")
        except (TypeError, ValueError):
            result = {}
        age_min = max(0, (now - int(ok[1] or 0)) // 60_000)
        return f"run #{ok[0]} {age_min}m ago: {summarise(result if isinstance(result, dict) else {})}"

    study.__name__ = f"_scheduled_{job}"
    return study


def _summarise_entry_study(result: dict[str, Any]) -> str:
    parts = []
    for chain, body in sorted(result.items()):
        if isinstance(body, dict):
            parts.append(f"{chain} n={body.get('sample')} baseline={body.get('baseline_rate')}% "
                         f"cells={body.get('cells')}")
    return "; ".join(parts) or "empty result"


def _summarise_deployer_stats(result: dict[str, Any]) -> str:
    written = result.get("deployers_written")
    if not isinstance(written, dict):
        return "empty result"
    return " ".join(f"{chain}={_short(v)}" for chain, v in sorted(written.items()))


def _short(value: Any) -> str:
    if isinstance(value, dict):
        return ",".join(f"{k}:{v}" for k, v in value.items() if not isinstance(v, (dict, list)))
    return str(value)


#: name -> study. Ordered cheapest first so a timeout truncates the expensive tail rather
#: than the cheap head. See the module docstring for what is NOT here and why.
STUDIES: dict[str, Callable[[sqlite3.Connection], str]] = {
    "entry_study": _scheduled("entry_study", _summarise_entry_study),
    "deployer_stats": _scheduled("deployer_stats", _summarise_deployer_stats),
    "validation": _validation,
    "exit_study": _exit_study,
}

#: Studies deliberately left out of the scheduled sweep, with the reason. A test asserts
#: each is absent from :data:`STUDIES`, so putting one back is a decision with a diff.
NOT_SWEPT: dict[str, str] = {
    "hold_study": (
        "loads every priced swap per chain into memory (1279-3237 s per sweep, MEASURED "
        "2026-10-01/02) and the sweep kept only a verdict count that is always 0; run "
        "`python -m kaiba.learning.hold_study --db <copy>` when a human wants the report"
    ),
}


def run_sweep(
    conn: sqlite3.Connection | None = None,
    *,
    only: str | None = None,
    journal_it: bool = True,
) -> SweepReport:
    """Run every study, journal the result, and return it. Never raises."""
    c = conn or get_conn()
    report = SweepReport(started_ms=int(time.time() * 1000))
    for name, fn in STUDIES.items():
        if only and name != only:
            continue
        started = time.monotonic()
        try:
            headline = fn(c)
            report.results.append(
                StudyResult(name, True, time.monotonic() - started, headline=headline)
            )
        except Exception as exc:  # noqa: BLE001 - one broken study must not stop the sweep
            log.exception("learning study %s failed", name)
            report.results.append(
                StudyResult(name, False, time.monotonic() - started,
                            error=f"{type(exc).__name__}: {exc}"[:200])
            )
    if journal_it:
        try:
            from kaiba.core import journal

            journal.append("observation", "\n".join(report.lines()), conn=c)
        except Exception:  # noqa: BLE001 - the findings matter more than the record of them
            log.exception("could not journal the learning sweep")
    return report
