---
name: social-follow-snipe
description: Track X follows; route official CA posts via launch-snipe.
version: 0.1.0
author: the operator, Codex
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [Intelligence, Social, Execution]
---

# Following graph and official contract posts

## What this skill is for

Tracking who Kaiba's watched X accounts follow, inspecting first-seen project edges, and operating authorized official-account contract-post sniping through the existing launch-snipe gates.

The owner authorizes automatic project-contract sniping when the official token account posts. Follow events discover candidates; they are not endorsements or profitable entry evidence. Keep the 5%-of-supply creator launch buy separate: this source uses ordinary launch-snipe sizing, existing per-entry limits, tax checks, dossiers, risk, engine/executor and protection. Do not widen risk/modes/venues or write signer trust.

## When to use it

- Checking follow-graph health, new first-seen edges or candidate projects.
- Checking whether a project account is verified, and whether its contract post was queued or refused.
- Evaluating this source's coverage, latency and net expectancy.

Source: the poll interval, page size, project cap, post age and `live_official_posts` flag are in `config/social_follow.yaml`; the $20 lifetime / $0.50 daily research ledger is the `twitterapi-research` budget (`skills/twitterapi-research/references/api-and-budget.md`); the operating runbook is `docs/runbooks/social-follow-snipe.md`.

## Procedure

```sh
cd ~/kaiba
.venv/bin/python -m kaiba.hunters.social_follow status
.venv/bin/python -m kaiba.hunters.social_follow edges --limit 20
.venv/bin/python -m kaiba.hunters.social_follow projects --limit 20
systemctl --user is-active kaiba-social-follow.service kaiba-social-follow-maintain.timer
```

J7 `following_update` / `unfollowing_update` enter the existing shared feed. `follow_scan` is a latest historical contract lookup; never re-date it as a new announcement or blindly buy its result. The paid fallback rotates the 91 watched sources, reading their newest 20 follows under the same $20 lifetime/$0.50 daily research ledger. Initial snapshots are baselines; later first-seen edges have observation times, not precise follow times. Missing rows from a partial page are not unfollows. Polling is not sub-second follow tracking. Budget exhaustion or API failures mean unavailable evidence, not an empty following list. Never switch/reset the ledger or buy another subscription.

Project identity is bound to immutable X user ID, its profile website and that public HTTPS website's exact X-account backlink. Parody/private accounts and profiles without this proof remain unarmed; badges/follower counts are not proof. A handle/website identity change revokes verification. At most 20 current projects get three bounded owned rules on the same TwitterAPI socket; no launch author is implicitly added. Inspect the evidence URL before describing a project as official. This identity proof is not a fraud assessment or guarantee of issuer legitimacy.

Only fresh own text from the exact verified account ID can queue an entry. A unique exact CA and an explicit positive contract/mint announcement are required. Token metadata's issuer X profile must match that account; a followed trader's promotion or a narrative `.../status/...` link is not issuer proof. Missing issuer data remains unavailable, not guessed. Quoted/retweeted contracts, ambiguous chains, missing author IDs, warnings about fake/testnet/old contracts and stale posts are recorded/refused. Exact compound-key token rows establish the chain; symbols are never guessed. Existing launch-snipe evaluation applies every venue, deployer, early-book and tax veto, plus daily count vetoes only when finite, then the normal dossier and engine handoff. This source never invokes a swap or token create. A stable official-source/token signal ID prevents repeated official entries while preserving an earlier paper launch signal. Latest owner directive 2026-10-07 removes daily spend and launch-count allowances for all three chains: max_snipes_per_day is explicitly unlimited. Existing per-entry sizing, loss stops, exposure and protection remain unchanged. If finite caps are configured again, they count distinct live/canary entry reservations, excluding paper entries. Provider-price, order, fill, protection and net fee cashflow are separate receipts.

Current live-arming status: `live_official_posts: false`. All three existing launch-snipe live venue allowlists are empty; BSC also has a hard engine paper boundary. The tested source-only engine patch is a separate review artifact (`docs/research/official-post-arming-candidate.patch`), not installed. Do not claim automatic real-money buys from a graph receipt, verified watch or handed signal. Codex requested an explicit exception to the lead-owned phase-gate boundary; keep existing gates until the authorized integration is completed.

The standalone follow database uses short, closed connections. Reads of the live financial database must be under `nice -n 19 ionice -c3 timeout`, check WAL before each chunk and stay below 60 seconds; over 1.5 GB wait 60 seconds and recheck. Do not edit core/risk/signer, Hermes auth or another service. BSC's existing paper-only launch-snipe gate stays intact if present; report its exact refusal rather than claiming BNB orders are live.

Evaluate all observed candidates including refused ones, using receipt-time executable quotes, taxes, fees and fill slippage. Report sample size, coverage, publication-to-receipt and receipt-to-entry latency, missing records and net expectancy. No size increase follows from multiple famous accounts following a project. Quiet monitoring is not proof the source works; verify real graph snapshots and post/decision receipts.

## What not to do

- Widen risk, modes or venues, write signer trust, or apply the 5%-of-supply creator launch buy to this source.
- Treat a follow event as an endorsement or profitable entry evidence; re-date a `follow_scan` result as a new announcement, or blindly buy it.
- Switch or reset the research ledger, or buy another subscription.
- Describe a project as official without inspecting its evidence URL; badges and follower counts are not proof.
- Guess missing issuer data, chains or symbols.
- Claim automatic real-money buys from a graph receipt, verified watch or handed signal while `live_official_posts` is false.
- Edit core/risk/signer, Hermes auth or another service.
- Increase size because several famous accounts follow a project.
