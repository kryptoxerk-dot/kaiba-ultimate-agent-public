-- Confluence scoring: the weights, and the reason each one is the size it is.
--
-- docs/EDGE-AND-VARIABLES.md ranks the variables by published evidence. That ranking is
-- the specification, and this is where it turns into numbers. Three rules shape the
-- schema, each one a thing that would otherwise go wrong quietly.
--
-- 1. **A weight without a provenance is not storable.** `provenance` is NOT NULL and
--    `kaiba/intelligence/confluence.py` plus `tests/test_confluence.py` enforce that an
--    INVENTED weight says so in that string. The system already carries one lane full of
--    unmarked guesses; it must not get a second, and a comment in a Python file is not a
--    control.
--
-- 2. **Weights are data with a version, not constants in code.** A future fit against our
--    own closed trades is a new `version` row here, never an edit to a literal. Nothing is
--    fitted today: there are zero closed trades, and docs/research/13 §B3 puts the
--    requirement at 3,500-5,500 closed trades for a bare t=1.96 and 10,000-25,000 once
--    deflated. A weight fitted on nothing is worse than a weight chosen from a paper,
--    because it carries the same authority and none of the sample.
--
-- 3. **The seed below must equal the Python default exactly.** `LITERATURE_V1` is the
--    fallback when this table is unreachable, because the scorer has to be replayable in a
--    process that never opened the operator's database. Two copies of a number is a drift
--    hazard, so `tests/test_confluence.py::test_sql_seed_matches_python_weight_set`
--    compares them row by row, provenance strings included.
--
-- Weights are half log-odds separations in nats. A variable's full swing from its worst
-- state to its best equals the separation the literature measured, so "how much should
-- this move the answer" is a published number rather than a preference.

CREATE TABLE IF NOT EXISTS confluence_weight_sets (
  version                  TEXT    PRIMARY KEY,
  created_ms               INTEGER NOT NULL,
  -- 'literature' for a set derived from published effect sizes, 'fitted' for one estimated
  -- from our own outcomes. Nothing may be 'fitted' while fitted_from_trades is NULL.
  source                   TEXT    NOT NULL,
  fitted_from_trades       INTEGER,          -- NULL until a fit happens; never 0
  unknown_penalty_cap_nats REAL    NOT NULL,
  base_graduation_rate     REAL    NOT NULL,
  active                   INTEGER NOT NULL DEFAULT 0,
  note                     TEXT
);

CREATE TABLE IF NOT EXISTS confluence_weights (
  version            TEXT    NOT NULL,
  variable           TEXT    NOT NULL,
  weight_nats        REAL    NOT NULL,       -- 0.0 is a real answer, not a missing one
  grade              TEXT    NOT NULL,       -- measured | derived | invented
  provenance         TEXT    NOT NULL,       -- a citation, a derivation, or the word INVENTED
  mapping            TEXT    NOT NULL,       -- key into confluence.MAPPINGS
  direction          TEXT    NOT NULL,
  params_json        TEXT    NOT NULL DEFAULT '{}',
  correlation_group  TEXT,                   -- NULL means this variable stands alone
  contribution_factor REAL   NOT NULL DEFAULT 1.0,
  factor_reason      TEXT,
  position           INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (version, variable),
  FOREIGN KEY (version) REFERENCES confluence_weight_sets(version)
);
CREATE INDEX IF NOT EXISTS idx_confluence_weights_grade ON confluence_weights(version, grade);

-- One row per scored token per (as_of_ms, weights version). This is the point-in-time
-- record a later fit learns from, and the reason `as_of_ms` is part of the identity: a
-- score recomputed at a different instant is a different observation, not a correction.
-- `point_in_time` is 0 when at least one input could not be replayed exactly as of that
-- instant - the dedup registry resolves first-use against its present contents, and a
-- dossier's provider readings were taken at scan time. The promotion gates require
-- replayability, so this flag decides whether a row is admissible evidence.
CREATE TABLE IF NOT EXISTS confluence_scores (
  score_id             TEXT    PRIMARY KEY,
  chain                TEXT    NOT NULL,
  token                TEXT    NOT NULL,
  as_of_ms             INTEGER NOT NULL,
  weights_version      TEXT    NOT NULL,
  model_version        TEXT    NOT NULL,
  score_evidenced_nats REAL    NOT NULL,
  score_all_nats       REAL    NOT NULL,
  delta_invented_nats  REAL    NOT NULL,
  unknown_penalty_nats REAL    NOT NULL,
  coverage             REAL    NOT NULL,
  divergent            INTEGER NOT NULL DEFAULT 0,
  point_in_time        INTEGER NOT NULL DEFAULT 1,
  breakdown_json       TEXT    NOT NULL DEFAULT '{}',
  created_ms           INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_confluence_scores_token ON confluence_scores(chain, token, as_of_ms DESC);
CREATE INDEX IF NOT EXISTS idx_confluence_scores_run   ON confluence_scores(weights_version, as_of_ms DESC);

-- ------------------------------------------------------------------------------------
-- Seed: literature-v1. Every weight below is half a log-odds separation in nats, and the
-- provenance column carries the arithmetic. INSERT OR IGNORE so re-running is harmless
-- and so an operator edit to a weight is never silently reverted by a migration re-run.
-- ------------------------------------------------------------------------------------

INSERT OR IGNORE INTO confluence_weight_sets
  (version, created_ms, source, fitted_from_trades, unknown_penalty_cap_nats,
   base_graduation_rate, active, note)
VALUES (
  'literature-v1', 0, 'literature', NULL,
  0.40483482944157134, 0.05, 1,
  'Derived entirely from published effect sizes and from kaiba''s own creator measurement. Nothing here is fitted; there are zero closed trades and a weight fitted on nothing is worse than a weight chosen from a paper.'
);

INSERT OR IGNORE INTO confluence_weights
  (version, variable, weight_nats, grade, provenance, mapping, direction, params_json,
   correlation_group, contribution_factor, factor_reason, position)
VALUES

  --  0. copycat_reuse
  ('literature-v1', 'copycat_reuse', 1.2289500403873792, 'measured',
   'MEASURED - Szwajcok, Tsuchiya, Liu, Soska, Payer, Christin, ''Meme Coin Factories'', ACM CCS''26, arXiv:2609.10246, all 15,245,966 pump.fun coins: originals graduate at 9.20%, copycats at 0.86% (10.7x). Weight = half the log-odds separation ln(.092/.908) - ln(.0086/.9914) = 2.45790 nats. 17.7% of graduates are themselves copycats, so this is a strong prior and not a veto, which is exactly what a bounded log-odds contribution expresses.',
   'signed', 'signed', '{}',
   'creator_identity', 1.0, NULL, 0),
  --  1. creator_graduation_history
  ('literature-v1', 'creator_graduation_history', 0.40483482944157134, 'derived',
   'DERIVED - direction and concentration are measured (CCS''26: top 1% of creator clusters produce 53-59% of all coins; kaiba''s own backfill, docs/STATE.md: 576 creators / 47,433 coins / 3.43% graduation), but arXiv:2602.14860 tested whether prolific or historically successful creators predict graduation and found ''limited predictive power, with observed patterns consistent with value extraction rather than project support''. With a direct null against the naive form, Rule U applies: weight = the smallest measured separation in this set / 2 = 0.40483 nats. The mapping is a shrunk empirical log-odds against kaiba''s own population rate, normalised by the largest measured separation so no single variable can say more than the strongest measured one. Shrinkage prior = 1/pop_rate launches, i.e. the prior is worth exactly one expected graduation.',
   'creator_log_odds', 'higher_is_better', '{"normaliser":2.4579000807747584,"pop_rate":0.0343,"prior_launches":29.15451895043732}',
   'creator_identity', 0.44999999999999996, 'A per-address dev-history lookup is defeated 55% of the time by cluster-fresh creator wallets (arXiv:2609.10246 via docs/research/10 §4.4), so an unclustered reading survives with probability 0.45. Set Observation.factor=1.0 once the 3-hop funding graph resolves the creator.', 1),
  --  2. curve_velocity_sol_per_swap
  ('literature-v1', 'curve_velocity_sol_per_swap', 0.40483482944157134, 'derived',
   'DERIVED - Marino, Naviglio, Tarantelli, Lillo, arXiv:2602.14860, 655,770 tokens: ''the number of swaps required to reach a given vSol level is the dominant predictor of graduation''; liquidity accumulated in ~10 trades dramatically outperforms the same liquidity over 1,000+; the median successful launch graduates in 457 steps. The paper states rank, not effect size, so Rule U fixes the magnitude at the measured floor / 2 = 0.40483 nats - deliberately under-weighting the highest-ranked variable rather than borrowing another paper''s magnitude for it. Every mapping anchor is published and none is invented: neutral at 85 SOL / 457 steps = 0.18600 SOL per swap, +1 at 85/10 = 8.5, -1 at 85/1000 = 0.085. Caveat from docs/research/13 §B5.3: this predictor is coincident, not leading - it is observable only after the liquidity has arrived.',
   'log_band', 'higher_is_better', '{"hi":8.5,"lo":0.085,"mid":0.18599562363238512}',
   'early_flow', 1.0, NULL, 2),
  --  3. wash_trading
  ('literature-v1', 'wash_trading', 0.40483482944157134, 'measured',
   'MEASURED - arXiv:2609.10246, all 15.2M pump.fun coins: wash-traded coins graduate at 2.0%, non-wash-traded at 0.90%, and doubling wash-trading transactions raises graduation odds by ~19% (p = 3e-59). The sign is POSITIVE, which is the opposite of consensus and of how kaiba''s dossier used to score it. Weight = half the log-odds separation ln(.02/.98) - ln(.009/.991) = 0.80967 nats. The intermediate mapping uses the published per-doubling coefficient ln(1.19). Absence scores -1 only above 10 observed swaps, because the same paper measures a 0.41% wash rate among coins with 1-10 transactions against 50.31% among coins with 10,000+.',
   'wash', 'higher_is_better', '{"half_separation":0.40483482944157134,"min_support":10.0,"odds_per_doubling":1.19}',
   'early_flow', 1.0, NULL, 3),
  --  4. bot_dominated_early_activity
  ('literature-v1', 'bot_dominated_early_activity', 0.40483482944157134, 'derived',
   'DERIVED - arXiv:2602.14860: ''markets dominated by bot-like activity exhibit systematically lower graduation probabilities... high turnover and algorithmic trading do not translate into sustained capital commitment''. Direction published, magnitude not, so Rule U: measured floor / 2 = 0.40483 nats. The paper''s own bot flag is a coarse proxy (frontend routing vs direct contract calls) and ours is a different coarse proxy - early-window turnover concentration, 1 - distinct wallets / swaps - so the reading is ESTIMATED and flagged proxy=True. The mapping is a linear share with its own midpoint as neutral; it has no cutoff and no free parameter, which is the only honest shape when no threshold is published.',
   'share_inverse', 'lower_is_better', '{"min_support":10.0}',
   'early_flow', 1.0, NULL, 4),
  --  5. bundle_adjusted_concentration
  ('literature-v1', 'bundle_adjusted_concentration', 0.40483482944157134, 'derived',
   'DERIVED - MELT, arXiv:2602.13480: high-risk tokens show a 24 percentage-point higher bundle-adjusted top-10 concentration increase against 6pp for low-risk tokens; bundle statistics are 35 of 122 features and rank #2 in importance. That is a discriminative gap, not a pair of conditional rates, and converting it to log-odds needs a within-class variance the paper does not publish, so the magnitude cannot be computed. The same paper''s ablation - AUPRC 0.5729 -> 0.5451 removing bundle stats, on an 84%-positive class - argues for the floor rather than a large magnitude, so Rule U: 0.40483 nats. Mapping anchors are the published 6pp / 24pp pair applied to the adjusted-minus-raw delta, which is the quantity the paper actually measured and is exactly ConcentrationReport.delta.',
   'pp_band', 'lower_is_better', '{"bad_pp":24.0,"good_pp":6.0}',
   'holder_structure', 1.0, NULL, 5),
  --  6. raw_top10_concentration
  ('literature-v1', 'raw_top10_concentration', 0.0, 'measured',
   'MEASURED NULL - weight is deliberately zero. MELT (arXiv:2602.13480) ablation: removing concentration features alone moves AUPRC 0.5729 -> 0.5693, i.e. -0.0036, ''essentially nothing on its own''. docs/research/10 §4.7 is blunt that no published study establishes any specific holder-concentration threshold on Solana memecoins with a measured hit rate; those thresholds appear only in vendor blogs. The evidenced version of this measurement is bundle_adjusted_concentration, which is a different quantity. Reported so a caller can see the number without it moving the score.',
   'report_only', 'report_only', '{}',
   'holder_structure', 1.0, NULL, 6),
  --  7. dev_bought_own_bundle
  ('literature-v1', 'dev_bought_own_bundle', 0.0, 'measured',
   'MEASURED NULL - weight is deliberately zero. MELT (arXiv:2602.13480): 98.7% of creation events co-occur with developer purchases at the lowest pricing tier. A signal that fires on essentially every launch carries no information, and as a binary filter it would reject ~99% of the universe. What carries signal is bundle magnitude, which is bundle_adjusted_concentration.',
   'report_only', 'report_only', '{}',
   NULL, 1.0, NULL, 7),
  --  8. freeze_authority_live
  ('literature-v1', 'freeze_authority_live', 0.0, 'measured',
   'MEASURED NULL - weight is deliberately zero. ''From Hype to Collapse'', arXiv:2603.24625, 76,469 Solana rug candidates: Pump-and-Dump 78.9%, Liquidity Withdrawal 20.4%, Freeze Authority Abuse 0.6% (461 cases). Keep the check - it is cheap and it is a real blocker when it fires - but it must not be counted as coverage, which is what a nonzero weight here would do.',
   'report_only', 'report_only', '{}',
   NULL, 1.0, NULL, 8),
  --  9. sniper_exposure
  ('literature-v1', 'sniper_exposure', 0.0, 'measured',
   'MEASURED NULL - weight is deliberately zero. Sniper presence lifts buyer count by +16.1% [13.0, 19.4] but SOL inflow by +6.3% [-0.5, +15.1], which contains zero (docs/research/10 §2.4b). That is the appearance of demand without the capital, which is what a trap looks like rather than what an edge looks like. Webacy''s Critical/High/Medium tiers have no published validation against outcomes.',
   'report_only', 'report_only', '{}',
   'holder_structure', 1.0, NULL, 9),
  -- 10. wallet_pnl_grade
  ('literature-v1', 'wallet_pnl_grade', 0.0, 'measured',
   'MEASURED NULL - weight is deliberately zero. arXiv:2602.14860 conditioned graduation probability on whether ex-ante identified historically profitable wallets traded a token, across 655,770 tokens, and found ''at most a modest and non-monotonic effect''; their explanation is that a skilled wallet in the book is also a competent seller. docs/research/13 Part A: no cohort-persistence study exists anywhere, and our 6,236 imported wallets came from vendor leaderboards that publish no method. Input only, never a trigger.',
   'report_only', 'report_only', '{}',
   NULL, 1.0, NULL, 10),
  -- 11. independent_entity_count
  ('literature-v1', 'independent_entity_count', 0.0, 'derived',
   'DERIVED, report only - weight is deliberately zero. docs/CONTRACT.md and kaiba.intelligence.entity.independent_entity_count: confluence counts entities, not addresses, because five addresses funded by one wallet are one opinion. The collapse is applied when this number is computed. It carries no weight because the underlying evidence for wallet quality is a published null (see wallet_pnl_grade); promoting it to a weighted variable would re-import the wallet-PnL assumption through the side door. Use it for sizing, per docs/EDGE-AND-VARIABLES.md §4 item 10.',
   'report_only', 'report_only', '{}',
   NULL, 1.0, NULL, 11),
  -- 12. raw_top10_threshold
  ('literature-v1', 'raw_top10_threshold', 0.40483482944157134, 'invented',
   'INVENTED - mirrors kaiba.intelligence.dyor.TOP10_PCT_WARN = 35%. No published study establishes any holder-concentration threshold on Solana memecoins (docs/research/10 §4.7, docs/EDGE-AND-VARIABLES.md §1 #8). The weight is set to the measured floor so that its effect on the ranking can be measured against the evidenced score, not because 35% is justified. It is not.',
   'threshold_above_bad', 'lower_is_better', '{"limit":35.0}',
   'holder_structure', 1.0, NULL, 12),
  -- 13. dev_supply_threshold
  ('literature-v1', 'dev_supply_threshold', 0.40483482944157134, 'invented',
   'INVENTED - mirrors kaiba.intelligence.dyor.DEV_PCT_BLOCK = 10%. No published source sets a dev-supply threshold; the one adjacent published number is that 98.7% of launches involve a developer purchase at all (MELT, arXiv:2602.13480), which says nothing about magnitude. Weight at the measured floor so the comparison is visible.',
   'threshold_above_bad', 'lower_is_better', '{"limit":10.0}',
   'holder_structure', 1.0, NULL, 13),
  -- 14. bundler_exposure_threshold
  ('literature-v1', 'bundler_exposure_threshold', 0.40483482944157134, 'invented',
   'INVENTED - mirrors kaiba.intelligence.dyor.BUNDLER_PCT_WARN = 15%. MELT finds coordinated accounts hold 36.5% of supply on average, so 15% is below the population mean and would fire on most launches; no study sets a cutoff. Weight at the measured floor so the comparison is visible.',
   'threshold_above_bad', 'lower_is_better', '{"limit":15.0}',
   'holder_structure', 1.0, NULL, 14),
  -- 15. sniper_exposure_threshold
  ('literature-v1', 'sniper_exposure_threshold', 0.40483482944157134, 'invented',
   'INVENTED - mirrors kaiba.intelligence.dyor.SNIPER_PCT_WARN = 20%. The published sniper result is a null on SOL inflow (docs/research/10 §2.4b) and the vendor tiers that name 20% publish no validation. Weight at the measured floor so the comparison is visible.',
   'threshold_above_bad', 'lower_is_better', '{"limit":20.0}',
   'holder_structure', 1.0, NULL, 15),
  -- 16. liquidity_floor
  ('literature-v1', 'liquidity_floor', 0.40483482944157134, 'invented',
   'INVENTED - mirrors kaiba.intelligence.dyor.LOW_LIQUIDITY_USD = $10,000. A depth floor is a sizing constraint with a real rationale (our own exit is the adverse move), but $10,000 is not a measured discriminator of anything and no study establishes one. Ungrouped, because depth is not a read of the holder table. Weight at the measured floor so the comparison is visible.',
   'threshold_below_bad', 'higher_is_better', '{"limit":10000.0}',
   NULL, 1.0, NULL, 16);
