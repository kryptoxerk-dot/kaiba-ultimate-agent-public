---
name: social-feed-ops
description: Operate Kaiba's shared social feed and research inbox, inspect account coverage and measured latency, and maintain bounded NFT/airdrop/token discovery without delaying financial protection.
metadata:
  hermes:
    tags: [Operations, Social, Latency]
---

# Shared social feed operations

```sh
cd ~/kaiba
.venv/bin/python -m kaiba.hunters.social_hunt status
.venv/bin/python -m kaiba.hunters.social_hunt accounts
systemctl --user status kaiba-social-hunt.service kaiba-social-hunt-maintain.timer
```

One upstream stream fans out locally before launch creativity, model calls, HTTP enrichment or transaction submission. New research accounts live in `config/social_hunt.yaml`, separately from financially armed launch accounts. Custom-rule coverage must include the union of launch, alpha and research accounts; changing a local list alone does not change provider subscriptions. Verify actual rule coverage after updates. Do not open a second TwitterAPI socket for the same key.

On the current prepaid custom-rule backend, rules poll at 5 seconds. Report measured publication-to-receipt and receipt-to-inbox p50/p95 separately; neither a vendor latency claim nor a synthetic local IPC benchmark proves live delivery speed. J7 mode uses its already-delivered roster and requires the existing authorized session; do not extract browser storage, reauthenticate Hermes or purchase a plan to chase latency.

The user-level service runs continuously. `kaiba-social-hunt-maintain.timer` performs bounded retention/status every 15 minutes. Events never wait for that timer. The isolated store retains seven days, refuses growth above 256 MiB or below 2 GiB free disk, and opens/closes short connections. Do not replace it with a long read of the live Kaiba database.

Check journal warnings for unavailable/dropped local fan-out and last handled timestamp. Empty inbox means no measured receipt, not proof the upstream provider is quiet. Preserve first receipt time across revisions. Enrich candidates outside the ingest path using existing NFT/airdrop tools. Keep unchanged scheduled runs quiet and surface only actionable signals or failures.
