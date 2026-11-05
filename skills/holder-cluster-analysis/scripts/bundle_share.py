#!/usr/bin/env python3
"""Compute bundled share and currently-held share from a list of early buys.

Stdlib only. Input is JSON on stdin or a file path:

```json
{
  "total_supply": 1000000000,
  "creator": "<address>",
  "create_slot": 292100000,
  "jito_tip_accounts": ["...", "..."],
  "buys": [
    {"wallet": "A", "slot": 292100000, "amount": 21000000, "tip_lamports": 0},
    {"wallet": "B", "slot": 292100000, "amount": 18000000, "tip_lamports": 12000}
  ],
  "holdings": {"A": 21000000, "B": 0}
}
```

`holdings` is the *current* balance per wallet. Leaving a wallet out means unknown, which
is reported separately — it is never treated as zero, because "we could not read it" and
"they sold" are different facts (``docs/CONTRACT.md`` rule 2).

Output: per-slot grouping, the bundle verdict, bundled % and currently-held %.

Thresholds (``../SKILL.md``): a slot with >= 3 non-creator wallets is never chance
(TrenchBot); a bundle needs a Jito tip of >= 1,000 lamports in the slot; bundled or
currently-held share above 20% is a red flag (Solana Tracker), and
``curve-velocity.max_bundler_pct`` in ``config/risk.yaml`` is 20.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from typing import Any

JITO_TIP_MIN_LAMPORTS = 1_000
SAME_BUNDLE_MIN_WALLETS = 2
NEVER_BY_CHANCE_WALLETS = 3
SAME_BUNDLE_MAX_GROUP = 25
RED_FLAG_PCT = 20.0


def analyse(payload: dict[str, Any]) -> dict[str, Any]:
    supply = float(payload.get("total_supply") or 0)
    creator = payload.get("creator")
    buys: list[dict[str, Any]] = payload.get("buys") or []
    holdings: dict[str, Any] = payload.get("holdings") or {}

    by_slot: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for b in buys:
        by_slot[int(b["slot"])].append(b)

    slots: list[dict[str, Any]] = []
    bundled_amount = 0.0
    bundle_wallets: set[str] = set()

    for slot in sorted(by_slot):
        group = by_slot[slot]
        wallets = {b["wallet"] for b in group if b["wallet"] != creator}
        tipped = any(float(b.get("tip_lamports") or 0) >= JITO_TIP_MIN_LAMPORTS for b in group)
        oversized = len(wallets) > SAME_BUNDLE_MAX_GROUP
        is_bundle = (
            len(wallets) >= SAME_BUNDLE_MIN_WALLETS and tipped and not oversized
        )
        note = ""
        if oversized:
            note = "launch stampede, not a bundle (group > 25)"
        elif len(wallets) >= NEVER_BY_CHANCE_WALLETS and not tipped:
            note = "3+ wallets in one slot with no tip: coordinated, tip not observed"
        amount = sum(float(b.get("amount") or 0) for b in group if b["wallet"] != creator)
        if is_bundle:
            bundled_amount += amount
            bundle_wallets |= wallets
        slots.append(
            {
                "slot": slot,
                "non_creator_wallets": len(wallets),
                "tipped": tipped,
                "is_bundle": is_bundle,
                "amount": amount,
                "note": note,
            }
        )

    held = 0.0
    unknown_holdings = sorted(w for w in bundle_wallets if w not in holdings)
    for w in bundle_wallets:
        if w in holdings and holdings[w] is not None:
            held += float(holdings[w])

    def pct(x: float) -> float | None:
        return round(100.0 * x / supply, 4) if supply else None

    bundled_pct = pct(bundled_amount)
    held_pct = pct(held)

    flags: list[str] = []
    for label, value in (("bundled_pct", bundled_pct), ("currently_held_pct", held_pct)):
        if value is not None and value > RED_FLAG_PCT:
            flags.append(f"{label}={value} > {RED_FLAG_PCT} red flag")
    if unknown_holdings:
        flags.append(
            f"currently_held_pct is a floor: {len(unknown_holdings)} bundle wallet(s) unread"
        )
    if supply == 0:
        flags.append("total_supply unknown: percentages unavailable")

    return {
        "slots": slots,
        "bundle_wallets": sorted(bundle_wallets),
        "bundled_amount": bundled_amount,
        "bundled_pct": bundled_pct,
        "currently_held_amount": held,
        "currently_held_pct": held_pct,
        "unknown_holdings": unknown_holdings,
        "flags": flags,
    }


def main(argv: list[str]) -> int:
    src = argv[1] if len(argv) > 1 else "-"
    text = sys.stdin.read() if src == "-" else open(src, encoding="utf-8").read()
    print(json.dumps(analyse(json.loads(text)), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
