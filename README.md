# my-opencode-config

一个面向 OpenCode 的多 Agent 配置仓库，目标是在 **不新增模型、仅使用现有 3 个模型** 的前提下，提升日常使用时的可用性、易用性和稳定性。

## 当前配置概览

- 默认主 Agent：`orchestrator`
- 主模型：`deepseek/deepseek-v4-pro`
- 轻量模型：`deepseek/deepseek-v4-flash`
- 第三模型：`alibaba-cn/qwen3.7-max`
- 自动更新策略：`notify`
- 快照：已开启（`snapshot: true`）
- 上下文压缩：已开启自动压缩与历史裁剪
- 插件：`superpowers`

## 模型分工

本仓库严格限制在以下 3 个模型内进行分工，不引入其他模型：

| 模型 | 用途 |
| --- | --- |
| `deepseek/deepseek-v4-pro` | 复杂规划、重实现、代码分析、代码审查、主控调度 |
| `deepseek/deepseek-v4-flash` | 快速探索、外部检索、标题/摘要/压缩等轻量任务 |
| `alibaba-cn/qwen3.7-max` | 通用问答、轻量编排、咨询与开放式讨论 |

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

| Agent | 模型 | 作用 |
| --- | --- | --- |
| `planner` | `deepseek/deepseek-v4-pro` | 规划、架构、拆解任务 |
| `deep-worker` | `deepseek/deepseek-v4-pro` | 重实现、多文件改动、复杂调试 |
| `oracle` | `deepseek/deepseek-v4-pro` | 根因分析、深度理解代码、问题定位 |
| `reviewer` | `deepseek/deepseek-v4-pro` | 代码审查、质量检查 |
| `ui-builder` | `deepseek/deepseek-v4-pro` | 前端与 UI 相关任务 |
| `librarian` | `deepseek/deepseek-v4-flash` | 文档检索、Web 搜索、资料查询 |
| `light-orchestrator` | `alibaba-cn/qwen3.7-max` | 轻量任务、简单配置、单文件小改动 |
| `consultant` | `alibaba-cn/qwen3.7-max` | 方案讨论、最佳实践建议、权衡分析 |
| `generalist` | `alibaba-cn/qwen3.7-max` | 通用型兜底处理 |

## 内置能力补充

除自定义 `agent/*.md` 外，`opencode.json` 还为内置能力显式绑定了模型：

| 内置能力 | 模型 |
| --- | --- |
| `build` | `deepseek/deepseek-v4-pro` |
| `plan` | `deepseek/deepseek-v4-pro` |
| `general` | `alibaba-cn/qwen3.7-max` |
| `explore` | `deepseek/deepseek-v4-flash` |
| `title` | `deepseek/deepseek-v4-flash` |
| `summary` | `deepseek/deepseek-v4-flash` |
| `compaction` | `deepseek/deepseek-v4-flash` |

## 快捷命令

当前已配置以下命令别名：

| 命令 | 对应 Agent | 用途 |
| --- | --- | --- |
| `/deep` | `deep-worker` | 重实现、复杂工程任务 |
| `/quick` | `light-orchestrator` | 快速处理简单任务 |
| `/ui` | `ui-builder` | 前端/UI 工作 |
| `/review` | `reviewer` | 代码审查 |
| `/plan` | `planner` | 制定计划、技术方案 |
| `/search` | `librarian` | 外部搜索、查文档 |
| `/oracle` | `oracle` | 深度分析、问题溯源 |
| `/consult` | `consultant` | 咨询、对比、建议 |

## 稳定性与易用性优化点

本次配置重点做了以下增强：

1. **显式声明默认模型与轻量模型**
   - 降低运行时回退不确定性
   - 让标题、摘要、上下文压缩等任务固定走轻量模型

2. **为 Agent 增加 `steps` 限制**
   - 避免复杂代理无限迭代
   - 控制成本并提升响应稳定性

3. **按角色设置 `temperature`**
   - 分析/审查类角色更稳定、更确定
   - 咨询/通用类角色更灵活

4. **增加颜色标识**
   - 在 TUI 中更容易区分不同 Agent

5. **启用 `snapshot`**
   - 提升回滚与恢复体验

6. **启用 `compaction`**
   - 自动裁剪历史，降低上下文膨胀带来的不稳定

7. **补充路由与回退策略**
   - `orchestrator` 中明确了失败后的 fallback chain

## 仓库结构

```text
.
├── agent/
│   ├── consultant.md
│   ├── deep-worker.md
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

这是一个偏重 **角色分工清晰、成本可控、行为稳定** 的 OpenCode 配置，而不是追求 Agent 数量或模型数量的堆叠。
