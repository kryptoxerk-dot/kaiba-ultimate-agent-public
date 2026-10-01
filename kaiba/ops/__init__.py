"""Operations: the deterministic maintenance scheduler (Phase 7, "Operate").

Everything in here runs whether or not a model is available. Hermes cron
(``hermes/cron/jobs.yaml``) is for prompts; this package is for the backfills, grades,
polls and checks the agent's intelligence goes stale without.
"""
