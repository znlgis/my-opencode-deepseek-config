# 八阶段管线规格

## P0 定基准（先写文档，再写代码）

一份 `CONVENTIONS.md` 锁死所有换算，全项目只引用它：

```
分辨率 1920x1080 / 帧率 30 / 采样率 48000
BPM 96, 4/4 → 1 小节 = 2.5 s = 75 帧 @30fps
总时长 = 段数 × 段长（由旁白决定，不预设）
幕边界必须落在小节线上：幕 n 起始帧 = round(bar * 2.5 * FPS)
```

**为什么值得花 10 分钟**：画面、配乐、音效、字幕四个子系统全部引用同一份换算；任何一处各写各的 `150`，全片就会错位。

## P1 写剧本 film.json

```json
{
  "title": "片名（顶栏常驻）",
  "voice": "zh-CN-YunyangNeural",
  "rate": "+6%",
  "crf": 18,
  "lead": 0.55, "tail": 0.85, "min_dur": 3.0,
  "music": {"enabled": true, "seg": 4.0, "amp": 0.055,
            "chords": [[220.0, 261.63, 329.63], [174.61, 220.0, 261.63]]},
  "whoosh": true,
  "lines": [
    {"id": "l01", "kind": "hook", "text": "朗读文本（自然口语，可以长）",
     "sub": "上屏字幕（短，一行优先）", "big": "画面大字"},
    {"id": "l02", "kind": "body", "chapter": "02 · 章节名", "kicker": "PIPELINE",
     "text": "...", "sub": "...", "big": "标题", "subtitle": "副标题",
     "items": [{"text": "要点", "ok": true}]},
    {"id": "l03", "kind": "outro", "text": "...", "big": "收尾大字"}
  ]
}
```

**关键设计：`text`（朗读）与 `sub`（字幕）分离**。朗读要口语自然（可以有"其实""也就是说"），字幕要一眼看完（≤24 字）。用一个字段两头凑，必然一边将就。

## P2 事实核查（科普/纪录片必做）

产出 `FACTS.md`，三条要求：
1. 上屏的每个数字/年份都有**编号 + 出处链接**
2. 单独一节"不能上屏"黑名单：写清为什么不行、实际是什么
3. 口径表：同一数字在报告/字幕/画面里表述完全一致

真实收益（一次实际核查的账）：75 条主张 + 25 条黑名单，抓出 8 处硬伤，其中"疫情在拆把手后很快平息"这类因果讲反的错误会被评论区第一条指出。

## P3 TTS（唯一正确的时长来源）

```python
import filmkit as K
lines = K.tts_batch(script["lines"], work, voice="zh-CN-YunyangNeural", rate="+6%")
# 每句得到 {path, dur} —— dur 是 ffmpeg 解码后实测的秒数，不是估算
```

要点：
- **逐句合成并缓存**（按 id 命名），改一句只重跑一句
- 同时用 `ffmpeg -f f32le -ac 1 -ar 48000` 解码测时长，不要信 mp3 头
- edge-tts 每条尾部有 ~0.95 s 静音，片段之间累计能多出几十秒；需要紧凑时用 `fade_out` 或裁尾
- 中文音色：`YunyangNeural`（男声叙述，最稳）、`XiaoxiaoNeural`（女声）、`YunjianNeural`（男声有力）
- 语速 `+0%` 到 `+10%` 之间；超过 `+15%` 会有机械感

## P4 排时间轴

```python
tl = K.build_timeline(lines, lead=0.55, tail=0.85, min_dur=3.0)
# 每段: start / end / dur / voice_start / voice_end
# 不变式: voice_start + dur_voice <= end - tail   （旁白必须读完）
```

字幕窗口规则（防交叠，来自实战）：
```
t0 = voice_start - 0.12
t1 = voice_end + 0.45                     # 没有 5.2 s 硬钳；要钳短显式传 max_hold
t1 = min(t1, next.voice_start - 0.12)   # 绝不压住下一句
```
停留时长参考 `0.5 + 字数/9` 秒。**没有默认上限**——
真要缩短就显式传 `max_hold` / `sub_max_hold`。按字符比例平均分配会产生大量交叠。

## P5 混音（numpy 总线）

```python
bus = K.AudioBus(total_dur)
for s in tl["scenes"]:
    bus.place(K.decode_pcm(s["path"]), s["voice_start"], gain=1.0, fade_in=0.02, fade_out=0.06)
# 配乐：和弦垫铺满
# 音效：每个转场点一个短促 whoosh
K.wav_write("work/audio.wav", bus.mix())
```

**绝不**用一条 ffmpeg 命令喂几十上百个输入：Windows 下输入线程池竞争会产生非确定性静音（同一条命令跑三次，正常/静音/正常）。

## P6 母版（先看再渲）

```bash
# 母版预览：先渲 3 帧看风格（缺 tts/layout 会自动补，联网合成一次旁白）
python render.py ... --stage stills --stills "l01:3,l02:5"
# 定稿之后才渲全片
python render.py ... --stage frames --jobs 4          # 分块并行渲染
python render.py ... --stage frames --jobs 4 --resume   # 只补缺块（按绝对帧号）
python render.py ... --stage qa                          # 只读质检，不重渲不重编
```
看三件事：构图有没有重心、配色是否统一、字重和字号是否舒服。**定了再渲全片**。

## P7 成片

```python
sink = K.FrameSink("work/silent.mp4", crf=18, preset="medium")   # rawvideo 直通
...
# 视频流默认 -c:v copy（不二次编码）；需要改分辨率时 reencode_video=True
K.mux("work/silent.mp4", "work/audio.wav", "out/film.mp4")
```

编码参数：

| 用途 | 参数 |
|---|---|
| 母版/发布 | `-c:v libx264 -preset medium -crf 18 -pix_fmt yuv420p -g 60` |
| 快速预览 | `-preset veryfast -crf 23` |
| 高动态/粒子多 | `-crf 16 -preset slow` |
| 音频 | `-c:a aac -b:a 192k -ar 48000` |
| Web 播放 | `-movflags +faststart` |

## 目录约定

```
myfilm/
  film.json           剧本（唯一内容真源）
  scenes.py           场景绘制（唯一画面真源）
  filmkit.py          内核（不要改，除非你确实要扩展）
  render.py           编排器
  work/
    tts/              逐句 mp3 + .json（缓存）
    tts.json          实测时长表
    timeline.json     时间轴（唯一时间真源）
    sub.srt
    audio.wav         混音总线
    silent.mp4        无声成片
    qa/               静帧 + 联系表
  out/
    film.mp4
```

## 多 agent 并行分工（长片适用）

三条硬契约，否则必打架：
1. **写入边界硬性**：每人只写自己的文件；改公共模块先问 Lead
2. **接口先写死**：场景接口 / `film.json` 格式 / `timeline.json` 字段先定版
3. **一份施工文档**：把确定性铁律、单帧预算、安全区、镜头语言写成 `SCENE_SPEC.md` 发给每个人

长片按"幕"分派最有效：Lead 写规格+剧本+引擎+装配，每个 agent 负责 2–3 幕 `scenes/aN_*.py`，验收标准统一为"渲 3 帧 + read 亲眼看过 + 只写自己文件"。
