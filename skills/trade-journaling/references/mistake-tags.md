# Mistake tag vocabulary and rollup metrics

`MISTAKE_TAGS` in `kaiba.core.schemas` is a `frozenset`. A tag outside it is rejected by
the reflection validator, not repaired. Adding one is a schema change.

## The thirteen tags

| Tag | Definition | Typical evidence |
|---|---|---|
| `late_entry` | filled after the move was already paid for | price drift between signal event time and our fill exceeds the lane's limit |
| `early_entry` | entered before the setup confirmed | the trigger condition was met only after we were in |
| `ignored_invalidation` | the recorded `invalidation` fired and we stayed | the `Decision.invalidation` text plus the tape |
| `size_too_big` | thesis fine, fraction wrong | position above the lane band, or MAE deeper than the stop could absorb |
| `size_too_small` | under-sized against the lane's own ladder | score band justified more |
| `thesis_wrong` | the reasoning was wrong, execution was fine | outcome unrelated to fills |
| `execution_slippage` | fill materially worse than quote | `slippage_bps` on the trade record |
| `regime_misread` | setup fine, market context wrong | per-regime rollup |
| `data_error` | acted on wrong, stale or unknown-as-zero data | dossier `unknowns`, provider conflict |
| `held_through_migration` | held across a bonding-curve migration | 73% of migrations fall below 40% within 20 min |
| `no_protection` | `ACQUIRED_UNPROTECTED` at any point in the life of the position | `protection.set` missing on the bus |
| `chased_kol` | entered on a call rather than on evidence | `alpha.call` in the signal chain; 80% of KOL-promoted coins are down ≥70% in a week |
| `cluster_missed` | counted one entity's addresses as independent | entity roll-up after the fact |

## Journal kinds

| Kind | Use |
|---|---|
| `observation` | something seen: a dossier read, a provider fact, a wallet read |
| `lesson` | a generalisation with evidence, ≤ 280 characters |
| `experiment` | a proposal or its result |
| `change` | a parameter, mode or cohort change and why |
| `outcome` | a closed trade's narrative complement to the structured record |
| `correction` | an incident, a reconciliation, or a previous entry being superseded |

The journal is append-only and hash-chained. Corrections are new entries; nothing is
edited in place.

## Nightly rollups (PLAN §8)

- expectancy per lane and per mode
- profit factor
- Sharpe / Sortino
- maximum drawdown
- **Brier score** of stated `confidence` against outcomes — the calibration number
- per-tag mistake frequency
- per-regime performance
- skip outcomes over the following 24 hours

## Reflection output limits (`kaiba/learning/reflect.py`)

| Limit | Value |
|---|---|
| `MAX_LESSONS` | 5 |
| `MAX_LESSON_CHARS` | 280 |
| `MAX_PLAYBOOK_DELTAS` | 3 |
| `MAX_PARAM_PROPOSALS` | 2 |
| `MAX_THESIS_CHARS` | 240 |
| `SKIP_OUTCOME_HORIZON_MS` | 24 hours |

Parameter keys the reflection job may never propose (`FORBIDDEN_PARAM_KEYS`): every field
of `EnvelopeBounds`, plus `mode`, `kill_switch`, `entries_paused`, `reduce_only`,
`global_mode`, `bounds`, `wallet`, `bankroll_base_units`.

## A lesson that passes review

> Entries where bundler share exceeded 20% lost money in 7 of 9 cases this week,
> mean −34%. Tag: `cluster_missed`.

Generalises, cites the count and the denominator, carries a number, names a tag.

## A lesson that does not

> TOKEN_C was a bad trade and I should have been more careful.

No mechanism, no count, no threshold, no tag, and it names a single instance. The
reflection packet masks token identities precisely so this shape of "lesson" is hard to
write.
