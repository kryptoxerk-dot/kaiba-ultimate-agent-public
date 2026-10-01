#!/usr/bin/env python3
"""Apply the dossier blocker table to a JSON dossier and print the verdict.

Standalone on purpose: stdlib only, no imports from ``kaiba``. It is the paper version of
``TokenDossier.tradeable`` so a human (or the agent, offline) can check a dossier by hand
or replay one from a journal entry.

Usage::

    python verdict.py dossier.json
    kaiba token dossier <ca> --json | python verdict.py -

Input is the ``dossier`` body of ``kaiba_token``. Every threshold below is sourced in
``../references/blocker-table.md``. A field that is absent or ``None`` is *unknown* and is
reported as such — it is never read as a passing zero.
"""

from __future__ import annotations

import json
import sys
from typing import Any

#: field -> (comparison, threshold, TokenRisk) for the hard blockers
HARD_NUMERIC: tuple[tuple[str, str, float, str], ...] = (
    ("dev_pct", ">", 10.0, "dev_concentration"),
    ("cluster_pct", ">", 30.0, "cluster_concentration"),
    ("buy_tax_bps", ">", 1000.0, "high_tax"),
    ("sell_tax_bps", ">", 1000.0, "high_tax"),
    ("rug_ratio", ">", 0.30, "rug_history"),
)

#: field -> (comparison, threshold, TokenRisk) for the review conditions
REVIEW_NUMERIC: tuple[tuple[str, str, float, str], ...] = (
    ("cluster_pct", ">", 20.0, "cluster_concentration"),
    ("bundler_pct", ">", 20.0, "bundler_exposure"),
    ("sniper_pct", ">", 10.0, "sniper_exposure"),
    ("top10_pct", ">", 15.0, "top10_concentration"),
)

#: boolean facts that must be explicitly True. None means unknown, which is not False.
HARD_BOOLEAN: tuple[tuple[str, str], ...] = (
    ("mint_authority_revoked", "mint_authority"),
    ("freeze_authority_revoked", "freeze_authority"),
    ("can_sell", "honeypot"),
)

#: fields a decision may not proceed without
REQUIRED = tuple(name for name, _ in HARD_BOOLEAN) + (
    "liquidity_usd",
    "dev_pct",
    "lp_burned_pct",
)


def value_of(dossier: dict[str, Any], field: str) -> float | bool | None:
    """Unwrap a ``Measure`` ({"value": ..., "basis": ...}) or a plain scalar."""
    raw = dossier.get(field)
    if isinstance(raw, dict):
        raw = raw.get("value")
    return raw


def assess(dossier: dict[str, Any]) -> dict[str, Any]:
    blockers: list[str] = []
    review: list[str] = []
    unknowns: list[str] = list(dossier.get("unknowns") or [])

    for field, _op, limit, risk in HARD_NUMERIC:
        v = value_of(dossier, field)
        if v is None:
            continue
        if float(v) > limit:
            blockers.append(f"{risk}: {field}={v} > {limit}")

    for field, risk in HARD_BOOLEAN:
        v = value_of(dossier, field)
        if v is None:
            unknowns.append(field)
        elif v is not True:
            blockers.append(f"{risk}: {field} is not true")

    lp = value_of(dossier, "lp_burned_pct")
    if lp is None:
        unknowns.append("lp_burned_pct")
    elif float(lp) <= 0:
        blockers.append("lp_not_burned: lp_burned_pct=0")

    for field, _op, limit, risk in REVIEW_NUMERIC:
        v = value_of(dossier, field)
        if v is None:
            unknowns.append(field)
        elif float(v) > limit and not any(r.startswith(risk) for r in blockers):
            review.append(f"{risk}: {field}={v} > {limit}")

    for field in REQUIRED:
        if value_of(dossier, field) is None and field not in unknowns:
            unknowns.append(field)

    if unknowns:
        review.append(f"unknown_safety: {len(set(unknowns))} unread field(s)")

    if blockers:
        verdict = "REJECT"
    elif review:
        verdict = "REVIEW"
    else:
        verdict = "PASS"

    return {
        "verdict": verdict,
        "blockers": blockers,
        "review": review,
        "unknowns": sorted(set(unknowns)),
    }


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    text = sys.stdin.read() if argv[1] == "-" else open(argv[1], encoding="utf-8").read()
    payload = json.loads(text)
    # Accept either the kaiba_token envelope or the bare dossier body.
    dossier = payload.get("dossier", payload) if isinstance(payload, dict) else {}
    result = assess(dossier)
    print(json.dumps(result, indent=2))
    return 0 if result["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
