"""The smart-flow feeder counts robinhood trusted_copy wallets the way sm-trenches does.

ADDED 2026-10-04 with the lane's trusted_copy route. Two tagged smart wallets plus one
owner-curated trusted wallet meet the lane's three-buyer bar, but the feeder used to count
only tags and archetypes, so such a token was offered only if another work source happened
to scan it. Robinhood only, cohort from the database, every bound unchanged.

All addresses are synthetic.
"""

from __future__ import annotations

import pytest

from kaiba.core.schemas import Chain, now_ms
from kaiba.execution import lanes as lanes_mod
from kaiba.execution import scanner

TOKEN = "0x" + "71" * 20
TAGGED = ["0x" + f"{i:02d}" * 20 for i in (11, 12, 13, 14, 15, 16, 17, 18)]
TRUSTED = "0x" + "c3" * 20


def wallet(conn, address: str, chain: Chain, *, tags: str = "[]", cohort: str | None = None) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO wallets (chain, address, first_seen_ms, last_seen_ms, tags_json, cohort) "
        "VALUES (?,?,?,?,?,?)",
        (chain.value, address, now_ms(), now_ms(), tags, cohort),
    )


def buy(conn, address: str, token: str, chain: Chain, age_s: int = 10) -> None:
    conn.execute(
        "INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, amount_token, usd_value, source) "
        "VALUES (?,?,?,?,?,'buy','1000','500','alchemy:ws')",
        (chain.value, f"tx_{address}_{token}_{age_s}", now_ms() - age_s * 1000, address, token),
    )


def two_tagged_and_one(conn, chain: Chain, cohort: str | None) -> None:
    for a in TAGGED[:2]:
        wallet(conn, a, chain, tags='["smart_money"]')
        buy(conn, a, TOKEN, chain)
    wallet(conn, TRUSTED, chain, cohort=cohort)
    buy(conn, TRUSTED, TOKEN, chain)
    conn.commit()
    scanner._SMART_SET_CACHE.clear()


def offered(conn) -> list[tuple[str, int]]:
    return [(i.token, i.extras["smart_buyers"])
            for i in scanner._smart_flow_work(conn, 10, config=scanner.DEFAULT_CONFIG)]


def test_a_robinhood_trusted_buyer_completes_the_feeders_bar(tmp_db):
    two_tagged_and_one(tmp_db, Chain.ROBINHOOD, "trusted_copy")
    assert offered(tmp_db) == [(TOKEN, 3)]


@pytest.mark.parametrize("cohort", [None, "tracked", "research"])
def test_any_other_cohort_leaves_the_token_two_buyers_short(tmp_db, cohort):
    two_tagged_and_one(tmp_db, Chain.ROBINHOOD, cohort)
    assert offered(tmp_db) == []


@pytest.mark.parametrize("chain", [Chain.BSC, Chain.SOL])
def test_trusted_copy_off_robinhood_is_not_smart_flow(tmp_db, chain):
    """bsc holds unrelated trusted_copy rows; the lane ignores them and so must the feeder."""
    two_tagged_and_one(tmp_db, chain, "trusted_copy")
    assert offered(tmp_db) == []


def test_the_feeder_reads_the_lanes_chain_set_not_its_own(tmp_db, monkeypatch):
    two_tagged_and_one(tmp_db, Chain.BSC, "trusted_copy")
    monkeypatch.setattr(lanes_mod, "TRUSTED_COPY_CHAINS", frozenset({Chain.BSC}))
    assert offered(tmp_db) == [(TOKEN, 3)]
    monkeypatch.setattr(lanes_mod, "TRUSTED_COPY_CHAINS", frozenset())
    scanner._SMART_SET_CACHE.clear()
    assert offered(tmp_db) == []


def test_the_share_cap_still_bounds_smart_flow(tmp_db):
    """Eight robinhood tokens each with two tagged + one trusted buyer: still capped."""
    wallet(tmp_db, TRUSTED, Chain.ROBINHOOD, cohort="trusted_copy")
    for t in range(8):
        tok = "0x" + f"{t + 40:02d}" * 20
        for a in TAGGED[:2]:
            wallet(tmp_db, a, Chain.ROBINHOOD, tags='["smart_money"]')
            buy(tmp_db, a, tok, Chain.ROBINHOOD)
        buy(tmp_db, TRUSTED, tok, Chain.ROBINHOOD)
    tmp_db.commit()
    scanner._SMART_SET_CACHE.clear()
    assert len(offered(tmp_db)) == 8  # every one qualifies on its own
    n = 8
    work = scanner.next_work(tmp_db, n, config=scanner.DEFAULT_CONFIG)
    smart = [i for i in work if i.source == "smart_flow"]
    assert 1 <= len(smart) <= max(1, int(n * scanner.DEFAULT_CONFIG.smart_flow_share)), len(smart)


def test_the_smart_set_is_still_one_query_per_ttl(tmp_db):
    two_tagged_and_one(tmp_db, Chain.ROBINHOOD, "trusted_copy")
    seen: list[str] = []
    tmp_db.set_trace_callback(seen.append)
    try:
        for _ in range(3):
            scanner._smart_flow_work(tmp_db, 10, config=scanner.DEFAULT_CONFIG)
    finally:
        tmp_db.set_trace_callback(None)
    assert sum("FROM wallets w" in s for s in seen) == 1
