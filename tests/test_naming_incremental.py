"""The incremental namer: only wallets whose evidence moved, bounded, resumable.

The full pass (a GROUP BY over every swap plus a LIKE over every wallet.trade event) had not
finished inside its 300 s timeout since 2026-09-24. These tests hold the replacement to
three things: it writes exactly what the full pass would have written for the wallets it
names; it names every wallet whose evidence moved, eventually, without losing one to a
cursor; and it stops where it is told to.
"""

from __future__ import annotations

from typing import Any

import pytest

from kaiba.core.db import fetch_all, fetch_one, jload
from kaiba.core.schemas import Chain, now_ms
from kaiba.ingest import gmgn_feeds
from kaiba.intelligence import feed_tags, naming
from kaiba.ops import scheduler as S

A = "Bnd5oBSWpPpXoaWTckgXTVx9TCKkX5GyVqxcrwBzUYsB"
B = "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"
C = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
D = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
E = "3KmBrSmVYz3GtGJtxsyF2hS6r8Zad5hjHjvAqCFNVpwE"
EVM = "0x68eee5c2fe8883a63cd9e5f0e71a3116fb728b3a"
TOKEN_1 = "So11111111111111111111111111111111111111112"
TOKEN_2 = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def swap(conn: Any, chain: str, wallet: str, i: int, *, side: str = "buy", source: str = "pumpfun:trades",
         ts: int | None = None) -> None:
    conn.execute(
        "INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, amount_token, source) VALUES (?,?,?,?,?,?,?,?)",
        (chain, f"tx-{wallet[:6]}-{i}-{side}", ts if ts is not None else 1_000 + i, wallet,
         TOKEN_1 if i % 2 else TOKEN_2, side, str(i + 1), source),
    )


def feed(conn: Any, chain: Chain, wallet: str, tx: str, tags: list[str], *, kind: str = "smartmoney") -> None:
    gmgn_feeds.write_swap(conn, gmgn_feeds.SwapRow(
        chain=chain, tx=tx, ts_ms=now_ms(), wallet=wallet, token=TOKEN_1, side="buy", amount_token="1",
        feed=kind, tags=tags))


def seed(conn: Any) -> None:
    for i in range(5):
        swap(conn, "sol", A, i)
    for i in range(12):
        swap(conn, "sol", B, i, side="sell")  # sell-only, swaps evidence only
    for i in range(3):
        swap(conn, "sol", C, i)
    swap(conn, "sol", D, 0)  # one trade: nothing else known
    for i in range(3):
        swap(conn, "robinhood", EVM, i, source="robinhood")
    feed(conn, Chain.SOL, A, "fa1", ["smart_degen", "gmgn"])
    feed(conn, Chain.SOL, A, "fa2", ["smart_degen", "axiom"])
    feed(conn, Chain.SOL, C, "fc1", ["wash_trader", "trojan"], kind="kol")
    conn.execute("INSERT INTO entities (entity_id, chain, archetype, confidence, size, edge_types_json, created_ms, "
                 "updated_ms, version) VALUES (?,?,?,?,?,?,?,?,?)",
                 ("sol:ent:ec0941ded8a7ebec", "sol", "trader", 0.9, 2, "[]", 1, 1, 1))
    for m in (A, B):
        conn.execute("INSERT INTO entity_members (entity_id, chain, address) VALUES (?,?,?)",
                     ("sol:ent:ec0941ded8a7ebec", "sol", m))
    conn.execute("INSERT INTO token_bundle_members (chain, token, address, role, atoms, buys) VALUES (?,?,?,?,?,?)",
                 ("sol", TOKEN_1, A, "sniper", "1000", 1))
    conn.execute("INSERT INTO wallet_scores (chain, address, score, grade, evidence_weight, archetype, "
                 "model_version, scored_at_ms) VALUES (?,?,?,?,?,?,?,?)", ("sol", A, 50.0, "B", 80.0, "sniper", "v1", 1))
    conn.execute("INSERT INTO tokens (chain, address, creator, first_seen_ms) VALUES (?,?,?,?)", ("sol", TOKEN_2, C, 1))


ZERO = {f: 0 for f in naming._ID_FEEDS}


def registry(conn: Any) -> dict[tuple[str, str], tuple[Any, ...]]:
    return {(r["chain"], r["address"]): (r["name"], r["tags_json"], r["meta_json"], r["first_seen_ms"],
                                         r["last_seen_ms"], r["source"])
            for r in fetch_all(conn, "SELECT * FROM wallets")}


def make_ready(conn: Any, monkeypatch: Any) -> None:
    monkeypatch.setattr(feed_tags, "PARITY_MIN_SAMPLE", 1)
    assert feed_tags.backfill_step(conn, deadline_ms=now_ms() + 30_000)["complete"]
    assert feed_tags.parity_check(conn, sample=50, record=True)["ok"]
    assert feed_tags.table_ready(conn)


# ---------------------------------------------------------------- same facts, same rows


@pytest.mark.parametrize("ready", [False, True])
def test_per_wallet_facts_equal_the_full_pass(tmp_db, monkeypatch, ready):
    seed(tmp_db)
    if ready:
        make_ready(tmp_db, monkeypatch)
    full, _ = naming.gather_facts(tmp_db)
    some = naming.gather_facts_for(tmp_db, list(full))
    assert some.keys() == full.keys()
    for key in full:
        assert some[key] == full[key], key
    assert some[("sol", A)].gmgn_tags == {"smart_degen": 2, "gmgn": 1, "axiom": 1}
    assert some[("sol", A)].gmgn_feeds == {"smartmoney": 2}


def test_incremental_from_zero_writes_exactly_what_the_full_pass_writes(tmp_db, monkeypatch):
    seed(tmp_db)
    make_ready(tmp_db, monkeypatch)
    rep = naming.name_wallets_incremental(tmp_db, start=ZERO, insert_swap_only=True, lag_ms=-10_000)
    assert rep.stopped is None and rep.naming.inserted == 5
    incremental = registry(tmp_db)
    full = naming.name_wallets(tmp_db)
    assert full.inserted == 0 and full.updated == 0 and full.unchanged == full.considered == 5
    assert registry(tmp_db) == incremental


# ---------------------------------------------------------------- only what moved


def test_first_run_starts_at_the_heads_and_names_the_labelled_wallets(tmp_db, monkeypatch):
    seed(tmp_db)
    make_ready(tmp_db, monkeypatch)
    rep = naming.name_wallets_incremental(tmp_db, lag_ms=-10_000)
    # only the labels feed starts at 0: A and C carry GMGN labels; the tape waits for news
    assert set(registry(tmp_db)) == {("sol", A), ("sol", C)}
    assert rep.sources["swaps"]["lag_ids"] == 0 and rep.sources["tags"]["wallets"] == 2
    state = jload(fetch_one(tmp_db, "SELECT value FROM kv WHERE key = ?", (naming.INCREMENTAL_STATE_KEY,))["value"])
    assert state["swaps"] == tmp_db.execute("SELECT max(id) FROM swaps").fetchone()[0]

    # nothing moved: nothing is read
    again = naming.name_wallets_incremental(tmp_db, lag_ms=-10_000)
    assert again.wallets == 0 and again.chunks == 0


def test_new_trades_rename_only_their_wallets_and_swap_only_strangers_get_no_row(tmp_db, monkeypatch):
    seed(tmp_db)
    make_ready(tmp_db, monkeypatch)
    naming.name_wallets_incremental(tmp_db, lag_ms=-10_000)
    before = registry(tmp_db)
    buys_before = jload(before[("sol", A)][2])["naming"]["observed"]["buys"]

    swap(tmp_db, "sol", A, 100, ts=50_000)  # a known wallet trades
    swap(tmp_db, "sol", E, 101, ts=50_001)  # a stranger trades: swaps evidence only
    rep = naming.name_wallets_incremental(tmp_db, lag_ms=-10_000)
    assert rep.sources["swaps"]["wallets"] == 2
    assert rep.naming.updated == 1 and rep.naming.inserted == 0 and rep.skipped_swap_only_new == 1
    after = registry(tmp_db)
    assert set(after) == set(before) and after[("sol", C)] == before[("sol", C)]
    assert jload(after[("sol", A)][2])["naming"]["observed"]["buys"] == buys_before + 1

    # the same stranger with the switch on is inserted like any other wallet
    swap(tmp_db, "sol", E, 102, ts=50_002)
    naming.name_wallets_incremental(tmp_db, lag_ms=-10_000, insert_swap_only=True)
    assert registry(tmp_db)[("sol", E)][0].startswith("unknown#")


def test_a_new_label_a_new_grade_and_a_new_role_each_rename_their_wallet(tmp_db, monkeypatch):
    seed(tmp_db)
    make_ready(tmp_db, monkeypatch)
    naming.name_wallets_incremental(tmp_db, start=ZERO, insert_swap_only=True, lag_ms=-10_000)
    feed(tmp_db, Chain.SOL, D, "fd1", ["kol"], kind="kol")
    tmp_db.execute("INSERT INTO wallet_score_history (chain, address, score, grade, model_version, scored_at_ms) "
                   "VALUES (?,?,?,?,?,?)", ("sol", B, 10.0, "QUARANTINED", "v1", 2))
    tmp_db.execute("INSERT INTO wallet_scores (chain, address, score, grade, evidence_weight, archetype, "
                   "model_version, scored_at_ms) VALUES (?,?,?,?,?,?,?,?)", ("sol", B, 10.0, "QUARANTINED", 1.0,
                                                                          "bot", "v1", 2))
    tmp_db.execute("INSERT INTO token_bundle_members (chain, token, address, role, atoms, buys) VALUES (?,?,?,?,?,?)",
                   ("robinhood", TOKEN_2, EVM, "bundler", "1", 1))
    rep = naming.name_wallets_incremental(tmp_db, lag_ms=-10_000)
    assert rep.naming.updated == 3, rep.as_dict()
    names = {k: v[0] for k, v in registry(tmp_db).items()}
    assert "[gmgn:kol]" in names[("sol", D)]
    assert names[("sol", B)].endswith("QUARANTINED")
    assert names[("robinhood", EVM)].startswith("bundler#")


# ---------------------------------------------------------------- bounded and resumable


def test_a_capped_run_resumes_where_it_stopped_and_loses_nobody(tmp_db, monkeypatch):
    seed(tmp_db)
    make_ready(tmp_db, monkeypatch)
    # one instant per wallet: a single instant shared by two wallets is the one case that
    # may exceed the cap (it is taken whole), and the cap is what this test holds
    tmp_db.execute("UPDATE wallet_feed_tags SET last_ms = CASE address WHEN ? THEN 1000 ELSE 2000 END", (A,))
    runs = []
    while True:
        rep = naming.name_wallets_incremental(tmp_db, start=ZERO, insert_swap_only=True, lag_ms=-10_000,
                                              max_wallets=1, chunk_wallets=1, id_window=1)
        runs.append(rep)
        if rep.stopped is None:
            break
        assert rep.stopped == "max_wallets" and rep.wallets <= 1
        assert len(runs) < 60
    assert len(runs) > 5
    capped = registry(tmp_db)
    full = naming.name_wallets(tmp_db)
    assert full.inserted == 0 and full.updated == 0, "the capped runs together named everyone, correctly"
    assert registry(tmp_db) == capped


def test_the_deadline_stops_a_run_before_it_starts_another_chunk(tmp_db, monkeypatch):
    seed(tmp_db)
    make_ready(tmp_db, monkeypatch)
    tmp_db.execute("UPDATE wallet_feed_tags SET last_ms = CASE address WHEN ? THEN 1000 ELSE 2000 END", (A,))
    t = {"s": now_ms() / 1000}
    real_write = naming._write

    def slow_write(*args: Any, **kw: Any) -> None:  # every chunk's write takes 10 s
        real_write(*args, **kw)
        t["s"] += 10.0

    monkeypatch.setattr(naming, "_write", slow_write)
    rep = naming.name_wallets_incremental(tmp_db, start=ZERO, insert_swap_only=True, lag_ms=-10_000,
                                          chunk_wallets=1, id_window=1, clock=lambda: t["s"],
                                          deadline_ms=int((t["s"] + 15) * 1000))
    assert rep.stopped == "deadline" and rep.chunks == 2 and len(registry(tmp_db)) == 2


def test_a_deadline_inside_a_chunk_writes_nothing_and_keeps_the_cursor(tmp_db, monkeypatch):
    seed(tmp_db)
    make_ready(tmp_db, monkeypatch)
    t = {"s": now_ms() / 1000}
    real_rows = feed_tags.wallet_rows

    def slow_rows(*args: Any, **kw: Any) -> Any:  # the first fact read runs past the deadline
        t["s"] += 60.0
        return real_rows(*args, **kw)

    monkeypatch.setattr(feed_tags, "wallet_rows", slow_rows)
    monkeypatch.setattr(naming, "FACT_BATCH", 1)
    rep = naming.name_wallets_incremental(tmp_db, start=ZERO, insert_swap_only=True, lag_ms=-10_000,
                                          clock=lambda: t["s"], deadline_ms=int((t["s"] + 30) * 1000))
    assert rep.stopped == "deadline_mid_chunk" and rep.chunks == 0
    assert registry(tmp_db) == {}
    assert naming._load_incremental_state(tmp_db)["tags"] == 0, "the chunk's cursor did not move"
    monkeypatch.setattr(feed_tags, "wallet_rows", real_rows)
    assert naming.name_wallets_incremental(tmp_db, insert_swap_only=True, lag_ms=-10_000).stopped is None
    assert len(registry(tmp_db)) == 5


def test_a_new_stranger_with_nothing_but_trades_costs_no_tape_read(tmp_db, monkeypatch):
    seed(tmp_db)
    make_ready(tmp_db, monkeypatch)
    naming.name_wallets_incremental(tmp_db, lag_ms=-10_000)
    swap(tmp_db, "sol", E, 101, ts=50_001)  # stranger: no row, no label, role, entity, grade
    swap(tmp_db, "sol", A, 102, ts=50_002)  # known wallet
    read: list[Any] = []
    real = naming.fetch_all

    def spy(conn: Any, sql: str, params: Any = ()) -> Any:
        if "FROM swaps" in sql:
            read.extend(p for p in params if p in (A, E))
        return real(conn, sql, params)

    monkeypatch.setattr(naming, "fetch_all", spy)
    rep = naming.name_wallets_incremental(tmp_db, lag_ms=-10_000)
    assert rep.skipped_swap_only_new == 1 and E not in read and A in read


def test_a_run_killed_mid_chunk_leaves_that_chunk_for_the_next(tmp_db, monkeypatch):
    seed(tmp_db)
    feed(tmp_db, Chain.SOL, D, "fd9", ["fomo"])  # D: reachable through its label and nothing else
    make_ready(tmp_db, monkeypatch)
    tmp_db.execute("UPDATE wallet_feed_tags SET last_ms = CASE address WHEN ? THEN 1000 WHEN ? THEN 2000 "
                   "ELSE 3000 END", (A, D))
    head = tmp_db.execute("SELECT max(id) FROM swaps").fetchone()[0]
    start = {**ZERO, "swaps": head}  # the tape feed must not rescue the lost chunk
    real_write = naming._write
    calls = {"n": 0}

    def dies_on_second(*args: Any, **kw: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 2:  # the chunk holding D
            raise RuntimeError("killed")
        real_write(*args, **kw)

    monkeypatch.setattr(naming, "_write", dies_on_second)
    with pytest.raises(RuntimeError):
        naming.name_wallets_incremental(tmp_db, start=start, insert_swap_only=True, lag_ms=-10_000,
                                        chunk_wallets=1, id_window=1)
    assert set(registry(tmp_db)) == {("sol", A)}
    assert naming._load_incremental_state(tmp_db)["tags"] == 1000, "the killed chunk's cursor did not move"
    monkeypatch.setattr(naming, "_write", real_write)
    naming.name_wallets_incremental(tmp_db, insert_swap_only=True, lag_ms=-10_000)
    assert ("sol", D) in registry(tmp_db), "the killed chunk was named by the next run"


def test_a_tape_lag_past_the_cap_skips_ahead_and_says_so(tmp_db, monkeypatch):
    seed(tmp_db)
    make_ready(tmp_db, monkeypatch)
    head = tmp_db.execute("SELECT max(id) FROM swaps").fetchone()[0]
    rep = naming.name_wallets_incremental(tmp_db, start={**ZERO, "swaps": 0}, insert_swap_only=True,
                                          lag_ms=-10_000, max_tape_lag_ids=3)
    assert rep.sources["swaps"]["skipped_ahead_ids"] == head - 3
    state = naming._load_incremental_state(tmp_db)
    assert state["swaps"] == head and state["swaps_skipped_total"] == head - 3
    # within the cap nothing is skipped
    swap(tmp_db, "sol", A, 200, ts=60_000)
    again = naming.name_wallets_incremental(tmp_db, lag_ms=-10_000, max_tape_lag_ids=3)
    assert "skipped_ahead_ids" not in again.sources["swaps"] and again.sources["swaps"]["wallets"] == 1


# ---------------------------------------------------------------- the labels feed


def test_a_label_row_younger_than_the_lag_waits(tmp_db, monkeypatch):
    seed(tmp_db)
    make_ready(tmp_db, monkeypatch)
    rep = naming.name_wallets_incremental(tmp_db, lag_ms=3_600_000)
    assert rep.sources["tags"]["wallets"] == 0 and registry(tmp_db) == {}
    rep = naming.name_wallets_incremental(tmp_db, lag_ms=-10_000)
    assert rep.sources["tags"]["wallets"] == 2


def test_a_page_never_ends_inside_one_instant(tmp_db, monkeypatch):
    seed(tmp_db)
    make_ready(tmp_db, monkeypatch)
    # every label row at one instant: a page smaller than the instant takes the whole instant
    tmp_db.execute("UPDATE wallet_feed_tags SET last_ms = 5000")
    keys, cursor = naming._tags_chunk(tmp_db, 0, 10_000, want=1)
    assert cursor == 5000 and set(keys) == {("sol", A), ("sol", C)}
    keys, cursor = naming._tags_chunk(tmp_db, 5000, 10_000, want=1)
    assert keys == [] and cursor == 10_000
    # and a boundary inside a page stops before the tied instant, never in it
    tmp_db.execute("UPDATE wallet_feed_tags SET last_ms = 4000 WHERE address = ?", (A,))
    keys, cursor = naming._tags_chunk(tmp_db, 0, 10_000, want=1)
    assert cursor == 4000 and keys == [("sol", A)]


def test_a_completed_backfill_rewinds_the_labels_feed_once(tmp_db, monkeypatch):
    seed(tmp_db)
    naming.name_wallets_incremental(tmp_db, lag_ms=-10_000)  # before the backfill: cursor runs ahead
    state = naming._load_incremental_state(tmp_db)
    assert state["tags"] > 0 and "tags_reset_for" not in state
    make_ready(tmp_db, monkeypatch)
    rep = naming.name_wallets_incremental(tmp_db, lag_ms=-10_000)
    assert rep.sources["tags"]["cursor_before"] == state["tags"]  # what kv said...
    assert rep.sources["tags"]["rewound_for_backfill"] is True
    assert rep.sources["tags"]["wallets"] == 2  # ...and the run still walked the table again
    assert naming._load_incremental_state(tmp_db)["tags_reset_for"] == feed_tags.backfill_state(tmp_db)["completed_ms"]
    assert naming.name_wallets_incremental(tmp_db, lag_ms=-10_000).sources["tags"]["wallets"] == 0


def test_before_the_table_is_ready_labels_come_from_the_events(tmp_db):
    seed(tmp_db)
    rep = naming.name_wallets_incremental(tmp_db, start=ZERO, insert_swap_only=True, lag_ms=-10_000)
    assert rep.tags_source == "events"
    a = jload(registry(tmp_db)[("sol", A)][2])["naming"]
    assert a["gmgn_tags"] == {"smart_degen": 2} and a["gmgn_apps"] == {"axiom": 1, "gmgn": 1}


def test_dry_run_writes_nothing_not_even_its_cursor(tmp_db, monkeypatch):
    seed(tmp_db)
    make_ready(tmp_db, monkeypatch)
    rep = naming.name_wallets_incremental(tmp_db, start=ZERO, insert_swap_only=True, lag_ms=-10_000, dry_run=True)
    assert rep.naming.inserted == 5 == rep.wallets, "each wallet once a run, whatever feeds it is on"
    assert registry(tmp_db) == {} and naming._load_incremental_state(tmp_db) == {}


# ---------------------------------------------------------------- the job


def test_the_job_runs_incrementally_inside_its_budget(tmp_db, monkeypatch):
    seen: dict[str, Any] = {}

    def fake(conn: Any, **kw: Any) -> Any:
        seen.update(kw)
        return naming.IncrementalReport(dry_run=False, naming=naming.NamingReport(False, -1, -1))

    monkeypatch.setattr(naming, "name_wallets_incremental", fake)
    now = now_ms()
    ctx = S.JobContext("wallet_naming", tmp_db, {"budget_s": 500, "max_wallets": 7}, S.ScheduleConfig(), now,
                       now + 300_000)
    out = S.job_wallet_naming(ctx)
    assert out["mode"] == "incremental" and seen["max_wallets"] == 7 and seen["insert_swap_only"] is False
    assert seen["deadline_ms"] <= ctx.deadline_ms - 30_000, "always 30 s inside the timeout"
