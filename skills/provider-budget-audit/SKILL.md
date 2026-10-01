---
name: provider-budget-audit
description: Audit provider credit burn before paying more.
version: 0.1.0
author: Kaiba contributors
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Operations, Providers, Budget]
    related_skills: [incident-recovery, alpha-radar, trade-journaling, strategy-experiment]
---

# Provider budget audit

## What this skill is for

Reading the shared weighted limiter, understanding why a provider is cold, and deciding
whether a paid tier is genuinely justified. The limiter is `kaiba/core/limiter.py`; its
per-provider settings are the `provider_budgets:` block of `config/risk.yaml`.

The habit this skill enforces: **audit before upgrading.** Duplicate listeners, abandoned
jobs, retry storms and over-eager polling look exactly like "we need a bigger plan", and
they are far cheaper to fix.

## When to use it

- Daily, as the scheduled budget audit.
- A provider shows `banned: true`, a non-zero `penalty_level`, or a `family_bans` entry.
- A feed went quiet and you need to know whether it is the market or the budget.
- Before recommending any paid plan to the operator.
- After adding a new feed, to see what it actually costs.
- When protection or reconciliation is competing with research for the same budget.

## Procedure

1. **Read the limiter.** `kaiba_status()` returns `providers`, one row per provider:
   `credit` (current bucket, refilled by elapsed time), `capacity`, `inflight`,
   `penalty_level`, `spent_today`, `daily_cap`, `banned`, and `family_bans` with
   `banned_until_ms` per endpoint family.
2. **Read the errors, not just the state.**
   `kaiba_events(kinds=["provider.error","provider.budget"])` shows what happened and in
   what order. A ban is the end of a story that started with retries.
3. **Interpret the numbers** (table below). `credit` near zero with `inflight` at
   `max_inflight` is saturation. `penalty_level` above 0 means we have already been
   told to slow down. `spent_today` against `daily_cap` is the money question.
4. **Attribute the burn.** Which job, which endpoint, which chain, which worker. Look for
   duplicate listeners (the same swap arriving from two feeds), abandoned cron jobs,
   historical backfills running in the foreground, and retry loops.
5. **Check priority.** Protection and unresolved orders must always have budget.
   Research and new entries yield. If they are not yielding, that is the finding.
6. **Only then consider a plan.** State the exact plan, the renewal basis, the quota, the
   feature that is actually blocked, and the measured benefit. "We hit the cap" is not a
   justification; "quote and swap are structurally impossible on this tier" is.
7. **Journal the audit.** `kaiba_journal_append("observation", ...)` with per-provider
   burn, bans, and any recommendation. the operator decides on spending; you present the case.
8. **Reduce load if needed.** `kaiba_pause` stops entries (and their quote traffic);
   `kaiba_set_lane_mode(lane, "off")` retires a lane's feed consumption entirely.
   The limiter's `reset` is an operator action and is deliberately not on your surface.

## Reading the limiter

| Field | Meaning | What to do |
|---|---|---|
| `credit` | tokens in the leaky bucket now | near 0 → you are the bottleneck, not the provider |
| `capacity` | bucket size | a burst larger than this can never succeed |
| `refill_per_s` | sustained rate | sustained RPS for an endpoint = refill ÷ its weight |
| `inflight` / `max_inflight` | concurrent calls | GMGN is `max_inflight: 1` on purpose |
| `min_interval_ms` | pacing floor | GMGN 1,200 ms; DexScreener 1,100 ms |
| `penalty_level` | escalating backoff after 429s | > 0 means stop adding load |
| `spent_today` / `daily_cap` | credit burn | Helius `daily_credit_cap: 900000` against a 1M free tier |
| `banned` | provider-wide cooldown | wait; never rotate keys or IPs |
| `family_bans` | one endpoint family parked | the rest of the provider still works |

Shipped weights: GMGN `{quote: 10, swap: 10, default: 1}`. That single line is the most
consequential number in the file, and the next section is why.

## When a paid tier is actually justified

The one clear-cut case we have, from `docs/research/04-data-sources.md` and
`docs/research/08-gating-and-budget-verification.md`:

**GMGN Free is weight 5. `order quote` and `swap` are weight 10 each.** A weight-10 call
against an allowance of 5 cannot succeed — not slowly, not with better pacing, not at
3 a.m. The GMGN lane's execution path is structurally unavailable on Free. Plus at
$29/mo (or $290/yr) raises the allowance to weight 20; Pro at $990/yr is weight 50 and
buys throughput we have no evidence of needing.

That is the shape of a justified upgrade: **a capability that is impossible on the
current tier, not a quota we keep hitting.** Compare with the rest:

| Provider | Free tier | Upgrade | Justified when |
|---|---|---|---|
| GMGN | weight 5 — cannot quote or swap | Plus $29/mo, weight 20 | now, for the GMGN execution lane |
| Helius | 1M credits, 10 RPS, webhooks, Sender at 0 credits | $49 Developer (10M), $499 Business (LaserStream mainnet) | when `spent_today` sustainably exceeds the free credits, or a lane is proven latency-bound |
| Solana Tracker | 10k req/mo, 3 rps | €50 Advanced (200k), €397 Premium (Datastream rooms) | when first-buyer and PnL v2 calls are the binding constraint on wallet intelligence |
| Bitquery | 7-day trial | $39/mo billed annually | it is the **only** source with Pons/Robinhood, LaunchLab and DBC streams |
| DexScreener | free, keyless, 60 rpm on profiles/boosts/metas | — | never; pace instead |
| Jupiter | keyless 0.5 rps, free key 1 rps | $25 Developer (10 rps) | when the direct lane's quote rate is the bottleneck |
| xAI / `x_search` | SuperGrok $30/mo OAuth | API ≈ $5 per 1k posts | per-post billing began 2026-09-21 — measure post volume first |
| Yellowstone gRPC | — | $99 Subglow | **only after** a lane is positive in shadow and proven latency-bound (PLAN §12) |

Two facts that change the arithmetic and are easy to forget: an upgrade does **not**
clear an active GMGN IP lock (disable retries first), and webhooks are not free — Helius
charges 1 credit per push.

## Thresholds and their source

| Item | Value | Source |
|---|---|---|
| GMGN weights | quote 10, swap 10, default 1 | `config/risk.yaml`; research 04 |
| GMGN plan weights | Free 5, Plus 20, Pro 50 | research 04, observed 2026-09-13 |
| GMGN bucket | leaky bucket 20/20 since 2026-05-13; sustained RPS = 20 ÷ W | research 04 |
| GMGN pacing | `min_interval_ms: 1200`, `max_inflight: 1` | `config/risk.yaml` |
| Helius daily cap | 900,000 credits against a 1M free tier | `config/risk.yaml` |
| Helius costs | webhook 1 credit/push, `getTransactionsForAddress` 10 credits/100 tx, Sender 0 credits | research 04 |
| Solana Tracker free | 10k req/mo, 3 rps | research 04 |
| DexScreener | 60 rpm (profiles/boosts/metas) | research 04 |
| Monthly budget targets | ~$110 minimum / ~$520–560 recommended / ~$2,100–2,400 pro | PLAN §12 |
| Priority rule | protection and unresolved orders before research and entries | master prompt §6 |

## Failure modes

- **Upgrading around a bug.** A duplicate listener doubles the burn and a bigger plan
  hides it for a month.
- **Retry storms.** Retrying into a 429 is what triggers an IP lock. The limiter's
  `penalty_level` is the early warning.
- **Client-side retries fighting the limiter.** A CLI with its own retry logic
  double-counts against the bucket. Coordinate or disable it.
- **Assuming free means free.** Webhook pushes, delivery costs and per-post billing are
  real.
- **Auditing without bounds.** The audit itself consumes budget. Read our own tables
  first; call the provider's usage endpoint once.
- **Stopping unrelated applications.** Other apps share this account's allowance; reserve
  headroom rather than taking it all.
- **Reading a provider outage as a market signal.** A silent feed is not a quiet market.

## What NOT to do

- **Do not rotate API keys or IPs to evade a rate limit or a ban.**
- **Do not retry into a 429.**
- **Do not recommend a plan without the exact price, renewal basis, quota, the blocked
  capability and the measured benefit.**
- **Do not change an existing paid plan** or cancel another application's usage without
  the operator's authorisation.
- **Do not starve protection or reconciliation to finish a research sweep.**
- **Do not quote a price from memory.** Prices moved several times in 2026; cite
  `docs/research/08-gating-and-budget-verification.md` or re-verify.
- **Do not treat a cap as a capability.** Hitting a quota is a scheduling problem;
  weight 10 against allowance 5 is a capability problem.
