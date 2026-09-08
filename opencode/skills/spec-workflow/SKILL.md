---
name: spec-workflow
description: Run a lightweight, spec-driven change workflow — proposal (WHY/WHAT), specs (WHAT), design (HOW), tasks checklist, then archive. Use when the task is a non-trivial feature or behavior change that benefits from durable, git-tracked planning artifacts, or when the request mentions "spec", "proposal", "propose a change", "spec-driven", "openspec", "design doc", "requirements", or "tasks checklist". Not for one-line fixes or pure Q&A.
---

# Spec-Driven Change Workflow

Plan and land a change through durable, git-tracked artifacts shared by humans
and coding agents. Adapted (pure prompt, no tooling) from OpenSpec OPSX.

## Philosophy

- **Enablers, not gates.** Artifacts show what's possible next, not what you MUST do — edit an earlier artifact anytime.
- **Artifacts are the source of truth.** Plans live as markdown in the repo, so they survive the session and stay greppable.
- **Brownfield-friendly + scale down.** Delta specs (ADDED/MODIFIED/REMOVED/RENAMED) describe only what changes; skip the ceremony for a one-line fix.

## Directory convention

```text
openspec/
├── specs/<capability>/spec.md          ← source of truth (shipped behavior)
├── explorations/<topic>.md             ← optional pre-proposal notes
└── changes/
    ├── <change-id>/                    ← one in-flight change (kebab-case)
    │   ├── proposal.md                 ← WHY + WHAT (required)
    │   ├── design.md                   ← HOW (optional, only when warranted)
    │   ├── tasks.md                    ← checklist the apply step tracks (required)
    │   └── specs/<capability>/spec.md  ← delta: what this change adds/edits
    └── archive/YYYY-MM-DD-<change-id>/ ← completed changes, kept for history
```

Project-wide context and rules live in `AGENTS.md` — honor it in every artifact.

## Action: propose

Create `openspec/changes/<change-id>/` with a proposal, tasks, delta specs, and
(when warranted) a design. Research existing code and `openspec/specs/` first.

- **proposal.md** (1–2 pages, WHY not HOW): `## Why`, `## What Changes` (mark **BREAKING**), `## Capabilities`, `## Impact`, an initially-empty `## Updates`.
- **tasks.md**: `- [ ]` checkboxes, grouped and ordered by dependency; each task
  fits one session and states its observable done-condition in the checkbox text
  (the verification that must pass to mark it `[x]`).
- **design.md**: only if cross-cutting, a new pattern/dependency/data-model, or
  security/performance/migration complexity (Context · Goals/Non-Goals ·
  Decisions · Risks · Migration · Open Questions).
- **skip_specs** — a pure refactor, tooling, or docs change with no observable
  behavior change needs no spec contract. Set `skip_specs: true` in
  `proposal.md` and omit delta specs; the tasks checklist still carries its own
  done-conditions.

## Writing spec files

Specs are behavior contracts, not implementation plans — they define WHAT the system does and are testable.

- Requirement header: `### Requirement: <name>` + description using **SHALL/MUST**.
- Scenario header: `#### Scenario: <name>` — **exactly 4 hashtags**, then WHEN/THEN bullets (3 hashtags or bullet form is silently ignored).
- Every requirement MUST have at least one scenario.

Delta specs (files under a change's `specs/`) group edits by operation header:

- `## ADDED Requirements` — brand-new requirements.
- `## MODIFIED Requirements` — copy the **entire** existing block, then edit it.
- `## REMOVED Requirements` — include **Reason** and **Migration**.
- `## RENAMED Requirements` — `FROM:`/`TO:` only.

## Action: apply

Read the proposal, design, and specs for the change, then work through `tasks.md`:
pick the next unchecked task, implement it, mark it `- [x]`, repeat. Pause and ask
if you hit a blocker or the design proves wrong (update the artifacts, don't diverge).

## Action: update

Revise an existing (unarchived) proposal in place:
1. Read proposal.md, design.md (if present), tasks.md.
2. Understand what should change and why.
3. Edit ONLY the plan artifacts (proposal/design/tasks/delta specs) — never edit product code.
4. Do NOT backfill missing artifacts (no design.md if none was warranted).
5. Confirm each edit before applying; changes may flow in any direction (spec → design → tasks → spec).
6. Uncheck completed tasks affected by the update; add new tasks.
7. Annotate `## Updates` with "Updated: YYYY-MM-DD — [reason]".
8. Report what changed and what needs re-execution.

## Update vs New Change — three questions

Before touching an existing (unarchived) change, ask in order:

1. Same intent? (same problem statement, not a shifted goal)
2. >50% scope overlap? (the update addresses substantially the same surface)
3. Can the original change still complete on its own?

- **Yes × 3 → update** in place. *Update preserves context.*
- **No to any → new change**. *New change provides clarity.*
  (a direction change is a new branch, not an amend — like git.)

## Action: verify (before archive)

1. Every task in tasks.md is `[x]`. If not, stop — do not archive with open tasks.
2. For each ADDED/MODIFIED requirement, point to the code fulfilling it.
3. Proposal "What Changes" matches implementation; else run `update` first.

Each task must declare its verification method up front (build / test / manual
command) — see AGENTS.md Self-Verification. Report CRITICAL (blocks archive) /
WARNING (artifact drift) / SUGGESTION; WARNING and SUGGESTION do not block.

## Action: archive (delta merge)

After verify passes (no CRITICAL), fold each delta spec into the source of
truth using operation headers only:

- `## ADDED`    → append the requirement block to specs/<capability>/spec.md.
- `## REMOVED`  → delete that requirement (record Reason + Migration first).
- `## MODIFIED` → replace the whole block (deltas must carry the FULL block).
- `## RENAMED`  → rename the header (keep content, update FROM/TO).

Anything NOT mentioned by an operation header is untouched — do not rewrite
unrelated requirements. Then move changes/<change-id>/ to archive/ and commit
with `docs(openspec): archive <change-id>`.

## Anti-patterns

- Ceremony for a trivial fix — use `planner`'s ephemeral Handoff Plan instead.
- MODIFIED specs with partial content (loses detail at archive time).
- Scenarios with 3 hashtags or bullet form (silently untracked).
- Implementing past the tasks list without updating the artifacts.
