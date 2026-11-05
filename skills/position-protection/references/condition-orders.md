# Provider-native condition orders and the watchdog contract

## GMGN condition orders

Attached on the swap call (`gmgn-cli swap ... --condition-orders '<json>'`). Types, from
`docs/research/04-data-sources.md`:

| Type | Meaning | Key parameters |
|---|---|---|
| `profit_stop` | fixed take profit | `price_scale`, `sell_ratio` |
| `loss_stop` | fixed stop loss | `price_scale`, `sell_ratio` |
| `profit_stop_trace` | trailing take profit | `price_scale`, `sell_ratio`, `drawdown_rate` |
| `loss_stop_trace` | trailing stop loss | `price_scale`, `drawdown_rate` |

Operational facts that shape how we use them:

- Automated trades require `GMGN_ALLOW_AUTOMATED_TRADES=1` and `--yes`.
- Execution needs the Ed25519 request-signing key plus an IP allowlist; reads and
  `order quote` need only the API key.
- Quote and swap are weight 10 each. The Free plan's allowance is 5, so **the Free plan
  cannot quote or swap at all**; Plus (weight 20) can. See `provider-budget-audit`.
- The router accepts one call per 5 s, and anti-MEV requires a fee of at least
  0.002 SOL. Fine for copy and protection; too slow for slot-0 sniping.
- `order strategy create|list|cancel` manages strategy orders; `order get` is the
  reconciliation read.

Mapping our `config/risk.yaml` ladder onto them:

| Our setting | Condition order |
|---|---|
| `stop_loss_bps: 3000` | `loss_stop`, `price_scale` = −30%, `sell_ratio` = 100% |
| `tp_ladder` rung `[2.0, 50]` | `profit_stop`, `price_scale` = 2×, `sell_ratio` = 50% |
| `trailing` rung `[5.0, 2500]` | `profit_stop_trace`, activation 5×, `drawdown_rate` = 25% |
| `emergency_loss_bps: 5000` | watchdog only — a venue order at −50% would race the −30% stop |
| `rug_liquidity_drop_pct: 40` | watchdog only — liquidity is not a price condition |
| `anti_wick_min_ratio: 0.7` | watchdog only — the venue fills at its own price |

The split is the point: the venue holds the price-triggered protections so they survive
our downtime, and the watchdog holds everything that needs a second data source or a
re-quote.

## The direct lane

Jupiter Ultra, PumpPortal and the EVM routers have no server-side condition orders. On
that lane the watchdog is the only protection, which is a reason to prefer the GMGN lane
for positions we intend to hold, and a reason the watchdog must never be the thing that
was skipped "because GMGN has it".

## Watchdog contract

- Poll interval 5 s, evaluated on **executable value** (a real quote), not a displayed
  price.
- Persist: high-water mark, `tp_done` rungs, protection identifiers, ownership of each
  quantity.
- One owner per quantity. Cancel before replace. Never two live exits for the same units.
- On restart: reconcile from chain plus journal before acting
  (`../../incident-recovery/SKILL.md`), then re-attach protection.
- `ACQUIRED_UNPROTECTED` is an explicit state with a bounded repair-or-exit response, not
  a log line.
- Partial exits resize remaining protective orders; they never orphan them.

## Position states worth distinguishing

| State | Meaning |
|---|---|
| `protected: true` | venue order confirmed and/or watchdog owns every rung |
| `protected: false` | `ACQUIRED_UNPROTECTED` — repair immediately, exit if repair fails |
| `tp_done: [...]` | rungs already sold; a restart must not resell them |
| closed | `closed_ms` set; appears in `kaiba_positions(include_closed=true)` |

## Cost reality (Solana, 2026-09-19, SOL $111.51)

Landed Jito tip p50 ≈ $0.0003, p75 ≈ $0.0012, p95 ≈ $0.014, p99 ≈ $0.062; base fee
≈ $0.0006. A normal memecoin swap costs $0.001–0.01; viral competitive landing
$0.02–0.10; sniper tips 0.001–0.01 SOL. An exit that nets less than its own cost is not
a take profit — price the round trip, not the entry.
