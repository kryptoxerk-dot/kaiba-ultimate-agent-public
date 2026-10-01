---
name: position-protection
description: Protect every position server-side and locally.
version: 0.1.0
author: Kaiba contributors
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Trading, Protection, Exits]
    related_skills: [trade-intent, incident-recovery, trusted-wallet-copy, trade-journaling]
---

# Position protection

## What this skill is for

Making sure every open position has working exits, and that those exits survive us being
offline. The watchdog is a deterministic service in `kaiba/execution/`, polling every
5 seconds; the parameters live in `config/risk.yaml` under `protection:`. This skill is
how you read the protection state, what the four protections mean, and what to do when a
position does not have them.

The design rule that justifies the whole thing: **a cron prompt is never a stop-loss**
(PLAN §3). Protection is code, and where the venue supports it, protection is code
running on the venue's side.

## When to use it

- `kaiba_positions()` shows `protected` false, or an `ACQUIRED_UNPROTECTED` event appears
  on the bus.
- A position just opened and you are confirming the exits attached.
- Liquidity on a held token dropped sharply.
- the operator asks what is protecting a position, or asks you to close one.
- A restart happened while positions were open (`incident-recovery` runs first).

## Procedure

1. **List the book.** `kaiba_positions()` returns each position's chain, token, lane,
   mode, quantity, cost and `protected` flag, plus `tp_done` — which rungs have already
   fired.
2. **Confirm protection exists on the venue, not only in our intent.** An entry response
   is not proof that a protective order exists. Verify the protection identifiers, their
   status, their quantity and their persistence. `kaiba_events(kinds=["protection.set"])`
   shows what the service recorded.
3. **Treat `protected: false` as an incident.** That is the `ACQUIRED_UNPROTECTED` state.
   The bounded response is to repair immediately, and if repair fails, to exit:
   `kaiba_request_exit(position_id, pct=100, reason="unprotected, repair failed")`.
4. **Check the four protections are all in place** (below). A fixed stop without a
   trailing rung leaves the upside unmanaged; a TP ladder without a stop leaves the
   downside unmanaged.
5. **Check the ownership model.** Native venue orders and our watchdog must not both
   sell the same quantity. Every rung has one owner; a cancel/replace precedes any
   change. If you see two exits for one quantity, stop and journal it rather than
   improvising.
6. **Watch the rug monitor.** A single-interval liquidity drop of 40% forces an exit.
   This runs without you.
7. **Exit through the service.** `kaiba_request_exit(position_id, pct, reason)` asks the
   protection service to close a percentage. Exits are always permitted — they work under
   `reduce_only`, under `entries_paused`, and after the daily stop has fired.
8. **Journal the exit and why.** Exit reason, rung, realised PnL and any mistake tags
   (`trade-journaling`). `no_protection` is in the fixed vocabulary.

## The four protections

| Protection | What it does | Default |
|---|---|---|
| **Fixed stop loss** | Hard exit below entry, before TP1 has moved anything | `stop_loss_bps: 3000` (−30%) |
| **Take profit** | Ladder of partial exits at multiples of entry | `[2.0×, 50%] [5.0×, 25%] [10.0×, 15%]` |
| **Trailing stop loss** | High-water mark with a callback distance that tightens as the multiple rises | `[2×, 3000 bps] [5×, 2500] [10×, 2000] [25×, 1500] [100×, 1000]` |
| **Trailing take profit** | Rides a run instead of capping it at the last rung; the tightening trail above is its implementation | same table |

Plus two safety nets: `emergency_loss_bps: 5000` (−50%, an unconditional exit) and the
rug monitor at `rug_liquidity_drop_pct: 40`.

**The TP ladder** sells 50% at 2× — the community "recover principal" convention, which
has no published backtest (research 02) and is therefore a parameter under measurement,
not a law. Percentages are of the **remaining** quantity at each rung, and `tp_done`
records which rungs have fired so a restart does not sell a rung twice.

**The trailing ratchet** tightens with the multiple: 30% callback at 2×, 10% at 100×.
The logic is that a position up 100× has convexity worth protecting harder than a
position up 2×, where normal volatility would stop you out for nothing.

**The breakeven lock** (`breakeven_after_tp1: true`) moves the stop to entry once TP1
fires. After the first rung, principal is off the table and the remainder is playing with
realised profit. This is the single cheapest protection in the table.

**Anti-wick verification** (`anti_wick_min_ratio: 0.7`) is the one people remove and
regret. A take profit triggers on a *price*, but we sell into a *quote*. On thin
memecoin liquidity a one-block wick prints a 2× that no one could actually sell into. So
before honouring a TP, the watchdog re-quotes for the real executable value and holds the
rung unless the quote is at least 70% of the target. Selling the whole rung into a wick
that has already gone is how a take profit becomes a loss.

## Server-side first

GMGN supports condition orders on the swap call: `profit_stop`, `loss_stop`,
`profit_stop_trace` and `loss_stop_trace`, parameterised with `price_scale`, `sell_ratio`
and `drawdown_rate` (`docs/research/04-data-sources.md`). `use_provider_orders: true` in
`config/risk.yaml` means we attach them at entry.

The reason is not convenience. **Protection carried server-side survives our downtime.**
If the VPS reboots, the model quota runs out, OAuth expires, or the watchdog crashes, a
GMGN condition order is still sitting on GMGN's side. Our 5-second watchdog is the
independent second layer, not the only layer — and it exists because the venue's
semantics must be verified rather than assumed, and because the direct lane has no venue
to hold orders for it.

Both layers, one ownership model, tested cancel/replace. That is the whole design.

## Thresholds and their source

| Parameter | Value | Source |
|---|---|---|
| `poll_interval_s` | 5 | `config/risk.yaml` |
| `stop_loss_bps` | 3,000 (−30%) | `config/risk.yaml` |
| `tp_ladder` | 2× → 50%, 5× → 25%, 10× → 15% | `config/risk.yaml`; community convention, no published backtest (research 02) |
| `trailing` | 2×→3000, 5×→2500, 10×→2000, 25×→1500, 100×→1000 bps | `config/risk.yaml` |
| `breakeven_after_tp1` | true | `config/risk.yaml` |
| `anti_wick_min_ratio` | 0.7 of target, executable quote | `config/risk.yaml` |
| `rug_liquidity_drop_pct` | 40% in one interval | `config/risk.yaml` |
| `emergency_loss_bps` | 5,000 (−50%) | `config/risk.yaml` |
| `use_provider_orders` | true | `config/risk.yaml`; PLAN §6.3, upgrade 4 |
| Why exits are urgent | 73% of migrated tokens below 40% of migration price within 20 min; 92.2% of tokens with ≥30 swaps show a dump | MemeTrans arXiv 2602.13480; arXiv 2602.14860 |
| Anti-MEV cost | GMGN anti-MEV needs fee ≥ 0.002 SOL; router allows 1 call / 5 s | research 02 |

## Failure modes

- **Assuming an entry response means protection exists.** Verify identifiers and status.
- **Double-sell.** Native order and watchdog both firing on one rung. One owner per
  quantity; cancel before replace.
- **Selling a rung twice after a restart.** `tp_done` is the guard; reconcile before
  acting (`incident-recovery`).
- **Wick-triggered TP.** Prevented by the 70% executable-quote check. Do not disable it
  because a rung "should have filled".
- **Trailing state lost on restart.** The high-water mark must be persisted, not
  recomputed from current price — recomputing resets the trail to a worse level.
- **Stop that cannot fill.** A stop is an instruction, not a guarantee of liquidity, a
  fill, or a particular loss. On a rug there is nothing on the other side.
- **Protection cancelled by a partial exit.** Reducing the position must resize the
  remaining protective orders, not orphan them.
- **Fee blindness.** Exits cost fees and tips too; a rung that nets negative after costs
  is not a take profit.

## What NOT to do

- **Do not hold a position through a migration.** `migration-fade` sets
  `never_hold_through_migration: true`, and `held_through_migration` is in the mistake
  vocabulary.
- **Do not widen a stop to avoid realising a loss.** That is the loss plus a worse one.
- **Do not disable anti-wick verification**, the rug monitor, or the breakeven lock to
  "let a winner run". Propose a parameter change through `strategy-experiment` instead.
- **Do not leave a position unprotected while you investigate.** Repair or exit first,
  investigate after.
- **Do not rely on the watchdog alone** where the venue can hold the order. Our uptime is
  not a risk control.
- **Do not treat a paused or reduce-only state as a reason to delay an exit.** Exits are
  always permitted, deliberately.

## Arming and repairing protection

`kaiba_set_protection(position_id, stop_loss_bps=..., trail_bps=...)` asks the
protection service to arm or repair a position. Values are clamped to 1..9999 bps.

Use it the moment `kaiba_positions` shows an open position with `protected: false`.
An unprotected filled position is the single most expensive state this system can
be in, and it is the one case where acting immediately beats investigating first.
