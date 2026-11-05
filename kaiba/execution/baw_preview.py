"""Inspect pinned BAW preview evidence without inventing execution proofs.

The official contract-call response reports simulation and risks, but does not
declare complete asset effects or a locked total native fee ceiling. Those
remain unresolved even when every reported change array is empty. No network,
wallet key, signing, budget mutation or financial operation occurs here.
"""
from __future__ import annotations

import hashlib
import json
import re

from kaiba.execution.nft_mint_policy import CHAINS, MintRefused
from kaiba.providers.binance_wallet_cli import CLI_SHA256, CLI_VERSION

MAX_PREVIEW_BYTES = 64 * 1024
VERSION = "baw-preview-evidence/1"


def _count(parent, field):
    value = parent.get(field) if isinstance(parent, dict) else None
    return len(value) if isinstance(value, list) else None


def inspect_preview(raw, request, *, wallet, observed_ms, at_ms):
    """Describe a captured CLI data payload and bind it to the local request.

    `request` is the worker's immutable candidate, not a provider-returned echo.
    Binding the local request does NOT prove that every provider effect was
    disclosed, nor that a source account owns the named project.
    """
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_PREVIEW_BYTES:
        raise MintRefused("preview_evidence_size")
    if (type(observed_ms) is not int or type(at_ms) is not int or
            observed_ms < 0 or not 0 <= at_ms - observed_ms <= 60_000):
        raise MintRefused("preview_evidence_clock")
    if (not isinstance(request, dict) or request.get("chain") not in CHAINS or
            not isinstance(wallet, str) or not wallet):
        raise MintRefused("preview_request_binding")
    try:
        data = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeDecodeError) as exc:
        raise MintRefused("preview_evidence_json") from exc
    binding = {"candidate": request, "wallet": wallet}
    bound = json.dumps(binding, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    blockers = {"complete_asset_effect_proof_required", "locked_fee_bound_unverified",
                "official_collection_and_owned_recipient_unverified", "execution_adapter_not_armed"}
    out = {"schema": VERSION, "raw_sha256": hashlib.sha256(raw).hexdigest(),
           "local_request_sha256": hashlib.sha256(bound).hexdigest(),
           "cli_version": CLI_VERSION, "cli_sha256": CLI_SHA256, "observed_ms": observed_ms,
           "provider_simulation": "unknown", "request_id_present": False,
           "expires_ms": None, "provider_confirmation_required": None,
           "change_counts": {}, "risk_counts": {},
           "complete_asset_effects_verified": False, "locked_fee_ceiling_verified": False,
           "financial_execution": False}
    if not isinstance(data, dict):
        blockers.add("preview_data_object_required")
        data = {}
    request_id = data.get("requestId")
    out["request_id_present"] = bool(isinstance(request_id, str) and
                                     re.fullmatch(r"[A-Za-z0-9_-]{1,160}", request_id))
    if not out["request_id_present"]:
        blockers.add("preview_request_id_missing_or_invalid")
    expiry = data.get("expiresAt")
    if type(expiry) is int and expiry > 0:
        out["expires_ms"] = expiry
        if expiry <= at_ms:
            blockers.add("preview_expired")
    else:
        blockers.add("preview_expiry_unknown")
    confirmation = data.get("requireConfirmation")
    if type(confirmation) is bool:
        out["provider_confirmation_required"] = confirmation
        if confirmation:
            blockers.add("provider_app_confirmation_required")
    else:
        blockers.add("provider_confirmation_unknown")
    simulation = data.get("simulationResult")
    if isinstance(simulation, dict):
        code, error = simulation.get("simulationCode"), simulation.get("simulationErrorDetail")
        if code == "000000000" and error is None and "simulationErrorDetail" in simulation:
            out["provider_simulation"] = "succeeded"
        elif (isinstance(code, str) and code and code != "000000000") or error is not None:
            out["provider_simulation"] = "failed"
    for field in ("balanceChanges", "allowanceChanges", "authorityChanges"):
        count = _count(simulation, field)
        out["change_counts"][field] = count
        if count is None:
            blockers.add(field + "_unknown")
        elif count and field != "balanceChanges":
            blockers.add(field + "_present")
    if out["provider_simulation"] != "succeeded":
        blockers.add("simulation_" + out["provider_simulation"])
    risks = data.get("risks")
    for field in ("riskDetails", "riskBehaviors"):
        count = _count(risks, field)
        out["risk_counts"][field] = count
        if count is None:
            blockers.add(field + "_unknown")
        elif count:
            blockers.add(field + "_present")
    addresses = risks.get("addresses") if isinstance(risks, dict) else None
    out["risk_counts"]["addresses"] = len(addresses) if isinstance(addresses, dict) else None
    if addresses != {}:
        blockers.add("address_risks_present_or_unknown")
    out["blockers"] = sorted(blockers)
    return out
