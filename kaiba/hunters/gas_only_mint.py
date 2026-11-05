"""Additive gas-only NFT research preflight; never signs or authorizes a mint.

Codex TWEET-STRATEGY-REVIEW-20261102. The caller must produce a normalized,
source-bound simulation receipt from the exact wallet/chain/contract. This is
not a raw BAW payload parser or proof producer. Existing qualification remains
binding. No provider, key, budget writer, database or execution call is made.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from kaiba.hunters.qualification import qualification


def _amount(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and result >= 0 else None
    except (ValueError, InvalidOperation):
        return None


def assess_gas_only(row, receipt, *, wallet, contract, runtime_chains, at_ms,
                    per_mint_gas_native, daily_remaining_native, balance_native,
                    reserve_native, ev_floor=Decimal("0")):
    """Return preparation blockers without weakening existing economic gates.

    Gas amounts are native units for THIS chain; do not compare BNB to ETH or
    SOL. Daily remaining must already subtract confirmed, failed and reserved
    in-flight gas. The execution layer must reserve atomically and recheck.
    """
    base = qualification(row, at_ms=at_ms, ev_floor=ev_floor)
    reasons = list(base["reasons"])
    if row.get("kind") != "nft_mint":
        reasons.append("nft_only")
    if not isinstance(receipt, dict):
        receipt = {}
    if receipt.get("version") != "gas-only-receipt/1":
        reasons.append("simulation_receipt_missing")
    chain = row.get("chain")
    if not isinstance(runtime_chains, (list, tuple, set, frozenset)) or chain not in runtime_chains:
        reasons.append("runtime_chain_unverified")
    # Exact identities, including checksum/case: normalization belongs to the
    # source adapter, not a guess about the identity format of an unknown chain.
    if not wallet or not contract or any(receipt.get(k) != v for k, v in
                                       (("wallet", wallet), ("contract", contract), ("chain", chain))):
        reasons.append("simulation_identity_mismatch")
    digest = receipt.get("raw_sha256")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        reasons.append("raw_receipt_unbound")
    observed, expires = receipt.get("observed_ms"), receipt.get("expires_ms")
    if (type(observed) is not int or type(expires) is not int or
            not 0 <= at_ms - observed <= 60_000 or expires <= at_ms):
        reasons.append("simulation_stale")
    for field in ("simulation_succeeded", "asset_changes_complete", "fees_complete",
                  "official_contract_verified", "owned_recipient_verified"):
        if receipt.get(field) is not True:
            reasons.append(field + "_missing")
    for field in ("mint_price_native", "contract_fees_native", "non_gas_native_debit"):
        amount = _amount(receipt.get(field))
        if amount is None or amount != 0:
            reasons.append(field + "_not_zero")
    for field in ("token_debits", "approval_changes", "authority_changes", "risks"):
        # Missing is not empty. The source adapter must include every change,
        # not just parsedTx.value or a selector that happens to spell 'mint'.
        if receipt.get(field) != []:
            reasons.append(field + "_not_empty_or_unknown")
    gas = _amount(receipt.get("gas_upper_native"))
    cap, remaining, balance, reserve = map(_amount, (
        per_mint_gas_native, daily_remaining_native, balance_native, reserve_native))
    if gas is None or gas <= 0:
        reasons.append("gas_upper_unavailable")
    if any(x is None for x in (cap, remaining, balance, reserve)):
        reasons.append("funded_gas_budget_unavailable")
    elif gas is not None and (gas > cap or gas > remaining or gas > balance - reserve):
        reasons.append("gas_budget_exceeded")
    if receipt.get("provider_confirmation_required") is not False:
        reasons.append("provider_confirmation_required_or_unknown")
    return {"version": "gas-only-preflight/1", "gas_only_research_qualified": not reasons,
            "reasons": sorted(set(reasons)), "funded_action_authorized": False,
            "profit_proven": False,
            "note": "Normalized receipt checks only; raw proof, atomic gas reservation and signing integration required."}
