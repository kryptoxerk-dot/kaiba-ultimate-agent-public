"""Financial counterexamples and finite-cash controls, with no external services."""
from decimal import Decimal
from fractions import Fraction

import pytest

from kaiba.learning.ticket_economics import (
    Costs,
    breakeven_gross,
    exit_floor,
    main,
    net_lamports,
    ticket_case,
)


def case(**changes):
    args = dict(notional=1_000_000_000, bankroll=3_294_269_853, reserve=50_000_000,
                daily_loss_stop=329_426_985, costs=Costs("handling", 100, 100))
    return ticket_case(**(args | changes))


def test_each_leg_fee_reduces_its_own_flow_and_fixed_costs_are_not_double_counted():
    assert net_lamports(1_000_000_000, "0", Costs("both", 100, 100)) == -19_900_000
    assert net_lamports(1_000_000_000, "0.10", Costs("both", 100, 100)) == 78_110_000
    assert net_lamports(1_000_000_000, "0.10", Costs("fixed", 100, 100, 1000, 2000)) == 78_107_000


def test_no_fee_control_and_loss_cannot_create_proceeds():
    assert net_lamports(300_000_000, ".10", Costs("none", 0, 0)) == 30_000_000
    assert net_lamports(300_000_000, "-1", Costs("total-loss", 100, 100, 1000, 2000)) == -300_003_000


def test_proceeds_are_rounded_down_not_optimistically_up():
    assert net_lamports(1, ".99", Costs("none", 0, 0)) == 0
    assert net_lamports(1, "-.01", Costs("none", 0, 0)) == -1


def test_exact_breakeven_inverts_fee_and_fixed_cashflows():
    costs = Costs("both", 100, 100, 1000, 2000)
    threshold = breakeven_gross(300_000_000, costs)
    assert threshold == Fraction(100001, 100000) / Fraction(9801, 10000) - 1
    with pytest.raises(ValueError):
        net_lamports(300_000_000, threshold, costs)  # implicit float/Fraction conversion forbidden
    assert net_lamports(300_000_000, "0.020314", costs) < 0
    assert net_lamports(300_000_000, "0.020315", costs) >= 0


def test_exit_floor_covers_actual_entry_debit_without_recharging_entry_or_venue_costs():
    args = dict(entry_debit=303_015_010, exit_bps=100, fixed_exit=15_010)
    below = exit_floor(exit_quote=306_090_929, **args)
    exact = exit_floor(exit_quote=306_090_930, **args)
    assert below["break_even_exit_quote_lamports"] == exact["break_even_exit_quote_lamports"] == 306_090_930
    assert below["modeled_net_lamports"] == -1
    assert exact["modeled_net_lamports"] == 0
    assert exact["funding_allowed"] is False
    assert exit_floor(entry_debit=100, exit_quote=100, exit_bps=0, fixed_exit=0)["modeled_net_lamports"] == 0


def test_high_turnover_fee_burn_does_not_ignore_finite_daily_loss_stop():
    row = case()
    assert row["unconstrained_flat_price_pnl_lamports"] == -1_990_000_000
    assert row["finite_cash_flat_round_trips_completed"] == 17
    assert row["finite_cash_flat_end_lamports"] == 2_955_969_853
    assert row["finite_cash_flat_stop_reason"] == "daily_loss_stop_before_next_entry"


def test_daily_stop_limits_the_next_entry_and_cannot_guarantee_one_loss_stays_below_it():
    row = case(costs=Costs("audit", 325, 325, 1_000_000, 1_000_000))
    assert row["modeled_stop_net_lamports"] == -346_760_625
    assert row["one_modeled_stop_exceeds_daily_loss_limit"] is True
    assert row["above_assumed_max_ticket_fraction"] is True
    assert case(notional=300_000_000)["above_assumed_max_ticket_fraction"] is False


def test_cash_reserve_positive_control_and_no_implicit_leverage():
    costs = Costs("none", 0, 0)
    refused = case(bankroll=1_049_999_999, costs=costs)
    assert refused["finite_cash_flat_round_trips_completed"] == 0
    assert refused["finite_cash_flat_stop_reason"] == "cash_reserve_before_next_entry"
    admitted = case(bankroll=1_050_000_000, costs=costs)
    assert admitted["finite_cash_flat_round_trips_completed"] == 100
    assert admitted["finite_cash_flat_end_lamports"] == 1_050_000_000


def test_exit_fee_is_reserved_before_entry_and_does_not_depend_on_a_future_sale():
    row = case(bankroll=1_051_999_999, costs=Costs("fixed", 0, 0, 1_000_000, 1_000_000))
    assert row["cash_reserved_for_entry_and_exit_lamports"] == 1_002_000_000
    assert row["finite_cash_flat_round_trips_completed"] == 0
    control = case(bankroll=1_052_000_000, costs=Costs("fixed", 0, 0, 1_000_000, 1_000_000), round_trips=1)
    assert control["finite_cash_flat_round_trips_completed"] == 1
    assert control["finite_cash_flat_end_lamports"] == 1_050_000_000


def test_more_fees_never_improve_net_and_smaller_fixed_fee_does_not_change_gross_returns():
    for gross in ("-0.3", "0", "0.03", "0.10", "1"):
        for amount in (300_000_000, 500_000_000, 1_000_000_000):
            assert net_lamports(amount, gross, Costs("high", 325, 325)) <= net_lamports(amount, gross, Costs("low", 100, 100))
    costs = Costs("fixed", 0, 0, 1_000_000, 1_000_000)
    assert breakeven_gross(300_000_000, costs) > breakeven_gross(1_000_000_000, costs)


@pytest.mark.parametrize("gross", [True, 0.1, "NaN", "Infinity", "-1.0001", "1e999999", "1e-999999", "0E+999999999", "1E+999999999"])
def test_malformed_or_unbounded_returns_are_not_money(gross):
    with pytest.raises(ValueError):
        net_lamports(1_000_000_000, gross, Costs("test", 100, 100))


@pytest.mark.parametrize("amount", [True, 0, -1, 1.2, "300000000"])
def test_amounts_must_be_positive_integer_lamports(amount):
    with pytest.raises(ValueError):
        case(notional=amount)


@pytest.mark.parametrize("fee", [True, -1, 10000, 1.2])
def test_impossible_fee_assumptions_refuse(fee):
    with pytest.raises(ValueError):
        Costs("test", fee, 100)


def test_every_output_withholds_profit_and_risk_permission():
    row = case()
    assert row["funding_allowed"] is False and row["profit_proven"] is False
    assert "executable_entry_and_exit" in row["unknown"]
    assert row["required_mean_net_return_pct_at_requested_turnover"] == "2.000000"
    assert row["cash_only_concurrent_upper_bound_not_risk_permission"] == 3


def test_cli_requires_explicit_bankroll_and_refuses_sub_lamport_amount(capsys):
    assert main(["--bankroll-sol", "3.294269853", "--reserve-sol", ".05",
                 "--daily-loss-stop-sol", ".329426985", "--ticket-sol", ".0000000001"]) == 2
    assert '"funding_allowed": false' in capsys.readouterr().out
    assert isinstance(breakeven_gross(1_000_000_000, Costs("none", 0, 0)), Fraction)
    assert net_lamports(1_000_000_000, Decimal("0.1"), Costs("none", 0, 0)) == 100_000_000
