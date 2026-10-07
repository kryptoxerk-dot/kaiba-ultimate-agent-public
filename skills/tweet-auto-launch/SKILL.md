---
name: tweet-auto-launch
description: Operate tweet-triggered token launches on evidence.
version: 0.1.0
author: Kaiba contributors
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Execution, Social, Launch]
    related_skills: [launch-metadata, launch-logo-generation, twitterapi-research, j7-social-tracking, position-protection]
---

# Tweet-triggered token launches

## What this skill is for

When a watched X account posts, Kaiba decides whether the post is launchable, derives a name
and ticker from it, and launches a token through GMGN `cooking create` from the agent wallet,
with a dev buy of about 5% of total supply inside the creation transaction. 5% means total
token supply, not wallet equity. The watchdog's ladder sells the bag in increments. This skill
is how the operator agent runs, checks and reports on that workflow
(`kaiba.execution.tweet_launch`, `config/tweet_launch.yaml`).

## When to use it

- Checking whether the launcher is receiving posts and what it decided (launch or skip, why).
- Reviewing a launch: its quote, its real allocation, its protection and its exits.
- Preparing to arm a chain, or explaining why a post was not launched.

## How it works

**Arming.** The shipped config is a paper template (`mode: shadow`, every chain
`live: false`): every decision is recorded in the `tweet_launches` table and nothing is sent.
Live needs ALL of: `mode: live`, a non-empty `armed_by`, the chain's `live: true`, the
`tweet-launch` Lane in `kaiba/core/schemas.py` and a live `tweet-launch` block in
`config/risk.yaml`. The per-launch and per-day caps in `config/tweet_launch.yaml` are
maximums, not required spend.

**Feed** (`kaiba.ingest.tweet_launch_feed`, `feed.backend`):

- `twitterapi_rule` -- a twitterapi.io `tweet_filter` rule (`from:a OR from:b`) delivered on the
  stream WebSocket; pay-per-use credits. Create and activate the rule on twitterapi.io.
- `twitterapi_monitor` -- the account-monitoring Stream plan (subscribes handles).
- `j7` -- J7Tracker's Socket.IO feed (`kaiba.ingest.j7_launch_bridge`); needs a J7 session JWT
  in `J7_SESSION_JWT` or `~/.config/kaiba/j7.session` and `python-socketio[client]`.

One socket per API key: only the `kaiba-tweet-launch` unit opens it. Only the configured
launch accounts are considered; never auto-launch from a whole tracker roster.

**Five-percent allocation** (`kaiba.execution.tweet_launch_policy`). `quote_five_percent` is
for a fresh constant-product curve with fees charged on top; `quote_five_percent_fee_on_input`
is for Pons, whose protocol fee is deducted from the input. Both target 5% of total supply,
rounded up by less than one indivisible unit, with integer arithmetic. `require_cap` refuses a
quote above the configured cap: the launch is skipped, never undersized. `allocation_receipt`
compares the minted supply with what the dev wallet actually received. Source for the modelled
costs (pump.fun ~1.485 SOL, Flap ~0.2933 BNB, four.meme ~0.3040 BNB, Pons V2 ~0.0893 ETH, fees
included; a BNB post launches on four.meme and Flap in parallel via the `also:` route): the
`dev_buy_native` curve models in `kaiba/execution/tweet_launch.py`. They are models, not fills.

**Vamps** (`kaiba.execution.tweet_vamp`). A vamp reuses a source token's exact name, ticker and
logo and launches through the same route, 5% buy, holder-fee settings and exits, sharing the
`tl:<tweet_id>:<chain>` intent with normal launches. It requires positive finite 5-minute
volume and swaps, a fresh original watched post, and source creation after that post. Name
matching infers context; it does not prove affiliation or organic volume. Copycats graduate
about 0.86% of the time against about 9.2% for originals (source: the study cited in
`config/tweet_launch.yaml`), so vamps ship in shadow.

**Exits.** The watchdog is the single exit owner. The shipped `protection.lanes.tweet-launch`
ladder in `config/risk.yaml` sells 20% / 25% / 33% / 50% of the then-remaining bag at 1.3x /
1.6x / 2x / 3x of entry, plus a full exit after 60 seconds without a trade on the curve.
`remaining_increment` freezes each rung's target and returns zero while an order is unresolved,
so a partial fill never re-sells the same percentage of a shrinking balance.

**Holder fees.** BNB / Flap: a dividend tax paid entirely to holders (`holder_fee_args`, zero
creator / burn / LP allocation). BNB / four.meme: a 1% fee paid to holders as dividends
(`dividend_fee_pct`), not yet verified live. Solana / pump.fun: through GMGN only Cashback, which pays
traders, not holders. Robinhood / Pons: no holder-fee mechanism through GMGN.
`j7_holder_fields` encodes J7's documented holder fields; it sends nothing and is not a GMGN
flag mapping.

## Procedure

1. Read `python -m kaiba.execution.tweet_launch report`: posts received, verdicts, reasons.
   No posts at all is a feed problem, not a quiet market -- say which.
2. Dry-run a specific post with
   `python -m kaiba.execution.tweet_launch plan --author <handle> --text "..."` (no I/O).
3. For a live launch, confirm in order: the exact 5% quote was under the cap, the order
   reconciled, the position row exists with protection armed, and `allocation_receipt` matches.
   Any gap is a discrepancy to report, not to paper over.
4. For holder fees on BNB, look for a real accrual or claim before calling them working.
5. Report what is measured and what is still unverified, separately.

## What not to do

- Do not arm a chain, widen a cap or change `armed_by`: that is the operator's decision, in
  writing.
- Do not top up after an ambiguous or partial creation, and never undersize a 5% buy to fit a cap.
- Do not attach GMGN cooking auto-sells while the watchdog holds the bag (two exit owners).
- Do not substitute cashback to traders, a creator split or a burn for holder payments.
- Do not move signing material between providers to work around a missing route.
- Do not claim the launcher makes money: tweet-launched tokens graduate less often than the
  baseline, and only being first shows a visible edge.

## Definition of done

Every post the feed delivered has a recorded verdict with reasons; every live launch has a
quote, a reconciled order, a protected position and an allocation receipt; anything missing is
named as unverified in the report.
