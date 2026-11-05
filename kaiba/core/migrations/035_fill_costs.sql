-- What each live fill actually paid, decoded from its own transaction
-- (kaiba/execution/fill_costs.py, ops job `fill_costs`). One row per order.
--
-- basis: venue_event (decoded), venue_event_unpriced, multi_hop_route, no_venue_event,
-- decode_refused, fetch_failed (retried). Money is integer atoms as TEXT (lamports, wei);
-- per-token prices and fee splits are Decimal strings. Compare money in Python with int(),
-- never CAST AS INTEGER: wei amounts exceed 2**63 (MEASURED 2026-10-05).
--
-- First dry run on 16 real fills (2026-10-05): robinhood uniswap_v4 2.2%-5.7% per leg,
-- sol pump_swap 2.3%-3.3% per leg; GMGN's router take is 1.0% of every leg on both chains.

CREATE TABLE IF NOT EXISTS fill_costs (
    order_id                        TEXT PRIMARY KEY,
    chain                           TEXT,
    side                            TEXT,
    lane                            TEXT,
    token                           TEXT,
    signature                       TEXT,
    version                         TEXT,
    basis                           TEXT,
    venue                           TEXT,
    event                           TEXT,
    event_source                    TEXT,
    programs_json                   TEXT,
    block_time_ms                   INTEGER,
    native_leg                      TEXT,
    token_leg                       TEXT,
    wallet_native_delta             TEXT,
    wallet_token_delta              TEXT,
    venue_fee_native                TEXT,
    venue_fees_json                 TEXT,
    network_fee_native              TEXT,
    priority_fee_native             TEXT,
    router_fee_native               TEXT,
    router_json                     TEXT,
    spot_pre_native_per_token       TEXT,
    spot_basis                      TEXT,
    exec_native_per_token           TEXT,
    price_impact_bps                INTEGER,
    curve_impact_bps                INTEGER,
    impact_cost_native              TEXT,
    allin_native_per_token          TEXT,
    decision_ref_native_per_token   TEXT,
    decision_ref_basis              TEXT,
    exec_vs_decision_bps            INTEGER,
    allin_vs_decision_bps           INTEGER,
    order_exec_vs_decision_bps      INTEGER,
    total_cost_native               TEXT,
    total_cost_bps                  INTEGER,
    residual_native                 TEXT,
    reconciled                      INTEGER,
    detail_json                     TEXT,
    computed_ms                     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_fill_costs_chain_time ON fill_costs (chain, block_time_ms);
