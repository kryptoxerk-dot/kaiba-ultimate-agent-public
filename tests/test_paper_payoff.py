from decimal import Decimal

import pytest

from kaiba.learning.paper_payoff import assess

ROW = {"arm_status": "closed", "reopened": 0, "ticket_sol": 1, "starting_token_fraction": ".5",
       "net_sol": ".13745401424440504", "fee_pct": "3.3803300048091627",
       "inventory_pct": "11.995495185754798", "exit_cost_pct": "1.5104237661234559",
       "tx_sol": ".0012", "rent_sol": "0"}


def test_dump_example_separates_directional_gain_from_fees_and_hold_hurdle():
    out = assess(ROW)
    assert Decimal(out["inventory_change_sol"]) == Decimal("0.11995495185754798")
    assert Decimal(out["net_with_flat_inventory_sol"]) == Decimal("0.017499062386857068")
    assert Decimal(out["static_hold_matching_token_return_pct"]) == Decimal("27.490802848881008")
    assert out["profit_proven"] is False


def test_all_sol_has_no_token_hurdle_and_unknown_or_reopened_inputs_refuse():
    assert assess(ROW | {"starting_token_fraction": 0})["static_hold_matching_token_return_pct"] is None
    with pytest.raises(ValueError, match="requires_closed_non_reopened_arm"):
        assess(ROW | {"reopened": 1})
    with pytest.raises(ValueError, match="paper_components_do_not_reconcile"):
        assess(ROW | {"rent_sol": ".07143744"})
