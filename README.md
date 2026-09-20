# My OpenCode × DeepSeek Config

**简体中文** | [English](README.en-US.md)

**OpenCode × DeepSeek 最优配置** —— 在 OpenCode 多 Agent 框架下，将 DeepSeek V4 模型族（Pro + Flash）的能力发挥到极致的配置方案。核心理念：**Token 效率优先，用最小的上下文成本达到最好的开发效果**。

## 当前配置概览

- 默认主 Agent：`orchestrator`
- 主模型：`deepseek/deepseek-v4-pro`，轻量/多模态模型：`deepseek/deepseek-flash`（V4.1 Flash，原生多模态）
- 代理层级：`subagent_depth: 2`（恰好覆盖实际最深链路 `orchestrator → light-orchestrator → oracle`；其余 subagent 一律禁止委派，更深层级只是白送 token 放大面）
- 会话分享：关闭（`share: "disabled"`）
- 权限基线：默认放行，破坏性 bash 命令设为 `ask`；`.env` 类敏感文件 `deny`；外部目录 `ask`；只读 Agent 的 bash 白名单（默认 deny 全部 + 仅放行只读子命令）
- 上下文压缩：内置 compaction（opencode.jsonc）管自动触发 + prune 裁旧工具输出，DCP（dcp.jsonc）管主动去重 + 压缩阈值，两者互补
- 全局规则：`AGENTS.md`（核心原则、任务拒绝契约、自我验证、反模式等；上下文/Token 纪律在 `AGENTS.md`）
- 技能：`skills/` 目录下 **23 个** `SKILL.md` 技能，通过原生 `skill` 工具按需加载；各 Agent 再用 `permission.skill` 白名单裁剪名册（名册的 name+description 是**每轮常驻**成本，故按职责最小化）
- 命令：**18 个**快捷命令（Agent 路由 / 操作 / 内联 / 规约四类），见下文
- 插件：`superpowers`（git URL 固定 tag `#v6.3.0`，过程型技能）、`@tarquinen/opencode-dcp`（固定版本 `@3.1.15`，智能上下文裁剪）；两者均固定版本（pin）以保证字节稳定前缀、避免自动更新导致的前缀漂移

### 插件与模型映射（重要）

两个插件**均不提供模型映射能力**，模型路由只能在 Agent 层完成——本配置已如此实现，无需也无法在插件内指定模型：

- **DCP 3.1.15**：`PluginConfig` 仅暴露 `modelMaxLimits` / `modelMinLimits`（按模型设压缩阈值），**没有** per-step 模型指派字段。本配置把**两个模型都显式列出**（键为精确 `providerID/modelID`，已核对 `dist/index.js`）：pro `55K/26K`、flash `77K/38K`——pro 输入价 3× flash，所以先切贵的窗口。这是 DCP 层面唯一可做的模型相关调优。
- **superpowers v6.3.0**：纯 skill 注入插件（`.opencode/plugins/superpowers.js`），无任何模型配置面。

因此"规划/架构/复杂审查用 pro，执行/初检/文档/批量/视觉用 flash"这一分工，全部由 `agents/*.md` 的 `model:` 字段与 thinking tiers 实现（见下文路由策略），而非插件配置。此结论已核实插件源码，勿重复调研。

## DeepSeek 模型配置

### 前置条件

- OpenCode ≥ v1.18.x（DeepSeek provider 为内置）
- DeepSeek API Key：[platform.deepseek.com/api_keys](https://platform.deepseek.com/api_keys) 申请
- 后台子智能体（`background: true` 的委派）需要环境变量 `OPENCODE_EXPERIMENTAL_BACKGROUND_SUBAGENTS=true`（PowerShell：`$env:OPENCODE_EXPERIMENTAL_BACKGROUND_SUBAGENTS="true"`）；未设置时后台委派会直接报错，应退回前台串行委派

### 方式一：TUI 交互式配置（推荐）

```bash
opencode
# 在 TUI 中输入: /connect → 选择 DeepSeek → 粘贴 API Key
# 然后: /models → 选择 deepseek-v4-pro
```

API Key 会自动持久化到 `~/.local/share/opencode/auth.json`。

### 方式二：环境变量

Windows PowerShell:
```powershell
$env:DEEPSEEK_API_KEY="sk-your-key-here"
opencode
```

永久设置：将 `DEEPSEEK_API_KEY` 添加到系统环境变量。

### Provider 配置参考

```jsonc
{
  "model": "deepseek/deepseek-v4-pro",
  "small_model": "deepseek/deepseek-flash"
}
```

本配置在 `provider` 层拆分 thinking：flash 关闭 thinking 并固定 `temperature: 0`（最快最省），pro 保持默认（thinking 开启）。`deepseek-flash` 是合并后的 V4.1 Flash 模型——原文本版与视觉版已合并为一个原生多模态模型，因此由它声明 `modalities`（图像输入）。示例（flash）：

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

> **模型 ID 命名规则**：`provider_id/model_id`，即 `deepseek/deepseek-v4-pro` 和 `deepseek/deepseek-flash`。

## 快速开始

```powershell
# 1. 克隆
git clone https://github.com/znlgis/my-opencode-deepseek-config.git

# 2. 让 opencode 使用本仓库的配置目录（当前会话生效）
$env:OPENCODE_CONFIG_DIR = "D:\path\to\my-opencode-deepseek-config\opencode"

# 3. 配置 API Key（二选一：TUI 里 /connect，或环境变量）
$env:DEEPSEEK_API_KEY = "sk-your-key-here"

# 4. 启动
opencode
```

启动后自检三条：`/models` 显示 `deepseek/deepseek-v4-pro`；Agent 列表有 12 个自定义 Agent；随便描述一个需求，`orchestrator` 会自动分类并路由（想跳过路由就直接用 `/quick`、`/deep`、`/review` 等命令）。

改完仓库文件后需要同步到全局配置目录（`~/.config/opencode` 是独立副本，不是符号链接）：

```powershell
.\scripts\sync-config.ps1
```

## 安装部署

### 方式一：克隆 + 环境变量（推荐，跨平台通用）

```bash
git clone https://github.com/znlgis/my-opencode-deepseek-config.git
```

然后将 `OPENCODE_CONFIG_DIR` 指向仓库内的 `opencode/` 子目录即可使用。

**Windows（PowerShell）** —— 永久生效：

```powershell
[Environment]::SetEnvironmentVariable("OPENCODE_CONFIG_DIR", "D:\path\to\my-opencode-deepseek-config\opencode", "User")
```

**Windows（PowerShell）** —— 临时生效（仅当前会话）：

```powershell
$env:OPENCODE_CONFIG_DIR = "D:\path\to\my-opencode-deepseek-config\opencode"
opencode
```

**Linux / macOS** —— 追加到 `~/.bashrc` 或 `~/.zshrc`：

```bash
export OPENCODE_CONFIG_DIR="$HOME/path/to/my-opencode-deepseek-config/opencode"
```

### 方式二：符号链接到全局配置目录

**Windows（PowerShell，需管理员）：**

```powershell
New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\.config"
New-Item -ItemType SymbolicLink -Path "$env:USERPROFILE\.config\opencode" -Target "D:\path\to\my-opencode-deepseek-config\opencode"
```

**Linux / macOS：**

```bash
ln -s /path/to/my-opencode-deepseek-config/opencode ~/.config/opencode
```

> **兼容性说明**：`~/.config/opencode` 是 OpenCode 的标准全局配置路径。本仓库的 `opencode/` 子目录内含 `agents/`、`skills/`、`AGENTS.md` 等文件，布局完全遵循 OpenCode 约定，通过环境变量或符号链接指向后即可被自动识别。

### 验证安装

启动 OpenCode 确认：
1. `/models` → 当前模型为 `deepseek/deepseek-v4-pro`
2. Agent 列表应能看到 `orchestrator`、`planner`、`deep-worker` 等 12 个 Agent
3. 输入任意请求，Orchestrator 自动分析意图并路由

### 同步

`~/.config/opencode` 是独立副本（非符号链接），本仓库才是配置源。改完仓库后需手动同步才生效。Windows 下运行：

```powershell
.\scripts\sync-config.ps1
```

将 `opencode/` 下的配置文件同步到 `~/.config/opencode/`（排除 `node_modules`、`package.json`、`package-lock.json`）。脚本支持 `-Src` 传参指定源目录，便于其他机器使用：

```powershell
.\scripts\sync-config.ps1 -Src "D:\path\to\my-opencode-deepseek-config\opencode"
```

> **注意**：`skills/`、`agents/`、`commands/` 的删除清理走 git 索引 + 历史记录。刚 `git rm` 但**尚未提交**的删除不会被识别为“待清理文件”，此时需手动删一次全局目录里的同名副本（提交后再跑一次同步即可自动对账）。

## 模型分工

本仓库严格限制在 DeepSeek V4 模型族内分工，不引入其他模型——**2 个模型 × 按 Agent 分档的思考强度（thinking tier）**，而非多模型矩阵：

| 模型 | 用途 |
| --- | --- |
| `deepseek/deepseek-v4-pro` | 深度推理、根因分析、代码审查、重型多文件实现 |
| `deepseek/deepseek-flash` | 编排/路由、规划、常规实现、咨询、UI、探索、外部检索、轻量编辑、标题/摘要/压缩；原生多模态（图像/截图/图表/UI 稿理解） |

思考强度由 **`reasoning_effort`** 控制——它是**请求级**的思考强度开关（`low`/`high`/`max`），**不是模型 ID**，按 Agent 经 frontmatter `options`（camelCase `reasoningEffort`，深度合并到 `model.options`）设置，不改 `model:` 字段。这样 2 模型矩阵不变，同一模型可跑不同思考档：

| 档位 | 模型 × thinking | 典型 Agent |
| --- | --- | --- |
| 低（trivial） | flash · thinking 关 | `explore`/`librarian`/`consultant`/`ui-builder`/`orchestrator` |
| 中（routine） | flash · thinking 开 + `reasoningEffort: low` | `planner`/`light-orchestrator` |
| 高（deep） | pro · 默认 high | `deep-worker`/`oracle`/`reviewer` |
| 跟随会话 | 会话所选模型（默认 pro） | `solo`（无 `model` 字段，思考档随该模型） |

成本比：pro 输入价 3× flash（0.66 vs 0.22 / 1M tokens），故 trivial 任务绝不落到 pro。

### 路由策略

- **Trivial → flash off**：搜索、查询、咨询、UI、探索、文档检索等明确定义的轻任务走 flash agent，thinking 关闭（最省）
- **Routine-nontrivial → flash low**：规划、常规多文件实现等稍有难度的任务走 flash + `reasoningEffort: low`
- **Deep/uncertain → pro high**：深度推理、根因分析、重型多文件实现——只用 pro
- **代码审查 → flash 初检，pro 升级**：`/review` 默认走 flash 初检（Abbreviated 路径），仅在升级触发条件命中时委派 `reviewer`（pro）；`/deep-review` 强制 pro 全量审查
- **Vision 专责多模态**：仅在用户明确提供图像/截图或明确要求时，路由到 `vision` agent（`deepseek-flash`，原生多模态）。**视觉输入是 opt-in**：非视觉任务不主动传图、不生成图、不调用视觉能力（`AGENTS.md`「Constraints」已把这条写成硬约束）；附件统一先经 `attachment.image` 缩到 1600px / 2MiB，避免 base64 字节浪费
- **自动升级**：flash agent 无法胜任时自动升级到 pro（带完整上下文）

用法示例：`「这个库怎么用」` → flash off（librarian）；`「给用户模块加导出功能」` → flash low（planner）；`「排查登录接口报错的根因」` → pro high（oracle）；`/review` → flash 初检（小 diff 直接出报告）；`/review #123`（大 diff / 触及信任边界）→ flash 初检后升级 pro。

#### 代码审查的两级模型（轻量化 Review）

| 层级 | 模型 | 执行者 | 覆盖范围 |
| --- | --- | --- | --- |
| Tier 1（默认） | `deepseek-flash` | `light-orchestrator`（`/review`） | Abbreviated 路径：≤8 个逻辑文件且 ≤300 有效行，且无高风险触发 |
| Tier 2（升级） | `deepseek-v4-pro` | `reviewer`（`/deep-review` 或自动升级） | Full 路径、高风险触发、或 Tier 1 发现需跨文件确认的 critical/major |

升级触发条件（命中任一即升级）：路径为 Full（大 diff 或高风险正则命中）；Tier 1 发现 critical/major 但无法仅凭 diff 确认影响；diff 触及信任边界（同时加载 `security-review`）；用户明确要求深度审查。

Tier 1 报告是**完整审查**而非预览——干净结果不因"再确认一下"而升级。升级时把 Tier 1 发现作为**未验证线索**传给 Tier 2，让 pro 确认而非重新推导，避免重复 token 消耗。

### 成本对比

价格取自 `opencode.jsonc` 的 `provider.deepseek.models`（USD / 1M tokens，2026-08-16 生效的 off-peak 价；peak 时段翻倍）。`cache_write` 无独立官方价，按 cache-miss 输入价映射：

| 模型 | 输入 | 输出 | 缓存命中（cache_read） | 缓存写入（cache_write） | 相对 flash 输入价 |
| --- | --- | --- | --- | --- | --- |
| `deepseek-flash` | 0.22 | 0.66 | 0.007 | 0.22 | 1× |
| `deepseek-v4-pro` | 0.66 | 1.98 | 0.022 | 0.66 | 3× |

两个成本杠杆：

- **模型档位**：pro 输入/输出价均为 flash 的 3×，故 trivial 任务绝不落到 pro（见上文路由策略）。
- **提示词缓存**：`cache_read` 比输入价便宜约 **30×**（flash 0.007 vs 0.22；pro 0.022 vs 0.66）。本配置的字节稳定前缀 + 易变区纪律（见 `AGENTS.md`）正是为了最大化缓存命中率。

**典型会话成本估算**（假设 200K 输入 tokens，其中 150K 命中缓存，30K 输出）：

| 模型 | 缓存命中 | 输入未命中 | 输出 | 合计 |
| --- | --- | --- | --- | --- |
| flash | 150K × 0.007 = $0.001 | 50K × 0.22 = $0.011 | 30K × 0.66 = $0.020 | **≈ $0.032** |
| pro | 150K × 0.022 = $0.003 | 50K × 0.66 = $0.033 | 30K × 1.98 = $0.059 | **≈ $0.096** |

同一 token 量下 pro ≈ 3× flash。可用 `scripts/estimate-cost.js` 按实际 token 数估算。

#### 优化前后成本对比

以"审查一个 300 有效行的本地 diff"为例（假设 60K 输入 tokens，其中 45K 命中缓存，8K 输出）：

| 方案 | 模型 | 缓存命中 | 输入未命中 | 输出 | 合计 |
| --- | --- | --- | --- | --- | --- |
| 优化前（`/review` 固定 pro） | pro | 45K × 0.022 = $0.001 | 15K × 0.66 = $0.010 | 8K × 1.98 = $0.016 | **≈ $0.027** |
| 优化后（`/review` flash 初检） | flash | 45K × 0.007 = $0.0003 | 15K × 0.22 = $0.003 | 8K × 0.66 = $0.005 | **≈ $0.009** |
| 优化后（升级到 pro 全量） | pro | 45K × 0.022 = $0.001 | 15K × 0.66 = $0.010 | 8K × 1.98 = $0.016 | **≈ $0.027** |

**节省比例**：小 diff 走 flash 初检约省 **67%**（$0.027 → $0.009）；只有命中升级触发条件时才付 pro 全价，且升级时传递未验证线索避免重复推导。

其他优化项的 token 节省（字节均为 LF 计数，即仓库实际存储大小）：

| 优化项 | 变更 | 节省 |
| --- | --- | --- |
| `AGENTS.md` 精简（累计两轮） | 15173 → 13938 字节 | 每轮常驻上下文省 **8.1%**（该文件每轮都加载，收益随会话轮数线性放大） |
| `orchestrator.md` 精简（累计两轮） | 14678 → 12776 字节 | 省 **12.9%**（本轮 −1157 字节：删除与 `AGENTS.md` 重复的 thinking tier / retry cap / reference-paths 规则、10 处 `· ~½ cost` 冗余标注） |
| 默认入口静态前缀合计 | `AGENTS.md` + `orchestrator.md` 28050 → 26714 字节 | 每轮省 **1336 字节 ≈ 334 tokens**（约占默认入口常驻前缀的 5%） |
| Skills 名册瘦身 | 25 → 23 个（合并 `wait-what`/`grill-with-docs` 进 `grilling`） | 名册 name+description 常驻成本 **10,127 → 9,802 字节**（−325 字节 ≈ −81 tokens），且少两个可能选错的入口 |
| `subagent_depth` 3 → 2 | 覆盖实际最深链路即可 | 关掉未使用的第 3 层嵌套，避免意外 token 放大 |
| `dcp.jsonc` 注释扩充 | 仅注释，键值仅新增 flash 的显式阈值（与全局默认同值） | 注释不进入 API 请求，零运行时成本 |
| 内置 utility agent 全走 flash | build/plan/title/summary/compaction | 单次调用成本降至 pro 的 **1/3** |

> 说明：常驻前缀的变化在**缓存未命中**的请求上体现为全额 token 差异，命中缓存时按 `cache_read` 价（flash 0.007 / pro 0.022 per 1M）计——但前缀越小，命中率越高、压缩触发越晚，两者叠加才是节省的完整来源。

**实测基线**（本机 2026-09-20，精简后配置）：

```powershell
opencode run "Reply with exactly: OK" --agent orchestrator --format json
```

| 指标 | 实测值 |
| --- | --- |
| 首轮 input tokens | **16,254**（cache read 0 —— 冷启动无缓存命中） |
| output tokens | 1 |
| 单次费用 | **$0.00358**（flash，off-peak 价） |

这 16,254 tokens 里，`AGENTS.md`（13,938 B）+ `orchestrator.md`（12,776 B）≈ 6.7K tokens，其余是 opencode 基础系统提示与工具 schema——**本仓库能直接控制的就是前面这部分**，所以「精简提示词」是唯一能持续压缩常驻成本的手段。复现这条命令即可核对当前常驻开销。

## Agent 结构

### Primary Agent

| Agent | 模型 | 作用 |
| --- | --- | --- |
| `orchestrator` | flash | 默认入口：意图门控（Intent Gate）+ 模型感知路由 + 后备链 |
| `solo` | v4-pro（默认） | 单模型内联执行器：零委派、不用后台助手，全程在当前会话所选模型内完成 |

> `solo` 是第二个 primary agent：`permission.task: "*": "deny"`（零委派，不调用任何子智能体）、无显式 `model` 字段（跟随会话默认模型 pro）、不用 build/plan 等后台助手（它们跑在内置 flash 上，会破坏全程单模型的保证），分析、规划、实现、验证全部在当前会话内联完成。

### Subagents

| Agent | 模型 | 权限 | 作用 |
| --- | --- | --- | --- |
| `planner` | flash | 读写 | 规划、架构、拆解任务 |
| `deep-worker` | v4-pro | 读写 | 重型实现、多文件改动、复杂调试 |
| `oracle` | v4-pro | **只读** | 根因分析、深度理解代码 |
| `reviewer` | v4-pro | **只读** | 代码审查升级层：Full 路径 / 高风险触发 / 确认 flash 初检线索 |
| `ui-builder` | flash | 读写 | 前端与 UI 相关任务 |
| `consultant` | flash | 读写 | 方案讨论、最佳实践建议 |
| `explore` | flash | **只读** | 代码库搜索、并行探索 |
| `librarian` | flash | **只读** | 文档检索、Web 搜索 |
| `light-orchestrator` | flash | 读写 | 轻量任务、单文件编辑 |
| `vision` | flash | 读写 | 多模态：图像/截图/图表/UI 稿理解 |

> `deep-worker` 和 `light-orchestrator` 遵循"禁止研究、禁止委托"原则——执行而非探索，上下文由 orchestrator 提供。`deep-worker` 另带 "What you DON'T handle" 拒绝契约：琐碎单文件编辑 → 拒接（路由 `light-orchestrator`）、纯研究/查询 → 拒接（路由 `oracle`/`explore`）、任何 flash 能完成的任务 → 拒接（pro 是 3× flash）。
>
> 只读 Agent（`oracle`/`reviewer`/`explore`）真只读化：`edit: deny` + bash 白名单（默认 deny 全部，仅放行 `git status/diff/log/show/blame/grep`、`rg` 等只读子命令；`oracle`/`reviewer` 另允许 `gh pr view/diff`、`gh issue view`、`gh api` 以支持 `/review` 回帖）。`librarian` 更严格：`bash: "*": deny`，无任何 bash 白名单。
>
> 各 agent 带 `skills` 白名单（默认 deny + 按职责放行，防误加载重型 skill）：`orchestrator` → `codemap`/`grilling`；`planner` → `spec-workflow`/`codebase-design`；`deep-worker` → `remove-deadcode`/`spec-workflow`/`git-release`/`to-tickets`/`triage`/`git-master`/`resolving-merge-conflicts`/`opencode-config`/`writing-for-agents`/`diagnosing-bugs`/`codebase-design`/`domain-modeling`；`oracle` → `reflect`/`simplify`/`diagnosing-bugs`；`reviewer` → `code-review`/`security-review`/`gh-cli`；`explore` → `codemap`；`librarian` → `verify-with-docs`；`light-orchestrator` → `handoff`/`simplify`/`spec-workflow`/`code-review`/`gh-cli`；`consultant` → `domain-modeling`；`ui-builder` → `codebase-design`；`vision` → `vision-prep`；`solo` → 全部本地 skill（内联执行器需要完整工具链，仍以 `"*": deny` 兜底）。白名单不只是权限——**被 deny 的 skill 不会出现在该 Agent 的 skill 名册里**，所以这份名单同时就是常驻上下文预算。
>
> **思考分档（thinking tiers）**：`reasoning_effort` 是按 Agent 经 frontmatter `options` 设置的请求级思考强度（`low`/`high`/`max`），不是模型 ID。`explore`/`librarian`/`consultant`/`ui-builder`/`orchestrator` = flash · thinking 关（最省）；`planner`/`light-orchestrator` = flash · thinking 开 + `reasoningEffort: low`；`deep-worker`/`oracle`/`reviewer` = pro · 默认 high；`solo` 无 `model` 字段，跟随会话所选模型（默认 pro），思考档随该模型而定。

## 快捷命令

### Agent 路由命令

| 命令 | Agent | 用途 |
| --- | --- | --- |
| `/deep` | `deep-worker` | 重型实现、多文件改动 |
| `/quick` | `light-orchestrator` | 轻量任务、单文件编辑 |
| `/ui` | `ui-builder` | 前端/UI 工作 |
| `/vision` | `vision` | 多模态：图像/截图/图表理解 |
| `/review` | `light-orchestrator`（code-review）→ 按需升级 `reviewer` | 代码审查：默认 flash 初检（Abbreviated 路径直接出报告）；命中升级触发条件时委派 `reviewer`（pro）并传递未验证线索；带 PR ref/URL 时回帖 GitHub（event=COMMENT） |
| `/deep-review` | `reviewer`（code-review + gh-cli） | 强制 pro 全量审查，跳过 flash 初检；带 PR ref/URL 时回帖 GitHub（event=COMMENT） |
| `/plan` | `planner` | 制定计划、技术方案 |
| `/oracle` | `oracle` | 深度分析、问题溯源 |

### 操作命令

| 命令 | Agent | 用途 |
| --- | --- | --- |
| `/commit` | `light-orchestrator` | 生成 Conventional Commits 提交信息（内联格式） |
| `/release` | `deep-worker`（git-release） | 准备 Tag 发布 |
| `/reflect` | `oracle`（reflect） | 发现摩擦 → 提出配置优化 |
| `/handoff` | `light-orchestrator`（handoff） | 压缩会话为交接文档 |

### 内联命令

| 命令 | Agent | 用途 |
| --- | --- | --- |
| `/codemap` | `explore`（codemap） | 生成仓库结构图 |
| `/learn` | `deep-worker` | 把会话中的非显然经验沉淀到目录级 AGENTS.md（根/包/特性级） |
| `/simplify` | `light-orchestrator`（simplify）→ spawn `oracle` | spawn oracle 只读分析 → light-orchestrator 应用编辑 |
| `/rmslop` | `deep-worker`（remove-deadcode） | 清理死代码和 AI slop |

### 规约命令

| 命令 | Agent | 用途 |
| --- | --- | --- |
| `/spec-propose` | `planner`（spec-workflow） | 探索代码 → 起草变更提案 |
| `/spec-apply` | `deep-worker`（spec-workflow） | 按 tasks.md 逐一实现 → 自动归档 |

## 技能（Skills）

OpenCode 通过原生 `skill` 工具按需暴露技能——Agent 只在需要时才加载，不占用上下文。

| Skill | 作用 |
| --- | --- |
| `code-review` | 单遍代码审查 + 证据门控；flash 初检 / pro 升级两级模型；大 diff（>~500 行）拆 Standards/Spec 两轴合并报告 |
| `codemap` | 生成带标注的仓库结构图，快速定向，节省探索 token |
| `gh-cli` | GitHub CLI v2.100+ 参考：PR 回帖、api、rate limit、gh pr checks、gh skill/gh-aw、GHSA 安全要点 |
| `git-master` | 高级 Git 操作：rebase、squash、fixup、bisect、reflog、代码考古、worktree |
| `git-release` | Tag 发布：发布说明、SemVer 推断、gh release 命令 |
| `resolving-merge-conflicts` | 逐 hunk 解析合并冲突：追溯原始意图、永不发明新行为、永不 --abort |
| `handoff` | 压缩会话为交接文档（路径引用，不复制内容） |
| `opencode-config` | 编写和维护本仓库 OpenCode 配置（agents/skills/commands/permissions） |
| `office-docs` | 读写 Word（.docx）/Excel（.xlsx），与纯文本/Markdown 互转；纯 Python 脚本，无需 MS Office 或 MCP |
| `reflect` | 持续改进：发现摩擦 → 提出最小可维护修复 |
| `remove-deadcode` | 安全查找并删除死代码，删除前经工具链/LSP 验证 |
| `security-review` | 合并前安全审查（注入/XSS/SSRF/密钥/反序列化/路径穿越），只报不改 |
| `simplify` | 行为保持的代码简化（oracle 分析 → 应用） |
| `spec-workflow` | 轻量规约驱动变更：proposal → delta specs → tasks → update 三问决策树 → verify → archive |
| `verify-with-docs` | 编码前核对 API 文档，检索优先，防幻觉 |
| `vision-prep` | 大图/PDF 送入视觉模型前的预处理：大图切块、PDF 栅格化，规避 ~800x800 降采样损失 |
| `grilling` | 需求对齐访谈：一次一问、多选优先，歧义收敛后再动手；并含「消息完全没听懂 → 一句话重述确认」与「领域术语模糊 → 协同 `domain-modeling` 锐化」两条分支（已合并原 `wait-what`/`grill-with-docs`） |
| `writing-for-agents` | 写给 agent 看的文档（skill/AGENTS.md/指针文档）的写作杠杆 |
| `to-tickets` | 把 spec/plan 拆解为可追踪的 GitHub issue（每单一个可独立完成+验收的单元，带验收标准） |
| `triage` | 基于 label 的 issue 分流：拉取 → 分类 → 打标签/派单（gh），只分流不改内容 |
| `diagnosing-bugs` | 系统化排障：先搭紧致的红态反馈回路再理论化 → 复现最小化 → 3-5 个可证伪假设 → 单变量插桩（`[DEBUG-<hex>]` 标记）→ 正确接缝处修复 + 回归测试 → 清理 |
| `codebase-design` | 架构词汇表：module/interface/depth/seam/adapter/leverage/locality，删除测试、深度测试，评估模块边界是否合理 |
| `domain-modeling` | 主动领域建模：维护 CONTEXT.md 术语表（仅词汇，不含实现细节），会话中挑战/锐化模糊术语，仅在必要时提议 ADR；含重复解释触发——同一概念被反复解释时落一条术语 |

> **名册即预算**：`skill` 工具的 `<available_skills>` 里每个 skill 的 name + description 都是**每轮常驻**上下文（本仓库 23 个本地 + 14 个 superpowers + 1 个内置 ≈ 10KB ≈ 2.5K tokens，对未设白名单的内置 Agent 全量生效）。所以「不再高频使用」或「与现有 skill 重复」的 skill 应当合并删除，而不是留着备用；超长的 `description` 要按触发词必需性裁剪。
| `grill-with-docs` | 组合 `grilling` + `domain-modeling`：需求歧义且领域术语模糊时，一次一问收敛意图并同步锐化术语表 |

## 仓库结构

```text
├── opencode/          # OpenCode 配置目录（agents/、skills/、opencode.jsonc、AGENTS.md、dcp.jsonc）
├── scripts/           # sync-config.ps1（同步到全局配置）
│                      # validate-jsonc.js（JSONC 校验）
│                      # estimate-cost.js（按 token 数估算费用）
├── README.md          # 简体中文（默认）
├── README.en-US.md    # English
└── LICENSE
```

## 配置变更点速查

改配置时按「想改什么 → 改哪个文件」定位，避免改错层：

| 想改的东西 | 文件 | 位置 |
| --- | --- | --- |
| 模型清单 / 价格 / thinking / temperature | `opencode.jsonc` | `provider.deepseek.models` |
| 画像：默认 Agent、小模型、嵌套深度、工具输出上限、压缩参数、附件缩放 | `opencode.jsonc` | 顶层同名键 |
| 权限（读 / bash / skill / 外部目录） | `opencode.jsonc` | `permission` |
| 内置 utility agent（build/plan/title/summary/compaction）的模型 | `opencode.jsonc` | `agent` |
| 快捷命令的 Agent 与模板 | `opencode.jsonc` | `command` |
| 单个 Agent 的模型、思考档、工具与 skill 白名单、拒绝契约 | `opencode/agents/<name>.md` | frontmatter + 正文 |
| 全局行为规则（原则、失败纪律、缓存纪律、反模式） | `opencode/AGENTS.md` | 对应小节 |
| 插件版本（pin） | `opencode/opencode.jsonc` | `plugin` |
| DCP 压缩阈值 / 去重 / 错误清理 | `opencode/dcp.jsonc` | `compress` / `strategies` |
| Skill 的行为与触发词 | `opencode/skills/<name>/SKILL.md` | frontmatter `description` + 正文 |

> 改完必须 `.\scripts\sync-config.ps1` 同步到 `~/.config/opencode`，并重启 opencode（配置只在启动时加载一次）。校验：`node scripts/validate-jsonc.js`；查已解析结果：`opencode debug config`。

## 使用指南

### 模式一：Orchestrator 自动路由（默认）

用自然语言描述需求，Orchestrator 自动分析意图、选择最合适的 Agent 和模型执行。

```text
「帮我排查这个登录接口的报错」     → oracle 分析根因 → 返回诊断报告        （pro high）
「优化这段循环，性能太差了」         → oracle 分析 → deep-worker 实施优化    （pro high）
「这个 PR 帮我审查一下」             → reviewer 多维度审查 → 返回分级报告   （pro high）
「我想给用户模块加个导出功能」       → planner 制定方案 → deep-worker 实现  （flash low → pro high）
「React 19 的 use() API 怎么用」    → librarian 查文档 → 返回签名和示例   （flash off）
```

### 模式二：命令别名直达

| 场景 | 命令 |
| --- | --- |
| 复杂实现 / 多文件改动 | `/deep` |
| 轻量修改 / 单文件编辑 | `/quick` |
| 制定技术方案 / 架构设计 | `/plan` |
| 排查 Bug / 深度分析 | `/oracle` |
| 代码审查 | `/review` |
| 前端 / UI 工作 | `/ui` |
| 多模态 / 图像理解 | `/vision` |

### 典型工作流

**开发新功能（规约驱动）：**
```text
/spec-propose  → /spec-apply  → /review
```

**排查 Bug：**
```text
/oracle  → /deep  → /rmslop  → /commit
```

**代码审查（flash 初检 → pro 升级）：**
```text
/review        ← 默认：flash 初检（Abbreviated 路径直接出报告，最省）
/review #123   ← PR 模式：审查 PR + 回帖 GitHub（gh-cli，event=COMMENT）
/deep-review   ← 强制 pro 全量审查（大 diff / 高风险 / 需要跨文件确认时）
```

**日常编码（flash 为主）：**
```text
「给 utils 加一个日期格式化函数」  → light-orchestrator 单文件实现   （flash low）
「这个函数为什么返回 undefined」   → explore 定位 → 直接回答        （flash off）
/quick 修一下这个拼写错误          → light-orchestrator 直接改       （flash low）
```

## 本次重构变更记录

面向「省 token + 少 API 调用」的一次收敛：**只做减法与显式化，不引入任何新模型、新依赖、新工具**。

**删除**

| 删除项 | 理由 |
| --- | --- |
| `skills/wait-what/` | 与 `grilling` 的「先确认再动手」规则重复；一个行为不该占两个名册位 |
| `skills/grill-with-docs/` | 只是 `grilling` + `domain-modeling` 的组合说明，组合关系已在 `grilling` 正文写明 |
| `orchestrator.md` 中重复 `AGENTS.md` 的 4 条规则 | thinking tier / retry cap / reference-paths 等已由 `AGENTS.md` 单点定义，重复表述只增加常驻 token |
| `orchestrator.md` 路由表中 10 处 `· ~½ cost` 标注 | 同一信息在表头已声明一次，逐行重复属于噪音 |
| `subagent_depth: 3` 的第 3 层 | 实际最深链路只有 2 层，第 3 层无人使用，只提供意外嵌套放大的可能 |

**新增 / 强化**

| 新增项 | 预期收益 |
| --- | --- |
| `AGENTS.md` 硬约束「Vision input is opt-in」 | 从规则层保证非视觉任务不传图/不生成图，只有用户提供图像时才走 `vision`（flash 多模态） |
| `dcp.jsonc` 显式列出**两个模型**的压缩阈值 | 模型映射不再依赖继承；pro `55K/26K`、flash `77K/38K`，读配置即可确认分工 |
| `vision-prep` 修正附件上限陈述 | 原文档写「2000×2000 / 5MiB（opencode 默认）」，与本仓 `attachment.image`（1600px / 2MiB）矛盾，会误导预处理 |
| README「配置变更点速查」表 | 维护时一次定位到文件与小节，减少试错（试错本身就是 token 消耗） |
| README「快速开始」四步 TL;DR | 新机器上手从「读完全文」变成「照抄四行」 |

**两个模型的最终路由规则**（全仓仅此两个模型，无任何其他模型引用）

| 模型 | 触发条件 | 承担角色 | 计费（USD/1M，off-peak） |
| --- | --- | --- | --- |
| `deepseek/deepseek-flash` | **默认**；绝大部分高频任务 | 编排/路由、规划、常规实现、单文件编辑、咨询、UI、探索、文档检索、标题/摘要/压缩、批量生成；**唯一**的视觉理解入口（仅在用户提供图像时启用） | in 0.22 / out 0.66 / cache 0.007 |
| `deepseek/deepseek-v4-pro` | 任务复杂度高、需深层逻辑分析时（自动或手动 `/deep`、`/oracle`、`/deep-review`） | 复杂推理、根因分析、代码审查升级层、架构设计、疑难调试、重型多文件实现 | in 0.66 / out 1.98 / cache 0.022 |

切换方式：自动——`orchestrator` 按意图分类路由，flash agent 无法胜任时自升级；手动——`/deep`、`/oracle`、`/deep-review` 直达 pro，其余默认 flash。

## 借鉴来源

核心思路借鉴 [oh-my-openagent](https://github.com/code-yeongyu/oh-my-openagent)（意图门控、只读隔离、反模式）、[oh-my-opencode-slim](https://github.com/alvinunreal/oh-my-opencode-slim)（调度器优先、后备链、拒绝契约、提示词缓存安全）、[anomalyco/opencode](https://github.com/anomalyco/opencode)（配置 Schema、技能体系）、[cli/cli](https://github.com/cli/cli)（gh v2.100 命令集）、[OpenSpec](https://github.com/Fission-AI/OpenSpec)（delta specs）、[mattpocock/skills](https://github.com/mattpocock/skills)（冲突解析、交接文档、排障/架构/领域建模技能）、[pi](https://github.com/earendil-works/pi)（先答后改、精简响应）、[deepreview](https://github.com/mechanai/deepreview)（有效大小路由）。纯配置实现，零额外依赖。**借鉴而非照搬**：只汲取轻量化设计理念，精简优先于新增。

## 设计哲学

- **纯配置驱动，零额外依赖** —— 所有能力由 `opencode.jsonc` + `agents/*.md` + `skills/*/SKILL.md` + `AGENTS.md` 实现
- **DeepSeek V4 模型族极致利用** —— Pro 做深度推理与重型实现，Flash 做路由、规划、常规执行与原生多模态
- **Token 效率优先** —— 路径引用替代粘贴文件、技能按需加载、压缩分级管理
- **插件增效但不喧宾夺主** —— superpowers 提供过程纪律，DCP（dcp.jsonc）主动去重+压缩阈值，内置 compaction（opencode.jsonc）自动触发+prune 兜底；两插件均固定版本（pin）以保字节稳定前缀，避免自动更新导致前缀漂移
- **执行与探索分离** —— deep-worker/light-orchestrator 禁止研究/委托，explore/librarian 禁止修改
- **缓存与 thinking 纪律** —— 静态前缀稳定以命中 DeepSeek 提示词缓存；flash 关 thinking + temperature 0（provider 层），pro 默认 thinking 开
- **Scope First + Delegate Always** —— 先定范围（2+ 步/多文件/架构变更先走 planner），再委派执行，顶层 token 只留给路由与难题
- **原子 TODO** —— 多步任务先写有序 TODO，逐条 in_progress→completed；格式 `path: action for scenario — verify by check`
- **进度可控 + 失败隔离** —— 每个 TODO 带可验证完成判据，阶段边界汇报 `[done/total]`；错误分 transient/recoverable/fatal 三类，同一操作最多重试 3 次且每次必须换策略；大任务拆成独立单元，单元失败不阻塞其余，最终汇总 `succeeded / failed / skipped`
- **按模型成本分级压缩** —— DCP 的 `modelMaxLimits`/`modelMinLimits` 让 pro（输入成本 3× flash）更早压缩、flash 更晚压缩；两个模型都**显式列出**，读配置即可确认分工
- **视觉输入成本封顶** —— `attachment.image` 自动缩放超大图（>1600px / >2MB 先缩放再上传），配合 flash 内部 ~800x800 降采样，避免 base64 字节浪费
- **验证预算 + 证据强度** —— 动手前设定最小非重复证据路径；"能 typecheck" 不等于行为变更的 QA
- **易变区纪律** —— 时间戳/随机 ID/动态文件列表等易变内容置于 payload 尾部，保护 DeepSeek 提示词缓存前缀
- **名册即预算** —— 每个 Agent 的 `permission.skill` 白名单同时决定 skill 名册大小；被 deny 的 skill 不进名册，也就不进每轮的常驻上下文
- **视觉 opt-in** —— 图像只在用户提供时进入 payload；非视觉任务不传图、不生成图
- **持续改进** —— reflect 机制化发现摩擦、code-review 证据门控保证质量
- **审查成本分级** —— 代码审查默认 flash 初检（省约 67%），仅在升级触发条件命中时付 pro 全价；升级时传递未验证线索，避免重复推导
