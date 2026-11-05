"""Grok Bot launch cards -> the tweet launcher (lead, 2026-11-02, GROK-LAUNCH-FEED-CLAUDE-20261102).

The intake is Codex's real ``grok_alpha_bridge.Store`` (a schema change there breaks these tests);
the picker, the logo and the send are stubs, so nothing reaches a model, an image host or GMGN.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

from kaiba.core.schemas import Chain
from kaiba.execution import tweet_creative as tc
from kaiba.execution import tweet_launch as tl
from kaiba.hunters import grok_alpha_processor as gp
from kaiba.hunters.grok_alpha_bridge import Store, normalize
from kaiba.ingest import grok_launch_feed as glf

T0 = 1_793_600_000_000                 # 2026-11-02T06:13:20Z, the intake's clock
PUBLISHED = "2026-11-02T06:03:20Z"     # ten minutes before T0
PUB_MS = T0 - 600_000
IMG = "https://pbs.twimg.com/media/Gx1abc.jpg"


class Clock:
    def __init__(self, at=T0):
        self.at = at

    def __call__(self):
        return self.at


def launch_card(post="2117002382601015453", author="ElonMusk", text="Every dog deserves a tiny hat. This is Gerald.",
                published=PUBLISHED, media=(IMG,), kind="post", track="launch"):
    return {"schema_version": "kaiba.grok.observation/1", "track": track, "project": f"{author} post {post}",
            "claim": "Short post naming one subject, with a photo", "network": None, "contract": None,
            "sources": [{"url": f"https://x.com/{author}/status/{post}", "published_at": published, "text": text}],
            "observed_at": "2026-11-02T06:10:00Z", "state": {"post_kind": kind, "media": list(media)},
            "provenance": {"backend": "grok_bot_x_search"}}


def ai(launch=True):
    return tc.AiIdentity(launch=launch, name="Gerald", symbol="GERALD", logo_prompt="a dog in a tiny hat",
                         reason="named subject", latency_ms=900, model="claude-opus-5-5")


def launcher_cfg(**over) -> tl.Config:
    """An ARMED launcher (stream live on sol) with Grok enabled in shadow; no model, logo or Codex."""
    base = tl.Config(mode="live", armed_by="test", accounts={"elonmusk": (Chain.SOL,)},
                     chains={Chain.SOL: tl.ChainRoute(Chain.SOL, "pump", Decimal("1.5"), live=True)},
                     namer_enabled=True, logo_generate=True, logo_backends=("pollinations",),
                     grok_enabled=True, grok_mode="shadow", grok_max_post_age_s=1800)
    return replace(base, **over)


@pytest.fixture
def intake(tmp_path):
    return SimpleNamespace(store=Store(tmp_path / "inbox.db", clock=Clock()), path=tmp_path / "inbox.db")


@pytest.fixture
def clock_at_t0(monkeypatch):
    monkeypatch.setattr(tl.time, "time", lambda: T0 / 1000)


def read_one(path):
    items, _ = glf.read_new(path, glf.start_cursor(0))
    assert len(items) == 1
    return items[0]


# ---------------------------------------------------------------- the card and the post


def test_the_intake_accepts_a_launch_card():
    key, digest, raw, expires = normalize(launch_card(), now_ms=T0)
    assert key and digest and expires is None


def test_a_launch_card_becomes_the_same_post_the_stream_yields(intake):
    intake.store.ingest([launch_card()])
    post, why = read_one(intake.path)
    assert why == "ok"
    assert (post.tweet_id, post.author, post.kind) == ("2117002382601015453", "elonmusk", "post")
    assert post.text == "Every dog deserves a tiny hat. This is Gerald." and post.media == (IMG,)
    # Times are the post's publication and the INTAKE's receipt: the Grok path is never made to look faster.
    assert post.created_ms == PUB_MS and post.received_ms == T0 and post.feed_delay_ms == 600_000
    assert post.backend == "grok_bot_x_search" and post.url == "https://x.com/elonmusk/status/2117002382601015453"
    assert post.raw["grok"]["text_source"] == "sender_reported"


def test_only_launch_cards_are_read_and_the_cursor_moves_on(intake):
    intake.store.ingest([launch_card(track="nft"), launch_card(post="2117002382601015454")])
    items, cursor = glf.read_new(intake.path, glf.start_cursor(0))
    assert [p.tweet_id for p, _ in items] == ["2117002382601015454"]
    assert glf.read_new(intake.path, cursor) == ([], cursor)


def test_media_is_kept_only_from_x_image_host_over_https():
    bad = ["http://pbs.twimg.com/media/a.jpg", "https://evil.example/a.jpg", "https://pbs.twimg.com:8443/a.jpg",
           "https://user@pbs.twimg.com/a.jpg", "https://pbs.twimg.com.evil.example/a.jpg", 7, "x" * 600]
    good = [f"https://pbs.twimg.com/media/{i}.jpg" for i in range(6)]
    post, _ = glf.to_post(launch_card(media=bad + good), received_ms=T0, observation_id="o", revision="r")
    assert post.media == tuple(good[:glf.MAX_MEDIA])


@pytest.mark.parametrize("card, why", [
    (launch_card(track="macro"), "not_launch_track"),
    ({**launch_card(), "sources": [{"url": "https://x.com/i/web/status/2117002382601015453",
                                    "published_at": PUBLISHED}]}, "first_source_not_an_attributable_post"),
    (launch_card(published=None), "publication_time_missing"),
    (launch_card(published="2026-11-02T06:03:20"), "publication_time_missing"),   # no timezone
])
def test_posts_that_cannot_be_attributed_or_dated_are_refused(card, why):
    assert glf.to_post(card, received_ms=T0, observation_id="o", revision="r") == (None, why)


def test_an_unknown_kind_is_a_post_and_a_missing_store_is_an_empty_read(tmp_path):
    post, _ = glf.to_post(launch_card(kind="space"), received_ms=T0, observation_id="o", revision="r")
    assert post.kind == "post"
    cursor = glf.start_cursor(5)
    assert glf.read_new(tmp_path / "absent.db", cursor) == ([], cursor)


# ---------------------------------------------------------------- grok_config: own age limit, own mode


def test_a_grok_post_never_inherits_the_streams_arming():
    c = launcher_cfg()
    assert c.live                                          # the stream is armed ...
    post, _ = glf.to_post(launch_card(), received_ms=T0, observation_id="o", revision="r")
    eff = tl.grok_config(c, post)
    assert not eff.live and eff.mode == "shadow"           # ... a Grok post is not
    assert eff.max_tweet_age_s == 1800 and eff.accounts == {"elonmusk": (Chain.SOL,)}
    assert tl.grok_config(replace(c, grok_mode="live"), post).live            # both armed: live
    assert not tl.grok_config(replace(c, grok_mode="live", mode="shadow"), post).live  # Grok alone: nothing


def test_an_unlisted_author_needs_any_author():
    post, _ = glf.to_post(launch_card(author="someone_new"), received_ms=T0, observation_id="o", revision="r")
    assert tl.grok_config(launcher_cfg(grok_any_author=False), post) is None
    eff = tl.grok_config(launcher_cfg(grok_any_author=True, grok_default_chains=(Chain.BSC,)), post)
    assert eff.accounts == {"someone_new": (Chain.BSC,)}


def test_the_shipped_config_reads_grok_cards_in_shadow():
    c = tl.load_config()
    assert c.grok_enabled and c.grok_mode == "shadow" and c.grok_any_author
    post, _ = glf.to_post(launch_card(author="someone_new"), received_ms=T0, observation_id="o", revision="r")
    armed = replace(c, mode="live", armed_by="test")
    assert not tl.grok_config(armed, post).live


# ---------------------------------------------------------------- through the launcher


def test_a_grok_post_is_named_planned_and_recorded_in_shadow(intake, tmp_db, clock_at_t0):
    intake.store.ingest([launch_card(media=())])
    post, _ = read_one(intake.path)
    calls = []
    plans = tl.handle_post(tmp_db, post, tl.grok_config(launcher_cfg(), post),
                           namer=lambda *a, **k: calls.append("ai") or ai(),
                           logo_maker=lambda *a, **k: calls.append("logo") or tc.Logo(source="pollinations", b64="QUJD" * 10))
    assert calls == ["ai", "logo"]
    [p] = plans
    assert (p.mode, p.verdict, p.symbol, p.image_source) == ("shadow", "launch", "GERALD", "pollinations")
    assert p.decide_latency_ms == 600_000
    row = tmp_db.execute("SELECT backend, feed_delay_ms FROM tweet_launch_tweets").fetchone()
    assert tuple(row) == ("grok_bot_x_search", 600_000)
    assert tuple(tmp_db.execute("SELECT mode, verdict FROM tweet_launches").fetchone()) == ("shadow", "launch")


def test_a_grok_post_older_than_its_limit_is_recorded_too_late_and_never_named(tmp_db, clock_at_t0):
    post, _ = glf.to_post(launch_card(published="2026-11-02T04:00:00Z"), received_ms=T0, observation_id="o", revision="r")
    calls = []
    [p] = tl.handle_post(tmp_db, post, tl.grok_config(launcher_cfg(), post),
                         namer=lambda *a, **k: calls.append("ai"), logo_maker=lambda *a, **k: calls.append("logo"))
    assert calls == [] and p.verdict == "skip" and any(r.startswith("too_late:") for r in p.reasons)


def test_the_service_path_never_sends_a_shadow_grok_plan(intake, tmp_db, clock_at_t0, monkeypatch):
    intake.store.ingest([launch_card()])
    post, _ = read_one(intake.path)
    sent = []
    monkeypatch.setattr(tc, "pick_identity", lambda *a, **k: ai())
    monkeypatch.setattr(tc, "make_logo", lambda *a, **k: tc.Logo(source="tweet_media", url=IMG))
    monkeypatch.setattr(tl, "send", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(tl, "_send_own_conn", lambda *a, **k: sent.append(a))
    tl._process_post(post, tl.grok_config(launcher_cfg(), post))
    assert sent == []
    assert tuple(tmp_db.execute("SELECT mode, verdict FROM tweet_launches").fetchone()) == ("shadow", "launch")
    # Positive control: the same post under an armed Grok source reaches send().
    post2 = replace(post, tweet_id="2117002382601015999")
    tl._process_post(post2, tl.grok_config(launcher_cfg(grok_mode="live"), post2))
    assert len(sent) == 1


def test_the_service_reads_grok_cards_while_the_x_stream_cannot_start(tmp_db, tmp_path, monkeypatch):
    """A box with no twitterapi key and no J7 session (the 2026-11-02 state, and a fresh install)."""
    import asyncio
    import contextlib

    import yaml

    from kaiba.execution import launch_preflight
    from kaiba.ingest import tweet_launch_feed

    raw = yaml.safe_load(tl.CONFIG_PATH.read_text(encoding="utf-8"))
    raw["grok"] = {**raw["grok"], "poll_s": 1}
    path = tmp_path / "tweet_launch.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    post, _ = glf.to_post(launch_card(), received_ms=T0, observation_id="o", revision="r")
    reads, processed, attempts = [], [], []

    def read_new(p, cursor, **k):
        reads.append(cursor)
        return ([(post, "ok")], (T0, "k", "d")) if len(reads) == 1 else ([], cursor)

    async def no_stream(*a, **k):
        attempts.append(1)
        raise tweet_launch_feed.FeedConfigurationError("missing_twitterapi_key")
        yield  # pragma: no cover - makes this an async generator, like the real stream

    monkeypatch.setattr(glf, "read_new", read_new)
    monkeypatch.setattr(tweet_launch_feed, "stream", no_stream)
    monkeypatch.setattr(tl, "_process_post", lambda p, c: processed.append((p.tweet_id, c.live, c.max_tweet_age_s)))
    for name in ("observe_once", "_upkeep_own_conn", "_activity_own_conn", "_vamp_own_conn", "_refs_marks_own_conn"):
        monkeypatch.setattr(tl, name, lambda *a, **k: 0)
    monkeypatch.setattr(launch_preflight, "read_pump_global", lambda *a, **k: None)
    monkeypatch.setattr(tl, "STREAM_RETRY_S", 0.05)

    async def main():
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(tl.run(path), timeout=1.5)

    asyncio.run(main())
    assert processed == [("2117002382601015453", False, 1800)]
    assert len(attempts) >= 2                                   # the stream is retried, the service lives


# ---------------------------------------------------------------- the processor leaves them to the launcher


def test_the_processor_routes_launch_cards_to_the_launcher_not_the_model(intake, tmp_db, tmp_path):
    def never(*a, **k):
        raise AssertionError("a launch card must not reach a scan, an intent or a provider")

    intake.store.ingest([launch_card()])
    out = gp.Processor(tmp_db, intake=intake.path, pipeline=gp.Pipeline(tmp_path / "pipeline.db"), clock=Clock(),
                       scan=never, intent=never, load_dossier=never, token_facts=never, token_market=never).run()
    assert out["processed"] == 1 and out["left_for_model"] == 0 and out["registered"] == 0
    [row] = gp.Pipeline(tmp_path / "pipeline.db", readonly=True).view(10)["rows"]
    assert (row["route"], row["next_action"]) == ("tweet_launch_feed", "launcher_decides")
    with sqlite3.connect(intake.path) as con:
        assert con.execute("SELECT state, lease_until_ms FROM revisions").fetchone() == ("unverified", 0)
