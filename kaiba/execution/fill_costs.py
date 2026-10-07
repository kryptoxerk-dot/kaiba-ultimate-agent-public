"""What a fill actually paid, read from the transaction itself.

Two layers live here.

1. :func:`parse_solana_costs` / :func:`parse_evm_costs` (unchanged; used by
   ``inventory_repair``): pure RPC cost observations, never ledger quantities or complete
   swap cashflow. Identity checks bind caller identifiers to RPC fields; they are not
   signature or consensus verification. No I/O, floats or provider attribution.

2. The venue-event decoder (2026-10-04, below the ``Venue trade events`` banner):
   :func:`decode_solana_fill` / :func:`decode_evm_fill` read the venue program's OWN trade
   event out of a fill's transaction -- pump.fun ``TradeEvent``, PumpSwap ``BuyEvent`` /
   ``SellEvent``, Raydium LaunchLab ``TradeEvent``, and on Robinhood Chain the Uniswap
   v4/v3/v2 ``Swap``, Pons ``CurveBuy``/``CurveSell`` and ``HookFeeCollected`` logs plus
   GMGN's router fee event -- and split the all-in cost into venue fees, price impact
   against the pre-trade reserves, the network fee, the router's (GMGN's) take and a
   residual that must reconcile to the wallet's own balance change.
   :func:`record_fill_costs` stores one row per order in ``fill_costs`` (migration 035).
   Nothing is guessed: a fill with no attributable venue event is recorded as
   ``no_venue_event`` with the programs it touched. Money is integer atoms; prices and
   ratios are ``Decimal``; nothing calls ``float()``.

Event layouts and the event-CPI framing are adapted from chainstacklabs/pumpfun-bonkfun-bot
(Apache-2.0); see ``reference/chainstack-pumpfun/SOURCE.md``.
"""

import argparse
import base64
import hashlib
import json
import logging
import os
import re
import sqlite3
import struct
import time
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext

log = logging.getLogger(__name__)

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


# ======================================================================================
# Venue trade events: what each fill paid, component by component (2026-10-04)
# ======================================================================================
#
# Why this exists. MEASURED 2026-10-04 on the box, live filled orders of the last 30 days:
# 673 orders, every one with a tx hash and a GMGN fill quantity; 0 with ``orders.fee_native``;
# 0 ``fill_reconciliations`` rows (``fills.reconcile_onchain`` has no production caller); and
# 0 of 284 live ``trades`` rows with ``slippage_bps``, because ``accounting._close_trade`` is
# called with ``slippage_bps=None`` by construction. The decision-time reference was never
# missing: every order carries ``min_out = expected * (10000 - slippage_bps) / 10000``.
#
# What the chain says GMGN's own numbers leave out (MEASURED on the same sample):
#   * sol: GMGN takes ~1% as a plain system transfer OUTSIDE the venue instruction, plus a
#     10,000-lamport relay tip. ``orders.amount_in`` excludes both on a buy; on a sell
#     ``filled_out`` is the venue's output BEFORE them -- and on a pump.fun curve sell it is the
#     curve's GROSS ``sol_amount``, before pump.fun's own fees as well.
#   * robinhood: the GMGN router (the tx ``to``) emits its fee; ``amount_in`` INCLUDES it on a
#     buy and ``filled_out`` is net of it on a sell.

VERSION = "fill-costs-v1"

_CTX = Context(prec=50, rounding=ROUND_HALF_EVEN)
_TEN_K = Decimal(10_000)

WSOL_MINT = "So11111111111111111111111111111111111111112"
DEFAULT_PUBKEY = "11111111111111111111111111111111"
SYSTEM_PROGRAM = "11111111111111111111111111111111"
PUMP_FUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_SWAP_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
LAUNCHLAB_PROGRAM = "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj"
VENUE_PROGRAMS: dict[str, str] = {
    PUMP_FUN_PROGRAM: "pump_fun",
    PUMP_SWAP_PROGRAM: "pump_swap",
    LAUNCHLAB_PROGRAM: "launchlab",
}
#: Anchor ``emit_cpi!``: the program invokes itself with this tag, then the event bytes.
EVENT_CPI_TAG = bytes.fromhex("e445a52e51cb9a1d")
#: Base fee per signature, a protocol constant; ``meta.fee`` above it is the priority fee.
LAMPORTS_PER_SIGNATURE = 5_000

# --- base58 / ed25519 ------------------------------------------------------------------


def _b58decode(text: str) -> bytes:
    if not isinstance(text, str):
        raise ValueError("malformed_base58")
    n = 0
    for char in text:
        i = _BASE58.find(char)
        if i < 0:
            raise ValueError("malformed_base58")
        n = n * 58 + i
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\0" * (len(text) - len(text.lstrip("1"))) + body


def _b58encode(raw: bytes) -> str:
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _BASE58[r] + out
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + out


_ED_P = 2**255 - 19
_ED_D = (-121665 * pow(121666, _ED_P - 2, _ED_P)) % _ED_P


def _on_ed25519_curve(raw: bytes) -> bool:
    """Whether 32 bytes decompress to an ed25519 point (curve25519-dalek semantics)."""
    y = (int.from_bytes(raw, "little") & ((1 << 255) - 1)) % _ED_P
    yy = y * y % _ED_P
    u = (yy - 1) % _ED_P
    v = (_ED_D * yy + 1) % _ED_P
    if v == 0:
        return u == 0
    x2 = u * pow(v, _ED_P - 2, _ED_P) % _ED_P
    return x2 == 0 or pow(x2, (_ED_P - 1) // 2, _ED_P) == 1


def find_program_address(seeds: list[bytes], program_id: str) -> tuple[str, int]:
    """Solana ``find_program_address``: the first off-curve hash from bump 255 down."""
    program = _b58decode(program_id)
    if len(program) != 32:
        raise ValueError("malformed_program_id")
    for bump in range(255, -1, -1):
        digest = hashlib.sha256(b"".join(seeds) + bytes([bump]) + program + b"ProgramDerivedAddress").digest()
        if not _on_ed25519_curve(digest):
            return _b58encode(digest), bump
    raise ValueError("no_viable_bump")


#: The only account an Anchor event-CPI carries, and the reason it cannot be forged: the
#: program's ``__event_cpi`` handler requires this PDA to SIGN, which only the program can do.
EVENT_AUTHORITY: dict[str, str] = {p: find_program_address([b"__event_authority"], p)[0] for p in VENUE_PROGRAMS}

# --- event layouts (from the vendored IDLs; tests pin them field-for-field) -------------

_SHAREHOLDER = (("address", "pubkey"), ("share_bps", "u16"))

#: pump.fun ``TradeEvent``; the first 16 fields are required, the rest were appended by later
#: program versions and are decoded when present.
PUMP_TRADE_EVENT: tuple = (
    ("mint", "pubkey"), ("sol_amount", "u64"), ("token_amount", "u64"), ("is_buy", "bool"),
    ("user", "pubkey"), ("timestamp", "i64"), ("virtual_sol_reserves", "u64"),
    ("virtual_token_reserves", "u64"), ("real_sol_reserves", "u64"), ("real_token_reserves", "u64"),
    ("fee_recipient", "pubkey"), ("fee_basis_points", "u64"), ("fee", "u64"), ("creator", "pubkey"),
    ("creator_fee_basis_points", "u64"), ("creator_fee", "u64"),
    ("track_volume", "bool"), ("total_unclaimed_tokens", "u64"), ("total_claimed_tokens", "u64"),
    ("current_sol_volume", "u64"), ("last_update_timestamp", "i64"), ("ix_name", "string"),
    ("mayhem_mode", "bool"), ("cashback_fee_basis_points", "u64"), ("cashback", "u64"),
    ("buyback_fee_basis_points", "u64"), ("buyback_fee", "u64"),
    ("shareholders", ("vec", _SHAREHOLDER)), ("quote_mint", "pubkey"), ("quote_amount", "u64"),
    ("virtual_quote_reserves", "u64"), ("real_quote_reserves", "u64"), ("holder_rewards_bps", "u64"),
    ("holder_rewards", "u64"),
)
PUMP_SWAP_BUY_EVENT: tuple = (
    ("timestamp", "i64"), ("base_amount_out", "u64"), ("max_quote_amount_in", "u64"),
    ("user_base_token_reserves", "u64"), ("user_quote_token_reserves", "u64"),
    ("pool_base_token_reserves", "u64"), ("pool_quote_token_reserves", "u64"), ("quote_amount_in", "u64"),
    ("lp_fee_basis_points", "u64"), ("lp_fee", "u64"), ("protocol_fee_basis_points", "u64"),
    ("protocol_fee", "u64"), ("quote_amount_in_with_lp_fee", "u64"), ("user_quote_amount_in", "u64"),
    ("pool", "pubkey"), ("user", "pubkey"), ("user_base_token_account", "pubkey"),
    ("user_quote_token_account", "pubkey"), ("protocol_fee_recipient", "pubkey"),
    ("protocol_fee_recipient_token_account", "pubkey"), ("coin_creator", "pubkey"),
    ("coin_creator_fee_basis_points", "u64"), ("coin_creator_fee", "u64"),
    ("track_volume", "bool"), ("total_unclaimed_tokens", "u64"), ("total_claimed_tokens", "u64"),
    ("current_sol_volume", "u64"), ("last_update_timestamp", "i64"), ("min_base_amount_out", "u64"),
    ("ix_name", "string"), ("cashback_fee_basis_points", "u64"), ("cashback", "u64"),
    ("buyback_fee_basis_points", "u64"), ("buyback_fee", "u64"), ("virtual_quote_reserves", "i128"),
    ("can_boost", "bool"), ("base_supply", "u64"), ("holder_rewards_bps", "u64"), ("holder_rewards", "u64"),
)
PUMP_SWAP_SELL_EVENT: tuple = (
    ("timestamp", "i64"), ("base_amount_in", "u64"), ("min_quote_amount_out", "u64"),
    ("user_base_token_reserves", "u64"), ("user_quote_token_reserves", "u64"),
    ("pool_base_token_reserves", "u64"), ("pool_quote_token_reserves", "u64"), ("quote_amount_out", "u64"),
    ("lp_fee_basis_points", "u64"), ("lp_fee", "u64"), ("protocol_fee_basis_points", "u64"),
    ("protocol_fee", "u64"), ("quote_amount_out_without_lp_fee", "u64"), ("user_quote_amount_out", "u64"),
    ("pool", "pubkey"), ("user", "pubkey"), ("user_base_token_account", "pubkey"),
    ("user_quote_token_account", "pubkey"), ("protocol_fee_recipient", "pubkey"),
    ("protocol_fee_recipient_token_account", "pubkey"), ("coin_creator", "pubkey"),
    ("coin_creator_fee_basis_points", "u64"), ("coin_creator_fee", "u64"),
    ("cashback_fee_basis_points", "u64"), ("cashback", "u64"), ("buyback_fee_basis_points", "u64"),
    ("buyback_fee", "u64"), ("virtual_quote_reserves", "i128"), ("can_boost", "bool"),
    ("base_supply", "u64"), ("holder_rewards_bps", "u64"), ("holder_rewards", "u64"),
)
LAUNCHLAB_TRADE_EVENT: tuple = (
    ("pool_state", "pubkey"), ("total_base_sell", "u64"), ("virtual_base", "u64"), ("virtual_quote", "u64"),
    ("real_base_before", "u64"), ("real_quote_before", "u64"), ("real_base_after", "u64"),
    ("real_quote_after", "u64"), ("amount_in", "u64"), ("amount_out", "u64"), ("protocol_fee", "u64"),
    ("platform_fee", "u64"), ("creator_fee", "u64"), ("share_fee", "u64"),
    ("trade_direction", ("enum", ("Buy", "Sell"))), ("pool_status", ("enum", ("Fund", "Migrate", "Trade"))),
    ("exact_in", "bool"),
)


def event_discriminator(name: str) -> bytes:
    """Anchor's event discriminator: ``sha256("event:<Name>")[:8]``."""
    return hashlib.sha256(f"event:{name}".encode()).digest()[:8]


#: (emitting program, discriminator) -> (label, layout, required field count). Keyed by the
#: PROGRAM as well as the discriminator because pump.fun and LaunchLab both name their event
#: ``TradeEvent`` -- identical discriminators, different layouts.
VENUE_EVENTS: dict[tuple[str, bytes], tuple[str, tuple, int]] = {
    (PUMP_FUN_PROGRAM, event_discriminator("TradeEvent")): ("pump_fun.TradeEvent", PUMP_TRADE_EVENT, 16),
    (PUMP_SWAP_PROGRAM, event_discriminator("BuyEvent")): ("pump_swap.BuyEvent", PUMP_SWAP_BUY_EVENT, 23),
    (PUMP_SWAP_PROGRAM, event_discriminator("SellEvent")): ("pump_swap.SellEvent", PUMP_SWAP_SELL_EVENT, 22),
    (LAUNCHLAB_PROGRAM, event_discriminator("TradeEvent")): ("launchlab.TradeEvent", LAUNCHLAB_TRADE_EVENT, 17),
}

_MAX_STRING = 256
_MAX_VEC = 64


def _read_field(data: bytes, offset: int, kind) -> tuple[object, int]:
    """One Borsh value. Raises ``ValueError`` on anything short or malformed."""
    try:
        if kind == "pubkey":
            chunk = data[offset:offset + 32]
            if len(chunk) != 32:
                raise ValueError("short_pubkey")
            return _b58encode(chunk), offset + 32
        if kind == "u64":
            return struct.unpack_from("<Q", data, offset)[0], offset + 8
        if kind == "i64":
            return struct.unpack_from("<q", data, offset)[0], offset + 8
        if kind == "u16":
            return struct.unpack_from("<H", data, offset)[0], offset + 2
        if kind == "i128":
            chunk = data[offset:offset + 16]
            if len(chunk) != 16:
                raise ValueError("short_i128")
            return int.from_bytes(chunk, "little", signed=True), offset + 16
        if kind == "bool":
            if offset >= len(data) or data[offset] not in (0, 1):
                raise ValueError("invalid_bool")
            return data[offset] == 1, offset + 1
        if kind == "string":
            n = struct.unpack_from("<I", data, offset)[0]
            chunk = data[offset + 4:offset + 4 + n]
            if n > _MAX_STRING or len(chunk) != n:
                raise ValueError("invalid_string")
            return chunk.decode("utf-8"), offset + 4 + n
        if isinstance(kind, tuple) and kind[0] == "enum":
            if offset >= len(data) or data[offset] >= len(kind[1]):
                raise ValueError("invalid_enum")
            return kind[1][data[offset]], offset + 1
        if isinstance(kind, tuple) and kind[0] == "vec":
            n = struct.unpack_from("<I", data, offset)[0]
            if n > _MAX_VEC:
                raise ValueError("invalid_vec")
            offset += 4
            items = []
            for _ in range(n):
                item = {}
                for name, sub in kind[1]:
                    item[name], offset = _read_field(data, offset, sub)
                items.append(item)
            return items, offset
    except (struct.error, UnicodeDecodeError) as exc:
        raise ValueError("event_data_truncated") from exc
    raise ValueError(f"unsupported_field_kind:{kind!r}")


def decode_event_fields(data: bytes, layout: tuple, required: int) -> tuple[dict, int]:
    """Decode ``layout`` from ``data`` (discriminator already stripped).

    The first ``required`` fields must all be present. Later fields are decoded while bytes
    remain and dropped at the first one that does not fit, because programs append event
    fields over time and an older transaction carries fewer. Returns (fields, bytes used);
    bytes past the last known field are a newer program version, counted by the caller.
    """
    fields: dict = {}
    offset = 0
    for i, (name, kind) in enumerate(layout):
        if i >= required and offset >= len(data):
            break
        try:
            value, after = _read_field(data, offset, kind)
        except ValueError:
            if i < required:
                raise ValueError(f"required_event_field_unreadable:{name}") from None
            break
        fields[name] = value
        offset = after
    return fields, offset


def _is_program_id(text: str) -> bool:
    try:
        return len(_b58decode(text)) == 32
    except ValueError:
        return False


def emitting_programs(logs: list) -> list[str | None]:
    """Map each log line to the program whose invocation emitted it, or ``None``.

    ``Program data:`` names no program, so without this any program sharing the transaction
    can emit bytes that decode as a venue's event. The runtime brackets every invocation with
    ``Program <id> invoke [n]`` and ``Program <id> success|failed``; program-written lines are
    always prefixed ``Program log:`` / ``Program data:`` / ``Program return:``, so a frame
    cannot be forged -- the id position is required to be a real 32-byte key, which
    ``log:``/``data:`` are not. Adapted from chainstacklabs/pumpfun-bonkfun-bot
    ``PumpFunEventParser._emitting_program`` (Apache-2.0), tightened on that one point.
    """
    emitters: list[str | None] = []
    stack: list[str] = []
    for line in logs:
        parts = line.split(" ") if isinstance(line, str) else []
        is_frame = len(parts) >= 3 and parts[0] == "Program" and _is_program_id(parts[1])
        if is_frame and parts[2] == "invoke":
            stack.append(parts[1])
            emitters.append(None)
            continue
        if is_frame and parts[2] in ("success", "failed"):
            emitters.append(None)
            if stack and stack[-1] == parts[1]:
                stack.pop()
            continue
        emitters.append(stack[-1] if stack else None)
    return emitters


def _decode_payload(program: str, raw: bytes, path: str, source: str) -> dict | None:
    spec = VENUE_EVENTS.get((program, raw[:8]))
    if spec is None:
        return None
    label, layout, required = spec
    try:
        fields, used = decode_event_fields(raw[8:], layout, required)
    except ValueError as exc:
        return {"program": program, "venue": VENUE_PROGRAMS[program], "event": label, "error": str(exc),
                "source": source, "path": path}
    return {
        "program": program, "venue": VENUE_PROGRAMS[program], "event": label, "fields": fields,
        "trailing_bytes": len(raw) - 8 - used, "source": source, "path": path,
        "payload_sha256": hashlib.sha256(raw).hexdigest(),
    }


def find_venue_events(result: dict) -> tuple[list[dict], list[str]]:
    """Every venue trade event in a ``getTransaction`` result, attributed to its emitter.

    Two carriers. (1) Event CPI: an inner instruction to the venue program whose data starts
    with :data:`EVENT_CPI_TAG` and whose only account is that program's event authority.
    (2) ``Program data:`` log lines, kept only when the invoke-stack says the venue program
    itself was executing. CPI events win when both exist (logs can be truncated); the two
    are compared and a disagreement is reported. A ``Program data:`` line written by any
    other program is ignored, whatever it decodes to.
    """
    meta = _mapping(result.get("meta"))
    notes: list[str] = []
    cpi: list[dict] = []
    for g, group in enumerate(meta.get("innerInstructions") or []):
        for k, ix in enumerate(_mapping(group).get("instructions") or []):
            program = ix.get("programId") if isinstance(ix, dict) else None
            if program not in VENUE_PROGRAMS or not isinstance(ix.get("data"), str):
                continue
            try:
                raw = _b58decode(ix["data"])
            except ValueError:
                continue
            if raw[:8] != EVENT_CPI_TAG:
                continue
            if ix.get("accounts") != [EVENT_AUTHORITY[program]]:
                notes.append("event_cpi_authority_mismatch")
                continue
            event = _decode_payload(program, raw[8:], f"meta.innerInstructions[{g}].instructions[{k}]", "event_cpi")
            if event is not None:
                cpi.append(event)
    logged: list[dict] = []
    logs = meta.get("logMessages")
    if isinstance(logs, list):
        if any(isinstance(line, str) and "Log truncated" in line for line in logs):
            notes.append("logs_truncated")
        emitters = emitting_programs(logs)
        for i, line in enumerate(logs):
            if not isinstance(line, str) or not line.startswith("Program data: "):
                continue
            program = emitters[i]
            if program not in VENUE_PROGRAMS:
                continue
            try:
                raw = base64.b64decode(line[len("Program data: "):], validate=True)
            except ValueError:
                continue
            event = _decode_payload(program, raw, f"meta.logMessages[{i}]", "program_log")
            if event is not None:
                logged.append(event)
    if cpi:
        if logged and sorted(e.get("payload_sha256", "") for e in logged) != sorted(
                e.get("payload_sha256", "") for e in cpi):
            notes.append("log_and_cpi_events_disagree")
        return cpi, notes
    return logged, notes


# --- instruction walk, wallet flows ---------------------------------------------------


def _walk_instructions(message: dict, meta: dict):
    """Yield ``(path, instruction, invoking_programs)`` for every instruction.

    ``invoking_programs`` is the chain of programs above the instruction (``()`` for a
    top-level one), rebuilt from ``stackHeight``; ``None`` when the RPC gave no height.
    """
    top = message.get("instructions") or []
    for i, ix in enumerate(top):
        yield f"transaction.message.instructions[{i}]", ix, ()
    for g, group in enumerate(meta.get("innerInstructions") or []):
        i = group.get("index")
        if type(i) is not int or not 0 <= i < len(top):
            continue
        stack: list | None = [top[i].get("programId")]
        for k, ix in enumerate(group.get("instructions") or []):
            path = f"meta.innerInstructions[{g}].instructions[{k}]"
            h = ix.get("stackHeight") if isinstance(ix, dict) else None
            if stack is None or type(h) is not int or not 2 <= h <= len(stack) + 1:
                stack = None
                yield path, ix, None
                continue
            parents = tuple(stack[:h - 1])
            stack = stack[:h - 1] + [ix.get("programId")]
            yield path, ix, parents


def _wallet_accounts(message: dict, meta: dict, keys: list[str], wallet: str) -> tuple[set[str], dict[str, str]]:
    """Token accounts the wallet owns in this transaction, and every known account's mint.

    From the balance rows (owner + mint) AND from parsed ATA-create / initialize / close
    instructions, because a temporary WSOL account created and closed inside the transaction
    has no balance row at all and would otherwise read as the wallet paying a stranger.
    """
    owned: set[str] = set()
    mints: dict[str, str] = {}
    for rows in (meta.get("preTokenBalances") or [], meta.get("postTokenBalances") or []):
        for row in rows:
            i = row.get("accountIndex")
            if type(i) is not int or not 0 <= i < len(keys):
                continue
            if isinstance(row.get("mint"), str):
                mints[keys[i]] = row["mint"]
            if row.get("owner") == wallet:
                owned.add(keys[i])
    for _, ix, _parents in _walk_instructions(message, meta):
        parsed = ix.get("parsed") if isinstance(ix, dict) else None
        info = parsed.get("info") if isinstance(parsed, dict) else None
        if not isinstance(info, dict):
            continue
        kind = parsed.get("type")
        account = info.get("account") or info.get("newAccount")
        if kind in ("create", "createIdempotent") and info.get("wallet") == wallet:
            owned.add(account)
        elif kind in ("initializeAccount", "initializeAccount2", "initializeAccount3") and info.get("owner") == wallet:
            owned.add(account)
        elif kind == "closeAccount" and info.get("owner") == wallet:
            owned.add(account)
        if isinstance(account, str) and isinstance(info.get("mint"), str):
            mints.setdefault(account, info["mint"])
    owned.discard(None)
    owned.discard(wallet)
    return owned, mints


def _token_delta(meta: dict, keys: list[str], owned: set[str], mint: str) -> int:
    pre: dict[int, int] = {}
    post: dict[int, int] = {}
    for rows, into in ((meta.get("preTokenBalances") or [], pre), (meta.get("postTokenBalances") or [], post)):
        for row in rows:
            i = row.get("accountIndex")
            if type(i) is int and 0 <= i < len(keys) and keys[i] in owned and row.get("mint") == mint:
                into[i] = _uint(_mapping(row.get("uiTokenAmount")).get("amount"))
    return sum(post.get(i, 0) - pre.get(i, 0) for i in pre.keys() | post.keys())


def _router_transfers(message: dict, meta: dict, wallet: str, owned: set[str]) -> tuple[list[dict], int, bool]:
    """System transfers out of the wallet that the venue did NOT make.

    Returns (items, lamports moved by the venue itself, attributable). A transfer into the
    wallet's own token account (wrapping SOL) is internal and skipped; one made inside a
    venue program's invocation is the venue leg; everything else -- GMGN's fee, a relay tip --
    is the router's take. ``attributable`` is False when an inner transfer had no stack height.
    """
    items: list[dict] = []
    venue_lamports = 0
    attributable = True
    for path, ix, parents in _walk_instructions(message, meta):
        if not isinstance(ix, dict) or ix.get("programId") != SYSTEM_PROGRAM:
            continue
        parsed = ix.get("parsed")
        if not isinstance(parsed, dict) or parsed.get("type") not in ("transfer", "transferWithSeed"):
            continue
        info = _mapping(parsed.get("info"))
        if info.get("source") != wallet:
            continue
        lamports = _uint(info.get("lamports"))
        destination = info.get("destination")
        if destination in owned:
            continue
        if parents is None:
            attributable = False
            items.append({"destination": destination, "lamports": str(lamports), "invoked_by": None, "path": path})
        elif any(p in VENUE_PROGRAMS for p in parents):
            venue_lamports += lamports
        else:
            items.append({"destination": destination, "lamports": str(lamports),
                          "invoked_by": parents[-1] if parents else "transaction", "path": path})
    return items, venue_lamports, attributable


# --- per-venue terms -------------------------------------------------------------------
#
# Each returns the venue's own account of the trade in native atoms:
#   tokens  -- token atoms the venue moved for the user
#   swap    -- native atoms through the price curve, fees excluded
#   user    -- native atoms the user paid into (buy) / received from (sell) the venue
#   fees    -- {component: native atoms} the venue charged the user
#   spot    -- native atoms per token atom immediately BEFORE the trade, or None
# plus the basis of ``spot`` and informational splits (parts OF the fees above, never added).

_K_TOLERANCE = Decimal("1e-7")


def _pump_fun_terms(f: dict, side: str) -> dict:
    quote = f.get("quote_mint")
    if quote is not None and quote not in (WSOL_MINT, DEFAULT_PUBKEY):
        raise ValueError("non_native_quote_mint")
    sol, tok = f["sol_amount"], f["token_amount"]
    fees = {"fee": f["fee"], "creator_fee": f["creator_fee"]}
    vs, vt = f["virtual_sol_reserves"], f["virtual_token_reserves"]
    # MEASURED 2026-10-04: these reserves are POST-trade (on a live sell, (vs+sol)*(vt-tok)
    # matched vs*vt to 5e-11, the pre-trade reading missed by 2e-5). Both readings are tested
    # on every fill, and the spot is used only when exactly one holds.
    if side == "buy":
        pre, post_if_pre = (vs - sol, vt + tok), (vs + sol, vt - tok)
    else:
        pre, post_if_pre = (vs + sol, vt - tok), (vs - sol, vt + tok)
    spot, basis = None, "reserve_semantics_unverified"
    if vs > 0 and vt > 0 and min(pre + post_if_pre) > 0:
        with localcontext(_CTX):
            k = Decimal(vs) * vt
            err_post = abs(Decimal(pre[0]) * pre[1] / k - 1)
            err_pre = abs(Decimal(post_if_pre[0]) * post_if_pre[1] / k - 1)
            if err_post <= _K_TOLERANCE < err_pre:
                spot, basis = Decimal(pre[0]) / pre[1], "event_reserves_post_trade_verified"
            elif err_pre <= _K_TOLERANCE < err_post:
                spot, basis = Decimal(vs) / vt, "event_reserves_pre_trade_verified"
    user = sol + sum(fees.values()) if side == "buy" else sol - sum(fees.values())
    splits = {k: f[k] for k in ("buyback_fee", "cashback", "holder_rewards") if f.get(k)}
    notes = ["cashback_nonzero_unverified"] if f.get("cashback") else []
    return {"tokens": tok, "swap": sol, "user": user, "fees": fees, "spot": spot, "spot_basis": basis,
            "splits": splits, "notes": notes}


def _pump_swap_terms(f: dict, side: str, mints: dict[str, str]) -> dict:
    if mints.get(f["user_quote_token_account"]) != WSOL_MINT:
        raise ValueError("quote_mint_not_wsol_or_unproved")
    fees = {"lp_fee": f["lp_fee"], "protocol_fee": f["protocol_fee"], "coin_creator_fee": f["coin_creator_fee"]}
    fee_total = sum(fees.values())
    base, quote = f["pool_base_token_reserves"], f["pool_quote_token_reserves"]
    # MEASURED 2026-10-04: the event's pool reserves are PRE-trade and the curve quotes off
    # pool_quote + virtual_quote_reserves: sells reproduced quote_amount_out to the atom on
    # 3/3 fills, buys base_amount_out within 3e-9 on 4/4. Without the virtual term every
    # fill missed by 1-20%.
    quote_eff = quote + (f.get("virtual_quote_reserves") or 0)
    notes: list[str] = []
    if side == "buy":
        tok = f["base_amount_out"]
        a, b = f["quote_amount_in"], f["user_quote_amount_in"]
        # buy_exact_quote_in reports the total as quote_amount_in and the curve input as
        # user_quote_amount_in; the identity decides which is which, never the field name.
        if a - b == fee_total:
            user, swap = a, b
        elif b - a == fee_total:
            user, swap = b, a
        else:
            user, swap = max(a, b), min(a, b)
            notes.append("pump_swap_fee_identity_failed")
        predicted = base * swap // (quote_eff + swap) if quote_eff + swap > 0 else -1
        ok = abs(predicted - tok) <= max(1, tok // 1_000_000)
    else:
        tok = f["base_amount_in"]
        swap, user = f["quote_amount_out"], f["user_quote_amount_out"]
        if swap - user != fee_total:
            notes.append("pump_swap_fee_identity_failed")
        predicted = quote_eff * tok // (base + tok) if base + tok > 0 else -1
        ok = abs(predicted - swap) <= max(1, swap // 1_000_000)
    spot = None
    basis = "reserve_semantics_unverified"
    if ok and base > 0 and not notes:
        with localcontext(_CTX):
            spot = Decimal(quote_eff) / base
        basis = "event_reserves_pre_trade_verified"
    splits = {k: f[k] for k in ("buyback_fee", "cashback", "holder_rewards") if f.get(k)}
    if f.get("cashback"):
        notes.append("cashback_nonzero_unverified")
    return {"tokens": tok, "swap": swap, "user": user, "fees": fees, "spot": spot, "spot_basis": basis,
            "splits": splits, "notes": notes}


def _launchlab_terms(f: dict, side: str) -> dict:
    """Raydium LaunchLab. Synthetic-tested only: no live fill of ours used it as of 2026-10-04."""
    fees = {k: f[k] for k in ("protocol_fee", "platform_fee", "creator_fee", "share_fee")}
    fee_total = sum(fees.values())
    base_res = f["virtual_base"] - f["real_base_before"]
    quote_res = f["virtual_quote"] + f["real_quote_before"]
    notes = ["launchlab_quote_mint_proved_only_by_reconciliation"]
    if side == "buy":
        tok, swap = f["amount_out"], f["real_quote_after"] - f["real_quote_before"]
        user = swap + fee_total
        if user != f["amount_in"]:
            notes.append("launchlab_fee_identity_failed")
        predicted = base_res * swap // (quote_res + swap) if quote_res + swap > 0 else -1
        ok = swap > 0 and abs(predicted - tok) <= max(1, tok // 1_000_000)
    else:
        tok, swap = f["amount_in"], f["real_quote_before"] - f["real_quote_after"]
        user = swap - fee_total
        if user != f["amount_out"]:
            notes.append("launchlab_fee_identity_failed")
        predicted = quote_res * tok // (base_res + tok) if base_res + tok > 0 else -1
        ok = swap > 0 and abs(predicted - swap) <= max(1, swap // 1_000_000)
    spot, basis = None, "reserve_semantics_unverified_or_not_constant_product"
    if ok and base_res > 0 and "launchlab_fee_identity_failed" not in notes:
        with localcontext(_CTX):
            spot = Decimal(quote_res) / base_res
        basis = "event_reserves_pre_trade_verified"
    return {"tokens": tok, "swap": swap, "user": user, "fees": fees, "spot": spot, "spot_basis": basis,
            "splits": {}, "notes": notes}


# --- shared arithmetic -------------------------------------------------------------------


def _dstr(value: Decimal | None) -> str | None:
    if value is None:
        return None
    with localcontext(Context(prec=28, rounding=ROUND_HALF_EVEN)):
        return format(+value, "f") if abs(value) >= Decimal("1e-6") else format(+value, "E")


def _bps(value: Decimal | None) -> int | None:
    if value is None:
        return None
    with localcontext(_CTX):
        return int((value * _TEN_K).to_integral_value(rounding=ROUND_HALF_EVEN))


def _ratio(a, b) -> Decimal | None:
    if a is None or b is None or b == 0:
        return None
    with localcontext(_CTX):
        return Decimal(a) / Decimal(b)


def decision_reference(*, side: str, amount_in, min_out, slippage_bps) -> tuple[Decimal | None, str]:
    """Native atoms per token atom the order was PLANNED at, from the order row alone.

    Every live order is built as ``min_out = floor(expected * (10000 - slippage_bps) / 10000)``
    -- by ``engine._min_out_atoms`` from the dossier price on a buy, by the watchdog's
    ``_min_out`` from the exit quote on a sell, by ``copy_manager.min_out_units`` likewise. So
    ``expected`` and with it the decision-time price are recoverable to well under one atom.
    """
    try:
        amount_in, min_out, slippage_bps = int(amount_in), int(min_out), int(slippage_bps)
    except (TypeError, ValueError):
        return None, "order_fields_unreadable"
    if amount_in <= 0:
        return None, "amount_in_not_positive"
    if min_out <= 1:
        # watchdog clamps a rounded-away exit floor to 1; that floor encodes no price
        return None, "min_out_clamped_no_price"
    if not 0 <= slippage_bps < 10_000:
        return None, "slippage_band_out_of_range"
    with localcontext(_CTX):
        expected = Decimal(min_out) * _TEN_K / (_TEN_K - slippage_bps)
        price = Decimal(amount_in) / expected if side == "buy" else expected / Decimal(amount_in)
    return price, "min_out_over_slippage_band"


def _signed_change(exec_price: Decimal | None, reference: Decimal | None) -> Decimal | None:
    """``executed / reference - 1``: positive on a buy is paying more, negative on a sell is
    receiving less. Both are what a reader of a cost has to sign-adjust by side."""
    if exec_price is None or reference is None or reference == 0:
        return None
    with localcontext(_CTX):
        return exec_price / reference - 1


def _cost_blank(chain: str, unit: str) -> dict:
    return {
        "version": VERSION, "chain": chain, "native_unit": unit, "basis": None, "venue": None, "event": None,
        "event_source": None, "programs": [], "block_time_ms": None, "wallet_bound": False,
        "native_leg": None, "swap_native": None, "token_leg": None, "wallet_token_delta": None,
        "wallet_native_delta": None, "venue_fees": {}, "venue_fee_splits": {}, "venue_fee_native": None,
        "network_fee_native": None, "priority_fee_native": None, "router_transfers": [], "router_fee_native": None,
        "spot_pre": None, "spot_basis": None, "exec_price": None, "price_impact_bps": None,
        "curve_impact_bps": None, "impact_cost_native": None, "allin_price": None,
        "total_cost_native": None, "total_cost_bps": None, "residual_native": None, "reconciled": False,
        "notes": [],
    }


def _cost_refuse(out: dict, reason: str, basis: str = "decode_refused") -> dict:
    out["basis"] = basis
    out["notes"].append(reason)
    return out


def _finish_costs(out: dict, *, side: str, terms: dict, wallet_tokens: int, outflow: int | None,
                  network: int | None, router: int | None, extra_fees: dict[str, Decimal] | None = None) -> None:
    """Fill the price, impact, total-cost and residual fields from a venue's terms.

    ``outflow`` is the wallet's all-in native movement, positive = paid: on a buy the native
    it gave up (fees, tips and the network fee included, recoverable rent excluded); on a
    sell, minus what it ended up with. ``extra_fees`` are venue fees charged in TOKEN units,
    already converted to native at the pre-trade spot.
    """
    spot: Decimal | None = terms["spot"]
    tokens, swap, user = terms["tokens"], terms["swap"], terms["user"]
    fees = dict(terms["fees"])
    out["native_leg"], out["swap_native"], out["token_leg"] = str(user), str(swap), str(tokens)
    out["venue_fees"] = {k: str(v) for k, v in fees.items()}
    out["venue_fee_splits"] = {k: str(v) for k, v in terms.get("splits", {}).items()}
    out["spot_basis"] = terms["spot_basis"]
    out["notes"].extend(terms.get("notes", []))
    with localcontext(_CTX):
        fee_native = Decimal(sum(fees.values())) + sum((extra_fees or {}).values(), Decimal(0))
        out["venue_fee_native"] = _dstr(fee_native)
        if extra_fees:
            out["venue_fees"].update({k: _dstr(v) for k, v in extra_fees.items()})
        held = abs(wallet_tokens)
        out["exec_price"] = _dstr(_ratio(user, held)) if held else None
        out["spot_pre"] = _dstr(spot)
        if spot is None or spot <= 0 or tokens <= 0 or not held:
            return
        value_event = Decimal(tokens) * spot
        out["price_impact_bps"] = _bps(_signed_change(Decimal(user) / held, spot))
        out["curve_impact_bps"] = _bps(_signed_change(Decimal(swap) / tokens, spot))
        impact = (Decimal(swap) - value_event) if side == "buy" else (value_event - Decimal(swap))
        out["impact_cost_native"] = _dstr(impact)
        if outflow is None or network is None:
            return
        value_wallet = Decimal(held) * spot
        total = (Decimal(outflow) - value_wallet) if side == "buy" else (value_wallet - Decimal(-outflow))
        out["total_cost_native"] = _dstr(total)
        out["total_cost_bps"] = _bps(total / value_wallet)
        if router is None:
            return
        # Tokens the venue moved but the wallet did not end up with (a transfer tax) are a
        # cost too; valued at the same pre-trade spot.
        token_fees = terms.get("token_fee_atoms", 0)
        shortfall = (Decimal(tokens - token_fees - held) * spot) if side == "buy" else (Decimal(held - tokens) * spot)
        explained = fee_native + impact + network + router + shortfall
        residual = total - explained
        out["residual_native"] = _dstr(residual)
        if shortfall:
            out["token_shortfall_native"] = _dstr(shortfall)
        # Two atoms of rounding per integer step in the venue's arithmetic, at most.
        out["reconciled"] = abs(residual) <= max(Decimal(10), value_wallet * Decimal("1e-6"))


# --- Solana ------------------------------------------------------------------------------


def decode_solana_fill(result: dict, *, token: str, side: str, signature: str | None = None) -> dict:
    """Decompose one Solana fill from its ``getTransaction`` (jsonParsed) result.

    The wallet is the fee payer (``accountKeys[0]``, a signer); every venue event used must
    name it as ``user`` and the order's mint (LaunchLab carries no user, so its token amount
    must equal the wallet's own token change instead). Never raises.
    """
    out = _cost_blank("sol", "lamport")
    try:
        if side not in ("buy", "sell"):
            raise ValueError("invalid_side")
        r = _mapping(result)
        meta = _mapping(r.get("meta"))
        if meta.get("err") is not None:
            raise ValueError("transaction_not_successful")
        tx = _mapping(r.get("transaction"))
        message = _mapping(tx.get("message"))
        signatures = _sequence(tx.get("signatures"))
        if signature is not None and (not signatures or signatures[0] != signature):
            raise ValueError("transaction_signature_mismatch")
        entries = [_mapping(e) for e in _sequence(message.get("accountKeys"))]
        keys = [e.get("pubkey") for e in entries]
        if not entries or entries[0].get("signer") is not True or not all(isinstance(k, str) for k in keys):
            raise ValueError("account_keys_not_json_parsed_or_payer_not_signer")
        wallet = keys[0]
        pre = [_uint(n) for n in _sequence(meta.get("preBalances"))]
        post = [_uint(n) for n in _sequence(meta.get("postBalances"))]
        if len(pre) != len(keys) or len(post) != len(keys):
            raise ValueError("native_balance_account_count_mismatch")
        fee = _uint(meta.get("fee"))
    except (KeyError, TypeError, ValueError) as exc:
        return _cost_refuse(out, str(exc))
    if type(r.get("blockTime")) is int:
        out["block_time_ms"] = r["blockTime"] * 1000
    out["programs"] = sorted({ix.get("programId") for _, ix, _p in _walk_instructions(message, meta)
                              if isinstance(ix, dict) and isinstance(ix.get("programId"), str)})
    out["network_fee_native"] = str(fee)
    out["priority_fee_native"] = str(max(0, fee - LAMPORTS_PER_SIGNATURE * len(signatures)))
    try:
        owned, mints = _wallet_accounts(message, meta, keys, wallet)
        index = {k: i for i, k in enumerate(keys)}
        # The wallet's economic native change: its own lamports plus those of every token
        # account it owns. Rent locked into a new token account stays the wallet's (it comes
        # back on close), and a WSOL account opened and closed in the tx nets to zero.
        econ = post[0] - pre[0] + sum(post[index[a]] - pre[index[a]] for a in owned if a in index)
        wallet_tokens = _token_delta(meta, keys, owned, token)
        out["wallet_native_delta"] = str(econ)
        out["wallet_token_delta"] = str(wallet_tokens)
        items, venue_lamports, attributable = _router_transfers(message, meta, wallet, owned)
    except (KeyError, TypeError, ValueError) as exc:
        return _cost_refuse(out, "wallet_flows_unproved: " + str(exc))
    if wallet_tokens:
        with localcontext(_CTX):
            out["allin_price"] = _dstr(Decimal(abs(econ)) / abs(wallet_tokens))
    try:
        events, notes = find_venue_events(r)
    except (KeyError, TypeError, ValueError) as exc:
        return _cost_refuse(out, "venue_events_unreadable: " + str(exc))
    out["notes"].extend(notes)
    out["venue_events_seen"] = len(events)

    def _matches(ev: dict) -> bool:
        f = ev.get("fields")
        if not isinstance(f, dict):
            return False
        if ev["venue"] == "pump_fun":
            return f["mint"] == token and f["user"] == wallet and f["is_buy"] == (side == "buy")
        if ev["venue"] == "pump_swap":
            want = "pump_swap.BuyEvent" if side == "buy" else "pump_swap.SellEvent"
            return (ev["event"] == want and f["user"] == wallet
                    and mints.get(f["user_base_token_account"]) == token)
        if ev["venue"] == "launchlab":
            amount = f["amount_out"] if side == "buy" else f["amount_in"]
            return f["trade_direction"] == ("Buy" if side == "buy" else "Sell") and amount == abs(wallet_tokens)
        return False

    matched = [e for e in events if _matches(e)]
    undecodable = [e for e in events if "error" in e]
    if undecodable:
        out["notes"].append("venue_event_undecodable: " + undecodable[0]["error"])
    if not matched:
        out["basis"] = "no_venue_event"
        if events:
            out["notes"].append("venue_events_present_but_not_this_wallet_or_token")
        return out
    if len(matched) > 1:
        out["basis"] = "multiple_venue_events"
        out["venue"] = ",".join(sorted({e["venue"] for e in matched}))
        return out
    ev = matched[0]
    out["venue"], out["event"], out["event_source"] = ev["venue"], ev["event"], ev["source"]
    out["wallet_bound"] = ev["venue"] != "launchlab"
    if ev.get("trailing_bytes"):
        out["notes"].append(f"event_has_{ev['trailing_bytes']}_bytes_beyond_known_layout")
    try:
        if ev["venue"] == "pump_fun":
            terms = _pump_fun_terms(ev["fields"], side)
        elif ev["venue"] == "pump_swap":
            terms = _pump_swap_terms(ev["fields"], side, mints)
        else:
            terms = _launchlab_terms(ev["fields"], side)
    except (KeyError, TypeError, ValueError) as exc:
        return _cost_refuse(out, "venue_terms_unproved: " + str(exc), basis="venue_event_unpriced")
    out["basis"] = "venue_event"
    if ev["venue"] == "pump_fun" and side == "buy" and venue_lamports and venue_lamports != terms["user"]:
        # pump.fun takes a buyer's SOL by system transfers inside its own instruction, so the
        # venue leg is observable independently of the event; say when they differ.
        out["notes"].append(f"pump_fun_user_leg_mismatch:event={terms['user']},transfers={venue_lamports}")
    router = sum(int(i["lamports"]) for i in items) if attributable else None
    out["router_transfers"] = items
    out["router_fee_native"] = str(router) if router is not None else None
    _finish_costs(out, side=side, terms=terms, wallet_tokens=wallet_tokens, outflow=-econ, network=fee,
                  router=router)
    return out


# --- EVM (Robinhood Chain) ---------------------------------------------------------------

TOPIC_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
#: keccak of ``Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)`` -- v4 PoolManager
TOPIC_V4_SWAP = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
#: keccak of ``Swap(address,address,int256,int256,uint160,uint128,int24)`` -- a v3 pool
TOPIC_V3_SWAP = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
#: keccak of ``Swap(address,uint256,uint256,uint256,uint256,address)`` -- a v2 pair
TOPIC_V2_SWAP = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"
#: Pons ``CurveBuy(trader, recipient, quoteIn, tokensOut, fee, tax)`` / ``CurveSell(trader,
#: recipient, tokensIn, quoteOut, fee, tax)``, emitted by the token's own curve.
TOPIC_PONS_CURVE_BUY = "0xec36bf571f136799e8dc0b0b8bea4b04d8bd3d43de838aab0d5fc21d4cbfc455"
TOPIC_PONS_CURVE_SELL = "0x8113d738abdcb6b38357e9d53a54a7157861a09031b453651f0fe7fe151f59df"
#: Pons ``HookFeeCollected(bytes32 indexed poolId, address currency, uint256 feeAmount,
#: uint256 taxAmount)`` on the graduated-pool hook.
TOPIC_PONS_HOOK_FEE = "0xc532c43b3423e14ef72748f1c8291238829ca0af8ba9b67975ad1483485a4b4d"
#: GMGN router events, MEASURED 2026-10-04 on 5/5 Robinhood fills, emitted by the router the
#: transaction called. Fee: topics (sig, 0, user), data [fee_wei, deadline]; the fee is
#: exactly 1% of the order's amount_in on a buy and of the unwrapped proceeds on a sell.
#: Swap: topics (sig, user, recipient, 0), data [amount_in, amount_out, ...] -- equal to the
#: order row's amount_in and filled_out on all 5.
TOPIC_GMGN_FEE = "0x205442d60b70af1203d43cab62352c3b69b94f091be32fe683198057282b5c92"
TOPIC_GMGN_SWAP = "0x8619026a40d38bedb4002fe511cea4bc4a9b336710efe8f21a61869a7ee0f02a"
RH_POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
RH_PONS_MEME_HOOK = "0xe5e702641ea86f4ae6cc3cdaed2b886f976be044"
RH_WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
EVM_ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
GMGN_ROUTERS: dict[str, frozenset[str]] = {
    "robinhood": frozenset({"0x65050a9b7e5075a2ba5ced7b1b64ee66262c40dc"}),
}
NATIVE_WRAPPED: dict[str, str] = {"robinhood": RH_WETH}
_Q96 = Decimal(2) ** 96


def _hexint(value) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{1,64}", value):
        raise ValueError("malformed_hex_quantity")
    return int(value, 16)


def _words(data) -> list[int]:
    if not isinstance(data, str) or not re.fullmatch(r"0x(?:[0-9a-fA-F]{64})*", data):
        raise ValueError("malformed_log_data")
    body = data[2:]
    return [int(body[i:i + 64], 16) for i in range(0, len(body), 64)]


def _signed256(word: int) -> int:
    return word - (1 << 256) if word >> 255 else word


def _topic_address(topic: str) -> str:
    return "0x" + topic[-40:].lower()


def _logs(receipt: dict) -> list[dict]:
    answer = []
    for lg in _sequence(receipt.get("logs")):
        lg = _mapping(lg)
        topics = [t.lower() for t in _sequence(lg.get("topics"))]
        if not topics:
            continue
        answer.append({"address": str(lg.get("address", "")).lower(), "topics": topics, "data": lg.get("data", "0x"),
                       "index": lg.get("logIndex")})
    return answer


def _clmm_spot(*, sqrt_post_x96: int, liquidity: int, out_amount: int, out_is_token1: bool,
               in_amount: int, token_is_token1: bool) -> tuple[Decimal | None, Decimal | None]:
    """Pre-trade price of a concentrated-liquidity pool from its post-swap state.

    Within one tick range the pool is ``x = L/s``, ``y = L*s`` with ``s = sqrt(P)``. The
    OUTPUT side carries no fee, so it fixes the pre-trade ``s`` exactly; the input side then
    implies the fee that was charged. If a tick was crossed ``L`` changed mid-swap, the
    implied fee comes out wrong, and the caller refuses the spot. Returns (native atoms per
    token atom before the trade, implied fee fraction).
    """
    if liquidity <= 0 or sqrt_post_x96 <= 0 or out_amount <= 0 or in_amount <= 0:
        return None, None
    with localcontext(_CTX):
        L = Decimal(liquidity)
        s_post = Decimal(sqrt_post_x96) / _Q96
        if out_is_token1:      # token0 in, token1 out: s falls
            s_pre = s_post + Decimal(out_amount) / L
            in_net = L * (1 / s_post - 1 / s_pre)
        else:                  # token1 in, token0 out: s rises
            inv_pre = 1 / s_post + Decimal(out_amount) / L
            s_pre = 1 / inv_pre
            in_net = L * (s_post - s_pre)
        implied_fee = 1 - in_net / Decimal(in_amount)
        price1_per_0 = s_pre * s_pre           # token1 atoms per token0 atom
        spot = 1 / price1_per_0 if token_is_token1 else price1_per_0
    return spot, implied_fee


def decode_evm_fill(receipt: dict, *, token: str, side: str, chain: str = "robinhood",
                    signature: str | None = None) -> dict:
    """Decompose one EVM fill from its receipt. Never raises.

    Single-hop fills through the v4 PoolManager (with the Pons hook's fee and tax), a v3 pool
    or a Pons curve are priced; a route through more than one pool is ``multi_hop_route``
    and keeps only the router fee, gas and the all-in price. The wallet is ``receipt.from``.
    """
    out = _cost_blank(chain, "wei")
    try:
        if side not in ("buy", "sell"):
            raise ValueError("invalid_side")
        receipt = _mapping(receipt)
        if signature is not None and str(receipt.get("transactionHash", "")).lower() != signature.lower():
            raise ValueError("transaction_hash_mismatch")
        if _hexint(receipt.get("status")) != 1:
            raise ValueError("transaction_not_successful")
        wallet = str(receipt.get("from", "")).lower()
        if not re.fullmatch(r"0x[0-9a-f]{40}", wallet):
            raise ValueError("malformed_sender")
        to = str(receipt.get("to") or "").lower()
        gas = _hexint(receipt.get("gasUsed")) * _hexint(receipt.get("effectiveGasPrice"))
        token = token.lower()
        logs = _logs(receipt)
        for lg in logs:
            _words(lg["data"])
    except (KeyError, TypeError, ValueError) as exc:
        return _cost_refuse(out, str(exc))
    out["block_time_ms"] = None
    out["network_fee_native"] = str(gas)
    out["programs"] = sorted({lg["address"] for lg in logs if lg["topics"][0] in (
        TOPIC_V4_SWAP, TOPIC_V3_SWAP, TOPIC_V2_SWAP, TOPIC_PONS_CURVE_BUY, TOPIC_PONS_CURVE_SELL,
        TOPIC_GMGN_SWAP)} | ({to} if to else set()))
    # GMGN's own fee and swap summary, only from the router the transaction called.
    gmgn_fee = gmgn_in = gmgn_out = None
    if to in GMGN_ROUTERS.get(chain, frozenset()):
        fees = [lg for lg in logs if lg["address"] == to and lg["topics"][0] == TOPIC_GMGN_FEE
                and len(lg["topics"]) >= 3 and _topic_address(lg["topics"][2]) == wallet]
        swaps = [lg for lg in logs if lg["address"] == to and lg["topics"][0] == TOPIC_GMGN_SWAP
                 and len(lg["topics"]) >= 2 and _topic_address(lg["topics"][1]) == wallet]
        if len(fees) == 1:
            gmgn_fee = _words(fees[0]["data"])[0]
        if len(swaps) == 1 and len(_words(swaps[0]["data"])) >= 2:
            gmgn_in, gmgn_out = _words(swaps[0]["data"])[:2]
    out["router_fee_native"] = str(gmgn_fee) if gmgn_fee is not None else None
    if gmgn_fee is not None:
        out["router_transfers"] = [{"destination": to, "wei": str(gmgn_fee), "basis": "gmgn_router_fee_event"}]
    transfers = []
    for lg in logs:
        if lg["address"] == token and lg["topics"][0] == TOPIC_TRANSFER and len(lg["topics"]) == 3:
            transfers.append((_topic_address(lg["topics"][1]), _topic_address(lg["topics"][2]), _words(lg["data"])[0]))
    wallet_tokens = sum(a for _, dst, a in transfers if dst == wallet) - sum(a for src, _, a in transfers if src == wallet)
    out["wallet_token_delta"] = str(wallet_tokens)
    # All-in native movement from GMGN's own summary, which equals the order row; gas on top.
    outflow = None
    if gmgn_in is not None and gmgn_out is not None:
        outflow = (gmgn_in + gas) if side == "buy" else (gas - gmgn_out)
        out["wallet_native_delta"] = str(-outflow)
        if wallet_tokens:
            with localcontext(_CTX):
                out["allin_price"] = _dstr(Decimal(abs(outflow)) / abs(wallet_tokens))
    venue_logs = [lg for lg in logs if (lg["topics"][0] == TOPIC_V4_SWAP and lg["address"] == RH_POOL_MANAGER)
                  or lg["topics"][0] in (TOPIC_V3_SWAP, TOPIC_V2_SWAP, TOPIC_PONS_CURVE_BUY, TOPIC_PONS_CURVE_SELL)]
    out["venue_events_seen"] = len(venue_logs)
    if not venue_logs:
        out["basis"] = "no_venue_event"
        return out
    if len(venue_logs) > 1:
        out["basis"] = "multi_hop_route"
        return out
    lg = venue_logs[0]
    topic = lg["topics"][0]
    words = _words(lg["data"])
    wrapped = NATIVE_WRAPPED.get(chain)

    def _flow(address: str, asset_logs_token: str) -> tuple[int, int]:
        """(atoms of ``asset_logs_token`` into ``address``, atoms out of it)."""
        inflow = outflow_ = 0
        for x in logs:
            if x["address"] == asset_logs_token and x["topics"][0] == TOPIC_TRANSFER and len(x["topics"]) == 3:
                amount = _words(x["data"])[0]
                if _topic_address(x["topics"][2]) == address:
                    inflow += amount
                if _topic_address(x["topics"][1]) == address:
                    outflow_ += amount
        return inflow, outflow_

    try:
        if topic in (TOPIC_V4_SWAP, TOPIC_V3_SWAP):
            terms = _evm_clmm_terms(out, lg, words, token=token, side=side, logs=logs, flow=_flow, wrapped=wrapped)
        elif topic in (TOPIC_PONS_CURVE_BUY, TOPIC_PONS_CURVE_SELL):
            terms = _evm_pons_terms(out, lg, words, token=token, side=side, wallet=wallet, flow=_flow, wrapped=wrapped)
        else:
            out["venue"] = "uniswap_v2"
            out["basis"] = "venue_event_undecoded"
            return out
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        return _cost_refuse(out, "venue_terms_unproved: " + str(exc), basis="venue_event_unpriced")
    out["basis"] = "venue_event"
    out["wallet_bound"] = True
    extra = terms.pop("extra_fees", {})
    if gmgn_in is not None and gmgn_fee is not None:
        venue_native = gmgn_in - gmgn_fee if side == "buy" else gmgn_out + gmgn_fee
        if venue_native != terms["user"]:
            out["notes"].append(f"router_and_venue_native_legs_differ:{venue_native}!={terms['user']}")
    _finish_costs(out, side=side, terms=terms, wallet_tokens=wallet_tokens, outflow=outflow, network=gas,
                  router=gmgn_fee, extra_fees=extra)
    return out


def _evm_clmm_terms(out: dict, lg: dict, words: list[int], *, token: str, side: str, logs: list[dict],
                    flow, wrapped: str | None) -> dict:
    v4 = lg["topics"][0] == TOPIC_V4_SWAP
    if len(words) < (6 if v4 else 5):
        raise ValueError("swap_log_short")
    a0, a1 = _signed256(words[0]), _signed256(words[1])
    sqrt_post, liquidity = words[2], words[3]
    pool = lg["address"]
    out["venue"] = "uniswap_v4" if v4 else "uniswap_v3"
    out["event"] = "Swap"
    out["event_source"] = "receipt_log"
    token_in, token_out = flow(pool, token)
    # v4 amounts are the SWAPPER's deltas (+ received); v3 amounts are the POOL's (+ received).
    user_a0, user_a1 = (a0, a1) if v4 else (-a0, -a1)
    want = token_out if side == "buy" else -token_in
    if user_a1 == want and want != 0:
        token_is_1, native_user = True, user_a0
    elif user_a0 == want and want != 0:
        token_is_1, native_user = False, user_a1
    else:
        raise ValueError("pool_token_leg_not_bound_to_token_transfers")
    # The other currency must be native: either no ERC-20 moved it (native ETH, v4) or the
    # wrapped-native token moved exactly that amount through the pool.
    native_amount = abs(native_user)
    if wrapped:
        w_in, w_out = flow(pool, wrapped)
        if (w_in or w_out) and native_amount not in (w_in, w_out):
            raise ValueError("pool_quote_leg_not_native")
    others = [x for x in logs if x["topics"][0] == TOPIC_TRANSFER and x["address"] not in (token, wrapped)
              and pool in (_topic_address(x["topics"][1]), _topic_address(x["topics"][2]))]
    if others:
        raise ValueError("pool_quote_leg_not_native")
    if side == "buy" and not native_user < 0 < want:
        raise ValueError("swap_direction_contradicts_side")
    if side == "sell" and not want < 0 < native_user:
        raise ValueError("swap_direction_contradicts_side")
    out_is_1 = token_is_1 if side == "buy" else not token_is_1
    out_amount = abs(want) if side == "buy" else native_amount
    in_amount = native_amount if side == "buy" else abs(want)
    spot, implied_fee = _clmm_spot(sqrt_post_x96=sqrt_post, liquidity=liquidity, out_amount=out_amount,
                                   out_is_token1=out_is_1, in_amount=in_amount, token_is_token1=token_is_1)
    lp_fee_fraction = Decimal(words[5]) / Decimal(1_000_000) if v4 else implied_fee
    basis = "pool_state_post_swap_single_range_verified"
    notes = []
    if implied_fee is None or lp_fee_fraction is None:
        spot, basis = None, "pool_state_unusable"
    elif v4 and abs(implied_fee - lp_fee_fraction) > Decimal("1e-4"):
        spot, basis = None, "tick_crossed_or_fee_mismatch"
    elif not v4 and not Decimal("-1e-4") <= implied_fee <= Decimal("0.0101"):
        spot, basis = None, "tick_crossed_or_fee_out_of_range"
    elif not v4:
        notes.append(f"v3_fee_tier_implied:{_dstr(implied_fee)}")
    # Hook fee and tax (Pons graduated pools): charged in whichever currency the hook names.
    extra: dict[str, Decimal] = {}
    hook_native = 0
    token_fee_atoms = 0
    if v4:
        pool_id = lg["topics"][1] if len(lg["topics"]) > 1 else None
        for x in logs:
            if (x["address"] == RH_PONS_MEME_HOOK and x["topics"][0] == TOPIC_PONS_HOOK_FEE
                    and len(x["topics"]) > 1 and x["topics"][1] == pool_id):
                w = _words(x["data"])
                currency = "0x" + format(w[0], "040x")
                if currency == token:
                    token_fee_atoms += w[1] + w[2]
                    if spot is None:
                        notes.append(f"hook_fee_token_atoms_unvalued:{w[1]}+{w[2]}")
                        continue
                    extra["hook_fee"] = Decimal(w[1]) * spot
                    extra["hook_tax"] = Decimal(w[2]) * spot
                elif currency in (EVM_ZERO_ADDRESS, wrapped):
                    hook_native += w[1] + w[2]
                    extra["hook_fee"] = Decimal(w[1])
                    extra["hook_tax"] = Decimal(w[2])
                else:
                    raise ValueError("hook_fee_currency_unknown")
    with localcontext(_CTX):
        lp_fee = Decimal(native_amount) * lp_fee_fraction if side == "buy" else (
            Decimal(abs(want)) * lp_fee_fraction * spot if spot is not None else Decimal(0))
    extra["lp_fee"] = lp_fee
    if side == "buy":
        # native in pays the LP fee; the pool's token output is gross of the hook's take
        tokens, user = abs(want), native_amount
        swap = Decimal(native_amount) - lp_fee
    else:
        # token in pays the LP fee inside the pool, so the curve saw the native output PLUS
        # that fee's value; counting it here keeps impact and the LP fee from overlapping
        tokens = abs(want)
        user = native_amount - hook_native
        swap = Decimal(native_amount) + lp_fee
    return {"tokens": tokens, "swap": swap, "user": user, "fees": {}, "spot": spot, "spot_basis": basis,
            "notes": notes, "extra_fees": extra, "token_fee_atoms": token_fee_atoms}


def _evm_pons_terms(out: dict, lg: dict, words: list[int], *, token: str, side: str, wallet: str,
                    flow, wrapped: str | None) -> dict:
    """A Pons bonding-curve trade. The event carries no reserves, so no pre-trade spot."""
    if len(words) < 4:
        raise ValueError("curve_log_short")
    curve = lg["address"]
    buy = lg["topics"][0] == TOPIC_PONS_CURVE_BUY
    if buy != (side == "buy"):
        raise ValueError("curve_event_contradicts_side")
    out["venue"], out["event"], out["event_source"] = "pons_curve", ("CurveBuy" if buy else "CurveSell"), "receipt_log"
    token_in, token_out = flow(curve, token)
    quote_in, quote_out = flow(curve, wrapped) if wrapped else (0, 0)
    fee, tax = words[2], words[3]
    if buy:
        quote, tokens = words[0], words[1]
        if token_out != tokens or quote_in != quote:
            raise ValueError("curve_legs_not_bound_to_transfers_or_quote_not_native")
        user, swap = quote, Decimal(quote - fee - tax)
    else:
        tokens, quote = words[0], words[1]
        if token_in != tokens or quote_out != quote:
            raise ValueError("curve_legs_not_bound_to_transfers_or_quote_not_native")
        user, swap = quote, Decimal(quote + fee + tax)
    return {"tokens": tokens, "swap": swap, "user": user, "fees": {"curve_fee": fee, "curve_tax": tax},
            "spot": None, "spot_basis": "curve_event_has_no_reserves", "notes": [], "extra_fees": {}}


# --- the fill_costs table -------------------------------------------------------------------

COLUMNS: tuple[str, ...] = (
    "order_id", "chain", "side", "lane", "token", "signature", "version", "basis", "venue", "event",
    "event_source", "programs_json", "block_time_ms", "native_leg", "token_leg", "wallet_native_delta",
    "wallet_token_delta", "venue_fee_native", "venue_fees_json", "network_fee_native", "priority_fee_native",
    "router_fee_native", "router_json", "spot_pre_native_per_token", "spot_basis", "exec_native_per_token",
    "price_impact_bps", "curve_impact_bps", "impact_cost_native", "allin_native_per_token",
    "decision_ref_native_per_token", "decision_ref_basis", "exec_vs_decision_bps", "allin_vs_decision_bps",
    "order_exec_vs_decision_bps", "total_cost_native", "total_cost_bps", "residual_native", "reconciled",
    "detail_json", "computed_ms",
)

#: Rows that are retried by the next run; every other basis is a final observation.
RETRY_BASES = frozenset({"fetch_failed"})

_PENDING_SQL = (
    "SELECT o.order_id, o.chain, o.side, o.lane, o.token, o.tx_hash, o.amount_in, o.min_out, o.slippage_bps, "
    "o.filled_out, o.updated_ms FROM orders o WHERE o.mode='live' AND o.state='filled' AND o.tx_hash IS NOT NULL "
    "AND o.updated_ms >= ? AND o.chain IN ({chains}){unrecorded} ORDER BY o.updated_ms LIMIT ?"
)
_UNRECORDED = (" AND NOT EXISTS (SELECT 1 FROM fill_costs fc WHERE fc.order_id = o.order_id "
               "AND fc.basis NOT IN ('fetch_failed'))")


def pending_orders(conn: sqlite3.Connection, *, since_ms: int, limit: int,
                   chains: tuple[str, ...] = ("sol", "robinhood")) -> list[dict]:
    """Live filled orders with a transaction and no final ``fill_costs`` row. One short read."""
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fill_costs'").fetchone() is not None
    sql = _PENDING_SQL.format(chains=",".join("?" * len(chains)), unrecorded=_UNRECORDED if has_table else "")
    cur = conn.execute(sql, (int(since_ms), *chains, int(limit)))
    names = [d[0] for d in cur.description]
    return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


def compute_fill_cost(order: dict, result, error: str | None) -> dict:
    """One ``fill_costs`` row from an order row and its fetched transaction (or the error)."""
    chain, side = order["chain"], order["side"]
    if result is None:
        costs = _cost_blank(chain, "lamport" if chain == "sol" else "wei")
        costs["basis"] = "fetch_failed"
        costs["notes"].append(str(error or "no_result")[:200])
    elif chain == "sol":
        costs = decode_solana_fill(result, token=order["token"], side=side, signature=order["tx_hash"])
    else:
        costs = decode_evm_fill(result, token=order["token"], side=side, chain=chain, signature=order["tx_hash"])
    reference, ref_basis = decision_reference(side=side, amount_in=order["amount_in"], min_out=order["min_out"],
                                              slippage_bps=order["slippage_bps"])
    try:
        amount_in, filled = int(order["amount_in"]), int(order["filled_out"] or 0)
    except (TypeError, ValueError):
        amount_in, filled = 0, 0
    order_exec = (_ratio(amount_in, filled) if side == "buy" else _ratio(filled, amount_in)) if filled and amount_in else None
    exec_price = Decimal(costs["exec_price"]) if costs.get("exec_price") else None
    allin = Decimal(costs["allin_price"]) if costs.get("allin_price") else None
    detail = {k: costs[k] for k in ("notes", "venue_fee_splits", "native_unit", "swap_native", "venue_events_seen",
                                    "token_shortfall_native", "wallet_bound") if k in costs}
    return {
        "order_id": order["order_id"], "chain": chain, "side": side, "lane": order.get("lane"),
        "token": order["token"], "signature": order["tx_hash"], "version": VERSION, "basis": costs["basis"],
        "venue": costs["venue"], "event": costs["event"], "event_source": costs["event_source"],
        "programs_json": json.dumps(costs["programs"]), "block_time_ms": costs["block_time_ms"],
        "native_leg": costs["native_leg"], "token_leg": costs["token_leg"],
        "wallet_native_delta": costs["wallet_native_delta"], "wallet_token_delta": costs["wallet_token_delta"],
        "venue_fee_native": costs["venue_fee_native"], "venue_fees_json": json.dumps(costs["venue_fees"], sort_keys=True),
        "network_fee_native": costs["network_fee_native"], "priority_fee_native": costs["priority_fee_native"],
        "router_fee_native": costs["router_fee_native"], "router_json": json.dumps(costs["router_transfers"]),
        "spot_pre_native_per_token": costs["spot_pre"], "spot_basis": costs["spot_basis"],
        "exec_native_per_token": costs["exec_price"], "price_impact_bps": costs["price_impact_bps"],
        "curve_impact_bps": costs["curve_impact_bps"], "impact_cost_native": costs["impact_cost_native"],
        "allin_native_per_token": costs["allin_price"],
        "decision_ref_native_per_token": _dstr(reference), "decision_ref_basis": ref_basis,
        "exec_vs_decision_bps": _bps(_signed_change(exec_price, reference)),
        "allin_vs_decision_bps": _bps(_signed_change(allin, reference)),
        "order_exec_vs_decision_bps": _bps(_signed_change(order_exec, reference)),
        "total_cost_native": costs["total_cost_native"], "total_cost_bps": costs["total_cost_bps"],
        "residual_native": costs["residual_native"], "reconciled": 1 if costs["reconciled"] else 0,
        "detail_json": json.dumps(detail, sort_keys=True, default=str), "computed_ms": int(time.time() * 1000),
    }


def write_fill_costs(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """Upsert rows in ONE short transaction. A final row is never re-selected, so re-running
    only ever replaces ``fetch_failed`` rows; replacing a final row with itself is harmless."""
    if not rows:
        return 0
    from kaiba.core.db import tx

    sql = (f"INSERT OR REPLACE INTO fill_costs ({', '.join(COLUMNS)}) "
           f"VALUES ({', '.join('?' * len(COLUMNS))})")
    with tx(conn):
        for row in rows:
            conn.execute(sql, tuple(row[c] for c in COLUMNS))
    return len(rows)


def _request(chain: str, signature: str) -> tuple[str, list]:
    if chain == "sol":
        return "getTransaction", [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0,
                                              "commitment": "finalized"}]
    return "eth_getTransactionReceipt", [signature]


def limiter_fetcher(conn: sqlite3.Connection | None = None):
    """Production fetch: our RPC endpoint through the shared limiter at RESEARCH priority, so
    it can never take a slot protection needs. Not cached: a backfill would fill the disk."""
    from kaiba.core.config import get_settings
    from kaiba.core.limiter import Priority
    from kaiba.core.schemas import Chain
    from kaiba.providers._http import post_json, redact_text

    def fetch(chain: str, signature: str):
        url = get_settings().rpc_for(Chain(chain))
        if not url:
            return None, "no_rpc_configured"
        method, params = _request(chain, signature)
        family = "eth" if chain != "sol" else "sol"
        got = post_json("rpc", f"{family}.{method}", url, json_body={"jsonrpc": "2.0", "id": 1, "method": method,
                                                                    "params": params},
                        priority=Priority.RESEARCH, ttl_s=0.0, timeout_s=20.0, retries=1, wait_for_slot_s=5.0,
                        conn=conn)
        if not got.ok or not isinstance(got.data, dict):
            return None, redact_text(str(got.receipt.note or "rpc_unavailable"))[:200]
        if got.data.get("error"):
            return None, redact_text(json.dumps(got.data["error"], default=str))[:200]
        return got.data.get("result"), None

    return fetch


def direct_fetcher():
    """Read-only fetch for a dry run: no limiter, so nothing is written to any database.
    The endpoint URL holds an API key; it is never printed, and errors are redacted."""
    import httpx

    from kaiba.core.config import get_settings
    from kaiba.core.schemas import Chain
    from kaiba.providers._http import redact_text

    def fetch(chain: str, signature: str):
        url = get_settings().rpc_for(Chain(chain))
        if not url:
            return None, "no_rpc_configured"
        method, params = _request(chain, signature)
        try:
            resp = httpx.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=20.0)
            data = resp.json()
        except Exception as exc:  # noqa: BLE001 - any transport failure is a fetch failure
            return None, redact_text(type(exc).__name__)
        if not isinstance(data, dict):
            return None, "malformed_rpc_reply"
        if data.get("error"):
            return None, redact_text(json.dumps(data["error"], default=str))[:200]
        return data.get("result"), None

    return fetch


def compute_pending(orders: list[dict], fetch, *, pace_s: float = 1.0) -> list[dict]:
    """Fetch and decode, at most one request per ``pace_s``. Holds no database handle."""
    rows = []
    last = 0.0
    for order in orders:
        wait = pace_s - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
        last = time.monotonic()
        try:
            result, error = fetch(order["chain"], order["tx_hash"])
        except Exception as exc:  # noqa: BLE001 - one bad fetch must not stop the sweep
            result, error = None, type(exc).__name__
        if result is None and error is None:
            error = "transaction_not_found"
        rows.append(compute_fill_cost(order, result, error))
    return rows


def record_fill_costs(conn: sqlite3.Connection, *, since_ms: int, limit: int = 50, fetch=None,
                      pace_s: float = 1.0, chains: tuple[str, ...] = ("sol", "robinhood")) -> dict:
    """Decode and store the costs of up to ``limit`` live fills since ``since_ms``.

    Idempotent: one row per order id, a final row is never re-selected, ``fetch_failed`` rows
    are retried. The selection is one short read; RPC work happens with no transaction open;
    the writes are one short transaction at the end.
    """
    orders = pending_orders(conn, since_ms=since_ms, limit=limit, chains=chains)
    rows = compute_pending(orders, fetch or limiter_fetcher(conn), pace_s=pace_s)
    written = write_fill_costs(conn, rows)
    return {"selected": len(orders), "written": written, "coverage": coverage(rows)}


def coverage(rows: list[dict]) -> dict:
    out: dict = {}
    for row in rows:
        key = f"{row['chain']}:{row['basis']}"
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items()))


def _ro_connect(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m kaiba.execution.fill_costs",
                                     description="Decode what live fills paid (venue fees, impact, router, network).")
    parser.add_argument("command", choices=["backfill"])
    parser.add_argument("--since-days", type=float, default=30.0)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--chains", default="sol,robinhood")
    parser.add_argument("--pace-s", type=float, default=1.0)
    parser.add_argument("--dry-run", action="store_true",
                        help="read-only: compute and print, write nothing, bypass the (DB-backed) limiter")
    parser.add_argument("--db", default=None, help="database path for --dry-run (default: the configured one)")
    parser.add_argument("--order-ids", default=None, help="comma-separated order ids instead of the pending scan")
    args = parser.parse_args(argv)
    chains = tuple(c.strip() for c in args.chains.split(",") if c.strip())
    since_ms = int(time.time() * 1000 - args.since_days * 86_400_000)
    if args.dry_run:
        path = args.db or os.path.expanduser("~/kaiba/data/kaiba.db")
        conn = _ro_connect(path)
        try:
            if args.order_ids:
                ids = [x for x in args.order_ids.split(",") if x]
                cur = conn.execute(_PENDING_SQL.split(" WHERE ")[0] + " WHERE o.order_id IN ("
                                   + ",".join("?" * len(ids)) + ")", ids)
                names = [d[0] for d in cur.description]
                orders = [dict(zip(names, r, strict=True)) for r in cur.fetchall()]
            else:
                orders = pending_orders(conn, since_ms=since_ms, limit=args.limit, chains=chains)
        finally:
            conn.close()
        rows = compute_pending(orders, direct_fetcher(), pace_s=args.pace_s)
        for row in rows:
            print(json.dumps(row, sort_keys=True))
        print(json.dumps({"dry_run": True, "selected": len(orders), "coverage": coverage(rows)}, sort_keys=True))
        return 0
    from kaiba.core.db import get_conn

    summary = record_fill_costs(get_conn(), since_ms=since_ms, limit=args.limit, pace_s=args.pace_s, chains=chains)
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
