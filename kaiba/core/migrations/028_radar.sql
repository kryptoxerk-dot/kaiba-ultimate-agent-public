-- Discovery radar: the tables that make "new" a fact instead of an opinion.
--
-- Why this exists. Every venue this system trades was added BY HAND. pump.fun was
-- hardcoded. Robinhood Chain and Pons cost a 24-hour research agent. StonkFun cost a
-- research agent plus a build agent and was found ~7 weeks after it started, while it was
-- already 23.9% of the Solana launchpad fee pool. Nothing in the 93-table schema as of
-- 2026-09-21 would have noticed any of them: `listing_events` holds CEX listings,
-- `alpha_signals` holds nine token/domain/repo-keyed sources, and not one of them watches
-- a launchpad, a DEX factory or a chain list. There was no venue registry anywhere.
--
-- The hard part is not finding candidates. It is remembering which ones we have already
-- said out loud. A detector that re-reports the same venue on every sweep is a detector
-- the operator stops reading, and the early-alpha module's own docstring already records
-- that lesson ("a diff source needs a baseline first"). So the memory is the schema.
--
-- Three design decisions are load-bearing and each one is a measurement, not a taste.
--
-- 1. `chain_slug` IS FREE TEXT AND MUST STAY FREE TEXT.
--
--    `kaiba.core.schemas.Chain` is a closed StrEnum whose values are gmgn-cli's `--chain`
--    argument (sol eth bsc base robinhood arc stable). `AlphaSignal.chain` is typed
--    `Chain | None`, so pydantic rejects any slug that is not already a tradeable lane.
--    That is *correct* for the trading contract and it is exactly the mechanism that made
--    every venue so far a hand integration: you could not record a discovery without
--    first editing the trading enum, so nobody recorded one. Discovery and tradeability
--    are different questions. This column answers the first without touching the second;
--    `radar_registry.native_lane` records whether a lane happens to exist.
--
-- 2. EVERY MONEY COLUMN IS `TEXT`, HOLDING A `Decimal` STRING, AND NULL MEANS UNKNOWN.
--
--    docs/CONTRACT.md rule 2. A zero here is not a rounding error, it is a category
--    error: StonkFun quotes only 6.9% of its pools in wrapped SOL and the tail runs to
--    501 distinct quote mints, so a venue whose quote asset we cannot price MUST read as
--    None/UNAVAILABLE. A 0 there reads as "too small to care", which is precisely how the
--    next StonkFun gets missed. `last_basis` carries the EvidenceBasis for every value.
--
-- 3. THE CONFIRMATION LADDER IS IN COLUMNS, NOT IN PYTHON STATE.
--
--    `confirm_days` / `confirm_last_day` count CONSECUTIVE UTC DAYS above the floor, so a
--    layer polled every two hours cannot satisfy a three-day rule twelve times faster
--    than a layer polled daily. `tier` and `reported_tier` are what make the report fire
--    once: a candidate is announced when its tier exceeds the tier it was last announced
--    at, so a venue oscillating around the floor is silent forever while a venue going
--    from $25k/day to $2.5M/day gets to raise its hand again.

-- =====================================================================================
-- radar_registry — one row per candidate the radar has ever seen. The memory.
-- =====================================================================================
CREATE TABLE IF NOT EXISTS radar_registry (
  radar_key        TEXT PRIMARY KEY,          -- digest(kind, identity); stable across runs
  kind             TEXT NOT NULL,             -- venue | chain | platform_config
  layer            TEXT NOT NULL,             -- which poller owns it
  identity         TEXT NOT NULL,             -- DefiLlama slug, chain slug, or config pubkey
  display_name     TEXT,
  -- Free text on purpose. See note 1 above. NEVER constrain this to Chain.
  chain_slug       TEXT,
  -- 1 when a gmgn-cli lane exists for chain_slug, i.e. when the slug maps into Chain.
  -- Recorded, never required: a chain we cannot trade is still a chain worth watching.
  native_lane      INTEGER NOT NULL DEFAULT 0,

  first_seen_ms    INTEGER NOT NULL,          -- our clock; the only first-seen we can trust
  last_seen_ms     INTEGER NOT NULL,
  observations     INTEGER NOT NULL DEFAULT 0,
  -- 1 = this row was written by the layer's FIRST sweep. Baseline rows are never
  -- reported, however large. Without this the first run of the venue layer announces 48
  -- Solana launchpads and 195 chains as discoveries, all of them years old.
  baseline         INTEGER NOT NULL DEFAULT 0,

  -- evidence. TEXT Decimal, never REAL, never 0-for-missing.
  last_value       TEXT,
  last_unit        TEXT,                      -- usd_per_day | usd_7d | usd_per_day_proxy
  last_basis       TEXT NOT NULL DEFAULT 'unavailable',
  last_floor       TEXT,
  best_value       TEXT,
  best_seen_ms     INTEGER,

  confirm_days     INTEGER NOT NULL DEFAULT 0,
  confirm_last_day TEXT,                      -- YYYY-MM-DD, UTC
  tier             INTEGER NOT NULL DEFAULT 0,-- 0 below floor, 1 floor, 2 10x, 3 100x
  reported_tier    INTEGER NOT NULL DEFAULT 0,
  reported_ms      INTEGER,

  tractability     TEXT,                      -- full | partial | opaque | unknown
  meta_json        TEXT NOT NULL DEFAULT '{}',

  UNIQUE (kind, identity)
);
CREATE INDEX IF NOT EXISTS idx_radar_registry_layer ON radar_registry(layer, last_seen_ms DESC);
CREATE INDEX IF NOT EXISTS idx_radar_registry_tier  ON radar_registry(tier DESC, last_seen_ms DESC);
CREATE INDEX IF NOT EXISTS idx_radar_registry_chain ON radar_registry(chain_slug);

-- =====================================================================================
-- radar_finds — the announcement log. Append-only by convention: one row per report.
-- =====================================================================================
--
-- Separate from the registry because the registry is current state and this is history.
-- "When did we first say this out loud, and what did we know at the time" has to survive
-- the candidate's numbers moving, otherwise the only way to audit a miss is to trust the
-- current row, which is the thing that changed.
CREATE TABLE IF NOT EXISTS radar_finds (
  find_id       INTEGER PRIMARY KEY AUTOINCREMENT,
  radar_key     TEXT    NOT NULL,
  reported_ms   INTEGER NOT NULL,
  kind          TEXT    NOT NULL,
  layer         TEXT    NOT NULL,
  identity      TEXT    NOT NULL,
  display_name  TEXT,
  chain_slug    TEXT,
  tier          INTEGER NOT NULL,
  value         TEXT,                          -- NULL = we could not price it. Not 0.
  unit          TEXT,
  basis         TEXT    NOT NULL,
  floor         TEXT    NOT NULL,
  headroom      TEXT,                          -- value / floor, dimensionless
  rank_score    TEXT,                          -- headroom x tractability weight
  tractability  TEXT    NOT NULL,
  -- 0 whenever we cannot read the venue, WHATEVER the size. A venue we cannot read is
  -- not an opportunity for us; the report says so instead of pretending otherwise.
  actionable    INTEGER NOT NULL,
  verdict       TEXT    NOT NULL,              -- one sentence a human can act on
  payload_json  TEXT    NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_radar_finds_when ON radar_finds(reported_ms DESC);
CREATE INDEX IF NOT EXISTS idx_radar_finds_key  ON radar_finds(radar_key, reported_ms DESC);

-- =====================================================================================
-- radar_layer_health — a quiet radar and a quiet market look identical.
-- =====================================================================================
--
-- Same argument as `alpha_source_health`, and the same failure it prevents: the radar
-- going silent because DefiLlama paywalled a route (it did exactly that to /raises and
-- /emissions between two research passes) is indistinguishable, from the outside, from a
-- month with no new venues. `requests_last` is here because rule 5 of this task is "cheap
-- and polite" and an unmeasured request budget is an unkept promise.
CREATE TABLE IF NOT EXISTS radar_layer_health (
  layer           TEXT PRIMARY KEY,
  kind            TEXT NOT NULL,
  interval_s      INTEGER NOT NULL DEFAULT 0,
  last_poll_ms    INTEGER,
  last_ok_ms      INTEGER,
  last_count      INTEGER NOT NULL DEFAULT 0,   -- candidates the layer saw
  last_new        INTEGER NOT NULL DEFAULT 0,   -- of those, never seen before
  last_reported   INTEGER NOT NULL DEFAULT 0,   -- of those, announced
  total_reported  INTEGER NOT NULL DEFAULT 0,
  requests_last   INTEGER NOT NULL DEFAULT 0,   -- provider calls the last poll cost
  requests_total  INTEGER NOT NULL DEFAULT 0,
  fail_streak     INTEGER NOT NULL DEFAULT 0,
  baseline_ms     INTEGER,                      -- when this layer's baseline was set
  last_error      TEXT
);

-- =====================================================================================
-- radar_cursor — per-layer high-water marks, so an incremental poll stays incremental.
-- =====================================================================================
--
-- The LaunchLab cross-platform feed is newest-first and `sort=old|oldest|asc` all return
-- HTTP 500 (measured 2026-09-21), so the only way to stop paging is to recognise tape we
-- already have. Keeping the mark in the database rather than in the process means a
-- restart does not re-walk 7,825 pools/day of someone else's infrastructure.
CREATE TABLE IF NOT EXISTS radar_cursor (
  layer      TEXT PRIMARY KEY,
  mark_ms    INTEGER,                           -- newest item timestamp we have processed
  updated_ms INTEGER NOT NULL
);
