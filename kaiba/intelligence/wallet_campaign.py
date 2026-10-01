"""Read-only campaign accounting. Never assigns grades, trust, or trading settings.

Provider arithmetic and independently replayed tape remain separate evidence classes.
Existing history-model B rows require explicit review rather than borrowing tape's
sample guarantee. A target count is an inventory milestone, not a profit prediction.
"""
from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable
from typing import Any

from kaiba.core.schemas import looks_evm, looks_solana
from kaiba.intelligence import grade

CHAINS = ("bsc", "sol", "robinhood")
PROVIDER_MODEL = "kaiba-wallet-gmgn-v1"
MAX_EVIDENCE_AGE_MS = 86_400_000


def key(chain: str, address: str) -> tuple[str, str] | None:
    if chain not in CHAINS or not isinstance(address, str):
        return None
    address = address.strip()
    valid = looks_solana(address) if chain == "sol" else looks_evm(address)
    return (chain, address if chain == "sol" else address.lower()) if valid else None


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        n = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return n if math.isfinite(n) else None


def summarize(
    rows: Iterable[dict[str, Any]],
    tape_audits: Iterable[dict[str, Any]],
    *,
    now_ms: int,
    target: int = 10_000,
) -> dict[str, Any]:
    if target < 1:
        raise ValueError("target must be positive")
    audit_by_key = {key(a["chain"], a["address"]): a for a in tape_audits}
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    duplicates = set()
    invalid_identity = 0
    for row in rows:
        k = key(row.get("chain"), row.get("address"))
        if k is None:
            invalid_identity += 1
            continue
        if k in unique:
            duplicates.add(k)
        unique[k] = row
    counts: Counter[str] = Counter()
    chain_counts = {c: Counter() for c in CHAINS}
    qualified = []
    stored_ab = 0
    old_provider_rows = 0
    for k, row in unique.items():
        if row.get("grade") not in ("A", "B"):
            continue
        stored_ab += 1
        category = "requires_review"
        score = _number(row.get("score"))
        weight = _number(row.get("evidence_weight"))
        stamp = _number(row.get("scored_at_ms"))
        floor = grade.A_MIN_SCORE if row["grade"] == "A" else grade.B_MIN_SCORE
        if k in duplicates:
            category = "duplicate_identity_requires_review"
        elif score is None or weight is None or score < floor or weight < grade.MIN_EVIDENCE_WEIGHT:
            category = "invalid_grade_fields"
        elif stamp is None or stamp <= 0 or stamp > now_ms:
            category = "invalid_or_future_grade_time"
        elif row.get("model_version") == PROVIDER_MODEL:
            # The existing provider model is EVM-only. A provider label is not this model.
            if k[0] != "sol" and row["grade"] == "B":
                category = "provider_reported_B"
                old_provider_rows += int(now_ms-stamp > MAX_EVIDENCE_AGE_MS)
        elif row.get("model_version") == grade.MODEL_ID_TAPE:
            audit = audit_by_key.get(k, {})
            audit_stamp = _number(audit.get("audit_started_ms"))
            audit_score = _number(audit.get("current_tape_score"))
            closed = _number(audit.get("clean_closed_episodes"))
            tokens = _number(audit.get("closed_tokens"))
            if (row["grade"] == "B" and audit.get("current_tape_grade") == "B"
                    and audit_stamp is not None and stamp <= audit_stamp <= now_ms
                    and now_ms-audit_stamp <= MAX_EVIDENCE_AGE_MS
                    and audit_score is not None and abs(audit_score-score) <= 0.0001
                    and closed is not None and closed >= grade.TAPE_MIN_CLOSED_FOR_B
                    and tokens is not None and tokens >= grade.TAPE_MIN_TOKENS_FOR_B
                    and (_number(audit.get("evidence_weight")) or 0) >= grade.MIN_EVIDENCE_WEIGHT):
                category = "sample_checked_tape_B"
        counts[category] += 1
        chain_counts[k[0]][category] += 1
        if category in ("provider_reported_B", "sample_checked_tape_B"):
            qualified.append(k)
    # The same EVM address on two chains still needs TWO independent grades but counts
    # once toward the conservative unique-address milestone. Never lower-case Solana.
    unique_addresses = {address for _, address in qualified}
    return {
        "target": target, "observed_ms": now_ms,
        "stored_ab_chain_address_pairs": stored_ab,
        "categories": dict(counts), "by_chain": {c: dict(n) for c, n in chain_counts.items()},
        "eligible_chain_address_pairs": len(qualified),
        "eligible_unique_addresses": len(unique_addresses),
        "remaining_unique_addresses": max(0, target-len(unique_addresses)),
        "inventory_target_reached": len(unique_addresses) >= target,
        "completion_requires_final_evidence_audit": True,
        "invalid_identity_rows": invalid_identity, "duplicate_keys": len(duplicates),
        "provider_evidence_is_not_reconstructed_history": True,
        "provider_rows_older_than_24h_require_final_refresh": old_provider_rows,
    }
