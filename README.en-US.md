# My OpenCode × DeepSeek Config

[简体中文](README.md) | **English**

**OpenCode × DeepSeek Optimal Config** — a configuration scheme that pushes the DeepSeek V4 model family (Pro + Flash) to its full potential within OpenCode's multi-agent framework. Core philosophy: **token efficiency first — the best development results at the lowest context cost**.

## Current Configuration Overview

- Default primary agent: `orchestrator`
- Primary model: `deepseek/deepseek-v4-pro`; lightweight/multimodal model: `deepseek/deepseek-flash` (V4.1 Flash, natively multimodal)
- Agent nesting: `subagent_depth: 3` (supports 3 levels of subagent nesting)
- Session sharing: off (`share: "disabled"`)
- Permission baseline: allow by default, destructive bash commands set to `ask`; sensitive `.env`-type files `deny`; external directories `ask`; read-only agents get a bash allowlist (deny all by default + allow read-only subcommands only)
- Context compression: built-in compaction (opencode.jsonc) handles auto-triggering + pruning of stale tool output; DCP (dcp.jsonc) handles proactive dedup + compression thresholds — the two complement each other
- Global rules: `AGENTS.md` (core principles, task rejection contract, self-verification, anti-patterns, etc.; context/token discipline in `AGENTS.md`)
- Skills: **25** `SKILL.md` skills under `skills/`, loaded on demand via the native `skill` tool
- Plugins: `superpowers` (git URL pinned to tag `#v6.3.0`, process skills), `@tarquinen/opencode-dcp` (pinned to `@3.1.15`, intelligent context pruning); both are version-pinned to keep the prefix byte-stable and prevent prefix drift from auto-updates

### Plugins and Model Mapping (Important)

Neither plugin exposes a model-mapping capability, so model routing can only happen at the agent layer — which is exactly how this config implements it. There is no way (and no need) to assign models inside the plugins:

- **DCP 3.1.15**: `PluginConfig` exposes only `modelMaxLimits` / `modelMinLimits` (per-model compression thresholds); there is **no** per-step model-assignment field. This config already sets an earlier compression threshold for pro (`dcp.jsonc`) — the only model-related tuning DCP allows.
- **superpowers v6.3.0**: a pure skill-injection plugin (`.opencode/plugins/superpowers.js`) with no model configuration surface.

So the split "planning/architecture/complex review on pro; execution/first-pass/doc/batch/vision on flash" is implemented entirely through the `model:` field and thinking tiers in `agents/*.md` (see the routing strategy below), not through plugin config. This conclusion was verified against the plugin source — do not re-investigate.

## DeepSeek Model Configuration

### Prerequisites

- OpenCode ≥ v1.18.x (the DeepSeek provider is built in)
- DeepSeek API key: request one at [platform.deepseek.com/api_keys](https://platform.deepseek.com/api_keys)
- Background subagents (delegation with `background: true`) require the environment variable `OPENCODE_EXPERIMENTAL_BACKGROUND_SUBAGENTS=true` (PowerShell: `$env:OPENCODE_EXPERIMENTAL_BACKGROUND_SUBAGENTS="true"`); when unset, background delegation errors out and should fall back to foreground/serial dispatch

### Option 1: Interactive TUI Setup (Recommended)

```bash
opencode
# In TUI enter: /connect → select DeepSeek → paste API Key
# Then: /models → select deepseek-v4-pro
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
  "model": "deepseek/deepseek-v4-pro",
  "small_model": "deepseek/deepseek-flash"
}
```

This config splits thinking at the `provider` layer: flash disables thinking and pins `temperature: 0` (fastest, cheapest), while pro keeps the default (thinking on). `deepseek-flash` is the merged V4.1 Flash model — the former text-only and vision variants are now one natively multimodal model, so it declares `modalities` (image input). Example (flash):

```jsonc
"provider": {
  "deepseek": {
    "models": {
      "deepseek-flash": {
        "modalities": {
          "input": ["text", "image"],
          "output": ["text"]
        },
        "options": {
          "temperature": 0,
          "thinking": { "type": "disabled" }
        }
      }
    }
  }
}
```

> **Model ID naming convention**: `provider_id/model_id` — i.e. `deepseek/deepseek-v4-pro` and `deepseek/deepseek-flash`.

## Installation

### Option 1: Clone + Environment Variable (Recommended, Cross-Platform)

```bash
git clone https://github.com/znlgis/my-opencode-deepseek-config.git
```

Then point `OPENCODE_CONFIG_DIR` at the `opencode/` subdirectory in the repo and you're ready to go.

**Windows (PowerShell)** — permanent:

```powershell
[Environment]::SetEnvironmentVariable("OPENCODE_CONFIG_DIR", "D:\path\to\my-opencode-deepseek-config\opencode", "User")
```

**Windows (PowerShell)** — temporary (current session only):

```powershell
$env:OPENCODE_CONFIG_DIR = "D:\path\to\my-opencode-deepseek-config\opencode"
opencode
```

**Linux / macOS** — append to `~/.bashrc` or `~/.zshrc`:

```bash
export OPENCODE_CONFIG_DIR="$HOME/path/to/my-opencode-deepseek-config/opencode"
```

### Option 2: Symlink to the Global Config Directory

**Windows (PowerShell, admin required):**

```powershell
New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\.config"
New-Item -ItemType SymbolicLink -Path "$env:USERPROFILE\.config\opencode" -Target "D:\path\to\my-opencode-deepseek-config\opencode"
```

**Linux / macOS:**

```bash
ln -s /path/to/my-opencode-deepseek-config/opencode ~/.config/opencode
```

> **Compatibility note**: `~/.config/opencode` is OpenCode's standard global config path. The `opencode/` subdirectory in this repo contains `agents/`, `skills/`, `AGENTS.md`, and more, and follows OpenCode's layout conventions exactly — point to it via environment variable or symlink and it is picked up automatically.

### Verify the Installation

Launch OpenCode and confirm:
1. `/models` → the current model is `deepseek/deepseek-v4-pro`
2. The agent list shows all 12 agents, including `orchestrator`, `planner`, and `deep-worker`
3. Send any request — the Orchestrator analyzes intent and routes automatically

### Sync

`~/.config/opencode` is an independent copy (not a symlink) — the repo is the source of truth. After editing the repo, sync manually for changes to take effect. On Windows:

```powershell
.\scripts\sync-config.ps1
```

This copies the config files under `opencode/` into `~/.config/opencode/` (excluding `node_modules`, `package.json`, and `package-lock.json`). Pass `-Src` to specify a custom source directory for use on other machines:

```powershell
.\scripts\sync-config.ps1 -Src "D:\path\to\my-opencode-deepseek-config\opencode"
```

## Model Division of Labor

This repo strictly divides work within the DeepSeek V4 model family — no other models are introduced. The division is **2 models × per-agent thinking strength (thinking tier)**, not a multi-model matrix:

| Model | Purpose |
| --- | --- |
| `deepseek/deepseek-v4-pro` | Deep reasoning, root-cause analysis, code review, heavy multi-file implementation |
| `deepseek/deepseek-flash` | Orchestration/routing, planning, routine implementation, consultation, UI, exploration, external lookup, light edits, title/summary/compaction; natively multimodal (images/screenshots/charts/UI mockups) |

Thinking strength is controlled by **`reasoning_effort`** — a **request-level** thinking-strength control (`low`/`high`/`max`), **not a model id** — set per-agent via frontmatter `options` (camelCase `reasoningEffort`, deep-merged over `model.options`), never by changing `model:`. The 2-model matrix stays intact; the same model can run at different thinking strengths:

| Tier | Model × thinking | Typical agents |
| --- | --- | --- |
| Trivial | flash · thinking off | `explore`/`librarian`/`consultant`/`ui-builder`/`orchestrator` |
| Routine | flash · thinking on + `reasoningEffort: low` | `planner`/`light-orchestrator` |
| Deep | pro · default high | `deep-worker`/`oracle`/`reviewer` |
| Session | session's selected model (pro by default) | `solo` (no `model` field; thinking tier follows that model) |

Cost ratio: pro input price is 3× flash (0.66 vs 0.22 per 1M tokens), so trivial work never lands on pro.

### Routing Strategy

- **Trivial → flash off**: well-defined light tasks — search, lookup, consultation, UI, exploration, doc retrieval — go to flash agents with thinking off (cheapest)
- **Routine-nontrivial → flash low**: planning and routine multi-file work use flash + `reasoningEffort: low`
- **Deep/uncertain → pro high**: deep reasoning, root-cause analysis, code review, heavy multi-file implementation — pro only
- **Vision owns multimodal**: when visual input (images, screenshots, charts) is detected, route to the `vision` agent (`deepseek-flash`, natively multimodal)
- **Automatic escalation**: when a flash agent can't handle a task, it escalates to pro automatically (with full context)

Usage examples: "how does this library work?" → flash off (librarian); "add an export feature to the user module" → flash low (planner); "what's the root cause of this login error?" → pro high (oracle).

### Cost Comparison

Prices are taken from `provider.deepseek.models` in `opencode.jsonc` (USD per 1M tokens, off-peak rates effective 2026-08-16; peak hours double). `cache_write` has no standalone official price and is mapped from the cache-miss input rate:

| Model | Input | Output | Cache hit (cache_read) | Cache write (cache_write) | Input price vs flash |
| --- | --- | --- | --- | --- | --- |
| `deepseek-flash` | 0.22 | 0.66 | 0.007 | 0.22 | 1× |
| `deepseek-v4-pro` | 0.66 | 1.98 | 0.022 | 0.66 | 3× |

Two cost levers:

- **Model tier**: pro's input/output prices are 3× flash, so trivial work never lands on pro (see the routing strategy above).
- **Prompt cache**: `cache_read` is ~**30×** cheaper than the input price (flash 0.007 vs 0.22; pro 0.022 vs 0.66). This config's byte-stable prefix + volatile-zone discipline (see `AGENTS.md`) exists precisely to maximize the cache-hit rate.

**Typical session cost estimate** (assume 200K input tokens, 150K cache hits, 30K output):

| Model | Cache hit | Input miss | Output | Total |
| --- | --- | --- | --- | --- |
| flash | 150K × 0.007 = $0.001 | 50K × 0.22 = $0.011 | 30K × 0.66 = $0.020 | **≈ $0.032** |
| pro | 150K × 0.022 = $0.003 | 50K × 0.66 = $0.033 | 30K × 1.98 = $0.059 | **≈ $0.096** |

At the same token volume, pro ≈ 3× flash. Use `scripts/estimate-cost.js` to estimate from actual token counts.

## Agent Structure

### Primary Agent

| Agent | Model | Role |
| --- | --- | --- |
| `orchestrator` | flash | Default entry point: intent gate + model-aware routing + fallback chains |
| `solo` | v4-pro (default) | Single-model inline executor: zero delegation, no background helpers, all work done inline in the current session |

> `solo` is the second primary agent: `permission.task: "*": "deny"` (zero delegation — spawns no subagents), no explicit `model` field (follows the session's default model, pro), and no build/plan background helpers (they run on built-in flash and would break the single-model guarantee). Analysis, planning, implementation, and verification all happen inline in the current session.

### Subagents

| Agent | Model | Permission | Role |
| --- | --- | --- | --- |
| `planner` | flash | read-write | Planning, architecture, task breakdown |
| `deep-worker` | v4-pro | read-write | Heavy implementation, multi-file changes, complex debugging |
| `oracle` | v4-pro | **read-only** | Root-cause analysis, deep code understanding |
| `reviewer` | v4-pro | **read-only** | Single-pass code review (evidence-gated) |
| `ui-builder` | flash | read-write | Frontend and UI tasks |
| `consultant` | flash | read-write | Approach discussions, best-practice advice |
| `explore` | flash | **read-only** | Codebase search, parallel exploration |
| `librarian` | flash | **read-only** | Documentation lookup, web search |
| `light-orchestrator` | flash | read-write | Lightweight tasks, single-file edits |
| `vision` | flash | read-write | Multimodal: images/screenshots/charts/UI mockups |

> `deep-worker` and `light-orchestrator` follow a "no research, no delegation" principle — they execute, not explore; context is provided by the orchestrator. `deep-worker` also carries a "What you DON'T handle" rejection contract: trivial single-file edits → refuse (route to `light-orchestrator`), pure research/lookups → refuse (route to `oracle`/`explore`), anything a flash agent can finish → refuse (pro is 3× flash).
>
> Read-only agents (`oracle`/`reviewer`/`explore`) are truly read-only: `edit: deny` + a bash allowlist (deny all by default, allow only read-only subcommands such as `git status/diff/log/show/blame/grep` and `rg`; `oracle`/`reviewer` additionally allow `gh pr view/diff`, `gh issue view`, and `gh api` to support `/review` replies). `librarian` is stricter: `bash: "*": deny`, no bash allowlist at all.
>
> Each agent carries a `skills` allowlist (deny by default + allow by role, to prevent loading heavyweight skills): `orchestrator` → `codemap`/`grilling`/`wait-what`/`grill-with-docs`; `planner` → `spec-workflow`/`codebase-design`; `deep-worker` → `remove-deadcode`/`spec-workflow`/`git-release`/`to-tickets`/`triage`/`git-master`/`resolving-merge-conflicts`/`opencode-config`/`writing-for-agents`/`diagnosing-bugs`/`codebase-design`/`domain-modeling`; `oracle` → `reflect`/`simplify`/`diagnosing-bugs`; `reviewer` → `code-review`/`security-review`/`gh-cli`; `explore` → `codemap`; `librarian` → `verify-with-docs`; `light-orchestrator` → `handoff`/`simplify`/`spec-workflow`; `consultant` → `domain-modeling`; `ui-builder` → `codebase-design`; `vision` → `vision-prep`; `solo` → the full local skill set (the inline executor needs the complete toolchain, still guarded by `"*": deny`).
>
> **Thinking tiers**: `reasoning_effort` is a request-level thinking-strength control (`low`/`high`/`max`) set per-agent via frontmatter `options` — not a model id. `explore`/`librarian`/`consultant`/`ui-builder`/`orchestrator` = flash · thinking off (cheapest); `planner`/`light-orchestrator` = flash · thinking on + `reasoningEffort: low`; `deep-worker`/`oracle`/`reviewer` = pro · default high; `solo` has no `model` field and follows the session's selected model (pro by default), so its thinking tier follows that model.

## Quick Commands

### Agent Routing Commands

| Command | Agent | Purpose |
| --- | --- | --- |
| `/deep` | `deep-worker` | Heavy implementation, multi-file changes |
| `/quick` | `light-orchestrator` | Lightweight tasks, single-file edits |
| `/ui` | `ui-builder` | Frontend/UI work |
| `/vision` | `vision` | Multimodal: image/screenshot/chart understanding |
| `/review` | `reviewer` (code-review + gh-cli) | Local-diff or PR review: with a PR ref/URL, posts to GitHub (event=COMMENT); otherwise reviews the local diff and reports by severity; scope-first gate (>500 effective lines or trivial → report a scoped plan and stop) |
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

OpenCode exposes skills on demand via the native `skill` tool — agents load them only when needed, so they never occupy context.

| Skill | Purpose |
| --- | --- |
| `code-review` | Single-pass code review + evidence gating; large diffs (>~500 lines) split into Standards/Spec two axes merged into one report |
| `codemap` | Generates an annotated repository structure map for quick orientation, saving exploration tokens |
| `gh-cli` | GitHub CLI v2.100+ reference: PR posting, api, rate limits, gh pr checks, gh skill/gh-aw, GHSA security notes |
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
| `grilling` | Requirements-alignment interview: one question at a time, multiple choice preferred, converge on ambiguity before acting |
| `wait-what` | Restates hard-to-parse user messages in one sentence for confirmation before acting |
| `writing-for-agents` | Writing leverage for agent-facing docs (skills/AGENTS.md/pointer docs) |
| `to-tickets` | Breaks a spec/plan into trackable GitHub issues (one independently completable, verifiable unit per issue, with acceptance criteria) |
| `triage` | Label-based issue triage: pull → classify → apply labels/assignees (gh); routing only, never edits content |
| `diagnosing-bugs` | Systematic debugging: build a tight red-capable feedback loop BEFORE theorizing → reproduce + minimise → 3-5 falsifiable hypotheses → instrument one variable at a time (`[DEBUG-<hex>]` tagged) → fix at the correct seam + regression test → clean up |
| `codebase-design` | Architecture vocabulary: module/interface/depth/seam/adapter/leverage/locality, deletion test, depth test — assess whether module boundaries are sound |
| `domain-modeling` | Active domain modeling: maintain a CONTEXT.md glossary (vocabulary only, no implementation details), challenge/sharpen fuzzy terms during sessions, offer ADRs only when warranted; includes the repeated-explanation trigger (pin a term when the same concept keeps being re-explained) |
| `grill-with-docs` | Composes `grilling` + `domain-modeling`: when requirements are ambiguous AND domain language is fuzzy, converge intent one question at a time while sharpening the glossary |

## Repository Structure

```text
├── opencode/          # OpenCode config directory (agents/, skills/, opencode.jsonc, AGENTS.md, dcp.jsonc)
├── scripts/           # sync-config.ps1 (sync to global config) + validate-jsonc.js (JSONC validation)
├── README.md          # Simplified Chinese (default)
├── README.en-US.md    # English
└── LICENSE
```

## Usage Guide

### Mode 1: Orchestrator Auto-Routing (Default)

Describe your needs in natural language; the Orchestrator analyzes intent and picks the most suitable agent and model to execute.

```text
"Help me debug the login API error"     → oracle analyzes root cause → returns diagnostic report   (pro high)
"Optimize this loop, performance is poor" → oracle analyzes → deep-worker implements optimization  (pro high)
"Review this PR for me"                 → reviewer performs multi-dimensional review → returns tiered report  (pro high)
"I want to add an export feature to the user module" → planner drafts plan → deep-worker implements  (flash low → pro high)
"How to use React 19's use() API"       → librarian checks docs → returns signature and examples  (flash off)
```

### Mode 2: Command Alias Shortcuts

| Scenario | Command |
| --- | --- |
| Complex implementation / multi-file changes | `/deep` |
| Lightweight changes / single-file edits | `/quick` |
| Technical proposal / architecture design | `/plan` |
| Bug hunting / deep analysis | `/oracle` |
| Code review | `/review` |
| Frontend / UI work | `/ui` |
| Multimodal / image understanding | `/vision` |

### Typical Workflows

**Building a new feature (spec-driven):**
```text
/spec-propose  → /spec-apply  → /review
```

**Debugging a bug:**
```text
/oracle  → /deep  → /rmslop  → /commit
```

**Code review:**
```text
/review #123   ← PR mode: review PR + post to GitHub (gh-cli, event=COMMENT)
/review        ← local-diff mode: report by severity (scope-first gate)
```

## Sources

The core ideas draw on [oh-my-openagent](https://github.com/code-yeongyu/oh-my-openagent) (intent gating, read-only isolation, anti-patterns), [oh-my-opencode-slim](https://github.com/alvinunreal/oh-my-opencode-slim) (dispatcher-first, fallback chains, rejection contract, prompt-cache safety), [anomalyco/opencode](https://github.com/anomalyco/opencode) (config schema, skill system), [cli/cli](https://github.com/cli/cli) (gh v2.100 command set), [OpenSpec](https://github.com/Fission-AI/OpenSpec) (delta specs), [mattpocock/skills](https://github.com/mattpocock/skills) (conflict resolution, handoff documents, debugging/architecture/domain-modeling skills), [pi](https://github.com/earendil-works/pi) (answer first then act, terse responses), and [deepreview](https://github.com/mechanai/deepreview) (effective-size routing). Pure config, zero extra dependencies. **Borrow, don't copy**: take only lightweight design ideas, simplify before adding.

## Design Philosophy

- **Pure config-driven, zero extra dependencies** — every capability comes from `opencode.jsonc` + `agents/*.md` + `skills/*/SKILL.md` + `AGENTS.md`
- **Maximum use of the DeepSeek V4 model family** — Pro for deep reasoning and heavy implementation, Flash for routing, planning, routine execution, and native multimodal tasks
- **Token efficiency first** — path references instead of pasted files, skills loaded on demand, tiered compression management
- **Plugins add value without stealing the spotlight** — superpowers provides process discipline, DCP (dcp.jsonc) handles proactive dedup + compression thresholds, built-in compaction (opencode.jsonc) handles auto-trigger + prune fallback; both plugins are version-pinned to keep the prefix byte-stable and prevent prefix drift from auto-updates
- **Execution separated from exploration** — deep-worker/light-orchestrator must not research or delegate; explore/librarian must not modify
- **Cache + thinking discipline** — stable static prefixes to hit DeepSeek's prompt cache; flash disables thinking + temperature 0 (provider layer), pro keeps thinking on by default
- **Scope First + Delegate Always** — define scope first (2+ steps / multi-file / architecture changes go through planner), then delegate execution; top-level tokens are reserved for routing and hard problems
- **Atomic TODOs** — multi-step tasks start with an ordered TODO list, one item in_progress → completed at a time; format `path: action for scenario — verify by check`
- **Controllable progress + failure isolation** — every TODO carries a verifiable completion criterion and reports `[done/total]` at phase boundaries; errors are classified transient/recoverable/fatal, with at most 3 attempts per operation and a mandatory strategy change on each retry; large tasks split into independent units so one failure never blocks the rest, closing with a `succeeded / failed / skipped` tally
- **Per-model cost-tiered compression** — DCP's `modelMaxLimits`/`modelMinLimits` make pro (3× flash input cost) compress earlier and flash later, trading a smaller context window for a cheaper compression point
- **Vision input cost cap** — `attachment.image` auto-resizes oversized images (>1600px / >2MB before upload), combined with flash's internal ~800x800 downsampling, to avoid wasted base64 bytes
- **Verification budget + evidence strength** — set the minimum non-duplicative evidence path up front; "it typechecks" alone is not QA for a behavior change
- **Volatile-zone discipline** — volatile content (timestamps, random IDs, dynamic file lists) sits at the payload tail to protect DeepSeek's prompt-cache prefix
- **Continuous improvement** — reflect mechanizes friction discovery, code-review's evidence gating guards quality
