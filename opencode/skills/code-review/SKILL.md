---
name: code-review
description: Lightweight single-pass code review for a diff/branch/PR. Use when reviewing changes, checking a PR, or the task mentions "code review", "review my changes", "review this PR", "找 bug", "审查代码". Reports blockers (critical/major) and notes (minor/nit) with evidence; never rewrites code.
---

# Code Review

## Overview

A single-pass review: one pass covers all dimensions, depth scales to effective
size. Pair with `security-review` only when the diff touches a trust boundary.

## Scope by *effective* size, not raw lines

Establish the change set before reading code:

- Branch vs base: `git diff --stat main...HEAD` (or the stated base)
- PR: use the `gh-cli` skill (`gh pr diff <n> --patch`, `gh pr view <n>`)
- Explicit files: just those paths

Weight each changed file by category — a 2000-line lockfile diff is not a
2000-line review. Sum the **effective logic lines**:

| File category | Weight | Examples |
| --- | --- | --- |
| generated / mechanical | **0×** | lockfiles, `*.pb.go`, snapshots, `@generated`, import-only reshuffles |
| data / config | **0.25×** | `.json`, `.yaml`, `.toml`, `.tf`, fixtures |
| tests | **0.5×** | `*_test.*`, `*.spec.*`, `__tests__/` |
| logic | **1×** | everything else (source code) |

Pick the path from effective size **and** stakes:

| Condition | Path | Behavior |
| --- | --- | --- |
| **≤ 8 logic files and ≤ 300 effective lines** | **Abbreviated** (default) | Single focused pass over the diff and its immediate callers. Report inline. |
| **larger** | **Full** | Walk each dimension deliberately. |

**High-stakes override** — route **Full** regardless of size when any changed
logic/config file matches: `auth|authz|migration|schema|lock|concurr|public api|
wire contract|serializ`. A hit upgrades the depth; no hit keeps the default.

Abbreviated is the default — it costs ~an order of magnitude fewer tokens.
State which path you took, the effective size, and any stakes trigger in one line.

**Scope constraint:** report only findings attributable to this diff. Nothing
outside the blast radius — not pre-existing unchanged code, not unrelated files.
ADRs and other historical decision documents are records, not living specs: do
not flag them stale.

## Large-diff two-axis split

Default is the single pass above. Only when the effective diff is large
(>~500 effective lines), split into two parallel axes and merge into one
report — never a consensus loop:

- **Standards axis** — style, naming, convention drift, AGENTS.md
  Comment Discipline / Anti-Patterns, copy-paste, dead imports.
- **Spec axis** — does the diff satisfy its stated requirements / referenced
  spec / acceptance criteria?

Run both axes over the same diff, merge findings under the one severity
scheme below, and report once. Two passes, one verdict, no re-review loop.

## Review dimensions

One pass covering all dimensions the diff touches. Skip dimensions with no
relevant changes — don't pad.

1. **Correctness** — logic bugs, off-by-one, null/undefined, unhandled edge
   cases, error paths. No empty catch, no `@ts-ignore` without comment.
2. **Security** — injection, XSS, authz/authn gaps, secrets, path traversal,
   SSRF, unsafe deserialization.
3. **Compatibility** — breaking API/signature changes, altered public contracts,
   changed defaults, DB/schema migrations, callers left unupdated.
4. **Maintainability** — naming (no catch-all files, descriptive filenames, no
   import aliases), function size, magic numbers, duplicated logic, convention
   drift, no wildcard imports, no commented-out code.
5. **Docs & comments** — why not what, no AI filler words, no emoji, no stale
   docs. Enforce AGENTS.md Comment Discipline and Anti-Patterns.
6. **Performance** — N+1 queries, unbounded loops/allocations, blocking calls
   on hot paths, missing pagination/timeouts, leaks.
7. **Architecture** — inappropriate coupling, leaky abstractions, wrong-layer
   responsibility, needless complexity, race conditions (if applicable).
8. **Mechanical scan** — 6+ identical lines in 2+ places (copy-paste); pattern
   drift; naming mismatch; dead imports.

Before reporting, silently verify: read every changed file end-to-end; check
unused imports, leftover TODOs, debug prints; confirm new functions have callers.

## Severity levels

- **critical** — data loss, security hole, crash, or broken core behavior. Must fix.
- **major** — real bug or regression under plausible input; wrong results.
- **minor** — narrow-impact bug, weak error handling, notable smell.
- **nit** — style/naming/comment polish. Report only if it compounds into a
  maintainability problem; otherwise omit.

Assign only the level the evidence supports — when in doubt, go one level down,
never up.

## Under-claim + suppress known-design noise

Under-claim per the Severity levels rule above. Treat documented decisions (caller context note, `AGENTS.md`/`CLAUDE.md`)
as intentional; flag only when concretely unsafe.

## Approval gate

APPROVE unless you can cite a specific requirement the diff fails, with
evidence. A requirement is the diff's stated purpose, an AGENTS.md rule, or a
concrete broken behavior you can point at. A gap you cannot tie to such a
criterion is a NOTE (minor/nit), not a blocker. Blockers are `critical` and
`major` only. Omit issues a green gate (CI/lint/typecheck) already enforces.
Under-claim per the Severity levels rule above. A short
review with one substantiated blocker beats a long list of nits.

## Report format

Lead with a one-line severity summary:

```
critical: N | major: N | minor: N | nit: N  (path: abbreviated|full, effective size N lines)
```

Then list findings, ordered by severity, each as:

```
[severity] <title>
location: path/to/file.ext:LINE
issue:  <what is wrong and the input/condition that triggers it>
impact: <what breaks, or what an attacker/user gains>
evidence: <the specific code/logic proving it — or why this is a NOTE not a blocker>
fix:    <minimal concrete remediation>
```

Close with a one-line merge-ready/blocking assessment. If the change is
genuinely clean, say so plainly in one line — do not manufacture findings.

## Size discipline

A ~200-line PR gets a real review; a ~2000-line PR gets a rubber stamp. Depth
scales to effective size — don't fake thoroughness.

## Review → fix loop

The reviewer never fixes code (read-only). When a fix is requested, the
orchestrator runs a bounded loop:

1. Reviewer reports blockers (critical/major) + notes.
2. If blockers: deep-worker fixes only criterion-cited blockers, delta-only.
3. Fresh one-shot reviewer re-reviews only the delta diff. At most 2 re-reviews.
4. Budget exhausted or clean → surface remaining risk to the user; never
   reopen accepted/resolved concerns.

## Posting to a PR

To publish findings (e.g. `/review`), load the `gh-cli` skill — its
'Reviewing PRs' section is the single source of truth. Never auto-`APPROVE`.

## Rules

- Report findings; do not modify code.
- Review the diff and blast radius first; widen only when needed.
- Follow AGENTS.md quality and comment rules.
- Cite concrete `file:line` locations.
- No performative positivity or inflated severity.
- Review independently — do not anchor on the implementer's summary or prior
  review comments; form your own read of the diff first, then reconcile.
