# My OpenCode × DeepSeek Config

**简体中文** | [English](README.en-US.md)

**OpenCode × DeepSeek 最优配置** —— 在 OpenCode 多 Agent 框架下，将 DeepSeek V4 模型族（Pro + Flash + Flash-Vision）的能力发挥到极致的配置方案。核心理念：**Token 效率优先，用最小的上下文成本达到最好的开发效果**。

## 当前配置概览

- 默认主 Agent：`orchestrator`
- 主模型：`deepseek/deepseek-v4-pro`，轻量模型：`deepseek/deepseek-v4-flash`，多模态模型：`deepseek/deepseek-v4-flash-vision-exp`
- 代理层级：`subagent_depth: 3`（支持 3 级代理嵌套）
- 会话分享：关闭（`share: "disabled"`）
- 权限基线：默认放行，破坏性 bash 命令设为 `ask`；`.env` 类敏感文件 `deny`；外部目录 `ask`；只读 Agent 的 bash 白名单（默认 deny 全部 + 仅放行只读子命令）
- 上下文压缩：内置 compaction（opencode.jsonc）管自动触发 + prune 裁旧工具输出，DCP（dcp.jsonc）管主动去重 + 压缩阈值，两者互补
- 全局规则：`AGENTS.md`（核心原则、任务拒绝契约、自我验证、反模式等；上下文/Token 纪律在 `AGENTS.md`）
- 技能：`skills/` 目录下 **25 个** `SKILL.md` 技能，通过原生 `skill` 工具按需加载
- 插件：`superpowers`（git URL 固定 tag `#v6.3.0`，过程型技能）、`@tarquinen/opencode-dcp`（固定版本 `@3.1.15`，智能上下文裁剪）；两者均固定版本（pin）以保证字节稳定前缀、避免自动更新导致的前缀漂移

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
  "small_model": "deepseek/deepseek-v4-flash"
}
```

本配置在 `provider` 层拆分 thinking：flash 关闭 thinking 并固定 `temperature: 0`（最快最省），pro 保持默认（thinking 开启）。多模态 `deepseek-v4-flash-vision-exp` 同为 flash 档，沿用 flash 设置。示例（flash）：

```jsonc
"provider": {
  "deepseek": {
    "models": {
      "deepseek-v4-flash": {
        "options": {
          "temperature": 0,
          "thinking": { "type": "disabled" }
        }
      },
      "deepseek-v4-flash-vision-exp": {
        "options": {
          "temperature": 0,
          "thinking": { "type": "disabled" }
        }
      }
    }
  }
}
```

> **模型 ID 命名规则**：`provider_id/model_id`，即 `deepseek/deepseek-v4-pro`、`deepseek/deepseek-v4-flash` 和 `deepseek/deepseek-v4-flash-vision-exp`。

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

## 模型分工

本仓库严格限制在 DeepSeek V4 模型族内分工，不引入其他模型——**3 个模型 × 按 Agent 分档的思考强度（thinking tier）**，而非 4 模型矩阵：

| 模型 | 用途 |
| --- | --- |
| `deepseek/deepseek-v4-pro` | 深度推理、根因分析、代码审查、重型多文件实现 |
| `deepseek/deepseek-v4-flash` | 编排/路由、规划、常规实现、咨询、UI、探索、外部检索、轻量编辑、标题/摘要/压缩 |
| `deepseek/deepseek-v4-flash-vision-exp` | 多模态：图像/截图/图表/UI 稿的理解与描述 |

思考强度由 **`reasoning_effort`** 控制——它是**请求级**的思考强度开关（`low`/`high`/`max`），**不是模型 ID**，按 Agent 经 frontmatter `options`（camelCase `reasoningEffort`，深度合并到 `model.options`）设置，不改 `model:` 字段。这样 3 模型矩阵不变，同一模型可跑不同思考档：

| 档位 | 模型 × thinking | 典型 Agent |
| --- | --- | --- |
| 低（trivial） | flash · thinking 关 | `explore`/`librarian`/`consultant`/`ui-builder`/`orchestrator` |
| 中（routine） | flash · thinking 开 + `reasoningEffort: low` | `planner`/`light-orchestrator` |
| 高（deep） | pro · 默认 high | `deep-worker`/`oracle`/`reviewer`/`solo` |

成本比：pro 输入价 3× flash（0.66 vs 0.22 / 1M tokens），故 trivial 任务绝不落到 pro。

### 路由策略

- **Trivial → flash off**：搜索、查询、咨询、UI、探索、文档检索等明确定义的轻任务走 flash agent，thinking 关闭（最省）
- **Routine-nontrivial → flash low**：规划、常规多文件实现等稍有难度的任务走 flash + `reasoningEffort: low`
- **Deep/uncertain → pro high**：深度推理、根因分析、代码审查、重型多文件实现——只用 pro
- **Vision 专责多模态**：检测到图像/截图/图表等视觉输入时，路由到 `vision` agent（flash-vision 模型）
- **自动升级**：flash agent 无法胜任时自动升级到 pro（带完整上下文）

用法示例：`「这个库怎么用」` → flash off（librarian）；`「给用户模块加导出功能」` → flash low（planner）；`「排查登录接口报错的根因」` → pro high（oracle）。

### 成本对比

价格取自 `opencode.jsonc` 的 `provider.deepseek.models`（USD / 1M tokens，2026-08-16 生效的 off-peak 价；peak 时段翻倍）。`cache_write` 无独立官方价，按 cache-miss 输入价映射：

| 模型 | 输入 | 输出 | 缓存命中（cache_read） | 缓存写入（cache_write） | 相对 flash 输入价 |
| --- | --- | --- | --- | --- | --- |
| `deepseek-v4-flash` | 0.22 | 0.66 | 0.007 | 0.22 | 1× |
| `deepseek-v4-flash-vision-exp` | 0.22 | 0.66 | 0.007 | 0.22 | 1× |
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

## Agent 结构

### Primary Agent

| Agent | 模型 | 作用 |
| --- | --- | --- |
| `orchestrator` | v4-flash | 默认入口：意图门控（Intent Gate）+ 模型感知路由 + 后备链 |
| `solo` | v4-pro（默认） | 单模型内联执行器：零委派、不用后台助手，全程在当前会话所选模型内完成 |

> `solo` 是第二个 primary agent：`permission.task: "*": "deny"`（零委派，不调用任何子智能体）、无显式 `model` 字段（跟随会话默认模型 pro）、不用 build/plan 等后台助手（它们跑在内置 flash 上，会破坏全程单模型的保证），分析、规划、实现、验证全部在当前会话内联完成。

### Subagents

| Agent | 模型 | 权限 | 作用 |
| --- | --- | --- | --- |
| `planner` | v4-flash | 读写 | 规划、架构、拆解任务 |
| `deep-worker` | v4-pro | 读写 | 重型实现、多文件改动、复杂调试 |
| `oracle` | v4-pro | **只读** | 根因分析、深度理解代码 |
| `reviewer` | v4-pro | **只读** | 单遍代码审查（证据门控） |
| `ui-builder` | v4-flash | 读写 | 前端与 UI 相关任务 |
| `consultant` | v4-flash | 读写 | 方案讨论、最佳实践建议 |
| `explore` | v4-flash | **只读** | 代码库搜索、并行探索 |
| `librarian` | v4-flash | **只读** | 文档检索、Web 搜索 |
| `light-orchestrator` | v4-flash | 读写 | 轻量任务、单文件编辑 |
| `vision` | v4-flash-vision-exp | 读写 | 多模态：图像/截图/图表/UI 稿理解 |

> `deep-worker` 和 `light-orchestrator` 遵循"禁止研究、禁止委托"原则——执行而非探索，上下文由 orchestrator 提供。`deep-worker` 另带 "What you DON'T handle" 拒绝契约：琐碎单文件编辑 → 拒接（路由 `light-orchestrator`）、纯研究/查询 → 拒接（路由 `oracle`/`explore`）、任何 flash 能完成的任务 → 拒接（pro 是 3× flash）。
>
> 只读 Agent（`oracle`/`reviewer`/`explore`）真只读化：`edit: deny` + bash 白名单（默认 deny 全部，仅放行 `git status/diff/log/show/blame/grep`、`rg` 等只读子命令；`oracle`/`reviewer` 另允许 `gh pr view/diff`、`gh issue view`、`gh api` 以支持 `/review` 回帖）。`librarian` 更严格：`bash: "*": deny`，无任何 bash 白名单。
>
> 各 agent 带 `skills` 白名单（默认 deny + 按职责放行，防误加载重型 skill）：`orchestrator` → `codemap`/`grilling`/`wait-what`/`grill-with-docs`；`planner` → `spec-workflow`/`codebase-design`；`deep-worker` → `remove-deadcode`/`spec-workflow`/`git-release`/`to-tickets`/`triage`/`git-master`/`resolving-merge-conflicts`/`opencode-config`/`writing-for-agents`/`diagnosing-bugs`/`codebase-design`/`domain-modeling`；`oracle` → `reflect`/`simplify`/`diagnosing-bugs`；`reviewer` → `code-review`/`security-review`/`gh-cli`；`explore` → `codemap`；`librarian` → `verify-with-docs`；`light-orchestrator` → `handoff`/`simplify`/`spec-workflow`；`consultant` → `domain-modeling`；`ui-builder`/`vision`/`solo` 无白名单。
>
> **思考分档（thinking tiers）**：`reasoning_effort` 是按 Agent 经 frontmatter `options` 设置的请求级思考强度（`low`/`high`/`max`），不是模型 ID。`explore`/`librarian`/`consultant`/`ui-builder`/`orchestrator` = flash · thinking 关（最省）；`planner`/`light-orchestrator` = flash · thinking 开 + `reasoningEffort: low`；`deep-worker`/`oracle`/`reviewer`/`solo` = pro · 默认 high。

## 快捷命令

### Agent 路由命令

| 命令 | Agent | 用途 |
| --- | --- | --- |
| `/deep` | `deep-worker` | 重型实现、多文件改动 |
| `/quick` | `light-orchestrator` | 轻量任务、单文件编辑 |
| `/ui` | `ui-builder` | 前端/UI 工作 |
| `/vision` | `vision` | 多模态：图像/截图/图表理解 |
| `/review` | `reviewer`（code-review + gh-cli） | 本地 diff 或 PR 审查：带 PR ref/URL 时回帖 GitHub（event=COMMENT），否则审查本地 diff 并按严重度分级报告；scope-first 门控（>500 有效行或琐碎 diff 先报 scoped 计划并停止） |
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
| `code-review` | 单遍代码审查 + 证据门控；大 diff（>~500 行）拆 Standards/Spec 两轴合并报告 |
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
| `grilling` | 需求对齐访谈：一次一问、多选优先，歧义收敛后再动手 |
| `wait-what` | 用户消息难懂时先一句话重述确认，再动手 |
| `writing-for-agents` | 写给 agent 看的文档（skill/AGENTS.md/指针文档）的写作杠杆 |
| `to-tickets` | 把 spec/plan 拆解为可追踪的 GitHub issue（每单一个可独立完成+验收的单元，带验收标准） |
| `triage` | 基于 label 的 issue 分流：拉取 → 分类 → 打标签/派单（gh），只分流不改内容 |
| `diagnosing-bugs` | 系统化排障：先搭紧致的红态反馈回路再理论化 → 复现最小化 → 3-5 个可证伪假设 → 单变量插桩（`[DEBUG-<hex>]` 标记）→ 正确接缝处修复 + 回归测试 → 清理 |
| `codebase-design` | 架构词汇表：module/interface/depth/seam/adapter/leverage/locality，删除测试、深度测试，评估模块边界是否合理 |
| `domain-modeling` | 主动领域建模：维护 CONTEXT.md 术语表（仅词汇，不含实现细节），会话中挑战/锐化模糊术语，仅在必要时提议 ADR；含重复解释触发——同一概念被反复解释时落一条术语 |
| `grill-with-docs` | 组合 `grilling` + `domain-modeling`：需求歧义且领域术语模糊时，一次一问收敛意图并同步锐化术语表 |

## 仓库结构

```text
├── opencode/          # OpenCode 配置目录（agents/、skills/、opencode.jsonc、AGENTS.md、dcp.jsonc）
├── scripts/           # sync-config.ps1（同步到全局配置）+ validate-jsonc.js（JSONC 校验）
├── README.md          # 简体中文（默认）
├── README.en-US.md    # English
└── LICENSE
```

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

**代码审查：**
```text
/review #123   ← PR 模式：审查 PR + 回帖 GitHub（gh-cli，event=COMMENT）
/review        ← 本地 diff 模式：按严重度分级报告（scope-first 门控）
```

## 借鉴来源

核心思路借鉴 [oh-my-openagent](https://github.com/code-yeongyu/oh-my-openagent)（意图门控、只读隔离、反模式）、[oh-my-opencode-slim](https://github.com/alvinunreal/oh-my-opencode-slim)（调度器优先、后备链、拒绝契约、提示词缓存安全）、[anomalyco/opencode](https://github.com/anomalyco/opencode)（配置 Schema、技能体系）、[cli/cli](https://github.com/cli/cli)（gh v2.100 命令集）、[OpenSpec](https://github.com/Fission-AI/OpenSpec)（delta specs）、[mattpocock/skills](https://github.com/mattpocock/skills)（冲突解析、交接文档、排障/架构/领域建模技能）、[pi](https://github.com/earendil-works/pi)（先答后改、精简响应）、[deepreview](https://github.com/mechanai/deepreview)（有效大小路由）。纯配置实现，零额外依赖。**借鉴而非照搬**：只汲取轻量化设计理念，精简优先于新增。

## 设计哲学

- **纯配置驱动，零额外依赖** —— 所有能力由 `opencode.jsonc` + `agents/*.md` + `skills/*/SKILL.md` + `AGENTS.md` 实现
- **DeepSeek V4 模型族极致利用** —— Pro 做深度推理与重型实现，Flash 做路由、规划与常规执行，Flash-Vision 专责多模态
- **Token 效率优先** —— 路径引用替代粘贴文件、技能按需加载、压缩分级管理
- **插件增效但不喧宾夺主** —— superpowers 提供过程纪律，DCP（dcp.jsonc）主动去重+压缩阈值，内置 compaction（opencode.jsonc）自动触发+prune 兜底；两插件均固定版本（pin）以保字节稳定前缀，避免自动更新导致前缀漂移
- **执行与探索分离** —— deep-worker/light-orchestrator 禁止研究/委托，explore/librarian 禁止修改
- **缓存与 thinking 纪律** —— 静态前缀稳定以命中 DeepSeek 提示词缓存；flash 关 thinking + temperature 0（provider 层），pro 默认 thinking 开
- **Scope First + Delegate Always** —— 先定范围（2+ 步/多文件/架构变更先走 planner），再委派执行，顶层 token 只留给路由与难题
- **原子 TODO** —— 多步任务先写有序 TODO，逐条 in_progress→completed；格式 `path: action for scenario — verify by check`
- **按模型成本分级压缩** —— DCP 的 `modelMaxLimits`/`modelMinLimits` 让 pro（输入成本 3× flash）更早压缩、flash 更晚压缩，用更小的上下文窗口换取更省的压缩点
- **视觉输入成本封顶** —— `attachment.image` 自动缩放超大图（>1600px / >2MB 先缩放再上传），配合 vision-exp 内部 ~800x800 降采样，避免 base64 字节浪费
- **验证预算 + 证据强度** —— 动手前设定最小非重复证据路径；"能 typecheck" 不等于行为变更的 QA
- **易变区纪律** —— 时间戳/随机 ID/动态文件列表等易变内容置于 payload 尾部，保护 DeepSeek 提示词缓存前缀
- **持续改进** —— reflect 机制化发现摩擦、code-review 证据门控保证质量
