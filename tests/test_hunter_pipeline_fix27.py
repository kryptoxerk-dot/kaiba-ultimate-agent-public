"""Synthetic refusal regressions; captures are replayed separately, never called here."""
import json
from decimal import Decimal
import pytest
from kaiba.hunters import airdrops, nft
from kaiba.hunters.ev import HunterConfig, OpportunityEvidence, OpportunityKind
from kaiba.core.schemas import Chain, now_ms


def test_collection_retains_parse_error_in_source_health(tmp_db):
    collectors={'alphadrops':lambda conn=None: airdrops.from_alphadrops('<html>upstream maintenance</html>',conn=conn)}
    assert airdrops.collect(tmp_db,collectors)==[]
    source=dict(tmp_db.execute("SELECT * FROM hunter_sources WHERE name='alphadrops'").fetchone())
    assert 'unrecognized points page' in (source['last_error'] or '')
    assert source['fail_streak']==1
    assert source['last_ok_ms'] is None


def test_recognized_empty_source_clears_failure_without_becoming_provider_down(tmp_db):
    airdrops.record_source(tmp_db,'alphadrops','airdrop',0,error='previous outage')
    empty='<script id="__NEXT_DATA__">{"props":{"pageProps":{"programs":[]}}}</script>'
    assert airdrops.collect(tmp_db,{'alphadrops':lambda conn=None:airdrops.from_alphadrops(empty,conn=conn)})==[]
    source=dict(tmp_db.execute("SELECT * FROM hunter_sources WHERE name='alphadrops'").fetchone())
    assert source['fail_streak']==0
    assert source['last_count']==0
    assert source['last_ok_ms'] is not None
    assert source['last_error'] is None


@pytest.mark.parametrize('source,collector,body',[
    ('airdrops_io',airdrops.from_airdrops_io_telegram,'<html>maintenance</html>'),
    ('airdropalert',airdrops.from_airdropalert_rss,'<html><body>maintenance</body></html>'),
    ('defillama',airdrops.from_defillama,[{'error':'maintenance'}]),
])
def test_unrecognized_feed_is_not_healthy_empty(tmp_db,source,collector,body):
    assert airdrops.collect(tmp_db,{source:lambda conn=None:collector(body,conn=conn)})==[]
    row=dict(tmp_db.execute('SELECT * FROM hunter_sources WHERE name=?',(source,)).fetchone())
    assert row['last_error']
    assert row['fail_streak']==1 and row['last_ok_ms'] is None


def _qualified_airdrop(**updates):
    # Explicit synthetic evidence for gate tests, not a scraped or live opportunity.
    values=dict(kind=OpportunityKind.AIRDROP,name='Test programme',chain=Chain.SOL,
                confirmed_token=True,expected_value_usd=Decimal('5000'),
                capital_required_usd=Decimal('10'),official_url='https://project.example/season-2',
                meta={'qualification':{'observed_ms':now_ms(),'official_source_verified':True,
                'eligibility_verified':True,'value_cost_verified':True,
                'execution_path_verified':True,'participation_open':True}})
    values.update(updates)
    return OpportunityEvidence(**values)


@pytest.mark.parametrize('change,reason',[
    ({'deadline_ms':1},'participation_deadline_passed'),
    ({'expected_value_usd':None},'reward_or_exit_value_unsourced'),
    ({'expected_value_usd':Decimal('1')},'ev_below_floor'),
    ({'meta':{}},'eligibility_verified_missing'),
    ({'official_url':None},'official_source_unavailable'),
    ({'wallet_count':0},'single_wallet_required'),
])
def test_direct_airdrop_plan_cannot_bypass_qualification(tmp_db,change,reason):
    plan=airdrops.build_plan(_qualified_airdrop(**change),tmp_db, HunterConfig())
    assert plan.refused, plan
    assert reason in plan.refusal_reason
    assert plan.steps==[] and plan.executable_steps==[]
    assert plan.total_capital_usd==0


def test_qualified_airdrop_fixture_gets_recorded_plan_not_claim_authority(tmp_db):
    plan=airdrops.build_plan(_qualified_airdrop(),tmp_db,HunterConfig())
    assert not plan.refused and plan.steps
    claim=next(step for step in plan.steps if step.action==airdrops.PlanAction.CLAIM)
    assert claim.requires_operator and not claim.executable


def test_positive_numeric_score_does_not_emit_qualified_found_without_evidence(tmp_db):
    opp=_qualified_airdrop(meta={})
    airdrops.refresh(tmp_db,{'test':lambda conn=None:[opp]},HunterConfig())
    row=dict(tmp_db.execute('SELECT * FROM opportunities').fetchone())
    assert json.loads(row['plan_json'])['refused']
    from kaiba.core.events import recent
    from kaiba.core.schemas import EventKind
    assert recent(kinds=[EventKind.HUNTER_FOUND.value],conn=tmp_db)==[]


def _mint_payload(program='core'):
    import base58
    machine=base58.b58encode(bytes([7])*32).decode()
    guard=base58.b58encode(bytes([8])*32).decode()
    filler=base58.b58encode(bytes([9])*32).decode()
    instruction={
        'programId':nft.CORE_CANDY_MACHINE_PROGRAM if program=='core' else nft.CANDY_GUARD_PROGRAM,
        'accounts':[machine]+[filler]*10 if program=='core' else [guard,nft.CORE_CANDY_MACHINE_PROGRAM,machine]+[filler]*10,
        'data':base58.b58encode(bytes([84,175,211,156,56,250,104,118])+bytes(4) if program=='core' else bytes([145,98,192,118,184,147,118,104])+bytes(5)).decode(),
    }
    return {'type':'NFT_MINT','signature':'synthetic-signature','slot':123456,'timestamp':1700000000,
            'instructions':[instruction],'events':{'nft':{'nfts':[{'name':'Synthetic NFT','mint':filler}]}}},machine


def test_helius_does_not_invent_machine_from_unrelated_account_data():
    payload,_=_mint_payload()
    payload['instructions']=[]
    payload['accountData']=[{'account':'arbitrary-payer'}]
    assert nft.parse_helius_payload(payload).candy_machine is None


def test_guard_mint_decodes_machine_account_not_guard_address():
    payload,machine=_mint_payload('guard')
    assert nft.parse_helius_payload(payload).candy_machine==machine


def test_program_name_without_known_instruction_does_not_prove_machine():
    payload,_=_mint_payload()
    payload['instructions'][0].pop('data')
    assert nft.parse_helius_payload(payload).candy_machine is None


def test_observed_mint_is_not_verified_public_start():
    payload,_=_mint_payload()
    evidence=nft.parse_helius_payload(payload)
    assert evidence.start_slot is None and evidence.launch_ms is None
    assert evidence.meta['observed_slot']==123456
    assert evidence.meta['observed_chain_ms']==1700000000000
    assert evidence.observed_mints==1 and evidence.public_phase is None


def test_update_is_not_a_mint_and_failed_transaction_is_not_an_observation():
    payload,_=_mint_payload()
    payload['type']='CANDY_MACHINE_UPDATE'
    assert nft.parse_helius_payload(payload).observed_mints==0
    payload['type']='NFT_MINT'
    payload['transactionError']={'error':'instruction failed'}
    assert nft.parse_helius_payload(payload) is None


def test_refresh_report_distinguishes_new_duplicate_unqualified_and_empty(tmp_db):
    report_fn=getattr(airdrops,'refresh_report',None)
    assert callable(report_fn), 'scheduled hunters need a structured output entry point'
    opp=_qualified_airdrop(meta={},expected_value_usd=None)
    collectors={'test':lambda conn=None:[opp]}
    first=report_fn(tmp_db,collectors,HunterConfig())
    assert first['written']==1 and first['new_count']==1
    assert first['qualified_count']==0 and first['unqualified_count']==1
    assert first['funded_action_authorized'] is False
    assert first['outcome']=='unqualified_leads'
    second=report_fn(tmp_db,collectors,HunterConfig())
    assert second['written']==1 and second['new_count']==0
    assert second['outcome']=='duplicate_only'
    empty=report_fn(tmp_db,{'test':lambda conn=None:[]},HunterConfig())
    assert empty['outcome']=='healthy_empty'
    assert empty['sources']['test']['state']=='empty'
    assert empty['qualified_count']==0


def test_disabled_nft_report_makes_no_source_or_database_call(monkeypatch):
    report_fn=getattr(nft,'refresh_report',None)
    assert callable(report_fn), 'disabled is not a successful zero-result search'
    monkeypatch.delenv('KAIBA_ENABLE_NFT_HUNTER',raising=False)
    monkeypatch.setattr(nft,'get_conn',lambda: pytest.fail('disabled hunter opened database'))
    report=report_fn()
    assert report['outcome']=='disabled' and report['written']==0
    assert report['sources']=={} and report['qualified_count']==0


def test_explicit_no_sources_does_not_launch_default_sweep(tmp_db,monkeypatch):
    monkeypatch.setattr(airdrops,'COLLECTORS',{'unexpected':lambda conn=None:pytest.fail('default sweep')})
    report=airdrops.refresh_report(tmp_db,{},HunterConfig())
    assert report['outcome']=='no_sources' and report['written']==0


def test_publication_clock_is_reported_separately_from_fetch_time(tmp_db):
    ts=now_ms()-60_000
    opp=airdrops._evidence_from_text(name='New points programme',text='New points programme',
                                    url='https://project.example',source='test',ts_ms=ts)
    assert opp.meta.get('publication_ms')==ts
    report=airdrops.refresh_report(tmp_db,{'test':lambda conn=None:[opp]},HunterConfig())
    assert report['publication_recent_count']==1 and report['qualified_count']==0


def test_nft_receipt_observes_delivery_arriving_during_other_source(tmp_db,monkeypatch):
    def catalogue(conn=None):
        payload,_=_mint_payload()
        nft.record_helius_payload(conn,payload)
        nft._source_receipt(conn,'opensea',0)
        return []
    # 2026-10-02: OpenSea's drop list is the catalogue source that runs before Helius.
    monkeypatch.setattr(nft,'opensea_drops',catalogue)
    report=nft.refresh_report(tmp_db,cfg=HunterConfig(),force=True)
    assert report['sources']['helius_mint_watch']['state']=='ok'
    assert report['written']==1


def test_stored_refusal_cannot_be_laundered_by_direct_planner(tmp_db):
    opp=_qualified_airdrop()
    airdrops.refresh(tmp_db,{'test':lambda conn=None:[opp]},HunterConfig())
    tmp_db.execute("UPDATE opportunities SET status='refused' WHERE opportunity_id=?",(opp.key,))
    plan=airdrops.build_plan(opp.key,tmp_db,HunterConfig())
    assert plan.refused and plan.total_capital_usd==0
    assert 'not_open_or_refused' in plan.refusal_reason


def test_first_real_qualification_alert_is_not_suppressed_by_old_numeric_score(tmp_db):
    from kaiba.core.events import recent
    from kaiba.core.schemas import EventKind
    opp=_qualified_airdrop(meta={})
    airdrops.refresh(tmp_db,{'test':lambda conn=None:[opp]},HunterConfig())
    qualified=_qualified_airdrop()
    airdrops.refresh(tmp_db,{'test':lambda conn=None:[qualified]},HunterConfig())
    assert len(recent(kinds=[EventKind.HUNTER_FOUND.value],conn=tmp_db))==1
    airdrops.refresh(tmp_db,{'test':lambda conn=None:[qualified]},HunterConfig())
    assert len(recent(kinds=[EventKind.HUNTER_FOUND.value],conn=tmp_db))==1


def test_negative_economics_cannot_plan_even_with_negative_configured_floor(tmp_db):
    opp=_qualified_airdrop(expected_value_usd=Decimal('1'),gas_cost_usd=Decimal('10'))
    plan=airdrops.build_plan(opp,tmp_db,HunterConfig(ev_threshold_usd=Decimal('-100')))
    assert plan.refused and plan.total_capital_usd==0


def test_captured_claims_compilation_is_not_a_programme():
    # Exact title in the retained September 27 Telegram body; body minimized.
    assert airdrops._evidence_from_text(
        name='Airdrop Checkers/Claims Compilation',
        text='Airdrop Checkers/Claims Compilation\nClaim now live for several projects',
        url='https://airdrops.io',source='airdrops_io',
    ) is None


def test_missing_source_receipt_is_unavailable_not_healthy_empty(tmp_db):
    report=airdrops.refresh_summary(tmp_db,set(),set(),0,HunterConfig(),['missing'])
    assert report['sources']['missing']['parsed_count'] is None
    assert report['outcome']=='source_unavailable'


def test_report_keeps_provider_error_distinct_from_empty(tmp_db):
    report=airdrops.refresh_report(tmp_db,{'alphadrops':lambda conn=None:airdrops.from_alphadrops('<html>maintenance</html>',conn=conn)},HunterConfig())
    assert report['outcome']=='source_error' and report['written']==0
    assert 'unrecognized points page' in report['sources']['alphadrops']['error']


def test_report_positive_control_and_expired_window(tmp_db):
    qualified=_qualified_airdrop()
    result=airdrops.refresh_report(tmp_db,{'test':lambda conn=None:[qualified]},HunterConfig())
    assert result['outcome']=='qualified_research' and result['qualified_count']==1
    expired=_qualified_airdrop(deadline_ms=1)
    result=airdrops.refresh_report(tmp_db,{'test':lambda conn=None:[expired]},HunterConfig())
    assert result['known_expired_count']==1 and result['qualified_count']==0


def test_guard_and_core_inner_instruction_agree_and_unknown_opcode_is_not_a_route():
    core,machine=_mint_payload('core')
    guard,_=_mint_payload('guard')
    guard['instructions'][0]['innerInstructions']=core['instructions']
    assert nft.parse_helius_payload(guard).candy_machine==machine
    guard['instructions'][0]['data']='1111111111111'
    guard['instructions'][0]['innerInstructions']=[]
    assert nft.parse_helius_payload(guard).candy_machine is None


def test_no_delivery_keeps_last_good_poll_unverified(tmp_db):
    # Watcher itself reports no-delivery directly; no provider health success is fabricated.
    receipt={}
    assert nft.helius_mint_watch(tmp_db,source_report=receipt)==[]
    assert receipt['helius_mint_watch']=='no_deliveries'
    row=dict(tmp_db.execute("SELECT * FROM hunter_sources WHERE name='helius_mint_watch'").fetchone())
    assert row['last_ok_ms'] is None


def test_malformed_machine_identifier_is_not_silently_normalized():
    payload,_=_mint_payload()
    payload['instructions'][0]['accounts'][0]+=' '
    assert nft.parse_helius_payload(payload).candy_machine is None
