"""Source-bound airdrop scenarios and shared hunter evidence.

Allocation, probability, unlock terms, sale prices and costs must be explicit estimates
with source receipts. Unknown proceeds stay unknown. Recipient sell share and incidence
of price declines cannot determine dollars retained; the historical v1 helpers below are
kept only for compatibility and are never used by the current airdrop scorer.

A scenario is an auditable estimate, not proof of eligibility, an executable sale quote,
or a promised payout. Independent qualification and financial gates still apply.
"""

from __future__ import annotations

import logging
import os
import re
from decimal import Decimal, InvalidOperation, localcontext
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from kaiba.core.config import DEFAULT_RISK_PATH
from kaiba.core.schemas import Chain, digest, now_ms

log = logging.getLogger(__name__)

CENT = Decimal("0.01")
AIRDROP_EV_VERSION = "airdrop-ev/2"
SCENARIO_VERSION = "airdrop-scenarios/1"
MAX_SCENARIO_BYTES = 32_768
MAX_SCENARIO_AGE_MS = 86_400_000

# ---------------------------------------------------------------------------- base rates

# Historical v1 inputs only. Neither statistic measures expected price retention.
TGE_SELL_BASE_RATE = 0.64
#: Complement of "88% of airdropped tokens lose value within three months".
THREE_MONTH_SURVIVAL = 0.12
# Historical v1 vesting assumption only; current scoring requires explicit unlocks.
DEFAULT_VESTED_FRACTION = 0.7
# Historical v1 fallback only; current scoring never substitutes it for missing evidence.
FALLBACK_VALUE_USD = Decimal("20")
#: Annual opportunity cost of locked capital.
CAPITAL_RATE_ANNUAL = Decimal("0.08")
# Historical v1 multiplier only; current scoring refuses multi-wallet plans.
SYBIL_MULTIPLIER = Decimal("0.2")


class OpportunityKind(StrEnum):
    AIRDROP = "airdrop"
    POINTS = "points"
    NFT_MINT = "nft_mint"
    LISTING = "listing"


class SybilRisk(StrEnum):
    """How hard the programme is known to filter, not how hard we are cheating."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# ------------------------------------------------------------------------------- config


class HunterConfig(BaseModel):
    """Tunables. Read from the ``hunters:`` block of ``config/risk.yaml`` if present.

    ``RiskConfig`` ignores unknown top-level keys, so an operator can add the block without
    breaking the risk loader, and the hunters read it straight off disk the same way
    ``get_risk()`` does - the file is edited while the agent runs.
    """

    operator_hourly_usd: Decimal = Decimal("60")
    capital_rate_annual: Decimal = CAPITAL_RATE_ANNUAL
    ev_threshold_usd: Decimal = Decimal("25")
    # Historical configuration remains readable; these v1 payout knobs are ignored by
    # the scenario scorer. They are not estimates of expected proceeds or eligibility.
    sybil_multiplier: Decimal = SYBIL_MULTIPLIER
    tge_capture: float = TGE_SELL_BASE_RATE
    post_tge_survival: float = THREE_MONTH_SURVIVAL
    default_vested_fraction: float = DEFAULT_VESTED_FRACTION
    fallback_value_usd: Decimal = FALLBACK_VALUE_USD
    p_confirmed: float = 0.9
    p_points: float = 0.5
    p_speculative: float = 0.15
    max_wallets: int = 1


def _risk_path() -> Path:
    return Path(os.environ.get("KAIBA_RISK_PATH", DEFAULT_RISK_PATH))


def hunter_config(path: Path | None = None) -> HunterConfig:
    """Not cached: ``risk.yaml`` is edited while the agent runs."""
    p = path or _risk_path()
    block: dict[str, Any] = {}
    try:
        if p.exists():
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            block = dict(raw.get("hunters") or {})
    except (OSError, yaml.YAMLError) as exc:  # a broken config must not stop the hunt
        log.warning("hunters: could not read %s (%s); using defaults", p, exc)
    for env_key, field in (
        ("KAIBA_OPERATOR_HOURLY_USD", "operator_hourly_usd"),
        ("KAIBA_HUNTER_EV_THRESHOLD_USD", "ev_threshold_usd"),
    ):
        if os.environ.get(env_key):
            block[field] = os.environ[env_key]
    try:
        return HunterConfig.model_validate(block)
    except ValueError as exc:
        log.warning("hunters: invalid config block (%s); using defaults", exc)
        return HunterConfig()


# ------------------------------------------------------------------------------ evidence


def normalise_name(name: str) -> str:
    """Dedupe key half: ``"LayerZero (Season 2)"`` and ``"layerzero season 2"`` are one."""
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def normalise_url(url: str | None) -> str:
    """Scheme, ``www.``, query, fragment and trailing slash carry no identity."""
    if not url:
        return ""
    u = url.strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    u = u.split("?", 1)[0].split("#", 1)[0]
    return u.rstrip("/")


def opportunity_id(name: str, url: str | None, kind: str | OpportunityKind) -> str:
    k = kind.value if isinstance(kind, OpportunityKind) else str(kind)
    return digest({"k": k, "n": normalise_name(name), "u": normalise_url(url)})[:32]


class _ScenarioModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    @field_validator("*", mode="before")
    @classmethod
    def no_boolean_numbers(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("boolean is not a scenario number")
        return value


class AirdropSource(_ScenarioModel):
    """Receipt for an input document; its hash is declared, never fetched here."""

    source_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    url: str = Field(max_length=2048)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_ms: int = Field(strict=True, gt=0)

    @field_validator("url")
    @classmethod
    def https_receipt(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None):
            raise ValueError("source receipt requires an HTTPS URL without credentials")
        return value


class AirdropUnlock(_ScenarioModel):
    fraction: Decimal = Field(ge=0, le=1, max_digits=19, decimal_places=18)
    delay_days: int = Field(strict=True, ge=0, le=36_500)
    price_usd: Decimal = Field(ge=0, max_digits=28, decimal_places=12)
    price_source_id: str
    terms_source_id: str


class AirdropOutcome(_ScenarioModel):
    name: str = Field(min_length=1, max_length=128)
    probability: Decimal = Field(ge=0, le=1, max_digits=19, decimal_places=18)
    probability_source_id: str
    allocation_units: Decimal = Field(ge=0, max_digits=28, decimal_places=12)
    allocation_source_id: str
    unlocks: list[AirdropUnlock] = Field(max_length=16)
    claim_cost_usd: Decimal = Field(ge=0, max_digits=28, decimal_places=12)
    sale_cost_usd: Decimal = Field(ge=0, max_digits=28, decimal_places=12)
    execution_loss_usd: Decimal = Field(ge=0, max_digits=28, decimal_places=12)
    cost_source_id: str

    @model_validator(mode="after")
    def complete_allocation(self) -> AirdropOutcome:
        if self.allocation_units == 0:
            if self.unlocks:
                raise ValueError("zero allocation requires no unlocks")
        elif sum((u.fraction for u in self.unlocks), Decimal(0)) != 1:
            raise ValueError("positive allocation requires unlock fractions summing to one")
        return self


class AirdropValuation(_ScenarioModel):
    version: Literal["airdrop-scenarios/1"]
    opportunity_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    chain: Chain
    observed_ms: int = Field(strict=True, gt=0)
    expires_ms: int = Field(strict=True, gt=0)
    rules_source_id: str
    cost_source_id: str
    cost_inputs_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    discount_rate_annual: Decimal = Field(ge=0, le=100, max_digits=18, decimal_places=12)
    sources: list[AirdropSource] = Field(min_length=1, max_length=16)
    outcomes: list[AirdropOutcome] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def complete_scenarios(self) -> AirdropValuation:
        ids = {s.source_id for s in self.sources}
        if len(ids) != len(self.sources):
            raise ValueError("source IDs must be unique")
        references = {self.rules_source_id, self.cost_source_id}
        for outcome in self.outcomes:
            references.update((outcome.probability_source_id, outcome.allocation_source_id,
                               outcome.cost_source_id))
            for unlock in outcome.unlocks:
                references.update((unlock.price_source_id, unlock.terms_source_id))
        if not references <= ids:
            raise ValueError("every scenario input requires a source receipt")
        if sum((o.probability for o in self.outcomes), Decimal(0)) != 1:
            raise ValueError("outcome probabilities must sum to one, including zero reward")
        if not 0 < self.expires_ms - self.observed_ms <= MAX_SCENARIO_AGE_MS:
            raise ValueError("scenario lifetime must be positive and at most 24 hours")
        if any(s.observed_ms > self.observed_ms for s in self.sources):
            raise ValueError("source receipt cannot postdate the scenario")
        if len(self.model_dump_json().encode("utf-8")) > MAX_SCENARIO_BYTES:
            raise ValueError("scenario receipt exceeds bounded storage")
        return self


class OpportunityEvidence(BaseModel):
    """What we actually know about an opportunity, with its sources attached.

    Anything unknown stays unknown: ``expected_value_usd=None`` means we have no
    comparable, and the scorer says so in the rationale instead of inventing a number that
    reads as safe.
    """

    model_config = ConfigDict(extra="forbid")

    kind: OpportunityKind
    name: str
    chain: Chain | None = None
    confirmed_token: bool = False
    points_program: bool = False
    prior_airdrop_history: list[Decimal] = Field(default_factory=list)
    expected_value_usd: Decimal | None = None
    valuation_scenarios: AirdropValuation | None = None
    vested_fraction: float | None = None
    capital_required_usd: Decimal = Decimal("0")
    capital_lockup_days: int = 0
    time_cost_hours: float = 0.0
    gas_cost_usd: Decimal = Decimal("0")
    deadline_ms: int | None = None
    sybil_risk: SybilRisk = SybilRisk.MEDIUM
    wallet_count: int = 1
    chain_hint: str | None = None
    source_count: int = 1
    sources: list[str] = Field(default_factory=list)
    official_url: str | None = None
    url: str | None = None
    symbol: str | None = None
    first_seen_ms: int = Field(default_factory=now_ms)
    meta: dict[str, Any] = Field(default_factory=dict)

    @property
    def link(self) -> str | None:
        return self.official_url or self.url

    @property
    def key(self) -> str:
        return opportunity_id(self.name, self.link, self.kind)

    @property
    def dedupe_key(self) -> tuple[str, str]:
        return (normalise_name(self.name), normalise_url(self.link))


class EvScore(BaseModel):
    """An auditable estimate, or an explicit unknown. NFT callers retain v1 defaults."""

    ev_usd: Decimal | None
    confidence: float
    rationale: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    # audit trail for the dashboard and the weekly report
    gross_usd: Decimal | None = Decimal("0")
    cost_usd: Decimal | None = Decimal("0")
    p_token: float | None = 0.0
    haircut: float | None = 0.0
    model_version: str = "hunter-ev/1"
    valuation_available: bool = True
    missing_inputs: list[str] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)
    refused: bool = False
    blockers: list[str] = Field(default_factory=list)

    @property
    def positive(self) -> bool:
        return self.ev_usd is not None and self.ev_usd > 0 and not self.refused


def _d(value: float | int | str | Decimal) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT)


def haircut_for(vested_fraction: float, cfg: HunterConfig) -> float:
    """Historical v1 diagnostic only; these rates cannot estimate proceeds."""
    v = max(0.0, min(1.0, vested_fraction))
    return (1.0 - v) * cfg.tge_capture + v * cfg.post_tge_survival


def cost_breakdown(opp: OpportunityEvidence, cfg: HunterConfig) -> tuple[Decimal, list[str]]:
    gas = _d(opp.gas_cost_usd)
    capital = _d(opp.capital_required_usd)
    lockup = Decimal(max(0, opp.capital_lockup_days))
    carry = capital * (lockup / Decimal(365)) * cfg.capital_rate_annual
    time_cost = _d(opp.time_cost_hours) * cfg.operator_hourly_usd
    lines = [
        f"cost: gas ${_money(gas)}",
        f"cost: capital carry ${_money(carry)} = ${capital} x {lockup}/365 x {cfg.capital_rate_annual}",
        f"cost: time ${_money(time_cost)} = {opp.time_cost_hours}h x ${cfg.operator_hourly_usd}/h",
    ]
    return gas + carry + time_cost, lines


def valuation_cost_binding(opp: OpportunityEvidence, cfg: HunterConfig) -> str:
    """Bind a source receipt to all declared base costs used by this calculation.

    The receipt's cost source must support these estimates. The hash binds values; it
    does not verify their accuracy. Canonical numeric strings tolerate harmless scale.
    """
    values = {
        "gas_cost_usd": opp.gas_cost_usd,
        "capital_required_usd": opp.capital_required_usd,
        "capital_lockup_days": opp.capital_lockup_days,
        "time_cost_hours": opp.time_cost_hours,
        "operator_hourly_usd": cfg.operator_hourly_usd,
        "capital_rate_annual": cfg.capital_rate_annual,
    }
    canonical = {}
    for key, value in values.items():
        number = _d(value)
        if not number.is_finite() or not 0 <= number <= Decimal("1e16"):
            raise ValueError("base cost inputs must be finite, bounded and nonnegative")
        canonical[key] = format(number.normalize(), "f")
    return digest(canonical)


def _score_opportunity(
    opp: OpportunityEvidence, cfg: HunterConfig | None = None, *, at_ms: int | None = None,
) -> EvScore:
    """Value explicit joint outcomes, without inventing an allocation or probability.

    Each probability includes token launch, organic eligibility, claim and sale risk for
    that outcome. No extra static token, Sybil or price-retention multiplier is applied.
    Unlock proceeds use simple discounting: units*fraction*price/(1+rate*days/365).
    Base participation costs are unconditional; outcome claim/sale/loss costs are weighted
    by their outcome probability. Execution loss must exclude already netted sale fees.
    """
    cfg = cfg or hunter_config()
    at = now_ms() if at_ms is None else at_ms
    missing: list[str] = []
    rationale = ["Source-bound scenario estimates; not promised payouts or eligibility proof."]
    warnings = ["Allocation, probabilities, future prices and costs are declared estimates; "
                "independent qualification is required."]
    cost: Decimal | None = None
    try:
        binding = valuation_cost_binding(opp, cfg)
        cost, lines = cost_breakdown(opp, cfg)
        rationale.extend(lines)
    except (ValueError, InvalidOperation, OverflowError):
        binding = None
        missing.append("base_cost_inputs_invalid")

    bundle = None
    if opp.valuation_scenarios is None:
        missing.append("source_bound_scenarios_missing")
    else:
        # Revalidate mutable/model_copy inputs rather than trusting a previous parse.
        try:
            raw = opp.valuation_scenarios
            bundle = AirdropValuation.model_validate(
                raw.model_dump(mode="json") if isinstance(raw, AirdropValuation) else raw,
            )
        except (ValueError, TypeError):
            missing.append("source_bound_scenarios_invalid")
    if bundle is not None:
        if bundle.opportunity_id != opp.key or bundle.chain != opp.chain:
            missing.append("scenario_identity_mismatch")
        if not bundle.observed_ms <= at <= bundle.expires_ms:
            missing.append("scenario_stale_or_future")
        if any(not 0 <= at - s.observed_ms <= MAX_SCENARIO_AGE_MS for s in bundle.sources):
            missing.append("scenario_source_stale_or_future")
        if bundle.cost_inputs_sha256 != binding:
            missing.append("scenario_cost_inputs_mismatch")
    if opp.kind not in {OpportunityKind.AIRDROP, OpportunityKind.POINTS}:
        missing.append("airdrop_scenarios_wrong_kind")

    blockers = []
    if opp.wallet_count != 1 or opp.wallet_count > cfg.max_wallets:
        blockers.append("single_wallet_required")
        warnings.append("Kaiba requires one organic wallet; a multi-wallet payout is not valued.")
    gross: Decimal | None = None
    ev: Decimal | None = None
    provenance: dict[str, Any] = {"assumptions": "declared estimates"}
    if bundle is not None and not missing and not blockers and cost is not None:
        gross = Decimal(0)
        extra_cost = Decimal(0)
        for outcome in bundle.outcomes:
            proceeds = sum((
                outcome.allocation_units * unlock.fraction * unlock.price_usd
                / (1 + bundle.discount_rate_annual * Decimal(unlock.delay_days) / Decimal(365))
                for unlock in outcome.unlocks
            ), Decimal(0))
            execution_cost = (outcome.claim_cost_usd + outcome.sale_cost_usd
                              + outcome.execution_loss_usd)
            gross += outcome.probability * proceeds
            extra_cost += outcome.probability * execution_cost
            rationale.append(
                f"outcome {outcome.name}: probability {outcome.probability}, allocation "
                f"{outcome.allocation_units}, discounted proceeds ${_money(proceeds)}, "
                f"claim/sale/execution costs ${_money(execution_cost)}"
            )
        cost += extra_cost
        ev = _money(gross - cost)
        rationale.append(f"EV = weighted proceeds ${_money(gross)} - total costs "
                         f"${_money(cost)} = ${ev}; no static retention multiplier")
        if ev <= 0:
            warnings.append(f"non-positive scenario EV (${ev}): do not spend capital or hours here")
        provenance.update({
            "scenario_version": bundle.version,
            "scenario_sha256": digest(bundle.model_dump(mode="json")),
            "source_ids": [s.source_id for s in bundle.sources],
            "observed_ms": bundle.observed_ms,
            "expires_ms": bundle.expires_ms,
            "cost_inputs_sha256": bundle.cost_inputs_sha256,
        })
    else:
        warnings.append("EV unavailable: missing or invalid source-bound valuation inputs")
        rationale.append("Headline values, comparable payouts, token/points labels and legacy "
                         "recipient sell/price-decline rates do not supply a current dollar EV.")
    if opp.deadline_ms is not None:
        remaining_h = (opp.deadline_ms - at) / 3_600_000
        rationale.append(f"deadline in {remaining_h:.1f}h")
        if remaining_h < 0:
            warnings.append("deadline has passed")
        elif remaining_h < 48:
            warnings.append(f"deadline in {remaining_h:.1f}h: organic activity window may be too short")
    if opp.capital_lockup_days > 90:
        warnings.append(f"capital locked {opp.capital_lockup_days} days: include its opportunity cost")
    return EvScore(
        ev_usd=ev, gross_usd=_money(gross) if gross is not None else None,
        cost_usd=_money(cost) if cost is not None else None,
        confidence=confidence_for(opp, ev is None), rationale=rationale, warnings=warnings,
        p_token=None, haircut=None, model_version=AIRDROP_EV_VERSION,
        valuation_available=ev is not None, missing_inputs=missing, provenance=provenance,
        refused=bool(blockers), blockers=blockers,
    )


def score_opportunity(
    opp: OpportunityEvidence, cfg: HunterConfig | None = None, *, at_ms: int | None = None,
) -> EvScore:
    """Validate and value one scenario with precision sufficient for bounded inputs."""
    with localcontext() as context:
        context.prec = 80
        return _score_opportunity(opp, cfg, at_ms=at_ms)


def confidence_for(opp: OpportunityEvidence, guessed_value: bool) -> float:
    """How much we trust the inputs - separate from how big the number is."""
    c = 0.2
    if opp.confirmed_token:
        c += 0.25
    elif opp.points_program:
        c += 0.1
    if len(opp.prior_airdrop_history) >= 2:
        c += 0.15
    elif opp.prior_airdrop_history:
        c += 0.05
    if opp.official_url:
        c += 0.1
    c += min(0.2, 0.05 * max(0, opp.source_count - 1))
    if guessed_value:
        c -= 0.2
    if opp.sybil_risk is SybilRisk.HIGH:
        c -= 0.1
    return round(max(0.05, min(0.95, c)), 2)


def rank(
    scored: list[tuple[OpportunityEvidence, EvScore]],
) -> list[tuple[OpportunityEvidence, EvScore]]:
    """Highest EV first; refused plans sink regardless of their arithmetic."""
    return sorted(scored, key=lambda pair: (
        not pair[1].refused, pair[1].ev_usd is not None,
        pair[1].ev_usd if pair[1].ev_usd is not None else Decimal(0),
    ), reverse=True)


def merge_evidence(a: OpportunityEvidence, b: OpportunityEvidence) -> OpportunityEvidence:
    """Two sources on one opportunity. Corroboration raises ``source_count``; the more
    definite claim wins each field, because "confirmed" is a fact and "maybe" is an absence.
    """
    sources = list(dict.fromkeys([*a.sources, *b.sources]))
    merged = a.model_copy(
        update={
            "confirmed_token": a.confirmed_token or b.confirmed_token,
            "points_program": a.points_program or b.points_program,
            "prior_airdrop_history": a.prior_airdrop_history or b.prior_airdrop_history,
            "expected_value_usd": (
                a.expected_value_usd if a.expected_value_usd is not None else b.expected_value_usd
            ),
            # Keep the receipt intact. If conservative merged costs/identity differ, the
            # scorer rejects its binding instead of silently rebuilding its assumptions.
            "valuation_scenarios": a.valuation_scenarios or b.valuation_scenarios,
            "vested_fraction": a.vested_fraction if a.vested_fraction is not None else b.vested_fraction,
            "capital_required_usd": max(a.capital_required_usd, b.capital_required_usd),
            "capital_lockup_days": max(a.capital_lockup_days, b.capital_lockup_days),
            "time_cost_hours": max(a.time_cost_hours, b.time_cost_hours),
            "gas_cost_usd": max(a.gas_cost_usd, b.gas_cost_usd),
            "deadline_ms": a.deadline_ms if a.deadline_ms is not None else b.deadline_ms,
            "sybil_risk": max(a.sybil_risk, b.sybil_risk, key=lambda r: list(SybilRisk).index(r)),
            "wallet_count": max(a.wallet_count, b.wallet_count),
            "official_url": a.official_url or b.official_url,
            "url": a.url or b.url,
            "symbol": a.symbol or b.symbol,
            "chain": a.chain or b.chain,
            "sources": sources,
            "source_count": len(sources) or max(a.source_count, b.source_count),
            "meta": {**b.meta, **a.meta},
            "first_seen_ms": min(a.first_seen_ms, b.first_seen_ms),
        }
    )
    return merged


def dedupe(items: list[OpportunityEvidence]) -> list[OpportunityEvidence]:
    """Collapse by normalised name + url, keeping the first occurrence's position."""
    by_key: dict[tuple[str, str], OpportunityEvidence] = {}
    by_name: dict[str, tuple[str, str]] = {}
    order: list[tuple[str, str]] = []
    for item in items:
        key = item.dedupe_key
        # same name, one source with a url and one without: still the same programme
        if key not in by_key and key[0] in by_name:
            existing = by_name[key[0]]
            if not key[1] or not existing[1]:
                key = existing
        if key in by_key:
            by_key[key] = merge_evidence(by_key[key], item)
        else:
            by_key[key] = item
            by_name.setdefault(key[0], key)
            order.append(key)
    return [by_key[k] for k in order]
