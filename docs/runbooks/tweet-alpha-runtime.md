# Tweet launch and alpha runtime — 2026-10-07

Latest updates supersede the historical daily allowances and source hashes below: daily spend/count allowances are unlimited on all three chains. The launcher now has 53 reference authors, includes all 20 researched core accounts, and uses the correct Four.meme activity reader. See [current three-chain runbook](tweet-three-chain-live.md); SOL ten confirmed creates are observed, while BNB/Robinhood own-create receipts remain pending. Existing independent alpha dispatch remains enabled.

The existing tweet consumer remains financially armed for Solana pump, BNB Flap plus Four.meme, and Robinhood Pons. Owner caps are unchanged: 1.5 SOL / 0.5 BNB / 0.1 ETH per launch; 6 SOL / 1 BNB / 0.3 ETH per UTC day. The same chain budget covers both BNB venues. A daily-budget refusal is expected behavior; do not widen limits to generate more launches.

Alpha research now dispatches before launch naming, logo generation or GMGN submission. Two dedicated workers measure the union of 55 launch/alpha authors, at most 32 pending posts and three refs per post. All 91 purpose-scoped accounts continue through the existing social inbox. No second upstream socket is opened. Explicit alpha chain hints override launch hints. Quoted-post and reply-parent text can supply a contract. Ambiguous EVM chains require an explicit hint rather than a guessed BNB price. Queued posts beyond the age limit cannot create fake early-entry measurements. Provider requests retain RESEARCH priority. Alpha measurements and forward marks submit no purchases.

Inspect the bounded read-only inbox:

```sh
cd ~/kaiba
nice -n 19 ionice -c3 timeout 15 .venv/bin/python -m kaiba.execution.tweet_alpha_dispatch status --limit 20
nice -n 19 ionice -c3 timeout 15 .venv/bin/python -m kaiba.execution.tweet_alpha_dispatch status --chain robinhood
.venv/bin/python -m kaiba.hunters.social_hunt status
systemctl --user is-active kaiba-tweet-launch.service kaiba-protection.service kaiba-social-hunt.service
```

The CLI scans only the latest 500 rowids, caps output at 50, closes the connection, interrupts queries after three seconds and refuses live reads when the WAL exceeds 1.5 GB. Wait 60 seconds and recheck before another live query chunk. Do not run unbounded reports against the live database.

Evidence from the deployment: 154 tests passed locally in 8.26 seconds, and against the exact current VPS source in an isolated stage in 27.19 seconds. Only the tweet consumer was restarted, PID 1149828 → 1188386. Protection PID 888510 and risk/signer/launch configuration, lead creative/reference source remained unchanged. Installed launcher SHA256: `cf4a7370b6d99bb10cee442d8cb1ab64aade80b64668644aee1c230a812d19fc`.

Post-deployment natural posts reached the alpha worker with queue delays 69, 91 and 73 ms; no observed worker errors or queue saturation. Those posts named no resolvable refs, so the alpha table was still empty at this check. This is proof of dispatch, not a live provider-price receipt or profitable alpha. Existing NFT/airdrop/token inbox receipts are separately recorded. The dispatch change removes avoidable observation delay; it has not demonstrated higher expected value per trade and adds no trading filter or confidence sizing.

Solana has confirmed launch receipts and protected/closed position records. Today's reserved launch buy input was <amount> SOL; another <amount> SOL launch is refused by the 6 SOL daily cap. The bounded recent launch sample has no confirmed BNB or Robinhood launches; their route flags and fixture routing pass, but live three-chain execution is not established by those tests. No synthetic launch, trade, transfer or payout was sent during this work.

Remaining requested integration: J7 external session is absent; current prepaid TwitterAPI feed is receiving posts. AI image generation still lacks a verified working VPS backend. SOL cashback is not proof of holder dividends, Robinhood's plain GMGN Pons path has no configured holder routing, and BNB dividend arguments are not payout receipts. Position closure records alone do not prove complete fee/proceeds accounting or profit. These pre-existing financial/creative seams remain with the lead; this support change does not edit protected architecture or claim they are solved.
