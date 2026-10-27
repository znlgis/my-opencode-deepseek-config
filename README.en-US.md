# My OpenCode × DeepSeek Config

[简体中文](README.md) | **English**

**OpenCode v2 × DeepSeek Optimal Config** — a configuration scheme that keeps a single model, `deepseek/deepseek-flash`, and splits agent roles by thinking tier within OpenCode v2's multi-agent framework. Core philosophy: **token efficiency first — the best development results at the lowest context cost**.

## Current Configuration Overview

- Default primary agent: `orchestrator`
- Sole model: `deepseek/deepseek-flash` (V4.1 Flash, natively multimodal); agents differ only by thinking tier — default (thinking off) / `#low` / `#high`
- Agent nesting: `experimental.subagent_depth: 2` (v2-native key; the v1 top-level `subagent_depth` is dropped by v2 and no longer written. Exactly covers the deepest chain in use, `orchestrator → light-orchestrator → oracle`; every other subagent is denied delegation, so a deeper level only widens the token-amplification surface)
- Session sharing: off (`share: "disabled"`)
- Permission baseline: `read` allowed by default, `shell` set to `ask` (unmatched commands go to a human; dangerous patterns are not enumerated); sensitive `.env`-type files `deny`; external directories `ask`; read-only agents get shell rules (deny all by default + allow read-only subcommands only)
- Context compression: **built-in compaction only** (opencode.jsonc) — `limit.input` declares the working window, and it fires at `limit.input − compaction.buffer` (`131072−16000=115,072` tokens), keeping a verbatim tail of `keep.tokens: 12000`; the v1 spellings (`prune`/`tail_turns`/`reserved`/`preserve_recent_tokens`) do nothing on v2 and were removed (the v1 `reserved` maps to v2 `buffer`); no third-party compression plugin
- Global rules: `AGENTS.md` (core principles, task rejection contract, self-verification, anti-patterns, etc.; context/token discipline in `AGENTS.md`)
- Skills: **23** `SKILL.md` skills under `skills/`, loaded on demand via the native `skill` tool; each agent then trims the roster with a `skill` permission allowlist (the roster's name+description is an **always-on** per-turn cost, so it is kept minimal by role)
- Commands: **18** shortcut commands (agent routing / operations / inline / spec), see below
- Plugins: `superpowers` only (git URL pinned to tag `#v7.0.1`, loaded via the v2-native `plugins` array) — pure skill injection with **no model-mapping capability**, so model routing happens only at the agent layer (the skill → agent → tier table lives in `AGENTS.md`'s Plugins section); the pin keeps the prefix byte-stable and prevents drift from auto-updates. Task sub-sessions receive no bootstrap injection, so delegation chains don't pay that always-on cost.

## DeepSeek Model Configuration

### Prerequisites

- OpenCode v2 (desktop app's bundled CLI **2.2.0**, verified 2026-10-27; 1.18.x and older are **no longer supported**)
- **Runtime caveat**: the `opencode` on `PATH` (WinGet install) is **1.18.4**, not this config's runtime — it takes the V1→V2 migration path. For headless runs use the desktop app's bundled CLI (path in the Install and Start ⚠️ note)
- DeepSeek API key: request one at [platform.deepseek.com/api_keys](https://platform.deepseek.com/api_keys)
- Background subagents (delegation with `background: true`) require the environment variable `OPENCODE_EXPERIMENTAL_BACKGROUND_SUBAGENTS=true` (PowerShell: `$env:OPENCODE_EXPERIMENTAL_BACKGROUND_SUBAGENTS="true"`); when unset, background delegation errors out and should fall back to foreground/serial dispatch

### Option 1: Interactive TUI Setup (Recommended)

```bash
opencode
# In TUI enter: /connect → select DeepSeek → paste API Key
# Then: /models → select deepseek-flash
```

The API key is automatically persisted to `~/.local/share/opencode/auth.json`.

### Option 2: Environment Variable

Windows PowerShell:
```powershell
$env:DEEPSEEK_API_KEY="sk-your-key-here"
opencode
```

Permanent setup: add `DEEPSEEK_API_KEY` to your system environment variables.

### Provider Configuration Reference

```jsonc
{
  "model": "deepseek/deepseek-flash"
}
```

This config keeps only `deepseek-flash` and splits thinking at the `providers` layer with **variants**: the default tier disables thinking and pins `temperature: 0` (fastest, cheapest), while `#low`/`#high` turn thinking on and set `settings.reasoningEffort` to `low`/`high`. `deepseek-flash` is the merged V4.1 Flash model — the former text-only and vision variants are now one natively multimodal model, so it declares `capabilities` (image input). Example (v2-native keys):

```jsonc
"providers": {
  "deepseek": {
    "models": {
      "deepseek-flash": {
        "body": {
          "thinking": { "type": "disabled" },
          "temperature": 0
        },
        "capabilities": {
          "tools": true,
          "input": ["text", "image"],
          "output": ["text"]
        },
        "variants": [
          { "id": "low",  "settings": { "reasoningEffort": "low" },  "body": { "thinking": { "type": "enabled" } } },
          { "id": "high", "settings": { "reasoningEffort": "high" }, "body": { "thinking": { "type": "enabled" } } }
        ]
      }
    }
  }
}
```

> `thinking`/`temperature` must live under `body`: `settings` carries provider options only (`timeout`/`chunkTimeout`/`reasoningEffort`, …), and keys placed there are silently dropped.

> **Model ID naming convention**: `provider_id/model_id`, with variants as a `#` suffix. This repo has exactly `deepseek/deepseek-flash`, `deepseek/deepseek-flash#low`, and `deepseek/deepseek-flash#high`.

### v2-native keys (the only dialect this repo writes)

This repo is an **OpenCode v2-only config** and writes only v2-native keys: `plugins` (not `plugin`), `providers`/`models` (not `provider`), `agents`/`commands` (not `agent`/`command`), `permissions` (ordered `{action, resource, effect}`, not `permission`; the action names are `shell`/`subagent`, not `bash`/`task`), `media.image` (not `attachment`), nesting depth as `experimental.subagent_depth`, and compaction as `{auto, keep, buffer}` (`prune`/`tail_turns`/`reserved` are v1 spellings v2 does not read). If any v1 trigger key appears (`provider`/`agent`/`command`/`plugin`/`permission`/`tools`/`attachment`/`small_model`, …), v2 takes the V1→V2 migration path instead and nothing described here holds.

> `$schema` stays `https://opencode.ai/config.json` (the URL the v2 app itself writes).

## Install and Start

```powershell
# 1. Clone
git clone https://github.com/znlgis/my-opencode-deepseek-config.git

# 2. Point opencode at this repo's config directory (current session)
$env:OPENCODE_CONFIG_DIR = "D:\path\to\my-opencode-deepseek-config\opencode"

# 3. API key (or run /connect in the TUI after launch)
$env:DEEPSEEK_API_KEY = "sk-your-key-here"

# 4. Start
opencode
```

Permanent (Windows):

```powershell
[Environment]::SetEnvironmentVariable("OPENCODE_CONFIG_DIR", "D:\path\to\my-opencode-deepseek-config\opencode", "User")
```

Linux / macOS: append `export OPENCODE_CONFIG_DIR="$HOME/path/to/my-opencode-deepseek-config/opencode"` to `~/.bashrc` or `~/.zshrc`.

**Alternative**: symlink `opencode/` into the global config path — on Windows (admin required) `New-Item -ItemType SymbolicLink -Path "$env:USERPROFILE\.config\opencode" -Target "<repo>\opencode"`, on Linux/macOS `ln -s <repo>/opencode ~/.config/opencode`.

Three self-checks after launch: `/models` shows `deepseek/deepseek-flash`; the agent list has all 12 custom agents; describe any task in plain language and `orchestrator` classifies and routes it (to skip routing, use `/quick`, `/deep`, `/review`, … directly).

> **⚠️ Must run on OpenCode V2.x**: the `opencode.exe` on `PATH` is the WinGet install of **v1.18.4**, while the runtime actually in use is the desktop app **v2.2.0** and its bundled CLI `"$env:LOCALAPPDATA\Programs\@opencode-aidesktop\resources\opencode-cli.exe"`. Launching under v1 takes the V1→V2 migration path, and none of the routing, permission, compaction or roster behavior described here holds; for headless runs use that CLI.

After editing repo files, sync them (`~/.config/opencode` is an independent copy, not a symlink):

```powershell
.\scripts\sync-config.ps1              # -Src overrides the source directory
```

The sync excludes `node_modules`/`.git`/package manifests and lockfiles; stale-file cleanup inside `skills/`, `agents/` and `commands/` is driven by the git index plus full history, so a `git rm`-staged deletion is removed from the global directory even before it is committed.

## Model Division of Labor

**Why `deepseek-flash` is the only model** (the config was consolidated right after opencode v2 went GA):

- **Everyday development is not bounded by the model's ceiling.** Reading code, editing files, running tests, writing docs, reviewing a diff — these want speed, reliability and low unit cost, not maximum reasoning. Paying pro rates for them buys no matching output.
- **Flash is where every vendor is concentrating investment.** Competition is clustered at this tier, so capability at a given price is still climbing fast; pinning the config to flash means riding the fastest-moving curve.
- **Pro's capability and price both overshoot daily work.** It serves a minority of hard problems, while a config pays for every ordinary call — keeping a pricier model resident for the occasional hard task is the wrong cost structure.
- **When more reasoning is needed, the same model supplies it via a thinking tier.** `#high` deepens reasoning through reasoning tokens: cost is incurred per task and grows linearly at the output rate, with no second price sheet and no second cache prefix.
- **The trade-off is stated, not hidden: ceiling in exchange for cost.** On extreme problems flash may need one more round or an escalation to `#high` instead of landing in one shot — and that cost falls on a minority of tasks rather than taxing every ordinary call.

This repo uses exactly **one model**, `deepseek/deepseek-flash`, and introduces no other model. Agents differ along a single dimension — the **thinking tier** — expressed as model variants:

| Tier | Model reference | Mechanism | Typical agents |
| --- | --- | --- | --- |
| Cheap (trivial) | `deepseek/deepseek-flash` | body `{"type":"disabled"}` + `temperature: 0` | `orchestrator`/`explore`/`librarian`/`consultant`/`ui-builder`/`vision` |
| Mid (routine) | `deepseek/deepseek-flash#low` | thinking on + `settings.reasoningEffort: low` | `planner`/`light-orchestrator` |
| Deep | `deepseek/deepseek-flash#high` | thinking on + `settings.reasoningEffort: high` | `deep-worker`/`oracle`/`reviewer`/`solo` |

Thinking strength is driven by two **request parameters**, both declared at the **model layer** in `opencode.jsonc`; agents only choose a model reference and write no thinking parameters at all:

- **thinking toggle** — `{"thinking":{"type":"enabled"|"disabled"}}`, a **request BODY field**. The default tier is off (cheapest); `#low`/`#high` turn it on.
- **thinking effort** — `{"reasoning_effort":"low"|"high"|"max"}`, driven by the **provider setting** `settings.reasoningEffort` (camelCase in config, snake_case on the wire) — the only path the aisdk layer turns into `reasoning_effort`.

> **Checked against the official guide (2026-10-27, `guides/thinking_mode`)**: ① thinking is **on by default with effort `high`** — so only the default tier (explicit `disabled`) actually saves reasoning tokens; `#high` merely spells out the default, and `#low` is the only middle rung that exists. There is no medium effort: `minimal`→low, `medium`/`xhigh`→high, `max`/`ultra`→max. ② Thinking mode **does not support** `temperature`/`presence_penalty`/`frequency_penalty` — passing them raises no error and has no effect, so the `temperature: 0` in the table binds only on the default tier while `#low`/`#high` inherit it inertly. ③ `top_p` is the opposite: on `deepseek-flash` it applies only with thinking on (valid range 0.95–1.0, values below are treated as 0.95), and is pinned to 1.0 with passed values ignored when thinking is off — so this config sets no `top_p`. ④ When a request carries `tools`, every historical `reasoning_content` must be sent back or the API returns 400; opencode forces the replay for all deepseek models (`requireReasoning` in `ai/protocols/openai-chat.ts`), so nothing extra is needed here.

> **⚠️ Spelling trap (measured 2026-10-16)**: `thinking`/`temperature` under `settings` are inert (the openai-chat protocol implements no such option), and `reasoningEffort` under an agent's `options` is inert too (`options` only reaches `request.body`, and camelCase is not the `reasoning_effort` the API reads). Both must live at the **model layer**: the toggle in `body`, the effort in `settings`. Before the fix flash ran with thinking on all along, paying reasoning tokens on every step.

**Cost structure**: all three tiers share flash's single price sheet (in 0.15 / out 0.60 / cache_read 0.003 USD per 1M, off-peak) — no model premium. A tier difference shows up only in the **number of reasoning tokens**, which bill at the output rate. "Escalating" therefore buys more output tokens, not a more expensive unit price — deep work no longer pays a 4.4× input premium.

> **Not yet verified**: `reasoningEffort: "high"` on flash has not been measured for actual reasoning depth or token delta (this machine's live service only has the `opencode-go` provider; `-m deepseek/*` returns `provider.no-route`). Re-check with one `/deep-review` run on a machine that has DeepSeek credentials.

### Routing Strategy

- **Trivial → thinking off**: well-defined light tasks — search, lookup, consultation, UI, exploration, doc retrieval — go to the default tier (cheapest)
- **Routine-nontrivial → `#low`**: planning and routine multi-file work use `deepseek/deepseek-flash#low` (thinking on + effort `low`)
- **Deep/uncertain → `#high`**: deep reasoning, root-cause analysis, heavy multi-file implementation use `deepseek/deepseek-flash#high` (thinking on + effort `high`)
- **Code review → thinking-off first pass, `#high` escalation**: `/review` runs a thinking-off first pass (Abbreviated path) by default and delegates to `reviewer` (`#high`) only when an escalation trigger fires; `/deep-review` forces a full `#high` review
- **Vision owns multimodal**: only when the user explicitly provides an image/screenshot or explicitly asks, route to the `vision` agent (`deepseek-flash`, natively multimodal). **Vision input is opt-in**: non-visual tasks never attach images, generate images, or invoke vision (enforced as a hard constraint in `AGENTS.md` → "Constraints"); attachments are first resized by `media.image` to 1600px / 2MiB to avoid wasting base64 bytes
- **Automatic escalation**: when a thinking-off or `#low` agent can't handle a task, it escalates to `#high` automatically (with full context)

**Model resolution chain (v2.0.24 source + live-verified; re-checked on v2.0.27 on 2026-10-12 with an environment caveat; source re-checked on v2.2.0 on 2026-10-27)**: `agent.model` decides the actual model in exactly three places — ① subagent/child sessions (including `/` commands: a command bound to a subagent-mode agent **spawns a child session** running at that agent's tier, e.g. `/deep`→`#high`, `/quick`→`#low`, and the current session model is untouched); ② the title/summary/compaction one-shot calls; ③ the desktop App's per-agent default for new sessions (build/plan/orchestrator → flash). The main session itself resolves `-m` > stored session model > global `model` (flash, default tier): in the desktop App you switch via the model picker (your pick is remembered); headless `opencode run` **does not read** `agent.model` — the main session resolves the global `model`; pass `-m deepseek/deepseek-flash#high` to pin a thinking tier. **⚠️ 2026-10-12 re-test (v2.0.27)**: on this machine (whose live service only has the `opencode-go` provider) `-m deepseek/*` returns `provider.no-route`, and only the run without `-m` succeeds — so the `-m` semantics depend on the target machine having `/connect deepseek` with a working provider route; re-verify there before relying on it.

Usage examples: "how does this library work?" → thinking off (librarian); "add an export feature to the user module" → `#low` (planner); "what's the root cause of this login error?" → `#high` (oracle); `/review` → thinking-off first pass (small diffs report directly); `/review #123` (large diff / trust boundary) → first pass then `#high` escalation.

#### Two-Tier Code Review (Lightweight Review)

| Tier | Model | Executor | Coverage |
| --- | --- | --- | --- |
| Tier 1 (default) | `deepseek/deepseek-flash` (thinking off) | `light-orchestrator` (`/review`) | Abbreviated path: ≤8 logic files and ≤300 effective lines, no high-stakes trigger |
| Tier 2 (escalation) | `deepseek/deepseek-flash#high` | `reviewer` (`/deep-review` or auto-escalation) | Full path, a high-stakes trigger, or a Tier-1 critical/major needing cross-file confirmation |

Escalation triggers (any one): the path is Full (large diff or high-stakes regex hit); Tier 1 found a critical/major it cannot confirm from the diff alone; the diff touches a trust boundary (also load `security-review`); the user explicitly asked for a deep review.

A Tier-1 report is a **complete review**, not a preview — a clean result is never escalated merely to double-check. On escalation, Tier-1 findings are passed as **unverified leads** so the `#high` tier confirms rather than re-derives them, avoiding duplicate token spend.

### Cost Comparison

Prices are taken from `providers.deepseek.models` in `opencode.jsonc` (USD per 1M tokens; **re-verified item-by-item against the official pricing page on 2026-10-27**, off-peak rates). Peak hours are exactly double: 01:00–04:00 and 06:00–10:00 UTC Mon–Fri, excluding Chinese public holidays (= 09:00–12:00 and 14:00–18:00 Beijing); all other hours, weekends and Chinese public holidays are off-peak. `cache_write` has no standalone official price and is mapped from the cache-miss input rate:

| Model | Input | Output | Cache hit (cache_read) | Cache write (cache_write) |
| --- | --- | --- | --- | --- |
| `deepseek-flash` | 0.15 | 0.60 | 0.003 | 0.15 |

Two cost levers:

- **Thinking tier**: all three tiers bill at the same unit price; the only difference is the number of reasoning tokens (billed at the output rate) — escalating spends more output tokens, not a higher unit price.
- **Prompt cache**: `cache_read` is **50×** cheaper than the input price (0.003 vs 0.15). This config's byte-stable prefix + volatile-zone discipline (see `AGENTS.md`) exists precisely to maximize the cache-hit rate.

**Typical session cost estimate** (assume 200K input tokens, 150K cache hits, 30K output):

| Tier | Cache hit | Input miss | Output | Total |
| --- | --- | --- | --- | --- |
| thinking off | 150K × 0.003 = $0.0005 | 50K × 0.15 = $0.0075 | 30K × 0.60 = $0.018 | **≈ $0.026** |

`#low` / `#high` add only the extra reasoning tokens on top of the same visible token volume, billed linearly at the output rate. Use `scripts/estimate-cost.js` to estimate from actual token counts.

#### Thinking-Tier Cost Comparison

Reviewing a 300-effective-line local diff (assume 60K input tokens, 45K cache hits, 8K visible output; `#high` adds 4K reasoning tokens billed at the output rate):

| Tier | Cache hit | Input miss | Output (incl. reasoning) | Total |
| --- | --- | --- | --- | --- |
| thinking off (`/review` first pass) | 45K × 0.003 = $0.0001 | 15K × 0.15 = $0.0023 | 8K × 0.60 = $0.0048 | **≈ $0.007** |
| `#high` (`/deep-review`) | 45K × 0.003 = $0.0001 | 15K × 0.15 = $0.0023 | 12K × 0.60 = $0.0072 | **≈ $0.010** |

**Conclusion**: with one model, the gap between the two review tiers is not a "model unit price" but the extra reasoning tokens — on the same input base `#high` costs about 40% more ($0.007 → $0.010), far below the 3.7× pro premium of the two-model era. The reasoning-token count is an assumption; re-measure with `scripts/estimate-cost.js` on a machine holding DeepSeek credentials.

## Agent Structure

### Primary Agent

| Agent | Model | Role |
| --- | --- | --- |
| `orchestrator` | flash | Default entry point: intent gate + model-aware routing + fallback chains |
| `solo` | flash#high | Single-model inline executor: zero delegation, no background helpers, all work done inline on flash's `#high` tier in the current session |

> `solo` is the second primary agent: `{action: subagent, resource: "*", effect: deny}` (zero delegation — spawns no subagents), an explicit `model: deepseek/deepseek-flash#high` (always the strongest thinking tier), and no build/plan built-in helpers (their configured tier binds only child sessions and one-shot calls — the main session never follows it, so mixing them would break the single-model guarantee). Analysis, planning, implementation, and verification all happen inline in the current session.

### Subagents

| Agent | Model | Permission | Role |
| --- | --- | --- | --- |
| `planner` | flash#low | read-write | Planning, architecture, task breakdown |
| `deep-worker` | flash#high | read-write | Heavy implementation, multi-file changes, complex debugging |
| `oracle` | flash#high | **read-only** | Root-cause analysis, deep code understanding |
| `reviewer` | flash#high | **read-only** | Review escalation tier: Full path / high-stakes trigger / confirming cheap-tier first-pass leads |
| `ui-builder` | flash | read-write | Frontend and UI tasks |
| `consultant` | flash | read-write | Approach discussions, best-practice advice |
| `explore` | flash | **read-only** | Codebase search, parallel exploration |
| `librarian` | flash | **read-only** | Documentation lookup, web search |
| `light-orchestrator` | flash#low | read-write | Lightweight tasks, single-file edits |
| `vision` | flash | read-write | Multimodal: images/screenshots/charts/UI mockups |

> `deep-worker` and `light-orchestrator` follow a "no research, no delegation" principle — they execute, not explore; context is provided by the orchestrator; the sole exception is `light-orchestrator`'s two escalation paths — `oracle` (read-only analysis) and `reviewer` (`#high` review), serving `/simplify` and `/review`. `deep-worker` also carries a "What you DON'T handle" rejection contract: trivial single-file edits → refuse (route to `light-orchestrator`), pure research/lookups → refuse (route to `oracle`/`explore`), anything a cheaper tier can finish → refuse (this tier pays reasoning tokens on every step).
>
> Read-only agents (`oracle`/`reviewer`/`explore`) are truly read-only: `edit: deny` + a shell allowlist (deny all by default, allow only read-only subcommands such as `git status/diff/log/show/blame/grep` and `rg`; `oracle`/`reviewer` additionally allow `gh pr view/diff`, `gh issue view`, and `gh api` to support `/review` replies). `librarian` is stricter: the `shell` ruleset denies everything by default, with no shell allowlist at all.
>
> Each agent carries a `skills` allowlist (deny by default + allow by role, to prevent loading heavyweight skills): `orchestrator` → `codemap`/`grilling`; `planner` → `spec-workflow`/`codebase-design`/`writing-plans`; `deep-worker` → `remove-deadcode`/`spec-workflow`/`git-release`/`to-tickets`/`triage`/`git-master`/`resolving-merge-conflicts`/`opencode-config`/`writing-for-agents`/`diagnosing-bugs`/`codebase-design`/`domain-modeling`/`test-driven-development`/`verification-before-completion`; `oracle` → `reflect`/`simplify`/`diagnosing-bugs`; `reviewer` → `code-review`/`security-review`/`gh-cli`; `explore` → `codemap`; `librarian` → `verify-with-docs`; `light-orchestrator` → `handoff`/`simplify`/`spec-workflow`/`code-review`/`gh-cli`/`verification-before-completion`; `consultant` → `domain-modeling`; `ui-builder` → `codebase-design`; `vision` → `vision-prep`; `solo` → the full local skill set plus `brainstorming`/`test-driven-development`/`verification-before-completion`/`writing-plans`/`executing-plans` (the inline executor needs the complete toolchain, still guarded by a `resource: "*"` deny rule). The allowlist is not just permissions — **a denied skill never enters that agent's skill roster**, so the list is simultaneously its always-loaded context budget. Only 4 superpowers entries are wired (`writing-plans` → `planner`; `test-driven-development`/`verification-before-completion` → `deep-worker`; `verification-before-completion` → `light-orchestrator`); every other superpowers skill has a local equivalent and stays unwired so two competing processes never coexist — the mapping table is in `AGENTS.md`'s Plugins section.
>
> **Thinking tiers**: all declared at the **model layer** in `opencode.jsonc`; agents only choose a model reference (see "Model Division of Labor"). `explore`/`librarian`/`consultant`/`ui-builder`/`orchestrator`/`vision` = flash · thinking off (cheapest, model-level body `{"type":"disabled"}`); `planner`/`light-orchestrator` = `deepseek/deepseek-flash#low` (thinking on + `settings.reasoningEffort: low`); `deep-worker`/`oracle`/`reviewer`/`solo` = `deepseek/deepseek-flash#high` (thinking on + `settings.reasoningEffort: high`). **No thinking parameter ever appears in agent frontmatter** — `options.*` cannot hold a provider setting and additionally pushes that agent back onto the V1 migration path (see the spelling-trap note under "Model Division of Labor").

## Quick Commands

### Agent Routing Commands

| Command | Agent | Purpose |
| --- | --- | --- |
| `/deep` | `deep-worker` | Heavy implementation, multi-file changes |
| `/quick` | `light-orchestrator` | Lightweight tasks, single-file edits |
| `/ui` | `ui-builder` | Frontend/UI work |
| `/vision` | `vision` | Multimodal: image/screenshot/chart understanding |
| `/review` | `light-orchestrator` (code-review) → escalates to `reviewer` as needed | Code review: thinking-off first pass by default (Abbreviated path reports directly); delegates to `reviewer` (`#high`) with unverified leads when an escalation trigger fires; posts to GitHub (event=COMMENT) with a PR ref/URL |
| `/deep-review` | `reviewer` (code-review + gh-cli) | Forces a full `#high` review, skipping the cheap first pass; posts to GitHub (event=COMMENT) with a PR ref/URL |
| `/plan` | `planner` | Create plans and technical proposals |
| `/oracle` | `oracle` | Deep analysis, root-cause tracing |

### Operation Commands

| Command | Agent | Purpose |
| --- | --- | --- |
| `/commit` | `light-orchestrator` | Generate Conventional Commits messages (inline format) |
| `/release` | `deep-worker` (git-release) | Prepare a tagged release |
| `/reflect` | `oracle` (reflect) | Surface friction → propose config improvements |
| `/handoff` | `light-orchestrator` (handoff) | Compress the session into a handoff document |

### Inline Commands

| Command | Agent | Purpose |
| --- | --- | --- |
| `/codemap` | `explore` (codemap) | Generate a repository structure map |
| `/learn` | `deep-worker` | Distill non-obvious session learnings into directory-level AGENTS.md files (root/package/feature) |
| `/simplify` | `light-orchestrator` (simplify) → spawns `oracle` | spawns read-only oracle → light-orchestrator applies the edits |
| `/rmslop` | `deep-worker` (remove-deadcode) | Clean up dead code and AI slop |

### Spec Commands

| Command | Agent | Purpose |
| --- | --- | --- |
| `/spec-propose` | `planner` (spec-workflow) | Explore the code → draft a change proposal |
| `/spec-apply` | `deep-worker` (spec-workflow) | Implement item by item per tasks.md → auto-archive |

## Skills

OpenCode exposes skills on demand via the native `skill` tool — agents load them only when needed, so the **bodies** never occupy context. The roster of names + descriptions, however, is always on: see the note after the table.

| Skill | Purpose |
| --- | --- |
| `code-review` | Single-pass code review + evidence gating; two tiers — thinking-off first pass / `#high` escalation; large diffs (>~500 lines) split into Standards/Spec two axes merged into one report |
| `codemap` | Generates an annotated repository structure map for quick orientation, saving exploration tokens |
| `gh-cli` | GitHub CLI v2.103+ reference: PR posting, api, rate limits, gh pr checks, gh skill/gh-aw, GHSA security notes |
| `git-master` | Advanced Git operations: rebase, squash, fixup, bisect, reflog, code archaeology, worktrees |
| `git-release` | Tagged releases: release notes, SemVer inference, gh release commands |
| `resolving-merge-conflicts` | Resolve merge conflicts hunk by hunk: trace original intent, never invent new behavior, never --abort |
| `handoff` | Compresses a session into a handoff document (path references, no copied content) |
| `opencode-config` | Writes and maintains OpenCode config in this repo (agents/skills/commands/permissions) |
| `office-docs` | Read/write Word (.docx) and Excel (.xlsx) by converting to/from plain text or Markdown; pure Python, no MS Office or MCP required |
| `reflect` | Continuous improvement: surface friction → propose minimal, maintainable fixes |
| `remove-deadcode` | Safely finds and deletes dead code, verified via toolchain/LSP before removal |
| `security-review` | Pre-merge security review (injection/XSS/SSRF/secrets/deserialization/path traversal); reports, never auto-fixes |
| `simplify` | Behavior-preserving code simplification (oracle analyzes → applied) |
| `spec-workflow` | Lightweight spec-driven change: proposal → delta specs → tasks → update three-question decision tree → verify → archive |
| `verify-with-docs` | Verifies API docs before coding — retrieval-first, hallucination-proof |
| `vision-prep` | Preprocesses large images and PDFs before a vision model: tiles oversized images, rasterizes PDF pages (DeepSeek vision downscales to ~800x800 and rejects PDF input) |
| `grilling` | Requirements-alignment interview: one question at a time, multiple choice preferred, until intent is clear; also carries the "message didn't land → restate in one sentence" branch and the "fuzzy domain language → pair with `domain-modeling`" branch (absorbs the former `wait-what`/`grill-with-docs`) |
| `writing-for-agents` | Writing leverage for agent-facing docs (skills/AGENTS.md/pointer docs) |
| `to-tickets` | Breaks a spec/plan into trackable GitHub issues (one independently completable, verifiable unit per issue, with acceptance criteria) |
| `triage` | Label-based issue triage: pull → classify → apply labels/assignees (gh); routing only, never edits content |
| `diagnosing-bugs` | Systematic debugging: build a tight red-capable feedback loop BEFORE theorizing → reproduce + minimise → 3-5 falsifiable hypotheses → instrument one variable at a time (`[DEBUG-<hex>]` tagged) → fix at the correct seam + regression test → clean up |
| `codebase-design` | Architecture vocabulary: module/interface/depth/seam/adapter/leverage/locality, deletion test, depth test — assess whether module boundaries are sound |
| `domain-modeling` | Active domain modeling: maintain a CONTEXT.md glossary (vocabulary only, no implementation details), challenge/sharpen fuzzy terms during sessions, offer ADRs only when warranted; includes the repeated-explanation trigger (pin a term when the same concept keeps being re-explained) |

> **The roster is the budget**: every skill's name + description in the `skill` tool's `<available_skills>` list is **always-on** per-turn context. Measured 2026-10-16 (everything allowed at the time): **30 entries** (23 local + 7 wired superpowers skills), rendered roster block **12,186 chars / 11,927 bytes ≈ 3.3K tokens** — while `orchestrator`, which allows only 2 skills, carries just 804 chars ≈ 223 tokens, a **15×** difference. Since 2026-10-20 `solo` drops 2 wired entries that duplicate local skills (`systematic-debugging` → `diagnosing-bugs`, `writing-skills` → `writing-for-agents`), bringing its roster to 28 entries; no agent allows all 30 any more. So a skill that is no longer used often, or duplicates another, should be merged or deleted rather than kept "just in case"; over-long `description` fields should be trimmed to the trigger words that are actually needed. **A denied skill does not appear in that agent's roster at all** (core filters the roster with the `skill` permission ruleset), so each agent's allowlist *is* its always-on context budget. **The hidden one-shot agents (title/summary/compaction) carry an explicit `skill:* deny` — they never load a skill, so the roster was pure waste on them** .

## Repository Structure

```text
├── opencode/          # OpenCode config directory (agents/, skills/, opencode.jsonc, AGENTS.md)
├── scripts/           # sync-config.ps1 (sync to global config)
│                      # validate-jsonc.js (JSONC validation)
│                      # estimate-cost.js (cost estimate from token counts)
├── README.md          # Simplified Chinese (default)
├── README.en-US.md    # English
└── LICENSE
```

## Configuration Change Points

When editing config, locate "what to change → which file", so you never change the wrong layer:

| What you want to change | File | Location |
| --- | --- | --- |
| Model list / prices / thinking / temperature | `opencode.jsonc` | `providers.deepseek.models` |
| Global profile: default agent, nesting depth, tool-output caps, compaction, media resize | `opencode.jsonc` | top-level keys |
| Permissions (read / shell / skill / external directories) | `opencode.jsonc` | `permissions` |
| Built-in agents (build/plan/title/summary/compaction/general) model, and the one-shot agents' skill denial | `opencode.jsonc` | `agents` (inline `model` / `permissions`; inline permissions are **appended last** and win under last-match-wins ) |
| Shortcut command agents and templates | `opencode.jsonc` | `commands` |
| One agent's model, thinking tier, tool and skill allowlists, rejection contract | `opencode/agents/<name>.md` | frontmatter + body |
| Global behavior rules (principles, failure discipline, cache discipline, anti-patterns) | `opencode/AGENTS.md` | the matching section |
| Plugin versions (pins) | `opencode/opencode.jsonc` | `plugins` |
| Compression trigger window (per model) / buffer / preserved tail | `opencode/opencode.jsonc` | `providers.deepseek.models.<id>.limit.input` + `compaction` (`buffer`/`keep`) |
| A skill's behavior and triggers | `opencode/skills/<name>/SKILL.md` | frontmatter `description` + body |

> After editing, run `.\scripts\sync-config.ps1` to sync into `~/.config/opencode`, then reload (desktop v2: `opencode-cli.exe reload`; or restart opencode). Validate with `node scripts/validate-jsonc.js`; inspect the resolved result with `opencode-cli.exe debug config`.

## Usage Guide

Orchestrator routes automatically and picks the tier by task difficulty:

```text
"Help me debug the login API error"   → oracle analyzes root cause (flash#high)
"Optimize this loop, performance is poor" → oracle analyzes → deep-worker implements (flash#high)
"Review this PR for me"               → light-orchestrator pre-screen (reviewer#high only on a trigger)
"I want to add an export feature to the user module" → planner drafts → deep-worker implements (flash#low → flash#high)
"How to use React 19's use() API"     → librarian checks docs (thinking off)
```

For direct access: `/deep` (complex implementation), `/quick` (single file), `/plan` (proposal), `/oracle` (debugging), `/review` (code review), `/ui`, `/vision`; the full list is under "Quick Commands".

**When to escalate to `#high`**: touches ≥3 modules, a decision needs 5+ files read, a bug resists one attempt, or real design trade-offs. Single file, an error message that already points at the line, mechanical replacement, or an existing pattern to copy — don't escalate. Let a cheaper tier take one pass; escalate only when you can state the reason.

## Sources

The core ideas draw on [oh-my-openagent](https://github.com/code-yeongyu/oh-my-openagent) (intent gating, read-only isolation, anti-patterns), [oh-my-opencode-slim](https://github.com/alvinunreal/oh-my-opencode-slim) (dispatcher-first, fallback chains, rejection contract, prompt-cache safety), [anomalyco/opencode](https://github.com/anomalyco/opencode) (config schema, skill system), [cli/cli](https://github.com/cli/cli) (gh v2.103 command set), [OpenSpec](https://github.com/Fission-AI/OpenSpec) (delta specs), [mattpocock/skills](https://github.com/mattpocock/skills) (conflict resolution, handoff documents, debugging/architecture/domain-modeling skills), [pi](https://github.com/earendil-works/pi) (answer first then act, terse responses), and [deepreview](https://github.com/mechanai/deepreview) (effective-size routing). Pure config, zero extra dependencies. **Borrow, don't copy**: take only lightweight design ideas, simplify before adding.

## Design Philosophy

- **Pure config-driven, zero extra dependencies** — every capability comes from `opencode.jsonc` + `agents/*.md` + `skills/*/SKILL.md` + `AGENTS.md`
- **One model + thinking tiers** — `deepseek-flash` only: thinking off for routing and routine execution, `#low` for planning, `#high` for deep reasoning and heavy implementation; no second model's price sheet resident for an occasional hard task
- **Token efficiency first** — path references instead of pasted files, skills loaded on demand; each agent's `skill` allowlist also sets its roster size, and a denied skill never enters the always-loaded context
- **Plugins add value without stealing the spotlight** — the only plugin, superpowers, provides process discipline only (pinned to keep the prefix byte-stable); compression is 100% built-in compaction
- **Execution separated from exploration** — deep-worker/light-orchestrator must not research or delegate; explore/librarian must not modify
- **Cache + thinking discipline** — stable static prefixes to hit the prompt cache, with volatile content (timestamps, random IDs, dynamic lists) at the payload tail; the default tier disables thinking + `temperature: 0`, `#low`/`#high` turn it on
- **Scope First + Delegate Always** — scope first (2+ steps / multi-file / architecture changes go through planner), then delegate; top-level tokens are reserved for routing and hard problems
- **Explicit compression window** — `limit.input` declares the working window: `131072−16000=115,072`; the trigger point is computed, not inherited
- **Vision is opt-in** — images enter the payload only when the user supplies them; `media.image` resizes oversized images before upload
