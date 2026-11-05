# Dossier blockers, warnings and unknowns

`TokenDossier.tradeable` is `not self.blockers`. Nothing else overrides it.

## Field map

| Dossier field | Type | Notes |
|---|---|---|
| `price_usd`, `liquidity_usd`, `market_cap_usd`, `volume_24h_usd` | `Measure` | short freshness budget |
| `holder_count`, `top10_pct` | `Measure` | provider-sourced; record the denominator |
| `dev_pct`, `insider_pct`, `bundler_pct`, `sniper_pct`, `cluster_pct` | `Measure` | our entity graph plus provider cross-checks |
| `buy_tax_bps`, `sell_tax_bps` | `Measure` | EVM mainly; > 1,000 bps blocks |
| `rug_ratio` | `Measure` | creator's historical rug share |
| `mint_authority_revoked`, `freeze_authority_revoked`, `can_sell` | `bool \| None` | **`None` is unknown, not false** |
| `lp_burned_pct` | `Measure` | burned or locked both count; record which |
| `blockers`, `warnings` | `list[TokenRisk]` | closed enum |
| `unknowns` | `list[str]` | field names we could not read |
| `graded_wallets`, `entity_count` | evidence | independence lives in `entity_count` |
| `receipts` | `list[Receipt]` | provider, endpoint, observed_at, basis — one per number |

## Decision table

```
if any hard blocker:            reject, journal, done
elif cluster_pct in (20, 30]:   review — needs an explicit non-infrastructure explanation
elif any review condition:      downgrade the dossier grade, allow only reduced size
elif unknowns is non-empty:     state the count; a required-field unknown is a blocker
else:                           pass to trade-intent
```

## Hard blockers

| `TokenRisk` | Trigger |
|---|---|
| `HONEYPOT` | sell simulation fails |
| `MINT_AUTHORITY` | not revoked |
| `FREEZE_AUTHORITY` | not revoked |
| `LP_NOT_BURNED` | LP neither burned nor locked |
| `HIGH_TAX` | buy or sell tax > 1,000 bps |
| `DEV_CONCENTRATION` | dev-attributable supply > 10% |
| `CLUSTER_CONCENTRATION` | unexplained cluster > 30% |
| `RUG_HISTORY` | creator rug ratio > 0.30 |
| `TRANSFER_HOOK` | Token-2022 hook / non-transferable / default-frozen state |

## Review conditions

| `TokenRisk` | Trigger |
|---|---|
| `CLUSTER_CONCENTRATION` | 20–30% unexplained |
| `BUNDLER_EXPOSURE` | > 20% bundled or currently held |
| `SNIPER_EXPOSURE` | > 10% |
| `TOP10_CONCENTRATION` | > 15% |
| `INSIDER_EXPOSURE` | snipers with a funding link to the creator cluster |
| `DEV_SOLD` | creator sold before graduation |
| `METADATA_MUTABLE` | mutable metadata authority |
| `WASH_TRADING` | WT1/WT2 patterns present |
| `LOW_LIQUIDITY` | below the lane's floor |
| `PROVIDER_CONFLICT` | two providers disagree materially |
| `UNKNOWN_SAFETY` | a required safety field is unreadable |

## Sources consulted (each writes a `Receipt`)

| Source | Gives |
|---|---|
| GMGN `token info` / `token security` / `token holders` / `token traders --tag` | market, authorities, tagged holder composition |
| GoPlus | EVM token security, malicious-address database |
| RugCheck | free report, insiders graph, insider networks, rug SSE stream |
| Solana Tracker | risk score 1–10, first buyers with PnL, deployer history |
| DexScreener | pairs, liquidity, boosts/profiles/CTO |
| Our entity graph | `cluster_pct`, `bundler_pct`, `insider_pct`, `entity_count` |
| Our grader | `graded_wallets` |

## Third-party score calibration

| Tool | Scale | Reading |
|---|---|---|
| RugCheck | 0–100+ | < 30 low, 30–60 moderate, > 60 high |
| Solana Tracker | 1–10 | ≥ 8 unsafe |
| Solana Tracker weights | points | rugged 20,000 · bundlers ≥1,000 wallets 15,000 · snipers >50% 10,000 (>10% → 3,000) · dev >50% 10,000 · freeze 7,500 · single holder >90% 7,000 · top-10 >15% 5,000 · LP not burned 4,000 · mint authority 2,500 |

None of these replaces the blocker table. They are cross-checks that produce a
`PROVIDER_CONFLICT` warning when they disagree with us.
