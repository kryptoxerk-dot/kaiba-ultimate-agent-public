"""The owner's trusted Robinhood wallets, written to ``swaps`` about a second after the block.

Why this exists. OWNER DECISION 2026-10-04: his hand-picked robinhood wallets are strong
evidence, NOT blind copies -- "Copy only the tokens the agent wants to. With confluences."
``sm-trenches`` counts smart buyers from the ``swaps`` tape, and MEASURED on the box the
same day the tape held ZERO robinhood rows for any of his hand-picked wallets, all-time,
from every source. They trade through third-party routers, the v4 PoolManager and EIP-7702
self-calls, so the Pons poller books their curve trades under the ROUTER's address
(``CurveBuy.trader`` is ``msg.sender``) and the GMGN smart-money sweep does not cover them
at all. The lane could not see them, whatever it was told about them.

WHICH wallets is a private choice and lives only in the database: the tracked set is the
robinhood ``wallets`` rows whose ``cohort`` is ``trusted_copy``. No address is written in
this source (it is exported publicly); the operator sets the cohort on the box.

What it does, in order:

1. Subscribe (``alchemy_ws.stream``) to ERC-20 ``Transfer`` logs into and out of the
   tracked set: robinhood wallets whose DATABASE cohort is ``trusted_copy``, minus routing
   bots (wallets whose activity nets to zero inside each transaction), refreshed hourly.
   A trusted wallet the router screen drops is never dropped silently: every refresh that
   excludes one emits a ``system`` event naming it with its evidence
   (``status: router_excluded``).
2. A push is only a trigger. The transaction's RECEIPT is the evidence: every Transfer leg
   of every tracked wallet in it is decoded from the receipt and netted per wallet and
   token (``alchemy_ws.fold_tx``), so a leg that arrived in a later push, or never, still
   counts.
3. Only a CONFIRMED trade is written. Confirmation is one of:
   * the token came from (buy) / went to (sell) an address that emitted a swap event in
     the same receipt -- a Pons curve (``CurveBuy``/``CurveSell``), a Uniswap v2/v3 pool,
     or the v4 PoolManager -- directly or through a router in between (:func:`venue_of`);
   * a quote-asset leg (WETH, USDG) moved the other way in the same transaction;
   * a native-ETH payment, MEASURED as the wallet's balance change across the block with
     the gas fee added back (:func:`native_delta`). Native ETH emits no log, so this is
     the only way to see it; it is used only where nothing else in the block can explain
     the change (see that function).
   A Transfer nothing corroborates (an airdrop, a gift, a mint) is never written.
4. Units are the conventions of the rows the lane already reads (``ingest/robinhood.py``'s
   Pons rows): ``amount_token`` token atoms, ``amount_native`` wei, ``usd_value`` = wei /
   1e18 x the SAME ETH/USD source the Pons poller uses (``robinhood.EthPrice``),
   ``price_usd`` = USD per WHOLE token using the token's MEASURED ``decimals()``, ``ts_ms``
   the block timestamp, ``slot`` the block number. Where a value cannot be made consistent
   it is ``NULL``, never invented:
   * a USDG (or other non-ETH) quote leg goes to ``amount_quote``/``quote_mint`` (base
     units, migration 027) and leaves ``amount_native``, ``usd_value`` and ``price_usd``
     NULL -- the Pons poller leaves non-ETH quotes unpriced and so does this;
   * a token whose Pons curve is quoted in a non-ETH pair token (registry
     ``quote_is_native: false``) never gets ``amount_native``: the Pons poller already
     writes PAIR-token atoms into that column for those tokens, and adding wei beside them
     would mix units on one token's tape. Measured ETH goes to ``amount_quote`` with
     ``quote_mint`` = the zero address instead;
   * unknown decimals -> ``price_usd`` NULL; unknown ETH price -> ``usd_value`` NULL.
5. Dedupe: a (tx, wallet, token) another feed already wrote is not written again
   (:func:`existing_rows`); the table's own UNIQUE key catches our own replays.
6. Emit ``wallet.trade`` with the row, exactly as ``robinhood.record_trade`` does.

Known interactions with the other feeds, ACCEPTED by the lead on 2026-10-04 (measured in
the box scratch runs that day; none of them changes a lane decision today):

* ``window_*`` entry features double-count router trades. When a tracked wallet buys a Pons
  curve through a router, the Pons poller books the same CurveBuy under the ROUTER's
  address and this module books it under the wallet (13 of 39 rows in the 6 h replay).
  ``lanes._window_flow`` counts both rows, so ``window_swaps``/``window_buy_usd``/
  ``window_buyers`` are inflated for those tokens. The routers are graded C/D "sniper",
  so they never become smart buyers, and no threshold reads ``window_*``.
* A GMGN row for the same trade written AFTER ours is not caught: its ``amount_token`` is
  in UI units, so the table's UNIQUE key (which includes ``amount_token``) differs and the
  trade appears twice under the same wallet. ``lanes._net_buyers`` keys by wallet, so the
  smart COUNT is unaffected; that wallet's ``buy_usd`` (and ``smart_buy_usd``) doubles.
* A direct curve trade (``CurveBuy.trader`` is the wallet itself) produces exactly the row
  the Pons poller writes. We are usually first (~0.7 s after the block against the
  poller's ~6-13 s), so the UNIQUE key keeps OUR row and the poller's insert is ignored.
  Readers that filter ``source = 'robinhood'`` (``learning.alpha_sources``, the scheduler's
  early-tape job, ``intelligence.bundles``/``lookalike``) then lose that print. MEASURED:
  0 such collisions for the trusted wallets in the replay (they use routers); 13 of 32 in
  a scratch sample of GMGN smart-money wallets.

What this module does NOT do: decide anything. The cohort a lane trusts is read by the
lane from the ``wallets`` table (``lanes._cohort``), never from these rows, and the rows
carry no cohort or tag claim at all.

RPC. Enrichment reads go over plain HTTPS to the same Alchemy endpoint as the socket, NOT
through the shared ``robinhood-rpc`` limiter bucket: that bucket is 0.6/s with
``max_inflight: 1`` and carries live stop-loss reads, and an ingest reader must never sit
in front of a stop. Volume is small (MEASURED 2026-10-04: 726 Transfer logs in 3 days for
the 15 tracked wallets, ~10 trades an hour), each trade is one receipt read plus, for a
native-ETH trade, one batch of three. The URL carries the API key; every string that can
carry it goes through ``alchemy_ws.redact``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import Counter, deque
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from kaiba.core.db import fetch_all, fetch_one, get_conn, jload
from kaiba.core.events import emit, emit_once
from kaiba.core.schemas import Chain, EventKind, now_ms
from kaiba.ingest import alchemy_ws as aws
from kaiba.ingest import robinhood as rh

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------------------
# identity
# --------------------------------------------------------------------------------------

CHAIN = Chain.ROBINHOOD
#: ``swaps.source``. Deliberately NOT ``gmgn:``-prefixed: ``grade.HUMAN_UNIT_SOURCE_PREFIXES``
#: reads that prefix as "UI units", and these rows are base units like every on-chain source.
SOURCE = "alchemy:ws"
#: The ``kaiba.ingest.runner`` registry name and ``ingest_status.feed``.
FEED = "rh_wallets"
TRUSTED_COHORT = "trusted_copy"

# --------------------------------------------------------------------------------------
# chain constants (each MEASURED on Robinhood Chain; tests pin them to their other homes)
# --------------------------------------------------------------------------------------

#: WETH (18) and USDG (6), by ``symbol()``/``decimals()`` on chain 2026-10-02 -- the same
#: values as ``execution.onchain_pool.ROBINHOOD_WETH``/``ROBINHOOD_USDG``. Spelled here so an
#: ingest process does not import the protection-side module; a test pins them equal.
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
QUOTE_ASSETS: frozenset[str] = frozenset({WETH, USDG})
#: Quote assets that ARE native ETH, 1:1 in wei: wrapped ETH and the v4/Pons native marker.
NATIVE_QUOTES: frozenset[str] = frozenset({WETH, aws.ZERO_ADDRESS})
WEI = Decimal(10) ** 18

#: Swap-event topics. CurveBuy/CurveSell are ``robinhood``'s constants (replayed against
#: real logs in its tests). The three Uniswap topics are keccak of their canonical
#: signatures and were MEASURED 2026-10-04 on 120 receipts of the tracked wallets: the v4
#: topic was emitted only by the PoolManager (71x), the v3 one by pool contracts (29x), the
#: v2 one by a pair (2x).
#: ``Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)``
TOPIC_V4_SWAP = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
#: ``Swap(address,address,int256,int256,uint160,uint128,int24)``
TOPIC_V3_SWAP = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
#: ``Swap(address,uint256,uint256,uint256,uint256,address)``
TOPIC_V2_SWAP = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"

PROGRAM_PONS = "pons-curve"  # the Pons poller's own spelling for the same venue
SWAP_PROGRAMS: dict[str, str] = {
    rh.TOPIC_CURVE_BUY: PROGRAM_PONS,
    rh.TOPIC_CURVE_SELL: PROGRAM_PONS,
    TOPIC_V4_SWAP: "uniswap-v4",
    TOPIC_V3_SWAP: "uniswap-v3",
    TOPIC_V2_SWAP: "uniswap-v2",
}
#: Venues known without a receipt: the v4 PoolManager holds every v4 pool's tokens.
STATIC_VENUES: dict[str, str] = {rh.POOL_MANAGER: "uniswap-v4"}

SELECTOR_APPROVE = "0x095ea7b3"  # approve(address,uint256)
SELECTOR_DECIMALS = "0x313ce567"  # decimals()

#: How a token's own path to a venue may wander through routers inside one receipt.
MAX_VENUE_HOPS = 4

#: Routing bots: a wallet with at least this many transactions in the lookback ...
ROUTER_MIN_TXS = 20
#: ... of which at least this share netted to zero for it (MEASURED 2026-10-03 on 400 RH
#: wallets: one router made 74.8% of all notifications and netted to zero every time).
ROUTER_ZERO_NET_SHARE = 0.8
ROUTER_LOOKBACK_BLOCKS = 212_000  # ~6 h at ~0.1015 s/block
REFRESH_S = 3600.0
#: Rows of the same (tx, wallet, token) another feed wrote are searched this far either
#: side of the block time: GMGN stamps its own clock, and its lag MEASURED p90 94 s.
DEDUPE_WINDOW_MS = 3_600_000
STATS_EVERY_S = 600.0
#: The socket's no-frame reconnect. MEASURED 2026-10-04: the 15 tracked wallets went more
#: than 600 s (the ``alchemy_ws`` default) without one Transfer, so the default reconnected
#: every ten quiet minutes. The websocket ping (20 s) still catches a dead TCP path, and the
#: subscribe-time anchor (``anchor_on_subscribe``) makes every reconnect backfill its gap.
IDLE_TIMEOUT_S = 1800.0
KV_RESUME = "ingest:rh_wallets:last_block"
#: A stored resume point older than this is ignored (the socket's own backfill cap).
RESUME_MAX_BLOCKS = 200_000

#: Bases of the quote amount a row carries, recorded on the event (never on the row).
QB_CURVE = "curve_event"
QB_WETH = "weth_leg"
QB_QUOTE = "quote_leg"
QB_NATIVE = "native_balance"

#: Alchemy compute units per call (alchemy.com/docs/reference/compute-unit-costs, read
#: 2026-10-03; values not listed there are counted at 20 so the estimate errs high).
CU_PER_CALL: dict[str, int] = {
    "eth_getTransactionReceipt": 20, "eth_getBalance": 20, "eth_getBlockByNumber": 20,
    "eth_getLogs": 60, "eth_call": 26, "eth_blockNumber": 10,
}


# --------------------------------------------------------------------------------------
# small pure helpers
# --------------------------------------------------------------------------------------


def _addr(value: Any) -> str | None:
    return aws.normalize_address(value)


def _hex_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return None
    try:
        return int(value, 16)
    except ValueError:
        return None


def _topic0(entry: Mapping[str, Any]) -> str:
    topics = entry.get("topics")
    if not isinstance(topics, Sequence) or isinstance(topics, str) or not topics:
        return ""
    return str(topics[0]).lower()


def swap_emitters(logs: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """``address -> program`` for every address that emitted a known swap event."""
    out: dict[str, str] = {}
    for entry in logs:
        program = SWAP_PROGRAMS.get(_topic0(entry))
        addr = _addr(entry.get("address"))
        if program and addr:
            out.setdefault(addr, program)
    return out


def token_edges(logs: Iterable[Mapping[str, Any]], token: str) -> list[tuple[str, str]]:
    """``(from, to)`` of every non-zero ERC-20 Transfer of ``token`` in a receipt."""
    edges: list[tuple[str, str]] = []
    for entry in logs:
        if _addr(entry.get("address")) != token or _topic0(entry) != aws.TRANSFER_TOPIC:
            continue
        topics = entry.get("topics") or []
        if len(topics) != 3:
            continue
        src, dst = aws.topic_address(topics[1]), aws.topic_address(topics[2])
        data = entry.get("data")
        try:
            amount = int(str(data)[2:66], 16) if isinstance(data, str) and data.startswith("0x") else 0
        except ValueError:
            amount = 0
        if src and dst and amount > 0:
            edges.append((src, dst))
    return edges


def venue_of(
    logs: Sequence[Mapping[str, Any]],
    token: str,
    wallet: str,
    side: str,
    emitters: Mapping[str, str],
    *,
    static: Mapping[str, str] = STATIC_VENUES,
    max_hops: int = MAX_VENUE_HOPS,
) -> tuple[str | None, str | None]:
    """``(program, venue)`` when the token's own path inside the receipt reaches a venue.

    Walks this token's Transfers upstream from the wallet for a buy (where did the tokens
    come from?) and downstream for a sell, through at most ``max_hops`` addresses, and
    stops at the first address that emitted a swap event in this receipt or is a static
    venue. A router in between is fine; an address that merely sent the tokens and swapped
    nothing is not a venue. The zero address (a mint) is never one.
    """
    edges = token_edges(logs, token)
    frontier = {wallet}
    seen = {wallet}
    for _ in range(max(1, int(max_hops))):
        if side == "buy":
            nxt = {src for src, dst in edges if dst in frontier}
        else:
            nxt = {dst for src, dst in edges if src in frontier}
        nxt -= seen
        nxt.discard(aws.ZERO_ADDRESS)
        if not nxt:
            return None, None
        for addr in sorted(nxt):
            if addr in emitters:
                return emitters[addr], addr
            if addr in static:
                return static[addr], addr
        seen |= nxt
        frontier = nxt
    return None, None


def curve_quote(
    logs: Iterable[Mapping[str, Any]], curve: str, side: str, net_atoms: int
) -> int | None:
    """The quote amount of THE curve trade that moved exactly ``net_atoms`` tokens.

    Exactly one ``CurveBuy`` (buy) / ``CurveSell`` (sell) from ``curve`` must carry a token
    amount equal to the wallet's net: a curve trade split across several recipients, or
    two of them in one transaction, cannot be attributed to this wallet and returns
    ``None``. Same word layout ``robinhood.parse_curve_trade`` decodes.
    """
    topic = rh.TOPIC_CURVE_BUY if side == "buy" else rh.TOPIC_CURVE_SELL
    hits: list[int] = []
    for entry in logs:
        if _addr(entry.get("address")) != curve or _topic0(entry) != topic:
            continue
        data = entry.get("data")
        if not isinstance(data, str) or not data.startswith("0x") or len(data) < 2 + 128:
            continue
        try:
            a0, a1 = int(data[2:66], 16), int(data[66:130], 16)
        except ValueError:
            continue
        quote, tokens = (a0, a1) if side == "buy" else (a1, a0)
        if tokens == int(net_atoms):
            hits.append(quote)
    return hits[0] if len(hits) == 1 else None


def routed_by_wallet(records: Iterable[aws.WalletTransfer]) -> dict[str, dict[str, bool]]:
    """``wallet -> {tx: routed}``: routed means every token it touched netted to zero.

    Raw signed sums over ALL legs, quote assets and mints included: a wallet that wraps ETH
    and pays the WETH in one transaction nets WETH to zero but keeps the token it bought,
    so it is not routing. A router passes everything through and nets zero on all of it.
    """
    nets: dict[tuple[str, str], dict[str, int]] = {}
    for r in records:
        if r.removed:
            continue
        signed = r.amount_atoms if r.side == "buy" else -r.amount_atoms
        bucket = nets.setdefault((r.wallet, r.tx), {})
        bucket[r.token] = bucket.get(r.token, 0) + signed
    out: dict[str, dict[str, bool]] = {}
    for (wallet, tx), per_token in nets.items():
        out.setdefault(wallet, {})[tx] = all(v == 0 for v in per_token.values())
    return out


def router_evidence(routed: Mapping[str, Mapping[str, bool]]) -> dict[str, dict[str, Any]]:
    """``wallet -> {txs, zero_net, zero_net_share}``: what the router screen decided on."""
    out: dict[str, dict[str, Any]] = {}
    for wallet, txs in routed.items():
        n = len(txs)
        z = sum(1 for v in txs.values() if v)
        out[wallet] = {"txs": n, "zero_net": z, "zero_net_share": round(z / n, 4) if n else None}
    return out


def classify_routers(
    routed: Mapping[str, Mapping[str, bool]],
    *,
    min_txs: int = ROUTER_MIN_TXS,
    share: float = ROUTER_ZERO_NET_SHARE,
) -> frozenset[str]:
    """Wallets with at least ``min_txs`` transactions of which ``share`` netted to zero."""
    out: set[str] = set()
    for wallet, txs in routed.items():
        n = len(txs)
        if n >= max(1, int(min_txs)) and sum(1 for v in txs.values() if v) / n >= share:
            out.add(wallet)
    return frozenset(out)


def cohort_wallets(conn: Any) -> frozenset[str]:
    """Robinhood wallets the DATABASE puts in ``trusted_copy``. Never a feed's claim."""
    rows = fetch_all(
        conn,
        "SELECT address FROM wallets WHERE chain = ? AND cohort = ?",
        (CHAIN.value, TRUSTED_COHORT),
    )
    return frozenset(a for a in (_addr(r["address"]) for r in rows) if a)


def tracked_set(conn: Any, *, routers: Iterable[str] = ()) -> tuple[str, ...]:
    """``trusted_copy`` robinhood wallets (DATABASE cohort) minus routing bots.

    The caller reports every trusted wallet this drops (:func:`excluded_trusted`); a
    curated wallet must never vanish from the stream without a trace.
    """
    drop = frozenset(a for a in (_addr(r) for r in routers) if a)
    return tuple(sorted(cohort_wallets(conn) - drop))


def excluded_trusted(conn: Any, routers: Iterable[str]) -> tuple[str, ...]:
    """The ``trusted_copy`` robinhood wallets the router screen removed, sorted."""
    drop = frozenset(a for a in (_addr(r) for r in routers) if a)
    return tuple(sorted(cohort_wallets(conn) & drop))


def _fee(receipt: Mapping[str, Any]) -> int | None:
    """Gas actually paid: ``gasUsed x effectiveGasPrice``. On Robinhood (Arbitrum Nitro)
    ``gasUsed`` already carries the L1 component. MEASURED 2026-10-04: a buy's balance
    change plus this fee equals ``-value`` to the wei on every sampled EOA buy."""
    used = _hex_int(receipt.get("gasUsed"))
    price = _hex_int(receipt.get("effectiveGasPrice"))
    if used is None or price is None:
        return None
    return used * price


def native_delta(
    wallet: str,
    tx: str,
    *,
    balance_before: Any,
    balance_after: Any,
    block: Mapping[str, Any] | None,
    receipts: Mapping[str, Mapping[str, Any]],
) -> tuple[int | None, str]:
    """``(wei, reason)``: the wallet's ETH change in ``tx``, gas added back; ``None`` if unsure.

    The balance is read at the block before and the block of the trade, so it covers the
    WHOLE block. It is attributed to this one transaction only when nothing else in the
    block can explain any of it:

    * the wallet SENT this transaction (otherwise someone else paid the gas, and an
      ERC-4337 / relayer trade moves the wallet's ETH in ways a balance cannot separate);
    * every other transaction the wallet sent in the block is a zero-value ``approve``
      (MEASURED: router sells are commonly preceded by one in the same block), whose fee
      is added back from its own receipt in ``receipts``;
    * no transaction in the block sends the wallet ETH directly.

    An internal ETH transfer to the wallet from SOMEONE ELSE's transaction in the same
    block cannot be seen this way; on ~0.1 s blocks that is rare, and it is the one
    residual error this measure can carry.
    """
    b0, b1 = _hex_int(balance_before), _hex_int(balance_after)
    if b0 is None or b1 is None or not isinstance(block, Mapping):
        return None, "unreadable"
    txs = block.get("transactions")
    if not isinstance(txs, list) or not all(isinstance(t, Mapping) for t in txs):
        return None, "block_without_transactions"
    mine = [t for t in txs if _addr(t.get("from")) == wallet]
    if not any(str(t.get("hash") or "").lower() == tx for t in mine):
        return None, "not_sender"
    for t in txs:
        if _addr(t.get("to")) == wallet and _addr(t.get("from")) != wallet and (_hex_int(t.get("value")) or 0) > 0:
            return None, "eth_in_same_block"
    fees = 0
    for t in mine:
        h = str(t.get("hash") or "").lower()
        if h != tx:
            data = str(t.get("input") or "").lower()
            if (_hex_int(t.get("value")) or 0) != 0 or not data.startswith(SELECTOR_APPROVE):
                return None, "other_tx_in_block"
        fee = _fee(receipts.get(h) or {})
        if fee is None:
            return None, "fee_unreadable"
        fees += fee
    return b1 - b0 + fees, "ok"


def _dec_str(value: Decimal) -> str:
    """Same text shape the Pons poller writes (``str(Decimal)``)."""
    return str(value)


def price_fields(
    *, native_wei: int | None, net_atoms: int, decimals: int | None, eth_usd: Decimal | None
) -> tuple[str | None, str | None]:
    """``(usd_value, price_usd)`` exactly as ``robinhood.parse_curve_trade`` computes them.

    ``usd_value`` = wei / 1e18 x ETH/USD; ``price_usd`` = usd_value / (atoms / 10^decimals).
    Either is ``None`` when its inputs are: an unknown ETH price, an unmeasured native
    amount, an unknown token decimals. Never 0 for unknown (``lanes._net_buyers`` reads an
    unpriced fill as no conviction, and an invented number would manufacture some).
    """
    if native_wei is None or native_wei <= 0 or eth_usd is None or eth_usd <= 0:
        return None, None
    usd = (Decimal(native_wei) / WEI) * eth_usd
    price: Decimal | None = None
    if decimals is not None and 0 <= decimals <= 36 and net_atoms > 0:
        try:
            price = usd / (Decimal(net_atoms) / (Decimal(10) ** int(decimals)))
        except (InvalidOperation, ZeroDivisionError):
            price = None
    return _dec_str(usd), (_dec_str(price) if price is not None else None)


def existing_rows(
    conn: Any, *, wallet: str, token: str, tx: str, ts_ms: int, window_ms: int = DEDUPE_WINDOW_MS
) -> list[dict[str, Any]]:
    """Rows any feed already wrote for this (tx, wallet, token), on either side.

    Seeks ``idx_swaps_wallet`` (chain, wallet, ts_ms) over a bounded window -- never a
    scan of ``swaps`` -- then compares the hash case-insensitively.
    """
    rows = fetch_all(
        conn,
        "SELECT source, side, amount_token, tx FROM swaps "
        "WHERE chain = ? AND wallet = ? AND ts_ms BETWEEN ? AND ? AND token = ?",
        (CHAIN.value, wallet, int(ts_ms) - int(window_ms), int(ts_ms) + int(window_ms), token),
    )
    return [r for r in rows if str(r.get("tx") or "").lower() == tx]


_INSERT = (
    "INSERT OR IGNORE INTO swaps (chain, tx, slot, block_index, ts_ms, wallet, token, side, "
    "amount_token, amount_native, amount_quote, quote_mint, price_usd, usd_value, program, source) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)
ROW_KEYS: tuple[str, ...] = (
    "chain", "tx", "slot", "block_index", "ts_ms", "wallet", "token", "side", "amount_token",
    "amount_native", "amount_quote", "quote_mint", "price_usd", "usd_value", "program", "source",
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
    """An enrichment read failed. The message is already redacted."""


RpcBatch = Callable[[Sequence[tuple[str, list[Any]]]], Awaitable[list[Any]]]


class HttpRpc:
    """Batched JSON-RPC over HTTPS. ``None`` per call that answered with an error."""

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

    async def batch(self, calls: Sequence[tuple[str, list[Any]]]) -> list[Any]:
        if not calls:
            return []
        body = [{"jsonrpc": "2.0", "id": i + 1, "method": m, "params": list(p)} for i, (m, p) in enumerate(calls)]
        for m, _ in calls:
            self.calls[m] += 1
        try:
            client = await self._http()
            resp = await client.post(self.url, json=body)
            data = resp.json()
        except Exception as exc:  # noqa: BLE001 - every failure is one redacted error
            raise RpcFailure(aws.redact(f"{type(exc).__name__}: {exc}", self.url)) from None
        if not isinstance(data, list):
            raise RpcFailure(aws.redact(f"non-batch answer: {str(data)[:200]}", self.url))
        by_id = {item.get("id"): item for item in data if isinstance(item, Mapping)}
        return [(by_id.get(i + 1) or {}).get("result") for i in range(len(calls))]

    def estimated_cu(self) -> int:
        return sum(CU_PER_CALL.get(m, 20) * n for m, n in self.calls.items())

    async def close(self) -> None:
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.aclose()
            self._client = None


# --------------------------------------------------------------------------------------
# the engine
# --------------------------------------------------------------------------------------


@dataclass
class TokenInfo:
    decimals: int | None = None
    pair_token: str | None = None
    quote_is_native: bool | None = None
    launchpad: str | None = None
    fetched_ms: int = 0


@dataclass
class Outcome:
    """What happened to one (tx, wallet, token) trade. Returned for tests and the scratch run."""

    tx: str
    wallet: str
    token: str
    side: str
    status: str  # written | matched_existing | duplicate | unconfirmed
    row: dict[str, Any] | None = None
    matched_sources: tuple[str, ...] = ()
    quote_basis: str | None = None
    venue_program: str | None = None
    native_reason: str | None = None
    recv_latency_ms: int | None = None
    write_latency_ms: int | None = None


@dataclass
class StreamStats:
    triggers: int = 0
    txs: int = 0
    receipts_missing: int = 0
    trades: int = 0
    written: int = 0
    duplicates: int = 0
    unconfirmed: int = 0
    unpriced_written: int = 0
    errors: int = 0
    matched: Counter[str] = field(default_factory=Counter)
    quote_basis: Counter[str] = field(default_factory=Counter)
    native_reasons: Counter[str] = field(default_factory=Counter)
    recv_latency_ms: deque[int] = field(default_factory=lambda: deque(maxlen=2000))
    write_latency_ms: deque[int] = field(default_factory=lambda: deque(maxlen=2000))
    tracked: int = 0
    routers_excluded: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        def pct(v: Sequence[int], q: float) -> int | None:
            s = sorted(v)
            return s[min(len(s) - 1, int(q * len(s)))] if s else None

        return {
            "triggers": self.triggers, "txs": self.txs, "receipts_missing": self.receipts_missing,
            "trades": self.trades, "written": self.written, "duplicates": self.duplicates,
            "unconfirmed": self.unconfirmed, "unpriced_written": self.unpriced_written,
            "errors": self.errors, "matched": dict(self.matched),
            "quote_basis": dict(self.quote_basis), "native_reasons": dict(self.native_reasons),
            "recv_latency_ms_p50": pct(self.recv_latency_ms, 0.5),
            "recv_latency_ms_p90": pct(self.recv_latency_ms, 0.9),
            "write_latency_ms_p50": pct(self.write_latency_ms, 0.5),
            "write_latency_ms_p90": pct(self.write_latency_ms, 0.9),
            "tracked": self.tracked, "routers_excluded": list(self.routers_excluded),
        }


def _clock_ms() -> int:
    return time.time_ns() // 1_000_000


class WalletStream:
    """Receipt-driven processing of tracked-wallet trades. Every dependency is injectable.

    ``reader`` answers the wallets/tokens/existing-rows questions; ``writer`` takes the rows
    and events. In production they are the same connection; the scratch run on the box
    reads the live database read-only and writes a scratch copy of the schema.
    """

    def __init__(
        self,
        rpc: RpcBatch,
        *,
        reader: Any,
        writer: Any,
        eth_usd: Callable[[], Decimal | None],
        clock_ms: Callable[[], int] = _clock_ms,
        receipt_retries: Sequence[float] = (0.3, 0.6, 1.2, 2.4, 4.8),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        token_ttl_ms: int = 600_000,
        on_written: Callable[[Outcome], None] | None = None,
    ) -> None:
        self.rpc = rpc
        self.reader = reader
        self.writer = writer
        self.eth_usd = eth_usd
        self.clock_ms = clock_ms
        self.receipt_retries = tuple(receipt_retries)
        self.sleep = sleep
        self.token_ttl_ms = int(token_ttl_ms)
        self.on_written = on_written
        self.stats = StreamStats()
        self.wallets: frozenset[str] = frozenset()
        self._tokens: dict[str, TokenInfo] = {}
        self._decimals: dict[str, int | None] = {}
        self._decimals_failed_ms: dict[str, int] = {}
        self.last_block: int | None = None
        self.router_evidence: dict[str, dict[str, Any]] = {}

    # ---------------------------------------------------------------- reads

    async def _receipt(self, tx: str) -> Mapping[str, Any] | None:
        for delay in (0.0, *self.receipt_retries):
            if delay:
                await self.sleep(delay)
            try:
                (got,) = await self.rpc([("eth_getTransactionReceipt", [tx])])
            except RpcFailure as exc:
                log.debug("rh_wallets: receipt %s failed: %s", tx[:12], exc)
                continue
            if isinstance(got, Mapping) and isinstance(got.get("logs"), list):
                return got
        return None

    async def _block_ts_ms(self, block: int) -> int | None:
        try:
            (got,) = await self.rpc([("eth_getBlockByNumber", [hex(block), False])])
        except RpcFailure:
            return None
        ts = _hex_int(got.get("timestamp")) if isinstance(got, Mapping) else None
        return ts * 1000 if ts else None

    def _token_info(self, token: str) -> TokenInfo:
        now = self.clock_ms()
        hit = self._tokens.get(token)
        if hit is not None and now - hit.fetched_ms < self.token_ttl_ms:
            return hit
        info = TokenInfo(fetched_ms=now)
        row = fetch_one(
            self.reader,
            "SELECT decimals, launchpad, meta_json FROM tokens WHERE chain = ? AND address = ?",
            (CHAIN.value, token),
        )
        if row:
            info.decimals = int(row["decimals"]) if row.get("decimals") is not None else None
            info.launchpad = row.get("launchpad")
            meta = jload(row.get("meta_json"), {}) or {}
            info.pair_token = _addr(meta.get("pair_token"))
            qin = meta.get("quote_is_native")
            info.quote_is_native = qin if isinstance(qin, bool) else None
        self._tokens[token] = info
        return info

    async def _decimals_of(self, token: str, info: TokenInfo) -> int | None:
        """The registry's decimals, else ``decimals()`` on chain, cached. Never assumed."""
        if info.decimals is not None:
            return info.decimals
        if token in self._decimals:
            return self._decimals[token]
        failed = self._decimals_failed_ms.get(token)
        if failed is not None and self.clock_ms() - failed < self.token_ttl_ms:
            return None
        try:
            (got,) = await self.rpc([("eth_call", [{"to": token, "data": SELECTOR_DECIMALS}, "latest"])])
        except RpcFailure:
            got = None
        value = _hex_int(got) if isinstance(got, str) and got not in ("0x", "") else None
        if value is None or not 0 <= value <= 36:
            self._decimals_failed_ms[token] = self.clock_ms()
            return None
        self._decimals[token] = value
        return value

    async def _native(self, wallet: str, tx: str, block: int, receipt: Mapping[str, Any]) -> tuple[int | None, str]:
        try:
            b0, b1, blk = await self.rpc([
                ("eth_getBalance", [wallet, hex(block - 1)]),
                ("eth_getBalance", [wallet, hex(block)]),
                ("eth_getBlockByNumber", [hex(block), True]),
            ])
        except RpcFailure:
            return None, "rpc_failed"
        receipts: dict[str, Mapping[str, Any]] = {tx: receipt}
        if isinstance(blk, Mapping) and isinstance(blk.get("transactions"), list):
            others = [str(t.get("hash") or "").lower() for t in blk["transactions"]
                      if isinstance(t, Mapping) and _addr(t.get("from")) == wallet
                      and str(t.get("hash") or "").lower() != tx
                      and (_hex_int(t.get("value")) or 0) == 0
                      and str(t.get("input") or "").lower().startswith(SELECTOR_APPROVE)]
            if others:
                try:
                    got = await self.rpc([("eth_getTransactionReceipt", [h]) for h in others])
                except RpcFailure:
                    got = []
                for h, r in zip(others, got, strict=False):
                    if isinstance(r, Mapping):
                        receipts[h] = r
        return native_delta(wallet, tx, balance_before=b0, balance_after=b1, block=blk, receipts=receipts)

    # ---------------------------------------------------------------- the trade

    async def process_tx(self, tx: str, trigger: aws.WalletTransfer | None = None) -> list[Outcome]:
        """Every confirmed trade of every tracked wallet in ``tx``, written once."""
        tx = str(tx).lower()
        self.stats.txs += 1
        receipt = await self._receipt(tx)
        if receipt is None:
            self.stats.receipts_missing += 1
            log.info("rh_wallets: no receipt for %s after retries", tx[:14])
            return []
        if _hex_int(receipt.get("status")) != 1:
            return []
        block = _hex_int(receipt.get("blockNumber"))
        if block is None:
            return []
        ts_ms = trigger.block_ts_ms if trigger is not None else None
        if ts_ms is None:
            ts_ms = await self._block_ts_ms(block)
        recv_ms = trigger.recv_ms if trigger is not None else self.clock_ms()
        logs: list[Mapping[str, Any]] = [x for x in receipt["logs"] if isinstance(x, Mapping)]
        watch = self.wallets
        records: list[aws.WalletTransfer] = []
        for entry in logs:
            records += aws.decode_transfer(entry, wallets=watch, recv_ms=recv_ms,
                                           quote_assets=QUOTE_ASSETS, chain=CHAIN.value)
        trades = [t for t in aws.fold_tx(records, venues=frozenset(STATIC_VENUES))
                  if t.basis != aws.BASIS_MINT and t.token not in QUOTE_ASSETS]
        if not trades:
            return []
        self.last_block = block if self.last_block is None else max(self.last_block, block)
        emitters = swap_emitters(logs)
        per_wallet = Counter(t.wallet for t in trades)
        sender = _addr(receipt.get("from"))
        out: list[Outcome] = []
        for trade in trades:
            self.stats.trades += 1
            try:
                outcome = await self._one(trade, tx=tx, block=block, ts_ms=ts_ms, recv_ms=recv_ms,
                                          logs=logs, emitters=emitters, receipt=receipt,
                                          sender=sender, single=per_wallet[trade.wallet] == 1,
                                          backfilled=bool(trigger.backfilled) if trigger else False)
            except Exception as exc:  # noqa: BLE001 - one bad trade never stops the feed
                self.stats.errors += 1
                log.warning("rh_wallets: %s %s failed: %s", tx[:14], trade.wallet[:10], exc)
                continue
            out.append(outcome)
        return out

    async def _one(
        self,
        t: aws.TxTrade,
        *,
        tx: str,
        block: int,
        ts_ms: int | None,
        recv_ms: int,
        logs: Sequence[Mapping[str, Any]],
        emitters: Mapping[str, str],
        receipt: Mapping[str, Any],
        sender: str | None,
        single: bool,
        backfilled: bool,
    ) -> Outcome:
        program, venue = venue_of(logs, t.token, t.wallet, t.side, emitters)
        info = self._token_info(t.token)
        native_wei: int | None = None
        native_asset: str | None = None  # which native form paid: ETH (zero address) or WETH
        amount_quote: int | None = None
        quote_mint: str | None = None
        quote_basis: str | None = None
        native_reason: str | None = None

        # 1. The Pons curve's own event: the exact quote the Pons poller would record.
        if program == PROGRAM_PONS and venue is not None and info.pair_token is not None:
            q = curve_quote(logs, venue, t.side, t.net_atoms)
            if q is not None:
                quote_basis = QB_CURVE
                if info.pair_token in NATIVE_QUOTES:
                    native_wei, native_asset = q, info.pair_token
                else:
                    amount_quote, quote_mint = q, info.pair_token
        # 2. A quote-asset leg the other way in the same transaction.
        if quote_basis is None and t.basis in (aws.BASIS_PAID_QUOTE, aws.BASIS_RECEIVED_QUOTE) \
                and t.quote_token is not None and t.quote_atoms:
            if t.quote_token in NATIVE_QUOTES:
                native_wei, native_asset, quote_basis = int(t.quote_atoms), t.quote_token, QB_WETH
            else:
                amount_quote, quote_mint, quote_basis = int(t.quote_atoms), t.quote_token, QB_QUOTE
        # 3. Native ETH, measured from the balance -- only for the sender, one token, no
        #    other quote leg, and only in the direction the trade went.
        if quote_basis is None and sender == t.wallet and single:
            wei, native_reason = await self._native(t.wallet, tx, block, receipt)
            if wei is not None and ((t.side == "buy" and wei < 0) or (t.side == "sell" and wei > 0)):
                native_wei, native_asset, quote_basis = abs(wei), aws.ZERO_ADDRESS, QB_NATIVE
            elif wei is not None:
                native_reason = "wrong_direction"
            self.stats.native_reasons[native_reason or "none"] += 1

        if quote_basis is None and program is None:
            self.stats.unconfirmed += 1
            return Outcome(tx, t.wallet, t.token, t.side, "unconfirmed", native_reason=native_reason)

        # Tape consistency: a token whose Pons curve quotes a non-ETH pair already carries
        # PAIR-token atoms in amount_native (robinhood.parse_curve_trade). Wei beside them
        # would mix units on one tape, so measured ETH moves to amount_quote (base units of
        # the named native form, migration 027) instead. Its USD value is still real.
        usd_wei = native_wei
        if native_wei is not None and info.quote_is_native is False:
            amount_quote, quote_mint = native_wei, native_asset
            native_wei = None
        decimals = await self._decimals_of(t.token, info) if usd_wei else info.decimals
        eth_usd = self.eth_usd() if usd_wei else None
        usd_value, price_usd = price_fields(native_wei=usd_wei, net_atoms=t.net_atoms,
                                            decimals=decimals, eth_usd=eth_usd)
        when = int(ts_ms) if ts_ms is not None else int(recv_ms)
        row = {
            "chain": CHAIN.value,
            "tx": tx,
            "slot": block,
            "block_index": _first_log_index(logs, t.token, t.wallet),
            "ts_ms": when,
            "wallet": t.wallet,
            "token": t.token,
            "side": t.side,
            "amount_token": str(int(t.net_atoms)),
            "amount_native": str(native_wei) if native_wei is not None else None,
            "amount_quote": str(amount_quote) if amount_quote is not None else None,
            "quote_mint": quote_mint,
            "price_usd": price_usd,
            "usd_value": usd_value,
            "program": program,
            "source": SOURCE,
        }
        recv_latency = recv_ms - ts_ms if ts_ms is not None else None
        existing = existing_rows(self.reader, wallet=t.wallet, token=t.token, tx=tx, ts_ms=when)
        if existing:
            sources = tuple(sorted({str(r["source"]) for r in existing}))
            if sources == (SOURCE,):
                self.stats.duplicates += 1
                return Outcome(tx, t.wallet, t.token, t.side, "duplicate", row=row, matched_sources=sources)
            for s in sources:
                self.stats.matched[s] += 1
            return Outcome(tx, t.wallet, t.token, t.side, "matched_existing", row=row,
                           matched_sources=sources, quote_basis=quote_basis, venue_program=program,
                           recv_latency_ms=recv_latency)
        written_ms = self.clock_ms()
        write_latency = written_ms - ts_ms if ts_ms is not None else None
        extra = {
            "quote_basis": quote_basis,
            "venue": venue,
            "confirm_basis": t.basis,
            "native_reason": native_reason,
            "token_decimals": decimals,
            "eth_usd": str(eth_usd) if eth_usd is not None else None,
            "block_ts_ms": ts_ms,
            "recv_ms": recv_ms,
            "written_ms": written_ms,
            "recv_latency_ms": recv_latency,
            "write_latency_ms": write_latency,
            "backfilled": backfilled,
        }
        if not write_trade(self.writer, row, extra):
            self.stats.duplicates += 1
            return Outcome(tx, t.wallet, t.token, t.side, "duplicate", row=row)
        self.stats.written += 1
        self.stats.quote_basis[quote_basis or "venue_only"] += 1
        if usd_value is None:
            self.stats.unpriced_written += 1
        if recv_latency is not None and not backfilled:
            self.stats.recv_latency_ms.append(recv_latency)
        if write_latency is not None and not backfilled:
            self.stats.write_latency_ms.append(write_latency)
        outcome = Outcome(tx, t.wallet, t.token, t.side, "written", row=row, quote_basis=quote_basis,
                          venue_program=program, native_reason=native_reason,
                          recv_latency_ms=recv_latency, write_latency_ms=write_latency)
        _note_written(self.writer)
        if self.on_written is not None:
            with contextlib.suppress(Exception):
                self.on_written(outcome)
        return outcome

    # ---------------------------------------------------------------- routers

    async def seed_routers(self, candidates: Iterable[str], head: int,
                           lookback_blocks: int = ROUTER_LOOKBACK_BLOCKS,
                           chunk_blocks: int = 100_000) -> frozenset[str]:
        """Classify routing bots from the candidates' own Transfer logs over the lookback.

        The per-wallet evidence is kept on :attr:`router_evidence` so the caller can say
        WHY a trusted wallet was excluded.
        """
        if not aws.normalize_wallets(candidates):
            self.router_evidence = {}
            return frozenset()
        subs = aws.build_subscriptions(candidates)
        start = max(0, int(head) - int(lookback_blocks))
        records: list[aws.WalletTransfer] = []
        watch = frozenset(aws.normalize_wallets(candidates))
        for lo in range(start, int(head) + 1, max(1, int(chunk_blocks))):
            hi = min(int(head), lo + int(chunk_blocks) - 1)
            got = await self.rpc([("eth_getLogs", [sub.logs_filter(lo, hi)]) for sub in subs])
            for logs in got:
                for entry in logs or []:
                    records += aws.decode_transfer(entry, wallets=watch, recv_ms=0,
                                                   quote_assets=QUOTE_ASSETS, chain=CHAIN.value)
        dedup = {r.key: r for r in records}
        routed = routed_by_wallet(dedup.values())
        self.router_evidence = router_evidence(routed)
        return classify_routers(routed)


def _first_log_index(logs: Sequence[Mapping[str, Any]], token: str, wallet: str) -> int | None:
    """Log index of the first Transfer of ``token`` touching ``wallet`` (Pons rows carry the
    CurveBuy's index; either way it orders rows inside a block)."""
    best: int | None = None
    for entry in logs:
        if _addr(entry.get("address")) != token or _topic0(entry) != aws.TRANSFER_TOPIC:
            continue
        topics = entry.get("topics") or []
        if len(topics) != 3 or wallet not in (aws.topic_address(topics[1]), aws.topic_address(topics[2])):
            continue
        idx = _hex_int(entry.get("logIndex"))
        if idx is not None and (best is None or idx < best):
            best = idx
    return best


def _note_written(conn: Any) -> None:
    try:
        from kaiba.ingest.runner import note_events  # lazy: runner imports this module

        note_events(FEED, 1, conn)
    except Exception as exc:  # noqa: BLE001 - bookkeeping never breaks the feed
        log.debug("rh_wallets: ingest_status note failed: %s", exc)


# --------------------------------------------------------------------------------------
# the listener
# --------------------------------------------------------------------------------------


def alchemy_url() -> str | None:
    """The configured Robinhood endpoint, only if it is a websocket-capable Alchemy URL.

    The public RPC refuses ``wss://`` (HTTP 400, ``robinhood.py`` module doc), so anything
    else is "not configured" rather than a socket to retry forever.
    """
    from kaiba.core.config import get_settings

    url = str(get_settings().rpc_for(CHAIN) or "")
    return url if "/v2/" in url else None


def _resume_from_kv(conn: Any, head: int) -> int | None:
    """The last block this feed processed, if recent enough to backfill from on start.

    Closes the gap a service restart would otherwise leave: the socket backfills from here
    (``alchemy_ws.stream(from_block=...)``) and our own rows already written read back as
    duplicates. Older than :data:`RESUME_MAX_BLOCKS` is ignored, not half-replayed.
    """
    try:
        row = fetch_one(conn, "SELECT value FROM kv WHERE key = ?", (KV_RESUME,))
    except Exception:  # noqa: BLE001
        return None
    text = str((row or {}).get("value") or "")
    value = int(text) if text.isdigit() else None
    if value is None or value > head or head - value > RESUME_MAX_BLOCKS:
        return None
    return value


def _save_resume(conn: Any, block: int) -> None:
    try:
        conn.execute(
            "INSERT INTO kv (key, value, updated_ms) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_ms = excluded.updated_ms",
            (KV_RESUME, str(int(block)), now_ms()),
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("rh_wallets: resume point not saved: %s", exc)


async def run(
    stop: asyncio.Event | None = None,
    *,
    url: str | None = None,
    reader: Any = None,
    writer: Any = None,
    rpc: Any = None,
    eth_usd: Callable[[], Decimal | None] | None = None,
    refresh_s: float = REFRESH_S,
    connect: Callable[[str], Any] | None = None,
    resume: bool = True,
    on_written: Callable[[Outcome], None] | None = None,
    on_status: Callable[[dict[str, Any]], None] | None = None,
    engine_out: list[WalletStream] | None = None,
) -> dict[str, Any]:
    """Follow the tracked wallets until ``stop``. Shaped for ``kaiba.ingest.runner``."""
    stop = stop or asyncio.Event()
    url = url or alchemy_url()
    if not url:
        log.warning("rh_wallets: no Alchemy websocket endpoint configured for robinhood; idle")
        return {"idle": "no websocket endpoint"}
    w = writer if writer is not None else get_conn()
    r = reader if reader is not None else w
    client = rpc or HttpRpc(url)
    if eth_usd is None:
        price = rh.EthPrice()

        def eth_usd() -> Decimal | None:
            return price.get(conn=w)

    engine = WalletStream(client.batch, reader=r, writer=w, eth_usd=eth_usd, on_written=on_written)
    if engine_out is not None:
        engine_out.append(engine)
    queue: asyncio.Queue[tuple[str, aws.WalletTransfer]] = asyncio.Queue(maxsize=10_000)
    seen_tx = aws._RecentKeys(50_000)  # noqa: SLF001 - shared bounded set, by design
    feed_stats = aws.FeedStats()

    async def head_block() -> int | None:
        try:
            (got,) = await client.batch([("eth_blockNumber", [])])
        except RpcFailure as exc:
            log.warning("rh_wallets: head unreadable: %s", exc)
            return None
        return _hex_int(got)

    async def choose_set(head: int | None) -> tuple[str, ...]:
        candidates = tracked_set(r)
        routers: frozenset[str] = frozenset()
        if head is not None:
            try:
                routers = await engine.seed_routers(candidates, head)
            except RpcFailure as exc:
                log.warning("rh_wallets: router screen skipped: %s", exc)
        wallets = tracked_set(r, routers=routers)
        excluded = excluded_trusted(r, routers)
        engine.stats.routers_excluded = excluded
        engine.stats.tracked = len(wallets)
        if excluded:
            # Never silent: a curated wallet the screen drops is named, with the evidence,
            # on every refresh that drops it.
            emit(EventKind.SYSTEM, {
                "component": f"ingest.{FEED}", "status": "router_excluded",
                "wallets": [{"address": a, **engine.router_evidence.get(a, {})} for a in excluded],
                "rule": {"min_txs": ROUTER_MIN_TXS, "zero_net_share": ROUTER_ZERO_NET_SHARE,
                         "lookback_blocks": ROUTER_LOOKBACK_BLOCKS},
            }, chain=CHAIN, level="warn", conn=w)
            log.warning("rh_wallets: router screen excluded %d trusted wallet(s): %s",
                        len(excluded), ", ".join(excluded))
        return wallets

    async def worker() -> None:
        while True:
            tx, trig = await queue.get()
            try:
                await engine.process_tx(tx, trig)
            except Exception as exc:  # noqa: BLE001 - one tx never stops the feed
                engine.stats.errors += 1
                log.warning("rh_wallets: %s failed: %s", tx[:14], aws.redact(exc, url))
            finally:
                queue.task_done()

    def status(payload: dict[str, Any]) -> None:
        if on_status is not None:
            with contextlib.suppress(Exception):
                on_status(payload)

    head = await head_block()
    wallets = await choose_set(head)
    engine.wallets = frozenset(wallets)
    resume_from = _resume_from_kv(w, head) if (resume and head is not None) else None
    emit(EventKind.SYSTEM, {"component": f"ingest.{FEED}", "status": "starting", "chain": CHAIN.value,
                            "tracked": len(wallets), "routers_excluded": list(engine.stats.routers_excluded),
                            "resume_from_block": resume_from, "endpoint": aws.mask_url(url)},
         chain=CHAIN, conn=w)
    task = asyncio.ensure_future(worker())
    last_stats = time.monotonic()
    last_saved = 0.0
    try:
        while not stop.is_set():
            if not wallets:
                # No trusted_copy wallet (or every one screened out): nothing to subscribe
                # to. Say so and look again at the next refresh instead of crash-looping
                # the supervisor on alchemy_ws's empty-list refusal.
                emit(EventKind.SYSTEM, {"component": f"ingest.{FEED}", "status": "idle",
                                        "reason": "no trusted_copy robinhood wallet to track",
                                        "routers_excluded": list(engine.stats.routers_excluded)},
                     chain=CHAIN, level="warn", conn=w)
                with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=refresh_s)
                if stop.is_set():
                    break
                head = await head_block()
                wallets = await choose_set(head)
                engine.wallets = frozenset(wallets)
                resume_from = head  # nothing was tracked before this block
                continue
            gen_stop = asyncio.Event()
            pending: dict[str, Any] = {}

            async def refresher(current: tuple[str, ...], gen: asyncio.Event = gen_stop,
                                box: dict[str, Any] = pending) -> None:
                while not stop.is_set() and not gen.is_set():
                    with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
                        await asyncio.wait_for(stop.wait(), timeout=refresh_s)
                    if stop.is_set():
                        break
                    h = await head_block()
                    nxt = await choose_set(h)
                    if nxt != current and h is not None:
                        box["wallets"], box["resume"] = nxt, h
                        break
                gen.set()

            ref = asyncio.ensure_future(refresher(wallets))
            try:
                async for rec in aws.stream(url, wallets, chain=CHAIN.value, quote_assets=QUOTE_ASSETS,
                                            stop=gen_stop, connect=connect, stats=feed_stats,
                                            on_status=status, from_block=resume_from,
                                            anchor_on_subscribe=True, idle_timeout_s=IDLE_TIMEOUT_S):
                    engine.stats.triggers += 1
                    if rec.removed or not seen_tx.add(rec.tx):
                        continue
                    with contextlib.suppress(asyncio.QueueFull):
                        queue.put_nowait((rec.tx, rec))
                    if engine.last_block and time.monotonic() - last_saved > 30:
                        _save_resume(w, engine.last_block)
                        last_saved = time.monotonic()
                    if time.monotonic() - last_stats > STATS_EVERY_S:
                        last_stats = time.monotonic()
                        emit(EventKind.SYSTEM, {"component": f"ingest.{FEED}", "status": "stats",
                                                **engine.stats.as_dict(), "socket": feed_stats.to_dict(),
                                                "rpc_cu": getattr(client, "estimated_cu", lambda: None)()},
                             chain=CHAIN, conn=w)
            finally:
                ref.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await ref
            if stop.is_set():
                break
            if "wallets" in pending:
                wallets = pending["wallets"]
                engine.wallets = frozenset(wallets)
                resume_from = pending.get("resume")
                emit(EventKind.SYSTEM, {"component": f"ingest.{FEED}", "status": "resubscribing",
                                        "tracked": len(wallets), "from_block": resume_from,
                                        "routers_excluded": list(engine.stats.routers_excluded)},
                     chain=CHAIN, conn=w)
            else:
                # The socket gave up (aws.stream only returns on stop or max_attempts):
                # hand back to the supervisor, which restarts with backoff.
                break
        # Drain what was already queued, briefly, so a stop does not drop a fresh trade.
        with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
            await asyncio.wait_for(queue.join(), timeout=10.0)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        if engine.last_block:
            _save_resume(w, engine.last_block)
        if rpc is None:
            await client.close()
    return {"stats": engine.stats.as_dict(), "socket": feed_stats.to_dict(),
            "rpc_cu": getattr(client, "estimated_cu", lambda: None)()}


__all__: Sequence[str] = (
    "CHAIN", "SOURCE", "FEED", "TRUSTED_COHORT", "WETH", "USDG", "QUOTE_ASSETS",
    "NATIVE_QUOTES", "SWAP_PROGRAMS", "STATIC_VENUES", "HttpRpc", "Outcome", "RpcFailure",
    "StreamStats", "TokenInfo", "WalletStream", "alchemy_url", "classify_routers", "cohort_wallets",
    "curve_quote", "excluded_trusted", "router_evidence", "existing_rows", "native_delta", "price_fields", "routed_by_wallet", "run",
    "swap_emitters", "token_edges", "tracked_set", "venue_of", "write_trade",
)
