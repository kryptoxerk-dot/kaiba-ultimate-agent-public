"""Ethereum and Base paper, design 2026-10-21 steps 6-8 (built 2026-10-23).

Skepticism rule: a chain gets paper entries only when (a) the entry fills at an executable on-chain
price at our size, (b) the watchdog can price the same token from the pool on its next tick, and (c)
those reads live on a limiter key and host no discovery / scan / snipe load shares and that protection
does not dial for another chain. These tests pin each leg, and the refusals where a leg cannot hold.

Fixtures (``tests/fixtures/eth_base_pool/pool_reads_20261023.json``) are REAL reads taken 2026-10-23
with this module's own call builders (eth.drpc.org and base-mainnet.public.blastapi.io, read-only, 16
sends): PEPE and USDC on ethereum, DEGEN and USDC on base -- public tokens, no wallet addresses.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from kaiba.core.db import fetch_all, jdump, jload
from kaiba.core.schemas import Chain, Lane, LaneMode, now_ms
from kaiba.execution import engine, viability
from kaiba.execution import evm_pool_price as m
from kaiba.execution import paper_costs as pc
from kaiba.execution import watchdog as wd
from tests.test_paper import make_decision, write_dossier
from tests.test_paper_entry_quote_evm import LANDED
from tests.test_watchdog import RecordingSubmitter, make_position, risk_file  # noqa: F401

FX = json.loads((Path(__file__).parent / "fixtures" / "eth_base_pool" / "pool_reads_20261023.json").read_text())
ETH_USD = Decimal("2390")
TICKET = 4 * 10**16  # 0.04 ETH
ETH_TOKEN = "0x" + "e7" * 20
BASE_TOKEN = "0x" + "ba" * 20

#: The box's protection hosts on 2026-10-23 (``~/.config/kaiba/.env`` read by host name only).
BOX_PROTECTION = [
    "https://bsc-rpc.publicnode.com", "https://solana-rpc.publicnode.com", "https://rpc.mainnet.chain.robinhood.com",
    "https://bsc-dataseed.binance.org", "https://api.mainnet-beta.solana.com",
]
GOOD_ENV = {"ETH_POOL_RPC_URL": "https://eth.drpc.org", "BASE_POOL_RPC_URL": "https://base-mainnet.public.blastapi.io",
            "BSC_POOL_RPC_URL": "https://bsc-dataseed1.defibit.io", "BSC_SNIPE_RPC_URL": "https://bsc-dataseed.bnbchain.org",
            "BSC_SNIPE_WS_URL": "wss://bsc-rpc.publicnode.com"}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    from kaiba.execution import onchain_pool as op

    monkeypatch.setattr(op, "after_tick", lambda watchdog, positions, cfg: False)
    m.clear_caches()
    m.set_backup_quote(None)
    viability.reset_venue_cache()
    viability.reset_protectable_cache()
    engine._POOL_REFUSALS.clear()
    yield
    m.clear_caches()
    m.set_backup_quote(None)
    viability.reset_venue_cache()
    viability.reset_protectable_cache()


def hosts(monkeypatch, **override):
    env = {**GOOD_ENV, **override}
    monkeypatch.setattr(m, "_env_or_dotenv", lambda name: env.get(name))
    monkeypatch.setattr(m, "protection_rpc_urls", lambda: list(BOX_PROTECTION))
    monkeypatch.setattr(m, "_setting", lambda name: None)
    m.clear_caches()


class FakeRpc:
    """A transport answering from a table keyed ``(to, data)``; counts every SEND."""

    def __init__(self, table=None, *, default=None) -> None:
        self.table = {(t.lower(), d.lower()): r for (t, d), r in (table or {}).items()}
        self.default = default
        self.sends: list[list[tuple[str, str]]] = []

    def __call__(self, calls):
        self.sends.append(list(calls))
        return [self.table.get((t.lower(), d.lower()), self.default) for t, d in calls]


def fixture_marks(key: str):
    r = FX[key]
    chain = Chain(key.split("_")[0])
    cands, v4marks, dec = m.uniswap_candidates(chain, r["token"], r["discovery"])
    marks, why = m.uniswap_marks(chain, r["token"], cands, r["state"], dec)
    return chain, r, cands, v4marks, dec, marks, why


def word(value: int) -> str:
    return "0x" + format(value, "064x")


# ----------------------------------------------------------------------------- (a) real reads, fills at our size


def test_a_real_eth_v2_pair_is_found_from_the_factory_and_fills_by_the_30_bps_formula():
    chain, r, cands, v4marks, dec, marks, why = fixture_marks("eth_pepe")
    assert dec == 18 and [c[0] for c in cands] == ["v2", "v3", "v3", "v3", "v3"]
    v2 = next(mk for mk in marks if mk.ref.kind == "v2")
    assert v2.ref.venue == "uniswap-v2" and v2.ref.fee == m.UNISWAP_V2_FEE_BPS == 30 and v2.ref.quote == m.WETH_ETH
    assert m.deepest([*v4marks, *marks]) is v2  # 6,334 WETH of real reserve beats every concentrated range
    # two out-of-range v3 pools are refused by name, never priced off a stale slot0
    assert sum("no_in_range_liquidity" in w for w in why) == 2
    fill, note = m.fill_from(v2.ref, v2, None, side="buy", amount_in=TICKET, native_usd=ETH_USD)
    r0, r1 = v2.reserves
    r_token, r_native = (r0, r1) if v2.ref.token_is0 else (r1, r0)
    assert fill.amount_out == TICKET * 9970 * r_token // (r_native * 10_000 + TICKET * 9970), note
    assert fill.source == "pool:uniswap-v2:v2_formula"
    gap = (fill.executable_price_usd / fill.spot_price_usd - 1) * 10_000
    assert 30 < gap < 31, gap  # the pair's 30 bps plus our impact against ~$30M of depth


def test_a_real_base_v3_pool_fills_through_quoterv2_at_the_pools_own_tier():
    chain, r, cands, v4marks, dec, marks, why = fixture_marks("base_degen")
    best = m.deepest([*v4marks, *marks])
    assert (best.ref.kind, best.ref.fee, best.ref.venue) == ("v3", 3000, "uniswap-v3") and why == []
    read = FX["base_degen"]["best"]["quote_read"]
    mark = m.mark_from(best.ref, read[:2], dec)
    fill, note = m.fill_from(best.ref, mark, read[2], side="buy", amount_in=TICKET, native_usd=ETH_USD)
    assert fill.basis == "quoter_v2" and fill.amount_out == int(read[2][2:66], 16), note
    gap = (fill.executable_price_usd / fill.spot_price_usd - 1) * 10_000
    assert 30 < gap < 32, gap  # 30 bps tier + ~1 bps of impact; the quoter, not slot0, is the fill
    assert m.pool_fee_bps(best.ref, mark) == 30


@pytest.mark.parametrize("key,fee,spacing,fee_bps", [("eth_pepe", 3000, 60, 35), ("eth_usdc", 100, 1, 2)])
def test_a_hookless_v4_pool_is_found_from_its_key_and_its_protocol_fee_is_charged(key, fee, spacing, fee_bps):
    """No log scan and no hint: a hookless pool's id is a function of its key. Its slot0 carries a
    protocol fee that v4 charges ON TOP of the LP fee (500 pips on PEPE/ETH, 25 on ETH/USDC)."""
    chain, r, cands, v4marks, dec, marks, why = fixture_marks(key)
    v4 = FX[key]["v4"]
    ref = next(mk.ref for mk in v4marks if mk.ref.pool == v4["pool"])
    assert (ref.fee, ref.tick_spacing, ref.quote, ref.hooks) == (fee, spacing, None, None)
    from kaiba.execution import onchain_pool as op

    assert ref.pool == op.v4_pool_id(m.ZERO, r["token"], fee, spacing, m.ZERO)  # native ETH sorts first
    mark = m.mark_from(ref, v4["quote_read"][:2], dec)
    assert m.pool_fee_bps(ref, mark) == fee_bps
    fill, note = m.fill_from(ref, mark, v4["quote_read"][2], side="buy", amount_in=TICKET, native_usd=ETH_USD)
    assert fill.basis == "v4_quoter" and fill.source == "pool:uniswap-v4:v4_quoter", note
    gap = (fill.executable_price_usd / fill.spot_price_usd - 1) * 10_000
    assert gap >= fee_bps - 1, gap  # the quoter ran the swap: the fee is in the fill


def test_the_quoters_and_factories_called_are_the_verified_uniswap_deployments():
    for chain in (Chain.ETH, Chain.BASE):
        d = m.UNISWAP[chain]
        calls = m.uniswap_discovery_calls(chain, ETH_TOKEN)
        assert calls[0][0] == d.v2_factory and {c[0] for c in calls[1:5]} == {d.v3_factory}
        assert {c[0] for c in calls[5:-1]} == {d.pool_manager} and calls[-1] == (ETH_TOKEN, m.SEL_DECIMALS)
        v3 = m.PoolRef(chain, ETH_TOKEN, "0x" + "31" * 20, "v3", "uniswap-v3", True, m.WRAPPED_NATIVE[chain], 3000)
        to, data = m._quoter_call(v3, TICKET, buy=True)
        assert to == d.v3_quoter and data[10:74].endswith(m.WRAPPED_NATIVE[chain][2:])  # a buy pays WETH in
        v4 = m.v4_no_hook_refs(chain, ETH_TOKEN)[0]
        assert m._quoter_call(v4, TICKET, buy=True)[0] == d.v4_quoter
        assert m.mark_calls(v4)[0][0] == d.pool_manager
    # robinhood's v4 reads still go to robinhood's PoolManager
    rh = m.PoolRef(Chain.ROBINHOOD, ETH_TOKEN, "0x" + "4" * 64, "v4", "uniswap-v4", False, None, 3000, 60, None)
    assert m.mark_calls(rh)[0][0] == m.RH_POOL_MANAGER


# ----------------------------------------------------------------------------- venue refusals


def _token_row(conn, chain: Chain, token: str, *, launchpad: str | None, meta: dict) -> None:
    conn.execute("INSERT OR REPLACE INTO tokens (chain, address, launchpad, first_seen_ms, meta_json) VALUES (?,?,?,?,?)",
                 (chain.value, token, launchpad, now_ms(), jdump(meta)))


@pytest.mark.parametrize("launchpad,meta,why", [
    ("", {"exchange": "0x420dd381b31aef6683db6b902084cb0ffece40da"}, "venue_unverified:aerodrome"),
    ("clanker", {"exchange": m.UNISWAP[Chain.BASE].pool_manager, "quote_address": m.WETH_BASE},
     "v4_hook_unverified:clanker"),
    ("flaunch", {"exchange": "0x23321f11a6d44fd1ab790044fdfde5758c902fdc"}, "venue_unverified:flaunch"),
    ("zora", {"exchange": m.UNISWAP[Chain.BASE].v3_factory, "quote_address": "0x1111111111166b7fe7bd91427724b487980afc69"},
     "quote_asset_not_native:0x1111111111166b7fe7bd91427724b487980afc69"),
    ("virtuals_v2", {"exchange": m.UNISWAP[Chain.BASE].v2_factory, "quote_address": "0x0b3e328455c4059eeb9e3f84b5543f74e24e7e1b"},
     "quote_asset_not_native:0x0b3e328455c4059eeb9e3f84b5543f74e24e7e1b"),
])
def test_unverified_launch_venues_and_foreign_quotes_are_refused_before_any_read(tmp_db, launchpad, meta, why):
    _token_row(tmp_db, Chain.BASE, BASE_TOKEN, launchpad=launchpad, meta=meta)
    rpc = FakeRpc()
    assert m.resolve_uniswap(Chain.BASE, BASE_TOKEN, rpc, tmp_db) == (None, why)
    assert rpc.sends == [], "a recorded venue we refuse costs no read"
    assert m.resolve_uniswap(Chain.BASE, BASE_TOKEN, rpc, tmp_db)[1] == f"{why} (cached)"


def test_a_plain_uniswap_record_is_not_refused_and_the_chain_read_decides(tmp_db):
    _token_row(tmp_db, Chain.ETH, ETH_TOKEN, launchpad="pool_uniswap_v3",
               meta={"exchange": m.UNISWAP[Chain.ETH].v3_factory, "quote_address": m.WETH_ETH})
    assert m.recorded_venue_refusal(Chain.ETH, ETH_TOKEN, tmp_db) is None
    rpc = FakeRpc(default="0x" + "0" * 64)  # every factory says "no pool", every v4 slot is empty
    rpc.table[(ETH_TOKEN, m.SEL_DECIMALS)] = word(18)
    assert m.resolve_uniswap(Chain.ETH, ETH_TOKEN, rpc, tmp_db) == (None, "no_native_pool")
    assert len(rpc.sends) == 1


# ----------------------------------------------------------------------------- (c) own key, own host, own operator


def test_an_unset_eth_pool_host_refuses_and_never_falls_back_to_publicnode(tmp_db, monkeypatch):
    hosts(monkeypatch, ETH_POOL_RPC_URL=None)
    monkeypatch.setenv("ETH_RPC_URL", "https://ethereum-rpc.publicnode.com")  # rpc_for(eth) is NOT a fallback

    def no_network(*a, **k):
        raise AssertionError("an eth pool read went out with no eth pool host configured")

    monkeypatch.setattr("kaiba.providers._http.post_json", no_network)
    assert m.pool_rpc_url(Chain.ETH) == (None, "eth_pool_rpc_unset")
    q = m.PoolPriceSource(tmp_db, chain=Chain.ETH).quote(Chain.ETH, ETH_TOKEN)
    assert not q.usable and "eth_pool_rpc_unset" in (q.note or "")
    assert m.entry_fill(Chain.ETH, ETH_TOKEN, TICKET, tmp_db)[1] in ("eth_pool_rpc_unset", "paper_native_usd_missing")
    assert m.gas_price_wei(Chain.ETH, tmp_db) == (None, "eth_pool_rpc_unset")


@pytest.mark.parametrize("chain,url,why", [
    (Chain.ETH, "https://ethereum-rpc.publicnode.com", "eth_pool_rpc_shares_host:ethereum-rpc.publicnode.com"),
    (Chain.ETH, "https://eth-mainnet.publicnode.com", "eth_pool_rpc_shares_operator:publicnode.com"),
    (Chain.BASE, "https://mainnet.base.org", "base_pool_rpc_shares_host:mainnet.base.org"),
    (Chain.BASE, "https://developer-access-mainnet.base.org", "base_pool_rpc_shares_operator:base.org"),
    (Chain.ETH, "https://rpc.mevblocker.io", "eth_pool_rpc_shares_host:rpc.mevblocker.io"),  # stockereum's
    (Chain.ETH, "https://bsc-dataseed1.defibit.io", "eth_pool_rpc_shares_host:bsc-dataseed1.defibit.io"),  # bsc pool
    (Chain.BASE, "https://eth.drpc.org", "base_pool_rpc_shares_host:eth.drpc.org"),  # the eth pool host
    (Chain.BASE, "https://base.drpc.org", "base_pool_rpc_shares_operator:drpc.org"),  # same operator as eth's
])
def test_a_pool_host_shared_with_protection_discovery_or_the_other_chain_is_refused(monkeypatch, chain, url, why):
    hosts(monkeypatch, **{m.POOL_RPC_ENV[chain]: url})
    assert m.pool_rpc_url(chain) == (None, why)


#: Hosts ONLY protection dials: in no public table (dyor_verify.PUBLIC_RPC, watch_wallets,
#: stockereum.RPCS), not the robinhood pacer, not another pool or feed env. Every case above also
#: sits in one of those, so dropping protection_rpc_urls() from shared_rpc_urls() passed them all
#: (2026-10-23 review, blocking #2). The key in the alchemy URLs is a placeholder.
PROTECTION_ONLY = [
    # exact host: one multi-chain host, our sol stops on it -> the eth / base pool may not use it
    (Chain.ETH, "SOLANA_RPC_URL", "https://rpc.ankr.com/solana", "https://rpc.ankr.com/eth", "eth_pool_rpc_shares_host:rpc.ankr.com"),
    (Chain.BASE, "BSC_RPC_URL", "https://rpc.ankr.com/bsc", "https://rpc.ankr.com/base", "base_pool_rpc_shares_host:rpc.ankr.com"),
    # operator: the review's case, the box's own eth pool host with sol stops on the same operator
    (Chain.ETH, "SOLANA_RPC_URL", "https://solana.drpc.org", "https://eth.drpc.org", "eth_pool_rpc_shares_operator:drpc.org"),
    (Chain.ETH, "SOLANA_RPC_URL", "https://solana-mainnet.g.alchemy.com/v2/PLACEHOLDER",
     "https://eth-mainnet.g.alchemy.com/v2/PLACEHOLDER", "eth_pool_rpc_shares_operator:alchemy.com"),
    # the box's own base pool host with bsc stops on the same operator
    (Chain.BASE, "BSC_RPC_URL", "https://bsc-mainnet.public.blastapi.io", "https://base-mainnet.public.blastapi.io",
     "base_pool_rpc_shares_operator:blastapi.io"),
]


@pytest.mark.parametrize("chain,stop_env,stop_url,pool_url,why", PROTECTION_ONLY)
def test_a_pool_host_only_protection_dials_is_refused(monkeypatch, chain, stop_env, stop_url, pool_url, why):
    hosts(monkeypatch, **{m.POOL_RPC_ENV[chain]: pool_url})
    assert m.pool_rpc_url(chain) == (pool_url, "ok"), "control: the host is allowed while protection is elsewhere"
    monkeypatch.setattr(m, "protection_rpc_urls", lambda: [*BOX_PROTECTION, stop_url])
    m.clear_caches()
    assert m.pool_rpc_url(chain) == (None, why)


@pytest.mark.parametrize("chain,stop_env,stop_url,pool_url,why", PROTECTION_ONLY)
def test_the_protection_host_is_read_from_the_settings_protection_reads(monkeypatch, chain, stop_env, stop_url, pool_url,
                                                                     why):
    """The same refusal through the REAL ``protection_rpc_urls`` (Settings.rpc_for, from the environment the
    way the box reads it), so moving the box's SOLANA_RPC_URL / BSC_RPC_URL can never put a pool read on
    a stop host without this failing."""
    import kaiba.ingest.robinhood as rh
    from kaiba.core.config import get_settings

    env = {**GOOD_ENV, m.POOL_RPC_ENV[chain]: pool_url}
    monkeypatch.setattr(m, "_env_or_dotenv", lambda name: env.get(name))
    monkeypatch.setattr(m, "_setting", lambda name: None)
    monkeypatch.setattr(rh, "RPC_URL", "https://rpc.mainnet.chain.robinhood.com")
    stops = {"SOLANA_RPC_URL": "https://api.mainnet-beta.solana.com", "BSC_RPC_URL": "https://bsc-dataseed.binance.org",
             "ROBINHOOD_RPC_URL": "", "ETH_RPC_URL": "", "BASE_RPC_URL": ""}
    for name, value in stops.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    m.clear_caches()
    try:
        assert m.pool_rpc_url(chain) == (pool_url, "ok"), "control: protection elsewhere"
        monkeypatch.setenv(stop_env, stop_url)
        get_settings.cache_clear()
        m.clear_caches()
        assert stop_url in m.protection_rpc_urls()
        assert m.pool_rpc_url(chain) == (None, why)
    finally:
        get_settings.cache_clear()


def test_the_chosen_hosts_are_allowed_and_read_on_their_own_keys(monkeypatch):
    hosts(monkeypatch)
    assert m.pool_rpc_url(Chain.ETH) == ("https://eth.drpc.org", "ok")
    assert m.pool_rpc_url(Chain.BASE) == ("https://base-mainnet.public.blastapi.io", "ok")
    assert m.POOL_PROVIDER[Chain.ETH] == "eth-pool-rpc" and m.POOL_PROVIDER[Chain.BASE] == "base-pool-rpc"
    assert "rpc" not in (m.POOL_PROVIDER[Chain.ETH], m.POOL_PROVIDER[Chain.BASE])
    assert m.operator_of("eth.drpc.org") == "drpc.org" and m.operator_of("a.b.co.uk") == "b.co.uk"
    assert m.operator_of("127.0.0.1") == "127.0.0.1"


def test_an_eth_pool_429_lands_on_eth_pool_rpc_never_on_rpc(tmp_db, monkeypatch, risk_file):  # noqa: F811
    import httpx

    from kaiba.core.db import fetch_one

    hosts(monkeypatch)

    def four_twenty_nine(method, url, **kw):
        return httpx.Response(429, request=httpx.Request(method, url), text="slow down")

    monkeypatch.setattr(httpx, "request", four_twenty_nine)
    send = m.pool_transport(Chain.ETH, tmp_db, url="https://eth.drpc.org", wait_for_slot_s=0.0)
    assert send([(m.WETH_ETH, m.SEL_DECIMALS)]) == [None]
    pool = fetch_one(tmp_db, "SELECT penalty_level, banned_until_ms FROM provider_state WHERE provider=?",
                     ("eth-pool-rpc",))
    assert pool is not None and (pool["penalty_level"] > 0 or pool["banned_until_ms"] > now_ms()), pool
    shared = fetch_one(tmp_db, "SELECT penalty_level, banned_until_ms FROM provider_state WHERE provider='rpc'")
    assert shared is None or (shared["penalty_level"] == 0 and shared["banned_until_ms"] == 0), shared
    assert m.breaker_open(m.host_key("https://eth.drpc.org")) is not None  # and the host's breaker opened


@pytest.mark.parametrize("chain,url", [(Chain.ETH, "https://eth.drpc.org"),
                                       (Chain.BASE, "https://base-mainnet.public.blastapi.io")])
def test_a_gas_price_429_lands_on_the_pool_key_never_on_rpc(tmp_db, monkeypatch, risk_file, chain, url):  # noqa: F811
    """eth_gasPrice runs on every eth / base paper entry and venue read, on the pool host. A 429 there
    sent through ``rpc`` would raise the provider-level penalty sol and bsc STOP reads pay -- the
    coupling the pool key exists to rule out (2026-10-23 review, blocking #3: the 429 test covered only
    ``pool_transport``, and changing the gas read's key to ``rpc`` passed every test)."""
    import httpx

    from kaiba.core.db import fetch_one

    hosts(monkeypatch)
    sent: list[str] = []

    def four_twenty_nine(method, target, **kw):
        sent.append(target)
        return httpx.Response(429, request=httpx.Request(method, target), text="slow down")

    monkeypatch.setattr(httpx, "request", four_twenty_nine)
    assert m.gas_price_wei(chain, tmp_db) == (None, "gas_price_unreadable")
    assert sent == [url], sent  # the pool host, once
    pool = fetch_one(tmp_db, "SELECT penalty_level, banned_until_ms FROM provider_state WHERE provider=?",
                     (m.POOL_PROVIDER[chain],))
    assert pool is not None and (pool["penalty_level"] > 0 or pool["banned_until_ms"] > now_ms()), pool
    shared = fetch_one(tmp_db, "SELECT penalty_level, banned_until_ms FROM provider_state WHERE provider='rpc'")
    assert shared is None or (shared["penalty_level"] == 0 and shared["banned_until_ms"] == 0), shared
    assert m.breaker_open(m.host_key(url)) is not None


# ----------------------------------------------------------------------------- (b) protection: the pool, every tick


def _v2_book(tmp_db, chain: Chain, n: int):
    rpc = FakeRpc()
    tokens = []
    for i in range(n):
        token, pair = "0x" + f"{i + 0x11:02x}" * 20, "0x" + f"{i + 0x61:02x}" * 20
        m.remember_ref(m.PoolRef(chain, token, pair, "v2", "uniswap-v2", True, m.WRAPPED_NATIVE[chain], 30), tmp_db)
        m._DECIMALS[(chain.value, token)] = 18
        rpc.table[(pair.lower(), m.SEL_GET_RESERVES)] = word(1000 * 10**18) + format(2 * 10**18, "064x") + format(1, "064x")
        make_position(tmp_db, position_id=f"pos_{chain.value}_{i}", token=token, chain=chain, entry="1.0",
                      mode=LaneMode.SHADOW)
        tokens.append(token)
    tmp_db.commit()
    return tokens, rpc


@pytest.mark.parametrize("chain", [Chain.ETH, Chain.BASE])
def test_a_tick_over_six_positions_reads_the_whole_book_in_one_pool_call(tmp_db, monkeypatch, risk_file, chain):  # noqa: F811
    monkeypatch.setattr(m, "mark_native_usd", lambda c, conn: (Decimal("2000"), "fixture"))
    tokens, rpc = _v2_book(tmp_db, chain, 6)
    root = wd.FallbackPriceSource(m.PoolPriceSource(tmp_db, chain=chain, transport=rpc), name="test")
    dog = wd.Watchdog(tmp_db, price_source=root, submitter=RecordingSubmitter())
    report = dog.tick()
    assert report.checked == 6 and report.blind == 0
    assert len(rpc.sends) == 1, f"{len(rpc.sends)} pool calls in one tick; the prime did not batch"
    q = m.PoolPriceSource(tmp_db, chain=chain, transport=rpc).quote(chain, tokens[0])
    assert q.source == "evm-pool:uniswap-v2" and q.pool_id and q.liquidity_kind == "pool_reserve_usd"
    assert q.settlement == wd.SETTLE_NATIVE_POOL and q.price_usd == Decimal("4.000")


# 2026-10-23 review, blocking #1: a quiet eth / base token's position never closed. eth / base have no
# wallet feed, so the swaps tape is empty for nearly every token; a pool's mark only moves when someone
# trades; nothing closes manual or sm-trenches positions by age. Such a position sat between the -30%
# stop and TP1 for good, out of the scoreboard and holding a per-(lane, chain) shadow slot and one of the
# 25 every chain draws from. The pool protection reads every tick is now the activity clock.

QUIET_S = 3_700  # past protection.stale_no_volume_exit_s (3600) in the shipped risk.yaml


def _quiet_book(tmp_db, monkeypatch, chain: Chain):
    """One manual-lane paper position on ``chain``, 3 h old at -15% (pool $4.00, entry $4.70), off every
    tape, whose v2 pool protection first read ``QUIET_S`` ago and has not changed since."""
    monkeypatch.setattr(m, "mark_native_usd", lambda c, conn: (Decimal("2000"), "fixture"))
    (token,), rpc = _v2_book(tmp_db, chain, 1)
    tmp_db.execute("UPDATE positions SET lane=?, entry_price_usd='4.7', peak_price_usd='4.7', opened_ms=?",
                   (Lane.MANUAL.value, now_ms() - 3 * 3_600_000))
    tmp_db.commit()
    src = m.PoolPriceSource(tmp_db, chain=chain, transport=rpc)
    quiet = now_ms() - QUIET_S * 1000
    with monkeypatch.context() as earlier:
        earlier.setattr(m, "now_ms", lambda: quiet)
        assert src.prime(chain, [token]) == 1  # protection's read an hour ago: the clock's first read
    submitter = RecordingSubmitter()
    dog = wd.Watchdog(tmp_db, price_source=wd.FallbackPriceSource(src, name="test"), submitter=submitter)
    return token, rpc, quiet, dog, submitter, src


@pytest.mark.parametrize("chain", [Chain.ETH, Chain.BASE])
def test_a_position_on_a_pool_nobody_trades_closes_by_stale_no_volume(tmp_db, monkeypatch, risk_file, chain):  # noqa: F811
    token, rpc, quiet, dog, submitter, _src = _quiet_book(tmp_db, monkeypatch, chain)
    position = wd.open_positions(tmp_db)[0]
    assert fetch_all(tmp_db, "SELECT 1 FROM swaps WHERE chain=? AND token=?", (chain.value, token)) == []
    assert dog._last_trade_ms(position) == quiet, "the pool clock is the only activity record off the tape"
    report = dog.tick()  # the prime reads the same reserves: no trade, the silence holds
    assert report.blind == 0 and report.exits == 1, report
    (pid, _pct, reason), = submitter.calls
    assert pid == position.position_id and reason.startswith("stale_no_volume:"), reason
    assert int(reason.split(":")[1].rstrip("s")) >= QUIET_S
    stored = jload(fetch_all(tmp_db, "SELECT value FROM kv WHERE key=?",
                             (f"{wd.RESERVE_CLOCK_PREFIX}{chain.value}:{token}",))[0]["value"], {})
    assert stored["moved_ms"] == quiet, stored  # mirrored, so a protection restart keeps the silence


@pytest.mark.parametrize("chain", [Chain.ETH, Chain.BASE])
def test_a_pool_that_traded_since_the_last_read_holds_the_position(tmp_db, monkeypatch, risk_file, chain):  # noqa: F811
    token, rpc, quiet, dog, submitter, _src = _quiet_book(tmp_db, monkeypatch, chain)
    pair = next(t for (t, d) in rpc.table if d == m.SEL_GET_RESERVES)
    # someone bought: 1 ETH in, ~3 tokens out, the price barely moves (still -15%)
    rpc.table[(pair, m.SEL_GET_RESERVES)] = word(997 * 10**18) + format(2003 * 10**15, "064x") + format(2, "064x")
    report = dog.tick()
    assert report.blind == 0 and report.exits == 0 and submitter.calls == [], submitter.calls
    position = wd.open_positions(tmp_db)[0]
    assert now_ms() - dog._last_trade_ms(position) < 60_000


@pytest.mark.parametrize("chain", [Chain.ETH, Chain.BASE])
def test_a_pool_read_alone_also_moves_the_clock(tmp_db, monkeypatch, risk_file, chain):  # noqa: F811
    """A tick whose prime was refused by our own limiter (``PRIME_WAIT_FOR_SLOT_S`` 0), or a token first
    seen this tick, is priced by ``quote`` reading its pool alone. That read must move the clock too,
    or a pool that traded would be called silent on the evidence of an older read."""
    token, rpc, quiet, dog, submitter, src = _quiet_book(tmp_db, monkeypatch, chain)
    pair = next(t for (t, d) in rpc.table if d == m.SEL_GET_RESERVES)
    rpc.table[(pair, m.SEL_GET_RESERVES)] = word(997 * 10**18) + format(2003 * 10**15, "064x") + format(2, "064x")
    m._PRIMED.clear()  # nothing primed this tick
    q = src.quote(chain, token)
    assert q.usable and "read alone" in (q.note or ""), q
    assert now_ms() - m.pool_clock(chain, token)[2] < 60_000
    assert now_ms() - dog._last_trade_ms(wd.open_positions(tmp_db)[0]) < 60_000


@pytest.mark.parametrize("chain", [Chain.ETH, Chain.BASE])
def test_the_pool_silence_survives_a_protection_restart(tmp_db, monkeypatch, risk_file, chain):  # noqa: F811
    token, rpc, quiet, dog, submitter, _src = _quiet_book(tmp_db, monkeypatch, chain)
    position = wd.open_positions(tmp_db)[0]
    assert dog._last_trade_ms(position) == quiet  # mirrored to kv
    m._MOVED.clear()  # what a restart loses: the in-process clock (refs and decimals reload from kv)
    assert dog._last_trade_ms(position) is None, "never read by this process: unknown, so it never sells"
    report = dog.tick()  # the new process reads the SAME reserves: unchanged since before the restart
    assert report.exits == 1 and submitter.calls[0][2].startswith("stale_no_volume:"), submitter.calls


@pytest.mark.parametrize("key", ["base_degen", "eth_pepe"])  # a real v3 pool, a real hookless v4 pool
def test_the_pool_clock_moves_on_a_swap_or_a_liquidity_change_and_not_on_a_repeat_read(key):
    from dataclasses import replace

    chain, r, cands, v4marks, dec, marks, why = fixture_marks(key)
    if key == "base_degen":
        ref, read = m.deepest([*v4marks, *marks]).ref, FX[key]["best"]["quote_read"][:2]
    else:
        ref = next(mk.ref for mk in v4marks if mk.ref.pool == FX[key]["v4"]["pool"])
        read = FX[key]["v4"]["quote_read"][:2]
    assert ref.kind == ("v3" if key == "base_degen" else "v4")

    def bump(raw: str) -> str:  # +1 in the low bits of the first word: sqrtPriceX96 in v3 slot0 and v4 slot0
        return "0x" + format(int(raw[2:66], 16) + 1, "064x") + raw[66:]

    first = m.mark_from(ref, read, dec, observed_ms=1_000)
    assert first.state is not None and m.pool_state(first)[1:] == first.state
    m.note_pool_state(first)
    m.note_pool_state(replace(first, observed_ms=2_000))
    assert m.pool_clock(chain, ref.token) == (m.pool_state(first), 1_000, 1_000), "a repeat read is no trade"
    m.note_pool_state(m.mark_from(ref, [bump(read[0]), read[1]], dec, observed_ms=3_000))
    assert m.pool_clock(chain, ref.token)[1:] == (1_000, 3_000), "sqrtPrice moved: a swap"
    m.note_pool_state(m.mark_from(ref, [bump(read[0]), bump(read[1])], dec, observed_ms=4_000))
    assert m.pool_clock(chain, ref.token)[1:] == (1_000, 4_000), "liquidity moved"
    bsc = replace(first, ref=replace(ref, chain=Chain.BSC))
    m.note_pool_state(bsc)
    assert m.pool_clock(Chain.BSC, ref.token) is None  # bsc and robinhood keep their tapes; no clock written


def test_eth_and_base_route_to_the_pool_and_never_reach_dexscreener(tmp_db):
    from kaiba.execution import evm_price as ep

    routed = ep.venue_price_source()
    for chain in (Chain.ETH, Chain.BASE):
        route = routed.routes[chain]
        leaves = list(wd.source_leaves(routed, chain))
        assert isinstance(leaves[0], m.PoolPriceSource) and leaves[0].chain is chain
        assert isinstance(leaves[-1], m.BackupPriceSource) and wd.corroboration_only(leaves[-1])
        assert not any(isinstance(leaf, wd.ProviderPriceSource) for leaf in leaves), route
    assert m.CODEX_BACKUP_CHAINS >= {Chain.ETH, Chain.BASE}


# ----------------------------------------------------------------------------- the entry probe


class _Answer:
    def __init__(self, source: str) -> None:
        self.source = source

    def quote(self, chain, token):
        return wd.PriceQuote(price_usd=Decimal("0.01"), basis=wd.EvidenceBasis.PROVIDER_REPORTED,
                             observed_ms=now_ms(), source=self.source, chain=chain, token=token)


@pytest.mark.parametrize("chain", [Chain.ETH, Chain.BASE])
def test_the_probe_admits_a_token_only_when_the_pool_leaf_priced_it(monkeypatch, chain):
    monkeypatch.setattr(wd, "resolve_price_source", lambda name: _Answer("prices:dexscreener"))
    ok, why = viability.quote_asset_is_protectable(chain, ETH_TOKEN)
    assert ok is False and why.startswith("protection_path_not_the_pool")
    monkeypatch.setattr(wd, "resolve_price_source", lambda name: _Answer("gmgn:token.info"))
    viability.reset_protectable_cache()
    assert viability.quote_asset_is_protectable(chain, ETH_TOKEN)[0] is False
    monkeypatch.setattr(wd, "resolve_price_source", lambda name: _Answer("evm-pool:uniswap-v3"))
    viability.reset_protectable_cache()
    assert viability.quote_asset_is_protectable(chain, ETH_TOKEN) == (True, "protectable")
    # a chain that already trades is unchanged: any usable answer still protects it
    monkeypatch.setattr(wd, "resolve_price_source", lambda name: _Answer("prices:dexscreener"))
    viability.reset_protectable_cache()
    assert viability.quote_asset_is_protectable(Chain.BSC, ETH_TOKEN)[0] is True


def test_the_entry_gate_asks_the_probe_about_the_positions_own_token(monkeypatch):
    asked = []
    monkeypatch.setattr(engine, "_protectability_probe", lambda chain, token: asked.append((chain, token)) or (False, "x"))
    for chain, token in ((Chain.ETH, ETH_TOKEN), (Chain.BASE, BASE_TOKEN)):
        assert engine._protection_refusal(make_decision(chain=chain, token=token, size=TICKET)) == "x"
    assert asked == [(Chain.ETH, ETH_TOKEN), (Chain.BASE, BASE_TOKEN)]


# ----------------------------------------------------------------------------- the paper-only guard


def _guard_cfg(mode: LaneMode):
    from kaiba.core.config import load_risk

    cfg = load_risk()
    cfg.global_mode = LaneMode.LIVE
    cfg.bounds.max_lane_mode = LaneMode.LIVE
    cfg.entries_paused = False
    cfg.kill_switch = False
    cfg.chains[Chain.ETH] = cfg.chain_budget(Chain.ETH).model_copy(update={
        "enabled": False, "bankroll_base_units": 10**18, "min_position_base_units": TICKET,
        "max_position_base_units": TICKET})
    lane = cfg.lane(Lane.SM_TRENCHES)
    cfg.lanes[Lane.SM_TRENCHES] = lane.model_copy(update={"mode": mode, "chains": [*lane.chains, Chain.ETH]})
    return cfg


@pytest.mark.parametrize("mode", [LaneMode.LIVE, LaneMode.CANARY])
def test_eth_with_a_notional_bankroll_can_never_trade_for_real(tmp_db, monkeypatch, mode):
    from kaiba.execution.risk import RiskGate

    cfg = _guard_cfg(mode)
    verdict = RiskGate(lambda: cfg).check_entry(Chain.ETH, Lane.SM_TRENCHES, TICKET, tmp_db, token=ETH_TOKEN)
    assert not verdict.allowed and verdict.reason == "chain_disabled", verdict
    monkeypatch.setattr(engine, "get_risk", lambda: cfg)
    if mode is LaneMode.LIVE:
        assert engine._local_refusal(Chain.ETH, LaneMode.LIVE, tmp_db) == "chain_disabled"


def test_the_same_config_in_shadow_sizes_paper_at_the_flat_ticket(tmp_db, monkeypatch):
    from kaiba.execution.risk import RiskGate

    cfg = _guard_cfg(LaneMode.SHADOW)

    def no_band(*a, **k):
        raise RuntimeError("no pool in this test")

    real_band = viability.sizing_band
    monkeypatch.setattr(viability, "sizing_band", no_band)
    gate = RiskGate(lambda: cfg)
    assert gate.position_size(Chain.ETH, Lane.SM_TRENCHES, 80.0, tmp_db, token=ETH_TOKEN) == TICKET
    monkeypatch.setattr(viability, "sizing_band", real_band)
    hosts(monkeypatch, ETH_POOL_RPC_URL=None)  # the entry's own cost check then refuses, NOT the chain guard
    verdict = gate.check_entry(Chain.ETH, Lane.SM_TRENCHES, TICKET, tmp_db, token=ETH_TOKEN)
    assert verdict.reason != "chain_disabled", verdict


# ----------------------------------------------------------------------------- costs


def test_a_mark_priced_eth_leg_pays_the_worst_tier_and_a_quoter_fill_pays_none_of_it():
    # quoter / v2 fills carry their pool's own fee: router only
    assert pc.exit_bps(Chain.ETH, 0, venue_fee_in_price=True) == 100
    # a leg priced at a MARK: router 100 + (Uniswap's top tier 100 - the broker's own 60) = 200 with the broker
    assert pc.exit_bps(Chain.ETH, 0) == 140 and pc.exit_bps(Chain.BASE, 0) == 140
    assert pc.broker_fee_bps(Chain.ETH) == 60 and pc.venue_charge_bps(Chain.BASE, False, None) == 40
    assert pc.entry_bps(Chain.ETH, 0, venue_fee_in_price=True) == 100 and Chain.ETH in pc.ASSUMED_ROUTER_FEE_CHAINS


def test_a_gas_spike_can_never_make_paper_cheaper_than_live():
    # MEASURED 2026-10-23: eth 0.062 gwei -- 3.6e13 wei a round trip against the broker's 4e14: no extra
    assert pc.gas_extra_bps(Chain.ETH, 62_119_446, TICKET) == 0
    assert pc.gas_extra_bps(Chain.BASE, 6_000_000, TICKET) == 0
    # 2 gwei: 250,000 x 2e9 x 2.3 = 1.15e15 wei; less the broker's 2 x 2e14 = 7.5e14 = 187.5 bps of 0.04 ETH
    assert pc.gas_extra_bps(Chain.ETH, 2 * 10**9, TICKET) == 188
    assert pc.entry_bps(Chain.ETH, 0, venue_fee_in_price=True, gas_bps=188) == 288


def test_an_unknown_token_tax_refuses_an_eth_or_base_paper_entry():
    for chain in (Chain.ETH, Chain.BASE):
        assert pc.leg_tax_bps(chain, None) == (None, pc.PAPER_TAX_UNKNOWN)
        assert pc.leg_tax_bps(chain, -10_000) == (None, pc.PAPER_TAX_UNKNOWN)  # GMGN's "-1"
        assert pc.leg_tax_bps(chain, 300) == (300, None)


def test_an_exit_converted_at_the_placeholder_native_price_leaves_the_scoreboard(tmp_db):
    pc.record_entry_basis(tmp_db, "pos_np", Chain.ETH, source="pool:uniswap-v2:v2_formula", fill="v2_formula",
                          buy_tax_bps=0, sell_tax_bps=0, tax_in_price=False, landing=LANDED)
    tag = pc.record_exit_basis(tmp_db, "pos_np", Chain.ETH, mark_source="evm-pool:uniswap-v2", fill="v2_formula",
                               tax_bps=0, filled=True)
    assert pc.counts_on_scoreboard(Chain.ETH, tag) and tag["entry"]["router_fee_assumed"] is True
    tag = pc.record_exit_basis(tmp_db, "pos_np", Chain.ETH, mark_source="evm-pool:uniswap-v2", fill="v2_formula",
                               tax_bps=0, filled=True, native_placeholder=True)
    assert tag[pc.NATIVE_PLACEHOLDER] is True and not pc.counts_on_scoreboard(Chain.ETH, tag)


# ----------------------------------------------------------------------------- the engine: pool-only entries


@pytest.mark.parametrize("chain,token", [(Chain.ETH, ETH_TOKEN), (Chain.BASE, BASE_TOKEN)])
def test_a_pool_refusal_on_eth_or_base_is_never_rescued_by_the_dossier(tmp_db, monkeypatch, chain, token):
    dossier = write_dossier(tmp_db, token=token, chain=chain, price="0.5", liquidity="9999")
    monkeypatch.setattr(m, "paper_entry", lambda *a, **k: (None, "pool:no_native_pool"))
    decision = make_decision(chain=chain, token=token, size=TICKET, ts=now_ms())
    assert engine.paper_entry_quote(decision, dossier, tmp_db) == (None, None, "pool_refused", None)
    assert engine._POOL_REFUSALS[decision.decision_id] == "pool:no_native_pool"


def _eth_handoff(tmp_db, monkeypatch, *, native=True, gas=62_119_446, tax=0):
    from kaiba.core.schemas import EvidenceBasis, Measure, Receipt

    dossier = write_dossier(tmp_db, token=ETH_TOKEN, chain=Chain.ETH, price="0.0006", liquidity="50000")
    if tax is not None:
        mk = Measure(value=Decimal(tax), basis=EvidenceBasis.PROVIDER_REPORTED,
                     receipt=Receipt(provider="fixture", endpoint="dyor"), freshness_budget_s=86_400)
        dossier = dossier.model_copy(update={"buy_tax_bps": mk, "sell_tax_bps": mk})
        tmp_db.execute("UPDATE token_dossiers SET dossier_json=? WHERE chain='eth' AND address=?",
                       (dossier.model_dump_json(), ETH_TOKEN))
    entry = m.PaperEntry(source="pool:uniswap-v2:v2_formula", price_usd=Decimal("0.0006"),
                         liquidity_usd=Decimal("600000"), executable_price_usd=Decimal("0.0006018"),
                         fill="v2_formula", pool_id="0x" + "6a" * 20, found_by="factory_getPair",
                         landing=dict(LANDED))  # fd_: a measured landing drift, so the entry can count
    monkeypatch.setattr(engine, "_protection_refusal", lambda decision: None)
    monkeypatch.setattr(viability, "paper_native_usd",
                        lambda conn, at_ms=None, chains=None: {Chain.ETH: ETH_USD} if native else {})
    monkeypatch.setattr(m, "paper_entry", lambda *a, **k: (entry, "ok"))
    monkeypatch.setattr(m, "gas_price_wei", lambda chain, conn=None, priority=None: (gas, "fixture") if gas else (None, "gas_price_unreadable"))
    decision = make_decision(chain=Chain.ETH, token=ETH_TOKEN, size=TICKET, ts=now_ms())
    return decision, engine.handoff(decision, None, tmp_db)


def _events(conn, needle: str) -> list[dict]:
    return [jload(r["payload"], {}) for r in fetch_all(conn, "SELECT payload FROM events") if needle in r["payload"]]


def test_an_eth_pool_entry_fills_at_our_size_and_is_tagged_honest(tmp_db, monkeypatch):
    decision, order = _eth_handoff(tmp_db, monkeypatch)
    assert order is not None and order.state.value == "filled"
    [event] = _events(tmp_db, "paper_entry_priced")
    assert event["source"] == "pool:uniswap-v2:v2_formula" and event["honest"] is True
    assert event["router_fee_assumed"] is True and event["gas_bps"] == 0 and event["entry_cost_bps"] == 100
    [pid] = [r["position_id"] for r in fetch_all(tmp_db, "SELECT position_id FROM positions")]
    tag = pc.read_basis(tmp_db, pid)
    assert tag["chain"] == "eth" and tag["entry"]["honest"] is True and tag["entry"]["gas_bps"] == 0


def test_no_native_sample_refuses_the_paper_entry_instead_of_the_4000_dollar_placeholder(tmp_db, monkeypatch):
    decision, order = _eth_handoff(tmp_db, monkeypatch, native=False)
    assert order is None and fetch_all(tmp_db, "SELECT * FROM orders") == []
    [refused] = _events(tmp_db, engine.PAPER_NATIVE_PRICE_MISSING)
    assert refused["reason"] == engine.PAPER_NATIVE_PRICE_MISSING


def test_an_unreadable_gas_price_refuses_and_a_spike_is_charged(tmp_db, monkeypatch):
    decision, order = _eth_handoff(tmp_db, monkeypatch, gas=None)
    assert order is None and _events(tmp_db, engine.PAPER_GAS_UNKNOWN)


def test_a_gas_spike_is_charged_on_the_entry(tmp_db, monkeypatch):
    from kaiba.execution.paper import fill_basis

    decision, order = _eth_handoff(tmp_db, monkeypatch, gas=2 * 10**9)
    assert order is not None and order.state.value == "filled"
    [event] = _events(tmp_db, "paper_entry_priced")
    assert event["gas_bps"] == 188 and event["entry_cost_bps"] == 288
    # and it is what the broker was handed: the mark and our size's fill both pay router + gas
    basis = fill_basis(tmp_db, order.order_id)
    assert Decimal(basis["quote_price_usd"]) == Decimal("0.0006") * (1 + Decimal(288) / 10_000)
    assert Decimal(basis["executable_price_usd"]) == Decimal("0.0006018") * (1 + Decimal(288) / 10_000)


def test_an_unknown_eth_tax_refuses_through_handoff(tmp_db, monkeypatch):
    decision, order = _eth_handoff(tmp_db, monkeypatch, tax=None)
    assert order is None and _events(tmp_db, pc.PAPER_TAX_UNKNOWN)


# ----------------------------------------------------------------------------- sizing: the pool is the venue


def _venue_setup(tmp_db, monkeypatch, ref: m.PoolRef, table: dict):
    hosts(monkeypatch)
    rpc = FakeRpc(table)
    monkeypatch.setattr(m, "transport_for", lambda chain, conn=None, priority=None: rpc)
    monkeypatch.setattr(m, "gas_price_wei", lambda chain, conn=None, priority=None: (62_119_446, "fixture"))
    monkeypatch.setattr(viability, "_native_usd", lambda chain, conn, at_ms=None: ETH_USD)
    m.remember_ref(ref, tmp_db)
    m._DECIMALS[(ref.chain.value, ref.token)] = 18
    write_dossier(tmp_db, token=ref.token, chain=ref.chain, price="0.0006", liquidity="435628")
    from kaiba.core.schemas import EvidenceBasis, Measure, Receipt

    mk = Measure(value=Decimal(0), basis=EvidenceBasis.PROVIDER_REPORTED, receipt=Receipt(provider="fixture", endpoint="dyor"),
                 freshness_budget_s=86_400)
    from kaiba.execution.engine import load_dossier

    dossier = load_dossier(ref.chain, ref.token, tmp_db).model_copy(update={"buy_tax_bps": mk, "sell_tax_bps": mk})
    tmp_db.execute("UPDATE token_dossiers SET dossier_json=? WHERE chain=? AND address=?",
                   (dossier.model_dump_json(), ref.chain.value, ref.token))
    return rpc


def _v4_slot(sqrt: int, lp_fee: int, protocol: int = 0) -> str:
    return word((lp_fee << 208) | (protocol << 184) | sqrt)


def test_the_venue_is_the_tokens_own_pool_and_its_own_fee(tmp_db, monkeypatch):
    chain, r, cands, v4marks, dec, marks, why = fixture_marks("eth_pepe")
    v2 = next(mk for mk in marks if mk.ref.kind == "v2")
    ref = m.PoolRef(Chain.ETH, ETH_TOKEN, v2.ref.pool, "v2", "uniswap-v2", True, m.WETH_ETH, 30)
    _venue_setup(tmp_db, monkeypatch, ref, {(v2.ref.pool, m.SEL_GET_RESERVES): FX["eth_pepe"]["state"][2]})
    venue = viability.read_venue(Chain.ETH, ETH_TOKEN, tmp_db)
    assert venue.priced and venue.fee_bps_per_leg == 30 and venue.tax_bps_per_leg == 0, venue.note
    assert isinstance(venue.depth, viability.PoolDepth) and venue.depth.source.startswith("evm-pool:uniswap-v2")
    assert venue.depth.basis is viability.EvidenceBasis.VERIFIED_ONCHAIN
    # never the dossier's DexScreener liquidity ($435,628 in this fixture): the pool's own reserve
    assert venue.depth.liquidity_usd != Decimal("435628")
    assert viability.resolve_depth(Chain.ETH, ETH_TOKEN, tmp_db) == venue.depth


def test_an_80_percent_fee_pool_is_refused_by_the_round_trip_ceiling(tmp_db, monkeypatch):
    """Ethereum Clanker's 80% a side, as a hookless v4 key fee: read, charged, refused."""
    ref = m.PoolRef(Chain.ETH, ETH_TOKEN, "0x" + "8" * 64, "v4", "uniswap-v4", False, None, 800_000, 200, None)
    calls = m.mark_calls(ref)
    sqrt = 2**96 // 40  # 1 ETH buys 1,600 tokens
    rpc = _venue_setup(tmp_db, monkeypatch, ref, {tuple(calls[0]): _v4_slot(sqrt, 800_000),
                                                   tuple(calls[1]): word(10**24)})
    venue = viability.read_venue(Chain.ETH, ETH_TOKEN, tmp_db)
    assert venue.fee_bps_per_leg == 8000, venue.note
    verdict = viability.check_size(Chain.ETH, TICKET, tmp_db, token=ETH_TOKEN)
    assert not verdict.ok and verdict.reason.startswith("no_viable_size"), verdict.reason
    assert verdict.estimate.pct is None or verdict.estimate.pct > 7, verdict.estimate
    assert rpc.sends, "the fee was READ off the pool, not assumed"


def test_a_pool_whose_fee_cannot_be_read_is_refused(tmp_db, monkeypatch):
    hooked = m.PoolRef(Chain.BASE, BASE_TOKEN, "0x" + "9" * 64, "v4", "uniswap-v4", False, None, 10_000, 200,
                       "0x" + "c" * 40)
    calls = m.mark_calls(hooked)
    _venue_setup(tmp_db, monkeypatch, hooked, {tuple(calls[0]): _v4_slot(2**96 // 40, 10_000),
                                                tuple(calls[1]): word(10**24)})
    venue = viability.read_venue(Chain.BASE, BASE_TOKEN, tmp_db)
    assert not venue.priced and "venue_fee_unknown" in venue.note
    verdict = viability.check_size(Chain.BASE, TICKET, tmp_db, token=BASE_TOKEN)
    assert not verdict.ok


# ----------------------------------------------------------------------------- the exit, from the pool at our quantity


def test_an_eth_paper_exit_fills_from_the_pool_at_the_positions_quantity(tmp_db, monkeypatch):
    chain, r, cands, v4marks, dec, marks, why = fixture_marks("eth_pepe")
    v2 = next(mk for mk in marks if mk.ref.kind == "v2")
    hosts(monkeypatch)
    monkeypatch.setattr(m, "fill_native_usd", lambda chain, conn, at_ms=None: (ETH_USD, "fixture"))
    ref = m.PoolRef(Chain.ETH, ETH_TOKEN, v2.ref.pool, "v2", "uniswap-v2", True, m.WETH_ETH, 30, found_by="factory_getPair")
    m.remember_ref(ref, tmp_db)
    m._DECIMALS[(Chain.ETH.value, ETH_TOKEN)] = 18
    rpc = FakeRpc({(v2.ref.pool, m.SEL_GET_RESERVES): FX["eth_pepe"]["state"][2]})
    make_position(tmp_db, position_id="pos_eth_x", token=ETH_TOKEN, chain=Chain.ETH, qty=10**24)
    from kaiba.execution.paper import load_position

    position = load_position(tmp_db, "pos_eth_x")
    fill, why = m.exit_fill(position, 10**24, tmp_db, qty_decimals=18, transport=rpc)
    assert fill is not None and fill.basis == "v2_formula" and fill.side == "sell", why
    r0, r1 = v2.reserves
    assert fill.amount_out == 10**24 * 9970 * r1 // (r0 * 10_000 + 10**24 * 9970)
    assert len(rpc.sends) == 1


# ----------------------------------------------------------------------------- feeds and the report


def test_the_token_feeds_poll_eth_and_base_from_their_own_list_while_both_stay_disabled(tmp_path, monkeypatch):
    from kaiba.ingest import gmgn_feeds as gf

    path = tmp_path / "schedule.yaml"
    path.write_text("gmgn_feeds:\n  wallet_chains: [sol]\n  token_chains: [sol, bsc, robinhood, eth, base, nope]\n")
    chains = gf.feed_chains(path)
    assert chains == [Chain.SOL, Chain.BSC, Chain.ROBINHOOD, Chain.ETH, Chain.BASE]
    assert gf.wallet_chains(path) == [Chain.SOL], "wallet feeds are a separate list and are not widened"
    from kaiba.core.config import get_risk

    assert not get_risk().chain_budget(Chain.ETH).enabled and not get_risk().chain_budget(Chain.BASE).enabled
    assert {Chain.ETH, Chain.BASE} <= gf.TRENCHES_CHAINS
    for body in ("gmgn_feeds:\n  wallet_chains: [sol]\n", "gmgn_feeds:\n  token_chains: []\n"):
        path.write_text(body)
        assert gf.token_chains(path) is None
        assert Chain.ETH not in gf.feed_chains(path), "absent or empty -> the enabled set, as before"


def test_the_daily_card_shows_every_owner_chain_and_counts_eth_only_when_honest(tmp_db):
    from kaiba.learning import paper_report as PR

    now = now_ms()
    for pid, chain, honest in (("e1", "eth", True), ("e2", "eth", False), ("b1", "base", False)):
        tmp_db.execute(
            "INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms, closed_ms, qty, qty_total, "
            "cost_native, proceeds_native, realized_native, exit_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, chain, "0x" + pid.encode().hex().ljust(40, "0"), "sm-trenches", "shadow", now - 3_600_000,
             now - 60_000, "0", "100", "1000", "1100", "100", "tp"))
        if honest:
            pc.record_entry_basis(tmp_db, pid, Chain(chain), source="pool:uniswap-v3:quoter_v2", fill="quoter_v2",
                                  buy_tax_bps=0, sell_tax_bps=0, tax_in_price=False, landing=LANDED)
            pc.record_exit_basis(tmp_db, pid, Chain(chain), mark_source="evm-pool:uniswap-v3", fill="quoter_v2",
                                 tax_bps=0, filled=True)
    board = PR.scoreboard(tmp_db, now, trials={})
    acts = {a.chain: a for a in PR.with_honesty(tmp_db, now, PR.activity(tmp_db, now), board.rows)}
    assert list(acts) == ["sol", "bsc", "robinhood", "eth", "base"]
    assert (acts["eth"].closed_window, acts["eth"].counted_window) == (2, 1) and acts["eth"].honest_share == 0.5
    assert abs(acts["eth"].counted_mean - 0.1) < 1e-9
    assert (acts["base"].closed_window, acts["base"].counted_window, acts["base"].counted_mean) == (1, 0, None)
    text = PR.render(now_ms=now, acts=list(acts.values()), papers=board.papers, replay={}, replay_ms=None,
                     ready=PR.Readiness(), no_depth=0, leftovers={})
    for chain in ("sol", "bsc", "robinhood", "eth", "base"):
        assert f"  {chain}: " in text
    assert "eth: 2 opened, 2 closed | 12d: 2 closed, 1 counted (50% honest), mean +10.0%" in text
    assert PR.NATIVE_SYMBOL["base"] == "ETH" and PR.NATIVE_DECIMALS["base"] == 18


def test_when_the_pool_refuses_the_depth_is_nothing_never_the_dossier_or_dexscreener(tmp_db, monkeypatch):
    hosts(monkeypatch, ETH_POOL_RPC_URL=None)
    monkeypatch.setattr(viability, "_native_usd", lambda chain, conn, at_ms=None: ETH_USD)

    def no_pair(*a, **k):
        raise AssertionError("DexScreener was asked for an eth depth")

    monkeypatch.setattr(viability, "pair_depth", no_pair)
    write_dossier(tmp_db, token=ETH_TOKEN, chain=Chain.ETH, price="0.0006", liquidity="435628")
    depth = viability.resolve_depth(Chain.ETH, ETH_TOKEN, tmp_db)
    assert isinstance(depth, viability.NoDepth) and depth.source == "pool:eth_pool_rpc_unset", depth


@pytest.mark.parametrize("why,kind", [
    ("pool_discovery_failed (cached)", m.EXIT_RETRY), ("pool_state_unreadable", m.EXIT_RETRY),
    ("eth_pool_rpc_circuit_open", m.EXIT_RETRY), ("base_pool_rpc_circuit_open:12s", m.EXIT_RETRY),
    ("no_usable_native_pool:v2:0xaa:reserves_empty,v3:0xbb:no_in_range_liquidity", m.EXIT_NO_FILL),
    ("no_usable_native_pool:v2:0xaa:v2_factory_unknown", m.EXIT_AT_MARK),
    ("eth_pool_rpc_unset", m.EXIT_AT_MARK), ("venue_unverified:aerodrome", m.EXIT_AT_MARK),
    ("no_native_pool (cached)", m.EXIT_AT_MARK),
])
def test_eth_and_base_exit_refusals_are_classified_like_bscs(why, kind):
    assert m.exit_refusal_kind(why) == kind


def _eth_position(tmp_db, monkeypatch, *, native: bool):
    from kaiba.execution.paper import load_position

    hosts(monkeypatch)
    monkeypatch.setattr(viability, "paper_native_usd",
                        lambda conn, at_ms=None, chains=None: {Chain.ETH: ETH_USD} if native else {})
    make_position(tmp_db, position_id="pos_eth_m", token=ETH_TOKEN, chain=Chain.ETH, qty=1000 * 10**18, entry="0.6")
    pc.record_entry_basis(tmp_db, "pos_eth_m", Chain.ETH, source="pool:uniswap-v2:v2_formula", fill="v2_formula",
                          buy_tax_bps=0, sell_tax_bps=0, tax_in_price=False, landing=LANDED)
    tmp_db.commit()
    return load_position(tmp_db, "pos_eth_m")


def _mark(price: str = "0.6") -> wd.PriceQuote:
    return wd.PriceQuote(price_usd=Decimal(price), liquidity_usd=Decimal("1200000"),
                         basis=wd.EvidenceBasis.VERIFIED_ONCHAIN, observed_ms=now_ms(), source="evm-pool:uniswap-v2",
                         chain=Chain.ETH, token=ETH_TOKEN, pool_id="0x" + "6a" * 20)


def test_an_eth_exit_the_pool_cannot_price_fills_at_the_mark_and_pays_the_worst_tier(tmp_db, monkeypatch, risk_file):  # noqa: F811
    from kaiba.execution.paper import fill_basis

    position = _eth_position(tmp_db, monkeypatch, native=True)
    monkeypatch.setattr(m, "exit_fill", lambda *a, **k: (None, "eth_pool_rpc_unset"))
    out = wd.DefaultExitSubmitter(tmp_db, RecordingSubmitter())._paper(position, Decimal(100), _mark(), "stop_loss")
    assert out.ok, out.detail
    sell_id = fetch_all(tmp_db, "SELECT order_id FROM orders WHERE side='sell'")[0]["order_id"]
    # router 100 + the worst Uniswap tier net of the broker's own 60 (= 40), the broker's 60 on top
    assert Decimal(fill_basis(tmp_db, sell_id)["quote_price_usd"]) == Decimal("0.6") * (1 - Decimal(140) / 10_000)
    tag = pc.read_basis(tmp_db, "pos_eth_m")
    assert tag["exits"][-1]["fill"] == "mark" and tag["exits"][-1]["venue_bps"] == 40
    assert pc.exit_degraded(tag) and pc.counts_on_scoreboard(Chain.ETH, tag)


def test_an_eth_exit_with_no_native_sample_still_closes_and_leaves_the_scoreboard(tmp_db, monkeypatch, risk_file):  # noqa: F811
    position = _eth_position(tmp_db, monkeypatch, native=False)
    monkeypatch.setattr(m, "exit_fill", lambda *a, **k: (None, "eth_pool_rpc_unset"))
    out = wd.DefaultExitSubmitter(tmp_db, RecordingSubmitter())._paper(position, Decimal(100), _mark(), "stop_loss")
    assert out.ok, "a position must still close"
    tag = pc.read_basis(tmp_db, "pos_eth_m")
    assert tag[pc.NATIVE_PLACEHOLDER] is True and tag["exits"][-1]["native_placeholder"] is True
    assert not pc.counts_on_scoreboard(Chain.ETH, tag)


def test_the_pool_is_chosen_for_what_our_ticket_gets_not_for_its_depth_figure(tmp_db):
    """A tight v3 range shows a huge in-range virtual reserve with few real tokens behind it; the pool a
    single-pool route would take is the one that gives the most tokens for 0.04 ETH."""
    chain, token = Chain.ETH, ETH_TOKEN
    d = m.UNISWAP[chain]
    deep, real = "0x" + "d1" * 20, "0x" + "a5" * 20
    discovery = m.uniswap_discovery_calls(chain, token)
    rpc = FakeRpc(default="0x" + "0" * 64)  # no v2 pair, empty v4 slots, unless listed below
    rpc.table[(token, m.SEL_DECIMALS)] = word(18)
    rpc.table[discovery[1 + m.UNISWAP_V3_FEE_TIERS.index(500)]] = word(int(real, 16))
    rpc.table[discovery[1 + m.UNISWAP_V3_FEE_TIERS.index(10000)]] = word(int(deep, 16))
    sqrt = 2**96  # 1 token = 1 ETH in both pools; WETH (0xc02a...) sorts below the token, so it is token0
    for pool, fee, liquidity, out in ((real, 500, 10**20, 39 * 10**15), (deep, 10000, 10**23, 2 * 10**15)):
        rpc.table[(pool, m.SEL_TOKEN0)] = word(int(m.WETH_ETH, 16))
        rpc.table[(pool, m.SEL_FEE)] = word(fee)
        rpc.table[(pool, m.SEL_SLOT0)] = word(sqrt) + "0" * 64 * 6
        rpc.table[(pool, m.SEL_LIQUIDITY)] = word(liquidity)
        rpc.table[m.quote_v3_call(d.v3_quoter, m.WETH_ETH, token, m.RESOLVE_QUOTE_WEI, fee)] = word(out) + "0" * 64 * 3
    mark, why = m.resolve_uniswap(chain, token, rpc, tmp_db)
    assert why == "ok" and mark.ref.pool == real, (why, mark and mark.ref)
    assert m.deepest([mark]) is mark and len(rpc.sends) == 2, "discovery, then state + every quote in one send"
    assert m.cached_ref(chain, token, tmp_db) == mark.ref
