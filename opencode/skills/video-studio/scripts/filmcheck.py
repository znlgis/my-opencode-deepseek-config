# -*- coding: utf-8 -*-
"""filmcheck.py — film.json 契约校验器（手写递归，只用标准库）。

为什么需要它
------------
film.json 是唯一内容真源，但 render.py 的 load_script() 只拦四类硬错误
（缺 title/lines、lines 为空、某条没 text、id 重复、bpm 非法）。**未知键会被静默忽略**：
把 hold 写成 holds、kind 拼错、sfx.cues[].at 写成 time，全都"跑得通但效果没了"。
本脚本把这类问题变成"看得见的 WARN + 一句怎么改"。

用法
----
    python filmcheck.py film.json            # 不写路径：先找 cwd/film.json，再找脚本同目录
    python filmcheck.py film.json --strict   # warning 也算失败（CI / 交付门禁用）
    python filmcheck.py film.json --json     # 机读输出（stdout 只有 JSON）

退出码
------
    0  没有 error（--strict 时也没有 warning）
    1  有 error（或 --strict 下有 warning）
    2  用法错误 / 文件不存在 / 不是合法 JSON

三件事 schema（schemas/film.schema.json）做不到，由本脚本补：
    1. 未知键 → WARN + "是不是想写 X"（编辑距离 ≤2 / 前缀匹配 / 常见拼错别名）
    2. 跨字段：sfx.cues[].at 引用的场景 id 必须存在于 lines；hold+语音预估与 timeline 对账
    3. style 枚举**从 stylekit.STYLES 单向读取**（不复制一份），stylekit 不可用时降级为 WARN

注意：这里是"补强"，不是替代。load_script() 仍是权威——两边都通过才说明剧本真的能跑。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _early_layout(argv=None):
    """在 import filmkit/previewkit **之前**把 --layout 落到环境变量。

    为什么要在模块顶部做：scenes.py 写的是 `from filmkit import W, H`，而 filmkit 的 W/H
    来自 layoutkit —— 值在导入那一刻就绑死了。等 argparse 解析完再设环境变量，此时画布
    早就定了，`--layout portrait` 会静默失效（最坏的那种 bug：以为在出竖屏，实际是横屏）。
    与 render.py 同一条纪律（那边是为了 `--stage frames`，这边是为了提案/校验）。

    只认 `--layout X` 与 `--layout=X` 两种写法；别的参数一概不碰。
    """
    import os as _os, sys as _sys
    args = list(_sys.argv[1:] if argv is None else argv)
    want = ""
    for i, tok in enumerate(args):
        if tok == "--layout" and i + 1 < len(args):
            want = args[i + 1]
            break
        if tok.startswith("--layout="):
            want = tok.split("=", 1)[1]
            break
    if want:
        _os.environ["VIDEO_STUDIO_LAYOUT"] = want


_early_layout()


# ============================================================================
# 与 render.py / stylekit.py / voicekit.py 对齐的硬事实（改了那边要同步这里）
# ============================================================================

KINDS = ("hook", "body", "quote", "outro", "emphasis", "calm")   # 也是 voicekit 的语气 id

# 视觉场景类型：决定"这段画面长成什么构图"，与叙事 kind **正交**——
#   kind 管声线/语气/配乐能量（voicekit / scorekit 读它）；
#   type 只管构图（assets/scenes.py 的 _DRAWERS 读它）。
# 所以**不新增 kind 值**：hero/stat/... 不是语气，硬塞进 KINDS 会撞 voicekit 的 tone id
# （TONE_IDS 与 KINDS 是同一组 6 个）。缺省（不写 lines[].type）= 由 grammar.py 按内容推导。
SCENE_TYPES = ("hero", "statement", "list", "stat", "compare", "process",
               "quote", "chapter", "chart", "outro")
# 与 assets/scenes.py 的 _DRAWERS 键对齐，改一边必须改另一边。
# （grammar.SCENE_TYPES 是同一组 10 个值，但那份顺序用于"平手取位"；这里只做成员校验，
#  本元组的顺序只影响报错信息里的枚举列举，不影响判定。三处不要再各写一份新名单。）
PRESETS = ("ultrafast", "superfast", "veryfast", "faster", "fast",
           "medium", "slow", "slower", "veryslow")
MUSIC_MODES = ("score", "bed", "file", "off")
ENGINES = ("pil", "web")

RATE_RE = re.compile(r"^\s*[+-]?\d+\s*%\s*$")
PITCH_RE = re.compile(r"^\s*[+-]?\d+\s*Hz\s*$")
ID_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")

CPS = 4.2                                   # 每秒约 4.2 字（voicekit 实测中位口径）
# 语速区间而不是单一 4.2：voicekit 校准实测 3.84–4.61 cps（9 个 persona，固定校准句），
# 而 stylekit 的声线策略允许的 cps_band 是 3.6–5.6（rate 覆盖 ±15% 后）。
# 只用中位数会误报：夹具片实测 5.2 字/秒的句子被按 4.2 估 -> 全片多算 5.2 s ->
# "剧本与 timeline 差 5.23 s"的假 WARN（改按声线带宽判，夹具片可复现）。
CPS_LO, CPS_HI = 3.6, 5.6
DEFAULT_LEAD, DEFAULT_TAIL, DEFAULT_MIN_DUR = 0.62, 0.92, 3.0
TIMELINE_TOL = 0.5                          # 估算与 timeline 差超过这个秒数就 WARN

# 常见拼错（编辑距离/前缀匹配对 time→at 无效，靠这张表兜底）
KEY_ALIASES = {
    "holds": "hold", "kinds": "kind", "time": "at", "times": "at", "when": "at",
    "start_time": "at", "at_time": "at", "duration": "dur", "length": "dur",
    "delay": "dur", "vol": "gain", "volume": "gain", "db": "gain_db",
    "captions": "sub", "caption": "sub",
    "item": "items", "item_list": "items", "style_id": "style",
    "voices": "voice", "styles": "style", "texts": "text", "lyric": "text",
    "cue": "cues", "sfx_cues": "cues", "sound": "sfx", "bgm": "music",
}

# 不是 film.json 的其他产物（例如配乐引擎落盘的 score.json 报告）——
# 交给本脚本前先认出来，直接 SKIP 而不是按剧本报"缺 title"的错误。
OTHER_ARTIFACT_KEYS = ("generator", "meas", "reproduce", "sections", "beats",
                       "transitions", "sr", "duration", "score")


# ============================================================================
# 报告
# ============================================================================

class Report:
    def __init__(self):
        self.errors = []
        self.warnings = []

    def err(self, path, msg, code="invalid"):
        self.errors.append(dict(path=path, code=code, message=msg))

    def warn(self, path, msg, code="warning"):
        self.warnings.append(dict(path=path, code=code, message=msg))


# ============================================================================
# 小工具：类型名、编辑距离、拼错建议
# ============================================================================

_TYPE_TEST = {
    "str": lambda v: isinstance(v, str),
    "num": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "int": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "bool": lambda v: isinstance(v, bool),
    "list": lambda v: isinstance(v, list),
    "dict": lambda v: isinstance(v, dict),
    "null": lambda v: v is None,
}
_TYPE_CN = {"str": "字符串", "num": "数字", "int": "整数", "bool": "true/false",
            "list": "数组", "dict": "对象", "null": "null"}


def _tname(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true/false"
    if isinstance(v, (int, float)):
        return "数字"
    if isinstance(v, str):
        return "字符串"
    if isinstance(v, list):
        return "数组"
    if isinstance(v, dict):
        return "对象"
    return type(v).__name__


def _lev(a, b):
    """编辑距离（键名都很短，O(nm) 足够）。"""
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _suggest(key, known):
    """给未知键找一个"最像"的合法键：别名表 → 编辑距离 ≤2 → 前缀匹配。"""
    low = str(key).lower()
    for cand in known:
        if cand.lower() == low:
            return cand
    alias = KEY_ALIASES.get(low)
    if alias in known:
        return alias
    best, bestd = None, 99
    for cand in known:
        d = _lev(low, cand.lower())
        if d < bestd:
            best, bestd = cand, d
    if best is not None and bestd <= 2:
        return best
    for cand in known:
        cl = cand.lower()
        if min(len(low), len(cl)) >= 2 and (low.startswith(cl) or cl.startswith(low)):
            return cand
    return None


def _f(v, default=0.0):
    """宽容取数：剧本里写脏了（字符串/None/对象）也不让校验器自己崩掉。"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def _weighted_chars(text):
    """等效字数：汉字 1 / 英文数字 0.6 / 标点 0.3（与工具 estimate.py 同口径）。"""
    n = 0.0
    for ch in str(text or ""):
        o = ord(ch)
        if 0x3400 <= o <= 0x4DBF or 0x4E00 <= o <= 0x9FFF or 0xF900 <= o <= 0xFAFF:
            n += 1.0
        elif ch.isspace():
            continue
        elif ch.isascii() and ch.isalnum():
            n += 0.6
        else:
            n += 0.3
    return n


# ============================================================================
# 契约（JSON Schema 子集的执行版；schema 文件负责文档，这里负责跑）
# ============================================================================

LINE_ITEM_SPEC = {
    "t": "dict", "required": ["text"],
    "props": {
        "text": {"t": "str", "nonempty": True},
        "ok": {"t": "bool"},
    },
}

# ---- 视觉场景字段的嵌套结构（写法照抄 LINE_ITEM_SPEC）----
# 这些对象的键由 scenes.py 的 10 个绘制器读：拼错就是"写了不生效"，所以**不**用
# open=True（那是留给 sfx 配方参数那种真·透传的）。每层列全 props，才能把拼错报成
# unknown_key（WARN + "是不是想写 X"），把类型写错报成 ERROR。
# required 与 schema 表里的问号标记一致：必填 = 绘制器没有它就没法画。
LINE_STAT_SPEC = {                   # type=stat 的数字卡
    "t": "dict", "required": ["value"],
    "props": {
        # 数字或字符串（"92"、">3x"、"1/2" 这类写法只能当字符串）
        "value": {"t": ("num", "str"), "nonempty": True},
        "unit": {"t": "str"},
        "label": {"t": "str"},
        "note": {"t": "str"},
    },
}

LINE_SERIES_SPEC = {                 # type=chart 的数据条
    "t": "dict", "required": ["label", "value"],
    "props": {
        "label": {"t": "str", "nonempty": True},
        "value": {"t": ("num", "str"), "nonempty": True},
        "unit": {"t": "str"},
    },
}

LINE_STEP_SPEC = {                   # type=process 的流程节点
    "t": "dict", "required": ["label"],
    "props": {
        "label": {"t": "str", "nonempty": True},
        "note": {"t": "str"},
    },
}

# 对比卡的一侧：items 每项可以写字符串，也可以写 {text}（短条目直接写字符串更省事，两种都认）
LINE_SIDE_SPEC = {
    "t": "dict",
    "props": {
        "head": {"t": "str"},
        "items": {"t": "list", "item": {
            "t": ("str", "dict"), "required": ["text"],
            "props": {"text": {"t": "str", "nonempty": True}},
        }},
    },
}

LINE_SIDES_SPEC = {                  # type=compare 的左右对比（left/right 都可省略）
    "t": "dict",
    "props": {
        "left": LINE_SIDE_SPEC,
        "right": LINE_SIDE_SPEC,
    },
}

LINE_SPEC = {
    "t": "dict", "required": ["text"],
    "props": {
        "id": {"t": "str", "pattern": ID_RE, "example": "l01"},
        "kind": {"t": "str", "enum": KINDS},
        "text": {"t": "str", "nonempty": True},
        "sub": {"t": "str"},
        "big": {"t": "str"},
        "subtitle": {"t": "str"},
        "chapter": {"t": "str"},
        "kicker": {"t": "str"},
        "items": {"t": "list", "item": LINE_ITEM_SPEC},
        "hold": {"t": "num", "min": 0.0},
        "lead": {"t": "num", "min": 0.0},
        "tail": {"t": "num", "min": 0.0},
        "persona": {"t": "str"},
        "tone": {"t": "str", "enum": KINDS},
        "voice": {"t": "str"},
        # ---- 视觉场景字段（7 个全可选）----
        # type 缺省由 grammar.py 按内容推导；其余字段是给绘制器的原料，不写就不画。
        # 注意这些字段与 kind **正交**，不是新 kind：kind 仍只有上面 6 个（voicekit 的语气 id）。
        "type": {"t": "str", "enum": SCENE_TYPES},
        "quote": {"t": "str"},
        "by": {"t": "str"},
        "stats": {"t": "list", "item": LINE_STAT_SPEC},
        "series": {"t": "list", "item": LINE_SERIES_SPEC},
        "steps": {"t": "list", "item": LINE_STEP_SPEC},
        "sides": LINE_SIDES_SPEC,
    },
}

# sfxkit 配方专属参数（references/sfx-design.md §7 列出的主要参数）：
# 写在这里只为了"不被当成拼错"，一律 {'t': 'any'} 原样透传给配方，不做类型校验。
CUE_RECIPE_PARAMS = ("pitch", "kind", "direction", "tone", "end", "brightness", "metal",
                     "tail", "start_hz", "end_hz", "rate", "density", "accel", "stop",
                     "shift", "depth", "strength", "gustiness", "whistle", "env",
                     "intensity", "surface", "crackle")

CUE_SPEC = {
    "t": "dict", "required": ["at", "type"], "passthrough": True,
    "props": dict({
        "at": {"t": ("num", "str")},
        "type": {"t": "str", "nonempty": True},
        "dur": {"t": "num", "min": 0.0},
        "gain": {"t": "num"},
        "size": {"t": "num"},
        "pan": {"t": "num"},
        "seed": {"t": "int"},
        "visual_at": {"t": ("num", "str")},
        "visual_t": {"t": ("num", "str")},
    }, **{k: {"t": "any"} for k in CUE_RECIPE_PARAMS}),
}

SCRIPT_SPEC = {
    "t": "dict", "required": ["title", "lines"],
    "props": {
        "title": {"t": "str", "nonempty_warn": True},
        "end_text": {"t": "str"},
        # style is deprecated: appearance comes from work/design.json.
        # It used to use enum_dynamic -> _load_styles(); STYLE_IDS is empty, so it emitted
        # a misleading WARN saying stylekit.py was missing (the file was there).
        # Now it is an inert key: accepted (old scripts must not hard-fail) but reported.
        "style": {"t": "str", "inert": "已废弃：外观由 work/design.json 决定（python propose.py --brief ... 再 --confirm），这里写任何值都不生效"},
        "engine": {"t": "str", "enum": ENGINES},
        "voice": {"t": ("str", "dict"), "open": "dict"},
        "rate": {"t": "str", "pattern": RATE_RE, "example": "+6%"},
        "pitch": {"t": "str", "pattern": PITCH_RE, "example": "+0Hz"},
        "bpm": {"t": ("num", "str"), "auto": True, "auto_only": True, "positive": True},
        "seed": {"t": "int", "min": 0},
        "crf": {"t": "int", "min": 1, "max": 51},
        "preset": {"t": "str", "enum": PRESETS},
        "lines": {"t": "list", "item": LINE_SPEC, "min_items": 1},
        "lead": {"t": "num", "min": 0.0},
        "tail": {"t": "num", "min": 0.0},
        "min_dur": {"t": "num", "min": 0.0},
        "hold": {"t": "num", "min": 0.0},
        "max_dur": {"t": "num", "min": 0.0},
        "bar": {"t": "num", "min": 0.0},
        "sub_max_hold": {"t": ("num", "null")},
        "trim_silence": {"t": "bool"},
        "trim_db": {"t": "num"},
        "tts_workers": {"t": "int", "min": 1},
        "chunk_frames": {"t": "int", "min": 0},
        "reencode_video": {"t": "bool"},
        "voice_gain": {"t": "num"},
        "whoosh": {"t": "bool"},
        "whoosh_amp": {"t": "num", "min": 0.0},
        "tail_map": {"t": "dict", "open": True},
        "pacing": {"t": "dict", "props": {
            "lead": {"t": "num", "min": 0.0}, "tail": {"t": "num", "min": 0.0},
            "min_dur": {"t": "num", "min": 0.0}, "hold": {"t": "num", "min": 0.0},
            "item_stagger": {"t": "num", "min": 0.0}}},
        "visual": {"t": "dict", "props": {
            "theme": {"t": "str"}, "layout": {"t": "str"},
            "theme_overrides": {"t": "dict", "open": True}}},
        "music": {"t": "dict", "props": {
            "enabled": {"t": "bool"},
            "mode": {"t": "str", "enum": MUSIC_MODES},
            "style": {"t": "str"},
            "mood": {"t": "str"},
            "bpm": {"t": ("num", "str"), "auto": True, "auto_only": True, "positive": True},
            "key": {"t": ("num", "str"), "auto": True},
            "seed": {"t": "int", "min": 0},
            "seed_from_content": {"t": "bool"},
            "gain_db": {"t": "num"}, "duck_db": {"t": "num"},
            # 旁白优先：mix_film 读这四个（背景在人声处额外让位多少 / 阈值）
            "voice_duck_db": {"t": "num"}, "voice_duck_sfx_db": {"t": "num"},
            "voice_thresh_db": {"t": "num"}, "voice_floor_db": {"t": "num"},
            # SNR 兜底：混音后实测人声/背景差，不足就整体压背景
            "guard_snr_db": {"t": "num"}, "guard_cap_db": {"t": "num"},
            "seg": {"t": "num", "min": 0.0}, "amp": {"t": "num", "min": 0.0},
            "gain": {"t": "num", "min": 0.0}, "xfade": {"t": "num", "min": 0.0},
            "chords": {"t": "list", "item": {"t": "list", "item": {"t": "num"}}},
            "sections": {"t": "list", "item": {"t": "dict", "open": True}}}},
        "sfx": {"t": "dict", "props": {
            "enabled": {"t": "bool"},
            "gain_db": {"t": "num"},
            "mix_gain_db": {"t": "num"},
            # render.py 的 st_sfx 把它透传给 CueBus.build_audio()：按配方名覆盖基础电平。
            # 以前 schema/filmcheck 都没写，用户写了会被当未知键警告（其实真的会生效）。
            "base_db": {"t": "dict", "open": True},
            # 音频差异化：content_seed 由 CueBus 读（位置派生 vs 内容派生）；
            # duck_transitions 由 render.st_sfx 读（段落边界前的转场音效自动让位）。
            "content_seed": {"t": "bool"},
            "duck_transitions": {"t": "bool"},
            "cues": {"t": "list", "item": CUE_SPEC}}},
        "web": {"t": "dict", "props": {
            "entry": {"t": "str"},
            "chunk_s": {"t": "num", "min": 0.0}}},
        # ---- 词级时间戳 / TTS 后端 ----
        # 词级时间戳开关（字幕按词折行 / cue 词锚点 / G10 门禁都依赖它）
        "words": {"t": "bool"},
        # TTS 后端选择（接口化；默认 edge，其它后端需先 register_tts_backend）
        "tts": {"t": "dict", "props": {
            "backend": {"t": "str"},
            "voice": {"t": ("str", "dict")}}},
        # 画幅与帧率：横屏 16:9 与竖屏 9:16 各自只能同比例下采样（见 filmkit.video_plan）
        "video": {"t": "dict", "props": {
            "width": {"t": "int", "min": 2},
            "height": {"t": "int", "min": 2},
            "out_width": {"t": "int", "min": 2},
            "out_height": {"t": "int", "min": 2},
            "fps": {"t": "int", "min": 1, "max": 120}}},
        # 合成参数（可选）：true_peak_db 覆盖 mux 的 loudnorm 真峰目标（默认 -1.5 dBTP）
        "mux": {"t": "dict", "props": {
            "true_peak_db": {"t": "num", "max": -0.1}}},
        # 质检门禁参数（全片 VMAF/SSIM 阻断 + ASR 回读）
        "qa": {"t": "dict", "props": {
            "vmaf": {"t": "dict", "props": {
                # enabled / require_vmaf 是起草时留下的**惰性键**：schema 与这里都认，
                # 但 render.py / qavmaf.py 全仓没有读取点——写了它门禁照跑，用户以为自己关掉了。
                # 真正生效的是 mode=off（或命令行 --vmaf off）。这里标 inert 让 filmcheck 报 WARN，
                # 因为本项目的立场是"写对了却不生效"比"报未知键"更坏。
                "enabled": {"t": "bool", "inert": "写它不生效；要跳过 G11 请写 qa.vmaf.mode=\"off\"（或 --vmaf off）"},
                "mode": {"t": "str", "enum": ("full", "sample", "off")},
                "pass": {"t": "num", "min": 0.0},
                "warn": {"t": "num", "min": 0.0},
                # 键名对齐 schemas/film.schema.json 与 qavmaf.report(sample_step=...)。
                # 这里以前写的是 sample_s —— 全仓没有任何消费者（render 不读、qavmaf 只认
                # sample_step），于是"写对了却完全没生效"，而 schema 里合法写完还会被本脚本
                # 报成未知键。别再兼容 sample_s：它从来没生效过，留着只会让人以为能用。
                "sample_step": {"t": "int", "min": 1}}},
            "ssim": {"t": "dict", "props": {
                "enabled": {"t": "bool", "inert": "写它不生效；SSIM 本来就只是诊断项（不单独阻断）"},
                "pass": {"t": "num", "min": 0.0, "max": 1.0},
                "warn": {"t": "num", "min": 0.0, "max": 1.0}}},
            "asr": {"t": "dict", "props": {
                "enabled": {"t": "bool"},
                "model": {"t": "str"},
                "recall_pass": {"t": "num", "min": 0.0, "max": 1.0},
                "recall_warn": {"t": "num", "min": 0.0, "max": 1.0}}},
            "require_vmaf": {"t": "bool", "inert": "写它不生效；要强制 G11 别记 skipped 请用 --strict-gates"}}},
    },
}


# ============================================================================
# 递归校验
# ============================================================================

def _types_cn(types):
    return " 或 ".join(_TYPE_CN.get(t, t) for t in types)


def _check(val, rule, path, rep, ctx):
    types = rule.get("t", "any")
    if isinstance(types, str):
        types = (types,)
    if "any" in types:
        return                                  # 配方专属参数：只认键名，不做类型校验
    if val is None:
        if "null" in types:
            return
        rep.err(path, "不能是 null：删掉这个键，或填 %s" % _types_cn(types), "null")
        return
    if isinstance(val, str) and rule.get("auto") and val.strip().lower() in ("auto", ""):
        return                                    # "" 与 "auto" 同义（load_script 也这么认）
    if isinstance(val, str) and rule.get("auto_only") and val.strip():
        rep.err(path, "字符串只能写 \"auto\"（bpm 要么是大于 0 的数字，要么是 \"auto\"；"
                      "收到 %r，render.py 会直接报错）" % val, "bad_auto")
        return
    if not any(_TYPE_TEST[t](val) for t in types if t in _TYPE_TEST):
        rep.err(path, "类型错误：应为 %s，实际是 %s（%r）"
                % (_types_cn(types), _tname(val), val), "bad_type")
        return

    # 惰性键：schema 与 filmcheck 都认它，但代码里没有任何读取点。
    # 报 WARN 而不是 ERROR —— 老剧本里写了它不该变成硬失败；但也绝不能静默通过，
    # 否则"写对了却不生效"会一路带到交付（本项目历史上最贵的一类缺陷）。
    if rule.get("inert"):
        rep.warn(path, "这个键**当前不生效**：%s" % rule["inert"], "inert_key")

    # 枚举校验：SPEC 里的 `enum` 声明必须真的被执行。
    # 这里曾经是"通用 enum + style 动态 enum"的混合实现；v1.0.2 删 style 动态校验时
    # 把**通用那段一起删掉了**（commit b4378ca），于是 kind/type/engine/preset/mode
    # 全部退回"写什么都通过"。而 line 619 的注释仍写着"LINE_SPEC 里已经有 enum，
    # 那是 ERROR"——注释在、执行不在，正是本项目最贵的一类缺陷。
    enum = rule.get("enum")
    if enum and isinstance(val, str) and val not in enum:
        near = _suggest(val, [e for e in enum if isinstance(e, str)])
        hint = ("；是不是想写 %r？" % near) if near else ""
        rep.err(path, "枚举错误：%r 不是可用值（可用：%s）%s"
                % (val, " / ".join(str(e) for e in enum), hint), "bad_enum")
        return

    if isinstance(val, (int, float)) and not isinstance(val, bool):
        if rule.get("positive") and val <= 0:
            rep.err(path, "必须大于 0（收到 %r；bpm 这类字段也可以写 \"auto\"）" % val, "bad_range")
        if rule.get("min") is not None and val < rule["min"]:
            rep.err(path, "不能小于 %s（收到 %r）" % (rule["min"], val), "bad_range")
        if rule.get("max") is not None and val > rule["max"]:
            rep.err(path, "不能大于 %s（收到 %r）" % (rule["max"], val), "bad_range")

    if isinstance(val, str):
        if rule.get("nonempty") and not val.strip():
            rep.err(path, "不能是空字符串（render.py 会把这条当缺字段）", "empty")
        if rule.get("nonempty_warn") and not val.strip():
            rep.warn(path, "是空字符串：顶栏/片名会是空的（不影响跑通，但成片会露馅）", "empty")
        pat = rule.get("pattern")
        if pat is not None and not pat.match(val):
            rep.err(path, "格式不对：%r（要匹配 %s；例如 %s）"
                    % (val, pat.pattern, rule.get("example", "")), "bad_pattern")

    if isinstance(val, list):
        if rule.get("min_items") and len(val) < rule["min_items"]:
            rep.err(path, "至少要 %d 项（当前 %d 项）" % (rule["min_items"], len(val)), "too_few")
        item = rule.get("item")
        if item:
            for i, it in enumerate(val):
                _check(it, item, "%s[%d]" % (path, i), rep, ctx)

    if isinstance(val, dict):
        if rule.get("open"):
            return                                  # 自由格式字典（tail_map/theme_overrides…）：不查未知键
        props = rule.get("props") or {}
        for req in rule.get("required") or []:
            if req not in val:
                rep.err("%s.%s" % (path, req), "缺少必填键 %r（%s）"
                        % (req, "render.py 会直接拒绝这份剧本"), "missing")
        for k, v in val.items():
            if k in props:
                _check(v, props[k], "%s.%s" % (path, k), rep, ctx)
            elif str(k).startswith("_"):
                continue                                  # _comment 之类的扩展键：不查不报
            else:
                near = _suggest(k, list(props))
                if near:
                    rep.warn("%s.%s" % (path, k),
                             "未知键 %r —— 是不是想写 %r？（render.py 不认识它会静默忽略："
                             "跑得通，但这个设置不生效）" % (k, near), "unknown_key")
                elif rule.get("passthrough"):
                    rep.warn("%s.%s" % (path, k),
                             "未知参数 %r：会原样透传给 sfxkit 配方（拼错则静默无效）；"
                             "常用的是 at/type/dur/gain/size/pan/seed，见 references/sfx-design.md"
                             % k, "unknown_cue_param")
                else:
                    rep.warn("%s.%s" % (path, k),
                             "未知键 %r：render.py 不读它，会被静默忽略"
                             "（确实要放扩展信息就写成 _%s 开头）" % (k, k), "unknown_key")


# ============================================================================
# 跨字段校验
# ============================================================================

def _cue_ref(at):
    """复刻 filmkit.cue_time 的解析：返回 ("abs"|"id"|"bad", 值或错误说明)。"""
    if isinstance(at, (int, float)) and not isinstance(at, bool):
        return "abs", float(at)
    s = str(at or "").strip()
    if not s:
        return "bad", "at 是空字符串（写 12.5 / abs:12.5 / l03 / l03+0.35）"
    if s.startswith("abs:"):
        try:
            return "abs", float(s[4:])
        except ValueError:
            return "bad", "abs: 后面不是数字（写 abs:12.5）"
    try:
        return "abs", float(s)
    except ValueError:
        pass
    cut = len(s)
    for i, ch in enumerate(s):
        if ch in "+-" and i > 0:
            cut = i
            break
    sid, off = s[:cut], s[cut:]
    if off:
        try:
            float(off)
        except ValueError:
            return "bad", "偏移 %r 不是数字（写 l03+0.35 或 l03-0.2）" % off
    # 词锚点：<id>:w<N>[+/-秒]。把 ":wN" 剥掉再做 id 存在性检查，
    # 否则合法写法会被报成"场景 id 不存在"（假失败比不校验更糟）。
    if ":w" in sid:
        head, _sep, tail = sid.partition(":w")
        if not tail.isdigit() or int(tail) < 1:
            return "bad", "词锚点要写成 l03:w2（词序号从 1 开始），收到 %r" % sid
        return "id", head
    return "id", sid


def _find_timeline(script_path):
    cands = [script_path.parent / "work" / "timeline.json",
             Path.cwd() / "work" / "timeline.json",
             script_path.parent / "timeline.json"]
    for c in cands:
        if c.is_file():
            return c
    return None


def check_cross(script, script_path, rep, ctx):
    """schema 表达不了的跨字段约束：id 引用、hold 与 timeline 对账。"""
    lines = [ln for ln in (script.get("lines") or []) if isinstance(ln, dict)]

    # ---- lines id：缺失 / 重复 ----
    ids = []
    for i, ln in enumerate(lines):
        lid = ln.get("id")
        if lid is None or not str(lid).strip():
            rep.warn("lines[%d].id" % i, "没有 id（render.py 会自动生成 l%02d）；"
                                          "要写 sfx.cues 就显式给 id" % (i + 1), "missing_id")
            continue
        lid = str(lid)
        if lid in ids:
            rep.err("lines[%d].id" % i, "id 重复：%r（TTS 缓存与 cue 会串，后一条会覆盖前一条）"
                    % lid, "dup_id")
        ids.append(lid)
    idset = set(ids)

    # ---- lines[].persona / lines[].tone 拼错会静默回落：枚举从 voicekit 单向读取 ----
    personas, tones = _load_personas()
    if personas:
        for i, ln in enumerate(lines):
            raw = ln.get("persona")
            if raw in (None, ""):
                continue
            name = str(raw).strip()
            # 原始 edge-tts voice id（zh-CN-YunxiNeural）也允许写在 persona 位
            if name in personas or ("-" in name and name.endswith("Neural")):
                continue
            near = _suggest(name, personas)
            rep.warn("lines[%d].persona" % i,
                     "未知 persona %r —— voicekit 会**静默回落**到 kind 默认声线"
                     "（成片换人，且不会有任何报错）%s。可用：%s"
                     % (name, ("；是不是想写 %r？" % near) if near else "",
                        " / ".join(personas[:9])), "unknown_persona")
    # tone 不在这里查：LINE_SPEC 里已经有 enum（KINDS 与 voicekit.TONE_IDS 是同一组 6 个），
    # 那是 ERROR 而不是 WARN。这里只补 persona —— 它没有静态枚举，拼错会静默换声线。
    _ = tones

    # ---- lines[].tail 当前不生效（保留字段）：写了会静默无效，必须说出来 ----
    for i, ln in enumerate(lines):
        if _f(ln.get("tail", 0.0)) > 0:
            lid = ln.get("id") or ("l%02d" % (i + 1))
            rep.warn("lines[%d].tail" % i, (
                "film.json 的逐句 tail 目前**不生效**：filmkit.build_timeline 只读 "
                "tail_map，不读 lines[].tail（渲染跑得通，但句后留白没变）。要逐句"
                "控制句后留白，请写顶层 tail_map（形如 l03: 1.4），或交给 voicekit 的 "
                "voice_plan（--stage tts 落 work/voice_plan.json，st_layout 会并进 "
                "tail_map）。例如 %s: 1.4" % lid),
                "inert_line_tail")

    # ---- 场景语法：绘制器的容量上限（超了会被静默截断） ----
    # 这是本项目最忌讳的一类坑：作者写对了、跑得通、但画面上少了几条，而没有任何提示。
    # 容量由 assets/scenes.py 的实现决定（stats 取前 3 条、series 前 6 条、steps 前 5 条、
    # sides 每侧前 3 条），改那边要同步改这里。
    _CAP = (("stats", 3), ("series", 6), ("steps", 5))
    for i, ln in enumerate(lines):
        for fld, cap in _CAP:
            v = ln.get(fld)
            if isinstance(v, list) and len(v) > cap:
                rep.warn("lines[%d].%s" % (i, fld),
                         "%s 有 %d 条，但 %s 构图只画前 %d 条（其余不会出现在画面上）。"
                         "要全画请拆成多段，或减到 %d 条以内"
                         % (fld, len(v), fld, cap, cap), "scene_capacity")
        sides = ln.get("sides")
        if isinstance(sides, dict):
            for side in ("left", "right"):
                blk = sides.get(side)
                its = blk.get("items") if isinstance(blk, dict) else None
                if isinstance(its, list) and len(its) > 3:
                    rep.warn("lines[%d].sides.%s.items" % (i, side),
                             "sides.%s 有 %d 条，但左右对比构图每侧只画前 3 条"
                             % (side, len(its)), "scene_capacity")

    # ---- video 块：只支持 16:9 横屏 + 下采样输出 ----
    # **不要在这里重写一遍规则**：允许列表 / 宽高比 / 上采样拒绝的权威实现在
    # filmkit.video_plan()。以前这里是第二套手写判据，于是"1600x900"能过 filmcheck
    # （0 error 0 warning）却在 render 的 st_layout 被 video_plan 拒片——校验器放行、
    # 渲染器拒收，用户只会觉得门禁在骗人。这里改成直接调用权威实现，只做错误翻译。
    vv = script.get("video") if isinstance(script.get("video"), dict) else None
    if vv:
        try:
            import filmkit as _K
        except Exception:                                   # noqa: BLE001
            _K = None
        if _K is not None:
            try:
                _K.video_plan(script)
            except ValueError as e:
                rep.err("video", "%s（filmkit.video_plan 会在渲染时拒片，这里提前拦）" % e,
                        "bad_video")
            except Exception as e:                          # noqa: BLE001
                rep.err("video", "画幅解析失败：%s" % e, "bad_video")
        else:
            # filmkit 不可用（拷出来只带了 filmcheck）时退回最小校验：只查 fps 枚举，
            # 并明说"完整校验需要 filmkit.py"，避免让人以为已经全查过。
            rep.warn("video", "找不到 filmkit.py：只做了 fps 枚举校验，"
                              "画幅/上采样规则未校验（完整校验请把 filmkit.py 放在同目录）",
                     "video_partial")

    # ---- sfx.cues[].at 引用的场景 id 必须在 lines 里 ----
    sfx = script.get("sfx") if isinstance(script.get("sfx"), dict) else {}
    cues = sfx.get("cues") if isinstance(sfx.get("cues"), list) else []
    for i, cue in enumerate(cues):
        if not isinstance(cue, dict):
            continue
        at = cue.get("at", cue.get("t"))
        if at is None:
            continue                                      # 缺 at 已由必填检查报过
        kind, val = _cue_ref(at)
        if kind == "bad":
            rep.err("sfx.cues[%d].at" % i, "at 写法不对：%s" % val, "bad_cue_at")
        elif kind == "id" and val not in idset:
            rep.err("sfx.cues[%d].at" % i,
                    "at=%r 引用的场景 id %r 不在 lines 里（现有：%s）。"
                    "改过台词 id 就要同步这里，否则这条音效会被 CueBus 静默跳过"
                    % (at, val, ", ".join(ids[:8]) or "lines 里没有 id"), "cue_id_missing")

    # ---- hold 之和 + 语音预估 vs work/timeline.json（差 > 0.5 s 就 WARN）----
    tl_path = _find_timeline(script_path)
    ctx["timeline"] = str(tl_path) if tl_path else None
    if tl_path is None:
        return
    try:
        tl = json.loads(tl_path.read_text(encoding="utf-8"))
        total = float(tl["total"])
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as e:
        rep.warn(str(tl_path), "读不了 timeline（%s），跳过时长对账" % e, "timeline_unreadable")
        return
    # 节拍覆盖的优先级与 stylekit.resolve_script_style 一致：pacing > 顶层字段 > 默认。
    # 不读 pacing 会把"pacing.lead=2.0"当成"没写 lead"，估出来的时长偏小一半 -> 假 WARN。
    pace = script.get("pacing") if isinstance(script.get("pacing"), dict) else {}

    def _pace(key, default):
        for box in (pace, script):
            v = box.get(key) if isinstance(box, dict) else None
            if v is not None:
                return _f(v, default)
        return float(default)

    lead = _pace("lead", DEFAULT_LEAD)
    tail = _pace("tail", DEFAULT_TAIL)
    min_dur = _pace("min_dur", DEFAULT_MIN_DUR)
    ghold = _pace("hold", 0.0)
    holds, speech, est_lo, est_hi = 0.0, 0.0, 0.0, 0.0
    for ln in lines:
        chars = _weighted_chars(ln.get("text"))
        hold = _f(ln.get("hold", ghold))
        holds += hold
        speech += chars / CPS
        # 快语速 = 时长短（下界），慢语速 = 时长长（上界）
        est_lo += max(min_dur, lead + chars / CPS_HI + tail + hold)
        est_hi += max(min_dur, lead + chars / CPS_LO + tail + hold)
    # bar > 0 把每段向上取整到小节倍数，total 天然比逐句估算大；这时不拿估算对账。
    drift = 0.0
    if not (_f(script.get("bar", 0.0)) > 0):
        if total < est_lo - TIMELINE_TOL:
            drift = total - est_lo
        elif total > est_hi + TIMELINE_TOL:
            drift = total - est_hi
    if drift:
        rep.warn(str(tl_path),
                 "剧本 hold 合计 %.2f s + 语音预估 %.2f–%.2f s（含 lead/tail）≈ 全片 "
                 "%.2f–%.2f s，与 timeline 的 total=%.2f s 差 %.2f s > %.1f s。"
                 "改过台词 / hold / 节奏参数就重跑 --stage layout（别拿旧时间轴直接渲）"
                 % (holds, est_lo, est_hi, est_lo, est_hi, total,
                    abs(drift), TIMELINE_TOL), "timeline_drift")
    # 台词内容逐句对账（最可靠的一条：build_timeline 把整条 line 原样拷进 scene，
    # 所以 timeline.scenes[i].text 就是"上次排轴时那句话"）。改过台词没重跑 layout，
    # 这里能精确点出来；比按时长估算准得多，也不会被语速/语气差异干扰。
    tl_by_id = {str(s.get("id")): s for s in (tl.get("scenes") or []) if isinstance(s, dict)}
    text_drift = []
    for ln in lines:
        lid = ln.get("id")
        if lid is None or not str(lid).strip():
            continue
        scene = tl_by_id.get(str(lid))
        if not isinstance(scene, dict) or "text" not in scene:
            continue                                  # 老版本 timeline 不带 text：跳过
        if str(scene.get("text")) != str(ln.get("text")):
            text_drift.append(str(lid))
    if text_drift:
        rep.warn(str(tl_path),
                 "timeline 里 %d 句的台词与 film.json 不一致（%s）：TTS 与字幕都还是旧词，"
                 "改完台词必须重跑 --stage tts layout 再渲"
                 % (len(text_drift), ", ".join(text_drift[:6])), "timeline_text")
    tl_ids = [str(s.get("id")) for s in (tl.get("scenes") or []) if isinstance(s, dict)]
    if idset and tl_ids and set(tl_ids) != idset:
        miss = sorted(idset - set(tl_ids))
        extra = sorted(set(tl_ids) - idset)
        rep.warn(str(tl_path),
                 "timeline 的场景 id 与 lines 对不上（lines 有但 timeline 没有：%s；"
                 "timeline 有但 lines 没有：%s）：增删过台词就重跑 --stage layout"
                 % (miss[:6] or "无", extra[:6] or "无"), "timeline_ids")


# ============================================================================
# 入口
# ============================================================================

def _setup_stdio():
    """被管道/文件重定向时切 UTF-8（与 render.py 同一口径，避免 Windows 控制台乱码）。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                                   # noqa: BLE001
            pass


def _fail(msg, as_json, code=2):
    if as_json:
        sys.stdout.write(json.dumps({"ok": False, "error": msg}, ensure_ascii=False) + "\n")
    else:
        sys.stderr.write("[filmcheck] %s\n" % msg)
        sys.stderr.write("  用法：python filmcheck.py <film.json> [--strict] [--json]\n")
    raise SystemExit(code)


def _load_personas():
    """从 voicekit 单向读取 persona 名与 tone id；拿不到返回 (None, None) 降级成不查。

    为什么要查：voicekit 对写错的 persona/tone 是**静默回落**（reason 里留
    unknown_persona:xxx，但不报错、不告警），于是"拼错一个声线名"的后果是整片
    静默换成 kind 默认声线——听感变了，却没有任何一处提示。schema 层只校验 t=str，
    拦不住；这里补上（规则仍然只有一份：枚举从 voicekit 读，不复制）。
    """
    try:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import voicekit                                  # noqa: F401  （只读枚举，不复制）
        personas = list(getattr(voicekit, "PERSONA_IDS", ()) or
                        getattr(voicekit, "VOICE_PERSONAS", {}).keys())
        tones = list(getattr(voicekit, "TONE_IDS", ()) or
                     getattr(voicekit, "VOICE_TONES", {}).keys())
        return (personas or None), (tones or None)
    except Exception:                                    # noqa: BLE001
        return None, None


def _classify(data):
    """film = 像剧本；artifact = 其他产物（配乐报告等），跳过；unknown = 判不了。"""
    if not isinstance(data, dict):
        return "unknown"
    if "lines" in data or "title" in data:
        return "film"
    if any(k in data for k in OTHER_ARTIFACT_KEYS):
        return "artifact"
    return "unknown"


def _default_path():
    for c in (Path.cwd() / "film.json", HERE / "film.json"):
        if c.is_file():
            return c
    return None


def main(argv=None):
    _setup_stdio()
    ap = argparse.ArgumentParser(
        description="film.json 契约校验器（未知键 WARN + 枚举/范围 ERROR + 跨字段对账）")
    ap.add_argument("path", nargs="?", default="", help="film.json 路径（缺省找 cwd/film.json）")
    ap.add_argument("--strict", action="store_true", help="warning 也算失败（退出码 1）")
    ap.add_argument("--layout", default="", choices=("", "landscape", "portrait"),
                    help="画布布局：landscape=1920x1080（默认）；portrait=1080x1920"
                         "（experimental：版式骨架与 compare/process/chart 已竖屏原生，"
                         "排版节奏未按竖屏复核）。必须与 film.json 的 video 块宽高比一致")
    ap.add_argument("--json", action="store_true", dest="as_json", help="机读输出（stdout 只有 JSON）")
    args = ap.parse_args(argv)

    path = Path(args.path).expanduser() if args.path else _default_path()
    if path is None:
        _fail("找不到 film.json：先 cd 到项目目录，或显式传路径", args.as_json)
    if not path.exists():
        _fail("文件不存在：%s" % path, args.as_json)
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        _fail("读不了 %s：%s（用 UTF-8 另存）" % (path, e), args.as_json)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        _fail("%s 不是合法 JSON：第 %d 行第 %d 列 %s（先跑 python -m json.tool 看一眼）"
              % (path, e.lineno, e.colno, e.msg), args.as_json)

    kind = _classify(data)
    rep = Report()
    ctx = {"timeline": None}
    skipped = False
    if kind == "artifact":
        skipped = True
    elif kind == "unknown":
        rep.err("$", "顶层既没有 title 也没有 lines，无法按 film.json 校验"
                     "（render.py 会直接报“缺少字段”）", "not_film")
    else:
        _check(data, SCRIPT_SPEC, "$", rep, ctx)
        check_cross(data, path, rep, ctx)

    failed = bool(rep.errors) or (bool(args.strict) and bool(rep.warnings))
    payload = {
        "ok": not failed,
        "file": str(path),
        "strict": bool(args.strict),
        "skipped": skipped,
        "errors": rep.errors,
        "warnings": rep.warnings,
        "counts": {"errors": len(rep.errors), "warnings": len(rep.warnings)},
        "timeline": ctx.get("timeline"),
    }

    if args.as_json:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    else:
        print("[filmcheck] %s" % path)
        if skipped:
            print("  [SKIP] 这不是 film.json（顶层没有 title/lines，像是配乐报告或其他产物）"
                  "——跳过契约校验。")
        for e in rep.errors:
            print("  [ERROR] %s: %s" % (e["path"], e["message"]))
        for w in rep.warnings:
            print("  [WARN]  %s: %s" % (w["path"], w["message"]))
        if skipped:
            print("  结论：跳过（0 error / 0 warning）")
        elif not rep.errors and not rep.warnings:
            print("  结论：契约通过（0 error / 0 warning）")
        else:
            tail = "（--strict：warning 也算失败）" if args.strict else ""
            print("  结论：%d error / %d warning%s%s"
                  % (len(rep.errors), len(rep.warnings), " → 失败" if failed else " → 通过", tail))
            if rep.errors:
                print("  先按上面 [ERROR] 逐条改；[WARN] 里未知键的，正是“改了没生效”的那类坑。")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
