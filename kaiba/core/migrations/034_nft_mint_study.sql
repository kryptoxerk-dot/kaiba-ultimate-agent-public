-- PAPER NFT mint study on Robinhood Chain (kaiba/learning/mint_study.py). Spends nothing.
--
-- Question: would auto-minting OpenSea (SeaDrop) drops on Robinhood Chain make money? The
-- design (2026-10-02) found one 34-minute window with 171 SeaDrop mints across 27
-- collections and >= 300 Seaport fills over 66 collections, but only 2 of the 27 minted
-- collections traded at all, and OpenSea's "floor" for a fresh mint is an ask nobody paid.
-- So the study prices every paper mint at what the chain says someone actually PAID, never
-- at an ask.
--
-- Four tables, all written by the `nft_mint_study` ops job, nothing else reads them for a
-- decision:
--
--   nft_mints        SeaDrop `SeaDropMint` events (kaiba/ingest/nft_tape.py). The scanned
--                    population: every collection minting through OpenSea on this chain.
--   nft_fills        Seaport 1.6 `OrderFulfilled` events that are SALES: `listing` (a buyer
--                    paid an ask) or `offer` (a seller accepted a bid). The seller's own
--                    mirror order in an accept-offer match (offerer == recipient) is NOT a
--                    sale and is not stored. All collections are stored, not only the ones
--                    we decide on: a collection's resale evidence must cover the 24 h BEFORE
--                    we first look at it, and a tracked-only tape would have dropped exactly
--                    that window for every collection already minting when the tape started.
--   nft_paper_mints  one row per (collection, UTC day): verdict `mint` (the rule says mint)
--                    or `shadow` (mintable, the rule says no). Shadow rows are scored the same
--                    way, so the rule is measured against the population it filters.
--   nft_paper_marks  the realisable exit at +24 h and +72 h, from real fills only.
--
-- Money is integer base units as TEXT (wei, token atoms), like every other money column
-- here. Compare with CAST(... AS INTEGER), never as text. Retention: nft_mints / nft_fills
-- are pruned by the job in bounded chunks through their ts_ms indexes (default 14 days);
-- the paper tables are small (<= a few hundred rows a day) and are kept.
CREATE TABLE IF NOT EXISTS nft_mints (
    tx             TEXT    NOT NULL,
    log_index      INTEGER NOT NULL,
    block          INTEGER NOT NULL,
    ts_ms          INTEGER NOT NULL,
    ts_exact       INTEGER NOT NULL DEFAULT 0,  -- 1: block timestamp read; 0: interpolated
    collection     TEXT    NOT NULL,            -- the NFT contract (lowercase)
    minter         TEXT    NOT NULL,
    payer          TEXT,
    fee_recipient  TEXT    NOT NULL,
    quantity       INTEGER NOT NULL,
    unit_price_wei TEXT    NOT NULL,            -- what each unit cost, fee INCLUDED
    fee_bps        INTEGER NOT NULL,            -- the marketplace's split OF that price
    stage_index    INTEGER NOT NULL,            -- 0 is not proof of a public mint (signed mints use 0 too)
    PRIMARY KEY (tx, log_index)
);
CREATE INDEX IF NOT EXISTS idx_nft_mints_collection_ts ON nft_mints(collection, ts_ms);
CREATE INDEX IF NOT EXISTS idx_nft_mints_ts ON nft_mints(ts_ms);

CREATE TABLE IF NOT EXISTS nft_fills (
    tx             TEXT    NOT NULL,
    log_index      INTEGER NOT NULL,
    block          INTEGER NOT NULL,
    ts_ms          INTEGER NOT NULL,
    ts_exact       INTEGER NOT NULL DEFAULT 0,
    kind           TEXT    NOT NULL,            -- listing | offer
    collection     TEXT    NOT NULL,
    token_id       TEXT,                        -- first NFT item's id (decimal text)
    units          INTEGER NOT NULL,            -- NFT units moved by this order
    payment_token  TEXT    NOT NULL,            -- 0x0 native ETH | WETH | USDG | other ERC-20
    gross          TEXT    NOT NULL,            -- what the buyer paid, all recipients, token atoms
    seller_net     TEXT    NOT NULL,            -- what the seller kept after fee and royalty
    market_fee     TEXT    NOT NULL,            -- paid to OpenSea's fee recipient
    royalty        TEXT    NOT NULL,            -- paid to anyone else (creator royalty)
    seller         TEXT,
    buyer          TEXT,
    zone           TEXT,
    PRIMARY KEY (tx, log_index)
);
CREATE INDEX IF NOT EXISTS idx_nft_fills_collection_ts ON nft_fills(collection, ts_ms);
CREATE INDEX IF NOT EXISTS idx_nft_fills_ts ON nft_fills(ts_ms);

CREATE TABLE IF NOT EXISTS nft_paper_mints (
    paper_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    collection     TEXT    NOT NULL,
    day_utc        TEXT    NOT NULL,            -- one decision per collection per UTC day
    decided_ms     INTEGER NOT NULL,
    decided_block  INTEGER,                     -- the tape's frontier when we decided
    verdict        TEXT    NOT NULL,            -- mint | shadow
    rule_version   TEXT    NOT NULL,
    failed_json    TEXT    NOT NULL DEFAULT '[]',  -- rule clauses that failed (empty for mint)
    features_json  TEXT    NOT NULL DEFAULT '{}',  -- every input, for later out-of-sample study
    label          TEXT,                        -- OpenSea slug/name when the hunter listed it
    mint_price_wei TEXT    NOT NULL,            -- on-chain public price for ONE unit (fee included)
    mint_fee_wei   TEXT    NOT NULL,            -- the marketplace's share of that price (informational)
    mint_gas_wei   TEXT    NOT NULL,            -- estimated at the frontier block's base fee
    sell_gas_wei   TEXT    NOT NULL,            -- estimated, charged only if there is an exit
    cost_wei       TEXT    NOT NULL,            -- mint_price_wei + mint_gas_wei
    gas_price_wei  TEXT,
    eth_usd        TEXT,                        -- ETH/USD at decision, for display only
    UNIQUE (collection, day_utc)
);
CREATE INDEX IF NOT EXISTS idx_nft_paper_mints_decided ON nft_paper_mints(decided_ms);

CREATE TABLE IF NOT EXISTS nft_paper_marks (
    paper_id       INTEGER NOT NULL,
    horizon_h      INTEGER NOT NULL,            -- 24 | 72
    mark_ms        INTEGER NOT NULL,
    window_from_ms INTEGER NOT NULL,
    window_to_ms   INTEGER NOT NULL,
    n_offer        INTEGER NOT NULL,            -- accepted-bid fills in the window
    n_listing      INTEGER NOT NULL,            -- ask fills in the window
    n_unpriced     INTEGER NOT NULL,            -- fills in a token we could not convert to ETH
    exit_basis     TEXT    NOT NULL,            -- offer_median | listing_half | none
    exit_wei       TEXT    NOT NULL,            -- per unit, after fee and royalty, before gas
    net_exit_wei   TEXT    NOT NULL,            -- after sell gas (0 when not worth selling)
    pnl_wei        TEXT    NOT NULL,            -- net_exit_wei - cost_wei   (the gated policy)
    best_basis     TEXT    NOT NULL,            -- best_offer | best_sale | none
    best_exit_wei  TEXT    NOT NULL,
    pnl_best_wei   TEXT    NOT NULL,            -- optimistic policy, reported beside, never gated
    scored_ms      INTEGER NOT NULL,
    mark_version   TEXT    NOT NULL,
    PRIMARY KEY (paper_id, horizon_h)
);
