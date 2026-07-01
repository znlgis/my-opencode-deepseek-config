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
- 上下文压缩：已开启自动压缩与历史裁剪（`preserve_recent_tokens: 16000`）
- 工具输出控制：`tool_output` 限制单次输出（max_lines: 2000, max_bytes: 51200）
- 全局规则：`AGENTS.md`（被 OpenCode 自动加载，并在 `instructions` 中显式声明）
- 并行执行：开启 `experimental.batch_tool`，支持一次发起多个工具调用
- 技能（Skills）：`skills/` 目录下的 `SKILL.md`（6 个：`gh-cli`、`conventional-commits`、`security-review`、`git-release`、`remove-deadcode`、`opencode-config`），通过原生 `skill` 工具按需加载
- 插件：`superpowers`、`@tarquinen/opencode-dcp`

## 模型分工

本仓库严格限制在以下 2 个模型内进行分工，不引入其他模型：

| 模型 | 用途 |
| --- | --- |
| `deepseek/deepseek-v4-pro` | 复杂规划、重型实现、代码分析、代码审查、主控调度、咨询与开放式讨论 |
| `deepseek/deepseek-v4-flash` | 快速探索、外部检索、标题/摘要/压缩等轻量任务、通用问答、轻量编排 |

这样的分层兼顾了：

### 路由策略

通过 orchestrator 的模型感知路由，实现 cost-aware 分发：
- **Flash 优先**：搜索、查找、简单编辑、文档生成等明确定义的任务，优先走 flash agent
- **Pro 升级路径**：flash agent 无法胜任时，自动升级到 pro（带完整上下文）
- **Pro 专注推理**：规划、架构、根因分析、代码审查、复杂实现——只用 pro
- **边界右移**：模糊任务默认走 flash 尝试，失败了才升级，不浪费 pro 资源

- **稳定性**：重任务和轻任务分流，避免所有任务都堆到高成本模型
- **可用性**：常见任务都有明确归属，减少路由不清导致的体验波动
- **易用性**：通过命令和角色划分，让使用者更容易选择正确 Agent

## Agent 结构

### Primary Agent

| Agent | 模型 | 作用 |
| --- | --- | --- |
| `orchestrator` | `deepseek/deepseek-v4-pro` | 默认入口，负责分类（含 6 类 Task Categories）、分发、兜底；含增强意图门控（12 种模式）、纪律规则（5 条）、8 条降级链 |

### Subagents

| Agent | 模型 | 权限 | 作用 |
| --- | --- | --- | --- |
| `planner` | `deepseek/deepseek-v4-pro` | 读写 | 规划、架构、拆解任务，输出可直接交给 `deep-worker` 的 Handoff Plan |
| `deep-worker` | `deepseek/deepseek-v4-pro` | 读写 | 重型实现、多文件改动、复杂调试，含 Todo 管理、代码库评估、增强自检清单、注释纪律、无死代码规则 |
| `oracle` | `deepseek/deepseek-v4-pro` | **只读** | 根因分析、深度理解代码、问题定位；含增强上下文纪律与置信度评分 |
| `reviewer` | `deepseek/deepseek-v4-pro` | **只读** | 代码审查、质量检查；含审查前检查清单、注释质量准则、security-review 技能推荐 |
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
| `/commit` | `light-orchestrator`（加载 `conventional-commits` 技能） | 用 `!` 注入 `git status`/`git diff`，暂存改动并写符合 Conventional Commits 的提交信息（不推送） |
| `/learn` | `light-orchestrator` | 把本次会话中「持久、非显而易见」的项目事实沉淀进 `AGENTS.md`，节省后续 token |
| `/rmslop` | `deep-worker`（加载 `remove-deadcode` 技能） | 清理本次改动引入的 AI slop：无意义注释、注释掉的代码、死代码（不改变行为，验证后报告） |

## 技能（Skills）

除 Agent 外，本仓库在 `skills/` 目录下提供可复用的 `SKILL.md` 技能。OpenCode 通过
原生 `skill` 工具把这些技能按需暴露给所有 Agent——Agent 只在需要时才加载完整内容，
不会一直占用上下文。

| Skill | 作用 |
| --- | --- |
| `gh-cli` | 用官方 `gh` CLI 操作 GitHub：PR、Issue、CI/Actions、Release、Projects、Label、Secret/Variable、扩展、别名、`gh api`，以及 Discussions、Organizations、Rulesets、Attestation（供应链安全）、Copilot（现为内置 Copilot CLI）、Agent Tasks、Skills（`gh skill`）、Cache、Preview 等现代命令，已对齐 [cli/cli](https://github.com/cli/cli) 最新版 |
| `conventional-commits` | 按 Conventional Commits 规范写提交信息与 PR 标题 |
| `security-review` | 合并前对 diff 做安全审查（注入、XSS、SSRF、鉴权、密钥等清单） |
| `git-release` | 准备打 Tag 的发布：依据 Conventional Commits 推断 SemVer、生成发布说明、给出 `git tag` 与 `gh release create` 命令（借鉴 [anomalyco/opencode](https://github.com/anomalyco/opencode) 文档中的 `git-release` 示例技能） |
| `remove-deadcode` | 查找并安全删除死代码（未引用的文件/导出/函数/变量/导入），删除前用工具链/LSP 验证，分批无冲突提交（借鉴 [oh-my-openagent](https://github.com/code-yeongyu/oh-my-openagent) 的 `remove-deadcode` 工作流，去掉插件/team-mode 依赖） |
| `opencode-config` | 编写本仓库的 OpenCode 配置：`opencode.json` 键、`agent/*.md` frontmatter、`SKILL.md` 格式、命令与权限约定（借鉴 anomalyco `effect` 技能「锚定权威来源」的思路，为本仓库领域提供编写指南，节省重复推导 token） |

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
| 10 | 压缩 | 自动 compaction + 历史裁剪（`tail_turns: 12`） |
| 11 | 命令 | 12 个快捷命令（`/deep` `/quick` `/ui` `/review` `/plan` `/search` `/oracle` `/consult` `/release` `/commit` `/learn` `/rmslop`）|
| 12 | 净化 | 移除不稳定的 `sequential-thinking` 与 `memory` MCP，依靠 superpowers 技能替代
| 13 | 上下文 | orchestrator 增加 Task Categories（6 类）和 Discipline Rules（5 条），强化"意图优先、保守分类、规划先行、并行委派、保持精简"
| 14 | 降级 | 扩展 Fallback Chains 从 4 条到 8 条（consultant→planner、reviewer→oracle、ui-builder→deep-worker、planner→consultant）
| 15 | 规范 | AGENTS.md 新增 Context Management、Comment Discipline（禁止 AI 套话注释）、Self-Verification（修改后自检清单）、File Creation Discipline（禁止主动创建文件）
| 16 | 质量 | AGENTS.md 新增"无死代码"和"端到端自读修改文件"两条质量要求
| 17 | 工具 | opencode.json 新增 `tool_output`（max_lines: 2000, max_bytes: 51200）和 `compaction.preserve_recent_tokens: 16000`
| 18 | 技能 | gh-cli 技能扩展至 360 行，新增 Discussions、Organizations、Rulesets、Attestation、Copilot、Agent Tasks、Skills、Preview 等命令；git-release 新增预发布和预检；conventional-commits 新增惯例检查；security-review 新增供应链验证引用
| 19 | Agent | deep-worker/oracle/reviewer/explore 均增强，增加 AGENTS.md 规则引用、自检流程、上下文纪律和注释质量标准
| 20 | 模型 | orchestrator 增加 Model-Aware Routing（5 条选择原则），所有 agent 增加 Model Leverage/Awareness 章节，明确 v4-pro 的推理深度利用和 v4-flash 的指令式执行
| 21 | 审计 | 全面审查一致性：为 planner/consultant/ui-builder/oracle/reviewer 补齐 Model Leverage，为 librarian 补齐 Model Awareness，6 个 agent 补齐 AGENTS.md 引用
| 22 | 审计 | 第二轮全面审查：修正 README tail_turns 文档不同步（9→12）；补齐 light-orchestrator 的 AGENTS.md 引用；explore 路径规则适配 Windows；加固 bash 权限（PowerShell/cmd 命令 + 空格变体 + format）；orchestrator 开放式改动明确路由、新增 explore fallback、统一模型名格式；git-release 补 git push 步骤；ui-builder 补升级提示；planner steps 20→30
| 23 | 技能 | 对齐 [cli/cli](https://github.com/cli/cli) 最新版修正 `gh-cli`：`gh copilot` 改为内置 Copilot CLI 用法（不再是 `suggest`/`explain` 扩展）、`gh skill install/preview` 补齐「repo + 技能名」两参数、新增 `gh cache` 与 `gh actions` 帮助入口
| 24 | 技能 | 新增两个技能：`remove-deadcode`（借鉴 oh-my-openagent，去插件/team-mode 依赖）与 `opencode-config`（借鉴 anomalyco `effect` 技能的领域锚定思路，为本仓库配置编写提供指南、节省重复推导 token）
| 25 | 命令 | 借鉴 anomalyco/opencode 命令并结合 OpenCode 的 `!` 内联 shell 注入，新增 `/commit`、`/learn`、`/rmslop` 三个纯配置命令（节省 token、沉淀上下文、清理 AI slop），命令数 9→12

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
│   ├── git-release/SKILL.md
│   ├── remove-deadcode/SKILL.md
│   └── opencode-config/SKILL.md
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
- 提交前用 `/commit` 生成规范提交信息，用 `/rmslop` 清理 AI slop
- 收尾时用 `/learn` 把可复用的项目事实沉淀进 `AGENTS.md`

## 说明

这是一个偏重 **角色分工清晰、成本可控、行为稳定** 的 OpenCode 配置，而不是追求 Agent 数量或模型数量的堆叠。设计原则借鉴了 [oh-my-openagent](https://github.com/code-yeongyu/oh-my-openagent) 中的核心思路（意图门控、只读隔离、并行探索、结构化输出、用 `AGENTS.md` 沉淀全局规则、用 Skills 沉淀可复用能力），参考了 [anomalyco/opencode](https://github.com/anomalyco/opencode) 最新版本的配置 Schema 与推荐用法（Skills、`permission`、`share`、命令的 `!` 内联 shell 注入等），并参考 [cli/cli](https://github.com/cli/cli) 完善了 `gh` 技能，但全部通过纯配置/提示词实现，无需引入额外依赖，也不引入新模型。之所以**只借鉴而不直接引入 oh-my-openagent**，是因为其体量大、变动频繁；这里只把"纯改 OpenCode 配置就能模仿实现"的优点吸收进来。

本轮迭代聚焦「借鉴外部最佳实践、纯配置落地」：(1) 对齐 cli/cli 最新版修正 `gh-cli` 技能中过时/不准确的命令（尤其 `gh copilot` 已变为内置 Copilot CLI）；(2) 新增 `remove-deadcode`（借鉴 oh-my-openagent、去插件依赖）与 `opencode-config`（借鉴 anomalyco `effect` 技能的领域锚定思路）两个技能；(3) 借鉴 anomalyco 命令并结合 OpenCode 的 `!` 内联 shell 注入，新增 `/commit`、`/learn`、`/rmslop` 三个命令，围绕「节省 token、沉淀上下文、清理 AI slop」提升开发效果。全程未引入新模型或新依赖。
