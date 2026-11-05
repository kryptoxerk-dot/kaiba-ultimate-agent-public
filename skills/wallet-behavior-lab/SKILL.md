---
name: wallet-behavior-lab
description: Read how a tracked wallet trades, not just if it won.
version: 0.1.0
author: the operator, Claude
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Wallets, Research, Behavior]
    related_skills: [wallet-hunt, wallet-grading, wallet-confluence, trusted-wallet-copy]
---

# Wallet behavior lab

## What this skill is for

Answering "how does this wallet actually trade?" from measured behavior instead of from a
grade, a win rate or a popularity list. Every day the box re-reads the last 14 days of GMGN
activity for each wallet on the tracked Solana S/A/B list and writes one profile per wallet:

| feature | meaning |
|---|---|
| `F1_entry_age_s` | median seconds from token creation to the wallet's first buy |
| `F2_entry_vs_prior_high` | median entry price / highest 5m high in the 6 h before (below 1 = buying under the recent high) |
| `F5_usd`, `F5_bps_of_mcap` | median first-buy size, in USD and in basis points of market cap |
| `F6_first_sell_s`, `F6_first_sell_frac` | median time to the first sell, and the share of the bag it takes |
| `F7_hold_s`, `share_fully_exited` | median time to a full exit, and how often it fully exits |
| `share_multi_entry` | share of tokens where it added on a 30% dip or re-entered after selling |
| `closed_net_usd`, `closed_won` | sells minus buys on tokens it fully exited (GMGN prices), and how many won |

and the behavior classes from the pre-registration (a wallet may hold several; it needs at
least 5 tokens): `sniper`, `fast_flipper`, `early_accumulator`, `dip_buyer`,
`breakout_buyer`, `multi_entry`, `staged_exit`, `small_size`.

A profile is **descriptive**. It says how the wallet trades. Whether a follower makes money
copying that behavior is a different question, answered only by the lab's pre-registered
follower test, which `kaiba_wallet_behavior()` returns as `verdict`.

## When to use it

- the operator asks about a wallet, or a wallet on the S/A/B list shows up in a confluence alert.
- You are about to describe a wallet as "early", "a sniper", "a dip buyer" or "a holder":
  check the profile instead of inferring it from one trade.
- You are deciding whether a wallet's buy is worth surfacing at all (a `fast_flipper`'s buy is
  usually sold before a follower's order lands).
- The daily labs review job runs.

## Procedure

1. **Read the profile.** `kaiba_wallet_behavior(address=...)`. `profile: None` means the
   wallet is not on the tracked sol S/A/B list; say so, do not estimate.
2. **Check freshness.** `stale: true` means the daily job has not run for 36 h: report the
   date (`at`) with every number.
3. **Read the classes with their numbers.** Quote the medians behind a class ("sniper:
   median entry 40 s after creation over 12 tokens"), never the label alone. `f2_priced`
   says how many tokens had candles for the prior-high comparison.
4. **Read the verdict before you recommend following anything.**
   `kaiba_wallet_behavior()` (no address) returns `verdict`. If `passing` is empty, no
   behavior class of these wallets made money for a follower 60 s late at 5.5% round-trip
   cost, so say so. A passing cell is a hypothesis for a forward paper test that the operator
   decides on. It is never a lane and never a reason to size up.
5. **Compare with what we already measured.** Following wallets has lost money every way we
   have tested it (live fills 09-24, copy forward test 10-02, copy-exit-early 10-07, the 10-27
   wallet review, A/B confluence 10-28). A good-looking profile does not overturn that.
6. **Use it for context, not for orders.** The output of this skill is a description
   ("this wallet buys within a minute of launch and sells half the bag inside two minutes"),
   not a buy or a sell.

## Thresholds and their source

| threshold | value | source |
|---|---|---|
| profile window | 14 days, newest 25 tokens per wallet | `docs/research/claude-wallet-lab-prereg-20261028.md` |
| class rules | as in the table of the pre-registration | same file, "Behavior classes" |
| minimum tokens to class | 5 | same file |
| follower model | buy the first 1m close at or after leader + 60 s; exits mirror / 30 min / 4 h; 5.5% round trip | same file, "Follower outcome" and Amendment 1 |
| pass line | n ≥ 100 from ≥ 10 wallets, mean > 0, wallet-clustered 95% lower bound > 0, beats the control class | same file, "Pass line" |
| staleness | 36 h | `kaiba/learning/labs.py` `STALE_S` |
| daily job | `claude-wallet-lab-daily.timer`, GMGN reads only, 1,500-call budget | `deploy/research/wallet_lab/daily.py` |

## What not to do

- Do not call a wallet profitable to follow because its `closed_net_usd` is positive: that
  is the wallet's own fills at its own speed, not a follower's.
- Do not treat a class as a signal, or wire it into a lane, a cohort or a size. That needs a
  passing cell AND the operator's decision.
- Do not lower the class thresholds or the pass line to make more wallets qualify.
- Do not read wallets outside the tracked list through other tools to "complete" the lab;
  the population is fixed by the daily export.
- Do not sell, buy or exit anything on the strength of a profile.
