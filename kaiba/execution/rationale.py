"""Plain-English reasons for every entry and every exit.

The agent already recorded WHAT it did and which gate let it through. It did not record
WHY in terms a person can audit after the fact: which wallets were behind a buy, what the
book looked like, why that size and not another, what the plan was, and -- on the way out
-- what the price actually did between entry and exit.

Two rules run through everything here, both from ``docs/CONTRACT.md``:

* **Unknown is unknown.** Every helper returns ``None``/``"unknown"`` when the evidence is
  absent and the narrative says so out loud. Nothing here substitutes a zero for a missing
  measurement, because a sentence that reads "liquidity $0" when we simply could not read
  it is worse than one that says we could not read it.
* **Money is base units.** Native amounts stay integers. A percentage is derived only when
  both sides of the ratio are known and the denominator is positive.

Nothing in this module writes to the database or calls a provider. It turns evidence that
has already been gathered into sentences, so it can be unit-tested against fixtures and
can never change a trading decision.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

__all__ = [
    "entry_rationale",
    "exit_rationale",
    "format_entry",
    "format_exit",
]

#: Below this move we do not describe a position as having "run"; it is noise on a
#: memecoin book. INVENTED as a reporting threshold only -- nothing trades on it.
MOVED_PCT = Decimal("5")


def _dec(value: Any) -> Decimal | None:
    """A Decimal, or None for anything that is not a usable number. Never raises."""
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d.is_finite() else None


def _pct_change(frm: Any, to: Any) -> Decimal | None:
    """``to`` against ``frm`` as a percentage, or None if either side is unusable.

    A zero or negative base returns None rather than infinity: an entry price of zero is a
    broken record, not a trade that made infinite money.
    """
    a, b = _dec(frm), _dec(to)
    if a is None or b is None or a <= 0:
        return None
    return (b / a - 1) * 100


def _q(value: Decimal | None, suffix: str = "%", places: str = "0.1") -> str:
    if value is None:
        return "unknown"
    return f"{value.quantize(Decimal(places))}{suffix}"


def _usd(value: Any) -> str:
    d = _dec(value)
    if d is None:
        return "unknown"
    if d >= 1000:
        return f"${d.quantize(Decimal('1')):,}"
    return f"${d.quantize(Decimal('0.0001')).normalize()}"


# --------------------------------------------------------------------------------------
# entry
# --------------------------------------------------------------------------------------


def entry_rationale(
    *,
    signal: Any,
    dossier: Any = None,
    token_meta: Any = None,
    size_base_units: int | None = None,
    size_pct_bankroll: Any = None,
    plan: dict[str, Any] | None = None,
    invalidation: str | None = None,
) -> dict[str, Any]:
    """Structured "why we bought", built only from evidence already in hand.

    ``plan`` is the protection that will be armed -- ``stop_price_usd``, ``first_tp_x``,
    ``trail_arms_at_x`` -- so the record says what we intended to do with the position and
    not merely that we opened one. It is optional: a lane that cannot state a plan says so
    rather than having one invented for it.
    """
    payload = dict(getattr(signal, "payload", None) or {})
    wallets = list(getattr(signal, "wallets", None) or [])
    entities = list(getattr(signal, "entities", None) or [])

    liquidity = _dec(_measure_value(dossier, "liquidity_usd"))
    launchpad = str(getattr(token_meta, "launchpad", None) or "").strip() or None

    who: list[str] = []
    if wallets:
        who.append(f"{len(wallets)} smart wallet(s) bought inside the window")
        who.append("wallets: " + ", ".join(w[:10] for w in wallets[:5])
                   + (f" (+{len(wallets) - 5} more)" if len(wallets) > 5 else ""))
    else:
        who.append("no smart wallets named on the signal")
    if entities:
        who.append(f"{len(entities)} independent entit(ies), so this is not one actor")

    what: list[str] = [
        f"launchpad: {launchpad}" if launchpad
        else "manual deploy: no launchpad recorded, so the higher confluence bar applied",
        f"liquidity at entry: {_usd(liquidity)}" if liquidity is not None
        else "liquidity: UNKNOWN (the lane refuses an unreadable book, so this was read)",
    ]
    window_s = getattr(signal, "window_s", None)
    if window_s:
        what.append(f"confluence window: {int(window_s)}s")

    how: list[str] = []
    if size_base_units is not None:
        how.append(f"size: {int(size_base_units):,} base units")
    pct = _dec(size_pct_bankroll)
    if pct is not None:
        how.append(f"{_q(pct * 100 if pct <= 1 else pct)} of bankroll")
    strength = _dec(getattr(signal, "strength", None))
    if strength is not None:
        how.append(f"lane conviction {_q(strength * 100)} (this is conviction, not a win probability)")

    the_plan: list[str] = []
    plan = plan or {}
    stop = _dec(plan.get("stop_price_usd"))
    if stop is not None:
        the_plan.append(f"hard stop at {_usd(stop)}")
    if plan.get("first_tp_x"):
        the_plan.append(f"first take-profit at {plan['first_tp_x']}x")
    if plan.get("trail_arms_at_x"):
        the_plan.append(
            f"trailing stop arms at {plan['trail_arms_at_x']}x -- below that multiple the "
            "hard stop is the ONLY protection this position has"
        )
    if not the_plan:
        the_plan.append("protection plan: not stated at decision time")

    return {
        "why_entered": " | ".join(filter(None, [
            "; ".join(who), "; ".join(what), "; ".join(how) or None,
        ])),
        "plan": "; ".join(the_plan),
        "invalidation": invalidation or "not stated",
        "evidence": {
            "lane": str(getattr(getattr(signal, "lane", None), "value", "") or ""),
            "smart_wallets": len(wallets),
            "independent_entities": len(entities),
            "wallets": wallets[:20],
            "liquidity_usd": str(liquidity) if liquidity is not None else None,
            "liquidity_basis": _measure_basis(dossier, "liquidity_usd"),
            "launchpad": launchpad,
            "deploy": "launchpad" if launchpad else "manual",
            "size_base_units": None if size_base_units is None else int(size_base_units),
            "lane_conviction": None if strength is None else float(strength),
            "reasons": list(getattr(signal, "reasons", None) or []),
            "signal_payload_keys": sorted(payload)[:20],
        },
    }


def _measure_value(dossier: Any, field: str) -> Any:
    m = getattr(dossier, field, None) if dossier is not None else None
    return getattr(m, "value", None) if m is not None else None


def _measure_basis(dossier: Any, field: str) -> str | None:
    m = getattr(dossier, field, None) if dossier is not None else None
    basis = getattr(m, "basis", None) if m is not None else None
    return getattr(basis, "value", None) or (str(basis) if basis is not None else None)


def format_entry(rationale: dict[str, Any]) -> str:
    """One human-readable block. Used in notifications and the worklog."""
    return (
        f"ENTERED because {rationale['why_entered']}\n"
        f"  PLAN: {rationale['plan']}\n"
        f"  WRONG IF: {rationale['invalidation']}"
    )


# --------------------------------------------------------------------------------------
# exit
# --------------------------------------------------------------------------------------

#: Exit reasons that mean "the plan worked", as opposed to a stop or a fault.
PLANNED_PREFIXES = ("take_profit", "tp", "trailing", "trim", "target")
#: Exit reasons that mean something went wrong with the position itself.
FAULT_PREFIXES = ("emergency", "rug", "dust", "blind", "unpriceable", "wallet_empty")


def classify_exit(reason: str | None) -> str:
    r = (reason or "").strip().lower()
    if not r:
        return "unknown"
    if r.startswith(PLANNED_PREFIXES):
        return "planned"
    if r.startswith(FAULT_PREFIXES):
        return "fault"
    if r.startswith("stop"):
        return "stopped"
    return "other"


def exit_rationale(
    *,
    position: Any,
    reason: str | None,
    exit_price_usd: Any = None,
    pct: Any = None,
    threshold: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Structured "why we sold", including what the price did while we held it.

    The path matters more than the reason code. A stop-out that never moved up is a bad
    entry; a stop-out that ran to +47% first and gave it all back is a missing protection
    rung, and the two need different fixes. MEASURED 2026-09-23 over 91 stopped-out live
    fills: 58% had moved UP before they reversed, median +10.3%, and 27% reached +25% or
    better -- all of them with no protection armed, because the first trailing rung sits
    at 2.0x.
    """
    entry = _dec(getattr(position, "entry_price_usd", None))
    peak = _dec(getattr(position, "peak_price_usd", None))
    exit_px = _dec(exit_price_usd)
    cost = _dec(getattr(position, "cost_native", None))
    proceeds = _dec(getattr(position, "proceeds_native", None))
    realized = _dec(getattr(position, "realized_native", None))

    mfe = _pct_change(entry, peak)
    result = _pct_change(entry, exit_px)
    on_money = None
    if cost is not None and cost > 0 and realized is not None:
        on_money = realized / cost * 100

    kind = classify_exit(reason)
    said = {
        "planned": "the plan worked and this leg was taken as designed",
        "stopped": "the stop fired",
        "fault": "something was wrong with the position, not with the price",
        "other": "an exit rule fired",
        "unknown": "the exit reason was not recorded",
    }[kind]

    path: list[str] = []
    if entry is not None:
        path.append(f"entry {_usd(entry)}")
    if peak is not None:
        path.append(f"peak {_usd(peak)}" + (f" ({_q(mfe)} above entry)" if mfe is not None else ""))
    if exit_px is not None:
        path.append(f"exit {_usd(exit_px)}" + (f" ({_q(result)})" if result is not None else ""))
    if not path:
        path.append("no usable price record for this position")

    lesson = None
    if kind == "stopped" and mfe is not None:
        if mfe >= 25:
            lesson = (
                f"this position reached {_q(mfe)} before reversing into the stop. Nothing "
                "protects a position below the first trailing rung, so the whole move was "
                "given back"
            )
        elif mfe >= MOVED_PCT:
            lesson = f"it moved {_q(mfe)} up before reversing"
        else:
            lesson = "it never moved up: this was an entry that did not work, not a stop set too tight"

    sold = f"sold {_q(_dec(pct), suffix='%', places='1')} of the position" if _dec(pct) is not None else "sold"

    return {
        "why_exited": f"{said} ({reason or 'unknown'}); {sold}; " + ", ".join(path),
        "lesson": lesson,
        "outcome": {
            "classification": kind,
            "reason": reason,
            "entry_price_usd": str(entry) if entry is not None else None,
            "peak_price_usd": str(peak) if peak is not None else None,
            "exit_price_usd": str(exit_px) if exit_px is not None else None,
            "mfe_pct": float(mfe) if mfe is not None else None,
            "result_pct_on_price": float(result) if result is not None else None,
            "return_pct_on_cost": float(on_money) if on_money is not None else None,
            "cost_native": int(cost) if cost is not None else None,
            "proceeds_native": int(proceeds) if proceeds is not None else None,
            "realized_native": int(realized) if realized is not None else None,
            "threshold": threshold or {},
        },
    }


def format_exit(rationale: dict[str, Any]) -> str:
    out = f"EXITED because {rationale['why_exited']}"
    o = rationale.get("outcome") or {}
    if o.get("return_pct_on_cost") is not None:
        out += f"\n  RESULT: {o['return_pct_on_cost']:.1f}% on cost"
    if rationale.get("lesson"):
        out += f"\n  READ: {rationale['lesson']}"
    return out
