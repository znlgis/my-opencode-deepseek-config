# 渲染前预检 / 全片 VMAF / ASR 回读三道门禁

> 本页只讲这三个**模块**（`G0` / `G11` / `G12`）。账本里还有第四行 `G10` 词级时间戳，
> 它的判据与关闭方式在 [word-timeline.md](word-timeline.md)。

> 三个模块、三道账本门禁，都是**可选**的（模块不可用、web 引擎或显式关闭就记 `skipped` 并写明原因，不会让渲染失败）。
> 三档语义：`pass` = 这次真的查了且通过；`fail` = 查了没过；
> `skipped` = **这次没执行**——它**不等于通过**，交付时要逐项说明为什么没跑。

| 模块 | 门禁 id | 什么时候跑 | 实测代价 | 关掉 |
|---|---|---|---|---|
| `scripts/preflight.py` | `G0` | `frames` 之前（任何 stage 开渲前） | < 1 s | `--no-preflight`（留痕，不算通过） |
| `scripts/qavmaf.py` | `G11` | `qa` 收尾，全片逐帧 | 29 s 片 **14.2 s**（同机负载高时 28.8 s） | `--vmaf off` / `"qa": {"vmaf": {"mode": "off"}}` |
| `scripts/qaasr.py` | `G12` | `qa` 收尾，转写成片音轨 | 29 s 片 **8.7 s**（faster-whisper small / CPU） | `"qa": {"asr": {"enabled": false}}` |

---

## 1. G0：渲染前资源预检（`preflight.py`）

### 1.1 为什么要有它

缺字形（✓ / emoji / 生僻字）和坏 TTS 音频都是"**渲染完才报**"的问题：`filmkit.glyph_missing_chars()`
本身是准的，但只在 `layout` / `qa` 才被调用——等 870 帧渲完，才发现某句 `big` 里有个方块。
preflight 不新写检测器，只把现成检测**前移**，并把结论整理成「字符 + 字段 + 句 id」：

```
[FAIL][preflight] 2 处问题（下面是前几条）：
  - l02.big：缺字形 '✓'（字体候选里没有这个字，渲染时会画成方框）
  - l03.sub：缺字形 '🚀'
```

### 1.2 五个检查项（三档语义，**"没查"不许写成"通过"**）

| id | 查什么 | skipped 何时出现 |
|---|---|---|
| `script` | 剧本可解析、最小结构合法（title / lines / id 唯一 / text 非空） | 不 skip |
| `glyphs` | 所有上屏文本逐字过 `glyph_missing_chars`；**按字段实际绘制的字重**检测（big/kicker=bold、lead=light、sub=reg+light、items=reg、text=bold+reg、title/chapter=reg） | 不 skip |
| `tts` | `work/tts/*.mp3` **若存在**，必须能被 ffprobe 解析出音频流（坏文件/半截缓存在这里就暴露） | `work` 没给 / 还没有 TTS 产物 |
| `fonts` | 字体候选至少一个真的能加载（对 bold / reg / light 各取一次 `filmkit.font()`） | 不 skip |
| `web_page` | `engine="web"` 时入口页存在（`script.web.entry` 或 `<剧本目录>/web/index.html`） | pil 引擎 |

> 为什么要按权重分：`msyhbd / msyh / msyhl` 是三个文件，字形覆盖不完全相同。用错权重会把
> "只有 reg 缺、但该字段画 reg"误报成没事，或者反过来。

### 1.3 单独跑 / 接 CI

```bash
python preflight.py --script film.json --work work --engine pil --json
#   --json 时 stdout 只有 JSON，人读提示走 stderr
#   退出码 0 = 没有 error；1 = 有 error；--strict 时 skipped 也算 error
```

它是**纯函数**：`check(script, *, work=None, engine="pil", timeline=None) -> {ok, errors, warnings, checks, engine}`，
不打印、不落盘、不改入参。`render.py` 调 `_run_preflight`，有 error 就**在渲染前**返回非 0
（`--no-preflight` 可通过，但会在 QC 报告"本次绕过的门禁"里留痕）。

---

## 2. G11：全片 VMAF / SSIM（`qavmaf.py`）

### 2.1 这条门禁的关键不是阈值，是**参考帧从哪来**

**参考帧必须由场景函数实时重渲染**（`scenes.draw_frame`），绝不能拿成片自己抽的帧当参考——
那是"拿成片和成片比"，VMAF 恒等于 100，门禁形同虚设。

实现路径与 `frames` 阶段同一套绘制管线：`draw_frame -> draw_subtitle -> 片头/片尾淡入淡出`，
逐帧生成 rgb24 经 **stdin 管道**喂给 ffmpeg 的 rawvideo 输入，与成片解码流做 libvmaf / ssim 比较。
**只渲一遍帧**：一次 ffmpeg 调用里用 `filter_complex + split` 同时算 VMAF 与 SSIM。内存恒定（不缓存整片帧序列）。

### 2.2 三档语义与阈值

| 档 | 条件 | 账本 |
|---|---|---|
| `fail` | VMAF 均值 **< 92.0**（`qavmaf.VMAF_PASS`） | G11 = fail，阻断交付 |
| `pass` + warning | **92.0 ≤ VMAF < 96.0**（`qavmaf.VMAF_WARN`） | G11 = pass，但打印"建议复核 / 别降档" |
| `pass` | VMAF ≥ 96.0 | G11 = pass |
| `skipped` | 没拷 `qavmaf.py` / web 引擎（没有可重渲的参考帧）/ `--vmaf off` / 成片不存在 / 缺 timeline / 缺 filmkit / ffmpeg 无 libvmaf / 解码失败 / 比较 0 帧 | G11 = skipped，写明原因 |

**SSIM 是诊断项**，低于阈值只记 warning、不单独阻断（文本画面的 SSIM 对字幕边缘过敏感，VMAF 才是感知门禁）。

### 2.3 阈值 92 / 96 是怎么来的（**只有一份权威定义**）

判据来自 qavmaf 作者对**同一部示例片**（参考帧含字幕与片头片尾淡入淡出）的三组实测：
crf17 → **97.24**（pass）、crf30 → **95.74**（pass + warning）、二次编码 + 缩放坏片 → **29.45**（fail）。

**阈值只有一份**：权威定义是 `qavmaf.py` 的 `VMAF_PASS=92.0` / `VMAF_WARN=96.0`
（SSIM 0.95 / 0.98 只作诊断、不单独阻断）。`render.py` **刻意不再设第二套常量**——
同一事实只留一个权威值，改阈值只改 `qavmaf.py` 一处。

按片收紧或放宽：`film.json` 的 `"qa": {"vmaf": {"pass": 92, "warn": 96}}`（会透传给 qavmaf）。

> 参考：另一份 1080p **无字幕纯测试图**的 CRF 阶梯是 96.98 / 96.32 / 94.87 / **92.25** / 83.03 / 50.23
> （crf18 / 22 / 26 / 30 / 36 / 46）——它的 crf30 恰好压在 92 的失败线上。**素材不同不可直接比较**，
> 所以判据以带字幕的真实成片标定为准；这也说明"crf30 附近会抖动"，交付时按片覆盖阈值或直接降一步 CRF。

**门禁反证（判据必须能失败）**：

| 素材 | VMAF | 判定 |
|---|---|---|
| crf17 正常片 | **97.24** | pass |
| crf30 正常片（模块默认 92/96 下） | **95.74** | pass + warning（低于 warn 96） |
| 二次编码 + 缩放坏片 | **29.45** | **fail** |

也就是说：好片落在 95+、坏片直接掉到 29，中间没有灰区——阈值取 92/96（模块默认）时，
"crf30 该提醒"和"坏片该阻断"两件事同时成立。

**720p / 540p 的阈值沿用 1080p 标定**：分数不完全可比（降采样后比较分辨率变了），
qavmaf 会在报告里打 warning 提醒这一点，但**不因此改判**——实测同片 720p VMAF 比 1080p 只低
**0.12 分**（另一份独立素材测得 -0.22），说明"内部 1080p 构图 + 编码时 lanczos 下采样"
确实是**超采样式降规格**，不是糊。

### 2.4 分辨率：下采样合法，上采样是 fail

结构性不符是 **fail**（不是"没法查"）：

- 成片分辨率 **> 内部构图 1920x1080**：上采样不是合法交付（把 1080p 包装成"更高分辨率"）；
- 宽高比不是 16:9 横屏；成片尺寸与 `timeline.video` 声明的交付尺寸不一致；读不出成片分辨率；
- **下采样交付是合法配置**：内部构图恒 1080p，编码时 lanczos 缩到 720p / 540p。此时 qavmaf
  **在成片分辨率下比较**——参考流在 ffmpeg 滤镜图里用 `scale=w:h:flags=lanczos` 缩到成片分辨率
  （与 `FrameSink` 编码时同口径，不用 PIL resize），返回的 `ref_scale` 记录映射关系。
  VMAF 阈值仍按 1080p 标定沿用（实测 720p 与 1080p 的 VMAF 差不到 3 分）。

### 2.5 实测耗时与成本

| 项 | 实测 |
|---|---|
| 29.011 s 示例片全片（870 帧，1080p 交付） | **14.2 s**（同机负载高时同一份代码测到过 28.8 s，见下"成本口径"） |
| 720p@24 交付（696 帧） | **7.2 s**（帧数少 + libvmaf 比较分辨率低） |
| 分项吞吐 | 参考帧渲染受 ffmpeg 消费速度限制；libvmaf threads=min(8, CPU) 时约 124–160 帧/秒（threads=0 只有约 20 帧/秒） |
| 抽样模式 | `--vmaf sample`（每 5 帧取 1）实测 8.0 s 跑完 184 帧，VMAF 与全片差 0.34 分 |
| 分数稳定性 | 对 n_threads **完全不敏感**（threads=0/1/4/8/16 与跨进程复跑的逐帧分数 sha1 全同） |

**成本口径要说清**：全片门禁 = 参考帧渲染 + 比较，模板场景 1080p 约 **0.5× 片长**（29 s 片 14.2 s）；
创意场景 `draw_frame` 更重（模板 6–10 ms/帧，复杂场景 30–50 ms/帧），整体按 **0.4–1× 片长** 估，
12 分钟片按 0.4× 外推约 **5 分钟**。**同一台机器负载不同能差近 2×**（另一轮同代码实测 28.8 s），
所以这里只写"这一跑"的数，不写承诺。纯 libvmaf 比较（参考帧已就绪）约 1.2× 实时——
**别把"纯比较的 1.2×"当成端到端成本**。

**qa 比渲染还贵**：29 s 示例片 `--stage all --cost` 实测总 **47.3 s**，
其中 `frames` 12.5 s、`qa` 23.9 s（VMAF 14.2 + ASR 8.7 + 其余）。
决策影响：**批量出片的瓶颈在质检而不是渲染**——想压回渲染量级就 `--vmaf off` 或 `--vmaf sample`。

### 2.6 单独跑 / 可证伪性

```bash
python qavmaf.py --film out/film.mp4 --timeline work/timeline.json \
    --scenes scenes.py --script film.json --mode full --out work/qa/vmaf.json --json
# 退出码：0=pass；1=fail；2=skipped（CI 必须把 2 也当"没通过"处理）
python qavmaf.py --selftest     # 内置自检：验证三档语义不许"没查写成通过"
```

**证明它真的在查**：把 `crf` 从 18 调到 36 重渲一遍，G11 **必须**从 pass 掉进 fail。
不掉就是参考帧/对齐环节没接对。

---

## 3. G12：ASR 回读（`qaasr.py`）

### 3.1 为什么

容器 / 响度 / 帧 / 音画同步这几项质检都建立在一个假设上：
**成片音轨里念的就是剧本那几句话**。可是 TTS 缓存串了、换声线时漏了某句、混音把某段压没了、
重新渲了但音轨是旧的——这些从容器和波形上都看不出来。把音轨转回文字逐句对账才能发现。

### 3.2 引擎优先级与匹配方式

1. **faster-whisper**（本机已装，import 失败才降级）→
2. **ffmpeg whisper 滤镜**（ffmpeg 9.0.2-full_build 带 `--enable-whisper`，实测有 whisper 滤镜、没有 asr 滤镜）→
3. 两者都不可用 → `skipped`（**不许把"没查"写成 pass**）。

匹配：按 `timeline.json` 每段 `start/end` 取 ASR 结果里落在窗口内的文本，
去标点 / 空白 / 数字写法差异 / 常用繁体（whisper 中文输出常夹繁体）后做**字符级多重集召回**。
- 字符级：中文没有词边界，词级召回不成立；
- 多重集而不是集合："是"这种字重复出现要计数，否则少一句也能凑出高召回。

每句还报 ASR 首字相对 `voice_start`（缺省 `start`）的偏移，`|偏移| > 0.35 s` 给 warning。

### 3.3 三档语义与阈值

| 档 | 条件 | 账本 |
|---|---|---|
| `fail` | 字符召回 **< 0.72**（`render.QA_ASR_RECALL_WARN`） | G12 = fail |
| `pass` + warning | **0.72 ≤ 召回 < 0.85**（`render.QA_ASR_RECALL_PASS`） | G12 = pass，但提示"可能串了缓存" |
| `pass` | 召回 ≥ 0.85 | G12 = pass |
| `skipped` | 没拷 `qaasr.py` / `qa.asr.enabled=false` / 两个引擎都不可用 / 模型下载失败 / 成片没音轨 | G12 = skipped，写明原因 |

按片覆盖：`"qa": {"asr": {"recall_pass": 0.85, "recall_warn": 0.72, "model": "small"}}`。

### 3.4 为什么默认线是 0.85 而不是 0.95

实测（29 s 示例片，含 "DeepSeek 4.1 Flash / DeepSeek Harness" 这类中英混排专名）：

| 场景 | 召回 |
|---|---|
| 干净示例片 1080p（4 段全对上、缺句 0） | **0.803** |
| 干净示例片 720p@24（同剧本） | **0.853** |
| 干净示例片（另一轮） | **0.811 – 0.828** |
| 丢句反证：把 l03 从音轨剪掉、timeline 仍写 3 句 | **0.677**（`missing_lines=["l03"]`、gate=fail、exit 1） |

0.803–0.853 不是流水线坏了，是 ASR 对中英混排专名的正常误差——实测把 "DeepSeek 4.1 Flash"
听成过 "DIXX4.1flash"、"DeepX Carnacy" 等变体，但 4 段**全对上、缺句 0**。
所以这条门禁的定位是**"抓整句丢失 / 音轨不是这一版"，不是逐字校对**：

- **< 0.72 才说明"整句丢了 / 音轨跟剧本不是一套"**——这是真正要阻断的；
- 0.72–0.85 给 warning：仍然出片，但提示可能是缓存串了；
- **≥ 0.95 会把干净的示例片判成 fail**，所以不能拿短测试片的 0.985（19.87 s 纯中文素材，
  没有专名）当默认线。

### 3.5 实测耗时

| 阶段 | 实测 |
|---|---|
| 模型加载（small，CPU int8） | 1.99 s |
| 转写 28.9 s 音轨 | 3.79 s（RTF ≈ 0.13） |
| 合计（第一次实测） | **6.18 s**（`elapsed_s`） |
| 合计（全链 `--stage all` 里） | 1080p 交付 **8.7 s** / 720p 交付 **6.4 s**（同机负载与交付时长不同） |
| 模型体积 | 484 MB（HF 缓存 972 MB = fp16 + int8 两份），内存峰值 < 1 GB |
| tiny 模型 | 只适合快速冒烟：加载 1.36 s、转写 0.50 s，但错字多、时间戳只到整秒 |

### 3.6 两个已知限制（写下来别装看不见）

1. **中英混排专名会掉召回**：这是判据本身的限制，不是片子坏了。真要更严就按片覆盖
   `recall_pass`，但要先确认"专名被听错"和"真的丢了句"能区分开——用 `missing_lines` 定位。
2. **偏移检查依赖 `voice_start`**：手搓的 timeline 若没有 `voice_start`，偏移会退化成"相对段 start"，
   于是每句都多算一个 `lead`（默认 0.55 s）→ 全片报偏移 warning。`filmkit.build_timeline` 写的
   timeline 一定带 `voice_start`，只有手搓才会缺。

### 3.7 单独跑 / 可证伪性

```bash
python qaasr.py out/film.mp4 --timeline work/timeline.json --script film.json \
    --model small --lang zh --out work/qa/asr.json
# 退出码：0=pass；1=fail；2=skipped
# 阈值只有一份权威：qaasr.py 的 QA_RECALL_PASS=0.85 / QA_RECALL_WARN=0.72，
# render.py 的 G12 直接引用同一组常量（CLI 默认值与 --recall-pass/--recall-warn 同源）。
python qaasr.py --selftest                    # 不联网自检
# 离线回放（CI 用，不加载模型）：
python qaasr.py film.mp4 --timeline ... --asr-json <已有 ASR 结果.json>
```

**证明它真的在查**：拿一条音轨把某句剪掉、timeline 不动，G12 **必须** fail（实测召回 0.677、exit 1）。
不 fail 就是窗口/对齐没接对。

---

## 4. 三道门禁一起关掉会怎样

`skipped` 是**合法的**（例如线上机器没装 faster-whisper、或 web 引擎路线），
但交付前请把每一行 `skipped` 的理由抄进验收话术：**"没查"必须说出来，不能混进"通过"里**。
`--strict-gates` 会把所有 `skipped` 升级成失败——交付流水线里建议开着它跑最后一次。

另外 `<SKILL_DIR>/tools/golden_frames.py` 是**像素级**金帧回归（不进门禁账本）：
`--check` 渲 3 帧算 SHA-256 与 `assets/golden_frames.json` 比对，
同环境像素变了 = fail，换机器（字体指纹不同）= skipped。改绘制代码后先跑它，再跑 `selfcheck.py`。
