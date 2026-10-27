---
name: opencode-config
description: Author and modify this repository's OpenCode v2 config — opencode.jsonc, agents, skills, commands, permissions. Use when editing opencode.jsonc, adding or changing an agent, writing a skill or command, adjusting model routing/permissions, or the task mentions "opencode config", "agent prompt", "SKILL.md", "command", or "permission".
---

# OpenCode Config Authoring (v2)
For the authoritative v2 shapes, read the schema in the local docs repo rather
than reconstructing them from memory — `packages/schema/src/config*.ts` is the
loader's own contract:
`C:\Users\Administrator\.local\share\opencode\repos\github.com\anomalyco\opencode@dev`.
This file only covers this repository's local conventions plus the traps already
paid for.

## Repository layout
| Path | Role |
| --- | --- |
| `opencode.jsonc` | Global config: model, permissions, plugins, agents, commands, compaction |
| `AGENTS.md` | Global rules auto-loaded into every agent's context |
| `agents/<name>.md` | One custom agent per file (frontmatter + system prompt) |
| `skills/<name>/SKILL.md` | On-demand skills, auto-discovered from the config dir |

## Hard constraints
- Only the natively multimodal `deepseek/deepseek-flash`, with its thinking variants `#low` / `#high`. Never another model; the one model handles both text and visual input. A **variant** (`deepseek/deepseek-flash#low`) is a thinking mode of the allowed model, not another model.
- **v2-only, native keys.** This repo targets OpenCode v2 (the desktop app's bundled `opencode-cli.exe`, measured on **2.2.0** on 2026-10-27). OpenCode 1.18.x is not supported. Write the v2-native spellings — `plugins`, `providers`, `agents`, `commands`, `permissions`, `media` — never the v1 spellings (`plugin`, `provider`, `agent`, `command`, `permission`, `attachment`, `small_model`, `autoupdate`, top-level `subagent_depth`).
- **The `opencode` on `PATH` is the wrong binary.** Measured 2026-10-16: WinGet ships **1.18.4** there, while the runtime in use is the desktop app's **2.2.0**. Always invoke `"$env:LOCALAPPDATA\Programs\@opencode-aidesktop\resources\opencode-cli.exe"`; `opencode --version` → `1.18.4` means you are about to test the wrong thing.

## Target runtime: desktop v2 (CLI 2.2.0 measured)
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
| providers | `providers.<id>.models.<mid>.{settings, body, capabilities, cost, limit, variants}` | `settings` = **provider options** only (`timeout`, `chunkTimeout`, `compaction`, `encoding`, and provider-specific ones like `reasoningEffort`) — it is NOT a raw request body and NOT a place for DeepSeek's `thinking`/`temperature`; use `body` for those; `capabilities` = `{tools, input[], output[]}` (image input lives here); `cost` = ARRAY of `{input, output, cache: {read, write}}`; `limit.input` drives compaction; `variants` = `[{id, settings?, headers?, body?}]` selected as `model: <provider>/<model>#<id>` |
| agents / commands | `agents`, `commands` | inline overrides; file-based agents stay in `agents/*.md` |
| permissions | `permissions: [{action, resource, effect}]`, ordered, last-match-wins | v2 action names: `shell` (not `bash`), `subagent` (not `task`); `write`/`patch` normalize to `edit` |
| media | `media.image.{auto_resize, max_width, max_height, max_base64_bytes}` | former `attachment` |
| compaction | `{auto: bool, keep: {tokens}, buffer: int}` | `prune`/`tail_turns`/`reserved`/`preserve_recent_tokens` are v1-only — do not add |
| subagent_depth | `experimental.subagent_depth` | top-level `subagent_depth` is dropped by v2 — never write it |
| skills | flat `string[]` of paths/URLs | not used here (config-dir skills are auto-discovered) |
| small_model | — | v2 expands it into the `title` agent; this repo pins `title` explicitly instead |

`settings.cache.ttl` (`Provider.CacheTTL`, e.g. `"5m"`/`"1h"`) is the new
provider knob found in v2.2.0. DeepSeek's context cache is automatic and its
pricing page exposes no client-side TTL, so this repo does not set it —
unverified against DeepSeek, not adopted.

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
4. **Inline `agents.<id>.permissions` are appended, never merged.** The config
   plugin pushes them onto the agent's list *after* the built-in agent rules
   and *after* the global `permissions` list, so they win — and a built-in
   lock-down can be silently defeated by the global list sitting under it
   (shipped bug: `title`/`summary` push `*:* deny`, but the global
   `skill:* allow` below re-opened the always-on skill roster). Re-assert any
   built-in deny you actually depend on.
5. **Skill frontmatter can take a skill off the roster.** `disable-model-invocation: true`
   (+ `metadata: { "opencode/autoinvoke": false }`) keeps the skill loadable by
   ID but drops it from `<available_skills>` — the way to stop paying a skill's
   name+description on every request while a command template still calls it.
6. **Agent overlays cannot carry provider settings.** An agent's `request` is
   `{headers, body}` only — there is no `settings` field. Provider settings
   resolve from the provider/model config, and a model **variant** is the only
   per-agent way to change one (`model: provider/model#variant-id`). For
   DeepSeek: the thinking toggle is a request **`body`** field
   (`{"thinking":{"type":"disabled"}}`), while effort belongs in
   **`settings.reasoningEffort`** — the only key the aisdk layer turns into the
   API's `reasoning_effort`. Writing `thinking`/`temperature` under `settings`,
   or `reasoningEffort` under an agent's `options`, is silently inert (the
   openai-chat protocol implements no such option). Shipped that way in 2026-10,
   fixed 2026-10-16 — do not reintroduce it.
7. **What `settings` *is* for.** Real provider settings: `timeout`,
   `headerTimeout`, `chunkTimeout` (defaults 300s / 900s — DeepSeek has asked
   clients for the long chunk timeout because it can stream nothing for minutes
   while reasoning), `compaction`, `encoding`, plus provider-specific options.
   Anything else you put there is dropped by the protocol layer.

## Config key shapes (authoritative)
- **references** — alias → `{"repository" | "path", "branch"?, "description"?}`. `repository` takes a Git URL / host-path / `owner/repo` (+ `branch` to pin a ref); `path` takes relative / absolute / `~/`; `description` tells agents *when* to use it.
- **agents (inline)** — `"agents": { "build": { "model": "…" } }`; inline keys override file-based `agents/<name>.md`.
- **compaction** — `{ "auto": bool, "keep": { "tokens": n }, "buffer": n }`. `keep.tokens` is the verbatim tail kept across a compaction; `buffer` is the overflow headroom. Do NOT write the v1 names (`reserved`, `preserve_recent_tokens`, `prune`, `tail_turns`).
- **Environment escape hatches** — `OPENCODE_CONFIG_DIR` points at a custom config dir (searched like `.opencode`, loaded after it so it *overrides*); `OPENCODE_CONFIG` points at a single custom config file (loaded between global and project).

## Agent frontmatter (`agents/<name>.md`)
Only these keys are native. **Any other key sets `legacy = true` and routes the whole file through the V1→V2 migration path** (`core/config/plugin/agent.ts`), so this list is a hard whitelist, not a suggestion:

| Key | Convention |
| --- | --- |
| `description` | When to use this agent (drives routing + @-menu) |
| `mode` | `primary` \| `subagent` |
| `model` | `deepseek/deepseek-flash` (natively multimodal, thinking off) \| `deepseek/deepseek-flash#low` \| `deepseek/deepseek-flash#high` (thinking variants) |
| `steps` | step budget; heavier agents get more |
| `color` | "#RRGGBB" |
| `hidden` | optional: hide from @-menu |
| `disabled` | optional: remove the agent |
| `permissions` | optional ordered ARRAY `[{action, resource, effect}]`; read-only agents (`oracle`, `reviewer`, `explore`, `librarian`) set `{action: edit, resource: "*", effect: deny}` + a read-only `shell` allowlist |
| `request` | optional `{headers, body}` overlay — **no `settings`**, so no provider setting can live here |
| `system`, `variant` | supported but not used here (the prompt body *is* the system prompt) |

- **Never write** `name`, `options`, or `permission` (the v1 spellings). `name`
  is not needed (the id comes from the filename) and all three flip the file
  onto the migration path — `options` additionally ends up in `request.body`,
  where a camelCase `reasoningEffort` is not the `reasoning_effort` DeepSeek
  reads. This is the 2026-10-16 fix; all 12 agents are native now (verified
  rule-identical to the pre-migration map form).
- `permissions` order **is** the policy (last match wins). Put the `"*"` deny
  first, then the allowlist. v2 action names are `shell`/`subagent`/`edit`
  (not `bash`/`task`/`write`); `subagent` rules are matched against the target
  agent id (`subagent.ts` evaluates `Permission.evaluate("subagent", agent.id, …)`).
- Each prompt references `AGENTS.md` (not restating it) plus a short thinking-tier note (thinking-off cheap tier vs `#high` deep tier).

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
- Both the root config and agent frontmatter write the native ordered array of `{action, resource, effect}`; they resolve with last-match-wins semantics.
- Default-ask, not default-allow: keep the `"*"` `ask` catch-all FIRST so an unrecognized destructive variant is put to a human. Never append a catch-all — a trailing one silently shadows every rule above it.
- `deny` `.env*` reads (except `.env.example`); `ask` on destructive shell (`rm -rf`, `git push -f`, `git reset --hard`, PowerShell/cmd equivalents) and `external_directory`. Cover shell variants (Unix + Windows) so guards can't be bypassed.

## Verification (desktop v2)
- `opencode-cli.exe debug config` / `debug agents` / `plugin list` are **service-bound**: they read the running desktop service, NOT the CLI process env — a temp-dir experiment won't show up there.
- `debug config` prints an **array of entries** — `{type: "document", path, info}` per config file plus `{type: "directory", path}` markers. Read `info` of the document entry; `debug agents` prints the fully merged, permission-expanded agent list (that is the honest evidence, since unknown keys are dropped silently).
- Capture both as UTF-8 or the multi-byte content corrupts the JSON:
  `[Console]::OutputEncoding = [System.Text.Encoding]::UTF8` before the call, `| Set-Content -Encoding utf8 <file>` after it.
- `reload` is present on **2.2.0** (`opencode-cli.exe --help` lists it; absent in 2.0.27, restored in 2.1.0). The config watcher also reloads on file change by itself; use `opencode-cli.exe service restart` when a forced restart is needed.
- Edit loop: change files → `node scripts/validate-jsonc.js` → `.\scripts\sync-config.ps1` → re-run `debug config`/`debug agents` and compare.
- `plugin check` / `plugin update` inspect and move package plugins; the repo keeps an explicit tag pin instead, so treat those as diagnostics only.
- The 1.18.x line is out of scope; do not verify against it.

## Before you finish
1. Re-read every changed file end-to-end.
2. Run `node scripts/validate-jsonc.js`.
3. Keep `README.md` in sync — agent, skills, and command tables, repo-structure tree.
4. Confirm no third model. `validate-jsonc.js` fails on any top-level v1 key; agent frontmatter still needs the key whitelist above checked by hand.
