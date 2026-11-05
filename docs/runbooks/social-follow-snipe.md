# Following graph and official contract posts

Latest owner update 2026-10-07: daily native spend and launch-count allowances are unlimited on SOL, BNB and Robinhood, including vamps and launch-snipe daily counts. This supersedes earlier finite daily allowance statements below. Per-launch caps, loss stops, sizing, exposure and protection are unchanged. Source-matched VPS tests: 311 passed in 17.50s. Official-post arming remains separately pending; the shared Twitter research budget is unchanged.


Codex owns this additive source under SOCIAL-FOLLOW-SNIPE-20261007. The receiver and maintenance timer are deployed on the VPS. Real-money arming remains pending the explicit exception to the lead-owned engine paper boundary. Do not change risk, signer trust, Hermes auth or the regular sniper to activate this source.

## Verified runtime, 2026-10-07

Latest bounded verification: 506 active source/target edges, 477 distinct discovered accounts, six accounts with website-to-X identity proof. The watch includes avici, jbrukh, ellisliu9, 0xmfox, hosseeb and mignano. Website identity is not proof that someone issues a token: the announcement path also requires the token metadata's issuer profile to match the exact announcing account. No live official-token buy or issuer-provider receipt has yet been observed. Initial post backlogs were received and refused as stale.

The receiver PID is 1231811; the maintenance timer is active. Shared tweet consumer PID 1199136 and protection PID 888510 remain unchanged after the final source update. `live_official_posts` is false; all existing launch-snipe live venue lists are empty, and BSC also has the engine's hard paper boundary. The reviewable narrow exception is [official-post-arming-candidate.patch](../research/official-post-arming-candidate.patch). It has not been applied. Fixture tests prove the candidate's three-chain gate behavior; they do not prove provider fills, profit, or live protection of a new entry.

## Commands

```sh
cd ~/kaiba
.venv/bin/python -m kaiba.hunters.social_follow status
.venv/bin/python -m kaiba.hunters.social_follow edges --limit 20
.venv/bin/python -m kaiba.hunters.social_follow projects --limit 20
systemctl --user is-active kaiba-social-follow.service kaiba-social-follow-maintain.timer
journalctl --user -u kaiba-social-follow.service -n 30 --no-pager
```

State is `data/social-follow.db`; the feed reads `data/social-project-watch.json`. Configuration is `config/social_follow.yaml`. The database stores immutable account IDs, exact handles, source/target edges, baseline flags, identity evidence, and announcement acceptance/refusal receipts. Never infer an unfollow from an account disappearing from a partial page.

## Data and latency

The existing shared feed accepts J7 following/unfollowing events, follow-scan historical results, and project post events. A historical follow-scan CA is research, never a newly published announcement. J7's supported VPS session is still absent. The paid TwitterAPI fallback is working; it reads the newest 20 follows of one source per maintenance run. There are 91 source accounts. Maintenance waits 120 seconds after each run, so an ideal full rotation takes at least three hours, plus provider and verification time. This is observation-time tracking, not an exact follow-time feed or complete historical graph.

At most 20 identity-verified candidate accounts receive bounded owned rules on the existing TwitterAPI socket. Rules use its five-second interval. There is no new socket, unlimited scan or extra paid subscription. The background receiver prevents website/provider work from blocking the upstream tweet stream. Fresh announcements have a 30-second publication age limit, rechecked after provider/dossier work and at the prepared engine gate. Existing engine cadence and provider checks add latency; zero-delay trading is not established.

API requests reserve their maximum expected cost before dispatch against the existing shared $20 lifetime/$0.50 UTC-daily ledger. Latest ledger: $0.22395 daily and $0.31320 lifetime reserved, including other research. Failures consume reserved cost; exhaustion refuses requests until available budget, without resetting the ledger. Rule-control charges share that budget. Full-day uninterrupted polling is not guaranteed at these limits.

Provider contracts: [TwitterAPI followings](https://docs.twitterapi.io/api-reference/endpoint/get_user_followings), [J7 feed events](https://docs.j7tracker.io/docs/feed-events), [J7 account types](https://docs.j7tracker.io/docs/data-types). Actual followings responses used a legacy `url` with t.co links; the deployed normalizer handles that verified shape and excludes private/contact fields.

## Announcement-to-engine path

1. Require a currently followed, website-verified account, exact immutable author ID, and a fresh own post. A handle/site change revokes verification.
2. Extract one exact CA from the author's own text and require explicit contract/mint announcement wording. Reject retweets, quoted CAs, ambiguous addresses, missing IDs, stale posts and fake/testnet/old-contract warnings.
3. Resolve exactly one indexed chain/token row. Supported handoff venues are SOL Pump.fun/LaunchLab, BNB Flap, and Robinhood Pons. The prepared live proof currently allows Pump.fun, Flap and Pons only. Four.meme creator launches belong to the separate launcher; Four.meme official-post sniping is not implemented here.
4. Reuse launch-snipe deployer/venue checks, Robinhood tax/early-book checks, BNB portal/native-price checks, dossier and ordinary RiskGate sizing. Token metadata's issuer profile must match the announcing account; narrative tweet links and trader promotions cannot substitute. Missing evidence refuses the handoff.
5. Persist the exact resolved chain/venue/issuer check. Record one stable `sig_official_...` signal per chain/token, separately from an earlier paper launch signal. Repeat official posts cannot create another source signal.
6. Preserve existing numeric daily caps (observed SOL 5, BNB 30, Robinhood 5), counting distinct live/canary launch-snipe entry reservations for this source. Paper entries do not consume its real-money quota. No creator 5%-of-supply buy or creator-launch budget exception is applied.

The source never calls swap/create directly. Arming the prepared hook requires the owner-enabled flag plus an actual verified follow/announcement/issuer receipt. The normal engine, sizing, venue viability, execution and protection remain responsible for money. Disabling the source flag is not a cancellation of orders already planned by the engine.

## Evidence and limits

Initial source-matched VPS integration suite: 198 passed in 40.64s. Final changed-path suite: 65 passed locally in 4.68s and on the VPS in 3.34s; Ruff passed. Tests include stale/forged identity, chain ambiguity, promotion versus issuer, daily-cap positive controls, preservation of earlier paper signals, repeat dedupe and the unapplied gate candidate. A previous broad stage timed out before installation; it is not counted as passing evidence.

The follow store has a 256 MiB capacity bound. Network reads are bounded, public HTTPS uses DNS validation and pinned public IP/TLS hostname verification, the receiver queue/workers are bounded, and own rules have a six-hour reconciliation cache. Identity checks refresh after 24 hours. Projects without website backlink proof remain unarmed; Twitter-only projects are a coverage limitation.

For live financial DB reads: check WAL before each chunk; over 1.5 GB wait and recheck. Use autocommit, materialize and close short reads, `nice -n 19 ionice -c3 timeout`, and no read transaction over 60 seconds. No live DB copy to `/tmp`. No synthetic orders, token creation, transfers or fee payouts were performed by this task. Expected value is unproven; evaluate all observed candidates and refusals with executable prices, taxes, fees, fills and slippage before claiming this source improves profit.
