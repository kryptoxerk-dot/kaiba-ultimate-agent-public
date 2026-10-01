# From here to a complete, running Kaiba Ultimate Agent

**Date:** 2026-09-20 · Supersedes the phase ordering in `docs/PLAN.md` v1.1, which assumed
the eight trading lanes were the product. The evidence says otherwise, and the ordering
below reflects that.

Read `docs/AUDIT-2026-09-20.md` first for the state of the system, and
`docs/EDGE-AND-VARIABLES.md` for which variables the evidence actually supports.
This document is what to do about both.

---

## The strategic reframe, in one page

The original plan treated **entry signals** as the asset and safety as the overhead. The
research says invert that.

**What the evidence supports (build on this):**

- **Exclusion filters.** Rejecting a honeypot, a live mint or freeze authority, a
  modifiable tax, a clustered supply, a serial rugger. Low evidentiary bar, and the
  evidence clears it. This is the only part of the intelligence stack that is defensible
  today.
- **Liquidity velocity** as a graduation predictor. In the one published out-of-sample
  study it is "the single most informative predictor of graduation among all variables
  considered" — and notably, *bot-dominated early activity predicts lower graduation.*
- **Certificate-transparency monitoring** for pre-announcement signals. Verified on this
  machine today: `claim.scroll.io`'s certificate was issued 2024-10-19 against a
  2024-10-22 event. Three days of lead, roughly 100% precision, free.
- **Venue announcement endpoints.** Binance, OKX and Upbit all serve new-listing
  announcements over free unauthenticated JSON, verified live.

**What the evidence does not support (demote or delete):**

- **Wallet copy-trading as an entry trigger.** Four of five selection methods in the one
  paper that models it produce positive leader returns and *negative copier* returns, at
  zero latency, before competition. Copying is mechanically negative on a bonding curve.
- **Smart-money grading as a pillar.** Persistence is unestablished. No published
  autocorrelation study, no forward-tracked cohort with a control, no vendor validation.
- **NFT mints as a revenue line.** A backtest of the entire Magic Eden launchpad found 8%
  of 75 paid Solana mints trade at or above mint, and mint-everything returns −85.4%. The
  launchpad feed has published nothing since February 2026. **Cut this.**
- **Airdrop farming at our scale.** Roughly $5–12 per wallet-hour and +2% to +8% a year on
  capital, below passive stablecoin yield. Worse, sybil detection reliably catches small
  clusters with shared funding and correlated timing, which is exactly the shape a solo
  operator's wallet set takes. **Keep the detection, drop the farming.**

**The reallocation that follows.** The airdrop detection signals are worth more pointed at
the *trading* book than the farming book. Knowing a claim page went live three days early
is a better pre-event trade than a reason to farm twenty wallets.

**And a larger reallocation to decide on, in Phase 6b.** Ranked by evidence, directional
trading is the *eighth* most reliable way to make money in this ecosystem and copy trading
is the ninth. The top three are referral capture, creator-fee capture and infrastructure
resale, none of which require predicting anything.

**The argument I could not rebut.** Reaching statistical significance needs roughly
3,500–5,500 closed trades, or 10,000–25,000 after deflating for the number of
configurations tried. Venue rules change every couple of months. At an honest 30–60
independent trades a week we reach 250–500 per regime, an order of magnitude short. We may
structurally never accumulate a stationary sample. **Phase 4 exists to confront this
directly rather than to route around it.**

---

## Phase 0 — Unblock (you, not me)

Nothing below Phase 1 can complete without these. They are all yours.

| # | Decision | Why it blocks | Cost |
|---|---|---|---|
| 0.1 | Buy GMGN Plus on the account owning the API key | Free plan cannot afford a quote or a swap, weight 10 against an allowance of 5. No execution at all without it. | $29/mo |
| 0.2 | Run `gmgn-cli config` to bind the Ed25519 request-signing key | `GMGN_PRIVATE_KEY` is unset, so portfolio calls return 401. Buying Plus does **not** fix this. | free |
| 0.3 | Declare your wallets in **two** places | This is more specific than I previously wrote. `config/risk.yaml` needs `chains.<chain>.wallet` for the executor, and `config/signer-policy.yaml` needs `owned_addresses.<chain>` for the signer policy, which is currently **empty for every chain**. A swap is refused with `universal_router_no_owned_address` until the second one is filled, which is the policy working correctly: it will only let a swap settle to an address you have explicitly declared. | free |
| ~~0.4~~ | ~~Pay for a trade-level data source~~ | **I was wrong about this and it is not a blocker.** Helius's free tier returns parsed transactions with swap classification, and the credential is already set. Verified working today. The backfill is being built against it now. Revisit only if 1M credits a month proves too few. | $0 |
| ~~0.4b~~ | ~~Get a Robinhood Chain router address~~ | **Done, 2026-09-20.** Identified directly from the chain rather than a published source: `0x8876789976decbfcbbbe364623c63652db8c0904`, the Uniswap Universal Router on chain 4663. It is called with both Universal Router `execute` selectors, holds 24,546 bytes of code, and its successful transactions emit Uniswap V4 Swap events, matching Pons' documented graduation to Uniswap v4. In `config/signer-policy.yaml` with the evidence and a falsification test. Confirm on a block explorer before moving real size. | done |
| 0.5 | Confirm the production host | Everything after Phase 2 needs to run continuously. | existing box |
| 0.6 | Approve rotating the leaked keys in `docs/research/01-prior-work-inventory.md` §8 | Security debt carried from the prior system. | free |
| 0.7 | Decide on the Telegram listener | Telegram's content licensing terms prohibit harvesting channel data for machine-learning use. I have made it opt-in rather than default; whether to run it at all is your call. | free |

**Minimum to proceed: 0.1, 0.2 and 0.3.** Roughly **$29/month**, which is GMGN Plus alone.

**A chain-allocation change falls out of this.** The plan was Solana-first. The measured
fee data says Robinhood Chain hosts the largest launchpad in the world by a factor of
2.8, and we already have a `pons-robinhood` lane that cannot fire. Solana remains the
deepest venue for the intelligence work, but Robinhood deserves parity of effort rather
than the afterthought it currently gets. On Solana, retarget from pump.fun alone to
pump.fun plus StonkFun, which is a month old and already takes 42 of Jupiter's top 100
tokens by organic score, and stop modelling bonk.fun, which returned zero launches in a
197-token sample.

---

## Phase 1 — Make it see (1 week)

*Goal: the intelligence tables stop being empty. Nothing else can be validated until this
is true.*

Today 36 of 47 tables have never held a row. `swaps` and `first_buyers` are empty, so the
grader returns UNSCORED for all 6,236 imported wallets and every lane that depends on them
is inert.

1. **Wire a trade-level feed** from whichever source Phase 0.4 picked, writing `swaps`.
2. **Backfill `first_buyers`** for every token that reaches the curve threshold.
3. **Record signer and address-lookup-table metadata** on ingest, via `record_swap_meta`.
   Without it the three hard cluster edge types return zero, permanently.
4. **Fix the sell-only wallet trap** before any grading runs. Published leaderboards are
   partly settlement and aggregation addresses — one had 1,793 trades and zero buys. Our
   grader would rank it highly. Reject buy/sell-asymmetric addresses at scoring time.
5. **Stabilise the listener.** The live run reconnected roughly every seven seconds.

**Done when:** 24 hours of continuous ingest, `swaps` above 100k rows, `ingest_status`
showing a stable feed, and at least one wallet with a non-zero evidence weight.

---

## Phase 2 — Make the exclusions real (1–2 weeks)

*Goal: the part of the system the evidence actually supports becomes trustworthy.*

1. **Live-verify every DYOR field mapping.** Today there is **zero** live provider
   verification: every GoPlus and RugCheck fixture is hand-built to documented shapes, so
   every mapping is unproven. Run a few hundred real tokens through and reconcile.
2. **Fill the two blind fields.** Bundler and sniper percentage are unknown on every scan
   and are GMGN-only. With Plus they become available.
3. **Move cluster detection to the veto path.** It is evidenced as an exclusion filter and
   not evidenced as an entry trigger. Stop using it as both.
4. **Measure the filter, not the trade.** Track precision on a labelled set: of the tokens
   we rejected, how many actually rugged. This is measurable without ever placing a trade,
   which makes it the cheapest real validation available to us.

5. **Add the three cheap filters we do not have**, all evidenced, none needing speed:
   - **IPFS image and metadata hash de-duplication.** Originals graduate at 9.20%,
     copycats at 0.86%. A 10.7× separation for one hash comparison, and the
     highest-value variable in the whole review. We have nothing like it.
   - **Bundle-adjusted holder concentration**, replacing raw top-10 as the primary read.
     The discriminative gap is 24pp against 6pp.
   - **Creator clustering over a 3-hop funding graph** rather than a per-address lookup,
     which is defeated 55% of the time by construction.

6. **Build the execution envelope before any further signal work.** Mechanical brackets
   recovered **+39.0 bps per position** in a 3,505-vault fleet, and **49.3% of positions
   that went meaningfully green still closed red**. That is an exit-discipline problem
   solvable without predicting anything, and it is the best-evidenced intervention we
   have found.

**Done when:** exclusion precision and recall are reported on a labelled set of at least
500 tokens, the dossier's unknown-field count is in single digits, and every open position
carries a bracket.

---

## Phase 3 — Make it fast enough, or admit it is not (1 week)

*Goal: decide honestly which strategies are reachable.*

Measured today: a full safety scan takes **7.6 seconds**, a price read **0.70 seconds**,
against **14 launches a minute**. We can triage roughly 8 tokens a minute. The competitive
window for a snipe is sub-second.

1. **Accept that sniping is out of reach** on this architecture and say so in the config,
   rather than leaving lanes that quietly never win. A Python process calling a vendor CLI
   over HTTP cannot compete with a colocated client on a staked connection.
2. **Build a two-tier triage.** A cheap sub-100ms screen on the event itself, then the
   expensive 7.6-second dossier only for what survives. This is the only way the scan rate
   ever exceeds the launch rate.
3. **Re-target the lanes at windows we can actually hit.** Post-migration, post-listing and
   multi-minute confluence are reachable. First-block entry is not.
4. **Pick the latency target from the strategy**, not the other way round.

5. **Correct the assumptions that will break silently.** Curve velocity must be SOL per
   *swap*, not per minute, which is the error most implementations make and ours makes
   too. Stop hard-coding the 85 SOL graduation threshold, which is wrong for Meteora DBC.
   Stop hard-coding venue program IDs, since 39% of Solana DEX volume sits in prop AMMs
   that redeploy and four went from billions to zero this year. Refresh blockhashes
   aggressively or use durable nonces; the 150-block window is now about 37.5 seconds.

6. **Move the boundary to the right place if we ever need real speed.** The language is
   almost never the binding constraint; the data path is. A thin Go or Rust daemon on a
   shred feed publishing to Python over a local socket is the architecture, not rewriting
   the strategy layer. Only worth doing if Phase 4 finds an edge that needs it.

**Done when:** the triage screen sustains the full launch rate, and every lane's documented
window is one the measured latency can meet.

---

## Phase 4 — Shadow, and confront the sample-size problem (6–8 weeks)

*Goal: find out whether there is any edge here at all, before risking money.*

This is the phase that matters and the one most likely to end the project honestly.

1. **Run the full stack in shadow** with real signals and paper fills, continuously.
2. **Keep an append-only trial registry.** Every parameter set tried is recorded before it
   is run. Without this the deflated Sharpe ratio is uncomputable and every later number is
   inflated by silent multiple testing.
3. **Run the matched-control arm for wallet grading** — the test the literature has never
   run. Graded cohort against a matched random cohort, significance at p<0.01. If it fails,
   delete the pillar rather than keeping it for comfort.
4. **Apply the six gates** from `docs/research/13-validation-and-copytrading-2026.md`:
   data integrity, execution realism, statistics, shadow, micro-live, scale. Including the
   brutal one: **still positive after deleting the best 5% of trades.**
5. **Confront the regime problem directly.** If we reach 300 trades in eight weeks and need
   3,500, then "validate before trading" is not a gate we can pass. At that point there are
   three honest options, and we pick one deliberately rather than drifting:
   - trade a much narrower, higher-conviction thesis where n can be small;
   - accept trading on unvalidated edge with size small enough that being wrong is tuition;
   - do not trade directionally at all, and keep the system as intelligence and exclusion.

**Done when:** the gates have a verdict. A "no" here is a successful outcome, not a failure.

---

## Phase 4 result, 2026-09-20: the gate cannot be passed by waiting

The harness computed it from our own database. **5,447 closed trades needed; 1.7–3.5 years
at an achievable rate, 3.5–17.5 allowing for clustering, against an 8-week regime.** To
finish inside one regime the book would need 681 independent trades a week against an
achievable 30–60.

This is a conclusion available now, not one that needs a year of data. Phase 5 is gated on
Phase 4, and Phase 4 says the directional book cannot be validated in principle at our
scale. Phase 6b is therefore no longer an optional consideration; it is the live question.

## Phase 5 — Live-readiness, being built now (2026-09-20)

Reordered after Phase 4. This is no longer "micro-live"; it is everything that has to be
true before a real fill is survivable, built regardless of whether Option 1 in
`docs/DECISION-6B.md` is taken, because Options 2 and 3 need most of it too.

| Item | Why it gates going live | Status |
|---|---|---|
| Live verification of every GoPlus and RugCheck field mapping against chain truth | The exclusion layer is the asset and its provider mappings have **never** been checked against a real response. A misread field is a confidently wrong verdict. | building |
| Real fill price from the fill itself, contemporaneous native/USD, chain decimals | The stop-loss is computed from the entry price, and the entry price was an estimate that excludes slippage. On a snipe that is routinely 10%+. | building |
| Robinhood Chain ingest for the `pons-robinhood` lane | The world's largest launchpad by fees is on a chain we do not ingest. Router found today; launchpad contracts being identified from the chain. | building |
| Maintenance scheduler | Creator history, wallet grades, early-alpha sources and price samples all go stale unless someone types a command. Budget-aware, with the wallet job targeting buyers we have actually seen. | building |
| Chain reconciliation of a live fill | Catches a venue reporting one fill and settling another. | building |

**Done when:** every row above has run against real data and reported, and the preflight
gate passes on the production host.

## Phase 6B — the decision (yours)

`docs/DECISION-6B.md` lays out five options with numbers and a recommendation. Nothing in
it is blocked on engineering. The short version: our measured edge is knowing what not to
buy, which is worth more to other people's flow than to our own; route our own trades
through our own referral codes today, build the intelligence into a product that others
trade through, and keep the directional book at tuition size.

## Phase 6 — Early-alpha detection as its own product (parallel, 1 week)

Cheap, independent of the trading question, and useful even if Phase 4 says no.

1. **Certificate transparency watcher** on a project watchlist. Verified today. Free.
   Needs retry logic; crt.sh returns intermittent 502s and timed out on one of two domains.
2. **Venue announcement pollers** for Binance, OKX and Upbit. All free, unauthenticated,
   verified live.
3. **Governance and repository watchers.** Snapshot's GraphQL endpoint, Discourse
   `/latest.json` on the major forums, and per-repository Atom feeds, which cost no API
   quota at all.
4. **Job board signals** via the Greenhouse and Lever public board APIs.
5. **Point all of it at the trading book**, not a farming book.

**Delete:** the NFT mint hunter. The numbers do not support it.

---

## Phase 7 — Operate

Deployment units, runbooks and backups already exist and the CLI surface now matches them.
What remains is continuous operation, the nightly reflection loop with something real to
read, and the promotion gates running on their own schedule with an eight-week expiry tied
to venue changes.

---

## Honest timeline — build time and calendar time are different things

My first version of this table conflated two things and read as "three to four months of
work". That was wrong and it is worth separating properly, because the distinction changes
what you should do.

**Build time is days.** Every phase below is a few days of work, and most of it can run in
parallel because the pieces touch different files. Phases 1, 2, 3 and 6 are being built
concurrently right now.

**Calendar time is the shadow-mode data.** Phase 4 needs closed trades to accumulate, and
no amount of effort makes trades happen faster than the market produces them. That is the
only genuinely slow part, and it is **waiting, not working** — so it runs in the
background from the moment Phase 1 lands, while everything else continues.

| Phase | Build effort | Calendar | Runs in parallel with |
|---|---|---|---|
| 0 Unblock | yours, minutes | — | everything |
| 1 Make it see | 1–2 days | — | 2, 3, 6 |
| 2 Exclusions real | 2–3 days | — | 1, 3, 6 |
| 3 Latency and triage | 1–2 days | — | 1, 2, 6 |
| 6 Early alpha | 1–2 days | — | 1, 2, 3 |
| 4 Shadow + validation | 1 day to wire | **6–8 weeks of accruing data** | starts as soon as 1 lands, and everything else continues during it |
| 5 Micro-live | 2–3 days | gated on 4 | — |
| 6b Monetisation decision | yours | — | — |
| 7 Operate | ongoing | — | — |

**So: about a week to a complete, running agent in shadow mode, and then six to eight weeks
before the statistics can say anything.** The agent is live, ingesting, filtering, scoring
and paper-trading the whole time. The only thing gated on the wait is putting real money
behind a directional thesis, and that gate exists because the alternative is discovering
the answer with your capital instead of with paper.

If the validation window turns out to be unaffordable in calendar terms, Phase 6b is the
honest alternative: the top three monetisation routes need no validation window at all,
because they are fee capture rather than prediction.
The one thing that cannot be compressed by working harder is the validation window,
because it is made of trades the market has not produced yet. Everything else can.
