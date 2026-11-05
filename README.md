# Kaiba Ultimate Agent

An autonomous on-chain trading and research agent for memecoin markets on **Solana**,
**BNB Chain** and **Robinhood Chain**. Deterministic Python services do the mechanical work:
ingest, scan, score, size, execute, protect, reconcile and journal. An LLM operator built on
[Hermes Agent](https://github.com/NousResearch/hermes-agent) sits on top for judgement,
research and reporting over Telegram. A Grok Bot routine can feed it X research and
launchable posts.

> **Read this first. This is experimental software that can trade real money, and it has not
> been shown to be profitable.**
> - **Live results.** On the author's 191 closed live fills, the mean return was **−14.2% per
>   trade**, with a 25% win rate.
> - **Confidence did not help.** Its own confidence measures (conviction score, number of smart
>   wallets, wallet grade) did *worse* the higher they were. Entry size is therefore flat.
> - **Why share it.** So you can study, copy and improve the *system*: the safety rails, the
>   measurement discipline and the agent setup. It is not a money printer.
>
> Nothing here is financial advice. Everything ships in **paper (shadow) mode**. Never fund it
> with money you cannot afford to lose.

**Start here:** [How it works](docs/HOW-IT-WORKS.md), then
[What you need to provide](docs/WHAT-YOU-NEED.md).

## What it does

| Area | What runs |
|---|---|
| **Ingest** | pump.fun / PumpPortal streams, GMGN smart-money + KOL + trending feeds, the Pons (Robinhood) and Flap / four.meme (BNB) launchpads, on-chain tape, DexScreener, X posts (twitterapi.io or J7Tracker), optional Telegram call rooms |
| **Scan** | Tiered token triage, then a dossier: holders, bundles, dev history, liquidity, tax, honeypot and RugCheck/GoPlus checks |
| **Lanes** | Pluggable strategies, each `off` / `shadow` / `canary` / `live`. Examples: `sm-trenches` (several independent smart-money wallets accumulating), `launch-snipe`, `tweet-launch`, `manual` |
| **Risk gate** | Flat sizing, per-chain bankroll, daily loss stop, basket exposure cap, kill switch, reduce-only, entries pause |
| **Execution** | GMGN swap API via `gmgn-cli`, with a signer policy that can only settle to wallets you declare |
| **Protection** | An always-on watchdog: stop-loss, take-profit ladder, trailing stop, rug, liquidity and stale-token exits, and blind-position halts. Exits always outrank entries |
| **Tweet auto-launch** | A watched account posts. The model picks launch-or-skip, name and ticker. A logo is made, and GMGN `cooking create` launches the token with a ~5% dev buy. The ladder then sells in steps. Ships in shadow |
| **Grok Bot intake** | Ed25519-signed research cards from a Grok Bot routine: tokens, NFT mints, airdrops, new chains. `launch` cards feed the tweet launcher. Every card goes through the same gates, in shadow |
| **Wallet intelligence** | Gathering, grading (A–D), clustering into entities, forward-validated cohorts |
| **Learning** | Journal, loss reviews with per-fill cost evidence, replays, signal audits, experiment gates (replay → shadow → promote / roll back) |
| **Hunters** | Airdrop / points, NFT drops, listings, social research inboxes, a DLMM LP lab. All research-only |
| **Ops** | Scheduler, WAL and disk guards, row retention, backups, Telegram trade cards and pages, a scripted daily report, a web console with a chat to the operator |

The withdrawal path is the one hard gate. No tool moves funds to an address you did not
declare in `config/signer-policy.yaml`.

## Quick start (paper mode)

Python 3.12. Node 24 only for `gmgn-cli`.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env            # fill in only the keys you have; most providers are optional
python -m pytest -q             # offline test suite (known failures listed below)
python deploy/run-local.py      # ingest + scan + engine + protection + ops + dashboard, paper only
```

Out of the box nothing can trade:
- `config/risk.yaml` has `global_mode: shadow`, `bounds.max_lane_mode: shadow` and
  `entries_paused: true`. Every chain is disabled, with a zero bankroll and no wallet.
- `config/schedule.yaml` has `execute_planned.enabled: false`.
- `config/tweet_launch.yaml` has every route `live: false` and Grok cards in shadow.

To see paper trades:
1. Set `entries_paused: false`.
2. Enable a chain with a **notional** bankroll.

The test suite runs against its own fixture config (`tests/fixtures/config/`, fake wallets)
via `tests/conftest.py`.

## What you need to provide

The full inventory, with the file that reads every value, is in
[docs/WHAT-YOU-NEED.md](docs/WHAT-YOU-NEED.md). In short:

| To... | You need |
|---|---|
| **Run it in paper mode** | A Linux box (Ubuntu 24.04; 4 vCPU / 8 GB RAM / 80 GB SSD recommended, and the database grows to tens of GB; Windows works for development), Python 3.12, Node 24 + `gmgn-cli`. Free data (DexScreener, Jupiter lite, PumpPortal, RugCheck, GoPlus, public RPCs) works with no keys. A **GMGN API key** is strongly recommended even for paper; the smart-money lanes have nothing to count without it |
| **Talk to it** | [Hermes Agent](https://github.com/NousResearch/hermes-agent) logged in to a model (Claude via `hermes auth add anthropic`; Codex as a fallback), and a Telegram bot token from @BotFather plus your numeric Telegram user id |
| **Trade live** | A paid GMGN plan with its request-signing key (`gmgn-cli config`), a GMGN-bound wallet per chain that **you** fund, your own addresses in `config/risk.yaml` and `config/signer-policy.yaml`, `GMGN_ALLOW_AUTOMATED_TRADES=1` on every process that submits, and a bankroll and daily stop you can afford to lose. Read `docs/runbooks/arming.md` first |
| **Auto-launch tokens from tweets** | A tweet feed: a twitterapi.io key (pay-per-use filter rule or the Stream plan), a J7Tracker session file, or Grok Bot launch cards. An AI namer (the logged-in Hermes profile, or `ANTHROPIC_API_KEY`). A logo source (the Codex CLI login, `OPENAI_API_KEY`, or free Pollinations, then a drawn badge). Capital for the ~5% dev buy: about 1.5 SOL / 0.6 BNB / 0.09 ETH per launch |
| **Feed it Grok research** | A Grok Bot routine with its own Ed25519 key pair; the **public** key goes into `~/.config/kaiba/grok-alpha-worker.json`. A Caddy route to the intake. See [docs/runbooks/grok-launch-cards.md](docs/runbooks/grok-launch-cards.md) |
| **Richer data (optional)** | Helius (Solana history), Etherscan (EVM wallet history), CoinGecko, GoPlus and RugCheck keys, codex.io. Each one is optional and named in the guide |

Credentials live only in `.env` or `~/.config/kaiba/` (both git-ignored), never in the repo.

## Tweet auto-launch and Grok

When a watched X account posts, `kaiba/execution/tweet_launch.py` runs these steps:
1. Decide whether the post is launchable.
2. Have the model pick a name and ticker.
3. Choose a logo. The post's own image is used only if it is roughly square. Otherwise an AI
   image, else a drawn badge.
4. Launch through GMGN `cooking create` with a dev buy of about **5% of total supply**.
5. Sell the bag in steps with the protection ladder, and sell everything after 60 s with no
   trades.

Posts come from the X stream (twitterapi.io or J7Tracker) or from **Grok Bot `launch` cards**.
Grok-sourced posts have their own age limit and their own `grok.mode` switch, so they stay in
shadow even if the launcher is armed. Operating notes:
- `skills/tweet-auto-launch/SKILL.md`
- `docs/runbooks/grok-launch-cards.md` (the card contract, and the instruction to give Grok)
- `docs/runbooks/tweet-three-chain-live.md`

Measured caveats (author's census of pump.fun launches, October 2026):
- **Only the first launch on a post shows any edge.** The first launcher graduated 3.9% vs
  1.3% for later ones, and the median first launch landed **23 s** after the post. Later
  launches were negative.
- **Grok cannot be first.** Its routine runs every ≥5 minutes, and the posts it cites were a
  median ~2 h old on arrival. Treat Grok as a measurement source, not a sniper.
- **The author's 45 live launches drew no buyers.** 44 were sold back by the no-volume exit,
  at a small net loss. On a fresh curve the dev buy is bounded: you are the first buyer.
- Nothing here shows the launcher makes money.

## Layout

```
kaiba/          the agent (core, providers, ingest, execution, intelligence, learning, hunters, ops, mcp, cli)
hermes/         Hermes Agent profiles: SOUL.md personalities + config for operator / research / reflect, cron jobs
skills/         agentskills.io-format SKILL.md folders the LLM operator uses
config/         risk / schedule / signer-policy / tweet_launch / social YAML — TEMPLATES, shipped in paper mode
dashboard/      operator console (FastAPI + HTMX + SSE)
deploy/         VPS install scripts, systemd units, Caddy front end, backups
docs/           how it works, what you need, trading method, contract, runbooks
tests/          pytest suite (fixtures never call live providers)
```

## Going live (only after you have measured it)

1. Create your own GMGN API key and signing key (`gmgn-cli config`). Keep them in
   `~/.config/kaiba/.env`, never in the repo.
2. Put **your** wallet address in `config/signer-policy.yaml` → `owned_addresses` and in
   `config/risk.yaml` → `chains.<chain>.wallet`.
3. Set a bankroll and a daily loss stop you can afford to lose. Enable one chain and move
   **one** lane from `shadow` to `live`.
4. Read `docs/runbooks/arming.md` and run `python deploy/preflight.py`.

## The LLM operator (Hermes)

`hermes/profiles/*/SOUL.md` define the three personalities:
- an **operator** you talk to on Telegram;
- a scheduled **research** agent;
- a nightly **reflect** agent.

`deploy/install-hermes.sh` installs them. The operator reaches the system through the MCP
server in `kaiba/mcp/`. Token names, posts and API responses are treated as data, never as
instructions. The operator may pause entries itself. It may not sell on its own judgement,
and it can never raise a lane past your ceiling.

## Safety rules worth copying even if you copy nothing else

- Never commit `.env`, keys or a database. `.gitignore` blocks the common key filenames.
- A strategy earns money only after a shadow run says it should, measured out of sample on the
  population you *scan*, not just the trades you took.
- Exits are never blocked by the brakes that stop entries.
- Never send a swap with `min_out = 0`.
- If you trade the same wallet by hand or with another bot, the agent's ledger will drift.

## Known state of this snapshot

Measured on this exact tree (Windows, Python 3.12): **14,724 tests pass**, 75 are skipped (opt-in
live-provider tests and POSIX-only checks) and 31 fail.
- 28 also fail in the private tree this snapshot was cut from: 19 in
  `tests/test_wallet_acquisition.py`, 4 in `tests/test_skills.py`, 3 in
  `tests/test_desktop_intel_sync.py`, 1 each in `tests/test_limiter_family_isolation.py` and
  `tests/test_gmgn_kline_readonly.py`.
- 3 read files that are not part of the snapshot: `tests/test_hermes_codex_fallback.py` reads
  `docs/PLAN.md`; `tests/test_paper_scoreboard_shared.py` reads the author's dashboard script;
  `tests/test_live_cashflow.py` reads one of the author's fill fixtures.

Good first issues if you want to help.

Not included:
- the author's research notes, research scripts, work logs and task board;
- tests and fixtures that hold the author's own fills, ledger snapshots or private data
  directories;
- one-off maintenance scripts for the author's server;
- the Binance wallet pairing tools;
- the tweet launcher's deployment receipts (`skills/tweet-auto-launch/references/readiness.md`
  is a stub, and exact amounts in the launch runbooks read `<amount>`);
- the J7Tracker account roster and the KOL registry (both ship header-only).

The author's wallet addresses, transaction ids, order ids, chat ids, host names and bot name
are replaced with fakes or placeholders throughout.

## License

MIT — see `LICENSE`.
