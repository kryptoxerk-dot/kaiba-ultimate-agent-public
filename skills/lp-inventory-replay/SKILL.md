---
name: lp-inventory-replay
description: Replay DLMM inventory, fees and observed position actions.
license: MIT
metadata:
  version: 0.3.0
  author: Codex
---

# LP inventory replay

## What this skill is for

Reconcile observed DLMM inventory and fee accounting against source receipts.

## When to use it

Use for saved position observations or a supplied SOL-quoted accounting trace.

## Procedure

Run from the Kaiba repository:

```sh
python -m kaiba.learning.lp_inventory_replay /absolute/path/to/trace.json
```

The function `kaiba.learning.lp_inventory_replay.replay(trace)` returns the same report for another Python consumer. The CLI accepts at most 4 MB / 10,000 events, reads one local file and prints JSON. It has no database, provider, signing or order access. Inventory accounting and saved-history reconciliation can run as standalone tools. The lead owns any integration with a Hermes MCP tool, scheduler or dashboard.

## Capture actual DLMM bin state

The account-state input stage is now available on the workstation. Select a public pool and
position from a recorded population first; do not select it because of its eventual return.
Create a parent under ignored `data/`, then use a **new** attempt directory on every observation:

```sh
python -m kaiba.learning.dlmm_capture --pool POOL --position POSITION --output data/lp-captures/observation-001
python -m kaiba.learning.dlmm_capture --pool POOL --position POSITION --output data/lp-captures/observation-002
python -m kaiba.learning.dlmm_replay data/lp-captures/observation-001/response-2.json data/lp-captures/observation-002/response-2.json
```

Each capture permits two keyless public Solana `getMultipleAccounts` requests, separated by
1.2 seconds, with a 20-second timeout and 1 MB response ceiling. It never retries a throttle or
unknown attempt, uses no credentials, and accepts no paid endpoint override. Keep it on the
workstation until the lead approves production collection within the existing provider budget.
The first read discovers the current position range; the second captures pool, position, bin
arrays and mint accounts together in one finalized bank context. Missing or changed accounts
produce an unavailable result, not zero balances. An old API cache is not current account state.

Raw captures and derived `state.json` contain public pool/position identities and must remain
in ignored `data/`. The printed summary and comparison omit those identities. The inputs use
integer atoms, including u128 shares: consume their JSON with Python or another lossless parser.
Ordinary JavaScript `Number` cannot preserve those integers. No mint UI scaling is applied.

`kaiba.learning.dlmm_state.snapshot(receipt)` exposes bin principal and gross fee entitlements.
`kaiba.learning.dlmm_replay.compare(receipts)` reconciles fee accrual with cumulative claims and
flags changed position shares. A claim transfers existing earned fees; it is not new income.
Endpoint shares may match even when intervening actions occurred. Sampled active-bin positions
are observations, not continuous time in range. No price, swap, rent or gas is fabricated.

This input stage is not the completed counterfactual engine: keep/narrow/rebalance/close returns
remain unavailable until ordered swaps/actions, executable quotes, capital reconciliation and
network/rent/transfer-fee cashflows exist. It does not generate normalized action traces from
sparse endpoints or choose a policy winner. The original event-accounting tool below remains
the target for a complete decoded trace. See `docs/research/codex-dlmm-replay-20261029.md` for
the pinned SDK/IDL, actual-source validation, scope and integration requirements.

## Reconcile actual position actions and contributions

The new `kaiba.learning.dlmm_actions.decode_transaction(...)` decodes finalized saved
`jsonParsed` transaction results; it performs no RPC calls. It checks the DLMM event program
and event-authority account, pool/position/owner identities and exact pinned event layouts.
It reconciles deposits, withdrawals and gross fee claims against owner token/native balances,
including temporary wrapped SOL, network fees and encumbered account funding. Composition
fees are embedded in transaction amounts and must never be charged a second time. Other
native transfers remain separately labelled with unknown purpose.

`kaiba.learning.dlmm_history.reconcile(capture, pages, transactions)` binds archived RPC
requests to their responses and reconciles the position's indexed lifetime to the snapshot.
It requires an initial creation event, a terminal **empty** pagination response after the
oldest signature, every indexed transaction through the snapshot, and matching fee-claim
and account-funding counters. A short page, null transaction, missing balance, unsupported
event/version or unexplained cashflow cannot turn into a complete history or zero cost.
Same-slot action ordering is currently refused without additional block-order evidence.

```sh
python -m kaiba.learning.dlmm_history --capture data/lp-captures/observation-002/response-2.json --page data/lp-history/signatures.json --page data/lp-history/terminal.json --transaction data/lp-history/transaction-001.json
```

Repeat `--page` in newest-to-oldest pagination order and `--transaction` for every fetched
receipt. Each receipt must archive `{request, response, started_ms, received_ms}`. Signature
requests use `getSignaturesForAddress`, the exact position, finalized commitment, limit at
most 100, and `minContextSlot >= snapshot.slot`; subsequent pages use the previous page's
last signature as `before`. History collection must start after the snapshot receipt.
Transaction requests use `getTransaction`, `encoding: jsonParsed`, `commitment: finalized`,
`maxSupportedTransactionVersion: 0`. Unsupported versions remain unavailable, never skipped.
The CLI reads at most 20 pages / 200 transactions / 32 MB total, with the existing 4 MB per-file
limit. It neither collects nor schedules requests and prints no raw position/owner identities.
Keep raw receipts in ignored `data/`.

`provider_index_coverage_reconciled` describes this provider's indexed evidence plus creation
and counters; it is not independent consensus verification. The optional SOL comparison uses
the observed active-bin Q64 mark to value ending LP principal, pending gross fees and past
distributions against holding every contributed asset. It separates transaction fees from
other native debits of unknown purpose, and keeps account funding outside either marked
basket. It assumes distributions are held unchanged. It is neither original-cost PnL nor an
executable exit or a policy counterfactual. `net_pnl`, `net_return` remain null and
`strategy_promotable` remains false. Unknown transfer deductions, rewards, liquidation impact,
funding recovery and opportunity cost still matter. Never rank by gross fees alone.

Read `fee_hurdle_at_mark` beside `gross_fee_value_y_atoms`. The hurdle is the
gross fee value needed to match holding the contributed assets after observed
network fees, with principal and withdrawals held at the same bin price. The
additional fees needed show the remaining shortfall after pending and claimed
gross fees. A second shortfall includes the observed native debits of unknown
purpose. All values are in wrapped SOL atoms. Keep this retrospective mark
separate from any fee forecast or executable exit. Missing history yields no
hurdle. For the eight-post review and worked example, see
`docs/research/codex-eight-posts-20261104.md`.

Actual validation and a negative LP-versus-holding example are in
`docs/research/codex-dlmm-cashflows-20261029.md`. Integrate recurring use through the
existing lab owner with explicit storage retention and provider budget.

## Evidence first

Use **normalized position-level receipts**, not candles, pool volume, APR, total pool reserves, or the existing aggregate `data/labs/lp/result.json`. A decoder must allocate each bin's actual inventory and fees to this position's liquidity shares. Preserve the source artifact's SHA-256 and transaction/instruction identities in the private trace. Never commit a trace containing owner or agent wallet addresses.

v1 supports token/SOL positions only. SOL pays gas and rent from the same starting inventory. Third-asset rewards, unrelated transfers, and USDC-quoted positions with a separate SOL gas balance require an expanded ledger; do not hide them in zero cost fields. Fees in token or SOL are supported. Explicitly attest whether the inventory, fee and cost history is complete. A true attestation is the caller's claim, not chain verification by this tool.

Every event must include a distinct `id`, causal `at_ms`, `prices_usd: [token_price, SOL_price]`, integer `active_bin`, and `cost_quote`. Decimal quantities must be strings or integers; floats, NaN and infinity are rejected. Equal timestamps retain supplied order. Initial state must start before opening the LP, with all funds in the wallet. Do not inject additional external capital mid-trace.

`cost_quote` is either `null` (unknown) or all three nonnegative SOL amounts: `network`, `rent_spent`, `other`. Costs are debited once from free SOL **before** that event. Use actual observed transaction cost once per transaction, explicit zeros on its other normalized instructions. Unknown is never a zero. Execution slippage and swap fees already embedded in inventory deltas must not be charged again as `other`.

| Event kind | Additional fields | Effect |
|---|---|---|
| `deposit`, `withdraw` | `bin`, `amount: [token, SOL]` | Transfer principal between wallet and bin |
| `inventory_swap` | `bin`, signed `delta: [token, SOL]` | Observed principal exchange within that bin, excluding separately accrued fees |
| `wallet_swap` | signed `delta` | Observed exchange to rebalance or liquidate |
| `fee` | `amount` | Accrue earned fees, not yet claimed |
| `claim` | `amount` | Transfer pending fees to wallet without earning them again |
| `rent_lock`, `rent_refund` | `amount_quote` | Lock/return recoverable SOL; refund cannot exceed prior lock |
| `mark` | none | Update valuations and active bin |
| `checkpoint` | `expected`, `terminal` | Reconcile wallet, aggregate LP, pending fees and rent |

Final event must be a reconciled terminal checkpoint after the start. The supplied checkpoint validates arithmetic against the supplied balances, not authenticity. Terminal means end of measurement; an LP can still be open and marked.

Minimal **synthetic** trace (no LP position, demonstrating the file contract):

```json
{
  "schema": "lp-inventory-replay/1",
  "quote_asset": "SOL",
  "policy": {"id": "fixture_hold", "declared_ms": 0},
  "evidence": {
    "kind": "fixture",
    "source_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "inventory_complete": true, "fees_complete": true, "costs_complete": true
  },
  "initial": {"at_ms": 0, "wallet": ["10", "2"], "prices_usd": ["10", "100"]},
  "events": [{
    "id": "fixture-end", "at_ms": 1000, "kind": "checkpoint", "terminal": true,
    "prices_usd": ["10", "100"], "active_bin": 0,
    "cost_quote": {"network": "0", "rent_spent": "0", "other": "0"},
    "expected": {"wallet": ["10", "2"], "lp": ["0", "0"],
                 "pending_fees": ["0", "0"], "locked_quote": "0"}
  }]
}
```

## What not to do when reading the answer

- Report `evidence_kind`, `evidence_gaps`, source/input hashes and measurement window first. A fixture, modeled trace, late policy declaration, incomplete history or any unknown cost withholds `net_pnl_usd` and `net_return`.
- `*_known_costs` fields are partial accounting when gaps exist. Never relabel them net profit. `net_available` means the declared inputs reconcile; it is not a statistical pass.
- Compare the ending value with the original asset basket, all-token, all-SOL and constant USD cash. Cash is a zero-yield USD proxy, not observed USDC performance. Benchmark acquisition costs are not modeled.
- Fees at accrual plus `inventory_and_mark_change_usd` minus costs equal known PnL. The residual includes subsequent changes in the prices of earned fee assets. It is **not** a causal adverse-selection or loss-versus-rebalancing estimate.
- Capital time separates deposited principal, the active bin, free wallet funds and locked rent. It holds prior marks/bin activity constant between supplied events. Sparse samples cannot establish continuous time in range.
- Marks are not executable liquidation prices. A positive marked balance with open bins or token holdings does not prove a realizable exit.
- `chain_verified` and `strategy_promotable` are always false. The tool does not pick keep, narrow, rebalance or close. Compare such policies only with independently generated causal traces sharing the same starting capital, population and horizon, including each policy's executable costs. A backtest winner still needs the existing preregistered forward evaluation.

For decoder integration, use the current Meteora SDK/IDL: [official account model](https://github.com/MeteoraAg/docs/blob/main/developer-guides/dlmm/program/accounts.mdx). Bin fee growth, liquidity supply, position shares and claim checkpoints are separate state. The aggregate daily LP API report is insufficient to reconstruct them.
