# -*- coding: utf-8 -*-
"""musicgen.py — P2 参数化配乐编排器（纯 numpy + stdlib，确定性、可单独 import）。

配乐不由 style id 决定（本仓库没有曲风表），而是由一组可采样参数现场编排：

    render(duration, *, bpm=96.0, key='Dm', scale='minor', mood='steady',
           seed=0, sections=None, progressions=None, motif=None,
           drums=None, voices=None, energy_curve=None) -> (samples, report)

  * progressions  {tier: [级数, ...]}       和声进行（calm/body/drive[/climax]）
  * motif         [[拍, 级数, 时值], ...]    旋律骨架（级数相对主音）
  * drums         {tier: {kick/hat/clap}}   鼓型（拍位置列表；hat 也接受步长标量）
  * voices        {pad/bass/arp/lead: 音色}  六种合成：saw/sine/square/triangle/bell/pluck
  * sections      [{start,name,energy}]      段落（决定层数与电平）
  * energy_curve  数字列表 / [[秒, 0-1], ...] 覆盖能量曲线

确定性：不使用 random / time / os.urandom；所有随机量来自 numpy.random.RandomState，
种子由 seed + 用途经 sha1 派生。同一组参数两次运行逐采样一致。

音频契约：48 kHz、立体声 (N,2) float32、时长 duration ±1 采样、无 NaN/Inf、
峰值 -6 dBFS。母带响度（loudnorm）留给下游。

本模块不 import scorekit / filmkit / PIL，可单独拷走运行。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import wave
from pathlib import Path

import numpy as np

SR = 48000
PEAK_DBFS = -6.0
TARGET_PEAK = 10.0 ** (PEAK_DBFS / 20.0)

VOICE_IDS = ("saw", "sine", "square", "triangle", "bell", "pluck")
VOICE_SLOTS = ("pad", "bass", "arp", "lead")
MOOD_IDS = ("steady", "calm", "reflective", "hopeful", "uplifting", "driving", "tense")
MOOD_ALIASES = {
    "drive": "driving", "dark": "tense", "epic": "uplifting", "warm": "hopeful",
    "soft": "calm", "minimal": "calm", "playful": "uplifting", "bright": "uplifting",
    "sad": "reflective", "energetic": "driving", "calm-down": "calm",
}
SCALE_IDS = ("minor", "major", "dorian", "phrygian", "lydian", "mixolydian", "harmonic-minor")
SCALE_ALIASES = {
    "min": "minor", "natural-minor": "minor", "aeolian": "minor",
    "maj": "major", "ionian": "major",
    "harmonic": "harmonic-minor", "harmonic_minor": "harmonic-minor",
}
TIER_IDS = ("calm", "body", "drive", "climax")

_SCALE_STEPS = {
    "major": (0, 2, 4, 5, 7, 9, 11),
    "minor": (0, 2, 3, 5, 7, 8, 10),
    "dorian": (0, 2, 3, 5, 7, 9, 10),
    "phrygian": (0, 1, 3, 5, 7, 8, 10),
    "lydian": (0, 2, 4, 6, 7, 9, 11),
    "mixolydian": (0, 2, 4, 5, 7, 9, 10),
    "harmonic-minor": (0, 2, 3, 5, 7, 8, 11),
}
_SCALE_CN = {
    "major": "大调", "minor": "小调", "dorian": "多利亚", "phrygian": "弗里几亚",
    "lydian": "利底亚", "mixolydian": "混合利底亚", "harmonic-minor": "和声小调",
}
_ROOT_SEMI = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
_PITCH_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
_ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7}
_ROMAN_TOKENS = ("I", "II", "III", "IV", "V", "VI", "VII",
                 "i", "ii", "iii", "iv", "v", "vi", "vii")

# 情绪档案：只调"亮度/层门限/电平/和声节奏"，不写死具体音符。
_MOOD = {
    "steady":     dict(bright=0.00, gate=0.00, lead_db=0.0, drum_db=0.0,
                       pad_gain=1.00, bass_gain=1.00, chord_bars=1,
                       energy_bias=0.00, swing=0.00),
    "calm":       dict(bright=-0.20, gate=0.08, lead_db=-1.5, drum_db=-3.0,
                       pad_gain=1.12, bass_gain=0.90, chord_bars=2,
                       energy_bias=-0.08, swing=0.00),
    "reflective": dict(bright=-0.12, gate=0.05, lead_db=-1.0, drum_db=-2.0,
                       pad_gain=1.08, bass_gain=0.94, chord_bars=2,
                       energy_bias=-0.04, swing=0.00),
    "hopeful":    dict(bright=0.05, gate=-0.02, lead_db=0.5, drum_db=0.0,
                       pad_gain=0.98, bass_gain=1.00, chord_bars=1,
                       energy_bias=0.02, swing=0.02),
    "uplifting":  dict(bright=0.14, gate=-0.05, lead_db=1.0, drum_db=1.0,
                       pad_gain=0.95, bass_gain=1.06, chord_bars=1,
                       energy_bias=0.06, swing=0.04),
    "driving":    dict(bright=0.20, gate=-0.07, lead_db=1.2, drum_db=2.0,
                       pad_gain=0.92, bass_gain=1.10, chord_bars=1,
                       energy_bias=0.08, swing=0.05),
    "tense":      dict(bright=0.02, gate=0.00, lead_db=0.5, drum_db=0.5,
                       pad_gain=1.00, bass_gain=1.02, chord_bars=1,
                       energy_bias=0.03, swing=0.00),
}

_DEFAULT_VOICES = {"pad": "saw", "bass": "sine", "arp": "pluck", "lead": "bell"}

# 音色不只换波形：不同"乐器"的自然音区/衰减/揉弦也不同。voices 因此真的改变编排。
_VOICE_TRAITS = {
    # pad_style 覆盖 seed 织体：不同"乐器"的自然奏法本来就不同（弦乐铺底/方波脉冲/钟琴点描）
    "saw":      dict(pad_oct=0,  arp_oct=0,  lead_oct=0,  pad_decay=None, bass_decay=None,
                     arp_decay=0.30, lead_decay=None, trem=0.00, vib=0.0000,
                     pad_style=None, bass_style="pulse", arp_step=None),
    "sine":     dict(pad_oct=0,  arp_oct=0,  lead_oct=0,  pad_decay=None, bass_decay=None,
                     arp_decay=None, lead_decay=None, trem=0.00, vib=0.0015,
                     pad_style="swell", bass_style=None, arp_step=None),
    "square":   dict(pad_oct=0,  arp_oct=0,  lead_oct=0,  pad_decay=None, bass_decay=0.35,
                     arp_decay=None, lead_decay=0.45, trem=0.25, vib=0.0000,
                     pad_style="pulse", bass_style="pulse", arp_step=0.25),
    "triangle": dict(pad_oct=0,  arp_oct=0,  lead_oct=0,  pad_decay=0.90, bass_decay=None,
                     arp_decay=0.55, lead_decay=None, trem=0.05, vib=0.0022,
                     pad_style="swell", bass_style="half", arp_step=0.25),
    "bell":     dict(pad_oct=12, arp_oct=12, lead_oct=0,  pad_decay=1.30, bass_decay=0.50,
                     arp_decay=1.10, lead_decay=1.30, trem=0.00, vib=0.0000,
                     pad_style="sustain", bass_style="whole", arp_step=0.25),
    "pluck":    dict(pad_oct=0,  arp_oct=0,  lead_oct=0,  pad_decay=0.50, bass_decay=0.18,
                     arp_decay=0.20, lead_decay=0.22, trem=0.00, vib=0.0000,
                     pad_style=None, bass_style=None, arp_step=0.5),
}


def _voice_trait(voice):
    return _VOICE_TRAITS[str(voice)]
_MOOD_VOICES = {
    "steady": {},
    "calm": {"pad": "triangle"},
    "reflective": {"pad": "triangle"},
    "hopeful": {},
    "uplifting": {"bass": "square"},
    "driving": {"bass": "square", "lead": "pluck"},
    "tense": {"pad": "square", "bass": "saw"},
}

# 缺省和声进行（按调式家族 + 情绪；用户 progressions 优先）。
_MINOR_PROGS = {
    "steady": dict(calm=("i", "VI", "iv", "v"), body=("i", "VI", "III", "VII"),
                   drive=("i", "VII", "VI", "V"), climax=("i", "VI", "III", "V")),
    "calm": dict(calm=("i", "iv", "VI", "v"), body=("i", "iv", "VII", "i"),
                 drive=("i", "VI", "iv", "v"), climax=("i", "iv", "VI", "V")),
    "reflective": dict(calm=("i", "VI", "iv", "i"), body=("i", "iv", "VI", "VII"),
                       drive=("i", "VI", "III", "i"), climax=("i", "iv", "VI", "V")),
    "hopeful": dict(calm=("i", "III", "VI", "v"), body=("i", "III", "VI", "VII"),
                    drive=("i", "VI", "III", "VII"), climax=("i", "III", "VI", "V")),
    "uplifting": dict(calm=("i", "VI", "iv", "v"), body=("i", "VI", "III", "VII"),
                      drive=("i", "V", "VI", "VII"), climax=("i", "VI", "III", "V")),
    "driving": dict(calm=("i", "i", "VII", "VI"), body=("i", "VII", "VI", "V"),
                    drive=("i", "VII", "VI", "V"), climax=("i", "VII", "V", "V")),
    "tense": dict(calm=("i", "bII", "iv", "i"), body=("i", "bII", "iv", "V"),
                  drive=("i", "bII", "V", "i"), climax=("i", "bII", "iv", "V")),
}
_MAJOR_PROGS = {
    "steady": dict(calm=("Imaj7", "vi7", "ii7", "V7"), body=("I", "V", "vi", "IV"),
                   drive=("I", "IV", "V", "vi"), climax=("I", "IV", "V", "V")),
    "calm": dict(calm=("Imaj7", "vi7", "ii7", "V7"), body=("I", "vi", "IV", "V"),
                 drive=("I", "IV", "V", "vi"), climax=("I", "IV", "V", "V")),
    "reflective": dict(calm=("I", "vi", "ii", "IV"), body=("I", "iii", "vi", "IV"),
                       drive=("I", "IV", "vi", "V"), climax=("I", "IV", "V", "V")),
    "hopeful": dict(calm=("I", "IV", "vi", "V"), body=("I", "IV", "V", "vi"),
                    drive=("I", "V", "vi", "IV"), climax=("I", "IV", "V", "V")),
    "uplifting": dict(calm=("I", "V", "vi", "IV"), body=("I", "V", "vi", "IV"),
                      drive=("I", "IV", "V", "vi"), climax=("I", "IV", "V", "V")),
    "driving": dict(calm=("I", "I", "IV", "V"), body=("I", "IV", "V", "vi"),
                    drive=("I", "IV", "V", "I"), climax=("I", "IV", "V", "I")),
    "tense": dict(calm=("I", "bII", "IV", "I"), body=("I", "bII", "IV", "V"),
                  drive=("I", "bII", "V", "I"), climax=("I", "bII", "IV", "V")),
}

# 缺省动机（8 拍乐句；级数相对主音）。
_MINOR_MOTIFS = {
    "steady": ((0.0, "i", 1.0), (1.5, "v", 1.0), (2.5, "III", 1.5),
               (4.0, "iv", 1.0), (5.5, "v", 1.0), (6.5, "III", 1.5)),
    "calm": ((0.0, "i", 2.0), (2.0, "III", 2.0), (4.0, "VI", 2.0), (6.0, "v", 1.5)),
    "reflective": ((0.0, "III", 1.5), (2.0, "v", 1.0), (3.0, "i", 2.0), (5.5, "VI", 1.5)),
    "hopeful": ((0.0, "i", 1.0), (1.0, "III", 1.0), (2.0, "VI", 1.5),
                (3.5, "v", 1.0), (5.0, "III", 1.0), (6.0, "i", 2.0)),
    "uplifting": ((0.0, "i", 0.75), (0.75, "III", 0.75), (1.5, "v", 0.75),
                  (2.25, "VI", 0.75), (3.0, "III", 1.0), (4.5, "v", 0.75),
                  (5.25, "i", 0.75), (6.0, "III", 0.75), (7.0, "v", 1.0)),
    "driving": ((0.0, "i", 0.5), (0.5, "i", 0.5), (1.0, "III", 0.5), (1.5, "v", 0.5),
                (2.0, "i", 0.5), (2.5, "i", 0.5), (3.0, "iv", 0.5), (3.5, "v", 0.5),
                (4.0, "i", 0.5), (4.5, "III", 0.5), (5.0, "VI", 0.5), (5.5, "v", 0.5),
                (6.0, "i", 0.5), (6.5, "v", 0.5), (7.0, "III", 1.0)),
    "tense": ((0.0, "i", 1.0), (1.0, "bII", 1.0), (2.0, "i", 1.0), (3.0, "V", 1.5),
              (5.0, "bII", 0.5), (5.5, "i", 0.5), (6.5, "V", 1.5)),
}
_MAJOR_MOTIFS = {
    "steady": ((0.0, "I", 1.0), (1.5, "V", 1.0), (2.5, "vi", 1.5),
               (4.0, "IV", 1.0), (5.5, "V", 1.0), (6.5, "I", 1.5)),
    "calm": ((0.0, "I", 2.0), (2.0, "vi", 2.0), (4.0, "IV", 2.0), (6.0, "V", 1.5)),
    "reflective": ((0.0, "vi", 1.5), (2.0, "IV", 1.0), (3.0, "I", 2.0), (5.5, "V", 1.5)),
    "hopeful": ((0.0, "I", 1.0), (1.0, "IV", 1.0), (2.0, "V", 1.5),
                (3.5, "vi", 1.0), (5.0, "IV", 1.0), (6.0, "I", 2.0)),
    "uplifting": ((0.0, "I", 0.75), (0.75, "V", 0.75), (1.5, "vi", 0.75),
                  (2.25, "IV", 0.75), (3.0, "V", 1.0), (4.5, "vi", 0.75),
                  (5.25, "I", 0.75), (6.0, "IV", 0.75), (7.0, "V", 1.0)),
    "driving": ((0.0, "I", 0.5), (0.5, "I", 0.5), (1.0, "IV", 0.5), (1.5, "V", 0.5),
                (2.0, "I", 0.5), (2.5, "I", 0.5), (3.0, "IV", 0.5), (3.5, "V", 0.5),
                (4.0, "I", 0.5), (4.5, "IV", 0.5), (5.0, "vi", 0.5), (5.5, "V", 0.5),
                (6.0, "I", 0.5), (6.5, "V", 0.5), (7.0, "IV", 1.0)),
    "tense": ((0.0, "I", 1.0), (1.0, "bII", 1.0), (2.0, "I", 1.0), (3.0, "V", 1.5),
              (5.0, "bII", 0.5), (5.5, "I", 0.5), (6.5, "V", 1.5)),
}

_DEGREE_RE = re.compile(r"^\s*([b#]?)(VII|VI|V|IV|III|II|I|vii|vi|v|iv|iii|ii|i)(.*)$")
_KEY_RE = re.compile(r"^\s*([A-Ga-g])([#b]?)(.*)$")


def _require(cond, msg):
    if not cond:
        raise ValueError(msg)


def _setup_stdio():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


def _clip01(x):
    return float(min(1.0, max(0.0, float(x))))


def _smoothstep(x):
    x = np.clip(x, 0.0, 1.0)
    return x * x * (3.0 - 2.0 * x)


def _db(x):
    return 10.0 ** (float(x) / 20.0)


def _sha_hex(*parts):
    raw = "|".join(str(p) for p in parts).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()


def _rng(*parts):
    """独立随机流：同 parts 必得同一 RandomState（sha1 派生，不依赖 PYTHONHASHSEED）。"""
    seed = int(_sha_hex(*parts)[:16], 16) % (2 ** 32 - 1)
    return np.random.RandomState(seed)


def _parse_key(key):
    """'Dm' / 'F#m' / 'Bb' / 'C' -> (chroma, minor_hint, 显示名, 根音拼写)。"""
    s = str(key).strip()
    m = _KEY_RE.match(s)
    _require(m is not None, "key 解析失败：%r（应形如 Dm / F#m / Bb / C）" % (key,))
    root = _ROOT_SEMI[m.group(1).upper()]
    acc = m.group(2)
    if acc == "#":
        root += 1
    elif acc == "b":
        root -= 1
    rest = m.group(3).strip().lower()
    _require(rest in ("", "m", "min", "minor", "maj", "major"),
             "key 后缀只支持 m/min/minor/maj/major，收到 %r" % (key,))
    minor_hint = rest.startswith("m") and not rest.startswith("maj")
    chroma = root % 12
    root_name = m.group(1).upper() + m.group(2)
    disp = root_name + ("m" if minor_hint else "")
    return chroma, bool(minor_hint), disp, root_name


def _normalize_scale(scale):
    s = str(scale).strip().lower().replace("_", "-").replace(" ", "-")
    s = SCALE_ALIASES.get(s, s)
    _require(s in SCALE_IDS, "未知调式 %r；可选：%s" % (scale, "/".join(SCALE_IDS)))
    return s


def _normalize_mood(mood):
    s = str(mood).strip().lower()
    s = MOOD_ALIASES.get(s, s)
    _require(s in MOOD_IDS, "未知情绪 %r；可选：%s" % (mood, "/".join(MOOD_IDS)))
    return s


def _scale_family(scale):
    return "major" if scale in ("major", "lydian", "mixolydian") else "minor"


def _default_progressions(scale, mood):
    table = _MAJOR_PROGS if _scale_family(scale) == "major" else _MINOR_PROGS
    return {t: tuple(table[mood][t]) for t in TIER_IDS}


def _default_motif(scale, mood):
    table = _MAJOR_MOTIFS if _scale_family(scale) == "major" else _MINOR_MOTIFS
    return [list(x) for x in table[mood]]


def _grid(step, limit=4.0):
    step = float(step)
    _require(step > 0.0 and math.isfinite(step), "hat 步长必须是有限正数，收到 %r" % (step,))
    out = []
    x = 0.0
    guard = 0
    while x < limit - 1e-9 and guard < 256:
        out.append(round(x, 4))
        x += step
        guard += 1
    return out


def _default_drums(mood):
    """按"情绪密度带 × tier"派生四档鼓型：同一 tier 在不同情绪下也不同密度。

    密度带只决定三档骨架（low/mid/high），tier 决定骨架内的位置组合；
    这样 12 s 短片的 calm/body/drive 不是同一张四踩底鼓表。
    """
    density = {
        "steady": 0.50, "calm": 0.18, "reflective": 0.26, "hopeful": 0.56,
        "uplifting": 0.68, "driving": 0.84, "tense": 0.60,
    }[mood]
    band = "low" if density < 0.50 else ("mid" if density < 0.68 else "high")
    skeletons = {
        "low": {
            "calm": ([], [], []),
            "body": ([0.0, 2.0], _grid(0.5), []),
            "drive": ([0.0, 2.0], _grid(0.5), []),
            "climax": ([0.0, 1.0, 2.0, 3.0], _grid(0.5), [1.0, 3.0]),
        },
        "mid": {
            "calm": ([0.0], [2.0], []),
            "body": ([0.0, 2.0], _grid(0.5), []),
            "drive": ([0.0, 1.0, 2.0, 3.0], _grid(0.5), [1.0, 3.0]),
            "climax": ([0.0, 0.75, 1.5, 2.0, 2.75, 3.5], _grid(0.25), [1.0, 3.0]),
        },
        "high": {
            "calm": ([0.0], [2.0], []),
            "body": ([0.0, 2.0], _grid(0.5), [1.0, 3.0]),
            "drive": ([0.0, 1.0, 2.0, 3.0], _grid(0.25), [1.0, 3.0]),
            "climax": ([0.0, 0.75, 1.5, 2.0, 2.75, 3.5], _grid(0.25), [1.0, 3.0]),
        },
    }
    out = {}
    for tier in TIER_IDS:
        kick, hat, clap = skeletons[band][tier]
        kick, hat, clap = list(kick), list(hat), list(clap)
        if mood == "tense" and tier in ("body", "drive", "climax"):
            kick = [0.0, 1.5, 2.0, 3.5]
            if tier != "body":
                hat = _grid(0.25)
        if mood in ("calm", "reflective"):
            hat = [x for x in hat if abs(x - round(x)) < 1e-9]
        out[tier] = {"kick": kick, "hat": hat, "clap": clap}
    return out


def _parse_degree(token):
    """解析罗马数字级数，返回 dict(acc, deg, upper, intervals, token)。

    大小写决定三和弦性质（i 小 / I 大），后缀可覆盖：
    maj7 / 7 / m7 / dim / dim7 / m7b5 / aug / sus4 / sus2 / add9 / 6。
    """
    s = str(token).strip()
    m = _DEGREE_RE.match(s)
    _require(m is not None, "级数解析失败：%r" % (token,))
    acc_s, roman, suffix_raw = m.group(1), m.group(2), m.group(3).strip()
    _require(roman in _ROMAN_TOKENS, "未知级数 %r（只支持 I-VII / i-vii）" % (token,))
    deg = _ROMAN[roman.upper()]
    upper = roman.isupper()
    acc = -1 if acc_s == "b" else (1 if acc_s == "#" else 0)
    suffix = suffix_raw.lower()
    if suffix_raw in ("M7", "Maj7", "Δ7"):
        suffix = "maj7"
    if suffix == "":
        iv = (0, 4, 7) if upper else (0, 3, 7)
    elif suffix in ("maj", "major"):
        iv = (0, 4, 7)
    elif suffix in ("m", "min", "minor"):
        iv = (0, 3, 7)
    elif suffix in ("dim", "o", "°"):
        iv = (0, 3, 6)
    elif suffix in ("aug", "+"):
        iv = (0, 4, 8)
    elif suffix == "7":
        iv = (0, 4, 7, 10) if upper else (0, 3, 7, 10)
    elif suffix == "maj7":
        iv = (0, 4, 7, 11)
    elif suffix == "m7":
        iv = (0, 3, 7, 10)
    elif suffix == "dim7":
        iv = (0, 3, 6, 9)
    elif suffix == "m7b5":
        iv = (0, 3, 6, 10)
    elif suffix == "sus4":
        iv = (0, 5, 7)
    elif suffix == "sus2":
        iv = (0, 2, 7)
    elif suffix == "add9":
        iv = (0, 4, 7, 14) if upper else (0, 3, 7, 14)
    elif suffix == "6":
        iv = (0, 4, 7, 9) if upper else (0, 3, 7, 9)
    else:
        raise ValueError("未知和弦后缀 %r（级数 %r）" % (suffix_raw, token))
    return {"acc": acc, "deg": deg, "upper": upper, "intervals": iv, "token": s}


def _midi_freq(midi):
    return 440.0 * (2.0 ** ((float(midi) - 69.0) / 12.0))


# ============================================================================
# 合成原语：波表振荡器（按音高限谐波防混叠）+ 包络 + 鼓
# ============================================================================
_TABLE_N = 2048
_TABLE_MASK = _TABLE_N - 1
_TABLE_CAPS = (1, 2, 4, 6, 8, 12, 16, 24, 32, 48)
_TABLES = {}


def _harmonic_table(voice, cap):
    """单周期波表：saw/square/triangle/pluck 都是加性合成，按 cap 限谐波。"""
    key = (voice, int(cap))
    tab = _TABLES.get(key)
    if tab is not None:
        return tab
    cap = max(1, int(cap))
    ph = np.arange(_TABLE_N, dtype=np.float64) / float(_TABLE_N)
    if voice == "sine":
        wave = np.sin(2.0 * math.pi * ph)
    elif voice == "saw":
        wave = np.zeros(_TABLE_N)
        for k in range(1, cap + 1):
            wave += (1.0 / k) * np.sin(2.0 * math.pi * k * ph)
    elif voice == "square":
        wave = np.zeros(_TABLE_N)
        for k in range(1, cap + 1, 2):
            wave += (1.0 / k) * np.sin(2.0 * math.pi * k * ph)
    elif voice == "triangle":
        wave = np.zeros(_TABLE_N)
        for k in range(1, cap + 1, 2):
            sign = 1.0 if ((k // 2) % 2 == 0) else -1.0
            wave += (sign / (k * k)) * np.sin(2.0 * math.pi * k * ph)
    elif voice == "pluck":
        wave = np.zeros(_TABLE_N)
        for k in range(1, cap + 1):
            weight = (1.0 if (k % 2) else 0.62) / (k ** 1.35)
            wave += weight * np.sin(2.0 * math.pi * k * ph)
    else:
        raise ValueError("未知音色 %r；可选：%s" % (voice, "/".join(VOICE_IDS)))
    wave -= float(wave.mean())
    peak = float(np.max(np.abs(wave)))
    if peak < 1e-9:
        peak = 1.0
    tab = (wave / peak).astype(np.float64)
    _TABLES[key] = tab
    return tab


def _cap_for_freq(freq, bright):
    """按 Nyquist 选谐波上限；bright>0 更亮，bright<0 更暗。"""
    limit = 0.45 * SR / max(float(freq), 1e-6)
    want = int(max(1.0, min(48.0, limit)))
    if bright > 0.0:
        want = int(want * (1.0 + 0.35 * bright))
    elif bright < 0.0:
        want = int(want * (1.0 + 0.60 * bright))
    want = max(1, min(48, want))
    chosen = 1
    for c in _TABLE_CAPS:
        if c <= want:
            chosen = c
    return chosen


def _table_osc(voice, freq, n, phase0, bright, vib=0.0, vib_rate=5.2):
    cap = _cap_for_freq(freq, bright)
    tab = _harmonic_table(voice, cap)
    t = np.arange(n, dtype=np.float64) / float(SR)
    cycles = float(phase0) + float(freq) * t
    if vib:
        cycles = cycles + float(vib) * np.sin(2.0 * math.pi * float(vib_rate) * t)
    idx = cycles * float(_TABLE_N)
    base = np.floor(idx)
    frac = idx - base
    i0 = base.astype(np.int64) & _TABLE_MASK
    i1 = (i0 + 1) & _TABLE_MASK
    return tab[i0] + (tab[i1] - tab[i0]) * frac


_BELL_PARTS = ((1.0, 1.0, 1.0), (2.0, 0.42, 1.55), (2.76, 0.28, 2.15),
               (4.07, 0.14, 2.90), (5.43, 0.08, 3.70))


def _bell_signal(freq, n, phase0, decay):
    t = np.arange(n, dtype=np.float64) / float(SR)
    sig = np.zeros(n, dtype=np.float64)
    dcy = max(0.08, float(decay))
    for ratio, amp, mult in _BELL_PARTS:
        sig += amp * np.sin(2.0 * math.pi * float(freq) * ratio * t + float(phase0) * ratio) \
            * np.exp(-t / (dcy * mult))
    return sig / 1.9


def _note_signal(voice, freq, n, phase0, bright, decay, detune_cents, vib=0.0):
    if voice == "bell":
        return _bell_signal(freq, n, phase0, decay)
    if detune_cents and float(detune_cents) > 0.0:
        d = float(detune_cents) / 1200.0
        a = _table_osc(voice, freq * (2.0 ** d), n, phase0, bright, vib)
        b = _table_osc(voice, freq * (2.0 ** (-d)), n, phase0 + 0.37, bright, vib, 5.9)
        return 0.5 * (a + b)
    return _table_osc(voice, freq, n, phase0, bright, vib)


def _env_curve(n, attack, release, decay=None):
    t = np.arange(n, dtype=np.float64) / float(SR)
    env = np.ones(n, dtype=np.float64)
    if decay is not None and float(decay) > 0.0:
        env *= np.exp(-t / float(decay))
    na = int(round(max(0.0, float(attack)) * SR))
    na = min(n, max(1, na))
    if na > 2:
        env[:na] *= 0.5 - 0.5 * np.cos(np.linspace(0.0, math.pi, na))
    nr = int(round(max(0.0, float(release)) * SR))
    nr = min(n, max(1, nr))
    if nr > 2:
        env[-nr:] *= 0.5 + 0.5 * np.cos(np.linspace(0.0, math.pi, nr))
    return env


def _pan_gains(pan):
    p = max(-1.0, min(1.0, float(pan)))
    ang = (p + 1.0) * (math.pi / 4.0)
    return math.cos(ang), math.sin(ang)


def _add_mono(buf, start, sig, pan=0.0):
    s = int(round(float(start) * SR))
    if s >= len(buf):
        return 0
    if s < 0:
        sig = sig[-s:]
        s = 0
    m = min(len(sig), len(buf) - s)
    if m <= 0:
        return 0
    g_l, g_r = _pan_gains(pan)
    buf[s:s + m, 0] += (sig[:m] * g_l).astype(np.float32)
    buf[s:s + m, 1] += (sig[:m] * g_r).astype(np.float32)
    return m


def _render_note(buf, start, dur, midi, voice, amp, pan, attack, release,
                 decay=None, bright=0.0, detune=0.0, rng=None, vib=0.0):
    n = int(round(max(0.0, float(dur)) * SR))
    if n < 16:
        return 0
    phase0 = float(rng.rand()) if rng is not None else 0.13
    sig = _note_signal(voice, _midi_freq(midi), n, phase0, bright, decay, detune, vib)
    # bell 的衰减已经写在各分音里，包络不再乘一次 decay，否则尾音被砍半
    sig *= _env_curve(n, attack, release, None if voice == "bell" else decay)
    sig *= float(amp)
    return _add_mono(buf, start, sig, pan)


# ---------------------------------------------------------------- 鼓组
def _render_kick(buf, t, vel, rng):
    n = int(0.52 * SR)
    tt = np.arange(n, dtype=np.float64) / float(SR)
    freq = 43.0 + 105.0 * np.exp(-tt / 0.035)
    phase = 2.0 * math.pi * np.cumsum(freq) / float(SR)
    body = np.sin(phase) * np.exp(-tt / 0.20)
    click = rng.normal(0.0, 1.0, n) * np.exp(-tt / 0.0035) * 0.30
    sig = np.tanh(1.5 * (body + click)) * 0.92
    _add_mono(buf, t, sig * float(vel), 0.0)


def _render_hat(buf, t, vel, rng, open_hat=False):
    n = int((0.30 if open_hat else 0.10) * SR)
    tt = np.arange(n, dtype=np.float64) / float(SR)
    noise = rng.normal(0.0, 1.0, n)
    hf = np.diff(noise, prepend=0.0)
    hf /= (float(np.std(hf)) + 1e-9)
    env = np.exp(-tt / (0.13 if open_hat else 0.034))
    sig = hf * env * 0.30
    _add_mono(buf, t, sig * float(vel), 0.28 if open_hat else 0.36)


def _render_clap(buf, t, vel, rng, fill=False):
    n = int(0.20 * SR)
    tt = np.arange(n, dtype=np.float64) / float(SR)
    noise = rng.normal(0.0, 1.0, n)
    bp = np.diff(np.diff(noise, prepend=0.0), prepend=0.0)
    bp /= (float(np.std(bp)) + 1e-9)
    env = np.zeros(n, dtype=np.float64)
    for k in (0.0, 0.011, 0.022):
        i = int(k * SR)
        if i < n:
            env[i:] += np.exp(-(tt[i:] - k) / (0.008 if fill else 0.012))
    sig = bp * env * (0.24 if fill else 0.34)
    _add_mono(buf, t, sig * float(vel), -0.12)


# ============================================================================
# 参数归一化：段落 / 和声 / 动机 / 鼓型 / 音色 / 能量曲线
# ============================================================================
def _default_sections(duration, mood, bpm=96.0):
    """默认段落：**按小节切**，不按时长比例切。

    为什么必须对齐小节：tier 是按小节取的（一小节一个 tier，见 _build_plan），
    而段落原本按时长分数切。两套网格对不齐时，峰值段可能整段落在两个小节中心之间，
    **climax 永远触发不了** —— 实测 8 s / 96 BPM 就是这种情况（中心 1.25/3.75/6.25/8.75，
    peak 段 4.16–6.08，三个中心全在窗外），调用方传 {"climax": [...]} 完全无效。
    改成按小节切之后，每段天然覆盖整数个小节，climax 一定落在至少一个完整小节里。
    """
    bias = _MOOD[mood]["energy_bias"]
    dur = float(duration)
    bar_s = 4.0 * (60.0 / float(bpm))
    nbars = max(1, int(math.ceil(dur / bar_s - 1e-9)))

    def _bar_fracs(weights):
        """把"每段占几小节"的权重组，按**小节边界**转成时间分数。

        分配规则：先按权重比例算累计小节数并四舍五入，再强制"每段至少 1 小节"、
        总和等于 nbars —— 否则末段会不足一小节，它的 probe 窗口被邻居覆盖（见上）。
        """
        if len(weights) == 1:
            return [0.0]
        total = float(sum(weights))
        raw = [float(w) / total * nbars for w in weights]
        bars = [max(1, int(round(x))) for x in raw]
        # 修正总数：多了从最长的段减、少了给最长的段加（保持相对比例）
        while sum(bars) > nbars:
            i = max(range(len(bars)), key=lambda k: bars[k])
            if bars[i] <= 1:
                break
            bars[i] -= 1
        while sum(bars) < nbars:
            i = max(range(len(bars)), key=lambda k: raw[k])
            bars[i] += 1
        out, acc = [0.0], 0
        for b in bars[:-1]:
            acc += b
            out.append(min(1.0, max(0.0, acc * bar_s / dur)))
        return out
    # 段数按"总小节数"定，权重按"每段占几小节"给 —— 这样切点天然落在小节边界。
    # peak 段必须是一整个小节（0.90 > _tier_for 的 0.88 阈值），且它左右各留至少一小节，
    # 否则高潮会被相邻段吃掉或被收尾段压平。
    if nbars <= 1:
        weights, names, energies = (1.0,), ("全片",), (0.55,)
    elif nbars == 2:
        # 两小节片（约 5–7 s）：没有空间再放收尾段。peak 落在最后一小节是**有意的**，
        # 由末尾 fade-out 收住，不硬塞一个只有零点几秒的 outro（那种 outro 的能量
        # 探测窗口还会被前一小节的高潮污染，反而判成 climax）。
        weights, names, energies = (1.0, 1.0), ("build", "peak"), (0.55, 0.90)
    elif nbars == 3:
        weights, names, energies = (1.0, 1.0, 1.0), ("intro", "peak", "outro"), (0.45, 0.90, 0.30)
    elif nbars == 4:
        # 4 小节：outro 只占 1 小节，peak 也 1 小节。
        weights, names, energies = (1.0, 1.0, 1.0, 1.0), ("intro", "build", "peak", "outro"), (0.30, 0.52, 0.90, 0.30)
    elif nbars <= 7:
        weights, names, energies = (1.0, 1.0, 1.0, 1.0, 1.0), ("intro", "build", "body", "peak", "outro"), (0.24, 0.42, 0.58, 0.90, 0.30)
    else:
        # 权重之和必须**等于 nbars**：每段至少占满一个小节。
        # 早先 body 用 nbars-4、末尾再叠 1 小节 outro，权重和会超出 nbars，
        # 于是最后一段被压到不足一小节 —— 它的探测窗口被前一整小节的 peak 覆盖，
        # 收尾小节被判成 climax（实测 8 s / 96 BPM：outro 只有 0.5 s）。
        body_bars = max(1, nbars - 4)
        peak_bars = 1
        weights = (1.0, 1.0, float(body_bars), float(peak_bars), 1.0)
        names = ("intro", "build", "body", "peak", "outro")
        energies = (0.24, 0.42, 0.58, 0.90, 0.30)
    fracs = _bar_fracs(weights)
    out = []
    for i, name in enumerate(names):
        start = 0.0 if i == 0 else min(dur, fracs[i] * dur)
        out.append({"name": name, "start": start, "energy": _clip01(energies[i] + bias)})
    for i, item in enumerate(out):
        item["end"] = out[i + 1]["start"] if i + 1 < len(out) else dur
    return out


def _normalize_sections(duration, sections, mood, bpm=96.0):
    dur = float(duration)
    if not sections:
        return _default_sections(dur, mood, bpm)
    _require(isinstance(sections, (list, tuple)) and len(sections) > 0,
             "sections 必须是非空列表：[{start,name,energy}, ...]")
    parsed = []
    for i, s in enumerate(sections):
        _require(isinstance(s, dict), "sections[%d] 必须是 dict，收到 %r" % (i, type(s).__name__))
        try:
            start = float(s.get("start", 0.0))
            energy = float(s.get("energy", 0.5))
        except (TypeError, ValueError):
            raise ValueError("sections[%d] 的 start/energy 必须是数字：%r" % (i, s)) from None
        _require(math.isfinite(start) and math.isfinite(energy),
                 "sections[%d] 的 start/energy 必须是有限数" % i)
        name = str(s.get("name") or ("sec%d" % (i + 1))).strip() or ("sec%d" % (i + 1))
        parsed.append({"name": name, "start": max(0.0, min(dur, start)),
                       "energy": _clip01(energy)})
    parsed.sort(key=lambda x: x["start"])
    uniq = []
    for item in parsed:
        if uniq and abs(item["start"] - uniq[-1]["start"]) < 1e-6:
            uniq[-1]["energy"] = max(uniq[-1]["energy"], item["energy"])
        else:
            uniq.append(item)
    uniq = [x for x in uniq if x["start"] < dur - 1e-9]
    _require(bool(uniq), "sections 全部落在片长之外（%.3f s）" % dur)
    if uniq[0]["start"] > 1e-6:
        uniq.insert(0, {"name": "intro", "start": 0.0,
                        "energy": _clip01(uniq[0]["energy"] * 0.75)})
    uniq[0]["start"] = 0.0
    for i, item in enumerate(uniq):
        end = uniq[i + 1]["start"] if i + 1 < len(uniq) else dur
        item["end"] = max(item["start"], min(dur, float(end)))
    return uniq


def _normalize_progressions(user, scale, mood):
    defaults = _default_progressions(scale, mood)
    if user is None:
        return {t: list(defaults[t]) for t in TIER_IDS}
    if isinstance(user, (list, tuple)):
        user = {t: list(user) for t in TIER_IDS}
    _require(isinstance(user, dict),
             "progressions 必须是 {tier: [级数,...]} 或一个级数列表，收到 %r"
             % (type(user).__name__,))
    out = {}
    for tier in TIER_IDS:
        val = user.get(tier)
        if val is None:
            out[tier] = list(defaults[tier])
            continue
        _require(not isinstance(val, str),
                 "progressions[%r] 必须是级数列表（单个数也请写成 ['i']），收到 %r"
                 % (tier, val))
        _require(isinstance(val, (list, tuple)) and len(val) > 0,
                 "progressions[%r] 必须是非空级数列表" % (tier,))
        toks = []
        for tok in val:
            d = _parse_degree(tok)
            toks.append(d["token"])
        out[tier] = toks
    return out


def _normalize_motif(user, scale, mood):
    if user is None:
        return [list(x) for x in _default_motif(scale, mood)]
    _require(isinstance(user, (list, tuple)) and len(user) > 0,
             "motif 必须是非空列表：[[拍, 级数, 时值], ...]")
    out = []
    for i, item in enumerate(user):
        _require(isinstance(item, (list, tuple)) and len(item) >= 3,
                 "motif[%d] 必须是 [拍, 级数, 时值] 三元组：%r" % (i, item))
        try:
            beat = float(item[0])
            dur = float(item[2])
        except (TypeError, ValueError):
            raise ValueError("motif[%d] 的拍/时值必须是数字：%r" % (i, item)) from None
        _require(math.isfinite(beat) and math.isfinite(dur), "motif[%d] 拍/时值必须有限" % i)
        _require(beat >= 0.0, "motif[%d] 的拍不能为负：%r" % (i, beat))
        _require(dur > 0.0, "motif[%d] 的时值必须 > 0：%r" % (i, dur))
        d = _parse_degree(item[1])
        out.append([round(beat, 6), d["token"], round(dur, 6)])
    out.sort(key=lambda x: x[0])
    return out


def _drum_positions(val, key):
    if val is None or val is False:
        return []
    if isinstance(val, (int, float, np.integer, np.floating)):
        v = float(val)
        _require(math.isfinite(v) and v >= 0.0, "%s 的拍位置必须是有限非负数：%r" % (key, val))
        if key == "hat" and (v in (0.25, 0.5, 1.0, 2.0) or 0.0 < v < 1.0):
            return _grid(v)
        return [round(v % 4.0, 4)]
    _require(isinstance(val, (list, tuple)), "%s 必须是拍位置列表，收到 %r" % (key, val))
    out = []
    for x in val:
        try:
            v = float(x)
        except (TypeError, ValueError):
            raise ValueError("%s 的拍位置必须是数字，收到 %r" % (key, x)) from None
        _require(math.isfinite(v) and v >= 0.0, "%s 的拍位置必须是有限非负数：%r" % (key, x))
        out.append(round(v % 4.0, 4))
    uniq = []
    for x in sorted(out):
        if not uniq or abs(x - uniq[-1]) > 1e-6:
            uniq.append(x)
    return uniq[:64]


def _normalize_drums(user, mood):
    defaults = _default_drums(mood)
    if user is None:
        return {t: {k: list(defaults[t][k]) for k in ("kick", "hat", "clap")} for t in TIER_IDS}
    _require(isinstance(user, dict), "drums 必须是 {tier: {kick/hat/clap}} 字典")
    out = {}
    for tier in TIER_IDS:
        spec = user.get(tier, {})
        if spec is None:
            spec = {}
        _require(isinstance(spec, dict), "drums[%r] 必须是 dict" % (tier,))
        extra = [k for k in spec if k not in ("kick", "hat", "clap")]
        _require(not extra, "drums[%r] 出现未知键 %s（只支持 kick/hat/clap）"
                 % (tier, "/".join(str(k) for k in extra)))
        out[tier] = {}
        for k in ("kick", "hat", "clap"):
            if k in spec:
                out[tier][k] = _drum_positions(spec[k], k)
            else:
                out[tier][k] = list(defaults[tier][k])
    return out


def _normalize_voices(user, mood):
    merged = dict(_DEFAULT_VOICES)
    merged.update(_MOOD_VOICES[mood])
    if user is None:
        return merged
    _require(isinstance(user, dict), "voices 必须是 {pad/bass/arp/lead: 音色} 字典")
    for slot, val in user.items():
        slot_l = str(slot).strip().lower()
        _require(slot_l in VOICE_SLOTS, "未知音色槽 %r；可选：%s" % (slot, "/".join(VOICE_SLOTS)))
        v = str(val).strip().lower()
        _require(v in VOICE_IDS, "未知音色 %r；可选：%s" % (val, "/".join(VOICE_IDS)))
        merged[slot_l] = v
    return merged


def _energy_from_user(energy_curve, duration):
    """把 energy_curve 归一化成 (xs, ys)；None 时返回 None。"""
    if energy_curve is None:
        return None
    dur = float(duration)
    points = energy_curve
    if isinstance(points, dict):
        points = points.get("points", points.get("curve"))
    _require(points is not None, "energy_curve 缺少 points/curve")
    if isinstance(points, np.ndarray):
        points = points.tolist()
    _require(isinstance(points, (list, tuple)) and len(points) > 0,
             "energy_curve 必须是非空列表")
    if all(isinstance(x, (int, float, np.integer, np.floating)) for x in points):
        for x in points:
            _require(math.isfinite(float(x)), "energy_curve 的值必须是有限数：%r" % (x,))
        ys = np.array([_clip01(x) for x in points], dtype=np.float64)
        if len(ys) == 1:
            return np.array([0.0, dur]), np.array([ys[0], ys[0]])
        xs = np.linspace(0.0, dur, len(ys))
        return xs, ys
    pairs = []
    for p in points:
        _require(isinstance(p, (list, tuple)) and len(p) >= 2,
                 "energy_curve 的每个点必须是 [秒, 0-1]：%r" % (p,))
        try:
            t = float(p[0])
            e = float(p[1])
        except (TypeError, ValueError):
            raise ValueError("energy_curve 点必须是数字：%r" % (p,)) from None
        _require(math.isfinite(t) and math.isfinite(e), "energy_curve 点必须是有限数：%r" % (p,))
        pairs.append((min(dur, max(0.0, t)), _clip01(e)))
    pairs.sort()
    uniq = []
    for t, e in pairs:
        if uniq and abs(t - uniq[-1][0]) < 1e-9:
            uniq[-1] = (t, e)
        else:
            uniq.append((t, e))
    if uniq[0][0] > 1e-9:
        uniq.insert(0, (0.0, uniq[0][1]))
    else:
        uniq[0] = (0.0, uniq[0][1])
    if uniq[-1][0] < dur - 1e-9:
        uniq.append((dur, uniq[-1][1]))
    else:
        uniq[-1] = (dur, uniq[-1][1])
    xs = np.array([x[0] for x in uniq], dtype=np.float64)
    ys = np.array([x[1] for x in uniq], dtype=np.float64)
    return xs, ys


def _seed_variants(seed, mood):
    """seed 不是"加一点噪声"，而是选择编排型：音区/织体/琶音/动机变奏/贝斯型。"""
    s = int(seed)
    pad_styles = ("sustain", "pulse", "swell")
    pad_style = pad_styles[(s // 2) % 3]
    pad_register = 12 if (s % 2 == 0) else 0
    arp_patterns = ((0, 1, 2, 3), (0, 2, 1, 3), (0, 1, 2, 3, 2, 1),
                    (0, 2, 3, 1), (3, 2, 1, 0), (0, 1, 2, 1))
    arp_pattern = arp_patterns[(s // 3) % len(arp_patterns)]
    lead_variant = s % 4
    bass_variant = (s // 4) % 2
    if mood in ("calm", "reflective") and pad_style == "pulse":
        pad_style = "swell"
    return {
        "pad_style": pad_style,
        "pad_register": pad_register,
        "arp_pattern": list(arp_pattern),
        "lead_variant": lead_variant,
        "bass_variant": bass_variant,
    }


def _tier_for(energy):
    e = float(energy)
    if e < 0.40:
        return "calm"
    if e < 0.70:
        return "body"
    if e < 0.88:
        return "drive"
    return "climax"


def _layer_thresholds(mood):
    g = float(_MOOD[mood]["gate"])
    return {
        "bass": 0.06 + g,
        "kick": 0.28 + g,
        "arp": 0.34 + g,
        "hat": 0.40 + g,
        "lead": 0.44 + g,
        "clap": 0.54 + g,
    }


def _layers_for_energy(energy, mood):
    e = float(energy)
    thr = _layer_thresholds(mood)
    out = ["pad"]
    for name in ("bass", "kick", "arp", "hat", "lead", "clap"):
        if e >= thr[name]:
            out.append(name)
    return out


def _build_plan(duration, bpm, key, scale, mood, seed, sections, progressions,
                motif, drums, voices, energy_curve):
    try:
        dur = float(duration)
    except (TypeError, ValueError):
        raise ValueError("duration 必须是数字，收到 %r" % (duration,)) from None
    _require(math.isfinite(dur) and dur > 0.0, "duration 必须是有限正数，收到 %r" % (duration,))
    if bpm is None:
        bpm = 96.0
    if key is None:
        key = "Dm"
    if scale is None:
        scale = "minor"
    if mood is None:
        mood = "steady"
    try:
        bpm_f = float(bpm)
    except (TypeError, ValueError):
        raise ValueError("bpm 必须是数字，收到 %r" % (bpm,)) from None
    _require(math.isfinite(bpm_f) and bpm_f > 0.0, "bpm 必须是有限正数，收到 %r" % (bpm,))
    try:
        seed_i = 0 if seed is None else abs(int(seed))
    except (TypeError, ValueError):
        raise ValueError("seed 必须是整数，收到 %r" % (seed,)) from None
    key_chroma, _minor_hint, key_disp, key_root = _parse_key(key)
    scale_s = _normalize_scale(scale)
    mood_s = _normalize_mood(mood)
    scale_iv = _SCALE_STEPS[scale_s]

    secs = _normalize_sections(dur, sections, mood_s, bpm_f)
    progs = _normalize_progressions(progressions, scale_s, mood_s)
    motif_n = _normalize_motif(motif, scale_s, mood_s)
    drums_n = _normalize_drums(drums, mood_s)
    voices_n = _normalize_voices(voices, mood_s)

    # 段落能量是"阶梯保持 + 边界前短斜坡"：段落中段必须维持本段能量，
    # 否则长段落会在后半段被下一段的低能量拖下去（lift 反而不如 body 响）。
    pts = [(0.0, secs[0]["energy"])]
    for i, s in enumerate(secs):
        s_start = float(s["start"])
        s_end = float(s["end"])
        pts.append((s_start, s["energy"]))
        if i + 1 < len(secs):
            ramp = max(0.05, min(0.50, (s_end - s_start) * 0.15))
            hold = max(s_start, s_end - ramp)
            pts.append((hold, s["energy"]))
        else:
            pts.append((dur, s["energy"]))
    xs_list, ys_list = [], []
    for x, y in pts:
        x = min(dur, max(0.0, float(x)))
        if xs_list and x - xs_list[-1] <= 1e-9:
            ys_list[-1] = float(y)
        else:
            xs_list.append(x)
            ys_list.append(float(y))
    if xs_list[0] > 1e-9:
        xs_list.insert(0, 0.0)
        ys_list.insert(0, ys_list[0])
    if xs_list[-1] < dur - 1e-9:
        xs_list.append(dur)
        ys_list.append(ys_list[-1])
    xs = np.array(xs_list, dtype=np.float64)
    ys = np.array(ys_list, dtype=np.float64)
    user_curve = _energy_from_user(energy_curve, dur)
    if user_curve is not None:
        xs, ys = user_curve

    n = max(1, int(round(dur * SR)))
    beat_s = 60.0 / bpm_f
    bar_s = 4.0 * beat_s
    nbars = max(1, int(math.ceil(dur / bar_s - 1e-9)))
    centers = np.array([max(0.0, min(dur - 1e-9, (b + 0.5) * bar_s)) for b in range(nbars)],
                       dtype=np.float64)
    # 一小节的能量取"小节内最响的时刻"，窗口 **±0.20 小节**（约半小节）。
    #
    # 取舍：① 只取中心点 -> 段落与小节网格错位时 climax 永远触发不了；
    #       ② 窗口放宽到 ±0.42 几乎盖满整小节 -> 收尾小节被前一节的高潮污染
    #          （实测 8 s / 96 BPM 的 outro 被判成 climax）。
    # ±0.20 是两头都照顾的取值：段切在中心附近偏差 ≤0.2 小节能被捕获，
    # 又不会让相邻段把尾巴拖进来。
    #
    # 末尾小节会被片长截断，此时实际跨度可能远小于整小节（8 s / 96 BPM 的最后一小节
    # 只剩 0.5 s），探测点会全挤在片尾同一个值上。跨度过小时退化为"中心点单点"，
    # 语义更诚实：这一小节本来就没多少内容。
    _half = 0.20 * bar_s
    _deltas = np.linspace(-_half, _half, 5)
    _point = np.zeros((len(centers), len(_deltas)), dtype=np.float64)
    for _b in range(len(centers)):
        for _k, _d in enumerate(_deltas):
            _point[_b, _k] = min(dur - 1e-9, max(0.0, centers[_b] + _d))
    bar_energy = np.max(np.interp(_point.ravel(), xs, ys).reshape(_point.shape), axis=1)
    bar_tier = [_tier_for(e) for e in bar_energy]

    chord_bars = int(_MOOD[mood_s]["chord_bars"])
    # 全局小节位置推进和声（而不是每个 tier 重置）：12 s 里 body 的第二小节
    # 能接上 III，drive 小节能落在 V，短片的和声进行才不是 i–VI 两个和弦打转。
    bar_chord = []
    for b in range(nbars):
        p = progs[bar_tier[b]]
        bar_chord.append(p[(b // chord_bars) % len(p)])

    variation = _seed_variants(seed_i, mood_s)
    variation.update({"fills": 0, "ghosts": 0, "skipped_lead": 0,
                      "chord_changes": 0, "grace_notes": 0})
    for b in range(1, nbars):
        if bar_chord[b] != bar_chord[b - 1]:
            variation["chord_changes"] += 1

    max_beat = max(float(m[0]) + float(m[2]) for m in motif_n)
    phrase_beats = max(8, int(math.ceil((max_beat - 1e-9) / 4.0)) * 4)
    key_name = "%s %s" % (key_root, _SCALE_CN[scale_s])

    payload = {
        "version": 1,
        "duration": round(dur, 6),
        "bpm": round(bpm_f, 6),
        "key": key_disp,
        "scale": scale_s,
        "mood": mood_s,
        "seed": seed_i,
        "sections": [[s["name"], round(s["start"], 6), round(s["end"], 6),
                      round(s["energy"], 6)] for s in secs],
        "progressions": progs,
        "motif": motif_n,
        "drums": drums_n,
        "voices": voices_n,
        "energy": [[round(float(x), 6), round(float(y), 6)] for x, y in zip(xs, ys)],
        "variation": variation,
    }
    fp = _sha_hex(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":")))[:12]

    return {
        "duration": dur,
        "n": n,
        "sr": SR,
        "bpm": bpm_f,
        "beat_s": beat_s,
        "bar_s": bar_s,
        "nbars": nbars,
        "key": key_disp,
        "key_name": key_name,
        "key_chroma": key_chroma,
        "scale": scale_s,
        "scale_iv": scale_iv,
        "mood": mood_s,
        "seed": seed_i,
        "sections": secs,
        "energy_x": xs,
        "energy_y": ys,
        "bar_centers": centers,
        "bar_energy": bar_energy,
        "bar_tier": bar_tier,
        "bar_chord": bar_chord,
        "progressions": progs,
        "motif": motif_n,
        "phrase_beats": phrase_beats,
        "drums": drums_n,
        "voices": voices_n,
        "variation": variation,
        "fingerprint": fp,
    }


def _chord_midis(token, key_chroma, scale_iv, base=60, inversion=0,
                  register=0, want7=False):
    d = _parse_degree(token)
    root = base + key_chroma + scale_iv[d["deg"] - 1] + d["acc"] + int(register)
    notes = [root + iv for iv in d["intervals"]]
    if want7 and len(notes) == 3:
        notes.append(root + 10)
    inv = int(inversion) % max(1, len(notes))
    for k in range(inv):
        notes[k] += 12
    notes.sort()
    while notes[0] < 40:
        notes = [x + 12 for x in notes]
    while notes[-1] > 88:
        notes = [x - 12 for x in notes]
    return notes


def _bass_pattern(tier, variant):
    if tier == "calm":
        return [(0.0, 0, 3.6, 0.85)]
    if tier == "body":
        if variant:
            return [(0.0, 0, 1.2, 1.0), (1.75, 0, 0.8, 0.70), (2.5, 0, 1.3, 0.85)]
        return [(0.0, 0, 1.7, 1.0), (2.0, 0, 1.7, 0.78)]
    if tier == "drive":
        if variant:
            return [(0.0, 0, 0.42, 1.0), (0.75, 0, 0.42, 0.70),
                    (1.5, 1, 0.42, 0.82), (2.0, 0, 0.42, 0.95),
                    (2.75, 2, 0.42, 0.68), (3.5, 1, 0.42, 0.82)]
        return [(k * 0.5, (1 if k in (3, 7) else 0), 0.42,
                 0.72 + (0.24 if k % 2 == 0 else 0.0)) for k in range(8)]
    return [(k * 0.5, (2 if k in (3, 7) else 0), 0.40,
             0.76 + (0.20 if k % 2 == 0 else 0.0)) for k in range(8)]


_ARP_OCT = (0, 0, 12, 0, 0, 12, 0, 0)
_LEAD_OCTS = (0, 0, 12, -12)
_LEAD_SHIFTS = (
    (0, -1, 1, 0, -1),
    (0, 1, -1, 2, 0),
    (0, -2, 1, -1, 2),
    (0, 0, 1, -1, 0),
)


def _snap_midi(midi, chord_midis, max_move=2):
    pcs = {int(x) % 12 for x in chord_midis}
    if int(midi) % 12 in pcs:
        return int(midi)
    for delta in (1, -1, 2, -2):
        if abs(delta) <= max_move and (int(midi) + delta) % 12 in pcs:
            return int(midi) + delta
    return int(midi)


def _arrange(plan):
    """把 plan 展开成逐事件的编排（不合成音频，describe 也复用）。"""
    arr = {"pad": [], "pad_hi": [], "bass": [], "arp": [], "lead": [],
           "kick": [], "hat": [], "clap": []}
    dur = plan["duration"]
    beat_s = plan["beat_s"]
    bar_s = plan["bar_s"]
    nbars = plan["nbars"]
    key_chroma = plan["key_chroma"]
    scale_iv = plan["scale_iv"]
    mood = plan["mood"]
    voices = plan["voices"]
    chord_bars = int(_MOOD[mood]["chord_bars"])
    pad_tr = _voice_trait(voices["pad"])
    bass_tr = _voice_trait(voices["bass"])
    arp_tr = _voice_trait(voices["arp"])
    lead_tr = _voice_trait(voices["lead"])
    var = plan["variation"]
    rng_h = _rng("human", plan["seed"])
    bright = float(_MOOD[mood]["bright"])

    # ---- pad：sustain / pulse / swell 三种织体；seed 选缺省，voices 可按乐器覆盖
    pad_style = pad_tr["pad_style"] or var["pad_style"]
    var["pad_style_effective"] = pad_style
    b = 0
    while b < nbars:
        tier = plan["bar_tier"][b]
        token = plan["bar_chord"][b]
        if pad_style == "pulse":
            positions = (0.0, 1.5, 2.5) if tier in ("drive", "climax") else (0.0, 2.0)
            notes = _chord_midis(token, key_chroma, scale_iv, base=48 + pad_tr["pad_oct"],
                                 inversion=(plan["seed"] + b) % 3,
                                 register=var["pad_register"],
                                 want7=(mood in ("hopeful", "uplifting") and b % 4 == 3))
            if not notes:
                b += 1
                continue
            for pos in positions:
                t = b * bar_s + pos * beat_s
                if t >= dur:
                    continue
                pans = _pan_spread(len(notes), 0.55)
                arr["pad"].append({
                    "t": t, "dur": 0.95 * beat_s, "notes": list(notes), "pans": pans,
                    "amp": 0.34 / math.sqrt(max(1, len(notes))),
                    "attack": 0.02, "release": 0.20,
                    "decay": pad_tr["pad_decay"] if pad_tr["pad_decay"] is not None else 0.55,
                    "bright": bright, "detune": 3.0, "vib": pad_tr["vib"],
                })
            b += 1
            continue
        # sustain / swell：按 chord_bars 合并，但遇到 tier 或和弦变化就切开
        b2 = b + 1
        while (b2 < nbars and b2 < b + chord_bars
               and plan["bar_tier"][b2] == tier and plan["bar_chord"][b2] == token):
            b2 += 1
        start = b * bar_s
        end = min(dur, b2 * bar_s)
        if end - start < 0.05:
            b = b2
            continue
        # base=48 而不是 60：频谱实测发现 120–500 Hz 只剩 3.16%（20–120 Hz 占 50%、
        # 500–2000 Hz 占 37%），中间整段是空的 —— 听感就是"低频轰、中高频薄、没有厚度"。
        # 根因：pad 用 C4(261 Hz) 起，而 bass 被夹在 31–52（49–164 Hz），两者之间没有东西。
        # pad 降到 C3(131 Hz) 起正好填这一段，和弦性质不变（只是移位）。
        notes = _chord_midis(token, key_chroma, scale_iv, base=48 + pad_tr["pad_oct"],
                             inversion=(plan["seed"] + b // 2) % 3,
                             register=var["pad_register"],
                             want7=(mood in ("hopeful", "uplifting") and b % 4 == 3))
        pans = _pan_spread(len(notes), 0.62)
        arr["pad"].append({
            "t": start, "dur": max(0.2, end - start + 0.30), "notes": list(notes),
            "pans": pans, "amp": 0.30 / math.sqrt(max(1, len(notes))),
            "attack": 0.50 if pad_style == "swell" else 0.16,
            "release": 0.65, "decay": pad_tr["pad_decay"], "bright": bright,
            "detune": 4.0, "vib": pad_tr["vib"],
        })
        b = b2

    # ---- bass：每小节按 tier + seed 变体取型；根音来自当前和弦
    for bi in range(nbars):
        token = plan["bar_chord"][bi]
        d = _parse_degree(token)
        root = 36 + key_chroma + scale_iv[d["deg"] - 1] + d["acc"]
        while root < 31:
            root += 12
        while root > 52:
            root -= 12
        bass_style = bass_tr["bass_style"]
        if bass_style == "whole":
            pat = [(0.0, 0, 3.6, 0.85)]
        elif bass_style == "half":
            pat = [(0.0, 0, 1.7, 1.0), (2.0, 0, 1.7, 0.78)]
        elif bass_style == "pulse":
            pat = [(k * 0.5, (1 if k in (3, 7) else 0), 0.40,
                    0.70 + (0.22 if k % 2 == 0 else 0.0)) for k in range(8)]
        else:
            pat = _bass_pattern(plan["bar_tier"][bi], var["bass_variant"])
        for (beat, kind, dur_beats, vel) in pat:
            t = bi * bar_s + beat * beat_s
            if t >= dur:
                continue
            midi = root + (7 if kind == 1 else (12 if kind == 2 else 0))
            note_dur = min(dur - t, dur_beats * beat_s * 0.96)
            if note_dur < 0.04:
                continue
            arr["bass"].append({
                "t": t, "dur": note_dur, "midi": midi,
                "amp": 0.46 * vel, "pan": 0.0,
                "attack": 0.006, "release": 0.06,
                "decay": bass_tr["bass_decay"],
                "bright": bright * 0.5, "detune": 0.0, "vib": bass_tr["vib"],
            })

    # ---- body 层：260–520 Hz 的"和弦骨架"，填 120–250 Hz 与 500 Hz 之间那段空档 ----
    #
    # 频谱实测（12 s）：20–120 Hz 49.6% / 120–250 Hz **2.5%** / 250–500 Hz 16.5% /
    # 500–2000 Hz 18.2% —— 中间这一段几乎为空，听感是"低频轰、中频没有身体"。
    # 根因：pad 根音在 C3(131 Hz) 但它的和弦音往上走（131/155/196 Hz），而 arp/lead
    # 都在 C5 以上；250–520 Hz 这个"有厚度的区间"没有持续音。
    # 解法不是再开一个合成器，而是让 pad 多叠一层中音区和弦（+12 半音后限幅），
    # 用同一套包络与 pan，成本近零、也不会引入新的调性信息。
    # 实测各层频段分布（12 s）：pad 全在 250–2000 Hz（120–250 Hz 只有 0.7%）、
    # bass 96% 在 20–120 Hz、arp/lead 90%+ 在 500 Hz 以上 —— 所以 120–250 Hz 是挖空的。
    # 补法：把和弦**根音再降一个八度**叠一层（MIDI 40–55，即 82–196 Hz 的上半段），
    # 它的基频正好落在 120–250 Hz；用 pad 音色与包络，音量压到 pad 的 0.42 倍，
    # 只当作"厚度"而不是独立声部。
    for ev in arr["pad"]:
        low = [m - 12 for m in ev["notes"] if 40 <= m - 12 <= 57][:2]
        if low:
            # 音量：pad 的 1.15 倍。为什么这么大——实测 bass RMS 0.112 / kick 0.102，
            # 而 pad 只有 0.047、pad_hi 0.016，120–250 Hz 被 20–120 Hz 压到只剩 3%。
            # 低频本来就在能量上占优（等响曲线），要有"厚度"必须把中低频推上来。
            arr["pad_hi"].append(dict(ev, notes=low,
                                      amp=float(ev["amp"]) * 1.15,
                                      pans=_pan_spread(len(low), 0.30)))

    # ---- arp：琶音型由 seed 选，16 分/8 分随 tier
    pattern = var["arp_pattern"]
    for bi in range(nbars):
        tier = plan["bar_tier"][bi]
        e = float(plan["bar_energy"][bi])
        if e < 0.31:
            continue
        step = arp_tr["arp_step"] or (0.25 if tier in ("drive", "climax") else 0.5)
        notes = _chord_midis(plan["bar_chord"][bi], key_chroma, scale_iv,
                             base=72, inversion=0, register=0)
        if not notes:
            continue
        nsteps = int(round(4.0 / step))
        for k in range(nsteps):
            pos = k * step
            t = bi * bar_s + pos * beat_s
            if t >= dur:
                continue
            idx = pattern[k % len(pattern)] % len(notes)
            midi = notes[idx] + arp_tr["arp_oct"] + _ARP_OCT[(k + plan["seed"]) % len(_ARP_OCT)]
            while midi > 88:
                midi -= 12
            pan = 0.48 if (k % 2 == 0) else -0.48
            if bi % 2:
                pan = -pan
            arr["arp"].append({
                "t": max(0.0, t + (rng_h.rand() - 0.5) * 0.006), "dur": step * beat_s * 0.82,
                "midi": midi, "amp": (0.23 if k % 4 == 0 else 0.16),
                "pan": pan, "attack": 0.003, "release": 0.06,
                "decay": arp_tr["arp_decay"] if arp_tr["arp_decay"] is not None else 0.20,
                "bright": bright + 0.05, "detune": 0.0, "vib": arp_tr["vib"],
            })

    # ---- lead：动机骨架 + seed 变奏 + 强拍吸附和弦音
    motif = plan["motif"]
    phrase_beats = plan["phrase_beats"]
    phrase_s = phrase_beats * beat_s
    nph = max(1, int(math.ceil(dur / phrase_s - 1e-9)))
    shifts = _LEAD_SHIFTS[var["lead_variant"] % len(_LEAD_SHIFTS)]
    for ph in range(nph):
        base_t = ph * phrase_s
        if base_t >= dur:
            break
        shift = shifts[(ph + plan["seed"]) % len(shifts)]
        octv = _LEAD_OCTS[(plan["seed"] // 5 + ph) % len(_LEAD_OCTS)]
        for _mi, (beat, deg, ndur) in enumerate(motif):
            t = base_t + float(beat) * beat_s
            if t >= dur:
                continue
            d = _parse_degree(deg)
            step = d["deg"] - 1 + shift
            oct_extra = (step // 7) * 12
            deg_idx = step % 7
            midi = 60 + lead_tr["lead_oct"] + key_chroma + scale_iv[deg_idx] + d["acc"] + octv + oct_extra
            while midi < 52:
                midi += 12
            while midi > 88:
                midi -= 12
            bi = min(nbars - 1, int(t / bar_s))
            chord_notes = _chord_midis(plan["bar_chord"][bi], key_chroma, scale_iv,
                                       base=60, inversion=0, register=0)
            if abs(float(beat) - round(float(beat))) < 1e-6:
                midi = _snap_midi(midi, chord_notes)
            if (rng_h.rand() < 0.06 and abs(float(beat) - round(float(beat))) > 1e-6):
                var["skipped_lead"] += 1
                continue
            note_dur = min(dur - t, max(0.06, float(ndur) * beat_s * 0.92))
            vel = 0.74 + (0.12 if abs(float(beat) % 4.0) < 1e-6 else 0.0)
            vel += (rng_h.rand() - 0.5) * 0.10
            arr["lead"].append({
                "t": max(0.0, t + (rng_h.rand() - 0.5) * 0.008), "dur": note_dur, "midi": midi,
                "amp": 0.44 * vel, "pan": 0.16,
                "attack": 0.015, "release": 0.10,
                "decay": lead_tr["lead_decay"],
                "bright": bright + 0.10, "detune": 0.0, "vib": lead_tr["vib"],
            })
        # 乐句末加一个短倚音（seed 变体为奇数时更容易出现）
        if motif and (var["lead_variant"] % 2 == 1) and rng_h.rand() < 0.55:
            beat, deg, _ndur = motif[-1]
            t = base_t + float(beat) * beat_s - 0.075
            if t >= 0.0 and t + 0.07 < dur:
                d = _parse_degree(deg)
                step = d["deg"] - 1 + shift - 1
                oct_extra = (step // 7) * 12
                deg_idx = step % 7
                midi = 60 + lead_tr["lead_oct"] + key_chroma + scale_iv[deg_idx] + d["acc"] + octv + oct_extra
                while midi < 52:
                    midi += 12
                while midi > 88:
                    midi -= 12
                arr["lead"].append({
                    "t": max(0.0, t), "dur": 0.07, "midi": midi,
                    "amp": 0.22, "pan": 0.10, "attack": 0.004, "release": 0.02,
                    "decay": 0.10, "bright": bright, "detune": 0.0, "vib": 0.0,
                })
                var["grace_notes"] += 1

    # ---- drums：tier 型 + seed/小节位置的 ghost 与过门
    for bi in range(nbars):
        tier = plan["bar_tier"][bi]
        spec = plan["drums"][tier]
        start = bi * bar_s
        kick = list(spec["kick"])
        hat = list(spec["hat"])
        clap = list(spec["clap"])
        if tier != "calm" and kick and rng_h.rand() < 0.22:
            ghost = 3.75 if bi % 2 else 1.75
            if all(abs(ghost - x) > 1e-6 for x in kick):
                kick.append(ghost)
                var["ghosts"] += 1
        if tier != "calm" and hat and rng_h.rand() < 0.25:
            ghost = round(0.25 + 0.5 * int(rng_h.randint(0, 8)), 4)
            if all(abs(ghost - x) > 1e-6 for x in hat):
                hat.append(ghost)
                var["ghosts"] += 1
        fill = bool((bi + 1) % 4 == 0 and tier in ("drive", "climax")
                    and plan["bar_energy"][bi] >= 0.55)
        for p in sorted(set(round(float(x), 4) for x in kick)):
            t = start + p * beat_s
            if t >= dur:
                continue
            vel = 1.0 if abs(p) < 1e-6 else (0.88 if abs(p - 2.0) < 1e-6
                                            else (0.92 if abs(p - round(p)) < 1e-6 else 0.70))
            vel *= 0.94 + 0.12 * rng_h.rand()
            arr["kick"].append({"t": max(0.0, t + (rng_h.rand() - 0.5) * 0.003), "vel": vel})
        for _hi, p in enumerate(sorted(set(round(float(x), 4) for x in hat))):
            t = start + p * beat_s
            if t >= dur:
                continue
            vel = 0.82 if abs(p - round(p)) < 1e-6 else 0.58
            vel *= 0.92 + 0.16 * rng_h.rand()
            arr["hat"].append({"t": max(0.0, t + (rng_h.rand() - 0.5) * 0.005), "vel": vel,
                               "open": bool(rng_h.rand() < 0.10)})
        for p in sorted(set(round(float(x), 4) for x in clap)):
            t = start + p * beat_s
            if t >= dur:
                continue
            arr["clap"].append({"t": t, "vel": 0.92 * (0.94 + 0.12 * rng_h.rand()),
                                "fill": False})
        if fill:
            var["fills"] += 1
            for k, p in enumerate((3.0, 3.25, 3.5, 3.75)):
                t = start + p * beat_s
                if t >= dur:
                    continue
                arr["clap"].append({"t": t, "vel": 0.48 + 0.12 * k, "fill": True})
    for key in ("pad", "bass", "arp", "lead", "kick", "hat", "clap"):
        arr[key].sort(key=lambda ev: ev["t"])
    return arr


def _pan_spread(count, width):
    if count <= 1:
        return [0.0]
    return [-width + 2.0 * width * (i / (count - 1.0)) for i in range(count)]


# ============================================================================
# 渲染：逐事件合成 -> 层门限/侧链/乒乓延迟 -> 母带峰值归一化
# ============================================================================
def _pingpong(src, delay_s, feedback=0.33, taps=3):
    n = len(src)
    wet = np.zeros_like(src)
    d = max(1, int(round(float(delay_s) * SR)))
    for k in range(1, int(taps) + 1):
        sh = d * k
        if sh >= n:
            break
        g = float(feedback) ** k
        if k % 2:
            wet[sh:, 0] += src[:-sh, 1] * g
            wet[sh:, 1] += src[:-sh, 0] * g
        else:
            wet[sh:, 0] += src[:-sh, 0] * g
            wet[sh:, 1] += src[:-sh, 1] * g
    return wet


def _sidechain(n, times, depth=0.40, tail=0.17):
    env = np.ones(n, dtype=np.float64)
    m = int(tail * SR)
    if m <= 2:
        return env
    k = np.arange(m, dtype=np.float64)
    shape = 1.0 - float(depth) * np.clip(k / (0.004 * SR), 0.0, 1.0) \
        * np.exp(-k / (0.055 * SR))
    for t in times:
        i = int(round(float(t) * SR))
        i = max(0, min(n - 1, i))
        j = min(n, i + m)
        env[i:j] = np.minimum(env[i:j], shape[:j - i])
    return env


def _layer_gain(plan, layer, t_all):
    e = plan["bar_energy"]
    mood = plan["mood"]
    if layer == "pad":
        gate = np.ones_like(e)
    else:
        thr = _layer_thresholds(mood)[layer]
        gate = _smoothstep((e - thr) / 0.10)
    mult = 1.0
    if layer == "pad":
        mult *= float(_MOOD[mood]["pad_gain"])
    elif layer == "bass":
        mult *= float(_MOOD[mood]["bass_gain"])
    elif layer == "lead":
        mult *= _db(_MOOD[mood]["lead_db"])
    elif layer in ("kick", "hat", "clap"):
        mult *= _db(_MOOD[mood]["drum_db"])
    curve = mult * (0.30 + 0.70 * e) * gate
    return np.interp(t_all, plan["bar_centers"], curve,
                     left=float(curve[0]), right=float(curve[-1]))


def _render_stems(plan, arr):
    """逐层合成（未做母线混音），QA 可直接量各层 RMS；render 只负责总线。"""
    n = int(plan["n"])
    voices = plan["voices"]
    rng_phase = _rng("phase", plan["seed"])
    rng_noise = _rng("noise", plan["seed"])

    pad = np.zeros((n, 2), dtype=np.float32)
    pad_hi = np.zeros((n, 2), dtype=np.float32)
    bass = np.zeros((n, 2), dtype=np.float32)
    arp = np.zeros((n, 2), dtype=np.float32)
    lead = np.zeros((n, 2), dtype=np.float32)
    kick = np.zeros((n, 2), dtype=np.float32)
    hat = np.zeros((n, 2), dtype=np.float32)
    clap = np.zeros((n, 2), dtype=np.float32)

    for ev in arr["pad"]:
        for midi, pan in zip(ev["notes"], ev["pans"]):
            _render_note(pad, ev["t"], ev["dur"], midi, voices["pad"], ev["amp"], pan,
                         ev["attack"], ev["release"], ev["decay"], ev["bright"],
                         ev["detune"], rng_phase, ev.get("vib", 0.0))
    for ev in arr.get("pad_hi") or []:
        for midi, pan in zip(ev["notes"], ev["pans"]):
            _render_note(pad_hi, ev["t"], ev["dur"], midi, voices["pad"], ev["amp"], pan,
                         ev["attack"], ev["release"], ev["decay"], ev["bright"],
                         ev["detune"], rng_phase, ev.get("vib", 0.0))
    for ev in arr["bass"]:
        _render_note(bass, ev["t"], ev["dur"], ev["midi"], voices["bass"], ev["amp"],
                     ev["pan"], ev["attack"], ev["release"], ev["decay"],
                     ev["bright"], ev["detune"], rng_phase, ev.get("vib", 0.0))
    for ev in arr["arp"]:
        _render_note(arp, ev["t"], ev["dur"], ev["midi"], voices["arp"], ev["amp"],
                     ev["pan"], ev["attack"], ev["release"], ev["decay"],
                     ev["bright"], ev["detune"], rng_phase, ev.get("vib", 0.0))
    for ev in arr["lead"]:
        _render_note(lead, ev["t"], ev["dur"], ev["midi"], voices["lead"], ev["amp"],
                     ev["pan"], ev["attack"], ev["release"], ev["decay"],
                     ev["bright"], ev["detune"], rng_phase, ev.get("vib", 0.0))
    for ev in arr["kick"]:
        _render_kick(kick, ev["t"], ev["vel"], rng_noise)
    for ev in arr["hat"]:
        _render_hat(hat, ev["t"], ev["vel"], rng_noise, bool(ev["open"]))
    for ev in arr["clap"]:
        _render_clap(clap, ev["t"], ev["vel"], rng_noise, bool(ev["fill"]))

    t_all = np.arange(n, dtype=np.float64) / float(SR)
    # pad_hi 与 pad 同层同门限（它就是 pad 的中音区叠层），只是一起吃 pad 的 layer gain
    for name, buf in (("pad", pad), ("pad", pad_hi), ("bass", bass), ("arp", arp),
                      ("lead", lead), ("kick", kick), ("hat", hat), ("clap", clap)):
        if not buf.any():
            continue
        buf *= _layer_gain(plan, name, t_all)[:, None].astype(np.float32)

    # 乐器性格：square 的 pad 带一点颤音（同一 voices 槽位在不同音色下织体不同）
    trem = float(_voice_trait(voices["pad"])["trem"])
    if trem > 1e-6 and pad.any():
        pad *= (1.0 - 0.5 * trem + 0.5 * trem
                * np.sin(2.0 * math.pi * 0.33 * t_all)[:, None]).astype(np.float32)

    # 侧链：kick 位置压 pad/bass（鼓型一变，铺底也跟着有呼吸）
    sc = _sidechain(n, [ev["t"] for ev in arr["kick"]], depth=0.52)
    pad *= sc[:, None].astype(np.float32)
    pad_hi *= sc[:, None].astype(np.float32)
    bass *= (0.72 + 0.28 * sc)[:, None].astype(np.float32)

    # 乒乓延迟：给 arp / lead 一点空间，长度不变（尾部截断）
    delay_s = min(0.45, 0.75 * plan["beat_s"])
    arp = arp + 0.20 * _pingpong(arp, delay_s, 0.30, 3)
    lead = lead + 0.30 * _pingpong(lead, delay_s * 1.5, 0.34, 3)
    return {"pad": pad, "pad_hi": pad_hi, "bass": bass, "arp": arp, "lead": lead,
            "kick": kick, "hat": hat, "clap": clap}


def _render_audio(plan, arr):
    stems = _render_stems(plan, arr)
    pad, pad_hi, bass, arp = stems["pad"], stems["pad_hi"], stems["bass"], stems["arp"]
    lead, kick, hat, clap = stems["lead"], stems["kick"], stems["hat"], stems["clap"]
    n = int(plan["n"])

    # 总线配比：pad/arp/lead 抬起来，bass/kick 收一点 —— 低频别把 500 Hz 以上全盖住
    # 总线配比：pad_hi（中低频厚度）给到 1.45，bass 收到 0.50、kick 0.62 ——
    # 不是为了"好听"随便调，是因为实测 20–120 Hz 占了 50%，把中低频整个盖住。
    mix = (pad.astype(np.float64) * 1.55 + pad_hi.astype(np.float64) * 1.45
           + bass.astype(np.float64) * 0.50
           + arp.astype(np.float64) * 1.05 + lead.astype(np.float64) * 1.20
           + kick.astype(np.float64) * 0.62 + hat.astype(np.float64) * 0.72
           + clap.astype(np.float64) * 0.62)
    mix -= mix.mean(axis=0, keepdims=True)

    ni = min(n, int(0.020 * SR))
    if ni > 1:
        mix[:ni] *= (0.5 - 0.5 * np.cos(np.linspace(0.0, math.pi, ni)))[:, None]
    no = min(n, max(1, int(min(1.8, max(0.25, plan["duration"] * 0.22)) * SR)))
    if no > 1:
        mix[-no:] *= (0.5 + 0.5 * np.cos(np.linspace(0.0, math.pi, no)))[:, None]

    peak = float(np.max(np.abs(mix))) if mix.size else 0.0
    if peak > 1e-12:
        mix *= TARGET_PEAK / peak
    mix = np.clip(mix, -1.0, 1.0)
    samples = np.ascontiguousarray(mix.astype(np.float32))
    if not np.all(np.isfinite(samples)):
        raise RuntimeError("musicgen: 渲染出了 NaN/Inf（内部数值错误，拒绝输出）")
    return samples

# ============================================================================
# 报告 / 存盘
# ============================================================================
def _make_report(plan, arr, samples):
    n = int(plan["n"])
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    peak_db = 20.0 * math.log10(peak) if peak > 0.0 else -120.0
    dc_max = float(np.max(np.abs(samples.mean(axis=0)))) if samples.size else 0.0
    grid = np.linspace(0.0, plan["duration"], max(2, int(math.ceil(plan["duration"] * 8.0)) + 1))
    e_stat = np.interp(grid, plan["energy_x"], plan["energy_y"])
    secs = [{
        "name": s["name"],
        "start": round(float(s["start"]), 3),
        "end": round(float(s["end"]), 3),
        "energy": round(float(s["energy"]), 3),
        "tier": _tier_for(s["energy"]),
        "layers": _layers_for_energy(s["energy"], plan["mood"]),
    } for s in plan["sections"]]
    times = set()
    for s in plan["sections"]:
        times.add(round(float(s["start"]), 3))
        times.add(round(float(s["end"]), 3))
    for c in plan["bar_centers"]:
        times.add(round(float(c), 3))
    times.add(round(plan["duration"], 3))
    curve = [[t, round(float(np.interp(t, plan["energy_x"], plan["energy_y"])), 3)]
             for t in sorted(times)]
    variation = dict(plan["variation"])
    variation["arp_pattern"] = list(variation.get("arp_pattern", []))
    notes = {k: (sum(len(ev["notes"]) for ev in arr["pad"]) if k == "pad" else len(arr[k]))
             for k in ("pad", "bass", "arp", "lead", "kick", "hat", "clap")}
    rep = {
        "version": 1,
        "generator": "musicgen",
        "mode": "parametric",
        "duration": round(plan["duration"], 3),
        "actual_duration": round(n / float(SR), 6),
        "samples": n,
        "sr": SR,
        "bpm": plan["bpm"],
        "key": plan["key"],
        "key_name": plan["key_name"],
        "scale": plan["scale"],
        "mood": plan["mood"],
        "seed": plan["seed"],
        "bar_s": round(plan["bar_s"], 4),
        "beat_s": round(plan["beat_s"], 4),
        "bars": int(plan["nbars"]),
        "sections": secs,
        "energy": {
            "mean": round(float(np.mean(e_stat)), 3),
            "min": round(float(np.min(e_stat)), 3),
            "max": round(float(np.max(e_stat)), 3),
            "curve": curve,
        },
        "variation": variation,
        "fingerprint": plan["fingerprint"],
        "progressions": {k: list(v) for k, v in plan["progressions"].items()},
        "motif": [list(x) for x in plan["motif"]],
        "drums": {t: {k: list(v) for k, v in d.items()} for t, d in plan["drums"].items()},
        "voices": dict(plan["voices"]),
        "notes": notes,
        "peak": round(peak, 6),
        "peak_dbfs": round(peak_db, 2),
        "dc_max": round(dc_max, 8),
        "clipped": bool(np.any(np.abs(samples) > 1.0)) if samples.size else False,
        "finite": bool(np.all(np.isfinite(samples))) if samples.size else True,
    }
    return rep


def save(path, samples) -> str:
    """写 24-bit PCM WAV；返回写出的路径字符串（stdlib wave 可直接读回）。"""
    p = Path(path)
    arr = np.asarray(samples)
    _require(arr.ndim in (1, 2), "save: 只支持 1D/2D 采样，收到 shape=%r" % (arr.shape,))
    if arr.ndim == 1:
        arr = np.stack([arr, arr], axis=1)
    _require(arr.shape[1] in (1, 2), "save: 只支持单/双声道，收到 shape=%r" % (arr.shape,))
    _require(arr.shape[0] > 0, "save: 采样为空，拒绝写 WAV")
    arr = np.asarray(arr, dtype=np.float64)
    if not np.all(np.isfinite(arr)):
        bad = int(arr.size - np.count_nonzero(np.isfinite(arr)))
        raise ValueError("save: 采样里有 %d 个 NaN/Inf（%s），拒绝写出损坏的 WAV" % (bad, p))
    pcm = np.rint(np.clip(arr, -1.0, 1.0) * 8388607.0).astype(np.int32)
    raw = pcm.astype("<i4").view(np.uint8).reshape(-1, 4)[:, :3]
    p.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(p), "wb") as w:
        w.setnchannels(int(arr.shape[1]))
        w.setsampwidth(3)
        w.setframerate(int(SR))
        w.writeframes(raw.tobytes())
    return str(p)


def render(duration, *, bpm=96.0, key="Dm", scale="minor", mood="steady",
           seed=0, sections=None, progressions=None, motif=None,
           drums=None, voices=None, energy_curve=None):
    """一次性接口：按参数现场编排并合成整片配乐，返回 (samples, report)。

    samples 为 (N,2) float32，N = round(duration*48000)，峰值 -6 dBFS；
    report 至少包含 bpm/key/scale/mood/seed/sections/energy/variation/fingerprint。
    同参数两次运行逐采样一致（确定性来自 sha1 派生的 RandomState）。
    """
    plan = _build_plan(duration, bpm, key, scale, mood, seed, sections,
                       progressions, motif, drums, voices, energy_curve)
    arr = _arrange(plan)
    samples = _render_audio(plan, arr)
    return samples, _make_report(plan, arr, samples)


# ============================================================================
# 预览 / CLI
# ============================================================================
def _fmt_positions(vals):
    if not vals:
        return "-"
    return ",".join("%g" % float(x) for x in vals)


def describe(params) -> str:
    """不渲染音频，只把参数展开成可读的编排预览（CLI --describe 也用）。"""
    _require(isinstance(params, dict), "describe(params) 需要 dict，收到 %r" % (type(params).__name__,))
    duration = float(params.get("duration", 12.0))
    plan = _build_plan(
        duration,
        params.get("bpm", 96.0),
        params.get("key", "Dm"),
        params.get("scale", "minor"),
        params.get("mood", "steady"),
        params.get("seed", 0),
        params.get("sections"),
        params.get("progressions"),
        params.get("motif"),
        params.get("drums"),
        params.get("voices"),
        params.get("energy_curve"),
    )
    arr = _arrange(plan)
    lines = []
    lines.append("musicgen 参数编排预览")
    lines.append("  时长 %.3f s / %g BPM / %s（%s）/ mood %s / seed %d / 指纹 %s"
                 % (plan["duration"], plan["bpm"], plan["key"], plan["key_name"],
                    plan["mood"], plan["seed"], plan["fingerprint"]))
    lines.append("  小节 %.3f s（4 拍）/ 共 %d 小节 / 乐句 %d 拍 / 指纹变体 pad=%s 音区+%d 琶音=%s"
                 % (plan["bar_s"], plan["nbars"], plan["phrase_beats"],
                    plan["variation"].get("pad_style_effective", plan["variation"]["pad_style"]),
                    plan["variation"]["pad_register"],
                    ",".join(str(x) for x in plan["variation"]["arp_pattern"])))
    lines.append("  段落 %d 个：" % len(plan["sections"]))
    for s in plan["sections"]:
        lines.append("    %-8s %7.2f–%7.2f s  energy %.2f  tier %-6s 层 %s"
                     % (s["name"], s["start"], s["end"], s["energy"],
                        _tier_for(s["energy"]), "/".join(_layers_for_energy(s["energy"], plan["mood"]))))
    lines.append("  能量曲线：" + "  ".join("%.2f→%.2f" % (x, y) for x, y in zip(plan["energy_x"], plan["energy_y"])))
    lines.append("  和弦进行：")
    for tier in TIER_IDS:
        lines.append("    %-7s %s" % (tier, "–".join(plan["progressions"][tier])))
    lines.append("  动机（%d 音，乐句 %d 拍）：%s"
                 % (len(plan["motif"]), plan["phrase_beats"],
                    "；".join("%g %s %g" % (x[0], x[1], x[2]) for x in plan["motif"])))
    lines.append("  鼓型：")
    for tier in TIER_IDS:
        d = plan["drums"][tier]
        lines.append("    %-7s kick[%s] hat[%s] clap[%s]"
                     % (tier, _fmt_positions(d["kick"]), _fmt_positions(d["hat"]),
                        _fmt_positions(d["clap"])))
    lines.append("  音色：pad=%s / bass=%s / arp=%s / lead=%s"
                 % (plan["voices"]["pad"], plan["voices"]["bass"],
                    plan["voices"]["arp"], plan["voices"]["lead"]))
    lines.append("  预计事件：" + " / ".join(
        "%s %d" % (k, (sum(len(ev["notes"]) for ev in arr["pad"]) if k == "pad" else len(arr[k])))
        for k in ("pad", "bass", "arp", "lead", "kick", "hat", "clap")))
    return "\n".join(lines)


def _format_report(rep):
    lines = []
    lines.append("[musicgen] %.3f s / %g BPM / %s（%s）/ mood %s / seed %d"
                 % (rep["duration"], rep["bpm"], rep["key"], rep["key_name"],
                    rep["mood"], rep["seed"]))
    lines.append("  小节 %.3f s × %d；峰值 %.2f dBFS；指纹 %s"
                 % (rep["bar_s"], rep["bars"], rep["peak_dbfs"], rep["fingerprint"]))
    lines.append("  段落：")
    for s in rep["sections"]:
        lines.append("    %-8s %7.2f–%7.2f s  energy %.2f  tier %-6s 层 %s"
                     % (s["name"], s["start"], s["end"], s["energy"], s["tier"],
                        "/".join(s["layers"])))
    lines.append("  和声：" + " ｜ ".join("%s %s" % (t, "–".join(rep["progressions"][t]))
                                          for t in TIER_IDS))
    lines.append("  音色：pad=%s / bass=%s / arp=%s / lead=%s"
                 % (rep["voices"]["pad"], rep["voices"]["bass"],
                    rep["voices"]["arp"], rep["voices"]["lead"]))
    lines.append("  事件：" + " / ".join("%s %d" % (k, rep["notes"][k]) for k in rep["notes"]))
    v = rep["variation"]
    lines.append("  变奏：织体 %s / 音区 +%d / 过门 %d / ghost %d / 和弦变化 %d / 倚音 %d"
                 % (v.get("pad_style_effective", v.get("pad_style")),
                    int(v.get("pad_register", 0)),
                    int(v.get("fills", 0)), int(v.get("ghosts", 0)),
                    int(v.get("chord_changes", 0)), int(v.get("grace_notes", 0))))
    return "\n".join(lines)


def _load_json_arg(value, name):
    s = str(value).strip()
    if s.startswith("{") or s.startswith("["):
        try:
            return json.loads(s)
        except json.JSONDecodeError as exc:
            raise ValueError("%s 不是合法 JSON：%s" % (name, exc)) from exc
    p = Path(s)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("%s 读取失败：%s" % (name, exc)) from exc
    raise ValueError("%s 既不是 JSON 也不是存在的文件：%r" % (name, value))


def main(argv=None) -> int:
    _setup_stdio()
    ap = argparse.ArgumentParser(
        prog="musicgen.py",
        description="musicgen — 参数化配乐编排器（numpy，确定性，无采样）",
    )
    ap.add_argument("--selftest", action="store_true", help="跑自测（无网络，逐条 PASS/FAIL）")
    ap.add_argument("--describe", action="store_true", help="只打印编排预览，不渲染")
    ap.add_argument("--duration", type=float, default=12.0, help="片长（秒），默认 12")
    ap.add_argument("--bpm", type=float, default=96.0, help="BPM，默认 96")
    ap.add_argument("--key", default="Dm", help="调性，如 Dm / Am / C / F#m，默认 Dm")
    ap.add_argument("--scale", default="minor", help="调式：" + "/".join(SCALE_IDS))
    ap.add_argument("--mood", default="steady", help="情绪：" + "/".join(MOOD_IDS))
    ap.add_argument("--seed", type=int, default=0, help="种子，默认 0")
    ap.add_argument("--sections", default=None, help="段落 JSON / JSON 文件")
    ap.add_argument("--progressions", default=None, help="和声进行 JSON / JSON 文件")
    ap.add_argument("--motif", default=None, help="动机 JSON / JSON 文件")
    ap.add_argument("--drums", default=None, help="鼓型 JSON / JSON 文件")
    ap.add_argument("--voices", default=None, help="音色 JSON / JSON 文件")
    ap.add_argument("--energy-curve", default=None, help="能量曲线 JSON / JSON 文件")
    ap.add_argument("--out", default=None, help="输出 WAV（24-bit PCM）")
    ap.add_argument("--report", default=None, help="输出 report JSON")
    ap.add_argument("--json", action="store_true", help="stdout 打印完整 report JSON")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    params = {
        "bpm": args.bpm, "key": args.key, "scale": args.scale, "mood": args.mood,
        "seed": args.seed,
    }
    for name in ("sections", "progressions", "motif", "drums", "voices"):
        val = getattr(args, name)
        if val is not None:
            params[name] = _load_json_arg(val, "--" + name)
    if args.energy_curve is not None:
        params["energy_curve"] = _load_json_arg(args.energy_curve, "--energy-curve")

    if args.describe:
        print(describe(dict(params, duration=args.duration)))
        return 0

    samples, rep = render(args.duration, **params)
    if args.out:
        print("  已写 WAV：%s" % save(args.out, samples))
    if args.report:
        p = Path(args.report)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
        print("  已写报告：%s" % p)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print(_format_report(rep))
    return 0


# ============================================================================
# 自测
# ============================================================================
def _corr(a, b):
    x = np.asarray(a, dtype=np.float64).ravel()
    y = np.asarray(b, dtype=np.float64).ravel()
    if x.size != y.size or x.size < 2:
        return 0.0
    if float(np.std(x)) < 1e-12 or float(np.std(y)) < 1e-12:
        return 0.0
    return float(np.corrcoef(x, y)[0, 1])


def _raises(fn):
    try:
        fn()
    except (ValueError, TypeError):
        return True
    return False


def selftest() -> int:
    import tempfile

    failures = []
    checks = 0

    def ck(name, cond, detail=""):
        nonlocal checks
        checks += 1
        print("%s  %-58s %s" % ("PASS" if cond else "FAIL", name, detail))
        if not cond:
            failures.append(name)

    s, rep = render(12.0, bpm=96, key="Dm", scale="minor", mood="steady", seed=7)
    n_expected = int(round(12.0 * SR))
    ck("12 s 形状 (N,2) float32", s.shape == (n_expected, 2) and s.dtype == np.float32,
       "shape=%r dtype=%s" % (s.shape, s.dtype))
    ck("12 s 时长 ±1 采样", abs(len(s) - n_expected) <= 1, "N=%d 期望=%d" % (len(s), n_expected))
    ck("无 NaN/Inf", bool(np.all(np.isfinite(s))), "finite=%s" % bool(np.all(np.isfinite(s))))
    peak = float(np.max(np.abs(s)))
    ck("无削波 |x|<=1", peak <= 1.0, "peak=%.6f" % peak)
    ck("峰值 -6 dBFS 左右", abs(20.0 * math.log10(max(peak, 1e-12)) + 6.0) < 0.5,
       "peak=%.6f (%.2f dBFS)" % (peak, 20.0 * math.log10(max(peak, 1e-12))))
    ck("report 必需字段", all(k in rep for k in
                            ("bpm", "key", "scale", "mood", "seed", "sections",
                             "energy", "variation", "fingerprint")),
       "fingerprint=%s" % rep.get("fingerprint"))

    s2, rep2 = render(12.0, bpm=96, key="Dm", scale="minor", mood="steady", seed=7)
    ck("同参数逐采样一致", bool(np.array_equal(s, s2)),
       "array_equal=%s fingerprint_same=%s" % (bool(np.array_equal(s, s2)),
                                               rep["fingerprint"] == rep2["fingerprint"]))

    for dur in (1.0, 5.5, 30.0):
        x, _r = render(dur, bpm=96, key="Dm", scale="minor", mood="steady", seed=7)
        ck("时长边界 %g s" % dur, abs(len(x) - int(round(dur * SR))) <= 1 and bool(np.all(np.isfinite(x))),
           "N=%d" % len(x))

    with tempfile.TemporaryDirectory(prefix="musicgen_") as td:
        p = Path(td) / "musicgen_selftest.wav"
        out = save(p, s)
        with wave.open(str(p), "rb") as w:
            ch, sw, fr, nf = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        ck("save 返回 str 且可被 wave 读回",
           isinstance(out, str) and ch == 2 and sw == 3 and fr == SR and nf == len(s),
           "ch=%d sw=%d sr=%d frames=%d" % (ch, sw, fr, nf))
        ck("save 拒绝 NaN", _raises(lambda: save(Path(td) / "bad.wav",
                                                  np.array([[0.0, float("nan")]]))),
           "NaN 被拒")

    ck("未知音色报错", _raises(lambda: render(2.0, voices={"pad": "noise"})), "ValueError")
    ck("未知情绪报错", _raises(lambda: render(2.0, mood="zzz")), "ValueError")
    ck("未知调式报错", _raises(lambda: render(2.0, scale="zzz")), "ValueError")
    ck("未知调性报错", _raises(lambda: render(2.0, key="H")), "ValueError")
    ck("坏级数报错", _raises(lambda: render(2.0, progressions={"body": ["i", "ZZ"]})),
       "ValueError")
    ck("空动机报错", _raises(lambda: render(2.0, motif=[])), "ValueError")

    # 参数敏感性：换任一参数，波形相关性必须显著低于 0.95
    base, _b = render(12.0, bpm=96, key="Dm", scale="minor", mood="steady", seed=7)
    variants = [
        ("seed 7→8", render(12.0, bpm=96, key="Dm", scale="minor", mood="steady", seed=8)[0]),
        ("mood steady→driving", render(12.0, bpm=96, key="Dm", scale="minor",
                                       mood="driving", seed=7)[0]),
        ("voices 全换", render(12.0, bpm=96, key="Dm", scale="minor", mood="steady",
                              seed=7, voices={"pad": "square", "bass": "square",
                                              "arp": "saw", "lead": "triangle"})[0]),
        ("progressions 换", render(12.0, bpm=96, key="Dm", scale="minor", mood="steady",
                                  seed=7, progressions={
                                      "calm": ["i", "III", "VII", "iv"],
                                      "body": ["i", "V", "VI", "iv"],
                                      "drive": ["i", "iv", "VII", "III"],
                                      "climax": ["i", "V", "VI", "III"]})[0]),
        ("drums 换", render(12.0, bpm=96, key="Dm", scale="minor", mood="steady", seed=7,
                           drums={
                               "calm": {"kick": [0.75], "hat": [1.25, 2.75], "clap": []},
                               "body": {"kick": [0.5, 2.5], "hat": [0, 0.75, 1.5, 2.25, 3.0, 3.75],
                                        "clap": [2.0]},
                               "drive": {"kick": [0, 0.5, 1.25, 2, 2.5, 3.25],
                                         "hat": [0, 0.375, 0.75, 1.125, 1.5, 1.875, 2.25,
                                                 2.625, 3.0, 3.375, 3.75],
                                         "clap": [0.5, 2.5]},
                               "climax": {"kick": [0, 1, 2, 3],
                                          "hat": [0.125, 0.375, 0.625, 0.875, 1.125, 1.375,
                                                  1.625, 1.875, 2.125, 2.375, 2.625, 2.875,
                                                  3.125, 3.375, 3.625, 3.875],
                                          "clap": [1, 3]},
                           })[0]),
    ]
    print("  参数相关性（12 s，base=seed 7 steady；|r| < 0.95 为通过）：")
    for label, other in variants:
        r = _corr(base, other)
        ck("参数生效：" + label, abs(r) < 0.95,
           "r=%+0.4f |r|=%.4f" % (r, abs(r)))

    sec_s, sec_r = render(6.0, sections=[{"start": 0.0, "name": "A", "energy": 0.2},
                                         {"start": 3.0, "name": "B", "energy": 0.9}])
    ck("sections 生效", sec_r["sections"][0]["name"] == "A"
       and sec_r["sections"][-1]["name"] == "B"
       and abs(sec_r["sections"][-1]["energy"] - 0.9) < 1e-6,
       "names=%s" % [x["name"] for x in sec_r["sections"]])
    ec_s, ec_r = render(6.0, energy_curve=[0.1, 0.95])
    ck("energy_curve 生效", ec_r["energy"]["max"] > 0.8 and ec_r["energy"]["min"] < 0.2,
       "min=%.2f max=%.2f" % (ec_r["energy"]["min"], ec_r["energy"]["max"]))

    txt = describe({"duration": 8.0, "bpm": 100.0, "key": "Am", "mood": "driving",
                    "seed": 3})
    ck("describe 返回可读预览", isinstance(txt, str) and "100" in txt and "Am" in txt
       and "和弦进行" in txt, "%d 字符" % len(txt))

    print("")
    if failures:
        print("SUMMARY: %d checks, %d FAILED -> %s" % (checks, len(failures),
                                                      ", ".join(failures)))
        return 1
    print("SUMMARY: %d checks, all PASS" % checks)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
