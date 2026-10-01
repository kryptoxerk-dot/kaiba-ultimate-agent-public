-- Clustering support tables. The entity graph itself lives in 002_intelligence.sql;
-- this migration only adds what the derivation layer needs on top of it.

-- Per-transaction metadata the clustering heuristics need but `swaps` does not carry:
-- the full signer list (co-signing is a hard link) and the authority of any address
-- lookup table used by the transaction (bundlers create one LUT per wallet set).
-- Kept in a side table so the ingest layer owns `swaps` alone; the reader also accepts a
-- `swaps.meta_json` column if one is ever added there.
CREATE TABLE IF NOT EXISTS swap_meta (
  chain      TEXT NOT NULL,
  tx         TEXT NOT NULL,
  meta_json  TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (chain, tx)
);

-- `derive_shared_cex_deposit` looks up exchange hubs by kind on every run.
CREATE INDEX IF NOT EXISTS idx_hub_kind ON hub_addresses(chain, kind);

-- Entity rebuilds scan the edge table one chain at a time.
CREATE INDEX IF NOT EXISTS idx_edges_chain_type ON cluster_edges(chain, edge_type);
