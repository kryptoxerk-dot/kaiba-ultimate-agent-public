"""Pure ticket-size economics; hypothetical cashflows, never execution evidence.

Owner: Codex, SOL-METHOD-LAB-20261103. Not an admission gate or backtest.
The input notional includes percentage entry fees; fixed fees are additional.
Both percentage fees reduce swap value. Liquidity, impact, tax, latency, failed
orders and rent are NOT known here. A larger ticket's old small-trade return is
never reused. No configuration, provider, journal or execution imports.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from fractions import Fraction

LAMPORTS = 1_000_000_000
VERSION = "ticket-economics/1"


def _integer(value: int, *, positive: bool = False) -> int:
    if type(value) is not int or value < (1 if positive else 0) or value > 10**18:
        raise ValueError("invalid_integer_amount")
    return value


def _ratio(value: str | Decimal) -> Fraction:
    if not isinstance(value, (str, Decimal)) or len(str(value)) > 80:
        raise ValueError("invalid_ratio")
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        raise ValueError("invalid_ratio") from None
    if not parsed.is_finite() or parsed.copy_abs() > 1_000 or abs(parsed.as_tuple().exponent) > 30:
        raise ValueError("invalid_ratio")
    return Fraction(parsed)


def _decimal(value: Fraction, places: int = 6) -> str:
    with localcontext() as ctx:
        ctx.prec = 70
        return format((Decimal(value.numerator) / Decimal(value.denominator))
                      .quantize(Decimal(1).scaleb(-places)), "f")


@dataclass(frozen=True)
class Costs:
    label: str
    entry_bps: int
    exit_bps: int
    fixed_entry_lamports: int = 0
    fixed_exit_lamports: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.label, str) or not 0 < len(self.label) <= 100:
            raise ValueError("invalid_label")
        for fee in (self.entry_bps, self.exit_bps):
            if type(fee) is not int or not 0 <= fee < 10_000:
                raise ValueError("invalid_fee_bps")
        _integer(self.fixed_entry_lamports)
        _integer(self.fixed_exit_lamports)

    @property
    def retained(self) -> Fraction:
        return Fraction(10_000 - self.entry_bps, 10_000) * Fraction(10_000 - self.exit_bps, 10_000)


def net_lamports(notional: int, gross_return: str | Decimal, costs: Costs) -> int:
    """Modeled completed round trip, rounding proceeds DOWN to whole lamports."""
    _integer(notional, positive=True)
    gross = _ratio(gross_return)
    if gross < -1:
        raise ValueError("return_below_total_loss")
    proceeds = notional * (1 + gross) * costs.retained
    return proceeds.numerator // proceeds.denominator - notional - costs.fixed_entry_lamports - costs.fixed_exit_lamports


def breakeven_gross(notional: int, costs: Costs) -> Fraction:
    _integer(notional, positive=True)
    fixed = costs.fixed_entry_lamports + costs.fixed_exit_lamports
    return (1 + Fraction(fixed, notional)) / costs.retained - 1


def exit_floor(*, entry_debit: int, exit_quote: int, exit_bps: int, fixed_exit: int) -> dict:
    """Compare a post-venue exit quote before separately charged router and network costs."""
    _integer(entry_debit, positive=True)
    _integer(exit_quote)
    costs = Costs("explicit_remaining_exit_costs", 0, exit_bps, fixed_exit_lamports=fixed_exit)
    required = Fraction(entry_debit + fixed_exit, 1) / costs.retained
    floor = -(-required.numerator // required.denominator)
    modeled_exit = exit_quote * (10_000 - exit_bps) // 10_000 - fixed_exit
    return {
        "version": VERSION, "basis": "hypothetical_post_venue_exit_quote", "funding_allowed": False,
        "profit_proven": False, "entry_debit_lamports": entry_debit, "exit_quote_lamports": exit_quote,
        "exit_bps": exit_bps, "fixed_exit_lamports": fixed_exit,
        "break_even_exit_quote_lamports": floor, "modeled_exit_credit_lamports": modeled_exit,
        "modeled_net_lamports": modeled_exit - entry_debit,
    }


def ticket_case(*, notional: int, bankroll: int, reserve: int, daily_loss_stop: int,
                costs: Costs, round_trips: int = 100, target_profit: int = 2 * LAMPORTS,
                gross_stop: str | Decimal = "-0.30", max_ticket_fraction: str | Decimal = "0.30") -> dict:
    """Finite sequential-cash sensitivity; every price outcome here is hypothetical.

    A daily stop is checked BEFORE the next entry, so a realized loss can overshoot
    it. Cash-only capacity ignores basket limits and is not permission to trade.
    This is not the production RiskGate and never changes its decisions.
    """
    for value in (notional, bankroll, daily_loss_stop, target_profit):
        _integer(value, positive=True)
    _integer(reserve)
    if reserve >= bankroll or type(round_trips) is not int or not 1 <= round_trips <= 100_000:
        raise ValueError("invalid_cash_scenario")
    cap = _ratio(max_ticket_fraction)
    stop = _ratio(gross_stop)
    if not 0 < cap <= 1 or not -1 <= stop < 0:
        raise ValueError("invalid_risk_scenario")
    debit = notional + costs.fixed_entry_lamports
    reserved_cost = debit + costs.fixed_exit_lamports
    flat_net = net_lamports(notional, "0", costs)
    stop_net = net_lamports(notional, gross_stop, costs)
    cash, completed, reason = bankroll, 0, "requested_turnover_completed"
    for _ in range(round_trips):
        if cash - bankroll <= -daily_loss_stop:
            reason = "daily_loss_stop_before_next_entry"
            break
        if cash - reserved_cost < reserve:
            reason = "cash_reserve_before_next_entry"
            break
        cash += flat_net
        completed += 1
    return {
        "version": VERSION, "basis": "hypothetical_cost_sensitivity", "funding_allowed": False,
        "profit_proven": False, "notional_lamports": notional, "bankroll_lamports": bankroll,
        "reserve_lamports": reserve, "daily_loss_stop_lamports": daily_loss_stop,
        "costs": {"label": costs.label, "entry_bps": costs.entry_bps, "exit_bps": costs.exit_bps,
                  "fixed_entry_lamports": costs.fixed_entry_lamports,
                  "fixed_exit_lamports": costs.fixed_exit_lamports},
        "entry_debit_lamports": debit,
        "cash_reserved_for_entry_and_exit_lamports": reserved_cost,
        "ticket_share_of_bankroll_pct": _decimal(Fraction(notional, bankroll) * 100),
        "above_assumed_max_ticket_fraction": Fraction(notional, bankroll) > cap,
        "cash_only_concurrent_upper_bound_not_risk_permission": (bankroll - reserve) // reserved_cost,
        "breakeven_gross_move_pct": _decimal(breakeven_gross(notional, costs) * 100),
        "flat_price_net_per_round_trip_lamports": flat_net,
        "requested_round_trips": round_trips,
        "unconstrained_flat_price_pnl_lamports": flat_net * round_trips,
        "finite_cash_flat_round_trips_completed": completed,
        "finite_cash_flat_end_lamports": cash,
        "finite_cash_flat_stop_reason": reason,
        "gross_stop_scenario": str(gross_stop), "modeled_stop_net_lamports": stop_net,
        "one_modeled_stop_exceeds_daily_loss_limit": -stop_net > daily_loss_stop,
        "full_notional_plus_fixed_cost_at_risk_lamports": debit + costs.fixed_exit_lamports,
        "target_profit_lamports": target_profit,
        "required_mean_net_return_pct_at_requested_turnover":
            _decimal(Fraction(target_profit, notional * round_trips) * 100),
        "unknown": ["executable_entry_and_exit", "liquidity_and_own_impact", "latency_and_drift",
                    "additional_venue_or_transfer_fees", "failed_and_unsellable_exits", "rent"],
    }


def _sol(value: str) -> int:
    number = _ratio(value) * LAMPORTS
    if number < 0 or number.denominator != 1:
        raise ValueError("invalid_sol_amount")
    return number.numerator


def main(argv=None) -> int:
    args_in = sys.argv[1:] if argv is None else argv
    if args_in and args_in[0] == "exit-floor":
        parser = argparse.ArgumentParser(description=exit_floor.__doc__)
        parser.add_argument("--entry-debit-sol", required=True)
        parser.add_argument("--exit-quote-sol", required=True)
        parser.add_argument("--exit-bps", type=int, required=True)
        parser.add_argument("--fixed-exit-sol", required=True)
        args = parser.parse_args(args_in[1:])
        try:
            result = exit_floor(entry_debit=_sol(args.entry_debit_sol), exit_quote=_sol(args.exit_quote_sol),
                                exit_bps=args.exit_bps, fixed_exit=_sol(args.fixed_exit_sol))
        except (ValueError, ArithmeticError):
            print(json.dumps({"version": VERSION, "status": "invalid_input", "funding_allowed": False}))
            return 2
        print(json.dumps(result, indent=2))
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bankroll-sol", required=True)
    parser.add_argument("--reserve-sol", required=True)
    parser.add_argument("--daily-loss-stop-sol", required=True)
    parser.add_argument("--ticket-sol", nargs="+", default=["0.3", "0.5", "1"])
    parser.add_argument("--round-trips", type=int, default=100)
    args = parser.parse_args(args_in)
    try:
        if not 1 <= len(args.ticket_sol) <= 20:
            raise ValueError("scenario_count")
        scenarios = (Costs("gmgn_handling_only_lower_cost_bound", 100, 100),
                     Costs("declared_325bps_per_leg_sensitivity", 325, 325),
                     Costs("325bps_plus_0.001SOL_each_leg_stress", 325, 325, 1_000_000, 1_000_000))
        rows = [ticket_case(notional=_sol(ticket), bankroll=_sol(args.bankroll_sol),
                            reserve=_sol(args.reserve_sol), daily_loss_stop=_sol(args.daily_loss_stop_sol),
                            costs=costs, round_trips=args.round_trips)
                for ticket in args.ticket_sol for costs in scenarios]
    except (ValueError, ArithmeticError):
        print(json.dumps({"version": VERSION, "status": "invalid_input", "funding_allowed": False}))
        return 2
    print(json.dumps({"version": VERSION, "status": "research_only", "rows": rows}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
