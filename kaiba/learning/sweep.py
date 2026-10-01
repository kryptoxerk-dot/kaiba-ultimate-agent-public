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
"""

from __future__ import annotations

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


def _hold_study(conn: sqlite3.Connection) -> str:
    from kaiba.learning.hold_study import run

    report = run(conn)
    verdicts = getattr(report, "verdicts", None) or []
    return f"{len(verdicts)} verdict(s): " + "; ".join(str(v)[:60] for v in verdicts[:2])


def _validation(conn: sqlite3.Connection) -> str:
    from kaiba.learning.validation import run_gates

    report = run_gates(conn, record=True)
    gates = getattr(report, "gates", None) or []
    passed = sum(1 for g in gates if str(getattr(g, "status", "")).lower() in {"pass", "passed"})
    return f"{passed}/{len(gates)} gates passed"


def _entry_study(conn: sqlite3.Connection) -> str:
    from kaiba.learning.entry_study import record, run_study

    result = run_study(conn)
    record(conn, result)
    best = max(result.cells, key=lambda c: c.rate, default=None)
    if best is None:
        return f"n={result.sample}, no cell above the minimum"
    return (f"n={result.sample} baseline={result.baseline_rate:.1f}% "
            f"best={best.name} {best.rate:.1f}%")


def _deployer_stats(conn: sqlite3.Connection) -> str:
    from kaiba.core.schemas import Chain
    from kaiba.intelligence.deployer import refresh

    return " ".join(f"{c.value}={refresh(conn, c)}"
                    for c in (Chain.SOL, Chain.BSC, Chain.ROBINHOOD))


#: name -> study. Ordered cheapest first so a timeout truncates the expensive tail rather
#: than the cheap head.
STUDIES: dict[str, Callable[[sqlite3.Connection], str]] = {
    "entry_study": _entry_study,
    "deployer_stats": _deployer_stats,
    "validation": _validation,
    "exit_study": _exit_study,
    "hold_study": _hold_study,
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
