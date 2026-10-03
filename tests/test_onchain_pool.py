"""LOG-ONLY on-chain pool prices for Robinhood positions (``kaiba.execution.onchain_pool``).

Three things are pinned here, in this order of importance:

1. **It cannot change or slow a decision.** Off by default and inert while off; a reader
   that raises never breaks a tick; the tick only hands over a snapshot; and the budget it
   spends is never budget a live sell needs (reserved at ``Priority.POSITION``, so it is
   refused exactly where ``Priority.EXIT`` would have been let through).
2. **One HTTP request per tick.** Discovery rides the same multicall as the price reads;
   the rare v4 ``Initialize`` lookup rides the same JSON-RPC batch.
3. **The bytes are decoded correctly.** ``live_20261002.json`` is a capture of the real
   reads this module made against Robinhood Chain on 2026-10-02, replayed through an
   ABI encoder/decoder written independently in this file.
"""

from __future__ import annotations

import json
import threading
import time
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

from kaiba.core import limiter
from kaiba.core.db import fetch_all, fetch_one
from kaiba.core.schemas import Chain, Lane, LaneMode, now_ms
from kaiba.execution import onchain_pool as op
from kaiba.execution import watchdog as wd
from kaiba.execution.protection import ProtectionConfig
from tests.test_watchdog import (  # noqa: F401 - fixtures are used by name
    FakeSource,
    RecordingSubmitter,
    events_named,
    make_position,
    risk_file,
)

FIXTURE = Path(__file__).parent / "fixtures" / "onchain_pool" / "live_20261002.json"

# --------------------------------------------------------------------------------------
# an ABI implementation independent of the module's
# --------------------------------------------------------------------------------------


def _w(value: int) -> bytes:
    return int(value).to_bytes(32, "big")


def decode_call3(data_hex: str) -> list[tuple[str, str]]:
    """``aggregate3`` calldata -> ``[(target, calldata)]``, written from the ABI spec."""
    assert data_hex.startswith(op.SEL_AGGREGATE3)
    raw = bytes.fromhex(data_hex[10:])

    def word(at: int) -> int:
        return int.from_bytes(raw[at : at + 32], "big")

    base = word(0)
    n = word(base)
    heads = base + 32
    out = []
    for i in range(n):
        start = heads + word(heads + 32 * i)
        target = "0x" + raw[start + 12 : start + 32].hex()
        assert word(start + 32) == 1, "allowFailure must be true for every subcall"
        data_at = start + word(start + 64)
        length = word(data_at)
        out.append((target, "0x" + raw[data_at + 32 : data_at + 32 + length].hex()))
    return out


def encode_results(results: list[tuple[bool, bytes]]) -> str:
    """``(bool, bytes)[]`` the way Multicall3 returns it."""
    tuples = []
    for ok, blob in results:
        tuples.append(_w(1 if ok else 0) + _w(0x40) + _w(len(blob)) + blob + bytes((-len(blob)) % 32))
    offsets, pos = [], 32 * len(tuples)
    for t in tuples:
        offsets.append(_w(pos))
        pos += len(t)
    return "0x" + (_w(0x20) + _w(len(tuples)) + b"".join(offsets) + b"".join(tuples)).hex()


class FakeChain:
    """Answers multicalls from a ``"target|calldata" -> (ok, hex)`` table. Records every POST."""

    def __init__(self, answers: dict, logs: dict | None = None, *, block: int = 77_767_820,
                 block_ts: int = 1_790_897_500, fail: Exception | None = None) -> None:
        self.answers = answers
        self.logs = logs or {}
        self.block, self.block_ts = block, block_ts
        self.fail = fail
        self.posts: list = []
        self.asked: list[list[tuple[str, str]]] = []
        self.unknown: list[str] = []

    def answer(self, target: str, data: str) -> tuple[bool, bytes]:
        if (target, data) == (op.ARBSYS, op.SEL_ARB_BLOCK_NUMBER):
            return True, _w(self.block)
        if (target, data) == (op.MULTICALL3, op.SEL_BLOCK_TIMESTAMP):
            return True, _w(self.block_ts)
        found = self.answers.get(f"{target}|{data}")
        if found is None:
            self.unknown.append(f"{target}|{data}")
            return False, b""
        ok, blob = found
        return bool(ok), bytes.fromhex(blob) if isinstance(blob, str) else blob

    def __call__(self, payload, timeout_s):  # noqa: ANN001 - transport double
        self.posts.append(payload)
        if self.fail is not None:
            raise self.fail
        items = payload if isinstance(payload, list) else [payload]
        out = []
        for item in items:
            if item["method"] == "eth_call":
                assert item["params"][0]["to"] == op.MULTICALL3
                calls = decode_call3(item["params"][0]["data"])
                self.asked.append(calls)
                result = encode_results([self.answer(t, d) for t, d in calls])
                out.append({"jsonrpc": "2.0", "id": item["id"], "result": result})
            elif item["method"] == "eth_getLogs":
                pool = item["params"][0]["topics"][1]
                out.append({"jsonrpc": "2.0", "id": item["id"], "result": self.logs.get(pool, [])})
            else:  # pragma: no cover - the module sends nothing else
                raise AssertionError(item["method"])
        return out if isinstance(payload, list) else out[0]


def _set_rh_budget(risk_path: Path) -> None:
    """The box's robinhood-rpc envelope, written explicitly so no test leans on config/."""
    import yaml

    raw = yaml.safe_load(risk_path.read_text())
    raw.setdefault("provider_budgets", {})["robinhood-rpc"] = {
        "min_interval_ms": 1500, "capacity": 3, "refill_per_s": 0.6, "max_inflight": 1,
    }
    risk_path.write_text(yaml.safe_dump(raw))


def _cfg(**kw) -> ProtectionConfig:
    base = {"onchain_price_log": True, "onchain_price_log_min_interval_s": 0}
    base.update(kw)
    return ProtectionConfig(**base)


def _eth_sample(conn, price: str = "2698.22", ts: int | None = None) -> None:
    conn.execute(
        "INSERT INTO native_prices (chain, ts_ms, price_usd, source, pair, liquidity_usd, receipt_json) "
        "VALUES ('eth', ?, ?, 'dexscreener', 'test', '1', '{}')",
        (ts or now_ms(), price),
    )


def _rh_position(conn, pid: str, token: str, *, mode: LaneMode = LaneMode.LIVE, qty: int = 1_000,
                 entry: str = "1.0") -> None:
    make_position(conn, position_id=pid, token=token, mode=mode, chain=Chain.ROBINHOOD,
                  lane=Lane.SM_TRENCHES,
                  qty=qty, entry=entry)


def _remember(conn, pid: str, token: str, *, pool_id: str | None, source: str = "prices:dexscreener",
              price: str = "0.5", observed_ms: int | None = None) -> None:
    value = {"price": {"price_usd": price, "source": source, "observed_ms": observed_ms or now_ms(),
                       "pool_id": pool_id, "chain": "robinhood", "token": token}}
    conn.execute("INSERT INTO kv (key, value, updated_ms) VALUES (?,?,?)",
                 (f"watchdog.quote:{pid}:robinhood:{token}", json.dumps(value), now_ms()))


def _token(conn, address: str, decimals: int = 18) -> None:
    conn.execute("INSERT INTO tokens (chain, address, symbol, decimals, first_seen_ms) VALUES ('robinhood',?,?,?,?)",
                 (address, "T", decimals, now_ms()))


def _job(conn) -> op.Job:
    from kaiba.execution.paper import open_positions

    targets = [
        op.Target(p.position_id, p.token.lower(), p.token, p.entry_price_usd)
        for p in open_positions(conn) if p.chain is Chain.ROBINHOOD and p.mode is not LaneMode.SHADOW
    ]
    return op.Job(targets=targets, created_ms=now_ms())


# --------------------------------------------------------------------------------------
# constants and the codec
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "signature"),
    [
        (op.SEL_AGGREGATE3, "aggregate3((address,bool,bytes)[])"),
        (op.SEL_BLOCK_TIMESTAMP, "getCurrentBlockTimestamp()"),
        (op.SEL_ARB_BLOCK_NUMBER, "arbBlockNumber()"),
        (op.SEL_SLOT0, "slot0()"),
        (op.SEL_GET_RESERVES, "getReserves()"),
        (op.SEL_TOKEN0, "token0()"),
        (op.SEL_TOKEN1, "token1()"),
        (op.SEL_DECIMALS, "decimals()"),
        (op.SEL_LIQUIDITY, "liquidity()"),
        (op.SEL_EXTSLOAD, "extsload(bytes32)"),
        (op.SEL_POOL_KEYS, "poolKeys(bytes25)"),
        (op.SEL_GET_POOL, "getPool(address,address,uint24)"),
    ],
)
def test_every_selector_is_the_function_it_claims(value, signature):
    from kaiba.execution.policy import selector_of

    assert value == selector_of(signature)


def test_initialize_topic_is_the_v4_event():
    from kaiba.execution.policy import keccak256

    sig = b"Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)"
    assert op.TOPIC_V4_INITIALIZE == "0x" + keccak256(sig).hex()


def test_v4_contracts_match_the_ingest_module():
    from kaiba.ingest import robinhood

    assert op.POOL_MANAGER == robinhood.POOL_MANAGER
    assert op.POSITION_MANAGER == robinhood.POSITION_MANAGER
    assert op.RPC_URL == robinhood.RPC_URL
    assert op.PROVIDER == robinhood.PROVIDER


def test_aggregate3_round_trips_through_an_independent_decoder():
    calls = [
        (op.ARBSYS, op.SEL_ARB_BLOCK_NUMBER),
        ("0x" + "ab" * 20, op.SEL_SLOT0),
        (op.POOL_MANAGER, op.encode_call(op.SEL_EXTSLOAD, bytes(range(32)))),
        ("0x" + "cd" * 20, op.SEL_POOL_KEYS + "11" * 25 + "00" * 7),  # 36-byte calldata, padded
    ]
    data = op.encode_aggregate3(calls)
    assert data.startswith(op.SEL_AGGREGATE3)
    assert (len(data) - 10) % 64 == 0, "the body must be whole words"
    assert decode_call3(data) == calls


def test_results_decode_against_an_independent_encoder():
    results = [(True, _w(7)), (False, b""), (True, b"\x01" * 33), (True, _w(1) + _w(2) + _w(3))]
    assert op.decode_aggregate3(encode_results(results), len(results)) == results


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "deadbeef",
        "0xzz",
        "0x",
        encode_results([(True, _w(1))])[:-64],  # truncated inside the last word
    ],
)
def test_malformed_returns_are_no_data_not_an_exception(bad):
    assert op.decode_aggregate3(bad, 1) is None


def test_a_return_with_the_wrong_count_is_refused():
    assert op.decode_aggregate3(encode_results([(True, _w(1)), (True, _w(2))]), 3) is None


def test_an_offset_pointing_past_the_end_is_refused():
    good = bytes.fromhex(encode_results([(True, _w(1))])[2:])
    # the single tuple offset -> far past the end of the buffer
    hostile = good[:64] + _w(10_000) + good[96:]
    assert op.decode_aggregate3("0x" + hostile.hex(), 1) is None


# --------------------------------------------------------------------------------------
# pricing arithmetic
# --------------------------------------------------------------------------------------


def _sqrt_x96(raw_ratio: Decimal) -> int:
    with localcontext() as ctx:
        ctx.prec = 80
        return int(raw_ratio.sqrt() * (Decimal(2) ** 96))


def test_sqrt_price_is_decimal_adjusted_and_oriented():
    # token0 = an 18-dec meme, token1 = USDG (6 dec), 2.5 USDG per token:
    # raw = 2.5 * 10^6 / 10^18 base units per base unit.
    sqrt = _sqrt_x96(Decimal("2.5") * Decimal(10) ** -12)
    as0 = op.price_from_sqrt(sqrt, 18, 6, token_is0=True)
    assert abs(as0 / Decimal("2.5") - 1) < Decimal("1e-15")
    # Same pool read from the other side: USDG is currency0's partner, so the price of
    # currency1 (USDG) in currency0 is the inverse, 0.4.
    as1 = op.price_from_sqrt(sqrt, 18, 6, token_is0=False)
    assert abs(as1 / Decimal("0.4") - 1) < Decimal("1e-15")


def test_v2_reserves_price_both_ways():
    r0, r1 = 1_000 * 10**18, 2_500 * 10**6  # 1000 tokens (18) against 2500 USDG (6)
    assert op.price_from_reserves(r0, r1, 18, 6, token_is0=True) == Decimal("2.5")
    assert op.price_from_reserves(r0, r1, 18, 6, token_is0=False) == Decimal("0.4")
    assert op.price_from_reserves(0, r1, 18, 6, token_is0=True) is None


def test_v4_slot_masks_the_tick_and_fee_bits():
    sqrt = _sqrt_x96(Decimal(1))
    word = (0xABCDEF << 160) | sqrt  # tick / fee bits above the price
    assert op.sqrt_price_from_v4_slot(_w(word)) == sqrt
    assert op.sqrt_price_from_v4_slot(_w(0)) is None, "an empty slot is not a price of zero"


# --------------------------------------------------------------------------------------
# resolution, against real chain bytes
# --------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def live() -> dict:
    return json.loads(FIXTURE.read_text())


HARMONIC_POOL = "0x77ea11bbfb8f1259c702cb0ebc2105b7c007db89cf30c2f1a8776886a4467c07"
HARMONIC = "0xdee52f2ab639b6942b0d0f0565400b93b7a0fbe5"
SYNTHO_POOL = "0x132a8fa82173b6f44ce97ae79cbd1a049e25d0a94a7b48802f1dadfdb7fd5f14"
SYNTHO = "0xa44eaa51fa091f58412c66e389d9058144ce5aac"


def _pool_keys_call(pool_id: str) -> str:
    return f"{op.POSITION_MANAGER}|{op.SEL_POOL_KEYS}{pool_id[2:52]}{'00' * 7}"


def test_a_real_pool_key_hashes_back_to_its_pool_and_its_slot_holds_a_price(live):
    ok, blob = live["calls"][_pool_keys_call(HARMONIC_POOL)]
    key = op.decode_pool_key(bytes.fromhex(blob))
    assert ok and key is not None
    assert op.v4_pool_id(*key) == HARMONIC_POOL
    entry = op.PoolEntry(pool=HARMONIC_POOL, token=HARMONIC)
    entry.take_discovery([(True, bytes.fromhex(blob))])
    assert (entry.state, entry.kind, entry.token_is0, entry.quote) == ("ready", "v4", False, None)
    # The extsload at the computed slot is what came back non-empty from the chain.
    target, data = entry.price_call()
    ok_s, slot_blob = live["calls"][f"{target}|{data}"]
    assert ok_s and op.sqrt_price_from_v4_slot(bytes.fromhex(slot_blob))


def test_a_key_the_position_manager_lacks_comes_from_the_real_initialize_log(live):
    ok, blob = live["calls"][_pool_keys_call(SYNTHO_POOL)]
    entry = op.PoolEntry(pool=SYNTHO_POOL, token=SYNTHO)
    entry.take_discovery([(ok, bytes.fromhex(blob))])
    assert entry.state == "logs" and entry.reason == "pool_keys_empty"
    params = entry.logs_params(77_767_820)
    span = int(params["toBlock"], 16) - int(params["fromBlock"], 16) + 1
    assert span == op.GET_LOGS_MAX_SPAN, "the public RPC refuses one block more than this"
    entry.take_logs(live["logs"][SYNTHO_POOL], reached_genesis=False)
    assert (entry.state, entry.kind, entry.token_is0) == ("ready", "v4", False)
    assert entry.quote == "0x2e0847e8910a9732eb3fb1bb4b70a580adad4fe3"


def test_a_key_that_does_not_hash_to_the_pool_is_refused(live):
    ok, blob = live["calls"][_pool_keys_call(HARMONIC_POOL)]
    key = list(op.decode_pool_key(bytes.fromhex(blob)))
    key[2] = 3000  # a different fee tier is a different pool
    words = (
        op._addr_word(key[0]) + op._addr_word(key[1]) + _w(key[2]) + _w(key[3]) + op._addr_word(key[4])
    )
    entry = op.PoolEntry(pool=HARMONIC_POOL, token=HARMONIC)
    entry.take_discovery([(True, words)])
    assert (entry.state, entry.reason) == ("dead", "v4_key_mismatch")


def test_a_pool_that_does_not_hold_the_token_is_refused(live):
    ok, blob = live["calls"][_pool_keys_call(HARMONIC_POOL)]
    entry = op.PoolEntry(pool=HARMONIC_POOL, token=SYNTHO)
    entry.take_discovery([(True, bytes.fromhex(blob))])
    assert (entry.state, entry.reason) == ("dead", "token_not_in_pool")


def test_v3_and_v2_are_told_apart_by_which_state_call_answers():
    token, quote = "0x" + "11" * 20, "0x" + "22" * 20
    t0, t1 = (True, op._addr_word(token)), (True, op._addr_word(quote))
    slot0 = (True, _w(_sqrt_x96(Decimal(1))) + _w(0) * 6)
    reserves = (True, _w(10**18) + _w(2 * 10**18) + _w(123))
    nothing = (False, b"")

    v3 = op.PoolEntry(pool="0x" + "33" * 20, token=token)
    v3.take_discovery([t0, t1, slot0, nothing])
    assert (v3.state, v3.kind, v3.token_is0, v3.quote) == ("ready", "v3", True, quote)

    v2 = op.PoolEntry(pool="0x" + "44" * 20, token=token)
    v2.take_discovery([t0, t1, nothing, reserves])
    assert (v2.state, v2.kind) == ("ready", "v2")

    both = op.PoolEntry(pool="0x" + "55" * 20, token=token)
    both.take_discovery([t0, t1, slot0, reserves])
    assert both.state == "key" and both.reason == "pool_kind_ambiguous"


def test_the_finder_picks_the_deepest_usdg_pool_and_never_a_dry_one():
    asset = "0x" + "66" * 20
    finder = op.V3Finder(asset=asset)
    queries = finder._queries()
    assert len(finder.discovery_calls(0)) == len(queries) == 8
    pools = {q: "0x" + format(i + 1, "040x") for i, q in enumerate(queries)}
    finder.take_discovery([(True, op._addr_word(pools[q])) for q in queries], 0)
    assert finder.state == "depth" and len(finder.candidates) == 8
    good = _w(_sqrt_x96(Decimal(1))) + _w(0) * 6
    liquidity = {  # USDG 100 is DRY (its slot0 is garbage), 3000 is deepest live
        (op.ROBINHOOD_USDG, 100): 0, (op.ROBINHOOD_USDG, 500): 10, (op.ROBINHOOD_USDG, 3000): 1_000,
        (op.ROBINHOOD_USDG, 10000): 5, (op.ROBINHOOD_WETH, 3000): 10**12,
    }
    results = []
    for q, fee, _pool in finder.candidates:
        results += [(True, good), (True, _w(liquidity.get((q, fee), 0)))]
    finder.take_discovery(results, 0)
    assert (finder.state, finder.pool, finder.quote) == ("done", pools[(op.ROBINHOOD_USDG, 3000)], op.ROBINHOOD_USDG)


def test_a_dry_usdg_pool_alone_is_not_a_route():
    """MEASURED shape: META/USDG 100 bps exists with zero liquidity and reads 1.0e12."""
    asset = "0x" + "99" * 20
    finder = op.V3Finder(asset=asset)
    queries = finder._queries()
    hits = {(op.ROBINHOOD_USDG, 100): "0x" + "aa" * 20, (op.ROBINHOOD_WETH, 3000): "0x" + "bb" * 20}
    finder.take_discovery([(True, op._addr_word(hits[q]) if q in hits else _w(0)) for q in queries], 0)
    garbage = _w(_sqrt_x96(Decimal(10) ** 12)) + _w(0) * 6
    depth = {hits[(op.ROBINHOOD_USDG, 100)]: 0, hits[(op.ROBINHOOD_WETH, 3000)]: 10**18}
    finder.take_discovery([r for _q, _f, pool in finder.candidates for r in ((True, garbage), (True, _w(depth[pool])))], 0)
    assert (finder.state, finder.quote, finder.pool) == ("done", op.ROBINHOOD_WETH, hits[(op.ROBINHOOD_WETH, 3000)])


def test_the_finder_falls_back_to_weth_then_gives_up_and_retries_later():
    asset = "0x" + "77" * 20
    finder = op.V3Finder(asset=asset)
    queries = finder._queries()
    only_weth = [(True, op._addr_word("0x" + "88" * 20)) if q == (op.ROBINHOOD_WETH, 500) else (True, _w(0))
                 for q in queries]
    finder.take_discovery(only_weth, 0)
    finder.take_discovery([(True, _w(_sqrt_x96(Decimal(1))) + _w(0) * 6), (True, _w(99))], 0)
    assert (finder.state, finder.quote) == ("done", op.ROBINHOOD_WETH)

    none = op.V3Finder(asset=asset)
    none.take_discovery([(True, _w(0))] * 8, 1_000)
    assert none.state == "dead" and none.reason == "no_v3_pool"
    assert none.discovery_calls(1_000) == [], "a dead finder waits out its backoff"
    assert len(none.discovery_calls(1_000 + op.FINDER_RETRY_MS)) == 8, "and then asks again"


# --------------------------------------------------------------------------------------
# replay of the live capture: the whole worker, one request per job
# --------------------------------------------------------------------------------------


def _seed_live(conn, live) -> None:
    seed = live["seed"]
    for p in seed["positions"]:
        row = dict(p)
        cols = list(row)
        conn.execute(f"INSERT INTO positions ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                     [row[c] for c in cols])
    for key, v in seed["kv"].items():
        conn.execute("INSERT INTO kv (key, value, updated_ms) VALUES (?,?,?)", (key, v["value"], v["updated_ms"]))
    for address, t in seed["tokens"].items():
        conn.execute("INSERT INTO tokens (chain, address, symbol, decimals, first_seen_ms) VALUES ('robinhood',?,?,?,?)",
                     (address, t["symbol"], t["decimals"], now_ms()))
    for pid, stop in seed["watchdog_state"].items():
        conn.execute("INSERT INTO watchdog_state (position_id, stop_price_usd, updated_ms) VALUES (?,?,?)",
                     (pid, stop, now_ms()))
    _eth_sample(conn, seed["eth_usd"])


def _refill(conn) -> None:
    """What ten seconds of refill does on the box, for jobs a test runs back to back."""
    conn.execute("UPDATE provider_state SET last_call_ms=0, credit_milli=3000, last_refill_ms=? "
                 "WHERE provider=?", (now_ms(), op.PROVIDER))


def _rows(conn, suffix: str) -> list[dict]:
    return fetch_all(conn, "SELECT * FROM onchain_price_samples WHERE position_id LIKE ? ORDER BY id",
                     (f"%{suffix}",))


def test_replaying_the_live_reads_prices_the_open_book(tmp_db, risk_file, live):
    _set_rh_budget(risk_file)
    _seed_live(tmp_db, live)
    chain = FakeChain(live["calls"], live["logs"])
    logger = op.OnchainPriceLog(post=chain, inline=True)
    for _ in range(5):
        logger.run_job(_job(tmp_db))
        _refill(tmp_db)  # back-to-back jobs; on the box they are >= 10 s apart

    assert chain.unknown == [], f"asked the chain something it was never asked live: {chain.unknown[:3]}"
    assert len(chain.posts) == 5, "one HTTP request per job, discovery included"
    batches = [p for p in chain.posts if isinstance(p, list)]
    assert batches and all([i["method"] for i in b] == ["eth_call", "eth_getLogs"] for b in batches)

    # HARMONIC: v4, native ETH quote, DexScreener 58 s old at 0.007433.
    harmonic = _rows(tmp_db, "03c93aaa")
    assert harmonic and harmonic[-1]["pool_kind"] == "v4" and harmonic[-1]["quote_token"] is None
    # Within 10% of a 58 s-old mark; a wrong side or a wrong decimal is off by orders of
    # magnitude, which is what this bound is for. (Live: +2.1% and +4.3% two blocks apart.)
    assert abs(Decimal(harmonic[-1]["price_usd"]) / Decimal("0.007433") - 1) < Decimal("0.10")
    assert harmonic[-1]["incumbent_source"] == "prices:dexscreener"
    assert harmonic[-1]["source"] == "onchain:v4|eth_usd:native_prices"
    assert harmonic[-1]["block_number"] and harmonic[-1]["stop_price_usd"]

    # Syntho: v4 key only in the Initialize log, quote priced through its own v3 USDG pool.
    syntho = _rows(tmp_db, "0278b8d7")
    assert syntho and abs(Decimal(syntho[-1]["price_usd"]) / Decimal("0.00002363") - 1) < Decimal("0.01")
    assert syntho[-1]["source"].startswith("onchain:v4|ref:0x")

    # NVDA: a v3 pool against USDG (6 decimals).
    nvda = _rows(tmp_db, "64fdf79e")
    assert nvda[-1]["pool_kind"] == "v3" and nvda[-1]["source"] == "onchain:v3|usdg_peg"
    assert Decimal("150") < Decimal(nvda[-1]["price_usd"]) < Decimal("350")

    # CSTR is quoted in another meme token with no v3 route: price in quote units only.
    cstr = _rows(tmp_db, "57ae2852")
    assert cstr and cstr[-1]["price_usd"] is None and cstr[-1]["price_quote"]
    assert "quote_unpriced:no_v3_pool" in cstr[-1]["source"]

    # ORD is priced on its Pons curve already (evm-venue:pons): never read here.
    assert _rows(tmp_db, "654b3376") == []
    # MEGADUCK has no pool anywhere we can see: no rows, and no invented price.
    assert _rows(tmp_db, "e9ac2620") == []


# --------------------------------------------------------------------------------------
# a synthetic book with exact expected prices, including a WETH-routed quote
# --------------------------------------------------------------------------------------

TOKEN_T = "0x" + "a1" * 20
QUOTE_X = "0x" + "b2" * 20
REF_X_WETH = "0x" + "c3" * 20
HOOKS = "0x" + "d4" * 20


def _synthetic_answers() -> tuple[dict, str]:
    # v4 pool T/X: 0xa1.. < 0xb2.., so T is currency0 and the raw ratio is X per T.
    key = (TOKEN_T, QUOTE_X, 8_388_608, 200, HOOKS)
    pool_id = op.v4_pool_id(*key)
    # 0.002 X per T, both 18 decimals -> raw ratio 0.002 (currency1 per currency0)
    t_slot = (0x01F371 << 160) | _sqrt_x96(Decimal("0.002"))
    # X/WETH v3: 0.5 WETH per X; X (0xb2) > WETH (0x0b), so WETH is token0 and the raw
    # ratio (X per WETH) is 2.
    ref_slot0 = _w(_sqrt_x96(Decimal(2))) + _w(0) * 6
    answers = {
        _pool_keys_call(pool_id): (True, b"".join(
            [op._addr_word(key[0]), op._addr_word(key[1]), _w(key[2]), _w(key[3]), op._addr_word(key[4])])),
        f"{op.POOL_MANAGER}|{op.encode_call(op.SEL_EXTSLOAD, bytes.fromhex(op.v4_state_slot(pool_id)[2:]))}":
            (True, _w(t_slot)),
        f"{QUOTE_X}|{op.SEL_DECIMALS}": (True, _w(18)),
        f"{REF_X_WETH}|{op.SEL_SLOT0}": (True, ref_slot0),
        f"{REF_X_WETH}|{op.SEL_LIQUIDITY}": (True, _w(10**20)),
    }
    for q in (op.ROBINHOOD_USDG, op.ROBINHOOD_WETH):
        for fee in op.V3_FEE_TIERS:
            data = op.encode_call(op.SEL_GET_POOL, op._addr_word(QUOTE_X), op._addr_word(q), _w(fee))
            hit = q == op.ROBINHOOD_WETH and fee == 3000
            answers[f"{op.V3_FACTORY}|{data}"] = (True, op._addr_word(REF_X_WETH) if hit else _w(0))
    return answers, pool_id


def _synthetic_book(conn, *, eth: str | None = "2000") -> tuple[FakeChain, str]:
    answers, pool_id = _synthetic_answers()
    _rh_position(conn, "pos_t", TOKEN_T)
    _token(conn, TOKEN_T, 18)
    _remember(conn, "pos_t", TOKEN_T, pool_id=pool_id, price="1.9", observed_ms=now_ms() - 30_000)
    conn.execute("INSERT INTO watchdog_state (position_id, stop_price_usd, updated_ms) VALUES ('pos_t','0.7',?)",
                 (now_ms(),))
    if eth is not None:
        _eth_sample(conn, eth)
    return FakeChain(answers), pool_id


def test_a_weth_routed_quote_prices_exactly(tmp_db, risk_file):
    _set_rh_budget(risk_file)
    chain, _ = _synthetic_book(tmp_db)
    logger = op.OnchainPriceLog(post=chain, inline=True)
    for _ in range(5):
        logger.run_job(_job(tmp_db))
        _refill(tmp_db)
    assert chain.unknown == []
    rows = _rows(tmp_db, "pos_t")
    assert rows, logger.stats.payload()
    last = rows[-1]
    # T = 0.002 X; X = 0.5 WETH; WETH = $2000  ->  $2.00
    assert abs(Decimal(last["price_usd"]) - Decimal("2")) < Decimal("1e-9")
    assert abs(Decimal(last["price_quote"]) - Decimal("0.002")) < Decimal("1e-15")
    assert last["source"] == f"onchain:v4|ref:{REF_X_WETH}:eth_usd:native_prices"
    assert last["incumbent_price_usd"] == "1.9" and 29_000 <= last["incumbent_age_ms"] < 120_000
    assert last["stop_price_usd"] == "0.7" and last["block_number"] == chain.block


def test_no_eth_rate_records_the_quote_price_and_no_usd(tmp_db, risk_file):
    _set_rh_budget(risk_file)
    chain, _ = _synthetic_book(tmp_db, eth=None)
    logger = op.OnchainPriceLog(post=chain, inline=True)
    for _ in range(5):
        logger.run_job(_job(tmp_db))
        _refill(tmp_db)
    rows = _rows(tmp_db, "pos_t")
    assert rows and rows[-1]["price_usd"] is None and rows[-1]["price_quote"], "never a guessed rate"


# --------------------------------------------------------------------------------------
# the budget: never a live sell's
# --------------------------------------------------------------------------------------


def _state(conn, **cols) -> None:
    limiter._state(conn, op.PROVIDER)  # create the row
    sets = ",".join(f"{k}=?" for k in cols)
    conn.execute(f"UPDATE provider_state SET {sets} WHERE provider=?", (*cols.values(), op.PROVIDER))


def test_the_only_inflight_slot_held_by_someone_else_means_no_call(tmp_db, risk_file):
    """max_inflight is 1 and something holds it. EXIT would bypass that; we must not."""
    _set_rh_budget(risk_file)
    chain, _ = _synthetic_book(tmp_db)
    _state(tmp_db, inflight=1, last_call_ms=now_ms() - 5_000, credit_milli=3_000, last_refill_ms=now_ms())

    report = op.OnchainPriceLog(post=chain, inline=True).run_job(_job(tmp_db))
    assert chain.posts == [] and report["skipped"] == "limiter:max inflight"

    # Control: the SAME state lets an exit's read through -- so the refusal above is the
    # priority we chose, not a state in which nobody could call.
    weight = limiter.reserve(op.PROVIDER, "chain.price", limiter.Priority.EXIT)
    limiter.release(op.PROVIDER, "chain.price", weight=weight)


def test_the_exit_overdraft_is_never_drawn(tmp_db, risk_file):
    """Bucket below one call: EXIT may overdraw to -EXIT_OVERDRAFT_WEIGHT; a log read may not."""
    _set_rh_budget(risk_file)
    chain, _ = _synthetic_book(tmp_db)
    _state(tmp_db, inflight=0, last_call_ms=now_ms() - 60_000, credit_milli=0, last_refill_ms=now_ms())

    report = op.OnchainPriceLog(post=chain, inline=True).run_job(_job(tmp_db))
    assert chain.posts == [] and report["skipped"] == "limiter:bucket exhausted"
    credit = fetch_one(tmp_db, "SELECT credit_milli FROM provider_state WHERE provider=?", (op.PROVIDER,))
    assert credit["credit_milli"] <= 1_000 and credit["credit_milli"] >= 0, "nothing was charged"

    weight = limiter.reserve(op.PROVIDER, "chain.price", limiter.Priority.EXIT)  # the control
    limiter.release(op.PROVIDER, "chain.price", weight=weight)


def test_a_family_cooldown_refuses_the_logger_too(tmp_db, risk_file):
    _set_rh_budget(risk_file)
    chain, _ = _synthetic_book(tmp_db)
    _state(tmp_db, inflight=0, last_call_ms=0, credit_milli=3_000, last_refill_ms=now_ms())
    tmp_db.execute("INSERT INTO provider_family_bans (provider, family, banned_until_ms, reason) VALUES (?,?,?,?)",
                   (op.PROVIDER, "chain", now_ms() + 60_000, "429"))
    report = op.OnchainPriceLog(post=chain, inline=True).run_job(_job(tmp_db))
    assert chain.posts == [] and report["skipped"] == "limiter:chain cooldown"


def test_a_free_bucket_is_one_reservation_and_it_is_released(tmp_db, risk_file):
    _set_rh_budget(risk_file)
    chain, _ = _synthetic_book(tmp_db)
    op.OnchainPriceLog(post=chain, inline=True).run_job(_job(tmp_db))
    assert len(chain.posts) == 1
    st = fetch_one(tmp_db, "SELECT inflight FROM provider_state WHERE provider=?", (op.PROVIDER,))
    assert st["inflight"] == 0
    calls = fetch_all(tmp_db, "SELECT endpoint, status FROM provider_calls WHERE provider=?", (op.PROVIDER,))
    assert calls == [{"endpoint": op.ENDPOINT, "status": "ok"}]


def test_a_raising_transport_is_counted_released_and_never_raised(tmp_db, risk_file):
    _set_rh_budget(risk_file)
    chain, _ = _synthetic_book(tmp_db)
    chain.fail = ConnectionError("rpc down")
    logger = op.OnchainPriceLog(post=chain, inline=True)
    report = logger.run_job(_job(tmp_db))
    assert report["skipped"] == "rpc_error:ConnectionError"
    # Counted where an operator will see it: the status event (the first one is written at
    # once, so a freshly enabled reader shows up on the bus without a ten-minute wait).
    status = [e for e in events_named(tmp_db, "status") if e.get("service") == "onchain_price_log"]
    assert status and status[0]["errors"] == 1 and status[0]["calls"] == 0
    st = fetch_one(tmp_db, "SELECT inflight FROM provider_state WHERE provider=?", (op.PROVIDER,))
    assert st["inflight"] == 0, "the slot comes back even when the call dies"


def test_a_json_rpc_rate_limit_opens_the_cooldown(tmp_db, risk_file):
    _set_rh_budget(risk_file)
    _synthetic_book(tmp_db)

    def limited(payload, timeout_s):  # noqa: ANN001
        return {"jsonrpc": "2.0", "id": 1, "error": {"code": 429, "message": "Too Many Requests"}}

    op.OnchainPriceLog(post=limited, inline=True).run_job(_job(tmp_db))
    ban = fetch_one(tmp_db, "SELECT banned_until_ms FROM provider_family_bans WHERE provider=? AND family='chain'",
                    (op.PROVIDER,))
    assert ban and ban["banned_until_ms"] > now_ms()


# --------------------------------------------------------------------------------------
# the tick contract
# --------------------------------------------------------------------------------------


def test_flag_off_is_inert(monkeypatch):
    def boom(*a, **k):  # noqa: ANN002, ANN003
        raise AssertionError("the logger was reached with the flag off")

    monkeypatch.setattr(op, "_singleton", boom)
    before = {t.name for t in threading.enumerate()}
    assert op.after_tick(object(), [object()], ProtectionConfig()) is False
    assert ProtectionConfig().onchain_price_log is False, "the shipped default is off"
    assert {t.name for t in threading.enumerate()} == before


def test_a_watchdog_tick_with_the_flag_off_never_reaches_the_logger(tmp_db, risk_file, monkeypatch):
    _rh_position(tmp_db, "pos_rh", TOKEN_T)
    reached: list = []
    monkeypatch.setattr(op.OnchainPriceLog, "on_tick", lambda self, *a: reached.append(a) or True)
    dog = wd.Watchdog(tmp_db, price_source=FakeSource({TOKEN_T: "1.0"}), submitter=RecordingSubmitter(),
                      cfg_provider=lambda: ProtectionConfig())
    dog.tick()
    assert reached == []


def test_the_watchdog_hands_over_its_book_after_deciding(tmp_db, risk_file, monkeypatch):
    _rh_position(tmp_db, "pos_rh", TOKEN_T, entry="1.0")
    seen: list = []

    class Stub:
        def on_tick(self, watchdog, positions, cfg):  # noqa: ANN001
            # Every stop has already been decided when the hook runs.
            seen.append((sorted(p.position_id for p in positions), list(submitter.calls)))
            return True

    monkeypatch.setattr(op, "_singleton", lambda: Stub())
    submitter = RecordingSubmitter()
    dog = wd.Watchdog(tmp_db, price_source=FakeSource({TOKEN_T: "0.6"}), submitter=submitter,
                      cfg_provider=lambda: _cfg())
    dog.tick()
    assert seen and seen[0][0] == ["pos_rh"]
    assert seen[0][1] and seen[0][1][0][0] == "pos_rh", "the stop fired before the hook ran"


def test_a_raising_reader_never_breaks_the_tick_or_the_stop(tmp_db, risk_file, monkeypatch):
    _rh_position(tmp_db, "pos_rh", TOKEN_T, entry="1.0")

    def boom(*a, **k):  # noqa: ANN002, ANN003
        raise RuntimeError("reader on fire")

    monkeypatch.setattr(op, "after_tick", boom)
    submitter = RecordingSubmitter()
    dog = wd.Watchdog(tmp_db, price_source=FakeSource({TOKEN_T: "0.6"}), submitter=submitter,
                      cfg_provider=lambda: _cfg())
    report = dog.tick()
    assert report.checked == 1 and report.errors == 0
    assert submitter.calls and submitter.calls[0][2] == "stop_loss"
    assert events_named(tmp_db, "heartbeat"), "the tick finished and said so"


def test_on_tick_reads_only_live_robinhood_inventory(tmp_db):
    from kaiba.execution.paper import open_positions

    _rh_position(tmp_db, "pos_live", TOKEN_T)
    _rh_position(tmp_db, "pos_shadow", "0x" + "e5" * 20, mode=LaneMode.SHADOW)
    _rh_position(tmp_db, "pos_empty", "0x" + "f6" * 20, qty=0)
    make_position(tmp_db, position_id="pos_sol", mode=LaneMode.LIVE)
    jobs: list[op.Job] = []
    logger = op.OnchainPriceLog(inline=True)
    logger.run_job = lambda job: jobs.append(job) or {}  # type: ignore[method-assign]
    assert logger.on_tick(type("Dog", (), {"_quote_cache": {}})(), open_positions(tmp_db), _cfg())
    assert [t.position_id for t in jobs[0].targets] == ["pos_live"]


def test_the_cadence_caps_reads(tmp_db):
    from kaiba.execution.paper import open_positions

    _rh_position(tmp_db, "pos_live", TOKEN_T)
    clock = [1_000_000]
    logger = op.OnchainPriceLog(inline=True, clock_ms=lambda: clock[0])
    logger.run_job = lambda job: {}  # type: ignore[method-assign]
    dog, cfg = type("Dog", (), {"_quote_cache": {}})(), _cfg(onchain_price_log_min_interval_s=10)
    positions = open_positions(tmp_db)
    assert logger.on_tick(dog, positions, cfg) is True
    clock[0] += 9_999
    assert logger.on_tick(dog, positions, cfg) is False and logger.stats.skips["cadence"] == 1
    clock[0] += 1
    assert logger.on_tick(dog, positions, cfg) is True


def test_the_tick_does_not_wait_for_the_chain(tmp_db):
    """Worker mode: a 0.5 s RPC costs the tick nothing, and a busy worker means a skip."""
    from kaiba.execution.paper import open_positions

    _rh_position(tmp_db, "pos_live", TOKEN_T)
    release = threading.Event()
    started = threading.Event()
    logger = op.OnchainPriceLog()

    def slow(job):  # noqa: ANN001
        started.set()
        release.wait(5)
        return {}

    logger.run_job = slow  # type: ignore[method-assign]
    dog, cfg, positions = type("Dog", (), {"_quote_cache": {}})(), _cfg(), open_positions(tmp_db)
    t0 = time.perf_counter()
    assert logger.on_tick(dog, positions, cfg) is True
    assert (time.perf_counter() - t0) < 0.05
    assert started.wait(2)
    assert logger.on_tick(dog, positions, cfg) is False and logger.stats.skips["worker_busy"] == 1
    release.set()


def test_retention_prunes_only_what_is_older_than_the_window(tmp_db):
    now = now_ms()
    for ts in (now - 15 * 86_400_000, now - 13 * 86_400_000, now):
        tmp_db.execute(
            "INSERT INTO onchain_price_samples (ts_ms, position_id, token, pool, pool_kind, price_quote, source) "
            "VALUES (?, 'p', 't', 'pool', 'v4', '1', 'onchain:v4')", (ts,))
    logger = op.OnchainPriceLog(inline=True)
    removed = logger._maybe_prune(tmp_db, op.Job(targets=[], created_ms=now, retention_days=14), now)
    assert removed == 1
    left = [r["ts_ms"] for r in fetch_all(tmp_db, "SELECT ts_ms FROM onchain_price_samples ORDER BY ts_ms")]
    assert left == [now - 13 * 86_400_000, now]
