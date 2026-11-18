# 帧级深度质检（qa_frames）

> 成片质检的容器 / 响度 / 黑帧 / 锐度几项只看"能出片"，看不出"画面里有一帧白闪"——本模块补的正是这一层。
> 这份文档讲清楚 **判据、阈值来源、报告字段、怎么调参、以及这套方法的边界**。

实现：`skills/video-studio/scripts/qa_frames.py`（只依赖 stdlib + numpy + Pillow，全部解码走 ffmpeg）。

**改编自 ai-history-film（MIT）**：
- 单帧闪烁扫描：`docs/开发日志.md` 第十四轮（逐帧解码 160×90 → 减去前后帧平均 → 分块统计 → 找"只亮一帧"的格子）；
  阈值经验同样来自该日志：真实异常 10–15，正常运动画面 ≤ 6.4。
- 画面空窗扫描：第五轮（逐帧亮度扫出 ≥ 1 s 近黑片段，保留片头/片尾等刻意黑）。
- 音画同步对照：`video/qa_sync.js`（画面视觉事件 vs 音频 cue）。
- 响度曲线：`video/audio/inspect_mix.py` 的短时 RMS 思路（matplotlib 换成 PIL 手绘）。
- 第二十九/三十轮经验：**按块统计就够，不需要 SSIM**（SSIM 对"局部只闪一帧"不敏感，还更慢）。

---

## 0. 三层质检里的位置

| 层 | 工具 | 看什么 | 看不出什么 |
|---|---|---|---|
| ① 容器/母带层 | `filmkit.qa_report()`（probe / loudness / detect_black / encode_quality / 字幕条数） | 分辨率、帧率、音轨、时长、响度/真峰、长黑帧、锐度比、字幕条数 | 画面内部的一帧白闪/空窗 |
| ② 帧级/时间轴层 | **本模块** `qa_frames.report()`（`render.py` 的 `qa` stage 自动调用） | 单帧闪烁、画面空窗、音画同步、亮度/响度曲线 | 字形/语义（文字压字、出画） |
| ③ 文字/目检层 | `webengine.qa_text()` + `read` 静帧/联系表 | 文字重叠、出画、一闪而过、从没露面；构图、元素存在性 | 逐帧时间轴上的瞬时异常（抽帧碰运气） |

② 的独特价值是 **"时间轴 × 局部空间"**：160×90 全片逐帧过一遍，比抽 6 张静帧可靠得多；
但它只看亮度统计。**三层互补，不能互相替代**——只做 ① 会漏掉"一帧白闪"，只做 ③ 会漏掉"两秒空窗"。

---

## 1. 快速开始

```bash
# 深检一份成片，写报告 JSON + 诊断 PNG
python scripts/qa_frames.py out/film.mp4 \
    --cues work/cues.json \
    --out work/qa/deep.json \
    --png work/qa/deep.png \
    --expect-duration 389.0

# 自检（合成语料，不联网，约 5 s，22 项 PASS/FAIL）
python scripts/qa_frames.py --selftest
```

退出码：`0` = 无 error；`1` = 有 error 或运行失败。`report()` 也返回同一份 dict，可供脚本直接判断。

`cues.json` 用 CONTRACT §4.2 的格式：

```jsonc
{"version": 1, "duration": 389.0, "fps": 30, "width": 1920, "height": 1080,
 "cues":   [{"t": 12.5, "type": "impact", "gain": 0.0, "dur": 1.2}],
 "events": [{"t": 12.5, "kind": "flash", "a": 0.8}]}      // 可选：页面导出的视觉事件
```

`report()` 的 `cues_json=` 既可以传路径，也可以直接传已载入的 dict。

---

## 2. API（签名冻结，见 CONTRACT §5.4）

```python
sample_frames(path, w=160, h=90, from_s=0.0, to_s=None) -> np.ndarray  # (n,h,w) float32 0..1
flicker_scan(path, *, thresh=6.0, block=8) -> list[dict]               # [{"t","score","bbox"}]
black_windows(path, *, luma_max=0.045, min_dur=0.8, head=1.2, tail=1.2) -> list[dict]
luma_curve(path, fps=None) -> list[dict]                               # [{"t","v"}]
loudness_curve(audio_path, win=0.1) -> list[dict]                      # [{"t","lufs"}]
sync_check(cues: list, events: list, tol=0.06) -> list[dict]
report(film, *, cues_json=None, expect_duration=None, out_json=None, out_png=None) -> dict
selftest() -> int
```

### 2.1 `sample_frames` / 解码约定

- 命令固定为 `ffmpeg … -vf scale=W:H:flags=area -f rawvideo -pix_fmt gray`，**一个进程流式读完**，
  不逐帧开进程、不写临时帧文件。
- 返回 `(n, h, w)` float32，0.0–1.0（灰度 / 255）。`from_s/to_s` 用输入侧 `-ss` + `-t`，
  取到的时间窗内逐帧解码。
- ffmpeg 路径解析顺序：`FILMKIT_FFMPEG` 环境变量 → `filmkit.FFMPEG` → `shutil.which("ffmpeg")`。
- 注意**长片内存**：389 s @30fps ≈ 11670 帧，float32 约 670 MB。只想看曲线/闪烁/空窗时用
  `luma_curve / flicker_scan / black_windows / report`——内部是流式的，只保留每帧均值与前后 2 帧。

### 2.2 `flicker_scan` — 单帧闪烁

**第一步：逐帧异常度**（在 160×90 采样空间，单位 0–255 灰度）：

```
d[t] = |F[t] - (F[t-1] + F[t+1]) / 2|          # 二阶差分：只闪一帧的像素会差得多
s[t] = max over 8x8 blocks of mean(d[t])        # 分块取最大均值（块内取均值抗单像素噪声）
```

块切分用 `reduceat`，160×90 在 `block=8` 下是 20×12 块（最后一行/列不足 8 的按实际像素数平均），
**bbox 是 160×90 采样坐标** `(x0, y0, x1, y1)`，不用换算回 1080p。

**第二步：孤立性**（相对比值，`FLICKER_ISO = 1.5`）

`t` 取 1..n-2，只有一条：`s[t] >= thresh` 且 `s[t] >= FLICKER_ISO * max(s[t-1], s[t+1])`。

单帧事件的分差会被前后帧各"看到一半"：全白单帧闪中间帧 `s≈178`、前后帧 `s≈89`，比值 **≈2.0**；
连续运动/硬切三帧都在变，比值 **≈1.0**。**不能要求"邻居一定低于阈值"**——全白闪的邻居也有 89；
反过来，连续小元素运动在阈值附近波动时，偶尔会"两侧恰好低于阈值"造成假孤立
（见下面的 16.933 s 案例：6.48 vs 5.66/3.52，比值只有 1.15）。

实测正常运动画面 `s <= 6.4`，真实异常 `s = 10–15`，所以阈值默认 `6.0`。

> **不要加回"严格孤立"**（要求 `s[t-1] < thresh` 且 `s[t+1] < thresh`）：它只会制造假阳性
> （16.933 s 案例就是两侧恰好低谷），判据只保留比值。

**第三步：必须"回到原位"**（`FLICKER_RETURN_RATIO = 1.6`）：

```
r[t] = |F[t+1] - F[t-1]|      # 同一块上的"前后两帧彼此差多少"
条件：r[t] <= FLICKER_RETURN_RATIO * s[t]
```

**为什么必须有这一步**：二阶差分 `d[t]` 分不清两件完全不同的事——

| | 单帧闪烁（真 bug） | 加速运动（正常画面） |
|---|---|---|
| 机理 | 只有 t 这一帧变了，t±1 都正常 | 三帧都在变化，且 t 恰好是变化最快的一帧 |
| `s[t]` | 大 | 大 |
| `r[t]` | **≈ 0**（前后两帧几乎相同） | **≥ 2·s**（前后两帧差得更多） |
| 判定 | 记 error | 跳过 |

没有这道闸时，**每一段"缓动入场 / 逐字入场 / 转场黑场起坡"的最快帧都会被报成闪烁**。
实测反例（本技能自带的 `assets/web/` 示例片）：12 s 的片子里报了 3 处——

| 时刻 | score | 真相 |
|---|---|---|
| 0.733 s | 9.70 | 逐字入场到**最后一个字**的那一帧（alpha 0.86 → 0.98） |
| 3.700 s | 7.80 | 4 s 场景交界黑场 `dip = clamp(1 - \|t-4.0\|/0.3)` 的**起坡帧** |
| 7.700 s | 8.16 | 同上，8 s 交界 |

逐一逐帧渲染核对（不只是看分数）确认都是加速运动；加闸后该片 flicker 归零，
而 `--selftest` 里注入的单帧白闪**仍然被检出**——门禁没有被削弱。

**这道闸的已知盲区（必须知道）**：快速运镜/大幅位移的段落里，如果"前后两帧因**运动**产生的差异"
大于 1.6 × 闪烁幅度，真实的单帧闪会被放过。所以：

- `score >= FLICKER_SUSPECT_SCORE`（10.0）却被这道闸拒绝的候选，**不进 error、但进 warning**
  （文案带"高分异常被判为运动（建议人工复核）"+ 时刻/分数/return）；
- 命令行摘要会多打一段"高分异常待复核 N"。

**原则：门禁可以保守，但不能静默。** 被判成运动的高分候选必须留痕，
否则下一次"一帧白闪没人发现"就发生在这里。

**排除设计好的重击**：`report()` 会把 cue 文件里 `kind` 属于
`flash / shake / ring / impact / …`（见模块常量 `VISUAL_EVENT_KINDS`）的时刻当作"设计视觉事件"，
闪烁时刻与它们相差 ≤ `FLICKER_EXPLAIN_WIN`（0.2 s）时标 `"expected": true`，
**只记 warning、不记 error**。`flicker_scan()` 本身不做这一步（它只看画面）。

**设计事件要主动登记**：交界黑场、转场闪白这类"设计上就会亮一下"的时刻，
页面用 `S.event(t, 'flash')`、PIL 路线写进 `cues.json` 的 `events`——
否则它们会被当成 bug（或者被迫调高阈值，把真 bug 一起放过）。

### 2.3 `black_windows` — 画面空窗

- 逐帧平均亮度 `v[t] < luma_max`（默认 0.045，约 11/255）视为近黑；连续区间 ≥ `min_dur`（0.8 s）记一段。
- **片头 `head` 秒、片尾 `tail` 秒内不算**（刻意淡入淡出）；窗口与保护带重叠的部分会被裁掉，
  裁剪后仍 ≥ `min_dur` 才报。例如片头 0–0.8 s 纯黑：`t1=0.8 < head=1.2`，整段被裁掉 → 不误报。
- 短片兜底：若总时长 ≤ `head + tail + min_dur`，首尾保护带按总时长的 10% 缩小（否则短测试片会被全部裁掉）。
- 返回 `[{"t0","t1"}]`（秒，3 位小数）。

> 阈值来源：上游第五轮用"成片逐帧亮度扫出 ≥1 s 近黑片段"抓空窗，并人工确认保留的黑（片头、心跳光点、片尾）。
> 本模块不做"是否刻意"的判断，**长黑一律进 error**；设计上有意留黑时请把该段控制在 0.8 s 以内，或排查原因。

### 2.4 `luma_curve` / `loudness_curve`

- `luma_curve(path, fps=None)`：每帧 160×90 平均亮度 `[{"t","v"}]`，`v ∈ 0..1`，点距 1/fps。
  用来在 PNG 上看"亮度是不是突然掉下去/卡帧"。
- `loudness_curve(audio_path, win=0.1)`：ffmpeg 解码成 48 kHz **单声道** f32le → 每 `win` 秒算 RMS →
  `dBFS = 20*log10(max(rms, 1e-6))` → `lufs ≈ dBFS - 0.691`。

  ⚠ **这是近似 LUFS，只能看相对结构**：没有做 BS.1770 的 K 加权，也没有绝对门限；单声道下混会改变
  立体声能量，低频偏重的音乐可能偏低 1–3 LU，数字静音被钳在 -120 LUFS。
  **绝对响度是否合规（-16 LUFS ±、真峰 ≤ -1 dBTP）以 `filmkit.loudness()`（ffmpeg ebur128）为门禁**，
  本曲线只回答"哪段掉下去了、重击比环境高多少、空窗处声音是不是也停了"。

### 2.4b `_static_warnings` — 画面长时间静止

判据（**不要用**"全帧平均亮度 3 s 内波动 < 0.002"这类公式）：

```
drift[t] = max over 8x8 blocks of mean |F[t] - F[t-lag]| * 255     (lag = FLAT_LAG_S = 0.5 s)
连续 FLAT_MIN_DUR = 6.0 s 内每一帧都 drift <= FLAT_DRIFT_EPS = 1.5  →  WARN
```

物理含义：**0.5 秒内，画面里最活跃的那块 8×8 区域（占全屏 0.53%）平均变化不到 1.5 级灰度**——
人眼看到的就是一张静止的图。用"最活跃块"而不是"全帧均值"，是因为逐字入场、数字滚动、卡片切换
只占全屏几个百分点，全帧均值几乎不动，而观众显然看得到它们在动。

为什么不用全帧均值：它对局部动画天生不敏感。自带示例片（30.10 s）实测误报 **4 段**
（411/814 个滑动窗口命中），而画面一直在动。现判据在同一片上 **0 段**。

实测分布（160×90 解码，H.264）：

| 内容 | drift 量级 |
|---|---|
| 逐字节冻结 | 0–1.5（只剩编码噪声） |
| 300 帧缓慢渐变 | 约 6 |
| 逐字入场 / 呼吸光 / 粒子 | 20–108 |
| 场景切换 / 白闪 | 40–105 |

阈值怎么调：漏报卡帧 → 调小 `FLAT_MIN_DUR` 或 `FLAT_DRIFT_EPS`；设计上确实要静止 >6 s →
按 SKILL.md「每 3–6 秒要有视觉变化」改画面节奏，而不是把阈值调大。

它只出 **warning**（最多 6 条），与"画面空窗"（近黑 ≥0.8 s，出 error）是两回事：
静止不等于黑，可能是刻意定版，所以不阻断交付。

### 2.5 `sync_check` — 音画同步

对每个 cue（`{"t","type",…}`）找对应视觉事件（`{"t","kind",…}`）：

1. **同 kind 优先**：`kind == type` 的事件里取时间最近者，搜索窗 2 s；
2. 否则**时间最近**：任意事件里取最近者，**但只有 `|dt| <= tol` 才认可**——用来佐证"确实同步了"；
3. 都找不到 → 视为"这个 cue 没有对应画面事件"，**不判超差**。

第 2 条为什么只认 `|dt| <= tol` 的最近事件：画面事件和音频 cue 本来就不是一一对应。
实测反例：4.100 s 的 `data_chirp` 被 3.700 s 的"交界黑场起坡"事件配对，报 `dt = -0.400 s` 超差；
8.020 s 的 `reverse_swell` 被 7.700 s 的事件配成 `-0.320 s`。两者都是**假 FAIL**——
跨 kind 的"最近事件"根本不是"预期事件"。
收紧后两个演示片 sync 超差归零，而同 kind 的真实错位（例如 impact 画面在 12.5 s、音效在 12.9 s）仍然会报。

配对成功后 `dt = event_t - cue_t`；`|dt| > tol`（默认 0.06 s）记一条：
`[{"cue","cue_t","event_t","dt"}]`（秒，3 位小数；`dt` 为负表示声音比画面晚）。

⚠ **命名约定决定了这个门禁会不会失效**（最容易被忽略的一条）：

同 kind 判超差的前提是 **`events.kind` 与 cue 的配方名同名**。如果页面只导出
`flash / shake / ring` 而 cue 是 `impact / whoosh`，同 kind 永远配不上，跨 kind 又只做确认，
结果就是 **`sync` 恒为空、检查静默全过**——看起来"零超差"，其实是"一条都没在查"。

#### PIL 路线的两条补救（否则这条门禁不会真的在查）

`--engine pil` 根本没有页面、没有 `events`，所以上面的配对必然配不上。两条补救：

1. **cue 自带画面锚点**：给 cue 加 `"visual_at"`（或 `"visual_t"`）字段，值是画面上该事件
   **真正发生的绝对秒数**。`sync_check` 会直接算 `dt = visual_at - cue.t` 并判超差，
   不依赖 `events`、也不依赖命名约定：

   ```json
   {"at": "l03+0.05", "type": "impact", "visual_at": 15.77}
   ```

   锚点必须写画面上的真实时刻。**随手填一个等于自己骗自己**，而且骗得比没有门禁更彻底。

2. **把"没查"升级为失败**：`QA_REQUIRE_SYNC=1`（或 `qa_frames.py --require-sync`）会把
   "`sync` 一条都没判"从 warning 升级为 error。`render.py` 的 `qa` stage 调 `report()` 时
   自动继承该环境变量，所以 CI 里设一次就够。

`deep.json` 里现在还有一份 `sync_info`（`anchors / same / judged / blockers`），用来看
"到底判了几条"。**`sync: []` + `judged: 0` 就是"没在查"**，不要读成"零超差"。

两条路任选：

1. **推荐**：`getEvents()` 里额外登记一条用配方名做 kind 的事件
   （`S.event(t, 'impact')` / `S.event(t, 'whoosh')`），与 `S.cue` 成对出现；
2. 下一版：`events` 每项支持可选 `"cue": "impact"` 字段，配对时优先按它匹配。

自查方法：故意把一个 cue 的时刻改错 0.3 s，跑一遍 `sync_check`——**如果它不报错，说明配对没生效**。

### 2.6 `report` — 一次扫描出报告

- 顶层字段固定为 §4.5 的 7 个键：
  `flicker / black_windows / luma / loudness / sync / errors / warnings`。
- `flicker` 每项：`{"t","score","bbox","ratio","median","return"}`，再**额外带 `"expected": bool`**
  （`expected` 只有本报告加，`flicker_scan` 不返回）：

| 字段 | 含义 |
|---|---|
| `t` | 秒（3 位小数） |
| `score` | 二阶差分块最大均值（0–255 灰度） |
| `bbox` | 160×90 采样坐标 `(x0,y0,x1,y1)` |
| `ratio` | 孤立比值 `score / max(前,后)`（≥1.5 才入列，留作复核线索） |
| `median` | 异常块内差异的**中值**：真单帧闪整块一起变（中值≈score，实测 143–199）；小元素漂移/加速运动只有少数像素变（中值远小于 score，16.933 s 案例是 1.0） |
| `return` | 前后两帧彼此差异（"回到原位"闸的原始量） |
| `expected` | 与已登记闪白/震屏/冲击环重合 → true，只记 warning |

- `black_windows` 每项 `{"t0","t1"}`。
- `errors` 分档（每条都是人话，可直接贴给用户）：

| error 类型 | 触发条件 |
|---|---|
| 单帧闪烁 | `flicker` 里 `expected=false` 的项 |
| 画面空窗 | `black_windows` 非空 |
| 音画同步超差 | `sync` 非空 |
| 时长偏差 | 传了 `expect_duration` 且 \|实际 - 预期\| > 0.5 s |

- `warnings`：设计事件解释掉的闪烁（一条汇总）、**高分异常被判为运动（建议人工复核）**、
  **"`音画同步检查可能没生效`"（cues 与 events 没有任何同名 type/kind 时）**、
  无音轨、`cues.json` 缺失/解析失败/无 events、响度曲线生成失败、
  亮度长时间无变化（`FLAT_MIN_DUR=3 s` 内波动 < `FLAT_EPS=0.002`，最多 6 条）。
- `out_json`：UTF-8、`indent=1`、`ensure_ascii=False`、禁止 NaN（`allow_nan=False`，
  任何非有限值都会在这里暴露而不是静默写 `NaN`）。
- `out_png`：1600×830 的 PIL 手绘图，见下节。

### 怎么读 `deep.json`（交付前 60 秒）

| 字段 | 含义 | 怎么用 |
|---|---|---|
| `flicker` | 单帧闪烁列表 | 非空且 `expected=false` → **必须查**；`expected=true` 是设计事件 |
| `black_windows` | 近黑连续段 `[{"t0","t1"}]` | 非空 → 看成片对应时间是否卡帧/漏画 |
| `luma` | 逐帧平均亮度 | 曲线突然掉下去 / 长时间平直（卡帧） |
| `loudness` | 0.1 s 窗近似 LUFS | 空窗处声音是否也停、重击落差够不够 |
| `sync` | cue↔event 超差 | 非空 → 查对应 cue 与事件 |
| `errors` / `warnings` | 人话分档 | **errors 必须清零才交付**；warnings 逐条确认 |

### 手工复核命令（怀疑有闪烁时照这个走）

```bash
# 1) 先看摘要
python -c "import json;d=json.load(open('work/qa/deep.json',encoding='utf-8'));print(d['flicker']);print(d['errors'])"

# 2) 抽 t 前后各几帧对比（ffmpeg 9 用 -fps_mode passthrough；-vsync 已移除）
ffmpeg -ss 16.80 -i out/film.mp4 -frames:v 8 -fps_mode passthrough qa/flick_%02d.png

# 3) 换两种缩放复核：数字**不能跨滤波器比较**，只用来判断"是否存在单帧尖峰"
python - <<'PY'
import subprocess, numpy as np
def scores(flags):
    p = subprocess.run(["ffmpeg","-v","error","-ss","16.7","-i","out/film.mp4",
        "-frames:v","12","-vf","scale=160:90:flags="+flags,
        "-f","rawvideo","-pix_fmt","gray","pipe:1"], capture_output=True)
    a = np.frombuffer(p.stdout, np.uint8).astype(np.float32).reshape(-1,90,160)
    d = np.abs(a[1:-1] - (a[:-2]+a[2:])/2)
    return [round(float(v),2) for v in d.max(axis=(1,2))]
print("area   ", scores("area"))
print("lanczos", scores("lanczos"))
PY

# 4) 复核规模：真闪是"整块/整片"，小元素只占全屏 <1%（对照 median 字段）
```

⚠ **不要拿 LANCZOS 的分数去否定 area 的分数**：同一批帧 area 6.48 / bicubic 3.49 / LANCZOS 3.32 /
nearest 26.5——差异全在高频小元素上，跨滤波器比较会得出完全相反的结论。

---

## 3. 诊断 PNG 怎么看

```
1600 x 830，三个面板共享同一条时间轴（竖网格对齐，同一时刻的亮度/响度/事件一眼对上）：

  面板 1  亮度曲线（蓝线，0–1）
          红色半透明块 = 画面空窗；竖线 = 单帧闪烁（红=异常，橙=与设计事件重合）；顶部红/橙三角+秒数
  面板 2  响度曲线（橙线，近似 LUFS，自动量程）
          同一批空窗阴影；闪烁竖线贯通到这一层，方便判断"闪的时候声音在干嘛"
  面板 3  事件带：单帧闪烁 / 画面空窗 / 同步超差（紫色，标 dt=+0.35s）/
          [设计事件]（绿，来自 cues.json events，标 whoosh/flash/…）
          顶部徽章 PASS / FAIL ×N
```

标的都是**问题发生的位置**：空窗区会同时把亮度、响度压成一块红色；同步超差会同时标出 cue 时刻与
`dt` 数值；设计事件为绿色，和红色闪烁点对齐时就是"这是故意的重击"，不对齐时一眼就能看出来。

---

## 4. 可调参数（模块级常量）

`report()` 的签名是冻结的，阈值调不了——需要非常规阈值时请直接调 `flicker_scan/black_windows/sync_check`，
或按需改这些模块常量（都有中文注释）：

| 常量 | 默认 | 含义 |
|---|---|---|
| `FLICKER_THRESH` | 6.0 | 单帧异常度阈值（0–255 灰度单位） |
| `FLICKER_BLOCK` | 8 | 分块大小（8×8） |
| `FLICKER_ISO` | 1.5 | 相对孤立比值 |
| `FLICKER_RETURN_RATIO` | 1.6 | "回到原位"闸门：前后两帧差异必须 ≤ 1.6×中间帧偏离量（区分闪烁与加速运动） |
| `FLICKER_EXPLAIN_WIN` | 0.2 s | 与设计视觉事件的重合窗 |
| `VISUAL_EVENT_KINDS` | flash/shake/ring/… | 哪些 event.kind 算"设计视觉事件" |
| `BLACK_LUMA_MAX` | 0.045 | 近黑亮度上限（0–1） |
| `BLACK_MIN_DUR` | 0.8 s | 空窗最短时长 |
| `HEAD_SKIP` / `TAIL_SKIP` | 1.2 s | 片头/片尾保护带 |
| `SYNC_TOL` | 0.06 s | 音画同步容差 |
| `FLICKER_SUSPECT_SCORE` | 10.0 | 高分候选阈值：被"回到原位"闸拒绝且分数 ≥ 它 → 转 warning（保留复核线索，防静默漏检） |
| `SYNC_SAME_WIN` | 2.0 s | 同 kind 配对搜索窗（`SYNC_NEAR_WIN` 保留给下一版显式跨 kind 配对，当前只作兼容位） |
| `FLAT_MIN_DUR` / `FLAT_EPS` | 3.0 s / 0.002 | 亮度长时间无变化告警 |

调阈值前先跑一遍自检，确认没有把正常运动画面（`s ≤ 6.4`）判成闪烁。

---

## 5. 与 render.py 集成

`render.py` 的 `qa` stage 会 `import qa_frames` 并调用（已有 `_qa_deep` 兜底，缺模块/抛异常都不阻断成片）：

```python
deep = qf.report(film, cues_json="work/cues.json", expect_duration=tl["total"],
                 out_json="work/qa/deep.json", out_png="work/qa/deep.png")
```

`render.py` 把 `deep["errors"]` 并入最终 failures、`deep["warnings"]` 打 `[WARN][deep]`，
并打印"闪烁 N / 空窗 N / 同步超差 N"。接口只依赖上面 7 个键，字段内部可扩展（如 `expected`）。

---

## 6. 自检（`--selftest`，22 项，约 5 s，不联网）

合成语料完全由本模块用 numpy + ffmpeg 现场生成（rawvideo 管道 → libx264 + AAC），不依赖仓库素材：

- `clean.mp4`：4 s 平滑运动画面（正常运动不应误报）；
- `injected.mp4`：8 s，片头 0.8 s 纯黑 + 2.0–3.5 s 注入黑窗 + 5.5 s 注入**单帧全白闪**，
  音轨为 440 Hz 正弦且 1.5–2.5 s 数字静音；
- `cues_plain.json` / `cues_flash.json`：3 个 cue/event，含一处 +0.35 s 同步超差；flash 版多一个
  `{"t":5.5,"kind":"flash"}` 用来验证"设计事件不记 error"。

断言覆盖（22 项）：ffmpeg 可用、`sample_frames` 形状/类型/范围、`from_s/to_s` 时间窗确实是黑帧、
干净画面 0 误报（闪烁+空窗）、注入白闪被检出（时刻/强度/bbox 合法）、1.5 s 黑窗被检出且片头 0.8 s 黑不误报、
亮度曲线暗场/正常段、响度曲线能测到静音谷（<-45 LUFS）、`sync_check` 只报超差、
**孤立性判据回归**（连续小元素运动不报 / 单帧尖峰要报 / 硬切不报 / 加速运动放过 /
高分运动候选进 `suppressed` 提醒复核）、**音画同步命名约定守卫**（events 与 cue 全不同名时要给 warning）、
`report` 三档 error 齐全且 JSON 可序列化、设计事件被排除、预期时长校验、JSON+PNG 落盘可读、
CLI 端到端（有 error 时 exit 1）、`import` 无打印无副作用。

---

## 7. 实测样例（合成演示片）

`demo_film.mp4`：16 s / 30 fps / 480×270，8 段不同亮度的运动画面，6.0 s 设计闪白（events 有 flash）、
9.0 s 未登记单帧白闪、12.0–13.2 s 画面空窗、音轨分段电平 + 120 BPM 底鼓 + 6.0 s 重击，
cues 里 whoosh 比视觉事件晚 0.35 s。实测输出：

```
闪烁 2（其中与已登记视觉事件重合 1） / 空窗 1 / 同步超差 1
[FAIL] 单帧闪烁 t=9.000s score=142.9（孤立比值 2.00，块中值 143.00，异常块 56,72–64,80，阈值 6.0）
[FAIL] 画面空窗 12.00–13.20s（持续 1.20s 近黑，阈值 0.045，片头/片尾 1.2s 内不计）
[FAIL] 音画同步超差：cue 'whoosh' @ 4.000s 与视觉事件 @ 4.350s（dt=+0.350s，容差 ±0.060s）
[WARN] 已排除 1 处与已登记视觉事件（闪白/震屏/冲击环）重合的单帧闪烁；见 flicker[*].expected
```

JSON 里 `flicker` 两项：

```json
[{"t":6.0,"score":154.0,"bbox":[88,88,96,90],"ratio":1.65,"median":154.0,"expected":true},
 {"t":9.0,"score":142.9,"bbox":[56,72,64,80],"ratio":2.0,"median":143.0,"expected":false}]
```

`black_windows=[{"t0":12.0,"t1":13.2}]`、`sync=[{"cue":"whoosh","cue_t":4.0,"event_t":4.35,"dt":0.35}]`、
`luma` 480 点（30 fps × 16 s）、`loudness` 160 点（0.1 s 窗）。PNG 见 `work/qa/deep.png`。

**注意 `median` 的用法**：两项的 score 都是 140+，但 6.0 s 那项 `median=154`（整块一起变亮 = 真闪白），
9.0 s 那项 `median=143`（同样是整块变）。而 16.933 s 那个**误报**案例是 score 6.48 / `median` **1.0**
（只有少数像素在动）——**score 高不高不说明问题，median 才说明"整块还是一小撮"**。

---

## 8. 已知限制

1. **按 CFR 处理**：`t = 帧号 / fps`，fps 取 ffprobe 的 `r_frame_rate/avg_frame_rate`。VFR 素材会有时间漂移，
   本项目成片固定 30 fps，不受影响。
2. **160×90 会丢小目标**：1080p 上 1–2 px 的单帧坏点被 area 缩放下采样后可能低于阈值。这是上游验证过的
   取舍——它抓的是"看得见的闪"（整块/整片亮度突变），不是压缩坏点。要抓更小的瑕疵请对原始分辨率分块扫描。
3. **强制 16:9 采样**：非 16:9 素材会被拉伸到 160×90（判据看亮度变化，形状失真不影响；但 bbox 不再是等比坐标）。
4. **响度是相对值**：见 2.4，绝对合规仍走 `filmkit.loudness()`。
5. **设计事件依赖 cues.json**：没有 `events` 时所有闪烁都会报 error（自检里 `cues_plain` 正是这种情况）。
6. **JSON 体积**：`luma` 是逐帧的（389 s @30fps ≈ 330 KB），足够小，但别把它当逐帧视频元数据滥用。
7. **暗色主题片贴近阈值**：整片平均亮度 0.04–0.06 时离 `BLACK_LUMA_MAX=0.045` 很近，容易误报空窗。
   先看 `luma` 基线，误报就把 `luma_max` 调低（如 0.03）——真空窗是"比基线明显更低"，不是绝对值。
8. **`returns` 闸的边界**：快速运镜段落里，真实单帧闪若"前后帧运动差 > 1.6 × 闪烁幅度"会被放过。
   高分候选（≥10）会转 warning 提醒复核，但**它终究不是 error**——大片快速运动 + 慎用闪白的片子请人工过一遍。
9. **同步检查依赖命名约定**：`events.kind` 与 cue 配方名不同名时，同 kind 配不上、跨 kind 只做确认，
   `sync` 会**静默全过**（见 §2.5）。改完事件登记方式后，用"故意错 0.3 s"做一次反证。

---

## 9. 案例：16.933 s「小元素动画」误报

PIL 模板演示片（30 s / 1080p30）报过 `t=16.933s score=6.61`，bbox `96,40–104,48`。
逐层复现（160×90、8×8 块、area 采样）后，数据是这样的：

| 帧号（t） | 504 (16.800) | 505 (16.833) | 506 (16.867) | 507 (16.900) | 508 (16.933) | 509 (16.967) | 510 (17.000) |
|---|---|---|---|---|---|---|---|
| 源帧（未编码，area） | 9.59 | 10.27 | 6.57 | 5.59 | **6.52** | 3.59 | 4.78 |
| 成片（area） | 9.53 | 10.45 | 7.03 | 5.66 | **6.48** | 3.52 | 5.00 |

三条结论（每一条都修正了一个错误假设）：

1. **不是编码噪声**：源帧 6.52 vs 成片 6.48，编码前后几乎一样。
2. **不是 LANCZOS 细笔画抖动**：本模块用 ffmpeg `flags=area`（盒式平均），根本不用 LANCZOS。
   而且同一批帧换缩放算法，分数差一个数量级（area 6.48 / bicubic 3.49 / LANCZOS 3.32 / nearest 26.5）——
   **数字不能跨滤波器比较**。
3. **真相是"小元素在动"**：卡片上一个绿色光点在漂移（全分辨率二阶差分 > 30 的像素只占全屏 0.33%），
   504–510 帧连续都在动；508 只是恰好被两侧 507/509 的低谷夹住，`ratio = 6.48 / max(5.66,3.52) = 1.15 < 1.5`。

**所以判据不要求"邻居低于阈值"**：那条严格路径只会制造这种假阳性，只保留比值判据；
另加"回到原位"闸（§2.2）处理加速运动。
复核这类案例最快的两个线索：**`median`（这里是 1.0，真白闪是 143–199）** 和
**变化像素占全屏的比例（这里是 0.33%）**。

---

## 10. 排错

| 现象 | 原因/处理 |
|---|---|
| `找不到 ffmpeg` | 设 `FILMKIT_FFMPEG` 或安装 ffmpeg；Windows 上 `filmkit._find_ffmpeg` 会找 WinGet Links |
| `音频解码失败`（warning） | 成片没有音轨或音轨编码异常；帧级判据不受影响 |
| 大量闪烁误报 | 先看是不是**加速运动**（逐字入场、缓动推近、转场起坡）：`r[t]` 会明显大于 `s[t]`，应确认 `FLICKER_RETURN_RATIO` 生效；再确认设计事件有没有登记 |
| 仍误报 | 画面本身有高频噪点/压缩闪烁（例如整片抖动的胶片效果）→ 调高 `FLICKER_THRESH` 或 `FLICKER_ISO`，也可在 cues.json 里把对应时刻登记成设计事件 |
| 反过来：真闪烁被漏掉 | 检查 `FLICKER_RETURN_RATIO` 是否被调得过大（>2 会让"闪一帧后紧接着又变"的情况漏检）；注入单帧白闪的 selftest 就是这条闸的守门测试 |
| 空窗漏报 | 近黑但没到 0.045（例如深灰 0.06）→ 调高 `BLACK_LUMA_MAX`；或时长不足 0.8 s |
| 同步全不报 | ① `cues.json` 没有 `events`（页面未导出）；② **events.kind 与 cue 配方名不同名**（最常见，且不会任何报错）；③ cue 与事件时间差 > 同 kind 搜索窗 2 s |
| 想确认同步检查真的在工作 | 故意把某个 cue 的时刻改错 0.3 s 重跑，**必须**报出超差；不报就是没配上（见上一行） |

## 11. 取景抖动 —— 单帧闪烁判据抓不到的一类

**现象**：成片看起来"一直在抖"或"一直在慢慢动"，但 `flicker_scan` 报 0、`ssim`/`vmaf` 都很好。
用户的一句话往往是"生成的视频有明显的抖动感，这是为啥？"

**根因有两类，症状一样、修法不同**：

1. **底图重采样错位**：底图把颗粒（`_grain_layer`）和材质一起烘进了 1920×1080 的静态图层，
   逐帧再用 `Image.EXTENT + NEAREST` 按缩放比 `s` 取景。NEAREST 的采样网格随 `s` 整像素跳变，
   颗粒图案跟着**整体错位 1px**——"满屏噪点在沸腾"。
2. **前景跟着相机做慢速亚像素位移**：`_View.x/y` 是 `int(round())`，而镜头推 4% 时前景总位移
   只有 7px，摊到 8 秒 = 0.03px/帧 —— 取整后变成「每 5 帧整体跳 1px」。实测 push=0.14 的 4 秒段
   里文字锚点每秒跳 12 次，一次跳跃 5000+ 个像素差 >40 灰阶；push=0.02 也有 0.67 次/秒。

两类都是**位移**型瑕疵，不是亮度型，所以按"前后帧平均亮度差"定义的 `flicker_scan` 对它们完全不敏感。

**探测方法**（不用整片，10 秒就够）：

```python
import numpy as np
from PIL import Image
# 同一主题，连续取 s = 1.0 -> 1+push 的 20~40 帧
frames = [mod._bg_plate(th, 1.0 + th["push"] * i / 39.0) for i in range(40)]
worst = max(int(np.abs(np.asarray(a, np.int16) - np.asarray(b, np.int16)).max())
            for a, b in zip(frames, frames[1:]))
print(worst)    # NEAREST 直采 4~6 灰阶（可见沸腾）；端点预烘 + blend 后 0~1
```

**修法**（两类各一条，缺一条就只治好一半）：

1. **底图**：不用 NEAREST 直采缩放（BILINEAR/BICUBIC 整帧重采样 29/64 ms，超单帧预算）。
   改成**端点预烘 + 逐帧 blend**：`s=1.0` 与 `s=1+push` 各烘一张 BILINEAR 底版（每张约 60ms，
   只做一次），逐帧用 `Image.blend(lo, hi, (s-1)/push)` 插值（约 4.4 ms/帧）。
   端点内部是平滑重采样，没有网格跳变；颗粒质感保留。上限 `1+push` 是精确边界
   （`_push_scale` 恒 <= 1+push），超出就钳到端点上，不做外推。
2. **前景**：**不要让前景跟着相机走**。`_view()` 现在恒返回 `_View(1.0)`，文字/卡片按整像素
   落位、静止；只有底版随 `push` 缩放。默认设计另有 `push=0`（`designkit._PUSHES = (0.0,)`），
   于是段落中段是完全静止的帧。想整幅推镜，在自己写的 `scenes.py` 里做超采样，别把慢速位移
   塞回 `_view`——1x 光栅上它一定会碎成 1px 台阶。

**护栏**：`selfcheck` 第 68 条（`t_zoom_stability`）断言底版相邻帧最大差 <= 2 灰阶；
第 72 条（`t_foreground_pinned`）断言 `_view` 与 `s` 无关、`push=0` 时缩放恒 1.0、
标题带连续 30 帧逐字节一致。改回 NEAREST 直采或让前景再跟相机，这两条立刻 fail。

