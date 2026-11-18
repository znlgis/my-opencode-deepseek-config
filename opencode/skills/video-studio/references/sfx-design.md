# 音效设计（sfxkit）— 物理建模程序化音效库

> 实现 `skills/video-studio/scripts/sfxkit.py` ｜ 契约 CONTRACT.md §5.1
>
> 算法思想参考 **ai-history-film（MIT）** 的 `sfx/` 包（模态合成、泊松/Cox 纹理、镜像声源混响、
> BS.1770 响度、块级动态处理）。本版本为**单文件重写**：为“确定性 / 无 scipy 也能跑 / 长片也能跑”
> 三条项目约束重新实现，未整目录拷贝，未引入任何三方依赖。

## 1. 一句话定位

用 numpy 从零合成 24 种音效：**每个声音都有物理依据**（模态、多普勒、泊松事件、房间声学），
而不是“正弦 + 白噪声 + 随便画个包络”。同一 `seed` 逐采样可复现，可直接被
`filmkit.CueBus` 按画面提示点调用。

## 2. 依赖与运行环境

| 项 | 说明 |
|---|---|
| 必需 | Python ≥ 3.9、numpy（本机 3.12.16 / numpy 2.5.4） |
| 可选 | scipy（本机 1.18.1）：装了用 `scipy.signal` 做时间域 IIR，快；**没装也能跑** |
| 禁止 | 不 import soundfile / librosa / matplotlib / 任何三方音频库；WAV 读写用 stdlib `wave` |
| import 副作用 | 无：不渲染、不读文件、不打印（`python -c "import sfxkit"` 干净退出） |

**无 scipy 兜底策略**（契约 §2 硬要求）：所有静态 IIR 都变成“有界 Python 递推算短冲激响应
（≤ 4 万采样，按极点衰减速度定长）+ `np.fft` 卷积”；时变滤波变成“汉宁窗 50% 重叠相加，
每块用块中心系数”。仍然**没有逐采样 Python 大循环**，长片性能与 scipy 路径同量级
（自检 case 13 专门验证这条路径）。

## 3. 信号约定

- 采样率 `SR = 48000`，内部 float64。
- 单声道 `(n,)`，立体声 `(n, 2)`（列优先约定，不用交错）。
- 时间参数秒、频率 Hz、增益 dB（函数名带 `_db`）。
- 一切随机走 `rng(seed)`（`np.random.default_rng`）；禁止 `random` / 无种子调用。

## 4. 分层架构（文件内分节）

| 分节 | 职责 | 关键函数 |
|---|---|---|
| core | 采样率/时间轴/随机源/声像/淡入淡出/混音总线/慢变随机 | `n_samples` `time_axis` `rng` `pan` `pan_dynamic` `smooth_random` `Mix` |
| noise | 白/粉红/布朗/任意斜率噪声、短爆发 | `white` `colored` `pink` `brown` `burst` |
| env | 指数衰减、起音+衰减、分段包络 | `exp_decay` `attack_decay` `breakpoints` |
| synth | 带限振荡器（PolyBLEP）、FM、超级锯齿、Karplus-Strong 拨弦、ADSR | `sine` `saw` `pulse` `triangle` `fm` `supersaw` `pluck` `adsr` |
| filters | RBJ 双二阶、Butterworth 级联、时变滤波、模态谐振器、空气吸收 | `biquad` `sosfilt` `lowpass` `highpass` `bandpass` `tv_filter` `resonator` |
| modal | **物理建模核心**：材质 × 形状 × 敲击位置 × 接触时间 → 撞击声 | `MATERIALS` `Hit` `Body` `plate` `bar` `membrane` `ring` `bell` `random_body` `contact_noise` |
| texture | 泊松/Cox 随机事件、幂律幅度、人的节奏、粘滑摩擦、气泡 | `poisson_times` `modulated_times` `power_law` `human_rhythm` `scatter` `stick_slip` `bubble` |
| space | 镜像声源房间混响 + 扩散尾音 + 低频模态；户外回声；距离 | `room_ir` `reverb` `outdoor` `distance` |
| dynamics | 真峰值、tanh 饱和、块级压缩、前瞻限幅 | `true_peak` `saturate` `compress` `limit` |
| master | 母带（切次声/裁静音/响度或峰值归一/真峰值校正/淡入淡出）、WAV 读写 | `finalize` `save` `load` |
| analysis | K 计权响度、RMS 包络、客观指标、自动质检 | `integrated_loudness` `envelope_db` `analyze` `qc` |
| recipes | 24 个配方 + 别名 + 注册表 + 渲染入口 + 自检 | `REGISTRY` `recipe` `render` `list_recipes` `load_all` |

## 5. 关键的物理/信号模型（为什么“像真的”）

### 5.1 模态合成（modal）

被敲击的物体 = 一组固有振动，每个模态是 `amp · r^n · sin(ωn)`：

- **形状决定频率比**：棒（1, 2.756, 5.404, 8.933…）、板（(m/a)²+(k/b)²）、膜（1, 1.594, 2.136…）、
  圆环（k(k²−1)/√(k²+1)）、钟（0.5, 1, 1.183, 1.506, 2.0…）。非整数倍泛音是“真实物体 vs 合成器”的分水岭。
- **材质决定衰减**：`t60 = ln(1000) / (α + π·f·η)`，高频衰减更快（木头 vs 金属的主要差别）。
- **敲击位置决定幅度**：模态振型 `sin(mπx)·sin(kπy)`，每击位置随机抖动 → 音色自然变化。
- **接触时间决定亮度**：半正弦力脉冲的频谱零点在 `1.5/Tc`；力度越大接触越短（Hertz 接触 `Tc ∝ v^-0.2`）。
- **简并分裂**：`Body.split()` 让每个模态变成两个极近频率，拍频产生钟/杯的 wah-wah 闪烁。

### 5.2 随机纹理（texture）

真实纹理声 = 大量略有不同的小事件 × 随机时间分布：

- **泊松时间**：事件间隔指数分布（不是等间隔）；
- **Cox 过程**：事件速率被慢变随机信号调制 → 事件“成簇”（雨的忽大忽小、火苗的爆裂群）；
- **幂律幅度**：很多小的、少数大的（`power_law`），而不是均匀随机；
- **多核散射**：同类事件的变体核随机选用，再用 FFT 卷积一次性铺到时间线上（`scatter`），
  几千个事件也快；变体不够多会叠出固定共振峰（听起来有音高）。

### 5.3 空间（space）

`room_ir` = 镜像声源法的早期反射（离散回声，决定“房间多大、墙在哪”）
+ 频率相关衰减的扩散尾音（高频先消失，决定“是否吸音”）
+ Schroeder 频率以下的轴向驻波（小房间撞击尾巴发“嗡”）。
`seed=None` 时按房间名缓存固定种子 —— 混响是“房间”的属性，同一房间反复调用应是同一空间，
也避免每次重算（hall 一次约 0.26 s）。

### 5.4 飞过 / 多普勒（whoosh）

不是“滤波器扫频”，而是按真实传播几何读源：`pos(t)` → 直达路径 `rd(t)`、地面反射路径 `rr(t)`；
按 `r(t)/c` 的延迟读湍流噪声（多普勒自然出现），音量 ∝ 1/r（钟形宽度约整段的 1/5），
越远越闷；方向决定声像移动方向。

### 5.5 动态与母带

- `compress` / `limit`：**block=64 峰值检测 + 一极点攻击/释放平滑 + `np.interp` 到音频率**，
  全程向量化（禁止逐采样循环）。攻击/释放用“短几何核卷积”精确实现一极点；
  “快攻击、慢释放”取 `min(快平滑, 慢平滑)`，与经典前馈压缩器听感一致。
- `true_peak`：多相窗 sinc 带限插值（4 相，各相 DC 增益归一），逐声道取最大。
  对 389 s 立体声约 1.4 s —— 所以 `finalize` 只在需要时调用一次。
- `finalize` 两条路径：
  - 单次音效（默认）：按真峰值 **-1 dBTP** 精确归一。
  - 环境声（`master={"lufs": ...}`）：先积分响度对齐目标；再用“真峰值 ≤ K × 采样峰值”的
    **严格上界**判断是否需要压真峰值（`K = max Σ|h_phase|`）。环境纹理峰均比高，硬压真峰值会整体掉 2 LU，
    因此契约允许环境声在 -20 ~ -24 LUFS 间取值，冲突时优先保住“不超 ceiling”。
- `integrated_loudness`：BS.1770 K 计权（48 kHz 用标准表值系数）+ 400 ms 块 / 75% 重叠 +
  -70 LUFS 绝对门限 + -10 LU 相对门限。自检与 `ffmpeg ebur128` 对拍，实测差 0.01 LU。

## 6. API（CONTRACT §5.1 冻结签名）

```python
SR = 48000
# core
n_samples(dur, sr=SR) -> int ; time_axis(n, sr=SR) -> ndarray
rng(seed=None) -> Generator ; db_to_amp(db) ; amp_to_db(x, floor=1e-12)
to_stereo(x) ; to_mono(x) ; pan(x, position) ; pan_dynamic(x, positions)
fade(x, fade_in=0.0, fade_out=0.0) ; pad_to(x, n) ; peak(x) ; rms(x) ; normalize(x, peak_db=-1.0)
smooth_random(dur, rate, seed=None, octaves=1, persistence=0.5)
class Mix: __init__(dur=0.0, sr=SR); add(x, at=0.0, gain_db=0.0, pan_pos=None); out() -> (n,2)
# noise / env
white(n, seed=None) ; colored(n, slope_db_oct, seed=None, hp_hz=20.0) ; pink(n, seed=None) ; brown(n, seed=None)
burst(dur, seed=None, slope_db_oct=0.0, attack=0.0002, t60=None)
exp_decay(n, t60) ; attack_decay(dur, attack, t60, curve=2.0) ; breakpoints(points, dur, curve="linear")
# synth
hz(note) -> float ; sine / saw / pulse / triangle / supersaw / fm / pluck / adsr
# filters
biquad(kind, f0, q=0.7071, gain_db=0.0) -> sos(1,6) ; sosfilt(sos, x)
lowpass(x, fc, q=0.7071, order=2) ; highpass(...) ; bandpass(x, f0, q=1.0)
peak_eq / lowshelf / highshelf ; tv_filter(x, kind, freq, q=0.7071, gain_db=0.0, block=64)
resonator(excitation, f, t60, amp=1.0) ; resonator_bank(excitation, freqs, t60s, amps)
spectral_tilt_by_distance(x, meters)
# modal / texture / space / dynamics / master
MATERIALS ; class Hit(t, velocity=1.0, contact_ms=1.0, pos=None, hardness=0.0)
class Body: render(hits, dur=None) -> (n,) ; split(amount=0.001, seed=None)
plate / bar / membrane / ring / bell / random_body / contact_noise
poisson_times / modulated_times / power_law / human_rhythm / impulses / scatter / stick_slip / bubble
room_ir / reverb / outdoor / distance
true_peak(x, oversample=4) ; saturate ; compress ; limit
finalize(x, peak_db=-1.0, lufs=None, highpass_hz=20.0, trim=True) -> ndarray
save(path, x, subtype="PCM_24") ; load(path) -> ndarray
integrated_loudness(x) ; envelope_db(x, win=0.005) ; analyze(x) -> dict ; qc(metrics) -> list[str]
# recipes
REGISTRY ; recipe(name=None, *, tags=(), master=None) ; load_all()
render(name, seed=0, **params) -> ndarray | None
list_recipes() -> list[dict]      # [{name,title,tags,params}]
CUE_ALIASES                       # 语义别名表
```

### 6.1 未知 type 的语义

`render("不存在")` **不抛异常**：打印一次警告并返回 `None`（CONTRACT §5.1「静默跳过，
不许 crash 整片混音」）。`filmkit.CueBus.build_audio` 对它已有 `if x is None: continue` 兜底。

### 6.2 参数透传

所有配方签名末位都是 `**kw`，`film.json` 的 cue 会透传 `gain/pan/dur/size/pitch` 等，
配方不认识的键**不会**抛 TypeError。

## 7. 24 个配方（名字就是 cue type）

| 类别 | 配方 | 主要参数 | 物理依据 |
|---|---|---|---|
| 界面 | `ui_blip` | pitch=2200, kind=tick | FM 微音 + 塑料/玻璃接触瞬态，音高吸附五声音阶（随配乐调性；未给 key 时 D 小调五声） |
| 界面 | `ui_click` | press, release | 塑料外壳模态 + 接触噪声，按下/松开两声 |
| 界面 | `relay_click` | count, gap | 衔铁金属模态 + 外壳闷响 + 触点回弹 |
| 界面 | `roulette_tick` | last=False | 硬木舌拨木钉，两次接触；last 更低更重 |
| 界面 | `answer_chime` | note=1174.66, grace | 金属条/钟琴：倚音 + 主音，大厅混响 |
| 文字 | `typing` | keys=12, speed=1.0 | 人的节奏（不等间隔）+ 键帽塑料模态 + 触底“咚” |
| 文字 | `typing_at` | times, spaces | 严格落在给定时刻（画面上每出一个字响一下） |
| 文字 | `text_stream` | times, voice=tone/soft | 逐字 AI 输出：五声微音 / 无音高沙沙颗粒 |
| 数据 | `data_chirp` | dur, rate=28 | 极短 FM 微音，音高在五声上随机跳动 |
| 转场 | `whoosh` | dur=0.9, size, direction | 真实传播几何：多普勒 + 1/r + 空气吸收 + 地面反射 |
| 转场 | `riser` | dur=4.0, tone, end | 带通中心指数上升 + 上行正弦 + 渐快颤音 |
| 转场 | `impact` | size, brightness, metal, tail | 次低频下坠 + 金属/木模态 + 瞬态 + 大厅尾 |
| 转场 | `sub_drop` | dur, start_hz, end_hz | 正弦指数下坠 + 轻度饱和补谐波（小音箱也有体感） |
| 转场 | `reverse_swell` | dur, size | 倒放的噪声+和弦，把“重击”吸进来 |
| 转场 | `glitch` | dur, density | 采样保持 + 位深破坏 + 随机小块重复 |
| 转场 | `tape_rewind` | dur, accel, stop | 带速抖动啸叫 + 摩擦噪声 + 急停 |
| 转场 | `worldline_shift` | dur, shift, depth | 倒放金属混响 + 和弦降调 + 低频 + 跳帧 |
| 环境 | `wind` | dur=15, strength, gustiness, whistle, env | 风速曲线驱动：低频隆隆/中频呼啸/高频嘶声/缝隙口哨/树叶 |
| 环境 | `rain` | dur=12, intensity, surface | 泊松雨滴 × 表面核 + 雨幕底噪 + 水洼气泡 + 雨势起伏 |
| 环境 | `campfire` | dur=12, size, crackle | 低频呼呼 + 嘶嘶 + Cox 成簇噼啪 + 木柴塌落 |
| 撞击 | `knock_wood` | knocks=3, force, door | 门板模态（木材阻尼）+ 指节接触噪声 + 房间反射 |
| 撞击 | `metal_hit` | obj=pipe/plate/bell | 金属模态（余音长、非谐波）+ 简并分裂拍频 + 硬接触“叮” |
| 撞击 | `glass_clink` | force, fill | 圆环模态 + 简并分裂；液面越高音越低、余音越短 |
| 撞击 | `go_stone` | force, big | 石棋子高频模态 + 木盘板模态 + 硬接触“咔” |

### 7.1 语义别名（CUE_ALIASES）

`blip→ui_blip`、`click→ui_click`、`relay→relay_click`、`chirp→data_chirp`、`sub→sub_drop`、
`riser→riser`、`reverse→reverse_swell`、`rewind→tape_rewind`、`stone→go_stone`、`wind→wind`
（另加 `stream→text_stream` 兼容写法）。

## 8. 性能预算（本机实测，48 kHz）

| 场景 | 目标 | 实测（空载） | 说明 |
|---|---|---|---|
| 任一配方单次 `render()` | ≤ 1.5 s（有 scipy）/ ≤ 3.5 s（无 scipy） | 最慢 wind 0.83 s / rain 0.74 s / impact 0.49 s | 两条实现路径**分开定价**，见 `PERF_BUDGET_SCIPY/FALLBACK`；自检会打印当前按哪档判定 |
| 389 s wind | ≤ 8 s | **6.0–7.8 s** | 随负载浮动 |
| 389 s rain | ≤ 8 s | **7.3–7.9 s** | rain 离预算最近，并发负载下余量只剩 ~0.1 s |
| 389 s campfire | ≤ 8 s | **6.3–6.7 s** | — |
| `--selftest` 全量 | ≤ 60 s | 约 11 s（无 scipy 时更久，含性能复核一次重试） | — |

> 表格里的区间是**两次不同负载**下的实测，不是抖动范围猜测。
> 所有数字都是"作者机器、空载或已知负载"口径，换机请用 `--bench-long` 重测。

**长环境声的成本控制**（`_sustain`）：短于 ~32 s 直接整段渲染；更长则渲染一段 24 s
素材，再用**升余弦交叉淡化平铺**到全长（窗口和恒为 1），最后叠一条全片慢变增益
（`_long_mod`，0.07 Hz）打散重复感。这样 389 s 的成本与 24 s 片段同量级，
而不是对 18.7 M 采样做整段 FFT（单次 `colored` 就要 3.3 s）。

## 9. 确定性与可复现

- 同一 `(配方, seed, 参数)` **逐采样 bit-identical**（自检 case 04：impact/whoosh/ui_blip/typing/rain）。
- 混响 IR 用“房间名 + 固定种子”缓存：同房间恒等，且不因调用顺序变化。
- 无 `time.time()` / 无 `random` / 无网络。

## 10. 集成方式

1. **pil 引擎（filmkit.CueBus）**：`film.json` 的 `sfx.cues` → `CueBus` →
   `build_audio()` 逐条 `SX.render(type, seed=..., key=..., **params)`：seed 由
   `content_seed(base, style, content_fp, recipe, t)` 内容派生（`sfx.content_seed=false` 时退回
   `seed_base + i`），`key` 是配乐调性；按 `pan` 展开立体声、按 `CUE_BASE_DB` 与 `gain`
   落位到音频总线；未知 type 告警一次并跳过。
2. **web 引擎**：页面 `window.getCues()` 导出同 schema 的 `work/cues.json`（CONTRACT §4.2）。
3. **配乐（musicgen）**：和自己写音效一样，直接用 sfxkit 原语；`import sfxkit as S`，有 `sine/saw/supersaw/fm/pluck/adsr`、
   `lowpass/highpass/tv_filter`、`white/burst/exp_decay/attack_decay`、`smooth_random`、`Mix`。

## 11. 自检

`python scripts/sfxkit.py --selftest`（16 条，不联网，约 11 s，逐条 PASS/FAIL，失败 exit 1）：

1) 注册表 24 个配方且字段齐；2) CUE_ALIASES 含契约 10 条且都指向真实配方；
3) 公共 `recipe()` 可注册；4) 同 seed 逐采样一致；5) 24 个配方都出声/不越界/≤1.5 s；
6) 别名渲染等价（blip/riser）；7) cue 多余参数被吞；8) 未知 type 返回 None 不崩；
9) 时长符合预期；10) 真峰值 ≤ -1 dBTP 且多相法与整段 FFT 差 ≤ 0.4 dB；11) 立体声/单声道形状；
12) 环境声响度贴合 master（±1.5 LU）；13) **无 scipy 兜底路径**可用；
14) analyze/qc 指标可用且能检出注入的削波；15) WAV（PCM_24/PCM_16）往返一致；
16) **响度对齐 ffmpeg ebur128**（≤ 1.5 LU，实测差 0.01 LU）。

另有 `--bench-long` 单独测 389 s wind/rain/campfire（不进 60 s 自检）：
`python scripts/sfxkit.py --bench-long`。

## 12. 已知取舍与边界

- **环境声响度 vs 真峰值**：雨/火这类高尖峰纹理，若强行压到 -1 dBTP 会整体掉 ~2 LU；
  按契约在 -20 ~ -24 LUFS 内取目标，冲突时优先“不超 ceiling”（线性缩放，不引入失真）。
- **长环境声平铺**：>32 s 的稳态纹理用 24 s 素材交叉淡化平铺 + 全片慢变调制打散重复；
  需要“雨越下越大”这类叙事弧时，应由 `film.json` 拆成多个 cue，而不是一条 389 s cue。
- **无 scipy 时**：时变滤波是“块中心系数 + 重叠相加”的近似（高频控制信号会有轻微平滑），
  静态滤波与 scipy 相对误差 ≤ 2e-4；无 scipy 机器上的长片会更慢（兜底路径以正确性优先）。
- **真峰值**：多相法与 ffmpeg 实测差 ≤ 0.05 dB；对极端近 Nyquist 内容仍是标准近似（与专业表一致）。
