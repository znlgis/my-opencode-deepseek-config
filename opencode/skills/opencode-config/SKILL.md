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
- Write the classic singular keys (`plugin`, `snapshot`, `attachment`, `permission`); desktop v2 up-converts them. Do not switch to v2-native spellings (`plugins`, `snapshots`, `media`, `permissions`) — 1.18.4 silently ignores those.

## Runtime compatibility: desktop v2 (primary) + 1.18.4 (floor)
Two generations read this repo's config: **desktop v2** — the app's bundled
`opencode-cli.exe` (2.0.x), used daily — and the **1.18.4 CLI**, the minimum
supported version (classic line). Write the classic spellings; v2's compat
layer up-converts them (`agent`→`agents`, `command`→`commands`,
`plugin`→`plugins`, `attachment`→`media`, `snapshot`→`snapshots`,
`permission`→an ordered `permissions` list, `small_model`→the `title` agent's
model, `skills.paths`/`urls`→a flat array). Two keys diverge (see the table):
`subagent_depth` needs both spellings written, and `compaction` maps
`reserved`/`preserve_recent_tokens` while dropping `prune`/`tail_turns`. Agent
frontmatter `options` (thinking, `reasoningEffort`) survives on v2 — it lands
in the request body. Verify against **both** binaries (`debug config` on
each); unknown keys are silently dropped by both, so a config can look valid
while a key does nothing.

| Concern | 1.18.4 (floor) | desktop v2 (primary) |
| --- | --- | --- |
| Key spellings | classic: `agent`, `command`, `plugin`, `snapshot`, `permission`, `attachment` | v2-native: `agents`, `commands`, `plugins`, `snapshots`, `permissions`, `media` (the compat layer converts) |
| `small_model` | present | expanded into the `title` agent's `model` |
| `subagent_depth` | top-level, read as-is | only `experimental.subagent_depth`; top-level is dropped — write both |
| `skills` | object (`paths`/`urls`) | flat array of path/URL strings |
| `compaction` | `prune`, `tail_turns`, `preserve_recent_tokens`, `reserved` | `keep.tokens` (from `preserve_recent_tokens`), `buffer` (from `reserved`); `prune`/`tail_turns` dropped |
| Model `cost` | flat `cache_read` / `cache_write` | nested `cache: { read, write }` |
| Agent `options` (thinking, `reasoningEffort`) | supported | supported — lands in the agent's request body |

### Two silent-failure traps (both bit this repo)
1. **YAML strictness (v2 only).** v2's frontmatter parser is strict YAML; an
   unquoted `: ` in `description` aborts the parse, and v2 falls back to
   "whole file as system prompt + default agent" (no model, no steps, no
   permissions, `mode: primary`). 1.18.4's parser tolerates it, so the break
   is invisible on the classic line. Quote any description containing `: `.
2. **Permission order (both lines).** Resolution is last-match-wins over the
   merged list [v2 defaults → global config → agent]: put the catch-all `*`
   FIRST and every specific rule after it. A trailing catch-all silently
   shadows all rules above it.

## Config key shapes (authoritative)
- **references** — alias → `{"repository" | "path", "branch"?, "description"?}`. `repository` takes a Git URL / host-path / `owner/repo` (+ `branch` to pin a ref); `path` takes relative / absolute / `~/`; `description` tells agents *when* to use it. String shorthand (`"alias": "../docs"`) allowed.
- **skills.paths** — extra skill dirs: `"skills": { "paths": ["../shared-skills"] }`; supports `~/` and relative paths; `skills.urls` pulls remote skills. (classic shape; desktop v2 up-converts it to a flat string array.)
- **agent (inline)** — override built-ins or define agents inline in `opencode.jsonc`: `"agent": { "build": { "model": "…", "mode": "subagent" } }`. Inline keys override file-based `agents/<name>.md`.
- **compaction** — `{ "auto": bool, "prune": bool, "tail_turns": number, "preserve_recent_tokens": number, "reserved": number }` (defaults: `auto` true, `prune` false). `tail_turns` caps how many recent user turns (plus their assistant/tool responses) stay verbatim; `preserve_recent_tokens` caps the verbatim token budget for recent turns; `reserved` is the token buffer kept to avoid overflow during compaction. Desktop v2 maps `reserved`→`buffer` and `preserve_recent_tokens`→`keep.tokens`, and drops `prune`/`tail_turns`.
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
- **Order: last match wins.** Put the catch-all `*` first; every specific rule after it overrides. Applies to the JSONC blocks and to agent `permission` blocks (v2 normalizes everything into one ordered rule list per action). v2 action names: `bash`→`shell`, `task`→`subagent`.

## Before you finish
1. Re-read every changed file end-to-end.
2. Run `node scripts/validate-jsonc.js` to validate JSONC syntax (strips comments + trailing commas, parses as JSON).
3. Keep `README.md` in sync — agent, skills, and command tables, repo-structure tree.
4. Confirm no third model slipped in and no new dependency/plugin without justification.
