---
name: opencode-config
description: Author and modify OpenCode config in this repository — opencode.json, agents, skills, commands, permissions. Use when editing opencode.json, adding or changing an agent, writing a skill or command, adjusting model routing/permissions, or the task mentions "opencode config", "agent prompt", "SKILL.md", "command", or "permission".
---

# OpenCode Config Authoring
For generic opencode config shapes, see the built-in `customize-opencode` skill;
this file only covers this repository's local conventions.

## Repository layout
| Path | Role |
| --- | --- |
| `opencode.jsonc` | Global config: model, permissions, plugins, agents, commands, compaction |
| `AGENTS.md` | Global rules auto-loaded into every agent's context |
| `agents/<name>.md` | One custom agent per file (frontmatter + system prompt) |
| `skills/<name>/SKILL.md` | On-demand skills, auto-discovered from the config dir |

## Hard constraints
- Only `deepseek/deepseek-v4-pro` and the natively multimodal `deepseek/deepseek-flash`. Never a third model; flash handles both text and visual input.
- Use the singular keys (`plugin`, `snapshot`), not the fork's plural (`plugins`, `snapshots`).

## Config key shapes (authoritative)
- **references** — alias → `{"repository" | "path", "branch"?, "description"?}`. `repository` takes a Git URL / host-path / `owner/repo` (+ `branch` to pin a ref); `path` takes relative / absolute / `~/`; `description` tells agents *when* to use it. String shorthand (`"alias": "../docs"`) allowed.
- **skills.paths** — extra skill dirs: `"skills": { "paths": ["../shared-skills"] }`; supports `~/` and relative paths; `skills.urls` pulls remote skills.
- **agent (inline)** — override built-ins or define agents inline in `opencode.jsonc`: `"agent": { "build": { "model": "…", "mode": "subagent" } }`. Inline keys override file-based `agents/<name>.md`.
- **compaction** — `{ "auto": bool, "prune": bool, "reserved": number }` (defaults: `auto` true, `prune` false). `reserved` is the token buffer kept to avoid overflow during compaction.
- **Environment escape hatches** — `OPENCODE_CONFIG_DIR` points at a custom config dir (searched like `.opencode`, loaded after it so it *overrides*); `OPENCODE_CONFIG` points at a single custom config file (loaded between global and project).

## Agent frontmatter (`agents/<name>.md`)
| Key | Convention |
| --- | --- |
| `name` | kebab-case, matches filename |
| `description` | When to use this agent (drives routing + @-menu) |
| `mode` | `primary` \| `subagent` |
| `model` | `deepseek/deepseek-v4-pro` \| `deepseek/deepseek-flash` (natively multimodal) |
| `steps` | step budget; heavier agents get more |
| `color` | "#RRGGBB" |
| `hidden` | optional: hide from @-menu |
| `permission` | optional tool locks; read-only agents (`oracle`, `reviewer`, `explore`, `librarian`) must set `edit: deny` + read-only bash whitelist |

- Each prompt references `AGENTS.md` (not restating it) plus a short Model Leverage (pro) / Model Awareness (flash) note.

## Skill file format (`skills/<name>/SKILL.md`)
- One folder per skill; file must be `SKILL.md` (uppercase).
- Frontmatter requires `name` (kebab-case, matches folder) and `description` stating **what** and **when**, front-loading trigger keywords.
- Names must be unique across all sources (this repo + `superpowers`); check collisions before naming.

## Commands (`opencode.json` → `command`)
```jsonc
"command": {
  "name": {
    "description": "Shown in the command menu",
    "agent": "<agent name>",
    "template": "Instruction sent as the user message."
  }
}
```

- `template` inlines live shell output with `!`, run at invocation and injected:
  ```jsonc
  "template": "Current status:\n!`git status --short`\nNow stage and commit."
  ```

## Permissions (`opencode.json` → `permission`)
- Default-allow, deny the dangerous: `deny` `.env*` reads (except `.env.example`); `ask` on destructive bash (`rm -rf`, `git push -f`, `git reset --hard`, PowerShell/cmd equivalents) and `external_directory`. Cover shell variants (Unix + Windows) so guards can't be bypassed.

## Before you finish
1. Re-read every changed file end-to-end.
2. Run `node scripts/validate-jsonc.js` to validate JSONC syntax (strips comments + trailing commas, parses as JSON).
3. Keep `README.md` in sync — agent, skills, and command tables, repo-structure tree.
4. Confirm no third model slipped in and no new dependency/plugin without justification.
