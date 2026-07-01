---
name: opencode-config
description: Author and modify OpenCode configuration in this repository — opencode.json keys, agent/*.md frontmatter, skills/*/SKILL.md format, commands, and permissions. Use when editing opencode.json, adding or changing an agent, writing a skill or command, adjusting model routing/permissions, or the task mentions "opencode config", "agent prompt", "SKILL.md", "command", or "permission".
license: MIT
compatibility: opencode
metadata:
  audience: maintainers
  workflow: opencode
---

# OpenCode Config Authoring

This repository **is** an OpenCode configuration. When changing it, ground every
edit in how OpenCode actually reads config — do not guess key names or invent
fields. Verify against the schema at `https://opencode.ai/config.json` (already
referenced by `opencode.json`'s `$schema`) before adding a new key.

## Source of truth

- The live schema is `https://opencode.ai/config.json` (`$schema` in
  `opencode.json`). If unsure whether a key exists, check the schema or the
  official docs at https://opencode.ai/docs rather than assuming.
- The two-model constraint is absolute: only `deepseek/deepseek-v4-pro` and
  `deepseek/deepseek-v4-flash`. Never introduce a third model anywhere.
- Prefer pure config/prompt changes over new plugins or dependencies.

## Repository layout

| Path | Role |
| --- | --- |
| `opencode.json` | Global config: model, permissions, plugins, agents (built-in overrides), commands, compaction |
| `AGENTS.md` | Global rules auto-loaded into every agent's context |
| `agent/<name>.md` | One custom agent per file (frontmatter + system prompt) |
| `skills/<name>/SKILL.md` | On-demand skills, auto-discovered when this repo is the global config dir |

## Agent file format (`agent/<name>.md`)

Frontmatter keys in use here (keep them consistent):

```yaml
---
name: <kebab-case, matches filename>
description: When to use this agent (drives routing + @-menu)
mode: primary | subagent
model: deepseek/deepseek-v4-pro | deepseek/deepseek-v4-flash
steps: <int>            # step budget; heavier agents get more
temperature: <0.0–1.0>  # low for deterministic (search/review), higher for ideation
color: "#RRGGBB"
hidden: true            # optional: hide from @-menu (explore, librarian)
permission:             # optional per-agent tool locks
  edit: deny
  write: deny
  task: deny
---
```

Conventions:
- Read-only agents (`oracle`, `reviewer`, `explore`, `librarian`) must set
  `edit: deny` and `write: deny`. Never let them modify files.
- Every agent's prompt should reference the global rules in `AGENTS.md` rather
  than re-stating them, and include a short **Model Leverage** (pro) or
  **Model Awareness** (flash) note explaining how it uses its model tier.
- Flash agents are directive ("get in, do the defined task, get out"); pro
  agents reason through trade-offs. Match the prompt to the tier.

## Skill file format (`skills/<name>/SKILL.md`)

- One folder per skill; the file must be named `SKILL.md` (uppercase).
- Frontmatter requires `name` (matching the folder, kebab-case) and
  `description`. The `description` must state **what** it does **and when** to
  trigger it, front-loading keywords so the model selects it correctly.
- Keep skills self-contained and copy-pasteable; a skill is loaded on demand, so
  it should carry everything the agent needs without extra context.
- Skill names must be unique across all sources (this repo + the `superpowers`
  plugin). Check for collisions before naming a new skill.

## Commands (`opencode.json` → `command`)

Commands are slash-command aliases that route to an agent with a templated
prompt:

```jsonc
"command": {
  "name": {
    "description": "Shown in the command menu",
    "agent": "<agent name>",
    "template": "Instruction sent as the user message."
  }
}
```

- `template` can inline live shell output with the `!` prefix. OpenCode runs the
  command at invocation time and injects the output into the prompt. For example,
  a template containing an inline `!` command wrapping `git status --short`:

  ```jsonc
  "template": "Current status:\n!`git status --short`\nNow stage and commit."
  ```

  Use this to gather context (git status, diffs, tags) without a manual step.
- Route commands to the cheapest capable agent: flash for mechanical tasks
  (commit, docs), pro for judgement-heavy tasks (slop removal, refactors).

## Permissions (`opencode.json` → `permission`)

- Keep the default-allow, deny-the-dangerous baseline: `deny` reads of `.env*`
  (except `.env.example`), `ask` on destructive bash (`rm -rf`, `git push -f`,
  `git reset --hard`, PowerShell/cmd equivalents), and `ask` on
  `external_directory`.
- When adding a bash guard, cover the spacing and shell variants (Unix + Windows
  PowerShell/cmd) so the guard cannot be trivially bypassed.

## Before you finish

1. Re-read every changed file end-to-end.
2. Keep `README.md` in sync — its agent table, skills table, command table,
   repo-structure tree, and iteration log must match the actual config.
3. Confirm no third model slipped in and no new dependency/plugin was added
   without explicit justification.
