"""Read settled single-NFT Robinhood Seaport bid proceeds from exact receipts."""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Mapping
from pathlib import Path

from kaiba.execution.policy import keccak256
from kaiba.providers import seaport_rh as market

VERSION = "rh-nft-sale-evidence/1"
MAX_BYTES = 512 * 1024
MAX_MINT_TO_SALE_BLOCKS = 10_000
ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")
HASH = re.compile(r"0x[0-9a-fA-F]{64}")
QUANTITY = re.compile(r"0x(?:0|[1-9a-fA-F][0-9a-fA-F]{0,63})")
TRANSFER = "0x" + keccak256(b"Transfer(address,address,uint256)").hex()


def address(value):
    if not isinstance(value, str) or ADDRESS.fullmatch(value) is None:
        raise ValueError("invalid_address")
    return value.lower()


def hash32(value):
    if not isinstance(value, str) or HASH.fullmatch(value) is None:
        raise ValueError("invalid_hash")
    return value.lower()


def quantity(value):
    if not isinstance(value, str) or QUANTITY.fullmatch(value) is None:
        raise ValueError("invalid_quantity")
    return int(value, 16)


def topic_address(value):
    word = hash32(value)
    if word[2:26] != "0" * 24:
        raise ValueError("invalid_address_topic")
    return address("0x" + word[26:])


class ReceiptBatch:
    def __init__(self, batch):
        requests, responses = batch["request"], batch["response"]
        if not (
            isinstance(requests, list) and isinstance(responses, list)
            and 4 <= len(requests) == len(responses) <= 10
        ):
            raise ValueError("invalid_receipt_batch")
        if any(
            not isinstance(row, Mapping) or row.get("jsonrpc") != "2.0"
            or type(row.get("id")) is not int or row["id"] <= 0
            for row in (*requests, *responses)
        ):
            raise ValueError("invalid_rpc_envelope")
        self.requests = requests
        self.responses = {row["id"]: row for row in responses}
        if len(self.responses) != len(requests) or {row["id"] for row in requests} != self.responses.keys():
            raise ValueError("unbound_rpc_response")
        if quantity(self.result("eth_chainId", [])) != market.CHAIN_ID:
            raise ValueError("wrong_chain")
        self.finalized = quantity(self.result("eth_getBlockByNumber", ["finalized", False])["number"])

    def result(self, method, params):
        matching = [row["id"] for row in self.requests if row.get("method") == method and row.get("params") == params]
        if len(matching) != 1:
            raise ValueError("missing_or_duplicate_rpc_request")
        response = self.responses[matching[0]]
        if "error" in response or response.get("result") is None:
            raise ValueError("rpc_result_unavailable")
        return response["result"]

    def transaction(self, tx_hash, wallet):
        tx_hash = hash32(tx_hash)
        tx = self.result("eth_getTransactionByHash", [tx_hash])
        receipt = self.result("eth_getTransactionReceipt", [tx_hash])
        block = quantity(receipt["blockNumber"])
        block_hash = hash32(receipt["blockHash"])
        if (
            hash32(tx["hash"]) != tx_hash or hash32(receipt["transactionHash"]) != tx_hash
            or quantity(tx["blockNumber"]) != block or hash32(tx["blockHash"]) != block_hash
            or quantity(tx.get("chainId", hex(market.CHAIN_ID))) != market.CHAIN_ID
            or not 0 < block <= self.finalized or quantity(receipt["status"]) != 1
            or address(tx["from"]) != wallet or address(receipt["from"]) != wallet
            or address(tx["to"]) != address(receipt["to"])
        ):
            raise ValueError("transaction_identity_or_finality")
        canonical = self.result("eth_getBlockByNumber", [hex(block), False])
        if quantity(canonical["number"]) != block or hash32(canonical["hash"]) != block_hash:
            raise ValueError("receipt_block_not_canonical")
        logs = receipt["logs"]
        if not isinstance(logs, list) or not 1 <= len(logs) <= 64:
            raise ValueError("receipt_log_scope")
        indexes = set()
        for log in logs:
            index = quantity(log["logIndex"])
            if (
                index in indexes or log.get("removed") is not False
                or hash32(log["transactionHash"]) != tx_hash or hash32(log["blockHash"]) != block_hash
                or quantity(log["blockNumber"]) != block
            ):
                raise ValueError("receipt_log_identity")
            indexes.add(index)
        gas_used, gas_price = quantity(receipt["gasUsed"]), quantity(receipt["effectiveGasPrice"])
        if not gas_used or not gas_price:
            raise ValueError("receipt_gas_unavailable")
        # Robinhood's Nitro total gas already accounts for L1 data.
        return tx, logs, block, gas_used * gas_price


def wallet_transfers(logs, wallet):
    nfts = []
    weth_delta = 0
    for log in logs:
        topics = log["topics"]
        if not isinstance(topics, list) or any(not isinstance(topic, str) for topic in topics):
            raise ValueError("invalid_log_topics")
        if not topics or topics[0].lower() != TRANSFER:
            continue
        if len(topics) not in (3, 4):
            raise ValueError("unsupported_transfer_shape")
        sender, receiver = map(topic_address, topics[1:3])
        if wallet not in (sender, receiver):
            continue
        contract = address(log["address"])
        if len(topics) == 4 and log["data"] == "0x":
            nfts.append((contract, int(hash32(topics[3]), 16), sender, receiver))
        elif len(topics) == 3 and contract == market.WETH:
            amount = int(hash32(log["data"]), 16)
            weth_delta += amount * ((receiver == wallet) - (sender == wallet))
        else:
            raise ValueError("unexpected_wallet_transfer")
    return nfts, weth_delta


def require_continuous_holding(batch, collection, token_id, mint_block, sale_block, mint_logs, sale_logs):
    if not 0 < sale_block - mint_block <= MAX_MINT_TO_SALE_BLOCKS:
        raise ValueError("mint_to_sale_history_outside_bound")
    topics = [TRANSFER, None, None, "0x" + format(token_id, "064x")]
    history = batch.result("eth_getLogs", [{
        "address": collection, "fromBlock": hex(mint_block), "toBlock": hex(sale_block), "topics": topics,
    }])
    if not isinstance(history, list) or len(history) != 2:
        raise ValueError("nft_ownership_continuity_unverified")

    def identity(log):
        return tuple(log[key] for key in (
            "address", "transactionHash", "blockHash", "blockNumber", "logIndex", "data", "topics", "removed",
        ))

    target = [log for log in (*mint_logs, *sale_logs) if log["address"].lower() == collection
              and len(log["topics"]) == 4 and log["topics"][0].lower() == TRANSFER
              and log["topics"][3].lower() == topics[3]]
    if len(target) != 2 or [identity(log) for log in history] != [identity(log) for log in target]:
        raise ValueError("nft_ownership_continuity_unverified")


def assess(capture):
    if capture["version"] != VERSION:
        raise ValueError("unsupported_capture")
    wallet, collection = address(capture["wallet"]), address(capture["collection"])
    token_id = quantity(capture["token_id"])
    batch = ReceiptBatch(capture["batch"])
    sale_tx, logs, sale_block, sale_gas = batch.transaction(capture["sale_tx"], wallet)
    if address(sale_tx["to"]) != market.SEAPORT or quantity(sale_tx["value"]) != 0:
        raise ValueError("unsupported_sale_call")
    candidates = []
    for entry in logs:
        of = market.decode_order_fulfilled(entry)
        if of is None or len(of.offer) != 1:
            continue
        nfts = [item for item in of.consideration if item.is_nft]
        if len(nfts) != 1 or nfts[0].item_type != market.ITEM_ERC721 or nfts[0].amount != 1:
            continue
        kind, sale = market.classify(of)
        if (
            kind == "offer" and sale is not None and sale.seller == wallet
            and sale.collection == collection and sale.token_id == token_id
            and sale.payment_token == market.WETH and sale.buyer != market.ZERO_ADDRESS
        ):
            candidates.append(sale)
    if len(candidates) != 1:
        raise ValueError("exact_accepted_bid_unavailable")
    sale = candidates[0]
    nft, paid = wallet_transfers(logs, wallet)
    if nft != [(collection, token_id, wallet, sale.buyer)] or paid != sale.seller_net:
        raise ValueError("sale_payout_or_nft_unreconciled")
    exit_net = paid - sale_gas
    mint_value = mint_gas = round_trip = None
    if capture.get("mint_tx") is not None:
        mint_tx, mint_logs, mint_block, mint_gas = batch.transaction(capture["mint_tx"], wallet)
        if mint_block >= sale_block or address(mint_tx["to"]) != market.SEADROP:
            raise ValueError("unsupported_mint_call_or_clock")
        mints = [mint for log in mint_logs if (mint := market.decode_seadrop_mint(log)) is not None]
        minted, weth = wallet_transfers(mint_logs, wallet)
        mint_value = quantity(mint_tx["value"])
        if (
            len(mints) != 1 or mints[0].minter != wallet or mints[0].payer != wallet
            or mints[0].collection != collection or mints[0].quantity != 1 or mints[0].value_wei != mint_value
            or minted != [(collection, token_id, market.ZERO_ADDRESS, wallet)] or weth != 0
        ):
            raise ValueError("mint_cost_or_nft_unreconciled")
        require_continuous_holding(batch, collection, token_id, mint_block, sale_block, mint_logs, logs)
        round_trip = exit_net - mint_value - mint_gas
    return {
        "version": VERSION, "basis": "finalized_rpc_exact_nft_and_weth_logs",
        "financial_execution": False, "live_offer_verified": False,
        "bid_gross_wei": sale.gross, "market_fee_wei": sale.market_fee, "royalty_wei": sale.royalty,
        "actual_weth_received_wei": paid, "sale_total_gas_wei": sale_gas,
        "sale_net_eth_equivalent_wei": exit_net, "mint_value_wei": mint_value,
        "mint_total_gas_wei": mint_gas, "scoped_round_trip_net_eth_equivalent_wei": round_trip,
        "additional_approval_unwrap_and_internal_eth_cashflow_unmeasured": True,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    args = parser.parse_args(argv)
    try:
        with args.capture.open("rb") as source:
            raw = source.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("capture_size")
        result = assess(json.loads(raw))
    except (OSError, ValueError, TypeError, KeyError, IndexError, RecursionError):
        print(json.dumps({"version": VERSION, "status": "unpriced", "financial_execution": False}))
        return 2
    print(json.dumps(result, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
