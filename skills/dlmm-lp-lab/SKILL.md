---
name: dlmm-lp-lab
description: Judge DLMM LP keep, re-center or close on evidence.
version: 0.1.0
author: the operator, Claude
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Liquidity, Meteora, Research]
    related_skills: [operator-readouts, strategy-experiment, token-intelligence-brief]
---

# DLMM LP lab

## What this skill is for

Answering questions about providing liquidity on Meteora DLMM with what real positions
actually earned, not with APR banners. Every day the box re-reads a frozen panel of
Meteora DLMM pools (picked by fees on 2026-10-28) and the LPs sampled in each, using only
Meteora's keyless data API, and measures each position against holding:

- **LP vs holding SOL** (`pnlSol` / SOL deposited) and **vs USDC** (`pnlUsd`).
- **LP vs holding the deposit**: the tokens it put in, held instead, at the pool's close.
- **Fees vs inventory**: what fees earned and what the inventory change cost.
- **Per SOL per day**: total profit / (deposit × days held), the fair comparison between a
  20-minute position and a two-day one.
- **What decides the outcome**: terciles of time in range, duration, width, and the price move
  before opening, plus "frequent LPs" (≥ 10 positions, median ≤ 6 h).
- **Forward record**: positions opened after the freeze. It accumulates daily and is the part
  free of the pick-by-today bias.

Kaiba runs no LP lane and holds no LP position. This skill produces evidence, never an order.

**Read the headline against the de-biased result.** The panel's pools were chosen because they
were still earning fees on 2026-10-28. Pools sampled by creation date instead (phase 1b: 120
random DLMM pools created 2026-08-24..09-23, 94% dead now) LOST -0.29% per SOL-day against holding
SOL (-0.53% per position, 54.5% positive), and the pre-registered pass line failed
(`docs/research/claude-dlmm-birth-pools-20261028.md`). Picking a pool is the whole game, and nothing
measured yet picks well. Time in range still separated outcomes there (always in range +2.5% per
position, mostly out of range -4.7%), so the keep / rebalance / close evidence stands.

## When to use it

- the operator asks whether LPing a pool is worth it, which pools LPs make money in, or what to do
  with an LP position (keep, re-center, narrow, close).
- Someone quotes a pool's APR or fee/TVL as if it were a return.
- The daily labs review job runs.

## Procedure

1. **Headline first.** `kaiba_lp_lab()` gives `vs_sol`, `vs_holding_token`, `per_sol_day`,
   `fees_cover_share`, the splits and `forward`. Quote the median next to the mean: most
   positions earn very little, and the mean is carried by fewer, longer, in-range ones.
2. **Check freshness and the forward record.** `stale: true` means no run for 36 h. Say how
   many forward positions exist (`forward.lp_sol.n`) before leaning on the headline, because
   the headline still contains the panel's selection bias.
3. **One pool.** `kaiba_lp_lab(pool="<address>")` returns that pool's row: positions, LPs, LP
   vs SOL, vs holding, time in range, open positions in range, per SOL-day, TVL and fees/TVL.
   A pool not in the panel returns `note: "not in the panel"`. Say that; do not extrapolate.
4. **One position.** `kaiba_lp_lab(in_range_pct=..., hours_open=..., out_of_range_now=...,
   pool=...)` returns `decision` (`keep` / `rebalance` / `close`) and the `evidence` lines,
   each with the measured number. Relay the evidence, not just the word. The rules in order:
   - the pool's panel LPs lost **and** did worse than holding → `close`;
   - out of range now → `rebalance` when frequent re-centering measured better per SOL-day,
     else `close`;
   - otherwise `keep`.
   Narrowing is recommended only when one width tercile was best in both halves. The tool
   says whether that holds today.
5. **Name the costs the data leaves out.** Bin-array rent (~0.07 SOL when a range needs a new
   bin array, not refunded) and transaction fees are not in `pnlSol`. On a 0.5 SOL position
   that rent alone is ~14%, so small positions in fresh ranges are worse than the table.
6. **Stop at evidence.** If the operator wants to act, the next step is a pre-registered paper study
   (`strategy-experiment`). Kaiba does not open LP positions.

## Thresholds and their source

| item | value | source |
|---|---|---|
| panel | 50 pools (40 SOL-quoted, TVL ≥ $20k; 10 USDC/USDT), up to 80 LPs each, frozen 2026-10-28 | `docs/research/claude-lp-lab-prereg-20261028.md` |
| window | positions created in the last 30 days, open ones marked at the API's current value | `deploy/research/lp_replay/lp_lab.py` |
| winsorising | each position's return capped at -100% / +300% | same file, `measure()` |
| finding rule | same sign in both halves AND ≥ 10 pools | `docs/research/claude-lp-lab-20261028.md` |
| tercile cuts | read from the day's result (were 27% / 95% time in range, 27 min / 7.2 h duration) | `data/labs/lp/result.json` |
| staleness | 36 h | `kaiba/learning/labs.py` `STALE_S` |
| daily job | `claude-lp-lab-daily.timer`, Meteora data API only, no RPC | `deploy/research/lp_replay/lp_lab.py daily` |

## What not to do

- Do not present a pool's APR, fee/TVL or `per_sol_day` as an expected return: per SOL-day
  is noisy for pools of very short positions.
- Do not treat the headline as unbiased: pools were chosen by fees on the freeze day, and
  every LP sampled had a position open then.
- Do not recommend narrowing a range unless the tool says the width finding held in both
  halves.
- Do not open, add to, re-center or close any LP position, and do not ask for keys or a
  wallet to do so. Any LP activity is the operator's decision.
