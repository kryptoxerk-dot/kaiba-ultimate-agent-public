---
name: signal-normalization
description: Turn raw feeds into deduped Signal records.
version: 0.1.0
author: Kaiba contributors
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Intelligence, Signals, Ingest]
    related_skills: [alpha-radar, wallet-confluence, entity-clustering, trade-intent]
---

# Signal normalization

## What this skill is for

The boundary between "something happened on a feed" and "a lane has evidence". A
`Signal` (`kaiba.core.schemas.Signal`) is a typed record with a lane, a chain, a token, a
strength, reasons, **wallets and entities as separate lists**, a window and a payload.
Everything upstream of it is untyped provider output; everything downstream of it is a
decision.

Getting this layer wrong is how the same buy gets counted three times, how a backfill
replays as live alpha, and how a token name becomes an instruction.

## When to use it

- When adding or debugging a feed (PumpPortal, Helius, GMGN, DexScreener, Alchemy,
  Telethon, RSS, `x_search`).
- When a lane's wallet count looks too good and you suspect double counting.
- When `kaiba_signals()` shows signals whose `wallets` and `entities` lists are the same
  length on every row — that usually means entity resolution did not run.
- When an event arrives with a timestamp far from now and you need to decide whether it
  is a backfill.
- Before proposing any change to a lane's window or minimum-buy parameters.

## Procedure

1. **Decode the event, do not trust its label.** A token arriving in a wallet is not a
   buy. Decode the swap: program, route, side, amounts in base units, fee payer, the
   wallet whose balance actually changed. `docs/CONTRACT.md` rule 1 — on-chain amounts
   are integer base units, USD is `Decimal`, floats never touch money.
2. **Stamp two times.** Observation time (when we saw it) and event time (when it
   happened on chain). Every window in the system is an event-time window; latency
   between the two is measured, not assumed.
3. **Resolve identities.** Token to a `chain:address` key via `normalize_address`
   (EVM lowercased, Solana case-sensitive). Wallet to its cohort and grade, and — the
   part that matters — to its `entity_id` from `entity-clustering`.
4. **Deduplicate.** Emit through the event bus with a `dedupe_key`; `emit_once` derives
   one from the payload. The same swap seen on a websocket and again on a webhook is one
   event. A duplicate returns `None`, which is the correct outcome, not an error.
5. **Drop what is not evidence.** Transfers, airdrops, dust, internal moves between two
   wallets of the same entity, repeated buys by one wallet, and replayed or backfilled
   events older than the lane's `max_signal_age_s` (30 s for `confluence-5`).
6. **Aggregate into a `Signal`.** Fill `wallets` with the distinct addresses and
   `entities` with the distinct entity ids. When the two lists differ in length, the
   shorter one is the truth for any count-based lane.
7. **Score the strength.** A decay-weighted score over the lane's window, with grade
   points as the weight (`GRADE_POINTS` in `kaiba/execution/lanes.py`: A 4, B 3, C 2,
   D 1, UNSCORED 0, QUARANTINED −1). Strength is comparative, not a probability.
8. **Verify what landed.** `kaiba_signals(limit, lane)` returns the stored rows with
   their reasons, wallets and entities; `kaiba_events(kinds=["signal.fired"])` shows the
   bus side. If you cannot see it in both, the pipeline dropped it.
9. **Journal systematic gaps.** `kaiba_journal_append("observation", ...)` when a feed
   is systematically late, duplicated or silent — that is a provider fact the reflection
   job should see.

## Thresholds and their source

| Parameter | Value | Source |
|---|---|---|
| `confluence-5` window | 120 s event time, `max_signal_age_s` 30 | `config/risk.yaml`; PLAN §6.1 |
| `confluence-5` minimum buy | $50 | `config/risk.yaml` (dust filter) |
| `pons-robinhood` minimum buy | $25, `max_age_s` 1,800 | `kaiba/execution/lanes.py` `DEFAULT_PARAMS` |
| `sm-trenches` window | 300 s | `DEFAULT_PARAMS` |
| `kol-fade` call staleness | `max_call_age_s` 300 | `DEFAULT_PARAMS`; KOL value decays in minutes |
| Co-occurrence needs | ≥ 3 distinct tokens before it is an edge | `COOCCUR_MIN_TOKENS`; Kamat arXiv 2607.02795 |
| Lead-lag copier window | ≤ 30 s, ≥ 5 tokens | `LEAD_LAG_*` in `cluster.py` |
| Grade weights | A 4 / B 3 / C 2 / D 1 / UNSCORED 0 / QUARANTINED −1 | `GRADE_POINTS` |
| Alpha event kinds | `alpha.signal`, `alpha.boost`, `alpha.cto`, `alpha.meta`, `alpha.call`, `alpha.news`, `alpha.listing` | `EventKind` |
| Text handling | scrub + truncate to 400 chars, ≤ 50 rows per list | `MAX_TEXT`, `MAX_ROWS` in `kaiba/mcp/server.py` |

## Why a single-token co-occurrence is noise

Two wallets buying the same token within the same minute feels like a signal. It is not,
and the arithmetic is unforgiving.

A popular launch draws hundreds of buyers in its first minutes. Pairs grow with the
square of the buyer count, so in a 300-buyer launch there are roughly 45,000 wallet pairs
that "co-bought within 60 seconds". Every one of them looks exactly like coordination if
you only ever look at one token. The published sniper-ring work found 1,012 persistent
rings across 166,000 launches precisely by requiring repetition across launches, not
within one (arXiv 2607.02795).

So the rule: **co-occurrence becomes evidence at ≥ 3 distinct tokens**
(`COOCCUR_MIN_TOKENS`), with confidence ramping from 0.5 to a ceiling of 0.8 by eight
tokens. On a single token, a co-buy is context — it belongs in the payload, it may raise
strength slightly, and it must never create an entity edge or count as independent
confirmation.

The same logic caps group sizes: `SAME_SLOT_MAX_GROUP = 50`. Above fifty buyers in one
slot you are looking at a stampede, and pairing them all produces tens of thousands of
false edges.

## Failure modes

- **Double counting across feeds.** PumpPortal, Helius and GMGN will all report the same
  swap. Without a `dedupe_key` the confluence count triples.
- **Backfill replayed as live.** A webhook redelivery or a restart cursor replaying an
  hour of history will fire every lane at once. Event-time age checks are the guard.
- **Transfer read as a buy.** Airdrops, bot dust and internal entity moves all increase a
  balance.
- **One wallet counted five times.** Repeated buys by a single address are one wallet,
  and five addresses in one entity are one entity.
- **Wall-clock windows.** Using observation time makes the window elastic under provider
  latency, which is exactly when you least want it to be.
- **Float money.** A price stored as a float and multiplied through a size calculation is
  a reconciliation bug waiting to happen.
- **Provider prose reaching a decision.** Token names and social text are scrubbed and
  truncated on the way out of the MCP server; anything you fetch yourself is not.
- **Silent feeds.** A websocket that stops delivering looks identical to a quiet market.
  Track per-feed heartbeat and emit `provider.error`.

## What NOT to do

- **Do not create a `Signal` without an `entities` list.** A count-based lane reading
  `wallets` because `entities` was empty is the independence bug.
- **Do not repair a malformed provider payload into a usable event.** Reject it and emit
  `provider.error`. Guessing the missing field is how a truncated message becomes a buy.
- **Do not widen a window to get more signals.** Emptiness is information; the lane
  parameters go through `kaiba_propose_experiment` and the gates.
- **Do not let a feed's own "smart money" flag substitute for our grade.** It is a
  reason string and a payload field, not a wallet score.
- **Do not emit a signal for a token whose dossier has a hard blocker.** Filter early; a
  blocked candidate that keeps firing wastes budget and attention.
- **Do not treat a single-token co-buy as confluence.** See above.
