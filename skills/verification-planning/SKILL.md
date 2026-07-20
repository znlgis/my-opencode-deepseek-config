---
name: verification-planning
description: Plan verification before implementation — choose the narrowest verification path for a change. Use when about to implement, before writing code, or when the task mentions "how will we verify", "verification plan", "evidence before claiming done".
---

# Verification Planning

Before writing a single line of code, answer: "How will this change be proven correct?"

## When to use

- Before implementing any non-trivial change (2+ files, logic change, new feature)
- When AGENTS.md Self-Verification asks for a verification plan
- On `/deep` or `/apply` before the first edit

## Step 1: Choose the narrowest verification path

Pick the cheapest path that provides sufficient confidence:

| Verification Path | When to use | Example |
|---|---|---|
| **Build check** | Type errors would catch the bug | `tsc --noEmit`, `cargo check` |
| **Lint** | Style/format rule violation | `eslint`, `biome check` |
| **Unit test** | Isolated logic change | `vitest run path/to/test` |
| **Integration test** | Cross-module data flow | `pytest tests/integration/` |
| **Manual command** | One-shot effect, easy to inspect | `curl localhost:3000/endpoint` |
| **File existence** | File creation, config change | `Test-Path`, `ls` |
| **Oracle review** | Complex logic, unclear correctness | Delegate to oracle agent |
| **Full suite** | Wide impact, high risk | `npm test`, `cargo test` |

Start narrow. Only widen if the narrow path can't prove correctness or if risk demands it.

## Step 2: State the evidence threshold

Define one observable condition that means "verified":
- "The build passes with zero type errors"
- "`curl /api/users` returns 200 with expected JSON schema"
- "The test `should reject invalid email` passes"
- "`Test-Path .opencode/deepwork/task.done.md` returns true"

## Step 3: Run verification, report outcome

After implementation:
1. Execute the chosen verification path
2. Report: what was checked, what was observed, why it's enough
3. If verification fails, fix and re-verify — do not skip

## Rules

- Never claim completion without evidence. "Looks right" is not verification.
- Do NOT run the full test suite just because files changed — match scope to change.
- If no existing verification path can prove correctness, create a minimal one (one test, one command).
- On failure, fix the code, not the verification — never relax the evidence standard.
