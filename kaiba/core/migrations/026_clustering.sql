-- The clustering pass: what ran, what it could see, and where it refused to decide.
--
-- Why this migration exists.
--
--   `cluster_edges`, `entities` and `entity_members` have existed since 002/005 and held
--   0 rows on 2026-09-20, with 6,236 wallets and 24,909 swaps already in the database.
--   Every "entity-aware" claim in the system therefore resolved each address to itself,
--   and `confluence-5` — the gate that fires when five *independent entities* buy the
--   same token — was spoofable by anyone who could fund five wallets. The derivation
--   (`intelligence/cluster.py`) and the resolution (`intelligence/entity.py`) were both
--   written and tested; nothing had ever run them. `intelligence/clustering.py` is that
--   pass, and the four tables below are the part of its output that the existing schema
--   has nowhere to put: what a run did, what it could see per address, the components it
--   refused to attribute, and the funding facts it paid a provider for.
--
-- The one thing this schema exists to make impossible.
--
--   **"We did not look" must not read as "they are independent".**
--
--   `entity_members` can only say that an address is in an entity. Its absence is
--   ambiguous between three different facts:
--
--     a. we checked this address against every linking rule and found no link;
--     b. we hold no funding record and no signer list for it, so the two rules that
--        could have linked it were never evaluated;
--     c. we did link it, into a component so large that asserting one operator behind it
--        would be a bigger lie than asserting none.
--
--   Under `confluence-5`, (b) and (c) silently become "independent" and manufacture the
--   agreement the gate exists to detect. `clustering_coverage.status` separates all
--   three, and `checked` is a separate NOT NULL column precisely so that a consumer has
--   to read it. There is deliberately no DEFAULT on `checked` and no 'independent' value
--   in the status CHECK: an address is `unclustered`, which is an observation about our
--   evidence, not `independent`, which would be a claim about the world.
--
-- The direction-of-error problem, and why one table serves both sides of it.
--
--   Over-merging makes independent buyers look like one actor; under-merging makes one
--   actor look like a crowd. `confluence-5` (five entities must agree) is broken by
--   under-merging; a concentration gate (reject if one cluster holds >30%) is broken by
--   over-merging. They read the same graph, so no single merge threshold is safe for
--   both. The resolution is that a component too large to assert is *recorded* rather
--   than either merged or discarded: `clustering_quarantine` holds it, the confluence
--   reader collapses it to one vote (it is linked, whoever owns it), and the
--   concentration reader refuses to report a number while it is unresolved. Deleting
--   these components instead — the obvious implementation — would return their members
--   to `unclustered` and hand `confluence-5` exactly the fake independence it guards
--   against.

-- One row per execution of the pass. Append-only in practice; a re-run inserts a new row
-- rather than editing the last one, so the size distribution of clusters can be diffed
-- across runs and a rebuild that suddenly merges half the chain is visible as a jump in
-- `largest_component` rather than as a silent overwrite.
CREATE TABLE IF NOT EXISTS clustering_runs (
  run_id              INTEGER PRIMARY KEY AUTOINCREMENT,
  chain               TEXT    NOT NULL,
  model               TEXT    NOT NULL,          -- clustering.MODEL_ID
  started_ms          INTEGER NOT NULL,
  finished_ms         INTEGER,
  status              TEXT    NOT NULL CHECK (status IN ('running', 'ok', 'failed')),
  reason              TEXT    NOT NULL DEFAULT '',

  edges_by_rule_json  TEXT    NOT NULL DEFAULT '{}',   -- edge_type -> count derived this run
  entities            INTEGER,
  entity_members      INTEGER,
  largest_entity      INTEGER,
  quarantined         INTEGER,                         -- components refused as too large
  quarantined_members INTEGER,
  largest_component   INTEGER,                         -- before the size rule, including refused
  addresses_seen      INTEGER,
  addresses_checked   INTEGER,
  -- Addresses this pass considers services on fan-out grounds. Re-reported every run,
  -- not just when first found: a count that fell to zero once the labels existed would
  -- read as though the hub-collapse problem had gone away.
  fanout_hubs         INTEGER,

  -- Provider spend is reported, never estimated after the fact. NULL means the funding
  -- pass did not run at all, which is different from running and costing nothing.
  helius_credits      INTEGER,
  funding_looked_up   INTEGER,
  funding_resolved    INTEGER,

  detail_json         TEXT    NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_clustering_runs_chain ON clustering_runs(chain, started_ms DESC);

-- One row per address the pass considered, with what we actually held on it.
--
-- `checked` answers "could any rule have merged this address?", not "did one?". An
-- address with no funding record and no signer list has never been exposed to either of
-- the two rules that make an identity claim, so calling it unlinked is a statement about
-- our coverage. `rules_json` lists the rule families whose inputs were present, so the
-- definition of `checked` is auditable from the row rather than only from the code.
CREATE TABLE IF NOT EXISTS clustering_coverage (
  chain         TEXT    NOT NULL,
  address       TEXT    NOT NULL,

  -- clustered    : a member of a persisted entity.
  -- unclustered  : considered, no surviving link. NOT 'independent'.
  -- quarantined  : linked into a component too large to attribute to one operator.
  -- hub          : a service address, never clusterable (exchange, router, disperser).
  status        TEXT    NOT NULL CHECK (status IN ('clustered', 'unclustered', 'quarantined', 'hub')),

  -- 0 when neither a funding record nor a complete signer list exists for this address,
  -- i.e. when the identity-grade rules had nothing to work with.
  checked       INTEGER NOT NULL CHECK (checked IN (0, 1)),

  funding_basis TEXT    NOT NULL,        -- EvidenceBasis of the funding fact, or 'unavailable'
  signer_basis  TEXT    NOT NULL,
  rules_json    TEXT    NOT NULL DEFAULT '[]',

  entity_id     TEXT,                    -- set when status='clustered'
  component_key TEXT,                    -- set when status='quarantined'
  degree        INTEGER NOT NULL DEFAULT 0,   -- surviving edges incident to this address
  run_id        INTEGER NOT NULL,
  updated_ms    INTEGER NOT NULL,
  PRIMARY KEY (chain, address)
);
CREATE INDEX IF NOT EXISTS idx_clustering_coverage_status
  ON clustering_coverage(chain, status, checked);
CREATE INDEX IF NOT EXISTS idx_clustering_coverage_component
  ON clustering_coverage(chain, component_key);

-- Components the pass declined to call one operator.
--
-- `component_key` is a digest of the sorted member set, so a component keeps its key
-- until its membership genuinely changes and a re-run neither duplicates nor renumbers
-- it. `held_by_json` names the highest-degree members: a component held together by one
-- or two addresses is usually an unlabelled service, and those are the addresses a human
-- should look at first.
CREATE TABLE IF NOT EXISTS clustering_quarantine (
  chain         TEXT    NOT NULL,
  component_key TEXT    NOT NULL,
  size          INTEGER NOT NULL,
  reason        TEXT    NOT NULL,
  edge_types_json TEXT  NOT NULL DEFAULT '[]',
  held_by_json  TEXT    NOT NULL DEFAULT '[]',
  members_json  TEXT    NOT NULL DEFAULT '[]',
  run_id        INTEGER NOT NULL,
  first_seen_ms INTEGER NOT NULL,
  last_seen_ms  INTEGER NOT NULL,
  PRIMARY KEY (chain, component_key)
);

-- The funding fact for one address, with the receipt that paid for it.
--
-- Separate from `wallets.first_funder` on purpose, for two reasons. That column is a bare
-- address with no provenance, and docs/CONTRACT.md requires a provider-derived number to
-- carry who said it, when and at what evidence basis — so the receipt lives here. And
-- `wallets` is owned by the ingest layer: the pass reads it but does not write it, and
-- derives its SAME_FUNDER edges from this table instead (`clustering.derive_funding_edges`,
-- which reuses `cluster.py`'s confidence constants so there is still one definition of how
-- strong a funder link is). `wallets.first_funder` therefore stays NULL until whoever owns
-- that table chooses to mirror these rows into it; `execution/triage.py` reads that column
-- and stays degraded until they do.
--
-- A lookup that ran and got an answer is always stored, including the answer "the
-- earliest transaction did not fund this address" (basis 'unavailable', with a reason).
-- Omitting it would make the next run pay for the same answer again and would leave "no
-- funder found" indistinguishable from "never looked". A lookup that never got an answer
-- — the limiter refused, the provider was down — is deliberately NOT stored: it cost
-- nothing, and recording it would turn a retry-in-0.1s refusal into a seven-day cooldown.
-- That is `clustering.FundingFact.retryable`, and it exists because it happened.
CREATE TABLE IF NOT EXISTS clustering_funding (
  chain          TEXT    NOT NULL,
  address        TEXT    NOT NULL,
  funder         TEXT,                     -- NULL when the lookup could not resolve one
  amount_lamports TEXT,                    -- base units as TEXT, per docs/CONTRACT.md
  funded_ms      INTEGER,
  tx             TEXT,
  hops           INTEGER NOT NULL DEFAULT 1,
  basis          TEXT    NOT NULL,         -- EvidenceBasis
  reason         TEXT    NOT NULL DEFAULT '',
  provider       TEXT    NOT NULL,
  endpoint       TEXT    NOT NULL,
  observed_at_ms INTEGER NOT NULL,
  credits        INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (chain, address)
);
CREATE INDEX IF NOT EXISTS idx_clustering_funding_funder
  ON clustering_funding(chain, funder);
