# Best method, best variables, best edge — what the evidence actually supports

**Date:** 2026-09-20 · Sources: `docs/research/10-solana-edge-2026.md`,
`12-early-alpha-airdrops-nfts-2026.md`, `13-validation-and-copytrading-2026.md`. Every
claim here traces to a cited paper or a measurement taken on this machine.

This is the direct answer to "the best method, the best variables, the best edge". The
short version is that the best variables are not the ones the trenches talk about, and the
best method is mostly not prediction.

---

## 1. The best variables, ranked by evidence

These are ranked by how much published evidence exists, not by how often they are
mentioned. The top one is barely discussed anywhere and we do not have it.

| # | Variable | Measured effect | Cost to compute | Have it? |
|---|---|---|---|---|
| 1 | **IPFS image and metadata hash de-duplication** | Originals graduate at **9.20%**, copycats at **0.86%**. A **10.7× separation**, peer-reviewed at CCS'26. | One hash comparison. | **No** |
| 2 | **Mechanical exit brackets** | **+39.0 bps per position** in a 3,505-vault production fleet. 43.2% of positions reached +300 bps of favourable excursion and **49.3% of those still closed negative**. | Zero intelligence, zero speed. | Partly |
| 3 | **Curve velocity as SOL per *swap*** | The single most informative published predictor of graduation. | One division. | **Wrong units** |
| 4 | **Funding-graph creator clustering, up to 3 hops** | A per-address dev-history lookup is defeated **55% of the time**. | Cached graph query. | Partly |
| 5 | **Bundle-adjusted holder concentration** | Discriminative gap of **24pp**, against **6pp** for raw top-10. | Cached bundle resolution. | **No** |
| 6 | **Bot-dominated early activity** | Predicts *lower* graduation. | Free from the trade stream. | No |
| 7 | **Certificate transparency for claim subdomains** | 3–4 days of lead, ~100% precision, 55–60% recall. Verified here today: `claim.scroll.io` certificate 2024-10-19 against a 2024-10-22 event. | Free HTTP. | **No** |
| 8 | Holder concentration, raw top-10 | 6pp gap. Weak, and **no published study establishes any threshold** — ours is invented. | Cheap. | Yes |
| 9 | Wallet PnL grading | **No established persistence.** | Expensive. | Yes, unfed |

**The headline.** Our system implements #8 and #9 well — the weakest and the unestablished
one — and implements none of #1, #5 or #7, which are cheap and well-evidenced. Number 3 we
implement with the wrong denominator: we use SOL per minute where the literature uses SOL
per swap, and the research notes most implementations make exactly this error.

---

## 2. The best method

### Put the boundary between *detect* and *decide*, not between *decide* and *send*

The language is almost never the binding constraint. The data path is. A Rust bot polling
HTTP loses to a Python bot on a gRPC stream. Detection by HTTP polling is 400 ms to 30 s
late, which is 1.6 to 120 slots, and that is fatal regardless of language.

The architecture that follows is a thin Go or Rust ingestion daemon on a shred or
preconfirmation feed, publishing decoded events over a local socket, with Python doing
strategy, risk and accounting, and submitting on warm keep-alive HTTPS to two paths in
parallel with preflight disabled.

### Treat graduation as an exit, not an entry

**84% of graduates are down more than 70% within 20 minutes.** Our `migration-fade` lane is
pointed the right way. Any lane that buys a graduation is pointed the wrong way.

### Build the execution envelope before any signal

This is the most boring and best-evidenced finding in the entire review. A deterministic
bracket recovered +39.0 bps per position and needs neither speed nor prediction. Half of
all positions that went meaningfully green still closed red. That is an exit-discipline
problem, not a signal problem, and it is solvable without predicting anything.

### Accept what is closed, and stop building toward it

Definitively out of reach on our architecture: first-slot and same-slot sniping, the
first-bundle race, atomic backrun arbitrage, JIT liquidity, liquidation races, and
sandwiching, which is structurally dead since Jito removed the public mempool in 2024.
Jito auctions run on 50 ms ticks and prioritise by tip-per-CU; at 1–2 seconds we are 20–40
ticks late. Our measured decision path is 7.6 seconds for a safety scan against 14 launches
per minute.

---

## 3. The best edge, stated honestly

There are four candidates and only the last two are defensible for us.

**Speed.** Closed. The frontier is a well-placed Amsterdam client at 0.29 slots. We are at
4–8 slots, 14–28× slower. The tier that makes the frontier real is roughly $1,500–$3,500 a
month plus tips, and tips at 1,000 contested attempts a day run $3,300–$21,600 a month.

**Prediction.** Very weak. About 94% of Solana memecoin traders lose money over 90 days,
and among the ~6% who profit, 88% make under $100. The best published risk model turns a
−61% expected loss into a −27% expected loss; it does not turn it positive. A pre-registered
model went from AUROC 0.86 to 0.46 across a two-week holdout on the same platform, and
cross-venue transfer was worse than random.

**Coordination.** Out of scope, and correctly so. The plausible edge behind many persistent
cohorts is knowing the mint before it is public. Coordinated pre-launch accumulation with
undisclosed selling is the behaviour the literature documents as manipulation.

**Exclusion plus discipline plus lead time.** This is ours if we want it. Cheap
well-evidenced filters that cut the loss rate, mechanical exits that recover measured basis
points, and free pre-announcement signals like certificate transparency and venue
announcement endpoints that give days of warning. None of it requires being fast and none
of it requires being right about which token moons.

### Where the money verifiably is, ranked

This table is the single most useful output of the whole review. It ranks by evidence
quality and durability, not by headline size. Every figure is from a published percentage
or a measured dollar total.

| Rank | Play | Economics | Evidence |
|---|---|---|---|
| 1 | **Referral and fee-share capture** | 30% of a 1% fee = **27 bps of referred volume**. **$471M paid out all-time, $36.8M in the last 30 days** across tracked apps. | Published percentages |
| 2 | **Creator-fee capture** | **0.95% of post-graduation volume** in the sweet spot, which is 79% of every fee dollar. About **$620M a year** flows to creators. StonkFun did $13.9M in its first month. | Published fee table, read from mainnet |
| 3 | **Infrastructure resale** | **~$400/mo of bare metal resells as a $2,900/mo dedicated node.** Counter-cyclical to trading. | Published price lists |
| 4 | Prop-AMM market making | **29.6% of all Solana DEX volume, $597B all-time, ~zero declared fees** because the earnings are spread. | Structural, PnL unmeasured |
| 5 | Arbitrage against prop AMMs | **62.3% profitable with access, 21.0% and a net loss without it.** Average arb profit fell ~100× since 2024, to about $0.016. | Peer-reviewed |
| 6 | Passive memecoin LP | 126% gross APR headline, but the capital-weighted reality is **0.01–1.6% a year**, and every concentrated-LP study finds passive LPs lose. | Gross verified, net unknown |
| 7 | Sandwiching and validator MEV | Structurally closed. Tips fell **98%**. | Verified closed |
| 8 | Directional trading | **The best single day among 561 curated professional traders was $33,820.** By rank 45 it is under $2,000. | Verified |
| 9 | Copy trading | **Provably negative.** | Theorem |

**On copy trading, the result is stronger than "unproven".** It is a theorem: the leader's
buy moves price along the bonding curve, so the copier's buy necessarily executes at a
strictly worse point. No latency optimisation removes it, because only being *ahead* of
the leader would, and that is front-running rather than copying. Measured decay is 14% for
the leader to 3% for the copier with a purpose-built research filter, and negative with the
statistical models every commercial product ships. **No academic study, in crypto or
traditional finance, demonstrates that copy traders earn positive risk-adjusted excess
returns.**

Worth noting the asymmetry: copy trading does not work for the copier and works extremely
well for the venue. fomo Wallet, a social copy-trading app, went from $0.2M to **$24.5M in
monthly fees in fourteen months.**

### The wallet leaderboards are advertisements, and ours are built from them

This matters directly, because our 6,236 imported wallets came from GMGN labels.

* **Kolscan was acquired by pump.fun in July 2025.** The largest memecoin launchpad, which
  earns roughly $150M a month in fees from memecoin trading, owns the public scoreboard
  that advertises how profitable memecoin trading is. Its terms disclaim accuracy
  outright, its weekly and monthly toggles return byte-identical numbers to the daily one,
  and its API returns unauthorized. The board is 50 hand-picked wallets.
* **GMGN defines "Smart Money" as "wallet who often earns money"** and publishes no
  computation method, no cost-basis convention and no out-of-sample test. Its own product
  separately flags "wallets that farm copy trade users", so it models the existence of
  wallets whose business is extracting from people copying them.
* **Direct on-chain check of four leaderboard wallets**: two are running at **36% and 49%
  transaction failure rates**, which is bot execution rather than a person. Jupiter's own
  published sybil filter excludes any wallet above 50% failure as "code instead of food".
  The tenth-ranked trader in the world is one percentage point under that line.
* **No cohort-persistence study exists anywhere.** Nobody has published whether last
  year's top-ranked traders are still profitable. Until someone does, every leaderboard
  here is an advertisement.

### Airdrop farming resolved against farmers in six months

Jupiter **cancelled** an already-approved 700M JUP drop and returned the tokens. Kamino
went from 750M tokens across 250,000 wallets to 88M across 15,279 users, then stopped
announcing seasons. marginfi never paid at all. DFlow's farmers got an acquisition instead
of a token. Meteora paid its LP army 15% while the team and reserve took 52%, and its
token's all-time high was its own launch day.

The cost side is now trivially cheap, which is exactly why the surviving programmes moved
to mandatory identity checks, fee-denominated scoring and deliberately opaque formulas.
Measured today: $0.000545 per signature, a **zero** median priority fee, $0.162 per token
account, and $1,650 to stand up 10,000 wallets.

**One time-sensitive item.** Meteora's LP Stimulus Season 2 is the only live programme
with a disclosed denominator, scored at 1,000 points per dollar of trading fees actually
generated. **Its claim window closes 21 October 2026.** Fee-denominated scoring is also
much harder to game than volume, because you have to pay the fee you are scored on.

### Three design constraints that follow

1. **Blockspace is free; app fees are not.** I measured 146 of the last 150 slots at a zero
   minimum priority fee, and Jito's median landed tip at about $0.00025. Meanwhile the bot
   takes 1% and the AMM up to 1.25%. **Optimise fee routing, not latency.**
2. **Venue access dominates strategy quality.** The same arbitrage logic is 62% profitable
   with prop-AMM access and 21% without. Integration beats cleverness.
3. **Everything rotates in about six months.** BullX, Photon, HumidiFi and ZeroFi each went
   from dominant to near-zero inside a year. Build for venue-agnostic redeployment.

### The reframe worth sitting with

The verifiable money on Solana is $1.4B of protocol revenue in a year, $204B of prop-AMM
volume, and $1.13B of pump.fun revenue. **If you can quote, route, or toll rather than
predict, the evidence says do that instead.** That is a different business from the one we
have built, and it is the one with the money in it. I am not recommending we pivot today,
but it should be on the table rather than unexamined.

---

## 3a. The biggest strategic finding: the venue moved off Solana

Measured from the DefiLlama API on 2026-09-20, all-chain launchpad fee ranking:

| Rank | Launchpad | Chain | Fees 30d |
|---|---|---|---|
| **1** | **Pons V2** | **Robinhood Chain** | **$131.0M** |
| 2 | pump.fun | Solana | $46.0M |
| 3 | Flap.sh | BSC / X Layer / Monad / Robinhood | $34.2M |
| 4 | StonkFun | Solana | $13.9M |
| 5 | Pons V1 | Robinhood Chain | $7.3M |

**Pons on Robinhood Chain earns 2.8× pump.fun.** Robinhood Chain does $1.16B a day of DEX
volume against Solana's $3.23B, which puts it level with Ethereum and ahead of BSC. Ten or
more launchpads have deployed there on a chain roughly a year old, and both Bags and
clanker have added deployments.

**We have a `pons-robinhood` lane already, and it cannot execute.** Our signer policy has
**zero router addresses configured for Robinhood**, against five for BSC and four for Base,
so every transaction on that chain is refused by construction. Finding and verifying the
router address is now the highest-value single unblock in the system, ahead of anything on
Solana.

**On Solana itself the landscape moved too.** StonkFun did not exist before July 2026 and
is already the #2 launchpad by fees, taking 8.6% of launches but **42 of Jupiter's top 100
tokens by organic score**. bonk.fun returned **zero** launches in a 197-token sample.
Believe, Heaven, boop.fun and time.fun are dead; Bags collapsed in early September.
Measured pump.fun graduation rate is about **5%**, and graduation is 85 SOL, about $9,262
raised at a $44,769 market cap.

## 3b. Facts that moved recently and will break stale assumptions

Four of these post-date most of what is written about this space, and two of them are in
our code or config today.

| Fact | As of | Consequence |
|---|---|---|
| Solana slots are **250 ms** | 2026-09-18 | The blockhash window is now ~**37.5 s** of wall clock, not the ~90 s a 2024-era bot assumes. A queueing bot breaks silently. |
| **Jito ShredStream shut down** | 2026-09-05 | Any design naming it is already dead. |
| **BAM preconfirmations live**, 34% of stake | 2026-09-09 | The fastest tier is now 5–10 ms ahead of shred streams and reachable only through a vendor. |
| pump.fun graduation is ≈**$44,700**, not $69k | at SOL $108.89 | The curve is unchanged at 30+85=115 SOL; the dollar figure everyone quotes is stale. |
| Meteora pool fees permit a **99% maximum** with configurable decay | current | A first-slot buy into that schedule is a donation. Our code must not assume a fee ceiling. |
| **Solana runs at 250 ms slots**, confirmed by reading the feature-gate accounts on-chain | activated 2026-09-16 | Three slot-time cuts landed in 28 days (400→350→300→250 ms). Nobody's cost models have caught up. |
| The MEV tip collapse is **structural, not cyclical** | measured | In SOL terms tips fell 25–30× while priority fees fell only 4–8× and are recovering. August 2026 was the best month for L1 fees since February 2025. The private tip auction lost to prop-AMM internalisation, not to a competitor: bloXroute fell 99.75% and Nozomi 96% over the same window. |
| Geography beats vendor routing | measured | Anza's **0.29 slots from Amsterdam on a plain staked connection** beats the leading commercial router's advertised 0.77 p50. |

## 4. What this changes in our system

**Add (cheap, well-evidenced, none of it needs speed):**

1. IPFS image and metadata hash de-duplication on every new token.
2. Bundle-adjusted holder concentration, replacing raw top-10 as the primary read.
3. Certificate transparency watcher, plus the Binance, OKX and Upbit announcement
   endpoints, all free and verified live.
4. Mechanical brackets on every position, before any further signal work.

**Fix:**

5. Curve velocity to SOL per swap. Needs trade-level data, so it is Phase 1.
6. Creator clustering to a 3-hop funding graph rather than a per-address lookup.
7. Stop hard-coding the 85 SOL graduation threshold; it is wrong for Meteora DBC, which is
   configurable per launch. Stop hard-coding venue program IDs; 39% of Solana DEX volume
   sits in prop AMMs that redeploy, and four went from billions to zero this year.
8. Refresh blockhashes aggressively or use durable nonces. The 150-block window is now
   about 37.5 seconds of wall clock, which will silently break a queueing bot written
   against 2024 assumptions.

**Demote:**

9. Wallet grading, from pillar to instrumented hypothesis with a matched control arm.
10. Copy-trading, from entry trigger to at most a sizing input. Four of five selection
    methods in the one paper that models it produce positive leader and negative copier
    returns at zero latency.

**Delete:**

11. The NFT mint hunter. 8% of 75 paid Solana launchpad mints trade at or above mint, and
    mint-everything returns −85.4%.
12. Airdrop farming as a revenue line. Keep the detection; it is worth more pointed at the
    trading book.

**Recalibrate — our filter weights contradict the published base rates:**

13. **`freeze_authority_live` is a blocker in our dossier.** Freeze-authority abuse is
    **0.6% of Solana rugs**. Keeping it as a blocker is cheap, but it is doing almost none
    of the work we think it is, and it should not be counted as coverage.
14. **`wash_trading` is a warning in our dossier.** Wash trading **positively** predicts
    graduation, 2.0% against 0.90%, p = 3×10⁻⁵⁹. That is the opposite of consensus and the
    opposite of how we score it. Either flip it or remove it, but do not keep penalising it.
15. **`top10_concentration > 35%` is our threshold.** **No published study establishes any
    holder-concentration threshold.** Ours is invented. The evidenced version is
    bundle-adjusted concentration, which is a different measurement.
16. **"The dev bought his own bundle" has a 98.7% base rate.** A signal that fires on
    essentially every launch carries no information. Check whether anything in our stack
    treats it as adverse.
17. Discard every threshold set before 2026. Graduation rates moved roughly 10× in 18
    months. Hold out by time, never at random; every temporal generalisation test in the
    literature failed.
