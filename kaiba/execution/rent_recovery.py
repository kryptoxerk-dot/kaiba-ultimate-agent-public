"""Read-only SPL empty-account assessment, never a cleanup executor.

Layout/close semantics: solana-program/token program@v9.0.0 state.rs and
processor.rs. Inputs are provider attestations, not independently verified state.
Candidates need fresh revalidation, fee evidence, position exclusions and an
authorized signer before any close. Recovered lamports are not trading profit.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections import Counter
from decimal import Decimal
from typing import Any

import base58

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
MAX_ACCOUNTS = 5_000


class RentEvidenceError(ValueError):
    """Closed identity-free reason safe for an operator summary."""

    def __init__(self, reason: str, *, details: dict | None = None):
        super().__init__(reason)
        self.details = details or {}


def _key(value: Any) -> bytes:
    if not isinstance(value, str) or not 32 <= len(value) <= 44:
        raise RentEvidenceError("invalid_public_key")
    try:
        raw = base58.b58decode(value)
    except (ValueError, TypeError) as exc:
        raise RentEvidenceError("invalid_public_key") from exc
    if len(raw) != 32 or not any(raw) or base58.b58encode(raw).decode() != value:
        raise RentEvidenceError("invalid_public_key")
    return raw


def _uint(value: Any) -> int:
    if type(value) is not int or not 0 <= value < 2**64:
        raise RentEvidenceError("invalid_integer")
    return value


def _extensions(raw: bytes) -> list[int]:
    """Strict v10 Token-2022 account/TLV subset, not an extension-completeness claim."""
    if len(raw) == 165:
        return []
    if len(raw) < 166 or len(raw) == 355 or raw[165] != 2:
        raise RentEvidenceError("invalid_token2022_account_type_or_size")
    seen = set()
    pos = 166
    while pos < len(raw):
        if not any(raw[pos:]):  # only zero unused padding, not hidden future data
            break
        if len(raw)-pos < 4:
            raise RentEvidenceError("malformed_tlv")
        kind = int.from_bytes(raw[pos:pos+2], "little")
        size = int.from_bytes(raw[pos+2:pos+4], "little")
        if kind in seen:
            raise RentEvidenceError("duplicate_extension")
        if kind not in (2, 7):
            raise RentEvidenceError("unassessed_token2022_extension", details={"first_unassessed_type": kind})
        if size != (8 if kind == 2 else 0) or pos+4+size > len(raw):
            raise RentEvidenceError("malformed_tlv")
        if kind == 2 and int.from_bytes(raw[pos+4:pos+12], "little"):
            raise RentEvidenceError("withheld_transfer_fees", details={
                "withheld_atoms": str(int.from_bytes(raw[pos+4:pos+12], "little"))})
        seen.add(kind)
        pos += 4+size
    return sorted(seen)


def _candidate(row: dict, owner: bytes, program: str) -> dict:
    account_key = row["pubkey"]
    if _key(account_key) == owner:
        raise RentEvidenceError("source_equals_wallet")
    account = row["account"]
    if not isinstance(account, dict):
        raise RentEvidenceError("malformed_account")
    if account.get("owner") != program:
        raise RentEvidenceError("unsupported_program")
    if account.get("executable") is not False:
        raise RentEvidenceError("executable_or_unknown")
    lamports = _uint(account["lamports"])
    if not lamports:
        raise RentEvidenceError("no_lamports")
    encoded = account["data"]
    if (not isinstance(encoded, list) or len(encoded) != 2 or encoded[1] != "base64"
            or not isinstance(encoded[0], str) or not 220 <= len(encoded[0]) <= 5464):
        raise RentEvidenceError("account_encoding_or_size")
    try:
        raw = base64.b64decode(encoded[0], validate=True)
    except (ValueError, binascii.Error) as exc:
        raise RentEvidenceError("invalid_base64") from exc
    if base64.b64encode(raw).decode() != encoded[0]:
        raise RentEvidenceError("invalid_base64")
    if (not 165 <= len(raw) <= 4096 or (program == TOKEN_PROGRAM and len(raw) != 165)
            or ("space" in account and (type(account["space"]) is not int or account["space"] != len(raw)))):
        raise RentEvidenceError("account_layout")
    if raw[32:64] != owner:
        raise RentEvidenceError("token_owner_mismatch")
    if raw[108] not in (1, 2):  # initialized or frozen; classic zero frozen accounts may close
        raise RentEvidenceError("uninitialized_or_invalid_state")
    delegate = int.from_bytes(raw[72:76], "little")
    native = int.from_bytes(raw[109:113], "little")
    close = int.from_bytes(raw[129:133], "little")
    if any(tag not in (0, 1) for tag in (delegate, native, close)):
        raise RentEvidenceError("invalid_option_tag")
    if int.from_bytes(raw[64:72], "little"):
        raise RentEvidenceError("nonzero_token_amount")
    if native:
        raise RentEvidenceError("native_wrapper_excluded")
    if close and raw[133:165] != owner:
        raise RentEvidenceError("foreign_close_authority")
    # Establish owned/zero/non-native/authority scope before classifying withheld
    # fees as a known blocker rather than an unknown foreign-account observation.
    extensions = _extensions(raw) if program == TOKEN_2022_PROGRAM else []
    return {"account": account_key, "lamports": lamports, "extension_types": extensions,
            "state": "frozen" if raw[108] == 2 else "initialized"}


def assess(request: dict, response: dict, *, expected_wallet: str, observed_ms: int, at_ms: int,
           program: str = TOKEN_PROGRAM) -> dict:
    """Assess one finalized program response; private result includes account IDs.

    No recipient is selected, instructions built, fees guessed or state changed.
    A fresh receipt clock is necessary but cannot certify RPC state freshness.
    """
    out = {"version": "solana-rent-assessment/2", "status": "unavailable",
           "financial_execution": False, "profit_proven": False,
           "observed_ms": observed_ms if type(observed_ms) is int else None, "candidate_count": None,
           "candidate_lamports": None, "net_recoverable_lamports": None,
           "fee_lamports": None, "reason": None, "candidates": []}
    try:
        owner = _key(expected_wallet)
        if program not in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
            raise RentEvidenceError("unsupported_program_scope")
        if (type(at_ms) is not int or type(observed_ms) is not int
                or not 0 <= at_ms-observed_ms <= 60_000):
            raise RentEvidenceError("stale_or_future_receipt")
        if (not isinstance(request, dict) or request.get("jsonrpc") != "2.0"
                or type(request.get("id")) is not int
                or request.get("method") != "getTokenAccountsByOwner"
                or request.get("params") != [expected_wallet, {"programId": program},
                                             {"encoding": "base64", "commitment": "finalized"}]):
            raise RentEvidenceError("request_scope_mismatch")
        if (not isinstance(response, dict) or response.get("jsonrpc") != "2.0"
                or type(response.get("id")) is not int or response["id"] != request["id"]
                or "error" in response):
            raise RentEvidenceError("rpc_error_or_identity")
        result = response["result"]
        slot = _uint(result["context"]["slot"])
        rows = result["value"]
        if not isinstance(rows, list) or len(rows) > MAX_ACCOUNTS:
            raise RentEvidenceError("account_list_missing_or_over_cap")
        keys = [row.get("pubkey") if isinstance(row, dict) else None for row in rows]
        if any(not isinstance(k, str) for k in keys) or len(set(keys)) != len(keys):
            raise RentEvidenceError("missing_or_duplicate_account_identity")
        # Hash the exact parsed request/response representation, not original transport bytes.
        bound = json.dumps({"request": request, "response": response}, sort_keys=True,
                           separators=(",", ":"), allow_nan=False).encode()
        if len(bound) > 2*1024*1024:
            raise RentEvidenceError("snapshot_over_cap")
        refused = Counter()
        unknown_types = Counter()
        blocked = []
        candidates = []
        for row in rows:
            try:
                candidates.append(_candidate(row, owner, program))
            except RentEvidenceError as exc:
                refused[str(exc)] += 1
                if "first_unassessed_type" in exc.details:
                    unknown_types[str(exc.details["first_unassessed_type"])] += 1
                if str(exc) == "withheld_transfer_fees":
                    blocked.append({"account": row["pubkey"], "lamports": row["account"]["lamports"],
                                    "withheld_atoms": exc.details["withheld_atoms"]})
            except (KeyError, TypeError, ValueError):
                refused["malformed_account"] += 1
        total = sum(c["lamports"] for c in candidates)
        unknown = sum(v for k, v in refused.items() if k not in {
            "nonzero_token_amount", "native_wrapper_excluded", "foreign_close_authority", "no_lamports",
            "withheld_transfer_fees"})
        out.update(status="candidates_observed" if candidates else "incomplete" if unknown else "no_candidates_observed",
                   context_slot=slot, returned_accounts=len(rows), candidate_count=len(candidates),
                   candidate_lamports=total, candidate_sol=str(Decimal(total)/Decimal(1_000_000_000)),
                   refused_count=sum(refused.values()), unknown_count=unknown,
                   refusal_reasons=dict(sorted(refused.items())), candidates=candidates,
                   first_unassessed_extension_counts=dict(sorted(unknown_types.items())),
                   withheld_fee_account_count=len(blocked),
                   withheld_fee_account_lamports=sum(b["lamports"] for b in blocked),
                   blocked_accounts=blocked,
                   parsed_snapshot_sha256=hashlib.sha256(bound).hexdigest(),
                   program=program, coverage="one_wallet_one_token_program_provider_attestation",
                   pending_requirements=["fresh_account_revalidation", "active_position_and_order_exclusions",
                                         "exact_fee_and_simulation", "owned_destination_and_signer",
                                         "submit_and_reconciliation"])
    except RentEvidenceError as exc:
        out["reason"] = str(exc)
    except (KeyError, TypeError, ValueError, ArithmeticError):
        out["reason"] = "malformed_rpc_snapshot"
    return out


def public_summary(result: dict) -> dict:
    """Whitelist aggregates; never leak wallet, token-account or mint identities."""
    fields = ("version", "status", "financial_execution", "profit_proven", "observed_ms", "reason",
              "context_slot", "returned_accounts", "candidate_count", "candidate_lamports", "candidate_sol",
              "refused_count", "unknown_count", "refusal_reasons", "parsed_snapshot_sha256", "coverage",
              "fee_lamports", "net_recoverable_lamports", "pending_requirements", "program",
              "first_unassessed_extension_counts", "withheld_fee_account_count", "withheld_fee_account_lamports")
    return {k: result[k] for k in fields if k in result}
