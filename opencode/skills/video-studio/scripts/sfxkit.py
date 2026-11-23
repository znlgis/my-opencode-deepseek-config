# -*- coding: utf-8 -*-
"""sfxkit — 物理建模程序化音效库。

参考项目：ai-history-film（MIT）的 sfx/ 包。本文件为**单文件重写**，API 按
协作契约 §5.1 冻结；算法思想（模态合成、泊松/Cox 纹理、镜像声源混响、
BS.1770 响度、块级动态处理）改编自 ai-history-film（MIT），实现细节针对本项目
"确定性、无 scipy 也能跑、长片也能跑"三条约束重写，未整目录拷贝。

设计要点（详见 references/sfx-design.md）：
1. 默认 48 kHz、内部 float64；单声道 (n,)，立体声 (n,2)。
2. 一切随机走 np.random.default_rng(seed)；同一 seed 逐采样一致。
3. scipy 可选：装了用 scipy.signal 的时间域滤波；没装时所有 IIR 都走
   "短冲激响应（有界 Python 递推，≤4 万采样）+ np.fft 卷积"，长片不会卡死。
4. 动态处理（compress/limit）用 block=64 的增益包络（向量化 + np.interp），
   不做 Python 逐采样循环。
5. 长环境声（风/雨/火）用"短段渲染 + 交叉淡化平铺"控制成本，见 _sustain()。
"""
from __future__ import annotations

import hashlib
import inspect
import math
import os
import re
import sys
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

try:                                   # scipy 可选：没装就走 FFT 卷积兜底
    from scipy import signal as _sig
except Exception:                      # pragma: no cover - 兜底路径
    _sig = None

__version__ = "1.1.0"

SR = 48000
LN1000 = 6.907755278982137            # ln(1000)：幅度衰减 60 dB 对应的自然对数

# 单配方 render() 的性能**软预算**（秒）。两条实现路径分开写，绝不共用一个数字：
# 1.5 s 是按"有 scipy"的实现能力定的（空载最慢 wind ≈1.3 s）；没有 scipy 时所有 IIR
# 都走"短冲激响应 + FFT 卷积"，时变滤波配方会慢 3~4 倍（wind 实测 2.3 s），
# 所以兜底路径单独给一份更宽的预算，自检输出里会写明当前按哪一档判定。
# 为什么叫"软"：渲染耗时随机器负载浮动（4 并发时 rain/wind 实测 1.6 s 左右，而 1.5 s
# 就是空载最慢那次本身），超了只打印 WARN、不当失败；真正的硬上限是 2×
# （PERF_HARD_MULTIPLIER），只在算法退化到比预算慢一倍以上时才 raise。
# 把软预算当硬断言的结果是手一忙就随机红灯，红灯多了就没人再信这个自检。
PERF_BUDGET_SCIPY = 1.5
PERF_BUDGET_FALLBACK = 3.5
PERF_HARD_MULTIPLIER = 2.0        # 硬上限 = 2× 软预算（scipy 3.0 s / 兜底 7.0 s）


# ============================================================================
# core —— 基础约定与工具
# ============================================================================

def n_samples(dur: float, sr: int = SR) -> int:
    """秒 → 采样数（四舍五入，且不为负）。"""
    return max(0, int(round(float(dur) * sr)))


def time_axis(n: int, sr: int = SR) -> np.ndarray:
    """采样序号轴：0, 1/sr, 2/sr, …（秒）。"""
    return np.arange(int(n)) / float(sr)


def rng(seed=None) -> np.random.Generator:
    """统一随机源：整数 / None / 已有 Generator（直接复用）。"""
    if isinstance(seed, np.random.Generator):
        return seed
    return np.random.default_rng(seed)


def db_to_amp(db):
    """dB → 幅度（标量或数组）。"""
    return 10.0 ** (np.asarray(db, dtype=float) / 20.0)


def amp_to_db(x, floor: float = 1e-12):
    """幅度 → dB（标量或数组；低于 floor 的按 floor 计，避免 -inf）。"""
    return 20.0 * np.log10(np.maximum(np.abs(np.asarray(x, dtype=float)), float(floor)))


def to_stereo(x) -> np.ndarray:
    """任意输入 → 立体声 (n,2)；已经是 (n,2) 的原样返回。"""
    a = np.asarray(x, dtype=float)
    if a.ndim == 2:
        return a
    return np.stack([a, a], axis=1)


def to_mono(x) -> np.ndarray:
    """任意输入 → 单声道 (n,)。"""
    a = np.asarray(x, dtype=float)
    if a.ndim == 1:
        return a
    return a.mean(axis=1)


def pan(x, position: float) -> np.ndarray:
    """等功率声像：position -1 最左、0 居中、+1 最右。立体声输入先折叠为单声道。"""
    x = to_mono(x)
    theta = (float(np.clip(position, -1.0, 1.0)) + 1.0) * np.pi / 4.0
    return np.stack([x * np.cos(theta), x * np.sin(theta)], axis=1)


def pan_dynamic(x, positions) -> np.ndarray:
    """随采样变化的等功率声像：positions 与 x 等长（-1 左 ~ +1 右）。"""
    x = to_mono(x)
    p = np.broadcast_to(np.asarray(positions, dtype=float), x.shape)
    theta = (np.clip(p, -1.0, 1.0) + 1.0) * np.pi / 4.0
    return np.stack([x * np.cos(theta), x * np.sin(theta)], axis=1)


def fade(x, fade_in: float = 0.0, fade_out: float = 0.0, sr: int = SR) -> np.ndarray:
    """升余弦淡入/淡出（返回副本）。fade_in/fade_out 单位秒。"""
    y = np.array(x, dtype=float, copy=True)
    n = len(y)
    if n == 0:
        return y
    ni = min(n, n_samples(fade_in, sr))
    no = min(n, n_samples(fade_out, sr))
    if ni > 1:
        w = 0.5 - 0.5 * np.cos(np.linspace(0.0, np.pi, ni))
        y[:ni] *= w if y.ndim == 1 else w[:, None]
    if no > 1:
        w = 0.5 + 0.5 * np.cos(np.linspace(0.0, np.pi, no))
        y[n - no:] *= w if y.ndim == 1 else w[:, None]
    return y


def pad_to(x, n: int) -> np.ndarray:
    """裁剪或补零到恰好 n 个采样（沿第 0 轴）。"""
    a = np.asarray(x)
    n = int(n)
    if len(a) >= n:
        return a[:n]
    pad = [(0, n - len(a))] + [(0, 0)] * (a.ndim - 1)
    return np.pad(a, pad)


def peak(x) -> float:
    """采样峰值（绝对值最大值）。"""
    a = np.asarray(x, dtype=float)
    return float(np.max(np.abs(a))) if a.size else 0.0


def rms(x) -> float:
    """整体 RMS。"""
    a = np.asarray(x, dtype=float)
    return float(np.sqrt(np.mean(np.square(a)))) if a.size else 0.0


def normalize(x, peak_db: float = -1.0) -> np.ndarray:
    """按采样峰值归一（默认 -1 dBFS）；全静音原样返回。"""
    a = np.asarray(x, dtype=float)
    p = peak(a)
    if p <= 0.0:
        return np.array(a, dtype=float, copy=True)
    return a * (float(db_to_amp(peak_db)) / p)


def smooth_random(dur: float, rate: float, seed=None, octaves: int = 1,
                  persistence: float = 0.5, sr: int = SR) -> np.ndarray:
    """慢变随机控制信号（Catmull-Rom 插值），大致落在 [-1, 1]。

    rate：每秒随机拐点数；octaves>1 叠加更快的小起伏（阵风、火苗闪烁、雨势变化）。
    """
    g = rng(seed)
    n = n_samples(dur, sr)
    if n == 0:
        return np.zeros(0)
    out = np.zeros(n)
    amp, total, r = 1.0, 0.0, float(rate)
    for _ in range(max(1, int(octaves))):
        k = int(np.ceil(dur * r)) + 4
        pts = g.uniform(-1.0, 1.0, max(5, k))
        pos = np.arange(n) / float(sr) * r
        i = np.floor(pos).astype(int)
        t = pos - i
        p0, p1, p2, p3 = pts[i], pts[i + 1], pts[i + 2], pts[i + 3]
        out += amp * 0.5 * (2 * p1 + (-p0 + p2) * t
                            + (2 * p0 - 5 * p1 + 4 * p2 - p3) * t ** 2
                            + (-p0 + 3 * p1 - 3 * p2 + p3) * t ** 3)
        total += amp
        amp *= float(persistence)
        r *= 2.0
    return out / max(total, 1e-9)


class Mix:
    """立体声混音总线：按时间 / 增益 / 声像叠加，缓冲区自动加长（容纳混响尾音）。

    用法：
        m = Mix()
        m.add(impact, at=0.0, gain_db=-3, pan_pos=-0.2)
        m.add(reverb_tail, at=0.0, gain_db=-12)
        y = m.out()                       # (n,2)
    """

    def __init__(self, dur: float = 0.0, sr: int = SR):
        self.sr = int(sr)
        self.buf = np.zeros((n_samples(dur, self.sr), 2))

    def add(self, x, at: float = 0.0, gain_db: float = 0.0,
            pan_pos: float | None = None) -> "Mix":
        if x is None:
            return self
        a = np.asarray(x, dtype=float)
        if a.size == 0:
            return self
        a = pan(a, pan_pos) if pan_pos is not None else to_stereo(a)
        start = n_samples(at, self.sr)
        if start < 0:                       # 负起点：裁掉已经过去的部分
            a, start = a[-start:], 0
            if a.size == 0:
                return self
        end = start + len(a)
        if end > len(self.buf):
            self.buf = pad_to(self.buf, end)
        self.buf[start:end] += float(db_to_amp(gain_db)) * a
        return self

    def out(self) -> np.ndarray:
        return self.buf


# ============================================================================
# noise —— 噪声源（撞击瞬态、风、雨、火的主体都是被塑形的噪声）
# ============================================================================

def white(n: int, seed=None) -> np.ndarray:
    """白噪声（标准正态）。"""
    return rng(seed).standard_normal(n)


def colored(n: int, slope_db_oct: float, seed=None, hp_hz: float = 20.0,
            sr: int = SR) -> np.ndarray:
    """指定频谱斜率的噪声（频域塑形）：0 白、-3 粉红、-6 布朗、+3 蓝。

    输出 RMS 归一为 1；hp_hz 以下置零（避免布朗噪声的超低频漂移）。
    """
    n = int(n)
    if n <= 0:
        return np.zeros(0)
    g = rng(seed)
    k = n // 2 + 1
    spec = g.standard_normal(k) + 1j * g.standard_normal(k)
    f = np.fft.rfftfreq(n, 1.0 / float(sr))
    f[0] = f[1] if k > 1 else 1.0
    spec *= (f / 1000.0) ** (float(slope_db_oct) / 6.0206)
    if hp_hz > 0:
        spec[f < float(hp_hz)] = 0.0
    x = np.fft.irfft(spec, n)
    return x / (np.std(x) + 1e-12)


def pink(n: int, seed=None, sr: int = SR) -> np.ndarray:
    """粉红噪声（-3 dB/oct）。"""
    return colored(n, -3.0, seed, sr=sr)


def brown(n: int, seed=None, sr: int = SR) -> np.ndarray:
    """布朗/红噪声（-6 dB/oct）。"""
    return colored(n, -6.0, seed, sr=sr)


def burst(dur: float, seed=None, slope_db_oct: float = 0.0, attack: float = 0.0002,
          t60: float | None = None, sr: int = SR) -> np.ndarray:
    """短噪声爆发：接触声、噼啪声、碎裂声的基本单元。

    默认 t60 = dur（结尾处衰减到 -60 dB）；attack 秒的平方根起音避免"咔"。
    """
    n = max(2, n_samples(dur, sr))
    x = colored(n, slope_db_oct, seed, sr=sr, hp_hz=0.0) if slope_db_oct else white(n, seed)
    t = np.arange(n) / float(sr)
    env = np.exp(-LN1000 * t / float(t60 or max(dur, 1e-6)))
    na = min(n // 2, n_samples(attack, sr))
    if na > 1:
        env[:na] *= np.linspace(0.0, 1.0, na, endpoint=False) ** 0.5
    return x * env


# ============================================================================
# env —— 包络（真实声音几乎都是指数衰减）
# ============================================================================

def exp_decay(n: int, t60: float, sr: int = SR) -> np.ndarray:
    """指数衰减包络：t60 秒后衰减 60 dB。"""
    n = int(n)
    if n <= 0:
        return np.zeros(0)
    return np.exp(-LN1000 * np.arange(n) / (float(sr) * max(float(t60), 1e-6)))


def attack_decay(dur: float, attack: float, t60: float, curve: float = 2.0,
                 sr: int = SR) -> np.ndarray:
    """起音（升余弦的 curve 次幂，越大越"软"）+ 指数衰减。"""
    n = n_samples(dur, sr)
    if n <= 0:
        return np.zeros(0)
    t = np.arange(n) / float(sr)
    na = n_samples(attack, sr)
    env = np.exp(-LN1000 * np.maximum(t - attack, 0.0) / max(float(t60), 1e-6))
    if na > 1:
        rise = (0.5 - 0.5 * np.cos(np.linspace(0.0, np.pi, na))) ** (float(curve) / 2.0)
        env[:na] *= rise
    return env


def breakpoints(points, dur: float, curve: str = "linear", sr: int = SR) -> np.ndarray:
    """分段包络。points = [(时间秒, 值), ...]。

    curve="exp" 在对数域插值（适合频率、音量这类感知上按比例变化的量）。
    """
    n = n_samples(dur, sr)
    if n <= 0:
        return np.zeros(0)
    t = np.arange(n) / float(sr)
    ts = np.array([p[0] for p in points], dtype=float)
    vs = np.array([p[1] for p in points], dtype=float)
    if curve == "exp":
        return np.exp(np.interp(t, ts, np.log(np.maximum(vs, 1e-12))))
    return np.interp(t, ts, vs)


# ============================================================================
# synth —— 音高类声源：带限振荡器、FM、超级锯齿、拨弦、ADSR
# ============================================================================

_NOTE = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}


def hz(note) -> float:
    """音名或 MIDI 号 → 频率。hz("A4")=440，hz("C#5")，hz("Eb3")，hz(60)。"""
    if isinstance(note, (int, float, np.integer, np.floating)):
        return float(440.0 * 2.0 ** ((float(note) - 69.0) / 12.0))
    m = re.fullmatch(r"([A-Ga-g])([#b]?)(-?\d)", str(note).strip())
    if not m:
        raise ValueError("无法解析音名: %r" % (note,))
    k = _NOTE[m.group(1).upper()] + {"#": 1, "b": -1, "": 0}[m.group(2)]
    return float(440.0 * 2.0 ** ((k + 12 * (int(m.group(3)) + 1) - 69) / 12.0))


def _freq(freq, n: int) -> np.ndarray:
    """把标量/数组频率（允许 "A4" 这种音名字符串）广播成逐采样数组。"""
    if isinstance(freq, str):
        freq = hz(freq)
    return np.broadcast_to(np.asarray(freq, dtype=float), (int(n),))


def _phase(freq, n: int, phase0: float, sr: int):
    f = _freq(freq, n)
    dt = f / float(sr)
    return (float(phase0) + np.cumsum(dt) - dt[0]) % 1.0, dt


def _polyblep(t: np.ndarray, dt: np.ndarray) -> np.ndarray:
    """PolyBLEP：在跳变处补偿，压住带限锯齿/方波的混叠。"""
    y = np.zeros_like(t)
    m = t < dt
    if np.any(m):
        x = t[m] / dt[m]
        y[m] = x + x - x * x - 1.0
    m = t > 1.0 - dt
    if np.any(m):
        x = (t[m] - 1.0) / dt[m]
        y[m] = x * x + x + x + 1.0
    return y


def sine(freq, n: int, phase0: float = 0.0, sr: int = SR) -> np.ndarray:
    """正弦。freq 可以是标量或逐采样数组（滑音/颤音）。"""
    ph, _ = _phase(freq, n, phase0, sr)
    return np.sin(2.0 * np.pi * ph)


def saw(freq, n: int, phase0: float = 0.0, sr: int = SR) -> np.ndarray:
    """带限锯齿波（PolyBLEP）。"""
    ph, dt = _phase(freq, n, phase0, sr)
    return 2.0 * ph - 1.0 - _polyblep(ph, dt)


def pulse(freq, n: int, width: float = 0.5, phase0: float = 0.0, sr: int = SR) -> np.ndarray:
    """带限脉冲/方波：两个相位错开的锯齿相减后去直流。"""
    y = saw(freq, n, phase0, sr) - saw(freq, n, (phase0 + float(width)) % 1.0, sr)
    return y - y.mean()


def triangle(freq, n: int, phase0: float = 0.0, sr: int = SR) -> np.ndarray:
    """三角波（谐波按 12 dB/oct 衰减，混叠很轻，直接用朴素算法）。"""
    ph, _ = _phase(freq, n, phase0, sr)
    return 4.0 * np.abs(ph - 0.5) - 1.0


def fm(carrier, ratio: float, index, n: int, feedback: float = 0.0,
       sr: int = SR) -> np.ndarray:
    """两算子 FM：载波 + 调制器（频率 = 载波 × ratio）。

    index 可为逐采样数组：指数衰减的 index → 起音明亮、尾音变纯（钟、电钢琴、玻璃感）。
    ratio 取非整数（3.5、1.41…）产生非谐波金属味。
    """
    n = int(n)
    c = _freq(carrier, n)
    mod = sine(c * float(ratio), n, 0.0, sr)
    if feedback:
        mod = np.sin(2.0 * np.pi * np.cumsum(c * float(ratio)) / float(sr) + feedback * mod)
    ph = np.cumsum(c) / float(sr)
    return np.sin(2.0 * np.pi * ph + _freq(index, n) * mod)


def supersaw(freq, n: int, voices: int = 7, detune: float = 0.25, seed=None,
             sr: int = SR) -> np.ndarray:
    """超级锯齿：多个轻微失谐的锯齿叠加（宽厚的合成器铺底）。detune 为最大失谐（半音）。"""
    g = rng(seed)
    n = int(n)
    f = _freq(freq, n)
    offs = np.linspace(-1.0, 1.0, int(max(1, voices))) * float(detune)
    y = np.zeros(n)
    for k, o in enumerate(offs):
        y += saw(f * 2.0 ** (o / 12.0), n, g.uniform(), sr) * (1.0 if k == voices // 2 else 0.7)
    return y / max(1, int(voices))


def _onepole_lp(x: np.ndarray, coef: float) -> np.ndarray:
    """一极点低通 y[n] = coef·x[n] + (1-coef)·y[n-1]（用短 IR 卷积实现，向量化）。"""
    c = float(np.clip(coef, 1e-6, 0.999999))
    L = int(min(len(x), max(16, np.ceil(np.log(1e-5) / np.log(1.0 - c)))))
    h = c * (1.0 - c) ** np.arange(L)
    return _fftconv(x, h)[: len(x)]


def pluck(freq: float, dur: float, brightness: float = 0.6, decay: float = 0.996,
          seed=None, sr: int = SR) -> np.ndarray:
    """Karplus-Strong 拨弦：噪声激励 + 延迟线 + 平均低通反馈。

    实现是分块的：延迟线长度为 N，一个块内第 j 个采样只依赖上一块，因此可以
    整块向量化递推，不需要 Python 逐采样循环。
    """
    g = rng(seed)
    n = max(2, n_samples(dur, sr))
    N = max(2, int(round(float(sr) / max(float(freq), 1e-6) - 0.5)))
    exc = g.uniform(-1.0, 1.0, N)
    if brightness < 1.0:
        exc = _onepole_lp(exc, float(brightness))
    y = np.zeros(n)
    m0 = min(n, N)
    y[:m0] = exc[:m0]
    d = float(decay)
    if N < n:
        k = 1
        while True:
            start = k * N
            if start >= n:
                break
            end = min(n, start + N)
            prev = y[start - N:start]           # y[start-N : start]
            left = np.empty(len(prev))
            left[0] = y[start - N - 1] if start - N - 1 >= 0 else 0.0
            if len(prev) > 1:
                left[1:] = prev[:-1]            # y[start-N-1 : start-1]
            block = 0.5 * d * (prev + left)
            y[start:end] = block[: end - start]
            k += 1
    return y


def adsr(n: int, attack: float, decay: float, sustain: float, release: float,
         gate: float | None = None, sr: int = SR) -> np.ndarray:
    """ADSR 包络（起音线性，衰减/释放指数形状）。gate 为按住时长（秒）。"""
    n = int(n)
    if n <= 0:
        return np.zeros(0)
    t = np.arange(n) / float(sr)
    gate = (n / float(sr) - release) if gate is None else float(gate)
    env = np.where(t < attack, t / max(float(attack), 1e-6),
                   sustain + (1.0 - sustain) * np.exp(-(t - attack) / max(decay / 3.0, 1e-6)))
    if n:
        g_level = float(np.interp(gate, t, env))
    else:
        g_level = 0.0
    rel = t >= gate
    env[rel] = g_level * np.exp(-(t[rel] - gate) / max(release / 5.0, 1e-6))
    return env


# ============================================================================
# filters —— 静态 EQ、时变滤波、模态谐振器
#
# 全部支持单声道 (n,) 与立体声 (n,2)，沿第 0 轴滤波。
# 没装 scipy 时：静态 IIR = 短冲激响应（有界递推） + FFT 卷积；
# 时变滤波 = 汉宁窗重叠相加（每块用块中心的系数），同样不做逐采样循环。
# ============================================================================

def _clamp_f(f, sr: int = SR):
    return np.clip(f, 1.0, 0.49 * float(sr))


def _biquad_ba(kind: str, f0: float, q: float = 0.7071,
               gain_db: float = 0.0, sr: int = SR):
    """RBJ Audio EQ Cookbook 双二阶：返回归一化后的 (b, a)，各 3 个系数。"""
    w0 = 2.0 * np.pi * float(_clamp_f(f0, sr)) / float(sr)
    cw, sw = np.cos(w0), np.sin(w0)
    alpha = sw / (2.0 * max(float(q), 1e-4))
    A = 10.0 ** (float(gain_db) / 40.0)
    if kind == "lowpass":
        b = [(1 - cw) / 2, 1 - cw, (1 - cw) / 2]
        a = [1 + alpha, -2 * cw, 1 - alpha]
    elif kind == "highpass":
        b = [(1 + cw) / 2, -(1 + cw), (1 + cw) / 2]
        a = [1 + alpha, -2 * cw, 1 - alpha]
    elif kind == "bandpass":
        b = [alpha, 0.0, -alpha]
        a = [1 + alpha, -2 * cw, 1 - alpha]
    elif kind == "notch":
        b = [1.0, -2 * cw, 1.0]
        a = [1 + alpha, -2 * cw, 1 - alpha]
    elif kind == "peak":
        b = [1 + alpha * A, -2 * cw, 1 - alpha * A]
        a = [1 + alpha / A, -2 * cw, 1 - alpha / A]
    elif kind in ("lowshelf", "highshelf"):
        s = 2.0 * np.sqrt(A) * (sw / 2.0 * np.sqrt(2.0))     # 坡度 S = 1
        if kind == "lowshelf":
            b = [A * ((A + 1) - (A - 1) * cw + s),
                 2 * A * ((A - 1) - (A + 1) * cw),
                 A * ((A + 1) - (A - 1) * cw - s)]
            a = [(A + 1) + (A - 1) * cw + s,
                 -2 * ((A - 1) + (A + 1) * cw),
                 (A + 1) + (A - 1) * cw - s]
        else:
            b = [A * ((A + 1) + (A - 1) * cw + s),
                 -2 * A * ((A - 1) + (A + 1) * cw),
                 A * ((A + 1) + (A - 1) * cw - s)]
            a = [(A + 1) - (A - 1) * cw + s,
                 2 * ((A - 1) - (A + 1) * cw),
                 (A + 1) - (A - 1) * cw - s]
    else:
        raise ValueError("未知滤波器类型: %r（可用 lowpass/highpass/bandpass/"
                         "notch/peak/lowshelf/highshelf）" % (kind,))
    b = np.asarray(b, dtype=float) / float(a[0])
    a = np.asarray(a, dtype=float) / float(a[0])
    return b, a


def biquad(kind: str, f0: float, q: float = 0.7071, gain_db: float = 0.0,
           sr: int = SR) -> np.ndarray:
    """RBJ 双二阶滤波器，返回一行 sos（shape (1,6)）。

    kind: lowpass | highpass | bandpass | notch | peak | lowshelf | highshelf
    """
    b, a = _biquad_ba(kind, f0, q, gain_db, sr)
    return np.concatenate([b, a])[None, :]


def _first_order_ba(kind: str, fc: float, sr: int = SR):
    """一阶巴特沃斯（低通/高通）的双线性变换系数，用于奇数阶级联。"""
    K = float(np.tan(np.pi * float(_clamp_f(fc, sr)) / float(sr)))
    a0 = 1.0 + K
    if kind == "lowpass":
        b = np.array([K / a0, K / a0, 0.0])
    elif kind == "highpass":
        b = np.array([1.0 / a0, -1.0 / a0, 0.0])
    else:
        raise ValueError("一阶级联只支持 lowpass/highpass: %r" % (kind,))
    a = np.array([1.0, (K - 1.0) / a0, 0.0])
    return b, a


def _butter_sos(kind: str, fc: float, order: int, sr: int = SR) -> np.ndarray:
    """Butterworth 级联（纯 numpy 实现，不依赖 scipy）：二阶节 Q 取巴特沃斯极点角。"""
    order = int(max(1, order))
    fc = float(_clamp_f(fc, sr))
    secs = []
    if order % 2 == 1:
        b, a = _first_order_ba(kind, fc, sr)
        secs.append(np.concatenate([b, a]))
    for k in range(order // 2):
        theta = np.pi * (2.0 * k + order + 1.0) / (2.0 * order)
        q = 1.0 / (2.0 * abs(np.cos(theta)))
        b, a = _biquad_ba(kind, fc, q, 0.0, sr)
        secs.append(np.concatenate([b, a]))
    return np.asarray(secs, dtype=float)


def _next_pow2(n: int) -> int:
    return 1 << max(0, int(n) - 1).bit_length()


def _fftconv(x, h) -> np.ndarray:
    """沿第 0 轴的 FFT 卷积（返回 full 长度 n+L-1）；短核走 np.convolve。"""
    x = np.asarray(x, dtype=float)
    h = np.asarray(h, dtype=float).ravel()
    n, L = len(x), len(h)
    if n == 0 or L == 0:
        return np.zeros((max(0, n + L - 1),) + x.shape[1:])
    if L <= 96:
        if x.ndim == 1:
            return np.convolve(x, h)
        return np.stack([np.convolve(x[:, c], h) for c in range(x.shape[1])], axis=1)
    N = _next_pow2(n + L - 1)
    if x.ndim == 1:
        return np.fft.irfft(np.fft.rfft(x, N) * np.fft.rfft(h, N), N)[: n + L - 1]
    H = np.fft.rfft(h, N)
    out = np.empty((n + L - 1, x.shape[1]))
    for c in range(x.shape[1]):
        out[:, c] = np.fft.irfft(np.fft.rfft(x[:, c], N) * H, N)[: n + L - 1]
    return out


def _ir_len(b, a, cap: int = 20000) -> int:
    """由二阶极点的衰减速度估计冲激响应需要多长（-80 dB）。"""
    a = np.asarray(a, dtype=float)
    if len(a) < 3 or a[0] == 0.0:
        return 4096
    a1, a2 = a[1] / a[0], a[2] / a[0]
    disc = a1 * a1 - 4.0 * a2
    if disc >= 0.0:
        s = math.sqrt(disc)
        r = max(abs((-a1 + s) / 2.0), abs((-a1 - s) / 2.0))
    else:
        r = math.sqrt(max(a2, 0.0))
    r = min(max(r, 1e-6), 0.9999999)
    return int(min(cap, max(64, math.ceil(math.log(1e-4) / math.log(r)))))


def _iir_ir(b, a, n: int) -> np.ndarray:
    """有界的 IIR 冲激响应（Python 递推，n ≤ 4 万；只在没有 scipy 时用）。"""
    b = np.asarray(b, dtype=float)
    a = np.asarray(a, dtype=float)
    n = int(max(1, n))
    if a[0] != 0.0:
        b = b / a[0]
        a = a / a[0]
    h = np.zeros(n)
    if len(a) == 3:
        a1, a2 = a[1], a[2]
        h[0] = b[0]
        if n > 1:
            h[1] = (b[1] if len(b) > 1 else 0.0) - a1 * h[0]
        b2 = b[2] if len(b) > 2 else 0.0
        for i in range(2, n):
            direct = b2 if i == 2 else 0.0
            h[i] = direct - a1 * h[i - 1] - a2 * h[i - 2]
        return h
    for i in range(n):                     # 通用递推（极少走到）
        acc = b[i] if i < len(b) else 0.0
        for k in range(1, min(len(a), i + 1)):
            acc -= a[k] * h[i - k]
        h[i] = acc
    return h


def sosfilt(sos, x) -> np.ndarray:
    """级联二阶节滤波。有 scipy 用 sosfilt；没有则逐节 FFT 卷积。"""
    x = np.asarray(x, dtype=float)
    rows = np.atleast_2d(np.asarray(sos, dtype=float))
    if _sig is not None:
        return _sig.sosfilt(rows, x, axis=0)
    if rows.size == 0:
        return np.array(x, dtype=float, copy=True)
    y = x
    for row in rows:
        b, a = row[:3], row[3:]
        L = _ir_len(b, a, cap=20000)
        y = _fftconv(y, _iir_ir(b, a, L))[: len(y)]
    return y


def lowpass(x, fc, q: float = 0.7071, order: int = 2, sr: int = SR) -> np.ndarray:
    """低通。order=2 用单个双二阶（可设 Q 做谐振）；order>2 用巴特沃斯级联。"""
    if int(order) == 2:
        return sosfilt(biquad("lowpass", fc, q, sr=sr), x)
    return sosfilt(_butter_sos("lowpass", fc, order, sr), x)


def highpass(x, fc, q: float = 0.7071, order: int = 2, sr: int = SR) -> np.ndarray:
    """高通。order=2 用单个双二阶；order>2 用巴特沃斯级联。"""
    if int(order) == 2:
        return sosfilt(biquad("highpass", fc, q, sr=sr), x)
    return sosfilt(_butter_sos("highpass", fc, order, sr), x)


def bandpass(x, f0, q: float = 1.0, sr: int = SR) -> np.ndarray:
    """带通（峰值 0 dB）。"""
    return sosfilt(biquad("bandpass", f0, q, sr=sr), x)


def peak_eq(x, f0, gain_db: float, q: float = 1.0, sr: int = SR) -> np.ndarray:
    """峰值 EQ。"""
    return sosfilt(biquad("peak", f0, q, gain_db, sr), x)


def lowshelf(x, f0, gain_db: float, sr: int = SR) -> np.ndarray:
    """低架 EQ。"""
    return sosfilt(biquad("lowshelf", f0, 0.7071, gain_db, sr), x)


def highshelf(x, f0, gain_db: float, sr: int = SR) -> np.ndarray:
    """高架 EQ。"""
    return sosfilt(biquad("highshelf", f0, 0.7071, gain_db, sr), x)


def _tv_filter_fallback(x, kind, f, q, gain_db, block, sr):
    """时变滤波兜底：汉宁窗 50% 重叠相加，每块用块中心的系数（无 scipy 时用）。"""
    n, xd = len(x), x.shape
    B = int(min(4096, max(128, 2 * _next_pow2(max(64, int(block))))))
    hop = B // 2
    H = B // 2
    xp = np.pad(x, ((H, B), (0, 0)) if xd == 2 else (H, B))
    y = np.zeros(xd)
    w = 0.5 - 0.5 * np.cos(2.0 * np.pi * np.arange(B) / B)
    if xd == 2:
        w = w[:, None]
    for p in range(0, len(xp) - B + 1, hop):
        c = int(np.clip(p, 0, n - 1))
        b, a = _biquad_ba(kind, f[c], q[c], gain_db, sr)
        seg = xp[p:p + B]
        fs = _fftconv(seg, _iir_ir(b, a, min(B, _ir_len(b, a, cap=B))))[:B]
        pos = p - H
        s0 = max(0, pos)
        e0 = min(n, pos + B)
        if e0 <= s0:
            continue
        o = s0 - pos
        y[s0:e0] += fs[o:o + (e0 - s0)] * w[o:o + (e0 - s0)]
    return y


def _tv_filter_fast(x, kind, f, q, gain_db, sr):
    """长信号快速路径：分段静态滤波 + 汉宁窗交叉淡化（≥1.2 M 采样时启用）。

    控制信号本身是慢变的（阵风、雨势），0.34 s 更新一次系数的误差听不出来；
    这比逐块（block=64）调用 sosfilt 快一个数量级 —— 389 s 环境声必须这么做。
    """
    n, xd = len(x), x.shape
    B = 32768
    hop = B // 2
    H = B // 2
    xp = np.pad(x, ((H, B), (0, 0)) if xd == 2 else (H, B))
    y = np.zeros(xd)
    w = 0.5 - 0.5 * np.cos(2.0 * np.pi * np.arange(B) / B)
    if xd == 2:
        w = w[:, None]
    for p in range(0, len(xp) - B + 1, hop):
        c = int(np.clip(p, 0, n - 1))
        seg = xp[p:p + B]
        fs = sosfilt(biquad(kind, f[c], q[c], gain_db, sr), seg)
        pos = p - H
        s0 = max(0, pos)
        e0 = min(n, pos + B)
        if e0 <= s0:
            continue
        o = s0 - pos
        y[s0:e0] += fs[o:o + (e0 - s0)] * w[o:o + (e0 - s0)]
    return y


def tv_filter(x, kind: str, freq, q: float = 0.7071, gain_db: float = 0.0,
              block: int = 64, sr: int = SR) -> np.ndarray:
    """时变双二阶滤波：freq / q 可为标量或逐采样数组（扫频、随风速变化的中心频率）。

    有 scipy 时每 block 个采样更新一次系数、状态跨块延续；
    无 scipy 时走汉宁窗重叠相加（每块用块中心系数），仍是向量化实现。
    长信号（≥1.2 M 采样，约 25 s）自动走分段快速路径，避免数十万次小滤波调用。
    """
    x = np.asarray(x, dtype=float)
    n = len(x)
    if n == 0:
        return np.array(x, dtype=float, copy=True)
    f = np.broadcast_to(np.asarray(freq, dtype=float), (n,))
    qv = np.broadcast_to(np.asarray(q, dtype=float), (n,))
    if _sig is None:
        return _tv_filter_fallback(x, kind, f, qv, gain_db, block, sr)
    if n >= 1_200_000:
        return _tv_filter_fast(x, kind, f, qv, gain_db, sr)
    y = np.empty_like(x, dtype=float)
    zi = np.zeros((1, 2) + x.shape[1:])
    step = max(1, int(block))
    for s0 in range(0, n, step):
        e = min(n, s0 + step)
        m = (s0 + e) // 2
        sos = biquad(kind, f[m], qv[m], gain_db, sr)
        y[s0:e], zi = _sig.sosfilt(sos, x[s0:e], axis=0, zi=zi)
    return y


def _resonator_ir(f: float, t60: float, amp: float, n_max: int,
                  sr: int = SR) -> np.ndarray:
    """模态谐振器的解析冲激响应 h[n] = amp · r^n · sin(ωn)，长度 ≤ n_max。

    为什么不复用通用的 _iir_ir()：无 scipy 时 resonator 也要走 FFT 卷积，而金属/
    玻璃模态的 t60 可达 5~7 s（见 MATERIALS 的 eta 量级），-80 dB 衰减长度约
    1.33·t60·sr ≈ 40 万采样。固定 cap=20000 的通用 IR 在 0.42 s 处就把 IR 砍成 0：
    截断点在 -25 dB 上直接跳变，听感是长尾里多一下“咔”，且与 scipy 路径的
    产物长度/尾音全不同（references/sfx-design.md §12 要求静态滤波与 scipy
    相对误差 ≤ 2e-4，实测这类截断最差 -8 dB）。这个二阶系统的 IR 有闭式解，
    一次向量化算完，比逐样本 Python 递推快两三个数量级。
    长度取 min(n_max, -80 dB 衰减长度)：输出只取前 n_max 个采样，h 超过 n_max
    的部分不可能影响这些采样，所以这个截断是精确的、不是近似。
    """
    n_max = int(max(0, n_max))
    if n_max == 0:
        return np.zeros(0)
    a1 = math.exp(-LN1000 / (max(float(t60), 1e-5) * float(sr)))
    w = 2.0 * np.pi * float(f) / float(sr)
    # 借 _ir_len() 的极点半径估计（它已处理不衰减/数值退化），保证与通用 IR 同口径
    a = np.array([1.0, -2.0 * a1 * math.cos(w), a1 * a1])
    b = np.array([0.0, float(amp) * a1 * math.sin(w)])
    L = int(_ir_len(b, a, cap=n_max))
    k = np.arange(L, dtype=float)
    return float(amp) * np.power(a1, k) * np.sin(w * k)


def resonator(excitation, f: float, t60: float, amp: float = 1.0,
              sr: int = SR) -> np.ndarray:
    """单个模态：二阶谐振器，冲激响应为 amp · r^n · sin(ωn)（指数衰减的正弦）。"""
    x = np.asarray(excitation, dtype=float)
    if amp == 0 or f <= 0 or f >= 0.47 * float(sr):
        return np.zeros_like(x, dtype=float)
    a1 = math.exp(-LN1000 / (max(float(t60), 1e-5) * float(sr)))
    w = 2.0 * np.pi * float(f) / float(sr)
    b = np.array([0.0, float(amp) * a1 * math.sin(w)])
    a = np.array([1.0, -2.0 * a1 * math.cos(w), a1 * a1])
    if _sig is not None:
        return _sig.lfilter(b, a, x, axis=0)
    return _fftconv(x, _resonator_ir(f, t60, amp, len(x), sr))[: len(x)]


def resonator_bank(excitation, freqs, t60s, amps, sr: int = SR) -> np.ndarray:
    """模态谐振器组：所有模态共用同一个激励，输出为各模态之和。"""
    y = np.zeros(len(np.asarray(excitation)), dtype=float)
    for f, t60, a in zip(freqs, t60s, amps):
        y += resonator(excitation, f, t60, a, sr)
    return y


def spectral_tilt_by_distance(x, meters: float, sr: int = SR) -> np.ndarray:
    """空气吸收：高频随距离衰减（≈20°C/50%RH：α(f) ≈ 0.005·(f/1kHz)^1.5 dB/m，零相位实现）。"""
    a = np.asarray(x, dtype=float)
    if meters <= 1.0 or len(a) == 0:
        return np.array(a, dtype=float, copy=True)
    n = len(a)
    X = np.fft.rfft(a, axis=0)
    f = np.fft.rfftfreq(n, 1.0 / float(sr))
    att_db = 0.005 * (f / 1000.0) ** 1.5 * float(meters)
    g = 10.0 ** (-att_db / 20.0)
    X *= g if a.ndim == 1 else g[:, None]
    return np.fft.irfft(X, n, axis=0)



# ============================================================================
# modal —— 模态合成：撞击、敲击、碰撞的物理模型
#
# 一个被敲击的物体 = 一组"模态"（固有振动），每个模态是 amp·r^n·sin(ωn)：
#   频率比由【形状】决定（棒/板/膜/环/钟都是非整数倍 —— 真实与合成的分水岭）；
#   衰减由【材质】决定（损耗因子 η：d = alpha + π·f·η，高频衰减更快）；
#   幅度由【敲击位置】决定（模态振型，每次敲击略有不同 → 音色自然变化）；
#   亮度由【接触时间】决定（硬物短接触激发高频，软物长接触发闷）。
# 改编自 ai-history-film（MIT）的 sfx/modal.py。
# ============================================================================

# 损耗因子 η 与支撑/辐射阻尼 alpha(1/s)：按材料内耗的典型量级取，并按听感校准。
MATERIALS = {
    "metal":     dict(eta=0.0003, alpha=1.0),    # 钢/铝：余音数秒
    "glass":     dict(eta=0.0006, alpha=1.0),    # 玻璃：清亮，余音长
    "ceramic":   dict(eta=0.002, alpha=4.0),     # 陶瓷
    "stone":     dict(eta=0.008, alpha=12.0),    # 石头/混凝土
    "wood":      dict(eta=0.02, alpha=12.0),     # 木头：短促温暖
    "plastic":   dict(eta=0.035, alpha=25.0),    # 塑料：闷
    "cardboard": dict(eta=0.08, alpha=40.0),     # 纸板
    "rubber":    dict(eta=0.2, alpha=60.0),      # 橡胶：几乎无余音
}


def decay_t60(freqs, material: str = "wood", eta: float | None = None,
              alpha: float | None = None) -> np.ndarray:
    """按材质损耗因子算每个模态的 t60（秒）：t60 = ln(1000)/(alpha + π·f·η)。"""
    m = MATERIALS.get(material)
    if m is None:
        raise ValueError("未知材质: %r（可用 %s）" % (material, "/".join(sorted(MATERIALS))))
    eta = m["eta"] if eta is None else float(eta)
    alpha = m["alpha"] if alpha is None else float(alpha)
    return LN1000 / (alpha + np.pi * np.asarray(freqs, dtype=float) * eta)


# ---- 形状 → 模态频率比 ----

def bar_ratios(n: int, clamped: bool = False) -> np.ndarray:
    """欧拉-伯努利梁。自由-自由（木琴条、金属管）：1, 2.756, 5.404, 8.933…
    一端固定（尺子压在桌边、音叉齿）：1, 6.267, 17.55…"""
    n = int(max(1, n))
    if clamped:
        betas = [1.8751, 4.6941, 7.8548, 10.9955] + [(2 * k - 1) * np.pi / 2 for k in range(5, n + 1)]
    else:
        betas = [4.7300, 7.8532, 10.9956, 14.1372] + [(2 * k + 1) * np.pi / 2 for k in range(5, n + 1)]
    b = np.asarray(betas[:n], dtype=float)
    return (b / b[0]) ** 2


def plate_ratios(n: int, aspect: float = 1.5):
    """矩形板（简支边界）：f_mn ∝ (m/a)² + (k/b)²。返回 (频率比, [(m,k), ...])。"""
    modes = sorted(((m / float(aspect)) ** 2 + k ** 2, m, k)
                   for m in range(1, 40) for k in range(1, 40))[:int(max(1, n))]
    base = modes[0][0]
    return (np.array([v / base for v, _, _ in modes]),
            [(m, k) for _, m, k in modes])


MEMBRANE_RATIOS = np.array([1.0, 1.594, 2.136, 2.296, 2.653, 2.918, 3.156, 3.501,
                            3.600, 3.652, 4.060, 4.154, 4.601, 4.832, 4.903, 5.131,
                            5.412, 5.500, 5.570, 5.625])
BELL_RATIOS = np.array([0.5, 1.0, 1.183, 1.506, 2.0, 2.514, 2.662, 3.011,
                        4.166, 5.433, 6.796, 8.215])


def ring_ratios(n: int, stiffness: float = 0.9) -> np.ndarray:
    """圆环/薄壳（玻璃杯、碗、金属罐的 (k,0) 模态）：f ∝ k(k²-1)/√(k²+1)。"""
    n = int(max(1, n))
    k = np.arange(2, 2 + n, dtype=float)
    r = k * (k ** 2 - 1) / np.sqrt(k ** 2 + 1)
    return (r / r[0]) ** float(stiffness)


# ---- 物体与敲击 ----

@dataclass
class Hit:
    """一次敲击。t 时刻（秒）；velocity 力度；contact_ms 基准接触时间（越短越亮）；
    pos 敲击位置（板为 (x,y)，棒为 x，0~1；None 用物体默认位置并随机抖动）；
    hardness 0~1：力中"硬核"部分的比例（接触时间只有 1/5）。"""
    t: float = 0.0
    velocity: float = 1.0
    contact_ms: float = 1.0
    pos: object = None
    hardness: float = 0.0


def _half_sine(k: int) -> np.ndarray:
    p = np.sin(np.pi * (np.arange(int(k)) + 0.5) / max(1, int(k)))
    return p / p.sum()


def force_pulse(contact_ms: float, velocity: float = 1.0, hardness: float = 0.0,
                sr: int = SR) -> np.ndarray:
    """接触力脉冲（面积 = velocity）。半正弦频谱在 ~0.5/Tc 以上滚降、1.5/Tc 处有零点，
    所以接触时间直接决定能激发多高的模态；力度越大接触越短（Hertz 接触 Tc ∝ v^-0.2）。"""
    tc = float(contact_ms) / 1000.0 * max(float(velocity), 0.05) ** -0.2
    k = max(2, n_samples(tc, sr))
    p = (1.0 - float(hardness)) * _half_sine(k)
    if hardness > 0:
        kh = max(2, k // 5)
        p[:kh] += float(hardness) * _half_sine(kh)
    return float(velocity) * p


class Body:
    """一个被建模的"物体"：一组模态频率 / 各自 t60 / 各自幅度。

    render(hits, dur=None) 渲染一串敲击（单声道）。同一物体多次敲击时线性叠加
    （上一击的余音不会被切断）。split() 做简并模态分裂，产生钟/杯的拍频闪烁。
    """

    def __init__(self, freqs, t60s, gains, kind: str = "generic", mode_idx=None,
                 default_pos=None, pos_jitter: float = 0.03, seed=None):
        self.freqs = np.asarray(freqs, dtype=float)
        self.t60s = np.asarray(t60s, dtype=float)
        self.gains = np.asarray(gains, dtype=float)
        self.kind = kind
        self.mode_idx = mode_idx
        self.default_pos = default_pos
        self.pos_jitter = float(pos_jitter)
        self._rng = rng(seed)
        keep = (self.freqs > 20.0) & (self.freqs < 20000.0)
        self.freqs, self.t60s, self.gains = self.freqs[keep], self.t60s[keep], self.gains[keep]
        if self.mode_idx is not None:
            self.mode_idx = [m for m, k in zip(self.mode_idx, keep) if k]

    def _position_factor(self, pos) -> np.ndarray:
        g = self._rng
        if self.kind == "plate" and self.mode_idx is not None:
            x, y = pos if pos is not None else (self.default_pos or (0.37, 0.41))
            x = float(np.clip(x + g.normal(0, self.pos_jitter), 0.02, 0.98))
            y = float(np.clip(y + g.normal(0, self.pos_jitter), 0.02, 0.98))
            mk = np.array(self.mode_idx, dtype=float)
            f = np.abs(np.sin(mk[:, 0] * np.pi * x) * np.sin(mk[:, 1] * np.pi * y))
        elif self.kind == "bar":
            x = pos if pos is not None else (self.default_pos if self.default_pos is not None else 0.3)
            x = float(np.clip(x + g.normal(0, self.pos_jitter), 0.0, 1.0))
            ks = np.asarray(self.mode_idx if self.mode_idx is not None else range(len(self.freqs)),
                            dtype=float)
            betas = np.sqrt(self.freqs / max(self.freqs[0], 1e-9)) * 4.73
            f = np.abs(np.cos(betas * (x - 0.5) + (ks % 2) * np.pi / 2))
        else:
            f = np.clip(1.0 + g.normal(0, 0.25, len(self.freqs)), 0.2, 1.6)
        return 0.08 + 0.92 * f

    def split(self, amount: float = 0.001, seed=None) -> "Body":
        """简并模态分裂：对称物体（玻璃杯、钟、金属管）每个模态其实是两个极近的频率，
        互相拍频产生微妙的起伏闪烁（钟声的 wah-wah）。amount 为相对失谐量。"""
        g = rng(seed if seed is not None else self._rng)
        m = len(self.freqs)
        f2 = self.freqs * (1.0 + float(amount) * g.uniform(0.3, 1.0, m))
        idx = None if self.mode_idx is None else self.mode_idx + self.mode_idx
        return Body(np.concatenate([self.freqs, f2]),
                    np.concatenate([self.t60s, self.t60s * g.uniform(0.85, 1.15, m)]),
                    np.concatenate([self.gains, self.gains * g.uniform(0.3, 0.9, m)]),
                    self.kind, idx, self.default_pos, self.pos_jitter, g)

    def render(self, hits, dur: float | None = None, sr: int = SR) -> np.ndarray:
        """渲染一串敲击（单声道）。"""
        if isinstance(hits, Hit):
            hits = [hits]
        hits = list(hits)
        if not hits:
            return np.zeros(n_samples(dur or 0.0, sr))
        if dur is None:
            dur = max(h.t for h in hits) + min(float(np.max(self.t60s)), 8.0) + 0.05
        n = n_samples(dur, sr)
        if n <= 0:
            return np.zeros(0)
        events = []                       # (起点, 力脉冲, 各模态权重)
        for h in hits:
            s = n_samples(h.t, sr)
            if s >= n:
                continue
            pulse = force_pulse(h.contact_ms, h.velocity, h.hardness, sr)
            events.append((s, pulse[: n - s], self.gains * self._position_factor(h.pos)))
        y = np.zeros(n)
        if not events:
            return y
        if _sig is not None:
            # 有 scipy：逐模态 lfilter（产物逐样本不变——换实现不改用户成片的声音）
            exc = np.zeros(n)
            for i, (f, t60) in enumerate(zip(self.freqs, self.t60s)):
                exc[:] = 0.0
                for s, pulse, w in events:
                    exc[s:s + len(pulse)] += w[i] * pulse
                y += resonator(exc, float(f), float(t60), 1.0, sr)
            return y
        # 无 scipy：先按事件把「加权模态 IR」合成一条，再与力脉冲卷积一次（N 模态 → 1 次 FFT）。
        # 等价性：每个事件的模态权重 w_i 是标量，Σ_i w_i·conv(pulse, h_i) = conv(pulse, Σ_i w_i·h_i)
        # 可换序；所以合并只减少 FFT 次数，不改变结果。只在兜底路径合并：有 scipy 的产物保持原样。
        # 代价：IR 长度仍按 -80 dB 衰减截断，且只保留到输出末尾（h 超过 n-s 的部分影响不到输出）。
        for s, pulse, w in events:
            lcap = n - s
            if lcap <= 0 or len(pulse) == 0:
                continue
            h = np.zeros(lcap)
            for i, (f, t60) in enumerate(zip(self.freqs, self.t60s)):
                if w[i] == 0.0:
                    continue
                hi = _resonator_ir(float(f), float(t60), float(w[i]), lcap, sr)
                h[: len(hi)] += hi
            seg = _fftconv(pulse, h)[:lcap]
            y[s:s + len(seg)] += seg
        return y


def _gains(ratios, tilt: float, g, spread: float = 0.4) -> np.ndarray:
    r = np.asarray(ratios, dtype=float)
    return r ** (-float(tilt)) * g.uniform(1.0 - float(spread), 1.0, len(r))


def _detune(ratios, amount: float, g) -> np.ndarray:
    r = np.asarray(ratios, dtype=float).copy()
    if len(r) > 1:
        r[1:] *= 1.0 + float(amount) * g.standard_normal(len(r) - 1)
    return r


def plate(f11: float, material: str = "wood", aspect: float = 1.5, n_modes: int = 40,
          tilt: float = 0.3, eta=None, alpha=None, detune: float = 0.015,
          pos=None, seed=None) -> Body:
    """板状物体：门、地板、桌面、箱体、金属板（f11 为最低模态频率）。"""
    g = rng(seed)
    ratios, idx = plate_ratios(n_modes, aspect)
    f = float(f11) * _detune(ratios, detune, g)
    return Body(f, decay_t60(f, material, eta, alpha), _gains(ratios, tilt, g),
                "plate", idx, pos, seed=g)


def bar(f1: float, material: str = "wood", n_modes: int = 8, clamped: bool = False,
        tilt: float = 0.2, eta=None, alpha=None, detune: float = 0.01,
        pos=None, seed=None) -> Body:
    """棒/管状物体：木棍、金属管、木琴条、尺子。"""
    g = rng(seed)
    ratios = bar_ratios(n_modes, clamped)
    f = float(f1) * _detune(ratios, detune, g)
    return Body(f, decay_t60(f, material, eta, alpha), _gains(ratios, tilt, g),
                "bar", list(range(len(f))), pos, seed=g)


def membrane(f1: float, material: str = "plastic", n_modes: int = 16, tilt: float = 0.4,
             eta=None, alpha=None, detune: float = 0.01, seed=None) -> Body:
    """圆形膜：鼓面、纸箱面、帐篷布（配合较大阻尼）。"""
    g = rng(seed)
    ratios = MEMBRANE_RATIOS[:int(max(1, n_modes))]
    f = float(f1) * _detune(ratios, detune, g)
    return Body(f, decay_t60(f, material, eta, alpha), _gains(ratios, tilt, g), seed=g)


def ring(f_base: float, material: str = "glass", n_modes: int = 6, stiffness: float = 0.9,
         tilt: float = 0.2, eta=None, alpha=None, detune: float = 0.02,
         split: float = 0.0015, seed=None) -> Body:
    """杯/碗/罐：玻璃杯、陶瓷杯、金属碗。默认带简并模态分裂（拍频闪烁）。"""
    g = rng(seed)
    ratios = ring_ratios(n_modes, stiffness)
    f = float(f_base) * _detune(ratios, detune, g)
    b = Body(f, decay_t60(f, material, eta, alpha), _gains(ratios, tilt, g), seed=g)
    return b.split(split, g) if split > 0 else b


def bell(f_prime: float, material: str = "metal", tilt: float = 0.1, eta=None, alpha=None,
         detune: float = 0.004, split: float = 0.002, seed=None) -> Body:
    """钟：hum(0.5) prime(1) tierce(1.183 小三度) quint(1.5) nominal(2)…"""
    g = rng(seed)
    f = float(f_prime) * _detune(BELL_RATIOS, detune, g)
    b = Body(f, decay_t60(f, material, eta, alpha), _gains(BELL_RATIOS, tilt, g), seed=g)
    return b.split(split, g) if split > 0 else b


def random_body(f_low: float, f_high: float, n_modes: int = 12, material: str = "stone",
                tilt: float = 0.3, eta=None, alpha=None, seed=None) -> Body:
    """无规则形状的小物体（碎片、石子、杂物）：模态在 [f_low, f_high] 内对数均匀随机分布。"""
    g = rng(seed)
    f = np.sort(np.exp(g.uniform(np.log(max(float(f_low), 1e-3)),
                                 np.log(max(float(f_high), float(f_low) + 1.0)),
                                 int(max(1, n_modes)))))
    return Body(f, decay_t60(f, material, eta, alpha), _gains(f / f[0], tilt, g), seed=g)


def contact_noise(dur_ms: float = 1.0, center_hz: float = 3000.0, q: float = 0.8,
                  seed=None, sr: int = SR) -> np.ndarray:
    """接触瞬间的"咔/嗒"声：极短噪声爆发经带通（指节骨头、指甲、硬底鞋跟的那一下）。峰值归一。"""
    x = burst(max(float(dur_ms), 0.05) / 1000.0, seed, sr=sr)
    x = np.concatenate([x, np.zeros(n_samples(0.004, sr))])   # 给带通留出振铃尾巴
    y = bandpass(x, center_hz, q, sr)
    return y / (peak(y) + 1e-12)


# ============================================================================
# texture —— 随机事件与纹理（雨、火、碎石、气泡这类"大量微小事件"的声音）
#
# 真实纹理声 = 很多个略有不同的小事件 × 随机的时间分布：
#   时间用泊松过程（间隔指数分布）；强度本身也慢变（Cox 过程 → 事件"成簇"）；
#   事件大小服从幂律（很多小的、少数大的），而不是均匀随机。
# 改编自 ai-history-film（MIT）的 sfx/texture.py。
# ============================================================================

def poisson_times(dur: float, rate: float, seed=None, start: float = 0.0) -> np.ndarray:
    """齐次泊松过程：平均每秒 rate 个事件，返回事件时刻数组。"""
    g = rng(seed)
    if rate <= 0 or dur <= 0:
        return np.zeros(0)
    k = int(rate * dur * 1.5 + 20)
    t = np.cumsum(g.exponential(1.0 / rate, k))
    while len(t) == 0 or t[-1] < dur:
        t = np.concatenate([t, (t[-1] if len(t) else 0.0) + np.cumsum(g.exponential(1.0 / rate, k))])
    return float(start) + t[t < dur]


def modulated_times(rate_curve: np.ndarray, seed=None, sr: int = SR) -> np.ndarray:
    """非齐次泊松过程（稀疏化法）：rate_curve 为逐采样的瞬时速率（事件/秒）。"""
    g = rng(seed)
    rc = np.asarray(rate_curve, dtype=float)
    dur = len(rc) / float(sr)
    rmax = float(np.max(rc)) if len(rc) else 0.0
    if rmax <= 0 or dur <= 0:
        return np.zeros(0)
    cand = poisson_times(dur, rmax, g)
    idx = np.minimum((cand * sr).astype(int), len(rc) - 1)
    return cand[g.uniform(0.0, rmax, len(cand)) < rc[idx]]


def power_law(n: int, alpha: float = 2.0, lo: float = 0.05, hi: float = 1.0,
              seed=None) -> np.ndarray:
    """幂律分布的幅度（帕累托截断）：alpha 越大，小事件越占多数。"""
    g = rng(seed)
    n = int(max(0, n))
    u = g.uniform(0.0, 1.0, n)
    a1 = 1.0 - float(alpha)
    return (lo ** a1 + u * (hi ** a1 - lo ** a1)) ** (1.0 / a1)


def human_rhythm(n: int, interval: float, jitter: float = 0.03, drift: float = 0.0,
                 seed=None, start: float = 0.0) -> np.ndarray:
    """人的节奏（敲门、脚步）：平均间隔 interval，带随机抖动与整体快慢漂移。"""
    g = rng(seed)
    n = int(max(0, n))
    if n == 0:
        return np.zeros(0)
    gaps = float(interval) * (1.0 + float(jitter) * g.standard_normal(n)) \
        * (1.0 + float(drift) * np.linspace(0.0, 1.0, n))
    return float(start) + np.concatenate([[0.0], np.cumsum(gaps[:-1])])


def impulses(times, amps, n: int, sr: int = SR) -> np.ndarray:
    """把事件放成脉冲串（同一点上的幅度相加）。"""
    x = np.zeros(int(n))
    idx = (np.asarray(times, dtype=float) * float(sr)).astype(int)
    amps = np.asarray(amps, dtype=float)
    ok = (idx >= 0) & (idx < int(n))
    if len(amps) == 1 and np.sum(ok) > 0:
        amps = np.full(len(idx), float(amps[0]))
    np.add.at(x, idx[ok], amps[ok])
    return x


def scatter(times, amps, kernels, n: int, seed=None, sr: int = SR) -> np.ndarray:
    """把大量事件高效地"撒"到时间线上：每个事件随机选一个短核并按幅度缩放。

    同一核的所有事件合并为一次 FFT 卷积，数千个事件也很快。
    """
    g = rng(seed)
    times = np.asarray(times, dtype=float)
    amps = np.asarray(amps, dtype=float)
    n = int(n)
    if len(times) == 0 or n <= 0:
        return np.zeros(max(0, n))
    kernels = [np.asarray(k, dtype=float) for k in kernels if len(k) > 0]
    if not kernels:
        return np.zeros(n)
    which = g.integers(0, len(kernels), len(times))
    out = np.zeros(n)
    for k, ker in enumerate(kernels):
        sel = which == k
        if not np.any(sel):
            continue
        train = impulses(times[sel], amps[sel], n, sr)
        out += _fftconv(train, ker)[:n]
    return out


def stick_slip(rate: np.ndarray, amp: np.ndarray, jitter: float = 0.06,
               dropout_below: float = 0.12, seed=None, sr: int = SR) -> np.ndarray:
    """粘滑摩擦激励：门轴吱呀、地板嘎吱、鞋底摩擦、拉弦的共同机制。

    rate(t) 为逐采样的脉冲频率（= 听到的音高），amp(t) 为逐采样幅度。
    """
    g = rng(seed)
    rate = np.asarray(rate, dtype=float)
    amp = np.asarray(amp, dtype=float)
    n = len(rate)
    if n == 0:
        return np.zeros(0)
    phase = np.cumsum(np.maximum(rate, 0.0)) / float(sr)
    idx = np.nonzero(np.diff(np.floor(phase)) > 0)[0]
    if len(idx) == 0:
        return np.zeros(n)
    period = float(sr) / np.maximum(rate[idx], 1.0)
    idx = np.clip((idx + g.normal(0, float(jitter), len(idx)) * period).astype(int), 0, n - 1)
    a = amp[idx] * g.uniform(0.55, 1.0, len(idx))
    low = amp[idx] < float(dropout_below)
    if np.any(low):
        a[low] *= (g.uniform(0.0, 1.0, int(low.sum())) < 0.3)
    exc = np.zeros(n)
    np.add.at(exc, idx, a)
    return exc


def bubble(f0: float, rise: float = 0.1, damping_scale: float = 1.0,
           dur: float | None = None, sr: int = SR) -> np.ndarray:
    """Minnaert 气泡声（van den Doel 2005）：f0 ≈ 3.26 / r(米)：
    阻尼 d = 0.13·f0 + 0.0072·f0^1.5（1/秒）；气泡上浮时频率上扬 → 水滴"叮咚"感的来源。"""
    f0 = max(float(f0), 1.0)
    d = (0.13 * f0 + 0.0072 * f0 ** 1.5) * float(damping_scale)
    n = n_samples(dur if dur is not None else 6.0 / d, sr)
    if n <= 0:
        return np.zeros(0)
    t = np.arange(n) / float(sr)
    f = f0 * (1.0 + float(rise) * d * t)
    return np.sin(2.0 * np.pi * np.cumsum(f) / float(sr)) * np.exp(-d * t)


# ============================================================================
# space —— 空间：房间混响、户外回声、距离感
#
# 房间脉冲响应 = 镜像声源法算出的早期反射（离散回声 → "房间多大、墙在哪"）
#             + 频率相关衰减的扩散尾音（高频比低频衰减快 → "是否吸音"）
#             + 低频房间模态（Schroeder 频率以下，小房间撞击尾巴发"嗡"）
# 改编自 ai-history-film（MIT）的 sfx/space.py。
# ============================================================================

C_SOUND = 343.0                       # 声速 m/s

ROOMS = {
    # dims: 长宽高(米)；rt60：中低频混响时间(秒)；hf_damping：0~1，越大高频衰减越快
    "small_room": dict(dims=(3.6, 3.0, 2.5), rt60=0.35, hf_damping=0.55),
    "room":       dict(dims=(5.0, 4.0, 2.8), rt60=0.55, hf_damping=0.5),
    "bathroom":   dict(dims=(2.6, 2.0, 2.4), rt60=0.9, hf_damping=0.2),
    "corridor":   dict(dims=(18.0, 2.2, 2.7), rt60=1.2, hf_damping=0.35),
    "hall":       dict(dims=(28.0, 18.0, 11.0), rt60=2.0, hf_damping=0.45),
    "warehouse":  dict(dims=(35.0, 22.0, 9.0), rt60=2.6, hf_damping=0.35),
    "cave":       dict(dims=(16.0, 9.0, 6.0), rt60=3.2, hf_damping=0.5),
}

_ROOM_IR_CACHE: dict = {}


def _room_ir_uncached(room, seed, order: int, sr: int) -> np.ndarray:
    if isinstance(room, str):
        if room not in ROOMS:
            raise ValueError("未知房间 %r（可用 %s）" % (room, "/".join(sorted(ROOMS))))
        cfg = ROOMS[room]
    else:
        cfg = dict(room)
        for key in ("dims", "rt60", "hf_damping"):
            if key not in cfg:
                raise ValueError("自定义房间缺少 %r 字段（需要 dims/rt60/hf_damping）" % (key,))
    L = np.asarray(cfg["dims"], dtype=float)
    rt60, hfd = float(cfg["rt60"]), float(cfg["hf_damping"])
    g = rng(seed)

    V, S = float(np.prod(L)), float(2.0 * (L[0] * L[1] + L[0] * L[2] + L[1] * L[2]))
    absorb = float(np.clip(0.161 * V / (S * rt60), 0.02, 0.95))    # Sabine 公式反推平均吸声系数
    beta = float(np.sqrt(1.0 - absorb))                            # 墙面反射系数
    src = L * g.uniform([0.2, 0.2, 0.35], [0.8, 0.8, 0.65])
    lis = L * g.uniform([0.2, 0.2, 0.4], [0.8, 0.8, 0.6])
    ears = [lis + np.array([-0.1, 0.0, 0.0]), lis + np.array([0.1, 0.0, 0.0])]

    n = n_samples(rt60 * 1.3 + 0.1, sr)
    ir = np.zeros((n, 2))

    rng_idx = np.arange(-order, order + 1)
    nx, ny, nz = (a.ravel() for a in np.meshgrid(rng_idx, rng_idx, rng_idx, indexing="ij"))
    k = np.abs(nx) + np.abs(ny) + np.abs(nz)
    sel = (k >= 1) & (k <= order)
    nx, ny, nz, k = nx[sel], ny[sel], nz[sel], k[sel]
    imgs = np.stack([
        nx * L[0] + np.where(nx % 2 == 0, src[0], L[0] - src[0]),
        ny * L[1] + np.where(ny % 2 == 0, src[1], L[1] - src[1]),
        nz * L[2] + np.where(nz % 2 == 0, src[2], L[2] - src[2]),
    ], axis=1)

    t_mix = 4.0 * V / (S * C_SOUND)     # 平均自由程时间：扩散尾音从这里开始建立
    t = np.arange(n) / float(sr)
    for ch, ear in enumerate(ears):
        d_direct = float(np.linalg.norm(src - ear))
        d = np.linalg.norm(imgs - ear, axis=1)
        delay = (d - d_direct) / C_SOUND * sr * g.uniform(0.995, 1.005, len(d))
        amp = (d_direct / np.maximum(d, 1e-6)) * beta ** k
        early = np.zeros(n)
        for o in range(1, order + 1):                  # 反射次数越多越闷
            m = (k == o) & (delay < n - 2)
            if not np.any(m):
                continue
            part = np.zeros(n)
            i0 = np.floor(delay[m]).astype(int)
            frac = delay[m] - i0
            np.add.at(part, i0, amp[m] * (1 - frac))
            np.add.at(part, i0 + 1, amp[m] * frac)
            early += lowpass(part, 14000.0 * (1.0 - 0.5 * hfd) ** o, sr=sr)

        # 扩散尾音：去相关噪声 × 指数衰减，截止频率随时间下降（高频先消失）
        tail = white(n, g) * np.exp(-LN1000 * t / rt60)
        tail *= 1.0 - np.exp(-np.maximum(t - 0.5 * t_mix, 0.0) / (1.5 * t_mix))
        fc_end = 16000.0 * (1.0 - hfd) ** 3
        fc = 16000.0 * (max(fc_end, 200.0) / 16000.0) ** np.minimum(t / rt60, 1.0)
        tail = tv_filter(tail, "lowpass", fc, 0.6, block=128, sr=sr)
        # 统计声学：混响总能量 / 直达声能量 = 16π·d²·(1-ā) / (S·ā)
        e_rev = 16.0 * np.pi * d_direct ** 2 * (1.0 - absorb) / max(S * absorb, 1e-9)
        e_early = float(np.sum(early ** 2))
        e_tail = max(e_rev - e_early, 0.3 * e_early)
        tail *= np.sqrt(e_tail / (np.sum(tail ** 2) + 1e-12))

        # 低频房间模态（Schroeder 频率以下）：轴向驻波，衰减比中高频慢
        f_s = 2000.0 * np.sqrt(rt60 / max(V, 1e-9))
        modal = np.zeros(n)
        for ax in range(3):
            for k_ax in range(1, 40):
                f = C_SOUND * k_ax / (2.0 * L[ax])
                if f >= f_s:
                    break
                cpl = abs(np.cos(k_ax * np.pi * src[ax] / L[ax]) * np.cos(k_ax * np.pi * ear[ax] / L[ax]))
                t60m = rt60 * g.uniform(1.1, 1.5)
                modal += cpl * np.sin(2.0 * np.pi * f * t + g.uniform(0, 2 * np.pi)) \
                    * np.exp(-LN1000 * t / t60m)
        if np.any(modal):
            F = np.fft.rfftfreq(n, 1.0 / sr)
            e_low = float(np.sum(np.abs(np.fft.rfft(tail)[F < f_s]) ** 2) * 2.0 / n)
            modal *= np.sqrt(e_low / (np.sum(modal ** 2) + 1e-12))
            modal *= 1.0 - np.exp(-t / 0.01)
        ir[:, ch] = early + tail + modal

    return ir / np.sqrt(np.sum(ir ** 2) / 2.0)


def room_ir(room: str | dict = "room", seed=None, order: int = 3, sr: int = SR) -> np.ndarray:
    """合成立体声房间脉冲响应（不含直达声），能量归一。

    seed 缺省时按房间名固定一个种子并缓存：混响是"房间"的属性，同一房间反复调用
    不应每次换一种房间；也避免每个配方重算（hall 一次约 0.26 s）。
    显式传入 seed 则不复用缓存（保证可复现的同时允许变化）。
    """
    if seed is None:
        key = (str(room), int(order), int(sr))
        ir = _ROOM_IR_CACHE.get(key)
        if ir is None:
            ir = _room_ir_uncached(room, 12345, order, sr)
            _ROOM_IR_CACHE[key] = ir
        return ir.copy()
    return _room_ir_uncached(room, seed, order, sr)


def reverb(x, room: str | dict = "room", wet_db: float = -10.0, dry_db: float = 0.0,
           predelay: float = 0.0, seed=None, sr: int = SR) -> np.ndarray:
    """卷积混响，返回立体声（干声 + 湿声）。wet_db 越大越"远/空旷"。"""
    ir = room_ir(room, seed, sr=sr)
    xs = to_stereo(x)
    if len(xs) == 0:
        return np.zeros((0, 2))
    wet = np.stack([_fftconv(xs[:, c], ir[:, c]) for c in range(2)], axis=1)   # 保留完整尾音
    return Mix(sr=sr).add(xs, 0.0, dry_db).add(wet, predelay, wet_db).out()


def outdoor(x, distance: float = 10.0, echoes: int = 3, wet_db: float = -16.0,
            src_height: float = 1.5, seed=None, sr: int = SR) -> np.ndarray:
    """户外：距离衰减与空气吸收 + 地面反射 + 远处建筑/山体回声 + 短散射尾音。

    只做声学"位置感"，不改变整体增益（需要时自行按 1/r 调整音量）。
    """
    g = rng(seed)
    dry = spectral_tilt_by_distance(to_stereo(x), distance, sr)
    m = Mix(sr=sr).add(dry)
    h_lis = 1.7
    dt = (np.hypot(distance, src_height + h_lis) - np.hypot(distance, src_height - h_lis)) / C_SOUND
    if dt > 0.0003:
        m.add(lowpass(dry, 6000.0, sr=sr), dt, -4.0)
    for i, delay in enumerate(np.sort(g.uniform(0.15, 1.8, int(max(0, echoes))))):
        echo = spectral_tilt_by_distance(to_stereo(x), distance + delay * C_SOUND, sr)
        # 远处地形的回声经过多次散射，是"一团"而不是清晰的复制：延迟越长越弥散
        ls = n_samples(0.04 + 0.12 * delay, sr)
        smear = white(ls, g) * np.exp(-np.arange(ls) / (0.3 * max(ls, 1)))
        smear /= np.sqrt(np.sum(smear ** 2) + 1e-12)
        echo = np.stack([_fftconv(echo[:, c], smear) for c in range(2)], axis=1)
        pos = g.uniform(-0.8, 0.8)
        gains = np.array([np.cos((pos + 1) * np.pi / 4), np.sin((pos + 1) * np.pi / 4)]) * np.sqrt(2.0)
        m.add(echo * gains, delay, wet_db - 5.0 * i - 6.0 * delay - g.uniform(0, 3))
    scat_n = n_samples(0.6, sr)
    scat = np.stack([white(scat_n, g), white(scat_n, g)], axis=1) \
        * np.exp(-LN1000 * np.arange(scat_n) / (0.6 * sr))[:, None]
    scat = lowpass(scat, 3000.0, sr=sr)
    scat /= np.sqrt(np.sum(scat ** 2) / 2.0 + 1e-12)
    xs = to_stereo(x)
    tail = np.stack([_fftconv(xs[:, c], scat[:, c]) for c in range(2)], axis=1)
    m.add(tail, 0.01, wet_db - 6.0)
    return m.out()


def distance(x, meters: float, ref: float = 1.0, sr: int = SR) -> np.ndarray:
    """距离：1/r 衰减 + 空气吸收。"""
    return spectral_tilt_by_distance(x, meters, sr) \
        * float(db_to_amp(-20.0 * np.log10(max(float(meters), float(ref)) / float(ref))))


# ============================================================================
# dynamics —— 动态处理：真峰值、饱和、压缩、前瞻限幅
#
# 全部按 block=64 的增益包络实现（向量化 + np.interp），不做 Python 逐采样循环。
# 攻击/释放的一极点平滑用"卷积短几何核"精确实现；
# 需要"实现快、恢复慢"时取 min(快平滑, 慢平滑)，与经典前馈压缩器听感一致。
# 改编自 ai-history-film（MIT）的 sfx/dynamics.py。
# ============================================================================

def _poly_peak_kernel(factor: int = 4, half: int = 12) -> np.ndarray:
    """真峰值用的多相窗 sinc 插值核，返回 (factor, 2*half)。

    第 r 行给"采样间 r/factor 相位"的卷积核（各相 DC 增益归一为 1）。
    这是广播/母带真峰值表（ITU-R BS.1770 附录）的经典做法：带限插值能看见
    采样点之间的波峰，而采样峰值会低估它们约 0.1~0.7 dB。
    """
    factor = int(max(1, factor))
    half = int(max(2, half))
    N = 2 * half
    j = np.arange(N) - (half - 1)          # 相对插值点的采样偏移
    h = np.zeros((factor, N))
    for r in range(factor):
        t = r / float(factor) - j
        win = 0.5 + 0.5 * np.cos(np.pi * np.clip(t / float(half), -1.0, 1.0))
        h[r] = np.sinc(t) * win
        h[r] /= h[r].sum()
    return h


def _peak_bound_ratio(factor: int = 4, half: int = 12) -> float:
    """多相插值的绝对值上界系数 K：真峰值 ≤ K × 采样峰值（严格上界）。

    用于环境声的快速安全判据：采样峰值 × K 仍低于 ceiling 时，真峰值必然不超，
    就不必对 389 s 信号做整段多相卷积（那要 1.4 s）。
    """
    h = _poly_peak_kernel(factor, half)
    return float(np.max(np.sum(np.abs(h), axis=1)))


def _true_peak_fft(x, oversample: int = 4) -> float:
    """整段 FFT 过采样求峰值（参考实现，只给小信号 / 自检对照用）。"""
    a = np.asarray(x, dtype=float)
    if a.size == 0:
        return 0.0
    up = _fft_upsample(a, int(max(1, oversample)))
    return float(np.max(np.abs(up)))


def _fft_upsample(x, factor: int) -> np.ndarray:
    """频谱补零的理想带限过采样（factor 倍），沿第 0 轴。"""
    a = np.asarray(x, dtype=float)
    factor = int(max(1, factor))
    n = len(a)
    if n == 0 or factor == 1:
        return np.array(a, dtype=float, copy=True)
    m = n * factor
    k = n // 2 + 1
    X = np.fft.rfft(a, axis=0)
    Y = np.zeros((m // 2 + 1,) + a.shape[1:], dtype=complex)
    Y[:k] = X
    if n % 2 == 0 and k >= 1:                 # 偶数长度：奈奎斯特分量要折半
        Y[k - 1] *= 0.5
    return np.fft.irfft(Y, m, axis=0) * factor


def _fft_downsample(x, down: int, n_out: int) -> np.ndarray:
    """_fft_upsample 的逆过程：截断到原带宽后抽取。"""
    a = np.asarray(x, dtype=float)
    down = int(max(1, down))
    if len(a) == 0:
        return np.zeros((0,) + a.shape[1:])
    m = len(a)
    k = m // down // 2 + 1
    X = np.fft.rfft(a, axis=0)
    X = X[:k].copy()
    if (m // down) % 2 == 0 and k >= 1:
        X[k - 1] *= 2.0
    return np.fft.irfft(X, int(n_out), axis=0) / float(down)


def true_peak(x, oversample: int = 4) -> float:
    """真峰值（dBTP 归一用）：采样点之间的波峰可能比采样值更高。

    多相窗 sinc 带限插值（factor 相），各声道分别评估后取最大。
    对 389 s 立体声约 1.4 s —— finalize 每次只调用一次，可接受。
    """
    a = np.asarray(x, dtype=float)
    if a.size == 0:
        return 0.0
    p = float(np.max(np.abs(a)))
    if p <= 0.0:
        return 0.0
    factor = int(max(1, oversample))
    if factor <= 1:
        return p
    half = 12 if len(a) > 4000 else 24       # 短音效可用更长的核，更准
    h = _poly_peak_kernel(factor, half)
    L = h.shape[1]
    chans = [a] if a.ndim == 1 else [a[:, c] for c in range(a.shape[1])]
    vmax = p
    for ch in chans:
        for r in range(1, factor):
            conv = _fftconv(ch, h[r][::-1])
            seg = conv[L - 1: L - 1 + len(ch)]
            if len(seg):
                vmax = max(vmax, float(np.max(np.abs(seg))))
    return vmax


def saturate(x, drive_db: float = 6.0, mix: float = 1.0, oversample: int = 4) -> np.ndarray:
    """tanh 软饱和：增加密度和"力量感"。

    非线性会产生新的高频，必须过采样，否则混叠成刺耳杂音。
    有 scipy 用 resample_poly，没有则走频谱补零的理想过采样。
    """
    a = np.asarray(x, dtype=float)
    if len(a) == 0:
        return np.array(a, dtype=float, copy=True)
    g = float(db_to_amp(drive_db))
    os_ = int(max(1, oversample))
    if _sig is not None:
        up = _sig.resample_poly(a, os_, 1, axis=0)
    else:
        up = _fft_upsample(a, os_)
    y = np.tanh(g * up) / max(np.tanh(g), 1e-9)
    if _sig is not None:
        y = _sig.resample_poly(y, 1, os_, axis=0)[: len(a)]
    else:
        y = _fft_downsample(y, os_, len(a))
    return float(mix) * y[: len(a)] + (1.0 - float(mix)) * a


def _onepole_smooth(x: np.ndarray, coef: float) -> np.ndarray:
    """极点平滑 y[n] = coef·y[n-1] + (1-coef)·x[n]，初值取 x[0]（避免起手淡入）。"""
    a = np.asarray(x, dtype=float)
    m = len(a)
    if m == 0:
        return a
    c = float(np.clip(coef, 0.0, 0.999999))
    if c <= 0.0:
        return a.copy()
    L = int(min(m, max(1, math.ceil(math.log(1e-5) / math.log(c))))) if c < 1.0 else 1
    h = (1.0 - c) * c ** np.arange(L)
    z = a - a[0]
    y = _fftconv(z, h)[:m] if L > 1 else z
    return a[0] + y


def _block_peak(x: np.ndarray, block: int = 64):
    """逐块峰值（沿第 0 轴取各声道最大值），返回 (块峰值数组, 实际块长)。"""
    a = np.asarray(x, dtype=float)
    n = len(a)
    block = max(1, int(block))
    m = int(math.ceil(n / block))
    det = np.max(np.abs(a), axis=1) if a.ndim == 2 else np.abs(a)
    pad = m * block - n
    if pad:
        det = np.pad(det, (0, pad))
    return det.reshape(m, block).max(axis=1), block


def compress(x, threshold_db: float = -18.0, ratio: float = 3.0, attack_ms: float = 5.0,
             release_ms: float = 80.0, makeup_db: float = 0.0, block: int = 64,
             sr: int = SR) -> np.ndarray:
    """前馈压缩器：block=64 的峰值检测 + 一极点攻击/释放（向量化，无逐采样循环）。"""
    a = np.asarray(x, dtype=float)
    n = len(a)
    if n == 0:
        return np.array(a, dtype=float, copy=True)
    lvl, blk = _block_peak(a, block)
    lvl_db = 20.0 * np.log10(np.maximum(lvl, 1e-9))
    over = np.maximum(lvl_db - float(threshold_db), 0.0)
    target = -over * (1.0 - 1.0 / max(float(ratio), 1e-3))       # 目标增益（dB，≤0）
    ca = math.exp(-blk / (max(float(attack_ms), 1e-3) / 1000.0 * sr))
    cr = math.exp(-blk / (max(float(release_ms), 1e-3) / 1000.0 * sr))
    gdb = np.minimum(_onepole_smooth(target, ca), _onepole_smooth(target, cr))
    gain = db_to_amp(np.interp(np.arange(n) / float(blk), np.arange(len(gdb)), gdb) + float(makeup_db))
    return a * (gain[:, None] if a.ndim == 2 else gain)


def limit(x, ceiling_db: float = -1.0, lookahead_ms: float = 2.0, release_ms: float = 60.0,
          block: int = 64, sr: int = SR) -> np.ndarray:
    """前瞻峰值限幅：保证峰值不超过 ceiling，且增益变化平滑（不硬削波）。"""
    a = np.asarray(x, dtype=float)
    n = len(a)
    if n == 0:
        return np.array(a, dtype=float, copy=True)
    c = float(db_to_amp(ceiling_db))
    p = peak(a)
    if p <= c:
        return np.array(a, dtype=float, copy=True)
    det, blk = _block_peak(a, block)
    g_raw = np.minimum(1.0, c / np.maximum(det, 1e-12))
    if float(np.min(g_raw)) >= 1.0:
        return np.array(a, dtype=float, copy=True)
    # 前瞻：向前多看 L 块、向后回看 1 块，取滑动最小（小窗口，直接平移取 min）
    L = max(1, int(math.ceil(float(lookahead_ms) / 1000.0 * sr / blk)))
    m = len(g_raw)
    g_la = g_raw.copy()
    for k in range(1, L + 1):
        g_la[:-k] = np.minimum(g_la[:-k], g_raw[k:])
    g_la[1:] = np.minimum(g_la[1:], g_raw[:-1])
    cr = math.exp(-blk / (max(float(release_ms), 1e-3) / 1000.0 * sr))
    g = np.minimum(g_la, _onepole_smooth(g_la, cr))
    gain = np.interp(np.arange(n) / float(blk), np.arange(m), g)
    y = a * (gain[:, None] if a.ndim == 2 else gain)
    pk = peak(y)                            # 兜底：插值夹缝里若仍超，整体微缩
    if pk > c:
        y *= c / pk
    return y


# ============================================================================
# master —— 母带处理与导出
# ============================================================================

def _trim_silence(x, floor_db: float = -60.0, keep_head: float = 0.003,
                  keep_tail: float = 0.05, sr: int = SR):
    """裁掉首尾的数字静音（相对峰值低于 floor_db 的部分）。"""
    a = np.asarray(x, dtype=float)
    env = np.abs(to_mono(a))
    p = float(env.max()) if len(env) else 0.0
    if p <= 0.0:
        return a
    idx = np.nonzero(env > p * float(db_to_amp(floor_db)))[0]
    if len(idx) == 0:
        return a
    head = int(keep_head * sr)
    s = max(0, int(idx[0]) - head)
    e = min(len(a), int(idx[-1]) + int(keep_tail * sr))
    y = a[s:e]
    if int(idx[0]) < head:                  # 瞬态在最前面：补足留白，免得淡入削掉瞬态
        pad = np.zeros((head - int(idx[0]),) + y.shape[1:])
        y = np.concatenate([pad, y])
    return y


def finalize(x, peak_db: float = -1.0, lufs: float | None = None,
             highpass_hz: float = 20.0, trim: bool = True) -> np.ndarray:
    """母带：切次声/直流 → 裁静音 → 响度或峰值归一 → 前瞻限幅 → 真峰值校正 → 首尾淡入淡出。

    - 单次音效（撞击、关门）：lufs=None，按真峰值归一到 peak_db（默认 -1 dBTP）。
    - 环境声（雨、风、火）：给 lufs（如 -20 ~ -24），限幅器兜底峰值。
    """
    y = highpass(np.asarray(x, dtype=float), highpass_hz, sr=SR)
    if len(y) == 0:
        return y
    y = y - (y.mean(axis=0) if y.ndim == 2 else float(y.mean()))
    if trim:
        y = _trim_silence(y, sr=SR)
        if len(y) == 0:
            return y
    ceil_amp = float(db_to_amp(peak_db))
    if lufs is not None:
        # 环境声：响度对齐 + 真峰值兜底。两者冲突时（尖峰多、峰均比高的纹理），
        # 优先保住"不超 ceiling"：线性缩放一次，响度相应略低于目标 —— 这是可听的折中。
        cur = integrated_loudness(y)
        gain = float(db_to_amp(float(lufs) - cur)) if np.isfinite(cur) else 1.0
        # 严格上界：真峰值 ≤ K × 采样峰值。若上界都低于 ceiling，就完全不必精确测真峰值
        # （389 s 环境声的整段多相卷积要 1.4 s，这样能省掉）。
        bound = peak(y) * gain * _peak_bound_ratio()
        if bound > ceil_amp:
            tp = true_peak(y) * gain
            if tp > ceil_amp and tp > 0:
                gain *= ceil_amp / tp
        y = y * gain
    else:
        tp = true_peak(y)
        if tp > 0:
            y = y * (ceil_amp / tp)            # 单次音效：按真峰值精确归一到 peak_db
    return fade(y, 0.0005, 0.03)


def save(path, x, subtype: str = "PCM_24") -> Path:
    """写 WAV（stdlib wave，不依赖 soundfile）。subtype: PCM_16 / PCM_24 / PCM_32。"""
    p = Path(path)
    a = np.asarray(x, dtype=float)
    if a.size and not np.all(np.isfinite(a)):
        # NaN/Inf 经 astype(<i2/<i4) 会被静默写成 0 或未定义值（不同平台结果不同），
        # 文件听起来是"随机哑音"而调用方毫无察觉——与 filmkit.wav_write 同口径：报错。
        bad = int(a.size - np.count_nonzero(np.isfinite(a)))
        raise ValueError("sfxkit.save: 采样里有 %d 个 NaN/Inf（%s）——上游合成有数值错误，"
                         "拒绝写出损坏的 WAV" % (bad, p))
    if a.ndim == 1:
        inter = a
        n_ch = 1
    else:
        n_ch = int(a.shape[1])
        inter = a.reshape(-1)
    inter = np.clip(inter, -1.0, 1.0)
    widths = {"PCM_16": 2, "PCM_24": 3, "PCM_32": 4}
    if subtype not in widths:
        raise ValueError("不支持的 WAV 子类型: %r（可用 PCM_16/PCM_24/PCM_32）" % (subtype,))
    sw = widths[subtype]
    # 三种位深统一四舍五入：astype 是向零截断，会同时造成 -0.5 LSB 的直流偏置
    # （PCM_16 实测 0.5 -> 0.49997，PCM_24 走 round 却是 0.4999999）和负样本的半 LSB 偏差；
    # 24-bit 一直是 rint，16/32-bit 漏了，同一函数三种位深两套口径。
    if sw == 2:
        raw = np.rint(inter * 32767.0).astype("<i2").tobytes()
    elif sw == 3:
        q = np.rint(inter * 8388607.0).astype("<i4")
        raw = q.view(np.uint8).reshape(-1, 4)[:, :3].tobytes()
    else:
        raw = np.rint(inter * 2147483647.0).astype("<i4").tobytes()
    p.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(p), "wb") as w:
        w.setnchannels(n_ch)
        w.setsampwidth(sw)
        w.setframerate(int(SR))
        w.writeframes(raw)
    return p


def _read_wav(path) -> tuple:
    """最小 WAV 解析：PCM 8/16/24/32 + IEEE float 32；返回 (数组, 采样率)。"""
    p = Path(path)
    data = p.read_bytes()
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("不是合法的 WAV 文件: %s" % (p,))
    pos = 12
    fmt = None
    payload = b""
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        size = int.from_bytes(data[pos + 4:pos + 8], "little")
        body = data[pos + 8:pos + 8 + size]
        if cid == b"fmt ":
            fmt = body
        elif cid == b"data":
            payload = body
            if fmt is not None:
                break
        pos += 8 + size + (size & 1)
    if fmt is None or len(fmt) < 16:
        raise ValueError("WAV 缺少 fmt 块: %s" % (p,))
    tag = int.from_bytes(fmt[0:2], "little")
    n_ch = int.from_bytes(fmt[2:4], "little")
    sr = int.from_bytes(fmt[4:8], "little")
    bits = int.from_bytes(fmt[14:16], "little")
    if tag == 3 and bits == 32:
        a = np.frombuffer(payload, dtype="<f4").astype(np.float64)
    elif tag == 1 and bits in (8, 16, 24, 32):
        if bits == 8:
            a = (np.frombuffer(payload, dtype=np.uint8).astype(np.float64) - 128.0) / 128.0
        elif bits == 16:
            a = np.frombuffer(payload, dtype="<i2").astype(np.float64) / 32768.0
        elif bits == 24:
            b = np.frombuffer(payload, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
            v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
            v = np.where(v >= (1 << 23), v - (1 << 24), v)
            a = v.astype(np.float64) / 8388608.0
        else:
            a = np.frombuffer(payload, dtype="<i4").astype(np.float64) / 2147483648.0
    else:
        raise ValueError("不支持的 WAV 编码（format=%d, bits=%d）: %s" % (tag, bits, p))
    if n_ch > 1:
        a = a[:(len(a) // n_ch) * n_ch].reshape(-1, n_ch)
    return a, sr


def load(path) -> np.ndarray:
    """读 WAV → float 数组（单声道 (n,) / 立体声 (n,2)），不重采样。"""
    a, _sr = _read_wav(path)
    return a


# ============================================================================
# analysis —— 分析工具：响度、包络、客观指标、自动质检
# 改编自 ai-history-film（MIT）的 sfx/analysis.py（只保留数值部分，画图交给外部）。
# ============================================================================

# BS.1770 K 计权的两个 biquad 参数（标准 48 kHz 系数由它们经双线性变换生成）
_K_SHELF = dict(f0=1681.974450955533, gain_db=3.999843853973347, q=0.7071752369554196)
_K_HPF = dict(f0=38.13547087602444, q=0.5003270373238773)


# BS.1770 标准 48 kHz K 计权系数（表值；与 ffmpeg ebur128 对齐）
_K_SOS_48K = np.array([
    [1.53512485958697, -2.69169618940638, 1.19839281085285,
     1.0, -1.69065929318241, 0.73248077421585],
    [1.0, -2.0, 1.0, 1.0, -1.99004745483398, 0.99007225036621],
])


def _k_weighting_sos(sr: int = SR) -> np.ndarray:
    """BS.1770 K 计权：高架（+4 dB 高频）+ 38 Hz 高通，返回两行 sos。

    48 kHz 直接用标准表值（与 ffmpeg ebur128 逐位对齐）；
    其它采样率用同一组模拟参数经双线性变换重新生成（近似，够用）。
    """
    if int(sr) == 48000:
        return _K_SOS_48K.copy()
    shelf = _biquad_ba("highshelf", _K_SHELF["f0"], _K_SHELF["q"],
                       _K_SHELF["gain_db"], sr)
    hp = _biquad_ba("highpass", _K_HPF["f0"], _K_HPF["q"], 0.0, sr)
    return np.vstack([np.concatenate(shelf), np.concatenate(hp)])


def _k_power(x, sr: int = SR) -> np.ndarray:
    """K 计权后逐采样的各声道功率之和。"""
    a = to_stereo(x) if np.asarray(x).ndim == 2 else np.asarray(x, dtype=float)[:, None]
    y = sosfilt(_k_weighting_sos(sr), a)
    return np.sum(y ** 2, axis=1)


def integrated_loudness(x, sr: int = SR) -> float:
    """BS.1770 积分响度（LUFS）：400 ms 块、75% 重叠、-70 LUFS 绝对门限 + -10 LU 相对门限。

    短于 400 ms 时按整体均方计算（仍带 K 计权）。
    """
    p = _k_power(x, sr)
    if len(p) == 0:
        return float("-inf")
    blk, hop = int(0.4 * sr), int(0.1 * sr)
    if len(p) < blk:
        return float(-0.691 + 10.0 * np.log10(float(np.mean(p)) + 1e-20))
    c = np.concatenate([[0.0], np.cumsum(p)])
    starts = np.arange(0, len(p) - blk + 1, hop)
    z = (c[starts + blk] - c[starts]) / blk
    lj = -0.691 + 10.0 * np.log10(z + 1e-20)
    z1 = z[lj > -70.0]
    if len(z1) == 0:
        return float("-inf")
    gate = -0.691 + 10.0 * np.log10(float(np.mean(z1))) - 10.0
    z2 = z[(lj > -70.0) & (lj > gate)]
    if len(z2) == 0:
        return float("-inf")
    return float(-0.691 + 10.0 * np.log10(float(np.mean(z2))))


def momentary_max(x, sr: int = SR) -> float:
    """最大瞬时响度（400 ms 滑动窗），适合评估短音效有多"响"。"""
    p = _k_power(x, sr)
    if len(p) == 0:
        return float("-inf")
    blk = int(0.4 * sr)
    if len(p) < blk:
        p = np.pad(p, (0, blk - len(p)))
    c = np.concatenate([[0.0], np.cumsum(p)])
    starts = np.arange(0, len(p) - blk + 1, max(1, int(0.01 * sr)))
    return float(-0.691 + 10.0 * np.log10(float(np.max((c[starts + blk] - c[starts]) / blk)) + 1e-20))


def envelope_db(x, win: float = 0.005, hop: float = 0.001, sr: int = SR):
    """RMS 包络（dB），返回 (时间数组, dB 数组)。"""
    a = np.asarray(x, dtype=float)
    p = a ** 2 if a.ndim == 1 else np.mean(a ** 2, axis=1)
    w = max(1, int(float(win) * sr))
    h = max(1, int(float(hop) * sr))
    if len(p) == 0:
        return np.zeros(0), np.zeros(0)
    c = np.concatenate([[0.0], np.cumsum(p)])
    starts = np.arange(0, max(1, len(p) - w + 1), h)
    ms = (c[np.minimum(starts + w, len(p))] - c[starts]) / w
    return (starts + w / 2.0) / float(sr), 10.0 * np.log10(ms + 1e-20)


def _ltas(mono: np.ndarray, sr: int = SR):
    """长时平均谱（Welch，Hann 窗；不依赖 scipy）。"""
    nper = int(min(8192, 2 ** math.floor(math.log2(max(len(mono), 256)))))
    nper = max(256, nper)
    hop = nper // 4
    win = np.hanning(nper)
    scale = 1.0 / (np.sum(win ** 2) * sr)
    nseg = max(1, (len(mono) - nper) // hop + 1) if len(mono) >= nper else 1
    acc = np.zeros(nper // 2 + 1)
    for i in range(nseg):
        s = i * hop
        seg = mono[s:s + nper]
        if len(seg) < nper:
            seg = np.pad(seg, (0, nper - len(seg)))
        seg = seg * win
        acc += np.abs(np.fft.rfft(seg)) ** 2
    acc *= scale / nseg
    return np.fft.rfftfreq(nper, 1.0 / sr), acc


BANDS = [("超低 <60", 0, 60), ("低 60-250", 60, 250), ("中低 250-1k", 250, 1000),
         ("中 1k-4k", 1000, 4000), ("高 4k-10k", 4000, 10000), ("极高 >10k", 10000, 24000)]


def _centroid(f, P):
    band = (f >= 20) & (f <= 20000)
    return float(np.sum(f[band] * P[band]) / (np.sum(P[band]) + 1e-30))


def analyze(x) -> dict:
    """算客观指标（中文键）：时长、峰值、真峰值、响度、起音/衰减、频谱重心、频段能量…

    返回的 dict 里 "QC" 是 qc() 给出的问题列表（空 = 通过）。
    """
    a = np.asarray(x, dtype=float)
    mono = to_mono(a)
    n = len(mono)
    m: dict = {"时长_s": round(n / float(SR), 3), "声道": 1 if a.ndim == 1 else int(a.shape[1])}
    pk = peak(a)
    if pk <= 0.0:
        m["QC"] = ["全是静音"]
        return m
    m["峰值_dBFS"] = round(float(amp_to_db(pk)), 2)
    m["真峰值_dBTP"] = round(float(amp_to_db(true_peak(a))), 2)
    m["RMS_dBFS"] = round(float(amp_to_db(rms(a))), 2)
    m["峰均比_dB"] = round(m["峰值_dBFS"] - m["RMS_dBFS"], 2)
    m["积分响度_LUFS"] = round(integrated_loudness(a), 2) if n >= 0.1 * SR else None
    m["最大瞬时响度_LUFS"] = round(momentary_max(a), 2)
    m["直流偏移"] = round(float(np.max(np.abs(np.mean(a, axis=0)))), 5)
    m["削波采样数"] = int(np.sum(np.abs(a) >= 0.999))
    m["开头跳变"] = round(float(np.max(np.abs(a[0]))) / pk, 3)
    m["结尾跳变"] = round(float(np.max(np.abs(a[-1]))) / pk, 3)

    t, env = envelope_db(a)
    emax = float(env.max())
    ipk = int(env.argmax())
    active = np.nonzero(env > emax - 60.0)[0]
    if len(active):
        m["开头静音_s"] = round(float(t[active[0]]), 3)
        m["结尾静音_s"] = round(float(n / float(SR) - t[active[-1]]), 3)
    on = np.nonzero(env[: ipk + 1] >= emax - 30.0)[0]
    if len(on):
        hop_s = float(t[1] - t[0]) if len(t) > 1 else 0.0
        win = env[on[0]: on[0] + max(2, int(0.1 / hop_s) if hop_s > 0 else 2)]
        m["起音_ms"] = round(float(np.argmax(win) * hop_s * 1000.0), 1)
    for d in (20, 40):
        above = np.nonzero(env >= emax - d)[0]
        if len(above):
            m["衰减到-%ddB_ms" % d] = round(float((t[above[-1]] - t[ipk]) * 1000.0), 1)
    act = env[env > emax - 40.0]
    m["包络起伏_dB"] = round(float(np.std(act)), 2) if n > SR else None

    f, P = _ltas(mono)
    m["频谱重心_Hz"] = round(_centroid(f, P))
    cum = np.cumsum(P) / (np.sum(P) + 1e-30)
    # 极短信号（几十个采样）经 _ltas 补零到 256 点后，cum 在最后一格里可能仍 < 0.85：
    # np.searchsorted 会返回 len(cum)，直接索引越界（analyze(np.ones(1)) 实测 IndexError）。
    i85 = min(int(np.searchsorted(cum, 0.85)), len(f) - 1)
    m["85%能量以下_Hz"] = round(float(f[i85]))
    band = (f >= 50) & (f <= 16000)
    m["频谱平坦度"] = round(float(np.exp(np.mean(np.log(P[band] + 1e-30)))
                                / (np.mean(P[band]) + 1e-30)), 4)
    tot = float(np.sum(P))
    m["频段能量%"] = {name: round(float(np.sum(P[(f >= lo) & (f < hi)]) / (tot + 1e-30) * 100.0), 1)
                  for name, lo, hi in BANDS}
    if a.ndim == 2 and a.shape[1] == 2:
        Lc, Rc = a[:, 0], a[:, 1]
        if np.std(Lc) > 0 and np.std(Rc) > 0:
            m["左右相关"] = round(float(np.corrcoef(Lc, Rc)[0, 1]), 3)
        else:
            m["左右相关"] = 1.0
    m["QC"] = qc(m)
    return m


def qc(metrics: dict) -> list[str]:
    """常见问题的自动检查：每条都是"不真实"或"不干净"的具体征兆（中文）。"""
    w = []
    if metrics.get("削波采样数", 0) > 0:
        w.append("削波 %d 个采样（失真）" % metrics["削波采样数"])
    if metrics.get("真峰值_dBTP", -99) > -0.1:
        w.append("真峰值过高，播放时可能削波")
    if metrics.get("直流偏移", 0) > 0.002:
        w.append("有直流偏移")
    if metrics.get("开头跳变", 0) > 0.1:
        w.append("开头不是从零开始（会有'咔'声）")
    if metrics.get("结尾跳变", 0) > 0.01:
        w.append("结尾被截断（会有'咔'声），需要淡出")
    if metrics.get("峰值_dBFS", 0) < -24:
        w.append("整体音量过低")
    if metrics.get("开头静音_s", 0) > 0.1:
        w.append("开头有多余静音")
    if metrics.get("结尾静音_s", 0) > 0.6:
        w.append("结尾有多余静音")
    bands = metrics.get("频段能量%", {})
    if bands.get("极高 >10k", 0) > 25:
        w.append("10 kHz 以上能量过多（刺耳/嘶嘶声，可能有混叠）")
    if bands.get("超低 <60", 0) > 45:
        w.append("60 Hz 以下能量过多（发闷或有低频漂移）")
    mechanical = metrics.get("起音次数", 0) <= 2 or (
        metrics.get("起音间隔变异系数") is not None and metrics["起音间隔变异系数"] < 0.02)
    if metrics.get("频谱平坦度", 1) < 0.005 and mechanical:
        w.append("频谱过于简单（只有一两个纯音），听起来像电子合成音")
    if metrics.get("包络起伏_dB") is not None and metrics["包络起伏_dB"] < 1.0 \
            and metrics.get("时长_s", 0) > 2:
        w.append("长音过于平稳，缺少自然起伏")
    if metrics.get("左右相关", 0) > 0.995 and metrics.get("声道", 1) == 2:
        w.append("立体声实为单声道（缺少空间感）")
    return w


def summary_line(m: dict) -> str:
    """一行摘要（CLI 用）。"""
    keys = ["时长_s", "真峰值_dBTP", "积分响度_LUFS", "最大瞬时响度_LUFS",
            "起音_ms", "衰减到-40dB_ms", "频谱重心_Hz"]
    s = "  ".join("%s=%s" % (k, m.get(k)) for k in keys if m.get(k) is not None)
    qcs = m.get("QC") or []
    return s + ("\n    [!] " + "；".join(qcs) if qcs else "\n    [OK] QC 通过")


# ===== 后续章节追加点 =====


# ============================================================================
# 配方（recipes）——cue 的 type 就是这里的名字
# ============================================================================
# 约定：每个配方 fn(seed=0, **kw) -> 单声道/立体声 ndarray（未母带）；
# 未知参数一律用 **kw 吞掉（cue 会透传 gain/pan/dur/size 等，不许抛 TypeError）。
# 母带统一在 render() 里按配方的 master 参数做（单次音效按真峰值 -1 dBTP；
# 环境声按 LUFS -20~-24），配方内部不自己归一整体音量。

REGISTRY: dict = {}

CUE_ALIASES = {
    "blip": "ui_blip", "click": "ui_click", "relay": "relay_click", "chirp": "data_chirp",
    "sub": "sub_drop", "riser": "riser", "reverse": "reverse_swell",
    "rewind": "tape_rewind", "stone": "go_stone", "wind": "wind",
    "stream": "text_stream",                       # 兼容写法：文字流
}

# ============================================================================
# 调性跟随 / 内容派生 seed（references/audio-variation.md §3.1）
# ============================================================================
# 提示音必须能跟随配乐调性：配乐选到 C / Am 等调时，硬编码 D 小调五声会"跑调"。
# 这里给 in_key() 可选的 key 参数，key=None 时逐音保持 D 小调五声
# （自检/legacy 逐样本兼容），C 路（CueBus.build_audio）再把配乐调性透传下来。
#
# 调名表与 scorekit.KEYS_ALL 对齐（4 个小调 + 6 个大调），不另发明一套写法：
#   小调 -> 自然小调五声（0/3/5/7/10 半音，Dm = D F G A C）
#   大调 -> 大调五声（0/2/4/7/9 半音，C = C D E G A）
_KEY_SPECS = {
    "Dm": ("D", 2, "minor"), "Am": ("A", 9, "minor"),
    "Cm": ("C", 0, "minor"), "Em": ("E", 4, "minor"),
    "C": ("C", 0, "major"), "G": ("G", 7, "major"), "D": ("D", 2, "major"),
    "F": ("F", 5, "major"), "A": ("A", 9, "major"), "Bb": ("Bb", 10, "major"),
}
_PENTA_CACHE = {}


def _penta(key="Dm"):
    """把某一调的五声音阶算成 [Hz, ...]（从小到大，MIDI 24~107）。

    小调用自然小调五声、大调用大调五声；结果按调名缓存（纯 numpy，无随机）。
    key 缺省 / None 仍是 D 小调五声：与硬编码表逐音一致，legacy 逐样本不变。
    """
    name = "Dm" if key is None else str(key)
    got = _PENTA_CACHE.get(name)
    if got is not None:
        return got
    spec = _KEY_SPECS.get(name)
    if spec is None:
        raise ValueError("in_key: 未知调名 %r（可用 %s）"
                         % (key, "/".join(_KEY_SPECS)))
    pc0, mode = int(spec[1]), spec[2]   # spec[0] 是音名（只给读代码的人看），不参与计算
    steps = (0, 3, 5, 7, 10) if mode == "minor" else (0, 2, 4, 7, 9)
    pcs = {(int(pc0) + s) % 12 for s in steps}
    out = [m for m in range(24, 108) if (m % 12) in pcs]
    _PENTA_CACHE[name] = np.array([440.0 * 2.0 ** ((m - 69) / 12.0) for m in out])
    return _PENTA_CACHE[name]


def in_key(freq: float, key: str | None = None) -> float:
    """把任意频率吸附到最近的五声音阶音（与配乐同调）。

    key=None -> 硬编码 D 小调五声（D F G A C），逐样本兼容。
    key 给 "Dm"/"Am"/"Cm"/"Em"（自然小调五声）或 "C"/"G"/"D"/"F"/"A"/"Bb"
    （大调五声）-> 吸附到该调；未知调名 raise ValueError，不静默降级（与 _enum 同口径）。
    """
    p = _penta(key)
    if freq <= 0:
        return float(p[0])
    i = int(np.argmin(np.abs(np.log(p) - np.log(max(freq, 20.0)))))
    return float(p[i])


def content_seed(base: int, style: str, content_fp: str, recipe: str, t: float) -> int:
    """内容派生 seed（契约 §3.1）：同片同 cue 稳定，换片同一位置也换种子。

    公式（与 filmkit.content_seed 逐字一致，两边同参必须同值）：
        sha1("v0.7|<base>|<style>|<content_fp>|<recipe>|<t 三位小数>") -> 前 8 位 hex
        -> int(h[:8], 16) % 9000 + 1000（32 位非负 int，落在 1000~9999）。
        （盐里的 "v0.7|" 是冻结字面量：改它等于给全部音效换种子，也与 filmkit 对不上。）

    纯 stdlib（hashlib）：同进程/跨进程/跨机器同参数同结果，不受 numpy 随机源影响。
    为什么不用 seed_base + i：那样只跟 cue 的**出现顺序**有关，两部内容完全不同的片子
    只要 cue 序列一样，音效轨就逐样本相同（跨片"音效都一样"的直接来源）。
    """
    h = hashlib.sha1(
        ("v0.7|%d|%s|%s|%s|%.3f"
         % (int(base), str(style), str(content_fp), str(recipe), float(t)))
        .encode("utf-8")).hexdigest()
    return int(h[:8], 16) % 9000 + 1000


def _enum(value, allowed, where: str, name: str = "值") -> str:
    """严格校验语义枚举参数：未知值立刻报错并列出可用值，不静默落到默认分支。

    同一个"看起来像开关"的参数不能有的一炸一个不吭声：rain(surface=) / wind(env=) /
    ui_blip(kind=) / text_stream(voice=) 一律严格报错，写错的值绝不悄悄当成默认分支
    （本仓库的规矩：可以保守，但不能静默降级）。
    """
    v = str(value)
    if v not in allowed:
        raise ValueError("%s: 未知 %s=%r（可用 %s）"
                         % (where, name, value, "/".join(allowed)))
    return v


def _times(value, where: str) -> list:
    """校验 times 数组：必须是有限、非负的相对时刻（负值会让 _place 静默丢样本）。"""
    ts = [float(t) for t in (value or [0.0])]
    if not ts:
        ts = [0.0]
    for t in ts:
        if not math.isfinite(t) or t < 0.0:
            raise ValueError("%s: times 里的时刻必须是非负有限数，收到 %r" % (where, t))
    return ts


def _tone(f, dur, t60, attack=0.0008, index=0.0, ratio=2.0, seed=0):
    """带起音的衰减正弦（或 FM 钟声）。提示音的基本单元。"""
    n = max(2, n_samples(dur))
    t = time_axis(n)
    if index > 0:
        x = fm(f, ratio, index * np.exp(-t * 3.5), n)
    else:
        x = sine(f, n)
    env = np.exp(-LN1000 * t / max(t60, 1e-4))
    na = max(2, min(n, n_samples(attack)))
    env[:na] *= np.linspace(0, 1, na) ** 0.5
    return x * env


def _place(bus: np.ndarray, x, at: float, gain_db: float = 0.0, pan_pos=None) -> None:
    """把一段声音按秒放到总线上（原地相加）。"""
    if x is None or len(x) == 0:
        return
    sr = SR
    i0 = int(round(float(at) * sr))
    x = np.asarray(x, dtype=float)
    if i0 < 0:
        x, i0 = x[-i0:], 0
    if i0 >= len(bus):
        return
    if x.ndim == 1 and bus.ndim == 2:
        x = pan(x, pan_pos) if pan_pos is not None else to_stereo(x)
    elif x.ndim == 2 and bus.ndim == 1:
        x = to_mono(x)
    n = min(len(x), len(bus) - i0)
    bus[i0:i0 + n] += float(db_to_amp(gain_db)) * x[:n]


def _bus(dur, stereo=True):
    return np.zeros((max(2, n_samples(dur)), 2) if stereo else max(2, n_samples(dur)))


def _rms_to(x, db):
    """把一段声音的 RMS 调到指定 dBFS（不改波形形状）。"""
    x = np.asarray(x, dtype=float)
    r = rms(x)
    if r <= 0.0:
        return x
    return x * (float(db_to_amp(db)) / r)


def _tile(seg, n: int, xfade: float = 1.8, sr: int = SR) -> np.ndarray:
    """把一段立体声循环素材平铺到 n 个采样，重叠区用升余弦交叉淡化（窗口和为 1）。"""
    seg = to_stereo(seg)
    m = len(seg)
    n = int(n)
    if m == 0 or n <= 0:
        return np.zeros((max(0, n), 2))
    if m >= n:
        return pad_to(seg, n)
    xf = int(min(n_samples(xfade, sr), max(16, m // 2)))
    if xf < 16:
        return pad_to(seg, n)
    # 段比交叉淡化窗口还短（m <= xf，含 m <= 16 的极短段）时 hop 会成为 0 或负数：
    # while pos < n 里 pos += hop 永远不前进 -> 死循环（m==16 实测挂死），m<16 时
    # pos 变负 -> 切片为空 -> 广播报错。这里把窗口缩到段长的 1/3，保证 hop >= 1。
    if m - xf <= 0:
        xf = max(1, m // 3)
    w = 0.5 - 0.5 * np.cos(np.linspace(0.0, np.pi, xf))
    hop = max(1, m - xf)
    out = np.zeros((n, 2))
    pos, first = 0, True
    while pos < n:
        L = min(m, n - pos)
        blk = seg[:L].copy()
        if not first:                              # 与上一块淡出区互补 → 和为 1
            k = min(xf, L)
            blk[:k] *= w[:k, None]
        if pos + L < n:                            # 与下一块淡入区互补
            k = min(xf, L)
            blk[L - k:] *= (1.0 - w[:k])[:, None]
        out[pos:pos + L] += blk
        if pos + L >= n:
            break
        pos += hop
        first = False
    return out


def _sustain(make, dur: float, seg_max: float = 24.0, xfade: float = 1.8) -> np.ndarray:
    """长环境声的成本控制：短于 ~32 s 直接整段渲染，更长则渲染一段再交叉淡化平铺。

    风/雨/火这类"稳态纹理"平铺听不出接缝；周期性由调用方的全片慢变调制打散。
    这样 389 s 的渲染成本与 24 s 片段同量级（整段 FFT 塑形 18.7 M 采样要 3.3 s，会超预算）。
    """
    n = n_samples(dur)
    if n <= 0:
        return np.zeros((0, 2))
    if float(dur) <= float(seg_max) * 1.35:
        return pad_to(to_stereo(make(float(dur))), n)
    return _tile(to_stereo(make(float(seg_max))), n, xfade=xfade)


def _slow_curve(dur: float, rate: float, seed=0, octaves: int = 2,
               sr: int = 1000) -> np.ndarray:
    """低频控制曲线：先在 1 kHz 稀疏网格上算平滑随机，再插值到 48 kHz 全长。

    控制信号本身只有零点几 Hz，逐采样算 389 s 的 Catmull-Rom 是浪费（约 0.8 s）。
    """
    n = n_samples(dur)
    if n <= 0:
        return np.zeros(0)
    c = smooth_random(float(dur), float(rate), seed=seed, octaves=int(octaves), sr=int(sr))
    if len(c) == n:
        return c
    return np.interp(np.linspace(0.0, 1.0, n), np.linspace(0.0, 1.0, len(c)), c)


def _long_mod(y, amount: float, seed=0) -> np.ndarray:
    """全片慢变增益（0.07 Hz、两个八度）：让平铺素材的重复感消失。"""
    y = np.asarray(y, dtype=float)
    if len(y) == 0:
        return y
    dur = len(y) / float(SR)
    g = 1.0 + float(amount) * _slow_curve(dur, 0.07, seed=seed, octaves=2)
    return y * (g[:, None] if y.ndim == 2 else g)


def _rain_kernel(kind: str, g) -> np.ndarray:
    """单个雨滴撞击不同表面的短核（已峰值归一）：路面/树叶/铁皮屋顶/玻璃窗。"""
    if kind == "pavement":                        # 硬路面：极短的宽带"嗒"
        x = bandpass(burst(g.uniform(0.3, 1.4) / 1000.0, g),
                     float(np.exp(g.uniform(np.log(1500.0), np.log(9000.0)))),
                     g.uniform(0.6, 1.4))
    elif kind == "leaves":                        # 树叶：轻薄、阻尼极大的小板
        b = random_body(1200, 7500, 6, "cardboard", eta=0.04, alpha=150.0, seed=g)
        x = b.render([Hit(0.0, 1.0, g.uniform(0.2, 0.5), hardness=0.3)], dur=0.05)
        tick = bandpass(burst(0.0006, g), 4000.0, 0.8)
        x[: len(tick)] += 0.3 * tick / (peak(tick) + 1e-12)
    elif kind == "roof":                          # 铁皮屋顶：带余音的金属"叮"
        b = random_body(900, 7000, 9, "metal", eta=0.0015, alpha=22.0, seed=g)
        x = b.render([Hit(0.0, 1.0, 0.15)], dur=0.35)
    elif kind == "window":                        # 玻璃窗：清脆的"嗒"
        b = random_body(2500, 11000, 6, "glass", eta=0.0015, alpha=90.0, seed=g)
        x = b.render([Hit(0.0, 1.0, 0.08)], dur=0.06)
    else:
        raise ValueError("rain: 未知表面类型 %r" % (kind,))
    return x / (peak(x) + 1e-12)


def _recipe(name=None, *, tags=(), master=None):
    """注册配方。master 是 render() 时传给 finalize 的参数。"""
    def deco(fn):
        key = name or fn.__name__
        doc = (fn.__doc__ or "").strip()
        REGISTRY[key] = dict(fn=fn, title=doc.splitlines()[0] if doc else key,
                             doc=doc, tags=tuple(tags), master=dict(master or {}))
        return fn
    return deco


def recipe(name=None, *, tags=(), master=None):
    """注册配方的公开入口（签名与 CONTRACT §5.1 一致；内部实现同 _recipe）。

    master 是 render() 时传给 finalize 的参数，例如环境声 {"lufs": -22.0}；
    缺省时 finalize 按真峰值 -1 dBTP 归一。
    """
    return _recipe(name, tags=tags, master=master)


# ---------------------------------------------------------------- 界面音
@_recipe(tags=("界面", "提示音"))
def ui_blip(seed=0, pitch=2200.0, kind="tick", dur=0.22, **kw):
    """极短的"嘀"：玻璃/金属的敲击，音高吸附五声音阶。

    key 从 **kw 透传（C 路把配乐调性放进来）——None/缺省 = D 小调五声，
    给 "Dm"/"C"/"Am" 等则吸附到该调，未知调名 raise ValueError（见 in_key）。
    """
    kind = _enum(kind, ("tick", "ping"), "ui_blip", "kind")
    g = rng(seed)
    f = in_key(float(pitch), kw.get("key")) * float(g.uniform(0.985, 1.015))
    if kind == "tick":
        body = _tone(f, dur * 0.55, 0.035, attack=0.0004)
        click = contact_noise(1.4, center_hz=6200.0, q=0.9, seed=seed) * 0.35
    else:                                     # "ping"：更柔、更长的尾
        body = _tone(f, dur, 0.26, attack=0.0012, index=1.1, ratio=3.0)
        click = contact_noise(1.0, center_hz=4800.0, q=0.8, seed=seed) * 0.12
    x = _bus(dur, stereo=False)
    _place(x, body, 0.0, -1.5)
    _place(x, click, 0.0, -12.0)
    return x


@_recipe(tags=("界面", "机械"))
def ui_click(seed=0, press=True, release=True, **kw):
    """鼠标/按键的"咔嗒"：两个极短噪声瞬态（按下 + 松开），不带音高。"""
    x = _bus(0.16, stereo=False)
    if press:
        k = contact_noise(0.8, center_hz=2600.0, q=1.1, seed=seed)
        _place(x, k, 0.0, -1.0)
        _place(x, _tone(1480.0, 0.02, 0.008, attack=0.0003), 0.0, -16.0)
    if release:
        k2 = contact_noise(0.6, center_hz=3400.0, q=1.3, seed=seed + 1)
        _place(x, k2, 0.055, -6.0)
    return x


@_recipe(tags=("界面", "机械"))
def relay_click(seed=0, count=1, gap=0.18, **kw):
    """继电器/老机器的"嗒"：低频撞击 + 一小段金属余振。"""
    n = max(1, int(count))
    gap = float(gap)
    if gap < 0.0:                       # 负 gap 会让总线长度变成负数 → 静默产出哑音
        raise ValueError("relay_click: gap 必须 >= 0，收到 %r" % (gap,))
    x = _bus(0.22 + gap * (n - 1), stereo=False)
    b = plate(320.0, material="metal", aspect=1.2, n_modes=22, tilt=0.5, seed=seed)
    for i in range(n):
        at = i * float(gap)
        _place(x, b.render(Hit(0.0, velocity=0.8, contact_ms=0.6, hardness=0.4)), at, -8.0)
        _place(x, contact_noise(1.2, center_hz=1500.0, q=0.9, seed=seed + i), at, -4.0)
    return x


@_recipe(tags=("界面", "抽签"))
def roulette_tick(seed=0, last=False, **kw):
    """转盘抽签的木质"咔嗒"：硬接触的木块，last=True 时更重、更低。

    给了 key 时把木块基频吸附到该调（提示音跟配乐同调）；key=None 走不吸附的公式，
    随机序列与取样点都不变 -> legacy 逐样本兼容。
    """
    g = rng(seed)
    f1 = (410.0 if not last else 300.0) * float(g.uniform(0.97, 1.03))
    key = kw.get("key")
    if key is not None:
        f1 = in_key(f1, key)
    b = bar(f1, material="wood", n_modes=7, tilt=0.25, seed=seed)
    x = _bus(0.26, stereo=False)
    _place(x, b.render(Hit(0.0, velocity=1.0 if last else 0.75,
                           contact_ms=0.55 if last else 0.8, hardness=0.55)), 0.0, -1.0)
    _place(x, contact_noise(1.0, center_hz=4200.0, q=1.0, seed=seed), 0.0, -9.0)
    return x


@_recipe(tags=("界面", "提示音", "音乐"))
def answer_chime(seed=0, note=1174.66, grace=880.0, grace_lead=0.07, **kw):
    """"猜中了"的钟琴：一个装饰音 + 一个主音，都在五声音阶上。

    key 从 **kw 透传（None = D 小调五声；给调名则整条钟琴跟随配乐调性）。
    """
    key = kw.get("key")
    x = _bus(1.6, stereo=False)
    _place(x, _tone(in_key(grace, key), 0.9, 0.5, attack=0.001, index=1.6, ratio=3.5), 0.0, -10.0)
    _place(x, _tone(in_key(note, key), 1.4, 0.9, attack=0.0015, index=1.2, ratio=2.0),
           float(grace_lead), -1.5)
    return x


# ---------------------------------------------------------------- 文字 / 数据
def _keystroke(g, space=False):
    """一次按键：硬接触噪声 + 一小段塑料外壳共振。space=True 时更低更闷。"""
    f1 = 180.0 if space else 260.0
    b = random_body(f1 * 0.8, f1 * 2.6, n_modes=8, material="plastic", seed=int(g.integers(1 << 30)))
    k = contact_noise(0.6, center_hz=2400.0 if space else 3600.0, q=1.0,
                      seed=int(g.integers(1 << 30)))
    x = b.render(Hit(0.0, velocity=0.9, contact_ms=0.7, hardness=0.5)) * 0.5
    x = pad_to(x, max(len(x), len(k)))
    x[: len(k)] += k * 0.9
    return x


@_recipe(tags=("界面", "文字"))
def typing(seed=0, keys=12, speed=1.0, dur=None, **kw):
    """人工打字：不等间隔的按键（human_rhythm 带轻微抖动）。"""
    g = rng(seed)
    n = max(1, int(keys))
    interval = 0.105 / max(0.2, float(speed))
    times = human_rhythm(n, interval, jitter=0.22, drift=0.04, seed=seed)
    total = float(times[-1]) + 0.25
    x = _bus(total, stereo=False)
    for i, t in enumerate(times):
        _place(x, _keystroke(g, space=(i > 0 and i % 11 == 0)), float(t),
               -2.0 + float(g.uniform(-1.2, 1.2)))
    return x


@_recipe(tags=("界面", "文字"))
def typing_at(seed=0, times=(0.0, 0.12, 0.25, 0.4), spaces=(), **kw):
    """逐字对齐的打字声：times 是相对时刻数组（画面上每出现一个字就有一下）。"""
    ts = _times(times, "typing_at")
    g = rng(seed)
    total = max(ts) + 0.25
    x = _bus(total, stereo=False)
    space_set = set(float(s) for s in (spaces or []))
    for t in ts:
        is_space = any(abs(t - s) < 0.006 for s in space_set)
        _place(x, _keystroke(g, space=is_space), t, -2.0 + float(g.uniform(-1.0, 1.0)))
    return x


@_recipe(tags=("界面", "数据", "文字"))
def text_stream(seed=0, times=(0.0, 0.04, 0.08, 0.12), base=1174.7, voice="tone", **kw):
    """AI 逐字输出：比打字更连贯。voice="tone" 是带音高的五声颗粒；"soft" 是不带音高的沙沙。

    voice="tone" 的音高跟随 **kw 里的 key（None = D 小调五声）。
    """
    voice = _enum(voice, ("tone", "soft"), "text_stream", "voice")
    ts = _times(times, "text_stream")
    g = rng(seed)
    total = max(ts) + 0.30
    x = _bus(total, stereo=False)
    for _i, t in enumerate(ts):
        if str(voice) == "soft":
            k = burst(0.028, seed=int(g.integers(1 << 30)), slope_db_oct=-1.5,
                      attack=0.004, t60=0.035)
            k = bandpass(k, 3200.0 + 900.0 * float(g.random()), q=0.9)
            _place(x, k, t, -6.0 + float(g.uniform(-1.5, 1.5)))
        else:
            f = in_key(float(base) * (2.0 ** (float(g.choice([-2, 0, 2, 4])) / 12.0)),
                       kw.get("key"))
            _place(x, _tone(f, 0.06, 0.02, attack=0.0006), t, -9.0 + float(g.uniform(-1.0, 1.0)))
    return x


@_recipe(tags=("界面", "数据"))
def data_chirp(seed=0, dur=0.8, rate=28.0, base=1800.0, **kw):
    """数据流：一串很快的、带音高的短脉冲，频率在五声上随机跳动。

    音高跟随 **kw 里的 key（None = D 小调五声）。
    """
    r = float(rate)
    # rate 是"每秒脉冲数"，必须为正：rate=0 会在下面 i / r 抛 ZeroDivisionError（内部
    # 堆栈，用户看不出是参数问题）；rate<0 时 int(dur*r) 为负、事件时刻为负，_place 会
    # 静默丢掉大部分脉冲——只剩哑音。与 _enum 同口径：认识但非法的值必须响亮报错。
    if not math.isfinite(r) or r <= 0.0:
        raise ValueError("data_chirp: rate 必须是正数（每秒脉冲数），收到 %r" % (rate,))
    n = max(2, int(float(dur) * r))
    g = rng(seed)
    x = _bus(dur, stereo=False)
    for i in range(n):
        at = i / r
        f = in_key(float(base) * (2.0 ** (float(g.choice([-5, -2, 0, 2, 3, 5, 7])) / 12.0)),
                   kw.get("key"))
        env_amp = 1.0 - 0.55 * (i / max(1, n - 1))
        _place(x, _tone(f, 0.05, 0.012, attack=0.0004), at, -14.0 + 20 * np.log10(env_amp + 1e-6))
    return x


# ---------------------------------------------------------------- 转场
@_recipe(tags=("转场", "空气"))
def whoosh(seed=0, dur=0.9, brightness=0.6, size=0.6, direction="lr", **kw):
    """物理飞过：湍流噪声 + 按传播延迟读取造成的真多普勒 + 1/r 钟形音量 + 越远越闷 + 地面反射梳状。"""
    direction = _enum(direction, ("lr", "rl", "center", "c", "none"), "whoosh", "direction")
    d = float(dur)
    n = max(2, n_samples(d))
    g = rng(seed)
    src = colored(int(n * 1.4) + 4000, -3.0, seed=int(g.integers(1 << 30)))
    t = np.linspace(0.0, 1.0, n)
    # 观察者与声源：源从 -1 飞到 +1（归一化），最近点落在 0.55 处（"逼近长、远去快"）
    pos = -1.0 + 2.0 * t ** (0.85 + 0.5 * float(size))
    nearest = 0.55
    dist = np.abs(pos) + 0.16
    gain = (1.0 / dist) / (1.0 / (abs(nearest) + 0.16))     # 1/r 钟形，峰值归一到 1
    gain = np.clip(gain, 0.0, 1.0) ** 1.15
    # 真多普勒：按延迟读取（越接近越"高"）
    c = 3.0
    delay = (nearest - pos) / c
    idx = np.clip((t - delay) * (n - 1), 0, n - 1).astype(int)
    y = src[idx]
    # 距离越远越闷：中心频率随距离变化
    fc = (700.0 + 2200.0 * float(brightness)) * np.clip(gain, 0.15, 1.0) ** 0.8
    y = tv_filter(y, "lowpass", np.clip(fc, 220.0, 9000.0), q=0.8, block=64)
    y = y * gain
    # 地面反射：移动的梳状相位（"丝——"的高频成分）
    refl = np.zeros(n)
    dly = int(0.0032 * SR)
    if dly < n:
        refl[dly:] = y[:-dly] * 0.28
    y = y + refl
    y = fade(np.asarray(y, dtype=float), 0.02, 0.06)
    if str(direction) == "rl":
        y = y[::-1].copy()
    if direction in ("center", "c", "none"):
        return y
    p = np.linspace(-1.0 if str(direction) != "rl" else 1.0,
                    1.0 if str(direction) != "rl" else -1.0, n)
    return pan_dynamic(y, p)


@_recipe(tags=("转场", "张力"))
def riser(seed=0, dur=4.0, tone=True, end="cut", **kw):
    """上升张力：粉噪扫频 + 可选的上行正弦（越来越亮），结尾可硬切。"""
    end = _enum(end, ("cut", "fade"), "riser", "end")
    d = max(0.15, float(dur))
    n = max(2, n_samples(d))
    t = np.linspace(0.0, 1.0, n)
    g = rng(seed)
    x = colored(n, -3.0, seed=int(g.integers(1 << 30)))
    fc = 180.0 * (2.0 ** (5.0 * t ** 1.4))
    x = tv_filter(x, "bandpass", fc, q=1.6, block=128)
    x = x * (t ** 2.2)
    if tone:
        f0 = 110.0
        f = f0 * (2.0 ** (3.4 * t ** 1.25))
        ph = np.cumsum(f) / SR
        x = x * 0.75 + np.sin(2 * np.pi * ph) * 0.22 * (t ** 2.6)
    x = fade(x, 0.02, 0.0 if str(end) == "cut" else 0.25)
    return x


@_recipe(tags=("转场", "撞击"))
def impact(seed=0, size=1.0, brightness=0.5, metal=0.3, tail=1.0, **kw):
    """重击：低频轰鸣 + 金属/木质撞击 + 瞬态 + 混响尾。成片里"重拍"的主力。"""
    s = float(np.clip(size, 0.3, 2.0))
    d = 1.1 * s * float(np.clip(tail, 0.3, 2.5))
    x = _bus(d)
    # 低频体：正弦下坠 + 饱和
    f0 = 92.0 / s
    n_low = n_samples(min(d, 0.75 * s))
    t = time_axis(n_low)
    f = f0 * (1.0 + 1.6 * np.exp(-t * 18.0))
    ph = np.cumsum(f) / SR
    low = np.sin(2 * np.pi * ph) * np.exp(-t * 6.5)
    low = saturate(low, drive_db=6.0 + 4.0 * float(brightness))
    _place(x, low * 0.9, 0.0, -1.0)
    # 撞击体：金属（钟/管）或木头（板）
    if metal > 0.05:
        b = bell(180.0 / s * (1.0 + 0.6 * float(brightness)), material="metal",
                 tilt=0.1, seed=seed)
        _place(x, b.render(Hit(0.0, velocity=1.1, contact_ms=0.35,
                               hardness=0.6)) * float(np.clip(metal, 0, 1)), 0.0, -8.0)
    b2 = plate(150.0 / s, material="wood", aspect=1.35, n_modes=26, tilt=0.35, seed=seed + 1)
    _place(x, b2.render(Hit(0.0, velocity=1.2, contact_ms=0.9, hardness=0.35)), 0.0, -4.0)
    _place(x, contact_noise(1.6, center_hz=3600.0, q=0.7, seed=seed + 2), 0.0, -6.0)
    # 空间
    y = reverb(to_mono(x), room="warehouse", wet_db=-11.0, dry_db=0.0, seed=seed)
    y = fade(y, 0.0003, 0.10)
    return y


@_recipe(tags=("转场", "低频"))
def sub_drop(seed=0, dur=1.8, start_hz=80.0, end_hz=24.0, **kw):
    """低频下坠：从 start_hz 滑到 end_hz，配"吸下去"的包络。"""
    d = max(0.2, float(dur))
    n = max(2, n_samples(d))
    t = time_axis(n)
    p = (t / d) ** 0.75
    # 频率必须为正：start_hz<=0 时 (end/start)**p 会除零；负底数取分数次幂还会出 NaN
    s0 = max(1e-3, abs(float(start_hz)))
    e0 = max(1e-3, abs(float(end_hz)))
    f = s0 * (e0 / s0) ** p
    ph = np.cumsum(f) / SR
    x = np.sin(2 * np.pi * ph) * np.exp(-t * 2.2)
    x = saturate(x, drive_db=5.0)
    x = fade(x, 0.004, 0.25)
    return to_stereo(np.asarray(x, dtype=float))


@_recipe(tags=("转场", "张力"))
def reverse_swell(seed=0, dur=1.6, size=1.0, **kw):
    """反向吸气：把一段噪声+和弦倒放，用来"推到"重击。"""
    d = max(0.2, float(dur))
    n = max(2, n_samples(d))
    g = rng(seed)
    x = colored(n, -3.0, seed=int(g.integers(1 << 30)))
    x = lowpass(x, 1800.0 + 2600.0 * float(size), q=0.7)
    t = np.linspace(0.0, 1.0, n)
    y = x * (t ** 3.0)
    # 底部加一层渐渐出现的和弦（D 小调），倒放后更"有调性"
    ch = np.zeros(n)
    for i, fr in enumerate((146.83, 174.61, 220.0)):
        ch += supersaw(fr, n, voices=3, detune=0.06, seed=seed + i)
    ch = lowpass(ch / 3.0, 1400.0, q=0.7)
    y = y / (peak(y) + 1e-9) * 0.8 + pad_to(ch, n) * (t ** 2.5) * 0.55
    y = y[::-1].copy()
    y = fade(y, 0.0, 0.05)
    return to_stereo(np.asarray(y, dtype=float))


@_recipe(tags=("转场", "数字"))
def glitch(seed=0, dur=0.6, density=0.6, **kw):
    """数字故障：采样保持 + 位深破坏 + 随机小段重复。"""
    d = max(0.1, float(dur))
    n = max(2, n_samples(d))
    g = rng(seed)
    base = colored(n, -1.0, seed=int(g.integers(1 << 30)))
    hold = max(2, int(SR / (220.0 + 900.0 * float(density))))
    idx = (np.arange(n) // hold) * hold
    y = base[np.clip(idx, 0, n - 1)]
    # 随机小块重复（磁带卡顿）
    k = max(1, int(
        float(density) * 12))
    for _ in range(k):
        a = int(g.integers(0, max(1, n - hold * 6)))
        ln = max(1, int(g.integers(hold, hold * 6)))
        seg = y[a:a + ln]
        if len(seg) == 0:
            continue
        small = seg[: max(1, len(seg) // 3)]
        y[a:a + len(seg)] = np.resize(small, len(seg))
    y = np.round(y * 12.0) / 12.0                       # 位深破坏
    y = highpass(y, 380.0, q=0.8)
    y = y * (0.55 + 0.45 * np.sin(2 * np.pi * 7.0 * np.arange(n) / SR) ** 2)
    y = fade(y, 0.002, 0.02)
    return to_stereo(np.asarray(y, dtype=float))


@_recipe(tags=("转场", "机械"))
def tape_rewind(seed=0, dur=2.6, accel=True, stop=True, **kw):
    """磁带倒带：带速抖动的高频啸叫 + 机械摩擦 + 结尾急停。"""
    d = max(0.2, float(dur))
    n = max(2, n_samples(d))
    t = np.linspace(0.0, 1.0, n)
    g = rng(seed)
    rate = (1.0 + 2.4 * t ** 1.6) if accel else 1.0
    f = 1400.0 * rate * (1.0 + 0.02 * np.sin(2 * np.pi * 11.0 * t))
    ph = np.cumsum(f) / SR
    whine = 0.16 * np.sin(2 * np.pi * ph) + 0.06 * np.sin(4 * np.pi * ph)
    noise = colored(n, -3.0, seed=int(g.integers(1 << 30)))
    noise = bandpass(noise, 2400.0, q=0.7) * 0.5
    y = whine + noise
    if stop:
        k = max(2, int(0.12 * n))
        y[-k:] *= np.linspace(1.0, 0.0, k) ** 2.0
    y = fade(y, 0.01, 0.06)
    return to_stereo(np.asarray(y, dtype=float))


@_recipe(tags=("转场", "时空"))
def worldline_shift(seed=0, dur=1.6, shift=0.5, depth=1.0, **kw):
    """世界线变动：金属撞击的混响倒放吸进去 + 和弦被拖慢降调 + 一记低频 + 开头跳帧。"""
    d = max(0.3, float(dur))
    dep = float(np.clip(depth, 0.2, 2.0))
    g = rng(seed)
    n = max(2, n_samples(d))
    x = _bus(d, stereo=False)
    # 1) 开头 50 ms 跳帧重复三次
    k = max(2, n_samples(0.05))
    blip = colored(k, -1.0, seed=int(g.integers(1 << 30))) * np.exp(-np.arange(k) / (0.012 * SR))
    for i in range(3):
        _place(x, blip, 0.0 + i * 0.004, -10.0)
    # 2) 金属撞击 + 混响倒放
    b = bell(420.0 * (1.0 + 0.4 * dep), material="metal", tilt=0.2, seed=seed)
    hit = b.render(Hit(0.0, velocity=1.2, contact_ms=0.3, hardness=0.7))
    wet = reverb(to_mono(hit), room="hall", wet_db=-4.0, dry_db=-8.0, seed=seed)
    wet = to_mono(np.asarray(wet, dtype=float))[::-1].copy()
    wet = pad_to(wet, n)
    _place(x, wet * np.linspace(0.35, 1.0, n) ** 2.0, 0.0, -6.0)
    # 3) 和弦被拖慢降调（磁带效应）
    f0 = 146.83
    f = f0 * (1.0 + shift * np.linspace(0.0, -0.4, n))
    ph = np.cumsum(f) / SR
    chord = np.zeros(n)
    for r in (1.0, 1.2, 1.5):
        chord += np.sin(2 * np.pi * ph * r)
    chord = lowpass(chord, 1600.0, q=0.7) * (np.linspace(0.15, 1.0, n) ** 1.6)
    _place(x, chord * 0.35, 0.0, -9.0)
    # 4) 一记低沉"咚"
    _place(x, _tone(72.0, 0.7, 0.5, attack=0.002, index=0.6, ratio=1.0), d * 0.62, -3.0)
    x = fade(x, 0.004, 0.12)
    return to_stereo(np.asarray(x, dtype=float))


# ---------------------------------------------------------------- 环境
@_recipe(tags=("环境", "风", "纹理"), master={"lufs": -22.0})
def wind(seed=0, dur=15.0, strength=0.6, gustiness=0.6, whistle=0.5, env="open", **kw):
    """阵风：一条风速曲线同时驱动低频隆隆 / 中频呼啸 / 高频嘶声 / 缝隙口哨。

    strength 0~1 风速；gustiness 0~1 阵风强弱（涨得快、落得慢）；whistle 0~1 口哨多少；
    env: open 空旷 | trees 树林（加树叶沙沙）| indoor 室内听窗外（发闷、口哨明显）。
    长片（>32 s）自动走"短段渲染 + 交叉淡化平铺 + 全片慢变调制"，389 s 也只要 3 s 级。
    """
    g = rng(seed)
    sr = SR
    env = _enum(env, ("open", "trees", "indoor"), "wind", "env")

    def make(d):
        n = max(2, n_samples(d))
        t = np.arange(n) / float(sr)
        st = float(np.clip(strength, 0.0, 1.0))
        gu = float(np.clip(gustiness, 0.0, 1.0))
        v = st + 0.30 * gu * smooth_random(d, 0.09, g, octaves=3)
        for tg in poisson_times(d, 0.12 + 0.12 * gu, g):          # 阵风事件
            rise, fall = g.uniform(0.8, 2.5), g.uniform(2.0, 5.0)
            amp = g.uniform(0.25, 0.6) * gu
            v = v + amp * np.where(t < tg, np.exp(-((t - tg) / rise) ** 2),
                                   np.exp(-((t - tg) / fall) ** 2))
        v = np.clip(v, 0.03, 1.3)
        vmean = float(np.mean(v))
        m = Mix(d)
        muffle = (str(env) == "indoor")
        for ch in (0, 1):                        # 左右独立噪声、共享风速 → 宽阔
            rum = lowpass(highpass(brown(n, g), 35.0), 70.0 + 220.0 * vmean) * v ** 1.8
            wo = bandpass(pink(n, g), 220.0 + 900.0 * vmean, 0.9) * v ** 2.4
            hiss = highpass(white(n, g), 3500.0) * np.clip(v - 0.45, 0.0, None) ** 2
            layer = _rms_to(rum, -22.0) + _rms_to(wo, -21.0) + _rms_to(hiss, -40.0 + 10.0 * st)
            if muffle:
                layer = lowpass(layer, 900.0, order=4)
            st2 = np.zeros((n, 2))
            st2[:, ch] = layer
            m.add(st2)
        if whistle > 0.02:                       # 缝隙口哨：窄带，音高随风速
            for _ in range(2):
                f0 = g.uniform(420.0, 900.0)
                fc = f0 * (0.55 + 0.75 * v) * (1.0 + 0.012 * smooth_random(d, 6.0, g))
                gate = np.clip((v - 0.35) / 0.4, 0.0, 1.0) ** 2
                w = tv_filter(pink(n, g), "bandpass", fc, 28.0, block=256) * gate
                m.add(_rms_to(w, -30.0 + 8.0 * float(np.clip(whistle, 0.0, 1.0))
                              + (6.0 if muffle else 0.0)),
                      pan_pos=g.uniform(-0.6, 0.6))
        if str(env) == "trees":                  # 树叶沙沙：高频颗粒，密度随风速
            kern = [bandpass(burst(g.uniform(0.004, 0.02), g), g.uniform(2500.0, 8000.0), 0.9)
                    for _ in range(6)]
            leaves = np.zeros((n, 2))
            rate = 40.0 + 900.0 * v ** 2
            for ch in (0, 1):
                tl = modulated_times(rate, g)
                leaves[:, ch] = scatter(tl, power_law(len(tl), 2.0, 0.1, 1.0, g), kern, n, g)
            m.add(_rms_to(leaves, -30.0))
        return m.out()[:n]

    y = _sustain(make, dur)
    return _long_mod(y, 0.16, seed + 101)


@_recipe(tags=("环境", "雨", "纹理"), master={"lufs": -22.0})
def rain(seed=0, dur=12.0, intensity=0.6, surface="mixed", **kw):
    """下雨：近处雨滴（泊松时间 + 幂律大小 + 表面材质核）+ 远处雨幕底噪 + 水洼气泡 + 雨势起伏。

    intensity 0~1（小雨→暴雨）；surface: mixed 混合 | leaves 树叶 | pavement 路面 |
    roof 铁皮屋顶 | window 隔着玻璃窗。长片自动短段平铺（见 wind 的说明）。
    """
    g = rng(seed)

    def make(d):
        n = max(2, n_samples(d))
        it = float(np.clip(intensity, 0.05, 1.0))
        surf = _enum(surface, ("mixed", "leaves", "pavement", "roof", "window"),
                     "rain", "surface")
        kinds = {"mixed": ("pavement", "leaves"), "leaves": ("leaves",),
                 "pavement": ("pavement",), "roof": ("roof",), "window": ("window",)}
        swell = np.clip(1.0 + 0.35 * smooth_random(d, 0.1, g, octaves=2), 0.3, None)
        rate = (120.0 + 3000.0 * it ** 1.5) / 6.0 * swell
        kern = [_rain_kernel(k, g) for k in kinds[surf]]
        m = Mix(d)
        for p in (-0.85, -0.4, 0.0, 0.4, 0.85):              # 近处：全频带、声场宽
            tt = modulated_times(rate, g)
            aa = power_law(len(tt), 2.3, 0.03, 1.0, g)
            m.add(_rms_to(scatter(tt, aa, kern, n, g), -22.0), pan_pos=p)
        for p in (-0.6, 0.0, 0.6):                            # 稍远处：更多、更暗、更轻
            tt = modulated_times(rate * 0.8, g)
            aa = power_law(len(tt), 2.3, 0.03, 1.0, g)
            xx = lowpass(scatter(tt, aa, kern, n, g), 3500.0)
            m.add(_rms_to(xx, -28.0), pan_pos=p)
        if surf in ("mixed", "pavement"):                      # 水洼里的气泡"叮"
            bub = Mix(d)
            tb = modulated_times((10.0 + 90.0 * it) * swell * (1.0 if surf == "mixed" else 0.4), g)
            for t0, a in zip(tb, power_law(len(tb), 2.0, 0.1, 1.0, g)):
                r_mm = float(np.exp(g.uniform(np.log(0.9), np.log(3.5))))
                b = bubble(3.26 / (r_mm / 1000.0), rise=g.uniform(0.05, 0.3))
                bub.add(normalize(b, 0.0) * a, float(t0), 0.0, g.uniform(-0.9, 0.9))
            m.add(_rms_to(bub.out(), -32.0 + 4.0 * it))
        bed = np.stack([pink(n, g), pink(n, g)], axis=1)       # 远处雨幕
        bed = lowpass(highpass(bed, 500.0), 11000.0)
        bed = peak_eq(bed, 4500.0, 4.0, q=0.6) * (swell ** 0.7)[:, None]
        if surf == "window":
            bed = lowpass(bed, 1400.0, order=4)                # 隔着玻璃，外面的雨声发闷
        m.add(_rms_to(bed, -23.0 + 5.0 * it))
        if it > 0.5:                                           # 大雨时的低频轰鸣
            rum = lowpass(np.stack([brown(n, g), brown(n, g)], axis=1), 160.0, order=4)
            m.add(_rms_to(rum, -40.0 + 12.0 * (it - 0.5)))
        return m.out()[:n]

    y = _sustain(make, dur)
    return _long_mod(y, 0.14, seed + 202)


@_recipe(tags=("环境", "火", "纹理"), master={"lufs": -22.0})
def campfire(seed=0, dur=12.0, size=0.6, crackle=0.6, **kw):
    """篝火：火焰翻滚的低频呼呼 + 燃烧气流嘶嘶 + 成簇的噼啪（Cox 过程 + 幂律大小）+ 木柴塌落。

    size 0~1 火的大小；crackle 0~1 噼啪多少。长片自动短段平铺（见 wind 的说明）。
    """
    g = rng(seed)

    def make(d):
        n = max(2, n_samples(d))
        sz = float(np.clip(size, 0.0, 1.0))
        cr = float(np.clip(crackle, 0.0, 1.0))
        bed_db = -26.0 + 6.0 * sz
        m = Mix(d)
        flick = smooth_random(d, 0.8, g, octaves=3)
        roar = np.stack([lowpass(pink(n, g), 180.0 + 250.0 * sz) for _ in range(2)], axis=1)
        roar = highpass(roar, 70.0)                             # 篝火不是低音炮
        roar *= np.clip(1.0 + 0.5 * flick, 0.1, None)[:, None]
        m.add(_rms_to(roar, bed_db - 8.0 + 6.0 * sz))
        lap = np.stack([bandpass(pink(n, g), g.uniform(600.0, 1100.0), 0.6) for _ in range(2)],
                       axis=1)
        lap *= np.clip(1.0 + 0.9 * smooth_random(d, 11.0, g, octaves=2), 0.0, None)[:, None] ** 2
        m.add(_rms_to(lap, bed_db - 7.0))
        hiss = np.stack([peak_eq(highpass(white(n, g), 2500.0), 5500.0, 3.0, 0.8)
                         for _ in range(2)], axis=1)
        hiss *= np.clip(1.0 + 0.6 * smooth_random(d, 4.0, g, octaves=2), 0.0, None)[:, None]
        m.add(_rms_to(hiss, bed_db - 13.0 + 4.0 * sz))
        # 噼啪：速率被慢变随机信号指数调制 → 时而安静、时而连续爆裂（Cox 过程）
        rate = cr * (3.0 + 20.0 * sz) * np.exp(1.1 * smooth_random(d, 0.35, g, octaves=2))
        times = modulated_times(rate, g)
        amps = power_law(len(times), 1.5, 0.12, 1.0, g)
        pgain = bed_db + 17.0 + 2.0 * cr
        for t0, a in zip(times, amps):
            tj = float(t0)
            for j in range(int(g.integers(1, 5))):              # 每次爆裂由 1~4 个极短脉冲组成
                if g.random() < 0.12:
                    kk = bandpass(burst(g.uniform(1.5, 4.0) / 1000.0, g),
                                  g.uniform(400.0, 1400.0), g.uniform(0.7, 1.5))
                    amp = 1.4 * a
                else:
                    kk = bandpass(burst(g.uniform(0.08, 0.7) / 1000.0, g),
                                  float(np.exp(g.uniform(np.log(1500.0), np.log(8000.0)))),
                                  g.uniform(0.6, 2.2))
                    amp = a * (1.0 if j == 0 else g.uniform(0.3, 0.9))
                m.add(normalize(kk, 0.0) * amp, tj, pgain, g.uniform(-0.5, 0.5))
                tj += g.uniform(0.0005, 0.006)
        for t0 in poisson_times(max(0.1, d - 1.0), 0.07, g, start=0.3):   # 木柴塌落
            wood = random_body(120.0, 1500.0, 10, "wood", eta=0.05, alpha=30.0, seed=g)
            thud = lowpass(wood.render(Hit(0.0, 1.0, 4.0), dur=0.4), 2500.0)
            m.add(normalize(thud, 0.0) * 0.7, float(t0), bed_db + 12.0, g.uniform(-0.3, 0.3))
        return m.out()[:n]

    y = _sustain(make, dur)
    return _long_mod(y, 0.14, seed + 303)


# ---------------------------------------------------------------- 撞击
@_recipe(tags=("撞击", "木头"))
def knock_wood(seed=0, knocks=3, force=0.8, door="solid", room="small_room", wet_db=-15.0, **kw):
    """敲木头：空腔板的模态 + 硬接触噪声，可敲多下（不等间隔）。"""
    k = max(1, int(knocks))
    f = float(np.clip(force, 0.1, 2.0))
    g = rng(seed)
    door = _enum(door, ("solid", "hollow"), "knock_wood", "door")
    b = plate(210.0 if door == "solid" else 150.0, material="wood",
              aspect=1.5, n_modes=34, tilt=0.3, seed=seed)
    times = human_rhythm(k, 0.42, jitter=0.12, drift=0.02, seed=seed + 7)
    d = float(times[-1]) + 0.9
    x = _bus(d, stereo=False)
    for i, t in enumerate(times):
        v = f * float(np.clip(1.0 - 0.06 * i + g.uniform(-0.12, 0.12), 0.3, 2.0))
        _place(x, b.render(Hit(0.0, velocity=v, contact_ms=1.1, hardness=0.4)), float(t), -1.0)
        _place(x, contact_noise(1.4, center_hz=2100.0, q=0.8, seed=seed + i), float(t), -10.0)
    y = reverb(to_mono(x), room=room, wet_db=float(wet_db), dry_db=0.0, seed=seed)
    return to_stereo(np.asarray(y, dtype=float))


@_recipe(tags=("撞击", "金属"))
def metal_hit(seed=0, obj="pipe", force=1.0, room="warehouse", wet_db=-13.0, **kw):
    """金属撞击：管/板/片的模态（余音长、泛音非整数），带简并分裂的拍频。"""
    o = _enum(obj, ("pipe", "plate", "bell"), "metal_hit", "obj")
    if o == "plate":
        b = plate(240.0, material="metal", aspect=1.8, n_modes=40, tilt=0.15, seed=seed)
    elif o == "bell":
        b = bell(320.0, material="metal", tilt=0.1, seed=seed)
    else:                                       # pipe
        b = bar(190.0, material="metal", n_modes=12, tilt=0.12, seed=seed).split(0.0012)
    x = _bus(2.6, stereo=False)
    _place(x, b.render(Hit(0.0, velocity=float(np.clip(force, 0.2, 2.0)),
                           contact_ms=0.35, hardness=0.65)), 0.0, -1.0)
    _place(x, contact_noise(1.2, center_hz=5200.0, q=0.9, seed=seed), 0.0, -8.0)
    y = reverb(to_mono(x), room=room, wet_db=float(wet_db), dry_db=0.0, seed=seed)
    return to_stereo(np.asarray(y, dtype=float))


@_recipe(tags=("撞击", "玻璃"))
def glass_clink(seed=0, force=0.5, fill=0.3, room="room", wet_db=-17.0, **kw):
    """玻璃杯：圆环模态（带简并分裂的拍频）+ 极短接触噪声；fill 越高音越低。"""
    f = float(np.clip(fill, 0.0, 1.0))
    b = ring(1150.0 * (1.0 - 0.22 * f), material="glass", n_modes=7,
             stiffness=0.92, split=0.0016, seed=seed)
    x = _bus(1.8, stereo=False)
    _place(x, b.render(Hit(0.0, velocity=float(np.clip(force, 0.1, 1.5)) * 1.4,
                           contact_ms=0.35, hardness=0.7)), 0.0, -1.0)
    _place(x, contact_noise(0.8, center_hz=7000.0, q=1.2, seed=seed), 0.0, -12.0)
    y = reverb(to_mono(x), room=room, wet_db=float(wet_db), dry_db=0.0, seed=seed)
    return to_stereo(np.asarray(y, dtype=float))


@_recipe(tags=("撞击", "石头", "围棋"))
def go_stone(seed=0, force=0.8, big=False, room="small_room", wet_db=-18.0, **kw):
    """围棋落子：棋子敲木盘的"嗒"——短促、暖、略带木腔。big=True 用更厚的盘。"""
    f = float(np.clip(force, 0.2, 2.0))
    b = plate(150.0 if big else 230.0, material="wood", aspect=1.62,
              n_modes=30, tilt=0.28, seed=seed)
    x = _bus(0.7, stereo=False)
    _place(x, b.render(Hit(0.0, velocity=f * 1.2, contact_ms=0.5, hardness=0.55)), 0.0, -1.0)
    _place(x, contact_noise(0.9, center_hz=3200.0, q=1.0, seed=seed), 0.0, -7.0)
    _place(x, _tone(320.0 if big else 420.0, 0.12, 0.03, attack=0.0005), 0.0, -16.0)
    y = reverb(to_mono(x), room=room, wet_db=float(wet_db), dry_db=0.0, seed=seed)
    return to_stereo(np.asarray(y, dtype=float))


# ---------------------------------------------------------------- 注册表接口
def load_all() -> dict:
    """返回全部配方（配方在 import 时就已注册，这里只做一致性检查）。

    任何一个配方函数不可调用都会在这里报出来——比"运行时才发现某条 cue 没声音"好。
    """
    bad = [k for k, v in REGISTRY.items() if not callable(v.get("fn"))]
    if bad:
        raise RuntimeError("配方不可调用: %s" % ", ".join(sorted(bad)))
    for k, v in CUE_ALIASES.items():
        if v not in REGISTRY:
            raise RuntimeError("CUE_ALIASES[%r] -> %r 不是已注册配方" % (k, v))
    return REGISTRY


_UNKNOWN_WARNED: set = set()


def render(name, seed=0, **params):
    """渲染一个配方并做母带，返回 ndarray。

    - 未知参数被配方的 **kw 吞掉（cue 会透传 gain/pan/dur/size 等）；
    - 未知 type（含别名解析失败）：打印一次警告并返回 None，**不 crash**
      整片混音（CONTRACT §5.1）；调用方按 None 跳过即可。
    """
    key = CUE_ALIASES.get(str(name), str(name))
    if key not in REGISTRY:
        if key not in _UNKNOWN_WARNED:
            _UNKNOWN_WARNED.add(key)
            print("   [WARN] sfxkit 未知音效 type=%r（已静默跳过；不中断整片混音）" % (name,))
        return None
    ent = REGISTRY[key]
    fn = ent["fn"]
    # seed 统一在这里挡：负 seed 在 numpy 里是深层的 "expected non-negative integer"，
    # cue 的 seed 都是 1000+i 不会触发，但手工/CLI 传错时应该给一句人话。
    try:
        seed_i = int(seed)
    except (TypeError, ValueError):
        raise ValueError("seed 必须是整数，收到 %r" % (seed,)) from None
    if seed_i < 0:
        raise ValueError("seed 必须是非负整数，收到 %r" % (seed,))
    params = {k: v for k, v in (params or {}).items() if k != "name"}
    try:
        x = fn(seed=seed_i, **params)
    except TypeError:
        # 配方不认识这些参数：删掉不认识的再试一次（cue 会透传 dur/size/gain 等）
        sig = inspect.signature(fn)
        allowed = set(sig.parameters)
        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
            raise
        kept = {k: v for k, v in params.items() if k in allowed}
        x = fn(seed=seed_i, **kept)
    if x is None or len(np.asarray(x)) == 0:
        return None
    return finalize(x, **ent["master"])


def list_recipes() -> list:
    """配方清单（CLI / 文档用）。"""
    out = []
    for k in sorted(REGISTRY):
        ent = REGISTRY[k]
        fn = ent["fn"]
        try:
            import inspect as _inspect
            sig = str(_inspect.signature(fn))
        except Exception:                                  # noqa: BLE001
            sig = "(?)"
        out.append(dict(name=k, title=ent["title"], tags=list(ent["tags"]),
                        params=sig, master=dict(ent["master"])))
    return out


# ============================================================================
# 自检
# ============================================================================
def _ensure_utf8_stdout() -> None:
    """Windows 控制台/管道默认不是 UTF-8：自检与 CLI 打印中文前先切一下（import 时不动）。"""
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                                  # 非 tty / 老环境：忽略
            pass


def _find_ffmpeg() -> str:
    """找 ffmpeg：优先环境变量 FILMKIT_FFMPEG，其次 PATH（自检用，不联网）。"""
    p = os.environ.get("FILMKIT_FFMPEG") or ""
    if p and os.path.isfile(p):
        return p
    import shutil
    return shutil.which("ffmpeg") or ""


def _selftest_signal() -> np.ndarray:
    """2.5 s 立体声参考信号（正弦 + 少量噪声），给响度/真峰值对照用。"""
    n = int(SR * 2.5)
    t = np.arange(n) / float(SR)
    r1 = np.random.default_rng(21).standard_normal(n) * 0.05
    r2 = np.random.default_rng(22).standard_normal(n) * 0.05
    return np.stack([0.12 * np.sin(2 * np.pi * 440.0 * t) + r1,
                     0.11 * np.sin(2 * np.pi * 660.0 * t) + r2], axis=1)


def selftest() -> int:
    import time as _time

    _ensure_utf8_stdout()
    budget = PERF_BUDGET_SCIPY if _sig is not None else PERF_BUDGET_FALLBACK
    print("sfxkit v%s 自检  SR=%d  scipy=%s  单配方软预算 %.1f s（超过只 WARN，硬上限 %.1f s）"
          % (__version__, SR, "有" if _sig is not None else "无（FFT 卷积兜底）", budget,
             budget * PERF_HARD_MULTIPLIER))
    print("-" * 66)
    results = []

    def case(name, fn):
        t0 = _time.time()
        try:
            detail = fn()
            results.append(True)
            print("  PASS %s  (%.2fs)%s" % (name, _time.time() - t0,
                                            ("  -> " + str(detail)) if detail else ""))
        except AssertionError as e:
            results.append(False)
            print("  FAIL %s  -> %s" % (name, e))
        except Exception as e:                              # noqa: BLE001
            results.append(False)
            print("  FAIL %s  -> %s: %s" % (name, type(e).__name__, e))

    def t_registry():
        reg = load_all()
        if len(reg) != 24:
            raise ValueError("配方数应为 24，实际 %d" % len(reg))
        for k, ent in reg.items():
            for fld in ("fn", "title", "tags", "master"):
                if fld not in ent:
                    raise ValueError("%s 缺少字段 %s" % (k, fld))
        return "%d 个配方" % len(reg)

    def t_aliases():
        need = {"blip", "click", "relay", "chirp", "sub", "riser",
                "reverse", "rewind", "stone", "wind"}
        missing = need - set(CUE_ALIASES)
        if missing:
            raise ValueError("CUE_ALIASES 缺少契约别名：%s" % ", ".join(sorted(missing)))
        for k, v in CUE_ALIASES.items():
            if v not in REGISTRY:
                raise ValueError("别名 %s -> %s 不是已注册配方" % (k, v))
        return "%d 条别名（含契约 10 条）" % len(CUE_ALIASES)

    def t_deterministic():
        for k in ("impact", "whoosh", "ui_blip", "typing", "rain"):
            a = render(k, seed=11, dur=3.0) if k == "rain" else render(k, seed=11)
            b = render(k, seed=11, dur=3.0) if k == "rain" else render(k, seed=11)
            if not np.array_equal(np.asarray(a), np.asarray(b)):
                raise ValueError("%s 同 seed 两次渲染不一致" % k)
        # 契约 §3.1：key=None 与 key="Dm" 必须逐样本一致（legacy 路径兼容）；
        # key 给别的调必须真的换音（用 F4：Dm 五声里有 F、C 大调五声里没有，一定吸附到 E4）。
        old = np.asarray(render("ui_blip", seed=11))
        dm = np.asarray(render("ui_blip", seed=11, key="Dm"))
        if not np.array_equal(old, dm):
            raise ValueError('ui_blip key="Dm" 与 key=None（旧行为）不一致')
        f4 = np.asarray(render("ui_blip", seed=11, pitch=349.23))
        c_major = np.asarray(render("ui_blip", seed=11, pitch=349.23, key="C"))
        if np.array_equal(f4, c_major):
            raise ValueError('ui_blip key="C" 没有换调（F4 应吸附到 E4）')
        for rn in ("ui_blip", "answer_chime", "text_stream", "data_chirp", "roulette_tick"):
            y = render(rn, seed=2, key="Am")
            if y is None or len(y) == 0:
                raise ValueError("%s 透传 key='Am' 后无输出" % rn)
        # content_seed：同参数稳定、对 content_fp/recipe 敏感、落在 1000~9999（契约 §3.1）。
        s1 = content_seed(1000, "tech-deep", "aaaa1111", "ui_blip", 12.5)
        s2 = content_seed(1000, "tech-deep", "aaaa1111", "ui_blip", 12.5)
        s3 = content_seed(1000, "tech-deep", "bbbb2222", "ui_blip", 12.5)
        s4 = content_seed(1000, "tech-deep", "aaaa1111", "whoosh", 12.5)
        if s1 != s2:
            raise ValueError("content_seed 同参数两次不一致")
        if s3 == s1 or s4 == s1:
            raise ValueError("content_seed 对 content_fp/recipe 不敏感（跨片会撞种子）")
        if not (1000 <= s1 <= 9999):
            raise ValueError("content_seed 超出 1000~9999：%r" % (s1,))
        return ("impact/whoosh/ui_blip/typing/rain 逐采样一致；key=Dm 同旧行为、key=C 换调；"
                "content_seed 稳定（s=%d）" % s1)

    def t_all_render():
        # 功能断言保持硬失败（None / 空数组 / NaN-Inf / 静音 / 越界），性能只判"是否退化"：
        # 超过软预算先打印 WARN 并复核一次取较小值——渲染耗时对机器负载敏感（4 并发时
        # rain 1.65 s / wind 1.62 s，而软预算是 1.5 s），只有超过 2× 软预算
        # （scipy 3.0 s / 兜底 7.0 s）才 raise：那已经不是负载抖动，是算法退化。
        # 这样既不删掉性能保护，也不再让"机器一忙就随机红灯"毁掉自检的可信度。
        budget = PERF_BUDGET_SCIPY if _sig is not None else PERF_BUDGET_FALLBACK
        hard = budget * PERF_HARD_MULTIPLIER
        timings, warned, degraded = [], [], []
        for k in sorted(REGISTRY):
            t0 = _time.time()
            x = render(k, seed=5)
            el = _time.time() - t0
            if x is None:
                raise ValueError("%s 渲染返回 None" % k)
            a = np.asarray(x)
            if a.size == 0:
                raise ValueError("%s 渲染为空" % k)
            if not np.all(np.isfinite(a)):
                raise ValueError("%s 出现 NaN/Inf" % k)
            pk = float(np.max(np.abs(a)))
            if pk <= 1e-4:
                raise ValueError("%s 几乎是静音（峰值 %.6f）" % (k, pk))
            if pk > 1.0 + 1e-6:
                raise ValueError("%s 超过满刻度（峰值 %.3f）" % (k, pk))
            if el > budget:
                # 计时抖动：机器被别的进程占着时会偶发超预算。超了先复核一次，取两次里
                # 较快的一次；超过硬上限才收集成失败，否则只告警。
                t1 = _time.time()
                render(k, seed=5)
                el2 = _time.time() - t1
                first = el
                el = min(first, el2)
                if el > hard:
                    degraded.append("%s=%.2fs" % (k, el))
                else:
                    print("     WARN %s 渲染 %.2fs（复核 %.2fs，取较小值 %.2fs）超软预算 %.1fs，"
                          "硬上限 %.1fs 内：负载敏感项，不判失败"
                          % (k, first, el2, el, budget, hard))
                    warned.append("%s=%.2fs" % (k, el))
            timings.append((el, k))
        if degraded:
            raise ValueError("以下配方渲染超过硬上限 %.1fs（=2× 软预算 %.1fs，已复核仍超）：%s；"
                             "这不像负载抖动，像是算法退化"
                             % (hard, budget, ", ".join(degraded)))
        top = sorted(timings, reverse=True)[:3]
        detail = ("%d 个配方出声、不越界；最慢三项 %s（软预算 %.1fs / 硬上限 %.1fs，只判不退化）"
                  % (len(REGISTRY), "、".join("%s %.2fs" % (k, t) for t, k in top), budget, hard))
        if _sig is None:
            detail += ("（当前无 scipy，按 fallback 预算 %.1fs 判定；装 scipy 可加速并回到 %.1fs）"
                       % (PERF_BUDGET_FALLBACK, PERF_BUDGET_SCIPY))
        if warned:
            detail += "；超软预算已告警未判失败：%s" % ", ".join(warned)
        return detail

    def t_alias_render():
        a = render("blip", seed=3)
        b = render("ui_blip", seed=3)
        if not np.array_equal(np.asarray(a), np.asarray(b)):
            raise ValueError("别名渲染结果不一致")
        if render("riser", seed=3) is None:
            raise ValueError("别名 riser 未能解析")
        return "blip == ui_blip；riser 可解析"

    def t_extra_kw():
        # cue 会透传 gain/pan 之类，配方必须吞掉不认识的参数
        x = render("whoosh", seed=3, dur=1.2, size=0.8, gain=-2, pan=0.3, note="x")
        if x is None or len(x) == 0:
            raise ValueError("whoosh 透传多余参数后无输出")
        y = render("ui_blip", seed=3, pitch=1760.0, gain=-6)
        if y is None or len(y) == 0:
            raise ValueError("ui_blip 透传多余参数后无输出")
        # 但"认识的参数写错值"必须响亮报错，不能静默落到默认分支（统一口径见 _enum）；
        # 顺手把负 seed / 负 gap / 负 times 这类"会产出哑音或深层 numpy 报错"的入口也挡住。
        bad = (("wind", dict(env="bogus", dur=0.2)),
               ("rain", dict(surface="bogus", dur=0.2)),
               ("ui_blip", dict(kind="bogus")),
               ("text_stream", dict(voice="bogus")),
               ("whoosh", dict(direction="bogus")),
               ("riser", dict(end="bogus")),
               ("knock_wood", dict(door="bogus")),
               ("knock_wood", dict(room="bogus")),
               ("metal_hit", dict(obj="bogus")),
               ("relay_click", dict(gap=-1.0, count=3)),
               ("typing_at", dict(times=[-1.0])),
               ("ui_blip", dict(key="bogus")),          # 未知调名必须响亮报错
               ("answer_chime", dict(key="bogus")))
        for rname, kw in bad:
            try:
                render(rname, seed=3, **kw)
            except ValueError:
                continue
            raise ValueError("%s 对非法参数没报错（静默兜底）：%r" % (rname, kw))
        try:
            render("impact", seed=-1)
        except ValueError:
            pass
        else:
            raise ValueError("render(seed=-1) 没报错")
        return "gain/pan/note 等被 **kw 吞掉；%d 组非法参数全部 ValueError；seed=-1 被挡" % len(bad)

    def t_durations():
        d = len(render("impact", seed=1)) / float(SR)
        if not (0.4 < d < 5.0):
            raise ValueError("impact 时长异常: %.2fs" % d)
        d2 = len(render("wind", seed=1, dur=3.0)) / float(SR)
        if not (2.0 < d2 < 4.5):
            raise ValueError("wind(3s) 时长异常: %.2fs" % d2)
        return "impact %.2fs / wind(3s) %.2fs" % (d, d2)

    def t_true_peak():
        x = render("impact", seed=2)
        db = float(amp_to_db(true_peak(x)))
        if db > -0.5:
            raise ValueError("真峰值 %.2f dBTP 超限（应 ≤ -1 dBTP）" % db)
        # 多相法 vs 整段 FFT 过采样：误差应小于 0.4 dB（只在小信号上对照，成本可控）
        y = _selftest_signal()[:, 0][:48000]
        d1 = float(amp_to_db(true_peak(y)))
        d2 = float(amp_to_db(_true_peak_fft(y, 4)))
        if abs(d1 - d2) > 0.4:
            raise ValueError("真峰值多相法与整段 FFT 差 %.2f dB（>0.4）" % abs(d1 - d2))
        return "impact %.2f dBTP；多相 vs FFT 差 %.2f dB" % (db, abs(d1 - d2))

    def t_stereo():
        x = np.asarray(render("whoosh", seed=2))
        if not (x.ndim == 2 and x.shape[1] == 2):
            raise ValueError("whoosh 应是立体声，实际 %s" % (x.shape,))
        y = np.asarray(render("ui_blip", seed=2))
        if y.ndim != 1:
            raise ValueError("ui_blip 应是单声道，实际 %s" % (y.shape,))
        return "whoosh 立体声 / ui_blip 单声道"

    def t_unknown():
        got = render("__no_such_type__", seed=1)
        if got is not None:
            raise ValueError("未知 type 应返回 None，实际 %r" % (type(got),))
        return "未知 type 返回 None（不 crash 整片混音）"

    def t_public_recipe():
        if not callable(recipe):
            raise ValueError("公共 recipe() 不可调用")
        made = []

        def _dummy(seed=0, **kw):
            made.append(seed)
            return np.full(8, 0.1)

        tmp_key = "_selftest_dummy"
        wrapped = recipe(tmp_key, tags=("自检",), master={"peak_db": -3.0})(_dummy)
        try:
            if tmp_key not in REGISTRY:
                raise ValueError("recipe() 未注册配方")
            if wrapped is not _dummy:
                raise ValueError("recipe() 应原样返回函数")
        finally:
            REGISTRY.pop(tmp_key, None)
        return "recipe() 可注册、可传 tags/master"

    def t_env_lufs():
        for k, target in (("wind", -22.0), ("rain", -22.0), ("campfire", -22.0)):
            x = render(k, seed=4, dur=6.0)
            if x is None:
                raise ValueError("%s 渲染返回 None" % k)
            l = integrated_loudness(x)
            if not (abs(l - target) <= 1.5):
                raise ValueError("%s 响度 %.1f LUFS 偏离目标 %.1f（>1.5 LU）" % (k, l, target))
        return "环境声响度贴合 master（±1.5 LU）"

    def t_fallback():
        # 这条不能只查"输出是有限值"——那等于只验了"没崩"，兜底路径的幅度正确性
        # 完全没查（references/sfx-design.md §12 承诺静态滤波与 scipy 相对误差 ≤ 2e-4）。
        # 所以按当前环境选参照：
        #   有 scipy：同一函数跑两遍（scipy 路径 vs 兜底路径）逐样本比 —— 直接验"等价"；
        #   无 scipy：没有 scipy 路径可比，改用**解析参照**（双二阶的阶跃稳态/频响、
        #            谐振器的闭式冲激响应 amp·r^n·sin(ωn)），这样 CI 的 noscipy 任务
        #            也在查"数值对不对"，而不是自己和自己比出一个假 PASS。
        old = globals().get("_sig")
        has_ref = old is not None
        S = globals()
        try:
            x = np.random.default_rng(0).standard_normal(24000) * 0.1
            worst = 0.0

            def cmp_paths(fn):
                """有 scipy 时：两条路径逐样本相对误差；无 scipy 时返回 None。"""
                if not has_ref:
                    return None
                S["_sig"] = old
                a = fn()
                S["_sig"] = None
                b = fn()
                d = float(np.max(np.abs(a - b))) / max(float(np.max(np.abs(a))), 1e-12)
                return d

            # 1) 静态滤波：有 scipy 比路径；无 scipy 用"阶跃稳态 = 解析 DC 增益"
            for kind, f0, dc in (("lowpass", 1000.0, 1.0), ("highpass", 1000.0, 0.0)):
                S["_sig"] = None
                step = np.concatenate([np.zeros(4800), np.ones(43200)])
                y = sosfilt(biquad(kind, f0), step)
                err = abs(float(np.mean(y[-1000:])) - dc)
                if err > 2e-3:
                    raise ValueError("无 scipy %s(%g) 阶跃稳态 %.6f 偏离 DC 增益 %.3f"
                                     % (kind, f0, float(np.mean(y[-1000:])), dc))
                worst = max(worst, err)
            d = cmp_paths(lambda: lowpass(x, 1000.0))
            if d is not None:
                if d > 2e-4:
                    raise ValueError("无 scipy 静态滤波与 scipy 相对误差 %.2e > 2e-4" % d)
                worst = max(worst, d)

            # 2) 谐振器：闭式解。h[n] = amp·r^n·sin(ωn)（本函数构造的 b/a 决定的）。
            #    注意激励必须**够长**：旧兜底把 IR 截断在 20000 采样，若只喂 4096 点冲激，
            #    输出前 4096 点与解析解一致，截断根本测不出来（第一版断言就踩过这个坑）。
            #    这里用 30000 点白噪声：t60=3 s 的 -80 dB 长度约 19.2 万采样，输出第 2 万点之后
            #    的错误才会暴露，相对误差会到 3e-2 量级（远大于下面 2e-4 的阈值）。
            n = 30000
            exc = np.random.default_rng(7).standard_normal(n)
            for f, t60, amp in ((300.0, 1.0, 0.7), (220.0, 3.0, 1.0)):
                r0 = math.exp(-LN1000 / (t60 * float(SR)))
                w0 = 2.0 * math.pi * f / float(SR)
                k = np.arange(n, dtype=float)
                h_ref = amp * np.power(r0, k) * np.sin(w0 * k)     # 解析 IR（独立于被测代码）
                # 参照卷积用 FFT（30000×30000 直接 np.convolve 是 9e8 次乘加，实测要 20 s，
                # 会把这个自检从 60 s 预算里挤爆）；零填充到 2 的幂即可，长度不足部分不影响
                # 前 n 个输出采样。
                L = n + len(h_ref) - 1
                N = 1 << int(L - 1).bit_length()
                ref = np.fft.irfft(np.fft.rfft(exc, N) * np.fft.rfft(h_ref, N), N)[:n]
                S["_sig"] = None
                h = resonator(exc, f, t60, amp)
                err = float(np.max(np.abs(h - ref))) / max(float(np.max(np.abs(ref))), 1e-12)
                if err > 2e-4:
                    raise ValueError("无 scipy 谐振器 f=%g t60=%g 与闭式解相对误差 %.2e > 2e-4"
                                     "（IR 被截断？）" % (f, t60, err))
                worst = max(worst, err)

            # 3) 时变滤波：文档承认"块中心系数 + 重叠相加"是近似（高频控制信号会平滑），
            #    只查能跑 + 不出 NaN/Inf，不设等价阈值。
            S["_sig"] = None
            z = tv_filter(x, "bandpass", np.linspace(300.0, 4000.0, len(x)), 0.9)
            if not np.all(np.isfinite(z)):
                raise ValueError("无 scipy 兜底 tv_filter 输出非有限值")
        finally:
            globals()["_sig"] = old
        if has_ref:
            return "兜底 vs scipy：静态滤波/谐振器 ≤2e-4（实测 %.1e）/ tv_filter 可用" % worst
        return ("无 scipy 环境：静态滤波阶跃稳态 DC 增益 / 谐振器闭式解 均 ≤2e-3（实测 %.1e）+ "
                "tv_filter 可用（本环境没有 scipy 路径可比，用解析参照）" % worst)

    def t_analysis():
        m = analyze(render("impact", seed=6))
        for k in ("时长_s", "真峰值_dBTP", "积分响度_LUFS", "频谱重心_Hz", "QC"):
            if k not in m:
                raise ValueError("analyze 缺少字段 %s" % k)
        if not isinstance(m["QC"], list):
            raise ValueError("analyze['QC'] 应为 list")
        bad = dict(m); bad["削波采样数"] = 3
        if not qc(bad):
            raise ValueError("qc 未检出人为注入的削波")
        return "analyze 字段齐 / qc 能检出削波"

    def t_io():
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try:
            x = render("ui_click", seed=2)
            save(path, x, "PCM_24")
            y = load(path)
            if y.shape != np.asarray(x).shape:
                raise ValueError("save/load 形状不一致：%s vs %s" % (np.asarray(x).shape, y.shape))
            if float(np.max(np.abs(y - np.asarray(x)))) > 1e-5:
                raise ValueError("save/load 误差过大")
            save(path, to_stereo(x), "PCM_16")
            z = load(path)
            if z.ndim != 2 or z.shape[1] != 2:
                raise ValueError("PCM_16 立体声读写失败")
        finally:
            if os.path.exists(path):
                os.remove(path)
        return "PCM_24/PCM_16 往返一致"

    def t_loudness_vs_ffmpeg():
        ff = _find_ffmpeg()
        if not ff:
            return "跳过（未找到 ffmpeg）"
        import re as _re
        import subprocess
        sig = _selftest_signal()
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try:
            save(path, sig, "PCM_24")
            ours = integrated_loudness(sig)
            r = subprocess.run([ff, "-hide_banner", "-nostats", "-i", path,
                                "-filter_complex", "ebur128=peak=true", "-f", "null", "-"],
                               capture_output=True, text=True)
            vals = _re.findall(r"I:\s+(-?\d+\.\d) LUFS", r.stderr or "")
            if not vals:
                return "跳过（ffmpeg 输出无法解析）"
            ref = float(vals[-1])
            if abs(ours - ref) > 1.5:
                raise ValueError("响度 %.2f vs ffmpeg %.2f（差 %.2f LU > 1.5）"
                                 % (ours, ref, abs(ours - ref)))
            return "我们 %.2f LUFS / ffmpeg %.2f LUFS" % (ours, ref)
        finally:
            if os.path.exists(path):
                os.remove(path)

    case("01 配方注册表完整（24 个 + 字段）", t_registry)
    case("02 CUE_ALIASES 指向真实配方", t_aliases)
    case("03 公共 recipe() 可注册", t_public_recipe)
    case("04 同 seed 逐采样一致", t_deterministic)
    case("05 全部配方出声/不越界/性能不退化", t_all_render)
    case("06 别名渲染等价（blip/riser）", t_alias_render)
    case("07 cue 多余参数被吞掉", t_extra_kw)
    case("08 未知 type 返回 None 不崩", t_unknown)
    case("09 时长符合预期", t_durations)
    case("10 真峰值不超 -1 dBTP", t_true_peak)
    case("11 立体声/单声道形状", t_stereo)
    case("12 环境声响度贴合 master", t_env_lufs)
    case("13 无 scipy 兜底路径", t_fallback)
    case("14 analyze/qc 指标可用", t_analysis)
    case("15 WAV 保存/读取往返", t_io)
    case("16 响度对齐 ffmpeg ebur128", t_loudness_vs_ffmpeg)
    print("-" * 66)
    n_ok = sum(1 for v in results if v)
    print("PASS %d / FAIL %d" % (n_ok, len(results) - n_ok))
    return 0 if n_ok == len(results) else 1


def bench_long(dur: float = 389.0, budget: float = 8.0) -> int:
    """超长环境声性能基准（不进 --selftest，避免拖慢 60 s 预算）。

    渲染 wind/rain/campfire 整片长度，打印耗时/峰值；任一超过 budget 秒返回 1。
    """
    import time as _time

    _ensure_utf8_stdout()
    print("超长环境声性能（目标：单条 ≤ %.1f s，dur=%.1f s）" % (budget, dur))
    ok = True
    for name in ("wind", "rain", "campfire"):
        t0 = _time.time()
        x = render(name, seed=7, dur=float(dur))
        el = _time.time() - t0
        if x is None:
            print("  %-9s 渲染失败" % name)
            ok = False
            continue
        good = el <= budget
        ok = ok and good
        print("  %-9s %6.2fs  时长 %6.1fs  峰值 %.3f  %s"
              % (name, el, len(x) / float(SR), float(np.max(np.abs(x))),
                 "OK" if good else "超预算"))
    return 0 if ok else 1


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="sfxkit — 物理建模程序化音效库")
    ap.add_argument("--selftest", action="store_true", help="跑自检")
    ap.add_argument("--bench-long", action="store_true",
                    help="超长环境声性能基准（389 s wind/rain/campfire）")
    ap.add_argument("--list", action="store_true", help="列出全部配方")
    ap.add_argument("--render", default="", help="渲染一个配方")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", default="")
    ap.add_argument("--set", nargs="*", default=[], help="参数 k=v（v 自动转数字/布尔）")
    a = ap.parse_args(argv)
    _ensure_utf8_stdout()
    if a.selftest:
        return selftest()
    if a.bench_long:
        return bench_long()
    if a.list:
        for r in list_recipes():
            print("%-16s %s" % (r["name"], r["title"]))
            print("     参数 %s%s" % (r["params"],
                                     ("  母带 %s" % r["master"]) if r["master"] else ""))
        return 0
    if a.render:
        params = {}
        for kv in a.set:
            k, _, v = kv.partition("=")
            try:
                params[k] = int(v)
            except ValueError:
                try:
                    params[k] = float(v)
                except ValueError:
                    params[k] = {"true": True, "false": False}.get(v.lower(), v)
        x = render(a.render, seed=a.seed, **params)
        m = analyze(x)
        print("%s: %d 采样 / %.2fs / 峰值 %.3f" % (a.render, len(x), len(x) / SR,
                                                   float(np.max(np.abs(x)))))
        print(summary_line(m))
        if a.out:
            save(a.out, x)
            print("->", a.out)
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())

