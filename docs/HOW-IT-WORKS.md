# How Kaiba works

Kaiba Ultimate Agent is a Python 3.12 system that watches memecoin markets on Solana, BNB
Chain and Robinhood Chain. It scores tokens and wallets, decides whether a strategy
("lane") wants a position, and can buy and sell through GMGN's CLI. An LLM operator
(Hermes) sits on top and talks to you on Telegram.

**Please read this first.** The code does not demonstrate an edge. The repo's own
measurement over 191 closed live trades is a mean of **-14.2% per trade** with a 25% win
rate. It also found that its own confidence measures (conviction score, number of smart
wallets, wallet grade) are *anti*-calibrated: higher confidence did worse. It is shared as
a system to study. Run it in shadow (paper) mode. It is not financial advice.

## The pipeline

```
 feeds ──► ingest ──► triage ──► scanner ──► lanes ──► engine.decide ──► RiskGate
 (launches, (kaiba-    (tier-0    (dossier:   (signals)  (one decision    (mode, bankroll,
  GMGN, X,   ingest)    filter)    price, liq,            per signal)      caps, daily stop)
  wallets)                         holders, safety)                             │
                                                                         planned order
          ┌─────────────────────────────────────────────────────────────────────┘
          ▼
   execute_planned (kaiba-ops) ──► executor ──► gmgn-cli swap --anti-mev ──► venue
                                      │  signer-policy check; shadow = paper fill, never sent
                                      ▼
                                  reconcile ──► position ──► protection watchdog
                                                                │ TP ladder, -30% stop,
                                                                │ trailing, rug/stale exits
                                                                ▼
                                                   sell at EXIT priority ──► reconcile
                                                                ▼
                               learning: loss review, replays, signal audit, reports
```

1. **Ingest** (`kaiba/ingest/`). Sources: pump.fun launches over PumpPortal, GMGN
   smart-money / KOL / trending feeds, the Pons (Robinhood) and Flap (BNB) launchpads,
   tracked wallets, X posts (twitterapi.io or J7Tracker), and optionally Telegram call
   rooms. Everything goes into one SQLite database (WAL mode).
2. **Triage and scan** (`kaiba/execution/scanner.py`, `kaiba/intelligence/`). A cheap
   tier-0 filter comes first. The tokens that pass get a dossier: price and liquidity
   (DexScreener, on-chain pool reads), holders and bundles, GoPlus/RugCheck safety
   checks, and creator history.
3. **Lanes** (`config/risk.yaml` `lanes:`). These are the strategies, such as
   `sm-trenches`, `confluence-5`, `launch-snipe`, `trusted-copy`, `tweet-launch` and
   `manual`. Each one runs `off`, `shadow`, `canary` or `live`.
4. **Engine and risk gate** (`kaiba/execution/engine.py`, `risk.py`). Each signal becomes
   one decision. The gate checks mode, bankroll, the per-token / basket / per-trade caps,
   the daily loss stop and the round-trip cost ceiling, then sizes the position. Size is
   deliberately flat, not scaled by confidence.
5. **Execution** (`kaiba/execution/executor.py`). The `execute_planned` job sends planned
   orders through `gmgn-cli swap` from your GMGN-bound wallet with anti-MEV on, after the
   order body passes the signer-policy check.
6. **Protection** is a separate service that starts *before* the engine. It prices every
   open position on a short poll and runs the exit ladder: take-profits, a -30% stop, a
   trailing stop, rug and graduation handling, a stale-token exit and a moon bag. Its
   sells outrank every entry in the rate limiter.
7. **Reconcile** turns GMGN order reports into fills, positions, fees and slippage.
8. **Learning** (`kaiba/learning/`): loss reviews, replays, signal audit, experiments.
   The repo is candid that this has not yet produced a validated edge.

## The operator: Hermes on Telegram plus MCP tools

Hermes (NousResearch's agent runtime) runs three profiles. `kaiba-operator` chats on
Telegram. `kaiba-research` and `kaiba-reflect` run scheduled jobs. The primary model is
Claude, via Hermes OAuth, with a Codex OAuth fallback. Hermes reaches Kaiba through an MCP
server with 44 `kaiba_*` tools: status, positions, signals, wallets, token scans, tweets,
pause, lane modes and more. In the system-wide install it runs as its own Unix user,
which cannot read the database, the keys or the provider credentials. It may lower a lane
or pause entries on its own. To raise a lane it needs a quoted owner request, and it can
never go past the ceiling in `risk.yaml` `bounds`.

## Tweet auto-launcher (ships in shadow)

```
 watched X post ─► score (fresh? image? author?) ─► AI: launch or skip, name, ticker
       │                                              (Hermes one-shot, then Anthropic API)
       ▼
 logo: the post's image, else Codex/OpenAI/Pollinations, else a drawn badge
       ▼
 GMGN `cooking create` on pump.fun / Flap + four.meme / Pons, with a ~5%-of-supply dev buy
       ▼
 position in the `tweet-launch` lane ─► sell in steps at 1.3x/1.6x/2x/3x;
                                        sell everything after 60 s with no trades
```

Settings live in `config/tweet_launch.yaml`. In shadow it only records what it *would*
have launched. If you arm it: one launch costs about 1.5 SOL / 0.6 BNB / 0.09 ETH, and
this lane skips the flat per-trade size cap.

## Grok Bot research intake

A Grok Bot routine posts research "cards" (NFT mints, tokens, airdrops, new chains), each
signed with an Ed25519 key, to `/grok-alpha/*` behind Caddy. Kaiba checks the signature,
the timestamp and a single-use nonce, then stores the card in its own inbox. Every five
minutes a processor turns token cards into ordinary `manual`-lane intents. Those pass
through every gate and stay paper while that lane is in shadow. NFT and airdrop cards go
to the opportunities registry for review.

**Launch cards** (track `launch`): one launchable X post per card, with its exact text,
publish time and images. The tweet auto-launcher reads them every few seconds and runs
them through the same picker → logo → plan path. Grok-sourced launches have their own age
limit and their own `grok.mode` switch, so they stay in shadow even when the launcher is
armed. Grok scans every 15 minutes at best, while the first launch on a viral post
usually lands within ~25 seconds. Treat Grok posts as a measurement source, not a sniper.
Card contract: `docs/runbooks/grok-launch-cards.md`.

## Dashboard

A FastAPI console (`dashboard/`): status, positions, signals, wallets, paper scoreboard,
Grok pipeline, pause / reduce-only / kill controls, and a chat with Hermes. Localhost
only until a password is set. Put Caddy (TLS) or Tailscale in front.

## Safety rails

- **Shadow by default.** A lane's effective mode is the *lowest* of its own mode, the
  global mode and your ceiling (`bounds.max_lane_mode`). Shadow orders become paper fills
  and are never sent. `execute_planned.enabled` is a separate switch for whether any
  decision can reach a venue at all.
- **No withdrawal path.** `config/signer-policy.yaml` can only *narrow* what may be
  signed. Transfers, `transferFrom`, approvals-for-all and bridges are hard-denied in
  code, and GMGN's API has no transfer endpoint.
- **Brakes stop entries, never exits.** The kill switch, pause, reduce-only and the daily
  loss stop (realised PnL, UTC day) all refuse new buys while protection keeps selling.
- **Self-tuning cannot widen the envelope.** Saving `risk.yaml` keeps the on-disk `bounds`.
- **Host guards.** A WAL guard, and a disk guard: page at 80%, prune at 85%, halt at 92%.

Next: `WHAT-YOU-NEED.md`, then `docs/runbooks/deploy.md`, then `docs/runbooks/arming.md`.
