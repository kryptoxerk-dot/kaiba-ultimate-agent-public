---
name: trusted-wallet-copy
description: Copy a trusted wallet safely, exits included.
version: 0.1.0
author: Kaiba contributors
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Trading, Copy, Lanes]
    related_skills: [wallet-grading, wallet-confluence, trade-intent, position-protection]
---

# Trusted wallet copy

## What this skill is for

The `trusted-copy` lane: a single buy by a wallet in the `trusted_copy` cohort can open a
position without waiting for five entities. It is the second lane the operator mandated, and it
is the one with the shortest distance between someone else's decision and our capital.

The cohort is curated by the owner. The agent may propose additions and must be
conservative about them; `kaiba_set_cohort` exists and works, which is exactly why the
bar for using it on `trusted_copy` is high.

## When to use it

- A `trusted-copy` signal appears in `kaiba_signals(lane="trusted-copy")`.
- A source wallet you copy has sold and you need to decide whether to follow.
- You believe a wallet has earned promotion into the cohort, or a member has earned
  removal.
- The operator asks how a copy position was sized or why a copy was skipped.

## Procedure

1. **Confirm cohort membership.** `kaiba_wallet(address, chain)` returns `cohort`. Only
   `trusted_copy` triggers this lane. A high grade in `tracked` does not.
2. **Check the delay.** `max_copy_delay_s` is 20 s from the source's **event time**. Past
   that, the trade is a different trade; skip it and journal the latency.
3. **Check the price drift.** `max_price_drift_pct` is 12%. If price has already moved
   more than that from the source's fill, we are buying their exit liquidity, not their
   idea.
4. **Run the mandatory checks anyway.** Custody, budget, tradability and the dossier
   blockers all still apply. `kaiba_token(address, chain)` must pass; a trusted source
   does not clear a honeypot.
5. **Check state.** `kaiba_status()` for `kill_switch`, `entries_paused`, `reduce_only`,
   lane effective mode and today's halt. `reduce_only` blocks the copy and permits exits.
6. **Size from our own book.** `trade-intent` owns sizing: 1–5% of bankroll for this lane,
   scaled by score, clamped to `bounds.max_size_pct_bankroll`. The source's position size
   is information about them, not an instruction to us.
7. **Keep the books separate.** Our cost basis, inventory and PnL are ours. A source's
   average entry is not our average entry, and a source's transfer out of a token is not
   a sell.
8. **Report source exits; do not sell on them yourself.** A sale by the source is news for
   the operator, not a sell order for you (owner rule 2026-10-06: never sell through MCP on your
   own). Our protection ladder runs underneath and does the exits (`position-protection`).
9. **Journal the copy and the source.** `kaiba_journal_append` with the source address,
   the delay achieved, the drift at fill, and the outcome. Per-source expectancy is what
   later justifies keeping or dropping them.

## Parameters and their source

| Parameter | Default | Source |
|---|---|---|
| `max_copy_delay_s` | 20 s | `config/risk.yaml` |
| `max_price_drift_pct` | 12% | `config/risk.yaml` |
| `follow_exits` | true | `config/risk.yaml` |
| `size_pct_min` / `size_pct_max` | 1.0% / 5.0% | `config/risk.yaml`; PLAN §6.2 |
| Envelope ceiling | 5.0% of bankroll | `bounds.max_size_pct_bankroll` |
| Chains | sol, robinhood | `config/risk.yaml` |
| Cohort values | `tracked`, `trusted_copy`, `blacklist`, `research` | `kaiba_set_cohort` signature |
| Copy-source hazard | sources front-run copiers and conceal positions | arXiv 2601.08641 (copy-trade baiting), research 02 grade A− |
| Execution latency reality | GMGN router allows 1 call per 5 s; anti-MEV needs fee ≥ 0.002 SOL | `docs/research/02-memecoin-edge-and-risk.md` |
| Wallet-quality context | 73.3% of wallets were net-positive Apr 2026, but 65% made ≤ $500 | research 02 |

## Promoting into the cohort

This is a risk decision, not a scoring decision, and the SOUL file calls it out by name.
A wallet entering `trusted_copy` converts one of its buys into one of our buys.

Necessary, and still not sufficient:

- Grade A under `wallet-grading` — score ≥ 70 with `evidence_weight` ≥ 75, ≥ 10 closed
  episodes and ≥ 5 distinct tokens.
- Entity checked: the wallet's cluster-mates are not losing money, and the wallet is not
  a leg of a farm that trades its own cluster's launches.
- Not a copier itself: no `LEAD_LAG` relation where this wallet is the follower.
- Measured **copyability** — how much price moved between their fill and a follower's
  realistic fill, across enough tokens to mean something. A wallet with real edge and
  zero copyability is unfollowable.
- Baiting check: does the source sell into copier flow? The published pattern is sources
  front-running their own followers.
- Shadow evidence: the lane run against this source in shadow, with positive expectancy
  after modelled fees and slippage.

Then, and only then, `kaiba_set_cohort(address, chain, "trusted_copy")` with a journal
entry stating the evidence. Never promote a provider's generic smart-money wallet, a
newly discovered address, or a high score alone — the master prompt forbids all three.

Removal is cheaper than addition and needs less evidence. When a source's measured
expectancy turns negative, move them to `tracked` and say why.

## Failure modes

- **Source transfer misread as a sell.** A transfer out is a transfer; follow only
  decoded sells.
- **Our stop and their exit fighting.** If our trailing stop already sold 50%, a "follow
  their full exit" instruction must not try to sell 100% of a position that is half gone.
  Ownership is tracked in `position-protection`; requesting a percentage of the remaining
  quantity is the correct shape.
- **Copy-of-a-copy.** The source is itself copying someone with a 2-second lag; we are
  third in the queue and the price shows it.
- **Baiting.** The source's buy is bait for copiers and their sell is into our fills.
  Per-source expectancy is the only detector we have.
- **Stale delay under provider latency.** The 20 s budget is event time. If our feed is
  8 s behind, the real decision window is 12 s. Measure it.
- **Concentration through one source.** Several copies from one source in a session is
  one thesis, sized several times. The per-entity correlation cap applies.
- **Source goes quiet.** A cohort member who has not traded in weeks is a stale
  permission sitting in the config.

## What NOT to do

- **Do not auto-promote into `trusted_copy`** from a grade, a provider label, a good
  week, or a discovery run.
- **Do not skip the dossier because the source is trusted.** Custody, budget, tradability
  and blocker checks are mandatory on every entry.
- **Do not mirror the source's size, leverage or cost basis.** Size from our bankroll and
  our envelope.
- **Do not treat a source transfer as a sell**, and do not treat their partial as our
  full.
- **Do not chase past the drift limit.** 12% is the line; beyond it the idea has already
  been paid for.
- **Do not copy into a lane state that forbids entries.** `reduce_only` and
  `entries_paused` exist for moments exactly like a fast copy signal.
- **Do not let a trusted source bypass `position-protection`.** Every copy position gets
  the same ladder, trailing and rug monitor as anything else.
