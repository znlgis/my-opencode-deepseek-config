---
name: video-studio
description: 做中文视频的两条生产线：(A) 素材剪辑 footage——有素材（本地文件/链接/网上下载）就按文案剪成片，自带素材检索、转写定位镜头、字幕水印治理；(B) 零素材——没有素材且找不到时用代码逐帧画。两条都走 edge-tts 实测时长排轴 + 采样级混音 + 自动质检。做视频/宣传片/纪录片/配音配字幕/把报告变成视频/修音画不同步/修画面糊，或提到 ffmpeg、字幕、分镜、混音时使用；开工先跑 intake.py 问完 8 个问题。 | Two pipelines for Chinese videos — footage-cut (source clips -> narration -> timeline) or code-generated frames; ffmpeg, edge-tts, subtitles, QA gates.
---

# 视频工作室（video-studio）

## 目录

- [§0 铁律](#0-铁律违反任意一条成片一定会出问题)
- [§0.1 分诊](#01-铁律先分诊有素材走-footage没素材才自己画)
- [§0.4 问诊](#04-铁律先问诊再提案每个新成片任务的第一步)
- [§0.5 提案](#05-铁律先提案再出片不论用户是否要求)
- [§1 上手](#1-五步上手零拷贝不拷脚本不拷模板)
- [§2 八阶段](#2-标准八阶段p0p7-前置-p05)
- [§3 引擎](#3-渲染引擎怎么选)
- [§4 画质](#4-画质清单这几项决定像不像专业片)
- [§5 声音与设计 DSL](#5-声音配乐--音效--提示点)
- [§6 停留与词级时间轴](#6-停留窗口让观众看清楚而不是整片放慢)
- [§7 质检门禁](#7-质检门禁error-必过--warning-别当看不见)
- [§7.5 交付生产线](#75-交付生产线画幅--批量--预览--运行版本化)
- [§8 故障速查](#8-常见故障速查)
- [§9 参考资料](#9-参考资料按需加载)
- [§10 脚本资产](#10-脚本资产)
- [§11 验收话术](#11-验收话术不要自我宣称做好了)
- [§12 参考机器](#12-参考机器的已验证环境事实)
- [§13 换机自检](#13-换机-60-秒自检比读文档快)

**一句话：视频 = 剧本 + 音频实测时长 + 画面（素材或代码生成）+ ffmpeg 编码 + 自动质检。**
两条生产线共用同一套底盘（TTS 实测时长、时间轴、采样级混音、loudnorm、门禁），
区别只在"画面从哪来"。

## 0.1 【铁律】先分诊：有素材走 footage，没素材才自己画

> 位置说明：本节排在 §0 之前是有意为之——**分诊永远排第一**，故放在 §0 之前；别为编号整齐挪下去。

> 用户说"做个视频"时，**先问自己手里有没有画面素材**，再决定走哪条线。
> 走错线的代价是整片返工（拿代码画的图文去顶"有真实影像"的需求，用户一眼就否）。

| 顺序 | 情形 | 走哪条 |
|---|---|---|
| 1 | 用户给了素材（视频 / 图片 / 链接 / 一个片子文件） | **footage 线**：`scripts/footage.py` |
| 2 | 没给素材，但**网上能找到**可用画面（纪录片、新闻片、公有领域影像） | **先 `--stage fetch` 下载，再走 footage 线** |
| 3 | 找不到，或题材不该用现成画面（数据、抽象、品牌、动画） | **零素材线**：`scripts/render.py` + `design.json` |

> **这张表现在由问诊第 2 题「制作方式」直接问用户**（自己画 / 网上素材剪辑 / 两者结合），
> 并按题材给推荐值（`--recommend`）：真实影像题材 → footage、数据/原理题材 → 自己画、
> 两边都占 → 两者结合。用户怎么选就怎么走；显式选择永远优先于推荐。
> 上表的"顺序"是**推荐逻辑**，不是"agent 自己猜"——别跳过这一问替用户定产线。

- **footage 线的契约是 `footage.json`**（不是 `film.json`）：见 [references/footage.md](references/footage.md)。
- **素材不必提前准备好**：`sources[]` 可以写 `url`，`--stage fetch` 负责下载；
  本地 `path` 更可复现。
- 两条线都遵守 §0 的其它铁律（音频先做、时间轴由实测驱动、采样级混音、交付前跑门禁）。
- 走了素材线，**提案层（`propose.py` / 设计 DSL）不适用**——外观由素材本身的调色与遮幅决定，
  不是"采样几个设计选一个"（`propose.py` 会在问诊记录 `mode=footage` 时明确提示这一点）。
- **"两者结合"有三种形态，先选对再动手**（详见 [references/hybrid-overlay.md](references/hybrid-overlay.md)）：
  1. **按段拼接**：素材段走 `footage.py`、图文段走 `render.py`，最后 `concat`（图文段占 30~90%）。
     **只有形态 1 才这么干，且必须显式选择**：`intake.py --hybrid-form 1`
  2. **整屏卡片覆盖**：基本不该用——素材被遮掉，等于没用素材
  3. **素材打底 + 小面积叠加**（`compose.py`，**技能默认形态**）：素材就是画面，
     只把"必要的一点信息"（一行对照、一个标注）按时间窗叠上去，自绘面积**设计目标 ≤15%**（`compose.py` 机器审计：warn 20% / fail 30%）
  **技能默认形态 = 3（代码默认同此）**：`intake.py` 的 `--hybrid-form {3,1}` 缺省为 3，形态落盘进
  `intake.json` 的 `hybridForm` 字段，`--finalize` 会打印「形态: 3」与 compose 命令。链路是
  `footage.py --stage all`（底片 / 混音 / 字幕）→ 写 `overlay.json` → `compose.py --stage all`
  （叠层 + 烧字幕 + 两遍 loudnorm）。**形态 3 不需要 `propose.py` / `matchstyle.py`**；形态 1 需显式 `--hybrid-form 1`，那时才要那两个。
  判断法：先想"去掉所有自绘，只留素材 + 旁白还成立吗？" 成立 → 形态 3；不成立且缺的是
  "对比/数据/流程"这类结构信息 → 形态 1。**别默认选 1**：实测 88% 时长画整屏卡片
  会被判"割裂"，改成形态 3 反而更统一、工作量更小。
- **别在素材上叠整帧主题色**：素材段保持原色（只有 crop/scale/遮幅 + 显式 `treatment.color`）。
  想让两段像一部片，方向反过来——素材不动，把**图文段的配色**锚到素材上：
  `python matchstyle.py --footage footage.json --work work --apply`（见 §0.1.1）。
  半透明色罩盖上去不会"统一"，只会让素材发灰、发闷、细节像蒙了雾。

### 0.1.1 hybrid 的一致性：素材不动，让图文段向素材看齐

> 一句话：**素材是基准，图文段是跟随者**。反过来（拿主题色去盖素材）就是糊画面。
>
> **适用范围**：图文段占相当比例（形态 1）时才需要。**技能默认形态是 3，形态 1 需显式 `--hybrid-form 1`**；
> 也就是说 `matchstyle.py` 只服务形态 1，默认链路里不该出现它。走**形态 3**（素材打底 + 小面积叠加）
> 时图文段只有 ~15%，套不套调色差别很小，**却可能把整套设计意图改掉**——所以
> `matchstyle.py` 现在会检查"设计原本亮底 vs 素材把配色锚到暗底"并给出警告
> （`day_night_conflict` 字段可机读）。出现警告时优先改形态 3，而不是硬套调色。

```bash
python matchstyle.py --footage footage.json --work work --report               # 先看画像（只写 work/style_profile.json 快照，不改 design/footage）
python matchstyle.py --footage footage.json --work work --apply --align-bars   # 落地
python propose.py --adopt --work work                                          # 调色板变了，重绑确认
```

| 它做什么 | 为什么这么做 |
|---|---|
| 按 EDL 抽帧（只抽成片里真会用的镜头），取**中位数**色相/饱和度/明度 | 平均值会被一块黑场或一片天空带跑；观众记住的是"典型颜色" |
| 明暗跟素材走（luma < 0.35 记暗底，与 `stylekit.is_dark` 同一判据） | 暗素材配浅底，观感就是"两部片拼一起" |
| 色相全收，饱和度**打折**（默认 ×0.6，`--sat-damp` 可调） | 素材里一块高饱和招牌铺满整屏会变荧光板 |
| 配色交给 `designkit._solve_palette`，写盘前过 `theme_lint` | 对比度门槛只有一份判据；写进去再发现，就是"渲完才看见字幕看不清" |
| `letterbox_color` 改成**同族近黑** | 自绘段黑边是纯黑；素材黑边若是米色，拼接处会直接跳 |
| `--align-bars` 把素材可见高算到自绘段画幅 | 40/44px vs 居中黑边，差 3px 以上就看得出画幅跳 |
| `--align-bars` 会**同时收缩 crop** | pad 是纵向居中的：只改 scale 时，宽度不够的素材右边会冒出黑带（实测 640x360 素材对齐 996 高时右侧 150px）。取景窗宽高比必须等于目标画幅；代价是多裁几个百分点，报告会写清楚 |

- 只改两块：`design.json` 的 `visual.palette` 与 `footage.json` 的 `treatment`（颜色 + 可选 scale）。
  **不碰** `treatment.color`——那是素材自己的显式调色，不是主题色。
- 失败一律零写入：配色无解 / 缺 `design.json` → 退 2 并打印"该补什么"；只写 `work/style_profile.json`。
- 覆盖前留备份：`work/backup/*.before-matchstyle.json`（回滚靠它，不靠记忆）。
- 算出来的颜色照样要**亲眼看**：`render.py --stage stills`，铁律 6 不因为"颜色是算出来的"就免掉。

## 0. 铁律（违反任意一条，成片一定会出问题）

1. **音频先做，画面后做**。时间轴必须由 TTS 实测时长反推，**绝不按字数估算**。按字数排的时间轴会在第 20 句开始明显漂移。
2. **每帧是纯函数**：`draw(t, frame) -> Image`。禁 `random`/`time()`，要随机用 `filmkit.hash2/hash3/vnoise`。同一帧号必须永远画出同一张图。
3. **字幕与大字必须落在安全区**：正文 `y ∈ [58, 974]`，字幕带 `y ∈ [872, 1030]`，永不超过 1030。
4. **原始帧直通编码**：PIL 帧 → `rawvideo` 管道 → `libx264`。**不要**用 `cv2.VideoWriter` 做中间文件（慢 2 倍且二次有损）。
5. **混音用 numpy 采样级叠加**，不要用一条 ffmpeg 命令喂上百个输入（Windows 上会非确定性静音）。
6. **交付前必须跑质检脚本 + 亲眼看 3 张静帧**（用 `read`）。"能出图"不等于"没问题"。
7. **声音跟着画面走**：该响一下的地方在**场景代码里登记提示点**（`K.CueBus` / `scenes.py` 的 `cues()` 钩子），
   不要事后手工对时间。画面改了（换停留窗口、调节奏），提示点自动跟着改。
8. **没看清就加停留，别整片放慢**：`film.json` 的 `hold`（全局）或 `lines[].hold`（单句）
   在语音结束后加停留；配乐用 `bpm` 对齐小节。

## 0.4 【铁律】先问诊，再提案（每个新成片任务的第一步）

> **先把只有用户能决定的事一次问完，再去出方案。** 这条是为了消掉"有时一个问题不问就一路跑完、
> 有时只问一两个"的随机性：`propose.py` 只负责"外观由谁来拍板"，而题材/时长/气质/声线/配乐
> 这些**输入**不在它里面；输入由 agent 当场猜，用户就得到成片才第一次看到自己的片子长什么样，
> 返工全在渲染之后。

**固定动作（排在 §0.5 提案之前）**：

```bash
# 1) 生成问诊单：固定 8 题（题材 / 制作方式 / 时长 / 主题气质 / 画幅 / 旁白声线 / 配乐 / 补充）
#    带 --topic 时，「制作方式」题会附 recommended / recommend_reason（按题材算好）
python <SKILL_DIR>/scripts/intake.py --work work --questions --json --topic "AI 大模型的三年"
#    想先单独看推荐也可以（不给文本就用问诊单里已写的题材）：
python <SKILL_DIR>/scripts/intake.py --recommend "中国高铁十年，纪录片"

# 2) 把 questions 里的每一题用 question **一次问完**（不要一句一轮），再写回：
python <SKILL_DIR>/scripts/intake.py --work work \
       --set "topic:AI 大模型的三年，讲给普通观众" --set mode:auto --set duration:45s \
       --set tone:bright-lively --set voice:female-bright --set music:upbeat \
       --set "note:不要放射线，结尾放二维码"

# 3) 定稿：校验必答题、合成 --brief 文本（work/intake.brief.txt），并按制作方式打印下一步
python <SKILL_DIR>/scripts/intake.py --work work --finalize
```

| 规则 | 说明 |
|---|---|
| **一次问完** | 8 题在同一次 question 里给出，每题都带推荐项；用户只需勾选/一句话补充 |
| **制作方式先定产线** | 第 2 题问「自己画 / 网上素材剪辑 / 两者结合」：自己画走 `render.py`、素材剪辑走 `footage.py`、两者结合默认形态 3（`footage.py` + `compose.py`），按段拼接须显式 `--hybrid-form 1`。选错产线整片返工，所以它是必答题；`auto` 档按题材推荐落地（真实影像→footage、数据/原理→draw、两边都占→hybrid），显式选择优先 |
| **推荐要有理由** | `--recommend` / 问诊单的 `recommend_reason` 会列出命中的题材词；把推荐说给用户听，但用户不同意就按用户的来——不看理由直接默认等于替用户拍板 |
| **明亮活泼是默认** | 任何题材（科普、纪录片、宣传片、动画短片、复盘汇报）的气质题推荐项都是「明亮活泼」，且它排在第一位 |
| **首个候选必亮** | 没点名气质时，提案的候选 A 固定落到明亮活泼档；其余候选保留完整空间（含暗色）——"优先明亮"不等于"只有一种口味" |
| **没答完不许出方案** | 首次 `propose.py` 会因问诊未完成而失败，并打印"怎么问 / 怎么答 / 怎么定稿"三步 |
| **逃生口留痕** | 确实不需要问（批量补渲、已在别处答过）用 `propose.py --no-intake`，提案里会记 `intake.status = bypassed`，事后查得到 |
| **回答进 brief** | 答案合成 `intake.brief.txt`，再喂给 `--brief`：气质→tone、声线→voice、时长→duration_target，`不要 X` 进 avoid。**制作方式不进 brief**：它是产线路由，写进去只会污染 `propose._brief_of` 的关键词解析 |

有用户直接说"你替我定"时：**仍然先问诊**（用推荐项作为各题默认，只让用户确认一次），
再出提案；不要因为"用户没意见"就把 8 个决策全部改成程序默认值。

## 0.5 【铁律】先提案，再出片（不论用户是否要求）

> **用户没要求，也必须先给方案。** 这是本技能最重要的一条产品行为，不是建议。
> 理由：外观一旦由程序替用户选，用户就从头到尾没参与决策，结果必然是"每个视频都差不多"。
> 所以流程固定为：**现场采样出几个方案 → 用户挑一个 → 冻结成设计 → 再出片**。

**固定动作（问诊答完后的第一件事）**：

```bash
python propose.py --brief "<题材/受众/时长/语气>" --script film.json --n 3
```

它会产出 **N 个候选方案**（每个 = 一份设计 + 4 张静帧 + 成本 + 与其它候选的设计距离），
落盘 `work/proposal.json`，并打印一句"确认其中一个 / 都不满意就 `--revise`"。**全程离线、秒级。**

| 用户反应 | 你要做的 |
|---|---|
| "就这个" / 选了某个 | `python propose.py --confirm --candidate B`，然后才允许渲染 |
| "太冷了/太花哨/换个声音/节奏快点" | `python propose.py --revise "<原话>"` —— 新 rev，旧 rev 保留可 diff |
| "都不行，重来" | 同上，把要求写具体（`--revise` 的文本会存进 `proposal.feedback`） |
| 明确说"你定就行" | **仍然先出方案**，挑一个你推荐的并说明理由，让用户点头 |

**硬约束**：

1. `propose.py --confirm` 之前，`render.py` 的 `frames`/`all` 会被提案闸拦下（默认警告，`--require-proposal` 失败）。
2. 候选必须**两两在 ≥2 个设计维度上不同**（配色族/母题/转场/缓动/编号/配乐/声线）。只换颜色不算多方案，`diversity.vs[].n < 2` 会被标出来。
3. 每个候选必须有一句中文**立意**（`pitch`），说清"这个方案想干成什么样"——不是"方案 A/方案 B"。
4. **没有内置模板可套**：`design.json` 的所有字段都开放（35 字段主题 + 五组视觉语言签名 +
   声音/配乐/节奏），配色是**连续色相空间**里的任意一点，不是从若干套现成外观里挑。
   每次提案都是现场生成——同一个 brief 连拍两次会得到不同设计（要复现就传 `--seed`）。
5. **首个候选默认明亮活泼**：没点名气质时候选 A 走浅底带色（"明色"标签），
   其余候选在完整空间里取（含暗色）。任何题材的气质题推荐项都是「明亮活泼」。
6. **片间去重台账（默认开启）**：`--confirm` 会把冻结的设计记进
   `~/.config/opencode/video-studio/design-ledger.json`（最多 500 条），下次提案主动避开最近交付过的外观——
   候选与台账里任一部片的**设计距离 < 2 维**时自动换 nonce 重采并告警。
   这是"不同片不重复"从"靠随机"变成"有记忆"的落地机制。`--show-ledger` 查台账、
   `--no-ledger` 本次忽略、环境变量 `VIDEO_STUDIO_LEDGER` 改路径或设 `off` 关闭。
   台账读写失败一律当空表，**永不阻塞出片**。

**只有一种情况可以跳过提案**：纯修复 / 纯重渲（不改内容、不改设计，只修音画不同步或重出片）。
这种情况下用 `--require-proposal` 之外的方式跑，并在回复里说明"本次未提案，因为…"。

### 0.5.1 外观从哪来：只有两条路

**没有"内置风格"可选。** `film.json` 里写任何 `style` 值（`auto` / `legacy` / 某个 id）都会
被明确拒绝并给两步指引——剧本不决定长相，长相由 `work/design.json` 决定。
同理，技能里没有 `THEMES`/`STYLES`/`VISUAL_LANGUAGES` 表，也没有 `presets/`；
配乐由 `musicgen.py` 按参数现场编排，`scenes.draw_frame` 也没有 legacy 分支。

要特定外观只有两条路：

```bash
# ① 自然语言提要求（认得 must_have/avoid 里的关键词）
python propose.py --brief "科技发布会，深蓝，要遮幅" --script film.json

# ② 直接手写 design.json（字段全开放，配色是任意 RGB）
python render.py --script film.json --stage all     # 前提：work/design.json 已存在
```

详细契约见 [references/proposal.md](references/proposal.md) 与 `schemas/design.schema.json`。

---

## 1. 五步上手（零拷贝：不拷脚本，不拷模板）

> **这条路径没有任何"拷贝"动作。** 技能脚本留在技能目录里直接执行；
> 你的工程目录里只需要一个 `film.json`（外加运行产生的 `work/` 与 `out/`）。
>
> `<SKILL_DIR>` 指**本技能所在目录**：OpenCode 从 `~/.config/opencode/skills/` 加载技能，所以它是
> `$env:USERPROFILE\.config\opencode\skills\video-studio`（本仓库的 `opencode/skills/video-studio`
> 与它逐字节一致；改仓库后先跑 `scripts/sync-config.ps1`）。
> 找不到时按名字定位：`Get-ChildItem -Recurse -Filter filmkit.py $env:USERPROFILE\.config\opencode`。
>
> 收编时把上游包根的 `tools/` 与 `schemas/` 搬进了技能目录，所以 `<SKILL_DIR>/tools/` 与
> `<SKILL_DIR>/schemas/` 都是有效路径；上游原本把它们写在包根下（`<包根>/tools/`、`<包根>/schemas/`）。

```bash
# 1) 只写一个 film.json（工程目录里唯一需要你手写的文件）
mkdir -p myfilm && cd myfilm
cat > film.json <<'JSON'
{
  "title": "我的片子",
  "lines": [
    {"id": "l01", "kind": "hook",  "chapter": "01 · 开场",
     "text": "这是用代码从零生成的画面与声音。", "sub": "零素材，全代码生成", "big": "零素材，全代码生成"},
    {"id": "l02", "kind": "body",  "chapter": "02 · 正文", "kicker": "PIPELINE",
     "text": "整条管线只有五步：写剧本、生成旁白、排时间轴、逐帧渲染、混音合成。",
     "sub": "五步成片，每步落盘", "big": "五步成片",
     "items": [{"text": "剧本 film.json"}, {"text": "旁白"}, {"text": "时间轴"}, {"text": "逐帧渲染"}, {"text": "混音"}]},
    {"id": "l03", "kind": "outro", "chapter": "99 · 收尾",
     "text": "先定脚本，再定声音，最后才是画面。", "sub": "顺序比努力重要", "big": "顺序比努力重要"}
  ]
}
JSON
#    lines[].text = 朗读文本（自然口语）；sub = 上屏字幕（短）；kind = hook/body/quote/outro/emphasis/calm
#    画风/配乐/声线/节奏全部由 design 决定，film.json 里**没有** style 字段可写。
#    契约校验（可选，几秒）：
python <SKILL_DIR>/scripts/filmcheck.py film.json

# 2) 先自检一次环境（可选；105 条回归护栏，约 10–25 秒，不联网；缺可选模块会标 SKIP）
python <SKILL_DIR>/scripts/selfcheck.py

# 3) 【铁律】先问诊（8 题一次问完），再出方案——同一个 brief 每次都不一样，必须让用户看过
python <SKILL_DIR>/scripts/propose.py \
       --brief "AI 大模型的三年，30 秒科普" \
       --script film.json --work work
#    问诊没答完时这条会被拦（加 --no-intake 可跳过，会记进 proposal.intake 留痕）；
#    不传 --brief 时 propose 直接读 work/intake.brief.txt。
#    产出 3 个候选：各自一份 design + 静帧联系表（work/proposals/rev1/_compare.png）+ 设计距离，
#    2 秒级出结果、全程离线。看并排图，挑一个。

# 4) 确认候选，冻结设计（work/design.json）——此后渲染只认这份设计
python <SKILL_DIR>/scripts/propose.py --confirm --candidate A --work work
#    不满意就换一个：--revise "再冷一点，少装饰，换女声"（新 rev，旧 rev 留着眼 diff）
#    挑的时候可以顺手看一眼客观分：python <SKILL_DIR>/scripts/critic.py --work work
#    （离线启发式 0-100，只用于排序候选；分差 >15 通常说明该换一个，它不阻断交付）
#    想复现某一次：首次提案时加 --seed 42（不传 seed = 每次新 nonce，这是"每片不同"的来源）

# 5) 先看静帧，再出全片；--scenes 不写就用技能内置渲染器
python <SKILL_DIR>/scripts/render.py --script film.json --work work --stage stills \
       --stills "l01:3,l02:5,l03:2.5"
#    → work/stills/_sheet.png：用 read 亲眼看，别跳过

python <SKILL_DIR>/scripts/render.py --script film.json --work work \
       --out out/film.mp4 --stage all
#    TTS→排版→配乐→音效→混音→逐帧→编码→质检 一条龙；中间产物全在 work/，可断点续跑。
```

**关于 `--scenes`（零拷贝的关键）**：`render.py` 不写 `--scenes` 时的定位顺序是
**① 剧本同目录的 `scenes.py` → ② 技能内置 `assets/scenes.py`**。所以默认情况下你什么都不用写、
也不用拷，内置渲染器直接出片；**只有想改画面**时才在剧本同目录放一个自己的 `scenes.py`
（实现下面的 `draw_frame` 与可选的 `cues()`），它会被自动发现——也可以显式 `--scenes my_scenes.py`。
内置渲染器源码就是最好的范例：`<SKILL_DIR>/assets/scenes.py`。

```python
# 真实签名（六个参数，少一个都会崩）：`scenes.draw_frame`
def draw_frame(scene, t_local, dur, t_global, script, frame):
    # scene    : 这一段在 timeline 里的字典（id / kind / scene_type / sub / big / items / stats ...）
    # t_local  : 段内时间（秒，0..dur）——入场/持续动效都按它在段内推进
    # dur      : 这一段的总时长（秒）
    # t_global : 全片时间（秒）——跨段的连续动效（镜头缓推、呼吸）用它
    # script   : 整份剧本/计划字典（plan、主题色、words 等；有缓存，别原地改）
    # frame    : 绝对帧号（整数）——需要确定性噪声/相位时用它，别用 time()
    return img   # PIL.Image，必须 1920x1080 RGB（用 K.new_frame() 起手最稳）
```

> 写自定义 `scenes.py` 时先抄 `<SKILL_DIR>/assets/scenes.py` 的一个绘制器（`_draw_hero` 之类），
> 它已经把纯函数、缓存排版、预烘焙底版这些坑都绕过一遍了。

**为什么先提案**：没有"内置风格"可选，外观由 `design.json` 决定。
同一个 brief 连拍两次会得到不同设计（配色/母题/转场/配乐/声线都可能不同），
所以流程必须是"生成方案 → 人确认 → 冻结 → 出片"；不确认就渲染，`render.py` 会打提案警告
（加 `--require-proposal` 则直接失败）。设计契约见 [references/proposal.md](references/proposal.md)。

**分阶段跑**（每个 stage 只做自己的事，落盘后可单独重跑）：

```bash
python <SKILL_DIR>/scripts/render.py --script film.json --work work --stage style   # 解析已确认的 design，打印主题/视觉语言/版式轮换/字幕样式
python <SKILL_DIR>/scripts/render.py --script film.json --work work --stage tts      # 合成旁白 + 实测时长（带输入校验的缓存），并顺带排一次轴
python <SKILL_DIR>/scripts/render.py --script film.json --work work --stage layout   # 只排时间轴 + 选构图，落 timeline.json / sub.srt / storyboard.json
python <SKILL_DIR>/scripts/render.py --script film.json --work work --stage cues     # 只出提示点 -> work/cues.json
python <SKILL_DIR>/scripts/render.py --script film.json --work work --stage score    # 只出配乐 -> work/music.wav（musicgen 现场编排）
python <SKILL_DIR>/scripts/render.py --script film.json --work work --stage sfx      # 只出音效轨（sfxkit，按 cues.json）
python <SKILL_DIR>/scripts/render.py --script film.json --work work --stage audio    # 只混音（旁白+配乐+音效，旁白优先）
python <SKILL_DIR>/scripts/render.py --script film.json --work work --stage frames   # 只渲染帧（--engine pil|web，--jobs N 分块并行）
python <SKILL_DIR>/scripts/render.py --script film.json --work work --stage mux      # 只合成（视频流 copy，两遍 loudnorm）
python <SKILL_DIR>/scripts/render.py --script film.json --work work --stage qa       # 只质检（只读输入，写 work/qa/；另按 §7.0 写 out/QC-REPORT.md 与 work/gates.json）
python <SKILL_DIR>/scripts/render.py --script film.json --work work --stage stills   # 只渲母版静帧 + 联系表
python <SKILL_DIR>/scripts/render.py --script film.json --work work --stage accept --gate stills   # 人工签核一道闸（写 work/signoff.json）
python <SKILL_DIR>/scripts/render.py --script film.json --work work --stage explain  # 只报"哪些阶段过时、为什么、该跑什么"，不跑任何 stage
```

`--stage all` 的完整顺序：`style → tts → layout → cues → score → sfx → audio → frames → mux → qa`。
**任何一步出问题都会在该步停下**，不会带着坏输入继续往下跑。

**开渲前先算成本**（可选；`estimate.py` 是技能自带工具，按 `<SKILL_DIR>` 调用）：

```bash
python <SKILL_DIR>/tools/estimate.py --script film.json --jobs 4
```

它给帧数/各阶段耗时/磁盘/分块建议（`--json` 可机读），是**区间上界**不是承诺；
有 `work/timeline.json` 时用真实时长，否则按字数粗估。要省掉全片 VMAF 就在 `film.json` 写
`"qa": {"vmaf": {"mode": "off"}}`（estimate 与 render 都读这个字段，G11 记 skipped）。

**看交付凭据再签核**：

```bash
cat out/QC-REPORT.md     # gate matrix 分 pass / fail / skipped 三档；skipped = 本次没执行，不等于通过
python <SKILL_DIR>/scripts/render.py --script film.json --work work --stage accept --gate release
```

签核绑内容指纹：改了剧本 / `scenes.py` / 设计，旧签核自动失效，要重新看、重新签。
交付前想强制（skipped 也算失败）：加 `--strict-gates`。

**长片渲染**（分块并行 + 断点续跑）：

```bash
python <SKILL_DIR>/scripts/render.py --script film.json --work work --stage frames --jobs 4
python <SKILL_DIR>/scripts/render.py --script film.json --work work --stage frames --jobs 4 --resume
```

> 分块落在 `work/segments/`，按 `seg_<起始帧>_<结束帧>.mp4` 命名；拼接前会**显式检测缺口**，
> 缺块直接报错，不允许"少一段也能出片"。默认 chunk 按 `总帧数 / jobs / 4` 向整秒取整（jobs=4、
> 900 帧时约 8 秒一段），可用 `film.json` 的 `chunk_frames` 覆盖。
>
> **改台词 / 改 scenes.py / 改设计 / 改编码参数之后，`--resume` 会拒绝复用旧分块**（`work/segments/_plan.json`
> 里存着计划指纹），并提示你 `rm -rf work/segments`——这是刻意的：旧分块配新时间轴会拼出
> "画面和旁白对不上"的片子，而且时长门禁查不出来。

## 2. 标准八阶段（P0–P7）+ 前置 P0.5

> 表里共 9 行：**P0.5 定设计**是前置条件（没有 design 就没有外观），不是第九阶段。
> 它排在最前面是因为"先定外观再干活"——跳过它，后面每一步都在猜长相。

| 阶段 | 产出 | 关键动作 | 常见错误 |
|---|---|---|---|
| **P0.5 定设计** | `work/design.json` + `work/style.json` | `propose.py` 出方案 → `--confirm` 冻结设计；`--stage style` 只解析已确认的设计并打印主题/视觉语言/版式轮换/字幕 | 跳过 → `render.py` 报错（没有 design 就没有外观）；不确认就渲染会打提案警告 |
| **P0 定基准** | `CONVENTIONS.md` | 分辨率/帧率/时长/BPM→帧换算一次算死，全项目引用同一份 | 各文件自己写 `150` 这种魔数 → 幕边界对不齐 |
| **P1 写剧本** | `film.json` | 每句拆 `text`（朗读）与 `sub`（字幕）；每段一个 `kind`；可选 `lines[].type` 锁定构图 | 一个字段两头用 → 字幕太长或读起来生硬；**别写 `style`**（不再影响画面，外观由 `work/design.json` 决定） |
| **P2 事实核查** | `FACTS.md` | 上屏的每个数字/年份有出处编号；建"不能上屏"黑名单 | 跳过这步 → 画面和旁白数字打架，评论区第一条就是你错了 |
| **P3 TTS** | `work/tts/*.mp3` + `tts.json` | edge-tts 逐句合成，`ffmpeg -f f32le` 实测时长，磁盘缓存 | 按字数估时长 → 字幕漂移 |
| **P4 排时间轴** | `work/timeline.json` + `sub.srt` | `dur = lead + 实测语音 + tail`，句读必须在本段内 | 段太短 → 旁白被下一段截断 |
| **P5 混音** | `work/audio.wav` | numpy 总线：旁白 + 配乐 + 音效按采样点叠加 | ffmpeg 多输入混流 → 非确定性静音 |
| **P6 母版** | 3 张静帧 | 先渲 3 帧看构图/配色/字重，定了再渲全片 | 直接渲全片 → 36000 帧后发现字体丑 |
| **P7 成片** | `out/film.mp4` | rawvideo→libx264 CRF18 + 两遍 loudnorm 到 -16 LUFS | 码率过低/响度 -25 LUFS → "像草稿" |

> 注：`filmkit` / `render.py` 的默认是 `crf=18`；
> `assets/film.json` 只是**测试夹具**（跑通全链与金帧/门禁回归用，不是给你抄的模板），它写的是 17。
> 自己写剧本时不写 `crf` 就是 18；追求更锐利的文字可以写 17（体积约 +30%）。

## 3. 渲染引擎怎么选

| 场景 | 选型 | 实测速度 | 适合 |
|---|---|---|---|
| 图文/数据/科普/UI 风 | **PIL（numpy+cv2 辅助）** — `filmkit` 默认 | 40–80 fps @1080p30（示例片 75） | 绝大多数中文科普片、报告转视频 |
| 需要流畅矢量/粒子/3D/纸纹质感 | **无头浏览器 Canvas2D/WebGL + CDP 截图 → image2pipe**（`webengine.py`） | 80–130 ms/帧（核显，PNG 截图） | 纪录片级长片、需要复杂视觉语言 |
| 纯数据动画 | **manim**（若已安装） | 视场景而定 | 数学/公式推导 |

> 上表速度是**单机空载**实测口径（换机、重负载画面都会变；详见 §12）。别把区间当保证。

> **设计只覆盖 PIL 引擎**（`engine: pil`，默认）：`work/design.json` 的配色/字体/版式/母题
> 由 `assets/scenes.py` 读取。`--engine web` 的页面由**你自己**写（`assets/web/` 只是能跑通的示例页，
> 不是出片模板），画面不跟随设计；但配乐与旁白**照样**跟随 `design.json`（它们不依赖渲染引擎）。

**默认走 PIL**：依赖少（numpy+Pillow 就够）、启动快、确定性强、调试成本低。
需要"一镜到底的粒子形变 / 实时着色器质感"才上浏览器方案：

```bash
cp -r <SKILL_DIR>/assets/web ./web                     # 页面模板（原生 Canvas2D，零三方库）
pip install playwright && python -m playwright install chromium   # 只在走 web 引擎时需要
python render.py ... --engine web --stage cues        # 从页面导出提示点
python render.py ... --engine web --stage frames --jobs 3 --resume
python webengine.py textqa ./web/index.html            # 文字重叠/出画/一闪而过/从没露面
```

两条路线的取舍（**先想清楚再选，换引擎要重做画面**）：

| | PIL（`engine: pil`） | 浏览器（`engine: web`） |
|---|---|---|
| 依赖 | numpy + Pillow（+cv2 做锐度比） | + playwright + Chromium/Edge |
| 速度 | 40–80 帧/秒 | 8–12 帧/秒（逐帧截图+编码，慢一个数量级） |
| 画面语言 | 位图合成、渐变、几何、文字 | Canvas2D/WebGL：粒子、形变、着色器、矢量动画 |
| 确定性 | 天然（纯函数画图） | 靠"把时间轴精确 seek 到 f/FPS"保证（`window.renderFrame`） |
| 调试 | 改一帧跑一帧 | 浏览器里 `index.html?dev=1&t=秒` 直接看，出了 bug 好查 |
| 适合 | 图文/数据/科普/宣传片 | 纪录片级长片、需要一镜到底的视觉语言 |

**浏览器引擎的三条硬约束**（`webengine.py` 已实现，自己写页面时也要守）：
1. 所有补间放在一条**暂停的**主时间轴上，渲染第 f 帧 = 把它 seek 到 `f / FPS` 秒；**不许依赖播放**。
2. 循环/粒子/波浪这类程序化内容用"按时间直算"的更新器（`update(fn)`），不许用 `setInterval` 累加。
3. 页面必须暴露 `window.ready / DURATION / renderFrame(f) / getCues()`；**不连 CDN、不请求网络**。

要**跨浏览器会话**的字节级可复现（崩溃重开、多次渲染做 diff、质检要比对两张图）时开确定性模式：
`--deterministic` 或环境变量 `WEBENGINE_DETERMINISTIC=1`（关 GPU 光栅，实测 8/8 会话一致，
代价约 +22%/帧）。默认不开：同一会话内本来就一致，跨会话只有 ≤4 像素、±1 级的抗锯齿差异。

## 4. 画质清单（这几项决定"像不像专业片"）

- **亮度与层次**：暗底 (`#0a1420`) + 顶部亮 accent；不要纯黑纯白，用户屏幕会吃掉渐变。
- **字重与字号**：大字 84–96px、副标题 34–38px、字幕 46px；中文用 `msyhbd.ttc`（微软雅黑粗体）。
- **字幕**：≤ 2 行、单行约 24 个中文字、带半透明背板、`y ≥ 872`。
  实际折行看的是**像素宽度**（`fit_sub_lines` 按 `max_w=1560 px` 自动折），24 字是"大致上限"并非硬校验
  ——`SUB_MAX_CHARS=24` 这个常量目前**没有被任何代码引用**，别指望它拦你。
- **节奏**：每 3–6 秒要有视觉变化（新卡片 / 位移 / 数字滚动）；静止超过 6 秒观众就走。
- **镜头**：只做 3–15% 缓推，不要大幅拉远拉近。
- **调色**：整片一个主色 + 一个强调色，别超过 4 个色相。
- **音频**：母带 -16 LUFS / 真峰 ≤ -1 dBTP；旁白比间隙响 12 dB 以上；AAC 前留 1.4 dB 余量。

**素材线 / hybrid 底片还要过 F8 与素材预算**（F1–F9 查素材治理与成片对齐，F8 量"画面够不够"；细则见 [references/footage.md](references/footage.md)）：

- **素材预算**（`footagekit.source_budget`，按片长反算独立来源数）：
  `recommended = max(3, ceil(分钟数 × 3))`（推荐 **3 个独立来源/分钟**）、
  `minimum = min(recommended, max(3, ceil(分钟数 × 2)))`（硬下限 **2 个/分钟**），
  两条线都再夹一个**绝对下限 3 个**。低于硬下限时 `--stage edl` 就直接 `SystemExit`，不等切完镜。
- **F8 素材多样性**（`footageqa.f8_source_variety`，已注册进 `GATES`，素材线门禁集是 F1–F9）：
  `distinct < 硬下限` → **fail**；`< 推荐` → **pass + warning**；单源连续 ≥3 镜、重复取景窗 >0 → warning。
  `footage.json` 写 `"licensing": {"allow_sparse_sources": true, "why": "..."}` 才记 **skipped**。
- 逃生口：`footage.py --allow-sparse-sources`（低于硬下限也继续，F8 仍记 fail 的旁路说明）、
  `--allow-missing-sources`（有源下载失败也继续；默认 fail-closed）。

## 5. 声音：配乐 + 音效 + 提示点

> 这一节的思路来自一部真实成片（`ai-history-film`）的复盘：**声音不好不是"混得不够响"，而是"声音不认识画面"**。
> 解决顺序永远是：先让声音知道画面上发生了什么（提示点），再谈音色，最后才是母带。

### 5.1 三层声音

| 层 | 谁做 | 落盘 | 说明 |
|---|---|---|---|
| 旁白 | `filmkit.tts`（edge-tts） | `work/tts/*.mp3` | **唯一的时长权威**：时间轴由它反推 |
| 配乐 | `musicgen.py`（参数化编排） | `work/music.wav` | 按设计里的 bpm/key/scale/mood/seed + 编排参数现场生成；**没有现成曲风可挑** |
| 音效 | `sfxkit.py`（物理建模） | `work/sfx.wav` | 按 `cues.json` 里登记的提示点逐条合成 |

三条总线在 `--stage audio` 里合成：**音效响起时配乐自动让位**（`duck_db` 默认 4 dB，
cinema/epic 5.5 dB——3 dB 的让位量在段落过门时配乐几乎与音效等高，听感会糊成一团），
最后交给 ffmpeg 两遍 loudnorm 到 -16 LUFS / 真峰 ≤ -1 dBTP。**不要**自己在 numpy 里做最终响度标准化——
你有 `loudnorm`，它比手写归一化准得多，而且真峰才是门禁。

> **片内分离**：配乐在段落交界本来就有过门（编排器写的 swell / riser / sub_boom），
> sfx 轨不在同一个边界重复放 whoosh/riser——`--stage sfx` 会对落在
> [边界 - 2.2 s, 边界] 内的转场类 cue 自动 -3 dB（`sfx.duck_transitions=false` 可关）。
> 提示音的音高跟随配乐调性（`in_key(freq, key)`），不硬编码某个调的五声音阶。

### 5.2 提示点：让声音跟着画面走

**这是整套声音设计的地基。** 画面在该响一下的地方登记一个提示点，音频阶段按提示点合成音效：

```python
# 方式一：写在 film.json 里（好改，适合少量关键音效）
"sfx": {"cues": [
  {"at": "l03+0.05", "type": "impact", "size": 1.1},
  {"at": "abs:12.5", "type": "whoosh", "dur": 0.9}
]}

# 方式二：写在 scenes.py 的 cues() 钩子里（推荐——画面改了声音自动跟着改）
def cues(tl, script):
    out = []
    for s in tl["scenes"]:
        for i, _it in enumerate(s.get("items") or []):
            out.append({"at": "%s+%.2f" % (s["id"], 0.48 + 0.32 * i), "type": "ui_blip"})
    return out

# 方式三：web 引擎里由页面导出（提示点跟着动画代码走）
S.cue(t, 'impact', {size: 1.1});   // 页面里的时刻是"本幕局部秒"
python render.py ... --engine web --stage cues   # 自动导出 work/cues.json
```

`at` 有三种写法：`"abs:12.5"`（绝对秒）、`12.5`（数字同义）、`"l03+0.35"` / `"l03-0.2"`（段 id ± 偏移）。

**为什么值得多写这一层**：改一句台词、加一个停留窗口、把 BPM 从 120 改成 100，
所有音效仍然贴着画面，不需要重新对时间。纯手工"在 12.5 秒放一声"的方案，改一次节奏就全废。

### 5.3 配乐（musicgen 编排；scorekit 只是合成原语）

> 配乐的"身份"（调式/调性/BPM/情绪）由 `design.json` 的 `audio.music` 决定（§5.5），
> 下面这些都是**可选**的底层覆盖；不写就听设计的。

`film.json` 里仍可手调这些底层参数（`bpm: "auto"` 表示按 design 的 BPM 走）：

```json
"bpm": "auto",
"music": {"enabled": true, "mode": "score", "mood": "auto",
          "key": "auto", "seed": 7, "seed_from_content": true,
          "gain_db": -12, "duck_db": 4.0,
          "sections": [{"at": "l01", "name": "intro", "energy": 0.3},
                       {"at": "l03", "name": "drive", "energy": 0.8}]}
```

> 没有"配乐风格表"这回事：`music.style` 写了也不参与渲染。
> 配乐的调式/调性/BPM/情绪/seed 由 `design.json` 的 `audio.music` 决定，`musicgen` 现场编排。

- **一张和弦表贯穿全片**，段落之间只换"色彩与能量"，不换调——这是"听起来像一首曲子"而不是"一堆配乐"的关键。
- `energy` 控制律动层数（pad / bass / kick / hat / arp）与亮度；安静段比律动段低 4–8 dB。
- 段落边界**对齐小节线**（按 `bpm` 算），画面重音才落在拍点上。
- 重击前用 `Score.suck()` 让配乐"吸一口气"（压低 3–8 dB、0.2 s 内恢复），落差立刻出来——
  **不要**用静音断层，那会让声音像断了。
- `"mode": "bed"` 是和弦床兜底（没有 bpm 编排时用）；`"mode": "off"` 不要配乐。

### 5.4 音效（sfxkit）

`sfxkit` 是**物理建模**而不是"画波形"：撞击 = 材质（衰减率）× 形状（模态比）× 敲击位置 × 接触时间；
风 = 噪声 + 时变滤波（参数由慢变控制曲线驱动）；雨/火 = 泊松事件 + 幂律幅度 + 多核散布。

```bash
python sfxkit.py --list                      # 全部配方与参数
python sfxkit.py --render impact --seed 3 --set size=1.2 --out /tmp/i.wav
python -c "import sfxkit; print(sfxkit.render('whoosh', dur=1.2).shape)"
```

常用配方（**cue 的 `type` 就是这些名字**）：

| 用途 | 配方 |
|---|---|
| 界面/提示 | `ui_blip` `ui_click` `relay_click` `roulette_tick` `answer_chime` |
| 文字 | `typing` `typing_at`（逐字对齐，`times` 是相对提示点的秒数组） `text_stream` |
| 转场 | `whoosh` `riser` `impact` `sub_drop` `reverse_swell` `glitch` `tape_rewind` |
| 环境 | `wind` `rain` `campfire` |
| 撞击 | `knock_wood` `metal_hit` `glass_clink` `go_stone` |

**音效设计的三条经验**（都是踩过坑才写下的）：

1. **"叮叮咚咚"是头号污染源**：有音高的小提示音多了就变成八音盒。全片只留几个**关键**的叮
   （开场任务完成、认出猫、片尾），其余换成不带音高的"咔嗒"。
2. **"嗖"要从物理出发**：多普勒 + 1/r 音量钟形 + 越远越闷 + 地面反射的梳状相位。
   合成器式的扫频"咻"听起来像电子音，物理飞过模型才像"有东西飞过去"。
   它的定位是**环境声**：基准电平要比同刻配乐低 10 dB 以上。
3. **音量一律在成片混音里量**，不要按配方内部音量推断——混音时每个音效会先按峰值归一，配方里的"响"是没用的。

### 5.5 设计 DSL：外观的唯一来源

**没有"内置风格"可选。** 技能里没有 `STYLES` / `THEMES` / `VISUAL_LANGUAGES` / `presets/`；
`film.json` 里写任何 `style` 值都不影响画面。
外观的唯一来源是 `work/design.json`——由 `propose.py` 现场采样、你 `--confirm` 之后冻结。

```bash
python <SKILL_DIR>/scripts/propose.py --brief "科技发布会，深蓝，要遮幅" --script film.json --work work
python <SKILL_DIR>/scripts/propose.py --confirm --candidate A --work work   # -> work/design.json
python <SKILL_DIR>/scripts/render.py --script film.json --work work --stage style   # 看这份设计到底定成什么样
```

`design.json` 的六块（完整契约见 [references/proposal.md](references/proposal.md) 与
`<SKILL_DIR>/schemas/design.schema.json`）：

| 块 | 决定什么 | 关键字段 |
|---|---|---|
| `structure` | 段落数与目标/下限时长 | `segments` / `beats` / `hold_policy` / `target_sec` / `target_mode`（exact / at_least） |
| `visual` | 画面：配色、字体、版式、动效、装饰、字幕 | 见下面的字段表 |
| `audio` | 配乐参数、声线、音效密度 | `music`（调式/BPM/调性/情绪/seed）/ `voice` / `sfx` |
| `pacing` | 节奏 | `lead` / `tail` / `min_dur` / `hold` / `item_stagger` |
| `id` / `label` | 指纹与中文标签 | `design:<hash>` / 一句中文立意 |
| `pitch` | 说清"这个方案想干成什么样" | 中文一句话 |

`visual` 的字段（`design.json` 可手改，改完用 `python <SKILL_DIR>/scripts/designkit.py --lint work/design.json` 校验）：

| 字段 | 取值 | 说明 |
|---|---|---|
| `palette` | 11 个颜色角色 | **连续色相空间**里的一点（`hsv` 求解），每套都必须过 `theme_lint` 对比度体检 |
| `family` | `sans` / `serif` / `kai` / `round` | 字族 |
| `typography` | `tracking` / `line_gap` / `number`（8 种编号写法）/ `ratio` / `align` | 排版 |
| `motif` | 9 种装饰母题 × 密度（默认 `plain`） | `plain` 是**素净**：装饰层整层不画、底纹为 none、卡片无描边——默认设计不带任何细线。其余 8 种才是显式选择：`brackets` / `hairlines` / `handdrawn` / `slabs` / `minimal` / `letterbox` / `signal` / `rays`（brief 点名「括角 / 细线 / 手绘 / 色块 / 极简 / 遮幅 / 信号 / 放射」即可采到） |
| `transition` | 6 种段界转场 + 时长 | `dip` / `wash` / `paper` / `wipe` / `cut` / `iris` |
| `motion` | `ease`（4 档）/ `enter` / `push` / `breathe_hz` | 入场与**背景**镜头缓推。默认 `push=0`（不推）：慢速推镜在整数光栅上必然变成 1px 台阶抖动，所以前景永远锁定、只有背景板缩放 |
| `bias` | `x` / `y` / `inset` | 整体重心偏移 |
| `layout` | `left-stack` / `center-stack` / `split-right` | 默认版式 |
| `layout_cycle` | 版式数组（可选） | 按章节轮换，见下 |
| `hud` | `chip-left` / `corner-line` / `none`（默认 `none`） | 页眉装饰。默认关闭：`chip-left` 是「6px 通栏进度条 + 1px 胶囊描边」，`corner-line` 更是一组细线 |
| `size` / `fps` | `1920x1080` / `30` | 横屏 16:9（1920x1080，可下采样 720p/540p）与**竖屏 9:16**（1080x1920，experimental）都支持；竖屏要配 `--layout portrait`，见 §7.5.1 |
| `subtitle` | 字幕样式（可选） | 见下 |

离散维度合计 **9 母题 × 6 转场 × 4 缓动 × 8 编号 × 3 版式 × 3 HUD × 4 字族 × 7 情绪 × 2 调式 =
870,912 种组合**，再乘配色这个连续量（色相 0–360° 任意取值，只是采样器会在锚点附近取）；
**这个数是"字段能组合出多少种"，不是"推荐你从中挑一套"**——好看与否仍由对比度体检、分镜语法和你的目检决定。

#### 5.5.0b 手改 `design.json` 之后：`--adopt`，别重跑提案

`work/design.json` 的每个字段都是开放的（配色、节奏、字幕、母题都能改），但 `proposal.json`
存的是**确认那一刻**的设计指纹。改完直接渲染，提案闸会判 `stale` 并在 QC 报告里留 WARN；
而重跑 `propose --confirm` 会把你刚改的东西覆盖掉。正确做法是重新绑指纹：

```bash
python <SKILL_DIR>/scripts/propose.py --adopt --work work --script film.json
#   == adopt：把 work/design.json 重新绑成 confirmed
#      新指纹: 37f33435d890c2c3
#      旧指纹: 92479a7fccfb917e（手改过 design.json（符合预期））
```

- 只改 `designHash`（+ 剧本指纹），**不动** candidates / chosen；
- 设计过不了 `validate_design`（比如改出一个对比度不达标的强调色）时**拒绝**，不会把坏设计绑成已确认；
- 旧指纹与提案里的候选指纹也对不上时，会把两种可能都说出来（手改 / 选错文件）。

### 5.5.1 字幕样式 `visual.subtitle`（可选）

字幕的字号 / 位置 / 背板透明度 / 圆角 / 字族 / 颜色 / 折行数都在设计 DSL 里。
**不写 = 补齐默认值**（与最朴素的字幕带逐像素一致），所以手写的旧 `design.json` 不受影响。

```json
"subtitle": {"size": 46, "y": 560, "align": "left", "plate_alpha": 170,
             "radius": 10, "family": "sans", "color": [245, 245, 245], "max_lines": 2}
```

| 字段 | 默认 | 范围 / 取值 | 作用 |
|---|---|---|---|
| `size` | `46` | 20–96 | 字幕字号 |
| `y` | `null` | 0–1030 | 字幕带基准线；`null` = 默认贴底安全区 |
| `align` | `center` | `center` / `left` | 对齐 |
| `plate_alpha` | `170` | 0–255 | 字幕背板透明度（0 = 无背板） |
| `radius` | `10` | 0–48 | 背板圆角 |
| `family` | `null` | `sans` / `serif` / `kai` / `round`；`null` = 跟随大标题字族 | 字幕字族 |
| `color` | `null` | `[r,g,b]` 或 `"#rrggbb"`；`null` = 默认墨色 | 字幕颜色 |
| `max_lines` | `2` | 1–3 | 最多折几行 |

采样器默认就会在这些区间里变化（字号 40–56、背板透明度 96–194、圆角 2–20、
多数居中偶尔左对齐、1/3 概率抬高），所以**默认出的片字幕也不会两条一模一样**。

#### 5.5.2 片内版式轮换 `visual.layout_cycle`（可选）

`visual.layout` 是单值，只能给一个默认骨架；`layout_cycle` 给一串版式让
`grammar` 在**章节**维度上轮换：同一章内版式固定（观众视角里一章是一段整体），**换章才换版式**。
这就是"同一视频内部不要大面积重复"的落地方式。

```json
"layout_cycle": ["split-right", "left-stack", "center-stack"]
```

- 元素只能取 `left-stack` / `center-stack` / `split-right`；**至少 2 种不同版式**（只写一种等于不轮换，
  `designkit.validate_design` 直接报错，不静默放过）。
- **不写**：`design_to_plan` 按设计指纹自动派生 **2–3 种**的轮换序列——默认就不会"全片一张版式"。
- 可审计：`--stage layout` 打印每段选了哪类构图，并落 `work/storyboard.json`，
  每段都带 `layout` 字段（哪一章用了哪个骨架一眼可查）。

#### 5.5.2b 时长要求：「不少于 N 分钟」是**下限**，不是精确目标

问诊的时长题给的是「不少于 1 / 3 / 5 / 7 / 9 分钟」+ 自定义原话（也认「2 分钟」「90 秒」这类精确说法）。
解析结果分两档，落在 `design.structure.target_sec` 与 `target_mode`：

| `target_mode` | 触发写法 | 渲染对账 |
|---|---|---|
| `at_least` | 「不少于 5 分钟」「至少 3 分钟」「12 分钟以上」 | **只有不足**才告警（超出去是好事，不报） |
| `exact` | 「2 分钟」「90 秒」「一分半」 | 双向告警（差 >15%） |

两档都只影响两件事：候选的段落数（`designkit` 按 `target_sec` 决定 `structure.segments`）与渲染时的对账口径。

**都不会截断旁白**：成片长度永远由 TTS 实测时长决定。写了「不少于 5 分钟」而台词只够 4 分钟，
渲染器只会告诉你「没有达到下限（差 60.0s）」，要补的是台词量（或 `hold`/`lead`/`tail`），
不是指望它凭空拉长。

#### 5.5.3 设计多样性：候选之间、以及片与片之间

`propose.py` 的候选不是"换个颜色"就叫多方案。两个设计在 **10 个离散轴**上比较
（`designkit.design_distance`）：配色 / 母题 / 转场 / 缓动 / 编号 / **配乐（调式 + BPM 档 + 情绪复合键）** /
声线 / 情绪 / 版式 / 调式。规则：

1. **候选之间**至少 **2 个轴**不同，否则重新采样（`propose` 会把"距离"打在候选表里，一眼能看出谁和谁像）。
2. **底色必须拉开色相**：两个候选的底色色相差 < 6° 一律重采（并排看就是同一片底色）；
   色相差 < 12° 时，只有**结构差异 ≥ 3 个轴**才接受。
   判重比的是**色相距离**而不是"配色族分桶"：带 `--brief "深蓝"` 这类约束时可选色相窗口只有 52°，
   分桶会把可行区间切碎成不到 3 个桶、n=3 直接采不出来。实测带约束的 brief 连跑 10 次 **10/10 成功**。
3. **配乐轴**按 **调式 / BPM 档（±6 归一）/ 情绪** 的复合键判定——"两个候选配乐不一样"是真的在比。

#### 5.5.4 片间去重台账（默认开启）

"每条视频都不一样"不能只靠随机：同一个题材连拍三部，仍可能撞到相近的配色/版式组合。
`propose.py` 现在把**确认过（= 真要出片）的设计**记进用户级台账，提案时主动避开：

| 项 | 值 |
|---|---|
| 默认路径 | `~/.config/opencode/video-studio/design-ledger.json`（最多保留 500 条） |
| 环境变量 | `VIDEO_STUDIO_LEDGER` 可改路径；设为 `off` / `0` / `false` / `no` 即关闭 |
| CLI | `--no-ledger`（本次不读不写）/ `--show-ledger`（打印路径与最近 20 条） |
| 判据 | 这批候选与最近交付设计的最小距离 < **2 维** → 换新 nonce 重采（最多 4 轮），并在输出里告警 |
| 记录时机 | **`--confirm` 时**才记（草稿候选不进台账） |

读不到 / 写坏台账都当空表处理——**台账永远不会因为文件损坏而阻塞出片**。
写文档或做回归时把 `VIDEO_STUDIO_LEDGER` 指到临时目录，别动用户真实台账。

#### 5.5.5 三条硬约束（违反会让片子"花"或者被门禁拦）

1. 同一部片**不许跨明暗族**换主题（深蓝段直接切暖白段会精确触发闪烁门禁，观众也看着像换片）。
2. 主题必须过 `stylekit.theme_lint` 的对比度体检（ink≥7 / muted≥4.5 / dim≥3 / accent≥4.5 / accent2≥4.5）。
3. **默认整片只有一个旁白声音**（`max_distinct=1`）。只有用户明确点名「多人对话 / 对谈 / 访谈 / 广播剧 / 分角色 / 男女声」时才采多声线，且固定 2–3 把；裸词「对话」「多人」不算——「大模型的对话能力」不是要两个旁白。显式说「一个人念 / 同一个人」则强制单声线。

#### 5.5.6 没有设计就出不了片（fail-closed）

`film.json` 里写 `"style"`（`auto` / `legacy` / 任何 id）都不影响画面；`render.py --preset` 也不存在。
没有 `work/design.json` 时 `render.py` 与 `filmkit.resolve_theme()` / `style_plan()` 都**直接报错**
并给出两步指引，而不是静默回落一套默认外观——静默回落等于用一套不存在的模板出片。

外观细节（配色怎么解、主题 35 个字段、如何手写一份设计）见
[references/appearance.md](references/appearance.md)。提案流程与契约见 [references/proposal.md](references/proposal.md)。

### 5.5b 场景语法：同一部片内部的骨架也要变

画面不只有"皮肤"（配色/字体），还有三层正交结构：

| 层 | 决定什么 | 谁读它 | 产物 |
|---|---|---|---|
| 叙事 `kind` | 声线、语气、配乐能量 | voicekit / musicgen | `kind` 的取值集合不随场景类型变化 |
| 场景 `type`（10 类构图） | 这段画面长什么样 | `assets/scenes.py` 的 10 个绘制器（`_DRAWERS`） | timeline 的 `scene_type`、`work/storyboard.json` |
| 设计 `visual` 五组签名 | 这套设计怎么画（母题/转场/字距/重心） | 设计 DSL → `work/style.json` | `work/style.json` 的 `language` 与 `layout_cycle` |

场景类型有 10 个：`hero` 封面 / `statement` 金句 / `list` 要点 / `stat` 数字卡 /
`compare` 左右对比 / `process` 流程轴 / `quote` 引语 / `chapter` 章节卡 /
`chart` 数据条 / `outro` 收尾。**不写就不管**——grammar 按内容自动选（字段存在性 >
关键词 > 结构位置，段长不足按固定表降级，连续同类型 ≤2 段）；要锁定就写行级 `lines[].type`。

看选型（命令都不需要 `--scenes`，除非你要用自己的渲染器）：

```bash
python <SKILL_DIR>/scripts/render.py --script film.json --work work --stage layout
#   分镜: 3 段 / 封面×1 要点列表×1 收尾×1
#     l01    hook     -> hero      v0 d0  首段/开场
#     l02    body     -> list      v1 d1  items>=3
#     l03    outro    -> outro     v0 d0  末段/收尾
#   同时落 work/storyboard.json：每段的 type / variant / decor / layout / 理由，可审计

python <SKILL_DIR>/scripts/render.py --script film.json --work work --stage style
#   版式轮换: split-right -> left-stack（按章节轮换，防全片一张版式）
#   视觉语言: 母题=plain 转场=paper(0.40s) 字距=3.0 编号=cn 重心=(-60,+60)   # 默认素净
#   字幕: 底部安全区 44px 对齐=center 背板α=138 圆角=2 最多1行
#   （坏配置在这里就拦，不等渲完 300 帧）
```

**边界（必须记住）**：

1. **10 类构图有渲染开销，但仍在预算内**：示例片 frames 约 **19 ms/帧（54–61 fps）**，预算 25 ms。
   开销来自每类构图各自的装饰与几何；装饰母题**烘焙进背景底版**（1.3–2.0 ms）而不是逐帧 paste
   （实测 5.6 ms），这是把开销压回预算内的关键。
2. **同一部片仍锁一个明暗族**：段界跨族会整屏换底、触发闪烁门禁，也像换片。
3. **web 引擎不跟随场景语法**：`--engine web` 的页面由你自己写，没有 10 类构图，
   画风也只有你页面里的颜色。

选型规则、`storyboard.json` 字段、加第 11 类构图的步骤、常见误用，见
[scene-grammar.md](references/scene-grammar.md)；配色/母题/字幕等设计字段见
[appearance.md](references/appearance.md)。

### 5.6 混音链（照着做，别自创）

```
旁白(单声道, 居中) ─────────────────────────────┐
配乐(立体声, -12dB 基线) ─> 音效duck(-4dB) ─> 旁白duck(-6dB) ─┤─> 求和 ─> 峰值保护(0.89) ─> loudnorm 两遍 ─> -16 LUFS / TP<=-1
音效(立体声) ───────────────────> 旁白duck(-3dB) ─────────────┘
```

- **旁白优先是硬行为**，不是文档口号：`mix_film` 里旁白固定 `voice_gain=1.0` 居中、不被任何侧链影响；
  配乐基线 `music.gain_db=-12`（再高一档背景就会与台词几乎等高）；有人声的地方再按旁白包络压
  配乐 6 dB、音效 3 dB（`music.voice_duck_db` / `music.voice_duck_sfx_db`，间隙自动恢复）。
  实测人声段"旁白/背景"响度差 **12.9 dB**（真实配乐素材下；合成信号口径 15 dB），
  selfcheck 第 35/36 条与 `audio_similarity.py --strict` 的 D 组守着这条。
- **SNR 兜底（混音的最后一道闸）**：混音后按台词活跃区实测"人声/背景"，不足 12 dB 就整体再压背景
  （`music.guard_snr_db`，最多 `guard_cap_db=30`；补压 >18 dB 会提示检查素材电平）。
  这是唯一一条"混音后自己改电平"的门禁，保证换任何配乐素材都不会把旁白盖住。
- **BGM 与音效是可选层**：它们的作用是铺情绪与打重音，不该抢字。想更安静就调低 `music.gain_db`
  或设 `music.enabled=false`；"让观众听清旁白"永远优先于"背景好听"。
- 音效响时配乐自动让位：`design.json` 的 `audio.music.duck_db` 决定档位（默认 4.0 dB），
  `film.json` 的 `music.duck_db` 显式值永远优先。3.0 dB 的让位量不够——段落过门时配乐几乎与音效等高，
  听着像"配乐和音效是同一套"。但让得太多会"音乐一抖一抖"，所以按设计分级而不是一律压狠。
- **本项目不做母带压缩**：链路是 求和 → duck → 峰值保护 0.89 → `ffmpeg loudnorm` 两遍。
  代码里**没有压缩器**（想加请自行在 `mix_film` 之后接），所以也不存在"压太狠"的问题；
  要让重击前后有能量差，靠的是配乐分段 energy 与 `suck()`，不是压缩。
- 成片音轨必须 48 kHz（`mux` 已显式 `-ar 48000`，selfcheck 第 12 条守着）。

### 5.7 声音的片间差异与片内分离

> 问题陈述：**同一套参数下每部片的配乐仍像同一首**。内容指纹只进 seed 时只改音色细节，
> 和弦进行/动机/鼓型全固定——实测同 bpm、同调、只换内容指纹，两段配乐相关性 **0.966**。

指纹必须推进到**结构层**（详见 **references/audio-variation.md**）：

| 层 | 做法 |
|---|---|
| 配乐和声 | 指纹决定**每两小节一次的稳定和弦替换**（不影响乐句末与片尾规则） |
| 配乐进行 | 指纹在 calm/body/drive 三档里选一档（同素材，只换功能顺序） |
| 配乐动机 | 指纹对级数做 **±1 稳定偏移**（节奏不变，问句/答句独立派生；跨主音带八度修正） |
| 配乐鼓型 | 指纹加 ghost kick（不与现有落点重复），clap 位置 ±0.5 拍 |
| 出片接线 | `render.py` 的 `st_score` 把 `seed_effective`（含指纹）传给 `musicgen.render()` |
| 音效 seed | `content_seed(seed_base, style, content_fp, type, t)`（跟内容走，不跟 cue 顺序走） |
| 提示音音高 | `in_key(freq, key)` 跟随配乐调性 |
| 段落边界 | 边界前 2.2 s 内的 whoosh/riser **自动 -3 dB**（不与配乐过门撞车） |
| duck | 按设计分级（默认 4.0 dB），配乐让得彻底而不至于一抖一抖 |
| 旁白优先 | 配乐 -12 dB 基线 + 人声处再压 6 dB（音效 3 dB），人声段 SNR 实测 15 dB |

**可关**：`sfx.content_seed: false` 时音效走 `seed_base + i`（只跟 cue 顺序）的路径，
同参数逐样本稳定（selfcheck 第 33 条守着）。注意：**没有 legacy 外观**——外观永远来自 `design.json`。

验收工具（改音频相关代码后必须跑）：

```bash
python <SKILL_DIR>/tools/audio_similarity.py --strict   # A/B/C/D 四组阈值，失败 exit 1
```

## 6. 停留窗口：让观众看清楚，而不是整片放慢

> 来自同一部成片的复盘："部分画面没看清就被切走了"。**解决办法不是把整片放慢，而是按内容加停留。**

```jsonc
"hold": 0.4,                 // 每句语音结束后统一多停 0.4 s
"lines": [{"id": "l03", "hold": 1.2, ...}]   // 这一句单独停 1.2 s
```

- `hold` 只加在**语音结束后**，不影响旁白节奏；句子读得还是那么快，只是停在画面上的时间变长。
- 加长量取 `0.5 s`（120 BPM 下的**四分音符**＝一拍）的整数倍，画面重音仍落在拍点上。
- 配乐 `sections` 用 `at: "l03"` 而不是绝对秒——停留窗口一改，段落边界自动跟着走。
- `bar` 字段可以让每段时长自动对齐小节（`"bar": 2.0` = 120 BPM 一小节），**代价是段落时长不再精确等于
  语音+停留**，只在对齐更重要时才用。

**判断哪里需要停留**（按顺序看这三条）：
1. 画面上出现了**要读的字**（数字滚完、卡片落定、字幕出齐）→ 至少留 `0.5 s + 字数/9`。
2. 出现了**要理解的结构**（网络层次、坐标轴、流程图）→ 留到能扫一眼看完。
3. 两个镜头之间**没有内容连续性**（黑一下、空一拍）→ 让下一幕提前开始，而不是加长黑场。

## 6.1 词级时间轴与词锚点

> **一句话**：句时长仍然是权威；词级时间戳是**附加值**，只用来「字幕按词折行 / cue 打到某个词上 /
> 门禁判它有没有变」——它不参与排轴，也**绝不**用词时长之和去推算句子时长。

**开与关**：`film.json` 顶层 `"words": true|false`，默认 **true**。
`--stage tts` 会额外收 edge-tts 的 `WordBoundary` 事件，落盘 `work/words.json`：

```jsonc
{ "schema": 1, "enabled": true,
  "lines": { "l03": { "dur": 7.18, "n": 13, "covered": 5.2, "tail": 0.09,
                      "gaps": [0.18, 0.21, ...], "mono": true,
                      "words": [{"text":"画质","start":0.04,"end":0.32}, ...] } },
  "stats": { "lines": 4, "words": 64, "empty": [] } }
```

**它怎么来的（实现口径，改代码前先看）**：
- 必须显式 `edge_tts.Communicate(..., boundary="WordBoundary").stream()` 同时收音频与词事件。
  用 `save()` 会把词事件丢掉；不传 `boundary` 时默认是 `SentenceBoundary`，**一条词事件都收不到**
  （实测 edge-tts 7.3.1 / zh-CN：默认设置下词事件数 = 0）。
- 音频先写 `<句>.mp3.part` 再 `os.replace`。旧写法直接 `open(..., "wb")`，一次断网就把**已缓存的好音频
  截成 0 字节**；现在失败只丢这次的新音频，旧缓存还在。
- 词事件 offset 是相对**原始 mp3** 的 100 ns 单位；`trim_silence=True` 时时间轴用的是裁剪后的语音，
  所以 `words_normalize(offset_shift=裁掉的句首秒数)` 会把词表整体平移。**不平移，词会整体提前 0.1–1 s**
  （实测模板句 shift = 0.10 s 量级）。
- 词表有独立缓存键 `tts_words_key`，没并进 `tts_key_v2`：老音频照旧复用，只在"这次显式要词级"时补采一次。
- 词事件收不到**不是失败**：meta 里记 `words_ok=false`、`words_events=0`，打印 WARN 并复用已有音频；
  G10 会记 `skipped`（"词事件为空"），不许冒充 pass。

**下游怎么用**：

| 能力 | 用法 | 退化行为 |
|---|---|---|
| 字幕按词折行 | `fit_sub_lines(..., words=)` / `draw_subtitle(..., words=)` | 词表对不上原文或折出来超过 2 行 -> **回退字符折行**（老项目排版不变） |
| cue 词锚点 | `"at": "l03:w2"` / `"at": "l03:w2+0.1"`（**从 1 数**，w1 = 第一个词） | 该句没有那么多词 -> 退化为 `l03+偏移` 并打 WARN；场景 id 也不存在才报错 |
| 门禁判据 | G10 读 `work/words.json`（唯一事实来源，不再各自翻 `*.mp3.json`） | 没开/没数据 -> skipped |

**三个坑（都在真实跑里踩过）**：

1. **词间有空隙**：实测 edge-tts 相邻词之间有 0.1–0.3 s 不在任何事件里。所以"词时长求和"永远小于句长，
   G10 的覆盖率阈值才定在 45%（低于它才是"明显少了"），别拿 `sum(dur) == dur` 当判据。
2. **句尾静音不在词事件里**：约 0.5 s，靠**整句实测时长**兜底。G10 只检查"末词终点离句尾不超过 0.40 s"
   （示例片实测 0.07–0.12 s），超过它通常意味着词事件本身错位了。
3. **词锚点会静默退化**：`l03:w99` 不会炸，只会 WARN 后退回句偏移。**改台词后先看 stdout 有没有这条 WARN**——
   否则你会以为自己精确打到了某个词。同样的，`words=false` 或词事件为空时，字幕不会报错，只是退回整句断行。

**老项目第一次升级**：默认 `words=true` 会让每句**重新联网合成一次**（老缓存里没有 `words_key`）。
一次全片 TTS 的网络成本，换词级能力；不想付就把 `"words": false` 写进 `film.json`，行为退回整句断行。

细则（词级契约、时长权威性、裁剪平移公式、词锚点退化路径）见 [references/word-timeline.md](references/word-timeline.md)。

## 7. 质检门禁（error 必过 / warning 别当看不见）

```bash
python render.py ... --stage qa     # 只读质检：分辨率/音轨/时长/响度/真峰/锐度比/黑帧/字幕条数 + 帧级深检
python selfcheck.py                 # 技能自身回归（105 条针对真实缺陷的断言）
python qa_frames.py out/film.mp4 --cues work/cues.json   # 单独跑帧级深检，出 JSON + 图
# qa / all 收尾会自动写 out/QC-REPORT.md（机器可读交付结论）
#       并落 work/gates.json（三档 gate matrix）与 work/cost.jsonl（实测耗时台账）
```

### 7.0 交付凭据：`out/QC-REPORT.md` 与人工签核

> 这一节解决的是"**质检结论只活在控制台里**"——会话一断，"我查过了"就无从复现。

**三档状态**（这是最关键的语义，不许混）：

| 状态 | 含义 | 退出码 |
|---|---|---|
| `pass` | 本次**真的查了**且通过 | 0 |
| `fail` | 查了没过 | 非 0（除非 `--no-gate` 绕过，且会留痕） |
| `skipped` | 本次**没有执行**（没装 cv2 / 没写 visual_at / 没签核） | 默认 0，`--strict-gates` 下非 0 |

**"skipped ≠ 通过"** 是这套机制的全部意义：以前"没查"和"查了没事"在输出上都叫"没报错"。

```bash
cat out/QC-REPORT.md                          # gate matrix + 实测值 + 签核 + 耗时，交付时一起给
python render.py ... --stage qa --strict-gates   # 交付前：skipped 也算失败
python render.py ... --stage accept --gate stills --note "看过联系表"   # 人工签核
```

**报告头部有「运行编号」与「输入指纹」两行**：前者是 `--run-id`（不给就按时间戳生成），
后者是 script / scenes / words / filmkit 四个内容的指纹。同一部片跑了好几版之后，
"上一版的门禁结论到底是什么"就靠 `work/runs/<runId>/` 里的快照回答（只归档小文件，
视频/音频/分块就地保留）。

**四道人工签核闸**（`style` 风格与声线 / `stills` 母版静帧 / `facts` 上屏数字 / `release` 成片验收）：

- 签核前会检查"证据产物存在"（如 `stills` 要求 `work/qa/sheet.png`），**没有证据不许签**——
  否则签核就是橡皮图章。
- 签核绑**内容指纹**（剧本 + `scenes.py` + 引擎）。改了其中任何一个，旧签核**自动失效**，
  报告里会标成 `skipped` 并写明"内容已变"。这不是麻烦，是防止"我签过了"变成空头支票。
- 逃生阀是 `--no-gate`（整体跳过签核检查），但会写进 QC 报告的"本次绕过的门禁"一节，
  **留痕不等于通过**。

**耗时台账** `work/cost.jsonl`：每个 stage 一行实测墙钟。**只有显式加 `--cost` 才记**——`--stage all` 不会替你打开它（旧文写"自动记"，与实现不符）。
它是 SKILL §12 那张"排期公式"的对照——公式从来没被验证过，现在有数据了。

### 7.0b 阶段新鲜度：别让旧产物混进新片

> 与 §1 的分块指纹同源：那里只修了 `frames` 一个点，其余 stage 此前是裸奔的
> （改了 `style` 后 `--stage audio` 仍读旧 `music.wav`；改了 `hold` 后 `--stage frames` 仍用旧 `cues.json`，**都不报错**）。

```bash
python render.py ... --stage explain          # 哪些阶段过时了、为什么、最小重跑命令
python render.py ... --stage qa --strict-stale   # 有过时就失败（CI / 交付前）
# 机读：问 stagewatch（render.py 的 explain 只打人读文本，不接受 --json）
python scripts/stagewatch.py --work work --json
```

机制：每个 stage 结束往 `work/manifest.json` 记一行 `{stage, at, inputs:{名:指纹}, outputs:{名:指纹}}`；
下次该 stage 开始前重算上游输入与自己的产物指纹。**默认只告警**（`--strict-stale` 升级为失败，
`--allow-stale` 显式放行并留痕）。改一句要重跑什么，见 [references/change-impact.md](references/change-impact.md)。

**五道资源/画质/版式门禁**（账本 id：`G0` / `G10` / `G11` / `G12` / `G13`，报告里记在 `G4` 之后）：

| 门禁 | 查什么 | 三档语义（`pass` / `fail` / `skipped`） |
|---|---|---|
| `G0` 渲染前资源预检 | 剧本结构、全部上屏文本逐字过字形检测（按字段选字重）、`work/tts/*.mp3` 能否被 ffprobe 解析、字体候选能否加载、web 入口页是否存在 | 有 error -> `fail`；只有 warning -> `pass`（warning 仍进账本）；没拷 `preflight.py` -> `skipped` |
| `G10` 词级时间戳 | `work/words.json` 的覆盖率/单调性/末词与首词偏差 | 判据不过 -> `fail`；`words=false`、没有 `words.json`、词事件为空 -> `skipped`（写明原因） |
| `G11` 全片 VMAF/SSIM | 参考帧由**场景函数实时重渲**，经 rawvideo 喂 ffmpeg 与成片逐帧比（不是从成片抽帧——那样恒等于 100） | VMAF < **92** -> `fail`；[92, 96) -> `pass` + warning；缺 `qavmaf.py` / web 引擎 / `--vmaf off` / 没 libvmaf -> `skipped`。阈值只有一份，定义在 `qavmaf.VMAF_PASS / VMAF_WARN` |
| `G12` ASR 回读 | 成片音轨转文字，按 timeline 每段窗口算**字符级多重集召回**（中文没有词边界，字符级才成立；多重集是因为"是"这种字要计次数） | 召回 < 0.72 -> `fail`；[0.72, 0.85) -> `pass` + warning；两个引擎都不可用 / 没装 `qaasr.py` / 显式关闭 -> `skipped` |
| `G13` 版式文字冲突 | 在 `_draw_text`（所有文字的唯一出口）登记最终落笔矩形，逐场景重渲关键帧后判定：两行字相交 >= 较小框 18% / 越出 SAFE / 压进字幕带 | 有冲突 -> `fail`；`scenes.py` 没有 `text_qa_start` 采集钩子（旧版/自写渲染器）-> `skipped` 并额外告警；阈值定义在 `qatext.py`（`OVERLAP_FRAC=0.18`）；`hud/number/decor` 角色豁免 |

**为什么 G11 值得全片跑**：锐度比只在 4 个时刻采样，能发现"整体发虚"，发现不了"某 30 秒里字幕糊了"。
VMAF 是唯一覆盖整条时间轴的客观感知指标。代价是**每片多渲一遍参考帧**（29 s 片实测 14.2 s 全跑完；
> 机器被其它任务占满时同一份代码测到过 28.8 s，同机不同负载差近 2×，只认"这一跑"的数字）。
**为什么 G12 值得跑**：容器/响度/帧级质检全都默认"音轨里念的就是剧本那几句"——缓存串了、漏句、被压没了，
从波形和容器上都看不出来。代价约 6 s/片（small 模型，CPU）。

**为什么 G13 值得跑**：一次真实交付里，一部 19 分钟的片子整片字压字（章节卡的小标签压在章节名上、要点列表的标题压住小标签），而 G0/G4-G12 与 F1-F8 全部通过（当时的门禁集；F9 是本版之后补的）。因为既有门禁都在量帧上的亮度、位移、音频，没有一门在看两行字是不是画在同一块位置。G13 补的就是这一层：它不看成片，而是在渲染进程里重渲关键帧、取落笔矩形，既能抓位置算错，也能抓改了代码没生效（后者表现为采集钩子缺失 -> skipped，报告里必须写明）。

G0 / G11 / G12 三个新模块（外加 `G10` 词级）的实测数据、阈值来源与关闭方式见 [references/delivery-gates.md](references/delivery-gates.md)。

**质检分三层**，一层比一层更接近"观众真的看到了什么"：

| 层 | 查什么 | 工具 | 抓得住什么 |
|---|---|---|---|
| 容器层 | 分辨率/帧率/音轨/时长/码率 | `ffprobe`（`filmkit.probe`） | 参数写错、音轨丢了 |
| 信号层 | 响度/真峰/锐度比/黑帧/静音/字幕条数 | `filmkit.qa_report` | 像草稿、文字发虚、静默丢字幕 |
| **时间层** | **单帧闪烁 / 画面空窗 / 音画同步 / 响度曲线** | `qa_frames.py` | **"一帧白闪""两秒没东西看""重击没声音"** |
| 页面层（web 引擎） | 文字重叠/出画/一闪而过/从没露面 | `webengine.py textqa` | 页面里被别的元素压住的字 |

时间层需要单独说，因为它是**唯一靠"看完全片"才能发现**的一类问题：单看任何一帧都正常。

**先分清两档**（这张表以前混在一起，标题写"必过"，实际 8 行里 5 行只是 warning）：

- **error 档**：出现即 `qa` 返回非 0、CI 红。这些是"片子坏了"。
- **warning 档**：只提示，不阻断交付。这些是"能更好"或"门禁没跑"。

#### error 档（必须过）

| 判据 | 阈值 | 失败怎么办 |
|---|---|---|
| 分辨率/帧率 | 严格等于**交付规格**（`timeline.video` 的 out_w/out_h/fps；缺省回落 1080p/30） | 检查 `FrameSink` 参数与 `film.json` 的 `video` 块是否一致；`strict=True` 会直接报错而不是静默缩放 |
| 音轨缺失 | 必须有音轨 | mux 参数漏了 `-i audio.wav` |
| 时长 | 与 `frames/FPS` 差 < 0.5 s | 断点续渲漏帧；分块模式检查缺口检测 |
| **真峰** | **> -1.0 dBTP**（-1.0 ~ -1.4 之间只 warn） | 混流前目标设 -1.5（AAC 会把真峰抬高 ~0.9 dB） |
| **编码锐度比** | **< 0.85**（0.85–0.92 只 warn；没装 cv2 则门禁跳过 + warn） | 降 CRF 或提 preset |
| **单帧闪烁** | **0 条** | 位移/推力没归零、粒子贴镜头过曝、转速突变；见 §8 故障速查 |
| **画面空窗** | 首尾以外 0 段（≥0.8 s 近黑） | 转场之间没有内容连续性：让下一幕提前开始 |
| **音画同步** | 判到超差（`dt > 60 ms`）即 error；**但默认可能根本没在查**（见下方两小节） | 要给 cue 写 `visual_at`，或设 `QA_REQUIRE_SYNC=1` 把"没查"也变成 error |
| **字幕条数** | **= 剧本句数** | 少一条就是静默丢字幕，查 `sub_window` 与 `write_srt` |
| **时长（时间层复核）** | 与预期差 > 0.5 s | `qa_frames` 会独立再查一次时长 |
| **渲染前资源预检（G0）** | 有任何 error（缺字形/坏 TTS 音频/字体全不可加载/web 入口页不存在） | 按报错里的「字符 + 字段 + 句 id」改：换说法、改用矢量画、`--force-tts` 重合成。**这一步在渲第一帧之前**，改起来最便宜；`--no-preflight` 可跳过（会在 QC 报告留痕，不算通过） |
| **词级时间戳（G10）** | 词覆盖率 < 45%、词偏移非单调、末词终点离句尾 > 0.40 s、首词起点离句首 > 0.20 s、或有句子一条词事件都收不到 | 多为 edge-tts 服务端行为变了或该句被裁坏了：先看 `work/words.json` 的 `stats.empty`，再 `--force-tts` 重采；长期不可靠就把 `"words": false`（退回整句字幕，G10 记 skipped） |
| **全片 VMAF/SSIM（G11）** | VMAF 均值 **< 92** fail；**[92, 96)** 只记 warning（能出片，提示复核/别降档）；SSIM（0.95/0.98）是诊断项不单独阻断 | 先看是不是 CRF/preset 调高了或走了二次编码；阈值只有一份（`qavmaf.py` 的 `VMAF_PASS/WARN`），按片覆盖写 `"qa": {"vmaf": {"pass": 92, "warn": 96}}`；`--vmaf off` 可关（记 skipped）。web 引擎恒 skipped（没有可重渲的参考帧） |
| **ASR 回读（G12）** | 字符召回 **< 0.72** fail；**[0.72, 0.85)** pass + warning；**≥ 0.85** 干净 | 抓的是"音轨念的不是剧本"：TTS 缓存串了、换声线漏句、混音压没了。低于 0.72 先 `--force-tts` + 检查 `work/tts.json`；`"qa": {"asr": {"enabled": false}}` 可关（记 skipped） |

#### warning 档（不阻断，但别当看不见）

| 判据 | 触发条件 | 怎么办 |
|---|---|---|
| 音轨采样率 | ≠ 48 kHz（**音轨缺失才是 error**） | mux 的 `-ar 48000` 被漏了 |
| 响度略偏 | -17.2 ~ -14.5 之外但仍在 -19 ~ -12 内 | 目标 -16 ± 1 是"舒适区"；重跑 loudnorm |
| 真峰余量不足 | -1.4 ~ -1.0 dBTP | AAC 编码后可能更高，留 1.4 dB 余量 |
| 锐度比偏低 | 0.85 ~ 0.92 | 可接受但可更好 |
| 长黑帧 | 首尾以外出现 ≥0.4 s 黑帧 | 检查素材/淡入淡出 |
| 长静音 | 出现 ≥1.2 s 静音 | 确认是否有意留白 |
| 字幕安全区 | `qa_text_safe` 有警告（>2 行、越界、触底） | 缩短字幕或调 `SUB_BAND` |
| 缺字形 | `check_glyphs` 有警告 | 改文字或用矢量线条画（msyhbd 缺 ✓ × ↵ 等） |
| **画面长时间静止** | 连续 6 s 内最活跃 8×8 块的 0.5 s 漂移 ≤ 1.5 级灰度 | 加视觉变化（§4「每 3–6 秒要有变化」）；确实要静止就接受它 |
| 门禁没执行 | 没装 cv2 / 测不到响度 / 找不到 sub.srt / 没 events 也没锚点 | **这一条最重要：它说明"看起来通过的项，其实没在查"** |

**三个必须修对的实现细节（做错了门禁就是摆设），早期版本的"门禁"其实是摆设）：**

1. **锐度比的参考帧不能是成片自己抽的帧。** 那样 `sharp_ref` 与 `sharp_dec` 几乎相同，
   ratio 恒 ≈ 1.0。必须用**场景函数重新渲染**同一时刻的原图（`st_qa` 已这么做）。
2. **真峰必须显式开 `ebur128=peak=true`。** 默认参数下解析不到 `Peak:`，
   "真峰 ≤ -1 dBTP" 这条永远不触发。
3. **码率不是质量指标。** 暗底大字内容 0.5–1 Mbps 是正常的；把它当"下限"会天天误报。

**"不能有"和"必须有"两类都要查**：闪烁/重叠/出画/削波是"不能有"；图名框/淡入/字幕/每段至少一个动态元素是"必须有"——后者最容易静默失效。

**时间层门禁怎么读**（`work/qa/deep.json`）：

| 字段 | 意义 | 判据 |
|---|---|---|
| `flicker[]` | 只亮一帧的格子：`|F[t]-(F[t-1]+F[t+1])/2|` 分块超阈值 **且** `|F[t+1]-F[t-1]|` 很小（"回到原位"——加速运动不满足这条） | 0 条；有的话 `bbox` 告诉你在画面哪个位置 |
| `black_windows[]` | ≥0.8 s 近黑（片头片尾不算） | 0 段；有就补内容连续性 |
| `sync[]` | cue 与视觉事件的时间差 | 全部 `|dt| ≤ 0.06 s` |
| `luma[]` / `loudness[]` | 逐帧亮度 / 短时响度曲线 | 看形状：有没有"该有起伏的地方一条直线" |

**别把"重击"当闪烁**：设计好的闪白/震屏会出现在 `events` 里（web 引擎由页面导出，pil 引擎写进 `cues.json`），
`qa_frames` 会先排除它们再报 `flicker`。没登记却又闪的，才是 bug。

**也别把"运动最快的一帧"当闪烁**：二阶差分对加速运动天生过敏（逐字入场、缓动推近、
交界黑场的起坡帧）。所以判据还有第三道闸——**单帧事件必须"回到原位"**：
`|F[t+1] - F[t-1]|` 要远小于中间帧的偏离量（`FLICKER_RETURN_RATIO = 1.6`）。
高分（≥10）但被判成运动的候选**会进 warning**（"高分异常被判为运动，建议人工复核"）——
**门禁可以保守，但不能静默**。

实测反例（本技能自带的 web 示例页）：报了 3 处闪烁，逐一逐帧查下来**全部是**
"最后一个字入场"和"交界黑场起坡"这两类加速运动；加上这道闸后归零，
而 selftest 里注入的单帧白闪仍然能被检出——**没被削弱的门禁才叫门禁**。
调阈值之前先确认这条闸在不在：没有它，你会把正常运动当 bug，改到死。

### 音画同步的命名约定（门禁会不会静默失效就看这条）

`sync_check` **先按 `events.kind == cue 的配方名` 配对**。所以画面里"该响一下"的事件，
kind 要写成**同一个配方名**：

```js
S.cue(0.62, 'impact', {size: 1.15});   // 音频：配方名 impact
S.event(0.62, 'impact', {});           // 画面：同名的 visual event
S.event(0.62, 'ring',   {});           // 纯装饰事件：用通用名，不参与配对
```

**不这么写会怎样**：页面只导出 `flash / shake / ring` 而 cue 是 `impact / whoosh` 时，
同 kind 永远配不上、跨 kind 又只做确认，`sync` 会恒为空 —— **看起来"零超差"，其实一条都没在查**。

两道保护：
1. `qa_frames` 发现"cues 与 events 没有任何同名项"时**主动给 warning**（"音画同步检查可能没生效"）；
2. 自查：故意把某个 cue 的时刻改错 0.3 s 重跑，**必须**能报出超差。不报就是没配上。

### PIL 路线的同步门禁怎么真的生效

`--engine pil` 没有页面，也就没有 `events`，"同 kind 配对"永远配不上——**这条门禁默认是没在查的**。
两条真正能用的路：

1. **给 cue 写画面锚点**（推荐）：在 `film.json` 的 `sfx.cues` 或 `scenes.py` 的 `cues()` 里，
   给每条 cue 加一个可选字段 `"visual_at"`（或 `"visual_t"`），值是**画面上那个事件真正发生的绝对秒数**：

   ```json
   { "at": "l03+0.05", "type": "impact", "visual_at": 15.77 }
   ```

   `qa_frames` 会直接算 `|cue 时刻 - visual_at|` 并报超差，不依赖 events / 命名约定。
   锚点必须写画面上的真实时刻——随便填一个等于自己骗自己。

2. **把"没查"变成失败**：设环境变量 `QA_REQUIRE_SYNC=1`（或 `qa_frames.py --require-sync`），
   "音画同步门禁没执行"就从 warning 升级成 error。CI / 交付前建议开着。

两条都没做时，`deep.json` 里 `sync` 恒为 `[]`，同时会有一条 warning 明说"同步对照跳过"。
**看到 `sync: []` 先确认是"真的同步"还是"根本没查"**——`warnings` 里有没有那句话就是判据。

## 7.5 交付生产线：画幅 / 批量 / 预览 / 运行版本化

### 7.5.1 画幅与帧率（横屏 16:9 / 竖屏 9:16）

```jsonc
"video": { "width": 1280, "height": 720, "fps": 24 }   // 不写 = 1920x1080@30
```

| 规则 | 实际行为 |
|---|---|
| 内部构图 | 跟随 `layoutkit` 的 profile：**横屏 1920x1080 / 竖屏 1080x1920**；输出更小时在编码阶段用 ffmpeg `scale`（lanczos）下采样（`FrameSink(out_w=, out_h=)`） |
| 允许的输出尺寸 | 横屏 1920x1080 / 1280x720 / 960x540（2560x1440 在"允许列表"里但**会被上采样规则拒掉**）；竖屏 1080x1920 / 720x1280 / 540x960 |
| 允许的 fps | 24 / 25 / 30 / 50 / 60 |
| 竖屏 | **支持（experimental）**：加 `--layout portrait`（render / propose / filmcheck 都有这个参数），画布切到 1080x1920，安全区与字幕带随之重算。10 类构图**全部竖屏原生**（stat/quote/chapter/hero/statement/list/outro 纵向重排，compare 上下、process 竖向轴、chart 竖排条） |
| 竖屏字号 | `layoutkit.TYPE_SCALE = 0.89`（横屏 1.0）：判据是「正文 ≥ `MIN_BODY_PX = 30px`」，不是几何比例——designkit 的 body 基准是 34px，纯几何缩放 0.5625 只有 ~19px；30/34≈0.882，取 0.89 把正文抬到 30px |
| 竖屏逐类停留 | `render._apply_type_hold`：chart +0.45 / compare +0.40 / process +0.40 / list +0.30 / stat +0.25 / chapter +0.15 / outro +0.15，hero/statement/quote 不加；总增量上限片长 6%。段变长会整体后移后续段并重算 total/frames。横屏零变化 |
| 竖屏输出比例 | 非 9:16 的输出**直接报错**；横屏片与竖屏片都只能同比例下采样 |
| 上采样 | **直接报错**。输出大于当前画布 = 把 1080p 包装成"更高分辨率"（假 4K）；要真高清得改内部构图 |
| 非法 fps | **直接报错**（报错里列出可用值） |

链路：`filmkit.video_plan(script)` 在 `layout` 阶段就解析并校验（错了不拖到渲帧），
结果写进 `timeline.json` 的 `video` 块；`G4` 容器门禁改成**按交付规格**比对（不是恒看 1920x1080）；
`encode_quality` 的参考帧会先缩到交付分辨率再比锐度。

> **跨分辨率别比锐度比**：同一片 1080p 交付实测 1.009、720p 交付实测 1.206——数值不可横向比较
> （参考帧也被缩到交付分辨率，笔画占画面的比例变了）。它只用来判"这一版有没有比上一版糊"，别拿它当"720p 更清晰"。
>
> 720p 交付已实测通过全部门禁：`G4 = 1280x720@24`、`G11 = VMAF 97.10 / SSIM 0.9925`（696 帧，7.2 s）。
> `filmkit.qa_report` 为此新增 `expect_w/expect_h/expect_fps`——容器门禁比的是**交付规格**，
> 不是死比 1920x1080；改规格时记得让 `timeline.video` 与之一致（qavmaf 会核对两者是否同源）。

### 7.5.2 批量生产（`--batch`）

```jsonc
// films.json —— 两种写法都接受
["a/film.json", "b/film.json"]
{"films": [{"script": "a/film.json", "scenes": "a/scenes.py",
            "work": "a/work", "out": "out/a.mp4", "label": "A"}]}
```

```bash
python render.py --batch films.json
```

| 行为 | 细节 |
|---|---|
| 上限 | **100 条 / 1 MiB**（清单本身不该是个大 JSON）；超了直接报错让你拆份 |
| 开跑前 | **全量 `filmcheck` 预检**：任一条不通过就**一条都不跑**，把明细一次性报全（`batch-report.json` 里 `phase="preflight"`） |
| 执行 | 逐条起**子进程**渲染（每条都是完整的 `--stage all`），**单条失败不阻断其余** |
| 收尾 | `batch-report.json`（`phase="render"`，含每条 `exit` / `wall_s` / 日志尾）+ 退出码：全成功 `0`，**部分失败 `1`** |
| 预检失败的退出码 | 也是 `1`（`SystemExit` 带文字，Python 的默认行为）；区别看 `batch-report.json` 的 `phase` |
| 逐条透传的开关 | `--jobs`（≠1 时）/`--resume`/`--force-tts`/`--force-music`/`--strict-stale`/`--allow-stale`/`--no-gate`/`--require-proposal`/`--strict-scenes`：这些对每条都成立，原样传给子进程 |
| 只能写进清单的开关 | `--out`/`--work`/`--script`/`--scenes`/`--note`/`--by`/`--verdict`/`--stills` 是**逐条**概念，命令行给了就报错（旧行为是静默忽略——写了以为生效） |
| `scenes` 缺省 | 清单条目没写 `scenes` 时**不强行补**"剧本同目录 scenes.py"：文件不存在就回落技能内置渲染器，与单跑一致（旧行为是子进程直接退出） |
| 撞车检测 | 两条的 `work` 或 `out` 解析到同一路径时**开跑前报错**（否则后一条会覆盖前一条的成片，两条却都报 OK） |

实测（本机，2 条 29 s 示例片）：**67.2 s / 60.1 s**，各 `pass 11 · fail 0 · skipped 1`。
注意每条都要付 G11 的参考帧成本——批量不是"摊销"，是线性叠加。**而且瓶颈已经转到 qa**：
单条全链实测 47.3 s 里 `qa` 占 23.9 s（见 §1 的时间表），批量时这个比例会原样复制。

### 7.5.3 预览片（`--preview N`）

```bash
python render.py ... --preview 15        # 只渲前 15 秒
```

- 产物：`out/preview/<片名>_preview.mp4` + 同名 `.gif`（480 px / 10 fps，两遍调色板生成）。
- 音轨：有 `work/audio.wav` 就**先截到预览时长再混入**。直接拿全长音轨去配短片，`mux` 会判
  "音视频时长差 ≥ 0.05 s" 而失败（开发期实测踩过）；没有音轨就出无声版，不阻断。
- **不落交付账本**：它是给人快速看效果的，不是交付证据；正式结论仍以 `--stage qa` 的报告为准。
- **只支持 PIL 引擎**：`--preview` + `--engine web` 直接报错（预览必须与成片同一条渲染路径，否则预览"好看"不代表成片好看）。
- **与单 stage 互斥**：`--preview` 只和 `--stage all`（或缺省）同用；写 `--stage qa --preview 15` 会报错，而不是静默只出预览。
- **秒数下限是一帧**：0、负数、`nan`/`inf` 一律报错（旧行为是渲 0 帧却退 0，等于假装成功）。

### 7.5.4 运行版本化（`--run-id` + `work/runs/`）

```bash
python render.py ... --run-id hero-v3     # 不给就按时间戳生成 rYYYYMMDD-HHMMSS
```

- `work/gates.json` 增 `runId` 与 `inputs`（**script / scenes / words / filmkit** 四个内容指纹）。
  为什么要逐项指纹：`contentHash` 只说"变了没"，说不出"变了哪个"；批量出片最常见的事故就是
  "这一条用的还是上一版的词级时间戳"。
- 每次收尾（`gates.archive_run`）把**账本快照 + QC-REPORT.md + run.json** 复制到
  `work/runs/<runId>/`。只归档小文件；视频/音频/分块体积大，就地保留。
- `out/QC-REPORT.md` 头部多两行「运行编号」「输入指纹」，交付时一眼能对上是哪一版跑的。

## 8. 常见故障速查

| 现象 | 根因 | 修法 |
|---|---|---|
| 后半段全程静音 | ffmpeg 多输入混流竞争 | 改 numpy 总线 `bus[i:i+n] += src*gain` |
| 混音爆音/削波 | 先 `astype(float32)` 再判 dtype，归一化分支不执行 | **先判 dtype 再转**，并打印中间峰值 |
| 字幕越出画面被裁 | 字幕带贴底 | `SUB_BAND` 上移到 872，`base_y` 夹紧 |
| 文字发虚 | cv2 中间文件二次压缩 | 改 rawvideo 直通 + CRF18，用锐度比验证 |
| 音画不同步 | 按字数估时长 | 改实测时长驱动 |
| 字幕交叠 | 按字符比例分配字幕时长 | 以 TTS 句边界为窗，窗内按标点切 |
| 全片该有的淡入/图名框消失 | 字段改名没跑冒烟 | 改名后必跑 `snap`；质检加"元素存在性" |
| 渲染越跑越慢 / 内存涨 | 每块开新浏览器没关 | 复用会话 + `finally` 关闭 |
| 拼出的片子缺一段还不报错 | 分块按"第几块"命名 | 按**绝对起始帧**命名 + 显式缺口检测 |
| **请求 524 帧却只出 488 帧** | `-shortest` 按较短一路（音频）截断 | 去掉 `-shortest`，显式 `-t <视频时长>` + 校验音视频时长差 < 0.05 s |
| 产出 13 小时的怪物文件 | `-stream_loop -1` 让 `-shortest` 失效 | 显式 `-t`，别依赖 `-shortest` |
| BGM 混音后掉 6 dB / 听不清 | `amix` 默认 `normalize=1` 会按输入数均分 | `amix=...:normalize=0` 再 `loudnorm=I=-16` |
| 旁白整体偏移 ~0.6 s | `adelay` 铺轨有系统性偏移 | 别用 adelay，改 numpy 按采样点写位 |
| 烧录字幕字号小得看不清 | SRT 交 libass 按 `PlayResY=288` 排版 | `FontSize/MarginV` 要 ×3.75，或直接用 ASS 指定 PlayRes |
| ASS 字幕位置全乱 | Format 行漏了 `MarginL/MarginR/MarginV` 字段 | ASS 头 Format 行字段顺序必须与 Dialogue 完全对应 |
| 渲染出上一部片的内容 | `sys.path` 顺序导致 import 到旧模块 | 用绝对路径 + 每次全新进程；子进程别继承父进程模块缓存 |
| 画面里 ✓ × ↵ 显示成方框 | msyhbd 缺这些字形 | 用矢量画（ImageDraw 线条），不要用符号字符；或先跑 `K.check_glyphs` |
| **改了台词但配音没变** | TTS 缓存只判断"文件存在" | text/voice/rate/pitch 哈希必须写进 `*.mp3.json`；只判存在性的旧缓存要 `--force-tts` 一次 |
| **长句字幕读到一半消失** | `sub_window` 把 t1 钳在 `voice_start+5.2` | 默认覆盖整句；要钳短显式传 `max_hold` |
| **--stage qa 也重渲全片** | stage 分发写成 `in ("all","frames","mux","qa")` | 每个 stage 独立；qa 只读成片 |
| **concatenation 打不开分块** | concat.txt 里写相对路径，ffmpeg 按自己 cwd 解析 | 写**绝对路径** |
| 成片比 silent.mp4 慢一倍 | mux 对已编码视频又编一遍 | `-c:v copy`（默认）；只在要改分辨率时 `reencode_video=True` |
| 每帧渲染慢 2–3 倍 | 每帧重新 `wrap_cn`（中文逐字测宽） | 用 `K.wrap_cn_cached` / `K.text_w_cached` |
| 混音偶发爆表 | 归一化分支没执行 / 叠加峰值 >1 | `AudioBus.mix()` 会打印叠加峰值并软限幅，峰值 >0.97 报警 |
| **成片音轨变成 96kHz** | mux 没指定 `-ar`，loudnorm 内部升到 192kHz 后 AAC 取了 96k | mux 显式 `-ar 48000`（内置于 mux，selfcheck 第 12 条守着） |

## 9. 参考资料（按需加载）

- `references/footage.md` — **素材剪辑线（footage 模式）**：分诊规则、`footage.json` 契约、10 个 stage、素材定位（转写/关键词/中文关键词的 GBK 坑）、画面治理四类配方（烧录字幕/水印/画幅/调色）、F1–F9 门禁与签核、常见故障、交付清单
- `references/proposal.md` — **提案优先**：为什么先出方案、`design.json` 契约、候选采样与设计距离、`visual.subtitle` / `layout_cycle` 字段、片间去重台账、提案闸五档、常见误用
- `references/scene-grammar.md` — **场景语法**：10 类构图怎么选、`storyboard.json` 字段、设计五组视觉签名、加第 11 类构图的步骤、常见误用
- `references/hybrid-overlay.md` — **hybrid 形态 3（默认）**：素材打底 + 小面积叠加的 overlay 契约、叠加预算四条判据、compose 链路、何时该用形态 1
- `references/pipeline.md` — 八阶段详细规格、契约 JSON 格式、目录约定、并行分工
- `references/scene-authoring.md` — 场景函数写法、构图规则、Canvas/CDP 方案、超采样与后处理
- `references/audio.md` — 采样级混音、程序化配乐/音效、响度链路、循环环境音、提示点系统
- `references/audio-variation.md` — **片间差异**：内容指纹怎么进和声/进行/动机/鼓型与验收阈值
- `references/sfx-design.md` — **音效设计**：物理拆解流程、配方表、听感诊断表、QC 规则
- `references/web-engine.md` — **浏览器引擎**：页面契约、确定性时间轴、分块渲染、崩溃重开
- `references/frame-qa.md` — **帧级质检**：单帧闪烁判据、画面空窗、音画同步、报告怎么读
- `references/quality-gates.md` — 方法论 G0–G8 全部门禁、可复制代码、验收清单（文内另有「两套编号别混用」的映射说明：账本是 `G0`/`G4–G12`）
- `references/word-timeline.md` — **词级时间轴**：词级契约、句时长为何仍是权威、词锚点写法与退化路径、四个真实坑
- `references/delivery-gates.md` — **三道可选门禁**：VMAF/SSIM、ASR 回读、渲前预检的判据、阈值来源、实测数据与关闭方式（词级 G10 见 `word-timeline.md`）
- `references/change-impact.md` — **改动影响表**：改了 text/sub/hold/design.json/scenes.py 各要重跑什么
- `references/appearance.md` — **设计 DSL 外观定制**：35 字段主题、连续色相配色、母题/转场/字幕/版式轮换怎么写、常见误用
- `references/pitfalls.md` — 20 条最佳实践 + 17 条反模式 + 5 个 bug 复盘（含取景抖动与音轨中断）
- `references/machine-facts.md` — **参考机器的实测基准**：ffmpeg 构建、编码速度、各模块耗时、排期公式、三条隐形坑（换机后按 §13 重测，别照抄）
- `references/script-index.md` — **脚本资产完整表**：每个脚本的作用、参数、契约与坑位（§10 的详细版）
- `references/portrait-and-motion.md` — **竖屏与动感**（`--layout portrait` 已在 render / propose / filmcheck 三处入口生效，`film.json` 的 `video` 块与 `visual.size` 都接受两套尺寸）：竖屏已能出片，版式骨架与 `compare`/`process`/`chart` 的构图内部都是竖屏原生（`layoutkit` + `stylekit.LAYOUTS_PORTRAIT` + 分 profile 金帧），剩下的边界是排版节奏未按竖屏复核；动感（亚像素推镜）仍未做，判据在此：竖屏 9:16（LayoutProfile 三步走 + 会被打破的 4 个契约）、亚像素推镜（超采样 vs 仿射平移 + 可量化的验收判据）。想知道“为什么现在还只能横屏/为什么默认不动”，看这一篇

## 10. 脚本资产

> 全部脚本都在技能目录里按路径调用，**不需要拷进工程目录**（零拷贝，见 §1）。
> 这里只列"哪个脚本干什么"；逐脚本的参数、契约、坑位见 `references/script-index.md` 和各自的 `--help`。

| 你要干的事 | 用哪个 |
|---|---|
| 出片前把 8 个问题问完（含制作方式与产线推荐；hybrid 形态用 `--hybrid-form {3,1}` 显式指定，默认 3） | `scripts/intake.py` |
| 出候选 / 冻结设计 / 换方向重出 / 手改设计后 `--adopt` 重绑指纹 | `scripts/propose.py` |
| 排轴 / 渲染 / 混音 / 质检 / 批量 / 断点续跑 | `scripts/render.py`（14 个 stage） |
| 评候选静帧"好不好看"（离线打分 + 可选 VLM） | `scripts/critic.py` |
| 记不住入口时 | `scripts/studio.py <子命令>`（薄门面） |
| 设计 DSL / 预览 / 场景语法 / 声线 / 排版内核 | `designkit.py` `previewkit.py` `grammar.py` `voicekit.py` `filmkit.py` |
| 配乐 / 音效 / 混音原语 | `musicgen.py` `sfxkit.py` `scorekit.py` |
| 门禁账本 / 阶段新鲜度 / 契约 / 渲前预检 | `gates.py` `stagewatch.py` `filmcheck.py` `preflight.py` |
| 帧级 / 全片 / ASR 质检 | `qa_frames.py` `qavmaf.py` `qaasr.py` |
| 浏览器逐帧渲染 | `webengine.py` |
| **素材剪辑一条龙**（下载/转写定位/切镜/混音/字幕/门禁） | `scripts/footage.py`（10 个 stage） |
| 素材线核心：取材 / 剪辑 / 专属门禁（`footageqa.py` 的 F1–F9；`footagekit.source_budget` 素材预算） | `footagekit.py` `footageedit.py` `footageqa.py` |
| **hybrid 形态 3 合成**（素材打底 + 小面积叠加 + 强制响度归一） | `scripts/compose.py` |
| **hybrid 风格对齐**（素材画像 -> 图文段配色/画幅；素材本身不上色） | `scripts/matchstyle.py` |
| 回归自检（105 条断言） | `selfcheck.py` |

内置资产：`assets/scenes.py`（默认渲染器；要改画面就拷成自己的 `scenes.py`）、
`assets/film.json`（测试夹具，不是模板）、`assets/design_fixture.json`（金帧基准）、
`assets/voice_loudness.json`（响度补偿实测表）。

技能自带工具（在 `<SKILL_DIR>/tools/`）：`estimate.py` 开渲前预演、`golden_frames.py` 金帧像素回归、
`silhouette_qa.py` 剪影可区分（S13，离线检查，不进账本）、`audio_similarity.py` 音频相似度、
`footage_selftest.py` 素材线自检、`matchstyle_selftest.py` hybrid 风格对齐自检、
`scenes_hybrid_selftest.py` hybrid 模板渲染自检。

## 11. 验收话术（不要自我宣称"做好了"）

交付时必须能贴出：
1. `ffprobe` 的时长/分辨率/码率
2. `ebur128` 的积分响度与**真峰**（两个数都要，只有响度不算）
3. 编码锐度比（≥0.85）**并说明参考帧来自场景函数重新渲染**
4. 联系表路径 + "已用 read 亲眼确认"的**具体观察**（不是"看起来不错"）
5. 字幕条目数 = 剧本句数（无静默丢失）
6. `python selfcheck.py` 的 PASS 计数
7. **帧级深检的三个数**：单帧闪烁 N 条 / 画面空窗 N 段 / 音画同步超差 N 条
   （有 web 引擎时再加页面层的 文字重叠 / 出画 / 一闪而过 / 从没露面 四项）
8. **新门禁的数**：G10 词级（N 句 / M 词、末词偏差）、G11 的 **VMAF 与 SSIM**（外加用了多少帧）、
   G12 的**字符召回**。这三项若在报告里是 `skipped`，要说明为什么没跑（模块缺失 / web 引擎 / 显式关闭），
   **不能只字不提**。

> **不要口头复述这些数字**：`out/QC-REPORT.md` 是机器生成的交付凭据，
> 照抄报告里的"实测值"即可；报告里的 `skipped` 项必须逐项说明为什么没执行。
>
> 完整示例（本技能自带示例片的实测，可以照这个格式抄；**数字每次跑会差一点**——
> TTS 实测时长、AAC 编码都会让真峰/响度/锐度在小范围内浮动，别 chasing 小数点）：
> ```
> 容器：1920x1080 / 30 fps / h264+aac / 48 kHz / 时长 30.10 s / 1.00 Mbps
> 信号：-16.0 LUFS / 真峰 -1.2 dBTP / 锐度比 1.01 / 黑帧 0 / 字幕 4 条 = 剧本 4 句
> 时间：单帧闪烁 0 / 画面空窗 0 / 音画同步超差 0（本条为 web 引擎或写了 visual_at 时才有意义）
> 词级：4 句 / 64 词 / 末词偏差最大 0.09 s（G10）
> 画质：VMAF 97.22 / SSIM 0.9928 / 870 帧（G11，参考帧由场景函数重渲）
> 回读：字符召回 0.803（G12，faster-whisper small；中英混排专名会掉分，< 0.72 才 fail）
> 自检：selfcheck PASS <实测条数> / FAIL 0 / SKIP 0
> 音频：work/score.json 的 variation 非 0（指纹参与了结构变奏）、audio_similarity --strict 全 PASS
> 目检：work/qa/sheet.png，四帧分别为开场星射线、五步成片卡片、画质三件事卡片、收尾居中大字；
>       字幕黑底白字不压正文，大字边距均匀，无空洞帧。
> ```
>
> 总数别从示例里抄：发版前跑一遍把真实值填进去。真源是 `selfcheck.CASES`（当前 105 条断言）——
> 本收编版只有这一份总数口径，没有第二处要同步。

> 自查口令：如果一条门禁从来没失败过，先确认它是不是根本测不到。

## 12. 参考机器的已验证环境事实

> **这些是作者机器（Windows 11 / Ryzen AI 9 HX 370 / OSGeo4W Python 3.12）的实测基准，不是跨机器承诺。**
> 换机先跑 §13 的 60 秒自检，把结论写回自己的笔记——别照抄这里的版本号与路径。
> 完整表格（ffmpeg 构建、编码速度、各模块耗时、排期公式、三条隐形坑）见
> `references/machine-facts.md`。
## 13. 换机 60 秒自检（比读文档快）

```bash
cd <SKILL_DIR>/scripts
python selfcheck.py                  # 105 条断言；FAIL 就照提示修环境
python sfxkit.py --selftest          # 音效库（可选模块，装了才跑）
python musicgen.py --selftest        # 配乐编排器（参数化）
python scorekit.py --selftest        # 配乐合成原语（不是配乐引擎）
python webengine.py --selftest       # 浏览器引擎（36 项，会真起无头浏览器，约 60 秒）
python qa_frames.py --selftest       # 帧级质检（自己造测试视频）
python <SKILL_DIR>/tools/footage_selftest.py   # 素材剪辑线（离线造素材跑全链 + F1–F9 断言）
```

selfcheck 自己造测试信号、现场调 ffmpeg 编码，**不联网、不依赖任何成片**。
每条 FAIL 都对应一个具体环境缺口（缺字体 / ffmpeg 无 libx264 / 无 numpy / 缺 sfxkit），
报错信息直接写该改哪里。可选模块缺失时 selfcheck 标 `SKIP` 而不是 FAIL——
**别把"没装可选件"和"坏了"混为一谈**。

> **本收编版没有仓库级体检脚本。** 上游的 `tools/ci_check.py` 只查“这个仓库作为可分发包是否完整”
> （语法 / JSON / 依赖白名单 / 打包卫生 / 文档路径引用），与 OpenCode 的收编形态无关，未搬入。
> 这里的可用性判据只有一条：`selfcheck.py` 输出中 FAIL 0、SKIP 0（条数以它自己的输出为准）。
>
> 契约校验的真源仍各有一份：`scripts/filmcheck.py` 校验 `film.json`，`schemas/*.schema.json`
> 描述字段（含 `footage.schema.json`）。需要时直接调：
> `python <SKILL_DIR>/scripts/filmcheck.py film.json`。

