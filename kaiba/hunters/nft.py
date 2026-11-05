# Codex 2026-09-27: owner-requested repair integration; see docs/TASKS.md REPAIR-INTEGRATION-20260927.
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
import time
from collections.abc import Callable, Iterable
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

import base58
import httpx
from pydantic import BaseModel, Field

from kaiba.core.config import get_settings
from kaiba.core.db import fetch_all, fetch_one, get_conn, jdump, upsert
from kaiba.core.events import emit
from kaiba.core.limiter import Priority, guarded
from kaiba.core.schemas import Chain, EventKind, now_ms
from kaiba.hunters.airdrops import (
    USER_AGENT,
    ParticipationPlan,
    PlanAction,
    fetch_json,
    gas_estimate,
    make_step,
    parse_iso_ms,
    provider_error,
    record_source,
    refresh_summary,
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

#: OpenSea's public drops list and collection stats. Keyless as of 2026-10-02, verified
#: from the VPS: ``drops?type=upcoming|recently_minted|featured&limit=100`` and
#: ``collections/{slug}/stats`` answer 200 with no API key (a bare ``/drops`` with no type
#: answers 401 "Missing an API Key", so the type is always sent). Unlike the Magic Eden
#: launchpad list -- 200 entries, newest launch 2026-02-20, zero future launches when
#: re-measured on 2026-10-02 -- this one carries live public stages on Robinhood Chain,
#: where the agent holds ETH: 34 of 97 recently minted drops and 5 of 9 featured.
OPENSEA_DROPS_URL = "https://api.opensea.io/api/v2/drops?type={type}&limit=100"
OPENSEA_STATS_URL = "https://api.opensea.io/api/v2/collections/{slug}/stats"
OPENSEA_DROP_TYPES = ("featured", "upcoming", "recently_minted")
#: OpenSea chain slug -> Chain. Only chains a Kaiba wallet can hold funds on; every other
#: chain (ape_chain, shape, avalanche, ...) is skipped rather than stored as unroutable.
OPENSEA_CHAINS: dict[str, Chain] = {
    "robinhood": Chain.ROBINHOOD,
    "ethereum": Chain.ETH,
    "base": Chain.BASE,
    "bsc": Chain.BSC,
    "solana": Chain.SOL,
}
#: Stats are one extra call per drop. Bound the pass: ~27 open drops on these chains was
#: the measured population, at one call per ``OPENSEA_PACE_S``.
OPENSEA_MAX_STATS = 25
#: The limiter's default bucket for an unconfigured provider is 1 call/s with a 1 s
#: minimum interval and REFUSES (it does not wait), so the collector paces itself.
OPENSEA_PACE_S = 1.2
#: A native price older than this is not used to put a dollar figure on a mint.
NATIVE_PRICE_MAX_AGE_MS = 6 * 3_600_000
_ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
#: The default sweep's sources, in the order they run (see :func:`refresh`).
NFT_SOURCES = ("opensea", "helius_mint_watch")
_USD_UNITS = frozenset({"USD", "USDG", "USDC", "USDT", "DAI", "USDS", "PYUSD"})
_ETH_UNITS = frozenset({"ETH", "WETH"})

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
    """One catalogue entry or newly-detected mint; neither proves an open sale.

    ``guards_verified`` is the field that keeps us honest: until something has actually
    read the Candy Guard account, the guard list is a guess and is treated as blocking.
    """

    name: str
    symbol: str | None = None
    chain: Chain | None = None
    chain_hint: str | None = None
    contract_address: str | None = None
    candy_machine: str | None = None
    collection: str | None = None
    mint_price_native: Decimal | None = None
    mint_price_usd: Decimal | None = None
    supply: int | None = None
    launch_ms: int | None = None
    # A launch start never establishes a participation deadline.
    end_ms: int | None = None
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

    @property
    def floor_comparable_usd(self) -> Decimal | None:
        # An observed zero floor must not be replaced by a better historical comparable.
        return self.recent_floor_usd if self.recent_floor_usd is not None else self.creator.prior_floor_usd

    @property
    def valuation_complete(self) -> bool:
        return all(_dec(value) is not None for value in (
            self.mint_price_native, self.mint_price_usd, self.floor_comparable_usd,
            self.gas_cost_usd, self.time_cost_hours,
        ))

    def to_evidence(self) -> OpportunityEvidence:
        """Shared shape; nullable NFT values live in meta, not the legacy capital default.

        The shared model requires numeric capital. Its zero for an unpriced mint is not
        a price estimate: valuation_complete=False forbids funding and numeric ranking.
        """
        return OpportunityEvidence(
            kind=OpportunityKind.NFT_MINT,
            name=self.name,
            symbol=self.symbol,
            chain=self.chain,
            chain_hint=self.chain_hint,
            confirmed_token=False,
            points_program=False,
            expected_value_usd=self.floor_comparable_usd,
            capital_required_usd=self.mint_price_usd or Decimal("0"),
            capital_lockup_days=0,
            time_cost_hours=self.time_cost_hours,
            gas_cost_usd=self.gas_cost_usd,
            deadline_ms=self.end_ms,
            sybil_risk=SybilRisk.MEDIUM,
            wallet_count=1,
            sources=[self.source],
            url=self.url,
            meta={
                **self.meta,
                "contract_address": self.contract_address,
                "launch_ms": self.launch_ms,
                "end_ms": self.end_ms,
                "candy_machine": self.candy_machine,
                "start_slot": self.start_slot,
                "guards": [g.value for g in self.guards],
                "guards_verified": self.guards_verified,
                "public_phase": self.public_phase,
                "supply": self.supply,
                "mint_price_native": str(self.mint_price_native) if self.mint_price_native is not None else None,
                "mint_price_usd": str(self.mint_price_usd) if self.mint_price_usd is not None else None,
                "recent_floor_usd": str(self.recent_floor_usd) if self.recent_floor_usd is not None else None,
                "floor_comparable_usd": str(self.floor_comparable_usd) if self.floor_comparable_usd is not None else None,
                "valuation_complete": self.valuation_complete,
                "observed_mints": self.observed_mints,
            },
        )


# ------------------------------------------------------------------------- magic eden


def _source_receipt(
    conn: sqlite3.Connection | None, name: str, count: int, error: str | None = None,
    *, no_deliveries: bool = False,
) -> None:
    """Keep no-call/empty distinct without changing the shared health API or counters."""
    state = "no_deliveries" if no_deliveries else "error" if error else "ok" if count else "empty"
    log.info("NFT source %s: %s (%s records)%s", name, state, count,
             "; webhook registration/receiver coverage unverified" if no_deliveries else "")
    if conn is None:
        return
    if error is not None or count:
        record_source(conn, name, "nft_mint", count, error=error)
        return
    ts = now_ms()
    row = {"name": name, "kind": "nft_mint", "last_run_ms": ts, "last_count": 0}
    if not no_deliveries:
        row.update(last_ok_ms=ts, fail_streak=0, last_error=None)
    # No delivery is not a successful remote poll, and cannot clear a past failure.
    upsert(conn, "hunter_sources", row, conflict=["name"], update=[k for k in row if k != "name"])


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
        _source_receipt(conn, source, 0, error="fetch unavailable; see provider error receipt")
        return []
    if not isinstance(data, list):
        detail = f"expected list, got {type(data).__name__}"
        provider_error(source, "launchpad.collections", detail, conn=conn)
        _source_receipt(conn, source, 0, error=detail)
        return []
    out: list[MintEvidence] = []
    sol_fx = _dec(sol_price_usd)
    try:
        for row in data:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or row.get("symbol") or "").strip()
            if not name:
                continue
            price_native = _dec(row.get("price"))
            chain_id = row.get("chainId")
            chain_raw = str(chain_id if chain_id is not None else row.get("chain") or "")
            chain = _launchpad_chain(chain_raw)
            legacy_chain = row.get("chain")
            if chain_id is not None and legacy_chain and str(legacy_chain) != chain_raw:
                if chain is None or chain is not _launchpad_chain(str(legacy_chain)):
                    chain = None  # contradictory source identities cannot justify SOL conversion
            launch_ms = parse_iso_ms(row.get("launchDatetime") or row.get("launchDate"))
            out.append(
                MintEvidence(
                    name=name,
                    symbol=(row.get("symbol") or None),
                    chain=chain,
                    chain_hint=chain_raw or None,
                    contract_address=row.get("contractAddress"),
                    collection=row.get("symbol"),
                    mint_price_native=price_native,
                    mint_price_usd=(
                        (price_native * sol_fx).quantize(CENT)
                        if price_native is not None and sol_fx is not None and sol_fx > 0 and chain is Chain.SOL
                        else None
                    ),
                    supply=_int(row.get("size")),
                    launch_ms=launch_ms,
                    guards_verified=False,  # the launchpad list says nothing about guards
                    public_phase=None,
                    source=source,
                    url=f"https://magiceden.io/launchpad/{row.get('symbol')}" if row.get("symbol") else None,
                    meta={
                        "featured": bool(row.get("featured")), "stage": row.get("launchStage"),
                        "source_chain_id": chain_id, "source_chain": legacy_chain,
                    },
                )
            )
        if data and not out:
            raise ValueError("nonempty launchpad payload has no identifiable mint records")
        _source_receipt(conn, source, len(out))
        return out
    except Exception as exc:  # noqa: BLE001 - a layout change is not a crash
        provider_error(source, "launchpad.collections", f"parse failed: {exc}", conn=conn)
        _source_receipt(conn, source, 0, error=f"parse failed: {exc}")
        return []


def _launchpad_chain(name: str) -> Chain | None:
    return {
        "solana": Chain.SOL, "sol": Chain.SOL,
        "ethereum": Chain.ETH, "eth": Chain.ETH, "1": Chain.ETH,
        "base": Chain.BASE, "8453": Chain.BASE,
        "bsc": Chain.BSC, "56": Chain.BSC,
    }.get(name)


def _dec(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
        return parsed if parsed.is_finite() and parsed >= 0 else None
    except (ValueError, ArithmeticError):
        return None


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------------------- opensea


def _eth_usd(conn: sqlite3.Connection | None) -> Decimal | None:
    """Latest stored ETH/USD sample if fresh. Robinhood Chain's native asset IS ETH."""
    if conn is None:
        return None
    try:
        from kaiba.providers.native_price import latest

        sample = latest(Chain.ETH, conn)
    except Exception:  # noqa: BLE001 - an unpriced mint stays unpriced, it does not crash
        return None
    if sample is None or now_ms() - int(sample.ts_ms) > NATIVE_PRICE_MAX_AGE_MS:
        return None
    return _dec(sample.price_usd)


def _wei_to_native(value: Any) -> Decimal | None:
    """An integer wei string to whole native units. Anything else is unknown, not zero."""
    if value is None or isinstance(value, bool):
        return None
    try:
        wei = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not wei.is_finite() or wei < 0 or wei != wei.to_integral_value():
        return None
    return wei / (Decimal(10) ** 18)


def _drop_stage(drop: dict[str, Any], at_ms: int) -> tuple[dict[str, Any] | None, str]:
    """The stage a buyer could still use, and whether it is open now or upcoming.

    An ended stage is skipped, so a drop whose active stage closed but whose next stage
    is still ahead reports the next one. No usable stage is ``(None, "closed")``.
    """
    for key in ("active_stage", "next_stage"):
        stage = drop.get(key)
        if not isinstance(stage, dict):
            continue
        end = parse_iso_ms(stage.get("end_time"))
        if end is not None and end <= at_ms:
            continue
        start = parse_iso_ms(stage.get("start_time"))
        return stage, ("upcoming" if start is not None and start > at_ms else "open")
    return None, "closed"


def _opensea_floor(stats: Any, eth_usd: Decimal | None) -> tuple[Decimal | None, dict[str, Any]]:
    """Secondary floor in USD from ``collections/{slug}/stats``, plus the raw facts.

    The floor is OpenSea's lowest ask. An ask that has never once been filled is not a
    market, so a collection with zero sales has no floor comparable. A floor quoted in a
    unit we cannot price (APE, an unknown ERC-20) stays unknown.
    """
    total = stats.get("total") if isinstance(stats, dict) else None
    if not isinstance(total, dict):
        return None, {"stats_state": "unrecognized"}
    floor = _dec(total.get("floor_price"))
    unit = str(total.get("floor_price_symbol") or "").upper() or None
    sales = _int(total.get("sales"))
    one_day = next(
        (i for i in (stats.get("intervals") or []) if isinstance(i, dict) and i.get("interval") == "one_day"),
        {},
    )
    facts = {
        "stats_state": "ok",
        "floor_price": str(floor) if floor is not None else None,
        "floor_unit": unit,
        "sales_total": sales,
        "sales_1d": _int(one_day.get("sales")),
        "owners": _int(total.get("num_owners")),
    }
    if floor is None or not sales:
        return None, facts
    if unit in _USD_UNITS:
        return floor.quantize(CENT), facts
    if unit in _ETH_UNITS and eth_usd is not None:
        return (floor * eth_usd).quantize(CENT), facts
    return None, facts


def fetch_opensea_stats(slug: str, conn: sqlite3.Connection | None = None) -> tuple[Any, str]:
    """``(body, state)`` for one collection's stats. 401 is a documented gate, not an outage.

    MEASURED 2026-10-02: 8 of 25 keyless stats calls answered 401 "Missing an API Key",
    and the same 8 slugs kept answering 401 when retried one at a time 5 s apart while
    their neighbours answered 200 -- so it is per collection, not a throttle. Those return
    ``needs_api_key`` without a provider error; a real failure still records one.
    """
    url = OPENSEA_STATS_URL.format(slug=slug)
    try:
        with guarded("opensea", "collections.stats", Priority.DISCOVERY, conn=conn):
            resp = httpx.get(url, timeout=20.0, headers={"user-agent": USER_AGENT},
                             follow_redirects=True)
            if resp.status_code == 401:
                return None, "needs_api_key"
            resp.raise_for_status()
            if len(resp.content) > 1_000_000:
                raise ValueError(f"stats body is {len(resp.content)} bytes")
            return resp.json(), "ok"
    except Exception as exc:  # noqa: BLE001 - one collection's stats are not an outage
        provider_error("opensea", "collections.stats", f"{type(exc).__name__}: {exc}",
                       conn=conn, url=url)
        return None, "unavailable"


def opensea_drops(
    raw: dict[str, Any] | None = None,
    stats: dict[str, Any] | None = None,
    eth_usd: Decimal | None = None,
    conn: sqlite3.Connection | None = None,
    *,
    at_ms: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> list[MintEvidence]:
    """Live and upcoming public mints on chains a Kaiba wallet can fund, with their floor.

    Replaces the Magic Eden launchpad list as the default NFT source (see
    ``OPENSEA_DROPS_URL``). Only drops with a stage that has not ended are kept; a drop
    with no stage has nothing left to mint. For drops that are open now, one stats call
    adds the secondary floor, which is what turns "a mint exists" into "a mint below its
    own market" -- the only NFT fact research 06 says has ever been worth acting on.

    ``raw`` maps drop type -> the list response and ``stats`` maps slug -> stats response,
    both for tests and replay; with ``raw`` given nothing is fetched. Nothing here mints,
    signs or registers anything: the rows it produces are refused for automated execution
    by :func:`assess_mint` (no EVM mint route), and are research for the owner.
    """
    source = "opensea"
    at = now_ms() if at_ms is None else int(at_ms)
    fx = _dec(eth_usd) if eth_usd is not None else _eth_usd(conn)
    calls = 0

    def pace() -> None:
        nonlocal calls
        if calls:
            sleep(OPENSEA_PACE_S)
        calls += 1

    def fetch(endpoint: str, url: str) -> Any:
        pace()
        return fetch_json(source, endpoint, url, priority=Priority.DISCOVERY, conn=conn)

    drops: dict[str, dict[str, Any]] = {}
    lists: dict[str, list[str]] = {}
    errors: list[str] = []
    for drop_type in OPENSEA_DROP_TYPES:
        if raw is not None:
            if drop_type not in raw:
                continue
            body = raw[drop_type]
        else:
            body = fetch("drops.list", OPENSEA_DROPS_URL.format(type=drop_type))
        rows = body.get("drops") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            errors.append(f"{drop_type}: unavailable or unrecognized")
            continue
        for row in rows:
            slug = str(row.get("collection_slug") or "").strip() if isinstance(row, dict) else ""
            if not slug:
                continue
            drops.setdefault(slug, row)
            lists.setdefault(slug, []).append(drop_type)
    if errors and not drops:
        detail = "; ".join(errors)
        provider_error(source, "drops.list", detail, conn=conn)
        _source_receipt(conn, source, 0, error=detail)
        return []

    candidates: list[tuple[str, dict[str, Any], Chain, dict[str, Any], str]] = []
    skipped = {"chain": 0, "closed": 0}
    for slug, drop in drops.items():
        chain = OPENSEA_CHAINS.get(str(drop.get("chain") or "").lower())
        if chain is None:
            skipped["chain"] += 1
            continue
        stage, state = _drop_stage(drop, at)
        if stage is None:
            skipped["closed"] += 1
            continue
        candidates.append((slug, drop, chain, stage, state))
    # Stats go to the drops a buyer could act on now, funded chain first.
    candidates.sort(key=lambda c: (c[4] != "open", c[2] is not Chain.ROBINHOOD,
                                   not bool(c[1].get("is_minting")), c[0]))

    out: list[MintEvidence] = []
    stats_calls = 0
    for slug, drop, chain, stage, state in candidates:
        floor_usd: Decimal | None = None
        facts: dict[str, Any] = {"stats_state": "not_open" if state != "open" else "skipped_cap"}
        if state == "open" and (stats is not None or stats_calls < OPENSEA_MAX_STATS):
            if stats is not None:
                body = stats.get(slug)
                state_of_stats = "ok" if body is not None else "unavailable"
            else:
                stats_calls += 1
                pace()
                body, state_of_stats = fetch_opensea_stats(slug, conn)
            if body is None:
                facts = {"stats_state": state_of_stats}
            else:
                floor_usd, facts = _opensea_floor(body, fx)
        native = str(stage.get("price_currency_address") or _ZERO_ADDRESS).lower() == _ZERO_ADDRESS
        price_native = _wei_to_native(stage.get("price")) if native and chain is not Chain.SOL else None
        price_usd = (price_native * fx).quantize(CENT) if price_native is not None and fx else None
        if price_native is not None and price_native == 0:
            price_usd = Decimal("0.00")  # a free mint costs nothing to price
        stage_type = str(stage.get("stage_type") or "") or None
        out.append(
            MintEvidence(
                name=str(drop.get("collection_name") or slug).strip() or slug,
                symbol=slug,
                chain=chain,
                chain_hint=str(drop.get("chain")),
                contract_address=drop.get("contract_address"),
                collection=slug,
                mint_price_native=price_native,
                mint_price_usd=price_usd,
                launch_ms=parse_iso_ms(stage.get("start_time")),
                end_ms=parse_iso_ms(stage.get("end_time")),
                guards_verified=False,  # an EVM drop has no Candy Guard; nothing was read
                public_phase=stage_type == "public_sale",
                recent_floor_usd=floor_usd,
                gas_cost_usd=gas_estimate(chain),
                source=source,
                url=str(drop.get("opensea_url") or f"https://opensea.io/collection/{slug}"),
                meta={
                    "stage_state": state,
                    "stage_type": stage_type,
                    "stage_label": stage.get("label"),
                    "is_minting": bool(drop.get("is_minting")),
                    "max_per_wallet": _int(stage.get("max_per_wallet")),
                    "drop_type": drop.get("drop_type"),
                    "opensea_lists": lists.get(slug, []),
                    "price_currency": "native" if native else stage.get("price_currency_address"),
                    "eth_usd": str(fx) if fx is not None else None,
                    **facts,
                },
            )
        )
    log.info("opensea drops: %d kept, %d off-chain, %d closed, %d stats calls",
             len(out), skipped["chain"], skipped["closed"], stats_calls)
    # A drop list that answered is a healthy source even when nothing on our chains is
    # minting today; only "every list failed" is an error (handled above).
    _source_receipt(conn, source, len(out))
    return out


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
    """Identify only decoded Core mint instructions, never an arbitrary account.

    Metaplex's generated mintV1 uses machine index 2 (index 0 is the guard);
    mintAssetFromCandyMachine uses index 0. Unknown discriminators, update
    instructions and conflicting machines remain unresolved. This is transaction
    identity, not proof of a current public mint route or verified guards.
    """
    shapes = {
        CORE_CANDY_MACHINE_PROGRAM: (bytes([84, 175, 211, 156, 56, 250, 104, 118]), 0, 11, 12),
        CANDY_GUARD_PROGRAM: (bytes([145, 98, 192, 118, 184, 147, 118, 104]), 2, 13, 13),
    }
    outer = payload.get("instructions")
    instructions = list(outer) if isinstance(outer, list) else []
    for ins in list(instructions):
        if isinstance(ins, dict) and isinstance(ins.get("innerInstructions"), list):
            instructions.extend(ins["innerInstructions"])
    machines: set[str] = set()
    for ins in instructions:
        if not isinstance(ins, dict):
            continue
        shape = shapes.get(str(ins.get("programId")))
        accounts, data = ins.get("accounts"), ins.get("data")
        if shape is None or not isinstance(accounts, list) or not isinstance(data, str) or len(data) > 4096:
            continue
        discriminator, index, account_count, data_size = shape
        if len(accounts) < account_count:
            continue
        if index == 2 and accounts[1] != CORE_CANDY_MACHINE_PROGRAM:
            continue
        machine = accounts[index]  # do not filter accounts and shift their positions
        if not isinstance(machine, str) or not 32 <= len(machine) <= 44:
            continue
        try:
            decoded = base58.b58decode(data)
            key_bytes = base58.b58decode(machine)
            if (len(decoded) >= data_size and decoded[:8] == discriminator
                    and len(key_bytes) == 32 and base58.b58encode(key_bytes).decode("ascii") == machine):
                machines.add(machine)
        except ValueError:
            continue
    return next(iter(machines)) if len(machines) == 1 else None


def parse_helius_payload(payload: dict[str, Any]) -> MintEvidence | None:
    """One enhanced-webhook delivery to one mint observation. Unknown shapes return None."""
    if not isinstance(payload, dict):
        return None
    if payload.get("transactionError") is not None:
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
        # Another wallet's successful mint may be private/allowlisted. Its observation
        # clock is neither a verified public start nor protection from the Bot Tax.
        launch_ms=None,
        start_slot=None,
        guards=[],
        guards_verified=False,
        public_phase=None,
        source="helius",
        url=f"https://solscan.io/account/{machine}" if machine else None,
        observed_mints=int(event_type == "NFT_MINT"),
        meta={
            "event_type": event_type, "signature": payload.get("signature"),
            "observed_slot": slot,
            "observed_chain_ms": int(ts * 1000) if isinstance(ts, int | float) and not isinstance(ts, bool) else None,
        },
    )


def helius_mint_watch(
    conn: sqlite3.Connection | None = None,
    payloads: list[dict[str, Any]] | None = None,
    limit: int = 500,
    *, source_report: dict[str, str] | None = None,
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
    parse_errors: list[str] = []
    for row_id, payload in rows:
        if row_id is not None:
            processed.append(row_id)
        try:
            ev = parse_helius_payload(payload)
        except Exception as exc:  # noqa: BLE001 - one bad delivery must not stop the drain
            provider_error("helius", "webhook.parse", f"{type(exc).__name__}: {exc}", conn=c)
            parse_errors.append(f"parse failed: {type(exc).__name__}: {exc}")
            continue
        if ev is None:
            continue
        key = ev.candy_machine or ev.name
        prior = by_machine.get(key)
        if prior is None:
            by_machine[key] = ev
        else:
            prior.observed_mints += ev.observed_mints
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
    _source_receipt(c, "helius_mint_watch", len(by_machine),
                    error="; ".join(parse_errors)[:500] if parse_errors else None,
                    no_deliveries=not rows)
    if source_report is not None:
        source_report["helius_mint_watch"] = (
            "no_deliveries" if not rows else "error" if parse_errors else "ok" if by_machine else "empty"
        )
    return list(by_machine.values())


# ---------------------------------------------------------------------------- assessment


def assess_mint(evidence: MintEvidence, cfg: HunterConfig | None = None) -> EvScore:
    """Gate the route, lifecycle, inputs and EV before any funded plan.

    Solana guard rules apply only to a known Solana route. Incomplete valuations retain
    numeric diagnostics for the shared EvScore API, but are refused and never stored as
    priced rankings. Positive arithmetic alone is not a verified opportunity.
    """
    cfg = cfg or hunter_config()
    rationale: list[str] = []
    warnings: list[str] = []
    blockers: list[str] = []

    if evidence.chain is not Chain.SOL:
        blockers.append(
            f"unsupported NFT mint execution route for chain {evidence.chain or evidence.chain_hint or 'unknown'}"
        )
    else:
        if not evidence.candy_machine or not evidence.candy_machine.strip():
            blockers.append("no verified Candy Machine route: a collection contract alone is not a mint route")
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
        if evidence.public_phase is not True:
            blockers.append("no verified public phase")

        if evidence.start_slot is None or evidence.start_slot <= 0:
            blockers.append(
                "no valid on-chain start slot: Candy Guard's Bot Tax charges SOL for failed guard "
                "checks, so we will not send anything we cannot time"
            )
        else:
            rationale.append(f"start slot {evidence.start_slot}: executor waits for it, never before")
        if MintGuard.END_DATE in evidence.guards and evidence.end_ms is None:
            blockers.append("endDate guard present but end time unknown")

    if evidence.end_ms is not None:
        if evidence.end_ms <= now_ms():
            blockers.append("mint participation window expired: actual end time has passed")
        if evidence.launch_ms is not None and evidence.end_ms <= evidence.launch_ms:
            blockers.append("invalid lifecycle: end time is at or before the launch start")

    if not evidence.valuation_complete:
        warnings.append(
            "incomplete valuation: numeric score is diagnostic only, not a mint price or funded opportunity"
        )
    if _dec(evidence.mint_price_native) is None:
        blockers.append("native mint price missing or invalid: no priced mint instruction")
    gas = _dec(evidence.gas_cost_usd)
    hours = _dec(evidence.time_cost_hours)
    if gas is None or hours is None:
        blockers.append("gas or time cost missing or invalid")
    price = _dec(evidence.mint_price_usd)
    if price is None:
        blockers.append("mint price not priced in USD or invalid: unknown stays unknown")
        price = Decimal("0")
    else:
        rationale.append(f"mint price ${price}")

    floor = _dec(evidence.floor_comparable_usd)
    if floor is None:
        blockers.append(
            "no floor comparable: ~96% of collections end with zero activity, so an "
            "unpriceable mint is a donation"
        )
        floor = Decimal("0")
    else:
        which = "recent floor" if evidence.recent_floor_usd is not None else "creator's prior collection floor"
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
    time_cost = ((hours or Decimal("0")) * cfg.operator_hourly_usd).quantize(CENT)
    cost = (price + (gas or Decimal("0")) + time_cost).quantize(CENT)
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
        blockers.append(f"non-positive expected value (${ev}): no funded mint plan")
    elif ev < cfg.ev_threshold_usd:
        blockers.append(f"expected value ${ev} below configured threshold ${cfg.ev_threshold_usd}")

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
        total_capital_usd=Decimal("0"),  # no allocation until all gates pass
        warnings=list(score.warnings),
    )
    if score.refused:
        plan.refused = True
        plan.refusal_reason = "; ".join(score.blockers)
        return plan

    plan.total_capital_usd = evidence.mint_price_usd
    plan.steps = [
        make_step(
            1,
            PlanAction.WALLET_CONNECT,
            f"use the single mint wallet, funded with {evidence.mint_price_native} SOL + fees",
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
    report: dict[str, Any] | None = None,
) -> int:
    """Score and store everything the mint watchers found. Returns rows written.

    Returns 0 immediately unless the hunter is enabled. See
    :data:`NFT_HUNTER_DISABLED_REASON` for why it is off by default.
    """
    if not (force or enabled()):
        log.info("nft hunter is disabled: %s", NFT_HUNTER_DISABLED_REASON)
        if report is not None:
            report.update(outcome="disabled", written=0, new_count=0, qualified_count=0,
                          sources={}, funded_action_authorized=False,
                          reason=NFT_HUNTER_DISABLED_REASON)
        return 0
    c = conn or get_conn()
    cfg = cfg or hunter_config()
    source_states: dict[str, str] = {}
    # Magic Eden's launchpad list left the default sweep on 2026-10-02: re-measured that
    # day it still returned the same 200 entries, newest launch 2026-02-20 and none in the
    # future, so every pass re-upserted 200 refused rows and found nothing. OpenSea's drop
    # list is the working public source. ``magic_eden_launchpad`` stays callable.
    found = mints if mints is not None else [
        *opensea_drops(conn=c), *helius_mint_watch(c, source_report=source_states),
    ]
    written = 0
    ids: set[str] = set()
    new_ids: set[str] = set()
    for mint in found:
        score = assess_mint(mint, cfg)
        evidence = mint.to_evidence()
        before = fetch_one(
            c, "SELECT ev_score FROM opportunities WHERE opportunity_id=?", (evidence.key,)
        )
        ids.add(evidence.key)
        if before is None:
            new_ids.add(evidence.key)
        plan = build_mint_plan(mint, cfg)
        store_opportunity(c, evidence, score, plan=plan.model_dump(mode="json"))
        if not mint.valuation_complete:
            # Shared EvScore/store_opportunity still require numeric fields. They are
            # diagnostics on refused mints, not complete values for the common ranking.
            cost_known = all(_dec(value) is not None for value in (
                mint.mint_price_usd, mint.gas_cost_usd, mint.time_cost_hours,
            ))
            c.execute(
                "UPDATE opportunities SET ev_score=NULL, cost_usd=?, gross_usd=? WHERE opportunity_id=?",
                (str(score.cost_usd) if cost_known else None,
                 str(score.gross_usd) if _dec(mint.floor_comparable_usd) is not None else None,
                 evidence.key),
            )
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
    if report is not None:
        report.update(refresh_summary(c, ids, new_ids, written, cfg,
                                      NFT_SOURCES if mints is None else (),
                                      source_states=source_states))
    return written


def refresh_report(
    conn: sqlite3.Connection | None = None,
    mints: list[MintEvidence] | None = None,
    cfg: HunterConfig | None = None,
    *, force: bool = False,
) -> dict[str, Any]:
    """Research collection receipt; never registers a webhook or executes a mint."""
    report: dict[str, Any] = {}
    refresh(conn, mints, cfg, force=force, report=report)
    return report


def weekly_report(conn: sqlite3.Connection | None = None, limit: int = 15) -> str:
    """Retained-registry audit, not the shared qualified-research reader."""
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
                cost=f"${row['cost_usd']}" if row.get("cost_usd") is not None else "unknown",
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
    "refresh_report",
    "register_helius_webhook",
    "weekly_report",
]
