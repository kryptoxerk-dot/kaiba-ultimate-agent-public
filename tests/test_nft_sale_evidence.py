from copy import deepcopy

import pytest

from kaiba.learning import nft_sale_evidence as evidence
from kaiba.providers import seaport_rh as market
from tests.test_nft_mint_study import FILL_LOGS


def word(value):
    return "0x" + (format(value, "064x") if isinstance(value, int) else market.pad_address(value))


def sale_capture():
    logs = [deepcopy(log) for log in FILL_LOGS if log["transactionHash"].startswith("0x26307f62")]
    sales = [market.classify(market.decode_order_fulfilled(log)) for log in logs]
    sale = next(row for kind, row in sales if kind == "offer")
    block_hash = "0x" + "9" * 64
    block = hex(sale.block)
    identity = {"transactionHash": sale.tx, "blockNumber": block, "blockHash": block_hash, "removed": False}
    for log in logs:
        log.update(identity)
    index = max(int(log["logIndex"], 16) for log in logs)
    logs += [
        {
            **identity, "address": sale.collection, "logIndex": hex(index + 1), "data": "0x",
            "topics": [evidence.TRANSFER, word(sale.seller), word(sale.buyer), word(sale.token_id)],
        },
        {
            **identity, "address": market.WETH, "logIndex": hex(index + 2), "data": word(sale.seller_net),
            "topics": [evidence.TRANSFER, word(sale.buyer), word(sale.seller)],
        },
    ]
    tx = {
        "hash": sale.tx, "blockHash": block_hash, "blockNumber": block,
        "to": market.SEAPORT, "from": sale.seller, "value": "0x0", "chainId": hex(market.CHAIN_ID),
    }
    receipt = {**identity, "from": sale.seller, "to": market.SEAPORT, "status": "0x1",
               "gasUsed": hex(284_614), "effectiveGasPrice": hex(35_243_000), "logs": logs}
    calls = [
        ("eth_chainId", [], hex(market.CHAIN_ID)),
        ("eth_getTransactionByHash", [sale.tx], tx),
        ("eth_getTransactionReceipt", [sale.tx], receipt),
        ("eth_getBlockByNumber", ["finalized", False], {"number": hex(sale.block + 10)}),
        ("eth_getBlockByNumber", [block, False], {"number": block, "hash": block_hash}),
    ]
    return {
        "version": evidence.VERSION, "sale_tx": sale.tx, "collection": sale.collection,
        "wallet": sale.seller, "token_id": hex(sale.token_id),
        "batch": {
            "request": [{"jsonrpc": "2.0", "id": i, "method": m, "params": p}
                        for i, (m, p, _) in enumerate(calls, 1)],
            "response": [{"jsonrpc": "2.0", "id": i, "result": r}
                         for i, (_, _, r) in enumerate(calls, 1)],
        },
    }


def test_accepted_bid_proves_seller_receipt_and_gas_without_inventing_mint_cost():
    output = evidence.assess(sale_capture())
    assert output["bid_gross_wei"] == 16_600_000_000_000_000
    assert output["actual_weth_received_wei"] == 15_355_000_000_000_000
    assert output["sale_net_eth_equivalent_wei"] == 15_355_000_000_000_000 - 284_614 * 35_243_000
    assert output["scoped_round_trip_net_eth_equivalent_wei"] is None
    assert output["live_offer_verified"] is False


def test_mismatched_nft_payment_and_finalized_block_fail_closed():
    wrong = sale_capture()
    wrong["token_id"] = hex(int(wrong["token_id"], 16) + 1)
    with pytest.raises(ValueError, match="exact_accepted_bid_unavailable"):
        evidence.assess(wrong)
    wrong = sale_capture()
    wrong["batch"]["response"][2]["result"]["logs"][-1]["data"] = word(1)
    with pytest.raises(ValueError, match="sale_payout_or_nft_unreconciled"):
        evidence.assess(wrong)
    wrong = sale_capture()
    wrong["batch"]["response"][4]["result"]["hash"] = "0x" + "8" * 64
    with pytest.raises(ValueError, match="receipt_block_not_canonical"):
        evidence.assess(wrong)


def test_mint_cost_requires_the_exact_token_and_subtracts_both_paid_gas_legs():
    capture = sale_capture()
    batch = capture["batch"]
    wallet, collection = capture["wallet"], capture["collection"]
    sale = batch["response"][2]["result"]
    mint_hash, block_hash = "0x" + "7" * 64, "0x" + "6" * 64
    mint_block = hex(int(sale["blockNumber"], 16) - 1)
    price = 15_000_000_000_000
    identity = {"transactionHash": mint_hash, "blockNumber": mint_block, "blockHash": block_hash, "removed": False}
    logs = [
        {**identity, "address": market.SEADROP, "logIndex": "0x0",
         "topics": [market.TOPIC_SEADROP_MINT, word(collection), word(wallet), word(market.OPENSEA_FEE_RECIPIENT)],
         "data": "0x" + "".join(word(v)[2:] for v in (wallet, 1, price, 1000, 0))},
        {**identity, "address": collection, "logIndex": "0x1", "data": "0x",
         "topics": [evidence.TRANSFER, word(market.ZERO_ADDRESS), word(wallet), word(int(capture["token_id"], 16))]},
    ]
    tx = {"hash": mint_hash, "blockHash": block_hash, "blockNumber": mint_block,
          "from": wallet, "to": market.SEADROP, "value": hex(price)}
    receipt = {**identity, "from": wallet, "to": market.SEADROP, "status": "0x1",
               "gasUsed": hex(137_585), "effectiveGasPrice": hex(35_000_000), "logs": logs}
    calls = [
        ("eth_getTransactionByHash", [mint_hash], tx),
        ("eth_getTransactionReceipt", [mint_hash], receipt),
        ("eth_getBlockByNumber", [mint_block, False], {"number": mint_block, "hash": block_hash}),
        ("eth_getLogs", [{
            "address": collection, "fromBlock": mint_block, "toBlock": sale["blockNumber"],
            "topics": [evidence.TRANSFER, None, None, word(int(capture["token_id"], 16))],
        }], [deepcopy(logs[1]), deepcopy(sale["logs"][-2])]),
    ]
    for i, (method, params, result) in enumerate(calls, 6):
        batch["request"].append({"jsonrpc": "2.0", "id": i, "method": method, "params": params})
        batch["response"].append({"jsonrpc": "2.0", "id": i, "result": result})
    capture["mint_tx"] = mint_hash
    output = evidence.assess(capture)
    assert output["scoped_round_trip_net_eth_equivalent_wei"] == (
        output["sale_net_eth_equivalent_wei"] - price - 137_585 * 35_000_000
    )
    history = batch["response"][-1]["result"]
    history.insert(1, deepcopy(history[0]))
    with pytest.raises(ValueError, match="nft_ownership_continuity_unverified"):
        evidence.assess(capture)
    history.pop(1)
    logs[1]["topics"][-1] = word(int(capture["token_id"], 16) + 1)
    with pytest.raises(ValueError, match="mint_cost_or_nft_unreconciled"):
        evidence.assess(capture)
