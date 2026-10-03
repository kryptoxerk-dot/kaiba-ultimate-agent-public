---
name: trade-journaling
description: Journal every decision, including the skips.
version: 0.1.0
author: Kaiba contributors
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Learning, Journal, Discipline]
    related_skills: [trade-intent, strategy-experiment, incident-recovery, position-protection]
---

# Trade journaling

## What this skill is for

Writing the records the learning loop runs on. Two structured records — `Decision` and
`TradeOutcome` — plus an append-only, hash-chained journal of observations, lessons,
changes, experiments, outcomes and corrections.

The rule that makes the whole loop work, and the one most often skipped: **a decision to
stand aside is journalled with the same care as a trade.** A journal of winners teaches
nothing. Standing aside correctly and standing aside expensively look identical on a PnL
curve and completely different in the record.

## When to use it

- After every decision, including `action: skip`.
- After every closed position.
- After an incident (`incident-recovery` writes a `correction`).
- After a parameter or cohort change — `kaiba_set_lane_param`,
  `kaiba_set_lane_mode` and `kaiba_set_cohort` journal automatically, but the *reason*
  worth reading is the one you add.
- When you notice something generalisable that is not tied to one trade.

## Procedure

1. **Write the decision at decision time**, not after the outcome is known. A record
   written afterwards is a rationalisation with a timestamp.
2. **Fill the `Decision` fields** (below). `thesis`, `confidence`, `invalidation` and
   `blockers` are the ones the reflection job actually uses.
3. **State the invalidation before entering.** "What would make this wrong?" recorded up
   front is what makes `ignored_invalidation` detectable later.
4. **On close, write the `TradeOutcome`** with fees, slippage, MAE and MFE — not just
   PnL. A trade that made money after being 60% underwater is a different trade from one
   that never dipped.
5. **Tag mistakes from the fixed vocabulary only.** A tag outside `MISTAKE_TAGS` is
   rejected, not repaired.
6. **Write one lesson, and make it generalise.** ≤ 280 characters
   (`MAX_LESSON_CHARS`). "Bundler share above 20% preceded a dump in 7 of 9 observed
   cases" is a lesson; "bought TOKEN, it went down" is not.
7. **Append it.** `kaiba_journal_append(kind, body, subject)` where `kind` is one of
   `observation`, `lesson`, `experiment`, `change`, `outcome`, `correction`. The entry
   is hash-chained; you get back a sequence number and a hash prefix.
8. **Read before you write.** `kaiba_journal_read(limit, kind)` and
   `kaiba_performance(days, mode)` — do not re-learn a lesson that is already rule 14 in
   `kaiba_playbook()`.

## Decision record fields

From `kaiba.core.schemas.Decision`:

| Field | Notes |
|---|---|
| `decision_id`, `ts_ms` | identity and decision time |
| `lane`, `mode` | `mode` is the **effective** mode; a shadow decision is a paper decision |
| `chain`, `token` | normalised address |
| `action` | `enter`, `exit`, `scale_in`, `scale_out`, `hold`, **`skip`** |
| `thesis` | why, in one or two sentences |
| `confidence` | 0–1; scored against outcomes by the nightly Brier calculation |
| `signals` | the signal ids that fired |
| `dossier_grade` | the DYOR grade at decision time |
| `size_base_units`, `size_pct_bankroll` | integer base units; USD is `Decimal` |
| `expected_return_pct` | your estimate, recorded so it can be wrong in public |
| `invalidation` | what would make this wrong — mandatory in practice |
| `regime` | market context, so per-regime performance is computable |
| `blockers` | why not, on a skip |
| `params_version`, `model`, `trace_id` | reproducibility |

## Trade record fields

From `kaiba.core.schemas.TradeOutcome`: `trade_id`, `position_id`, `decision_id`, `lane`,
`mode`, `chain`, `token`, `opened_ms`, `closed_ms`, `hold_s`, `cost_native`,
`proceeds_native`, `pnl_native`, `pnl_pct`, `fees_native`, `slippage_bps`, `mae_pct`,
`mfe_pct`, `exit_reason`, `mistakes`, `lesson`, `params_version`.

All on-chain amounts are integer base units (`docs/CONTRACT.md` rule 1). Open inventory
is not a realised loss.

## The fixed mistake vocabulary

`MISTAKE_TAGS` in `kaiba.core.schemas` — thirteen tags, closed set:

| Tag | Use it when |
|---|---|
| `late_entry` | the move was already paid for before we filled |
| `early_entry` | the setup had not confirmed |
| `ignored_invalidation` | the recorded invalidation triggered and we stayed |
| `size_too_big` | correct thesis, wrong fraction |
| `size_too_small` | correct thesis, under-sized against its own lane band |
| `thesis_wrong` | the reasoning, not the execution, was the problem |
| `execution_slippage` | fill materially worse than quote |
| `regime_misread` | the setup was fine, the market was not |
| `data_error` | acted on a wrong, stale or unknown-as-zero number |
| `held_through_migration` | the one the `migration-fade` lane exists to avoid |
| `no_protection` | `ACQUIRED_UNPROTECTED` at any point |
| `chased_kol` | entered on a call rather than on evidence |
| `cluster_missed` | counted addresses as independent that were one entity |

A new tag is a schema change proposed through `strategy-experiment`, not an ad-hoc
string. The vocabulary is fixed so that per-tag frequencies mean something across months.

## Why skips are journalled

The nightly reflection packet carries every closed trade **and** every `skip` decision in
the window, with what the token subsequently did over the next 24 hours
(`SKIP_OUTCOME_HORIZON_MS`). Without skips:

- lane hit rate is computed only over the trades we took — pure survivorship;
- a filter that rejects every future winner looks perfect, because its rejections are
  invisible;
- confidence calibration has no negative examples;
- "we were right to stand aside" can never be distinguished from "we missed it".

So a skip record needs the same `thesis`, `confidence` and `blockers` an entry would
have. `blockers` on a skip is the field that tells the reflection job *which* rule cost
or saved money.

## Thresholds and their source

| Item | Value | Source |
|---|---|---|
| Lesson length | ≤ 280 characters | `MAX_LESSON_CHARS` in `kaiba/learning/reflect.py` |
| Lessons per night | ≤ 5 | `MAX_LESSONS` |
| Playbook deltas per night | ≤ 3 | `MAX_PLAYBOOK_DELTAS` |
| Parameter proposals per night | ≤ 2 | `MAX_PARAM_PROPOSALS` |
| Skip outcome horizon | 24 h | `SKIP_OUTCOME_HORIZON_MS` |
| Journal body cap | 4,000 characters | `kaiba_journal_append` |
| Journal kinds | `observation`, `lesson`, `experiment`, `change`, `outcome`, `correction` | `kaiba_journal_append` |
| Mistake tags | the 13 above, closed set | `MISTAKE_TAGS` |
| Rollup metrics | expectancy, profit factor, Sharpe/Sortino, max drawdown, Brier score of `confidence`, per-tag mistakes, per-regime performance | PLAN §8 |
| Playbook shape | numbered rules with hit/miss counters, appended and curated, never rewritten wholesale | PLAN §8 (ACE pattern) |

## Failure modes

- **Writing the decision after the outcome.** The record becomes a story about why we
  were always right.
- **Skipping the skips.** The most common and most expensive omission.
- **Diary entries instead of lessons.** Specific, unrepeatable, untestable.
- **Free-text mistake tags.** They break every per-tag frequency comparison.
- **Confidence inflation.** The Brier score is the only calibration we have; padding
  confidence destroys it.
- **PnL without fees, slippage, MAE and MFE.** A profitable-looking lane that never nets
  positive after costs is the standard way to fool yourself.
- **Realising open inventory as a loss** (or an unrealised gain as profit).
- **Rewriting the playbook wholesale.** It is append-and-curate; a rule that has not
  earned a hit in thirty days retires on its own.
- **Mode confusion.** A shadow record reported as a trade.

## Nightly reflection audit safeguards

- Build `mask(build_review(...)).packet` with one fixed 24-hour cutoff and verify its trade/skip counts against SQL. Group every skip by lane, mode, chain, blockers and outcome basis; do not sample only interesting rejections.
- Check the installed implementation: `build_review` may omit attribution, and `calibration_points` may include only decisions joined to closed trades. Report these limits rather than assuming the packet implements every intended metric.
- Separate skip-horizon maturity from coverage. `_skip_outcome` uses later realised trades, potentially from another lane or mode, not a fixed-24h price return; this proxy does not establish a filter hit rate. Last-24h skips generally have not completed their 24h horizon.
- For Robinhood benchmark attribution, inspect `native_price.price_source_chain` before declaring benchmarks absent: the installed alias reads ETH samples. Retain endpoint timestamps/distances, match return denominations, and never treat a tiny-sample OLS beta as reliable edge.
- Inspect `journal.append` before wrapping `apply_reflection` in a transaction. An implementation with its own unconditional `BEGIN IMMEDIATE` rejects an outer transaction. Use the supported autocommit connection, verify rollback after an error, and check exact run/lesson/rule targets before retrying to prevent duplicate writes. Do not edit journal or gate code to force the run through.
- Retain the masked packet, validated response, audit and read-back receipt under the active profile's reports directory, not expiring scratch. Verify the exact reflection run, journal entries, playbook IDs and journal hash chain before reporting completion.

## What NOT to do

- **Do not journal only the trades that closed green.**
- **Do not invent a mistake tag.**
- **Do not edit a past journal entry.** The journal is append-only and hash-chained;
  corrections are new entries of kind `correction`.
- **Do not write a lesson that names one token.** Generalise or do not write it.
- **Do not put provider prose, token names or social text into a lesson.** The reflection
  packet masks token identities on purpose — a model that sees a familiar ticker reasons
  from memory instead of from the factors.
- **Do not claim the agent "learned" something.** Journalling changes retrieval, rules
  and parameters. It does not retrain a model, and saying so is a false claim about the
  system.
- **Do not defer the journal until the end of a session.** Decision time is a field, and
  the value of a decision record decays with every minute you wait.
