"""kaiba/ingest/alchemy_ws.py: subscription builder, Transfer decoder, tx fold, reconnect.

Every address and hash here is synthetic. The decoder fixtures are the exact shape the
Robinhood Alchemy endpoint pushed on 2026-10-03 (keys: address, blockHash, blockNumber,
blockTimestamp, data, logIndex, removed, topics, transactionHash, transactionIndex).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from kaiba.ingest import alchemy_ws as aw

WALLET_A = "0x" + "a1" * 20
WALLET_B = "0x" + "b2" * 20
POOL = "0x" + "c3" * 20
STRANGER = "0x" + "d4" * 20
TOKEN = "0x" + "e5" * 20
WETH = "0x" + "f6" * 20
TRACKED = frozenset({WALLET_A, WALLET_B})
BLOCK_TS = 1_791_018_311  # seconds; Nitro stamps whole seconds
RECV_MS = BLOCK_TS * 1000 + 734


def word(n: int) -> str:
    return "0x" + format(n, "064x")


def topic(addr: str) -> str:
    return "0x" + "0" * 24 + addr[2:]


def transfer_log(
    *,
    sender: str,
    receiver: str,
    amount: int,
    token: str = TOKEN,
    tx: str = "0x" + "11" * 32,
    block: int = 78_964_433,
    log_index: int = 3,
    ts: int | None = BLOCK_TS,
    removed: bool = False,
    extra_topic: str | None = None,
) -> dict[str, Any]:
    topics = [aw.TRANSFER_TOPIC, topic(sender), topic(receiver)]
    if extra_topic is not None:
        topics.append(extra_topic)
    entry: dict[str, Any] = {
        "address": token,
        "topics": topics,
        "data": word(amount),
        "blockNumber": hex(block),
        "transactionHash": tx,
        "transactionIndex": "0x1",
        "blockHash": "0x" + "22" * 32,
        "logIndex": hex(log_index),
        "removed": removed,
    }
    if ts is not None:
        entry["blockTimestamp"] = hex(ts)
    return entry


def decode(entry: dict[str, Any], **kw: Any) -> list[aw.WalletTransfer]:
    kw.setdefault("wallets", TRACKED)
    kw.setdefault("recv_ms", RECV_MS)
    return aw.decode_transfer(entry, **kw)


# --------------------------------------------------------------------------------------
# secrets and urls
# --------------------------------------------------------------------------------------


def test_mask_url_hides_everything_after_v2() -> None:
    url = "https://robinhood-mainnet.g.alchemy.com/v2/SeCrEtKeY_123-abc"
    masked = aw.mask_url(url)
    assert masked == "https://robinhood-mainnet.g.alchemy.com/v2/***"
    assert "SeCrEtKeY" not in masked
    assert aw.mask_url("https://rpc.example.org/some/path?apikey=zzz") == "https://rpc.example.org/***"


def test_redact_removes_the_key_wherever_it_appears() -> None:
    url = "wss://robinhood-mainnet.g.alchemy.com/v2/SeCrEtKeY_123"
    text = f"InvalidStatus: rejected {url} (key SeCrEtKeY_123)"
    out = aw.redact(text, url)
    assert "SeCrEtKeY_123" not in out
    assert "/v2/***" in out
    # without the url, any /v2/<segment> is still masked
    bare = aw.redact("dial wss://other.example/v2/AnotherKey9 failed: 'https://x.example/v2/K2?y=1'")
    assert "AnotherKey9" not in bare and "K2" not in bare
    assert bare.count("/v2/***") == 2


def test_ws_url_converts_scheme_and_refuses_others_without_leaking() -> None:
    assert aw.ws_url("https://h.example/v2/k") == "wss://h.example/v2/k"
    assert aw.ws_url("http://h.example/v2/k") == "ws://h.example/v2/k"
    assert aw.ws_url("wss://h.example/v2/k") == "wss://h.example/v2/k"
    with pytest.raises(ValueError) as err:
        aw.ws_url("ftp://h.example/v2/TOPSECRET")
    assert "TOPSECRET" not in str(err.value)


# --------------------------------------------------------------------------------------
# topics and subscriptions
# --------------------------------------------------------------------------------------


def test_address_topic_round_trip_and_refuses_dirty_high_bytes() -> None:
    t = aw.address_topic(WALLET_A.upper().replace("0X", "0x"))
    assert t == topic(WALLET_A)
    assert aw.topic_address(t) == WALLET_A
    dirty = "0x" + "1" + "0" * 23 + WALLET_A[2:]
    assert aw.topic_address(dirty) is None
    with pytest.raises(ValueError):
        aw.address_topic("0x1234")


def test_build_subscriptions_chunks_dedupes_and_places_the_wallet_topic() -> None:
    wallets = [WALLET_A, WALLET_A.upper().replace("0X", "0x"), WALLET_B, POOL, "not-an-address", STRANGER, TOKEN]
    subs = aw.build_subscriptions(wallets, chunk_size=2)
    # 5 distinct valid addresses -> 3 chunks x 2 directions
    assert [s.key for s in subs] == ["in:0", "out:0", "in:1", "out:1", "in:2", "out:2"]
    assert sorted({w for s in subs for w in s.wallets}) == sorted({WALLET_A, WALLET_B, POOL, STRANGER, TOKEN})

    inbound = subs[0].subscribe_params()
    assert inbound[0] == "logs"
    t_in = inbound[1]["topics"]
    assert t_in[0] == aw.TRANSFER_TOPIC
    assert t_in[1] is None  # any sender
    assert t_in[2] == [topic(w) for w in subs[0].wallets]  # tracked wallet is the RECEIVER

    t_out = subs[1].subscribe_params()[1]["topics"]
    assert t_out == [aw.TRANSFER_TOPIC, [topic(w) for w in subs[1].wallets]]  # tracked is the SENDER


def test_logs_filter_is_the_same_filter_over_a_block_range() -> None:
    sub = aw.build_subscriptions([WALLET_A])[0]
    f = sub.logs_filter(100, 112)
    assert f == {"fromBlock": "0x64", "toBlock": "0x70", "topics": sub.topics()}


def test_build_subscriptions_refuses_bad_arguments() -> None:
    with pytest.raises(ValueError):
        aw.build_subscriptions([WALLET_A], chunk_size=0)
    with pytest.raises(ValueError):
        aw.build_subscriptions([WALLET_A], directions=("sideways",))


# --------------------------------------------------------------------------------------
# decoder
# --------------------------------------------------------------------------------------


def test_tokens_arriving_at_a_tracked_wallet_decode_as_a_buy() -> None:
    [rec] = decode(transfer_log(sender=POOL, receiver=WALLET_A, amount=12_345))
    assert rec.side == "buy"
    assert rec.wallet == WALLET_A
    assert rec.counterparty == POOL
    assert rec.token == TOKEN
    assert rec.amount_atoms == 12_345
    assert rec.kind == aw.KIND_TRANSFER
    assert rec.block_number == 78_964_433
    assert rec.log_index == 3
    assert rec.block_ts_ms == BLOCK_TS * 1000
    assert rec.recv_ms == RECV_MS
    assert rec.latency_ms == 734


def test_tokens_leaving_a_tracked_wallet_decode_as_a_sell() -> None:
    [rec] = decode(transfer_log(sender=WALLET_B, receiver=POOL, amount=777))
    assert rec.side == "sell"
    assert rec.wallet == WALLET_B
    assert rec.counterparty == POOL
    assert rec.token == TOKEN
    assert rec.amount_atoms == 777


def test_untracked_ends_decode_to_nothing() -> None:
    assert decode(transfer_log(sender=POOL, receiver=STRANGER, amount=5)) == []


def test_wallet_matching_is_exact_not_prefix_or_case_blind_to_the_set() -> None:
    near = WALLET_A[:-1] + "0"  # differs in the last nibble only
    assert decode(transfer_log(sender=POOL, receiver=near, amount=5)) == []


def test_transfer_between_two_tracked_wallets_yields_both_sides() -> None:
    recs = decode(transfer_log(sender=WALLET_A, receiver=WALLET_B, amount=9))
    by_side = {r.side: r for r in recs}
    assert set(by_side) == {"buy", "sell"}
    assert by_side["sell"].wallet == WALLET_A and by_side["sell"].counterparty == WALLET_B
    assert by_side["buy"].wallet == WALLET_B and by_side["buy"].counterparty == WALLET_A


def test_zero_amount_transfer_is_dropped_as_poisoning_spam() -> None:
    assert decode(transfer_log(sender=WALLET_A, receiver=STRANGER, amount=0)) == []


def test_erc721_shape_and_foreign_topics_are_not_decoded() -> None:
    nft = transfer_log(sender=POOL, receiver=WALLET_A, amount=1, extra_topic=word(42))
    nft["data"] = "0x"
    assert decode(nft) == []
    # Four topics is not the ERC-20 shape even when a data word happens to be present.
    four = transfer_log(sender=POOL, receiver=WALLET_A, amount=1, extra_topic=word(42))
    assert decode(four) == []
    other = transfer_log(sender=POOL, receiver=WALLET_A, amount=1)
    other["topics"][0] = "0x" + "99" * 32
    assert decode(other) == []


def test_malformed_entries_never_raise() -> None:
    for bad in (None, "x", 7, {}, {"topics": "nope"}, {"topics": [aw.TRANSFER_TOPIC]},
                {**transfer_log(sender=POOL, receiver=WALLET_A, amount=1), "data": "0xzz"},
                {**transfer_log(sender=POOL, receiver=WALLET_A, amount=1), "data": "0x01"},
                {**transfer_log(sender=POOL, receiver=WALLET_A, amount=1), "address": "0x12"},
                {**transfer_log(sender=POOL, receiver=WALLET_A, amount=1), "blockNumber": None}):
        assert decode(bad) == []


def test_amount_keeps_full_uint256_precision() -> None:
    big = 2**255 + 12_345_678_901_234_567_890
    [rec] = decode(transfer_log(sender=POOL, receiver=WALLET_A, amount=big))
    assert rec.amount_atoms == big
    assert rec.to_dict()["amount_atoms"] == str(big)
    json.dumps(rec.to_dict())  # serialisable


def test_mint_burn_and_quote_legs_are_labelled() -> None:
    [mint] = decode(transfer_log(sender=aw.ZERO_ADDRESS, receiver=WALLET_A, amount=5))
    assert (mint.side, mint.kind) == ("buy", aw.KIND_MINT)
    [burn] = decode(transfer_log(sender=WALLET_A, receiver=aw.ZERO_ADDRESS, amount=5))
    assert (burn.side, burn.kind) == ("sell", aw.KIND_BURN)
    [quote] = decode(transfer_log(sender=WALLET_A, receiver=POOL, amount=5, token=WETH),
                     quote_assets=frozenset({WETH}))
    assert (quote.side, quote.kind, quote.token) == ("sell", aw.KIND_QUOTE, WETH)
    # a quote asset minted to the wallet is a wrap, and stays a QUOTE leg (not a mint)
    [wrap] = decode(transfer_log(sender=aw.ZERO_ADDRESS, receiver=WALLET_A, amount=5, token=WETH),
                    quote_assets=frozenset({WETH}))
    assert (wrap.side, wrap.kind) == ("buy", aw.KIND_QUOTE)


def test_zero_block_timestamp_is_absent_not_1970() -> None:
    [rec] = decode(transfer_log(sender=POOL, receiver=WALLET_A, amount=5, ts=0))
    assert rec.block_ts_ms is None and rec.latency_ms is None
    [rec2] = decode(transfer_log(sender=POOL, receiver=WALLET_A, amount=5, ts=None))
    assert rec2.block_ts_ms is None and rec2.latency_ms is None


def test_tx_hash_is_lowercased_and_removed_flag_kept() -> None:
    [rec] = decode(transfer_log(sender=POOL, receiver=WALLET_A, amount=5, tx="0x" + "AB" * 32, removed=True))
    assert rec.tx == "0x" + "ab" * 32
    assert rec.removed is True


# --------------------------------------------------------------------------------------
# fold_tx
# --------------------------------------------------------------------------------------


def legs(*entries: dict[str, Any], quote: frozenset[str] = frozenset({WETH})) -> list[aw.WalletTransfer]:
    out: list[aw.WalletTransfer] = []
    for e in entries:
        out.extend(aw.decode_transfer(e, wallets=TRACKED, recv_ms=RECV_MS, quote_assets=quote))
    return out


def test_buy_paid_in_a_quote_asset_is_corroborated() -> None:
    recs = legs(
        transfer_log(sender=WALLET_A, receiver=POOL, amount=50, token=WETH, log_index=1),
        transfer_log(sender=POOL, receiver=WALLET_A, amount=1_000, log_index=2),
    )
    [trade] = aw.fold_tx(recs)
    assert (trade.side, trade.token, trade.net_atoms) == ("buy", TOKEN, 1_000)
    assert trade.basis == aw.BASIS_PAID_QUOTE
    assert (trade.quote_token, trade.quote_atoms) == (WETH, 50)
    assert trade.copyable


def test_sell_for_a_quote_asset_is_corroborated() -> None:
    recs = legs(
        transfer_log(sender=WALLET_A, receiver=POOL, amount=1_000, log_index=1),
        transfer_log(sender=POOL, receiver=WALLET_A, amount=40, token=WETH, log_index=2),
    )
    [trade] = aw.fold_tx(recs)
    assert (trade.side, trade.basis, trade.quote_atoms) == ("sell", aw.BASIS_RECEIVED_QUOTE, 40)
    assert not trade.copyable


def test_wrap_then_pay_reads_as_paid_and_sell_then_unwrap_as_received() -> None:
    # MEASURED shape on Robinhood: WETH minted to the wallet, WETH sent to the pool, token back.
    buy = legs(
        transfer_log(sender=aw.ZERO_ADDRESS, receiver=WALLET_A, amount=50, token=WETH, log_index=1),
        transfer_log(sender=WALLET_A, receiver=POOL, amount=50, token=WETH, log_index=2),
        transfer_log(sender=POOL, receiver=WALLET_A, amount=1_000, log_index=3),
    )
    [trade] = aw.fold_tx(buy)
    assert (trade.side, trade.token, trade.basis, trade.quote_atoms) == ("buy", TOKEN, aw.BASIS_PAID_QUOTE, 50)
    sell = legs(
        transfer_log(sender=WALLET_A, receiver=POOL, amount=1_000, log_index=1),
        transfer_log(sender=POOL, receiver=WALLET_A, amount=40, token=WETH, log_index=2),
        transfer_log(sender=WALLET_A, receiver=aw.ZERO_ADDRESS, amount=40, token=WETH, log_index=3),
    )
    [trade] = aw.fold_tx(sell)
    assert (trade.side, trade.basis, trade.quote_atoms) == ("sell", aw.BASIS_RECEIVED_QUOTE, 40)
    # a bare wrap is not a trade of anything
    assert aw.fold_tx(legs(transfer_log(sender=aw.ZERO_ADDRESS, receiver=WALLET_A, amount=5, token=WETH))) == []


def test_bare_transfer_in_is_not_copyable_until_the_venue_is_named() -> None:
    recs = legs(transfer_log(sender=POOL, receiver=WALLET_A, amount=1_000))
    [trade] = aw.fold_tx(recs)
    assert trade.basis == aw.BASIS_TRANSFER_ONLY and not trade.copyable
    [named] = aw.fold_tx(recs, venues=frozenset({POOL}))
    assert named.basis == aw.BASIS_VENUE and named.copyable


def test_routing_through_the_wallet_nets_to_nothing() -> None:
    recs = legs(
        transfer_log(sender=POOL, receiver=WALLET_A, amount=1_000, log_index=1),
        transfer_log(sender=WALLET_A, receiver=STRANGER, amount=1_000, log_index=2),
    )
    assert aw.fold_tx(recs) == []


def test_mint_is_never_a_copyable_buy_and_removed_legs_are_ignored() -> None:
    [trade] = aw.fold_tx(legs(transfer_log(sender=aw.ZERO_ADDRESS, receiver=WALLET_A, amount=5)),
                         venues=frozenset({aw.ZERO_ADDRESS}))
    assert trade.basis == aw.BASIS_MINT and not trade.copyable
    assert aw.fold_tx(legs(transfer_log(sender=POOL, receiver=WALLET_A, amount=5, removed=True))) == []


# --------------------------------------------------------------------------------------
# backoff
# --------------------------------------------------------------------------------------


def test_backoff_grows_and_caps() -> None:
    zero = lambda: 0.0  # noqa: E731
    assert [aw.backoff_delay(a, rand=zero) for a in range(8)] == [1, 2, 4, 8, 16, 32, 60, 60]
    assert aw.backoff_delay(10, rand=lambda: 1.0) == 60.0


# --------------------------------------------------------------------------------------
# stream: a scripted endpoint
# --------------------------------------------------------------------------------------


class FakeWS:
    """Answers eth_subscribe / eth_blockNumber / eth_getLogs and plays scripted pushes."""

    def __init__(self, server: FakeServer, conn_no: int) -> None:
        self.server = server
        self.conn_no = conn_no
        self.q: asyncio.Queue[Any] = asyncio.Queue()
        self.sub_ids: dict[str, str] = {}
        self.subscribes = 0

    async def __aenter__(self) -> FakeWS:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def send(self, text: str) -> None:
        req = json.loads(text)
        self.server.requests.append((self.conn_no, req))
        script = self.server.scripts[self.conn_no]
        method, rid = req["method"], req["id"]
        if method == "eth_subscribe":
            topics = req["params"][1]["topics"]
            direction = "in" if topics[1] is None else "out"
            self.subscribes += 1
            if script.get("reject_subscribe"):
                self.q.put_nowait(json.dumps({"jsonrpc": "2.0", "id": rid,
                                              "error": {"code": -32000, "message": "nope"}}))
                return
            sid = f"0x{self.conn_no}{self.subscribes:03d}"
            self.sub_ids[direction] = sid
            # a push that races the remaining subscribe acks must be queued, not dropped
            for d, entry in script.get("early", []):
                if d == direction:
                    self.q.put_nowait(self.push(d, entry))
            self.q.put_nowait(json.dumps({"jsonrpc": "2.0", "id": rid, "result": sid}))
            if self.subscribes == script.get("expect_subs", 2):
                for item in script.get("pushes", []):
                    self.q.put_nowait(item if isinstance(item, BaseException) else self.push(*item))
        elif method == "eth_blockNumber":
            self.q.put_nowait(json.dumps({"jsonrpc": "2.0", "id": rid, "result": hex(script["head"])}))
        elif method == "eth_getLogs":
            flt = req["params"][0]
            self.server.get_logs.append(flt)
            direction = "in" if flt["topics"][1] is None else "out"
            logs = script.get("backfill", {}).get(direction, [])
            self.q.put_nowait(json.dumps({"jsonrpc": "2.0", "id": rid, "result": logs}))

    def push(self, direction: str, entry: dict[str, Any]) -> str:
        return json.dumps({"jsonrpc": "2.0", "method": "eth_subscription",
                           "params": {"subscription": self.sub_ids[direction], "result": entry}})

    async def recv(self) -> Any:
        item = await self.q.get()
        if isinstance(item, BaseException):
            raise item
        return item


class FakeServer:
    def __init__(self, scripts: list[dict[str, Any]]) -> None:
        self.scripts = scripts
        self.requests: list[tuple[int, dict[str, Any]]] = []
        self.get_logs: list[dict[str, Any]] = []
        self.dials = 0
        self.urls: list[str] = []

    def connect(self, url: str) -> FakeWS:
        self.urls.append(url)
        n = self.dials
        self.dials += 1
        if n >= len(self.scripts):
            raise ConnectionRefusedError(f"no script for dial {n} to {url}")
        if self.scripts[n].get("refuse"):
            raise ConnectionRefusedError(f"refused {url}")
        return FakeWS(self, n)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []

    async def fake_wait(stop: asyncio.Event, seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(aw, "_wait", fake_wait)
    monkeypatch.setattr(aw, "_rand", lambda: 0.0)
    return slept


async def collect(server: FakeServer, n: int, **kw: Any) -> tuple[list[aw.WalletTransfer], list[dict[str, Any]], aw.FeedStats]:
    stop = asyncio.Event()
    statuses: list[dict[str, Any]] = []
    stats = aw.FeedStats()
    out: list[aw.WalletTransfer] = []
    kw.setdefault("max_attempts", 3)

    async def run() -> None:
        async for rec in aw.stream("https://node.example/v2/SECRETKEY987", [WALLET_A, WALLET_B],
                                   stop=stop, connect=server.connect, stats=stats,
                                   on_status=statuses.append, poll_s=0.01, **kw):
            out.append(rec)
            if len(out) >= n:
                stop.set()

    await asyncio.wait_for(run(), timeout=5)
    return out, statuses, stats


def test_stream_yields_decoded_pushes_and_counts_bytes() -> None:
    buy = transfer_log(sender=POOL, receiver=WALLET_A, amount=10, block=200, log_index=1)
    sell = transfer_log(sender=WALLET_B, receiver=POOL, amount=20, block=201, log_index=2)
    noise = transfer_log(sender=POOL, receiver=STRANGER, amount=30, block=201, log_index=3)
    server = FakeServer([{"pushes": [("in", noise), ("in", buy), ("out", sell)]}])
    out, statuses, stats = asyncio.run(collect(server, 2))
    assert [(r.side, r.wallet, r.amount_atoms) for r in out] == [("buy", WALLET_A, 10), ("sell", WALLET_B, 20)]
    assert [r.subscription for r in out] == ["in:0", "out:0"]
    assert server.urls == ["wss://node.example/v2/SECRETKEY987"]
    assert stats.subscribe_calls == 2 and stats.connects == 1
    assert stats.notifications == 3 and stats.records == 2
    assert stats.notification_bytes > 0
    assert stats.estimated_cu() == pytest.approx(2 * 10 + stats.notification_bytes * 0.04)
    assert statuses[0]["event"] == "subscribed"


def test_pushes_racing_the_subscribe_acks_are_not_lost() -> None:
    early = transfer_log(sender=POOL, receiver=WALLET_A, amount=11, block=300)
    server = FakeServer([{"early": [("in", early)]}])
    out, _, _ = asyncio.run(collect(server, 1))
    assert [(r.side, r.amount_atoms) for r in out] == [("buy", 11)]


def test_drop_reconnects_resubscribes_and_backfills_the_gap_without_duplicates() -> None:
    live = transfer_log(sender=POOL, receiver=WALLET_A, amount=1, block=100, log_index=0, tx="0x" + "01" * 32)
    missed = transfer_log(sender=POOL, receiver=WALLET_A, amount=2, block=105, log_index=0, tx="0x" + "02" * 32)
    after = transfer_log(sender=POOL, receiver=WALLET_A, amount=3, block=113, log_index=0, tx="0x" + "03" * 32)
    server = FakeServer([
        {"pushes": [("in", live), ConnectionResetError("peer reset")]},
        {"head": 112, "backfill": {"in": [live, missed]}, "pushes": [("in", after)]},
    ])
    out, statuses, stats = asyncio.run(collect(server, 3))
    assert [(r.amount_atoms, r.backfilled) for r in out] == [(1, False), (2, True), (3, False)]
    assert stats.duplicates == 1  # block 100 came back in the backfill and was not re-yielded
    assert stats.connects == 2 and stats.disconnects == 1
    # backfill starts AT the last block seen (inclusive) and runs to the head, both directions
    assert [(f["fromBlock"], f["toBlock"]) for f in server.get_logs] == [("0x64", "0x70")] * 2
    events = [s["event"] for s in statuses]
    assert events == ["subscribed", "disconnected", "subscribed", "backfilled"]
    # resubscribed on the new socket
    assert sum(1 for c, r in server.requests if c == 1 and r["method"] == "eth_subscribe") == 2


def test_backfill_is_truncated_to_its_cap_and_says_so() -> None:
    live = transfer_log(sender=POOL, receiver=WALLET_A, amount=1, block=100)
    after = transfer_log(sender=POOL, receiver=WALLET_A, amount=3, block=10_001, tx="0x" + "03" * 32)
    server = FakeServer([
        {"pushes": [("in", live), ConnectionResetError("x")]},
        {"head": 10_000, "pushes": [("in", after)]},
    ])
    out, statuses, _ = asyncio.run(collect(server, 2, backfill_max_blocks=500))
    assert [r.amount_atoms for r in out] == [1, 3]
    trunc = [s for s in statuses if s["event"] == "gap_truncated"]
    assert trunc and trunc[0]["lost_blocks"] == 10_000 - 500 - 100
    assert server.get_logs[0]["fromBlock"] == hex(9_500)


def test_a_long_gap_is_backfilled_in_slices_that_tile_the_range() -> None:
    live = transfer_log(sender=POOL, receiver=WALLET_A, amount=1, block=100)
    after = transfer_log(sender=POOL, receiver=WALLET_A, amount=3, block=1_001, tx="0x" + "03" * 32)
    server = FakeServer([
        {"pushes": [("in", live), ConnectionResetError("x")]},
        {"head": 1_000, "pushes": [("in", after)]},
    ])
    out, statuses, stats = asyncio.run(collect(server, 2, backfill_chunk_blocks=400))
    assert [r.amount_atoms for r in out] == [1, 3]
    ranges = sorted({(int(f["fromBlock"], 16), int(f["toBlock"], 16)) for f in server.get_logs})
    assert ranges == [(100, 499), (500, 899), (900, 1_000)]  # contiguous, no gap, no overlap
    assert stats.get_logs_calls == 6  # 3 slices x 2 directions
    assert not [s for s in statuses if s["event"] == "gap_truncated"]


def test_rejected_subscribe_is_a_reconnect_and_dead_endpoint_gives_up(_no_sleep: list[float]) -> None:
    server = FakeServer([{"reject_subscribe": True}, {"refuse": True}, {"refuse": True}])
    out, statuses, stats = asyncio.run(collect(server, 1, max_attempts=3))
    assert out == []
    assert [s["event"] for s in statuses] == ["disconnected"] * 3 + ["gave_up"]
    assert _no_sleep == [1.0, 2.0, 4.0]
    assert stats.connects == 0 and stats.disconnects == 3
    blob = json.dumps(statuses)
    assert "SECRETKEY987" not in blob  # the dial error carried the url; the status must not
    assert "/v2/***" in blob


def test_stream_refuses_an_empty_wallet_list() -> None:
    async def go() -> None:
        async for _ in aw.stream("https://node.example/v2/k", ["junk"]):
            pass

    with pytest.raises(ValueError):
        asyncio.run(go())
