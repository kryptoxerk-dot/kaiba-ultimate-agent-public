# Autonomous paper learning

Updated 2026-10-15. Owned by Codex under AUTOLEARN-IMPLEMENT-20261014 and AGENT-UPGRADE-20261015.

The worker searches historical evidence, freezes experiments, runs separate PaperBroker
books on newly captured prices, records outcomes in the existing journal, and evaluates
fresh results. It does not arm capital, change risk or lane modes, promote wallets, activate
playbook rules, or guarantee profit. A passing paper result remains a research result.

## What runs

The initial campaign has twelve books: four exits for `sm-trenches` on each of Solana,
BSC and Robinhood. The exits are the captured incumbent ladder, a 60-second maximum
hold, a five-minute maximum hold, and taking the entire position at 1.25 times entry.
Other ladder settings remain frozen. These numbers are hypotheses, not measured edges.

At most eighteen books exist across at most two campaigns. When enough complete
historical evidence produces a search survivor, daily discovery can freeze a second
campaign after seven days, using remaining capacity for incumbent/learned-candidate
pairs. Existing books retain their original filters and exits. The worker does not
continuously replace campaigns or rewrite its own source code.

Each prospective entry uses a flat **$30 paper stake including the entry transaction
tip**, converted using the native price captured at the actual fill snapshot. Confidence
scores and the incumbent decision's size do not increase this amount. Each arm has the
same three-open-position capacity. The fixed research ceiling is $120 per entry.

The prospective population is **incumbent entry/scale-in decisions with a positive
configured size**, not every scanned token or every wallet nomination. Filter exclusions,
missed opportunities, refusals and unresolved inventory remain visible. This experiment
does not establish the profitability of buying tokens the incumbent never admitted.

## Prices, costs and verdicts

The worker uses stored, fresh curve reserves or pool price/depth receipts, with known
decimals and a native price observed no later than the captured quote. Missing, stale or
future receipts cannot invent a fill. Entry features come from the original signal
payload with matching lane/chain/token identity and usable pre-decision measurements.
Only those same prospective feature fields may be learned from the replay.

The input adapter also reuses existing GMGN `token info` cache responses. A matching
chain/address response can supply exact decimal precision for at most 24 hours;
no chain-wide precision default is guessed. A fresh coherent cached price/depth packet
can supply a quote under the same sixty-second quote limit. Capture time and a SHA256
receipt are retained; touching the file cannot refresh it. Precision captured after a
price advances quote availability, and cannot backdate a curve or native-price sample.
Source metadata and price failures are recorded explicitly for each attempted entry.

An entry requires a quote at least thirty seconds after the decision and becomes
unreachable after 120 seconds. Price exits require a later quote at least twelve seconds
after their trigger; actual collection delays can be longer and are recorded. Quotes
older than sixty seconds cannot advance a book. Between-quote price excursions are
unobserved and cannot trigger modeled exits.

Quote availability and price-observation time are separate. A later depth or precision
receipt cannot satisfy the entry/exit delay using an older price, or trigger protection
twice. The frozen runner policy records this rule; existing populated books cannot
silently adopt it. Legacy packets without a separate price clock are treated as coherent
at their original quote clock, and existing protection clocks are preserved conservatively.

PaperBroker's entry debit and exit credit already include modeled fees/tips/impact.
The evaluator never subtracts the explanatory fee column a second time. Unknown EVM
token taxes prevent complete cost evidence, even when a modeled close looks profitable.
Paper model completeness is distinct from verified live wallet economics.

`UNPROVEN` preserves unknown economics, open/unfillable entered inventory, missing
paired incumbent evidence, changed code or stale complete closes. Proved refusals with
no order/fill/inventory and zero cost retain coverage; they receive no invented return
denominator. Pre-freeze signals and filter exclusions do not become unresolved trades.

`PAPER_EVIDENCE_READY` requires the existing absolute net pass line, correction across
every frozen arm, corresponding incumbent signal comparisons, and three consecutive
UTC dates with genuinely new complete outcome IDs beyond the previous close frontier.
The pass line requires at least forty complete closes, fifteen in each elapsed-time half,
positive means in both halves and a positive 90% bootstrap lower bound. A new report
timestamp, historical search survivor, repeated row or aging sample cannot establish
readiness. The result always has `funding_allowed: false`.

Complete paper outcomes create evidence-linked lessons and hit/miss credits using the
existing journal/playbook APIs. Evidence IDs prevent repeat credit after retries. Rules
remain **proposed**. Incomplete canonical live outcomes retain their ledger values and
unknown economic values; the projection is not a claim of fully reconciled live PnL.

## Run and inspect

From `~/kaiba` on the VPS, with the configured Kaiba environment:

```sh
timeout -k 5 900 nice -n 19 ionice -c3 .venv/bin/python -m kaiba.learning.autolearn --once
systemctl --user status kaiba-autolearn.timer kaiba-autolearn.service
journalctl --user -u kaiba-autolearn.service -n 40 --no-pager
```

The new user timer starts two minutes after boot and runs sixty seconds after the
previous worker finishes. Its unit applies low CPU/I/O priority, a 900-second hard
timeout and a 350 MB memory ceiling (verify effective unit overrides). It uses local captured data, with network
address families restricted to Unix sockets. Existing market-data services supply prices.

Research evidence lives under `data/learning/autolearn/`:

- `latest-report.md` and `latest-report.json`: statuses, coverage, errors and query receipts.
- `state.json`: cursors, campaign registrations, readiness state and pending journal writes.
- `pc_*.json`: frozen manifests, entry filters, exits, data cutoff and source hashes.
- `latest-discovery.json`: bounded historical search and its unproven candidate manifest.
- `discovery-work.json`: resumable frozen roster, processed cursor and priced observations;
  `previous-discovery-work.json` keeps the last completed/archived work snapshot.
- `latest-economic-outcomes.json`: the latest canonical closed-position projection.
- `latest-wallet-review.json`: source freshness, exclusions, observation queue and bounded research intake.
- `latest-agent-review.json`: hourly chain/lane ledger, fee coverage, operating failures and paper-entry funnel.
- `agent-review-<UTC-day-number>.json`: immutable complete daily journal receipt;
  `receipt_hash` is SHA256 of canonical sorted compact JSON, not the pretty-printed file.
- `retired-*.json`: retirement receipts for code-changed campaigns that never created any order or position.
- `paper/candidate-*.db`: isolated PaperBroker books, attempted entries and fill evidence.

Historical discovery is due daily and skips while pending/open paper inventory needs a
poll. It freezes at most 750 settled historical signals, capped fairly by group, with the
original audit's first row ID per token/UTC day and a frozen source row-ID frontier.
It prices that same roster in resumable twenty-second chunks on idle worker ticks.
Large tapes use exact `(timestamp,rowid)` paging under a frozen global insert frontier;
ties and late backfills cannot silently change the sample. An oversized tape or three
nonprogressing tape attempts ends the whole sample as unproven. WAL admission deferrals
do not count as evidence failure. Each completed signal persists its observation/cursor; no raw swap tape is saved. A
guarded query deferral preserves that cursor. **Only a fully processed frozen roster
reaches the strategy factory.** Interrupted samples supply no candidate observations.

The history window starts at most twelve days back and respects the swaps retention
watermark. Every resumed chunk checks that its oldest swap, including the pre-signal
lookback, is within thirteen days of the current time; an aging roster cannot bypass this.
Deleted/changed selected signals, changed collector code/policy, or a later watermark
expiring a selected signal invalidate the whole sample. The error stays visible and its
work remains available; the next eligible daily collection uses a fresh roster. Completed
discovery records include selection evidence and an aggregate journal lesson. A small
or unpriceable sample may produce no learned candidate. No new measured strategy is
implied by the existence of the worker or its timer.

A collector code/policy upgrade journals and retains invalidated partial work, then starts
a separately frozen sample on the next idle tick. It never continues that old sample under
new code or changes an existing forward paper strategy.

## Database and upgrade discipline

Every source query checks WAL admission first, has a three-second default progress
deadline, enforces a row cap and closes its connection before further work. WAL above
1.5 GB defers source work with at least sixty seconds of retry backoff. Do not bypass
these guards, hold a read transaction over sixty seconds, or copy the live database.
The service may freeze an empty exploratory campaign while source reads are deferred;
that records hypotheses and creates no evidence of profitable execution.

The manifest freezes source hashes for the campaign/search/evaluator, broker, protection,
candidate runner and input adapter. Changing those files invalidates the old campaign's
eligibility; an existing book also refuses changes to its frozen execution identity.
An entirely never-filled campaign can be retired automatically after checking **every**
book has no orders and no positions. Its manifest, entries, assessment and books are
retained; a new campaign starts at the current clock. Any actual broker history refuses
automatic retirement and produces a current partial report requiring integration.
Do not relabel old cashflows with a new spec hash,
erase state to make a stale result look fresh, or silently continue an old cohort under
new pricing or feature code.

Expired pending entries with proven zero order/position exposure can expire locally
even during source WAL backoff. Existing or uncertain exposure never receives this
shortcut. Old failures without diagnostics retain unknown causes.

Idle expiry and watch-list checks use fresh read-only book connections. A due expiry
closes its read probe, acquires the configured writer, and rechecks identity and actual
order/position exposure inside its transaction. Existing schemas run no repeated
migrations or CREATE statements. The worker performs one pre-admission expiry pass;
watching the same frozen tick does not repeat it. This removes measured polling I/O
without relaxing entry freshness, delay, costs or risk policy.

## Wallet observation and agent review

Every fifteen minutes the worker reviews existing runner exports and captured provider
profiles. Address identity includes chain; Solana casing is preserved. Provider `_at`
timestamps, adverse history, operator labels and known entity links remain evidence.
Positive provider PnL is a research nomination, and a fast sniper requires a delayed
follower test. None of these data makes a wallet copy-qualified.

The twenty-slot observation queue reserves exploration and rotates fairly across chains
and known entities. At most twenty previously absent identities enter the existing
`research` cohort per UTC day; existing wallet records and trust are preserved. The
existing scheduled gatherer decides whether to collect history under its existing quotas.
BSC/Robinhood have no equivalent paid full-history route here, and registry intake is
not evidence that observation has finished. No provider credit allowance is increased.

Hourly operating reviews retain separate populations for twelve-day live closes, the
last two thousand order-rowid slots and the last eight hundred operation-rowid slots.
Truncation, unavailable sections and unreconciled costs remain visible. Native assets
are never pooled across chains. Daily evidence-linked journal entries record these
reviews and proposed repairs; proposed playbook rules do not become trading filters.
The daily journal stores a compact valid JSON summary and receipt hash. A lost caller
acknowledgment reuses the first immutable daily receipt; it cannot overwrite the evidence
already named by a canonical journal row.
An operating timer, a new wallet label, or zero surviving historical strategies cannot
establish profitable trading.

Focused offline verification from the repository root:

Processing time is separate from captured market time. The worker refreshes its
frontier after I/O; paper books recheck freshness and entry expiry after opening a
book and immediately before an order. A fresh metadata receipt cannot refresh the
price, depth or native conversion rate. Original quote clocks remain in the fill
evidence beside actual processing time. Changed processing policies require a new
frozen campaign; existing broker history must be preserved and integrated.

Selected-wallet behavior uses the existing inventory-aware analyzers on bounded
retained tape. Its fixed selection and baseline receipts distinguish new trades
from the sale of an old bag. These are partial gross leader observations; missing
transfers, opening custody, fees, failures and achievable follower execution remain
unknown. Research imports are not evidence that history was collected: acquisition
diagnostics distinguish the existing SOL quota from unavailable scheduled EVM routes.

```sh
.venv/bin/python -m pytest tests/test_strategy_campaign.py tests/test_candidate_paper.py tests/test_economic_outcomes.py tests/test_autolearn_io.py tests/test_autolearn_discovery.py tests/test_autolearn.py -q
.venv/bin/python -m ruff check kaiba/learning/strategy_campaign.py kaiba/learning/candidate_paper.py kaiba/learning/economic_outcomes.py kaiba/learning/autolearn_io.py kaiba/learning/autolearn_discovery.py kaiba/learning/autolearn.py
```

Windows uses `.venv/Scripts/python.exe` for the same offline commands. Fixtures must
remain separate from live provider proof. Deployment/service receipts and campaign
evidence must be inspected before claiming the worker is running or a strategy is profitable.

Economic evidence is a separate rolling historical audit, not forward strategy
validation. `economic-work.json` checkpoints at most 128 real-money episodes within
a 16 MiB budget, retaining raw packets no larger than 64 KiB. Each tick can capture
one episode, rotating priority among new twelve-day closes, open inventory and
older retries. Candidate lookahead stays bounded to three closes, two open rows
and three retries. Cursors acknowledge only the captured contiguous prefix.
Closed-window sweeps can reacquire evicted episodes. Eviction, expiration, missing
rows and raw truncation are explicit; this retained subset never claims to be the
whole trading population. The assembler uses fresh guarded connections, at most 3 seconds
per query and 15 seconds / 128 queries per command. No source ledger or provider is written
by capture. Future or contradictory fee rows remain raw audit evidence while their
known-current-fee fields remain unavailable.

Unchanged retries preserve the evidence revision. Late evidence changes retain
source hashes and enter a durable 256-record journal outbox before caller state
can acknowledge them. Canonical idempotent journalling removes only acknowledged
outbox IDs. A full outbox defers further selection so existing receipts can drain.
These revisions earn no trading hit/miss credit and never manufacture complete
wallet identity, custody, failed-attempt coverage, recoveries or economic cashflow.

The candidate fee model includes a published GMGN commission assumption separately
from venue/tax evidence. Generic SOL/RH pool allowances are 225/160 bps; BSC 200 bps
already includes its commission allowance. The PumpSwap path is 220 bps; the curve
path uses a conservative derived 228 bps aggregate bound. These are modeled fees,
not observed all-in route costs. Missing venue or tax evidence permits research
fills but leaves cost/evidence readiness incomplete. Policy/source changes freeze
a new experiment; populated books cannot be reinterpreted in place.

Selected-wallet acquisition still needs the existing collector owner's integration.
See [the concrete review-only handoff](../research/codex-wallet-acquisition-handoff-20261015.md)
for the SOL nomination hook, crash/accounting proofs and EVM limitations. It does
not authorize a second paid collector, new quotas or fabricated provider labels.

The research timer waits fifteen seconds after each completed oneshot. Effective
polling cadence includes the cycle's duration. Per-phase receipts expose source,
quote, paper and optional-maintenance time. Candidates retain separate first source
completion, dispatch, registration, preparation, admission and quote clocks; none
can refresh a price or backdate a fill. Expiry receipts distinguish observed stale
quotes from processing delays and leave availability between polls unknown.

Pending entries defer slow optional maintenance for at most two minutes. Economic
capture is eligible at most once per minute. Optional review/capture query failures
and SQLite intake contention have separate sixty-second retry clocks so the next
paper poll can proceed. Every actual read still checks WAL pressure and its short
deadline; WAL-over-limit failures anywhere retain the global sixty-second pause.
Cached economics preserve the original source frontier and journal outbox. A
zero-read full-outbox pass cannot claim a new economic observation.

Canonical journal write contention also has its own retry clock. A failed batch
keeps every pending evidence ID and acknowledges no economic revision, including
when a prefix committed before the error. The existing canonical dedupe receipt
resolves that prefix on retry. This cycle still attempts at most forty lesson IDs;
an unacknowledged startup ID remains visibly pending. Nested operating-review and
discovery result packets preserve WAL refusals into the global source pause.

For a research code update, stop only this owned timer, wait for its service to
finish naturally, then apply the staged compare-and-swap under the worker lock.
This prevents a process that imported old modules before acquiring the lock from
mixing versions. Verify the actual user-unit timer, reload user units and start
only the research timer afterward. Financial/protection services remain running.


## Targeted quote observer and original receipt store (2026-10-15)

`kaiba-autolearn-quotes.timer` runs the separate bounded quote observer every
10 seconds after completion. The main research timer remains at 15 seconds and
its service remains restricted to AF_UNIX. The observer uses typed GMGN token.info
at RESEARCH priority through the existing shared limiter. It admits one token per
invocation, at most 100 attempts per UTC day, for genuine pending or open PAPER
candidates from the active frozen campaign. It retains original provider/cache
clocks and accepts no stale grace period. Research requests cannot execute a trade.

Inspect `data/learning/prospective-quotes/latest-report.json` and `state.json` for
original receipt hashes, admitted attempts, target windows and refusal reasons.
A cadence deferral or empty eligible population can be normal. A source-version
mismatch must be resolved by the main worker's preserved-history campaign upgrade;
do not edit manifests or backdate observations. Both units use bounded runtime,
memory, low CPU priority and idle I/O.

The new `transaction_receipts` module stores one explicitly selected original
Solana getTransaction attempt under `data/learning/transaction-receipts/`. It uses
the existing fill-cost job lock and shared RESEARCH RPC limiter, with durable
admission limits and immutable response hashes. The current manual route permits
only the configured unauthenticated public mainnet endpoint. No full-database
scan or provider cache dump is needed. A limiter refusal is not a transaction
response, and an ambiguous admitted attempt must not be reset to force a retry.

Full stored responses, when available, can establish observed transaction
components. Historical trading-wallet identity, external funding, rent/recovery,
complete attempts and custody reconciliation still need independent proof before
an episode may be labeled economically complete. Never substitute the current
wallet or fee payer for missing historical submission identity.

Deployment proof: 721 staged tests passed; journal6340 records the eighteen exact
installed source hashes. `docs/research/codex-observation-20261015.md` records the
actual first quote captures, RPC cooldown refusal and wallet quota/registry limits.


The selected-wallet report additionally exposes `acquisition_queue`,
`acquisition_candidates` and `acquisition_learning`. The corresponding bounded
state is retained under the main worker's `wallet_review_state.acquisition_state`.
These records preserve original nomination clocks through export rotation and
are separate from the prospective behavior-study selection. Only a current
registered research identity passing fresh exclusions may appear in the manual
acquisition queue. Newly imported identities wait for the next registry check.
The20/day intake limit, existing provider quota and grade eligibility still apply.
Unknown/future cursor or registry evidence prevents a false completion. A source
upgrade with incompatible acquisition state requires explicit migration.


Final wallet-continuity build:745staged tests passed in98.63s,19isolated acquisition
proofs passed in39.951s, and canonicaljournal6346 verifies the latest eighteen
source hashes. The first full runtime review completed with3pending SOL
nominations,0currently collectible targets and intake20/20. This is evidence
continuity, not a qualified-wallet or profitable-strategy claim. Cold reviews may
take minutes;15s/10s are intervals after completion, not guaranteed response times.

## Admission-clock cohort upgrade

The owned research command supports `--admission-clock`. This flag leaves the
default library runner compatible, while the research unit uses a separate
`admission_paper/1` identity and `autolearn_clock/1` transition controller.
Prices keep their original component clocks. Broker ledger times use actual
admission; timed holds start there, and price exits wait after actual recognition.
Fee/tax assumptions and the statistical family remain separately qualified.

Inspect `data/learning/autolearn/state.json` and `latest-report.json` under
`clock_upgrade`. During `phase: draining`, old books accept no new registrations;
their existing pending attempts and ordinary exits continue. Open inventory,
ambiguous orders, unmatched quantities, missing books or capped evidence block
cutover. Do not fake a close, delete history, rewrite an identity or add a second
active allowance to get past this state.

After every old arm is freshly settled, the worker retains original manifests,
books, WAL hashes, outcome IDs and assessments in its cutover receipt. The atomic
active pointer names one separately frozen cohort, capped at the configured
twelve arms/$30stake/three open positions per arm. Its activation frontier can be
later than hypothesis preparation; only signals strictly after activation may
register. Recovery before pointer commit uses a fresh frontier. Recovery after a
committed pointer preserves the same cohort, even under KeyboardInterrupt or
SystemExit and the parent's unconditional finally persistence.

The quote observer selects the exact validated legacy or admission runner for
each active book. Existing observer state and its100/day durable admission limit
are preserved. A local refusal is an admitted attempt only if its durable
reservation already happened; never reset counters to retry an ambiguous read.

Deployment must stage against current VPS dependencies and verify exact raw
base-nine model hashes separately from normalized deployment hashes. Hold only
the two research timers, allow their workers to finish, then CAS the owned source
and research unit. Reload and verify the loaded ExecStart includes
`--admission-clock` before resuming those timers. Financial services are outside
this procedure. Root helpers in `data/codex-execution-evidence-20261015/` retain
installation, runtime and indexed canonical acknowledgment receipts.

The selected-wallet collector and executor submission-intent hooks remain
separate lead-owned integration candidates. Neither this flag nor a statistically
successful paper model authorizes changing financial sizing, modes or rosters.

## Observer admission quality

The observer now checks an exact bounded canonical cache before admitting a new
read. A still-fresh valid snapshot is left at its original `fetched_ms`, with no
new receipt or counter increment. Only its fairness cursor advances. A read-only
preflight may also defer a known current GMGN research refusal; two indexed
queries each close their connection and retain the existing WAL/backoff guards.
Missing/expired in-flight leases and uncertain state still reach the ordinary
atomic limiter. The preflight never reserves, reclaims, changes priorities or
grants permission to bypass that limiter.

The admission day, token cadence and pending-entry deadline are checked again
after preflight I/O. Once an attempt is durably admitted it is never refunded or
automatically retried, even if the provider wrapper later sees a cache hit or a
new limiter refusal. `provider_attempts` counts typed-wrapper invocations; it is
not a network-call or billed-spend counter. Unavailable results retain fixed
failure categories, never raw provider notes. Old causes that were not retained
remain unknown.

The 2026-10-15 release changed only `prospective_quotes.py`; all other deployed
research sources, unit files, frozen paper policies, existing counters and
financial services were preserved. Current-VPS-source stage:853 tests passed,
exact-file Ruff and all4 unit checks passed. Installation identity is
`f4353458e706ad015608a8bb`, acknowledged in canonical journal6363. The paper
lesson in journal6358 and wallet lesson in journal6362 record the measured
six losses/two original signals, cost/stop-quote findings and separate wallet
windows as unproven research. Exhausted-day cycles skip book/limiter reads and
report eligibility unknown; next-day normal admission remains verified.
See `docs/research/codex-observation-quality-20261015.md` and the bounded preserved
receipt bundle. Local offline reproduction needs no provider or database:

```powershell
.venv/Scripts/python.exe docs/research/codex-observation-quality-20261015/reproduce.py
```
