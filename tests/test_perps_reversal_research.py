"""Economics/causality controls for the offline intraday perps hypotheses.

Fictional inputs only. No profitability, feed or execution proof.
"""
from __future__ import annotations

import copy
import json

import pytest

from kaiba.perps import reversal_research as rr


def bar(i, **kw):
    return dict(t=i*rr.MINUTE, available_ms=(i+1)*rr.MINUTE,
                o=100., h=100.05, l=99.95, c=100., volume=100.,
                liq_basis="observed", long_liq_usd=0., short_liq_usd=0.,
                buy_usd=50., sell_usd=50.) | kw


def quote(t, price=100., **kw):
    return dict(t=t, available_ms=t, bid=price, ask=price, mark=price) | kw


def data(bars=None, quotes=None, **kw):
    return dict(schema=rr.VERSION, symbol="BTCUSDT", venue="fictional",
                contract_type="linear", quote_asset="USDT",
                source="fixture", bars=bars or [bar(0, long_liq_usd=20_000)],
                quotes=quotes or [], funding=[], funding_complete=True,
                fee_bps_per_side=0., slippage_bps_per_side=0.) | kw


def base(d, stress=False):
    return rr.run(d)["results"]["LF"]["cost_stress_2x" if stress else "base"]


def test_entry_waits_for_closed_bar_availability_and_delay():
    d = data(quotes=[quote(64_999, 90), quote(65_000), quote(66_000, 100.5)])
    r = base(d)
    assert r["closed"] == 1
    assert r["trades"][0]["entry_ms"] == 65_000
    assert r["trades"][0]["entry"] == 100
    assert r["trades"][0]["reason"] == "target"
    assert r["net_pnl"] == pytest.approx(.5)


def test_late_backfill_does_not_get_historical_price():
    d = data([bar(0, available_ms=100_000, long_liq_usd=20_000)],
             [quote(65_000), quote(105_000, 101), quote(106_000, 104)])
    t = base(d)["trades"][0]
    assert t["entry_ms"] == 105_000 and t["entry"] == 101


def test_entry_requires_quote_observed_after_ready_time_not_only_received_late():
    p = rr.Policy(max_quote_age_ms=20_000)
    d = data(quotes=[quote(60_000, 90, available_ms=70_000),
                     quote(70_000), quote(71_000, 100.5)])
    t = rr.run(d, p)["results"]["LF"]["base"]["trades"][0]
    assert t["entry_ms"] == 70_000 and t["entry"] == 100


def test_fills_charge_ask_bid_spread_and_both_fee_notionals():
    d = data(quotes=[quote(65_000, ask=100.02, bid=99.98),
                     quote(66_000, 100.5, bid=100.48, ask=100.52)], fee_bps_per_side=2)
    t = base(d)["trades"][0]
    qty = 100/100.02
    expected = qty*(100.48-100.02) - (100+qty*100.48)*.0002
    assert t["modeled_pnl"] == pytest.approx(expected)
    assert base(d)["net_pnl"] == pytest.approx(expected)
    assert base(d, True)["modeled_closed_pnl"] < base(d)["modeled_closed_pnl"]


def test_break_even_exit_is_not_cost_free():
    d = data(quotes=[quote(65_000), quote(66_000, 100.12),
                     quote(67_000, 100, bid=99.99, ask=100.01)], fee_bps_per_side=2)
    t = base(d)["trades"][0]
    assert t["reason"] == "stop" and t["modeled_pnl"] < 0


def test_mark_triggers_stop_even_when_tradeable_bid_is_above_stop():
    d = data(quotes=[quote(65_000), quote(66_000, 100, mark=99.7)])
    assert base(d)["trades"][0]["reason"] == "stop"


def test_short_fades_short_liquidations_and_exits_on_ask():
    d = data([bar(0, short_liq_usd=20_000)],
             [quote(65_000), quote(66_000, 99.5, ask=99.55)])
    t = base(d)["trades"][0]
    assert t["side"] == -1 and t["exit"] == 99.55
    assert t["modeled_pnl"] == pytest.approx(.45)


@pytest.mark.parametrize("side", [1, -1])
def test_funding_uses_settlement_mark_and_real_position_quantity(side):
    b = bar(0, long_liq_usd=20_000 if side == 1 else 0, short_liq_usd=20_000 if side == -1 else 0)
    d = data([b], [quote(65_000), quote(66_000, 100+side*.5)],
             funding=[dict(t=65_000, mark=100., rate=.1),
                      dict(t=65_500, mark=102., rate=.001),
                      dict(t=67_000, mark=100., rate=.1)])
    t = base(d)["trades"][0]
    assert t["funding_paid"] == pytest.approx(side*.102)
    assert t["modeled_pnl"] == pytest.approx(.5-side*.102)


@pytest.mark.parametrize("basis", ["proxy", "snapshot", "unknown"])
def test_liquidation_proxy_and_sampled_snapshots_do_not_masquerade_as_full_events(basis):
    d = data([bar(0, long_liq_usd=20_000, liq_basis=basis)])
    sig, gaps = rr.signals(d)
    assert not any(s["arm"].startswith("LF") for s in sig)
    assert gaps["liquidation_input_unqualified"] == 1


def test_unknown_liquidations_are_not_zero_liquidation_exhaustion():
    xs = [bar(i) for i in range(10)] + [bar(10, long_liq_usd=300_000),
          bar(11, long_liq_usd=None, c=100.02)]
    sig, _ = rr.signals(data(xs))
    assert not any(s["arm"] == "LF_CASCADE" for s in sig)


def test_cascade_waits_for_reversal_after_forced_selling_subsides():
    xs = [bar(i) for i in range(10)] + [bar(10, long_liq_usd=300_000),
          bar(11, long_liq_usd=200_000, o=100.02, c=100),
          bar(12, o=100, c=100.02)]
    sig, _ = rr.signals(data(xs))
    cs = [s for s in sig if s["arm"] == "LF_CASCADE"]
    assert len(cs) == 1 and cs[0]["bar_ms"] == 12*rr.MINUTE and cs[0]["side"] == 1


def test_flow_confirmation_is_causal_and_explicitly_missing():
    xs = [bar(0, long_liq_usd=20_000, c=100.02, buy_usd=80, sell_usd=20),
          bar(1, long_liq_usd=20_000, c=100.02, buy_usd=None)]
    sig, gaps = rr.signals(data(xs))
    assert len([s for s in sig if s["arm"] == "LF_FLOW"]) == 1
    assert gaps["LF_FLOW:unknown_flow"] == 1


def test_sweep_reclaim_uses_only_prior_highs_and_lows():
    xs = [bar(i) for i in range(20)] + [bar(20, l=99.8, c=100.01)]
    sig, _ = rr.signals(data(xs))
    assert any(s["arm"] == "SWEEP_RECLAIM" and s["side"] == 1 for s in sig)
    xs.append(bar(21, h=200, c=100))
    sig2, _ = rr.signals(data(xs))
    assert [s for s in sig2 if s["bar_ms"] <= 20*rr.MINUTE] == sig


def test_vwap_reclaim_requires_recovery_after_a_prior_stretch():
    xs = [bar(i) for i in range(19)] + [bar(19, l=98.9, c=99),
          bar(20, o=99.9, h=100.3, l=99.9, c=100.2)]
    sig, _ = rr.signals(data(xs))
    assert any(s["arm"] == "VWAP_RECLAIM" and s["side"] == 1 for s in sig)
    xs[-1] = bar(20, o=99.9, l=99.8, c=99.9)
    sig, _ = rr.signals(data(xs))
    assert not any(s["arm"] == "VWAP_RECLAIM" for s in sig)


@pytest.mark.parametrize("side", [1, -1])
def test_trend_comparator_uses_prior_moving_averages_and_pullback(side):
    xs = []
    for i in range(60):
        p = 90+i*.1 if side == 1 else 110-i*.1
        xs.append(bar(i, o=p, c=p, l=p-.05, h=p+.05))
    xs.append(bar(60, o=94.9, l=94.8, h=95.2, c=95.1) if side == 1 else
              bar(60, o=105.1, l=104.8, h=105.2, c=104.9))
    sig, _ = rr.signals(data(xs))
    assert any(s["arm"] == "TREND_PULLBACK" and s["side"] == side for s in sig)


def test_open_position_blocks_subsequent_signal_in_same_book():
    d = data([bar(0, long_liq_usd=20_000), bar(1, long_liq_usd=20_000)],
             [quote(65_000), quote(125_000)])
    r = base(d)
    assert r["open"] == 1 and r["skips"]["position_busy"] == 1


@pytest.mark.parametrize("change", ["funding", "gap", "open"])
def test_incomplete_economics_withholds_net_results(change):
    qs = [quote(65_000), quote(66_000, 100.5)]
    d = data(quotes=qs)
    if change == "funding":
        d["funding_complete"] = False
    if change == "gap":
        qs[1] = quote(70_000, 100.5)
    if change == "open":
        qs.pop()
    r = base(d)
    assert r["net_pnl"] is None
    assert r["economic_coverage"] == "incomplete_or_empty"


def test_no_eligible_quote_does_not_invent_a_fill():
    r = base(data(quotes=[quote(61_000)]))
    assert r["closed"] == 0 and r["net_pnl"] is None
    assert r["skips"]["no_quote_after_signal"] == 1


def test_expired_signal_never_gets_filled():
    r = base(data(quotes=[quote(200_000)]))
    assert not r["trades"] and r["skips"]["expired_signal"] == 1


def test_stale_quote_cannot_supply_a_retroactive_entry():
    d = data(quotes=[quote(65_000, available_ms=68_000)])
    r = base(d)
    assert not r["trades"] and r["open"] == 0 and r["skips"]["stale_quote"] == 1


def test_inverse_contract_cannot_use_linear_pnl_arithmetic():
    d = data(contract_type="inverse")
    with pytest.raises(ValueError, match="linear_quote_contract_required"):
        rr.run(d)


def test_fixed_capital_prevents_entry_even_when_a_signal_exists():
    d = data(quotes=[quote(65_000), quote(66_000, 100.5)], fee_bps_per_side=1)
    r = rr.run(d, rr.Policy(initial_cash=100, notional=100))["results"]["LF"]["base"]
    assert r["closed"] == 0 and r["skips"]["cash_unavailable"] == 1


def test_empty_data_is_never_zero_profit_or_promotable():
    d = data()
    d["bars"] = []
    r = rr.run(d)
    assert r["trial_count"] == 6 and not r["promotable"] and not r["execution_allowed"]
    assert r["verdict"] == "UNPROVEN"
    assert all(v["base"]["net_pnl"] is None for v in r["results"].values())


@pytest.mark.parametrize("mutation", ["nan", "open_bar", "crossed", "duplicate", "missing_funding"])
def test_bad_input_is_refused(mutation):
    d = data(quotes=[quote(65_000)])
    if mutation == "nan":
        d["bars"][0]["c"] = float("nan")
    elif mutation == "open_bar":
        d["bars"][0]["available_ms"] = 59_999
    elif mutation == "crossed":
        d["quotes"][0]["bid"] = 102
    elif mutation == "duplicate":
        d["quotes"].append(copy.deepcopy(d["quotes"][0]))
    else:
        d.pop("funding_complete")
    with pytest.raises(ValueError):
        rr.run(d)


def test_cli_runs_existing_package_without_network_or_overwriting_report(tmp_path):
    src, dst = tmp_path/"input.json", tmp_path/"report.json"
    src.write_text(json.dumps(data(quotes=[quote(65_000), quote(66_000, 100.5)])))
    assert rr.main(["--input", str(src), "--output", str(dst)]) == 0
    assert json.loads(dst.read_text())["execution_allowed"] is False
    with pytest.raises(FileExistsError):
        rr.main(["--input", str(src), "--output", str(dst)])
