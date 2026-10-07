"""Trusted and proven SOLANA wallets, written to ``swaps`` from an Alchemy WebSocket.

Why this exists. OWNER DECISION 2026-10-04: launch sniping on Solana should also fire when
really good wallets are among a new token's first buyers, and the trade feed is built on the
owner's paid Alchemy plan (no PumpPortal trade-stream key). ``execution/snipe.py``'s
``trusted_early`` rule reads early buyers from the ``swaps`` tape up to its decision moment;
on Solana nothing wrote a known-good wallet's buy of a brand-new token there in time. This is
the Solana twin of ``ingest/wallet_stream.py`` (Robinhood) and follows its conventions.

WHICH wallets lives only in the database: the tracked set is the sol ``wallets`` rows whose
``cohort`` is ``trusted_copy`` plus the newest frozen ``proven:sol`` cohort
(``learning.proven.proven_members``, the 72 h age limit ``lanes`` and ``snipe`` apply). No
address is written in this source (it is exported publicly; a test enforces it).

What it does, in order:

1. Subscribe (:func:`stream`) with ``logsSubscribe`` ``{"mentions": [wallet]}`` at
   ``confirmed``, ONE subscription per wallet (Solana accepts a single address per mentions
   filter). A notification is only a trigger: it carries the signature, the error flag and
   the program logs. A failed transaction is never read (its notification says so), and
   one whose complete logs invoke no swap program is not read either.
2. The transaction itself is the evidence: ``getTransaction`` (``jsonParsed``,
   ``maxSupportedTransactionVersion`` 0). For every tracked wallet in it, the NET change is
   derived from ``pre/postTokenBalances`` (by ``owner``) and ``pre/postBalances``, with the
   fee added back when the wallet paid it and WSOL folded into SOL -- the same derivation
   as ``ingest/backfill.py`` (``helius:backfill``), so the two on-chain sources agree.
3. Only a CONFIRMED swap is written (:func:`classify`): a pump.fun / pump AMM / Raydium /
   Meteora / Jupiter program in the transaction, exactly one non-quote mint moved for the
   wallet, and SOL/WSOL moved the opposite way by at least :data:`MIN_QUOTE_LAMPORTS`.
   A stablecoin-quoted trade, a multi-token route, an airdrop or a transfer is never written.
4. Units are those of the sol rows ``lanes.py`` and ``snipe.py`` read (MEASURED on the box
   2026-10-04 against ``pumpfun:trades`` and ``helius:backfill`` rows for the same
   transactions): ``amount_token`` token atoms, ``amount_native`` lamports, ``usd_value`` =
   lamports / 1e9 x SOL/USD from ``native_prices`` (``native_price.at``, 5 min tolerance),
   ``price_usd`` = USD per WHOLE token using the decimals the transaction itself reports,
   ``ts_ms`` the block time, ``slot`` the slot, ``block_index`` the transaction's index in
   the block, ``fee_payer`` the first signer, ``program`` the venue's program id (the
   ``helius:backfill`` spelling, NOT ``pumpfun:trades``' venue names: ``learning.replay``
   solves pool depth from rows with ``program = 'pump_amm'`` and these are wallet-side
   amounts). Where a value cannot be made consistent it is ``NULL``, never invented: no
   SOL/USD sample within tolerance -> ``usd_value`` and ``price_usd`` NULL.
   WHAT THE NUMBER INCLUDES: ``amount_native`` is what the wallet paid or received net, so
   it carries the venue's fees, any tip and token-account rent paid or refunded in the same
   transaction (as ``helius:backfill`` does); ``pumpfun:trades`` reports the curve's own
   amount. Same unit, same scale. MEASURED on 34 transactions both feeds booked:
   ``amount_token``, ``side``, ``slot``, ``block_index`` and ``ts_ms`` identical in 34/34;
   ``amount_native`` ours/theirs p50 1.0046 (p90 1.017); ``price_usd`` p50 0.9986.
5. Dedupe: a (tx, wallet, token) any feed already wrote is not written again
   (:func:`existing_rows`); the table's own UNIQUE key catches our own replays.
6. Emit ``wallet.trade`` with the row, exactly as the Robinhood twin does.

The tracked set is screened hourly, and again as soon as a tracked wallet's own swaps cross
the router rule (:meth:`SolWalletStream.screen`, at most every :data:`RESCREEN_MIN_S`), and
every wallet it removes is named in a ``system`` event with its evidence
(``status: excluded``):

* a BOT: at least :data:`BOT_MAX_TXS_PER_HOUR` successful transactions an hour over its
  newest :data:`SCREEN_SIGS` signatures (MEASURED 2026-10-04 on the 16 proven sol wallets,
  three reads: one ran 7,257-55,516/h; every other stayed under 500/h);
* a ROUTER: at least :data:`ROUTER_MIN_TXS` swap transactions it signed, of which
  :data:`ROUTER_ZERO_NET_SHARE` left it holding no new position (MEASURED the same day: one
  wallet netted zero on 30 of 30 of its swaps -- arbitrage, not conviction);
* MOSTLY FAILED (cost guard, the lead 2026-10-04): at least ``max_failed_share`` (0.8) of at
  least ``min_tx_for_failed_screen`` (50) of its newest signatures failed. A failed
  transaction carries no trade but every one is a push the plan bills by the byte. MEASURED
  2026-10-04: one proven wallet failed 89% of its transactions and made 69-80% of the reads
  and nearly all of the ~24 MB/h of pushes.

Between screens a hard per-wallet budget applies: a wallet whose pushes exceed
``max_notifications_per_wallet_per_hour`` (2,000) in the last hour is unsubscribed
(``logsUnsubscribe``) until the next screen, with a ``system`` event (``status: excluded``,
``reason: notification_budget``). Every threshold above is a param (:data:`DEFAULT_PARAMS`),
tunable without code under ``lanes.launch-snipe.params.sol_wallets`` in ``config/risk.yaml``
and re-read at every screen. The ``stats`` event carries each wallet's measured cost per hour
(:class:`WalletMeter`).

Known interactions with the other feeds (same shape as the Robinhood twin's):

* ``token_flow.write_trades`` skips any signature already present for a mint, so when this
  feed books a trade first, the later ``pumpfun:trades`` page does not add its own row for
  that signature: the tape keeps ours (wallet-side amounts) instead of the curve's.
* A GMGN row for the same trade written AFTER ours is not caught: its ``amount_token`` is in
  UI units, so the UNIQUE key differs. ``lanes._net_buyers`` keys by wallet, so the smart
  COUNT is unaffected; that wallet's ``buy_usd`` doubles.

What this module does NOT do: decide anything. The cohort a lane trusts is read by the lane
from the database, never from these rows, and the rows carry no cohort or tag claim.

Latency (MEASURED 2026-10-04 on the box, 14 tracked wallets, 40 min, 713 trades written):
block -> push p50 1.2 s / p90 1.6 s / max 2.1 s on a healthy connection. Block times are
whole seconds, so every block-relative figure overstates by up to 1 s. Alchemy connections
are NOT uniformly healthy: of four identical concurrent sockets two ran on time and two ran
p50 22.9 s behind (the same lag showed on an ``accountSubscribe`` socket in another run),
so the listener measures every live read's block->push delay and reconnects a lagging
connection (:class:`LagWatch`, :data:`LAG_RECONNECT_MS`); the reconnect backfills the gap.
MEASURED in a 20-min box run with the watchdog: the first four connections lagged (median
8.7-39.3 s) and each was replaced within about a minute; the fifth held for the remaining
17 minutes, and the run's block -> row was p50 1.7 s / p90 2.6 s over 437 live trades.

RPC. Reads go over plain HTTPS to the same Alchemy endpoint as the socket, NOT through the
shared ``rpc`` limiter bucket (that bucket carries sizing and protection reads). The URL
carries the API key; every string that can carry it goes through ``alchemy_ws.redact``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from collections import Counter, OrderedDict, deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from kaiba.core.db import fetch_all, fetch_one, get_conn
from kaiba.core.events import emit, emit_once
from kaiba.core.schemas import Chain, EventKind, Lane, now_ms
from kaiba.ingest import alchemy_ws as aws

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------------------
# identity
# --------------------------------------------------------------------------------------

CHAIN = Chain.SOL
#: ``swaps.source``, shared with the Robinhood twin. Not ``gmgn:``-prefixed: these rows are
#: base units (``grade.HUMAN_UNIT_SOURCE_PREFIXES`` reads that prefix as UI units).
SOURCE = "alchemy:ws"
#: The ``kaiba.ingest.runner`` registry name and ``ingest_status.feed``.
FEED = "sol_wallets"
TRUSTED_COHORT = "trusted_copy"
COMMITMENT = "confirmed"
#: ``lanes._proven_cohort`` / ``snipe.DEFAULT_PARAMS["proven_max_cohort_age_s"]`` (72 h).
PROVEN_MAX_AGE_S = 259_200.0

# --------------------------------------------------------------------------------------
# chain constants (program ids and mints; a test pins them to their other homes)
# --------------------------------------------------------------------------------------

WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
STABLE_MINTS: frozenset[str] = frozenset({USDC_MINT, USDT_MINT})
QUOTE_MINTS: frozenset[str] = frozenset({WSOL_MINT}) | STABLE_MINTS

#: The venues a confirmed swap must touch, with ``execution.policy.DEX_PROGRAMS``' names.
#: MEASURED 2026-10-04 in 160 sampled transactions of the proven sol wallets: pump AMM in
#: 69, Jupiter 27, pump.fun 24, Meteora DLMM 11, DAMM v2 5, DBC 1, Raydium CPMM 1, CLMM 1.
SWAP_PROGRAMS: dict[str, str] = {
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "pump.fun",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "pumpswap",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "raydium-amm-v4",
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": "raydium-cpmm",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "raydium-clmm",
    "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj": "raydium-launchlab",
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": "meteora-dlmm",
    "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB": "meteora-damm-v1",
    "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG": "meteora-damm-v2",
    "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN": "meteora-dbc",
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "jupiter-v6",
}

#: ``ingest/backfill.py``'s floor: below this the SOL leg is rent, a tip or rounding.
MIN_QUOTE_LAMPORTS = 10_000
LAMPORTS_PER_SOL = Decimal(10) ** 9

#: The tracked-set screen.
SCREEN_SIGS = 1000            # newest signatures read per wallet (one 40 CU call)
BOT_MAX_TXS_PER_HOUR = 1000   # see the module doc: the measured gap is ~500/h to 7,257+/h
BOT_MIN_SIGS = 50             # a rate from fewer signatures is not evidence
ROUTER_MIN_TXS = 20           # the Robinhood twin's rule
ROUTER_ZERO_NET_SHARE = 0.8
#: Newest unseen successful txs read per wallet per screen. Above ROUTER_MIN_TXS on purpose:
#: MEASURED 2026-10-04 (box smoke run) a 20-tx sample left a 100%-zero-net wallet at 18-19
#: pieces of evidence, under the rule's floor, and it stayed tracked at ~460 txs/h.
ROUTER_SAMPLE_TXS = 30
#: A tracked wallet that crosses the router rule mid-hour triggers a rescreen, at most this often.
RESCREEN_MIN_S = 300.0
EVIDENCE_KEEP = 200           # per wallet, newest first out
READ_KEEP = 20_000            # signatures remembered as already read (~1-2 days of reads)
REFRESH_S = 3600.0

#: Rows of the same (tx, wallet, token) another feed wrote are searched this far either
#: side of the block time (GMGN stamps the block second; its lag MEASURED p90 94 s).
DEDUPE_WINDOW_MS = 3_600_000
STATS_EVERY_S = 600.0
#: No-frame reconnect. The ping (20 s) catches a dead TCP path; this catches a socket that
#: pongs but whose subscriptions silently went away. A quiet wallet set must not flap.
IDLE_TIMEOUT_S = 1800.0
KV_RESUME = "ingest:sol_wallets:last_slot"
#: A stored resume point older than this many slots is ignored (~4-6 h of slots).
RESUME_MAX_SLOTS = 54_000
#: ``getSignaturesForAddress`` costs 40 CU whatever its ``limit`` (max 1000), so pages are
#: full. MEASURED 2026-10-04: a 100-signature page bound cut a 70 s reconnect gap short for a
#: wallet that sends ~2 transactions a second, most of them failing.
BACKFILL_PAGE = 1000
BACKFILL_MAX_PAGES = 3
#: ``getTransaction`` at ``confirmed`` can answer ``null`` for a moment after the push.
#: MEASURED 2026-10-04 (60 pushes): first answer p50 0.16 s, p90 0.67 s, max 0.82 s; 1 of
#: 60 never answered within 60 s although the signature was finalized. In a 20-min run 202
#: null answers came back on 543 reads and 9 reads never answered.
TX_RETRIES: tuple[float, ...] = (0.15, 0.3, 0.6, 1.2, 2.4, 4.8)
#: Transactions read at once. MEASURED 2026-10-04 (40-min box run): with ONE reader, each
#: missing transaction's retries held up everything queued behind it (block->row p90 8.2 s
#: against block->push p90 1.6 s).
WORKERS = 2
#: A connection is LAGGING when the median block->push delay of its last LAG_WINDOW live
#: reads exceeds LAG_RECONNECT_MS; the listener then reconnects, which backfills the gap.
#: MEASURED 2026-10-04 on the box: of four identical concurrent sockets two ran on time
#: (behind the earliest p90 0.01 s) and two ran p50 22.9 s / p90 39.5 s / max 46.8 s behind;
#: an on-time socket showed block->push p50 1.2 s, p90 1.6 s, max 2.1 s over 707 reads.
LAG_RECONNECT_MS = 8_000
LAG_WINDOW = 5

#: Alchemy compute units (alchemy.com/docs/reference/compute-unit-costs, read 2026-10-04):
#: Solana methods per call; Solana subscriptions per byte DELIVERED ("1 byte = .0002 CU").
#: A method not listed is counted at 40 so the estimate errs high.
CU_PER_CALL: dict[str, int] = {
    "getTransaction": 40, "getSignaturesForAddress": 40, "getSlot": 20, "getBlockTime": 20,
}
CU_PER_STREAM_BYTE = 0.0002
#: The rate Alchemy publishes for EVM subscriptions (``alchemy_ws.CU_PER_NOTIFICATION_BYTE``).
#: Reported beside the Solana estimate until the dashboard confirms which one is billed.
EVM_CU_PER_STREAM_BYTE = aws.CU_PER_NOTIFICATION_BYTE

#: Tunable thresholds. Overridden by ``lanes.launch-snipe.params.sol_wallets`` in
#: ``config/risk.yaml`` (the lane this feed serves), read by :func:`params` at every screen.
PARAMS_LANE = Lane.LAUNCH_SNIPE
PARAMS_KEY = "sol_wallets"
DEFAULT_PARAMS: dict[str, float | int] = {
    "max_failed_share": 0.8,
    "min_tx_for_failed_screen": 50,
    "max_notifications_per_wallet_per_hour": 2000,
    "bot_max_txs_per_hour": BOT_MAX_TXS_PER_HOUR,
    "bot_min_sigs": BOT_MIN_SIGS,
    "router_min_txs": ROUTER_MIN_TXS,
    "router_zero_net_share": ROUTER_ZERO_NET_SHARE,
    "lag_reconnect_ms": LAG_RECONNECT_MS,
    "lag_window": LAG_WINDOW,
}
#: Every param and what would settle it. A test pins that each one has an entry.
PARAMS_PROVENANCE: dict[str, str] = {
    "max_failed_share": "LEAD 2026-10-04 (cost guard). MEASURED: one proven wallet failed 89% and drove 69-80% of reads.",
    "min_tx_for_failed_screen": "LEAD 2026-10-04: a share from fewer transactions is not evidence.",
    "max_notifications_per_wallet_per_hour": "LEAD 2026-10-04 (hard budget). 0 = off. MEASURED: the 89%-failed wallet pushed ~8,200/h.",
    "bot_max_txs_per_hour": "MEASURED 2026-10-04: proven sol wallets ran <500/h except one at 7,257-55,516/h.",
    "bot_min_sigs": "DERIVED: a rate from fewer signatures is not evidence.",
    "router_min_txs": "The Robinhood twin's rule (wallet_stream.ROUTER_MIN_TXS).",
    "router_zero_net_share": "The Robinhood twin's rule; MEASURED 2026-10-04 one sol wallet netted zero on 30/30 swaps.",
    "lag_reconnect_ms": "MEASURED 2026-10-04: healthy sockets block->push max 2.1 s; lagging sockets p50 22.9 s.",
    "lag_window": "DERIVED: five live reads, so one late push never reconnects a healthy socket.",
}
#: Params that are a share: anything outside [0, 1] is refused.
_SHARE_PARAMS = frozenset({"max_failed_share", "router_zero_net_share"})


def params(cfg: Any = None) -> dict[str, float | int]:
    """:data:`DEFAULT_PARAMS` overlaid with ``lanes.launch-snipe.params.sol_wallets``.

    Unknown keys and unusable values are logged and ignored (the default stays), never
    raised: a typo in the operator's file must not take the feed down. Never raises.
    """
    out: dict[str, float | int] = dict(DEFAULT_PARAMS)
    try:
        if cfg is None:
            from kaiba.core.config import get_risk

            cfg = get_risk()
        raw = (cfg.lane(PARAMS_LANE).params or {}).get(PARAMS_KEY)
    except Exception as exc:  # noqa: BLE001 - defaults are the safe reading
        log.debug("sol_wallets: params unreadable, defaults used (%s)", exc)
        return out
    if not isinstance(raw, Mapping):
        return out
    for key, value in raw.items():
        if key not in DEFAULT_PARAMS:
            log.warning("sol_wallets: unknown param %r ignored", key)
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            log.warning("sol_wallets: param %s=%r is not a number; default kept", key, value)
            continue
        if number != number or number < 0 or (key in _SHARE_PARAMS and number > 1):
            log.warning("sol_wallets: param %s=%r out of range; default kept", key, value)
            continue
        out[key] = int(number) if isinstance(DEFAULT_PARAMS[key], int) else number
    return out

_B58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_INVOKE = re.compile(r"^Program (\S+) invoke \[\d+\]$")

# --------------------------------------------------------------------------------------
# small pure helpers
# --------------------------------------------------------------------------------------


def is_sol_address(value: Any) -> bool:
    return isinstance(value, str) and bool(_B58.match(value.strip()))


def normalize_wallets(wallets: Iterable[Any]) -> tuple[str, ...]:
    """De-duplicated, sorted base58 addresses. Case is significant on Solana and kept."""
    return tuple(sorted({w.strip() for w in wallets if is_sol_address(w)}))


def _int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def account_keys(tx: Mapping[str, Any]) -> list[str]:
    """The full account list in balance order. ``jsonParsed`` already appends the
    lookup-table addresses; a ``json`` payload carries them in ``meta.loadedAddresses``."""
    msg = ((tx.get("transaction") or {}).get("message") or {}) if isinstance(tx, Mapping) else {}
    keys = [str(k.get("pubkey") if isinstance(k, Mapping) else k) for k in msg.get("accountKeys") or []]
    meta = tx.get("meta") or {}
    loaded = meta.get("loadedAddresses") if isinstance(meta, Mapping) else None
    pre = meta.get("preBalances") if isinstance(meta, Mapping) else None
    if isinstance(loaded, Mapping) and isinstance(pre, list) and len(keys) < len(pre):
        keys += [str(a) for a in loaded.get("writable") or []] + [str(a) for a in loaded.get("readonly") or []]
    return keys


def signers(tx: Mapping[str, Any]) -> list[str]:
    """Signers in order (``jsonParsed`` marks them); the first one paid the fee."""
    msg = (tx.get("transaction") or {}).get("message") or {}
    out = [str(k.get("pubkey")) for k in msg.get("accountKeys") or [] if isinstance(k, Mapping) and k.get("signer")]
    if out:
        return out
    keys = account_keys(tx)
    return keys[:1]


def programs_in(tx: Mapping[str, Any]) -> list[str]:
    """Every program the transaction ran, top level first, then inner, in order, once each."""
    msg = (tx.get("transaction") or {}).get("message") or {}
    keys = account_keys(tx)
    seen: list[str] = []

    def add(ix: Any) -> None:
        if not isinstance(ix, Mapping):
            return
        pid = ix.get("programId")
        if pid is None and isinstance(ix.get("programIdIndex"), int) and ix["programIdIndex"] < len(keys):
            pid = keys[ix["programIdIndex"]]
        if pid and str(pid) not in seen:
            seen.append(str(pid))

    for ix in msg.get("instructions") or []:
        add(ix)
    for inner in (tx.get("meta") or {}).get("innerInstructions") or []:
        for ix in (inner or {}).get("instructions") or []:
            add(ix)
    return seen


def swap_program(tx: Mapping[str, Any]) -> str | None:
    """The first known venue the transaction ran (top level before inner), else ``None``."""
    return next((p for p in programs_in(tx) if p in SWAP_PROGRAMS), None)


def logs_mention_swap(logs: Any) -> bool | None:
    """From a notification's program logs: ``True``/``False`` whether a venue was invoked,
    ``None`` when the logs cannot say (absent, empty, or truncated by the node)."""
    if not isinstance(logs, (list, tuple)) or not logs:
        return None
    hit = False
    for line in logs:
        text = str(line)
        if "log truncated" in text.lower():
            return None
        m = _INVOKE.match(text)
        if m and m.group(1) in SWAP_PROGRAMS:
            hit = True
    return hit


@dataclass(frozen=True, slots=True)
class WalletDelta:
    """One wallet's net movement in one transaction."""

    native: int | None              # lamports, before the fee add-back; None = not an account here
    tokens: dict[str, int]          # mint -> atoms, by token-account OWNER
    decimals: dict[str, int]
    fee_payer: bool
    signer: bool
    touched: bool                   # owns a token account the transaction listed


def wallet_delta(tx: Mapping[str, Any], wallet: str) -> WalletDelta:
    meta = tx.get("meta") or {}
    keys = account_keys(tx)
    pre_b, post_b = meta.get("preBalances") or [], meta.get("postBalances") or []
    native: int | None = None
    for i, k in enumerate(keys):
        if k == wallet and i < len(pre_b) and i < len(post_b):
            a, b = _int(pre_b[i]), _int(post_b[i])
            if a is not None and b is not None:
                native = (native or 0) + (b - a)
    tokens: dict[str, int] = {}
    decimals: dict[str, int] = {}
    touched = False
    for key, sign in (("preTokenBalances", -1), ("postTokenBalances", 1)):
        for bal in meta.get(key) or []:
            if not isinstance(bal, Mapping) or bal.get("owner") != wallet:
                continue
            ui = bal.get("uiTokenAmount") or {}
            amount = _int(ui.get("amount")) if isinstance(ui, Mapping) else None
            mint = str(bal.get("mint") or "")
            if not mint or amount is None:
                continue
            touched = True
            tokens[mint] = tokens.get(mint, 0) + sign * amount
            dec = _int(ui.get("decimals"))
            if dec is not None:
                decimals[mint] = dec
    sig = signers(tx)
    return WalletDelta(native=native, tokens=tokens, decimals=decimals,
                       fee_payer=bool(sig) and sig[0] == wallet, signer=wallet in sig, touched=touched)


@dataclass(frozen=True, slots=True)
class Leg:
    """A confirmed swap: one token against SOL, net, for one wallet."""

    token: str
    side: str
    atoms: int
    lamports: int
    decimals: int | None
    program: str


def classify(tx: Mapping[str, Any], wallet: str) -> tuple[Leg | None, str]:
    """``(leg, "ok")`` for a confirmed SOL swap, else ``(None, reason)``. Pure; never raises.

    The fee is added back when the wallet paid it, so the figure is the swap, not the swap
    plus network overhead; WSOL is SOL (``ingest/backfill.py``'s derivation).
    """
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return None, "failed_transaction"
    d = wallet_delta(tx, wallet)
    if d.native is None and not d.touched:
        return None, "not_a_party"
    quote = (d.native or 0) + ((_int(meta.get("fee")) or 0) if d.fee_payer else 0)
    tokens = dict(d.tokens)
    quote += tokens.pop(WSOL_MINT, 0)
    base = {m: a for m, a in tokens.items() if a and m not in QUOTE_MINTS}
    stables = {m: a for m, a in tokens.items() if a and m in STABLE_MINTS}
    if not base:
        return None, "no_position_leg"
    if len(base) > 1:
        return None, "multi_leg_route"
    program = swap_program(tx)
    if program is None:
        return None, "no_swap_program"
    (token, atoms), = base.items()
    side = "buy" if atoms > 0 else "sell"
    sol_moved = abs(quote) >= MIN_QUOTE_LAMPORTS
    if stables:
        # A stablecoin leg: the SOL (if any) cannot be attributed to the token alone, and a
        # stable-quoted trade has no SOL leg at all. Neither is written.
        return None, "mixed_quote_legs" if sol_moved else "stable_quoted"
    if not sol_moved or (quote > 0) == (atoms > 0):
        return None, "no_opposite_sol"
    return Leg(token=token, side=side, atoms=abs(atoms), lamports=abs(quote),
               decimals=d.decimals.get(token), program=program), "ok"


def zero_net(tx: Mapping[str, Any], wallet: str) -> bool | None:
    """Router evidence: did a swap this wallet SIGNED leave it holding no new position?

    ``None`` when the transaction is not evidence either way (failed, not signed by the
    wallet, no venue, no token account of the wallet's in it).
    """
    if (tx.get("meta") or {}).get("err") is not None or swap_program(tx) is None:
        return None
    d = wallet_delta(tx, wallet)
    if not d.signer or not d.touched:
        return None
    return all(a == 0 for m, a in d.tokens.items() if m not in QUOTE_MINTS)


def classify_routers(
    evidence: Mapping[str, Mapping[str, bool]],
    *,
    min_txs: int = ROUTER_MIN_TXS,
    share: float = ROUTER_ZERO_NET_SHARE,
) -> frozenset[str]:
    out: set[str] = set()
    for wallet, txs in evidence.items():
        n = len(txs)
        if n >= max(1, int(min_txs)) and sum(1 for v in txs.values() if v) / n >= share:
            out.add(wallet)
    return frozenset(out)


def router_evidence(evidence: Mapping[str, Mapping[str, bool]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for wallet, txs in evidence.items():
        n = len(txs)
        z = sum(1 for v in txs.values() if v)
        out[wallet] = {"swap_txs": n, "zero_net": z, "zero_net_share": round(z / n, 4) if n else None}
    return out


def signature_rate(sigs: Sequence[Mapping[str, Any]], *, now_s: float) -> dict[str, Any]:
    """Bot evidence from one ``getSignaturesForAddress`` page (newest first)."""
    times = [int(s["blockTime"]) for s in sigs if isinstance(s, Mapping) and _int(s.get("blockTime"))]
    ok = sum(1 for s in sigs if isinstance(s, Mapping) and s.get("err") is None)
    n = len(sigs)
    span_s = max(1.0, now_s - min(times)) if times else None
    return {
        "sigs": n,
        "ok": ok,
        "failed": n - ok,
        "failed_share": round((n - ok) / n, 4) if n else None,
        "span_h": round(span_s / 3600, 3) if span_s else None,
        "ok_per_hour": round(ok * 3600 / span_s, 1) if span_s else None,
    }


def is_bot(rate: Mapping[str, Any], *, max_per_hour: float = BOT_MAX_TXS_PER_HOUR,
           min_sigs: int = BOT_MIN_SIGS) -> bool:
    per_h = rate.get("ok_per_hour")
    return bool(per_h is not None and int(rate.get("sigs") or 0) >= min_sigs and per_h >= max_per_hour)


def mostly_failed(rate: Mapping[str, Any], *, max_share: float = float(DEFAULT_PARAMS["max_failed_share"]),
                  min_tx: int = int(DEFAULT_PARAMS["min_tx_for_failed_screen"])) -> bool:
    """At least ``max_share`` of at least ``min_tx`` signatures failed (the cost guard)."""
    n = int(rate.get("sigs") or 0)
    failed = int(rate.get("failed") or 0)
    return bool(n > 0 and n >= max(1, int(min_tx)) and failed / n >= float(max_share))


class NotificationBudget:
    """Pushes per wallet over the last hour, in one-minute buckets. ``limit`` 0 = off."""

    def __init__(self, limit_per_hour: int = int(DEFAULT_PARAMS["max_notifications_per_wallet_per_hour"])) -> None:
        self.limit = int(limit_per_hour)
        self._buckets: dict[str, deque[list[int]]] = {}

    def add(self, wallet: str, now_s: float) -> int:
        """Count one push; returns the wallet's pushes over the last 60 minutes."""
        minute = int(now_s // 60)
        b = self._buckets.setdefault(wallet, deque())
        if b and b[-1][0] == minute:
            b[-1][1] += 1
        else:
            b.append([minute, 1])
        while b and b[0][0] <= minute - 60:
            b.popleft()
        return sum(c for _, c in b)

    def over(self, count: int) -> bool:
        return self.limit > 0 and count > self.limit


class WalletMeter:
    """Each wallet's cost since the last snapshot: pushes and their bytes, and the RPC calls
    made on its behalf (reads of its transactions, screen and backfill signature reads)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.since = clock()
        self.notices: Counter[str] = Counter()
        self.failed: Counter[str] = Counter()
        self.bytes: Counter[str] = Counter()
        self.calls: dict[str, Counter[str]] = {}

    def note_notice(self, wallet: str, *, failed: bool, nbytes: int) -> None:
        self.notices[wallet] += 1
        self.bytes[wallet] += int(nbytes)
        if failed:
            self.failed[wallet] += 1

    def note_call(self, wallet: str | None, method: str) -> None:
        if wallet:
            self.calls.setdefault(wallet, Counter())[method] += 1

    def snapshot(self, *, reset: bool = True) -> dict[str, dict[str, Any]]:
        """Per wallet, per hour over the window since the last reset."""
        hours = max(1e-9, (self.clock() - self.since) / 3600)
        out: dict[str, dict[str, Any]] = {}
        for wallet in sorted(set(self.notices) | set(self.calls)):
            calls = self.calls.get(wallet, Counter())
            call_cu = sum(CU_PER_CALL.get(m, 40) * n for m, n in calls.items())
            nbytes = self.bytes[wallet]
            out[wallet] = {
                "notifications_per_hour": round(self.notices[wallet] / hours, 1),
                "failed_per_hour": round(self.failed[wallet] / hours, 1),
                "reads_per_hour": round(calls.get("getTransaction", 0) / hours, 1),
                "stream_bytes_per_hour": round(nbytes / hours),
                "est_cu_per_hour": round((call_cu + nbytes * CU_PER_STREAM_BYTE) / hours, 1),
                "est_cu_per_hour_at_evm_byte_rate": round((call_cu + nbytes * EVM_CU_PER_STREAM_BYTE) / hours, 1),
            }
        if reset:
            self.notices.clear()
            self.failed.clear()
            self.bytes.clear()
            self.calls.clear()
            self.since = self.clock()
        return out


def price_fields(*, lamports: int | None, atoms: int, decimals: int | None,
                 sol_usd: Decimal | None) -> tuple[str | None, str | None]:
    """``(usd_value, price_usd)``: USD, and USD per WHOLE token. Either is ``None`` when its
    inputs are unknown; never 0 for unknown (``lanes._net_buyers`` would read a manufactured
    number as conviction)."""
    if lamports is None or lamports <= 0 or sol_usd is None or sol_usd <= 0:
        return None, None
    usd = Decimal(lamports) / LAMPORTS_PER_SOL * sol_usd
    price: Decimal | None = None
    if decimals is not None and 0 <= decimals <= 36 and atoms > 0:
        try:
            price = usd / (Decimal(atoms) / (Decimal(10) ** int(decimals)))
        except (InvalidOperation, ZeroDivisionError):
            price = None
    return str(usd), (str(price) if price is not None else None)


# --------------------------------------------------------------------------------------
# the tape
# --------------------------------------------------------------------------------------


def trusted_wallets(conn: Any) -> frozenset[str]:
    """Sol wallets the DATABASE puts in ``trusted_copy``. Never a feed's claim."""
    rows = fetch_all(conn, "SELECT address FROM wallets WHERE chain = ? AND cohort = ?",
                     (CHAIN.value, TRUSTED_COHORT))
    return frozenset(normalize_wallets(r["address"] for r in rows))


def proven_wallets(conn: Any, *, at_ms: int | None = None, max_age_s: float = PROVEN_MAX_AGE_S) -> frozenset[str]:
    """The newest frozen ``proven:sol`` cohort if it is younger than ``max_age_s``, else none."""
    try:
        from kaiba.learning.proven import proven_members

        cohort = proven_members(conn, CHAIN, max_age_s=float(max_age_s), at_ms=at_ms)
    except Exception as exc:  # noqa: BLE001 - a missing cohort is no cohort, never a crash
        log.warning("sol_wallets: proven cohort unreadable (%s)", type(exc).__name__)
        return frozenset()
    return frozenset(normalize_wallets(cohort.members)) if cohort is not None else frozenset()


def candidate_set(conn: Any, *, at_ms: int | None = None) -> tuple[str, ...]:
    """``trusted_copy`` sol wallets plus the newest proven sol cohort, before the screen."""
    return tuple(sorted(trusted_wallets(conn) | proven_wallets(conn, at_ms=at_ms)))


def tracked_set(conn: Any, *, excluded: Iterable[str] = (), at_ms: int | None = None) -> tuple[str, ...]:
    drop = frozenset(excluded)
    return tuple(w for w in candidate_set(conn, at_ms=at_ms) if w not in drop)


def existing_rows(conn: Any, *, wallet: str, token: str, tx: str, ts_ms: int,
                  window_ms: int = DEDUPE_WINDOW_MS) -> list[dict[str, Any]]:
    """Rows any feed already wrote for this (tx, wallet, token). Seeks ``idx_swaps_wallet``
    over a bounded window, never a scan; signatures compare exactly (base58 is case-sensitive)."""
    rows = fetch_all(
        conn,
        "SELECT source, side, amount_token, tx FROM swaps "
        "WHERE chain = ? AND wallet = ? AND ts_ms BETWEEN ? AND ? AND token = ?",
        (CHAIN.value, wallet, int(ts_ms) - int(window_ms), int(ts_ms) + int(window_ms), token),
    )
    return [r for r in rows if str(r.get("tx") or "") == tx]


_INSERT = (
    "INSERT OR IGNORE INTO swaps (chain, tx, slot, block_index, ts_ms, wallet, token, side, "
    "amount_token, amount_native, amount_quote, quote_mint, price_usd, usd_value, program, source, fee_payer) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)
ROW_KEYS: tuple[str, ...] = (
    "chain", "tx", "slot", "block_index", "ts_ms", "wallet", "token", "side", "amount_token",
    "amount_native", "amount_quote", "quote_mint", "price_usd", "usd_value", "program", "source", "fee_payer",
)


def write_trade(conn: Any, row: Mapping[str, Any], extra: Mapping[str, Any] | None = None) -> bool:
    """Insert one ``swaps`` row and emit ``wallet.trade``. False if the row was already there."""
    cur = conn.execute(_INSERT, tuple(row[k] for k in ROW_KEYS))
    if not cur.rowcount:
        return False
    emit_once(
        EventKind.WALLET_TRADE,
        {**dict(row), **dict(extra or {})},
        chain=CHAIN,
        subject=row["wallet"],
        dedupe_key=(
            f"{EventKind.WALLET_TRADE.value}:{SOURCE}:{row['tx']}:{row['wallet']}:"
            f"{row['token']}:{row['side']}"
        ),
        conn=conn,
    )
    return True


# --------------------------------------------------------------------------------------
# RPC
# --------------------------------------------------------------------------------------


class RpcFailure(RuntimeError):
    """A read failed. The message is already redacted."""


RpcCall = Callable[[str, list[Any]], Awaitable[Any]]

_ERR_CODE = re.compile(r"""['"]code['"]:\s*(-?\d+)""")
_ERR_HTTP = re.compile(r"http (\d{3})")


def error_key(exc: BaseException) -> str:
    """A short, secret-free bucket for an RPC failure: ``http:429``, ``code:-32005``, a type."""
    text = str(exc)
    m = _ERR_HTTP.search(text)
    if m:
        return f"http:{m.group(1)}"
    m = _ERR_CODE.search(text)
    if m:
        return f"code:{m.group(1)}"
    head = text.split(":", 2)
    return head[1].strip()[:40] if len(head) > 1 else type(exc).__name__


class SolRpc:
    """One JSON-RPC call per request over HTTPS, counted per method for the CU estimate."""

    def __init__(self, url: str, *, timeout_s: float = 15.0, client: Any = None) -> None:
        self.url = url
        self.timeout_s = timeout_s
        self._client = client
        self.calls: Counter[str] = Counter()

    async def _http(self) -> Any:
        if self._client is None:
            import httpx  # lazy: importable without the extra

            self._client = httpx.AsyncClient(timeout=self.timeout_s)
        return self._client

    async def call(self, method: str, params: list[Any]) -> Any:
        self.calls[method] += 1
        try:
            client = await self._http()
            resp = await client.post(self.url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        except Exception as exc:  # noqa: BLE001 - every failure is one redacted error
            raise RpcFailure(aws.redact(f"{method}: {type(exc).__name__}: {exc}", self.url)) from None
        status = getattr(resp, "status_code", 200)
        if status != 200:
            raise RpcFailure(f"{method}: http {status}")
        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise RpcFailure(aws.redact(f"{method}: {type(exc).__name__}: {exc}", self.url)) from None
        if not isinstance(data, Mapping):
            raise RpcFailure(aws.redact(f"{method}: non-object answer {str(data)[:200]}", self.url))
        if data.get("error") is not None:
            raise RpcFailure(aws.redact(f"{method}: {data['error']}", self.url))
        return data.get("result")

    def estimated_cu(self) -> int:
        return sum(CU_PER_CALL.get(m, 40) * n for m, n in self.calls.items())

    async def close(self) -> None:
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.aclose()
            self._client = None


# --------------------------------------------------------------------------------------
# the socket
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Notice:
    """One signature that mentioned a tracked wallet: pushed, or read back to fill a gap."""

    signature: str
    wallet: str
    slot: int | None
    failed: bool
    logs: tuple[str, ...] | None
    recv_ms: int
    backfilled: bool = False
    block_time_ms: int | None = None
    #: Which connection of this stream pushed it (``SolFeedStats.connects`` at the time).
    conn: int = 0
    #: Bytes of the push as received (0 for a backfilled signature).
    nbytes: int = 0


@dataclass
class SolFeedStats:
    """What the connection did. Bytes are the pushes received, which is what Alchemy bills."""

    connects: int = 0
    disconnects: int = 0
    subscribe_calls: int = 0
    notifications: int = 0
    notification_bytes: int = 0
    failed: int = 0
    duplicates: int = 0
    undecoded: int = 0
    unknown_subscription: int = 0
    backfilled: int = 0
    gaps_truncated: int = 0
    lag_reconnects: int = 0
    unsubscribes: int = 0
    muted_dropped: int = 0
    per_wallet: dict[str, int] = field(default_factory=dict)

    def estimated_cu(self) -> float:
        return self.notification_bytes * CU_PER_STREAM_BYTE

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["per_wallet"] = len(self.per_wallet)
        d["estimated_cu"] = round(self.estimated_cu(), 2)
        return d


async def signatures_since(rpc: RpcCall, wallet: str, start_slot: int, *, page: int = BACKFILL_PAGE,
                           max_pages: int = BACKFILL_MAX_PAGES,
                           commitment: str = COMMITMENT) -> tuple[list[dict[str, Any]], bool]:
    """Successful signatures of ``wallet`` at or after ``start_slot``, OLDEST first, and whether
    the walk reached ``start_slot`` (``False`` = the page bound cut it: a gap was left)."""
    out: list[dict[str, Any]] = []
    before: str | None = None
    for _ in range(max(1, int(max_pages))):
        opts: dict[str, Any] = {"limit": int(page), "commitment": commitment}
        if before:
            opts["before"] = before
        got = await rpc("getSignaturesForAddress", [wallet, opts])
        if not isinstance(got, list) or not got:
            return out[::-1], True
        for s in got:
            if not isinstance(s, Mapping):
                continue
            slot = _int(s.get("slot"))
            if slot is not None and slot < start_slot:
                return out[::-1], True
            if s.get("err") is None and isinstance(s.get("signature"), str):
                out.append(dict(s))
        if len(got) < int(page):
            return out[::-1], True
        last = got[-1]
        before = str(last.get("signature")) if isinstance(last, Mapping) else None
        if not before:
            return out[::-1], True
    return out[::-1], False


class LaggingConnection(RuntimeError):
    """The listener measured this connection's pushes arriving too late; reconnect."""


class LagWatch:
    """Median block->push delay of the current connection's last ``window`` live reads.

    Reads from an older connection (still in the queue after a reconnect) are ignored, and
    the window restarts on every new connection, so one slow backend gives one verdict.
    """

    def __init__(self, *, limit_ms: int = LAG_RECONNECT_MS, window: int = LAG_WINDOW) -> None:
        self.limit_ms = int(limit_ms)
        self.window = max(1, int(window))
        self.conn: int | None = None
        self.lags: deque[int] = deque(maxlen=self.window)

    def add(self, conn: int, lag_ms: int) -> int | None:
        """The window's median when this connection is lagging (the window then restarts), else None."""
        if self.conn is not None and conn < self.conn:
            return None
        if conn != self.conn:
            self.conn = conn
            self.lags.clear()
        self.lags.append(int(lag_ms))
        if len(self.lags) < self.window:
            return None
        median = sorted(self.lags)[len(self.lags) // 2]
        if median <= self.limit_ms:
            return None
        self.lags.clear()
        return median


class ResumePoint:
    """The slot a restart must backfill from: the oldest slot still queued or being read,
    else the newest slot read. With several readers the newest slot read can be ahead of
    one still in flight; saving it would let a crash skip that one."""

    def __init__(self) -> None:
        self.waiting: Counter[int] = Counter()
        self.done_max: int | None = None

    def queued(self, slot: int | None) -> None:
        if slot is not None:
            self.waiting[int(slot)] += 1

    def finished(self, slot: int | None) -> None:
        if slot is None:
            return
        slot = int(slot)
        if self.waiting[slot] <= 1:
            self.waiting.pop(slot, None)
        else:
            self.waiting[slot] -= 1
        self.done_max = slot if self.done_max is None else max(self.done_max, slot)

    def value(self) -> int | None:
        lo = min(self.waiting) if self.waiting else None
        vals = [v for v in (lo, self.done_max) if v is not None]
        return min(vals) if vals else None


async def _slot(rpc: RpcCall) -> int | None:
    try:
        return _int(await rpc("getSlot", [{"commitment": COMMITMENT}]))
    except RpcFailure:
        return None


async def stream(
    url: str,
    wallets: Iterable[Any],
    *,
    rpc: RpcCall,
    stop: asyncio.Event | None = None,
    connect: Callable[[str], Any] | None = None,
    stats: SolFeedStats | None = None,
    on_status: Callable[[dict[str, Any]], None] | None = None,
    max_attempts: int | None = None,
    idle_timeout_s: float = IDLE_TIMEOUT_S,
    poll_s: float = 1.0,
    from_slot: int | None = None,
    anchor_on_subscribe: bool = True,
    backfill_page: int = BACKFILL_PAGE,
    backfill_max_pages: int = BACKFILL_MAX_PAGES,
    backfill_max_slots: int = RESUME_MAX_SLOTS,
    dedupe_size: int = 20_000,
    commitment: str = COMMITMENT,
    reconnect: asyncio.Event | None = None,
    muted: set[str] | None = None,
    meter: WalletMeter | None = None,
) -> AsyncIterator[Notice]:
    """Yield a :class:`Notice` per (signature, tracked wallet) until ``stop``.

    Each connection subscribes ``logsSubscribe`` once per wallet, then -- when it has a
    point to resume from (``from_slot`` on the first connection, the last slot seen on a
    reconnect) -- reads every wallet's signatures since that slot over ``rpc`` and yields
    them as ``backfilled``, then reads pushes. The subscription comes first so nothing falls
    between the two; a bounded set drops the overlap. ``anchor_on_subscribe`` reads the
    slot after every subscribe so a quiet set still has a resume point (the Robinhood
    twin's MEASURED fix). Any failure is a reconnect after ``alchemy_ws.backoff_delay``;
    ``max_attempts`` bounds CONSECUTIVE failures. A gap longer than ``backfill_max_slots``,
    or one a wallet's page bound cuts short, is reported (``gap_truncated``), never skipped
    silently. ``reconnect``, when set (the listener's :class:`LagWatch` verdict), drops the
    current connection and reconnects through the same backoff and backfill. A wallet the
    listener adds to ``muted`` is unsubscribed (``logsUnsubscribe``) on the live connection,
    not subscribed on a new one, and its in-flight pushes are counted and dropped. ``meter``
    receives every push's size and every backfill read, per wallet.
    """
    target = aws.ws_url(url)
    stop = stop or asyncio.Event()
    dial = connect or aws._default_connect  # noqa: SLF001 - the shared dialer, by design
    stats = stats or SolFeedStats()
    watch = normalize_wallets(wallets)
    if not watch:
        raise ValueError("no valid Solana wallet addresses to track")
    seen = aws._RecentKeys(dedupe_size)  # noqa: SLF001
    last_slot: int | None = int(from_slot) if from_slot is not None else None
    attempt = 0

    while not stop.is_set():
        if max_attempts is not None and attempt >= max_attempts:
            aws._status(on_status, "gave_up", target, attempts=attempt)  # noqa: SLF001
            break
        try:
            async with dial(target) as ws:
                session = aws._Session(ws, stats, target)  # noqa: SLF001
                by_id: dict[int, str] = {}
                sub_of: dict[str, int] = {}
                for w in watch:
                    if muted and w in muted:
                        continue
                    sub_id = await session.call("logsSubscribe", [{"mentions": [w]}, {"commitment": commitment}])
                    stats.subscribe_calls += 1
                    if isinstance(sub_id, bool) or not isinstance(sub_id, int):
                        raise aws.RpcError(f"logsSubscribe returned {sub_id!r}")
                    by_id[sub_id] = w
                    sub_of[w] = sub_id
                stats.connects += 1
                attempt = 0
                aws._status(on_status, "subscribed", target, subscriptions=len(by_id),  # noqa: SLF001
                            wallets=len(watch), connects=stats.connects)

                head: int | None = None
                if last_slot is not None:
                    head = await _slot(rpc)
                    if head is not None and head >= last_slot:
                        start = last_slot  # inclusive: a slot can hold signatures not yet seen
                        if head - start > backfill_max_slots:
                            stats.gaps_truncated += 1
                            aws._status(on_status, "gap_truncated", target, from_slot=start, head=head,  # noqa: SLF001
                                        lost_slots=head - backfill_max_slots - start)
                            start = head - backfill_max_slots
                        got = 0
                        for w in watch:
                            if muted and w in muted:
                                continue

                            async def counted(method: str, args: list[Any], _w: str = w) -> Any:
                                if meter is not None:
                                    meter.note_call(_w, method)
                                return await rpc(method, args)

                            try:
                                sigs, complete = await signatures_since(counted, w, start, page=backfill_page,
                                                                        max_pages=backfill_max_pages,
                                                                        commitment=commitment)
                            except RpcFailure as exc:
                                stats.gaps_truncated += 1
                                aws._status(on_status, "gap_truncated", target, wallet=w,  # noqa: SLF001
                                            reason=str(exc)[:160])
                                continue
                            if not complete:
                                stats.gaps_truncated += 1
                                aws._status(on_status, "gap_truncated", target, wallet=w, from_slot=start,  # noqa: SLF001
                                            reason="page_bound")
                            at = aws._now_ms()  # noqa: SLF001
                            for s in sigs:
                                bt = _int(s.get("blockTime"))
                                n = Notice(signature=str(s["signature"]), wallet=w, slot=_int(s.get("slot")),
                                           failed=False, logs=None, recv_ms=at, backfilled=True,
                                           block_time_ms=bt * 1000 if bt else None, conn=stats.connects)
                                if not seen.add((n.signature, w)):
                                    stats.duplicates += 1
                                    continue
                                stats.backfilled += 1
                                got += 1
                                yield n
                        aws._status(on_status, "backfilled", target, from_slot=start, to_slot=head,  # noqa: SLF001
                                    records=got)

                if anchor_on_subscribe:
                    if head is None:
                        head = await _slot(rpc)
                    if head is not None:
                        last_slot = head if last_slot is None else max(last_slot, head)

                last_frame = time.monotonic()
                if reconnect is not None:
                    reconnect.clear()  # a verdict about the previous connection is spent
                while not stop.is_set():
                    if reconnect is not None and reconnect.is_set():
                        reconnect.clear()
                        stats.lag_reconnects += 1
                        raise LaggingConnection("pushes measured late; reconnecting")
                    for w in [x for x in (muted or ()) if x in sub_of]:
                        sid = sub_of.pop(w)  # by_id keeps it: pushes already in flight are counted
                        await session.call("logsUnsubscribe", [sid])
                        stats.unsubscribes += 1
                        aws._status(on_status, "unsubscribed", target, wallet=w)  # noqa: SLF001
                    item = await session.next_message(poll_s)
                    if item is None:
                        if time.monotonic() - last_frame > idle_timeout_s:
                            raise aws.StaleConnection(f"no frame for {idle_timeout_s:.0f}s")
                        continue
                    raw, at_ms = item
                    last_frame = time.monotonic()
                    try:
                        msg = json.loads(raw)
                    except (TypeError, ValueError):
                        stats.undecoded += 1
                        continue
                    if not isinstance(msg, dict) or msg.get("method") != "logsNotification":
                        continue
                    params = msg.get("params") or {}
                    w = by_id.get(params.get("subscription")) if isinstance(params, Mapping) else None
                    if w is None:
                        stats.unknown_subscription += 1
                        continue
                    result = params.get("result")
                    result = result if isinstance(result, Mapping) else {}
                    value = result.get("value")
                    value = value if isinstance(value, Mapping) else {}
                    sig = value.get("signature")
                    if not isinstance(sig, str):
                        stats.undecoded += 1
                        continue
                    nbytes = len(raw if isinstance(raw, bytes) else raw.encode())
                    stats.notifications += 1
                    stats.notification_bytes += nbytes
                    stats.per_wallet[w] = stats.per_wallet.get(w, 0) + 1
                    ctx = result.get("context")
                    slot = _int(ctx.get("slot")) if isinstance(ctx, Mapping) else None
                    if slot is not None:
                        last_slot = slot if last_slot is None else max(last_slot, slot)
                    failed = value.get("err") is not None
                    if failed:
                        stats.failed += 1
                    if meter is not None:
                        meter.note_notice(w, failed=failed, nbytes=nbytes)
                    if muted and w in muted:
                        stats.muted_dropped += 1  # pushed before the unsubscribe took effect
                        continue
                    logs = value.get("logs")
                    n = Notice(signature=sig, wallet=w, slot=slot, failed=failed,
                               logs=tuple(str(x) for x in logs) if isinstance(logs, list) else None,
                               recv_ms=at_ms, conn=stats.connects, nbytes=nbytes)
                    if not seen.add((sig, w)):
                        stats.duplicates += 1
                        continue
                    yield n
            break
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - every failure is a reconnect
            stats.disconnects += 1
            delay = aws.backoff_delay(attempt)
            attempt += 1
            aws._status(on_status, "disconnected", target, error=f"{type(exc).__name__}: {exc}",  # noqa: SLF001
                        reconnect_in_s=delay, attempt=attempt, last_slot=last_slot)
            await aws._wait(stop, delay)  # noqa: SLF001


# --------------------------------------------------------------------------------------
# the engine
# --------------------------------------------------------------------------------------


@dataclass
class Outcome:
    """What happened to one (signature, wallet) pair. Returned for tests and the scratch run."""

    tx: str
    wallet: str
    status: str  # written | matched_existing | duplicate | unconfirmed
    reason: str = ""
    token: str | None = None
    side: str | None = None
    row: dict[str, Any] | None = None
    matched_sources: tuple[str, ...] = ()
    recv_latency_ms: int | None = None
    write_latency_ms: int | None = None
    backfilled: bool = False


@dataclass
class SolStreamStats:
    notices: int = 0
    skipped_muted: int = 0
    skipped_failed: int = 0
    skipped_no_swap_program: int = 0
    txs: int = 0
    tx_missing: int = 0
    trades: int = 0
    written: int = 0
    duplicates: int = 0
    unpriced_written: int = 0
    errors: int = 0
    tx_null_answers: int = 0
    rpc_errors: Counter[str] = field(default_factory=Counter)
    matched: Counter[str] = field(default_factory=Counter)
    unconfirmed: Counter[str] = field(default_factory=Counter)
    read_lag_ms: deque[int] = field(default_factory=lambda: deque(maxlen=2000))
    recv_latency_ms: deque[int] = field(default_factory=lambda: deque(maxlen=2000))
    write_latency_ms: deque[int] = field(default_factory=lambda: deque(maxlen=2000))
    tracked: int = 0
    excluded: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        def pct(v: Sequence[int], q: float) -> int | None:
            s = sorted(v)
            return s[min(len(s) - 1, int(q * len(s)))] if s else None

        return {
            "notices": self.notices, "skipped_muted": self.skipped_muted, "skipped_failed": self.skipped_failed,
            "skipped_no_swap_program": self.skipped_no_swap_program, "txs": self.txs,
            "tx_missing": self.tx_missing, "trades": self.trades, "written": self.written,
            "duplicates": self.duplicates, "unpriced_written": self.unpriced_written,
            "errors": self.errors, "tx_null_answers": self.tx_null_answers, "rpc_errors": dict(self.rpc_errors),
            "matched": dict(self.matched), "unconfirmed": dict(self.unconfirmed),
            "push_lag_ms_p50": pct(self.read_lag_ms, 0.5), "push_lag_ms_p90": pct(self.read_lag_ms, 0.9),
            "recv_latency_ms_p50": pct(self.recv_latency_ms, 0.5),
            "recv_latency_ms_p90": pct(self.recv_latency_ms, 0.9),
            "write_latency_ms_p50": pct(self.write_latency_ms, 0.5),
            "write_latency_ms_p90": pct(self.write_latency_ms, 0.9),
            "tracked": self.tracked, "excluded": len(self.excluded),
        }


def _clock_ms() -> int:
    return time.time_ns() // 1_000_000


def sol_usd_at(conn: Any, ts_ms: int) -> Decimal | None:
    """SOL/USD from ``native_prices`` within ``native_price``'s default tolerance, else None."""
    try:
        from kaiba.providers import native_price

        got = native_price.at(CHAIN, int(ts_ms), conn)
    except Exception as exc:  # noqa: BLE001 - unpriced, not crashed
        log.debug("sol_wallets: SOL/USD unreadable (%s)", exc)
        return None
    value = getattr(got, "price_usd", None)
    try:
        return Decimal(str(value)) if value is not None and Decimal(str(value)) > 0 else None
    except (InvalidOperation, ValueError):
        return None


class SolWalletStream:
    """Transaction-driven processing of tracked-wallet swaps. Every dependency is injectable.

    ``reader`` answers the wallets / cohort / existing-rows / SOL price questions; ``writer``
    takes rows and events. In production they are one connection; the scratch run reads the
    live database read-only and writes a scratch copy of the schema.
    """

    def __init__(
        self,
        rpc: RpcCall,
        *,
        reader: Any,
        writer: Any,
        sol_usd: Callable[[int], Decimal | None],
        clock_ms: Callable[[], int] = _clock_ms,
        tx_retries: Sequence[float] = TX_RETRIES,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        on_written: Callable[[Outcome], None] | None = None,
        on_outcome: Callable[[Outcome], None] | None = None,
        on_router: Callable[[str], None] | None = None,
        on_push_lag: Callable[[Notice, int], None] | None = None,
    ) -> None:
        self.rpc = rpc
        self.reader = reader
        self.writer = writer
        self.sol_usd = sol_usd
        self.clock_ms = clock_ms
        self.tx_retries = tuple(tx_retries)
        self.sleep = sleep
        self.on_written = on_written
        self.on_outcome = on_outcome
        #: Called once when a TRACKED wallet's evidence crosses the router rule, so the
        #: listener can drop it before the hourly refresh (every one of its swaps costs a read).
        self.on_router = on_router
        #: Called with each LIVE read's block->push delay (the listener's lag watchdog).
        self.on_push_lag = on_push_lag
        self._flagged: set[str] = set()
        self.stats = SolStreamStats()
        self.wallets: frozenset[str] = frozenset()
        self.evidence: dict[str, OrderedDict[str, bool]] = {}
        self.screen_evidence: dict[str, dict[str, Any]] = {}
        self.last_slot: int | None = None
        #: The thresholds of the last screen (:func:`params`); the live router flag uses them.
        self.params: dict[str, float | int] = dict(DEFAULT_PARAMS)
        #: Per-wallet cost accounting, when the listener provides one.
        self.meter: WalletMeter | None = None
        #: Signatures this engine has already read (any outcome), so the screen never pays
        #: 40 CU twice for one transaction.
        self._read: OrderedDict[str, None] = OrderedDict()

    # ---------------------------------------------------------------- reads

    async def _get_tx(self, sig: str, wallet: str | None = None) -> Mapping[str, Any] | None:
        params = [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": COMMITMENT}]
        for delay in (0.0, *self.tx_retries):
            if delay:
                await self.sleep(delay)
            if self.meter is not None:
                self.meter.note_call(wallet, "getTransaction")
            try:
                got = await self.rpc("getTransaction", params)
            except RpcFailure as exc:
                self.stats.rpc_errors[error_key(exc)] += 1
                log.debug("sol_wallets: getTransaction %s failed: %s", sig[:12], exc)
                continue
            if isinstance(got, Mapping) and isinstance(got.get("meta"), Mapping):
                return got
            self.stats.tx_null_answers += 1
        return None

    async def _block_ts_ms(self, tx: Mapping[str, Any]) -> int | None:
        bt = _int(tx.get("blockTime"))
        if bt:
            return bt * 1000
        slot = _int(tx.get("slot"))
        if slot is None:
            return None
        try:
            got = _int(await self.rpc("getBlockTime", [slot]))
        except RpcFailure:
            return None
        return got * 1000 if got else None

    # ---------------------------------------------------------------- evidence

    def _mark_read(self, sig: str) -> None:
        self._read[sig] = None
        while len(self._read) > READ_KEEP:
            self._read.popitem(last=False)

    def note_evidence(self, wallet: str, sig: str, zero: bool | None, *, notify: bool = True) -> None:
        """Record one router-evidence point. ``notify=False`` is the screen's own sampling,
        which decides on the evidence itself and must not request a second screen."""
        if zero is None:
            return
        book = self.evidence.setdefault(wallet, OrderedDict())
        book[sig] = bool(zero)
        book.move_to_end(sig)
        while len(book) > EVIDENCE_KEEP:
            book.popitem(last=False)
        if notify and wallet in self.wallets and wallet not in self._flagged and classify_routers(
                {wallet: book}, min_txs=int(self.params["router_min_txs"]),
                share=float(self.params["router_zero_net_share"])):
            self._flagged.add(wallet)
            log.info("sol_wallets: a tracked wallet now meets the router rule; rescreen requested")
            if self.on_router is not None:
                with contextlib.suppress(Exception):
                    self.on_router(wallet)

    # ---------------------------------------------------------------- the trade

    def _emit(self, outcome: Outcome) -> Outcome:
        if self.on_outcome is not None:
            with contextlib.suppress(Exception):
                self.on_outcome(outcome)
        return outcome

    async def process_signature(self, sig: str, notice: Notice | None = None,
                                tx: Mapping[str, Any] | None = None) -> list[Outcome]:
        """Every confirmed swap of every tracked wallet in ``sig``, written once."""
        self.stats.txs += 1
        tx = tx if tx is not None else await self._get_tx(sig, notice.wallet if notice is not None else None)
        self._mark_read(sig)
        if tx is None:
            self.stats.tx_missing += 1
            log.info("sol_wallets: no transaction for %s after retries", sig[:14])
            return []
        slot = _int(tx.get("slot"))
        if slot is not None:
            self.last_slot = slot if self.last_slot is None else max(self.last_slot, slot)
        bt = _int(tx.get("blockTime"))
        if notice is not None and not notice.backfilled and bt:
            lag = int(notice.recv_ms) - bt * 1000
            self.stats.read_lag_ms.append(lag)
            if self.on_push_lag is not None:
                with contextlib.suppress(Exception):
                    self.on_push_lag(notice, lag)
        keys = set(account_keys(tx))
        meta = tx.get("meta") or {}
        owners = {str(b.get("owner")) for k in ("preTokenBalances", "postTokenBalances")
                  for b in meta.get(k) or [] if isinstance(b, Mapping) and b.get("owner")}
        here = sorted(w for w in self.wallets if w in keys or w in owners)
        if not here:
            return []
        ts_ms: int | None = None
        recv_ms = notice.recv_ms if notice is not None else self.clock_ms()
        backfilled = bool(notice.backfilled) if notice is not None else False
        out: list[Outcome] = []
        for wallet in here:
            self.note_evidence(wallet, sig, zero_net(tx, wallet))
            leg, reason = classify(tx, wallet)
            if leg is None:
                if reason != "not_a_party":
                    self.stats.unconfirmed[reason] += 1
                out.append(self._emit(Outcome(sig, wallet, "unconfirmed", reason=reason, backfilled=backfilled)))
                continue
            self.stats.trades += 1
            if ts_ms is None:
                ts_ms = await self._block_ts_ms(tx)
            if ts_ms is None:
                self.stats.unconfirmed["no_block_time"] += 1
                out.append(self._emit(Outcome(sig, wallet, "unconfirmed", reason="no_block_time",
                                              token=leg.token, side=leg.side, backfilled=backfilled)))
                continue
            try:
                out.append(self._emit(self._write(tx, sig, wallet, leg, ts_ms=ts_ms, recv_ms=recv_ms,
                                                  backfilled=backfilled)))
            except Exception as exc:  # noqa: BLE001 - one bad trade never stops the feed
                self.stats.errors += 1
                log.warning("sol_wallets: %s %s failed: %s", sig[:14], wallet[:8], exc)
        return out

    def _write(self, tx: Mapping[str, Any], sig: str, wallet: str, leg: Leg, *, ts_ms: int,
               recv_ms: int, backfilled: bool) -> Outcome:
        sol_usd = self.sol_usd(ts_ms)
        usd_value, price_usd = price_fields(lamports=leg.lamports, atoms=leg.atoms, decimals=leg.decimals,
                                            sol_usd=sol_usd)
        sig_list = signers(tx)
        row = {
            "chain": CHAIN.value,
            "tx": sig,
            "slot": _int(tx.get("slot")),
            "block_index": _int(tx.get("transactionIndex")),
            "ts_ms": int(ts_ms),
            "wallet": wallet,
            "token": leg.token,
            "side": leg.side,
            "amount_token": str(int(leg.atoms)),
            "amount_native": str(int(leg.lamports)),
            "amount_quote": None,
            "quote_mint": None,
            "price_usd": price_usd,
            "usd_value": usd_value,
            "program": leg.program,
            "source": SOURCE,
            "fee_payer": sig_list[0] if sig_list else None,
        }
        recv_latency = recv_ms - ts_ms if not backfilled else None
        existing = existing_rows(self.reader, wallet=wallet, token=leg.token, tx=sig, ts_ms=ts_ms)
        if existing:
            sources = tuple(sorted({str(r["source"]) for r in existing}))
            if sources == (SOURCE,):
                self.stats.duplicates += 1
                return Outcome(sig, wallet, "duplicate", token=leg.token, side=leg.side, row=row,
                               matched_sources=sources, backfilled=backfilled)
            for s in sources:
                self.stats.matched[s] += 1
            return Outcome(sig, wallet, "matched_existing", token=leg.token, side=leg.side, row=row,
                           matched_sources=sources, recv_latency_ms=recv_latency, backfilled=backfilled)
        written_ms = self.clock_ms()
        write_latency = written_ms - ts_ms if not backfilled else None
        extra = {
            "venue": SWAP_PROGRAMS.get(leg.program),
            "token_decimals": leg.decimals,
            "sol_usd": str(sol_usd) if sol_usd is not None else None,
            "fee_added_back": wallet == row["fee_payer"],
            "block_ts_ms": ts_ms,
            "recv_ms": recv_ms,
            "written_ms": written_ms,
            "recv_latency_ms": recv_latency,
            "write_latency_ms": write_latency,
            "backfilled": backfilled,
        }
        if not write_trade(self.writer, row, extra):
            self.stats.duplicates += 1
            return Outcome(sig, wallet, "duplicate", token=leg.token, side=leg.side, row=row, backfilled=backfilled)
        self.stats.written += 1
        if usd_value is None:
            self.stats.unpriced_written += 1
        if recv_latency is not None:
            self.stats.recv_latency_ms.append(recv_latency)
        if write_latency is not None:
            self.stats.write_latency_ms.append(write_latency)
        outcome = Outcome(sig, wallet, "written", token=leg.token, side=leg.side, row=row,
                          recv_latency_ms=recv_latency, write_latency_ms=write_latency, backfilled=backfilled)
        _note_written(self.writer)
        if self.on_written is not None:
            with contextlib.suppress(Exception):
                self.on_written(outcome)
        return outcome

    # ---------------------------------------------------------------- the screen

    async def screen(self, candidates: Iterable[str], *, now_s: float | None = None,
                     p: Mapping[str, Any] | None = None) -> dict[str, dict[str, Any]]:
        """``{wallet: {"reason": "bot"|"failed"|"router", ...evidence}}`` for every candidate to exclude.

        ``p`` are the thresholds (:func:`params`; the defaults when omitted). Rules, first
        match wins: a BOT by its successful-transaction rate, MOSTLY FAILED by its failed
        share (the cost guard), a ROUTER by its zero-net swaps.

        One ``getSignaturesForAddress`` per candidate (the bot rate), then, for a candidate
        that is not a bot and has fewer than :data:`ROUTER_MIN_TXS` swaps of evidence, up to
        :data:`ROUTER_SAMPLE_TXS` of its newest successful transactions this feed has not
        already read. Router evidence accumulates from every transaction the feed reads, so
        a busy wallet costs nothing extra after the first screen. A candidate whose screen
        read fails is kept: an unread wallet is not a bot.
        """
        now = time.time() if now_s is None else float(now_s)
        pp: dict[str, Any] = {**DEFAULT_PARAMS, **dict(p or {})}
        self.params = pp
        excluded: dict[str, dict[str, Any]] = {}
        self.screen_evidence = {}
        self._flagged = set()
        for wallet in normalize_wallets(candidates):
            if self.meter is not None:
                self.meter.note_call(wallet, "getSignaturesForAddress")
            try:
                sigs = await self.rpc("getSignaturesForAddress",
                                      [wallet, {"limit": SCREEN_SIGS, "commitment": COMMITMENT}])
            except RpcFailure as exc:
                log.info("sol_wallets: screen read failed for a wallet: %s", exc)
                continue
            sigs = [s for s in sigs or [] if isinstance(s, Mapping)]
            rate = signature_rate(sigs, now_s=now)
            self.screen_evidence[wallet] = rate
            hits = [rule for rule, hit in (
                ("bot", is_bot(rate, max_per_hour=float(pp["bot_max_txs_per_hour"]), min_sigs=int(pp["bot_min_sigs"]))),
                ("failed", mostly_failed(rate, max_share=float(pp["max_failed_share"]),
                                         min_tx=int(pp["min_tx_for_failed_screen"]))),
            ) if hit]
            if hits:
                # ``reason`` is the first rule that matched; ``rules`` names every one, so a
                # wallet that is both a bot and mostly failed reads as both.
                excluded[wallet] = {"reason": hits[0], "rules": hits, **rate}
                continue
            if len(self.evidence.get(wallet, {})) >= int(pp["router_min_txs"]):
                continue  # enough evidence already; the live feed keeps it current
            todo = [str(s["signature"]) for s in sigs if s.get("err") is None
                    and isinstance(s.get("signature"), str) and s["signature"] not in self._read][:ROUTER_SAMPLE_TXS]
            for sig in todo:
                if self.meter is not None:
                    self.meter.note_call(wallet, "getTransaction")
                tx = await self._get_tx_once(sig)
                self._mark_read(sig)
                if tx is not None:
                    self.note_evidence(wallet, sig, zero_net(tx, wallet), notify=False)
        routers = classify_routers({w: self.evidence.get(w, {}) for w in normalize_wallets(candidates)},
                                   min_txs=int(pp["router_min_txs"]), share=float(pp["router_zero_net_share"]))
        ev = router_evidence(self.evidence)
        for wallet in routers:
            if wallet not in excluded:
                excluded[wallet] = {"reason": "router", **ev.get(wallet, {}),
                                    **self.screen_evidence.get(wallet, {})}
        self.stats.excluded = {w: str(v["reason"]) for w, v in excluded.items()}
        return excluded

    async def _get_tx_once(self, sig: str) -> Mapping[str, Any] | None:
        try:
            got = await self.rpc("getTransaction", [sig, {"encoding": "jsonParsed",
                                                         "maxSupportedTransactionVersion": 0,
                                                         "commitment": COMMITMENT}])
        except RpcFailure:
            return None
        return got if isinstance(got, Mapping) and isinstance(got.get("meta"), Mapping) else None


def _note_written(conn: Any) -> None:
    try:
        from kaiba.ingest.runner import note_events  # lazy: runner imports this module

        note_events(FEED, 1, conn)
    except Exception as exc:  # noqa: BLE001 - bookkeeping never breaks the feed
        log.debug("sol_wallets: ingest_status note failed: %s", exc)


# --------------------------------------------------------------------------------------
# the listener
# --------------------------------------------------------------------------------------


def alchemy_url() -> str | None:
    """The configured Solana endpoint, only if it is an Alchemy ``/v2/`` URL (websocket-capable)."""
    from kaiba.core.config import get_settings

    url = str(get_settings().rpc_for(CHAIN) or "")
    return url if "/v2/" in url else None


def _resume_from_kv(conn: Any, head: int) -> int | None:
    try:
        row = fetch_one(conn, "SELECT value FROM kv WHERE key = ?", (KV_RESUME,))
    except Exception:  # noqa: BLE001
        return None
    text = str((row or {}).get("value") or "")
    value = int(text) if text.isdigit() else None
    if value is None or value > head or head - value > RESUME_MAX_SLOTS:
        return None
    return value


def _save_resume(conn: Any, slot: int) -> None:
    try:
        conn.execute(
            "INSERT INTO kv (key, value, updated_ms) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_ms = excluded.updated_ms",
            (KV_RESUME, str(int(slot)), now_ms()),
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("sol_wallets: resume point not saved: %s", exc)


def should_read(notice: Notice) -> str | None:
    """Why a notification is NOT worth a ``getTransaction`` (40 CU), or ``None`` to read it."""
    if notice.failed:
        return "failed"
    if logs_mention_swap(notice.logs) is False:
        return "no_swap_program"
    return None


async def run(
    stop: asyncio.Event | None = None,
    *,
    url: str | None = None,
    reader: Any = None,
    writer: Any = None,
    rpc: Any = None,
    sol_usd: Callable[[int], Decimal | None] | None = None,
    refresh_s: float = REFRESH_S,
    connect: Callable[[str], Any] | None = None,
    resume: bool = True,
    on_written: Callable[[Outcome], None] | None = None,
    on_outcome: Callable[[Outcome], None] | None = None,
    on_status: Callable[[dict[str, Any]], None] | None = None,
    engine_out: list[SolWalletStream] | None = None,
    feed_stats: SolFeedStats | None = None,
) -> dict[str, Any]:
    """Follow the tracked wallets until ``stop``. Shaped for ``kaiba.ingest.runner``."""
    stop = stop or asyncio.Event()
    url = url or alchemy_url()
    if not url:
        log.warning("sol_wallets: no Alchemy websocket endpoint configured for solana; idle")
        return {"idle": "no websocket endpoint"}
    w = writer if writer is not None else get_conn()
    r = reader if reader is not None else w
    client = rpc or SolRpc(url)
    if sol_usd is None:
        def sol_usd(ts_ms: int) -> Decimal | None:
            return sol_usd_at(r, ts_ms)

    rescreen = asyncio.Event()
    lagging = asyncio.Event()
    cfg = {"p": params()}
    watch = LagWatch(limit_ms=int(cfg["p"]["lag_reconnect_ms"]), window=int(cfg["p"]["lag_window"]))
    budget = NotificationBudget(int(cfg["p"]["max_notifications_per_wallet_per_hour"]))
    #: Wallets the budget unsubscribed; lifted at the next screen.
    muted: set[str] = set()
    meter = WalletMeter()

    def on_push_lag(notice: Notice, lag_ms: int) -> None:
        median = watch.add(notice.conn, lag_ms)
        if median is not None and not lagging.is_set():
            lagging.set()
            emit(EventKind.SYSTEM, {"component": f"ingest.{FEED}", "status": "lagging_reconnect",
                                    "median_push_lag_ms": median, "window": watch.window,
                                    "limit_ms": watch.limit_ms, "connection": notice.conn},
                 chain=CHAIN, level="warn", conn=w)

    engine = SolWalletStream(client.call, reader=r, writer=w, sol_usd=sol_usd, on_written=on_written,
                             on_outcome=on_outcome, on_router=lambda _w: rescreen.set(),
                             on_push_lag=on_push_lag, tx_retries=TX_RETRIES)
    engine.meter = meter
    if engine_out is not None:
        engine_out.append(engine)
    queue: asyncio.Queue[tuple[str, Notice]] = asyncio.Queue(maxsize=10_000)
    seen_sig = aws._RecentKeys(20_000)  # noqa: SLF001 - shared bounded set, by design
    fstats = feed_stats if feed_stats is not None else SolFeedStats()

    def cu() -> float:
        return round(float(getattr(client, "estimated_cu", lambda: 0)() or 0) + fstats.estimated_cu(), 1)

    screened = {"at": time.monotonic()}

    async def choose_set() -> tuple[str, ...]:
        screened["at"] = time.monotonic()
        p = params()
        cfg["p"] = p
        watch.limit_ms, watch.window = int(p["lag_reconnect_ms"]), max(1, int(p["lag_window"]))
        budget.limit = int(p["max_notifications_per_wallet_per_hour"])
        candidates = candidate_set(r)
        excluded: dict[str, dict[str, Any]] = {}
        if candidates:
            try:
                excluded = await engine.screen(candidates, p=p)
            except Exception as exc:  # noqa: BLE001 - an unscreened set is still a set
                log.warning("sol_wallets: screen skipped: %s", aws.redact(exc, url))
        wallets = tuple(x for x in candidates if x not in excluded)
        engine.stats.tracked = len(wallets)
        for address, evidence in sorted(excluded.items()):
            # Never silent: every wallet the screen drops is named, with its evidence, on
            # every refresh that drops it.
            emit(EventKind.SYSTEM, {
                "component": f"ingest.{FEED}", "status": "excluded", "address": address, **evidence,
                "rule": {k: p[k] for k in ("bot_max_txs_per_hour", "bot_min_sigs", "max_failed_share",
                                           "min_tx_for_failed_screen", "router_min_txs", "router_zero_net_share")},
            }, chain=CHAIN, subject=address, level="warn", conn=w)
        if excluded:
            log.warning("sol_wallets: screen excluded %d wallet(s): %s", len(excluded),
                        ", ".join(f"{a} ({v['reason']})" for a, v in sorted(excluded.items())))
        return wallets

    resume_point = ResumePoint()

    async def worker() -> None:
        while True:
            sig, notice = await queue.get()
            try:
                await engine.process_signature(sig, notice)
            except Exception as exc:  # noqa: BLE001 - one tx never stops the feed
                engine.stats.errors += 1
                log.warning("sol_wallets: %s failed: %s", sig[:14], aws.redact(exc, url))
            finally:
                resume_point.finished(notice.slot)
                queue.task_done()

    def status(payload: dict[str, Any]) -> None:
        if on_status is not None:
            with contextlib.suppress(Exception):
                on_status(payload)

    def mute(wallet: str, count: int) -> None:
        """The hard budget: unsubscribe a wallet until the next screen, and say so."""
        muted.add(wallet)
        engine.wallets = frozenset(x for x in engine.wallets if x != wallet)
        engine.stats.excluded[wallet] = "notification_budget"
        emit(EventKind.SYSTEM, {"component": f"ingest.{FEED}", "status": "excluded", "address": wallet,
                                "reason": "notification_budget", "notifications_last_hour": count,
                                "rule": {"max_notifications_per_wallet_per_hour": budget.limit},
                                "until": "next screen"},
             chain=CHAIN, subject=wallet, level="warn", conn=w)
        log.warning("sol_wallets: %s pushed %d notifications in the last hour (limit %d); unsubscribed "
                    "until the next screen", wallet, count, budget.limit)

    head = await _slot(client.call)
    wallets = await choose_set()
    engine.wallets = frozenset(wallets)
    resume_from = _resume_from_kv(w, head) if (resume and head is not None) else None
    emit(EventKind.SYSTEM, {"component": f"ingest.{FEED}", "status": "starting", "chain": CHAIN.value,
                            "tracked": len(wallets), "excluded": engine.stats.excluded,
                            "resume_from_slot": resume_from, "endpoint": aws.mask_url(url)},
         chain=CHAIN, conn=w)
    tasks = [asyncio.ensure_future(worker()) for _ in range(max(1, int(WORKERS)))]
    last_stats = time.monotonic()
    last_saved = 0.0
    try:
        while not stop.is_set():
            if not wallets:
                emit(EventKind.SYSTEM, {"component": f"ingest.{FEED}", "status": "idle",
                                        "reason": "no trusted_copy or proven sol wallet to track",
                                        "excluded": engine.stats.excluded},
                     chain=CHAIN, level="warn", conn=w)
                with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=refresh_s)
                if stop.is_set():
                    break
                head = await _slot(client.call)
                wallets = await choose_set()
                engine.wallets = frozenset(wallets)
                resume_from = head  # nothing was tracked before this slot
                continue
            gen_stop = asyncio.Event()
            pending: dict[str, Any] = {}

            async def refresher(current: tuple[str, ...], gen: asyncio.Event = gen_stop,
                                box: dict[str, Any] = pending) -> None:
                while not stop.is_set() and not gen.is_set():
                    # The hourly refresh, or sooner when a tracked wallet starts routing, but
                    # never two screens within RESCREEN_MIN_S.
                    due = time.monotonic() + refresh_s
                    while not stop.is_set() and time.monotonic() < due:
                        if rescreen.is_set() and time.monotonic() - screened["at"] >= RESCREEN_MIN_S:
                            break
                        with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
                            await asyncio.wait_for(stop.wait(), timeout=min(1.0, max(0.01, due - time.monotonic())))
                    if stop.is_set():
                        break
                    rescreen.clear()
                    h = await _slot(client.call)
                    nxt = await choose_set()
                    # A budget mute lasts until this screen: restart whenever one is in force.
                    if (nxt != current or muted) and h is not None:
                        box["wallets"], box["resume"] = nxt, h
                        break
                gen.set()

            ref = asyncio.ensure_future(refresher(wallets))
            try:
                async for notice in stream(url, wallets, rpc=client.call, stop=gen_stop, connect=connect,
                                           stats=fstats, on_status=status, from_slot=resume_from,
                                           anchor_on_subscribe=True, idle_timeout_s=IDLE_TIMEOUT_S,
                                           reconnect=lagging, muted=muted, meter=meter):
                    engine.stats.notices += 1
                    if not notice.backfilled:
                        count = budget.add(notice.wallet, time.monotonic())
                        if budget.over(count) and notice.wallet not in muted:
                            mute(notice.wallet, count)
                    if notice.wallet in muted:
                        engine.stats.skipped_muted += 1
                        continue
                    skip = should_read(notice)
                    if skip == "failed":
                        engine.stats.skipped_failed += 1
                    elif skip == "no_swap_program":
                        engine.stats.skipped_no_swap_program += 1
                    elif seen_sig.add(notice.signature):
                        try:
                            queue.put_nowait((notice.signature, notice))
                            resume_point.queued(notice.slot)
                        except asyncio.QueueFull:
                            engine.stats.errors += 1
                    if resume_point.value() and time.monotonic() - last_saved > 30:
                        _save_resume(w, int(resume_point.value() or 0))
                        last_saved = time.monotonic()
                    if time.monotonic() - last_stats > STATS_EVERY_S:
                        last_stats = time.monotonic()
                        emit(EventKind.SYSTEM, {"component": f"ingest.{FEED}", "status": "stats",
                                                **engine.stats.as_dict(), "socket": fstats.to_dict(),
                                                "rpc_calls": dict(getattr(client, "calls", {}) or {}),
                                                "estimated_cu": cu(), "per_wallet": meter.snapshot(),
                                                "muted": sorted(muted)},
                             chain=CHAIN, conn=w)
            finally:
                ref.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await ref
            if stop.is_set():
                break
            if "wallets" in pending:
                wallets = pending["wallets"]
                muted.clear()  # the screen just decided again
                engine.wallets = frozenset(wallets)
                resume_from = pending.get("resume")
                emit(EventKind.SYSTEM, {"component": f"ingest.{FEED}", "status": "resubscribing",
                                        "tracked": len(wallets), "from_slot": resume_from,
                                        "excluded": engine.stats.excluded},
                     chain=CHAIN, conn=w)
            else:
                break  # the socket gave up: hand back to the supervisor, which restarts with backoff
        with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
            await asyncio.wait_for(queue.join(), timeout=10.0)
    finally:
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        if resume_point.value():
            _save_resume(w, int(resume_point.value() or 0))
        if rpc is None:
            await client.close()
    return {"stats": engine.stats.as_dict(), "socket": fstats.to_dict(),
            "rpc_calls": dict(getattr(client, "calls", {}) or {}), "estimated_cu": cu(),
            "per_wallet": meter.snapshot(reset=False), "muted": sorted(muted)}


__all__: Sequence[str] = (
    "CHAIN", "SOURCE", "FEED", "TRUSTED_COHORT", "WSOL_MINT", "QUOTE_MINTS", "SWAP_PROGRAMS",
    "DEFAULT_PARAMS", "LagWatch", "LaggingConnection", "Leg", "Notice", "NotificationBudget", "Outcome",
    "PARAMS_PROVENANCE", "ResumePoint", "RpcFailure", "WalletMeter", "mostly_failed", "params",
    "SolFeedStats", "SolRpc", "SolStreamStats", "error_key",
    "SolWalletStream", "WalletDelta", "account_keys", "alchemy_url", "candidate_set", "classify",
    "classify_routers", "existing_rows", "is_bot", "logs_mention_swap", "normalize_wallets",
    "price_fields", "programs_in", "proven_wallets", "router_evidence", "run", "should_read",
    "signature_rate", "signatures_since", "signers", "sol_usd_at", "stream", "swap_program",
    "tracked_set", "trusted_wallets", "wallet_delta", "write_trade", "zero_net",
)
