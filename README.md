# my-opencode-config

一个面向 OpenCode 的多 Agent 配置仓库，目标是在 **不新增模型、仅使用现有 2 个模型** 的前提下，提升日常使用时的可用性、易用性和稳定性。

## 当前配置概览

- 默认主 Agent：`orchestrator`
- 主模型：`deepseek/deepseek-v4-pro`
- 轻量模型：`deepseek/deepseek-v4-flash`
- 自动更新策略：`notify`
- 会话分享：已关闭（`share: "disabled"`，避免误分享，提升隐私）
- 快照：已开启（`snapshot: true`）
- 权限基线：默认放行；仅对破坏性 `bash` 命令（`rm -rf`、`git push --force/-f`、`git reset --hard`）设为 `ask`，对读取 `.env` 类敏感文件设为 `deny`，对工作目录外路径（`external_directory`）设为 `ask`
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
| `/release` | `deep-worker`（加载 `git-release` 技能） | 准备打 Tag 的发布：发布说明、版本号、`gh release` 命令 |

## 技能（Skills）

除 Agent 外，本仓库在 `skills/` 目录下提供可复用的 `SKILL.md` 技能。OpenCode 通过
原生 `skill` 工具把这些技能按需暴露给所有 Agent——Agent 只在需要时才加载完整内容，
不会一直占用上下文。

| Skill | 作用 |
| --- | --- |
| `gh-cli` | 用官方 `gh` CLI 操作 GitHub：PR、Issue、CI/Actions、Release、Projects、Label、Secret/Variable、扩展、别名、`gh api` 等，参考 [cli/cli](https://github.com/cli/cli) |
| `conventional-commits` | 按 Conventional Commits 规范写提交信息与 PR 标题 |
| `security-review` | 合并前对 diff 做安全审查（注入、XSS、SSRF、鉴权、密钥等清单） |
| `git-release` | 准备打 Tag 的发布：依据 Conventional Commits 推断 SemVer、生成发布说明、给出 `git tag` 与 `gh release create` 命令（借鉴 [anomalyco/opencode](https://github.com/anomalyco/opencode) 文档中的 `git-release` 示例技能） |

### 加载与发现机制

- 当本仓库作为全局配置目录（`~/.config/opencode`）部署时，`skills/<name>/SKILL.md`
  会被**自动发现**，无需在 `opencode.json` 中显式注册 `skills.paths`。
- 每个技能一个文件夹，文件名必须是全大写的 `SKILL.md`，且 frontmatter 必须包含
  `name` 与 `description`；`name` 需与所在文件夹同名（小写、连字符分隔）。
- `description` 应同时说明「做什么」和「何时触发」，并前置关键触发词，便于模型正确选用。
- 通过 `opencode.json` 的 `permission.skill`（默认 `"*": "allow"`）控制技能可见性。
- `superpowers` 插件也会贡献自己的技能（规划、TDD、调试、代码审查等），因此本仓库
  新增技能均采用不冲突的命名（技能名在所有来源中必须唯一）。

## 设计决策与迭代记录

核心设计思路借鉴了 [oh-my-openagent](https://github.com/code-yeongyu/oh-my-openagent)（意图门控、只读隔离、并行探索、结构化输出、`AGENTS.md` 沉淀全局规则）和 [anomalyco/opencode](https://github.com/anomalyco/opencode)（配置 Schema、Skills、permission），全部通过纯配置/提示词实现，不引入新依赖或模型。

| # | 类别 | 决策 |
|---|------|------|
| 1 | 模型 | 显式声明 `model`/`small_model`，所有 agent 固定绑定 `deepseek-v4-pro` 或 `deepseek-v4-flash` |
| 2 | Agent | 为各 agent 按角色设置 `steps`、`temperature`、`color`；`oracle`/`reviewer`/`explore`/`librarian` 设为只读 |
| 3 | 路由 | orchestrator 实现意图门控（Phase 0）、分类规则、Fallback Chain |
| 4 | 协议 | `AGENTS.md` 沉淀 Todo 纪律、澄清/提问格式、挑战用户机制、最小改动原则 |
| 5 | 并行 | 启用 `experimental.batch_tool`，`explore`/`librarian` 强制并发工具调用 |
| 6 | 输出 | `explore` 返回统一 `<results>` 格式，`planner` 输出统一 Handoff Plan，`oracle` 分层响应 |
| 7 | 技能 | 引入 `skills/` 目录（`gh-cli`、`conventional-commits`、`security-review`、`git-release`），通过原生 `skill` 工具按需加载 |
| 8 | 安全 | `.env` 文件读取设为 `deny`，破坏性 bash 命令（`rm -rf`、`git push -f`、`git reset --hard`）设为 `ask`，`external_directory` 设为 `ask` |
| 9 | 隐私 | `share: "disabled"`，`snapshot: true` |
| 10 | 压缩 | 自动 compaction + 历史裁剪（`tail_turns: 9`） |
| 11 | 命令 | 9 个快捷命令（`/deep` `/quick` `/ui` `/review` `/plan` `/search` `/oracle` `/consult` `/release`） |
| 12 | 净化 | 移除不稳定的 `sequential-thinking` 与 `memory` MCP，依靠 superpowers 技能替代

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
│   ├── security-review/SKILL.md
│   └── git-release/SKILL.md
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

这是一个偏重 **角色分工清晰、成本可控、行为稳定** 的 OpenCode 配置，而不是追求 Agent 数量或模型数量的堆叠。设计原则借鉴了 [oh-my-openagent](https://github.com/code-yeongyu/oh-my-openagent) 中的核心思路（意图门控、只读隔离、并行探索、结构化输出、用 `AGENTS.md` 沉淀全局规则、用 Skills 沉淀可复用能力），参考了 [anomalyco/opencode](https://github.com/anomalyco/opencode) 最新版本的配置 Schema 与推荐用法（Skills、`permission`、`share` 等），并参考 [cli/cli](https://github.com/cli/cli) 完善了 `gh` 技能，但全部通过纯配置/提示词实现，无需引入额外依赖，也不引入新模型。之所以**只借鉴而不直接引入 oh-my-openagent**，是因为其体量大、变动频繁；这里只把"纯改 OpenCode 配置就能模仿实现"的优点吸收进来。

