"""BSC Flap launches: the portal events, the WebSocket stream, the ingest writer, the table tail.

Every address here is synthetic (no owner or live wallet). The event layouts are the ones
MEASURED from the box on 2026-10-05 (docs in ``launch_feed`` / ``ingest/flap.py``).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from kaiba.core.db import fetch_all, fetch_one, jdump
from kaiba.core.schemas import Chain
from kaiba.execution import evm_price as ep
from kaiba.execution.policy import keccak256
from kaiba.ingest import feed_endpoints as fe
from kaiba.ingest import flap as flap_ingest
from kaiba.ingest import launch_feed as lf

TOKEN = "0x" + "ab" * 18 + "7777"
CREATOR = "0x" + "c0" * 20
POOL = "0x" + "9a" * 20
T0 = 1_791_000_000


def word(n: int) -> str:
    return format(n, "064x")


def addr_word(a: str) -> str:
    return a.removeprefix("0x").rjust(64, "0")


def abi_string(s: str) -> str:
    raw = s.encode()
    return word(len(raw)) + raw.hex().ljust(((len(raw) + 31) // 32) * 64, "0")


def created_log(*, token=TOKEN, creator=CREATOR, ts=T0, block=125_000_000, tx="0x" + "11" * 32, log_index=4,
                name="Faucet", symbol="FCT", meta="https://ipfs.io/ipfs/x", address=ep.FLAP_PORTAL) -> dict:
    """``TokenCreated(uint256 ts, address creator, uint256 nonce, address token, string, string, string)``,
    nothing indexed: 1 topic, the strings after three offset words (224/288/352 on the box)."""
    tail = [abi_string(name), abi_string(symbol), abi_string(meta)]
    head_bytes = 7 * 32
    offsets, at = [], head_bytes
    for part in tail:
        offsets.append(at)
        at += len(part) // 2
    data = "0x" + word(ts) + addr_word(creator) + word(7) + addr_word(token) + "".join(word(o) for o in offsets) + "".join(tail)
    return {"address": address, "topics": [lf.FLAP_TOPIC_TOKEN_CREATED], "data": data, "blockNumber": hex(block),
            "transactionHash": tx, "logIndex": hex(log_index)}


def graduated_log(*, token=TOKEN, pool=POOL, indexed=False, block=125_000_900, ts=T0 + 400) -> dict:
    topics = [lf.FLAP_TOPIC_LAUNCHED_TO_DEX] + (["0x" + addr_word(token)] if indexed else [])
    data_words = ([] if indexed else [addr_word(token)]) + [addr_word(pool), word(2 * 10**26), word(16 * 10**18)]
    return {"address": ep.FLAP_PORTAL, "topics": topics, "data": "0x" + "".join(data_words), "blockNumber": hex(block),
            "blockTimestamp": hex(ts), "transactionHash": "0x" + "22" * 32, "logIndex": "0x9"}


# ------------------------------------------------------------------------- constants

@pytest.mark.parametrize("const,signature,width", [
    (lf.FLAP_TOPIC_TOKEN_CREATED, "TokenCreated(uint256,address,uint256,address,string,string,string)", 66),
    (lf.FLAP_TOPIC_LAUNCHED_TO_DEX, "LaunchedToDEX(address,address,uint256,uint256)", 66),
    (ep.SEL_GET_TOKEN_V8_SAFE, "getTokenV8Safe(address)", 10),
    (ep.SEL_MAX_BUY_PER_ORIGIN, "maxBuyPerOrigin(address)", 10),
    (ep.SEL_QUOTE_EXACT_INPUT, "quoteExactInput((address,address,uint256))", 10),
])
def test_every_flap_topic_and_selector_is_the_keccak_of_its_signature(const, signature, width):
    assert const == "0x" + keccak256(signature.encode()).hex()[: width - 2]


def test_the_subscription_is_the_portal_and_two_topics_and_nothing_else():
    # Cost guard: ~970 launches an hour at ~54 CU each; a wider filter (every trade) is ~10x.
    assert lf.FLAP_FILTER == {"address": ep.FLAP_PORTAL,
                              "topics": [[lf.FLAP_TOPIC_TOKEN_CREATED, lf.FLAP_TOPIC_LAUNCHED_TO_DEX]]}


# ------------------------------------------------------------------------- parsing

def test_a_token_created_log_reads_as_a_bsc_flap_launch_timed_by_its_own_ts_word():
    got = lf.flap_launch_from_log(created_log(), received_ms=T0 * 1000 + 900)
    assert got is not None
    assert (got.chain, got.token, got.venue, got.creator) == (Chain.BSC, TOKEN, "flap", CREATOR)
    assert got.launched_ms == T0 * 1000 and got.latency_ms == 900 and got.block == 125_000_000
    assert (got.name, got.symbol) == ("Faucet", "FCT")
    assert got.meta["source"] == lf.FLAP_SOURCE and got.meta["nonce"] == 7 and got.meta["log_index"] == 4
    assert got.pair_token is None and got.curve is None  # the quote is read off the portal, not guessed


def test_foreign_removed_or_short_logs_are_not_flap_launches():
    assert lf.flap_launch_from_log({**created_log(), "removed": True}, received_ms=1) is None
    assert lf.flap_launch_from_log(created_log(address="0x" + "44" * 20), received_ms=1) is None
    other = created_log()
    other["topics"] = ["0x" + "11" * 32]
    assert lf.flap_launch_from_log(other, received_ms=1) is None
    assert lf.flap_launch_from_log({**created_log(), "data": "0x" + word(1)}, received_ms=1) is None


@pytest.mark.parametrize("indexed", [False, True])
def test_a_graduation_reads_its_token_and_pool_whether_or_not_the_token_is_indexed(indexed):
    got = lf.flap_graduation_from_log(graduated_log(indexed=indexed), received_ms=5)
    assert got is not None and got.token == TOKEN and got.pool == POOL and got.migrated_ms == (T0 + 400) * 1000
    assert lf.flap_event_from_log(graduated_log(indexed=indexed), received_ms=5) == got
    assert isinstance(lf.flap_event_from_log(created_log(), received_ms=5), lf.Launch)


# ------------------------------------------------------------------------- the stream

class FakeSocket:
    def __init__(self, plan):
        self.plan, self.sent, self.inbox = plan, [], asyncio.Queue()

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


def test_the_flap_stream_subscribes_once_backfills_with_the_same_filter_and_yields_both_events(monkeypatch):
    monkeypatch.setattr(lf.aws, "backoff_delay", lambda attempt, **k: 0.0)
    first = created_log(block=1000, tx="0x" + "01" * 32)
    missed = created_log(token="0x" + "cd" * 18 + "7777", block=1004, tx="0x" + "02" * 32)
    sockets = [FakeSocket({"pushes": [first], "drop": True}),
               FakeSocket({"head": 1010, "backfill": [first, missed, graduated_log(block=1005)], "pushes": []})]
    dials = iter(sockets)
    stop = asyncio.Event()

    async def collect():
        out = []
        async for item in lf.stream_flap("https://bnb.example/v2/KEY", stop=stop, connect=lambda url: next(dials),
                                         poll_s=0.01, max_attempts=3):
            out.append(item)
            if len(out) == 3:
                stop.set()
        return out

    got = asyncio.run(asyncio.wait_for(collect(), timeout=5))
    assert [type(g).__name__ for g in got] == ["Launch", "Launch", "FlapGraduation"]
    assert got[1].backfilled and got[2].token == TOKEN
    assert sockets[0].sent[0]["params"] == ["logs", lf.FLAP_FILTER]
    gl = [m for m in sockets[1].sent if m["method"] == "eth_getLogs"][0]["params"][0]
    assert gl["address"] == ep.FLAP_PORTAL and gl["topics"] == lf.FLAP_FILTER["topics"] and gl["fromBlock"] == hex(1000)


# ------------------------------------------------------------------------- the ingest writer

def test_the_listener_idles_unless_the_lane_lists_bsc_and_an_alchemy_endpoint_is_set(tmp_db):
    assert asyncio.run(flap_ingest.run(enabled=lambda: False, writer=tmp_db)) == {"idle": "launch-snipe params.chains has no bsc"}
    got = asyncio.run(flap_ingest.run(enabled=lambda: True, url=None, writer=tmp_db))
    assert got == {"idle": "no websocket endpoint"}  # the test settings carry no BSC_RPC_URL
    assert fetch_one(tmp_db, "SELECT COUNT(*) AS n FROM tokens")["n"] == 0


def test_the_listener_writes_a_launch_row_and_stamps_a_graduation(tmp_db, monkeypatch):
    monkeypatch.setattr(lf.aws, "backoff_delay", lambda attempt, **k: 0.0)
    sock = FakeSocket({"pushes": [created_log(), graduated_log()], "drop": True})
    dials = iter([sock])

    def dial(url):
        try:
            return next(dials)
        except StopIteration:
            raise ConnectionError("no more sockets") from None

    got = asyncio.run(asyncio.wait_for(flap_ingest.run(enabled=lambda: True, url="https://bnb.example/v2/KEY",
                                                       connect=dial, writer=tmp_db, max_attempts=2), timeout=10))
    assert got["launches"] == 1 and got["graduations"] == 1 and got["errors"] == 0
    row = fetch_one(tmp_db, "SELECT * FROM tokens WHERE chain='bsc' AND address=?", (TOKEN,))
    assert row["launchpad"] == "flap" and row["creator"] == CREATOR and row["created_ms"] == T0 * 1000
    assert row["migrated_ms"] == (T0 + 400) * 1000 and row["pool"] == POOL
    assert json.loads(row["meta_json"])["source"] == lf.FLAP_SOURCE
    # no event per launch (module doc): token.migrated would turn every graduation into a tier-1 scan
    assert fetch_all(tmp_db, "SELECT kind FROM events WHERE kind IN ('token.created','token.migrated')") == []


# ------------------------------------------------------------------------- the tail

def _flap_row(conn, address, created_ms, *, source=lf.FLAP_SOURCE, chain="bsc", launchpad="flap"):
    conn.execute("INSERT INTO tokens (chain, address, symbol, name, creator, created_ms, launchpad, first_seen_ms, meta_json) "
                 "VALUES (?,?,?,?,?,?,?,?,?)",
                 (chain, address, "S", "N", CREATOR, created_ms, launchpad, created_ms + 900,
                  jdump({"source": source, "block": 9, "signature": "0x" + "33" * 32})))


def test_the_bsc_tail_hands_out_listener_rows_once_and_only_listener_rows(tmp_db):
    _flap_row(tmp_db, "0x" + "01" * 20, 10_000)
    _flap_row(tmp_db, "0x" + "02" * 20, 10_000)  # same block second
    _flap_row(tmp_db, "0x" + "03" * 20, 10_500, source="gmgn:smartmoney")  # GMGN's late copy: not a snipe
    _flap_row(tmp_db, "0x" + "04" * 20, 10_600, chain="sol", launchpad="flap")
    mark = lf.BscWatermark(created_ms=9_000)
    first = lf.tail_bsc(tmp_db, mark, now_ms=11_000)
    assert [x.token for x in first] == ["0x" + "01" * 20, "0x" + "02" * 20]
    assert first[0].latency_ms == 1_000 and first[0].tx == "0x" + "33" * 32 and first[0].venue == "flap"
    # a row written late for an older second is still found (the overlap), and nothing is handed out twice
    _flap_row(tmp_db, "0x" + "05" * 20, 9_500)
    second = lf.tail_bsc(tmp_db, mark, now_ms=12_000)
    assert [x.token for x in second] == ["0x" + "05" * 20]
    assert lf.tail_bsc(tmp_db, mark, now_ms=12_000) == []


def test_the_bsc_tail_reads_through_the_created_index_not_the_whole_chain(tmp_db):
    """Without the unary ``+`` the planner takes idx_tokens_creator (chain=?): every bsc token."""
    plan = " ".join(str(r["detail"]) for r in fetch_all(tmp_db, "EXPLAIN QUERY PLAN " + lf.BSC_TAIL_SQL,
                                                        (0, "bsc", "flap", 5)))
    assert "idx_tokens_created" in plan and "idx_tokens_creator" not in plan, plan


# ------------------------------------------------------------------------- review fixes 2026-10-05

def _launch(**kw):
    return lf.flap_launch_from_log(created_log(**kw), received_ms=T0 * 1000 + 900)


def test_a_graduation_never_creates_a_row(tmp_db):
    """An ownerless graduation-only row (symbol, name, creator and birth all NULL) could never
    be filled or screened by GMGN; a held token always has a row of its own."""
    grad = lf.flap_graduation_from_log(graduated_log(), received_ms=5)
    assert flap_ingest.record_migration(grad, tmp_db) is False
    assert fetch_one(tmp_db, "SELECT COUNT(*) AS n FROM tokens")["n"] == 0
    flap_ingest.record_new_token(flap_ingest.token_from_launch(_launch()), tmp_db)
    assert flap_ingest.record_migration(grad, tmp_db) is True
    later = lf.flap_graduation_from_log(graduated_log(ts=T0 + 9_999), received_ms=6)
    flap_ingest.record_migration(later, tmp_db)  # a second stamp never moves the grace period
    row = fetch_one(tmp_db, "SELECT migrated_ms, pool FROM tokens WHERE address=?", (TOKEN,))
    assert row["migrated_ms"] == (T0 + 400) * 1000 and row["pool"] == POOL


def _gmgn_row(**facts):
    from kaiba.ingest import gmgn_feeds

    base = {"address": TOKEN, "symbol": "GMSYM", "name": "gmgn name", "creator": "0x" + "dd" * 20, "created_ms": 5,
            "migrated_ms": None, "launchpad": "flap", "pool": None, "meta": {"logo": "https://x/logo.png"}}
    merged = {**base, **facts}
    return gmgn_feeds.AlphaRow(chain=Chain.BSC, feed="trending", token=merged["address"], ts_ms=T0 * 1000,
                               token_facts=merged)


def test_gmgn_merges_into_a_listener_row_and_screens_it_exactly_once(tmp_db, monkeypatch):
    """The blocker (review 2026-10-05): GMGN returned "kept" for a listener row, so no Flap
    token GMGN surfaced was ever triaged again -- and on bsc GMGN is the only screener."""
    import kaiba.execution.triage as triage
    from kaiba.ingest import gmgn_feeds

    assert lf.FLAP_SOURCE in gmgn_feeds.MERGEABLE_LISTENER_SOURCES
    screened = []
    monkeypatch.setattr(triage, "screen_launch", lambda launch, conn=None: screened.append(dict(launch)))
    flap_ingest.record_new_token(flap_ingest.token_from_launch(_launch()), tmp_db)
    tmp_db.execute("UPDATE tokens SET symbol=NULL WHERE address=?", (TOKEN,))  # the chain gave no symbol

    assert gmgn_feeds.write_token(tmp_db, _gmgn_row()) == "merged"
    assert gmgn_feeds.write_token(tmp_db, _gmgn_row(migrated_ms=7)) == "merged"
    row = fetch_one(tmp_db, "SELECT * FROM tokens WHERE chain='bsc' AND address=?", (TOKEN,))
    meta = json.loads(row["meta_json"])
    assert len(screened) == 1 and screened[0]["mint"] == TOKEN and screened[0]["source"] == "gmgn:trending"
    assert screened[0]["creator"] == CREATOR  # the listener's chain fact, not GMGN's
    assert row["creator"] == CREATOR and meta["source"] == lf.FLAP_SOURCE and meta[gmgn_feeds.GMGN_SCREENED_KEY]
    assert row["symbol"] == "GMSYM" and row["name"] == "Faucet" and row["migrated_ms"] == 7  # NULLs filled only
    assert meta["logo"] == "https://x/logo.png" and meta["block"] == 125_000_000
    assert len(fetch_all(tmp_db, "SELECT payload FROM events WHERE kind='token.created'")) == 1
    # the snipe tail still owns the row
    assert lf.bsc_launch_from_row(dict(row), received_ms=1) is not None


def test_a_listener_refresh_keeps_gmgns_latch_and_a_backfill_never_takes_a_gmgn_row(tmp_db, monkeypatch):
    import kaiba.execution.triage as triage
    from kaiba.ingest import gmgn_feeds

    screened = []
    monkeypatch.setattr(triage, "screen_launch", lambda launch, conn=None: screened.append(launch))
    token = flap_ingest.token_from_launch(_launch())
    flap_ingest.record_new_token(token, tmp_db)
    gmgn_feeds.write_token(tmp_db, _gmgn_row())
    assert flap_ingest.record_new_token(token, tmp_db) == "refreshed"  # a re-backfill after a restart
    gmgn_feeds.write_token(tmp_db, _gmgn_row())
    assert len(screened) == 1, "the refresh dropped gmgn_screened and GMGN screened the token twice"

    other = "0x" + "ef" * 18 + "7777"
    assert gmgn_feeds.write_token(tmp_db, _gmgn_row(address=other)) == "inserted"
    owned = fetch_one(tmp_db, "SELECT meta_json FROM tokens WHERE address=?", (other,))
    assert json.loads(owned["meta_json"])["source"] == "gmgn:trending"
    assert flap_ingest.record_new_token(flap_ingest.token_from_launch(_launch(token=other)), tmp_db) == "filled"
    after = fetch_one(tmp_db, "SELECT creator, created_ms, meta_json FROM tokens WHERE address=?", (other,))
    assert json.loads(after["meta_json"])["source"] == "gmgn:trending" and after["creator"] == "0x" + "dd" * 20
    assert after["created_ms"] == 5


class FakeRpc:
    """``eth_getTransactionByHash`` / ``eth_getCode`` stand-in that counts what it is asked."""

    def __init__(self, sender: str, code: dict | None = None, *, dead: bool = False):
        self.sender, self.code, self.dead, self.calls = sender, code or {}, dead, []

    def __call__(self, calls):
        self.calls.extend(m for m, _ in calls)
        if self.dead:
            return None
        return [{"from": self.sender} if m == "eth_getTransactionByHash" else self.code.get(a[0], "0x")
                for m, a in calls]


LAUNCHER = "0x" + "1a" * 20
DEV = "0x" + "de" * 20


@pytest.mark.parametrize("creator,sender,code,want,basis", [
    (CREATOR, CREATOR, {}, CREATOR, "creator_is_sender"),
    (LAUNCHER, DEV, {LAUNCHER: "0x6080604052"}, DEV, "sender_of_contract_creator"),
    (CREATOR, DEV, {CREATOR: "0xef0100" + "ab" * 20}, CREATOR, "creator_eoa_not_sender"),  # EIP-7702: an EOA
    (CREATOR, DEV, {}, CREATOR, "creator_eoa_not_sender"),                              # relayed EOA
], ids=["eoa", "launcher_contract", "eip7702", "relayed"])
def test_the_row_is_keyed_on_the_real_sender_when_the_creator_is_a_launcher(creator, sender, code, want, basis):
    rpc = FakeRpc(sender, code)
    got = flap_ingest.SenderResolver(rpc).resolve(_launch(creator=creator))
    assert (got.creator, got.event_creator, got.basis) == (want, creator, basis)
    assert rpc.calls[0] == "eth_getTransactionByHash"
    assert ("eth_getCode" in rpc.calls) is (creator != sender)  # the code is read only when they differ


def test_a_launchers_code_is_read_once_and_a_dead_rpc_keeps_the_event_creator():
    rpc = FakeRpc(DEV, {LAUNCHER: "0x6080604052"})
    resolver = flap_ingest.SenderResolver(rpc)
    for i in range(3):
        resolver.resolve(_launch(creator=LAUNCHER, tx="0x" + format(i, "02x") * 32))
    assert rpc.calls.count("eth_getCode") == 1 and resolver.cu == 3 * flap_ingest.CU_GET_TX + flap_ingest.CU_GET_CODE
    dead = flap_ingest.SenderResolver(FakeRpc(DEV, dead=True)).resolve(_launch(creator=LAUNCHER))
    assert (dead.creator, dead.basis) == (LAUNCHER, "unresolved:no_tx_from") and not dead.resolved


def test_the_listener_writes_the_resolved_sender_and_keeps_the_event_creator(tmp_db, monkeypatch):
    monkeypatch.setattr(lf.aws, "backoff_delay", lambda attempt, **k: 0.0)
    sock = FakeSocket({"pushes": [created_log(creator=LAUNCHER)], "drop": True})
    dials = iter([sock])

    def dial(url):
        try:
            return next(dials)
        except StopIteration:
            raise ConnectionError("no more sockets") from None

    resolver = flap_ingest.SenderResolver(FakeRpc(DEV, {LAUNCHER: "0x60"}))
    asyncio.run(asyncio.wait_for(flap_ingest.run(enabled=lambda: True, url="https://bnb.example/v2/KEY", connect=dial,
                                                 writer=tmp_db, max_attempts=2, resolver=resolver), timeout=10))
    row = fetch_one(tmp_db, "SELECT creator, meta_json FROM tokens WHERE address=?", (TOKEN,))
    meta = json.loads(row["meta_json"])
    assert row["creator"] == DEV and meta["event_creator"] == LAUNCHER and meta["tx_from"] == DEV
    assert meta["creator_basis"] == "sender_of_contract_creator"


async def _run_until(cond, **kw):
    stop = asyncio.Event()
    task = asyncio.create_task(flap_ingest.run(stop=stop, **kw))
    for _ in range(200):
        await asyncio.sleep(0.02)
        if cond():
            break
    stop.set()
    return await asyncio.wait_for(task, timeout=10)


def test_a_restart_backfills_from_the_persisted_cursor_and_stamps_a_graduation_from_the_gap(tmp_db):
    """Before the cursor, every ingest restart lost the downtime's graduations for good, and
    protection read a held Flap token that graduated meanwhile as a rug."""
    flap_ingest.record_new_token(flap_ingest.token_from_launch(_launch(block=1000)), tmp_db)
    flap_ingest.write_cursor(tmp_db, 1000)  # what the previous process persisted
    sock = FakeSocket({"head": 1010, "backfill": [graduated_log(block=1005)], "pushes": []})

    def stamped():
        return fetch_one(tmp_db, "SELECT migrated_ms FROM tokens WHERE address=?", (TOKEN,))["migrated_ms"]

    got = asyncio.run(_run_until(stamped, enabled=lambda: True, url="https://bnb.example/v2/KEY",
                                 connect=lambda url: sock, writer=tmp_db,
                                 resolver=flap_ingest.SenderResolver(FakeRpc(CREATOR)), recheck_s=0.05))
    gl = [m for m in sock.sent if m["method"] == "eth_getLogs"]
    assert gl and gl[0]["params"][0]["fromBlock"] == hex(1000), "the first connection did not backfill"
    assert stamped() == (T0 + 400) * 1000
    assert got["graduations"] == 1 and got["cursor"] == 1005 and flap_ingest.read_cursor(tmp_db) == 1005


class ClosingSocket(FakeSocket):
    def __init__(self, plan):
        super().__init__(plan)
        self.closed = 0

    async def __aexit__(self, *a):
        self.closed += 1
        return False


def _system_events(conn, status):
    out = []
    for r in fetch_all(conn, "SELECT payload FROM events WHERE kind='system'"):
        body = json.loads(r["payload"])
        if body.get("status") == status:
            out.append(body)
    return out


def test_the_listener_closes_its_socket_when_the_lane_drops_bsc(tmp_db):
    switch = {"on": True}
    sock = ClosingSocket({"pushes": [created_log()]})

    def flip():
        if fetch_one(tmp_db, "SELECT COUNT(*) AS n FROM tokens")["n"]:
            switch["on"] = False
        return sock.closed > 0

    got = asyncio.run(_run_until(flip, enabled=lambda: switch["on"], url="https://bnb.example/v2/KEY",
                                 connect=lambda url: sock, writer=tmp_db,
                                 resolver=flap_ingest.SenderResolver(FakeRpc(CREATOR)), recheck_s=0.05))
    assert sock.closed and got["pauses"] == 1 and got["launches"] == 1
    assert _system_events(tmp_db, "paused")[0]["reason"] == "lane_dropped_bsc"


def test_the_listener_idles_itself_past_the_days_cu_ceiling_and_remembers_the_spend(tmp_db):
    sock = ClosingSocket({"pushes": [created_log()]})
    got = asyncio.run(_run_until(lambda: sock.closed > 0, enabled=lambda: True, url="https://bnb.example/v2/KEY",
                                 connect=lambda url: sock, writer=tmp_db, max_cu_per_day=5,
                                 resolver=flap_ingest.SenderResolver(FakeRpc(CREATOR)), recheck_s=0.05))
    assert got["pauses"] == 1 and sock.closed
    assert _system_events(tmp_db, "paused")[0]["reason"].startswith("cu_ceiling:")
    assert flap_ingest.CuMeter.load(tmp_db).cu >= 5  # a restart the same UTC day starts from the spend


def test_the_feed_never_uses_the_shared_bsc_key(monkeypatch):
    from kaiba.core.config import get_settings

    monkeypatch.setenv("BSC_RPC_URL", "https://bnb-mainnet.example/v2/SHARED")
    monkeypatch.setattr(lf, "bsc_snipe_rpc_url", lambda: None)
    get_settings.cache_clear()
    try:
        assert flap_ingest.alchemy_url() is None
        monkeypatch.setattr(lf, "bsc_snipe_rpc_url", lambda: "https://bnb-mainnet.example/v2/OWN")
        assert flap_ingest.alchemy_url() == "https://bnb-mainnet.example/v2/OWN"
    finally:
        get_settings.cache_clear()


# ------------------------------------------------------------------ endpoints (2026-10-21)
#
# flap's socket and its HTTP reads come from kaiba/ingest/feed_endpoints.py. Hosts below are
# public endpoints; no key anywhere.

PUBLICNODE_BSC = "https://bsc-rpc.publicnode.com"
DATASEED = "https://bsc-dataseed.bnbchain.org"


@pytest.fixture
def bsc_env(monkeypatch):
    from kaiba.core.config import get_settings

    for name in (fe.ENV_PUBLIC_FALLBACK, fe.ENV_SHARED_WS_OK, fe.ENV_ALLOW_SHARED_ALCHEMY, fe.ENV_PREFER_PUBLIC,
                 fe.ENV_BSC_SNIPE_WS, fe.ENV_BSC_SNIPE_RPC):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BSC_RPC_URL", PUBLICNODE_BSC)  # protection's bsc host, as on the box
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


def test_flap_refuses_http_on_protection_host(tmp_db, bsc_env):
    """BSC_SNIPE_RPC_URL on protection's host (case and port aside) idles flap with a reason.
    Mutation: delete the http host check."""
    bsc_env.setenv(fe.ENV_PUBLIC_FALLBACK, "flap")
    bsc_env.setenv(fe.ENV_BSC_SNIPE_WS, "wss://bsc-ws.feed.example")
    bsc_env.setenv(fe.ENV_BSC_SNIPE_RPC, "https://BSC-RPC.publicnode.com:443/")
    def no_dial(url):
        raise ConnectionRefusedError("no socket may be opened")

    try:  # an idle feed returns at once; one that found an endpoint would run until stopped
        got = asyncio.run(asyncio.wait_for(flap_ingest.run(enabled=lambda: True, writer=tmp_db, connect=no_dial),
                                           timeout=5))
    except TimeoutError:
        got = {"ran": "it found an endpoint"}
    assert set(got) == {"idle"} and "http host == protection host bsc-rpc.publicnode.com" in got["idle"]
    # positive control: the HTTP reads moved to dataseed (PROBES.md: it passed every method flap sends)
    bsc_env.setenv(fe.ENV_BSC_SNIPE_RPC, DATASEED)
    ok = fe.resolve("flap")
    assert isinstance(ok, fe.FeedEndpoint) and ok.http_host == "bsc-dataseed.bnbchain.org" and not ok.shared_ws


def test_flap_shared_ws_needs_flag(bsc_env):
    """The only public BSC socket that works is protection's provider (PROBES.md): sharing it
    takes KAIBA_FEED_SHARED_WS_OK=bsc. Mutation: drop the flag requirement."""
    bsc_env.setenv(fe.ENV_PUBLIC_FALLBACK, "flap")
    bsc_env.setenv(fe.ENV_BSC_SNIPE_WS, "wss://bsc-rpc.publicnode.com")
    bsc_env.setenv(fe.ENV_BSC_SNIPE_RPC, DATASEED)
    refused = fe.resolve("flap")
    assert isinstance(refused, fe.Refusal) and f"set {fe.ENV_SHARED_WS_OK}=bsc" in refused.reason
    bsc_env.setenv(fe.ENV_SHARED_WS_OK, "sol")  # another chain's flag does not count
    assert isinstance(fe.resolve("flap"), fe.Refusal)
    bsc_env.setenv(fe.ENV_SHARED_WS_OK, "bsc")
    ok = fe.resolve("flap")
    assert isinstance(ok, fe.FeedEndpoint) and ok.shared_ws and ok.ws_host == "bsc-rpc.publicnode.com"
    # socket-only mode: no BSC_SNIPE_RPC_URL at all is allowed for flap (no sender lookups)
    bsc_env.delenv(fe.ENV_BSC_SNIPE_RPC)
    bare = fe.resolve("flap")
    assert isinstance(bare, fe.FeedEndpoint) and bare.http_url is None


def _shared_endpoint(ws: str = "wss://bsc-rpc.publicnode.com") -> fe.FeedEndpoint:
    return fe.FeedEndpoint(feed="flap", chain=Chain.BSC, kind=fe.KIND_PUBLIC, ws_url=ws, http_url=DATASEED,
                           ws_host=fe.host_of(ws) or "", http_host="bsc-dataseed.bnbchain.org", shared_ws=True)


def _set_rpc_penalty(conn, level: int) -> None:
    from kaiba.core import limiter

    limiter._state(conn, "rpc")  # noqa: SLF001 - creates the row
    conn.execute("UPDATE provider_state SET penalty_level=? WHERE provider='rpc'", (level,))


@pytest.mark.parametrize("penalty", [1, 0])
def test_flap_pauses_on_rpc_penalty(tmp_db, monkeypatch, penalty):
    """While the socket shares protection's host, no (re)connect -- and so no eth_getLogs
    backfill -- starts while protection's `rpc` bucket carries a penalty. Positive control: at
    0 it connects and backfills. Mutation: remove the pressure check."""
    monkeypatch.setattr(flap_ingest, "PRESSURE_RECHECK_S", 0.02)
    _set_rpc_penalty(tmp_db, penalty)
    flap_ingest.write_cursor(tmp_db, 1000)
    sock = FakeSocket({"head": 1010, "backfill": [], "pushes": []})
    dials: list[str] = []

    def dial(url):
        dials.append(url)
        return sock

    def backfilled() -> bool:
        return any(m["method"] == "eth_getLogs" for m in sock.sent)

    got = asyncio.run(_run_until(backfilled, enabled=lambda: True, endpoint=_shared_endpoint(), connect=dial,
                                 writer=tmp_db, resolver=flap_ingest.SenderResolver(FakeRpc(CREATOR)),
                                 recheck_s=0.05, pressure=flap_ingest.PressureGauge(tmp_db, ttl_s=0.0)))
    if penalty:
        assert dials == [] and not backfilled(), "a backfill went to protection's host under pressure"
        (paused,) = _system_events(tmp_db, "paused")
        assert paused["reason"] == "protection_rpc_pressure:penalty=1" and got["pauses"] == 1
    else:
        assert dials == ["wss://bsc-rpc.publicnode.com"] and backfilled()


def test_flap_pressure_is_read_once_per_ttl_and_sees_a_family_ban(tmp_db):
    clock = {"t": 0.0}
    gauge = flap_ingest.PressureGauge(tmp_db, ttl_s=10.0, clock=lambda: clock["t"])
    assert gauge.reason() is None
    _set_rpc_penalty(tmp_db, 2)
    assert gauge.reason() is None and gauge.reads == 1  # cached for 10 s
    clock["t"] = 10.0
    assert gauge.reason() == "protection_rpc_pressure:penalty=2" and gauge.reads == 2
    tmp_db.execute("UPDATE provider_state SET penalty_level=0 WHERE provider='rpc'")
    tmp_db.execute("INSERT INTO provider_family_bans (provider, family, banned_until_ms, reason) VALUES "
                   "('rpc','rpc',?,'429')", (flap_ingest.now_ms() + 60_000,))
    assert flap_ingest.protection_rpc_pressure(tmp_db) == "protection_rpc_pressure:ban=rpc"


class CappedNode(FakeSocket):
    """A node that refuses eth_getLogs over more than 5,000 blocks and serves smaller ranges."""

    LIMIT = 5_000

    async def send(self, raw):
        msg = json.loads(raw)
        if msg["method"] == "eth_getLogs":
            self.sent.append(msg)
            q = msg["params"][0]
            span = int(q["toBlock"], 16) - int(q["fromBlock"], 16) + 1
            if span > self.LIMIT:
                await self.inbox.put(json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                                                 "error": {"code": -32005, "message": "block range too large"}}))
            else:
                await self.inbox.put(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": []}))
            return
        await super().send(raw)


def _reached_head(node: FakeSocket, head: int) -> bool:
    return any(m["method"] == "eth_getLogs" and int(m["params"][0]["toBlock"], 16) == head for m in node.sent)


@pytest.mark.parametrize("chunk,ok_first_try", [(2_000, True), (20_000, False)])
def test_flap_backfill_chunk_on_capped_node(tmp_db, monkeypatch, chunk, ok_first_try):
    """A 2,000-block chunk backfills at once; a 20,000-block chunk the node refuses ends within
    a bounded number of attempts (halved after BACKFILL_FAILURE_LIMIT refusals) instead of
    looping. Mutation: remove the guard."""
    # A tiny but real backoff: these fake sockets never block, so a 0 s backoff would spin the
    # reconnect loop without ever yielding to the test's own clock.
    monkeypatch.setattr(lf.aws, "backoff_delay", lambda attempt, **k: 0.001)
    flap_ingest.write_cursor(tmp_db, 1_000)
    head = 1_000 + 30_000
    nodes: list[CappedNode] = []
    stop_after = 30

    def dial(url):
        node = CappedNode({"head": head, "pushes": []})
        nodes.append(node)
        return node

    def done() -> bool:
        return len(nodes) >= stop_after or any(_reached_head(n, head) for n in nodes)

    got = asyncio.run(_run_until(done, enabled=lambda: True, url="wss://bsc-ws.feed.example", connect=dial,
                                 writer=tmp_db, resolver=flap_ingest.SenderResolver(FakeRpc(CREATOR)),
                                 recheck_s=0.05, backfill_chunk_blocks=chunk))
    assert len(nodes) < stop_after, f"{len(nodes)} connections: the refused backfill looped"
    assert _reached_head(nodes[-1], head), "the backfill never completed"
    if ok_first_try:
        assert len(nodes) == 1 and got["backfill_failures"] == 0 and got["backfill_chunk_blocks"] == 2_000
    else:
        # 20,000 refused x3 -> 10,000 refused x3 -> 5,000 served: 7 connections
        assert len(nodes) == 2 * flap_ingest.BACKFILL_FAILURE_LIMIT + 1
        assert got["backfill_chunk_blocks"] == 5_000 and got["chunk_shrinks"] == 2
        assert [e["backfill_chunk_blocks"] for e in _system_events(tmp_db, "backfill_chunk_reduced")] == [10_000, 5_000]


class GapNode(FakeSocket):
    """Answers the backfill with no logs, then drops the socket: the stale cursor never moves."""

    async def send(self, raw):
        msg = json.loads(raw)
        if msg["method"] == "eth_getLogs":
            self.sent.append(msg)
            await self.inbox.put(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": []}))
            if int(msg["params"][0]["toBlock"], 16) == self.plan["head"]:
                await self.inbox.put(None)  # dropped right after the backfill
            return
        await super().send(raw)


def test_stale_cursor_reports_gap_truncated_once(tmp_db, monkeypatch):
    """A cursor past the backfill cap (the box's: ~12 days old) is reported once, not on every
    reconnect that re-reads the same stale cursor. Mutation: remove the dedupe."""
    monkeypatch.setattr(lf.aws, "backoff_delay", lambda attempt, **k: 0.001)  # yields; see above
    flap_ingest.write_cursor(tmp_db, 1_000)
    head = 1_000 + 2_000_000  # far past the 40,000-block cap
    nodes: list[GapNode] = []

    def dial(url):
        node = GapNode({"head": head, "pushes": []})
        nodes.append(node)
        return node

    got = asyncio.run(_run_until(lambda: len(nodes) >= 3, enabled=lambda: True, url="wss://bsc-ws.feed.example",
                                 connect=dial, writer=tmp_db, resolver=flap_ingest.SenderResolver(FakeRpc(CREATOR)),
                                 recheck_s=0.05, backfill_chunk_blocks=40_000))
    gaps = _system_events(tmp_db, "gap_truncated")
    assert len(nodes) >= 3 and len(gaps) == 1, gaps
    assert gaps[0]["from_block"] == 1_000 and gaps[0]["lost_blocks"] == head - 40_000 - 1_000
    assert got["gap_repeats"] >= 2 and got["backfill_failures"] == 0


def test_flap_public_endpoint_uses_a_120s_idle_timeout_and_the_chunk_parameter(tmp_db, monkeypatch):
    """Two quiet minutes on a public socket is a dead socket (147-288 launches an hour)."""
    seen: list[dict] = []

    async def fake_stream(url, **kw):
        seen.append({"url": url, **kw})
        kw["stop"].set()
        return
        yield  # pragma: no cover - an async generator

    monkeypatch.setattr(lf, "stream_flap", fake_stream)

    async def go(**kw):
        stop = asyncio.Event()
        task = asyncio.create_task(flap_ingest.run(stop=stop, enabled=lambda: True, writer=tmp_db,
                                                   resolver=flap_ingest.SenderResolver(FakeRpc(CREATOR)),
                                                   recheck_s=0.05, **kw))
        for _ in range(100):
            await asyncio.sleep(0.01)
            if seen:
                break
        stop.set()
        return await asyncio.wait_for(task, 5)

    asyncio.run(go(endpoint=_shared_endpoint(), backfill_chunk_blocks=20_000, pause_on_rpc_pressure=False))
    assert seen[0]["url"] == "wss://bsc-rpc.publicnode.com"
    assert seen[0]["idle_timeout_s"] == flap_ingest.PUBLIC_IDLE_TIMEOUT_S == 120.0
    assert seen[0]["backfill_chunk_blocks"] == 20_000
    seen.clear()
    asyncio.run(go(url="https://bnb.example/v2/KEY"))  # an explicit (keyed) URL keeps the old 300 s / 5,000
    assert seen[0]["idle_timeout_s"] == 300.0 and seen[0]["backfill_chunk_blocks"] == 5_000
