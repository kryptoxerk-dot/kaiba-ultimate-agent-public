"""Pons quote controls: measured cashflows, minimum allocation and old-formula failure."""
from decimal import Decimal, localcontext

import pytest

from kaiba.core.schemas import Chain
from kaiba.execution import tweet_launch as tl
from kaiba.execution.tweet_launch_policy import (
    RequirementUnavailable,
    quote_five_percent_fee_on_input,
)

SUPPLY = 10**27
PHANTOM = 1_680_000_000_000_000_000


def quote(fee=100):
    return quote_five_percent_fee_on_input(total_supply_atoms=SUPPLY,
        virtual_token_atoms=SUPPLY, virtual_native_atoms=PHANTOM,
        fee_bps=fee, native_decimals=18)


def output(gross):
    net = gross - gross * 100 // 10000
    return SUPPLY * net // (PHANTOM + net)


def test_target_reached_and_one_wei_less_is_insufficient():
    q = quote()
    assert output(q.native_atoms) >= SUPPLY // 20
    assert output(q.native_atoms - 1) < SUPPLY // 20
    # Positive mutation control: the former fee-on-top calculation does NOT reach5%.
    old = (PHANTOM * 10100 + 19 * 10000 - 1) // (19 * 10000)
    assert output(old) < SUPPLY // 20


def test_real_launcher_uses_corrected_quote_and_still_refuses_over_cap():
    q = quote()
    route = tl.ChainRoute(Chain.ROBINHOOD, "pons", Decimal("0.1"))
    amount, share, _ = tl.dev_buy_native(route, 5)
    assert amount == q.native_amount and share == 5
    assert tl.dev_buy_native(tl.ChainRoute(Chain.ROBINHOOD, "pons", Decimal("0.037")), 5) == (
        None, None, "five_percent_exceeds_route_cap")


@pytest.mark.parametrize("gross,fee,tax,actual", [
    (40_000_000_000_000_000, 400_000_000_000_000, 0, 23028611304954640614096301),
    (12_000_000_000_000_000, 120_000_000_000_000, 360_000_000_000_000, 6810442678774120317820658),
    (17_000_000_000_000_000, 170_000_000_000_000, 340_000_000_000_000, 9720069083814228200578842),
])
def test_actual_public_creation_receipts_match_fee_deducted_from_input(gross, fee, tax, actual):
    # Sources: 2034d475..., fdac4529..., 3d139dbd... in the dated Codex research report.
    net = gross - fee - tax
    assert SUPPLY * net // (PHANTOM + net) == actual
    assert SUPPLY * gross // (PHANTOM + gross) != actual


def test_integer_quote_and_decimal_conversion_ignore_ambient_precision():
    expected = quote()
    with localcontext() as context:
        context.prec = 5
        actual = quote()
        assert actual.native_atoms == expected.native_atoms
        assert actual.native_amount == expected.native_amount


@pytest.mark.parametrize("fee", [True, -1, 1.5, 10000, 10001])
def test_invalid_or_fully_consumed_fee_is_refused(fee):
    with pytest.raises(RequirementUnavailable):
        quote(fee)
