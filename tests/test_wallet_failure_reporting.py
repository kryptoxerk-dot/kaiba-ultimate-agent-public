"""A partial wallet write must remain visible and fail the scheduler receipt."""
import sqlite3
from types import SimpleNamespace

import pytest

from kaiba.intelligence import grade, seeds
from kaiba.ops import scheduler


@pytest.fixture
def ctx():
    with sqlite3.connect(':memory:') as conn:
        conn.execute('CREATE TABLE kv(key TEXT PRIMARY KEY,value TEXT,updated_ms INTEGER)')
        yield SimpleNamespace(conn=conn, param={'chains': ['sol', 'bsc'], 'store': True}.get,
                              now=lambda: 123)


def test_tape_partial_store_is_failed_with_exact_totals(ctx, monkeypatch):
    def replay(conn, chain, **kwargs):
        failed = 5 if chain.value == 'sol' else 0
        return grade.TapeRunReport(chain=chain.value, wallets_seen=100,
            wallets_scored=100, stored=100-failed, store_failed=failed, by_grade={'C': 100})
    monkeypatch.setattr(grade, 'grade_tape', replay)
    with pytest.raises(scheduler.JobFailed) as exc:
        scheduler.job_wallet_tape(ctx)
    result=exc.value.result
    assert result['stored']==195 and result['store_failed']==5
    assert result['wallets_scored']==200
    assert result['per_chain']['sol']['store_failed']==5
    assert result['per_chain']['bsc']['store_failed']==0


def test_tape_clean_write_is_success(ctx, monkeypatch):
    monkeypatch.setattr(grade, 'grade_tape', lambda c, chain, **kw:
        grade.TapeRunReport(chain=chain.value, wallets_scored=10, stored=10))
    result=scheduler.job_wallet_tape(ctx)
    assert result['stored']==20 and result['store_failed']==0


@pytest.mark.parametrize('failed', [0, 2])
def test_seeds_partial_failure_controls_job_status(ctx, monkeypatch, failed):
    monkeypatch.setattr(seeds, 'run', lambda chain, conn, **kw:
        {'seeds': 10, 'written': 100-failed, 'failed': failed})
    if failed:
        with pytest.raises(scheduler.JobFailed) as exc:
            scheduler.job_wallet_seeds(ctx)
        assert exc.value.result['failed']==4
        assert exc.value.result['written']==196
        assert len(exc.value.result['per_chain'])==2
    else:
        result=scheduler.job_wallet_seeds(ctx)
        assert result['written']==200 and result['failed']==0
