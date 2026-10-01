---
name: entity-clustering
description: Merge wallets into entities without hub collapse.
version: 0.1.0
author: Kaiba contributors
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Intelligence, Graph, Entities]
    related_skills: [wallet-grading, holder-cluster-analysis, wallet-confluence, developer-and-social-research]
---

# Entity clustering

## What this skill is for

Deciding when several addresses are one operator, and — more often — deciding that they
are not. The pipeline is in `kaiba/intelligence/cluster.py` and `hubs.py`. This skill is
how you interpret its output and how you argue with it.

One sentence carries the commercial weight of the whole skill: **five addresses inside one
entity are one signal, not five.** Every lane that counts wallets counts entities. If you
get this wrong, `confluence-5` fires on a bundler's fan-out and we buy a farm's exit.

## When to use it

- Before trusting any count of wallets: `confluence-5`, `sm-trenches`, `pons-robinhood`.
- When `kaiba_wallet` returns an `entity_size` greater than 1 for a wallet you were about
  to treat as independent evidence.
- When a token's holder list looks broad but the grades all appeared on the same day.
- When a wallet grades A and you want to know whether its PnL is its own.
- When a merge looks wrong and you need to work out which edge caused it.

## Procedure

1. **Prune hubs first, always.** `kaiba/intelligence/hubs.py` seeds CEX hot wallets,
   routers, pools, bridges, dispersers and programs, and applies
   `DEFAULT_DEGREE_CAP = 200` to anything unlabelled. Clustering before pruning collapses
   half of Solana into one entity via Binance's hot wallet. If a result looks absurdly
   large, suspect an unlabelled hub before you suspect the parameters.
2. **Apply hard edges with union-find.** `CO_SIGNED`, `SAME_BUNDLE`,
   `SHARED_ALT_AUTHORITY`, `DIRECT_TRANSFER` between two unlabelled non-hub wallets. All
   carry `HARD_CONFIDENCE = 0.90` — not 1.0, because an unlabelled relayer or a fee
   sponsor still looks like a co-signer.
3. **Apply soft edges as a weighted graph, then Louvain per component.** `SAME_FUNDER`,
   `SAME_SLOT_BUY`, `FIRST_N_COOCCUR`, `LEAD_LAG`, `SHARED_CEX_DEPOSIT`,
   `SHARED_COUNTERPARTY`. Soft edges never merge on their own evidence alone; they raise
   or lower the weight of a community.
4. **Read the result through the tools.** `kaiba_wallet(address, chain)` gives
   `entity_id` and `entity_size`. `kaiba_signals()` returns each signal's `entities` list
   next to its `wallets` list — the two lengths differing is the point of the field.
   `kaiba_events(kinds=["entity.updated"])` shows the nightly re-run.
5. **Check the independence claim explicitly.** For a confluence candidate, count distinct
   `entity_id` values, not addresses. Wallets with no entity row are their own entity of
   size 1 — unknown independence is not proven independence, so say which it is.
6. **Score the cluster, not only the member.** A wallet whose cluster-mates lost money is
   a farm's winning leg. Report the cluster's aggregate as well as the address's.
7. **Journal a merge you disagree with.** `kaiba_journal_append("observation", ...)` with
   the entity id and the edge type you think is wrong. Edge parameters are lane-adjacent
   tunables; changing one goes through `kaiba_propose_experiment`, not by hand.

## Hard versus soft edges

| Edge | Class | Rule | Confidence |
|---|---|---|---|
| `CO_SIGNED` | hard | multiple wallet signers in one tx, ≤ 8 signers total | 0.90 |
| `SAME_BUNDLE` | hard | same slot, same mint, ≥ 2 non-creator wallets, slot ends in a Jito tip ≥ 1,000 lamports | 0.90 |
| `SHARED_ALT_AUTHORITY` | hard | address-lookup table whose authority is an unlabelled wallet, group ≤ 25 | 0.90 |
| `DIRECT_TRANSFER` | hard | transfer between two unlabelled non-hub wallets, above dust | 0.90 |
| `SAME_FUNDER` | soft | first funder, recursive ≤ 3 hops, within 30 days | 0.85 base |
| `SAME_SLOT_BUY` | soft | same-slot co-buy, counted across tokens | 0.5 → 0.8 |
| `FIRST_N_COOCCUR` | soft | both in the first 20 buyers, counted across tokens | 0.5 → 0.8 |
| `LEAD_LAG` | soft | B buys within 30 s of A on ≥ 5 tokens | 0.60 |
| `SHARED_CEX_DEPOSIT` | soft | same deposit address, group ≤ 1,000 | 0.75 |
| `SHARED_COUNTERPARTY` | soft | repeated third-party interaction | low |

## Parameters and where they come from

| Parameter | Value | Source |
|---|---|---|
| Funder recursion | ≤ 3 hops, ≤ 30 days | `SAME_FUNDER_MAX_HOPS`, `SAME_FUNDER_WINDOW_DAYS`; research 03 heuristic 1 (Szwajcok 2026; Arbitrum/zkSync practice) |
| Funder amount bonus | +0.05 when amounts are within 2% | `SAME_FUNDER_AMOUNT_BONUS/TOLERANCE` |
| Funder timing bonus | +0.05 when Δt < 30 min | `SAME_FUNDER_TIME_BONUS/WINDOW_MS`; GMGN "Suspected Insider" = same creation time + same funder + same transfer time |
| Funder hop penalty | −0.05 per extra hop | `SAME_FUNDER_HOP_PENALTY` — obfuscation chains are cheap to build |
| Funder group cap | 250 | `SAME_FUNDER_GROUP_CAP`; Victor 2020 excludes clusters > 1,000 |
| Co-buy needs | ≥ 3 distinct tokens | `COOCCUR_MIN_TOKENS`; Kamat arXiv 2607.02795 (1,012 persistent rings, 2–12 wallets) |
| Co-buy saturation | confidence 0.5 → 0.8 by 8 tokens | `COOCCUR_SATURATION_TOKENS` |
| Same-slot group cap | 50 buyers | `SAME_SLOT_MAX_GROUP` — above that it is a launch stampede |
| First-N window | first 20 buyers | `FIRST_N_DEFAULT`; research 03 heuristic 11 |
| Lead-lag | ≤ 30 s delay, ≥ 5 tokens | `LEAD_LAG_MAX_DELAY_S/MIN_TOKENS`; solana-copy-trade-detect block-diff method |
| Jito bundle | ≤ 5 tx, same slot, tip ≥ 1,000 lamports to one of 8 tip accounts in the last tx | research 03 heuristic 7 |
| Co-signed ceiling | 8 signers | `CO_SIGNED_MAX_SIGNERS` — above that it is a multisig or a relayer batch |
| CEX deposit cap | 1,000 senders | `SHARED_CEX_DEPOSIT_CAP`; Victor 2020 (17.9% of EOAs, > 340k entities) |
| Degree cap | 200 counterparties | `DEFAULT_DEGREE_CAP`; sits above a bundler's ~20 sub-wallets and below a CEX sweeper |
| Evidence per edge | ≤ 5 signatures | `MAX_EVIDENCE` — enough for a human to check one merge |

## The lead-lag rule

`LEAD_LAG` is the edge that most wants to be misread. B buying 30 seconds behind A on five
tokens is strong evidence that B *watches* A. It is not evidence that B *is* A.

**Lead-lag never merges two wallets into one entity.** It produces a directed relation
that feeds two other things: the `side_wallet` and `copybot` archetypes, and the
independence test in `wallet-confluence` — a copier's buy is not an independent
confirmation of the source's buy, it is an echo of it. Treating it as a merge would
collapse every successful trader together with the people copying them.

## Failure modes

- **Hub collapse.** An unlabelled CEX deposit sweeper or launch-service treasury links
  thousands of wallets. Symptom: one entity with hundreds of members and mixed
  archetypes. Fix the hub table, do not loosen the edge.
- **Label circularity.** We label a wallet from a cluster we built using that label.
  Keep provider labels as attributes, never as clustering inputs.
- **Multi-hop obfuscation.** Adversaries route funding through 5–7 hops specifically to
  beat 3-hop recursion, and ship "Bubblemaps bypass" bundlers that use CEX routing and
  randomised amounts. Absence of a funding link is weak evidence of independence; weight
  co-signer, ALT and same-slot persistence higher than transfer links.
- **Known false positives** (TrenchBot's own list): multi-wallet trading terminals, copy
  bots, organic volume on a hyped launch, Squads multisigs, mint keypairs signing
  `create`, public protocol ALTs, fee sponsorship and relayers.
- **Resolution limit.** Louvain merges small communities inside a large component. A
  two-wallet entity inside a 400-node component may be an artefact.
- **Version drift.** Cluster ids are versioned and re-run nightly. An `entity_id` quoted
  in an old journal entry may no longer exist.

## What NOT to do

- **Do not present five addresses in one entity as five independent signals.** This is
  the owner's explicit mandate and the reason the `entities` field exists on `Signal`.
- **Do not treat shared exchange funding as proof of common control.** It is one soft
  edge with a 1,000-address cap, and the master prompt calls it out by name as
  insufficient.
- **Do not merge on lead-lag.** See above.
- **Do not cluster before pruning hubs.** Every bad cluster this system has produced
  started here.
- **Do not raise `DEFAULT_DEGREE_CAP` to "catch more".** More edges is not more truth;
  it is the hub collapse arriving by a different route.
- **Do not claim independence you did not test.** A wallet with no entity row means we
  have not linked it, not that we have cleared it. Write "unlinked", not "independent".

## Rebuilding the graph

`kaiba_rebuild_clusters(chain)` re-derives edges and entities for a chain and
returns the per-edge-type counts. Run it after a large wallet import, or when a
confluence signal names addresses you suspect are one operator. If the counts for
`co_signed`, `same_bundle` and `shared_alt_authority` are all zero, the ingest
layer is not recording signer or lookup-table metadata and the three strongest
hard heuristics are blind. Report that rather than trusting the remaining edges.
