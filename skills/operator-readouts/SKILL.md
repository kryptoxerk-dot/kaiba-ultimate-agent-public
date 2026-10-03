---
name: operator-readouts
description: Answer status questions from the read-out tools.
version: 0.1.0
author: Kaiba contributors
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Operations, Reporting, Health]
    related_skills: [incident-recovery, provider-budget-audit, trade-journaling, wallet-grading]
---

# Operator read-outs

## What this skill is for

Answering the operator's recurring questions (is it trading, is it making money, what did the
copy manager do, how is wallet hunting going, is the box healthy) from four read-only MCP
tools instead of terminal and SQL. Each tool reads bounded queries from the database and
returns numbers; none of them changes anything. They share their code with the
deterministic daily report (`python -m kaiba.ops.daily_report`), so a tool and the
report can never disagree.

## When to use it

- Any "status", "health", "is it trading", "why no entries" question: `kaiba_health`.
- Any "are we making money" question: `kaiba_live_ev` (defaults to Robinhood, 7 days).
- Any question about the owner's GMGN copy trades: `kaiba_copy_manager`.
- Any question about wallet grading or the Solana A/B hunt: `kaiba_wallet_grade_counts`.

## Procedure

1. **Health first.** `kaiba_health()` returns `red` (a list of plain-language problems),
   `controls` (kill switch, entries paused, reduce-only, enabled chains, live lanes,
   execute_planned), `daily_loss` (today's realized against the daily stop, the same
   number the entry gate uses), `open_positions` (age, cost, exit attempts, blind time),
   `watchdog` (heartbeat age, blind, stranded), `jobs` (ops job ok/error/timeout with the
   top error), `storage` (disk %, WAL bytes) and `pipeline_last_seen_s` (seconds since
   the last scan, signal, decision, order and close). Lead with `red`; if it is empty, say
   so.
2. **Money.** `kaiba_live_ev(days=1)` and `kaiba_live_ev(days=7)`: n, wins, mean and
   median %, net native and USD, and `by_exit_reason` with the costliest exit first.
   Quote n beside every mean; a mean over a handful of closes is not evidence.
3. **Copy trades.** `kaiba_copy_manager(hours=24)`: `live` (false = dry run), per token
   the last decision with P&L at that moment, the peak, how many runs repeated it, and
   `live_sells`. To score the dry run, use `first_decision_in_window`, not the repeats.
4. **Wallets.** `kaiba_wallet_grade_counts(hours=24)`: totals by grade, A/B by chain,
   and A/B scored in the window. Grades too large to split by chain are totals only.
5. If a field carries `error` or a section says `cannot measure`, report that verbatim.

## What not to do

- Do not re-derive these numbers with SQL or the terminal; if a tool cannot answer, say
  `cannot measure: <field>` and tell the operator which tool needs extending.
- Do not pause on `live_ev` alone: a negative expectancy is the owner's decision, not a
  mechanical fault. Pause only for a mechanical fault from `kaiba_health`, named with the
  reading that clears it.
- Do not flip copy_manager live, change GMGN copy settings, or sell copy positions.
- Do not lower a grade bar to raise `kaiba_wallet_grade_counts`.

## Source

`kaiba/mcp/server.py` (operator read-outs section) and `kaiba/ops/daily_report.py`;
background in `docs/research/audit-20261001-hermes.md`.
