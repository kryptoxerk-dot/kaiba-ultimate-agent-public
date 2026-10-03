---
name: strategy-experiment
description: Take a hypothesis through the promotion gates.
version: 0.1.0
author: Kaiba contributors
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Learning, Experiments, Gates]
    related_skills: [trade-journaling, trade-intent, wallet-confluence, provider-budget-audit]
---

# Strategy experiment

## What this skill is for

Moving an idea from "I noticed something" to "this lane has capital" without lying to
ourselves on the way. The path is fixed: **propose → replay gate → shadow gate → canary →
promote**, with a versioned record at the end.

The structural rule, from PLAN §8 and the reflect profile: **the agent cannot edit the
gate.** The gate code and the envelope bounds are root-owned files Hermes can read and
not write. This is not distrust for its own sake; self-modifying agents that can approve
themselves have been documented producing exactly the results they were asked to produce.

## When to use it

- The nightly reflection produced a parameter idea worth testing.
- A lane's measured expectancy suggests a threshold is wrong.
- You want a new lane, or want an existing shadow lane promoted.
- `kaiba_performance()` shows a lane that looks profitable and you are about to say so.
- A playbook rule has accumulated enough hits to become a parameter.

## Procedure

1. **State the hypothesis as a falsifiable sentence with a number.** "Raising
   `confluence-5.min_entities` from 5 to 6 raises per-trade expectancy by more than the
   signal count it costs" — not "tighter confluence is better".
2. **Propose it.** `kaiba_propose_experiment(hypothesis, lane, diff)`. The `diff` is the
   exact parameter change. It is stored with status `proposed` and journalled. It is not
   applied, and that is by design.
3. **Replay gate — offline, point-in-time.** The variant runs against the same
   chronological candidate stream as the incumbent, using only information available at
   each decision time. It must clear **all** of:
   - PBO (probability of backtest overfitting) **< 0.5**
   - deflated Sharpe ratio positive after the multiple-testing correction
   - out-of-sample performance ≥ incumbent
   - maximum drawdown ≤ 1.1× incumbent
   - a minimum number of trades (small N is not a result)
4. **Shadow gate.** 2–4 weeks, or the configured trade count, with Thompson-sampled
   paper capital across at most three candidates at once. Same candidate stream as live,
   modelled fees and slippage. `kaiba_set_lane_mode(lane, "shadow")` puts a lane there.
5. **Attribution, before you call it edge** (below).
6. **Canary.** 10–25% of normal size, live, with automatic rollback on a breach.
   `kaiba_set_lane_mode(lane, "canary")` — and note that
   `bounds.max_lane_mode` is the ceiling; the tool refuses anything above it and tells
   you to ask the operator.
7. **Promote, with a versioned record.** Parameters, the gate results, the sample sizes,
   the date, and the rollback plan. `kaiba_journal_append("experiment", ...)`.
8. **Monitor and be willing to retire.** A promoted change that decays gets rolled back
   the same way it arrived. Retirement is not failure; an unretired dead rule is.

## Attribution before crediting a lane with edge

A lane that made money while the chain's native token rose 30% may have no edge at all.
Before any promotion, decompose the return:

- **Beta.** What did a passive hold of the chain's native asset do over the same window?
  Most memecoin lanes are long-beta by construction.
- **Regime.** Was the whole window one market condition? Per-regime performance is in the
  nightly rollup for this reason.
- **Selection.** Did the lane pick these tokens, or did a shared upstream filter pick
  them and the lane just happened to be switched on?
- **Costs.** Fees, tips, slippage and failed fills, priced at the real numbers
  ($0.001–0.01 per normal Solana swap; $0.02–0.10 on a viral landing).
- **Sample.** How many independent trades, not how many rows. Twenty entries on four
  tokens is four observations wearing a disguise.
- **Survivorship.** Skips are in the packet
  (`SKIP_OUTCOME_HORIZON_MS` = 24 h) precisely so hit rate is not computed only over
  taken trades.

If the honest answer is "we cannot separate the lane from the market", say so. The reflect
profile is explicit: an honest negative finding is worth more than an encouraging one.

## Thresholds and their source

| Gate | Threshold | Source |
|---|---|---|
| PBO | < 0.5 | PLAN §8 promotion gates |
| Deflated Sharpe | positive after multiple-testing correction | PLAN §8; `kaiba/learning/metrics.py` |
| Out-of-sample | ≥ incumbent | PLAN §8 |
| Max drawdown | ≤ 1.1× incumbent | PLAN §8 |
| Minimum trades | configured, non-trivial | PLAN §8 |
| Shadow duration | 2–4 weeks or N trades | PLAN §8 |
| Concurrent shadow candidates | ≤ 3, Thompson-sampled paper capital | PLAN §8 |
| Canary size | 10–25% of normal, auto-rollback | PLAN §8, §11 Phase 4 |
| Lane mode ceiling | `bounds.max_lane_mode` (currently `shadow`) | `config/risk.yaml`; `kaiba_set_lane_mode` enforces it |
| Size clamp | `bounds.max_size_pct_bankroll` = 5.0% | `kaiba_set_lane_param` clamps `size_pct_*` |
| Proposals per night | ≤ 2 | `MAX_PARAM_PROPOSALS` |
| Forbidden proposal keys | all of `EnvelopeBounds`, plus `mode`, `kill_switch`, `entries_paused`, `reduce_only`, `global_mode`, `bounds`, `wallet`, `bankroll_base_units` | `FORBIDDEN_PARAM_KEYS` in `kaiba/learning/reflect.py` |
| Evidence grades to respect | curve velocity **A**, bundle/sniper filters **A/C**, smart-money presence **A (modest)**, migration dump **A**, confluence **C**, time-of-day **C** | `docs/research/02-memecoin-edge-and-risk.md` |
| Retracted finding | social-link presence, OOS AUROC 0.46 | v5 of arXiv 2607.02823 |

## Failure modes

- **Optimising on the holdout.** Each re-use of the same out-of-sample window makes the
  deflated Sharpe less meaningful. Count the trials; that is what "deflated" deflates by.
- **Future leakage.** Using a token's later graduation status, or a wallet grade computed
  after the fact, in a point-in-time replay. Grades are versioned for this reason.
- **Inconclusive presented as improvement.** "Slightly better on 22 trades" is
  inconclusive. Say so.
- **Cherry-picked window.** A three-week sample that happens to start after a crash.
- **Parameter creep.** Five small unreviewed tweaks are one large unreviewed change.
- **Reward hacking.** Any path where the thing being measured can be adjusted by the
  thing being evaluated. This is why the gate is root-owned.
- **Hot-editing the funded executor.** Versioned release, reconciled activity, preserved
  protections, tested rollback. A winning trade is not a deployment permission.
- **Promoting on a good week.** Base rates are brutal enough that a week of noise looks
  like skill; 73.3% of wallets were net-positive in April 2026 and 65% of them made
  ≤ $500.

## What NOT to do

- **Do not edit the gates, the envelope bounds or the signer policy.** They are outside
  the agent's write scope deliberately, and attempting it is the failure mode the design
  anticipates.
- **Do not apply a parameter change directly** when it belongs in an experiment.
  `kaiba_propose_experiment` exists so the change has a record and a gate.
- **Do not promote a lane past `bounds.max_lane_mode`.** `kaiba_set_lane_mode` refuses,
  and the correct response is to ask the operator, not to route around it.
- **Do not credit a lane with edge before attribution.**
- **Do not add a trusted-copy wallet, a larger budget, a relaxed protection limit or a
  new financial permission through this path.** Those stay owner-controlled
  (master prompt §9).
- **Do not run more than three shadow candidates at once.** Paper capital is finite and
  so is the candidate stream.
- **Do not skip the versioned promotion record.** A change nobody can date or roll back
  is not a promotion, it is drift.

## Gate readiness and durable verdicts

- Inspect the installed replay contract before submitting a diff. A numeric `min_*` or `max_*` threshold must map to a feature retained on the original decision or its point-in-time signals. An arbitrary nested configuration, execution-time expiry, exit protocol or placeholder variable is not automatically replayable by a selection evaluator.
- Preserve the original proposal. If a diagnostic adapter normalizes its representation, label that input separately and do not pretend the original proposal or unsupported runtime parameter was successfully validated.
- Retain the exact returned verdict, including early refusals. Check whether the evaluator's `record=True` path actually saves every return; where it does not, use the returned `GateResult.record()` without changing `passed`, reasons or statistics. Zero stored rows prove no retained verdicts, not that evaluation was never attempted.
- Distinguish invalid specification, missing point-in-time features, zero labelled candidate trades, underpowered statistics and adverse performance. A readiness refusal is not a demonstrated losing strategy.
- Verify chain/mode scope before interpreting a replay; a routine that pools all lane records must not be presented as chain-specific live validation. Use a bounded isolated snapshot for scoped evaluation and retain the population and honest trial count.
- Collect candidate/control observations prospectively. Never relabel historical trades after seeing their outcomes merely to supply the shadow gate with a sample.
- Record gate/source hashes, exact output IDs and read-back evidence, and keep judgement separate from promotion. A passed test suite or a saved verdict is not proof of a profitable deployed change.

## Reading your own gate results

`kaiba_propose_experiment(...)` records a proposal. `kaiba_experiments(status=...)`
reads proposals back with their gate verdicts attached, which is how you find out
whether last week's idea survived the replay and shadow gates.

You cannot promote an experiment yourself and you cannot edit the gate. If a
proposal keeps failing, the useful move is a different hypothesis, not a louder
version of the same one.
