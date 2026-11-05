"""Bundle-adjusted holder concentration: how much supply one *hand* controls.

Raw top-10 concentration treats ten addresses funded from one source as ten holders. It
is the number our dossier uses today and it is the weakest variable we implement: a 6
percentage-point discriminative gap, against **24pp for the bundle-adjusted figure**
(``docs/EDGE-AND-VARIABLES.md`` §1, variables #5 and #8). Worse, no published study
establishes any threshold for the raw number, so the 35% line in ``dyor.py`` is invented.
Collapsing addresses into entities before counting is what makes the measurement mean
something, and it is a cached graph query rather than new data.

Three rules the contract already imposes and this module exists to honour:

* **Count entities, not addresses.** Resolution is delegated to
  :mod:`kaiba.intelligence.entity`, which is the module that decides who is one hand and
  is deliberately biased to under-merge. Nothing here reimplements clustering.
* **Report both numbers.** The raw figure, the adjusted figure and the delta between
  them are all returned. The delta *is* the signal: a 40% raw top-10 that collapses to
  one entity is a different token from a 40% raw top-10 spread over ten strangers, and
  substituting one number for the other silently would hide exactly that.
* **Never dress the raw number up as adjusted.** The cluster graph is empty today and
  stays empty until the funding backfill lands. In that state ``adjusted`` is a
  ``Measure`` with ``value=None`` and ``basis=UNAVAILABLE``; the raw figure is still
  returned, plainly labelled, so a caller has something to size on and no way to mistake
  it for the evidenced measurement.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from kaiba.core.db import get_conn
from kaiba.core.schemas import Chain, EvidenceBasis, Measure, Receipt, now_ms
from kaiba.intelligence.entity import SOLO_PREFIX, entity_ids_for
from kaiba.intelligence.hubs import hub_set, safe_normalize

log = logging.getLogger(__name__)

DEFAULT_TOP_N = 10

#: Freshness budget for a derived graph number. The entity graph is rebuilt nightly, so
#: an answer older than a day is a stale read of a graph that has since moved.
FRESHNESS_BUDGET_S = 86_400

#: Addresses that hold supply but are not holders. The bonding curve, the AMM pool and
#: any vesting contract are token-specific and must be passed in by the caller; these are
#: the chain-wide ones that are always wrong to count.
BURN_ADDRESSES: dict[Chain, frozenset[str]] = {
    Chain.SOL: frozenset(
        {
            "11111111111111111111111111111111",
            "1nc1nerator11111111111111111111111111111111",
        }
    ),
}
_EVM_BURN = frozenset(
    {
        "0x0000000000000000000000000000000000000000",
        "0x000000000000000000000000000000000000dead",
    }
)

ZERO = Decimal(0)
HUNDRED = Decimal(100)


@dataclass(frozen=True, slots=True)
class Holding:
    """One address's share of supply, as a percentage in 0..100."""

    address: str
    pct: Decimal


@dataclass(frozen=True, slots=True)
class EntityShare:
    """One resolved operator's share of supply."""

    entity_id: str
    pct: Decimal
    members: tuple[str, ...]

    @property
    def clustered(self) -> bool:
        """False for a lone address we have never linked to anything."""
        return not self.entity_id.startswith(SOLO_PREFIX)

    @property
    def size(self) -> int:
        return len(self.members)


@dataclass(frozen=True)
class ConcentrationReport:
    """Both numbers, the gap between them, and what the gap is made of."""

    chain: Chain
    token: str
    top_n: int
    raw: Measure
    adjusted: Measure
    delta: Measure
    holder_count: int
    entity_count: int | None
    graph_entity_count: int
    adjustment_available: bool
    collapsed_addresses: int
    largest_entity: EntityShare | None
    top_entities: tuple[EntityShare, ...] = ()
    excluded: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()
    note: str | None = None

    @property
    def raw_pct(self) -> Decimal | None:
        return self.raw.value

    @property
    def adjusted_pct(self) -> Decimal | None:
        return self.adjusted.value

    @property
    def delta_pct(self) -> Decimal | None:
        return self.delta.value

    @property
    def best(self) -> Measure:
        """The adjusted figure when we have it, the raw one when we do not.

        A caller that wants one number should use this and read ``.basis``: it is
        ``DERIVED`` when the bundle adjustment actually ran and ``ESTIMATED`` when this is
        the unadjusted top-N standing in for it.
        """
        if self.adjusted.known:
            return self.adjusted
        if not self.raw.known:
            return Measure.unknown(FRESHNESS_BUDGET_S)
        return Measure(
            value=self.raw.value,
            basis=EvidenceBasis.ESTIMATED,
            receipt=self.raw.receipt,
            freshness_budget_s=FRESHNESS_BUDGET_S,
        )

    def summary(self) -> str:
        raw = "n/a" if self.raw_pct is None else f"{self.raw_pct:.2f}%"
        if not self.adjustment_available:
            return f"top{self.top_n} raw {raw}; bundle adjustment unavailable ({self.note})"
        adj = "n/a" if self.adjusted_pct is None else f"{self.adjusted_pct:.2f}%"
        delta = "n/a" if self.delta_pct is None else f"{self.delta_pct:+.2f}pp"
        return (
            f"top{self.top_n} raw {raw} -> adjusted {adj} ({delta}), "
            f"{self.collapsed_addresses} addresses collapsed"
        )


# ------------------------------------------------------------------------- input parsing


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return None if out.is_nan() or out.is_infinite() else out


#: Keys real providers use for the holder address and its share. GMGN, Helius and
#: RugCheck all disagree, and none of them is this module's business to import.
_ADDRESS_KEYS = ("address", "wallet_address", "owner", "account", "holder", "wallet", "pubkey")
_PCT_KEYS = ("amount_percentage", "percentage", "pct", "supply_pct", "share", "percent", "uiPct")
_AMOUNT_KEYS = ("amount", "balance", "uiAmount", "ui_amount", "raw_amount", "token_amount")


def holdings_from_percentages(
    holders: Mapping[str, Any] | Iterable[tuple[str, Any]],
    chain: Chain = Chain.SOL,
    *,
    as_fraction: bool | None = None,
) -> list[Holding]:
    """Build holdings from address -> share of supply.

    Providers disagree on the unit and do not say which they use: GMGN's
    ``amount_percentage`` is a *fraction* (0.1308 means 13.08%), others report percents.
    Reading one as the other is a silent 100x error, and the dangerous direction is
    understating — a 13% holder read as 0.13% looks safe. So the unit is decided by a
    rule that cannot land there:

    1. any value above 1 must be a percent, because a fraction cannot exceed 1;
    2. otherwise assume fractions, then check: if scaling by 100 would put the total
       above 100%, the assumption was wrong and it was percents all along.

    Step 2's check is what removes the genuinely ambiguous band, where every holder is
    under 1% and the list could be read either way. Pass ``as_fraction`` to skip the
    inference when the caller knows.
    """
    pairs = list(holders.items()) if isinstance(holders, Mapping) else list(holders)
    parsed: list[tuple[str, Decimal]] = []
    for address, value in pairs:
        pct = _decimal(value)
        if not address or pct is None or pct <= ZERO:
            continue
        parsed.append((str(address), pct))
    if parsed:
        if as_fraction is None:
            biggest = max(p for _, p in parsed)
            scaled_total = sum((p for _, p in parsed), ZERO) * HUNDRED
            as_fraction = biggest <= Decimal(1) and scaled_total <= Decimal("100.5")
        if as_fraction:
            parsed = [(a, p * HUNDRED) for a, p in parsed]
    return _collapse(parsed, chain)


def holdings_from_amounts(
    holders: Mapping[str, Any] | Iterable[tuple[str, Any]],
    total_supply: int | Decimal,
    chain: Chain = Chain.SOL,
) -> list[Holding]:
    """Build holdings from address -> base-unit amount, plus the supply to divide by."""
    supply = _decimal(total_supply)
    if supply is None or supply <= ZERO:
        return []
    pairs = list(holders.items()) if isinstance(holders, Mapping) else list(holders)
    parsed: list[tuple[str, Decimal]] = []
    for address, value in pairs:
        amount = _decimal(value)
        if not address or amount is None or amount <= ZERO:
            continue
        parsed.append((str(address), amount / supply * HUNDRED))
    return _collapse(parsed, chain)


def holdings_from_rows(
    rows: Sequence[Mapping[str, Any]],
    chain: Chain = Chain.SOL,
    *,
    total_supply: int | Decimal | None = None,
    as_fraction: bool | None = None,
) -> list[Holding]:
    """Best-effort parse of a provider's holder list. Returns ``[]`` rather than guessing.

    Prefers an explicit percentage field; falls back to a raw amount only when
    ``total_supply`` is supplied, because an amount without a denominator is not a share
    and inventing one would produce a concentration figure out of nothing.
    """
    pct_pairs: list[tuple[str, Any]] = []
    amount_pairs: list[tuple[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        address = next((str(row[k]) for k in _ADDRESS_KEYS if row.get(k)), None)
        if not address:
            continue
        pct = next((row[k] for k in _PCT_KEYS if row.get(k) is not None), None)
        if pct is not None:
            pct_pairs.append((address, pct))
            continue
        amount = next((row[k] for k in _AMOUNT_KEYS if row.get(k) is not None), None)
        if amount is not None:
            amount_pairs.append((address, amount))
    if pct_pairs:
        return holdings_from_percentages(pct_pairs, chain, as_fraction=as_fraction)
    if amount_pairs and total_supply is not None:
        return holdings_from_amounts(amount_pairs, total_supply, chain)
    return []


def _collapse(pairs: Sequence[tuple[str, Decimal]], chain: Chain) -> list[Holding]:
    """Normalise addresses and sum duplicates, then sort largest first."""
    totals: dict[str, Decimal] = {}
    for address, pct in pairs:
        key = safe_normalize(address, chain)
        if not key:
            continue
        totals[key] = totals.get(key, ZERO) + pct
    return sorted(
        (Holding(a, p) for a, p in totals.items()), key=lambda h: (-h.pct, h.address)
    )


# --------------------------------------------------------------------------- measurement


def _receipt(endpoint: str, basis: EvidenceBasis) -> Receipt:
    return Receipt(
        provider="kaiba.intelligence", endpoint=endpoint, observed_at_ms=now_ms(), basis=basis
    )


def _measure(value: Decimal | None, endpoint: str, basis: EvidenceBasis) -> Measure:
    if value is None:
        return Measure.unknown(FRESHNESS_BUDGET_S)
    return Measure(
        value=value,
        basis=basis,
        receipt=_receipt(endpoint, basis),
        freshness_budget_s=FRESHNESS_BUDGET_S,
    )


def default_exclusions(chain: Chain) -> frozenset[str]:
    """Burn and system addresses that hold supply but are not holders of it."""
    if chain in BURN_ADDRESSES:
        return BURN_ADDRESSES[chain]
    return _EVM_BURN


def resolve_entities(
    chain: Chain,
    holdings: Sequence[Holding],
    conn: sqlite3.Connection | None = None,
) -> tuple[list[EntityShare], int]:
    """Collapse holdings into entities. Returns ``(shares, addresses collapsed)``.

    An address the graph does not know becomes its own single-member entity. That is the
    same convention :func:`kaiba.intelligence.entity.independent_entity_count` uses, and
    it is the conservative one: absence of a link is not evidence of independence, but
    inventing a link is worse.
    """
    c = conn if conn is not None else get_conn()
    mapping = entity_ids_for(chain, [h.address for h in holdings], c)
    totals: dict[str, Decimal] = {}
    members: dict[str, list[str]] = {}
    for holding in holdings:
        key = mapping.get(holding.address) or f"{SOLO_PREFIX}{holding.address}"
        totals[key] = totals.get(key, ZERO) + holding.pct
        members.setdefault(key, []).append(holding.address)
    shares = [
        EntityShare(entity_id=key, pct=total, members=tuple(sorted(members[key])))
        for key, total in totals.items()
    ]
    shares.sort(key=lambda s: (-s.pct, s.entity_id))
    collapsed = sum(len(s.members) - 1 for s in shares if len(s.members) > 1)
    return shares, collapsed


def concentration(
    chain: Chain,
    token: str,
    holdings: Sequence[Holding],
    conn: sqlite3.Connection | None = None,
    *,
    top_n: int = DEFAULT_TOP_N,
    exclude: Iterable[str] = (),
    exclude_hubs: bool = False,
) -> ConcentrationReport:
    """Raw and bundle-adjusted top-``N`` concentration for one token.

    ``exclude`` must carry the token's bonding curve, AMM pool and any vesting or
    treasury contract: those hold supply without being holders of it, and leaving them in
    makes every freshly launched token look maximally concentrated. Chain-wide burn
    addresses are removed automatically.
    """
    c = conn if conn is not None else get_conn()
    token = safe_normalize(token, chain)
    drop = {safe_normalize(a, chain) for a in exclude if a} | default_exclusions(chain)
    if exclude_hubs:
        # Kinds from kaiba.intelligence.hubs.HUB_KINDS. Only the two that genuinely hold
        # other people's supply: a router or a program balance is not a holder either,
        # but excluding them hides nothing a caller would want to see.
        drop |= hub_set(chain, c, kind="cex") | hub_set(chain, c, kind="pool")
    removed = tuple(sorted(h.address for h in holdings if h.address in drop))
    kept = [h for h in holdings if h.address not in drop]

    if not kept:
        return ConcentrationReport(
            chain=chain, token=token, top_n=top_n,
            raw=Measure.unknown(FRESHNESS_BUDGET_S),
            adjusted=Measure.unknown(FRESHNESS_BUDGET_S),
            delta=Measure.unknown(FRESHNESS_BUDGET_S),
            holder_count=0, entity_count=None, graph_entity_count=0,
            adjustment_available=False, collapsed_addresses=0, largest_entity=None,
            excluded=removed, unknowns=("holders",),
            note="no holder list: concentration is unknown, not zero",
        )

    raw_value = sum((h.pct for h in kept[:top_n]), ZERO)
    raw = _measure(raw_value, "concentration.raw", EvidenceBasis.DERIVED)

    graph_size = _graph_entity_count(chain, c)
    if graph_size == 0:
        return ConcentrationReport(
            chain=chain, token=token, top_n=top_n,
            raw=raw,
            adjusted=Measure.unknown(FRESHNESS_BUDGET_S),
            delta=Measure.unknown(FRESHNESS_BUDGET_S),
            holder_count=len(kept), entity_count=None, graph_entity_count=0,
            adjustment_available=False, collapsed_addresses=0, largest_entity=None,
            excluded=removed, unknowns=("bundle_adjustment",),
            note=(
                "cluster graph is empty for this chain, so the bundle adjustment could not "
                "run; the raw top-N is reported unadjusted and must not be read as adjusted"
            ),
        )

    shares, collapsed = resolve_entities(chain, kept, c)
    adjusted_value = sum((s.pct for s in shares[:top_n]), ZERO)
    adjusted = _measure(adjusted_value, "concentration.adjusted", EvidenceBasis.DERIVED)
    delta = _measure(adjusted_value - raw_value, "concentration.delta", EvidenceBasis.DERIVED)
    # ``members`` deliberately lists only the holders that collapsed into the entity, not
    # the entity's full roster: it must be exactly the set ``pct`` is the sum of.
    largest = shares[0] if shares else None
    note = None
    if collapsed == 0:
        note = (
            f"graph holds {graph_size} entities but none of these {len(kept)} holders are "
            "in it, so the adjustment ran and changed nothing"
        )
    return ConcentrationReport(
        chain=chain, token=token, top_n=top_n,
        raw=raw, adjusted=adjusted, delta=delta,
        holder_count=len(kept), entity_count=len(shares), graph_entity_count=graph_size,
        adjustment_available=True, collapsed_addresses=collapsed, largest_entity=largest,
        top_entities=tuple(shares[:top_n]), excluded=removed, note=note,
    )


def _graph_entity_count(chain: Chain, conn: sqlite3.Connection) -> int:
    """How many entities the graph holds for this chain. Zero means no adjustment is possible."""
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM entities WHERE chain=?", (chain.value,)
        ).fetchone()
    except sqlite3.Error as exc:
        log.warning("entity table unreadable, treating the graph as empty: %s", exc)
        return 0
    return int(row["n"]) if row else 0


def from_rows(
    chain: Chain,
    token: str,
    rows: Sequence[Mapping[str, Any]],
    conn: sqlite3.Connection | None = None,
    *,
    total_supply: int | Decimal | None = None,
    top_n: int = DEFAULT_TOP_N,
    exclude: Iterable[str] = (),
) -> ConcentrationReport:
    """One-call convenience: a provider's holder rows straight to a report."""
    return concentration(
        chain, token, holdings_from_rows(rows, chain, total_supply=total_supply), conn,
        top_n=top_n, exclude=exclude,
    )


__all__ = [
    "DEFAULT_TOP_N",
    "ConcentrationReport",
    "EntityShare",
    "Holding",
    "concentration",
    "default_exclusions",
    "from_rows",
    "holdings_from_amounts",
    "holdings_from_percentages",
    "holdings_from_rows",
    "resolve_entities",
]
