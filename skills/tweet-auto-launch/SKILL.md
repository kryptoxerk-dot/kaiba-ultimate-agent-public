---
name: tweet-auto-launch
description: Operate the tweet-to-token launch workflow on three chains.
version: 0.1.0
author: the operator, Codex
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [Execution, Social, Launch]
---

# Tweet-triggered token launches

## What this skill is for

Operating Kaiba's tweet-to-token launch workflow for Solana, BNB and Robinhood, with a five-percent supply allocation, staged exits and verified holder-reward routing.

Latest runtime verification 2026-10-07: 53 creator-reference accounts now include the researched 20-account core. Pump.fun/Bags route to SOL, Flap to BNB's Flap+Four.meme pair, and Pons to Robinhood; these four were already on the shared socket as alpha sources. SOL has ten confirmed own launches; BNB and Robinhood are armed/funded and their planning routes pass, but recent own-create receipts are still absent. Four.meme activity now uses its own chain reader rather than Flap's portal; 181 source-matched VPS tests passed. Read [current three-chain evidence](references/three-chain-runtime.md) for scope, timing and unresolved holder/image/fee evidence. Older source hashes and readiness numbers below are historical and do not override this update. No foreign executor, risk, signer, creative source or Hermes auth/profile configuration was modified for this runtime repair.

Owner directives dated 2026-10-06: automate launches from tracked tweets on Solana, BNB and Robinhood; buy 5% at launch; sell in increments; send distributable fees to holders. Interpret 5% as total token supply, not wallet equity. These are standing feature requirements, not evidence that the currently deployed launcher is armed or that any candidate launch fits its financial limits.

## When to use it

- Operating, previewing or reviewing the tweet-launch runner on Solana, BNB or Robinhood.
- Planning a five-percent initial allocation, a volume-backed vamp, staged sales or holder-fee routing for a tweet launch.
- Reporting launch readiness or integration gaps to the lead.

Source: the per-launch ceilings are the owner's 2026-10-07 authorization (`docs/research/codex-tweet-launch-authorization-20261007.md`) and are configured in `config/tweet_launch.yaml`, which also records the later unlimited daily allowances quoted below; test counts, timings and readiness numbers are in [references/readiness.md](references/readiness.md) and [references/three-chain-runtime.md](references/three-chain-runtime.md); vamp details are in `docs/research/codex-tweet-vamp-20261007.md`.

## Procedure

Reuse `kaiba.execution.tweet_launch` and `config/tweet_launch.yaml`. Latest owner directive 2026-10-07: "no daily allowance for every chain / Unlimited". Daily native spend and launch-count allowances are explicitly unlimited on Solana, BNB and Robinhood, including volume-backed vamps and launch-snipe daily counts. This supersedes the earlier 6 SOL / 1 BNB / 0.3 ETH daily allowances and finite daily launch counts. Per-launch ceilings remain 1.5 SOL / 0.5 BNB / 0.1 ETH. These are maximum amounts, not required spend. Loss stops, ordinary trade sizing, exposure, holder routing, source dedupe/cooldown, protection and withdrawals retain their current controls. The separate $20 prepaid Twitter data budget is unchanged. Do not reintroduce historical daily allowances or interpret this as arming a paper-only source. VPS source-matched allowance suite:311passed17.50s; see references/readiness.md for dated history.

The lead changed the launcher after the previously tested Codex release. Current source SHA38b6bf8... uses a reduced admission path that omits free funds, daily-stop reservation and aggregate exposure. Report these foreign-owned integration gaps to the lead; earlier176tests do not verify the changed runner.68focused budget/policy tests now pass locally, including22new boundaries; these do not verify live signing or payout. SOL cashback still pays traders, RH plain Pons lacks holder routing, and J7 compatible signing is absent. Actual net5% allocation/protection/holder receipts remain unverified. Read [references/readiness.md](references/readiness.md) for current evidence and historical snapshots.

## Hermes owns launch words and logo concepts

Owner2026-10-07 delegates word choice and asks for metadata/logo image-generation skills. Load [launch-metadata](../launch-metadata/SKILL.md) for contextual name/ticker/description/logo prompts and [launch-logo-generation](../launch-logo-generation/SKILL.md) for the native Hermes image workflow and validated file handoff. Load them via `skill_view` using their skill names. Let Hermes make the creative choice; keep source metadata fixed for a vamp unless a variation is explicitly requested. The new `launch_assets` helper uses a tool-less Hermes request, validates strict metadata and local raster signatures/size, and returns only metadata/image arguments, with no financial submission or fallback words.

Readiness: a real tool-less Hermes response succeeded in12.9s; the existing naming daemon's8s timeout can expire first. Native `image_generate` is installed but all3profiles disable `image_gen`, and the selected backend readiness check returnedfalse. New skills do not themselves enable that backend or grant provider credentials. Preserve existing auth and lead-owned profile settings; no logout/re-auth/reinstall/gateway restart. Full native image generation and live extended-metadata/asset consumption still require the lead's integration. See readiness for verified scope.

## Feed and planning

Use J7's social JWT, never a wallet key, from `~/.config/kaiba/j7.session` only. Only the owner writes that file, over SSH (docs/runbooks/j7-feed.md); never ask for the token, obtain it or set `J7_SESSION_JWT` (the `j7_feed` listener does not run while it is set, and this bridge's reader would prefer it to the file). It is currently absent on the VPS. The new `kaiba.ingest.j7_launch_bridge.stream` supplies the same XPost interface as the existing TwitterAPI stream. The bridge requires Socket.IO, emits user_connected, rejects backlog/deletions/non-X events/unwatched authors, merges nonempty enrichment, waits for tweet images and preserves first-seen versus eligible-observed time. Only the configured launch-account subset is considered; do not auto-launch all 1,497 J7 accounts.

For a bounded read-only preview, from `~/kaiba`:

```sh
PYTHONPATH=~/kaiba/data/codex-tweet-launch-20261006/j7-deps .venv/bin/python -m kaiba.ingest.j7_launch_bridge --seconds 60 --output data/social/j7-launch-preview.jsonl
```

The preview forces shadow even if given a live config and creates no live database rows or transactions. The J7 iterator is wired into the selectable runner; selecting it requires the authorized social session and Socket.IO dependency path. J7 authentication itself remains unverified. The current funded Twitter filter connection is separately verified below. Do not buy a monthly account-monitoring plan from the owner's$20credits without authorization.

`kaiba.ingest.tweet_launch_feed` selects J7, a TwitterAPI filter rule, or the legacy monitor through `feed.backend`. The active runner now uses `twitterapi_rule`; J7/rule modes do not call monitor-subscription endpoints. The old source-matched review patch has been integrated, with short atomic reservations and freshness/status repairs; the latest lead admission change requires review as described above. Do not reapply the historical patch over the installed module. Pending exposure is reserved per chain; configured daily count/spend rules apply only when finite; the owner currently selects unlimited. Cooldown groups a source post so its explicitly configured chain routes can proceed; different posts from that author still observe the configured cooldown.

The TwitterAPI.io filter `kaiba-tweet-launch-shadow-20261006` is now active, despite its historical tag: the update was acknowledged and GET returned `is_effect:1`. It matches the existing six launch accounts every5s, uses the funded prepaid key and the single existing service socket. No monthly subscription, credit recharge or second socket was started.20actual rule posts were recorded and all skipped in the verification window. Control calls reserved600credits, shared helper allowance now1965credits; these are not the streaming bill or an account-wide cap. Stream matches debit provider credits independently. Preserve the owner's$20data mandate; do not purchase/top up another plan or silently widen data funding. Receipts: `data/codex-tweet-launch-20261007/{filter-activation,runtime}-receipt.json`.

## Five-percent initial allocation

Use `kaiba.execution.tweet_launch_policy.quote_five_percent` only for a verified fresh constant-product curve with fees explicitly charged on top. Its target is 5% of total supply, rounded up by less than one indivisible unit; native spend rounds up using integer rational arithmetic. Transfer/anti-sniper taxes and creation gas need separate venue-specific evidence. An assumed curve or a capped amount labelled 5% is not an exact allocation.

For Pons' measured fee deducted from input, use `quote_five_percent_fee_on_input` instead. The production Pons branch now uses this integer model: minimum gross0.089314194577352472ETH for100bpsprotocol fee with zero additional creator/transfer/anti-sniper tax. A fresh public sample replays3/3atomic-buy outputs exactly using quoteIn−fee−creatorTax. Actual tax settings and final wallet allocation still need receipts; do not apply the fee-on-top helper to Pons or claim an exact final fill from either model.

`require_cap` refuses a five-percent quote that exceeds the existing spend limit. Skip the whole launch rather than silently buying less. Verify final minted total supply and actual dev-wallet receipt with `allocation_receipt`; a short/extra fill is a discrepancy. Never automatically top up after an ambiguous/partial creation. Funds, network costs, aggregate exposure and daily loss reservations must pass admission before creation; the current reduced lead path does not yet enforce all of these.

Ordinary trade caps remain separate from the explicitly authorized tweet-launch ceilings. Current launch models estimate5% buys at about1.485SOL,0.2933BNB and0.0893ETH, within the approved launch caps. Skip rather than undersize if a fresh exact quote exceeds its cap. Retain submitting/submitted/confirmed/ambiguous reservation evidence even when daily allowances are unlimited. Approval is specific to tweet launches; it does not widen ordinary trade sizing, daily loss stops, free balance, shared exposure limits or withdrawals. The newer lead admission currently bypasses ordinary sizing but omits some shared controls; report and resolve those integration gaps without treating this authorization as an exemption.

Pons native initial-buy exemption is measured, not wholly unknown: the lead's chain study and a2026-10-07public sample agree that atomic launch buys can pay only the100bpsprotocol fee, including buys through a router. The fresh sample independently reads creation-block supply/reserves/fee and separates creator tax from anti-sniper tax. It does not identify the router as GMGN or verify the bound agent-wallet role, exact5%net receipt or holder rewards. See readiness for the proof scope; do not disable all tax checks on this basis.

## Vamp a tweet launch with reported volume

Owner 2026-10-07: "Also the agent can vamp launches out of tweets / Vamp launches with volume / Just use same everything." A vamp reuses the source token's exact name, ticker and logo, then creates a new token with the existing configured route, agent wallet,5%initial buy, holder-fee settings and watchdog exits. Existing per-launch limits and shared brakes apply to combined normal+vamp activity; daily allowances are unlimited; no separate allowance, volume-generation loop or second exit manager.

Use installed `kaiba.execution.tweet_vamp.from_gmgn` on an authenticated, fresh existing `gmgn_cli.token_info` result. Documented fields are `address`, `name`, `symbol`, `logo`, `creation_timestamp`, `price.volume_5m`, `price.swaps_5m`; preserve the actual Receipt. Require positive finite volume and swaps, fresh original watched tweet and receipt, source creation after that tweet, and matching contract or both deterministic identity fields. Name matching infers context; it does not prove affiliation or organic volume. Exclude our own source tokens. Any positive five-minute volume is the current default; the optional owner threshold question remains open. Do not invent a high-volume dollar threshold or advertise a profitable edge.

`plan_vamp(post,candidate,cfg,now_ms=...)` retains configured limits and publication-age rules, copies exact metadata, and refuses unsupported values. It shares `tl:<tweet_id>:<chain>` with normal launches. `record_vamp` can claim a new intent or an untouched skip only; existing normal plans and failed/ambiguous/submitted launches win. Revalidate volume and original tweet freshness at the actual launch boundary. Use the existing launcher reservation/reconciliation/send path only once full shared admission and holder routing are integrated by the lead. No per-tweet approval is required within the standing authorized mandate; unresolved technical preconditions are not new budget questions.

Offline fixture preview: `.venv/bin/python -m kaiba.execution.tweet_vamp --input data/social/vamp-preview-input.json`. It forcesSHADOW, calls no providers and writes no live ledger. User-provided JSON receipts are fixtures, not trusted provider authentication.33vamp tests plus68budget/policy tests pass locally2.27s and on isolated current VPS source12.44s. Additive planner installed, no daemon hook/reload/live financial transaction. Live automatic vamp execution remains an integration gap alongside the shared admission/holder blockers, not a completed feature. See readiness and `docs/research/codex-tweet-vamp-20261007.md`.

## Staged sales

Keep the existing watchdog as the single exit owner. The latest VPS tweet-launch override uses 1.3x/1.6x/2x/3x entry, selling 20%/25%/33%/50% of the then-remaining inventory, plus a full exit after 60 seconds without volume. This replaces the earlier global-ladder snapshot for this lane. This is the lead's configured schedule, not a verified fill or profitability claim. Full emergency exits remain protective actions; normal take-profit sales are incremental.

For each rung, freeze the admitted inventory quantity and derive its target once. `remaining_increment` subtracts confirmed fills from that target and returns zero while an order is unresolved. A partial fill must not repeatedly sell the same percentage of a shrinking balance. Mark completion only from reconciled quantities. Do not independently attach GMGN cooking auto-sells while the watchdog also controls the bag: that creates competing exit owners.

## Holder fees

Network gas, platform charges and protocol deductions cannot all be redirected. The owner's requirement applies to 100% of distributable creator tax/fees. Do not substitute a creator-wallet fee split, cashback to traders or burn for payments to holders.

The pure `holder_fee_args` helper prepares BNB Flap or FourMeme configuration with all dividend allocation and zero creator/burn/LP allocation. The actual tax rate still needs the owner's choice. Values used in tests are fixtures; no rate is silently approved. Eligibility thresholds and excluded system/pool addresses must be disclosed. A configured dividend contract can accrue rewards before dispatch; do not promise an immediate transfer after every trade. Verify deployed tax processor and dividend contract plus a real accrual/claim receipt before marking the requirement satisfied.

Solana Pump and Robinhood Pons holder distribution is not verified through the current GMGN route. The helper refuses those combinations, including Robinhood Flap which is supported by J7 but absent from this GMGN cooking chain/DEX matrix. Do not fall back to a plain token or route holder fees to the agent wallet. An audited alternate venue/distributor and signer-policy approval are separate work, not a field name to guess.

The separate pure `j7_holder_fields` helper now encodes J7's documented fields: native Pons `creator_tax_bps`+`holder_dividends:true` without buyback/creator recipient; native Flap100%dividend allocation with explicit chain/taxes; SOLPump OTC rewards with an operator-selected registry-admitted reward mint/symbol. It emits no credentials and sends nothing. It is not a GMGN flag mapping or an automatic provider fallback. J7 currently shows0importedSOL/EVMwallets; its separate encrypted signing credential is absent, and GMGN wallet compatibility is unproved. Do not export/import a wallet signing credential or select a stock reward to bypass those missing inputs. Reward eligibility, actual net5%allocation, protection and payout still need receipts. See readiness for the176test support release.

No deployment, purchase, sale, fee claim or transfer is performed by the helpers in this skill. Enabling the actual launcher must follow the owner's numeric limits and the existing lead-owned execution/risk integration. Once a bounded automated mandate is configured and verified, preserve that authorization rather than asking again for every tweet. Every creation needs a persistent unique source-post/chain intent, an atomic spend reservation and an unambiguous reconciliation/protection path.

## What not to do

- Reintroduce the historical daily allowances, or read the unlimited-allowance directive as arming a paper-only source. Per-launch ceilings, loss stops, ordinary sizing, exposure, holder routing, protection and withdrawal controls keep their current settings.
- Auto-launch all 1,497 J7 accounts; only the configured launch-account subset is considered.
- Buy a monthly plan, top up credits, start a second socket or silently widen data funding beyond the owner's $20 data mandate.
- Reapply the historical review patch over the installed module.
- Label an assumed curve or a capped amount as 5%, apply the fee-on-top helper to Pons, or claim an exact final fill from either model.
- Buy less than 5% when a fresh exact quote exceeds its cap (skip instead), or automatically top up after an ambiguous/partial creation.
- Attach GMGN cooking auto-sells while the watchdog owns the exit.
- Substitute a creator-wallet fee split, trader cashback or burn for holder payments, fall back to a plain token, or route holder fees to the agent wallet.
- Export/import a wallet signing credential or select a stock reward to bypass missing J7 inputs.
- Invent a high-volume dollar threshold or advertise a profitable edge for vamps.
- Disable all tax checks on the strength of the Pons initial-buy measurement.
- Log out, re-authenticate, reinstall or restart the Hermes gateway, or change lead-owned profile settings.
