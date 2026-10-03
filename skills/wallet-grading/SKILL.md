---
name: wallet-grading
description: Score a wallet and read the grade honestly.
version: 0.1.0
author: Kaiba contributors
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Intelligence, Wallets, Scoring]
    related_skills: [entity-clustering, developer-and-social-research, trusted-wallet-copy, trade-journaling]
---

# Wallet grading

## What this skill is for

Turning a bare address into a defensible opinion: a grade (A/B/C/D, or `UNSCORED` /
`QUARANTINED`), an archetype, and a list of blockers that say why the grade is capped.
The rubric lives in `kaiba/intelligence/grade.py` (`MODEL_ID = "kaiba-wallet-v1"`); this
skill is how you *run* it, how you *read* it, and the two failure modes that made the
previous system produce zero Grade-A wallets for eight months.

The one idea the whole rubric rests on: **a component we could not measure is not a
zero.** The score is normalised over `evidence_weight` — the sum of the maximum points of
the components that actually had data. A wallet with three components measured cannot
reach A no matter how good those three look.

## When to use it

- The operator asks `/grade <address>` or "is this wallet any good?".
- A new cohort landed from a gather run and needs scoring before it can feed a lane.
- A `confluence-5` or `sm-trenches` signal fired and you want to know whether the wallets
  behind it are real or a farm.
- You are about to propose a wallet for the `trusted_copy` cohort (see
  `trusted-wallet-copy` — grading is necessary there, never sufficient).
- A graded wallet's recent trades look wrong and you suspect the grade is stale.

## Procedure

1. **Read what we already know.** `kaiba_wallet(address, chain)` returns grade, score,
   archetype, `evidence_weight`, `blockers`, entity id, entity size and the last 20 swaps.
   `found: false` means nobody has graded it; that is a gap, not a bad wallet.
2. **Check the evidence weight before the score.** Below `MIN_EVIDENCE_WEIGHT = 30` the
   grader returns `UNSCORED` and refuses to guess. A score of 88 on an
   `evidence_weight` of 34 is a rumour with a decimal point. Report the pair, never the
   score alone.
3. **Check the entity, not just the address.** If `entity_size > 1`, the wallet's PnL may
   be the cluster's PnL. Run `entity-clustering` before you believe a high grade: a
   "smart" wallet whose cluster-mates lost money is a farm
   (`docs/research/03-wallet-clustering.md`, scoring section).
4. **Read the blockers.** They are the honest part of the output. `A_MIN_CLOSED_EPISODES`
   and `A_MIN_DISTINCT_TOKENS` appear here when a wallet scores like an A but has not
   traded enough to earn one.
5. **Cross-check the archetype against behaviour.** `kaiba_wallet` gives recent trades;
   a `diamond` archetype with 40-second holds in the tape is a naming bug or stale data.
   Say so in the journal rather than quietly trusting the label.
6. **Record the read.** `kaiba_journal_append("observation", ...)` with the address, the
   grade, the evidence weight and what you concluded. If the grade changed your view of a
   live or shadow lane, say which lane.
7. **Act only through cohorts.** The only grade-driven action available to you is
   `kaiba_set_cohort(address, chain, cohort)` — `tracked`, `research`, `blacklist`, or
   `trusted_copy`. Grading does not size a trade; `trade-intent` does.

## The eight components and their weights

| Component | Max | What it measures |
|---|---|---|
| `realized_profit` | 20 | Fee-adjusted realised PnL from our own reconstruction |
| `early_edge` | 16 | Universe-relative entry rank: validated-early, sniper and insider token counts |
| `roi` | 14 | Return on deployed capital, not headline profit |
| `win_rate` | 12 | Share of closed episodes in profit |
| `big_win_rate` | 11 | Share of closed episodes that returned ≥6× ("actual winner") |
| `seed_confluence` | 12 | Overlap with the seed token set — the component that was always 0 |
| `breadth` | 9 | Distinct tokens and distinct creators traded |
| `reputation` | 6 | Verified social / KOL / renowned tags |

Sums to 100 by construction (`COMPONENT_MAX` in `kaiba/intelligence/grade.py`).

## Thresholds and where they come from

| Threshold | Value | Source |
|---|---|---|
| Grade A | score ≥ 70 **and** `evidence_weight` ≥ 75 **and** ≥ 10 closed episodes **and** ≥ 5 distinct tokens | `grade.py` (`A_MIN_*`); PLAN §5.2 |
| Grade B | score ≥ 40 | `grade.py` `B_MIN_SCORE`; PLAN §5.2 |
| Grade C | score ≥ 20 | `grade.py` `C_MIN_SCORE` |
| `UNSCORED` | `evidence_weight` < 30 | `grade.py` `MIN_EVIDENCE_WEIGHT` |
| Provider-reported PnL credit | × 0.6, never full | `grade.py` `PROVIDER_CREDIT`; PLAN §5.2 ("grades do not depend on provider-reported numbers") |
| Creator self-dealing | × 0.45 when the wallet mostly trades tokens it created | `grade.py` `CREATOR_SELF_DEALING_MULTIPLIER` |
| "Actual winner" | a closed position that returned ≥ 6× | KAIBA CORP AGENT SCORING-V2, carried into `big_win_rate` |
| `validated_early` | top-3 entrant on a token that drew ≥ 10 buyers | `wallet-grading/05_metrics.py`, `EarlyMetrics` docstring |
| `insider_tokens` | top-2 buyer within 60 s | same |
| `sniper_tokens` | bought within 5 minutes of launch | same |
| Archetype floors | KOL ≥ 5,000 followers; insider ≥ 3 tokens; sniper ≥ 5 tokens with median hold ≤ 300 s; diamond ≥ 24 h hold and ≥ 50% win rate | `kaiba/intelligence/naming.py` |
| Context for any "good trader" claim | 73.3% of wallets were net-positive in Apr 2026 but 65% made ≤ $500 | `docs/research/02-memecoin-edge-and-risk.md` (CoinGecko via CoinLaw) |

## The zero-Grade-A trap

The prior scorer (`grade.mjs`) never produced a Grade A, and the reason was not the
thresholds. It was **seed selection**.

`seed_confluence` asks: did this wallet buy the tokens that turned out to matter? The old
pipeline seeded from *trending lists* — tokens that were already trending when we looked.
A wallet that bought them early is, by construction, not in the trending snapshot's buyer
set at the time we sampled it, so `seed_confluence` came back 0 for essentially everyone.
Twelve of the hundred points were permanently unavailable, which also dragged
`evidence_weight` below the A floor of 75.

**The fix, and the rule: seed from outcomes, not from attention.** Build the seed set from
tokens that graduated or did ≥5× in the last 30 days, then ask which wallets were early in
*those*. Only then does `seed_confluence` carry information.

A second-order trap follows from it: `seed_confluence = None` means "we never ran the seed
pass" and must not be scored; `seed_confluence = 0` means "we ran it and this wallet
touched no seed" and is real evidence. The `WalletEvidence` model keeps the two apart on
purpose. If you find yourself explaining a fleet of UNSCORED wallets, check which one you
are looking at before you touch a threshold.

## Failure modes

- **Grade inflation from provider numbers.** GMGN's reported profit is applied at 0.6
  credit. If our reconstruction is missing, a wallet can look strong on borrowed evidence.
  Check whether `pnl` or only `provider_stats` was present.
- **Cluster-farmed PnL.** The wallet won on tokens its own cluster created or bundled.
  `entity-clustering` plus the per-cluster score catches this; the wallet grade alone
  never will.
- **Spray bots.** High distinct-token counts with tiny median per-token PnL and
  buy-within-3-seconds rates. Breadth is 9 points and should not rescue a bot.
- **Wash trading.** WT1 (buy and sell identical amounts in one tx) and WT2 (round trips
  at ~equal price) inflate every volume-linked component
  (`docs/research/03-wallet-clustering.md` heuristic 14).
- **Stale grades.** A grade is a snapshot. A wallet that was A three weeks ago and has
  been quiet since is an A-shaped memory. Check `recent_trades` timestamps.
- **Sample caps.** `sample_capped` means our reconstruction stopped early; the wallet may
  be larger and worse (or better) than the window shows.

## What NOT to do

- **Do not report a score without its evidence weight.** They are one number with two
  parts.
- **Do not treat five addresses from one entity as five graded wallets.** That is the
  `entity-clustering` rule and it overrides anything grading says.
- **Do not promote into `trusted_copy` because a grade is high.** A high grade plus a
  single buy triggers real capital. Promotion needs measured copyability evidence — see
  `trusted-wallet-copy`.
- **Do not change `A_MIN_*` or `MIN_EVIDENCE_WEIGHT` to make the A count look healthier.**
  If the A count is zero, the seed set is the suspect. Tuning the threshold to produce
  a desired output is the reward-hacking failure mode the gates exist to prevent; propose
  it through `strategy-experiment` if you genuinely believe the floor is wrong.
- **Do not read a provider tag as a grade.** "Smart Money" is a label somebody else
  computed with unpublished parameters. It is worth 6 points of reputation, at most.
- **Do not grade from a leaderboard's top five.** Mine ranks 20–100 and repeat early
  buyers across 20× tokens instead (`docs/research/03-wallet-clustering.md`,
  anti-gaming).

## Hourly-cycle preflight

- Inspect the installed seed implementation before running it: an all-history max/first-price pass is not a rolling 30-day graduation-or-5x pass. Verify the outcome timestamp filter and graduation union; count failures inside successful job envelopes. Do not certify seed compliance from the job name alone.
- Check live scheduler ownership before invoking `ops run --once`: startup recovery may touch unrelated running-job state even with `--only`. Prefer scoped tools while the daemon is alive; do not bypass quotas or cron approval denials.
- Treat empty `wallet.graded` / `entity.updated` event windows as event absence, not proof of no database changes. Verify bulk-grader event coverage and compare saved grade/member snapshots before calling a change new.

## Grading a wallet on demand

`kaiba_grade_wallet(address, chain)` runs the rubric now against the evidence in
the database and stores the result. Use it when a wallet appears in a signal but
has no grade yet. It returns the factor breakdown, so if the grade is lower than
you expected, read which components were missing rather than arguing with the
number: a thin-evidence wallet is capped by design.

`kaiba_wallets(cohort=..., grade=...)` lists the cohort. Use it to answer "who is
in trusted_copy" before you propose a change to it.
