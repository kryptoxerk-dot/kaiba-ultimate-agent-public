---
name: twitterapi-research
description: Research X posts via TwitterAPI.io on a capped data budget.
version: 0.1.0
author: the operator, Codex
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Intelligence, Social, Research]
---

# Paid X research for Kaiba

## What this skill is for

Researching X posts, contract-address narratives and J7-tracked accounts using TwitterAPI.io with a persistent data-spending limit.

## When to use it

Use this skill for token narrative evidence, upcoming launch announcements, or historical posts from J7 accounts. The owner authorized a $20 data budget on 2026-10-06; this is a total budget, not $20 per run. The bundled reader permits only balance and advanced-search GET requests. It does not require an X login.

Source: rates, reservation sizes and response conventions are in [references/api-and-budget.md](references/api-and-budget.md); the $20 budget is the owner's 2026-10-06 authorization, recorded in `docs/research/codex-j7tracker-research-20261006.md`.

## Procedure

Run `scripts/research.py` with the Kaiba Python environment. Credentials come from `TWITTERAPI_IO_KEY`, or a one-line file outside the repository (default `~/.config/kaiba/twitterapi-research.key`), then the existing `TWITTERAPI_IO_KEY` entry in `~/.config/kaiba/.env`. The deployed existing key matched the owner-supplied funded key; no credential replacement was required. Never print the credential or put it in a command argument. Resolve relative script paths against this skill directory.

```sh
python scripts/research.py balance
python scripts/research.py search --query 'from:cz_binance' --max-pages 1 --output ~/kaiba/data/social/cz.jsonl
python scripts/research.py search --query 'EXACT_CONTRACT_ADDRESS' --max-pages 2 --output ~/kaiba/data/social/token.jsonl
```

The default ledger is `~/.config/kaiba/social-research-budget.json`: $20 lifetime, $0.50 UTC daily, shared by invocations using that path. Each search reserves the documented maximum 20 tweets at $0.00015 each before sending, without refunds for ambiguous requests. The cap is intentionally conservative. Never reset or switch ledgers to evade exhaustion. The script reports reserved cost, which is not the vendor's bill. Check balance before and after a batch when actual spend matters. See [references/api-and-budget.md](references/api-and-budget.md) for rates and response conventions.

Research the chain launchpad and official accounts first, then the trader/caller cohort. The J7 skill includes a frozen roster and prioritized shortlist; membership demonstrates feed coverage, not a popularity ranking or a proven trading edge. Use exact contract addresses for token searches: symbols collide. An EVM address alone does not establish BNB versus Robinhood. Verify the chain using existing Kaiba token/provider evidence.

The JSONL records contain publication time, first observation time, author ID/handle, post ID, text, source and address candidates. Keep observation time when joining to decisions. A historical post retrieved today was not available to our agent yesterday. Reposts, quotes, paid promotions and several accounts from one entity are not independent confirmations.

Compare source cohorts prospectively on all encountered candidates, including skips, with executable delayed quotes and net round-trip fees/slippage/taxes. Freeze the rules before the next evaluation window. Report coverage, latency, fills, missing data, net expectancy and confidence interval. Preserve flat sizing and existing risk controls. No popularity-based size increase or unmeasured live filter follows from this skill.

HTTP 401/402/429, semantic provider failures or malformed responses are unavailable evidence. Stop the batch and report the stable reason; do not return a zero narrative score. Honor Retry-After before a later run. Bound each invocation by page count and a shell timeout. This reader writes standalone JSONL only; lead-owned `x_search`, `x_narrative`, database and scheduler wiring remain separate integration work.

## What not to do

- Print the credential, or put it in a command argument.
- Reset or switch ledgers to evade exhaustion, or read the reserved cost as the vendor's bill.
- Search by symbol alone (symbols collide), or infer BNB versus Robinhood from an EVM address.
- Count reposts, quotes, paid promotions or several accounts from one entity as independent confirmations, or treat a post retrieved today as available to the agent earlier.
- Return a zero narrative score on HTTP 401/402/429, semantic provider failures or malformed responses; stop the batch and report the reason.
- Derive a popularity-based size increase or an unmeasured live filter from this research.
