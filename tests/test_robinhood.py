"""Pons V2 on Robinhood Chain: parsing, persistence, curve mathematics and the poll loop.

Offline by default. Every fixture under ``tests/fixtures/robinhood/`` is **real recorded
chain data** — logs, transactions, receipts and ``eth_call`` results pulled from
``https://rpc.mainnet.chain.robinhood.com`` on 2026-09-20 — rather than hand-written
shapes. That matters more than usual here: the addresses and topics in
``kaiba/ingest/robinhood.py`` are hard-coded constants, so a test written against
invented data would pass against a wrong constant. Replaying the real logs means a wrong
constant fails.

The live tests at the bottom are marked ``@pytest.mark.live`` and skipped unless
``KAIBA_LIVE_TESTS=1``.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path

import pytest

from kaiba.core.db import fetch_all, fetch_one
from kaiba.core.schemas import Chain, EventKind
from kaiba.ingest import robinhood as rh

FIXTURES = Path(__file__).parent / "fixtures" / "robinhood"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _clean_module_state():
    """Module-level caches are process-global; a test must not inherit another's."""
    rh.reset_latency()
    rh._BLOCK_TS.clear()
    yield
    rh.reset_latency()
    rh._BLOCK_TS.clear()


@pytest.fixture
def launch_fx() -> dict:
    return fixture("launch")


@pytest.fixture
def window_fx() -> dict:
    return fixture("poll_window")


@pytest.fixture
def mid_raise_fx() -> dict:
    return fixture("curve_mid_raise")


@pytest.fixture
def graduation_fx() -> dict:
    return fixture("graduation")


def _launch_logs(window: dict) -> list[dict]:
    return [x for x in window["factory_logs"] if x["topics"][0] == rh.TOPIC_TOKEN_LAUNCHED]


def _trade_logs(window: dict) -> list[dict]:
    return [x for x in window["curve_logs"]
            if x["topics"][0] in (rh.TOPIC_CURVE_BUY, rh.TOPIC_CURVE_SELL)]


def _traded_launch(window: dict) -> tuple[dict, rh.CurveMeta, list[dict]]:
    """A launch from the window that actually has trades on its curve.

    Not every launch trades inside the recorded window, so tests that need a trade have
    to look for one rather than taking the first launch and hoping.
    """
    trades_by_curve: dict[str, list[dict]] = {}
    for log in _trade_logs(window):
        trades_by_curve.setdefault(log["address"].lower(), []).append(log)
    for log in _launch_logs(window):
        parsed = rh.parse_token_launched(log)
        if parsed is None:
            continue
        _, meta = parsed
        if trades_by_curve.get(meta.curve):
            return log, meta, trades_by_curve[meta.curve]
    pytest.skip("no launch with trades on its own curve in the recorded window")


# ---------------------------------------------------------------- the constants are real


def test_recorded_launch_log_carries_the_topic_we_hard_coded(launch_fx):
    """If TOPIC_TOKEN_LAUNCHED were wrong, the listener would match nothing, silently."""
    assert launch_fx["log"]["topics"][0] == rh.TOPIC_TOKEN_LAUNCHED
    assert launch_fx["log"]["address"].lower() == rh.FACTORY_V2


def test_the_launch_transaction_proves_the_factory_is_the_launchpad(launch_fx):
    """The role claim, asserted rather than asserted-in-a-comment.

    A launch is a transaction *to* the factory carrying exactly the 0.0005 ETH launch fee,
    whose receipt emits ``TokenLaunched`` from that same address. That is what makes this
    contract the launchpad, as opposed to merely a busy address.
    """
    tx, receipt = launch_fx["transaction"], launch_fx["receipt"]
    assert tx["to"].lower() == rh.FACTORY_V2
    assert int(tx["value"], 16) == rh.LAUNCH_FEE_WEI == 500_000_000_000_000
    assert receipt["status"] == "0x1"
    emitters = {log["address"].lower() for log in receipt["logs"]}
    assert rh.FACTORY_V2 in emitters
    assert any(log["topics"][0] == rh.TOPIC_TOKEN_LAUNCHED for log in receipt["logs"])


def test_curve_trade_topics_appear_on_per_token_curves_not_on_the_factory(window_fx):
    """Curves are per token: the trade topics never come from the factory address."""
    trades = _trade_logs(window_fx)
    assert trades, "fixture should contain curve trades"
    emitters = {x["address"].lower() for x in trades}
    assert rh.FACTORY_V2 not in emitters
    assert len(emitters) > 1, "many distinct curve contracts, one per token"


def test_graduation_is_atomic_with_the_buy_that_triggers_it(graduation_fx):
    """The venue's defining property, pinned by a real receipt.

    On pump.fun a migration is its own transaction minutes later, which is the window
    ``migration-fade`` trades. Here the curve buy, the pool creation, the liquidity seed,
    ``PoolGraduated`` and the first Uniswap v4 swap share one transaction hash. Any design
    that plans to react to a graduation needs this test to fail first.
    """
    receipt = graduation_fx["receipt"]
    topics = [log["topics"][0] for log in receipt["logs"]]
    v4_initialize = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"
    v4_swap = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
    assert rh.TOPIC_CURVE_BUY in topics, "the buy that crossed the threshold"
    assert rh.TOPIC_POOL_GRADUATED in topics
    assert v4_initialize in topics, "the v4 pool is created in this same transaction"
    assert v4_swap in topics, "and traded in it"
    # One transaction, not several.
    assert {log["transactionHash"] for log in receipt["logs"]} == {graduation_fx["log"]["transactionHash"]}


# ---------------------------------------------------------------------------- parsers


def test_parse_token_launched_reads_a_real_log(launch_fx):
    parsed = rh.parse_token_launched(launch_fx["log"], ts_ms=1_789_897_359_000)
    assert parsed is not None
    token, meta = parsed
    assert token.address == launch_fx["token"]
    assert token.chain is Chain.ROBINHOOD
    assert token.launchpad == "pons"
    assert token.pool == launch_fx["curve"] == meta.curve
    assert token.creator == meta.deployer
    assert token.created_ms == 1_789_897_359_000
    assert meta.graduation_threshold > 0
    assert token.meta["factory"] == rh.FACTORY_V2


def test_parsed_addresses_are_lowercased(launch_fx):
    """docs/CONTRACT.md rule 4: EVM addresses are lowercased on the way in."""
    token, meta = rh.parse_token_launched(launch_fx["log"])
    for value in (token.address, token.creator, token.pool, meta.curve, meta.token,
                  meta.deployer, meta.pair_token):
        assert value == value.lower()


def test_parse_token_launched_refuses_a_truncated_log(launch_fx):
    """A launch we cannot fully read is None, never a half-filled row."""
    broken = dict(launch_fx["log"])
    broken["topics"] = broken["topics"][:2]
    assert rh.parse_token_launched(broken) is None
    no_data = dict(launch_fx["log"], data="0x")
    assert rh.parse_token_launched(no_data) is None
    assert rh.parse_token_launched({"topics": [rh.TOPIC_CURVE_BUY]}) is None
    assert rh.parse_token_launched(None) is None
    assert rh.parse_token_launched("not a log") is None


def test_parse_curve_trade_orients_buy_and_sell_amounts(window_fx):
    """The amount fields swap meaning by side; the fee is the cross-check.

    The fee is charged on the **quote** leg in both directions, so ``amount_native`` and
    ``fee`` are denominated in the same token and their ratio is a percentage. Get the
    orientation backwards and ``fee`` would be compared against a token amount instead:
    these tokens have 1e27 supplies, so that ratio collapses to ~1e-8 rather than landing
    in a percentage band. The band is deliberately wide because ``curveFeeBps`` is
    per-launch-config, not a constant — observed values run from 1% to over 7%.
    """
    meta_by_curve = {}
    for log in _launch_logs(window_fx):
        _, meta = rh.parse_token_launched(log)
        meta_by_curve[meta.curve] = meta

    checked = {"buy": 0, "sell": 0}
    for log in _trade_logs(window_fx):
        meta = meta_by_curve.get(log["address"].lower())
        if meta is None or not meta.quote_is_native:
            continue
        row = rh.parse_curve_trade(log, meta=meta, ts_ms=1_700_000_000_000)
        assert row is not None
        fee = row["fee"]
        if not fee:
            continue
        quote = int(row["amount_native"])
        token_leg = int(row["amount_token"])
        if not quote or not token_leg:
            continue
        fee_share = Decimal(fee) / Decimal(quote)
        assert Decimal("0.0001") < fee_share < Decimal("0.5"), (
            f"{row['side']}: fee {fee} is {fee_share} of the leg called amount_native "
            f"({quote}) - that leg is not the quote leg"
        )
        assert Decimal(fee) / Decimal(token_leg) < Decimal("1e-6"), (
            f"{row['side']}: the fee is comparable to amount_token, so the legs are swapped"
        )
        checked[row["side"]] += 1
    assert checked["buy"] and checked["sell"], f"needed both sides, got {checked}"


def test_parse_curve_trade_fills_the_swap_row_shape(window_fx):
    _, meta, trades = _traded_launch(window_fx)
    row = rh.parse_curve_trade(trades[0], meta=meta, ts_ms=1_700_000_000_000)
    assert row["chain"] == "robinhood"
    assert row["token"] == meta.token
    assert row["side"] in ("buy", "sell")
    assert row["program"] == "pons-curve"
    assert row["source"] == "robinhood"
    assert row["wallet"].startswith("0x") and row["wallet"] == row["wallet"].lower()
    assert isinstance(row["amount_token"], str) and isinstance(row["amount_native"], str)
    assert int(row["amount_native"]) >= 0


def test_an_unpriced_trade_has_usd_none_not_zero(window_fx):
    """A zero would read to lanes._net_buyers as a real, tiny fill."""
    _, meta, trades = _traded_launch(window_fx)
    row = rh.parse_curve_trade(trades[0], meta=meta, ts_ms=1, eth_usd=None)
    assert row["usd_value"] is None
    assert row["price_usd"] is None


def test_usd_value_is_computed_from_the_eth_leg_when_a_price_exists(window_fx):
    _, meta, trades = _traded_launch(window_fx)
    if not meta.quote_is_native:
        pytest.skip("the traded launch in this window is not ETH-quoted")
    row = rh.parse_curve_trade(trades[0], meta=meta, ts_ms=1, eth_usd=Decimal("2575"))
    expected = (Decimal(int(row["amount_native"])) / rh.WEI) * Decimal("2575")
    assert Decimal(row["usd_value"]) == expected


def test_a_non_native_quote_is_never_priced_as_eth(window_fx):
    """Wei arithmetic on a 6-decimal quote token overstates value by 1e12."""
    _, native, trades = _traded_launch(window_fx)
    foreign = rh.CurveMeta(
        curve=native.curve, token=native.token, deployer=native.deployer,
        pair_token="0x5fc5360d0400a0fd4f2af552add042d716f1d168",
        launch_config_id=0, graduation_threshold=1, launched_block=1,
    )
    assert foreign.quote_is_native is False
    row = rh.parse_curve_trade(trades[0], meta=foreign, ts_ms=1, eth_usd=Decimal("2575"))
    assert row["usd_value"] is None
    assert row["quote_token"] == foreign.pair_token


def test_parse_pool_graduated_reads_a_real_graduation(graduation_fx):
    info = rh.parse_pool_graduated(graduation_fx["log"], ts_ms=1_700_000_000_000)
    assert info is not None
    assert info["mint"] == "0x" + graduation_fx["log"]["topics"][1][-40:]
    assert info["pool"] == "uniswap-v4"
    assert info["pool_address"] == rh.POOL_MANAGER
    assert info["pair_token_amount"] > 0
    assert info["migrated_ms"] == 1_700_000_000_000


# ------------------------------------------------------------------- curve mathematics


def test_parse_curve_state_reads_a_fresh_launch(launch_fx):
    state = rh.parse_curve_state(
        launch_fx["curve_state_raw"], curve=launch_fx["curve"], token=launch_fx["token"],
        quote_is_native=True,
    )
    assert state is not None
    assert state.real_quote_reserve == 0
    assert state.quote_reserve == state.phantom_quote
    assert state.launch_supply > 0
    assert state.fee_bps == 100
    assert state.graduated is False


def test_the_constant_product_holds_on_recorded_reserves(mid_raise_fx):
    """quoteReserve * curveTokenBalance == phantomQuote * launchSupply.

    This is the invariant the whole curve model rests on, and it is not the one the
    obvious reading gives: the token leg is the curve's **entire** balance, not
    ``sellableTokens()``. Getting this wrong makes every derived price and progress figure
    wrong, so it is pinned against real numbers.
    """
    state = rh.parse_curve_state(
        mid_raise_fx["curve_state_raw"], curve=mid_raise_fx["curve"],
        token=mid_raise_fx["token"], quote_is_native=True,
    )
    assert state is not None and state.real_quote_reserve > 0
    k_now = state.quote_reserve * state.token_reserve
    k_0 = state.phantom_quote * state.launch_supply
    assert abs(k_now - k_0) / k_0 < Decimal("1e-8")


def test_reserved_tokens_are_exactly_what_the_threshold_leaves(launch_fx):
    """reserved/supply == phantom/(phantom+threshold).

    The consequence is the interesting part: ``sellableTokens`` hits zero exactly when the
    raise hits the graduation threshold, so the reserved block is precisely what seeds the
    Uniswap v4 pool. This is a designed curve, not an emergent one.
    """
    state = rh.parse_curve_state(launch_fx["curve_state_raw"], curve=launch_fx["curve"],
                                 token=launch_fx["token"], quote_is_native=True)
    lhs = Decimal(state.reserved_tokens) / Decimal(state.launch_supply)
    rhs = Decimal(state.phantom_quote) / Decimal(state.phantom_quote + state.graduation_threshold)
    assert abs(lhs - rhs) < Decimal("1e-15")


def test_parse_curve_state_refuses_a_partial_read(launch_fx):
    raw = list(launch_fx["curve_state_raw"])
    assert rh.parse_curve_state(raw[:4], curve=launch_fx["curve"], token=launch_fx["token"],
                                quote_is_native=True) is None
    broken = list(raw)
    broken[4] = "0x"  # graduationThreshold unreadable
    assert rh.parse_curve_state(broken, curve=launch_fx["curve"], token=launch_fx["token"],
                                quote_is_native=True) is None


def test_curve_dict_uses_the_keys_the_scanner_emits(mid_raise_fx):
    """The contract with ``scanner.curve_from_payload``: same keys, so the same consumers.

    The names are Solana's. On this chain ``sol_in_curve`` is ETH and
    ``sol_in_curve_lamports`` is wei; that is documented in the module and preferred over
    renaming ``scanner.py`` and ``lanes.py``, which this task does not own.
    """
    state = rh.parse_curve_state(mid_raise_fx["curve_state_raw"], curve=mid_raise_fx["curve"],
                                 token=mid_raise_fx["token"], quote_is_native=True)
    curve, basis = rh.curve_from_state(state, swaps=12, swaps_basis="observed")
    assert curve is not None
    shared = {
        "progress_pct", "progress_basis", "sol_in_curve", "sol_in_curve_lamports",
        "sol_per_min", "swaps", "swaps_basis", "graduation_sol",
        "sol_raised_pct_of_graduation", "virtual_sol_initial", "real_token_initial",
        "classic_curve", "virtual_sol_reserves", "virtual_token_reserves",
        "real_token_reserves", "created_ms", "age_s", "last_trade_ms", "market_cap_usd",
        "source", "observed_ms",
    }
    assert shared <= set(curve), f"missing {shared - set(curve)}"
    assert isinstance(curve["progress_pct"], Decimal)
    assert isinstance(curve["sol_in_curve"], Decimal)
    assert isinstance(curve["sol_in_curve_lamports"], int)
    assert curve["swaps"] == 12
    assert basis == "quote_per_swap"


def test_curve_progress_is_the_raise_against_the_threshold(mid_raise_fx):
    state = rh.parse_curve_state(mid_raise_fx["curve_state_raw"], curve=mid_raise_fx["curve"],
                                 token=mid_raise_fx["token"], quote_is_native=True)
    curve, _ = rh.curve_from_state(state)
    expected = (Decimal(state.real_quote_reserve) / Decimal(state.graduation_threshold)) * 100
    assert curve["progress_pct"] == expected
    assert 0 <= curve["progress_pct"] <= 100
    assert curve["quote_raised_wei"] == state.real_quote_reserve


def test_an_absent_swap_count_is_absent_not_zero(mid_raise_fx):
    state = rh.parse_curve_state(mid_raise_fx["curve_state_raw"], curve=mid_raise_fx["curve"],
                                 token=mid_raise_fx["token"], quote_is_native=True)
    curve, basis = rh.curve_from_state(state, swaps=None)
    assert "swaps" not in curve
    assert basis in ("quote_per_min", "none")


def test_a_graduated_curve_produces_no_curve_dict(mid_raise_fx):
    state = rh.parse_curve_state(mid_raise_fx["curve_state_raw"], curve=mid_raise_fx["curve"],
                                 token=mid_raise_fx["token"], quote_is_native=True)
    done = rh.CurveState(**{**state.__dict__, "real_quote_reserve": state.graduation_threshold})
    curve, note = rh.curve_from_state(done)
    assert curve is None and note == "curve_complete"


def test_snipe_tax_decays_from_the_recorded_start_to_zero(launch_fx):
    """The recorded launch really does open at 9900 bps for 3 seconds."""
    state = rh.parse_curve_state(launch_fx["curve_state_raw"], curve=launch_fx["curve"],
                                 token=launch_fx["token"], quote_is_native=True)
    assert state.snipe_tax_start_bps == 9900
    assert state.snipe_tax_seconds == 3
    assert state.snipe_tax_bps_at(state.launched_at_s) == 9900
    assert state.snipe_tax_bps_at(state.launched_at_s + 3) == 0
    assert state.snipe_tax_bps_at(state.launched_at_s + 300) == 0
    assert 0 < state.snipe_tax_bps_at(state.launched_at_s + 2) < 9900


# ------------------------------------------------------------------------- persistence


def test_record_new_token_writes_the_row_and_the_event(tmp_db, launch_fx):
    token, _ = rh.parse_token_launched(launch_fx["log"], ts_ms=1_700_000_000_000)
    assert rh.record_new_token(token, conn=tmp_db) is True
    row = fetch_one(tmp_db, "SELECT * FROM tokens WHERE chain=? AND address=?",
                    ("robinhood", token.address))
    assert row["launchpad"] == "pons"
    assert row["creator"] == token.creator
    assert row["pool"] == token.pool
    assert row["created_ms"] == 1_700_000_000_000
    events = fetch_all(tmp_db, "SELECT * FROM events WHERE kind=?",
                       (EventKind.TOKEN_CREATED.value,))
    assert len(events) == 1
    assert json.loads(events[0]["payload"])["mint"] == token.address


def test_record_new_token_is_idempotent(tmp_db, launch_fx):
    token, _ = rh.parse_token_launched(launch_fx["log"], ts_ms=1)
    assert rh.record_new_token(token, conn=tmp_db) is True
    assert rh.record_new_token(token, conn=tmp_db) is False
    assert len(fetch_all(tmp_db, "SELECT * FROM events WHERE kind=?",
                         (EventKind.TOKEN_CREATED.value,))) == 1
    assert len(fetch_all(tmp_db, "SELECT * FROM tokens", ())) == 1


def test_record_migration_stamps_the_token_without_clobbering_it(tmp_db, launch_fx,
                                                                 graduation_fx):
    token, _ = rh.parse_token_launched(launch_fx["log"], ts_ms=1)
    rh.record_new_token(token, conn=tmp_db)
    info = dict(rh.parse_pool_graduated(graduation_fx["log"], ts_ms=999))
    info["mint"] = token.address
    assert rh.record_migration(info, conn=tmp_db) is True
    row = fetch_one(tmp_db, "SELECT * FROM tokens WHERE address=?", (token.address,))
    assert row["migrated_ms"] == 999
    assert row["creator"] == token.creator, "creation data must survive the migration write"
    assert rh.record_migration(info, conn=tmp_db) is False


def test_record_trade_writes_a_swap_and_dedupes(tmp_db, window_fx):
    _, meta, trades = _traded_launch(window_fx)
    row = rh.parse_curve_trade(trades[0], meta=meta, ts_ms=1_700_000_000_000)
    assert rh.record_trade(row, conn=tmp_db) is True
    assert rh.record_trade(row, conn=tmp_db) is False
    swaps = fetch_all(tmp_db, "SELECT * FROM swaps", ())
    assert len(swaps) == 1
    assert swaps[0]["chain"] == "robinhood"
    assert swaps[0]["token"] == meta.token
    assert swaps[0]["source"] == "robinhood"
    assert len(fetch_all(tmp_db, "SELECT * FROM events WHERE kind=?",
                         (EventKind.WALLET_TRADE.value,))) == 1


def test_two_trades_in_one_transaction_are_both_kept(tmp_db, window_fx):
    """A router can buy several tokens in one tx; log index is what separates them."""
    _, meta, trades = _traded_launch(window_fx)
    first = rh.parse_curve_trade(trades[0], meta=meta, ts_ms=1)
    second = dict(first, block_index=(first["block_index"] or 0) + 1, amount_token="12345")
    assert rh.record_trade(first, conn=tmp_db) is True
    assert rh.record_trade(second, conn=tmp_db) is True
    assert len(fetch_all(tmp_db, "SELECT * FROM swaps", ())) == 2


# -------------------------------------------------------------------------- the index


def test_curve_index_maps_both_directions_and_is_bounded(launch_fx):
    _, meta = rh.parse_token_launched(launch_fx["log"])
    index = rh.CurveIndex(max_entries=2)
    index.add(meta)
    assert index.get(meta.curve) is meta
    assert index.get(meta.curve.upper()) is meta
    assert index.by_token(meta.token) is meta
    for i in range(5):
        index.add(rh.CurveMeta(curve=f"0x{i:040x}", token=f"0x{i + 100:040x}",
                               deployer="0x" + "1" * 40, pair_token=rh.ZERO_ADDRESS,
                               launch_config_id=0, graduation_threshold=1, launched_block=1))
    assert len(index) == 2
    assert index.get(meta.curve) is None, "oldest evicted first"


# --------------------------------------------------------------------------- the poll


class FakeRpc:
    """Replays a recorded window in place of :func:`robinhood.rpc_batch`."""

    def __init__(self, window: dict, *, head: int | None = None):
        self.window = window
        self.head = head if head is not None else window["to_block"]
        self.calls: list[str] = []

    def _in_range(self, logs, query):
        """Honour fromBlock/toBlock, as the real node does.

        Worth the few lines: a fake that returns the whole window whatever it was asked
        for hides every cursor and span bug there is.
        """
        lo = int(query["fromBlock"], 16)
        hi = self.head if query.get("toBlock") == "latest" else int(query["toBlock"], 16)
        kept = []
        for x in logs:
            # Malformed entries are passed straight through: the node would not send
            # them, but a test that injects one is checking that poll_once survives it.
            if not isinstance(x, dict) or "blockNumber" not in x:
                kept.append(x)
                continue
            if lo <= int(x["blockNumber"], 16) <= hi:
                kept.append(x)
        return kept

    def __call__(self, calls, *, endpoint="chain.batch", **kw):
        self.calls.append(endpoint)
        results: list = []
        for method, params in calls:
            if method == "eth_blockNumber":
                results.append(hex(self.head))
            elif method == "eth_getBlockByNumber":
                block_hex = params[0]
                ts = self.window["block_timestamps"].get(block_hex)
                results.append({"timestamp": ts} if ts else None)
            elif method == "eth_getLogs":
                query = params[0]
                source = (self.window["factory_logs"]
                          if query.get("address") == rh.FACTORY_V2
                          else self.window["curve_logs"])
                results.append(self._in_range(source, query))
            else:
                results.append(None)
        return rh.RpcResult(results, True)


@pytest.fixture
def no_price(monkeypatch):
    """Ingest tests must not reach a price provider."""
    monkeypatch.setattr(rh.EthPrice, "get", lambda self, conn=None: None)


@pytest.fixture
def full_budget(monkeypatch):
    """Lift the per-poll timestamp cap.

    The recorded window is 600 blocks wide with logs in 101 of them, which is far more
    than a live 3-second poll (~30 blocks) ever sees, so the cap would otherwise split it
    across several polls and obscure what these tests are actually about. The cap itself
    is the subject of its own tests.
    """
    monkeypatch.setattr(rh, "_MAX_TS_BLOCKS_PER_POLL", 10_000)


def test_poll_once_writes_tokens_trades_and_events(tmp_db, window_fx, monkeypatch, no_price, full_budget):
    fake = FakeRpc(window_fx)
    monkeypatch.setattr(rh, "rpc_batch", fake)
    index = rh.CurveIndex()

    result = rh.poll_once(from_block=window_fx["from_block"], index=index, conn=tmp_db)

    assert result.ok
    assert result.launches == len(_launch_logs(window_fx))
    assert result.trades > 0
    tokens = fetch_all(tmp_db, "SELECT * FROM tokens", ())
    assert len(tokens) == result.launches
    assert {t["launchpad"] for t in tokens} == {"pons"}
    assert {t["chain"] for t in tokens} == {"robinhood"}
    swaps = fetch_all(tmp_db, "SELECT * FROM swaps", ())
    assert len(swaps) == result.trades
    created = fetch_all(tmp_db, "SELECT * FROM events WHERE kind=?",
                        (EventKind.TOKEN_CREATED.value,))
    assert len(created) == result.launches


def test_a_polls_rpc_cost_is_hard_capped(tmp_db, window_fx, monkeypatch, no_price):
    """A poll is one log batch plus the timestamps its own logs need, and nothing else.

    This is the property that keeps the listener alive. The provider has no
    ``limiter.DEFAULTS`` entry, so it refills at 1 call/s and answers a crossing with a
    60-second cooldown on the whole ``chain.*`` family. When the timestamp fetch was
    unbounded, a cooldown created a backlog, the backlog needed ~15 timestamp batches,
    and those triggered the next cooldown — two live runs span in that loop.

    The fixture window is 600 blocks with logs in 101 of them, only one of which carries
    a usable ``blockTimestamp``, so this is very much the expensive case.
    """
    fake = FakeRpc(window_fx)
    monkeypatch.setattr(rh, "rpc_batch", fake)
    result = rh.poll_once(from_block=window_fx["from_block"], index=rh.CurveIndex(),
                          conn=tmp_db)
    assert fake.calls[0] == "chain.logs", "head and both log queries ride in one batch"
    assert fake.calls.count("chain.logs") == 1, "exactly one log query batch per poll"
    blocks = {int(x["blockNumber"], 16)
              for x in window_fx["factory_logs"] + window_fx["curve_logs"]}
    assert result.rpc_calls <= 1 + -(-len(blocks) // 40), "one log batch plus its timestamps"


def test_an_unstampable_poll_pulls_its_cursor_back_rather_than_inventing_a_time(
    tmp_db, window_fx, monkeypatch, no_price
):
    """When the timestamp budget runs out, the un-stamped blocks are re-read next poll.

    The three wrong answers here are: invent a timestamp (every lane window then treats
    a fabricated number as measured), write ``ts_ms = 0`` (the trade reads as 56 years
    old), or advance the cursor past logs we never wrote (silent data loss). The right
    one is to advance only as far as we can honestly stamp.
    """
    # A cap derived from the fixture: big enough to reach the first launch, far too small
    # for the whole 101-block window, so the cursor must be held back mid-window.
    blocks = sorted({int(x["blockNumber"], 16)
                     for x in window_fx["factory_logs"] + window_fx["curve_logs"]})
    first_launch = min(int(x["blockNumber"], 16) for x in _launch_logs(window_fx))
    cap = blocks.index(first_launch) + 2
    assert cap < len(blocks), "must not cover the whole window"
    monkeypatch.setattr(rh, "_MAX_TS_BLOCKS_PER_POLL", cap)
    fake = FakeRpc(window_fx)
    monkeypatch.setattr(rh, "rpc_batch", fake)
    result = rh.poll_once(from_block=window_fx["from_block"], index=rh.CurveIndex(),
                          conn=tmp_db)
    assert result.to_block < window_fx["to_block"], "cursor held back"
    assert result.to_block >= result.from_block, "but it still made progress"
    assert "timestamp budget" in (result.note or "")
    # Everything written carries a real block time, and nothing beyond the cursor was
    # written at all - those logs are re-read next poll.
    stamped = {int(h, 16): int(t, 16) * 1000 for h, t in window_fx["block_timestamps"].items()}
    real_times = set(stamped.values())
    tokens = fetch_all(tmp_db, "SELECT created_ms FROM tokens", ())
    assert tokens, "the affordable prefix of the window was still ingested"
    for row in tokens:
        assert row["created_ms"] in real_times, "a real block time, not 'now'"
    for row in fetch_all(tmp_db, "SELECT ts_ms, slot FROM swaps", ()):
        assert row["slot"] <= result.to_block
        assert row["ts_ms"] == stamped[row["slot"]]


def test_block_timestamps_are_cached_across_polls(tmp_db, window_fx, monkeypatch, no_price):
    """The head race is resolved by re-reading blocks, so re-fetching them must be free.

    Without this, every overlapping poll re-bought timestamps it already had — and RPC
    calls are the scarce resource here, not bandwidth: exhausting the limiter's bucket
    costs a 60-second family cooldown, which is what actually drives detection latency.
    """
    fake = FakeRpc(window_fx)
    monkeypatch.setattr(rh, "rpc_batch", fake)
    known = [int(h, 16) for h in window_fx["block_timestamps"]]
    assert known, "fixture must carry some block timestamps"

    stamps, calls = rh._block_timestamps(known, conn=tmp_db)
    assert calls > 0 and len(stamps) == len(known)

    again, calls_again = rh._block_timestamps(known, conn=tmp_db)
    assert calls_again == 0, "a cached block must cost nothing"
    assert again == stamps




def test_a_normal_sized_poll_costs_two_http_calls(tmp_db, window_fx, monkeypatch, no_price):
    """The live case: a ~20-block window is one log batch plus one timestamp batch."""
    narrow_lo = window_fx["to_block"] - 20
    narrow = dict(
        window_fx,
        from_block=narrow_lo,
        factory_logs=[x for x in window_fx["factory_logs"]
                      if int(x["blockNumber"], 16) >= narrow_lo],
        curve_logs=[x for x in window_fx["curve_logs"]
                    if int(x["blockNumber"], 16) >= narrow_lo],
    )
    monkeypatch.setattr(rh, "rpc_batch", FakeRpc(narrow))
    result = rh.poll_once(from_block=narrow_lo, index=rh.CurveIndex(), conn=tmp_db)
    assert result.rpc_calls <= 2


def test_timestamps_are_taken_from_the_log_when_the_node_supplies_one(window_fx):
    """Nitro stamps logs near the head and returns ``0x0`` further back.

    That is the opposite of the intuition, and it was measured: ``head-30..latest``
    returned 6 of 8 entries stamped, while ``head-100..head-50`` and everything older
    returned ``0x0`` throughout. So the field is an opportunistic saving, never a
    design — which is why the fallback exists and is capped rather than removed.

    ``0x0`` must read as *absent*: taken literally it is the Unix epoch, and a swap row
    stamped 1970 is not a small error, it is a trade that every lane window mis-ages by
    half a century.
    """
    entries = window_fx["factory_logs"] + window_fx["curve_logs"]
    assert all("blockTimestamp" in e for e in entries), "fixture is real RPC output"
    usable = [e for e in entries if rh.log_timestamp_ms(e) is not None]
    assert usable, "at least the head block should be stamped"
    one = usable[0]
    assert rh.log_timestamp_ms(one) == int(one["blockTimestamp"], 16) * 1000
    assert rh.log_timestamp_ms({"blockTimestamp": "0x0"}) is None
    assert rh.log_timestamp_ms({}) is None


def test_poll_once_skips_trades_on_curves_it_has_never_seen(tmp_db, window_fx, monkeypatch,
                                                            no_price):
    """A trade names its curve, not its token. Without a mapping we skip, never guess."""
    stripped = dict(window_fx, factory_logs=[])
    fake = FakeRpc(stripped)
    monkeypatch.setattr(rh, "rpc_batch", fake)
    result = rh.poll_once(from_block=stripped["from_block"], index=rh.CurveIndex(),
                          conn=tmp_db)
    assert result.launches == 0
    assert result.trades == 0
    in_range = [x for x in _trade_logs(stripped)
                if int(x["blockNumber"], 16) <= result.to_block]
    assert in_range, "the window must actually contain trades"
    assert result.skipped_unknown_curve == len(in_range)
    assert fetch_all(tmp_db, "SELECT * FROM swaps", ()) == []


def test_poll_once_advances_past_the_head_it_read(tmp_db, window_fx, monkeypatch, no_price, full_budget):
    fake = FakeRpc(window_fx, head=window_fx["to_block"])
    monkeypatch.setattr(rh, "rpc_batch", fake)
    result = rh.poll_once(from_block=window_fx["from_block"], index=rh.CurveIndex(),
                          conn=tmp_db)
    assert result.to_block == window_fx["to_block"]
    assert result.head == window_fx["to_block"]


def test_poll_once_bounds_its_span_when_far_behind(tmp_db, window_fx, monkeypatch, no_price):
    """After an outage the catch-up is chunked, not one enormous query."""
    captured: list[dict] = []

    def rpc(calls, *, endpoint="chain.batch", **kw):
        for method, params in calls:
            if method == "eth_getLogs":
                captured.append(params[0])
        return FakeRpc(window_fx, head=window_fx["from_block"] + 500_000)(
            calls, endpoint=endpoint, **kw)

    monkeypatch.setattr(rh, "rpc_batch", rpc)
    config = rh.ListenerConfig(max_span_blocks=1_000)
    result = rh.poll_once(from_block=window_fx["from_block"], index=rh.CurveIndex(),
                          conn=tmp_db, config=config,
                          head=window_fx["from_block"] + 500_000)
    # The span bound is visible in the query it sent; the cursor may then be held
    # further back by the timestamp budget, which is a separate mechanism.
    assert all(q["toBlock"] != "latest" for q in captured)
    assert int(captured[0]["toBlock"], 16) == window_fx["from_block"] + 999
    assert result.to_block <= window_fx["from_block"] + 999
    assert result.lag_blocks > 0


def test_poll_once_reports_a_failure_instead_of_raising(tmp_db, monkeypatch, no_price):
    monkeypatch.setattr(rh, "rpc_batch",
                        lambda calls, **kw: rh.RpcResult([], False, "connection refused"))
    result = rh.poll_once(from_block=100, index=rh.CurveIndex(), conn=tmp_db)
    assert result.ok is False
    assert "connection refused" in (result.note or "")


def test_poll_once_survives_a_malformed_log(tmp_db, window_fx, monkeypatch, no_price, full_budget):
    """One bad entry must not cost us the rest of the window."""
    poisoned = dict(window_fx)
    poisoned["factory_logs"] = [{"topics": ["0xdeadbeef"], "data": "0x"}, "not a dict",
                                *window_fx["factory_logs"]]
    monkeypatch.setattr(rh, "rpc_batch", FakeRpc(poisoned))
    result = rh.poll_once(from_block=poisoned["from_block"], index=rh.CurveIndex(),
                          conn=tmp_db)
    assert result.ok
    assert result.launches == len(_launch_logs(window_fx))


def test_poll_once_hands_launches_to_tier0_triage(tmp_db, window_fx, monkeypatch, no_price, full_budget):
    """The whole point: tier-0 must screen these exactly as it screens pump.fun launches."""
    monkeypatch.setattr(rh, "rpc_batch", FakeRpc(window_fx))
    result = rh.poll_once(from_block=window_fx["from_block"], index=rh.CurveIndex(),
                          conn=tmp_db)
    decisions = fetch_all(tmp_db, "SELECT * FROM triage_decisions", ())
    assert len(decisions) == result.launches
    assert {d["chain"] for d in decisions} == {"robinhood"}
    assert all(d["token"] for d in decisions), "triage must not drop the 0x address"


def test_triage_sees_a_robinhood_launch_as_robinhood(tmp_db, launch_fx):
    """triage.parse_launch defaults to Solana and applies looks_solana to the mint.

    If the chain were not passed through, every 0x address would be discarded and tier 0
    would screen a launch with no token at all.
    """
    from kaiba.execution.triage import parse_launch

    token, _ = rh.parse_token_launched(launch_fx["log"], ts_ms=1)
    rh._screen(token, tmp_db)
    facts = parse_launch({"chain": "robinhood", "mint": token.address, "launchpad": "pons"})
    assert facts.chain is Chain.ROBINHOOD
    assert facts.mint == token.address
    decision = fetch_one(tmp_db, "SELECT * FROM triage_decisions WHERE token=?",
                         (token.address,))
    assert decision is not None


def test_screen_never_raises_when_triage_explodes(tmp_db, launch_fx, monkeypatch):
    """Losing the ingest row to a screening bug is strictly worse than not screening."""
    import kaiba.execution.triage as triage

    def boom(*a, **kw):
        raise RuntimeError("triage exploded")

    monkeypatch.setattr(triage, "screen_launch", boom)
    token, _ = rh.parse_token_launched(launch_fx["log"], ts_ms=1)
    rh._screen(token, tmp_db)  # must not raise


# ------------------------------------------------------------------------- the runner


def test_run_polls_then_stops(tmp_db, window_fx, monkeypatch, no_price, full_budget):
    monkeypatch.setattr(rh, "rpc_batch", FakeRpc(window_fx))
    seen: list[rh.PollResult] = []

    async def go():
        return await rh.run(conn=tmp_db, start_block=window_fx["from_block"], max_polls=2,
                            config=rh.ListenerConfig(poll_interval_s=0.0),
                            on_poll=seen.append)

    totals = asyncio.run(go())
    assert totals["polls"] == 2
    assert totals["launches"] == len(_launch_logs(window_fx))  # second poll is a no-op
    assert len(seen) == 2


def test_run_retries_a_failing_poll_without_dying(tmp_db, monkeypatch, no_price):
    calls = {"n": 0}

    def flaky(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return rh.PollResult(0, 1, 0, ok=False, note="boom")
        return rh.PollResult(10, 1, 10)

    monkeypatch.setattr(rh, "backoff_delay", lambda *a, **kw: 0.0)

    async def go():
        return await rh.run(conn=tmp_db, start_block=1, max_polls=2, poll=flaky,
                            config=rh.ListenerConfig(poll_interval_s=0.0))

    totals = asyncio.run(go())
    assert totals["errors"] == 1
    assert totals["polls"] == 2


def test_run_feeds_the_last_known_head_back_into_the_next_poll(tmp_db, no_price):
    """So a poll that has fallen behind bounds its own span.

    Without this, the catch-up after an outage is one enormous ``eth_getLogs``, and the
    topic-only curve query hits the RPC's result cap long before an hour of blocks - which
    fails forever instead of catching up.
    """
    heads: list[int | None] = []

    def spy(**kw):
        heads.append(kw.get("head"))
        return rh.PollResult(head=5_000 + len(heads), from_block=kw["from_block"],
                             to_block=5_000 + len(heads))

    async def go():
        return await rh.run(conn=tmp_db, start_block=1, max_polls=3, poll=spy,
                            config=rh.ListenerConfig(poll_interval_s=0.0))

    asyncio.run(go())
    assert heads[0] == 0, "first poll knows only what start_block implies"
    assert heads[1] == 5_001, "subsequent polls carry the head the previous one read"
    assert heads[2] == 5_002


def test_the_listener_is_registered_with_the_ingest_runner():
    from kaiba.ingest import runner

    assert "robinhood" in runner.REGISTRY


def test_latency_tracking_records_the_block_to_receive_gap(tmp_db, window_fx, monkeypatch,
                                                           no_price, full_budget):
    monkeypatch.setattr(rh, "rpc_batch", FakeRpc(window_fx))
    rh.poll_once(from_block=window_fx["from_block"], index=rh.CurveIndex(), conn=tmp_db)
    stats = rh.latency_stats()
    assert stats["streams"], "a poll with launches must produce latency samples"
    assert rh.STREAM_LAUNCH in stats["streams"]
    assert stats["streams"][rh.STREAM_LAUNCH]["samples"] > 0


# --------------------------------------------------------------------------- live only


# The live tests take ``tmp_db`` because the limiter keeps its token bucket and family
# cooldowns in the database: without a migrated one, every guarded call fails on
# "no such table: provider_state" and the test looks like a chain problem.


@pytest.mark.live
def test_live_chain_identity(tmp_db):
    got = rh.rpc_batch([("eth_chainId", []), ("web3_clientVersion", [])], conn=tmp_db)
    assert got.ok
    assert int(got.results[0], 16) == rh.CHAIN_ID == 4663


@pytest.mark.live
def test_live_factory_still_emits_launches(tmp_db):
    """If Pons redeploys its factory, this fails and the constants need revisiting."""
    head_res = rh.rpc_batch([("eth_blockNumber", [])], conn=tmp_db)
    head = int(head_res.results[0], 16)
    got = rh.rpc_batch([rh.get_logs_call(
        from_block=head - 20_000, to_block=head, address=rh.FACTORY_V2,
        topics=[[rh.TOPIC_TOKEN_LAUNCHED]])], conn=tmp_db)
    logs = got.results[0]
    assert isinstance(logs, list) and logs, "no Pons launches in ~34 minutes of blocks"
    parsed = rh.parse_token_launched(logs[-1])
    assert parsed is not None


@pytest.mark.live
def test_live_curve_reads_back_and_the_invariant_holds(tmp_db):
    head = int(rh.rpc_batch([("eth_blockNumber", [])], conn=tmp_db).results[0], 16)
    logs = rh.rpc_batch([rh.get_logs_call(
        from_block=head - 20_000, to_block=head, address=rh.FACTORY_V2,
        topics=[[rh.TOPIC_TOKEN_LAUNCHED]])], conn=tmp_db).results[0]
    token, meta = rh.parse_token_launched(logs[-1])
    state = rh.read_curve(meta.curve, meta.token, quote_is_native=meta.quote_is_native,
                          conn=tmp_db)
    assert state is not None
    k_now = state.quote_reserve * state.token_reserve
    k_0 = state.phantom_quote * state.launch_supply
    assert abs(k_now - k_0) / k_0 < Decimal("1e-7")
