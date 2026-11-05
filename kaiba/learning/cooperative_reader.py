"""Standalone same-thread PAPER servicing; not wired into any installed worker.

Owned by Codex AGENT-RESEARCH-SERVICING. The integrating caller supplies a
paper-only callback and an explicit safe-boundary predicate. No callback may
run a whole worker/review, acknowledge an economic outbox, or use this wrapper.
The callback creates its own bare reader. This module adds no source allowance,
SQL, persistence, provider, process, thread or trading operation.
"""
from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from kaiba.learning.autolearn_io import SourceDeferred

VERSION = "cooperative-paper-service/1"
WAL_QUIET_SECONDS = 60.0


def _number(value: Any, *, positive: bool) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or (value <= 0 if positive else value < 0):
        raise SourceDeferred("cooperative:invalid_clock_or_interval")
    return float(value)


class PaperService:
    """Service one trusted paper-only callback at explicit, safe checkpoints.

    Initially due. Only a completed callback moves the next due frontier. Wall
    clocks are recorded as sampled, including backward changes; scheduling uses
    monotonic time. WAL quiet requires both clocks to pass their original60s
    frontiers, so wall-clock jumps cannot shorten it. This in-memory pause is
    not a replacement for the integrating worker's durable source retry state.
    """

    def __init__(self, callback: Callable[[], Any], *, interval_s: float = 15.0,
                 monotonic_clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], float] = time.time):
        if not callable(callback) or not callable(monotonic_clock) or not callable(wall_clock):
            raise ValueError("cooperative callbacks and clocks must be callable")
        self.callback = callback
        self.interval_s = _number(interval_s, positive=True)
        self.monotonic_clock, self.wall_clock = monotonic_clock, wall_clock
        self._owner = threading.get_ident()
        self._busy = False
        self._dispatching = False
        self._last_mono: float | None = None
        self._last_wall: float | None = None
        self._completed_mono: float | None = None
        self._quiet_mono = self._quiet_wall = 0.0
        self._wal_clock_unknown = False
        self._wall_regressions = self._reported_regressions = 0
        self._counts = {"serviced": 0, "failed": 0, "not_due": 0, "wal_refusals": 0}
        self.last_receipt: dict[str, Any] | None = None

    def _owner_check(self, *, idle: bool = True) -> None:
        if threading.get_ident() != self._owner:
            raise SourceDeferred("cooperative:foreign_thread_refused")
        if idle and self._busy:
            raise SourceDeferred("cooperative:paper_callback_reentry_refused")
        if idle and self._dispatching:
            raise SourceDeferred("cooperative:active_source_dispatch_reentry_refused")

    def _sample(self) -> tuple[float, float, bool]:
        self._owner_check(idle=False)
        mono = _number(self.monotonic_clock(), positive=False)
        wall = _number(self.wall_clock(), positive=True)
        if self._last_mono is not None and mono < self._last_mono:
            raise SourceDeferred("cooperative:monotonic_clock_regressed")
        regressed = self._last_wall is not None and wall < self._last_wall
        self._wall_regressions += int(regressed)
        self._last_mono, self._last_wall = mono, wall
        return mono, wall, regressed

    def before_source(self) -> None:
        """Refuse reentry, foreign threads or known WAL quiet before dispatch."""
        self._owner_check()
        if self._wal_clock_unknown:
            raise SourceDeferred("cooperative:wal_quiet_clock_unknown")
        mono, wall, _ = self._sample()
        if mono < self._quiet_mono or wall < self._quiet_wall:
            raise SourceDeferred("cooperative:wal_source_pause; retry after both quiet frontiers")

    def note_source_failure(self, error: BaseException) -> bool:
        """Observe a real WAL refusal; ordinary failures never become empty data."""
        self._owner_check(idle=False)
        if not isinstance(error, SourceDeferred) or "wal_over_limit:" not in str(error):
            return False
        self._wal_clock_unknown = True
        mono, wall, _ = self._sample()
        self._quiet_mono = max(self._quiet_mono, mono + WAL_QUIET_SECONDS)
        self._quiet_wall = max(self._quiet_wall, wall + WAL_QUIET_SECONDS)
        self._counts["wal_refusals"] += 1
        self._wal_clock_unknown = False
        return True

    def checkpoint(self, reason: str, *, force: bool = False,
                   guard: Callable[[], Any] | None = None) -> dict[str, Any]:
        """Run only at a caller-approved stable boundary, before any SQL dispatch.

        A callback must return None (explicit void completion) or a mapping
        with status='complete'. Other returns or failures do not count as a
        serviced paper tick. Exceptions propagate to the caller.
        A successful receipt describes callback completion, never profitability.
        """
        if not isinstance(reason, str) or not reason or len(reason) > 120 or type(force) is not bool:
            raise ValueError("cooperative checkpoint reason/force invalid")
        self.before_source()
        began_mono, began_wall, regressed = self._sample()
        receipt = {"reason": reason, "started_ms": int(began_wall * 1000),
                   "started_monotonic_ms": int(began_mono * 1000),
                   "wall_clock_regressed": regressed or self._wall_regressions > self._reported_regressions}
        if not force and self._completed_mono is not None and began_mono - self._completed_mono < self.interval_s:
            self._counts["not_due"] += 1
            return {**receipt, "status": "not_due"}
        self._busy = True
        try:
            if guard is not None:
                guard()
            result = self.callback()
            if result is not None and not (isinstance(result, Mapping) and result.get("status") == "complete"):
                raise SourceDeferred("cooperative:paper_callback_did_not_complete")
            ended_mono, ended_wall, ended_regressed = self._sample()
            receipt.update(status="serviced", completed_ms=int(ended_wall * 1000),
                           completed_monotonic_ms=int(ended_mono * 1000),
                           elapsed_ms=int((ended_mono - began_mono) * 1000),
                           wall_elapsed_ms=int((ended_wall - began_wall) * 1000),
                           wall_clock_regressed=regressed or ended_regressed
                           or self._wall_regressions > self._reported_regressions)
            self._completed_mono = ended_mono
            self._counts["serviced"] += 1
            self.last_receipt = receipt
            self._reported_regressions = self._wall_regressions
            return dict(receipt)
        except BaseException as error:
            try:
                self.note_source_failure(error)
            finally:
                self._counts["failed"] += 1
                self.last_receipt = {**receipt, "status": "failed", "failure_type": type(error).__name__}
                self._reported_regressions = self._wall_regressions
            raise
        finally:
            self._busy = False

    def snapshot(self) -> dict[str, Any]:
        self._owner_check(idle=False)
        return {"version": VERSION, "counts": dict(self._counts),
                "quiet_until_wall_ms": int(self._quiet_wall * 1000),
                "quiet_until_monotonic_ms": int(self._quiet_mono * 1000),
                "wal_quiet_clock_unknown": self._wal_clock_unknown,
                "wall_clock_regressions": self._wall_regressions,
                "last_receipt": dict(self.last_receipt) if self.last_receipt else None,
                "busy": self._busy, "source_dispatch_active": self._dispatching,
                "funding_allowed": False}


class CooperativeReader:
    """Delegate original reads exactly once; callback admission defaults FALSE.

    The trusted predicate receives (operation, sql, tuple(args)). It must admit
    only named stable registry/operating boundaries. In particular it must NOT
    admit tape physical/preflight/horizon reads, economic capture queries or an
    operation whose caller has already reserved a source attempt. No classifier
    is supplied here. Unknown SQL still gets its normal guarded read, no yield.
    """

    def __init__(self, reader: Any, service: PaperService, *,
                 safe_predicate: Callable[[str, str, tuple], bool] | None = None):
        if not isinstance(service, PaperService) or safe_predicate is not None and not callable(safe_predicate):
            raise ValueError("cooperative service/predicate invalid")
        self.reader, self.service, self.safe_predicate = reader, service, safe_predicate

    def __getattr__(self, name: str) -> Any:
        return getattr(self.reader, name)

    def _guard(self) -> Any:
        self.service.before_source()
        try:
            return self.reader.guard()
        except BaseException as error:
            self.service.note_source_failure(error)
            raise

    def guard(self) -> Any:
        return self._guard()

    def _read(self, operation: str, sql: str, args: Sequence[Any]) -> list:
        self._guard()
        bound = tuple(args)
        safe = False if self.safe_predicate is None else self.safe_predicate(operation, sql, bound)
        if type(safe) is not bool:
            raise ValueError("cooperative predicate must explicitly return bool")
        if safe:
            self.service.checkpoint("before_" + operation, guard=self.reader.guard)
        self._guard()  # callback duration or state cannot bypass the source guard
        self.service.before_source()
        self.service._dispatching = True
        try:
            return getattr(self.reader, operation)(sql, bound)
        except BaseException as error:
            self.service.note_source_failure(error)
            raise
        finally:
            self.service._dispatching = False

    def rows(self, sql: str, args: Sequence[Any] = ()) -> list[dict]:
        return self._read("rows", sql, args)

    def q(self, sql: str, args: Sequence[Any] = ()) -> list[tuple]:
        return self._read("q", sql, args)


__all__ = ["PaperService", "CooperativeReader", "VERSION"]
