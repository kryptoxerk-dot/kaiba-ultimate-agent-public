"""kaiba/ingest/wallet_stream.py: trusted Robinhood wallets -> confirmed `swaps` rows.

Every address and hash is synthetic. Receipts follow the shape Alchemy's Robinhood endpoint
returns (``logs``, ``from``, ``status``, ``blockNumber``, ``gasUsed``, ``effectiveGasPrice``).
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from typing import Any

import pytest

from kaiba.core.db import fetch_all
from kaiba.execution import onchain_pool
from kaiba.ingest import alchemy_ws as aw
from kaiba.ingest import robinhood as rh
from kaiba.ingest import runner
from kaiba.ingest import wallet_stream as ws
from tests.test_alchemy_ws import FakeServer, topic, transfer_log, word

W1 = "0x" + "a1" * 20          # tracked
W2 = "0x" + "a2" * 20          # tracked
STRANGER = "0x" + "d4" * 20
ROUTER = "0x" + "9e" * 20
CURVE = "0x" + "c0" * 20
V3_POOL = "0x" + "3f" * 20
TOKEN = "0x" + "e5" * 20       # Pons, ETH-quoted
TOKEN_X = "0x" + "e6" * 20     # Pons, quoted in PAIR_X
TOKEN_U = "0x" + "e7" * 20     # not in the registry
PAIR_X = "0x" + "b7" * 20
PM = rh.POOL_MANAGER
TX = "0x" + "11" * 32
BLOCK = 79_000_000
BLOCK_TS = 1_791_080_000
RECV_MS = BLOCK_TS * 1000 + 700
NOW_MS = BLOCK_TS * 1000 + 1_500
ETH_USD = Decimal("2500")
GAS_USED, GAS_PRICE = 200_000, 20_000_000  # fee 4e12 wei
FEE = GAS_USED * GAS_PRICE
E18 = 10 ** 18


# --------------------------------------------------------------------------------------
# log / receipt builders
# --------------------------------------------------------------------------------------


def tlog(sender: str, receiver: str, amount: int, *, token: str = TOKEN, idx: int = 1, tx: str = TX) -> dict[str, Any]:
    return transfer_log(sender=sender, receiver=receiver, amount=amount, token=token, tx=tx,
                        block=BLOCK, log_index=idx, ts=None)


def curve_log(kind: str, *, trader: str, recipient: str, a0: int, a1: int, curve: str = CURVE,
              idx: int = 2, tx: str = TX) -> dict[str, Any]:
    topic0 = rh.TOPIC_CURVE_BUY if kind == "buy" else rh.TOPIC_CURVE_SELL
    data = "0x" + "".join(format(x, "064x") for x in (a0, a1, a0 // 100, 0))
    return {"address": curve, "topics": [topic0, topic(trader), topic(recipient)], "data": data,
            "blockNumber": hex(BLOCK), "transactionHash": tx, "logIndex": hex(idx)}


def swap_log(address: str, topic0: str, *, idx: int = 3, tx: str = TX) -> dict[str, Any]:
    return {"address": address, "topics": [topic0, "0x" + "ab" * 32, topic(ROUTER)],
            "data": word(1) + word(2)[2:], "blockNumber": hex(BLOCK), "transactionHash": tx,
            "logIndex": hex(idx)}


def receipt(logs: list[dict[str, Any]], *, sender: str = W1, tx: str = TX, status: int = 1) -> dict[str, Any]:
    return {"transactionHash": tx, "blockNumber": hex(BLOCK), "from": sender, "to": ROUTER,
            "status": hex(status), "gasUsed": hex(GAS_USED), "effectiveGasPrice": hex(GAS_PRICE),
            "logs": logs}


def tx_obj(h: str, *, sender: str, to: str = ROUTER, value: int = 0, data: str = "0xdeadbeef") -> dict[str, Any]:
    return {"hash": h, "from": sender, "to": to, "value": hex(value), "input": data}


class FakeRpc:
    """Answers the reads the stream makes; records every method it was asked for."""

    def __init__(self) -> None:
        self.receipts: dict[str, Any] = {}
        self.balances: dict[tuple[str, int], int] = {}
        self.blocks: dict[int, list[dict[str, Any]]] = {}
        self.decimals: dict[str, int | None] = {TOKEN: 18, TOKEN_X: 18, TOKEN_U: 18}
        self.logs: list[dict[str, Any]] = []
        self.head = BLOCK + 10
        self.calls: list[str] = []

    async def __call__(self, calls: list[tuple[str, list[Any]]]) -> list[Any]:
        out: list[Any] = []
        for method, params in calls:
            self.calls.append(method)
            if method == "eth_getTransactionReceipt":
                out.append(self.receipts.get(params[0]))
            elif method == "eth_getBalance":
                v = self.balances.get((params[0], int(params[1], 16)))
                out.append(hex(v) if v is not None else None)
            elif method == "eth_getBlockByNumber":
                b = int(params[0], 16)
                out.append({"number": hex(b), "timestamp": hex(BLOCK_TS),
                            "transactions": self.blocks.get(b, []) if params[1] else []})
            elif method == "eth_call":
                d = self.decimals.get(params[0]["to"])
                out.append(None if d is None else "0x" + format(d, "064x"))
            elif method == "eth_getLogs":
                out.append(list(self.logs))
            elif method == "eth_blockNumber":
                out.append(hex(self.head))
            else:
                out.append(None)
        return out


def register(conn) -> None:
    for token, pair, native in ((TOKEN, rh.ZERO_ADDRESS, True), (TOKEN_X, PAIR_X, False)):
        conn.execute(
            "INSERT OR REPLACE INTO tokens (chain, address, decimals, launchpad, pool, first_seen_ms, meta_json) "
            "VALUES ('robinhood', ?, NULL, 'pons', ?, 0, ?)",
            (token, CURVE, json.dumps({"curve": CURVE, "pair_token": pair, "quote_is_native": native})),
        )


def engine(conn, rpc: FakeRpc, *, eth_usd: Decimal | None = ETH_USD) -> ws.WalletStream:
    async def no_sleep(_: float) -> None:
        return None

    e = ws.WalletStream(rpc, reader=conn, writer=conn, eth_usd=lambda: eth_usd,
                        clock_ms=lambda: NOW_MS, sleep=no_sleep)
    e.wallets = frozenset({W1, W2})
    return e


def trigger(wallet: str = W1, tx: str = TX, *, backfilled: bool = False) -> aw.WalletTransfer:
    return aw.WalletTransfer(chain="robinhood", wallet=wallet, token=TOKEN, side="buy", kind="transfer",
                             amount_atoms=1, counterparty=ROUTER, tx=tx, log_index=1, block_number=BLOCK,
                             block_ts_ms=BLOCK_TS * 1000, recv_ms=RECV_MS,
                             latency_ms=RECV_MS - BLOCK_TS * 1000, backfilled=backfilled)


def process(e: ws.WalletStream, tx: str = TX, trig: aw.WalletTransfer | None = None) -> list[ws.Outcome]:
    return asyncio.run(e.process_tx(tx, trig if trig is not None else trigger(tx=tx)))


def rows(conn) -> list[dict[str, Any]]:
    return fetch_all(conn, "SELECT * FROM swaps WHERE source = ? ORDER BY id", (ws.SOURCE,))


def native_buy_setup(rpc: FakeRpc, *, paid: int, wallet: str = W1, extra_txs: list[dict[str, Any]] | None = None,
                     extra_fee: int = 0, delta_noise: int = 0) -> None:
    before = 10 * E18
    rpc.balances[(wallet, BLOCK - 1)] = before
    rpc.balances[(wallet, BLOCK)] = before - paid - FEE - extra_fee + delta_noise
    rpc.blocks[BLOCK] = [tx_obj(TX, sender=wallet, value=paid), *(extra_txs or [])]


# --------------------------------------------------------------------------------------
# the trades
# --------------------------------------------------------------------------------------


def test_pons_curve_buy_through_a_router_matches_the_pons_pollers_own_units(tmp_db):
    """Same CurveBuy, two writers: the stream's row and robinhood.parse_curve_trade must agree
    to the digit on amount_native, usd_value and price_usd."""
    register(tmp_db)
    n, q = 4_000_000 * E18, 49_500_000_000_000_000
    buy = curve_log("buy", trader=ROUTER, recipient=ROUTER, a0=q, a1=n)
    rpc = FakeRpc()
    rpc.receipts[TX] = receipt([tlog(CURVE, ROUTER, n, idx=1), buy, tlog(ROUTER, W1, n, idx=3)])
    (out,) = process(engine(tmp_db, rpc))
    assert out.status == "written" and out.quote_basis == ws.QB_CURVE and out.venue_program == "pons-curve"
    (row,) = rows(tmp_db)
    meta = rh.CurveMeta(token=TOKEN, curve=CURVE, deployer=STRANGER, pair_token=rh.ZERO_ADDRESS,
                        launch_config_id=0, graduation_threshold=0, launched_block=BLOCK)
    pons = rh.parse_curve_trade(buy, meta=meta, ts_ms=BLOCK_TS * 1000, eth_usd=ETH_USD)
    assert row["amount_native"] == pons["amount_native"] == str(q)
    assert row["usd_value"] == pons["usd_value"]
    assert row["price_usd"] == pons["price_usd"]
    assert row["amount_token"] == pons["amount_token"] == str(n)
    assert (row["chain"], row["wallet"], row["token"], row["side"], row["program"]) == \
        ("robinhood", W1, TOKEN, "buy", "pons-curve")
    assert row["slot"] == BLOCK and row["ts_ms"] == BLOCK_TS * 1000 and row["block_index"] == 3  # the wallet's own leg
    assert row["amount_quote"] is None and row["quote_mint"] is None
    assert "eth_getBalance" not in rpc.calls  # the curve's own event decided it
    ev = fetch_all(tmp_db, "SELECT kind, subject, payload, dedupe_key FROM events WHERE kind = 'wallet.trade'")
    assert len(ev) == 1 and ev[0]["subject"] == W1
    payload = json.loads(ev[0]["payload"])
    assert payload["source"] == ws.SOURCE and payload["quote_basis"] == ws.QB_CURVE
    assert payload["recv_latency_ms"] == 700 and payload["write_latency_ms"] == 1500
    assert "cohort" not in payload and "tags" not in payload  # the feed claims nothing about trust


def test_native_eth_buy_on_v4_is_measured_from_the_balance(tmp_db):
    n, paid = 10 ** 21, 5 * 10 ** 16
    rpc = FakeRpc()
    rpc.receipts[TX] = receipt([tlog(PM, ROUTER, n, token=TOKEN_U, idx=1),
                                swap_log(PM, ws.TOPIC_V4_SWAP, idx=2),
                                tlog(ROUTER, W1, n, token=TOKEN_U, idx=3)])
    native_buy_setup(rpc, paid=paid)
    (out,) = process(engine(tmp_db, rpc))
    assert out.status == "written" and out.quote_basis == ws.QB_NATIVE and out.venue_program == "uniswap-v4"
    (row,) = rows(tmp_db)
    assert row["amount_native"] == str(paid)
    assert Decimal(row["usd_value"]) == Decimal(paid) / Decimal(E18) * ETH_USD
    assert Decimal(row["price_usd"]) == Decimal(row["usd_value"]) / (Decimal(n) / Decimal(E18))


def test_a_transfer_nothing_corroborates_is_never_written(tmp_db):
    """A gift from an EOA: no swap event, no quote leg, the wallet did not send the tx."""
    rpc = FakeRpc()
    rpc.receipts[TX] = receipt([tlog(STRANGER, W1, 10 ** 20)], sender=STRANGER)
    (out,) = process(engine(tmp_db, rpc))
    assert out.status == "unconfirmed"
    assert rows(tmp_db) == []
    assert fetch_all(tmp_db, "SELECT 1 FROM events WHERE kind = 'wallet.trade'") == []


def test_a_wallet_routing_tokens_through_itself_writes_nothing(tmp_db):
    rpc = FakeRpc()
    rpc.receipts[TX] = receipt([tlog(PM, W1, 10 ** 20, idx=1), swap_log(PM, ws.TOPIC_V4_SWAP, idx=2),
                                tlog(W1, STRANGER, 10 ** 20, idx=3)])
    assert process(engine(tmp_db, rpc)) == []
    assert rows(tmp_db) == []


def test_weth_leg_is_native_wei(tmp_db):
    n, paid = 10 ** 21, 3 * 10 ** 16
    rpc = FakeRpc()
    rpc.receipts[TX] = receipt([tlog(W1, V3_POOL, paid, token=ws.WETH, idx=1),
                                swap_log(V3_POOL, ws.TOPIC_V3_SWAP, idx=2),
                                tlog(V3_POOL, W1, n, token=TOKEN_U, idx=3)])
    (out,) = process(engine(tmp_db, rpc))
    assert out.quote_basis == ws.QB_WETH and out.venue_program == "uniswap-v3"
    (row,) = rows(tmp_db)
    assert row["amount_native"] == str(paid) and row["usd_value"] is not None
    assert "eth_getBalance" not in rpc.calls


def test_usdg_leg_is_recorded_in_its_own_units_and_left_unpriced(tmp_db):
    """A non-ETH quote is base units in amount_quote with its mint; no wei, no USD invented."""
    rpc = FakeRpc()
    rpc.receipts[TX] = receipt([tlog(W1, PM, 250_000_000, token=ws.USDG, idx=1),
                                swap_log(PM, ws.TOPIC_V4_SWAP, idx=2),
                                tlog(PM, W1, 10 ** 21, token=TOKEN_U, idx=3)])
    (out,) = process(engine(tmp_db, rpc))
    assert out.quote_basis == ws.QB_QUOTE
    (row,) = rows(tmp_db)
    assert row["amount_quote"] == "250000000" and row["quote_mint"] == ws.USDG
    assert row["amount_native"] is None and row["usd_value"] is None and row["price_usd"] is None


def test_non_native_curve_token_never_gets_wei_in_amount_native(tmp_db):
    """The Pons poller writes PAIR-token atoms into amount_native for these tokens; the
    stream must not put wei beside them."""
    register(tmp_db)
    n, q = 10 ** 21, 7 * 10 ** 20
    rpc = FakeRpc()
    rpc.receipts[TX] = receipt([tlog(CURVE, W1, n, token=TOKEN_X, idx=1),
                                curve_log("buy", trader=W1, recipient=W1, a0=q, a1=n, idx=2)])
    (out,) = process(engine(tmp_db, rpc))
    (row,) = rows(tmp_db)
    assert out.quote_basis == ws.QB_CURVE
    assert row["amount_native"] is None and row["amount_quote"] == str(q) and row["quote_mint"] == PAIR_X
    assert row["usd_value"] is None and row["price_usd"] is None


def test_measured_eth_on_a_non_native_curve_token_moves_to_amount_quote(tmp_db):
    register(tmp_db)
    n, paid = 10 ** 21, 4 * 10 ** 16
    rpc = FakeRpc()
    rpc.receipts[TX] = receipt([tlog(PM, W1, n, token=TOKEN_X, idx=1), swap_log(PM, ws.TOPIC_V4_SWAP, idx=2)])
    native_buy_setup(rpc, paid=paid)
    process(engine(tmp_db, rpc))
    (row,) = rows(tmp_db)
    assert row["amount_native"] is None
    assert row["amount_quote"] == str(paid) and row["quote_mint"] == rh.ZERO_ADDRESS
    assert Decimal(row["usd_value"]) == Decimal(paid) / Decimal(E18) * ETH_USD  # real dollars


def test_unknown_decimals_leave_price_null_and_unknown_eth_leaves_usd_null(tmp_db):
    n, paid = 10 ** 21, 5 * 10 ** 16
    rpc = FakeRpc()
    rpc.decimals[TOKEN_U] = None
    rpc.receipts[TX] = receipt([tlog(PM, W1, n, token=TOKEN_U), swap_log(PM, ws.TOPIC_V4_SWAP)])
    native_buy_setup(rpc, paid=paid)
    process(engine(tmp_db, rpc))
    (row,) = rows(tmp_db)
    assert row["usd_value"] is not None and row["price_usd"] is None

    tx2 = "0x" + "22" * 32
    rpc2 = FakeRpc()
    rpc2.receipts[tx2] = receipt([tlog(PM, W2, n, token=TOKEN_U, tx=tx2), swap_log(PM, ws.TOPIC_V4_SWAP, tx=tx2)],
                                 sender=W2, tx=tx2)
    native_buy_setup(rpc2, paid=paid, wallet=W2)
    rpc2.blocks[BLOCK] = [tx_obj(tx2, sender=W2, value=paid)]
    process(engine(tmp_db, rpc2, eth_usd=None), tx2, trigger(W2, tx2))
    row2 = rows(tmp_db)[-1]
    assert row2["amount_native"] == str(paid)
    assert row2["usd_value"] is None and row2["price_usd"] is None


def test_a_sell_after_an_approve_in_the_same_block_adds_back_both_fees(tmp_db):
    n, proceeds = 10 ** 21, 8 * 10 ** 16
    approve_hash, approve_fee = "0x" + "aa" * 32, 3 * 10 ** 12
    rpc = FakeRpc()
    rpc.receipts[TX] = receipt([tlog(W1, PM, n, token=TOKEN_U), swap_log(PM, ws.TOPIC_V4_SWAP)])
    rpc.receipts[approve_hash] = {"gasUsed": hex(approve_fee), "effectiveGasPrice": hex(1), "logs": []}
    before = 10 * E18
    rpc.balances[(W1, BLOCK - 1)] = before
    rpc.balances[(W1, BLOCK)] = before + proceeds - FEE - approve_fee
    rpc.blocks[BLOCK] = [tx_obj(approve_hash, sender=W1, to=TOKEN_U, data=ws.SELECTOR_APPROVE + "00" * 64),
                         tx_obj(TX, sender=W1)]
    (out,) = process(engine(tmp_db, rpc))
    assert out.side == "sell" and out.quote_basis == ws.QB_NATIVE
    assert rows(tmp_db)[0]["amount_native"] == str(proceeds)


@pytest.mark.parametrize("case", ["other_tx_in_block", "eth_in_same_block", "wrong_direction", "not_sender"])
def test_an_ambiguous_balance_change_is_null_not_a_guess(tmp_db, case):
    """The venue still confirms the trade, so the row is written -- unpriced."""
    n, paid = 10 ** 21, 5 * 10 ** 16
    rpc = FakeRpc()
    sender = STRANGER if case == "not_sender" else W1
    rpc.receipts[TX] = receipt([tlog(PM, W1, n, token=TOKEN_U), swap_log(PM, ws.TOPIC_V4_SWAP)], sender=sender)
    if case == "other_tx_in_block":
        native_buy_setup(rpc, paid=paid, extra_txs=[tx_obj("0x" + "bb" * 32, sender=W1, value=1)])
    elif case == "eth_in_same_block":
        native_buy_setup(rpc, paid=paid, extra_txs=[tx_obj("0x" + "cc" * 32, sender=STRANGER, to=W1, value=E18)])
    elif case == "wrong_direction":
        native_buy_setup(rpc, paid=paid, delta_noise=2 * paid + FEE)  # balance went UP on a buy
    else:
        native_buy_setup(rpc, paid=paid)
    (out,) = process(engine(tmp_db, rpc))
    assert out.status == "written" and out.quote_basis is None
    if case != "not_sender":
        assert out.native_reason == case
    (row,) = rows(tmp_db)
    assert row["amount_native"] is None and row["usd_value"] is None and row["price_usd"] is None


def test_native_delta_refuses_a_transaction_the_wallet_did_not_send():
    """Defence in depth: process_tx only asks for the sender, and native_delta refuses
    anyone else itself -- a relayer's or bundler's transaction moves the wallet's ETH in
    ways a whole-block balance cannot separate."""
    block = {"transactions": [tx_obj(TX, sender=STRANGER, value=E18)]}
    got = ws.native_delta(W1, TX, balance_before=hex(10 * E18), balance_after=hex(9 * E18),
                          block=block, receipts={TX: receipt([])})
    assert got == (None, "not_sender")
    mine = {"transactions": [tx_obj(TX, sender=W1, value=E18)]}
    assert ws.native_delta(W1, TX, balance_before=hex(10 * E18), balance_after=hex(9 * E18 - FEE),
                           block=mine, receipts={TX: receipt([])}) == (-E18, "ok")


def test_two_tokens_bought_in_one_tx_share_no_attributable_eth(tmp_db):
    rpc = FakeRpc()
    rpc.receipts[TX] = receipt([tlog(PM, W1, 10 ** 21, token=TOKEN_U, idx=1),
                                tlog(PM, W1, 10 ** 20, token=TOKEN, idx=2),
                                swap_log(PM, ws.TOPIC_V4_SWAP, idx=3)])
    native_buy_setup(rpc, paid=10 ** 17)
    outs = process(engine(tmp_db, rpc))
    assert len(outs) == 2 and all(o.status == "written" for o in outs)
    assert "eth_getBalance" not in rpc.calls
    assert all(r["amount_native"] is None and r["usd_value"] is None for r in rows(tmp_db))


# --------------------------------------------------------------------------------------
# dedupe
# --------------------------------------------------------------------------------------


def _gmgn_row(conn, *, tx: str, wallet: str = W1, token: str = TOKEN_U, ts_ms: int = BLOCK_TS * 1000 + 9_000) -> None:
    conn.execute(
        "INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, amount_token, usd_value, source) "
        "VALUES ('robinhood', ?, ?, ?, ?, 'buy', '1000.5', '125.0', 'gmgn:smartmoney')",
        (tx, ts_ms, wallet, token),
    )


def _v4_buy(rpc: FakeRpc, tx: str = TX) -> None:
    rpc.receipts[tx] = receipt([tlog(PM, W1, 10 ** 21, token=TOKEN_U, tx=tx), swap_log(PM, ws.TOPIC_V4_SWAP, tx=tx)],
                               tx=tx)
    native_buy_setup(rpc, paid=5 * 10 ** 16)
    rpc.blocks[BLOCK] = [tx_obj(tx, sender=W1, value=5 * 10 ** 16)]


def test_a_trade_another_feed_already_wrote_is_not_written_again(tmp_db):
    hexy = "0x" + "ab" * 32
    _gmgn_row(tmp_db, tx="0x" + "AB" * 32)  # same hash in upper case, other units
    rpc = FakeRpc()
    _v4_buy(rpc, hexy)
    (out,) = process(engine(tmp_db, rpc), hexy)
    assert out.status == "matched_existing" and out.matched_sources == ("gmgn:smartmoney",)
    assert rows(tmp_db) == []


def test_dedupe_is_per_tx_wallet_token_not_per_wallet(tmp_db):
    """Positive control: the same wallet's OTHER trade on the same token is not a match."""
    _gmgn_row(tmp_db, tx="0x" + "77" * 32)
    rpc = FakeRpc()
    _v4_buy(rpc)
    (out,) = process(engine(tmp_db, rpc))
    assert out.status == "written" and len(rows(tmp_db)) == 1


def test_replaying_our_own_trade_writes_one_row_and_one_event(tmp_db):
    rpc = FakeRpc()
    _v4_buy(rpc)
    e = engine(tmp_db, rpc)
    assert process(e)[0].status == "written"
    assert process(e)[0].status == "duplicate"
    assert len(rows(tmp_db)) == 1
    assert len(fetch_all(tmp_db, "SELECT 1 FROM events WHERE kind = 'wallet.trade'")) == 1


# --------------------------------------------------------------------------------------
# pure pieces
# --------------------------------------------------------------------------------------


def test_venue_walks_through_routers_but_not_past_its_hop_limit_or_to_a_mint():
    logs = [tlog(PM, "0x" + "01" * 20, 5), tlog("0x" + "01" * 20, "0x" + "02" * 20, 5),
            tlog("0x" + "02" * 20, W1, 5), swap_log(PM, ws.TOPIC_V4_SWAP)]
    em = ws.swap_emitters(logs)
    assert ws.venue_of(logs, TOKEN, W1, "buy", em) == ("uniswap-v4", PM)
    assert ws.venue_of(logs, TOKEN, W1, "buy", em, static={}, max_hops=2) == (None, None)
    minted = [tlog(rh.ZERO_ADDRESS, W1, 5)]
    assert ws.venue_of(minted, TOKEN, W1, "buy", {rh.ZERO_ADDRESS: "x"}) == (None, None)
    sells = [tlog(W1, CURVE, 5), curve_log("sell", trader=W1, recipient=W1, a0=5, a1=9)]
    assert ws.venue_of(sells, TOKEN, W1, "sell", ws.swap_emitters(sells)) == ("pons-curve", CURVE)


def test_curve_quote_needs_exactly_one_event_with_the_wallets_token_amount():
    one = [curve_log("buy", trader=W1, recipient=W1, a0=10, a1=99)]
    assert ws.curve_quote(one, CURVE, "buy", 99) == 10
    assert ws.curve_quote(one, CURVE, "buy", 98) is None
    two = one + [curve_log("buy", trader=W2, recipient=W2, a0=11, a1=99, idx=5)]
    assert ws.curve_quote(two, CURVE, "buy", 99) is None
    assert ws.curve_quote([curve_log("sell", trader=W1, recipient=W1, a0=99, a1=7)], CURVE, "sell", 99) == 7


def test_the_tracked_set_is_the_databases_robinhood_trusted_copy_minus_routers(tmp_db):
    for addr, chain, cohort in ((W1, "robinhood", "trusted_copy"), (W2, "robinhood", "trusted_copy"),
                                (STRANGER, "bsc", "trusted_copy"), (ROUTER, "robinhood", "research")):
        tmp_db.execute("INSERT INTO wallets (chain, address, cohort, first_seen_ms, last_seen_ms) VALUES (?,?,?,0,0)",
                       (chain, addr, cohort))
    routed: dict[str, dict[str, bool]] = {
        W1: {f"t{i}": True for i in range(25)},
        W2: {**{f"t{i}": True for i in range(15)}, **{f"u{i}": False for i in range(10)}},  # 60%
    }
    routers = ws.classify_routers(routed)
    assert routers == frozenset({W1})
    assert ws.classify_routers({W1: {f"t{i}": True for i in range(5)}}) == frozenset()  # too few
    assert ws.cohort_wallets(tmp_db) == frozenset({W1, W2})  # bsc row and research row ignored
    assert ws.tracked_set(tmp_db) == (W1, W2)
    assert set(ws.tracked_set(tmp_db, routers=routers)) == {W2}
    assert ws.excluded_trusted(tmp_db, routers) == (W1,)
    # a router that is NOT a trusted wallet is not "excluded": it was never in the set
    assert ws.excluded_trusted(tmp_db, {ROUTER}) == ()
    ev = ws.router_evidence(routed)
    assert ev[W1] == {"txs": 25, "zero_net": 25, "zero_net_share": 1.0}
    assert ev[W2]["zero_net_share"] == 0.6


def test_no_wallet_address_is_written_in_the_stream_source():
    """The owner's wallet list is a private choice; this source is exported publicly. The
    only 20-byte hex literals allowed are the two measured quote-asset contracts."""
    import re
    from pathlib import Path

    text = Path(ws.__file__).read_text(encoding="utf-8")
    literals = {m.lower() for m in re.findall(r"0x[0-9a-fA-F]{40}(?![0-9a-fA-F])", text)}
    assert literals == {ws.WETH, ws.USDG}
    assert not hasattr(ws, "OWNER_PINNED")


def test_routed_by_wallet_counts_a_wrap_and_pay_buy_as_a_trade():
    legs = []
    legs += aw.decode_transfer(tlog(rh.ZERO_ADDRESS, W1, 5, token=ws.WETH, idx=1), wallets={W1}, recv_ms=0,
                               quote_assets=ws.QUOTE_ASSETS)
    legs += aw.decode_transfer(tlog(W1, PM, 5, token=ws.WETH, idx=2), wallets={W1}, recv_ms=0,
                               quote_assets=ws.QUOTE_ASSETS)
    legs += aw.decode_transfer(tlog(PM, W1, 9, idx=3), wallets={W1}, recv_ms=0, quote_assets=ws.QUOTE_ASSETS)
    assert ws.routed_by_wallet(legs) == {W1: {TX: False}}
    passthrough = aw.decode_transfer(tlog(PM, W2, 9, idx=1), wallets={W2}, recv_ms=0) + \
        aw.decode_transfer(tlog(W2, STRANGER, 9, idx=2), wallets={W2}, recv_ms=0)
    assert ws.routed_by_wallet(passthrough) == {W2: {TX: True}}


def test_seed_routers_reads_the_lookback_logs(tmp_db):
    rpc = FakeRpc()
    rpc.logs = [entry for i in range(25) for entry in (tlog(PM, W2, 7, tx=f"0x{i:064x}", idx=1),
                                               tlog(W2, STRANGER, 7, tx=f"0x{i:064x}", idx=2))]
    e = engine(tmp_db, rpc)
    got = asyncio.run(e.seed_routers([W1, W2], head=BLOCK, lookback_blocks=150_000))
    assert got == frozenset({W2})
    assert rpc.calls.count("eth_getLogs") == 2 * 2  # 2 chunks x 2 directions


def test_price_fields_are_none_when_any_input_is_unknown():
    assert ws.price_fields(native_wei=None, net_atoms=1, decimals=18, eth_usd=ETH_USD) == (None, None)
    assert ws.price_fields(native_wei=E18, net_atoms=1, decimals=18, eth_usd=None) == (None, None)
    usd, price = ws.price_fields(native_wei=E18, net_atoms=2 * E18, decimals=18, eth_usd=ETH_USD)
    assert Decimal(usd) == ETH_USD and Decimal(price) == ETH_USD / 2
    assert ws.price_fields(native_wei=E18, net_atoms=2 * 10 ** 6, decimals=6, eth_usd=ETH_USD)[1] == \
        ws.price_fields(native_wei=E18, net_atoms=2 * E18, decimals=18, eth_usd=ETH_USD)[1]
    assert ws.price_fields(native_wei=E18, net_atoms=5, decimals=None, eth_usd=ETH_USD)[1] is None


def test_constants_agree_with_their_other_homes():
    assert ws.WETH == onchain_pool.ROBINHOOD_WETH and ws.USDG == onchain_pool.ROBINHOOD_USDG
    assert ws.STATIC_VENUES == {onchain_pool.POOL_MANAGER: "uniswap-v4"}
    assert ws.SWAP_PROGRAMS[rh.TOPIC_CURVE_BUY] == "pons-curve"
    assert not ws.SOURCE.startswith("gmgn:")  # base units, not the grader's UI-unit prefix


def test_the_listener_is_registered_with_the_ingest_runner():
    assert "rh_wallets" in runner.REGISTRY
    assert "rh_wallets" not in runner.DEFAULT_FEEDS


def test_http_rpc_errors_never_carry_the_key():
    class Boom:
        async def post(self, url: str, json: Any) -> Any:
            raise ConnectionError(f"cannot reach {url}")

    rpc = ws.HttpRpc("https://rh.example/v2/SECRETKEY123", client=Boom())
    with pytest.raises(ws.RpcFailure) as err:
        asyncio.run(rpc.batch([("eth_blockNumber", [])]))
    assert "SECRETKEY123" not in str(err.value) and "/v2/***" in str(err.value)


# --------------------------------------------------------------------------------------
# the listener, end to end against a scripted socket
# --------------------------------------------------------------------------------------


def test_from_block_backfills_on_the_first_connection():
    missed = tlog(PM, W1, 3, tx="0x" + "03" * 32)
    missed["blockNumber"] = hex(150)
    server = FakeServer([{"head": 160, "backfill": {"in": [missed]}}])

    async def go() -> list[aw.WalletTransfer]:
        stop = asyncio.Event()
        out: list[aw.WalletTransfer] = []
        async for rec in aw.stream("https://n.example/v2/K", [W1], stop=stop, connect=server.connect,
                                   poll_s=0.01, max_attempts=1, from_block=140):
            out.append(rec)
            stop.set()
        return out

    out = asyncio.run(asyncio.wait_for(go(), timeout=5))
    assert [(r.amount_atoms, r.backfilled) for r in out] == [(3, True)]
    assert server.get_logs[0]["fromBlock"] == hex(140) and server.get_logs[0]["toBlock"] == hex(160)


@pytest.mark.parametrize("anchor", [True, False], ids=["anchored", "unanchored-control"])
def test_a_quiet_reconnect_backfills_its_gap_only_when_anchored(monkeypatch, anchor):
    """MEASURED on the box: 15 quiet wallets hit the 600 s no-frame timeout before any record,
    so the reconnect had no backfill point. The anchor (head read after subscribe) gives it one."""
    async def no_wait(stop: asyncio.Event, seconds: float) -> None:
        return None

    monkeypatch.setattr(aw, "_wait", no_wait)
    missed = tlog(PM, W1, 4, tx="0x" + "04" * 32)
    missed["blockNumber"] = hex(205)
    server = FakeServer([{"head": 200}, {"head": 210, "backfill": {"in": [missed]}}])
    out: list[aw.WalletTransfer] = []

    async def go() -> None:
        stop = asyncio.Event()
        async for rec in aw.stream("https://n.example/v2/K", [W1], stop=stop, connect=server.connect,
                                   poll_s=0.01, idle_timeout_s=0.05, max_attempts=3,
                                   anchor_on_subscribe=anchor):
            out.append(rec)
            stop.set()

    try:
        asyncio.run(asyncio.wait_for(go(), timeout=1.5))
    except TimeoutError:
        pass
    if anchor:
        assert [(r.amount_atoms, r.backfilled) for r in out] == [(4, True)]
        assert server.get_logs[0]["fromBlock"] == hex(200) and server.get_logs[0]["toBlock"] == hex(210)
    else:
        assert out == [] and server.get_logs == []


def _trusted(conn, *addresses: str, chain: str = "robinhood", cohort: str = "trusted_copy") -> None:
    for a in addresses:
        conn.execute("INSERT OR REPLACE INTO wallets (chain, address, cohort, first_seen_ms, last_seen_ms) "
                     "VALUES (?,?,?,0,0)", (chain, a, cohort))


def _run_with_fake_stream(conn, monkeypatch, rpc: FakeRpc) -> dict[str, Any]:
    """ws.run against a stub socket that records how it was asked and returns at once."""
    seen: dict[str, Any] = {"calls": 0}

    async def fake_stream(url: str, wallets: Any, **kw: Any):
        seen["calls"] += 1
        seen.update(kw, wallets=tuple(wallets))
        kw["stop"].set()
        return
        yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(ws.aws, "stream", fake_stream)

    class Batch:
        batch = staticmethod(rpc)

    stop = asyncio.Event()

    async def go() -> None:
        async def later() -> None:
            await asyncio.sleep(0.05)
            stop.set()

        asyncio.ensure_future(later())
        await ws.run(stop, url="https://rh.example/v2/K", reader=conn, writer=conn, rpc=Batch(),
                     eth_usd=lambda: ETH_USD, resume=False)

    asyncio.run(asyncio.wait_for(go(), timeout=5))
    return seen


def _system(conn, status: str) -> list[dict[str, Any]]:
    out = [json.loads(r["payload"]) for r in fetch_all(conn, "SELECT payload FROM events WHERE kind = 'system'")]
    return [p for p in out if p.get("status") == status]


def test_a_trusted_wallet_the_router_screen_drops_is_named_not_dropped_silently(tmp_db, monkeypatch):
    _trusted(tmp_db, W1, W2)
    rpc = FakeRpc()
    rpc.logs = [entry for i in range(25) for entry in (tlog(PM, W2, 7, tx=f"0x{i:064x}", idx=1),
                                                       tlog(W2, STRANGER, 7, tx=f"0x{i:064x}", idx=2))]
    seen = _run_with_fake_stream(tmp_db, monkeypatch, rpc)
    assert seen["wallets"] == (W1,)  # W2 is not subscribed ...
    (event,) = _system(tmp_db, "router_excluded")  # ... and that is said, with the evidence
    assert event["wallets"] == [{"address": W2, "txs": 25, "zero_net": 25, "zero_net_share": 1.0}]
    assert event["rule"]["min_txs"] == ws.ROUTER_MIN_TXS
    assert _system(tmp_db, "starting")[0]["routers_excluded"] == [W2]


def test_with_no_trusted_wallet_the_feed_idles_and_says_so(tmp_db, monkeypatch):
    _trusted(tmp_db, W1, chain="bsc")          # not robinhood
    _trusted(tmp_db, W2, cohort="research")    # not trusted
    seen = _run_with_fake_stream(tmp_db, monkeypatch, FakeRpc())
    assert seen["calls"] == 0  # no socket for an empty set (alchemy_ws would refuse it)
    assert _system(tmp_db, "idle")[0]["reason"] == "no trusted_copy robinhood wallet to track"


def test_run_asks_the_socket_for_an_anchor_and_a_long_idle_timeout(tmp_db, monkeypatch):
    _trusted(tmp_db, W1)
    seen = _run_with_fake_stream(tmp_db, monkeypatch, FakeRpc())
    assert seen["anchor_on_subscribe"] is True
    assert seen["idle_timeout_s"] == ws.IDLE_TIMEOUT_S >= 1800
    assert seen["wallets"] == (W1,)


def test_run_writes_a_pushed_trade_and_idles_without_an_alchemy_url(tmp_db, monkeypatch):
    tmp_db.execute("INSERT INTO wallets (chain, address, cohort, first_seen_ms, last_seen_ms) "
                   "VALUES ('robinhood', ?, 'trusted_copy', 0, 0)", (W1,))
    rpc = FakeRpc()
    _v4_buy(rpc)
    push = tlog(PM, W1, 10 ** 21, token=TOKEN_U)
    push["blockTimestamp"] = hex(BLOCK_TS)
    server = FakeServer([{"head": BLOCK, "pushes": [("in", push)]}])
    written: list[ws.Outcome] = []

    class Batch:
        batch = staticmethod(rpc)

        def estimated_cu(self) -> int:
            return 0

    async def go() -> dict[str, Any]:
        stop = asyncio.Event()

        def done(o: ws.Outcome) -> None:
            written.append(o)
            stop.set()

        return await asyncio.wait_for(
            ws.run(stop, url="https://rh.example/v2/SECRETKEY", reader=tmp_db, writer=tmp_db, rpc=Batch(),
                   eth_usd=lambda: ETH_USD, connect=server.connect, resume=False, on_written=done),
            timeout=10)

    summary = asyncio.run(go())
    assert [o.status for o in written] == ["written"]
    assert summary["stats"]["written"] == 1 and summary["stats"]["tracked"] == 1
    assert rows(tmp_db)[0]["wallet"] == W1
    resume = fetch_all(tmp_db, "SELECT value FROM kv WHERE key = ?", (ws.KV_RESUME,))
    assert resume and int(resume[0]["value"]) == BLOCK
    system = json.dumps(fetch_all(tmp_db, "SELECT payload FROM events WHERE kind = 'system'"))
    assert "SECRETKEY" not in system

    monkeypatch.setattr(ws, "alchemy_url", lambda: None)
    assert asyncio.run(ws.run(asyncio.Event(), reader=tmp_db, writer=tmp_db)) == {"idle": "no websocket endpoint"}
