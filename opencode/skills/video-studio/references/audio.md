# 音频：采样级混音、配乐与音效

> 读法：§1–§9.5 是底层原理与手写示例；§10–§12 是推荐做法——
> §10 提示点系统（声音跟着画面走）、§11 程序化配乐（musicgen 参数化编排）、§12 程序化音效（sfxkit）。
> 先读 §10，它决定了另外两节怎么用。

## 1. 为什么不用 ffmpeg 混音

一条 ffmpeg 命令喂 100+ 个输入做混流，在 Windows 上会产生**非确定性静音**：输入线程池竞争导致某个输入未就绪就被读取，断媒体收到空流，后续输出静音。同一条命令连跑三次：正常 / 静音 / 正常。

**正确做法**：全部解码成 float32 PCM，在 numpy 总线上按采样点精确叠加，写 WAV，再一次性喂给 ffmpeg。

```python
bus = K.AudioBus(total=600.0, sr=48000)
bus.place(pcm, at=12.35, gain=1.0, fade_in=0.02, fade_out=0.06)
bus.place(music_seg, at=0.0, gain=0.35, loop=True, fade_in=1.5, fade_out=2.0)
wav = bus.mix(normalize=True, fade=0.6)
```

优点：确定性（同输入必同输出）、精度 1/48000 s、可单层重放、可任意点插入、可中途打印峰值自检。

## 2. 音频参数铁律

| 参数 | 值 | 说明 |
|---|---|---|
| 采样率 | 48000 | 视频配音标准，AAC 原生支持 |
| 位深 | float32 内部 / int16 落盘 | 内部保留余量，落盘前才量化 |
| 声道 | 单声道总线 → 双声道输出 | 旁白单声道足够 |
| 母带响度 | **-16 LUFS ± 1** | 网络视频标准 |
| 真峰 | **≤ -1 dBTP** | AAC 编码会推高约 0.9 dB，混流前留 1.4 dB 余量 |
| 旁白与间隙比 | ≥ 12 dB | 人声必须明显突出（有代码保障：`mix_film` 旁白侧链） |
| 配乐电平 | 旁白以下 12–18 dB | 否则听不清字（默认 `music.gain_db=-12`；-1.5 时背景与台词几乎等高，不合格） |

## 3. 响度链路（三步）

```python
# 本仓库**实际**链路（filmkit.mix_film + mux）：
#   旁白 voice_gain=1.0 居中（不被侧链动）
#   → 音效按 CUE_BASE_DB 逐条，再按旁白包络压 3 dB
#   → 配乐 music.gain_db(默认 -12) → 音效响时 duck(按风格 3.0–5.5) → 旁白包络再压 6 dB
#   → 求和 → 峰值保护 0.89 → ffmpeg loudnorm 两遍到 -16 LUFS / 目标真峰 -1.5
af = K.loudnorm_two_pass("work/audio.wav", target_i=-16.0, target_tp=-1.5)
```

> **下面是"设计建议"，本仓库没有实现**：
> 「各轨目标 LUFS 旁白 -13.5 / 音效 -24.5 / 配乐 -26.5」+「前瞻限幅器补差额」——
> 这套是专业分轨母带流程，本项目用"相对增益 + 最终 loudnorm"替代，**代码里没有逐轨 LUFS 对齐，
> 也没有前瞻限幅器**。要那套效果得自己在 `mix_film` 之后接。

**不要用 RMS 近似 LUFS**：RMS 是线性平均，LUFS 有 K 计权 + 门限，误差可达 6–8 dB。
**不要整体拉高补响度**：会把峰值推爆。正确顺序是"分轨对齐 → 限幅压峰 → 母带归一"。

## 4. 程序化配乐（numpy 合成）

### 4.1 和弦铺底

```python
def pad_chord(freqs, dur, sr=48000, amp=0.05, detune=0.004):
    n = int(dur * sr); t = np.arange(n) / sr
    out = np.zeros(n, dtype=np.float32)
    for f in freqs:
        for d in (-detune, 0.0, detune):          # 三声部微失谐 = 合唱感
            out += amp * np.sin(2 * np.pi * f * (1 + d) * t)
    return (out * env / max(1, len(freqs))).astype(np.float32)
```

和弦进行模板：
- 抒情/纪录：Am–F–C–G（A2 110, F2 87.3, C3 130.8, G2 98.0 Hz）
- 推进/数据：Dm–Bb–F–C
- 收尾转大调：把最后一段根音升高小三度

### 4.2 节奏型

```python
kick = K.tone(55.0, 0.28, amp=0.5, decay=14.0)      # 底鼓
hat  = K.noise_burst(0.05, amp=0.12, decay=60.0)     # 踩镲
```

### 4.3 段落响度叙事

同一张和弦表，按段落改配器与电平：叙事段比推进段低 **4–6 dB**，高潮段多加一层八度。观众感觉"有起伏"，但换段不会有突兀的电平跳变。

## 5. 程序化音效（物理建模）

**核心通则：激励用加速度（接触力的一阶导），不用位移。**
- 位移激励 → 频谱质心只有约 200 Hz，听起来像闷鼓
- 加速度激励 → 质心提到 900+ Hz，才像"敲击"

```python
def impact(dur=0.35, sr=48000, freq=180.0, decay=18.0):
    n = int(dur*sr); t = np.arange(n)/sr
    body = np.sin(2*np.pi*freq*t) * np.exp(-decay*t)
    body = body * (-(2*np.pi*freq)**2)          # 二阶导 = 加速度激励
    click = np.random.RandomState(7).randn(n).astype(np.float32) * np.exp(-decay*3*t)
    body = body / (np.max(np.abs(body)) + 1e-9)
    return (0.3*body + 0.12*click).astype(np.float32)
```

常用配方：

| 音效 | 做法 |
|---|---|
| 纸张翻动 | 短噪声 8–20 ms + 带通 2–6 kHz + 快衰减 |
| 笔尖/墨水 | 极短噪声 + 高通 + 随机微延时（沙沙感） |
| 键盘/机械 | 冲击 + 金属共振（几个固定频率正弦，衰减 8–12） |
| 转场 whoosh | 噪声 + 带通频率扫描 500 Hz→4 kHz + 0.3 s 包络 |
| 数字落定 | 正弦 660/880 Hz + 极快衰减 + 短混响尾 |
| 环境风声 | 粉噪声 + 带通 200–800 Hz + 慢 LFO 调制幅度 |

## 6. 循环环境音（无缝）

**频域整周期噪声法**：在频域生成噪声，只保留整数个周期，逆变换后首尾天然相接。

```python
def loop_noise(dur=8.0, sr=48000, lo=180, hi=900, seed=5):
    n = int(dur*sr)
    rng = np.random.RandomState(seed)
    spec = np.fft.rfft(rng.randn(n).astype(np.float32))
    freqs = np.fft.rfftfreq(n, 1/sr)
    band = ((freqs >= lo) & (freqs <= hi)).astype(np.float32)
    win = np.hanning(64); win = win / win.sum()
    band = np.convolve(band, win, mode="same")     # 软化边缘防振铃
    out = np.fft.irfft(spec * band, n).astype(np.float32)
    return out / (np.max(np.abs(out)) + 1e-9) * 0.3
```

接缝跳变比 < 1.5 视为无缝（首尾样本差 / 常态相邻样本差）。

## 7. 旁白处理

```python
# edge-tts 每条尾部约 0.95 s 静音，55 条累计 61.5 s
# filmkit.tts(trim=True) 默认裁掉首尾静音，并把裁剪后的 wav 作为 decode 源：
#   meta.path -> xxx.trim.wav, meta.dur = 裁剪后实测时长；原 mp3 保留在 meta.mp3
# 缓存 key = sha1(text|voice|rate|pitch)；改台词会自动重新合成
pcm = K.decode_pcm(mp3)
trimmed = K.trim_silence(pcm, thresh_db=-42.0, keep_ms=80)   # 不是 trim_tail：本仓库没这个函数

# 轻 EQ：200 Hz 以下衰减 2 dB（去闷），3 kHz 提 1 dB（清晰度）
```

### 7.1 声线（edge voice id）与语气（tone）：没有 persona 身份表

> **没有 persona 身份表**（`bright_female` / `narrator_male` 之类 9 个身份）：`voice` 只认
> **裸 edge-tts voice id**（如 `zh-CN-YunxiNeural`）+ 实测响度补偿表 `assets/voice_loudness.json`；
> tone 的 6 档（hook/body/quote/outro/emphasis/calm）照旧。

```json
// 默认（采样器产出的样子）：整片一个旁白，map 的四个 kind 都指向同一个 id
{"voice": {"id": "zh-CN-YunxiNeural",
           "map": {"hook": "zh-CN-YunxiNeural", "body": "zh-CN-YunxiNeural",
                   "quote": "zh-CN-YunxiNeural", "outro": "zh-CN-YunxiNeural"},
           "jitter": 0.35, "max_distinct": 1}}

// 用户点名「多人对话 / 访谈 / 广播剧」时的多声线档（仍受 max_distinct 约束）
// {"id": "zh-CN-YunxiNeural",
//  "map": {"hook": "zh-CN-YunxiaNeural", "body": "zh-CN-YunxiNeural",
//          "quote": "zh-CN-YunyangNeural", "outro": "zh-CN-YunxiNeural"},
//  "jitter": 0.35, "max_distinct": 3}
```

```bash
python <SKILL_DIR>/scripts/voicekit.py --list    # 音色响度表（裸 id + 实测 gain_db）与 6 档 tone
python <SKILL_DIR>/scripts/voicekit.py --plan film.json   # 打印逐句声线计划（不发请求）
```

- **voice id** 直接就是 edge-tts 的音色名；`--list` 里带的是**实测响度补偿**（不同音色天然差 3–7 dB，
  实测跨度 6.0 dB）。写了表里没有的音色，补偿回落 0 = 旁白电平没校准。
- **tone** 决定相对偏移与句前/句后停顿：hook / body / quote / outro / emphasis / calm
  （`body` 是基准档：lead 0.62 / tail 0.92 / 所有 delta=0）。
- **内容规则**会自动升级语气：引号句 → quote（并换到 quote 声线）、数字密集 → emphasis、
  问句/感叹 → 稍快稍亮、长句 → 降速加尾部留白；显式 `lines[].voice/tone` 优先级最高。
- **jitter**（由设计采样，范围 0.10–0.30；不写则 voicekit 按 0.0 = 无抖动）用 sha1 派生 ±1.5% / ±1Hz / ±0.04s 的微扰，去掉"每句等距等音量"的机械感。
- **单声线是默认**：采样器只在你明确要求「多人对话 / 访谈 / 广播剧 / 分角色」时才产 `max_distinct >= 2`；其余情况四个 kind 都映射同一个 id，整片只有一个旁白（更不会出现"换个 kind 就换人"）。手改 `voice.map` 永远优先，`lines[].voice` 最高。

**缓存键**：`sha1(v2|text|voice|rate|pitch|volume|trim|trim_db)`。key 必须含 trim——
不含 trim 时把 `trim_silence` 改成 false 仍会命中裁剪版，"关了裁剪其实没关"。
`v1` key 仍给既有缓存保留。

## 8. 混音自检（必做）

```python
# 每 20 秒窗口测 RMS，低于 0.002 记 SILENT —— 防"后半段全程静音"
for i in range(0, len(wav), 20*48000):
    seg = wav[i:i+20*48000]
    if np.sqrt(np.mean(seg**2)) < 0.002:
        print("SILENT at %.0fs" % (i/48000))

print("peak:", np.max(np.abs(bus.bus)))      # 期望 < 0.99
```

## 9. 常见音频故障

| 现象 | 根因 | 修法 |
|---|---|---|
| 后半段静音 | ffmpeg 多输入混流 | numpy 总线 |
| 爆音/削波 | 先 astype 再判 dtype | 先判 dtype 再转；打印中间峰值 |
| 听起来糊 | 配乐电平过高 | 配乐压到旁白以下 12–18 dB（默认 -12 且带旁白侧链） |
| 听不清台词 | 背景与人声响度接近 | `music.gain_db` 降到 -12、`voice_duck_db` 开 6；别用"整体拉高"救 |
| 响度上不去 | AAC 推高真峰后限幅 | 混流前留 1.4 dB 余量 |
| 环境音接缝咔 | 直接拼接 | 频域整周期噪声 |
| 配音机械 | 语速超过 +15% | 降到 +6% 左右，加自然停顿 |
| 字幕比声音慢/快 | 按字数估时长 | 实测反推 |

## 9.5 两条混音路线的取舍（含实测坑）

### 路线 A：numpy 采样级叠加（推荐）

完全确定、精度 1/48000 s、可单层重放、可中途打印峰值。`filmkit.AudioBus` 就是这条。

### 路线 B：ffmpeg 滤镜链（轻量场景可用）

单遍完成，不用把 PCM 都拉进内存：

```bash
ffmpeg -i voice.wav -i bgm.wav -filter_complex \
  "[1:a]volume=0.13[b];\
   [0:a][b]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[m];\
   [m]loudnorm=I=-16:TP=-1.5:LRA=11[o]" \
  -map "[o]" -c:a aac -b:a 192k out.m4a
```

**三个必须记住的参数**：

| 参数 | 不这么写会怎样 |
|---|---|
| `amix=...:normalize=0` | 默认 `normalize=1` 按输入数均分，2 路输入直接**掉 6 dB** |
| `amix=...:duration=first` | 否则按最长一路，可能被循环 BGM 拉到无限长 |
| 显式 `-t <总时长>` | `-stream_loop -1` 会让 `-shortest` 失效，产出 13 小时的怪物文件 |

### 不要用 adelay 铺轨

`adelay=ms|ms` 存在**系统性约 -0.6 s 偏移**（实测），越长的片子错得越明显。要精确对位就用 numpy 按采样点写位：

```python
i0 = int(round(at * sr))
bus[i0:i0+len(src)] += src * gain      # 精确到 1/48000 秒
```

### 首镜必须从 0 开始

典型事故：第一个镜头 `start ≠ 0`，全片音画错位（实测 1.2 s：画面先跑，声音从 0 开始）。**时间轴第一段 start 必须严格等于 0**，且 `voice_start ≥ start`。

### BGM 循环的淡出陷阱

BGM 素材自带淡出尾音，被 `-stream_loop` 循环后会在每个接缝处"死一下"。做法：循环时只取素材的**稳态段**（裁掉头尾淡入淡出），或改用频域整周期噪声。

## 10. 提示点系统（先读这一节）

**问题**：手工把音效放到"第 12.5 秒"，只要片子里改一句台词、加一个停留窗口、把 BPM 从 120 改成 100，
所有音效就全错位。改一次要重新对一遍时间——这就是为什么很多程序化视频"声音和画面对不上"。

**做法**（来自 ai-history-film 的 cue 系统）：**画面登记提示点，音频照着提示点合成**。

```
场景代码 ──cue(t, type)──> work/cues.json ──> sfxkit 逐条合成 ──> work/sfx.wav ──> 混音
                ▲                                      ▲
                │                                      │
        web 引擎由页面导出                     每条 cue 的 t 是成片绝对秒
```

### 10.1 三种登记方式

```python
# A. film.json（少量关键音效，最好改）
"sfx": {"cues": [
  {"at": "l03+0.05", "type": "impact", "size": 1.1},
  {"at": "abs:12.5",  "type": "whoosh", "dur": 0.9, "gain": -2}
]}

# B. scenes.py 的 cues() 钩子（推荐：画面算到哪就响到哪）
def cues(tl, script):
    out = []
    for s in tl["scenes"]:
        if s.get("kind") == "body":
            for i, _ in enumerate(s.get("items") or []):
                out.append({"at": "%s+%.2f" % (s["id"], 0.48 + 0.32 * i),
                            "type": "ui_blip", "pitch": 1760.0 + 110.0 * i, "gain": -6})
    return out

# C. web 引擎直接调 K.CueBus（页面里：S.cue(t, type, params)）
```

### 10.2 at 的写法

| 写法 | 含义 | 什么时候用 |
|---|---|---|
| `"abs:12.5"` | 绝对秒 | 固定位置的重击（片头标题落定） |
| `12.5` | 数字，同义 | 同上 |
| `"l03+0.35"` | 场景 l03 起点 + 0.35 s | **绝大多数情况**：跟着这一段的节奏 |
| `"l03-0.2"` | 场景 l03 起点 - 0.2 s | 转场前的"吸气"/预兆 |
| `"l03"` | 场景起点 | 段落切换的 whoosh |

**先想"这个声音属于哪个事件"，再写 at。** 属于"第 3 段第 2 张卡片落位"就写 `l03+0.9`，
不要写 `abs:21.3`——后者在停留窗口一改就废。

### 10.3 gain 怎么给

`gain` 是**相对该类音效基准电平**的 dB 偏移（基准见 `filmkit.CUE_BASE_DB`）：
- `impact` 基准 -5 dB → 写 `gain: -3` 就是"这一记重击比默认轻 3 dB"
- 同一类音效在一段里连响 5 次，后面的要递减（`-2, -3, -4, -5, -6`），否则听众只听见前两下

### 10.4 预览与检查

```bash
python render.py ... --stage cues      # 打印条数、类型分布
python render.py ... --stage sfx       # 合成音效轨，打印峰值
python qa_frames.py out/film.mp4 --cues work/cues.json   # 看音画同步 dt
```

**音画同步 dt > 60 ms 就是错**：人眼/耳对 40–60 ms 以内的错位不敏感，超过就会"感觉哪里不对但说不出来"。
dt 大的典型原因：cue 挂在字幕出现的时刻上，而画面重击晚 100 ms——应该挂在动画时刻上。

## 11. 程序化配乐（musicgen）

> **没有「配乐模板」。** 配乐由 `musicgen.py` 按参数**现场编排**：没有可选曲风，也没有风格表，
> 只有参数。下面仍是「一张和弦表贯穿全片」的技术思路，实现落在参数化编排器上。

```python
import musicgen as MG
# 参数就是「设计」：bpm / key / scale / mood / seed，加可选的编排参数
samples, report = MG.render(389.0, bpm=120, key="Dm", scale="minor",
                            mood="steady", seed=7)   # 同参数逐采样一致
MG.save("work/music.wav", samples)
```

要更细的控制就传 `sections` / `progressions` / `motif` / `drums` / `voices` ——
它们真的参与事件展开，不是死旋钮（接口见 `musicgen.py` 模块 docstring）。

底层原语（吸气、分段能量、过门）仍在 `scorekit.py` 里，但**它不是配乐引擎**：
它只提供和声表 / 律动层 / 混音 / 响度测量这些可复用件。要自己拼一条非常规配乐可以直接
调它的 `Score` 再用 `scorekit.save()` 落盘；成片链路用的是 musicgen。

### 11.1 为什么"一张和弦表贯穿全片"

这是"像一首曲子"和"一堆配乐拼起来"的分水岭：全片同一个调、同一张和弦进行，
换段只改**律动层数与亮度**。观众听到的是"同一首曲子在推进"，而不是"换了五首 BGM"。

### 11.2 分段能量

| energy | 配器 | 用在哪 |
|---|---|---|
| 0.1–0.3 | pad + 偶尔的拨弦 | 开场、回忆、安静陈述 |
| 0.4–0.6 | + bass + kick（每小节 2 下） | 正文讲解 |
| 0.7–0.85 | + hat + arp | 数据推进、竞赛 |
| 0.9–1.0 | 全开 + 加花 | 高潮、爆发 |

规则：**相邻两段能量差 ≤ 0.3**，否则会"突然换歌"。
真正的高潮靠**前面几小节压住**（`suck`）然后放开，不是靠这一段更响。

### 11.3 段落边界对齐小节

`plan_from_anchors` 把每个场景的起点 snap 到最近的小节线（`bpm=120` → 每小节 2 s）。
代价是段落边界与画面边界差 ≤ 1 s（观众感知不到），收益是**所有画面重音都落在拍点上**。

### 11.4 性能与确定性

- 389 s 立体声 48 kHz 合成应当 ≤ 60 s；慢就是某个乐器在 Python 循环里逐采样合成，改成向量化。
- 同 seed 必须逐采样一致（`np.random.default_rng(seed)`，不许用全局 `np.random`）。
- 配乐只保证"不削波、不直流、峰值 ≤ -1 dBFS"；**最终响度交给 ffmpeg loudnorm**。

## 12. 程序化音效（sfxkit）

### 12.1 为什么不"画波形"

画波形（正弦叠加 + 包络）得到的是**合成器音色**；真实音效是**物理过程**的记录。
两者的差别在三个地方，而这三个地方恰好是人耳判断"真不真"的依据：

| 物理量 | 决定什么 | 合成器常犯的错 |
|---|---|---|
| 模态比（形状） | 音色是"棒/板/膜/钟" | 用整数倍泛音 → 听起来像风琴 |
| 衰减率（材质） | "木头还是金属" | 所有材质用同一个衰减 |
| 接触时间（力度/软硬） | 亮不亮 | 力度只改音量，不改亮度 |

`sfxkit` 就是按这三个物理量建模的：模块顶部模态区里的 `MATERIALS`（eta/alpha）、
各种形状的模态比（`bar_ratios` / `plate_ratios` / `ring_ratios` / `BELL_RATIOS`），
以及 `Hit(contact_ms, hardness)`——**力度越大接触越短，所以重击天然更亮**，不需要额外调 EQ。

```python
import sfxkit as S
S.render("impact", seed=3, size=1.2)          # 已做完母带（按真峰 -1 dBTP 归一）
S.list_recipes()                               # [{name, title, tags, params}]
```

### 12.2 配方速查（cue 的 type 就是名字）

| 类别 | 配方 | 备注 |
|---|---|---|
| 界面 | `ui_blip` `ui_click` `relay_click` `roulette_tick` `answer_chime` | `ui_blip` 有音高，**全片少用** |
| 文字 | `typing` `typing_at` `text_stream` | `typing_at`/`text_stream` 带 `times[]` 逐字对齐 |
| 转场 | `whoosh` `riser` `impact` `sub_drop` `reverse_swell` `glitch` `tape_rewind` | `impact` 是"重击"的主力 |
| 环境 | `wind` `rain` `campfire` | 长环境声，用 `dur` 控制长度 |
| 撞击 | `knock_wood` `metal_hit` `glass_clink` `go_stone` | 模态合成，改参数换材质 |

### 12.3 逐字对齐（AI 打字声的正确做法）

画面上"AI 逐字输出"时，声音不能是一串等间隔的咔哒——要**跟着字出现的时刻**：

```python
out.append({"at": "%s+0.3" % s["id"], "type": "text_stream",
            "times": [i * 0.055 for i in range(len(s["sub"]))]})   # 相对提示点的秒数组
```

人在打字用 `typing_at`（更脆、更不规则），AI 输出用 `text_stream`（更连贯、偏"沙沙"）。

### 12.4 听感问题诊断表（照症状查）

| 症状 | 根因 | 改法 |
|---|---|---|
| 一团闷的低频 | 接触时间太长 / 模态只到低频 | 缩短 `contact_ms`、加 `hardness`、加辐射高通 |
| 像电子音、纯音 | 整数倍泛音、没有噪声成分 | 用真实形状的模态比；加 `contact_noise`；对称物体用 `split` 产生拍频 |
| 一串同样的"哒哒哒" | 同一个核被重复几百次 | 核变体 ≥ 30 个；`scatter` 里每粒用不同 seed |
| 刺耳、嘶嘶 | 10 kHz 以上太多 / 非线性混叠 | 空气吸收 `spectral_tilt_by_distance`、低通、过采样饱和 |
| 机械感（等间隔等音量） | 事件是均匀网格 | `poisson_times` / `human_rhythm` + `power_law` 幅度 |
| 悬在真空里 | 没有空间 | `reverb` / `outdoor`，湿声 -18～-9 dB |
| 小音箱听不见 | 能量集中 60 Hz 以下 | 降次低频层，补中频"质感"层 |
| 长音死板 | 没有慢变起伏 | `smooth_random` 调制音量/滤波频率/事件速率 |

### 12.5 音量只在成片混音里量

混音时每个音效会先按峰值归一（`finalize`），所以**配方内部的 amp 参数不影响最终响度**。
真实电平由 `filmkit.CUE_BASE_DB[type] + gain` 决定。要"更响"就调 `gain`，不要改配方里的 amp。

### 12.6 命名与许可

`sfxkit` 的算法思想改编自 `ai-history-film`（MIT）的 `sfx/` 包，实现为本项目单文件重写；
文件头已注明出处。配方名、参数名保持与上游一致，方便对照上游文档。

## 13. 片间差异与片内分离

> 完整契约见 **references/audio-variation.md**；这里只记"怎么用、怎么验"。

### 13.1 为什么"差不多"

"每片都不一样"不能只靠随机，得让**内容进结构**；下面三种做法就会让片子"差不多"：

- 配乐：内容指纹只进 `seed`（音色细节），和弦进行/动机/鼓型/段落骨架全固定
  → 两段 20 s 配乐相关性 **0.966**（实测）。
- 音效：`CueBus.build_audio` 的 seed 取 `seed_base + i`（只跟 cue 顺序）
  → 同一批 cue 跨片逐样本相同。
- 片内：提示音硬编码 D 小调五声、配乐过门与 sfx 转场在同一段落边界撞车、duck 只有 3 dB。

### 13.2 怎么变

| 层 | 行为 | 开关 |
|---|---|---|
| 配乐和声/动机/鼓型 | 由 musicgen 的编排参数直接决定：`sections` / `progressions` / `motif` / `drums` 参与事件展开（没有可挑的曲风表） | `music.seed_from_content` 控制 seed 是否掺内容指纹；结构差来自上述编排参数，而不是「换一套风格」 |
| 音效 seed | `content_seed(base, style, fp, type, t)`，随内容变 | `sfx.content_seed`（默认 true；false = 退回 `seed_base + i`） |
| 提示音调性 | `in_key(freq, key)` 跟随配乐调性 | `CueBus.build_audio(key=...)`，render.py 自动传 |
| 段落边界 | [边界-2.2s, 边界] 内的 whoosh/riser 自动 -3 dB | `sfx.duck_transitions`（默认 true） |
| duck | 按风格 3.0–5.5 dB（默认 4.0） | `music.duck_db` 显式覆盖 |

**兼容开关**：`sfx.content_seed: false` 时音效退回 `seed_base + i`（逐样本可复现）；`key` 为空时提示音回落 D 小调五声。

### 13.2.1 旁白优先

视频以旁白为主，**BGM 与音效是可选层**：它们负责情绪与重音，不能抢字。具体做法：

| 手段 | 默认值 | 说明 |
|---|---|---|
| 配乐基线 | `music.gain_db=-12` | -1.5 时背景与台词几乎等高；-12 是"人声明显压住背景"的经验线 |
| 旁白侧链（配乐） | `voice_duck_db=6` | 有人声的地方再压 6 dB；间隙由包络自动恢复（不会整片闷住） |
| 旁白侧链（音效） | `voice_duck_sfx_db=3` | 提示音/转场也让人声 |
| 语音阈值 | `voice_thresh_db=-42` / `voice_floor_db=-60` | 10 ms 块 RMS 映射成 0~1 的活动度，起音快、释放慢 |
| 旁白自身 | `voice_gain=1.0` | **不被任何侧链影响**——旁白优先不等于"压人声" |
| SNR 兜底 | `guard_snr_db=12` / `guard_cap_db=30` | 混音后实测人声活跃区的人声/背景差，不足就整体补压背景（只压背景） |

实测（真实 edge-tts 旁白 + scorekit 配乐）：不做上面这些处理时 **SNR -4.2 dB**（背景比台词还响，
就是"听不清"的原因）；按上表默认值 **SNR 12.0 dB**；合成信号口径 15.0 dB，真实配乐素材口径 12.9 dB。
设 `voice_duck_db=0` / `voice_duck_sfx_db=0` / `guard_snr_db=0` 可分别关掉对应机制。

### 13.3 验收：别靠耳朵，跑脚本

```bash
python <SKILL_DIR>/tools/audio_similarity.py --strict   # 在包根执行
```

A 组量"同参数换内容指纹"和"换编排参数"的相关性；B 组量"跨片音效是否还逐样本相同"（同时验
位置派生可复现）；C 组读 `render.py` 的 duck 默认值并打印质心对照；
D 组构造"有人声段 + 间隙"，量人声段旁白/背景响度差。任何一项不达标 exit 1。

### 13.4 改音频代码后的自检清单

1. `python <SKILL_DIR>/tools/audio_similarity.py --strict` —— A/B/C/D 四组达标（任意目录）。
2. `python <SKILL_DIR>/scripts/selfcheck.py` —— **85 条**全 PASS（第 33 条盯内容派生 seed、第 35–38 条盯旁白优先，第 39 条盯音频边界）。
3. `python musicgen.py --selftest` / `python sfxkit.py --selftest` —— 各自全 PASS（musicgen 的 26 项覆盖编排与确定性）。
4. 改 `assets/scenes.py` 的 `cues()` 后，跑一次 `--stage cues` 看类型分布是否覆盖 hook/body/quote/outro。