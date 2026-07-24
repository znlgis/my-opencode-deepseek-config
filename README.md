# My OpenCode × DeepSeek Config

**OpenCode × DeepSeek 最优配置** —— 在 OpenCode 多 Agent 框架下，将 DeepSeek V4 双模型（Pro + Flash）的能力发挥到极致的配置方案。核心理念：**Token 效率优先，用最小的上下文成本达到最好的开发效果**。

## 当前配置概览

- 默认主 Agent：`orchestrator`
- 主模型：`deepseek/deepseek-v4-pro`，轻量模型：`deepseek/deepseek-v4-flash`
- 代理层级：`subagent_depth: 3`（支持 3 级代理嵌套）
- 模型隔离：`enabled_providers: ["deepseek"]` + `disabled_providers` 双重锁
- 会话分享：关闭（`share: "disabled"`）；快照：开启（`snapshot: true`）
- 权限基线：默认放行，破坏性 bash 命令设为 `ask`；`.env` 类敏感文件 `deny`；外部目录 `ask`
- 上下文压缩：自动压缩 + 历史裁剪（`preserve_recent_tokens: 12000`、`reserved: 10240`）
- 全局规则：`AGENTS.md`（核心原则、任务拒绝契约、Token 效率、缓存安全、证据纪律、反模式等）
- 技能：`skills/` 目录下 **16 个** `SKILL.md` 技能，通过原生 `skill` 工具按需加载
- 插件：`superpowers`（14 个过程型技能）、`@tarquinen/opencode-dcp`（智能上下文裁剪，专为 DeepSeek V4 调优）
- 上下文管理：DCP 主动压缩 + OpenCode 原生 compaction 兜底，双保险防溢出
- 外部参考：`references` 指向 `anomalyco/opencode` 官方文档仓库

## DeepSeek 模型配置

### 前置条件

- OpenCode ≥ v1.14.24（DeepSeek provider 为内置，无需额外安装）
- DeepSeek API Key：[platform.deepseek.com/api_keys](https://platform.deepseek.com/api_keys) 申请

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

OpenCode 内置 `deepseek` provider，默认连接 `https://api.deepseek.com`。以下为本仓库 `opencode.json` 中的核心模型配置：

```jsonc
{
  "model": "deepseek/deepseek-v4-pro",
  "small_model": "deepseek/deepseek-v4-flash",
  "enabled_providers": ["deepseek"],
  "disabled_providers": ["openai", "anthropic", "google", "openrouter"]
}
```

如需为 Pro 模型启用 thinking/reasoning 增强，可在 `provider` 中追加：

```jsonc
"provider": {
  "deepseek": {
    "models": {
      "deepseek-v4-pro": {
        "options": {
          "thinking": { "type": "enabled" }
        }
      }
    }
  }
}
```

> **模型 ID 命名规则**：`provider_id/model_id`，即 `deepseek/deepseek-v4-pro` 和 `deepseek/deepseek-v4-flash`。
> 旧版 `deepseek-chat` / `deepseek-reasoner` 已被 V4 系列取代，请统一使用 V4 系列 ID。

## 安装部署

本仓库是一个**纯配置驱动**的 OpenCode 配置文件集，安装即生效，无需额外依赖。

### 前置条件

- 已安装 [OpenCode](https://opencode.ai) ≥ v1.14.24
- 已配置 DeepSeek API Key（见上方「前置条件」中的 TUI 或环境变量方式）

### 方式一：克隆到全局配置目录（推荐）

直接将本仓库克隆为全局 OpenCode 配置目录。克隆后 Agent、技能、命令和全局规则即时生效，适用于个人开发环境。

**Windows（PowerShell）：**

```powershell
# 1. 确保 ~/.config 目录存在
New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\.config"

# 2. 克隆到全局配置目录
git clone https://github.com/<your-username>/my-opencode-deepseek-config.git "$env:USERPROFILE\.config\opencode"

# 3. 验证：启动 opencode，应加载本仓库的配置
opencode
```

**Linux / macOS：**

```bash
git clone https://github.com/<your-username>/my-opencode-deepseek-config.git ~/.config/opencode
```

> **兼容性说明**：`~/.config/opencode` 是 OpenCode 的标准全局配置路径。本仓库的 `agent/`、`skills/`、`AGENTS.md` 等文件遵循 OpenCode 的约定布局，克隆后无需额外配置即可被自动识别。

### 方式二：克隆到任意位置 + 环境变量

如果你希望将配置文件单独存放（例如放在工作目录中），克隆后设置 `OPENCODE_CONFIG_DIR` 环境变量指向仓库路径：

```bash
git clone https://github.com/<your-username>/my-opencode-deepseek-config.git /path/to/opencode-config
```

**Windows（PowerShell，临时生效）：**

```powershell
$env:OPENCODE_CONFIG_DIR = "D:\path\to\opencode-config"
opencode
```

**Windows（永久生效）：**

```powershell
[Environment]::SetEnvironmentVariable("OPENCODE_CONFIG_DIR", "D:\path\to\opencode-config", "User")
```

**Linux / macOS：**

```bash
export OPENCODE_CONFIG_DIR=/path/to/opencode-config
# 永久生效：将上述行追加到 ~/.bashrc 或 ~/.zshrc
```

### 方式三：仅加载特定配置文件

如果你已有全局配置，只想复用本仓库的部分配置（如 `opencode.json`），可设置 `OPENCODE_CONFIG` 指向具体文件：

```bash
export OPENCODE_CONFIG=/path/to/opencode-config/opencode.json
```

> 这种方式只会加载 `opencode.json` 中的配置项，不会自动加载 `agent/`、`skills/` 等目录。如果你需要完整的 Agent + 技能体系，请使用方式一或方式二。

### 验证安装

启动 OpenCode，确认以下内容：

```bash
opencode
```

1. 在 TUI 中输入 `/models`，确认当前模型为 `deepseek/deepseek-v4-pro`，轻量模型为 `deepseek/deepseek-v4-flash`
2. 查看 agent 列表（`/agents` 或 @ 菜单），应能看到 `orchestrator`（默认）、`planner`、`deep-worker`、`oracle`、`reviewer` 等 10 个 Agent
3. 输入任意请求，观察 Orchestrator 是否自动分析意图并路由到合适的 Agent
4. 尝试输入 `/review`、`/plan`、`/search` 等命令，确认对应 Agent 正常启动

如果模型未加载或 Agent 未出现，检查：
- `opencode.json` 中的 `enabled_providers` 是否包含 `deepseek` 且未被 `disabled_providers` 覆盖
- DeepSeek API Key 是否正确配置（`opencode` → `/connect`）
- 克隆路径是否为 `~/.config/opencode`，或 `OPENCODE_CONFIG_DIR` 是否正确设置

## 模型分工

本仓库严格限制在 DeepSeek V4 双模型内分工，不引入其他模型：

| 模型 | 用途 |
| --- | --- |
| `deepseek/deepseek-v4-pro` | 复杂规划、重型实现、代码分析、代码审查、主控调度、咨询与决策 |
| `deepseek/deepseek-v4-flash` | 快速探索、外部检索、轻量任务、通用问答、简单编辑 |

### 路由策略

通过 orchestrator 的模型感知路由实现 cost-aware 分发：
- **Flash 优先**：搜索、查找、简单编辑等明确定义的任务优先走 flash agent
- **Pro 升级路径**：flash agent 无法胜任时自动升级到 pro（带完整上下文）
- **Pro 专注推理**：规划、架构、根因分析、代码审查、复杂实现——只用 pro
- **成本信号**：Agent Directory 为每个 agent 标注相对成本/速度信号，让路由决策显式权衡
- **委托决策表**：orchestrator 内置 Delegation Decision Quick Reference，明确每个 agent 的「何时委托 / 何时不委托」边界

## Agent 结构

### Primary Agent

| Agent | 模型 | 作用 |
| --- | --- | --- |
| `orchestrator` | v4-pro | 默认入口，意图门控、Task Categories（6 类）、模型感知路由、多级代理深度 |

### Subagents

| Agent | 模型 | 权限 | 作用 |
| --- | --- | --- | --- |
| `planner` | v4-pro | 读写 | 规划、架构、拆解任务，输出 Handoff Plan |
| `deep-worker` | v4-pro | 读写 | 重型实现、多文件改动、复杂调试。禁止研究/委托，直接执行 |
| `oracle` | v4-pro | **只读** | 根因分析、深度理解代码、问题定位 |
| `reviewer` | v4-pro | **只读** | 代码审查（多维度 + 对抗性自检 + 项目上下文校准）、质量检查 |
| `ui-builder` | v4-pro | 读写 | 前端与 UI 相关任务 |
| `consultant` | v4-pro | 读写 | 方案讨论、最佳实践建议、权衡分析 |
| `explore` | v4-flash | **只读（隐藏）** | 代码库搜索、并行探索、结构化返回结果 |
| `librarian` | v4-flash | **只读（隐藏）** | 文档检索、Web 搜索、资料查询 |
| `light-orchestrator` | v4-flash | 读写 | 轻量任务、简单配置、单文件小改动。禁止研究/委托 |

> `explore` 和 `librarian` 标记为 `hidden: true`，仅通过 orchestrator 调度或命令别名访问。
>
> `deep-worker` 和 `light-orchestrator` 均遵循"禁止研究、禁止委托"原则——执行而非探索，所有必要上下文由 orchestrator 提供。

## 快捷命令

| 命令 | 对应 Agent | 用途 |
| --- | --- | --- |
| `/deep` | `deep-worker` | 重型实现、复杂工程任务 |
| `/deepwork` | `deep-worker`（deepwork） | 审查门控分阶段执行：plan → review gate → implement → verify → report |
| `/quick` | `light-orchestrator` | 快速处理简单任务 |
| `/ui` | `ui-builder` | 前端/UI 工作 |
| `/review` | `reviewer`（code-review） | 代码审查：按 diff 体量分级、7 维度+严重度、威胁模型校准、对抗性自检 |
| `/review-loop` | `deep-worker`（code-review） | 审查→修复循环：修复+校验+复审，直至干净/5 轮/停滞 |
| `/review-pr` | `reviewer`（code-review + gh-cli） | 审查 PR 并把结论以待定 Review 形式回帖到 GitHub |
| `/plan` | `planner` | 制定计划、技术方案 |
| `/search` | `librarian` | 外部搜索、查文档 |
| `/oracle` | `oracle` | 深度分析、问题溯源 |
| `/consult` | `consultant` | 咨询、对比、建议 |
| `/docs` | `librarian`（verify-with-docs） | 编码前按当前文档核对 API 签名，避免幻觉 |
| `/release` | `deep-worker`（git-release） | 准备 Tag 发布：发布说明、版本号、`gh release` |
| `/reflect` | `oracle`（reflect） | 持续改进：回顾会话摩擦点，提出最小化配置修复方案 |
| `/commit` | `light-orchestrator`（conventional-commits） | 暂存改动并写 Conventional Commits 提交信息 |
| `/learn` | `light-orchestrator` | 把会话中可复用的项目事实沉淀进 `AGENTS.md` |
| `/rmslop` | `deep-worker`（remove-deadcode） | 清理 AI slop：无意义注释、注释掉的代码、死代码 |
| `/explore` | `explore`（spec-workflow explore） | 提案前探索：研究现状、勾画方案、暴露问题，不锁定提案方向 |
| `/propose` | `planner`（spec-workflow） | 起草规约驱动的变更提案 |
| `/apply` | `deep-worker`（spec-workflow） | 按 `tasks.md` 清单逐项实现 |
| `/archive` | `light-orchestrator`（spec-workflow） | 归档已完成变更，合并 delta spec |
| `/codemap` | `explore`（codemap） | 生成带标注的仓库结构图 |
| `/simplify` | `oracle`（simplify） | 行为保持的代码简化 |
| `/skill` | `light-orchestrator`（gh-skill） | 管理 Agent 技能：搜索/预览/安装/更新/发布 |
| `/verify-plan` | `deep-worker`（verification-planning） | 实现前规划验证路径：选择最窄但足够充分的证明方式 |

## 技能（Skills）

OpenCode 通过原生 `skill` 工具按需暴露技能——Agent 只在需要时才加载完整内容，不会一直占用上下文。

| Skill | 作用 |
| --- | --- |
| `gh-cli` | 用官方 `gh` CLI 操作 GitHub（PR、Issue、Release、Actions、Search 等），基于 cli/cli v2.96+ |
| `gh-skill` | 用 `gh skill` 发现、安装、更新、发布 Agent 技能 |
| `conventional-commits` | 按 Conventional Commits 规范写提交信息与 PR 标题 |
| `security-review` | 合并前对 diff 做安全审查：注入、XSS、认证、密钥、路径遍历等 |
| `code-review` | Token 高效多维度代码审查：分级、维度+严重度、对抗性自检+显式拒绝准则、项目上下文校准、文档漂移、审查→修复循环 |
| `git-release` | 准备 Tag 发布：SemVer 推断、发布说明、`gh release create` |
| `remove-deadcode` | 安全查找并删除死代码，删除前用 LSP 验证 |
| `opencode-config` | 编写和维护本仓库 OpenCode 配置（agents、skills、commands、permissions） |
| `spec-workflow` | 规约驱动变更工作流：explore → propose → apply → verify → archive，含更新 vs 新建决策框架 |
| `verify-with-docs` | 编码前按当前文档核对 API 签名，检索优先，不凭记忆 |
| `git-master` | 高级 Git：rebase、squash、fixup、blame、bisect、reflog、worktree |
| `codemap` | 生成带标注的仓库层次结构图，一站式替代盲目探索 |
| `simplify` | 行为保持的代码简化：减少嵌套、消除不必要抽象、裁剪无用变量 |
| `deepwork` | 审查门控分阶段执行：plan → review gate → implement → verify → report |
| `reflect` | 持续改进循环：回顾近期工作，发现重复摩擦，提出最小配置修复方案 |
| `verification-planning` | 实现前规划最窄验证路径——在编码前确定如何证明变更正确，选择最窄但足够充分的验证方式 |

## 设计决策与迭代记录

核心思路借鉴了 [oh-my-openagent](https://github.com/code-yeongyu/oh-my-openagent)（意图门控、只读隔离、反模式）、[oh-my-opencode-slim](https://github.com/alvinunreal/oh-my-opencode-slim)（成本信号、结构化输出、禁止研究/委托、cache safety、session 复用）、[anomalyco/opencode](https://github.com/anomalyco/opencode)（配置 Schema、Skills、subagent_depth）、[cli/cli](https://github.com/cli/cli)（gh 完整命令集、--json+--jq 结构化输出）、[Fission-AI/OpenSpec](https://github.com/Fission-AI/OpenSpec)（delta specs、enablers not gates、progressive rigor）和 [deepreview](https://github.com/mechanai/deepreview)（上下文注入校准、反面论证自检、三层 placement、strict response contract），纯配置实现，零额外依赖。

> **借鉴而非照搬**：外部分支采用复数键 schema 与本仓库单数键不兼容，不套用；过重的流水线（7 审查 agent + 验证器）只汲取其轻量化设计理念；冗余功能由现有 agent/skill 覆盖，不新增。遵循"精简优先于新增"原则，每次迭代都以净减 token 为目标。

### 迭代里程碑

| 阶段 | 关键变更 |
| --- | --- |
| **v1-v7（奠基）** | 双模型绑定、agent 角色体系、意图门控/分类路由、AGENTS.md 全局规则、skills 目录与命令别名、权限基线、12 技能 / 19+ 命令、模型隔离双重锁 |
| **v8-v12（审查+规约）** | 借鉴 deepreview 增强 code-review（分级/校准/自检/拒绝准则）、借鉴 OpenSpec 建立 spec-workflow（explore→propose→apply→archive）、新增 deepwork/reflect/verification-planning 技能、gh-cli 对齐 v2.96+ |
| **v13-v14（契约+精简）** | AGENTS.md 新增 Evidence Discipline / Task Rejection Contract / stop condition 原则；全量去重审核 11 个 agent prompt 与全局规则的重复内容 |
| **v15（高效执行）** | 补齐后台子 agent 错误核查纪律、收敛审查→修复循环退出条件；当前规模：11 agent / 16 技能 / 26 命令 |
| **v16（Agent 清理）** | 移除所有神话名称、合并 4 张路由表为 1 张统一表、gh-cli 扩至 Issues 2.0、新增 `/verify-plan` 命令 |
| **v17（精准增强）** | spec-workflow 增加 verify 动作 / 更新 vs 新建框架 / Open Questions；reviewer 自动项目上下文校准 |
| **v18（激进重构）** | 移除 generalist 代理（由 light-orchestrator 覆盖）、所有 agent prompt 精简 30-50%（移除与 AGENTS.md 重复的 Model Leverage 段落）、AGENTS.md 新增 cache safety / 压缩Skills列表、opencode.json 新增 subagent_depth:3、6 个 skill 清理过时上游引用、README 迭代记录从 15 条压缩至本表；当前规模：**10 agent / 16 技能 / 25 命令** |

## 仓库结构

```text
.
├── agent/
│   ├── orchestrator.md        ← 主编排器（意图门控 + 委托决策表 + 成本感知路由）
│   ├── planner.md
│   ├── deep-worker.md         ← 重型实现（结构化输出 + 禁止研究/委托）
│   ├── oracle.md
│   ├── reviewer.md            ← 审查（多维度 + 对抗性自检 + 上下文校准）
│   ├── consultant.md
│   ├── ui-builder.md
│   ├── explore.md
│   ├── librarian.md
│   └── light-orchestrator.md  ← 轻量执行（结构化输出 + 禁止研究/委托）
├── skills/                    ← 16 个可复用技能，按需加载
│   ├── gh-cli/SKILL.md        ← GitHub CLI 全面操作（v2.96+，含 agent-task/copilot）
│   ├── gh-skill/SKILL.md
│   ├── conventional-commits/SKILL.md
│   ├── security-review/SKILL.md
│   ├── code-review/SKILL.md   ← Token 高效多维度审查（分级/校准/自检+拒绝准则/噪声抑制/循环）
│   ├── git-release/SKILL.md
│   ├── remove-deadcode/SKILL.md
│   ├── opencode-config/SKILL.md
│   ├── spec-workflow/SKILL.md
│   ├── verify-with-docs/SKILL.md
│   ├── git-master/SKILL.md
│   ├── codemap/SKILL.md
│   ├── simplify/SKILL.md
│   ├── deepwork/SKILL.md      ← 审查门控分阶段复杂任务执行
│   ├── reflect/SKILL.md       ← 持续改进：发现摩擦 → 提出最小修复
│   └── verification-planning/SKILL.md  ← 实现前规划最窄验证路径
├── AGENTS.md                  ← 全局规则（含反模式、自检原则），所有 Agent 共享
├── opencode.json
└── README.md
```

## 使用指南

本配置提供两种使用模式：**Orchestrator 自动路由**（推荐日常使用）和**命令别名直达**（任务明确时更高效）。两种模式共享同一套 Agent 体系和技能体系，可根据场景灵活切换。

### 模式一：Orchestrator 自动路由（默认）

安装后启动 `opencode`，默认 Agent 为 `orchestrator`。你只需用自然语言描述需求，Orchestrator 会自动分析意图、选择最合适的 Agent 和模型执行，并将结果汇总返回。

```text
# 示例：描述需求，Orchestrator 自动分发

「帮我排查这个登录接口的报错」    → oracle 分析根因 → 返回诊断报告
「优化这段循环，性能太差了」        → oracle 分析 → deep-worker 实施优化
「这个 PR 帮我审查一下」            → reviewer 多维度审查 → 返回分级报告
「我想给用户模块加个导出功能」      → planner 制定方案 → deep-worker 实现
「React 19 的 use() API 怎么用」    → librarian 查文档 → 返回签名和示例
「帮我写个按钮组件的样式」          → ui-builder 设计并实现
```

这种模式下你不需要关心 Agent 怎么选、模型怎么分——Orchestrator 内置的意图门控（17 种模式）和模型感知路由会自动做出 cost-aware 的决策。

### 模式二：命令别名直达

当任务明确时，用斜杠命令直接路由到对应 Agent，跳过意图分析，一步到位：

| 场景 | 命令 | 效果 |
| --- | --- | --- |
| 复杂实现 / 多文件改动 | `/deep` | 直达 `deep-worker`，专注执行 |
| 轻量修改 / 单文件编辑 | `/quick` | 直达 `light-orchestrator`，快速完成 |
| 制定技术方案 / 架构设计 | `/plan` | 直达 `planner`，输出 Handoff Plan |
| 排查 Bug / 深度分析 | `/oracle` | 直达 `oracle`，追溯根因 |
| 代码审查 | `/review` | 直达 `reviewer`，多维度分级报告 |
| 外部搜索 / 查 API | `/search` | 直达 `librarian`，检索返回 |
| 前端 / UI 工作 | `/ui` | 直达 `ui-builder`，专注界面 |
| 方案讨论 / 对比取舍 | `/consult` | 直达 `consultant`，分析选项 |
| 兜底 / 杂项任务 | `/quick` | 直达 `light-orchestrator`，灵活处理 |

### 典型工作流示例

#### 1. 开发新功能

```text
/plan   ← 先制定方案（输出架构设计、文件清单、实现步骤）
/deep   ← 按方案落地实现（多文件改动、复杂逻辑）
/review ← 实现完成后审查代码（多维度报告 + 严重度分级）
/commit ← 生成规范提交信息，准备提交
```

较大功能建议走**规约驱动工作流**：

```text
/explore  ← 探索现状，分析可行方案，不锁定方向
/propose  ← 起草变更提案（proposal + specs + tasks）
/apply    ← 按 tasks.md 清单逐项实现
/review   ← 审查实现成果
/archive  ← 归档并合并 delta spec
```

#### 2. 排查 Bug

```text
/oracle    ← 描述现象，让 oracle 追溯根因
/deep      ← 拿到诊断结论后，让 deep-worker 修复
/rmslop    ← 清理修复过程中产生的 slop 代码
/commit    ← 生成修复提交
```

#### 3. 代码审查（PR review）

```text
/review-pr  ← 审查 PR 并自动将分级报告以 Review 形式回帖到 GitHub
/review-loop  ← 审查当前改动 → 自动修复 → 再审查，直到干净或 5 轮上限
```

#### 4. 持续改进与维护

```text
/reflect  ← 定期回顾近期会话，发现重复摩擦，提出配置优化方案
/codemap  ← 进入陌生代码库时快速建立项目认知
/simplify ← 精简刚写完的代码（减少嵌套、消除冗余抽象）
/rmslop   ← 清理 AI 产生的无意义注释、注释掉的代码、死代码
/learn    ← 把会话中可复用的项目事实沉淀进 AGENTS.md
/skill    ← 搜索、安装、更新 Agent 技能
```

#### 5. 发布准备

```text
/release  ← 自动分析 PR 合并记录 → 推断 SemVer 版本 → 生成发布说明 → 输出 gh release 命令
```

### 编写代码时的最佳实践

- **实现前核对文档**：用 `/docs <库名>` 检索当前版本 API 签名，避免幻觉
- **实现前规划验证路径**：用 `/verify-plan` 明确如何证明变更正确，而非写完再想
- **匹配模型到任务**：轻量编辑走 `/quick`，复杂逻辑走 `/deep`——让 Flash 做简单的事，Pro 做推理
- **审查后再提交**：`/review` 先查一遍，再 `/commit`——减少问题流入仓库
- **精简即交付**：写完代码记得 `/simplify` 和 `/rmslop`——去除不必要的复杂性

## 设计哲学

这是一个 **纯配置驱动** 的 OpenCode + DeepSeek 方案，核心理念：

- **角色分工清晰、成本可控、行为稳定** —— 追求配置质量而非 Agent/模型数量的堆叠
- **DeepSeek V4 双模型极致利用** —— Pro 做推理与决策，Flash 做查询与轻量执行，模型感知路由实现 cost-aware 分发
- **纯配置落地，零额外依赖** —— 所有能力由 `opencode.json` + `agent/*.md` + `skills/*/SKILL.md` + `AGENTS.md` 实现
- **插件增效但不喧宾夺主** —— superpowers 提供过程纪律（先 brainstorm 再实现、先 debug 再修复），DCP 智能压缩替代简单截断
- **执行与探索分离** —— deep-worker 和 light-orchestrator 禁止研究/委托，专注执行；explore/librarian 专注探索，禁止修改
- **持续改进** —— reflect 技能机制化发现摩擦、deepwork 审查门控保证质量、自检原则防止自欺欺人
