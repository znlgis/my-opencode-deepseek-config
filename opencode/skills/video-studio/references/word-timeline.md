# 词级时间轴与词锚点

> 本页是 `work/words.json` 的**契约**、`cue at` 词锚点的**语义**，以及四个在真实跑里踩过的坑。
> 设计前提一句话：**句子时长仍以 TTS 实测时长为权威**；词级时间戳只是附加值，
> 用来折字幕、打锚点、让门禁发现"词事件突然没了"。

---

## 1. 数据从哪来

```
film.json 的 "words": true（默认）
        ↓  --stage tts
edge_tts.Communicate(text, voice, rate=, pitch=, volume=,
                     boundary="WordBoundary").stream()
        ↓ 同一路 async for 里  chunk["type"]=="audio" 写文件 / 其余进 raw_events
work/tts/<id>.mp3.part  --os.replace-->  work/tts/<id>.mp3
        ↓ decode_pcm + trim_silence_bounds（算出被裁掉的句首秒数 shift）
work/tts/<id>.mp3.json 的 meta：words / words_key / words_events / words_shift / words_ok
        ↓  _write_words()
work/words.json          ← G10、字幕折行、cue 词锚点**唯一**的事实来源
        ↓  build_timeline(..., fps=)
timeline.json 的 scenes[].words_abs  （句内词表平移到绝对时间）
```

三道防呆，缺一条都会出问题：

| 防呆 | 为什么 |
|---|---|
| 显式 `boundary="WordBoundary"` + `stream()` | 用 `save()` 会把词事件丢掉；不传 `boundary` 时默认 `SentenceBoundary`，**一条词事件都收不到**（实测 edge-tts 7.3.1 / zh-CN：词事件数 = 0） |
| 音频先写 `.part` 再 `os.replace` | 直接 `open(目标, "wb")` 时，一次断网/超时就把**已缓存的好音频截成 0 字节**；先写 `.part` 再替换，失败只丢本次新音频，旧缓存还在 |
| 词表独立缓存键 `tts_words_key` | 并进 `tts_key_v2` 会让**全部已有音频缓存一次性失效**（必须整片重新联网合成）。分开记 -> 已有音频照旧复用，只在"这次显式要词级"时补采一次 |

### 1.1 `work/words.json` 契约

```jsonc
{
  "schema": 1,
  "enabled": true,
  "lines": {
    "l03": {
      "dur": 7.1836,          // 该句语音实测时长（秒，裁剪后）
      "n": 13,                // 词事件条数
      "covered": 5.2013,      // 词时长之和 —— 注意它**小于** dur（词间空隙 + 句尾静音）
      "tail": 0.0901,         // dur - 末词 end，句尾静音量
      "start": 0.0412,        // 首词起点（相对本句语音起点）
      "gaps": [0.183, 0.207], // 相邻词之间的空隙（秒）
      "mono": true,           // 词偏移是否单调
      "words": [{"text": "画质", "start": 0.0412, "dur": 0.2814, "end": 0.3226}, ...]
    }
  },
  "stats": { "lines": 4, "words": 64, "empty": [] }   // empty = 拿不到词事件的句 id
}
```

`stats.empty` 非空就是"这些句这次没有词级"。它**不是崩溃**，而是服务端行为变了或者该句被裁坏了；
G10 会因此记 `skipped` 或 `fail`（看是全部为空还是个别句为空）。

---

## 2. 关键函数（`filmkit.py`）

| 函数 | 作用 | 退化行为 |
|---|---|---|
| `tts_words_key(text, voice, rate, pitch, volume, trim, trim_db)` | 词级缓存的独立键 | — |
| `trim_silence_bounds(pcm, sr, thresh_db=-42.0, keep_ms=80)` | 返回 `trim_silence` 会保留的样本区间 `[a, b)`；`a/sr` 就是 `words_shift` | 全静音 -> `(0, 0)`，等于不裁剪 |
| `words_normalize(events, *, offset_shift=0.0, dur=None, min_gap=0.0)` | 100 ns 事件 -> `{text,start,dur,end}`；减 `offset_shift`；负起点钳到 0；按 `prev_end` 夹紧保证单调；末词 end 不超过 `dur` | 空事件 -> `[]` |
| `words_summary(words, dur=None)` | 出 `n / covered / gaps / tail / mono / start`（G10 判据全在这里算） | — |
| `sub_words_at(scenes, t)` | 当前上屏字幕对应的 `words_abs`，没有就 `None` | 无词级 -> `None`（调用方回退整句） |
| `wrap_by_words(text, f, max_w, words)` | 按真实词边界折行；词表与原文对不上（有尾差/切不开）就返回 `None` | `None` -> `fit_sub_lines` 回退字符折行 |
| `fit_sub_lines(..., words=)` / `draw_subtitle(..., words=)` | 排版入口，多一个可选参数 | `words=None` 时排版与不带词级时逐像素一致 |
| `build_timeline(..., fps=)` | 给每段补 `words_abs`（句内词表平移到绝对时间） | 该句没有 `words` 就没有 `words_abs` |
| `cue_time(at, scenes=, total=)` | 解析 `at`（含 `l03:w2` 词锚点） | 见 §3 |

---

## 3. 词锚点：`"l03:w2"` / `"l03:w2+0.1"`

```jsonc
{"at": "l03:w2",      "type": "impact"}   // 第 3 段第 2 个词的起点
{"at": "l03:w2+0.1",  "type": "impact"}   // 该词起点 + 0.1 s（偏移可带符号）
{"at": "l03:w2-0.05", "type": "whoosh"}   // 提前 50 ms
```

- **下标从 1 开始**：`w1` = 第一个词（和"第 1 句是 l01"一致，不玩 0 基）。
- 解析优先读 `scenes[].words_abs`，没有就退到 `scenes[].words`（句内相对时间）。
- **找不到该词时静默退化**：`l03:w99` 不会炸，只打一条
  `[WARN] cue at='l03:w99'：l03 只有 13 个词，w99 不存在，退化为句偏移`，然后按 `l03+偏移` 处理。
  —— 所以**改完台词一定要看 stdout 有没有这条 WARN**，否则你会以为自己精确打到了某个词。
- 场景 id 本身不存在时才 `raise ValueError`（先跑 layout，或者改用 `abs:秒`）。
- `abs:12.5` / `12.5` / `l03` / `l03+0.35` 这几种写法不含词锚点，行为与不带词级时**完全一致**。

---

## 4. 四个坑（都是实测踩出来的）

### 4.1 词间有 0.1–0.3 s 的空隙

edge-tts 的 WordBoundary 只覆盖"发音区间"，相邻词之间的停顿**不在任何事件里**。
所以 `covered = Σ dur` 永远小于句长。示例片实测 4 句 64 词，覆盖率约 0.7 量级。
**判据不能写 `covered == dur`**——G10 的覆盖率下限是 **45%**（低于它才是"明显少了"）。

### 4.2 句尾静音不在事件里（约 0.5 s）

排轴用的是**整句实测时长**，不是词时长求和。G10 只检查"末词终点离句尾不超过 **0.40 s**"
（示例片实测 0.09 s）。这条超过 0.40 s，通常不是"片子坏了"，而是词事件本身整体错位了。

### 4.3 裁剪会让词整体提前（`words_shift`）

`trim_silence=True`（默认）会裁掉句首尾静音，时间轴用的是裁剪后的语音；但词事件的 offset 是相对
**原始 mp3** 的。`trim_silence_bounds()` 与 `trim_silence()` 共用同一套判据，
`words_normalize(offset_shift=a/sr)` 负责平移。**不平移，词会整体提前 0.1–1 s**
（模板句实测 shift ≈ 0.10 s 量级）。这两个函数必须**永远**共用判据，改一个不改另一个就会系统偏移。

### 4.4 首词起点也要查

裁剪后首词通常离句首 ≤ 0.05 s；G10 的上限是 **0.20 s**。这条超了，多半是"裁掉的静音量算错了"
或"词事件从更早的句子串过来了"——两种都会让字幕比声音先亮。

---

## 5. 自己验一遍（不联网也能查一半）

```bash
# 1) 词级有没有真的落盘（看每句 n / covered / tail）
python -c "import json;d=json.load(open('work/words.json',encoding='utf-8'));print(d['stats']);[print(k, v['n'], v['covered'], v['tail']) for k,v in d['lines'].items()]"

# 2) 词锚点解析（故意写一个不存在的词，必须看到 WARN）
python -c "import filmkit as K, json; tl=json.load(open('work/timeline.json',encoding='utf-8')); \
print(K.cue_time('l01:w1', scenes=tl['scenes'])); print(K.cue_time('l01:w999', scenes=tl['scenes']))"

# 3) 全片门禁视角
python render.py ... --stage qa      # 看 QC-REPORT.md 的 G10 行
```

**可证伪性检查**（这条最值钱）：随便把某句的 `work/words.json` 手改一个词的 `start` 到乱序，
重跑 `--stage qa`，G10 **必须**报"词偏移非单调"。不报就说明门禁没读到那个文件。

---

## 6. 关掉它

- 单片：`film.json` 写 `"words": false` -> `--stage tts` 不再收词事件，G10 `skipped`（写明"本片未启用"），
  字幕退回整句折行。**不要词级能力就写这一行。**
- 只跳过门禁不跳过能力：没有开关——G10 就是词级数据的体检，没数据就 skipped，不会 fail。
- 词事件拿不到（服务端变了）：不阻断出片，G10 `skipped` / `fail` 二选一看覆盖面，
  meta 里 `words_ok=false`、`words_events=0` 是排查入口。
