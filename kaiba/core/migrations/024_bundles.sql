-- Launch bundle and sniper measurement, computed from our own tape.
--
-- Why this table exists.
--
--   `bundler_pct` and `sniper_pct` are two of the eight evidence-ranked variables in
--   docs/EDGE-AND-VARIABLES.md §1 and they were unavailable on 132 of 132 dossiers we
--   have ever built, because the only source wired for them is GMGN and the account is
--   on the free tier. Bundle-adjusted concentration separates at 24pp where raw top-10
--   separates at 6pp (§1 #5), so this is the fifth-strongest variable we hold and it had
--   never once been computed. It does not need a vendor: a bundle is a structural fact
--   about the block, and `swaps.slot` / `swaps.block_index` already record the structure.
--
-- Four things this schema is shaped to make impossible.
--
--   1. **A missing measurement cannot be read as zero.** `coverage` is NOT NULL and
--      CHECK-constrained, and `bundled_pct` is nullable. A row with
--      `coverage='unavailable'` carries NULL percentages and a `reason`. That distinction
--      is the whole point: `curve-velocity` gates on `max_bundler_pct: 20` and fails
--      closed on an unknown, so a false 0% would silently convert a fail-closed gate into
--      a fail-open one and fire the lane on exactly the launches it exists to avoid.
--      There is deliberately no DEFAULT 0 anywhere in this file.
--
--   2. **The denominator is recorded, not assumed.** `supply_basis` says which supply the
--      percentage divides by. Curve reserves at launch are 79.31% of total supply on a
--      standard pump.fun mint, so the same bundle is 1.26x larger as a fraction of the
--      curve than of the supply. MELT (arXiv:2602.13480), the source of the 24pp figure,
--      measures share of *total supply*. A row that does not say which denominator it
--      used cannot be compared with that number, and a silent mismatch is how a filter
--      ends up calibrated against nothing.
--
--   3. **Bundling and sniping are separate columns.** GMGN ships them together and they
--      are different behaviours: a bundler rides inside the launch transaction ordering,
--      a sniper races it from outside. Collapsing them loses the distinction that makes
--      either one actionable.
--
--   4. **Entity resolution provenance is stored per row.** `entity_source` records
--      whether `kaiba.intelligence.entity` carried the result or the tape-derived
--      fallback did. `entities` and `cluster_edges` are empty today, so every row written
--      now says `fallback` or `none`, and a later rebuild of the graph must be able to
--      find the rows that predate it.

-- One row per token per computation. Replaced on recompute: the newest read of a fixed
-- historical fact is the best one, and the tape only ever gets more complete.
CREATE TABLE IF NOT EXISTS token_bundles (
  chain            TEXT    NOT NULL,
  token            TEXT    NOT NULL,
  computed_ms      INTEGER NOT NULL,
  model            TEXT    NOT NULL,          -- definition version; see bundles.MODEL_ID

  -- 'measured'    : the tape is proved complete back to launch and the numbers below hold.
  -- 'unavailable' : we did not look, or could not. Percentages are NULL. Read `reason`.
  coverage         TEXT    NOT NULL CHECK (coverage IN ('measured', 'unavailable')),
  reason           TEXT    NOT NULL,          -- always populated, including on success

  -- Denominator. NULL supply means percentages are NULL too; the share-of-launch-buys
  -- figure is still computable and is the one to use in that case.
  supply_atoms     TEXT,                      -- base units, TEXT per docs/CONTRACT.md
  supply_basis     TEXT,                      -- see bundles.SupplyBasis

  bundled_atoms    TEXT,
  bundled_pct      TEXT,                      -- Decimal as TEXT; 0..100; never a float
  sniped_atoms     TEXT,
  sniped_pct       TEXT,
  launch_buy_atoms TEXT,                      -- every buy in the launch window
  bundled_share_of_launch_buys TEXT,          -- needs no supply; always present when measured

  bundle_groups    INTEGER,
  bundle_entities  INTEGER,
  sniper_entities  INTEGER,
  launch_buys      INTEGER,
  unindexed_buys   INTEGER,                   -- launch buys with no block_index

  entity_source    TEXT,                      -- graph | fallback | none
  creator          TEXT,
  create_slot      INTEGER,
  last_slot        INTEGER,                   -- last slot of the launch window
  detail_json      TEXT    NOT NULL DEFAULT '{}',
  PRIMARY KEY (chain, token)
);
CREATE INDEX IF NOT EXISTS idx_token_bundles_coverage
  ON token_bundles(chain, coverage, computed_ms DESC);

-- One row per address that took part in the launch, with what it took and how.
--
-- Kept because the aggregate is not auditable on its own: "18% bundled" is a claim about
-- a specific set of addresses in a specific slot, and without the members nobody can
-- check it, re-run it against a later entity graph, or notice that the same ring is
-- bundling every launch from one creator.
CREATE TABLE IF NOT EXISTS token_bundle_members (
  chain        TEXT    NOT NULL,
  token        TEXT    NOT NULL,
  address      TEXT    NOT NULL,

  -- creator : the mint's creator, whose own launch buy has a 98.7% base rate
  --           (EDGE §4 #16) and therefore carries no information by itself.
  -- bundler : part of a qualifying same-slot contiguous group of >= 2 entities.
  -- sniper  : bought inside the launch window outside any qualifying group.
  role         TEXT    NOT NULL CHECK (role IN ('creator', 'bundler', 'sniper')),
  entity_id    TEXT,                          -- NULL when nothing linked this address
  atoms        TEXT    NOT NULL,              -- token base units bought in the window
  lamports     TEXT,                          -- native paid, NULL when the route omitted it
  first_slot   INTEGER,
  first_index  INTEGER,
  buys         INTEGER NOT NULL,
  PRIMARY KEY (chain, token, address)
);
CREATE INDEX IF NOT EXISTS idx_token_bundle_members_role
  ON token_bundle_members(chain, token, role);
CREATE INDEX IF NOT EXISTS idx_token_bundle_members_address
  ON token_bundle_members(chain, address);
