"""Provenance-checked DLMM position actions and transaction cash-flow reconciliation.

Codex DLMM-CASHFLOWS-20261029. Pinned SDK/IDL revision is dlmm_state.SDK_REVISION.
Pure receipt decoding; no signing, provider, DB, execution or price estimation.
"""

from __future__ import annotations

import base64
import hashlib
from collections import Counter

import base58

from kaiba.execution.fill_costs import (
    EVENT_CPI_TAG,
    _account_keys,
    _instructions,
    _native_balances,
    _sol_id,
    _token_deltas,
    _uint,
    emitting_programs,
    event_discriminator,
    find_program_address,
)
from kaiba.learning.dlmm_state import PROGRAM, DecodeError, public_key

SYSTEM = "11111111111111111111111111111111"
TOKEN_PROGRAMS = {"TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"}
WSOL = "So11111111111111111111111111111111111111112"
EVENT_AUTHORITY = find_program_address([b"__event_authority"], PROGRAM)[0]
PPO = (("pool", "key"), ("position", "key"), ("owner", "key"))
LIQUIDITY = (("pool", "key"), ("actor", "key"), ("position", "key"),
             ("x", "u64"), ("y", "u64"), ("active_bin", "i32"))
LAYOUTS = {
    "PositionCreate": PPO,
    "PositionClose": (("position", "key"), ("owner", "key")),
    "AddLiquidity": LIQUIDITY,
    "RemoveLiquidity": LIQUIDITY,
    "ClaimFee": PPO + (("x", "u64"), ("y", "u64")),
    "ClaimFee2": PPO + (("x", "u64"), ("y", "u64"), ("active_bin", "i32")),
    "Rebalancing": PPO + (("active_bin", "i32"),) + tuple((k, "u64") for k in (
        "x_withdrawn", "x_added", "y_withdrawn", "y_added", "x_fee", "y_fee")) + tuple((k, "i32") for k in (
        "old_min", "old_max", "new_min", "new_max")) + (("reward_0", "u64"), ("reward_1", "u64")),
    "CompositionFee": (("actor", "key"), ("bin", "i16")) + tuple((k, "u64") for k in (
        "x", "y", "protocol_x", "protocol_y")),
}
EVENT_NAMES = {event_discriminator(name): name for name in LAYOUTS}


def _payload(raw: bytes, path: str, carrier: str, group: int | None) -> dict:
    name = EVENT_NAMES.get(raw[:8])
    if name is None:
        return {"name": "unknown", "path": path, "carrier": carrier, "group": group,
                "payload_sha256": hashlib.sha256(raw).hexdigest()}
    offset, fields = 8, {}
    for key, kind in LAYOUTS[name]:
        width = 32 if kind == "key" else int(kind[1:]) // 8
        if offset + width > len(raw):
            raise DecodeError("truncated_action_event")
        part = raw[offset:offset + width]
        fields[key] = base58.b58encode(part).decode() if kind == "key" else int.from_bytes(part, "little", signed=kind.startswith("i"))
        offset += width
    if offset != len(raw):
        raise DecodeError("unknown_action_event_extension")
    return {"name": name, "fields": fields, "path": path, "carrier": carrier, "group": group,
            "payload_sha256": hashlib.sha256(raw).hexdigest()}


def _events(meta: dict) -> tuple[list[dict], list[str]]:
    cpi, logged, gaps = [], [], []
    groups = meta.get("innerInstructions")
    if not isinstance(groups, list):
        raise DecodeError("inner_instructions_unavailable")
    for g, group in sorted(enumerate(groups), key=lambda item: item[1]["index"]):
        for i, ix in enumerate(group["instructions"]):
            if ix.get("programId") != PROGRAM or not isinstance(ix.get("data"), str):
                continue
            if len(ix["data"]) > 4096:
                raise DecodeError("action_payload_oversized")
            raw = base58.b58decode(ix["data"])
            if raw[:8] != EVENT_CPI_TAG:
                continue
            if ix.get("accounts") != [EVENT_AUTHORITY]:
                raise DecodeError("event_authority_mismatch")
            cpi.append(_payload(raw[8:], f"inner[{g}][{i}]", "event_cpi", group["index"]))
    logs = meta.get("logMessages")
    if isinstance(logs, list):
        truncated = any(isinstance(x, str) and "Log truncated" in x for x in logs)
        for i, (line, emitter) in enumerate(zip(logs, emitting_programs(logs), strict=True)):
            if emitter != PROGRAM or not isinstance(line, str) or not line.startswith("Program data: "):
                continue
            encoded = line[len("Program data: "):]
            if len(encoded) > 4096:
                raise DecodeError("action_payload_oversized")
            logged.append(_payload(base64.b64decode(encoded, validate=True), f"log[{i}]", "program_log", None))
        if cpi and logged and not truncated and Counter(x["payload_sha256"] for x in cpi) != Counter(x["payload_sha256"] for x in logged):
            raise DecodeError("event_carriers_disagree")
        if not cpi and truncated:
            gaps.append("logs_truncated")
    if not cpi:
        gaps.append("event_cpi_unavailable")
    return cpi or logged, gaps


def _cashflows(result: dict, *, keys: list[str], owner: str, pool: str, position: str, mints: tuple[str, str],
               instructions: list, expected: list[int], fee: int) -> dict:
    meta, message = result["meta"], result["transaction"]["message"]
    pre, post = _native_balances(meta, keys)
    owner_index, position_index = keys.index(owner), keys.index(position)
    owner_native = post[owner_index] - pre[owner_index]
    position_funding = post[position_index] - pre[position_index]
    owned, mint_by_account, token_deltas, gaps = set(), {}, [], []
    # Existing strict helper requires balance identity and proof of new token accounts.
    for mint in mints:
        rows = _token_deltas(meta, message, keys, mint)
        token_deltas.append(sum(r["delta"] for r in rows.values() if r["owner"] == owner))
        for account, row in rows.items():
            if row["owner"] == owner:
                owned.add(account)
                mint_by_account[account] = mint
    # A temporary wrapped-SOL account can be born and closed inside one transaction and have
    # no token balance row. Only real token-program initializations establish its ownership.
    for _path, ix, program in instructions:
        parsed = ix.get("parsed")
        if program not in TOKEN_PROGRAMS or not isinstance(parsed, dict):
            continue
        info = parsed.get("info", {})
        if parsed.get("type") in ("initializeAccount", "initializeAccount2", "initializeAccount3") and info.get("owner") == owner:
            account, mint = info.get("account"), info.get("mint")
            if account not in keys or mint not in mints:
                gaps.append("unrelated_token_account_initialization")
                continue
            if account in mint_by_account and mint_by_account[account] != mint:
                raise DecodeError("token_account_mint_changed")
            owned.add(account)
            mint_by_account[account] = mint
    incoming, outgoing = 0, 0
    for _path, ix, program in instructions:
        parsed = ix.get("parsed")
        if program != SYSTEM or not isinstance(parsed, dict) or parsed.get("type") not in ("transfer", "transferWithSeed"):
            continue
        info = parsed.get("info", {})
        source, destination = info.get("source"), info.get("destination")
        if source not in keys or destination not in keys:
            raise DecodeError("native_transfer_account_missing")
        amount = _uint(info.get("lamports"))
        if source == owner and destination not in owned | {owner, position}:
            outgoing += amount
        if destination == owner and source not in owned | {owner, position}:
            incoming += amount
    token_rent_delta = 0
    for account in owned:
        index = keys.index(account)
        delta = post[index] - pre[index]
        if mint_by_account[account] == WSOL:
            # Split persistent wrapped-SOL principal from its account lamports.
            rows = _token_deltas(meta, message, keys, WSOL)
            if account in rows:
                delta -= rows[account]["delta"]
            elif pre[index] != 0 or post[index] != 0:
                raise DecodeError("temporary_wsol_balance_unproved")
        token_rent_delta += delta
    owner_fee = fee if keys[0] == owner else 0
    combined_native = owner_native + sum(d for mint, d in zip(mints, token_deltas, strict=True) if mint == WSOL)
    expected_native = sum(d for mint, d in zip(mints, expected, strict=True) if mint == WSOL)
    native_residual = combined_native - expected_native + owner_fee + position_funding + token_rent_delta + outgoing - incoming
    token_residual = [None if mint == WSOL else actual - implied
                      for mint, actual, implied in zip(mints, token_deltas, expected, strict=True)]
    if native_residual or any(r not in (None, 0) for r in token_residual):
        gaps.append("wallet_event_cashflow_residual")
    if incoming:
        gaps.append("unrelated_native_inflow")
    if keys[0] != owner:
        gaps.append("network_fee_paid_by_other_party")
    return {"owner_native_delta": owner_native, "owner_token_delta_atoms": token_deltas,
            "owner_combined_sol_delta": combined_native, "owner_network_fee_lamports": owner_fee,
            "position_account_funding_delta_lamports": position_funding,
            "owned_token_account_funding_delta_lamports": token_rent_delta,
            "other_native_outflows_lamports": outgoing, "other_native_inflows_lamports": incoming,
            "native_residual_lamports": native_residual, "token_residual_atoms": token_residual,
            "reconciled": not gaps, "gaps": gaps,
            "funding_note": "Account funding is encumbered native capital, not a proved rental expense/refund.",
            "outflow_note": "Other transfers are observed debits; their recipient/service purpose is not inferred."}


def decode_transaction(result: dict, *, signature: str, pool: str, position: str, owner: str,
                       mints: tuple[str, str]) -> dict:
    """Decode one finalized receipt supplied by a separately bound collector.

    Identity/amount checks are RPC evidence, not signature or consensus verification. Only
    successful effects count as actions. Failed attempts still retain the transaction fee.
    """
    if not isinstance(mints, (list, tuple)) or len(mints) != 2 or len(set(mints)) != 2:
        raise DecodeError("invalid_mint_pair")
    for key in (pool, position, owner, *mints):
        public_key(key)
    _sol_id(signature, 64)
    if not isinstance(result, dict) or not (result.get("version") == "legacy" or
                                          type(result.get("version")) is int and result["version"] == 0):
        raise DecodeError("unsupported_transaction_version")
    meta, tx = result.get("meta"), result.get("transaction")
    if not isinstance(meta, dict) or not isinstance(tx, dict) or "err" not in meta:
        raise DecodeError("transaction_metadata_unavailable")
    signatures, message = tx.get("signatures"), tx.get("message")
    if not isinstance(signatures, list) or not signatures or signatures[0] != signature or not isinstance(message, dict):
        raise DecodeError("transaction_signature_mismatch")
    keys, _ = _account_keys(message, signatures, meta)
    if any(k not in keys for k in (pool, position, owner, PROGRAM)):
        raise DecodeError("position_transaction_identity_missing")
    fee = _uint(meta.get("fee"))
    output = {"schema": "kaiba-dlmm-actions/1", "signature": signature, "slot": _uint(result.get("slot")),
              "block_time": result.get("blockTime"), "network_fee_lamports": fee, "actions": [],
              "cashflows": None, "chain_verified": False, "net_pnl": None, "financial_execution": False}
    if meta["err"] is not None:
        pre, post = _native_balances(meta, keys)
        gaps = []
        if any(b - a != (-fee if i == 0 else 0) for i, (a, b) in enumerate(zip(pre, post, strict=True))):
            gaps.append("failed_transaction_native_residual")
        if keys[0] != owner:
            gaps.append("network_fee_paid_by_other_party")
        output.update(status="failed", gaps=gaps, owner_network_fee_lamports=fee if keys[0] == owner else 0,
                      owner_native_delta=post[keys.index(owner)] - pre[keys.index(owner)])
        return output
    instructions = _instructions(message, meta, keys)
    if len(instructions) > 500:
        raise DecodeError("instruction_budget_exceeded")
    events, gaps = _events(meta)
    target = []
    for event in events:
        if event["name"] == "unknown":
            gaps.append("unknown_dlmm_event")
            continue
        fields = event["fields"]
        if event["name"] == "CompositionFee":
            continue
        if fields.get("position") != position or fields.get("pool", pool) != pool:
            gaps.append("other_position_or_pool_in_transaction")
            continue
        if fields.get("owner", fields.get("actor")) != owner:
            gaps.append("action_actor_is_not_position_owner")
        target.append(event)
    if not target:
        gaps.append("no_target_position_event")
    expected, deposits, withdrawals, claimed = [0, 0], [0, 0], [0, 0], [0, 0]
    for event in target:
        name, fields = event["name"], event["fields"]
        normalized = {k: v for k, v in event.items() if k != "fields"}
        normalized["values"] = {k: v for k, v in fields.items() if k not in ("pool", "position", "owner", "actor")}
        output["actions"].append(normalized)
        if name == "AddLiquidity":
            for i, key in enumerate(("x", "y")):
                deposits[i] += fields[key]
        elif name == "RemoveLiquidity":
            for i, key in enumerate(("x", "y")):
                withdrawals[i] += fields[key]
        elif name.startswith("ClaimFee"):
            for i, key in enumerate(("x", "y")):
                claimed[i] += fields[key]
        elif name == "Rebalancing":
            for i, prefix in enumerate(("x", "y")):
                deposits[i] += fields[prefix + "_added"]
                withdrawals[i] += fields[prefix + "_withdrawn"]
                claimed[i] += fields[prefix + "_fee"]
            if fields["reward_0"] or fields["reward_1"]:
                gaps.append("third_asset_rewards_unreconciled")
    expected = [w + c - d for w, c, d in zip(withdrawals, claimed, deposits, strict=True)]
    # Composition fees describe embedded costs; they are NEVER debited a second time.
    composition = []
    for event in events:
        if event["name"] != "CompositionFee":
            continue
        matches = [t for t in target if t["group"] is not None and t["group"] == event["group"]]
        if len(matches) != 1 or event["fields"]["actor"] != owner:
            gaps.append("composition_fee_position_ambiguous")
            continue
        composition.append({k: v for k, v in event["fields"].items() if k != "actor"})
    output.update(event_implied_owner_delta_atoms=expected, deposits_atoms=deposits,
                  withdrawals_atoms=withdrawals, claimed_fee_atoms_gross=claimed, composition_fees_embedded=composition)
    try:
        output["cashflows"] = _cashflows(result, keys=keys, owner=owner, pool=pool, position=position,
                                        mints=mints, instructions=instructions, expected=expected, fee=fee)
        gaps.extend(output["cashflows"]["gaps"])
    except (ValueError, KeyError, TypeError) as exc:
        gaps.append("cashflow_evidence_unavailable:" + type(exc).__name__)
    output.update(status="observed", gaps=sorted(set(gaps)),
                  action_cashflows_reconciled=not gaps and output["cashflows"] is not None)
    return output
