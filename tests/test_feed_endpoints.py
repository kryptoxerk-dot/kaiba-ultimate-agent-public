"""kaiba/ingest/feed_endpoints.py and the runner's endpoint switches (2026-10-21).

Four feeds sat dark for twelve days because each gated on ``"/v2/" in url`` against a URL it
shared with protection. The resolver gives each feed its own endpoint, never on a protection
host, and changes nothing until a feed is listed in ``KAIBA_FEED_PUBLIC_FALLBACK``.

Every guard here has a test that fails with the guard removed (the work log records each
mutation run). Hosts are public endpoints; every key is a fake.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from types import SimpleNamespace
from typing import Any

import pytest

from kaiba.core.db import fetch_all, fetch_one
from kaiba.core.limiter import Priority, reserve
from kaiba.core.schemas import Chain
from kaiba.ingest import feed_endpoints as fe
from kaiba.ingest import pumpportal, runner

# The box's URL shapes on 2026-10-21 (hosts read from ~/.config/kaiba/.env; no key in any).
BOX_SOL = "https://solana-rpc.publicnode.com"
BOX_BSC = "https://bsc-rpc.publicnode.com"
BOX_RH = "https://rpc.mainnet.chain.robinhood.com"
BOX_ENV = {"BSC_SNIPE_RPC_URL": BOX_BSC}

#: What each feed's ``run`` returned before 2026-10-21 with that configuration. Spelled out
#: here, NOT read from ``fe.TODAY_IDLE``, so a change to the constant cannot hide a change of
#: behaviour.
YESTERDAYS_IDLE = {
    "leader_exit": {"idle": "no Alchemy Solana endpoint (SOLANA_RPC_URL with /v2/)"},
    "sol_wallets": {"idle": "no websocket endpoint"},
    "flap": {"idle": "no websocket endpoint"},
    "rh_wallets": {"idle": "no websocket endpoint"},
}

ALCHEMY_SOL = "https://solana-mainnet.alchemy.example/v2/KEYSECRET123"


def settings(sol: str = BOX_SOL, bsc: str = BOX_BSC, rh: str = BOX_RH) -> Any:
    urls = {Chain.SOL: sol, Chain.BSC: bsc, Chain.ROBINHOOD: rh}
    return SimpleNamespace(bsc_rpc_url=bsc, rpc_for=lambda chain: urls.get(chain, ""))


def no_probe(chain: Chain, url: str) -> str:
    raise AssertionError("no capacity probe may run here")


class Probe:
    def __init__(self, *verdicts: str) -> None:
        self.verdicts, self.calls = list(verdicts), 0

    def __call__(self, chain: Chain, url: str) -> str:
        self.calls += 1
        return self.verdicts.pop(0) if len(self.verdicts) > 1 else self.verdicts[0]


def _clear_feed_env(monkeypatch) -> None:
    for name in (fe.ENV_PUBLIC_FALLBACK, fe.ENV_ALLOW_SHARED_ALCHEMY, fe.ENV_SHARED_WS_OK, fe.ENV_PREFER_PUBLIC,
                 fe.ENV_SOL_FEED_ALCHEMY, fe.ENV_RH_FEED_ALCHEMY, fe.ENV_SOL_FEED_WS, fe.ENV_SOL_FEED_HTTP,
                 fe.ENV_BSC_SNIPE_WS, fe.ENV_BSC_SNIPE_RPC, "SOLANA_RPC_URL", "BSC_RPC_URL", "ROBINHOOD_RPC_URL"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def box_env(monkeypatch):
    """The process environment set to the box's shapes, every feed switch off."""
    from kaiba.core.config import get_settings

    _clear_feed_env(monkeypatch)
    monkeypatch.setenv("SOLANA_RPC_URL", BOX_SOL)
    monkeypatch.setenv("BSC_RPC_URL", BOX_BSC)
    monkeypatch.setenv("ROBINHOOD_RPC_URL", BOX_RH)
    monkeypatch.setenv("BSC_SNIPE_RPC_URL", BOX_BSC)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ------------------------------------------------------------------ off by default


@pytest.mark.parametrize("feed", sorted(YESTERDAYS_IDLE))
def test_off_by_default(feed):
    """An empty fallback list and the box's URL shapes give exactly yesterday's idle result.
    Mutation: a non-empty default for KAIBA_FEED_PUBLIC_FALLBACK."""
    got = fe.resolve(feed, env=BOX_ENV, settings=settings(), probe=no_probe)
    assert isinstance(got, fe.Refusal)
    assert got.as_idle() == YESTERDAYS_IDLE[feed]


def test_deploying_with_every_switch_off_changes_nothing_on_the_box(tmp_db, box_env, monkeypatch):
    """Each feed's run(), through the resolver and the real environment, returns what it
    returned before the resolver existed. No probe, no socket, no row."""
    from kaiba.ingest import flap, wallet_stream_sol
    from kaiba.learning import leader_exit

    monkeypatch.setattr(fe, "probe_capacity", no_probe)

    def no_dial(url: str) -> Any:
        raise AssertionError("no socket may be opened")

    def bounded(coro: Any) -> Any:
        # an idle feed returns at once; one that found an endpoint would run until stopped
        try:
            return asyncio.run(asyncio.wait_for(coro, timeout=5))
        except TimeoutError:
            return "kept running: it found an endpoint"

    assert bounded(leader_exit.run(asyncio.Event(), connect=no_dial)) == YESTERDAYS_IDLE["leader_exit"]
    assert bounded(wallet_stream_sol.run(asyncio.Event(), reader=tmp_db, writer=tmp_db,
                                         connect=no_dial)) == YESTERDAYS_IDLE["sol_wallets"]
    assert bounded(flap.run(asyncio.Event(), enabled=lambda: True, writer=tmp_db,
                            connect=no_dial)) == YESTERDAYS_IDLE["flap"]
    assert fetch_one(tmp_db, "SELECT COUNT(*) AS n FROM provider_calls")["n"] == 0
    assert fetch_one(tmp_db, "SELECT COUNT(*) AS n FROM tokens")["n"] == 0


def test_a_listed_feed_runs_on_the_public_host_and_never_on_protections():
    got = fe.resolve("leader_exit", env={fe.ENV_PUBLIC_FALLBACK: "leader_exit, flap"}, settings=settings(),
                     probe=no_probe)
    assert isinstance(got, fe.FeedEndpoint) and got.kind == fe.KIND_PUBLIC
    assert (got.ws_url, got.http_url) == ("wss://api.mainnet-beta.solana.com", "https://api.mainnet-beta.solana.com")
    assert got.http_host not in fe.protection_hosts(Chain.SOL, settings())
    # a feed that is not listed stays as it was
    assert fe.resolve("sol_wallets", env={fe.ENV_PUBLIC_FALLBACK: "leader_exit"}, settings=settings(),
                      probe=no_probe).as_idle() == YESTERDAYS_IDLE["sol_wallets"]


# ------------------------------------------------------------------ order and the probe


def test_alchemy_preferred_when_probe_ok():
    """A feeds-only key that passes its probe wins over the public fallback.
    Mutation: reversed order (public first)."""
    env = {fe.ENV_PUBLIC_FALLBACK: "sol_wallets", fe.ENV_SOL_FEED_ALCHEMY: ALCHEMY_SOL}
    probe = Probe(fe.VERDICT_OK)
    got = fe.resolve("sol_wallets", env=env, settings=settings(), probe=probe)
    assert isinstance(got, fe.FeedEndpoint) and got.kind == fe.KIND_ALCHEMY and probe.calls == 1
    assert got.http_url == ALCHEMY_SOL and got.ws_url == "wss://solana-mainnet.alchemy.example/v2/KEYSECRET123"
    # KAIBA_FEED_PREFER_PUBLIC=sol turns the order around (to measure both, DESIGN.md)
    flipped = fe.resolve("sol_wallets", env={**env, fe.ENV_PREFER_PUBLIC: "sol"}, settings=settings(), probe=probe)
    assert isinstance(flipped, fe.FeedEndpoint) and flipped.kind == fe.KIND_PUBLIC


class FakeHttp:
    """Stands in for ``httpx.post`` inside the probe."""

    def __init__(self, status: int, text: str) -> None:
        self.status, self.text, self.calls = status, text, 0

    def __call__(self, url: str, **kw: Any) -> Any:
        self.calls += 1
        return SimpleNamespace(status_code=self.status, text=self.text)


def test_monthly_cap_429_falls_back_and_caches(tmp_db, monkeypatch):
    """A 429 saying "capacity" skips the key and is cached (host only): a second resolve makes
    0 probe calls. Mutation: drop the 429 classification."""
    import httpx

    http = FakeHttp(429, '{"jsonrpc":"2.0","error":{"code":429,"message":"Monthly capacity limit exceeded."}}')
    monkeypatch.setattr(httpx, "post", http)
    env = {fe.ENV_PUBLIC_FALLBACK: "leader_exit", fe.ENV_SOL_FEED_ALCHEMY: ALCHEMY_SOL}
    first = fe.resolve("leader_exit", env=env, settings=settings(), conn=tmp_db)
    second = fe.resolve("leader_exit", env=env, settings=settings(), conn=tmp_db)
    assert isinstance(first, fe.FeedEndpoint) and first.kind == fe.KIND_PUBLIC
    assert isinstance(second, fe.FeedEndpoint) and second.kind == fe.KIND_PUBLIC
    assert http.calls == 1, "the second resolve probed again"
    cached = json.loads(fetch_one(tmp_db, "SELECT value FROM kv WHERE key='feed_endpoint:alchemy:sol'")["value"])
    assert cached["verdict"] == "capped" and cached["host"] == "solana-mainnet.alchemy.example"
    assert "KEYSECRET123" not in json.dumps(cached) and "/v2/" not in json.dumps(cached)
    calls = fetch_all(tmp_db, "SELECT provider, status FROM provider_calls")
    assert calls == [{"provider": fe.PROBE_BUCKET, "status": "rate_limited"}]  # its own bucket, never `rpc`
    # a plain burst 429 is NOT a cap (the key is still used); Helius words its cap "usage"
    assert not fe.capped_body('{"error":{"code":429,"message":"Too many requests for a specific RPC call"}}')
    assert fe.capped_body('{"error":{"code":-32429,"message":"max usage reached"}}')


def test_a_cached_verdict_expires_and_never_applies_to_another_host(tmp_db):
    fe.save_verdict(Chain.SOL, "a.example", fe.VERDICT_CAPPED, conn=tmp_db, at_ms=1_000)
    assert fe.cached_verdict(Chain.SOL, "a.example", conn=tmp_db, at_ms=1_000 + fe.CACHE_TTL_MS - 1) == "capped"
    assert fe.cached_verdict(Chain.SOL, "a.example", conn=tmp_db, at_ms=1_000 + fe.CACHE_TTL_MS) is None
    assert fe.cached_verdict(Chain.SOL, "b.example", conn=tmp_db, at_ms=2_000) is None


# ------------------------------------------------------------------ the host invariant


def test_refuses_protection_host(monkeypatch):
    """SOLANA_RPC_URL unset: config.py's default IS mainnet-beta, the sol fallback host, so the
    feed is refused; host case and port are normalised. Mutation: delete the check."""
    from kaiba.core.config import Settings

    monkeypatch.delenv("SOLANA_RPC_URL", raising=False)
    default = Settings(_env_file=None)
    assert default.solana_rpc_url == "https://api.mainnet-beta.solana.com"  # the premise (config.py)
    env = {fe.ENV_PUBLIC_FALLBACK: "leader_exit", fe.ENV_SOL_FEED_WS: "wss://ws.sol-feed.example"}
    got = fe.resolve("leader_exit", env=env, settings=default, probe=no_probe)
    assert isinstance(got, fe.Refusal) and "http host == protection host api.mainnet-beta.solana.com" in got.reason
    # the same host spelled with capitals, the default port and a trailing slash
    odd = settings(sol="https://API.Mainnet-Beta.Solana.com:443/")
    got = fe.resolve("leader_exit", env=env, settings=odd, probe=no_probe)
    assert isinstance(got, fe.Refusal) and "api.mainnet-beta.solana.com" in got.reason
    # positive control: protection on another host, the same feed config resolves
    assert isinstance(fe.resolve("leader_exit", env=env, settings=settings(), probe=no_probe), fe.FeedEndpoint)


def _rpc_bucket_url(monkeypatch, chain: Chain, live: Any) -> str | None:
    """The URL protection's ``rpc``-bucket reader actually uses with ``live`` settings: the
    production function, not a copy of its rule, so the guard is checked against the caller."""
    from kaiba.core import config
    from kaiba.execution import evm_price

    monkeypatch.setattr(config, "get_settings", lambda: live)
    return evm_price._rpc_url(chain)


def test_bsc_rpc_url_unset_guards_protections_public_fallback(monkeypatch):
    """Review 2026-10-21: config.py defaults bsc_rpc_url to "", and protection's BSC price reads
    (evm_price.json_rpc_batch at EXIT) then fall back to dyor_verify.PUBLIC_RPC[BSC]. The guard
    held an empty set, so flap's HTTP was allowed onto that exact host and, with shared_ws
    False, its protection-pressure pause was never armed. Mutation: drop the PUBLIC_RPC
    fallback in protection_hosts and this test fails."""
    from kaiba.core.config import Settings

    monkeypatch.delenv("BSC_RPC_URL", raising=False)
    default = Settings(_env_file=None)
    assert default.bsc_rpc_url == ""  # the premise (config.py)
    used = fe.host_of(_rpc_bucket_url(monkeypatch, Chain.BSC, default))
    assert used == "bsc-dataseed.binance.org"  # where protection reads go with BSC_RPC_URL unset
    assert used in fe.protection_hosts(Chain.BSC, default)
    env = {fe.ENV_PUBLIC_FALLBACK: "flap", fe.ENV_BSC_SNIPE_WS: "wss://bsc-rpc.publicnode.com",
           fe.ENV_BSC_SNIPE_RPC: "https://bsc-dataseed.binance.org"}
    got = fe.resolve("flap", env=env, settings=default, probe=no_probe)
    assert isinstance(got, fe.Refusal) and "http host == protection host bsc-dataseed.binance.org" in got.reason
    # socket-only on that host: refused too (a ws on a protection host needs the shared-ws switch)
    sock = {fe.ENV_PUBLIC_FALLBACK: "flap", fe.ENV_BSC_SNIPE_WS: "wss://bsc-dataseed.binance.org"}
    got = fe.resolve("flap", env=sock, settings=default, probe=no_probe)
    assert isinstance(got, fe.Refusal) and "ws host == protection host bsc-dataseed.binance.org" in got.reason
    # positive control: protection configured on another host, the same flap config resolves
    ok = fe.resolve("flap", env={**env, fe.ENV_BSC_SNIPE_WS: "wss://ws.bsc-feed.example"},
                    settings=settings(bsc=BOX_BSC), probe=no_probe)
    assert isinstance(ok, fe.FeedEndpoint) and ok.http_host == "bsc-dataseed.binance.org"


def test_solana_rpc_url_blank_guards_protections_public_fallback(monkeypatch):
    """``SOLANA_RPC_URL=`` (a blank line in .env) overrides config.py's mainnet-beta default with
    "", and protection's ``rpc``-bucket readers (dyor_verify._rpc_url, evm_price._rpc_url) then
    fall back to PUBLIC_RPC[SOL], which is mainnet-beta: the sol feeds' public host. The guard
    held an empty set. Mutation: drop the PUBLIC_RPC fallback and this test fails."""
    from kaiba.core.config import Settings
    from kaiba.intelligence import dyor_verify

    monkeypatch.setenv("SOLANA_RPC_URL", "")
    blank = Settings(_env_file=None)
    assert blank.solana_rpc_url == ""  # the premise: blank is not unset
    used = fe.host_of(_rpc_bucket_url(monkeypatch, Chain.SOL, blank))
    assert used == fe.host_of(dyor_verify._rpc_url(Chain.SOL)) == "api.mainnet-beta.solana.com"
    assert used in fe.protection_hosts(Chain.SOL, blank)
    env = {fe.ENV_PUBLIC_FALLBACK: "leader_exit", fe.ENV_SOL_FEED_WS: "wss://ws.sol-feed.example"}
    got = fe.resolve("leader_exit", env=env, settings=blank, probe=no_probe)
    assert isinstance(got, fe.Refusal) and "http host == protection host api.mainnet-beta.solana.com" in got.reason
    # positive control: protection on its own host, the same feed config resolves to mainnet-beta
    ok = fe.resolve("leader_exit", env=env, settings=settings(), probe=no_probe)
    assert isinstance(ok, fe.FeedEndpoint) and ok.http_host == "api.mainnet-beta.solana.com"


@pytest.mark.parametrize("chain", [Chain.SOL, Chain.BSC, Chain.ROBINHOOD])
@pytest.mark.parametrize("configured", ["", "   ", "https://own-rpc.example/path"])
def test_the_guard_covers_the_url_every_rpc_bucket_reader_uses(monkeypatch, chain, configured):
    """Whatever the chain URL is (blank, whitespace or set), the host protection's reader will
    dial is in the guard. Mutation: drop the PUBLIC_RPC fallback and the blank cases fail."""
    live = settings(sol=configured, bsc=configured, rh=configured)
    used = fe.host_of(_rpc_bucket_url(monkeypatch, chain, live))
    if used is None:  # robinhood has no PUBLIC_RPC row; its reader is robinhood.RPC_URL
        assert chain is Chain.ROBINHOOD
        from kaiba.ingest import robinhood

        used = fe.host_of(robinhood.RPC_URL)
    assert used in fe.protection_hosts(chain, live)


def test_a_sol_feed_socket_may_never_share_protections_host():
    env = {fe.ENV_PUBLIC_FALLBACK: "sol_wallets", fe.ENV_SOL_FEED_HTTP: "https://http.sol-feed.example",
           fe.ENV_SHARED_WS_OK: "sol,bsc"}
    got = fe.resolve("sol_wallets", env=env, settings=settings(sol="https://api.mainnet-beta.solana.com"),
                     probe=no_probe)
    assert isinstance(got, fe.Refusal) and "ws host == protection host api.mainnet-beta.solana.com" in got.reason


def test_the_silent_publicnode_solana_socket_is_refused_even_when_configured():
    """MEASURED 2026-10-21: it answered 6 of 34 subscribes and silently accepted a filter it
    then never served. Mutation: delete the silent-host check."""
    env = {fe.ENV_PUBLIC_FALLBACK: "sol_wallets", fe.ENV_SOL_FEED_WS: "wss://solana-rpc.publicnode.com",
           fe.ENV_SOL_FEED_HTTP: "https://http.sol-feed.example"}
    got = fe.resolve("sol_wallets", env=env, settings=settings(sol="https://other-sol.example"), probe=no_probe)
    assert isinstance(got, fe.Refusal) and "delivers nothing" in got.reason
    assert fe.host_of(fe.SOL_PUBLIC_WS_DEFAULT) == "api.mainnet-beta.solana.com"


def test_the_shared_alchemy_key_needs_its_own_switch():
    """The chain URL protection uses (with /v2/) was the feeds' only endpoint until today. It
    now needs KAIBA_FEED_ALLOW_SHARED_ALCHEMY=1, because sharing that key blocked BSC entries."""
    shared = settings(sol=ALCHEMY_SOL)
    got = fe.resolve("leader_exit", env={}, settings=shared, probe=no_probe)
    assert isinstance(got, fe.Refusal) and fe.ENV_ALLOW_SHARED_ALCHEMY in got.reason
    ok = fe.resolve("leader_exit", env={fe.ENV_ALLOW_SHARED_ALCHEMY: "1"}, settings=shared, probe=Probe("ok"))
    assert isinstance(ok, fe.FeedEndpoint) and ok.kind == fe.KIND_ALCHEMY_SHARED and ok.http_url == ALCHEMY_SOL


def test_robinhood_has_no_public_route():
    got = fe.resolve("rh_wallets", env={fe.ENV_PUBLIC_FALLBACK: "rh_wallets"}, settings=settings(), probe=no_probe)
    assert isinstance(got, fe.Refusal) and "no public websocket for robinhood" in got.reason
    hosts = fe.protection_hosts(Chain.ROBINHOOD, settings(rh="https://rh-alchemy.example/v2/K"))
    assert hosts == {"rh-alchemy.example", "rpc.mainnet.chain.robinhood.com"}  # robinhood.RPC_URL too


# ------------------------------------------------------------------ the runner


@pytest.fixture
def quick_runner(monkeypatch):
    waits: list[float] = []

    async def fake_wait(stop: asyncio.Event, seconds: float) -> None:
        waits.append(seconds)
        await asyncio.sleep(0)

    monkeypatch.setattr(runner, "_wait", fake_wait)
    monkeypatch.setattr(pumpportal, "_rand", lambda: 0.0)
    monkeypatch.setattr(runner, "IDLE_RETURN_MS", 200)
    return waits


def _states(conn, feed: str) -> list[str]:
    out = []
    for row in fetch_all(conn, "SELECT payload FROM events WHERE kind='system' ORDER BY id"):
        body = json.loads(row["payload"])
        if body.get("component") == f"ingest.{feed}":
            out.append(str(body.get("status")))
    return out


def test_idle_recheck_6h(tmp_db, quick_runner):
    """An idle feed is tried again after IDLE_RECHECK_S; a recheck that is still idle stays
    quiet (no running blip, no restart, no second idle event); one that runs is back.
    Mutation: return on idle (no recheck)."""
    calls = {"n": 0}

    async def feed(stop: asyncio.Event) -> Any:
        calls["n"] += 1
        if calls["n"] <= 4:  # three quick returns -> idle; the first recheck: still idle
            return {"idle": "no websocket endpoint"}
        await asyncio.sleep(0.4)  # the second recheck: configured now, it runs
        stop.set()
        return None

    restarts = asyncio.run(asyncio.wait_for(runner._supervise("probe_recheck", feed, asyncio.Event(), conn=tmp_db,
                                                              idle_recheck_s=runner.IDLE_RECHECK_S), timeout=10))
    assert calls["n"] == 5
    assert quick_runner.count(runner.IDLE_RECHECK_S) == 2
    assert runner.IDLE_RECHECK_S == 6 * 3600
    states = _states(tmp_db, "probe_recheck")
    assert states.count("idle") == 1 and states[-1] == "resumed"
    assert restarts == 2  # the two quick returns before idle; rechecks add none
    assert fetch_one(tmp_db, "SELECT state FROM ingest_status WHERE feed='probe_recheck'")["state"] == "stopped"


def test_run_all_wires_the_idle_recheck(tmp_db, monkeypatch):
    """Production supervision (run_all) rechecks; a bare _supervise keeps the old contract."""
    seen: dict[str, Any] = {}

    async def fake_supervise(name, factory, stop, **kw):
        seen[name] = kw.get("idle_recheck_s")
        return 0

    monkeypatch.setattr(runner, "_supervise", fake_supervise)
    monkeypatch.setattr(runner, "_heartbeat", lambda *a, **k: asyncio.sleep(0))
    asyncio.run(runner.run_all(["x"], asyncio.Event(), registry={"x": lambda s: asyncio.sleep(0)}, conn=tmp_db))
    assert seen == {"x": runner.IDLE_RECHECK_S}


def test_fast_fallbacks_never_idle(tmp_db, quick_runner):
    """A fallback return switches endpoints; however fast, it never counts toward idle.
    Mutation: drop the exemption (three quick fallbacks would park the feed)."""
    calls = {"n": 0}

    async def feed(stop: asyncio.Event) -> Any:
        calls["n"] += 1
        if calls["n"] <= 3:
            return {"fallback": fe.FALLBACK_MONTHLY_CAP}
        stop.set()
        return None

    asyncio.run(asyncio.wait_for(runner._supervise("probe_fast_fb", feed, asyncio.Event(), conn=tmp_db,
                                                   idle_recheck_s=runner.IDLE_RECHECK_S), timeout=10))
    assert calls["n"] == 4 and "idle" not in _states(tmp_db, "probe_fast_fb")


def test_midrun_cap_restarts_not_idles(tmp_db, quick_runner, monkeypatch):
    """A monthly-cap 429 mid-run: the feed returns a fallback, the runner restarts it, it
    re-resolves to the public endpoint, and it is never marked idle (fake clock: the capped
    run lasted 2.5 s, so it is not an 'immediate' return either)."""
    clock = {"ms": int(time.time() * 1000)}
    monkeypatch.setattr(runner, "now_ms", lambda: clock["ms"])
    env = {fe.ENV_PUBLIC_FALLBACK: "leader_exit", fe.ENV_SOL_FEED_ALCHEMY: ALCHEMY_SOL}
    resolve_probe = Probe(fe.VERDICT_OK)
    kinds: list[str] = []

    async def feed(stop: asyncio.Event) -> Any:
        ep = fe.resolve("leader_exit", env=env, settings=settings(), conn=tmp_db, probe=resolve_probe,
                        at_ms=clock["ms"])
        assert isinstance(ep, fe.FeedEndpoint)
        kinds.append(ep.kind)
        if ep.kind == fe.KIND_PUBLIC:
            stop.set()
            return {"stats": "ran on public"}
        watch = fe.CapWatch(ep, probe=lambda chain, url: fe.VERDICT_CAPPED, conn=tmp_db)
        clock["ms"] += 2_500
        if not await watch.check("getTransaction: http 429"):
            stop.set()  # the cap went unnoticed: end here, the assertions below say so
            return None
        return watch.fallback()

    asyncio.run(asyncio.wait_for(runner._supervise("leader_exit", feed, asyncio.Event(), conn=tmp_db,
                                                   idle_recheck_s=runner.IDLE_RECHECK_S), timeout=10))
    assert kinds == [fe.KIND_ALCHEMY, fe.KIND_PUBLIC]
    states = _states(tmp_db, "leader_exit")
    assert "idle" not in states and "restarting" in states
    restarting = [json.loads(r["payload"]) for r in fetch_all(tmp_db, "SELECT payload FROM events WHERE kind='system'")
                  if json.loads(r["payload"]).get("status") == "restarting"]
    assert restarting[0]["error"] == "returned early (fallback: alchemy_monthly_cap)"


def test_a_capwatch_on_a_public_endpoint_never_probes():
    ep = fe.resolve("leader_exit", env={fe.ENV_PUBLIC_FALLBACK: "leader_exit"}, settings=settings(), probe=no_probe)
    assert isinstance(ep, fe.FeedEndpoint)
    watch = fe.CapWatch(ep, probe=no_probe)
    assert asyncio.run(watch.check("http 429")) is False and watch.probes == 0


# ------------------------------------------------------------------ secrets


def test_no_key_in_any_output(tmp_db, quick_runner, monkeypatch, caplog):
    """A fake key in a feed URL never reaches a log line, an event, ingest_status, kv or
    provider_calls.detail -- through the probe, the resolver's reasons and repr, a 429 on the
    feed's own bucket, a mid-run cap fallback and the runner. Mutation: log the raw URL."""
    import httpx

    from kaiba.ingest import wallet_stream_sol as ss

    caplog.set_level(logging.DEBUG)
    key = "KEYSECRET123"
    keyed = f"https://solana-mainnet.alchemy.example/v2/{key}"
    env = {fe.ENV_PUBLIC_FALLBACK: "sol_wallets", fe.ENV_SOL_FEED_ALCHEMY: keyed}

    # 1) the key resolves (its probe passed), then the node runs out: the real probe reads a
    #    429 whose body even echoes the URL back
    monkeypatch.setattr(httpx, "post", FakeHttp(429, '{"error":{"code":429,"message":"Monthly capacity limit '
                                                     f'exceeded for {keyed}"}}}}'))

    class Resp:
        status_code = 429
        headers = {"retry-after": "10"}
        text = ""

        def raise_for_status(self) -> None:
            return None

    monkeypatch.setattr(httpx, "request", lambda *a, **kw: Resp())
    ep = fe.resolve("sol_wallets", env=env, settings=settings(), conn=tmp_db, probe=lambda c, u: fe.VERDICT_OK)
    assert isinstance(ep, fe.FeedEndpoint) and ep.kind == fe.KIND_ALCHEMY
    logging.getLogger("test").info("endpoint %r %s", ep, ep.describe())

    async def feed(stop: asyncio.Event) -> Any:
        watch = fe.CapWatch(ep, conn=tmp_db)
        rpc = ss.SolRpc(ep.http_url or "", capwatch=watch, wait_for_slot_s=0.0)
        with contextlib.suppress(ss.RpcFailure):
            await rpc.call("getTransaction", ["sig"])
        stop.set()
        return watch.fallback() if watch.capped else None

    asyncio.run(asyncio.wait_for(runner._supervise("sol_wallets", feed, asyncio.Event(), conn=tmp_db,
                                                   idle_recheck_s=runner.IDLE_RECHECK_S), timeout=10))
    # 2) after the cap the same config resolves to the public host, and a refusal names hosts only
    again = fe.resolve("sol_wallets", env=env, settings=settings(), conn=tmp_db)
    assert isinstance(again, fe.FeedEndpoint) and again.kind == fe.KIND_PUBLIC
    refused = fe.resolve("sol_wallets", env={fe.ENV_SOL_FEED_ALCHEMY: keyed}, settings=settings(sol=keyed),
                         conn=tmp_db)
    assert isinstance(refused, fe.Refusal)
    asyncio.run(runner._supervise("sol_wallets_r", lambda s: _idle(refused), asyncio.Event(), conn=tmp_db))

    stored = json.dumps([fetch_all(tmp_db, "SELECT * FROM events"), fetch_all(tmp_db, "SELECT * FROM ingest_status"),
                         fetch_all(tmp_db, "SELECT * FROM kv"),
                         fetch_all(tmp_db, "SELECT provider, endpoint, status, detail FROM provider_calls"),
                         fetch_all(tmp_db, "SELECT * FROM provider_family_bans")], default=str)
    assert fetch_all(tmp_db, "SELECT DISTINCT provider FROM provider_calls ORDER BY provider") == \
        [{"provider": fe.PROBE_BUCKET}, {"provider": ss.FEED_BUCKET}]
    assert key not in stored, "a key reached the database"
    assert key not in caplog.text, "a key reached a log line"
    assert key not in repr(ep) and key not in refused.reason


async def _idle(refusal: fe.Refusal) -> dict[str, str]:
    return refusal.as_idle()


def test_an_exit_reserve_on_rpc_is_untouched_by_the_feed_probe(tmp_db, monkeypatch):
    """The probe's 429 lands on its own bucket: protection's `rpc` keeps no penalty and no ban."""
    import httpx

    monkeypatch.setattr(httpx, "post", FakeHttp(429, "Monthly capacity limit exceeded"))
    assert fe.probe_capacity(Chain.SOL, ALCHEMY_SOL) == fe.VERDICT_CAPPED
    assert fetch_all(tmp_db, "SELECT * FROM provider_family_bans WHERE provider='rpc'") == []
    assert reserve("rpc", "rpc.pump_curve", Priority.EXIT) >= 1
