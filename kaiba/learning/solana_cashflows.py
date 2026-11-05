"""Pure transaction components from an original retained SOL RPC envelope.

This leaf does no acquisition, storage, or financial writes. The caller must first
authenticate the stored file against its original manifest and supply the exact
independently selected context. Receipt digests establish internal consistency,
not cryptographic consensus or a historical submit-wallet binding.

The added sensor joins native balances to token-authority account lifecycles.
Token authority does not establish close authority, rent reserve, or beneficial
ownership. Its explicitly partial native candidate must never be used as an
attributed ``economic_outcomes`` cashflow. Fees are observations already inside
balance changes and are never subtracted again.
"""
from __future__ import annotations

import re
from collections.abc import Mapping

from kaiba.execution import fill_costs as costs
from kaiba.execution.fills import MAX_TX_VERSION
from kaiba.learning import transaction_receipts as receipts

VERSION = "solana-cashflow-components/1"
MAX_ACCOUNTS = 512
MAX_INSTRUCTIONS = 4096
MAX_NODES = 50_000
MAX_DEPTH = 32
_HEX = re.compile(r"[0-9a-f]{64}")
_CONTEXT = {"position_id", "order_id", "chain", "token", "side", "signature", "mode", "lane"}


class _Refused(ValueError):
    pass


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise _Refused(reason)


def _clock(value) -> bool:
    return type(value) is int and 0 < value < 2**63


def _bounded(value) -> None:
    pending, count = [(value, 0)], 0
    while pending:
        item, depth = pending.pop()
        count += 1
        _require(depth <= MAX_DEPTH and count <= MAX_NODES, "structure_budget")
        if isinstance(item, dict):
            _require(len(item) <= MAX_NODES - count - len(pending), "structure_budget")
            _require(all(isinstance(k, str) for k in item), "nonstring_object_key")
            _require(all(len(k) <= receipts.MAX_FILE_BYTES for k in item), "string_budget")
            pending.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, list):
            _require(len(item) <= MAX_NODES - count - len(pending), "structure_budget")
            pending.extend((v, depth + 1) for v in item)
        elif isinstance(item, str):
            _require(len(item) <= receipts.MAX_FILE_BYTES, "string_budget")
        elif isinstance(item, (bytes, bytearray, memoryview)):
            raise _Refused("binary_evidence_not_json")
    receipts._encode(value, receipts.MAX_FILE_BYTES)


def _blank() -> dict:
    return {
        "version": VERSION, "status": "refused", "reason": None,
        "chain": "sol", "native_unit": "lamport", "native_decimals": 9,
        "economic_native_delta": None, "economic_complete": False,
        "complete_cashflow": False, "funding_allowed": False,
        "identity_verified": False, "historical_submit_wallet_binding": False,
        "attribution_proven": False, "ownership_complete": False,
        "transfers_classified": False, "rent_classified": False,
        "native_quote_verified": False, "fee_inclusion": "not_attributed",
        "wallet_native_delta": None, "cash_native_delta": None,
        "wallet_token_delta": None, "token_decimals": None,
        "network_fee_native_atoms": None,
        "network_fee_paid_by_wallet_native_atoms": None,
        "rent_locked_native": None, "rent_recovered_native": None,
        "usd_value": None, "usd_basis": "contemporaneous_conversion_unavailable",
        "account_changes": [], "native_delta_candidate": None, "lineage": {},
        "coverage": dict.fromkeys(("orders_complete", "attempts_complete",
                                    "recoveries_complete", "inventory_complete"), False),
        "unknown_reasons": [],
    }


def _validate(content: Mapping, context: Mapping, wallet: str, now_ms: int) -> dict:
    _require(isinstance(content, dict) and isinstance(context, dict), "envelope_or_context_shape")
    _bounded(content)
    _bounded(context)
    _require(_clock(now_ms), "invalid_projection_clock")
    _require(set(context) == _CONTEXT and context.get("chain") == "sol"
             and context.get("side") in ("buy", "sell")
             and context.get("mode") in ("live", "canary"), "expected_context_shape")
    _require(all(isinstance(context.get(k), str) and 0 < len(context[k]) <= 256
                 for k in _CONTEXT), "expected_context_shape")
    try:
        costs._sol_id(wallet, 32)
        costs._sol_id(context["token"], 32)
        costs._sol_id(context["signature"], 64)
    except ValueError:
        raise _Refused("chain_identifier_invalid") from None
    _require(content.get("version") == receipts.VERSION and content.get("status") == "retained",
             "original_retained_envelope_required")
    _require(content.get("context") == context
             and content.get("context_hash") == receipts._hash(context), "context_binding_mismatch")
    aid = receipts._hash({"chain": "sol", "signature": context["signature"]})
    _require(content.get("attempt_id") == aid, "attempt_binding_mismatch")
    _require(content.get("expected_wallet") in (None, wallet), "captured_wallet_mismatch")
    packet_sha = content.get("input_packet_sha256")
    _require(isinstance(packet_sha, str) and _HEX.fullmatch(packet_sha) is not None,
             "input_packet_digest_missing")
    receipt = content.get("receipt")
    _require(isinstance(receipt, dict), "provider_receipt_missing")
    clocks = [content.get("admitted_ms"), receipt.get("observed_at_ms"), content.get("completed_ms")]
    _require(all(_clock(v) for v in clocks) and clocks[0] <= clocks[1] <= clocks[2] <= now_ms,
             "observation_clock_invalid_or_future")
    request = content.get("request")
    params = request.get("params") if isinstance(request, dict) else None
    options = params[1] if isinstance(params, list) and len(params) == 2 else None
    requested_version = options.get("maxSupportedTransactionVersion") if isinstance(options, dict) else None
    _require(type(requested_version) is int and 0 <= requested_version <= MAX_TX_VERSION,
             "original_request_mismatch")
    _require(request == {
        "jsonrpc": "2.0", "id": "kaiba-economic-" + aid[:16], "method": "getTransaction",
        "params": [context["signature"], {"encoding": "jsonParsed", "commitment": "finalized",
                                          "maxSupportedTransactionVersion": requested_version}]},
        "original_request_mismatch")
    _require(content.get("endpoint_category") in ("public_solana_mainnet", "configured_solana_rpc"),
             "source_category_unproved")
    configured = content.get("endpoint_category") == "configured_solana_rpc"
    _require(configured == (content.get("acquisition_policy") == receipts.CONFIGURED_POLICY),
             "source_category_policy_mismatch")
    summary = receipts.summarize_receipt(content)
    _require(summary.get("status") == "observed_success", "original_observation_not_successful")
    _require(bool(summary.get("native_account_balances")), "native_balance_arrays_unproved")
    result = content["response"]["result"]
    version = result.get("version")
    _require(version == "legacy" or type(version) is int and 0 <= version <= requested_version,
             "transaction_version_unsupported")
    message, meta = result["transaction"]["message"], result["meta"]
    _require(isinstance(message.get("accountKeys"), list)
             and 0 < len(message["accountKeys"]) <= MAX_ACCOUNTS, "account_budget")
    top, groups = message.get("instructions"), meta.get("innerInstructions")
    _require(isinstance(top, list) and isinstance(groups, list) and len(groups) <= MAX_ACCOUNTS,
             "instruction_arrays_unproved")
    count = len(top)
    for group in groups:
        _require(isinstance(group, dict) and isinstance(group.get("instructions"), list),
                 "instruction_arrays_unproved")
        count += len(group["instructions"])
    _require(count <= MAX_INSTRUCTIONS, "instruction_budget")
    for field in ("preTokenBalances", "postTokenBalances"):
        _require(isinstance(meta.get(field), list) and len(meta[field]) <= MAX_ACCOUNTS,
                 "token_balance_arrays_unproved")
    return result


def _token_rows(rows: list, keys: list[str]) -> dict:
    """Validate every mint, rather than silently dropping malformed other assets."""
    answer = {}
    for row in rows:
        _require(isinstance(row, dict), "token_account_lineage_unproved")
        i = row.get("accountIndex")
        _require(type(i) is int and 0 <= i < len(keys) and i not in answer,
                 "token_account_index_or_duplicate")
        amount = row.get("uiTokenAmount")
        _require(isinstance(amount, dict), "token_account_units_unproved")
        raw, decimals = amount.get("amount"), amount.get("decimals")
        _require(isinstance(raw, str) and re.fullmatch(r"0|[1-9][0-9]{0,19}", raw) is not None
                 and int(raw) < 2**64 and type(decimals) is int and 0 <= decimals <= 255,
                 "token_account_units_unproved")
        try:
            mint, owner = costs._sol_id(row.get("mint"), 32), costs._sol_id(row.get("owner"), 32)
        except ValueError:
            raise _Refused("token_account_identity_unproved") from None
        _require(row.get("programId") in costs._TOKEN_PROGRAMS, "token_account_program_unproved")
        answer[i] = {"mint": mint, "owner": owner, "program": row["programId"],
                     "decimals": decimals, "amount": int(raw)}
    return answer


def _account_changes(result: dict, wallet: str, token: str) -> tuple[list[dict], set[str]]:
    """Observe token authorities and lamports, never infer recoverable rent."""
    meta, tx = result["meta"], result["transaction"]
    message = tx["message"]
    keys, _ = costs._account_keys(message, tx["signatures"], meta)
    pre_native, post_native = costs._native_balances(meta, keys)
    pre, post = (_token_rows(meta[field], keys) for field in ("preTokenBalances", "postTokenBalances"))
    instructions = costs._instructions(message, meta, keys)
    initialized, closed, authority = {}, {}, set()
    for path, ix, program in instructions:
        if program not in costs._TOKEN_PROGRAMS:
            continue
        parsed = ix.get("parsed")
        _require(isinstance(parsed, dict) and isinstance(parsed.get("info"), dict),
                 "opaque_token_program_activity")
        info, kind = parsed["info"], parsed.get("type")
        account = info.get("account")
        if kind in ("initializeAccount", "initializeAccount2", "initializeAccount3"):
            _require(account in keys and account not in initialized, "conflicting_account_initialization")
            initialized[account] = (info, program, path)
        elif kind == "closeAccount":
            _require(account in keys and account not in closed, "conflicting_account_closure")
            closed[account] = (info, program, path)
        elif kind == "setAuthority":
            _require(account in keys, "authority_change_account_unproved")
            authority.add(account)
    relevant = set(pre) | set(post)
    for account, (info, _program, _path) in initialized.items():
        if info.get("owner") == wallet:
            relevant.add(keys.index(account))
    answer, unknowns = [], set()
    for i in sorted(relevant):
        before, after, account = pre.get(i), post.get(i), keys[i]
        if before and after:
            _require(all(before[k] == after[k] for k in ("mint", "owner", "program", "decimals")),
                     "token_account_identity_changed")
        row = after or before
        init, close = initialized.get(account), closed.get(account)
        if row is None:
            _require(init is not None, "temporary_account_initialization_unproved")
            info, program, _path = init
            if info.get("owner") != wallet:
                continue
            try:
                mint = costs._sol_id(info.get("mint"), 32)
            except ValueError:
                raise _Refused("temporary_account_mint_unproved") from None
            row = {"owner": wallet, "mint": mint, "program": program, "decimals": None, "amount": None}
        if row["owner"] != wallet:
            continue
        _require(account != wallet, "wallet_token_account_alias")
        if init:
            info, program, _path = init
            _require(info.get("owner") == wallet and info.get("mint") == row["mint"]
                     and program == row["program"], "initialization_balance_identity_mismatch")
        creation = False
        if before is None and after is not None:
            creation = costs._creation_proved(i, row, row["mint"], keys, message, meta)
            if not creation:
                unknowns.add("missing_pre_balance_not_assumed_zero")
        closure = bool(close and close[0].get("owner") == wallet
                       and close[0].get("destination") == wallet and close[1] == row["program"]
                       and post_native[i] == 0)
        if close and post_native[i] != 0:
            unknowns.add("closure_and_post_native_balance_conflict")
        if before is not None and after is None and not closure:
            unknowns.add("missing_post_balance_not_assumed_closed")
        temporary = before is None and after is None
        if temporary and not (init and closure and pre_native[i] == post_native[i] == 0):
            unknowns.add("temporary_account_lifecycle_unproved")
        if account in authority:
            unknowns.add("token_account_authority_change_observed")
        is_wsol = row["mint"] == costs.WSOL_MINT
        if is_wsol:
            unknowns.add("wsol_principal_and_rent_not_separated")
        if row["program"] == costs._TOKEN_2022_PROGRAM:
            unknowns.add("token_2022_extensions_and_close_authority_unproved")
        answer.append({
            "account_index": i, "is_target_token": row["mint"] == token, "is_wsol": is_wsol,
            "token_program": row["program"], "token_decimals": row["decimals"],
            "pre_token_atoms": str(before["amount"]) if before else "0" if creation else None,
            "post_token_atoms": str(after["amount"]) if after else None,
            "pre_native_atoms": str(pre_native[i]), "post_native_atoms": str(post_native[i]),
            "native_delta_atoms": str(post_native[i] - pre_native[i]),
            "token_authority_observed": True, "lamport_control_proved": False,
            "initialization_observed": creation or temporary and init is not None,
            "closure_to_wallet_observed": closure, "authority_change_observed": account in authority,
            "rent_reserve_native_atoms": None, "rent_recovered_native_atoms": None,
            "field_sources": {"native": f"meta.preBalances/postBalances[{i}]",
                              "token": f"meta.preTokenBalances/postTokenBalances accountIndex={i}",
                              "initialization": init[2] if init else None,
                              "closure": close[2] if close else None},
        })
    return answer, unknowns


def project_solana_cashflow(content: Mapping, *, expected_context: Mapping,
                           expected_wallet: str, now_ms: int) -> dict:
    """Return qualified transaction components, never episode profit or authority.

    ``content`` is the original immutable retained content, not a decoder summary.
    Its external file/manifest authentication remains the caller's responsibility.
    ``expected_context`` must come from the independently selected order/position
    link. Supplying a wallet proves at most that the RPC reports it as a signer.
    Extra caller flags cannot promote custody, attribution, or episode coverage.
    """
    out = _blank()
    try:
        result = _validate(content, expected_context, expected_wallet, now_ms)
        parsed = costs.parse_solana_costs(result, wallet=expected_wallet,
            token=expected_context["token"], signature=expected_context["signature"],
            side=expected_context["side"])
        _require(parsed.get("identity_verified") is True, "supplied_wallet_signer_unproved")
        quantity = parsed.get("wallet_token_delta_atoms")
        if quantity is not None:
            _require((int(quantity) > 0 if expected_context["side"] == "buy" else int(quantity) < 0),
                     "target_quantity_side_mismatch")
        out.update(status="observed_components", wallet=expected_wallet, token=expected_context["token"],
            tx_hash=expected_context["signature"], order_id=expected_context["order_id"],
            position_id=expected_context["position_id"], kind="entry" if expected_context["side"] == "buy" else "exit",
            transaction_success=True, signer_observed=True,
            wallet_native_delta=parsed.get("wallet_native_delta_atoms"),
            cash_native_delta=parsed.get("wallet_native_delta_atoms"), wallet_token_delta=quantity,
            token_decimals=parsed.get("token_decimals") if quantity is not None else None,
            network_fee_native_atoms=parsed.get("network_fee_native_atoms"),
            network_fee_paid_by_wallet_native_atoms=parsed.get("network_fee_paid_by_wallet_native_atoms"))
        out["lineage"] = {
            "content_sha256": receipts._hash(content), "context_sha256": content["context_hash"],
            "input_packet_sha256": content["input_packet_sha256"],
            "request_digest": content["receipt"]["request_digest"],
            "response_digest": content["receipt"]["response_digest"],
            "admitted_ms": content["admitted_ms"], "observed_ms": content["receipt"]["observed_at_ms"],
            "completed_ms": content["completed_ms"], "transaction_ms": result["blockTime"] * 1000,
            "slot": result["slot"], "basis": "original_rpc_provider_reported",
            "chain_witness": "original_configured_genesis" if content.get("acquisition_policy") else
                             "original_public_endpoint_category",
            "external_manifest_authentication": "required_of_caller_not_proved_by_projector",
            "cash_source": "meta.postBalances - meta.preBalances at supplied signer accountIndex",
            "quantity_source": parsed["field_sources"].get("wallet_token_delta_atoms"),
            "fee_handling": "balance_deltas_unchanged_fees_never_subtracted",
        }
        unknowns = {"historical_submit_wallet_binding_unproved", "transaction_attribution_unproved",
                    "complete_wallet_account_ownership_unproved", "unrelated_transfers_not_classified",
                    "rent_reserve_and_close_authority_unproved", "episode_attempt_and_inventory_coverage_unproved",
                    "contemporaneous_usd_conversion_unavailable", "fee_payer_reimbursement_unreconciled"}
        if quantity is None:
            unknowns.add("target_token_quantity_or_units_unproved")
        if out["cash_native_delta"] is None:
            unknowns.add("wallet_native_balance_delta_unproved")
        if out["network_fee_native_atoms"] is None:
            unknowns.add("network_fee_component_unproved")
        try:
            changes, lineage_unknowns = _account_changes(result, expected_wallet, expected_context["token"])
            out["account_changes"] = changes
            unknowns.update(lineage_unknowns)
            blocking = lineage_unknowns - {"wsol_principal_and_rent_not_separated",
                                           "token_2022_extensions_and_close_authority_unproved"}
            if out["cash_native_delta"] is not None and not blocking:
                associated = sum(int(row["native_delta_atoms"]) for row in changes)
                out["native_delta_candidate"] = {
                    "value_atoms": str(int(out["cash_native_delta"]) + associated),
                    "associated_account_delta_atoms": str(associated),
                    "basis": "wallet_cash_plus_observed_token_authority_account_lamports",
                    "scope": "partial_transaction_components_not_isolated_trade",
                    "ownership_complete": False, "rent_classified": False,
                    "fee_handling": "already_in_balance_delta_do_not_subtract",
                }
        except _Refused as exc:
            unknowns.add(str(exc))
        except (ValueError, KeyError, TypeError, OverflowError):
            unknowns.add("associated_account_lineage_unproved")
        out["unknown_reasons"] = sorted(unknowns)
        if (quantity is None or out["cash_native_delta"] is None or out["native_delta_candidate"] is None
                or out["network_fee_native_atoms"] is None):
            out["status"] = "observed_partial_components"
        return out
    except (_Refused, receipts.ReceiptRefused) as exc:
        out = _blank()
        out.update(reason=str(exc), unknown_reasons=[str(exc)])
        return out
    except (KeyError, TypeError, ValueError, OverflowError, RecursionError, AttributeError):
        out = _blank()
        out.update(reason="malformed_original_evidence", unknown_reasons=["malformed_original_evidence"])
        return out
