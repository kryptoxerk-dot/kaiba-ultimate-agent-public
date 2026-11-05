"""Reconcile bounded, saved DLMM position history against a finalized account snapshot.

Codex DLMM-CASHFLOWS-20261029. Pure evidence stage: no provider, DB, signing or cron access.
Provider-index coverage is not independent proof of chain completeness or realized profit.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
from decimal import Decimal, localcontext
from pathlib import Path

import base58

from kaiba.execution.fill_costs import _sol_id, _uint
from kaiba.learning.dlmm_actions import WSOL, decode_transaction
from kaiba.learning.dlmm_state import DecodeError, snapshot
from kaiba.learning.lp_inventory_replay import ReplayError, load_trace

MAX_PAGES = 20
MAX_TRANSACTIONS = 200
MAX_TOTAL_BYTES = 32 * 1024 * 1024


def _receipt(receipt: dict, method: str) -> tuple[list, object]:
    if not isinstance(receipt, dict):
        raise DecodeError("invalid_receipt")
    request, response = receipt.get("request"), receipt.get("response")
    if not isinstance(request, dict) or not isinstance(response, dict):
        raise DecodeError("missing_rpc_envelope")
    if (request.get("jsonrpc") != "2.0" or response.get("jsonrpc") != "2.0" or
            type(request.get("id")) is not int or type(response.get("id")) is not int or
            request["id"] != response["id"] or request.get("method") != method or
            "error" in response or "result" not in response):
        raise DecodeError("rpc_binding_invalid")
    started, received = _uint(receipt.get("started_ms")), _uint(receipt.get("received_ms"))
    if not 0 <= received - started <= 60_000:
        raise DecodeError("receipt_clock_invalid")
    params = request.get("params")
    if not isinstance(params, list) or len(params) != 2 or not isinstance(params[1], dict):
        raise DecodeError("rpc_parameters_invalid")
    return params, response["result"]


def _signature_rows(pages: list[dict], state: dict) -> tuple[list[dict], bool]:
    if not isinstance(pages, list) or not 1 <= len(pages) <= MAX_PAGES:
        raise DecodeError("signature_page_budget_exceeded")
    rows, seen, cursor, terminal, prior_slot = [], set(), None, False, None
    previous_received = state["received_ms"]
    for page in pages:
        params, batch = _receipt(page, "getSignaturesForAddress")
        options = params[1]
        limit = options.get("limit")
        if (params[0] != state["position_address"] or options.get("commitment") != "finalized" or
                set(options) - {"commitment", "limit", "minContextSlot", "before"} or
                type(limit) is not int or not 1 <= limit <= 100 or
                _uint(options.get("minContextSlot")) < state["slot"] or options.get("before") != cursor):
            raise DecodeError("signature_query_scope_invalid")
        if terminal or page["started_ms"] < previous_received:
            raise DecodeError("signature_page_chronology_invalid")
        previous_received = page["received_ms"]
        if not isinstance(batch, list) or len(batch) > limit:
            raise DecodeError("signature_page_invalid")
        terminal = not batch
        for row in batch:
            if not isinstance(row, dict) or "err" not in row or row.get("confirmationStatus") != "finalized":
                raise DecodeError("signature_row_invalid")
            signature, slot = row.get("signature"), _uint(row.get("slot"))
            _sol_id(signature, 64)
            if signature in seen:
                raise DecodeError("duplicate_signature")
            # Same-slot ordering requires block transaction ordering, which v1 does not fetch.
            if prior_slot is not None and slot >= prior_slot:
                raise DecodeError("signature_slot_order_unproved")
            if len(rows) >= MAX_TRANSACTIONS:
                raise DecodeError("history_transaction_budget_exceeded")
            seen.add(signature)
            rows.append(row)
            prior_slot = slot
        if batch:
            cursor = batch[-1]["signature"]
    return rows, terminal


def _marked_hold_comparison(state: dict, deposits: list[int], withdrawals: list[int], claims: list[int],
                            network_fees: int, other_debits: int, history_reconciled: bool) -> dict:
    unavailable = {"status": "unavailable", "net_pnl": None, "executable": False}
    if not history_reconciled or state["pool"]["mints"][1] != WSOL:
        return unavailable
    active = [b for b in state["pool_bins"] if b["id"] == state["pool"]["active_bin"]]
    if len(active) != 1 or not active[0]["price_q64"]:
        return unavailable
    # Atom ratio from an observed bin mark, NOT an executable liquidation quote. Holdings
    # benchmark retains each gross contribution in its contributed asset until this endpoint.
    # No capital-weighted return, historical USD/SOL cost basis or hypothetical fill is invented.
    with localcontext() as ctx:
        ctx.prec = 80
        price = Decimal(active[0]["price_q64"]) / Decimal(2**64)
        mark = lambda amounts: Decimal(amounts[0]) * price + Decimal(amounts[1])  # noqa: E731
        hold = mark(deposits)
        principal = mark([state["principal_atoms"][i] + withdrawals[i] for i in range(2)])
        gross_fees = mark([state["fee_atoms_gross"][i] + claims[i] for i in range(2)])
        lp = principal + gross_fees
        gross = lp - hold
        after_network = gross - network_fees
        after_debits = after_network - other_debits
        return {"status": "observed_bin_mark_only", "price_y_atoms_per_x_atom": str(price),
                "hold_contributed_assets_value_y_atoms": str(hold), "lp_and_distributions_value_y_atoms_gross": str(lp),
                "gross_lp_minus_hold_y_atoms": str(gross), "observed_network_fee_lamports": network_fees,
                "other_native_debits_purpose_unknown_lamports": other_debits,
                "lp_minus_hold_after_network_fees_y_atoms": str(after_network),
                "lp_minus_hold_after_all_observed_native_debits_y_atoms": str(after_debits),
                "gross_fee_value_y_atoms": str(gross_fees),
                "fee_hurdle_at_mark": {
                    "principal_and_withdrawals_minus_hold_y_atoms": str(principal - hold),
                    "gross_fees_needed_after_network_y_atoms": str(max(Decimal(0), hold - principal + network_fees)),
                    "additional_fees_needed_after_network_y_atoms": str(max(Decimal(0), -after_network)),
                    "additional_fees_needed_after_all_observed_debits_y_atoms": str(max(Decimal(0), -after_debits)),
                },
                "net_pnl": None, "net_return": None, "executable": False,
                "limits": ["This compares the same contributed asset stream at one terminal bin mark; it is not investment PnL.",
                           "Withdrawals and claims are assumed held in their distributed assets; subsequent wallet trades are out of scope.",
                           "Account funding is separate encumbered capital; its recovery and opportunity cost are not proved.",
                           "Other native debits are shown separately; their service/transfer purpose is unknown.",
                           "Transfer fees, liquidation impact and future exit fees are not deducted; fees are gross.",
                           "One selected external position cannot select or promote a Kaiba strategy."]}


def reconcile(capture: dict, pages: list[dict], transactions: list[dict]) -> dict:
    """Bind every indexed transaction, require creation plus terminal pagination, reconcile cashflows.

    Inputs must be archived after the snapshot; a short signature page alone never proves
    complete coverage. Invalid identities refuse; unavailable evidence produces named gaps.
    """
    state = snapshot(capture)
    rows, terminal = _signature_rows(pages, state)
    if not isinstance(transactions, list) or len(transactions) > MAX_TRANSACTIONS:
        raise DecodeError("transaction_receipt_budget_exceeded")
    position_account = capture["response"]["result"]["value"][1]
    owner = base58.b58encode(base64.b64decode(position_account["data"][0], validate=True)[40:72]).decode()
    indexed = {r["signature"]: r for r in rows}
    fetched, gaps = {}, []
    for receipt in transactions:
        params, result = _receipt(receipt, "getTransaction")
        signature, options = params
        if (signature not in indexed or signature in fetched or options != {
                "encoding": "jsonParsed", "commitment": "finalized", "maxSupportedTransactionVersion": 0} or
                type(options.get("maxSupportedTransactionVersion")) is not int or
                receipt["started_ms"] < state["received_ms"]):
            raise DecodeError("transaction_query_scope_invalid")
        if result is None:
            fetched[signature] = None
            continue
        decoded = decode_transaction(result, signature=signature, pool=state["pool_address"],
                                     position=state["position_address"], owner=owner, mints=tuple(state["pool"]["mints"]))
        row = indexed[signature]
        if decoded["slot"] != row["slot"] or result["meta"]["err"] != row["err"] or result.get("blockTime") != row.get("blockTime"):
            raise DecodeError("transaction_index_disagrees")
        fetched[signature] = decoded
    if not terminal:
        gaps.append("terminal_empty_page_missing")
    missing = [r for r in rows if r["slot"] <= state["slot"] and fetched.get(r["signature"]) is None]
    if missing:
        gaps.append("indexed_transaction_unavailable")
    decoded = [fetched[r["signature"]] for r in reversed(rows)
               if r["slot"] <= state["slot"] and fetched.get(r["signature"]) is not None]
    for tx in decoded:
        gaps.extend(tx["gaps"])
    actions = [a for tx in decoded if tx["status"] == "observed" for a in tx["actions"]]
    if not actions or actions[0]["name"] != "PositionCreate" or sum(a["name"] == "PositionCreate" for a in actions) != 1:
        gaps.append("unique_initial_creation_unproved")
    if any(a["name"] == "PositionClose" for a in actions):
        gaps.append("position_closed_before_live_snapshot")
    deposits, withdrawals, claims = [0, 0], [0, 0], [0, 0]
    fees = funding = token_funding = other = 0
    compositions = [0, 0]
    for tx in decoded:
        if tx["status"] == "failed":
            fees += tx["owner_network_fee_lamports"]
            continue
        for total, key in ((deposits, "deposits_atoms"), (withdrawals, "withdrawals_atoms"), (claims, "claimed_fee_atoms_gross")):
            for i in range(2):
                total[i] += tx[key][i]
        cash = tx["cashflows"]
        if cash is not None:
            fees += cash["owner_network_fee_lamports"]
            funding += cash["position_account_funding_delta_lamports"]
            token_funding += cash["owned_token_account_funding_delta_lamports"]
            other += cash["other_native_outflows_lamports"]
        for fee in tx["composition_fees_embedded"]:
            compositions[0] += fee["x"]
            compositions[1] += fee["y"]
    if claims != state["position"]["claimed_fees"]:
        gaps.append("claimed_fee_counter_mismatch")
    if funding != position_account["lamports"]:
        gaps.append("position_funding_counter_mismatch")
    if state["pool"]["rewards_present"]:
        gaps.append("pool_rewards_not_valued")
    gaps = sorted(set(gaps))
    return {"schema": "kaiba-dlmm-history/1", "snapshot_slot": state["slot"],
            "identity_fingerprint": hashlib.sha256((state["pool_address"] + ":" + state["position_address"]).encode()).hexdigest(),
            "provider_index_coverage_reconciled": not gaps, "gaps": gaps, "terminal_empty_page": terminal,
            "indexed_transactions": len(rows), "decoded_transactions_at_snapshot": len(decoded),
            "indexed_after_snapshot": sum(r["slot"] > state["slot"] for r in rows),
            "missing_transactions_at_snapshot": len(missing), "failed_transactions": sum(t["status"] == "failed" for t in decoded),
            "action_counts": {name: sum(a["name"] == name for a in actions) for name in sorted({a["name"] for a in actions})},
            "deposits_atoms": deposits, "withdrawals_atoms": withdrawals, "claimed_fee_atoms_gross": claims,
            "ending_principal_atoms": state["principal_atoms"], "ending_pending_fees_atoms_gross": state["fee_atoms_gross"],
            "observed_owner_network_fee_lamports": fees, "observed_other_native_outflows_lamports": other,
            "position_account_funding_lamports": funding, "owned_token_account_funding_delta_lamports": token_funding,
            "composition_fees_embedded_atoms": compositions,
            "marked_hold_comparison": _marked_hold_comparison(state, deposits, withdrawals, claims, fees, other, not gaps),
            "net_pnl": None, "net_return": None, "strategy_promotable": False, "chain_verified": False,
            "financial_execution": False,
            "limitations": ["Completeness is relative to this RPC provider's address index plus observed creation and counters.",
                            "No independent signature/consensus verification or individual intervening swap replay.",
                            "No executable liquidation quote, complete cost basis, policy winner or realized profit.",
                            "Failed transactions retain observed payer fees; composition fees are not debited twice.",
                            "Unknown events, third-party fee payers, rewards and unexplained wallet changes withhold coverage."]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--page", type=Path, action="append", required=True)
    parser.add_argument("--transaction", type=Path, action="append", default=[])
    args = parser.parse_args()
    paths = [args.capture, *args.page, *args.transaction]
    try:
        if len(args.page) > MAX_PAGES or len(args.transaction) > MAX_TRANSACTIONS or sum(p.stat().st_size for p in paths) > MAX_TOTAL_BYTES:
            raise DecodeError("history_file_budget_exceeded")
        inputs = [load_trace(p) for p in paths]
        report = reconcile(inputs[0][0], [r for r, _ in inputs[1:1 + len(args.page)]], [r for r, _ in inputs[1 + len(args.page):]])
        report["input_sha256"] = [digest for _, digest in inputs]
    except (DecodeError, ReplayError, OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, f"history refused: {type(exc).__name__}\n")
    print(json.dumps(report, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
