# Kaiba skills

Eighteen agentskills.io-format skills, shared by Hermes, Codex CLI and Claude Code. They
are the procedural half of the agent: the deterministic services in `kaiba/` do the
mechanical work, `hermes/profiles/*/SOUL.md` sets the posture, and these files say how a
specific job is done, with which tool, against which threshold, and what the failure
modes are.

Every number in a skill cites where it came from — `config/risk.yaml`, a module constant,
`docs/PLAN.md`, or a research digest in `docs/research/`. A threshold without a source is
a bug.

## Layout

```
skills/
  README.md                     this file
  <skill-name>/
    SKILL.md                    frontmatter + body (required)
    references/*.md             tables and parameter dumps too long for the body
    scripts/*.py                stdlib-only helpers, runnable without the kaiba package
```

One directory per skill, named in kebab-case, and the directory name must equal the
`name` in the frontmatter. `tests/test_skills.py` enforces that and more.

## The eighteen

**Intelligence**

| Skill | What it decides |
|---|---|
| `wallet-grading` | whether a wallet is any good, and how much evidence stands behind the grade |
| `entity-clustering` | whether several addresses are one operator |
| `gmgn-token-dyor` | whether a token is tradeable at all |
| `holder-cluster-analysis` | who holds the supply and how they got it |
| `developer-and-social-research` | whether the creator and the social identity check out |
| `signal-normalization` | what counts as a signal, and what is a duplicate |

**Trading**

| Skill | What it decides |
|---|---|
| `wallet-confluence` | whether five independent entities really bought |
| `trusted-wallet-copy` | whether to copy, how fast, and when to follow an exit |
| `trade-intent` | the precedence chain, the lane, and the size |
| `position-protection` | the four protections, the ladder, and the anti-wick check |
| `launch-snipe` | whether the sniper is healthy, whether it makes money, and what it may change |
| `incident-recovery` | what to do when a send, a provider or the day goes wrong |

**Operations and learning**

| Skill | What it decides |
|---|---|
| `provider-budget-audit` | where the credits went and whether a paid tier is justified |
| `trade-journaling` | what gets recorded, including the stand-asides |
| `strategy-experiment` | how an idea earns capital |
| `alpha-radar` | what deserves attention this cycle |
| `early-alpha-hunt` | which finds are real by measured source quality, and which deployers to snipe |
| `airdrop-hunter` | which programmes clear the EV bar, and why multi-wallet does not |

## Frontmatter contract

```yaml
---
name: kebab-case-name          # matches the directory, <= 64 chars
description: One sentence under 60 characters ending with a period.
version: 0.1.0
author: Kaiba contributors
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Short, Tags]
    related_skills: [other-skill-name]
---
```

Body structure, in this order: what the skill is for · when to use it (concrete
triggers) · the procedure as numbered steps naming the exact `kaiba_*` MCP tools · the
thresholds with their source · failure modes · what NOT to do.

Bodies stay under 500 lines. Anything longer goes in `references/`.

## How Hermes loads them

All three profiles point at this directory:

```yaml
# hermes/profiles/{kaiba-operator,kaiba-research,kaiba-reflect}/config.yaml
skills:
  external_dirs:
    - "{{KAIBA_ROOT}}/skills"
```

`{{KAIBA_ROOT}}` is substituted by `deploy/install-hermes.sh` when the profile is
installed to `~/.hermes/profiles/<profile>/config.yaml`. Hermes scans the directory,
reads each `SKILL.md` frontmatter, and exposes the skill through the `skills` toolset;
the body is loaded when the skill is invoked. `references/` and `scripts/` are read by
the agent on demand, not preloaded.

The operator profile also enables `skill_management`, so Hermes may author and edit
skills here. Edits go through review and tests like any other change
(`strategy-experiment`), and a skill cannot grant itself execution privileges by loading
another skill — the tool surface is fixed by the MCP server, not by a skill's prose.

Codex CLI and Claude Code read the same directory, which is why the format is
agentskills.io and not a Hermes-specific one.

## The MCP tool surface these skills may name

Read: `kaiba_status`, `kaiba_events`, `kaiba_wallet`, `kaiba_token`, `kaiba_signals`,
`kaiba_positions`, `kaiba_performance`, `kaiba_journal_read`, `kaiba_playbook`.

Act: `kaiba_pause`, `kaiba_resume`, `kaiba_reduce_only`, `kaiba_set_lane_mode`,
`kaiba_set_lane_param`, `kaiba_set_cohort`, `kaiba_request_exit`.

Learn: `kaiba_journal_append`, `kaiba_propose_experiment`.

`tests/test_skills.py` asserts that every `kaiba_*` name mentioned in any `SKILL.md`
exists in `kaiba.mcp.server.TOOLS`. That test is the one that matters: it catches drift
between what the skills tell the agent to do and what the agent can actually call.

There is no tool that moves value out of an agent wallet, in this list or anywhere else,
and no skill may describe one. The signer has no code path that signs a transfer to a
non-owned address and the GMGN Agent API exposes no such endpoint; the test also asserts
that no skill mentions one.

## Adding a skill

1. Create `skills/<name>/SKILL.md` with the frontmatter above.
2. Write the body in the six-part structure. Cite every threshold.
3. Only name `kaiba_*` tools that exist.
4. Point `related_skills` at real siblings.
5. Run `python -m pytest tests/test_skills.py -q`.
