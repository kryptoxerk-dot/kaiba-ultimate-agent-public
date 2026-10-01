"""Codex GRADE-15: validated provider outcome evidence; no scoring or storage.

GMGN documents the top bucket as P&L >500%, not gross proceeds >5x:
https://github.com/GMGNAI/gmgn-skills/blob/main/skills/gmgn-wallet-score/SKILL.md
The denominator is TOKENS. Nothing here establishes closed episodes, realized-only
returns or complete wallet history. A consumer must retain that distinction.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from kaiba.core.schemas import Chain, EvidenceBasis, Receipt, looks_evm


@dataclass(frozen=True)
class ProviderOutcomes:
    chain: Chain
    address: str
    period: str
    token_count: int
    above_500pct: int
    from_200_to_500pct: int
    from_0_to_200pct: int
    from_minus50_to_0pct: int
    below_minus50pct: int
    average_hold_s: Decimal | None
    receipt: Receipt
    basis: str = "provider_reported_token_distribution_not_closed_episodes"

    @property
    def above_500pct_token_share(self) -> Decimal | None:
        return Decimal(self.above_500pct) / self.token_count if self.token_count else None


def _number(value: Any) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError("missing_or_boolean_number")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("invalid_number") from exc
    if not number.is_finite() or number < 0:
        raise ValueError("nonfinite_or_negative_number")
    return number


def _count(value: Any) -> int:
    number = _number(value)
    if number != number.to_integral_value():
        raise ValueError("fractional_count")
    return int(number)


def extract_provider_outcomes(
    row: dict[str, Any], *, chain: Chain, address: str, period: str, receipt: Receipt
) -> ProviderOutcomes:
    """Parse one response bound to its requested identity, period and receipt.

    ``period`` must come from the original request/cache envelope; never infer it
    from another endpoint. The provider does not echo it inside this response.
    No network requests, labels, score changes, database writes or ROI inference.
    """
    chain = Chain(chain)
    actual = row.get("wallet_address")
    if not isinstance(actual, str) or not isinstance(address, str):
        raise ValueError("wallet_identity")
    if chain != Chain.SOL:
        if not looks_evm(address) or not looks_evm(actual):
            raise ValueError("wallet_identity")
        actual, address = actual.lower(), address.lower()
    if not address or actual != address:
        raise ValueError("wallet_identity")
    if period not in {"1d", "7d", "30d", "all"}:
        raise ValueError("unsupported_period")
    if (receipt.provider != "gmgn" or receipt.endpoint != "portfolio.stats"
            or receipt.basis != EvidenceBasis.PROVIDER_REPORTED):
        raise ValueError("receipt_basis_or_endpoint")
    pnl = row.get("pnl_stat")
    if not isinstance(pnl, dict):
        raise ValueError("missing_distribution")
    names = ("pnl_gt_5x_num", "pnl_2x_5x_num", "pnl_0x_2x_num",
             "pnl_nd5_0x_num", "pnl_lt_nd5_num")
    buckets = [_count(pnl.get(name)) for name in names]
    total = _count(pnl.get("token_num"))
    if sum(buckets) != total:
        raise ValueError("bucket_total_mismatch")
    hold = pnl.get("avg_holding_period")
    return ProviderOutcomes(
        chain, address, period, total, *buckets,
        _number(hold) if hold is not None else None, receipt,
    )
