# Runbook: arming a lane

**Question this answers:** how does a lane move shadow → canary → live, what evidence is
required at each step, and how do I reverse it?

**Why this document is strict.** The previous repo never traded because it could not
decide it was ready. The one before it would have traded anything. Both failure modes
come from the same thing: no written bar. This is the bar.

Arming is per **lane**, per **chain**. "The agent is live" is not a state that exists.

---

## The modes

| Mode | What happens | Money at risk |
|---|---|---|
| `off` | lane does not evaluate | none |
| `shadow` | evaluates, records signals and paper fills, never sends | none |
| `canary` | sends real orders at reduced size | capped, small |
| `live` | sends real orders at envelope size | real |

`effective_mode()` takes the **minimum** of the lane's mode, `global_mode`, and
`bounds.max_lane_mode`. So there are three separate places to raise, and any one of them
can hold a lane back. That is intentional: the ceiling is the operator's, the global is
the day's posture, the lane's is the agent's.

---

## shadow → canary

### Evidence required

All of these, in writing, in the journal:

1. **≥ 7 consecutive days in shadow** on the same parameter version. A parameter change
   resets the clock. `params_version` in the `trades` table is what you count against.
2. **≥ 30 shadow trades** on that lane/chain pair. Below that you are reading noise.
3. **Positive expectancy after modelled costs** — fees, priority fees, and slippage as
   the paper broker modelled them, not gross. One number, written down.
4. **Max drawdown inside the daily loss stop** across the whole shadow window. If the
   worst shadow day would have tripped the stop, the size is wrong, not the lane.
5. **A protection record.** Every shadow position reached a terminal state through the
   ladder. Any that "just ended" mean protection was not actually exercised.
6. **A rehearsed exit.** You have run
   [incident-position-unprotected.md](incident-position-unprotected.md) against a shadow
   position and it worked.

### Pre-flight

```sh
sudo /opt/kaiba/current/deploy/preflight.py
```

Green, including `signer refuses a withdrawal`. That check is not a formality; it is the
only hard gate in the system and it is verified against the running signer.

### Fund it

Canary size is small enough that losing all of it teaches you something and costs you
nothing you will miss. Per PLAN §12 capital tiers, and set in base units:

```yaml
# /etc/kaiba/config/risk.yaml
chains:
  sol:
    bankroll_base_units: 2000000000       # 2 SOL
    max_position_base_units: 20000000     # 0.02 SOL
```

### Flip

```sh
# the lane
sudo -u kaiba-core /opt/kaiba/venv/bin/python -m kaiba.cli.main risk lane confluence-5 --mode canary
# the ceiling, if it is still shadow
sudo -u kaiba-core /opt/kaiba/venv/bin/python -m kaiba.cli.main risk bounds --max-lane-mode canary
# the global posture
sudo -u kaiba-core /opt/kaiba/venv/bin/python -m kaiba.cli.main risk global --mode canary
```

`bounds` is operator-owned: `save_risk()` re-reads it from disk on every write precisely
so the agent cannot raise its own ceiling. Editing it is a deliberate human act. Do it
with an editor, on a stopped engine, or through that command — never by letting the
agent do it.

### Then watch

First live order: watch it end to end. Not the dashboard — the log.

```sh
sudo journalctl -u kaiba-engine -u kaiba-protection -u kaiba-signer -f
```

You are looking for: intent → policy pass → signed → submitted → confirmed → position
created → **protection attached**. If protection does not attach on the first one, stop
and go back to shadow.

### Canary acceptance

`LIVE_ROUND_TRIP`: at least one position opened and closed through the ladder, on each
chain, with fees reconciled against the chain and the realised PnL matching the
journal's number. Not "it bought something".

---

## canary → live

### Evidence required

1. **≥ 14 days in canary**, ≥ 20 real round trips on that lane/chain.
2. **Realised expectancy within 1σ of the shadow estimate.** If live is much worse than
   paper, the paper model is wrong and every other lane's shadow number is suspect too.
   That is a bigger problem than this lane.
3. **Zero unresolved orders** over the window, or every one of them resolved by
   reconcile with a journal entry.
4. **Zero unprotected-position incidents.**
5. **Slippage within the modelled band.** Measured, from `trades.slippage_bps`.
6. **A drawdown you actually sat through.** A lane that has never had a losing day has
   not been tested; wait for one.

### Flip

```sh
sudo -u kaiba-core /opt/kaiba/venv/bin/python -m kaiba.cli.main risk bounds --max-lane-mode live
sudo -u kaiba-core /opt/kaiba/venv/bin/python -m kaiba.cli.main risk lane confluence-5 --mode live
```

Raise **one lane at a time**, and leave a week between lanes. Two lanes promoted
together cannot be told apart afterwards.

---

## Reversing it

Fastest first. Know all four and which one you want.

```sh
# 1. pause entries. Exits keep working. Reversible in a second.
kaiba risk pause

# 2. reduce-only. No new positions, existing ones may only be closed.
kaiba risk reduce-only --on

# 3. demote one lane. Everything else keeps running.
kaiba risk lane confluence-5 --mode shadow

# 4. kill switch. Every lane's effective entry mode becomes off; protection keeps running.
kaiba risk kill
```

**The kill switch stops new entries; it does not stop exits.** `effective_mode()` returns
`OFF` for every lane when `kill_switch` is set, while the watchdog and executor deliberately
leave already-open positions exitable. Use kill when you do not trust the system's entry
judgement — a suspected bug in sizing, a compromised credential, or a signer you are unsure
about. Use pause or reduce-only when you simply want it to stop buying. Protection must keep
running during all three responses.

Clearing the kill switch is deliberate and separate:

```sh
kaiba risk resume --clear-kill
```

## Automatic promotion

`bounds.allow_self_promotion` lets Hermes move a lane up through the same gates without
asking (PLAN §11 Phase 5). Two things stay true when it is on:

- it cannot exceed `bounds.max_lane_mode`, which is yours; and
- every promotion is a `change` entry in the hash-chained journal with the evidence that
  justified it.

If you find a promotion in the journal whose evidence does not meet the bar on this
page, the gate is wrong. Fix the gate, demote the lane, and write a `correction`.

## Every arming decision gets written down

```sh
sudo -u kaiba-core /opt/kaiba/venv/bin/python -m kaiba.cli.main journal add change \
  "armed confluence-5 sol shadow->canary; 9d shadow, 41 trades, expectancy +0.8% after \
   costs, max dd 3.1% vs 10% stop; preflight green at <release stamp>; \
   canary bankroll 2 SOL, max position 0.02 SOL"
```

If you cannot write that sentence with real numbers in it, you are not ready to arm.
