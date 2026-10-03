"""An explicit owner A/B-only watch policy cannot be bypassed by a cohort verdict."""
import json
import pytest
from kaiba.core.schemas import Chain, Grade, now_ms
from kaiba.intelligence import tracker

ADDRESS = '0x452591192ee3d82e0167c2a51f2175ed8640a5d1'
KEY = 'tracker:active_grade_policy'

def enable(conn):
    conn.execute('INSERT INTO kv(key,value,updated_ms) VALUES(?,?,?)',
                 (KEY,json.dumps({'enabled':True,'allowed_grades':['A','B'],'target_wallets':5000,'max_grade_age_s':604800}),now_ms()))

def score(conn, grade):
    conn.execute('INSERT OR REPLACE INTO wallet_scores(chain,address,score,grade,evidence_weight,archetype,model_version,scored_at_ms) VALUES(?,?,?,?,?,?,?,?)',
                 ('robinhood',ADDRESS,50,grade,66,'trader','kaiba-wallet-tape-v1',now_ms()))

def test_owner_ab_policy_refuses_c_even_with_preapproved_cohort_verdict(tmp_db):
    enable(tmp_db)
    score(tmp_db,'C')
    screen=tracker.Screen(chain=Chain.ROBINHOOD,address=ADDRESS,grade=Grade.C,score=39)
    verdict=tracker.Verdict(policy=tracker.ADMISSION_POLICY_COHORT,admissible=True,tier=tracker.Tier.OBSERVE)
    result=tracker.admit(Chain.ROBINHOOD,ADDRESS,reason='owner nominee',source='test',added_by='test',conn=tmp_db,screen=screen,verdict=verdict)
    assert result is None
    assert tmp_db.execute("SELECT count(*) FROM tracker_watchlist WHERE status='active'").fetchone()[0] == 0


def admit_b(conn):
    score(conn,'B')
    screen=tracker.Screen(chain=Chain.ROBINHOOD,address=ADDRESS,grade=Grade.B,score=50)
    return tracker.admit(Chain.ROBINHOOD,ADDRESS,reason='measured B',source='test',added_by='test',conn=conn,screen=screen)


def test_downgrade_stops_detection_and_polling_before_cleanup(tmp_db):
    enable(tmp_db)
    assert admit_b(tmp_db) is not None
    score(tmp_db,'C')
    assert ADDRESS not in tracker.watched_addresses(Chain.ROBINHOOD,tmp_db)
    assert ADDRESS not in tracker.pollable_addresses(Chain.ROBINHOOD,tmp_db)


def test_owner_policy_keeps_fresh_b(tmp_db):
    enable(tmp_db)
    assert admit_b(tmp_db) is not None
    assert ADDRESS in tracker.watched_addresses(Chain.ROBINHOOD,tmp_db)


def test_cleanup_keeps_history_and_removes_only_ineligible_active_rows(tmp_db):
    assert admit_b(tmp_db) is not None
    enable(tmp_db)
    score(tmp_db,'C')
    from kaiba.intelligence import watchlist_policy
    result=watchlist_policy.reconcile_active(tmp_db,actor='test')
    assert result['removed'] == 1
    assert tmp_db.execute('SELECT status FROM tracker_watchlist').fetchone()[0] == 'removed'
    assert tmp_db.execute('SELECT grade FROM wallet_scores').fetchone()[0] == 'C'
    assert watchlist_policy.reconcile_active(tmp_db,actor='test')['removed'] == 0


def test_malformed_evidence_is_refused_without_crashing(tmp_db):
    enable(tmp_db)
    assert admit_b(tmp_db) is not None
    tmp_db.execute('UPDATE wallet_scores SET evidence_weight=?',('not a number',))
    assert ADDRESS not in tracker.watched_addresses(Chain.ROBINHOOD,tmp_db)


def test_periodic_rescreen_enforces_policy_on_all_chains_locally(tmp_db,monkeypatch):
    from types import SimpleNamespace
    from kaiba.ops.scheduler import job_tracker_rescreen
    assert admit_b(tmp_db) is not None
    enable(tmp_db)
    score(tmp_db,'D')
    monkeypatch.setattr(tracker,'rescreen',lambda *a,**kw:{'kept':[],'evicted':[],'credits':0})
    job_tracker_rescreen(SimpleNamespace(conn=tmp_db,param=lambda key,default:default))
    assert tmp_db.execute('SELECT status FROM tracker_watchlist').fetchone()[0] == 'removed'


@pytest.mark.parametrize('delta',[-604800001,60000])
def test_stale_or_future_b_is_not_active(tmp_db,delta):
    enable(tmp_db)
    assert admit_b(tmp_db) is not None
    tmp_db.execute('UPDATE wallet_scores SET scored_at_ms=?',(now_ms()+delta,))
    assert ADDRESS not in tracker.watched_addresses(Chain.ROBINHOOD,tmp_db)


@pytest.mark.parametrize('value',['broken', '{"enabled":true}', '[]'])
def test_malformed_policy_fails_closed(tmp_db,value):
    enable(tmp_db)
    assert admit_b(tmp_db) is not None
    tmp_db.execute('UPDATE kv SET value=? WHERE key=?',(value,KEY))
    assert ADDRESS not in tracker.watched_addresses(Chain.ROBINHOOD,tmp_db)


def test_grade_b_in_quarantined_cluster_is_not_active(tmp_db):
    enable(tmp_db)
    assert admit_b(tmp_db) is not None
    tmp_db.execute('INSERT INTO clustering_coverage(chain,address,status,checked,funding_basis,signer_basis,run_id,updated_ms) VALUES(?,?,?,?,?,?,?,?)',
                   ('robinhood',ADDRESS,'quarantined',1,'unavailable','unavailable',1,now_ms()))
    assert ADDRESS not in tracker.watched_addresses(Chain.ROBINHOOD,tmp_db)


def test_cached_b_cannot_override_missing_current_score(tmp_db):
    enable(tmp_db)
    screen=tracker.Screen(chain=Chain.ROBINHOOD,address=ADDRESS,grade=Grade.B,score=50)
    assert tracker.admit(Chain.ROBINHOOD,ADDRESS,reason='cached B',source='test',added_by='test',conn=tmp_db,screen=screen) is None
