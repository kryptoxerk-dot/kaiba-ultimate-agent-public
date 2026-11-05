"""Design 2026-10-21 steps 4a and 4b: the smart-flow feeder matches the lane, its reads are
off the scan thread and never scan a table, a slow pass is abandoned, and one chain cannot
take every scan second.

MEASURED on the box before this (2026-10-21): the smart SET was two full table scans (1.93M
``wallets``, 1.09M ``wallet_scores``) on the scan thread -- 9.4 GB read in 13 minutes after a
restart with no batch finished, and ``kaiba scan run`` the most-behind WAL reader in 35
db-guard reports. Over 24 h, 62 sol passes over 120 s took 45,360 s, 76% of all sol scan time.

All addresses are synthetic.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from kaiba.core.db import fetch_one, jload, tx
from kaiba.core.schemas import Chain, now_ms
from kaiba.execution import lanes as lanes_mod
from kaiba.execution import scanner as S
from kaiba.execution import triage as T

RH_TOKEN = "0x" + "a1" * 20
BSC_TOKEN = "0x" + "b2" * 20
SOL_TOKEN = "ErR5nqFX3XVtzYvoeTqHQHqjFb6Zb4WkUGE5dWkwpump"


def wallet(conn, address: str, chain: Chain, tags: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO wallets (chain, address, first_seen_ms, last_seen_ms, tags_json) VALUES (?,?,?,?,?)",
        (chain.value, address, now_ms(), now_ms(), tags),
    )


def buy(conn, address: str, token: str, chain: Chain, age_s: int = 10, side: str = "buy") -> None:
    conn.execute(
        "INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, amount_token, usd_value, source) "
        "VALUES (?,?,?,?,?,?,'1000','500','gmgn:smartmoney')",
        (chain.value, f"tx_{address}_{token}_{age_s}_{side}", now_ms() - age_s * 1000, address, token, side),
    )


def two_bare_and_one_vendor(conn, chain: Chain, token: str, vendor_tags: str = '["gmgn:smart_degen"]') -> None:
    for i in (1, 2):
        a = "0x" + f"{i:02d}" * 20
        wallet(conn, a, chain, '["smart_money"]')
        buy(conn, a, token, chain)
    v = "0x" + "77" * 20
    wallet(conn, v, chain, vendor_tags)
    buy(conn, v, token, chain)
    S._SMART_SET_CACHE.clear()


def offered(conn) -> list[tuple[str, str, int]]:
    return [(i.chain.value, i.token, i.extras["smart_buyers"])
            for i in S._smart_flow_work(conn, 10, config=S.DEFAULT_CONFIG)]


# ------------------------------------------------------------------ 4a: the lane's rule


def test_a_robinhood_vendor_labelled_buyer_completes_the_feeders_bar(tmp_db):
    """Two bare-tagged buyers plus one ``gmgn:smart_degen`` on robinhood is the lane's three."""
    two_bare_and_one_vendor(tmp_db, Chain.ROBINHOOD, RH_TOKEN)
    assert Chain.ROBINHOOD in lanes_mod.SMART_COHORT_LABEL_CHAINS
    assert offered(tmp_db) == [("robinhood", RH_TOKEN, 3)]


def test_the_vendor_label_does_not_count_off_the_lanes_label_chains(tmp_db):
    two_bare_and_one_vendor(tmp_db, Chain.BSC, BSC_TOKEN)
    assert Chain.BSC not in lanes_mod.SMART_COHORT_LABEL_CHAINS
    assert offered(tmp_db) == []


def test_the_feeder_reads_the_label_chains_from_the_lane(tmp_db, monkeypatch):
    two_bare_and_one_vendor(tmp_db, Chain.BSC, BSC_TOKEN)
    monkeypatch.setattr(lanes_mod, "SMART_COHORT_LABEL_CHAINS", frozenset({Chain.BSC}))
    assert offered(tmp_db) == [("bsc", BSC_TOKEN, 3)]


def test_a_vendor_label_on_a_quarantined_bot_does_not_count(tmp_db):
    """The lane's own exception: smart AND a sandwich bot is a sandwich bot."""
    two_bare_and_one_vendor(tmp_db, Chain.ROBINHOOD, RH_TOKEN,
                            vendor_tags='["gmgn:smart_degen", "gmgn:sandwich_bot"]')
    assert offered(tmp_db) == []


def test_the_feeder_agrees_with_the_lane_wallet_by_wallet():
    """Every route, on and off the label chains, read the way ``sm_trenches`` reads it."""
    rule = S._smart_by_lane_rule
    assert rule("sol", '["smart_money"]', None, None, None)
    assert rule("robinhood", '["gmgn:app_smart_money"]', None, None, None)
    assert not rule("bsc", '["gmgn:launchpad_smart"]', None, None, None)
    assert rule("robinhood", "[]", lanes_mod.TRUSTED_COPY_COHORT, None, None)
    assert not rule("sol", "[]", lanes_mod.TRUSTED_COPY_COHORT, None, None)
    assert rule("bsc", "[]", None, "top_trader", "B")
    assert not rule("sol", '["smart_money"]', "blacklist", None, None)
    assert not rule("sol", '["smart_money"]', None, "smart_money", "QUARANTINED")
    assert not rule("sol", '["Smart_Money"]', None, None, None)  # the lane's set match is exact


# ------------------------------------------------------------------ 4b: off the scan thread


def test_under_run_loop_the_tape_is_refreshed_only_on_its_own_thread(tmp_db, monkeypatch):
    """The scan thread never runs the smart-flow reads (design 4b: assert the thread name)."""
    two_bare_and_one_vendor(tmp_db, Chain.ROBINHOOD, RH_TOKEN)
    scanned: list[str] = []

    def fake_scan(chain, token, conn=None, **kw):  # noqa: ANN001, ANN003
        scanned.append(token)
        return S.ScanResult(chain=chain, token=token, source=kw.get("source", ""), ok=True, ts_ms=now_ms())

    monkeypatch.setattr(S, "scan", fake_scan)
    cfg = replace(S.DEFAULT_CONFIG, smart_tape_refresh_s=0.05, idle_sleep_s=0.02, copy_flow=False,
                  migration_max_age_s=0, db_queue=False, scan_timeout_s=0)
    S.run_loop(tmp_db, queue=T.TriageQueue(name="empty"), config=cfg, max_seconds=1.5)
    tape = S._SMART_SET_CACHE[S._db_key(tmp_db)]
    assert tape.refreshes >= 1
    assert tape.refreshed_on == {"kaiba-smart-set"}, tape.refreshed_on
    assert RH_TOKEN in scanned  # and the scan thread still got the candidate


def test_without_a_tape_thread_the_feeder_refreshes_inline(tmp_db):
    """A one-shot caller (``scan once``, the tests) has no thread and still gets smart flow."""
    two_bare_and_one_vendor(tmp_db, Chain.ROBINHOOD, RH_TOKEN)
    assert offered(tmp_db) == [("robinhood", RH_TOKEN, 3)]
    assert S._SMART_SET_CACHE[S._db_key(tmp_db)].refreshed_on == {threading.current_thread().name}


def test_a_live_tape_thread_is_not_raced_by_the_scan_thread(tmp_db):
    two_bare_and_one_vendor(tmp_db, Chain.ROBINHOOD, RH_TOKEN)
    tape = S._smart_tape(tmp_db)
    tape.heartbeat = time.monotonic()  # a tape thread reported just now
    assert offered(tmp_db) == []  # nothing read yet, and the scan thread did not read it
    assert tape.refreshes == 0


def _spy_lookups(monkeypatch, *, slow_calls: int = 0, sleep_s: float = 0.0) -> list[int]:
    """Record how many wallets each classification statement names; the first ``slow_calls``
    statements also take ``sleep_s`` (a cold disk)."""
    sizes: list[int] = []
    real = S._lookup

    def spy(conn, sql, args):  # noqa: ANN001
        sizes.append(len(jload(args[1], [])))
        if len(sizes) <= slow_calls:
            time.sleep(sleep_s)
        return real(conn, sql, args)

    monkeypatch.setattr(S, "_lookup", spy)
    return sizes


def test_no_classification_statement_names_more_than_fifty_wallets(tmp_db, monkeypatch):
    """MEASURED on the box 2026-10-23: one 500-address ``wallets`` lookup held its autocommit
    snapshot >= 67.2 s cold (53 ms warm), past the 60 s read rule; 50 cold addresses took
    3.1-10.6 s. Driven through the tape's own refresh, with 120 distinct window buyers."""
    sizes = _spy_lookups(monkeypatch)
    for i in range(120):
        buy(tmp_db, f"Buyer{i:04d}" + "1" * 30, SOL_TOKEN, Chain.SOL)
    tape = S.SmartTape()
    assert tape.refresh(tmp_db, window_s=1800, ttl_s=600)
    assert len(tape.verdicts) == 120
    assert sizes and max(sizes) <= 50, sizes
    assert sum(sizes) == 2 * 120  # every buyer looked up in both tables, none twice


def test_a_slow_lookup_halves_the_chunk_for_the_rest_of_the_pass(tmp_db, monkeypatch):
    """The disk can be slower than the one measured: a statement over the budget shortens the
    next ones instead of letting the snapshot grow with it."""
    monkeypatch.setattr(S, "SMART_LOOKUP_BUDGET_S", 0.1)
    sizes = _spy_lookups(monkeypatch, slow_calls=2, sleep_s=0.25)  # the first wallet+score pair
    tape = S.SmartTape()
    out = tape._classify(tmp_db, [("sol", f"W{i:04d}") for i in range(120)], ttl_s=60)
    assert len(out) == 120
    assert sizes == [50, 50, 25, 25, 25, 25, 20, 20], sizes
    assert tape.lookup_halvings == 1 and tape.lookup_slowest_s >= 0.25


def test_a_disk_that_stays_slow_halves_down_to_the_floor_not_to_zero(tmp_db, monkeypatch):
    monkeypatch.setattr(S, "SMART_LOOKUP_BUDGET_S", 0.01)
    sizes = _spy_lookups(monkeypatch, slow_calls=10_000, sleep_s=0.02)
    out = S.SmartTape()._classify(tmp_db, [("sol", f"W{i:04d}") for i in range(110)], ttl_s=60)
    assert len(out) == 110
    per_statement = sizes[::2]
    assert per_statement[:4] == [50, 25, 12, 6], per_statement
    assert all(n == S.SMART_LOOKUP_MIN_CHUNK for n in per_statement[4:-1]), per_statement
    assert sum(per_statement) == 110


def test_a_slow_wallet_scores_statement_alone_halves_the_chunk(tmp_db, monkeypatch):
    """``wallet_scores`` is the slow table on the box (10.62 s MEASURED for 50 addresses). The
    budget must see the SECOND statement of each pair, not only the first."""
    monkeypatch.setattr(S, "SMART_LOOKUP_BUDGET_S", 0.1)
    sizes: list[int] = []
    slowed: list[int] = []
    real = S._lookup

    def spy(conn, sql, args):  # noqa: ANN001
        sizes.append(len(jload(args[1], [])))
        if sql == S.SMART_SCORE_LOOKUP_SQL and not slowed:
            slowed.append(1)
            time.sleep(0.25)
        return real(conn, sql, args)

    monkeypatch.setattr(S, "_lookup", spy)
    tape = S.SmartTape()
    assert len(tape._classify(tmp_db, [("sol", f"W{i:04d}") for i in range(120)], ttl_s=60)) == 120
    assert sizes == [50, 50, 25, 25, 25, 25, 20, 20], sizes
    assert tape.lookup_halvings == 1 and tape.lookup_slowest_s >= 0.25


def test_smart_wallets_selling_a_token_are_not_counted_as_buying_it(tmp_db):
    """Three smart wallets on a token, one of them SELLING: two buyers, under the lane's three."""
    smart = [f"Smart{i}" + "1" * 38 for i in range(3)]
    for a in smart:
        wallet(tmp_db, a, Chain.SOL, '["smart_money"]')
    buy(tmp_db, smart[0], SOL_TOKEN, Chain.SOL)
    buy(tmp_db, smart[1], SOL_TOKEN, Chain.SOL)
    buy(tmp_db, smart[2], SOL_TOKEN, Chain.SOL, side="sell")
    S._SMART_SET_CACHE.clear()
    assert offered(tmp_db) == []
    assert {b[3] for b in S._SMART_SET_CACHE[S._db_key(tmp_db)].buys} == set(smart[:2])
    buy(tmp_db, smart[2], SOL_TOKEN, Chain.SOL, age_s=5)  # the third now buys: the positive control
    assert offered(tmp_db) == [("sol", SOL_TOKEN, 3)]


class _FailsOnTailChunk:
    """A connection whose ``n``-th tape tail chunk raises (a locked or interrupted read)."""

    def __init__(self, conn: sqlite3.Connection, n: int) -> None:
        self.conn, self.n, self.seen = conn, n, 0

    def execute(self, sql: str, *args: Any) -> Any:
        if sql == S.SMART_TAPE_TAIL_SQL:
            self.seen += 1
            if self.seen == self.n:
                raise sqlite3.OperationalError("database is locked")
        return self.conn.execute(sql, *args)


def test_a_tail_chunk_that_raises_leaves_the_tape_where_it_was(tmp_db, monkeypatch):
    """The cursor used to move with each chunk read; when a later one raised, the rows of the
    earlier ones were never appended and never read again."""
    monkeypatch.setattr(S, "SMART_TAPE_CHUNK", 2)
    tape = S.SmartTape()
    assert tape.refresh(tmp_db, window_s=1800, ttl_s=600)  # the first pass: nothing yet
    start = tape.last_id
    for i in range(5):
        buy(tmp_db, f"Buyer{i:04d}" + "1" * 30, SOL_TOKEN, Chain.SOL)
    with pytest.raises(sqlite3.OperationalError):
        tape.refresh(_FailsOnTailChunk(tmp_db, 2), window_s=1800, ttl_s=600)  # type: ignore[arg-type]
    assert tape.last_id == start and not tape.buys
    assert tape.refresh(tmp_db, window_s=1800, ttl_s=600)
    assert len(tape.buys) == 5


def test_the_tape_reads_only_new_rows_after_the_first_pass(tmp_db):
    two_bare_and_one_vendor(tmp_db, Chain.ROBINHOOD, RH_TOKEN)
    offered(tmp_db)
    tape = S._SMART_SET_CACHE[S._db_key(tmp_db)]
    first_id = tape.last_id
    seen: list[str] = []
    tmp_db.set_trace_callback(seen.append)
    try:
        offered(tmp_db)
    finally:
        tmp_db.set_trace_callback(None)
    reads = [s for s in seen if "FROM swaps WHERE id >" in s]
    assert reads and all(f"id > {first_id}" in s for s in reads), reads
    assert not any("FROM wallets w" in s for s in seen)  # verdicts reused inside the TTL


# ------------------------------------------------------------------ 4b: the scan timeout


def _slow_and_fast(monkeypatch, *, slow: str, sleep_s: float, late_write: bool = False) -> None:
    def fake_build_context(chain, token, conn, **kw):  # noqa: ANN001, ANN003
        if token == slow:
            time.sleep(sleep_s)
            if late_write:  # what a pass that outlived its timeout would do next: write
                conn.execute("INSERT INTO kv (key, value, updated_ms) VALUES ('late_write', '1', 0)")
        raise RuntimeError("stub context: no lanes needed")

    monkeypatch.setattr(S, "build_context", fake_build_context)


def test_a_pass_past_the_timeout_is_abandoned_and_the_next_chains_token_is_scanned(tmp_db, monkeypatch):
    _slow_and_fast(monkeypatch, slow=SOL_TOKEN, sleep_s=3.0)
    cfg = replace(S.DEFAULT_CONFIG, scan_timeout_s=0.3)
    started = time.monotonic()
    results = S.run_once(tmp_db, config=cfg, tokens=[(Chain.SOL, SOL_TOKEN), (Chain.ROBINHOOD, RH_TOKEN)])
    elapsed = time.monotonic() - started
    assert elapsed < 2.0, elapsed
    assert [r.token for r in results] == [SOL_TOKEN, RH_TOKEN]
    assert results[0].ok is False and (results[0].error or "").startswith("timeout")
    assert results[1].error and "stub context" in results[1].error  # it ran, on a fresh worker
    row = fetch_one(tmp_db, "SELECT payload FROM events WHERE kind=? AND subject=?", (S.EVENT_SCAN_FAILED, SOL_TOKEN))
    assert row is not None and jload(row["payload"], {}).get("reason") == "timeout"


def test_an_abandoned_pass_cannot_write_after_its_timeout(tmp_db, monkeypatch):
    _slow_and_fast(monkeypatch, slow=SOL_TOKEN, sleep_s=0.8, late_write=True)
    cfg = replace(S.DEFAULT_CONFIG, scan_timeout_s=0.2)
    S.run_once(tmp_db, config=cfg, tokens=[(Chain.SOL, SOL_TOKEN)])
    for w in list(S._ABANDONED):
        w.thread.join(timeout=5.0)
    assert fetch_one(tmp_db, "SELECT 1 FROM kv WHERE key='late_write'") is None


LONG_SELECT = "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n) SELECT max(i) FROM n"


def _cancel_soon(conn, ev: threading.Event, after_s: float = 0.2) -> threading.Thread:  # noqa: ANN001
    """What ``_ScanWorker.cancel`` does, from another thread: set the flag, then interrupt."""
    def go() -> None:
        time.sleep(after_s)
        ev.set()
        conn.interrupt()

    t = threading.Thread(target=go)
    t.start()
    return t


def _cancel_between_statements(conn, ev) -> None:  # noqa: ANN001
    with tx(conn):
        conn.execute("INSERT INTO kv (key, value, updated_ms) VALUES ('w1', '1', 0)")
        ev.set()
        conn.execute("INSERT INTO kv (key, value, updated_ms) VALUES ('w2', '1', 0)")


def _cancel_during_a_select(conn, ev) -> None:  # noqa: ANN001
    """SQLite rolls back an interrupted INSERT's transaction on its own; not a SELECT's."""
    with tx(conn):
        conn.execute("INSERT INTO kv (key, value, updated_ms) VALUES ('w1', '1', 0)")
        t = _cancel_soon(conn, ev)
        try:
            conn.execute(LONG_SELECT).fetchall()
        finally:
            t.join()


def _cancel_before_the_commit(conn, ev) -> None:  # noqa: ANN001
    with tx(conn):
        conn.execute("INSERT INTO kv (key, value, updated_ms) VALUES ('w1', '1', 0)")
        ev.set()  # the body is done; tx() is about to COMMIT


def _cancel_inside_a_savepoint(conn, ev) -> None:  # noqa: ANN001
    with tx(conn):
        conn.execute("INSERT INTO kv (key, value, updated_ms) VALUES ('w1', '1', 0)")
        with tx(conn):
            ev.set()
            conn.execute("INSERT INTO kv (key, value, updated_ms) VALUES ('w2', '1', 0)")


def _cancel_with_a_cursor_held_open(conn, ev) -> None:  # noqa: ANN001
    """A cursor left mid-iteration keeps a statement active, so SQLite keeps the interrupt
    flag set and the ROLLBACK itself comes back ``interrupted``."""
    conn.executemany("INSERT INTO kv (key, value, updated_ms) VALUES (?, '0', 0)", [(f"k{i}",) for i in range(50)])
    with tx(conn):
        held = conn.execute("SELECT key FROM kv")
        held.fetchone()
        conn.execute("INSERT INTO kv (key, value, updated_ms) VALUES ('w1', '1', 0)")
        ev.set()
        conn.interrupt()
        conn.execute("INSERT INTO kv (key, value, updated_ms) VALUES ('w2', '1', 0)")


@pytest.mark.parametrize("body", [_cancel_between_statements, _cancel_during_a_select, _cancel_before_the_commit,
                                  _cancel_inside_a_savepoint, _cancel_with_a_cursor_held_open],
                         ids=lambda f: f.__name__.removeprefix("_"))
def test_a_cancel_inside_a_transaction_rolls_it_back_and_frees_the_write_lock(tmp_db, body):
    """Review 2026-10-23: the refusal also refused tx()'s own ROLLBACK / RELEASE / COMMIT, so a
    cancel inside ``with tx(conn)`` left BEGIN IMMEDIATE open -- every other writer got
    ``database is locked`` and a TRUNCATE checkpoint came back busy -- until the pass ended."""
    path = Path(S._db_key(tmp_db))
    ev = threading.Event()
    conn = S._cancellable_connect(path, ev)
    try:
        with pytest.raises(sqlite3.OperationalError):
            body(conn, ev)
        assert not conn.in_transaction
        other = sqlite3.connect(path, timeout=0.3, isolation_level=None)
        try:
            other.execute("BEGIN IMMEDIATE")  # 'database is locked' while the pass holds it
            other.execute("ROLLBACK")
            assert tuple(other.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone())[0] == 0  # not busy
            assert other.execute("SELECT COUNT(*) FROM kv WHERE value='1'").fetchone()[0] == 0  # nothing committed
        finally:
            other.close()
        with pytest.raises(sqlite3.OperationalError, match="abandoned"):
            conn.execute("SELECT 1")  # and still refused afterwards
    finally:
        conn.close()


def test_an_abandoned_pass_that_carries_on_does_not_hold_the_write_lock(tmp_db, monkeypatch):
    """The real shape: the timeout lands inside a transaction, a broad ``except`` carries the
    pass on, and it then sits in a network wait nothing can interrupt. The lock must be free
    during that wait, not after the pass returns."""
    in_wait = threading.Event()
    release = threading.Event()

    def fake_build_context(chain, token, conn, **kw):  # noqa: ANN001, ANN003
        try:
            with tx(conn):
                conn.execute("INSERT INTO kv (key, value, updated_ms) VALUES ('in_tx', '1', 0)")
                time.sleep(0.6)  # the 0.2 s timeout lands here
                conn.execute("INSERT INTO kv (key, value, updated_ms) VALUES ('in_tx_2', '1', 0)")
        except sqlite3.OperationalError:
            pass  # what dedup / bundles / store_dossier's handlers do
        in_wait.set()
        release.wait(30.0)  # an in-flight HTTP call, released by the test once it has looked
        raise RuntimeError("stub context: no lanes needed")

    monkeypatch.setattr(S, "build_context", fake_build_context)
    path = Path(S._db_key(tmp_db))
    cfg = replace(S.DEFAULT_CONFIG, scan_timeout_s=0.2)
    # The loop runs beside the test, as on the box, so the lock is read WHILE the pass waits:
    # the loop's own writes after the timeout would otherwise sit out busy_timeout (10 s) until
    # the pass gave up and closed its connection, which hides the leak.
    loop = threading.Thread(target=S.run_once, args=(tmp_db,), kwargs={"config": cfg,
                            "tokens": [(Chain.SOL, SOL_TOKEN)]}, daemon=True)
    loop.start()
    try:
        assert in_wait.wait(5.0)
        other = sqlite3.connect(path, timeout=0.3, isolation_level=None)
        try:
            other.execute("BEGIN IMMEDIATE")  # 'database is locked' while the pass holds it
            other.execute("ROLLBACK")
            assert other.execute("SELECT COUNT(*) FROM kv WHERE key LIKE 'in_tx%'").fetchone()[0] == 0
        finally:
            other.close()
    finally:
        release.set()
        loop.join(timeout=30.0)
        for w in list(S._ABANDONED):
            w.thread.join(timeout=5.0)


def test_an_abandoned_pass_inside_a_long_statement_is_interrupted_and_its_thread_exits(tmp_db, monkeypatch):
    """The refusal only stops the NEXT statement; the one running when the timeout lands is
    stopped by ``interrupt()``. Without it a slow read keeps its thread, its connection and its
    WAL snapshot for as long as the statement runs (here: forever)."""
    started = threading.Event()

    def long_scan(chain, token, conn=None, **kw):  # noqa: ANN001, ANN003
        started.set()
        conn.execute(LONG_SELECT).fetchall()  # unbounded recursion: ends only when interrupted
        raise AssertionError("the statement ended on its own")

    monkeypatch.setattr(S, "scan", long_scan)
    before = list(S._ABANDONED)
    item = S.WorkItem(chain=Chain.SOL, token=SOL_TOKEN, source="token")
    result = S._bounded_scan(tmp_db, item, config=replace(S.DEFAULT_CONFIG, scan_timeout_s=0.3), pool={})
    assert started.is_set() and result.ok is False and (result.error or "").startswith("timeout")
    [worker] = [w for w in S._ABANDONED if w not in before]
    try:
        worker.thread.join(timeout=3.0)
        assert not worker.thread.is_alive(), "the abandoned statement kept running"
    finally:
        if worker.thread.is_alive() and worker.conn is not None:  # a mutant run: stop the spin
            sqlite3.Connection.interrupt(worker.conn)
            worker.thread.join(timeout=5.0)


def test_a_worker_that_opens_after_the_ready_wait_exits_instead_of_waiting_forever(tmp_db, monkeypatch):
    """``_new_worker`` gives up on a slow open and the batch scans inline; the worker's thread,
    once its connection does open, must close it and exit, not sit on ``jobs.get()``."""
    gate = threading.Event()
    real_connect = S._cancellable_connect

    def slow_connect(path, cancelled):  # noqa: ANN001
        gate.wait(10.0)
        return real_connect(path, cancelled)

    made: list[S._ScanWorker] = []

    class Recorded(S._ScanWorker):
        def __init__(self, path: str) -> None:
            super().__init__(path)
            made.append(self)

    monkeypatch.setattr(S, "_cancellable_connect", slow_connect)
    monkeypatch.setattr(S, "SCAN_WORKER_READY_S", 0.1)
    monkeypatch.setattr(S, "_ScanWorker", Recorded)
    assert S._new_worker(S._db_key(tmp_db)) is None
    [worker] = made
    gate.set()
    try:
        worker.thread.join(timeout=3.0)
        assert not worker.thread.is_alive(), "the late worker is waiting for a job that will never come"
    finally:
        worker.jobs.put(None)  # a mutant run: let it go
        worker.thread.join(timeout=5.0)


def test_a_timed_out_token_waits_out_its_own_cooldown(tmp_db, monkeypatch):
    _slow_and_fast(monkeypatch, slow=SOL_TOKEN, sleep_s=1.0)
    cfg = replace(S.DEFAULT_CONFIG, scan_timeout_s=0.2, rescan_cooldown_s=0)
    S.run_once(tmp_db, config=cfg, tokens=[(Chain.SOL, SOL_TOKEN)])
    queue = T.TriageQueue(capacity=8, name="t")
    queue.offer(T.TriageDecision(chain=Chain.SOL, token=SOL_TOKEN, verdict=T.Verdict.PROMOTE, score=0.9,
                                 ts_ms=now_ms()))
    assert S.next_work(tmp_db, 4, queue=queue, config=cfg) == []
    queue.offer(T.TriageDecision(chain=Chain.SOL, token=SOL_TOKEN, verdict=T.Verdict.PROMOTE, score=0.9,
                                 ts_ms=now_ms()))
    assert [i.token for i in S.next_work(tmp_db, 4, queue=queue,
                                         config=replace(cfg, scan_timeout_cooldown_s=0))] == [SOL_TOKEN]


def test_an_in_memory_database_scans_inline_as_before():
    """No file to reopen: the pass runs on the caller's connection, unbounded, as it did."""
    import sqlite3

    conn = sqlite3.connect(":memory:")
    calls: list[Any] = []
    item = S.WorkItem(chain=Chain.SOL, token=SOL_TOKEN, source="token")
    orig = S.scan
    try:
        S.scan = lambda chain, token, c=None, **kw: calls.append(c) or "ran"  # type: ignore[assignment]
        assert S._bounded_scan(conn, item, config=S.DEFAULT_CONFIG, pool={}) == "ran"
    finally:
        S.scan = orig  # type: ignore[assignment]
        conn.close()
    assert calls == [conn]


# ------------------------------------------------------------------ 4b: per-chain fair share


def _item(chain: Chain, token: str, source: str = "smart_flow") -> S.WorkItem:
    return S.WorkItem(chain=chain, token=token, source=source)


def _spend(db: str, chain: Chain, seconds: float) -> None:
    S._note_scan_seconds(db, [S.ScanResult(chain=chain, token="x", elapsed_ms=int(seconds * 1000))],
                         window_s=1800.0)


def test_a_chain_over_its_share_keeps_one_item_and_the_others_go_first(tmp_db):
    db = S._db_key(tmp_db)
    _spend(db, Chain.SOL, 900.0)
    _spend(db, Chain.ROBINHOOD, 30.0)
    items = [_item(Chain.SOL, f"sol{i}") for i in range(5)] + [_item(Chain.ROBINHOOD, RH_TOKEN),
                                                                _item(Chain.BSC, BSC_TOKEN)]
    out = S._fair_share(items, db, config=S.DEFAULT_CONFIG)
    assert [i.token for i in out] == [BSC_TOKEN, RH_TOKEN, "sol0"]


def test_urgent_items_are_never_capped_or_moved(tmp_db):
    db = S._db_key(tmp_db)
    _spend(db, Chain.SOL, 900.0)
    items = [_item(Chain.SOL, "mig", "migration"), _item(Chain.SOL, "sol1"), _item(Chain.SOL, "sol2"),
             _item(Chain.ROBINHOOD, RH_TOKEN)]
    out = S._fair_share(items, db, config=S.DEFAULT_CONFIG)
    assert [i.token for i in out] == ["mig", RH_TOKEN, "sol1"]


def test_a_chain_alone_in_the_batch_is_never_capped(tmp_db):
    db = S._db_key(tmp_db)
    _spend(db, Chain.SOL, 900.0)
    items = [_item(Chain.SOL, f"sol{i}") for i in range(4)]
    assert S._fair_share(items, db, config=S.DEFAULT_CONFIG) == items


def test_the_share_is_measured_from_real_passes(tmp_db, monkeypatch):
    _slow_and_fast(monkeypatch, slow=SOL_TOKEN, sleep_s=0.3)
    cfg = replace(S.DEFAULT_CONFIG, scan_timeout_s=5.0)
    S.run_once(tmp_db, config=cfg, tokens=[(Chain.SOL, SOL_TOKEN), (Chain.ROBINHOOD, RH_TOKEN)])
    shares = S.chain_scan_shares(S._db_key(tmp_db), window_s=1800.0)
    assert shares["sol"] > 0.6 > shares.get("robinhood", 0.0)


# ------------------------------------------------------------------ the swap-count read


@pytest.mark.parametrize("created_ms, want", [(None, "creation_time_unknown"), (0, None)])
def test_the_full_count_runs_only_when_the_coverage_can_pass(tmp_db, created_ms, want):
    buy(tmp_db, "0x" + "05" * 20, SOL_TOKEN, Chain.SOL)
    seen: list[str] = []
    tmp_db.set_trace_callback(seen.append)
    try:
        n, basis = S.swap_count_if_covered(Chain.SOL, SOL_TOKEN, tmp_db, created_ms=created_ms)
    finally:
        tmp_db.set_trace_callback(None)
    assert n is None
    if want is not None:
        assert basis == want
    else:
        assert basis.startswith("coverage_starts_"), basis
    assert not any("COUNT(*)" in s for s in seen), seen


def test_a_covered_token_still_gets_its_count(tmp_db):
    buy(tmp_db, "0x" + "05" * 20, SOL_TOKEN, Chain.SOL, age_s=10)
    buy(tmp_db, "0x" + "06" * 20, SOL_TOKEN, Chain.SOL, age_s=5)
    n, basis = S.swap_count_if_covered(Chain.SOL, SOL_TOKEN, tmp_db, created_ms=now_ms() - 20_000)
    assert (n, basis) == (2, "covered_from_launch")
