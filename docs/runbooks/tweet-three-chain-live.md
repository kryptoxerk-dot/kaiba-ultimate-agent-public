# Three-chain tweet launches — 2026-10-07

The live automatic creator launcher watches 53 reference accounts on the existing paid TwitterAPI socket. All three chain routes are armed in the launcher and risk lane. Daily spend/count allowances are unlimited by the owner's latest instruction. Per-create caps remain 1.5 SOL, 0.5 BNB and 0.1 ETH; the creator buy targets 5% of total supply. BNB source posts can create on both Flap and Four.meme, with a separate per-create cap for each venue. Existing dedupe, source cooldown, receipt breakers, loss stops and position protection remain in force.

## What has been verified

| Chain | Creator routes | Approximate modelled buy | Current evidence |
|---|---|---|---|
| Solana | Pump.fun | <amount> SOL | Ten confirmed launch records and matching provider-created tokens; prior eight protected/closed records verified. Latest exact token: `268H9pPjnZCJTs84nWVnBPUQ5RYXBJ3FBD7wufXKpump`. |
| BNB | Flap + Four.meme | <amount> + <amount> BNB | Both planner routes live and sized at 5%; native GMGN balance <amount> BNB. No confirmed own launch in the bounded recent sample. |
| Robinhood | Pons V2 | <amount> ETH | Planner route live, sized at 5%; native GMGN balance <amount> ETH. No confirmed own launch in the bounded recent sample. |

These are observed runtime/fixture checks, not a claim of successful provider creates on all three chains. No fabricated tweet, synthetic create, forced purchase, transfer or payout was submitted to demonstrate activity. The SOL token-balance request addressed WSOL and returned zero; that is not native SOL balance evidence. A later finalized native RPC receipt returned 3,977,599,342 lamports (<amount> SOL), and the ninth exact token above was confirmed protected and closed. The latest bounded route count has ten confirmed SOL creates and no unresolved launch. Native SOL is checked with [getBalance](https://solana.com/docs/rpc/http/getbalance), which returns lamports for the account, not its wrapped-token holdings.

The older unresolved SOL AFFLECK request received HTTP 403. A later exact receipt showed it resolved to failed with the existing runtime's note that no token was created by the wallet within 300 seconds; another genuine SOL launch then confirmed. No stale post was replayed or resubmitted by Codex. The shared executor's rejection parser currently recognizes trade endpoints but not cooking endpoints; its foreign-owned classification is reported rather than changed. Preserve actual ambiguity when an accepted order or transaction may exist.

## Core launch references

The existing [source-backed research](../research/codex-kol-priority-research-20261007.md) identifies a 20-account core. It does not establish a profitable account ranking or justify larger buys.

- Broad catalysts: `elonmusk`, `realDonaldTrump` across the three chains; `cz_binance`, `heyibinance` on BNB; `vladtenev`, `MEADGod` on Robinhood; `a1lon9` on SOL.
- CT/asset discussion: Ansem `blknoiz06`, `cobie`, `MustStopMurad`, `CrashiusClay69`, and `theunipcs`. Their existing configured chains/reply rules are preserved; exact asset discussion does not imply every derivative launch inherits its volume.
- Ecosystem references: `pumpfun`, `bagsapp`, `flapdotsh`, `ponsdotfamily`, `binance`, `RobinhoodApp`, `solana`, `toly`.

The four missing creator references are now added: Pump.fun/Bags → SOL, Flap → BNB's two configured venues, Pons → Robinhood. They were already subscribed as alpha sources, so no new provider rules, second connection or subscription were needed. The runner reloads configuration for each delivered post; these additions required no restart. Other authors remain available for broader discovery; no new unmeasured filter or confidence-size increase was shipped.

## Activity/protection repair

The old BNB activity branch always read Flap, including for Four.meme tokens. It could not observe Four.meme curve trades. The new `kaiba.execution.launch_activity.bsc_fingerprint` reads the own live launch's exact venue, then reuses the existing Four.meme or Flap chain reader at EXIT priority. Four.meme fingerprints use virtual token reserves, funds and price; failed reads are unavailable evidence, not silence. Proven graduation preserves the existing graduation marker. Other venue/exit semantics are unchanged.

Verification: 181 tests passed locally in 11.50s and against the exact current VPS package in 17.11s. New tests exercise the actual launcher-to-reader branch and activity writer: correct venue, EXIT priority, no guessed/shadow venue, unchanged curve, failed read, real movement and graduation. An earlier 27-test configuration/boundary check also passed. Fixture results are not real BNB or Robinhood fill receipts.

The source-matched deployment changed only the BNB activity branch plus its additive helper/test. Consumer PID 1265452 → 1274987; protection 888510, engine 888520 and ops 888707 stayed active/unchanged. Risk, signer policy, creative source, executor and the 53-account config hashes were preserved. The lead's concurrent runner changes were retained. Installed runner SHA: `3676a31ac2e89d635596886370e74e0ebebf8cf2c38b5a9a11d6f7577a7216ee`.

## Operating checks and limits

```sh
cd ~/kaiba
systemctl --user is-active kaiba-tweet-launch.service kaiba-protection.service kaiba-ops.service
.venv/bin/python -c 'from kaiba.execution.tweet_launch import load_config; c=load_config(); print(len(c.accounts), c.live, c.daily_launch_cap)'
```

Do not run the launcher's historical full-table report on the live 22 GB database. Read latest rowid-bounded subsets or exact indexed token/launch IDs; check WAL before each query, use autocommit and materialize/close under a short progress deadline, `nice -n 19 ionice -c3 timeout`. WAL over 1.5 GB means wait 60 seconds and recheck. All audit chunks for this task used two-to-four millisecond reads with a three-second cutoff; a later fresh check refused itself when WAL exceeded the threshold.

All 20 core handles were confirmed in the current active owned rules, with five-second intervals. Actual delivery can be slower. The provider's [WebSocket guide](https://twitterapi.io/blog/using-websocket-for-real-time-twitter-data) requires one connection per key and at least 90 seconds before reconnecting; server 1011 errors are observed. Keep that required backoff rather than claiming sub-second recovery. The authenticated J7 VPS session is still absent; the existing paid source continues operating. The research/control calls retain the separate shared $20 lifetime/$0.50 daily data ledger.

Hermes is configured first for naming, but its 2.5-second timeout is observed to expire and invoke the deterministic fallback. Image calls have a three-second budget; observed Pollinations 402/timeouts fall back to a drawn badge. A badge is not AI-generated art. No native model-profile/auth changes were made by this task.

BNB dividend flags are configured but payouts remain unproven; SOL cashback is not equivalent evidence of holder dividends; the current plain Pons path has no holder-fee routing flag. Closed/protected flags and tiny recorded realized PnL do not establish complete net fee cashflow or profitable expectancy. The latest provider-created SOL token reports nonzero fees, so do not treat a near-zero ledger result as fee-complete profit.

The independent official-project contract-post sniping gate remains a separate pending source exception. This runbook concerns creator launches inspired by KOL posts, not purchases of project-posted contracts.
