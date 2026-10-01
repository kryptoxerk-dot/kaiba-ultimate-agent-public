"""Per-token trade flow and curve-snapshot deltas — the two inputs four lanes were missing.

On 2026-09-20 seven of eight lanes could not fire. Four of them were blocked here, and for
two distinct reasons that this module answers separately.

**`confluence-5`, `trusted-copy` and `sm-trenches` need swaps for the token being scanned.**
The database held 1,142 swap rows and not one of them was for a token tier 1 had scanned:
they all came from the wallet backfill, which walks *wallet* history. Those three lanes are
blocked at their first gate, before a wallet grade matters. What they need is who bought
this mint, when, and for how much.

**`curve-velocity` needs a rate, and a rate needs two observations.** Tier 1 scans most
tokens inside their first minute, so `sol_per_min` has no denominator and `sol_per_swap` —
*the* published graduation predictor (docs/EDGE-AND-VARIABLES.md §1, variable #3) — has no
trade count. A single curve reading cannot produce either. Migration 015 stores the earlier
observation so the next one becomes a delta.

Both are solved by one free, keyless source, found by probing pump.fun's own frontend API
on 2026-09-20:

    GET https://frontend-api-v3.pump.fun/trades/{chain_id}/{mint}?limit=100[&before=<ordinalKey>]

It returns full trade records — signature, slot, block time, trader, side, base and quote
amounts, USD value — for both the bonding curve and the post-graduation venue, paginated
backwards by `ordinalKey`, with no API key. The two documented alternatives were measured
and both lose:

* **Helius**, aimed at the *mint* rather than a wallet, works but is lossy and expensive.
  On a live mint, one 100-transaction Enhanced page cost 100 credits and yielded 25
  transactions typed `SWAP`, of which `backfill.classify_swap` could use 17 — the other 66
  were typed `TRANSFER` because Helius's parser does not recognise most pump.fun curve
  trades. That is ~5.9 credits per usable swap, so one median graduating token (~457
  trades) costs ~2,700 credits and the whole 1M monthly free allowance buys ~370 tokens.
  Tier 1 scans that many in an hour.
* **PumpPortal `subscribeTokenTrade`** is closed. A live connection on 2026-09-20 answered
  verbatim: the method is "only available when connecting with an API key funded with at
  least 0.02 SOL". `kaiba/ingest/pumpportal.py` already declines to send the frame without
  a key, and that is correct.

Three rules this module will not bend.

1. **A rate from incomplete coverage is worse than no rate.** Three observed trades out of
   four hundred turns 0.19 SOL/swap into 25, clearing a 0.18 floor by ~130x on a token
   pacing at the population average. Every rate here is refused unless the collection is
   provably complete across the whole interval it divides by. :func:`velocity_between`
   returns a reason string, never a number it is unsure of.
2. **Nothing is a pump.fun constant.** Creators choose a starting market cap and observed
   graduation targets span 0.41 to 115 SOL. The only invariant is
   ``virtual_token_reserves - real_token_reserves = 279,900,000,000,000``. This module
   never derives curve geometry itself — it reads the dict
   `kaiba/execution/scanner.py::curve_from_payload` already produces and adds fields to it
   under the same key names, so the lane contract does not fork.
3. **Missing is `None`.** No zero-filled trade count, no zero velocity, no float anywhere
   near money. Lamports and atoms are `int`, SOL and USD are `Decimal`.

Pacing: the limiter has no entry for ``pumpfun``, so it takes the default
``min_interval_ms=1000`` — exactly the one request per second that held for 73 consecutive
pump.fun pages with zero 429s. Do not add a faster entry for this provider.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from kaiba.core.db import fetch_all, fetch_one, get_conn
from kaiba.core.limiter import Priority
from kaiba.core.schemas import Chain, EvidenceBasis, Receipt, now_ms
from kaiba.providers._http import get_json

log = logging.getLogger(__name__)

#: Same provider string the scanner's curve route uses, so the two share one limiter
#: bucket. They are the same host and a 429 on one is a 429 on both.
PROVIDER = "pumpfun"

#: ``family.name``; the family before the dot is what a 429 cools down. Deliberately the
#: same ``coins`` family as the scanner's ``coins.detail``, because both routes are
#: frontend-api-v3 and cooling one without the other would keep hammering the host.
TRADES_ENDPOINT = "coins.trades"

TRADES_URL = "https://frontend-api-v3.pump.fun/trades/{chain_id}/{mint}"

#: CAIP-2 id for Solana mainnet. pump.fun's v3 API requires it in the path and validates it
#: against ``^solana:[1-9A-HJ-NP-Za-km-z]{32}$``; it is also echoed as ``chain_id`` in every
#: ``/coins/{mint}`` payload, which is where this value was read from.
SOLANA_CHAIN_ID = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"

USER_AGENT = "Mozilla/5.0 (compatible; kaiba/0.1; +https://pump.fun)"

#: ``swaps.source`` for everything written here. Distinct from ``helius:backfill`` so the
#: two collection paths stay separable in a query.
SOURCE = "pumpfun:trades"

LAMPORTS_PER_SOL = Decimal(1_000_000_000)
SOL_DECIMALS = 9

#: Quote mints this module will price in lamports. The bonding curve quotes in native SOL
#: (the system program id) and the post-graduation venues quote in wrapped SOL; anything
#: else is a different unit and ``amount_native`` stays ``None`` rather than mixing them.
#: Mirrors ``scanner.SOL_QUOTE_MINTS``.
SOL_QUOTE_MINTS: frozenset[str] = frozenset(
    {
        "11111111111111111111111111111111",
        "So11111111111111111111111111111111111111112",
    }
)

#: pump.fun's own default. Only used when neither the caller, the ``tokens`` row nor the
#: coin payload supplies ``base_decimals``.
DEFAULT_TOKEN_DECIMALS = 6


# --------------------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FlowConfig:
    """Every number here is annotated with where it came from."""

    #: MEASURED: the API answers ``limit must not be greater than 100``.
    page_limit: int = 100

    #: Pages one :func:`collect_trades` call may fetch. At one request per second this is
    #: also the wall-clock budget. INVENTED, but sized against a measurement: a median
    #: graduating pump.fun token sees ~457 trades, so 8 pages covers one end to end.
    max_pages: int = 8

    #: Pages the *scan path* may fetch. Tier 1 already costs ~7.6 s per token; spending
    #: eight more seconds on trade history would halve throughput. A token scanned inside
    #: its first minute has well under 100 trades, so one page is the common case and this
    #: only bites on a mint that has been running for a while.
    scan_max_pages: int = 2

    #: Required by docs/CONTRACT.md: this adapter makes more than one call per logical
    #: operation, so the non-waiting default would silently drop every page after the
    #: first — the exact failure the ``_http`` docstring records for RugCheck.
    wait_for_slot_s: float = 15.0
    timeout_s: float = 15.0
    #: The route answers 503 intermittently under load; a retry a second later succeeded
    #: every time it was observed on 2026-09-20.
    retries: int = 3
    #: Trade history is append-only, but a page fetched a second ago is not the same page
    #: as one fetched now when the token is live. No caching on the newest page.
    ttl_s: float = 0.0

    #: A per-swap rate needs enough swaps that one outlier cannot set it. INVENTED. Three
    #: is the floor at which a median is meaningful at all; below it the interval is
    #: reported with its trade count and no rate.
    min_trades_for_rate: int = 3

    #: Two snapshots taken seconds apart divide a rounding error by a rounding error.
    #: INVENTED, and deliberately shorter than the 60 s the scanner requires for a
    #: per-*minute* rate: per-swap does not have time in its denominator, so the constraint
    #: is only that the interval be long enough to hold real trades.
    min_interval_s: float = 10.0

    #: Grace between a token's creation and our first recorded swap that still counts as
    #: "our coverage starts at launch". Matches ``scanner.ScanConfig.swap_coverage_grace_s``
    #: deliberately: two different answers to the same question is how a lane ends up
    #: reading a number the scanner would have refused.
    coverage_grace_s: int = 60

    #: Cross-check band. The SOL the curve gained between two snapshots should roughly
    #: equal the net SOL the trades we collected moved, less pump.fun's 1% fee. A wide
    #: band because fees, rounding and the odd unpriced leg all live in it; its job is to
    #: catch a *missing chunk of trades*, which shows up as a factor, not a percent.
    #: INVENTED.
    flow_ratio_min: Decimal = Decimal("0.5")
    flow_ratio_max: Decimal = Decimal("2")
    #: Below this the cross-check is noise, so it is skipped rather than failed. 0.001 SOL.
    flow_check_floor_lamports: int = 1_000_000

    #: Retention. At 6.4 tokens/min a snapshot per scan is ~9,200 rows a day forever. A
    #: velocity delta reads the previous snapshot or two; the rest is backtest material.
    snapshot_keep_per_token: int = 24
    snapshot_max_age_s: int = 7 * 86_400
    #: Prune on every Nth insert rather than every one. Deterministic (it keys off the new
    #: rowid), so a test can force it.
    prune_every: int = 200


DEFAULT_CONFIG = FlowConfig()


# --------------------------------------------------------------------------------------
# parsed shapes
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TradeRow:
    """One row destined for ``swaps``, in the column contract of ``001_core.sql``.

    No column is invented. ``fee_payer`` is ``None`` because this route reports the trader,
    not the signer, and guessing they are the same wallet would corrupt every cluster edge
    derived from fee payers.
    """

    chain: Chain
    tx: str
    slot: int | None
    block_index: int | None
    ts_ms: int
    wallet: str
    token: str
    side: str
    amount_token: int | None
    amount_native: int | None
    price_usd: Decimal | None
    usd_value: Decimal | None
    program: str | None
    ordinal: str = ""

    def as_params(self) -> tuple[Any, ...]:
        return (
            self.chain.value,
            self.tx,
            self.slot,
            self.block_index,
            self.ts_ms,
            self.wallet,
            self.token,
            self.side,
            None if self.amount_token is None else str(self.amount_token),
            None if self.amount_native is None else str(self.amount_native),
            None if self.price_usd is None else str(self.price_usd),
            None if self.usd_value is None else str(self.usd_value),
            self.program,
            SOURCE,
            0,
            None,
        )


@dataclass(frozen=True, slots=True)
class FlowResult:
    """What one :func:`collect_trades` call established.

    ``complete`` means the collection reached the ``since_ms`` it was asked for (or the
    beginning of the token's history). ``coverage_from_ms`` is the oldest moment the
    collection is known to be *gapless* down to, and is the only field a velocity
    calculation is allowed to trust. Both are ``None``/``False`` rather than optimistic.
    """

    chain: Chain
    token: str
    trades_seen: int = 0
    rows_written: int = 0
    rows_duplicate: int = 0
    pages: int = 0
    complete: bool = False
    coverage_from_ms: int | None = None
    oldest_ms: int | None = None
    newest_ms: int | None = None
    reason: str = "not_attempted"
    receipts: tuple[Receipt, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.pages > 0 and self.reason not in {"not_attempted", "unavailable"}

    def as_dict(self) -> dict[str, Any]:
        return {
            "chain": self.chain.value,
            "token": self.token,
            "trades_seen": self.trades_seen,
            "rows_written": self.rows_written,
            "rows_duplicate": self.rows_duplicate,
            "pages": self.pages,
            "complete": self.complete,
            "coverage_from_ms": self.coverage_from_ms,
            "oldest_ms": self.oldest_ms,
            "newest_ms": self.newest_ms,
            "reason": self.reason,
        }


# --------------------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------------------


def _int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(Decimal(str(value)))
        except (InvalidOperation, TypeError, ValueError):
            return None


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _scaled_atoms(raw: Any, reported_decimals: Any, target_decimals: int) -> int | None:
    """Rescale one of the route's trimmed ``{raw, decimals}`` pairs to base units.

    The route trims trailing zeros, so the same 6-decimal token arrives as
    ``{"raw": "32751487", "decimals": 6}`` on one trade and ``{"raw": "4975", "decimals": 7}``
    on the next. Reading ``raw`` as atoms is therefore wrong by a factor of ten per trimmed
    digit, which is exactly the kind of error that survives a code review and shows up as a
    wallet that appears to have bought a thousand times its actual size.
    """
    amount = _dec(raw)
    reported = _int(reported_decimals)
    if amount is None or reported is None or reported < 0 or target_decimals < 0:
        return None
    scaled = amount * (Decimal(10) ** (target_decimals - reported))
    if scaled != scaled.to_integral_value():
        # A fractional base unit means our idea of the token's decimals is wrong. Refuse
        # rather than truncate: a truncated amount is a plausible-looking wrong number.
        return None
    return int(scaled)


def _ordinal_ok(ordinal: str) -> bool:
    """The route validates ``before`` against ``^\\d{1,20}-\\d{1,10}-\\d{1,10}-\\d{1,15}$``."""
    parts = ordinal.split("-")
    if len(parts) != 4:
        return False
    widths = (20, 10, 10, 15)
    return all(p.isdigit() and 1 <= len(p) <= w for p, w in zip(parts, widths, strict=True))


def token_decimals(
    chain: Chain,
    token: str,
    conn: sqlite3.Connection | None = None,
    *,
    payload: Mapping[str, Any] | None = None,
) -> int | None:
    """Decimals for a mint, from the coin payload then the ``tokens`` row. ``None`` if neither."""
    if payload is not None:
        dec = _int(payload.get("base_decimals"))
        if dec is not None and 0 <= dec <= 18:
            return dec
    if conn is None:
        return None
    try:
        row = fetch_one(
            conn, "SELECT decimals FROM tokens WHERE chain=? AND address=?", (chain.value, token)
        )
    except sqlite3.Error as exc:
        log.warning("token_flow: tokens row unreadable for %s (%s)", token[:12], exc)
        return None
    dec = _int(row["decimals"]) if row else None
    return dec if dec is not None and 0 <= dec <= 18 else None


# --------------------------------------------------------------------------------------
# the trade route
# --------------------------------------------------------------------------------------


def fetch_trades_page(
    mint: str,
    *,
    before: str | None = None,
    chain_id: str = SOLANA_CHAIN_ID,
    config: FlowConfig = DEFAULT_CONFIG,
    priority: Priority = Priority.DISCOVERY,
    conn: sqlite3.Connection | None = None,
) -> tuple[dict[str, Any] | None, Receipt]:
    """One page of ``/trades/{chain_id}/{mint}``, newest first. Never raises.

    ``before`` is an ``ordinalKey`` from a previous page and is **exclusive** — verified on
    2026-09-20 by walking two pages and finding zero overlap. A page shorter than ``limit``
    is the end of history.
    """
    params: dict[str, Any] = {"limit": max(1, min(int(config.page_limit), 100))}
    if before:
        if not _ordinal_ok(before):
            return None, Receipt(
                provider=PROVIDER,
                endpoint=TRADES_ENDPOINT,
                basis=EvidenceBasis.UNAVAILABLE,
                note=f"malformed ordinal cursor: {before[:60]}",
            )
        params["before"] = before

    got = get_json(
        PROVIDER,
        TRADES_ENDPOINT,
        TRADES_URL.format(chain_id=chain_id, mint=mint),
        params=params,
        headers={"user-agent": USER_AGENT, "accept": "*/*"},
        ttl_s=config.ttl_s,
        priority=priority,
        wait_for_slot_s=config.wait_for_slot_s,
        timeout_s=config.timeout_s,
        retries=config.retries,
        conn=conn,
    )
    if not got.ok or not isinstance(got.data, dict):
        return None, got.receipt
    return got.data, got.receipt


def parse_trade(
    raw: Mapping[str, Any], token: str, *, decimals: int, chain: Chain = Chain.SOL
) -> TradeRow | None:
    """One trade record to a ``swaps`` row, or ``None`` when it cannot be trusted.

    Pure, so every unit conversion is testable without a network.
    """
    signature = str(raw.get("txId") or "")
    wallet = str((raw.get("trader") or {}).get("address") or "")
    side = str(raw.get("side") or "").lower()
    ts_ms = _int(raw.get("blockTimeMs"))
    if not signature or not wallet or side not in {"buy", "sell"} or ts_ms is None:
        return None
    if str(raw.get("kind") or "swap").lower() != "swap":
        return None

    base = raw.get("baseAmount") or {}
    amount_token = _scaled_atoms(base.get("raw"), base.get("decimals"), decimals)

    amount_native: int | None = None
    quote_mint = str((raw.get("quote") or {}).get("id") or "")
    if quote_mint in SOL_QUOTE_MINTS:
        quote = raw.get("quoteAmount") or {}
        amount_native = _scaled_atoms(quote.get("raw"), quote.get("decimals"), SOL_DECIMALS)
        if amount_native is None:
            # ``valueNative`` is the same figure as a decimal string; use it when the
            # trimmed pair did not resolve rather than dropping the quote leg entirely.
            native = _dec(raw.get("valueNative"))
            if native is not None:
                scaled = native * LAMPORTS_PER_SOL
                if scaled == scaled.to_integral_value():
                    amount_native = int(scaled)

    return TradeRow(
        chain=chain,
        tx=signature,
        slot=_int(raw.get("blockId")),
        block_index=_int(raw.get("txIndex")),
        ts_ms=ts_ms,
        wallet=wallet,
        token=token,
        side=side,
        amount_token=amount_token,
        amount_native=amount_native,
        price_usd=_dec(raw.get("priceUsd")),
        usd_value=_dec(raw.get("valueUsd")),
        # The route names the venue ("pump", "raydium_amm_v4"), not a program id. It goes
        # in ``program`` because that is the column for "what executed this", and a venue
        # table is deliberately not hard-coded here: 39% of Solana DEX volume sits in prop
        # AMMs that redeploy (docs/EDGE-AND-VARIABLES.md §4).
        program=str(raw.get("venue")) if raw.get("venue") else None,
        ordinal=str(raw.get("ordinalKey") or ""),
    )


_SWAP_INSERT = (
    "INSERT OR IGNORE INTO swaps "
    "(chain, tx, slot, block_index, ts_ms, wallet, token, side, amount_token, amount_native, "
    " price_usd, usd_value, program, source, is_create_tx, fee_payer) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def write_trades(conn: sqlite3.Connection, rows: Sequence[TradeRow]) -> tuple[int, int]:
    """``(written, duplicate)``.

    Idempotent on the transaction signature. The table's own UNIQUE constraint is
    ``(chain, tx, wallet, token, side, amount_token)``, which does the work whenever
    ``amount_token`` is known — but SQLite treats NULLs as distinct, so a row whose atoms
    could not be resolved would insert a fresh duplicate on every re-run. Signatures
    already present for this mint are therefore filtered first, which also means a row the
    Helius backfill wrote is never counted twice.
    """
    if not rows:
        return 0, 0
    known: set[str] = set()
    by_token: dict[tuple[str, str], list[str]] = {}
    for row in rows:
        by_token.setdefault((row.chain.value, row.token), []).append(row.tx)
    for (chain_value, token), signatures in by_token.items():
        for start in range(0, len(signatures), 400):
            chunk = signatures[start : start + 400]
            placeholders = ",".join("?" * len(chunk))
            try:
                found = fetch_all(
                    conn,
                    f"SELECT DISTINCT tx FROM swaps WHERE chain=? AND token=? AND tx IN ({placeholders})",
                    (chain_value, token, *chunk),
                )
            except sqlite3.Error as exc:
                log.warning("token_flow: duplicate probe failed (%s)", exc)
                found = []
            known.update(str(r["tx"]) for r in found)

    written = 0
    attempted = 0
    for row in rows:
        if row.tx in known:
            continue
        attempted += 1
        try:
            written += conn.execute(_SWAP_INSERT, row.as_params()).rowcount or 0
        except sqlite3.Error as exc:
            log.warning("token_flow: swap insert failed for %s (%s)", row.tx[:12], exc)
    return written, len(rows) - written


def collect_trades(
    chain: Chain,
    token: str,
    conn: sqlite3.Connection | None = None,
    *,
    since_ms: int | None = None,
    created_ms: int | None = None,
    decimals: int | None = None,
    max_pages: int | None = None,
    chain_id: str = SOLANA_CHAIN_ID,
    config: FlowConfig = DEFAULT_CONFIG,
    priority: Priority = Priority.DISCOVERY,
) -> FlowResult:
    """Page this mint's trades backwards to ``since_ms`` and write them to ``swaps``.

    Stops on the first of: a page shorter than the limit (end of history), a page whose
    oldest trade is at or before ``since_ms``, or the page budget. The common case for a
    freshly launched token is **one request** — it has fewer than 100 trades, so the first
    page is also the last.
    """
    if chain is not Chain.SOL:
        return FlowResult(chain=chain, token=token, reason=f"no_trade_source_for_{chain.value}")
    conn = conn if conn is not None else get_conn()
    budget = max(1, int(max_pages if max_pages is not None else config.max_pages))

    dec = decimals if decimals is not None else token_decimals(chain, token, conn)
    if dec is None:
        # pump.fun mints are 6 decimals in practice, but an assumed exponent is a silent
        # factor-of-a-million error in every amount, so it is recorded as an assumption.
        dec = DEFAULT_TOKEN_DECIMALS
        log.debug("token_flow: assuming %d decimals for %s", dec, token[:12])

    receipts: list[Receipt] = []
    rows: list[TradeRow] = []
    seen_ordinals: set[str] = set()
    before: str | None = None
    pages = 0
    oldest: int | None = None
    newest: int | None = None
    end_of_history = False
    reached_since = False
    reason = "page_budget_exhausted"

    while pages < budget:
        payload, receipt = fetch_trades_page(
            token, before=before, chain_id=chain_id, config=config, priority=priority, conn=conn
        )
        receipts.append(receipt)
        if payload is None:
            reason = "unavailable" if pages == 0 else "partial_provider_error"
            break
        pages += 1
        page = payload.get("trades")
        if not isinstance(page, list):
            reason = "malformed_page"
            break
        if not page:
            end_of_history = True
            reason = "end_of_history"
            break

        page_oldest: int | None = None
        last_ordinal: str | None = None
        for item in page:
            if not isinstance(item, Mapping):
                continue
            ordinal = str(item.get("ordinalKey") or "")
            last_ordinal = ordinal or last_ordinal
            if ordinal and ordinal in seen_ordinals:
                continue
            row = parse_trade(item, token, decimals=dec, chain=chain)
            if row is None:
                continue
            if ordinal:
                seen_ordinals.add(ordinal)
            rows.append(row)
            page_oldest = row.ts_ms if page_oldest is None else min(page_oldest, row.ts_ms)
            oldest = row.ts_ms if oldest is None else min(oldest, row.ts_ms)
            newest = row.ts_ms if newest is None else max(newest, row.ts_ms)

        if len(page) < config.page_limit:
            end_of_history = True
            reason = "end_of_history"
            break
        if since_ms is not None and page_oldest is not None and page_oldest <= since_ms:
            reached_since = True
            reason = "reached_watermark"
            break
        if not last_ordinal or not _ordinal_ok(last_ordinal):
            reason = "no_usable_cursor"
            break
        before = last_ordinal

    written, duplicate = write_trades(conn, rows)

    if end_of_history:
        coverage_from = created_ms if created_ms is not None else oldest
        if coverage_from is not None and oldest is not None:
            coverage_from = min(coverage_from, oldest)
    elif oldest is not None:
        coverage_from = oldest
    else:
        coverage_from = None

    return FlowResult(
        chain=chain,
        token=token,
        trades_seen=len(rows),
        rows_written=written,
        rows_duplicate=duplicate,
        pages=pages,
        complete=end_of_history or reached_since,
        coverage_from_ms=coverage_from,
        oldest_ms=oldest,
        newest_ms=newest,
        reason=reason,
        receipts=tuple(receipts),
    )


# --------------------------------------------------------------------------------------
# snapshots
# --------------------------------------------------------------------------------------


def record_snapshot(
    chain: Chain,
    token: str,
    curve: Mapping[str, Any],
    conn: sqlite3.Connection | None = None,
    *,
    trades_seen: int | None = None,
    trades_basis: str | None = None,
    coverage_from_ms: int | None = None,
    config: FlowConfig = DEFAULT_CONFIG,
) -> int | None:
    """Persist one curve observation. Returns the row id, or ``None`` if it was not stored.

    ``curve`` is the dict ``scanner.curve_from_payload`` produces; the raw reserve fields
    are what is stored and every derived figure is a convenience column.
    """
    conn = conn if conn is not None else get_conn()
    observed = _int(curve.get("observed_ms")) or now_ms()
    real_sol = _int(curve.get("sol_in_curve_lamports"))
    virtual_sol = _int(curve.get("virtual_sol_reserves"))
    real_token = _int(curve.get("real_token_reserves"))
    virtual_token = _int(curve.get("virtual_token_reserves"))
    if real_sol is None or virtual_sol is None or real_token is None or virtual_token is None:
        log.debug("token_flow: refusing snapshot for %s, reserves incomplete", token[:12])
        return None

    def _text(value: Any) -> str | None:
        return None if value is None else str(value)

    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO curve_snapshots "
            "(chain, token, observed_ms, real_sol_lamports, virtual_sol_lamports, "
            " real_token_atoms, virtual_token_atoms, progress_pct, graduation_sol, "
            " sol_in_curve, trades_seen, trades_basis, coverage_from_ms, created_ms, source) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                chain.value,
                token,
                observed,
                real_sol,
                virtual_sol,
                str(real_token),
                str(virtual_token),
                _text(curve.get("progress_pct")),
                _text(curve.get("graduation_sol")),
                _text(curve.get("sol_in_curve")),
                trades_seen,
                trades_basis,
                coverage_from_ms,
                _int(curve.get("created_ms")),
                str(curve.get("source") or PROVIDER),
            ),
        )
    except sqlite3.Error as exc:
        log.warning("token_flow: snapshot insert failed for %s (%s)", token[:12], exc)
        return None
    if not cur.rowcount:
        return None
    row_id = int(cur.lastrowid or 0)
    if config.prune_every > 0 and row_id % config.prune_every == 0:
        prune_snapshots(conn, config=config)
    return row_id


def latest_snapshot(
    chain: Chain,
    token: str,
    conn: sqlite3.Connection | None = None,
    *,
    before_ms: int | None = None,
) -> dict[str, Any] | None:
    """The newest stored snapshot strictly older than ``before_ms``."""
    conn = conn if conn is not None else get_conn()
    sql = "SELECT * FROM curve_snapshots WHERE chain=? AND token=?"
    params: list[Any] = [chain.value, token]
    if before_ms is not None:
        sql += " AND observed_ms < ?"
        params.append(int(before_ms))
    sql += " ORDER BY observed_ms DESC LIMIT 1"
    try:
        return fetch_one(conn, sql, tuple(params))
    except sqlite3.Error as exc:
        log.warning("token_flow: snapshot read failed for %s (%s)", token[:12], exc)
        return None


def prune_snapshots(
    conn: sqlite3.Connection | None = None,
    *,
    config: FlowConfig = DEFAULT_CONFIG,
    at_ms: int | None = None,
) -> int:
    """Enforce both retention bounds. Returns rows deleted.

    Age first, then the per-token cap. A token that is scanned every few seconds would
    otherwise keep thousands of rows well inside the age window.
    """
    conn = conn if conn is not None else get_conn()
    now = at_ms if at_ms is not None else now_ms()
    cutoff = now - int(config.snapshot_max_age_s) * 1000
    deleted = 0
    try:
        deleted += conn.execute(
            "DELETE FROM curve_snapshots WHERE observed_ms < ?", (cutoff,)
        ).rowcount or 0
        deleted += conn.execute(
            "DELETE FROM curve_snapshots WHERE id IN ("
            "  SELECT id FROM ("
            "    SELECT id, ROW_NUMBER() OVER ("
            "      PARTITION BY chain, token ORDER BY observed_ms DESC) AS rn"
            "    FROM curve_snapshots"
            "  ) WHERE rn > ?"
            ")",
            (max(1, int(config.snapshot_keep_per_token)),),
        ).rowcount or 0
    except sqlite3.Error as exc:
        log.warning("token_flow: snapshot prune failed (%s)", exc)
        return deleted
    return deleted


# --------------------------------------------------------------------------------------
# coverage and rates
# --------------------------------------------------------------------------------------


def trades_between(
    chain: Chain,
    token: str,
    conn: sqlite3.Connection,
    *,
    after_ms: int,
    until_ms: int,
) -> tuple[int, int | None]:
    """``(count, net_lamports)`` for swap rows in ``(after_ms, until_ms]``.

    ``net_lamports`` is buys minus sells and is ``None`` when any row in the window has no
    quote leg — a partially priced window cannot be cross-checked against the curve, and
    treating an unpriced leg as zero would make a missing chunk of flow look like agreement.
    """
    try:
        rows = fetch_all(
            conn,
            "SELECT side, amount_native FROM swaps "
            "WHERE chain=? AND token=? AND ts_ms > ? AND ts_ms <= ?",
            (chain.value, token, int(after_ms), int(until_ms)),
        )
    except sqlite3.Error as exc:
        log.warning("token_flow: swap window unreadable for %s (%s)", token[:12], exc)
        return 0, None
    net = 0
    priced = True
    for row in rows:
        amount = _int(row["amount_native"])
        if amount is None:
            priced = False
            continue
        net += amount if str(row["side"]).lower() == "buy" else -amount
    return len(rows), (net if priced else None)


def swap_count(chain: Chain, token: str, conn: sqlite3.Connection) -> int:
    """Every swap row we hold for this mint, from any source."""
    try:
        row = fetch_one(
            conn,
            "SELECT COUNT(*) AS n FROM swaps WHERE chain=? AND token=?",
            (chain.value, token),
        )
    except sqlite3.Error as exc:
        log.warning("token_flow: swap count unreadable for %s (%s)", token[:12], exc)
        return 0
    return _int(row["n"]) if row else 0


def launch_coverage_proved(
    chain: Chain,
    token: str,
    conn: sqlite3.Connection,
    *,
    created_ms: int | None,
) -> bool:
    """Has any collection ever proved we hold this mint's trades back to its launch?

    This is the thing a timestamp heuristic cannot know. ``scanner.swap_count_if_covered``
    asks "is our first swap row within a minute of the token's creation", which is a proxy
    and it is wrong in one direction that matters: a token whose first trade came six
    minutes after launch is refused, although we hold every trade it has ever had. On the
    live sample of 2026-09-20 that proxy alone refused 4 of 10 tokens for which the
    collector had walked the trade history to its end.

    Reaching the end of the trade route's history is direct evidence, and
    ``curve_snapshots.coverage_from_ms`` is where it is recorded: a value at or before the
    token's creation means the walk terminated rather than being cut off.
    """
    if created_ms is None:
        return False
    try:
        row = fetch_one(
            conn,
            "SELECT MIN(coverage_from_ms) AS m FROM curve_snapshots "
            "WHERE chain=? AND token=? AND coverage_from_ms IS NOT NULL",
            (chain.value, token),
        )
    except sqlite3.Error as exc:
        log.warning("token_flow: coverage lookup failed for %s (%s)", token[:12], exc)
        return False
    earliest = _int(row["m"]) if row else None
    return earliest is not None and earliest <= int(created_ms)


def gross_flow(
    chain: Chain,
    token: str,
    conn: sqlite3.Connection,
    *,
    since_ms: int | None = None,
) -> dict[str, Any]:
    """Gross buy and sell volume in lamports, and the counts behind them.

    Diagnostic, and it exists because of something the live run made obvious.
    ``sol_per_swap`` as the scanner defines it divides ``real_sol_reserves`` — the SOL
    *still in* the curve — by the trade count. On 2026-09-20 several live mints took in
    real money and gave all of it back, so they read as ~1 lamport across 27 trades: a
    correct statement about what is committed now, and a misleading one about how much
    each participant committed. The published predictor is closer to gross inflow per buy.

    Changing the lane's denominator is a lane decision, so this does not touch
    ``sol_per_swap``. It publishes the gross figures alongside it under keys no lane reads,
    so the two can be compared on real data before anything is changed.
    """
    sql = (
        "SELECT side, COUNT(*) AS n, SUM(CAST(amount_native AS INTEGER)) AS total, "
        "SUM(CASE WHEN amount_native IS NULL THEN 1 ELSE 0 END) AS unpriced "
        "FROM swaps WHERE chain=? AND token=?"
    )
    params: list[Any] = [chain.value, token]
    if since_ms is not None:
        sql += " AND ts_ms > ?"
        params.append(int(since_ms))
    sql += " GROUP BY side"
    try:
        rows = fetch_all(conn, sql, tuple(params))
    except sqlite3.Error as exc:
        log.warning("token_flow: gross flow unreadable for %s (%s)", token[:12], exc)
        return {}
    out: dict[str, Any] = {"buys": 0, "sells": 0, "buy_lamports": 0, "sell_lamports": 0, "unpriced": 0}
    for row in rows:
        side = str(row["side"]).lower()
        count = _int(row["n"]) or 0
        total = _int(row["total"]) or 0
        out["unpriced"] += _int(row["unpriced"]) or 0
        if side == "buy":
            out["buys"] += count
            out["buy_lamports"] += total
        elif side == "sell":
            out["sells"] += count
            out["sell_lamports"] += total
    return out


def covered_swap_count(
    chain: Chain,
    token: str,
    conn: sqlite3.Connection,
    *,
    created_ms: int | None,
    config: FlowConfig = DEFAULT_CONFIG,
) -> tuple[int | None, str]:
    """Our cumulative swap count for this mint, **only** when coverage starts at launch.

    Same rule and the same grace as ``scanner.swap_count_if_covered``, restated here so
    this module imports nothing from ``kaiba.execution``. Deliberately duplicated logic
    rather than a shared helper in a file this task does not own; if the two ever diverge,
    the scanner's is authoritative.
    """
    try:
        row = fetch_one(
            conn,
            "SELECT COUNT(*) AS n, MIN(ts_ms) AS first_ms FROM swaps WHERE chain=? AND token=?",
            (chain.value, token),
        )
    except sqlite3.Error as exc:
        log.warning("token_flow: swap count unreadable for %s (%s)", token[:12], exc)
        return None, "swaps_unreadable"
    count = _int(row["n"]) if row else 0
    if not count:
        return None, "no_swap_rows"
    if created_ms is None:
        return None, "creation_time_unknown"
    first_ms = _int(row["first_ms"]) if row else None
    if first_ms is None:
        return None, "no_swap_rows"
    lag_s = (first_ms - int(created_ms)) / 1000.0
    if lag_s > config.coverage_grace_s:
        return None, f"coverage_starts_{lag_s:.0f}s_after_launch"
    return count, "covered_from_launch"


def velocity_between(
    older: Mapping[str, Any],
    newer: Mapping[str, Any],
    *,
    trades: int,
    net_lamports: int | None = None,
    config: FlowConfig = DEFAULT_CONFIG,
) -> dict[str, Any]:
    """Derive ``sol_per_swap`` and ``sol_per_min`` from two snapshots.

    Returns a dict that always carries ``basis`` and, when it refused, ``refusal``. It
    never returns a rate it is unsure of: every refusal below is a case where the arithmetic
    would produce a plausible number from evidence that does not support one.
    """
    out: dict[str, Any] = {
        "sol_per_swap": None,
        "sol_per_min": None,
        "trades": trades,
        "basis": "none",
        "refusal": None,
        "interval_s": None,
        "delta_sol": None,
        "flow_ratio": None,
    }

    t0 = _int(older.get("observed_ms"))
    t1 = _int(newer.get("observed_ms"))
    sol0 = _int(older.get("real_sol_lamports"))
    sol1 = _int(newer.get("real_sol_lamports"))
    if t0 is None or t1 is None or sol0 is None or sol1 is None:
        out["refusal"] = "snapshot_fields_missing"
        return out
    if t1 <= t0:
        out["refusal"] = "snapshots_out_of_order"
        return out

    interval_s = Decimal(t1 - t0) / Decimal(1000)
    out["interval_s"] = interval_s
    if interval_s < Decimal(str(config.min_interval_s)):
        out["refusal"] = f"interval_{interval_s}s_under_{config.min_interval_s}s"
        return out

    # Coverage is the whole point. The newer snapshot's collection must reach back past the
    # older snapshot, or the trade count in the denominator is a sample and the rate it
    # produces is an artefact of how much of the interval we happened to see.
    coverage_from = _int(newer.get("coverage_from_ms"))
    if coverage_from is None:
        out["refusal"] = "coverage_unknown"
        return out
    if coverage_from > t0:
        gap_s = (coverage_from - t0) / 1000.0
        out["refusal"] = f"coverage_misses_first_{gap_s:.0f}s_of_interval"
        return out

    delta_lamports = sol1 - sol0
    delta_sol = Decimal(delta_lamports) / LAMPORTS_PER_SOL
    out["delta_sol"] = delta_sol
    if delta_lamports <= 0:
        # A curve that gave SOL back is a real observation and a real signal, but it is not
        # a velocity. Reporting a negative or zero rate would let a token that is being
        # sold off clear a "minimum velocity" floor on the wrong side of zero.
        out["refusal"] = f"no_sol_added:{delta_sol}"
        return out

    out["sol_per_min"] = delta_sol / (interval_s / Decimal(60))

    if trades < config.min_trades_for_rate:
        out["refusal"] = f"only_{trades}_trades_in_interval"
        out["basis"] = "sol_per_min"
        return out

    # Independent cross-check: the SOL the curve gained should look like the net SOL the
    # trades we collected moved. A large disagreement means we are missing trades, which is
    # precisely the failure the coverage rule exists to catch and the one case where
    # coverage bookkeeping can be wrong without anything else noticing.
    if net_lamports is not None and abs(delta_lamports) >= config.flow_check_floor_lamports:
        if net_lamports <= 0:
            out["refusal"] = "collected_flow_contradicts_curve"
            out["basis"] = "sol_per_min"
            return out
        ratio = Decimal(net_lamports) / Decimal(delta_lamports)
        out["flow_ratio"] = ratio
        if ratio < config.flow_ratio_min or ratio > config.flow_ratio_max:
            out["refusal"] = f"flow_cross_check_ratio_{ratio:.3f}"
            out["basis"] = "sol_per_min"
            return out

    out["sol_per_swap"] = delta_sol / Decimal(trades)
    out["basis"] = "sol_per_swap"
    return out


# --------------------------------------------------------------------------------------
# the one call the scanner makes
# --------------------------------------------------------------------------------------


def observe(
    chain: Chain,
    token: str,
    curve: Mapping[str, Any] | None,
    conn: sqlite3.Connection | None = None,
    *,
    at_ms: int | None = None,
    collect: bool = True,
    config: FlowConfig = DEFAULT_CONFIG,
    priority: Priority = Priority.DISCOVERY,
) -> tuple[dict[str, Any] | None, str]:
    """Collect this mint's trades, store a curve snapshot, and return the enriched curve.

    This is the whole module in one call, shaped like ``scanner.curve_from_payload`` so the
    scanner can use it on one line and the lane sees the same key names either way. It
    returns ``(curve, basis)`` where ``basis`` is one of ``sol_per_swap``, ``sol_per_min``
    or ``none``, and the returned curve is a **copy** — the caller's dict is not mutated.

    Two independent routes to ``sol_per_swap``, preferred in this order:

    1. **Cumulative from launch.** Once the trades for a young mint have been collected,
       our coverage starts at its creation, so total curve SOL divided by total trades is
       the published predictor computed on the whole population. This is the stronger
       number and it needs only one snapshot.
    2. **Between two snapshots.** For a mint we met late, the delta over an interval we can
       prove we covered. Weaker, because it is a window rather than the life of the token,
       but honest.

    A refusal is not a failure: ``basis`` of ``none`` with the reason recorded in
    ``velocity_refusal`` is the correct answer when the evidence is thin, and it is what
    keeps ``curve-velocity`` from firing on a rate that is wrong by two orders of magnitude.
    """
    if curve is None:
        return None, "none"
    conn = conn if conn is not None else get_conn()
    out = dict(curve)
    now = at_ms if at_ms is not None else (_int(curve.get("observed_ms")) or now_ms())
    out["observed_ms"] = now
    created_ms = _int(curve.get("created_ms"))

    previous = latest_snapshot(chain, token, conn, before_ms=now)
    since_ms = _int(previous.get("observed_ms")) if previous else created_ms

    flow: FlowResult | None = None
    if collect and chain is Chain.SOL:
        flow = collect_trades(
            chain,
            token,
            conn,
            since_ms=since_ms,
            created_ms=created_ms,
            max_pages=config.scan_max_pages,
            config=config,
            priority=priority,
        )
        out["flow_pages"] = flow.pages
        out["flow_complete"] = flow.complete
        out["flow_reason"] = flow.reason
        out["flow_rows_written"] = flow.rows_written

    coverage_from = flow.coverage_from_ms if flow is not None else None

    # Two routes to a trustworthy trade count, in decreasing order of directness.
    #
    # Direct: the collector walked this mint's trade history to its end, so what we hold is
    # every trade there has ever been. Proxy: the scanner's rule, which asks whether our
    # first swap row is close to the token's creation. The proxy is kept as the fallback
    # because it works without a collection, but it must not be the primary — it refuses a
    # token whose first trade was simply late, which on the live sample was 4 of 10.
    proved = (
        coverage_from is not None and created_ms is not None and coverage_from <= created_ms
    ) or launch_coverage_proved(chain, token, conn, created_ms=created_ms)
    if proved:
        count = swap_count(chain, token, conn)
        cumulative = count or None
        cumulative_basis = "collected_from_launch" if count else "no_swap_rows"
    else:
        cumulative, cumulative_basis = covered_swap_count(
            chain, token, conn, created_ms=created_ms, config=config
        )
    record_snapshot(
        chain,
        token,
        out,
        conn,
        trades_seen=cumulative,
        trades_basis=cumulative_basis,
        coverage_from_ms=coverage_from,
        config=config,
    )

    # Diagnostics, under keys no lane reads. See :func:`gross_flow` for why they exist.
    gross = gross_flow(chain, token, conn)
    if gross:
        out["buys_seen"] = gross["buys"]
        out["sells_seen"] = gross["sells"]
        out["sol_bought_gross"] = Decimal(gross["buy_lamports"]) / LAMPORTS_PER_SOL
        out["sol_sold_gross"] = Decimal(gross["sell_lamports"]) / LAMPORTS_PER_SOL
        out["sol_per_buy_gross"] = (
            Decimal(gross["buy_lamports"]) / LAMPORTS_PER_SOL / Decimal(gross["buys"])
            if gross["buys"] and not gross["unpriced"]
            else None
        )

    sol_in_curve = _dec(curve.get("sol_in_curve"))
    basis = "none"
    refusal: str | None = None

    # Route 1: the whole life of the token.
    if cumulative is None:
        refusal = cumulative_basis
    elif cumulative < config.min_trades_for_rate:
        refusal = f"only_{cumulative}_trades_since_launch"
    elif sol_in_curve is None:
        refusal = "sol_in_curve_unknown"
    elif sol_in_curve <= 0:
        refusal = "no_sol_in_curve"
    else:
        out["sol_per_swap"] = sol_in_curve / Decimal(cumulative)
        out["swaps"] = cumulative
        out["swaps_basis"] = cumulative_basis
        out["velocity_window"] = "since_launch"
        basis = "sol_per_swap"

    # Route 2: the interval between the previous snapshot and this one.
    if basis != "sol_per_swap" and previous is not None:
        window_trades, net_lamports = trades_between(
            chain,
            token,
            conn,
            after_ms=_int(previous.get("observed_ms")) or 0,
            until_ms=now,
        )
        newer = {
            "observed_ms": now,
            "real_sol_lamports": _int(curve.get("sol_in_curve_lamports")),
            "coverage_from_ms": coverage_from,
        }
        derived = velocity_between(
            previous, newer, trades=window_trades, net_lamports=net_lamports, config=config
        )
        out["velocity_window"] = "between_snapshots"
        out["velocity_interval_s"] = derived["interval_s"]
        out["velocity_delta_sol"] = derived["delta_sol"]
        if derived["sol_per_swap"] is not None:
            out["sol_per_swap"] = derived["sol_per_swap"]
            out["swaps"] = derived["trades"]
            out["swaps_basis"] = "delta_between_snapshots"
            basis = "sol_per_swap"
            refusal = None
        else:
            refusal = derived["refusal"] or refusal
            if derived["sol_per_min"] is not None:
                # The scanner's own per-minute figure divides by the token's whole age,
                # which understates a token that only started moving recently. A
                # snapshot-to-snapshot per-minute rate is the better of the two, so it
                # replaces it. Still the weak fallback, still marked as such.
                out["sol_per_min"] = derived["sol_per_min"]

    if basis != "sol_per_swap":
        # Never leave a bare ``swaps`` behind: ``lanes.curve_velocity`` divides the whole
        # curve SOL by it when ``sol_per_swap`` is absent, so a window count left in the
        # dict would be silently reinterpreted as a lifetime count.
        out.pop("swaps", None)
        out.pop("swaps_basis", None)
        if _dec(out.get("sol_per_min")) is not None:
            basis = "sol_per_min"

    out["velocity_refusal"] = refusal if basis != "sol_per_swap" else None
    return out, basis


__all__ = [
    "DEFAULT_CONFIG",
    "PROVIDER",
    "SOLANA_CHAIN_ID",
    "SOURCE",
    "TRADES_ENDPOINT",
    "FlowConfig",
    "FlowResult",
    "TradeRow",
    "collect_trades",
    "covered_swap_count",
    "fetch_trades_page",
    "gross_flow",
    "latest_snapshot",
    "launch_coverage_proved",
    "observe",
    "parse_trade",
    "prune_snapshots",
    "record_snapshot",
    "swap_count",
    "token_decimals",
    "trades_between",
    "velocity_between",
    "write_trades",
]
