"""leader_exit: record-only forward evidence for "sell when the first signal wallet sells".

Synthetic chain throughout (no network, no real wallet): a fake JSON-RPC that answers the
exact reads the watcher makes, transactions built in the shape ``getTransaction`` returns.
"""

from __future__ import annotations

import ast
import asyncio
import base64
import contextlib
import hashlib
import json
import sqlite3
import struct
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from kaiba.core.db import fetch_all, fetch_one
from kaiba.ingest import wallet_stream_sol as wss
from kaiba.learning import graduation_observer as grad
from kaiba.learning import leader_exit as le

T0 = 1_791_200_000_000  # 2026-10-05 ~09:20 UTC
SOL = 10**9
PUMP_AMM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
SYSTEM = "11111111111111111111111111111111"


def syn(name: str) -> str:
    return grad.b58encode(hashlib.sha256(name.encode()).digest())


MINT = syn("mint")
L1, L2 = syn("leader-one"), syn("leader-two")
POOL_ACCT = syn("pool-account")
PID = "pos_test_1"


# --------------------------------------------------------------------------------------
# synthetic transactions
# --------------------------------------------------------------------------------------


def tb(index: int, mint: str, owner: str, amount: int) -> dict[str, Any]:
    return {"accountIndex": index, "mint": mint, "owner": owner, "programId": le.TOKEN_PROGRAM,
            "uiTokenAmount": {"amount": str(amount), "decimals": 6, "uiAmountString": "x"}}


def tx(*, wallet: str, sig: str, block_s: int, token_delta: int, sol_delta: int, pre_tokens: int = 10**12,
       programs: tuple[str, ...] = (PUMP_AMM,), err: Any = None, extra_mint: str | None = None) -> dict[str, Any]:
    """``wallet`` changes its ``MINT`` balance by ``token_delta`` and its SOL by ``sol_delta``."""
    accounts = [wallet, syn("ata" + wallet), POOL_ACCT, *programs]
    pre = [10 * SOL, 2_039_280, 50 * SOL] + [1] * len(programs)
    post = [10 * SOL + sol_delta - 5000, 2_039_280, 50 * SOL - sol_delta] + [1] * len(programs)
    pre_tb = [tb(1, MINT, wallet, pre_tokens)]
    post_tb = [tb(1, MINT, wallet, pre_tokens + token_delta)]
    if extra_mint:
        post_tb.append(tb(1, extra_mint, wallet, 5_000))
    keys = [{"pubkey": k, "signer": k == wallet, "source": "transaction", "writable": True} for k in accounts]
    return {"slot": 400_000_000 + block_s, "blockTime": block_s, "version": 0,
            "meta": {"err": err, "fee": 5000, "preBalances": pre, "postBalances": post,
                     "preTokenBalances": pre_tb, "postTokenBalances": post_tb, "innerInstructions": [],
                     "logMessages": []},
            "transaction": {"signatures": [sig], "message": {"accountKeys": keys, "instructions": [
                {"programId": p, "stackHeight": 1} for p in programs]}}}


def b64(data: bytes) -> list[str]:
    return [base64.b64encode(data).decode(), "base64"]


def token_account_bytes(amount: int) -> bytes:
    return bytes(64) + struct.pack("<Q", amount) + bytes(93)


def pool_bytes(mint: str, base_vault: str, quote_vault: str) -> bytes:
    data = bytearray(301)
    data[43:75] = grad.b58decode(mint)
    data[75:107] = grad.b58decode(grad.WSOL_MINT)
    data[139:171] = grad.b58decode(base_vault)
    data[171:203] = grad.b58decode(quote_vault)
    return bytes(data)


class FakeRpc:
    """Answers exactly the reads the watcher makes. ``reserves`` is the PumpSwap pool now."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.methods: list[str] = []
        self.base_vault, self.quote_vault = syn("base-vault"), syn("quote-vault")
        self.reserves = (100 * SOL, 10**15)  # (quote lamports, base atoms)
        self.balances: dict[str, int] = {}
        self.sigs: dict[str, list[dict[str, Any]]] = {}
        self.txs: dict[str, dict[str, Any]] = {}

    def account(self, key: str) -> dict[str, Any] | None:
        if key == MINT:
            return {"owner": le.TOKEN_PROGRAM, "data": b64(bytes(82))}
        if key == grad.pumpswap_pool_address(MINT):
            return {"owner": PUMP_AMM, "data": b64(pool_bytes(MINT, self.base_vault, self.quote_vault))}
        if key == self.base_vault:
            return {"owner": le.TOKEN_PROGRAM, "data": b64(token_account_bytes(self.reserves[1]))}
        if key == self.quote_vault:
            return {"owner": le.TOKEN_PROGRAM, "data": b64(token_account_bytes(self.reserves[0]))}
        if key in self.balances:
            return {"owner": le.TOKEN_PROGRAM, "data": b64(token_account_bytes(self.balances[key]))}
        return None  # the bonding curve: graduated and closed in this fixture

    async def __call__(self, method: str, params: list[Any]) -> Any:
        self.methods.append(method)
        if method == "getMultipleAccounts":
            return {"context": {"slot": 1}, "value": [self.account(k) for k in params[0]]}
        if method == "getTokenAccountsByOwner":
            return {"context": {"slot": 1}, "value": []}
        if method == "getSignaturesForAddress":
            return list(self.sigs.get(params[0], []))
        if method == "getTransaction":
            return self.txs.get(params[0])
        raise AssertionError(f"unexpected {method}")

    def add_sale(self, leader: str, sig: str, block_s: int, *, atoms: int = 4 * 10**11, lamports: int = SOL // 4,
                 **kw: Any) -> None:
        self.txs[sig] = tx(wallet=leader, sig=sig, block_s=block_s, token_delta=-atoms, sol_delta=lamports, **kw)

    def list_sig(self, leader: str, sig: str, block_s: int) -> None:
        ata = le.ata_address(leader, MINT, le.TOKEN_PROGRAM)
        assert ata is not None
        self.sigs.setdefault(ata, []).insert(0, {"signature": sig, "slot": 400_000_000 + block_s,
                                                 "blockTime": block_s, "err": None})


class FakeClock:
    def __init__(self, ms: int) -> None:
        self.ms = ms

    def __call__(self) -> int:
        return self.ms

    async def sleep(self, seconds: float) -> None:
        self.ms += int(seconds * 1000)


# --------------------------------------------------------------------------------------
# database fixtures
# --------------------------------------------------------------------------------------


def seed_position(conn: sqlite3.Connection, *, pid: str = PID, opened_ms: int = T0, mode: str = "live",
                  wallets: tuple[str, ...] = (L1, L2), closed_ms: int | None = None, cost: int = SOL // 10,
                  qty_total: int = 10**12, proceeds: int = 0, exit_reason: str | None = None) -> None:
    did, sid = f"dec_{pid}", f"sig_{pid}"
    conn.execute("INSERT INTO signals (signal_id, lane, chain, token, strength, wallets_json, created_ms) "
                 "VALUES (?,?,?,?,?,?,?)", (sid, le.LANE, "sol", MINT, 0.5, json.dumps(list(wallets)), opened_ms - 5000))
    conn.execute("INSERT INTO decisions (decision_id, ts_ms, lane, mode, chain, token, action, signals_json) "
                 "VALUES (?,?,?,?,?,?,?,?)", (did, opened_ms, le.LANE, mode, "sol", MINT, "enter", json.dumps([sid])))
    conn.execute("INSERT INTO decision_outcomes (decision_id, position_id, outcome, linked_ms) VALUES (?,?,?,?)",
                 (did, pid, "open", opened_ms))
    conn.execute("INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms, closed_ms, qty, qty_total, "
                 "cost_native, proceeds_native, realized_native, exit_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (pid, "sol", MINT, le.LANE, mode, opened_ms, closed_ms, str(qty_total), str(qty_total), str(cost),
                  str(proceeds), str(proceeds - cost), exit_reason))


def close_position(conn: sqlite3.Connection, *, closed_ms: int, proceeds: int, cost: int = SOL // 10,
                   sells: tuple[tuple[int, int, int], ...] = ()) -> None:
    conn.execute("UPDATE positions SET closed_ms=?, qty='0', proceeds_native=?, realized_native=?, exit_reason=? "
                 "WHERE position_id=?", (closed_ms, str(proceeds), str(proceeds - cost), "stop_loss", PID))
    for i, (ts, atoms, lamports) in enumerate(sells):
        oid = f"ord_sell_{i}"
        conn.execute("INSERT INTO orders (order_id, chain, token, side, lane, mode, input_token, output_token, "
                     "amount_in, min_out, slippage_bps, state, provider, filled_out, created_ms, updated_ms) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (oid, "sol", MINT, "sell", le.LANE, "live", MINT, grad.WSOL_MINT, str(atoms), "0", 100,
                      "filled", "gmgn", str(lamports), ts, ts))
        conn.execute("INSERT INTO position_orders (position_id, order_id, side, ts_ms) VALUES (?,?,?,?)",
                     (PID, oid, "sell", ts))


@pytest.fixture
def env(tmp_db):
    clock = FakeClock(T0 + 1_000)
    rpc = FakeRpc(clock)
    budget = le.CallBudget(10_000, 10_000)
    lrpc = le.LeaderRpc(rpc, budget, clock)

    @contextlib.contextmanager
    def db():
        yield tmp_db

    def watcher(**params: Any) -> le.LeaderExitWatcher:
        return le.LeaderExitWatcher(rpc=lrpc, db=db, params={"workers": 1, **params}, clock=clock,
                                    sleep=clock.sleep, stream_enabled=False)

    return {"conn": tmp_db, "clock": clock, "rpc": rpc, "lrpc": lrpc, "watcher": watcher, "db": db}


async def _settle(w: le.LeaderExitWatcher) -> None:
    await w.wait_started()
    tasks = [t for x in w.watches.values() for t in x.tasks]
    await asyncio.gather(*tasks, return_exceptions=True)


def obs(conn: sqlite3.Connection) -> dict[str, Any]:
    row = fetch_one(conn, f"SELECT * FROM {le.TABLE} WHERE position_id=?", (PID,))
    assert row is not None
    return row


# --------------------------------------------------------------------------------------
# the pre-declaration
# --------------------------------------------------------------------------------------


def test_declaration_is_pinned():
    """Changing the rule or the pass line after rows exist must fail here and be said out loud."""
    assert le.DECLARATION_DIGEST == "9a254e97064e44fe"
    assert le.PASS_LINE["min_n"] == 100
    assert le.PASS_LINE["ci"] == "0.90" and le.PASS_LINE["lower_percentile"] == "0.05"
    assert le.LEADER_EXIT_RULE["sell_cost_per_leg"] == "0.03"
    assert le.LEADER_EXIT_RULE["on_time_offset_ms"] == [5_000, 10_000]


# --------------------------------------------------------------------------------------
# what counts as a sale
# --------------------------------------------------------------------------------------


def test_sell_evidence_kinds():
    sale = le.sell_evidence(tx(wallet=L1, sig="s1", block_s=1, token_delta=-4 * 10**11, sol_delta=SOL // 4), L1, MINT)
    assert sale is not None and sale.kind == "swap_sell" and sale.triggers
    assert sale.atoms == 4 * 10**11 and sale.lamports == SOL // 4
    assert sale.fraction == Decimal("0.4")
    assert sale.lamports_per_atom == Decimal(SOL // 4) / Decimal(4 * 10**11)

    moved = le.sell_evidence(tx(wallet=L1, sig="s2", block_s=1, token_delta=-10**11, sol_delta=0, programs=(SYSTEM,)),
                             L1, MINT)
    assert moved is not None and moved.kind == "transfer_out" and not moved.triggers

    routed = le.sell_evidence(tx(wallet=L1, sig="s3", block_s=1, token_delta=-10**11, sol_delta=0,
                                 extra_mint=syn("other")), L1, MINT)
    assert routed is not None and routed.kind == "swap_sell_unpriced" and routed.triggers and routed.lamports is None

    assert le.sell_evidence(tx(wallet=L1, sig="s4", block_s=1, token_delta=10**11, sol_delta=-SOL), L1, MINT) is None
    assert le.sell_evidence(tx(wallet=L1, sig="s5", block_s=1, token_delta=-10**11, sol_delta=SOL,
                               err={"x": 1}), L1, MINT) is None
    assert le.sell_evidence({"meta": None}, L1, MINT) is None


# --------------------------------------------------------------------------------------
# the watcher
# --------------------------------------------------------------------------------------


def test_only_post_entry_sales_trigger(env):
    conn, rpc = env["conn"], env["rpc"]
    seed_position(conn)
    entry_s = T0 // 1000
    rpc.add_sale(L1, "pre_entry_sale", entry_s - 1)       # one second before our entry
    rpc.list_sig(L1, "pre_entry_sale", entry_s - 1)
    rpc.add_sale(L2, "post_entry_sale", entry_s + 30)
    rpc.list_sig(L2, "post_entry_sale", entry_s + 30)
    rpc.balances[le.ata_address(L1, MINT, le.TOKEN_PROGRAM)] = 6 * 10**11
    rpc.balances[le.ata_address(L2, MINT, le.TOKEN_PROGRAM)] = 10**12

    async def go() -> le.LeaderExitWatcher:
        env["clock"].ms = T0 + 31_500
        w = env["watcher"]()
        await w.sync()
        await w.wait_started()
        await _settle(w)
        return w

    asyncio.run(go())
    o = obs(conn)
    assert o["trigger_sig"] == "post_entry_sale" and o["trigger_wallet"] == L2
    assert o["trigger_block_ms"] == (entry_s + 30) * 1000
    assert o["watch_complete"] == 1 and o["n_leaders"] == 2
    sells = {r["signature"]: r for r in fetch_all(conn, f"SELECT * FROM {le.SELLS_TABLE}")}
    assert sells["pre_entry_sale"]["post_entry"] == 0
    assert sells["post_entry_sale"]["post_entry"] == 1
    leaders = json.loads(o["leaders_json"])
    assert {x["wallet"] for x in leaders} == {L1, L2} and all(x["state"] == "holding" for x in leaders)


def test_idempotent_across_repeats_and_restarts(env):
    conn, rpc = env["conn"], env["rpc"]
    seed_position(conn)
    s = T0 // 1000 + 20
    rpc.add_sale(L1, "sale_a", s)
    rpc.list_sig(L1, "sale_a", s)

    async def go() -> None:
        env["clock"].ms = T0 + 21_000
        w1 = env["watcher"]()
        await w1.sync()
        await w1.wait_started()
        n = wss.Notice(signature="sale_a", wallet=le.ata_address(L1, MINT, le.TOKEN_PROGRAM), slot=None,
                       failed=False, logs=None, recv_ms=T0 + 21_500)
        await w1.handle(PID, n)  # the same signature pushed after the backfill read it
        await _settle(w1)
        w2 = env["watcher"]()     # a restart: same rows, re-read from entry
        await w2.sync()
        await w2.wait_started()
        await _settle(w2)

    asyncio.run(go())
    assert fetch_one(conn, f"SELECT COUNT(*) AS n FROM {le.TABLE}")["n"] == 1
    assert fetch_one(conn, f"SELECT COUNT(*) AS n FROM {le.SELLS_TABLE}")["n"] == 1
    o = obs(conn)
    assert o["trigger_sig"] == "sale_a" and o["trigger_block_ms"] == s * 1000
    assert json.loads(o["plus5_mark_json"])["venue"] == "pumpswap"  # not reset by the repeats


def test_an_earlier_sale_found_later_becomes_the_trigger(env):
    conn, rpc = env["conn"], env["rpc"]
    seed_position(conn)
    rpc.add_sale(L1, "late_sale", T0 // 1000 + 60)
    rpc.add_sale(L2, "early_sale", T0 // 1000 + 40)

    async def go() -> None:
        env["clock"].ms = T0 + 2_000
        w = env["watcher"]()
        await w.sync()
        await w.wait_started()
        ata1, ata2 = (le.ata_address(x, MINT, le.TOKEN_PROGRAM) for x in (L1, L2))
        env["clock"].ms = T0 + 61_000
        await w.handle(PID, wss.Notice("late_sale", ata1, None, False, None, T0 + 61_000))
        await w.handle(PID, wss.Notice("early_sale", ata2, None, False, None, T0 + 61_200, backfilled=True))
        await _settle(w)

    asyncio.run(go())
    o = obs(conn)
    assert o["trigger_sig"] == "early_sale" and o["trigger_backfilled"] == 1
    # the +5 s mark of the earlier sale could only be read ~21 s late: recorded, and late
    assert json.loads(o["plus5_mark_json"])["offset_ms"] > 10_000


def test_marks_are_read_at_detection_and_at_plus_five_seconds(env):
    conn, rpc = env["conn"], env["rpc"]
    seed_position(conn)
    block_s = T0 // 1000 + 50
    rpc.add_sale(L1, "the_sale", block_s)

    async def go() -> None:
        env["clock"].ms = T0 + 1_000
        w = env["watcher"]()
        await w.sync()
        await w.wait_started()
        env["clock"].ms = block_s * 1000 + 1_500
        await w.handle(PID, wss.Notice("the_sale", le.ata_address(L1, MINT, le.TOKEN_PROGRAM), None, False, None,
                                       block_s * 1000 + 1_500))
        await _settle(w)

    asyncio.run(go())
    o = obs(conn)
    assert o["detect_latency_ms"] == 1_500 and o["trigger_backfilled"] == 0
    detect, plus5 = json.loads(o["detect_mark_json"]), json.loads(o["plus5_mark_json"])
    assert detect["at_ms"] == block_s * 1000 + 1_500 and detect["venue"] == "pumpswap"
    assert plus5["offset_ms"] == 5_000
    assert plus5["base"] == str(rpc.reserves[1]) and plus5["quote"] == str(rpc.reserves[0])
    assert o["held_at_trigger"] == str(10**12) and o["held_basis"] == "snapshot"


def test_full_round_trip_scores_the_rule_against_the_ladder(env):
    conn, rpc = env["conn"], env["rpc"]
    seed_position(conn)
    block_s = T0 // 1000 + 50
    rpc.add_sale(L1, "the_sale", block_s)
    cost, proceeds = SOL // 10, SOL // 20  # the ladder lost 50%

    async def go() -> None:
        env["clock"].ms = T0 + 1_000
        w = env["watcher"](close_grace_ms=0)
        await w.sync()
        await w.wait_started()
        env["clock"].ms = block_s * 1000 + 1_200
        await w.handle(PID, wss.Notice("the_sale", le.ata_address(L1, MINT, le.TOKEN_PROGRAM), None, False, None,
                                       block_s * 1000 + 1_200))
        await _settle(w)
        close_position(conn, closed_ms=block_s * 1000 + 60_000, proceeds=proceeds,
                       sells=((block_s * 1000 + 60_000, 10**12, proceeds),))
        env["clock"].ms = block_s * 1000 + 61_000
        await w.sync()
        await w.wait_started()
        await w.close()

    asyncio.run(go())
    o = obs(conn)
    assert o["status"] == "done" and o["fired"] == 1 and o["rule_basis"] == "plus5_pool"
    q, b = rpc.reserves
    value = grad.PoolState(quote=q, base=b, basis="x", fee_quote_bps=grad.PUMPSWAP_FEE_BPS).sell(10**12)
    expect = float((Decimal(value) * Decimal("0.97")) / Decimal(cost) - 1)
    assert o["rule_return"] == pytest.approx(expect)
    assert o["ladder_return"] == pytest.approx(-0.5)
    assert o["held_basis"] == "orders" and o["exit_reason"] == "stop_loss"


def test_position_without_signal_wallets_is_unwatchable_and_spends_nothing(env):
    conn, rpc = env["conn"], env["rpc"]
    seed_position(conn, wallets=())

    async def go() -> None:
        w = env["watcher"]()
        await w.sync()
        await w.wait_started()
        await _settle(w)

    asyncio.run(go())
    assert obs(conn)["status"] == "unwatchable"
    assert rpc.methods == []


# --------------------------------------------------------------------------------------
# the budget and the read-only transport
# --------------------------------------------------------------------------------------


def test_call_budget_caps_per_hour_and_persists():
    saved: dict[str, Any] = {}
    sent: list[str] = []

    async def send(method: str, params: list[Any]) -> Any:
        sent.append(method)
        return {}

    b = le.CallBudget(3, 100, save=lambda s: saved.update(s))
    rpc = le.LeaderRpc(send, b, clock=lambda: T0)

    async def go() -> None:
        for _ in range(3):
            await rpc.call("getSlot", [])
        with pytest.raises(wss.RpcFailure, match="budget"):
            await rpc.call("getSlot", [])

    asyncio.run(go())
    assert len(sent) == 3 and b.refused == 1 and saved["h"] == 3
    again = le.CallBudget(3, 100, load=lambda: dict(saved))  # a restart inside the same hour
    assert not again.take(1, at_ms=T0)
    assert again.take(1, at_ms=T0 + 3_600_000)               # the next UTC hour


def test_only_read_methods_leave_the_process():
    sent: list[str] = []

    async def send(method: str, params: list[Any]) -> Any:
        sent.append(method)

    rpc = le.LeaderRpc(send, le.CallBudget(10, 10), clock=lambda: T0)
    for method in ("sendTransaction", "requestAirdrop", "simulateTransaction"):
        with pytest.raises(PermissionError):
            asyncio.run(rpc.call(method, []))
    assert sent == [] and rpc.budget.state.get("h", 0) == 0
    assert le.READ_METHODS <= {"getMultipleAccounts", "getSignaturesForAddress", "getTransaction",
                               "getTokenAccountsByOwner", "getSlot", "getBlockTime"}


def test_no_path_to_orders():
    """No import of anything that can trade, and a whole watch writes only its own tables."""
    tree = ast.parse(Path(le.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            imported |= {f"{node.module}.{a.name}" for a in node.names}
    banned = ("executor", "gmgn_cli", "signer", "txwire", "paper", "engine", "watchdog", "protection",
              "risk", "copy_manager", "snipe")
    assert not [m for m in imported if any(b in m.split(".") for b in banned)]
    source = Path(le.__file__).read_text(encoding="utf-8")
    assert "gmgn-cli" not in source.replace("no ``gmgn-cli``", "")


def test_a_full_watch_writes_only_its_own_tables(env):
    conn, rpc = env["conn"], env["rpc"]
    seed_position(conn)
    block_s = T0 // 1000 + 50
    rpc.add_sale(L1, "the_sale", block_s)
    rpc.list_sig(L1, "the_sale", block_s)
    allowed = {le.TABLE, le.SELLS_TABLE, "kv"}
    written: set[str] = set()

    def authorizer(action: int, arg1: Any, arg2: Any, dbname: Any, source: Any) -> int:
        if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
            written.add(arg1)
            if arg1 not in allowed and not str(arg1).startswith("sqlite_"):
                return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    conn.set_authorizer(authorizer)
    try:
        async def go() -> None:
            env["clock"].ms = block_s * 1000 + 2_000
            w = env["watcher"](close_grace_ms=0)
            await w.sync()
            await w.wait_started()
            await _settle(w)
            conn.set_authorizer(None)
            close_position(conn, closed_ms=block_s * 1000 + 90_000, proceeds=SOL // 20)
            conn.set_authorizer(authorizer)
            env["clock"].ms = block_s * 1000 + 91_000
            await w.sync()
            await w.wait_started()
            await w.close()

        asyncio.run(go())
    finally:
        conn.set_authorizer(None)
    assert written and written <= allowed
    assert obs(conn)["status"] == "done"


# --------------------------------------------------------------------------------------
# the arithmetic
# --------------------------------------------------------------------------------------


def _mark(at_ms: int, quote: int = 100 * SOL, base: int = 10**15) -> str:
    return json.dumps({"at_ms": at_ms, "venue": "pumpswap", "quote": str(quote), "base": str(base),
                       "fee_quote_bps": 120})


def test_compute_outcome_math():
    pos = {"cost_native": str(SOL // 10), "proceeds_native": str(SOL // 20), "closed_ms": T0 + 100_000,
           "qty_total": str(10**12)}
    # never fired: the rule IS the ladder
    out = le.compute_outcome({"trigger_block_ms": None}, pos, [])
    assert out["fired"] == 0 and out["rule_return"] == out["ladder_return"] == pytest.approx(-0.5)
    # fired after our close: also not fired
    assert le.compute_outcome({"trigger_block_ms": T0 + 200_000}, pos, [])["fired"] == 0

    trig = T0 + 10_000
    base_obs = {"trigger_block_ms": trig, "held_at_trigger": str(10**12), "proceeds_before_trigger": "0",
                "leader_fill_lamports_per_atom": "0.0002", "plus5_mark_json": _mark(trig + 5_000),
                "detect_mark_json": _mark(trig + 1_500)}
    out = le.compute_outcome(base_obs, pos, [])
    pool = grad.PoolState(quote=100 * SOL, base=10**15, basis="x", fee_quote_bps=120)
    assert out["rule_basis"] == "plus5_pool" and out["held_basis"] is None
    assert out["rule_return"] == pytest.approx(pool.sell(10**12) * 0.97 / (SOL // 10) - 1)
    assert out["rule_return_leader_fill"] == pytest.approx(10**12 * 0.0002 * 0.97 / (SOL // 10) - 1)

    # a TP rung sold half before the trigger: realised proceeds count, only the rest is re-priced
    sells = [{"ts_ms": trig - 1, "amount_in": str(5 * 10**11), "filled_out": str(SOL // 8)},
             {"ts_ms": trig + 50_000, "amount_in": str(5 * 10**11), "filled_out": str(SOL // 50)}]
    out = le.compute_outcome(base_obs, pos, sells)
    assert out["held_basis"] == "orders" and out["held_at_trigger"] == str(5 * 10**11)
    assert out["rule_return"] == pytest.approx((SOL // 8 + pool.sell(5 * 10**11) * 0.97) / (SOL // 10) - 1)

    late = {**base_obs, "plus5_mark_json": _mark(trig + 12_000)}
    out = le.compute_outcome(late, pos, [])
    assert out["rule_return"] is None and out["rule_basis"] == "missed:late_12000ms"
    assert out["rule_return_leader_fill"] is not None  # the secondary survives

    missing = {**base_obs, "plus5_mark_json": json.dumps({"at_ms": trig + 5_000, "missed": "no_pool_state"})}
    assert le.compute_outcome(missing, pos, [])["rule_basis"] == "missed:no_pool_state"


def test_curve_mark_prices_through_the_curve_with_its_fee():
    from kaiba.execution.curve_price import RESERVED_TOKEN_ATOMS, CurveState, platform_fee

    data = b"\0" * 8 + struct.pack("<QQQQQ", RESERVED_TOKEN_ATOMS + 500 * 10**12, 40 * SOL,
                                   500 * 10**12, 10 * SOL, 10**15) + b"\0"
    mark = le.mark_from_accounts({"data": b64(data)}, None, None, at_ms=T0)
    assert mark["venue"] == "pump_curve"
    state, _ = CurveState.build(virtual_sol=40 * SOL, virtual_token=RESERVED_TOKEN_ATOMS + 500 * 10**12,
                                real_sol=10 * SOL, real_token=500 * 10**12)
    gross = state.sell_exact_in(10**12)[0]
    assert le.mark_sell_value(mark, 10**12) == gross - platform_fee(gross)[0]
    complete = data[:48] + b"\x01"
    assert le.mark_from_accounts({"data": b64(complete)}, None, None, at_ms=T0)["missed"] == "no_pool_state"


def _rows(diffs: list[float], *, rule: float = -0.05, missed: int = 0, mode: str = "live") -> list[dict[str, Any]]:
    rows = [{"status": "done", "watch_complete": 1, "fired": 1, "mode": mode, "ladder_return": rule - d,
             "rule_return": rule, "detect_latency_ms": 1500, "trigger_backfilled": 0} for d in diffs]
    rows += [{"status": "done", "watch_complete": 1, "fired": 1, "mode": mode, "ladder_return": -0.2,
              "rule_return": None} for _ in range(missed)]
    return rows


def test_judge_verdicts():
    good = [0.05 + 0.02 * ((i % 7) - 3) / 3 for i in range(100)]
    v = le.judge(_rows(good), resamples=2000)
    assert v["verdict_ship_exit_rule"] == "PASS" and v["ci90_rule_minus_ladder"][0] > 0
    assert v["verdict_justifies_entries"] == "FAIL"           # the rule still loses money on its own
    assert le.judge(_rows(good, rule=0.01), resamples=500)["verdict_justifies_entries"] == "PASS"
    assert le.judge(_rows(good[:99]), resamples=500)["verdict_ship_exit_rule"] == "PENDING"
    noisy = [0.002 + (0.5 if i % 2 else -0.5) for i in range(100)]
    assert le.judge(_rows(noisy), resamples=2000)["verdict_ship_exit_rule"] == "FAIL"   # CI spans zero
    assert le.judge(_rows(good, missed=30), resamples=500)["verdict_ship_exit_rule"] == "INCONCLUSIVE"
    incomplete = _rows(good) + [{"status": "done", "watch_complete": 0, "fired": 1, "ladder_return": 0.0,
                                 "rule_return": 0.9}]
    assert le.judge(incomplete, resamples=500)["excluded_incomplete_watch"] == 1


def test_bootstrap_interval_is_a_ninety_percent_band():
    xs = [float(i) for i in range(-50, 51)]
    lo, hi = le.bootstrap_interval(xs, resamples=4000, seed=1, lower=0.05)
    assert lo < 0 < hi and lo == pytest.approx(-hi, abs=1.5)


def test_pda_derivations_are_deterministic_and_program_specific():
    a = le.ata_address(L1, MINT, le.TOKEN_PROGRAM)
    b = le.ata_address(L1, MINT, le.TOKEN_2022_PROGRAM)
    assert a and b and a != b and wss.is_sol_address(a)
    assert a == le.ata_address(L1, MINT, le.TOKEN_PROGRAM)
    assert le.bonding_curve_address(MINT) and le.bonding_curve_address("not-base58!") is None


def test_signatures_after_the_trigger_are_not_read(env):
    """Only the earliest post-entry sale matters: later transactions cost nothing."""
    conn, rpc = env["conn"], env["rpc"]
    seed_position(conn)
    first = T0 // 1000 + 10
    for i, s in enumerate(("sale_first", "later_1", "later_2")):
        rpc.add_sale(L1, s, first + i * 10)
        rpc.list_sig(L1, s, first + i * 10)

    async def go() -> le.LeaderExitWatcher:
        env["clock"].ms = T0 + 60_000
        w = env["watcher"]()
        await w.sync()
        await w.wait_started()
        await _settle(w)
        await w.handle(PID, wss.Notice("later_push", le.ata_address(L1, MINT, le.TOKEN_PROGRAM), None, False, None,
                                       T0 + 61_000))
        return w

    w = asyncio.run(go())
    assert obs(conn)["trigger_sig"] == "sale_first"
    assert rpc.methods.count("getTransaction") == 1
    assert w.counts["after_trigger_skipped"] == 3


# --------------------------------------------------------------------------------------
# the socket: leader pushes trigger, the mint is a canary that reconnects a lagging socket
# --------------------------------------------------------------------------------------


class FakeWS:
    def __init__(self, server: FakeServer, n: int) -> None:
        self.server, self.n = server, n
        self.q: asyncio.Queue[Any] = asyncio.Queue()
        self.subs: dict[str, int] = {}

    async def __aenter__(self) -> FakeWS:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def send(self, text: str) -> None:
        req = json.loads(text)
        if req["method"] != "logsSubscribe":
            self.q.put_nowait(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": True}))
            return
        addr = req["params"][0]["mentions"][0]
        self.subs[addr] = 100 * (self.n + 1) + len(self.subs)
        self.q.put_nowait(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": self.subs[addr]}))
        script = self.server.scripts[self.n] if self.n < len(self.server.scripts) else []
        if len(self.subs) == self.server.expect_subs:
            for addr2, sig, slot in script:
                self.q.put_nowait(json.dumps({"jsonrpc": "2.0", "method": "logsNotification", "params": {
                    "subscription": self.subs[addr2],
                    "result": {"context": {"slot": slot}, "value": {"signature": sig, "err": None, "logs": []}}}}))

    async def recv(self) -> Any:
        return await self.q.get()


class FakeServer:
    def __init__(self, scripts: list[list[tuple[str, str, int]]], expect_subs: int) -> None:
        self.scripts, self.expect_subs, self.dials = scripts, expect_subs, 0

    def connect(self, url: str) -> FakeWS:
        self.dials += 1
        return FakeWS(self, self.dials - 1)


def test_socket_pushes_trigger_and_a_lagging_canary_reconnects(env, monkeypatch):
    from kaiba.ingest import alchemy_ws as aws

    async def no_wait(stop: asyncio.Event, seconds: float) -> None:
        return None

    monkeypatch.setattr(aws, "_wait", no_wait)
    conn, rpc = env["conn"], env["rpc"]
    seed_position(conn)
    block_s = T0 // 1000 + 50
    rpc.add_sale(L1, "sale_ws", block_s)
    ata1 = le.ata_address(L1, MINT, le.TOKEN_PROGRAM)
    real = rpc.__call__

    async def answer(method: str, params: list[Any]) -> Any:
        if method == "getSlot":
            return 400_000_100
        if method == "getBlockTime":
            rpc.methods.append(method)
            return le.now_ms() // 1000 - 30  # the canary's block was 30 s ago: this socket lags
        return await real(method, params)

    lrpc = le.LeaderRpc(answer, le.CallBudget(1000, 1000), env["clock"])
    server = FakeServer([[(MINT, "canary_push", 400_000_090), (ata1, "sale_ws", 400_000_091)], []], expect_subs=3)

    async def go() -> le.LeaderExitWatcher:
        env["clock"].ms = T0 + 1_000
        w = le.LeaderExitWatcher(rpc=lrpc, db=env["db"], url="https://sol.example/v2/KEY", connect=server.connect,
                                 params={"workers": 1}, clock=env["clock"], sleep=env["clock"].sleep)
        await w.sync()
        await w.wait_started()
        for _ in range(500):
            if w.counts["triggers"] and server.dials >= 2:
                break
            await asyncio.sleep(0.01)
        watch = w.watches[PID]
        assert watch.lag_reconnects == 1 and watch.lag_samples[0] >= 29_000
        await w.close()
        return w

    w = asyncio.run(asyncio.wait_for(go(), timeout=10))
    assert obs(conn)["trigger_sig"] == "sale_ws" and obs(conn)["trigger_backfilled"] == 0
    assert server.dials >= 2
    assert rpc.methods.count("getTransaction") == 1      # the canary push is never read
    assert w.counts["pushes"] == 1
