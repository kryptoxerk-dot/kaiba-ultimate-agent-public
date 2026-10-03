---
name: wallet-confluence
description: Enter on five independent entities buying.
version: 0.1.0
author: Kaiba contributors
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Trading, Confluence, Lanes]
    related_skills: [entity-clustering, signal-normalization, gmgn-token-dyor, trade-intent]
---

# Wallet confluence

## What this skill is for

Running the `confluence-5` lane: five or more **independent graded entities** net-buying
the same token on the same chain inside the window, with the dossier passing. It is one of
the two lanes the operator mandated directly, so it exists whether or not the research supports
it — and the research does not, yet. Confluence is evidence grade **C** in
`docs/research/02-memecoin-edge-and-risk.md`: a platform feature with no public backtest.
That is precisely why it starts in shadow and why the journal decides.

## When to use it

- A `confluence-5` signal appears in `kaiba_signals(lane="confluence-5")`.
- `kaiba_events(kinds=["signal.fired"])` shows the lane firing and you are deciding
  whether to act.
- The operator asks why a token with five smart-money buyers was not entered.
- You are reviewing the lane's expectancy and need to know what the parameters mean.
- The sibling lane `pons-robinhood` fires — it is the same shape with a lower entity
  floor.

## Procedure

1. **Read the signal.** `kaiba_signals(lane="confluence-5")` gives `wallets`,
   `entities`, `reasons`, `strength`, `window_s` and `created_ms`.
2. **Count entities, not wallets.** The lane's `min_entities` is 5 and it counts distinct
   `entity_id` values. If `len(entities) < 5` while `len(wallets) >= 5`, the lane has
   already done its job by refusing — say so rather than overriding it.
3. **Check the independence of those entities** (rule below). Unlinked is not the same as
   independent, and a copier's buy is an echo, not a confirmation.
4. **Check the age.** `max_signal_age_s` is 30 s. A confluence signal older than that
   describes a price you can no longer get.
5. **Check the dossier.** `kaiba_token(address, chain)` must be fresh enough and have no hard blockers. The lane's `require_wallet_grade` of B applies to contributing wallets, not to the token dossier. Read the actual engine's token policy rather than inventing a token-grade floor from the wallet-grade key. Five wallets never clear a blocker.
6. **Check the buyers are real evidence.** `kaiba_wallet` on the contributing addresses:
   grade, evidence weight, entity size, and whether the buy is a first entry or an add.
7. **Check lane state.** `kaiba_status()` for `kill_switch`, `entries_paused`,
   `reduce_only`, the lane's configured and effective mode, and today's halt state. The
   effective mode is the one that matters; a lane configured `live` under a `shadow`
   global mode is shadow.
8. **Hand to `trade-intent`.** Sizing, the precedence chain and the envelope live there.
   This skill's output is "lane eligible, N independent entities, dossier grade X".
9. **Journal either way.** A stand-aside with the entity count and the reason is the
   record the reflection job learns the lane's real hit rate from.

## Parameters and their source

| Parameter | Default | Source |
|---|---|---|
| `min_entities` | 5 | owner mandate ("at least five distinct eligible wallets"), stored in `config/risk.yaml` |
| `window_s` | 120 s, **event time** | `config/risk.yaml`; PLAN §6.1 marks it tunable |
| `min_buy_usd` | $50 | `config/risk.yaml` — the dust filter |
| `max_signal_age_s` | 30 s | `config/risk.yaml` |
| `require_wallet_grade` | B, for contributing wallets | `config/risk.yaml`; `lanes.confluence_5` |
| `size_pct_min` / `size_pct_max` | 1.0% / 5.0% of bankroll | `config/risk.yaml`; PLAN §6.2 |
| Envelope ceiling on size | 5.0% | `bounds.max_size_pct_bankroll` — operator-owned, unwritable by the agent |
| Chains | sol, robinhood, bsc, base | `config/risk.yaml` |
| `pons-robinhood` variant | `min_entities` 3, `max_age_s` 1,800, `min_liquidity_usd` 5,000, `min_buy_usd` 25 | `config/risk.yaml` + `DEFAULT_PARAMS`; lower floor justified by Pons's lower bot density (PLAN §6.1) |
| Evidence grade for the lane itself | **C** — platform feature, no public backtest | `docs/research/02-memecoin-edge-and-risk.md` |
| Copy-source hazard | sources front-run copiers and conceal positions | arXiv 2601.08641 (copy-trade baiting) |

## The independence rule

Five addresses are five signals only if five different people pressed five buttons.
Everything below reduces the count:

- **Same entity.** Any two addresses sharing an `entity_id` count once. Hard edges
  (`CO_SIGNED`, `SAME_BUNDLE`, `SHARED_ALT_AUTHORITY`, `DIRECT_TRANSFER`) merge at 0.90
  confidence; soft communities merge through Louvain. See `entity-clustering`.
- **Lead-lag.** If B buys within 30 s of A on ≥ 5 tokens, B is a copier or a side wallet.
  It does not merge the entities, but B's buy is not independent confirmation of A's.
  Count the leader.
- **Shared funding alone is not proof of common control** — the master prompt says so
  explicitly. It is a soft edge with a 250-address group cap, and it lowers confidence in
  independence without proving identity either way.
- **Unlinked ≠ independent.** A wallet with no entity row has not been cleared, only not
  linked. Report "3 confirmed independent, 2 unlinked" rather than "5 independent".
- **One wallet buying repeatedly is one wallet.** Adds are not new evidence.
- **Same-slot buyers are one bundle**, not five confirmations, when the slot ends in a
  Jito tip (`holder-cluster-analysis`).

When the count is ambiguous, the lane does not fire. Preserving the uncertainty is the
required behaviour, not a conservative style choice.

## Counting the wallets actually in use

Count stored A/B grades, active tracker addresses returned by the real selector, and wallets that actually contributed to emitted signals separately. Use `(chain,address)` identities and disclose cross-chain duplicate address strings. Report known hubs and quarantined cluster rows separately even when they retain a B label or an active watchlist flag; exclude them from the defensible qualified count without pretending a read-only audit removed them. Keep zero closed episodes distinct from unavailable counts and keep partial-tape grades explicit.

Read both `confluence-5` and `sm-trenches` modes and source-selection code. A shadow A/B-gated confluence lane is not a live strategy. The live smart-money lane can consume tags/archetypes without an A/B minimum; a large stored grade pool or an active-B roster must not be presented as an exclusive live-entry whitelist. Trace disabled shared watchlist policies and affected consumers before any cleanup, rather than silently changing other lanes while answering a count.

## Failure modes

- **Counting addresses.** The failure this lane exists to avoid. A bundler fanning out to
  twenty sub-wallets produces a perfect-looking confluence.
- **Stale dossier.** The candidate passed DYOR forty minutes ago and the LP has since
  moved. Execution-critical facts get re-read at decision time.
- **Duplicate entries as the count climbs.** Six wallets after five is not a second
  signal. Without an add-on policy, one entry per token per lane.
- **Late fill.** By the time five entities have bought, the price has moved. Measure
  copyability — how far the price ran between their fills and ours — and let the journal
  say whether the lane survives it.
- **Exit cascade.** Smart-money presence is a modest positive that also triggers
  correlated exits (research 02). The same five entities leaving is a fast move down.
- **Backfill.** A replayed hour of history fires this lane on every token at once. See
  `signal-normalization`.
- **Regime blindness.** Five entities buying during a market-wide rally is beta, not
  alpha. `strategy-experiment` covers attribution before crediting the lane.

## What NOT to do

- **Do not present five addresses from one entity as five signals.** This is the single
  most important rule in the lane.
- **Do not override a hard blocker, a stale dossier or a risk veto with the entity
  count.** Five wallets are not a permission.
- **Do not lower `min_entities` to make the lane fire more.** Propose it through
  `kaiba_propose_experiment` with shadow evidence; do not adjust it live because a
  candidate has four.
- **Do not write a fresh narrative essay on every trigger.** Maintain the dossier
  beforehand and refresh the execution-critical facts. Latency is part of the edge.
- **Do not size outside the lane's 1–5% band** or above `bounds.max_size_pct_bankroll`.
  Sizing belongs to `trade-intent`.
- **Do not count a KOL call as one of the five.** Calls are exit triggers by default
  (`alpha-radar`), and a caller's followers are not independent entities.
