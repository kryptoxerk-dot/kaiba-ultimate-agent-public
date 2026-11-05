---
name: social-feed-ops
description: Operate the shared social feed, its coverage and latency.
version: 0.1.0
author: the operator, Codex
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [Operations, Social, Latency]
---

# Shared social feed operations

## What this skill is for

Operating Kaiba's shared social feed and research inbox: inspecting account coverage and measured latency, and maintaining bounded NFT/airdrop/token discovery without delaying financial protection.

## When to use it

- Checking feed health, account and rule coverage, or delivery latency.
- After a research or launch account list changes (verify the actual provider rule coverage).
- When the inbox is empty, or the journal shows dropped local fan-out.
- Inspecting alpha contract/ticker measurement for one chain.

Source: retention is `retention_days` in `config/social_hunt.yaml`; the 256 MiB / 2 GiB store bounds, the 15-minute maintenance timer and the measured latencies are in `docs/research/codex-social-hunts-fast-20261007.md`; the 32-post queue, 500-rowid / 50-row read bounds and the 1.5 GB WAL refusal are constants in `kaiba/execution/tweet_alpha_dispatch.py`, following the database read rule in `AGENTS.md`.

## Procedure

```sh
cd ~/kaiba
.venv/bin/python -m kaiba.hunters.social_hunt status
.venv/bin/python deploy/social-hunt-monitor.py
.venv/bin/python -m kaiba.hunters.social_hunt accounts
systemctl --user status kaiba-social-hunt.service kaiba-social-hunt-maintain.timer
nice -n 19 ionice -c3 timeout 15 .venv/bin/python -m kaiba.execution.tweet_alpha_dispatch status --limit 20
```

One upstream stream fans out locally before launch creativity, model calls, HTTP enrichment or transaction submission. New research accounts live in `config/social_hunt.yaml`, separately from financially armed launch accounts. Custom-rule coverage must include the union of launch, alpha and research accounts; changing a local list alone does not change provider subscriptions. Verify actual rule coverage after updates. Do not open a second TwitterAPI socket for the same key.

On the current prepaid custom-rule backend, rules poll at 5 seconds. Report measured publication-to-receipt and receipt-to-inbox p50/p95 separately; neither a vendor latency claim nor a synthetic local IPC benchmark proves live delivery speed. J7 mode uses its already-delivered roster and requires the existing authorized session; do not extract browser storage, reauthenticate Hermes or purchase a plan to chase latency.

The user-level service runs continuously. `kaiba-social-hunt-maintain.timer` performs bounded retention/status every 15 minutes. Events never wait for that timer. The isolated store retains seven days, refuses growth above 256 MiB or below 2 GiB free disk, and opens/closes short connections. Do not replace it with a long read of the live Kaiba database.

Check journal warnings for unavailable/dropped local fan-out and last handled timestamp. Empty inbox means no measured receipt, not proof the upstream provider is quiet. Preserve first receipt time across revisions. Enrich candidates outside the ingest path using existing NFT/airdrop tools. Keep unchanged scheduled runs quiet and surface only actionable signals or failures.

The monitor returns `pending` IDs plus `delivery` and `research_budget` health. Treat
`no_retained_receipt`, `stale`, invalid clocks, missing stores and read errors as an evidence
problem, not “nothing interesting happened.” A healthy `recent` state still does not mean a
candidate is profitable. The monitor reads without creating schema and removes changing poll
clocks/receipt timestamps from its trigger payload, so the same fault does not repeatedly invoke
the agent. New pending IDs and health transitions still trigger the existing scout.

Verify the actual scheduled script, not just the repository source. The scout resolves
`kaiba-social-hunt-monitor.py` in `~/.hermes/profiles/kaiba-operator/scripts/`. On October 29
that protected copy still had the old pending-only output despite the new repository monitor
being installed. Lead integration and an ordinary scheduled output-hash check are required;
see `docs/research/codex-social-delivery-20261029.md`. Do not equate a successful direct command
or a cron `ok` with activation of the new health checks.

`persistent_lock_owner_unverified` means the shared research-budget lock has remained for at
least 60 seconds. This is an operational diagnostic threshold, not proof the owner process is
dead. Report the condition; inspect its actual owner before recovery. Never clear it based on
age alone, reset the spending ledger or route the request through a different ledger. Provider
GETs must first reserve against the existing budget; a blocked reservation is not a failed HTTP
request. No credential is read by the monitor.

Alpha contract/ticker measurement covers the union of launch and alpha accounts. Explicit alpha chain hints override launch hints. Two dedicated research workers dispatch before launch planning/submission, with at most 32 pending posts; saturation is logged, never allowed to delay exits. Replies and quote text can supply the contract. Multi-chain EVM addresses require an explicit chain hint; never label a Robinhood contract BNB by default. Queued posts older than the alpha age limit are skipped rather than assigned a fictitious early entry. This records provider prices and forward marks without submitting a buy. Unresolved cashtags are visible in the alpha inbox; no candidate is evidence of positive expected value merely because a watched account mentioned it.

The alpha CLI reads at most the latest 500 rowids and returns at most 50 rows, closes the connection, has a 3-second query cutoff and refuses reads when the WAL is over 1.5 GB. Wait 60 seconds and recheck before another live DB chunk. Use `--chain sol`, `--chain bsc` or `--chain robinhood` to inspect a chain. A daily launch-cap refusal is an intended budget stop, not a feed outage; never widen the owner's caps to create activity.

## What not to do

- Open a second TwitterAPI socket for the same key.
- Treat a vendor latency claim or a synthetic local IPC benchmark as proof of live delivery speed.
- Extract browser storage, reauthenticate Hermes or purchase a plan to chase latency.
- Replace the isolated store with a long read of the live Kaiba database.
- Read an empty inbox as proof that the upstream provider is quiet.
- Let research saturation delay exits, or label a Robinhood contract BNB by default.
- Treat a watched account's mention as evidence of positive expected value.
- Widen the owner's caps to create activity.
