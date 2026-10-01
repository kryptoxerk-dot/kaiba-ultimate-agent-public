"""Regression tests for watchdog quote workers and SQLite-backed provider state."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from decimal import Decimal
from pathlib import Path

import pytest

from kaiba.core import db, limiter
from kaiba.core.limiter import Priority
from kaiba.core.schemas import Chain, EvidenceBasis, LaneMode
from kaiba.execution import watchdog as wd
from tests.test_watchdog import RecordingSubmitter, make_position


TEST_PROVIDER = "watchdog-connection-isolation"


class ConnectionBoundSource:
    """A provider-shaped source whose limiter calls use one explicit connection."""

    name = "connection-bound"

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        errors: list[BaseException],
        active: list[int],
        max_active: list[int],
        counter_lock: threading.Lock,
        barrier: threading.Barrier | None = None,
        delay_s: float = 0.0,
    ) -> None:
        self.conn = conn
        self.errors = errors
        self.active = active
        self.max_active = max_active
        self.counter_lock = counter_lock
        self.barrier = barrier
        self.delay_s = delay_s

    def quote(self, chain: Chain, token: str) -> wd.PriceQuote:
        try:
            with limiter.guarded(
                TEST_PROVIDER,
                "quote",
                Priority.RESEARCH,
                conn=self.conn,
            ):
                with self.counter_lock:
                    self.active[0] += 1
                    self.max_active[0] = max(self.max_active[0], self.active[0])
                try:
                    if self.barrier is not None:
                        try:
                            self.barrier.wait(timeout=0.15)
                        except threading.BrokenBarrierError:
                            pass
                    if self.delay_s:
                        time.sleep(self.delay_s)
                finally:
                    with self.counter_lock:
                        self.active[0] -= 1
                return wd.PriceQuote(
                    price_usd=Decimal("1"),
                    liquidity_usd=Decimal("1000000"),
                    basis=EvidenceBasis.PROVIDER_REPORTED,
                    source=self.name,
                )
        except BaseException as exc:  # provider adapters convert failures to blindness
            self.errors.append(exc)
            return wd.PriceQuote.unavailable(f"provider raised {type(exc).__name__}", self.name)


def _book(conn: sqlite3.Connection, count: int) -> None:
    for index in range(count):
        token = f"TOKEN{index}pump" + ("x" * 36)
        make_position(
            conn,
            position_id=f"pos_connection_{index}",
            token=token,
            mode=LaneMode.SHADOW,
        )
    conn.commit()


@pytest.fixture
def permissive_test_limiter(monkeypatch):
    limits = limiter.Limits(
        # Negative keeps this deterministic stress harness from exercising the limiter's
        # intentional pacing; the regression is SQLite connection sharing, not throttling.
        min_interval_ms=-1,
        capacity=1_000,
        refill_per_s=1_000.0,
        max_inflight=100,
    )
    monkeypatch.setitem(limiter.DEFAULTS, TEST_PROVIDER, limits)
    real_limits_for = limiter.limits_for
    monkeypatch.setattr(
        limiter,
        "limits_for",
        lambda provider: limits if provider == TEST_PROVIDER else real_limits_for(provider),
    )


def test_connection_bound_quotes_do_not_share_sqlite_transactions(
    tmp_db,
    permissive_test_limiter,
    caplog,
):
    """Concurrent calls on one explicit connection reproduce the production failure."""
    del permissive_test_limiter
    caplog.set_level(logging.WARNING, logger="kaiba.core.limiter")
    errors: list[BaseException] = []
    active = [0]
    max_active = [0]
    counter_lock = threading.Lock()
    source = ConnectionBoundSource(
        tmp_db,
        errors=errors,
        active=active,
        max_active=max_active,
        counter_lock=counter_lock,
        barrier=threading.Barrier(4),
    )
    _book(tmp_db, 4)

    report = wd.Watchdog(
        tmp_db,
        price_source=source,
        submitter=RecordingSubmitter(),
    ).tick()

    transaction_failures = [
        record
        for record in caplog.records
        if "limiter release failed" in record.getMessage()
        or "cannot start a transaction within a transaction" in record.getMessage()
        or "cannot commit - no transaction is active" in record.getMessage()
    ]
    assert report.checked == 4
    assert not transaction_failures, "provider calls must not share an explicit SQLite connection"
    assert not errors
    row = tmp_db.execute(
        "SELECT inflight FROM provider_state WHERE provider=?",
        (TEST_PROVIDER,),
    ).fetchone()
    assert row is not None and row["inflight"] == 0


def test_prefetch_factory_gives_each_worker_its_own_connection(
    tmp_db,
    tmp_path,
    permissive_test_limiter,
    caplog,
):
    """The production source factory preserves quote concurrency without sharing a conn."""
    del permissive_test_limiter
    caplog.set_level(logging.WARNING, logger="kaiba.core.limiter")
    errors: list[BaseException] = []
    active = [0]
    max_active = [0]
    counter_lock = threading.Lock()
    created: list[ConnectionBoundSource] = []
    created_lock = threading.Lock()
    db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])

    def source_factory() -> ConnectionBoundSource:
        source = ConnectionBoundSource(
            db.connect(db_path),
            errors=errors,
            active=active,
            max_active=max_active,
            counter_lock=counter_lock,
            delay_s=0.05,
        )
        with created_lock:
            created.append(source)
        return source

    _book(tmp_db, 4)
    try:
        report = wd.Watchdog(
            tmp_db,
            price_source=ConnectionBoundSource(
                tmp_db,
                errors=errors,
                active=active,
                max_active=max_active,
                counter_lock=counter_lock,
            ),
            prefetch_source_factory=source_factory,
            submitter=RecordingSubmitter(),
        ).tick()
    finally:
        for source in created:
            source.conn.close()

    transaction_failures = [
        record
        for record in caplog.records
        if "limiter release failed" in record.getMessage()
        or "cannot start a transaction within a transaction" in record.getMessage()
        or "cannot commit - no transaction is active" in record.getMessage()
    ]
    assert report.checked == 4
    assert len(created) >= 2, "prefetch did not create worker-local sources"
    assert max_active[0] >= 2, "worker-local sources were not allowed to overlap"
    assert not transaction_failures
    assert not errors
    row = tmp_db.execute(
        "SELECT inflight FROM provider_state WHERE provider=?",
        (TEST_PROVIDER,),
    ).fetchone()
    assert row is not None and row["inflight"] == 0
