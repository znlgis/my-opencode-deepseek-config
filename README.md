# my-opencode-config

一个面向 OpenCode 的多 Agent 配置仓库，目标是在 **不新增模型、仅使用现有 2 个模型** 的前提下，提升日常使用时的可用性、易用性和稳定性。

## 当前配置概览

- 默认主 Agent：`orchestrator`
- 主模型：`deepseek/deepseek-v4-pro`
- 轻量模型：`deepseek/deepseek-v4-flash`
- 自动更新策略：`notify`
- 会话分享：已关闭（`share: "disabled"`，避免误分享，提升隐私）
- 快照：已开启（`snapshot: true`）
- 权限基线：默认放行，仅对破坏性 `bash` 命令（`rm -rf`、`git push --force/-f`、`git reset --hard`）设为 `ask`
- 上下文压缩：已开启自动压缩与历史裁剪
- 全局规则：`AGENTS.md`（被 OpenCode 自动加载，并在 `instructions` 中显式声明）
- 并行执行：开启 `experimental.batch_tool`，支持一次发起多个工具调用
- 技能（Skills）：`skills/` 目录下的 `SKILL.md`，通过原生 `skill` 工具按需加载
- 插件：`superpowers`、`@tarquinen/opencode-dcp`

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

## 技能（Skills）

除 Agent 外，本仓库在 `skills/` 目录下提供可复用的 `SKILL.md` 技能。OpenCode 通过
原生 `skill` 工具把这些技能按需暴露给所有 Agent——Agent 只在需要时才加载完整内容，
不会一直占用上下文。

| Skill | 作用 |
| --- | --- |
| `gh-cli` | 用官方 `gh` CLI 操作 GitHub：PR、Issue、CI/Actions、Release、搜索、`gh api` 等，参考 [cli/cli](https://github.com/cli/cli) |
| `conventional-commits` | 按 Conventional Commits 规范写提交信息与 PR 标题 |
| `security-review` | 合并前对 diff 做安全审查（注入、XSS、SSRF、鉴权、密钥等清单） |

### 加载与发现机制

- 当本仓库作为全局配置目录（`~/.config/opencode`）部署时，`skills/<name>/SKILL.md`
  会被**自动发现**，无需在 `opencode.json` 中显式注册 `skills.paths`。
- 每个技能一个文件夹，文件名必须是全大写的 `SKILL.md`，且 frontmatter 必须包含
  `name` 与 `description`；`name` 需与所在文件夹同名（小写、连字符分隔）。
- `description` 应同时说明「做什么」和「何时触发」，并前置关键触发词，便于模型正确选用。
- 通过 `opencode.json` 的 `permission.skill`（默认 `"*": "allow"`）控制技能可见性。
- `superpowers` 插件也会贡献自己的技能（规划、TDD、调试、代码审查等），因此本仓库
  新增技能均采用不冲突的命名（技能名在所有来源中必须唯一）。

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

### 第三轮：全局规则 + 对齐 OpenCode 推荐配置

借鉴 [oh-my-openagent](https://github.com/code-yeongyu/oh-my-openagent) 大量使用 `AGENTS.md` 沉淀共享规则的做法，并参考 [anomalyco/opencode](https://github.com/anomalyco/opencode) 最新版本的配置 Schema 与推荐用法，做了如下完善（依然是纯配置、不新增模型/依赖）：

17. **新增全局规则文件 `AGENTS.md`** — OpenCode 会自动加载它作为所有 Agent 的共享上下文，把"意图识别、最小改动、先读后写、并行执行、只读隔离、Todo 纪律、澄清与挑战协议、模型/依赖约束"等公共规则集中沉淀，各 `agent/*.md` 只保留各自独有的职责，减少重复与漂移。
18. **`instructions` 显式声明 `AGENTS.md`** — 既保证在配置目录（`~/.config/opencode`）作为全局规则自动加载，也保证作为项目级配置使用时同样生效；OpenCode 内部以绝对路径去重，不会重复加载。
19. **启用 `experimental.batch_tool`** — 对应多个 Agent 反复强调的"并行发起工具调用"，让批量读取/搜索真正落地，提升探索与实现的稳定性与速度。
20. **修正 `orchestrator` 中过期的模型表** — 原 Agent Directory 中 `consultant`/`generalist`/`light-orchestrator` 仍写着已不再使用的 `qwen3.7-max`，现已与实际配置（`deepseek-v4-pro` / `deepseek-v4-flash`）及成本列对齐。

### 第四轮：引入 Skills + 安全/隐私基线

继续借鉴 [oh-my-openagent](https://github.com/code-yeongyu/oh-my-openagent)「用技能沉淀可复用工作流」的思路，并对照 [anomalyco/opencode](https://github.com/anomalyco/opencode) 最新版本对 Skills、`permission`、`share` 的官方推荐，做了如下完善（仍是纯配置/提示词，不新增模型/依赖）：

21. **引入 `skills/` 目录与原生 Skill 机制** — 把可复用的操作型知识从 Agent 提示词中解耦出来，按需加载，避免常驻上下文膨胀。
22. **新增 `gh-cli` 技能** — 参考 [cli/cli](https://github.com/cli/cli)，覆盖 PR、Issue、CI/Actions、Release、搜索与 `gh api` 等常用操作，给出可直接复制的命令，解决官方/默认提示对 `gh` 描述过于简单的问题。
23. **新增 `conventional-commits`、`security-review` 两个推荐技能** — 与 `superpowers` 插件已有技能不重名、互补，分别覆盖提交规范与合并前安全审查。
24. **新增 `permission` 安全基线** — 默认放行，仅对破坏性 `bash` 命令（`rm -rf`、`git push --force/-f`、`git reset --hard`）设为 `ask`，借鉴 oh-my-openagent 的安全护栏但不牺牲日常可用性。
25. **关闭会话分享 `share: "disabled"`** — 对齐 OpenCode 推荐的隐私默认值，避免误把会话分享出去。
26. **在 `AGENTS.md` 中补充 Skills 使用约定** — 提示所有 Agent 优先加载相关技能，并强调技能名在所有来源中必须唯一。

## 仓库结构

```text
.
├── agent/
│   ├── consultant.md
│   ├── deep-worker.md
│   ├── explore.md          ← 提供结构化探索提示词
│   ├── generalist.md
│   ├── librarian.md
│   ├── light-orchestrator.md
│   ├── oracle.md
│   ├── orchestrator.md
│   ├── planner.md
│   ├── reviewer.md
│   └── ui-builder.md
├── skills/                 ← 可复用技能，作为全局配置目录时自动发现
│   ├── gh-cli/SKILL.md
│   ├── conventional-commits/SKILL.md
│   └── security-review/SKILL.md
├── AGENTS.md               ← 全局规则，被 OpenCode 自动加载，所有 Agent 共享
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

这是一个偏重 **角色分工清晰、成本可控、行为稳定** 的 OpenCode 配置，而不是追求 Agent 数量或模型数量的堆叠。设计原则借鉴了 [oh-my-openagent](https://github.com/code-yeongyu/oh-my-openagent) 中的核心思路（意图门控、只读隔离、并行探索、结构化输出、用 `AGENTS.md` 沉淀全局规则、用 Skills 沉淀可复用工作流），参考了 [anomalyco/opencode](https://github.com/anomalyco/opencode) 最新版本的配置 Schema 与推荐用法（Skills、`permission`、`share` 等），并参考 [cli/cli](https://github.com/cli/cli) 完善了 `gh` 技能，但全部通过纯配置/提示词实现，无需引入额外依赖，也不引入新模型。之所以**只借鉴而不直接引入 oh-my-openagent**，是因为其体量大、变动频繁；这里只把"纯改 OpenCode 配置就能模仿实现"的优点吸收进来。

