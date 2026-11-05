# Scheduled early discovery

Use the owner's existing Crypto X alpha scan routine and signed delivery. Keep
its 15-minute schedule, at most eight total X tool calls per run, included
allowance guard and zero paid extra usage. This ceiling includes searches,
thread/profile retrieval and retries; it is not a target or a dollar budget.
Record actual reported account allowance when available. An unknown allowance
is not proof of remaining capacity. Do not add a second collector or manual
sweep beside an already-running scan.

The native routine status may show enabled/next start without exposing whether
a run is active. A missing window/heartbeat file is not proof that it is idle:
on 3 November a scheduled run began nine minutes after its slot, overlapping
a replacement search. During verification, wait for its actual receipt instead
of forcing a replacement from an elapsed jitter estimate. A requested on-demand
scan needs coordination with the scheduled worker before either spends calls.

## Freshness and coverage

Keep a durable last-successful scan cutoff and a small overlap. Search Latest
results between that cutoff and this run's actual start time; use returned
publication timestamps to enforce the window when the tool only filters UTC
days. An initial run may use a bounded recent window. After an outage, record
the missing interval and bound catch-up instead of silently calling it covered.
Only advance the successful cutoff after the intended discovery searches
finish. Keep the signed outbox for retry if delivery fails.

Give most available reads to new announcements. Cover meme coin contracts and
catalysts (Solana priority, other networks retained), NFT phases, airdrop
criteria/claims, new chains and new launchpads over the rotation. Reserve room
for unfamiliar sources. Keep old-card follow-ups bounded; a repeated uncertain
mint should not monopolize discovery. Preserve original-author sources and
source-family dedupe. A scheduled public search is not a complete timeline or
a subsecond new-token feed.

An old post may be useful for a still-open future event. Label its actual age
and upcoming event time; do not call the announcement fresh. Material phase or
identity changes create a cited revision at the actual new observation/receipt
time. No material change means no duplicate card; a quiet scan still sends a
heartbeat. Access or quota failure gets `unavailable` or `usage_limited`.

## Opportunity type and routing

The search slot is provenance, not the card's type. Preserve it as
`state.discovery_track` in the current producer. NFT, token and airdrop findings retain their
semantic tracks even when a different search found them. Unknown classification
stays an explicit research gap; never guess it from the current clock.

New launchpads use the existing `chain` research track and
`state.opportunity_type: launchpad`. Preserve the platform name, official domain
and source, explicitly supported networks, prelaunch/testnet/mainnet status,
launch time, entry requirements, fees and missing checks. Keep unrecognized
network names in `state.network_text`, with canonical `network: null`. No
discovered URL becomes a trusted RPC or signer destination. Platform/router/
collection addresses are related addresses with their actual role, never a
fungible token by default. Separate a genuine announced token into its own
source-supported token card. `launch` cards have a different consumer and
contract in Kaiba's `docs/runbooks/grok-launch-cards.md`.

For NFTs distinguish free, allowlist-only and paid phases, eligibility,
contract/ABI/recipe, acquisition price and gas/platform/resale fees. For airdrops
preserve official terms, deadlines, lockup and time costs; points are not cash.
For tokens preserve one actual mainnet asset, original-source association and
unresolved identity without inventing a contract. A useful prelaunch watch lead
does not require an executable route yet.

## Verify the change

Read back the saved routine; an acknowledgement alone does not prove it was
updated. Retain a key-free producer/schema snapshot before changing its mapping
and exercise the real Kaiba receiver offline. Then check an actual scheduled
heartbeat and accepted cards in the isolated intake, followed by the existing
processor's recorded route. Preserve all previous evidence. Report publication
to observation and receipt delays separately. Count verified actionable leads
and eventual net outcomes before claiming an earlier or profitable edge.
