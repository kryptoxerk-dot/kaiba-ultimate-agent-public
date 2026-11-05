"""Hermetic sizing witnesses: snapshot fit must never imply execution or profit."""
from copy import deepcopy
from decimal import Decimal

import pytest

from kaiba.perps.order_constraints import assess_market_order, portfolio_from_companion, portfolio_snapshot

NOW = 1_793_725_349_003


def symbol(name="BTCUSDT", *, lo="0.001", hi="120", step="0.001", minimum="50"):
    return {"symbol": name, "status": "TRADING", "contractType": "PERPETUAL",
            "quoteAsset": "USDT", "marginAsset": "USDT", "quantityPrecision": 0,
            "orderTypes": ["MARKET"], "filters": [
                {"filterType": "LOT_SIZE", "minQty": lo, "maxQty": hi, "stepSize": step},
                {"filterType": "MARKET_LOT_SIZE", "minQty": lo, "maxQty": hi, "stepSize": step},
                {"filterType": "MIN_NOTIONAL", "notional": minimum}]}


def mark(name="BTCUSDT", price="77760.8"):
    return {"symbol": name, "markPrice": price, "time": NOW}


def test_small_btc_allocation_is_not_rounded_up_to_minimum():
    bad = assess_market_order(symbol(), mark(), "50", NOW)
    assert bad["status"] == "INFEASIBLE" and bad["reason"] == "below_min_quantity"
    assert Decimal(bad["indicative_admissible_signed_notional"]) == 0
    assert Decimal(bad["minimum_filter_fit_notional"]) == Decimal("77.7608")
    good = assess_market_order(symbol(), mark(), "100", NOW)
    assert good["status"] == "FITS_SNAPSHOT_FILTERS"
    assert Decimal(good["rounded_quantity"]) == Decimal(".001")
    assert Decimal(good["unallocated_signed_notional"]) == Decimal("22.2392")
    assert good["execution_authorized"] is False and good["margin_feasibility"] == "UNKNOWN"


def test_step_size_not_decimal_precision_and_short_rounds_toward_zero():
    result = assess_market_order(symbol(step=".025", lo=".025", minimum=".1"), mark(price="10"), "-.999", NOW)
    assert result["status"] == "FITS_SNAPSHOT_FILTERS"
    assert Decimal(result["rounded_quantity"]) == Decimal(".075")
    assert Decimal(result["unallocated_signed_notional"]) == Decimal("-.249")


def test_intersection_honors_both_distinct_grids_and_min_origins():
    info = symbol(lo=".01", step=".02", minimum=".1")
    info["filters"][1].update(minQty=".03", stepSize=".06")
    r = assess_market_order(info, mark(price="10"), "1.29", NOW)
    assert r["status"] == "FITS_SNAPSHOT_FILTERS"
    assert Decimal(r["rounded_quantity"]) == Decimal(".09")
    assert Decimal(r["minimum_filter_fit_notional"]) == Decimal(".3")


def test_no_intersection_stays_unknown():
    info = symbol(lo=".01", step=".02")
    info["filters"][1].update(minQty=".02", stepSize=".02")
    assert assess_market_order(info, mark(), "100", NOW)["reason"] == "incompatible_quantity_grids"


def test_market_max_and_min_notional_checked_after_rounding():
    info = symbol(lo=".1", step=".1", hi=".2", minimum="1.5")
    assert assess_market_order(info, mark(price="10"), "1.99", NOW)["reason"] == "below_min_notional"
    assert assess_market_order(info, mark(price="10"), "3", NOW)["reason"] == "above_max_quantity"
    info["filters"][0]["maxQty"] = ".1"
    assert assess_market_order(info, mark(price="10"), "2", NOW)["reason"] == "above_max_quantity"


def test_minimum_notional_can_require_several_quantity_steps():
    r = assess_market_order(symbol(minimum="100"), mark(), "100", NOW)
    assert r["reason"] == "below_min_notional"
    assert Decimal(r["rounded_quantity"]) == Decimal(".001")
    assert Decimal(r["minimum_filter_fit_notional"]) == Decimal("155.5216")


@pytest.mark.parametrize("delta", [-1, 60_001])
def test_bad_price_clock_is_unknown_with_fresh_positive_control(delta):
    assert assess_market_order(symbol(), mark(), "100", NOW+delta)["status"] == "UNKNOWN"
    assert assess_market_order(symbol(), mark(), "100", NOW)["status"] == "FITS_SNAPSHOT_FILTERS"


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-1", "0", 1.2, True])
def test_invalid_price_is_not_an_executable_zero(bad):
    assert assess_market_order(symbol(), mark(price=bad), "100", NOW)["status"] == "UNKNOWN"


@pytest.mark.parametrize("change,reason", [
    (lambda s: s["filters"].pop(0), "missing_quantity_filter"),
    (lambda s: s["filters"].append(deepcopy(s["filters"][0])), "duplicate_filter"),
    (lambda s: s["filters"].append({"filterType": "NEW_LIMIT"}), "unsupported_filter:NEW_LIMIT"),
    (lambda s: s["filters"][2].update(applyToMarket=False), "unsupported_notional_semantics"),
    (lambda s: s["filters"][1].update(stepSize="0"), "nonpositive_decimal"),
    (lambda s: s.update(status="CLOSE"), "unsupported_or_inactive_market"),
    (lambda s: s.update(marginAsset="BTC"), "unsupported_or_inactive_market"),
    (lambda s: s["filters"].append({"filterType": "POSITION_RISK_CONTROL", "positionControlSide": "LONG"}),
     "position_control_unassessed"),
])
def test_unproven_filter_semantics_not_silently_ignored(change, reason):
    info = symbol()
    change(info)
    assert assess_market_order(info, mark(), "100", NOW)["reason"] == reason


def test_failed_long_leaves_visible_net_short_without_reallocation():
    symbols = [symbol(), symbol("ETHUSDT", minimum="20")]
    prices = [mark(), mark("ETHUSDT", "2298.80566667")]
    r = portfolio_snapshot({"symbols": symbols}, prices, {"BTCUSDT": "50", "ETHUSDT": "-50"}, NOW)
    assert r["status"] == "FILTER_MISMATCH" and r["infeasible_legs"] == 1
    assert Decimal(r["intended_net_notional"]) == 0
    assert Decimal(r["known_indicative_net_notional"]) < 0
    assert abs(Decimal(r["known_indicative_gross_notional"])) < 50
    assert r["execution_authorized"] is False


@pytest.mark.parametrize("kind", ["missing", "duplicate_symbol", "duplicate_mark"])
def test_unknown_leg_kept_in_denominator(kind):
    symbols, prices = [symbol()], [mark()]
    if kind == "missing":
        prices = []
    elif kind == "duplicate_symbol":
        symbols += [symbol()]
    else:
        prices += [mark()]
    r = portfolio_snapshot({"symbols": symbols}, prices, {"BTCUSDT": "100"}, NOW)
    assert r["status"] == "UNKNOWN" and r["unknown_legs"] == 1
    assert Decimal(r["intended_gross_notional"]) == 100
    assert r["rows"][0]["reason"] == "missing_or_duplicate_input"


def test_inputs_are_not_mutated_and_repeat_is_identical():
    info, price = symbol(), mark()
    original = deepcopy((info, price))
    assert assess_market_order(info, price, "100", NOW) == assess_market_order(info, price, "100", NOW)
    assert (info, price) == original


def test_empty_portfolio_cannot_be_declared_sized():
    with pytest.raises(ValueError, match="empty_portfolio"):
        portfolio_snapshot({"symbols": []}, [], {}, NOW)


def test_actual_lead_companion_is_consumed_losslessly():
    from kaiba.perps.xs_paper import order_filters_snapshot

    info = {"symbols": [symbol()], "serverTime": NOW-10_000}
    companion = order_filters_snapshot(info, ["BTCUSDT"], m=NOW, run_now_ms=NOW, received_wall_ms=NOW)
    r = portfolio_from_companion(companion, [mark()], {"BTCUSDT": "100"}, NOW)
    assert r["status"] == "SIZING_ONLY" and r["companion_issue"] is None
    assert r["metadata_generation_freshness"] == "UNKNOWN"
    assert r["rows"][0]["metadata_freshness"] == "CALLER_MUST_ESTABLISH"
    # Same actual producer with duplicate source records: never collapse to one fit.
    info["symbols"].append(symbol())
    duplicate = order_filters_snapshot(info, ["BTCUSDT"], m=NOW, run_now_ms=NOW, received_wall_ms=NOW)
    assert portfolio_from_companion(duplicate, [mark()], {"BTCUSDT": "100"}, NOW)["unknown_legs"] == 1
    info["symbols"] = [symbol()]
    info["symbols"][0]["filters"].append(deepcopy(info["symbols"][0]["filters"][0]))
    duplicate = order_filters_snapshot(info, ["BTCUSDT"], m=NOW, run_now_ms=NOW, received_wall_ms=NOW)
    assert portfolio_from_companion(duplicate, [mark()], {"BTCUSDT": "100"}, NOW)["rows"][0]["reason"] == "duplicate_filter"


@pytest.mark.parametrize("change", [
    lambda c: c.update(schema="kaiba/perps-xs-order-filters/1"),
    lambda c: c.update(received_wall_ms=NOW-300_001),
    lambda c: c.update(received_wall_ms=NOW+1),
    lambda c: c["symbols"]["BTCUSDT"].update(raw_symbols=[symbol("ETHUSDT")]),
    lambda c: c["symbols"]["BTCUSDT"].pop("raw_symbols"),
])
def test_old_or_incomplete_companion_cannot_gain_fit_from_summary(change):
    companion = {"schema": "kaiba/perps-xs-order-filters/2", "received_wall_ms": NOW,
                 "symbols": {"BTCUSDT": {"filters": {"MIN_NOTIONAL": {"notional": "5"}},
                                         "raw_symbols": [symbol()]}}}
    change(companion)
    r = portfolio_from_companion(companion, [mark()], {"BTCUSDT": "100"}, NOW)
    assert r["status"] == "UNKNOWN" and r["unknown_legs"] == 1 and r["companion_issue"]


def test_fresh_metadata_does_not_rejuvenate_old_marks():
    companion = {"schema": "kaiba/perps-xs-order-filters/2", "received_wall_ms": NOW,
                 "symbols": {"BTCUSDT": {"raw_symbols": [symbol()]}}}
    price = mark()
    price["time"] -= 60_001
    r = portfolio_from_companion(companion, [price], {"BTCUSDT": "100"}, NOW)
    assert r["status"] == "UNKNOWN" and r["rows"][0]["reason"] == "mark_clock_missing_stale_or_future"
