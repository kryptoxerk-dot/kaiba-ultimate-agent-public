---
name: x-scan
description: Read X for narrative and launches, read-only, on a budget.
version: 0.1.0
author: Kaiba contributors
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Intelligence, Alpha, Narrative, X]
    related_skills: [early-alpha-hunt, launch-snipe, alpha-radar, developer-and-social-research]
---

# X scan

## When to use it

When you need narrative evidence for a token or launch, or want to find launches nobody has signalled yet. Not for buying on hype.

## What this skill is for

Finding out what X is saying about a token or a launch **before** the crowd, and recording it
so the system can learn whether X activity predicts winners. You read X; you never post.

Two things already run without you:

- **The recorder** (ops job `x_narrative`, every 2 minutes). It searches X for the contract
  address of every token our live lanes signal on and stores post count, authors, reach and
  timing at that moment (`x_token_obs`, `x_posts`).
- **The audit** (ops job `signal_audit`, daily). It scores those numbers as the features
  `x_posts` and `x_followers`. Cells are labelled `delayed:` because the X read lands a few
  minutes after the signal.

Your part is the search the recorder cannot do: finding launches and narratives nobody has
signalled yet.

## Tool

`kaiba_x_search(query, latest=True, max_posts=20)` is read-only. It returns posts with author,
followers, likes, views, timestamp, and the cashtags, Solana mints and EVM addresses found in
each post. Every call counts against the shared daily cap (400 queries, about $1.20/day on
twitterapi.io; source: `daily_query_cap` in config/schedule.yaml, job x_narrative), and every
returned post is stored.

If it answers `http 402` or `no X credential`, X is unfunded or not configured. Say so once in
your report and stop calling it. Do not retry in a loop.

## Procedure: how to search well

1. **Contract address beats cashtag.** `$PEPE` matches a hundred tokens; a mint or `0x`
   address matches one. Search the address when you have one.
2. **Find launches early.**
   - Search phrases with recency: `"launching" pump.fun`, `"CA drops"`, `"stealth launch"`,
     `"fair launch" robinhood chain`, `pons launch`, `flap.sh`.
   - Collect the addresses found in the results (`refs.sol`, `refs.evm`).
   - A good find is a dev or name for the sniper watchlist (`kaiba_snipe_watchlist`), under
     that skill's caps and evidence rules (early-alpha-hunt).
3. **Judge reach, not volume.** Ten posts from one account, or from accounts with fewer than
   100 followers, are one voice. Count distinct authors and the largest real following.
4. **Coordinated raids look organic.** Watch for:
   - many new accounts posting identical text within minutes;
   - the same handful of accounts on every launch.

   Those are paid shills: that is evidence AGAINST, not for.
5. **KOL posts are often the exit.** Measured 2026-10-05 on 623 sol signals: tokens where 1+
   KOL wallet bought before our signal did worse (-20%) than tokens with none (-11%). A KOL
   tweet is information about who will sell to whom. It is not a buy signal by itself.

## What not to do (and what you may do)

- **May:** search, record, propose watchlist entries with evidence, and write a journal lesson
  on what X showed for winners versus losers.
- **May not:**
  - post, like, follow, DM or log in to anything;
  - size up on X hype;
  - treat any X feature as an edge before the signal audit marks it `edge` or `lift` on both
    halves;
  - spend past the daily cap.

## Report shape (when asked)

At most 8 lines:

- queries used today of the cap;
- the launches found, as address, chain, first-seen time and distinct authors;
- what you added to a watchlist, and why;
- any `x_posts` / `x_followers` cell from the latest audit (`kaiba_signal_audit`), with its
  old and new means.
