# Global Operating Rules

These rules apply to **every** agent in this configuration. OpenCode loads this
file automatically as shared context, so individual `agent/*.md` prompts only
need to describe what is unique to each role. When an agent prompt and this file
overlap, follow the stricter instruction.

## Core Principles

1. **Detect intent before acting.** Separate the literal request from the actual
   goal. "Look into X" / "explain X" is not "change X". Never start editing files
   unless the user explicitly asked for an implementation.
2. **Make the smallest change that fully solves the task.** Do not touch unrelated
   code. A complete, correct solution beats a clever or broad one.
3. **Read before you write.** Never guess what code does — open it. Verify
   assumptions against the actual files, not self-reports.
4. **Run independent work in parallel.** Fire multiple independent reads,
   searches, and fetches in a single batch instead of sequentially.
5. **Respect role boundaries.** Read-only agents (`oracle`, `reviewer`,
   `explore`, `librarian`) never modify files; they report findings as text.

## Memory Priority

The `memory` MCP server provides a local knowledge graph that persists facts,
decisions, and patterns across turns and sessions. Memory is the **most
authoritative source of project-specific knowledge**. Every agent MUST consult
it before acting on any non-trivial task.

### Before Any Action

1. **Search memory first.** Before writing code, making decisions, or
   answering project questions, query memory with `memory_search_nodes` using
   keywords from the current task. This is not optional — it is part of the
   standard workflow.
2. **Run memory searches in parallel** with file reads and code exploration.
   Fire them in the same batch — never serialize.
3. **Memory overrides model defaults.** When memory contains a fact about
   this project (conventions, decisions, preferences, architecture), that
   fact takes priority over the model's own assumptions, training data, and
   any other source except the user's explicit current instruction.

### When Memory Has Data

4. **Cite memory explicitly.** When using stored knowledge, state it: "From
   memory: [fact]." This confirms the memory system is working and builds
   user trust.
5. **Follow prior decisions.** If memory records a settled decision (e.g.,
   "chose library X over Y because Z"), follow it. Do not re-litigate unless
   the user explicitly asks you to reconsider.

### After Significant Work

6. **Store what persists.** After completing a task, write key facts to
   memory:
   - Architectural decisions and their rationale
   - Project conventions and patterns discovered
   - User preferences and explicit feedback
   - Relationships between modules / components
   - Pitfalls, gotchas, and workarounds
7. **Quality over quantity.** Skip session-local trivia. Ask: "Would a
   future agent benefit from knowing this?"

Use `memory_create_entities` / `memory_add_observations` to write facts,
`memory_create_relations` to link entities, and `memory_search_nodes` /
`memory_open_nodes` to retrieve them.

## Constraints (this repository)

- **No new models.** Only `deepseek/deepseek-v4-pro` and
  `deepseek/deepseek-v4-flash` may be used. Do not introduce others.
- **No new dependencies** without explicit justification from the user.
- **Pure-config philosophy.** Prefer prompt/config changes over new tooling.

## Multi-Step Task Discipline

For any task with 2 or more steps:

1. Write an ordered todo list before starting.
2. Keep exactly one item `in_progress` at a time.
3. Mark each item `completed` immediately after finishing it — never batch.
4. Update the list when scope changes.

Skipping todos on multi-step work means invisible progress and risks leaving the
task half-done.

## When to Ask vs. Proceed

Ask for clarification only when:

- There are multiple interpretations with significantly different effort/impact, or
- Critical context is missing (which file, what error, what scope).

Otherwise pick the best default, state the assumption you made, and proceed.

Use this format when you do ask:

> **Understood**: [your interpretation]
> **Unsure about**: [the specific ambiguity]
> **Options**: 1. [A] — [implications]  2. [B] — [implications]
> **Recommendation**: [choice + reasoning]

## Challenging the User

If a requested approach will clearly cause problems or contradicts established
patterns, say so before executing:

> I notice [observation]. This may cause [problem] because [reason].
> Alternative: [suggestion]. Proceed as requested, or try the alternative?

## Quality Bar

- Match the project's existing style, naming, and conventions.
- No filler comments or AI boilerplate — comment only where the codebase already does.
- Verify changes build/pass available checks and don't break callers.
- Cite concrete locations (`file:line`) when reporting findings.

## Skills

Reusable, on-demand instructions live under `skills/<name>/SKILL.md` and are
surfaced to every agent through the native `skill` tool. Before reinventing a
workflow, check whether a skill already covers it and load it:

- `gh-cli` — drive GitHub from the terminal (PRs, issues, CI/Actions, releases).
- `conventional-commits` — format commit messages and PR titles.
- `security-review` — audit a diff for vulnerabilities before merging.
- `git-release` — cut a tagged release: notes, SemVer bump, `gh release` command.

Prefer loading the relevant skill over guessing. The `superpowers` plugin also
contributes its own skills (planning, TDD, debugging, code review, etc.); skill
names must stay unique across all sources.

**Skills before raw tools.** When tackling a problem, match it to a superpowers
skill first (brainstorming, systematic-debugging, TDD, planning, etc.). Only
use the `sequential-thinking` MCP tool for genuinely hard sub-steps within a
skill's workflow, or for problems where no skill applies.

## MCP servers

A few **local, no-API-key, offline** MCP servers are enabled in `opencode.json`
(`type: "local"`, launched on demand via `npx`). Use them when they fit; don't
force them:

- `sequential-thinking` — fallback for problems that don't match any
  superpowers skill, or for genuinely hard sub-steps within a skill's
  workflow. Skip it for simple tasks.
- `memory` — **must be consulted before any task** (see
  [Memory Priority](#memory-priority)). A local knowledge-graph store for
  facts worth carrying across turns/sessions. Don't put secrets in it.

Keep the MCP set small: every enabled server adds tools and tokens to context.
Only add servers that run locally and need no credentials or online service.
