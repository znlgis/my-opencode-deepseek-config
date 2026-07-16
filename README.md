# My OpenCode × DeepSeek Config

**OpenCode × DeepSeek 最优配置** —— 在 OpenCode 多 Agent 框架下，将 DeepSeek V4 双模型（Pro + Flash）的能力发挥到极致的配置方案。核心理念：**Token 效率优先，用最小的上下文成本达到最好的开发效果**。

## 当前配置概览

- 默认主 Agent：`orchestrator`
- 主模型：`deepseek/deepseek-v4-pro`，轻量模型：`deepseek/deepseek-v4-flash`
- 模型隔离：`enabled_providers: ["deepseek"]` + `disabled_providers` 双重锁，杜绝误入其他模型
- 会话分享：关闭（`share: "disabled"`）；快照：开启（`snapshot: true`）
- 权限基线：默认放行，破坏性 bash 命令设为 `ask`；`.env` 类敏感文件 `deny`；外部目录 `ask`
- 上下文压缩：自动压缩 + 历史裁剪（`preserve_recent_tokens: 16000`、`reserved: 10240`）
- 并行执行：`experimental.batch_tool`，支持批量工具调用
- 全局规则：`AGENTS.md`（含核心原则、Token 效率、反模式、代码风格、注释纪律等）
- 技能：`skills/` 目录下 **15 个** `SKILL.md` 技能，通过原生 `skill` 工具按需加载
- 插件：`superpowers`、`@tarquinen/opencode-dcp`
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
| `orchestrator` | v4-pro | 默认入口，意图门控（17 种模式）、Task Categories（6 类）、委托决策表、11 条纪律规则、9 条降级链 |

### Subagents

| Agent | 模型 | 权限 | 作用 |
| --- | --- | --- | --- |
| `planner` | v4-pro | 读写 | 规划、架构、拆解任务，输出 Handoff Plan |
| `deep-worker` | v4-pro | 读写 | 重型实现、多文件改动、复杂调试。禁止研究/委托，直接执行 |
| `oracle` | v4-pro | **只读** | 根因分析、深度理解代码、问题定位 |
| `reviewer` | v4-pro | **只读** | 代码审查（7 维度 + 对抗性自检+拒绝准则 + 项目上下文校准 + 决策噪声抑制）、质量检查 |
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
| `/generalist` | `generalist` | 通用兜底：处理不适合其他 agent 的杂项或模糊请求 |
| `/release` | `deep-worker`（git-release） | 准备 Tag 发布：发布说明、版本号、`gh release` |
| `/reflect` | `oracle`（reflect） | 持续改进：回顾会话摩擦点，提出最小化配置修复方案 |
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
| `gh-cli` | 用官方 `gh` CLI 操作 GitHub（PR、Issue、Release、Actions、Search 等），基于 cli/cli v2.96+ |
| `gh-skill` | 用 `gh skill` 发现、安装、更新、发布 Agent 技能 |
| `conventional-commits` | 按 Conventional Commits 规范写提交信息与 PR 标题 |
| `security-review` | 合并前对 diff 做安全审查：注入、XSS、认证、密钥、路径遍历等 |
| `code-review` | Token 高效多维度代码审查：分级、维度+严重度、对抗性自检+显式拒绝准则、设计决策噪声抑制、审查→修复循环 |
| `git-release` | 准备 Tag 发布：SemVer 推断、发布说明、`gh release create` |
| `remove-deadcode` | 安全查找并删除死代码，删除前用 LSP 验证 |
| `opencode-config` | 编写和维护本仓库 OpenCode 配置（agents、skills、commands、permissions） |
| `spec-workflow` | 规约驱动变更工作流：propose → apply → archive |
| `verify-with-docs` | 编码前按当前文档核对 API 签名，检索优先，不凭记忆 |
| `git-master` | 高级 Git：rebase、squash、fixup、blame、bisect、reflog、worktree |
| `codemap` | 生成带标注的仓库层次结构图，一站式替代盲目探索 |
| `simplify` | 行为保持的代码简化：减少嵌套、消除不必要抽象、裁剪无用变量 |
| `deepwork` | 审查门控分阶段执行：plan → review gate → implement → verify → report |
| `reflect` | 持续改进循环：回顾近期工作，发现重复摩擦，提出最小配置修复方案 |

## 设计决策与迭代记录

核心思路借鉴了 [oh-my-openagent](https://github.com/code-yeongyu/oh-my-openagent)（意图门控、只读隔离、反模式、intent/stop gate 停止条件）、[oh-my-opencode-slim](https://github.com/alvinunreal/oh-my-opencode-slim)（成本信号、结构化输出、禁止研究/委托、deepwork/reflect 工作流）、[anomalyco/opencode](https://github.com/anomalyco/opencode)（配置 Schema、Skills、`!` shell 注入、references、superpowers 插件）、[cli/cli](https://github.com/cli/cli)（gh 技能 v2.96+ 全面覆盖 30+ 命令组）、[Fission-AI/OpenSpec](https://github.com/Fission-AI/OpenSpec)（工件导向规约工作流、in-place update 动作）和 [deepreview](https://github.com/mechanai/deepreview)（对抗性自检+显式拒绝准则、项目上下文校准、文档漂移批处理、设计决策噪声抑制、finding-id 去重），纯配置实现，零额外依赖。

> **关于「借鉴而非照搬」**：anomalyco/opencode 官方通过 superpowers 插件提供的技能（brainstorming、systematic-debugging、TDD 等）已随插件自动可用，无需重复创建；其仓库自带的唯一技能 `effect`（专教 Effect TS 库）属项目专用、不通用，故不照搬。需要特别注意：anomalyco `dev` 分支采用复数键 schema（plugins/snapshots/agents），与本仓库遵循的 sst/opencode 单数键 schema（`plugin`/`snapshot`/`agent` + `enabled_providers`）不同，**不可套用**。oh-my-opencode-slim 的 worktrees/codemap/simplify、OpenSpec 的规约工作流（含 `explore`/`explorations/`）也已分别由现有 git-master/codemap/simplify/spec-workflow 覆盖。deepreview 本身过重（7 审查 agent + 校验器 + 综合器 + 规划器 + 执行器，非常耗 token），本仓库只借鉴其优点——对抗性自检、项目上下文校准、文档漂移批处理、PR 三级评论定位、文件式 IPC、设计决策噪声抑制、显式拒绝准则、finding-id 去重/原位更新——全部浓缩进现有 `code-review` 技能 + `reviewer` agent，而非移植整条流水线。anomalyco 新增的内置 subagent `scout`/`explore` 已由本仓库 explore/librarian 覆盖，其内置 `customize-opencode` 技能与本仓库 `opencode-config` 异名不冲突。遵循 Token 效率宗旨，优先精简与增强现有配置。

| 阶段 | 关键变更 |
| --- | --- |
| **v1–v6 奠基与扩展** | 双模型绑定、agent 角色/意图门控/分类规则/降级链、`AGENTS.md` 全局规则、`skills/` 目录与命令别名、权限基线、自动压缩；随后补齐 Task Categories、Discipline Rules、Model-Aware Routing、Comment Discipline、Self-Verification、Token Efficiency 章节、成本信号 Stats、模型隔离 `enabled_providers`、Code Style；技能扩到 12 个（gh-cli 对齐 cli/cli、remove-deadcode、opencode-config、spec-workflow、verify-with-docs、git-master、codemap、simplify）、命令扩到 19+，并用 cli/cli 官方版重写 gh-cli、新增 gh-skill/`skill` 命令 |
| **v7 综合优化** | 综合调研五个参考项目最新版：模型隔离双重锁、反模式章节、执行 agent 结构化输出格式 + 禁止研究/委托约束、gh-cli 扩容 Project V2/Ruleset/Cache/Agent-Task/Search 端到端覆盖、compaction reserved 提升至 8K |
| **v8 审查增强** | 借鉴 [deepreview](https://github.com/mechanai/deepreview) 优点（保持 Token 高效、纯配置）：新增 `code-review` 技能（diff 体量分级、维度+严重度清单、威胁模型校准、大 diff 落盘、审查→修复循环 + 修复后校验）、增强 `reviewer` agent、新增 `/review-loop` `/review-pr` 命令——技能 12→13、命令 19→21 |
| **v9 深度优化** | 综合调研 6 个参考项目最新版：orchestrator 增加 Delegation Decision Quick Reference 委托决策表、steps 50→25 精简；reviewer 增加 compatibility 维度 + 对抗性自检 + 项目上下文校准；gh-cli 扩至 v2.96+ 完整覆盖 30+ 命令组（Gist/Secret/Variable/Codespace/Config/Extension/Alias）+ CI 环境变量；code-review 融入对抗性自检、项目上下文校准、文档漂移批处理；新增 `deepwork`（审查门控分阶段执行）和 `reflect`（持续改进循环）技能；opencode.json 增加 `references`、compaction reserved 10K；AGENTS.md 增加自检原则——技能 13→15 |
| **v10 与时俱进** | 复查 6 个参考项目当前 HEAD，借鉴新增点并精简过时内容（零新增 agent/技能/命令）：gh-cli 补 `gh agent-task`/`gh copilot` AI 命令 + Issue 层级 GHES 版本要求；code-review 借鉴 [deepreview](https://github.com/mechanai/deepreview) 的 PR 三级评论定位（行/文件/正文）与文件式响应契约；orchestrator + ui-builder 借鉴 [oh-my-opencode-slim](https://github.com/alvinunreal/oh-my-opencode-slim) 的 design-handoff 纪律（不压平设计产出）；spec-workflow 借鉴 [OpenSpec](https://github.com/Fission-AI/OpenSpec) 当前 `/opsx:explore` 增加提案前 explore 动作 + `explorations/` 目录；修正 README 过时的模型弃用日期表述 |
| **v11 命令补全** | 修复 deep-worker.md 自相矛盾（Step 2 允许委托 vs Rules 禁止委托）、README 纪律规则计数错误（10→11）；为 `deepwork` 和 `reflect` 技能补齐 `/deepwork` `/reflect` 命令入口（此前仅通过 orchestrator 意图匹配可用，缺少命令别名） |
| **v12 审查再增强 + 精简** | 复查 6 个参考项目当前 HEAD（零新增 agent/技能/命令，遵循「精简优先于新增」）：确认 gh-cli 已覆盖 cli/cli v2.96、spec-workflow 已含 OpenSpec explore/explorations、anomalyco 仅提供项目专用 `effect` skill（不照搬，且其复数键 schema 不适用本仓库单数键 sst schema）；借鉴 [deepreview](https://github.com/mechanai/deepreview) validator 两个尚未覆盖的优点浓缩进 `code-review` 技能 + `reviewer` agent——**设计决策/上下文噪声抑制**（已知有意设计不作为 finding，等价于 `--context`）与**显式拒绝准则清单**（错误 file:line/未改动旧代码/严重度虚高/纯风格偏好/重复项 一律驳回）；精简 `reviewer.md` 与技能重复的维度/输出格式段落（token 净减）；gh-cli 小幅时效刷新（`gh release download` 公有仓库免认证、`gh skill` 交叉引用）；当前规模：11 agent / 15 技能 / 24 命令 |
| **v13 与时俱进 + 修正过时** | 复查 6 个参考项目当前 HEAD（零新增 agent/技能/命令，精简优先）：**修正 gh-cli 过时内容**——`gh copilot` 现为内置二进制透传（旧 `gh-copilot` 扩展 2025-10 弃用，删除 `explain/suggest` 陈述），补 `gh pr --reviewer @copilot`（Copilot 代码审查，v2.88+）与 `gh issue close --duplicate-of`；借鉴 [deepreview](https://github.com/mechanai/deepreview) **finding-id 去重/原位更新**浓缩进 `code-review` 技能「Posting to a PR」（loop 复审不再重复刷屏）；借鉴 [oh-my-openagent](https://github.com/code-yeongyu/oh-my-openagent) v4.18 的 **intent/stop gate**——AGENTS.md 新增「Know your stop condition」核心原则（目标达成即停，杜绝无谓打磨/多余验证，省 token）；借鉴 [OpenSpec](https://github.com/Fission-AI/OpenSpec) v1.6 的 `/opsx:update`——spec-workflow 新增 `update` 动作（原位修订工件不动实现）；确认 anomalyco `scout`/`explore` 内置 subagent 已由本仓库 explore/librarian 覆盖、其 `customize-opencode` 内置技能与本仓库 `opencode-config` 不冲突（异名）；README 版本引用刷新至 v2.96+ |

## 仓库结构

```text
.
├── agent/
│   ├── orchestrator.md        ← 主编排器（意图门控 + 委托决策表 + 成本感知路由）
│   ├── planner.md
│   ├── deep-worker.md         ← 重型实现（结构化输出 + 禁止研究/委托）
│   ├── oracle.md
│   ├── reviewer.md            ← 审查（7 维度 + 对抗性自检+拒绝准则 + 上下文校准 + 决策噪声抑制）
│   ├── consultant.md
│   ├── ui-builder.md
│   ├── explore.md
│   ├── librarian.md
│   ├── light-orchestrator.md  ← 轻量执行（结构化输出 + 禁止研究/委托）
│   └── generalist.md
├── skills/                    ← 15 个可复用技能，按需加载
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
│   └── reflect/SKILL.md       ← 持续改进：发现摩擦 → 提出最小修复
├── AGENTS.md                  ← 全局规则（含反模式、自检原则），所有 Agent 共享
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
- 复杂重构（3+ 文件）用 `/deepwork` 走审查门控流程：plan → review → implement → verify
- 定期用 `/reflect` 回顾近期工作，发现配置优化点
- 进入陌生代码库用 `/codemap` 快速建立认知，写完用 `/simplify` 精简
- 收尾时用 `/learn` 沉淀项目事实，用 `/skill` 管理技能

## 设计哲学

这是一个 **纯配置驱动** 的 OpenCode + DeepSeek 方案，核心理念：

- **角色分工清晰、成本可控、行为稳定** —— 追求配置质量而非 Agent/模型数量的堆叠
- **DeepSeek V4 双模型极致利用** —— Pro 做推理与决策，Flash 做查询与轻量执行，模型感知路由实现 cost-aware 分发
- **纯配置落地，零额外依赖** —— 所有能力由 `opencode.json` + `agent/*.md` + `skills/*/SKILL.md` + `AGENTS.md` 实现
- **Token 效率优先** —— 引用而非粘贴、技能按需加载、codemap 替代盲目探索、结构化输出减少回传 token、对抗性自检消灭假阳性
- **执行与探索分离** —— deep-worker 和 light-orchestrator 禁止研究/委托，专注执行；explore/librarian 专注探索，禁止修改
- **持续改进** —— reflect 技能机制化发现摩擦、deepwork 审查门控保证质量、自检原则防止自欺欺人
