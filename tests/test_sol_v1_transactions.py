"""Real version-1 Solana transactions through the lead-owned readers' parse paths.

Why (2026-10-21, ``docs/research/claude-profit-ways-2026-10-21/VERIFICATION.md`` section 4,
defect 4). ``wallet_stream_sol`` and ``leader_exit`` asked ``getTransaction`` for
``maxSupportedTransactionVersion`` 0, and the node refuses every v1 transaction with
``-32015``. MEASURED: v1 is 24.2% of successful PumpSwap swaps and 34% of pump.fun curve
swaps. The fixtures are real mainnet transactions from the public keyless RPC (public chain
data, no Kaiba wallet in them). Each holds the raw answer at version 128 and the raw refusal
at version 0. The fakes below replay them the way the node did, gated on the version the
reader asks for, so a reader that asks for 0 gets the real refusal.

The parse is checked against the VENUE's own record of the trade, decoded here
independently of the balance arithmetic under test. That record is the pump.fun
``TradeEvent`` or the PumpSwap ``BuyEvent`` / ``SellEvent``, emitted as an Anchor self-CPI
inner instruction.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import struct
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kaiba.core.db import fetch_all, fetch_one
from kaiba.execution import fills
from kaiba.ingest import wallet_stream_sol as ss
from kaiba.learning import graduation_observer as grad
from kaiba.learning import leader_exit as le

FIXTURES = Path(__file__).parent / "fixtures" / "sol_v1"
NAMES = ("pumpfun_buy_v1", "pumpfun_sell_v1", "pumpswap_sell_v1")
SELLS = ("pumpfun_sell_v1", "pumpswap_sell_v1")
PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_AMM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
VENUE_PROGRAM = {"pump.fun": PUMP, "pumpswap": PUMP_AMM}
#: Anchor's ``emit_cpi!`` prefix: sha256("anchor:event")[:8], little-endian.
EVENT_IX_TAG = bytes.fromhex("e445a52e51cb9a1d")


def load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def tx_of(fx: Mapping) -> dict[str, Any]:
    return fx["response_max_version_128"]["result"]


def _disc(event: str) -> bytes:
    return hashlib.sha256(f"event:{event}".encode()).digest()[:8]


def venue_trade(tx: Mapping) -> dict[str, Any]:
    """The venue's own record of the signer's trade: ``{side, atoms, quote, user, program}``.

    pump.fun ``TradeEvent``: mint(32) sol_amount(u64) token_amount(u64) is_buy(u8) user(32).
    PumpSwap ``BuyEvent`` / ``SellEvent``: timestamp(i64) base_amount(u64) and 12 more u64
    (the 13th is the user's quote in or out), then pool(32) and user(32).
    """
    found: list[dict[str, Any]] = []
    for group in tx["meta"]["innerInstructions"]:
        for ix in group["instructions"]:
            if ix.get("programId") not in (PUMP, PUMP_AMM) or not isinstance(ix.get("data"), str):
                continue
            raw = grad.b58decode(ix["data"])
            if raw[:8] != EVENT_IX_TAG:
                continue
            body = raw[8:]
            if body[:8] == _disc("TradeEvent"):
                sol, tok = struct.unpack("<QQ", body[40:56])
                found.append({"side": "buy" if body[56] else "sell", "atoms": tok, "quote": sol,
                              "user": grad.b58encode(body[57:89]), "program": PUMP})
            elif body[:8] in (_disc("BuyEvent"), _disc("SellEvent")):
                fields = struct.unpack("<q" + "Q" * 13, body[8:120])
                found.append({"side": "buy" if body[:8] == _disc("BuyEvent") else "sell", "atoms": fields[1],
                              "quote": fields[13], "user": grad.b58encode(body[152:184]), "program": PUMP_AMM})
    assert len(found) == 1, f"a single-hop fixture has exactly one venue event, found {len(found)}"
    return found[0]


def signer_lamports(tx: Mapping, signer: str) -> int:
    """The signer's SOL leg straight from the balance arrays: its own account index, the fee
    added back (it paid it), and WSOL counted as SOL."""
    keys = [k["pubkey"] for k in tx["transaction"]["message"]["accountKeys"]]
    meta = tx["meta"]
    i = keys.index(signer)
    native = meta["postBalances"][i] - meta["preBalances"][i] + meta["fee"]
    wsol = sum(int(b["uiTokenAmount"]["amount"]) * sign for key, sign in (("preTokenBalances", -1), ("postTokenBalances", 1))
               for b in meta[key] if b.get("owner") == signer and b.get("mint") == ss.WSOL_MINT)
    return abs(native + wsol)


class RecordedNode:
    """``httpx.AsyncClient`` stand-in that answers as the node did: the version-1 body to a
    request that accepts version 1, the recorded ``-32015`` refusal to one that does not."""

    def __init__(self, *fixtures: Mapping) -> None:
        self.by_sig = {f["signature"]: f for f in fixtures}
        self.requests: list[dict[str, Any]] = []

    def answer(self, method: str, params: list[Any]) -> dict[str, Any]:
        if method != "getTransaction":
            raise AssertionError(f"unexpected {method}")
        fx = self.by_sig[params[0]]
        version = tx_of(fx)["version"]
        accepts = params[1].get("maxSupportedTransactionVersion")
        ok = isinstance(accepts, int) and accepts >= version
        return fx["response_max_version_128"] if ok else fx["response_max_version_0"]

    async def post(self, url: str, json: dict[str, Any]) -> Any:  # noqa: A002 - httpx's keyword
        self.requests.append(json)
        body = self.answer(json["method"], json["params"])
        return SimpleNamespace(status_code=200, json=lambda: body)


async def _no_sleep(_: float) -> None:
    return None


# --------------------------------------------------------------------------------------
# the fixtures are what the node serves today
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", NAMES)
def test_fixture_is_a_version_one_swap_and_version_zero_is_refused(name):
    fx = load(name)
    tx = tx_of(fx)
    msg = tx["transaction"]["message"]
    assert tx["version"] == 1
    assert fx["response_max_version_0"]["error"]["code"] == -32015
    assert "Transaction version (1) is not supported" in fx["response_max_version_0"]["error"]["message"]
    # the v1 layout the parsers rely on: no lookup tables, every key inline, one balance per key
    assert "addressTableLookups" not in msg and "transactionConfig" in msg
    assert {k["source"] for k in msg["accountKeys"]} == {"transaction"}
    assert len(tx["meta"]["preBalances"]) == len(tx["meta"]["postBalances"]) == len(msg["accountKeys"])
    venue = venue_trade(tx)
    assert venue["user"] == fx["signer"] and venue["side"] == fx["side"]
    assert venue["program"] == VENUE_PROGRAM[fx["venue"]]


def test_the_readers_ask_for_the_version_fills_already_asks_for():
    params = ss.tx_params("sig")
    assert params[1]["maxSupportedTransactionVersion"] == fills.MAX_TX_VERSION >= 1
    assert params[1]["encoding"] == "jsonParsed" and params[1]["commitment"] == ss.COMMITMENT


def test_the_recorded_node_refuses_the_old_request_like_the_real_one():
    """The fake is only a test if it reproduces the refusal: version 0 must fail."""
    fx = load("pumpswap_sell_v1")
    rpc = ss.SolRpc("https://sol.example/v2/KEY", client=RecordedNode(fx))
    old = [fx["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"}]
    with pytest.raises(ss.RpcFailure) as err:
        asyncio.run(rpc.call("getTransaction", old))
    assert ss.error_key(err.value) == "code:-32015" == le.UNSUPPORTED_VERSION


# --------------------------------------------------------------------------------------
# wallet_stream_sol: the tracked-wallet tape
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", NAMES)
def test_wallet_stream_writes_a_version_one_swap(tmp_db, name):
    fx = load(name)
    tx, signer = tx_of(fx), fx["signer"]
    node = RecordedNode(fx)
    rpc = ss.SolRpc("https://sol.example/v2/KEY", client=node)
    e = ss.SolWalletStream(rpc.call, reader=tmp_db, writer=tmp_db, sol_usd=lambda ts: Decimal("200"),
                           clock_ms=lambda: tx["blockTime"] * 1000 + 1_500, sleep=_no_sleep)
    e.wallets = frozenset({signer})
    n = ss.Notice(signature=fx["signature"], wallet=signer, slot=tx["slot"], failed=False, logs=None,
                  recv_ms=tx["blockTime"] * 1000 + 1_000)
    (out,) = asyncio.run(e.process_signature(fx["signature"], n))
    assert out.status == "written", out
    assert [r["params"][1]["maxSupportedTransactionVersion"] for r in node.requests] == [fills.MAX_TX_VERSION]
    (row,) = fetch_all(tmp_db, "SELECT * FROM swaps WHERE source = ?", (ss.SOURCE,))
    venue = venue_trade(tx)
    assert (row["wallet"], row["token"], row["side"]) == (signer, fx["token"], venue["side"])
    assert row["amount_token"] == str(venue["atoms"])                  # exactly what the venue moved
    assert row["program"] == venue["program"]
    assert row["slot"] == tx["slot"] and row["block_index"] == tx["transactionIndex"]
    assert row["ts_ms"] == tx["blockTime"] * 1000 and row["fee_payer"] == signer
    lamports = int(row["amount_native"])
    assert lamports == signer_lamports(tx, signer)
    # wallet-side SOL vs the venue's own quote figure: fees, rent and tips only
    assert abs(lamports - venue["quote"]) * 10_000 <= fills.DEFAULT_MAX_NATIVE_DELTA_BPS * venue["quote"]


@pytest.mark.parametrize("name", NAMES)
def test_the_screen_reads_a_version_one_transaction(name):
    """The router screen's single read (``_get_tx_once``) took a v1 transaction as unreadable."""
    fx = load(name)
    rpc = ss.SolRpc("https://sol.example/v2/KEY", client=RecordedNode(fx))
    e = ss.SolWalletStream(rpc.call, reader=None, writer=None, sol_usd=lambda ts: None, sleep=_no_sleep)
    got = asyncio.run(e._get_tx_once(fx["signature"]))  # noqa: SLF001 - the screen's own read
    assert got is not None and got["version"] == 1
    assert ss.zero_net(got, fx["signer"]) is False  # a signed swap that left a position: evidence


# --------------------------------------------------------------------------------------
# leader_exit: a leader's sale
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", SELLS)
def test_sell_evidence_reads_a_version_one_sale(name):
    fx = load(name)
    tx, signer, token = tx_of(fx), fx["signer"], fx["token"]
    venue = venue_trade(tx)
    ev = le.sell_evidence(tx, signer, token)
    assert ev is not None and ev.kind == "swap_sell" and ev.triggers
    assert ev.atoms == venue["atoms"] and ev.lamports == signer_lamports(tx, signer)
    assert ev.program == venue["program"]
    pre = sum(int(b["uiTokenAmount"]["amount"]) for b in tx["meta"]["preTokenBalances"]
              if b.get("owner") == signer and b.get("mint") == token)
    assert ev.pre_balance == pre and ev.fraction == Decimal(venue["atoms"]) / Decimal(pre)


def test_a_version_one_buy_is_not_a_sale():
    fx = load("pumpfun_buy_v1")
    assert le.sell_evidence(tx_of(fx), fx["signer"], fx["token"]) is None


@pytest.mark.parametrize("name", SELLS)
def test_leader_exit_triggers_on_a_version_one_sale(tmp_db, name):
    """The whole read path: LeaderRpc -> _get_tx -> sell_evidence -> the trigger row. Asked
    for version 0, this read failed every attempt and the sale was cached as never seen."""
    fx = load(name)
    tx, signer, token, sig = tx_of(fx), fx["signer"], fx["token"], fx["signature"]
    block_ms = tx["blockTime"] * 1000
    node = RecordedNode(fx)
    sent: list[list[Any]] = []

    async def send(method: str, params: list[Any]) -> Any:  # ``default_send``'s contract
        sent.append(params)
        body = node.answer(method, params)
        if body.get("error") is not None:
            raise ss.RpcFailure(f"{method}: {body['error']}")
        return body.get("result")

    clock = SimpleNamespace(ms=block_ms + 1_500)

    async def sleep(seconds: float) -> None:
        clock.ms += int(seconds * 1000)

    @contextlib.contextmanager
    def db():
        yield tmp_db

    pid = "pos_v1_fixture"
    tmp_db.execute(f"INSERT INTO {le.TABLE} (position_id, chain, token, lane, mode, opened_ms, n_leaders, status, "
                   "created_ms, updated_ms) VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (pid, "sol", token, le.LANE, "shadow", block_ms - 60_000, 1, "watching", block_ms, block_ms))
    # the leader's token account for this mint, as the transaction lists it
    keys = [k["pubkey"] for k in tx["transaction"]["message"]["accountKeys"]]
    (acct,) = {keys[b["accountIndex"]] for b in tx["meta"]["preTokenBalances"]
               if b.get("owner") == signer and b.get("mint") == token}

    async def go() -> le.LeaderExitWatcher:
        w = le.LeaderExitWatcher(rpc=le.LeaderRpc(send, le.CallBudget(100, 100), lambda: clock.ms), db=db,
                                 params={"workers": 1}, clock=lambda: clock.ms, sleep=sleep, stream_enabled=False)
        w.watches[pid] = le.Watch(position_id=pid, token=token, mode="shadow", opened_ms=block_ms - 60_000,
                                  by_address={acct: signer}, started=True)
        await w.handle(pid, ss.Notice(signature=sig, wallet=acct, slot=tx["slot"], failed=False, logs=None,
                                      recv_ms=block_ms + 1_500))
        await asyncio.gather(*w.watches[pid].tasks, return_exceptions=True)
        return w

    w = asyncio.run(go())
    assert [p[1]["maxSupportedTransactionVersion"] for p in sent if p and p[0] == sig] == [fills.MAX_TX_VERSION]
    assert w.counts["tx_unread"] == 0 and w.counts["triggers"] == 1
    venue = venue_trade(tx)
    sale = fetch_one(tmp_db, f"SELECT * FROM {le.SELLS_TABLE} WHERE position_id=?", (pid,))
    assert sale is not None and sale["signature"] == sig and sale["wallet"] == signer
    assert sale["kind"] == "swap_sell" and sale["post_entry"] == 1 and sale["atoms"] == str(venue["atoms"])
    obs = fetch_one(tmp_db, f"SELECT * FROM {le.TABLE} WHERE position_id=?", (pid,))
    assert obs["trigger_sig"] == sig and obs["trigger_block_ms"] == block_ms and obs["trigger_kind"] == "swap_sell"
    lpa = Decimal(signer_lamports(tx, signer)) / Decimal(venue["atoms"])
    assert Decimal(obs["leader_fill_lamports_per_atom"]) == lpa
