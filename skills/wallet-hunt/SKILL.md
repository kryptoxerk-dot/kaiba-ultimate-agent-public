---
name: wallet-hunt
description: Find copyable, honest wallets for confluence.
version: 0.1.0
author: the operator, Claude
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Intelligence, Wallets, Confluence, Learning]
    related_skills: [wallet-confluence, wallet-grading, trusted-wallet-copy, x-scan]
---

# Wallet hunt

## When to use it

Daily (cron "Kaiba wallet hunt"), and whenever the operator asks which wallets are actually good.

## What this skill is for

Finding wallets whose buys are worth counting as **confluence**. A good wallet passes all five
tests below. Raw profit is not one of them: on sol, last week's most profitable wallets were not
next week's (rank correlation +0.05, 2026-10-07 and 10-08), and the fastest winners hold 13 s, too
fast for anyone to copy.

1. **Copyable profit.** Copying it at OUR delay (20-30 s) and OUR cost (about 3.25% per side on sol,
   4.5% on robinhood, 4% on bsc) made money. The system computes this as the **proven** cohort
   (`kaiba/learning/proven.py`), and only out of sample: the wallet is picked on older trades and
   judged on newer ones.
2. **Holds long enough to copy.** Median hold of minutes, not seconds.
3. **Does not eat its copiers.** Red flag: it sells within about 2 minutes, right after several new
   wallets bought behind it. That is a wallet farming its followers.
4. **Not a scammer or insider.** Red flags:
   - it belongs to a large co-timed cluster (the evidence carries `cotime_cluster`);
   - it was funded by the token's deployer, or it buys its own launches;
   - its "profit" is one trade (the best trade is more than half the total);
   - its median buy is dust (pennies), which marks a sol dust network.

Source for every threshold above: the costs come from `fill_costs` on our own fills; the
confluence settings (min 2 proven entities in 1800 s) are in `config/risk.yaml` under
`lanes.confluence-5.params`; the delay, hold and persistence findings are the 2026-10-07 and
10-08 journal lessons (subject `wallets`).
5. **The chain shows persistence at all.** If a chain's winners do not out-earn its losers out of
   sample, nobody on that chain is proven, however good they look. The funnel says so.

## Procedure

For each chain (sol, robinhood, bsc; eth is research-only, see the end):

1. `kaiba_proven_wallets(chain)`. Read:
   - the member count;
   - `funnel`: seen, active, eligible, passed, and the chance p-value;
   - whether the cohort is EMPTY.

   An EMPTY cohort is a real answer. Report it; do not pad the list.
2. **New members** (in today's cohort, not yesterday's): open each with `kaiba_wallet(chain, address)`
   and check tests 2-4. Put each one that fails a red flag on the record:
   `kaiba_journal_append("observation", "<address> fails <test>: <evidence>", subject="wallet-hunt")`.
3. **Confluence check.** Does the lane fire on these wallets, and do its paper trades make money?
   Use `kaiba_signals(lane="confluence-5")` (counts proven entities, min 2 in 30 min, paper mode) and
   the sprint scorecard.
4. **One lesson a day.** Which red flag removed the most candidates? Did yesterday's new members
   still earn today? Record it with `kaiba_journal_append("lesson", ...)`.
5. **Rule changes only through the gates.** A threshold change goes through
   `kaiba_propose_experiment` with `"chain"` set, never by hand.

**Ethereum (Stockereum):**
- `kaiba_wallets(chain="eth")` lists the wallets graded on REALIZED ETH by the `eth_traders` job.
- The list is research only. No ETH lane exists, and copy profit has not been measured there yet.

## What not to do

- Put a wallet in `trusted_copy`. That cohort is the operator's own list.
- Copy-trade, or enter a trade because ONE wallet bought.
- Include the operator's wallets, the agent wallets or any `trusted_copy` wallet in a list or export.
- Call gmgn-cli from research: its rate limit is shared with the stop-loss sells.

## Report shape (Telegram, at most 8 lines)

- Per chain: proven members (today vs yesterday), chance p, and whether the chain persists.
- Wallets removed today and why (one line).
- Confluence: signals in 24 h, and paper result so far.
- The day's lesson.
