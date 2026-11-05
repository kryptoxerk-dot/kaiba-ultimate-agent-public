# The trading method

**Date:** 2026-09-20 · This is the one document that says what the agent does when it
decides to buy something, what has to agree first, and what stops it. Every number here is
either measured on this machine, cited to `docs/research/`, or explicitly marked
**INVENTED**. Read `docs/EDGE-AND-VARIABLES.md` for the evidence behind the ranking and
`docs/AUDIT-2026-09-20.md` for the honest rating.

---

## 0. The method in one paragraph

**We do not try to pick winners. We try to be the last one holding a filter.** The evidence
says our measurable edge is knowing what *not* to buy — the exclusion layer quarantines
**10.6%** of what it scans on live data and that number is verifiable without a single
trade (it read 45% until 2026-09-20; see §4 Layer 1 for why that was wrong). So the
method is: screen everything cheaply, exclude hard, require several independent things to
agree before committing size, size by how much agrees, and exit mechanically on a ladder
that was decided before entry. **The exit is the part with published evidence behind it
(+39.0 bps for mechanical brackets). The entry is the part we are honest about not knowing.**

---

## 1. The pipeline, stage by stage

Each stage is a filter. The budget column is measured on this machine today.

| # | Stage | Budget | What it does | Live result |
|---|---|---|---|---|
| 1 | **Ingest** | continuous | pump.fun, PumpPortal, GMGN, Robinhood RPC, announcements, cert transparency | 435 tokens, 5,639 swaps |
| 2 | **Tier-0 triage** | **0.61 ms** vs 100 ms | Cheap structural screen on every launch. Reject / defer / promote | 150 / 368 / 89 |
| 3 | **Tier-1 dossier** | ~7.6 s | Safety providers, concentration, creator history, dedup | 132 dossiers |
| 4 | **Grade** | — | A / B / C / QUARANTINED | 2 A, 61 B, 10 C, 59 quarantined — **45 of those wrongly**, see §4 |
| 5 | **Lanes** | — | Eight strategies evaluate the same dossier independently | 1 of 8 firing |
| 6 | **Confluence** | — | How many independent things agree, entity-counted | see §3 |
| 7 | **Risk gate** | — | Bankroll, exposure, daily loss, lane mode, kill switch | all 20 decisions SKIP |
| 8 | **Signer policy** | — | Classifies the bytes. Withdrawal impossible | never bypassed |
| 9 | **Protection** | 5 s poll | Stop, ladder, ratchet, anti-wick, rug monitor | armed on fill |

**Stage 2 is what makes the rest affordable.** A 0.61 ms screen against a 14 launches/minute
firehose means the 7.6-second dossier only ever runs on 15% of launches. Without it the
system is throughput-bound and skips real candidates; with it, tier 1 keeps up at ~6.4
tokens/min on two workers.

---

## 2. The variables, ranked by evidence

This is the ranking from `docs/EDGE-AND-VARIABLES.md` §1, with the implementation status
on this machine. **The order is by strength of published evidence, not by how much I like
them.**

| # | Variable | Separation | Where it runs | Status |
|---|---|---|---|---|
| 1 | **IPFS image/metadata content hash reuse** | **10.7×** graduation split | `intelligence/dedup.py` | Live. 46.4% of 1,038 launches hit it — against the paper's 10.2%, so the 10.7× cannot be assumed to transfer. Used at **warning** severity only |
| 2 | **Mechanical exit brackets** | **+39.0 bps** vs discretionary | `execution/protection.py` | Live, pure, tested |
| 3 | **Curve velocity as SOL-per-swap** | the published predictor | `execution/lanes.py` | Computed on 100% of observations since the free trade feed landed. **Never yet run on a scanned token with coverage** |
| 4 | **Funding-graph creator clustering** | top 0.1% launch 60–7,630 coins each | `intelligence/creators.py` | 592 creators, 47,433 lifetime coins, 3.43% graduation. Top 1% of addresses → 13.3% of coins |
| 5 | **Bundle-adjusted concentration** | **24pp vs 6pp** naive | `intelligence/concentration.py` | Live. `bundler_pct` unavailable on **every** dossier — GMGN-only, needs Plus |
| 6 | Sell-only / asymmetric wallet filter | one leaderboard wallet: 1,793 trades, **zero buys** | `intelligence/grade.py` | Live |
| 7 | Entity resolution before counting | five addresses, one funder = one opinion | `intelligence/entity.py` | Live |
| 8 | Transaction failure rate | two of four leaderboard wallets at 36% / 49% | `intelligence/tracker.py` | Live — **corrected 2026-09-20**: this row previously said `grade.py`, which never implemented it. `WalletEvidence` has no such field. It is enforced at watchlist admission instead, and reproduced the finding on our own universe: 3 of 9 candidates refused at 37.0% / 23.3% / 23.0% |

### Variables I deliberately do not use

- **Wallet PnL rank.** GMGN defines Smart Money as "wallet who often earns money", no
  method, no out-of-sample test. Kolscan was bought by pump.fun — the venue earning the
  fees owns the board advertising how profitable trading is. Both are advertisements.
- **`rugged` as a creator flag.** Set to 0 for every creator on purpose. The only available
  proxy fires on 57.4% of coins that ever reached $10k, and since 84% of graduates fall
  more than 70% within twenty minutes, that proxy measures the asset class, not the
  developer.
- **Latency.** 146 of the last 150 slots had a **zero** minimum priority fee. The cost is
  app fees at 1% and AMM fees up to 1.25% — 500 to 1,000× the on-chain cost. Optimise fee
  routing, not speed.
- **GMGN's `rug_ratio` as evidence.** It is an opaque vendor score (0–1, "rug pull risk
  score" in the CLI, no published method), sent only on market-feed rows, never on token
  security or token info. MEASURED 2026-09-21/22: present on 180/180 Solana trenches rows
  but every EVM value ever seen is exactly 0 (bsc, robinhood, base), and on Solana a 0 marks
  the unscored young token (8 of 9 changes in one 90 s re-read were 0 → non-zero). So it is
  a veto only when a MEASURED value is at or above the ceiling; an unavailable or zero value
  is never a pass and never earns strength. Provenance per chain: `dyor.RUG_RATIO_PROVENANCE`.
  The real rug defences are the dossier blockers (honeypot, mint/freeze authority, dev
  concentration, cluster, already-rugged → QUARANTINED).

---

## 3. Confluence: what has to agree before we commit size

**One signal is noise.** The rule across every lane is that independent evidence types must
agree, and *independent* is doing real work in that sentence.

### The three rules that make confluence mean something

1. **Count entities, not addresses.** `intelligence/entity.py` resolves common funders and
   co-signing. Five addresses funded from one source are **one** opinion, not five. Without
   this, confluence is trivially spoofable by anyone with a script and 0.1 SOL.
2. **Independent evidence *types*, not repeated readings.** Two price providers agreeing is
   one fact. A graded buyer, a clean dossier and a curve reading agreeing is three.
3. **Unknown is never average.** A missing input is `None` with an `UNAVAILABLE` basis. It
   never scores 0 and it never scores neutral — the lane fails closed. This is why
   `curve-velocity` refuses rather than guessing when `bundler_pct` is missing.

### Confluence per lane, as configured

| Lane | What must agree | Mode |
|---|---|---|
| `confluence-5` | **5 independent entities** buying ≥$50 within **120 s**, dossier grade ≥B, signal <30 s old | shadow |
| `sm-trenches` | 3 smart-degen wallets across **≥2 independent entities**, rug ratio <0.3 | shadow |
| `curve-velocity` | progress 30–70%, SOL/swap ≥ **0.21% of that token's own graduation target**, bundlers <20%, ≥1 graded wallet | shadow |
| `trusted-copy` | copy delay <20 s, price drift <12%, exits followed | shadow |
| `migration-fade` | sell within 180 s, max hold 1,200 s, **never hold through migration** | shadow — **the only lane firing** |
| `kol-fade` | caller with ≥10 trades and measured positive expectancy | shadow |
| `pons-robinhood` | 3 entities, <30 min old, ≥$5k liquidity | shadow — **venue closed to both latency edges by contract design, see below** |
| `listing-pop` | announcement→entry <30 s | **off** |
| `manual` | the agent's own thesis + dossier ≥B | shadow |

**`migration-fade` is the only lane that has ever fired** — 14 signals. That is not a
success; it is a statement that seven of eight lanes are input-starved. The reasons are
specific and diagnosed from the live database, not guessed: three need swap coverage on
*scanned* tokens, one needs GMGN Plus, one needs a caller table that is empty, one needs
Robinhood ingest, one is switched off.

### Robinhood Chain: mapped, and closed to us by contract design

Verified on chain 2026-09-20 against the real `PonsV2LaunchFactory`
(`0x7ed598bcef8bd9edd8c97a195c6d13f40801ec7e`, proven from a launch transaction carrying
exactly its own `launchFee()`). **Both of our latency edges are closed, not by competition
but by how the contracts are written:**

- **A 99% anti-sniper tax** (`snipeTaxStartBps` 9900) decaying over three seconds. There is
  no profitable early entry to race for.
- **Graduation is atomic with the buy that triggers it.** CurveBuy, v4 Initialize, seed,
  PoolGraduated and the first v4 swap share **one transaction hash**. There is no migration
  window, so **`migration-fade` cannot be ported to this chain at any speed** — and
  `migration-fade` is the only lane that has ever fired.

The economics say the same thing. Same prize as pump.fun (~$10,800 per graduation), roughly
**5× worse graduation rate** (~1%), and **~1.2 ETH (~$3,100) of lifetime curve volume per
launch**. The ecosystem's fee total is large because there are ~300k launches a month, not
because any individual token is tradeable. **Not worth trading directionally** — which is a
finding, not a failure: it is an entire chain removed from the plan on evidence, before any
capital went near it.

`pons-robinhood` itself is blocked on exactly **one field**: `liquidity_usd` is unavailable
because GoPlus excludes chain 4663, RugCheck is Solana-only, and DexScreener does not index
pre-graduation curves. Supplied that single number, the lane fires at strength **0.949 with
171 independent entities** — so the blocker is plumbing, and the reason not to prioritise it
is the paragraph above, not the plumbing.

### The `manual` lane is the authority dial

Every intent the model submits runs on the `manual` lane **whatever lane it names**. So
promoting a backtested lane to live never silently promotes the agent's hunches with it.
That is the one place where "full authority" is made concrete and bounded by something
other than trust.

### Score → size

Confluence becomes money through one ladder, and below 70 there is no size at all:

| Score | Fraction of lane maximum |
|---|---|
| ≥95 | 100% |
| ≥90 | 75% |
| ≥80 | 50% |
| ≥70 | 25% |
| <70 | **0 — a signal we cannot grade is a signal we do not take** |

---

## 4. The safety net

Four layers, and they fail in different directions on purpose.

### Layer 1 — pre-trade exclusion (`intelligence/dyor.py`)

**Eleven blockers.** Any one quarantines the token outright:

honeypot · live mint authority · balance-rewrite authority · live freeze authority ·
transfer hook · owner-modifiable tax · tax ≥50% · **dev supply >10%** · **unexplained
cluster >30%** · already-rugged flag · **no provider could establish sellability**

That last one matters most: *not one provider could confirm it is sellable* is a blocker,
not a shrug. Eighteen further conditions raise warnings and cut size rather than refusing.

**A correction, and it is a large one.** Until 2026-09-20 this layer reported **59 of 132
quarantined (45%)** and I described that as the system's measurable strength. A live
verification against chain truth found the number was mostly an artefact:

`_RISK_MARKERS` matched the substring `"rugged"` against RugCheck's risk name **"Creator
history of rugged tokens"** — a statement about the developer's *other* launches — and set
`rugged=True`, which is the `already_rugged` **blocker**. Proof: a token returning
`"rugged": false` in the same report carrying that risk was quarantined anyway.

I verified it in the live database: **49 of 59 quarantines carried `rug_history`, and for
45 of them it was the only blocker.** Three quarters of everything this layer has ever
refused was refused on a substring collision. It is now routed to `creator_rug_count` and
priced as a warning.

| | before | after |
|---|---|---|
| Quarantined | 59 of 132 (45%) | **14 of 132 (10.6%)** |
| Sole blocker `rug_history` | 45 | 0 |

Four EVM tokens (PEPE, BOBO, SafeMoon v1, Pitbull) were also quarantined on *capabilities
with no actor* — `transfer_pausable` and `can_take_back_ownership` with a provably
renounced owner. Also fixed.

**What survived verification, and this is the part that matters:** both providers'
authority mappings agree with the mint account **12/12 each**, including negative controls
— neither reported USDC or USDT as revoked. The blockers that do real work are correct.
The headline rate was not.

**And the grade was never a gate.** Verified 2026-09-20 by reading every consumer:
`require_dossier_grade` is compared in `lanes.py` against each buyer's **`WalletScore`**,
not against the token dossier — it is a wallet grade floor with a misleading name, now
renamed `require_wallet_grade`. The `manual` lane's copy was read by nothing at all and is
deleted. `engine.decide` records `dossier.grade` on the Decision and compares only
`dossier.blockers`. **No trade has ever been gated on a token's grade.** The blockers in
this layer do the work; the letter was a label on a chart.

**The grade now expresses coverage, because it was expressing nothing.** It read 83/100
with an average of 6.9 unknown fields, and `bundler_pct`/`sniper_pct` were unknown on 615
of 615 dossiers — so an A meant "we found nothing wrong", not "we checked and it is fine".
Six of the fourteen tokens that later rugged were graded **A**.

A grade is now capped by what was actually established: concentration, bundling, sniping
and taxes must each be fully read to reach the top grade, and partial coverage is ranked by
the published separation of each field (24 for bundle share against 6 for naive top-10).
Scores are untouched; the ceiling only ever lowers.

| | A | B | C | QUARANTINED |
|---|---|---|---|---|
| Before | 18 | 526 | 18 | 124 |
| After | **0** | 392 | 169 | 124 |

**A is currently unreachable on every token in the database, and that is the honest state.**
It does not separate the rugged from the non-rugged — 1 B / 13 C in both groups — because
the missing fields are missing for both. The change is not that we can now tell them apart;
it is that the grade has stopped claiming a confidence it never had.

### Layer 2 — in-trade protection (`execution/protection.py`)

Pure logic, no network, no clock. Checked in this precedence:

1. **Rug monitor** — a 40% single-interval liquidity drop beats everything. If the pool is
   leaving, the price you can see is not a price you can get.
2. **Emergency loss** at −50%.
3. **Stop** — hard −30% before TP1, then whatever the ratchet raised it to. **It only ever
   rises.**
4. **TP ladder** — 2× sell 50%, 5× sell 25%, 10× sell 15%. One rung per call, recorded so
   it can never fire twice.
5. **Anti-wick** — hold a rung unless the *executable quote* is ≥70% of the chart price.
   The chart is not a fill, and selling into a wick is how a 3× becomes a 0.7×.
6. **Ratchet** — breakeven after TP1, then trailing 30% / 25% / 20% / 15% / 10% at
   2× / 5× / 10× / 25× / 100×.

**Known gap, stated rather than hidden:** `use_provider_orders` is `false` because nothing
in the tree calls `to_gmgn_condition_orders`. **No protection currently survives our process
dying.** It was left off rather than left lying, because an operator who believes their
stops are server-side will size accordingly.

**The exit invariant, and where it was being broken.** *An exit is never refused on
price.* A slippage cap protects an **entry**, where declining means not opening a
position; on the way out it means holding a token whose pool is draining, and 14 of our
first 41 closed trades exited to the rug monitor — so the pools we most need to leave are
exactly the ones that cannot clear a cap.

`RiskGate.check_exit` has always honoured this. **The paper broker did not**, and nobody
looked because the attention went to the risk gate. Measured 2026-09-21: **659 refused
sells, two positions retried 320 times each**, stranded, with the shadow record showing
them *open* rather than showing the loss we would really have taken. A sell now fills at
whatever the pool gives and carries `over_tolerance` and `tolerance_bps` in its basis
payload — **filling is the more honest record, not the laxer one**, because the cost is
visible either way and the alternative was a position that never closed.

The one exception, which proves the rule: **no price is not a bad price.** Filling outside
a tolerance is a real trade at a real cost; filling with no quote would be inventing one.
A sell with no price is still refused.

**Open gap:** a position that becomes permanently unpriceable — a dead pool DexScreener no
longer lists — stays open forever, warning every 60s. There is a blind *warning* and no
blind *write-off*. Carrying it open overstates exposure and understates realised losses;
two positions are in that state now.

### Layer 3 — account brakes (`execution/risk.py`)

Kill switch · entries paused · reduce-only · daily loss stop (resets 00:00 UTC) · per-chain
exposure cap 10% · max position 0.02 SOL · lane mode.

Two design rules:

- **Unfunded is not unlimited.** A chain with `bankroll_base_units: 0` cannot size at all.
  It does not fall back to "whatever the wallet holds". This is why all 20 decisions so far
  refuse with `bankroll_unfunded` — correctly.
- **Exits are never gated.** Every brake blocks *entries*. `check_exit` always allows. A
  brake that also stops you selling is not a brake, it is a way to lose the whole position.
  (This was a real bug: the kill switch used to trap money in live positions.)

### Layer 4 — the withdrawal gate (`execution/policy.py`)

The one thing the agent may not do, enforced three independent ways:

1. GMGN is custodial and **exposes no transfer endpoint** — verified against CLI 1.6.1.
2. The signer **decodes every instruction** and rejects value movement to a non-owned
   address. Fail-closed: an undecodable program, an unknown selector, an unresolved
   address-lookup-table account are all rejections.
3. **No MCP tool takes a destination parameter**, and the LLM never sees a key.

Configuration can only narrow. Adding `transfer` to the YAML, adding an address to the tip
list, or raising the tip cap changes nothing. **There is no boolean that turns withdrawal
on.**

---

## 5. The paper model

Paper trading is not a rehearsal here, it is the measurement instrument. With zero closed
trades, everything in §2 and §3 is a hypothesis.

**The structural problem it had to solve:** all three paper orders failed with `no_price`.
The cause was that DexScreener has no pair row for a token still on its curve — **not**, as
I first wrote here, that a curve token is unpriceable in general. Verified 2026-09-20 with
a raw call: Jupiter routes pump.fun curves directly, one hop, route label `Pump.fun`, real
impact on 0.02 SOL.

Three sources, in order:

| Where | Source | Why |
|---|---|---|
| Pre-graduation | **curve reserves** | local arithmetic, available the moment the mint exists |
| Pre-graduation cross-check | **Jupiter** | executable, but 400s until the token is indexed |
| Post-graduation | DexScreener / Jupiter | a pair exists |

Curve reserves stay primary pre-graduation because Jupiter's sustained ceiling is ~1
request/second before a **sticky** 429 with no `retry-after`, and a probe costs up to two
calls per token. A watchdog polling several positions cannot afford that per tick.

What a paper fill must model to be worth anything:

- Price from **curve reserves** pre-graduation, DEX pair post-graduation
- **The round-trip cost, not the impact figure.** Measured live at 0.02 SOL: **6 bps** on a
  liquid token, **260 bps** on a fresh curve token — dominated by pump.fun's ~1%-per-side
  fee rather than by impact. Fees are 500–1,000× the on-chain cost.
- **Never `priceImpactPct`.** Reproduced: Jupiter returns exact `0` on a split route in the
  same call where a curve token returns a real number. It computes against a reference
  route and degenerates to zero. Two quotes in opposite directions is the honest number.
- The same `OrderState` machine as live, including `UNKNOWN` for an ambiguous send

**What paper cannot tell us**, and I will not pretend otherwise: fill quality, real
slippage on a moving pool, and whether a quote is honoured. Those need one real trade.

---

## 5a. The first live result: our position size is below the economic floor

**Measured 2026-09-20 from 23 closed paper trades, not modelled.** This is what continuous
paper trading was for, and it found something no amount of research would have.

Fees decompose into a proportional part and a **flat part that does not scale with size**:
a router/tip fee plus account rent. Backing the proportional component out of every closed
trade gives a flat cost of **0.000877 SOL per leg**, and it is invariant from 0.015 SOL to
0.10 SOL — as it must be, because it is flat.

| Position | Flat fees, round trip |
|---|---|
| 0.005 SOL | **35.1%** |
| 0.010 SOL | **17.5%** |
| **0.020 SOL — our configured max** | **8.8%** |
| 0.050 SOL | 3.5% |
| 0.100 SOL | 1.8% |
| 0.250 SOL | 0.7% |

That 8.8% is paid **before** pump.fun's 250 bps round trip, before the broker's 60 bps
floor, before slippage, and before being right about direction. Observed total fees ran
**13.4–22.4% of cost** on the 0.016 SOL trades and **5.3–6.8%** on the 0.05–0.10 SOL ones.

**`max_position_base_units: 20000000` (0.02 SOL) is roughly 5× below the size at which a
directional trade can pay for itself.** To hold flat fees under 2% round trip needs
**0.088 SOL**; under 1%, **0.175 SOL**.

This is worse in reality than in the model. Of three real mainnet buys inspected, two paid
a **0.001 SOL bot-router fee** and all three paid **~0.0015 SOL of account rent** — so the
true flat cost is nearer 0.0025 SOL on the entry leg alone, which is **12.5% of a 0.02 SOL
position** for the entry by itself.

**Two corrections to the first version of this section, both from the viability model
that replaced it.**

*The flat figure was wrong.* 0.000877 was a least-squares artifact: trades exiting through
a TP ladder pay a third tip and sit at the top of the notional range, so OLS reads their
extra *flat* cost as a steeper *rate*. A Theil–Sen fit over 28 trades gives **0.00099295
SOL per leg flat and 127.53 bps per leg proportional**, recovering the known 125 bps to
within 2%; OLS returns 167 bps. Table above restated: 0.02 SOL costs **12.5%** round trip,
not 8.8%.

*And sizing up is not the fix.* Decomposing `migration-fade`'s −0.2286 SOL over 25 trades:

| | | share of the loss |
|---|---|---|
| Flat cost (55 legs) | 0.0546 SOL | **23.9%** |
| Proportional | 0.0074 SOL | 3.2% |
| **Directional** | **−0.1666 SOL** | **72.9%** |

Ex-fees the lane still returns **−41.1% on cost**, and zeroing every fee turns 4 winners
into 5 of 25. **Replaying the same percentage outcomes at 0.05 SOL gives −0.71 SOL; at
0.1 SOL, −1.42 SOL.** The fee *share* falls and the loss triples.

So the floor is real and roughly a quarter of the damage, but **size is a precondition for
profit, never a cause of it.** The gate's honest function here is a refusal to trade, not a
licence to trade bigger. `bounds.max_round_trip_cost_pct: 7.0` enforces it, entries only —
a cost gate that refused a sell would strand the position permanently, which is worse than
paying the fee.

## 5b. `migration-fade`, the only lane that fires: 4 wins in 20

| | |
|---|---|
| Trades | 20 |
| Wins | **4** |
| Net | **−0.16 SOL** |
| Average PnL | **−49.2%** |
| **Average MFE** | **+34.9%** |
| Fees as a share of cost | **15.6%** |
| Exits to a rug monitor | **11 of 20** |
| Exits at −100% | 4 |

The lane is not blind — average maximum favourable excursion is **+34.9%** and five trades
exceeded **+100%**, one reaching **+230%**. It finds movers. It also buys tokens that rug:
**eleven of twenty exits were the liquidity monitor firing**, at LP drops from −41.8% to
−97.7%.

So the loss has three separable causes, and only one of them is the entry:
1. **Fees at 15.6% of cost** — §5a, fixable by size.
2. **A 55% rug rate** — an exclusion problem, and the exclusion layer's real corrected
   quarantine rate is 10.6%, so it is letting these through.
3. **Exit timing** — trades that reached +100% still closed negative.

**Do not read this as "the lane is bad" yet.** Twenty trades is nothing, and two of the
three causes are not the lane's fault. It is, however, the first evidence any of this has
ever had, and it points at exclusions and sizing rather than at signal quality.

## 6. The thing this method cannot do, stated plainly

The validation harness computed from our own database that the directional book needs
**5,447 closed trades** to conclude anything (10,662 at 20 trials, 16,122 at 100). At an
achievable 30–60/week that is **1.7–3.5 years**, 3.5–17.5 with clustering, against a venue
whose rules change roughly every **8 weeks**. To finish inside one regime needs 681
trades/week.

**A sample gathered across regime boundaries is not stationary and cannot be tested as one.**
So "validate, then deploy capital" is not a sequence we can execute. That is not a reason to
stop; it is a reason to be precise about what the method is for:

- The **exclusion layer** can be validated without trading, and is the product.
- The **exit ladder** has published evidence and does not need our sample to be right.
- The **entry lanes** are instrumented hypotheses. They run in shadow, they accumulate
  evidence, and none of them has earned promotion.

Anyone who tells you their memecoin entry model is validated at this sample size is
describing a hope. The honest version is: we are very good at not buying things, we have a
disciplined exit, and the middle is an open question we are measuring rather than asserting.

---

## 7. What is weak

- **Tier 0's promote rate rests on an invented weight.** `creator_prior_graduate` = 0.45 is
  marked INVENTED in its own provenance table. Feeding it creator data took promote from 0%
  to 18.5%. The screen is unstarved, **not validated**.
- **The copycat 10.7× separation is unverified for 2026.** Base rate moved 10% → 46%, so the
  published split cannot be assumed to transfer. Only the content-hash subset is used, at
  warning severity.
- **`sol_per_swap` has never run on a scanned token with coverage.** It refuses rather than
  computing from three trades out of four hundred, which would overstate it ~130×.
- **`bundler_pct` and `sniper_pct` are unavailable on every dossier.** Two of the eight
  ranked variables are dark until GMGN Plus.
- **Stops do not survive a crash.** Layer 2 is in-process only. The mirror is now built,
  but `use_provider_orders` stays `false` behind five blockers — see §4 Layer 2.
- **`confluence-5` is starved by us, not by the market.** Measured 2026-09-20: five
  independent entities inside 120 seconds occurred in **351 of 728 windows (48%)** across
  306 tokens, peaking at 68. But the lane grades every buyer at **≥B before** counting
  entities, and the database holds **one** wallet at B or better. The lane needs five;
  `sm-trenches` needs three. Neither is reachable on any tape — arithmetic about our
  coverage, not about the market.
- **The entity collapse has never engaged on live tape — not once.** 174 entities covering
  559 addresses existed during that run and `buyers > entities` was true in **0 of 728
  windows**. Every "entity" count above is really an address count, so 48% is an upper
  bound with the anti-spoofing guard inert.
- **Zero positions, zero closed trades.** Every claim about entry performance in this
  document is a forward statement, not a measurement.
- **`can_sell` and `rugged` rest entirely on provider assertion.** Chain truth cannot check
  either. On Solana GoPlus derives `can_sell` from `non_transferable`, which is `"0"` on
  essentially everything — a near-constant `True` feeding a blocker.
- **`liquidity_usd` is single-sourced on Solana and can be wrong by four orders of
  magnitude.** One token read $7.54B from a pool whose two sides were $118 and $7.5B.
- **Wallet discovery found nothing.** 681 addresses screened, 27 candidates, **zero** with
  enough closed trades to be distinguished from luck (516 needed; the best has 23). At
  α=0.05, 34 of 681 zero-skill wallets would look significant by chance.
