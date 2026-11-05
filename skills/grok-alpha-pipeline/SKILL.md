---
name: grok-alpha-pipeline
description: Read what Kaiba did with each Grok research card.
version: 0.1.0
author: the operator, Claude
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Research, Social, Execution]
    related_skills: [trade-intent, airdrop-hunter, social-nft-hunt, trade-journaling, operator-readouts]
---

# Grok alpha pipeline

## What this skill is for

Grok Bot posts signed research cards (token, NFT, airdrop, chain, macro, AI, flow) to Kaiba's
intake. A deterministic processor (kaiba-ops job `grok_alpha_process`, every 5 minutes,
`kaiba/hunters/grok_alpha_processor.py`) does the mechanical part so you do not have to:

| track | what the processor already did |
|---|---|
| token, with a chain and contract it can resolve | rebuilt a stale dossier with the existing scan, then sent the token through the agent's existing entry path (`decide()` on the MANUAL lane: every gate, the operator's MANUAL mode, the flat size ladder) and reviewed the card `watch` |
| token, malformed or quote-asset contract | reviewed `rejected`; no decision |
| token without a resolvable chain or contract | recorded, left for you (`model_resolve_identity`) |
| token on an observed network (HyperEVM, Elysium) | recorded `token_network_observed_only`, never decided: blocker `network_observed_not_tradeable:<slug>`; `kaiba_network_status()` lists what is missing (market data, wallet intelligence, execution) |
| same, HyperEVM, with the card's own explicit mainnet identity (`state.chain_identity` 999, no testnet/HyperCore/other-chain wording anywhere in its texts) | latest-state token facts read ONCE per token, inside per-run budgets, shown as the row's `network_facts` (symbol, decimals, supply, unknowns, receipts); otherwise `network_facts_state` is `refused` with the reasons and nothing was read. Facts are not a price, holder, safety or trading signal |
| NFT / airdrop | registered as an UNVERIFIED lead in the opportunities registry; the existing qualification's refusal reasons are its blockers; NFT minting is also blocked by the incomplete BAW execution adapter (wallet unpaired, owner deferred) |
| chain / macro / AI / flow | recorded with the primary-source check still to do |

`kaiba_grok_pipeline()` shows all of it: intake delivery state, each card's identity and
unknowns, route, next action, blockers, the decision (mode, action) and the order/position
outcome with realised PnL once it exists. The dashboard's `/alpha` page shows the same rows.

## When to use it

- Codex's intake monitor wakes you for new Grok cards: read the pipeline before `next`.
- the operator asks what Grok found, what Kaiba did with it, or why something was not bought.
- A daily or weekly review of whether Grok's research pays after costs.

## Procedure

1. **Read the pipeline first.** `kaiba_grok_pipeline(limit=20)`. Check `intake.delivery`:
   `no_receipt` or `stale` means Grok has not delivered, which is a delivery fault, not a quiet
   market. Say which.
2. **Do not redo token decisions.** A row routed `token_decided` already went through every
   gate once. Read its `mode` (paper or live is the MANUAL lane's operator-set mode) and
   `blockers`. Never call `kaiba_submit_intent` again for the same token inside a day; the
   processor already refuses that.
3. **Your work is the rows routed `model_*`.** For those, follow the grok-alpha-processing
   skill: verify the claim against the primary source (official site, contract, programme
   terms), journal with `kaiba_journal_append`, then record the research disposition with the
   intake's `review` command. For a token you resolved, the next processor run decides it only
   if the card is re-delivered with the contract; otherwise report the resolved contract.
4. **NFT / airdrop leads.** `kaiba_opportunities(include_unqualified=True)` shows the registry
   row and why it is unqualified. Points and rumoured airdrops are not cash. Report the exact
   blocker; nothing here mints, claims or signs. `kaiba_baw_status()` says whether the Binance
   Agentic Wallet path could even preview a mint (pairing, developer mode, gas envelope, CLI
   limits); it never executes and never shows a wallet address.
5. **Outcomes.** Once a decided row has `realized_pct`, compare it with the estimate
   (`est_round_trip_bps` is the paper broker's own cost model). Judge sources on realised
   after-cost results, including refusals and missed deadlines, never on Grok's labels.

## Numbers and their source

| item | value | source |
|---|---|---|
| processor cadence | every 300 s, 240 s timeout | `config/schedule.yaml` job `grok_alpha_process` |
| dossier rebuilds per run | at most 3 (existing scan, shared limiter) | same, `max_scans` |
| same-token re-submission | not inside 24 h | same, `resubmit_after_s` |
| dossier must be younger than | 120 s before the decision (`decide()` refuses > 300 s) | `grok_alpha_processor.py`; `engine.DOSSIER_MAX_AGE_S` |
| entry size | flat ladder, capped by the envelope; nothing passed by the processor | `config/risk.yaml`, AGENTS.md owner directive 2026-09-24 |
| MANUAL lane mode | set by the operator only; shadow on 2026-11-02 | `config/risk.yaml` |

## What not to do

- Do not treat a signed card, a Grok `verified` label, a summary or a repeated post as proof of
  safety or edge. The signature identifies the sender only.
- Do not raise size, change a lane's mode, or add a filter because Grok liked a token.
- Do not add a new chain, RPC endpoint, bridge or signer because a card names a new network.
  HyperEVM (chain id 999) and Kinetiq Elysium (mainnet id unverified) are observed networks only:
  a verified chain identity in `kaiba_network_status()` is not a trading route. For a
  `token_network_observed_only` row, `kaiba_network_token_facts(network, contract)` reads the
  contract's code hash, symbol, decimals and supply (latest state, with receipts); it says nothing
  about price, liquidity, holders, safety or who controls it. `kaiba_network_wallet_facts(network,
  address)` reads an account's native balance, transaction count and whether code is deployed;
  it is not a token holdings list, a history, a grade, a PnL or proof of who owns the address.
  `kaiba_network_token_market(network, address)` (HyperEVM only) returns DexScreener's pools for
  the token: pool prices, liquidity, volume and FDV / market cap, vendor-reported with an unknown
  update time. Its USD price comes only from pools where the token is the base. It is never a
  fillable quote, executable depth, fee, tax or holder count. The processor stores one such
  observation per proven token at first sight (the row's `network_market`).
- Do not request BAW pairing or invent a mint or claim receipt; report the exact blocker.
- Do not call a paper fill a profit, and do not promise the owner a daily income.
