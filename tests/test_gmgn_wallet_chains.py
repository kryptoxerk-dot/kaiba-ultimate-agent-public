"""The GMGN wallet feeds poll the wallet-hunting chains, not the trading chains.

MEASURED 2026-10-02 on the box: Solana TRADING was switched off on 2026-10-01 and the sol
smart-money and KOL feeds stopped with it -- ``feed_chains()`` (risk.yaml's enabled chains)
decided every feed. Those two feeds are wallet-discovery inputs, and the owner wants more
Solana A/B wallets, so a chain with trading off must still get them. Token feeds keep
following trading.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from kaiba.core.schemas import Chain
from kaiba.ingest import gmgn_feeds as gf

ROOT = Path(__file__).resolve().parents[1]


def write_schedule(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "schedule.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_a_chain_with_trading_off_still_gets_its_wallet_feeds_and_only_those(tmp_db, tmp_path, monkeypatch):
    monkeypatch.setattr(gf, "feed_chains", lambda: [Chain.ROBINHOOD])  # sol and bsc trading OFF
    monkeypatch.setenv("KAIBA_SCHEDULE_CONFIG", str(write_schedule(
        tmp_path, "gmgn_feeds:\n  wallet_chains: [sol, bsc, robinhood]\n")))
    monkeypatch.setattr(gf, "pace_s", lambda: 0.0)
    monkeypatch.setattr(gf, "_sweep_turn", 0)
    seen: list[tuple[str, str]] = []
    stop = asyncio.Event()

    def spy(chain: Chain, feed: str, conn: Any = None, **_kw: Any) -> int:
        seen.append((chain.value, feed))
        return 0

    async def one_sweep_then_stop(seconds: float) -> None:
        stop.set()

    monkeypatch.setattr(gf, "poll_once", spy)
    monkeypatch.setattr(gf, "_wait", lambda s, seconds: one_sweep_then_stop(seconds))
    asyncio.run(gf.run(interval_s=0, stop=stop, conn=tmp_db))

    for chain in ("sol", "bsc", "robinhood"):
        assert (chain, "smartmoney") in seen and (chain, "kol") in seen, chain
    for feed in ("signal", "trenches", "trending"):
        assert (("robinhood", feed) in seen) and ("sol", feed) not in seen and ("bsc", feed) not in seen, feed
    assert len(seen) == len(set(seen)) == 3 * 2 + 3


def test_sweep_order_splits_wallet_feeds_from_trading_feeds():
    pairs = gf.sweep_order([Chain.ROBINHOOD], ["smartmoney", "kol", "signal"], 1,
                           wallet_chains=[Chain.SOL, Chain.BSC, Chain.ROBINHOOD])
    assert [p for p in pairs if p[1] == "signal"] == [(Chain.ROBINHOOD, "signal")]
    assert {c for c, f in pairs if f == "smartmoney"} == {Chain.SOL, Chain.BSC, Chain.ROBINHOOD}
    # feed-major and rotated, as before: turn 1 starts the wallet feeds on the second chain
    assert pairs[0] == (Chain.BSC, "smartmoney")
    # no trading chain at all still polls the wallet feeds
    assert gf.sweep_order([], ["smartmoney", "trending"], 0, wallet_chains=[Chain.SOL]) == [(Chain.SOL, "smartmoney")]
    # without wallet_chains nothing changes
    assert gf.sweep_order([Chain.SOL], ["smartmoney", "trending"], 0) == [
        (Chain.SOL, "smartmoney"), (Chain.SOL, "trending")]


def test_wallet_chains_config(tmp_path):
    assert gf.wallet_chains(write_schedule(tmp_path, "version: v1\n")) == list(gf.DEFAULT_WALLET_CHAINS)
    assert gf.wallet_chains(write_schedule(tmp_path, "gmgn_feeds:\n  wallet_chains: []\n")) == []
    assert gf.wallet_chains(write_schedule(
        tmp_path, "gmgn_feeds:\n  wallet_chains: [SOL, nope, sol, robinhood]\n")) == [Chain.SOL, Chain.ROBINHOOD]
    assert gf.wallet_chains(tmp_path / "missing.yaml") == list(gf.DEFAULT_WALLET_CHAINS)
    assert gf.wallet_chains(write_schedule(tmp_path, "gmgn_feeds:\n  wallet_chains: sol\n")) == list(
        gf.DEFAULT_WALLET_CHAINS)


def test_the_shipped_schedule_hunts_wallets_on_sol_whatever_trading_says():
    assert gf.wallet_chains(ROOT / "config" / "schedule.yaml") == [Chain.SOL, Chain.BSC, Chain.ROBINHOOD]
    assert gf.WALLET_FEEDS == {"smartmoney", "kol"}
    assert {"signal", "trenches", "trending"}.isdisjoint(gf.WALLET_FEEDS)
