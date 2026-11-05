"""Hunter readers distinguish discovery records from qualified research candidates.

Codex 2026-10-18: positive airdrop fixture uses synthetic source-scenario estimates;
existing refusal and no-funding assertions are retained (AIRDROP-EV-SEMANTICS).
"""
import hashlib
import json
from decimal import Decimal

import pytest

from kaiba.core.schemas import now_ms
from kaiba.hunters import qualification
from kaiba.hunters.ev import (
    HunterConfig,
    OpportunityEvidence,
    opportunity_id,
    score_opportunity,
    valuation_cost_binding,
)
from kaiba.mcp import server

CFG = HunterConfig()


@pytest.fixture(autouse=True)
def _consistent_cost_config(monkeypatch):
    monkeypatch.setattr(qualification, 'hunter_config', lambda: CFG)


def add(conn, ident='nft', kind='nft_mint', status='refused', ev=-15.5, evidence=None, deadline=None):
    stamp=now_ms()
    if kind in {'airdrop', 'points'} and ident == 'nft' and evidence and evidence.get('name'):
        ident = opportunity_id(evidence['name'], evidence.get('official_url') or evidence.get('url'), kind)
    conn.execute('INSERT INTO opportunities(opportunity_id,kind,name,chain,status,ev_score,cost_usd,deadline_ms,evidence_json,created_ms,updated_ms,last_seen_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                 (ident,kind,ident,'sol',status,ev,'15.50',deadline,json.dumps(evidence or {}),stamp,stamp,stamp))


def test_default_mcp_view_does_not_advertise_refused_negative_ev_mints(tmp_db,monkeypatch):
    add(tmp_db,deadline=now_ms()-86400000)
    monkeypatch.setattr(server,'_conn',lambda:tmp_db)
    got=server.kaiba_opportunities()
    assert got['opportunities']==[]
    assert got['registry_count']==1
    assert got['qualified_count']==0


def verified_evidence():
    """50% of 231 immediate $1 units, less $15.50 gas = exactly $100 EV.

    Every scenario number and receipt is a fixture declaration, not a live capture.
    """
    stamp = now_ms()
    opp = OpportunityEvidence.model_validate({
        'kind': 'airdrop', 'name': 'Fixture Issuer', 'chain': 'sol', 'wallet_count': 1,
        'official_url': 'https://issuer.example/campaign', 'gas_cost_usd': '15.50',
        'meta': {'qualification': {
            'official_source_verified': True, 'eligibility_verified': True,
            'value_cost_verified': True, 'execution_path_verified': True,
            'participation_open': True, 'observed_ms': stamp,
        }},
    })
    source = 'fixture-issuer'

    def outcome(name, units, unlocks):
        return {'name': name, 'probability': '0.5', 'probability_source_id': source,
                'allocation_units': units, 'allocation_source_id': source, 'unlocks': unlocks,
                'claim_cost_usd': '0', 'sale_cost_usd': '0', 'execution_loss_usd': '0',
                'cost_source_id': source}

    bundle = {
        'version': 'airdrop-scenarios/1', 'opportunity_id': opp.key, 'chain': 'sol',
        'observed_ms': stamp, 'expires_ms': stamp + 3_600_000,
        'rules_source_id': source, 'cost_source_id': source,
        'cost_inputs_sha256': valuation_cost_binding(opp, CFG), 'discount_rate_annual': '0',
        'sources': [{'source_id': source, 'url': opp.official_url,
                     'sha256': hashlib.sha256(b'synthetic issuer valuation fixture').hexdigest(),
                     'observed_ms': stamp}],
        'outcomes': [outcome('allocation estimate', '231', [
            {'fraction': '1', 'delay_days': 0, 'price_usd': '1',
             'price_source_id': source, 'terms_source_id': source},
        ]), outcome('no allocation estimate', '0', [])],
    }
    current = OpportunityEvidence.model_validate({**opp.model_dump(mode='json'), 'valuation_scenarios': bundle})
    score = score_opportunity(current, CFG)
    assert score.ev_usd == Decimal('100.00') and score.cost_usd == Decimal('15.50')
    current.meta['qualification']['valuation_scenario_sha256'] = score.provenance['scenario_sha256']
    return current.model_dump(mode='json')


def test_positive_ev_with_no_verification_remains_a_lead(tmp_db,monkeypatch):
    add(tmp_db,kind='airdrop',status='open',ev=100)
    monkeypatch.setattr(server,'_conn',lambda:tmp_db)
    assert server.kaiba_opportunities()['opportunities']==[]


def test_family_aliases_preserve_rejected_discovery_in_audit_view(tmp_db,monkeypatch):
    add(tmp_db,ident='a',kind='airdrop')
    add(tmp_db,ident='p',kind='points')
    add(tmp_db,ident='n',kind='nft_mint')
    monkeypatch.setattr(server,'_conn',lambda:tmp_db)
    assert server.kaiba_opportunities(kind='airdrop',include_unqualified=True)['registry_count']==2
    assert server.kaiba_opportunities(kind='nft',include_unqualified=True)['registry_count']==1


def test_verified_research_candidate_is_not_trade_authorization(tmp_db,monkeypatch):
    add(tmp_db,kind='airdrop',status='open',ev=100,evidence=verified_evidence())
    monkeypatch.setattr(server,'_conn',lambda:tmp_db)
    got=server.kaiba_opportunities()
    assert got['qualified_count']==1
    assert got['opportunities'][0]['qualification']['funded_action_authorized'] is False


def test_a_new_fetch_does_not_refresh_stale_official_evidence(tmp_db,monkeypatch):
    evidence=verified_evidence()
    evidence['meta']['qualification']['observed_ms']=now_ms()-2*86400000
    add(tmp_db,kind='airdrop',status='open',ev=100,evidence=evidence)
    monkeypatch.setattr(server,'_conn',lambda:tmp_db)
    assert server.kaiba_opportunities()['qualified_count']==0


def test_nft_launch_start_is_not_treated_as_sale_expiry(tmp_db,monkeypatch):
    add(tmp_db,deadline=now_ms()-86400000)
    monkeypatch.setattr(server,'_conn',lambda:tmp_db)
    row=server.kaiba_opportunities(kind='nft',include_unqualified=True)['opportunities'][0]
    assert 'participation_deadline_passed' not in row['qualification']['reasons']
    assert 'launch start' in row['stored_date_semantics']


def test_bad_comparable_cannot_make_an_opportunity_qualified(tmp_db,monkeypatch):
    evidence=verified_evidence()
    evidence['expected_value_usd']=None
    evidence['prior_airdrop_history']=['not a number']
    evidence['valuation_scenarios']=None
    add(tmp_db,kind='airdrop',status='open',ev=100,evidence=evidence)
    monkeypatch.setattr(server,'_conn',lambda:tmp_db)
    assert server.kaiba_opportunities()['qualified_count']==0


def test_cli_defaults_to_qualified_and_has_explicit_audit_view(tmp_db,monkeypatch):
    from typer.testing import CliRunner

    from kaiba.cli.main import app
    from kaiba.core import db
    add(tmp_db,ident='refused-mint')
    monkeypatch.setattr(db,'ensure_db',lambda:tmp_db)
    normal=CliRunner().invoke(app,['hunt','list'])
    assert normal.exit_code==0
    assert 'no qualified opportunities' in normal.stdout.lower()
    audit=CliRunner().invoke(app,['hunt','list','--all','--kind','nft'])
    assert audit.exit_code==0 and 'refused-mint' in audit.stdout


def test_body_chain_mismatch_is_not_qualified(tmp_db,monkeypatch):
    evidence=verified_evidence()
    evidence['chain']='bsc'
    add(tmp_db,kind='airdrop',status='open',ev=100,evidence=evidence)
    monkeypatch.setattr(server,'_conn',lambda:tmp_db)
    assert server.kaiba_opportunities()['qualified_count']==0


def test_missing_wallet_count_is_not_assumed_single_wallet(tmp_db,monkeypatch):
    evidence=verified_evidence()
    evidence.pop('wallet_count')
    add(tmp_db,kind='airdrop',status='open',ev=100,evidence=evidence)
    monkeypatch.setattr(server,'_conn',lambda:tmp_db)
    assert server.kaiba_opportunities()['qualified_count']==0
