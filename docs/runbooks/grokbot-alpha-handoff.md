# Grokbot early-alpha handoff — 2 November 2026

the operator is having Grokbot develop X discovery for NFT mints, tokens, airdrops and
new chains. The original local skill/contract checkpoint is now followed by a
deployed signed intake and real scheduled delivery. See
[runtime verification](../research/codex-grok-alpha-runtime-20261102.md).
The actual Grok Bot and Claude Code sessions are cooperating under the owner's
subsequent explicit request. Preserve their current board ownership.

## What already exists

These are source checks in the local checkout, not a fresh VPS feed-health test:

- `kaiba/providers/x_search.py` selects TwitterAPI.io or X API recent search.
  `kaiba_x_search` in `kaiba/mcp/server.py` uses that adapter. It is not a native
  Grok connector. Historical prices/availability in its docstring are not current
  account allowance evidence.
- `kaiba_tweets` reads the existing tweet surface. `kaiba/ingest/social_dispatch.py`
  fans the single provider stream out before costly enrichment.
- `kaiba/hunters/social_hunt.py` has `nft`, `airdrop` and `token` patterns/CLI
  lanes, a separate bounded inbox, source context and receipt/dispatch timing.
  There is no dedicated `chain` lane. Keyword matching is research triage.
- Existing `social-nft-hunt`, `social-airdrop-hunt`, `airdrop-hunter` and
  `early-alpha-hunt` describe downstream assessment. Reuse their implementations;
  do not replace them with a second watcher merely to label it Grok-powered.
- The older social monitor profile integration gap remains documented in
  `docs/runbooks/social-hunts.md`. This work did not inspect or change that
  protected Hermes copy and does not claim its runtime gap has been fixed.

## Deliverable and use

Read `skills/grok-alpha-research/SKILL.md` for the discovery/research workflow and
its `references/handoff.md` for the deployed evidence-card contract. The skill
supports Grokbot's real available X tools when used there and Kaiba's existing
tools when used in Hermes. It does not invent tool access or install a provider.

The contract preserves original sources, observation/receipt times, revisions,
cost usage, verification gaps and track-specific economics. It separates a
project announcement from eligibility, enforceable transaction conditions and
realizable proceeds. New-chain discoveries remain research cards pending a
supported route, not automatic funding or signer/network changes.

The original research skill is local. A separate tested processing skill and
monitor are installed in the operator profile with the signed receiver; Grok's
existing routine now delivers actual observations. Claude owns downstream
processing/cron/dashboard integration, now source-verified on the VPS. One
selective model follow-up is installed PAUSED: the operator/research profiles
lack usable Claude credentials. The deterministic five-minute processor and
Grok's scheduled signed delivery continue. The runtime report distinguishes
tested/deployed from pending work. BAW pairing remains deferred and its automatic
NFT execution remains incomplete/disabled.

## Official Grok capability and cost check

Grok Bot has a persistent cloud computer and can use connected apps or its
browser. That is distinct from calling the separately metered developer API;
verify the owner's actual browser/tool access before choosing a connector.
[Grok Bot overview](https://docs.x.ai/grok-bot/overview).
Its documented routine schedules are at least five minutes apart, so a scheduled
browser sweep cannot be advertised as a subsecond feed. Keep existing delivered
events available for fast observation and use browser/research discovery to
extend coverage. [Skills and routines](https://docs.x.ai/grok-bot/skills-routines-and-automations).

Grok's documented X tool supports keyword/semantic search, user discovery and
thread retrieval. Search handle/date filters do not constrain thread fetching;
date windows use UTC days. Returned usage reports posts/profiles fetched across
the request, including repeated retrievals. This supports targeted research but
does not promise complete or instantaneous coverage.
[Official X Search](https://docs.x.ai/developers/tools/x-search).

The API currently bills X retrieval by posts/profiles, plus model tokens; media
analysis adds token usage. Existing Grok application access must not be treated
as authorization for new API spend. The actual integration uses Grok Bot's
included native public-X tools, not a newly provisioned developer API key.
Additional account usage is disabled with a monthly cap of zero. Use current
included allowances/free capabilities only; no new top-ups or subscriptions.
[Official API pricing reference](https://docs.x.ai/developers/pricing).

## Review Grokbot's work before wiring it

1. Identify the real delivery mechanism and exact code/skill version. Confirm
   which runtime owns X access, account allowance and usage evidence. Do not
   copy browser storage or extract credentials to manufacture an integration.
2. Preserve its original artifacts and compare with the current local/VPS source
   before editing shared files. Keep the single existing stream; use Grok for
   complementary discovery or gap checks where actual capability is available.
3. Add a bounded adapter only after observing the real output shape. Stamp local
   receipt, preserve evidence/revisions, deduplicate without erasing changes,
   and retain unresolved observations. Do not forward a rewritten model summary
   as a raw provider post.
4. Wire existing NFT/airdrop/token qualification. Propose the new-chain research
   route with the lead; `social_hunt.PATTERNS` and its readers/skills need explicit
   compatible treatment, not a made-up `--lane chain` invocation. Protected
   core/Hermes/risk/signer files remain with the lead.
5. Verify source-matched fixtures and then one bounded real read through existing
   authorized access. Measure publication-to-provider, provider-to-observation,
   observation-to-Kaiba and Kaiba-to-action delays separately. Never assign a
   historical post time to a newly imported result to make latency look better.
6. Track every candidate, rejection, missed window and cost. Compare incremental
   verified opportunities and executable net outcomes against the existing feed
   before claiming earlier discovery or profitability. Source weights and live
   filters need prospective evidence; this task changes neither.

The original skill-only checkpoint has been superseded by the signed adapter,
scheduled receipts and deterministic processing above. No funded NFT execution,
complete timeline coverage or demonstrated edge is claimed. Receiving and
verification work queried only bounded research stores, not canonical trade SQL.
