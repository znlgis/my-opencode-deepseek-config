---
name: remove-deadcode
description: Safely find and delete dead code — unused files, exports, functions, variables, and imports — with verification before each removal. Use when the task mentions "dead code", "unused code", "remove slop", "clean up", "prune", "delete unused", "orphaned files", or after a refactor leaves leftovers. Verifies with the toolchain/LSP before deleting so nothing that is actually referenced is removed.
license: MIT
compatibility: opencode
metadata:
  audience: developers
  workflow: refactoring
---

# Remove Dead Code

Delete code that is provably unreferenced — without breaking anything. The rule
is simple: **prove it's dead before you delete it.** A confident-looking unused
symbol is often reached through a barrel export, a test, reflection, or a
framework entry point. Verify first.

Pure prompt-driven dead code removal — no plugin or team-mode required.

## Phase 1: Scan (parallel)

Fire independent detectors at once and collect candidates:

- **Compiler / linter unused flags** — e.g. TypeScript `tsc --noUnusedLocals
  --noUnusedParameters`, ESLint `no-unused-vars`, Go `deadcode`/`staticcheck`,
  Python `vulture`, Rust `cargo +nightly udeps` / dead-code warnings.
- **Orphaned files** — source files not imported by any other file.
- **Unused exports** — exported symbols never imported elsewhere.
- **LSP references** — "find all references" returning only the definition.

Delegate broad searches to `explore` subagents in parallel when the codebase is
large; keep each search scoped to one question.

## Phase 2: Verify each candidate (before deleting)

For every candidate, confirm it is genuinely dead. Discard it (keep the code) if
**any** of these is true:

- It is re-exported through a barrel/index file that external code imports.
- It is referenced by tests, fixtures, or snapshots.
- It is a framework/tooling entry point (route handler, CLI command, migration,
  plugin hook, `main`, serializer) discovered by convention, not import.
- It is reached by reflection, dynamic import, string key, or DI container.
- It is part of a published public API (`@public`, exported package surface).

Re-run "find references" (LSP) immediately before each edit — the graph may have
changed as you removed earlier items.

## Phase 3: Delete in conflict-free batches

- Group removals **by file**: everything in the same file goes in the same batch,
  so parallel edits never collide.
- Remove the symbol/file **and** its now-unused imports.
- Stage precisely: `git add <specific files>` — never `git add -A`.

## Phase 4: Prove the removal is safe

After each batch:

1. Build / type-check the project.
2. Run the test suite (or the affected subset).
3. Re-run the unused-symbol detector — the count should drop, not grow.

If the build or tests break, revert that batch and re-verify Phase 2 for the
offending item.

## Anti-patterns

- Deleting on a hunch without a references check.
- Bulk-deleting a whole directory because "it looks unused."
- Using `git add -A` and sweeping up unrelated changes.
- Removing something only reachable from tests, then deleting the tests too to
  make it look unused — that hides regressions.

## Report

List what was removed and the evidence it was dead:

```
- removed <file/symbol> — <how it was proven unused: no importers / LSP 0 refs / linter>
```

End with the build + test result so the caller can trust the change.
