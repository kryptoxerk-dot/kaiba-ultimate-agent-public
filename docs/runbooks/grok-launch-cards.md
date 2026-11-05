# Grok Bot → tweet auto-launcher (`launch` cards)

Lead (Claude), 2 November 2026. Board `GROK-LAUNCH-FEED-CLAUDE-20261102`.
Owner: "auto launching ... using grok bot doom scrolling make sure the feature work".

## What it does

The Grok Bot scans X on its own schedule and sends signed cards to the existing intake
(`kaiba-grok-alpha`, Codex's `kaiba/hunters/grok_alpha_bridge.py`). A card on the **`launch`**
track names one post that a token could be launched on. The tweet launcher (`kaiba-tweet-launch`)
reads launch cards from the intake every `grok.poll_s` seconds (`kaiba/ingest/grok_launch_feed.py`)
and runs each post through the same code as the X stream:

```
Grok routine (X search) ──signed card──▶ kaiba-grok-alpha intake (inbox.db)
                                               │ read-only, every 5 s
                                               ▼
                         kaiba-tweet-launch: picker (Hermes/Claude) → logo → plan → record
                                               │ only if BOTH the launcher and grok.mode are live
                                               ▼
                         gmgn-cli cooking create (~5% dev buy) → reconcile → protection ladder
```

The Grok processor (`grok_alpha_processor`, kaiba-ops) records launch cards as route
`tweet_launch_feed` / `launcher_decides`. They are never handed to the Hermes model follow-up and
never reach the opportunities registry.

## Measured before building (2026-11-02, box)

- **The stream feed is dead.** The launcher's twitterapi.io feed has delivered 0 posts since
  2026-10-07. The socket connects and the key is accepted, but no rule matches arrive. Restoring
  it costs credits; the 2026-11-02 spending directive allows no new top-ups.
- **The rest of the launcher works.** On the box, in shadow, one test post was planned end to
  end:
  - Hermes on Claude (`claude-opus-5-5`, minimal reasoning) chose launch / Gerald / $GERALD in
    9.7 s.
  - The Codex image login failed. Pollinations drew the logo in 14.6 s.
  - All four routes built a valid `cooking create` argv: sol/pump 1.4846 SOL, bsc/flap
    0.2933 BNB, bsc/four.meme 0.3040 BNB, robinhood/pons 0.0893 ETH.
- **Grok is slow for this job.**
  - The 88 cards in the intake cite 232 posts. None carries the post's text, and the authors
    are small project accounts, not the launcher's 53 accounts.
  - Cited posts were a median **125 min** old at receipt (p10 11 min, min 1.3 min).
  - Grok routines run at least 5 minutes apart (15 min today).
  - The 2026-10-06 census found that only the **first** launch on a post has an edge, and the
    median first launch lands **23 s** after the post. A Grok-sourced launch will normally be
    late.

That is why Grok posts have their own age limit and their own mode. They stay **shadow** unless
the owner arms the Grok source separately (see Arming).

## Card contract (`kaiba.grok.observation/1`, track `launch`)

One card per post. The first source **is** the post.

```json
{
  "schema_version": "kaiba.grok.observation/1",
  "track": "launch",
  "project": "elonmusk post: dog in a tiny hat",
  "claim": "Short post naming one subject (Gerald), with a photo; 4.1k likes in 6 min",
  "network": null,
  "contract": null,
  "sources": [{
    "url": "https://x.com/elonmusk/status/2117002382601015453",
    "published_at": "2026-11-02T06:03:20Z",
    "retrieved_at": "2026-11-02T06:09:41Z",
    "text": "Every dog deserves a tiny hat. This is Gerald."
  }],
  "observed_at": "2026-11-02T06:10:00Z",
  "state": {
    "post_kind": "post",
    "media": ["https://pbs.twimg.com/media/XXXXXXXX.jpg"],
    "display_name": "Elon Musk"
  },
  "provenance": {"backend": "grok_bot_x_search"}
}
```

Rules the receiver enforces or the launcher relies on:

- **First source.** It must be `https://x.com/<handle>/status/<id>`, so the author is known.
  `i/web/status` links are refused.
- **Publication time.** `published_at` is required, with a timezone. The launcher measures age
  from it, and a post older than `grok.max_post_age_s` (1800 s) is recorded `too_late` and never
  named.
- **Text.** `text` must be the post's exact text, not a summary. Without text or media there is
  nothing to name.
- **Media.** `state.media` holds at most 4 URLs, and only `https://pbs.twimg.com/...` is kept.
  Other hosts are dropped, so a card cannot point the logo fetcher elsewhere. Without media the
  logo is generated.
- **Post kind.** `state.post_kind` is `post`, `quote`, `reply` or `repost` (default `post`). The
  launcher's per-account kind rules still apply: replies count only for listed KOLs and only when
  they name a token.
- **Batch and size.** At most 20 cards per delivery, and each card under 32 KB (the bridge's
  limits).
- **Sender-reported fields.** Text, kind and media are Grok's reading of the post and are not
  verified against X. The picker treats the text as untrusted: Hermes one-shot with
  `--safe-mode`, no tools.

## Instruction for the Grok Bot (owner to send, or Codex with the owner's go-ahead)

> Add a launch scan to your Kaiba routine. Each run, find posts published in the last 15 minutes
> by these accounts, newest first: elonmusk, realDonaldTrump, cz_binance, heyibinance, binance,
> toly, a1lon9, VitalikButerin, MEADGod, vladtenev, RobinhoodApp, blknoiz06, cobie, MustStopMurad,
> pumpfun, flapdotsh, ponsdotfamily (rotate in the rest of Kaiba's 53 launch accounts as your X
> read budget allows). Also include any post going viral right now (thousands of likes within
> minutes) that names one memeable subject: a pet, a nickname, a short phrase, a meme image.
> For every such post send ONE card on track "launch": the post URL, its exact published_at
> (UTC, with Z), its exact text (copy it; never summarise), state.post_kind, state.media with the
> post's pbs.twimg.com image URLs, and a one-line claim saying why it is launchable. Skip
> replies that do not name a token, plain news links, and anything older than 30 minutes. Never
> invent a post, a time or an image. Keep your existing tracks and budgets unchanged.

Budget note (sender-reported 2026-11-02): about 8 X reads per run and about 940 a week, shared
with the research tracks. A launch scan of up to two batched reads every 15 minutes uses about
190 reads a week. More accounts or a faster schedule means fewer reads for research.

## Checking it

On the box, from `~/kaiba`:

```sh
.venv/bin/python -m kaiba.ingest.grok_launch_feed --hours 24      # launch cards the intake holds
.venv/bin/python -m kaiba.execution.tweet_launch report           # verdict counts by chain/mode
journalctl --user -u kaiba-tweet-launch | grep -i grok            # cards refused or with no chain
```

Each Grok post is recorded in `tweet_launch_tweets` with `backend = 'grok_bot_x_search'`, and its
`feed_delay_ms` is publication → intake receipt. Each plan is recorded in `tweet_launches` with
its `decide_latency_ms` (publication → decision), its reasons, and, on the observer's next pass,
how many same-ticker launches the market made within 2 / 10 / 30 minutes of the post.

## Arming (owner only)

A Grok-sourced plan is live only when **all** of these hold:

1. `config/tweet_launch.yaml`: `mode: live`, `armed_by` filled in, and the chain's `live: true`.
   This is the launcher itself, stopped by the owner on 2026-10-08.
2. `config/tweet_launch.yaml` → `grok.mode: live`.
3. The `tweet-launch` lane is live in `config/risk.yaml`. The kill switch, reduce-only,
   paused-entries and halted-day brakes all apply.

Arming the stream alone never arms Grok's posts, and arming Grok alone arms nothing.
`tests/test_grok_launch_feed.py` pins both, and was mutation-checked.

Cost of one live launch: a ~5% dev buy (about 1.485 SOL / 0.29 + 0.30 BNB / 0.089 ETH). Because it
is the first buy on our own fresh curve, a launch with no buyers sells back for roughly the fees.
Of the 45 live launches of 2026-10-06..08, 44 were sold by the 60 s no-volume exit with no
buyers, net −0.022 SOL (the 2026-10-08 record). Whether late, Grok-timed launches draw any buyers
is exactly what the shadow record measures.
