"""graduation_observer: paper-only forward measurement of graduations.

Synthetic chains throughout (no network, no owner wallet): a fake JSON-RPC that answers the
exact reads the observer makes, with pool reserves the test controls. One public token mint
pins the PumpSwap PDA derivation against the pool it was VERIFIED to own on mainnet.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import json
import sqlite3
import struct
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from kaiba.learning import graduation_observer as go

H = 3_600_000
T0 = 1_791_200_000_000  # 2026-10-05 ~09:20 UTC


# --------------------------------------------------------------------------------------
# synthetic chain
# --------------------------------------------------------------------------------------


def syn_mint(name: str) -> str:
    return go.b58encode(hashlib.sha256(name.encode()).digest())


def syn_evm(name: str) -> str:
    return "0x" + hashlib.sha256(name.encode()).hexdigest()[:40]


def word(n: int) -> str:
    return format(n % (1 << 256), "064x")


def addr_word(a: str) -> str:
    return a.lower().removeprefix("0x").rjust(64, "0")


class FakeChain:
    """Answers the observer's reads. ``reserves[token] = (quote, base)`` is the pool now."""

    def __init__(self) -> None:
        self.reserves: dict[str, tuple[int, int]] = {}
        self.methods: list[str] = []
        self.requests = 0
        self.fail = False
        self.sol_vault: dict[str, tuple[str, str]] = {}  # vault -> (token, side)
        self.pool_of: dict[str, str] = {}  # sol pool account -> mint
        self.rh_slots: dict[str, str] = {}  # state slot -> token
        self.rh_meta: dict[str, dict[str, Any]] = {}
        self.bsc_pair: dict[str, str] = {}  # pair -> token
        self.block_time: dict[str, int] = {}  # signature -> seconds

    def __call__(self, chain: str, calls: list[tuple[str, list[Any]]], endpoint: str) -> list[Any] | None:
        self.requests += 1
        self.methods += [m for m, _ in calls]
        if self.fail:
            return None
        return [self.answer(chain, m, a) for m, a in calls]

    # sol
    def add_sol(self, mint: str, quote: int, base: int) -> None:
        pool = go.pumpswap_pool_address(mint)
        assert pool is not None
        self.pool_of[pool] = mint
        bv, qv = syn_mint("bv" + mint), syn_mint("qv" + mint)
        self.sol_vault[bv], self.sol_vault[qv] = (mint, "base"), (mint, "quote")
        self.reserves[mint] = (quote, base)

    def sol_pool_bytes(self, mint: str) -> bytes:
        raw = bytearray(301)
        raw[43:75] = go.b58decode(mint)
        raw[75:107] = go.b58decode(go.WSOL_MINT)
        raw[139:171] = go.b58decode(syn_mint("bv" + mint))
        raw[171:203] = go.b58decode(syn_mint("qv" + mint))
        return bytes(raw)

    # robinhood
    def add_rh(self, token: str, quote_token: str, quote: int, base: int) -> dict[str, Any]:
        from kaiba.execution import onchain_pool as op

        c0, c1 = sorted([token.lower(), quote_token.lower()])
        hooks = syn_evm("pons-hook")
        pid = op.v4_pool_id(c0, c1, 0, 200, hooks)
        self.rh_slots[op.v4_state_slot(pid)] = token
        self.reserves[token] = (quote, base)
        self.rh_meta[token] = {"c0": c0, "c1": c1, "hooks": hooks, "pid": pid}
        return self.rh_meta[token]

    def rh_receipt(self, token: str) -> dict[str, Any]:
        from kaiba.execution import onchain_pool as op
        from kaiba.ingest import robinhood as rh

        m = self.rh_meta[token]
        out_tokens = 10**24
        token_is_c0 = token.lower() == m["c0"]
        a0, a1 = (out_tokens, -(10**17)) if token_is_c0 else (-(10**17), out_tokens)
        return {"logs": [
            {"address": op.POOL_MANAGER, "topics": [op.TOPIC_V4_INITIALIZE, m["pid"], "0x" + addr_word(m["c0"]),
                                                     "0x" + addr_word(m["c1"])],
             "data": "0x" + word(0) + word(200) + addr_word(m["hooks"]) + word(1 << 96) + word(0)},
            {"address": op.POOL_MANAGER, "topics": [go.topic_v4_modify_liquidity(), m["pid"], "0x" + word(1)],
             "data": "0x" + word(-887200) + word(887200) + word(10**22) + word(0)},
            {"address": op.POOL_MANAGER, "topics": [go.topic_v4_swap(), m["pid"], "0x" + word(1)],
             "data": "0x" + word(a0) + word(a1) + word(1 << 96) + word(10**22) + word(0) + word(0)},
            {"address": syn_evm("pons-hook"), "topics": [rh.TOPIC_HOOK_FEE, m["pid"]],
             "data": "0x" + addr_word(token) + word(out_tokens // 100) + word(0)},
        ]}

    def rh_extsload(self, token: str) -> str:
        """Encode (quote, base) as full-range virtual reserves: sqrtP and L."""
        from math import isqrt

        quote, base = self.reserves[token]
        m = self.rh_meta[token]
        r0, r1 = (base, quote) if token.lower() == m["c0"] else (quote, base)
        liquidity = isqrt(r0 * r1)
        sqrt_p = isqrt(r1 * (1 << 192) // r0) if r0 else 0
        slot0 = sqrt_p  # tick, protocolFee, lpFee all 0
        return "0x" + word(0x20) + word(4) + word(slot0) + word(0) + word(0) + word(liquidity)

    # bsc
    def add_bsc(self, token: str, quote: int, base: int, *, tax: int = 100, status: int = 4) -> str:
        pair = syn_evm("pair" + token)
        self.bsc_pair[pair] = token
        self.reserves[token] = (quote, base)
        self.reserves["status:" + token] = (status, tax)
        return pair

    def flap_record(self, token: str) -> str:
        status, tax = self.reserves["status:" + token]
        words = [0] * 18
        words[0], words[12], words[13] = status, tax, tax
        words[14] = int(syn_evm("pair" + token), 16)
        return "0x" + "".join(word(w) for w in words)

    def answer(self, chain: str, method: str, args: list[Any]) -> Any:
        from kaiba.execution import evm_price as ep

        if method == "getMultipleAccounts":
            keys, opts = args
            if "dataSlice" in opts:
                out = []
                for k in keys:
                    mint, side = self.sol_vault[k]
                    quote, base = self.reserves[mint]
                    amt = base if side == "base" else quote
                    out.append({"data": [base64.b64encode(struct.pack("<Q", amt)).decode(), "base64"]})
                return {"context": {"slot": 1}, "value": out}
            return {"context": {"slot": 1}, "value": [
                {"owner": go.PUMP_SWAP_PROGRAM, "data": [base64.b64encode(self.sol_pool_bytes(self.pool_of[k])).decode(),
                                                         "base64"]} if k in self.pool_of else None for k in keys]}
        if method == "getTransaction":
            bt = self.block_time.get(args[0])
            return {"blockTime": bt} if bt else None
        if method == "eth_getTransactionReceipt":
            token = args[0].removeprefix("tx-")
            return self.rh_receipt(token)
        if method == "eth_call":
            to, data = args[0]["to"], args[0]["data"]
            if chain == "robinhood":
                slot = "0x" + data[10:74]
                return self.rh_extsload(self.rh_slots[slot])
            if to == ep.FLAP_PORTAL:
                return self.flap_record("0x" + data[-40:])
            if data == go.SEL_GET_RESERVES:
                token = self.bsc_pair[to]
                quote, base = self.reserves[token]
                quote_addr = ep.BSC_WRAPPED_NATIVE
                r0, r1 = (base, quote) if token.lower() < quote_addr else (quote, base)
                return "0x" + word(r0) + word(r1) + word(0)
        raise AssertionError(f"unexpected read {method} {args}")


class Clock:
    def __init__(self, t: int) -> None:
        self.t = t

    def __call__(self) -> int:
        return self.t


def emit_migration(conn: sqlite3.Connection, chain: str, token: str, *, migrated_ms: int, ts_ms: int,
                   signature: str | None = None, source_ms: int | None = None) -> int:
    payload = {"mint": token, "migrated_ms": migrated_ms, "signature": signature, "source_ms": source_ms}
    cur = conn.execute("INSERT INTO events (ts_ms, kind, chain, subject, payload) VALUES (?,?,?,?,?)",
                       (ts_ms, "token.migrated", chain, token, json.dumps(payload)))
    return int(cur.lastrowid)


def rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(f"SELECT * FROM {go.TABLE} ORDER BY chain, token").fetchall()]


PARAMS = {"chains": ["sol", "robinhood", "bsc"], "sample_every": {"sol": 1, "robinhood": 1, "bsc": 1},
          "bsc_scan_every_s": 0, "max_calls_per_hour": 10_000, "max_calls_per_day": 100_000}


def start(conn: sqlite3.Connection, clock: Clock, fake: FakeChain, params: dict[str, Any] | None = None) -> dict:
    """Cold start: the first pass only sets the event cursor."""
    return go.tick(conn, params or PARAMS, rpc=fake, clock=clock)


# --------------------------------------------------------------------------------------
# schema
# --------------------------------------------------------------------------------------


def test_migration_creates_table_with_chain_detected_index(tmp_db):
    idx = {r["name"]: [c["name"] for c in tmp_db.execute(f"PRAGMA index_info({r['name']})").fetchall()]
           for r in tmp_db.execute(f"PRAGMA index_list({go.TABLE})").fetchall()}
    assert idx["idx_graduation_observations_chain"] == ["chain", "detected_ms"]
    go.ensure_table(tmp_db)  # idempotent on a migrated database


# --------------------------------------------------------------------------------------
# idempotency and detection
# --------------------------------------------------------------------------------------


def test_cold_start_skips_history_and_each_graduation_is_one_row(tmp_db):
    fake, clock = FakeChain(), Clock(T0)
    old = syn_evm("old-token")
    emit_migration(tmp_db, "robinhood", old, migrated_ms=T0 - H, ts_ms=T0 - H, signature="tx-" + old)
    start(tmp_db, clock, fake)
    assert rows(tmp_db) == []  # history before the observer existed is not back-filled

    tok = syn_evm("rh-1")
    fake.add_rh(tok, go.EVM_ZERO, 4 * 10**18, 2 * 10**26)
    emit_migration(tmp_db, "robinhood", tok, migrated_ms=T0 - 2_000, ts_ms=T0, signature="tx-" + tok)
    emit_migration(tmp_db, "robinhood", tok, migrated_ms=T0 - 2_000, ts_ms=T0 + 1, signature="tx-" + tok)  # replayed
    clock.t = T0 + 1_000
    go.tick(tmp_db, PARAMS, rpc=fake, clock=clock)
    go.tick(tmp_db, PARAMS, rpc=fake, clock=clock)
    tmp_db.execute("DELETE FROM kv WHERE key=?", (go.CURSOR_KEY,))  # a cursor lost on restart
    go.tick(tmp_db, PARAMS, rpc=fake, clock=clock)
    got = rows(tmp_db)
    assert len(got) == 1
    r = got[0]
    assert r["obs_id"] == go.obs_id_for("robinhood", tok)
    assert r["graduated_basis"] == "block_time" and r["detect_latency_ms"] == 3_000
    assert r["entry_due_ms"] == T0 - 2_000 + 30_000


def test_bsc_rows_detected_from_tokens_once(tmp_db):
    fake, clock = FakeChain(), Clock(T0)
    start(tmp_db, clock, fake)
    tok = syn_evm("bsc-1")
    fake.add_bsc(tok, 16 * 10**18, 2 * 10**26)
    tmp_db.execute("INSERT INTO tokens (chain, address, created_ms, launchpad, migrated_ms, first_seen_ms, meta_json) "
                   "VALUES ('bsc', ?, ?, 'flap', ?, ?, '{}')", (tok, T0 - 600_000, T0 - 1_000, T0 - 600_000))
    for t in (T0 + 1_000, T0 + 2_000, T0 + 3_000):
        clock.t = t
        go.tick(tmp_db, PARAMS, rpc=fake, clock=clock)
    got = rows(tmp_db)
    assert len(got) == 1 and got[0]["source"] == "tokens.migrated_ms"
    assert json.loads(got[0]["curve_json"])["seconds_to_graduate"] == 599


def test_late_detection_and_unsampled_rows_spend_no_rpc(tmp_db):
    fake, clock = FakeChain(), Clock(T0)
    start(tmp_db, clock, fake)
    late = syn_evm("rh-late")
    emit_migration(tmp_db, "robinhood", late, migrated_ms=T0 - 120_000, ts_ms=T0, signature="tx-" + late)
    every = 7
    mints = [syn_mint(f"sol-{i}") for i in range(40)]
    for m in mints:
        emit_migration(tmp_db, "sol", m, migrated_ms=T0, ts_ms=T0)
    params = {**PARAMS, "chains": ["robinhood", "sol"], "sample_every": {"sol": every}}
    fake.fail = True  # any read would fail; the point is what is NOT read
    clock.t = T0 + 500
    go.tick(tmp_db, params, rpc=fake, clock=clock)
    by = {r["token"]: r for r in rows(tmp_db)}
    assert by[late]["status"] == "late" and by[late]["rpc_calls"] == 0
    sampled = [m for m in mints if by[m]["sampled"]]
    assert {by[m]["status"] for m in mints if not by[m]["sampled"]} == {"unsampled"}
    assert sampled == [m for m in mints if go.in_sample(m, every)] and 0 < len(sampled) < len(mints)
    calls = sum(r["rpc_calls"] for r in by.values())
    assert calls == 0  # failed requests are not attributed; unsampled/late rows are never asked about


# --------------------------------------------------------------------------------------
# the lifecycle: resolve, enter at +30 s, marks at +5/+15/+60 min and +6 h
# --------------------------------------------------------------------------------------


def run_until(conn, clock, fake, until_ms, step_ms=5_000, params=None):
    while clock.t < until_ms:
        clock.t = min(until_ms, clock.t + step_ms)
        go.tick(conn, params or PARAMS, rpc=fake, clock=clock)


def test_full_lifecycle_entry_at_30s_marks_on_schedule_all_chains(tmp_db):
    fake, clock = FakeChain(), Clock(T0)
    start(tmp_db, clock, fake)
    sol, rh, bsc = syn_mint("sol-life"), syn_evm("rh-life"), syn_evm("bsc-life")
    fake.add_sol(sol, 80 * 10**9, 2 * 10**14)
    fake.block_time["sig-sol"] = (T0 - 4_000) // 1000
    fake.add_rh(rh, go.EVM_ZERO, 4 * 10**18, 2 * 10**26)
    fake.add_bsc(bsc, 16 * 10**18, 2 * 10**26, tax=100)
    emit_migration(tmp_db, "sol", sol, migrated_ms=T0, ts_ms=T0, signature="sig-sol")  # receive clock
    emit_migration(tmp_db, "robinhood", rh, migrated_ms=T0, ts_ms=T0 + 500, signature="tx-" + rh)
    tmp_db.execute("INSERT INTO tokens (chain, address, created_ms, launchpad, migrated_ms, first_seen_ms, meta_json) "
                   "VALUES ('bsc', ?, ?, 'flap', ?, ?, '{}')", (bsc, T0 - 60_000, T0, T0 - 60_000))
    run_until(tmp_db, clock, fake, T0 + 45_000)
    got = {r["chain"]: r for r in rows(tmp_db)}
    sol_r = got["sol"]
    assert sol_r["graduated_basis"] == "block_time" and sol_r["graduated_ms"] == T0 - 4_000
    for r in got.values():
        assert r["status"] == "open", r
        assert r["entry_ms"] >= r["graduated_ms"] + 30_000
        assert 30_000 <= r["entry_offset_ms"] < 30_000 + 5_000 + 1  # the first pass at or after +30 s
        assert int(r["entry_tokens"]) > 0 and r["next_due_ms"] == r["entry_ms"] + 300_000
    assert json.loads(got["robinhood"]["pool_meta_json"])["hook_bps"] == 100
    assert got["robinhood"]["entry_basis"] == "v4_full_range"

    # price doubles on robinhood before the 15-minute mark
    run_until(tmp_db, clock, fake, T0 + 45_000 + 400_000)
    q, b = fake.reserves[rh]
    fake.reserves[rh] = (q * 2, b // 2)  # spot x4 for constant k? no: quote x2, base /2 -> spot x4
    run_until(tmp_db, clock, fake, T0 + 30_000 + 21_600_000 + 10_000, step_ms=60_000)
    got = {r["chain"]: r for r in rows(tmp_db)}
    for r in got.values():
        marks = json.loads(r["marks_json"])
        assert r["status"] == "done" and r["next_due_ms"] is None
        assert sorted(marks, key=int) == [str(h) for h in go.HORIZONS_S]
        for h in go.HORIZONS_S:
            due = r["entry_ms"] + h * 1000
            assert due <= marks[str(h)]["at_ms"] <= due + go.grace_ms(h)
    rh_marks = json.loads(got["robinhood"]["marks_json"])
    assert int(rh_marks["900"]["value"]) > 3 * int(got["robinhood"]["entry_quote_in"])
    # Unchanged pool at +5 min: selling back with our paper buy put back in costs the hook fee on
    # both legs and nothing else (0.99^2). Selling into a pool WITHOUT our buy would also pay
    # the 1% buy's impact twice (~0.961).
    ratio = Decimal(rh_marks["300"]["value"]) / Decimal(got["robinhood"]["entry_quote_in"])
    assert abs(ratio - Decimal("0.9801")) < Decimal("0.0005"), ratio


def test_a_mark_past_its_grace_is_recorded_missed_not_invented(tmp_db):
    fake, clock = FakeChain(), Clock(T0)
    start(tmp_db, clock, fake)
    tok = syn_evm("rh-miss")
    fake.add_rh(tok, go.EVM_ZERO, 4 * 10**18, 2 * 10**26)
    emit_migration(tmp_db, "robinhood", tok, migrated_ms=T0, ts_ms=T0, signature="tx-" + tok)
    run_until(tmp_db, clock, fake, T0 + 40_000)
    fake.fail = True  # the endpoint goes dark across the 5- and 15-minute marks
    run_until(tmp_db, clock, fake, T0 + 40_000 + 900_000 + 200_000, step_ms=30_000)
    fake.fail = False
    run_until(tmp_db, clock, fake, T0 + 40_000 + 3_650_000, step_ms=30_000)
    marks = json.loads(rows(tmp_db)[0]["marks_json"])
    assert marks["300"]["missed"] and marks["900"]["missed"]
    assert marks["3600"].get("value") is not None and "missed" not in marks["3600"]


def test_entry_window_closes_unresolved_and_late(tmp_db):
    fake, clock = FakeChain(), Clock(T0)
    start(tmp_db, clock, fake)
    tok = syn_evm("bsc-never")
    fake.add_bsc(tok, 16 * 10**18, 2 * 10**26, status=1)  # still on the curve: never resolves
    tmp_db.execute("INSERT INTO tokens (chain, address, created_ms, launchpad, migrated_ms, first_seen_ms, meta_json) "
                   "VALUES ('bsc', ?, ?, 'flap', ?, ?, '{}')", (tok, T0 - 60_000, T0, T0 - 60_000))
    run_until(tmp_db, clock, fake, T0 + 120_000)
    r = rows(tmp_db)[0]
    assert r["status"] == "unresolved" and r["entry_ms"] is None and r["note"].startswith("flap_status")


# --------------------------------------------------------------------------------------
# the RPC cap
# --------------------------------------------------------------------------------------


def test_rpc_cap_per_hour_binds_persists_and_resets(tmp_db):
    fake, clock = FakeChain(), Clock(T0)
    params = {**PARAMS, "chains": ["robinhood"], "max_calls_per_hour": 3, "max_batch": 1}
    start(tmp_db, clock, fake, params)
    toks = [syn_evm(f"rh-cap-{i}") for i in range(6)]
    for t in toks:
        fake.add_rh(t, go.EVM_ZERO, 4 * 10**18, 2 * 10**26)
        emit_migration(tmp_db, "robinhood", t, migrated_ms=T0, ts_ms=T0, signature="tx-" + t)
    out = []
    for i in range(10):
        clock.t = T0 + 1_000 + i * 1_000
        out.append(go.tick(tmp_db, params, rpc=fake, clock=clock))  # a fresh Observer each pass
    assert len(fake.methods) == 3  # three calls in the hour, across ten passes and ten Observers
    assert sum(o["budget_refused"] for o in out) >= 1
    assert sum(1 for r in rows(tmp_db) if r["status"] == "resolved") == 3
    # the starved rows never got their entry: their window closed, and they say so
    clock.t = T0 + 200_000
    go.tick(tmp_db, params, rpc=fake, clock=clock)
    assert sorted(r["status"] for r in rows(tmp_db)) == ["late"] * 3 + ["unresolved"] * 3
    # next UTC hour: the cap resets and a new graduation is read
    fresh = syn_evm("rh-cap-next-hour")
    fake.add_rh(fresh, go.EVM_ZERO, 4 * 10**18, 2 * 10**26)
    emit_migration(tmp_db, "robinhood", fresh, migrated_ms=T0 + H, ts_ms=T0 + H, signature="tx-" + fresh)
    clock.t = T0 + H + 1_000
    go.tick(tmp_db, params, rpc=fake, clock=clock)
    assert len(fake.methods) == 4
    state = json.loads(tmp_db.execute("SELECT value FROM kv WHERE key=?", (go.BUDGET_KEY,)).fetchone()["value"])
    assert state["d"] == len(fake.methods)


def test_daily_cap_binds(tmp_db):
    budget = go.RpcBudget(tmp_db, per_hour=100, per_day=5)
    assert budget.take(5, at_ms=T0)
    assert not budget.take(1, at_ms=T0 + H)  # new hour, same day
    assert budget.take(1, at_ms=T0 + 24 * H)


# --------------------------------------------------------------------------------------
# nothing can create an order
# --------------------------------------------------------------------------------------


def test_only_read_methods_can_be_sent(tmp_db):
    fake = FakeChain()
    obs = go.Observer(tmp_db, PARAMS, rpc=fake)
    for method in ("eth_sendRawTransaction", "eth_sendTransaction", "sendTransaction", "eth_sign"):
        with pytest.raises(PermissionError):
            obs.send("bsc", [("eth_call", [{}]), (method, [])], "x")
    assert fake.requests == 0  # refused before anything was sent
    assert go.READ_METHODS == {"eth_call", "eth_getTransactionReceipt", "getMultipleAccounts", "getTransaction"}


def test_a_full_lifecycle_writes_only_its_own_table_and_kv(tmp_db):
    written: set[str] = set()

    def authorizer(action, arg1, arg2, dbname, source):
        if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
            written.add(arg1)
        return sqlite3.SQLITE_OK

    fake, clock = FakeChain(), Clock(T0)
    start(tmp_db, clock, fake)
    sol, rh, bsc = syn_mint("sol-w"), syn_evm("rh-w"), syn_evm("bsc-w")
    fake.add_sol(sol, 80 * 10**9, 2 * 10**14)
    fake.add_rh(rh, syn_evm("usdg-like"), 4 * 10**24, 2 * 10**26)
    fake.add_bsc(bsc, 16 * 10**18, 2 * 10**26)
    emit_migration(tmp_db, "sol", sol, migrated_ms=T0, ts_ms=T0)
    emit_migration(tmp_db, "robinhood", rh, migrated_ms=T0, ts_ms=T0, signature="tx-" + rh)
    tmp_db.execute("INSERT INTO tokens (chain, address, created_ms, launchpad, migrated_ms, first_seen_ms, meta_json) "
                   "VALUES ('bsc', ?, ?, 'flap', ?, ?, '{}')", (bsc, T0 - 60_000, T0, T0 - 60_000))
    tables = ("orders", "positions", "signals", "decisions", "events", "tokens")
    before = {t: tmp_db.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"] for t in tables}
    tmp_db.set_authorizer(authorizer)
    try:
        run_until(tmp_db, clock, fake, T0 + 30_000 + 21_700_000, step_ms=30_000)
        go.evaluate(tmp_db, resamples=50)
        go.coverage(tmp_db, since_ms=0)
    finally:
        tmp_db.set_authorizer(None)
    assert written <= {go.TABLE, "kv"}, written
    assert {t: tmp_db.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"] for t in tables} == before
    assert {r["status"] for r in rows(tmp_db)} == {"done"}
    assert set(fake.methods) <= go.READ_METHODS


def test_module_imports_nothing_that_trades():
    src = Path(go.__file__).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            imported.update(f"{node.module}.{a.name}" for a in node.names)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    banned = ("executor", "engine", "paper", "lanes", "snipe", "gmgn", "gmgn_cli", "risk", "watchdog", "fills", "ops",
              "subprocess")
    assert not [m for m in imported if any(part in banned for part in m.split("."))], imported
    tree = ast.parse(src)
    docstrings = {id(n.body[0].value) for n in ast.walk(tree)
                  if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef))
                  and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
    code_strings = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
                    and id(n) not in docstrings]
    assert not [s for s in code_strings if any(w in s.lower() for w in ("gmgn", "sendraw", "sendtransaction", "orders",
                                                                           "positions", "signals", "decisions"))]


# --------------------------------------------------------------------------------------
# pricing
# --------------------------------------------------------------------------------------


def test_round_trip_on_an_unchanged_pool_costs_exactly_its_fees():
    pool = go.PoolState(quote=10**20, base=10**26, basis="t", fee_input_ppm=2500, buy_tax_bps=100, sell_tax_bps=100)
    size = 10**16  # 0.01% of the pool: impact negligible
    tokens = pool.buy(size)
    back = pool.holding(pool.into_pool(size)).sell(tokens)
    expected = Decimal("0.9975") ** 2 * Decimal("0.99") ** 2
    assert abs(Decimal(back) / size - expected) < Decimal("0.0002")
    assert go.PoolState(quote=0, base=10, basis="t").sell(5) == 0  # a drained pool is worth 0


def test_v4_state_decodes_extsload_and_matches_reserves():
    fake = FakeChain()
    tok = syn_evm("rh-v4")
    meta = fake.add_rh(tok, go.EVM_ZERO, 4 * 10**18, 2 * 10**26)
    m = {"token_is_currency0": tok.lower() == meta["c0"], "hook_bps": 100, "tick_lower": -887200, "tick_upper": 887200}
    state = go.v4_state(fake.rh_extsload(tok), m)
    assert state.basis == "v4_full_range" and state.fee_output_bps == 100
    assert abs(state.quote - 4 * 10**18) / (4 * 10**18) < 1e-9 and abs(state.base - 2 * 10**26) / (2 * 10**26) < 1e-9
    slot0 = go.decode_v4_slot0((3000 << 208) | ((-5 % (1 << 24)) << 160) | 12345)
    assert slot0 == {"sqrt_price_x96": 12345, "tick": -5, "protocol_fee": 0, "lp_fee_ppm": 3000}


def test_graduation_receipt_parse_verifies_key_and_measures_hook_fee():
    fake = FakeChain()
    tok, quote = syn_evm("rh-rcpt"), syn_evm("equity-quote")
    fake.add_rh(tok, quote, 10**24, 10**26)
    meta = go.parse_graduation_receipt(fake.rh_receipt(tok), tok)
    assert meta["quote"] == quote and meta["hook_bps"] == 100
    assert (meta["tick_lower"], meta["tick_upper"]) == (-887200, 887200)
    assert go.parse_graduation_receipt(fake.rh_receipt(tok), syn_evm("someone-else")) is None
    # topics MEASURED in a real graduation receipt (2026-10-05, box)
    assert go.topic_v4_swap().startswith("0x40e9cecb9f")
    assert go.topic_v4_modify_liquidity().startswith("0xf208f49127")


def test_pumpswap_pool_pda_matches_mainnet():
    # A public token mint and the PumpSwap pool it migrated into, read on mainnet
    # 2026-10-05 (owner pAMMBay..., base mint at byte 43 == this mint, quote WSOL).
    assert go.pumpswap_pool_address(MAINNET_MINT) == MAINNET_POOL


def test_pumpswap_fee_is_the_trees_single_number():
    from kaiba.execution.curve_price import PUMPSWAP_TIER1_TOTAL_FEE_BPS

    assert go.PUMPSWAP_FEE_BPS == PUMPSWAP_TIER1_TOTAL_FEE_BPS


# --------------------------------------------------------------------------------------
# the evaluator and the pre-declared rules
# --------------------------------------------------------------------------------------


def test_hypotheses_are_frozen():
    # Changing a rule, a pass line or a horizon after results exist is changing the test.
    # Update this digest ONLY with a work-log line saying what changed and why.
    assert go.DECLARATION_DIGEST == "2c650e909f8656c9"
    assert [h["rule"][0] for h in go.GRADUATION_HYPOTHESES] == ["A", "B", "C"]
    assert {h["chain"]: h["cost_per_leg"] for h in go.GRADUATION_HYPOTHESES} == {
        "robinhood": "0.045", "bsc": "0.035", "sol": "0.030"}


def put_row(conn, chain, i, *, value_ratio, native=1, offset_ms=31_000, day=0, horizon=3600, missed=False):
    token = syn_evm(f"{chain}-{i}") if chain != "sol" else syn_mint(f"{chain}-{i}")
    graduated = T0 + day * 24 * H + i * 1000
    go.insert_observation(conn, chain=chain, token=token, source="test", graduated_ms=graduated,
                          graduated_basis="block_time", detected_ms=graduated + 1000, p=go._params(PARAMS))
    quote_in = 10**18
    marks = {str(horizon): {"missed": True} if missed else {"value": str(int(Decimal(quote_in) * Decimal(value_ratio)))}}
    conn.execute(f"UPDATE {go.TABLE} SET status='done', entry_ms=?, entry_offset_ms=?, entry_quote_in=?, "
                 "entry_tokens='1', marks_json=?, quote_is_native=? WHERE token=?",
                 (graduated + offset_ms, offset_ms, str(quote_in), json.dumps(marks), native, token))


def test_net_return_charges_cost_on_both_legs():
    assert go.net_return(110, 100, Decimal("0.03")) == Decimal("1.1") * Decimal("0.97") ** 2 - 1


def test_evaluator_math_and_verdicts(tmp_db):
    # C (sol, 3.0%/leg): 40 rows over 4 days at value 1.20 -> net 0.1290... each: PASS
    for i in range(40):
        put_row(tmp_db, "sol", i, value_ratio="1.20", day=i % 4)
    # outside the 30-90 s entry window: never counted
    put_row(tmp_db, "sol", 999, value_ratio="50", offset_ms=200_000)
    # B (bsc): 40 losing rows over 3 days: FAIL
    for i in range(40):
        put_row(tmp_db, "bsc", i, value_ratio="0.90", day=i % 3)
    # A (robinhood, non-native only): 20 native winners are NOT the population; 10 non-native: PENDING
    for i in range(20):
        put_row(tmp_db, "robinhood", i, value_ratio="3", native=1, day=i % 3)
    for i in range(20, 30):
        put_row(tmp_db, "robinhood", i, value_ratio="1.5", native=0, day=i % 3)
    report = {r["rule"][0]: r for r in go.evaluate(tmp_db, resamples=500)}
    c = report["C"]
    expected = float(Decimal("1.20") * Decimal("0.97") ** 2 - 1)
    assert c["n"] == 40 and abs(c["mean"] - expected) < 1e-12 and c["verdict"] == "PASS"
    assert abs(c["bootstrap_lower"] - expected) < 1e-12  # identical returns: no spread
    assert report["B"]["verdict"] == "FAIL" and report["B"]["mean"] < 0
    assert report["A"]["n"] == 10 and report["A"]["verdict"] == "PENDING"
    assert all(r["declaration"] == go.DECLARATION_DIGEST for r in report.values())


def test_evaluator_inconclusive_when_marks_missing_and_bootstrap_is_a_lower_bound(tmp_db):
    for i in range(40):
        put_row(tmp_db, "bsc", i, value_ratio="1.5" if i % 2 else "0.9", day=i % 4)
    for i in range(40, 50):
        put_row(tmp_db, "bsc", i, value_ratio="1", day=0, missed=True)
    r = next(x for x in go.evaluate(tmp_db, resamples=2000) if x["rule"].startswith("B"))
    assert r["missed"] == 10 and abs(r["missed_share"] - 0.2) < 1e-12
    assert r["verdict"] == "INCONCLUSIVE"
    assert r["bootstrap_lower"] < r["mean"]
    assert go.bootstrap_lower([1.0, 2.0, 3.0], resamples=200, seed=1, pct=0.025) == \
        go.bootstrap_lower([1.0, 2.0, 3.0], resamples=200, seed=1, pct=0.025)  # deterministic


MAINNET_MINT = "ArU3q1WMysppnTDNGMXFwDvaFb8B79iCZPEcXA46pump"  # a pump.fun token (not a wallet)
MAINNET_POOL = "9VjWegmHJKrpmmX6rXBLL67LdwP7UrM7yPzriGZoGn39"  # owner pAMMBay..., base mint == the mint
