"""Test the exact proposed one-line scheduler integration IN MEMORY only.

This test never modifies kaiba/ops/scheduler.py. Parent owns that integration.
"""
import ast
import pytest
import logging
from pathlib import Path
from types import SimpleNamespace
from kaiba.hunters import airdrops,nft,listings
from kaiba.hunters.ev import OpportunityEvidence,OpportunityKind


def test_proposed_scheduler_selects_structured_research_receipts(tmp_db,monkeypatch):
    source=(Path(__file__).parents[1]/'kaiba'/'ops'/'scheduler.py').read_text()
    tree=ast.parse(source)
    node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='job_hunters')
    original=ast.get_source_segment(source,node)
    old='refresh = getattr(mod, "refresh", None) or getattr(mod, "sweep", None)'
    new='refresh = getattr(mod, "refresh_report", None) or getattr(mod, "refresh", None) or getattr(mod, "sweep", None)'
    if original.count(old)==1:
        proposed=original.replace(old,new)
    else:
        assert original.count(new)==1
        proposed=original  # after parent integration, test the actual implementation
    scope={'log':logging.getLogger(__name__)}
    exec(compile('from __future__ import annotations\n'+proposed,'<explicit-scheduler-proposal>','exec'),scope)
    opp=OpportunityEvidence(kind=OpportunityKind.AIRDROP,name='Fixture research lead',sources=['fixture'])
    monkeypatch.setattr(airdrops,'COLLECTORS',{'fixture':lambda conn=None:[opp]})
    # 2026-10-02: OpenSea's drop list replaced Magic Eden in the default NFT sweep.
    monkeypatch.setattr(nft,'magic_eden_launchpad',lambda conn=None:pytest.fail('ME left the sweep'))
    monkeypatch.setattr(nft,'opensea_drops',lambda conn=None:[])
    monkeypatch.setattr(listings,'refresh',lambda conn=None:0)
    monkeypatch.setenv('KAIBA_ENABLE_NFT_HUNTER','1')
    result=scope['job_hunters'](SimpleNamespace(conn=tmp_db))
    assert result['airdrop']['written']==1
    assert result['airdrop']['new_count']==1 and result['airdrop']['qualified_count']==0
    assert result['nft']['sources']['helius_mint_watch']['state']=='no_deliveries'
    assert result['nft']['qualified_count']==0
    assert result['listing']==0
    assert all(result[k]['funded_action_authorized'] is False for k in ('airdrop','nft'))
