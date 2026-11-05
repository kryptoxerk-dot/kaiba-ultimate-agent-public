-- Wallets ranked by the daily signal audit (kaiba/learning/signal_audit.py, top_wallets): per
-- run and chain, the wallets whose signals replayed best over the window (>= 5 signals).
-- Records only. Whether a record means anything is decided per chain by the audit's
-- wallet_track_* cells on both halves, not by a wallet's place in this list.
CREATE TABLE IF NOT EXISTS audit_wallets (
    run_id   TEXT NOT NULL,
    chain    TEXT NOT NULL,
    address  TEXT NOT NULL,
    n        INTEGER NOT NULL,
    mean     REAL,
    win      REAL,
    PRIMARY KEY (run_id, chain, address)
);
