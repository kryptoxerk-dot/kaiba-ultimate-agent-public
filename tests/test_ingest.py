"""Real-time ingest: PumpPortal, GMGN feeds, Telegram call rooms, and the supervisor.

Offline by design. Every provider response comes from ``tests/fixtures/ingest/*.json``,
recorded against the documented shapes in ``docs/research/04-data-sources.md``. Nothing
here opens a socket, and the GMGN tests pass whether or not the CLI wrapper
(``kaiba/providers/gmgn_cli.py``, owned by the sibling task) exists yet.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
from pathlib import Path

import base58
import pytest

from kaiba.core.db import fetch_all, fetch_one
from kaiba.core.schemas import Chain, EventKind, now_ms
from kaiba.ingest import gmgn_feeds, pumpportal, runner, telegram_calls

FIXTURES = Path(__file__).parent / "fixtures" / "ingest"


def fixture(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def mint(seed: str) -> str:
    """A syntactically real Solana pubkey derived from a seed, for deterministic tests."""
    return base58.b58encode(hashlib.sha256(seed.encode()).digest()).decode()


def events_of(conn, kind: EventKind) -> list[dict]:
    return fetch_all(conn, "SELECT * FROM events WHERE kind = ? ORDER BY id", (kind.value,))


@pytest.fixture(autouse=True)
def _clean_latency():
    pumpportal.reset_latency()
    yield
    pumpportal.reset_latency()


@pytest.fixture
def no_limiter(monkeypatch):
    """Bypass the shared limiter so a test can poll twice in a row.

    The limiter itself is covered by ``test_poll_once_reserves_through_the_limiter``; the
    cursor tests would otherwise get a zero for the wrong reason (minimum interval).
    """

    @contextlib.contextmanager
    def passthrough(*_args, **_kwargs):
        yield

    monkeypatch.setattr(gmgn_feeds, "guarded", passthrough)


# =======================================================================================
# PumpPortal — parsers
# =======================================================================================


def test_parse_new_token_from_fixture():
    token = pumpportal.parse_new_token(fixture("pumpportal_new_token"))
    assert token is not None
    assert token.address == "5uiWN8tHqR4ykLDt88UR8htfcG6YK7fqcadfKBgDEPMh"
    assert token.chain is Chain.SOL
    assert token.symbol == "KFIX"
    assert token.name == "Kaiba Fixture Coin"
    assert token.creator == "C9abG9fSgwkenby3GiRfuufnMFuGCW9881WRHuT3fRTN"
    assert token.launchpad == "pump.fun"
    # pool is the derived bonding-curve PDA (2026-10-04): PumpPortal's frame key is wrong on
    # ~12% of creates, so it is kept only as meta.bonding_curve_frame.
    assert token.pool == "HaThj3NxyTjujduk6Ki7x6uLd5cKRiQ7rsZrMyRnMGh"
    assert token.meta["bonding_curve_frame"] == "5uEtuXD3vNYDVkBbfTca2P7mWiTy3vqRm9MFem8HfM1o"
    assert token.created_ms == 1758355200000  # seconds on the wire, ms in the model
    assert token.meta["uri"].startswith("https://ipfs.io/ipfs/")


def test_new_token_sol_amount_becomes_integer_lamports():
    token = pumpportal.parse_new_token(fixture("pumpportal_new_token"))
    assert token is not None
    # 1.95 SOL must survive as an int, not a float: the contract forbids floats on money.
    assert token.meta["initial_buy_lamports"] == 1_950_000_000
    assert isinstance(token.meta["initial_buy_lamports"], int)


def test_parse_migration_from_fixture():
    info = pumpportal.parse_migration(fixture("pumpportal_migration"))
    assert info is not None
    assert info["mint"] == "5uiWN8tHqR4ykLDt88UR8htfcG6YK7fqcadfKBgDEPMh"
    assert info["pool"] == "pumpswap"
    assert info["migrated_ms"] == 1758358800000  # already milliseconds on the wire
    assert info["slot"] == 312899412


def test_parse_trade_from_fixture():
    row = pumpportal.parse_trade(fixture("pumpportal_trade"))
    assert row is not None
    assert row["side"] == "buy"
    assert row["wallet"] == "3Cj4zJ9xdSY8Pc7e4owpSFPUSwGMthgPt5h5G12fy72J"
    assert row["amount_native"] == str(450_000_000)


def test_parsers_reject_each_others_messages():
    create = fixture("pumpportal_new_token")
    migrate = fixture("pumpportal_migration")
    assert pumpportal.parse_migration(create) is None
    assert pumpportal.parse_new_token(migrate) is None
    assert pumpportal.parse_trade(create) is None


@pytest.mark.parametrize("case", fixture("pumpportal_malformed"), ids=lambda c: c["case"])
def test_malformed_messages_are_skipped_not_crashed(case):
    msg = case["message"]
    assert pumpportal.parse_new_token(msg) is None
    assert pumpportal.parse_migration(msg) is None
    assert pumpportal.parse_trade(msg) is None


def test_malformed_messages_write_no_tokens(tmp_db):
    for case in fixture("pumpportal_malformed"):
        assert pumpportal.handle_message(case["message"], conn=tmp_db) is None
    assert pumpportal.handle_message("{not json at all", conn=tmp_db) is None
    assert fetch_all(tmp_db, "SELECT * FROM tokens") == []
    assert events_of(tmp_db, EventKind.TOKEN_CREATED) == []
    assert events_of(tmp_db, EventKind.TOKEN_MIGRATED) == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"timestamp": 1758355200}, 1758355200000),
        ({"timestamp": 1758355200000}, 1758355200000),
        ({"blockTime": 1758355200}, 1758355200000),
        ({"timestamp": 1758355200000000}, 1758355200000),
        ({"timestamp": 0}, None),
        ({"timestamp": "not a number"}, None),
        ({}, None),
    ],
)
def test_source_timestamp_units_are_inferred(raw, expected):
    assert pumpportal.source_timestamp_ms(raw) == expected


# =======================================================================================
# PumpPortal — the one-connection rule
# =======================================================================================


def test_lock_refuses_a_second_instance(tmp_path):
    path = tmp_path / "pumpportal.lock"
    first = pumpportal.ConnectionLock(path).acquire()
    with pytest.raises(pumpportal.AlreadyRunning):
        pumpportal.ConnectionLock(path).acquire()
    first.release()
    second = pumpportal.ConnectionLock(path).acquire()  # free again once released
    second.release()
    assert not path.exists()


def test_lock_reclaims_a_stale_holder(tmp_path):
    path = tmp_path / "pumpportal.lock"
    path.write_text(
        json.dumps({"pid": 999999, "host": "dead-box", "heartbeat_ms": now_ms() - 3_600_000}),
        encoding="utf-8",
    )
    lock = pumpportal.ConnectionLock(path, stale_after_s=60).acquire()
    assert lock.read()["pid"] != 999999
    lock.release()


def test_lock_treats_debris_as_stale(tmp_path):
    path = tmp_path / "pumpportal.lock"
    path.write_text("half-written garbage", encoding="utf-8")
    lock = pumpportal.ConnectionLock(path).acquire()
    lock.release()


def test_lock_heartbeat_keeps_a_holder_alive(tmp_path):
    path = tmp_path / "pumpportal.lock"
    lock = pumpportal.ConnectionLock(path, stale_after_s=0.0001).acquire()
    lock.heartbeat()
    assert lock.read()["heartbeat_ms"] > 0
    lock.release()


async def test_run_refuses_to_open_a_second_socket(tmp_db, tmp_path):
    path = tmp_path / "pumpportal.lock"
    held = pumpportal.ConnectionLock(path).acquire()

    def must_not_dial(url):  # pragma: no cover - the point is that it is never called
        raise AssertionError("run() dialled PumpPortal while another instance held the lock")

    try:
        with pytest.raises(pumpportal.AlreadyRunning):
            await pumpportal.run(
                asyncio.Event(), connect=must_not_dial, lock_path=path, conn=tmp_db
            )
    finally:
        held.release()


# =======================================================================================
# PumpPortal — reconnect schedule
# =======================================================================================


def test_backoff_schedule_is_exponential_and_capped():
    zero = lambda: 0.0  # noqa: E731 - a one-liner stub reads better inline here
    assert [pumpportal.backoff_delay(i, rand=zero) for i in range(8)] == [1, 2, 4, 8, 16, 32, 60, 60]


def test_backoff_jitter_stays_between_the_raw_delay_and_the_cap():
    for attempt in range(10):
        low = pumpportal.backoff_delay(attempt, rand=lambda: 0.0)
        high = pumpportal.backoff_delay(attempt, rand=lambda: 1.0)
        assert low <= high <= 60.0
    assert pumpportal.backoff_delay(2, rand=lambda: 1.0) == pytest.approx(5.0)


async def test_reconnect_sleeps_the_backoff_sequence(tmp_db, tmp_path, monkeypatch):
    delays: list[float] = []

    async def fake_wait(stop, seconds):
        delays.append(seconds)
        if len(delays) >= 4:
            stop.set()

    monkeypatch.setattr(pumpportal, "_wait", fake_wait)
    monkeypatch.setattr(pumpportal, "_rand", lambda: 0.0)

    def refused(url):
        raise ConnectionRefusedError("pumpportal is down")

    await pumpportal.run(
        asyncio.Event(), connect=refused, lock_path=tmp_path / "pp.lock", conn=tmp_db
    )
    assert delays == [1, 2, 4, 8]
    errors = events_of(tmp_db, EventKind.PROVIDER_ERROR)
    assert len(errors) == 4
    assert "ConnectionRefusedError" in json.loads(errors[0]["payload"])["error"]


async def test_reconnect_releases_the_lock_when_it_stops(tmp_db, tmp_path, monkeypatch):
    async def fake_wait(stop, seconds):
        stop.set()

    monkeypatch.setattr(pumpportal, "_wait", fake_wait)
    path = tmp_path / "pp.lock"

    def refused(url):
        raise ConnectionRefusedError("down")

    await pumpportal.run(asyncio.Event(), connect=refused, lock_path=path, conn=tmp_db)
    assert not path.exists()  # a crashed listener must not block the next start


# =======================================================================================
# PumpPortal — latency
# =======================================================================================


def test_percentile_is_nearest_rank():
    assert pumpportal.percentile([10, 20, 30], 50) == 20
    assert pumpportal.percentile(list(range(1, 101)), 95) == 95
    assert pumpportal.percentile([5], 99) == 5
    with pytest.raises(ValueError):
        pumpportal.percentile([], 50)


def test_latency_tracker_maths():
    tracker = pumpportal.LatencyTracker(window=10)
    base = 1_700_000_000_000
    for lag in (100, 200, 300, 400, 500):
        tracker.record(pumpportal.STREAM_NEW_TOKEN, base, base + lag)
    stats = tracker.stats()["streams"][pumpportal.STREAM_NEW_TOKEN]
    assert stats["samples"] == 5
    assert stats["p50_ms"] == 300
    assert stats["p95_ms"] == 500
    assert stats["min_ms"] == 100
    assert stats["max_ms"] == 500
    assert stats["mean_ms"] == 300.0


def test_latency_window_rolls_and_ignores_timestampless_messages():
    tracker = pumpportal.LatencyTracker(window=10)
    base = 1_700_000_000_000
    for i in range(25):
        tracker.record("trade", base, base + i)
    assert tracker.record("trade", None) is None
    assert tracker.stats()["streams"]["trade"]["samples"] == 10


def test_latency_stats_separates_streams_and_reports_overall():
    tracker = pumpportal.LatencyTracker()
    base = 1_700_000_000_000
    tracker.record(pumpportal.STREAM_NEW_TOKEN, base, base + 100)
    tracker.record(pumpportal.STREAM_MIGRATION, base, base + 900)
    stats = tracker.stats()
    assert set(stats["streams"]) == {pumpportal.STREAM_NEW_TOKEN, pumpportal.STREAM_MIGRATION}
    assert stats["overall"]["samples"] == 2
    assert stats["overall"]["max_ms"] == 900


def test_publish_latency_writes_a_rollup_and_a_budget_event(tmp_db):
    tracker = pumpportal.LatencyTracker()
    base = now_ms()
    for lag in (120, 240, 360):
        tracker.record(pumpportal.STREAM_NEW_TOKEN, base, base + lag)
    assert pumpportal.publish_latency(conn=tmp_db, tracker=tracker) is not None
    row = fetch_one(tmp_db, "SELECT * FROM ingest_latency")
    assert row["source"] == "pumpportal"
    assert row["samples"] == 3
    assert row["p50_ms"] == 240
    budget = events_of(tmp_db, EventKind.PROVIDER_BUDGET)
    assert len(budget) == 1
    assert json.loads(budget[0]["payload"])["provider"] == "pumpportal"


def test_publish_latency_is_a_noop_without_samples(tmp_db):
    assert pumpportal.publish_latency(conn=tmp_db, tracker=pumpportal.LatencyTracker()) is None


# =======================================================================================
# PumpPortal — persistence and the socket loop
# =======================================================================================


def test_new_token_is_upserted_and_emitted_once(tmp_db):
    token = pumpportal.parse_new_token(fixture("pumpportal_new_token"))
    assert pumpportal.record_new_token(token, conn=tmp_db) is True
    assert pumpportal.record_new_token(token, conn=tmp_db) is False  # dedupe key is the mint

    row = fetch_one(tmp_db, "SELECT * FROM tokens")
    assert row["address"] == token.address
    assert row["launchpad"] == "pump.fun"
    assert row["creator"] == token.creator
    created = events_of(tmp_db, EventKind.TOKEN_CREATED)
    assert len(created) == 1
    assert created[0]["subject"] == token.address
    assert created[0]["chain"] == "sol"


def test_migration_stamps_the_token_without_clobbering_creation_data(tmp_db):
    token = pumpportal.parse_new_token(fixture("pumpportal_new_token"))
    pumpportal.record_new_token(token, conn=tmp_db)
    info = pumpportal.parse_migration(fixture("pumpportal_migration"))
    assert pumpportal.record_migration(info, conn=tmp_db) is True
    assert pumpportal.record_migration(info, conn=tmp_db) is False

    row = fetch_one(tmp_db, "SELECT * FROM tokens")
    assert row["migrated_ms"] == 1758358800000
    assert row["symbol"] == "KFIX"  # creation data survived the migration upsert
    assert len(events_of(tmp_db, EventKind.TOKEN_MIGRATED)) == 1


def test_migration_of_an_unseen_mint_still_records(tmp_db):
    info = pumpportal.parse_migration(fixture("pumpportal_migration"))
    assert pumpportal.record_migration(info, conn=tmp_db) is True
    assert fetch_one(tmp_db, "SELECT * FROM tokens")["migrated_ms"] == 1758358800000


def test_trade_frames_land_in_swaps(tmp_db):
    row = pumpportal.parse_trade(fixture("pumpportal_trade"))
    assert pumpportal.record_trade(row, conn=tmp_db) is True
    assert pumpportal.record_trade(row, conn=tmp_db) is False
    swap = fetch_one(tmp_db, "SELECT * FROM swaps")
    assert swap["side"] == "buy"
    assert swap["source"] == "pumpportal"


def test_token_trade_subscription_needs_a_paid_key():
    watchlist = ["5uiWN8tHqR4ykLDt88UR8htfcG6YK7fqcadfKBgDEPMh"]
    free = pumpportal.subscribe_payloads(watchlist, api_key=None)
    assert [f["method"] for f in free] == ["subscribeNewToken", "subscribeMigration"]

    paid = pumpportal.subscribe_payloads(watchlist, api_key="a-key")
    assert [f["method"] for f in paid][-1] == "subscribeTokenTrade"
    assert paid[-1]["keys"] == watchlist

    assert len(pumpportal.subscribe_payloads(None, api_key="a-key")) == 2


class FakeSocket:
    """Minimal websocket stand-in: yields frames, then ends the stream."""

    def __init__(self, frames, *, fail_after: bool = False):
        self.frames = list(frames)
        self.sent: list[dict] = []
        self.fail_after = fail_after

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, payload):
        self.sent.append(json.loads(payload))

    def __aiter__(self):
        async def gen():
            for frame in self.frames:
                yield json.dumps(frame) if not isinstance(frame, str) else frame
            if self.fail_after:
                raise ConnectionResetError("socket closed by peer")

        return gen()


async def test_consume_handles_a_mixed_stream(tmp_db):
    frames = [
        {"message": "Successfully subscribed to token creation events."},
        fixture("pumpportal_new_token"),
        "{broken json",
        fixture("pumpportal_migration"),
        {"case": "junk"},
    ]
    handled = await pumpportal.consume(FakeSocket(frames), asyncio.Event(), conn=tmp_db)
    assert handled == 2
    assert len(events_of(tmp_db, EventKind.TOKEN_CREATED)) == 1
    assert len(events_of(tmp_db, EventKind.TOKEN_MIGRATED)) == 1
    assert pumpportal.latency_stats()["streams"][pumpportal.STREAM_NEW_TOKEN]["samples"] == 1


async def test_run_subscribes_then_consumes(tmp_db, tmp_path, monkeypatch):
    socket = FakeSocket([fixture("pumpportal_new_token")])

    async def fake_wait(stop, seconds):
        stop.set()

    monkeypatch.setattr(pumpportal, "_wait", fake_wait)
    await pumpportal.run(
        asyncio.Event(),
        connect=lambda url: socket,
        lock_path=tmp_path / "pp.lock",
        conn=tmp_db,
    )
    assert [f["method"] for f in socket.sent] == ["subscribeNewToken", "subscribeMigration"]
    assert len(events_of(tmp_db, EventKind.TOKEN_CREATED)) == 1


# =======================================================================================
# GMGN — envelopes and parsers
# =======================================================================================


def test_error_envelope_is_detected():
    assert gmgn_feeds.envelope_error(fixture("gmgn_error_envelope")).startswith("code=429")
    assert gmgn_feeds.envelope_error(fixture("gmgn_track_smartmoney")) is None
    assert gmgn_feeds.envelope_error(None) == "empty response"
    assert gmgn_feeds.envelope_error([{"a": 1}]) is None


def test_parse_smartmoney():
    rows = gmgn_feeds.parse_smartmoney(fixture("gmgn_track_smartmoney"))
    assert len(rows) == 2  # the row without a tx hash is dropped
    buy = rows[0]
    assert buy.chain is Chain.SOL
    assert buy.side == "buy"
    assert buy.wallet == "3Cj4zJ9xdSY8Pc7e4owpSFPUSwGMthgPt5h5G12fy72J"
    assert buy.token == "Gp32UJ3xavL1sHSH9QKRygZPiQXhcoxazE4G48fu4QcA"
    assert buy.ts_ms == 1758355240000
    assert buy.usd_value == "504.82"
    assert "smart_degen" in buy.tags
    assert rows[1].side == "sell"
    assert buy.to_swap()["source"] == "gmgn:smartmoney"


def test_parse_kol_keeps_each_rows_own_chain():
    rows = gmgn_feeds.parse_kol(fixture("gmgn_track_kol"))
    assert len(rows) == 2
    assert rows[0].chain is Chain.SOL
    assert rows[1].chain is Chain.ROBINHOOD  # the row says robinhood, not the request
    assert rows[1].wallet_name == "hood.eth"
    assert rows[0].feed == "kol"


def test_parse_signal():
    rows = gmgn_feeds.parse_signal(fixture("gmgn_market_signal"))
    assert len(rows) == 2  # the row without a token address is dropped
    assert rows[0].label == "Smart Money Net Buy"
    assert rows[0].payload["signal_type"] == 5
    assert rows[0].ident == "gmgn:signal:sol-8821743"
    assert rows[0].dedupe_key.endswith("sol-8821743")
    assert rows[1].payload["signal_name"] == "Dev Sold"


def test_parse_trenches():
    """The fixture is the real ``--raw`` shape: three lifecycle lists, never ``data.list``.

    Rows captured read-only 2026-09-22 from the live box's own provider cache (sol,
    ``market trenches --filter-preset smart-money``), trimmed to the keys the parser reads.
    """
    rows = gmgn_feeds.parse_trenches(fixture("gmgn_market_trenches"))
    assert len(rows) == 3
    assert [r.payload["trenches_category"] for r in rows] == [
        "new_creation", "near_completion", "completed",
    ]
    first = rows[0]
    assert first.token == "EoTd8vi6BFSzSZXn8FUnJVPnnL6HSyECP3v881Bgpump"
    assert first.chain is Chain.SOL
    assert first.payload["smart_degen_count"] == 1
    assert first.payload["rug_ratio"] == 0.3 and type(first.payload["rug_ratio"]) is float
    assert first.label == "smart-money"
    assert first.bucket_s == 0  # entering the list is a one-off event per token
    assert first.payload["created_ms"] == 1790021401000
    assert first.payload["migrated_ms"] is None  # complete_timestamp 0: not graduated
    done = rows[2]
    assert done.payload["migrated_ms"] == 1790020734000  # complete_timestamp -> graduation
    assert done.payload["rug_ratio"] == 0.5
    assert done.token_facts["launchpad"] == "Pump.fun"


def test_parse_trenches_still_accepts_a_list_shaped_payload():
    """The pre-2026-09-21 fixture guessed a ``data.rank`` list. The list path of ``_rows``
    survives for a vendor shape change; the guess is kept, labelled, only to pin it."""
    payload = fixture("gmgn_market_trenches_list")
    assert payload["_provenance"].startswith("INVENTED")
    rows = gmgn_feeds.parse_trenches(payload)
    assert [r.token for r in rows] == [
        "5uiWN8tHqR4ykLDt88UR8htfcG6YK7fqcadfKBgDEPMh",
        "4iUGz6BJNT7nrEYVDaq6ozDt6QN8WJRXfKQbbn43xMAX",
    ]
    assert rows[0].payload["rug_ratio"] == 0.05
    assert "trenches_category" not in rows[0].payload  # a bare list names no lifecycle stage


def test_parse_trending_buckets_hourly():
    rows = gmgn_feeds.parse_trending(fixture("gmgn_market_trending"))
    assert len(rows) == 2
    assert rows[0].payload["rank"] == 1
    assert rows[0].payload["price_change_pct"] == 42.8
    assert rows[0].bucket_s == 3600
    # trending is a state, so the same token in the same hour is one event
    assert rows[0].dedupe_key == rows[0].model_copy(update={"ts_ms": rows[0].ts_ms + 1}).dedupe_key


@pytest.mark.parametrize(
    "parser",
    [
        gmgn_feeds.parse_smartmoney,
        gmgn_feeds.parse_kol,
        gmgn_feeds.parse_signal,
        gmgn_feeds.parse_trenches,
        gmgn_feeds.parse_trending,
    ],
)
def test_every_parser_returns_nothing_for_an_error_envelope(parser):
    assert parser(fixture("gmgn_error_envelope")) == []
    assert parser({"code": 0, "data": {}}) == []
    assert parser(None) == []


# =======================================================================================
# GMGN — the invocations we expect from the CLI wrapper
# =======================================================================================


def test_expected_cli_invocations():
    """The contract with ``kaiba/providers/gmgn_cli.py``. Changing this changes the wrapper."""
    assert gmgn_feeds.invocation(Chain.SOL, "smartmoney") == (
        "track", "smartmoney", {"chain": "sol", "limit": 100},
    )
    assert gmgn_feeds.invocation(Chain.SOL, "kol") == (
        "track", "kol", {"chain": "sol", "limit": 100},
    )
    # No --limit on signal: gmgn-cli `market signal` rejects it (MEASURED 2026-09-21: 129/129
    # calls in an hour failed "unknown option '--limit'"; the feed had never returned a row).
    assert gmgn_feeds.invocation(Chain.BSC, "signal") == (
        "market", "signal", {"chain": "bsc"},
    )
    assert gmgn_feeds.invocation(Chain.SOL, "trenches") == (
        "market", "trenches", {"chain": "sol", "filter_preset": "smart-money", "limit": 50},
    )
    assert gmgn_feeds.invocation(Chain.ROBINHOOD, "trending") == (
        "market", "trending", {"chain": "robinhood", "interval": "1h", "limit": 50},
    )


def test_unsupported_chain_for_a_feed_is_skipped(tmp_db):
    called = []

    def spy(*args, **kwargs):  # pragma: no cover - must never run
        called.append(args)
        return {}

    # `market signal` is not offered on base, so we must not spend a request finding out.
    assert gmgn_feeds.poll_once(Chain.BASE, "signal", tmp_db, runner=spy) == 0
    assert called == []


def test_unknown_feed_name_is_a_programming_error(tmp_db):
    with pytest.raises(KeyError):
        gmgn_feeds.poll_once(Chain.SOL, "not-a-feed", tmp_db)


# =======================================================================================
# GMGN — polling, cursors, errors
# =======================================================================================


def test_poll_once_writes_swaps_and_wallet_trade_events(tmp_db, no_limiter):
    payload = fixture("gmgn_track_smartmoney")
    written = gmgn_feeds.poll_once(Chain.SOL, "smartmoney", tmp_db, runner=lambda *a, **k: payload)
    assert written == 2
    swaps = fetch_all(tmp_db, "SELECT * FROM swaps ORDER BY ts_ms")
    assert [s["side"] for s in swaps] == ["buy", "sell"]
    assert swaps[0]["source"] == "gmgn:smartmoney"
    assert len(events_of(tmp_db, EventKind.WALLET_TRADE)) == 2


def test_second_poll_of_identical_data_emits_nothing(tmp_db, no_limiter):
    payload = fixture("gmgn_track_smartmoney")
    call = lambda *a, **k: payload  # noqa: E731
    assert gmgn_feeds.poll_once(Chain.SOL, "smartmoney", tmp_db, runner=call) == 2
    assert gmgn_feeds.poll_once(Chain.SOL, "smartmoney", tmp_db, runner=call) == 0
    assert gmgn_feeds.poll_once(Chain.SOL, "smartmoney", tmp_db, runner=call) == 0
    assert len(fetch_all(tmp_db, "SELECT * FROM swaps")) == 2
    assert len(events_of(tmp_db, EventKind.WALLET_TRADE)) == 2


def test_cursor_advances_and_admits_only_newer_rows(tmp_db, no_limiter):
    payload = fixture("gmgn_track_smartmoney")
    gmgn_feeds.poll_once(Chain.SOL, "smartmoney", tmp_db, runner=lambda *a, **k: payload)
    cursor = gmgn_feeds.load_cursor(tmp_db, Chain.SOL, "smartmoney")
    assert cursor["ts_ms"] == 1758355305000  # the newest row in the fixture
    assert len(cursor["seen"]) == 2

    newer = json.loads(json.dumps(payload))
    fresh = dict(newer["data"]["list"][0])
    fresh["tx_hash"] = "NEWTXHASH" + "1" * 10
    fresh["timestamp"] = 1758355999
    newer["data"]["list"] = [fresh]
    assert gmgn_feeds.poll_once(Chain.SOL, "smartmoney", tmp_db, runner=lambda *a, **k: newer) == 1
    assert gmgn_feeds.load_cursor(tmp_db, Chain.SOL, "smartmoney")["ts_ms"] == 1758355999000


def test_cursors_are_per_chain_and_per_feed(tmp_db, no_limiter):
    payload = fixture("gmgn_track_smartmoney")
    call = lambda *a, **k: payload  # noqa: E731
    assert gmgn_feeds.poll_once(Chain.SOL, "smartmoney", tmp_db, runner=call) == 2
    assert gmgn_feeds.poll_once(Chain.BSC, "smartmoney", tmp_db, runner=call) == 2
    assert gmgn_feeds.load_cursor(tmp_db, Chain.SOL, "kol")["ts_ms"] == 0


def test_alpha_feeds_emit_alpha_signal_once_per_token(tmp_db, no_limiter):
    payload = fixture("gmgn_market_trenches")
    call = lambda *a, **k: payload  # noqa: E731
    assert gmgn_feeds.poll_once(Chain.SOL, "trenches", tmp_db, runner=call) == 3
    assert gmgn_feeds.poll_once(Chain.SOL, "trenches", tmp_db, runner=call) == 0
    signals = events_of(tmp_db, EventKind.ALPHA_SIGNAL)
    assert len(signals) == 3
    payloads = [json.loads(s["payload"]) for s in signals]
    assert {p["feed"] for p in payloads} == {"trenches"}
    assert payloads[0]["smart_degen_count"] == 1
    # the score reaches the events table as a JSON number, which is what dyor reads back
    assert [p["rug_ratio"] for p in payloads] == [0.3, 0.06, 0.5]


def test_signal_feed_emits_alpha_signal(tmp_db, no_limiter):
    payload = fixture("gmgn_market_signal")
    assert gmgn_feeds.poll_once(Chain.SOL, "signal", tmp_db, runner=lambda *a, **k: payload) == 2
    signals = events_of(tmp_db, EventKind.ALPHA_SIGNAL)
    assert {json.loads(s["payload"])["label"] for s in signals} == {
        "Smart Money Net Buy", "Dev Sold",
    }


def test_error_envelope_produces_zero_rows_and_a_provider_error(tmp_db, no_limiter):
    payload = fixture("gmgn_error_envelope")
    assert gmgn_feeds.poll_once(Chain.SOL, "smartmoney", tmp_db, runner=lambda *a, **k: payload) == 0
    assert fetch_all(tmp_db, "SELECT * FROM swaps") == []
    errors = events_of(tmp_db, EventKind.PROVIDER_ERROR)
    assert len(errors) == 1
    body = json.loads(errors[0]["payload"])
    assert body["provider"] == "gmgn"
    assert body["feed"] == "smartmoney"
    assert "429" in body["error"]


def test_a_raising_wrapper_is_an_event_not_an_exception(tmp_db, no_limiter):
    def boom(*_args, **_kwargs):
        raise TimeoutError("gmgn-cli did not respond")

    assert gmgn_feeds.poll_once(Chain.SOL, "kol", tmp_db, runner=boom) == 0
    assert "TimeoutError" in json.loads(events_of(tmp_db, EventKind.PROVIDER_ERROR)[0]["payload"])["error"]


def test_missing_wrapper_is_reported_and_does_not_crash(tmp_db, no_limiter, monkeypatch):
    monkeypatch.setattr(gmgn_feeds, "gmgn_run", None)
    monkeypatch.setattr(gmgn_feeds, "_warned_missing_wrapper", False)
    assert gmgn_feeds.poll_once(Chain.SOL, "smartmoney", tmp_db) == 0
    body = json.loads(events_of(tmp_db, EventKind.PROVIDER_ERROR)[0]["payload"])
    assert "wrapper unavailable" in body["error"]


def test_poll_once_reserves_through_the_limiter(tmp_db):
    payload = fixture("gmgn_track_smartmoney")
    gmgn_feeds.poll_once(Chain.SOL, "smartmoney", tmp_db, runner=lambda *a, **k: payload)
    calls = fetch_all(tmp_db, "SELECT * FROM provider_calls")
    assert len(calls) == 1
    assert calls[0]["provider"] == "gmgn"
    assert calls[0]["endpoint"] == "track.smartmoney"
    assert calls[0]["status"] == "ok"


def test_limiter_refusal_skips_the_poll_quietly(tmp_db, monkeypatch):
    from kaiba.core.limiter import RateLimited

    @contextlib.contextmanager
    def refuse(*_args, **_kwargs):
        raise RateLimited("gmgn", "minimum interval", 1.2)
        yield  # pragma: no cover

    monkeypatch.setattr(gmgn_feeds, "guarded", refuse)
    assert gmgn_feeds.poll_once(Chain.SOL, "smartmoney", tmp_db, runner=lambda *a, **k: {}) == 0
    assert events_of(tmp_db, EventKind.PROVIDER_ERROR) == []


async def test_poll_all_sweeps_every_pair(tmp_db, no_limiter, monkeypatch):
    seen: list[tuple[str, str]] = []

    def spy(chain, feed, conn=None, **kwargs):
        seen.append((chain.value, feed))
        return 1

    monkeypatch.setattr(gmgn_feeds, "poll_once", spy)
    monkeypatch.setattr(gmgn_feeds, "_sweep_turn", 0)
    total = await gmgn_feeds.poll_all([Chain.SOL, Chain.BSC], ["smartmoney", "kol"], tmp_db)
    assert total == 4
    # FEED-MAJOR, changed deliberately 2026-09-21. This used to pin chain-major
    # ("sol" twice, then "bsc" twice). That order is what starved bsc: sol drew all five
    # of its feeds before bsc was asked for anything, and against a bucket that only
    # covered part of a sweep bsc ingested nothing for its first half hour enabled.
    # Coverage -- every pair exactly once -- is what this test is really for, and it is
    # asserted separately below so the ordering assertion cannot silently weaken it.
    assert seen == [("sol", "smartmoney"), ("bsc", "smartmoney"), ("sol", "kol"), ("bsc", "kol")]
    assert sorted(seen) == sorted(
        (c, f) for c in ("sol", "bsc") for f in ("smartmoney", "kol")
    )


def test_sweep_order_covers_every_pair_exactly_once():
    chains = [Chain.SOL, Chain.ROBINHOOD, Chain.BSC]
    feeds = ["smartmoney", "kol", "trenches"]
    for turn in range(7):
        pairs = gmgn_feeds.sweep_order(chains, feeds, turn)
        assert len(pairs) == len(chains) * len(feeds)
        assert len(set(pairs)) == len(pairs)
        assert {c for c, _ in pairs} == set(chains)


def test_sweep_order_gives_every_chain_the_lead_in_turn():
    """The starvation fix. Nobody may be permanently last.

    bsc was last in ``feed_chains()`` and therefore last in every sweep, so it only ever
    saw whatever budget sol and robinhood left behind -- which was nothing.
    """
    chains = [Chain.SOL, Chain.ROBINHOOD, Chain.BSC]
    leaders = [gmgn_feeds.sweep_order(chains, ["smartmoney"], t)[0][0] for t in range(6)]
    assert set(leaders) == set(chains), "some chain never leads a sweep"
    assert leaders == leaders[:3] * 2, "rotation should have period len(chains)"

    # and the same chain must not trail every sweep either
    trailers = [gmgn_feeds.sweep_order(chains, ["smartmoney"], t)[-1][0] for t in range(6)]
    assert set(trailers) == set(chains)


def test_sweep_order_interleaves_so_a_short_budget_reaches_every_chain():
    """Feed-major is the half of the fix that works WITHIN one sweep.

    If the bucket dies partway through, what matters is what got asked first. Under
    chain-major the first four calls are one chain's four feeds; under feed-major they
    are the top feed on every chain.
    """
    chains = [Chain.SOL, Chain.ROBINHOOD, Chain.BSC]
    pairs = gmgn_feeds.sweep_order(chains, ["smartmoney", "kol", "trenches"], 0)
    served = pairs[:3]  # a budget that dies after three calls
    assert {c for c, _ in served} == set(chains), "a short budget starved a chain"
    assert {f for _, f in served} == {"smartmoney"}, "should spend it on the top feed"


def test_sweep_order_is_empty_when_there_is_nothing_to_do():
    assert gmgn_feeds.sweep_order([], ["smartmoney"], 0) == []
    assert gmgn_feeds.sweep_order([Chain.SOL], [], 3) == []


# =======================================================================================
# Telegram — address extraction
# =======================================================================================


def test_extract_evm_address_is_lowercased():
    found = telegram_calls.extract_addresses(
        "buy 0x68EEE5c2FE8883A63CD9E5F0e71a3116FB728B3a now", Chain.ROBINHOOD
    )
    assert found == [(Chain.ROBINHOOD, "0x68eee5c2fe8883a63cd9e5f0e71a3116fb728b3a")]


def test_extract_solana_address_keeps_its_case():
    address = "Gp32UJ3xavL1sHSH9QKRygZPiQXhcoxazE4G48fu4QcA"
    assert telegram_calls.extract_addresses(f"CA: {address}") == [(Chain.SOL, address)]


def test_a_word_that_looks_base58_is_not_an_address():
    # 36 base58-legal characters, but it decodes to 26 bytes, not a 32-byte pubkey.
    assert telegram_calls.extract_addresses("SuperAmazingMoonshotTokenCandyRocket pumping") == []


@pytest.mark.parametrize(
    "program",
    [
        "So11111111111111111111111111111111111111112",
        "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
        "11111111111111111111111111111111",
        "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
        "ComputeBudget111111111111111111111111111111",
        "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
    ],
)
def test_known_program_ids_are_filtered(program):
    assert telegram_calls.extract_addresses(f"program {program} used") == []


def test_truncated_evm_hex_is_not_matched():
    assert telegram_calls.extract_addresses("0x68EEE5c2FE8883A63CD9E5F0e71a3116FB728B") == []
    # a 64-hex-char blob (a tx hash) must not yield its first 40 characters
    assert telegram_calls.extract_addresses("0x" + "ab" * 32) == []


@pytest.mark.parametrize("case", fixture("telegram_messages"), ids=lambda c: c["case"])
def test_extraction_against_recorded_messages(case):
    spec = telegram_calls.parse_channel_spec(case["channel"])
    assert spec is not None
    found = [addr for _chain, addr in telegram_calls.extract_addresses(case["text"], spec.evm_chain)]
    assert found == case["expect"]


def test_multiple_addresses_keep_order_and_dedupe():
    a = "Gp32UJ3xavL1sHSH9QKRygZPiQXhcoxazE4G48fu4QcA"
    b = "9Dk5EpBqxzjU9125iiMd4Azb1exUEAz7GBS2qQ25ejmW"
    text = f"{a} then {b} and again {a}"
    assert telegram_calls.extract_addresses(text) == [(Chain.SOL, a), (Chain.SOL, b)]


def test_extraction_handles_empty_input():
    assert telegram_calls.extract_addresses("") == []


# =======================================================================================
# Telegram — channel specs
# =======================================================================================


@pytest.mark.parametrize(
    ("spec", "chat_id", "username", "topic", "chain"),
    [
        ("@kaiba_alpha_room", None, "kaiba_alpha_room", None, Chain.ETH),
        ("-1001752354955", -1001752354955, None, None, Chain.ETH),
        ("-1001752354955/250551", -1001752354955, None, 250551, Chain.ETH),
        ("https://t.me/c/1752354955/395868", 1752354955, None, 395868, Chain.ETH),
        ("t.me/c/1752354955/250551|robinhood", 1752354955, None, 250551, Chain.ROBINHOOD),
    ],
)
def test_parse_channel_spec(spec, chat_id, username, topic, chain):
    parsed = telegram_calls.parse_channel_spec(spec)
    assert parsed is not None
    assert parsed.chat_id == chat_id
    assert parsed.username == username
    assert parsed.topic_id == topic
    assert parsed.evm_chain is chain


def test_parse_channel_spec_rejects_empty():
    assert telegram_calls.parse_channel_spec("   ") is None


def test_match_channel_handles_the_minus100_prefix_and_topics():
    specs = telegram_calls.load_channel_specs(
        ["t.me/c/1752354955/250551|robinhood", "@kaiba_alpha_room"]
    )
    hit = telegram_calls.match_channel(specs, chat_id=-1001752354955, topic_id=250551)
    assert hit is not None and hit.evm_chain is Chain.ROBINHOOD
    assert telegram_calls.match_channel(specs, chat_id=-1001752354955, topic_id=999) is None
    assert telegram_calls.match_channel(specs, username="kaiba_alpha_room") is not None
    assert telegram_calls.match_channel(specs, chat_id=-1001111111111) is None


# =======================================================================================
# Telegram — recording calls
# =======================================================================================


def test_record_call_writes_the_call_the_caller_and_the_event(tmp_db):
    token = mint("call-token-1")
    assert telegram_calls.record_call(
        tmp_db,
        caller_id=771002003,
        chain=Chain.SOL,
        token=token,
        ts_ms=1_758_355_200_000,
        display_name="degen_mike",
        channel="-1001752354955/250551",
    )
    call = fetch_one(tmp_db, "SELECT * FROM caller_calls")
    assert call["platform"] == "telegram"
    assert call["outcome"] == "pending"
    caller = fetch_one(tmp_db, "SELECT * FROM callers")
    assert caller["calls"] == 1
    assert caller["mode"] == "observe"  # a new caller is watched, never trusted
    event = events_of(tmp_db, EventKind.ALPHA_CALL)[0]
    body = json.loads(event["payload"])
    assert body["caller_mode"] == "observe"
    assert body["lane_hint"] == "kol-fade"
    assert event["subject"] == token


def test_the_same_call_twice_is_counted_once(tmp_db):
    token = mint("call-token-2")
    kwargs = dict(caller_id=42, chain=Chain.SOL, token=token, ts_ms=1_758_355_200_000)
    assert telegram_calls.record_call(tmp_db, **kwargs) is True
    assert telegram_calls.record_call(tmp_db, **kwargs) is False
    assert fetch_one(tmp_db, "SELECT * FROM callers")["calls"] == 1
    assert len(events_of(tmp_db, EventKind.ALPHA_CALL)) == 1


def test_record_message_records_every_address_in_one_post(tmp_db):
    spec = telegram_calls.parse_channel_spec("@kaiba_alpha_room")
    text = f"{mint('multi-a')} and {mint('multi-b')}"
    new = telegram_calls.record_message(
        tmp_db, text=text, caller_id=99, spec=spec, display_name="alphabot"
    )
    assert len(new) == 2
    assert len(events_of(tmp_db, EventKind.ALPHA_CALL)) == 2


# =======================================================================================
# Telegram — caller reputation
# =======================================================================================


def _stats(**kw) -> telegram_calls.CallerStats:
    base = dict(caller_id="c1", calls=12, resolved=12, wins=6, losses=6, expectancy=0.4, win_rate=0.5)
    return telegram_calls.CallerStats(**{**base, **kw})


@pytest.mark.parametrize(
    ("stats", "expected"),
    [
        (_stats(resolved=9, calls=9), "observe"),          # not enough evidence yet
        (_stats(resolved=0, calls=30, expectancy=None, win_rate=None), "observe"),
        (_stats(expectancy=0.4, win_rate=0.5), "follow"),
        (_stats(expectancy=0.4, win_rate=0.05), "observe"),  # profit came from one lucky call
        (_stats(expectancy=0.0, win_rate=0.3), "observe"),   # flat is not positive
        (_stats(expectancy=-0.6, win_rate=0.0), "fade"),
        (_stats(expectancy=-0.20, win_rate=0.0), "fade"),
        (_stats(expectancy=-0.1, win_rate=0.1), "observe"),
        (_stats(calls=40, resolved=40, expectancy=0.01, win_rate=0.1), "ignore"),
    ],
)
def test_classify_caller_thresholds(stats, expected):
    assert telegram_calls.classify_caller(stats, telegram_calls.CallerThresholds()) == expected


def test_default_posture_is_observe_even_for_a_brilliant_run_that_is_too_short():
    stats = _stats(calls=9, resolved=9, expectancy=5.0, win_rate=1.0)
    assert telegram_calls.classify_caller(stats, telegram_calls.CallerThresholds()) == "observe"


def test_thresholds_come_from_the_risk_config():
    loaded = telegram_calls.load_thresholds()
    assert loaded.min_resolved == 10          # config/risk.yaml lane kol-fade
    assert loaded.follow_expectancy == 0.0


def _seed_call_with_prices(conn, caller_id, seed, *, entry: str, peak: str, ts_ms: int) -> str:
    """One call plus the swap prints that let :func:`score_callers` resolve it."""
    token = mint(seed)
    conn.execute(
        "INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, amount_token, price_usd, source) "
        "VALUES ('sol', ?, ?, 'w', ?, 'buy', '1', ?, 'test')",
        (f"tx-entry-{seed}", ts_ms - 1000, token, entry),
    )
    conn.execute(
        "INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, amount_token, price_usd, source) "
        "VALUES ('sol', ?, ?, 'w', ?, 'sell', '1', ?, 'test')",
        (f"tx-peak-{seed}", ts_ms + 60_000, token, peak),
    )
    telegram_calls.record_call(
        conn, caller_id=caller_id, chain=Chain.SOL, token=token, ts_ms=ts_ms,
        display_name="caller", channel="@room",
    )
    return token


def test_score_callers_marks_a_caller_that_consistently_dumped_as_fade(tmp_db):
    base = now_ms() - 3 * 86_400_000
    for i in range(12):
        _seed_call_with_prices(
            tmp_db, 5001, f"fade-{i}", entry="1.0", peak="0.4", ts_ms=base + i * 3_600_000
        )
    scored = telegram_calls.score_callers(tmp_db)
    assert len(scored) == 1
    stats = scored[0]
    assert stats.resolved == 12
    assert stats.wins == 0
    assert stats.expectancy == pytest.approx(-0.6)
    assert stats.mode == "fade"
    assert fetch_one(tmp_db, "SELECT * FROM callers")["mode"] == "fade"
    resolved = fetch_all(tmp_db, "SELECT outcome, peak_x FROM caller_calls")
    assert {r["outcome"] for r in resolved} == {"loss"}  # 0.4x: down, but not a rug
    assert all(r["peak_x"] == pytest.approx(0.4) for r in resolved)


def test_score_callers_promotes_a_measurably_good_caller_to_follow(tmp_db):
    base = now_ms() - 3 * 86_400_000
    for i in range(12):
        _seed_call_with_prices(
            tmp_db, 6001, f"follow-{i}", entry="1.0", peak="3.0", ts_ms=base + i * 3_600_000
        )
    stats = telegram_calls.score_callers(tmp_db)[0]
    assert stats.wins == 12
    assert stats.expectancy == pytest.approx(2.0)
    assert stats.mode == "follow"
    changes = events_of(tmp_db, EventKind.PARAM_CHANGE)
    assert json.loads(changes[0]["payload"])["to"] == "follow"


def test_score_callers_leaves_unpriced_calls_pending(tmp_db):
    base = now_ms() - 86_400_000
    for i in range(5):
        telegram_calls.record_call(
            tmp_db, caller_id=7001, chain=Chain.SOL, token=mint(f"nopx-{i}"),
            ts_ms=base + i * 60_000, display_name="quiet", channel="@room",
        )
    stats = telegram_calls.score_callers(tmp_db)[0]
    assert stats.calls == 5
    assert stats.resolved == 0
    assert stats.pending == 5
    assert stats.expectancy is None
    assert stats.mode == "observe"  # unknown is never scored as a loss
    assert {r["outcome"] for r in fetch_all(tmp_db, "SELECT outcome FROM caller_calls")} == {"pending"}


def test_score_callers_ignores_calls_outside_the_lookback(tmp_db):
    old = now_ms() - 90 * 86_400_000
    _seed_call_with_prices(tmp_db, 8001, "ancient", entry="1.0", peak="5.0", ts_ms=old)
    assert telegram_calls.score_callers(tmp_db, lookback_days=30) == []


def test_score_callers_is_idempotent(tmp_db):
    base = now_ms() - 2 * 86_400_000
    for i in range(12):
        _seed_call_with_prices(
            tmp_db, 9001, f"idem-{i}", entry="1.0", peak="0.4", ts_ms=base + i * 3_600_000
        )
    first = telegram_calls.score_callers(tmp_db)
    second = telegram_calls.score_callers(tmp_db)
    assert first[0].expectancy == second[0].expectancy
    # the mode transition is reported once, not on every run
    assert len(events_of(tmp_db, EventKind.PARAM_CHANGE)) == 1


# =======================================================================================
# Telegram — optional dependency
# =======================================================================================


async def test_listener_skips_cleanly_without_telethon(tmp_db, monkeypatch):
    monkeypatch.setattr(telegram_calls, "telethon_available", lambda: False)
    await telegram_calls.run(asyncio.Event(), channels=["@kaiba_alpha_room"], conn=tmp_db)
    body = json.loads(events_of(tmp_db, EventKind.SYSTEM)[0]["payload"])
    assert body["status"] == "skipped"
    assert body["reason"] == "telethon missing"


async def test_listener_is_idle_without_configured_channels(tmp_db):
    await telegram_calls.run(asyncio.Event(), channels=[], conn=tmp_db)
    body = json.loads(events_of(tmp_db, EventKind.SYSTEM)[0]["payload"])
    assert body["status"] == "disabled"


def test_session_path_lives_outside_the_repo():
    """The Telethon session is a full account credential; it must never sit in the tree."""
    from kaiba.core.config import REPO_ROOT

    path = telegram_calls.session_path()
    assert path.is_absolute()
    assert not path.is_relative_to(REPO_ROOT)


# =======================================================================================
# Runner
# =======================================================================================


@pytest.fixture
def patched_runner_wait(monkeypatch):
    """Record restart delays; park the heartbeat so it does not spin the loop."""
    delays: list[float] = []

    async def fake_wait(stop, seconds):
        if seconds >= 900:  # the heartbeat interval used by these tests
            await asyncio.Event().wait()
        delays.append(seconds)
        await asyncio.sleep(0)

    monkeypatch.setattr(runner, "_wait", fake_wait)
    monkeypatch.setattr(pumpportal, "_rand", lambda: 0.0)
    return delays


async def test_run_all_restarts_a_crashing_feed_without_touching_its_sibling(
    tmp_db, patched_runner_wait
):
    calls = {"bad": 0, "good": 0}

    async def bad(stop):
        calls["bad"] += 1
        if calls["bad"] >= 3:
            stop.set()
            return
        raise RuntimeError("boom")

    async def good(stop):
        calls["good"] += 1
        await stop.wait()

    restarts = await runner.run_all(
        ["bad", "good"],
        asyncio.Event(),
        registry={"bad": bad, "good": good},
        heartbeat_s=999,
        conn=tmp_db,
    )
    assert calls == {"bad": 3, "good": 1}
    assert restarts == {"bad": 2, "good": 0}
    assert patched_runner_wait == [1, 2]  # jittered exponential backoff, jitter pinned to 0


async def test_run_all_records_status_rows(tmp_db, patched_runner_wait):
    async def feed(stop):
        stop.set()

    await runner.run_all(
        ["gmgn"], asyncio.Event(), registry={"gmgn": feed}, heartbeat_s=999, conn=tmp_db
    )
    row = fetch_one(tmp_db, "SELECT * FROM ingest_status WHERE feed='gmgn'")
    assert row["state"] == "stopped"
    assert row["restarts"] == 0


async def test_run_all_disables_a_feed_that_cannot_take_the_pumpportal_lock(
    tmp_db, patched_runner_wait
):
    attempts = {"n": 0}

    async def locked(stop):
        attempts["n"] += 1
        raise pumpportal.AlreadyRunning(Path("data/pumpportal.lock"), {"pid": 1, "host": "vps"})

    restarts = await runner.run_all(
        ["pumpportal"],
        asyncio.Event(),
        registry={"pumpportal": locked},
        heartbeat_s=999,
        conn=tmp_db,
    )
    assert attempts["n"] == 1  # never retried: a retry loop is a one-hour ban
    assert restarts == {"pumpportal": 0}
    assert fetch_one(tmp_db, "SELECT * FROM ingest_status")["state"] == "disabled"


async def test_run_all_emits_status_heartbeats(tmp_db, monkeypatch):
    beats: list[float] = []

    async def fake_wait(stop, seconds):
        beats.append(seconds)
        if len(beats) >= 3:
            stop.set()
        await asyncio.sleep(0)

    monkeypatch.setattr(runner, "_wait", fake_wait)

    async def feed(stop):
        await stop.wait()

    await runner.run_all(
        ["gmgn"], asyncio.Event(), registry={"gmgn": feed}, heartbeat_s=60, conn=tmp_db
    )
    heartbeats = [
        json.loads(e["payload"])
        for e in events_of(tmp_db, EventKind.SYSTEM)
        if json.loads(e["payload"]).get("status") == "heartbeat"
    ]
    assert heartbeats
    assert heartbeats[0]["component"] == "ingest.runner"
    assert heartbeats[0]["feeds"] == ["gmgn"]
    assert beats[0] == 60


async def test_run_all_rejects_unknown_feeds(tmp_db):
    with pytest.raises(KeyError):
        await runner.run_all(["nope"], asyncio.Event(), conn=tmp_db)


async def test_run_all_with_no_feeds_is_a_noop(tmp_db):
    assert await runner.run_all([], asyncio.Event(), conn=tmp_db) == {}


def test_registry_covers_every_listener():
    # `robinhood` is the Pons V2 listener; `rhscannerr` is a public-preview,
    # observation-only alpha feed. Both are supervised listeners.
    assert set(runner.REGISTRY) == {"pumpportal", "gmgn", "telegram", "robinhood", "rhscannerr", "rh_wallets",
                                   "sol_wallets", "flap", "alpha_feeds", "graduation_observer", "leader_exit"}
    assert set(runner.DEFAULT_FEEDS) <= set(runner.REGISTRY)


def test_public_rhscannerr_feed_is_enabled_by_default_but_not_an_execution_lane():
    assert "rhscannerr" in runner.DEFAULT_FEEDS


def test_the_telegram_listener_is_available_but_not_on_by_default():
    """Telegram's content licensing terms prohibit harvesting channel data for ML use.

    The capability stays, because it is the operator's own account and their call. What
    changed is that it is no longer switched on by a default nobody chose. See
    docs/research/12-early-alpha-airdrops-nfts-2026.md.
    """
    assert "telegram" in runner.REGISTRY
    assert "telegram" not in runner.DEFAULT_FEEDS


# ------------------------------------------ an idle listener must not hot-loop


def test_a_listener_with_nothing_to_do_goes_idle_rather_than_restarting_forever(tmp_db):
    """The telegram listener with no channels configured restarted once a second forever.

    It returned immediately, the supervisor read that as a crash, backed off by one
    second and tried again. That burns CPU, floods the log, and shows up in the operator
    view as a feed that is constantly reconnecting rather than one that is misconfigured.
    """
    import asyncio

    calls = {"n": 0}

    async def returns_immediately(stop):
        calls["n"] += 1
        return None

    stop = asyncio.Event()
    asyncio.run(
        runner._supervise("probe", lambda s: returns_immediately(s), stop, conn=tmp_db)
    )
    assert calls["n"] == runner.IDLE_RETURN_LIMIT, calls
    row = tmp_db.execute("SELECT state FROM ingest_status WHERE feed='probe'").fetchone()
    assert row["state"] == "idle"


def test_a_listener_that_runs_for_a_while_then_returns_is_still_restarted(tmp_db):
    """Only an *immediate* return means idle. A real listener that dies must come back."""
    import asyncio

    calls = {"n": 0}

    async def runs_then_returns(stop):
        calls["n"] += 1
        await asyncio.sleep((runner.IDLE_RETURN_MS + 200) / 1000)
        if calls["n"] >= 2:
            stop.set()

    stop = asyncio.Event()
    asyncio.run(runner._supervise("probe2", lambda s: runs_then_returns(s), stop, conn=tmp_db))
    assert calls["n"] >= 2


# ------------------------------------------------- triage runs on every launch


def test_a_new_launch_is_screened_by_tier_zero(tmp_db):
    """Triage was built and nothing called it. This is the wire."""
    from kaiba.execution import triage

    triage.reset_dedup_probe()
    assert pumpportal.handle_message(fixture("pumpportal_new_token"), conn=tmp_db) == \
        pumpportal.STREAM_NEW_TOKEN
    rows = fetch_all(tmp_db, "SELECT verdict FROM triage_decisions", [])
    assert rows, "the launch was ingested but never screened"


def test_a_broken_screen_never_costs_us_the_ingest_row(tmp_db, monkeypatch):
    """Knowing a token exists is worth more than knowing what we thought of it."""
    import kaiba.execution.triage as triage

    def explode(*a, **kw):
        raise RuntimeError("triage is having a bad day")

    monkeypatch.setattr(triage, "screen_launch", explode)
    assert pumpportal.handle_message(fixture("pumpportal_new_token"), conn=tmp_db) == \
        pumpportal.STREAM_NEW_TOKEN
    assert fetch_all(tmp_db, "SELECT address FROM tokens", [])


from kaiba.core.limiter import RateLimited  # noqa: E402


def test_poll_once_waits_out_a_short_limiter_hint(tmp_db, no_limiter, monkeypatch):
    """The other half of the bsc starvation fix.

    ``no_limiter`` is required, not incidental: with the real limiter the retry's own
    ``guarded()`` refuses before the runner is reached (the first call just set the
    minimum-interval clock), so the test would pass for the wrong reason -- one call and
    no retry. Bypassing it puts the runner, not the limiter, in control of the outcome.

    Every deferral observed on the live box carried "retry in 0.0-0.1s" -- the gap
    between two calls under ``min_interval_ms: 250``, not an exhausted bucket. Losing a
    feed for a whole 60 s sweep to avoid a 100 ms wait is the wrong trade.
    """
    calls: list[int] = []
    slept: list[float] = []

    def flaky(group, command, **flags):
        calls.append(1)
        if len(calls) == 1:
            raise RateLimited("gmgn", "minimum interval", 0.05)
        return {"data": []}

    monkeypatch.setattr(gmgn_feeds.time, "sleep", lambda s: slept.append(s))
    written = gmgn_feeds.poll_once(
        Chain.SOL, "smartmoney", tmp_db, runner=flaky, max_wait_s=0.3
    )
    assert len(calls) == 2, "should have retried once after waiting"
    assert slept == [0.05], "should wait exactly the hint the limiter gave"
    assert written == 0
    assert events_of(tmp_db, EventKind.PROVIDER_ERROR) == []


def test_poll_once_does_not_wait_out_a_long_hint(tmp_db, monkeypatch):
    """A long hint means the bucket is genuinely empty. Yield the slot, do not sleep."""
    calls: list[int] = []
    slept: list[float] = []

    def always_limited(group, command, **flags):
        calls.append(1)
        raise RateLimited("gmgn", "bucket empty", 47.0)

    monkeypatch.setattr(gmgn_feeds.time, "sleep", lambda s: slept.append(s))
    assert gmgn_feeds.poll_once(
        Chain.SOL, "smartmoney", tmp_db, runner=always_limited, max_wait_s=0.3
    ) == 0
    assert len(calls) == 1, "must not retry a hint longer than the budget"
    assert slept == [], "must not sleep"


def test_poll_once_defaults_to_never_waiting(tmp_db, monkeypatch):
    """A caller that has not thought about its time budget keeps fail-fast behaviour."""
    calls: list[int] = []
    slept: list[float] = []

    def limited(group, command, **flags):
        calls.append(1)
        raise RateLimited("gmgn", "minimum interval", 0.01)

    monkeypatch.setattr(gmgn_feeds.time, "sleep", lambda s: slept.append(s))
    assert gmgn_feeds.poll_once(Chain.SOL, "smartmoney", tmp_db, runner=limited) == 0
    assert len(calls) == 1 and slept == []


def test_a_retry_that_also_fails_is_not_a_provider_error(tmp_db, no_limiter, monkeypatch):
    """Being rate limited twice is backpressure, not an outage. It must not page."""
    def always(group, command, **flags):
        raise RateLimited("gmgn", "minimum interval", 0.01)

    monkeypatch.setattr(gmgn_feeds.time, "sleep", lambda s: None)
    assert gmgn_feeds.poll_once(
        Chain.SOL, "smartmoney", tmp_db, runner=always, max_wait_s=0.3
    ) == 0
    assert events_of(tmp_db, EventKind.PROVIDER_ERROR) == []


def test_a_retry_that_hits_a_real_failure_is_recorded(tmp_db, no_limiter, monkeypatch):
    """But a genuine provider failure on the second attempt must still be reported."""
    state: list[int] = []

    def limited_then_broken(group, command, **flags):
        state.append(1)
        if len(state) == 1:
            raise RateLimited("gmgn", "minimum interval", 0.01)
        raise RuntimeError("upstream exploded")

    monkeypatch.setattr(gmgn_feeds.time, "sleep", lambda s: None)
    assert gmgn_feeds.poll_once(
        Chain.SOL, "smartmoney", tmp_db, runner=limited_then_broken, max_wait_s=0.3
    ) == 0
    assert events_of(tmp_db, EventKind.PROVIDER_ERROR), "a real failure must be recorded"


async def test_poll_all_paces_itself_to_the_provider_minimum_interval(tmp_db, no_limiter, monkeypatch):
    """The actual bsc bug: not out of budget, out of SPACING.

    MEASURED 2026-09-21 on the live box: gmgn was running at 0.40 req/s against a
    4.0 req/s ceiling while every bsc feed was refused on every sweep. ``poll_all``
    fired all fifteen pairs inside ~150 ms and ``min_interval_ms: 250`` bounced
    everything after the first, so the tail of the iteration was never asked at all.
    """
    waits: list[float] = []

    async def spy_wait(stop, seconds):
        waits.append(seconds)

    monkeypatch.setattr(gmgn_feeds, "_wait", spy_wait)
    monkeypatch.setattr(gmgn_feeds, "pace_s", lambda: 0.25)
    monkeypatch.setattr(gmgn_feeds, "poll_once", lambda chain, feed, conn=None, **kw: 1)
    monkeypatch.setattr(gmgn_feeds, "_sweep_turn", 0)

    total = await gmgn_feeds.poll_all(
        [Chain.SOL, Chain.BSC], ["smartmoney", "kol"], tmp_db
    )
    assert total == 4
    # one gap BETWEEN each pair of calls, never before the first and never after the last
    assert waits == [0.25, 0.25, 0.25], f"expected 3 gaps between 4 calls, got {waits}"


async def test_poll_all_pacing_is_derived_from_the_limiter_not_hardcoded(tmp_db, no_limiter, monkeypatch):
    """Raising or lowering the gmgn budget must move the pacing with it."""
    waits: list[float] = []

    async def spy_wait(stop, seconds):
        waits.append(seconds)

    monkeypatch.setattr(gmgn_feeds, "_wait", spy_wait)
    monkeypatch.setattr(gmgn_feeds, "poll_once", lambda chain, feed, conn=None, **kw: 0)
    monkeypatch.setattr(gmgn_feeds, "_sweep_turn", 0)
    monkeypatch.setattr(gmgn_feeds, "pace_s", lambda: 1.5)
    await gmgn_feeds.poll_all([Chain.SOL, Chain.BSC], ["smartmoney"], tmp_db)
    assert waits == [1.5]


async def test_poll_all_does_not_pace_when_the_budget_has_no_minimum(tmp_db, no_limiter, monkeypatch):
    """A provider with no minimum interval must not be slowed down for nothing."""
    waits: list[float] = []

    async def spy_wait(stop, seconds):
        waits.append(seconds)

    monkeypatch.setattr(gmgn_feeds, "_wait", spy_wait)
    monkeypatch.setattr(gmgn_feeds, "pace_s", lambda: 0.0)
    monkeypatch.setattr(gmgn_feeds, "poll_once", lambda chain, feed, conn=None, **kw: 0)
    monkeypatch.setattr(gmgn_feeds, "_sweep_turn", 0)
    await gmgn_feeds.poll_all([Chain.SOL, Chain.BSC], ["smartmoney", "kol"], tmp_db)
    assert waits == []


async def test_a_stop_during_the_pace_gap_ends_the_sweep(tmp_db, no_limiter, monkeypatch):
    """Pacing must not delay a shutdown by the length of a whole sweep."""
    stop = asyncio.Event()
    polled: list[str] = []

    async def stopping_wait(_stop, _seconds):
        stop.set()

    monkeypatch.setattr(gmgn_feeds, "_wait", stopping_wait)
    monkeypatch.setattr(gmgn_feeds, "pace_s", lambda: 0.25)
    monkeypatch.setattr(
        gmgn_feeds, "poll_once",
        lambda chain, feed, conn=None, **kw: (polled.append(f"{chain.value}/{feed}"), 1)[1],
    )
    monkeypatch.setattr(gmgn_feeds, "_sweep_turn", 0)
    total = await gmgn_feeds.poll_all(
        [Chain.SOL, Chain.BSC], ["smartmoney", "kol"], tmp_db, stop=stop
    )
    assert total == 1, "should have stopped in the first gap, after exactly one poll"
    assert len(polled) == 1


def test_pace_s_tracks_the_configured_budget(monkeypatch):
    """Added after a mutation survived: the pacing tests above all mock ``pace_s``.

    That made them blind to the exact regression the docstring warns about -- replacing
    the limiter lookup with a hardcoded 0.25. This tests the function itself, so a pace
    that stops following the gmgn budget now fails.
    """
    from kaiba.core import limiter

    seen: list[str] = []

    def fake_limits_for(provider):
        seen.append(provider)
        return limiter.Limits(min_interval_ms=1750)

    monkeypatch.setattr(limiter, "limits_for", fake_limits_for)
    assert gmgn_feeds.pace_s() == 1.75
    assert seen == [gmgn_feeds.PROVIDER], "must ask for the gmgn budget specifically"


def test_pace_s_is_zero_when_the_budget_has_no_minimum(monkeypatch):
    from kaiba.core import limiter

    monkeypatch.setattr(limiter, "limits_for", lambda p: limiter.Limits(min_interval_ms=0))
    assert gmgn_feeds.pace_s() == 0.0


def test_pace_s_survives_an_unreadable_budget(monkeypatch):
    """An unreadable budget must not stop the sweep; it just runs unpaced."""
    from kaiba.core import limiter

    def boom(provider):
        raise RuntimeError("config on fire")

    monkeypatch.setattr(limiter, "limits_for", boom)
    assert gmgn_feeds.pace_s() == 0.0


# --------------------------------------------------------------------------------------
# the GMGN trade-feed parser mismatch found 2026-09-21
# --------------------------------------------------------------------------------------

#: One row exactly as `gmgn-cli track smartmoney --chain sol --raw` returns it, captured
#: live 2026-09-21. Every field name here is the vendor's, not ours.
_LIVE_SMARTMONEY_ROW = {
    "amount_usd": "170.84808276074",
    "balance": 0,
    "base_address": "2c1weSacxYkxF5xAFSzdYQXoFp3XcjimXyaGSioTnpRs",
    "base_amount": "597992.750742",
    "base_token": {"symbol": "POPEYE", "logo": "https://example.invalid/x.png"},
    "buy_cost_usd": 0,
    "is_open_or_close": 0,
    "maker": "5Q7sU7RaR6XNcztFRzDgyDRod2CPmiuq8apJp6DpjsUM",
    "maker_info": {"avatar": "", "name": "", "tags": ["smart_degen", "arbitrager"]},
    "price": "2.45998448839838e-06",
    "price_usd": "0.00028570259848258785",
    "quote_amount": "1.471052891",
    "side": "buy",
    "timestamp": 1789983069,
    "token_amount": "597992.750742",
    "transaction_hash": "Qys5D2N6mndVHMibo8V46FHU6Rr2c81gv2fGuWc5YRmvoPfVkon2TS61E1P6w4QPz4Vp22",
}


def test_the_live_smartmoney_shape_parses():
    """FOUND 2026-09-21: this returned an empty list, and had been doing so all along.

    ``_parse_trades`` required ``tx``, ``wallet``, ``token`` and ``side`` together and
    skipped the row otherwise. It looked for ``tx_hash`` and ``token_address``; GMGN
    sends ``transaction_hash`` and ``base_address``. Two key names, and the entire paid
    smart-money and KOL tape was discarded on every chain -- 100 rows per call, parsed
    to zero, silently, because a skipped row is not an error.
    """
    rows = gmgn_feeds.parse_smartmoney([_LIVE_SMARTMONEY_ROW], Chain.SOL)
    assert len(rows) == 1, "the live vendor shape must parse"
    row = rows[0]
    assert row.token == "2c1weSacxYkxF5xAFSzdYQXoFp3XcjimXyaGSioTnpRs"
    assert row.wallet == "5Q7sU7RaR6XNcztFRzDgyDRod2CPmiuq8apJp6DpjsUM"
    assert row.tx.startswith("Qys5D2N6")
    assert row.side == "buy"


def test_the_nested_fields_are_not_dropped():
    """symbol, wallet name and cohort tags are all one level down in the live shape.

    The tags matter most: ``smart_degen``/``arbitrager`` are the cohort evidence the
    wallet grader and the confluence lanes read, so losing them loses the reason the
    wallet was worth following at all.
    """
    row = gmgn_feeds.parse_smartmoney([_LIVE_SMARTMONEY_ROW], Chain.SOL)[0]
    assert row.token_symbol == "POPEYE", "symbol lives at base_token.symbol"
    assert row.tags == ["smart_degen", "arbitrager"], "tags live at maker_info.tags"


def test_kol_uses_the_same_shape():
    assert len(gmgn_feeds.parse_kol([_LIVE_SMARTMONEY_ROW], Chain.BSC)) == 1


@pytest.mark.parametrize("chain", [Chain.SOL, Chain.BSC, Chain.ROBINHOOD])
def test_the_trade_feeds_parse_on_every_enabled_chain(chain):
    """Verified live on all three 2026-09-21: 100 raw -> 100 parsed, 100 tagged."""
    rows = gmgn_feeds.parse_smartmoney([_LIVE_SMARTMONEY_ROW], chain)
    assert len(rows) == 1 and rows[0].tags


def test_a_row_missing_its_identifiers_is_still_skipped():
    """The skip is correct behaviour and must survive the alias widening."""
    junk = {k: v for k, v in _LIVE_SMARTMONEY_ROW.items()
            if k not in ("transaction_hash", "base_address")}
    assert gmgn_feeds.parse_smartmoney([junk], Chain.SOL) == []
