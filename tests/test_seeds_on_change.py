"""``wallet_seeds`` writes what changed, from a numeric peak, inside its budget.

MEASURED on the box before 2026-10-02: every 4-hourly run rewrote ``wallets.meta_json``
for every wallet on the tape (sol 762,993 + robinhood 151,767 rows) because the stamp was
refreshed whether or not the count moved; runs took 489-1047 s, one timed out at 2,207 s,
one lost 200 wallets to ``database is locked``. The seed set came from a whole-tape
``GROUP BY token`` whose ``MAX(price_usd)`` compared TEXT.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from kaiba.core.schemas import Chain
from kaiba.intelligence import price_extent as PE
from kaiba.intelligence import seeds as SD

NOW = int(time.time() * 1000)
_seq = iter(range(10**9))


def swap(conn, wallet: str, token: str, price: Any, *, side: str = "buy", ts_ms: int | None = None,
         chain: str = "sol") -> None:
    conn.execute(
        "INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, amount_token, price_usd, "
        "usd_value, source) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (chain, f"tx{next(_seq)}", ts_ms if ts_ms is not None else NOW - 60_000 + next(_seq),
         wallet, token, side, "1", price, "10", "test"),
    )
    conn.commit()


def a_seed(conn, token: str, buyers: list[str]) -> None:
    """A token that ran 6x on eight prints, bought by ``buyers`` (the maker only sells)."""
    for px in ["1.0"] * 7 + ["6.0"]:
        swap(conn, "maker", token, px, side="sell")
    for b in buyers:
        swap(conn, b, token, "2.0")


def meta(conn, address: str) -> dict[str, Any]:
    row = conn.execute("SELECT meta_json FROM wallets WHERE chain='sol' AND address=?",
                       (address,)).fetchone()
    return json.loads(row[0]) if row else {}


def test_an_unchanged_count_is_not_rewritten(tmp_db):
    """THE WRITE STORM. The second run of an unchanged tape writes nothing at all."""
    a_seed(tmp_db, "Seed1", ["alice", "bob"])
    swap(tmp_db, "carol", "Dud", "1.0")
    first = SD.run(Chain.SOL, tmp_db)
    assert first["written"] == first["wallets_considered"] == 4  # maker, alice, bob, carol
    stamps = {a: meta(tmp_db, a)["seed_confluence_ms"] for a in ("alice", "carol")}
    time.sleep(0.01)
    second = SD.run(Chain.SOL, tmp_db)
    assert second["written"] == 0 and second["unchanged"] == 4, second
    assert {a: meta(tmp_db, a)["seed_confluence_ms"] for a in ("alice", "carol")} == stamps


def test_a_changed_count_and_a_new_wallet_are_written_and_nothing_else(tmp_db):
    a_seed(tmp_db, "Seed1", ["alice", "bob"])
    SD.run(Chain.SOL, tmp_db)
    a_seed(tmp_db, "Seed2", ["alice"])          # alice now holds two seeds
    swap(tmp_db, "dave", "Dud", "1.0")         # a new wallet: a measured zero
    rep = SD.run(Chain.SOL, tmp_db)
    assert rep["written"] == 2, rep
    assert meta(tmp_db, "alice")["seed_confluence"] == 2
    assert meta(tmp_db, "dave")["seed_confluence"] == 0, "a measured zero must still land"
    assert meta(tmp_db, "bob")["seed_confluence"] == 1


def test_other_meta_keys_survive_the_write(tmp_db):
    tmp_db.execute("INSERT INTO wallets (chain, address, meta_json, first_seen_ms, last_seen_ms) "
                   "VALUES ('sol','alice','{\"name\": \"kept\"}',1,1)")
    tmp_db.commit()
    a_seed(tmp_db, "Seed1", ["alice"])
    SD.run(Chain.SOL, tmp_db)
    assert meta(tmp_db, "alice") | {"seed_confluence_ms": 0} == {
        "name": "kept", "seed_confluence": 1, "seed_confluence_ms": 0}


@pytest.mark.parametrize(("rule", "seeds"), [(PE.PEAK_LEGACY_TEXT, 0), (PE.PEAK_NUMERIC, 1)])
def test_a_seed_written_in_scientific_notation(tmp_db, monkeypatch, rule, seeds):
    """THE BUG: '9e-06' is the TEXT maximum of these prints, so the scan read 0.9x.

    The legacy rule (shipped default) keeps that answer; the numeric rule sees the 6x.
    """
    monkeypatch.setattr(SD, "SEED_PEAK_RULE", rule)
    for px in ["0.00001"] * 6 + ["9e-06", "0.00006"]:
        swap(tmp_db, "maker", "TokA", px, side="sell")
    swap(tmp_db, "alice", "TokA", "0.00002")
    rep = SD.run(Chain.SOL, tmp_db)
    assert rep["seeds"] == seeds
    assert meta(tmp_db, "alice")["seed_confluence"] == seeds


def test_seeds_never_walk_the_whole_tape(tmp_db):
    a_seed(tmp_db, "Seed1", ["alice"])
    statements: list[str] = []
    tmp_db.set_trace_callback(statements.append)
    try:
        SD.seed_tokens(tmp_db, Chain.SOL)
    finally:
        tmp_db.set_trace_callback(None)
    assert not [s for s in statements if "FROM swaps" in s and "GROUP BY" in s], statements


def test_nothing_is_written_while_the_extent_is_bootstrapping(tmp_db):
    a_seed(tmp_db, "Seed1", ["alice"])
    rep = SD.run(Chain.SOL, tmp_db, deadline=0.0)
    assert rep["not_ready"] is True and rep["written"] == 0
    assert meta(tmp_db, "alice") == {}


def test_a_cut_pass_resumes_after_the_last_wallet_it_finished(tmp_db, monkeypatch):
    monkeypatch.setattr(SD, "SEED_READ_CHUNK", 2)
    a_seed(tmp_db, "Seed1", ["a1", "a2", "a3", "a4", "a5"])
    PE.advance(tmp_db)                                    # fold first; only the pass is cut
    ticks = iter([0.0, 9.0, 9.0, 9.0, 9.0, 9.0])
    first = SD.run(Chain.SOL, tmp_db, deadline=1.0, clock=lambda: next(ticks))
    assert first["truncated"] is True and first["written"] == 2  # a1, a2
    key = SD.KV_RESUME.format(chain="sol")
    assert tmp_db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()[0] == "a2"
    second = SD.run(Chain.SOL, tmp_db)
    assert second["resumed_after"] == "a2" and second["truncated"] is False
    assert second["written"] == 4  # a3, a4, a5, maker; a1/a2 come round again unchanged
    assert all(meta(tmp_db, w).get("seed_confluence") == 1 for w in ("a1", "a3", "a5"))
    assert tmp_db.execute("SELECT 1 FROM kv WHERE key=?", (key,)).fetchone() is None


def test_the_job_fails_while_bootstrapping_and_shares_its_budget(tmp_db):
    from kaiba.core.schemas import now_ms
    from kaiba.ops import scheduler as S

    a_seed(tmp_db, "Seed1", ["alice"])
    now = now_ms()
    ctx = S.JobContext("wallet_seeds", tmp_db, {"chains": ["sol", "bsc"]}, S.ScheduleConfig(),
                       now, now + 10_000)  # inside the 30 s margin: nothing can fold
    with pytest.raises(S.JobFailed) as err:
        S.job_wallet_seeds(ctx)
    assert "price extent" in str(err.value)

    ctx = S.JobContext("wallet_seeds", tmp_db, {"chains": ["sol", "bsc"]}, S.ScheduleConfig(),
                       now, now + 600_000)
    out = S.job_wallet_seeds(ctx)
    assert out["per_chain"]["sol"]["written"] == 2 and out["per_chain"]["bsc"]["written"] == 0

    clock = lambda: 1000.0  # noqa: E731
    share = S.JobContext("x", tmp_db, {}, S.ScheduleConfig(), 0, 1_000_000 + 330_000, clock=clock)
    assert S._share_deadline(share, 3) == pytest.approx(1000.0 + 300.0 / 3)
    assert S._share_deadline(share, 1) == pytest.approx(1300.0)


def test_the_wallet_index_folds_incrementally_by_id_range(tmp_db, monkeypatch):
    """2026-10-06: no whole-tape SELECT DISTINCT; new swap rows are folded by PK range."""
    monkeypatch.setattr(SD, "WALLET_SCAN_CHUNK", 2)
    a_seed(tmp_db, "Seed1", ["w1", "w2", "w3"])
    from kaiba.core.db import connect
    w = connect()
    first = SD.refresh_wallet_index(tmp_db, w, deadline=0.0, clock=lambda: 1.0)
    assert first["added"] >= 1 and not first["complete"], "one chunk, then the deadline stops it"
    rest = SD.refresh_wallet_index(tmp_db, w)
    assert rest["complete"] and rest["cursor"] == rest["max_id"]
    got = {r[0] for r in tmp_db.execute("SELECT wallet FROM seed_wallets WHERE chain='sol'")}
    assert {"w1", "w2", "w3"} <= got
    again = SD.refresh_wallet_index(tmp_db, w)
    assert again["added"] == 0, "caught up: nothing re-read"


def test_run_never_scans_the_whole_tape(tmp_db, monkeypatch):
    import inspect
    assert "SELECT DISTINCT wallet FROM swaps" not in inspect.getsource(SD.run)


def test_counts_never_group_the_swaps_tape(tmp_db):
    """2026-10-06: ``GROUP BY wallet ... token IN (seeds)`` on swaps walked idx_swaps_wallet
    for the whole chain (> 60 s a chunk on the box). Counts come from ``seed_buyers``."""
    a_seed(tmp_db, "Seed1", ["alice"])
    statements: list[str] = []
    tmp_db.set_trace_callback(statements.append)
    try:
        rep = SD.run(Chain.SOL, tmp_db)
    finally:
        tmp_db.set_trace_callback(None)
    assert meta(tmp_db, "alice")["seed_confluence"] == 1, rep
    assert not [s for s in statements if "FROM swaps" in s and "GROUP BY" in s], statements


def test_a_cut_backfill_writes_nothing_then_resumes_mid_token(tmp_db, monkeypatch):
    """An unfinished backfill is an undercount: nothing is written until every seed is read,
    and a token cut part-way resumes from its saved position rather than restarting."""
    monkeypatch.setattr(SD, "BACKFILL_PAGE", 3)
    a_seed(tmp_db, "Seed1", ["b1", "b2", "b3", "b4", "b5"])
    swap(tmp_db, "b1", "Dud", "1.0")
    PE.advance(tmp_db)
    first = SD.run(Chain.SOL, tmp_db, deadline=1.0, clock=lambda: 9.0)  # one unit of work
    assert first["backfilling"] is True and first["written"] == 0, first
    assert meta(tmp_db, "b1") == {}
    at = tmp_db.execute("SELECT at_id, done_ms FROM seed_buyer_tokens WHERE token='Seed1'").fetchone()
    assert at[0] > 0 and at[1] is None, "position saved, not done"

    reads: list[str] = []
    tmp_db.set_trace_callback(reads.append)
    try:
        second = SD.run(Chain.SOL, tmp_db)
    finally:
        tmp_db.set_trace_callback(None)
    assert not second.get("backfilling"), second
    assert second["seed_buyers"]["backfilled"] == 1
    pages = [s for s in reads if "INDEXED BY idx_swaps_token" in s]
    assert pages and f"id > {at[0]}" in pages[0], "resumed from the saved position"
    assert not [s for s in pages if "id > -1" in s], "never restarted from the beginning"
    assert all(meta(tmp_db, w)["seed_confluence"] == 1 for w in ("b1", "b3", "b5"))
    assert meta(tmp_db, "maker")["seed_confluence"] == 0, "the maker only sold"


def test_new_buys_of_a_seed_arrive_through_the_cursor(tmp_db):
    a_seed(tmp_db, "Seed1", ["alice"])
    a_seed(tmp_db, "Seed2", ["alice"])
    SD.run(Chain.SOL, tmp_db)
    assert meta(tmp_db, "alice")["seed_confluence"] == 2
    swap(tmp_db, "late", "Seed1", "3.0")
    swap(tmp_db, "late", "Seed2", "3.0")
    swap(tmp_db, "late", "Seed2", "3.0", chain="bsc")  # another chain's buy is not ours
    rep = SD.run(Chain.SOL, tmp_db)
    assert rep["seed_buyers"]["backfilled"] == 0, "already-read seeds are not re-read"
    assert meta(tmp_db, "late")["seed_confluence"] == 2
