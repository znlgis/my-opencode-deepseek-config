---
name: conventional-commits
description: Write commit messages and PR titles that follow the Conventional Commits standard. Use when committing changes, drafting a commit message, squashing, or naming a pull request, and you need the correct type(feat/fix/docs/...), scope, breaking-change marker, and imperative subject line.
license: MIT
compatibility: opencode
metadata:
  audience: developers
  workflow: git
---

# Conventional Commits

Produce commit messages and PR titles in the Conventional Commits format so
history stays machine-readable and changelogs/version bumps can be automated.
Spec: https://www.conventionalcommits.org

## Format

```
<type>(<optional scope>): <imperative subject>

<optional body — what changed and why, wrapped at ~72 cols>

<optional footer(s) — BREAKING CHANGE:, Closes #123, Co-authored-by:>
```

## Types

| Type       | Use for                                              | SemVer |
| ---------- | ---------------------------------------------------- | ------ |
| `feat`     | a new user-facing feature                            | minor  |
| `fix`      | a bug fix                                            | patch  |
| `docs`     | documentation only                                  | —      |
| `style`    | formatting/whitespace, no behavior change            | —      |
| `refactor` | code change that neither fixes a bug nor adds a feat | —      |
| `perf`     | performance improvement                              | patch  |
| `test`     | adding or fixing tests                              | —      |
| `build`    | build system or dependencies                        | —      |
| `ci`       | CI configuration and scripts                        | —      |
| `chore`    | maintenance that doesn't touch src or tests          | —      |
| `revert`   | reverts a previous commit                           | —      |

## Rules

1. **Subject is imperative mood**: "add", not "added"/"adds".
2. **No trailing period**; keep the subject ≤ ~50 chars when possible.
3. **Scope** is optional and names the affected area: `feat(parser): ...`.
4. **Breaking changes** are marked with `!` after the type/scope **and/or** a
   `BREAKING CHANGE:` footer explaining the migration.
5. **One logical change per commit.** Split unrelated work.
6. Reference issues in the footer (`Closes #42`), not the subject.

## Examples

```text
feat(auth): add OAuth2 device-code login flow

fix(api): guard against null user before serializing

docs: clarify skill auto-discovery paths in README

refactor(orchestrator)!: drop legacy qwen routing table

BREAKING CHANGE: the `qwen3.7-max` route is removed; callers must
use the deepseek-v4 tiers instead.
```

## When NOT to over-engineer

For a tiny one-file tweak, a single-line `type: subject` is enough — a body is
only needed when the *why* isn't obvious from the diff. Match the repository's
existing commit style if it already follows a different but consistent convention.
