"""Design 2026-10-21 step 4c: the Robinhood snipe reads the Pons poller's rows when there is
no WebSocket, instead of redialling one that cannot exist.

MEASURED on the box 2026-10-21: ``snipe._pons`` redialled ``wss://`` on the public RPC once a
minute (``HTTP 400``, ``last_block None``) from 10-09 to 10-21, so the lane observed no
Robinhood launch for 12 days, while the poller wrote 33 Pons rows in 30 minutes at a p50 of
4.1 s after the block. Addresses are synthetic.
"""

from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace

import pytest

from kaiba.core.db import fetch_all, jdump
from kaiba.core.schemas import Chain, Lane
from kaiba.execution import snipe
from kaiba.ingest import launch_feed as lf
from kaiba.ingest import robinhood as rh
from kaiba.ingest.robinhood import FACTORY_V2, TOPIC_TOKEN_LAUNCHED

needs_lane = pytest.mark.skipif(snipe.LANE_VALUE not in {x.value for x in Lane}, reason="Lane 'launch-snipe' not wired")

TOKEN = "0x9122cbc7a76c8518989aae029148d331538dadb7"
CURVE = "0x5575479424e114fafa0cd314115c7897a7a2441b"
DEPLOYER = "0x1a4e077c1abd3fe3674516aeff53ab400e92add7"
TS = 1_791_000_000


def _word(n: int) -> str:
    return format(n, "064x")


def _topic(a: str) -> str:
    return "0x" + a.removeprefix("0x").rjust(64, "0")


def launched_log(*, token: str = TOKEN, curve: str = CURVE, ts: int = TS, block: int = 1000) -> dict:
    return {
        "address": FACTORY_V2,
        "topics": [TOPIC_TOKEN_LAUNCHED, _topic(token), _topic(curve), _topic(DEPLOYER)],
        "data": "0x" + "0" * 64 + _word(7) + _word(4 * 10**18),
        "blockNumber": hex(block),
        "transactionHash": "0x" + "ab" * 32,
        "logIndex": hex(3),
        "blockTimestamp": hex(ts),
    }


def poller_writes(conn, entry: dict) -> None:
    """Exactly what the Pons poller persists for a launch (``robinhood.record_new_token``)."""
    token, _meta = rh.parse_token_launched(entry, ts_ms=int(entry["blockTimestamp"], 16) * 1000)
    rh.record_new_token(token, conn)


def foreign_row(conn, address: str, created_ms: int, *, source: str = "gmgn:signal") -> None:
    conn.execute(
        "INSERT INTO tokens (chain, address, creator, created_ms, launchpad, first_seen_ms, meta_json) "
        "VALUES ('robinhood',?,?,?,'pons',?,?)",
        (address, DEPLOYER, created_ms, created_ms + 60_000, jdump({"source": source, "curve": CURVE})),
    )


def poller_row_on(conn, chain: Chain, entry: dict) -> None:
    """The poller's exact row shape and meta, but stored under another chain."""
    token, _meta = rh.parse_token_launched(entry, ts_ms=int(entry["blockTimestamp"], 16) * 1000)
    conn.execute(
        "INSERT INTO tokens (chain, address, creator, created_ms, launchpad, first_seen_ms, meta_json) "
        "VALUES (?,?,?,?,?,?,?)",
        (chain.value, token.address, token.creator, token.created_ms, token.launchpad, token.created_ms,
         jdump(token.meta)),
    )


def _addr(n: int) -> str:
    return "0x" + format(n, "040x")


def poller_at(conn, n: int, ts: int) -> str:
    """A poller row for synthetic token ``n`` launched at block second ``ts``; its address."""
    poller_writes(conn, launched_log(token=_addr(0xE000 + n), curve=_addr(0xF000 + n), ts=ts, block=2000 + n))
    return _addr(0xE000 + n)


# ------------------------------------------------------------------------- the tail


def test_the_tail_hands_out_the_pollers_row_once_shaped_like_the_socket_launch(tmp_db):
    poller_writes(tmp_db, launched_log())
    mark = lf.RhWatermark(created_ms=TS * 1000 - 5_000)
    first = lf.tail_rh(tmp_db, mark, now_ms=TS * 1000 + 4_100)
    pushed = lf.launch_from_log(launched_log(), received_ms=TS * 1000 + 4_100)
    assert [x.token for x in first] == [TOKEN]
    got = first[0]
    for f in ("chain", "token", "venue", "creator", "curve", "pair_token", "graduation_threshold", "block",
              "launched_ms", "tx", "received_ms"):
        assert getattr(got, f) == getattr(pushed, f), f
    assert got.quote_is_native and got.latency_ms == 4_100 and not got.backfilled
    assert lf.tail_rh(tmp_db, mark, now_ms=TS * 1000 + 5_000) == []  # never twice


def test_rows_from_any_other_source_are_ignored(tmp_db):
    foreign_row(tmp_db, "0x" + "c1" * 20, TS * 1000)
    poller_writes(tmp_db, launched_log())
    mark = lf.RhWatermark(created_ms=TS * 1000 - 5_000)
    assert [x.token for x in lf.tail_rh(tmp_db, mark, now_ms=TS * 1000 + 1_000)] == [TOKEN]


def test_a_row_the_poller_rewrites_after_another_source_is_then_read(tmp_db):
    """GMGN first (not a launch), then the poller's upsert of the same row: handed out then."""
    foreign_row(tmp_db, TOKEN, TS * 1000)
    mark = lf.RhWatermark(created_ms=TS * 1000 - 5_000)
    assert lf.tail_rh(tmp_db, mark, now_ms=TS * 1000 + 1_000) == []
    poller_writes(tmp_db, launched_log())
    assert [x.token for x in lf.tail_rh(tmp_db, mark, now_ms=TS * 1000 + 2_000)] == [TOKEN]


def test_a_row_written_late_for_an_earlier_second_is_still_found(tmp_db):
    poller_writes(tmp_db, launched_log(token="0x" + "d1" * 20, ts=TS + 5))
    mark = lf.RhWatermark(created_ms=TS * 1000 - 5_000)
    assert len(lf.tail_rh(tmp_db, mark, now_ms=(TS + 6) * 1000)) == 1
    poller_writes(tmp_db, launched_log(token="0x" + "d2" * 20, ts=TS + 3))  # same block run, written after
    assert [x.token for x in lf.tail_rh(tmp_db, mark, now_ms=(TS + 7) * 1000)] == ["0x" + "d2" * 20]


def test_the_tail_reads_through_the_created_index_not_every_robinhood_token(tmp_db):
    plan = " ".join(str(r["detail"]) for r in fetch_all(tmp_db, "EXPLAIN QUERY PLAN " + lf.RH_TAIL_SQL,
                                                        (0, "robinhood", "pons", 5)))
    assert "idx_tokens_created" in plan and "idx_tokens_creator" not in plan, plan


def test_a_pons_row_on_another_chain_is_never_a_robinhood_launch(tmp_db):
    """The chain predicate is the only thing that keeps another chain's ``launchpad='pons'``
    row out: the row parser trusts the meta, and it would come back as a Robinhood launch."""
    other = "0x" + "b5" * 20
    poller_row_on(tmp_db, Chain.BASE, launched_log(token=other, curve="0x" + "b6" * 20, ts=TS + 1))
    poller_writes(tmp_db, launched_log())
    mark = lf.RhWatermark(created_ms=TS * 1000 - 5_000)
    assert [x.token for x in lf.tail_rh(tmp_db, mark, now_ms=(TS + 2) * 1000)] == [TOKEN]
    assert mark.created_ms == TS * 1000


# ------------------------------------------------------------------------- the mark


def test_the_mark_moves_to_the_newest_poller_row_and_no_further(tmp_db):
    """Not left where it started (the read would then grow until the page filled), and not
    moved by a foreign row ahead of the poller (the next read floor would pass the poller)."""
    poller_writes(tmp_db, launched_log())
    poller_at(tmp_db, 1, TS + 40)
    foreign_row(tmp_db, "0x" + "c7" * 20, (TS + 300) * 1000)
    mark = lf.RhWatermark(created_ms=TS * 1000 - 5_000)
    assert len(lf.tail_rh(tmp_db, mark, now_ms=(TS + 301) * 1000)) == 2
    assert mark.created_ms == (TS + 40) * 1000


def test_a_foreign_row_ahead_of_the_poller_does_not_skip_its_catch_up_rows(tmp_db):
    """Review 2026-10-23. The poller lags (box p90 249 s, max 1,316 s) and a GMGN row for a
    later launch can land first. The mark used to move to it; the next read floor (its
    ``created_ms`` - 30 s) then sat above the rows the poller wrote for the launches between."""
    poller_writes(tmp_db, launched_log())
    mark = lf.RhWatermark(created_ms=TS * 1000 - 5_000)
    assert [x.token for x in lf.tail_rh(tmp_db, mark, now_ms=TS * 1000 + 4_000)] == [TOKEN]
    foreign_row(tmp_db, "0x" + "c1" * 20, (TS + 300) * 1000)
    assert lf.tail_rh(tmp_db, mark, now_ms=(TS + 301) * 1000) == []
    caught_up = [poller_at(tmp_db, 1, TS + 120), poller_at(tmp_db, 2, TS + 250)]
    assert [x.token for x in lf.tail_rh(tmp_db, mark, now_ms=(TS + 310) * 1000)] == caught_up
    assert mark.created_ms == (TS + 250) * 1000


def test_every_launch_is_handed_out_once_across_full_pages(tmp_db):
    """A page of ``limit`` rows is read and the next starts behind its newest row: the mark
    has to move, or the same page comes back forever (in production, after 500 launches)."""
    tokens = [poller_at(tmp_db, i, TS + 40 * i) for i in range(7)]  # each further apart than the overlap
    mark = lf.RhWatermark(created_ms=TS * 1000 - 5_000)
    got: list[str] = []
    for _ in range(10):
        got += [x.token for x in lf.tail_rh(tmp_db, mark, limit=3, now_ms=(TS + 400) * 1000)]
    assert got == tokens


def test_a_full_page_of_foreign_rows_is_paged_past_not_reread_forever(tmp_db):
    """Foreign rows move no mark, so ``limit`` of them ahead of a stopped poller would fill
    every page; the poller's next row sits behind them and would never be read."""
    mark = lf.RhWatermark(created_ms=TS * 1000)
    for i in range(4):
        foreign_row(tmp_db, _addr(0xD000 + i), (TS + 40 * (i + 1)) * 1000)
    resumed = poller_at(tmp_db, 9, TS + 200)
    got: list[str] = []
    for _ in range(5):
        got += [x.token for x in lf.tail_rh(tmp_db, mark, limit=3, now_ms=(TS + 210) * 1000)]
    assert got == [resumed]


# ------------------------------------------------------------------------- the start


def test_a_restart_while_the_poller_lags_still_reads_the_backlog_it_writes_next(tmp_db):
    """Review 2026-10-23. Started at wall-clock "now", the first read floor was now - 30 s,
    above every launch the lagging poller had not written yet, though they were well inside
    the 900 s stale bound. Started at the poller's newest row, they are read as they land."""
    poller_writes(tmp_db, launched_log())  # the poller's newest row at the restart
    restart = (TS + 249) * 1000  # the box's p90 poller lag
    mark = lf.rh_start_mark(tmp_db, now_ms=restart)
    assert mark.created_ms == TS * 1000
    assert [x.token for x in lf.tail_rh(tmp_db, mark, now_ms=restart)] == [TOKEN]  # the overlap, re-read
    backlog = [poller_at(tmp_db, 1, TS + 100), poller_at(tmp_db, 2, TS + 200)]
    assert [x.token for x in lf.tail_rh(tmp_db, mark, now_ms=restart + 5_000)] == backlog


@needs_lane
def test_the_overlap_a_restart_re_reads_is_recorded_once(tmp_db):
    """What makes re-reading the overlap safe: the observation is INSERT OR IGNORE."""
    from kaiba.execution.snipe import Verdict

    poller_writes(tmp_db, launched_log())
    [first] = lf.tail_rh(tmp_db, lf.RhWatermark(created_ms=TS * 1000 - 5_000), now_ms=TS * 1000 + 4_000)
    snipe.record_observation(tmp_db, first, Verdict(False, ["no_alpha"]), {"basis": "curve_unread"})
    restarted = lf.rh_start_mark(tmp_db, now_ms=TS * 1000 + 60_000)
    [again] = lf.tail_rh(tmp_db, restarted, now_ms=TS * 1000 + 61_000)
    snipe.record_observation(tmp_db, again, Verdict(False, ["no_alpha"]), {"basis": "curve_unread"})
    rows = fetch_all(tmp_db, f"SELECT seen_ms FROM {snipe.TABLE} WHERE token=?", (TOKEN,))
    assert rows == [{"seen_ms": TS * 1000 + 4_000}]


def test_the_start_is_the_pollers_own_newest_row(tmp_db):
    poller_writes(tmp_db, launched_log())
    foreign_row(tmp_db, "0x" + "c2" * 20, (TS + 200) * 1000)  # GMGN, ahead of the poller
    poller_row_on(tmp_db, Chain.BASE, launched_log(token="0x" + "b7" * 20, curve="0x" + "b8" * 20, ts=TS + 220))
    assert lf.rh_start_mark(tmp_db, now_ms=(TS + 249) * 1000).created_ms == TS * 1000


def test_the_start_reaches_back_no_further_than_the_stale_bound(tmp_db):
    at = (TS + 2_000) * 1000
    assert lf.rh_start_mark(tmp_db, now_ms=at).created_ms == at - lf.RH_START_MAX_BACKLOG_MS  # no poller row
    poller_writes(tmp_db, launched_log())
    assert lf.rh_start_mark(tmp_db, now_ms=at, max_backlog_ms=900_000).created_ms == at - 900_000  # too old
    assert lf.rh_start_mark(tmp_db, now_ms=at, max_backlog_ms=3_000_000).created_ms == TS * 1000


def test_the_start_is_never_later_than_now_less_the_lookback(tmp_db):
    poller_writes(tmp_db, launched_log())
    behind = TS * 1000 - 10_000  # a box clock behind the block's
    assert lf.rh_start_mark(tmp_db, now_ms=behind).created_ms == behind
    assert lf.rh_start_mark(tmp_db, now_ms=behind, lookback_ms=5_000).created_ms == behind - 5_000
    assert lf.rh_start_mark(now_ms=behind, lookback_ms=5_000).created_ms == behind - 5_000  # no conn: as before


def test_the_start_reads_the_created_index_newest_first_without_a_sort(tmp_db):
    plan = " ".join(str(r["detail"]) for r in fetch_all(tmp_db, "EXPLAIN QUERY PLAN " + lf.RH_START_SQL,
                                                        (0, "robinhood", "pons", 5)))
    assert "idx_tokens_created" in plan and "idx_tokens_creator" not in plan and "TEMP B-TREE" not in plan, plan


@pytest.mark.parametrize("url, want", [
    ("https://rpc.mainnet.chain.robinhood.com", False),   # MEASURED: wss 400, /ws 404
    ("https://robinhood-mainnet.g.alchemy.com/v2/KEY", True),
    ("wss://example.invalid/ws", True),
    ("", False),
    (None, False),
    ("not a url", False),
])
def test_only_a_known_websocket_endpoint_is_dialled(url, want):
    assert lf.pons_websocket_available(url) is want


# ------------------------------------------------------------------------- the sniper


def _sniper(tmp_db, monkeypatch, url: str):
    sniper = snipe.Sniper(tmp_db, chains=["robinhood"])
    monkeypatch.setattr(snipe, "get_settings", lambda: SimpleNamespace(rpc_for=lambda chain: url))
    return sniper


@needs_lane
def test_without_a_websocket_the_sniper_tails_the_poller_and_never_dials(tmp_db, monkeypatch):
    sniper = _sniper(tmp_db, monkeypatch, "https://rpc.mainnet.chain.robinhood.com")

    def no_socket(*a, **k):  # noqa: ANN002, ANN003
        raise AssertionError("dialled a WebSocket the endpoint does not have")

    monkeypatch.setattr(lf, "stream_pons", no_socket)
    spawned: list[str] = []
    monkeypatch.setattr(sniper, "spawn", lambda launch: spawned.append(launch.token) or object())
    monkeypatch.setattr(lf, "rh_start_mark", lambda **k: lf.RhWatermark(created_ms=TS * 1000 - 5_000))
    monkeypatch.setattr(lf.aws, "_now_ms", lambda: TS * 1000 + 4_000)
    poller_writes(tmp_db, launched_log())

    async def run() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(sniper._pons(stop))
        for _ in range(50):
            await asyncio.sleep(0.02)
            if spawned:
                break
        stop.set()
        await asyncio.wait_for(task, timeout=3.0)

    asyncio.run(run())
    assert spawned == [TOKEN]
    assert sniper.stats.get("rh_tail") == 1


def _no_socket(*a, **k):  # noqa: ANN002, ANN003
    raise AssertionError("dialled a WebSocket the endpoint does not have")


@needs_lane
def test_a_restarted_sniper_reads_the_backlog_the_lagging_poller_writes_next(tmp_db, monkeypatch):
    """End to end through ``_pons``: the tail starts at the poller's newest row, so the row the
    poller writes after the restart for an earlier launch is still handed out."""
    sniper = _sniper(tmp_db, monkeypatch, "https://rpc.mainnet.chain.robinhood.com")
    monkeypatch.setattr(lf, "stream_pons", _no_socket)
    spawned: list[str] = []
    monkeypatch.setattr(sniper, "spawn", lambda launch: spawned.append(launch.token) or object())
    monkeypatch.setattr(lf.aws, "_now_ms", lambda: (TS + 249) * 1000)  # the box's p90 poller lag
    poller_writes(tmp_db, launched_log())
    backlog: list[str] = []

    async def run() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(sniper._pons(stop))
        try:
            for _ in range(200):
                await asyncio.sleep(0.02)
                if TOKEN in spawned and not backlog:
                    backlog.append(poller_at(tmp_db, 1, TS + 100))  # written after the restart
                if backlog and backlog[0] in spawned:
                    break
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=3.0)

    asyncio.run(run())
    assert spawned == [TOKEN, *backlog] and backlog


@needs_lane
def test_the_sniper_starts_no_further_back_than_its_own_stale_bound(tmp_db, monkeypatch):
    sniper = _sniper(tmp_db, monkeypatch, "https://rpc.mainnet.chain.robinhood.com")
    sniper.p = {**sniper.p, "rh_tail_max_launch_age_s": 600}
    at = (TS + 2_000) * 1000
    assert sniper.pons_tail_start(at_ms=at).created_ms == at - 600_000
    poller_at(tmp_db, 1, TS + 1_500)  # 500 s back: inside the bound
    assert sniper.pons_tail_start(at_ms=at).created_ms == (TS + 1_500) * 1000


@needs_lane
def test_a_locked_database_at_the_start_is_retried_not_replaced_by_now(tmp_db, monkeypatch):
    sniper = _sniper(tmp_db, monkeypatch, "https://rpc.mainnet.chain.robinhood.com")
    real = lf.rh_start_mark
    calls: list[int] = []

    def flaky(conn=None, **k):  # noqa: ANN001, ANN003
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(conn, **k)

    monkeypatch.setattr(lf, "rh_start_mark", flaky)
    monkeypatch.setattr(lf.aws, "_now_ms", lambda: (TS + 249) * 1000)
    spawned: list[str] = []
    monkeypatch.setattr(sniper, "spawn", lambda launch: spawned.append(launch.token) or object())
    poller_writes(tmp_db, launched_log())

    async def run() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(sniper._pons_tail(stop, poll_s=0.02))
        try:
            for _ in range(150):
                await asyncio.sleep(0.02)
                if spawned:
                    break
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=3.0)

    asyncio.run(run())
    assert spawned == [TOKEN] and len(calls) == 2


@needs_lane
def test_with_a_websocket_the_sniper_still_streams(tmp_db, monkeypatch):
    sniper = _sniper(tmp_db, monkeypatch, "https://robinhood-mainnet.g.alchemy.com/v2/KEY")
    streamed: list[str] = []

    async def fake_stream(url, *, stop=None, **k):  # noqa: ANN001, ANN003
        streamed.append(url)
        yield lf.launch_from_log(launched_log(), received_ms=TS * 1000 + 400)

    monkeypatch.setattr(lf, "stream_pons", fake_stream)
    monkeypatch.setattr(sniper, "_pons_tail", lambda stop: (_ for _ in ()).throw(AssertionError("tailed")))
    spawned: list[str] = []
    monkeypatch.setattr(sniper, "spawn", lambda launch: spawned.append(launch.token) or object())
    asyncio.run(sniper._pons(asyncio.Event()))
    assert streamed and spawned == [TOKEN]


@needs_lane
def test_a_launch_older_than_the_tail_bound_is_counted_not_handled(tmp_db, monkeypatch):
    sniper = _sniper(tmp_db, monkeypatch, "https://rpc.mainnet.chain.robinhood.com")
    spawned: list[str] = []
    monkeypatch.setattr(sniper, "spawn", lambda launch: spawned.append(launch.token) or object())
    poller_writes(tmp_db, launched_log())
    mark = lf.RhWatermark(created_ms=TS * 1000 - 5_000)
    max_age = int(sniper.p["rh_tail_max_launch_age_s"])
    assert sniper.pons_tail_once(mark, at_ms=TS * 1000 + max_age * 1000 + 1) == 0
    assert spawned == [] and sniper.stats["rh_tail_stale"] == 1


@needs_lane
def test_robinhood_has_its_own_dossier_budget_by_default(tmp_db):
    assert snipe.DEFAULT_PARAMS["dossier_budget_by_chain"].get("robinhood")
    sniper = snipe.Sniper(tmp_db, chains=[])
    sniper.p = {**sniper.p, "dossier_budget_by_chain": dict(snipe.DEFAULT_PARAMS["dossier_budget_by_chain"])}
    assert sniper.budget_for(Chain.ROBINHOOD) is not sniper.budget
    assert sniper.budget_for(Chain.SOL) is sniper.budget


@needs_lane
def test_the_rh_feed_alive_gate_sees_tailed_launches(tmp_db, monkeypatch):
    """Step 0's mark gate keys on recorded observations, so a tailed launch keeps marks on."""
    from kaiba.execution.snipe import Verdict

    poller_writes(tmp_db, launched_log())
    [launch] = lf.tail_rh(tmp_db, lf.RhWatermark(created_ms=TS * 1000 - 5_000), now_ms=TS * 1000 + 4_000)
    snipe.record_observation(tmp_db, launch, Verdict(False, ["no_alpha"]), {"basis": "curve_unread"})
    assert snipe.rh_feed_alive(tmp_db, at_ms=TS * 1000 + 60_000)


def _tailed_launches(conn, n: int) -> None:
    for i in range(n):
        poller_writes(conn, launched_log(token="0x" + format(0xA000 + i, "040x"), curve="0x" + format(0xC000 + i, "040x")))


@needs_lane
def test_the_hourly_cap_ships_on_at_fifty_and_holds_back_the_rest(tmp_db, monkeypatch):
    """ON by default (review 2026-10-23): each handled tail launch costs ~4.3 robinhood-rpc
    requests plus its marks, on a host already answering 429s that land on protection's own
    reads. Read off DEFAULT_PARAMS, not the ambient risk file, so the shipped default is what
    is tested; then the repo's risk file must not switch it off."""
    assert snipe.DEFAULT_PARAMS["rh_tail_max_per_hour"] == 50
    sniper = _sniper(tmp_db, monkeypatch, "https://rpc.mainnet.chain.robinhood.com")
    sniper.p = dict(snipe.DEFAULT_PARAMS)
    spawned: list[str] = []
    monkeypatch.setattr(sniper, "spawn", lambda launch: spawned.append(launch.token) or object())
    _tailed_launches(tmp_db, 52)
    mark = lf.RhWatermark(created_ms=TS * 1000 - 5_000)
    assert sniper.pons_tail_once(mark, at_ms=TS * 1000 + 4_000) == 50
    assert len(spawned) == 50 and sniper.stats["rh_tail_capped"] == 2
    assert snipe.params().get("rh_tail_max_per_hour") == 50


@needs_lane
def test_a_zero_cap_handles_every_fresh_launch(tmp_db, monkeypatch):
    sniper = _sniper(tmp_db, monkeypatch, "https://rpc.mainnet.chain.robinhood.com")
    sniper.p = {**snipe.DEFAULT_PARAMS, "rh_tail_max_per_hour": 0}
    spawned: list[str] = []
    monkeypatch.setattr(sniper, "spawn", lambda launch: spawned.append(launch.token) or object())
    _tailed_launches(tmp_db, 52)
    assert sniper.pons_tail_once(lf.RhWatermark(created_ms=TS * 1000 - 5_000), at_ms=TS * 1000 + 4_000) == 52
    assert "rh_tail_capped" not in sniper.stats


@needs_lane
def test_the_hourly_cap_counts_what_it_holds_back(tmp_db, monkeypatch):
    """Launches past the cap in a rolling hour are counted, not handled."""
    sniper = _sniper(tmp_db, monkeypatch, "https://rpc.mainnet.chain.robinhood.com")
    spawned: list[str] = []
    monkeypatch.setattr(sniper, "spawn", lambda launch: spawned.append(launch.token) or object())
    for i in range(3):
        poller_writes(tmp_db, launched_log(token="0x" + f"{i + 40:02d}" * 20, curve="0x" + f"{i + 60:02d}" * 20))
    mark = lf.RhWatermark(created_ms=TS * 1000 - 5_000)
    sniper.p = {**sniper.p, "rh_tail_max_per_hour": 2}
    assert sniper.pons_tail_once(mark, at_ms=TS * 1000 + 4_000) == 2
    assert len(spawned) == 2 and sniper.stats["rh_tail_capped"] == 1
