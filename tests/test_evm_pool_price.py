"""Pool quotes for robinhood and bsc paper, shaped for protection (design 2026-10-21 step 3a/3c).

Fixtures are real chain reads taken 2026-10-21 (``tests/fixtures/evm_pool_price``): a bsc
PancakeSwap v2 pair from a live sm-trenches paper position, a deep PancakeSwap v3 pool for the
QuoterV2 path, and two Pons graduations on robinhood (Uniswap v4 with the Pons hook) read from
the box in one JSON-RPC batch. Token, pool and hook addresses are synthetic; every number is as
read.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from kaiba.core.db import fetch_one, jdump
from kaiba.core.schemas import Chain, LaneMode, now_ms
from kaiba.execution import evm_pool_price as m
from kaiba.execution import onchain_pool as op
from kaiba.execution import watchdog as wd
from kaiba.execution.paper import load_position
from kaiba.execution.policy import selector_of
from tests.test_watchdog import FakeSource, RecordingSubmitter, make_position, risk_file  # noqa: F401

FX = json.loads((Path(__file__).parent / "fixtures" / "evm_pool_price" / "pool_reads_20261021.json").read_text())
BNB_USD = Decimal("600")
ETH_USD = Decimal("2500")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """Also stubs the robinhood pool SAMPLER (``onchain_pool.after_tick``): ``config/risk.yaml`` ships
    ``protection.onchain_price_log: true``, so a ``Watchdog.tick`` over robinhood positions would start
    its real worker thread -- a real HTTP post to the robinhood RPC, on a test database that teardown
    then closes under it (a Windows access violation, seen 5 runs in 6 before this stub)."""
    monkeypatch.setattr(op, "after_tick", lambda watchdog, positions, cfg: False)
    m.clear_caches()
    m.set_backup_quote(None)
    yield
    m.clear_caches()
    m.set_backup_quote(None)


class FakeRpc:
    """A transport answering from a table keyed ``(to, data)``; counts every SEND."""

    def __init__(self, table: dict[tuple[str, str], str | None] | None = None, *, default=None) -> None:
        self.table = {(t.lower(), d.lower()): r for (t, d), r in (table or {}).items()}
        self.default = default
        self.sends: list[list[tuple[str, str]]] = []

    def __call__(self, calls):
        self.sends.append(list(calls))
        return [self.table.get((t.lower(), d.lower()), self.default) for t, d in calls]


def _native(monkeypatch, usd: Decimal) -> None:
    monkeypatch.setattr(m, "fill_native_usd", lambda chain, conn, at_ms=None: (usd, "fixture"))
    monkeypatch.setattr(m, "mark_native_usd", lambda chain, conn: (usd, "fixture"))


# ----------------------------------------------------------------------------- selectors


@pytest.mark.parametrize("selector,signature", [
    (m.SEL_QUOTE_V3, "quoteExactInputSingle((address,address,uint256,uint24,uint160))"),
    (m.SEL_QUOTE_V4, "quoteExactInputSingle(((address,address,uint24,int24,address),bool,uint128,bytes))"),
    (m.SEL_POOL_AND_POSITION, "getPoolAndPositionInfo(uint256)"),
    (m.SEL_FACTORY, "factory()"), (m.SEL_FEE, "fee()"), (m.SEL_GET_PAIR, "getPair(address,address)"),
    (m.SEL_GET_RESERVES, "getReserves()"), (m.SEL_EXTSLOAD, "extsload(bytes32)"),
])
def test_selectors_are_the_functions_they_name(selector, signature):
    assert selector == selector_of(signature)


# ----------------------------------------------------------------------------- decoding real reads


def bsc_v2():
    fx = FX["bsc_v2"]
    cands, dec = m.bsc_candidates(fx["token"], fx["discovery"])
    marks, why = m.bsc_marks(fx["token"], cands, fx["candidates"], dec)
    return fx, cands, dec, marks, why


def test_a_real_pancake_v2_pair_decodes_to_its_reserves_and_price():
    fx, cands, dec, marks, why = bsc_v2()
    assert cands == [("v2", fx["pair"], 25)] and dec == 18 and why == []
    [mark] = marks
    assert mark.ref.kind == "v2" and mark.ref.venue == "pancake-v2" and mark.ref.token_is0
    assert mark.ref.quote == m.WBNB and mark.ref.fee == m.PANCAKE_V2_FEE_BPS
    r0, r1 = mark.reserves
    assert (r0, r1) == (69794800878721222378398443, 54542732462792648545)
    assert mark.price_native == op.price_from_reserves(r0, r1, 18, 18, True)
    assert str(mark.price_native)[:12] == "7.8147271395"
    assert mark.quote_reserve_base == r1  # the WBNB side


def test_the_v2_fill_is_the_pairs_own_getamountout_at_our_size():
    fx, cands, dec, marks, why = bsc_v2()
    [mark] = marks
    size = 13 * 10**16
    fill, note = m.fill_from(mark.ref, mark, None, side="buy", amount_in=size, native_usd=BNB_USD)
    r0, r1 = mark.reserves
    assert fill.amount_out == (size * 9975 * r0) // (r1 * 10_000 + size * 9975), note
    assert fill.basis == "v2_formula" and fill.source == "pool:pancake-v2:v2_formula"
    # the buy pays the pair fee (25 bps) and its own impact on ~55.8 WBNB of depth (~23 bps)
    gap_bps = (fill.executable_price_usd / fill.spot_price_usd - 1) * 10_000
    assert 40 < gap_bps < 60, gap_bps
    sell, _ = m.fill_from(mark.ref, mark, None, side="sell", amount_in=fill.amount_out, native_usd=BNB_USD)
    assert sell.amount_out < size  # a round trip through the pair never makes money


def test_a_real_pancake_v3_pool_quotes_through_quoterv2_and_agrees_with_its_slot0():
    fx = FX["bsc_v3"]
    cands, dec = m.bsc_candidates(fx["token"], fx["discovery"])
    marks, why = m.bsc_marks(fx["token"], cands, fx["candidates"], dec)
    assert [c[0] for c in cands] == ["v2", "v3", "v3", "v3", "v3"] and why == []
    assert m.deepest(marks).ref.kind == "v2"  # 14,905 WBNB real reserve beats every v3 range
    v3 = {mk.ref.fee: mk for mk in marks if mk.ref.kind == "v3"}
    read = fx["quote_reads"]["500"]
    mark = m.mark_from(v3[500].ref, read[:2], 18)
    fill, note = m.fill_from(v3[500].ref, mark, read[2], side="buy", amount_in=fx["size_wei"], native_usd=BNB_USD)
    assert fill is not None and fill.basis == "quoter_v2", note
    assert fill.amount_out == int("255e46954345cb3b0", 16)
    gap_bps = (fill.executable_price_usd / fill.spot_price_usd - 1) * 10_000
    # 5 bps tier + a 0.13 BNB order against 7,156 WBNB of in-range depth: a few bps, never negative
    assert 0 < gap_bps < 20, gap_bps


def rh_ref(i: int = 0):
    fx = FX["rh_v4"][i]
    key = op.decode_pool_key(bytes.fromhex(fx["pool_and_position"][2:]))
    ref, why = m.rh_ref_from_key(fx["token"], key, "pons_lp_nft")
    return fx, key, ref, why


def test_a_real_pons_graduation_key_decodes_to_a_native_v4_pool_with_its_hook():
    fx, key, ref, why = rh_ref(0)
    assert why == "ok"
    assert key[0] == m.ZERO and key[2] == 0 and key[3] == 200  # native ETH, dynamic hook fee, spacing 200
    assert ref.kind == "v4" and ref.quote is None and not ref.token_is0
    assert int(ref.hooks, 16) & 0x3FFF == 0x2044  # beforeInitialize | afterSwap | afterSwapReturnsDelta
    assert ref.pool == op.v4_pool_id(*key)


@pytest.mark.parametrize("i,low,high", [(0, 100, 130), (1, 200, 230)])
def test_the_v4quoter_fill_carries_the_hook_fee_that_spot_does_not(i, low, high):
    """MEASURED: V4Quoter at 0.01 ETH buys 113 / 216 bps above slot0 -- the Pons hook's cut plus ~12
    bps of our impact. This is the per-token fee paper_costs.FEE_IN_PRICE_FILLS keeps from being
    charged twice by the flat robinhood VENUE_EXTRA_BPS."""
    fx, key, ref, _ = rh_ref(i)
    mark = m.mark_from(ref, [fx["slot0"], fx["liquidity"]], 18)
    assert m.v4_slot0_fields(fx["slot0"])[3] == 0  # lpFee 0: the fee is the hook's, not in slot0
    buy, _ = m.fill_from(ref, mark, fx["quote_buy_0_01_eth"], side="buy", amount_in=10**16, native_usd=ETH_USD)
    sell, _ = m.fill_from(ref, mark, fx["quote_sell_40k_tokens"], side="sell", amount_in=40_000 * 10**18,
                          native_usd=ETH_USD)
    assert buy.basis == sell.basis == "v4_quoter"
    buy_bps = (buy.executable_price_usd / mark.price_native / ETH_USD - 1) * 10_000
    sell_bps = (1 - sell.executable_price_usd / mark.price_native / ETH_USD) * 10_000
    assert low < buy_bps < high and low - 15 < sell_bps < high - 15, (buy_bps, sell_bps)


def test_the_v4_quoter_call_encodes_the_full_pool_key():
    _, key, ref, _ = rh_ref(0)
    to, data = m.quote_v4_call(m.RH_V4_QUOTER, ref.v4_key, True, 10**16)
    words = [int(data[10 + 64 * i: 74 + 64 * i], 16) for i in range((len(data) - 10) // 64)]
    assert to == m.RH_V4_QUOTER and data.startswith(m.SEL_QUOTE_V4)
    assert words[0] == 0x20 and words[8] == 0x100 and words[9] == 0  # tuple offset, bytes offset, empty bytes
    assert ("0x" + format(words[2], "040x")) == key[1] and words[4] == 200 and words[6] == 1 and words[7] == 10**16


# ----------------------------------------------------------------------------- quoter calldata, pinned independently
#
# 2026-10-23 (step 3 review, blocking #2): every entry and exit test keyed its FakeRpc with
# ``m._quoter_call(...)`` -- the function under test -- so a SELL with the direction flipped (the
# quoter treats the token quantity as wei in and returns token atoms: an absurdly high
# executable, booked at the deciding mark and tagged honest) passed all 81 tests, and so did a
# hard-coded fee tier. These encoders are written from the ABI, not from the module, and the
# direction is stated by what the trade PAYS IN: a buy pays the native coin, a sell the token.


def _enc_addr(address: str | None) -> str:
    return (address or "0x" + "0" * 40)[2:].lower().rjust(64, "0")


def _enc_uint(value: int) -> str:
    return format(int(value), "064x")


def v3_quote_calldata(token_in: str, token_out: str, amount_in: int, fee: int) -> str:
    """QuoterV2.quoteExactInputSingle(QuoteExactInputSingleParams{tokenIn, tokenOut, amountIn, fee,
    sqrtPriceLimitX96 = 0}): a static struct, encoded inline."""
    return (selector_of("quoteExactInputSingle((address,address,uint256,uint24,uint160))")
            + _enc_addr(token_in) + _enc_addr(token_out) + _enc_uint(amount_in) + _enc_uint(fee) + _enc_uint(0))


def v4_quote_calldata(key: tuple, *, pays_in: str | None, amount_in: int) -> str:
    """V4Quoter.quoteExactInputSingle(QuoteExactSingleParams{PoolKey, zeroForOne, exactAmount,
    hookData = ""}). zeroForOne is TRUE when the currency paid in is currency0."""
    c0, c1, fee, spacing, hooks = key
    zero = "0x" + "0" * 40
    pays = (pays_in or zero).lower()
    assert pays in (c0.lower(), c1.lower()), "the input must be one side of the key"
    zero_for_one = pays == c0.lower()
    return (selector_of("quoteExactInputSingle(((address,address,uint24,int24,address),bool,uint128,bytes))")
            + _enc_uint(0x20) + _enc_addr(c0) + _enc_addr(c1) + _enc_uint(fee) + _enc_uint(spacing)
            + _enc_addr(hooks) + _enc_uint(1 if zero_for_one else 0) + _enc_uint(amount_in)
            + _enc_uint(0x100) + _enc_uint(0))


def _calldata_words(data: str) -> list[int]:
    return [int(data[10 + 64 * i: 74 + 64 * i], 16) for i in range((len(data) - 10) // 64)]


def bsc_v3_ref(fee: int = 500) -> m.PoolRef:
    fx = FX["bsc_v3"]
    cands, dec = m.bsc_candidates(fx["token"], fx["discovery"])
    marks, _ = m.bsc_marks(fx["token"], cands, fx["candidates"], dec)
    return {mk.ref.fee: mk.ref for mk in marks if mk.ref.kind == "v3"}[fee]


@pytest.mark.parametrize("fee", [500, 2500])
def test_the_v3_quoter_call_pays_wbnb_in_on_a_buy_and_the_token_in_on_a_sell_at_the_pools_own_tier(fee):
    ref = bsc_v3_ref(fee)
    token = FX["bsc_v3"]["token"].lower()
    assert ref.fee == fee and fee != 3000  # 3000 is not a pancake tier: a hard-coded fee cannot pass
    buy = m._quoter_call(ref, 13 * 10**16, buy=True)
    sell = m._quoter_call(ref, 7 * 10**21, buy=False)
    assert buy == (m.PANCAKE_V3_QUOTER, v3_quote_calldata(m.WBNB, token, 13 * 10**16, fee))
    assert sell == (m.PANCAKE_V3_QUOTER, v3_quote_calldata(token, m.WBNB, 7 * 10**21, fee))
    words = _calldata_words(buy[1])
    assert ("0x" + format(words[0], "040x")) == m.WBNB and words[3] == fee  # tokenIn, fee


def test_the_v4_quoter_direction_follows_the_currency_paid_in():
    _, key, ref, _ = rh_ref(0)
    token = ref.token
    assert key[0] == m.ZERO and key[1] == token  # native ETH is currency0 on a Pons graduation
    buy = m._quoter_call(ref, 10**16, buy=True)
    sell = m._quoter_call(ref, 40_000 * 10**18, buy=False)
    assert buy == (m.RH_V4_QUOTER, v4_quote_calldata(key, pays_in=None, amount_in=10**16))
    assert sell == (m.RH_V4_QUOTER, v4_quote_calldata(key, pays_in=token, amount_in=40_000 * 10**18))
    assert _calldata_words(buy[1])[6] == 1 and _calldata_words(sell[1])[6] == 0  # word 6 = zeroForOne
    # A WETH-quoted v4 key where the token is currency0: the direction flips with the key, not the side.
    low = "0x" + "01" * 20
    weth_key = (low, m.RH_WETH, 3000, 60, m.ZERO)
    flipped = m.PoolRef(Chain.ROBINHOOD, low, "0x" + "ab" * 32, "v4", "uniswap-v4", True, m.RH_WETH, 3000, 60, None)
    assert m._quoter_call(flipped, 10**16, buy=True)[1] == v4_quote_calldata(weth_key, pays_in=m.RH_WETH,
                                                                            amount_in=10**16)
    assert _calldata_words(m._quoter_call(flipped, 10**16, buy=True)[1])[6] == 0
    assert _calldata_words(m._quoter_call(flipped, 10**16, buy=False)[1])[6] == 1


def test_a_pool_quoted_in_anything_but_native_is_refused_until_measured():
    _, key, _, _ = rh_ref(0)
    usdg_key = (key[1], m.op.ROBINHOOD_USDG, key[2], key[3], key[4]) if key[1] < m.op.ROBINHOOD_USDG \
        else (m.op.ROBINHOOD_USDG, key[1], key[2], key[3], key[4])
    ref, why = m.rh_ref_from_key(key[1], usdg_key, "x")
    assert ref is None and why == f"quote_asset_not_native:{m.op.ROBINHOOD_USDG}"
    assert m.native_quote_refusal(Chain.BSC, m.WBNB) is None
    assert m.native_quote_refusal(Chain.ROBINHOOD, m.RH_WETH) is None
    assert m.native_quote_refusal(Chain.BSC, "0x55d398326f99059ff775485246999027b3197955").startswith(
        "quote_asset_not_native")


# ----------------------------------------------------------------------------- resolution


def test_a_pons_graduation_resolves_from_its_lp_nft_in_one_call_then_from_cache(tmp_db):
    fx, key, ref, _ = rh_ref(0)
    tmp_db.execute("INSERT INTO events (ts_ms, kind, payload, dedupe_key) VALUES (?,?,?,?)",
                   (now_ms(), "token.migrated", jdump({"position_id": fx["lp_token_id"]}),
                    f"token.migrated:robinhood:{fx['token']}"))
    rpc = FakeRpc({(m.RH_POSITION_MANAGER, m.SEL_POOL_AND_POSITION + m._word(fx["lp_token_id"])):
                   fx["pool_and_position"]})
    got, why = m.resolve_robinhood(fx["token"], rpc, tmp_db)
    assert got == ref and why == "ok" and len(rpc.sends) == 1
    m._REFS.clear()  # another process: the kv row is what it finds
    again, why = m.resolve_robinhood(fx["token"], rpc, tmp_db)
    assert again == ref and why == "cached" and len(rpc.sends) == 1


def test_an_unknown_robinhood_pool_is_refused_never_guessed(tmp_db):
    rpc = FakeRpc()
    ref, why = m.resolve_robinhood("0x" + "c" * 40, rpc, tmp_db)
    assert ref is None and why == "pool_unknown" and rpc.sends == []


def test_a_bsc_token_with_no_wbnb_pool_costs_one_discovery_then_nothing(tmp_db):
    rpc = FakeRpc(default=None)
    token = "0x" + "d" * 40
    rpc.table[(token, m.SEL_DECIMALS)] = "0x" + format(18, "064x")
    rpc.table.update({(t.lower(), d.lower()): "0x" + "0" * 64 for t, d in m.bsc_discovery_calls(token)[:-1]})
    assert m.resolve_bsc(token, rpc, tmp_db) == (None, "no_wbnb_pool")
    assert m.resolve_bsc(token, rpc, tmp_db)[0] is None
    assert len(rpc.sends) == 1, "a structural refusal must not be re-asked every tick"


# ----------------------------------------------------------------------------- the bsc leaf


def _v2_table(token: str, pair: str, r0: int, r1: int) -> dict:
    return {(pair, m.SEL_GET_RESERVES): "0x" + format(r0, "064x") + format(r1, "064x") + format(1, "064x")}


def book(tmp_db, n: int = 6) -> tuple[list[str], FakeRpc]:
    rpc = FakeRpc()
    tokens = []
    for i in range(n):
        token, pair = "0x" + f"{i + 1:02x}" * 20, "0x" + f"{i + 0x41:02x}" * 20
        m.remember_ref(m.PoolRef(Chain.BSC, token, pair, "v2", "pancake-v2", True, m.WBNB, 25), tmp_db)
        m._DECIMALS[(Chain.BSC.value, token)] = 18
        rpc.table.update({(k[0].lower(), k[1].lower()): v for k, v in _v2_table(token, pair, 1000 * 10**18,
                                                                              2 * 10**18).items()})
        tokens.append(token)
    return tokens, rpc


def test_one_aggregate3_primes_every_token_and_the_quotes_carry_what_protection_needs(tmp_db, monkeypatch):
    _native(monkeypatch, Decimal("500"))
    tokens, rpc = book(tmp_db, 6)
    leaf = m.PoolPriceSource(tmp_db, chain=Chain.BSC, transport=rpc)
    assert leaf.prime(Chain.BSC, tokens) == 6
    quotes = [leaf.quote(Chain.BSC, t) for t in tokens]
    assert len(rpc.sends) == 1 and len(rpc.sends[0]) == 6
    for q in quotes:
        assert q.usable and q.price_usd == Decimal("1.000") and q.source == "evm-pool:pancake-v2"
        assert q.pool_id and q.venue == "pancake-v2" and q.liquidity_usd == Decimal("2000")
        assert q.liquidity_kind == "pool_reserve_usd" and q.settlement == wd.SETTLE_NATIVE_POOL


def test_with_no_bsc_pool_host_the_leaf_refuses_without_touching_the_network(tmp_db, monkeypatch):
    _native(monkeypatch, Decimal("500"))
    monkeypatch.delenv(m.BSC_POOL_RPC_ENV, raising=False)
    monkeypatch.setenv("BSC_RPC_URL", "https://bsc-rpc.publicnode.com")  # rpc_for(BSC) is NOT a fallback
    monkeypatch.setattr(m, "_env_or_dotenv", lambda name: None)

    def no_network(*a, **k):
        raise AssertionError("a pool read went out with no pool host configured")

    monkeypatch.setattr("kaiba.providers._http.post_json", no_network)
    q = m.PoolPriceSource(tmp_db, chain=Chain.BSC).quote(Chain.BSC, "0x" + "e" * 40)
    assert not q.usable and "bsc_pool_rpc_unset" in (q.note or "")


def test_the_pool_host_must_not_be_protections_bsc_host(monkeypatch):
    monkeypatch.setattr(m, "_env_or_dotenv", lambda name: "https://bsc-rpc.publicnode.com/")
    url, why = m.bsc_pool_rpc_url()
    assert url is None and why.startswith("bsc_pool_rpc_shares_protection_host")
    m.clear_caches()
    monkeypatch.setattr(m, "_env_or_dotenv", lambda name: "https://bsc-dataseed.bnbchain.org")
    assert m.bsc_pool_rpc_url() == ("https://bsc-dataseed.bnbchain.org", "ok")


def test_a_429_on_the_pool_key_leaves_the_rpc_keys_penalty_untouched(tmp_db, monkeypatch, risk_file):  # noqa: F811
    """The isolation the design asks for: our cooldown families, not the provider's IP limit."""
    import httpx

    from kaiba.core import limiter
    from kaiba.core.config import load_risk, save_risk

    cfg = load_risk()
    cfg.provider_budgets = {**(cfg.provider_budgets or {}),
                            "bsc-pool-rpc": {"min_interval_ms": 250, "capacity": 10, "refill_per_s": 2.0,
                                             "max_inflight": 2}}
    save_risk(cfg, risk_file)
    assert limiter.limits_for(m.BSC_PROVIDER).min_interval_ms == 250  # the declared budget is the one read

    def four_twenty_nine(method, url, **kw):
        return httpx.Response(429, request=httpx.Request(method, url), text="slow down")

    monkeypatch.setattr(httpx, "request", four_twenty_nine)
    send = m.bsc_transport(tmp_db, url="https://pool.example.invalid", wait_for_slot_s=0.0)
    assert send([(m.WBNB, m.SEL_DECIMALS)]) == [None]
    pool = fetch_one(tmp_db, "SELECT penalty_level, banned_until_ms FROM provider_state WHERE provider=?",
                     (m.BSC_PROVIDER,))
    assert pool is not None and (pool["penalty_level"] > 0 or pool["banned_until_ms"] > now_ms()), pool
    shared = fetch_one(tmp_db, "SELECT penalty_level, banned_until_ms FROM provider_state WHERE provider='rpc'")
    assert shared is None or (shared["penalty_level"] == 0 and shared["banned_until_ms"] == 0), shared
    ban = fetch_one(tmp_db, "SELECT count(*) AS n FROM provider_family_bans WHERE provider='rpc'")
    assert ban["n"] == 0


# ----------------------------------------------------------------------------- through Watchdog.tick


def bsc_book_dog(tmp_db, n: int = 6):
    tokens, rpc = book(tmp_db, n)
    for i, token in enumerate(tokens):
        make_position(tmp_db, position_id=f"pos_bsc_{i}", token=token, chain=Chain.BSC, entry="1.0",
                      mode=LaneMode.SHADOW)
    tmp_db.commit()
    root = wd.FallbackPriceSource(m.PoolPriceSource(tmp_db, chain=Chain.BSC, transport=rpc), name="test")
    return tokens, rpc, wd.Watchdog(tmp_db, price_source=root, submitter=RecordingSubmitter())


def test_a_tick_over_six_bsc_positions_makes_exactly_one_pool_call(tmp_db, monkeypatch, risk_file):  # noqa: F811
    _native(monkeypatch, Decimal("500"))
    tokens, rpc, dog = bsc_book_dog(tmp_db, 6)
    report = dog.tick()
    assert report.checked == 6 and report.blind == 0
    assert len(rpc.sends) == 1, f"{len(rpc.sends)} pool calls in one tick; the prime hook did not batch"
    dog.tick()
    assert len(rpc.sends) == 2, "the next tick must read again: a primed price never outlives its tick"


def test_with_a_shared_factory_built_source_the_prime_still_reaches_the_leaf(tmp_db, monkeypatch, risk_file):  # noqa: F811
    """The box builds a fresh source per prefetch worker; the primed store is process-wide."""
    _native(monkeypatch, Decimal("500"))
    tokens, rpc = book(tmp_db, 4)
    for i, token in enumerate(tokens):
        make_position(tmp_db, position_id=f"pos_f_{i}", token=token, chain=Chain.BSC, mode=LaneMode.SHADOW)
    tmp_db.commit()

    def factory():
        return wd.FallbackPriceSource(m.PoolPriceSource(tmp_db, chain=Chain.BSC, transport=rpc), name="per-worker")

    dog = wd.Watchdog(tmp_db, price_source=factory(), prefetch_source_factory=factory,
                      submitter=RecordingSubmitter())
    dog.tick()
    assert len(rpc.sends) == 1


# ----------------------------------------------------------------------------- the robinhood leaf


def sample(conn, position_id: str, token: str, *, age_ms: int, price: str = "0.0002",
           quote_token=None, source: str = "onchain:v4|eth_usd:native_prices", pool: str = "0x" + "9" * 64) -> None:
    conn.execute(
        "INSERT INTO onchain_price_samples (ts_ms, position_id, token, pool, pool_kind, quote_token, price_quote, "
        "price_usd, source) VALUES (?,?,?,?,?,?,?,?,?)",
        (now_ms() - age_ms, position_id, token.lower(), pool, "v4", quote_token, "8e-8", price, source))


RH_TOKEN = "0x" + "7a" * 20


def test_the_sample_leaf_answers_within_two_polls_and_refuses_older(tmp_db):
    make_position(tmp_db, position_id="pos_rh", token=RH_TOKEN, chain=Chain.ROBINHOOD)
    leaf = m.OnchainSampleSource(tmp_db, max_age_ms=24_000, enabled=True)
    sample(tmp_db, "pos_rh", RH_TOKEN, age_ms=30_000)
    old = leaf.quote(Chain.ROBINHOOD, RH_TOKEN)
    # Refused by the LEAF's own bound (named), not merely left stale for PriceQuote to catch later.
    assert not old.usable and old.source == "onchain-sample" and "ms old (max 24000)" in (old.note or "")
    sample(tmp_db, "pos_rh", RH_TOKEN, age_ms=20_000, price="0.00021")
    q = leaf.quote(Chain.ROBINHOOD, RH_TOKEN)
    assert q.usable and q.price_usd == Decimal("0.00021") and q.source == "onchain-sample:v4"
    assert q.pool_id == "0x" + "9" * 64 and q.settlement == wd.SETTLE_NATIVE_POOL


@pytest.mark.parametrize("quote_token,source,why", [
    (op.ROBINHOOD_USDG, "onchain:v4|usdg_peg", "quote_asset_not_native"),
    (None, "onchain:v3|ref:0xabc:eth_usd:native_prices", "priced via ref"),
])
def test_the_sample_leaf_refuses_a_foreign_quoted_sample(tmp_db, quote_token, source, why):
    make_position(tmp_db, position_id="pos_rh", token=RH_TOKEN, chain=Chain.ROBINHOOD)
    sample(tmp_db, "pos_rh", RH_TOKEN, age_ms=1_000, quote_token=quote_token, source=source)
    q = m.OnchainSampleSource(tmp_db, max_age_ms=24_000, enabled=True).quote(Chain.ROBINHOOD, RH_TOKEN)
    assert not q.usable and why in (q.note or "")


def test_the_sample_leaf_is_off_until_protection_switches_it_on(tmp_db, risk_file):  # noqa: F811
    """INSUFFICIENT on the box (onchain_vs_feed, 14/30 stops) -> it must not decide by default."""
    make_position(tmp_db, position_id="pos_rh", token=RH_TOKEN, chain=Chain.ROBINHOOD)
    sample(tmp_db, "pos_rh", RH_TOKEN, age_ms=1_000)
    q = m.OnchainSampleSource(tmp_db, max_age_ms=24_000).quote(Chain.ROBINHOOD, RH_TOKEN)
    assert not q.usable and m.SAMPLE_MARKS_KEY in (q.note or "")


def test_a_robinhood_tick_spends_zero_calls_on_the_robinhood_host_for_its_marks(tmp_db, monkeypatch, risk_file):  # noqa: F811
    calls: list = []
    import kaiba.ingest.robinhood as rh

    monkeypatch.setattr(rh, "rpc_batch", lambda *a, **k: calls.append(a) or rh.RpcResult([], False))
    for i in range(3):
        token = "0x" + f"{i + 0x71:02x}" * 20
        make_position(tmp_db, position_id=f"pos_rh_{i}", token=token, chain=Chain.ROBINHOOD, entry="0.0002")
        sample(tmp_db, f"pos_rh_{i}", token, age_ms=2_000)
    tmp_db.commit()
    root = wd.FallbackPriceSource(m.OnchainSampleSource(tmp_db, max_age_ms=24_000, enabled=True),
                                  FakeSource({}), name="rh-test")
    report = wd.Watchdog(tmp_db, price_source=root, submitter=RecordingSubmitter()).tick()
    blind = [r[0] for r in tmp_db.execute("SELECT payload FROM events WHERE payload LIKE '%protection_blind%'")]
    assert report.checked == 3 and report.blind == 0, blind
    assert calls == [], "the robinhood marks must come from the sampler, not a new read"


# ----------------------------------------------------------------------------- corroboration


class QuoteSubmitter:
    """Records the quote each exit was decided on."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def submit_exit(self, position, pct, *, quote, reason):  # noqa: ANN001 - test double
        self.calls.append({"position_id": position.position_id, "quote": quote, "reason": reason})
        return wd.ExitOutcome(True, wd.OrderState.FILLED, "ord_fake", "filled")


class SamePoolDex:
    """DexScreener answering for the SAME pool the pool leaf read: not independent."""

    name = "prices"

    def __init__(self, pool: str, price: str) -> None:
        self.pool, self.price = pool, price

    def quote(self, chain, token):
        return wd.PriceQuote(price_usd=Decimal(self.price), liquidity_usd=Decimal("2000"),
                             basis=wd.EvidenceBasis.PROVIDER_REPORTED, source="prices:dexscreener", chain=chain,
                             token=token, pool_id=self.pool, venue="pancakeswap", liquidity_kind="dex_tvl_usd",
                             settlement=wd.SETTLE_NATIVE_POOL)


def test_a_new_pool_stop_is_corroborated_by_the_independent_leaf_not_a_second_read_of_the_pool(
        tmp_db, monkeypatch, risk_file):  # noqa: F811
    _native(monkeypatch, Decimal("500"))
    token, pair = "0x" + "31" * 20, "0x" + "32" * 20
    m.remember_ref(m.PoolRef(Chain.BSC, token, pair, "v2", "pancake-v2", True, m.WBNB, 25), tmp_db)
    m._DECIMALS[(Chain.BSC.value, token)] = 18
    rpc = FakeRpc(_v2_table(token, pair, 1000 * 10**18, 1 * 10**18))  # 0.001 BNB * $500 = $0.50: -50%
    make_position(tmp_db, position_id="pos_c", token=token, chain=Chain.BSC, entry="1.0")
    tmp_db.commit()
    backup_hits: list[str] = []

    def backup(chain, t):
        backup_hits.append(t)
        return wd.PriceQuote(price_usd=Decimal("0.5"), basis=wd.EvidenceBasis.PROVIDER_REPORTED,
                             source="codex-io:pair", chain=chain, token=t)

    pool_leaf = m.PoolPriceSource(tmp_db, chain=Chain.BSC, transport=rpc)
    root = wd.FallbackPriceSource(pool_leaf, SamePoolDex(pair, "0.5"), m.BackupPriceSource(backup), name="bsc")
    sub = QuoteSubmitter()
    dog = wd.Watchdog(tmp_db, price_source=root, submitter=sub)
    # The position had been following DexScreener: the pool mark is a NEW identity that would stop it.
    position = load_position(tmp_db, "pos_c")
    dog._remember_quote(position, wd.load_state(tmp_db, position),
                        SamePoolDex(pair, "1.0").quote(Chain.BSC, token), None)
    dog.tick()
    assert backup_hits == [token], "the same-pool DexScreener read must not corroborate the stop"
    assert sub.calls and sub.calls[0]["quote"].source == "codex-io:pair"


def test_with_no_independent_leaf_a_new_pool_stop_is_blind_not_a_sell(tmp_db, monkeypatch, risk_file):  # noqa: F811
    _native(monkeypatch, Decimal("500"))
    token, pair = "0x" + "33" * 20, "0x" + "34" * 20
    m.remember_ref(m.PoolRef(Chain.BSC, token, pair, "v2", "pancake-v2", True, m.WBNB, 25), tmp_db)
    m._DECIMALS[(Chain.BSC.value, token)] = 18
    rpc = FakeRpc(_v2_table(token, pair, 1000 * 10**18, 1 * 10**18))
    make_position(tmp_db, position_id="pos_d", token=token, chain=Chain.BSC, entry="1.0")
    tmp_db.commit()
    root = wd.FallbackPriceSource(m.PoolPriceSource(tmp_db, chain=Chain.BSC, transport=rpc),
                                  SamePoolDex(pair, "0.5"), m.BackupPriceSource(), name="bsc")
    sub = QuoteSubmitter()
    dog = wd.Watchdog(tmp_db, price_source=root, submitter=sub)
    position = load_position(tmp_db, "pos_d")
    dog._remember_quote(position, wd.load_state(tmp_db, position), SamePoolDex(pair, "1.0").quote(Chain.BSC, token),
                        None)
    report = dog.tick()
    assert sub.calls == [] and report.blind == 1


def test_the_backup_leaf_is_unwired_until_someone_installs_it():
    leaf = m.BackupPriceSource()
    assert leaf.quote(Chain.BSC, "0x" + "1" * 40).note == "backup_unwired"
    m.set_backup_quote(lambda chain, token: wd.PriceQuote(price_usd=Decimal("2"), source="codex-io:x",
                                                          basis=wd.EvidenceBasis.PROVIDER_REPORTED,
                                                          chain=chain, token="0x" + "2" * 40))
    assert not leaf.quote(Chain.BSC, "0x" + "1" * 40).usable, "an answer for another token is refused"


# ----------------------------------------------------------------------------- Codex.io, sampled


def _codex(monkeypatch, *, key: str = "k", price: str = "0.42"):
    from kaiba.providers import codex_io

    asked: list[tuple] = []

    def fake_price(chain, address, **kw):
        asked.append((chain, address, kw.get("priority")))
        return codex_io.TokenPrice(network_id=56, address=address, price_usd=Decimal(price),
                                   liquidity_usd=Decimal("12345"), ts_s=1, block_number=2, change24=None)

    monkeypatch.setattr(codex_io, "_api_key", lambda: key)
    monkeypatch.setattr(codex_io, "price", fake_price)
    return asked


def test_codex_backs_up_at_a_sampled_rate_never_per_tick(monkeypatch):
    """10,000 CU / 30 days for every user of the key: one call an hour here, one per token per 30 min."""
    asked = _codex(monkeypatch)
    a, b = "0x" + "a1" * 20, "0x" + "b2" * 20
    q = m.codex_backup_quote(Chain.BSC, a)
    assert q.usable and q.price_usd == Decimal("0.42") and q.source == "codex-io:aggregate"
    assert q.pool_id is None and q.settlement == wd.SETTLE_AGGREGATOR and q.liquidity_usd == Decimal("12345")
    assert m.codex_backup_quote(Chain.BSC, a).note == "codex_backup_token_gap"
    assert m.codex_backup_quote(Chain.ROBINHOOD, b).note == "codex_backup_hourly_budget_spent"
    assert len(asked) == 1 and asked[0][2] is not None
    m._CODEX_SENT[:] = [t - 3_600_001 for t in m._CODEX_SENT]  # an hour later
    assert m.codex_backup_quote(Chain.ROBINHOOD, b).usable and len(asked) == 2


def test_codex_is_never_asked_without_a_key_or_for_the_native_coin_or_sol(monkeypatch):
    asked = _codex(monkeypatch, key="")
    assert "not configured" in m.codex_backup_quote(Chain.BSC, "0x" + "a1" * 20).note
    _codex(monkeypatch)
    assert not m.codex_backup_quote(Chain.BSC, m.WBNB).usable
    assert not m.codex_backup_quote(Chain.SOL, "So11111111111111111111111111111111111111112").usable
    assert asked == [] and m._CODEX_SENT == []


def test_the_bsc_and_robinhood_routes_end_in_the_sampled_codex_backup(tmp_db):
    from kaiba.execution import evm_price as ep

    routes = ep.venue_price_source().routes
    for chain in (Chain.BSC, Chain.ROBINHOOD):
        backup = routes[chain].fallback.sources[-1]
        assert isinstance(backup, m.BackupPriceSource) and backup._fn is m.codex_backup_quote


def test_codex_corroborates_a_new_pool_stop_through_the_watchdog(tmp_db, monkeypatch, risk_file):  # noqa: F811
    _native(monkeypatch, Decimal("500"))
    asked = _codex(monkeypatch, price="0.5")
    token, pair = "0x" + "35" * 20, "0x" + "36" * 20
    m.remember_ref(m.PoolRef(Chain.BSC, token, pair, "v2", "pancake-v2", True, m.WBNB, 25), tmp_db)
    m._DECIMALS[(Chain.BSC.value, token)] = 18
    rpc = FakeRpc(_v2_table(token, pair, 1000 * 10**18, 1 * 10**18))
    make_position(tmp_db, position_id="pos_cx", token=token, chain=Chain.BSC, entry="1.0")
    tmp_db.commit()
    root = wd.FallbackPriceSource(m.PoolPriceSource(tmp_db, chain=Chain.BSC, transport=rpc),
                                  SamePoolDex(pair, "0.5"), m.BackupPriceSource(m.codex_backup_quote), name="bsc")
    sub = QuoteSubmitter()
    dog = wd.Watchdog(tmp_db, price_source=root, submitter=sub)
    position = load_position(tmp_db, "pos_cx")
    dog._remember_quote(position, wd.load_state(tmp_db, position), SamePoolDex(pair, "1.0").quote(Chain.BSC, token),
                        None)
    dog.tick()
    assert len(asked) == 1 and sub.calls and sub.calls[0]["quote"].source == "codex-io:aggregate"


def test_robinhood_fills_ride_protections_one_pacer_under_their_own_countable_endpoint(monkeypatch):
    """Same provider (robinhood-rpc) and family (chain.*) as the Pons stop reads, so ONE pacer and
    shared cooldowns; its own endpoint name so provider_calls can count it (3d: <= the entry rate)."""
    import kaiba.ingest.robinhood as rh

    seen: list[dict] = []

    def fake(calls, **kw):
        seen.append(kw)
        return rh.RpcResult(["0x" + "0" * 63 + "1"] * len(calls), True)

    monkeypatch.setattr(rh, "rpc_batch", fake)
    send = m.robinhood_transport(priority=m.Priority.ENTRY)
    assert send([(m.RH_WETH, m.SEL_DECIMALS), (m.RH_WETH, m.SEL_DECIMALS)]) == ["0x" + "0" * 63 + "1"] * 2
    assert len(seen) == 1 and seen[0]["endpoint"] == "chain.pool" and seen[0]["priority"] is m.Priority.ENTRY
    assert rh.PROVIDER == "robinhood-rpc" and "chain.pool".split(".")[0] == "chain.price".split(".")[0]


# ----------------------------------------------------------------------------- the backup is corroboration ONLY
#
# 2026-10-23 (step 3 review, blocking #3). The box's chain is Fallback(PumpSwap, PumpCurve, routed,
# gmgn), and the routed bsc/robinhood sources END in BackupPriceSource(codex_backup_quote). For a
# position GMGN prices (DexScreener, router and Pons refuse), Codex answered one tick an hour and
# became the remembered identity; GMGN's next stop was then a suspect stop on a new identity, GMGN
# was skipped as the rejected leaf, Codex refused (token gap) -> quote_uncorroborated_source_
# transition, BLIND every tick, and a shadow position over an hour old was written off
# abandoned_unpriceable (and dropped from the scoreboard by fabricated.py).


class _Refuses:
    name = "prices"

    def quote(self, chain, token):
        return wd.PriceQuote.unavailable("dexscreener has no pair", source="prices:dexscreener")


class _Gmgn:
    name = "gmgn"

    def __init__(self, price: str = "1.0") -> None:
        self.price = price

    def quote(self, chain, token):
        return wd.PriceQuote(price_usd=Decimal(self.price), basis=wd.EvidenceBasis.PROVIDER_REPORTED,
                             source="gmgn:token.info", chain=chain, token=token)


def _box_shape(gmgn) -> wd.FallbackPriceSource:
    routed = wd.FallbackPriceSource(_Refuses(), m.BackupPriceSource(m.codex_backup_quote), name="bsc-after-curve")
    return wd.FallbackPriceSource(routed, gmgn, name="box-shape")


@pytest.mark.parametrize("mode", [LaneMode.LIVE, LaneMode.SHADOW])
def test_a_gmgn_priced_stop_fires_although_the_sampled_codex_leaf_sits_ahead_of_gmgn(
        tmp_db, monkeypatch, risk_file, mode):  # noqa: F811
    asked = _codex(monkeypatch, price="0.97")
    token = "0x" + "5c" * 20
    make_position(tmp_db, position_id="pos_flip", token=token, chain=Chain.BSC, entry="1.0", mode=mode)
    tmp_db.commit()
    gmgn, sub = _Gmgn(), QuoteSubmitter()
    dog = wd.Watchdog(tmp_db, price_source=_box_shape(gmgn), submitter=sub)
    position = load_position(tmp_db, "pos_flip")
    dog._remember_quote(position, wd.load_state(tmp_db, position), gmgn.quote(Chain.BSC, token), None)
    first = dog.tick()
    assert first.blind == 0 and asked == [], "the sampled backup must never price a tick"
    assert dog._read_quote_memory(position)[0].source == "gmgn:token.info"
    gmgn.price = "0.5"  # -50%: a stop on any honest reading
    second = dog.tick()
    assert second.blind == 0
    assert sub.calls and sub.calls[0]["quote"].source == "gmgn:token.info", sub.calls


def test_an_hour_old_shadow_position_is_stopped_not_written_off_after_the_backup_could_answer(
        tmp_db, monkeypatch, risk_file):  # noqa: F811
    _codex(monkeypatch, price="0.97")
    token = "0x" + "5d" * 20
    make_position(tmp_db, position_id="pos_old", token=token, chain=Chain.BSC, entry="1.0", mode=LaneMode.SHADOW)
    tmp_db.execute("UPDATE positions SET opened_ms=? WHERE position_id='pos_old'", (now_ms() - 2 * 3_600_000,))
    tmp_db.commit()
    gmgn, sub = _Gmgn(), QuoteSubmitter()
    dog = wd.Watchdog(tmp_db, price_source=_box_shape(gmgn), submitter=sub)
    position = load_position(tmp_db, "pos_old")
    dog._remember_quote(position, wd.load_state(tmp_db, position), gmgn.quote(Chain.BSC, token), None)
    dog.tick()
    gmgn.price = "0.5"
    dog.tick()
    row = fetch_one(tmp_db, "SELECT closed_ms, exit_reason FROM positions WHERE position_id='pos_old'")
    assert row["closed_ms"] is None and row["exit_reason"] is None, row  # not abandoned_unpriceable
    assert sub.calls and sub.calls[0]["quote"].source == "gmgn:token.info"


def test_a_free_independent_leaf_corroborates_before_the_scarce_backup_is_spent(tmp_db, monkeypatch, risk_file):  # noqa: F811
    """A new-pool stop with GMGN (free) and Codex (one call an hour) both able to corroborate:
    GMGN does, and Codex's call is kept for when nothing else can."""
    _native(monkeypatch, Decimal("500"))
    asked = _codex(monkeypatch, price="0.5")
    token, pair = "0x" + "37" * 20, "0x" + "38" * 20
    m.remember_ref(m.PoolRef(Chain.BSC, token, pair, "v2", "pancake-v2", True, m.WBNB, 25), tmp_db)
    m._DECIMALS[(Chain.BSC.value, token)] = 18
    rpc = FakeRpc(_v2_table(token, pair, 1000 * 10**18, 1 * 10**18))  # $0.50: -50%
    make_position(tmp_db, position_id="pos_g", token=token, chain=Chain.BSC, entry="1.0")
    tmp_db.commit()
    routed = wd.FallbackPriceSource(m.PoolPriceSource(tmp_db, chain=Chain.BSC, transport=rpc), SamePoolDex(pair, "0.5"),
                                    m.BackupPriceSource(m.codex_backup_quote), name="bsc")
    sub = QuoteSubmitter()
    dog = wd.Watchdog(tmp_db, price_source=wd.FallbackPriceSource(routed, _Gmgn("0.5"), name="box"), submitter=sub)
    position = load_position(tmp_db, "pos_g")
    dog._remember_quote(position, wd.load_state(tmp_db, position), SamePoolDex(pair, "1.0").quote(Chain.BSC, token),
                        None)
    dog.tick()
    assert sub.calls and sub.calls[0]["quote"].source == "gmgn:token.info"
    assert asked == [], "the sampled backup was spent although GMGN could corroborate"


def test_a_backup_corroborated_quote_decides_its_tick_but_is_never_remembered(tmp_db, monkeypatch, risk_file):  # noqa: F811
    _native(monkeypatch, Decimal("500"))
    asked = _codex(monkeypatch, price="0.5")
    token, pair = "0x" + "39" * 20, "0x" + "3a" * 20
    m.remember_ref(m.PoolRef(Chain.BSC, token, pair, "v2", "pancake-v2", True, m.WBNB, 25), tmp_db)
    m._DECIMALS[(Chain.BSC.value, token)] = 18
    rpc = FakeRpc(_v2_table(token, pair, 1000 * 10**18, 1 * 10**18))
    make_position(tmp_db, position_id="pos_r", token=token, chain=Chain.BSC, entry="1.0")
    tmp_db.commit()

    class Fails(QuoteSubmitter):
        def submit_exit(self, position, pct, *, quote, reason):  # noqa: ANN001
            super().submit_exit(position, pct, quote=quote, reason=reason)
            return wd.ExitOutcome(False, wd.OrderState.FAILED, None, "venue said no")

    root = wd.FallbackPriceSource(m.PoolPriceSource(tmp_db, chain=Chain.BSC, transport=rpc), SamePoolDex(pair, "0.5"),
                                  m.BackupPriceSource(m.codex_backup_quote), name="bsc")
    sub = Fails()
    dog = wd.Watchdog(tmp_db, price_source=root, submitter=sub)
    position = load_position(tmp_db, "pos_r")
    dog._remember_quote(position, wd.load_state(tmp_db, position), SamePoolDex(pair, "1.0").quote(Chain.BSC, token),
                        None)
    dog.tick()
    assert len(asked) == 1 and sub.calls and sub.calls[0]["quote"].source == "codex-io:aggregate"
    # The position is still open (the sell failed): its remembered identity is still the one it
    # was following, never the one-off Codex answer.
    assert dog._read_quote_memory(position)[0].source == "prices:dexscreener"


# ----------------------------------------------------------------------------- the prime is bounded; a dead host is backed off
#
# 2026-10-23 (step 3 review, blocking #4), latent until BSC_POOL_RPC_URL is set. The prime ran on
# the tick thread before any stop is decided at the general 2 s slot wait + 4 s timeout; a failed
# prime left every bsc quote to re-read its pool alone, and a failed read had no backoff. Measured
# (2 live sol + 2 shadow bsc, 1.5 s per pool send): first live sol decision 47 -> 3,031 ms, 3 pool
# sends per tick, every tick.


class _SlowDead:
    def __init__(self, delay: float) -> None:
        self.delay, self.sends = delay, 0

    def __call__(self, calls):
        import time as _t

        self.sends += 1
        _t.sleep(self.delay)
        return [None] * len(calls)


class _Sol:
    name = "sol"

    def quote(self, chain, token):
        if chain is not Chain.SOL:
            return wd.PriceQuote.unavailable("sol only", source=self.name)
        return wd.PriceQuote(price_usd=Decimal("1.0"), basis=wd.EvidenceBasis.VERIFIED_ONCHAIN, source="sol:fake",
                             chain=chain, token=token)


class _Dex:
    name = "prices"

    def quote(self, chain, token):
        return wd.PriceQuote(price_usd=Decimal("1.0"), basis=wd.EvidenceBasis.PROVIDER_REPORTED,
                             source="prices:dexscreener", chain=chain, token=token)


def test_a_dead_pool_host_costs_one_bounded_send_then_nothing_while_live_sol_is_decided(
        tmp_db, monkeypatch, risk_file):  # noqa: F811
    import time as _t

    _native(monkeypatch, Decimal("600"))
    for i in range(2):
        make_position(tmp_db, position_id=f"sol_live_{i}", token=f"So1{i}" + "1" * 40, chain=Chain.SOL,
                      mode=LaneMode.LIVE)
    for i in range(2):
        token = "0x" + f"{i + 0x61:02x}" * 20
        m.remember_ref(m.PoolRef(Chain.BSC, token, "0x" + f"{i + 0x71:02x}" * 20, "v2", "pancake-v2", True, m.WBNB, 25),
                       tmp_db)
        m._DECIMALS[(Chain.BSC.value, token)] = 18
        make_position(tmp_db, position_id=f"bsc_shadow_{i}", token=token, chain=Chain.BSC, mode=LaneMode.SHADOW)
    tmp_db.commit()
    rpc = _SlowDead(0.8)
    root = wd.FallbackPriceSource(_Sol(), m.PoolPriceSource(tmp_db, chain=Chain.BSC, transport=rpc), _Dex(), name="t")
    dog = wd.Watchdog(tmp_db, price_source=root, submitter=RecordingSubmitter())
    seen: dict[str, float] = {}
    orig = dog._check_position

    def stamp(position, cfg, report):
        seen.setdefault(position.position_id, _t.monotonic())
        return orig(position, cfg, report)

    monkeypatch.setattr(dog, "_check_position", stamp)
    first = dog.tick()
    assert rpc.sends == 1, f"{rpc.sends} pool sends: after a failed prime the quotes re-read the dead host alone"
    assert first.blind == 0  # DexScreener answered behind the refused pool leaf
    seen.clear()
    started = _t.monotonic()
    second = dog.tick()
    first_live = min(v for k, v in seen.items() if k.startswith("sol_live")) - started
    assert rpc.sends == 1, "the breaker is open: no send at all on the next tick"
    assert second.blind == 0 and first_live < 0.8, f"first live sol decision after {first_live:.3f} s"
    assert "bsc_pool_rpc_circuit_open" in (m.PoolPriceSource(tmp_db, chain=Chain.BSC, transport=rpc)
                                           .quote(Chain.BSC, "0x" + "61" * 20).note or "")


def test_the_prime_never_waits_for_a_slot_and_gives_the_host_about_a_second(tmp_db, monkeypatch):
    from kaiba.providers import _http

    _native(monkeypatch, Decimal("600"))
    monkeypatch.setattr(m, "_env_or_dotenv", lambda name: "https://pool.example.invalid")
    sent: list[dict] = []

    def fake_post(provider, endpoint, url, **kw):
        sent.append({"provider": provider, **kw})
        return _http._unavailable(provider, endpoint, "ConnectError: refused", "d")

    monkeypatch.setattr(_http, "post_json", fake_post)
    tokens, _rpc = book(tmp_db, 3)
    leaf = m.PoolPriceSource(tmp_db, chain=Chain.BSC)
    assert leaf.prime(Chain.BSC, tokens) == 0
    assert len(sent) == 1 and sent[0]["provider"] == m.BSC_PROVIDER
    assert sent[0]["wait_for_slot_s"] == 0.0 and sent[0]["timeout_s"] <= 1.0, sent[0]
    # the failure opened the host's breaker: neither the next prime nor a quote goes out
    assert leaf.prime(Chain.BSC, tokens) == 0 and not leaf.quote(Chain.BSC, tokens[0]).usable
    assert len(sent) == 1


def test_our_own_limiter_contention_does_not_open_the_breaker_and_a_success_closes_it(monkeypatch):
    from types import SimpleNamespace

    from kaiba.providers import _http

    notes = iter(["rate limited: bsc-pool-rpc: minimum interval (retry in 0.4s)", "ReadTimeout: timed out", None])
    sent: list[int] = []
    # aggregate3 -> one (true, abi.encode(uint256 18)) result, ABI-encoded as Result[]
    good = ("0x" + format(0x20, "064x") + format(1, "064x") + format(0x20, "064x") + format(1, "064x")
            + format(0x40, "064x") + format(0x20, "064x") + format(18, "064x"))

    def fake_post(provider, endpoint, url, **kw):
        sent.append(1)
        note = next(notes)
        if note is None:
            return _http.Fetched({"result": good}, _http.Receipt(provider=provider, endpoint=endpoint))
        return _http._unavailable(provider, endpoint, note, "d")

    monkeypatch.setattr(_http, "post_json", fake_post)
    send = m.bsc_transport(url="https://pool.example.invalid")
    key = m.host_key("https://pool.example.invalid")
    assert send([(m.WBNB, m.SEL_DECIMALS)]) == [None] and m.breaker_open(key) is None  # contention
    assert send([(m.WBNB, m.SEL_DECIMALS)]) == [None] and m.breaker_open(key) is not None  # the host failed
    assert send([(m.WBNB, m.SEL_DECIMALS)]) == [None] and len(sent) == 2, "open: nothing was sent"
    # X13 (2026-10-23): the window passes on the CLOCK -- the breaker's entry is not edited to an
    # already-expired one, which made "a success closes it" untestable (an expired entry reads as
    # closed whether or not the success removed it).
    fails, until = m._BREAKER[key]
    assert fails == 1 and until > m.time.monotonic()
    clock = SimpleNamespace(monotonic=lambda: until - 0.01)
    monkeypatch.setattr(m, "time", clock)
    assert send([(m.WBNB, m.SEL_DECIMALS)]) == [None] and len(sent) == 2, "still open 10 ms before the end"
    clock.monotonic = lambda: until + 0.01
    assert send([(m.WBNB, m.SEL_DECIMALS)]) == ["0x" + format(18, "064x")] and len(sent) == 3, "one probe went"
    # ... and the success CLOSED it: the entry is gone, so the next failure opens a FIRST window
    # (BREAKER_OPEN_S). A success that only let the window lapse would leave fails=1 behind and the
    # next failure would double it.
    assert key not in m._BREAKER and m.breaker_open(key) is None
    m.breaker_record(key, False)
    fails, until2 = m._BREAKER[key]
    assert fails == 1 and until2 - clock.monotonic() == pytest.approx(m.BREAKER_OPEN_S)


def test_the_settlement_walk_never_takes_its_replacement_mark_from_the_sampled_backup():
    """A foreign-pool mark on a settlement chain is replaced by the first native-settled layer after
    the pool reader; that replacement DECIDES the exit and becomes the remembered identity, so the
    sampled backup (corroboration only) must not be it."""
    asked: list[str] = []

    def backup(chain, token):
        asked.append(token)
        return wd.PriceQuote(price_usd=Decimal("0.5"), basis=wd.EvidenceBasis.PROVIDER_REPORTED,
                             source="codex-io:aggregate", chain=chain, token=token, settlement=wd.SETTLE_AGGREGATOR)

    token = "0x" + "3b" * 20
    foreign = wd.PriceQuote(price_usd=Decimal("1.0"), liquidity_usd=Decimal("50000"),
                            basis=wd.EvidenceBasis.PROVIDER_REPORTED, source="prices:dexscreener", chain=Chain.BSC,
                            token=token, pool_id="0x" + "3c" * 20, settlement=wd.SETTLE_FOREIGN_POOL)
    root = wd.FallbackPriceSource(wd.ProviderPriceSource(), m.BackupPriceSource(backup), name="bsc")
    assert wd.settle_foreign_mark(root, Chain.BSC, token, foreign) is foreign
    assert asked == []


# ----------------------------------------------------------------------------- 2026-10-23 follow-up: protection safety


PROTECTION_URLS = {Chain.BSC: "https://bsc-protection.example.io/v2/KEY", Chain.SOL: "https://sol-stops.example.io/",
                   Chain.ROBINHOOD: "https://rh-settings.example.io"}
_ENV = {Chain.BSC: "BSC_RPC_URL", Chain.SOL: "SOLANA_RPC_URL", Chain.ROBINHOOD: "ROBINHOOD_RPC_URL"}


@pytest.fixture
def protection_hosts(monkeypatch):
    """Point protection's own readers (Settings.rpc_for, read from the environment exactly as the box
    reads it, and the robinhood pacer's URL) at known hosts; returns a function to move them."""
    import kaiba.ingest.robinhood as rh
    from kaiba.core.config import get_settings

    def point(urls: dict = PROTECTION_URLS, rh_pacer: str = "https://rh-pacer.example.io") -> None:
        for chain, name in _ENV.items():
            monkeypatch.setenv(name, urls.get(chain, ""))
        monkeypatch.setattr(rh, "RPC_URL", rh_pacer)
        get_settings.cache_clear()
        m.clear_caches()

    point()
    yield point
    get_settings.cache_clear()


@pytest.mark.parametrize("pool_url", [
    "https://bsc-protection.example.io/v2/OTHER-KEY",  # protection's bsc host (BSC_RPC_URL)
    "https://bsc-dataseed.binance.org",  # its PUBLIC_RPC fallback, dialed whenever BSC_RPC_URL is unset
    "https://SOL-STOPS.example.io/rpc",  # SOLANA_RPC_URL's host
    "https://rh-settings.example.io",  # ROBINHOOD_RPC_URL's host
    "https://rh-pacer.example.io",  # the robinhood pacer's own URL
    "https://bsc-rpc.publicnode.com",  # the static floor
])
def test_the_pool_host_is_refused_when_it_is_any_host_protection_dials(monkeypatch, protection_hosts, pool_url):
    """2026-10-23: the forbidden set was the one literal ``bsc-rpc.publicnode.com``; it is now every host
    protection's own readers dial, read at runtime, so moving the box's BSC_RPC_URL cannot silently
    put pool reads on the stop host."""
    monkeypatch.setattr(m, "_env_or_dotenv", lambda name: pool_url)
    url, why = m.bsc_pool_rpc_url()
    assert url is None and why.startswith("bsc_pool_rpc_shares_protection_host:"), (pool_url, why)


def test_an_unrelated_pool_host_is_allowed_and_the_forbidden_set_follows_the_settings(monkeypatch, protection_hosts):
    monkeypatch.setattr(m, "_env_or_dotenv", lambda name: "https://bsc-dataseed.bnbchain.org")
    assert m.bsc_pool_rpc_url() == ("https://bsc-dataseed.bnbchain.org", "ok")
    # the box moves protection's bsc reads onto that very host: the pool reader must stop at once
    protection_hosts({**PROTECTION_URLS, Chain.BSC: "https://bsc-dataseed.bnbchain.org/"})
    assert m.bsc_pool_rpc_url()[0] is None
    assert "bsc-dataseed.bnbchain.org" in m.forbidden_pool_hosts()


def _agg3(items: list[tuple[bool, bytes]]) -> str:
    """ABI-encode Multicall3 ``Result[]`` = ``(bool success, bytes returnData)[]``."""
    def word(v: int) -> str:
        return format(v, "064x")

    heads, tails, offset = [], [], 32 * len(items)
    for ok, data in items:
        padded = data + b"\0" * ((32 - len(data) % 32) % 32)
        elem = word(1 if ok else 0) + word(0x40) + word(len(data)) + padded.hex()
        heads.append(word(offset))
        tails.append(elem)
        offset += len(elem) // 2
    return "0x" + word(0x20) + word(len(items)) + "".join(heads) + "".join(tails)


FLAP_ENTRY_TOKEN = "0x" + "12" * 20


def test_with_a_pool_host_the_flap_entry_rides_the_pool_key_and_keeps_the_portals_revert(tmp_db, monkeypatch,
                                                                                        protection_hosts):
    """2026-10-23: the Flap-curve paper ENTRY posted as provider ``rpc`` (the sol/bsc stop bucket) to
    protection's bsc host. With BSC_POOL_RPC_URL set it goes on the pool key and host, in one
    aggregate3, and the portal's TokenNotFound still reads as "not a Flap token" -- so the entry
    falls through to the pool exactly as on json_rpc_batch(keep_reverts=True)."""
    from kaiba.execution import evm_price as ep
    from kaiba.providers import _http

    monkeypatch.setattr(m, "_env_or_dotenv", lambda name: "https://pool.example.invalid")
    not_found = bytes.fromhex(ep.SEL_FLAP_TOKEN_NOT_FOUND[2:]) + bytes(12) + bytes.fromhex(FLAP_ENTRY_TOKEN[2:])
    sent: list[dict] = []

    def fake_post(provider, endpoint, url, **kw):
        sent.append({"provider": provider, "url": url, **kw})
        result = _agg3([(False, not_found), (True, (18).to_bytes(32, "big")), (True, (10**27).to_bytes(32, "big"))])
        return _http.Fetched({"result": result}, _http.Receipt(provider=provider, endpoint=endpoint))

    monkeypatch.setattr(_http, "post_json", fake_post)
    rpc, key = m.bsc_curve_entry_transport(tmp_db)
    assert key == m.BSC_PROVIDER
    assert ep.read_flap(FLAP_ENTRY_TOKEN, rpc) == (None, f"{ep.FLAP_NOT_A_FLAP_TOKEN}:TokenNotFound")
    assert [(s["provider"], s["url"], s["priority"]) for s in sent] == [
        (m.BSC_PROVIDER, "https://pool.example.invalid", m.Priority.ENTRY)]
    # ... and through paper_entry itself (no curve_rpc injected): nothing goes out as 'rpc'
    sent.clear()
    monkeypatch.setattr(m, "fill_native_usd", lambda chain, conn, at_ms=None: (Decimal("600"), "fixture"))
    monkeypatch.setattr(m, "entry_fill", lambda *a, **k: (None, "no pool in this test"))
    entry, why = m.paper_entry(Chain.BSC, FLAP_ENTRY_TOKEN, 10**17, tmp_db)
    assert entry is None and "flap_not_a_flap_token" in why and "pool:no pool in this test" in why, why
    assert sent and {s["provider"] for s in sent} == {m.BSC_PROVIDER}


def test_without_a_pool_host_the_flap_entry_keeps_its_host_but_can_never_penalise_rpc(
        tmp_db, monkeypatch, risk_file, protection_hosts):  # noqa: F811
    """Unset (the box today): the read the box already makes, on protection's bsc host, under its OWN
    limiter key -- a 429 cools that key down and leaves ``rpc`` (every sol and bsc stop) untouched."""
    import httpx

    monkeypatch.setattr(m, "_env_or_dotenv", lambda name: None)
    dialed: list[str] = []

    def four_twenty_nine(method, url, **kw):
        dialed.append(url)
        return httpx.Response(429, request=httpx.Request(method, url), text="slow down")

    monkeypatch.setattr(httpx, "request", four_twenty_nine)
    monkeypatch.setattr(m, "fill_native_usd", lambda chain, conn, at_ms=None: (Decimal("600"), "fixture"))
    rpc, key = m.bsc_curve_entry_transport(tmp_db)
    assert key == m.BSC_CURVE_ENTRY_PROVIDER != "rpc"
    entry, why = m.paper_entry(Chain.BSC, FLAP_ENTRY_TOKEN, 10**17, tmp_db)
    assert entry is None and why.startswith("curve:"), why
    assert dialed and all(u == PROTECTION_URLS[Chain.BSC] for u in dialed), dialed
    own = fetch_one(tmp_db, "SELECT penalty_level, banned_until_ms FROM provider_state WHERE provider=?",
                    (m.BSC_CURVE_ENTRY_PROVIDER,))
    assert own is not None and (own["penalty_level"] > 0 or own["banned_until_ms"] > now_ms()), own
    shared = fetch_one(tmp_db, "SELECT penalty_level, banned_until_ms FROM provider_state WHERE provider='rpc'")
    assert shared is None or (shared["penalty_level"] == 0 and shared["banned_until_ms"] == 0), shared
    assert fetch_one(tmp_db, "SELECT count(*) AS n FROM provider_family_bans WHERE provider='rpc'")["n"] == 0


def test_the_codex_corroboration_call_is_bounded_well_inside_a_tick(tmp_db, monkeypatch):
    """2026-10-23: the backup is asked on protection's tick thread with a stop waiting on it, and it
    inherited codex_io's 15 s HTTP timeout. Checked where the request is built (codex_io's own
    post_json call), not at the codex_backup_quote call site, so the whole plumbing is pinned."""
    from kaiba.providers import _http, codex_io

    sent: list[dict] = []

    def fake_post(provider, endpoint, url, **kw):
        sent.append(kw)
        return _http._unavailable(provider, endpoint, "ReadTimeout: timed out", "d")

    monkeypatch.setattr(codex_io, "_api_key", lambda: "k")
    monkeypatch.setattr(codex_io, "post_json", fake_post)
    monkeypatch.setattr(codex_io, "get_conn", lambda path=None: tmp_db)
    q = m.codex_backup_quote(Chain.BSC, "0x" + "c4" * 20)
    assert not q.usable and len(sent) == 1
    kw = sent[0]
    # one attempt; httpx's connect and read phases each at timeout_s; plus the slot wait
    assert kw["retries"] <= 1 and kw["timeout_s"] == m.CODEX_BACKUP_TIMEOUT_S
    assert kw["wait_for_slot_s"] + 2 * kw["timeout_s"] <= m.CODEX_BACKUP_MAX_S <= 4.0, kw
    # the research callers keep their own, longer timeout
    assert codex_io.HTTP_TIMEOUT_S == 15.0


def test_an_ordinary_quote_after_a_backup_corroborated_tick_is_remembered_again(tmp_db, monkeypatch, risk_file):  # noqa: F811
    """X08: the "never remembered" mark is per CHECK (``_validate_quote`` clears it first). Without
    that, one backup-corroborated tick left the position unremembered forever: every later ordinary
    quote decided its tick and none became the identity, so the next real stop on it was a suspect
    stop on a stale identity."""
    _native(monkeypatch, Decimal("500"))
    _codex(monkeypatch, price="0.5")
    token, pair = "0x" + "3d" * 20, "0x" + "3e" * 20
    m.remember_ref(m.PoolRef(Chain.BSC, token, pair, "v2", "pancake-v2", True, m.WBNB, 25), tmp_db)
    m._DECIMALS[(Chain.BSC.value, token)] = 18
    rpc = FakeRpc(_v2_table(token, pair, 1000 * 10**18, 1 * 10**18))  # $0.50: a stop
    make_position(tmp_db, position_id="pos_x8", token=token, chain=Chain.BSC, entry="1.0")
    tmp_db.commit()

    class Fails(QuoteSubmitter):
        def submit_exit(self, position, pct, *, quote, reason):  # noqa: ANN001
            super().submit_exit(position, pct, quote=quote, reason=reason)
            return wd.ExitOutcome(False, wd.OrderState.FAILED, None, "venue said no")

    root = wd.FallbackPriceSource(m.PoolPriceSource(tmp_db, chain=Chain.BSC, transport=rpc), SamePoolDex(pair, "0.5"),
                                  m.BackupPriceSource(m.codex_backup_quote), name="bsc")
    sub = Fails()
    dog = wd.Watchdog(tmp_db, price_source=root, submitter=sub)
    position = load_position(tmp_db, "pos_x8")
    dog._remember_quote(position, wd.load_state(tmp_db, position), SamePoolDex(pair, "1.0").quote(Chain.BSC, token),
                        None)
    dog.tick()
    assert sub.calls and sub.calls[0]["quote"].source == "codex-io:aggregate"
    assert dog._read_quote_memory(position)[0].source == "prices:dexscreener", "the backup answer is not remembered"
    # The pool recovers to the entry: an ordinary, non-stopping quote from the pool leaf.
    rpc.table.update({(k[0].lower(), k[1].lower()): v for k, v in _v2_table(token, pair, 1000 * 10**18,
                                                                          2 * 10**18).items()})
    dog.tick()
    remembered = dog._read_quote_memory(position)[0]
    assert remembered is not None and remembered.source == "evm-pool:pancake-v2", remembered
