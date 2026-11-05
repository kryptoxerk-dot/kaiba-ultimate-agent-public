-- Leader-exit observer (kaiba/learning/leader_exit.py). RECORD ONLY: for every sol sm-trenches
-- position (live and shadow) it watches the signal wallets that triggered the entry ON CHAIN
-- and records the first time one of them sells the token after our entry, the pool price at
-- that moment and at +5 s, and later our real (ladder) exit, so the pre-declared rule
-- "sell when the FIRST signal wallet sells" (LEADER_EXIT_RULE in that module) can be judged
-- against the ladder per position. Nothing reads these tables to size, gate, exit or place
-- anything.
--
-- Money / atom columns are TEXT holding integers (lamports / token atoms). CAST before
-- comparing: TEXT compares as strings.
CREATE TABLE IF NOT EXISTS leader_exit_observations (
    position_id         TEXT PRIMARY KEY,            -- positions.position_id: one row per position
    chain               TEXT NOT NULL,
    token               TEXT NOT NULL,
    lane                TEXT NOT NULL,
    mode                TEXT NOT NULL,               -- live | shadow
    opened_ms           INTEGER NOT NULL,            -- positions.opened_ms (the post-entry boundary)
    decision_id         TEXT,
    signal_ids_json     TEXT NOT NULL DEFAULT '[]',
    leaders_json        TEXT NOT NULL DEFAULT '[]',  -- [{wallet, addresses, balance_at_watch, state, last_sig}]
    n_leaders           INTEGER NOT NULL DEFAULT 0,
    token_program       TEXT,
    venue_json          TEXT NOT NULL DEFAULT '{}',  -- curve PDA / PumpSwap pool + vaults, resolved at watch start
    watch_started_ms    INTEGER,
    watch_complete      INTEGER NOT NULL DEFAULT 0,  -- 1 = every leader resolved and backfilled from entry
    -- the trigger: the earliest post-entry swap that reduced a leader's balance of the token
    trigger_wallet      TEXT,
    trigger_sig         TEXT,
    trigger_slot        INTEGER,
    trigger_block_ms    INTEGER,
    trigger_detect_ms   INTEGER,
    detect_latency_ms   INTEGER,                     -- trigger_detect_ms - trigger_block_ms (block times are whole seconds)
    trigger_backfilled  INTEGER,                     -- 1 = found by a backfill read, not a push
    trigger_kind        TEXT,                        -- swap_sell | swap_sell_unpriced
    trigger_fraction    TEXT,                        -- share of the leader's balance that sale sold (Decimal)
    leader_fill_lamports_per_atom TEXT,              -- the leader's own fill (Decimal), NULL when unpriced
    held_at_trigger     TEXT,                        -- our atoms still held at the trigger
    proceeds_before_trigger TEXT,                    -- our lamports already realised before it
    held_basis          TEXT,                        -- orders | snapshot
    detect_mark_json    TEXT NOT NULL DEFAULT '{}',  -- pool read as soon as the trigger was seen
    plus5_mark_json     TEXT NOT NULL DEFAULT '{}',  -- pool read at trigger_block_ms + 5 s
    -- the ladder (our real exit)
    closed_ms           INTEGER,
    exit_reason         TEXT,
    cost_native         TEXT,
    proceeds_native     TEXT,
    realized_native     TEXT,
    -- computed at close
    fired               INTEGER,                     -- 1 = trigger before our close
    ladder_return       REAL,
    rule_return         REAL,                        -- primary: +5 s pool quote, 3% on the simulated leg
    rule_basis          TEXT,                        -- not_fired | plus5_pool | missed:<why>
    rule_return_leader_fill REAL,                    -- secondary: the leader's own fill price
    rule_return_detect  REAL,                        -- secondary: the pool read at detection
    socket_json         TEXT NOT NULL DEFAULT '{}',  -- canary block->push lag samples and reconnects
    status              TEXT NOT NULL,               -- watching | done | unwatchable
    note                TEXT,
    rpc_calls           INTEGER NOT NULL DEFAULT 0,
    created_ms          INTEGER NOT NULL,
    updated_ms          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_leader_exit_obs_status ON leader_exit_observations(status, opened_ms);
CREATE INDEX IF NOT EXISTS idx_leader_exit_obs_token ON leader_exit_observations(chain, token);

-- Every observed reduction of a leader's balance of the position's token (pre- and post-entry),
-- one row per (position, wallet, signature). The audit trail behind the trigger.
CREATE TABLE IF NOT EXISTS leader_exit_sells (
    position_id   TEXT NOT NULL,
    wallet        TEXT NOT NULL,
    signature     TEXT NOT NULL,
    slot          INTEGER,
    block_ms      INTEGER,
    detect_ms     INTEGER NOT NULL,
    backfilled    INTEGER NOT NULL DEFAULT 0,
    post_entry    INTEGER NOT NULL,
    kind          TEXT NOT NULL,                    -- swap_sell | swap_sell_unpriced | transfer_out
    atoms         TEXT NOT NULL,                    -- token atoms that left the wallet
    lamports      TEXT,                             -- SOL received (fee added back), swap_sell only
    pre_balance   TEXT,
    fraction      TEXT,
    program       TEXT,
    PRIMARY KEY (position_id, wallet, signature)
);
CREATE INDEX IF NOT EXISTS idx_leader_exit_sells_block ON leader_exit_sells(position_id, block_ms);
