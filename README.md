# my-opencode-deepseek-config

**OpenCode × DeepSeek 最优配置** —— 在 OpenCode 多 Agent 框架下，将 DeepSeek V4 双模型（Pro + Flash）的能力发挥到极致的配置方案。通过精心设计的 Agent 分工、模型感知路由、技能系统和全局规则，在不新增任何模型与依赖的前提下，最大化日常开发的可用性、易用性和稳定性。

## 当前配置概览

- 默认主 Agent：`orchestrator`
- 主模型：`deepseek/deepseek-v4-pro`，轻量模型：`deepseek/deepseek-v4-flash`
- 模型隔离：`enabled_providers: ["deepseek"]`，配置层强制仅 DeepSeek，杜绝误入其他模型
- 会话分享：关闭（`share: "disabled"`）；快照：开启（`snapshot: true`）
- 权限基线：默认放行，破坏性 bash 命令（`rm -rf`、`git push -f`、`git reset --hard`、PowerShell/cmd 等价命令等）设为 `ask`；`.env` 类敏感文件读取 `deny`；外部目录 `ask`
- 上下文压缩：自动压缩 + 历史裁剪（`preserve_recent_tokens: 16000`、`reserved: 4096`）
- 并行执行：`experimental.batch_tool`，支持批量工具调用
- 全局规则：`AGENTS.md`（含 Token 效率、Code Style、Comment Discipline 等）
- 技能：`skills/` 目录下 **12 个** `SKILL.md` 技能，通过原生 `skill` 工具按需加载
- 插件：`superpowers`、`@tarquinen/opencode-dcp`、`opencode-agent-memory`

## 模型分工

本仓库严格限制在 DeepSeek V4 双模型内分工，不引入其他模型：

| 模型 | 用途 |
| --- | --- |
| `deepseek/deepseek-v4-pro` | 复杂规划、重型实现、代码分析、代码审查、主控调度、咨询与决策 |
| `deepseek/deepseek-v4-flash` | 快速探索、外部检索、轻量任务、通用问答、简单编辑 |

### 路由策略

通过 orchestrator 的模型感知路由实现 cost-aware 分发：
- **Flash 优先**：搜索、查找、简单编辑、文档生成等明确定义的任务优先走 flash agent
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
| `deep-worker` | v4-pro | 读写 | 重型实现、多文件改动、复杂调试 |
| `oracle` | v4-pro | **只读** | 根因分析、深度理解代码、问题定位 |
| `reviewer` | v4-pro | **只读** | 代码审查、质量检查 |
| `ui-builder` | v4-pro | 读写 | 前端与 UI 相关任务 |
| `consultant` | v4-pro | 读写 | 方案讨论、最佳实践建议、权衡分析 |
| `explore` | v4-flash | **只读（隐藏）** | 代码库搜索、并行探索、结构化返回结果 |
| `librarian` | v4-flash | **只读（隐藏）** | 文档检索、Web 搜索、资料查询 |
| `light-orchestrator` | v4-flash | 读写 | 轻量任务、简单配置、单文件小改动 |
| `generalist` | v4-flash | 读写 | 通用型兜底处理 |

> `explore` 和 `librarian` 标记为 `hidden: true`，仅通过 orchestrator 调度或命令别名访问。

## 快捷命令

| 命令 | 对应 Agent | 用途 |
| --- | --- | --- |
| `/deep` | `deep-worker` | 重型实现、复杂工程任务 |
| `/quick` | `light-orchestrator` | 快速处理简单任务 |
| `/ui` | `ui-builder` | 前端/UI 工作 |
| `/review` | `reviewer` | 代码审查 |
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
| `gh-cli` | 用官方 `gh` CLI 操作 GitHub，基于 [cli/cli](https://github.com/cli/cli) 最新版：PR/Issue/Actions/Release/Discussion/Search/API 等 |
| `gh-skill` | 用 `gh skill` 管理 Agent 技能：搜索、预览、安装、更新、发布 |
| `conventional-commits` | 按 Conventional Commits 规范写提交信息与 PR 标题 |
| `security-review` | 合并前对 diff 做安全审查 |
| `git-release` | 准备打 Tag 的发布：SemVer 推断、发布说明、`gh release create` |
| `remove-deadcode` | 查找并安全删除死代码，删除前用工具链/LSP 验证 |
| `opencode-config` | 编写本仓库 OpenCode 配置的领域指南 |
| `spec-workflow` | 规约驱动的变更工作流：propose → apply → archive |
| `verify-with-docs` | 检索优先：编码前按当前文档核对 API 签名，不凭记忆 |
| `git-master` | 高级 Git 操作：rebase/squash/fixup/blame/bisect/reflog/worktree |
| `codemap` | 生成带标注的仓库层次结构图，替代盲目探索 |
| `simplify` | 行为保持的代码简化：减少嵌套、消除不必要抽象、裁剪无用变量 |

## 设计决策与迭代记录

核心思路借鉴了 [oh-my-openagent](https://github.com/code-yeongyu/oh-my-openagent)（意图门控、只读隔离、并行探索）、[oh-my-opencode-slim](https://github.com/alvinunreal/oh-my-opencode-slim)（成本信号、后台调度、session 复用）、[anomalyco/opencode](https://github.com/anomalyco/opencode)（配置 Schema、Skills、`!` shell 注入）、[cli/cli](https://github.com/cli/cli)（gh 技能）和 [Fission-AI/OpenSpec](https://github.com/Fission-AI/OpenSpec)（工件导向规约工作流），纯配置实现，零额外依赖。

关键迭代里程碑：

> **关于「借鉴而非照搬」**：anomalyco/opencode 官方唯一内置推荐技能是 `customize-opencode`（编辑配置时自动加载 schema），本仓库的 `opencode-config` 技能已等价覆盖并已对齐其最新行为，故不再新增重复技能；oh-my-opencode-slim 的 `worktrees`/`codemap`/`simplify`、OpenSpec 的规约工作流也分别由现有 `git-master`/`codemap`/`simplify`/`spec-workflow` 覆盖。遵循第 6、7 条宗旨，优先精简与增强，避免为凑数而堆叠技能、徒增 token。

| 阶段 | 关键变更 |
| --- | --- |
| **v1 基础** | 双模型绑定、agent 角色定义、意图门控、分类规则、降级链、`AGENTS.md` 全局规则、`skills/` 目录、命令别名、权限基线、自动压缩 |
| **v2 增强** | Task Categories、Discipline Rules、Model-Aware Routing、Model Leverage/Awareness、Comment Discipline、Self-Verification、File Creation Discipline |
| **v3 技能扩展** | gh-cli 对齐 cli/cli、remove-deadcode、opencode-config、spec-workflow、verify-with-docs、git-master、codemap、simplify——从 4 个扩展到 11 个技能 |
| **v4 命令与工作流** | `/commit` `/learn` `/rmslop` `/propose` `/apply` `/archive` `/codemap` `/simplify` `/docs`——命令从 9 个扩展到 18 个 |
| **v5 效率优化** | Token Efficiency 章节、成本信号 Stats、模型隔离 `enabled_providers`、compaction `reserved`、Code Style 规范 |
| **v6 精简重构** | gh-cli 用 cli/cli 官方版重写（438→155 行）、新增 gh-skill、增加 `/skill` 命令、迭代记录压缩 |
| **v7 综合优化** | 综合 oh-my-openagent / OpenSpec / oh-my-opencode-slim / anomalyco/opencode / cli/cli 最新版：gh-cli 补充 `pr revert`/`update-branch`/`variable`/`attestation`/`run watch --exit-status` 等新命令、分页拼接数组与 auth 退出码等陷阱；opencode-config 对齐 skill 发现顺序、user 覆盖 built-in、权限过滤可见性、sst↔fork schema 差异；AGENTS.md 补 context hygiene 并去重；精简 6 个 pro agent 的 Model Leverage 样板，降低每次调用的 token |

## 仓库结构

```text
.
├── agent/
│   ├── orchestrator.md        ← 主编排器
│   ├── planner.md
│   ├── deep-worker.md
│   ├── oracle.md
│   ├── reviewer.md
│   ├── consultant.md
│   ├── ui-builder.md
│   ├── explore.md
│   ├── librarian.md
│   ├── light-orchestrator.md
│   └── generalist.md
├── skills/                    ← 12 个可复用技能，按需加载
│   ├── gh-cli/SKILL.md
│   ├── gh-skill/SKILL.md
│   ├── conventional-commits/SKILL.md
│   ├── security-review/SKILL.md
│   ├── git-release/SKILL.md
│   ├── remove-deadcode/SKILL.md
│   ├── opencode-config/SKILL.md
│   ├── spec-workflow/SKILL.md
│   ├── verify-with-docs/SKILL.md
│   ├── git-master/SKILL.md
│   ├── codemap/SKILL.md
│   └── simplify/SKILL.md
├── AGENTS.md                  ← 全局规则，所有 Agent 共享
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
- **Token 效率优先** —— 引用而非粘贴、技能按需加载、codemap 替代盲目探索、会话复用
