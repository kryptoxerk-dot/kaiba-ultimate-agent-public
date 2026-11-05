"""Stockereum (Ethereum mainnet) launches and trades, from the platform's own event logs.

Owner 2026-10-09: "People are starting to trade on ERC Ethereum ... gather wallets that made huge
gains ... Stockereum and programmable launchpads". Stockereum launches fixed-supply tokens into a
permanent Uniswap v4 position owned by its hook, paired with ETH or tokenized stocks.

DECODED 2026-10-09 against mainnet transactions (topic2 / topic1 equal ``tx.from`` in every
sampled trade):

* factory ``0xc6B0...977B`` ``LAUNCH`` (topic0 ``0xcc4bd3d5...``): token, creator, quote asset;
* factory ``DEV_BUY`` (``0x84d429ed...``): token, buyer; data = ETH in, tokens out;
* router ``0xcdf8...3767`` ``BUY`` (``0xbf77fd13...``): trader, token; data = ETH in, tokens out;
* router ``SELL`` (``0x16dd1695...``): trader, token; data = tokens in, ETH out.

About 900 router trades and 270 launches a day. Trades sent through other routers (Uniswap's
Universal Router, bots) do not emit these events and are NOT seen: the tape is the platform's
own flow, which is where its users trade.

RPC: public Ethereum endpoints (the owner's Alchemy plan hit its monthly cap on 2026-10-09).
Tenderly's gateway served 50k-block ranges; MEV Blocker 10k. Block timestamps are interpolated
at 12 s per block between two fetched anchors (post-merge slots are exactly 12 s; a missed slot
skews a timestamp by 12 s, irrelevant for holding-time grading).
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

CHAIN = "eth"
SOURCE = "stockereum:events"
LAUNCHPAD = "stockereum"
FACTORY = "0xc6b080ded03c3382476a76345e79f82bd480977b"
ROUTER = "0xcdf832d2c11da16055bb6c6145cf38edd7233767"
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
TOPIC_LAUNCH = "0xcc4bd3d58f3b2210"
TOPIC_DEV_BUY = "0x84d429ed8af1c9cf"
TOPIC_BUY = "0xbf77fd13a39d"
TOPIC_SELL = "0x16dd16959a05"
RPCS = ("https://mainnet.gateway.tenderly.co", "https://rpc.mevblocker.io")
SPANS = {"https://mainnet.gateway.tenderly.co": 20_000, "https://rpc.mevblocker.io": 5_000}
SECONDS_PER_BLOCK = 12
KV_CURSOR = "stockereum:block_cursor"
#: Stockereum is a 2026 launchpad; a backfill walks back this many blocks at most (~90 days).
MAX_BACKFILL_BLOCKS = 650_000


@dataclass(frozen=True)
class Trade:
    tx: str
    block: int
    log_index: int
    wallet: str
    token: str
    side: str            # buy | sell
    eth_wei: int
    token_atoms: int
    kind: str            # router | dev_buy


@dataclass(frozen=True)
class Launch:
    tx: str
    block: int
    token: str
    creator: str
    quote: str


def _addr(topic: str) -> str:
    return "0x" + topic[-40:].lower()


def _word(data: str, i: int) -> int:
    return int(data[2 + 64 * i: 2 + 64 * (i + 1)], 16)


def decode(logs: Iterable[dict[str, Any]]) -> tuple[list[Trade], list[Launch]]:
    """Factory + router logs -> trades and launches. Unknown topics are ignored."""
    trades: list[Trade] = []
    launches: list[Launch] = []
    for lg in logs:
        topics = lg.get("topics") or []
        if not topics:
            continue
        t0 = topics[0].lower()
        src = str(lg.get("address", "")).lower()
        block = int(lg["blockNumber"], 16)
        idx = int(lg.get("logIndex", "0x0"), 16)
        tx = lg["transactionHash"]
        data = lg.get("data", "0x")
        try:
            if src == FACTORY and t0.startswith(TOPIC_LAUNCH) and len(topics) >= 4:
                launches.append(Launch(tx, block, _addr(topics[1]), _addr(topics[2]), _addr(topics[3])))
            elif src == FACTORY and t0.startswith(TOPIC_DEV_BUY) and len(topics) >= 3:
                trades.append(Trade(tx, block, idx, _addr(topics[2]), _addr(topics[1]), "buy",
                                    _word(data, 0), _word(data, 1), "dev_buy"))
            elif src == ROUTER and t0.startswith(TOPIC_BUY) and len(topics) >= 3:
                trades.append(Trade(tx, block, idx, _addr(topics[1]), _addr(topics[2]), "buy",
                                    _word(data, 0), _word(data, 1), "router"))
            elif src == ROUTER and t0.startswith(TOPIC_SELL) and len(topics) >= 3:
                trades.append(Trade(tx, block, idx, _addr(topics[1]), _addr(topics[2]), "sell",
                                    _word(data, 1), _word(data, 0), "router"))
        except (ValueError, IndexError):
            continue
    return trades, launches


class Rpc:
    """Public endpoints in order; a failing one is skipped for the rest of the call."""

    def __init__(self, urls: Iterable[str] = RPCS, *, pause_s: float = 0.25,
                 opener: Callable[..., Any] = urllib.request.urlopen) -> None:
        self.urls = list(urls)
        self.pause_s = pause_s
        self.opener = opener

    def call(self, method: str, params: list[Any], *, url: str | None = None) -> Any:
        last: Exception | None = None
        for u in ([url] if url else self.urls):
            req = urllib.request.Request(
                u, data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
                headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0 kaiba"})
            try:
                with self.opener(req, timeout=30) as resp:
                    out = json.load(resp)
                time.sleep(self.pause_s)
                if "result" in out:
                    return out["result"]
                last = RuntimeError(str(out.get("error"))[:200])
            except (urllib.error.URLError, TimeoutError, ValueError) as exc:
                last = exc
        raise RuntimeError(f"{method} failed on every endpoint: {last}")

    def logs(self, frm: int, to: int) -> list[dict[str, Any]]:
        """Factory + router logs in [frm, to], split to each endpoint's range limit."""
        for u in self.urls:
            span = SPANS.get(u, 5_000)
            try:
                b = frm
                got: list[dict[str, Any]] = []
                while b <= to:
                    e = min(to, b + span - 1)
                    got += self.call("eth_getLogs", [{"address": [FACTORY, ROUTER], "fromBlock": hex(b), "toBlock": hex(e)}], url=u)
                    b = e + 1
                return got
            except RuntimeError as exc:
                log.warning("stockereum: %s failed for %d-%d (%s); trying the next endpoint", u, frm, to, exc)
        raise RuntimeError("no endpoint served the log range")

    def block_ts(self, block: int) -> int:
        return int(self.call("eth_getBlockByNumber", [hex(block), False])["timestamp"], 16)


def write(conn: Any, trades: list[Trade], launches: list[Launch], ts_of: Callable[[int], int]) -> dict[str, int]:
    """Insert trades into ``swaps`` (chain eth) and launches into ``tokens``. Idempotent."""
    now = int(time.time() * 1000)
    n_tr = n_lp = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for t in trades:
            cur = conn.execute(
                "INSERT OR IGNORE INTO swaps (chain, tx, ts_ms, wallet, token, side, amount_token, amount_native, "
                "source, program, block_index) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (CHAIN, t.tx, ts_of(t.block) * 1000, t.wallet, t.token, t.side, str(t.token_atoms),
                 str(t.eth_wei), SOURCE, f"stockereum:{t.kind}", t.log_index))
            n_tr += cur.rowcount or 0
        for lp in launches:
            cur = conn.execute(
                "INSERT OR IGNORE INTO tokens (chain, address, decimals, launchpad, first_seen_ms) VALUES (?,?,?,?,?)",
                (CHAIN, lp.token, 18, LAUNCHPAD, ts_of(lp.block) * 1000))
            n_lp += cur.rowcount or 0
            conn.execute(
                "INSERT INTO kv (key, value, updated_ms) VALUES (?,?,?) ON CONFLICT(key) DO NOTHING",
                (f"stockereum:creator:{lp.token}", json.dumps({"creator": lp.creator, "quote": lp.quote, "tx": lp.tx}), now))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"trades_written": n_tr, "launches_written": n_lp}


def run(conn: Any, rpc: Rpc | None = None, *, deadline: float | None = None, chunk: int = 20_000,
        clock: Callable[[], float] = time.time) -> dict[str, Any]:
    """Advance the block cursor to the head (first run: backfill up to MAX_BACKFILL_BLOCKS)."""
    rpc = rpc or Rpc()
    head = int(rpc.call("eth_blockNumber", []), 16) - 3          # a few blocks of reorg slack
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (KV_CURSOR,)).fetchone()
    cursor = int(row[0]) if row and row[0] else head - MAX_BACKFILL_BLOCKS
    rep = {"from": cursor + 1, "to": head, "trades_written": 0, "launches_written": 0, "chunks": 0}
    while cursor < head:
        if deadline is not None and rep["chunks"] and clock() >= deadline:
            break
        frm, to = cursor + 1, min(head, cursor + chunk)
        logs = rpc.logs(frm, to)
        trades, launches = decode(logs)
        t_frm, t_to = rpc.block_ts(frm), rpc.block_ts(to)
        rate = (t_to - t_frm) / max(1, to - frm)

        def ts_of(b: int, _f: int = frm, _t: int = t_frm, _r: float = rate) -> int:
            return int(_t + (b - _f) * _r)
        got = write(conn, trades, launches, ts_of)
        conn.execute("INSERT INTO kv (key, value, updated_ms) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE "
                     "SET value = excluded.value, updated_ms = excluded.updated_ms", (KV_CURSOR, str(to), int(clock() * 1000)))
        rep["trades_written"] += got["trades_written"]
        rep["launches_written"] += got["launches_written"]
        rep["chunks"] += 1
        cursor = to
    rep["cursor"] = cursor
    rep["caught_up"] = cursor >= head
    return rep


__all__ = ["Trade", "Launch", "decode", "write", "run", "Rpc", "FACTORY", "ROUTER", "CHAIN", "LAUNCHPAD"]
