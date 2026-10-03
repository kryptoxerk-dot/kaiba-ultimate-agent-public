"""A freshly rebuilt dossier cannot make a stale price fresh."""
from decimal import Decimal
import pytest
from kaiba.core.schemas import Action,Chain,Decision,EvidenceBasis,Lane,LaneMode,Measure,Receipt,TokenDossier,now_ms
from kaiba.execution import engine

TOKEN='11111111111111111111111111111111'


def attempt(tmp_db,monkeypatch,*,age_ms=0,basis=EvidenceBasis.PROVIDER_REPORTED,receipt=True,elapsed_ms=0):
    at=now_ms()
    clock={"at":at}
    measure=Measure(value=Decimal('0.01'),basis=basis,receipt=Receipt(provider='fixture',endpoint='price',observed_at_ms=at-age_ms) if receipt else None,freshness_budget_s=15)
    dossier=TokenDossier(chain=Chain.SOL,address=TOKEN,price_usd=measure,built_at_ms=at)
    monkeypatch.setattr(engine,'now_ms',lambda:clock['at'])
    monkeypatch.setattr(engine,'load_dossier',lambda *a,**k:dossier)
    monkeypatch.setattr(engine,'_token_decimals',lambda *a,**k:(6,'verified_onchain','fixture'))
    def native(*a,**k):
        clock['at']+=elapsed_ms
        return Decimal(100),'fixture'
    monkeypatch.setattr(engine,'_native_usd',native)
    decision=Decision(decision_id='test',chain=Chain.SOL,token=TOKEN,lane=Lane.SM_TRENCHES,mode=LaneMode.LIVE,action=Action.ENTER,size_base_units=100000000)
    return engine._plan_min_out(decision,tmp_db)


def test_fresh_dossier_with_expired_price_refuses(tmp_db,monkeypatch):
    result=attempt(tmp_db,monkeypatch,age_ms=30000)
    assert result.value is None
    assert result.reason=='token_price_stale'


@pytest.mark.parametrize('kwargs',[{'age_ms':-1000},{'receipt':False},{'basis':EvidenceBasis.STALE}])
def test_unknown_future_or_stale_basis_price_never_plans(tmp_db,monkeypatch,kwargs):
    assert attempt(tmp_db,monkeypatch,**kwargs).value is None


def test_fresh_receipt_keeps_positive_floor(tmp_db,monkeypatch):
    assert attempt(tmp_db,monkeypatch,age_ms=1000).value>0


def test_price_expiring_during_lookups_does_not_become_a_plan(tmp_db,monkeypatch):
    assert attempt(tmp_db,monkeypatch,elapsed_ms=20000).value is None


def test_plan_retains_the_price_observation_clock(tmp_db,monkeypatch):
    plan=attempt(tmp_db,monkeypatch,age_ms=1000)
    assert plan.detail['token_price_observed_ms']>0
    assert plan.detail['token_price_freshness_budget_s']==15
    assert plan.detail['token_price_basis']=='provider_reported'
