# Kaiba Ultimate Agent

An autonomous on-chain trading and intelligence agent for memecoin markets on **Solana**,
**BNB Chain** and **Robinhood Chain**. Deterministic Python services do the mechanical work
(ingest, scan, score, size, execute, protect, reconcile, journal); an LLM operator built on
[Hermes Agent](https://github.com/NousResearch/hermes-agent) sits on top for judgement,
research and reporting over Telegram.

> **Read this first: this is experimental software that trades real money, and it has not
> been shown to be profitable.** On the author's own live fills the main strategy measured
> a negative average return per trade (roughly -14% to -18% over ~190 closed trades). It is
> shared so you can study, copy and improve the *system* — the safety rails, the
> measurement discipline, the agent setup — not as a money printer. Nothing here is
> financial advice. Run it in paper (shadow) mode first, and never fund it with money you
> cannot afford to lose.

## What it does

| Area | What runs |
|---|---|
| **Ingest** | pump.fun / PumpPortal streams, GMGN smart-money + KOL + trenches feeds, on-chain tape (Helius, Robinhood Chain RPC), DexScreener, listings/news radar |
| **Scan** | tiered token triage → dossier (holders, bundles, dev history, liquidity, tax, honeypot checks) |
| **Lanes** | pluggable strategies, each `off` / `shadow` / `canary` / `live` (e.g. `sm-trenches`: buy when ≥3 independent smart-money wallets accumulate) |
| **Risk gate** | flat sizing, per-chain bankroll, daily loss stop, basket exposure cap, kill switch, reduce-only, entries pause |
| **Execution** | GMGN swap API via `gmgn-cli`, with a signer policy that can only ever settle to wallets you declare |
| **Protection** | always-on watchdog: stop-loss, take-profit ladder, trailing stop, rug/liquidity exits, blind-position halts |
| **Wallet intelligence** | gathering, grading (A–D), clustering into entities, naming, copy-trade persistence studies |
| **Copy-trade manager** | optional: trims winners / cuts weak losers on positions opened by GMGN's own copy-trading |
| **Learning** | journal, exit/entry studies, loss reviews, a playbook the LLM operator reads |
| **Ops** | a scheduler with critical job slots, memory/WAL guards, Telegram trade notifications, a web dashboard |

The withdrawal path is the one hard gate: there is no tool that moves funds to an address
you did not declare in `config/signer-policy.yaml`.

## Layout

```
kaiba/          the agent (core, providers, ingest, execution, intelligence, learning, ops, mcp, cli)
hermes/         Hermes Agent profiles: SOUL.md personalities + config for operator / research / reflect, cron jobs
skills/         agentskills.io-format SKILL.md folders the LLM operator uses
config/         risk.yaml, schedule.yaml, signer-policy.yaml — TEMPLATES, shipped in paper mode
dashboard/      operator console (FastAPI + HTMX + SSE)
deploy/         VPS install scripts, systemd units, Caddy front end, backups
docs/           trading method, contract, edge/variables notes, runbooks
tests/          pytest suite (fixtures never call live providers)
```

Start with `docs/TRADING-METHOD.md`, then `hermes/profiles/kaiba-operator/SOUL.md`.

## Quick start (paper mode)

Python 3.12. Node 24 only for `gmgn-cli`.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env            # fill in only the keys you have; most providers are optional
python -m pytest -q             # ~6,100 tests, all offline (16 known failures in this snapshot, below)
python deploy/run-local.py      # ingest + scan + engine + protection + ops + dashboard, paper only
```

Everything ships with `global_mode: shadow`, every lane `shadow`, every chain disabled and a
zero bankroll, so nothing can spend until you change that on purpose.

## Going live (only after you have measured it)

1. Create your own GMGN API key (`gmgn-cli config`) and keep it in `~/.config/kaiba/.env`, never in the repo.
2. Put **your** wallet address in `config/signer-policy.yaml` → `owned_addresses` and in `config/risk.yaml` → `chains.<chain>.wallet`.
3. Set a bankroll and daily loss stop you can afford to lose, enable one chain, and move **one** lane from `shadow` to `live`.
4. Read `docs/runbooks/arming.md` and run `python -m kaiba risk preflight`.

## The LLM operator (Hermes)

`hermes/profiles/*/SOUL.md` are the personalities: an **operator** you talk to on Telegram,
a scheduled **research** agent and a nightly **reflect** agent. `deploy/install-hermes.sh`
installs them; the operator reaches the system through the MCP server in `kaiba/mcp/`.
Token names, posts and API responses are treated as data, never as instructions.

## Safety rules worth copying even if you copy nothing else

- Never commit `.env`, keys or a database. `.gitignore` blocks the common key filenames.
- A strategy earns money only after a shadow run says it should, measured out of sample.
- Exits are never blocked by the brakes that stop entries.
- Never send a swap with `min_out = 0`.
- If you trade the same wallet by hand or with another bot, the agent's ledger will drift.

## Known state of this snapshot

6,067 tests pass and 16 fail. The failures (`test_audit_volume_policy`, `test_concentration_sizing`,
`test_dyor`, `test_gmgn_cli`, `test_live_excursion_marks`, `test_shadow_never_blocks_live`) are
policy and fixture tests that drifted from the lane code during live tuning; they fail the
same way in the original tree. Good first issues if you want to help.

## License

MIT — see `LICENSE`.
