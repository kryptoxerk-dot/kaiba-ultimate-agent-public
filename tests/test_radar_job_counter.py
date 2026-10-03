"""The scheduler must report the radar's real seen count."""
from types import SimpleNamespace
from kaiba.hunters import radar
from kaiba.ops.scheduler import job_radar


def test_seen_candidates_are_not_lost_in_scheduler_summary(tmp_db,monkeypatch):
    monkeypatch.setattr(radar,'sweep',lambda *a,**k:radar.SweepResult(seen=52))
    result=job_radar(SimpleNamespace(conn=tmp_db,now=lambda:1000))
    assert result['candidates']==52


def test_no_due_work_is_zero_not_a_failure(tmp_db,monkeypatch):
    monkeypatch.setattr(radar,'sweep',lambda *a,**k:radar.SweepResult())
    result=job_radar(SimpleNamespace(conn=tmp_db,now=lambda:1000))
    assert result['candidates']==0 and result['layers_failed']==[]
