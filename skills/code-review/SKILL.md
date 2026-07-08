---
name: code-review
description: Token-frugal, multi-dimension code review for a diff/branch/PR. Use when reviewing changes, checking a PR, running a review→fix loop, or the task mentions "code review", "review my changes", "review this PR", "找 bug", "审查代码". Scales review depth to diff size, reports findings by dimension and severity, and calibrates against the project's threat model to avoid severity inflation. Reports findings; never rewrites code unless explicitly asked.
license: MIT
compatibility: opencode
metadata:
  audience: reviewers
  workflow: review
---

# Code Review

A structured, token-frugal review discipline. It borrows the strengths of
multi-agent review pipelines (dimension coverage, severity calibration,
review→fix loops) **without** their weight: one reviewer pass covers all
dimensions, depth scales to diff size, and large reviews communicate through
files instead of context. Pair with the `security-review` skill when the diff
touches a trust boundary.

## Step 0 — Scope the diff first

Never review blindly. Establish the change set and its size before reading code:

- Branch vs base: `git diff --stat main...HEAD` (or the stated base)
- PR: use the `gh-cli` skill (`gh pr diff <n> --patch`, `gh pr view <n>`)
- Explicit files: just those paths

Count changed files and net changed lines. Pick the path:

| Diff size | Path | Behavior |
| --- | --- | --- |
| **≤ 8 files and ≤ 500 lines** | **Abbreviated** (default) | Single focused pass over the diff and its immediate callers. Report inline. |
| **larger, or cross-cutting** | **Full** | Walk each dimension deliberately; write findings to a file (see below). |

Abbreviated is the default — it costs ~an order of magnitude fewer tokens.
Escalate to Full only when the diff is genuinely large or high-risk (auth,
migrations, public API, concurrency). State which path you took and why in one
line.

## Review dimensions

Cover every dimension that the diff actually touches. Skip dimensions with no
relevant changes rather than padding the report.

1. **Correctness** — logic bugs, off-by-one, null/undefined, unhandled edge
   cases, error paths, incorrect assumptions about callers.
2. **Security** — injection, XSS, authz/authn gaps, secrets, path traversal,
   SSRF, unsafe deserialization. If any apply, load the `security-review` skill
   for the full checklist and reporting format.
3. **Performance** — N+1 queries, unbounded loops/allocations, blocking calls on
   hot paths, missing pagination/timeouts, leaks.
4. **Architecture** — inappropriate coupling, leaky abstractions, responsibility
   in the wrong layer, needless complexity.
5. **Maintainability** — naming, function size, magic numbers, dead code,
   duplicated logic, convention drift from the surrounding codebase.
6. **Docs & comments** — AI-boilerplate comments that restate code, commented-out
   code, comments explaining WHAT not WHY, stale doc/README claims. Enforce
   AGENTS.md Comment Discipline.
7. **Compatibility** — breaking API/signature changes, altered public contracts,
   changed defaults, DB/schema migrations, callers left unupdated.

Before reporting, verify silently: read every changed file end-to-end; check for
unused imports, leftover TODOs, and debug prints; confirm new/changed functions
have callers.

## Severity levels

- **critical** — data loss, security hole, crash, or broken core behavior. Must
  fix before merge.
- **major** — real bug or regression under plausible input; wrong results.
- **minor** — narrow-impact bug, weak error handling, notable smell.
- **nit** — style/naming/comment polish. Report only if it compounds into a
  maintainability problem; otherwise omit.

## Severity calibration (fight inflation)

Judge impact in context, not by pattern-matching a rule. A finding's severity
depends on the project's actual threat model and conventions:

- Read `AGENTS.md` (and `CLAUDE.md` if present) for stated context, threat model,
  and conventions before assigning severity.
- **Calibrate to project context**: Read `package.json` or equivalent to detect
  project stage (v0.x vs v1+), deployment model (localhost tool? public service?
  internal tool? library?), and existing conventions. Apply these heuristics:
  - **v0.x projects**: API stability/compatibility findings → minor at most
    (semver expects breaking changes).
  - **Localhost-only tools**: auth/network attack surface → minor (documented
    constraint).
  - **Internal tools**: external attack vectors → minor.
  - **v1+ public libraries**: API breaks, unvalidated input → critical/major.
- Down-rank findings that don't apply to this project's reality. Note the
  calibration reason.
- Prefer one accurate high-severity finding over ten inflated ones. Every false
  alarm the reader dismisses erodes trust in the whole review.
- If you notice a systematic mismatch (a whole category consistently doesn't
  apply here), say so once and suggest the user record it in `AGENTS.md` — that
  is the durable, token-free equivalent of a calibration file.

### Suppress known-design noise

If the caller supplies a decisions/context note — inline ("we chose X on
purpose"), a path like `.opencode/decisions.md`, or documented constraints in
`AGENTS.md`/`CLAUDE.md` — treat those choices as **intentional** and do not flag
them as findings. Surface them only if the diff itself makes the documented
choice concretely unsafe (e.g. a decision that assumed trusted input is now
reachable from an untrusted path). This is the token-free equivalent of
deepreview's `--context` suppression: it kills the biggest source of review
noise — re-litigating settled design.

## Self-skepticism check (before output)

Before writing any finding, silently run this adversarial check. Default to
**rejection** — a finding earns its place only by surviving scrutiny:

1. **Could I disprove this?** Build a counter-argument. "This is fine because…
   the error handler on line N already covers this / it only fires on admin
   paths / the input is validated upstream at line M." If the counter-argument
   is stronger than the finding, discard it.
2. **Is the severity inflated?** Would the severity hold up under a second
   reviewer's scrutiny? If you have to stretch to justify "critical," it's not
   critical. Downgrade by one level if unsure.
3. **Is this a real issue or a preference?** "This variable name could be
   better" is not a finding unless it causes real confusion. Style preferences
   that the project doesn't enforce are not review items.

**Reject the finding outright if any of these hold** (borrowed from deepreview's
validator rejection rules, condensed):

- The cited `file:line` is wrong or the code isn't actually in the diff's blast
  radius.
- It targets **pre-existing, unchanged** code this diff didn't touch (note it as
  context at most — it is not a review item for this change).
- The severity is inflated relative to the project's threat model (see
  calibration above).
- It is a pure design/style **opinion** the project doesn't enforce.
- It **duplicates** another finding — merge, don't list twice.
- It is a documented, intentional decision (see suppression above).

Only surface findings that survive all three questions and none of the rejection
rules.

Lead with a one-line severity summary:
`critical: N | major: N | minor: N | nit: N` and the path taken (abbreviated/full).

Then list findings, ordered by severity, each as:

```
[severity] <title>  (dimension)
location: path/to/file.ext:LINE
issue:  <what is wrong and the input/condition that triggers it>
impact: <what breaks, or what an attacker/user gains>
fix:    <the minimal concrete remediation a flash agent could apply>
```

Close with a short overall assessment (merge-ready? blocking items?). If the
change is genuinely clean, say so plainly — do not manufacture findings.

### Doc drift batching

Minor and nit-level documentation/comment findings (outdated comments, restating-code
comments, stale references) are batched into a single "Docs drift" entry rather
than listed individually. Only surface a docs finding as its own entry when it
is genuinely dangerous (a false claim that could cause API misuse, a security-critical
misleading comment). This keeps the report focused on substantive issues.

### Large reviews — communicate through files

On the Full path, write the findings block to a file (e.g.
`.opencode/review-<short-ref>.md`) and return only the severity summary plus the
file path to the caller. This keeps large review content out of the
orchestrator's context — the single biggest review token cost.

**Response contract for file-based reviews:** after writing the file, your reply
is *only* the summary line + the absolute path — nothing else. Do not restate
findings in the chat; the file is the artifact. (Borrowed from deepreview's
file-IPC contract, minus its multi-agent pipeline.)

## Review → fix loop

When asked to review *and fix* (e.g. `/review-loop`), run a bounded loop:

1. Review the current diff (scope + dimensions + severity as above).
2. If no findings above `nit`, stop — report clean.
3. Apply the minimal fixes for `critical`/`major` findings (and clear `minor`
   ones). Follow AGENTS.md; keep changes surgical.
4. **Verify**: run the project's format/lint/test commands. Discover them from
   `AGENTS.md` first, else infer from the repo (`package.json` scripts,
   `Makefile`, `mise.toml`, `cargo`, `ruff`, etc.). If none exist, state that
   verification was skipped.
5. Re-review only the changed region. Repeat.

Stop conditions: clean, **max 5 iterations**, or a **plateau** (the same finding
persists across two iterations — surface it and pause for a human instead of
thrashing).

## Posting to a PR

To publish findings on GitHub (e.g. `/review-pr`), use the `gh-cli` skill:
draft a pending review with per-line comments (`gh pr review --comment`) or a
review body summarizing the severity table. Never auto-approve; leave the
verdict to a human.

Place each finding at the tightest scope its location allows (3-tier placement):

1. **Line comment** — the finding's `file:line` falls inside a changed hunk.
2. **File-level comment** — the file is in the diff but the line is outside any
   hunk (e.g. a caller you had to widen to).
3. **Review body** — the finding is about a file not in the diff at all
   (cross-cutting or blast-radius concerns).

This keeps comments anchored to the diff and avoids GitHub rejecting line
comments that fall outside the PR's changed ranges.

## Rules

- Report findings as text; **do not modify code** unless the task explicitly asks
  to fix (loop mode). Read-only agents never edit regardless.
- Review the **diff and its immediate blast radius** first; widen only when a
  finding points elsewhere.
- Follow AGENTS.md — Quality Bar and Comment Discipline in particular.
- Cite concrete `file:line` locations. "Line 42 is off-by-one because…" beats
  "this looks wrong".
- Honest assessment only — never performatively positive, never inflated.
