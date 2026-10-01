-- The dev veto moved from 10% to 30% on 2026-09-22 and `dev_supply_threshold` did not
-- follow it.
--
-- `kaiba.intelligence.dyor.DEV_PCT_BLOCK` was PLAN §5.5's 10% and is now 30%, on the
-- operator's later instruction ("if its bundled dev buying more than 20% 30% we can still
-- buy but we need to be careful"). The confluence variable seeded by 020_confluence.sql
-- cited that constant by name and by value, and it now mirrors `DEV_PCT_WARN` -- the same
-- 10%, kept as the line where a dev holding starts costing score rather than the line
-- where it is refused.
--
-- Only the provenance text changes. `params` stays `{"limit":10.0}` deliberately: the
-- scorer's question is where a creator's share starts being a mark against the token, and
-- that answer did not move. Repointing it at 30 would have widened the ranking silently on
-- the same change that widened the veto.
--
-- This is a migration rather than an edit to 020 because 020 has already been applied on
-- the live box, so editing it there changes nothing: `tests/test_confluence.py` compares
-- the stored row against the Python spec, and the row is what an already-migrated database
-- keeps serving.
UPDATE confluence_weights
SET provenance = 'INVENTED - mirrors kaiba.intelligence.dyor.DEV_PCT_WARN = 10%, which was the veto until 2026-09-22 and is now the review line; the veto moved to 30% and this variable deliberately did not follow it. No published source sets a dev-supply threshold; the one adjacent published number is that 98.7% of launches involve a developer purchase at all (MELT, arXiv:2602.13480), which says nothing about magnitude. Weight at the measured floor so the comparison is visible.'
WHERE version = 'literature-v1' AND variable = 'dev_supply_threshold';
