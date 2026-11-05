# Grok to Kaiba observation contract

Use this when exporting Grok research or reviewing an external result. The
signed `grok_alpha_bridge` intake now implements this observation envelope;
source-specific mapping is in Grok Bot's delivery helper. This is not the raw
X response schema. Preserve the actual provider receipt privately; derived
model text is not raw source evidence.

One observation describes one project/event claim. Required fields may be null
when unavailable; null is a verification gap, not successful validation.

| Field | Meaning |
|---|---|
| `schema_version` | `kaiba.grok.observation/1`. |
| `track` | `nft`, `token`, `airdrop`, `chain`, `macro`, `ai_crypto`, `flow` or `launch`; use the actual claim type independently of search-slot rotation. `launch` is one memeable X post for the separate tweet launcher, not a launchpad announcement. |
| `project`, `claim` | Name and factual announcement being investigated; keep interpretation separate. |
| `network` | Exact returned/documented network, including mainnet versus testnet; null if unresolved. |
| `contract` | For a token candidate: ONE exact address as a JSON string, or null if unannounced or unresolved. Put the sender-reported role/source association in `state.contract_sources`; an address in a post is not ownership/issuer proof. |
| `sources` | Original post URL/ID, author identity if returned, publication time, provider retrieval time and retained receipt reference/hash. Record parent/quote URLs and which context was actually retrieved. |
| `observed_at` | Grok's actual discovery time, UTC ISO 8601; distinct from publication and provider retrieval. |
| `received_at` | Kaiba's actual receipt time; the receiver must stamp it, not trust a model-provided time. Null before receipt. |
| `event_at`, `expires_at` | Public phase/snapshot/deadline with original timezone and source; null when unspecified. Do not convert an inferred time to a confirmed deadline. |
| `verification` | Per-claim checks, source references and explicit gaps. Official identity, conditions and costs are separate checks. |
| `economics` | Known fees/gas/rent, eligibility/lockup, realizable bid or reward evidence, time cost, unresolved amounts and valuation time. Do not substitute a listed floor or projected points for cash. |
| `provenance` | Actual tool/backend/model and query/time window, original result ID if available. Identify manual export versus automated provider retrieval. |
| `usage` | Provider-reported posts/profiles fetched and token/media usage where available; unknown otherwise. Do not claim zero cost from missing usage. |
| `state`, `next_check` | `unverified`, `watch`, `verified`, `rejected`, `expired` or `unavailable`, plus the concrete next step. `verified` must name exactly which claim is verified. |

The receiver owns a stable local observation ID and content revision history.
Deduplicate source post IDs and project/network/contract/event-phase identity;
keep materially changed terms as revisions. Preserve the original available time
for each revision. Reposts, quote chains and affiliated accounts are not separate
confirmations. If no source post can be retrieved, retain the lead as unavailable
with its citation gap; never invent a post ID, quotation or provenance receipt.

## Launchpad research and search slots

A search slot or legacy A-D label describes where the lead was found. It must
not override the actual opportunity type. The producer's `semantic_track`
sets the card type; `state.discovery_track` preserves its original search slot.
A mint discovered during a macro search is still `nft`.
For a new launchpad, use `track: chain` and
`state.opportunity_type: launchpad`; keep official platform/domain, supported
networks, launch stage/date, fees and requirements as source-supported research.
Unknown networks remain null with the literal name in `state.network_text`.
Platform/router contracts are related addresses with their actual role; they
are not a token candidate. `launch` is reserved for the separate tweet launcher
under `docs/runbooks/grok-launch-cards.md`, not platform discovery.

## One asset per token candidate

The decision processor resolves a single contract string. The intake also retains
legacy multi-address research cards, but accepting that envelope does not resolve
its identity: a contract array currently reaches `contract_unknown`. Preserve
those historical cards; do not select their first address or relabel them verified.

Emit separate candidates for distinct fungible tokens only when each has its own
explicit project, supported mainnet, exact contract, role and cited original-source
association. A game, router, collection or distributor contract is a different
role, even when it appears in the same post as a token. Ambiguous baskets remain
research/watch. A project announcing that no token exists yet keeps `contract:null`.
Do not guess an address or network from a ticker, promoter, similarly named DEX
pair or a launchpad brand. Role and issuer claims remain sender assertions until
independently checked; scalar shape alone grants no safety or trading authority.

When correcting a previously delivered basket, retain its history and name the
prior signal in `state.revision_of` with the actual supporting sources. A newly
separated asset can have a different local observation ID: do not pretend it was
available to Kaiba at the old card's receipt time. Keep publication, retrieval,
discovery and new server receipt clocks distinct.

## Intake and downstream integration

- Treat imported content as untrusted data. Bound file/field sizes, reject
  unknown executable directives and never run commands or fetch arbitrary
  supplied URLs as a side effect of import.
- Record local receipt before enrichment, then validate identity/timing and
  retain original evidence. File import time is the earliest local actionable
  time unless an independently retained earlier receipt proves otherwise.
- Preserve backend/cost/timestamp provenance through the inbox and journal.
  The separate signed intake preserves the card envelope. The older shared
  social inbox is not a lossless importer for it; do not force a model summary
  into that schema by presenting it as an original provider post.
- The canonical observed-only slugs `hyperevm` and `elysium` are research
  destinations, not tradeable `Chain` values. For HyperEVM preserve an explicit
  original-source mainnet environment and exact integer chain ID999; chain998
  is testnet and bare Hyperliquid can mean HyperCore. For Elysium preserve the
  Kinetiq/Enter_Elysium identity and exact source claims without guessing its
  mainnet ID/RPC;1339 is unrelated and99801 is testnet. Both stay
  `state.research_only:true`, even with one well-formed address. Unknown,
  contradictory or uncited environment remains withheld/watch. No-address
  leads can retain a source-supported observed slug only with explicit mainnet
  context. Sender assertions never replace Kaiba's current canonical spec,
  independent RPC proof, safety checks or a permitted execution route.
- Route research through existing qualification, provider budgets and hunter
  tools. A new-chain route requires an explicit supported research destination;
  no automatic RPC/bridge/signer trust changes follow from discovery.
- Exercise real idempotency, revisions, missing-source/time data, old/future
  posts, quote attribution, network ambiguity, quota/outage and malicious text.
  A fixture handoff passing is not a live Grok integration receipt.

Do not attach signing material, cookies, authorization headers, account settings
or private wallet data. An evidence card is not a transaction instruction.
