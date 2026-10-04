"""Canonical types for the whole agent.

Rules that the rest of the codebase relies on:

* Missing evidence is ``None`` and ``EvidenceBasis.UNAVAILABLE`` — never 0 and never a
  reassuring default. A failed provider lookup must not read as a clean result.
* On-chain amounts are integers in base units (lamports / wei / token atoms). USD values
  are ``Decimal``. Floats never touch money.
* Every record that leaves a provider carries a :class:`Receipt` so a number can be traced
  back to who said it and when.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# --------------------------------------------------------------------------------------
# chains
# --------------------------------------------------------------------------------------


class Chain(StrEnum):
    """Chains the agent can see or trade. Values match GMGN's `--chain` argument."""

    SOL = "sol"
    ETH = "eth"
    BSC = "bsc"
    BASE = "base"
    ROBINHOOD = "robinhood"
    ARC = "arc"
    STABLE = "stable"


EVM_CHAINS: frozenset[Chain] = frozenset(
    {Chain.ETH, Chain.BSC, Chain.BASE, Chain.ROBINHOOD, Chain.ARC, Chain.STABLE}
)

CHAIN_IDS: dict[Chain, int] = {
    Chain.ETH: 1,
    Chain.BSC: 56,
    Chain.BASE: 8453,
    Chain.ROBINHOOD: 4663,
}

NATIVE_SYMBOL: dict[Chain, str] = {
    Chain.SOL: "SOL",
    Chain.ETH: "ETH",
    Chain.BSC: "BNB",
    Chain.BASE: "ETH",
    Chain.ROBINHOOD: "ETH",
    Chain.ARC: "ETH",
    Chain.STABLE: "ETH",
}

NATIVE_DECIMALS: dict[Chain, int] = {c: (9 if c is Chain.SOL else 18) for c in Chain}

_EVM_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_B58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

EVM_ZERO = "0x0000000000000000000000000000000000000000"
SOL_NATIVE_MINT = "So11111111111111111111111111111111111111112"

#: The assets a position is DENOMINATED in, per chain: native, wrapped native and the
#: major stables. A candidate that is one of these is not a trade -- it is paying an asset
#: to receive the same asset, or swapping a dollar for a dollar, and the only certain
#: outcome is the round trip. See ``engine.decide``'s ``token_is_quote_asset`` refusal.
#:
#: MEASURED addresses only. A chain absent from this table (or an asset not listed) is
#: still covered by the structural check in ``engine.decide``, which compares the order's
#: own input and output legs; listing a guessed address here would be worse than the gap,
#: because a wrong entry silently admits the mint it was meant to refuse.
QUOTE_ASSETS: dict[Chain, frozenset[str]] = {
    Chain.SOL: frozenset({
        SOL_NATIVE_MINT,                                 # WSOL
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
        "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
    }),
    Chain.BSC: frozenset({
        EVM_ZERO,
        "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",  # WBNB
        "0x55d398326f99059ff775485246999027b3197955",  # USDT (BSC-USD)
        "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d",  # USDC
        "0xe9e7cea3dedca5984780bafc599bd69add087d56",  # BUSD
    }),
    Chain.ETH: frozenset({
        EVM_ZERO,
        "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",  # WETH
        "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",  # USDC
        "0xdac17f958d2ee523a2206206994597c13d831ec7",  # USDT
    }),
    Chain.BASE: frozenset({
        EVM_ZERO,
        "0x4200000000000000000000000000000000000006",  # WETH
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
    }),
    # Robinhood chain: the native leg is ETH and the wrapped/stable set is not MEASURED
    # here, so only EVM_ZERO is listed. `viability.read_pons_venue` already reads each
    # token's own `quote_is_native` from its `tokens` row, which is the per-token answer.
    Chain.ROBINHOOD: frozenset({EVM_ZERO}),
}


def is_quote_asset(chain: "Chain", token: str) -> bool:
    """True when ``token`` is an asset positions are denominated in on ``chain``.

    Case-insensitive on EVM (addresses reach us in both checksummed and lower forms) and
    exact on Solana, where base58 is case-significant.
    """
    known = QUOTE_ASSETS.get(chain)
    if not known:
        return False
    if token in known:
        return True
    return token.lower() in {k.lower() for k in known if k.startswith("0x")}


def looks_evm(address: str) -> bool:
    return bool(_EVM_RE.match(address.strip()))


def looks_solana(address: str) -> bool:
    a = address.strip()
    return bool(_B58_RE.match(a)) and not a.startswith("0x")


def infer_chain(address: str) -> Chain | None:
    """Best-effort chain family from an address shape.

    EVM addresses are ambiguous across EVM chains, so this returns :attr:`Chain.ETH` as the
    family marker; callers that know better must pass the chain explicitly.
    """
    if looks_evm(address):
        return Chain.ETH
    if looks_solana(address):
        return Chain.SOL
    return None


def normalize_address(address: str, chain: Chain) -> str:
    """Lowercase EVM addresses; leave base58 untouched (Solana is case-sensitive)."""
    a = address.strip()
    if chain in EVM_CHAINS:
        if not looks_evm(a):
            raise ValueError(f"not an EVM address: {a!r}")
        return a.lower()
    if not looks_solana(a):
        raise ValueError(f"not a Solana address: {a!r}")
    return a


def now_ms() -> int:
    return int(time.time() * 1000)


def digest(payload: Any) -> str:
    """Stable sha256 of any JSON-able payload, used for dedupe and evidence ids."""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


# --------------------------------------------------------------------------------------
# evidence
# --------------------------------------------------------------------------------------


class EvidenceBasis(StrEnum):
    """How much a number is worth. Adopted from the draft architecture note."""

    VERIFIED_ONCHAIN = "verified_onchain"
    PROVIDER_REPORTED = "provider_reported"
    DERIVED = "derived"
    ESTIMATED = "estimated"
    CACHED = "cached"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


class Receipt(BaseModel):
    """Proof that a provider said something at a point in time."""

    model_config = ConfigDict(frozen=True)

    provider: str
    endpoint: str
    observed_at_ms: int = Field(default_factory=now_ms)
    basis: EvidenceBasis = EvidenceBasis.PROVIDER_REPORTED
    request_digest: str | None = None
    response_digest: str | None = None
    note: str | None = None

    @property
    def age_seconds(self) -> float:
        return (now_ms() - self.observed_at_ms) / 1000.0


class Measure(BaseModel):
    """A single number plus its provenance. ``value is None`` means genuinely unknown."""

    value: Decimal | None = None
    basis: EvidenceBasis = EvidenceBasis.UNAVAILABLE
    receipt: Receipt | None = None
    freshness_budget_s: int = 3600

    @property
    def known(self) -> bool:
        return self.value is not None and self.basis is not EvidenceBasis.UNAVAILABLE

    @property
    def stale(self) -> bool:
        if self.receipt is None:
            return not self.known
        return self.receipt.age_seconds > self.freshness_budget_s

    @classmethod
    def unknown(cls, budget_s: int = 3600) -> Measure:
        return cls(value=None, basis=EvidenceBasis.UNAVAILABLE, freshness_budget_s=budget_s)


# --------------------------------------------------------------------------------------
# wallets
# --------------------------------------------------------------------------------------


class WalletTag(StrEnum):
    """Provider tags plus the archetypes we derive ourselves."""

    # GMGN / provider supplied
    SMART_MONEY = "smart_money"
    PUMP_SMART = "pump_smart"
    RENOWNED = "renowned"
    KOL = "kol"
    SNIPER = "sniper"
    INSIDER = "insider"
    SUSPECTED_INSIDER = "suspected_insider"
    BUNDLER = "bundler"
    DEV = "dev"
    FRESH_WALLET = "fresh_wallet"
    RAT_TRADER = "rat_trader"
    DEX_BOT = "dex_bot"
    MEV_BOT = "mev_bot"
    SANDWICH_BOT = "sandwich_bot"
    WASH_TRADER = "wash_trader"
    TRANSFER_IN = "transfer_in"
    BLUECHIP_OWNER = "bluechip_owner"
    SCAMMER = "scammer"
    PAPER_HAND = "paper_hand"
    DIAMOND_HAND = "diamond_hand"
    EXCHANGE = "exchange"
    POOL = "pool"
    CONTRACT = "contract"
    # derived by kaiba
    EARLY_BUYER = "early_buyer"
    SIDE_WALLET = "side_wallet"
    COPYBOT = "copybot"
    FOMO = "fomo"
    POSITION_HOLDER = "position_holder"
    TOP_TRADER = "top_trader"
    TOP_HOLDER = "top_holder"


#: Tags that disqualify a wallet from ever being a copy source.
HARD_QUARANTINE_TAGS: frozenset[WalletTag] = frozenset(
    {
        WalletTag.MEV_BOT,
        WalletTag.SANDWICH_BOT,
        WalletTag.WASH_TRADER,
        WalletTag.SCAMMER,
        WalletTag.EXCHANGE,
        WalletTag.POOL,
        WalletTag.CONTRACT,
    }
)

#: Tags that are a warning, scored as penalties rather than an outright block.
SOFT_PENALTY_TAGS: frozenset[WalletTag] = frozenset(
    {WalletTag.BUNDLER, WalletTag.RAT_TRADER, WalletTag.DEX_BOT, WalletTag.TRANSFER_IN}
)


class Grade(StrEnum):
    A = "A"
    B = "B"
    C = "C"
    D = "D"
    UNSCORED = "UNSCORED"
    QUARANTINED = "QUARANTINED"


class Archetype(StrEnum):
    INSIDER = "insider"
    SNIPER = "sniper"
    KOL = "kol"
    SIDE_WALLET = "side_wallet"
    BUNDLER = "bundler"
    DEV = "dev"
    EARLY_BUYER = "early_buyer"
    SMART_MONEY = "smart_money"
    TOP_TRADER = "top_trader"
    TOP_HOLDER = "top_holder"
    POSITION_HOLDER = "position_holder"
    DIAMOND = "diamond"
    FOMO = "fomo"
    COPYBOT = "copybot"
    BOT = "bot"
    TRADER = "trader"


class Wallet(BaseModel):
    """A tracked address. ``chain`` is the family the address was observed on."""

    address: str
    chain: Chain
    name: str | None = None
    source: str = "unknown"
    tags: list[WalletTag] = Field(default_factory=list)
    first_seen_ms: int = Field(default_factory=now_ms)
    last_seen_ms: int = Field(default_factory=now_ms)
    first_funder: str | None = None
    first_funded_ms: int | None = None
    twitter: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)

    @field_validator("address")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @property
    def key(self) -> str:
        return f"{self.chain.value}:{self.address}"


class ScoreFactor(BaseModel):
    name: str
    points: float
    max_points: float
    detail: str | None = None


class WalletScore(BaseModel):
    """Output of :mod:`kaiba.intelligence.grade`.

    ``evidence_weight`` is the sum of ``max_points`` for factors we could actually compute;
    the score is normalised over that, so a wallet with thin data cannot reach grade A.
    """

    address: str
    chain: Chain
    score: float
    grade: Grade
    evidence_weight: float
    factors: list[ScoreFactor] = Field(default_factory=list)
    penalties: list[str] = Field(default_factory=list)
    archetype: Archetype = Archetype.TRADER
    blockers: list[str] = Field(default_factory=list)
    realized_pnl_usd: Decimal | None = None
    win_rate: float | None = None
    closed_trades: int | None = None
    distinct_tokens: int | None = None
    median_hold_s: int | None = None
    scored_at_ms: int = Field(default_factory=now_ms)
    model_version: str = "kaiba-wallet-v1"
    receipts: list[Receipt] = Field(default_factory=list)


# --------------------------------------------------------------------------------------
# clustering
# --------------------------------------------------------------------------------------


class EdgeType(StrEnum):
    """Relations between wallets. Hard links justify a merge on their own."""

    CO_SIGNED = "co_signed"  # hard
    SAME_BUNDLE = "same_bundle"  # hard
    SHARED_ALT_AUTHORITY = "shared_alt_authority"  # hard
    DIRECT_TRANSFER = "direct_transfer"  # hard
    SAME_FUNDER = "same_funder"  # soft
    SAME_SLOT_BUY = "same_slot_buy"  # soft
    FIRST_N_COOCCUR = "first_n_cooccur"  # soft
    LEAD_LAG = "lead_lag"  # soft
    SHARED_CEX_DEPOSIT = "shared_cex_deposit"  # soft
    SHARED_COUNTERPARTY = "shared_counterparty"  # soft


HARD_EDGES: frozenset[EdgeType] = frozenset(
    {EdgeType.CO_SIGNED, EdgeType.SAME_BUNDLE, EdgeType.SHARED_ALT_AUTHORITY, EdgeType.DIRECT_TRANSFER}
)


class ClusterEdge(BaseModel):
    chain: Chain
    a: str
    b: str
    edge_type: EdgeType
    confidence: float = 0.5
    observations: int = 1
    first_seen_ms: int = Field(default_factory=now_ms)
    last_seen_ms: int = Field(default_factory=now_ms)
    evidence: list[str] = Field(default_factory=list)  # tx signatures / hashes

    @field_validator("confidence")
    @classmethod
    def _clamp(cls, v: float) -> float:
        return max(0.0, min(1.0, v))

    def sorted_pair(self) -> tuple[str, str]:
        return (self.a, self.b) if self.a <= self.b else (self.b, self.a)


class Entity(BaseModel):
    """A set of wallets believed to be one operator."""

    entity_id: str
    chain: Chain
    members: list[str]
    confidence: float
    archetype: Archetype = Archetype.TRADER
    label: str | None = None
    edge_types: list[EdgeType] = Field(default_factory=list)
    created_ms: int = Field(default_factory=now_ms)

    @property
    def size(self) -> int:
        return len(self.members)


# --------------------------------------------------------------------------------------
# tokens
# --------------------------------------------------------------------------------------


class TokenRisk(StrEnum):
    HONEYPOT = "honeypot"
    MINT_AUTHORITY = "mint_authority"
    FREEZE_AUTHORITY = "freeze_authority"
    TRANSFER_HOOK = "transfer_hook"
    TRANSFER_FEE = "transfer_fee"
    LP_NOT_BURNED = "lp_not_burned"
    HIGH_TAX = "high_tax"
    TOP10_CONCENTRATION = "top10_concentration"
    DEV_CONCENTRATION = "dev_concentration"
    CLUSTER_CONCENTRATION = "cluster_concentration"
    BUNDLER_EXPOSURE = "bundler_exposure"
    SNIPER_EXPOSURE = "sniper_exposure"
    INSIDER_EXPOSURE = "insider_exposure"
    WASH_TRADING = "wash_trading"
    RUG_HISTORY = "rug_history"
    LOW_LIQUIDITY = "low_liquidity"
    PROVIDER_CONFLICT = "provider_conflict"
    METADATA_MUTABLE = "metadata_mutable"
    DEV_SOLD = "dev_sold"
    UNKNOWN_SAFETY = "unknown_safety"


class Token(BaseModel):
    address: str
    chain: Chain
    symbol: str | None = None
    name: str | None = None
    decimals: int | None = None
    creator: str | None = None
    created_ms: int | None = None
    launchpad: str | None = None
    pool: str | None = None
    migrated_ms: int | None = None
    meta: dict[str, Any] = Field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.chain.value}:{self.address}"

    @property
    def age_s(self) -> float | None:
        if self.created_ms is None:
            return None
        return (now_ms() - self.created_ms) / 1000.0


class TokenDossier(BaseModel):
    """Merged DYOR view. Blockers reject, warnings downgrade."""

    address: str
    chain: Chain
    price_usd: Measure = Field(default_factory=Measure.unknown)
    liquidity_usd: Measure = Field(default_factory=Measure.unknown)
    market_cap_usd: Measure = Field(default_factory=Measure.unknown)
    volume_24h_usd: Measure = Field(default_factory=Measure.unknown)
    holder_count: Measure = Field(default_factory=Measure.unknown)
    top10_pct: Measure = Field(default_factory=Measure.unknown)
    dev_pct: Measure = Field(default_factory=Measure.unknown)
    insider_pct: Measure = Field(default_factory=Measure.unknown)
    bundler_pct: Measure = Field(default_factory=Measure.unknown)
    sniper_pct: Measure = Field(default_factory=Measure.unknown)
    cluster_pct: Measure = Field(default_factory=Measure.unknown)
    buy_tax_bps: Measure = Field(default_factory=Measure.unknown)
    sell_tax_bps: Measure = Field(default_factory=Measure.unknown)
    rug_ratio: Measure = Field(default_factory=Measure.unknown)
    mint_authority_revoked: bool | None = None
    freeze_authority_revoked: bool | None = None
    lp_burned_pct: Measure = Field(default_factory=Measure.unknown)
    can_sell: bool | None = None
    blockers: list[TokenRisk] = Field(default_factory=list)
    warnings: list[TokenRisk] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    graded_wallets: list[str] = Field(default_factory=list)
    entity_count: int = 0
    score: float | None = None
    grade: Grade = Grade.UNSCORED
    receipts: list[Receipt] = Field(default_factory=list)
    built_at_ms: int = Field(default_factory=now_ms)

    @property
    def tradeable(self) -> bool:
        return not self.blockers


# --------------------------------------------------------------------------------------
# events, signals, decisions, trades
# --------------------------------------------------------------------------------------


class EventKind(StrEnum):
    """Everything that lands on the bus. The dashboard, reflection job and traces read these."""

    TOKEN_CREATED = "token.created"
    TOKEN_MIGRATED = "token.migrated"
    TOKEN_SCANNED = "token.scanned"
    WALLET_TRADE = "wallet.trade"
    WALLET_GRADED = "wallet.graded"
    ENTITY_UPDATED = "entity.updated"
    ALPHA_SIGNAL = "alpha.signal"
    ALPHA_BOOST = "alpha.boost"
    ALPHA_CTO = "alpha.cto"
    ALPHA_META = "alpha.meta"
    ALPHA_CALL = "alpha.call"
    ALPHA_NEWS = "alpha.news"
    ALPHA_LISTING = "alpha.listing"
    SIGNAL_FIRED = "signal.fired"
    DECISION = "decision"
    ORDER_SUBMITTED = "order.submitted"
    ORDER_FILLED = "order.filled"
    ORDER_FAILED = "order.failed"
    POSITION_OPENED = "position.opened"
    POSITION_UPDATED = "position.updated"
    POSITION_CLOSED = "position.closed"
    PROTECTION_SET = "protection.set"
    PROTECTION_TRIGGERED = "protection.triggered"
    RISK_HALT = "risk.halt"
    HUNTER_FOUND = "hunter.found"
    AGENT_THOUGHT = "agent.thought"
    AGENT_TOOL = "agent.tool"
    REFLECTION = "reflection"
    PARAM_CHANGE = "param.change"
    PROVIDER_ERROR = "provider.error"
    PROVIDER_BUDGET = "provider.budget"
    SYSTEM = "system"
    # Written as raw strings by their modules since they landed; declared here 2026-09-22
    # because ``Event.kind`` is strict and every reader that builds an ``Event`` from a
    # row (``events.recent``/``tail`` without a kind filter) raised on them. MEASURED on
    # the live box: scan.tier1 7,585 rows, triage.verdict 970, triage.backpressure 557,
    # creators.backfill 35 -- all invisible to any ``Event``-typed reader.
    SCAN_TIER1 = "scan.tier1"
    SCAN_FAILED = "scan.failed"
    TRIAGE_VERDICT = "triage.verdict"
    TRIAGE_BACKPRESSURE = "triage.backpressure"
    CREATORS_BACKFILL = "creators.backfill"


class Event(BaseModel):
    id: int | None = None
    ts_ms: int = Field(default_factory=now_ms)
    kind: EventKind
    level: Literal["debug", "info", "warn", "error"] = "info"
    chain: Chain | None = None
    subject: str | None = None  # token or wallet address
    payload: dict[str, Any] = Field(default_factory=dict)
    trace_id: str | None = None
    dedupe_key: str | None = None


class Lane(StrEnum):
    """Entry strategies. Each runs in shadow before it gets capital."""

    CONFLUENCE_5 = "confluence-5"
    TRUSTED_COPY = "trusted-copy"
    CURVE_VELOCITY = "curve-velocity"
    SM_TRENCHES = "sm-trenches"
    MIGRATION_FADE = "migration-fade"
    KOL_FADE = "kol-fade"
    LISTING_POP = "listing-pop"
    PONS_ROBINHOOD = "pons-robinhood"
    #: Launch sniping on robinhood (Pons) and sol (pump.fun / LaunchLab): kaiba/execution/snipe.py
    #: records signals; the engine decides them under every gate. Owner, 2026-10-04: live small.
    LAUNCH_SNIPE = "launch-snipe"
    MANUAL = "manual"


class LaneMode(StrEnum):
    OFF = "off"
    SHADOW = "shadow"
    CANARY = "canary"
    LIVE = "live"


class Signal(BaseModel):
    signal_id: str
    lane: Lane
    chain: Chain
    token: str
    #: Lane conviction, **0.0 to 1.0**. Undeclared until 2026-09-21, which is how it came
    #: to be fed straight into ``risk.SCORE_LADDER`` -- a 0-100 scale whose lowest rung is
    #: 70. Every signal therefore scored below every rung and sized to zero: 986 signals,
    #: 0 that ever reached 80, 361 `size_not_positive` refusals and 11.6 hours with no
    #: order at all. Convert with ``risk.score_from_strength`` at the boundary; never pass
    #: this value to anything expecting a score.
    strength: float = 0.0
    reasons: list[str] = Field(default_factory=list)
    wallets: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    window_s: int | None = None
    created_ms: int = Field(default_factory=now_ms)
    payload: dict[str, Any] = Field(default_factory=dict)


class Action(StrEnum):
    ENTER = "enter"
    EXIT = "exit"
    SCALE_IN = "scale_in"
    SCALE_OUT = "scale_out"
    HOLD = "hold"
    SKIP = "skip"


class Decision(BaseModel):
    """One decision, including the ones where we stand aside — those are the training data."""

    decision_id: str
    ts_ms: int = Field(default_factory=now_ms)
    lane: Lane
    mode: LaneMode
    chain: Chain
    token: str
    action: Action
    thesis: str = ""
    confidence: float = 0.5
    signals: list[str] = Field(default_factory=list)
    dossier_grade: Grade = Grade.UNSCORED
    size_base_units: int | None = None
    size_pct_bankroll: float | None = None
    expected_return_pct: float | None = None
    invalidation: str | None = None
    regime: str | None = None
    blockers: list[str] = Field(default_factory=list)
    params_version: str = "v1"
    model: str | None = None
    trace_id: str | None = None


class OrderState(StrEnum):
    PLANNED = "planned"
    RESERVED = "reserved"
    SUBMITTING = "submitting"
    SUBMITTED = "submitted"
    PARTIAL = "partial"
    FILLED = "filled"
    FAILED = "failed"
    EXPIRED = "expired"
    UNKNOWN = "unknown"  # ambiguous send: never blindly resubmit
    CANCELLED = "cancelled"


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class Order(BaseModel):
    order_id: str
    decision_id: str | None = None
    chain: Chain
    token: str
    side: Side
    lane: Lane
    mode: LaneMode
    input_token: str
    output_token: str
    amount_in: int  # base units
    min_out: int
    slippage_bps: int
    state: OrderState = OrderState.PLANNED
    provider: str = "gmgn"
    provider_order_id: str | None = None
    tx_hash: str | None = None
    filled_out: int | None = None
    fee_native: int | None = None
    created_ms: int = Field(default_factory=now_ms)
    updated_ms: int = Field(default_factory=now_ms)
    error: str | None = None


class Position(BaseModel):
    position_id: str
    chain: Chain
    token: str
    lane: Lane
    mode: LaneMode
    opened_ms: int = Field(default_factory=now_ms)
    closed_ms: int | None = None
    qty: int = 0  # token atoms still held
    qty_total: int = 0
    cost_native: int = 0  # base units spent, fee inclusive
    proceeds_native: int = 0
    realized_native: int = 0
    entry_price_usd: Decimal | None = None
    peak_price_usd: Decimal | None = None
    stop_price_usd: Decimal | None = None
    tp_done: list[str] = Field(default_factory=list)
    protected: bool = False
    protection_ids: list[str] = Field(default_factory=list)
    mae_pct: float | None = None
    mfe_pct: float | None = None
    exit_reason: str | None = None

    @property
    def open(self) -> bool:
        return self.closed_ms is None and self.qty > 0


class TradeOutcome(BaseModel):
    """Closed round trip, written to the journal for the learning loop."""

    trade_id: str
    position_id: str
    decision_id: str | None = None
    lane: Lane
    mode: LaneMode
    chain: Chain
    token: str
    opened_ms: int
    closed_ms: int
    hold_s: int
    cost_native: int
    proceeds_native: int
    pnl_native: int
    pnl_pct: float
    fees_native: int = 0
    slippage_bps: int | None = None
    mae_pct: float | None = None
    mfe_pct: float | None = None
    exit_reason: str | None = None
    mistakes: list[str] = Field(default_factory=list)
    lesson: str | None = None
    params_version: str = "v1"


#: Fixed mistake vocabulary — the reflection job may only use these tags.
MISTAKE_TAGS: frozenset[str] = frozenset(
    {
        "late_entry",
        "early_entry",
        "ignored_invalidation",
        "size_too_big",
        "size_too_small",
        "thesis_wrong",
        "execution_slippage",
        "regime_misread",
        "data_error",
        "held_through_migration",
        "no_protection",
        "chased_kol",
        "cluster_missed",
    }
)
