# 场景语法（Scene Grammar）：内容决定构图

> 配套文件：`skills/video-studio/scripts/grammar.py`（纯标准库）、
> `skills/video-studio/assets/scenes.py` 第三节（10 类绘制器）、`tools/silhouette_qa.py`（剪影门禁 S13，离线检查）。
> 一句话：**这一段内容该长成什么构图，由内容自己决定；风格只决定"长成什么样"。**

## 0. 为什么需要它

设计层只覆盖**皮肤**：配色、字体族、纹理、颗粒、字距、缓动、HUD 三档、3 种版式几何。
皮肤换完，画面骨架并不会跟着变——同一套骨架（背景板 → 眉标 → 大字标题 → 副标题 → 强调线 → HUD）
只换颜色、字号、位移，**剪影（灰度三值化后的前景占比 / 重心 / 边缘占用 / 对称度）几乎不动**。

这不是审美判断，是可以量化的：用 `tools/silhouette_qa.py` 的判据两两对比 28 对同题材设计，
同明暗族 12 对里有 4 对五项指标一项都不达标——换皮肤真的只是换色。
用户的原话是"每个视频都差不多，只是主题和颜色不同"。

所以缺的那一层不是"更多风格"，而是把"内容 → 构图"这条选择逻辑独立出来。

## 1. 三层正交

    film.json（内容）
       │  ① 场景语法：这段内容该长什么样（由内容决定，不由风格决定）
       ▼
    scene_type ∈ {hero, statement, list, stat, compare, process, quote, chapter, chart, outro}
       │  ② 设计（design.json）：这套设计怎么画（motion / motif / transition / typography / bias）
       ▼
    work/design.json 的 visual 五组签名（designkit.design_to_plan 转成 plan）
       │  ③ 片内编排：同一部片里连续段落怎么不"复制粘贴"（变体轮换 / 装饰密度 / 相位，确定性）
       ▼
    draw_frame(...) → PIL.Image

三层的边界要分清，否则会写出"改了不生效"的配置：

| 层 | 由谁决定 | 落在哪 | 改它会怎样 |
|---|---|---|---|
| 叙事 kind | 作者写 `lines[].kind` | hook / body / quote / outro / emphasis / calm | 换旁白语气与配乐能量（voicekit / musicgen 读它） |
| 场景 type | grammar 按内容打分，或作者显式写 `lines[].type` | `work/storyboard.json`、timeline 的 `scene_type` | 换构图（10 个绘制器之一） |
| 设计 visual | `work/design.json`（由 `propose.py --confirm` 冻结） | `work/style.json` 的 `language` 与 `layout_cycle` | 换母题 / 转场 / 字距 / 重心 / 版式轮换 / 字幕样式 |

**为什么没有新增 kind 值**：kind 同时是 voicekit 的语气 id 与 filmcheck 的 `KINDS`
（两者是同一组 6 个值）。把 hero/stat 塞进 kind 会撞语气枚举，也让"引语"和"引语构图"
这种本来可以分开的两件事重新粘死。所以 type 是**新字段**，与 kind 正交：
一句话可以是 `kind=quote`（女声念引语）同时 `type=chapter`（画成章节卡）。

## 2. 10 类构图

坐标基于 1920×1080；正文底界 860（下方是字幕带），HUD 顶部 ≤96。
"最短时长"是 `grammar.MIN_DUR`（`skills/video-studio/scripts/grammar.py:49`）的硬门槛，
低于它按降级表换类型（见 §3.3）。类型名单与顺序在 `grammar.SCENE_TYPES`（grammar.py:30），
绘制器注册表在 `assets/scenes.py` 的 `_DRAWERS`（搜这个名字，行号会漂）。

| 类型 | 构图 / 重心 | 吃哪些字段 | 持续动效 | 自动选型特征 | 最短 |
|---|---|---|---|---|---|
| `hero` 封面 | 左栏起标题，右侧留白；variant 0/1/2 分别左/右/中 | `big` / `sub`(lead) / `kicker` / `chapter` / 顶层 `title` | 标题下强调线呼吸 + 背景同心弧缓推 | 首段或 `kind=hook` | 2.4 s |
| `statement` 金句 | 居中单列 ≤1400，逐字显现（字距随呼吸微动） | `big` 或 `text` 前 24 字；`sub` 作落款 | 下划线从左生长 + 字距呼吸 | 无条目且短句（≤16 字） | 2.6 s |
| `list` 要点 | 左标题 + 右条目列；variant 1 转全宽序号列 | `items[]`、`big`、`kicker` | 焦点行每 2.2 s 换一条（左侧竖条 + 行底微亮） | `items ≥ 3` | 3.0 s |
| `stat` 数字卡 | 巨数字居中（初始 240 px，超宽自动缩）+ 单位 + label | `stats[]`；缺省从 `text`/`sub` 正则抽"数字+单位" | count-up + 底部 12 格刻度逐格点亮 | 文本含数字+单位且 `items < 3` | 3.2 s |
| `compare` 左右对比 | 中缝竖线，左右双栏；栏头缺省时中缝上方画 VS 徽标 | `sides{}`（`left/right` 各 `head/items`）；缺省把 `items` 前后对半分 | 中缝高光下扫 + 左右两栏轮流反相高亮（3.2 s） | 命中对比词且 `items ≥ 2` | 4.0 s |
| `process` 流程轴 | 横向轴 + 最多 5 个节点，标签在轴上方、注在下方 | `steps[]`（`label/note`）；缺省按 `items` 顺序当步骤 | 进度光点沿轴匀速走完本段 | 命中顺序词且 `items` 3–6 条 | 4.0 s |
| `quote` 引语 | 左上巨型引号（字形不是矢量线）+ 引文 46 px 起 + 右下出处 | `quote` / `by`；缺省从 `text` 剥引号与出处 | 引号呼吸上浮 + 末行光标块闪烁 | 命中引语标记（「」""——说：…） | 4.0 s |
| `chapter` 章节卡 | 左上巨型编号（矩形裁剪揭示）+ 章节名 + 中缝横线 | `chapter`（编号从 `"03 · xxx"` 里取）/ `big` / `kicker` | 细光带在编号上缘横向扫（2.8 s 周期） | 章节变化 + 无条目 + 标题短 + 段短 + 无数字/引语 | 2.2 s |
| `chart` 数据条 | 左侧标签列 + 横向条 + 数值；最多 6 条 | `series[]`（`label/value/unit`）；缺省从 `items` 抽"标签+数字" | 最大条端点呼吸 + 网格高亮扫描 | `series` 非空，或 `items ≥ 3` 且 ≥3 条带数字 | 4.5 s |
| `outro` 收尾 | 正中心大字 + 副标 + 强调线；光点环绕，数量随母题变 | `big` / 顶层 `end_text` / `sub` / `title` | 强调线呼吸 + 光点环向内收 | 末段或 `kind=outro` | 2.4 s |

四条兜底（绘制器里的运行时兜底，与 §3.3 的段长门是两回事）：

- `stat` 拿不到任何数字 → 退到 `statement`；
- `process` 步骤 < 2 条 → 退到 `list`；
- `chart` 序列 < 2 条 → 退到 `stat`；
- `compare` 对半分不出两栏 → 退到 `statement`。

**容量上限**（超了会被静默截断，画面少几行）：`stats` 画前 3 条、`series` 前 6 条、
`steps` 前 5 个、`sides` 每侧前 3 条。filmcheck 对超容量报 `scene_capacity` **WARN**
（不阻断，但说明"那几条不会出现在画面上"）。

## 3. 自动选型：打分 → 段长门 → 降级 → 去重

选型是纯函数（`grammar.score_types` / `choose_type` / `downgrade_for`，
`skills/video-studio/scripts/grammar.py:202/303/315`），不读 random / time / hash()，
派生量只用 sha1 前 8 位。**同一份稿子永远同一张分镜表。**

### 3.1 打分

原则：**字段存在性 > 关键词 > 结构位置**。字段是作者明说的意图，关键词是猜的。

| 信号 | 加权 | 条件 |
|---|---|---|
| 显式 `type` | +10.0 | 值在 `SCENE_TYPES` 里；压倒一切 |
| `stats[]` 非空 | +6.0 | → stat |
| `series[]` 非空 | +6.0 | → chart |
| `sides{}` 非空 | +6.0 | → compare |
| `steps[]` 非空 | +6.0 | → process |
| `quote` 非空 | +6.0 | → quote |
| `items ≥ 3` | +5.0 | → list（通用兜底，所以权重低于更具体的形态） |
| `items ≥ 3` 且 ≥3 条带数字 | +5.5 | → chart |
| 命中对比词且 `items ≥ 2` | +5.5 | → compare |
| 命中顺序词且 `items` 3–6 | +5.5 | → process |
| 命中引语标记 | +5.5 | → quote |
| 文本含"数字+单位"且 `items < 3` | +3.5 | → stat |
| 无条目且短句（≤16 字） | +3.5 | → statement |
| 首段或 `kind=hook` | +4.6 | → hero |
| 末段或 `kind=outro` | +5.0 | → outro |
| 章节变化 + 纯标题卡 | +4.2 | → chapter；还要满足无条目、标题 ≤14 字、段 ≤4.0 s、无数字、无引语 |

几个刻意的取舍（都是实测反例逼出来的，别"优化"掉）：

- **关键词只看叙述文字，不看 `items`**。反例：示例片第 3 段正文讲"画质三件事"，
  条目里写的是 `rawvideo -> libx264 CRF17`，`->` 命中顺序词表，整段被误判成"流程轴"——
  画面上一条横轴连着四个不相干的要点。条目是载荷不是叙事。
- **`hero`/`outro` 的结构分刻意低于字段分**：开场如果带 5 条要点，观众要看的是要点，
  不是封面。结构位置只是"没有更具体形态时"的兜底。
- **`list` 只给 5.0**：它是"有条目"的通用兜底；一旦有更具体的形态（数字、对比、流程），
  必须压过它——否则"能画"永远赢过"该画"，所有带条目的段都会退化成列表。
- **年份不算数字**：单位表里有"年"，但 `2024 年` 这种纯年份被单独排除，
  它是时间背景不是"值得做成巨字卡"的数据。真要做数字卡，写 `stats[]`。

平手时按 `grammar.SCENE_TYPES` 的固定顺序取位（**改顺序会让同一稿子的分镜漂移**，
所以这个元组有独立语义，不能随便排序）。

### 3.2 段长门

每类有最短时长（`MIN_DUR`，见 §2 表格）。段长不足时**不是降分，是不许选**：
候选保留但分数 −6.0 并写明理由，便于审计；若所有候选都被段长门排除，退到 `statement`
（最耐短的类型）。

### 3.3 降级表

超过段长门后仍可能被降级（例如显式写了 `type=chart` 但这段只有 2 秒）：

| 原类型 | 降级到 |
|---|---|
| `chart` / `compare` / `process` | `list` |
| 其余（含 `list`、`hero`、`chapter`、`stat`、`quote`、`outro`） | `statement` |

降级表写死在 `grammar.DOWNGRADE`（grammar.py:63）里，防环（配置写错时不会挂死）。
降级不是静默的：`storyboard.json` 的 `reason` 里会写
"段长 x.xxs < y.ys，降级 chart -> list"。

### 3.4 类型去重

连续同类型最多 `MAX_REPEAT = 2` 段（grammar.py:77）。第 3 段如果还是同一类型，该候选 −5.0
（`chapter` / `outro` 除外——章节卡连排是刻意的分章锚点）。
效果是"三连 list"会变成 list → list → 下一个候选（通常是 statement 或别的更具体形态）。

## 4. `work/storyboard.json`：审计产物

`--stage layout`（以及 `--stage all`）会把选型结果落盘到 `work/storyboard.json`，
并在控制台打印一行分镜摘要 + 每段的"为什么"：

```bash
python render.py --script film.json --scenes scenes.py --work work --stage layout
#   分镜: 4 段 / 封面×1 要点列表×2 收尾×1
#     l01    hook     -> hero      v0 d0  big 后缩进
#     l02    body     -> list      v0 d2  items>=3
#     l03    body     -> list      v2 d1  items>=3
#     l04    outro    -> outro     v0 d0
```

字段（`grammar.plan_scenes`（grammar.py:467）的返回结构）：

| 字段 | 含义 |
|---|---|
| `version` | 目前恒为 1 |
| `generator` | `"grammar 0.9.0"`（改版本号时同步，便于区分不同版本的分镜表） |
| `style` / `legacy` | 本片设计标识（`design:<hash>`）/ 兼容位（恒 `false`，见 §7） |
| `scenes[]` | 每段一行：`id`、`kind`、`type`、`variant`(0–2)、`decor`(0–2)、`layout`、`dur`、`reason[]` |
| `type_counts` | `{"hero": 1, "list": 2, ...}`，给了哪些类型用过几次 |

同一份结果也会**就地**写进 timeline 的每个 scene（渲染器读这些字段）：
`scene_type` / `scene_variant` / `scene_decor` / `scene_layout` / `scene_phase` / `scene_reason`。
timeline 顶层再加一个 `storyboard: {"type_counts": ...}` 摘要。

想只看选型、不跑 TTS：`python grammar.py film.json`（按字数粗估时长，只用于看选型，
不是时间轴权威）。

## 5. 显式指定 `lines[].type`

不写 `type` 就自动选；写死在 `film.json` 的行级字段上（全部可选）：

```json
{"id": "l03", "kind": "body", "text": "残差网络在 2015 年把错误率降到 3.6%。",
 "type": "stat",
 "stats": [{"value": "3.6", "unit": "%", "label": "ImageNet top-5 错误率", "note": "2015"}],
 "chapter": "02 · 数据"}
```

| 字段 | 类型 | 给谁用 | 说明 |
|---|---|---|---|
| `type` | 枚举（10 类） | 绘制器派发 | 写了就以它为准（+10 分），但仍受段长门降级 |
| `quote` | 字符串 | `quote` | 打在屏幕上的引文；`text` 是 TTS 念的那句，两者可以不同 |
| `by` | 字符串 | `quote` | 出处/作者；缺省不画署名 |
| `stats[]` | `[{value, unit?, label?, note?}]` | `stat` | `value` 必填（数字或字符串，如 `">3x"`） |
| `series[]` | `[{label, value, unit?}]` | `chart` | 最多取 6 条 |
| `steps[]` | `[{label, note?}]` | `process` | 最多取 5 个节点 |
| `sides{}` | `{"left": {head?, items[]}, "right": {...}}` | `compare` | 每侧最多取 3 条 |

**写错 `type` 会被 filmcheck 报 ERROR**（枚举 `SCENE_TYPES`，唯一硬门禁：
`skills/video-studio/scripts/filmcheck.py:288`，schema 侧在 `schemas/film.schema.json:246`）；
`grammar` 自己遇到非法值时**静默忽略**（当作没写，继续按内容自动选）。
其余新字段写错键名会被 filmcheck 报 WARN（"是不是想写 X"），不会静默失效。

## 6. 片内编排（防"每屏同一个骨架"）

| 维度 | 规则 | 现状 |
|---|---|---|
| 版式变体 | `variant_of(type, chapter, k)` 派生 0/1/2，并强制与上一段同类型不同 | 写入 storyboard 的每段；**当前只有 hero / statement / list / stat 四个绘制器读取它**，其余六类构图唯一 |
| 装饰密度 | `decor_of`（grammar.py:349）三档 0.55 / 0.75 / 1.0，每 3 段回落到最低档 | 所有绘制器都读（经 `_decor_scale`）。**素净母题（默认）直接返回 0**：不画装饰层 |
| 相位偏移 | `phase_of(idx, chapter)` = 2π × sha1 派生值 | hero / statement / list / compare / process / quote / chapter / chart / outro 的持续动效都用它，避免同频"复印" |

变体号、密度档、相位都**不按时间/帧号/随机数**派生——同一稿子同一结果，
这是"可复现"的一部分。改 `chapter` 字段会改变 `variant_of` 的输入，进而改变变体号。

## 7. 没有 design 就没有画面（没有 legacy 路径）

不写 `style` 不会「走老绘制器」，而是**直接报错**，并给出两步指引（先提案、再确认）。
整条链路只认 `work/design.json`：

- `render.st_layout` 调 `grammar.plan_scenes`，按 `design.visual.layout_cycle` 做**按章节的版式轮换**，
  写 `work/storyboard.json`（每段的 type / variant / decor / layout / reason 都可审计）；
- `scenes.draw_frame` 只走 10 类构图绘制器（`_DRAWERS`），没有 legacy 分支；
- 字幕样式来自 `design.visual.subtitle`，字号/位置/背板/圆角由设计决定；
- 金帧基线 `assets/golden_frames.json` 绑的是固定 fixture 设计（`assets/design_fixture.json`），
  不是「某套内置风格」——改绘制代码后按 `tools/golden_frames.py --write` 重录。

没有 design 时的正确做法（脚本留在技能目录里，零拷贝）：

```bash
python <SKILL_DIR>/scripts/propose.py --brief "题材/要求" --script film.json --work work
python <SKILL_DIR>/scripts/propose.py --confirm --candidate A --work work
python <SKILL_DIR>/scripts/render.py --script film.json --work work --out out/film.mp4 --stage all
```

## 8. 加第 11 类构图（8 步）

以新增 `timeline`（时间轴图）为例。顺序不能乱，每步都有对应的门禁/断言在盯：

1. **`grammar.SCENE_TYPES`（grammar.py:30）末尾追加** `"timeline"`。**只追加不改顺序**——
   顺序是平手取位顺序，插队会让所有老稿子的分镜漂移。同时补 `SCENE_LABELS`。
2. **`grammar.MIN_DUR` 加最短时长**（按这套构图入场+读完所需的秒数，别照抄别的类型），
   并在 `DOWNGRADE` 里给它一个降级目标（键必须都在 `SCENE_TYPES` 里，且不能自环）。
3. **`grammar.score_types` 加打分规则**：优先"字段存在性"（比如 `timeline: []` 给 +6.0），
   其次才是关键词/结构位置。加上去之前先问：它和 chart / process 的区别是什么？
   如果画面上差别不大，就不要加——10 类已经覆盖了"数字/列表/对比/流程/引语/章节"这些基本形态。
4. **`assets/scenes.py` 加绘制器** `_draw_timeline(scene, t, dur, tg, script, frame, plan, th)`，
   签名与现有 10 个一致。约定：
   - 先用 `_plate`（背景板 + 母题烘焙）铺底，最后 `_apply_veil` + `_hud`；
   - 坐标过 `_view`（**恒为 1.0：前景不跟镜头**，只为统一接口），文字用 `_draw_text`（字号不重采样）；
   - 持续动效至少一个（相位用 `_phase(scene)`），否则 3–6 s 的静态画面会撞"画面静止"判据；
   - 重的东西预烘焙并合成进底版（参考 `_motif_layer` + `_plate`：一次烘焙，逐帧只 copy/EXTENT
     约 1.3–2.0 ms）。**不要逐帧全屏 RGBA paste**（实测 5.6 ms，直接撑爆单帧预算）；
     也不要每帧全屏 GaussianBlur / 全屏双线性 resize（预算 25 ms）。
5. **`assets/scenes.py` 的 `_DRAWERS` 加键**。缺了会在渲染时回落 list 并 warn 一次。
6. **`filmcheck.SCENE_TYPES` 加值**（这份只做成员校验，顺序无所谓），
   以及 **`schemas/film.schema.json` 的 `lines[].type` 枚举**加值（IDE 补全与文档）。
7. **`selfcheck.py` 第 54 条（`t_scene_types_single_truth`）会自动兜底**：它断言 grammar / filmcheck / scenes.`_DRAWERS` /
   schema enum 四处**集合相等**（顺序允许不同）。这四份真值来源必须同时改，漏一处立刻红。
8. **`tools/silhouette_qa.py` 的 `SCENE_CONTENT` 补一段合成内容**（给足该类型的原料字段），
   再跑一次剪影门禁（`--selftest` / `--designs N`）确认新类型在不同现场设计下也"看得出来"。这一步不做，等于新加的构图
   没有进入 S13 的覆盖范围。

跑完的验证顺序：`python selfcheck.py`（**105 条**，第 52–55 条专盯场景语法/视觉语言/单真值/剪影，第 71 条盯默认无细线（HUD 打开时也无线、遮幅不随 push 缩放），第 72 条盯前景不抖）
→ `python <SKILL_DIR>/tools/silhouette_qa.py`（不同设计两两仍可区分）。

## 9. 常见误用

| 现象 | 原因 | 修法 |
|---|---|---|
| 分镜表里一段都没变，全是 list | 没有 `work/design.json`，或 grammar 没跑 | 写 先 `propose.py --confirm` 冻结设计；再 `--stage layout` 看分镜表 |
| 某段想要数字卡，结果画成列表 | 没有 `stats[]`，文本里的数字是"2024 年"（年份被排除）或没有单位 | 显式写 `stats[]`；或让那句话带"数字 + 单位" |
| 写了 `type: "stats"` 却没生效，还报错 | `type` 是枚举，`stats` 才是字段名；写错值 filmcheck 报 ERROR | 改 `type` 为 10 类之一，数据放 `stats[]` |
| 对比段左右两栏一栏空 | 只有 1 条 `items`（对半分不出来），或 `sides{}` 只写了一侧 | 补到 ≥2 条，或显式写 `sides` 两侧 |
| 流程轴上的箭头/破折号来自条目 | 关键词只看叙述文字，不看 `items`——但如果你把"→"写进了 `text/big`，仍会命中 | 流程用 `steps[]` 或叙述里写"第一步…然后…"；条目里的排版符号不算顺序 |
| 章节卡把整段要点吃掉了 | 章节卡要求"无条目 + 标题 ≤14 字 + 段 ≤4.0 s"，但显式 `type=chapter` 会压过这些条件 | 章节卡单独成段（不带 items），要点另起一段 |
| 段长 2 秒写了 `type=quote`，出来是金句 | 段长门：quote 最短 4.0 s，按降级表落到 statement | 拆句/加停顿把段拉长，或接受降级；`storyboard.json` 的 reason 会写明 |
| 同一部片里前深后浅，闪烁门禁红了 | 同一部片仍锁一个明暗族（跨族会撞闪烁门禁） | 用同族主题；或拆成两部片 |
| 写了 5 条 `series`，画面只显示 3 条 | 容量上限：chart 只画前 6 条（stats 3 / steps 5 / sides 每侧 3） | filmcheck 会给 `scene_capacity` WARN；要全画就拆成多段 |
| 改了 `chapter` 后分镜变了 | `variant_of` / `chapter` 是变体与章节卡的输入 | 这是期望行为；要固定版式就写 `visual.layout` |

## 10. 已知限制（本版没做，写下来别装看不见）

- **web 引擎不跟随场景语法**：`--engine web` 的页面由用户自己写（`assets/web/film.js`），
  web 路线不经过 grammar。画风仍只有主题色，没有 10 类构图；
  剪影门禁也只覆盖 PIL 渲染器。
- **同一部片仍锁一个明暗族**：段界跨明暗族会整屏换底，精确触发闪烁门禁，观感也像换片。
  主题覆盖（`scene["theme"]`）只允许同族。
- **`lines[].type` 写错只有 filmcheck 拦**：grammar 对非法值静默回落（当没写）。
  不跑 filmcheck 的流水线，写错就只是"没生效"。
- **场景类型有 4 份真值来源**：`filmcheck.SCENE_TYPES`（`skills/video-studio/scripts/filmcheck.py:51`）/
  `grammar.SCENE_TYPES`（grammar.py:30）/
  `scenes._DRAWERS` / schema 的 enum。靠 selfcheck 第 54 条断言"集合相等"兜底；
  **顺序可以不同**——grammar 的那份顺序用于平手取位，另外三份只看成员。
- **版式变体只有 4 类绘制器读**：`variant_of` 对每段都算并写进 storyboard，
  但当前只有 hero / statement / list / stat 用它，其余六类构图唯一。
  这条不修的话，"每类型 3 个变体"只在四类上成立。
- **部分视觉语言字段"已声明、已体检、但绘制器没读"**：`motion.enter`（入场位移由主题的
  `entrance_dx/dy` 决定）、`typography.align` 由 `designkit.validate_design` 校验范围，
  但 `assets/scenes.py` 当前不读它们——写了不会报错，也不会改变画面。
  反例（都是有消费者的）：`typography.ratio` 经 `designkit._font_scale` 进主题字号盘；
  `motion.push` 进 `_push_scale`，但**只推背景板**——前景永远按整像素落位，
  因为慢速亚像素位移取整后就是 1px 抖动（见 [frame-qa.md](frame-qa.md) §11）。
  详见 [appearance.md](appearance.md) 的 §2 visual 字段表。
- **场景数据超容量会被截断**：`stats` >3 / `series` >6 / `steps` >5 / `sides` 每侧 >3
  的条目不会出现在画面上。filmcheck 报 `scene_capacity` WARN（容量表在
  `skills/video-studio/scripts/filmcheck.py:652`），但**不阻断渲染**——
  WARN 不是通过，也不是失败，是"画面上没有"。
- **`work/storyboard.json` 是审计产物，不是输入**：改它不会改变渲染结果；
  想要固定构图请改 `film.json` 的 `lines[].type`。