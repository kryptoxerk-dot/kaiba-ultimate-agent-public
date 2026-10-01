"""The scanner is fed what the smart-money lane can act on, not only new launches.

MEASURED 2026-09-22, the hour after the cohort was seeded: 388 tokens scanned, 97% of them
0-3 minutes old (114 at age 0, 138 at 1, 38 at 2, 6 at 3), because every work source was a
NEW-LAUNCH source. In the same window 6 of the 7 sol tokens with >=3 distinct smart-money
buyers were never lane-evaluated. ``sm_trenches`` needs three smart wallets to have
already bought, so it was only ever shown tokens too young for that to be true -- which is
why it had never produced a signal in the system's history.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from kaiba.core.schemas import SOL_NATIVE_MINT, Chain, Lane, WalletTag, now_ms
from kaiba.execution import lanes as lanes_mod
from kaiba.execution import scanner

TOKEN_A = "GMFCWQv8CfnjGR2xCJebyxqSsCmyMo31pXcCEnfDpump"
TOKEN_B = "EfcmyGs6M8auzbHmyW3tki8WbZh2rnbb3bS4eiyUHedq"
BSC_TOKEN = "0x18f91c1d14b0d4c528fb746836948944e6486666"
WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"


def smart_wallet(conn, address: str, chain: Chain = Chain.SOL, tag: str = "smart_money") -> None:
    conn.execute(
        "INSERT OR REPLACE INTO wallets (chain, address, first_seen_ms, last_seen_ms, tags_json) "
        "VALUES (?,?,?,?,?)",
        (chain.value, address, now_ms(), now_ms(), f'["{tag}"]'),
    )


def plain_wallet(conn, address: str, chain: Chain = Chain.SOL) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO wallets (chain, address, first_seen_ms, last_seen_ms, tags_json) "
        "VALUES (?,?,?,?,?)",
        (chain.value, address, now_ms(), now_ms(), '["gmgn:smart_degen"]'),
    )


def buy(conn, wallet: str, token: str, *, chain: Chain = Chain.SOL, age_s: int = 10) -> None:
    conn.execute(
        "INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, amount_token, usd_value, source) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (chain.value, f"tx_{wallet}_{token}_{age_s}", now_ms() - age_s * 1000, wallet, token,
         "buy", "1000", "500", "gmgn:smartmoney"),
    )


def _tokens(items) -> list[str]:
    return [i.token for i in items]


# ------------------------------------------------------------------ the measured case


def test_a_token_smart_money_is_buying_is_offered_whatever_its_age(tmp_db):
    """The population the lane needs and the old sampler could never reach."""
    for i in range(3):
        smart_wallet(tmp_db, f"smart{i}")
        buy(tmp_db, f"smart{i}", TOKEN_A)
    tmp_db.commit()

    # The token is not in the registry at all before the call, so no age could be read.
    assert tmp_db.execute("SELECT COUNT(*) FROM tokens").fetchone()[0] == 0

    work = scanner._smart_flow_work(tmp_db, 10, config=scanner.DEFAULT_CONFIG)

    assert _tokens(work) == [TOKEN_A]
    assert work[0].source == "smart_flow"
    assert work[0].extras["smart_buyers"] == 3
    # It is registered on the way out (the engine needs the row to verify decimals), but
    # with NO created_ms -- age is never consulted and never invented. That is the point.
    row = tmp_db.execute("SELECT created_ms FROM tokens WHERE address=?", (TOKEN_A,)).fetchone()
    assert row is not None and row["created_ms"] is None


def test_the_threshold_is_the_lane_s_own(tmp_db):
    """Two smart buyers is not a candidate; the lane needs three (``min_smart_degen``)."""
    assert scanner._smart_flow_min_wallets(scanner.DEFAULT_CONFIG) == 3
    for i in range(2):
        smart_wallet(tmp_db, f"smart{i}")
        buy(tmp_db, f"smart{i}", TOKEN_A)
    tmp_db.commit()
    assert scanner._smart_flow_work(tmp_db, 10, config=scanner.DEFAULT_CONFIG) == []


def test_unscreened_wallets_do_not_count(tmp_db):
    """A raw GMGN label is not evidence: only the screened tag or a scored archetype is.

    This is the tracker tag-leak rule seen from the feeder's side -- if a vendor label
    counted here, an unscreened wallet would pull tokens into the money path.
    """
    for i in range(4):
        plain_wallet(tmp_db, f"vendor{i}")
        buy(tmp_db, f"vendor{i}", TOKEN_A)
    tmp_db.commit()
    assert scanner._smart_flow_work(tmp_db, 10, config=scanner.DEFAULT_CONFIG) == []


def test_a_scored_archetype_counts_like_a_tag(tmp_db):
    """``lanes.sm_trenches`` accepts either route, so the feeder must too."""
    for i in range(3):
        tmp_db.execute(
            "INSERT OR REPLACE INTO wallet_scores (chain, address, score, grade, "
            "evidence_weight, archetype, model_version, scored_at_ms) VALUES (?,?,?,?,?,?,?,?)",
            (Chain.SOL.value, f"arch{i}", 80.0, "B", 1.0, "smart_money", "v1", now_ms()),
        )
        buy(tmp_db, f"arch{i}", TOKEN_B)
    tmp_db.commit()
    assert _tokens(scanner._smart_flow_work(tmp_db, 10, config=scanner.DEFAULT_CONFIG)) == [TOKEN_B]


def test_stale_buys_fall_out_of_the_window(tmp_db):
    """Accumulation older than the window is not "now".

    Aged relative to the CONFIGURED window rather than a literal: the window moved
    300 -> 1800 s on 2026-09-24 (at 300 s the measured supply of qualifying sol tokens
    was zero) and a hardcoded 900 s silently became an IN-window buy, so this test
    asserted the opposite of its own name.
    """
    stale_s = int(scanner.DEFAULT_CONFIG.smart_flow_window_s) + 600
    for i in range(3):
        smart_wallet(tmp_db, f"smart{i}")
        buy(tmp_db, f"smart{i}", TOKEN_A, age_s=stale_s)
    tmp_db.commit()
    assert scanner._smart_flow_work(tmp_db, 10, config=scanner.DEFAULT_CONFIG) == []


def test_buys_inside_the_window_are_offered(tmp_db):
    """The positive control: without it the test above passes on a broken query."""
    fresh_s = max(1, int(scanner.DEFAULT_CONFIG.smart_flow_window_s) // 2)
    for i in range(3):
        smart_wallet(tmp_db, f"fresh{i}")
        buy(tmp_db, f"fresh{i}", TOKEN_A, age_s=fresh_s)
    tmp_db.commit()
    assert _tokens(scanner._smart_flow_work(tmp_db, 10, config=scanner.DEFAULT_CONFIG)) == [TOKEN_A]


def test_the_quote_asset_is_never_offered(tmp_db):
    """WSOL was the top-ranked candidate the day the cohort landed. A pass on it is wasted."""
    for i in range(5):
        smart_wallet(tmp_db, f"smart{i}")
        buy(tmp_db, f"smart{i}", SOL_NATIVE_MINT)
        buy(tmp_db, f"smart{i}", TOKEN_A)
    smart_wallet(tmp_db, "bnbsmart", Chain.BSC)
    tmp_db.commit()
    got = _tokens(scanner._smart_flow_work(tmp_db, 10, config=scanner.DEFAULT_CONFIG))
    assert SOL_NATIVE_MINT not in got
    assert TOKEN_A in got


def test_wrapped_native_is_not_offered_on_evm_either(tmp_db):
    for i in range(3):
        smart_wallet(tmp_db, f"bsc{i}", Chain.BSC)
        buy(tmp_db, f"bsc{i}", WBNB, chain=Chain.BSC)
        buy(tmp_db, f"bsc{i}", BSC_TOKEN, chain=Chain.BSC)
    tmp_db.commit()
    got = _tokens(scanner._smart_flow_work(tmp_db, 10, config=scanner.DEFAULT_CONFIG))
    assert WBNB not in got and BSC_TOKEN in got


def test_busier_tokens_come_first(tmp_db):
    for i in range(3):
        smart_wallet(tmp_db, f"a{i}")
        buy(tmp_db, f"a{i}", TOKEN_A)
    for i in range(5):
        smart_wallet(tmp_db, f"b{i}")
        buy(tmp_db, f"b{i}", TOKEN_B)
    tmp_db.commit()
    assert _tokens(scanner._smart_flow_work(tmp_db, 10, config=scanner.DEFAULT_CONFIG)) == [TOKEN_B, TOKEN_A]


def test_it_is_bounded_by_n(tmp_db):
    for tok in (TOKEN_A, TOKEN_B, BSC_TOKEN):
        for i in range(3):
            smart_wallet(tmp_db, f"{tok[:6]}{i}")
            buy(tmp_db, f"{tok[:6]}{i}", tok)
    tmp_db.commit()
    assert len(scanner._smart_flow_work(tmp_db, 2, config=scanner.DEFAULT_CONFIG)) == 2


def test_the_switch_turns_it_off(tmp_db):
    for i in range(3):
        smart_wallet(tmp_db, f"smart{i}")
        buy(tmp_db, f"smart{i}", TOKEN_A)
    tmp_db.commit()
    off = replace(scanner.DEFAULT_CONFIG, smart_flow=False)
    assert scanner._smart_flow_work(tmp_db, 10, config=off) == []
    assert scanner.next_work(tmp_db, 10, config=off) == [] or all(
        i.source != "smart_flow" for i in scanner.next_work(tmp_db, 10, config=off)
    )


# ------------------------------------------------------------------ wiring into next_work


def test_next_work_offers_smart_flow(tmp_db):
    for i in range(3):
        smart_wallet(tmp_db, f"smart{i}")
        buy(tmp_db, f"smart{i}", TOKEN_A)
    tmp_db.commit()
    scanner.RECENT.clear() if hasattr(scanner.RECENT, "clear") else None
    work = scanner.next_work(tmp_db, 10, config=scanner.DEFAULT_CONFIG)
    assert TOKEN_A in _tokens(work), [(i.source, i.token) for i in work]


def test_smart_flow_cannot_take_the_whole_batch(tmp_db):
    """pons-robinhood and the migration lanes live on the launch sources below it."""
    for t in range(8):
        tok = f"tok{t}pumpXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"[:44]
        for i in range(3):
            smart_wallet(tmp_db, f"w{t}_{i}")
            buy(tmp_db, f"w{t}_{i}", tok)
    tmp_db.commit()
    n = 8
    work = scanner.next_work(tmp_db, n, config=scanner.DEFAULT_CONFIG)
    smart = [i for i in work if i.source == "smart_flow"]
    assert len(smart) <= max(1, int(n * scanner.DEFAULT_CONFIG.smart_flow_share)), len(smart)


# ------------------------------------------------------------- the registry invariant


def test_a_smart_flow_candidate_is_registered_before_it_is_offered(tmp_db):
    """``fills.token_decimals`` reads the chain only for a token already in ``tokens``.

    MEASURED 2026-09-22, minutes after arming smart flow: two LIVE sm-trenches entries
    were abandoned with ``token_decimals_unavailable`` -- "token not in the tokens
    registry; decimals not fetched". Discovery worked, sizing worked, and no order was
    ever written, because this route surfaced tokens straight from the swaps tape while
    every older route registers as it ingests.
    """
    for i in range(3):
        smart_wallet(tmp_db, f"smart{i}")
        buy(tmp_db, f"smart{i}", TOKEN_A)
    tmp_db.commit()
    assert tmp_db.execute("SELECT COUNT(*) FROM tokens WHERE address=?", (TOKEN_A,)).fetchone()[0] == 0

    work = scanner._smart_flow_work(tmp_db, 10, config=scanner.DEFAULT_CONFIG)

    assert _tokens(work) == [TOKEN_A]
    assert scanner._register_smart_flow_token(tmp_db, Chain.SOL, TOKEN_A) is False, (
        "already registered by the work call; a second write would be reported as True"
    )
    row = tmp_db.execute(
        "SELECT chain, first_seen_ms, created_ms, meta_json FROM tokens WHERE address=?", (TOKEN_A,)
    ).fetchone()
    assert row is not None, "the candidate was offered without being registered"
    assert row["chain"] == "sol"
    assert row["created_ms"] is None, "we did not see it launch; do not invent a launch time"
    assert scanner.SMART_FLOW_TOKEN_SOURCE in row["meta_json"]


def test_registration_never_overwrites_a_listener_row(tmp_db):
    """A launch listener's row is authoritative; this path must not touch it."""
    tmp_db.execute(
        "INSERT INTO tokens (chain, address, first_seen_ms, created_ms, symbol, meta_json) "
        "VALUES (?,?,?,?,?,?)",
        ("sol", TOKEN_A, 111, 222, "REAL", '{"source":"pumpportal"}'),
    )
    for i in range(3):
        smart_wallet(tmp_db, f"smart{i}")
        buy(tmp_db, f"smart{i}", TOKEN_A)
    tmp_db.commit()

    # The guard itself: it must report that it wrote nothing. (INSERT OR IGNORE would
    # also protect the row, so assert the guard directly or a regression hides behind it.)
    assert scanner._register_smart_flow_token(tmp_db, Chain.SOL, TOKEN_A) is False

    scanner._smart_flow_work(tmp_db, 10, config=scanner.DEFAULT_CONFIG)

    row = tmp_db.execute(
        "SELECT created_ms, symbol, meta_json FROM tokens WHERE address=?", (TOKEN_A,)
    ).fetchone()
    assert row["created_ms"] == 222 and row["symbol"] == "REAL"
    assert "pumpportal" in row["meta_json"] and scanner.SMART_FLOW_TOKEN_SOURCE not in row["meta_json"]


def test_registration_is_idempotent(tmp_db):
    for i in range(3):
        smart_wallet(tmp_db, f"smart{i}")
        buy(tmp_db, f"smart{i}", TOKEN_A)
    tmp_db.commit()
    scanner._smart_flow_work(tmp_db, 10, config=scanner.DEFAULT_CONFIG)
    scanner._smart_flow_work(tmp_db, 10, config=scanner.DEFAULT_CONFIG)
    assert tmp_db.execute("SELECT COUNT(*) FROM tokens WHERE address=?", (TOKEN_A,)).fetchone()[0] == 1


def test_first_seen_is_the_earliest_swap_we_hold(tmp_db):
    """Honest provenance: when we first saw it trade, not when it launched."""
    for i in range(3):
        smart_wallet(tmp_db, f"smart{i}")
        buy(tmp_db, f"smart{i}", TOKEN_A, age_s=100 - i * 10)
    tmp_db.commit()
    scanner._smart_flow_work(tmp_db, 10, config=scanner.DEFAULT_CONFIG)
    first = tmp_db.execute("SELECT first_seen_ms FROM tokens WHERE address=?", (TOKEN_A,)).fetchone()[0]
    earliest = tmp_db.execute("SELECT MIN(ts_ms) FROM swaps WHERE token=?", (TOKEN_A,)).fetchone()[0]
    assert first == earliest


# ------------------------------------------------------------------ the cost of asking


def test_the_flow_query_seeks_each_smart_wallets_recent_buys(tmp_db):
    # MEASURED 2026-09-29: the single join planned as a walk of all 7.48M idx_swaps_token
    # entries plus full scans of wallets and wallet_scores, on nearly every batch, and held
    # the scanner inside one long read -- a snapshot that stopped SQLite resetting the WAL.
    for w in ("W1", "W2", "W3"):
        smart_wallet(tmp_db, w)
        buy(tmp_db, w, TOKEN_A)
    seen: list[str] = []
    tmp_db.set_trace_callback(seen.append)
    try:
        assert _tokens(scanner._smart_flow_work(tmp_db, 10, config=scanner.DEFAULT_CONFIG)) == [TOKEN_A]
    finally:
        tmp_db.set_trace_callback(None)
    flow = [s for s in seen if "COUNT(DISTINCT s.wallet)" in s]
    assert flow
    for stmt in flow:
        plan = " | ".join(str(r[3]) for r in tmp_db.execute(f"EXPLAIN QUERY PLAN {stmt}"))
        assert "idx_swaps_wallet" in plan and "wallet=?" in plan, plan
        assert "idx_swaps_token" not in plan, plan


def test_the_smart_set_is_read_once_per_ttl_not_once_per_batch(tmp_db):
    for w in ("W1", "W2", "W3"):
        smart_wallet(tmp_db, w)
        buy(tmp_db, w, TOKEN_A)
    cfg = replace(scanner.DEFAULT_CONFIG, smart_set_ttl_s=300.0)
    scanner._SMART_SET_CACHE.clear()
    seen: list[str] = []
    tmp_db.set_trace_callback(seen.append)
    try:
        for _ in range(5):
            scanner._smart_flow_work(tmp_db, 10, config=cfg)
    finally:
        tmp_db.set_trace_callback(None)
    assert sum("FROM wallets w" in s for s in seen) == 1


def test_a_wallet_screened_in_after_the_ttl_is_counted(tmp_db):
    for w in ("W1", "W2"):
        smart_wallet(tmp_db, w)
        buy(tmp_db, w, TOKEN_A)
    plain_wallet(tmp_db, "W3")
    buy(tmp_db, "W3", TOKEN_A)
    cfg = replace(scanner.DEFAULT_CONFIG, smart_set_ttl_s=0.0)
    scanner._SMART_SET_CACHE.clear()
    assert scanner._smart_flow_work(tmp_db, 10, config=cfg) == []
    smart_wallet(tmp_db, "W3")  # the cohort screen promotes it
    assert _tokens(scanner._smart_flow_work(tmp_db, 10, config=cfg)) == [TOKEN_A]
