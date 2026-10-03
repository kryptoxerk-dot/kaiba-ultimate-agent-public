"""Every loss has a review, including book write-offs omitted from trades."""
from kaiba.core.schemas import now_ms


def position(conn,pid='lost',mode='live',pnl='-25',closed=True):
    ts=now_ms()-10000
    conn.execute('INSERT INTO positions(position_id,chain,token,lane,mode,opened_ms,closed_ms,qty,qty_total,cost_native,proceeds_native,realized_native,entry_price_usd,peak_price_usd) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                 (pid,'sol','fixture-token','sm-trenches',mode,ts,ts+1000 if closed else None,'0','100','100','75',pnl,'1','2'))


def test_loss_without_canonical_outcome_is_still_reviewed(tmp_db):
    from kaiba.learning.loss_review import build_reviews
    position(tmp_db)
    report=build_reviews(tmp_db,cutoff_ms=now_ms())
    assert report['counts']['live']==1
    assert report['reviews'][0]['position_id']=='lost'
    assert 'missing_canonical_outcome' in report['reviews'][0]['evidence_gaps']
    assert report['reviews'][0]['causal_verdict']=='UNPROVEN'


def test_live_shadow_and_open_inventory_are_not_confused(tmp_db):
    from kaiba.learning.loss_review import build_reviews
    position(tmp_db,'live')
    position(tmp_db,'paper',mode='shadow')
    position(tmp_db,'winner',pnl='20')
    position(tmp_db,'still-open',closed=False)
    report=build_reviews(tmp_db,cutoff_ms=now_ms())
    assert report['counts']=={'live':1,'shadow':1}
    assert {r['position_id'] for r in report['reviews']}=={'live','paper'}


def test_paper_decision_cannot_explain_a_live_loss(tmp_db):
    from kaiba.learning.loss_review import build_reviews
    position(tmp_db)
    ts=tmp_db.execute('SELECT opened_ms FROM positions').fetchone()[0]
    tmp_db.execute('INSERT INTO decisions(decision_id,ts_ms,lane,mode,chain,token,action,thesis,confidence) VALUES(?,?,?,?,?,?,?,?,?)',
                   ('paper-decision',ts-100,'sm-trenches','shadow','sol','fixture-token','enter','paper only',0.8))
    tmp_db.execute('INSERT INTO trades(trade_id,position_id,decision_id,lane,mode,chain,token,opened_ms,closed_ms,hold_s,cost_native,proceeds_native,pnl_native,pnl_pct,mfe_pct) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   ('trade','lost','paper-decision','sm-trenches','live','sol','fixture-token',ts,ts+1000,1,'100','75','-25',-25,0))
    review=build_reviews(tmp_db,cutoff_ms=now_ms())['reviews'][0]
    assert review['decision_id'] is None
    assert 'entry_decision_scope_mismatch' in review['evidence_gaps']
    assert 'zero_mfe_contradicts_recorded_peak' in review['evidence_gaps']


def test_unchanged_evidence_does_not_count_as_new_learning(tmp_db,tmp_path):
    from kaiba.learning.loss_review import build_reviews,write_reviews
    position(tmp_db)
    first=build_reviews(tmp_db,cutoff_ms=now_ms())
    assert write_reviews(first,tmp_path)['new']==1
    second=build_reviews(tmp_db,cutoff_ms=now_ms()+100)
    assert write_reviews(second,tmp_path)=={'new':0,'updated':0,'total':1}
    tmp_db.execute("UPDATE positions SET realized_native='-30'")
    assert write_reviews(build_reviews(tmp_db,cutoff_ms=now_ms()+100),tmp_path)['updated']==1


def test_incidents_are_scoped_to_position_lifetime_and_watchdog(tmp_db):
    import json
    from kaiba.learning.loss_review import build_reviews
    position(tmp_db)
    opened,closed=tmp_db.execute('SELECT opened_ms,closed_ms FROM positions').fetchone()
    for service,stamp in [('watchdog',opened+500),('watchdog',closed+500),('engine',opened+500)]:
        payload={'service':service,'event':'exit_failed','position_id':'lost','detail':'measured refusal'}
        tmp_db.execute('INSERT INTO events(ts_ms,kind,level,payload) VALUES(?,?,?,?)',(stamp,'system','error',json.dumps(payload)))
    tmp_db.execute('INSERT INTO events(ts_ms,kind,level,payload) VALUES(?,?,?,?)',(opened+500,'system','error','malformed json'))
    review=build_reviews(tmp_db,cutoff_ms=now_ms())['reviews'][0]
    assert len(review['incident_evidence'])==1
    assert 'exit_failure_observed_during_position' in review['observed_flags']


def test_realized_loss_is_not_automatically_a_wrong_exit(tmp_db):
    from kaiba.learning.loss_review import build_reviews
    position(tmp_db)
    tmp_db.execute("UPDATE positions SET exit_reason='stop_loss'")
    r=build_reviews(tmp_db,cutoff_ms=now_ms())['reviews'][0]
    assert r['causal_verdict']=='UNPROVEN'
    assert r['counterfactual_exit_quality'].startswith('UNAVAILABLE')


def test_zero_fee_default_without_order_evidence_is_unknown(tmp_db):
    from kaiba.learning.loss_review import build_reviews
    position(tmp_db)
    r=build_reviews(tmp_db,cutoff_ms=now_ms())['reviews'][0]
    assert 'fee_complete_cashflow_unavailable' in r['evidence_gaps']


def test_report_does_not_mutate_the_database(tmp_db):
    from kaiba.learning.loss_review import build_reviews
    position(tmp_db)
    before=tmp_db.total_changes
    build_reviews(tmp_db,cutoff_ms=now_ms())
    assert tmp_db.total_changes==before
