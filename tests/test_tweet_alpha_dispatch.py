"""Dispatch ordering, overload, staleness and three-chain measurement contracts."""
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from kaiba.core.schemas import Chain
from kaiba.execution import tweet_alpha_dispatch as ad
from kaiba.execution import tweet_launch as tl
from kaiba.execution import tweet_refs as tr
from kaiba.ingest.x_stream import XPost

NOW = 1791338300000
SOL = "GmJQAASzk2odWAQCh7CG1wXSMP3mNX5ofBKVqKJPpump"
EVM = "0x" + "12" * 20


def cfg(chain=Chain.SOL):
    return replace(tl.load_config(), accounts={"caller": (chain,)},
                   alpha=tr.AlphaConfig(accounts={}))


def post(text=SOL, **kwargs):
    return XPost(tweet_id=kwargs.pop("tweet_id", "alpha1"), author="caller", text=text,
                 kind="reply", media=(), created_ms=NOW-1000, received_ms=NOW,
                 feed_delay_ms=None, backend="test", **kwargs)


class Borrowed:
    def __init__(self, c):
        self.c = c
        self.closed = False

    def __getattr__(self, attr):
        return getattr(self.c, attr)

    def close(self):
        self.closed = True


@pytest.mark.parametrize("chain,token", [(Chain.SOL, SOL), (Chain.BSC, EVM), (Chain.ROBINHOOD, EVM)])
def test_real_measurement_records_three_chains_without_launch(tmp_db, chain, token):
    c = Borrowed(tmp_db)
    calls = []
    def info(address, ch, priority=None):
        calls.append((address, ch, priority.name))
        return SimpleNamespace(ok=True, data={"price": "0.005", "liquidity": "4000"})
    rows = ad.measure(post(token), cfg(chain), connect=lambda:c, info=info, now_ms=NOW)
    assert rows[0]["chain"] == chain.value and rows[0]["entry_price_usd"] == "0.005"
    assert calls == [(token, chain, "RESEARCH")]
    assert c.closed
    assert tmp_db.execute("SELECT COUNT(*) FROM tweet_launches").fetchone()[0] == 0
    assert tmp_db.execute("SELECT chain FROM tweet_refs").fetchone()[0] == chain.value


def test_queue_delay_cannot_fake_an_early_entry():
    ad.measure(post(), cfg(), now_ms=NOW+121000,
               connect=lambda:pytest.fail("stale post opened database"))


def test_explicit_hint_wins_and_quoted_contract_is_measured(tmp_db):
    c = Borrowed(tmp_db)
    config = replace(cfg(Chain.BSC), accounts={"caller":(Chain.BSC, Chain.ROBINHOOD)},
                     alpha=tr.AlphaConfig(accounts={"caller":(Chain.ROBINHOOD,)}))
    result = ad.measure(post("looks good", raw={"quoted_tweet":{"text":EVM}}), config,
                        connect=lambda:c, now_ms=NOW,
                        info=lambda *a,**k:SimpleNamespace(ok=True,data={"price":"1"}))
    assert result[0]["chain"] == "robinhood"


def test_ambiguous_evm_chain_does_not_price_the_wrong_chain():
    config = replace(cfg(), accounts={"caller":(Chain.SOL, Chain.BSC, Chain.ROBINHOOD)})
    assert ad.measure(post(EVM), config, now_ms=NOW,
                      connect=lambda:pytest.fail("ambiguous contract opened database")) == []


def test_full_queue_duplicate_and_closed_dispatch_never_block():
    entered = threading.Event()
    release = threading.Event()
    calls = []
    def worker(p,c):
        calls.append(p.tweet_id)
        entered.set()
        assert release.wait(2)
    d = ad.Dispatcher(worker=worker, workers=1, capacity=1)
    try:
        assert d.offer(post(), cfg())
        assert entered.wait(1)
        start = time.monotonic()
        assert not d.offer(post(), cfg())
        assert not d.offer(post(tweet_id="overflow"), cfg())
        assert time.monotonic()-start < .2
    finally:
        release.set()
        d.close()
    assert not d.offer(post(tweet_id="closed"), cfg())
    assert calls == ["alpha1"]


def test_no_alpha_runs_inside_blocking_launch_worker(monkeypatch):
    c = SimpleNamespace(close=lambda:None)
    monkeypatch.setattr(tl,"connect",lambda:c)
    monkeypatch.setattr(tl,"handle_post",lambda *a:[])
    monkeypatch.setattr(tr,"handle_refs",lambda *a:pytest.fail("alpha waited behind launch"))
    tl._process_post(post(),cfg())


def test_selected_stream_dispatches_alpha_before_launch_work(monkeypatch):
    import asyncio

    from kaiba.ingest import tweet_launch_feed
    events = []
    config = cfg()
    monkeypatch.setattr(tl,"load_config",lambda *a:config)
    monkeypatch.setattr(tl,"get_settings",lambda:SimpleNamespace(twitterapi_io_key="fixture"))
    monkeypatch.setattr(tl,"connect",lambda:SimpleNamespace())
    monkeypatch.setattr(tweet_launch_feed,"load_config",lambda *a:tweet_launch_feed.FeedConfig("twitterapi_rule"))
    for attr in ("observe_once","_refs_marks_own_conn","_resume_own_conn","_activity_own_conn","_vamp_own_conn"):
        monkeypatch.setattr(tl,attr,lambda *a:None)
    from kaiba.execution import launch_preflight
    monkeypatch.setattr(launch_preflight,"read_pump_global",lambda *a:None)
    class FakeDispatch:
        def offer(self,p,c):
            events.append("alpha")
        def close(self):
            events.append("closed")
    monkeypatch.setattr(ad,"Dispatcher",FakeDispatch)
    done = threading.Event()
    def launch(p,c):
        assert events == ["alpha"]
        events.append("launch")
        done.set()
    monkeypatch.setattr(tl,"_process_post",launch)
    class Finished(Exception):
        pass
    async def stream(*a,**k):
        yield post()
        assert await asyncio.to_thread(done.wait,2)
        raise Finished
    monkeypatch.setattr(tweet_launch_feed,"stream",stream)
    with pytest.raises(Finished):
        asyncio.run(tl.run())
    assert events == ["alpha","launch","closed"]
