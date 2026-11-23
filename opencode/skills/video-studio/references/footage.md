# 素材剪辑线（footage 模式）

> 一句话：**画面从现成素材来，声音和时间轴仍然由代码保证。**
> 零素材线（render.py + design.json）解决"没有画面可拍、要自己画"；
> 这条线解决"有画面素材（下载的、自己的、手上的片子），要按文案剪成片"。

## 0. 什么时候走这条线（分诊）

按顺序判断，**先素材后自绘**：

| 顺序 | 情形 | 走哪条 |
|---|---|---|
| 1 | 用户给了素材（视频 / 图片 / 链接 / 一个片子文件） | **footage** |
| 2 | 用户没给，但网上能找到可用画面（纪录片、新闻片、公有领域影像） | **先 fetch，再 footage** |
| 3 | 都找不到，或题材不该用现成画面（数据、抽象、品牌、动画） | 零素材线（render.py） |

两条线共用底盘：TTS 实测时长 → 时间轴 → numpy 采样级混音 → 两遍 loudnorm → 质检报告。
区别只在"画面从哪来"：零素材线来自 draw_frame(t, frame)，素材线来自 sources[].at + len。

> **素材不必提前准备好**：sources[] 可以直接写 url，--stage fetch 负责下载；
> 也可以写本地 path（推荐用于回归与离线复现）。

> **这条线由问诊第 2 题决定**：`intake.py` 的 `mode` 若解析成 `footage`（用户自己选了素材剪辑，
> 或选了 `auto` 且题材是真实影像），`--finalize` 会直接给出 `footage.py` 的下一步命令，
> 并提醒**提案层不适用**。走到这里说明产线已经定过了，别再回头跑 `propose.py` 给素材线定外观。
> `mode=hybrid` 时**默认走形态 3**（素材打底 + 小面积叠加）：本线出底片 / 混音 / 字幕，再写
> `overlay.json`，最后由 `compose.py --stage all` 叠层 + 烧字幕 + 响度归一；**默认链路不需要 `propose.py` / `matchstyle.py`**。
> 只有**显式** `intake.py --finalize --hybrid-form 1` 才走按段拼接（形态 1：`render.py` 出图文段 + `concat`），
> 那时才需要在拼接前对齐风格（否则就是"纪录片接 PPT"）：
> ```bash
> python matchstyle.py --footage footage.json --work work --report               # 只看素材画像
> python matchstyle.py --footage footage.json --work work --apply --align-bars   # 锚配色 + 对齐画幅
> python propose.py --adopt --work work                                          # 换了调色板要重绑
> ```
> 它把 **图文段** 的底色/主色锚到素材上，素材本身**一个像素都不上色**。
> 反过来的做法（在素材上叠一层主题色）实测只会让素材发灰、发闷——那不是统一，是糊。

## 1. 契约：footage.json

与 film.json **并列、不通用**。完整字段见 schemas/footage.schema.json，
可跑通的模板见 assets/footage/footage.template.json。

```jsonc
{
  "title": "片子名",
  "sources": [
    {
      "id": "film1983",
      "kind": "video",
      "path": "media/source.mp4",          // 或 "url": "https://..."
      "license_note": "出处/授权说明",       // F5 要求非空
      "burned_subs": {"y0": 750, "y1": 824},                 // 原片烧录字幕带（源像素）
      "watermark": {"x": 1580, "y": 16, "w": 316, "h": 44}   // 原片水印框（源像素）
    }
  ],
  "treatment": {                            // 画面治理：一次定义、全片复用
    "crop": "1576:660:172:80",
    "scale": "1920:804",
    "letterbox_color": "0x0b0c0e"
  },
  "video": {"width": 1920, "height": 1080, "fps": 30},
  "audio": {
    "voice": "zh-CN-YunyangNeural", "rate": "-6%",
    "env_gain": 0.12,                       // 素材原声（环境层）
    "music_gain": 0.10,
    "music": {"bpm": 92, "key": "Dm", "scale": "minor", "mood": "steady", "seed": 11}
  },
  "subtitle": {"size": 46, "margin_v": 38},
  "credit": {"text": "素材：<作品名>（<出品方>，<年份>）", "at": 188.0},
  "pacing": {"lead": 0.35, "tail": 0.55, "outro": 2.2},
  "lines": [
    {"id": "s01", "text": "朗读文本", "sub": "上屏字幕",
     "shot": {"source": "film1983", "at": 175.0, "why": "开场空镜"}}
  ]
}
```

四条硬规则：

1. **shot.at 是源文件内绝对秒数**，len 可省（省掉就跟随该句旁白的实测时长）。
2. **每句都要 shot**：只有一张图就一路指同一个源，at 不变。
3. **burned_subs / watermark 是门禁的输入**（F2/F3 做几何判定），
   --stage probe 会打印建议值，直接粘进 sources[]。
4. **license_note 与 credit.text 是交付条件**（F5），缺了直接 fail。

## 2. 十个 stage（`--stage all` 只跑出片必需的 7 个：probe→tts→edl→cut→mix→assemble→qa；素材不在本地时加 fetch。transcribe / locate 是**定位镜头的准备阶段**，要单跑）

```bash
python <SKILL_DIR>/scripts/footage.py --footage footage.json --work work --out out/film.mp4 --stage all
```

| stage | 做什么 | 落盘 |
|---|---|---|
| probe | 素材体检（时长/分辨率/有无音轨），给治理建议与声明值 | work/sources.json |
| fetch | 下载/落地素材（直链走 urllib，页面走 yt-dlp；sections 只下时间窗） | sources[].path / dl/ |
| transcribe | faster-whisper 转写（喂 float32 PCM，绕开 PyAV 版本坑） | work/transcripts/*.json |
| locate | 关键词定位镜头（**中文关键词不要走 shell 参数**） | work/locate.json |
| tts | 逐句旁白 + 实测时长（复用 filmkit.tts_batch） | work/tts/、work/tts.json |
| edl | 时间轴 + EDL（越界镜头在这里拦住） | work/timeline.json、work/edl.json |
| cut | 按 EDL 切镜（统一画幅 + 抽原声当环境层） | work/segs/ |
| mix | 三总线混音（旁白 1.0 / 原声 0.12 / 配乐 0.10） | work/mix.wav |
| assemble | 拼接 + ASS 字幕 + 两遍 loudnorm | 成片 |
| qa | F1..F9 门禁 + 通用质检 | out/QC-REPORT-footage.md |

--stage all 只跑出片必需阶段；素材都在本地时自动跳过 fetch，
transcribe / locate 是**定位镜头**用的准备阶段，单独跑。

## 2.1 素材预算：至少要有几个独立来源（F8 的量化口径）

口径的唯一真源是 `footagekit.source_budget()`（常量 `SOURCE_PER_MIN_RECOMMENDED=3` /
`SOURCE_PER_MIN_MINIMUM=2` / `SOURCE_ABS_MINIMUM=3`）：

```text
分钟数        = 片长(秒) / 60
recommended  = max(3, ceil(分钟数 × 3))                 # 推荐：3 个独立来源 / 分钟
minimum      = min(recommended, max(3, ceil(分钟数 × 2)))  # 硬下限：2 个 / 分钟
```

| 片长 | 推荐来源数 | 硬下限 |
|---|---|---|
| 45 s | 3 | 3 |
| 3 min | 9 | 6 |
| 6 min 25 s | 20 | 13 |

（按分钟向上取整，再夹**绝对下限 3 个**；短于 1 分钟的片子也是 3 个。）

**为什么要两条线**：只留推荐线会把正常片拦死；只留下限线，就会放过"2 张图撑 6 分钟"。

**F8 三档语义**（`footageqa.f8_source_variety`，与 F1–F9 一起注册在 `GATES` 里）：

| 判据 | 状态 | 说明 |
|---|---|---|
| `distinct < minimum` | **fail** | 明显偷懒：画面会全程是同一张图在平移 |
| `minimum ≤ distinct < recommended` | pass + **warning** | 画面重复度偏高，建议再补几个来源 |
| 单源连续 ≥3 镜 / 重复取景窗 >0 | warning | 指出是哪一段（同源且 `at` 相差 <0.5 s 算重复取景窗） |
| `licensing.allow_sparse_sources = true` | **skipped** | 显式豁免，detail 里写出 `why`；不许默认静默放过 |

**豁免写法**（先写明"为什么可以这么少"，再放行）：

```jsonc
{
  "licensing": {
    "allow_sparse_sources": true,
    "why": "客户只给了两张授权图，且要求全程同一文物特写"
  }
}
```

**EDL 阶段就拦**：`--stage edl` 会用同一个预算先量一遍，低于硬下限直接 `SystemExit`
（切了 40 个镜头才发现只有 2 个源 = 白 cut 一遍）。两个逃生口：

```bash
python footage.py --footage footage.json --stage edl --allow-sparse-sources   # 低于硬下限也继续；F8 仍记 fail
python footage.py --footage footage.json --stage all --allow-missing-sources  # 有源下载失败也继续（默认 fail-closed）
```

## 3. 素材定位：从"我要讲这句话"到"用第几秒"

素材线最耗时的一步是**找画面**。流程：

```bash
# 1) 转写（全片音轨，small 模型在 CPU 上约 15–20 倍速）
python footage.py --footage footage.json --stage transcribe --model small

# 2) 关键词定位（一次可给多个词；返回候选时间码 + 上下文）
python footage.py --footage footage.json --stage locate --keywords "土城,茅台,乌江,金沙江"

# 3) 把候选时间码填进 lines[].shot.at，再跑 edl/cut/...
```

三条经验（都是踩出来的）：

1. **中文关键词不要走 shell 命令行参数**。Windows 控制台是 GBK，
   python x.py 茅台 传进 Python 的字符串已经乱码，在 UTF-8 转写文本里永远 0 命中——
   会让人误判"转写里没有这个词"。要么走本脚本的 --keywords（Python 内处理），
   要么把关键词写进 JSON 再读。
2. **地名容易被转写错**：茅台/毛台、娄山关/楼山关、土城/土成 会混着出现。
   给同义词，或用 --n 多看几条，别只看第一条。
3. **先用 at 落点，再看 1–2 秒偏移**。电影的镜头切换很密，差 1 秒就换了画面；
   cut 出来的片段先抽帧看一眼，不对就调 at（改一句重跑只要几秒）。

## 3.1 公有领域素材抓取配方（实测可照抄）

历史 / 考据类片子最省事的素材源是 Wikimedia Commons（公有领域 / CC）。下面这套流程实测跑通过：

**Step 1 · 用 Commons API 查图，同时拿到许可**

```bash
curl -s "https://commons.wikimedia.org/w/api.php?action=query&format=json" \
     "&generator=search&gsrnamespace=6&gsrlimit=20&gsrsearch=%E5%8D%83%E9%87%8C%E6%B1%9F%E5%B1%B1%E5%9B%BE" \
     "&prop=imageinfo&iiprop=url|size|extmetadata&iiurlwidth=1920"
```

- `iiurlwidth=1920` 让 API 直接给出 1920 宽的缩略图 URL（`thumburl`），比下原图快一个量级。
- 许可与作者在 `extmetadata` 里（`LicenseShortName` / `Artist` / `Credit`），写进 `footage.json` 的
  `license_note`（F5 要求非空），片尾 `credit.text` 写作品名与出品方。
- 中文关键词直接放进 `gsrsearch`（URL 编码），**不要走 shell 参数**（Windows GBK 会乱码，见 §3）。

**Step 2 · 下载缩略图：主机与路径都有硬性要求**

| 坑 | 现象 | 正确做法 |
|---|---|---|
| 路径漏了 `commons` 这一层 | **一直 400** | 缩略图路径必须是 `/wikipedia/commons/thumb/...`；换成 `thumb.wikimedia.org` 却省掉这层同样 400 |
| 直接拉 `upload.wikimedia.org` 直链 | **429**（出口 IP 限流），批量下尤其容易 | 退避重试：等 1、2、4、8、16、32 秒（**6–8 次递增**）；还不行就降 `iiurlwidth` 或错峰 |
| 用原图 URL 而不是 `thumburl` | 单张几十 MB，慢且更容易 429 | 优先用 API 返回的 `thumburl`（已缩到目标宽度） |

**Step 3 · 下载后必须验真**（别信 HTTP 200 和文件大小）

```bash
ffprobe -v error -select_streams v:0 -show_entries stream=width,height,codec_name -of csv=p=0 dl/xxx.jpg
python -c "from PIL import Image; im=Image.open('dl/xxx.jpg'); print(im.size, im.mode)"
```

错误页也会以 200 落盘（文件大小看着"很合理"，ffprobe 报 `Invalid data found`）——这一步不能省。

**Step 4 · 取景：竖图与长卷两种配方**

- **竖图**（立轴 / 碑帖 / 竖拍照片）：**整幅居中 + 模糊暗底填充**——背景=原图拉伸铺满 + 强高斯模糊 +
  压暗到 0.44；前景=清晰原图整幅放下，高占画布 88%。不要硬裁 16:9，会把主体裁掉。
- **长卷**（千里江山图这类）：**横向多窗口取景**，按横向位置切窗口 `x0 = (W-cw) * p`，
  同一张画一次切 **4~6 个镜头**（不同 `p` 就是不同画面），比多找 4 张图更容易保持一致。
- 素材的馆藏编号 / 扫描色卡 / 水印按 §4 治理；出处卡要真的出现在成片里。

## 4. 画面治理：烧录字幕、水印、画幅

素材常带三种"不该出现在成片里"的东西：**烧录中文字幕**、**平台水印**、**不统一的画幅**。
treatment 一次定义、全片复用：

| 字段 | 作用 | 典型值 |
|---|---|---|
| crop | w:h:x:y，先裁（去字幕带、去水印带、去黑边） | 1576:660:172:80 |
| scale | w:h，再缩放（等比；不拉伸） | 1920:804 |
| color | 最后调色（eq=...），统一不同素材的色差 | eq=saturation=0.92:contrast=1.04 |
| letterbox_color | 补遮幅的颜色 | 0x0b0c0e |

选配方的顺序（不要反）：

1. --stage probe 看建议值（默认把底部 12% 当字幕带、右上 8% 当水印区）；
2. **抽一帧亲眼看**字幕带到底占哪几行（有些片字幕在正中间，自动裁底部反而切一半）；
3. 把真实区间写进 burned_subs / watermark，让 F2/F3 能判；
4. 定 crop，确认 at + len 都在源时长内。

> **别用 delogo 硬盖水印**：实测会留下竖条纹，比水印本身更显眼。
> 能裁就裁，裁不掉就换素材。

> **hybrid 里 treatment.color 只用来统一"素材之间"的色差**（白平衡/饱和度/对比度），
> 不要写成"向设计主题看齐"的色罩：那会让素材失去自己的色彩，也让后面的 matchstyle
> 失去可测的基准（它抽帧时会带上 treatment.color，色罩会把测量结果一起带偏）。

## 5. 门禁（F1–F9）

```bash
python footage.py --footage footage.json --stage qa          # 出 out/QC-REPORT-footage.md
python footage.py ... --stage qa --strict-qa                 # fail 即失败（交付前建议开）
```

| 门禁 | 查什么 | 判据 | skipped 什么时候出现 |
|---|---|---|---|
| **F1** | 镜头越界 / EDL 合法性 | at+len ≤ 源时长；镜头总长与时间轴差 ≤0.6s；图片源不参与越界（它的"时长"由 EDL 决定） | 没有 EDL；**或没有 sources.json / 源没探到 dur**（越界判定根本没执行 -> skipped 并写明"先跑 probe"，绝不记 pass） |
| **F2** | 原字幕残留 | burned_subs 与 crop 后可见区**几何相交** | 源没声明 burned_subs；**声明了但解析不出合法区间（y1≤y0 / w·h≤0 / 非数字）= fail**（不是 skipped） |
| **F3** | 水印残留 | watermark 与可见区几何相交 | 同上：没声明才 skipped，声明了但非法是 fail |
| **F4** | 音画等长 | 视频与音频时长差 ≤ 0.05s | 读不到流时长；**成片不存在也是 fail**（不是 skipped） |
| **F5** | 素材溯源 | 每个源有 license_note + 有 credit.text | 不会 skipped（缺就是 fail） |
| **F6** | 旁白/原声比例 | **本镜窗口内**旁白 RMS 比无旁白窗口（lead/hold/tail）RMS 高 ≥12 dB；无旁白窗口要 **≥0.25 s** 才计入（`tail` 调小到 0.2 s 以下时该镜不计） | 整片没有任何 ≥0.25 s 的无旁白窗口（每镜 lead/hold/tail 太小）；音轨短于 1 s |
| **F7** | 人工签核 | 联系表存在 + 签核指纹匹配（指纹含 lines/shot/treatment/subtitle/audio/credit/pacing/licensing/sources/video/EDL——改配乐音量、`licensing.allow_sparse_sources` 或片尾出处卡同样失效） | 未签核 / 内容已变 |
| **F8** | 素材多样性 | `distinct < 硬下限` fail；`< 推荐` pass + warning；单源连续 ≥3 镜 / 重复取景窗 >0 warning | 写了 `licensing.allow_sparse_sources`（需带 `why`）；没有时间轴 |
| **F9** | 字幕排版 | 每条 ≤2 行、每行 ≤ 安全宽（`w - margin_l - margin_r`，与写入端同口径）、`MarginV + 行数×1.2×字号` 不越画布底 | 没有 sub.ass / sub.ass 里没有 Dialogue 行 / 剧本无字幕 / 没有 filmkit（量不了像素宽） |

**F2/F3 为什么是几何判定而不是"看画面像不像有字"**：实测过图像判据（底部/中部能量比、
亮像素占比、跨帧静止度），在真实素材上正反例区间重叠——已治理的成片 1.41、
未治理的原片 1.81，任何阈值都会误杀一类。而"该裁的地方裁了没有"是几何问题，
有唯一正确答案。所以门禁只认声明 + 可见区，图像指标仅作为人工复核线索打印出来。

### 签核（F7）

--stage qa 会生成 work/qa/footage-sheet.png（每镜一帧 + 镜头号/时间码）。
看过之后写 work/footage-signoff.json：

```json
{"fingerprint": "<报告里的指纹>", "verdict": "approved", "by": "user", "note": "看过"}
```

指纹 = footage.json 的 **title / treatment / subtitle / video（fps/分辨率）/ audio（voice·rate·pitch·三路增益·music_file·music）/ pacing / credit / licensing / sources（id·kind·license_note·burned_subs·watermark）/ lines（id·text·sub·shot·hold） + EDL shots** 的规范化哈希（dict 排序、list 保序、float 归一 6 位、`1` 与 `1.0` 同值；不把 path/url 放进去——换机器不该失效）。
**改了台词、镜头、配乐音量或片尾出处卡，旧签核都会自动失效**（F7 变 skipped 并写明"内容已变"），必须重新看片。

## 6. 常见故障

| 现象 | 根因 | 修法 |
|---|---|---|
| 成片比旁白短、片尾被吃 | 尾留白没并进最后一镜 → 视频短于音频 | 已修：build_timeline 把 outro 并进最后一镜 |
| 音轨全空 / 混音没声音 | --download-sections 只给视频流，没有音轨 | 另下一路音轨，或换整段下载；probe 会报 audio=NO |
| 画面变形（人变瘦/胖） | scale 与 crop 宽高比不一致，被强行拉伸 | crop 后按**同比例**写 scale；或让 scale 省掉自动算 |
| 字幕烧不上去 / ffmpeg 报 No option name near | ass=filename='D:/a/b.ass' 里盘符冒号被当参数分隔 | 用相对文件名 + cwd（footageedit.assemble 已处理） |
| 成片底部还挂着原片字幕 | crop 没盖住字幕带 | 看 F2 报的区间，改 crop 或 burned_subs |
| 右上角台标还在 | crop 只裁了上下，没裁左右 | 水印在角上时，crop 要同时缩 x 与 w |
| 后段听不清旁白 | 原片自带解说，env_gain 太高 | 降到 0.06–0.12；F6 会量"旁白/背景差" |
| 转写一条词都没有 | 中文关键词走 shell，或 faster-whisper 没装 | 走 --keywords；或 pip install -U faster-whisper |
| locate 命中一堆不相干的 | 转写把地名转错 | 给同义词、加时间窗、看上下文再定 |
| 改了台词但镜头没变 | EDL 用了显式 len（不跟旁白走） | 删掉 shot.len，让镜头跟随实测时长 |

## 7. 交付前对照清单

1. --stage qa --strict-qa 无 fail；报告里每个 skipped 都能说出为什么。
2. out/QC-REPORT-footage.md 里的时长/响度/真峰抄进回复，不要凭印象说"没问题"。
3. work/qa/footage-sheet.png **亲眼看过**，并在回复里说出具体观察（哪几镜、有没有错位）。
4. 片尾出处卡在成片里（F5 pass 只是"写了"，还要看它真的出现在画面上）。
5. python <SKILL_DIR>/tools/footage_selftest.py 全绿（改了这条线的代码就跑一次，离线约 30 秒）。
6. 素材预算核对过：`distinct ≥ 硬下限`（F8 不是 fail / skipped；豁免必须写了 `licensing.why`）。
