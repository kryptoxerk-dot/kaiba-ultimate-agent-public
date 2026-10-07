---
name: incident-recovery
description: Recover from UNKNOWN sends, bans and halts.
version: 0.1.0
author: Kaiba contributors
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Operations, Recovery, Reconciliation]
    related_skills: [position-protection, provider-budget-audit, trade-journaling, trade-intent]
---

# Incident recovery

## What this skill is for

The four situations where doing the obvious thing costs money: an ambiguous send, a
banned provider, an unprotected position, and a fired daily loss stop. Each has a
required response that is not "retry".

The rule underneath all of them, from the owner's mandate: **an uncertain timeout is not
proof of failure.** Reconcile before you act.

## When to use it

- An order sits in `OrderState.UNKNOWN`, or a submit call timed out.
- The process restarted while positions were open.
- `kaiba_status()` shows a provider `banned: true` or a `family_bans` entry.
- A position shows `protected: false`.
- `today.halted` is true with a `halt_reason`.
- Two records disagree: chain says filled, journal says submitted.

## Order states

`planned → reserved → submitting → submitted → {partial, filled, failed, expired,
cancelled}` — plus `unknown`, which is not a stage but an admission.

| State | Meaning | Action |
|---|---|---|
| `reserved` | exposure booked, nothing sent | release the reservation if abandoned |
| `submitting` | in flight, no provider id yet | the dangerous one — treat as `unknown` after the timeout |
| `submitted` | provider accepted, no terminal result | poll, do not resend |
| `partial` | some quantity filled | protect what we own; decide on the remainder |
| `unknown` | we cannot say whether it landed | **reconcile, never blindly resubmit** |
| `expired` / `failed` | terminal, nothing owned | release exposure, journal, done |

## Procedure — an ambiguous send

1. **Stop.** Do not resubmit. A second financial action taken because the first was
   unclear is how one position becomes two at twice the intended size.
2. **Pause entries** if the ambiguity could repeat: `kaiba_pause("reconciling an
   UNKNOWN order")`. Exits keep running.
3. **Reconcile from chain plus journal.** Three sources, in this order of authority:
   (a) the chain — did a transaction with our signature land, and what did it do;
   (b) the provider — `order get` / the venue's order lookup by our client id;
   (c) our journal and `orders` table — what we intended and reserved.
   `kaiba_events(kinds=["order.submitted","order.filled","order.failed"])` and
   `kaiba_journal_read()` give the local side.
4. **Resolve the state from evidence, not from elapsed time.** A fill found on chain
   means we own the position, whatever our record said. An approval or protection error
   after a filled buy cannot erase ownership or suppress position tracking.
5. **Protect first, reason later.** If reconciliation shows we own something,
   `kaiba_positions()` should show it; if `protected` is false, repair immediately; if repair
   fails, tell the operator (position, chain, token, what is wrong, the numbers) and ask whether to sell. Never sell on your own (owner rule 2026-10-06: `kaiba_request_exit` needs `owner_request`).
6. **Only then decide about the original intent.** If the send definitively failed and
   the thesis still holds and the data is fresh, it is a *new* decision with a new
   record — not a retry.
7. **Journal the incident.** `kaiba_journal_append("correction", ...)` with what was
   ambiguous, what the chain said, and what we did. Tag `data_error` or
   `execution_slippage` in the trade record if it closed.
8. **Resume deliberately.** `kaiba_resume("reconciled; N orders resolved")`.

## Procedure — a banned provider

1. **Read the limiter.** `kaiba_status()` returns `providers` with `credit`, `capacity`,
   `inflight`, `penalty_level`, `spent_today`, `daily_cap`, `banned` and `family_bans`
   (per-endpoint bans with `banned_until_ms`).
2. **Respect the cooldown.** Never retry into a 429, and never rotate keys or IPs to
   evade a restriction. GMGN support is explicit that an upgrade does **not** clear an
   active IP lock, and that retries are what trigger the lock in the first place.
3. **Reprioritise, do not queue harder.** Protection and unresolved orders outrank
   research and new entries for whatever budget remains. If the remaining budget cannot
   cover protection, `kaiba_pause` entries.
4. **Fail over if a lane exists.** GMGN and the direct lane share the planner and the
   journal; a chain-level outage on one is not an outage of the system.
5. **Journal it as a provider fact** so `provider-budget-audit` and the nightly job see
   the frequency, not just the incident.

## Procedure — an unprotected position

1. `kaiba_positions()` → `protected: false` is `ACQUIRED_UNPROTECTED`.
2. Attempt the bounded repair: re-attach venue condition orders, or hand the position to
   the watchdog with its ladder.
3. If repair fails, tell the operator (position, chain, token, what is wrong, the numbers) and ask whether to sell. Never sell on your own (owner rule 2026-10-06: `kaiba_request_exit` needs `owner_request`). Say it is urgent: an unprotected
   memecoin position is an unbounded loss against a 92.2% dump base rate.
4. Journal with the `no_protection` mistake tag.

## Procedure — the daily loss stop fired

1. `kaiba_status()` shows `today.halted` with `halt_reason`. Entries are stopped; exits
   and protection are not.
2. **Do not re-arm by editing risk.** The stop is per-chain
   (`daily_loss_stop_base_units`) with `bounds.max_daily_loss_pct` at 10% as the operator
   envelope and 5% of bankroll as the operating target from PLAN §6.2. Raising it to keep
   trading is the exact behaviour the envelope exists to prevent.
3. **Work out why.** `kaiba_performance(days=1)` by lane and mode; `kaiba_journal_read()`
   for the decisions. One bad lane, one bad token, or a market-wide move are three
   different answers with three different responses.
4. **Consider `kaiba_reduce_only(True, reason)`** if open positions are the problem
   rather than new entries.
5. **Report to the operator.** The SOUL file lists a fired daily loss stop as something he is
   told about, unprompted.
6. **Journal a lesson, not a diary entry.** What would have prevented it.

## Thresholds and their source

| Item | Value | Source |
|---|---|---|
| Order states incl. `UNKNOWN` | `planned reserved submitting submitted partial filled failed expired unknown cancelled` | `kaiba.core.schemas.OrderState`; master prompt §8 |
| Never blindly resubmit | mandatory | master prompt §8; PLAN §6.3 |
| Reserve exposure before submit | mandatory | master prompt §8 |
| GMGN rate limit | leaky bucket 20/20 since 2026-05-13; sustained RPS = 20 ÷ weight | `docs/research/04-data-sources.md` |
| GMGN IP lock | an upgrade does not clear an active lock; disable retries | GMGN support, research 04 |
| GMGN wallet binding | `TRADE_WALLET_MISMATCH` (403) when `--from` is not bound to the key | `docs/research/08-gating-and-budget-verification.md` |
| Daily loss stop | 5% of bankroll operating target; `bounds.max_daily_loss_pct` 10% ceiling | PLAN §6.2; `config/risk.yaml` |
| Dump base rate justifying urgency | 92.2% of tokens with ≥30 swaps show a dump event | arXiv 2602.14860 |
| Mistake tags available | `data_error`, `no_protection`, `execution_slippage`, `ignored_invalidation`, … | `MISTAKE_TAGS` in `kaiba.core.schemas` |

## Failure modes

- **Retry-on-timeout.** The classic. Duplicated position, duplicated exposure, and a
  reconciliation problem that now spans two orders.
- **Trusting elapsed time.** "It has been two minutes, it must have failed" is not
  evidence. Slow chains and slow providers exist.
- **Treating open inventory as a realised loss.** It is not, and reporting it that way
  corrupts the performance numbers.
- **Reconciling from the journal alone.** The journal records intent; the chain records
  truth. Where they differ, the chain wins.
- **Cancelling protection during reconciliation.** Never leave a held position naked
  while you tidy the records.
- **Key or IP rotation to escape a ban.** Prohibited, and it makes the lock worse.
- **Restart amnesia.** Trailing high-water marks and `tp_done` rungs must be reloaded,
  not recomputed from the current price.
- **Silent recovery.** An incident nobody journalled will happen again and nobody will
  know it is the second time.

## What NOT to do

- **Do not resubmit an `UNKNOWN` order.** Reconcile.
- **Do not repair a truncated or malformed response into an action.** Reject it.
- **Do not raise a risk limit to clear a halt.** Diagnose it.
- **Do not rotate keys or IPs around a provider ban.**
- **Do not leave an `ACQUIRED_UNPROTECTED` position while investigating.**
- **Do not resolve an incident without a journal entry** naming what was ambiguous and
  what the chain said.
- **Do not report an incident as handled while an order is still in a non-terminal
  state.** Say which orders remain open.
