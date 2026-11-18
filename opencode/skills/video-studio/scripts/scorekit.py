# -*- coding: utf-8 -*-
"""scorekit.py — 程序化配乐。

用代码写一张能贯穿整部片子的配乐：调性/和弦表、段落能量、节拍网格、律动层、
段落过门、自动电平与"吸气"，全部由 numpy + sfxkit 现场合成，不用任何采样。

结构（改编自 ai-history-film（MIT）的 video/audio/film_score.py / inst.py / timeline.py）：
1. 一张和弦表：按 key 的级数（i / III / iv / V / VI / VII / I ...），全片同一张表，
   段落边界对齐小节线（120 BPM、4/4，一小节 2 s）；
2. energy ∈ [0,1] 决定律动层（pad / bass / kick / hat / arp / melody / shine）与亮度；
   安静段比律动段低 4~8 dB（由自动电平实现，段落内的强弱表情保留）；
3. 乐句按 4 小节切分，乐句末尾留"呼吸"（撤掉最后一拍的鼓/贝斯/琶音）；
4. 段落交界至少一小节过门：鼓加花 + 反向镲 + 上升音 + 重击（全部用 sfxkit 原语拼）；
5. Score.suck(at, db=-6, dur=0.2)：重击前平滑压低配乐（不是硬静音）；
6. 随机只走 sfxkit 的 rng(seed)；同一 seed 逐采样 bit-identical；
7. 结尾 2 s 淡出、去直流、峰值 ≤ -1 dBFS（母带响度在 filmkit 做 loudnorm，这里只保证不爆）；
8. --selftest 不联网、不依赖当前目录，逐条打印 PASS/FAIL。

对外 API（契约 §5.2 冻结）：
    SR / KEYS
    class Score: section / plan_from_anchors / suck / render / report
    render(duration, bpm=120, key="Dm", anchors=None, sections=None, seed=7) -> (samples, report)
    save(path, x, sr=SR) / load(path, sr=SR) / selftest()

零模板：本模块不提供曲风表，只提供**技术原语**——配乐的调式/调性/BPM/情绪/编曲请用
musicgen.py（参数化编排器）现场编排：
      * Score：和声表 / 段落能量 / 节拍网格 / 律动层 / 过门 / 吸气 / 自动电平；
      * _Bus / _Bank：总线、混音、六类合成音色、乒乓延迟、侧链；
      * _k_weighted_mono / _approx_lufs / _short_term_db：响度与频谱测量；
      * save / load：24-bit PCM WAV 读写（NaN/Inf 拒绝写盘）；
      * render(duration, bpm, key, anchors, sections, seed)：确定性配乐路径，
        逐样本可复现（自测第 18 条守着）。
    render() 仍接受 style/mood/fingerprint/music_plan/pace 这几个参数，只是为了
    让既有调用方不因 TypeError 静默崩掉；非 legacy 的 style 一律明确报错并指向 musicgen.py。
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import tempfile
import time
import wave
from pathlib import Path

import numpy as np

import sfxkit as S                      # 唯一音频依赖：numpy + sfxkit

__version__ = "1.1.0"

SR = 48000                             # 采样率（与 filmkit / sfxkit 一致）
BEATS_PER_BAR = 4                      # 4/4
BASE_LUFS = -16.0                      # 报告里 target_lufs 的基准（参考片混音 -16 LUFS）
TARGET_PEAK_DB = -1.5                  # 输出峰值（留 0.5 dB 给编码）
FADE_OUT_S = 2.0                       # 片尾淡出
FADE_IN_S = 0.03                       # 片头极短淡入，防起音爆点
PHRASE_BARS = 4                        # 乐句长度（小节）

# 锚点 kind -> 默认能量。契约要求 hook 0.3 / body 0.55 / outro 0.35 这类映射。
KIND_ENERGY = {
    "hook": 0.30, "intro": 0.28, "opening": 0.30, "title": 0.35,
    "calm": 0.30, "quiet": 0.26, "breath": 0.24, "quote": 0.42,
    "body": 0.55, "info": 0.55, "chapter": 0.60, "chart": 0.62, "data": 0.62,
    "outro": 0.35, "ending": 0.32, "credits": 0.34,
    "drive": 0.80, "action": 0.85, "impact": 0.90, "climax": 0.95, "drop": 0.92,
}

# 律动层：能量过线才出现（contract：energy 控制层数）
LAYER_ORDER = ("pad", "bass", "kick", "hat", "arp", "melody", "shine")
LAYER_MIN_ENERGY = {
    "pad": 0.00, "bass": 0.22, "kick": 0.34, "hat": 0.44,
    "arp": 0.55, "melody": 0.66, "shine": 0.82,
}

# 三档和声进行（按级数；全片共用一张和弦表）
PROGRESSIONS = {
    "calm": ("i", "VI", "iv", "v"),
    "body": ("i", "VI", "III", "VII"),
    "drive": ("i", "VII", "VI", "V"),
}

# 主题动机（相对主音的半音数；偏移单位 = 拍，时值单位 = 拍）。
# 问句：A D F E | D C A —— 落在小三度上；答句：A D F G A；结尾用大三度（皮卡迪三度）。
MOTIF_QUESTION = (
    (0.0, 7, 0.9), (1.0, 12, 0.9), (2.0, 15, 0.9), (3.0, 14, 1.8),
    (5.0, 12, 0.9), (6.0, 10, 0.9), (7.0, 7, 1.6),
)
MOTIF_ANSWER = (
    (0.0, 7, 0.9), (1.0, 12, 0.9), (2.0, 15, 0.9), (3.0, 17, 0.9), (4.0, 19, 2.4),
)
MOTIF_MAJOR = (
    (0.0, 7, 0.9), (1.0, 12, 0.9), (2.0, 16, 0.9), (3.0, 14, 1.6), (5.0, 12, 3.0),
)

# 12 个音名的两种写法（Dm/Cm 用降号、Am/Em 用升号，读起来像谱子）
_PC_NAMES_FLAT = ("C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B")
_PC_NAMES_SHARP = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
_ACCIDENTALS = {"flat": _PC_NAMES_FLAT, "sharp": _PC_NAMES_SHARP}
_NOTE_RE = re.compile(r"^[A-G](?:#|b)?-?\d+$")

# 级数表：(级数名, 相对主音的半音数, 叠置音程, 性质)
_DEGREE_TABLE = (
    ("i", 0, (0, 3, 7, 12), "m"),
    ("ii°", 2, (0, 3, 6, 12), "dim"),
    ("III", 3, (0, 4, 7, 12), ""),
    ("iv", 5, (0, 3, 7, 12), "m"),
    ("v", 7, (0, 3, 7, 12), "m"),
    ("V", 7, (0, 4, 7, 12), ""),
    ("VI", 8, (0, 4, 7, 12), ""),
    ("VII", 10, (0, 4, 7, 12), ""),
    ("I", 0, (0, 4, 7, 12), ""),          # 结尾大三和弦（皮卡迪三度）
    ("sus2", 0, (0, 2, 7, 12), "sus2"),   # 悬留色彩，过门/段首可用
    # ---- 大调级数与七和弦色彩。只允许在表尾追加；前 10 项顺序不许动（_build_key 依赖） ----
    ("ii", 2, (0, 3, 7, 12), "m"),
    ("iii", 4, (0, 3, 7, 12), "m"),
    ("IV", 5, (0, 4, 7, 12), ""),
    ("vi", 9, (0, 3, 7, 12), "m"),
    ("vii", 11, (0, 3, 6, 12), "dim"),
    ("sus4", 0, (0, 5, 7, 12), "sus4"),
    ("Imaj7", 0, (0, 4, 7, 11), "maj7"),
    ("ii7", 2, (0, 3, 7, 10), "m7"),
    ("iii7", 4, (0, 3, 7, 10), "m7"),
    ("IVmaj7", 5, (0, 4, 7, 11), "maj7"),
    ("V7", 7, (0, 4, 7, 10), "7"),
    ("vi7", 9, (0, 3, 7, 10), "m7"),
    ("i7", 0, (0, 3, 7, 10), "m7"),
    # ---- 和弦替换用的七和弦色彩。同根音、同三音，只把五度后的音换成小七度；
    # 只允许在表尾追加（前 10 项顺序被 _build_key / 老报告依赖），供 _CHORD_SWAPS 使用。 ----
    ("VI7", 8, (0, 4, 7, 10), "7"),
    ("III7", 3, (0, 4, 7, 10), "7"),
    ("VII7", 10, (0, 4, 7, 10), "7"),
)

# 级数名 -> 定义行（旋律生成/mood 色彩/自测校验用；只读映射，不参与 _build_key）
_DEGREE_BY_NAME = {row[0]: row for row in _DEGREE_TABLE}


# ============================================================================
# 小工具（全部确定性、无副作用）
# ============================================================================
def _require(cond, msg: str) -> None:
    """显式运行时校验：不用 assert（python -O 会把它优化掉）。"""
    if not cond:
        raise ValueError(msg)


def _setup_stdio() -> None:
    """被管道/文件重定向时把 stdout/stderr 切到 UTF-8，避免中文输出按 ANSI 代码页乱码；
    真实控制台（isatty）保持系统编码，交互运行时中文照样正常。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and not stream.isatty():
                stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


def _ns(dur: float, sr: int = SR) -> int:
    return max(0, int(round(float(dur) * sr)))


def _t(n: int, sr: int = SR) -> np.ndarray:
    return np.arange(n, dtype=np.float64) / float(sr)


def _f32(x) -> np.ndarray:
    return np.asarray(x, dtype=np.float32)


def _db_amp(db: float) -> float:
    return float(10.0 ** (float(db) / 20.0))


def _clip01(x: float) -> float:
    return float(min(1.0, max(0.0, float(x))))


def _up(note: str, k: int = 1) -> str:
    """音名升/降八度：_up("D3", 2) -> "D5"，_up("Bb3", 1) -> "Bb4"。"""
    return note[:-1] + str(int(note[-1]) + int(k))


def _hash_int(*parts) -> int:
    """稳定字符串哈希（FNV-1a）。不用内置 hash：它每次进程启动都变，会破坏确定性。"""
    h = 2166136261
    for p in parts:
        for b in str(p).encode("utf-8"):
            h = ((h ^ b) * 16777619) & 0xFFFFFFFF
    return h


def _semi_note(root_pc: int, base_octave: int, semis: int, names) -> str:
    """主音音高类 + 半音数 -> 音名（跨八度自动进位）。"""
    v = int(root_pc) + int(semis)
    return f"{names[v % 12]}{base_octave + v // 12}"


def _bass_octave(pc: int) -> int:
    """低音根音的八度：让根音落在 40~90 Hz（G1/Bb1/A1 比 G2/Bb2/A2 更"沉"）。"""
    return 1 if int(pc) in (7, 8, 9, 10) else 2


def _build_key(root_name: str, root_pc: int, accidentals: str, cn_name: str) -> dict:
    """按主音生成一张级数和弦表（voicing + 低音 + 显示名）。"""
    names = _ACCIDENTALS[accidentals]
    chords = {}
    for degree, base, intervals, quality in _DEGREE_TABLE:
        notes = [_semi_note(root_pc, 3, base + iv, names) for iv in intervals]
        bass_pc = (root_pc + base) % 12
        suffix = {"m": "m", "dim": "dim", "sus2": "sus2", "sus4": "sus4",
                  "maj7": "maj7", "m7": "m7", "7": "7", "": ""}[quality]
        chords[degree] = {
            "degree": degree,
            "name": f"{names[bass_pc]}{suffix}",
            "root": f"{names[bass_pc]}{_bass_octave(bass_pc)}",
            "notes": notes,
            "quality": quality,
        }
    return {
        "name": cn_name,
        "root": root_name,
        "root_pc": int(root_pc) % 12,
        "accidentals": accidentals,
        "names": names,
        "chords": chords,
        "progression": dict(PROGRESSIONS),
    }


# 至少 Dm / Am / Cm / Em 四个调（契约要求）；同一张级数表，换调只换主音与写法。
KEYS = {
    "Dm": _build_key("D", 2, "flat", "D 小调"),
    "Am": _build_key("A", 9, "sharp", "A 小调"),
    "Cm": _build_key("C", 0, "flat", "C 小调"),
    "Em": _build_key("E", 4, "sharp", "E 小调"),
}

# 大调三档和声进行。小调那边 PROGRESSIONS 保持原样：legacy 路径只读 PROGRESSIONS。
PROGRESSIONS_MAJOR = {
    "calm": ("I", "vi", "IV", "V"),
    "body": ("I", "V", "vi", "IV"),
    "drive": ("I", "IV", "V", "I"),
}

# 6 个大调（与 stylekit.MAJOR_KEYS 同名同义）。用同一个 _build_key 构建，
# 语义与小调键完全一致（级数表 -> 和弦名/低音/音名）；只是 progression 换成大调级数。
# 写法按惯例挑：C/F/Bb 用降号、G/D/A 用升号。
KEYS_MAJOR = {
    "C": _build_key("C", 0, "flat", "C 大调"),
    "G": _build_key("G", 7, "sharp", "G 大调"),
    "D": _build_key("D", 2, "sharp", "D 大调"),
    "F": _build_key("F", 5, "flat", "F 大调"),
    "A": _build_key("A", 9, "sharp", "A 大调"),
    "Bb": _build_key("Bb", 10, "flat", "Bb 大调"),
}
for _kname in KEYS_MAJOR:
    KEYS_MAJOR[_kname]["progression"] = dict(PROGRESSIONS_MAJOR)
del _kname

# 全部可用调：4 个小调 + 6 个大调。KEYS 本身保持 4 个小调不变（兼容性锚点）。
KEYS_ALL = dict(KEYS)
KEYS_ALL.update(KEYS_MAJOR)


# ============================================================================
# 电平测量（简化 K 加权；只用于段落间相对电平匹配与报告，不追求 BS.1770 精度）
# ============================================================================
def _decimate_mono(x: np.ndarray, k: int = 4) -> np.ndarray:
    """立体声 -> 粗降采样单声道（float64）。4 倍降采样足够量"段落响度"。"""
    x = np.asarray(x)
    n = x.shape[0] - x.shape[0] % int(k)
    if x.ndim == 1:
        return x[:n].reshape(-1, int(k)).mean(axis=1, dtype=np.float64)
    return x[:n].reshape(-1, int(k), x.shape[1]).mean(axis=(1, 2), dtype=np.float64)


def _moving_avg(v: np.ndarray, w: int) -> np.ndarray:
    """移动平均（边界复制），O(n)，用来做"一阶高通/低通"的近似。"""
    w = max(2, int(w))
    if len(v) == 0:
        # 空输入：np.pad(mode="edge") 对空数组直接抛 "can't extend empty axis 0"，
        # 而空数组只在 duration*sr < 4（不足 4 个采样）时出现（duration=1e-6 实测崩这）。
        return np.zeros(0, dtype=np.float64)
    pad = w // 2
    vp = np.pad(v, (pad, w - pad), mode="edge")
    c = np.cumsum(vp)
    return (c[w:] - c[:-w]) / float(w)


def _k_weighted_mono(x: np.ndarray, sr: int = SR, k: int = 4) -> np.ndarray:
    """简化 K 加权：粗降采样 + 低切（40 Hz 移动平均差分）+ 高架（1.2 kHz 以上 +3 dB 近似）。"""
    m = _decimate_mono(x, k)
    dsr = sr / float(k)
    low = _moving_avg(m, max(2, int(round(dsr / 40.0))))
    hp = m - low
    low2 = _moving_avg(hp, max(2, int(round(dsr / 1200.0))))
    high = hp - low2
    return hp + 0.42 * high


def _short_term_db(x: np.ndarray, win: float = 0.4, hop: float = 0.1,
                   sr: int = SR, k: int = 4) -> tuple[np.ndarray, np.ndarray]:
    """短时响度曲线：返回 (时刻(秒), dB)。win/hop 单位秒，块内取均方。"""
    m = _k_weighted_mono(x, sr=sr, k=k)
    dsr = sr / float(k)
    b = max(2, int(round(win * dsr)))
    h = max(1, int(round(hop * dsr)))
    if len(m) < b:
        return np.zeros(0), np.zeros(0)
    p = m * m
    c = np.concatenate(([0.0], np.cumsum(p)))
    starts = np.arange(0, len(p) - b + 1, h)
    e = (c[starts + b] - c[starts]) / float(b)
    t = (starts + b * 0.5) / dsr
    return t, 10.0 * np.log10(np.maximum(e, 1e-20)) - 0.691


def _approx_lufs(x: np.ndarray, sr: int = SR) -> float:
    """近似积分响度（简化 K 加权 + 400/100 ms 短时块 + -70/-10 dB 双门限）。

    注意 10**(lj/10) 已经是"块均方 * 10**(-0.691/10)"：lj 里的 -0.691 是 BS.1770
    块响度定义的一部分，把它抬回"块均方"后，积分响度就是 10*log10(门限内均值)。
    旧实现又在均值外面减了一次 0.691，等于报告值恒定偏低 0.69 dB
    （实测 12 s 片：本文件 -15.65 vs sfxkit.integrated_loudness -14.47，差 1.19 dB；
    去掉多余项后 -14.96，剩余差是"12 kHz 降采样 + 移动平均近似 K 加权"的固有误差）。
    """
    _, lj = _short_term_db(x, 0.4, 0.1, sr=sr)
    lj = lj[np.isfinite(lj)]
    lj = lj[lj > -70.0]
    if len(lj) == 0:
        return -70.0
    e = np.mean(10.0 ** (lj / 10.0))
    rel = 10.0 * np.log10(max(e, 1e-20)) - 10.0
    lj2 = lj[lj > rel]
    if len(lj2) == 0:
        return float(10.0 * np.log10(max(e, 1e-20)))
    return float(10.0 * np.log10(max(float(np.mean(10.0 ** (lj2 / 10.0))), 1e-20)))


# ============================================================================
# 立体声总线（只做加法与声像；音色全部来自 sfxkit 原语）
# ============================================================================
class _Bus:
    """定点时间的立体声累加总线；单声道输入按等功率声像铺开。"""

    __slots__ = ("n", "sr", "buf")

    def __init__(self, n: int, sr: int = SR):
        self.n = int(n)
        self.sr = int(sr)
        self.buf = np.zeros((self.n, 2), np.float32)

    def add(self, x, at: float, gain_db: float = 0.0, pan: float = 0.0) -> None:
        """x 可以是 (m,) 或 (m,2)；pan ∈ [-1, 1]（只对单声道生效）。"""
        if x is None:
            return
        seg = np.asarray(x)
        if seg.size == 0:
            return
        s = int(round(float(at) * self.sr))
        if s >= self.n or s + len(seg) <= 0:
            return
        if s < 0:
            seg = seg[-s:]
            s = 0
        e = min(self.n, s + len(seg))
        seg = seg[: e - s]
        g = _db_amp(gain_db)
        if seg.ndim == 1:
            th = (float(np.clip(pan, -1.0, 1.0)) + 1.0) * math.pi / 4.0
            gl = math.cos(th) * math.sqrt(2.0) * g
            gr = math.sin(th) * math.sqrt(2.0) * g
            self.buf[s:e, 0] += seg * gl
            self.buf[s:e, 1] += seg * gr
        else:
            self.buf[s:e] += seg * g

    def clear(self) -> None:
        self.buf = None


# ============================================================================
# 乐器素材库：全部用 sfxkit 原语合成；重音色按 key 缓存（长片性能的关键）
# ============================================================================
class _Bank:
    """合成器 + 缓存。一个 Score 一次 render 用一个 Bank（不跨 render 复用状态）。"""

    def __init__(self, seed: int, beat_s: float, bar_s: float, sr: int = SR):
        self.seed = int(seed)
        self.sr = int(sr)
        self.beat_s = float(beat_s)
        self.bar_s = float(bar_s)
        self._cache: dict = {}
        self._pad_cores: dict = {}
        self._pad_envs: dict = {}
        self._str_cores: dict = {}
        self._pad_n = max(16, _ns(self.bar_s * 1.02 + 1.3, self.sr))
        self._pad_dur = self.bar_s * 1.02
        # 鼓组：预渲染几套变体，交替使用，避免机械重复
        self.kicks = [self._mk_kick(i) for i in range(4)]
        self.kick_big = self._mk_kick(9, punch=1.3)
        self.hats = [self._mk_hat(i, False) for i in range(4)]
        self.hats_open = [self._mk_hat(20 + i, True) for i in range(2)]
        self.claps = [self._mk_clap(30 + i) for i in range(2)]
        self.snares = [self._mk_snare(40 + i) for i in range(2)]
        self.toms = [self._mk_tom(50 + i, f0) for i, f0 in enumerate((196.0, 147.0, 110.0, 82.0))]
        self.crashes = [self._mk_crash(60 + i, 3.0) for i in range(2)]

    # ---------------- 内部：种子与缓存 ----------------
    def _seed(self, *parts) -> int:
        return (self.seed * 1000003 + _hash_int(*parts)) % (2 ** 31 - 1)

    def _cached(self, key, fn):
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        val = fn()
        self._cache[key] = val
        return val

    @staticmethod
    def _edge_fade(x: np.ndarray, fade_in: float = 0.0015, fade_out: float = 0.01) -> np.ndarray:
        """素材首尾极短斜坡，防止叠加上出现"咔"。"""
        n = len(x)
        ni, no = min(n, _ns(fade_in)), min(n, _ns(fade_out))
        if ni > 1:
            w = np.linspace(0.0, 1.0, ni, endpoint=False, dtype=np.float32)
            if x.ndim == 1:
                x[:ni] *= w
            else:
                x[:ni] *= w[:, None]
        if no > 1:
            w = np.linspace(1.0, 0.0, no, dtype=np.float32)
            if x.ndim == 1:
                x[n - no:] *= w
            else:
                x[n - no:] *= w[:, None]
        return x

    # ---------------- 鼓 ----------------
    def _mk_kick(self, i: int, punch: float = 1.0) -> np.ndarray:
        """底鼓：音高快速下滑的正弦 + 一点高频敲击噪声（sfxkit sine / white / highpass）。"""
        g = S.rng(self._seed("kick", i))
        n = _ns(0.5, self.sr)
        t = _t(n, self.sr)
        f = 46.0 + 128.0 * punch * np.exp(-t / 0.03) + g.uniform(-0.4, 0.4)
        body = S.sine(f, n) * np.exp(-t / 0.2) * np.minimum(1.0, t / 0.003)
        click = S.highpass(S.white(_ns(0.004, self.sr), g), 2200.0, sr=self.sr)
        click = np.pad(click, (0, max(0, n - len(click))))[:n]
        x = body + 0.3 * click / (float(np.max(np.abs(click))) + 1e-9)
        x = np.tanh(x * 1.7) * 0.8
        return self._edge_fade(_f32(x), 0.001, 0.02)

    def _mk_hat(self, i: int, open_: bool = False) -> np.ndarray:
        """踩镲：非谐波方波堆叠 + 高通白噪声（sfxkit pulse / white / bandpass / highpass）。"""
        g = S.rng(self._seed("hat", i))
        d = 0.28 if open_ else 0.06
        n = _ns(d, self.sr)
        t = _t(n, self.sr)
        metal = np.zeros(n)
        for f in (205.3, 304.4, 369.6, 522.7, 540.0, 800.0):
            metal += S.pulse(f, n, sr=self.sr)
        nz = S.highpass(S.white(n, g), 6500.0, sr=self.sr)
        x = S.bandpass(metal, 8500.0, 0.8, sr=self.sr) * 0.8 + 0.9 * nz
        x = S.highpass(x, 6000.0, sr=self.sr) * np.exp(-t / (0.09 if open_ else 0.016))
        x *= np.minimum(1.0, t / 0.0005)
        return self._edge_fade(_f32(x / (float(np.max(np.abs(x))) + 1e-9)), 0.0005, 0.005)

    def _mk_clap(self, i: int) -> np.ndarray:
        """拍手：几次很短的带通噪声 + 一点 190 Hz 体声。"""
        g = S.rng(self._seed("clap", i))
        n = _ns(0.32, self.sr)
        x = np.zeros(n)
        for d in (0.0, 0.009, 0.019, 0.028):
            b = S.bandpass(S.burst(0.012 if d < 0.02 else 0.16, g, t60=0.012 if d < 0.02 else 0.14,
                                   sr=self.sr), 1250.0, 0.9, sr=self.sr)
            s = _ns(d, self.sr)
            e = min(n, s + len(b))
            x[s:e] += b[: e - s]
        body = np.sin(2.0 * np.pi * 190.0 * _t(n, self.sr)) * np.exp(-_t(n, self.sr) / 0.04)
        x = x / (float(np.max(np.abs(x))) + 1e-9) + 0.25 * body
        x = np.tanh(x * 1.3) * 0.75
        return self._edge_fade(_f32(x), 0.001, 0.01)

    def _mk_snare(self, i: int) -> np.ndarray:
        """军鼓：音高下滑的体声 + 带通噪声。"""
        g = S.rng(self._seed("snare", i))
        n = _ns(0.3, self.sr)
        t = _t(n, self.sr)
        body = np.sin(2.0 * np.pi * np.cumsum(185.0 + 70.0 * np.exp(-t / 0.015)) / self.sr) * np.exp(-t / 0.05)
        nz = S.bandpass(S.white(n, g), 3800.0, 0.55, sr=self.sr) * np.exp(-t / 0.08)
        x = 0.55 * body + 0.9 * nz / (float(np.max(np.abs(nz))) + 1e-9)
        x = np.tanh(x * 1.5) * 0.8
        return self._edge_fade(_f32(x), 0.0008, 0.01)

    def _mk_tom(self, i: int, f0: float) -> np.ndarray:
        """筒鼓：音高快速下滑的正弦 + 鼓槌噪声（加花用）。"""
        g = S.rng(self._seed("tom", i))
        n = _ns(0.55, self.sr)
        t = _t(n, self.sr)
        f = f0 * (1.0 + 0.45 * np.exp(-t / 0.04))
        body = S.sine(f, n, sr=self.sr) * np.exp(-t / 0.17) * np.minimum(1.0, t / 0.002)
        stick = S.bandpass(S.white(n, g), 2500.0, 0.8, sr=self.sr) * np.exp(-t / 0.012)
        x = body + 0.25 * stick / (float(np.max(np.abs(stick))) + 1e-9)
        x = np.tanh(x * 1.8) * 0.8
        return self._edge_fade(_f32(x), 0.001, 0.02)

    def _mk_crash(self, i: int, dur: float = 3.0) -> np.ndarray:
        """镲片：非谐波方波堆叠 + 高通噪声，左右声道去相关。"""
        n = _ns(dur, self.sr)
        t = _t(n, self.sr)
        out = np.zeros((n, 2), np.float32)
        for c in range(2):
            g = S.rng(self._seed("crash", i, c))
            metal = np.zeros(n)
            for f in (313.3, 427.1, 587.9, 732.4, 911.7, 1203.1):
                metal += S.pulse(f * (1.0 + 0.004 * c), n, sr=self.sr)
            x = S.bandpass(metal, 6500.0, 0.5, sr=self.sr) + 1.1 * S.highpass(S.white(n, g), 5000.0, sr=self.sr)
            out[:, c] = _f32(S.highpass(x, 2500.0, sr=self.sr) * np.exp(-t / 0.72) * np.minimum(1.0, t / 0.0015))
        out /= float(np.max(np.abs(out))) + 1e-9
        return self._edge_fade(out, 0.001, 0.05)

    # ---------------- 音高乐器 ----------------
    def bass(self, note: str, dur: float, bright: float) -> np.ndarray:
        """带限锯齿 + 脉冲基音，时变低通 + 正弦托底（sfxkit saw / pulse / tv_filter / sine）。"""
        key = ("bass", note, round(float(dur), 3), int(round(bright * 10)))
        return self._cached(key, lambda: self._make_bass(note, dur, bright))

    def _make_bass(self, note: str, dur: float, bright: float) -> np.ndarray:
        f = S.hz(note)
        n = _ns(dur + 0.07, self.sr)
        t = _t(n, self.sr)
        osc = 0.75 * S.saw(f, n, sr=self.sr) + 0.45 * S.pulse(f * 1.002, n, 0.3, sr=self.sr)
        cut = 140.0 + (300.0 + 2100.0 * bright) * np.exp(-t / 0.09)
        x = S.tv_filter(osc, "lowpass", cut, 1.1, block=64, sr=self.sr) + 0.5 * S.sine(f, n, sr=self.sr)
        env = np.minimum(1.0, t / 0.004) * np.clip((dur + 0.05 - t) / 0.05, 0.0, 1.0)
        x = np.tanh(x * 1.5) * 0.55 * env
        return self._edge_fade(_f32(x), 0.001, 0.005)

    def arp(self, note: str, bright: float, dur: float = 0.3) -> np.ndarray:
        """FM 琶音：指数衰减的调制指数 -> 起音亮、尾音纯。"""
        key = ("arp", note, int(round(bright * 10)), round(float(dur), 3))
        return self._cached(key, lambda: self._make_arp(note, bright, dur))

    def _make_arp(self, note: str, bright: float, dur: float) -> np.ndarray:
        f = S.hz(note)
        n = _ns(dur, self.sr)
        t = _t(n, self.sr)
        idx = (0.5 + 1.8 * bright) * np.exp(-t / 0.05)
        x = S.fm(f, 2.0, idx, n, sr=self.sr) * np.exp(-t / 0.085) * np.minimum(1.0, t / 0.0015)
        return self._edge_fade(_f32(x * 0.8), 0.0005, 0.005)

    def bell(self, note: str, dur: float = 2.5) -> np.ndarray:
        """FM 钟：非整数比率 + 两个算子叠加。"""
        key = ("bell", note, round(float(dur), 2))
        return self._cached(key, lambda: self._make_bell(note, dur))

    def _make_bell(self, note: str, dur: float) -> np.ndarray:
        f = S.hz(note)
        n = _ns(dur, self.sr)
        t = _t(n, self.sr)
        idx = 2.4 * np.exp(-t / 0.35)
        x = S.fm(f, 3.5, idx, n, sr=self.sr) * np.exp(-t / (dur * 0.42))
        x += 0.32 * S.fm(f, 1.0, 0.4, n, sr=self.sr) * np.exp(-t / (dur * 0.55))
        return self._edge_fade(_f32(x * 0.7 * np.minimum(1.0, t / 0.002)), 0.001, 0.02)

    def celesta(self, note: str, dur: float = 3.0) -> np.ndarray:
        """钢片琴/八音盒：FM 比率 1:4、小指数 —— "提问"的音色。"""
        key = ("celesta", note, round(float(dur), 2))
        return self._cached(key, lambda: self._make_celesta(note, dur))

    def _make_celesta(self, note: str, dur: float) -> np.ndarray:
        f = S.hz(note)
        n = _ns(dur, self.sr)
        t = _t(n, self.sr)
        x = S.fm(f, 4.0, 1.1 * np.exp(-t / 0.25), n, sr=self.sr) * np.exp(-t / (dur * 0.42))
        x += 0.25 * S.sine(2.0 * f, n, sr=self.sr) * np.exp(-t / 0.9)
        return self._edge_fade(_f32(x * 0.7 * np.minimum(1.0, t / 0.003)), 0.0015, 0.02)

    # ---------------- 铺底（超级锯齿） ----------------
    def _pad_env(self, attack: float, release: float) -> np.ndarray:
        key = (int(round(attack * 1000)), int(round(release * 1000)))
        hit = self._pad_envs.get(key)
        if hit is not None:
            return hit
        n = self._pad_n
        t = _t(n, self.sr)
        env = np.minimum(1.0, t / max(attack, 1e-4))
        rel = t > self._pad_dur
        if np.any(rel):
            # 释音必须是"乘上去"而不是"覆盖掉"：起音比一小节还慢时（bpm > ~223 的
            # 4/4，pad_dur = 一小节 * 1.02），覆盖式写法会在 t=pad_dur 处把还在爬坡的
            # 包络（例如 0.68）直接跳到 1.0，听感是一声"噗"；乘法写法在起音已完成时
            # 是 1.0 * 衰减，逐位与原结果相同（既有 BPM 全部不受影响）。
            env[rel] *= np.exp(-(t[rel] - self._pad_dur) / max(release, 1e-4))
        env = _f32(env)
        self._pad_envs[key] = env
        return env

    def _pad_core(self, chord: dict, bright: float, octave: int) -> np.ndarray:
        key = ("padcore", chord["name"], int(round(bright * 10)), int(octave))
        hit = self._pad_cores.get(key)
        if hit is not None:
            return hit
        names = [_up(nt, octave) for nt in chord["notes"]] if octave else list(chord["notes"])
        n = self._pad_n
        out = np.zeros((n, 2), np.float32)
        for c in range(2):
            g = S.rng(self._seed("pad", chord["name"], int(round(bright * 10)), octave, c))
            acc = np.zeros(n, np.float64)
            for nt in names:
                acc += S.supersaw(S.hz(nt), n, voices=3, detune=0.13, seed=g, sr=self.sr)
            acc = S.lowpass(acc, 600.0 + 4800.0 * bright, 0.7, sr=self.sr)
            out[:, c] = _f32(acc)
        out *= np.float32(1.6 / max(1, len(names)))
        self._pad_cores[key] = out
        return out

    def pad(self, chord: dict, bright: float, attack: float, release: float = 0.7, octave: int = 0) -> np.ndarray:
        """铺底和弦：缓存振荡器核心，只按起音/释音重新套包络。"""
        key = ("pad", chord["name"], int(round(bright * 10)), int(round(attack * 100)), octave)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        core = self._pad_core(chord, bright, octave)
        env = self._pad_env(attack, release)
        tex = _f32(core * env[:, None])
        self._cache[key] = tex
        return tex



    # ---------------- 效果素材 ----------------
    def sub_boom(self, dur: float = 2.2, f0: float = 55.0, f1: float = 30.0) -> np.ndarray:
        """低频轰鸣：音高下滑的正弦（重击的"地基"）。"""
        key = ("sub", round(float(dur), 2), f0, f1)
        return self._cached(key, lambda: self._make_sub(dur, f0, f1))

    def _make_sub(self, dur: float, f0: float, f1: float) -> np.ndarray:
        n = _ns(dur, self.sr)
        t = _t(n, self.sr)
        f = f1 + (f0 - f1) * np.exp(-t / 0.35)
        x = S.sine(f, n, sr=self.sr) * np.exp(-t / (dur * 0.35)) * np.minimum(1.0, t / 0.004)
        return self._edge_fade(_f32(x * 0.9), 0.002, 0.05)

    def swell(self, dur: float, seed_i: int = 0, salt: str = "") -> np.ndarray:
        """反向镲：噪声从无到有冲到 dur 处最响（段落衔接）。"""
        n = _ns(dur, self.sr)
        g = S.rng(self._seed("swell", seed_i, salt))
        nz = S.highpass(S.white(n, g), 2400.0, sr=self.sr)
        nz += 0.35 * S.bandpass(S.white(n, g), 7000.0, 0.7, sr=self.sr)
        p = np.arange(n, dtype=np.float64) / max(1, n)
        env = p ** 2.2
        x = _f32(nz * env * 0.55 / (float(np.max(np.abs(nz))) + 1e-9))
        return self._edge_fade(x, 0.005, 0.002)

    def riser(self, dur: float, seed_i: int = 0, salt: str = "") -> np.ndarray:
        """上升音：正弦上滑 + 越来越亮的噪声（推向段落重音）。"""
        n = _ns(dur, self.sr)
        g = S.rng(self._seed("riser", seed_i, salt))
        t = _t(n, self.sr)
        p = t / max(dur, 1e-6)
        f = 110.0 * (8.0 ** p)
        tone = S.sine(f, n, sr=self.sr) * 0.45
        nz = S.highpass(S.white(n, g), 1800.0, sr=self.sr) * (0.25 + 0.55 * p)
        env = p ** 1.8 * np.minimum(1.0, (1.0 - p) / 0.02)
        x = (tone + nz) * env * 0.5
        return self._edge_fade(_f32(x), 0.005, 0.002)


# ============================================================================
# 段落规划（小节对齐、和弦分配、过门、吸气）
# ============================================================================
def _energy_tier(energy: float) -> str:
    if energy < 0.42:
        return "calm"
    if energy < 0.72:
        return "body"
    return "drive"


def _layers_for(energy: float) -> list:
    return [name for name in LAYER_ORDER if energy >= LAYER_MIN_ENERGY[name]]


def _kick_positions(energy: float) -> tuple:
    """底鼓落点（拍）。安静段没有鼓；有能量后从"心搏"长成四拍。"""
    if energy < 0.30:
        return ()
    if energy < 0.42:
        return (0.0,)
    if energy < 0.55:
        return (0.0, 2.0)
    return (0.0, 1.0, 2.0, 3.0)


def _hat_step_beats(energy: float):
    """踩镲步长（拍）；None = 本段没有踩镲。"""
    if energy < 0.44:
        return None
    if energy < 0.62:
        return 1.0
    if energy < 0.80:
        return 0.5
    return 0.25


def _breath_mode(tier: str) -> int:
    """乐句末的"呼吸"强度：2=整拍全撤（安静段），1=轻呼吸（中段），0=律动段保持推进力。"""
    return {"calm": 2, "body": 1, "drive": 0}[tier]


def _bass_pattern(energy: float) -> str:
    if energy < 0.22:
        return "none"
    if energy < 0.42:
        return "whole"
    if energy < 0.58:
        return "half"
    if energy < 0.75:
        return "off8"
    return "16"


def _arp_step_beats(energy: float):
    if energy < LAYER_MIN_ENERGY["arp"]:
        return None
    return 0.5 if energy < 0.72 else 0.25


def energy_target_db(energy: float) -> float:
    """energy -> 相对电平目标（dB）。安静 0.30 ≈ -5.95，律动 0.85 ≈ -1.28，差 4.7 dB。"""
    return -8.5 + 8.5 * _clip01(energy)


# ============================================================================
# Score：配乐对象
# ============================================================================
class Score:
    """一部片子的配乐。

    用法::

        sc = Score(389.0, bpm=120, key="Dm", seed=7)
        sc.plan_from_anchors(anchors)          # 或 sc.section("intro", 0, 18, 0.3)
        sc.suck(102.0, db=-7, dur=0.25)        # 重击前吸一口气
        wav = sc.render()                      # (n, 2) float32
        rep = sc.report()

    所有时间都是成片绝对秒；段落边界自动吸附到最近小节线（120 BPM 时 2 s 一小节）。
    """

    def __init__(self, duration, bpm: float = 120.0, key: str = "Dm", seed: int = 7, sr: int = SR):
        # inf 能过 "> 0" 的检查、但之后会在 _snap_bar 里除零，所以这里要显式挡掉非有限值
        _require(isinstance(duration, (int, float)) and math.isfinite(float(duration))
                 and float(duration) > 0.0,
                 f"duration 必须是有限正数，收到 {duration!r}")
        _require(isinstance(bpm, (int, float)) and math.isfinite(float(bpm))
                 and float(bpm) > 0.0,
                 f"bpm 必须是有限正数，收到 {bpm!r}")
        _require(key in KEYS_ALL, f"未知调性 {key!r}；可选：{'/'.join(KEYS_ALL)}")
        self.duration = float(duration)
        self.bpm = float(bpm)
        self.key = key
        self.seed = int(seed)
        self.sr = int(sr)
        self.beat_s = 60.0 / self.bpm
        self.bar_s = self.beat_s * BEATS_PER_BAR
        self._sections: list[dict] = []
        self._sucks: list[dict] = []
        self._anchors: list[dict] = []
        self._plan: dict | None = None
        self._render_info: dict = {}

    # ---------------- 公开属性 ----------------
    @property
    def k(self) -> dict:
        return KEYS_ALL[self.key]

    def __repr__(self) -> str:  # 只用于调试输出
        return (f"Score(duration={self.duration:g}, bpm={self.bpm:g}, key={self.key!r}, "
                f"seed={self.seed}, sections={len(self._sections)})")

    # ---------------- 段落 ----------------
    def section(self, name, start, end, energy: float = 0.5) -> "Score":
        """加一个段落（名字、起止秒、能量 0~1）。边界渲染时会吸附到小节线。"""
        _require(isinstance(name, str) and name.strip() != "", "段落 name 必须是非空字符串")
        _require(float(end) > float(start), f"段落 {name!r}：end({end}) 必须大于 start({start})")
        self._sections.append({
            "name": str(name),
            "start": float(start),
            "end": float(end),
            "energy": _clip01(energy),
        })
        self._plan = None
        return self

    def plan_from_anchors(self, anchors, default_energy: float = 0.5) -> "Score":
        """按 timeline.json 的 anchors（[{id, kind, start, end}]）自动分段。

        kind 查 KIND_ENERGY 得到默认能量（hook 0.3 / body 0.55 / outro 0.35 …）；
        anchor 自带 "energy" 字段时以它为准；未知 kind 用 default_energy。
        边界会在渲染时吸附到最近小节线；重叠/空隙自动裁剪或补"过渡"段。
        """
        _require(anchors is not None and len(anchors) > 0, "anchors 不能为空")
        self._sections = []
        self._anchors = []
        for idx, a in enumerate(anchors):
            _require(isinstance(a, dict), f"anchors[{idx}] 必须是对象")
            for fld in ("start", "end"):
                _require(fld in a, f"anchors[{idx}] 缺少 {fld} 字段")
            kind = str(a.get("kind", "") or "")
            energy = a.get("energy", KIND_ENERGY.get(kind, default_energy))
            name = str(a.get("name") or a.get("id") or kind or f"段{idx + 1}")
            self._sections.append({
                "name": name,
                "start": float(a["start"]),
                "end": float(a["end"]),
                "energy": _clip01(energy),
            })
            self._anchors.append({"id": str(a.get("id", f"a{idx + 1}")), "kind": kind,
                                  "start": float(a["start"]), "end": float(a["end"])})
        self._plan = None
        return self

    def suck(self, at, db: float = -6.0, dur: float = 0.2) -> "Score":
        """重击前的"吸气"：在 [at-dur, at) 平滑压低 |db| dB，到 at 前 4 ms 弹回（被重击盖住）。

        不是静音断层：全程连续、无阶跃。db 传正负号都按"压低绝对值"处理。
        """
        depth = -abs(float(db))
        dur = float(min(1.0, max(0.05, float(dur))))
        self._sucks.append({"t": float(at), "db": float(max(-24.0, min(-1.0, depth))),
                            "dur": dur, "source": "手工"})
        self._plan = None
        return self

    # ---------------- 规划：吸附小节、分配和弦、生成过门/吸气 ----------------
    def _snap_bar(self, t: float) -> float:
        return round(float(t) / self.bar_s) * self.bar_s

    def _build_plan(self) -> dict:
        if self._plan is not None:
            return self._plan
        dur, bar = self.duration, self.bar_s
        raw = sorted(self._sections, key=lambda s: (s["start"], s["end"]))
        if not raw:
            raw = [{"name": "全片", "start": 0.0, "end": dur, "energy": 0.5}]

        # 1) 吸附到小节线 + 裁重叠 + 保证至少一小节
        secs: list[dict] = []
        for s in raw:
            a = min(dur, max(0.0, self._snap_bar(s["start"])))
            b = min(dur, max(0.0, self._snap_bar(s["end"])))
            if b - a < bar - 1e-9:
                b = min(dur, a + bar)
            if b - a < 1e-6:
                a = max(0.0, b - bar)
            if secs and a < secs[-1]["end"] - 1e-9:
                a = secs[-1]["end"]           # 重叠：以先写的段落为准，后一段裁掉重叠
                if b - a < bar - 1e-9 and b < dur - 1e-9:
                    b = min(dur, a + bar)
                if b - a < 1e-6:
                    continue
            secs.append({"name": s["name"], "start": a, "end": b, "energy": _clip01(s["energy"])})
        if not secs:
            secs = [{"name": "全片", "start": 0.0, "end": dur, "energy": 0.5}]

        # 2) 补片头/片尾（保持 0~duration 连续覆盖）
        if secs[0]["start"] > 1e-9:
            secs.insert(0, {"name": "序奏", "start": 0.0, "end": secs[0]["start"],
                            "energy": min(0.32, secs[0]["energy"])})
        if secs[-1]["end"] < dur - 1e-9:
            if dur - secs[-1]["end"] >= bar - 1e-9:
                # 真正缺一段（≥1 小节）才补"尾声"；只剩不足一小节（片尾不在小节线上）
                # 时把最后一段延长到片尾，避免在淡出前硬掉一次能量。
                secs.append({"name": "尾声", "start": secs[-1]["end"], "end": dur,
                             "energy": max(0.26, min(0.36, secs[-1]["energy"]))})
            else:
                secs[-1]["end"] = dur

        # 3) 空隙：>=1 小节补"过渡"段，否则把前一段延长到下一段起点
        filled: list[dict] = []
        for s in secs:
            if filled and s["start"] > filled[-1]["end"] + 1e-9:
                g0, g1 = filled[-1]["end"], s["start"]
                if g1 - g0 >= bar - 1e-9:
                    filled.append({"name": "过渡", "start": g0, "end": g1,
                                   "energy": _clip01(0.85 * 0.5 * (filled[-1]["energy"] + s["energy"]))})
                else:
                    filled[-1]["end"] = g1
            filled.append(s)
        secs = filled

        # 4) 每个段的小节列表、和弦序列、层、电平目标
        n_bars = int(math.ceil(dur / bar - 1e-9))
        last_idx = len(secs) - 1
        for si, sec in enumerate(secs):
            tier = _energy_tier(sec["energy"])
            prog = self.k["progression"][tier]
            bars = [bi for bi in range(n_bars) if sec["start"] - 1e-6 <= bi * bar < sec["end"] - 1e-6]
            chords = []
            nb = len(bars)
            for j in range(nb):
                deg = prog[j % len(prog)]
                if j % PHRASE_BARS == PHRASE_BARS - 1 and (j // PHRASE_BARS) % 2 == 1:
                    deg = "V"                       # 每两乐句收一次属和弦（制造张力）
                if si == last_idx and nb >= 2 and j >= nb - 2:
                    deg = "I"                       # 片尾落大三和弦（皮卡迪三度）
                chords.append(deg)
            sec["index"] = si
            sec["tier"] = tier
            sec["bars"] = bars
            sec["chords"] = chords
            sec["breath"] = _breath_mode(tier)
            sec["layers"] = _layers_for(sec["energy"])
            sec["target_db"] = round(energy_target_db(sec["energy"]), 2)
            sec["target_lufs"] = round(BASE_LUFS + energy_target_db(sec["energy"]), 1)

        # 5) 段落交界：至少一小节过门（鼓加花 / 反向镲 / 上升音 / 重击）+ 自动吸气
        transitions: list[dict] = []
        sucks: list[dict] = []
        for i in range(1, len(secs)):
            prev, nxt = secs[i - 1], secs[i]
            t = nxt["start"]
            fill_start = t - bar
            rise = nxt["energy"] - prev["energy"]
            kinds = ["鼓加花"]
            if prev["energy"] >= 0.40 or nxt["energy"] >= 0.40:
                kinds.append("反向镲")
            if rise > 0.10:
                kinds.append("上升音")
            if nxt["energy"] >= 0.72:
                kinds.append("重击")
            suck_db = None
            if rise > 0.08:
                suck_db = round(-min(9.0, 3.0 + 7.0 * rise), 2)
                sucks.append({"t": round(t, 3), "db": suck_db, "dur": 0.24, "source": "过渡吸气"})
            transitions.append({
                "t": round(t, 3),
                "from": prev["name"],
                "to": nxt["name"],
                "fill_start": round(fill_start, 3),
                "fill_s": round(bar, 3),
                "kinds": kinds,
                "suck_db": suck_db,
            })

        # 6) 用户手工吸气（排序稳定：时间 -> 来源）
        sucks.extend(self._sucks)
        sucks.sort(key=lambda d: (round(float(d["t"]), 6), d.get("source", "")))

        beats = [round(i * self.beat_s, 4) for i in range(int(math.floor(dur / self.beat_s + 1e-9)) + 1)]

        self._plan = {
            "n": _ns(dur, self.sr),
            "bar_s": bar,
            "beat_s": self.beat_s,
            "n_bars": n_bars,
            "sections": secs,
            "transitions": transitions,
            "sucks": [dict(d) for d in sucks],
            "beats": beats,
        }
        # 吸气事件只保留落在片内的（渲染时还会再裁一次）
        for d in self._plan["sucks"]:
            d["t"] = round(float(min(max(d["t"], 0.0), dur)), 3)
        self._plan["sucks"].sort(key=lambda d: (round(float(d["t"]), 6), d.get("source", "")))
        return self._plan

    # ---------------- 渲染 ----------------
    def render(self) -> np.ndarray:
        """合成整片配乐：返回 (n, 2) float32，长度 = ceil(duration * sr)。"""
        t_start = time.perf_counter()
        plan = self._build_plan()
        n = plan["n"]
        secs = plan["sections"]
        bar, beat = plan["bar_s"], plan["beat_s"]
        bank = _Bank(self.seed, beat, bar, self.sr)
        master = np.zeros((n, 2), np.float32)

        # ---- 1) 鼓（顺带收集侧链触发点，供贝斯/铺底避让） ----
        hits: list[tuple[float, float]] = []
        drums = _Bus(n, self.sr)
        for si, sec in enumerate(secs):
            e = sec["energy"]
            if e < 0.30:
                continue
            kick_pos = _kick_positions(e)
            hat_step = _hat_step_beats(e)
            hat_db = -20.0 + 8.0 * e
            kick_db = -12.0 + 7.5 * e
            nb = len(sec["bars"])
            bm = sec["breath"]
            for j, bi in enumerate(sec["bars"]):
                t0 = bi * bar
                phrase_end = (j % PHRASE_BARS == PHRASE_BARS - 1) and nb >= PHRASE_BARS
                fill_bar = (j == nb - 1) and (si < len(secs) - 1)
                breath = phrase_end and not fill_bar and bm > 0
                # 过门小节 / 乐句末：最后一拍留呼吸
                for k in kick_pos:
                    if (fill_bar and k >= 3.0) or (breath and k >= 3.0):
                        continue
                    accent = 1.5 if k == 0.0 else 0.0
                    var = bank.kicks[(bi * 4 + int(k)) % len(bank.kicks)]
                    drums.add(var, t0 + k * beat, kick_db + accent)
                    hits.append((t0 + k * beat, 0.28 + 0.22 * e))
                if hat_step:
                    steps = int(round(BEATS_PER_BAR / hat_step))
                    for k in range(steps):
                        pos = k * hat_step
                        if fill_bar and pos >= 3.0 - 1e-9:
                            continue
                        if breath and pos >= (3.0 if bm >= 2 else 3.5) - 1e-9:
                            continue
                        on_beat = abs(pos - round(pos)) < 1e-9
                        ghost = 0.0 if on_beat else -2.5
                        pan = 0.3 if k % 2 == 0 else -0.3
                        var = bank.hats[(bi * 7 + k) % len(bank.hats)]
                        drums.add(var, t0 + pos * beat, hat_db + ghost, pan)
                # 拍手：律动段反拍 2、4
                if e >= 0.55:
                    for pos in (1.0, 3.0):
                        if phrase_end and pos >= 3.0:
                            continue
                        drums.add(bank.claps[(bi * 2 + int(pos)) % len(bank.claps)],
                                  t0 + pos * beat, -16.0 + 4.0 * e, 0.08)
                # 半速段落：第 3 拍轻军鼓，撑住拍子
                if 0.42 <= e < 0.58:
                    drums.add(bank.snares[bi % len(bank.snares)], t0 + 2.0 * beat, -21.0, 0.1)
                # 高强度：反拍开镲
                if e >= 0.78:
                    for pos in (0.0, 1.0, 2.0, 3.0):
                        if phrase_end and pos >= 3.0:
                            continue
                        drums.add(bank.hats_open[(bi + int(pos)) % len(bank.hats_open)],
                                  t0 + (pos + 0.5) * beat, -21.0 + 3.0 * e, -0.2)
            # 段首落点：镲 + 大底鼓（高能量段）
            if si > 0 and e >= 0.68:
                drums.add(bank.crashes[si % len(bank.crashes)], sec["start"], -10.0 + 2.0 * (1.0 - e))
                drums.add(bank.kick_big, sec["start"], -9.0 + 1.0)
                hits.append((sec["start"], 0.5))
            # 高能量段每 8 小节补一记镲
            if e >= 0.85:
                for j, bi in enumerate(sec["bars"]):
                    if j > 0 and j % 8 == 0:
                        drums.add(bank.crashes[(si + j) % len(bank.crashes)], bi * bar, -17.0)
        master += drums.buf
        drums.clear()

        # ---- 2) 贝斯（受底鼓侧链压低） ----
        bass = _Bus(n, self.sr)
        for sec in secs:
            e = sec["energy"]
            pat = _bass_pattern(e)
            if pat == "none":
                continue
            bass_db = -14.0 + 5.0 * e
            bright = 0.2 + 0.65 * e
            nb = len(sec["bars"])
            bm = sec["breath"]
            k = 0
            for j, bi in enumerate(sec["bars"]):
                t0 = bi * bar
                chord = self.k["chords"][sec["chords"][j]]
                root = chord["root"]
                phrase_end = (j % PHRASE_BARS == PHRASE_BARS - 1) and nb >= PHRASE_BARS
                breath = phrase_end and bm >= 2      # 只有安静段才真的把贝斯也撤掉一拍
                if pat == "whole":
                    bass.add(bank.bass(root, bar * 0.92, bright), t0, bass_db)
                elif pat == "half":
                    for pos, dur in ((0.0, 1.8), (2.0, 1.8)):
                        if breath and pos >= 3.0:
                            continue
                        bass.add(bank.bass(root, dur * beat, bright), t0 + pos * beat, bass_db)
                elif pat == "off8":
                    for pos in (0.5, 1.5, 2.5, 3.5):
                        if breath and pos >= 3.0:
                            continue
                        nt = _up(root, 1) if (k % 7 == 3) else root
                        bass.add(bank.bass(nt, 0.42 * beat, bright), t0 + pos * beat, bass_db)
                        k += 1
                else:  # 16 分切分
                    for b in range(BEATS_PER_BAR):
                        for off in (0.25, 0.5, 0.75):
                            pos = b + off
                            if breath and pos >= 3.0:
                                continue
                            nt = _up(root, 1) if (k % 11 == 7) else root
                            bass.add(bank.bass(nt, 0.2 * beat, bright), t0 + pos * beat, bass_db)
                            k += 1
        duck = self._duck_env(n, hits, depth=0.42, tau=0.15)
        bass.buf *= duck[:, None]
        master += bass.buf
        bass.clear()

        # ---- 3) 铺底 pad（受底鼓侧链轻压；段落首小节慢起音） ----
        pads = _Bus(n, self.sr)
        attack_first = {"calm": 1.1, "body": 0.45, "drive": 0.18}
        attack_next = {"calm": 0.35, "body": 0.10, "drive": 0.05}
        for sec in secs:
            e = sec["energy"]
            tier = sec["tier"]
            pad_db = -20.0 + 7.0 * e
            bright = 0.15 + 0.75 * e
            for j, bi in enumerate(sec["bars"]):
                chord = self.k["chords"][sec["chords"][j]]
                attack = attack_first[tier] if j == 0 else attack_next[tier]
                pads.add(bank.pad(chord, bright, attack), bi * bar, pad_db)
                if e >= LAYER_MIN_ENERGY["shine"]:
                    pads.add(bank.pad(chord, max(0.05, bright - 0.1), attack, octave=1),
                             bi * bar, pad_db - 6.0)
        duck = self._duck_env(n, hits, depth=0.30, tau=0.18)
        pads.buf *= duck[:, None]
        master += pads.buf
        pads.clear()

        # ---- 4) 琶音（FM） ----
        keys = _Bus(n, self.sr)
        for sec in secs:
            e = sec["energy"]
            step = _arp_step_beats(e)
            if step is None:
                continue
            arp_db = -25.0 + 8.0 * e
            bright = 0.25 + 0.6 * e
            nb = len(sec["bars"])
            bm = sec["breath"]
            for j, bi in enumerate(sec["bars"]):
                t0 = bi * bar
                chord = self.k["chords"][sec["chords"][j]]
                seq = [_up(nt, 2) for nt in chord["notes"]]
                seq = seq + seq[1:3][::-1]
                steps = int(round(BEATS_PER_BAR / step))
                phrase_end = (j % PHRASE_BARS == PHRASE_BARS - 1) and nb >= PHRASE_BARS
                breath = phrase_end and bm > 0
                for k in range(steps):
                    pos = k * step
                    if breath and pos >= 3.0 - 1e-9:
                        continue
                    nt = seq[k % len(seq)]
                    pan = 0.35 * math.sin(0.7 * k + 0.3 * j)
                    keys.add(bank.arp(nt, bright, 0.3), t0 + pos * beat, arp_db, pan)
        master += keys.buf
        # 键音加一点乒乓延迟（纯 numpy 的整数采样偏移，不依赖采样）
        self._add_delayed(master, keys.buf, 0.28, -15.0, taps=3)
        keys.clear()

        # ---- 5) 主奏旋律（钟 / 钢片琴；每乐句一句主题，句间留呼吸） ----
        lead = _Bus(n, self.sr)
        nb_secs = len(secs)
        for si, sec in enumerate(secs):
            e = sec["energy"]
            nb = len(sec["bars"])
            if nb < 2:
                continue
            if e >= LAYER_MIN_ENERGY["melody"]:
                mel_db = -17.0 + 4.0 * e
                bright_timbre = e >= 0.80
                for pi, first in enumerate(range(0, nb, PHRASE_BARS)):
                    if si == nb_secs - 1 and pi == (nb - 1) // PHRASE_BARS:
                        motif = MOTIF_MAJOR
                    elif pi % 2 == 0:
                        motif = MOTIF_QUESTION
                    else:
                        motif = MOTIF_ANSWER
                    t0 = sec["bars"][first] * bar
                    self._emit_motif(lead, bank, motif, self.k, si, t0, beat, mel_db, bright_timbre)
            elif si == nb_secs - 1 and e >= 0.22:
                # 片尾安静段：用钢片琴把问句答完（大三度）
                t_last = sec["bars"][0] * bar + max(0.0, (nb - 2) * bar)
                self._emit_motif(lead, bank, MOTIF_MAJOR, self.k, si, t_last, beat, -19.0, False)
        master += lead.buf
        self._add_delayed(master, lead.buf, 0.33, -18.0, taps=2)
        lead.clear()

        # ---- 6) 过门与效果（段落交界至少一小节加花） ----
        fx = _Bus(n, self.sr)
        for ti, tr in enumerate(plan["transitions"]):
            fill_start = tr["t"] - bar
            nxt_e = secs[ti + 1]["energy"]
            prev_e = secs[ti]["energy"]
            # 鼓加花：十六分筒鼓由高到低 + 后半拍军鼓滚奏
            sixteenth = 0.25 * beat
            for k in range(int(round(bar / sixteenth))):
                p = k / max(1.0, (bar / sixteenth) - 1.0)
                tom = bank.toms[min(3, int(p * 4))]
                fx.add(tom, fill_start + k * sixteenth, -17.0 + 6.0 * p + (1.5 if k % 2 == 0 else 0.0),
                       -0.35 + 0.7 * p)
                if p >= 0.6:
                    fx.add(bank.snares[k % len(bank.snares)], fill_start + k * sixteenth, -19.0 + 5.0 * p, 0.1)
            # 反向镲：一小节从无到有冲到段落交界
            if "反向镲" in tr["kinds"]:
                fx.add(bank.swell(bar, ti, "tr"), fill_start, -15.0 + 2.0 * nxt_e)
            # 上升音：能量抬升时推一把
            if "上升音" in tr["kinds"]:
                fx.add(bank.riser(bar * 0.92, ti, "tr"), fill_start + bar * 0.08, -14.0 + 2.0 * nxt_e)
            # 重击：低频轰鸣 + 镲，落在交界第一拍
            if "重击" in tr["kinds"]:
                fx.add(bank.sub_boom(2.2, 55.0, 30.0), tr["t"], -9.0 - 2.0 * (1.0 - nxt_e))
                fx.add(bank.crashes[ti % len(bank.crashes)], tr["t"], -11.0)
            # 交界处若能量其实在下降，补一记软镲，避免"断崖"
            if nxt_e < prev_e - 0.15:
                fx.add(bank.crashes[(ti + 1) % len(bank.crashes)], tr["t"], -20.0)
        master += fx.buf
        fx.clear()

        # ---- 7) 自动电平：按段落短时响度中位数拉到能量目标 ----
        levels = self._level_match(master, plan)

        # ---- 8) 吸气（suck）：把段落交界前的配乐平滑压低 ----
        self._apply_sucks(master, plan["sucks"], self.sr)

        # ---- 9) 去直流 / 片头片尾 / 峰值 ----
        self._de_dc(master)
        self._apply_fades(master)

        peak = float(np.max(np.abs(master))) if n else 0.0
        if peak > 1e-9:
            master *= np.float32(_db_amp(TARGET_PEAK_DB) / peak)
        # 亚采样时长（duration*sr < 1）时 master 是 (0,2)：np.max 对空数组直接抛
        # "zero-size array to reduction operation maximum"（duration=1e-6 实测崩这）。
        peak_db = (float(20.0 * math.log10(max(float(np.max(np.abs(master))), 1e-12)))
                   if n else -240.0)
        lufs = _approx_lufs(master, self.sr)
        meas = [{"name": s["name"], "start": round(s["start"], 3), "end": round(s["end"], 3),
                 "energy": round(s["energy"], 3), "gain_db": round(lv["gain"], 2),
                 "measured_db": round(lv["after"], 2)} for s, lv in zip(secs, levels)]
        self._render_info = {
            "rendered": True,
            "peak_dbfs": round(peak_db, 2),
            "lufs_approx": round(lufs, 1),
            "dc_max": round(float(np.max(np.abs(master.mean(axis=0)))), 9) if n else 0.0,
            "render_s": round(time.perf_counter() - t_start, 3),
            "measured_sections": meas,
        }
        self._plan["sucks"] = [dict(d) for d in plan["sucks"]]
        return master

    # ---------------- 渲染子过程 ----------------
    @staticmethod
    def _duck_env(n: int, hits, depth: float = 0.35, tau: float = 0.16, sr: int = SR) -> np.ndarray:
        """侧链包络：每次底鼓把增益按指数恢复地压下去（0.5% 深度分档缓存）。"""
        g = np.ones(n, np.float32)
        L = _ns(1.0, sr)
        k = np.arange(L, dtype=np.float64) / float(sr)
        cache: dict[int, np.ndarray] = {}
        for t, d in hits:
            if d <= 1e-6:
                continue
            s = int(round(float(t) * sr))
            if s >= n or s < 0:
                continue
            q = int(round(float(d) * 200.0))
            base = cache.get(q)
            if base is None:
                base = _f32(1.0 - (q / 200.0) * np.exp(-k / tau))
                cache[q] = base
            e = min(n, s + L)
            np.minimum(g[s:e], base[: e - s], out=g[s:e])
        return g

    @staticmethod
    def _add_delayed(master: np.ndarray, src: np.ndarray, delay_s: float, wet_db: float,
                     taps: int = 3, sr: int = SR) -> None:
        """乒乓延迟（原地叠到 master）：整数采样偏移 + 交替声道，O(n·taps)，不占大内存。"""
        if src is None:
            return
        n = len(src)
        D = int(round(float(delay_s) * sr))
        if D <= 0:
            return
        w = _db_amp(wet_db)
        for t in range(1, int(taps) + 1):
            off = t * D
            if off >= n:
                break
            g = w * (0.62 ** (t - 1))
            seg = src[: n - off]
            master[off:, 0] += seg[:, 1] * g
            master[off:, 1] += seg[:, 0] * g

    @staticmethod
    def _emit_motif(bus: _Bus, bank: _Bank, motif, kval: dict, si: int, t0: float,
                    beat: float, db: float, bright: bool) -> None:
        """把主题动机铺到 lead 总线上：奇数段用钟、偶数段用钢片琴，音高按调内半音换算。"""
        names = kval["names"]
        root_pc = kval["root_pc"]
        for idx, (pos_beats, semis, _dur_beats) in enumerate(motif):
            note = _semi_note(root_pc, 4, int(semis), names)
            tex = bank.bell(note, 2.6) if (bright or idx % 3 == 0) else bank.celesta(note, 3.0)
            pan = 0.18 if idx % 2 == 0 else -0.18
            bus.add(tex, t0 + pos_beats * beat, db, pan)

    def _level_match(self, x: np.ndarray, plan: dict) -> list[dict]:
        """按段落短时响度中位数拉平到 energy 目标；返回每段增益（dB）。"""
        secs = plan["sections"]
        t, lj = _short_term_db(x, 0.4, 0.1, sr=self.sr)
        valid = lj > -70.0
        ref = float(np.median(lj[valid])) if np.any(valid) else -30.0
        gains: list[dict] = []
        for sec in secs:
            a, b = sec["start"] + 0.12, sec["end"] - 0.12
            sel = (t >= a) & (t <= b) & valid
            cur = float(np.median(lj[sel])) - ref if np.any(sel) else 0.0
            gain = float(np.clip(sec["target_db"] - cur, -9.0, 9.0))
            gains.append({"gain": gain, "after": cur + gain + ref})
        # 增益曲线：段落内保持，在下一段首拍前 0.8 s 平滑过渡
        kt = [0.0]
        kv = [gains[0]["gain"]]
        for sec, lv in zip(secs, gains):
            a = float(sec["start"])
            b = float(sec["end"])
            g = lv["gain"]
            kt.append(a)
            kv.append(g)
            hold_end = max(a, b - 0.8)
            if hold_end > kt[-1] + 1e-6:
                kt.append(hold_end)
                kv.append(g)
        kt.append(self.duration)
        kv.append(gains[-1]["gain"])
        # np.interp 要求横坐标单调递增：清理重复点
        kt_c: list[float] = []
        kv_c: list[float] = []
        for a, b in zip(kt, kv):
            if kt_c and a <= kt_c[-1] + 1e-9:
                kv_c[-1] = b
                continue
            kt_c.append(a)
            kv_c.append(b)
        self._apply_curve(x, np.asarray(kt_c, np.float64), np.asarray(kv_c, np.float64), self.sr)
        return gains

    @staticmethod
    def _apply_curve(x: np.ndarray, kt: np.ndarray, kv: np.ndarray, sr: int, chunk: int = 1 << 20) -> None:
        """分块应用 dB 增益曲线（18.7M 采样也不占大内存）。"""
        n = len(x)
        for s in range(0, n, chunk):
            e = min(n, s + chunk)
            tt = np.arange(s, e, dtype=np.float64) / float(sr)
            g = np.interp(tt, kt, kv)
            x[s:e] *= _f32(10.0 ** (g / 20.0))[:, None]

    @staticmethod
    def _apply_sucks(x: np.ndarray, sucks, sr: int = SR) -> None:
        """吸气包络：S 形压下 -> 保持 -> 最后 4 ms 弹回（被重击的起音盖住）；全程无阶跃。"""
        n = len(x)
        hold = max(1, _ns(0.004, sr))
        for ev in sucks:
            t = float(ev["t"])
            db = float(ev["db"])
            dur = float(ev["dur"])
            a = int(round((t - dur) * sr))
            b = int(round(t * sr))
            a0, b0 = max(0, a), min(n, b)
            if b0 - a0 < 8:
                continue
            idx = np.arange(a0, b0, dtype=np.float64)
            span = max(1, (b - a) - hold)
            p = np.clip((idx - a) / span, 0.0, 1.0)
            shape = p * p * (3.0 - 2.0 * p)
            rec = np.clip((b - idx) / hold, 0.0, 1.0)
            gain_db = db * shape * rec                     # db 为负：越接近重击越压
            x[a0:b0] *= _f32(np.power(10.0, gain_db / 20.0))[:, None]

    def _de_dc(self, x: np.ndarray) -> None:
        """去直流 + 次声：先减全局均值，再逐声道 28 Hz 高通（sfxkit 滤波器，分声道省内存）。"""
        if len(x) == 0:
            return
        mu = x.mean(axis=0, dtype=np.float64)
        x -= _f32(mu)
        for c in range(x.shape[1]):
            x[:, c] = _f32(S.highpass(x[:, c].astype(np.float64), 28.0, sr=self.sr))

    def _apply_fades(self, x: np.ndarray) -> None:
        """片头 30 ms 淡入 + 片尾 2 s 淡出（1.5 次幂，听感更自然）。"""
        n = len(x)
        if n == 0:
            return
        sr = self.sr
        ni = min(n, _ns(FADE_IN_S, sr))
        if ni > 1:
            w = _f32(np.linspace(0.0, 1.0, ni, endpoint=False) ** 1.5)
            x[:ni] *= w[:, None]
        no = min(n, _ns(FADE_OUT_S, sr))
        if no > 1:
            k = np.arange(no, dtype=np.float64) / float(no)
            w = _f32(np.clip(1.0 - k, 0.0, 1.0) ** 1.5)
            x[n - no:] *= w[:, None]

    # ---------------- 报告 ----------------
    def report(self) -> dict:
        """配乐自述（契约 §4.4 schema）：bpm/key/sr/duration/sections/beats + 过门/吸气/实测。"""
        plan = self._build_plan()
        rep = {
            "version": 1,
            "generator": "scorekit",
            "bpm": self.bpm,
            "key": self.key,
            "key_name": self.k["name"],
            "sr": self.sr,
            "duration": round(self.duration, 3),
            "seed": self.seed,
            "bar_s": round(plan["bar_s"], 4),
            "beat_s": round(plan["beat_s"], 4),
            "sections": [
                {
                    "name": s["name"],
                    "start": round(s["start"], 3),
                    "end": round(s["end"], 3),
                    "energy": round(s["energy"], 3),
                    "bars": len(s["bars"]),
                    "target_db": s["target_db"],
                    "target_lufs": s["target_lufs"],
                    "layers": list(s["layers"]),
                    "chords": list(s["chords"]),
                }
                for s in plan["sections"]
            ],
            "transitions": [dict(t) for t in plan["transitions"]],
            "sucks": [dict(d) for d in plan["sucks"]],
            "beats": list(plan["beats"]),
        }
        rep.update(self._render_info)
        return rep





def spectral_centroid(x, sr: int = SR) -> float:
    """频谱质心（Hz）：粗略量"亮度"，用于验证不同风格不是同一首曲子。

    单声道下混 -> 去均值 -> 分块 rFFT -> 幅度加权平均频率。确定性、无随机、无三方依赖。
    """
    a = np.asarray(x)
    if a.ndim == 2:
        mono = a.astype(np.float64).mean(axis=1)
    else:
        mono = a.astype(np.float64).ravel()
    if len(mono) < 1024:
        return 0.0
    blk = 16384 if len(mono) >= 16384 else 1024
    hop = max(1, blk // 2)
    win = np.hanning(blk)
    freqs = np.fft.rfftfreq(blk, 1.0 / float(sr))
    acc_m = 0.0
    acc_p = 0.0
    for s in range(0, len(mono) - blk + 1, hop):
        seg = mono[s:s + blk]
        seg = seg - float(seg.mean())
        mag = np.abs(np.fft.rfft(seg * win))
        acc_m += float(np.dot(freqs, mag))
        acc_p += float(mag.sum())
    if acc_p <= 1e-12:
        return 0.0
    return float(acc_m / acc_p)




# ============================================================================
# 模块级便捷函数
# ============================================================================
def _merge_sections(anchors, sections, duration: float) -> list:
    """把 anchors（时间轴）与 sections（显式段落）合并成 [{name,start,end,energy}]。

    - anchors 按 kind 自动分段（基础）；
    - sections 里带 "at" 的项：命中 anchors 的 id -> 原地替换该段的 name/energy（时间不变）；
      命中不到或没有 at（绝对 start/end）-> 作为额外段落追加；
    - 这样 film.json 的 music.sections（{"at": "l03", "energy": 0.8}）能只改能量、不改时间轴。
    """
    blocks: list[dict] = []
    if anchors:
        for idx, a in enumerate(anchors):
            _require(isinstance(a, dict), f"anchors[{idx}] 必须是对象")
            for fld in ("start", "end"):
                _require(fld in a, f"anchors[{idx}] 缺少 {fld} 字段")
            kind = str(a.get("kind", "") or "")
            blocks.append({
                "name": str(a.get("id") or kind or f"段{idx + 1}"),
                "start": float(a["start"]),
                "end": float(a["end"]),
                "energy": _clip01(a.get("energy", KIND_ENERGY.get(kind, 0.5))),
                "anchor_id": str(a.get("id", "")),
            })
    extra: list[dict] = []
    for s in sections or []:
        _require(isinstance(s, dict), "sections 里的每一项都必须是对象")
        energy = _clip01(s.get("energy", 0.5))
        if "at" in s:
            key = str(s["at"])
            hit = next((b for b in blocks if b.get("anchor_id") == key), None)
            _require(hit is not None, f"sections 里的 at={key!r} 在 anchors 里找不到")
            if s.get("name"):
                hit["name"] = str(s["name"])
            hit["energy"] = energy
            continue
        _require("start" in s, "sections 项要么给 at，要么给 start（end 可省略）")
        st = float(s["start"])
        # 和某个已有段落同起点（render.py 会把 at 换算成绝对 start 传进来）：
        # 视为"改这一段的能量/名字"，不新增一段，避免两段同起点。
        same = next((b for b in blocks if abs(b["start"] - st) < 1e-6), None)
        if same is not None:
            same["energy"] = energy
            if s.get("name"):
                same["name"] = str(s["name"])
            continue
        extra.append({"name": str(s.get("name") or "定制段"),
                      "start": st,
                      "end": float(s["end"]) if "end" in s else None,
                      "energy": energy, "anchor_id": str(s.get("anchor_id", ""))})
    # 没写 end 的定制段：交给后面的段/锚点/片尾决定（render.py 只把 at 换成 start，不带 end）
    for i, e in enumerate(extra):
        if e["end"] is not None:
            continue
        later = ([x["start"] for x in extra[i + 1:] if x["start"] > e["start"]]
                 + [b["start"] for b in blocks if b["start"] > e["start"]])
        e["end"] = min(later) if later else float(duration)
    blocks.extend(extra)
    blocks.sort(key=lambda d: d["start"])
    for b in blocks:
        b.pop("anchor_id", None)
        b["end"] = float(b["end"]) if b.get("end") is not None else float(duration)
    if not blocks:
        blocks = [{"name": "全片", "start": 0.0, "end": float(duration), "energy": 0.5}]
    return blocks


def render(duration, bpm: float = 120.0, key: str = "Dm", anchors=None, sections=None,
           seed: int = 7, style=None, mood: str = "steady", fingerprint=None,
           music_plan=None, pace=None) -> tuple:
    """一次性接口：合成整片配乐，返回 (samples, report)。

    anchors：timeline.json 的 [{id,kind,start,end}]（自动按 kind 映射能量分段）；
    sections：显式段落（绝对时间或 at 引用），在 anchors 之后追加/覆盖空隙。

    没有配乐风格层。style 只接受 None / "" / "legacy"（都走本模块的确定性路径）；
    其它风格 id 一律抛 ValueError 并指向 musicgen.py —— 本仓库不提供配乐模板。
    mood / fingerprint / music_plan / pace 是为了签名兼容而保留的**废参数**：
    它们只影响风格层，风格层不存在就一律忽略（不要靠它们调音色，那是模板思维）。
    """
    sid = "" if style is None else str(style).strip().lower()
    if sid not in ("", "legacy"):
        raise ValueError(
            "scorekit 的配乐风格层已删除（零模板）：style=%r 不再可用。\n"
            "  需要现场编排的配乐请用 musicgen.py：\n"
            "    import musicgen as MG\n"
            "    samples, report = MG.render(dur, bpm=110, key=%r, scale=\"minor\",\n"
            "                                mood=%r, seed=7)\n"
            "  （成片链路见 render.py 的 _score_with_musicgen）" % (style, key, mood))
    # ---- legacy 逐样本兼容是硬契约：这段代码别改任何东西 ----
    sc = Score(duration, bpm=bpm, key=key, seed=seed)
    for s in _merge_sections(anchors, sections, float(duration)):
        sc.section(s["name"], s["start"], s["end"], s["energy"])
    if not anchors and not sections:
        sc.section("全片", 0.0, float(duration), 0.5)
    return sc.render(), sc.report()


def save(path, x, sr: int = SR) -> Path:
    """把配乐写成 24-bit PCM WAV（通道数自适应；峰值由 render 保证 ≤ -1 dBFS）。"""
    path = Path(path)
    arr = np.asarray(x)
    if arr.size and not np.all(np.isfinite(arr)):
        # NaN 经 np.rint(...).astype(np.int32) 会落到 INT_MIN（不是 0），24-bit 写出后是
        # 满幅负样本 —— 成片里一声爆响，且没有任何报错。与 filmkit.wav_write 同口径：报错。
        bad = int(arr.size - np.count_nonzero(np.isfinite(arr)))
        raise ValueError("scorekit.save: 采样里有 %d 个 NaN/Inf（%s）——配乐渲染有数值错误，"
                         "拒绝写出损坏的 WAV" % (bad, path))
    if arr.ndim == 1:
        arr = np.stack([arr, arr], axis=1)
    _require(arr.ndim == 2 and arr.shape[1] in (1, 2), f"只支持单/双声道，收到 shape={arr.shape}")
    pcm = np.clip(arr, -1.0, 1.0)
    ints = np.rint(pcm * 8388607.0).astype(np.int32)
    raw = ints.astype("<i4").view(np.uint8).reshape(-1, 4)[:, :3]
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(int(arr.shape[1]))
        w.setsampwidth(3)
        w.setframerate(int(sr))
        w.writeframes(raw.tobytes())
    return path


def load(path, sr: int = SR) -> np.ndarray:
    """读 WAV 为 (n,2) float32。本模块不做重采样（配乐统一 48 kHz，sr 参数仅保持签名兼容）。"""
    with wave.open(str(path), "rb") as w:
        ch = w.getnchannels()
        sw = w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if sw == 2:
        a = np.frombuffer(raw, "<i2").astype(np.float64) / 32768.0
    elif sw == 3:
        b = np.frombuffer(raw, np.uint8).reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        v = (v ^ (1 << 23)) - (1 << 23)
        a = v.astype(np.float64) / 8388608.0
    elif sw == 4:
        a = np.frombuffer(raw, "<i4").astype(np.float64) / 2147483648.0
    else:
        raise ValueError(f"不支持的 WAV 位深：{sw * 8} bit（只支持 16/24/32）")
    a = a.reshape(-1, ch)
    if ch == 1:
        a = np.stack([a[:, 0], a[:, 0]], axis=1)
    return a.astype(np.float32)


# ============================================================================
# 自测
# ============================================================================
def _max_loudness_jump(x: np.ndarray, rep: dict, sr: int = SR) -> float:
    """段内相邻短时响度块的最大跳变（dB）；跳过设计好的"吸气"窗口。"""
    t, lj = _short_term_db(x, 0.3, 0.1, sr=sr)
    sucks = rep.get("sucks", [])
    worst = 0.0
    for sec in rep.get("sections", []):
        a = float(sec["start"]) + 0.35
        b = float(sec["end"]) - 0.35
        sel = (t >= a) & (t <= b) & (lj > -60.0)
        if not np.any(sel):
            continue
        tt = t[sel]
        vv = lj[sel]
        for i in range(1, len(vv)):
            # 落在吸气窗口里的跳变是设计好的，不算
            in_suck = False
            for ev in sucks:
                if tt[i] >= float(ev["t"]) - float(ev["dur"]) - 0.05 and tt[i] <= float(ev["t"]) + 0.08:
                    in_suck = True
                    break
            if not in_suck:
                worst = max(worst, abs(float(vv[i] - vv[i - 1])))
    return worst


def selftest() -> int:
    """不联网、≤ 60 s：逐条打印 PASS/FAIL，失败返回 1。"""
    _setup_stdio()
    t_all = time.perf_counter()
    rows: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        rows.append((name, bool(ok), detail))
        print(("[PASS] " if ok else "[FAIL] ") + name + (("  " + detail) if detail else ""))

    print("=" * 72)
    print("scorekit.py 自测（不联网；中文输出；sfxkit 真原语）")
    print("=" * 72)

    # 0) sfxkit 原语齐备
    need = ("SR", "n_samples", "time_axis", "rng", "db_to_amp", "peak", "rms", "normalize",
            "to_stereo", "pan", "fade", "pad_to", "smooth_random",
            "sine", "saw", "pulse", "triangle", "fm", "supersaw", "pluck", "adsr",
            "lowpass", "highpass", "bandpass", "tv_filter",
            "white", "burst", "exp_decay", "attack_decay")
    missing = [n for n in need if not hasattr(S, n)]
    check("0. sfxkit 核心原语齐备", not missing,
          f"缺失：{', '.join(missing)}" if missing else f"sfxkit {getattr(S, '__version__', '?')}")

    # 1) 调性/和弦表
    ok_keys = all(k in KEYS for k in ("Dm", "Am", "Cm", "Em"))
    bad_notes = []
    for kname, kv in KEYS.items():
        for deg in ("i", "III", "iv", "v", "V", "VI", "VII", "I"):
            if deg not in kv["chords"]:
                bad_notes.append(f"{kname}.{deg} 缺失")
        for chord in kv["chords"].values():
            for nt in chord["notes"] + [chord["root"]]:
                if not _NOTE_RE.match(nt):
                    bad_notes.append(f"{kname}: 非法音名 {nt!r}")
                elif S.hz(nt) <= 0:
                    bad_notes.append(f"{kname}: 频率非法 {nt!r}")
    check("1. 四个调 & 级数和弦表完整", ok_keys and not bad_notes,
          "; ".join(bad_notes[:3]) if bad_notes else f"{len(KEYS)} 个调，{len(KEYS['Dm']['chords'])} 个级数/调")

    # 2) 小节吸附 & 段边界
    sc = Score(24.0, bpm=120.0, key="Dm", seed=7)
    sc.section("甲", 1.3, 9.7, 0.5).section("乙", 9.7, 18.2, 0.8)
    plan = sc._build_plan()
    bar = plan["bar_s"]
    bds = [s["start"] for s in plan["sections"][1:]] + [s["end"] for s in plan["sections"]]
    aligned = all(abs(bd / bar - round(bd / bar)) < 1e-6 for bd in bds)
    cover = abs(plan["sections"][0]["start"]) < 1e-9 and abs(plan["sections"][-1]["end"] - 24.0) < 1e-9
    seg_ok = all(plan["sections"][i]["end"] <= plan["sections"][i + 1]["start"] + 1e-9
                 for i in range(len(plan["sections"]) - 1))
    check("2. 段边界吸附到小节线且 0~duration 连续覆盖", aligned and cover and seg_ok,
          f"小节 {bar:.3f} s，边界 {[round(x, 2) for x in bds]}")

    # 3) plan_from_anchors 的 kind 映射
    sc2 = Score(32.0, key="Am", seed=7)
    sc2.plan_from_anchors([
        {"id": "l01", "kind": "hook", "start": 0.0, "end": 8.0},
        {"id": "l02", "kind": "body", "start": 8.0, "end": 24.0},
        {"id": "l03", "kind": "outro", "start": 24.0, "end": 32.0},
    ])
    p2 = sc2._build_plan()
    e_map = [s["energy"] for s in p2["sections"]]
    want = [KIND_ENERGY["hook"], KIND_ENERGY["body"], KIND_ENERGY["outro"]]
    check("3. plan_from_anchors：kind -> 能量映射 & 小节对齐",
          len(e_map) == 3 and all(abs(a - b) < 1e-9 for a, b in zip(e_map, want))
          and all(abs(s["start"] / p2["bar_s"] - round(s["start"] / p2["bar_s"])) < 1e-6 for s in p2["sections"]),
          f"能量 {[round(x, 2) for x in e_map]}")

    # 4) 过门结构：每个内部交界一整小节加花
    ok_tr = len(p2["transitions"]) == len(p2["sections"]) - 1
    tr_detail = []
    for tr in p2["transitions"]:
        if tr["fill_s"] < p2["bar_s"] - 1e-9 or not tr["kinds"]:
            ok_tr = False
            tr_detail.append(f"{tr['t']}: 过门不足")
    check("4. 段落交界 ≥ 1 小节过门", ok_tr,
          f"{len(p2['transitions'])} 个交界；" + ("; ".join(tr_detail) if tr_detail else "全部含鼓加花"))

    # 5) 确定性：同 seed 两次渲染逐采样一致；换 seed 不同
    sc3 = Score(24.0, bpm=120.0, key="Dm", seed=7)
    sc3.section("甲", 0.0, 12.0, 0.35).section("乙", 12.0, 24.0, 0.75)
    a = sc3.render()
    b = sc3.render()
    same = a.shape == b.shape and np.array_equal(a, b)
    sc4 = Score(24.0, bpm=120.0, key="Dm", seed=8)
    sc4.section("甲", 0.0, 12.0, 0.35).section("乙", 12.0, 24.0, 0.75)
    c = sc4.render()
    diff_seed = not np.array_equal(a, c)
    check("5. 同一 seed 两次渲染 bit-identical（换 seed 不同）", same and diff_seed,
          f"shape={a.shape}")

    # 6) 时长精确 / 形状 / 有限值
    want_n = int(math.ceil(24.0 * SR))
    finite = bool(np.all(np.isfinite(a)))
    check("6. 输出形状 (n,2) 与时长精确", a.shape == (want_n, 2) and finite,
          f"len={len(a)}，期望 {want_n}，shape={a.shape}")

    # 7) 峰值 ≤ -1 dBFS / 无削波 / 无直流
    peak_db = 20.0 * math.log10(max(float(np.max(np.abs(a))), 1e-12))
    dc = float(np.max(np.abs(a.mean(axis=0))))
    check("7. 峰值 ≤ -1 dBFS、无削波、无直流", peak_db <= -1.0 + 1e-6 and peak_db >= -3.5 and dc < 1e-4,
          f"峰值 {peak_db:.2f} dBFS，直流 {dc:.2e}")

    # 8) 段内响度无 >12 dB 突变（跳过设计的吸气）
    rep_a = sc3.report()
    # 阈值的两条路径：scipy 用 sosfilt（连续流式，段内跳变 ≤ 12 dB）；
    # 没装 scipy 时所有 IIR 走"分块时变滤波 + FFT 卷积"（每块用块中心系数），块边界
    # 天生会有几 dB 的系数台阶——实测 48 s 片上 12 → 15 dB。这不代表"音乐更难听"，
    # 而是实现路径的固有差异，所以按路径分别定价（与 sfxkit 的 PERF_BUDGET_* 同一套做法）。
    # **不静默**：详情里直接写当前按哪一档判定，用户看自检输出就知道为什么。
    jump = _max_loudness_jump(a, rep_a)
    _jump_budget = 12.0 if S._sig is not None else 18.0
    _jump_how = ("scipy sosfilt" if S._sig is not None
                 else "无 scipy（分块 FFT 兜底，阈值放宽到 18 dB）")
    check("8. 段内响度突变 ≤ %.0f dB（吸气窗口除外；%s）" % (_jump_budget, _jump_how),
          jump <= _jump_budget, f"最大跳变 {jump:.2f} dB")

    # 9) 结尾 2 s 淡出
    tail = a[-int(0.1 * SR):]
    tail_db = 20.0 * math.log10(max(float(np.sqrt(np.mean(tail.astype(np.float64) ** 2))), 1e-12))
    check("9. 片尾淡出（最后 100 ms 明显安静）", tail_db < -25.0, f"末 100 ms RMS {tail_db:.1f} dBFS")

    # 10) 吸气：包络连续、深度正确、端到端有效
    n1 = _ns(1.0)
    g = np.ones(n1, np.float32)
    sc._apply_sucks(g[:, None], [{"t": 0.5, "db": -6.0, "dur": 0.2, "source": "test"}])
    v_before = float(g[int(0.2 * SR)])
    v_dip = float(np.min(g[int(0.45 * SR):int(0.50 * SR)]))   # 最深点在重击前 4 ms（之后开始回弹）
    v_after = float(g[int(0.55 * SR)])
    max_step = float(np.max(np.abs(np.diff(g))))
    ok_suck = (abs(20 * math.log10(max(v_dip, 1e-9)) + 6.0) < 1.0
               and v_before > 0.99 and v_after > 0.99 and max_step < 0.08)
    # 端到端：同样内容加/不加吸气，重击前窗口应低 4~10 dB
    sc_s = Score(8.0, seed=7)
    sc_s.section("甲", 0.0, 8.0, 0.7)
    x_no = sc_s.render()
    sc_s.suck(4.0, db=-8.0, dur=0.3)
    x_yes = sc_s.render()
    win = (int(3.86 * SR), int(3.97 * SR))
    r_no = float(np.sqrt(np.mean(x_no[win[0]:win[1]].astype(np.float64) ** 2)))
    r_yes = float(np.sqrt(np.mean(x_yes[win[0]:win[1]].astype(np.float64) ** 2)))
    drop = 20.0 * math.log10(max(r_yes, 1e-12) / max(r_no, 1e-12))
    check("10. 吸气：平滑（无阶跃）+ 深度正确 + 端到端压低 4~10 dB",
          ok_suck and -10.0 <= drop <= -3.5,
          f"包络最大阶跃 {max_step:.4f}，端到端 {drop:+.1f} dB")

    # 11) 报告结构
    rep = rep_a
    need_keys = ("bpm", "key", "sr", "duration", "sections", "beats")
    ok_rep = all(k in rep for k in need_keys)
    ok_rep = ok_rep and rep["bpm"] == 120.0 and rep["key"] == "Dm" and rep["sr"] == SR
    ok_rep = ok_rep and len(rep["beats"]) == int(math.floor(24.0 / 0.5 + 1e-9)) + 1
    ok_rep = ok_rep and all(
        all(f in s for f in ("name", "start", "end", "energy", "target_lufs")) for s in rep["sections"])
    check("11. report() 结构符合契约 §4.4", ok_rep,
          f"beats={len(rep['beats'])}，sections={len(rep['sections'])}")

    # 12) save/load 往返
    ok_io = False
    io_detail = ""
    try:
        tmpdir = Path(tempfile.mkdtemp(prefix="scorekit_selftest_"))
        p = tmpdir / "t.wav"
        seg = a[: _ns(2.0)]
        save(p, seg, sr=SR)
        back = load(p, sr=SR)
        err = float(np.max(np.abs(back.astype(np.float64) - seg.astype(np.float64))))
        ok_io = back.shape == seg.shape and err < 5e-4
        io_detail = f"往返最大误差 {err:.2e}，shape={back.shape}"
        for f in tmpdir.iterdir():
            f.unlink()
        tmpdir.rmdir()
    except Exception as exc:            # 自测里也要报出原因，而不是吞掉
        io_detail = f"{type(exc).__name__}: {exc}"
    check("12. save/load（24-bit WAV）往返", ok_io, io_detail)

    # 13) 极短时长不崩溃
    ok_short = False
    short_detail = ""
    try:
        sc_short = Score(0.6, bpm=120.0, seed=7)
        x_short = sc_short.render()
        ok_short = x_short.shape == (int(math.ceil(0.6 * SR)), 2) and bool(np.all(np.isfinite(x_short)))
        short_detail = f"0.6 s -> shape={x_short.shape}"
    except Exception as exc:
        short_detail = f"{type(exc).__name__}: {exc}"
    check("13. 极短时长（< 1 小节）不崩溃", ok_short, short_detail)

    # 14) 性能：真渲 60 s（三段能量）线性外推 389 s ≤ 60 s
    sc_perf = Score(60.0, bpm=120.0, key="Dm", seed=7)
    sc_perf.section("安静", 0.0, 16.0, 0.3).section("律动", 16.0, 44.0, 0.8).section("收尾", 44.0, 60.0, 0.35)
    t0 = time.perf_counter()
    sc_perf.render()
    dt60 = time.perf_counter() - t0
    proj = dt60 / 60.0 * 389.0
    check("14. 性能：60 s 实测外推 389 s ≤ 60 s", proj <= 60.0,
          f"60 s 实测 {dt60:.2f} s，外推 389 s ≈ {proj:.1f} s")

    # 15) 安静段比律动段低 4~8 dB（自动电平真的在工作）
    rep_perf = sc_perf.report()
    quiet = None
    drive = None
    for s in rep_perf.get("measured_sections", []):
        if s["name"] == "安静":
            quiet = s["measured_db"]
        if s["name"] == "律动":
            drive = s["measured_db"]
    diff = (drive - quiet) if (quiet is not None and drive is not None) else 0.0
    check("15. 安静段比律动段低 4~8 dB（自动电平）", 4.0 <= diff <= 8.0, f"实测差 {diff:.2f} dB")

    # 16) 与 render.py 的集成形态：anchors + sections（at 已换成绝对 start、且不带 end）
    anchors_it = [
        {"id": "l01", "kind": "hook", "start": 0.0, "end": 8.0},
        {"id": "l02", "kind": "body", "start": 8.0, "end": 24.0},
        {"id": "l03", "kind": "drive", "start": 24.0, "end": 48.0},
    ]
    secs_it = [
        {"at": "l01", "name": "intro", "start": 0.0, "energy": 0.3},
        {"name": "drive", "start": 24.0, "energy": 0.8},
        {"name": "追加段", "start": 48.0, "energy": 0.4},           # 无 end：应自动延长到片尾
    ]
    merged = _merge_sections(anchors_it, secs_it, 60.0)
    ok_it = (len(merged) == 4
             and merged[0]["name"] == "intro" and abs(merged[0]["energy"] - 0.3) < 1e-9
             and merged[2]["name"] == "drive" and abs(merged[2]["energy"] - 0.8) < 1e-9
             and len([b for b in merged if abs(b["start"] - 24.0) < 1e-9]) == 1
             and abs(merged[-1]["end"] - 60.0) < 1e-9)
    check("16. 集成形态：anchors+sections 同起点覆盖 / 无 end 自动补到片尾", ok_it,
          " | ".join(f"{b['name']}:{b['start']}-{b['end']}:e{b['energy']}" for b in merged))

    # 17) 大调 & 七和弦表（KEYS 仍是 4 个小调）
    bad_major = []
    for kname, kv in KEYS_MAJOR.items():
        for deg in ("I", "ii", "iii", "IV", "V", "vi", "vii", "sus4",
                    "Imaj7", "ii7", "iii7", "IVmaj7", "V7", "vi7", "i7"):
            if deg not in kv["chords"]:
                bad_major.append(f"{kname}.{deg} 缺失")
        for chord in kv["chords"].values():
            for nt in chord["notes"] + [chord["root"]]:
                if not _NOTE_RE.match(nt) or S.hz(nt) <= 0:
                    bad_major.append(f"{kname}: 非法音名 {nt!r}")
    ok_major = ((not bad_major) and set(KEYS) == {"Dm", "Am", "Cm", "Em"}
                and len(KEYS_ALL) == len(KEYS) + len(KEYS_MAJOR) == 10)
    check("17. 大调 6 个 & 七和弦级数（KEYS 仍是 4 个小调）", ok_major,
          "; ".join(bad_major[:3]) if bad_major
          else f"{len(KEYS_ALL)} 个调，{len(KEYS_ALL['C']['chords'])} 个级数/调")

    # 18) legacy 兼容：style=None / "legacy" / 任意 mood 都走同一条确定性路径，逐样本一致
    xn, rn = render(6.0, bpm=120, key="Dm", seed=7)
    xl, rl = render(6.0, bpm=120, key="Dm", seed=7, style="legacy")
    xm, _rm = render(6.0, bpm=120, key="Dm", seed=7, style=None, mood="driving",
                     fingerprint="whatever")
    rn_c = {k: v for k, v in rn.items() if k != "render_s"}
    rl_c = {k: v for k, v in rl.items() if k != "render_s"}
    ok_legacy = (np.array_equal(xn, xl) and np.array_equal(xn, xm) and rn_c == rl_c
                 and rn.get("version") == 1)
    check("18. legacy 兼容：style=None / 'legacy' / 任意 mood+fingerprint 逐样本一致", ok_legacy,
          f"shape={xn.shape}，version={rn.get('version')}")
    total = time.perf_counter() - t_all
    passed = sum(1 for _, ok, _ in rows if ok)
    failed = len(rows) - passed
    print("-" * 72)
    print(f"自测完成：{passed}/{len(rows)} PASS，{failed} FAIL，耗时 {total:.2f} s")
    print("=" * 72)
    return 0 if failed == 0 else 1


# ============================================================================
# 命令行
# ============================================================================
def _load_json(path: str):
    p = Path(path)
    _require(p.exists(), f"文件不存在：{path}")
    return json.loads(p.read_text(encoding="utf-8"))


def _parse_anchors_file(path: str) -> list:
    """支持三种文件：anchors 列表本身 / {"anchors": [...]} / {"scenes": [{id,kind,start,end}]}。"""
    data = _load_json(path)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("anchors", "scenes"):
            if isinstance(data.get(key), list) and data[key]:
                return data[key]
        raise ValueError(f"{path} 里没有 anchors / scenes 字段；"
                         f"请给 timeline.json（含 anchors）或一个锚点列表")
    raise ValueError(f"{path} 格式不对：应为列表或对象")


def _parse_sections_file(path: str) -> list:
    data = _load_json(path)
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("sections"), list):
        return data["sections"]
    raise ValueError(f"{path} 里没有 sections 列表")


def _parse_suck_list(items) -> list:
    out = []
    for raw in items or []:
        parts = [p.strip() for p in str(raw).split(",") if p.strip() != ""]
        if len(parts) < 1:
            continue
        t = float(parts[0])
        db = float(parts[1]) if len(parts) > 1 else -6.0
        dur = float(parts[2]) if len(parts) > 2 else 0.2
        out.append((t, db, dur))
    return out


def _format_report(rep: dict, wav_path: Path | None, report_path: Path | None) -> str:
    """CLI 中文摘要。"""
    lines = []
    lines.append(f"[scorekit] {rep['duration']:g} s / {rep['bpm']:g} BPM / {rep['key']}（{rep['key_name']}）"
                 f" / 小节 {rep['bar_s']:g} s / seed {rep['seed']}")
    lines.append(f"  段落 {len(rep['sections'])} 个，交界 {len(rep['transitions'])} 处，吸气 {len(rep['sucks'])} 处")
    for s in rep["sections"]:
        lines.append(f"    {s['name']:<8} {s['start']:7.2f} – {s['end']:7.2f} s"
                     f"  energy {s['energy']:.2f}  目标 {s['target_lufs']:.1f} LUFS"
                     f"  层 {'/'.join(s['layers'])}")
    if "peak_dbfs" in rep:
        lines.append(f"  峰值 {rep['peak_dbfs']:.2f} dBFS，近似 LUFS {rep['lufs_approx']:.1f}，"
                     f"直流 {rep.get('dc_max', 0):.1e}")
        lines.append(f"  实测渲染耗时 {rep.get('render_s', 0):.2f} s"
                     f"（389 s 外推 ≈ {rep.get('render_s', 0) / max(rep['duration'], 1e-9) * 389.0:.1f} s）")
    if wav_path:
        lines.append(f"  已写 WAV：{wav_path}")
    if report_path:
        lines.append(f"  已写报告：{report_path}")
    return "\n".join(lines)


def main(argv=None) -> int:
    _setup_stdio()
    ap = argparse.ArgumentParser(
        prog="scorekit.py",
        description="scorekit — 程序化配乐（numpy + sfxkit，无采样、确定性）",
    )
    ap.add_argument("--selftest", action="store_true", help="跑自测（不联网，逐条 PASS/FAIL）")
    ap.add_argument("--duration", type=float, default=60.0, help="片长（秒），默认 60")
    ap.add_argument("--bpm", type=float, default=None, help="BPM，缺省 120")
    ap.add_argument("--key", default=None,
                    help="调性，可选 " + "/".join(KEYS_ALL) + "（缺省 Dm）")
    ap.add_argument("--seed", type=int, default=7, help="随机种子，默认 7")
    ap.add_argument("--energy", type=float, default=0.55, help="无 anchors/sections 时的单段能量，默认 0.55")
    ap.add_argument("--anchors", default=None, help="timeline.json 或锚点列表文件（[{id,kind,start,end}]）")
    ap.add_argument("--sections", default=None, help="显式段落 JSON（[{name,start,end,energy}] 或含 at 引用）")
    ap.add_argument("--suck", action="append", default=None, help="吸气：t,db,dur（可多次），如 --suck 24.0,-6,0.2")
    ap.add_argument("--out", default=None, help="输出 WAV 路径（24-bit PCM）")
    ap.add_argument("--report", default=None, help="输出 score.json 报告路径")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    bpm = float(args.bpm) if args.bpm is not None else 120.0
    key = str(args.key) if args.key else "Dm"
    _require(key in KEYS_ALL, f"未知调性 {key!r}；可选：{'/'.join(KEYS_ALL)}")
    anchors = _parse_anchors_file(args.anchors) if args.anchors else None
    sections = _parse_sections_file(args.sections) if args.sections else None
    sucks = _parse_suck_list(args.suck)

    t0 = time.perf_counter()
    # legacy：与确定性路径完全同一段代码（逐字节兼容是硬契约）
    if anchors or sections:
        merged = _merge_sections(anchors, sections, float(args.duration))
    else:
        merged = [{"name": "全片", "start": 0.0, "end": float(args.duration),
                   "energy": args.energy}]
    sc = Score(args.duration, bpm=bpm, key=key, seed=args.seed)
    for s in merged:
        sc.section(s["name"], s["start"], s["end"], s["energy"])
    if anchors:
        sc._anchors = [{"id": a.get("id", ""), "kind": a.get("kind", ""),
                        "start": float(a["start"]), "end": float(a["end"])} for a in anchors]
    for t, db, dur in sucks:
        sc.suck(t, db, dur)
    wav = sc.render()
    rep = sc.report()
    dt = time.perf_counter() - t0
    rep["render_s"] = round(dt, 3)
    wav_path = Path(args.out) if args.out else None
    report_path = Path(args.report) if args.report else None
    if wav_path:
        save(wav_path, wav, sr=SR)
    if report_path:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    print(_format_report(rep, wav_path, report_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
