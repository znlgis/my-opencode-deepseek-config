# 音频差异机制 —— 接口契约

> 当前机制一句话：配乐由 `musicgen.py` 参数化编排（bpm/key/scale/mood/seed + 编排参数，
> 内容指纹与编排参数共同决定和声/动机/鼓型）；音效 seed 由 `content_seed(base, style, content_fp, recipe, t)`
> 按内容派生；提示音调性跟随配乐（`in_key`）；段落边界前 2.2 s 内的转场 cue 自动 -3 dB
> （`sfx.duck_transitions`）；混音旁白优先（配乐 -12 dB 基线 + 语音侧链 6/3 dB + SNR 兜底 12 dB）。
>
> 阈值由 `tools/audio_similarity.py` 按 §5 判定，诊断证据就是它的输出。

## 1. 要解决的两个问题（都有实测数据）

| 问题 | 实测 | 根因 |
|---|---|---|
| A. 跨视频配乐雷同 | 同 bpm/同调/同编排参数、只换内容指纹：corr **0.966** | 指纹只改 seed（音色细节），和弦进行/动机/鼓型/段落骨架全固定 |
| B. 跨视频音效雷同 | 同 cue 序列两次渲染逐样本相同 | `CueBus.build_audio` seed = `seed_base + i`，只跟 cue 顺序有关 |
| C. 片内配乐与音效难分 | impact 质心 1001 Hz / whoosh 1589 Hz / music 5451 Hz；提示音硬编码 Dm 五声 | 提示音调性硬编码 + 配乐过门与 sfx 轨同边界同频段 + duck 仅 2.5–4.5 dB |

## 2. 各层由谁负责

| 层 | 负责文件 | 做什么 |
|---|---|---|
| 配乐编排 | `skills/video-studio/scripts/musicgen.py` | 按 bpm / key / scale / mood / seed 编排；`sections` / `progressions` / `motif` / `drums` / `voices` / `energy_curve` 真的参与事件展开；内容指纹进 seed 与变奏参数 |
| 音效差异化 | `skills/video-studio/scripts/sfxkit.py`、`assets/scenes.py`、`assets/film.json` | `in_key` 跟随配乐调性；`content_seed` 内容派生 seed；`cues()` 按画面内容生成；夹具 cue 不固定三条 |
| 接线与混音 | `skills/video-studio/scripts/filmkit.py`、`render.py`、`selfcheck.py` | CueBus 内容派生 seed、调性透传、duck 分级、过门避让、旁白优先 |

## 3. 接口契约（逐字实现，向后兼容）

### 3.1 sfxkit

```python
# 1) 调性跟随：key 参数，默认 None = 固定 D 小调五声
def in_key(freq: float, key: str | None = None) -> float:
    """key=None -> 固定 D 小调五声（不随配乐调性变，逐样本可复现）。
    key 给 "Dm"/"C"/"Am"/... -> 吸附到该调（小调用自然小调五声，大调用大调五声）。"""

# 2) 所有走 in_key 的配方接受 key（通过 **kw 透传，签名末位加 key=None 或直接从 kw 取）：
#    ui_blip / answer_chime / text_stream(voice="tone") / data_chirp / roulette_tick
#    未知调名必须 raise ValueError（不静默降级，与 _enum 同口径）。

# 3) 内容派生 seed 的辅助函数（供 filmkit.CueBus 用）
def content_seed(base: int, style: str, content_fp: str, recipe: str, t: float) -> int:
    """稳定的 32 位非负 int：sha1("<固定盐>|<base>|<style>|<content_fp>|<recipe>|<t 三位小数>")
    -> int(h[:8], 16) % 9000 + 1000。"""
```

`<固定盐>` 是代码里写死的字符串常量：`sfxkit.content_seed` 与 `filmkit.content_seed` 逐字相同。
要复刻就照抄实现里的拼接串，**不要自己另拼一份**——换个盐等于换一套 seed，
"A1 同参数换指纹可区分"的验收直接失效。

### 3.2 filmkit.CueBus.build_audio

```python
def build_audio(self, total=None, sr=SR, base_db=None, default_gain_db=0.0,
                seed_base=1000, verbose=True,
                style=None,           # 设计标识（design:<hash>）；None = 位置派生
                content_fp=None,      # 内容指纹 full；None = 位置派生
                key=None):            # 配乐调性；None = 提示音按 D 小调五声
    """每条 cue 的 seed：
       - style / content_fp 任一为 None -> seed = seed_base + i（位置派生，逐样本可复现）
       - 都给 -> seed = sfxkit.content_seed(seed_base, style, content_fp, kind, t)
    并把 key 透传给配方（render(kind, seed=..., key=key, **params)）。"""
```

`render.py` 的 `_sfx_seed_context` 就是这条的调用方：它从 `resolve_style` 解出的设计计划里取
`style` / `fingerprint.full` / `music.key`；`sfx.content_seed=false`（或计划里没有指纹）时返回全 None，
`build_audio` 于是走位置派生。

### 3.3 assets/scenes.py 的 cues()

按画面事件生成，**类型按场景 kind 与内容变化**：

- `hook`：段首 `impact`（size 按字数 0.9~1.3），落定 `riser`。
- `body`：条目落位用 `ui_blip`，但 **pitch 由条目文本 sha1 派生**（json 的 items 文本）；
  最后一个条目后加 `answer_chime`（若 kind=body 且 items≥3）；段尾 `whoosh`（going 到下一段）。
- `quote`：`reverse_swell` + 落定 `impact`（轻）。
- `outro`：`answer_chime` + 片尾 `sub_drop`。
- 不许出现"每段都一样的三连击"：同类型 cue 在一段内重复时，`gain` 必须递减（-2/-4/-6），pitch 必须不同。
- **对齐**：stagger 对齐逻辑（与 `_draw_items` 同节拍）保留，返回 schema 不变。

### 3.4 assets/film.json 夹具

模板里的 `sfx.cues` 只保留 1 条转场 whoosh 作为示例，并注明"具体提示点交给 `scenes.py` 的 `cues()` 钩子"
（固定三条 whoosh/riser/impact 是"跨片雷同"的直接来源之一）。

- `sfx` 的 `content_seed: true`（默认）：走内容派生 seed；显式 `false` 回到 `seed_base + i`。

## 4. render.py 接线

- `_sfx_seed_context`：从设计计划取 `style` / `fingerprint.full` / `music.key`；
  `sfx.content_seed=false` 或计划没有指纹时返回全 None -> `build_audio` 走位置派生。
- `st_sfx`：把上面三样传给 `bus.build_audio(...)`；`sfx.duck_transitions`（默认 true）
  决定是否做过门避让。
- `st_audio`：duck 默认值按风格分级——`mono` 3.0 / `lo-fi`、`magazine` 3.5 / `cinema`、`epic` 5.5 /
  其余 4.0；`music.duck_db` 显式值永远优先。
  另外**过门避让**：配乐在 music.wav 里已经有段落过门，sfx 轨在
  [段落边界 - 2.2s, 边界] 区间内的 whoosh/riser/reverse_swell 需要自动 -3 dB
  （`_duck_transition_sfx` 实现，`sfx.duck_transitions=false` 可关）。

## 5. 验收阈值（改完必须重跑 `python <SKILL_DIR>/tools/audio_similarity.py`）

| 编号 | 指标 | 目标 | 实测（2026-11，本机） | 不这么做时 |
|---|---|---|---|---|
| A0 | 配乐非静音（RMS > -60 dBFS） | **必须 True** | True（rms 0.0733） | 静音时相关系数无定义，A1/A2 会「天然通过」 |
| A1 | 同参数换内容指纹 corr | **≤ 0.85**（仍要像同一部片子，但不能像同一首） | **0.190** | 0.966 |
| A2 | 换编排参数 corr | ≤ 0.52（不能变得更像） | **0.334** | 0.534 |
| A3 | 同指纹两次渲染逐样本相同 | **True** | True | 不可复现 |
| B0 | 音效轨非静音 / 非空（5 条） | **必须 True** | True（rms 0.0101 / len 288000） | 空轨时「逐样本相等」天然成立，B1/B3 假绿 |
| B1 | legacy（不传 style/fp）逐样本兼容 | **必须 True** | True | 老工程的音效会变 |
| B2 | 不同内容指纹 -> 音效轨不同 | **必须 True** | True | 跨片音效同一套（同质化） |
| B3 | 同内容指纹两次渲染相同 | **True** | True | 不可复现 |
| C1 | duck 默认深度 | **3.5–5.5 dB**（区间；量的是 render 基线默认值，不随风格变化） | 4.0（mono 3.0 / cinema 5.5） | 2.5–4.5 dB |
| D1 | 人声段旁白/背景响度差 | **≥ 12 dB** | **18.2 dB** | 约 1.5 dB（配乐 -1.5、无侧链） |
| D2 | 旁白处背景额外让位 | ≥ 4 dB | 6.0 dB | 0 |

> 编号以 `tools/audio_similarity.py` 的输出为准（B 组有 B0 前置断言：空轨/全静音直接 FAIL，
> 不会让「两个空数组相等」把 B1/B3 骗成 PASS）。上表「实测」列会随实现与标定漂移，
> **改完音频代码请重跑脚本以输出为准**，别照抄本表数字。
> D 组守的是"旁白优先"：视频以旁白为主，BGM 与音效是可选层。`mix_film` 的
> `voice_duck_db=6` / `voice_duck_sfx_db=3` + `music.gain_db=-12` 把"人声比背景响 12 dB"
> 从文档口号变成可执行断言（selfcheck 35/36 条 + 本脚本 D 组）。

另有硬性回归（任何一条红都不算完成）：

> 下面每条路径都按 `<SKILL_DIR>`（本技能所在目录）写；`<SKILL_DIR>` 见 SKILL.md §1 的定义。

1. `python <SKILL_DIR>/scripts/selfcheck.py` 全 PASS（**105 条**）。
2. `python <SKILL_DIR>/scripts/musicgen.py --selftest` 全 PASS（26 项）。
3. `python <SKILL_DIR>/scripts/sfxkit.py --selftest` 全 PASS（16 条）。
4. 音效位置派生的逐样本可复现：`selfcheck.py` 守着这条（`sfx.content_seed=false` 时与 `seed_base + i` 同值）。

## 6. 实现备注

几处落地细节（不改契约本身）：

| 项 | 现状 |
|---|---|
| `content_seed` 实现位置 | `filmkit` 与 `sfxkit` 各有一份逐字相同的实现（`filmkit` 走函数内延迟 import，模块级 `from` 会改导入时机）；两侧同参同值 |
| 同段重复 cue 的 gain | 每次再降 2 dB、不封顶（items 很多时会到 -2n）；模板 5 条时最末一条 -8 dB；要改听感就改 `_cue_gain_decay` 一行 |
| `roulette_tick` 调性 | `key=None` 走原公式（D 小调五声）；`key` 非空才吸附 |

**回归基线**：

- sfxkit 9 条渲染的 sha256（ui_blip ×3 / answer_chime / text_stream ×2 / data_chirp / roulette_tick ×2）逐条相同。
- scorekit 的 `render()` 只承担合成原语（成片配乐走 musicgen）：同 seed 逐样本一致（`scorekit.py --selftest` 第 18 条）。
