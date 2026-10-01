-- Copycat de-duplication registry (docs/EDGE-AND-VARIABLES.md §1, variable #1).
--
-- Originals graduate at 9.20%, copycats at 0.86% (CCS'26, arXiv 2609.10246, 15.2M
-- pump.fun coins). More than 10% of all launches reuse an existing name, symbol,
-- description or image. The whole edge is one hash comparison, so the only thing that has
-- to be right here is *who used the fingerprint first* — originality is a timestamp
-- question, not a similarity question.
--
-- Four tables, each with one job:
--   dedup_fingerprints       fingerprint -> the earliest mint that carried it
--   dedup_mint_fingerprints  mint -> its fingerprints (a fingerprint never changes, so
--                            this is a permanent cache, not a TTL cache)
--   dedup_mints              what we have looked at, including the failures
--   dedup_image_bands        8-bit bands of a 64-bit dHash, for near-duplicate lookup
--   dedup_coverage           when we started watching a chain, which is what makes
--                            "we have never seen this fingerprint" mean anything

-- The registry. One row per distinct fingerprint value per chain; `first_mint` is the
-- oldest mint we know carried it. `first_created_ms` is the *token's* creation time, not
-- ours: a backfill that arrives out of order must still resolve to the older token, so
-- `register()` demotes the incumbent when an older one turns up.
CREATE TABLE IF NOT EXISTS dedup_fingerprints (
  chain            TEXT    NOT NULL,
  kind             TEXT    NOT NULL,   -- image_sha256 | image_dhash | image_cid | metadata_cid
                                       -- | name | symbol | description
  value            TEXT    NOT NULL,
  first_mint       TEXT    NOT NULL,
  first_created_ms INTEGER,            -- token creation time; NULL when the source omits it
  first_seen_ms    INTEGER NOT NULL,   -- when *we* recorded it
  hits             INTEGER NOT NULL DEFAULT 1,
  last_mint        TEXT,
  last_seen_ms     INTEGER,
  PRIMARY KEY (chain, kind, value)
);
CREATE INDEX IF NOT EXISTS idx_dedup_fp_first_mint ON dedup_fingerprints(chain, first_mint);
CREATE INDEX IF NOT EXISTS idx_dedup_fp_hits ON dedup_fingerprints(chain, kind, hits DESC);

-- Per-mint fingerprint cache. A mint's name, symbol, description and image bytes are
-- immutable once launched, so this is computed once per mint ever and never expires.
CREATE TABLE IF NOT EXISTS dedup_mint_fingerprints (
  chain TEXT NOT NULL,
  mint  TEXT NOT NULL,
  kind  TEXT NOT NULL,
  value TEXT NOT NULL,
  PRIMARY KEY (chain, mint, kind)
);
CREATE INDEX IF NOT EXISTS idx_dedup_mint_fp_value ON dedup_mint_fingerprints(chain, kind, value);

-- Everything we have scanned, including scans that produced nothing. Without the failure
-- rows a re-scan cannot tell "never looked" from "looked and the image was a WebP we
-- cannot decode", and it would refetch the same dead URI forever.
CREATE TABLE IF NOT EXISTS dedup_mints (
  chain        TEXT    NOT NULL,
  mint         TEXT    NOT NULL,
  created_ms   INTEGER,
  scanned_ms   INTEGER NOT NULL,
  image_status TEXT,                    -- ok | skipped | unavailable | unsupported:<format>
  note         TEXT,
  PRIMARY KEY (chain, mint)
);
CREATE INDEX IF NOT EXISTS idx_dedup_mints_scanned ON dedup_mints(chain, scanned_ms DESC);

-- Near-duplicate lookup for the perceptual hash. A 64-bit dHash split into eight 8-bit
-- bands: by the pigeonhole principle two hashes within Hamming distance 7 must agree
-- exactly on at least one band, so eight indexed equality lookups find every near match
-- without scanning the table. Exact equality alone would miss the re-encode case, which
-- is the whole reason the perceptual hash exists.
CREATE TABLE IF NOT EXISTS dedup_image_bands (
  chain    TEXT    NOT NULL,
  band_no  INTEGER NOT NULL,
  band_val INTEGER NOT NULL,
  dhash    TEXT    NOT NULL,
  mint     TEXT    NOT NULL,
  PRIMARY KEY (chain, band_no, band_val, mint)
);
CREATE INDEX IF NOT EXISTS idx_dedup_bands ON dedup_image_bands(chain, band_no, band_val);

-- "We have never seen this fingerprint" is only evidence of originality if we were
-- already watching when the token was created. Before that point the honest verdict is
-- UNKNOWN, and this table is what makes the difference decidable.
CREATE TABLE IF NOT EXISTS dedup_coverage (
  chain         TEXT    PRIMARY KEY,
  first_scan_ms INTEGER NOT NULL,
  last_scan_ms  INTEGER NOT NULL,
  mints_seen    INTEGER NOT NULL DEFAULT 0
);
