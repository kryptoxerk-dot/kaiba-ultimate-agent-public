"""NFT mint hunter - opportunistic, and honest about it.

Research 06 is blunt: 2025 NFT volume was ~$5.5B, down 37% YoY and ~95% below peak; about
**96% of collections have zero activity**; the desirable mints are allowlist or cosigned,
and the public phases a bot *can* snipe are mostly the ones nobody wanted. This is not a
revenue line. It is a watcher that stays quiet until a new-chain launch wave throws up a
public phase with a real floor behind it, and the code is written so that "stay quiet" is
the default rather than a discipline someone has to remember.

Three rules are enforced here rather than advised:

1. **A mint behind a Candy Guard ``thirdPartySigner`` or ``gatekeeper`` is refused.**
   Headless minting is not merely risky there, it is impossible: the transaction needs a
   signature we do not have. Planning one is a guaranteed wasted fee.
2. **Guards we have not actually read are treated as blocking.** "We did not check" is not
   "there is nothing there".
3. **Nothing is submitted before the on-chain start slot.** Candy Guard's Bot Tax charges
   SOL for *failed* guard checks, so spamming a machine before it opens is a way to pay a
   project to reject you. The start slot is carried on the plan step as ``start_slot`` and
   the executor must wait for it; when we cannot determine one, we refuse to plan at all.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterable
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from kaiba.core.config import get_settings
from kaiba.core.db import fetch_all, fetch_one, get_conn, jdump
from kaiba.core.events import emit
from kaiba.core.limiter import Priority, guarded
from kaiba.core.schemas import Chain, EventKind, now_ms
from kaiba.hunters.airdrops import (
    ParticipationPlan,
    PlanAction,
    fetch_json,
    make_step,
    parse_iso_ms,
    provider_error,
    record_source,
    store_opportunity,
)
from kaiba.hunters.ev import (
    CENT,
    EvScore,
    HunterConfig,
    OpportunityEvidence,
    OpportunityKind,
    SybilRisk,
    hunter_config,
)

log = logging.getLogger(__name__)

MAGIC_EDEN_LAUNCHPAD_URL = "https://api-mainnet.magiceden.dev/v2/launchpad/collections"
HELIUS_WEBHOOK_API = "https://api.helius.xyz/v0/webhooks"

#: Metaplex programs worth watching (research 06). Core is the current standard.
CORE_CANDY_MACHINE_PROGRAM = "CMACYFENjoBMHzapRXyo1JZkVS6EtaDDzkjMrmQLvr4J"
CANDY_GUARD_PROGRAM = "CMAGAKJ67e9hRZgfC5SFTbZH8MgEmtqazKXjmkaJjWTJ"
WATCHED_MINT_EVENT_TYPES = ("NFT_MINT", "CANDY_MACHINE_UPDATE")

#: What a floor is really worth to us: marketplace fee + creator royalty + the fact that a
#: floor ask is not a bid, on a market where most collections never trade again.
SECONDARY_HAIRCUT = Decimal("0.60")
#: Probability we get out at all, by what we know about the creator.
P_EXIT_PROVEN_CREATOR = Decimal("0.60")
P_EXIT_UNKNOWN_CREATOR = Decimal("0.30")
P_EXIT_FLAGGED_CREATOR = Decimal("0.05")
#: Above this supply a public mint is a distribution event, not a scarcity event.
SUPPLY_DILUTION_WARN = 10_000


class MintGuard(StrEnum):
    """Candy Guard guards we care about by name (Metaplex naming, camelCase preserved)."""

    THIRD_PARTY_SIGNER = "thirdPartySigner"
    GATEKEEPER = "gatekeeper"
    ALLOW_LIST = "allowList"
    ADDRESS_GATE = "addressGate"
    TOKEN_GATE = "tokenGate"
    NFT_GATE = "nftGate"
    MINT_LIMIT = "mintLimit"
    PROGRAM_GATE = "programGate"
    START_DATE = "startDate"
    END_DATE = "endDate"
    BOT_TAX = "botTax"
    SOL_PAYMENT = "solPayment"
    FREEZE_SOL_PAYMENT = "freezeSolPayment"
    REDEEMED_AMOUNT = "redeemedAmount"


#: Guards that make a headless mint impossible - not hard, impossible.
BLOCKING_GUARDS: frozenset[MintGuard] = frozenset(
    {MintGuard.THIRD_PARTY_SIGNER, MintGuard.GATEKEEPER}
)
#: Guards that mean "you are not on the list", which is a refusal for us too.
ALLOWLIST_GUARDS: frozenset[MintGuard] = frozenset(
    {MintGuard.ALLOW_LIST, MintGuard.ADDRESS_GATE, MintGuard.TOKEN_GATE, MintGuard.NFT_GATE}
)


class CreatorHistory(BaseModel):
    """What the creator has done before. Absent history is not a clean record."""

    address: str | None = None
    prior_collections: int | None = None
    prior_floor_usd: Decimal | None = None
    rugged: bool = False
    flagged: bool = False

    @property
    def proven(self) -> bool:
        return bool(self.prior_collections) and not (self.rugged or self.flagged)


class MintEvidence(BaseModel):
    """One upcoming or newly-detected mint.

    ``guards_verified`` is the field that keeps us honest: until something has actually
    read the Candy Guard account, the guard list is a guess and is treated as blocking.
    """

    name: str
    symbol: str | None = None
    chain: Chain | None = Chain.SOL
    chain_hint: str | None = None
    candy_machine: str | None = None
    collection: str | None = None
    mint_price_native: Decimal | None = None
    mint_price_usd: Decimal | None = None
    supply: int | None = None
    launch_ms: int | None = None
    start_slot: int | None = None
    guards: list[MintGuard] = Field(default_factory=list)
    guards_verified: bool = False
    public_phase: bool | None = None
    recent_floor_usd: Decimal | None = None
    creator: CreatorHistory = Field(default_factory=CreatorHistory)
    gas_cost_usd: Decimal = Decimal("0.50")
    time_cost_hours: float = 0.25
    source: str = "unknown"
    url: str | None = None
    observed_mints: int = 0
    meta: dict[str, Any] = Field(default_factory=dict)

    def to_evidence(self) -> OpportunityEvidence:
        """Shared shape so mints rank in the same table as everything else."""
        return OpportunityEvidence(
            kind=OpportunityKind.NFT_MINT,
            name=self.name,
            symbol=self.symbol,
            chain=self.chain,
            chain_hint=self.chain_hint,
            confirmed_token=False,
            points_program=False,
            expected_value_usd=self.recent_floor_usd,
            capital_required_usd=self.mint_price_usd or Decimal("0"),
            capital_lockup_days=0,
            time_cost_hours=self.time_cost_hours,
            gas_cost_usd=self.gas_cost_usd,
            deadline_ms=self.launch_ms,
            sybil_risk=SybilRisk.MEDIUM,
            wallet_count=1,
            sources=[self.source],
            url=self.url,
            meta={
                "candy_machine": self.candy_machine,
                "start_slot": self.start_slot,
                "guards": [g.value for g in self.guards],
                "guards_verified": self.guards_verified,
                "supply": self.supply,
                "mint_price_native": str(self.mint_price_native) if self.mint_price_native else None,
                "observed_mints": self.observed_mints,
                **self.meta,
            },
        )


# ------------------------------------------------------------------------- magic eden


def magic_eden_launchpad(
    raw: Any | None = None,
    sol_price_usd: Decimal | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[MintEvidence]:
    """Keyless ``GET /v2/launchpad/collections`` (2 QPS).

    The list is curated and looked stale when the research was done (newest entry Feb
    2026), so it is a low-yield source that costs nothing to keep polling. Prices are in
    the chain's native unit; without a SOL/USD price we leave ``mint_price_usd`` unknown
    rather than inventing one, and :func:`assess_mint` then refuses to price the mint.
    """
    source = "magiceden"
    data = (
        raw
        if raw is not None
        else fetch_json(source, "launchpad.collections", MAGIC_EDEN_LAUNCHPAD_URL,
                        priority=Priority.DISCOVERY, conn=conn)
    )
    if data is None:
        return []
    if not isinstance(data, list):
        provider_error(source, "launchpad.collections", f"expected list, got {type(data).__name__}", conn=conn)
        return []
    out: list[MintEvidence] = []
    try:
        for row in data:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or row.get("symbol") or "").strip()
            if not name:
                continue
            price_native = _dec(row.get("price"))
            chain_raw = str(row.get("chain") or "solana").lower()
            chain = Chain.SOL if chain_raw in {"solana", "sol"} else _evm_chain(chain_raw)
            launch_ms = parse_iso_ms(row.get("launchDatetime") or row.get("launchDate"))
            out.append(
                MintEvidence(
                    name=name,
                    symbol=(row.get("symbol") or None),
                    chain=chain,
                    chain_hint=chain_raw,
                    collection=row.get("symbol"),
                    mint_price_native=price_native,
                    mint_price_usd=(
                        (price_native * sol_price_usd).quantize(CENT)
                        if price_native is not None and sol_price_usd is not None and chain is Chain.SOL
                        else None
                    ),
                    supply=_int(row.get("size")),
                    launch_ms=launch_ms,
                    guards_verified=False,  # the launchpad list says nothing about guards
                    public_phase=None,
                    source=source,
                    url=f"https://magiceden.io/launchpad/{row.get('symbol')}" if row.get("symbol") else None,
                    meta={"featured": bool(row.get("featured")), "stage": row.get("launchStage")},
                )
            )
        return out
    except Exception as exc:  # noqa: BLE001 - a layout change is not a crash
        provider_error(source, "launchpad.collections", f"parse failed: {exc}", conn=conn)
        return []


def _evm_chain(name: str) -> Chain | None:
    return {"ethereum": Chain.ETH, "eth": Chain.ETH, "base": Chain.BASE, "bsc": Chain.BSC}.get(name)


def _dec(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (ValueError, ArithmeticError):
        return None


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------------------- helius


def register_helius_webhook(
    webhook_url: str,
    conn: sqlite3.Connection | None = None,
    addresses: Iterable[str] = (CORE_CANDY_MACHINE_PROGRAM, CANDY_GUARD_PROGRAM),
) -> str | None:
    """Register the free-plan webhook for NFT_MINT / CANDY_MACHINE_UPDATE.

    Returns the webhook id, or ``None`` when there is no key or Helius says no. Helius has
    no polling endpoint for these events, so the receiver writes deliveries into
    ``hunter_webhook_events`` and :func:`helius_mint_watch` drains that table.
    """
    import httpx

    key = get_settings().helius_api_key
    if not key:
        log.info("helius: no api key; mint watch stays offline")
        return None
    body = {
        "webhookURL": webhook_url,
        "transactionTypes": list(WATCHED_MINT_EVENT_TYPES),
        "accountAddresses": list(addresses),
        "webhookType": "enhanced",
    }
    try:
        with guarded("helius", "webhook.create", Priority.RESEARCH, conn=conn):
            resp = httpx.post(f"{HELIUS_WEBHOOK_API}?api-key={key}", json=body, timeout=20.0)
            resp.raise_for_status()
            payload = resp.json()
        return str(payload.get("webhookID") or payload.get("webhookId") or "") or None
    except Exception as exc:  # noqa: BLE001
        provider_error("helius", "webhook.create", f"{type(exc).__name__}: {exc}", conn=conn)
        return None


def record_helius_payload(conn: sqlite3.Connection, payload: dict[str, Any]) -> int | None:
    """Door for the webhook receiver: store the raw delivery, parse later.

    Keeping the body means a parser bug is replayable instead of a lost mint.
    """
    event_type = str(payload.get("type") or "UNKNOWN")
    signature = payload.get("signature") or payload.get("txSignature")
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO hunter_webhook_events "
            "(provider, event_type, signature, received_ms, payload_json) VALUES (?,?,?,?,?)",
            ("helius", event_type, signature, now_ms(), jdump(payload)),
        )
        if cur.rowcount:
            return int(cur.lastrowid)
        # A re-delivery: return the existing row so the drain can still mark it processed.
        existing = fetch_one(
            conn,
            "SELECT id FROM hunter_webhook_events WHERE provider='helius' AND event_type=? "
            "AND signature IS ?",
            (event_type, signature),
        )
        return int(existing["id"]) if existing else None
    except sqlite3.Error as exc:
        log.warning("helius payload store failed: %s", exc)
        return None


def _candy_machine_of(payload: dict[str, Any]) -> str | None:
    for ins in payload.get("instructions") or []:
        if not isinstance(ins, dict):
            continue
        if ins.get("programId") in {CORE_CANDY_MACHINE_PROGRAM, CANDY_GUARD_PROGRAM}:
            accounts = [a for a in (ins.get("accounts") or []) if isinstance(a, str)]
            if accounts:
                return accounts[0]
    for acc in payload.get("accountData") or []:
        if isinstance(acc, dict) and acc.get("account"):
            return str(acc["account"])
    return None


def parse_helius_payload(payload: dict[str, Any]) -> MintEvidence | None:
    """One enhanced-webhook delivery to one mint observation. Unknown shapes return None."""
    if not isinstance(payload, dict):
        return None
    event_type = str(payload.get("type") or "")
    if event_type not in WATCHED_MINT_EVENT_TYPES:
        return None
    machine = _candy_machine_of(payload)
    nft_event = ((payload.get("events") or {}).get("nft") or {}) if isinstance(payload.get("events"), dict) else {}
    nfts = nft_event.get("nfts") or []
    first = nfts[0] if nfts and isinstance(nfts[0], dict) else {}
    if event_type == "CANDY_MACHINE_UPDATE":
        # A config change names nothing; the machine address is the only honest label.
        name = f"candy machine {machine[:8]}" if machine else "candy machine (unidentified)"
    else:
        name = (
            first.get("name")
            or nft_event.get("collectionName")
            or (str(payload.get("description") or "").split(" for ")[0][:80])
            or machine
            or "unknown candy machine"
        )
    lamports = nft_event.get("amount")
    price_native = (Decimal(str(lamports)) / Decimal(1_000_000_000)) if isinstance(lamports, int) else None
    slot = _int(payload.get("slot"))
    ts = payload.get("timestamp")
    return MintEvidence(
        name=str(name).strip() or "unknown candy machine",
        symbol=first.get("symbol"),
        chain=Chain.SOL,
        candy_machine=machine,
        collection=first.get("mint"),
        mint_price_native=price_native,
        mint_price_usd=None,  # no USD without a price feed; unknown stays unknown
        supply=None,
        launch_ms=int(ts * 1000) if isinstance(ts, int | float) else None,
        # Someone else's mint landed in this slot, so the machine is open as of here. That
        # is the earliest slot we know is safe from the Bot Tax.
        start_slot=slot,
        guards=[],
        guards_verified=False,
        public_phase=None,
        source="helius",
        url=f"https://solscan.io/account/{machine}" if machine else None,
        observed_mints=1,
        meta={"event_type": event_type, "signature": payload.get("signature")},
    )


def helius_mint_watch(
    conn: sqlite3.Connection | None = None,
    payloads: list[dict[str, Any]] | None = None,
    limit: int = 500,
) -> list[MintEvidence]:
    """Drain stored webhook deliveries (or the ones handed in) into mint evidence.

    Deliveries for the same candy machine collapse into one observation with a mint count,
    because fifty mints in one machine is one opportunity, not fifty.
    """
    c = conn or get_conn()
    rows: list[tuple[int | None, dict[str, Any]]] = []
    if payloads is not None:
        for p in payloads:
            rows.append((record_helius_payload(c, p), p))
    else:
        for row in fetch_all(
            c,
            "SELECT id, payload_json FROM hunter_webhook_events "
            "WHERE provider='helius' AND processed_ms IS NULL ORDER BY id LIMIT ?",
            (limit,),
        ):
            try:
                rows.append((int(row["id"]), json.loads(row["payload_json"])))
            except ValueError:
                rows.append((int(row["id"]), {}))

    by_machine: dict[str, MintEvidence] = {}
    processed: list[int] = []
    for row_id, payload in rows:
        if row_id is not None:
            processed.append(row_id)
        try:
            ev = parse_helius_payload(payload)
        except Exception as exc:  # noqa: BLE001 - one bad delivery must not stop the drain
            provider_error("helius", "webhook.parse", f"{type(exc).__name__}: {exc}", conn=c)
            continue
        if ev is None:
            continue
        key = ev.candy_machine or ev.name
        prior = by_machine.get(key)
        if prior is None:
            by_machine[key] = ev
        else:
            prior.observed_mints += 1
            if ev.start_slot is not None:
                prior.start_slot = min(prior.start_slot or ev.start_slot, ev.start_slot)
            if prior.mint_price_native is None:
                prior.mint_price_native = ev.mint_price_native
    if processed:
        ts = now_ms()
        c.executemany(
            "UPDATE hunter_webhook_events SET processed_ms=? WHERE id=?",
            [(ts, i) for i in processed],
        )
    record_source(c, "helius_mint_watch", "nft_mint", len(by_machine))
    return list(by_machine.values())


# ---------------------------------------------------------------------------- assessment


def assess_mint(evidence: MintEvidence, cfg: HunterConfig | None = None) -> EvScore:
    """Gate first, arithmetic second. Most mints never reach the arithmetic.

    Gates, in order: a blocking Candy Guard; guards we have not read; an allowlist-only
    phase; a missing start slot (Bot Tax); a missing USD mint price; a missing floor
    comparable. Any of them is a refusal, because each one means we either cannot mint or
    cannot say what minting is worth.
    """
    cfg = cfg or hunter_config()
    rationale: list[str] = []
    warnings: list[str] = []
    blockers: list[str] = []

    blocking = [g for g in evidence.guards if g in BLOCKING_GUARDS]
    if blocking:
        blockers.append(
            f"candy guard {', '.join(g.value for g in blocking)}: the mint transaction needs a "
            "signature we do not have - headless minting is impossible, not merely hard"
        )
    elif not evidence.guards_verified:
        blockers.append(
            "candy guard not read: thirdPartySigner/gatekeeper cannot be ruled out, and "
            '"we did not check" is not "there is nothing there"'
        )

    allowlisted = [g for g in evidence.guards if g in ALLOWLIST_GUARDS]
    if allowlisted and evidence.public_phase is not True:
        blockers.append(
            f"{', '.join(g.value for g in allowlisted)} with no public phase: we are not on the list"
        )
    if evidence.public_phase is False:
        blockers.append("no public phase")

    if evidence.start_slot is None:
        blockers.append(
            "no on-chain start slot: Candy Guard's Bot Tax charges SOL for failed guard "
            "checks, so we will not send anything we cannot time"
        )
    else:
        rationale.append(f"start slot {evidence.start_slot}: executor waits for it, never before")

    price = evidence.mint_price_usd
    if price is None:
        blockers.append("mint price not priced in USD: unknown stays unknown")
        price = Decimal("0")
    else:
        rationale.append(f"mint price ${price}")

    floor = evidence.recent_floor_usd or evidence.creator.prior_floor_usd
    if floor is None:
        blockers.append(
            "no floor comparable: ~96% of collections end with zero activity, so an "
            "unpriceable mint is a donation"
        )
        floor = Decimal("0")
    else:
        which = "recent floor" if evidence.recent_floor_usd else "creator's prior collection floor"
        rationale.append(f"{which} ${floor}")

    if evidence.creator.rugged or evidence.creator.flagged:
        p_exit = P_EXIT_FLAGGED_CREATOR
        why = "creator rugged or flagged"
        warnings.append("creator has a rug or a flag against them")
    elif evidence.creator.proven:
        p_exit = P_EXIT_PROVEN_CREATOR
        why = f"creator shipped {evidence.creator.prior_collections} prior collection(s)"
    else:
        p_exit = P_EXIT_UNKNOWN_CREATOR
        why = "unknown creator (the base case: most collections never trade again)"
    rationale.append(f"p(exit at a discounted floor) = {p_exit} ({why})")

    resale = (floor * SECONDARY_HAIRCUT).quantize(CENT)
    rationale.append(
        f"realisable resale = ${floor} x {SECONDARY_HAIRCUT} = ${resale} "
        "(marketplace fee + royalty, and a floor ask is not a bid)"
    )

    gross = (p_exit * resale).quantize(CENT)
    time_cost = (Decimal(str(evidence.time_cost_hours)) * cfg.operator_hourly_usd).quantize(CENT)
    cost = (price + evidence.gas_cost_usd + time_cost).quantize(CENT)
    rationale.append(f"gross = {p_exit} x ${resale} = ${gross}")
    rationale.append(
        f"cost = mint ${price} + gas ${evidence.gas_cost_usd} + time ${time_cost} "
        f"({evidence.time_cost_hours}h x ${cfg.operator_hourly_usd}/h) = ${cost}"
    )
    ev = (gross - cost).quantize(CENT)
    rationale.append(f"EV = ${gross} - ${cost} = ${ev}")

    if price > 0 and resale <= price:
        warnings.append(
            f"mint price ${price} is at or above the discounted floor ${resale}: minting is a loss "
            "before it is a trade"
        )
    if evidence.supply and evidence.supply > SUPPLY_DILUTION_WARN:
        warnings.append(f"supply {evidence.supply}: a distribution event, not a scarcity event")
    if evidence.launch_ms and evidence.launch_ms < now_ms():
        warnings.append("launch time has passed; the public phase may already be exhausted")
    if ev <= 0:
        warnings.append(f"negative expected value (${ev})")

    confidence = 0.2
    if evidence.guards_verified:
        confidence += 0.25
    if evidence.recent_floor_usd is not None:
        confidence += 0.25
    if evidence.creator.prior_collections:
        confidence += 0.1
    if evidence.start_slot is not None:
        confidence += 0.1
    if evidence.observed_mints:
        confidence += 0.05

    refused = bool(blockers)
    if refused:
        warnings.append("refused: " + "; ".join(blockers))
    return EvScore(
        ev_usd=ev,
        confidence=round(min(0.95, max(0.05, confidence)), 2),
        rationale=rationale,
        warnings=warnings,
        gross_usd=gross,
        cost_usd=cost,
        p_token=float(p_exit),
        haircut=float(SECONDARY_HAIRCUT),
        refused=refused,
        blockers=blockers,
    )


def build_mint_plan(evidence: MintEvidence, cfg: HunterConfig | None = None) -> ParticipationPlan:
    """A mint plan, or a refusal with the reason on it. Steps are recorded, not executed."""
    cfg = cfg or hunter_config()
    score = assess_mint(evidence, cfg)
    ev = evidence.to_evidence()
    plan = ParticipationPlan(
        opportunity_id=ev.key,
        name=evidence.name,
        kind=OpportunityKind.NFT_MINT,
        wallet_count=1,
        total_capital_usd=evidence.mint_price_usd or Decimal("0"),
        warnings=list(score.warnings),
    )
    if score.refused:
        plan.refused = True
        plan.refusal_reason = "; ".join(score.blockers)
        return plan

    plan.steps = [
        make_step(
            1,
            PlanAction.WALLET_CONNECT,
            f"use the single mint wallet, funded with {evidence.mint_price_native or '?'} SOL + fees",
            chain=evidence.chain,
        ),
        make_step(
            2,
            PlanAction.MINT,
            f"mint 1 via Candy Guard at or after slot {evidence.start_slot}; never send earlier "
            "(Bot Tax charges for failed guard checks)",
            capital_usd=evidence.mint_price_usd or Decimal("0"),
            chain=evidence.chain,
            start_slot=evidence.start_slot,
            url=evidence.url,
        ),
        make_step(
            3,
            PlanAction.HOLD,
            "list at the pre-set sell rule immediately; secondary liquidity is thin and gets "
            "thinner by the hour",
            chain=evidence.chain,
        ),
    ]
    return plan


# ------------------------------------------------------------------------------- refresh


#: This hunter is switched off. It is not deleted, because the repository has no commit
#: history yet and a delete would be unrecoverable; set ``KAIBA_ENABLE_NFT_HUNTER=1`` or
#: pass ``force=True`` to run it anyway.
#:
#: The evidence, from docs/research/12-early-alpha-airdrops-nfts-2026.md, measured
#: 2026-09-20: a backtest of the entire Magic Eden launchpad found **8% of 75 paid Solana
#: mints trade at or above mint price**, and a mint-everything portfolio returned
#: **-85.4%**, $12,641 down to $1,846. The launchpad feed's newest entry is 2026-02-20 —
#: nothing for seven months, while the same API still serves live data for other routes.
#: A ten-minute on-chain census of Ethereum found 214 ERC-721 mints across 9 contracts,
#: of which **three** were one-off collectibles and the rest were ENS registrations and
#: Uniswap LP positions. This is not a revenue line for an automated agent.
NFT_HUNTER_DISABLED_REASON = (
    "NFT minting is not a revenue line: 8% of 75 paid Solana launchpad mints trade at or "
    "above mint, mint-everything returns -85.4%, and the launchpad feed has been silent "
    "since 2026-02-20. Set KAIBA_ENABLE_NFT_HUNTER=1 to override."
)


def enabled() -> bool:
    """Whether the NFT hunter is permitted to run. Off unless the operator says otherwise."""
    import os

    return os.environ.get("KAIBA_ENABLE_NFT_HUNTER", "").strip() in {"1", "true", "yes"}


def refresh(
    conn: sqlite3.Connection | None = None,
    mints: list[MintEvidence] | None = None,
    cfg: HunterConfig | None = None,
    *,
    force: bool = False,
) -> int:
    """Score and store everything the mint watchers found. Returns rows written.

    Returns 0 immediately unless the hunter is enabled. See
    :data:`NFT_HUNTER_DISABLED_REASON` for why it is off by default.
    """
    if not (force or enabled()):
        log.info("nft hunter is disabled: %s", NFT_HUNTER_DISABLED_REASON)
        return 0
    c = conn or get_conn()
    cfg = cfg or hunter_config()
    found = mints if mints is not None else [*magic_eden_launchpad(conn=c), *helius_mint_watch(c)]
    written = 0
    for mint in found:
        score = assess_mint(mint, cfg)
        evidence = mint.to_evidence()
        before = fetch_one(
            c, "SELECT ev_score FROM opportunities WHERE opportunity_id=?", (evidence.key,)
        )
        plan = build_mint_plan(mint, cfg)
        store_opportunity(c, evidence, score, plan=plan.model_dump(mode="json"))
        written += 1
        was_above = before is not None and before["ev_score"] is not None and (
            Decimal(str(before["ev_score"])) >= cfg.ev_threshold_usd
        )
        if not score.refused and score.ev_usd >= cfg.ev_threshold_usd and not was_above:
            emit(
                EventKind.HUNTER_FOUND,
                {
                    "opportunity_id": evidence.key,
                    "kind": "nft_mint",
                    "name": mint.name,
                    "ev_usd": str(score.ev_usd),
                    "start_slot": mint.start_slot,
                    "mint_price_usd": str(mint.mint_price_usd) if mint.mint_price_usd else None,
                    "warnings": score.warnings,
                },
                chain=mint.chain,
                subject=evidence.key,
                conn=c,
            )
    return written


def weekly_report(conn: sqlite3.Connection | None = None, limit: int = 15) -> str:
    """Deliberately boring. If this report is empty most weeks, it is working."""
    c = conn or get_conn()
    rows = fetch_all(
        c,
        "SELECT * FROM opportunities WHERE kind='nft_mint' ORDER BY ev_score DESC LIMIT ?",
        (limit,),
    )
    lines = [
        "# NFT mint hunter - weekly",
        "",
        "Opportunistic only, never a revenue line: 2025 volume ~$5.5B (-37% YoY, ~95% below "
        "peak) and ~96% of collections have zero activity. A quiet week here is the expected "
        "outcome, not a failure.",
        "",
        "| # | Mint | Chain | EV (USD) | Mint cost | Start slot | Conf | Status |",
        "|---|---|---|---|---|---|---|---|",
    ]
    if not rows:
        lines.append("| - | *nothing minted or watchlisted* | | | | | | |")
    refused = 0
    for n, row in enumerate(rows, 1):
        meta = json.loads(row.get("evidence_json") or "{}").get("meta") or {}
        is_refused = row.get("status") == "refused"
        refused += int(is_refused)
        lines.append(
            "| {n} | {name} | {chain} | {ev} | {cost} | {slot} | {conf} | {status} |".format(
                n=n,
                name=str(row["name"]).replace("|", "/"),
                chain=row.get("chain") or "-",
                ev=f"${row['ev_score']:.2f}" if row.get("ev_score") is not None else "-",
                cost=f"${row.get('cost_usd') or '0'}",
                slot=meta.get("start_slot") or "unknown",
                conf=f"{row.get('confidence') or 0:.2f}",
                status="REFUSED" if is_refused else "watch",
            )
        )
    if refused:
        lines += [
            "",
            f"{refused} of {len(rows)} were refused outright (gated Candy Guard, unverified "
            "guards, no start slot or no floor comparable). Those are the cheapest decisions "
            "the agent makes all week.",
        ]
    return "\n".join(lines)


__all__ = [
    "ALLOWLIST_GUARDS",
    "BLOCKING_GUARDS",
    "CreatorHistory",
    "MintEvidence",
    "MintGuard",
    "assess_mint",
    "build_mint_plan",
    "helius_mint_watch",
    "magic_eden_launchpad",
    "parse_helius_payload",
    "record_helius_payload",
    "refresh",
    "register_helius_webhook",
    "weekly_report",
]
