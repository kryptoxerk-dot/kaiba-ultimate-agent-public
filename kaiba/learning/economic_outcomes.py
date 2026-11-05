"""Pure, conservative economic projection for frozen learning packets.

Codex owns this additive module under AUTOLEARN-IMPLEMENT-20261014. It does not
read a database, call providers, change the ledger, or decide strategy promotion.

``packet`` has ``position``, ``orders``, ``fill_costs``, ``trades``, ``attempts``,
``recoveries``, ``inventory``, ``identity``, and ``coverage`` mappings/lists. Raw
SQL rows are useful evidence but are NOT proof of isolated wallet economics.
Authoritative ``cashflows`` (alias ``transactions``) require chain/transaction,
wallet/token binding, an explicit delta basis, all-inclusive fees, proved
attribution, classified transfers, and token quantities. Monetary atoms must be
integers or integer strings, never floats. Output monetary atoms are strings.

Cashflow fields: ``chain, tx_hash, kind`` (entry/exit/failed_attempt/recovery/
rent_recovery), ``wallet, token, economic_native_delta, wallet_token_delta``,
``delta_basis, identity_verified, attribution_proven, native_quote_verified,
ownership_complete, transfers_classified, fee_inclusion='all_inclusive',
transaction_success``. SOL also requires ``rent_classified``. Entry rows require
``entry_outflow_native`` and ``entry_outflow_basis``. Each row names linked
``order_id``/``order_ids`` or its explicit ``position_id``. One transaction shared
by several orders needs a proved allocation. Partition allocations list *all*
parts and their transaction total; batch projection checks cross-episode reuse.

``inventory`` requires zero ``opening_quantity``, ``quantity_reconciled=True``,
``transfers_complete=True``, and ``qty_remaining``. Open inventory additionally
requires a size-specific, executable, all-inclusive ``quote`` at the evaluation
``frontier_ms``. Historical prices and future quotes cannot certify this value.
``coverage`` requires explicit ``orders_complete, attempts_complete,
recoveries_complete, inventory_complete`` booleans. Absence remains unknown.

PaperBroker packets use ``paper_model={name:'kaiba.execution.paper.PaperBroker',
fee_convention:'paper_broker_inclusive_ledger', complete:True}``. Its buy cost and
net sell proceeds already include model friction. ``trades.fees_native`` and
decoder ``total_cost_native`` are explanatory columns and are NEVER subtracted.
Model completeness remains distinct from measured economic completeness.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from decimal import Decimal, localcontext
from typing import Any

VERSION = "economic-outcomes-v2"
PAPER_CONVENTION = "paper_broker_inclusive_ledger"
_INTEGER = re.compile(r"[+-]?\d+\Z")
_MEASURED_BASES = {
    "sol_owned_accounts_observed", "wallet_balance_observed", "evm_native_trace_observed",
}
_ENTRY_BASES = {"observed_native_wallet_outflow", "observed_native_trace_outflow"}
_COVERAGE = ("orders_complete", "attempts_complete", "recoveries_complete", "inventory_complete")
_KINDS = {"entry", "exit", "failed_attempt", "recovery", "rent_recovery"}


def _map(value: Any) -> dict:
    return dict(value) if isinstance(value, Mapping) else {}


def _rows(value: Any) -> list[dict]:
    if isinstance(value, Mapping):
        return [dict(value)]
    if isinstance(value, (list, tuple)):
        return [_map(row) for row in value]
    return []


def _atoms(value: Any) -> int | None:
    if type(value) is int:
        return value
    if isinstance(value, str) and _INTEGER.fullmatch(value.strip()):
        return int(value.strip())
    return None


def _money(value: int | None) -> str | None:
    return str(value) if value is not None else None


def _canonical(value: Any) -> Any:
    """Stable hashable JSON; Decimal is preserved as text, unsupported types reject."""
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported packet value: {type(value).__name__}")


def _hash(value: Any) -> str:
    payload = json.dumps(_canonical(value), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def _ids(row: dict) -> list[str]:
    values = row.get("order_ids", [])
    values = values if isinstance(values, (list, tuple)) else []
    return sorted({str(value) for value in [*values, row.get("order_id")] if value is not None})


def _address_equal(left: Any, right: Any, chain: str) -> bool:
    if not isinstance(left, str) or not isinstance(right, str) or not left or not right:
        return False
    return left == right if chain == "sol" else left.lower() == right.lower()


def _tx_hash(row: dict, chain: str) -> str | None:
    value = row.get("tx_hash", row.get("signature"))
    if not isinstance(value, str) or not value:
        return None
    return value if chain == "sol" else value.lower()


def _rate(pnl: int | None, denominator: int | None) -> str | None:
    if pnl is None or denominator is None or denominator <= 0:
        return None
    with localcontext() as context:
        context.prec = max(50, len(str(abs(pnl))) + len(str(denominator)) + 10)
        return str(Decimal(pnl) * 100 / Decimal(denominator))


def _orders(packet: dict, position: dict, unknowns: set[str]) -> list[dict]:
    unique = {}
    for order in _rows(packet.get("orders")):
        oid = order.get("order_id")
        if not isinstance(oid, str) or not oid:
            unknowns.add("order_identity_missing")
            continue
        if oid in unique and _hash(order) != _hash(unique[oid]):
            unknowns.add("conflicting_order_rows")
        unique.setdefault(oid, order)
        if order.get("chain") != position.get("chain"):
            unknowns.add("order_chain_mismatch")
        if not _address_equal(order.get("token"), position.get("token"), position.get("chain", "")):
            unknowns.add("order_token_mismatch")
        if order.get("mode") != position.get("mode"):
            unknowns.add("order_mode_mismatch")
    if not unique:
        unknowns.add("linked_orders_missing")
    return list(unique.values())


def _allocation(row: dict, position_id: Any, linked_ids: list[str], delta: int | None,
                unknowns: set[str]) -> tuple[str, str, str | None]:
    allocation = _map(row.get("allocation"))
    scope = allocation.get("scope", "entire_transaction")
    key = str(allocation.get("key", "entire_transaction"))
    if allocation:
        if allocation.get("proven") is not True or allocation.get("position_id") != position_id:
            unknowns.add("transaction_allocation_unproved")
    elif len(linked_ids) > 1:
        unknowns.add("shared_order_transaction_allocation_unproved")
    if scope == "entire_transaction":
        if allocation and set(_ids(allocation)) != set(linked_ids):
            unknowns.add("allocation_order_set_mismatch")
        return scope, "entire_transaction", None
    if scope != "partition":
        unknowns.add("transaction_allocation_scope_unknown")
        return str(scope), key, None
    parts = _rows(allocation.get("parts"))
    total = _atoms(allocation.get("transaction_delta_native"))
    amounts = [_atoms(part.get("delta_native")) for part in parts]
    part_keys = [part.get("key") for part in parts]
    selected = [part for part in parts if part.get("key") == key]
    valid = (bool(parts) and all(isinstance(item, str) and item for item in part_keys)
             and len(set(part_keys)) == len(parts) and all(amount is not None for amount in amounts)
             and total is not None and sum(amounts) == total and len(selected) == 1)
    if valid:
        valid = (selected[0].get("position_id") == position_id
                 and _atoms(selected[0].get("delta_native")) == delta)
    if not valid:
        unknowns.add("transaction_partition_unproved")
    return scope, key, _hash({"parts": parts, "transaction_delta_native": total})


def _cashflows(packet: dict, position: dict, orders: list[dict], identity: dict,
               unknowns: set[str]) -> list[dict]:
    chain = position.get("chain", "")
    indexed_orders = {order["order_id"]: order for order in orders}
    transactions = _rows(packet.get("cashflows", packet.get("transactions")))
    # Recoveries are economic evidence too; listing them twice cannot charge twice.
    transactions.extend(_rows(packet.get("recoveries")))
    unique: dict[tuple, dict] = {}
    for row in transactions:
        tx_chain = row.get("chain")
        tx_hash = _tx_hash(row, tx_chain or "")
        linked_ids = _ids(row)
        kind = row.get("kind")
        delta = _atoms(row.get("economic_native_delta", row.get("wallet_native_delta")))
        row_unknowns: set[str] = set()
        if tx_chain != chain or tx_hash is None:
            row_unknowns.add("cashflow_transaction_identity_missing_or_mismatched")
        if not ((linked_ids and all(oid in indexed_orders for oid in linked_ids))
                or row.get("position_id") == position.get("position_id")):
            row_unknowns.add("cashflow_episode_link_unproved")
        if linked_ids and any(oid not in indexed_orders for oid in linked_ids):
            # An attempt may be outside position_orders, but must be explicitly linked.
            attempts = _rows(packet.get("attempts", packet.get("failed_attempts")))
            linked_attempts = {attempt.get("order_id") for attempt in attempts
                               if attempt.get("position_id") == position.get("position_id")}
            if not set(linked_ids).issubset(set(indexed_orders) | linked_attempts):
                row_unknowns.add("cashflow_order_link_unproved")
        if any(_tx_hash(indexed_orders[oid], chain) != tx_hash for oid in linked_ids if oid in indexed_orders):
            row_unknowns.add("cashflow_order_transaction_mismatch")
        if kind not in _KINDS:
            row_unknowns.add("cashflow_kind_unknown")
        if row.get("identity_verified") is not True or not _address_equal(
            row.get("wallet"), identity.get("wallet"), chain,
        ) or not _address_equal(row.get("token"), position.get("token"), chain):
            row_unknowns.add("cashflow_wallet_or_token_unproved")
        if row.get("attribution_proven") is not True:
            row_unknowns.add("cashflow_attribution_unproved")
        if row.get("native_quote_verified") is not True:
            row_unknowns.add("cashflow_native_quote_unproved")
        if row.get("ownership_complete") is not True or row.get("transfers_classified") is not True:
            row_unknowns.add("cashflow_transfers_or_ownership_unproved")
        if row.get("unrelated_transfers") or row.get("unknown_transfers"):
            row_unknowns.add("unrelated_or_unknown_transfers_present")
        if chain == "sol" and row.get("rent_classified") is not True:
            row_unknowns.add("cashflow_rent_classification_missing")
        basis = row.get("delta_basis")
        if (basis not in _MEASURED_BASES
                or (basis == "sol_owned_accounts_observed" and chain != "sol")
                or (basis == "evm_native_trace_observed" and chain == "sol")):
            row_unknowns.add("cashflow_delta_basis_unmeasured")
        if row.get("fee_inclusion") != "all_inclusive":
            row_unknowns.add("cashflow_fee_inclusion_unproved")
        if delta is None:
            row_unknowns.add("cashflow_delta_missing")
        if kind == "failed_attempt":
            if row.get("transaction_success") is not False or (delta is not None and delta > 0):
                row_unknowns.add("failed_transaction_cost_unproved")
        elif row.get("transaction_success") is not True:
            row_unknowns.add("cashflow_transaction_success_unproved")
        if kind == "entry" and delta is not None and delta >= 0:
            row_unknowns.add("entry_native_outflow_not_negative")
        if kind == "rent_recovery" and delta != 0:
            row_unknowns.add("rent_recovery_counted_as_profit")
        quantity = _atoms(row.get("wallet_token_delta"))
        if kind in {"entry", "exit"} and (quantity is None or (kind == "entry" and quantity <= 0)
                                          or (kind == "exit" and quantity >= 0)):
            row_unknowns.add("cashflow_token_quantity_unproved")
        if kind not in {"entry", "exit"} and quantity != 0:
            row_unknowns.add("nontrade_inventory_movement_unproved")
        expected_side = {"entry": "buy", "exit": "sell"}.get(kind)
        if expected_side and any(indexed_orders[oid].get("side") != expected_side
                                 for oid in linked_ids if oid in indexed_orders):
            row_unknowns.add("cashflow_order_side_mismatch")
        scope, allocation_key, proof_hash = _allocation(
            row, position.get("position_id"), linked_ids, delta, row_unknowns,
        )
        entry_outflow = _atoms(row.get("entry_outflow_native")) if kind == "entry" else None
        entry_basis = row.get("entry_outflow_basis") if kind == "entry" else None
        if kind == "entry" and (entry_outflow is None or entry_outflow <= 0
                                or entry_basis not in _ENTRY_BASES
                                or (entry_basis == "observed_native_trace_outflow" and chain == "sol")
                                or (delta is not None and entry_outflow < -delta)):
            row_unknowns.add("authoritative_entry_outflow_missing")
        normalized = {
            "chain": tx_chain, "tx_hash": tx_hash, "kind": kind, "order_ids": linked_ids,
            "delta_native": _money(delta), "delta_basis": basis,
            "token_delta": _money(quantity), "entry_outflow_native": _money(entry_outflow),
            "entry_outflow_basis": entry_basis, "fee_inclusion": row.get("fee_inclusion"),
            "cash_native_delta": _money(_atoms(row.get("cash_native_delta"))),
            "rent_locked_native": _money(_atoms(row.get("rent_locked_native"))),
            "rent_recovered_native": _money(_atoms(row.get("rent_recovered_native"))),
            "allocation_scope": scope, "allocation_key": allocation_key,
            "allocation_proof_hash": proof_hash, "complete": not row_unknowns,
            "unknown_reasons": sorted(row_unknowns),
        }
        key = (tx_chain, tx_hash, allocation_key)
        previous = unique.get(key)
        if previous is not None:
            if previous != normalized:
                previous["complete"] = False
                for field in ("delta_native", "token_delta", "entry_outflow_native"):
                    previous[field] = None
                previous["unknown_reasons"] = sorted(set(previous["unknown_reasons"])
                                                      | {"conflicting_transaction_rows"})
                unknowns.add("conflicting_transaction_rows")
            continue
        unique[key] = normalized
        unknowns.update(row_unknowns)
    if not unique:
        unknowns.add("attributable_transaction_cashflows_missing")
    return list(unique.values())


def _decoded_evidence(packet: dict, position: dict) -> list[dict]:
    """Preserve decoder qualifications; a reconciliation flag cannot certify money."""
    evidence = []
    for row in _rows(packet.get("fill_costs")):
        chain = row.get("chain", position.get("chain"))
        qualifications = _map(row.get("_capture_qualifications"))
        available = (qualifications.get("available_at_frontier") is not False
                     and qualifications.get("position_identity_verified") is not False)
        def known(field, source=row, usable=available):
            return source.get(field) if usable else None
        evidence.append({
            "order_id": row.get("order_id"), "chain": chain,
            "tx_hash": _tx_hash(row, chain or ""), "basis": row.get("basis"),
            "version": row.get("version"), "venue": row.get("venue"),
            "reported_reconciled": row.get("reconciled"),
            "available_at_projection_frontier": available,
            "capture_qualifications": qualifications,
            "field_qualifications": _map(row.get("_field_qualifications")),
            "wallet_native_delta": _money(_atoms(known("wallet_native_delta"))),
            "delta_basis": row.get("delta_basis") or (
                "sol_owned_accounts_decoder_requires_attribution" if chain == "sol"
                else "evm_gmgn_summary_plus_gas_requires_observation"),
            "network_fee_native": _money(_atoms(known("network_fee_native"))),
            "priority_fee_native": _money(_atoms(known("priority_fee_native"))),
            "router_fee_native": _money(_atoms(known("router_fee_native"))),
            "venue_fee_native": _money(_atoms(known("venue_fee_native"))),
            # Impact is Decimal explanatory cost, not another wallet payment.
            "total_cost_native": str(known("total_cost_native")) if known("total_cost_native") is not None else None,
            "economic_completeness_proved_by_reconciled": False,
        })
    return evidence


def _inventory(packet: dict, position: dict, unknowns: set[str], *, paper: bool) -> dict:
    inventory = _map(packet.get("inventory"))
    quantity = _atoms(inventory.get("qty_remaining"))
    position_quantity = _atoms(position.get("qty", position.get("qty_open")))
    if quantity is None or quantity < 0 or quantity != position_quantity:
        unknowns.add("remaining_inventory_quantity_unproved")
    if inventory.get("quantity_reconciled") is not True or inventory.get("transfers_complete") is not True:
        unknowns.add("inventory_reconciliation_missing")
    if _atoms(inventory.get("opening_quantity")) != 0:
        unknowns.add("opening_inventory_or_external_funding_unproved")
    if inventory.get("external_transfers") or inventory.get("airdrops"):
        unknowns.add("external_inventory_movements_present")
    closed = position.get("closed_ms") is not None
    if closed and quantity != 0:
        unknowns.add("closed_position_has_residual_inventory")
    quote_value = 0 if quantity == 0 else None
    quote = _map(inventory.get("quote"))
    if quantity is not None and quantity > 0:
        frontier = _atoms(inventory.get("frontier_ms", packet.get("frontier_ms")))
        at = _atoms(quote.get("at_ms"))
        max_age = _atoms(inventory.get("max_quote_age_ms", 5000))
        value = _atoms(quote.get("net_native"))
        usable = (quote.get("executable") is True and quote.get("fee_inclusion") == "all_inclusive"
                  and quote.get("chain") == position.get("chain")
                  and _address_equal(quote.get("token"), position.get("token"), position.get("chain", ""))
                  and _atoms(quote.get("qty")) == quantity and value is not None and value >= 0
                  and frontier is not None and at is not None and max_age is not None and max_age >= 0
                  and 0 <= frontier - at <= max_age and isinstance(quote.get("source"), str)
                  and bool(quote["source"]))
        if not usable:
            unknowns.add("executable_inventory_value_missing")
        else:
            quote_value = value
    return {
        "qty_remaining": _money(quantity), "opening_quantity": _money(_atoms(inventory.get("opening_quantity"))),
        "closed": closed, "liquidation_value_native": _money(quote_value),
        "frontier_ms": inventory.get("frontier_ms", packet.get("frontier_ms")),
        "valuation_basis": "paper_executable_quote" if paper and quantity else
        "executable_net_liquidation_quote" if quantity else "zero_inventory" if quantity == 0 else None,
    }


def _attempts(packet: dict, position: dict, orders: list[dict], cashflows: list[dict],
              unknowns: set[str], *, paper: bool) -> dict:
    attempts = _rows(packet.get("attempts", packet.get("failed_attempts")))
    rows = {str(row.get("attempt_id", row.get("order_id", index))): row
            for index, row in enumerate(attempts)}
    unresolved = 0
    no_submission = 0
    failed = 0
    represented_txs = {(flow["chain"], flow["tx_hash"]) for flow in cashflows}
    for row in [*rows.values(), *orders]:
        state = str(row.get("state", "")).lower()
        if state in {"filled", "partial"}:
            chain = row.get("chain", position.get("chain", ""))
            tx_hash = _tx_hash(row, chain)
            expected_kind = {"buy": "entry", "sell": "exit"}.get(row.get("side"))
            # A failed transaction or a recovery sharing an order ID cannot prove
            # its successful fill. Bind the successful native/token leg itself.
            represented_fill = any(
                flow["complete"] and flow["chain"] == chain and flow["tx_hash"] == tx_hash
                and flow["kind"] == expected_kind and row.get("order_id") in flow["order_ids"]
                for flow in cashflows
            )
            if not paper and not represented_fill:
                unknowns.add("filled_order_cashflow_missing")
            continue
        if state in {"planned", "refused", "rejected", "expired", "pre_send_refused"}:
            if row.get("no_submission_proven") is True or (paper and row.get("mode") == "shadow"):
                no_submission += 1
                continue
        if state in {"failed", "reverted"}:
            failed += 1
            tx = _tx_hash(row, position.get("chain", ""))
            if paper:
                continue
            if ((row.get("chain", position.get("chain")), tx) in represented_txs
                    and any(flow["kind"] == "failed_attempt" and flow["tx_hash"] == tx
                            for flow in cashflows)):
                continue
            unknowns.add("failed_attempt_cost_missing")
        else:
            unresolved += 1
            unknowns.add("unresolved_or_unproved_attempt")
    return {"explicit_attempt_count": len(rows), "failed_count": failed,
            "unresolved_count": unresolved, "proved_no_submission_count": no_submission}


def project_episode(packet: Mapping[str, Any]) -> dict[str, Any]:
    """Project one isolated episode; missing evidence produces UNPROVEN, never zero."""
    packet = _map(packet)
    position = _map(packet.get("position"))
    unknowns: set[str] = set()
    chain = position.get("chain")
    if not all(isinstance(position.get(key), str) and position[key]
               for key in ("position_id", "chain", "token", "mode")):
        unknowns.add("position_identity_missing")
    coverage = _map(packet.get("coverage"))
    for field in _COVERAGE:
        if coverage.get(field) is not True:
            unknowns.add(f"{field}_unproved")
    orders = _orders(packet, position, unknowns)
    trades = _rows(packet.get("trades", packet.get("trade")))
    ledger_pnl = _atoms(position.get("realized_native"))
    cost = _atoms(position.get("cost_native"))
    proceeds = _atoms(position.get("proceeds_native"))
    if ledger_pnl is None:
        unknowns.add("ledger_pnl_missing")
    for trade in trades:
        if _atoms(trade.get("pnl_native")) != ledger_pnl:
            unknowns.add("trade_position_ledger_mismatch")
    paper_model = _map(packet.get("paper_model"))
    paper = bool(paper_model)
    if paper:
        if (paper_model.get("name") != "kaiba.execution.paper.PaperBroker"
                or paper_model.get("fee_convention") != PAPER_CONVENTION
                or paper_model.get("complete") is not True or position.get("mode") != "shadow"):
            unknowns.add("paper_model_or_fee_convention_unproved")
        if any(not str(order.get("tx_hash", "")).startswith("paper:")
               for order in orders if order.get("state") in {"filled", "partial"}):
            unknowns.add("paper_order_origin_unproved")
        cashflows = []
        identity_complete = "position_identity_missing" not in unknowns
        if cost is None or cost <= 0 or proceeds is None or proceeds < 0:
            unknowns.add("paper_ledger_cashflow_missing")
        known_cashflow = proceeds - cost if proceeds is not None and cost is not None else None
        denominator = cost if cost is not None and cost > 0 else None
        if position.get("closed_ms") is not None and known_cashflow != ledger_pnl:
            unknowns.add("closed_ledger_cashflow_mismatch")
    else:
        identity = _map(packet.get("identity"))
        identity_complete = (identity.get("verified") is True and identity.get("chain") == chain
                             and _address_equal(identity.get("token"), position.get("token"), chain or "")
                             and isinstance(identity.get("wallet"), str) and bool(identity["wallet"]))
        if not identity_complete:
            unknowns.add("expected_trading_wallet_identity_unproved")
        cashflows = _cashflows(packet, position, orders, identity, unknowns)
        known_deltas = [_atoms(flow["delta_native"]) for flow in cashflows]
        known_cashflow = sum(value for value in known_deltas if value is not None) if any(
            value is not None for value in known_deltas) else None
        entry_flows = [flow for flow in cashflows if flow["kind"] == "entry"]
        outflows = [_atoms(flow["entry_outflow_native"]) for flow in entry_flows]
        denominator = sum(outflows) if outflows and all(value is not None for value in outflows) else None
        if not entry_flows:
            unknowns.add("entry_cashflow_missing")
    inventory = _inventory(packet, position, unknowns, paper=paper)
    if not paper and cashflows:
        quantities = [_atoms(flow["token_delta"]) for flow in cashflows]
        remaining = _atoms(inventory["qty_remaining"])
        if any(value is None for value in quantities) or remaining is None or sum(quantities) != remaining:
            unknowns.add("transaction_inventory_quantity_mismatch")
        entry_quantities = [_atoms(flow["token_delta"]) for flow in cashflows if flow["kind"] == "entry"]
        if (not entry_quantities or any(value is None for value in entry_quantities)
                or sum(entry_quantities) != _atoms(position.get("qty_total"))):
            unknowns.add("transaction_entry_quantity_ledger_mismatch")
    attempt_result = _attempts(packet, position, orders, cashflows, unknowns, paper=paper)
    if paper:
        buys = [order for order in orders if order.get("side") == "buy" and order.get("state") == "filled"]
        sells = [order for order in orders if order.get("side") == "sell" and order.get("state") == "filled"]
        bought = [_atoms(order.get("filled_out")) for order in buys]
        sold = [_atoms(order.get("amount_in")) for order in sells]
        received = [_atoms(order.get("filled_out")) for order in sells]
        total_qty = _atoms(position.get("qty_total"))
        if (not buys or any(value is None for value in [*bought, *sold])
                or total_qty is None or sum(bought) != total_qty
                or sum(bought) - sum(sold) != _atoms(inventory["qty_remaining"])):
            unknowns.add("paper_order_inventory_mismatch")
        if any(value is None for value in received) or sum(received) != proceeds:
            unknowns.add("paper_order_proceeds_mismatch")
    liquidation = _atoms(inventory["liquidation_value_native"])
    complete = not unknowns
    pnl = known_cashflow + liquidation if complete and known_cashflow is not None and liquidation is not None else None
    model_complete = paper and complete
    economic_complete = not paper and complete
    failed_costs = [-_atoms(flow["delta_native"]) for flow in cashflows
                    if flow["kind"] == "failed_attempt" and _atoms(flow["delta_native"]) is not None]
    recovery_deltas = [_atoms(flow["delta_native"]) for flow in cashflows
                       if flow["kind"] in {"recovery", "rent_recovery"} and _atoms(flow["delta_native"]) is not None]
    return _canonical({
        "version": VERSION, "spec_hash": _hash({"version": VERSION, "paper_convention": PAPER_CONVENTION}),
        "packet_hash": _hash(packet), "position_id": position.get("position_id"),
        "chain": chain, "token": position.get("token"), "mode": position.get("mode"),
        "lane": position.get("lane"), "opened_ms": position.get("opened_ms"), "closed_ms": position.get("closed_ms"),
        "ledger_pnl_native": _money(ledger_pnl), "ledger_cost_native": _money(cost),
        "ledger_proceeds_native": _money(proceeds), "known_cashflow_native": _money(known_cashflow),
        "known_cashflow_scope": "inclusive_paper_ledger" if paper else
        "complete_isolated_economic_cashflows" if complete else "unverified_or_partial_transaction_subset",
        "ledger_vs_cashflow_residual_native": _money(known_cashflow - ledger_pnl)
        if known_cashflow is not None and ledger_pnl is not None and position.get("closed_ms") is not None else None,
        "cashflow_is_complete": complete, "economic_complete": economic_complete,
        "model_complete": model_complete, "identity_complete": bool(identity_complete),
        "inventory_complete": not any("inventory" in reason or "opening_" in reason for reason in unknowns),
        "attempt_coverage_complete": coverage.get("attempts_complete") is True
        and not any("attempt" in reason for reason in unknowns),
        "attribution_complete": not paper and bool(cashflows) and all(flow["complete"] for flow in cashflows),
        "economic_pnl_native": _money(pnl) if economic_complete else None,
        "model_pnl_native": _money(pnl) if model_complete else None,
        "net_pnl_native": _money(pnl), "entry_outflow_native": _money(denominator),
        "entry_outflow_basis": PAPER_CONVENTION if paper else "independently_proved_native_entry_outflows",
        "net_return_pct": _rate(pnl, denominator), "economic_return_pct": _rate(pnl, denominator) if economic_complete else None,
        "model_return_pct": _rate(pnl, denominator) if model_complete else None,
        "failed_attempt_cost_native_known_subset": _money(sum(failed_costs)) if failed_costs else None,
        "recovery_delta_native_known_subset": _money(sum(recovery_deltas)) if recovery_deltas else None,
        "inventory": inventory, "attempts": attempt_result, "transactions": cashflows,
        "decoded_fill_evidence": _decoded_evidence(packet, position),
        "linked_order_ids": sorted(order["order_id"] for order in orders), "transaction_count": len(cashflows),
        "fee_inclusion": {"ledger": "existing_writer_convention", "cashflows": "all_inclusive_required",
                          "trade_fees_native": "informational_never_subtracted",
                          "decoded_total_cost_native": "informational_never_subtracted",
                          "priority_fee_native": "subset_of_network_fee_never_added",
                          "venue_and_router_fee_native": "already_in_inclusive_delta_never_added"},
        "paper_model": paper_model or None, "native_results_combined_across_chains": False,
        "usd_pnl": None, "usd_basis": "unavailable_without_contemporaneous_conversion",
        "unknown_reasons": sorted(unknowns),
        "verdict": "MODEL_COMPLETE" if model_complete else "ECONOMIC_COMPLETE" if economic_complete else "UNPROVEN",
    })


def project_outcomes(packets: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Idempotent batch projection with per-chain/mode coverage and reuse checks."""
    unique = {}
    conflicting_positions = set()
    for packet in packets:
        projected = project_episode(packet)
        key = (projected["chain"], projected["mode"], projected["position_id"])
        if key in unique and projected["packet_hash"] != unique[key]["packet_hash"]:
            conflicting_positions.add(key)
        unique.setdefault(key, projected)
    outcomes = list(unique.values())
    tx_owners = defaultdict(list)
    for outcome in outcomes:
        for flow in outcome["transactions"]:
            tx_owners[(flow["chain"], flow["tx_hash"])].append((outcome, flow))
    for owners in tx_owners.values():
        if len(owners) <= 1:
            continue
        keys = [flow["allocation_key"] for _, flow in owners]
        partition_proofs = {flow["allocation_proof_hash"] for _, flow in owners}
        valid_partition = (all(flow["allocation_scope"] == "partition" and flow["complete"]
                               for _, flow in owners) and len(set(keys)) == len(keys)
                           and len(partition_proofs) == 1 and None not in partition_proofs)
        if not valid_partition:
            for outcome, _ in owners:
                outcome["unknown_reasons"] = sorted(set(outcome["unknown_reasons"])
                                                      | {"transaction_reused_across_episodes_or_allocations"})
    groups = defaultdict(list)
    for outcome in outcomes:
        key = (outcome["chain"], outcome["mode"], outcome["position_id"])
        if key in conflicting_positions:
            outcome["unknown_reasons"] = sorted(set(outcome["unknown_reasons"]) | {"conflicting_episode_packets"})
        if outcome["unknown_reasons"]:
            for field in ("economic_complete", "model_complete", "cashflow_is_complete"):
                outcome[field] = False
            for field in ("economic_pnl_native", "model_pnl_native", "net_pnl_native",
                          "net_return_pct", "economic_return_pct", "model_return_pct"):
                outcome[field] = None
            outcome["verdict"] = "UNPROVEN"
        groups[(outcome["chain"], outcome["mode"])].append(outcome)
    summaries = []
    for (chain, mode), rows in sorted(groups.items(), key=lambda item: str(item[0])):
        measured = [row for row in rows if row["economic_complete"]]
        modelled = [row for row in rows if row["model_complete"]]
        summaries.append({
            "chain": chain, "mode": mode, "population_count": len(rows),
            "economic_complete_count": len(measured), "model_complete_count": len(modelled),
            "excluded_count": len(rows) - len(measured) - len(modelled),
            "economic_pnl_native_complete_subset": _money(sum(int(row["economic_pnl_native"]) for row in measured)) if measured else None,
            "model_pnl_native_complete_subset": _money(sum(int(row["model_pnl_native"]) for row in modelled)) if modelled else None,
            "economic_entry_outflow_native_complete_subset": _money(sum(int(row["entry_outflow_native"]) for row in measured)) if measured else None,
            "model_entry_outflow_native_complete_subset": _money(sum(int(row["entry_outflow_native"]) for row in modelled)) if modelled else None,
            "unknown_reason_counts": dict(Counter(reason for row in rows for reason in row["unknown_reasons"])),
        })
    return {"version": VERSION, "population_count": len(outcomes), "outcomes": outcomes,
            "by_chain_mode": summaries, "native_results_combined_across_chains": False,
            "economic_complete_count": sum(row["economic_complete"] for row in outcomes),
            "model_complete_count": sum(row["model_complete"] for row in outcomes),
            "excluded_count": sum(row["verdict"] == "UNPROVEN" for row in outcomes)}
