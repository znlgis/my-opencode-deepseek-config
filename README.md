# My OpenCode × DeepSeek Config

**OpenCode × DeepSeek 最优配置** —— 在 OpenCode 多 Agent 框架下，将 DeepSeek V4 双模型（Pro + Flash）的能力发挥到极致的配置方案。核心理念：**Token 效率优先，用最小的上下文成本达到最好的开发效果**。

## 当前配置概览

- 默认主 Agent：`orchestrator`
- 主模型：`deepseek/deepseek-v4-pro`，轻量模型：`deepseek/deepseek-v4-flash`
- 模型隔离：`enabled_providers: ["deepseek"]` + `disabled_providers` 双重锁，杜绝误入其他模型
- 会话分享：关闭（`share: "disabled"`）；快照：开启（`snapshot: true`）
- 权限基线：默认放行，破坏性 bash 命令设为 `ask`；`.env` 类敏感文件 `deny`；外部目录 `ask`
- 上下文压缩：自动压缩 + 历史裁剪（`preserve_recent_tokens: 16000`、`reserved: 8192`）
- 并行执行：`experimental.batch_tool`，支持批量工具调用
- 全局规则：`AGENTS.md`（含核心原则、Token 效率、反模式、代码风格、注释纪律等）
- 技能：`skills/` 目录下 **13 个** `SKILL.md` 技能，通过原生 `skill` 工具按需加载
- 插件：`superpowers`、`@tarquinen/opencode-dcp`

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
> 旧版 `deepseek-chat` / `deepseek-reasoner` 已于 2026/07/24 弃用，请使用 V4 系列。

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

## Agent 结构

### Primary Agent

| Agent | 模型 | 作用 |
| --- | --- | --- |
| `orchestrator` | v4-pro | 默认入口，意图门控（17 种模式）、Task Categories（6 类）、10 条纪律规则、9 条降级链 |

### Subagents

| Agent | 模型 | 权限 | 作用 |
| --- | --- | --- | --- |
| `planner` | v4-pro | 读写 | 规划、架构、拆解任务，输出 Handoff Plan |
| `deep-worker` | v4-pro | 读写 | 重型实现、多文件改动、复杂调试。禁止研究/委托，直接执行 |
| `oracle` | v4-pro | **只读** | 根因分析、深度理解代码、问题定位 |
| `reviewer` | v4-pro | **只读** | 代码审查、质量检查 |
| `ui-builder` | v4-pro | 读写 | 前端与 UI 相关任务 |
| `consultant` | v4-pro | 读写 | 方案讨论、最佳实践建议、权衡分析 |
| `explore` | v4-flash | **只读（隐藏）** | 代码库搜索、并行探索、结构化返回结果 |
| `librarian` | v4-flash | **只读（隐藏）** | 文档检索、Web 搜索、资料查询 |
| `light-orchestrator` | v4-flash | 读写 | 轻量任务、简单配置、单文件小改动。禁止研究/委托 |
| `generalist` | v4-flash | 读写 | 通用型兜底处理 |

> `explore` 和 `librarian` 标记为 `hidden: true`，仅通过 orchestrator 调度或命令别名访问。
> 
> `deep-worker` 和 `light-orchestrator` 均遵循"禁止研究、禁止委托"原则——执行而非探索，所有必要上下文由 orchestrator 提供。

## 快捷命令

| 命令 | 对应 Agent | 用途 |
| --- | --- | --- |
| `/deep` | `deep-worker` | 重型实现、复杂工程任务 |
| `/quick` | `light-orchestrator` | 快速处理简单任务 |
| `/ui` | `ui-builder` | 前端/UI 工作 |
| `/review` | `reviewer`（code-review） | 代码审查：按 diff 体量分级、维度+严重度、威胁模型校准 |
| `/review-loop` | `deep-worker`（code-review） | 审查→修复循环：修复+校验+复审，直至干净/5 轮/停滞 |
| `/review-pr` | `reviewer`（code-review + gh-cli） | 审查 PR 并把结论以待定 Review 形式回帖到 GitHub |
| `/plan` | `planner` | 制定计划、技术方案 |
| `/search` | `librarian` | 外部搜索、查文档 |
| `/oracle` | `oracle` | 深度分析、问题溯源 |
| `/consult` | `consultant` | 咨询、对比、建议 |
| `/docs` | `librarian`（verify-with-docs） | 编码前按当前文档核对 API 签名，避免幻觉 |
| `/release` | `deep-worker`（git-release） | 准备 Tag 发布：发布说明、版本号、`gh release` |
| `/commit` | `light-orchestrator`（conventional-commits） | 暂存改动并写 Conventional Commits 提交信息 |
| `/learn` | `light-orchestrator` | 把会话中可复用的项目事实沉淀进 `AGENTS.md` |
| `/rmslop` | `deep-worker`（remove-deadcode） | 清理 AI slop：无意义注释、注释掉的代码、死代码 |
| `/propose` | `planner`（spec-workflow） | 起草规约驱动的变更提案 |
| `/apply` | `deep-worker`（spec-workflow） | 按 `tasks.md` 清单逐项实现 |
| `/archive` | `light-orchestrator`（spec-workflow） | 归档已完成变更，合并 delta spec |
| `/codemap` | `explore`（codemap） | 生成带标注的仓库结构图 |
| `/simplify` | `oracle`（simplify） | 行为保持的代码简化 |
| `/skill` | `light-orchestrator`（gh-skill） | 管理 Agent 技能：搜索/预览/安装/更新/发布 |

## 技能（Skills）

OpenCode 通过原生 `skill` 工具按需暴露技能——Agent 只在需要时才加载完整内容，不会一直占用上下文。

| Skill | 作用 |
| --- | --- |
| `gh-cli` | 用官方 `gh` CLI 操作 GitHub，基于 [cli/cli](https://github.com/cli/cli) 最新版：PR/Issue/Actions/Release/Discussion/Search/Project/Ruleset/API 等 |
| `gh-skill` | 用 `gh skill` 管理 Agent 技能：搜索、预览、安装、更新、发布 |
| `conventional-commits` | 按 Conventional Commits 规范写提交信息与 PR 标题 |
| `security-review` | 合并前对 diff 做安全审查 |
| `code-review` | Token 高效的多维度代码审查：按 diff 体量分级、维度+严重度清单、威胁模型校准、审查→修复循环 |
| `git-release` | 准备打 Tag 的发布：SemVer 推断、发布说明、`gh release create` |
| `remove-deadcode` | 查找并安全删除死代码，删除前用工具链/LSP 验证 |
| `opencode-config` | 编写本仓库 OpenCode 配置的领域指南 |
| `spec-workflow` | 规约驱动的变更工作流：propose → apply → archive |
| `verify-with-docs` | 检索优先：编码前按当前文档核对 API 签名，不凭记忆 |
| `git-master` | 高级 Git 操作：rebase/squash/fixup/blame/bisect/reflog/worktree |
| `codemap` | 生成带标注的仓库层次结构图，替代盲目探索 |
| `simplify` | 行为保持的代码简化：减少嵌套、消除不必要抽象、裁剪无用变量 |

## 设计决策与迭代记录

核心思路借鉴了 [oh-my-openagent](https://github.com/code-yeongyu/oh-my-openagent)（意图门控、只读隔离、反模式）、[oh-my-opencode-slim](https://github.com/alvinunreal/oh-my-opencode-slim)（成本信号、结构化输出、禁止研究/委托）、[anomalyco/opencode](https://github.com/anomalyco/opencode)（配置 Schema、Skills、`!` shell 注入、superpowers 插件）、[cli/cli](https://github.com/cli/cli)（gh 技能全面更新）、[Fission-AI/OpenSpec](https://github.com/Fission-AI/OpenSpec)（工件导向规约工作流）和 [deepreview](https://github.com/mechanai/deepreview)（多维度审查、diff 体量分级、严重度校准、审查→修复循环），纯配置实现，零额外依赖。

> **关于「借鉴而非照搬」**：anomalyco/opencode 官方通过 superpowers 插件提供的技能（brainstorming、systematic-debugging、TDD 等）已通过插件自动可用，无需重复创建；oh-my-opencode-slim 的 worktrees/codemap/simplify、OpenSpec 的规约工作流也已分别由现有 git-master/codemap/simplify/spec-workflow 覆盖。deepreview 本身过重（7 审查 agent + 校验器 + 综合器 + 规划器 + 执行器，非常耗 token），本仓库只借鉴其优点：把多维度审查、diff 体量分级、严重度校准、审查→修复循环浓缩进单个 `code-review` 技能 + 现有 `reviewer` agent，而非移植整条流水线。遵循 Token 效率宗旨，优先精简与增强现有配置。

| 阶段 | 关键变更 |
| --- | --- |
| **v1 基础** | 双模型绑定、agent 角色定义、意图门控、分类规则、降级链、`AGENTS.md` 全局规则、`skills/` 目录、命令别名、权限基线、自动压缩 |
| **v2 增强** | Task Categories、Discipline Rules、Model-Aware Routing、Model Leverage/Awareness、Comment Discipline、Self-Verification、File Creation Discipline |
| **v3 技能扩展** | gh-cli 对齐 cli/cli、remove-deadcode、opencode-config、spec-workflow、verify-with-docs、git-master、codemap、simplify——从 4 个扩展到 12 个技能 |
| **v4 命令与工作流** | `/commit` `/learn` `/rmslop` `/propose` `/apply` `/archive` `/codemap` `/simplify` `/docs`——命令从 9 个扩展到 19 个 |
| **v5 效率优化** | Token Efficiency 章节、成本信号 Stats、模型隔离 `enabled_providers`、compaction `reserved`、Code Style 规范 |
| **v6 精简重构** | gh-cli 用 cli/cli 官方版重写、新增 gh-skill、增加 `/skill` 命令 |
| **v7 综合优化** | 综合调研五个参考项目最新版：模型隔离双重锁、反模式章节、执行 agent 结构化输出格式 + 禁止研究/委托约束、gh-cli 扩容 Project V2/Ruleset/Cache/Agent-Task/Search 端到端覆盖、compaction reserved 提升至 8K |
| **v8 审查增强** | 借鉴 [deepreview](https://github.com/mechanai/deepreview) 优点（保持 Token 高效、纯配置）：新增 `code-review` 技能（diff 体量分级、维度+严重度清单、威胁模型校准、大 diff 落盘、审查→修复循环 + 修复后校验）、增强 `reviewer` agent、新增 `/review-loop` `/review-pr` 命令——技能 12→13、命令 19→21 |

## 仓库结构

```text
.
├── agent/
│   ├── orchestrator.md        ← 主编排器（意图门控 + 成本感知路由）
│   ├── planner.md
│   ├── deep-worker.md         ← 重型实现（结构化输出 + 禁止研究/委托）
│   ├── oracle.md
│   ├── reviewer.md
│   ├── consultant.md
│   ├── ui-builder.md
│   ├── explore.md
│   ├── librarian.md
│   ├── light-orchestrator.md  ← 轻量执行（结构化输出 + 禁止研究/委托）
│   └── generalist.md
├── skills/                    ← 13 个可复用技能，按需加载
│   ├── gh-cli/SKILL.md        ← GitHub CLI 全面操作（v2.62+）
│   ├── gh-skill/SKILL.md
│   ├── conventional-commits/SKILL.md
│   ├── security-review/SKILL.md
│   ├── code-review/SKILL.md   ← Token 高效多维度代码审查（分级/校准/循环）
│   ├── git-release/SKILL.md
│   ├── remove-deadcode/SKILL.md
│   ├── opencode-config/SKILL.md
│   ├── spec-workflow/SKILL.md
│   ├── verify-with-docs/SKILL.md
│   ├── git-master/SKILL.md
│   ├── codemap/SKILL.md
│   └── simplify/SKILL.md
├── AGENTS.md                  ← 全局规则（含反模式），所有 Agent 共享
├── opencode.json
└── README.md
```

## 使用建议

- 默认直接使用 `orchestrator`，适合大多数场景
- 任务明确时，优先使用命令别名直达对应 Agent
- 复杂实现 `/deep`，要方案 `/plan`，查资料 `/search`，定位根因 `/oracle`
- 写代码前用 `/docs` 核对 API 签名，避免幻觉
- 提交前用 `/commit` 生成规范提交信息，用 `/rmslop` 清理 AI slop
- 较大变更用 `/propose` → `/apply` → `/archive` 规约驱动推进
- 收尾时用 `/learn` 沉淀项目事实，用 `/skill` 管理技能
- 进入陌生代码库用 `/codemap` 快速建立认知，写完用 `/simplify` 精简

## 设计哲学

这是一个 **纯配置驱动** 的 OpenCode + DeepSeek 方案，核心理念：

- **角色分工清晰、成本可控、行为稳定** —— 追求配置质量而非 Agent/模型数量的堆叠
- **DeepSeek V4 双模型极致利用** —— Pro 做推理与决策，Flash 做查询与轻量执行，模型感知路由实现 cost-aware 分发
- **纯配置落地，零额外依赖** —— 所有能力由 `opencode.json` + `agent/*.md` + `skills/*/SKILL.md` + `AGENTS.md` 实现
- **Token 效率优先** —— 引用而非粘贴、技能按需加载、codemap 替代盲目探索、结构化输出减少回传 token
- **执行与探索分离** —— deep-worker 和 light-orchestrator 禁止研究/委托，专注执行；explore/librarian 专注探索，禁止修改

