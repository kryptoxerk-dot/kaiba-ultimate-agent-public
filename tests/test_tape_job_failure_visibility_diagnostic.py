"""The tape job's write failures reach the scheduler receipt.

Originally a diagnostic that REPRODUCED the gap: ``job_wallet_tape`` dropped
``store_failed`` from both the totals and the per-chain summary, so a partial write read as
success. The job now carries the count and fails the run (``JobFailed``); this file pins
that, single-chain, alongside the two-chain totals in test_wallet_failure_reporting.py.
"""
import sqlite3
from types import SimpleNamespace

import pytest

from kaiba.intelligence import grade
from kaiba.ops import scheduler


def test_tape_wrapper_surfaces_nonzero_store_failed(monkeypatch):
    report = grade.TapeRunReport(
        chain="sol", wallets_seen=100, wallets_scored=100,
        stored=95, store_failed=5, by_grade={"C": 100},
    )
    assert report.as_dict()["store_failed"] == 5  # Positive source control.
    params = {"chains": ["sol"], "store": True}
    calls = []

    def fake_grade(conn, chain, **kwargs):
        calls.append((chain.value, kwargs["store"]))
        return report

    monkeypatch.setattr(grade, "grade_tape", fake_grade)
    with sqlite3.connect(":memory:") as conn:
        conn.execute("CREATE TABLE kv(key TEXT PRIMARY KEY,value TEXT,updated_ms INTEGER)")
        ctx = SimpleNamespace(conn=conn, param=params.get, now=lambda: 123,
                              deadline_ms=10**15, clock=lambda: 0.0)
        with pytest.raises(scheduler.JobFailed) as exc:
            scheduler.job_wallet_tape(ctx)
    result = exc.value.result
    assert calls == [("sol", True)]
    assert result["stored"] == 95
    assert result["wallets_scored"] == 100
    assert result["store_failed"] == 5
    assert result["per_chain"]["sol"]["store_failed"] == 5
