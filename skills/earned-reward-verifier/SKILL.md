---
name: earned-reward-verifier
description: Verify earned Merkl rewards from receipts and chain state.
version: 0.2.0
author: Codex
license: MIT
platforms: [linux, macos, windows]
---

# Earned reward verifier

## What this skill is for

Verify an existing wallet's earned Merkl allocation from source receipts and chain state.
The current adapter covers Base, Merkle-tree rewards and up to four token rows.
Merkl Flow/LEDGER signature rewards are unsupported and remain unavailable.

The implementation is `kaiba.hunters.merkl_capture` (capture) and
`kaiba.hunters.merkl_entitlements` (proof, replay, collector adapter). No signing or
claim transaction is implemented. No key material is accepted or used.

## When to use it

Use for an existing Merkl allocation, rather than a speculative points valuation.

## Procedure

For an authorized workstation observation, use one private file containing the existing
wallet address and a new attempt directory under repository `data/`:

```text
python -m kaiba.hunters.merkl_capture --wallet-file <private-address-file> --output data/<new-attempt>
```

Run it under a 150-second parent process deadline. The collector's HTTP timeout is not
a total process deadline. It makes one official rewards GET and, only if rows exist,
at most seven public read-only RPC requests. There are no retries, redirects, paid
endpoint fallback or automatic recovery of an interrupted attempt. An unavailable
response is not an empty reward balance. Retain the request admissions and original
response bytes privately; do not commit wallet addresses or raw captures.

Read `result.json` for the identity-free summary. For analysis, replay the original
`bundle.json` with `replay(bundle, at_ms=<current UTC milliseconds>)`. Receipts expire
after ten minutes for current qualification. Old receipts may be replayed at their
recorded observation time only as historical evidence; never refresh their timestamps.

Interpret these outputs precisely:

- `no_rewards_reported`: the API returned no rows for this wallet/chain/time. No RPC
  was needed. It does not establish universal ineligibility.
- `allocation_verified`: the wallet/token/cumulative amount proof matches the root
  returned by `getMerkleRoot()` at the recorded finalized block. Every contract read
  uses that same canonical block hash. Subtract the contract's cumulative claimed
  amount; do not add pending rewards or trust the API's claimed amount as current.
- Positive `remaining_atoms`: an allocation observation, not a verified transferable
  payout. Deployed implementation, claim recipient, wrapper/transfer behavior,
  executable sale value and all claim/sale costs still need evidence.
- `net_profit_usd=null`, `qualified=false`: expected until those missing inputs are
  independently established. Do not manufacture prices, costs or verification flags.

`opportunities(bundle, at_ms=...)` returns leads compatible with the existing
`airdrops.collect(..., collectors=...)` seam. It does not write to the database.
The legacy shared scorer displays base participation cost (zero additional farming for
an already-earned reward); that number excludes unpriced claim and sale costs. Preserve
`meta.unpriced_costs` in any dashboard view and never label that base cost a total.

## What not to do

Production scheduling is not installed. Integrate through the existing research
limiter, storage retention and hunter job; do not schedule the workstation public RPC
path independently of provider budgets. Preserve qualification and financial gates.
Use an evidence journal with last successful response, failure, block age, allocation,
remaining units, unpriced costs and actual post-claim outcome when independently known.

Protocol references: [Merkl integration](https://developers.merkl.xyz/integrate-merkl/quickstart),
[Distributor source](https://github.com/AngleProtocol/merkl-contracts/blob/01c99d0b5e5b8aa49ead52d819c21b8cd8f9c85a/contracts/Distributor.sol),
[Base network](https://docs.base.org/get-started/connect-to-base).
