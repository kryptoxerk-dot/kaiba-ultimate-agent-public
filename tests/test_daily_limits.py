"""Unlimited must be explicit; finite/zero limits and independent vetoes still bind."""
from dataclasses import replace
from decimal import Decimal

import pytest

from kaiba.core.schemas import Chain, EvidenceBasis
from kaiba.execution import snipe
from kaiba.execution.daily_limits import daily_amount, daily_count
from kaiba.ingest.launch_feed import Launch


def test_explicit_unlimited_and_finite_zero_controls():
    assert daily_count("unlimited") is None and daily_amount("unlimited") is None
    assert daily_count(5)==5 and daily_count(0)==0
    assert daily_amount("0.3")==Decimal("0.3")


@pytest.mark.parametrize("value",[None,True,-1,"NaN","Infinity","infinite","",{},[]])
def test_malformed_limits_cannot_become_unlimited(value):
    with pytest.raises(ValueError):
        daily_count(value)


def test_fractional_count_rejected():
    with pytest.raises(ValueError):
        daily_count("1.5")


@pytest.mark.parametrize("chain,venue",[(Chain.SOL,"pump.fun"),(Chain.BSC,"flap"),(Chain.ROBINHOOD,"pons")])
def test_unlimited_snipe_count_preserves_independent_veto(chain,venue):
    launch=Launch(chain=chain,token="fixture",creator=None,venue=venue,name="Project",symbol="PROJ",received_ms=1)
    p={**snipe.DEFAULT_PARAMS,"chains":[chain.value],"venues":{chain.value:[venue]},
       "name_watchlist":["PROJ"],"max_snipes_per_day":{chain.value:"unlimited"}}
    # Evaluate the real cap path; chain-specific evidence supplied by existing fixture types.
    record=snipe.Record(None,0,0,0,EvidenceBasis.UNAVAILABLE)
    kwargs={}
    if chain is Chain.ROBINHOOD:
        kwargs["book"]=snipe.EarlyBook(read=True,dev_buy_wei=10**18,outside_buys_window=1)
    if chain is Chain.BSC:
        # BSC's existing missing-portal veto is sufficient to prove daily override is narrow.
        kwargs["native_ok"]=True
    v=snipe.evaluate(launch,p,record,snipes_today=10**9,**kwargs)
    assert not any("daily_snipe_cap" in r for r in v.reasons)
    finite=snipe.evaluate(launch,{**p,"max_snipes_per_day":0},record,snipes_today=0,**kwargs)
    assert "daily_snipe_cap:0/0" in finite.reasons
    bad=snipe.evaluate(replace(launch,venue="unsupported"),p,record,snipes_today=10**9,**kwargs)
    assert "venue_not_enabled:unsupported" in bad.reasons
