# 升级指南

本文件是本仓库升级流程的唯一事实源。仓库根的 `AGENTS.md` 指向它；两份 README 只描述**当前状态**，不记录历史。

升级时按下面的顺序执行，不要凭记忆改配置。

## 1. 不变量

任何一次升级都必须保住这八条。破坏其中一条，就不算升级完成：

1. **单模型**：只有 `deepseek/deepseek-flash`，四个 thinking 档位——默认（thinking 关）、`#low`、`#high`、`#max`。
2. **thinking 参数只在模型层**：toggle 进 `providers.deepseek.models.<id>.body`，effort 进同名节点的 `settings.reasoningEffort`；Agent frontmatter 不出现任何思考参数。
3. **只写 v2 原生键**：v2 的配置正常化器会把 v1 键逐项迁移后与原生键合并，`logLevel`/`server`/`layout`/顶层 `subagent_depth` 则丢弃并记诊断——不报错，但实际生效的配置与本文描述不一致。
4. **`$schema` 固定为** `https://opencode.ai/config.json`。
5. **插件固定 pin**（当前 `#v7.0.2`），不跟 `latest`。
6. **零额外依赖**：能力只来自 `opencode.jsonc` + `agents/*.md` + `skills/*/SKILL.md` + `AGENTS.md`；压缩只用内置 compaction。唯一例外是 `video-studio` 技能自带 Python 脚本（不引入 npm 依赖；运行时另需 ffmpeg + numpy/Pillow/edge-tts，见 README「技能」）。
7. **README 不当变更记录**：历史在 `git log`，理由在 commit message。
8. **agent 级 permissions 不得放宽全局 deny**：`agents/*.md` frontmatter 与 `opencode.jsonc` 内 inline agent 的 `edit`/`shell`/`read`/`external_directory` allow 规则，不得命中全局 `permissions` 的 deny 模式；由 `scripts/validate-jsonc.js` 静态检查。

## 2. 基线

升级只改这张表的数字，以及 README 中引用这些数字的句子；两者不一致就是缺陷。

| 项 | 当前值 | 最近核验 |
| --- | --- | --- |
| 运行时 CLI（桌面版） | `opencode-cli.exe` **2.3.2**（桌面版自带；`update: notify`，需手动重启更新） | 2026-11-23 |
| 配置校验 CLI | 独立 `opencode.exe` **2.4.0**（GitHub release，`--standalone` 读取同一 `~/.config/opencode`） | 2026-11-23 |
| PATH 上的 `opencode` | **1.18.4**（WinGet 安装，v1，不要用） | 2026-11-23 |
| superpowers pin | `#v7.0.2`（当前最新 tag） | 2026-11-23 |
| DeepSeek 定价（USD / 1M，off-peak） | in 0.15 / out 0.60 / cache_read 0.003 / cache_write 0.15 | 2026-11-23 |
| thinking 语义 | 默认**开**、effort 默认 `high`；思考模式忽略 `temperature` | 2026-11-23 |
| 结构 | 1 模型 / 4 档 / 12 agents / 18 commands / 24 skills | 2026-11-23 |
| 压缩窗口 | `limit.input` 131072 − `compaction.buffer` 16000 = 115,072 | 2026-11-23 |

**为什么分两行**：v2.4.0（2026-11-23 发布）是当前配置的目标 schema；桌面版仍停在 2.3.2，且 `update: notify` 不会自动安装。安装桌面更新会重启 OpenCode（当前会话中断），因此由用户手动完成；在此之前用独立 2.4.0 CLI 的 `--standalone` 校验配置与 agent。桌面版更新后，把第一行的值改成 2.4.0。

定价附注：peak 时段 = off-peak ×2（UTC 01–04 与 06–10 的周一至周五，不含中国法定节假日）；`cache_write` 无独立官方价，按 cache-miss 输入价映射。

运行时 CLI 的路径：

```powershell
$cli = "$env:LOCALAPPDATA\Programs\@opencode-aidesktop\resources\opencode-cli.exe"
& $cli --version
```

## 3. 升级步骤

### 3.0 定目标

先写清三件事：升什么（CLI / 插件 / 模型与定价 / 配置 schema）、从哪个版本到哪个版本、为什么升。目标不明确就不动手——顺手升级通常只带来前缀漂移。

### 3.1 读上游，不凭记忆

按升级对象选读。行号会随上游漂移，用符号名检索：

| 升级对象 | 读哪里 |
| --- | --- |
| CLI 行为 / 配置 schema | opencode 源码仓库 `anomalyco/opencode` 的 `packages/core/src/config/`，并对照 `& $cli debug config` 的实际输出 |
| CLI 版本 / 发布说明 | 同仓库 GitHub 的 `/releases` 列表（**不要用 `/releases/latest`**，它仍指向 v1 线）；同时读 `Upgrade notes`，2.4.0 起权限动作、工具名与插件更新语义都有变化 |
| v2.4.0 新键 / 权限动作 | `releases/tag/v2.4.0` 的 Upgrade notes、`packages/schema/src/config.ts`（`automations`、`update`）与 `packages/schema/src/permission.ts`（动作是开放字符串，`browser` 是新增动作）；对照 `debug config` 的实际 `info` |
| v1 键的处理方式 | 同仓库 `packages/core/src/config/normalize.ts` 的 `unsupportedTopLevel`（丢弃并记诊断）与 legacy 键分支（逐键迁移后与原生键合并）；转换函数在 `packages/core/src/v1/config/migrate.ts` |
| 模型合并语义 | 同仓库 `packages/core/src/provider.ts` 的 `mergeOverlay` |
| 请求体如何上线路 | 同仓库 `packages/core/src/aisdk.ts`（`settings` → provider 选项）与 `packages/ai/src/protocols/openai-chat.ts`（选项 → 请求 body） |
| AGENTS.md 加载范围 | 同仓库 `packages/core/src/config/plugin/instruction.ts` |
| DeepSeek 定价 | <https://platform.deepseek.com/> 的定价页 |
| DeepSeek thinking 语义 | <https://api-docs.deepseek.com/zh-cn/guides/thinking_mode> |
| 插件 | 目标 tag 的 release notes，以及 `.opencode/plugins/superpowers.js` 在两 tag 间的 diff |

### 3.2 改配置

| 改什么 | 改哪里 |
| --- | --- |
| 模型、价格、thinking、上下文窗口 | `opencode/opencode.jsonc` 的 `providers.deepseek.models` |
| Agent 的模型引用、工具、skill 白名单 | `opencode/agents/<name>.md` 的 frontmatter 与正文 |
| 浏览器 / 定时任务策略 | `opencode/opencode.jsonc` 的 `permissions`（`browser: deny`）与顶层 `automations` |
| 路由、拒绝契约、缓存纪律 | `opencode/AGENTS.md` |
| 快捷命令模板 | `opencode/opencode.jsonc` 的 `commands` |

变体 body 只写实际要覆盖的字段：`mergeOverlay` 只在基值为 `undefined` 时保留基值，写 `null` 会原样出现在请求体里。

### 3.3 校验

`$cli` 见第 2 节。

```powershell
node scripts/validate-jsonc.js        # 退出码 0
node scripts/estimate-cost.js         # 默认参数合计约 $0.026
& $cli debug config                   # 键名与模型清单符合预期（无 unsupported / conflict 诊断）
& $cli debug agents                   # 12 个自定义 Agent，模型引用全部可解析
```

桌面版落后于目标版本时（本仓库当前就是这种状态），下载对应平台的 standalone 包，用**同一份** `~/.config/opencode` 校验目标版本：

```powershell
& .\opencode.exe --version                         # 目标版本，例如 v2.4.0
& .\opencode.exe --standalone debug config         # 2.4.0 loader 看到的配置（含新键）
& .\opencode.exe --standalone debug agents         # 12 个 Agent + 权限展开
```

再确认没有 v1 触发键——清单以 `packages/core/src/config/normalize.ts` 的 `unsupportedTopLevel` 与 legacy 键分支为准（`scripts/validate-jsonc.js` 的 `V1_TOP_LEVEL_KEYS` 只是手工镜像，以 `normalize.ts` 为准），尤其是顶层别出现 `provider` / `agent` / `command` / `plugin` / `permission` / `tools` / `attachment`。

### 3.4 同步与重载

```powershell
.\scripts\sync-config.ps1             # ~/.config/opencode 是独立副本，不是符号链接
& $cli reload                         # 或重启 opencode
```

`sync-config.ps1` 只同步 `opencode/` 目录；仓库根的 `AGENTS.md` 与 `docs/` 不会被同步。

### 3.5 文档对齐

- `README.md` 与 `README.en-US.md` 逐节同步：标题层级、顺序、数量一致
- 全文件 CRLF：`LF` 计数必须等于 `CRLF` 计数
- 交叉引用（`见「…」` / `see "…"`）必须落到现存标题
- README 中的版本号、定价、结构计数与第 2 节基线一致

### 3.6 记录

- commit message 写**为什么**与影响面，不逐行复述 diff
- 只暂存本次意图内的文件
- 不新增 changelog：历史看 `git log`

## 4. 已知陷阱

一行一条，都实测或核对过：

1. **`thinking` / `temperature` 写在 `settings` 下不生效** —— `settings` 只承载 provider option（`timeout` / `chunkTimeout` / `reasoningEffort`），其余被静默丢弃（2026-10-16 实测）。
2. **`reasoningEffort` 写在 Agent 的 `options` 下不生效** —— `options` 只落 `request.body`，且 camelCase 不是 API 认的 `reasoning_effort`。
3. **变体 body 写 `null` 会覆盖基值** —— `mergeRecord` 只把 `undefined` 当"未设置"；`null` 是合法 JSON 值，会原样出现在请求体里。
4. **v1 键不报错，只会被静默改写** —— v2.4.0 把 `provider`/`agent`/`command`/`plugin`/`permission`/`tools`/`attachment` 等 legacy 键逐键迁移后与原生键合并，`logLevel`/`server`/`layout`/顶层 `subagent_depth` 记 `unsupported` 诊断后丢弃；文件不会被整体拒绝（只有 JSONC 语法错误或归一化后无法解码才会拒绝，见 `config.ts`）。「看起来生效」不是写对了的证据，以 `scripts/validate-jsonc.js` 的键白名单为准。
5. **PATH 上的 `opencode` 是 v1.18.4** —— headless 运行必须用桌面版自带 CLI。
6. **DeepSeek 默认开思考、effort 默认 `high`** —— 不显式写 `disabled` 就一直在付 reasoning token。
7. **思考模式下 `temperature` / `presence_penalty` / `frequency_penalty` 被忽略**；`top_p` 只在思考模式生效（有效范围 0.95–1.0），非思考模式恒为 1.0。所以这些参数只在默认档有意义。
8. **带 `tools` 时必须回传历史 `reasoning_content`**，否则 API 返回 400；opencode 对所有 deepseek 模型强制回传（`openai-chat.ts` 的 `requireReasoning`），无需额外配置。
9. **`write` 工具落盘带 UTF-8 BOM** —— 用作 commit message 前先剥掉，否则 subject 首字符是 U+FEFF。
10. **技能名册是每轮常驻成本** —— 新增 skill 或加长 `description` 直接抬高每轮 token；不再使用的 skill 应当合并或删除。
11. **`/releases/latest` 指错版本线** —— 2026-11-23 核对：`/releases` 顶部最新 v2 为 v2.4.0（2026-11-23 发布）；`/releases/latest` 仍指 v1 线（当前 v1.19.6，2026-11-17 发布）。判断版本只认 `/releases` 列表 + CLI `--version`，不要用 latest 重定向，也不要用 PATH 上的 1.18.4。
12. **插件 bump 的闸门不是字节一致** —— 要读目标 tag 的 release notes，并 diff `.opencode/plugins/superpowers.js` 在两个 tag 间的变化；该文件是整套 opencode 集成。v7.0.1→v7.0.2 为 +61/−39（BOOTSTRAP_MARKER、startsWith 守卫、钩子扩到 context/compaction/generate、裸 skill id），注入请求类型变化会改变 compaction payload（2026-11-09 核对）。v2.4.0 起只有浮动引用（`@latest`、版本范围、分支、`semver:`）会自动更新；**精确 tag / commit 仍只手动更新**——本仓库的 `#v7.0.2` 不受自动更新影响。
13. **`max`/`ultra` 归一到 `max`，`medium`/`xhigh` 归一到 `high`** —— 上游模型有四个真实等级，`#max` 与 `#high` 有实质差别。
14. **自建 id 为 `max` 的变体与内置 `deepseekChat` 的同名变体合并** —— `mergeOverlay` 是合并，不会多出一条。
15. **`reasoning_effort` 按输出 token 计费** —— `#max` 只改变 reasoning token 数，不改变单价。
16. **v2.3.0 起子代理始终后台运行** —— 委派立即返回 `sessionID`，子会话空闲时向父会话上报（#63382）；`OPENCODE_EXPERIMENTAL_BACKGROUND_SUBAGENTS` 不再是开关，「未设置则退回前台」在 2.3.0 上不成立。父会话按 `sessionID` 收集结果，不要轮询。
17. **v2.3.1 起后台压缩默认开启** —— `compaction.background` 默认 `true`，会在触发压缩前预写摘要以减少停顿；未被使用的摘要仍按 token 计费。本仓库显式设为 `false`（成本优先），需要交互低延迟时才改回 `true`。
18. **v2.3.1 起超时命令转入后台，`timeout: 0` 不再是无限等待** —— 前台命令超时后继续在后台运行并回报结果；前台 `timeout: 0` 现在套用默认 120 秒。脚本需要长任务时显式给大值，不要用 0 表达「不超时」。
19. **v2.3.1 起目录没有 `AGENTS.md` 时会回退读取 `CLAUDE.md`** —— 全局 `AGENTS.md` 缺失时还会回退 `~/.claude/CLAUDE.md`。本仓库有 `AGENTS.md`，不受影响；但复制配置到只带 `CLAUDE.md` 的目录时，旧规则会被重新加载。
20. **v2.4.0 的 `browser` 是权限动作，不只是 UI** —— 桌面内置浏览器可读取/操作已登录页面；全局 `{action: browser, resource: "*", effect: deny}` 会拒绝**所有 agent** 的浏览器调用，手动使用浏览器面板不受影响。没有这条 deny 时，45 个 browser 工具会进入每个 agent 的工具列表。
21. **v2.4.0 的 automations 默认开启** —— 可定时跑 prompt / shell / Code Mode；顶层 `automations: false` 才是关闭开关。`experimental.automations` 与 `experimental.schedules` 在 2.4.0 被标为 removed（只认顶层键）。
22. **v2.4.0 图片工具改名** —— `experimental_image_generate` → `generate_image`；旧权限规则会自动映射，但用户点过的 "always allow" 不映射，会重新问一次。本仓库没有旧规则，无需改。
23. **v2.4.0 Code Mode session 工具路径改变** —— `tools.opencode.session_rename` / `session_move` → `tools.opencode.session.rename` / `.move`；直接工具名与权限动作不变。仓库内没有硬编码这些路径。
24. **`variant` 不是 agent frontmatter 键** —— v2.3.2 与 v2.4.0 的 `ConfigAgent.Info` 都没有 `variant`；思考档位用模型后缀（`#low`/`#high`/`#max`）。`variant` 是 command 字段，别写进 `agents/*.md`。
25. **v2.4.0 配置重载范围收窄** —— 只有喂给配置的文件变化才触发重载；改 `skills/` 下的文件不再触发整份配置重载。改完仍按 3.4 跑 `sync-config.ps1` + `reload`，强制重启用 `service restart`。

## 5. 验收清单

全部勾上方可宣告升级完成：

- [ ] `node scripts/validate-jsonc.js` 退出码 0（配置 + 12 个 agent frontmatter + 24 个 skill frontmatter + 权限放宽检查均无报错）
- [ ] `node scripts/estimate-cost.js` 退出码 0，且输出成本估算（默认参数应约 $0.026）
- [ ] `& $cli --version` 与第 2 节“运行时 CLI（桌面版）”一致；若桌面版已更新，同时把基线改成新版本
- [ ] standalone 目标 CLI 的 `--version` 是目标版本（当前 2.4.0），且 `--standalone debug config` / `debug agents` 无诊断
- [ ] `& $cli debug config` 输出中没有 unsupported / conflict 诊断；模型只有 `deepseek-flash` 四档（默认 / `#low` / `#high` / `#max`）
- [ ] `& $cli debug agents` 列出 12 个自定义 Agent，模型引用全部可解析；2.4.0 standalone 的同一命令结果一致
- [ ] `automations: false` 出现在 2.4.0 `debug config` 的 `info` 中；权限列表含 `browser: deny`
- [ ] `.\scripts\sync-config.ps1` 成功，`& $cli reload` 后 `/models` 与 Agent 列表符合预期
- [ ] 两份 README 标题逐条对齐，`LF = CRLF`
- [ ] 交叉引用全部可达
- [ ] 第 2 节基线表与 README 中的版本号 / 日期 / 计数一致
- [ ] `git status` 只含本次意图内的文件

## 6. 本文件何时改

- **基线数字变了** → 只改第 2 节，以及 README 中引用它的句子
- **踩到新坑** → 追加到第 4 节，一条一行，带日期或出处
- **流程本身变了** → 才改第 3 节

一次性排查过程不写进来，它不是规则。
