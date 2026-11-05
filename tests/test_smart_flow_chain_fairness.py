"""The smart-flow slice must not exclude a whole chain by construction.

MEASURED on the live box 2026-09-22, over 3 hours and 36 ticks of the REAL feeder:

    chain        qualified rows   got into the slice   pass rate   nw p90 / max
    bsc                   1,777                   93        5.2%        13 / 26
    sol                   1,755                   51        2.9%        11 / 20
    robinhood               528                    0        0.0%         4 /  6

Robinhood never won a single slot. Not rarely -- never. The feeder ranks every chain
together by `nw` (distinct smart wallets buying inside the window) and keeps the global
top n, and robinhood's counts top out at 6 where bsc reaches 26. A smaller chain is
therefore ranked out by construction, no matter how good its candidates are, and a token
that is never offered cannot become a signal or a trade. Over the same window robinhood
made ZERO decisions while sol and bsc traded.

That comparison is not meaningful: `nw` counts wallets from each chain's own cohort, and
those cohorts differ in size and in how much of each chain we observe. Ranking them in one
pool measures the chain, not the candidate.

The fix interleaves by chain -- best candidate from each chain, then the next best from
each, and so on -- so ordering still decides WHICH token from a chain is looked at, while
every chain with qualifying work gets looked at at all. It changes no threshold, spends no
extra provider capacity, and takes no more passes per tick.
"""

from __future__ import annotations

import sqlite3

import pytest

from kaiba.core.schemas import Chain, now_ms
from kaiba.execution import scanner


def seed(conn, rows):
    """rows: (chain, token, n_wallets). Writes the swaps and tagged wallets the CTE reads."""
    now = now_ms()
    for i, (chain, token, nw) in enumerate(rows):
        for k in range(nw):
            w = f"w{chain}{i}_{k}".ljust(32, "x")
            conn.execute(
                "INSERT OR REPLACE INTO wallets (chain, address, source, first_seen_ms, "
                "last_seen_ms, tags_json) VALUES (?,?,?,?,?,?)",
                (chain, w, "test", now, now, '["smart_money"]'),
            )
            conn.execute(
                "INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, amount_token, "
                "price_usd, usd_value, source) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (chain, f"tx{chain}{i}{k}", now - 1000, w, token, "buy", "1", "1.0", "100", "test"),
            )
    conn.commit()


CFG = scanner.DEFAULT_CONFIG


def test_a_chain_with_smaller_counts_is_not_excluded_outright(tmp_db):
    """THE LIVE BUG: robinhood qualified 528 times and was offered 0 slots."""
    seed(tmp_db, [
        ("sol", "solA" + "1" * 30, 9),
        ("sol", "solB" + "1" * 30, 8),
        ("bsc", "0xbsc" + "a" * 35, 7),
        ("bsc", "0xbsc" + "b" * 35, 6),
        ("robinhood", "0xrh" + "a" * 36, 3),   # genuinely qualifying, just smaller
    ])
    got = scanner._smart_flow_work(tmp_db, 4, config=CFG)
    chains = {w.chain for w in got}
    assert Chain.ROBINHOOD in chains, (
        f"a qualifying chain must be offered at least one slot; got {[w.chain.value for w in got]}"
    )


def test_every_qualifying_chain_appears_before_any_chain_repeats(tmp_db):
    """Interleaved, not grouped: one from each chain before a second from any."""
    seed(tmp_db, [
        ("sol", "solA" + "1" * 30, 9),
        ("sol", "solB" + "1" * 30, 8),
        ("sol", "solC" + "1" * 30, 7),
        ("bsc", "0xbsc" + "a" * 35, 6),
        ("robinhood", "0xrh" + "a" * 36, 3),
    ])
    got = scanner._smart_flow_work(tmp_db, 3, config=CFG)
    seen = [w.chain.value for w in got]
    assert len(set(seen)) == 3, f"first three slots must cover three chains, got {seen}"


def test_ordering_within_a_chain_is_still_by_strength(tmp_db):
    """Fairness decides WHICH chains are looked at; nw still decides which token."""
    seed(tmp_db, [
        ("sol", "solWEAK" + "1" * 27, 3),
        ("sol", "solSTRONG" + "1" * 25, 9),
    ])
    got = scanner._smart_flow_work(tmp_db, 1, config=CFG)
    assert got and "STRONG" in got[0].token, [w.token for w in got]


def test_one_chain_alone_still_fills_the_whole_slice(tmp_db):
    """Fairness must not idle capacity when only one chain has work."""
    seed(tmp_db, [("sol", f"sol{i}" + "1" * 30, 9 - i) for i in range(4)])
    got = scanner._smart_flow_work(tmp_db, 4, config=CFG)
    assert len(got) == 4, [w.token for w in got]
    assert {w.chain for w in got} == {Chain.SOL}


def test_the_slice_size_is_still_respected(tmp_db):
    seed(tmp_db, [
        ("sol", "solA" + "1" * 30, 9),
        ("bsc", "0xbsc" + "a" * 35, 8),
        ("robinhood", "0xrh" + "a" * 36, 7),
    ])
    assert len(scanner._smart_flow_work(tmp_db, 2, config=CFG)) == 2


def test_nothing_qualifying_yields_nothing(tmp_db):
    assert scanner._smart_flow_work(tmp_db, 4, config=CFG) == []


def test_the_strongest_chain_still_leads(tmp_db):
    """Fairness stops one chain taking EVERY slot; it must not reorder who goes first.

    The round-robin visits chains in descending order of their best candidate, so the
    strongest chain keeps first pick. Without this the weakest chain would lead, which
    trades one bias for another.
    """
    seed(tmp_db, [
        ("robinhood", "0xrh" + "a" * 36, 3),
        ("bsc", "0xbsc" + "a" * 35, 5),
        ("sol", "solA" + "1" * 30, 20),
    ])
    got = scanner._smart_flow_work(tmp_db, 3, config=CFG)
    assert [w.chain.value for w in got] == ["sol", "bsc", "robinhood"], [w.chain.value for w in got]


def test_uneven_buckets_do_not_run_off_the_end(tmp_db):
    """One chain with many candidates and another with one: the short bucket is exhausted
    in round 0 and must simply be skipped in round 1, not indexed past its end."""
    seed(tmp_db, [
        ("sol", "solA" + "1" * 30, 9),
        ("sol", "solB" + "1" * 30, 8),
        ("sol", "solC" + "1" * 30, 7),
        ("robinhood", "0xrh" + "a" * 36, 4),
    ])
    got = scanner._smart_flow_work(tmp_db, 4, config=CFG)
    assert len(got) == 4, [w.token for w in got]
    seen = [w.chain.value for w in got]
    assert seen.count("robinhood") == 1, seen
    assert seen.count("sol") == 3, seen


def test_a_short_bucket_is_skipped_in_later_rounds(tmp_db):
    """The same shape at a slice size that forces three full rounds."""
    seed(tmp_db, [("sol", f"sol{i}" + "1" * 30, 9 - i) for i in range(5)]
               + [("bsc", "0xbsc" + "a" * 35, 4)])
    got = scanner._smart_flow_work(tmp_db, 5, config=CFG)
    assert len(got) == 5
    assert [w.chain.value for w in got].count("bsc") == 1
