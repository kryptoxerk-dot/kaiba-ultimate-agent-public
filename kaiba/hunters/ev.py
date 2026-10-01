"""Expected value for hunter opportunities - the module that says "no" for us.

Every other hunter file is a scraper. This one is the reason the scrapers are allowed to
exist: it turns "S-tier airdrop, don't miss out" into a dollar number with the published
base rates priced in, so the agent ranks by EV instead of by how loud the source was.

The numbers are not invented. From ``docs/research/06-hunters-airdrops-nft.md``:

* typical drop $20-100, ~20% meaningful, **often 70% vested**;
* **88% of airdropped tokens lose value within three months**;
* **64% of recipients sell at TGE**;
* Sybil filtering is now routine - LayerZero flagged 803k of >2M addresses, Arbitrum,
  zkSync and Linea all cut connected components above ~20 addresses, and blacklists are
  shared across projects retroactively.

Those become three multipliers:

``p_token``
    0.9 confirmed token, 0.5 announced points programme, 0.15 speculative. A points
    programme is a promise, not a token; a blog post is not a promise.

``haircut``
    ``unlocked * 0.64 + vested * 0.12``. The unlocked part is marked at the TGE-sell base
    rate because we are one of the 64% selling into the same TGE liquidity, not a patient
    holder with a better exit. The vested part is marked at 12% - the complement of the
    88% three-month decay rate - because by the time it unlocks the base rate has had its
    way with it. A fully-unvested drop therefore keeps 64% of the headline comparable; a
    typical 70%-vested drop keeps 27.6%.

``sybil``
    Any plan needing more than one wallet multiplies the *gross* expected value by 0.2.
    It hits the gross rather than the net because a cluster cut destroys the reward and
    leaves every cost exactly where it was - gas is still spent, capital is still locked.
    That is strictly harsher than scaling the net, which is the point.

Costs are real and certain while the reward is a probability, so they are subtracted at
full weight: gas + ``capital * lockup_days/365 * 8%`` (what the same capital earns sitting
in a money-market or a stablecoin vault) + operator hours at the configured rate.

Money is ``Decimal`` everywhere, per ``docs/CONTRACT.md``. Probabilities and fractions are
floats and are converted at the boundary; they never touch a dollar as a float.
"""

from __future__ import annotations

import logging
import os
import re
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from kaiba.core.config import DEFAULT_RISK_PATH
from kaiba.core.schemas import Chain, digest, now_ms

log = logging.getLogger(__name__)

CENT = Decimal("0.01")

# ---------------------------------------------------------------------------- base rates

#: Share of recipients who sell at TGE (research 06). We model ourselves as one of them.
TGE_SELL_BASE_RATE = 0.64
#: Complement of "88% of airdropped tokens lose value within three months".
THREE_MONTH_SURVIVAL = 0.12
#: Research: drops are "often 70% vested". Used when a programme does not state its terms.
DEFAULT_VESTED_FRACTION = 0.7
#: Published low end of the typical $20-100 drop, used only when we have no comparables.
FALLBACK_VALUE_USD = Decimal("20")
#: Annual opportunity cost of locked capital.
CAPITAL_RATE_ANNUAL = Decimal("0.08")
#: What a multi-wallet plan is worth once >=20-address cluster cuts are priced in.
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


#: Probability that an organic single wallet is still counted eligible after filtering.
ELIGIBILITY: dict[SybilRisk, float] = {
    SybilRisk.LOW: 1.0,
    SybilRisk.MEDIUM: 0.85,
    SybilRisk.HIGH: 0.6,
}


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
    """A dollar number a human can audit line by line."""

    ev_usd: Decimal
    confidence: float
    rationale: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    # audit trail for the dashboard and the weekly report
    gross_usd: Decimal = Decimal("0")
    cost_usd: Decimal = Decimal("0")
    p_token: float = 0.0
    haircut: float = 0.0
    refused: bool = False
    blockers: list[str] = Field(default_factory=list)

    @property
    def positive(self) -> bool:
        return self.ev_usd > 0 and not self.refused


def _d(value: float | int | str | Decimal) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT)


def p_token(opp: OpportunityEvidence, cfg: HunterConfig) -> tuple[float, str]:
    if opp.confirmed_token:
        return cfg.p_confirmed, "confirmed token"
    if opp.points_program:
        return cfg.p_points, "announced points programme"
    return cfg.p_speculative, "speculative (no token, no points programme)"


def haircut_for(vested_fraction: float, cfg: HunterConfig) -> float:
    """``unlocked * TGE capture + vested * three-month survival``."""
    v = max(0.0, min(1.0, vested_fraction))
    return (1.0 - v) * cfg.tge_capture + v * cfg.post_tge_survival


def expected_value(opp: OpportunityEvidence, cfg: HunterConfig) -> tuple[Decimal, str, bool]:
    """Headline value before any haircut. Returns (value, how we got it, is_guess)."""
    if opp.expected_value_usd is not None:
        return _d(opp.expected_value_usd), "stated expected value", False
    hist = sorted(_d(h) for h in opp.prior_airdrop_history)
    if hist:
        mid = len(hist) // 2
        median = hist[mid] if len(hist) % 2 else (hist[mid - 1] + hist[mid]) / 2
        return (
            _d(median),
            f"median of {len(hist)} comparable outcome(s) {[str(h) for h in hist]}",
            False,
        )
    return cfg.fallback_value_usd, "no comparable: published low end of the $20-100 range", True


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


def score_opportunity(opp: OpportunityEvidence, cfg: HunterConfig | None = None) -> EvScore:
    """Rank by expected value, not by hype. Every number used ends up in the rationale."""
    cfg = cfg or hunter_config()
    rationale: list[str] = []
    warnings: list[str] = []

    p, p_why = p_token(opp, cfg)
    rationale.append(f"p(token) = {p} ({p_why})")

    value, value_why, guessed = expected_value(opp, cfg)
    rationale.append(f"headline value = ${value} ({value_why})")
    if guessed:
        warnings.append(
            "no comparable airdrop history: value is the published $20 low end, not evidence"
        )

    vested = opp.vested_fraction
    if vested is None:
        vested = cfg.default_vested_fraction
        rationale.append(
            f"vesting unstated: assuming the published {int(cfg.default_vested_fraction * 100)}% "
            "vested base rate"
        )
    hc = haircut_for(vested, cfg)
    rationale.append(
        f"haircut = {hc:.4f} = unlocked {1 - vested:.2f} x {cfg.tge_capture} TGE-sell base rate "
        f"+ vested {vested:.2f} x {cfg.post_tge_survival} three-month survival (88% decay)"
    )

    elig = ELIGIBILITY[opp.sybil_risk]
    rationale.append(f"eligibility after the programme's own filtering = {elig} ({opp.sybil_risk} sybil risk)")
    if opp.sybil_risk is SybilRisk.HIGH:
        warnings.append("programme filters hard: expect self-report/appeal work even for an organic wallet")

    gross = _d(p) * value * _d(hc) * _d(elig)
    rationale.append(
        f"gross = {p} x ${value} x {hc:.4f} x {elig} = ${_money(gross)}"
    )

    if opp.wallet_count > cfg.max_wallets:
        gross = gross * cfg.sybil_multiplier
        rationale.append(
            f"sybil penalty x{cfg.sybil_multiplier} on gross -> ${_money(gross)} "
            f"({opp.wallet_count} wallets; >=20-address clusters are cut outright and blacklists "
            "are retroactive and shared between projects)"
        )
        warnings.append(
            f"multi-wallet plan ({opp.wallet_count} wallets): Kaiba does not Sybil - "
            "single-wallet organic participation only"
        )

    cost, cost_lines = cost_breakdown(opp, cfg)
    rationale.extend(cost_lines)
    rationale.append(f"total cost = ${_money(cost)}")

    ev = _money(gross - cost)
    rationale.append(f"EV = gross ${_money(gross)} - cost ${_money(cost)} = ${ev}")

    if ev <= 0:
        warnings.append(f"negative expected value (${ev}): do not spend capital or hours here")

    if opp.deadline_ms is not None:
        remaining_h = (opp.deadline_ms - now_ms()) / 3_600_000
        if remaining_h < 0:
            warnings.append("deadline has passed")
        elif remaining_h < 48:
            warnings.append(f"deadline in {remaining_h:.1f}h: not enough time for organic activity")
        rationale.append(f"deadline in {remaining_h:.1f}h")

    if opp.capital_lockup_days > 90:
        warnings.append(
            f"capital locked {opp.capital_lockup_days} days: that is a bet on the programme, "
            "not a free lunch"
        )

    return EvScore(
        ev_usd=ev,
        confidence=confidence_for(opp, guessed),
        rationale=rationale,
        warnings=warnings,
        gross_usd=_money(gross),
        cost_usd=_money(cost),
        p_token=p,
        haircut=round(hc, 4),
    )


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
    return sorted(scored, key=lambda pair: (not pair[1].refused, pair[1].ev_usd), reverse=True)


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
