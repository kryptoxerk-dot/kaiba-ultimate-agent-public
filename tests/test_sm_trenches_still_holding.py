"""sm-trenches: a smart wallet that has already sold is not confluence.

BUG (2026-10-05 wallet study): ``_net_buyers`` nets USD, so a wallet that bought $500 and
dumped the whole bag at a loss for $300 stayed a "net buyer" of $200 and counted toward
``min_smart_degen``. The lane now also nets TOKENS per smart wallet, on the tape it holds,
at or before the decision, and counts the wallet only while it has sold less than
``SMART_SOLD_FRAC_MAX`` (0.5) of what it bought.

Units matter: gmgn feeds write ``amount_token`` in UI units, every other source in atoms,
and the same trade can be on the tape twice, once in each. All addresses are synthetic.
"""

from __future__ import annotations

import copy
from decimal import Decimal
from typing import Any

from kaiba.execution import lanes
from tests.test_lanes import NOW, build_ctx

TOKEN = "0x" + "71" * 20
A = "0x" + "a1" * 20
B = "0x" + "b2" * 20
C = "0x" + "c3" * 20
D = "0x" + "d4" * 20
E18 = 10**18


def _wallet(address: str) -> dict[str, Any]:
    return {"address": address, "cohort": "tracked", "tags": ["smart_money"]}


def _row(wallet: str, side: str, age_s: int, amount: str | None, *, usd: str = "500",
         source: str = "gmgn:smartmoney", tx: str | None = None) -> dict[str, Any]:
    return {"wallet": wallet, "side": side, "age_s": age_s, "usd_value": usd,
            "amount_native": "100000000000000000", "price_usd": "0.0001",
            "amount_token": amount, "source": source,
            "tx": tx or f"0x{wallet[2:6]}{side[0]}{age_s:059d}"}


def buy(wallet: str, age_s: int, amount: str | None = "1000", **kw: Any) -> dict[str, Any]:
    return _row(wallet, "buy", age_s, amount, **kw)


def sell(wallet: str, age_s: int, amount: str | None, *, usd: str = "300", **kw: Any) -> dict[str, Any]:
    return _row(wallet, "sell", age_s, amount, usd=usd, **kw)


BASE: dict[str, Any] = {
    "chain": "robinhood",
    "token": TOKEN,
    "wallets": [_wallet(A), _wallet(B), _wallet(C), _wallet(D)],
    "buys": [],
    "dossier": {"built_age_s": 5, "grade": "B", "price_usd": "0.0001",
                "liquidity_usd": "90000", "rug_ratio": "0.05"},
    "token_meta": {"symbol": "HOLD", "decimals": 18, "created_age_s": 3600, "launchpad": "pons"},
}

THREE = (buy(A, 300), buy(B, 250), buy(C, 200))


def fixture(*rows: dict[str, Any], decimals: int | None = 18) -> dict[str, Any]:
    fx = copy.deepcopy(BASE)
    fx["buys"] = list(rows)
    fx["token_meta"]["decimals"] = decimals
    return fx


def trenches(conn, fx: dict[str, Any], **kw: Any):
    return lanes.sm_trenches(build_ctx(conn, fx, **kw))


# ====================================================================== the bug


def test_a_wallet_that_sold_out_at_a_loss_is_not_counted(tmp_db):
    fx = fixture(*THREE, sell(C, 100, "1000", usd="300"))
    # The bug, reproduced: USD netting still calls C a buyer ($500 in, $300 out).
    ctx = build_ctx(tmp_db, fx)
    nb = lanes._net_buyers(ctx.recent_buys, since_ms=NOW - 1800_000, until_ms=NOW,
                           min_buy_usd=Decimal(0))
    assert C in nb
    # The fix: C sold 100% of its tokens, so only two smart wallets are still holding.
    assert lanes.sm_trenches(ctx) is None
    # Positive control: the same tape without the sell fires, with C counted.
    signal = trenches(tmp_db, fixture(*THREE))
    assert signal is not None and C in signal.wallets and signal.payload["smart_wallets"] == 3


def test_a_sold_wallet_is_dropped_but_the_rest_still_fire(tmp_db):
    fx = fixture(*THREE, buy(D, 150), sell(D, 60, "1000"))
    signal = trenches(tmp_db, fx)
    assert signal is not None
    assert D not in signal.wallets
    p = signal.payload
    assert p["smart_wallets"] == p["smart_wallets_still_holding"] == 3
    assert p["smart_wallets_seen"] == 4
    assert p["smart_wallets_sold"] == 1
    assert p["smart_wallets_sold_unknown"] == 0
    assert p["smart_wallets_trimmed"] == 0
    assert p["smart_sold_frac_max"] == "0.5"


# ====================================================================== threshold


def test_partial_sells_follow_the_threshold(tmp_db):
    # 40% sold: still holding, counted, recorded as trimmed
    s = trenches(tmp_db, fixture(*THREE, sell(C, 100, "400", usd="200")))
    assert s is not None and C in s.wallets and s.payload["smart_wallets_trimmed"] == 1
    # exactly 50%: the majority is no longer held -> not counted
    assert trenches(tmp_db, fixture(*THREE, sell(C, 100, "500", usd="250"))) is None
    # 60% across two sells: not counted
    assert trenches(tmp_db, fixture(*THREE, sell(C, 100, "300", usd="150"),
                                    sell(C, 90, "300", usd="150"))) is None


def test_the_threshold_constant_is_one_half():
    assert lanes.SMART_SOLD_FRAC_MAX == Decimal("0.5")


# ====================================================================== point in time


def test_sells_after_the_decision_are_ignored(tmp_db):
    """A sell stamped after now_ms did not exist when the lane decided."""
    late = sell(C, -30, "1000")  # 30 s in the future
    signal = trenches(tmp_db, fixture(*THREE, late))
    assert signal is not None and C in signal.wallets
    assert signal.payload["smart_wallets_sold"] == 0
    # and the same sell 30 s in the past refuses
    assert trenches(tmp_db, fixture(*THREE, sell(C, 30, "1000"))) is None


def test_a_sell_before_the_first_visible_buy_is_not_this_position(tmp_db):
    """It disposes of inventory bought before our tape starts; the position the lane is
    counting is the one opened by the buy that follows."""
    signal = trenches(tmp_db, fixture(sell(C, 600, "5000"), *THREE))
    assert signal is not None and C in signal.wallets


# ====================================================================== units


def test_gmgn_ui_units_and_atoms_are_reconciled_with_decimals(tmp_db):
    """Buy seen by gmgn in UI units, full exit seen by the chain decoder in atoms."""
    full_exit = sell(C, 100, str(1000 * E18), source="robinhood")
    assert trenches(tmp_db, fixture(*THREE, full_exit)) is None
    # without the conversion this would read as a 1e-18 sell; a 30% exit in atoms holds
    s = trenches(tmp_db, fixture(*THREE, sell(C, 100, str(300 * E18), source="robinhood")))
    assert s is not None and C in s.wallets


def test_the_same_trade_from_two_sources_counts_once(tmp_db):
    """C's buy is on the tape twice (gmgn UI + decoder atoms, same tx). Counted twice, a
    600-token sell would look like 30% and C would still count; once, it is 60%."""
    dup = buy(C, 200, str(1000 * E18), source="robinhood", tx=THREE[2]["tx"])
    assert trenches(tmp_db, fixture(*THREE, dup, sell(C, 100, "600"))) is None
    assert trenches(tmp_db, fixture(*THREE, dup, sell(C, 100, "400"))) is not None


def test_decimals_are_read_off_the_tape_when_the_token_row_has_none(tmp_db):
    dup = buy(C, 200, str(1000 * E18), source="robinhood", tx=THREE[2]["tx"])
    exit_atoms = sell(C, 100, str(1000 * E18), source="robinhood")
    assert lanes._decimals_from_tape([THREE[2], dup]) == 18
    assert trenches(tmp_db, fixture(*THREE, dup, exit_atoms, decimals=None)) is None


def test_an_unreadable_sell_size_is_not_shown_holding(tmp_db):
    """Sold something, size unknown: not counted, and recorded rather than hidden."""
    fx = fixture(*THREE, buy(D, 150), sell(D, 60, None))
    signal = trenches(tmp_db, fx)
    assert signal is not None and D not in signal.wallets
    assert signal.payload["smart_wallets_sold_unknown"] == 1
    assert signal.payload["smart_wallets_sold"] == 0


def test_mixed_units_without_decimals_are_unknown_not_guessed():
    rows = [
        {"wallet": A, "side": "buy", "ts_ms": 1, "amount_token": "1000", "source": "gmgn:smartmoney", "tx": "t1"},
        {"wallet": A, "side": "sell", "ts_ms": 2, "amount_token": "5", "source": "pumpfun:trades", "tx": "t2"},
    ]
    st = lanes._holding_status(rows, [A], until_ms=10, decimals=None)
    assert st[A]["status"] == "unknown" and st[A]["sold_frac"] is None
    st = lanes._holding_status(rows, [A], until_ms=10, decimals=0)
    assert st[A]["status"] == "holding" and st[A]["sold_frac"] == Decimal("0.005")


def test_a_wallet_with_no_sell_holds_even_with_unreadable_buy_sizes():
    rows = [{"wallet": A, "side": "buy", "ts_ms": 1, "amount_token": None, "tx": "t1"}]
    assert lanes._holding_status(rows, [A], until_ms=10, decimals=None)[A]["status"] == "holding"
