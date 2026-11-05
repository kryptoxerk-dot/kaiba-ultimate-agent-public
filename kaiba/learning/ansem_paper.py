"""Direct Solana paper watch. Public RPC and quote reads only, with a private ledger."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import time
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx
import websockets
from websockets.exceptions import WebSocketException

from kaiba.core.schemas import Chain
from kaiba.ingest.feed_endpoints import (
    SOL_PUBLIC_HTTP_DEFAULT,
    SOL_PUBLIC_WS_DEFAULT,
    protection_hosts,
)
from kaiba.ingest.wallet_stream_sol import (
    MIN_QUOTE_LAMPORTS,
    QUOTE_MINTS,
    STABLE_MINTS,
    WSOL_MINT,
    is_sol_address,
    logs_mention_swap,
    mentions_filter,
    swap_program,
    tx_params,
    wallet_delta,
)

log = logging.getLogger(__name__)
QUOTE_URL = "https://lite-api.jup.ag/swap/v1/quote"
TICKETS = {WSOL_MINT: 100_000_000, **dict.fromkeys(STABLE_MINTS, 10_000_000)}
FRESH_S = 10
CHASE_BPS = 1_000
SLIPPAGE_BPS = 100
HOLD_S = 300
MAX_OPEN = 3
LEDGER_ROWS = 256
EXIT_WINDOW_S = 20


@dataclass(frozen=True)
class Buy:
    token: str
    quote_mint: str
    paid: int
    received: int


@dataclass(frozen=True)
class Quote:
    expected: int
    minimum: int
    slot: int


def decode_buy(tx: dict, wallet: str) -> Buy | None:
    meta = tx.get("meta")
    if not isinstance(meta, dict) or "err" not in meta or meta["err"] is not None:
        return None
    delta = wallet_delta(tx, wallet)
    if not delta.signer or delta.native is None or not swap_program(tx):
        return None
    base = [(mint, atoms) for mint, atoms in delta.tokens.items() if atoms and mint not in QUOTE_MINTS]
    if len(base) != 1 or base[0][1] <= 0 or not is_sol_address(base[0][0]):
        return None
    token, received = base[0]
    fee = meta.get("fee")
    if delta.fee_payer and (type(fee) is not int or fee < 0):
        return None
    sol = delta.native + (fee if delta.fee_payer else 0) + delta.tokens.get(WSOL_MINT, 0)
    stables = [(mint, atoms) for mint, atoms in delta.tokens.items() if atoms and mint in STABLE_MINTS]
    if not stables and sol <= -MIN_QUOTE_LAMPORTS:
        return Buy(token, WSOL_MINT, -sol, received)
    if len(stables) == 1 and stables[0][1] < 0 and abs(sol) < MIN_QUOTE_LAMPORTS:
        mint, spent = stables[0]
        return Buy(token, mint, -spent, received)
    return None


def exact_quote(body: dict, input_mint: str, output_mint: str, amount: int) -> Quote:
    expected = int(body["outAmount"])
    minimum = int(body["otherAmountThreshold"])
    slot = int(body["contextSlot"])
    if (
        body["inputMint"] != input_mint
        or body["outputMint"] != output_mint
        or int(body["inAmount"]) != amount
        or body["swapMode"] != "ExactIn"
        or int(body["slippageBps"]) != SLIPPAGE_BPS
        or not body["routePlan"]
        or slot <= 0
        or not 0 < minimum <= expected
        or (minimum + 1) * 10_000 < expected * (10_000 - SLIPPAGE_BPS)
    ):
        raise ValueError("quote did not match exact-in request")
    return Quote(expected, minimum, slot)


def price_ran(buy: Buy, ticket: int, minimum: int) -> bool:
    return ticket * buy.received * 10_000 > minimum * buy.paid * (10_000 + CHASE_BPS)


class Ledger:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.rows = json.loads(path.read_text()) if path.exists() else []

    def save(self, row: dict) -> None:
        rows = [r for r in self.rows if r["signature"] != row["signature"]] + [row]
        # Keep pending exits even if a burst displaces old decisions.
        open_rows = [r for r in rows if r["status"] == "open"]
        other = [r for r in rows if r["status"] != "open"]
        rows = other[-(LEDGER_ROWS - len(open_rows)):] + open_rows
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(rows, separators=(",", ":")), encoding="utf-8")
        temp.replace(self.path)
        self.rows = rows


class Watcher:
    def __init__(self, wallet: str, client: httpx.AsyncClient, ledger: Ledger) -> None:
        self.wallet = wallet
        self.client = client
        self.ledger = ledger
        self.reads: deque[float] = deque()
        self.cooldown: dict[str, float] = {}

    async def request(self, kind: str, method: str, url: str, **kwargs) -> dict:
        now = time.monotonic()
        if now < self.cooldown.get(kind, 0):
            raise ValueError(f"{kind}_cooldown")
        response = await self.client.request(method, url, **kwargs)
        if response.status_code == 429:
            retry = response.headers.get("retry-after", "")
            self.cooldown[kind] = now + (max(60, int(retry)) if retry.isdecimal() else 60)
        response.raise_for_status()
        return response.json()

    async def quote(self, input_mint: str, output_mint: str, amount: int) -> Quote:
        body = await self.request(
            "quote", "GET", QUOTE_URL,
            params={"inputMint": input_mint, "outputMint": output_mint, "amount": amount,
                    "slippageBps": SLIPPAGE_BPS},
            headers={"Cache-Control": "no-cache"},
        )
        return exact_quote(body, input_mint, output_mint, amount)

    async def receipt(self, signature: str) -> dict | None:
        for delay in (0, 0.35):
            if delay:
                await asyncio.sleep(delay)
            body = await self.request(
                "rpc", "POST", SOL_PUBLIC_HTTP_DEFAULT,
                json={"jsonrpc": "2.0", "id": 1, "method": "getTransaction",
                      "params": tx_params(signature)},
            )
            if "result" not in body:
                raise ValueError("rpc_rejected_receipt")
            if body["result"] is not None:
                return body["result"]
        return None

    async def on_notice(self, signature: str, slot: int, received: float) -> None:
        if any(r["signature"] == signature for r in self.ledger.rows):
            return
        now = time.monotonic()
        while self.reads and self.reads[0] <= now - 60:
            self.reads.popleft()
        if len(self.reads) >= 6 or time.time() - received > FRESH_S:
            log.info("skipped notification due to local budget or queue age sig=%s", signature)
            return
        disk = shutil.disk_usage(self.ledger.path.parent)
        if disk.used * 100 >= disk.total * 92:
            log.warning("paper entries paused due to disk pressure")
            return
        self.reads.append(now)
        row = {"signature": signature, "received_at": received, "notice_slot": slot}
        try:
            await self.evaluate(row)
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
            row.update(status="provider_or_evidence_unavailable", error=type(exc).__name__)
        self.ledger.save(row)
        log.info("wallet observation status=%s sig=%s", row["status"], signature)

    async def evaluate(self, row: dict) -> None:
        tx = await self.receipt(row["signature"])
        if tx is None:
            row["status"] = "receipt_unavailable"
            return
        block_time = tx.get("blockTime")
        slot = row["notice_slot"]
        if (tx.get("slot") != slot
                or row["signature"] not in tx["transaction"]["signatures"]
                or type(block_time) is not int
                or not -2 <= time.time() - block_time <= FRESH_S):
            row["status"] = "stale_or_unbound_receipt"
            return
        buy = decode_buy(tx, self.wallet)
        if buy is None:
            row["status"] = "no_unambiguous_buy"
            return
        row.update(leader=asdict(buy), block_time=block_time)
        if sum(r["status"] == "open" for r in self.ledger.rows) >= MAX_OPEN:
            row["status"] = "paper_position_cap"
            return
        ticket = TICKETS[buy.quote_mint]
        started = time.time()
        quote = await self.quote(buy.quote_mint, buy.token, ticket)
        observed = time.time()
        row.update(ticket=ticket, entry_quote=asdict(quote), entry_quote_at=observed,
                   quote_latency_ms=round((observed - started) * 1000))
        if quote.slot < slot or not -2 <= observed - block_time <= FRESH_S:
            row["status"] = "stale_quote"
        elif price_ran(buy, ticket, quote.minimum):
            row["status"] = "price_already_ran"
        else:
            row.update(status="open", exit_due_at=observed + HOLD_S)

    async def exit_once(self, row: dict) -> None:
        due = row["exit_due_at"]
        observed = time.time()
        if observed > due + EXIT_WINDOW_S:
            self.ledger.save(dict(row, status="exit_quote_missed"))
            return
        buy = row["leader"]
        try:
            quote = await self.quote(buy["token"], buy["quote_mint"], row["entry_quote"]["minimum"])
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
            self.ledger.save(dict(row, status="exit_quote_unavailable", error=type(exc).__name__))
            return
        observed = time.time()
        if observed > due + EXIT_WINDOW_S or quote.slot <= row["entry_quote"]["slot"]:
            self.ledger.save(dict(row, status="exit_quote_stale"))
            return
        self.ledger.save(dict(row, status="closed", exit_quote=asdict(quote), exit_quote_at=observed,
                              quote_pnl_atoms=quote.minimum - row["ticket"]))
        log.info("paper close sig=%s", row["signature"])

    async def work(self, queue: asyncio.Queue) -> None:
        while True:
            try:
                notice = await asyncio.wait_for(queue.get(), timeout=1)
            except TimeoutError:
                notice = None
            if notice is not None:
                await self.on_notice(*notice)
            for row in self.ledger.rows:
                if row["status"] == "open" and row["exit_due_at"] <= time.time():
                    await self.exit_once(row)


async def listen(wallet: str, queue: asyncio.Queue) -> None:
    retry = 5
    while True:
        try:
            async with websockets.connect(SOL_PUBLIC_WS_DEFAULT, open_timeout=6, max_queue=16) as ws:
                requests = {
                    1: ("logsSubscribe", [mentions_filter(wallet), {"commitment": "confirmed"}]),
                    2: ("slotSubscribe", []),
                }
                for request_id, (method, params) in requests.items():
                    await ws.send(json.dumps(dict(jsonrpc="2.0", id=request_id, method=method, params=params)))
                subscribed = {}
                last_slot = time.monotonic()
                ready = False
                while True:
                    remaining = 15 - (time.monotonic() - last_slot)
                    message = json.loads(await asyncio.wait_for(ws.recv(), timeout=max(0, remaining)))
                    received = time.time()
                    request_id = message.get("id")
                    if request_id in requests:
                        if type(message.get("result")) is not int:
                            raise ValueError("subscription rejected")
                        subscribed[request_id] = message["result"]
                        continue
                    params = message.get("params", {})
                    if message.get("method") == "slotNotification" and params.get("subscription") == subscribed.get(2):
                        slot = params["result"]["slot"]
                        if 1 in subscribed and not ready:
                            log.info("direct wallet subscription acknowledged, slot heartbeat=%s", slot)
                            ready = True
                            retry = 5
                        last_slot = time.monotonic()
                    if (message.get("method") == "logsNotification" and 1 in subscribed
                            and params.get("subscription") == subscribed[1]):
                        result = params["result"]
                        value = result["value"]
                        if value["err"] is None and logs_mention_swap(value.get("logs")) is not False:
                            try:
                                queue.put_nowait((value["signature"], result["context"]["slot"], received))
                            except asyncio.QueueFull:
                                log.warning("direct wallet queue full; skipped notification")
        except (OSError, TimeoutError, ValueError, KeyError, WebSocketException) as exc:
            log.warning("direct wallet connection unavailable (%s); reconnect in %ss", type(exc).__name__, retry)
            await asyncio.sleep(retry)
            retry = min(300, retry * 2)


async def run(wallet: str, path: Path) -> None:
    if "api.mainnet-beta.solana.com" in protection_hosts(Chain.SOL):
        raise RuntimeError("public Solana endpoint overlaps live protection")
    ledger = Ledger(path)
    queue: asyncio.Queue = asyncio.Queue(maxsize=8)
    async with httpx.AsyncClient(timeout=2.5, trust_env=False) as client, asyncio.TaskGroup() as tasks:
        watcher = Watcher(wallet, client, ledger)
        tasks.create_task(listen(wallet, queue))
        tasks.create_task(watcher.work(queue))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wallet", required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    args = parser.parse_args()
    if not is_sol_address(args.wallet):
        parser.error("invalid Solana wallet")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(run(args.wallet, args.ledger))


if __name__ == "__main__":
    main()
