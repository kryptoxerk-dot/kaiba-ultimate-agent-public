# Edge parameters, in pipeline order

Source of truth: `kaiba/intelligence/cluster.py` and `kaiba/intelligence/hubs.py`.
Research basis: `docs/research/03-wallet-clustering.md`.

## Stage 0 — hub pruning (before anything else)

| Setting | Value |
|---|---|
| `DEFAULT_DEGREE_CAP` | 200 counterparties |
| `HUB_KINDS` | `cex`, `router`, `pool`, `bridge`, `disperser`, `program` |
| Seeded hubs | Solana + EVM lists in `hubs.py`, plus the 8 Jito tip accounts |

Label sources worth adding as they become available: Solana Tracker identity tags,
SolanaFM tagged accounts, Vybe known-accounts, RugCheck insiders, GMGN tags, SNS.

## Stage 1 — hard links, union-find, confidence 0.90

| Edge | Parameters |
|---|---|
| `CO_SIGNED` | `CO_SIGNED_MAX_SIGNERS = 8`; `CO_SIGNED_INCLUDE_SWAP_WALLETS = True` |
| `SAME_BUNDLE` | `JITO_TIP_MIN_LAMPORTS = 1_000`, `SAME_BUNDLE_MIN_WALLETS = 2`, `SAME_BUNDLE_MAX_GROUP = 25` |
| `SHARED_ALT_AUTHORITY` | `SHARED_ALT_MAX_GROUP = 25` |
| `DIRECT_TRANSFER` | `DIRECT_TRANSFER_MIN_AMOUNT = 1` base unit, both ends unlabelled non-hubs |

Bundle reconstruction: consecutive same-slot transactions touching the same mint where the
last one transfers a tip to one of the eight Jito tip accounts. Bundle ids are not
on-chain; pull the live tip-account list via `getTipAccounts`. A single transaction with a
tip is ordinary MEV protection, not a bundle.

## Stage 2 — soft links, weighted graph, Louvain per component

| Edge | Parameters |
|---|---|
| `SAME_FUNDER` | base 0.85; +0.05 if amounts within 2%; +0.05 if Δt < 30 min; ≤ 3 hops, −0.05 per extra hop; 30-day window; group cap 250 |
| `SAME_SLOT_BUY` | ≥ 3 distinct tokens; group cap 50 per slot; confidence 0.5 → 0.8 saturating at 8 tokens |
| `FIRST_N_COOCCUR` | first 20 buyers; group cap 50; same confidence ramp |
| `LEAD_LAG` | ≤ 30 s, ≥ 5 tokens, confidence 0.60, group cap 100 — **directed, never merges** |
| `SHARED_CEX_DEPOSIT` | cap 1,000 senders, confidence 0.75 |
| `SHARED_COUNTERPARTY` | repeated third-party interaction; exclude programs, pools and CEXs |

## Stage 3 — refinement

Behaviour similarity (hold times, size distribution, venue mix, time-of-day) splits
communities that a soft edge over-merged. Optional: K-means refinement (Trusta) and a
LightGBM classifier over 2-hop subgraph features (F1 0.93, arXiv 2505.09313).

## Stage 4 — token roll-ups

Computed per token and consumed by `holder-cluster-analysis` and `gmgn-token-dyor`:

- bundled % **and** currently-held % (these differ; the second one is the live risk)
- sniper %
- insider % — snipers with a funding link to the creator cluster
- dev-cluster %
- fresh-wallet ratio
- wash ratio
- top-10 share

## Stage 5 — nightly re-run

Versioned cluster ids, evidence signatures (≤ 5) per edge, `entity.updated` on the bus.

## How incumbents differ (useful when a result disagrees with a public tool)

- **Bubblemaps** clusters the top 250 holders; V1 links are any historical gas-token
  transfer. Transfer-only clustering is exactly what "bypass" bundlers optimise against.
- **Arkham** is entity-first with OSINT and bounties; verified ≥98%, predicted ≥80%
  confidence, no published parameters. Best used to prune services, not to confirm merges.
- **Nansen** publishes numeric behavioural labels (e.g. Memecoin Whale > 0.1% supply of a
  > $30M mcap token) and flagged 803k LayerZero addresses with Chaos Labs.
- **GMGN** exposes Suspected Insider (same creation time + same funder + same transfer
  time), Bundled Tx Wallet, Sniper, Fresh Wallet, Bot Wallet.
- **Solana Tracker** publishes risk weights: rugged 20,000; bundlers ≥1,000 wallets
  15,000; snipers >50% 10,000 (3,000 at >10%); dev >50% 10,000; freeze authority 7,500;
  single holder >90% 7,000; top-10 >15% 5,000; LP not burned 4,000; mint authority 2,500.

## Anti-gaming checklist

1. Cluster-farmed PnL — does the cluster contain the creator or bundlers of the tokens it
   won on?
2. WT1/WT2 wash detection across the whole cluster.
3. Spray bots — require distinct-token count and median per-token PnL.
4. Copier demotion via block-diff analysis.
5. Expect multi-hop funding and warm-up trades; weight co-signer / ALT / same-slot
   persistence above transfer links.
