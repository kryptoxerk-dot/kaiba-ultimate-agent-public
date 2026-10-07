# Shared social hunts

The provider stream dispatches locally before launch naming, logo generation or transaction submission. Research-only accounts never enter launch workers. In current custom-rule mode, update provider rules to cover the union of existing launch/alpha and research handles. In J7 mode, the existing authorized session can receive all 1,497 roster handles through the same source; selecting a larger local roster alone does not add upstream coverage.

```sh
cd ~/kaiba
.venv/bin/python -m kaiba.hunters.social_hunt accounts
.venv/bin/python -m kaiba.hunters.social_hunt status
.venv/bin/python -m kaiba.hunters.social_hunt list --lane nft --limit 20
.venv/bin/python -m kaiba.hunters.social_hunt list --lane airdrop --limit 20
.venv/bin/python -m kaiba.hunters.social_hunt next --limit 8
.venv/bin/python -m kaiba.hunters.social_hunt review TWEET_ID --lane nft --status verified --note 'Evidence and assessment reference'
```

`next` claims up to eight unreviewed candidates, prioritizing NFT then airdrop before token research, with a five-minute lease. A second worker cannot claim the same leased signal. Review statuses are verified, watch, rejected or unavailable. A lease expires if a worker fails. A verified status is research evidence, not trade authorization.

The local receiver runs continuously as `kaiba-social-hunt.service`; user timer `kaiba-social-hunt-maintain.timer` performs bounded retention/status every 15 minutes. The owner-requested Hermes monitor job uses `deploy/social-hunt-monitor.py` once a minute. Stable pending IDs suppress model runs on unchanged state. The job claims a bounded batch, invokes the existing NFT/airdrop assessment tools, records evidence and review status, and keeps unchanged/non-actionable runs quiet. It never changes wallet trust or capital limits.

Custom-rule mode currently polls at five seconds; J7's vendor latency is not a measured deployment result. The CLI reports publication-to-receipt and receipt-to-inbox latency separately. No HTTP or image/model call is made on local dispatch. Dropped/unavailable delivery is logged explicitly. Protect financial exits and the original single-provider connection; only reload the stream consumer for an exact tested code change when no launch submit is in flight, honoring its 90-second reconnect floor.

The isolated database retains seven days, stops recording above 256 MiB or below 2 GiB free disk, uses a short closed connection per query, and never scans the live financial database. It is an inbox; existing `kaiba.hunters.nft` and `airdrops` retain assessment/planning/execution responsibility.
