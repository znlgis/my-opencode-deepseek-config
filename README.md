# My OpenCode × DeepSeek Config

**简体中文** | [English](README.en-US.md)

**OpenCode v2 × DeepSeek 最优配置** —— 在 OpenCode v2 多 Agent 框架下，将 DeepSeek V4 模型族（Pro + Flash）的能力发挥到极致的配置方案。核心理念：**Token 效率优先，用最小的上下文成本达到最好的开发效果**。

## 当前配置概览

- 默认主 Agent：`orchestrator`
- 主模型：`deepseek/deepseek-v4-pro`，轻量/多模态模型：`deepseek/deepseek-flash`（V4.1 Flash，原生多模态）
- 代理层级：`experimental.subagent_depth: 2`（v2 原生键；v1 的顶层 `subagent_depth` 已被 v2 丢弃，本仓库不再写入。恰好覆盖实际最深链路 `orchestrator → light-orchestrator → oracle`；其余 subagent 一律禁止委派，更深层级只是白送 token 放大面）
- 会话分享：关闭（`share: "disabled"`）
- 权限基线：默认放行，破坏性 bash 命令设为 `ask`；`.env` 类敏感文件 `deny`；外部目录 `ask`；只读 Agent 的 bash 白名单（默认 deny 全部 + 仅放行只读子命令）
- 上下文压缩：**仅用内置 compaction**（opencode.jsonc）——`limit.input` 显式声明工作窗口，触发点 = `limit.input − compaction.buffer`（flash `131072−16000=115,072`、pro `163840−16000=147,840` tokens），保留尾部 `keep.tokens: 12000`；v1 拼写（`prune`/`tail_turns`/`reserved`/`preserve_recent_tokens`）在 v2 下不生效，已全部移除；无第三方压缩插件
- 全局规则：`AGENTS.md`（核心原则、任务拒绝契约、自我验证、反模式等；上下文/Token 纪律在 `AGENTS.md`）
- 技能：`skills/` 目录下 **23 个** `SKILL.md` 技能，通过原生 `skill` 工具按需加载；各 Agent 再用 `permission.skill` 白名单裁剪名册（名册的 name+description 是**每轮常驻**成本，故按职责最小化）
- 命令：**18 个**快捷命令（Agent 路由 / 操作 / 内联 / 规约四类），见下文
- 插件：仅 `superpowers`（git URL 固定 tag `#v6.4.2`，过程型技能；经 v2 原生 `plugins` 数组加载）；固定版本（pin）以保证字节稳定前缀、避免自动更新导致的前缀漂移。v6.4.2（2026-09-25 发布）已在桌面 v2 的 CLI 2.0.24 实测加载（`plugin list` → version `8ca22db`）；且**子会话（task subagent）不再注入 bootstrap**

### 插件与模型映射（重要）

唯一保留的插件 **superpowers v6.4.2** 是纯 skill 注入插件（`.opencode/plugins/superpowers.js`），**不提供模型映射能力**，模型路由只能在 Agent 层完成——本配置已如此实现，无需也无法在插件内指定模型。v6.4.2 的映射表写在 `AGENTS.md` 的 Plugins 小节（skill → Agent → 档位），并显式标注「本地已有等价 skill 的项不接线」，避免两套流程并存。

> 已移除 `@tarquinen/opencode-dcp`：它的两块价值（绝对值阈值提前压缩、工具调用去重）现在由内置 compaction 的**显式模型窗口**（`providers.deepseek.models.*.limit.input` + `buffer`）覆盖；少一个插件就少一条前缀漂移与启动开销路径。缓存、`dcp.jsonc` 与 `compress` 权限均已清理。

因此"规划/架构/复杂审查用 pro，执行/初检/文档/批量/视觉用 flash"这一分工，全部由 `agents/*.md` 的 `model:` 字段与 thinking tiers 实现（见下文路由策略），而非插件配置。此结论已核实插件源码（v6.4.2 复核：插件仍只做 skills 路径注册 + bootstrap 注入，无任何模型配置项），勿重复调研。

## DeepSeek 模型配置

### 前置条件

- OpenCode v2（桌面版内置 CLI 2.0.24 实测通过；1.18.x 及更早版本**不再支持**）
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
  "model": "deepseek/deepseek-v4-pro"
}
```

本配置在 `providers` 层拆分 thinking：flash 关闭 thinking 并固定 `temperature: 0`（最快最省），pro 保持默认（thinking 开启）。`deepseek-flash` 是合并后的 V4.1 Flash 模型——原文本版与视觉版已合并为一个原生多模态模型，因此由它声明 `capabilities`（图像输入）。示例（flash，v2 原生键）：

```jsonc
"providers": {
  "deepseek": {
    "models": {
      "deepseek-flash": {
        "settings": {
          "temperature": 0,
          "thinking": { "type": "disabled" }
        },
        "capabilities": {
          "tools": true,
          "input": ["text", "image"],
          "output": ["text"]
        }
      }
    }
  }
}
```

> **模型 ID 命名规则**：`provider_id/model_id`，即 `deepseek/deepseek-v4-pro` 和 `deepseek/deepseek-flash`。

### v2 原生键名（本仓库唯一写法）

本仓库为 **OpenCode v2 专属配置**（1.18.x 不再支持）。v2 加载配置文件有两条路径：**原生解码**，或——当文件含任一 v1 触发键时——官方的 **V1→V2 迁移路径**（`isV1` 触发键：`logLevel`/`server`/`command`/`reference`/`snapshot`/`plugin`/`autoshare`/`disabled_providers`/`enabled_providers`/`small_model`/`mode`/`agent`/`provider`/`permission`/`tools`/`attachment`/`layout`；源码 `packages/core/src/v1/config/migrate.ts`）。本仓库**统一走原生路径**——上述键一个都不写：

| 关注点 | 本仓库写法（v2 原生） | 要点 |
| --- | --- | --- |
| 插件 | `plugins`（字符串或 `{package, options}`） | superpowers 使用字符串形式 |
| Provider/模型 | `providers.<id>.models.<mid>.{settings, capabilities, cost, limit}` | `settings` = 请求透传（temperature/thinking）；`capabilities` = `{tools, input[], output[]}`；`cost` 为**数组** + 嵌套 `cache: {read, write}`；`limit.input` 驱动压缩触发点 |
| Agent / 命令 | `agents` / `commands` | 内联覆盖内置 agent；文件型 agent 保持 `agents/*.md` |
| 权限 | `permissions`（有序 `{action, resource, effect}` 列表） | last-match-wins；v2 action 名：`shell`（非 `bash`）、`subagent`（非 `task`） |
| 附件 | `media.image` | 原 `attachment` |
| 压缩 | `{auto, keep: {tokens}, buffer}` | `prune`/`tail_turns`/`reserved`/`preserve_recent_tokens` 为 v1 拼写，v2 不读，已移除 |
| 嵌套深度 | `experimental.subagent_depth` | 顶层 `subagent_depth` 已被 v2 丢弃 |
| small_model | —（不写） | v2 将其展开为 `title` agent 模型；本仓库直接钉 `title` |

迁移验证（2026-10-07）：迁移前后用 v2.0.24 `debug config`/`debug agents` 做逐字段 A/B——归一化输出完全等价（详见「本次重构变更记录」）。注意：`$schema` 保持 `https://opencode.ai/config.json`（v2 应用自身写入的就是该 URL）。

## 快速开始

```powershell
# 1. 克隆
git clone https://github.com/znlgis/my-opencode-deepseek-config.git

# 2. 让 opencode 使用本仓库的配置目录（当前会话生效）
$env:OPENCODE_CONFIG_DIR = "D:\path\to\my-opencode-deepseek-config\opencode"

# 3. 配置 API Key（二选一：TUI 里 /connect，或环境变量）
$env:DEEPSEEK_API_KEY = "sk-your-key-here"

# 4. 启动
opencode
```

启动后自检三条：`/models` 显示 `deepseek/deepseek-v4-pro`；Agent 列表有 12 个自定义 Agent；随便描述一个需求，`orchestrator` 会自动分类并路由（想跳过路由就直接用 `/quick`、`/deep`、`/review` 等命令）。

改完仓库文件后需要同步到全局配置目录（`~/.config/opencode` 是独立副本，不是符号链接）：

```powershell
.\scripts\sync-config.ps1
```

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

> **注意**：`skills/`、`agents/`、`commands/` 的删除清理走 git 索引 + 历史记录。刚 `git rm` 但**尚未提交**的删除不会被识别为“待清理文件”，此时需手动删一次全局目录里的同名副本（提交后再跑一次同步即可自动对账）。

## 模型分工

本仓库严格限制在 DeepSeek V4 模型族内分工，不引入其他模型——**2 个模型 × 按 Agent 分档的思考强度（thinking tier）**，而非多模型矩阵：

| 模型 | 用途 |
| --- | --- |
| `deepseek/deepseek-v4-pro` | 深度推理、根因分析、代码审查、重型多文件实现 |
| `deepseek/deepseek-flash` | 编排/路由、规划、常规实现、咨询、UI、探索、外部检索、轻量编辑、标题/摘要/压缩；原生多模态（图像/截图/图表/UI 稿理解） |

思考强度由 **`reasoning_effort`** 控制——它是**请求级**的思考强度开关（`low`/`high`/`max`），**不是模型 ID**，按 Agent 经 frontmatter `options`（camelCase `reasoningEffort`，深度合并到 `model.options`）设置，不改 `model:` 字段。这样 2 模型矩阵不变，同一模型可跑不同思考档：

| 档位 | 模型 × thinking | 典型 Agent |
| --- | --- | --- |
| 低（trivial） | flash · thinking 关 | `explore`/`librarian`/`consultant`/`ui-builder`/`orchestrator` |
| 中（routine） | flash · thinking 开 + `reasoningEffort: low` | `planner`/`light-orchestrator` |
| 高（deep） | pro · 默认 high | `deep-worker`/`oracle`/`reviewer` |
| 跟随会话 | 会话所选模型（默认 pro） | `solo`（无 `model` 字段，思考档随该模型） |

成本比：pro 输入价 4.4× flash（0.66 vs 0.15 / 1M tokens）、输出价 3.3×，故 trivial 任务绝不落到 pro。

### 路由策略

- **Trivial → flash off**：搜索、查询、咨询、UI、探索、文档检索等明确定义的轻任务走 flash agent，thinking 关闭（最省）
- **Routine-nontrivial → flash low**：规划、常规多文件实现等稍有难度的任务走 flash + `reasoningEffort: low`
- **Deep/uncertain → pro high**：深度推理、根因分析、重型多文件实现——只用 pro
- **代码审查 → flash 初检，pro 升级**：`/review` 默认走 flash 初检（Abbreviated 路径），仅在升级触发条件命中时委派 `reviewer`（pro）；`/deep-review` 强制 pro 全量审查
- **Vision 专责多模态**：仅在用户明确提供图像/截图或明确要求时，路由到 `vision` agent（`deepseek-flash`，原生多模态）。**视觉输入是 opt-in**：非视觉任务不主动传图、不生成图、不调用视觉能力（`AGENTS.md`「Constraints」已把这条写成硬约束）；附件统一先经 `media.image` 缩到 1600px / 2MiB，避免 base64 字节浪费
- **自动升级**：flash agent 无法胜任时自动升级到 pro（带完整上下文）

**模型解析链（v2.0.24 源码核验 + 本机实测）**：`agent.model` 只在三处决定实际模型——① subagent/子会话（含 `/` 命令：命令绑定 subagent agent 时会**创建子会话**并按该 agent 档位运行，如 `/deep`→pro、`/quick`→flash，当前会话模型不受影响）；② title/summary/compaction 一次性调用；③ 桌面 App 新会话的随 agent 联动默认值（build/plan/orchestrator → flash）。主会话自身按 `-m` > 会话已存模型 > 全局 `model`（pro）解析：桌面 App 用模型选择器切换（选择被记住）；headless `opencode run` **不读** `agent.model`——要 flash 必须显式加 `-m deepseek/deepseek-flash`（实测：不加 = pro ×2，加了 = flash）。

用法示例：`「这个库怎么用」` → flash off（librarian）；`「给用户模块加导出功能」` → flash low（planner）；`「排查登录接口报错的根因」` → pro high（oracle）；`/review` → flash 初检（小 diff 直接出报告）；`/review #123`（大 diff / 触及信任边界）→ flash 初检后升级 pro。

#### 代码审查的两级模型（轻量化 Review）

| 层级 | 模型 | 执行者 | 覆盖范围 |
| --- | --- | --- | --- |
| Tier 1（默认） | `deepseek-flash` | `light-orchestrator`（`/review`） | Abbreviated 路径：≤8 个逻辑文件且 ≤300 有效行，且无高风险触发 |
| Tier 2（升级） | `deepseek-v4-pro` | `reviewer`（`/deep-review` 或自动升级） | Full 路径、高风险触发、或 Tier 1 发现需跨文件确认的 critical/major |

升级触发条件（命中任一即升级）：路径为 Full（大 diff 或高风险正则命中）；Tier 1 发现 critical/major 但无法仅凭 diff 确认影响；diff 触及信任边界（同时加载 `security-review`）；用户明确要求深度审查。

Tier 1 报告是**完整审查**而非预览——干净结果不因"再确认一下"而升级。升级时把 Tier 1 发现作为**未验证线索**传给 Tier 2，让 pro 确认而非重新推导，避免重复 token 消耗。

### 成本对比

价格取自 `opencode.jsonc` 的 `providers.deepseek.models`（USD / 1M tokens，2026-10-07 官方页核验的 off-peak 价；peak 时段翻倍）。`cache_write` 无独立官方价，按 cache-miss 输入价映射：

| 模型 | 输入 | 输出 | 缓存命中（cache_read） | 缓存写入（cache_write） | 相对 flash 输入价 |
| --- | --- | --- | --- | --- | --- |
| `deepseek-flash` | 0.15 | 0.60 | 0.003 | 0.15 | 1× |
| `deepseek-v4-pro` | 0.66 | 1.98 | 0.022 | 0.66 | 4.4× |

两个成本杠杆：

- **模型档位**：pro 输入价 4.4× flash、输出价 3.3×，故 trivial 任务绝不落到 pro（见上文路由策略）。
- **提示词缓存**：`cache_read` 比输入价便宜 **50×（flash 0.003 vs 0.15）/ 30×（pro 0.022 vs 0.66）**。本配置的字节稳定前缀 + 易变区纪律（见 `AGENTS.md`）正是为了最大化缓存命中率。

**典型会话成本估算**（假设 200K 输入 tokens，其中 150K 命中缓存，30K 输出）：

| 模型 | 缓存命中 | 输入未命中 | 输出 | 合计 |
| --- | --- | --- | --- | --- |
| flash | 150K × 0.003 = $0.0005 | 50K × 0.15 = $0.0075 | 30K × 0.60 = $0.018 | **≈ $0.026** |
| pro | 150K × 0.022 = $0.003 | 50K × 0.66 = $0.033 | 30K × 1.98 = $0.059 | **≈ $0.096** |

同一 token 量下 pro ≈ 3.7× flash。可用 `scripts/estimate-cost.js` 按实际 token 数估算。

#### 优化前后成本对比

以"审查一个 300 有效行的本地 diff"为例（假设 60K 输入 tokens，其中 45K 命中缓存，8K 输出）：

| 方案 | 模型 | 缓存命中 | 输入未命中 | 输出 | 合计 |
| --- | --- | --- | --- | --- | --- |
| 优化前（`/review` 固定 pro） | pro | 45K × 0.022 = $0.001 | 15K × 0.66 = $0.010 | 8K × 1.98 = $0.016 | **≈ $0.027** |
| 优化后（`/review` flash 初检） | flash | 45K × 0.003 = $0.0001 | 15K × 0.15 = $0.0023 | 8K × 0.60 = $0.0048 | **≈ $0.007** |
| 优化后（升级到 pro 全量） | pro | 45K × 0.022 = $0.001 | 15K × 0.66 = $0.010 | 8K × 1.98 = $0.016 | **≈ $0.027** |

**节省比例**：小 diff 走 flash 初检约省 **73%**（$0.027 → $0.007）；只有命中升级触发条件时才付 pro 全价，且升级时传递未验证线索避免重复推导。

其他优化项的 token 节省（字节均为 LF 计数，即仓库实际存储大小）：

| 优化项 | 变更 | 节省 |
| --- | --- | --- |
| `AGENTS.md` + `orchestrator.md` 精简（上一轮） | 合计 29851 → 26843 字节（`AGENTS.md` 15173→14067、`orchestrator.md` 14678→12776） | 每轮常驻上下文省 **3008 字节 ≈ 752 tokens（−10.1%）**；该前缀每轮都加载，收益随会话轮数线性放大 |
| 本轮常驻前缀增量（2026-09-20） | 合计 26843 → 28470 字节（`AGENTS.md` +1383、`orchestrator.md` +244、`planner`/`deep-worker`/`light-orchestrator` 各 +27/+81/+44） | 每轮多 **1627 字节 ≈ 407 tokens**（冷启动按输入价，命中缓存后按 `cache_read` 价）——由下一条抵消 |
| 插件升到 v6.4.1：子会话不再注入 bootstrap | 每个 task 子会话 −3110 字节 | 每委派 1 个子智能体即省 **≈ 780 tokens**；只要链路里有委派（本仓库默认如此），本轮增量即被抵掉且净赚 |
| Skills 名册瘦身 | 25 → 23 个（合并 `wait-what`/`grill-with-docs` 进 `grilling`） | 名册 name+description 常驻成本 **10,127 → 9,802 字节**（−325 字节 ≈ −81 tokens），且少两个可能选错的入口 |
| `subagent_depth` 3 → 2 | 覆盖实际最深链路即可 | 关掉未使用的第 3 层嵌套，避免意外 token 放大 |
| 移除 DCP + 压缩窗口显式化 | 删插件与 `dcp.jsonc`；改由 `limit.input` 声明工作窗口（flash 115K / pro 148K） | 单次请求可携带的上下文上限从隐式 **968K** 降到 **115K/148K**（约 1/8）；缓存未命中时按同比例省钱，且少一个第三方插件 |
| 内置 agent 档位固定 | title/summary/compaction + `general` 等 subagent 目标固定 flash；桌面 App 新会话的 build/plan/orchestrator 默认选中 flash 档 | 高频路径单次调用输入价降至 pro 的 **1/4.4**（0.15 vs 0.66）；headless `run` 需显式 `-m deepseek/deepseek-flash`（见「模型解析链」） |

> 说明：常驻前缀的变化在**缓存未命中**的请求上体现为全额 token 差异，命中缓存时按 `cache_read` 价（flash 0.003 / pro 0.022 per 1M）计——但前缀越小，命中率越高、压缩触发越晚，两者叠加才是节省的完整来源。

**实测基线**（本机 2026-09-20，精简后配置）：

```powershell
opencode run "Reply with exactly: OK" --agent orchestrator --format json
```

| 指标 | 上一轮实测 | 本轮实测（2026-09-20，最终配置 + v6.4.1） |
| --- | --- | --- |
| 首轮 prompt tokens | 14,690（未命中 8,161 + 命中缓存 6,528） | **15,153**（未命中 11,825 + 命中缓存 3,328） |
| output tokens | 1 | 1 |
| 单次费用 | $0.00184 | **$0.00263**（flash，off-peak 价） |
| 插件 A/B（同一配置目录，仅 pin 不同） | — | v6.3.0 **15,159** vs v6.4.1 **15,183** prompt tokens → Δ+24 ≈ 噪声，升级本身不增加常驻成本 |
| 对比：移除 DCP 前同一命令 | 16,254 tokens（cache read 0）→ **−1,564（≈ −9.6%）** | — |

单次费用受**缓存命中率**支配（同一配置连跑两次，命中部分在 256～6,528 tokens 之间浮动），因此跨轮比较要看总 prompt tokens：**14,690 → 15,153（+463，≈ +3.2%）**，与上面的字节账（+1627 字节 ≈ +407 tokens）吻合。

这 15,153 tokens 里，`AGENTS.md`（15,450 B）+ `orchestrator.md`（13,020 B）≈ 6.9K tokens，其余是 opencode 基础系统提示与工具 schema——**本仓库能直接控制的就是前面这部分**，所以「精简提示词」是唯一能持续压缩常驻成本的手段。复现这条命令即可核对当前常驻开销。

## Agent 结构

### Primary Agent

| Agent | 模型 | 作用 |
| --- | --- | --- |
| `orchestrator` | flash | 默认入口：意图门控（Intent Gate）+ 模型感知路由 + 后备链 |
| `solo` | v4-pro（默认） | 单模型内联执行器：零委派、不用后台助手，全程在当前会话所选模型内完成 |

> `solo` 是第二个 primary agent：`permission.task: "*": "deny"`（零委派，不调用任何子智能体）、无显式 `model` 字段（跟随会话所选模型，默认 pro）、不用 build/plan 等内置后台助手（其档位只在子会话/一次性调用生效，主会话并不跟随——混用会让「全程单模型」不再成立），分析、规划、实现、验证全部在当前会话内联完成。

### Subagents

| Agent | 模型 | 权限 | 作用 |
| --- | --- | --- | --- |
| `planner` | flash | 读写 | 规划、架构、拆解任务 |
| `deep-worker` | v4-pro | 读写 | 重型实现、多文件改动、复杂调试 |
| `oracle` | v4-pro | **只读** | 根因分析、深度理解代码 |
| `reviewer` | v4-pro | **只读** | 代码审查升级层：Full 路径 / 高风险触发 / 确认 flash 初检线索 |
| `ui-builder` | flash | 读写 | 前端与 UI 相关任务 |
| `consultant` | flash | 读写 | 方案讨论、最佳实践建议 |
| `explore` | flash | **只读** | 代码库搜索、并行探索 |
| `librarian` | flash | **只读** | 文档检索、Web 搜索 |
| `light-orchestrator` | flash | 读写 | 轻量任务、单文件编辑 |
| `vision` | flash | 读写 | 多模态：图像/截图/图表/UI 稿理解 |

> `deep-worker` 和 `light-orchestrator` 遵循"禁止研究、禁止委托"原则——执行而非探索，上下文由 orchestrator 提供。`deep-worker` 另带 "What you DON'T handle" 拒绝契约：琐碎单文件编辑 → 拒接（路由 `light-orchestrator`）、纯研究/查询 → 拒接（路由 `oracle`/`explore`）、任何 flash 能完成的任务 → 拒接（pro 输入价 4.4× flash）。
>
> 只读 Agent（`oracle`/`reviewer`/`explore`）真只读化：`edit: deny` + bash 白名单（默认 deny 全部，仅放行 `git status/diff/log/show/blame/grep`、`rg` 等只读子命令；`oracle`/`reviewer` 另允许 `gh pr view/diff`、`gh issue view`、`gh api` 以支持 `/review` 回帖）。`librarian` 更严格：`bash: "*": deny`，无任何 bash 白名单。
>
> 各 agent 带 `skills` 白名单（默认 deny + 按职责放行，防误加载重型 skill）：`orchestrator` → `codemap`/`grilling`；`planner` → `spec-workflow`/`codebase-design`/`writing-plans`；`deep-worker` → `remove-deadcode`/`spec-workflow`/`git-release`/`to-tickets`/`triage`/`git-master`/`resolving-merge-conflicts`/`opencode-config`/`writing-for-agents`/`diagnosing-bugs`/`codebase-design`/`domain-modeling`/`test-driven-development`/`verification-before-completion`；`oracle` → `reflect`/`simplify`/`diagnosing-bugs`；`reviewer` → `code-review`/`security-review`/`gh-cli`；`explore` → `codemap`；`librarian` → `verify-with-docs`；`light-orchestrator` → `handoff`/`simplify`/`spec-workflow`/`code-review`/`gh-cli`/`verification-before-completion`；`consultant` → `domain-modeling`；`ui-builder` → `codebase-design`；`vision` → `vision-prep`；`solo` → 全部本地 skill + `brainstorming`/`systematic-debugging`/`test-driven-development`/`verification-before-completion`/`writing-plans`/`executing-plans`/`writing-skills`（内联执行器需要完整工具链，仍以 `"*": deny` 兜底）。白名单不只是权限——**被 deny 的 skill 不会出现在该 Agent 的 skill 名册里**，所以这份名单同时就是常驻上下文预算。superpowers 侧只有这 4 项接线（`writing-plans`→`planner`，`test-driven-development`/`verification-before-completion`→`deep-worker`，`verification-before-completion`→`light-orchestrator`），其余 skill 在本仓库都有等价物，一律不接线以免两套流程打架——映射表见 `AGENTS.md` Plugins 小节。
>
> **思考分档（thinking tiers）**：`reasoning_effort` 是按 Agent 经 frontmatter `options` 设置的请求级思考强度（`low`/`high`/`max`），不是模型 ID。`explore`/`librarian`/`consultant`/`ui-builder`/`orchestrator` = flash · thinking 关（最省）；`planner`/`light-orchestrator` = flash · thinking 开 + `reasoningEffort: low`；`deep-worker`/`oracle`/`reviewer` = pro · 默认 high；`solo` 无 `model` 字段，跟随会话所选模型（默认 pro），思考档随该模型而定。

## 快捷命令

### Agent 路由命令

| 命令 | Agent | 用途 |
| --- | --- | --- |
| `/deep` | `deep-worker` | 重型实现、多文件改动 |
| `/quick` | `light-orchestrator` | 轻量任务、单文件编辑 |
| `/ui` | `ui-builder` | 前端/UI 工作 |
| `/vision` | `vision` | 多模态：图像/截图/图表理解 |
| `/review` | `light-orchestrator`（code-review）→ 按需升级 `reviewer` | 代码审查：默认 flash 初检（Abbreviated 路径直接出报告）；命中升级触发条件时委派 `reviewer`（pro）并传递未验证线索；带 PR ref/URL 时回帖 GitHub（event=COMMENT） |
| `/deep-review` | `reviewer`（code-review + gh-cli） | 强制 pro 全量审查，跳过 flash 初检；带 PR ref/URL 时回帖 GitHub（event=COMMENT） |
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
| `code-review` | 单遍代码审查 + 证据门控；flash 初检 / pro 升级两级模型；大 diff（>~500 行）拆 Standards/Spec 两轴合并报告 |
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
| `grilling` | 需求对齐访谈：一次一问、多选优先，歧义收敛后再动手；并含「消息完全没听懂 → 一句话重述确认」与「领域术语模糊 → 协同 `domain-modeling` 锐化」两条分支（已合并原 `wait-what`/`grill-with-docs`） |
| `writing-for-agents` | 写给 agent 看的文档（skill/AGENTS.md/指针文档）的写作杠杆 |
| `to-tickets` | 把 spec/plan 拆解为可追踪的 GitHub issue（每单一个可独立完成+验收的单元，带验收标准） |
| `triage` | 基于 label 的 issue 分流：拉取 → 分类 → 打标签/派单（gh），只分流不改内容 |
| `diagnosing-bugs` | 系统化排障：先搭紧致的红态反馈回路再理论化 → 复现最小化 → 3-5 个可证伪假设 → 单变量插桩（`[DEBUG-<hex>]` 标记）→ 正确接缝处修复 + 回归测试 → 清理 |
| `codebase-design` | 架构词汇表：module/interface/depth/seam/adapter/leverage/locality，删除测试、深度测试，评估模块边界是否合理 |
| `domain-modeling` | 主动领域建模：维护 CONTEXT.md 术语表（仅词汇，不含实现细节），会话中挑战/锐化模糊术语，仅在必要时提议 ADR；含重复解释触发——同一概念被反复解释时落一条术语 |

> **名册即预算**：`skill` 工具的 `<available_skills>` 里每个 skill 的 name + description 都是**每轮常驻**上下文（本仓库 23 个本地 + 15 个 superpowers + 1 个内置，name+description 原始字节合计 ≈ 10.3KB ≈ 2.6K tokens，对未设白名单的内置 Agent 全量生效；superpowers v6.4.1 比 v6.3.0 多 1 个 skill、名册 +484 字节）。所以「不再高频使用」或「与现有 skill 重复」的 skill 应当合并删除，而不是留着备用；超长的 `description` 要按触发词必需性裁剪。

## 仓库结构

```text
├── opencode/          # OpenCode 配置目录（agents/、skills/、opencode.jsonc、AGENTS.md）
├── scripts/           # sync-config.ps1（同步到全局配置）
│                      # validate-jsonc.js（JSONC 校验）
│                      # estimate-cost.js（按 token 数估算费用）
├── README.md          # 简体中文（默认）
├── README.en-US.md    # English
└── LICENSE
```

## 配置变更点速查

改配置时按「想改什么 → 改哪个文件」定位，避免改错层：

| 想改的东西 | 文件 | 位置 |
| --- | --- | --- |
| 模型清单 / 价格 / thinking / temperature | `opencode.jsonc` | `providers.deepseek.models` |
| 画像：默认 Agent、小模型、嵌套深度、工具输出上限、压缩参数、附件缩放 | `opencode.jsonc` | 顶层同名键 |
| 权限（读 / bash / skill / 外部目录） | `opencode.jsonc` | `permissions` |
| 内置 agent（build/plan/title/summary/compaction/general）的模型 | `opencode.jsonc` | `agents` |
| 快捷命令的 Agent 与模板 | `opencode.jsonc` | `commands` |
| 单个 Agent 的模型、思考档、工具与 skill 白名单、拒绝契约 | `opencode/agents/<name>.md` | frontmatter + 正文 |
| 全局行为规则（原则、失败纪律、缓存纪律、反模式） | `opencode/AGENTS.md` | 对应小节 |
| 插件版本（pin） | `opencode/opencode.jsonc` | `plugins` |
| 压缩触发窗口（按模型）/ buffer / 保留尾部 | `opencode/opencode.jsonc` | `providers.deepseek.models.<id>.limit.input` + `compaction`（`buffer`/`keep`） |
| Skill 的行为与触发词 | `opencode/skills/<name>/SKILL.md` | frontmatter `description` + 正文 |

> 改完必须 `.\scripts\sync-config.ps1` 同步到 `~/.config/opencode`，然后重载（桌面 v2：`opencode-cli.exe reload`；或重启 opencode）。校验：`node scripts/validate-jsonc.js`；查已解析结果：`opencode-cli.exe debug config`。

## 使用指南

### 模式一：Orchestrator 自动路由（默认）

用自然语言描述需求，Orchestrator 自动分析意图、选择最合适的 Agent 和模型执行。

```text
「帮我排查这个登录接口的报错」     → oracle 分析根因 → 返回诊断报告        （pro high）
「优化这段循环，性能太差了」         → oracle 分析 → deep-worker 实施优化    （pro high）
「这个 PR 帮我审查一下」             → light-orchestrator flash 初检 → 返回分级报告（命中升级触发条件才走 reviewer，pro high）
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

**代码审查（flash 初检 → pro 升级）：**
```text
/review        ← 默认：flash 初检（Abbreviated 路径直接出报告，最省）
/review #123   ← PR 模式：审查 PR + 回帖 GitHub（gh-cli，event=COMMENT）
/deep-review   ← 强制 pro 全量审查（大 diff / 高风险 / 需要跨文件确认时）
```

**日常编码（flash 为主）：**
```text
「给 utils 加一个日期格式化函数」  → light-orchestrator 单文件实现   （flash low）
「这个函数为什么返回 undefined」   → explore 定位 → 直接回答        （flash off）
/quick 修一下这个拼写错误          → light-orchestrator 直接改       （flash low）
```

## 本次重构变更记录

### 2026-10-07（三）：价格核正 + 模型解析链实测修正

以「核价 + 修文档」为主的一轮：更新 flash 官方价，用 3 组真实调用钉死模型解析链，并把 README 中与实测不符的路由表述改对（不改模型矩阵与路由行为）。

| 变更 | 位置 | 说明 |
| --- | --- | --- |
| flash 价格核正 | `opencode.jsonc` `providers` | off-peak：in 0.22→**0.15**、out 0.66→**0.60**、cache_read 0.007→**0.003**，补 `cache.write: 0.15`（2026-10-07 官方页核验；peak 时段翻倍）；pro 不变。所有比率/示例按新价重算（pro/flash：输入 4.4×、输出 3.3×；cache_read 命中省 50×/30×；200K/30K 示例 flash ≈ $0.026 vs pro ≈ $0.096） |
| 模型解析链实测 | — | `opencode run --agent build`（不加 `-m`）连跑 2 次均落 **pro**（ses_eeaf576b、ses_eeaf6bb4）；加 `-m deepseek/deepseek-flash` 落 **flash**（ses_eeaec3aa，$0.00215 与 flash 新价一致）→ `agent.model` 不约束 run 主会话。源码同批核验（v2.0.24）：主会话 = `-m` > 会话模型 > 全局 `model`；`/` 命令绑定 subagent agent 时创建子会话并按该 agent 档运行；桌面 App 新会话按「草稿选择 > 会话已存模型 > 当前 agent 档 > 全局默认（pro）」解析 |
| 路由表述修正 | `README.md`/`README.en-US.md`/`opencode.jsonc` 注释 | 删除「build/plan 主会话跑 flash」的过强表述，改为逐面写明的「模型解析链」小节；`solo` 注记同步修正 |
| gh-cli 增例 | `skills/gh-cli/SKILL.md` | Quick reference 增加 `gh release view <tag> --json ...`（查 release 元数据） |
| 验证 | — | `validate-jsonc.js` 通过；`sync-config.ps1` 无陈旧文件、全局配置哈希与仓库一致；`reload` + `debug config`/`debug agents` 复核新价与 18 个 agent 档位；flash 覆盖运行计费 $0.00215 与离线估算一致 |

### 2026-10-07（二）：v2 专属升级 —— 全量切换 v2 原生键（移除 1.18.x 支持）

按「v2 专属」要求完成结构性升级：**删除全部 v1 触发键**，配置文件此后走 v2 的原生解码路径而非 V1→V2 迁移路径；1.18.x 不再受支持。

| 变更 | 位置 | 说明 |
| --- | --- | --- |
| 7 个 v1 键 → v2 原生键 | `opencode.jsonc` | `plugin`→`plugins`、`provider`→`providers`（`options`→`settings`、`modalities`→`capabilities`、平铺 `cost`→数组 + 嵌套 `cache`）、`agent`→`agents`、`command`→`commands`、`permission`→有序 `permissions` 列表（40 条，顺序与语义不变）、`attachment`→`media` |
| v1 死配置清理 | `opencode.jsonc` | 顶层 `subagent_depth`（v2 丢弃）、`compaction.prune`/`tail_turns`/`reserved`/`preserve_recent_tokens`（v2 不读）、`small_model`（由显式 `title` agent 模型取代）全部移除；`compaction` 写为 `{auto, keep:{tokens}, buffer}` |
| 技能重写 | `skills/opencode-config/SKILL.md` | 目标运行时改述为 desktop v2（2.0.24 实测）；新增 `isV1` 触发键清单（17 个）、原生键映射表与「未知键静默丢弃 → 必须实测验证」纪律 |
| 脚本适配 | `scripts/estimate-cost.js` | 读取 `providers.deepseek.models` 的原生 cost 数组 + 嵌套 `cache:{read,write}`；实跑输出与 README 示例一致 |
| 验证（A/B） | — | 迁移前/后 `debug config` + `debug agents` 逐字段等价（命令/引用/数值/权限顺序全等）；`plugin list` 仍解析 superpowers `8ca22db`；`validate-jsonc.js` 通过 |
| 有意不迁移 | `agents/*.md` | frontmatter 保持 v1 风格 `permission`/`options`（v2 接受且 12 个 agent 全部实测可用）；如未来采用原生 frontmatter，须一次性全量迁移并实测 |

### 2026-10-07：双运行时实测审计 + `general` 一行防漏

一次**实测审计 + 一行配置**（零付费调用）：用本机两代二进制端到端复核现配置，并补上唯一未定型的 subagent 的模型档。

| 变更 | 位置 | 说明 |
| --- | --- | --- |
| `general` 固定 flash | `opencode.jsonc` `agent` | 12 个自定义 agent 之外唯一未声明模型的 subagent（superpowers 泛化派发目标）；手动 @ 时会跟随会话模型（默认 pro），固定到 flash 兜底 |
| desktop v2（CLI 2.0.24）实测（只读） | — | `debug config` 确认 classic → 原生键上转换（完整映射见「v2 原生键名」）；`debug agents` 确认 18 个 agent 全部就位（12 自定义 + 6 内置）；`planner`/`light-orchestrator` 思考档落在 `request.body`（`thinking.enabled` + `reasoningEffort: low`）；`plugin list` 确认 superpowers `#v6.4.2`（version `8ca22db`） |
| 1.18.4 实测（只读） | — | `opencode debug config` 进程内解析通过；`OPENCODE_CONFIG_DIR` 指向仓库 `opencode/` 时按预期加载（marker 文件命中验证）；classic 键原样读取 |
| 常驻成本影响 | — | +1 行配置**不进入** prompt 前缀，本轮常驻上下文增量 **0**；未重跑付费基线（成本纪律：前缀零变更，重测无信息增量；上次基线 15,177 prompt tokens / $0.00269 不受影响） |

### 2026-09-29：审计轮 + 插件 pin 升至 v6.4.2（更精简的 writing-plans）

一次**只读审计 + 一处最小升级**：全仓结构化审查零违规；唯一实质变更是把 superpowers pin 从 `#v6.4.1` 升到 `#v6.4.2`——它把 `writing-plans` 重写为「签名 + 测试锚定」的精简计划格式，直接服务 token 预算。

| 变更 | 位置 | 说明 |
| --- | --- | --- |
| 插件 pin `#v6.4.1` → `#v6.4.2` | `opencode.jsonc` | v6.4.2（2026-09-25）只含 1 个提交（`8ca22db`，17 文件 +61/−85）：**运行时零改动**——`.opencode/plugins/superpowers.js` 字节不变，bootstrap 注入与常驻前缀不受影响（skill 名册不变，无新增/删除）；实质是 `writing-plans` 改为「只写执行者自己无法决定的内容」：步骤以精确签名 + 测试断言锚定，不再整段转抄实现代码（「比它描述的代码还长的计划 = 转录，不是计划」）。planner 的输出与执行者的阅读同时变省 |
| 全量结构审计（只读，零改动） | `skills/`、`agents/`、`opencode.jsonc`、`scripts/` | 23 个 `SKILL.md` 全部合规：`name` = 目录名、`description` ≤ 450 字符、无多余文件；12 个 agent 仅引用两个允许模型；`opencode.jsonc` 通过 `scripts/validate-jsonc.js`；仓库 ↔ 全局配置无语义漂移（仅 CRLF/LF 差异）。gh-cli 已含「创建 PR / 列 Issue」示例（371 行）、code-review 已实现 flash 初检 → pro 升级（`Model tier — flash first, pro on escalation`） |
| v6.4.2 首跑复测 | — | 2026-09-29 复跑基线命令（v1.18.4，全局配置）：prompt tokens **15,177**（未命中 12,105 + 缓存读 3,072）、output 1、单次 $0.00269（flash off-peak）——对比 v6.4.1 的 15,153，Δ+24 ≈ 噪声，确认升级不增加常驻成本；插件包 `superpowers.git#v6.4.2` 已安装并随首跑加载 |

### 2026-09-20：插件升级 + superpowers 接线 + 路由矛盾修正

同样是**最小 diff**：不加 skill、不加依赖、不动模型矩阵；只修插件 pin、把过程技能接到真正需要它的 Agent、修正路由表里两处与「flash 初检」策略矛盾的直连 pro 行。

| 变更 | 位置 | 说明 |
| --- | --- | --- |
| 插件 pin `#v6.3.0` → `#v6.4.1` | `opencode.jsonc` | v6.4.1（2026-09-19 稳定版）：① **子会话（task subagent）不再注入 bootstrap**（3,110 字节 ≈ 780 tokens/子会话）——opencode 1.18.x 的 V1 路径已在插件源码中显式实现（`firstUser.info.sessionID` + `parentID` 判定）；② 技能内容修复（TDD 改为跑项目测试命令、`executing-plans` 重写为 Native 内联执行、code-review 的 `BASE_SHA` 修正为 `git merge-base`）。已用临时配置目录做受控 A/B：prompt tokens 15,159 → 15,183（Δ+24 ≈ 噪声），确认升级本身不增加常驻成本 |
| superpowers 最小接线 | `agents/{planner,deep-worker,light-orchestrator}.md` | 只接**没有本地等价物**的 4 项：`writing-plans` → `planner`；`test-driven-development`、`verification-before-completion` → `deep-worker`；`verification-before-completion` → `light-orchestrator`。`systematic-debugging` ↔ `diagnosing-bugs`、`brainstorming` ↔ `spec-workflow`/`grilling`、`requesting-`/`receiving-code-review` ↔ `code-review` 等**不接线**，避免两套流程并存 |
| 插件 → 模型映射表 | `AGENTS.md` Plugins 小节 | 插件无模型配置项（源码复核），故在 Agent 层写明映射，并列出「本地已有等价物、不接线」清单，顺带标出每个 skill 的档位 |
| 路由表矛盾修正 | `agents/orchestrator.md` | `"review X"` 原先直连 `reviewer`（pro），与既有「flash 初检、命中触发条件才升级」策略矛盾 → 改为 `light-orchestrator` 初检 → 按需升级；`"add tests for X"` 原先直连 `deep-worker`（pro）→ 单文件走 flash、测试套件跨文件才走 pro |

### 上一轮：压缩层收敛（移除 DCP、skill 合并）

面向「省 token + 少 API 调用」的一次收敛：**只做减法与显式化，不引入任何新模型、新依赖、新工具**。

**删除**

| 删除项 | 理由 |
| --- | --- |
| `skills/wait-what/` | 与 `grilling` 的「先确认再动手」规则重复；一个行为不该占两个名册位 |
| `skills/grill-with-docs/` | 只是 `grilling` + `domain-modeling` 的组合说明，组合关系已在 `grilling` 正文写明 |
| `orchestrator.md` 中重复 `AGENTS.md` 的 4 条规则 | thinking tier / retry cap / reference-paths 等已由 `AGENTS.md` 单点定义，重复表述只增加常驻 token |
| `orchestrator.md` 路由表中 10 处 `· ~½ cost` 标注 | 同一信息在表头已声明一次，逐行重复属于噪音 |
| `subagent_depth: 3` 的第 3 层 | 实际最深链路只有 2 层，第 3 层无人使用，只提供意外嵌套放大的可能 |
| `@tarquinen/opencode-dcp` + `opencode/dcp.jsonc` + 插件缓存 | 其价值（绝对值阈值提前压缩、工具调用去重）已由内置 compaction 的显式模型窗口（`limit.input` + `buffer`）覆盖；少一个插件 = 少一份常驻注入、少一条前缀漂移路径 |

**新增 / 强化**

| 新增项 | 预期收益 |
| --- | --- |
| `AGENTS.md` 硬约束「Vision input is opt-in」 | 从规则层保证非视觉任务不传图/不生成图，只有用户提供图像时才走 `vision`（flash 多模态） |
| 压缩触发点从隐式默认改为显式声明 | 不写 `limit.input` 时触发点 = `context − maxOutputTokens` = **968K**，且 `compaction.reserved` 是**死配置**（只有 `input` 路径才读它）。现在 flash 115K / pro 148K，读配置即可确认 |
| `vision-prep` 修正附件上限陈述 | 原文档写「2000×2000 / 5MiB（opencode 默认）」，与本仓 `media.image`（1600px / 2MiB）矛盾，会误导预处理 |
| README「配置变更点速查」表 | 维护时一次定位到文件与小节，减少试错（试错本身就是 token 消耗） |
| README「快速开始」四步 TL;DR | 新机器上手从「读完全文」变成「照抄四行」 |

**两个模型的最终路由规则**（全仓仅此两个模型，无任何其他模型引用）

| 模型 | 触发条件 | 承担角色 | 计费（USD/1M，off-peak） |
| --- | --- | --- | --- |
| `deepseek/deepseek-flash` | **默认**；绝大部分高频任务 | 编排/路由、规划、常规实现、单文件编辑、咨询、UI、探索、文档检索、标题/摘要/压缩、批量生成；**唯一**的视觉理解入口（仅在用户提供图像时启用） | in 0.15 / out 0.60 / cache 0.003 |
| `deepseek/deepseek-v4-pro` | 任务复杂度高、需深层逻辑分析时（自动或手动 `/deep`、`/oracle`、`/deep-review`） | 复杂推理、根因分析、代码审查升级层、架构设计、疑难调试、重型多文件实现 | in 0.66 / out 1.98 / cache 0.022 |

切换方式：自动——`orchestrator` 按意图分类路由，flash agent 无法胜任时自升级；手动——`/deep`、`/oracle`、`/deep-review` 直达 pro，其余命令/subagent 按其 agent 档位走 flash（命令创建子会话，不改当前会话模型）；headless `opencode run` 默认按全局 `model`（pro），需 `-m deepseek/deepseek-flash` 显式切 flash。

## 借鉴来源

核心思路借鉴 [oh-my-openagent](https://github.com/code-yeongyu/oh-my-openagent)（意图门控、只读隔离、反模式）、[oh-my-opencode-slim](https://github.com/alvinunreal/oh-my-opencode-slim)（调度器优先、后备链、拒绝契约、提示词缓存安全）、[anomalyco/opencode](https://github.com/anomalyco/opencode)（配置 Schema、技能体系）、[cli/cli](https://github.com/cli/cli)（gh v2.100 命令集）、[OpenSpec](https://github.com/Fission-AI/OpenSpec)（delta specs）、[mattpocock/skills](https://github.com/mattpocock/skills)（冲突解析、交接文档、排障/架构/领域建模技能）、[pi](https://github.com/earendil-works/pi)（先答后改、精简响应）、[deepreview](https://github.com/mechanai/deepreview)（有效大小路由）。纯配置实现，零额外依赖。**借鉴而非照搬**：只汲取轻量化设计理念，精简优先于新增。

## 设计哲学

- **纯配置驱动，零额外依赖** —— 所有能力由 `opencode.jsonc` + `agents/*.md` + `skills/*/SKILL.md` + `AGENTS.md` 实现
- **DeepSeek V4 模型族极致利用** —— Pro 做深度推理与重型实现，Flash 做路由、规划、常规执行与原生多模态
- **Token 效率优先** —— 路径引用替代粘贴文件、技能按需加载、压缩分级管理
- **插件增效但不喧宾夺主** —— 唯一插件 superpowers 只提供过程纪律（固定 pin 以保字节稳定前缀）；上下文压缩 100% 交给内置 compaction，不引入第三方压缩层
- **执行与探索分离** —— deep-worker/light-orchestrator 禁止研究/委托，explore/librarian 禁止修改
- **缓存与 thinking 纪律** —— 静态前缀稳定以命中 DeepSeek 提示词缓存；flash 关 thinking + temperature 0（providers 模型层），pro 默认 thinking 开
- **Scope First + Delegate Always** —— 先定范围（2+ 步/多文件/架构变更先走 planner），再委派执行，顶层 token 只留给路由与难题
- **原子 TODO** —— 多步任务先写有序 TODO，逐条 in_progress→completed；格式 `path: action for scenario — verify by check`
- **进度可控 + 失败隔离** —— 每个 TODO 带可验证完成判据，阶段边界汇报 `[done/total]`；错误分 transient/recoverable/fatal 三类，同一操作最多重试 3 次且每次必须换策略；大任务拆成独立单元，单元失败不阻塞其余，最终汇总 `succeeded / failed / skipped`
- **按模型成本分级压缩** —— 用 `limit.input` 给两个模型分别声明工作窗口：flash `131072−16000=115,072`（高频路径，窗口更紧）、pro `163840−16000=147,840`（深度任务，留更多余量以减少有损压缩次数）；触发点是算出来的，不是继承来的
- **视觉输入成本封顶** —— `media.image` 自动缩放超大图（>1600px / >2MB 先缩放再上传），配合 flash 内部 ~800x800 降采样，避免 base64 字节浪费
- **验证预算 + 证据强度** —— 动手前设定最小非重复证据路径；"能 typecheck" 不等于行为变更的 QA
- **易变区纪律** —— 时间戳/随机 ID/动态文件列表等易变内容置于 payload 尾部，保护 DeepSeek 提示词缓存前缀
- **名册即预算** —— 每个 Agent 的 `permission.skill` 白名单同时决定 skill 名册大小；被 deny 的 skill 不进名册，也就不进每轮的常驻上下文
- **视觉 opt-in** —— 图像只在用户提供时进入 payload；非视觉任务不传图、不生成图
- **持续改进** —— reflect 机制化发现摩擦、code-review 证据门控保证质量
- **审查成本分级** —— 代码审查默认 flash 初检（省约 73%），仅在升级触发条件命中时付 pro 全价；升级时传递未验证线索，避免重复推导
