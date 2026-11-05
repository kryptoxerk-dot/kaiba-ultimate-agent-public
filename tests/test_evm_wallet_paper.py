from dataclasses import replace
from decimal import Decimal

import pytest

from kaiba.learning.evm_wallet_paper import Swap, paper

MINUTE = 60_000
LEADER = "0x" + "aa" * 20
OTHER = "0x" + "bb" * 20
FIRST = "0x" + "cc" * 20


def tick(minute, tx, *, token=FIRST, wallet=LEADER, market=None, side="buy", price="1"):
    return Swap(minute * MINUTE, market or token, token, wallet, tx, side, Decimal(price))


def test_same_sender_and_transaction_cannot_supply_second_confirmation():
    rows = [tick(1, "start", side="sell"), tick(101, "first"), tick(102, "first", wallet=OTHER),
            tick(103, "repeated"), tick(110, "confirm", wallet=OTHER), tick(111, "buy-second", price="2"),
            tick(133, "sell-first", side="sell", price="3"), tick(141, "sell-second", side="sell", price="3"),
            tick(161, "end", side="sell")]
    report = paper(rows, chain="robinhood", as_of_ms=200 * MINUTE)
    control = report["all_observed_control"]
    assert control["paired_settled"] == 1
    assert control["one_wallet"]["stress_mean_pct"] == 145.45455
    assert control["two_wallets"]["stress_mean_pct"] == 22.72727
    assert report["nominated_paper"]["one_wallet"]["signals"] == 0


def test_different_market_prints_cannot_settle_a_missing_sell():
    rows = [tick(1, "start", side="sell"), tick(101, "first"), tick(102, "entry"),
            tick(132, "wrong-pool", market="different-pool", side="sell", price="99"),
            tick(161, "late", side="sell", price="99")]
    out = paper(rows, chain="elysium", as_of_ms=200 * MINUTE)["all_observed_control"]["one_wallet"]
    assert out["signals"] == 1 and out["no_exit"] == 1 and out["stress_mean_pct"] is None


def test_positive_training_needs_asset_diversity_and_no_future_exit():
    start_end = [tick(1, "start", side="sell"), tick(400, "end", side="sell")]
    rows = start_end.copy()
    for i, minute in enumerate([5, 10, 15, 20, 125, 130, 135, 140]):
        token = "0x" + f"{i + 5:040x}"
        rows.extend([tick(minute, f"trigger{i}", token=token),
                     tick(minute + 1, f"entry{i}", token=token, wallet=OTHER),
                     tick(minute + 31, f"exit{i}", token=token, side="sell", price="2")])
    report = paper(rows, chain="bsc", as_of_ms=500 * MINUTE)
    assert [r["wallet"] for r in report["nominations"]] == [LEADER]
    assert report["nominations"][0]["distinct_tokens"] == 8
    delayed = [replace(r, at_ms=300 * MINUTE) if r.tx == "exit7" else r for r in rows]
    assert not paper(delayed, chain="bsc", as_of_ms=500 * MINUTE)["nominations"]


@pytest.mark.parametrize("change", [{"price": Decimal("NaN")}, {"at_ms": 201 * MINUTE}, {"token": "ticker"}])
def test_invalid_or_future_print_refused(change):
    with pytest.raises(ValueError, match="invalid_paper_swap"):
        paper([replace(tick(1, "bad"), **change)], chain="base", as_of_ms=200 * MINUTE)


def test_empty_chain_is_distinct_from_zero_return():
    out = paper([], chain="stable", as_of_ms=MINUTE)
    assert out["status"] == "no_usable_tape" and out["execution_ready"] is False
