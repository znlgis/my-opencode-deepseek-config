---
name: reflect
description: Review recent work to identify recurring friction and propose minimal, durable improvements to the opencode config (agents, skills, commands, rules). Use when the user says "reflect", "what can we improve", "review our setup", "optimize the config", or after a series of related sessions reveals a pattern worth codifying.
license: MIT
compatibility: opencode
metadata:
  audience: maintainers
  workflow: meta
---

# Reflect

A lightweight continuous-improvement loop for the opencode configuration. After
enough sessions, patterns emerge: the orchestrator routes wrong, a skill is
incomplete, a rule is ignored, a workflow is missing. Reflect surfaces these and
proposes minimal fixes.

Adapted from oh-my-opencode-slim's reflect skill.

## When to use

- The user says "reflect on our sessions" or "review the config"
- You notice the same friction in 3+ sessions (same agent fails, same skill
  loaded but misused, same rule violated)
- After a major config change — check that nothing broke

## Workflow

### Step 1: Gather signals

Review what's available from the current session and recent history:

1. What agents were dispatched? Any failures or escalations?
2. What skills were loaded? Were any requested but missing?
3. What AGENTS.md rules were violated or misunderstood?
4. What commands were used? Any templating issues?
5. Were there unnecessary token burns (large file reads that should've been
   explored, sequential reads that could've been parallel)?

### Step 2: Identify patterns

Look for low-cost, high-impact fixes:

| Signal | Possible Fix |
|---|---|
| Flash agent escalates to pro 3+ times for same category | Adjust agent description or routing rules |
| Skill triggered but agent ignores it | Skill description too vague — add trigger keywords |
| Repeated style violations | Add explicit rule to AGENTS.md or agent prompt |
| Agent keeps trying to delegate when it shouldn't | Add "no delegation" constraint to that agent |
| Sequential reads where parallel would work | Not a config fix — note in report as behavioral |
| Large diffs overwhelm reviewer | Threshold in code-review skill may be too high |

### Step 3: Propose changes

Present findings as a checklist:

```
## Reflect findings

### Patterns found
- [pattern]: [evidence in N sessions]

### Proposed changes
1. [change to file]: [why]
2. [change to file]: [why]

### Token savings estimate
- [N tokens saved per session by each change]

### NOT recommended
- [idea]: [why it's wrong or too expensive]
```

Only propose changes that are:
- **Durable** — applies to future sessions, not just this one
- **Minimal** — smallest config/prompt change that fixes the pattern
- **Verifiable** — you can check after the next session whether it helped

## Rules

- Do not propose changes without evidence (at least 2 sessions showing the pattern).
- Do not add complexity to fix a one-off issue. Reflect finds recurring friction.
- When proposing changes to agent prompts, prefer tightening (more specific
  triggers) over broadening (looser triggers cause more false routing).
- Follow the opencode-config skill when editing agent/skill/command files.
