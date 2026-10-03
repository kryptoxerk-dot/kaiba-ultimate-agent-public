"""Token DYOR: merge every safety provider into one :class:`TokenDossier`.

Three providers (GMGN ``token security``, GoPlus, RugCheck) answer overlapping questions
about the same token, and the naive merge — update a dict in provider order — is actively
dangerous. Four rules drive the design, and the tests exist to keep them true.

**1. A field nobody answered is unknown.** "We could not check whether the mint authority
is revoked" and "the mint authority is revoked" are opposite facts. A merge that writes
``False`` or ``0`` for a silent provider turns the second into the first, and that is the
mechanism by which a scanner blesses a rugpull during a provider outage. Every property
here is absent until some provider asserts it, unanswered names land in
``dossier.unknowns``, and missing evidence is subtracted from the grade rather than
scored as a pass.

**2. Disagreement is itself a signal.** When GoPlus says the mint authority is revoked and
RugCheck says it is live, one of them is wrong about a fact that decides whether the
position can be diluted to zero. Last-writer-wins would silently pick whichever adapter
ran second. Instead both claims are kept with their receipts, the dossier takes the
*pessimistic* reading, and ``PROVIDER_CONFLICT`` is raised as a warning — a token whose
own data sources contradict each other is a worse bet than one they agree on, regardless
of which of them is right.

**3. Blockers and warnings are different kinds of statement.** A blocker says the position
can go to zero through an action the issuer takes unilaterally, at any time, independent of
price — there is no size small enough to make that acceptable. A warning says the
distribution of outcomes is worse than it looks, which is a sizing input. Every blocker in
:data:`RULES` carries the one-line reason it is disqualifying, and the reason is attached
to the dossier as a DERIVED receipt so the trace survives outside this process.

**4. Evidence weight enters the grade.** A token three providers cleared and a token nobody
could reach must not come out looking the same. The score is normalised over the components
we could actually measure and then shrunk by how much of the rubric that was, the grade is
gated on a minimum evidence weight, and a scan with *no* security coverage at all is a
blocker in its own right (``UNKNOWN_SAFETY``) so ``dossier.tradeable`` is False.

**5. What we found and how much we looked are two different facts.** Rule 4 was not
enough, and the measurement that proved it is in ``kaiba/intelligence/coverage.py``: over
615 stored dossiers, grade A averaged 6.9 unknown fields and grade B 6.8, with
``bundler_pct`` and ``sniper_pct`` unknown on 100% of them. The score's own evidence
weight is dominated by the three free booleans and by an ``insider_supply`` block that
pays out in full when any one of five supply splits is known, so an A meant "we found
nothing wrong", not "we checked and it is fine". :mod:`kaiba.intelligence.coverage` scores
the *predictive* surface separately, weighted by published separation, and imposes a
**ceiling** on the grade. It never touches the score, because a single number cannot carry
both facts and trying to make it is what produced the bug.

24h USD volume is recovered from the exact token-info cache populated by the existing
GMGN safety read. This adds no network calls and never substitutes interval volume.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from kaiba.core.schemas import (
    EVM_CHAINS,
    Chain,
    EvidenceBasis,
    Grade,
    Measure,
    Receipt,
    TokenDossier,
    TokenRisk,
    normalize_address,
    now_ms,
)
from kaiba.intelligence import coverage as cov

log = logging.getLogger(__name__)

MODEL_ID = "kaiba-dyor-v1"

ZERO = Decimal(0)
HUNDRED = Decimal(100)


# --------------------------------------------------------------------------------------
# the shared property vocabulary
#
# Provider adapters (``goplus.normalize_security``, ``rugcheck.normalize_report``, ...)
# emit dicts keyed by these names. A key they leave out is unknown. A key they set to
# False or 0 is a measured negative, which is a completely different thing.
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class BoolSpec:
    """A boolean property and which of its two values is the unsafe one."""

    unsafe_value: bool
    label: str


@dataclass(frozen=True)
class NumSpec:
    """A numeric property, the direction that is worse, and the disagreement tolerance.

    ``relative`` tolerances are fractions of the larger value, for quantities like
    liquidity where two providers pricing the same pool five percent apart is normal and
    two providers an order of magnitude apart is a real contradiction.
    """

    worse: Literal["max", "min"]
    tolerance: Decimal
    label: str
    relative: bool = False


BOOL_PROPERTIES: dict[str, BoolSpec] = {
    "can_sell": BoolSpec(False, "can_sell"),
    "mint_authority_revoked": BoolSpec(False, "mint_authority_revoked"),
    "freeze_authority_revoked": BoolSpec(False, "freeze_authority_revoked"),
    "balance_mutable": BoolSpec(True, "balance_mutable_authority"),
    "transfer_hook": BoolSpec(True, "transfer_hook"),
    "tax_modifiable": BoolSpec(True, "tax_modifiable"),
    "metadata_mutable": BoolSpec(True, "metadata_mutable"),
    "rugged": BoolSpec(True, "rugged"),
    "dev_sold": BoolSpec(True, "dev_sold"),
    "wash_trading": BoolSpec(True, "wash_trading"),
    "copycat": BoolSpec(True, "copycat"),
    "source_verified": BoolSpec(False, "source_verified"),
    "token_2022": BoolSpec(True, "token_2022"),
}

NUM_PROPERTIES: dict[str, NumSpec] = {
    "buy_tax_bps": NumSpec("max", Decimal(50), "buy_tax_bps"),
    "sell_tax_bps": NumSpec("max", Decimal(50), "sell_tax_bps"),
    "transfer_fee_bps": NumSpec("max", Decimal(10), "transfer_fee_bps"),
    "lp_burned_pct": NumSpec("min", Decimal(5), "lp_burned_pct"),
    "top10_pct": NumSpec("max", Decimal(5), "top10_pct"),
    "dev_pct": NumSpec("max", Decimal(2), "dev_pct"),
    "insider_pct": NumSpec("max", Decimal(5), "insider_pct"),
    "cluster_pct": NumSpec("max", Decimal(5), "cluster_pct"),
    "bundler_pct": NumSpec("max", Decimal(5), "bundler_pct"),
    "sniper_pct": NumSpec("max", Decimal(5), "sniper_pct"),
    "rug_ratio": NumSpec("max", Decimal("0.05"), "rug_ratio"),
    "creator_rug_count": NumSpec("max", Decimal(0), "creator_rug_count"),
    "holder_count": NumSpec("min", Decimal("0.25"), "holder_count", relative=True),
    "liquidity_usd": NumSpec("min", Decimal("0.25"), "liquidity_usd", relative=True),
    "price_usd": NumSpec("min", Decimal("0.10"), "price_usd", relative=True),
    # Derived per-adapter from that adapter's own supply and price -- see
    # `gmgn_cli.normalize_security`. RELATIVE tolerance, like liquidity and price, because
    # two providers pricing the same token a few seconds apart legitimately disagree by a
    # few percent and an absolute band would manufacture a conflict on every scan.
    # Without this entry the claim is dropped by `resolve` and the field stays UNKNOWN,
    # which is what it was on 7,980 of 7,980 dossiers on 2026-09-22.
    "market_cap_usd": NumSpec("min", Decimal("0.25"), "market_cap_usd", relative=True),
    "volume_24h_usd": NumSpec("min", Decimal("0.25"), "volume_24h_usd", relative=True),
}
# ``total_supply`` is intentionally absent: providers report it in different units (UI
# amount vs raw atoms) and cross-provider comparison would invent conflicts. Each adapter
# uses its own supply internally, where the units are known.

TEXT_PROPERTIES: tuple[str, ...] = ("creator", "symbol", "name")

#: Property names that NO provider sends verbatim, so their presence means a body has
#: already been converted to our vocabulary rather than merely sharing a field name.
#:
#: Deliberately not "every property": `symbol`, `name`, `creator`, `holder_count`,
#: `can_sell` and `rug_ratio` are all spelled identically by at least one provider, and
#: including them is what made the `_unwrap_gmgn` short-circuit fire on every raw gmgn
#: body and skip the adapter entirely. See that function.
_CONVERTED_MARKERS: frozenset[str] = frozenset(
    {
        "liquidity_usd",
        "price_usd",
        "market_cap_usd",
        "top10_pct",
        "dev_pct",
        "insider_pct",
        "cluster_pct",
        "bundler_pct",
        "sniper_pct",
        "buy_tax_bps",
        "sell_tax_bps",
        "transfer_fee_bps",
        "lp_burned_pct",
        "mint_authority_revoked",
        "freeze_authority_revoked",
        "metadata_mutable",
        "creator_rug_count",
        "dev_sold",
    }
)

#: The properties that decide whether the token can be exited at all. If not one of them
#: could be established, we did not perform a safety check — we performed a coin flip.
CRITICAL_PROPERTIES: tuple[str, ...] = (
    "can_sell",
    "mint_authority_revoked",
    "freeze_authority_revoked",
)

#: The two entries of :data:`CRITICAL_PROPERTIES` that are SOLANA ACCOUNT AUTHORITIES and
#: do not exist on an EVM chain. :data:`_GMGN_SOL_ONLY_BOOLS` already established this for
#: the provider mapping -- GMGN sends them on EVM hardwired to ``false``, measured 38 of 38,
#: and piping them through produced false blockers on PEPE and SHIB, neither of which has a
#: mint function. This is the same fact applied to the SCORING, which did not have it.
#:
#: MEASURED 2026-09-22: robinhood built 63 dossiers in three hours and graded 34 C, 24 D,
#: 5 QUARANTINED and **zero B**, because ``security_coverage`` read 1 of 3 on every one of
#: them, fired ``partial_security_coverage``, and took the flat 15-point ``UNKNOWN_SAFETY``
#: penalty. A robinhood dossier scored 42.19 against a sol grade-B at 78.0. The token was
#: charged for not answering a question its chain does not ask.
_SOL_ONLY_CRITICAL: frozenset[str] = frozenset(
    {"mint_authority_revoked", "freeze_authority_revoked"}
)


def critical_properties_for(chain: "Chain | None") -> tuple[str, ...]:
    """The safety properties that EXIST on ``chain``.

    ``None`` keeps the strict Solana set: unknown chain means we cannot assert a property
    is inapplicable, and the safe direction for that error is to keep asking for it.

    ``can_sell`` is never dropped. It is the one member of the set that is real on every
    chain, and on EVM it is the check that matters -- a token we cannot sell is the whole
    risk. Dropping the other two does not weaken honeypot, tax, LP-burn, top-10 or dev-share
    checks, all of which are read on robinhood today and none of which are touched here.
    """
    if chain is not None and chain in EVM_CHAINS:
        return tuple(p for p in CRITICAL_PROPERTIES if p not in _SOL_ONLY_CRITICAL)
    return CRITICAL_PROPERTIES


#: Reported in ``dossier.unknowns`` when unanswered. Deliberately not every key in the
#: vocabulary: listing ``wash_trading`` as unknown on every scan trains the reader to skim.
TRACKED_PROPERTIES: tuple[str, ...] = (
    "can_sell",
    "mint_authority_revoked",
    "freeze_authority_revoked",
    "lp_burned_pct",
    "top10_pct",
    "dev_pct",
    "insider_pct",
    "cluster_pct",
    "bundler_pct",
    "sniper_pct",
    "buy_tax_bps",
    "sell_tax_bps",
    "liquidity_usd",
    "volume_24h_usd",
    "holder_count",
    "metadata_mutable",
)


# --------------------------------------------------------------------------------------
# claims and resolution
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Claim:
    """One provider's assertion about one property, with the proof it came from."""

    prop: str
    provider: str
    value: Any
    receipt: Receipt


@dataclass
class Resolution:
    """The merged view: adopted values, every claim behind them, and the disagreements."""

    values: dict[str, Any] = field(default_factory=dict)
    claims: dict[str, list[Claim]] = field(default_factory=dict)
    conflicts: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    receipts: list[Receipt] = field(default_factory=list)
    providers_ok: set[str] = field(default_factory=set)
    providers_down: set[str] = field(default_factory=set)
    #: Which chain this view describes. ``None`` keeps the strict Solana safety set; see
    #: :func:`critical_properties_for`.
    chain: "Chain | None" = None

    def b(self, prop: str) -> bool | None:
        value = self.values.get(prop)
        return value if isinstance(value, bool) else None

    def n(self, prop: str) -> Decimal | None:
        value = self.values.get(prop)
        return value if isinstance(value, Decimal) else None

    def receipt_for(self, prop: str) -> Receipt | None:
        claims = self.claims.get(prop)
        return claims[0].receipt if claims else None

    def basis_for(self, prop: str) -> EvidenceBasis:
        receipt = self.receipt_for(prop)
        if receipt is None:
            return EvidenceBasis.UNAVAILABLE
        return EvidenceBasis.DERIVED if prop in self.conflicts else receipt.basis

    def measure(self, prop: str, budget_s: int = 900) -> Measure:
        value = self.n(prop)
        if value is None:
            return Measure.unknown(budget_s)
        return Measure(
            value=value,
            basis=self.basis_for(prop),
            receipt=self.receipt_for(prop),
            freshness_budget_s=budget_s,
        )

    @property
    def critical_properties(self) -> tuple[str, ...]:
        """The safety properties that exist on this view's chain."""
        return critical_properties_for(self.chain)

    @property
    def security_coverage(self) -> int:
        return sum(1 for p in self.critical_properties if self.values.get(p) is not None)

    @property
    def stale_providers(self) -> list[str]:
        """Providers that answered from an expired cache rather than from the network.

        ``built_at_ms`` says when the *dossier* was assembled, not when the evidence was
        observed, so a scan served entirely from a stale cache would look brand new to the
        execution engine's freshness check. Naming it here is what keeps that honest.
        """
        return sorted(
            {r.provider for r in self.receipts if r.basis is EvidenceBasis.STALE}
        )


def _as_decimal(value: Any) -> Decimal | None:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool) or value is None:
        return None
    try:
        return Decimal(str(value))
    except (ValueError, ArithmeticError):
        return None


def _volume_decimal(value: Any) -> Decimal | None:
    """Only finite, nonnegative USD amounts; JSON numeric strings are provider-native."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    if not re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", str(value)):
        return None
    number = _as_decimal(value)
    return number if number is not None and number.is_finite() and number >= 0 else None


def _disagrees(spec: NumSpec, values: Sequence[Decimal]) -> bool:
    low, high = min(values), max(values)
    gap = high - low
    if not spec.relative:
        return gap > spec.tolerance
    scale = max(abs(high), abs(low))
    return scale > 0 and gap / scale > spec.tolerance


def resolve(claims: Iterable[Claim], *, chain: "Chain | None" = None) -> Resolution:
    """Fold per-provider claims into one view, keeping disagreements visible.

    Booleans resolve to the unsafe value when providers disagree and numbers to the worse
    side, because the cost of the two errors is not symmetric: treating a safe token as
    risky costs a trade we did not take, treating a risky token as safe costs the position.
    """
    grouped: dict[str, list[Claim]] = {}
    for claim in claims:
        if claim.prop == "volume_24h_usd":
            receipt = claim.receipt
            if (
                _volume_decimal(claim.value) is None
                or receipt.basis not in {
                    EvidenceBasis.PROVIDER_REPORTED, EvidenceBasis.CACHED, EvidenceBasis.VERIFIED_ONCHAIN,
                }
                or not (0 < receipt.observed_at_ms <= now_ms())
                or now_ms() - receipt.observed_at_ms > VOLUME_BUDGET_S * 1000
            ):
                continue
        grouped.setdefault(claim.prop, []).append(claim)

    out = Resolution(claims=grouped, chain=chain)
    for prop, items in grouped.items():
        if prop in BOOL_PROPERTIES:
            spec = BOOL_PROPERTIES[prop]
            seen = {bool(c.value) for c in items if isinstance(c.value, bool)}
            if not seen:
                continue
            if len(seen) > 1:
                out.conflicts.append(prop)
                out.values[prop] = spec.unsafe_value
            else:
                out.values[prop] = next(iter(seen))
        elif prop in NUM_PROPERTIES:
            spec = NUM_PROPERTIES[prop]
            if prop == "volume_24h_usd":
                # receipt_for takes the first claim. Keep the selected value and its
                # own timestamp together, not the timestamp of a different provider.
                items.sort(key=lambda c: (_as_decimal(c.value), c.receipt.observed_at_ms))
            numbers = [d for d in (_as_decimal(c.value) for c in items) if d is not None]
            if not numbers:
                continue
            if len(numbers) > 1 and _disagrees(spec, numbers):
                out.conflicts.append(prop)
            out.values[prop] = max(numbers) if spec.worse == "max" else min(numbers)
        elif prop in TEXT_PROPERTIES:
            texts = [str(c.value) for c in items if c.value]
            if texts:
                out.values[prop] = texts[0]

    out.unknown = [p for p in TRACKED_PROPERTIES if out.values.get(p) is None]
    out.conflicts.sort()
    return out


def conflict_notes(resolution: Resolution) -> list[str]:
    """One human line per disagreement, naming every provider and what it claimed."""
    notes: list[str] = []
    for prop in resolution.conflicts:
        said = ", ".join(f"{c.provider}={c.value}" for c in resolution.claims.get(prop, []))
        notes.append(f"providers disagree on {prop}: {said}; adopted {resolution.values.get(prop)}")
    return notes


# --------------------------------------------------------------------------------------
# derived facts
#
# A derivation is only allowed when it follows from facts we hold, never from an absence.
# --------------------------------------------------------------------------------------


def _derive(resolution: Resolution, chain: Chain) -> None:
    values = resolution.values

    # A classic SPL mint cannot carry a Token-2022 extension and has no tax mechanism at
    # all, so "no hook, no fee, no tax" here is a property of the program rather than an
    # absence of evidence. This is the one place an absence is allowed to mean something.
    if values.get("token_2022") is False:
        for prop, value in (
            ("transfer_hook", False),
            ("transfer_fee_bps", ZERO),
            ("buy_tax_bps", ZERO),
            ("sell_tax_bps", ZERO),
        ):
            if values.get(prop) is None:
                values[prop] = value
                _add_derived_claim(
                    resolution, prop, value, "mint is owned by the classic SPL Token program"
                )

    # Solana has no honeypot simulation, so sellability has to be reasoned about. It only
    # follows when *both* of the ways a Solana token can trap a holder are known-absent.
    if (
        chain is Chain.SOL
        and values.get("can_sell") is None
        and values.get("freeze_authority_revoked") is True
        and values.get("transfer_hook") is False
    ):
        values["can_sell"] = True
        _add_derived_claim(
            resolution,
            "can_sell",
            True,
            "freeze authority revoked and no transfer hook: nothing can block the transfer",
        )

    # A Token-2022 transfer fee is charged on the sell leg as surely as an EVM sell tax is.
    fee = resolution.n("transfer_fee_bps")
    if fee is not None and fee > 0 and values.get("sell_tax_bps") is None:
        values["sell_tax_bps"] = fee
        _add_derived_claim(resolution, "sell_tax_bps", fee, "token-2022 transfer fee applies to the sell leg")

    resolution.unknown = [p for p in TRACKED_PROPERTIES if values.get(p) is None]


def _add_derived_claim(resolution: Resolution, prop: str, value: Any, why: str) -> None:
    receipt = Receipt(
        provider="kaiba.dyor",
        endpoint=f"derive.{prop}",
        basis=EvidenceBasis.DERIVED,
        note=f"{prop}={value}: {why}"[:300],
    )
    resolution.claims.setdefault(prop, []).append(Claim(prop, "kaiba.dyor", value, receipt))
    resolution.receipts.append(receipt)


# --------------------------------------------------------------------------------------
# rules
#
# Thresholds marked "mandate" come from docs/PLAN.md §5.5 and are the operator's numbers,
# not tuning knobs.
# --------------------------------------------------------------------------------------


class Severity(StrEnum):
    BLOCKER = "blocker"
    WARNING = "warning"


@dataclass(frozen=True)
class Finding:
    risk: TokenRisk
    severity: Severity
    #: Why this specific property justifies its severity. One line, no hedging.
    reason: str
    detail: str

    def render(self) -> str:
        return f"{self.severity.value} {self.risk.value}: {self.reason} [{self.detail}]"


@dataclass(frozen=True)
class Rule:
    name: str
    risk: TokenRisk
    severity: Severity
    reason: str
    test: Callable[[Resolution], str | None]

    def evaluate(self, resolution: Resolution) -> Finding | None:
        try:
            detail = self.test(resolution)
        except Exception as exc:  # noqa: BLE001 - a broken rule must not break the scan
            log.warning("dyor rule %s raised (%s); treating as not fired", self.name, exc)
            return None
        if detail is None:
            return None
        return Finding(risk=self.risk, severity=self.severity, reason=self.reason, detail=detail)


# --------------------------------------------------------------------------------------
# dev-attributable supply. PLAN §5.5's 10% veto is SUPERSEDED here; read this before
# moving either number back.
#
# **The written mandate.** PLAN §5.5 set the reject at 10% and it was well argued on its
# own terms: the creator is one wallet with no cost basis, so 10% of supply is an exit
# that goes straight through our position before any price action we could react to. That
# reasoning is not wrong and is not deleted -- it is why 10% is still DEV_PCT_WARN below,
# why `kaiba.intelligence.confluence`'s `dev_supply_threshold` still scores against 10,
# and why `kaiba.execution.risk.DEV_SUPPLY_LADDER` starts charging size at 10.
#
# **The later instruction, which wins.** The operator, twice on 2026-09-22, in his own
# words: *"I am okay with trading live pair tokens that went live 1 second / But we need
# to be able to distinct bundled launch and not / if its bundled dev buying more than 20%
# 30% we can still buy but we need to be careful"*. A verbal instruction dated after the
# written plan is the operator's current intent, so above 10% stops being a veto and
# becomes a review. SUPERSEDED 2026-09-22; PLAN §5.5 is the record of what it was.
#
# **Why 30 and not 20 or 25.** It is the top of the band the operator named, and it is
# the number §5.5 *already* uses for the same shape of risk: CLUSTER_PCT_BLOCK = 30, "one
# unexplained cluster over 30% is a single actor wearing many wallets". A creator is that
# same single actor not bothering with the wallets. Refusing him at 10% while refusing his
# disguise at 30% was the inconsistency, and this removes it rather than inventing a third
# number. The comparison is `_over`, i.e. strictly greater, exactly as the cluster rule
# reads it: "more than ... 30%" is more than 30.
#
# INVENTED as a boundary -- no study prices a creator's supply share against a forward
# return, and the one adjacent published number (MELT, arXiv:2602.13480) says only that
# 98.7% of launches involve a developer purchase at all, which says nothing about
# magnitude. What would settle it: forward PnL of our own fills bucketed by dev_pct.
# `learning.outcomes` already reconstructs dev_pct per decision from the stored dossier
# (`dev_supply_threshold`), so the query exists; the SAMPLE does not, because until today
# every token above 10% was refused and therefore never priced. The veto was its own
# evidence blackout, and the first thing this change buys is the data to replace it.
#
# **What this raise actually buys, MEASURED ON THE LIVE BOX 2026-09-22** over the 546
# all-time decisions the 10% veto refused, joined to each token's stored dev_pct:
#
#      49   9.0%  10-20%  -- admitted, at 0.9x  (DEV_SUPPLY_LADDER)
#      29   5.3%  20-30%  -- admitted, at 0.85x
#     416  76.2%  >30%    -- still vetoed
#      24   4.4%  79.0-79.7% -- still vetoed
#      28   5.1%  <=10%   -- the rule fired for another reason, or the dossier moved since
#
# So 78 of 546 (14.3%) are admitted and 440 (80.6%) stay refused, out of a category that
# was ~36% of every refusal the agent made. That is the shape the operator asked for: a
# band that is bought carefully, not a loosening.
#
# **Where the ladder's first rung sits in the population.** Over all 9,809 dossiers on the
# box that carry a dev_pct at all: p50 0.35%, p90 13.79%, 58.1% under 1%. So the 10% rung
# bites at roughly the top decile of tokens and the median token is nowhere near it -- this
# mechanism is not a blanket haircut.
#
# **A correction, kept because the mistake is the reusable part.** These numbers were first
# measured on `data/kaiba.db`, the LOCAL scratch copy, which gave 6.3% admitted and claimed
# 78.8% of refusals sat in 79.0-79.7% -- i.e. that the pump.fun curve's 79.31% of supply
# was being read as the creator's balance. On the live box that band is 24 of 546 (4.4%)
# and 0.2% of all dossiers carrying a dev_pct, so the curve-misread theory is a rounding
# error, not a systematic bug, and the local corpus was not a sample of production at all.
# docs/PLAN.md's local-vs-VPS rule: measure the box the money is on.
# --------------------------------------------------------------------------------------

#: operator 2026-09-22, SUPERSEDING PLAN §5.5's 10%: above this is a reject. INVENTED.
# MEASURED 2026-09-23, population study over 7,738 tokens we hold both a dossier and a
# price tape for -- every token we SCANNED, not the ones we chose to buy, so the gate's own
# decisions do not select the sample. Forward multiple is max tape price after the first
# observation over the first observed price; baseline reach-2x is 26.2%.
#
#     dev_pct <20            n=6974   26.1%   lift 1.00
#     dev_pct 20-30          n= 178   27.0%   lift 1.03   (admitted before this change)
#     dev_pct 30-50          n=  98   37.8%   lift 1.44   (REFUSED before this change)
#     dev_pct >=50           n= 488   25.2%   lift 0.96
#
# The band this blocker was refusing was the best-performing one in the sample, and the
# band above it is ordinary. So the line moves to 50, where the reasoning it was built on
# actually bites: at half the float one actor can exit through any position we could take,
# and the measurement says we are paid nothing for carrying that.
#
# It moves as a SIZE, not a waiver. `risk.DEV_SUPPLY_LADDER` sizes 30-50% at 0.6x, below
# the 0.85x the 20-30% band gets, because reach-2x is not our P&L: we exit on a ladder and
# pay ~6.5% round trip, and a dev holding a third of the float is a real tail whatever the
# median does. n=98 is a real sample and a modest one; this is sized like one.
DEV_PCT_BLOCK = Decimal(50)
#: PLAN §5.5's original veto, kept as the review line: above this is "careful", not "no".
#: It is also where `kaiba.execution.risk.DEV_SUPPLY_LADDER` starts taking size off.
DEV_PCT_WARN = Decimal(10)
#: mandate (PLAN §5.5): unexplained cluster >20% is a review, >30% is a reject.
CLUSTER_PCT_BLOCK = Decimal(30)
CLUSTER_PCT_WARN = Decimal(20)
#: A round trip through a 50% sell tax cannot be profitable at any entry.
TAX_BPS_BLOCK = Decimal(5_000)
TAX_BPS_WARN = Decimal(1_000)
TOP10_PCT_WARN = Decimal(35)
INSIDER_PCT_WARN = Decimal(10)
BUNDLER_PCT_WARN = Decimal(15)
SNIPER_PCT_WARN = Decimal(20)
LP_BURNED_PCT_WARN = Decimal(50)
LOW_LIQUIDITY_USD = Decimal(10_000)


def _is_false(resolution: Resolution, prop: str) -> bool:
    return resolution.b(prop) is False


def _is_true(resolution: Resolution, prop: str) -> bool:
    return resolution.b(prop) is True


def _max_tax(resolution: Resolution) -> Decimal | None:
    taxes = [resolution.n(p) for p in ("buy_tax_bps", "sell_tax_bps", "transfer_fee_bps")]
    known = [t for t in taxes if t is not None]
    return max(known) if known else None


def _over(resolution: Resolution, prop: str, threshold: Decimal) -> str | None:
    value = resolution.n(prop)
    if value is None or value <= threshold:
        return None
    return f"{prop}={value:.2f} > {threshold}"


def _under(resolution: Resolution, prop: str, threshold: Decimal) -> str | None:
    value = resolution.n(prop)
    if value is None or value >= threshold:
        return None
    return f"{prop}={value:.2f} < {threshold}"


RULES: tuple[Rule, ...] = (
    # ---------------------------------------------------------------- blockers
    Rule(
        name="honeypot",
        risk=TokenRisk.HONEYPOT,
        severity=Severity.BLOCKER,
        reason="a token that cannot be sold turns the whole position into a total loss the "
        "moment it is opened; there is no size small enough to survive it",
        test=lambda r: "can_sell=False" if _is_false(r, "can_sell") else None,
    ),
    Rule(
        name="mint_authority_live",
        risk=TokenRisk.MINT_AUTHORITY,
        severity=Severity.BLOCKER,
        reason="a live mint authority lets the issuer print unlimited supply and dilute the "
        "position to zero at will, with no price action we could react to",
        test=lambda r: "mint_authority_revoked=False" if _is_false(r, "mint_authority_revoked") else None,
    ),
    Rule(
        name="balance_mutable",
        risk=TokenRisk.MINT_AUTHORITY,
        severity=Severity.BLOCKER,
        reason="an authority that can rewrite balances is a mint authority under another "
        "name: it can set our holding to zero without a transaction we can front-run",
        test=lambda r: "balance_mutable=True" if _is_true(r, "balance_mutable") else None,
    ),
    Rule(
        name="freeze_authority_live",
        risk=TokenRisk.FREEZE_AUTHORITY,
        severity=Severity.BLOCKER,
        reason="a live freeze authority can freeze our token account, which removes the exit "
        "entirely — an unsellable position is worth nothing regardless of the quoted price. "
        "Kept as a blocker because the downside is total and the check is free, but do not "
        "count it as coverage: measured at only 0.6% of Solana rugs "
        "(docs/research/10-solana-edge-2026.md), so it is catching a thin slice",
        test=lambda r: (
            "freeze_authority_revoked=False" if _is_false(r, "freeze_authority_revoked") else None
        ),
    ),
    Rule(
        name="transfer_hook",
        risk=TokenRisk.TRANSFER_HOOK,
        severity=Severity.BLOCKER,
        reason="a transfer hook is issuer-controlled code on every transfer, so the sell can "
        "be made to fail after we are in and without any on-chain warning",
        test=lambda r: "transfer_hook=True" if _is_true(r, "transfer_hook") else None,
    ),
    Rule(
        name="tax_modifiable",
        risk=TokenRisk.HIGH_TAX,
        severity=Severity.BLOCKER,
        reason="an owner-modifiable tax means today's 0% sell tax is not a commitment; it can "
        "be raised to 100% once we hold, which is a honeypot with a delay",
        test=lambda r: "tax_modifiable=True" if _is_true(r, "tax_modifiable") else None,
    ),
    Rule(
        name="tax_confiscatory",
        risk=TokenRisk.HIGH_TAX,
        severity=Severity.BLOCKER,
        reason="a tax at or above 50% cannot be recovered by any realistic move, so the round "
        "trip is a guaranteed loss rather than a risk",
        test=lambda r: (
            f"max_tax_bps={_max_tax(r)}" if (_max_tax(r) or ZERO) >= TAX_BPS_BLOCK else None
        ),
    ),
    Rule(
        name="dev_concentration",
        risk=TokenRisk.DEV_CONCENTRATION,
        severity=Severity.BLOCKER,
        reason="dev-attributable supply above 50% means the creator alone can exit through our "
        "entire position before we can react. 50 and not PLAN §5.5's 10: the operator's later "
        "instruction (2026-09-22) is that 20-30% is bought carefully rather than refused, and 30 "
        "is already this mandate's line for one unexplained actor holding the float "
        "(CLUSTER_PCT_BLOCK). Below it the cut is a size, not a veto — see "
        "kaiba.execution.risk.DEV_SUPPLY_LADDER",
        test=lambda r: _over(r, "dev_pct", DEV_PCT_BLOCK),
    ),
    Rule(
        name="cluster_concentration_reject",
        risk=TokenRisk.CLUSTER_CONCENTRATION,
        severity=Severity.BLOCKER,
        reason="mandate: one unexplained cluster holding over 30% is a single actor wearing "
        "many wallets, and their exit is the whole float",
        test=lambda r: _over(r, "cluster_pct", CLUSTER_PCT_BLOCK),
    ),
    Rule(
        name="already_rugged",
        risk=TokenRisk.RUG_HISTORY,
        severity=Severity.BLOCKER,
        reason="the token is flagged as already rugged; there is no thesis left, only an exit "
        "queue we would be joining at the back of",
        test=lambda r: "rugged=True" if _is_true(r, "rugged") else None,
    ),
    Rule(
        name="no_security_coverage",
        risk=TokenRisk.UNKNOWN_SAFETY,
        severity=Severity.BLOCKER,
        reason="not one provider could establish sellability or either authority, so every "
        "safety property is an assumption — this is a bet on the outage, not on the token",
        test=lambda r: (
            f"security_coverage=0/{len(r.critical_properties)} providers_down="
            f"{','.join(sorted(r.providers_down)) or 'none'}"
            if r.security_coverage == 0
            else None
        ),
    ),
    # ---------------------------------------------------------------- warnings
    Rule(
        name="partial_security_coverage",
        risk=TokenRisk.UNKNOWN_SAFETY,
        severity=Severity.WARNING,
        reason="part of the safety surface could not be checked, so the clean result covers "
        "less ground than it appears to",
        test=lambda r: (
            f"security_coverage={r.security_coverage}/{len(r.critical_properties)} "
            f"unknown={','.join(p for p in r.critical_properties if r.values.get(p) is None)}"
            if 0 < r.security_coverage < len(r.critical_properties)
            else None
        ),
    ),
    Rule(
        name="unverified_source",
        risk=TokenRisk.UNKNOWN_SAFETY,
        severity=Severity.WARNING,
        reason="the contract source is not verified, so the honeypot verdict is a simulation "
        "over bytecode nobody has read",
        test=lambda r: "source_verified=False" if _is_false(r, "source_verified") else None,
    ),
    Rule(
        name="stale_evidence",
        risk=TokenRisk.UNKNOWN_SAFETY,
        severity=Severity.WARNING,
        reason="part of this dossier was served from an expired cache, so it is newer than "
        "the facts in it and the engine's freshness check does not cover them",
        test=lambda r: (
            f"stale_providers={','.join(r.stale_providers)}" if r.stale_providers else None
        ),
    ),
    Rule(
        name="provider_conflict",
        risk=TokenRisk.PROVIDER_CONFLICT,
        severity=Severity.WARNING,
        reason="the sources contradict each other on a fact one of them must have wrong, so "
        "the whole dossier is less reliable than any single clean read suggests",
        test=lambda r: "; ".join(conflict_notes(r))[:400] or None,
    ),
    Rule(
        name="dev_concentration_review",
        risk=TokenRisk.DEV_CONCENTRATION,
        severity=Severity.WARNING,
        reason="the creator holds over 10% of supply: PLAN §5.5 made that a reject and the "
        "operator's later instruction (2026-09-22) makes it a review — we can still buy, but "
        "smaller and with the reason on the record. THRESHOLD IS INVENTED: no study prices a "
        "creator's supply share against a forward return; what would settle it is our own fills "
        "bucketed by dev_pct, which the 10% veto was preventing us from ever collecting. The "
        "size this costs is kaiba.execution.risk.DEV_SUPPLY_LADDER; a warning alone is not it",
        test=lambda r: _over(r, "dev_pct", DEV_PCT_WARN),
    ),
    Rule(
        name="cluster_concentration_review",
        risk=TokenRisk.CLUSTER_CONCENTRATION,
        severity=Severity.WARNING,
        reason="mandate: an unexplained cluster over 20% is a review — size down until the "
        "cluster is explained",
        test=lambda r: _over(r, "cluster_pct", CLUSTER_PCT_WARN),
    ),
    Rule(
        name="top10_concentration",
        risk=TokenRisk.TOP10_CONCENTRATION,
        severity=Severity.WARNING,
        reason="a concentrated top ten can move the price more than our exit can, which is a "
        "sizing constraint rather than a disqualification. THRESHOLD IS INVENTED: no "
        "published study establishes any holder-concentration threshold. The evidenced "
        "measure is bundle-adjusted concentration (24pp discriminative gap against 6pp for "
        "raw top ten), which is a different number — see kaiba/intelligence/concentration.py",
        test=lambda r: _over(r, "top10_pct", TOP10_PCT_WARN),
    ),
    Rule(
        name="insider_exposure",
        risk=TokenRisk.INSIDER_EXPOSURE,
        severity=Severity.WARNING,
        reason="insider-held supply is the first to sell into strength; it caps the realistic "
        "upside rather than removing it",
        test=lambda r: _over(r, "insider_pct", INSIDER_PCT_WARN),
    ),
    Rule(
        name="bundler_exposure",
        risk=TokenRisk.BUNDLER_EXPOSURE,
        severity=Severity.WARNING,
        reason="bundled supply entered at the same slot at the same price and will exit the "
        "same way, so the float is thinner than the holder count implies",
        test=lambda r: _over(r, "bundler_pct", BUNDLER_PCT_WARN),
    ),
    Rule(
        name="sniper_exposure",
        risk=TokenRisk.SNIPER_EXPOSURE,
        severity=Severity.WARNING,
        reason="sniper supply is short-horizon inventory that sells into the first move we "
        "would be buying",
        test=lambda r: _over(r, "sniper_pct", SNIPER_PCT_WARN),
    ),
    Rule(
        name="lp_not_burned",
        risk=TokenRisk.LP_NOT_BURNED,
        severity=Severity.WARNING,
        reason="unburned liquidity can be pulled, but locks and vesting are legitimate, so "
        "this sizes the position down instead of refusing it",
        test=lambda r: _under(r, "lp_burned_pct", LP_BURNED_PCT_WARN),
    ),
    Rule(
        name="high_tax",
        risk=TokenRisk.HIGH_TAX,
        severity=Severity.WARNING,
        reason="a tax over 10% raises the move needed to break even, which is an expected-value "
        "adjustment rather than a trap",
        test=lambda r: (
            f"max_tax_bps={_max_tax(r)}"
            if TAX_BPS_WARN <= (_max_tax(r) or ZERO) < TAX_BPS_BLOCK
            else None
        ),
    ),
    Rule(
        name="transfer_fee",
        risk=TokenRisk.TRANSFER_FEE,
        severity=Severity.WARNING,
        reason="a token-2022 transfer fee is charged on every leg and quietly changes the "
        "break-even, so it must be priced in rather than ignored",
        test=lambda r: _over(r, "transfer_fee_bps", ZERO),
    ),
    Rule(
        name="metadata_mutable",
        risk=TokenRisk.METADATA_MUTABLE,
        severity=Severity.WARNING,
        reason="mutable metadata lets the token rename itself into a different narrative after "
        "we buy, which breaks the thesis without breaking the chart",
        test=lambda r: "metadata_mutable=True" if _is_true(r, "metadata_mutable") else None,
    ),
    Rule(
        name="low_liquidity",
        risk=TokenRisk.LOW_LIQUIDITY,
        severity=Severity.WARNING,
        reason="thin liquidity means our own exit is the adverse move; it bounds the size, not "
        "the decision",
        test=lambda r: _under(r, "liquidity_usd", LOW_LIQUIDITY_USD),
    ),
    Rule(
        name="dev_sold",
        risk=TokenRisk.DEV_SOLD,
        severity=Severity.WARNING,
        reason="a creator who has already sold has no remaining incentive to support the "
        "token, which shortens every holding period",
        test=lambda r: "dev_sold=True" if _is_true(r, "dev_sold") else None,
    ),
    Rule(
        name="creator_rug_history",
        risk=TokenRisk.RUG_HISTORY,
        severity=Severity.WARNING,
        reason="the creator has shipped a honeypot or rug before, which raises the base rate "
        "for this launch without proving anything about it",
        test=lambda r: _over(r, "creator_rug_count", ZERO),
    ),
    Rule(
        name="copycat",
        risk=TokenRisk.UNKNOWN_SAFETY,
        severity=Severity.WARNING,
        # A warning rather than a blocker, deliberately. The 10.7x graduation separation
        # was measured on a corpus whose copycat rate was 10.2%; ours measures 46.4% on
        # live 2026 launches, so the separation is not established for today's market.
        # This fires only on a content-hash match, which is the defensible subset.
        reason="the image or metadata content hash is reused from an earlier mint, which "
        "on the CCS'26 corpus separated 9.20% graduation from 0.86% — treat as a strong "
        "size-down until someone runs the forward test on 2026 data",
        test=lambda r: "copycat=True" if _is_true(r, "copycat") else None,
    ),
    Rule(
        name="wash_trading",
        risk=TokenRisk.WASH_TRADING,
        severity=Severity.WARNING,
        # Deliberately narrowed, because the naive version of this rule had the sign wrong.
        # Wash trading *positively* predicts graduation: 2.0% against 0.90% for coins with
        # none, p = 3e-59 across 15.2M pump.fun coins (CCS'26, arXiv 2609.10246). Penalising
        # it as adverse was scoring against the evidence. What survives is the honest part:
        # washed volume inflates the depth reading we would size from. So this is a
        # *sizing* warning about liquidity quality, not a judgement on the token, and it
        # must never contribute to a blocker.
        reason="washed volume inflates the depth we would size from, so discount liquidity. "
        "Note it does NOT predict failure — measured at 2.0% graduation versus 0.90% "
        "without, so treat it as a sizing input only",
        test=lambda r: "wash_trading=True" if _is_true(r, "wash_trading") else None,
    ),
)


def evaluate(resolution: Resolution) -> list[Finding]:
    """Run every rule. A risk that blocks is not also reported as a warning."""
    findings = [f for f in (rule.evaluate(resolution) for rule in RULES) if f is not None]
    blocked = {f.risk for f in findings if f.severity is Severity.BLOCKER}
    return [f for f in findings if f.severity is Severity.BLOCKER or f.risk not in blocked]


def blocker_reasons() -> dict[TokenRisk, list[str]]:
    """Every blocking risk mapped to the reasons it is disqualifying. Used by the tests."""
    out: dict[TokenRisk, list[str]] = {}
    for rule in RULES:
        if rule.severity is Severity.BLOCKER:
            out.setdefault(rule.risk, []).append(rule.reason)
    return out


# --------------------------------------------------------------------------------------
# scoring
#
# Same shape as kaiba.intelligence.grade: components are normalised over the evidence that
# existed, and the grade is then gated on how much evidence that was.
# --------------------------------------------------------------------------------------

COMPONENT_MAX: dict[str, float] = {
    "sell_safety": 18.0,
    "mint_authority": 14.0,
    "freeze_authority": 12.0,
    "lp_security": 12.0,
    "holder_distribution": 12.0,
    "insider_supply": 14.0,
    "taxes": 8.0,
    "liquidity": 10.0,
}

#: Below this much of the rubric we do not have an opinion; saying "C" would be a lie.
MIN_EVIDENCE_WEIGHT = 40.0
#: An A additionally needs most of the rubric to have been measurable.
A_MIN_EVIDENCE_WEIGHT = 80.0
A_MIN_SCORE = 75.0
B_MIN_SCORE = 55.0
C_MIN_SCORE = 35.0

#: Even a perfect partial scan is discounted, because the number a caller reads off
#: ``dossier.score`` has to already carry the cost of what we could not check.
CONFIDENCE_FLOOR = Decimal("0.35")

#: The two scoring components that are SOLANA ACCOUNT AUTHORITIES, worth 26 of the
#: rubric's 100 points between them. No EVM chain can earn either -- see
#: :data:`_SOL_ONLY_CRITICAL` for the same fact on the coverage side.
_SOL_ONLY_COMPONENTS: frozenset[str] = frozenset({"mint_authority", "freeze_authority"})


def achievable_evidence_weight(chain: "Chain | None") -> float:
    """The most evidence weight a dossier on ``chain`` could possibly carry.

    MEASURED 2026-09-22: ``COMPONENT_MAX`` sums to exactly 100, and ``mint_authority``
    (14) plus ``freeze_authority`` (12) are unreachable on EVM, so an EVM ceiling is 74.
    Left unscaled that did two things, both wrong:

    * ``confidence`` discounted a PERFECT robinhood dossier to 0.35 + 0.65 * 0.74 = 0.831,
      charging it for questions its chain does not ask;
    * ``A_MIN_EVIDENCE_WEIGHT`` is 80, above the EVM ceiling of 74, so **no EVM token
      could ever be graded A** regardless of how clean it was.

    ``None`` keeps the full 100: an unknown chain is not a licence to assume a component
    is unreachable.
    """
    if chain is not None and chain in EVM_CHAINS:
        return sum(v for k, v in COMPONENT_MAX.items() if k not in _SOL_ONLY_COMPONENTS)
    return sum(COMPONENT_MAX.values())


def confidence_for(evidence_weight: float, chain: "Chain | None") -> float:
    """How much of what THIS CHAIN can answer was established, as a discount factor."""
    ceiling = achievable_evidence_weight(chain)
    if ceiling <= 0:
        return float(CONFIDENCE_FLOOR)
    fraction = min(Decimal(1), Decimal(str(evidence_weight)) / Decimal(str(ceiling)))
    return float(CONFIDENCE_FLOOR + (1 - CONFIDENCE_FLOOR) * fraction)


def min_evidence_for(threshold: float, chain: "Chain | None") -> float:
    """A threshold written against the Solana rubric, rescaled to ``chain``'s ceiling.

    The BAR IS THE SAME FRACTION on every chain. This is not a lower bar for EVM: it is
    the same one, measured against a ruler that fits the chain.
    """
    return threshold * achievable_evidence_weight(chain) / 100.0

#: Warnings whose property is already a smooth scoring component are not charged twice;
#: these are the ones nothing else prices in.
NON_COMPONENT_PENALTY: dict[TokenRisk, float] = {
    TokenRisk.UNKNOWN_SAFETY: 15.0,
    TokenRisk.PROVIDER_CONFLICT: 12.0,
    TokenRisk.RUG_HISTORY: 15.0,
    TokenRisk.WASH_TRADING: 12.0,
    TokenRisk.DEV_SOLD: 8.0,
    TokenRisk.METADATA_MUTABLE: 6.0,
    TokenRisk.TRANSFER_FEE: 6.0,
}


def _ramp_down(value: Decimal, good: Decimal, bad: Decimal) -> Decimal:
    """1.0 at or below ``good``, 0.0 at or above ``bad``, linear between."""
    if value <= good:
        return Decimal(1)
    if value >= bad:
        return ZERO
    return (bad - value) / (bad - good)


def _ramp_up(value: Decimal, bad: Decimal, good: Decimal) -> Decimal:
    if value >= good:
        return Decimal(1)
    if value <= bad:
        return ZERO
    return (value - bad) / (good - bad)


def _components(resolution: Resolution) -> dict[str, Decimal]:
    """Fraction-of-max per component, only for components we could measure."""
    out: dict[str, Decimal] = {}

    can_sell = resolution.b("can_sell")
    if can_sell is not None:
        out["sell_safety"] = Decimal(1) if can_sell else ZERO
    mint = resolution.b("mint_authority_revoked")
    if mint is not None:
        out["mint_authority"] = Decimal(1) if mint else ZERO
    freeze = resolution.b("freeze_authority_revoked")
    if freeze is not None:
        out["freeze_authority"] = Decimal(1) if freeze else ZERO

    lp = resolution.n("lp_burned_pct")
    if lp is not None:
        out["lp_security"] = _ramp_up(lp, ZERO, HUNDRED)
    top10 = resolution.n("top10_pct")
    if top10 is not None:
        out["holder_distribution"] = _ramp_down(top10, Decimal(20), Decimal(70))

    supply_risks = [
        resolution.n(p) for p in ("dev_pct", "insider_pct", "cluster_pct", "bundler_pct", "sniper_pct")
    ]
    known = [v for v in supply_risks if v is not None]
    if known:
        out["insider_supply"] = _ramp_down(max(known), ZERO, Decimal(30))

    tax = _max_tax(resolution)
    if tax is not None:
        out["taxes"] = _ramp_down(tax, ZERO, Decimal(1_500))

    liquidity = resolution.n("liquidity_usd")
    if liquidity is not None:
        out["liquidity"] = _ramp_up(liquidity, Decimal(1_000), Decimal(250_000))
    return out


@dataclass(frozen=True)
class Verdict:
    score: float
    grade: Grade
    evidence_weight: float
    notes: list[str]
    #: How much of the *predictive* surface the scan established, kept out of the score on
    #: purpose. ``grade`` is already capped by ``coverage.max_grade``; this field is how a
    #: caller tells "clean and well-covered" from "clean but barely examined".
    coverage: cov.CoverageReport


def coverage_of(resolution: Resolution) -> cov.CoverageReport:
    """Coverage of the predictive surface for one merged view. Pure, no provider call."""
    return cov.assess(name for name in cov.FIELD_WEIGHTS if resolution.values.get(name) is not None)


def score_dossier(resolution: Resolution, findings: Sequence[Finding]) -> Verdict:
    """Turn the merged view into a score and a grade, with evidence weight priced in.

    The score is a statement about what we found. It is deliberately *not* adjusted by
    coverage — coverage caps the grade instead, and is reported alongside it.
    """
    fractions = _components(resolution)
    evidence_weight = sum(COMPONENT_MAX[name] for name in fractions)
    reach = coverage_of(resolution)
    notes: list[str] = []

    if evidence_weight <= 0:
        return Verdict(0.0, Grade.QUARANTINED if _has_blocker(findings) else Grade.UNSCORED, 0.0,
                       ["no scoring component could be measured"], reach)

    raw = sum((fractions[name] * Decimal(str(COMPONENT_MAX[name])) for name in fractions), ZERO)
    normalised = raw * HUNDRED / Decimal(str(evidence_weight))
    ceiling = achievable_evidence_weight(resolution.chain)
    confidence = Decimal(str(confidence_for(evidence_weight, resolution.chain)))
    score = normalised * confidence
    if confidence < 1:
        notes.append(
            f"score discounted x{confidence:.2f} for evidence_weight "
            f"{evidence_weight:.0f}/{ceiling:.0f}"
        )

    # One penalty per risk: two rules that both raise UNKNOWN_SAFETY describe one problem.
    charged: set[TokenRisk] = set()
    for finding in findings:
        if finding.severity is not Severity.WARNING or finding.risk in charged:
            continue
        penalty = NON_COMPONENT_PENALTY.get(finding.risk)
        if penalty:
            charged.add(finding.risk)
            score -= Decimal(str(penalty))
            notes.append(f"-{penalty:g} {finding.risk.value}")
    score = max(ZERO, min(HUNDRED, score))

    grade = _assign_grade(float(score), evidence_weight, findings, notes, reach,
                          chain=resolution.chain)
    return Verdict(float(round(score, 2)), grade, float(evidence_weight), notes, reach)


def _has_blocker(findings: Sequence[Finding]) -> bool:
    return any(f.severity is Severity.BLOCKER for f in findings)


def _assign_grade(
    score: float,
    evidence_weight: float,
    findings: Sequence[Finding],
    notes: list[str],
    reach: cov.CoverageReport,
    *,
    chain: "Chain | None" = None,
) -> Grade:
    """Score first, then the coverage ceiling. The ceiling only ever lowers the answer.

    Keeping them in this order is the whole point: ``score`` says what we found and the
    ceiling says how far we looked, and a reader of ``notes`` can see which one moved.
    """
    grade = _grade_from_score(score, evidence_weight, findings, notes, chain=chain)
    capped = cov.cap(grade, reach)
    if capped is not grade:
        notes.append(
            f"capped to {capped.value} from {grade.value}: {reach.tier.value} coverage of the "
            f"predictive surface ({reach.established_weight}/{reach.total_weight}); "
            f"could not establish {','.join(reach.unestablished_families)}"
        )
    return capped


def _grade_from_score(
    score: float, evidence_weight: float, findings: Sequence[Finding], notes: list[str],
    *, chain: "Chain | None" = None,
) -> Grade:
    # Both evidence thresholds are written against the Solana rubric's 100 points and are
    # rescaled to this chain's ceiling. The BAR IS THE SAME FRACTION; see
    # `min_evidence_for`. Unscaled, `A_MIN_EVIDENCE_WEIGHT` (80) sat above the EVM ceiling
    # (74) and no EVM token could be graded A at all.
    min_evidence = min_evidence_for(MIN_EVIDENCE_WEIGHT, chain)
    a_min_evidence = min_evidence_for(A_MIN_EVIDENCE_WEIGHT, chain)
    if _has_blocker(findings):
        return Grade.QUARANTINED
    if evidence_weight < min_evidence:
        notes.append(
            f"UNSCORED: evidence_weight {evidence_weight:.0f} < {min_evidence:.0f} — "
            "too little of the rubric was measurable to have an opinion"
        )
        return Grade.UNSCORED
    if score >= A_MIN_SCORE:
        warnings = [f for f in findings if f.severity is Severity.WARNING]
        if evidence_weight < a_min_evidence:
            notes.append(f"capped to B: evidence_weight {evidence_weight:.0f} < {a_min_evidence:.0f}")
            return Grade.B
        if warnings:
            notes.append(f"capped to B: {len(warnings)} warning(s) outstanding")
            return Grade.B
        return Grade.A
    if score >= B_MIN_SCORE:
        return Grade.B
    if score >= C_MIN_SCORE:
        return Grade.C
    return Grade.D


# --------------------------------------------------------------------------------------
# provider collection
# --------------------------------------------------------------------------------------


#: A provider is ``ok`` (it answered), ``down`` (it should have answered and did not) or
#: ``n/a`` (it does not cover this chain). Only ``down`` is a gap in our coverage; calling
#: RugCheck "down" on an Ethereum token would make the outage reporting useless.
ProviderStatus = Literal["ok", "down", "n/a"]
Collected = tuple[list[Claim], list[Receipt], ProviderStatus]


def _claims_from(prop_map: dict[str, Any], provider: str, receipt: Receipt) -> list[Claim]:
    known = set(BOOL_PROPERTIES) | set(NUM_PROPERTIES) | set(TEXT_PROPERTIES)
    return [
        Claim(prop=k, provider=provider, value=v, receipt=receipt)
        for k, v in prop_map.items()
        if k in known and v is not None
    ]


def collect_goplus(address: str, chain: Chain, conn: Any = None) -> Collected:
    from kaiba.core.schemas import EVM_CHAINS

    if chain is not Chain.SOL and chain not in EVM_CHAINS:
        return [], [], "n/a"
    try:
        from kaiba.providers import goplus

        props, receipt = goplus.security_properties(address, chain, conn=conn)
    except Exception as exc:  # noqa: BLE001 - a provider being down is data, not a crash
        log.warning("goplus adapter failed for %s: %s", address, exc)
        return [], [_dead_receipt("goplus", "token.security", exc)], "down"
    return _claims_from(props, "goplus", receipt), [receipt], ("ok" if props else "down")


def collect_rugcheck(address: str, chain: Chain, conn: Any = None) -> Collected:
    if chain is not Chain.SOL:
        return [], [], "n/a"
    try:
        from kaiba.providers import rugcheck

        props, receipts = rugcheck.security_properties(address, chain, conn=conn)
    except Exception as exc:  # noqa: BLE001
        log.warning("rugcheck adapter failed for %s: %s", address, exc)
        return [], [_dead_receipt("rugcheck", "token.report", exc)], "down"
    live = next((r for r in receipts if r.basis is not EvidenceBasis.UNAVAILABLE), None)
    return (
        _claims_from(props, "rugcheck", live or receipts[0]),
        receipts,
        ("ok" if props else "down"),
    )


#: GMGN's ``token security`` adapter is being written in parallel with this module, so it
#: is imported by name at call time and every plausible entry point is tried. If it is not
#: there yet, or it changes shape, the scan loses one source and says so in the receipts —
#: it does not fail.
_GMGN_ENTRY_POINTS: tuple[str, ...] = (
    "security_properties",
    "token_security_properties",
    "token_security",
    "security",
)


def collect_dedup(address: str, chain: Chain, conn: Any = None) -> Collected:
    """Ask ``kaiba.intelligence.dedup`` whether this token is a copy of an earlier one.

    Originals graduate at 9.20% and copycats at 0.86% on the CCS'26 corpus — a 10.7×
    separation for one hash comparison, which makes it the cheapest strong signal
    available. It belongs here rather than in tier-0 triage because establishing it
    costs a metadata fetch (~1.9 s measured), which is a tier-1 price.

    **Only a content-hash match is reported as a copycat.** Our own measurement on 1,038
    consecutive live launches put the four-field copycat rate at 46.4%, against the
    paper's 10.2%. Either the base rate has moved by 4.5× or the fields are noisier now;
    in both cases the published separation cannot be assumed to transfer to a text match.
    The image or metadata CID subset was 17.8% and is the part that identifies a genuine
    reuse — one creator, one image CID, twenty mints, twelve tickers was the largest real
    cluster found, and no text fingerprint catches it.
    """
    try:
        from kaiba.intelligence import dedup
    except ImportError:
        return [], [], "n/a"
    try:
        verdict = dedup.classify(address, chain, conn)
    except Exception as exc:  # noqa: BLE001 - a dead sibling is data, not a crash
        log.debug("dedup unavailable for %s: %s", address[:16], exc)
        return [], [], "down"

    receipts = list(getattr(verdict, "receipts", []) or [])
    proof = getattr(verdict, "proof_match", None)
    status = getattr(getattr(verdict, "status", None), "value", None)
    if status == "unknown":
        # Never seen the fingerprint and no coverage claim: that is not "original".
        return [], receipts, "ok"
    value = bool(proof)
    receipt = receipts[0] if receipts else Receipt(
        provider="dedup", endpoint="dedup.classify", basis=EvidenceBasis.DERIVED
    )
    return [Claim(prop="copycat", provider="dedup", value=value, receipt=receipt)], receipts, "ok"


def collect_bundles(address: str, chain: Chain, conn: Any = None) -> Collected:
    """Ask ``kaiba.intelligence.bundles`` for ``bundler_pct`` and ``sniper_pct``.

    These two were unknown on **615 of 615** stored dossiers because GMGN is the only
    provider that sells them and we are on the free plan. But a bundle is a structural
    fact about the chain, not a vendor's opinion: ``bundles.analyse`` recovers it from
    slot / block_index / fee_payer adjacency in our own ``swaps`` table, which costs a
    local query and no provider call — it has to, because tier 1 has a 7.6 s budget and
    that budget is what decides how much of the launch firehose we see.

    Bundle-adjusted holder concentration is the strongest holder-structure variable we
    have (EDGE §1 #5: a 24pp discriminative gap against 6pp for naive top-10), and it is
    also the heaviest single field in :mod:`kaiba.intelligence.coverage`, so this is the
    one wiring that can move a dossier off the coverage floor.

    It refuses far more often than it answers, and that is correct rather than a fault: a
    mint whose tape we have not pulled back to the launch slot returns **no claims**, which
    leaves both properties in ``dossier.unknowns``. A confident 0% on an unexamined mint
    would be the single worst thing this collector could do — it is fail-open on exactly
    the field ``curve-velocity`` gates on, and ``bundles`` already had to fix one version
    of that bug.
    """
    try:
        from kaiba.intelligence.bundles import collect_bundles as _collect
    except Exception as exc:  # noqa: BLE001 - a dead sibling is data, not a crash
        log.info("bundles not available (%s); scanning without it", exc)
        return [], [_dead_receipt("bundles", "bundles.analyse", exc)], "down"
    try:
        return _collect(address, chain, conn)
    except Exception as exc:  # noqa: BLE001
        log.warning("bundles collector failed for %s: %s", address[:16], exc)
        return [], [_dead_receipt("bundles", "bundles.analyse", exc)], "down"


#: Freshness budget for ``dossier.rug_ratio`` -- the same 900 s every other percentage
#: measure on the dossier already carries. Named because two readers depend on it: the
#: ``Measure.stale`` check ``lanes._measure`` applies, and the stored-row fallback in
#: :func:`collect_gmgn_feed`, which refuses any row older than this rather than serving a
#: number the lane would reject as stale anyway.
RUG_RATIO_BUDGET_S = 900

#: Where each number behind the rug-ratio wiring comes from, in the vocabulary the rest
#: of this tree uses (MEASURED / CITED / DERIVED / INVENTED). A test walks this table, so a
#: constant that is not accounted for here cannot ship quietly.
RUG_RATIO_PROVENANCE: dict[str, str] = {
    "source_endpoint": (
        "MEASURED 2026-09-21: `token security` and `token info` carry no rug field on sol "
        "or bsc (35/176 and 27/163 keys read live, plus both recorded fixture pairs); the "
        "trenches feed row carries `rug_ratio` on 180/180 sol rows, 8/180 bsc, 8/180 "
        "robinhood, 54/180 base -- every EVM value seen was 0, the rest null."
    ),
    "unit": (
        "MEASURED 2026-09-21 on 180 sol trenches rows: 0-1 fraction, min 0, median 0.008, "
        "max 1.0, 90 zeros, 23 at or above 0.3; CITED from gmgn-cli's own flag help, "
        "'rug pull risk score (0-1)'."
    ),
    "semantics": (
        "MEASURED: it is not a creator-history ratio we can reproduce (matches "
        "1 - creator_created_open_ratio on 30/180 rows only) and it moved on 8/103 tokens "
        "between two reads minutes apart, five of them from 0 to non-zero. Treated as an "
        "opaque provider score, basis PROVIDER_REPORTED, never DERIVED."
    ),
    "RUG_RATIO_BUDGET_S": (
        "DERIVED: the 900 s default `Measure` budget this dossier already applied to "
        "rug_ratio and every other percentage measure; not a new number."
    ),
    "feed_cache_window": (
        "MEASURED that the default 15 s TTL / 60 s grace on market.trenches served 4 of 6 "
        "feed hits as STALE on the first 20-token re-scan (2026-09-21), each earning the "
        "stale_evidence warning for being under a minute old. INVENTED as a number: "
        "gmgn_cli.TRENCHES_FEED_TTL_S is 30 s with no grace; the score moved within "
        "minutes and, in one 90 s re-read (2026-09-22), within 90 s; nothing read the "
        "same token twice inside 30 s, so 30 s is a guess, not a bound, and it would be "
        "settled by polling the feed every 10 s for an hour and timing the first change "
        "per token."
    ),
    "evm_zero": (
        "INVENTED inference from a MEASURED distribution: every EVM rug_ratio ever observed "
        "is exactly 0 and category-linked (2026-09-22, two independent live-box re-reads: "
        "bsc 12/180 present all 0, robinhood 36/180 all 0, base 0/180, trending 50/50 int 0, "
        "signal 50/50 int 0), so both readers serve a reported 0 on an EVM chain as "
        "UNAVAILABLE with EVM_ZERO_RUG_NOTE, never as a measured 0. On sol a 0 is served "
        "with SOL_ZERO_RUG_NOTE because the rug==0 group is the young one (median age 211 s, "
        "54/95 first-time creators, against 771 s for rug>0) and 8 of 9 changes in a 90 s "
        "re-read were 0 -> non-zero. It would be settled by one EVM row carrying a non-zero "
        "value, or by GMGN publishing the score's definition."
    ),
    "lane_threshold_0.3": (
        "CITED: gmgn-cli's smart-money trenches preset applies max_rug_ratio 0.3 "
        "(dist/commands/market.js); lanes.SM_TRENCHES copies it. INVENTED as a trading "
        "threshold -- nothing has validated 0.3 against an outcome; it would be settled by "
        "the graduation rate of sol trenches tokens above and below it once the feed rows "
        "are stored per token."
    ),
}


#: Module-level so a test can hold it against ``EXPLAIN QUERY PLAN`` and prove it walks
#: ``idx_events_subj`` rather than the table.
STORED_FEED_SQL = (
    "SELECT ts_ms, payload FROM events WHERE subject=? AND kind=? AND chain=? "
    "AND ts_ms>=? ORDER BY ts_ms DESC LIMIT 25"
)


#: What a reported ``0`` means, in the words every rug-ratio receipt carries. These are
#: copies of ``gmgn_cli.EVM_ZERO_RUG_NOTE`` / ``SOL_ZERO_RUG_NOTE`` -- the stored-row
#: reader must work with that module absent -- and a test holds the two copies equal.
#: The numbers behind them are in :data:`RUG_RATIO_PROVENANCE` under ``evm_zero``.
EVM_ZERO_RUG_NOTE = (
    "reported 0 = unscored on this chain (INVENTED inference: 100% of EVM values observed are 0)"
)
SOL_ZERO_RUG_NOTE = "zero may mean unscored (young tokens flip 0 -> non-zero within minutes)"

#: The smart-money preset the feed ingester polls with. Its server-side filter is
#: ``max_rug_ratio 0.3`` (CITED: gmgn-cli dist/commands/market.js), so a stored row that
#: came through it is under 0.3 by construction and its receipt must say so.
SMART_MONEY_PRESET = "smart-money"


def _stored_row_preset_note(body: dict[str, Any]) -> str:
    """The ``filter_preset`` clause for a stored-row receipt note, or ``""``.

    The feed ingester is adding ``filter_preset`` to the rows it stores; a row without it
    (every row written before that, and any row from an unfiltered read) gets no clause
    rather than a guess. Only the smart-money preset earns the "< 0.3 by construction"
    claim, because that is the one whose server-side filter is known.
    """
    preset = body.get("filter_preset")
    if not isinstance(preset, str) or not preset.strip():
        return ""
    preset = preset.strip()
    if preset == SMART_MONEY_PRESET:
        return (
            f"; filter_preset={preset} (max_rug_ratio 0.3 applied server-side: "
            "< 0.3 by construction)"
        )
    return f"; filter_preset={preset} (server-side filtered; not a sample of the unfiltered feed)"


def _stored_feed_rug_ratio_read(
    conn: Any, address: str, chain: Chain
) -> tuple[Decimal | None, Receipt] | None:
    """The newest stored GMGN feed row for this token inside the budget, read honestly.

    Returns ``(value, receipt)`` with basis CACHED when the row carries a score; ``(None,
    receipt)`` with basis UNAVAILABLE when it carries a reported ``0`` on an EVM chain,
    which is read as "unscored" (:data:`EVM_ZERO_RUG_NOTE`: 100% of EVM values ever
    observed are 0); ``None`` when nothing usable is stored. On sol a 0 is served, with
    :data:`SOL_ZERO_RUG_NOTE` on the receipt. The newest in-range number decides: a null
    or out-of-range row is not an answer and is skipped, an unscored row is an answer
    ("no score as of then") and is not.

    ``kaiba.ingest.gmgn_feeds.write_alpha`` writes each trenches row as an
    ``alpha.signal`` event with the token as subject and ``rug_ratio`` in the payload.
    The event's ``ts_ms`` is the moment *we* saw the row, so it is the observation time
    the receipt carries; the payload's ``observed_ms`` is the token's own creation time
    and would make a stale row look fresh. A row that carries ``filter_preset`` has that
    on the receipt too (:func:`_stored_row_preset_note`), so a reader knows the stored
    fallback is preset-filtered rather than a sample of the whole feed.

    Rows older than :data:`RUG_RATIO_BUDGET_S` are not returned at all. A feed row is
    written once per token (the writer dedupes on the address) and the score is known to
    move within minutes, so serving an old first sighting would put a possibly-unscored 0
    in front of a gate that reads 0 as a pass.

    The subject is matched exactly, on purpose. ``events`` held 229,403 rows on the live
    box (2026-09-21) and its only useful index here is ``idx_events_subj(subject, id)``;
    a ``lower(subject)`` comparison would discard it and scan the table on every EVM scan.
    Exact is also correct: the feed writes EVM addresses lowercase on every bsc, robinhood
    and base row sampled, and ``scan_token`` lowercases the address before it gets here.
    """
    if conn is None:
        return None
    import json

    from kaiba.core.schemas import EventKind

    cutoff = now_ms() - RUG_RATIO_BUDGET_S * 1000
    evm = chain in EVM_CHAINS
    key = address.lower() if evm else address
    try:
        rows = conn.execute(
            STORED_FEED_SQL, (key, EventKind.ALPHA_SIGNAL.value, chain.value, cutoff)
        ).fetchall()
    except Exception as exc:  # noqa: BLE001 - a missing table is "no evidence", not a crash
        log.debug("stored feed lookup failed for %s: %s", address[:16], exc)
        return None
    for row in rows:
        # Positional on purpose: works on a plain connection and on one with a row factory.
        ts_raw, payload = row[0], row[1]
        try:
            body = json.loads(payload or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(body, dict) or body.get("provider") != "gmgn":
            continue
        value = _dec_or_none(body.get("rug_ratio"))
        if value is None or not (0 <= value <= 1):
            continue
        ts = int(ts_raw)
        # Clamped so the verdict and the caveat, not the feed's name, are what the 300-char
        # cap keeps; the real names are 6-10 characters (trenches, trending, signal).
        feed = str(body.get("feed") or "gmgn")[:24]
        endpoint = f"feed.{body.get('feed') or 'unknown'}"
        age = f"seen {(now_ms() - ts) / 1000:.0f}s before this scan"
        preset = _stored_row_preset_note(body)
        if evm and value == 0:
            return None, Receipt(
                provider="gmgn",
                endpoint=endpoint,
                observed_at_ms=ts,
                basis=EvidenceBasis.UNAVAILABLE,
                # The verdict first; the row context after it is what a 300-char cap cuts.
                note=f"{EVM_ZERO_RUG_NOTE}; stored {feed} feed row {age}{preset}"[:300],
            )
        unscored = f"; {SOL_ZERO_RUG_NOTE}" if value == 0 else ""
        receipt = Receipt(
            provider="gmgn",
            endpoint=endpoint,
            observed_at_ms=ts,
            basis=EvidenceBasis.CACHED,
            note=(
                f"rug_ratio={value} from the stored {feed} feed row, {age}{unscored}; "
                f"opaque GMGN 0-1 score{preset}"
            )[:300],
        )
        return value, receipt
    return None


def _stored_feed_rug_ratio(
    conn: Any, address: str, chain: Chain
) -> tuple[Decimal, Receipt] | None:
    """:func:`_stored_feed_rug_ratio_read` narrowed to rows that carry a score.

    :func:`collect_gmgn_feed` turns whatever this returns into a claim unconditionally,
    so the UNAVAILABLE verdict (a reported 0 on an EVM chain) is dropped to ``None`` here
    and logged, rather than handed up as a ``None``-valued claim or -- worse -- as the 0.
    The collector should move to the full form and carry that receipt into the dossier;
    until it does, an EVM 0 in the store is simply "nothing usable stored".
    """
    got = _stored_feed_rug_ratio_read(conn, address, chain)
    if got is None:
        return None
    value, receipt = got
    if value is None:
        log.info("stored feed row for %s on %s: %s", address[:16], chain.value, receipt.note)
        return None
    return value, receipt


def collect_gmgn_feed(address: str, chain: Chain, conn: Any = None) -> Collected:
    """``rug_ratio`` from GMGN's trenches feed -- the only place GMGN sends it.

    ``token security`` and ``token info`` have never carried a rug field (MEASURED live on
    sol and bsc, 2026-09-21), so :func:`collect_gmgn` could not produce it and
    ``rug_ratio`` was UNAVAILABLE on 4,298 of 4,298 stored dossiers. The number the
    ``sm-trenches`` lane gates on is the one on the feed row -- the lane's 0.3 is the
    feed's own smart-money preset -- so this collector reads that row, in this order:

    1. Live, through ``gmgn_cli.feed_rug_ratio`` (one cached weight-1 read per chain).
    2. Failing that, the newest ``alpha.signal`` row the feed ingester stored for the
       token, if it is inside :data:`RUG_RATIO_BUDGET_S`, with the true observation time
       on the receipt and basis CACHED.

    Never a number when neither answers, and a row whose ``rug_ratio`` is ``null`` is not
    an answer. On the EVM chains that is the usual outcome (bsc: 172 of 180 rows null),
    which means the lane's fail-closed gate still refuses every bsc token; that is a fact
    about the provider, reported here as UNAVAILABLE rather than papered over with a 0.

    Status: ``ok`` when GMGN answered (with or without a number for this token),
    ``down`` only when the read itself failed and nothing was stored.
    """
    receipts: list[Receipt] = []
    live_failed = False
    try:
        import importlib

        module = importlib.import_module("kaiba.providers.gmgn_cli")
        fn = getattr(module, "feed_rug_ratio", None)
    except Exception as exc:  # noqa: BLE001 - ImportError today, anything tomorrow
        log.info("gmgn_cli not available for the feed read (%s)", exc)
        fn = None
        live_failed = True

    if callable(fn):
        try:
            raw = fn(address, chain, conn=conn)
        except Exception as exc:  # noqa: BLE001 - a dead provider is data, not a crash
            log.warning("gmgn_cli.feed_rug_ratio failed for %s: %s", address[:16], exc)
            receipts.append(_dead_receipt("gmgn", "market.trenches", exc))
            live_failed = True
        else:
            data, receipt = _split_result(raw)
            if isinstance(receipt, Receipt):
                receipts.append(receipt)
            value = _dec_or_none(data.get("rug_ratio")) if isinstance(data, dict) else None
            if (
                value is not None
                and isinstance(receipt, Receipt)
                and receipt.basis is not EvidenceBasis.UNAVAILABLE
            ):
                return [Claim("rug_ratio", "gmgn", value, receipt)], receipts, "ok"
            # An UNAVAILABLE receipt on the *row* endpoint means the feed answered and
            # had nothing for this token; anything else is the read failing.
            row_endpoint = getattr(module, "TRENCHES_ROW_ENDPOINT", "market.trenches.row")
            live_failed = not (
                isinstance(receipt, Receipt) and receipt.endpoint == row_endpoint
            )

    stored = _stored_feed_rug_ratio(conn, address, chain)
    if stored is not None:
        value, receipt = stored
        receipts.append(receipt)
        return [Claim("rug_ratio", "gmgn", value, receipt)], receipts, "ok"
    return [], receipts, ("down" if live_failed else "ok")


def _split_result(raw: Any) -> tuple[Any, Receipt | None]:
    """``(data, receipt)`` from a ``GmgnResult``, a 2-tuple, or anything else."""
    if hasattr(raw, "data") and hasattr(raw, "receipt"):
        return raw.data, raw.receipt if isinstance(raw.receipt, Receipt) else None
    if isinstance(raw, tuple) and len(raw) == 2:
        return raw[0], raw[1] if isinstance(raw[1], Receipt) else None
    return None, None


def collect_gmgn(address: str, chain: Chain, conn: Any = None) -> Collected:
    try:
        import importlib

        module = importlib.import_module("kaiba.providers.gmgn_cli")
    except Exception as exc:  # noqa: BLE001 - ImportError today, anything tomorrow
        log.info("gmgn_cli not available (%s); scanning without it", exc)
        return [], [_dead_receipt("gmgn", "token.security", "module not available")], "down"

    for name in _GMGN_ENTRY_POINTS:
        fn = getattr(module, name, None)
        if not callable(fn):
            continue
        try:
            raw = _call_flexible(fn, address, chain, conn)
        except Exception as exc:  # noqa: BLE001
            log.warning("gmgn_cli.%s failed for %s: %s", name, address, exc)
            return [], [_dead_receipt("gmgn", f"token.{name}", exc)], "down"
        props, receipt = _unwrap_gmgn(raw, module, chain)
        if props:
            claims = _claims_from(props, "gmgn", receipt)
            own = _own_field_receipts(props)
            if not own:
                return claims, [receipt], "ok"
            claims = [replace(c, receipt=own[c.prop]) if c.prop in own else c for c in claims]
            extra = list(dict.fromkeys(r for r in own.values() if r is not receipt))
            return claims, [receipt, *extra], "ok"
        return [], [receipt], "down"
    return [], [_dead_receipt("gmgn", "token.security", "no known entry point on gmgn_cli")], "down"


def _own_field_receipts(props: Any) -> dict[str, Receipt]:
    """Per-property receipts a merged gmgn read attached (``gmgn_cli.SecurityProps``).

    MEASURED 2026-10-03 (journal #4981): the merged ``token.security+token.info`` receipt
    is stamped with its OLDER half, so a ``token info`` price seconds old reached the
    engine 304-601 s "old" and 20 approved live entries in 7 days were refused
    ``token_price_stale``. The adapter now names the read that actually observed each
    market field; this applies it. Anything that is not a usable ``Receipt`` is ignored,
    which leaves the merged (older) receipt in place -- the direction that can only refuse.
    """
    raw = getattr(props, "field_receipts", None)
    if not isinstance(raw, dict):
        return {}
    return {
        str(k): v
        for k, v in raw.items()
        if isinstance(v, Receipt) and v.basis is not EvidenceBasis.UNAVAILABLE
    }


# Same market freshness budget as price; the cache must still be inside token.info's
# 15-second fresh window when collected. Never read its stale grace or fetch on a miss.
VOLUME_BUDGET_S = 120
GMGN_VOLUME_CACHE_TTL_S = 15


def collect_cached_gmgn_volume(address: str, chain: Chain, conn: Any = None) -> Collected:
    """Recover price.volume_24h without repeating security_properties' token-info call.

    The exact key matches gmgn_cli.token_info -> _read -> run_read. Chain and address
    are request-bound, and the receipt keeps the cache's fetch time, not the scan time.
    Unknown volume produces no claim, never a zero.
    """
    from kaiba.core.schemas import digest
    from kaiba.providers._http import cache_read

    try:
        address = normalize_address(address, chain)
    except (TypeError, ValueError):
        return [], [], "n/a"
    key = f"gmgn-cli token info --address {address} --chain {chain.value} --raw"
    try:
        hit = cache_read("gmgn", key, ttl_s=GMGN_VOLUME_CACHE_TTL_S)
    except (OSError, TypeError, ValueError, OverflowError):
        return [], [], "n/a"
    if hit is None:
        return [], [], "n/a"
    body, _stale, fetched_ms = hit
    if not isinstance(body, dict) or not isinstance(body.get("price"), dict):
        return [], [], "n/a"
    if body.get("code") not in (None, 0, "0"):
        return [], [], "n/a"
    if _stale or not (0 < fetched_ms <= now_ms()):
        return [], [], "n/a"
    for part in (body, body["price"]):
        supplied_address = part.get("address")
        if not isinstance(supplied_address, str) or supplied_address != supplied_address.strip():
            return [], [], "n/a"
        try:
            if normalize_address(supplied_address, chain) != address:
                return [], [], "n/a"
        except ValueError:
            return [], [], "n/a"
        # token.info normally omits chain; the exact request key binds it. An explicit
        # contradictory chain is never ignored, including inside the price block.
        if "chain" in part and part["chain"] != chain.value:
            return [], [], "n/a"
    value = _volume_decimal(body["price"].get("volume_24h"))
    if value is None:
        return [], [], "n/a"
    receipt = Receipt(
        provider="gmgn", endpoint="token.info", observed_at_ms=fetched_ms,
        basis=EvidenceBasis.CACHED, request_digest=digest(key), response_digest=digest(body),
        note=f"{chain.value}:{address}; USD 24h volume from price.volume_24h; cache-only",
    )
    return [Claim("volume_24h_usd", "gmgn", value, receipt)], [receipt], "ok"


def _call_flexible(fn: Callable[..., Any], address: str, chain: Chain, conn: Any) -> Any:
    """Call an adapter whose exact signature we do not control, without calling it twice.

    The signature is inspected rather than probed: retrying on ``TypeError`` would re-issue
    a request every time the adapter itself raised one, and a safety scan that quietly
    triples its provider spend is its own kind of bug.
    """
    import inspect

    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return fn(address, chain)
    kwargs: dict[str, Any] = {}
    positional = [p for p in params.values() if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    if "conn" in params:
        kwargs["conn"] = conn
    if "chain" in params and len(positional) < 2:
        kwargs["chain"] = chain
        return fn(address, **kwargs)
    if len(positional) >= 2:
        return fn(address, chain, **kwargs)
    return fn(address, **kwargs)


def _unwrap_gmgn(raw: Any, module: Any, chain: Chain = Chain.SOL) -> tuple[dict[str, Any], Receipt]:
    """Accept ``(props, receipt)``, a ``Fetched``, or a bare payload.

    ``chain`` is only needed on the two untranslated paths below, and only because GMGN's
    ``renounced_mint`` / ``renounced_freeze_account`` are Solana authorities that it also
    emits, pinned to ``false``, on every EVM token -- see
    ``gmgn_cli._SOL_ONLY_BOOL_FIELDS`` and :data:`_GMGN_BOOL_MAP`. Forgetting to pass it
    reinstates that bug, so it is threaded rather than defaulted at the call site; the
    default here exists for the older two-argument callers and keeps their Solana
    behaviour, the direction that can only over-refuse.
    """
    receipt = Receipt(provider="gmgn", endpoint="token.security", basis=EvidenceBasis.PROVIDER_REPORTED)
    payload: Any = raw

    if isinstance(raw, tuple) and len(raw) == 2:
        payload, maybe = raw
        if isinstance(maybe, Receipt):
            receipt = maybe
        elif isinstance(maybe, list) and maybe and isinstance(maybe[0], Receipt):
            receipt = maybe[0]
    elif hasattr(raw, "data") and hasattr(raw, "receipt"):
        payload = raw.data
        if isinstance(raw.receipt, Receipt):
            receipt = raw.receipt
        if not getattr(raw, "ok", True):
            return {}, raw.receipt if isinstance(raw.receipt, Receipt) else receipt

    if not isinstance(payload, dict) or not payload:
        return {}, Receipt(
            provider="gmgn",
            endpoint=receipt.endpoint,
            basis=EvidenceBasis.UNAVAILABLE,
            note="gmgn_cli returned nothing usable",
        )
    # MEASURED 2026-09-22: this probe fired on EVERY gmgn payload and the adapter was
    # therefore never called. `token security` carries `can_sell`, `token info` carries
    # `symbol`, `name` and `holder_count` -- all four are OUR property names as well as
    # GMGN's field names, so "shares a key with our vocabulary" matched a raw body every
    # time. Everything needing translation was silently dropped: `liquidity` never became
    # `liquidity_usd`, `top_10_holder_rate` never became `top10_pct`, `buy_tax` never
    # became `buy_tax_bps`, and `can_sell: 0` arrived as an int, which
    # `resolve` discards -- exactly the evaporation `gmgn_cli.normalize_security`'s
    # docstring was written to fix.
    #
    # The probe now looks for a property whose NAME no provider sends verbatim. A body
    # that already speaks our vocabulary carries at least one of these; a raw provider
    # body carries none.
    if any(k in _CONVERTED_MARKERS for k in payload):
        return payload, receipt  # already speaks our vocabulary
    normalizer = getattr(module, "normalize_security", None)
    if callable(normalizer):
        try:
            # ``chain=`` by keyword, and a TypeError from an adapter that predates it is a
            # hard stop rather than a silent retry without it: calling again unqualified
            # would put the Solana authority mapping back on an EVM body, which is the
            # exact false positive this argument exists to remove. The bare ``normalize_gmgn``
            # below is chain-aware, so the fallback is still correct.
            converted = normalizer(payload, chain=chain)
            if isinstance(converted, dict) and converted:
                return converted, receipt
        except Exception as exc:  # noqa: BLE001
            log.debug("gmgn_cli.normalize_security failed: %s", exc)
    return normalize_gmgn(payload, chain=chain), receipt


def _dec_or_none(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (ValueError, ArithmeticError):
        return None


def _bool_or_none(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    s = str(value).strip().lower()
    if s in {"1", "true", "yes"}:
        return True
    if s in {"0", "false", "no"}:
        return False
    return None


#: GMGN field name -> (our property, transform). Written against the documented
#: ``token security`` payload; unrecognised spellings are ignored rather than guessed at.
_GMGN_BOOL_MAP: tuple[tuple[str, str, bool], ...] = (
    ("renounced_mint", "mint_authority_revoked", False),
    ("renounced_freeze_account", "freeze_authority_revoked", False),
    ("is_honeypot", "can_sell", True),
    ("can_not_sell", "can_sell", True),
    ("transfer_pausable", "freeze_authority_revoked", True),
    ("slippage_modifiable", "tax_modifiable", False),
    ("is_open_source", "source_verified", False),
    ("is_mutable_metadata", "metadata_mutable", False),
)

#: The two entries above that describe *Solana account authorities*. GMGN sends them on
#: EVM too, hardwired to ``false``: measured 2026-09-21 over 38 live ``token security``
#: reads (31 robinhood, PEPE/WBTC/SHIB on eth, CAKE/SafeMoon on bsc, DEGEN/BRETT on base),
#: ``renounced_mint`` and ``renounced_freeze_account`` were ``false`` on 38 of 38, while
#: the 4 Solana mints read alongside them each answered for themselves (WSOL, BONK and
#: 3NZ9..qmJh ``true``; 5XZw..uVqQ ``false``). Piping the EVM constant through produced
#: ``mint_authority_revoked=False`` and ``freeze_authority_revoked=False`` -- two BLOCKERs
#: -- on every EVM token GMGN could describe, including PEPE and SHIB, neither of which
#: has a mint function at all.
#:
#: They are **skipped**, not replaced. GMGN's ``is_renounced`` is ownership renouncement,
#: not mint authority, and it reported ``true`` for WBTC, whose ``owner()`` answered
#: 0xca06...beb7 on chain (see ``goplus._normalize_evm``). Substituting it would trade a
#: false blocker for a false all-clear. Unknown is the honest answer, and
#: ``partial_security_coverage`` is how the dossier says so.
_GMGN_SOL_ONLY_BOOLS: frozenset[str] = frozenset(
    {"renounced_mint", "renounced_freeze_account"}
)


def normalize_gmgn(payload: dict[str, Any], *, chain: Chain = Chain.SOL) -> dict[str, Any]:
    """Best-effort translation of a raw GMGN ``token security`` body.

    This is a fallback for the case where ``gmgn_cli`` hands back an untranslated payload.
    The field names are taken from GMGN's documented response; anything unrecognised stays
    unknown, which is the only safe direction for a guess to fail in.

    ``chain`` selects which fields are answers: on an EVM chain
    :data:`_GMGN_SOL_ONLY_BOOLS` are dropped. It defaults to ``Chain.SOL`` for callers that
    predate the argument -- the mapping they already had, and the one whose failure mode is
    an unwarranted refusal rather than an unwarranted green light.
    """
    out: dict[str, Any] = {}
    if not isinstance(payload, dict):
        return out
    body = payload.get("data") if isinstance(payload.get("data"), dict) else payload

    evm = chain in EVM_CHAINS
    for src, prop, invert in _GMGN_BOOL_MAP:
        if src not in body or (evm and src in _GMGN_SOL_ONLY_BOOLS):
            continue
        value = _bool_or_none(body.get(src))
        if value is None:
            continue
        # Two GMGN spellings can land on one property (``is_honeypot``/``can_not_sell`` ->
        # ``can_sell``; ``renounced_freeze_account``/``transfer_pausable`` ->
        # ``freeze_authority_revoked``). Take the unsafe one, the same way :func:`resolve`
        # settles two providers, rather than letting tuple order decide -- with a plain
        # assignment a body saying ``is_honeypot=1, can_not_sell=0`` came out
        # ``can_sell=True`` and the ``honeypot`` blocker never fired.
        claim = (not value) if invert else value
        unsafe = BOOL_PROPERTIES[prop].unsafe_value
        out[prop] = unsafe if unsafe in (out.get(prop), claim) else claim

    for src, prop, scale in (
        ("burn_ratio", "lp_burned_pct", HUNDRED),
        ("top_10_holder_rate", "top10_pct", HUNDRED),
        ("bundler_trader_amount_rate", "bundler_pct", HUNDRED),
        ("sniper_amount_rate", "sniper_pct", HUNDRED),
        ("insider_amount_rate", "insider_pct", HUNDRED),
        ("dev_token_amount_rate", "dev_pct", HUNDRED),
        ("buy_tax", "buy_tax_bps", Decimal(10_000)),
        ("sell_tax", "sell_tax_bps", Decimal(10_000)),
        # Kept for a feed row handed in raw; ``token security`` itself never carries it
        # (see ``collect_gmgn_feed``). A 0-1 fraction, dropped outside that range.
        ("rug_ratio", "rug_ratio", Decimal(1)),
        ("holder_count", "holder_count", Decimal(1)),
        ("liquidity", "liquidity_usd", Decimal(1)),
    ):
        value = _dec_or_none(body.get(src))
        if value is None or prop not in (set(NUM_PROPERTIES) | set(BOOL_PROPERTIES)):
            continue
        if prop == "rug_ratio" and not (0 <= value <= 1):
            continue
        out[prop] = value * scale

    status = str(body.get("creator_token_status") or "").lower()
    if status:
        out["dev_sold"] = status in {"creator_sell", "creator_close", "sell"}
    creator = body.get("creator_address") or body.get("creator")
    if creator:
        out["creator"] = creator
    return out


def _dead_receipt(provider: str, endpoint: str, note: Any) -> Receipt:
    return Receipt(
        provider=provider,
        endpoint=endpoint,
        basis=EvidenceBasis.UNAVAILABLE,
        note=str(note)[:300],
    )


# --------------------------------------------------------------------------------------
# the entry point the CLI and the MCP server call
# --------------------------------------------------------------------------------------


def build_dossier(address: str, chain: Chain, resolution: Resolution) -> TokenDossier:
    """Assemble the dossier from an already-resolved view. Pure; used directly by tests."""
    _derive(resolution, chain)
    findings = evaluate(resolution)
    verdict = score_dossier(resolution, findings)

    blockers: list[TokenRisk] = []
    warnings: list[TokenRisk] = []
    for finding in findings:
        target = blockers if finding.severity is Severity.BLOCKER else warnings
        if finding.risk not in target:
            target.append(finding.risk)

    receipts = list(resolution.receipts)
    receipts.extend(
        Receipt(
            provider="kaiba.dyor",
            endpoint=f"rule.{finding.severity.value}",
            basis=EvidenceBasis.DERIVED,
            note=finding.render()[:300],
        )
        for finding in findings
    )
    if verdict.notes:
        receipts.append(
            Receipt(
                provider="kaiba.dyor",
                endpoint="score.notes",
                basis=EvidenceBasis.DERIVED,
                note=("; ".join(verdict.notes))[:300],
            )
        )
    # Its own receipt rather than a line inside score.notes: coverage is the second fact
    # the caller needs and it must not be the part that gets truncated away at 300 chars.
    # ``coverage.assess_unknowns(dossier.unknowns)`` reproduces it from a stored row, so
    # this survives a round trip through ``token_dossiers`` without a new column.
    receipts.append(
        Receipt(
            provider="kaiba.dyor",
            endpoint="coverage.assessment",
            basis=EvidenceBasis.DERIVED,
            note=verdict.coverage.render()[:300],
        )
    )

    # built_at_ms is the assembly time and nothing else. It is never copied from a
    # provider response, cached or otherwise, because kaiba.execution.engine treats it as
    # the dossier's age and a back-dated or carried-over value would either discard a good
    # scan or let an old one authorise an entry. Evidence that is itself stale is reported
    # separately by the ``stale_evidence`` rule, since this field cannot express it.
    return TokenDossier(
        address=address,
        chain=chain,
        built_at_ms=now_ms(),
        price_usd=resolution.measure("price_usd", budget_s=120),
        liquidity_usd=resolution.measure("liquidity_usd", budget_s=600),
        # Same freshness budget as price: a market cap is a price with a constant on it,
        # and a stale one is stale for the same reason and at the same rate.
        market_cap_usd=resolution.measure("market_cap_usd", budget_s=120),
        volume_24h_usd=resolution.measure("volume_24h_usd", budget_s=VOLUME_BUDGET_S),
        holder_count=resolution.measure("holder_count", budget_s=900),
        top10_pct=resolution.measure("top10_pct"),
        dev_pct=resolution.measure("dev_pct"),
        insider_pct=resolution.measure("insider_pct"),
        bundler_pct=resolution.measure("bundler_pct"),
        sniper_pct=resolution.measure("sniper_pct"),
        cluster_pct=resolution.measure("cluster_pct"),
        buy_tax_bps=resolution.measure("buy_tax_bps"),
        sell_tax_bps=resolution.measure("sell_tax_bps"),
        rug_ratio=resolution.measure("rug_ratio", budget_s=RUG_RATIO_BUDGET_S),
        lp_burned_pct=resolution.measure("lp_burned_pct"),
        mint_authority_revoked=resolution.b("mint_authority_revoked"),
        freeze_authority_revoked=resolution.b("freeze_authority_revoked"),
        can_sell=resolution.b("can_sell"),
        blockers=blockers,
        warnings=warnings,
        unknowns=list(resolution.unknown),
        score=verdict.score,
        grade=verdict.grade,
        receipts=receipts,
    )


def store_dossier(dossier: TokenDossier, conn: Any = None) -> bool:
    """Upsert the dossier into ``token_dossiers``; returns whether the row landed.

    ``kaiba.execution.engine.load_dossier`` reads ``dossier_json`` out of this table and
    refuses every entry with ``no_dossier`` until a row exists, so a scan that is not
    persisted is a scan the trading system cannot see. Storage failure is logged rather
    than raised: losing the row is bad, losing the verdict the operator asked for is worse.
    """
    try:
        from kaiba.core.db import get_conn, jdump, tx, upsert

        c = conn or get_conn()
        row = {
            "chain": dossier.chain.value,
            "address": dossier.address,
            "built_at_ms": int(dossier.built_at_ms),
            "score": float(dossier.score) if dossier.score is not None else None,
            "grade": dossier.grade.value,
            "blockers_json": jdump([b.value for b in dossier.blockers]),
            "warnings_json": jdump([w.value for w in dossier.warnings]),
            "unknowns_json": jdump(list(dossier.unknowns)),
            "dossier_json": dossier.model_dump_json(),
        }
        with tx(c):
            upsert(c, "token_dossiers", row, ["chain", "address"])
        return True
    except Exception as exc:  # noqa: BLE001 - a dead database must not lose the verdict
        log.warning("could not persist dossier for %s:%s: %s", dossier.chain.value, dossier.address, exc)
        return False


def _rejected(address: str, chain: Chain, why: str) -> TokenDossier:
    """A dossier for something we refused to look up at all."""
    return TokenDossier(
        address=address,
        chain=chain,
        blockers=[TokenRisk.UNKNOWN_SAFETY],
        unknowns=list(TRACKED_PROPERTIES),
        score=0.0,
        grade=Grade.QUARANTINED,
        receipts=[
            Receipt(
                provider="kaiba.dyor",
                endpoint="validate.address",
                basis=EvidenceBasis.DERIVED,
                note=why[:300],
            )
        ],
    )


def scan_token(address: str, chain: Chain | str = Chain.SOL, *, conn: Any = None) -> TokenDossier:
    """Build a merged DYOR dossier for one token. Never raises on a provider being down.

    Called by ``kaiba token scan`` and by the MCP tool ``kaiba_scan_token``; both read
    ``.grade``, ``.score``, ``.blockers``, ``.warnings`` and ``.unknowns`` off the result.
    The dossier is persisted to ``token_dossiers`` before it is returned, because that
    table is how ``kaiba.execution.engine`` sees it.
    """
    ch = chain if isinstance(chain, Chain) else Chain(str(chain))
    try:
        addr = normalize_address(address, ch)
    except ValueError as exc:
        # Refuse before spending provider calls. A malformed address cannot come back
        # clean, so "every field unknown" would be a true but expensive way to say no.
        log.warning("dyor refused %r on %s: %s", address[:64], ch.value, exc)
        return _rejected(address.strip()[:64], ch, f"not a valid {ch.value} address: {exc}")

    connection = conn
    if connection is None:
        try:
            from kaiba.core.db import ensure_db

            connection = ensure_db()
        except Exception as exc:  # noqa: BLE001 - scanning must work before the db exists
            log.warning("dyor could not open the database (%s); running without it", exc)

    claims: list[Claim] = []
    receipts: list[Receipt] = []
    ok: set[str] = set()
    down: set[str] = set()
    for name, collector in (
        ("gmgn", collect_gmgn),
        ("gmgn_volume", collect_cached_gmgn_volume),
        # Directly after gmgn and never before it: the feed read is paced behind the two
        # ``security_properties`` reads with ``wait_for_slot_s``, whereas the unpaced
        # ``token security`` read would be refused by our own limiter if this ran first.
        ("gmgn_feed", collect_gmgn_feed),
        ("goplus", collect_goplus),
        ("rugcheck", collect_rugcheck),
        ("dedup", collect_dedup),
        # After gmgn on purpose. If GMGN ever answers, ``resolve`` then sees both claims
        # for bundler_pct and raises PROVIDER_CONFLICT on disagreement instead of one of
        # them silently winning — which is rule 2 of this module applied to our own
        # measurement rather than only to somebody else's.
        ("bundles", collect_bundles),
    ):
        got, provider_receipts, status = collector(addr, ch, connection)
        claims.extend(got)
        receipts.extend(provider_receipts)
        if status == "ok":
            ok.add(name)
        elif status == "down":
            down.add(name)

    # The chain is what makes `security_coverage` honest: without it every EVM dossier
    # is charged 15 points for not answering two Solana-only questions. See
    # `critical_properties_for`.
    resolution = resolve(claims, chain=ch)
    resolution.receipts.extend(receipts)
    resolution.providers_ok = ok
    resolution.providers_down = down

    dossier = build_dossier(addr, ch, resolution)
    # Stored even when every provider failed: an all-unknown QUARANTINED row is a record
    # that we looked, which is not the same thing as never having looked.
    store_dossier(dossier, connection)
    _announce(dossier, connection)
    return dossier


def _announce(dossier: TokenDossier, conn: Any) -> None:
    """Put the verdict on the bus. Telemetry failure must never fail a scan."""
    try:
        from kaiba.core.events import emit
        from kaiba.core.schemas import EventKind

        reach = cov.assess_unknowns(dossier.unknowns)
        emit(
            EventKind.TOKEN_SCANNED,
            {
                "grade": dossier.grade.value,
                "score": dossier.score,
                # Coverage rides alongside the grade rather than inside it, so a consumer
                # counting grades can also count how much of the rubric each one rests on.
                "coverage": reach.tier.value,
                "coverage_weight": reach.established_weight,
                "coverage_total": reach.total_weight,
                "coverage_missing_families": list(reach.unestablished_families),
                "blockers": [b.value for b in dossier.blockers],
                "warnings": [w.value for w in dossier.warnings],
                "unknowns": dossier.unknowns,
                "model": MODEL_ID,
            },
            chain=dossier.chain,
            subject=dossier.address,
            level="warn" if dossier.blockers else "info",
            conn=conn,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("could not emit token.scanned: %s", exc)


__all__ = [
    "BOOL_PROPERTIES",
    "CRITICAL_PROPERTIES",
    "COMPONENT_MAX",
    "MODEL_ID",
    "NUM_PROPERTIES",
    "RUG_RATIO_BUDGET_S",
    "RUG_RATIO_PROVENANCE",
    "RULES",
    "STORED_FEED_SQL",
    "TRACKED_PROPERTIES",
    "Claim",
    "Finding",
    "Resolution",
    "Rule",
    "Severity",
    "Verdict",
    "blocker_reasons",
    "build_dossier",
    "collect_bundles",
    "collect_gmgn",
    "collect_gmgn_feed",
    "collect_goplus",
    "collect_rugcheck",
    "conflict_notes",
    "coverage_of",
    "evaluate",
    "normalize_gmgn",
    "resolve",
    "scan_token",
    "score_dossier",
    "store_dossier",
]
