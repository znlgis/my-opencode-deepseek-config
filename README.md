# my-opencode-config

一个面向 OpenCode 的多 Agent 配置仓库，目标是在 **不新增模型、仅使用现有 2 个模型** 的前提下，提升日常使用时的可用性、易用性和稳定性。

## 当前配置概览

- 默认主 Agent：`orchestrator`
- 主模型：`deepseek/deepseek-v4-pro`
- 轻量模型：`deepseek/deepseek-v4-flash`
- 自动更新策略：`notify`
- 快照：已开启（`snapshot: true`）
- 上下文压缩：已开启自动压缩与历史裁剪
- 插件：`superpowers`

## 模型分工

本仓库严格限制在以下 2 个模型内进行分工，不引入其他模型：

| 模型 | 用途 |
| --- | --- |
| `deepseek/deepseek-v4-pro` | 复杂规划、重型实现、代码分析、代码审查、主控调度、咨询与开放式讨论 |
| `deepseek/deepseek-v4-flash` | 快速探索、外部检索、标题/摘要/压缩等轻量任务、通用问答、轻量编排 |

这样的分层兼顾了：

- **稳定性**：重任务和轻任务分流，避免所有任务都堆到高成本模型
- **可用性**：常见任务都有明确归属，减少路由不清导致的体验波动
- **易用性**：通过命令和角色划分，让使用者更容易选择正确 Agent

## Agent 结构

### Primary Agent

| Agent | 模型 | 作用 |
| --- | --- | --- |
| `orchestrator` | `deepseek/deepseek-v4-pro` | 默认入口，负责分类、分发、兜底 |

### Subagents

| Agent | 模型 | 权限 | 作用 |
| --- | --- | --- | --- |
| `planner` | `deepseek/deepseek-v4-pro` | 读写 | 规划、架构、拆解任务，输出可直接交给 `deep-worker` 的 Handoff Plan |
| `deep-worker` | `deepseek/deepseek-v4-pro` | 读写 | 重型实现、多文件改动、复杂调试，含 Todo 管理与代码库评估 |
| `oracle` | `deepseek/deepseek-v4-pro` | **只读** | 根因分析、深度理解代码、问题定位，不修改文件 |
| `reviewer` | `deepseek/deepseek-v4-pro` | **只读** | 代码审查、质量检查，不修改文件 |
| `ui-builder` | `deepseek/deepseek-v4-pro` | 读写 | 前端与 UI 相关任务 |
| `explore` | `deepseek/deepseek-v4-flash` | **只读（隐藏）** | 代码库搜索、并行探索、结构化返回结果 |
| `librarian` | `deepseek/deepseek-v4-flash` | **只读（隐藏）** | 文档检索、Web 搜索、资料查询 |
| `light-orchestrator` | `deepseek/deepseek-v4-flash` | 读写 | 轻量任务、简单配置、单文件小改动 |
| `consultant` | `deepseek/deepseek-v4-pro` | 读写 | 方案讨论、最佳实践建议、权衡分析 |
| `generalist` | `deepseek/deepseek-v4-flash` | 读写 | 通用型兜底处理 |

> `explore` 和 `librarian` 标记为 `hidden: true`，不出现在 `@` 自动补全菜单中，仅通过 orchestrator 调度或命令别名访问。

## 内置能力补充

除自定义 `agent/*.md` 外，`opencode.json` 还为内置能力显式绑定了模型：

| 内置能力 | 模型 |
| --- | --- |
| `build` | `deepseek/deepseek-v4-pro` |
| `plan` | `deepseek/deepseek-v4-pro` |
| `general` | `deepseek/deepseek-v4-flash` |
| `explore` | `deepseek/deepseek-v4-flash` |
| `title` | `deepseek/deepseek-v4-flash` |
| `summary` | `deepseek/deepseek-v4-flash` |
| `compaction` | `deepseek/deepseek-v4-flash` |

## 快捷命令

当前已配置以下命令别名：

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

## 稳定性与易用性优化点

### 第一轮：基础配置优化

1. **显式声明默认模型与轻量模型** — 降低运行时回退不确定性
2. **为 Agent 增加 `steps` 限制** — 控制成本并提升响应稳定性
3. **按角色设置 `temperature`** — 分析类更稳定，咨询类更灵活
4. **增加颜色标识** — 在 TUI 中更容易区分不同 Agent
5. **启用 `snapshot`** — 提升回滚与恢复体验
6. **启用 `compaction`** — 自动裁剪历史，降低上下文膨胀带来的不稳定
7. **补充路由与回退策略** — orchestrator 中明确了失败后的 Fallback Chain

### 第二轮：借鉴 oh-my-openagent 的行为增强

8. **IntentGate（意图门控）** — orchestrator 在分类前先识别用户真实意图，防止字面理解错误
9. **Todo 管理机制** — 多步任务强制写待办列表、逐步推进，避免中途停工
10. **澄清协议** — 规范了"何时提问、如何提问"的格式，减少无效猜测
11. **挑战用户机制** — 发现明显设计问题时，主动提出替代方案而非盲目执行
12. **只读权限隔离** — `oracle`、`reviewer`、`explore`、`librarian` 配置了 `permission.edit/write: deny`，防止误修改文件
13. **隐藏工具 Agent** — `explore`、`librarian` 设置 `hidden: true`，不污染 `@` 菜单
14. **并行执行强调** — `explore`、`librarian` 明确要求同时发起多个工具调用
15. **结构化输出格式** — `explore` 返回统一 `<results>` 格式，`planner` 输出统一 Handoff Plan 格式
16. **Oracle 响应结构** — 分层输出（底线/行动方案/扩展/边界条件）+ 决策框架

## 仓库结构

```text
.
├── agent/
│   ├── consultant.md
│   ├── deep-worker.md
│   ├── explore.md          ← 新增，提供结构化探索提示词
│   ├── generalist.md
│   ├── librarian.md
│   ├── light-orchestrator.md
│   ├── oracle.md
│   ├── orchestrator.md
│   ├── planner.md
│   ├── reviewer.md
│   └── ui-builder.md
├── opencode.json
└── README.md
```

## 使用建议

- 默认直接使用 `orchestrator`，适合大多数场景
- 任务明确时，优先使用命令别名直达对应 Agent
- 复杂实现优先 `/deep`
- 要方案不要代码时优先 `/plan`
- 查资料优先 `/search`
- 定位根因优先 `/oracle`
- 需要建议或权衡时优先 `/consult`

## 说明

这是一个偏重 **角色分工清晰、成本可控、行为稳定** 的 OpenCode 配置，而不是追求 Agent 数量或模型数量的堆叠。设计原则借鉴了 [oh-my-openagent](https://github.com/code-yeongyu/oh-my-openagent) 中的核心思路（意图门控、只读隔离、并行探索、结构化输出），但通过纯配置/提示词实现，无需引入额外依赖。

