#!/usr/bin/env python3
"""Expected value of one airdrop or points programme, in dollars.

Stdlib only, ``Decimal`` for money, so it can be run anywhere without the ``kaiba``
package. It mirrors ``kaiba/hunters/ev.py``; when the two disagree, the module wins.

Every constant is sourced in ``docs/research/06-hunters-airdrops-nft.md`` and repeated in
``../SKILL.md``.

Usage::

    python ev_estimate.py --drop-usd 120 --token-status confirmed \\
        --vested 0.7 --sybil-risk medium --capital-usd 2000 \\
        --lockup-days 90 --gas-usd 25 --hours 6

    python ev_estimate.py --wallets 12 ...   # see what a multi-wallet plan is worth
"""

from __future__ import annotations

import argparse
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")

TGE_SELL_BASE_RATE = Decimal("0.64")       # 64% of recipients sell at TGE
THREE_MONTH_SURVIVAL = Decimal("0.12")     # complement of the 88% three-month decay
DEFAULT_VESTED_FRACTION = Decimal("0.7")   # drops are "often 70% vested"
FALLBACK_VALUE_USD = Decimal("20")         # low end of the typical $20-100 drop
CAPITAL_RATE_ANNUAL = Decimal("0.08")
SYBIL_MULTIPLIER = Decimal("0.2")          # applied to GROSS for any multi-wallet plan
EV_THRESHOLD_USD = Decimal("25")
OPERATOR_HOURLY_USD = Decimal("60")

P_TOKEN = {
    "confirmed": Decimal("0.9"),
    "points": Decimal("0.5"),
    "speculative": Decimal("0.15"),
}

ELIGIBILITY = {
    "low": Decimal("1.0"),
    "medium": Decimal("0.85"),
    "high": Decimal("0.6"),
}


def money(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


def estimate(
    drop_usd: Decimal,
    token_status: str,
    vested: Decimal,
    sybil_risk: str,
    capital_usd: Decimal,
    lockup_days: int,
    gas_usd: Decimal,
    hours: Decimal,
    wallets: int,
    hourly: Decimal,
) -> dict[str, object]:
    p_token = P_TOKEN[token_status]
    eligibility = ELIGIBILITY[sybil_risk]
    unlocked = Decimal("1") - vested

    gross = drop_usd * p_token * eligibility * Decimal(wallets)
    haircut = unlocked * TGE_SELL_BASE_RATE + vested * THREE_MONTH_SURVIVAL
    sybil = SYBIL_MULTIPLIER if wallets > 1 else Decimal("1")

    reward = gross * haircut * sybil

    carry = capital_usd * (Decimal(lockup_days) / Decimal(365)) * CAPITAL_RATE_ANNUAL
    costs = gas_usd + carry + hours * hourly
    net = reward - costs

    notes: list[str] = []
    if wallets > 1:
        notes.append(
            f"{wallets} wallets: gross x{SYBIL_MULTIPLIER} for cluster cuts "
            "(LayerZero/Arbitrum/zkSync/Linea all cut clusters above ~20 addresses, "
            "and blacklists are shared retroactively). Kaiba does not Sybil."
        )
    if token_status != "confirmed":
        notes.append(f"token not confirmed: p_token={p_token}")
    if net < EV_THRESHOLD_USD:
        notes.append(f"below ev_threshold_usd ({EV_THRESHOLD_USD}): do not build a plan")

    return {
        "p_token": str(p_token),
        "eligibility": str(eligibility),
        "haircut": str(haircut.quantize(Decimal("0.0001"))),
        "gross_usd": str(money(gross)),
        "reward_after_haircut_usd": str(money(reward)),
        "cost_gas_usd": str(money(gas_usd)),
        "cost_capital_carry_usd": str(money(carry)),
        "cost_operator_usd": str(money(hours * hourly)),
        "costs_usd": str(money(costs)),
        "ev_usd": str(money(net)),
        "verdict": "BUILD A PLAN" if net >= EV_THRESHOLD_USD else "SKIP",
        "notes": notes,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--drop-usd", type=Decimal, default=FALLBACK_VALUE_USD)
    p.add_argument("--token-status", choices=sorted(P_TOKEN), default="points")
    p.add_argument("--vested", type=Decimal, default=DEFAULT_VESTED_FRACTION)
    p.add_argument("--sybil-risk", choices=sorted(ELIGIBILITY), default="medium")
    p.add_argument("--capital-usd", type=Decimal, default=Decimal("0"))
    p.add_argument("--lockup-days", type=int, default=0)
    p.add_argument("--gas-usd", type=Decimal, default=Decimal("0"))
    p.add_argument("--hours", type=Decimal, default=Decimal("0"))
    p.add_argument("--hourly", type=Decimal, default=OPERATOR_HOURLY_USD)
    p.add_argument("--wallets", type=int, default=1, help="1 is the only supported plan")
    a = p.parse_args()

    result = estimate(
        a.drop_usd, a.token_status, a.vested, a.sybil_risk, a.capital_usd,
        a.lockup_days, a.gas_usd, a.hours, a.wallets, a.hourly,
    )
    width = max(len(k) for k in result)
    for k, v in result.items():
        if k == "notes":
            for note in v:  # type: ignore[union-attr]
                print(f"{'note'.rjust(width)}  {note}")
        else:
            print(f"{k.rjust(width)}  {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
