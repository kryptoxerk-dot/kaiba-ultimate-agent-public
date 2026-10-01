---
name: airdrop-hunter
description: Rank airdrops by EV; single wallet, organic.
version: 0.1.0
author: Kaiba contributors
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Hunters, Airdrops, EV]
    related_skills: [trade-intent, provider-budget-audit, trade-journaling, alpha-radar]
---

# Airdrop hunter

## What this skill is for

Ranking airdrop and points programmes by expected value in dollars, and executing
**single-wallet organic** participation in the few that clear the bar. The EV model is
`kaiba/hunters/ev.py`; the research behind its constants is
`docs/research/06-hunters-airdrops-nft.md`.

This skill's most valuable output is usually "no". Its job is to turn "S-tier airdrop,
don't miss out" into a number.

## When to use it

- The daily airdrop registry run.
- the operator asks whether a specific programme is worth farming.
- A points programme announces a token and the ranking needs redoing.
- You are deciding how to spend a fixed capital allocation across programmes.
- Someone (or something) suggests running more wallets.

## Procedure

1. **Build or refresh the registry.** Sources, in descending usefulness:
   airdrops.io Telegram (`t.me/s/airdrops_io`), AirdropAlert RSS, the Alpha Drops
   points-programmes page, the DefiLlama airdrops page, DropsTab if subscribed. Quest
   platform APIs (Galxe, Zealy, Layer3) are campaign-owner tools and are not discovery
   feeds.
2. **Classify the token probability.** `p_token` = 0.9 confirmed token · 0.5 announced
   points programme · 0.15 speculative. A points programme is a promise; a blog post is
   not even that.
3. **Estimate the gross drop** from comparables, defaulting to $20 when there are none
   (`FALLBACK_VALUE_USD`). Typical drops are $20–100 and only ~20% are meaningful.
4. **Apply the haircut.** `unlocked × 0.64 + vested × 0.12`, with vesting defaulting to
   70%. The unlocked part is marked at the TGE-sell rate because we are one of the 64%
   selling into the same liquidity; the vested part at 12%, the complement of the 88%
   three-month decay. A typical 70%-vested drop therefore keeps 27.6% of its headline.
5. **Apply eligibility.** `ELIGIBILITY` by the programme's known filtering strength:
   low 1.0, medium 0.85, high 0.6. This is how hard *they* filter, not how hard we cheat.
6. **Subtract costs at full weight.** Gas, plus `capital × lockup_days / 365 × 8%`
   opportunity cost, plus operator hours at the configured rate ($60/h default). Costs
   are certain; the reward is a probability.
7. **Rank and cut.** Anything below `ev_threshold_usd` ($25 default) does not get built
   into a plan. Rank the rest and pick three to five.
8. **Execute single-wallet, organic.** Real swaps, real mints, real bridging within the
   allowlist, spread over time, with sizes that look like a person's. One wallet.
9. **Journal each plan and its outcome.** `kaiba_journal_append("observation", ...)` at
   ranking, `("outcome", ...)` at TGE or at abandonment. The registry is only as good as
   our record of what it predicted.

## The EV model

```
gross   = drop_value_usd * p_token * eligibility
haircut = unlocked_fraction * 0.64 + vested_fraction * 0.12
net     = gross * haircut * sybil_multiplier - costs
costs   = gas + capital * (lockup_days / 365) * 0.08 + operator_hours * hourly_rate
```

| Constant | Value | Source |
|---|---|---|
| `TGE_SELL_BASE_RATE` | 0.64 | 64% of recipients sell at TGE (research 06) |
| `THREE_MONTH_SURVIVAL` | 0.12 | complement of "88% of airdropped tokens lose value within 3 months" |
| `DEFAULT_VESTED_FRACTION` | 0.7 | drops are "often 70% vested" |
| `FALLBACK_VALUE_USD` | $20 | published low end of the typical $20–100 drop |
| `CAPITAL_RATE_ANNUAL` | 8% | opportunity cost of locked capital |
| `SYBIL_MULTIPLIER` | 0.2 | applied to **gross** for any multi-wallet plan |
| `ELIGIBILITY` | low 1.0 / medium 0.85 / high 0.6 | programme filtering strength |
| `ev_threshold_usd` | $25 | `hunters:` block of `config/risk.yaml` |
| `operator_hourly_usd` | $60 | same |

The Sybil multiplier hits the **gross**, not the net, on purpose: a cluster cut destroys
the reward and leaves every cost exactly where it was. Gas is still spent, capital is
still locked, hours are still gone. That is strictly harsher than scaling the net, which
is the correct shape for the risk.

## Sybil heuristics that get wallets cut

Every one of these is a documented production filter, not a theory:

| Heuristic | Where it was used |
|---|---|
| Common funding source | LayerZero's **primary** heuristic — 803,093 of >2M addresses flagged |
| Funder/sweep clustering into connected components + Louvain | Arbitrum; clusters **> 20 addresses** cut |
| Same CEX deposit address within a year, identical funding and similar amounts | zkSync; only >20 EOA clusters removed |
| Nansen labels plus AI clustering | Linea 2025: 1.30M → 780k eligible, ≤ 20 wallets per entity |
| Community self-report bounties | LayerZero 15% kept via self-report, bounty hunters 10% with a 20-address minimum; Hop accepted reports of ≥ 10 addresses |
| Timing correlation, identical action sequences and amounts | general |
| Gas fingerprints, dust activity | general |
| Shared IP / ASN / browser fingerprint | off-chain, and we have no defence against it |
| Cross-project retroactive blacklists | the reason a cut is not a one-time loss |
| Behavioural scores | Trusta MEDIA (Monetary 25 / Engagement 30 / Diversity 15 / Identity 10 / Age 20); Human Passport Stamps threshold 20 |

Note the convergence: three independent projects settled on roughly the same cluster
size (~20 addresses) and the same primary signal (shared funding). Our own
`entity-clustering` uses the same heuristics from the other side, which is a useful
sanity check — if our graph would merge a set of wallets, so will theirs.

## The honest verdict

**Multi-wallet farming is negative EV in 2026.** Not risky, not marginal — negative,
once you price:

- cluster cuts at ≥ 20 addresses, now standard;
- retroactive blacklists shared between projects, so one cut poisons future drops;
- typical drop $20–100 with only ~20% meaningful and often 70% vested;
- 88% of airdropped tokens losing value within three months;
- capital locked across every wallet simultaneously;
- drainers masquerading as farming bots — the popular GitHub farm bots require
  plaintext keys, which is a credential-exposure risk with no upside here;
- operator time, which is real money at any rate you would accept.

**Single-wallet, organic, capital-backed participation in three to five confirmed
programmes has modest positive EV.** That is the whole opportunity, and Kaiba does not
Sybil.

Confirmed for 2026 at the time of the research: Polymarket (token confirmed), Backpack
S4, MetaMask Rewards ($MASK), Aster, Kraken Ink, Meteora S2, Hyperliquid future
emissions, LayerZero S2. Speculative: Base, Abstract, Monad, MegaETH. OpenSea SEA
delayed. The structural shift is multi-season points over weeks or months rather than a
single snapshot, which raises the time cost and therefore the EV bar.

## Failure modes

- **Ranking by hype.** The loudest source is the one with the most affiliate links.
- **Forgetting the time cost.** Multi-season programmes charge in hours, and hours are
  in the cost line for a reason.
- **Double-counting capital.** The same $2,000 cannot back five programmes at once; the
  opportunity-cost term must not be charged five times against one balance either.
- **Treating points as tokens.** `p_token` 0.5 exists because points programmes fail to
  convert.
- **Running a farming bot.** Plaintext keys, unreviewed code, and drainers shipped under
  familiar repository names.
- **Ignoring the off-chain signals.** IP, ASN and fingerprint clustering are invisible
  to us and decisive to them.
- **Chasing an already-announced snapshot.** Retroactive eligibility windows close
  before the announcement.

## What NOT to do

- **Do not run multi-wallet or Sybil plans.** Not "carefully", not "with delays", not
  "with different funding paths". The model multiplies them by 0.2 and they still lose.
- **Do not fund one farm wallet from another.** That is the primary heuristic, handed
  over voluntarily.
- **Do not install a farming bot** or anything that asks for a key in plain text.
- **Do not present a drop's headline value as expected value.** Show the haircut.
- **Do not let a hunter plan consume trading capital or the gas reserve.** Airdrops are
  opportunistic; the reserve is not.
- **Do not skip the journal on a programme you abandoned.** A programme that looked good
  and then failed is the registry's most useful record.
- **Do not claim an EV number without its assumptions.** `p_token`, the vested fraction,
  the eligibility band and the lockup days are all guesses; state them.

## Running the hunters

`kaiba_run_hunter("airdrop")` refreshes the registry and returns how many
opportunities were found; `kaiba_opportunities(kind="airdrop")` reads them back
ranked by expected value.

Expect most entries to be negative-EV skips with the arithmetic attached. That is
the correct output, not a broken scraper. A scraped programme with no comparable
gets the published floor, and one hour of operator time already exceeds it.
`kaiba_run_hunter("nft")` and `kaiba_run_hunter("listing")` work the same way.
