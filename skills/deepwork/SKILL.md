---
name: deepwork
description: Structured workflow for complex/heavy tasks that benefit from a review-gated, phased approach. Use when the task involves 3+ files, a major refactor, or architectural changes — or when the task mentions "deep work", "complex refactor", "multi-phase", "staged rollout". Creates durable artifacts (plan, verify, report) so the orchestrator can track progress across sessions.
license: MIT
compatibility: opencode
metadata:
  audience: implementers
  workflow: deep
---

# Deepwork

A review-gated, phased execution discipline for complex tasks. It prevents
sprawl by forcing structure: you cannot proceed to implementation until the
review gate passes. Adapted from oh-my-opencode-slim's deepwork workflow.

## When to use

- 3+ files changing in non-trivial ways
- A refactor that could break callers
- Cross-cutting concerns (auth, DB schema, shared types)
- Tasks where the user wants to review a plan before code lands

**Don't use** for: single-file edits, typo fixes, config tweaks, or anything
`light-orchestrator` can handle.

## Workflow

### Phase 1: Plan (`<task>.plan.md`)

1. **Read before writing.** Explore all affected files and their callers.
2. Write a plan to `.opencode/deepwork/<task-slug>.plan.md` with:
   - **Goal**: one sentence on what changes and why
   - **Scope**: files to touch, files explicitly NOT to touch
   - **Approach**: step-by-step, each step as a checkable item
   - **Risks**: what could break, what callers to verify
   - **Verification**: specific commands to run after implementation
3. Report the plan path to the orchestrator. **Do not proceed until the plan is
   reviewed and approved.**

### Phase 2: Implement (review-gated)

1. Work through the plan one step at a time — mark each `- [x]` as done.
2. After **every file** is modified:
   - Re-read it end-to-end
   - Verify callers still work (grep for usages)
   - Run available tests/formatters/linters
3. If the plan proves wrong mid-implementation: stop, update the plan, re-present
   it. Do not diverge silently.

### Phase 3: Verify

1. Run all verification commands listed in the plan.
2. Check that every modified function has at least one caller.
3. Check for leftover TODOs, debug prints, dead code.
4. Write a brief completion report to `.opencode/deepwork/<task-slug>.done.md`:
   ```
   ## Done: <task>
   Completed: YYYY-MM-DD
   Files changed: N
   Verification: [passed/failed/skipped — reason]
   Notes: [any surprises or decisions worth recording]
   ```

## Rules

- Follow AGENTS.md — especially Multi-Step Task Discipline and Self-Verification.
- Never skip the review gate in Phase 1. The orchestrator must see and approve
  the plan before code lands.
- If the plan changes mid-implementation, pause and update it. Silent divergence
  from a reviewed plan is how bugs ship.
- Keep plans concise (~50 lines max). This is a checklist, not a design doc.
