# Provider limits, weights and prices

Verified 2026-09-19 in `docs/research/04-data-sources.md` and
`docs/research/08-gating-and-budget-verification.md`. Re-verify before quoting a price to
the operator; several of these moved during 2026.

## Shipped limiter settings (`config/risk.yaml` → `provider_budgets`)

| Provider | `min_interval_ms` | `capacity` | `refill_per_s` | `max_inflight` | Other |
|---|---|---|---|---|---|
| gmgn | 1,200 | 20 | 2.0 | 1 | weights `{quote: 10, swap: 10, default: 1}` |
| helius | 100 | 50 | 10.0 | 4 | `daily_credit_cap: 900000` |
| dexscreener | 1,100 | 60 | 1.0 | 2 | |
| solanatracker | 350 | 3 | 3.0 | 2 | |
| rugcheck | 500 | 10 | 2.0 | 2 | |
| goplus | 500 | 30 | 2.0 | 2 | |
| jupiter | 1,100 | 5 | 1.0 | 2 | |

Sustained requests per second for an endpoint = `refill_per_s ÷ weight`.

## GMGN

- Leaky bucket 20/20 since 2026-05-13, per-endpoint weights.
- Plan weights observed on this account 2026-09-13: **Free 5**, Plus 20, Pro 50.
- `order quote` and `swap` are weight 10 → **Free cannot execute at all**.
- Plus $29/mo or $290/yr. Pro $990/yr.
- Execution needs the Ed25519 request-signing key plus an IP allowlist; reads and
  `order quote` need only the API key.
- Wallets are custodial; `portfolio info` lists the wallets bound to the key; an unbound
  `--from` returns `TRADE_WALLET_MISMATCH` (403).
- The Agent API has **no** transfer, withdrawal, send or key-export operation. Value
  leaves a bound wallet only through `swap`, `multi-swap`, strategy orders and
  `cooking create`.
- Support notes: an upgrade does not clear an active IP lock; disable client retries.
- Router: 1 call / 5 s; anti-MEV needs a fee ≥ 0.002 SOL; IPv4 only.

## Helius

- Free: 1M credits, 10 RPS, webhooks, WSS, Sender at **0 credits**.
- Costs: webhook push 1 credit; `getTransactionsForAddress` 10 credits / 100 tx; Wallet
  API beta 100 credits/call.
- $49 Developer (10M, 50 RPS, gRPC devnet) · $499 Business (100M, 200 RPS, LaserStream
  mainnet, Identity labels) · $999 Pro · overage $5/M.

## Solana Tracker

- Free 10k req/mo, 3 rps. €50 Advanced (200k) · €200 Pro (1M) · €397 Premium (10M + WS
  Datastream sniper/bundler/insider/dev-holding rooms) · €599 Business.

## Others worth knowing

| Provider | Free | Paid | Note |
|---|---|---|---|
| DexScreener | keyless; 60 rpm profiles/boosts/metas | — | never needs paying for |
| Jupiter | keyless 0.5 rps; free key 1 rps | $25 / $100 / $500 | Ultra `/order` + `/execute` |
| RugCheck | free public report, insiders graph, rug SSE | — | |
| GoPlus | 150k CU/mo, 30k/day | $199+ | EVM-centric |
| Bitquery | 7-day trial | $39/mo annual, $79 Pro | **only** source with Pons/Robinhood, LaunchLab, DBC streams |
| Alchemy | 30M CU/mo, 25 rps, 5 webhooks | PAYG $0.525/1M CU | address-activity webhooks; verify the account's real CU interval before starting a collector |
| Nansen | 100 trial + 10/day | Pro ~$49 annual / $69 monthly; pay-per-use $0.01–$0.05/call | |
| Cielo | 5,000 credits/mo, `/feed` only | Builder $89 | |
| xAI SuperGrok | — | $30/mo (OAuth) or ≈$5 per 1k posts | per-post billing from 2026-09-21 |
| Subglow | trial | $99 Sniper (2 gRPC streams) | only after a lane is proven latency-bound |

## Retired or changed in 2026 (do not plan around them)

Dune Sim / Sim IDX retired 2026-08-01 · Flipside shut down · Moralis removed Solana
holders/discovery/sniper endpoints · Reservoir NFT API closed Oct 2025 · **Jito
ShredStream shut down 2026-09-05** · Hetzner raised prices June 2026 (CPX31 €62.49; no
Frankfurt location) · Helius cut streaming prices April 2026.

## Audit checklist

1. Duplicate listeners — is the same event arriving from two feeds?
2. Abandoned cron jobs still polling.
3. Historical backfills competing with live traffic.
4. Client-side retries fighting the shared limiter.
5. Polling where a webhook or websocket exists.
6. Payload size — are we asking for 200 rows to use 5?
7. Headroom reserved for other applications on the same account.
8. Priority: protection and unresolved orders ahead of research and entries.
