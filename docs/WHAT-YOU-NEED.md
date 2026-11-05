# What you need to provide

An inventory of every account, key, file, setting and machine requirement that Kaiba
Ultimate Agent reads, sorted by what you are trying to do:

- **Tier A: paper / shadow mode.** The agent watches markets, scores tokens and records
  paper trades. No money moves.
- **Tier B: live trading.** Everything in Tier A, plus what an order needs to reach a venue.
- **Tier C: optional upgrades.** Each one switches on a feature or improves the data.

Each row names the file that reads the value, so you can check it yourself. Prices
appear only where the repository documents them, with the date it was written. Where the
code or the docs do not settle a question, the row says **unknown**.

> Nothing in this repository has been shown to make money. The repo's own measurement,
> on 191 closed live fills, is a mean of **-14.2% per trade** with a 25% win rate. Run it
> in shadow mode and study it before you fund anything. This is not financial advice.

---

## 0. How configuration is read

| What | Where | Read by |
|---|---|---|
| Credentials and endpoints (the `Settings` class) | Environment variables, then the repo `.env`, then `~/.config/kaiba/.env`. Process environment wins; between the two files, the later one wins. `KAIBA_CONFIG_DIR` moves the second file. | `kaiba/core/config.py` |
| Money envelope: modes, bankrolls, wallets, stops, exits | `config/risk.yaml` (or `KAIBA_RISK_PATH`) | `kaiba/core/config.py`, `kaiba/execution/risk.py` |
| Withdrawal gate and "which addresses are ours" | `config/signer-policy.yaml` (or `KAIBA_SIGNER_POLICY_PATH`) | `kaiba/execution/policy.py` |
| Job schedule, feeds, retention, the live-entry switch | `config/schedule.yaml` (or `KAIBA_SCHEDULE_CONFIG`) | `kaiba/ops/scheduler.py` |
| Tweet auto-launcher | `config/tweet_launch.yaml` | `kaiba/execution/tweet_launch.py` |
| Social research inboxes | `config/social_follow.yaml`, `config/social_hunt.yaml` | `kaiba/hunters/social_follow.py`, `kaiba/hunters/social_hunt.py` |

You can run Kaiba in one of three layouts. Pick one before you start.

1. **Development / Windows / single machine.** Use `python deploy/run-local.py`, with
   `.env` and `config/` in the checkout (`deploy/run-local.py`, `README.md`).
2. **System-wide Linux install, the documented path.** `deploy/bootstrap.sh` then
   `deploy/install.sh` give you code in `/opt/kaiba`, credentials in
   `/etc/kaiba/{core,hermes,dashboard,signer,deploy}.env`, config in
   `/etc/kaiba/config/` and `/etc/kaiba/policy/`, and state in `/var/lib/kaiba`. The
   units are in `deploy/systemd/*.service`. Walkthrough: `docs/runbooks/deploy.md`.
3. **Per-user Linux layout.** A checkout in `~/kaiba` with `.venv`, credentials in
   `~/.config/kaiba/`, and `systemctl --user` units. The newer services (tweet launcher,
   Grok intake, social inboxes, guards, console) ship only in this form:
   `deploy/systemd/user/`, plus the `%h/...` units in `deploy/systemd/`. This repository
   does **not** include per-user unit files for the core services (engine, scan,
   protection, ingest, ops, mcp). You would have to write those yourself.

Check what you have configured at any time. `kaiba probe` contacts each core provider
once and prints names and statuses, never values (`kaiba/cli/probe.py`).
`deploy/preflight.py` is the full pre-arming gate.

---

## Tier A: paper / shadow mode

### A1. Host

| Item | Requirement | Source |
|---|---|---|
| OS | Ubuntu 24.04 LTS for the Linux installers. Development also runs on Windows (PowerShell commands in `README.md`) | `deploy/bootstrap.sh`, `deploy/install.sh`, `docs/runbooks/deploy.md` |
| CPU / RAM / disk | The repo recommends **4 vCPU, 8 GB RAM, 80 GB SSD** (author's notes, 2026-09-20) | see the next three rows |
| RAM, measured | The `MemoryMax=` limits of the nine always-on system units add up to about **6 GB**. Hermes alone has 2 GB; engine, scan and ingest have 768 MB each | `deploy/systemd/*.service` |
| Disk, measured | The author's live database reached **22 GB** on a 79 GB disk. It can write up to about 2.5 GB a day, and the WAL can spike to 3.5 GB. `kaiba-disk-guard` pages at 80% disk use, runs emergency retention at 85% and halts new entries at 92%. Swap history is kept 14 days | `kaiba/ops/disk_guard.py`, `config/schedule.yaml` `retention:` |
| Clock | NTP sync (`chrony`). Preflight fails when clock skew exceeds 2 s, because GMGN request signing is timestamped | `deploy/preflight.py` (`check_clock`) |
| Location | The repo suggests Frankfurt or Amsterdam: the workload is bound by provider API round trips, so a latency race is not the goal. At about €15–25/month (per the author's notes, 2026-09-20) | author's notes |
| Packages | `python3.12 python3.12-venv sqlite3 rsync chrony age git` | `docs/runbooks/deploy.md` |

### A2. Software

| Item | Notes | Source |
|---|---|---|
| Python 3.12 | `pip install -e ".[dev]"` for development. The installer uses the `production` extra (`telethon`, `mcp<2`, `solders`, `eth-account`, `cryptography`). The `mcp<2` pin matters: `mcp` 2.x breaks the server import | `pyproject.toml`, `deploy/install.sh` |
| Node 24 + `gmgn-cli` | `npm i -g gmgn-cli` (the repo notes version 1.6.1). Needed for any GMGN data and for every trade. The CLI is found through `GMGN_CLI_PATH`, then `PATH`, then `%APPDATA%\npm\...` on Windows | `docs/runbooks/deploy.md`, `.env.example`, `kaiba/providers/gmgn_cli.py` |
| Node JIT exemption | The hardened units set `MemoryDenyWriteExecute=yes`, which makes `gmgn-cli` (V8) exit silently. List each unit that runs it in `KAIBA_ALLOW_NODE_JIT` in `/etc/kaiba/deploy.env` before you install | `deploy/env/deploy.env.example`, `deploy/install.sh`, `deploy/systemd/dropins/allow-node-jit.conf` |

### A3. Config files to check before first start

| File / key | Set it to | Why |
|---|---|---|
| `config/risk.yaml` `global_mode` | `shadow` (as shipped) | A lane runs at the **lowest** of its lane mode, `global_mode` and `bounds.max_lane_mode` (`RiskConfig.effective_mode`, `kaiba/core/config.py`) |
| `config/risk.yaml` `bounds.max_lane_mode` | `shadow` (as shipped) | This is your ceiling. The agent cannot raise it: `save_risk` re-reads `bounds` from disk |
| `config/risk.yaml` `entries_paused` | `false` once you want paper decisions | Ships `true`, so nothing is entered until you choose to |
| `config/risk.yaml` `chains.<chain>.enabled`, `bankroll_base_units` | `true` plus a **notional** bankroll for each chain you want to paper-trade (native base units: lamports for SOL, wei for EVM chains) | Ships disabled with bankroll 0. Paper trades are sized too, and a zero bankroll refuses every decision as `size_not_positive:bankroll_zero` |
| `config/risk.yaml` `chains.<chain>.wallet` | empty for paper; your own GMGN-bound address for live | Ships empty |
| `config/schedule.yaml` `execute_planned.enabled` | `false` (as shipped) | This is the only route from a decision to a venue |
| `config/tweet_launch.yaml` `mode`, `armed_by`, `chains.<chain>.live`, `grok.mode` | `shadow`, `""`, `false`, `shadow` (as shipped) | Shadow records launch decisions and sends nothing |
| `config/signer-policy.yaml` `owned_addresses` | empty (as shipped), or your own addresses for live | An empty list makes the signer refuse everything |

A system-wide install starts from an **empty** `/etc/kaiba/config/risk.yaml`
(`deploy/bootstrap.sh`), which loads as all defaults: shadow mode, no chains, bankroll 0.
To get paper fills you still need to write a chain block.

### A4. Data that works without any key

| Source | Used for | Reader |
|---|---|---|
| DexScreener | Prices, liquidity, pairs on every chain | `kaiba/providers/dexscreener.py` |
| Jupiter (`lite-api.jup.ag`) | Executable Solana quotes for paper fills | `kaiba/providers/jupiter.py` |
| CoinGecko (keyless tier) | Native-coin USD price series | `kaiba/providers/native_price.py` |
| GoPlus (keyless, lower rate limit) | Token security | `kaiba/providers/goplus.py`, `kaiba/intelligence/dyor_verify.py` |
| RugCheck (public tier) | Solana token reports | `kaiba/providers/rugcheck.py` |
| PumpPortal websocket (free streams) | Live pump.fun launches and migrations | `kaiba/ingest/pumpportal.py` |
| pump.fun frontend API | Per-token trade tape. **Note:** this API answered 403 (Cloudflare) from the original host from 2026-10-23 onward | `kaiba/ops/scheduler.py` |
| Public RPCs (defaults) | `SOLANA_RPC_URL` = `api.mainnet-beta.solana.com`, `ROBINHOOD_RPC_URL` = `rpc.mainnet.chain.robinhood.com`, `HYPEREVM_RPC_URL` = the official Hyperliquid EVM RPC. BSC/Base/ETH readers fall back to a public table when unset | `kaiba/core/config.py`, `kaiba/intelligence/dyor_verify.py` (`PUBLIC_RPC`) |

### A5. GMGN API key (read-only data). Strongly recommended even for paper

| | |
|---|---|
| What | An API key from your GMGN account: GMGN portal → API keys |
| Goes in | `GMGN_API_KEY` in `.env` / `~/.config/kaiba/.env` (or `/etc/kaiba/core.env`). **`gmgn-cli` also reads `~/.config/gmgn/.env` with override on**, so a different key in that file silently wins (`kaiba/providers/gmgn_cli.py`, `credential_source`) |
| Used by | Smart-money, KOL, trending and signal feeds (`kaiba/ingest/gmgn_feeds.py`, the GMGN part of `config/schedule.yaml`), token info and holder data in the scanner, wallet portfolio and PnL, protection's last-resort price |
| Cost | The repo describes a free tier with "weight 5 per window" in which a quote or swap costs 10, so execution is impossible on free (the author's notes and `kaiba/providers/jupiter.py`, 2026-09-20) |
| Without it | Every GMGN call returns `UNAVAILABLE`. The smart-wallet lanes have nothing to count, and `kaiba probe` (and therefore preflight) reports GMGN not ok. DexScreener and PumpPortal still feed the scanner |

### A6. Console password (only if the dashboard is reachable from anything but localhost)

| | |
|---|---|
| Goes in | `KAIBA_DASHBOARD_PASSWORD`, in `/etc/kaiba/dashboard.env` (system layout) or `~/.config/kaiba/dashboard.env` (per-user). `deploy/set-console-password.sh` writes the per-user file: at least 12 characters, no quotes, `$` or backticks. Changing it signs every session out |
| Read by | `dashboard/auth.py`, `dashboard/app.py` |
| Without it | The dashboard refuses every client that is not on loopback |
| Ports | `KAIBA_DASHBOARD_HOST`/`KAIBA_DASHBOARD_PORT` default to `127.0.0.1:8788` (`kaiba/core/config.py`). The per-user console unit uses 8794 |

---

## Tier B: live trading

Tier B assumes everything in Tier A. Read `docs/runbooks/arming.md` before you start. It
sets out the evidence the repo itself requires before a lane moves from shadow to canary
to live (for example, 7 or more days and 30 or more shadow trades with positive
expectancy after costs).

### B1. GMGN for execution

| Item | Goes in | Read by | Cost (per repo docs) | Without it |
|---|---|---|---|---|
| Paid GMGN plan on the account that owns your API key | GMGN account | — | "GMGN Plus — $29/month" (author's notes, 2026-09-20) | Quotes and swaps cannot fit inside the free-tier weight budget |
| Request-signing key (Ed25519). This is **not** a wallet key | Run `gmgn-cli config`. The value goes in `GMGN_PRIVATE_KEY` | `kaiba/execution/standing.py`, `kaiba/providers/gmgn_cli.py` | — | Signed endpoints return 401. Standing blocker `gmgn_signing_key_missing` |
| Trading wallet bound to the API key, per chain: one Solana address and one EVM address. The EVM address serves BSC, Robinhood, Base and ETH | Bind it in the GMGN portal, put the address in `config/risk.yaml` `chains.<chain>.wallet`, and fund it | `kaiba/execution/executor.py` (`submit_gmgn`): "Custodial wallets; the API has no transfer endpoint" | Your capital | `ExecutionRefused: no wallet bound for <chain>`. A wallet the key is not bound to is refused by the venue |
| Automation flag | `GMGN_ALLOW_AUTOMATED_TRADES=1` in the environment of **every process that submits**: engine, protection (it sends every sell), ops (it runs `execute_planned`) and the tweet launcher | `kaiba/execution/standing.py`, `kaiba/execution/executor.py` | — | `gmgn-cli` stops at a confirmation prompt and the order is refused. The author once had an exit deadlock for 13 minutes this way. Only `deploy/systemd/kaiba-tweet-launch.service` sets the flag. The system units do not, so put it in `/etc/kaiba/core.env` |

### B2. Risk envelope (`config/risk.yaml`)

| Key | Meaning |
|---|---|
| `global_mode`, `bounds.max_lane_mode`, `lanes.<lane>.mode` | `off` / `shadow` / `canary` / `live`. A lane runs at the lowest of the three. Raise all three deliberately, one lane and one chain at a time |
| `chains.<chain>.enabled`, `wallet` | Turns the chain on, and names the GMGN-bound address |
| `chains.<chain>.bankroll_base_units` | The capital the sizer works from (lamports or wei). The sizer uses *equity*, which compounds down with realised losses (`kaiba/execution/risk.py`) |
| `min_position_base_units` / `max_position_base_units` | Per-trade floor and cap. If the lane's size band cannot clear the floor, that chain silently sizes to zero (`size_not_positive:below_min_position:...`) |
| `gas_reserve_base_units` | Native coin held back for fees |
| `daily_loss_stop_base_units` | When today's realised loss (UTC) reaches this, new entries halt and exits continue. This is the stop that is enforced. `bounds.max_daily_loss_pct` is declared but read by no code |
| `max_exposure_pct`, `bounds.max_total_exposure_pct`, `bounds.max_size_pct_bankroll` | Per-token cap, whole-basket cap, and per-trade cap |
| `bounds.max_slippage_bps` / `max_exit_slippage_bps` | 2500 for entries, 8800 for exits in the repo copy. Every swap is sent with `--anti-mev` |
| `size_by_market_cap`, `drawdown_recovery` | Optional ladders |
| `kill_switch`, `entries_paused`, `reduce_only` | Brakes. These stop entries and never block exits |
| `protection:` | Exit ladder. The repo copy has a -30% stop (`stop_loss_bps: 3000`), take-profits at 2x/5x/10x, trailing stops, a rug check (liquidity -40%), a stale exit after an hour with no volume, and a 20% moon bag |

The agent (Hermes, via MCP) may move numbers **inside** `bounds`. It cannot widen
`bounds`, because `save_risk` keeps the on-disk block. It may lower a lane at any time.
Raising a lane to canary or live through `kaiba_set_lane_mode` requires a quoted
`owner_request` (free text, not a cryptographic approval) and stays under
`bounds.max_lane_mode` (`kaiba/mcp/server.py`). If you never want the agent to arm
anything itself, keep `bounds.max_lane_mode: shadow` until you change it by hand. Also
note: `save_risk` drops any key that the model does not declare.

### B3. Turn on the entry path

| Item | Where |
|---|---|
| `execute_planned.enabled: true` | `config/schedule.yaml`. Shadow lanes still send nothing, because `executor._check_mode` re-reads `risk.yaml` at submit time |
| Supervised first pass | `kaiba ops run --once --only execute_planned --force` (from the comment in `config/schedule.yaml`) |

### B4. Signer policy and the local signer

| Item | Goes in | Read by | Notes |
|---|---|---|---|
| Your own addresses | `config/signer-policy.yaml` `owned_addresses.<chain>` (Solana is case-sensitive, EVM is not) | `kaiba/execution/policy.py`, `kaiba/execution/executor.py`, `kaiba/ops/scheduler.py` | These are consulted by the local-signer ("direct") route, by the manual sell-from-another-wallet override, and by the copy manager. GMGN swaps are checked against `chains.<chain>.wallet` instead. Set both |
| Local signer keys (system layout) | `kaiba signer keygen --chain sol|evm --keystore /etc/kaiba/signer/keys`, run as `kaiba-signer`. It prints only the address | `kaiba/execution/signer.py`, `kaiba/cli/main.py` | The engine routes orders through GMGN (`provider="gmgn"`, `kaiba/execution/engine.py`). The signer still has to answer, and to refuse a test withdrawal, for preflight to pass. The keystore is plain 0600 files. `KAIBA_SIGNER_PASSPHRASE` is read by no code (`docs/runbooks/keys.md`) |
| Signer network allow-list | `KAIBA_RPC_ALLOW_CIDRS` in `/etc/kaiba/deploy.env`. Empty means loopback only (fails closed) | `deploy/install.sh` | Only needed if the signer itself must reach an RPC |

### B5. RPC endpoints that protection relies on

Exits are priced and checked on chain. The public defaults work, but they share rate
limits with everyone else on the internet.

| Variable | Default | Read by |
|---|---|---|
| `SOLANA_RPC_URL` | public mainnet-beta | `kaiba/core/config.py` `rpc_for`, used by `pumpswap_price.py`, `fills.py`, `fill_costs.py`, `snipe.py`, `tweet_launch.py`, `launch_preflight.py` |
| `BSC_RPC_URL` | empty → `bsc-dataseed.binance.org` for readers with a public fallback | `rpc_for`, `kaiba/execution/evm_price.py`, `kaiba/ingest/feed_endpoints.py` |
| `ROBINHOOD_RPC_URL` | public Robinhood Chain RPC | `rpc_for`, `kaiba/execution/snipe.py` |
| `ETH_RPC_URL`, `BASE_RPC_URL` | empty | `rpc_for`. The `eth` and `base` chains ship disabled |

### B6. Before the first live order

- `deploy/preflight.py` must exit 0. It checks migrations, the journal chain, the envelope,
  wallets bound and funded, the signer (including refusing a withdrawal), the provider
  probe, the Hermes profiles, clock skew and the unix boundaries. The probe step needs
  every non-optional target green: GMGN, Helius, Birdeye, Etherscan, Alchemy and Telegram
  (`kaiba/cli/probe.py` `TARGETS`). See the disagreement list at the end: under current
  code that step cannot pass.
- Set up Telegram alerts first (C2). Pages such as halts, a blind protection loop or a
  full disk go there.
- Start protection before the engine (`docs/runbooks/deploy.md` §9).

### B7. Tweet auto-launcher, live (optional; it spends the most per event)

| Item | Where | Notes |
|---|---|---|
| Launcher mode | `config/tweet_launch.yaml` `mode: live`, `armed_by: "<who, when>"`, and `chains.<chain>.live: true` | `kaiba/execution/tweet_launch.py` |
| Lane mode | `config/risk.yaml` `lanes.tweet-launch.mode: live`, with the chain listed in `lanes.tweet-launch.chains` | `executor._check_mode` |
| Capital per launch | The dev buy targets 5% of supply. Modelled at about 1.485 SOL (pump.fun), 0.293 BNB (Flap) plus 0.304 BNB (four.meme), and 0.089 ETH (Pons), capped at 1.5 SOL / 0.5 BNB / 0.1 ETH (`config/tweet_launch.yaml`, 2026-10-06/07). **This lane deliberately skips the flat per-trade size cap** (`tweet_launch._admission`). Its bounds are `max_buy_native`, `daily_native_cap` and `daily_launch_cap`, and the repo copy sets the two daily caps to `unlimited`. Set real caps |
| A tweet feed and an AI namer | See C3 and C1/C7 | With `namer.require_ai: true`, no model answer means no launch |

---

## Tier C: optional upgrades

### C1. Hermes operator: chat, Telegram and MCP tools

| Item | Goes in / how | Read by | Cost (per repo) | Without it |
|---|---|---|---|---|
| Hermes Agent (NousResearch) | `curl -fsSL https://hermes-agent.nousresearch.com/install.sh \| bash`, then `deploy/install-hermes.sh` as the Hermes service user. This installs three profiles (`kaiba-operator`, `kaiba-research`, `kaiba-reflect`), the MCP wiring and the cron fleet | `deploy/install-hermes.sh`, `hermes/profiles/*/config.yaml`, `hermes/cron/jobs.yaml` | — | No chat operator. The trading pipeline still runs |
| Claude login (primary model) | `hermes --profile <each profile> auth add anthropic` (OAuth, stored in Hermes' own home) | Hermes | The profile comment says this path "requires a Claude Max plan with purchased usage credits" (`hermes/profiles/kaiba-operator/config.yaml`, 2026-09-21). No price stated | Hermes cannot answer. The tweet namer then falls through to `ANTHROPIC_API_KEY` |
| Codex login (fallback model) | `hermes --profile <each profile> auth add openai-codex` (device-code flow). Do not copy `~/.codex/auth.json` | Hermes | not documented | No fallback when Claude is unavailable |
| Telegram bot | Create one with @BotFather. Run `hermes --profile kaiba-operator gateway setup` and paste its token. The system layout keeps `TELEGRAM_BOT_TOKEN` in `/etc/kaiba/hermes.env` | Hermes gateway, `kaiba/cli/probe.py` (`getMe`) | free | No Telegram chat with the agent |
| Allowed Telegram users | `TELEGRAM_ALLOWED_USERS` = your numeric user id(s). The unit also sets `TELEGRAM_ALLOW_ALL_USERS=false` | The Hermes gateway (Kaiba code declares it but reads nothing from it) | — | Unknown senders are ignored |
| MCP socket | `KAIBA_MCP_SOCKET` (`/run/kaiba/mcp/kaiba.sock` in the units). 44 `kaiba_*` tools | `kaiba/mcp/socket_bridge.py`, `kaiba/mcp/server.py` | — | — |
| GMGN skill pack (optional) | `npx skills add GMGNAI/gmgn-skills` into `.agents/skills` | `hermes/profiles/kaiba-operator/config.yaml` | — | The profile warns that `gmgn-swap` and `gmgn-token-buy` bypass Kaiba's risk gates. Route trades through Kaiba |

### C2. Telegram notifications and pages (separate from the Hermes chat)

| Variable | Goes in | Read by | Without it |
|---|---|---|---|
| `KAIBA_TELEGRAM_BOT_TOKEN` (or `TELEGRAM_BOT_TOKEN`) | Process environment, then `/etc/kaiba-hermes/telegram.env`, then `~/.config/kaiba/notify.env` | `kaiba/ops/trade_notify.py`, `kaiba/ops/scheduler.py`, `kaiba/ingest/wallet_track.py` | Trade cards, daily reports and pages are not sent. Trading continues "quietly" |
| `KAIBA_TELEGRAM_CHAT_ID` (or `TELEGRAM_HOME_CHANNEL`) | same | same | same |
| `KAIBA_TELEGRAM_CHANNEL_ID` / `TELEGRAM_TRADES_CHANNEL` | same | `trade_notify.channel_id` | Optional public channel. Cards go there without transaction hashes |
| `~/.config/kaiba/notify.env` | **Must exist** for the per-user `kaiba-db-guard` and `kaiba-disk-guard` units, which list it as a required `EnvironmentFile` | `deploy/systemd/user/kaiba-{db,disk}-guard.service` | Those guard units fail to start |

The notifier runs as `python -m kaiba.ops.trade_notify`. The repo includes no unit file
for it.

### C3. X / Twitter feeds (tweet launcher, alpha research, social inboxes)

| Item | Goes in | Read by | Cost (per repo) | Without it |
|---|---|---|---|---|
| twitterapi.io key | `TWITTERAPI_IO_KEY`, from the environment, then `~/.config/kaiba/twitterapi-research.key`, then `~/.config/kaiba/.env` | `kaiba/execution/tweet_launch.py`, `kaiba/providers/x_search.py`, `skills/twitterapi-research/scripts/research.py` (the stream itself is `kaiba/ingest/x_stream.py`) | Stream Starter "$29/mo" for 6 accounts, Growth "$79/mo" for 20 (`config/tweet_launch.yaml`, 2026-10-06). Search is about $0.15 per 1,000 posts (`kaiba/providers/x_search.py`, 2026-10-05) | Launcher feed error `missing_twitterapi_key`. `kaiba_x_search` is unavailable |
| Feed backend | `config/tweet_launch.yaml` `feed.backend`: `twitterapi_monitor` (the code default; it subscribes the configured handles over REST), `twitterapi_rule` (filter rules **you** create in the twitterapi.io dashboard; the code never creates rules) or `j7` | `kaiba/ingest/tweet_launch_feed.py` | — | One websocket per key, with a 90 s reconnect floor. Do not run two consumers on one key |
| Research spend cap | A shared ledger at `~/.config/kaiba/social-research-budget.json`, capped in code at 2,000,000 credits lifetime and 50,000 per UTC day. `docs/runbooks/social-follow-snipe.md` equates that to $20 / $0.50 | `skills/twitterapi-research/scripts/research.py` (`reserve`), `kaiba/providers/social_follow.py` | — | Requests are refused once the cap is reached |
| Official X API v2 | `X_BEARER_TOKEN` | `kaiba/providers/x_search.py` (the second choice) | about $0.005 per post read (`x_search.py`, 2026-10-05) | twitterapi.io only |
| J7Tracker session | A J7 login JWT, written to **`~/.config/kaiba/j7.session`** (one line, mode 600, owned by the ingest user). Ask J7 how to get the feed session token. It is **not** J7's wallet "API keys". Never put a wallet key into Kaiba | `kaiba/ingest/j7_feed.py` (file only. If `J7_SESSION_JWT` is set in its environment, it refuses and pages), `kaiba/ingest/j7_launch_bridge.py` (prefers `J7_SESSION_JWT`, then the file) | unknown. Needs a J7 plan that allows the feed. 5 connections per account | `j7_feed` stays idle. `kaiba_tweets` has no posts |
| J7 extras | `pip install -e ".[social]"` (python-socketio + aiohttp). Optional `J7_FEED_REGION=dfw` | `pyproject.toml`, `kaiba/ingest/j7_feed.py` | — | `missing_socketio_dependency` |

Full J7 procedure: `docs/runbooks/j7-feed.md`.

### C4. Grok Bot research intake (signed cards)

| Item | Goes in | Read by | Notes |
|---|---|---|---|
| Ed25519 key pair | The **private** key stays with the sender (your Grok Bot routine). The **public** key goes into `~/.config/kaiba/grok-alpha-worker.json` as `{"port": 17876, "public_key_hex": "<64 lowercase hex>"}` | `kaiba/hunters/grok_alpha_bridge.py` (`serve` reads `--settings`) | A malformed key refuses to start (`intake_public_key`) |
| Service | `kaiba-grok-alpha` (per-user unit). It listens on `127.0.0.1:<port>` and writes `data/grok-alpha/inbox.db` | `deploy/systemd/kaiba-grok-alpha.service` | Needs `cryptography` (installed by the `signer`/`production` extra) |
| Public route | Import `deploy/grok-alpha.caddy` inside your console's Caddy site block. `/grok-alpha/*` is proxied to `127.0.0.1:17876`, with a body limit of 256 KB | `deploy/grok-alpha.caddy` | `deploy/grok-alpha-install.py` is hard-wired to the author's host (a placeholder here). Do the steps by hand |
| Request format | `POST /grok-alpha/v1/observations` or `/v1/heartbeat`, with headers `X-Kaiba-Timestamp` (13-digit ms, within ±5 min), `X-Kaiba-Nonce` (32 hex characters, single use) and `X-Kaiba-Signature` (base64 Ed25519 over `kaiba.grok.delivery/1\n<ts>\n<nonce>\n<sha256-hex of body>`). Sources must be `x.com`/`twitter.com` status URLs. Schema: `kaiba.grok.observation/1` | `grok_alpha_bridge.py`, `skills/grok-alpha-research/references/handoff.md` | Cards then go to `kaiba/hunters/grok_alpha_processor.py`. Token cards become `submit_agent_intent` on the `manual` lane, which follows that lane's mode (shadow by default) |
| Grok side | Grok Bot's built-in X tools. The repo says no xAI developer API key is used (`docs/runbooks/grokbot-alpha-handoff.md`) | — | Routine schedules are at least 5 minutes apart, so this is not a real-time feed |
| Launch cards (track `launch`) | One launchable X post per card: exact text, publish time, `pbs.twimg.com` image URLs | `kaiba/ingest/grok_launch_feed.py`, read by the tweet launcher every `grok.poll_s` | Grok-sourced launches stay in shadow unless `grok.mode: live` **and** the launcher is armed. Card contract and the instruction to give Grok: `docs/runbooks/grok-launch-cards.md` |

### C5. Keyed data providers

| Variable | Feature | Reader | Cost (per repo) | Without it |
|---|---|---|---|---|
| `HELIUS_API_KEY` | Solana history, DAS token accounts, priority fees, the NFT hunter | `kaiba/providers/helius.py`, `kaiba/hunters/nft.py` | Free 1,000,000 credits/month at 10 RPS. Developer $49/month for 10M (author's notes, 2026-09-20). The code stops at the monthly allowance instead of going into overage | `UNAVAILABLE` receipts. Thinner Solana wallet grading. Preflight's probe fails (Helius is a required probe target) |
| `HELIUS_WEBHOOK_SECRET` | HMAC check on Helius webhooks | `kaiba/providers/helius.py`, `kaiba/intelligence/tracker.py` | — | The webhook receiver accepts nothing (fails closed) |
| `ETHERSCAN_API_KEY` | EVM wallet history (eth/bsc/base/robinhood) for grading | `kaiba/providers/evm_history.py` | not documented | No EVM wallet indexes. Preflight's probe fails |
| `JUPITER_API_KEY` | Switches quotes to the metered `api.jup.ag` host | `kaiba/providers/jupiter.py` | not documented | The keyless `lite-api` host |
| `COINGECKO_API_KEY` | Higher limit on native-price samples | `kaiba/providers/native_price.py` | not documented | Keyless tier |
| `GOPLUS_APP_KEY`, `GOPLUS_APP_SECRET` | Higher GoPlus rate limit | `kaiba/providers/goplus.py`, `kaiba/intelligence/dyor_verify.py` | free (per the author's notes, 2026-09-20) | Keyless, slower |
| `RUGCHECK_JWT` | Authenticated RugCheck | `kaiba/providers/rugcheck.py` | free (same doc, 2026-09-20) | Public tier |
| `CODEX_IO_API_KEY` (+ `KAIBA_CODEX_IO_CU_CEILING`, default 8,000 CU) | Research prices, screens, swap tape (codex.io) | `kaiba/providers/codex_io.py`, `kaiba/hunters/radar.py`, `kaiba/execution/evm_pool_price.py` | Plan allowance 10,000 CU per 30 days (code constant). No price stated | Research reads are unavailable |
| `PUMPPORTAL_API_KEY` | Metered `subscribeTokenTrade` streams | `kaiba/ingest/pumpportal.py` | not documented | Free streams only |
| `BIRDEYE_API_KEY` | **Only** `kaiba probe` uses it. No adapter reads Birdeye | `kaiba/cli/probe.py` | free tier exists (same doc, 2026-09-20) | Nothing changes in the pipeline, but preflight's probe check fails |
| `ALCHEMY_API_KEY` | Disabled in code. `kaiba/providers/provider_policy.py` has `ALCHEMY_NETWORK_ENABLED = False` and no environment bypass. Every Alchemy host is refused in the shared HTTP and websocket paths | `kaiba/cli/probe.py`, `kaiba/ingest/feed_endpoints.py` | — | A key does nothing unless you change that policy. Alchemy-only feeds (`rh_wallets`, `sol_wallets`, `leader_exit`, and `flap` without a public socket) stay idle |

### C6. Extra feed and pool endpoints

| Variable | Feature | Reader |
|---|---|---|
| `KAIBA_FEED_PUBLIC_FALLBACK` (comma list of feeds) | Lets the `leader_exit`, `sol_wallets` and `flap` feeds use public endpoints. Empty by default, so those feeds idle | `kaiba/ingest/feed_endpoints.py` |
| `SOLANA_FEED_WS_URL`, `SOLANA_FEED_HTTP_URL` | Feed-only Solana endpoints. They must not share a host with protection | same |
| `BSC_SNIPE_WS_URL` (no default), `BSC_SNIPE_RPC_URL` | Flap launch feed and BSC snipe reads | same, `kaiba/ingest/launch_feed.py` |
| `KAIBA_FEED_SHARED_WS_OK=bsc`, `KAIBA_FEED_PREFER_PUBLIC`, `KAIBA_FEED_ALLOW_SHARED_ALCHEMY` | Host-sharing switches | same |
| `SOLANA_FEED_ALCHEMY_URL`, `ROBINHOOD_FEED_ALCHEMY_URL` | Keyed feed slots. Alchemy is blocked by policy (C5) | same |
| `BSC_POOL_RPC_URL`, `ETH_POOL_RPC_URL`, `BASE_POOL_RPC_URL` | Paper pool reads. Unset eth/base reads are refused, never quietly swapped for another host | `kaiba/execution/evm_pool_price.py` |
| `HYPEREVM_RPC_URL`, `ELYSIUM_RPC_URL` | Observed-only networks (not tradable). An empty Elysium value means no request | `kaiba/ops/network_status.py` (via `kaiba/core/config.py`) |

### C7. AI for the tweet launcher (naming and logos)

| Item | Goes in | Reader | Without it |
|---|---|---|---|
| Hermes one-shot (first namer) | The `kaiba-operator` profile logged in (C1). Binary at `~/.local/bin/hermes` | `kaiba/execution/tweet_creative.py` | Falls back to the next backend |
| `ANTHROPIC_API_KEY` (second namer) | Process environment / `~/.config/kaiba/.env` | `tweet_creative.py` | No AI name. With `require_ai: true`, no launch |
| Codex CLI login (logo backend `codex`) | `codex` on `PATH`, logged in under `~/.codex` (or `CODEX_HOME`) | `tweet_creative.py` | Falls back to Pollinations (free, keyless), then a drawn badge |
| `OPENAI_API_KEY` (logo backend `openai`) | Only when `openai` is listed in `logo.backends` | `tweet_creative.py` | Same fallbacks |

### C8. Telegram call-room listener (userbot, opt-in)

`TG_API_ID`, `TG_API_HASH` (from my.telegram.org), `TG_SESSION_PATH` (default
`~/.config/kaiba/telethon.session`, created by the QR login) and `TG_CALL_CHANNELS`
(comma list). Read by `kaiba/ingest/telegram_calls.py` inside `kaiba-ingest`. Needs the
`telegram` extra. Calls become `ALPHA_CALL` events and never trigger a buy. The repo flags
that Telegram's terms restrict harvesting channel data for ML use, which is why this is
opt-in. Without these values the listener idles ("no call channels configured").

### C9. TLS and a remote console

| Item | Goes in | Source |
|---|---|---|
| `KAIBA_DASHBOARD_HOSTNAME`, `KAIBA_ACME_EMAIL`, optional `KAIBA_DASHBOARD_UPSTREAM`, `KAIBA_TRUSTED_CIDRS` | Caddy's own environment (`systemctl edit caddy`). Install with `deploy/install.sh --with-caddy` | `deploy/Caddyfile` |
| The caddy-ratelimit plugin | `xcaddy build --with github.com/mholt/caddy-ratelimit`, or remove the `[PLUGIN]` blocks. Never leave `/control/*` without one of the two | `docs/runbooks/deploy.md` |
| Private console instead of a public one | A Tailscale account on its free personal plan. Run `deploy/private-console.sh` | `deploy/private-console.sh` |

### C10. Backups (`/etc/kaiba/deploy.env`)

`KAIBA_DEPLOYMENT_NAME`; `AGE_RECIPIENTS`, `AGE_RECIPIENTS_FILE` or `GPG_RECIPIENT`
(**public** keys only; keep the private half on your laptop); `KAIBA_BACKUP_KEEP_DAYS`;
optional `RCLONE_REMOTE` with `/etc/kaiba/rclone.conf`. Read by `deploy/backup.sh` and
`kaiba-backup.service`. Wallet keys are never backed up (`docs/runbooks/keys.md`).

### C11. Feature switches (environment)

| Variable | Effect | Reader |
|---|---|---|
| `KAIBA_ENABLE_NFT_HUNTER=1` | Allows the NFT hunter (off by default; the repo measured mint-everything at -85.4%) | `kaiba/hunters/nft.py` |
| `KAIBA_DYOR_CLUSTER_GATE=enforce` | Turns the holder-cluster rules from report-only into enforcing | `kaiba/intelligence/dyor.py` |
| `KAIBA_OPERATOR_HOURLY_USD`, `KAIBA_HUNTER_EV_THRESHOLD_USD` | Hunter EV thresholds (override the `hunters:` block in `risk.yaml`) | `kaiba/hunters/ev.py` |
| `KAIBA_SIGNALS_CONFIG` | Path to an optional `signals.yaml` | `kaiba/hunters/signals.py` |
| `KAIBA_LIVE_TESTS=1` | Runs the `@live` provider tests | `pyproject.toml` |

### C12. Binance Agentic Wallet (prepared, not armed)

A separate worker (`kaiba-baw-worker`, settings in `/etc/kaiba-baw/worker.json`) and a CLI
adapter (`kaiba/providers/binance_wallet_cli.py`). The runbook states automatic execution
is **not** implemented or armed (the author's runbook is not part of this snapshot). Treat
it as reading material.

---

## Declared, but read by nothing today

These appear in `.env.example`, `kaiba/core/config.py` and/or `deploy/rotate-keys.sh`, but
no code reads them. Setting them has no effect:

`XAI_API_KEY`, `OPENROUTER_API_KEY`, `SOLANA_TRACKER_API_KEY`, `BITQUERY_TOKEN`,
`ALCHEMY_WEBHOOK_SIGNING_KEY`, `BLOCKSCOUT_API_KEY`, `NANSEN_API_KEY`, `CIELO_API_KEY`,
`MOBULA_API_KEY`, `TWITTERSCORE_API_KEY`, `CRYPTOPANIC_API_KEY`, `NEWSAPI_KEY`,
`DUNE_API_KEY`, `VYBE_API_KEY`, `GOLDRUSH_API_KEY`, `GOLDSKY_API_KEY`, `MORALIS_API_KEY`,
`FINNHUB_API_KEY`, `FRED_API_KEY`, `GITHUB_API_TOKEN`, `PHOENIX_COLLECTOR_ENDPOINT`,
`LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST`, `TELEGRAM_BOT_USERNAME`,
`KAIBA_SIGNER_PASSPHRASE`. (Hermes' own `x_search` toolset may read an xAI key from
Hermes' environment. That is not verified in this repository.)

`KAIBA_ENV` does not choose a trading mode, whatever `.env.example` says. Only the
dashboard reads it: `prod` warms its caches.

---

## Where the docs and the code disagree (follow the code)

1. **Preflight cannot pass its provider check as written.** `kaiba probe` treats Birdeye
   and Alchemy as required. Birdeye is used by nothing else, and Alchemy is refused in
   code.
2. **`rotate-keys.sh` puts `TG_API_ID`/`TG_API_HASH` in `hermes.env`**, but their reader runs
   in `kaiba-ingest`, which loads `core.env`. Its catalogue also lacks `TWITTERAPI_IO_KEY`,
   `X_BEARER_TOKEN`, `GMGN_ALLOW_AUTOMATED_TRADES` and the `KAIBA_TELEGRAM_*` names.
3. **No system unit sets `GMGN_ALLOW_AUTOMATED_TRADES=1`.** The author's box used a
   `gmgn-automated.conf` drop-in, which is not in `deploy/systemd/dropins/`: add the variable yourself.
4. **`docs/runbooks/deploy.md` says `signer keygen --chain robinhood`.** The CLI accepts only
   `evm` or `sol`, and needs `--keystore` (`kaiba/cli/main.py`).
5. **Passphrase rotation.** `docs/runbooks/keys.md` and `rotate-keys.sh` describe it
   (`KAIBA_SIGNER_PASSPHRASE`, `signer rekey/retire`). None of it is implemented, and
   keys.md says so.
6. **J7.** The `kaiba/ingest/x_stream.py` docstring says J7 is "not wired".
   `kaiba/ingest/tweet_launch_feed.py` supports `backend: j7`.
7. **Telegram notifier.** `/etc/kaiba-hermes/telegram.env` (the notifier's fallback file) is
   created by no installer, and `kaiba-notify` has no unit file in the repo.

## Unknown

- Current prices for GMGN, Helius, twitterapi.io, X API and Claude/Codex plans. The figures
  above are the repo's own, with their dates.
- The price of J7Tracker and how long a J7 session token lasts (`docs/runbooks/j7-feed.md`:
  "not measured yet").
- The exact unit definitions for running the core services in the per-user layout.
- Whether the GMGN free tier still has the weight limits described on 2026-09-20.
