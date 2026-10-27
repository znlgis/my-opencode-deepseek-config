# My OpenCode × DeepSeek Config

**简体中文** | [English](README.en-US.md)

**OpenCode v2 × DeepSeek 最优配置** —— 在 OpenCode v2 多 Agent 框架下，只保留 `deepseek/deepseek-flash` 一个模型、用 thinking 档位区分 Agent 分工的配置方案。核心理念：**Token 效率优先，用最小的上下文成本达到最好的开发效果**。

## 当前配置概览

- 默认主 Agent：`orchestrator`
- 唯一模型：`deepseek/deepseek-flash`（V4.1 Flash，原生多模态）；Agent 只按 thinking 档位区分：默认（thinking 关）/ `#low` / `#high`
- 代理层级：`experimental.subagent_depth: 2`（v2 原生键；v1 的顶层 `subagent_depth` 已被 v2 丢弃，本仓库不再写入。恰好覆盖实际最深链路 `orchestrator → light-orchestrator → oracle`；其余 subagent 一律禁止委派，更深层级只是白送 token 放大面）
- 会话分享：关闭（`share: "disabled"`）
- 权限基线：`read` 默认放行、`shell` 默认 `ask`（未匹配的命令一律交人工确认，不枚举危险模式）；`.env` 类敏感文件 `deny`；外部目录 `ask`；只读 Agent 的 shell 规则（默认 deny 全部 + 仅放行只读子命令）
- 上下文压缩：**仅用内置 compaction**（opencode.jsonc）——`limit.input` 显式声明工作窗口，触发点 = `limit.input − compaction.buffer`（`131072−16000=115,072` tokens），保留尾部 `keep.tokens: 12000`；v1 拼写（`prune`/`tail_turns`/`reserved`/`preserve_recent_tokens`）在 v2 下不生效，已全部移除（v1 的 `reserved` 对应 v2 的 `buffer`）；无第三方压缩插件
- 全局规则：`AGENTS.md`（核心原则、任务拒绝契约、自我验证、反模式等；上下文/Token 纪律在 `AGENTS.md`）
- 技能：`skills/` 目录下 **23 个** `SKILL.md` 技能，通过原生 `skill` 工具按需加载；各 Agent 再用 `skill` 权限白名单裁剪名册（名册的 name+description 是**每轮常驻**成本，故按职责最小化）
- 命令：**18 个**快捷命令（Agent 路由 / 操作 / 内联 / 规约四类），见下文
- 插件：仅 `superpowers`（git URL 固定 tag `#v7.0.1`，经 v2 原生 `plugins` 数组加载）——纯 skill 注入，**不提供模型映射**，skill → Agent → 档位映射只存在于 `AGENTS.md` 的 Plugins 小节；固定 pin 以保字节稳定前缀，避免自动更新造成前缀漂移。子会话（task subagent）不注入 bootstrap，委派链路不付这份常驻成本。

## DeepSeek 模型配置

### 前置条件

- OpenCode v2（桌面版内置 CLI **2.2.0**，2026-10-27 核验通过；1.18.x 及更早版本**不再支持**）
- **运行时提醒**：PATH 上的 `opencode`（WinGet 安装）是 **1.18.4**，不是本配置的运行时；用它会走 V1→V2 迁移路径。headless 运行请用桌面版自带 CLI（路径见「安装与启动」⚠️）
- DeepSeek API Key：[platform.deepseek.com/api_keys](https://platform.deepseek.com/api_keys) 申请
- 后台子智能体（`background: true` 的委派）需要环境变量 `OPENCODE_EXPERIMENTAL_BACKGROUND_SUBAGENTS=true`（PowerShell：`$env:OPENCODE_EXPERIMENTAL_BACKGROUND_SUBAGENTS="true"`）；未设置时后台委派会直接报错，应退回前台串行委派

### 方式一：TUI 交互式配置（推荐）

```bash
opencode
# 在 TUI 中输入: /connect → 选择 DeepSeek → 粘贴 API Key
# 然后: /models → 选择 deepseek-flash
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
  "model": "deepseek/deepseek-flash"
}
```

本配置只保留 `deepseek-flash` 一个模型，在 `providers` 层用 **variant** 拆分 thinking：默认档关闭 thinking 并固定 `temperature: 0`（最快最省），`#low` / `#high` 两个 variant 打开 thinking，并分别把 `settings.reasoningEffort` 设为 `low` / `high`。`deepseek-flash` 是合并后的 V4.1 Flash 模型——原文本版与视觉版已合并为一个原生多模态模型，因此由它声明 `capabilities`（图像输入）。示例（v2 原生键）：

```jsonc
"providers": {
  "deepseek": {
    "models": {
      "deepseek-flash": {
        "body": {
          "thinking": { "type": "disabled" },
          "temperature": 0
        },
        "capabilities": {
          "tools": true,
          "input": ["text", "image"],
          "output": ["text"]
        },
        "variants": [
          { "id": "low",  "settings": { "reasoningEffort": "low" },  "body": { "thinking": { "type": "enabled" } } },
          { "id": "high", "settings": { "reasoningEffort": "high" }, "body": { "thinking": { "type": "enabled" } } }
        ]
      }
    }
  }
}
```

> `thinking`/`temperature` 必须写在 `body`：`settings` 只承载 provider 选项（`timeout`/`chunkTimeout`/`reasoningEffort` 等），写进去会被静默丢弃。

> **模型 ID 命名规则**：`provider_id/model_id`，variant 用 `#` 后缀。本仓库只有 `deepseek/deepseek-flash`、`deepseek/deepseek-flash#low`、`deepseek/deepseek-flash#high`。

### v2 原生键名（本仓库唯一写法）

本仓库是 **OpenCode v2 专属配置**，只写 v2 原生键：`plugins`（非 `plugin`）、`providers`/`models`（非 `provider`）、`agents`/`commands`（非 `agent`/`command`）、`permissions`（有序 `{action, resource, effect}`，非 `permission`；action 名是 `shell`/`subagent`，非 `bash`/`task`）、`media.image`（非 `attachment`），嵌套深度为 `experimental.subagent_depth`，压缩为 `{auto, keep, buffer}`（`prune`/`tail_turns`/`reserved` 是 v1 拼写，v2 不读）。只要文件里出现任一 v1 触发键（`provider`/`agent`/`command`/`plugin`/`permission`/`tools`/`attachment`/`small_model` 等），v2 就改走 V1→V2 迁移路径，本文描述的行为不再成立。

> `$schema` 保持 `https://opencode.ai/config.json`（v2 应用自身写入的就是该 URL）。

## 安装与启动

```powershell
# 1. 克隆
git clone https://github.com/znlgis/my-opencode-deepseek-config.git

# 2. 指向仓库内的 opencode/ 配置目录（当前会话生效）
$env:OPENCODE_CONFIG_DIR = "D:\path\to\my-opencode-deepseek-config\opencode"

# 3. API Key（也可启动后在 TUI 里 /connect）
$env:DEEPSEEK_API_KEY = "sk-your-key-here"

# 4. 启动
opencode
```

永久生效（Windows）：

```powershell
[Environment]::SetEnvironmentVariable("OPENCODE_CONFIG_DIR", "D:\path\to\my-opencode-deepseek-config\opencode", "User")
```

Linux / macOS：把 `export OPENCODE_CONFIG_DIR="$HOME/path/to/my-opencode-deepseek-config/opencode"` 追加到 `~/.bashrc` 或 `~/.zshrc`。

**替代方案**：把 `opencode/` 软链到全局配置路径——Windows 需管理员（`New-Item -ItemType SymbolicLink -Path "$env:USERPROFILE\.config\opencode" -Target "<repo>\opencode"`），Linux/macOS 用 `ln -s <repo>/opencode ~/.config/opencode`。

启动后自检三条：`/models` 显示 `deepseek/deepseek-flash`；Agent 列表有 12 个自定义 Agent；输入任意需求，`orchestrator` 会自动分析并路由（想跳过路由就直接用 `/quick`、`/deep`、`/review`）。

> **⚠️ 必须由 OpenCode V2.x 运行**：PATH 上的 `opencode.exe` 是 WinGet 安装的 **v1.18.4**，真正在跑的是桌面版 **v2.2.0** 及其自带 CLI `"$env:LOCALAPPDATA\Programs\@opencode-aidesktop\resources\opencode-cli.exe"`。用 v1 启动会走 V1→V2 迁移路径，本文档描述的路由、权限、压缩、名册行为都不成立；headless 运行请用该 CLI。

改完仓库文件后必须同步（`~/.config/opencode` 是独立副本，不是符号链接）：

```powershell
.\scripts\sync-config.ps1              # 可用 -Src 指定源目录
```

同步排除 `node_modules`/`.git`/包清单与锁文件；`skills/`/`agents/` 的删除清理走 git 索引 + 全历史，`git rm` 后即使未提交也会在同步时删掉目标目录里的同名副本。

## 模型分工

**为什么只保留 `deepseek-flash`**（opencode v2 正式版发布后顺势收敛配置）：

- **日常开发的瓶颈不在模型上限。** 读代码、改文件、跑测试、写文档、按 diff 审查——这些任务要的是快、稳、便宜，不是极限推理；用 pro 级模型做这些，多付的钱换不来对应的产出。
- **flash 是当前各家投入最密集的档位。** 竞争重心集中于此，意味着同价位的能力仍在快速上涨；配置压在 flash 上，等于跟着迭代最快的那条曲线走。
- **pro 的能力和价格对日常开发双重溢出。** 它服务的是少数难题，而配置要为每一次普通调用付费——为偶尔的硬任务常驻一个更贵的模型，账单结构本身就是错的。
- **需要更强推理时，用同一模型的 thinking 档位补。** `#high` 靠 reasoning token 加深推理，成本随任务发生、按输出价线性增长，不引入第二套费率和第二条缓存前缀。
- **代价是显式承认的：用上限换成本。** 极端难题上 flash 可能不如 pro 一次到位，会多一轮返工或升级到 `#high`——这部分代价只落在少数任务上，而不是让每次普通调用都为它买单。

本仓库只使用 **一个模型** `deepseek/deepseek-flash`，不引入任何其他模型。Agent 之间的差异只有一个维度——**思考档位（thinking tier）**，用模型 variant 表达：

| 档位 | 模型引用 | 机制 | 典型 Agent |
| --- | --- | --- | --- |
| 省（trivial） | `deepseek/deepseek-flash` | body `{"type":"disabled"}` + `temperature: 0` | `orchestrator`/`explore`/`librarian`/`consultant`/`ui-builder`/`vision` |
| 中（routine） | `deepseek/deepseek-flash#low` | thinking 开 + `settings.reasoningEffort: low` | `planner`/`light-orchestrator` |
| 高（deep） | `deepseek/deepseek-flash#high` | thinking 开 + `settings.reasoningEffort: high` | `deep-worker`/`oracle`/`reviewer`/`solo` |

思考强度由两个**请求参数**控制，二者都声明在 `opencode.jsonc` 的**模型层**；Agent 只选模型引用，不写任何思考参数：

- **thinking 开关** —— `{"thinking":{"type":"enabled"|"disabled"}}`，是**请求体（body）字段**。默认档关闭（最省），`#low`/`#high` 打开。
- **thinking 强度** —— `{"reasoning_effort":"low"|"high"|"max"}`，由 **provider setting** `settings.reasoningEffort` 驱动（配置里写 camelCase，线上是 snake_case）——这是 opencode aisdk 层唯一会转成 `reasoning_effort` 的路径。

> **官方文档核对（2026-10-27，`guides/thinking_mode`）**：① 思考模式**默认打开，effort 默认 `high`**——所以只有默认档（显式 `disabled`）真能省下 reasoning token，`#high` 只是把默认值写明，`#low` 是唯一存在的中间档；effort 没有中间值，`minimal`→low、`medium`/`xhigh`→high、`max`/`ultra`→max。② 思考模式**不支持** `temperature`/`presence_penalty`/`frequency_penalty`——传入不报错，但也不生效；上表的 `temperature: 0` 只在默认档真正起作用，`#low`/`#high` 继承它但无效。③ `top_p` 行为相反：`deepseek-flash` 仅在思考模式下生效（有效范围 0.95–1.0，低于 0.95 按 0.95 处理），非思考模式下恒为 1.0、传入值被忽略——所以本配置不设 `top_p`。④ 请求携带 `tools` 时，历史轮的 `reasoning_content` 必须完整回传，否则 API 返回 400；opencode 对所有 deepseek 模型强制回传（`ai/protocols/openai-chat.ts` 的 `requireReasoning`），无需额外配置。

> **⚠️ 写法陷阱（2026-10-16 实测修正）**：`thinking`/`temperature` 放在 `settings` 下不生效（openai-chat 协议不实现该 option），`reasoningEffort` 放在 Agent 的 `options` 下也不生效（`options` 只落到 `request.body`，且 camelCase 不是 API 认的 `reasoning_effort`）。两者都必须写在**模型层**：toggle 进 `body`、effort 进 `settings`。修正前 flash 一直开着 thinking 跑，每一步都在付 reasoning token。

**成本结构**：三个档位共用 flash 的同一份费率（in 0.15 / out 0.60 / cache_read 0.003 USD per 1M，off-peak），没有模型溢价；档位差异只体现在 **reasoning token 数量**，而 reasoning token 按输出价计费。所以「升档」是多花输出 token，不是换更贵的单价——深度任务不再付 4.4× 输入价。

> **未验证项**：`reasoningEffort: "high"` 在 flash 上的实际推理深度与 token 增量未实测（本机 live service 只有 `opencode-go` provider，`-m deepseek/*` 返回 `provider.no-route`）；请在具备 DeepSeek 凭据的机器上跑一次 `/deep-review` 复核。

### 路由策略

- **Trivial → thinking off**：搜索、查询、咨询、UI、探索、文档检索等明确定义的轻任务走默认档（最省）
- **Routine-nontrivial → `#low`**：规划、常规多文件实现等稍有难度的任务走 `deepseek/deepseek-flash#low`（thinking 开 + effort `low`）
- **Deep/uncertain → `#high`**：深度推理、根因分析、重型多文件实现走 `deepseek/deepseek-flash#high`（thinking 开 + effort `high`）
- **代码审查 → thinking off 初检，`#high` 升级**：`/review` 默认走默认档初检（Abbreviated 路径），仅在升级触发条件命中时委派 `reviewer`（`#high`）；`/deep-review` 强制 `#high` 全量审查
- **Vision 专责多模态**：仅在用户明确提供图像/截图或明确要求时，路由到 `vision` agent（`deepseek-flash`，原生多模态）。**视觉输入是 opt-in**：非视觉任务不主动传图、不生成图、不调用视觉能力（`AGENTS.md`「Constraints」已把这条写成硬约束）；附件统一先经 `media.image` 缩到 1600px / 2MiB，避免 base64 字节浪费
- **自动升级**：thinking off / `#low` agent 无法胜任时自动升级到 `#high`（带完整上下文）

**模型解析链（v2.0.24 源码核验 + 本机实测；2026-10-12 于 v2.0.27 复核 + 环境告警；2026-10-27 于 v2.2.0 源码复核）**：`agent.model` 只在三处决定实际模型——① subagent/子会话（含 `/` 命令：命令绑定 subagent agent 时会**创建子会话**并按该 agent 档位运行，如 `/deep`→`#high`、`/quick`→`#low`，当前会话模型不受影响）；② title/summary/compaction 一次性调用；③ 桌面 App 新会话的随 agent 联动默认值（build/plan/orchestrator → flash）。主会话自身按 `-m` > 会话已存模型 > 全局 `model`（flash 默认档）解析：桌面 App 用模型选择器切换（选择被记住）；headless `opencode run` **不读** `agent.model`——主会话按全局 `model` 解析；要指定思考档位就显式加 `-m deepseek/deepseek-flash#high`。**⚠️ 2026-10-12 复测（v2.0.27）**：在本机（live service 只配置了 `opencode-go` provider）`-m deepseek/*` 返回 `provider.no-route`，只有省略 `-m` 的运行成功——该结论依赖「目标机已 `/connect deepseek` 并具备 provider 路由」，请在具备 DeepSeek 凭据的机器上复核后再依赖 `-m` 语义。

用法示例：`「这个库怎么用」` → thinking off（librarian）；`「给用户模块加导出功能」` → `#low`（planner）；`「排查登录接口报错的根因」` → `#high`（oracle）；`/review` → thinking off 初检（小 diff 直接出报告）；`/review #123`（大 diff / 触及信任边界）→ 初检后升级 `#high`。

#### 代码审查的两级档位（轻量化 Review）

| 层级 | 模型 | 执行者 | 覆盖范围 |
| --- | --- | --- | --- |
| Tier 1（默认） | `deepseek/deepseek-flash`（thinking off） | `light-orchestrator`（`/review`） | Abbreviated 路径：≤8 个逻辑文件且 ≤300 有效行，且无高风险触发 |
| Tier 2（升级） | `deepseek/deepseek-flash#high` | `reviewer`（`/deep-review` 或自动升级） | Full 路径、高风险触发、或 Tier 1 发现需跨文件确认的 critical/major |

升级触发条件（命中任一即升级）：路径为 Full（大 diff 或高风险正则命中）；Tier 1 发现 critical/major 但无法仅凭 diff 确认影响；diff 触及信任边界（同时加载 `security-review`）；用户明确要求深度审查。

Tier 1 报告是**完整审查**而非预览——干净结果不因"再确认一下"而升级。升级时把 Tier 1 发现作为**未验证线索**传给 Tier 2，让 `#high` 确认而非重新推导，避免重复 token 消耗。

### 成本对比

价格取自 `opencode.jsonc` 的 `providers.deepseek.models`（USD / 1M tokens，**2026-10-27 官方定价页重新核验，与本表逐项一致**，off-peak 价；peak 时段 = off-peak ×2，北京时间周一至周五 09:00–12:00 与 14:00–18:00，其余时段含周末与中国法定节假日均为 off-peak）。`cache_write` 无独立官方价，按 cache-miss 输入价映射：

| 模型 | 输入 | 输出 | 缓存命中（cache_read） | 缓存写入（cache_write） |
| --- | --- | --- | --- | --- |
| `deepseek-flash` | 0.15 | 0.60 | 0.003 | 0.15 |

两个成本杠杆：

- **思考档位**：三档同一单价，差别只在 reasoning token 数量（按输出价计费）——升档是多花输出 token，不是换更贵的单价。
- **提示词缓存**：`cache_read` 比输入价便宜 **50×（0.003 vs 0.15）**。本配置的字节稳定前缀 + 易变区纪律（见 `AGENTS.md`）正是为了最大化缓存命中率。

**典型会话成本估算**（假设 200K 输入 tokens，其中 150K 命中缓存，30K 输出）：

| 档位 | 缓存命中 | 输入未命中 | 输出 | 合计 |
| --- | --- | --- | --- | --- |
| thinking off | 150K × 0.003 = $0.0005 | 50K × 0.15 = $0.0075 | 30K × 0.60 = $0.018 | **≈ $0.026** |

`#low` / `#high` 在同一可见 token 量上只需多出 reasoning token，就按输出价线性叠加。可用 `scripts/estimate-cost.js` 按实际 token 数估算。

#### 思考档位的成本对比

以"审查一个 300 有效行的本地 diff"为例（假设 60K 输入 tokens，其中 45K 命中缓存，8K 可见输出；`#high` 额外产生 4K reasoning tokens，按输出价计费）：

| 档位 | 缓存命中 | 输入未命中 | 输出（含 reasoning） | 合计 |
| --- | --- | --- | --- | --- |
| thinking off（`/review` 初检） | 45K × 0.003 = $0.0001 | 15K × 0.15 = $0.0023 | 8K × 0.60 = $0.0048 | **≈ $0.007** |
| `#high`（`/deep-review`） | 45K × 0.003 = $0.0001 | 15K × 0.15 = $0.0023 | 12K × 0.60 = $0.0072 | **≈ $0.010** |

**结论**：单模型下两级审查的价差不是「模型单价」，而是「多出的 reasoning token」——同一输入基数下 `#high` 约贵 40%（$0.007 → $0.010），远小于双模型时 pro 的 3.7×。reasoning token 数是假设值，实际值需在具备 DeepSeek 凭据的机器上用 `scripts/estimate-cost.js` 复测。

## Agent 结构

### Primary Agent

| Agent | 模型 | 作用 |
| --- | --- | --- |
| `orchestrator` | flash | 默认入口：意图门控（Intent Gate）+ 模型感知路由 + 后备链 |
| `solo` | flash#high | 单模型内联执行器：零委派、不用后台助手，全程在 flash 的 `#high` 档完成 |

> `solo` 是第二个 primary agent：`{action: subagent, resource: "*", effect: deny}`（零委派，不调用任何子智能体）、显式 `model: deepseek/deepseek-flash#high`（全程固定最强思考档）、不用 build/plan 等内置后台助手（其档位只在子会话/一次性调用生效，主会话并不跟随——混用会让「全程单模型」不再成立），分析、规划、实现、验证全部在当前会话内联完成。

### Subagents

| Agent | 模型 | 权限 | 作用 |
| --- | --- | --- | --- |
| `planner` | flash#low | 读写 | 规划、架构、拆解任务 |
| `deep-worker` | flash#high | 读写 | 重型实现、多文件改动、复杂调试 |
| `oracle` | flash#high | **只读** | 根因分析、深度理解代码 |
| `reviewer` | flash#high | **只读** | 代码审查升级层：Full 路径 / 高风险触发 / 确认便宜档初检线索 |
| `ui-builder` | flash | 读写 | 前端与 UI 相关任务 |
| `consultant` | flash | 读写 | 方案讨论、最佳实践建议 |
| `explore` | flash | **只读**（仅 `.opencode/codemap.md` 可写） | 代码库搜索、并行探索 |
| `librarian` | flash | **只读** | 文档检索、Web 搜索 |
| `light-orchestrator` | flash#low | 读写 | 轻量任务、单文件编辑 |
| `vision` | flash | 读写（shell 仅限 `ask` 门控的 vision-prep 脚本） | 多模态：图像/截图/图表/UI 稿理解 |

> `deep-worker` 和 `light-orchestrator` 遵循"禁止研究、禁止委托"原则——执行而非探索，上下文由 orchestrator 提供；唯一例外是 `light-orchestrator` 的两种定向升级——`oracle`（只读分析）与 `reviewer`（`#high` 审查），服务 `/simplify` 与 `/review`。`deep-worker` 另带 "What you DON'T handle" 拒绝契约：琐碎单文件编辑 → 拒接（路由 `light-orchestrator`）、纯研究/查询 → 拒接（路由 `oracle`/`explore`）、任何便宜档能完成的任务 → 拒接（本档每步都付 reasoning token）。
>
> 只读 Agent（`oracle`/`reviewer`/`explore`）真只读化：`edit: deny` + shell 白名单（默认 deny 全部，仅放行 `git status/diff/log/show/blame/grep`、`rg` 等只读子命令；`oracle`/`reviewer` 另允许 `gh pr view/diff`、`gh issue view`、`gh api` 以支持 `/review` 回帖）。`librarian` 更严格：`shell` 规则集默认全部 deny，且没有任何 shell 白名单。
>
> 各 agent 带 `skills` 白名单（默认 deny + 按职责放行，防误加载重型 skill）：`orchestrator` → `codemap`/`grilling`；`planner` → `spec-workflow`/`codebase-design`/`writing-plans`；`deep-worker` → `remove-deadcode`/`spec-workflow`/`git-release`/`to-tickets`/`triage`/`git-master`/`resolving-merge-conflicts`/`opencode-config`/`writing-for-agents`/`diagnosing-bugs`/`codebase-design`/`domain-modeling`/`test-driven-development`/`verification-before-completion`；`oracle` → `reflect`/`simplify`/`diagnosing-bugs`；`reviewer` → `code-review`/`security-review`/`gh-cli`；`explore` → `codemap`；`librarian` → `verify-with-docs`；`light-orchestrator` → `handoff`/`simplify`/`spec-workflow`/`code-review`/`gh-cli`/`verification-before-completion`；`consultant` → `domain-modeling`；`ui-builder` → `codebase-design`；`vision` → `vision-prep`；`solo` → 全部本地 skill + `brainstorming`/`test-driven-development`/`verification-before-completion`/`writing-plans`/`executing-plans`（内联执行器需要完整工具链，仍以 `resource: "*"` 的 deny 规则兜底）。白名单不只是权限——**被 deny 的 skill 不会出现在该 Agent 的 skill 名册里**，所以这份名单同时就是常驻上下文预算。superpowers 侧只有这 4 项接线（`writing-plans`→`planner`，`test-driven-development`/`verification-before-completion`→`deep-worker`，`verification-before-completion`→`light-orchestrator`），其余 skill 在本仓库都有等价物，一律不接线以免两套流程打架——映射表见 `AGENTS.md` Plugins 小节。
>
> **思考分档（thinking tiers）**：全部声明在 `opencode.jsonc` 的**模型层**，Agent 只选模型引用（详见「模型分工」）。`explore`/`librarian`/`consultant`/`ui-builder`/`orchestrator`/`vision` = flash · thinking 关（最省，模型层 body `{"type":"disabled"}`）；`planner`/`light-orchestrator` = `deepseek/deepseek-flash#low`（thinking 开 + `settings.reasoningEffort: low`）；`deep-worker`/`oracle`/`reviewer`/`solo` = `deepseek/deepseek-flash#high`（thinking 开 + `settings.reasoningEffort: high`）。**Agent frontmatter 里不再出现任何思考参数**——`options.*` 既装不下 provider setting，也会把该 agent 打回 V1 迁移路径（见「模型分工」的写法陷阱）。

## 快捷命令

### Agent 路由命令

| 命令 | Agent | 用途 |
| --- | --- | --- |
| `/deep` | `deep-worker` | 重型实现、多文件改动 |
| `/quick` | `light-orchestrator` | 轻量任务、单文件编辑 |
| `/ui` | `ui-builder` | 前端/UI 工作 |
| `/vision` | `vision` | 多模态：图像/截图/图表理解 |
| `/review` | `light-orchestrator`（code-review）→ 按需升级 `reviewer` | 代码审查：默认 thinking off 初检（Abbreviated 路径直接出报告）；命中升级触发条件时委派 `reviewer`（`#high`）并传递未验证线索；带 PR ref/URL 时回帖 GitHub（event=COMMENT） |
| `/deep-review` | `reviewer`（code-review + gh-cli） | 强制 `#high` 全量审查，跳过便宜档初检；带 PR ref/URL 时回帖 GitHub（event=COMMENT） |
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
| `code-review` | 单遍代码审查 + 证据门控；thinking off 初检 / `#high` 升级两级；大 diff（>~500 行）拆 Standards/Spec 两轴合并报告 |
| `codemap` | 生成带标注的仓库结构图，快速定向，节省探索 token |
| `gh-cli` | GitHub CLI v2.103+ 参考：PR 回帖、api、rate limit、gh pr checks、gh skill/gh-aw、GHSA 安全要点 |
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

> **名册即预算**：`skill` 工具的 `<available_skills>` 里每个 skill 的 name + description 都是**每轮常驻**上下文。2026-10-16 实测（当时全部放行）：名册 **30 条**（23 个本地 + 7 个 superpowers 接线项），渲染后的名册块 **12,186 字符 / 11,927 字节 ≈ 3.3K tokens**——`orchestrator` 只放行 2 个 skill，名册仅 804 字符 ≈ 223 tokens，差 **15×**。2026-10-20 起 `solo` 裁掉 2 个与本地 skill 语义重复的接线项（`systematic-debugging` → `diagnosing-bugs`、`writing-skills` → `writing-for-agents`），其名册降到 28 条，已无任何 Agent 放行全部 30 条。所以「不再高频使用」或「与现有 skill 重复」的 skill 应当合并删除，而不是留着备用；超长的 `description` 要按触发词必需性裁剪。**被 deny 的 skill 不会出现在该 Agent 的名册里**（core 用 `skill` 权限规则集过滤名册），所以每个 Agent 的 allowlist 就是它的常驻上下文预算。**隐藏的一次性 agent（title/summary/compaction）已显式 `skill:* deny`——它们永远不加载 skill，名册对它们纯属浪费**。

## 仓库结构

```text
├── opencode/          # OpenCode 配置目录（agents/、skills/、opencode.jsonc、AGENTS.md）
├── docs/UPGRADE.md    # 升级流程：不变量、基线表、上游核对点、验收命令
├── scripts/           # sync-config.ps1（同步到全局配置）
│                      # validate-jsonc.js（JSONC 校验）
│                      # estimate-cost.js（按 token 数估算费用）
├── AGENTS.md          # 本仓库维护规则（自动加载，指向 docs/UPGRADE.md）
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
| 内置 agent（build/plan/title/summary/compaction/general）的模型、以及一次性 agent 的 skill 屏蔽 | `opencode.jsonc` | `agents`（inline `model` / `permissions`；inline 权限**追加在最后**，last-match-wins） |
| 快捷命令的 Agent 与模板 | `opencode.jsonc` | `commands` |
| 单个 Agent 的模型、思考档、工具与 skill 白名单、拒绝契约 | `opencode/agents/<name>.md` | frontmatter + 正文 |
| 全局行为规则（原则、失败纪律、缓存纪律、反模式） | `opencode/AGENTS.md` | 对应小节 |
| 插件版本（pin） | `opencode/opencode.jsonc` | `plugins` |
| 压缩触发窗口（按模型）/ buffer / 保留尾部 | `opencode/opencode.jsonc` | `providers.deepseek.models.<id>.limit.input` + `compaction`（`buffer`/`keep`） |
| Skill 的行为与触发词 | `opencode/skills/<name>/SKILL.md` | frontmatter `description` + 正文 |

> 改完必须 `.\scripts\sync-config.ps1` 同步到 `~/.config/opencode`，然后重载（桌面 v2：`opencode-cli.exe reload`；或重启 opencode）。校验：`node scripts/validate-jsonc.js`；查已解析结果：`opencode-cli.exe debug config`。

> 升级 opencode、插件或模型/定价时不要凭记忆改：按 `docs/UPGRADE.md` 走（不变量、基线表、上游核对点、验收命令）。仓库根的 `AGENTS.md` 会在本仓库的每个会话自动加载并指向它。

## 使用指南

Orchestrator 默认自动路由，按任务难度选档：

```text
「帮我排查这个登录接口的报错」   → oracle 分析根因（flash#high）
「优化这段循环，性能太差了」     → oracle 分析 → deep-worker 实施（flash#high）
「这个 PR 帮我审查一下」         → light-orchestrator 初检（命中升级条件才走 reviewer#high）
「我想给用户模块加个导出功能」   → planner 定方案 → deep-worker 实现（flash#low → flash#high）
「React 19 的 use() API 怎么用」 → librarian 查文档（thinking off）
```

需要直达时用命令：`/deep`（复杂实现）、`/quick`（单文件）、`/plan`（方案）、`/oracle`（排障）、`/review`（审查）、`/ui`、`/vision`；完整清单见「快捷命令」。

**什么时候才升 `#high`**：跨 ≥3 个模块、需读 5+ 文件才能决策、bug 无法一次定位、需要设计取舍。单文件、错误信息已指明位置、纯机械替换、有现成模式可照抄——都不升。先用便宜档跑一轮，失败或含糊再升，且升级要能说出理由。

## 借鉴来源

核心思路借鉴 [oh-my-openagent](https://github.com/code-yeongyu/oh-my-openagent)（意图门控、只读隔离、反模式）、[oh-my-opencode-slim](https://github.com/alvinunreal/oh-my-opencode-slim)（调度器优先、后备链、拒绝契约、提示词缓存安全）、[anomalyco/opencode](https://github.com/anomalyco/opencode)（配置 Schema、技能体系）、[cli/cli](https://github.com/cli/cli)（gh v2.103 命令集）、[OpenSpec](https://github.com/Fission-AI/OpenSpec)（delta specs）、[mattpocock/skills](https://github.com/mattpocock/skills)（冲突解析、交接文档、排障/架构/领域建模技能）、[pi](https://github.com/earendil-works/pi)（先答后改、精简响应）、[deepreview](https://github.com/mechanai/deepreview)（有效大小路由）。纯配置实现，零额外依赖。**借鉴而非照搬**：只汲取轻量化设计理念，精简优先于新增。

## 设计哲学

- **纯配置驱动，零额外依赖** —— 能力全部由 `opencode.jsonc` + `agents/*.md` + `skills/*/SKILL.md` + `AGENTS.md` 实现
- **单模型 + thinking 档位** —— 只用 `deepseek-flash`：thinking 关做路由与常规执行，`#low` 做规划，`#high` 做深度推理与重型实现；不为偶尔的难题常驻第二个模型的单价
- **Token 效率优先** —— 路径引用替代粘贴文件、技能按需加载；每个 Agent 的 `skill` 白名单同时决定它的名册大小，被 deny 的 skill 不进每轮常驻上下文
- **插件增效但不喧宾夺主** —— 唯一插件 superpowers 只提供过程纪律（固定 pin 保字节稳定前缀）；压缩 100% 交给内置 compaction
- **执行与探索分离** —— deep-worker/light-orchestrator 禁止研究与委托，explore/librarian 禁止修改
- **缓存与 thinking 纪律** —— 静态前缀稳定以命中提示词缓存，易变内容（时间戳/随机 ID/动态列表）置于 payload 尾部；默认档关 thinking + `temperature: 0`，`#low`/`#high` 开 thinking
- **Scope First + Delegate Always** —— 2+ 步/多文件/架构变更先走 planner，再委派执行，顶层 token 只留给路由与难题
- **显式压缩窗口** —— 用 `limit.input` 声明工作窗口：`131072−16000=115,072`，触发点算出来而不是继承
- **视觉 opt-in** —— 图像只在用户提供时进入 payload；`media.image` 自动缩放超大图
