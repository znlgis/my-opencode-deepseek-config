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
6. **Don't create files unless asked.** Never proactively create README files,
   documentation, or any new file without explicit user request. The user asked
   for code changes, not project scaffolding.
7. **Right-size the model to the task.** v4-flash agents (explore, librarian,
   light-orchestrator) handle search, lookup, simple edits, and documentation.
   v4-pro agents (planner, deep-worker, oracle, reviewer) handle reasoning,
   complex decisions, multi-step analysis, and implementation. When a task
   borderlines between the two, prefer the flash agent; escalate to pro only
   when flash is genuinely out of its depth.

## Language

Reply to the user in the operating system's current locale language. All
agents should detect the OS language from the environment and use it for
all user-facing output — explanations, summaries, questions, and findings.
On a zh-CN Windows system, reply in Chinese. On an en-US system, reply in
English. Never force English unless the user explicitly requests it.
Subagents that return text to the orchestrator should also use the OS
language so the orchestrator can pass it through unchanged.

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

## Context Management

- **Delegate, don't accumulate.** Large files should be read by subagents, not
  loaded into the orchestrator's context. Use explore agents for broad searches.
- **Parallelize independent reads.** When you need to read 3+ files that don't
  depend on each other, fire all reads simultaneously.
- **Compress aggressively.** When a line of inquiry has run its course and its
  findings are clear, compress it. Don't let stale context accumulate.
- **One topic per subagent.** Don't ask a single subagent to do research AND
  implementation — split them.

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
- Every public function/method must have at least one caller before being
  committed. No dead code.
- Verify your changes by reading every modified file end-to-end before
  claiming completion.

## Comment Discipline

- No AI boilerplate comments. Never write comments like "Initialize the service", "Set up the handler", "Create a new instance" — the code should speak for itself.
- Comments explain WHY, not WHAT. If reading the code already tells you what it does, delete the comment.
- No commented-out code. Remove dead code; git history preserves it.
- No filler docstrings. Match the project's existing docstring convention; if the project doesn't use docstrings, don't add them.

## Skills

Reusable, on-demand instructions live under `skills/<name>/SKILL.md` and are
surfaced to every agent through the native `skill` tool. Before reinventing a
workflow, check whether a skill already covers it and load it:

- `gh-cli` — drive GitHub from the terminal (PRs, issues, CI/Actions, releases).
- `conventional-commits` — format commit messages and PR titles.
- `security-review` — audit a diff for vulnerabilities before merging.
- `git-release` — cut a tagged release: notes, SemVer bump, `gh release` command.
- `remove-deadcode` — find and safely delete unused code, verified before removal.
- `opencode-config` — author this repo's OpenCode config (agents, skills, commands, permissions).

Prefer loading the relevant skill over guessing. The `superpowers` plugin also
contributes its own skills (planning, TDD, debugging, code review, etc.); skill
names must stay unique across all sources.

**Skills before raw tools.** When tackling a problem, match it to a superpowers
skill first (brainstorming, systematic-debugging, TDD, planning, etc.). Only
fall back to step-by-step reasoning for genuinely hard sub-steps within a
skill's workflow, or for problems where no skill applies.

## Self-Verification

Before claiming any task is complete:
1. Re-read every modified file from top to bottom — look for leftover debug
   prints, TODO comments, incomplete logic
2. Verify the change doesn't break callers — grep for usages of modified
   functions/types
3. If the project has tests, run them; if not, state that tests were not available
4. Check that you haven't introduced unused imports, variables, or parameters
