# 改了东西要重跑什么（改动影响表）

> 这张表的存在理由：**"我就改一句台词，要重渲全片吗？"** ——别靠猜。
> 有两个依据：`render.py --stage explain`（机器判断哪些阶段过时）与本表（人读的因果）。
> 两者不一致时**以 `--stage explain` 为准**——它看的是文件的真实指纹，不是经验。

## 0. 先记住三条

1. **时间轴是唯一的地基**：凡改了影响"每句多长"的东西（台词、语速、停留），
   下游 `cues → score → sfx → audio → frames → mux → qa` 全部要重跑。
   改字幕文案（`sub`）不影响语音时长，但会改字幕窗口 → 也要重跑 layout 及之后。
2. **画面代码（scenes.py）变了，音轨可以复用**，但 `frames` 与 `qa` 必重跑
   （qa 的锐度参考帧是场景函数重新渲染的，画面一变参考帧就失效）。
3. **分块缓存会自己拒绝复用**：改过剧本/时长/分块参数后再 `--resume`，
   `_frames_plan_stamp` 会发现指纹不符并拒绝拼接——**别手工删 `_plan.json` 绕过它**，
   按提示 `rm -rf work/segments` 重渲。

## 1. 影响表（本机 1080p30 口径）

| 改了什么 | 失效产物 | 最小重跑 | 成本量级 |
|---|---|---|---|
| `lines[].text`（台词） | tts(该句) / timeline / cues / sfx / audio / frames / mux / qa | `tts → layout → cues → sfx → audio → frames → mux → qa` | 秒级(TTS) + 帧数÷40 s |
| `lines[].sub`（字幕文案） | sub.srt / sub.ass / timeline 的字幕窗口 / frames / mux / qa | `layout → frames → mux → qa`（TTS 可复用） | 帧数÷40 s |
| `lines[].hold` / `lead` / `tail` / `bar` | timeline / cues / frames / mux / qa | 同上 | 帧数÷40 s |
| `lines[].kind` | 声线计划 / 配乐分段 / 画面版式（scenes 按 kind 选绘制函数） | `style → tts → layout → …` 全链 | 全片 |
| `work/design.json`（设计变了：配色/字体/母题/版式/字幕/配乐/声线） | style.json / storyboard / voice_plan / tts(换声线时) / score / audio / frames / qa | 看 `--stage explain`：通常是全链 | 全片 |
| `bpm` / `music.*` | score / audio / mux | `score → audio → mux → qa` | 秒级 |
| `sfx.cues` / `scenes.py 的 cues()` | cues.json / sfx.wav / audio.wav / mux | `cues → sfx → audio → mux → qa` | 秒级 |
| `scenes.py`（画面代码） | frames / qa（参考帧失效） | `frames → mux → qa`（音轨可复用） | 帧数÷40 s |
| `crf` / `preset` | 只影响 mux | `mux → qa` | 1–3 s |
| `engine`（pil ↔ web） | frames / qa / 分块（两条渲染路径不留复用） | `frames → mux → qa` | 帧数×(1÷fps 或 0.08–0.13 s) |
| `tts_workers` / `chunk_frames` / `--jobs` | 只影响调度 | 无需重跑 | — |

## 2. 三个常见错误（都真的发生过）

| 错误做法 | 后果 | 正确做法 |
|---|---|---|
| 改了 `text` 只重跑 `tts` | 新语音配旧时间轴 → 字幕漂移、音效错位 | 跑 `--stage explain`，按它给的最小重跑命令 |
| 改了 `design.json`（或重新 `--confirm` 了另一份）只重跑 `frames` | 画风换了但配乐/声线还是旧的（或反之） | 设计是所有下游的上游，通常整链重跑；`--stage style` 先看解析结果 |
| 手工删 `work/segments/_plan.json` 绕过指纹 | 拼接出的片子画面和旁白对不上，且**不报错** | 删 `work/segments` 整目录重渲 |

## 3. 一条命令代替这张表

```bash
python render.py --stage explain          # 哪些阶段过时了、为什么、最小重跑命令
python render.py --stage qa --strict-stale  # 有过时就失败（CI / 交付前）
python scripts/stagewatch.py --work work --json   # 机读（给脚本/agent 用；render.py 没有 --json）
```

## 4. 和人工签核的关系

签核（`--stage accept`）绑的是**内容指纹**（剧本 + scenes.py + 引擎）。
所以：**改了上面表里任何一项，之前的签核都会自动失效**，需要重新看、重新签。
这不是麻烦，是这套机制存在的意义——否则"我签过了"会变成一张永远有效的空头支票。