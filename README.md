# Kaiba Ultimate Agent

An autonomous on-chain trading and intelligence agent for memecoin markets on **Solana**,
**BNB Chain** and **Robinhood Chain**. Deterministic Python services do the mechanical work
(ingest, scan, score, size, execute, protect, reconcile, journal); an LLM operator built on
[Hermes Agent](https://github.com/NousResearch/hermes-agent) sits on top for judgement,
research and reporting over Telegram.

> **Read this first: this is experimental software that trades real money, and it has not
> been shown to be profitable.** On the author's own live fills the main strategy measured
> a negative average return per trade (roughly -14% to -18% over ~190 closed trades; about
> -9% per trade over the last 30 days on Robinhood Chain alone). It is shared so you can
> study, copy and improve the *system* — the safety rails, the measurement discipline, the
> agent setup — not as a money printer. Nothing here is financial advice. Run it in paper
> (shadow) mode first, and never fund it with money you cannot afford to lose.

## What it does

| Area | What runs |
|---|---|
| **Ingest** | pump.fun / PumpPortal streams, GMGN smart-money + KOL + trenches feeds, on-chain tape (Helius, Robinhood Chain RPC), DexScreener, listings/news radar |
| **Scan** | tiered token triage → dossier (holders, bundles, dev history, liquidity, tax, honeypot checks) |
| **Lanes** | pluggable strategies, each `off` / `shadow` / `canary` / `live` (e.g. `sm-trenches`: buy when ≥3 independent smart-money wallets accumulate) |
| **Risk gate** | flat sizing, per-chain bankroll, daily loss stop, basket exposure cap, kill switch, reduce-only, entries pause, per-chain launchpad allowlists |
| **Execution** | GMGN swap API via `gmgn-cli`, with a signer policy that can only ever settle to wallets you declare |
| **Protection** | always-on watchdog: stop-loss, take-profit ladder, trailing stop, rug/liquidity exits, blind-position halts |
| **Wallet intelligence** | gathering, grading (A–D), clustering into entities, naming, forward-validated "proven" cohorts |
| **Copy-trade manager** | optional: trims winners / cuts weak losers on positions opened by GMGN's own copy-trading |
| **Learning** | journal, loss attribution, entry features, experiment gates (replay → shadow → promote / rollback), a playbook the LLM operator reads |
| **Hunters** | airdrop / points, NFT drops, listings and early-alpha radar, summarised in a daily digest |
| **Ops** | a scheduler with critical job slots, memory/WAL/disk guards, core-state backups, row retention, Telegram trade notifications, a scripted daily report, a web dashboard |

The withdrawal path is the one hard gate: there is no tool that moves funds to an address
you did not declare in `config/signer-policy.yaml`.

## What's new in this snapshot

- **Protection fixes.** A position whose wallet balance is already zero can no longer halt
  entries on every chain; the rate limiter waits out a minimum-interval refusal for stop and
  position reads instead of failing them; limiter-refused sells retry promptly; the GMGN
  fallback price read runs at exit priority with no stale grace; and a LOG-ONLY on-chain pool
  price (`kaiba/execution/onchain_pool.py`, `kaiba/learning/onchain_vs_feed.py`) records how
  far the feed mark lags the pool before anyone trusts it for a stop.
- **Experiment gates / self-learning.** `kaiba/learning/experiment_loop.py` is the scheduled
  runner that turns a proposal into a verdict: replay gate → shadow arm → promote or roll
  back, with a relative-improvement criterion for tightenings. It decides nothing itself; every
  verdict is arithmetic on recorded rows in `kaiba/learning/gates.py`. Lane signals now carry
  ~35 point-in-time entry features so a proposed filter can be measured before it ships.
- **Proven-wallet cohorts.** `kaiba/learning/proven.py` freezes, per chain, the wallets whose
  *copy* returns persist out of sample at our real lag and fees (grades alone did not predict
  on Solana). `confluence-5` can count only those wallets (`wallet_source: proven`); it ships
  in shadow.
- **Loss attribution.** `kaiba/learning/loss_attribution.py` splits each losing trade into
  entry vs exit cost from recorded rows only, and ranks what would have told them apart.
- **Paper NFT mint study.** `kaiba/learning/mint_study.py` + `kaiba/ingest/nft_tape.py`:
  would auto-minting OpenSea drops on Robinhood Chain make money? Spends nothing — no wallet,
  no signer, no transaction — it records paper mints against a pre-registered rule.
- **Hunters digest.** `kaiba/hunters/digest.py`: the few new airdrop, NFT, listing and alpha
  leads worth a human look today, ranked, read-only, no model.
- **Scripted daily report.** `kaiba/ops/daily_report.py` prints the operator's daily numbers
  (losses, wallet funnel, paper mints, hunters) from the database alone, so a report still
  arrives when every model provider is down. MCP read tools return the same figures.
- **Signer hardening.** `signer keygen` / `signer import` write keys 0600 with O_EXCL into an
  explicit keystore (an existing key is never overwritten), read an imported key from a hidden
  prompt rather than the command line, and print only the address. The signer's systemd unit
  now sets the environment names the code actually reads — it had been silently ignored —
  and `tests/test_signer_unit_env.py` fails if they drift again.
- Also: core-state backup + restore drill, row retention (ships disabled), GMGN wallet-tag
  rollups, bounded wallet jobs, incremental wallet naming, watch-only balance reports for
  wallets you hold no key for.

## Layout

```
kaiba/          the agent (core, providers, ingest, execution, intelligence, learning, hunters, ops, mcp, cli)
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
python -m pytest -q             # ~7,300 tests, all offline (known failures in this snapshot, below)
python deploy/run-local.py      # ingest + scan + engine + protection + ops + dashboard, paper only
```

Everything ships with `global_mode: shadow`, every lane `shadow`, every chain disabled and a
zero bankroll, so nothing can spend until you change that on purpose. The test suite reads its
own fixture config (`tests/fixtures/config/`, fake wallets) via `tests/conftest.py`.

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

7,257 tests pass, 40 are skipped (live-provider tests, opt-in) and 73 fail. Every one of the 73
fails the same way in the private tree this snapshot was cut from: they are policy and fixture
tests that drifted from the lane and wallet code during live tuning (`test_source_row_veto` 29,
`test_source_qualification` 9, `test_lanes` 9, `test_live_excursion_marks` 7,
`test_wallet_failure_reporting` 4, `test_dyor` 3, and one or two each in `test_gmgn_cli`,
`test_dashboard`, `test_concentration_sizing`, `test_watchdog_quote_provenance`, `test_tracker`,
`test_tape_job_failure_visibility_diagnostic`, `test_shadow_never_blocks_live`,
`test_nft_mint_study` and `test_audit_volume_policy`). Good first issues if you want to help.

Not included: tests that import the author's private data directory, captures of the author's
own wallets and ledger, and a one-off bookkeeping repair for one of the author's positions.
The author's own wallet addresses, transaction ids and chat ids are replaced with fakes throughout.

## License

MIT — see `LICENSE`.
