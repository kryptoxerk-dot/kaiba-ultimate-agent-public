---
name: trade-intent
description: Form an entry by precedence, lane and size.
version: 0.1.0
author: Kaiba contributors
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Trading, Sizing, Risk]
    related_skills: [wallet-confluence, trusted-wallet-copy, gmgn-token-dyor, position-protection, trade-journaling]
---

# Trade intent

## What this skill is for

Turning "this looks good" into a sized, checked, journalled entry — or, far more often,
into a recorded stand-aside. The precedence chain below is the owner's, from the master
prompt §7, and its order is the point: cheap vetoes run before expensive work, and no
later step can undo an earlier rejection.

The deterministic services place the trade. This skill is the judgement that decides
whether one should exist and how large it may be.

## When to use it

- A lane produced an eligible signal (`wallet-confluence`, `trusted-wallet-copy`, or a
  shadow lane) and something has to decide.
- The operator asks for a manual entry on a specific token.
- You are explaining why a strong-looking candidate was skipped.
- You are about to scale into or out of an existing position.

## Procedure — the precedence chain

Run in this order. The first failure ends it.

1. **Permission.** `kaiba_status()`: `kill_switch`, `entries_paused`, `reduce_only`, the
   lane's **effective** mode (not its configured mode), and `today.halted` with
   `halt_reason`. `reduce_only` and a fired daily stop both permit exits and forbid
   entries.
2. **Financial.** Chain enabled, bankroll known and non-zero, gas reserve intact,
   per-token exposure headroom, per-entity correlation headroom, provider budget
   available (`kaiba_status()` returns `providers`; see `provider-budget-audit`).
3. **Required data.** `kaiba_token(address, chain)` fresh enough for the execution-
   critical fields. Missing required data is a stop, not a discount — `docs/CONTRACT.md`
   rule 2.
4. **Risk veto.** Any hard blocker in the dossier. Any `QUARANTINED` wallet in the
   evidence. Any unknown in a required safety field.
5. **Lane eligibility.** The lane's own parameters: entity count and window for
   `confluence-5`, delay and drift for `trusted-copy`, progress and bundle share for
   `curve-velocity`. The lane skill owns this step.
6. **Quote and size.** Executable quote, slippage inside `bounds.max_slippage_bps`
   (2,500), minimum output set, size from the ladder below and clamped to the envelope.
7. **Execution.** Reserve exposure, then submit. The intent is persisted before the send
   (`incident-recovery` explains why).
8. **Verified protection.** A filled buy without confirmed protection is
   `ACQUIRED_UNPROTECTED` and gets repaired immediately (`position-protection`).

Then, whatever happened: **journal it**. `kaiba_journal_append` and the `Decision`
record, including `action: skip`. Skips are the training data
(`trade-journaling`).

## The score-to-allocation ladder

Lane maxima come from `config/risk.yaml`; the score bands come from PLAN §6.2.

| Signal score band | Allocation |
|---|---|
| Weakest qualifying | 25% of the lane's `size_pct_max` |
| Moderate | 50% |
| Strong | 75% |
| Strongest | 100% |

| Lane | Band | Source |
|---|---|---|
| `confluence-5`, `trusted-copy` | 1.0% – 5.0% of bankroll | `config/risk.yaml`; PLAN §6.2 "confluence/copy 1–5%" |
| `curve-velocity`, `migration-fade`, `kol-fade` | 0.25% – 1.0% | `config/risk.yaml`; PLAN §6.2 "snipes 0.25–1%" |
| `sm-trenches` | 0.25% – 1.5% | `config/risk.yaml` |
| `listing-pop`, `pons-robinhood` | 0.5% – 2.0% | `config/risk.yaml` |

Hard ceilings the ladder may never exceed:

| Bound | Value | Owner |
|---|---|---|
| `max_size_pct_bankroll` | 5.0% | operator, root-owned; `kaiba_set_lane_param` clamps to it |
| `max_daily_loss_pct` | 10.0% envelope; PLAN §6.2 operating target 5% | operator |
| `max_slippage_bps` | 2,500 | operator |
| `max_exposure_pct` per token | 10.0% per chain | `config/risk.yaml` |
| `max_concurrent_positions` | `null` — no count cap, by owner mandate | `config/risk.yaml` |
| `max_lane_mode` | `shadow` until the operator raises it | `bounds`; `kaiba_set_lane_mode` refuses above it |

Fixed fraction, not Kelly: at sub-5% win rates Kelly is dominated by estimation error
(`docs/research/02-memecoin-edge-and-risk.md`, risk controls). Canary sizes on Solana are
5,000,000–20,000,000 lamports (0.005–0.02 SOL) in the shipped `config/risk.yaml`.

## Thresholds and base rates you are sizing against

| Fact | Number | Source |
|---|---|---|
| Graduation rate | 0.26% (Jun 2026), 0.63% (Sep 2025) | The Block/CoinLaw; arXiv 2602.14860 |
| Post-migration collapse | 73% below 40% of migration price within 20 min | MemeTrans arXiv 2602.13480 |
| Rug candidates | 76% of new tokens, H1 2025 | arXiv 2603.24625 |
| Sandwich risk | 93% of sandwiches are multi-slot "wide" attacks by malicious validators | research 02 → use leader-aware routing / anti-MEV |
| Curve snipe slippage | 10–25% is normal on bonding curves | research 02 |
| Normal Solana swap cost | $0.001–0.01; viral landing $0.02–0.10; sniper tips 0.001–0.01 SOL | `docs/research/08-gating-and-budget-verification.md` |

## Tools you actually have

`kaiba_status`, `kaiba_token`, `kaiba_wallet`, `kaiba_signals`, `kaiba_positions`,
`kaiba_performance` to decide; `kaiba_set_lane_mode`, `kaiba_set_lane_param`,
`kaiba_pause`, `kaiba_resume`, `kaiba_reduce_only`, `kaiba_request_exit`,
`kaiba_set_cohort` to act; `kaiba_journal_append`, `kaiba_propose_experiment` to learn.

There is no order-submission tool on this surface today. Entries are produced by the
deterministic planner from lane signals and the risk envelope; your levers over an entry
are the lane's mode and parameters, the cohorts, the pause switches, and the journal.
Say "the lane is in shadow, so this was recorded and not bought" rather than claiming a
fill you cannot produce.

## Failure modes

- **Running the chain out of order.** Sizing a candidate before checking the kill switch
  wastes budget and, worse, builds the habit of treating vetoes as advisory.
- **Numeric authorisation missing read as unlimited.** "No configured maximum" is not
  infinity. A missing bound is a stop.
- **Stale quote.** A quote that predates the decision by more than seconds on a new
  launch is fiction. Re-check immediately before submission.
- **Exposure double-counted.** Two lanes firing on one token must share the per-token cap.
  Reserve before submit, not after fill.
- **Correlated sizing.** Five positions in one narrative during one rally is one position
  with five tickets. The per-entity and per-token caps are the only controls we have for
  SOL-beta correlation.
- **Confidence not calibrated.** `confidence_p` on the `Decision` is scored against
  outcomes by the nightly job. Inflating it corrupts the only calibration signal we have.
- **Shadow mistaken for live.** A lane in `shadow` produces a paper record. Report it as
  such.

## What NOT to do

- **Do not size outside the envelope.** `kaiba_set_lane_param` clamps `size_pct_*` to
  `bounds.max_size_pct_bankroll` and `kaiba_set_lane_mode` refuses a mode above
  `bounds.max_lane_mode`. Attempting to work around either is out of bounds; ask the operator to
  raise the ceiling instead.
- **Do not let a later step rescue an earlier rejection.** Five entities do not clear a
  blocker; a great narrative does not clear a paused lane.
- **Do not repair a malformed decision into a live buy.** Reject ambiguous, non-finite or
  wrong-chain values.
- **Do not enter without protection being verified afterwards.** An entry is not finished
  at the fill.
- **Do not add to a losing position because the thesis "is still valid".** Record the
  invalidation up front on the `Decision` and honour it; `ignored_invalidation` is in the
  mistake vocabulary for a reason.
- **Do not skip the journal on a stand-aside.** An unrecorded skip is a decision the
  system cannot learn from.

## Submitting an intent

`kaiba_submit_intent(chain, token, lane, size_base_units, thesis)` asks for an
entry. It does not place an order. The intent goes back through the risk gate, the
dossier blockers and the signer policy, exactly as an automatic signal would, and
a lane in shadow mode produces a paper fill rather than a live one.

The call returns `ok: false` with a reason whenever anything refuses. Read the
reason and fix the cause; do not resubmit the same intent hoping for a different
answer.
