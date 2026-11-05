"""Stockereum (Ethereum) ingest decoding and trader grading. Synthetic addresses only."""

from __future__ import annotations

from kaiba.ingest import stockereum as st
from kaiba.learning import eth_traders as et

TRADER = "0x" + "11" * 20
TOKEN = "0x" + "22" * 20
DEV = "0x" + "33" * 20


def topic(addr: str) -> str:
    return "0x" + "0" * 24 + addr[2:]


def word(v: int) -> str:
    return f"{v:064x}"


def lg(address, t0, topics, words, block=100, idx=0, tx="0xaa"):
    return {"address": address, "topics": [t0 + "0" * (66 - len(t0))] + topics, "data": "0x" + "".join(word(w) for w in words),
            "blockNumber": hex(block), "logIndex": hex(idx), "transactionHash": tx}


def test_decode_router_buy_sell_factory_launch_and_dev_buy():
    logs = [
        lg(st.ROUTER, st.TOPIC_BUY, [topic(TRADER), topic(TOKEN)], [25 * 10**15, 3507 * 10**18]),
        lg(st.ROUTER, st.TOPIC_SELL, [topic(TRADER), topic(TOKEN)], [3507 * 10**18, 40 * 10**15], idx=1),
        lg(st.FACTORY, st.TOPIC_LAUNCH, [topic(TOKEN), topic(DEV), topic(st.WETH)], [0] * 8, idx=2),
        lg(st.FACTORY, st.TOPIC_DEV_BUY, [topic(TOKEN), topic(DEV)], [10**16, 9 * 10**22], idx=3),
        lg("0x" + "99" * 20, st.TOPIC_BUY, [topic(TRADER), topic(TOKEN)], [1, 1], idx=4),  # not ours
    ]
    trades, launches = st.decode(logs)
    assert [(t.side, t.wallet, t.token, t.eth_wei, t.kind) for t in trades] == [
        ("buy", TRADER, TOKEN, 25 * 10**15, "router"),
        ("sell", TRADER, TOKEN, 40 * 10**15, "router"),
        ("buy", DEV, TOKEN, 10**16, "dev_buy"),
    ]
    assert launches == [st.Launch("0xaa", 100, TOKEN, DEV, st.WETH)]


def rows_for(wallet, n, buy_eth, sell_eth, token_prefix="t"):
    out = []
    for i in range(n):
        tok = f"{token_prefix}{i}"
        out.append((wallet, tok, "buy", "1000", str(int(buy_eth * 1e18)), 1000))
        out.append((wallet, tok, "sell", "1000", str(int(sell_eth * 1e18)), 61000))
    return out


def test_grades_are_realized_only_and_one_lucky_hit_is_not_an_a():
    steady = rows_for(TRADER, 6, 0.5, 0.75)                  # +1.5 ETH over 6 wins
    lucky = rows_for(DEV, 5, 0.1, 0.05) + [(DEV, "moon", "buy", "1000", str(10**17), 1), (DEV, "moon", "sell", "1000", str(5 * 10**18), 2)]
    holder = [("0x" + "44" * 20, "x", "buy", "1000", str(10**18), 1)]   # never sold: realizes nothing
    stats = et.compute(steady + lucky + holder, devs={DEV})
    assert stats[TRADER].grade == "A" and abs(stats[TRADER].realized_eth - 1.5) < 1e-9
    assert stats[TRADER].median_hold_s == 60
    assert stats[DEV].grade != "A" and stats[DEV].is_dev, "a single +4.9 ETH hit with 1/6 wins is not a trader"
    assert stats["0x" + "44" * 20].realized_eth == 0 and stats["0x" + "44" * 20].grade is None


def test_partial_sell_matches_proportional_cost():
    rows = [(TRADER, "t", "buy", "1000", str(10**18), 1), (TRADER, "t", "sell", "500", str(10**18), 2)]
    s = et.compute(rows, set())[TRADER]
    assert abs(s.realized_eth - 0.5) < 1e-9 and s.trips == 1


def test_only_weth_quoted_launches_are_graded_in_eth(tmp_db):
    """First live run: a PEPE-quoted pool's router amounts read as 219,971 'ETH'."""
    import json as _json

    from kaiba.ingest import stockereum as st_mod

    for tok, quote in (("0x" + "aa" * 20, st_mod.WETH), ("0x" + "bb" * 20, "0x" + "cc" * 20)):
        tmp_db.execute("INSERT INTO kv (key, value, updated_ms) VALUES (?,?,1)",
                       (f"stockereum:creator:{tok}", _json.dumps({"creator": DEV, "quote": quote})))
        for side, wei in (("buy", 10**18), ("sell", 2 * 10**18 if quote == st_mod.WETH else 10**30)):
            tmp_db.execute("INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, amount_token, amount_native, source) "
                           "VALUES ('eth', ?, 1, ?, ?, ?, '1000', ?, 'stockereum:events')",
                           (f"{tok}{side}", TRADER, tok, side, str(wei)))
    out = et.run(tmp_db, write=False)
    assert out["eth_quoted_tokens"] == 1 and out["eth_quoted_trades"] == 2
    assert out["huge"][0][1] == 1.0, out["huge"]
