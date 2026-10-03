"""Reproduce a lead-owned reporting gap; this is not a fix/regression guarantee."""
import sqlite3
from types import SimpleNamespace

from kaiba.intelligence import grade
from kaiba.ops import scheduler


def test_tape_wrapper_discards_nonzero_store_failed(monkeypatch):
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
        ctx = SimpleNamespace(conn=conn, param=params.get, now=lambda: 123)
        result = scheduler.job_wallet_tape(ctx)
    assert calls == [("sol", True)]
    assert result["stored"] == 95
    assert result["wallets_scored"] == 100
    assert "store_failed" not in result
    assert "store_failed" not in result["per_chain"]["sol"]
