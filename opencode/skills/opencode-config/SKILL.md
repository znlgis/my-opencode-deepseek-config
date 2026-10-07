---
name: opencode-config
description: Author and modify this repository's OpenCode v2 config — opencode.jsonc, agents, skills, commands, permissions. Use when editing opencode.jsonc, adding or changing an agent, writing a skill or command, adjusting model routing/permissions, or the task mentions "opencode config", "agent prompt", "SKILL.md", "command", or "permission".
---

# OpenCode Config Authoring (v2)
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
- **v2-only, native keys.** This repo targets OpenCode v2 (the desktop app's bundled `opencode-cli.exe`, measured on 2.0.24). OpenCode 1.18.x is not supported. Write the v2-native spellings — `plugins`, `providers`, `agents`, `commands`, `permissions`, `media` — never the v1 spellings (`plugin`, `provider`, `agent`, `command`, `permission`, `attachment`, `small_model`, top-level `subagent_depth`).

## Target runtime: desktop v2 (CLI 2.0.24 measured)
v2 loads `opencode.json`/`opencode.jsonc` through **two paths**: native decode,
or — if the file contains ANY v1 key — the official **V1→V2 migration**
(`isV1` trigger keys: `logLevel`, `server`, `command`, `reference`, `snapshot`,
`plugin`, `autoshare`, `disabled_providers`, `enabled_providers`, `small_model`,
`mode`, `agent`, `provider`, `permission`, `tools`, `attachment`, `layout`).
This repo standardizes on the native path: keep every v1 key out of the file,
so the loader never takes the migration branch.

### Native key map
| Concern | v2-native shape | Notes |
| --- | --- | --- |
| plugins | `plugins: [string \| {package, options}]` | string form in use |
| providers | `providers.<id>.models.<mid>.{settings, capabilities, cost, limit}` | `settings` = request passthrough (temperature, thinking); `capabilities` = `{tools, input[], output[]}` (image input lives here); `cost` = ARRAY of `{input, output, cache: {read, write}}`; `limit.input` drives compaction |
| agents / commands | `agents`, `commands` | inline overrides; file-based agents stay in `agents/*.md` |
| permissions | `permissions: [{action, resource, effect}]`, ordered, last-match-wins | v2 action names: `shell` (not `bash`), `subagent` (not `task`); `write`/`patch` normalize to `edit` |
| media | `media.image.{auto_resize, max_width, max_height, max_base64_bytes}` | former `attachment` |
| compaction | `{auto: bool, keep: {tokens}, buffer: int}` | `prune`/`tail_turns`/`reserved`/`preserve_recent_tokens` are v1-only — do not add |
| subagent_depth | `experimental.subagent_depth` | top-level `subagent_depth` is dropped by v2 — never write it |
| skills | flat `string[]` of paths/URLs | not used here (config-dir skills are auto-discovered) |
| small_model | — | v2 expands it into the `title` agent; this repo pins `title` explicitly instead |

`$schema` stays `https://opencode.ai/config.json` — the app itself writes that
URL even though the published schema only documents the v1 keys (observed
2026-10-07).

### Silent-failure traps
1. **Unknown keys are silently dropped.** Config and frontmatter loaders both
   drop keys they do not recognize — a file can look valid while a key does
   nothing. Every change gets verified against the live service (see below).
2. **YAML strictness (frontmatter).** v2's frontmatter parser is strict YAML;
   an unquoted `: ` in `description` aborts the parse, and the agent silently
   degrades to "whole file as system prompt + defaults" (no model, no steps,
   no permissions). Quote any description containing `: `.
3. **Permission order (last match wins).** Over the merged list
   [v2 defaults → global config → agent], put the catch-all `*` FIRST and
   every specific rule after it. A trailing catch-all silently shadows all
   rules above it.

## Config key shapes (authoritative)
- **references** — alias → `{"repository" | "path", "branch"?, "description"?}`. `repository` takes a Git URL / host-path / `owner/repo` (+ `branch` to pin a ref); `path` takes relative / absolute / `~/`; `description` tells agents *when* to use it.
- **agents (inline)** — `"agents": { "build": { "model": "…" } }`; inline keys override file-based `agents/<name>.md`.
- **compaction** — `{ "auto": bool, "keep": { "tokens": n }, "buffer": n }`. `keep.tokens` is the verbatim tail kept across a compaction; `buffer` is the overflow headroom. Do NOT write the v1 names (`reserved`, `preserve_recent_tokens`, `prune`, `tail_turns`).
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

- Frontmatter deliberately keeps the v1-style `permission` map and `options`
  (thinking, `reasoningEffort`) — v2 accepts both (they migrate onto its
  `permissions` list / request body) and all 12 agents are verified with them.
  Do not migrate single files to native frontmatter piecemeal; if adopted, do
  all agents in one verified pass.
- Each prompt references `AGENTS.md` (not restating it) plus a short Model Leverage (pro) / Model Awareness (flash) note.

## Skill file format (`skills/<name>/SKILL.md`)
- One folder per skill; file must be `SKILL.md` (uppercase).
- Frontmatter requires `name` (kebab-case, matches folder) and `description` stating **what** and **when**, front-loading trigger keywords.
- Names must be unique across all sources (this repo + `superpowers`); check collisions before naming.

## Commands (`opencode.jsonc` → `commands`)
```jsonc
"commands": {
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

## Permissions (`opencode.jsonc` → `permissions`)
- Root config writes the native ordered list; agent frontmatter keeps the map form (see above). Both resolve with last-match-wins semantics.
- Default-allow, deny the dangerous: `deny` `.env*` reads (except `.env.example`); `ask` on destructive bash (`rm -rf`, `git push -f`, `git reset --hard`, PowerShell/cmd equivalents) and `external_directory`. Cover shell variants (Unix + Windows) so guards can't be bypassed.

## Verification (desktop v2)
- `opencode-cli.exe debug config` / `debug agents` / `plugin list` are **service-bound**: they read the running desktop service, NOT the CLI process env — a temp-dir experiment won't show up there.
- Edit loop: change files → `node scripts/validate-jsonc.js` → `.\scripts\sync-config.ps1` → `opencode-cli.exe reload` → re-run `debug config`/`debug agents` and compare. Unknown keys are dropped silently, so a diff is the only honest evidence.
- The 1.18.x line is out of scope; do not verify against it.

## Before you finish
1. Re-read every changed file end-to-end.
2. Run `node scripts/validate-jsonc.js`.
3. Keep `README.md` in sync — agent, skills, and command tables, repo-structure tree.
4. Confirm no third model and no v1 key (`plugin`, `provider`, `agent`, `command`, `permission`, `attachment`, `small_model`, top-level `subagent_depth`) slipped into `opencode.jsonc`.
