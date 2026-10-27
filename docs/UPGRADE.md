# 升级指南

本文件是本仓库升级流程的唯一事实源。仓库根的 `AGENTS.md` 指向它；两份 README 只描述**当前状态**，不记录历史。

升级时按下面的顺序执行，不要凭记忆改配置。

## 1. 不变量

任何一次升级都必须保住这七条。破坏其中一条，就不算升级完成：

1. **单模型**：只有 `deepseek/deepseek-flash`，三个 thinking 档位——默认（thinking 关）、`#low`、`#high`。
2. **thinking 参数只在模型层**：toggle 进 `providers.deepseek.models.<id>.body`，effort 进同名节点的 `settings.reasoningEffort`；Agent frontmatter 不出现任何思考参数。
3. **只写 v2 原生键**：文件里出现任一 v1 触发键，v2 就整份改走迁移路径。
4. **`$schema` 固定为** `https://opencode.ai/config.json`。
5. **插件固定 pin**（当前 `#v7.0.1`），不跟 `latest`。
6. **零额外依赖**：能力只来自 `opencode.jsonc` + `agents/*.md` + `skills/*/SKILL.md` + `AGENTS.md`；压缩只用内置 compaction。
7. **README 不当变更记录**：历史在 `git log`，理由在 commit message。

## 2. 基线

升级只改这张表的数字，以及 README 中引用这些数字的句子；两者不一致就是缺陷。

| 项 | 当前值 | 最近核验 |
| --- | --- | --- |
| 运行时 CLI | `opencode-cli.exe` **2.2.0**（桌面版自带） | 2026-10-27 |
| PATH 上的 `opencode` | **1.18.4**（WinGet 安装，v1，不要用） | 2026-10-27 |
| superpowers pin | `#v7.0.1` | 2026-10-27 |
| DeepSeek 定价（USD / 1M，off-peak） | in 0.15 / out 0.60 / cache_read 0.003 / cache_write 0.15 | 2026-10-27 |
| thinking 语义 | 默认**开**、effort 默认 `high`；思考模式忽略 `temperature` | 2026-10-27 |
| 结构 | 1 模型 / 3 档 / 12 agents / 18 commands / 23 skills | 2026-10-27 |
| 压缩窗口 | `limit.input` 131072 − `compaction.buffer` 16000 = 115,072 | 2026-10-27 |

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
| v1→v2 迁移边界 | 同仓库 `packages/core/src/v1/config/migrate.ts` 的 `isV1` 键表 |
| 模型合并语义 | 同仓库 `packages/core/src/provider.ts` 的 `mergeRecord` |
| 请求体如何上线路 | 同仓库 `packages/core/src/ai/protocols/` 下的 `aisdk.ts` 与 `openai-chat.ts` |
| AGENTS.md 加载范围 | 同仓库 `packages/core/src/config/plugin/instruction.ts` |
| DeepSeek 定价 | <https://platform.deepseek.com/> 的定价页 |
| DeepSeek thinking 语义 | <https://api-docs.deepseek.com/zh-cn/guides/thinking_mode> |
| 插件 | 目标 tag 的 release notes |

### 3.2 改配置

| 改什么 | 改哪里 |
| --- | --- |
| 模型、价格、thinking、上下文窗口 | `opencode/opencode.jsonc` 的 `providers.deepseek.models` |
| Agent 的模型引用、工具、skill 白名单 | `opencode/agents/<name>.md` 的 frontmatter 与正文 |
| 路由、拒绝契约、缓存纪律 | `opencode/AGENTS.md` |
| 快捷命令模板 | `opencode/opencode.jsonc` 的 `commands` |

变体 body 只写实际要覆盖的字段：`mergeRecord` 只在基值为 `undefined` 时保留基值，写 `null` 会原样出现在请求体里。

### 3.3 校验

`$cli` 见第 2 节。

```powershell
node scripts/validate-jsonc.js        # 退出码 0
& $cli debug config                   # 键名与模型清单符合预期（无 v1 迁移痕迹）
& $cli debug agents                   # 12 个自定义 Agent，模型引用全部可解析
```

再确认没有 v1 触发键——清单以 `migrate.ts` 的 `isV1` 为准，尤其是顶层别出现 `provider` / `agent` / `command` / `plugin` / `permission` / `tools` / `attachment`。

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
4. **一个 v1 触发键就够** —— 整份配置改走迁移路径，本文件与 README 描述的行为全部失效。
5. **PATH 上的 `opencode` 是 v1.18.4** —— headless 运行必须用桌面版自带 CLI。
6. **DeepSeek 默认开思考、effort 默认 `high`** —— 不显式写 `disabled` 就一直在付 reasoning token。
7. **思考模式下 `temperature` / `presence_penalty` / `frequency_penalty` 被忽略**；`top_p` 只在思考模式生效（有效范围 0.95–1.0），非思考模式恒为 1.0。所以这些参数只在默认档有意义。
8. **带 `tools` 时必须回传历史 `reasoning_content`**，否则 API 返回 400；opencode 对所有 deepseek 模型强制回传（`openai-chat.ts` 的 `requireReasoning`），无需额外配置。
9. **`write` 工具落盘带 UTF-8 BOM** —— 用作 commit message 前先剥掉，否则 subject 首字符是 U+FEFF。
10. **技能名册是每轮常驻成本** —— 新增 skill 或加长 `description` 直接抬高每轮 token；不再使用的 skill 应当合并或删除。

## 5. 验收清单

全部勾上方可宣告升级完成：

- [ ] `node scripts/validate-jsonc.js` 退出码 0，无报错
- [ ] `& $cli --version` 是目标版本
- [ ] `& $cli debug config` 输出中没有 v1 迁移痕迹；模型只有 `deepseek-flash` 三档
- [ ] `& $cli debug agents` 列出 12 个自定义 Agent，模型引用全部可解析
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
