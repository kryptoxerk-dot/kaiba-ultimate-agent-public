# Integration contract

Everything in `kaiba/` builds on the core in `kaiba/core/`. This file is the API surface
other modules may rely on. If you need something that is not here, add it to the core and
update this file in the same change.

Python 3.12, pydantic v2, stdlib `sqlite3`, `httpx` for HTTP. No ORM, no async framework
below the ingest layer.

## Money and evidence rules

1. **On-chain amounts are `int` base units** (lamports, wei, token atoms). USD is `Decimal`.
   Floats never touch money. SQLite stores big integers as `TEXT` to avoid 2^63 overflow.
2. **Missing data is `None` with `EvidenceBasis.UNAVAILABLE`.** Never 0, never a default
   that reads as safe. A failed lookup is `unknowns.append(field)`, not a clean result.
3. **Every provider-derived number carries a `Receipt`** (provider, endpoint, observed_at,
   basis). `Measure` bundles value + basis + receipt + freshness budget.
4. **EVM addresses are lowercased; Solana addresses are case-sensitive.** Always go through
   `normalize_address(addr, chain)`.

## `kaiba.core.schemas`

```python
Chain           # sol eth bsc base robinhood arc stable   (values match gmgn-cli --chain)
EVM_CHAINS, CHAIN_IDS, NATIVE_SYMBOL, NATIVE_DECIMALS
looks_evm(a), looks_solana(a), infer_chain(a), normalize_address(a, chain)
now_ms() -> int, digest(obj) -> str            # stable sha256 for dedupe keys
EvidenceBasis, Receipt, Measure                 # Measure.unknown(), .known, .stale
Wallet, WalletTag, HARD_QUARANTINE_TAGS, SOFT_PENALTY_TAGS
Grade, Archetype, ScoreFactor, WalletScore
EdgeType, HARD_EDGES, ClusterEdge, Entity
Token, TokenRisk, TokenDossier
Event, EventKind, Lane, LaneMode, Signal
Action, Decision, OrderState, Side, Order, Position, TradeOutcome, MISTAKE_TAGS
```

## `kaiba.core.db`

```python
get_conn()                    # thread-local connection, WAL
connect(path=None)            # fresh connection
session(path=None)            # context manager, closes after
tx(conn=None)                 # BEGIN IMMEDIATE / COMMIT / ROLLBACK
migrate(conn=None)            # apply kaiba/core/migrations/*.sql in order
ensure_db(path=None)          # connect + migrate
jdump(obj) / jload(s, default)
upsert(conn, table, row: dict, conflict: list[str], update: list[str] | None)
fetch_all(conn, sql, params) -> list[dict]
fetch_one(conn, sql, params) -> dict | None
```

Add new tables as `kaiba/core/migrations/NNN_name.sql`. Never edit an applied migration;
add a new one. Existing: `001_core.sql`, `002_intelligence.sql`, `003_execution.sql`.

## `kaiba.core.events`

```python
emit(kind, payload, *, chain=None, subject=None, level="info", trace_id=None,
     dedupe_key=None, conn=None) -> int | None     # None means duplicate
emit_once(kind, payload, **kw)                     # dedupe key derived from payload
tail(after_id=0, limit=200, kinds=None, subject=None) -> list[Event]   # oldest first
recent(limit=100, kinds=None) -> list[Event]                            # newest first
latest_id(), counts_by_kind(since_ms), follow(after_id, kinds, poll_s)
```

Emit an event for anything a human or the reflection job would want to see. The dashboard
SSE feed, the nightly reflection and the trace viewer all read this one table.

## `kaiba.core.limiter`

Wrap **every** outbound provider call:

```python
from kaiba.core.limiter import guarded, Priority, RateLimited

with guarded("gmgn", "token.info", Priority.RESEARCH):
    resp = httpx.get(...)
```

`reserve()` charges capacity and is never refunded; `release()` records the outcome and, on
`status="rate_limited"`, opens a cooldown for that endpoint family. `Priority`:
`EXIT < UNRESOLVED < POSITION < ENTRY < DISCOVERY < RESEARCH`. Endpoint strings are
`family.name` (`token.info`, `trade.quote`) — the family is what gets cooled down.
`wait_for(...)` blocks until a reservation succeeds. `status()` powers the dashboard meters.

## `kaiba.core.journal`

```python
append(kind, body, subject=None, refs=None)   # observation|lesson|experiment|change|outcome|correction
read(limit, kind=None, subject=None), verify() -> (ok, error), stats()
```

Hash-chained and append-only. Never UPDATE or DELETE a row.

## `kaiba.core.config`

```python
get_settings() -> Settings      # cached; secrets from .env then ~/.config/kaiba/.env
get_risk() -> RiskConfig        # NOT cached; config/risk.yaml is edited while running
save_risk(cfg, path=None)       # bounds block is preserved from disk, never widened
```

`RiskConfig.effective_mode(lane)` is the only correct way to ask whether a lane may act;
it applies the kill switch, the global mode and the operator ceiling. `clamp_size_pct()`
applies `bounds.max_size_pct_bankroll`.

## Provider modules

### Build on `kaiba/providers/_http.py`. Do not write a second one.

```python
from kaiba.providers._http import Fetched, get_json, post_json, redact_text

got = get_json("dexscreener", "price.pairs", url, params=..., ttl_s=10,
               priority=Priority.EXIT, wait_for_slot_s=5)
if got.ok:
    use(got.data, got.receipt)
```

It handles the limiter, the disk cache, retries, receipts and credential redaction. Four
adapters were written in parallel against it; the three rules below are the ones that were
each independently got wrong before it existed.

* **`wait_for_slot_s` is required for anything that makes more than one call per logical
  operation.** The default returns `UNAVAILABLE` on a limiter refusal instead of waiting,
  so a three-route scan silently ran only its first route, and a batched price lookup
  silently lost every chunk after the first. Set it to 0 only for a genuinely single-shot
  call where being late is worse than being absent.
* **A cached hit carries the original fetch time**, not the read time, so `Measure.stale`
  works. Do not rebuild the Receipt yourself; if you must, copy `observed_at_ms` across.
* **Never put an exception string anywhere without `redact_text`.** httpx renders the full
  request URL into `HTTPStatusError`, so an unredacted message is the API key in plain
  text in the events table. `_http` already redacts what it writes; you must redact what
  *you* write.

JSON numbers are parsed with `parse_float=Decimal`, so a price or a liquidity figure never
passes through binary floating point.

### Every provider module

Lives in `kaiba/providers/<name>.py`, exposes plain functions returning
`(payload, Receipt)` or a typed model, and must:

* go through `limiter.guarded`;
* cache to `data/cache/<provider>/` with a TTL when the data is not real-time;
* never raise on a provider being down — return `None`/empty plus a receipt whose basis is
  `UNAVAILABLE`, and `emit(EventKind.PROVIDER_ERROR, ...)`;
* never log secrets; read credentials only from `get_settings()`.

## Tests

`tests/test_<module>.py`, pytest, offline by default. Use the `tmp_db` fixture from
`tests/conftest.py` for anything touching the database. Anything that hits a real provider
is marked `@pytest.mark.live` and is skipped unless `KAIBA_LIVE_TESTS=1`. Record fixtures
under `tests/fixtures/<provider>/*.json` rather than mocking ad hoc.

## Style

* Module docstring explaining *why*, not just what. Comments only where the reason is not
  obvious from the code.
* Type hints everywhere; `from __future__ import annotations` at the top.
* `ruff check kaiba tests` clean at line length 110.
* No `print()` in library code — use `logging.getLogger(__name__)`.
