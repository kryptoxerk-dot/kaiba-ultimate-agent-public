"""kaiba/ingest/launch_feed.py: Pons launches over the WebSocket, sol launches from the table."""

from __future__ import annotations

import asyncio
import json

import pytest

from kaiba.core.db import jdump
from kaiba.core.schemas import Chain
from kaiba.ingest import launch_feed as lf
from kaiba.ingest.robinhood import FACTORY_V2, TOPIC_TOKEN_LAUNCHED

TOKEN = "0x9122cbc7a76c8518989aae029148d331538dadb7"
CURVE = "0x5575479424e114fafa0cd314115c7897a7a2441b"
DEPLOYER = "0x1a4e077c1abd3fe3674516aeff53ab400e92add7"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"


def word(n: int) -> str:
    return format(n, "064x")


def addr_topic(a: str) -> str:
    return "0x" + a.removeprefix("0x").rjust(64, "0")


def launched_log(*, token=TOKEN, curve=CURVE, deployer=DEPLOYER, pair="0x" + "0" * 40, block=1000, ts=1_791_000_000,
                 tx="0x" + "ab" * 32, log_index=3, with_ts=True):
    entry = {
        "address": FACTORY_V2,
        "topics": [TOPIC_TOKEN_LAUNCHED, addr_topic(token), addr_topic(curve), addr_topic(deployer)],
        "data": "0x" + pair.removeprefix("0x").rjust(64, "0") + word(0) + word(4 * 10**18),
        "blockNumber": hex(block),
        "transactionHash": tx,
        "logIndex": hex(log_index),
    }
    if with_ts:
        entry["blockTimestamp"] = hex(ts)
    return entry


def test_a_pons_launch_log_reads_as_a_launch_with_its_block_time():
    got = lf.launch_from_log(launched_log(), received_ms=1_791_000_000_400)
    assert got is not None
    assert (got.chain, got.token, got.curve, got.creator, got.venue) == (Chain.ROBINHOOD, TOKEN, CURVE, DEPLOYER, "pons")
    assert got.quote_is_native and got.graduation_threshold == 4 * 10**18 and got.block == 1000
    assert got.launched_ms == 1_791_000_000_000 and got.latency_ms == 400


def test_a_token_paired_launch_keeps_its_pair_and_an_untimed_backfill_stays_untimed():
    got = lf.launch_from_log(launched_log(pair=USDG, with_ts=False), received_ms=5, backfilled=True)
    assert got.pair_token == USDG and not got.quote_is_native
    assert got.launched_ms is None and got.latency_ms is None and got.backfilled


def test_removed_or_foreign_logs_are_not_launches():
    assert lf.launch_from_log({**launched_log(), "removed": True}, received_ms=1) is None
    other = launched_log()
    other["topics"] = ["0x" + "11" * 32, *other["topics"][1:]]
    assert lf.launch_from_log(other, received_ms=1) is None


def test_the_subscription_is_the_factory_topic_and_nothing_else():
    # Cost guard: a wider filter (every curve, every Transfer) multiplies Alchemy compute.
    assert lf.PONS_FILTER == {"address": FACTORY_V2, "topics": [TOPIC_TOKEN_LAUNCHED]}


class FakeSocket:
    """Scripted Alchemy socket: answers calls, then pushes notifications, then drops."""

    def __init__(self, plan):
        self.plan = plan
        self.sent = []
        self.inbox = asyncio.Queue()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        if msg["method"] == "eth_subscribe":
            await self.inbox.put(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": "0xsub"}))
            for push in self.plan.get("pushes", []):
                await self.inbox.put(json.dumps({"jsonrpc": "2.0", "method": "eth_subscription",
                                                 "params": {"subscription": "0xsub", "result": push}}))
            if self.plan.get("drop"):
                await self.inbox.put(None)
        elif msg["method"] == "eth_blockNumber":
            await self.inbox.put(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": hex(self.plan["head"])}))
        elif msg["method"] == "eth_getLogs":
            await self.inbox.put(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": self.plan.get("backfill", [])}))

    async def recv(self):
        item = await self.inbox.get()
        if item is None:
            raise ConnectionError("socket dropped")
        return item


def test_stream_reconnects_backfills_the_gap_and_never_yields_a_launch_twice(monkeypatch):
    monkeypatch.setattr(lf.aws, "backoff_delay", lambda attempt, **k: 0.0)
    first = launched_log(block=1000, log_index=1, tx="0x" + "01" * 32)
    missed = launched_log(token="0x" + "22" * 20, curve="0x" + "33" * 20, block=1004, log_index=1, tx="0x" + "02" * 32)
    sockets = [
        FakeSocket({"pushes": [first], "drop": True}),
        # the second connection backfills from block 1000 (inclusive): `first` again (dedupe) and `missed`
        FakeSocket({"head": 1010, "backfill": [first, missed], "pushes": []}),
    ]
    dials = iter(sockets)
    stop = asyncio.Event()
    stats = lf.LaunchFeedStats()

    async def collect():
        out = []
        async for launch in lf.stream_pons("https://rh.example/v2/KEY", stop=stop, connect=lambda url: next(dials),
                                           stats=stats, poll_s=0.01, max_attempts=3):
            out.append(launch)
            if len(out) == 2:
                stop.set()
        return out

    got = asyncio.run(asyncio.wait_for(collect(), timeout=5))
    assert [g.token for g in got] == [TOKEN, "0x" + "22" * 20]
    assert got[1].backfilled and not got[0].backfilled
    assert stats.duplicates == 1 and stats.connects == 2 and stats.disconnects == 1
    gl = [m for m in sockets[1].sent if m["method"] == "eth_getLogs"][0]["params"][0]
    assert gl["fromBlock"] == hex(1000) and gl["toBlock"] == hex(1010)
    assert gl["address"] == FACTORY_V2 and gl["topics"] == [TOPIC_TOKEN_LAUNCHED]
    assert sockets[0].sent[0]["params"] == ["logs", lf.PONS_FILTER]


def test_sol_tail_reads_pumpportal_rows_in_order_without_skipping_a_shared_millisecond(tmp_db):
    rows = [
        ("Mint1", 1000, "pump", "Creator1"),
        ("Mint2", 1000, "bonk", "Creator2"),  # same millisecond as Mint1
        ("Mint3", 1001, "pump", "Creator3"),
    ]
    for addr, seen, pool, creator in rows:
        tmp_db.execute("INSERT INTO tokens (chain, address, symbol, name, creator, created_ms, launchpad, first_seen_ms, meta_json) "
                       "VALUES ('sol', ?, 'S', 'N', ?, ?, 'pump.fun', ?, ?)",
                       (addr, creator, seen - 300, seen, jdump({"pool": pool, "signature": "sig" + addr})))
    tmp_db.execute("INSERT INTO tokens (chain, address, launchpad, first_seen_ms) VALUES ('robinhood', '0xaa', 'pons', 1000)")
    mark = lf.SolWatermark(first_seen_ms=999)
    first = lf.tail_sol(tmp_db, mark, limit=2, now_ms=2000)
    second = lf.tail_sol(tmp_db, mark, limit=2, now_ms=2000)
    assert [x.token for x in first] == ["Mint1", "Mint2"] and [x.token for x in second] == ["Mint3"]
    assert [x.venue for x in first] == ["pump.fun", "launchlab"]
    assert first[0].creator == "Creator1" and first[0].launched_ms == 700 and first[0].latency_ms == 1300
    assert lf.tail_sol(tmp_db, mark, now_ms=2000) == []


@pytest.mark.parametrize("pool,venue", [("pump", "pump.fun"), ("bonk", "launchlab")])
def test_sol_pool_maps_to_its_venue(pool, venue):
    assert lf.sol_launch_from_row({"address": "M", "meta_json": jdump({"pool": pool})}, received_ms=1).venue == venue


#: Live pump.fun creates, 2026-10-04: (mint, PumpPortal's bondingCurveKey, the curve account).
#: The last two are mayhem-mode creates whose frame named an empty system account.
PUMP_CREATES = [
    ("GpNKQHyzF5A81TPmaDmFz6Fc949G4SBfPo4C42ynpump", "7vqRFPR67qqPPtNmKsw4sjB4LWpLp77gvkDpxFFGMsDM",
     "7vqRFPR67qqPPtNmKsw4sjB4LWpLp77gvkDpxFFGMsDM"),
    ("3fWpRpuqV5kwsrytF4PQ2Sy42cTmw1SrvjsXMHRwpump", "E6hdByPVNKuJXtGF4iDAgSjcPWKQnEtaFrLPBFVEYS4G",
     "E6hdByPVNKuJXtGF4iDAgSjcPWKQnEtaFrLPBFVEYS4G"),
    ("58hi4YJpuuD7HRmEMPUWyhYa514VqWyWsCThbK3D5LiH", "5BGmDrfPb8QDdcytWSxg7Cm5WMExZspZL7t81QucUQFu",
     "5BGmDrfPb8QDdcytWSxg7Cm5WMExZspZL7t81QucUQFu"),
    ("J8qxPSHY3B5B1bGxPMqG2HaWQ3uzJcHSDgy9EfVXpump", "BwWK17cbHxwWBKZkUYvzxLcNQ1YVyaFezduWbtm2de6s",
     "7vk2QivZqv2HLoUP7qDPGpVbrp5xwYMT5WpxEYFEG3Ra"),
    ("6kgG1ZBeyQYEVYbuLKzQ3WnDvi3gZhCnnJiL6ueDpump", "BwWK17cbHxwWBKZkUYvzxLcNQ1YVyaFezduWbtm2de6s",
     "JnZtRqmbWcK2HVWBMzUgGbN86y4rGYo63Ddq8VETxB1"),
]


@pytest.mark.parametrize("mint,frame_key,curve", PUMP_CREATES)
def test_the_pump_curve_is_derived_from_the_mint_not_taken_from_the_frame(mint, frame_key, curve):
    assert lf.pump_curve_address(mint) == curve
    launch = lf.sol_launch_from_row({"address": mint, "meta_json": jdump({"pool": "pump", "bonding_curve": frame_key})},
                                    received_ms=1)
    assert launch.meta["bonding_curve"] == curve and launch.meta["bonding_curve_frame"] == frame_key


def test_a_launchlab_pool_keeps_its_own_key_and_a_bad_mint_derives_nothing():
    launch = lf.sol_launch_from_row({"address": "Mint1111", "meta_json": jdump({"pool": "bonk", "bonding_curve": "PoolKey"})},
                                    received_ms=1)
    assert launch.meta["bonding_curve"] == "PoolKey"
    assert lf.pump_curve_address("not-base58-0OIl") is None and lf.pump_curve_address("1111") is None
