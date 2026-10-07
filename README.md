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

## Tweet auto-launch (new)

When a watched X account posts, `kaiba/execution/tweet_launch.py` decides whether the post is
launchable, picks a name and ticker from it (a model, with a deterministic fallback), and
launches a token through GMGN `cooking create` from the agent wallet with a dev buy of about
**5% of total supply** inside the creation transaction. The watchdog's ladder then sells the
bag in increments. Operating notes for the LLM operator: `skills/tweet-auto-launch/SKILL.md`
(plus `launch-metadata`, `launch-logo-generation`, `twitterapi-research`, `j7-social-tracking`).

**It ships in paper mode.** `config/tweet_launch.yaml` has `mode: shadow`, an empty
`armed_by`, every chain `live: false` and vamps in shadow: every post is decided and recorded
in `tweet_launches` (launch or skip, with reasons) and nothing is sent.

### What you need

| Need | Where it goes | Notes |
|---|---|---|
| GMGN API key **and** the GMGN API request-signing private key | `~/.config/gmgn/.env` (`GMGN_API_KEY`, `GMGN_PRIVATE_KEY`), read by `gmgn-cli` | Never in the repo. The signing key authenticates API calls; it is not a wallet key. |
| A wallet bound to that GMGN key, funded per chain | GMGN account; its address in `config/risk.yaml` `chains.<chain>.wallet` and `config/signer-policy.yaml` `owned_addresses` | A 5% dev buy is modelled at about **1.485 SOL** on pump.fun, **0.2933 BNB** on Flap, **0.0893 ETH + 0.0005 ETH launch fee** on Pons V2, plus gas. The template caps are 1.5 SOL / 0.5 BNB / 0.1 ETH per launch and 6 SOL / 1 BNB / 0.3 ETH per UTC day, 5 launches per day. A launch whose exact 5% quote exceeds the cap is skipped, never undersized. |
| `GMGN_ALLOW_AUTOMATED_TRADES=1` | the launcher's systemd unit (already set in `deploy/systemd/kaiba-tweet-launch.service`) | `gmgn-cli` refuses unattended `--yes` without it. Every unit that can submit needs it, including protection (it does the sells). |
| A post feed: `TWITTERAPI_IO_KEY` | `~/.config/kaiba/.env` | Either a twitterapi.io **`tweet_filter` rule** (pay-per-use credits; create and activate the rule on twitterapi.io, `feed.backend: twitterapi_rule`) or the **Stream plan** (account monitoring, `feed.backend: twitterapi_monitor`, then `python -m kaiba.execution.tweet_launch sync-accounts`). One socket per key. Optional alternative: J7Tracker (`feed.backend: j7`, `J7_SESSION_JWT`, `pip install "python-socketio[client]"`). |
| Optional: the post picker's model | a logged-in Hermes profile (`namer.hermes_profile`), or `ANTHROPIC_API_KEY` in `~/.config/kaiba/.env` | Without either, the deterministic word picker is used. |
| Optional: logos for posts without an image | `OPENAI_API_KEY` | Otherwise Pollinations (free, rate-limited). The author's profile picture is never used. |
| RPC endpoints | `SOLANA_RPC_URL`, `BSC_RPC_URL`, `ROBINHOOD_RPC_URL` in `.env` | Used by launch preflight and by protection's price reads. |
| Database tables | migrations `044_tweet_launches.sql`, `045_tweet_refs.sql`, `046_tweet_sources.sql` | `python -m kaiba db init` applies pending migrations. |
| Services | `deploy/systemd/kaiba-tweet-launch.service` (the launcher; the only process that opens the feed socket) and `deploy/systemd/kaiba-tweet-sources.service` (record-only: which post each new market launch came from) | Copy to `~/.config/systemd/user/`, `systemctl --user daemon-reload`, `enable --now`. |
| Config | `config/tweet_launch.yaml`; the `tweet-launch` lane block in `config/risk.yaml`; the `protection.lanes.tweet-launch` ladder in `config/risk.yaml` | The template ladder sells 20% / 25% / 33% / 50% of what remains at 1.3x / 1.6x / 2x / 3x, and everything after 60 s with no trade on the curve. |

### Order of operations

1. Deploy the code first (including `kaiba/core/schemas.py`, which declares the `tweet-launch`
   lane) and **restart every Kaiba service**. Only then add the `tweet-launch` block to an
   existing `config/risk.yaml`: a service still running the old schema cannot parse a lane it
   does not know. (A fresh install from this snapshot already has both.)
2. Apply the migrations, install the two units, start them, and run in **shadow** for a while.
   `python -m kaiba.execution.tweet_launch report` summarises what it would have done;
   `... plan --author <handle> --text "..."` dry-runs one post with no I/O.
3. To arm one chain: `config/tweet_launch.yaml` `mode: live` + `armed_by` (who, when) + that
   chain's `live: true`; in `config/risk.yaml` the chain enabled with a wallet and bankroll and the
   `tweet-launch` lane `mode: live`. The kill switch, reduce-only, paused-entries and halted-day
   brakes still apply; the flat per-trade size cap does not (the dev buy is bounded by the launch
   caps above instead).

### Holder fees

- **BNB / Flap:** works. A dividend tax with all of the tax paid to holders (the template uses
  1% buy/sell tax). Rewards accrue to a dividend contract; verify a real accrual or claim.
- **Solana / pump.fun:** through GMGN only **Cashback** is supported, which pays fees to
  traders, not holders.
- **Robinhood / Pons:** no holder-fee mechanism through GMGN.

### Measured caveats (read before arming)

- Tweet-launched tokens graduate **less** often than the baseline launch (8.1% vs 10.3% in our
  census; reaching 2x: 11.5% vs 24.1%).
- The only cell with a visible edge is **being first**: the first launcher on a multi-launch
  post graduated 3.9% vs 1.3% for later ones, and that comparison carries lookahead (counting
  every first launcher, 1.8%). The median first launch lands 23 s after the post.
- Copycats / vamps of an existing launch graduate about **0.86%** of the time vs ~9.2% for
  originals (published study), and every later-than-first launch on a post was negative in our
  census. Vamping ships in shadow.
- Nothing here shows the launcher makes money. It is shared as a measured, bounded experiment.

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
config/         risk.yaml, schedule.yaml, signer-policy.yaml, tweet_launch.yaml — TEMPLATES, shipped in paper mode
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
python -m pytest -q             # ~8,700 tests, all offline (known failures in this snapshot, below)
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

8,678 tests pass, 40 are skipped (live-provider tests, opt-in) and 17 fail. Every one of the 17
fails the same way in the private tree this snapshot was cut from: 13 are the repository's own
skill-format lint (`tests/test_skills.py`) on four newly added social/launch skills that do not
yet follow the six-part SKILL.md layout, 3 are in `test_tweet_launch_integration` (they expect
the ordinary per-trade size cap to refuse a launch, which the current launcher deliberately
bypasses for the dev buy) and 1 is `test_event_kinds_are_declared`.
One more test, `test_tweet_launch_integration.py::test_real_candidate_runner_uses_selected_feed_and_records_all_three_chains`
(2 cases), crashes the interpreter on Windows in both trees and was deselected for the count.
Good first issues if you want to help.

Not included: tests that import the author's private data directory, captures of the author's
own wallets and ledger, a one-off bookkeeping repair for one of the author's positions, the
tweet launcher's deployment receipts (`skills/tweet-auto-launch/references/readiness.md` is a
stub) and the J7Tracker account roster (export your own with
`skills/j7-social-tracking/scripts/collect.py accounts`).
The author's own wallet addresses, transaction ids and chat ids are replaced with fakes throughout.

## License

MIT — see `LICENSE`.
