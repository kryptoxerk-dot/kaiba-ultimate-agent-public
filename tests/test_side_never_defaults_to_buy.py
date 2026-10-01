"""An unknown ``side`` is UNKNOWN. It is never the value that authorises spending money.

The copy lane's version of this was fixed on 2026-09-22 -- `trusted_copy` had
``str(row.get("side") or "buy")``, which turned a missing field into the one word that
means somebody paid. The operator's rule is explicit: *"if tracked wallet bought we buy /
if they got transfered in, we dyor"*.

Two more instances of the same spelling survived that fix, in the same file, and both feed
CONVICTION rather than the copy decision:

* ``_net_buyers`` aggregates the recent tape into buy/sell pressure per wallet. An unknown
  side there inflates ``buy_usd``, ``buys`` and ``max_buy_usd``, which is what
  ``confluence-5`` and ``sm-trenches`` read as strength.
* the Pons early-buyer loop attributes supply to early buyers and to the dev on robinhood.
  An unknown side there counts inventory that merely arrived as a purchase.

MEASURED before changing them, 2026-09-22 on the live box: **zero** rows with a null or
empty side across 996,805 swaps in 24 h -- sol 502,381, robinhood 469,196, bsc 25,228.
So this costs nothing today and is defence against a feed that changes shape, which is
exactly when a fail-open default does its damage and exactly when nobody is looking.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from kaiba.execution import lanes

WALLET = "wa11etAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
TOKEN = "CJF7MNqb9xv1XrTs5St1Du5JuQXLsfFBB137vmYRKpnb"


def row(**over) -> dict:
    base = dict(wallet=WALLET, token=TOKEN, side="buy", ts_ms=1_000, usd_value="1000",
                amount_native="1000000000", tx="tx1", price_usd="1.0")
    base.update(over)
    return base


# ------------------------------------------------------------------ _net_buyers


def buyers(rows):
    """`_net_buyers` filters to wallets whose buys outweigh their sells, so a wallet that
    never registered a buy is absent from the result entirely. Absence is therefore the
    correct assertion for "this was not conviction", not a zero."""
    return lanes._net_buyers(rows, since_ms=0, until_ms=10_000, min_buy_usd=Decimal(0))


def test_a_known_buy_still_counts():
    """Without this the tests below could pass by counting nothing at all."""
    got = buyers([row()])
    assert WALLET in got, "a decoded buy must still register as conviction"
    assert Decimal(str(got[WALLET]["buy_usd"])) > 0
    assert int(got[WALLET]["buys"]) == 1


@pytest.mark.parametrize("side", [None, "", "   "])
def test_an_unknown_side_is_not_conviction(side):
    """THE DEFECT. `str(row.get("side") or "buy")` made a missing field mean "somebody
    paid", which is the one reading that authorises spending money."""
    assert WALLET not in buyers([row(side=side)]), (
        f"side={side!r} is UNKNOWN and must not be counted as a buy"
    )


@pytest.mark.parametrize("side", ["transfer_in", "receive", "airdrop", "mint", "in"])
def test_inventory_inflow_is_not_conviction(side):
    """Tokens arriving in a wallet is not evidence anyone bought them.

    These already fall through to the non-buy branch today, so this pins existing
    behaviour rather than changing it -- worth pinning because the operator's rule names
    exactly this case and nothing else asserted it here.
    """
    assert WALLET not in buyers([row(side=side)]), f"side={side!r} must not read as a buy"


def test_a_sell_is_not_conviction():
    assert WALLET not in buyers([row(side="sell")])


def test_a_buy_outweighed_by_a_sell_is_not_conviction():
    """The function's actual contract, pinned so the tests above cannot pass vacuously."""
    rows = [row(usd_value="100"), row(side="sell", usd_value="900", tx="tx2", ts_ms=2_000)]
    assert WALLET not in buyers(rows)


# ------------------------------------------------------------------ the shared helper


@pytest.mark.parametrize("side", [None, "", "  ", "transfer_in", "receive", "sell", "in"])
def test_the_decoded_buy_helper_refuses_everything_that_is_not_a_buy(side):
    assert lanes._is_decoded_buy({"side": side}) is False


def test_the_decoded_buy_helper_accepts_a_plain_buy():
    assert lanes._is_decoded_buy({"side": "buy"}) is True
    assert lanes._is_decoded_buy({"side": "BUY"}) is True


def test_no_fail_open_side_default_remains_in_the_module():
    """A grep-style guard. The spelling is the bug, and it reappeared twice already.

    Deliberately a source check rather than a behavioural one: the two call sites this
    protects are reached only through lane paths with heavy fixtures, and the thing worth
    preventing is the SPELLING being reintroduced anywhere in the file.
    """
    import pathlib

    src = pathlib.Path(lanes.__file__).read_text(encoding="utf-8")
    offenders = [
        line.strip()
        for i, line in enumerate(src.splitlines())
        if 'or "buy"' in line and not line.strip().startswith(("#", "*", '"', "``"))
    ]
    assert offenders == [], (
        "an unknown side must never default to 'buy'; use _is_decoded_buy. Found: "
        + " | ".join(offenders)
    )
