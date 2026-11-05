# Wallet grading rubric — component reference

Source of truth: `kaiba/intelligence/grade.py`. This file exists so a reader can check a
grade by hand without reading the module.

## Score assembly

```
score = 100 * (sum of earned points) / evidence_weight
evidence_weight = sum of COMPONENT_MAX for components that had data
```

A component with no data contributes neither points nor weight. That is the whole
evidence-normalisation idea: three measured components out of eight give a maximum
`evidence_weight` of well under 75, so A is unreachable without saying anything false.

| Component | Max | Present when |
|---|---|---|
| `realized_profit` | 20 | our reconstruction (`kaiba.intelligence.pnl`) or provider stats (×0.6) exist |
| `early_edge` | 16 | `EarlyMetrics` was computed for this wallet |
| `roi` | 14 | reconstruction has deployed capital and proceeds |
| `win_rate` | 12 | ≥1 closed episode |
| `big_win_rate` | 11 | ≥1 closed episode |
| `seed_confluence` | 12 | the seed pass ran (`seed_confluence is not None`) |
| `breadth` | 9 | distinct-token count known |
| `reputation` | 6 | a `Reputation` block exists |

## Grade gates

```
UNSCORED     evidence_weight < 30
A            score >= 70 and evidence_weight >= 75
             and closed_episodes >= 10 and distinct_tokens >= 5
B            score >= 40
C            score >= 20
D            otherwise
QUARANTINED  any tag in HARD_QUARANTINE_TAGS
```

Failing an A sub-gate does not lower the score; it caps the grade and appends a blocker
naming the gate that failed. Read the blocker before arguing with the grade.

## Multipliers

| Multiplier | Value | Applies to |
|---|---|---|
| `PROVIDER_CREDIT` | 0.60 | any component sourced from provider-reported numbers |
| `CREATOR_SELF_DEALING_MULTIPLIER` | 0.45 | wallets that mostly trade tokens they created |

## Archetype floors (`kaiba/intelligence/naming.py`)

| Constant | Value |
|---|---|
| `KOL_FOLLOWER_FLOOR` | 5,000 |
| `INSIDER_TOKEN_FLOOR` | 3 |
| `SNIPER_TOKEN_FLOOR` | 5 |
| `SNIPER_HOLD_S` | 300 |
| `SNIPER_TOKEN_BREADTH` | 40 |
| `EARLY_BUYER_RANK` | 10 |
| `DIAMOND_HOLD_S` | 86,400 |
| `DIAMOND_WIN_RATE` | 0.50 |
| `POSITION_HOLDER_SELL_RATIO` | 0.20 |
| `GMGN_CHUNK_SIZE` | 2,000 rows per imported follow list |

## Seed set construction (the zero-A fix)

Wrong: seed = tokens on a trending list at sample time.
Right: seed = tokens that **graduated** or returned **≥5×** in the last 30 days, then
find the wallets that were early in those.

Base rates that make the seed set small and therefore meaningful
(`docs/research/02-memecoin-edge-and-risk.md`):

- graduation rate 0.63% (Sep 2025, arXiv 2602.14860); 0.26% (Jun 2026, The Block/CoinLaw)
- 98.6% of pump.fun tokens fall below $1k liquidity (Solidus Labs)
- 92.2% of tokens with ≥30 swaps show a dump event (arXiv 2602.14860)

## Anti-gaming checks before trusting a high grade

1. Cluster-farmed PnL: does the wallet's entity contain the creator or the bundlers of the
   tokens it "won" on?
2. WT1/WT2 wash patterns across the cluster, not just the address.
3. Spray bots: require distinct-token count **and** median per-token PnL; penalise
   buy-within-3-seconds rates.
4. Copier edges: a wallet that buys 2 s behind a source is a copier, not an edge. Demote
   via block-diff analysis (`LEAD_LAG` in `entity-clustering`).
5. Still-alive filter: 7-day and 30-day activity. A dead wallet's grade is history.
