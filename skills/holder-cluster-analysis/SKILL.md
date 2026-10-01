---
name: holder-cluster-analysis
description: Read bundles, snipers and insiders in a token.
version: 0.1.0
author: Kaiba contributors
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Intelligence, Holders, Bundles]
    related_skills: [entity-clustering, gmgn-token-dyor, developer-and-social-research, wallet-grading]
---

# Holder cluster analysis

## What this skill is for

Working out who actually holds a token's supply and how they got it: bundled at launch,
sniped in the first slots, handed out by the deployer, or bought like everyone else. This
is the difference between "top 10 hold 35%" (a statistic) and "one operator holds 35%
through eleven addresses funded from the same wallet nine minutes before launch" (a
reason not to buy).

It consumes the token roll-ups from `entity-clustering` and feeds the concentration
blockers in `gmgn-token-dyor`.

## When to use it

- Any time `gmgn-token-dyor` shows `cluster_pct`, `bundler_pct`, `sniper_pct` or
  `insider_pct` above their review thresholds, or shows them as unknown.
- Before a `curve-velocity` entry — that lane's `max_bundler_pct` is 20 and the bundle
  share is the thing most likely to invalidate it.
- When a token's holder count looks healthy but the wallets were all created the same day.
- When deciding whether a `confluence-5` signal's wallets are genuinely separate buyers.
- Post-mortem on a position that dumped: was the supply already concentrated at entry?

## Procedure

1. **Pull the dossier.** `kaiba_token(address, chain)` gives `bundler_pct`, `sniper_pct`,
   `insider_pct`, `cluster_pct`, `top10_pct`, `dev_pct` and `entity_count`. Note which
   arrived as `unknown`.
2. **Separate bundled % from currently-held %.** Bundled % is how much was acquired in
   the create slot. Currently-held % is how much of that is still held. A token bundled
   40% that is now 3% held has already distributed; a token bundled 22% still 22% held has
   a loaded gun pointed at the chart. Report both or neither.
3. **Reconstruct the bundle.** Same slot (~0.4 s), same mint, ≥ 2 non-creator wallets,
   with the slot's last transaction transferring ≥ 1,000 lamports to one of the eight Jito
   tip accounts. Three or more wallets in one slot does not happen by chance (TrenchBot).
   `scripts/bundle_share.py` does the arithmetic from a list of same-slot buys.
4. **Take the first N buyers.** Default N = 20 (`FIRST_N_DEFAULT`). For each: wallet age,
   first funder, prior co-occurrence with the creator's cluster, and whether they still
   hold. Repeat co-occurrence across ≥ 3 distinct tokens is what turns a coincidence into
   an edge (`entity-clustering`).
5. **Identify insiders, properly.** An insider sniper is a sniper **with a funding link to
   the deployer**, not merely someone fast. Same-block is not insider without the funding
   edge. Pine's dataset: ~4,600 such wallets, 87% success rate, present in ~1.75% of
   launches, peaking 14–23 UTC.
6. **Roll clusters up to supply share.** Sum the holdings of each entity, not each
   address, and exclude labelled infrastructure (pools, routers, CEX, bridges, burn).
   State the denominator: circulating, total, or top-250-holder share.
7. **Cross-check one provider.** GMGN `token holders` / `token traders --tag` and the
   Solana Tracker risk score are useful second opinions. Disagreement is a
   `PROVIDER_CONFLICT` warning, not an average.
8. **Journal the composition.** `kaiba_journal_append("observation", ...)` with the four
   percentages, the denominator and the bundle reconstruction evidence. If it changed a
   decision, say which.

## Thresholds and their source

| Measure | Threshold | Source |
|---|---|---|
| Bundled share (or currently-held bundle share) | **> 20% is a red flag** | Solana Tracker; `docs/research/02-memecoin-edge-and-risk.md` signal catalog; `curve-velocity.max_bundler_pct = 20` in `config/risk.yaml` |
| Wallets in one slot on one mint | ≥ 3 is never chance | TrenchBot, research 02 |
| Bundle definition | ≤ 5 tx, same slot, tip ≥ 1,000 lamports to one of 8 Jito tip accounts in the last tx | research 03 heuristic 7 |
| Sniper share | > 10% → Solana Tracker weight 3,000; > 50% → 10,000 | research 03 |
| Insider sniper | deployer → sniper transfer **before** launch | research 03 heuristic 9 (Pine) |
| First-N buyers | N = 20 | `FIRST_N_DEFAULT`; research 03 heuristic 11 |
| Contamination-adjusted cohort lift | +16.1% buyer count with no real SOL inflow | arXiv 2607.02795 (1,012 rings, 2–12 wallets, 166k launches) |
| Unexplained cluster | > 20% review, > 30% reject | owner mandate |
| Dev-attributable supply | > 10% reject | owner mandate |
| Top-10 share | > 15% → Solana Tracker weight 5,000 | research 03 |
| Single holder | > 90% → weight 7,000 | research 03 |
| Bundler fan-out shape | ≤ 20 sub-wallets typical; LUTs, staggering, warm-up trades | research 03 heuristic 6; cicere/pumpfun-bundler read for detection |
| Holder-graph coverage | Bubblemaps clusters only the top 250 holders | research 03 |

## Failure modes

- **Counting addresses instead of entities.** Eleven addresses in one cluster is one
  holder. This is the same error as the confluence independence bug, at the supply level.
- **Bundled % reported without currently-held %.** The first number without the second
  tells you about the past, not the risk.
- **Treating same-slot as proof of coordination on a hyped launch.** `SAME_SLOT_MAX_GROUP`
  is 50 for exactly this reason; above that it is a stampede.
- **Calling a fast buyer an insider.** Without the deployer funding edge it is a sniper,
  and snipers are common.
- **Counting pump.fun's own create+buy as a bundle.** The launchpad's own transaction is
  infrastructure.
- **Copy fan-out mistaken for a bundler.** A popular wallet's copiers all buy in adjacent
  slots; block-diff analysis separates them.
- **Stale holder snapshots.** Composition on a new launch changes minute to minute. A
  ten-minute-old holder list is a different token.
- **Missing data read as clean.** If the holder endpoint failed, `bundler_pct` is
  unknown. Unknown bundle share on a fresh launch is itself a reason to stand aside.

## What NOT to do

- **Do not present a top-holder statistic as a cluster.** The owner's mandate is explicit:
  a connected cluster and a concentration number are different claims.
- **Do not net a cluster's share against an unlabelled address you assume is a pool.**
  Exclusions must be verified and recorded.
- **Do not let a good bundle number rescue a hard blocker** from `gmgn-token-dyor`.
- **Do not change the 20% bundle threshold because a candidate is at 22%.** Propose it
  through `strategy-experiment` with evidence across trades, or take the review path.
- **Do not infer identity from a funding link.** It is a strong soft edge; it is not
  proof, and it is certainly not an identity claim about a person.
- **Do not trust an adversary-aware metric naively.** "Bubblemaps bypass" bundlers exist
  specifically to defeat transfer-only clustering with CEX routing and randomised
  amounts; weight co-signer, ALT and same-slot persistence higher.
