"""Pure RPC cost observations, never ledger quantities or complete swap cashflow.

Identity checks bind caller identifiers to RPC fields; they are not signature or
consensus verification. No I/O, floats, price estimation or provider attribution.
"""

import re

_BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
# Exact deployed ids also used by execution.policy; deliberately no policy imports.
_TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
_TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
_TOKEN_PROGRAMS = (_TOKEN_PROGRAM, _TOKEN_2022_PROGRAM)


def _uint(value, *, bits=64):
    if type(value) is int:
        number = value
    elif isinstance(value, str) and len(value) <= 78 and re.fullmatch(r"0|[1-9][0-9]*", value):
        number = int(value)
    else:
        raise ValueError("invalid_unsigned_integer")
    if not 0 <= number < 2**bits:
        raise ValueError("integer_out_of_range")
    return number


def _sol_id(value, size):
    if not isinstance(value, str) or not size <= len(value) <= size * 2:
        raise ValueError("malformed_solana_identifier")
    n = 0
    for char in value:
        if char not in _BASE58:
            raise ValueError("malformed_solana_identifier")
        n = n * 58 + _BASE58.index(char)
    length = len(value) - len(value.lstrip("1")) + (n.bit_length() + 7) // 8
    if length != size:
        raise ValueError("malformed_solana_identifier")
    return value


def _mapping(value):
    if not isinstance(value, dict):
        raise ValueError("malformed_rpc_object")
    return value


def _sequence(value):
    if not isinstance(value, list):
        raise ValueError("malformed_rpc_array")
    return value


def _blank(source, unit):
    return {
        "network_fee_native_atoms": None,
        "network_fee_payer": None,
        "network_fee_paid_by_wallet_native_atoms": None,
        "tax_paid_token_atoms": None,
        "transfer_deduction_token_atoms": None,
        "wallet_token_delta_atoms": None,
        "wallet_native_delta_atoms": None,
        "provider_fee_native_atoms": None,
        "complete_cashflow": False,
        "native_unit": unit,
        "source": source,
        "basis": "rpc_reported",
        "field_sources": {},
        "identity_verified": False,
        "refusal_reason": None,
        "unknowns": ["complete_cashflow_unproved", "tax_attribution_unproved",
                     "provider_fee_unattributed", "swap_cashflow_not_isolated"],
    }


def _refuse(out, reason):
    out["refusal_reason"] = reason
    out["unknowns"].append(reason)
    return out


def _account_keys(message, signatures, meta):
    entries = _sequence(message.get("accountKeys"))
    if not entries or not all(isinstance(key, str) for key in entries):
        return _parsed_keys(message, signatures)
    keys = [_sol_id(key, 32) for key in entries]
    header = _mapping(message.get("header"))
    required = _index(header.get("numRequiredSignatures"), len(keys) + 1)
    readonly_signed = _index(header.get("numReadonlySignedAccounts"), required + 1)
    _index(header.get("numReadonlyUnsignedAccounts"), len(keys) - required + 1)
    if required == 0 or required != len(signatures) or readonly_signed >= required:
        raise ValueError("invalid_fee_payer_or_signatures")
    lookups = _sequence(message.get("addressTableLookups", []))
    expected = {"writable": 0, "readonly": 0}
    for lookup in lookups:
        lookup = _mapping(lookup)
        _sol_id(lookup.get("accountKey"), 32)
        for kind, field in (("writable", "writableIndexes"), ("readonly", "readonlyIndexes")):
            indices = _sequence(lookup.get(field))
            for index in indices:
                _index(index, 256)
            expected[kind] += len(indices)
    loaded = meta.get("loadedAddresses")
    if loaded is None and lookups:
        raise ValueError("loaded_addresses_unavailable")
    if loaded is not None:
        loaded = _mapping(loaded)
        for kind in ("writable", "readonly"):
            addresses = _sequence(loaded.get(kind))
            if len(addresses) != expected[kind]:
                raise ValueError("loaded_address_count_mismatch")
            keys.extend(_sol_id(key, 32) for key in addresses)
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate_account_keys")
    return keys, [True] * required + [False] * (len(keys) - required)


def _parsed_keys(message, signatures):
    entries = _sequence(message["accountKeys"])
    keys = []
    signers = []
    for entry in entries:
        e = _mapping(entry)
        keys.append(_sol_id(e["pubkey"], 32))
        if type(e.get("signer")) is not bool or type(e.get("writable")) is not bool:
            raise ValueError("invalid_account_key_flags")
        if e.get("source", "transaction") not in ("transaction", "lookupTable"):
            raise ValueError("invalid_account_key_source")
        if e["signer"] and e.get("source", "transaction") != "transaction":
            raise ValueError("lookup_table_account_cannot_sign")
        signers.append(e["signer"])
    if not keys or len(set(keys)) != len(keys):
        raise ValueError("invalid_account_keys")
    count = sum(signers)
    if (not signers[0] or entries[0]["writable"] is not True
            or len(signatures) != count or signers != [True] * count + [False] * (len(keys) - count)):
        raise ValueError("invalid_fee_payer_or_signatures")
    return keys, signers


def _native_balances(meta, keys):
    pre = [_uint(n) for n in _sequence(meta.get("preBalances"))]
    post = [_uint(n) for n in _sequence(meta.get("postBalances"))]
    if len(pre) != len(keys) or len(post) != len(keys):
        raise ValueError("native_balance_account_count_mismatch")
    return pre, post


def _index(value, length):
    if type(value) is not int or not 0 <= value < length:
        raise ValueError("invalid_account_or_instruction_index")
    return value


def _instructions(message, meta, keys):
    top = _sequence(message.get("instructions"))
    all_ix = [(f"transaction.message.instructions[{i}]", ix) for i, ix in enumerate(top)]
    seen = set()
    for j, group in enumerate(_sequence(meta.get("innerInstructions"))):
        group = _mapping(group)
        i = _index(group.get("index"), len(top))
        if i in seen:
            raise ValueError("duplicate_inner_instruction_group")
        seen.add(i)
        for k, ix in enumerate(_sequence(group.get("instructions"))):
            all_ix.append((f"meta.innerInstructions[{j}].instructions[{k}]", ix))
    answer = []
    for path, ix in all_ix:
        ix = _mapping(ix)
        if "programIdIndex" in ix:
            raise ValueError("compiled_or_conflicting_instruction_evidence")
        program = _sol_id(ix.get("programId"), 32)
        if program not in keys:
            raise ValueError("instruction_program_not_in_account_keys")
        answer.append((path, ix, program))
    return answer


def _token_rows(rows, token, keys):
    answer = {}
    seen = set()
    for row in _sequence(rows):
        row = _mapping(row)
        i = _index(row.get("accountIndex"), len(keys))
        if i in seen:
            raise ValueError("duplicate_token_balance_index")
        seen.add(i)
        mint = _sol_id(row.get("mint"), 32)
        if mint != token:
            continue
        amount = _mapping(row.get("uiTokenAmount"))
        decimals = _index(amount.get("decimals"), 256)
        program = row.get("programId")
        if program not in _TOKEN_PROGRAMS:
            raise ValueError("token_program_unproved")
        answer[i] = {"owner": _sol_id(row.get("owner"), 32), "program": program,
                     "decimals": decimals, "amount": _uint(amount.get("amount"))}
    return answer


def _creation_proved(index, row, token, keys, message, meta):
    pre_native, _ = _native_balances(meta, keys)
    if pre_native[index] != 0:
        return False
    matches = []
    for _, ix, program in _instructions(message, meta, keys):
        parsed = ix.get("parsed")
        if program != row["program"] or not isinstance(parsed, dict):
            continue
        info = parsed.get("info")
        if (parsed.get("type") in ("initializeAccount", "initializeAccount2", "initializeAccount3")
                and isinstance(info, dict) and info.get("account") == keys[index]):
            matches.append(info.get("mint") == token and info.get("owner") == row["owner"])
    return matches == [True]


def _token_deltas(meta, message, keys, token):
    pre = _token_rows(meta.get("preTokenBalances"), token, keys)
    post = _token_rows(meta.get("postTokenBalances"), token, keys)
    rows = {}
    units = set()
    for i in sorted(pre.keys() | post.keys()):
        before, after = pre.get(i), post.get(i)
        row = after or before
        if before is None:
            if not _creation_proved(i, row, token, keys, message, meta):
                raise ValueError("missing_pre_token_balance_without_creation_proof")
            initial = 0
        else:
            initial = before["amount"]
        if after is None:
            raise ValueError("missing_post_token_balance")
        if before and any(before[k] != after[k] for k in ("owner", "program", "decimals")):
            raise ValueError("token_balance_identity_changed")
        units.add((row["program"], row["decimals"]))
        rows[keys[i]] = {**row, "delta": after["amount"] - initial, "created": before is None}
    if len(units) != 1:
        raise ValueError("target_token_units_unproved")
    return rows


def _single_transfer(meta, message, keys, token, wallet, side, rows):
    program = next(iter(rows.values()))["program"]
    candidates = []
    for path, ix, pid in _instructions(message, meta, keys):
        if pid not in _TOKEN_PROGRAMS:
            continue
        parsed = ix.get("parsed")
        if not isinstance(parsed, dict):
            # No decoding guesses, even if an opaque instruction's accounts look unrelated.
            raise ValueError("unparsed_token_program_activity")
        info = _mapping(parsed.get("info"))
        kind = parsed.get("type")
        account_fields = (info.get("source"), info.get("destination"), info.get("account"))
        relevant = info.get("mint") == token or any(
            isinstance(a, str) and a in rows for a in account_fields)
        if not relevant:
            if pid == program:
                raise ValueError("unresolved_target_token_program_activity")
            continue
        if pid != program:
            raise ValueError("target_instruction_program_mismatch")
        if kind == "getAccountDataSize" and info.get("mint") == token:
            continue  # Read-only size query emitted by ATA creation.
        if kind in ("initializeImmutableOwner", "initializeAccount", "initializeAccount2", "initializeAccount3"):
            account = info.get("account")
            if isinstance(account, str) and account in rows and rows[account]["created"]:
                continue  # Creation was proved independently from balances and initialization.
        if kind != "transferChecked" or info.get("mint") != token:
            raise ValueError("additional_or_unsupported_target_token_operation")
        candidates.append((path, info))
    if len(candidates) != 1:
        raise ValueError("target_transfer_not_single")
    path, info = candidates[0]
    source = _sol_id(info.get("source"), 32)
    destination = _sol_id(info.get("destination"), 32)
    if source == destination or source not in rows or destination not in rows:
        raise ValueError("transfer_balance_accounts_unproved")
    src, dst = rows[source], rows[destination]
    amount = _mapping(info.get("tokenAmount"))
    gross = _uint(amount.get("amount"))
    decimals = _index(amount.get("decimals"), 256)
    net = dst["delta"]
    if (gross == 0 or decimals != src["decimals"] or src["delta"] != -gross
            or not 0 <= net <= gross):
        raise ValueError("transfer_amounts_do_not_reconcile")
    if program == _TOKEN_PROGRAM and net != gross:
        raise ValueError("legacy_spl_transfer_amounts_do_not_reconcile")
    if any(row["delta"] != 0 for key, row in rows.items() if key not in (source, destination)):
        raise ValueError("additional_target_token_movement")
    if side == "buy" and not (dst["owner"] == wallet and src["owner"] != wallet):
        raise ValueError("transfer_ownership_or_side_mismatch")
    if side == "sell" and not (src["owner"] == wallet and dst["owner"] != wallet):
        raise ValueError("transfer_ownership_or_side_mismatch")
    return {
        "gross_transfer_token_atoms": str(gross),
        "net_transfer_token_atoms": str(net),
        "transfer_deduction_token_atoms": str(gross - net),
        "transfer_deduction_basis": "single_checked_transfer_reconciled_to_source_and_destination_deltas",
        "transfer_source_account": source,
        "transfer_destination_account": destination,
        "transfer_token_program": program,
    }, path + "; meta.preTokenBalances/postTokenBalances and wallet ownership reconciled"


def parse_solana_costs(result: dict, *, wallet: str, token: str, signature: str, side: str) -> dict:
    """Parse a successful getTransaction result for a matching wallet signer.

    An unrelated/non-signing wallet is conservatively refused. A token account's
    owner is checked separately before attributing any token balance movement.
    Parsed keys and raw keys with a complete header/lookup mapping are accepted.
    Transfer deductions require parsed instructions and both endpoint balances;
    closures, compiled instructions, extra token operations and unknown owners
    remain unknown. Even a measured zero deduction is not proof of zero tax.
    All atom amounts are decimal integer strings. No USD or price is inferred.
    """
    out = _blank("solana.getTransaction", "lamport")
    try:
        _sol_id(wallet, 32)
        _sol_id(token, 32)
        _sol_id(signature, 64)
        if side not in ("buy", "sell"):
            raise ValueError("invalid_side")
        r = _mapping(result)
        meta = _mapping(r["meta"])
        if "err" not in meta or meta["err"] is not None:
            raise ValueError("transaction_not_successful")
        if "status" in meta and meta["status"] != {"Ok": None}:
            raise ValueError("contradictory_transaction_status")
        tx = _mapping(r["transaction"])
        signatures = _sequence(tx["signatures"])
        if not signatures or signatures[0] != signature:
            raise ValueError("transaction_signature_mismatch")
        for sig in signatures:
            _sol_id(sig, 64)
        message = _mapping(tx["message"])
        keys, signers = _account_keys(message, signatures, meta)
        if wallet not in keys or not signers[keys.index(wallet)]:
            raise ValueError("wallet_signer_unproved")
    except (KeyError, TypeError, ValueError) as exc:
        return _refuse(out, str(exc))
    out["identity_verified"] = True
    out["signature"] = signature
    out["wallet"] = wallet
    out["token"] = token
    out["side"] = side
    payer = keys[0]
    out["network_fee_payer"] = payer
    out["network_fee_scope"] = "transaction_meta_fee_only"
    out["network_fee_wallet_scope"] = "direct_fee_payer_debit_only"
    out["field_sources"]["network_fee_payer"] = "transaction.message.accountKeys[0], writable signer"
    out["unknowns"].append("fee_payer_reimbursement_unreconciled")
    try:
        fee = str(_uint(meta.get("fee")))
    except ValueError:
        out["unknowns"].append("invalid_network_fee")
    else:
        out["network_fee_native_atoms"] = fee
        out["network_fee_paid_by_wallet_native_atoms"] = fee if payer == wallet else "0"
        out["field_sources"]["network_fee_native_atoms"] = "meta.fee"
        out["field_sources"]["network_fee_paid_by_wallet_native_atoms"] = "meta.fee debited to accountKeys[0]; exact wallet comparison"
    try:
        pre_native, post_native = _native_balances(meta, keys)
        i = keys.index(wallet)
        out["wallet_native_delta_atoms"] = str(post_native[i] - pre_native[i])
        out["field_sources"]["wallet_native_delta_atoms"] = "meta.postBalances - meta.preBalances at wallet accountIndex"
        out["unknowns"].append("native_delta_includes_rent_fees_tips_and_other_transfers")
    except ValueError:
        out["unknowns"].append("native_balance_delta_unproved")
    try:
        rows = _token_deltas(meta, message, keys, token)
        owned = [row for row in rows.values() if row["owner"] == wallet]
        if not owned:
            raise ValueError("target_token_wallet_ownership_unproved")
        out["wallet_token_delta_atoms"] = str(sum(row["delta"] for row in owned))
        out["token_decimals"] = owned[0]["decimals"]
        out["field_sources"]["wallet_token_delta_atoms"] = (
            "meta.preTokenBalances/postTokenBalances by accountIndex, mint, owner and programId; "
            "absent pre balance zero only with zero pre lamports and matching initialization")
    except (KeyError, TypeError, ValueError) as exc:
        out["unknowns"].append("token_balance_delta_unproved: " + str(exc))
        out["unknowns"].append("transfer_deduction_unproved")
        return out
    try:
        transfer, source_path = _single_transfer(meta, message, keys, token, wallet, side, rows)
        out.update(transfer)
        out["field_sources"]["transfer_deduction_token_atoms"] = source_path
        out["field_sources"]["gross_transfer_token_atoms"] = source_path.partition(";")[0] + ".parsed.info.tokenAmount.amount"
        out["field_sources"]["net_transfer_token_atoms"] = (
            "meta.postTokenBalances - meta.preTokenBalances at transfer_destination_account; proven creation zero if absent")
    except (KeyError, TypeError, ValueError) as exc:
        out["unknowns"].append("transfer_deduction_unproved: " + str(exc))
    return out


def _evm_id(value, size):
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{" + str(size * 2) + r"}", value):
        raise ValueError("malformed_evm_identifier")
    return value


def _evm_uint(value):
    if isinstance(value, str) and len(value) <= 66 and re.fullmatch(r"0x(?:0|[1-9a-fA-F][0-9a-fA-F]*)", value):
        return _uint(int(value, 16), bits=256)
    return _uint(value, bits=256)


def parse_evm_costs(
    receipt: dict, *, wallet: str, signature: str, chain_id: int, expected_chain_id: int
) -> dict:
    """Observe execution gas only; a receipt is not a wallet cashflow trace.

    ``chain_id`` MUST be the caller's independently established RPC-source chain;
    receipts usually omit chainId. It must match the expected transaction chain.
    Sender ownership is required; smart-account/paymaster attribution is unsupported.
    """
    out = _blank("evm.eth_getTransactionReceipt", "wei")
    out["l1_fee_native_atoms"] = None
    out["l1_fee_additive_to_execution_fee"] = None
    out["unknowns"].append("chain_specific_fee_components_unreconciled")
    try:
        _evm_id(wallet, 20)
        _evm_id(signature, 32)
        if (type(chain_id) is not int or type(expected_chain_id) is not int
                or not 0 < chain_id < 2**256 or chain_id != expected_chain_id):
            raise ValueError("rpc_source_chain_mismatch_or_invalid")
        receipt = _mapping(receipt)
        if "chainId" in receipt and _evm_uint(receipt["chainId"]) != chain_id:
            raise ValueError("receipt_chain_mismatch")
        if _evm_id(receipt.get("transactionHash"), 32).lower() != signature.lower():
            raise ValueError("transaction_hash_mismatch")
        if _evm_id(receipt.get("from"), 20).lower() != wallet.lower():
            raise ValueError("wallet_sender_unproved")
        if _evm_uint(receipt.get("status")) != 1:
            raise ValueError("transaction_not_successful")
        _evm_uint(receipt.get("blockNumber"))
        _evm_id(receipt.get("blockHash"), 32)
    except (KeyError, TypeError, ValueError) as exc:
        return _refuse(out, str(exc))
    out["identity_verified"] = True
    out["signature"] = signature
    out["wallet"] = wallet
    out["chain_id"] = str(chain_id)
    out["chain_id_basis"] = "caller_bound_rpc_source_chain_id_matched_to_expected"
    out["network_fee_scope"] = "execution_gas_only"
    out["network_fee_payer"] = receipt["from"]
    out["field_sources"]["network_fee_payer"] = "receipt.from, matched to wallet as a 20-byte hex address"
    try:
        gas_used = _evm_uint(receipt.get("gasUsed"))
        gas_price = _evm_uint(receipt.get("effectiveGasPrice"))
        fee = str(_uint(gas_used * gas_price, bits=256))
    except ValueError:
        out["unknowns"].append("invalid_execution_gas_fee")
    else:
        out["gas_used"] = str(gas_used)
        out["effective_gas_price_native_atoms"] = str(gas_price)
        out["field_sources"]["gas_used"] = "receipt.gasUsed"
        out["field_sources"]["effective_gas_price_native_atoms"] = "receipt.effectiveGasPrice"
        out["network_fee_native_atoms"] = fee
        out["network_fee_paid_by_wallet_native_atoms"] = fee
        out["field_sources"]["network_fee_native_atoms"] = "receipt.gasUsed * receipt.effectiveGasPrice"
        out["field_sources"]["network_fee_paid_by_wallet_native_atoms"] = (
            "receipt.gasUsed * receipt.effectiveGasPrice; receipt.from matched to wallet; sender debit only")
    if "l1Fee" in receipt:
        try:
            out["l1_fee_native_atoms"] = str(_evm_uint(receipt["l1Fee"]))
            out["field_sources"]["l1_fee_native_atoms"] = (
                "receipt.l1Fee (reported component; chain-specific overlap not reconciled)")
        except ValueError:
            out["unknowns"].append("invalid_l1_fee_component")
    return out
