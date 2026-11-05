---
name: kaiba-baw-mint
description: Check Binance Wallet readiness and gas-only mint previews.
version: 0.4.0
author: Codex
license: MIT
platforms: [linux]
---

# Kaiba BAW NFT integration

## What this skill is for

Check wallet readiness and preview free-mint candidates without claiming execution.
The owner chose the existing VPS and limits of **$0.10 per mint, $10 per Jakarta
day total across all supported mint chains**, including failed and pending gas. These are
one combined allowance across BSC, Robinhood, Solana, Base and Ethereum. The worker is a staged connection/preview
service. It currently **cannot execute mints**; do not describe it as live trading.

## When to use it

Use for an NFT candidate or an operator wallet-readiness request.

## Procedure

Read `kaiba_baw_status()` first. It reads only the local worker's cached status,
with no Binance refresh, provider request or financial operation. It redacts
wallet/session values and reports the envelope, exact blockers and last preview
evidence when available. The terminal fallback is:

```bash
cd /opt/kaiba-baw/current
/usr/bin/python3 -m kaiba.hunters.baw_mint_worker status
```

Use `refresh` instead of `status` only when a fresh wallet capability check is
needed. It is limited to once a minute; avoid idle polling. A missing/zero
connection check means unknown, not confirmed unpaired. A failed check means
unavailable; do not invent a login failure or replace authentication. Only a
confirmed unconnected response establishes that pairing is needed. The owner
completed pairing on 2 November; check actual status before generating any QR.
QR sign-in is an operator-only helper;
do not read the worker's session files, run `sudo` into its account, or sign out
another wallet. Never change Hermes/Claude authentication for BAW.

For existing NFT research, use Kaiba's `kaiba_opportunities(kind="nft_mint")`;
the audit view can explain unqualified leads. Preserve its qualification and
source/provider budgets. Stage a candidate by writing a temporary JSON input
and calling `... baw_mint_worker stage --input FILE`. Accepted fields are
`chain` (`bsc`, `robinhood`, `sol`, `base`, `eth`), `opportunity_id`, and optional `contract`,
`calldata`, `unsigned_tx`. Data is not permission to sign. The worker does not
fetch arbitrary supplied URLs or execute supplied programs.

`preview --input FILE` accepts `{"candidate_id":"ID_FROM_STAGE"}`. A valid
runtime wallet, active Developer Mode and transaction builder are required.
Previews are retained privately and remain blocked while exact asset effects,
fee bounds and execution integration are unverified. Never turn missing data
into zero fees, a safe contract or a successful mint. Report that blocker.

`last_preview` identifies exact retained CLI-data bytes and the local request
binding. Its provider-reported simulation/change/risk facts are partial evidence,
not a complete chain receipt or asset-effect proof. Empty change arrays do not
establish a free mint. Missing legacy evidence remains unavailable; never rewrite
old hashes or treat a provider/model verification flag as signer authorization.
The EVM CLI's gas-unit limit alone does not lock a total native or dollar fee;
the Solana unsigned-message fee and backend-preservation proof remain separate.

The gas ledger reserves atomically and retains unknown submissions across day
boundaries. Only chain receipts can settle actual fees; a timeout must not
release a reservation or trigger a duplicate submit. The future execution
adapter must enforce a verified native fee ceiling, exact free-mint recipe,
owned recipient and no spending/approval/delegation/withdrawal effects.

## What not to do

Abstract is not in the authenticated supported-chain list checked 2 November.
Do not infer access from a reported App balance. USDT is not native gas. The
owner now also requests cheap mints; those need a separate bounded paid-mint
policy and verified acquisition costs, not relaxation of this zero-value path.

Do not treat an unverified source or a preview as a permitted mint.
The same-host account is least privilege, not a root-proof boundary: Hermes has
administrator access on this VPS. Preserve App-side restrictions and Kaiba's
withdrawal rule. No sends, swaps, approvals or external message signing are
exposed by this staged worker.
