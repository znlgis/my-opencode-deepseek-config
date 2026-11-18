# -*- coding: utf-8 -*-
"""designkit.py — 设计 DSL：把"这一版长什么样"写成可校验、可采样的 JSON。

设计原则（与 stylekit 一致的纪律）
----------------------------------
1. 纯标准库 + stylekit（stylekit 也不 import PIL / filmkit），不碰渲染。
   contrast / theme_lint / visual_lint 的阈值只有一份，全部从 stylekit 复用，本文件不抄第二份。
2. 确定性：只用 sha1 派生，禁 random / time / hash()（str hash 每进程带随机盐）。
   同一份 brief 永远采样出同一组候选。
3. fail-closed：设计缺字段、枚举越界、对比度不达标 -> 立刻报错，**绝不静默回落默认值**
   （静默回落 = 所有片子又长得一样）。

design.json 的形状（schema=1）
-----------------------------
    {
      "schema": 1,
      "id": "design-1a2b3c4d",          # ^[a-z0-9][a-z0-9._-]{1,63}$
      "label": "科技深蓝·四角括线",       # <= 40 字
      "pitch": "……",                    # 一句话立意（中文），<= 120 字
      "structure": {"segments": 6, "beats": [...], "hold_policy": "readable",
                    "target_sec": 45.0 | null},
      "visual": {
        "palette": {bg,bg2,surface,ink,muted,dim,accent,accent2,good,bad,rule},  # 11 色，[r,g,b]
        "family": "sans|serif|kai|round",
        "typography": {"tracking","line_gap","number","ratio","align"},
        "motif": {"kind","density","detail"},
        "transition": {"kind","dur"},
        "motion": {"ease","enter","push","breathe_hz"},
        "bias": {"x","y","inset"},
        "layout": "left-stack|center-stack|split-right",
        "hud": "chip-left|corner-line|none",
        "size": "1920x1080", "fps": 30            # 可选（schema 里可选）
      },
      "audio": {
        "voice": "zh-CN-YunxiNeural" 或
                 {"mode":"edge_id","id":"zh-CN-YunxiNeural",
                  # 默认单声线：四个 kind 都映射同一把；max_distinct=1。
                  # 多声线只在用户点名（对话/访谈/广播剧）时才采，2~3 把。
                  "map":{kind: id},"jitter":0.2,"max_distinct":1,
                  "rate":"+0%","pitch":"+0Hz"},
        "music": {"mode":"params","style":"tech-deep","scale":"minor",
                  "bpm":104,"key":"Dm","mood":"steady","seed":7},
        "sfx": {"enabled":true,"density":"normal","gain_db":-6}   # 可选
      },
      "pacing": {"lead","tail","min_dur","hold","item_stagger"},
      "_unmatched_constraints": ["没认出来的要求"]   # 采样期附加信息（不参与指纹）
    }

配色不是"随便取色"：语义色由对比度求解得到
-----------------------------------------
实测：纯 HSV 随机取色的 theme_lint 通过率只有 21/60 = 35%，不可用；
按"色相 + (s,v) 网格求解，取最接近设计意图且满足对比度的色"可达 60/60 = 100%。
网格是 s,v ∈ {0,0.1,...,1.0} 的 11×11；可行解按 |Δs|+|Δv| 取最小（并列取先遇到者，确定性）。
7 个语义色的对比度门槛直接取 stylekit.LINT_LIMITS（与 theme_lint 同一份）。

注意：**设计里存的是求解后的 11 个颜色**，不是配色名——这样 design.json 是自足的，
换机器/换版本不会因为"哪儿没有那个名字"而漂移；词 -> 色相锚点只用于采样期的求解过程。

对外接口
--------
    validate_design(design) -> list[str]        # 空 = 过；fail-closed 用
    design_to_plan(design)  -> dict             # 直接喂给 scenes.draw_frame 的 plan
    sample_designs(brief, n=3, seed=None) -> list[dict]
    design_fingerprint(design) -> str
    design_distance(a, b)   -> dict             # 用于"伪多方案"门禁

brief.must_have / avoid 是真过滤
----------------------------------------
词表见 _CONSTRAINT_WORDS：配色 / 母题 / 转场 / 情绪 / 声线，中文按子串认、英文按整词认。
must_have 是 AND（候选不许违反任何一条），avoid 是否定（违反一条就出局）。
认不出的那一条**不静默丢**，而是写进每个候选的 ``_unmatched_constraints``；只有"整个
must_have 一条都认不出"才报错——那等于用户的要求被整体忽略，继续"成功"是最坏的结果。
约束把某一类的选择域清空（两条互斥、或 must_have 与 avoid 撞车）时立刻报错，并在
报错里点名是哪一条，绝不放宽后照常返回。

CLI
---
    python designkit.py --list-palettes
    python designkit.py --sample "AI 大模型的三年" --n 3
    python designkit.py --lint design.json
"""
from __future__ import annotations

import argparse
import colorsys
import hashlib
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import stylekit as ST

SCHEMA = 1

# ============================================================================
# 一、采样域：直接复用 stylekit（不抄第二份）
# ============================================================================

MOTIFS = ST.VISUAL_MOTIFS
TRANSITIONS = ST.VISUAL_TRANSITIONS
EASES = ("out3", "out5", "inOut3", "outBack")
SCALES = ("minor", "major")
MOODS = ("calm", "reflective", "hopeful", "steady", "driving", "tense", "uplifting")
NUMBERS = ST.NUMBER_STYLES
LAYOUTS = tuple(ST.LAYOUT_IDS)
HUDS = ("chip-left", "corner-line", "none")
FONT_FAMILIES = ("sans", "serif", "kai", "round")
ALIGNS = ("left", "center", "right")
SIZES = ("1920x1080", "1280x720", "960x540",
         "1080x1920", "720x1280", "540x960")
# 尺寸与渲染画布必须同比例：横屏三档 / 竖屏三档分属两套版式（见 layoutkit 的 profile）。
# 校验口径放在这里，但"当前这一跑是横屏还是竖屏"由环境变量决定（layoutkit 导入期定死）。
FPSES = (24, 25, 30, 50, 60)
HOLD_POLICIES = ("none", "readable", "long")
SFX_DENSITIES = ("none", "sparse", "normal", "rich")


PORTRAIT_PACE = 1.08


def _pace_factor():
    """画布节奏系数：横屏 1.0 / 竖屏 1.08（首轮校准值，不是推导出来的真值）。

    为什么竖屏要慢一点：竖屏一屏能承载的视觉元素更少（list 只能单列、compare 上下各半屏），
    观众在同样的秒数里「看完一屏」更吃力，句前留白与句后停留都该略长。

    **这个 1.08 是首轮校准，不是从几何量推导出来的**：我先按安全区面积比开方算过，
    得出的是 0.93（竖屏 SAFE 面积其实比横屏小），方向相反——那个推导是错的，
    所以这里不再假装有公式，只留一个明确标注为「待实测修正」的常量：
    它改的是 `design.json` 的 pacing（lead/tail/min_dur/hold），可以逐片覆盖，
    也可以在 film.json 里用更慢的 hold 顶掉。要调就调这一个数。
    """
    try:
        import layoutkit                              # noqa: PLC0415
        if int(layoutkit.H) <= int(layoutkit.W):
            return 1.0
    except Exception:                                 # noqa: BLE001 —— 只读旁路
        return 1.0
    return PORTRAIT_PACE

def _canvas_size():
    """当前一跑的画布尺寸字符串（横屏 1920x1080 / 竖屏 1080x1920）。

    为什么采样器不再写死 "1920x1080"：visual.size 是设计的一部分，写死横屏等于
    "竖屏片子的设计里写着横屏尺寸"——design_to_plan 不读它、但 validate_design 会校验，
    用户手改会撞枚举。跟随 layoutkit 的 profile 才是同一份事实。
    """
    try:
        import layoutkit                              # noqa: PLC0415
        return "%dx%d" % (int(layoutkit.W), int(layoutkit.H))
    except Exception:                                 # noqa: BLE001 —— 只读旁路
        return "1920x1080"

# 配色：**没有命名家族**。
#
# 决定这件事的依据很直白：本技能只提供"生成视频的技术"，不提供模板。既然没有模板，
# 就不该存在"深蓝 / 近黑金 / 冷薄荷…"这样一份**可选清单** —— 有清单就意味着
# 用户的片子只能在少数几个预设外观里挑，下一个永远不存在。
#
# 模型是**连续色相空间**：
#   · 底色 = (色相 h ∈ [0,360), 饱和度 s, 明度 v) 三个连续量 -> 直接算出 bg/bg2/surface；
#   · 其余 8 个语义色（ink/muted/dim/accent/accent2/good/bad/rule）由对比度网格求解；
#   · 全部通过 theme_lint 才算数（对比度不足就重采，绝不悄悄放行）。
# 于是"第 9 种外观"不是新增模板，而是把 h 转到 200° 就自然得到的东西。
_PALETTE_SPACE = {
    True:  {"v": (0.030, 0.075), "s": (0.10, 0.55)},   # 暗色片：底子压很暗
    False: {"v": (0.90, 0.985),  "s": (0.02, 0.28)},   # 亮色片：纸感底子
    # 明亮活泼档：浅底，但底色自己带颜色（v 压在中高、s 拉到能看出偏色）。
    #
    # 为什么不把 False 的 s 上限直接调高：纸感浅底（s<=0.28）是"安静、克制"的表达，
    # 在纪录片与报告类题材里是对的；整体调高等于让所有浅色片都变花。
    # 所以"活泼"是**显式气质**（intake 的 tone / brief 的「明亮，活泼」），不是全局改口味。
    # 实测（色相每 10° × 三个 (s,v) 采样，共 108 点）：v (0.90,0.96) + s (0.26,0.46)
    # 的解出率 93.5%，不可解的 7 点全落在 0–20° 红橙带（浅底红橙上 ink 对比度到不了），
    # 由 _solve_palette_retry 逐档横向让开色相兜底。
    "vivid": {"v": (0.90, 0.96), "s": (0.26, 0.46)},
}

# 气质（tone）不是"配色族"（那会恢复一份可选外观清单），它只回答一个问题：
# **底色该压暗还是提亮**。色相仍然在连续空间里自由取。
_TONE_BRIGHT = "bright"      # 浅底、活泼（带色、有精神）——任何题材的推荐默认
_TONE_LIGHT = "light"        # 浅底、纸感（安静、克制）
_TONE_DARK = "dark"          # 暗底（发布会/技术向）
_TONES = (_TONE_BRIGHT, _TONE_LIGHT, _TONE_DARK)


def _palette_from_hue(hue, sat, val, dark):
    """(h,s,v) -> 底色三件套 bg/bg2/surface（连续量，无数值枚举）。"""
    def hsv(h, s, v):
        return ST.hsv_to_rgb(h % 360.0, _clamp(s, 0.0, 1.0), _clamp(v, 0.0, 1.0))
    bg = hsv(hue, sat, val)
    # bg2 更亮一档、surface 再亮一档：层次靠明度差，不靠换色相（换色相会变"两部片"）
    step2 = 0.055 if dark else -0.045
    step3 = 0.100 if dark else -0.075
    bg2 = hsv(hue + 6.0, sat * 1.10, val + step2)
    surface = hsv(hue + 10.0, sat * 1.18, val + step3)
    return bg, bg2, surface


# 没有"配色族"这个概念：这个空 dict 只是为了让按名遍历的调用点
# （`for name in PALETTE_FAMILIES` 之类）不 AttributeError —— 它必须保持为空：
# 一旦有人往里加东西，就等于偷偷恢复了一份"可选外观清单"。
PALETTE_FAMILIES = {}

# 但 brief 里仍然会有人说"要深蓝 / 近黑 / 薄荷"。这些词要能生效，否则用户的
# 自然语言要求就落空了。
# 做法：词 -> **色相锚点 + 明暗**，不是"族名 -> 一套现成外观"。锚点只是采样的起点，
# 色相仍然是连续量（可在锚点附近 ±HUE_WINDOW 内浮动）。
_PALETTE_ANCHOR = {
    "deep-blue":  (210.0, True),
    "near-black": (240.0, True),
    "cool-mint":  (168.0, True),
    "sand":       (36.0,  True),
    "slate":      (215.0, True),
    "forest":     (150.0, True),
    "warm-paper": (38.0,  False),
    "ink-paper":  (60.0,  False),
}
_HUE_WINDOW = 26.0          # 锚点附近 ±26°（再宽就不像"深蓝"了，再窄等于恢复成枚举）
_HUE_GRID = 24              # 约束色相窗口内的候选网格数（52° 窗口 = 每格约 2.2°）
# 多个候选之间的"底色必须看得出不一样"的色相最小间距（度）。
# 为什么是 12° 而不是分桶：12° 是并排看图时人眼能稳定分辨的色相下限附近
# （再小就成了同一片底色的细微偏色）。约束收窄时可选色相窗口只有 52° 宽，
# 12° 间距仍能放下 3–4 个候选，不会像"24° 分桶"那样在 n=3 时大半概率采样失败。
_PALETTE_KEEP_GAP = 12.0


def _hue_gap(a, b):
    """两个色相在色环上的最短距离（0..180）。色相是环形的：350° 与 10° 只差 20°。"""
    d = abs(float(a) - float(b)) % 360.0
    return d if d <= 180.0 else 360.0 - d

# 11 个颜色字段（theme_lint 检的就是这些；顺序固定，便于审计）
PALETTE_KEYS = ("bg", "bg2", "surface", "ink", "muted", "dim",
                "accent", "accent2", "good", "bad", "rule")

# 设计意图 (s0, v0)：求解器在可行域里找离它最近的网格点。
# 暗色族要"亮而不刺眼"的文字、明色族要"深而不死黑"的文字；accent 两族都要求高饱和。
_ROLE_INTENT = {
    True: {"ink": (0.08, 0.94), "muted": (0.22, 0.72), "dim": (0.24, 0.56),
           "accent": (0.86, 0.94), "accent2": (0.58, 0.90)},
    False: {"ink": (0.18, 0.16), "muted": (0.16, 0.36), "dim": (0.12, 0.52),
            "accent": (0.86, 0.56), "accent2": (0.58, 0.50)},
}
_ROLE_HUE = {"ink": 0, "muted": 0, "dim": 0, "accent": 0, "accent2": 150}
_GOOD_HUE = 135.0
_BAD_HUE = 5.0

# 情绪 -> BPM / 层门限。注意这里**没有"情绪 -> 曲风"的映射**：
# 把情绪映射到一套配乐风格表，等于把"配乐模板"伪装成参数。
# 配乐由 musicgen 现场编排，情绪只决定 BPM 与织体门限，不指向任何现成风格。
_MOOD_BPM = {
    "calm": 78, "reflective": 86, "hopeful": 92, "steady": 104,
    "driving": 124, "tense": 98, "uplifting": 118,
}
_MOOD_WORD = {
    "calm": "克制留白", "reflective": "回望与沉淀", "hopeful": "向上的暖意",
    "steady": "稳稳推进", "driving": "强驱动节奏", "tense": "绷紧的悬念",
    "uplifting": "昂扬收束",
}
_MOTIF_WORD = {
    "plain": "素净留白",
    "brackets": "四角括线", "hairlines": "细线页眉", "handdrawn": "手绘内框",
    "slabs": "粗色块", "minimal": "极简留白", "letterbox": "电影遮幅",
    "signal": "信号条", "rays": "放射线",
}
_TRANS_WORD = {
    "dip": "叠化", "wash": "淡入", "paper": "纸卷",
    "wipe": "擦入", "cut": "硬切", "iris": "光圈",
}

# 声线池：全部是可被 static 校验的 zh-CN-XxxNeural（本模块不 import voicekit——
# 那会拉起 edge_tts。格式只做正则，不做"这个 id 是否真的存在"的联网校验）。
#
# **只从"有响度补偿数据的音色"里采样**。
# 为什么：voicekit 的响度补偿是一张按 voice id 的数据表（VOICE_LOUDNESS / assets/voice_loudness.json）；
# 采样器如果产出表里没有的音色，那行的 gain_db 会回落 0 —— 等于"这个候选的旁白电平没校准"。
# 实测：5 个题材 × 3 候选共产出 7 个音色，其中 **4 个没有补偿数据**。
# 两处各存一份清单必然漂移，所以这里直接读共享 JSON（不 import voicekit）。
_VOICE_LOUDNESS_PATH = os.path.normpath(
    os.path.join(HERE, os.pardir, "assets", "voice_loudness.json"))


def _loudness_voices():
    """读共享响度表里的音色（读不到就返回空，由调用方兜底到内置池）。"""
    try:
        with open(_VOICE_LOUDNESS_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        voices = tuple(sorted(str(k) for k in (data.get("voices") or {})))
        return tuple(v for v in voices if _VOICE_RE.match(v))
    except (OSError, json.JSONDecodeError):
        return ()


_FALLBACK_VOICES = (
    "zh-CN-XiaoxiaoNeural", "zh-CN-XiaoyiNeural", "zh-CN-YunxiNeural",
    "zh-CN-YunjianNeural", "zh-CN-YunyangNeural",
)
# 只认三地中文（CN/TW/HK）：实测 edge-tts 免费端点的 zh 音色就这三类，
# 而响度补偿表里确实有 zh-TW-HsiaoChenNeural（软糯女声）——只写 zh-CN 会把它判非法。
_VOICE_RE = re.compile(r"^zh-(?:CN|TW|HK)-[A-Z][A-Za-z]*Neural$")
VOICES = _loudness_voices() or _FALLBACK_VOICES
VOICE_KINDS = ("hook", "body", "quote", "outro")

# 骨架几何按母题给（母题决定"框长什么样"，卡片只是它的容器）
# 素净母题：卡片只有填充、没有描边（描边就是细线）。
_CARD_BY_MOTIF = {
    "plain": dict(radius=14, fill_a=24, outline_a=0, pad=34, h=96, gap=118),
    "brackets": dict(radius=14, fill_a=16, outline_a=28, pad=34, h=96, gap=118),
    "hairlines": dict(radius=8, fill_a=26, outline_a=40, pad=30, h=92, gap=112),
    "handdrawn": dict(radius=4, fill_a=30, outline_a=52, pad=32, h=94, gap=116),
    "slabs": dict(radius=22, fill_a=30, outline_a=56, pad=32, h=100, gap=122),
    "minimal": dict(radius=0, fill_a=0, outline_a=70, pad=28, h=90, gap=108),
    "letterbox": dict(radius=10, fill_a=20, outline_a=32, pad=36, h=88, gap=124),
    "signal": dict(radius=12, fill_a=22, outline_a=36, pad=30, h=92, gap=114),
    "rays": dict(radius=18, fill_a=18, outline_a=46, pad=34, h=96, gap=126),
}
# 背景纹理也是细线/网点来源：素净母题不叠任何纹理。
_TEXTURE_BY_MOTIF = {
    "plain": "none",
    "brackets": "dots", "hairlines": "paper", "handdrawn": "paper",
    "slabs": "grid", "minimal": "none", "letterbox": "none",
    "signal": "scanlines", "rays": "none",
}

# 采样用的离散档位（取整避免浮点尾差，JSON 才好逐字节比对）
_DENSITIES = (0.55, 0.70, 0.85, 1.00)
# 默认不推镜（push=0）：慢速亚像素位移在 1x 光栅上必然表现为 1px 台阶，
# 实测 push=0.14 的 4 秒段每秒跳 12 次（见 scenes._view）。要推镜请手改 design。
_PUSHES = (0.0,)
# 默认母题池：只有素净（不画装饰细线）。其余母题必须由 brief 点名或手改 design 才会出现。
_DEFAULT_MOTIFS = ("plain",)
_BREATHES = (0.30, 0.40, 0.50, 0.60, 0.75, 0.90, 1.05)
_ENTERS = ((0.35, 0.75), (0.40, 0.90), (0.45, 0.85), (0.50, 1.00),
           (0.55, 1.00), (0.60, 1.10), (0.70, 1.20))
_XS = (-150, -90, -60, -30, 0, 30, 60, 90, 150)
_YS = (-90, -60, -30, 0, 30, 60, 90)
_INSETS = (0, 20, 40, 60)

# 校验用的键白名单（schema 的 additionalProperties=false 的同义实现）
# _unmatched_constraints 是采样期附带的"这条要求没生效"的记录（见 _resolve_constraints）：
# 不参与指纹、不参与渲染，但必须被契约接纳，否则采样器吐出的设计自己就过不了校验。
_TOP_KEYS = ("schema", "id", "label", "pitch", "structure", "visual", "audio", "pacing",
             "_unmatched_constraints")
_STRUCT_KEYS = ("segments", "beats", "hold_policy", "target_sec", "target_mode")
# 主题字典的字段清单（与 stylekit._theme 构造器的产出严格一致）。
# 从构造器现场造一份样板再取键，而不是手抄一份常量 —— 手抄的清单会和构造器漂移。
def _theme_field_keys():
    """主题字段清单：从 _theme 构造器现场造一份样板再取键。

    为什么不从某套内置主题取：内置主题表已经不存在，没有"参照物"。
    也不手抄一份常量 —— 手抄的清单一定会在某次改构造器时漂移，
    然后表现成"某个颜色字段取不到"这种半夜才能查出来的 KeyError。
    """
    pal = {"bg": (0, 0, 0), "bg2": (16, 16, 16), "surface": (32, 32, 32),
           "ink": (240, 240, 240), "muted": (180, 180, 180), "dim": (120, 120, 120),
           "accent": (200, 160, 60), "accent2": (80, 140, 220), "good": (60, 180, 110),
           "bad": (220, 70, 70), "rule": (60, 60, 60)}
    mat = {"vignette": 0.2, "vignette_tint": (0, 0, 0), "grain": 0.02,
           "grain_freq": 1.2, "texture": "none", "texture_alpha": 0.0}
    typ = {"family": "sans", "scale": {}, "line_gap": 1.3, "tracking": 0.0}
    lay = {"layout": "left-stack", "card": {}, "safe_bias": 0}
    mo = {"ease": "out3", "entrance_dx": 0, "entrance_dy": 30,
          "push": 0.04, "item_stagger": 0.3, "transition": "dip"}
    hud = {"style": "chip-left"}
    probe = ST._theme("probe", "探针", (), pal, mat, typ, lay, mo, hud,
                      {"luma_max": 0.045})
    return tuple(probe.keys())


# 模块加载时立刻算出来：它只是把构造器跑一遍取键名，没有副作用。
# （占位成空元组而忘了回填，所有 design 都会报"主题字段集合不一致"——
#   断言本身是对的，错在参照物是空的。）
_THEME_FIELD_KEYS = _theme_field_keys()


_VISUAL_KEYS = ("palette", "family", "typography", "motif", "transition", "motion",
                "bias", "layout", "layout_cycle", "hud", "size", "fps", "subtitle")
# 字幕参数：字号 / 位置 / 背板 / 圆角若全写死在渲染器里，
# **任何两条片子**的字幕带都一模一样 —— 恰好是原则 5 说的"大面积长时间重复"。
# 这些参数进设计 DSL（可选，缺省 = 最朴素的字幕带，不写 subtitle 的 design.json 逐像素不变）。
_SUBTITLE_DEFAULTS = {
    "size": 46, "y": None, "align": "center", "plate_alpha": 170,
    "radius": 10, "family": None, "color": None, "max_lines": 2,
}
_SUBTITLE_KEYS = tuple(_SUBTITLE_DEFAULTS.keys())
_SUBTITLE_ALIGNS = ("center", "left")
_FONT_FAMILIES_FOR_SUB = ("sans", "serif", "kai", "round")


def subtitle_of(design):
    """design -> 补齐默认值后的字幕参数（渲染侧只需读这个，不要各写一份默认值）。"""
    out = dict(_SUBTITLE_DEFAULTS)
    vis = design.get("visual") if isinstance(design, dict) else None
    sub = vis.get("subtitle") if isinstance(vis, dict) else None
    if isinstance(sub, dict):
        for k in _SUBTITLE_KEYS:
            if k in sub and sub[k] is not None:
                out[k] = sub[k]
    return out
_TYPO_KEYS = ("tracking", "line_gap", "number", "ratio", "align")
_MOTIF_KEYS = ("kind", "density", "detail")
_TRANS_KEYS = ("kind", "dur")
_MOTION_KEYS = ("ease", "enter", "push", "breathe_hz")
_BIAS_KEYS = ("x", "y", "inset")
_AUDIO_KEYS = ("voice", "music", "sfx")
_VOICE_KEYS = ("mode", "id", "rate", "pitch", "map", "jitter", "max_distinct")
_MUSIC_KEYS = ("mode", "arranger", "style", "scale", "bpm", "key", "mood", "seed",
               "gain_db", "duck_db", "sections", "file")
_SFX_KEYS = ("enabled", "density", "gain_db", "cues")
_PACING_KEYS = ("lead", "tail", "min_dur", "hold", "item_stagger")
_BRIEF_KEYS = ("topic", "duration_target", "duration_mode", "must_have", "avoid")

_MAX_RESAMPLE = 50


# ============================================================================
# 二、小工具：确定性哈希 / 颜色 / 类型
# ============================================================================


def _digest(*parts):
    """sha1 派生的稳定 48 位整数（禁 hash()：str hash 每次进程带随机盐）。"""
    raw = "|".join(str(p) for p in parts).encode("utf-8")
    return int(hashlib.sha1(raw).hexdigest()[:12], 16)


def _canonical(obj):
    """规范 JSON：排序键 + 紧凑分隔符；同一对象永远同一串（指纹与采样共用）。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _clamp(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


def _hsv(hue_deg, s, v):
    """HSV -> RGB 元组；色相用角度（0~360），s/v 用 0~1。"""
    r, g, b = colorsys.hsv_to_rgb((float(hue_deg) % 360.0) / 360.0,
                                  _clamp(float(s), 0.0, 1.0),
                                  _clamp(float(v), 0.0, 1.0))
    return (int(round(r * 255.0)), int(round(g * 255.0)), int(round(b * 255.0)))


def _hex(rgb):
    """(r,g,b) -> "#rrggbb"（只用于展示/距离描述）。"""
    try:
        t = ST._color_of(rgb, "display")
    except Exception:                       # noqa: BLE001 - 展示用，坏值返回空串
        return ""
    return "#%02x%02x%02x" % t


def _slug(text):
    s = re.sub(r"[^a-z0-9._-]+", "-", str(text or "").lower()).strip("-")
    return s or "design"


# ============================================================================
# 三、配色求解：色相锚点 + (s,v) 网格（实测唯一 100% 通过的做法）
# ============================================================================


def _solve_role(hue, s0, v0, need, refs):
    """在 s,v ∈ {0,0.1,...,1.0} 里找"对 refs 都 >= need"且最接近 (s0,v0) 的颜色。

    为什么是网格而不是二分：对比度对 s、v 并非单维单调（v 到 0/1 两端都能拉高对比），
    单维二分只能找到一个可行点，找不到**最贴近设计意图**的那个。11×11 网格足够密，
    且并列时取先遇到者（s 升序、v 升序），结果确定。
    """
    best, best_d = None, None
    for si in range(11):
        s = si / 10.0
        for vi in range(11):
            v = vi / 10.0
            c = _hsv(hue, s, v)
            if min(ST.contrast(c, r) for r in refs) < need:
                continue
            d = abs(s - s0) + abs(v - v0)
            if best is None or d < best_d - 1e-12:
                best, best_d = c, d
    return best


def _solve_palette_retry(family, hue_shift=0.0, target_shift=0.0):
    """求解失败时横向让开色相重试（最多 12 档、每档 7.5°），仍失败抛原错误。

    为什么需要：明亮活泼档（浅底 + 带色）在 0–20° 红橙带上无解——浅底红橙上
    ink 的对比度到不了门槛。这不是设计错误，是这一档的**局部不可行**；
    让色相往旁边挪一点仍然落在"明亮活泼"的语义里（用户要的是气质，不是某个精确色相）。
    失败档数是确定性的（按 k 递增、先正后负），所以同 seed 可复现。

    返回 {"palette", "family", "shift"}：family 是**实际用到**的 (h,s,v,dark)——
    调用方必须用它重算标签与色相差，否则 label 会与实际底色对不上。
    """
    hue, sat, val, dark = family[0], family[1], family[2], family[3]
    last = None
    for k in range(12):
        if k == 0:
            dh = 0.0
        else:
            step = 7.5 * ((k + 1) // 2)
            dh = step if k % 2 else -step
        try:
            pal = _solve_palette((float(hue) + dh, sat, val, dark), hue_shift, target_shift)
        except ValueError as e:
            last = e
            continue
        return {"palette": pal, "shift": dh,
                "family": (float(hue) + dh, sat, val, dark)}
    raise last if last is not None else ValueError("配色求解失败：%r" % (family,))


def _solve_palette(family, hue_shift=0.0, target_shift=0.0):
    """一个配色锚点 -> 11 个颜色（语义色全部求解；无解直接报错，不回落）。

    hue_shift / target_shift 是采样器给不同候选留的小扰动（确定性，幅度很小），
    让"同族不同设计"也有色相差异；扰动不会改变可行性（网格里始终有 v=0/v=1 的端点）。
    """
    # 只接受 (色相, 饱和度, 明度, 暗色)，不接受命名族（"deep-blue" 之类）：
    # design.json 里存的是**算好的 RGB**，从不存名字，
    # 所以"design 带名字"这件事根本不存在——留着分支只会在别处误导人。
    if not isinstance(family, (tuple, list)) or len(family) < 4:
        raise ValueError("配色必须是 (色相, 饱和度, 明度, 暗色) 四元组；收到 %r" % (family,))
    hue, sat, val = float(family[0]), float(family[1]), float(family[2])
    dark = bool(family[3])
    bg, bg2, surface = _palette_from_hue(hue, sat, val, dark)
    refs = (bg, bg2)
    colors = {"bg": bg, "bg2": bg2, "surface": surface}
    lost = []
    for role in ("ink", "muted", "dim", "accent", "accent2"):
        s0, v0 = _ROLE_INTENT[dark][role]
        c = _solve_role(hue + _ROLE_HUE[role] + hue_shift,
                        _clamp(s0 + target_shift, 0.0, 1.0),
                        _clamp(v0 - target_shift, 0.0, 1.0),
                        ST.LINT_LIMITS[role], refs)
        if c is None:
            lost.append(role)
        else:
            colors[role] = c
    for role, hue in (("good", _GOOD_HUE), ("bad", _BAD_HUE)):
        c = _solve_role(hue, 0.62, 0.66, ST.LINT_LIMITS[role], (surface,))
        if c is None:
            lost.append(role)
        else:
            colors[role] = c
    if lost:
        raise ValueError("配色 (hue=%.1f sat=%.2f val=%.3f dark=%s) 下这些语义色无解"
                         "（对比度不可达）：%s" % (hue, sat, val, dark, "、".join(lost)))
    colors["rule"] = (255, 255, 255) if dark else (16, 16, 16)
    return {k: colors[k] for k in PALETTE_KEYS}


# ============================================================================
# 四、design -> theme / language（35 字段主题；五组视觉签名）
# ============================================================================


def _font_scale(ratio):
    """字号盘：display:body = ratio（visual_lint 查的就是这个比值），其余槽位固定。

    基准 body=34 是本模块的画布假设；ratio 是设计变量，真正决定大标题有多大。
    """
    body = 34
    return {
        "display": (max(20, int(round(body * float(ratio)))), "bold"),
        "heading": (64, "bold"),
        "sub": (38, "light"),
        "body": (body, "reg"),
        "kicker": (28, "bold"),
        "chip": (27, "reg"),
        "brand": (23, "reg"),
    }


def _card_for(motif_kind):
    got = _CARD_BY_MOTIF.get(str(motif_kind))
    if got is None:
        raise ValueError("母题 %r 没有卡片几何（designkit 的 _CARD_BY_MOTIF 要跟着 "
                         "stylekit.VISUAL_MOTIFS 更新）" % (motif_kind,))
    return dict(got)


def _safe_bias(bias):
    # 主题的 safe_bias 是题目安全区的整体偏移；把它挂到设计重心上，避免两处各写一份。
    return int(_clamp(round(float(bias["x"]) * 0.12), -28, 28))


def _theme_id(design_id):
    # 设计 id 已经是 "design-xxxxxxxx"；这里只做字符规整，不叠前缀。
    return _slug(design_id)


def _theme_of_design(design):
    """把 design.visual 展开成 35 字段主题（颜色是 (r,g,b) 元组）。

    直接调 stylekit._theme：字段集合/默认口径只有一份，本文件不手搓 35 个键。
    随后校验"字段集合与 stylekit 完全一致"，防止 stylekit 加字段后这里悄悄少一个。
    """
    vis = design["visual"]
    pal = vis["palette"]
    colors = {k: ST._color_of(pal[k], "visual.palette." + k) for k in PALETTE_KEYS}
    dark = ST.is_dark({"bg": colors["bg"]})
    if dark:
        material = dict(vignette=0.42, vignette_tint=(0, 0, 0),
                        grain=5.0, grain_freq=0.31,
                        texture=_TEXTURE_BY_MOTIF[vis["motif"]["kind"]],
                        texture_alpha=(0.0 if _TEXTURE_BY_MOTIF[vis["motif"]["kind"]] == "none"
                                       else 0.045))
    else:
        material = dict(vignette=0.16,
                        vignette_tint=tuple(int(round(c * 0.86)) for c in colors["bg2"]),
                        grain=3.0, grain_freq=0.27,
                        texture=_TEXTURE_BY_MOTIF[vis["motif"]["kind"]],
                        texture_alpha=(0.0 if _TEXTURE_BY_MOTIF[vis["motif"]["kind"]] == "none"
                                       else 0.030))
    qa = dict(luma_max=(0.030 if ST.luminance(colors["bg"]) < 0.02 else 0.045),
              flicker_thresh=6.0, dark=bool(dark))
    mo = vis["motion"]
    th = ST._theme(
        _theme_id(design["id"]),
        str(design["label"]),
        (),
        colors,
        material,
        dict(family=vis["family"], scale=_font_scale(vis["typography"]["ratio"]),
             line_gap=float(vis["typography"]["line_gap"]), tracking=0.0),
        dict(layout=vis["layout"], card=_card_for(vis["motif"]["kind"]),
             safe_bias=_safe_bias(vis["bias"])),
        dict(ease=mo["ease"],
             entrance_dx=0,
             entrance_dy=int(round(14.0 + float(mo["push"]) * 100.0)),
             push=float(mo["push"]),
             item_stagger=float(design["pacing"]["item_stagger"]),
             transition=vis["transition"]["kind"]),
        dict(style=vis["hud"]),
        qa,
    )
    # 主题的字段清单：没有内置主题可参照，从构造器现场造一份样板取键。
    # 为什么还要求"字段完全一致"：scenes.py 按固定键名取色，少一个键就是 KeyError，
    # 多一个键则说明构造器与消费端脱节 —— 两边必须严丝合缝。
    ref = set(_THEME_FIELD_KEYS)
    if set(th.keys()) != ref:
        raise ValueError("主题字段集合与 stylekit 不一致：缺 %s / 多 %s"
                         % (sorted(ref - set(th.keys())), sorted(set(th.keys()) - ref)))
    return th


def _language_of(design):
    """design -> 五组视觉签名（motion / motif / transition / typography / bias）。

    与 stylekit.VISUAL_LANGUAGES 同形；scenes.py 直接读这五组（layout_cycle 没有消费者）。
    所有字段都直接下标：缺字段会抛 KeyError，由 validate_design 转成一条问题，
    绝不在这里塞默认值。
    """
    vis = design["visual"]
    ty = vis["typography"]
    mo = vis["motion"]
    return {
        "motion": {"ease": mo["ease"],
                   "enter": (float(mo["enter"][0]), float(mo["enter"][1])),
                   "push": float(mo["push"]),
                   "breathe_hz": float(mo["breathe_hz"])},
        "motif": {"kind": vis["motif"]["kind"],
                  "density": float(vis["motif"]["density"]),
                  "detail": dict(vis["motif"]["detail"])},
        "transition": {"kind": vis["transition"]["kind"],
                       "dur": float(vis["transition"]["dur"])},
        "typography": {"tracking": float(ty["tracking"]),
                       "line_gap": float(ty["line_gap"]),
                       "align": ty["align"],
                       "number": ty["number"],
                       "ratio": float(ty["ratio"])},
        "bias": {"x": int(vis["bias"]["x"]),
                 "y": int(vis["bias"]["y"]),
                 "inset": int(vis["bias"]["inset"])},
    }


_VISUAL_PROBE_ID = "__designkit_probe__"


def _visual_lint_language(lang):
    """对任意 language 字典做体检（直接调纯函数，不往 stylekit 的表里临时注册）。"""
    return ST.visual_lint_language(lang)


# ============================================================================
# 五、校验（fail-closed：缺字段/越界/对比度不达标全部变成问题）
# ============================================================================


def _need(obj, keys, path, bad):
    for k in keys:
        if k not in obj:
            bad.append("%s 缺必填字段 %r（fail-closed：不会取默认值）" % (path, k))


def _only(obj, allowed, path, bad):
    for k in sorted(obj, key=str):
        if k not in allowed:
            bad.append("%s 有未知字段 %r（设计契约不认这个键）" % (path, k))


def _num(v, path, bad, lo=None, hi=None):
    if not _is_num(v):
        bad.append("%s 必须是数字，收到 %r" % (path, v))
        return False
    if lo is not None and float(v) < lo:
        bad.append("%s=%r 不能小于 %s" % (path, v, lo))
        return False
    if hi is not None and float(v) > hi:
        bad.append("%s=%r 不能大于 %s" % (path, v, hi))
        return False
    return True


def _int_of(v, path, bad, lo=None, hi=None):
    if not _is_int(v):
        bad.append("%s 必须是整数，收到 %r" % (path, v))
        return False
    if lo is not None and v < lo:
        bad.append("%s=%r 不能小于 %s" % (path, v, lo))
        return False
    if hi is not None and v > hi:
        bad.append("%s=%r 不能大于 %s" % (path, v, hi))
        return False
    return True


def _enum_of(v, allowed, path, bad):
    if v not in allowed:
        bad.append("%s=%r 不在 %s 里" % (path, v, " / ".join(allowed)))
        return False
    return True


def _validate_voice(vo, bad):
    """voice 允许两种形态：裸 edge id 字符串，或 {mode,id,map,jitter,max_distinct}。"""
    if isinstance(vo, str):
        if not _VOICE_RE.match(vo):
            bad.append("audio.voice=%r 不是 edge voice id（必须匹配 zh-CN-XxxNeural）" % (vo,))
        return
    if not isinstance(vo, dict):
        bad.append("audio.voice 必须是字符串或 dict，收到 %r" % (type(vo).__name__,))
        return
    _only(vo, _VOICE_KEYS, "audio.voice", bad)
    if "mode" not in vo:
        bad.append("audio.voice 缺必填字段 'mode'（fail-closed：不会猜 edge_id/wav）")
        return
    mode = vo.get("mode")
    if mode not in ("edge_id", "wav"):
        bad.append("audio.voice.mode=%r 必须是 edge_id / wav" % (mode,))
        return
    vid = vo.get("id")
    if not isinstance(vid, str) or not vid.strip():
        bad.append("audio.voice.id 必须是非空字符串")
    elif mode == "edge_id" and not _VOICE_RE.match(vid):
        bad.append("audio.voice.id=%r 不是 edge voice id（必须匹配 zh-CN-XxxNeural）" % (vid,))
    if mode == "edge_id":
        _need(vo, ("map", "jitter", "max_distinct"), "audio.voice", bad)
    if "map" in vo:
        mp = vo["map"]
        if not isinstance(mp, dict):
            bad.append("audio.voice.map 必须是 dict")
        else:
            for k in sorted(mp, key=str):
                v = mp[k]
                if isinstance(v, str):
                    if not _VOICE_RE.match(v):
                        bad.append("audio.voice.map[%r]=%r 不是 edge voice id" % (k, v))
                elif isinstance(v, dict):
                    _only(v, ("mode", "id"), "audio.voice.map.%s" % k, bad)
                    if v.get("mode") not in ("edge_id", "wav"):
                        bad.append("audio.voice.map[%r].mode 必须是 edge_id / wav" % (k,))
                    if not isinstance(v.get("id"), str) or not v.get("id"):
                        bad.append("audio.voice.map[%r].id 必须是非空字符串" % (k,))
                    elif v.get("mode") == "edge_id" and not _VOICE_RE.match(str(v["id"])):
                        bad.append("audio.voice.map[%r].id=%r 不是 edge voice id"
                                   % (k, v.get("id")))
                else:
                    bad.append("audio.voice.map[%r] 必须是 voice id 或 {mode,id}" % (k,))
    if "jitter" in vo:
        _num(vo["jitter"], "audio.voice.jitter", bad, 0.0, 1.0)
    if "max_distinct" in vo:
        _int_of(vo["max_distinct"], "audio.voice.max_distinct", bad, 1, 5)
    for k in ("rate", "pitch"):
        if k in vo and not isinstance(vo[k], str):
            bad.append("audio.voice.%s 必须是字符串（如 \"+0%%\" / \"+0Hz\"）" % k)


def _validate_music(mu, bad):
    if not isinstance(mu, dict):
        bad.append("audio.music 必须是 dict")
        return
    _only(mu, _MUSIC_KEYS, "audio.music", bad)
    if "mode" not in mu:
        bad.append("audio.music 缺必填字段 'mode'（fail-closed：不会猜 params/file/off）")
        return
    mode = mu.get("mode")
    if mode not in ("params", "file", "off"):
        bad.append("audio.music.mode=%r 必须是 params / file / off" % (mode,))
    # **不要求 style**，也不限定成任何内置名。
    # 配乐由 musicgen 现场编排，它吃的是 bpm/key/scale/mood/seed + 可选的
    # progressions/motif/drums/voices —— 没有"曲风"这个概念。
    # design.json 里残留的 style 字段仍被接受（不校验取值），但不参与渲染，
    # 避免"带了这个键的设计全部报错"。
    _need(mu, ("scale", "bpm", "key", "mood", "seed"), "audio.music", bad)
    if "style" in mu and not isinstance(mu["style"], str):
        bad.append("audio.music.style 若存在必须是字符串（不参与渲染）")
    if "scale" in mu:
        _enum_of(mu["scale"], SCALES, "audio.music.scale", bad)
    if "mood" in mu:
        _enum_of(mu["mood"], MOODS, "audio.music.mood", bad)
    if "bpm" in mu:
        _num(mu["bpm"], "audio.music.bpm", bad, 40.0, 220.0)
    if "key" in mu and isinstance(mu["key"], str):
        scale = mu.get("scale")
        if scale in SCALES:
            pool = ST.MINOR_KEYS if scale == "minor" else ST.MAJOR_KEYS
            if mu["key"] not in pool:
                bad.append("audio.music.key=%r 与 scale=%r 不匹配（%s 调池：%s）"
                           % (mu["key"], scale, scale, " / ".join(pool)))
    elif "key" in mu:
        bad.append("audio.music.key 必须是字符串")
    if "seed" in mu:
        _int_of(mu["seed"], "audio.music.seed", bad)
    if mode == "file" and not (isinstance(mu.get("file"), str) and mu.get("file")):
        bad.append("audio.music.mode=file 时必须给非空 audio.music.file")
    if "gain_db" in mu:
        _num(mu["gain_db"], "audio.music.gain_db", bad, -60.0, 12.0)
    if "duck_db" in mu:
        _num(mu["duck_db"], "audio.music.duck_db", bad, 0.0, 24.0)
    if "arranger" in mu and not isinstance(mu["arranger"], str):
        bad.append("audio.music.arranger 必须是字符串")
    if "sections" in mu:
        sec = mu["sections"]
        if not isinstance(sec, (list, tuple)) or any(not isinstance(x, dict) for x in sec):
            bad.append("audio.music.sections 必须是对象数组")


def _validate_sfx(sfx, bad):
    if not isinstance(sfx, dict):
        bad.append("audio.sfx 必须是 dict")
        return
    _only(sfx, _SFX_KEYS, "audio.sfx", bad)
    if "enabled" in sfx and not isinstance(sfx["enabled"], bool):
        bad.append("audio.sfx.enabled 必须是布尔")
    if "density" in sfx:
        _enum_of(sfx["density"], SFX_DENSITIES, "audio.sfx.density", bad)
    if "gain_db" in sfx:
        _num(sfx["gain_db"], "audio.sfx.gain_db", bad, -40.0, 12.0)
    if "cues" in sfx:
        cues = sfx["cues"]
        if not isinstance(cues, (list, tuple)) or any(not isinstance(x, dict) for x in cues):
            bad.append("audio.sfx.cues 必须是对象数组")


def validate_design(design):
    """fail-closed 校验。返回问题列表（空 = 通过）。

    覆盖：
      1. schema 版本 / id / label / pitch / 四个必填块；
      2. 枚举：motif.kind / transition.kind / typography.number / ease / scale / mood
         （以及 family / align / layout / hud / size / fps / 声线格式）；
      3. 数值边界：**直接调 stylekit.visual_lint**（临时语言包），与阈值同源；
      4. 配色：把 design 展开成 35 字段主题后调 stylekit.theme_lint（不自己写对比度）；
      5. 任何"缺了就取默认值"的路径都在这里变成一条问题。
    """
    bad = []
    if not isinstance(design, dict):
        return ["design 不是 dict：%r" % (design,)]

    _need(design, ("schema", "id", "label", "pitch", "structure", "visual", "audio", "pacing"),
          "design", bad)
    _only(design, _TOP_KEYS, "design", bad)

    um = design.get("_unmatched_constraints")
    if um is not None and (not isinstance(um, (list, tuple))
                           or any(not isinstance(x, str) for x in um)):
        bad.append("_unmatched_constraints 必须是字符串数组（认不出的 brief 要求）")

    sch = design.get("schema")
    if not _is_int(sch):
        bad.append("schema 必须是整数 %d，收到 %r" % (SCHEMA, sch))
    elif sch != SCHEMA:
        bad.append("schema 版本不支持：%r（本模块只认 %d）" % (sch, SCHEMA))

    did = design.get("id")
    if not isinstance(did, str) or not re.match(r"^[a-z0-9][a-z0-9._-]{1,63}$", did):
        bad.append("id=%r 必须匹配 ^[a-z0-9][a-z0-9._-]{1,63}$" % (did,))
    label = design.get("label")
    if not isinstance(label, str) or not label.strip():
        bad.append("label 必须是非空字符串")
    elif len(label) > 40:
        bad.append("label 超过 40 字（当前 %d）" % len(label))
    pitch = design.get("pitch")
    if not isinstance(pitch, str) or not pitch.strip():
        bad.append("pitch 必须是非空字符串（一句话立意）")
    elif len(pitch) > 120:
        bad.append("pitch 超过 120 字（当前 %d）" % len(pitch))

    # ---- structure ----
    st = design.get("structure")
    if not isinstance(st, dict):
        bad.append("structure 必须是 dict")
    else:
        _only(st, _STRUCT_KEYS, "structure", bad)
        _need(st, ("segments",), "structure", bad)
        if "segments" in st:
            _int_of(st["segments"], "structure.segments", bad, 1, 40)
        if "beats" in st:
            beats = st["beats"]
            if not isinstance(beats, (list, tuple)) or any(not isinstance(x, str) for x in beats):
                bad.append("structure.beats 必须是字符串数组")
        if "hold_policy" in st:
            _enum_of(st["hold_policy"], HOLD_POLICIES, "structure.hold_policy", bad)
        if "target_sec" in st and st["target_sec"] is not None:
            _num(st["target_sec"], "structure.target_sec", bad, 3.0, 1800.0)
        if st.get("target_mode") is not None:
            _enum_of(st["target_mode"], ("exact", "at_least"), "structure.target_mode", bad)

    # ---- visual ----
    vis = design.get("visual")
    if not isinstance(vis, dict):
        bad.append("visual 必须是 dict")
        vis = {}
    else:
        _only(vis, _VISUAL_KEYS, "visual", bad)
        _need(vis, ("palette", "family", "typography", "motif", "transition", "motion",
                    "bias", "layout", "hud"), "visual", bad)

    pal = vis.get("palette")
    if not isinstance(pal, dict):
        bad.append("visual.palette 必须是 dict（11 色）")
    else:
        _only(pal, PALETTE_KEYS, "visual.palette", bad)
        _need(pal, PALETTE_KEYS, "visual.palette", bad)
        for k in PALETTE_KEYS:
            if k not in pal:
                continue
            try:
                ST._color_of(pal[k], "visual.palette." + k)
            except Exception as e:                       # noqa: BLE001
                bad.append("visual.palette.%s 不是颜色（[r,g,b] 或 \"#rrggbb\"）：%s"
                           % (k, e))

    if "family" in vis:
        _enum_of(vis["family"], FONT_FAMILIES, "visual.family", bad)

    ty = vis.get("typography")
    if not isinstance(ty, dict):
        bad.append("visual.typography 必须是 dict")
    else:
        _only(ty, _TYPO_KEYS, "visual.typography", bad)
        _need(ty, ("tracking", "line_gap", "number", "ratio", "align"),
              "visual.typography", bad)
        for k in ("tracking", "line_gap", "ratio"):
            if k in ty:
                _num(ty[k], "visual.typography." + k, bad)
        if "number" in ty:
            _enum_of(ty["number"], NUMBERS, "visual.typography.number", bad)
        if "align" in ty:
            _enum_of(ty["align"], ALIGNS, "visual.typography.align", bad)

    mo = vis.get("motif")
    if not isinstance(mo, dict):
        bad.append("visual.motif 必须是 dict")
    else:
        _only(mo, _MOTIF_KEYS, "visual.motif", bad)
        _need(mo, _MOTIF_KEYS, "visual.motif", bad)
        if "kind" in mo:
            _enum_of(mo["kind"], MOTIFS, "visual.motif.kind", bad)
        if "density" in mo:
            _num(mo["density"], "visual.motif.density", bad, 0.0, 1.0)
        if "detail" in mo and not isinstance(mo["detail"], dict):
            bad.append("visual.motif.detail 必须是 dict（可以是空 {}）")

    tr = vis.get("transition")
    if not isinstance(tr, dict):
        bad.append("visual.transition 必须是 dict")
    else:
        _only(tr, _TRANS_KEYS, "visual.transition", bad)
        _need(tr, _TRANS_KEYS, "visual.transition", bad)
        if "kind" in tr:
            _enum_of(tr["kind"], TRANSITIONS, "visual.transition.kind", bad)
        if "dur" in tr:
            _num(tr["dur"], "visual.transition.dur", bad, 0.0, 0.60)

    mo2 = vis.get("motion")
    if not isinstance(mo2, dict):
        bad.append("visual.motion 必须是 dict")
    else:
        _only(mo2, _MOTION_KEYS, "visual.motion", bad)
        _need(mo2, _MOTION_KEYS, "visual.motion", bad)
        if "ease" in mo2:
            _enum_of(mo2["ease"], EASES, "visual.motion.ease", bad)
        for k in ("push", "breathe_hz"):
            if k in mo2:
                _num(mo2[k], "visual.motion." + k, bad)
        if "enter" in mo2:
            ent = mo2["enter"]
            if (not isinstance(ent, (list, tuple)) or len(ent) != 2
                    or any(not _is_num(x) for x in ent)):
                bad.append("visual.motion.enter 必须是两个数字 [起, 止]")

    bias = vis.get("bias")
    if not isinstance(bias, dict):
        bad.append("visual.bias 必须是 dict")
    else:
        _only(bias, _BIAS_KEYS, "visual.bias", bad)
        _need(bias, _BIAS_KEYS, "visual.bias", bad)
        for k in ("x", "y"):
            if k in bias:
                _int_of(bias[k], "visual.bias." + k, bad, -240, 240)
        if "inset" in bias:
            _int_of(bias["inset"], "visual.bias.inset", bad, 0, 120)

    if "layout" in vis:
        _enum_of(vis["layout"], LAYOUTS, "visual.layout", bad)
    if "hud" in vis:
        _enum_of(vis["hud"], HUDS, "visual.hud", bad)
    if "size" in vis:
        _enum_of(vis["size"], SIZES, "visual.size", bad)
    if "fps" in vis:
        if not _is_int(vis["fps"]):
            bad.append("visual.fps 必须是整数")
        elif vis["fps"] not in FPSES:
            bad.append("visual.fps=%r 不在 %s 里" % (vis["fps"], FPSES))

    # 版式轮换：片内按章节轮换版式，防"全片一张版式"。
    # 不写 = 由 design_to_plan 按设计指纹自动派生一套轮换（plan 里没有 layout_cycle
    # 时 grammar 仍走 layout_default，与不写这个字段等价）。
    if "layout_cycle" in vis and vis["layout_cycle"] is not None:
        cyc = vis["layout_cycle"]
        if not isinstance(cyc, (list, tuple)) or not cyc:
            bad.append("visual.layout_cycle 必须是非空字符串数组（元素取自 %s）"
                       % " / ".join(LAYOUTS))
        else:
            for i, name in enumerate(cyc):
                if name not in LAYOUTS:
                    bad.append("visual.layout_cycle[%d]=%r 不是可用版式（可用：%s）"
                               % (i, name, " / ".join(LAYOUTS)))
            if len(set(str(x) for x in cyc)) < 2:
                bad.append("visual.layout_cycle 至少要有 2 种不同版式（只有一种等于不轮换）")

    # 字幕样式：可选；不写 = 最朴素字幕带的默认值（逐像素不变）。
    sub = vis.get("subtitle")
    if sub is not None:
        if not isinstance(sub, dict):
            bad.append("visual.subtitle 必须是 dict")
        else:
            _only(sub, _SUBTITLE_KEYS, "visual.subtitle", bad)
            if "size" in sub and sub["size"] is not None:
                _int_of(sub["size"], "visual.subtitle.size", bad, 20, 96)
            if "y" in sub and sub["y"] is not None:
                _int_of(sub["y"], "visual.subtitle.y", bad, 0, 1030)
            if "align" in sub and sub["align"] is not None:
                _enum_of(sub["align"], _SUBTITLE_ALIGNS, "visual.subtitle.align", bad)
            if "plate_alpha" in sub and sub["plate_alpha"] is not None:
                _int_of(sub["plate_alpha"], "visual.subtitle.plate_alpha", bad, 0, 255)
            if "radius" in sub and sub["radius"] is not None:
                _int_of(sub["radius"], "visual.subtitle.radius", bad, 0, 48)
            if "family" in sub and sub["family"] is not None:
                _enum_of(sub["family"], _FONT_FAMILIES_FOR_SUB, "visual.subtitle.family", bad)
            if "color" in sub and sub["color"] is not None:
                try:
                    ST._color_of(sub["color"], "visual.subtitle.color")
                except Exception as e:                   # noqa: BLE001
                    bad.append("visual.subtitle.color 不是颜色（[r,g,b] 或 \"#rrggbb\"）：%s" % e)
            if "max_lines" in sub and sub["max_lines"] is not None:
                _int_of(sub["max_lines"], "visual.subtitle.max_lines", bad, 1, 3)

    # ---- audio ----
    au = design.get("audio")
    if not isinstance(au, dict):
        bad.append("audio 必须是 dict")
    else:
        _only(au, _AUDIO_KEYS, "audio", bad)
        _need(au, ("voice", "music"), "audio", bad)
        if "voice" in au:
            _validate_voice(au["voice"], bad)
        if "music" in au:
            _validate_music(au["music"], bad)
        if "sfx" in au:
            _validate_sfx(au["sfx"], bad)

    # ---- pacing ----
    pa = design.get("pacing")
    if not isinstance(pa, dict):
        bad.append("pacing 必须是 dict")
    else:
        _only(pa, _PACING_KEYS, "pacing", bad)
        _need(pa, _PACING_KEYS, "pacing", bad)
        for k, lo, hi in (("lead", 0.0, 3.0), ("tail", 0.0, 3.0),
                          ("min_dur", 1.0, 12.0), ("hold", 0.0, 5.0),
                          ("item_stagger", 0.05, 1.2)):
            if k in pa:
                _num(pa[k], "pacing." + k, bad, lo, hi)

    # ---- 同源数值门禁：stylekit.visual_lint（临时语言包；门槛只有一份） ----
    try:
        lang = _language_of(design)
    except Exception as e:                                # noqa: BLE001
        bad.append("无法构造视觉语言包（visual_lint 没跑）：%s" % e)
    else:
        for p in _visual_lint_language(lang):
            bad.append("visual: " + p)

    # ---- 同源配色门禁：stylekit.theme_lint（不自己写对比度） ----
    try:
        th = _theme_of_design(design)
        for p in ST.theme_lint(th):
            bad.append("theme: " + p)
    except Exception as e:                                # noqa: BLE001
        bad.append("主题展开失败（theme_lint 没跑）：%s" % e)

    # 去重但保持首次出现的顺序（同一条缺字段会被多个检查提到）
    seen, out = set(), []
    for s in bad:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


# ============================================================================
# 六、design -> plan（scenes.draw_frame 直接可吃）
# ============================================================================


def _fingerprint_part(obj):
    raw = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def design_to_plan(design):
    """设计 -> 风格计划（形状对齐 stylekit.resolve_script_style 的返回）。

    theme 必须通过 stylekit.theme_lint；不通过 raise ValueError（带全部问题）。
    渲染侧把返回值挂到 script["_style"]，scenes._plan 会直接用它。
    """
    bad = validate_design(design)
    if bad:
        raise ValueError("设计没过校验（%d 条）：\n  - %s" % (len(bad), "\n  - ".join(bad)))

    th = _theme_of_design(design)
    lint = ST.theme_lint(th)
    if lint:
        raise ValueError("主题没过 stylekit.theme_lint（%d 条）：\n  - %s"
                         % (len(lint), "\n  - ".join(lint)))

    vis = design["visual"]
    au = design["audio"]
    pa = design["pacing"]
    mu = au["music"]
    lang = _language_of(design)

    seed = int(mu["seed"])
    fp = design_fingerprint(design)
    seed_effective = (seed * 1000003 + int(fp, 16)) % (2 ** 31 - 1)

    vo = au["voice"]
    if isinstance(vo, str):
        vid = vo
        vmap = {k: vid for k in VOICE_KINDS}
        v_jitter, v_maxd, v_auto = 0.0, 1, False
    else:
        vid = str(vo["id"])
        raw_map = vo["map"]
        vmap = {}
        for k in VOICE_KINDS:
            if k in raw_map:
                v = raw_map[k]
                vmap[k] = str(v["id"]) if isinstance(v, dict) else str(v)
        if not vmap:
            # 空 map 的语义 = 四个 kind 都用主声线（不是"缺字段回落"，是契约里写明的意思）
            vmap = {k: vid for k in VOICE_KINDS}
        v_jitter = float(vo["jitter"])
        v_maxd = int(vo["max_distinct"])
        v_auto = True
    if "body" in vmap:
        v_default = vmap["body"]
    else:
        v_default = vid

    return {
        "version": 1,
        "generator": "designkit 1.0",
        "style": "design:%s" % design["id"],
        "style_name": str(design["label"]),
        "style_source": "design",
        "style_reason": [str(design["pitch"])],
        "legacy": False,
        "theme_id": th["id"],
        "theme_label": th["label"],
        "theme": th,
        "language": lang,
        "layout_default": vis["layout"],
        # 片内版式轮换：grammar 在 chapter_layout 为空时按章节轮换这个序列。
        # 没有这个键（设计里不写 layout_cycle）时 grammar 走 layout_default。
        "layout_cycle": list(vis.get("layout_cycle") or []),
        "chapter_layout": {},
        # 字幕样式：render/filmkit 只读这一份，不各自写默认值。
        "subtitle": subtitle_of(design),
        # 目标时长（可能为 null）：只用于"实测 vs 目标"提示，不改变 TTS 决定的真实时长。
        "target_sec": (None if design.get("structure", {}).get("target_sec") is None
                       else float(design["structure"]["target_sec"])),
        "target_mode": str(design.get("structure", {}).get("target_mode") or "exact"),
        "music": {
            # 没有 "style" 这个键 —— 配乐由 musicgen 现场编排，
            # 没有"曲风表"可指向。设计里残留的 style 字段被忽略。
            "scale": mu["scale"],
            "bpm": float(mu["bpm"]),
            "bpm_source": "design",
            "key": mu["key"],
            "key_source": "design",
            "mood": mu["mood"],
            "mood_source": "design",
            "seed": seed,
            "seed_effective": int(seed_effective),
            "seed_from_content": True,
        },
        "voice": {
            "map": vmap,
            "default": v_default,
            "policy": {
                "auto": v_auto,
                "jitter": v_jitter,
                "max_distinct": v_maxd,
                "rate_range": (-15, 12),
                "pitch_range": (-8, 8),
                "cps_band": (3.6, 5.6),
            },
        },
        # ---- 声音参数 ----
        # design.audio.music.gain_db / duck_db 与 audio.sfx.* **必须真的被读**：
        # 只校验不读，用户改了 design.json 里的音量/音效开关，渲染结果一模一样（实测
        # enabled=false 仍出 8 条音效）。所以原样交给 render，由它填进 script 的
        # music/sfx 缺省位（film.json 显式写了就仍以 film.json 为准）。
        "audio_overrides": {
            "music": {k: float(mu[k]) for k in ("gain_db", "duck_db") if mu.get(k) is not None},
            "sfx": {k: (bool(au["sfx"][k]) if k == "enabled" else
                       (str(au["sfx"][k]) if k == "density" else float(au["sfx"][k])))
                    for k in ("enabled", "density", "gain_db")
                    if isinstance(au.get("sfx"), dict) and au["sfx"].get(k) is not None},
        },
        "pacing": {
            "lead": float(pa["lead"]),
            "tail": float(pa["tail"]),
            "min_dur": float(pa["min_dur"]),
            "hold": float(pa["hold"]),
            "item_stagger": float(pa["item_stagger"]),
        },
        "fingerprint": {
            "version": "design-fp-v1",
            "full": fp,
            "tempo": _fingerprint_part({"structure": design.get("structure"),
                                        "pacing": design.get("pacing")}),
            "style": _fingerprint_part({"visual": design.get("visual")}),
        },
    }


# ============================================================================
# 七、指纹 / 距离
# ============================================================================


def design_fingerprint(design):
    """设计指纹：只看"决定长相"的四个块，忽略 id/label/pitch 这类文案。

    为什么要忽略 id：propose.py 会给候选改写 id（A-1a2b3c4d），若把 id 算进指纹，
    同一个设计在 proposal 与 design.json 里会得到两个指纹，确认状态会"假 stale"。
    """
    if isinstance(design, dict):
        payload = {
            "schema": design.get("schema"),
            "structure": design.get("structure"),
            "visual": design.get("visual"),
            "audio": design.get("audio"),
            "pacing": design.get("pacing"),
        }
    else:
        payload = design
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _dig(obj, keys):
    cur = obj
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def _palette_key(design):
    pal = _dig(design, ("visual", "palette"))
    if not isinstance(pal, dict):
        return ""
    return "bg=%s accent=%s" % (_hex(pal.get("bg")), _hex(pal.get("accent")))


def _voice_key(design):
    vo = _dig(design, ("audio", "voice"))
    if isinstance(vo, str):
        return vo
    if isinstance(vo, dict):
        return str(vo.get("id") or "")
    return ""


# 维度顺序固定（展示与 diff 都按它）。前 7 个是任务要求的底线，后 3 个是额外观测项。
def _music_key(design):
    """design -> 配乐身份（"调式/速度档/情绪"复合键）。

    不能读 audio.music.style：那是"配乐风格表"的字段，采样器不产出它，读了这一维
    就**恒为 None**，两个候选永远"配乐相同"，SKILL §0.5 承诺的"候选至少在含配乐
    在内的 2 个维度上不同"会实际少一维（也是重采样失败率偏高的原因之一）。
    签名用参数化配乐真正决定听感的三件事：调式 + BPM 档（±6 归一）+ 情绪。
    """
    mu = _dig(design, ("audio", "music")) or {}
    if not isinstance(mu, dict):
        return None
    bpm = mu.get("bpm")
    try:
        bucket = int(round(float(bpm) / 6.0))
    except (TypeError, ValueError):
        bucket = None
    return "%s/%s/%s" % (mu.get("scale"), bucket, mu.get("mood"))


_AXES = (
    ("palette", _palette_key),
    ("motif", lambda d: _dig(d, ("visual", "motif", "kind"))),
    ("transition", lambda d: _dig(d, ("visual", "transition", "kind"))),
    ("ease", lambda d: _dig(d, ("visual", "motion", "ease"))),
    ("number", lambda d: _dig(d, ("visual", "typography", "number"))),
    ("music", _music_key),
    ("voice", _voice_key),
    ("mood", lambda d: _dig(d, ("audio", "music", "mood"))),
    ("layout", lambda d: _dig(d, ("visual", "layout"))),
    ("scale", lambda d: _dig(d, ("audio", "music", "scale"))),
)


def design_distance(a, b):
    """两个设计在哪些维度上不同（"伪多方案"门禁的判据）。

    返回 {"axes": [维度名...], "n": 不同维度数, "detail": {维度: (a值, b值)}}。
    只看离散签名，不看连续量（ratio/tracking 的细小差异不算另一个人）。
    """
    axes, detail = [], {}
    for name, getter in _AXES:
        va, vb = getter(a), getter(b)
        if va != vb:
            axes.append(name)
            detail[name] = (va, vb)
    return {"axes": axes, "n": len(axes), "detail": detail}


# ============================================================================
# 八、采样器：sha1 派生的确定性采样 + 两两差异自检
# ============================================================================


def _check_brief(brief):
    """brief 的形状校验：topic 必填，其余三个可选；未知键一律报错（fail-closed）。"""
    if not isinstance(brief, dict):
        raise TypeError("brief 必须是 dict，收到 %r" % (type(brief).__name__,))
    extra = [k for k in brief if k not in _BRIEF_KEYS]
    if extra:
        raise ValueError("brief 有未知字段 %s；只认 %s"
                         % (extra, " / ".join(_BRIEF_KEYS)))
    topic = brief.get("topic")
    if not isinstance(topic, str) or not topic.strip():
        raise ValueError("brief.topic 必须是非空字符串（至少告诉采样器在给什么题材做方案）")
    dt = brief.get("duration_target")
    if dt is not None:
        if not _is_num(dt):
            raise ValueError("brief.duration_target 必须是数字或 null，收到 %r" % (dt,))
        if not 3.0 <= float(dt) <= 1800.0:
            raise ValueError("brief.duration_target 必须在 3~1800 秒，收到 %r" % (dt,))
    mode = brief.get("duration_mode")
    if mode is not None and mode not in ("exact", "at_least"):
        raise ValueError("brief.duration_mode 只能是 exact / at_least / null，收到 %r" % (mode,))
    if mode == "at_least" and dt is None:
        raise ValueError("brief.duration_mode=at_least 需要同时给 duration_target")
    for k in ("must_have", "avoid"):
        v = brief.get(k)
        if v is None:
            continue
        if not isinstance(v, (list, tuple)) or any(not isinstance(x, str) for x in v):
            raise ValueError("brief.%s 必须是字符串数组" % k)


def _brief_canon(brief):
    """brief 的规范形态（只含契约字段；JSON 排序键后作为 sha1 的输入）。"""
    dt = brief.get("duration_target")
    return {
        "topic": str(brief["topic"]),
        "duration_target": None if dt is None else float(dt),
        "must_have": [str(x) for x in brief.get("must_have", [])],
        "avoid": [str(x) for x in brief.get("avoid", [])],
    }

# ---------------------------------------------------------------- 约束（真过滤）
# (类别, 取值, 认得的中英文说法)。只有这五类：它们正好各自对应 design 里一个能直接
# 判定的字段——配色族（由 bg/bg2/surface 反查）、母题、转场、情绪、声线性别。
_CONSTRAINT_WORDS = (
    ("palette", "deep-blue", ("深蓝", "deep-blue")),
    ("palette", "near-black", ("近黑", "near-black")),
    ("palette", "cool-mint", ("薄荷", "cool-mint")),
    ("palette", "sand", ("砂金", "sand")),
    ("palette", "slate", ("石青", "slate")),
    ("palette", "forest", ("森林", "forest")),
    ("palette", "warm-paper", ("暖纸", "warm-paper")),
    ("palette", "ink-paper", ("墨纸", "ink-paper")),
    ("motif", "brackets", ("括角", "brackets")),
    ("motif", "hairlines", ("细线", "hairlines")),
    ("motif", "handdrawn", ("手绘", "handdrawn")),
    ("motif", "slabs", ("色块", "slabs")),
    ("motif", "minimal", ("极简", "minimal")),
    ("motif", "letterbox", ("遮幅", "letterbox")),
    ("motif", "signal", ("信号", "signal")),
    ("motif", "rays", ("放射", "rays")),
    ("transition", "dip", ("叠化", "dip")),
    ("transition", "wash", ("晕染", "wash")),
    ("transition", "paper", ("纸卷", "paper")),
    ("transition", "wipe", ("擦入", "wipe")),
    ("transition", "cut", ("硬切", "cut")),
    ("transition", "iris", ("光圈", "iris")),
    ("mood", "calm", ("平静", "calm")),
    ("mood", "reflective", ("回望", "reflective")),
    ("mood", "hopeful", ("希望", "hopeful")),
    ("mood", "steady", ("稳", "steady")),
    ("mood", "driving", ("推进", "driving")),
    ("mood", "tense", ("紧张", "tense")),
    ("mood", "uplifting", ("上扬", "uplifting")),
    ("voice", "male", ("男声", "male")),
    ("voice", "female", ("女声", "female")),
    # 气质（底色明暗/活泼度）——任何题材的推荐默认都是「明亮，活泼」。
    # "明亮"必须比"深色"更短地排在前面：中文长词优先命中，用户只会点选，不会手写。
    ("tone", "bright", ("明亮活泼", "活泼", "明快", "鲜艳", "bright")),
    ("tone", "light", ("明亮", "浅色", "纸感", "干净", "light")),
    ("tone", "dark", ("深色", "暗色", "深底", "近黑底", "dark")),
)

_CATEGORY_VALUES = {
    # 配色没有"可选值清单"（连续色相空间），这里只保留空元组以兼容约束解析路径：
    # brief 里写"深蓝"这类词会走 _CONSTRAINT_WORDS -> 被解释成色相区间（见 _resolve_constraints）。
    "palette": (),
    "motif": tuple(MOTIFS),
    "transition": tuple(TRANSITIONS),
    "mood": tuple(MOODS),
    "voice": ("male", "female"),
    "tone": _TONES,
}
_CATEGORY_WORD = {"palette": "配色", "motif": "母题", "transition": "转场",
                  "mood": "情绪", "voice": "声线", "tone": "气质"}
# 英文按"整词"认：不加边界的话 "warm-paper" 会顺带命中"纸卷/paper"、"female" 会命中"male"。
_ASCII_WORD = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")


def _alias_spans(text_low, alias):
    """alias 在 text 里的所有起点；英文别名要求前后不紧贴字母数字（整词）。"""
    al = alias.lower()
    ascii_alias = al.isascii()
    out, i = [], text_low.find(al)
    while i != -1:
        j = i + len(al)
        ok = True
        if ascii_alias:
            if i > 0 and text_low[i - 1] in _ASCII_WORD:
                ok = False
            if j < len(text_low) and text_low[j] in _ASCII_WORD:
                ok = False
        if ok:
            out.append(i)
        i = text_low.find(al, i + 1)
    return out


def _constraints_in(text):
    """一句话 -> {类别: (取值, 命中的词)}，每类只留最长的那个词。

    为什么要"每类只留最长"：中文里"稳稳推进"同时命中"稳"(steady) 和"推进"(driving)，
    两者在 must_have 里无法同时成立；取长的那条才符合人的本意（他在说"推进"）。
    先按长度全局排序再吃掉重叠区间，"warm-paper"就整段算配色、不再顺带算一次"纸卷"。
    """
    low = str(text or "").lower()
    hits = []
    for order, (cat, val, aliases) in enumerate(_CONSTRAINT_WORDS):
        for alias in aliases:
            for start in _alias_spans(low, alias):
                hits.append((-len(alias), start, order, cat, val, alias))
    hits.sort()
    taken, got = [], {}
    for neg_len, start, _order, cat, val, alias in hits:
        end = start - neg_len
        if any(not (end <= a or start >= b) for a, b in taken):
            continue                        # 落在更长的命中里：是那个词的一部分
        taken.append((start, end))
        got.setdefault(cat, (val, alias))
    return got


def _vocab(cat):
    """某一类认得的词（只列中文，报错信息要一眼能读完）。"""
    return " / ".join(aliases[0] for c, _v, aliases in _CONSTRAINT_WORDS if c == cat)


def _too_narrow(cat, want, banned):
    """"约束太窄"的统一措辞：必须点名是哪条，否则用户不知道该删哪个。"""
    bits = []
    if want is not None:
        bits.append("must_have「%s」把%s锁成 %s" % (want[2], _CATEGORY_WORD[cat], want[1]))
    for _val, word, src in banned:
        bits.append("avoid「%s」又排除了「%s」" % (src, word))
    return ("brief 的约束把%s的可选范围清空了：%s——没有任何设计能同时满足，请放宽其中一条"
            % (_CATEGORY_WORD[cat], "；".join(bits)))


def _resolve_constraints(brief):
    """brief 的 must_have / avoid -> (must, avoid, domain, unmatched)。

    must   {类别: (取值, 命中的词, 原句)}
    avoid  {类别: [(取值, 命中的词, 原句), ...]}
    domain {类别: 允许的取值元组；None = 这一类没被约束（保持完整采样域）}
    unmatched 一句都没认出来的原句（按出现顺序去重）——交给顶层提示用户"这条没生效"
    """
    must_raw = [str(x) for x in (brief.get("must_have") or [])]
    avoid_raw = [str(x) for x in (brief.get("avoid") or [])]
    must, avoid, unmatched = {}, {}, []

    for src in must_raw:
        hits = _constraints_in(src)
        if not hits:
            unmatched.append(src)
            continue
        for cat, (val, word) in hits.items():
            old = must.get(cat)
            if old is not None and old[0] != val:
                # 同类两条不同的要求 = 无解（一个设计只有一族配色/一种母题），
                # 这不是"再采几次碰运气"能救的，必须马上告诉用户是哪两条在打架。
                raise ValueError(
                    "brief.must_have 的约束互斥：%s既要「%s」又要「%s」"
                    "（一个设计只能选一个，没法同时满足）——去掉一条，或把另一条放进 avoid"
                    % (_CATEGORY_WORD[cat], old[1], word))
            must[cat] = (val, word, src)

    for src in avoid_raw:
        hits = _constraints_in(src)
        if not hits:
            if src not in unmatched:
                unmatched.append(src)
            continue
        for cat, (val, word) in hits.items():
            avoid.setdefault(cat, []).append((val, word, src))

    for cat, (val, word, _src) in sorted(must.items()):
        for aval, aword, _asrc in avoid.get(cat, ()):
            if aval == val:
                raise ValueError(
                    "brief 自相矛盾：「%s」在 must_have 里要 %s=%s，"
                    "「%s」又在 avoid 里排除同一个值——没有任何候选能同时满足"
                    % (word, _CATEGORY_WORD[cat], val, aword))

    domain = {}
    for cat, values in _CATEGORY_VALUES.items():
        want = must.get(cat)
        banned = [v for v, _w, _s in avoid.get(cat, ())]
        if cat == "palette":
            # 配色是**连续色相空间**，不能按"枚举过滤"处理（那会直接清空可选范围）。
            # 约束在这里被翻译成色相窗口：要"深蓝"= 采 h≈210±26° 且暗色底；
            # avoid"放射"这类非配色词不会走到这；avoid"薄荷"= 跳过 168° 附近。
            if want is not None and want[0] not in _PALETTE_ANCHOR:
                raise ValueError("配色约束「%s」没有对应的色相锚点" % want[1])
            banned_hues = []
            for bv, bw, _bs in avoid.get(cat, ()):
                if bv not in _PALETTE_ANCHOR:
                    raise ValueError("配色排除词「%s」没有对应的色相锚点" % bw)
                banned_hues.append(_PALETTE_ANCHOR[bv][0])
            if want is None and not banned_hues:
                domain[cat] = None
            else:
                domain[cat] = {"anchor": _PALETTE_ANCHOR[want[0]] if want else None,
                               "banned_hues": tuple(banned_hues),
                               "window": _HUE_WINDOW}
            continue
        allowed = tuple(v for v in values
                        if (want is None or want[0] == v) and v not in banned)
        if not allowed:
            raise ValueError(_too_narrow(cat, want, avoid.get(cat, ())))
        domain[cat] = None if (want is None and not banned) else allowed

    if must_raw and not must:
        # 单条认不出 -> 记录继续；全都认不出 -> 报错。差别在于：后一种情况下用户的
        # 要求被整体忽略却还返回"成功"，比失败更危险（他会以为要求生效了）。
        raise ValueError(
            "brief.must_have 里没有一条能识别：%s\n"
            "  认得的说法：配色 %s / 母题 %s / 转场 %s / 情绪 %s / 声线 %s"
            % ("、".join("「%s」" % s for s in must_raw),
               _vocab("palette"), _vocab("motif"), _vocab("transition"),
               _vocab("mood"), _vocab("voice")))
    return must, avoid, domain, unmatched


def _narrow_hint(must, avoid):
    """采样失败时的附加说明：把收窄了可选域的约束点名（没有约束就返回空，不乱加提示）。"""
    words = [w for _v, w, _s in must.values()]
    for cat in avoid:
        words.extend(w for _v, w, _s in avoid[cat])
    return "" if not words else "；收窄了可选域的约束：%s" % " / ".join(words)


def _voices_for(genders):
    """声线池按性别过滤——"女声"必须落在挑 id 之前，否则只能靠重采样碰运气。

    只从"认得出性别"的声线里挑：响度表以后加了新命名，认不出的那些只是在有性别约束时
    不参与，绝不会因为认不出就被判成违规（那会让整批候选重采样到报错）。
    """
    if not genders:
        return VOICES
    got = tuple(v for v in VOICES if _voice_gender_of(v) in genders)
    if not got:
        raise ValueError("声线池里没有 %s 的声线（VOICES=%s）"
                         % ("/".join(genders), list(VOICES)))
    return got


def _palette_family_of(design):
    """design -> 配色分桶名（反查 bg 的色相与明暗；认不出返回 None）。

    为什么反查而不是在 design 里多存一个 family 字段：design.json 要保持"自足"，
    配色一旦存了桶名，改桶就会让既有设计漂移；底色是采样时原样写进去的，反查无歧义。
    """
    pal = _dig(design, ("visual", "palette"))
    if not isinstance(pal, dict):
        return None
    try:
        bg = tuple(ST._color_of(pal.get("bg"), "visual.palette.bg"))
    except Exception:                                     # noqa: BLE001
        return None
    # "同一个配色"= 底色的**色相 + 明暗**落在同一档。量化到 24° 是为了让
    # "差 3° 的两个色相"算同一种（人眼也分不出），而不是让每个颜色都算新配色。
    try:
        hue, _s, _v = ST.rgb_to_hsv(bg)
    except Exception:                                     # noqa: BLE001
        return None
    return "%s-%02d" % ("dark" if ST.is_dark({"bg": bg}) else "light",
                        int(round(hue / 24.0)) % 15)


def _voice_ids_of(design):
    """design 里用到的全部 edge voice id（主声线 + map 里的每一个）。"""
    vo = _dig(design, ("audio", "voice"))
    if isinstance(vo, str):
        return [vo]
    if not isinstance(vo, dict):
        return []
    ids = []
    if isinstance(vo.get("id"), str):
        ids.append(vo["id"])
    mp = vo.get("map")
    if isinstance(mp, dict):
        for k in sorted(mp, key=str):
            v = mp[k]
            ids.append(v.get("id") if isinstance(v, dict) else v)
    return [str(x) for x in ids if isinstance(x, str) and x]


def _by_id(rows, did):
    """从已接受候选里按 id 取回设计对象（找不到返回一个空 dict：让距离判据退化为"全不同"）。"""
    for row in rows or ():
        if isinstance(row, dict) and row.get("id") == did:
            return row
    return {}


def _design_value(design, cat):
    """design 在某个约束类别上的取值（声线要求"整条片子的声线都是这一性别"）。"""
    if cat == "palette":
        return _palette_family_of(design)
    if cat == "motif":
        return _dig(design, ("visual", "motif", "kind"))
    if cat == "transition":
        return _dig(design, ("visual", "transition", "kind"))
    if cat == "mood":
        return _dig(design, ("audio", "music", "mood"))
    if cat == "voice":
        kinds = {_voice_gender_of(v) for v in _voice_ids_of(design)}
        return kinds.pop() if len(kinds) == 1 else "mixed"
    if cat == "tone":
        return _tone_of(design)
    return None


def _tone_of(design):
    """design -> 气质档（bright / light / dark）；取不到返回 None。

    判据只看底色的**明暗 + 饱和度**：这几项决定了"第一眼亮不亮、活不活泼"，
    也正好是这类要求（"要亮一点""别太冷"）真正指的东西。阈值取了宽的档，
    因为用户说的是"感觉"，不是某个精确 s 值。
    """
    try:
        pal = _dig(design, ("visual", "palette"))
        bg = ST._color_of(pal.get("bg"), "visual.palette.bg")
    except Exception:                                     # noqa: BLE001
        return None
    if ST.is_dark({"bg": bg}):
        return _TONE_DARK
    _h, s, _v = ST.rgb_to_hsv(bg)
    return _TONE_BRIGHT if float(s) >= 0.22 else _TONE_LIGHT


def _constraint_problems(design, must, avoid):
    """按 brief 复核一个候选：返回违反项（空 = 过）。采样器用它兜底，不信"域已经限住"。"""
    bad = []
    for cat, (val, word, _src) in sorted(must.items()):
        if cat == "palette":
            # 配色按**色相窗口**复核（不是"族名相等"）：连续空间里"是不是深蓝"
            # 取决于底色的色相落在 210°±26° 内，而不是某个字符串匹配。
            got_h = _design_bg_hue(design)
            anchor = _PALETTE_ANCHOR.get(val)
            if got_h is None or anchor is None:
                bad.append("配色不可判定（%r / %s）" % (got_h, val))
            elif _hue_gap(got_h, anchor[0]) > _HUE_WINDOW:
                bad.append("配色=%.1f° 不满足 must_have「%s」（要 %s 的色相，即 %.0f°±%.0f°）"
                           % (got_h, word, val, anchor[0], _HUE_WINDOW))
            continue
        if cat == "tone":
            # 「明亮活泼」在复核时接受 bright 与 light 两档：用户说的是"要亮"，
            # 不是"要恰好这个饱和度"。hard _TONE_BRIGHT 留给采样器当推荐默认。
            got_t = _design_value(design, cat)
            ok = (got_t == val) or (val == _TONE_BRIGHT and got_t == _TONE_LIGHT)
            if not ok:
                bad.append("气质=%r 不满足 must_have「%s」（要 %s）"
                           % (got_t, word, val))
            continue
        got = _design_value(design, cat)
        if got != val:
            bad.append("%s=%r 不满足 must_have「%s」（%s 要 %r）"
                       % (_CATEGORY_WORD[cat], got, word, _CATEGORY_WORD[cat], val))
    for cat, rows in sorted(avoid.items()):
        if cat == "palette":
            got_h = _design_bg_hue(design)
            for val, word, _src in rows:
                anchor = _PALETTE_ANCHOR.get(val)
                if got_h is not None and anchor and _hue_gap(got_h, anchor[0]) <= _HUE_WINDOW:
                    bad.append("配色=%.1f° 违反了 avoid「%s」（落在 %s 的色相范围内）"
                               % (got_h, word, val))
            continue
        if cat == "tone":
            got_t = _design_value(design, cat)
            for val, word, _src in rows:
                hit = (got_t == val) or (val == _TONE_BRIGHT and got_t == _TONE_LIGHT)
                if hit:
                    bad.append("气质=%r 违反了 avoid「%s」" % (got_t, word))
            continue
        got = _design_value(design, cat)
        for val, word, _src in rows:
            if got == val:
                bad.append("%s=%r 违反了 avoid「%s」" % (_CATEGORY_WORD[cat], got, word))
    return bad


def _wrap360(h):
    """归一化到 [0, 360)。"""
    return float(h) % 360.0


def _design_bg_hue(design):
    """取一个设计底色的色相；取不到返回 None。"""
    try:
        pal = _dig(design, ("visual", "palette"))
        bg = ST._color_of(pal.get("bg"), "visual.palette.bg")
        return float(ST.rgb_to_hsv(bg)[0])
    except Exception:                                     # noqa: BLE001
        return None



def _beats_for(segments):
    """叙事用途序列：首 hook、尾 outro，中间按固定顺序取（不足就少取）。"""
    mid = ["context", "data", "compare", "process", "quote", "turn"]
    n = max(1, int(segments))
    if n == 1:
        return ["hook"]
    if n == 2:
        return ["hook", "outro"]
    return ["hook"] + mid[:n - 2] + ["outro"]


# 只有用户**明确点名**要多人声时才采多声线。关键词是中文口语里真实会出现的说法，
# 不靠猜：没点名就固定 1 把声音（旁白片默认就是一个播音员，不是广播剧）。
# 只收**明确要求多个声音**的说法。裸词「对话」「多人」「双人」刻意不收：
# 「大模型的对话能力」「多人协作」这类**题材**本身不是要两个旁白，误判会让整片突然换人。
_MULTI_VOICE_WORDS = ("多人对话", "双人对谈", "两人对谈", "对谈", "访谈", "广播剧",
                     "分角色", "多声线", "多个声线", "两个声音", "两个声线",
                     "男女声", "男声女声", "一男一女", "双主播", "两个主播",
                     "分别由两个人", "一人一句")
# 显式要求"只用一个人念"的说法：命中就压回单声线，优先级高于多声线词。
_SINGLE_VOICE_WORDS = ("一个人念", "一个人讲", "单人播报", "只用一种声音", "单声线",
                       "一个声音", "不要多声线", "不要换人", "同一个人")


def _wants_multi_voice(brief):
    """brief 是否**明确要求**多声线（看题目原文与 must_have，不看"认不出的约束"）。

    为什么读 topic：`_brief_of` 只把**认得出的**片段放进 must_have（"多人对话"不在
    五类约束词表里），所以它会留在题目原文里。只读 must_have 会让"做个多人对话的
    访谈片"静默退化成单声线——用户说了却没人听，正是本项目最忌讳的一类失败。
    """
    if not isinstance(brief, dict):
        return False
    texts = [str(brief.get("topic") or "")]
    for word in (brief.get("must_have") or ()):
        texts.append(str(word))
    joined = " ".join(texts)
    # 顺序很重要：显式排除/明说单声线 > 多声线词。
    # 否则"一个人讲完，不要多声线"会因为句子里有「多声线」而被判成多声线（实测踩到）。
    for word in (brief.get("avoid") or ()):
        if any(w in str(word) for w in _MULTI_VOICE_WORDS):
            return False
    if any(w in joined for w in _SINGLE_VOICE_WORDS):
        return False
    return any(w in joined for w in _MULTI_VOICE_WORDS)


def _voice_gender_of(vid):
    """edge voice id -> "male"/"female"；认不出返回 None（不猜：猜错等于把"女声"悄悄换成男声）。

    口径取自 edge 中文声线的命名：Xiao*/Hsiao*/Hiu*（晓/曉）是女声，Yun*/Wan*（云/雲）是
    男声；香港粤语的 HiuMaan（女）/ WanLung（男）也在这条命名里。认不出的 id
    （wav 声线、别的引擎）一律"认不出"，由调用方决定怎么处理——不猜。
    """
    stem = str(vid or "").split("-")[-1]
    if stem.startswith(("Xiao", "Hsiao", "Hiu")):
        return "female"
    if stem.startswith(("Yun", "Wan")):
        return "male"
    return None


def _voice_gender(vid):
    """pitch 文案用的单字：女 / 男（认不出的按男声写，输出口径固定）。"""
    return "女" if _voice_gender_of(vid) == "female" else "男"


def _make_design(brief, index, seed, attempt, domain=None, unmatched=(), used_hues=()):
    """按 sha1(brief 规范 JSON + index + seed + attempt) 派生一个完整 design。

    选择用"哈希 + 下标步进"而不是"每个字段独立随机"：对 n<=8 的候选，
    配色族/母题/转场/编号/声线/情绪等维度按互质步长轮转，天然保证两两至少差 2 维；
    attempt 只在自检失败时参与，仍全是 sha1，没有 random。

    domain 是 brief 约束算出来的"每类允许的取值"（None = 这一类不受限）。步进仍在
    **允许集**上做，所以"两两至少差 2 维"这条保证在过滤之后依然成立，而不是被过滤打散。
    """
    cb = _brief_canon(brief)
    ctext = _canonical(cb)
    # nonce 是"这一次生成本身"的身份：默认每次调用新生成，传入 seed 时固定。
    # 它必须参与哈希，否则同一 brief 的第二次生成与第一次逐位相同。
    nonce = brief.get("_nonce") if isinstance(brief, dict) else None
    tag = "%s|%s" % ("" if seed is None else str(seed), nonce or "")

    def h(salt):
        return _digest(salt, ctext, tag, index, attempt)

    dom = domain or {}
    # 配色 = 连续色相空间里的一个点（不是若干命名外观之一）。
    # 色相按黄金角 137.5° 步进：相邻候选的色相必不接近（比固定步长更难撞色），
    # 且整体分布均匀 —— 这就是"每条片底色都不同"的来源。
    # 气质：点名了就在允许集里取；没点名时**第一个候选固定明亮活泼**，
    # 其余候选保持完整空间（含暗色）——"优先给明亮"不等于"全片只有一种口味"。
    tone_dom = dom.get("tone")
    if tone_dom:
        tone = tone_dom[(h("tone")) % len(tone_dom)]
    elif index == 0 and not dom.get("palette"):
        tone = _TONE_BRIGHT
    else:
        tone = None
    dark = h("dark") % 100 < 62                       # 约 62% 暗色片（贴合"工程技术片"气质）
    vivid = False
    if tone == _TONE_BRIGHT:
        dark, vivid = False, True                     # 活泼 = 浅底 + 带色
    elif tone == _TONE_LIGHT:
        dark = False
    elif tone == _TONE_DARK:
        dark = True
    sp = _PALETTE_SPACE["vivid" if vivid else dark]
    # 配色：连续色相空间取点。若 brief 给了配色约束，就落在它的色相窗口内
    # （窗口内仍是连续的，不是"锁定到某一个预设"）。
    pcon = dom.get("palette")
    if isinstance(pcon, dict):
        anchor = pcon.get("anchor")
        if anchor is not None:
            base_hue, dark = anchor
            vivid = False                     # 配色锚点自带明暗，气质不再覆盖它
            # 约束窗口内的取点：**放在网格上挑、且不离开窗口**。
            #
            # 为什么不是"哈希取点 + 撞 banned 就让开"：让开会把点推出锚点 ±window
            # （实测「深蓝」被采到 240°，超出 210±26），而独立哈希在 52° 窗口里凑不出
            # 三个色相拉得开的底色 —— 2160 次 n=3 采样里 3~8 次在第 3 个候选上死循环，
            # 自检 63（带约束的 brief 不许高概率失败）会偶发飘红。
            #
            # 现在：窗口内均匀铺 _HUE_GRID 个格点（52° -> 每格约 2.2°），先剔除撞
            # banned 的格，再按「与已接受候选的最小色相间距」分两档排序：能拉开
            # >= _PALETTE_KEEP_GAP 的优先，其余按间距从大到小；档内顺序由 (nonce,
            # attempt) 派生的哈希决定，所以重采会换格、但同一次采样必定散开。
            span = float(pcon.get("window") or _HUE_WINDOW)
            banned = tuple(pcon.get("banned_hues") or ())
            grid = [_wrap360(base_hue - span / 2.0 + span * (k + 0.5) / _HUE_GRID)
                    for k in range(_HUE_GRID)]
            grid = [g for g in grid if not any(_hue_gap(g, bh) < span for bh in banned)]
            if not grid:
                raise ValueError("配色约束把色相窗口全部排除掉了：%r" % (banned,))
            # 分档：与任何已接受候选都拉开 >= _PALETTE_KEEP_GAP 的格子优先（第 0 档），
            # 其次是间距最大的一档。档内顺序由 (nonce, attempt) 派生的哈希决定 ——
            # 重采会换格，所以 50 次重试不会被同一个格子卡死。
            def _hue_rank(g):
                gap = min([_hue_gap(g, u) for u in used_hues] or [360.0])
                band = 0 if gap >= _PALETTE_KEEP_GAP else 1
                return (band, -gap, _digest("hue", tag, index, attempt, g))
            ranked = sorted(grid, key=_hue_rank)
            hue = ranked[0]
        else:
            hue = (h("palette") + index * 137.508) % 360.0
            banned_hues = pcon.get("banned_hues") or ()
            for _k in range(24):
                if not any(_hue_gap(hue, bh) < _HUE_WINDOW for bh in banned_hues):
                    break
                hue = (hue + 37.0) % 360.0
    else:
        hue = (h("palette") + index * 137.508) % 360.0
    sp = _PALETTE_SPACE["vivid" if vivid else dark]
    sat = sp["s"][0] + (h("sat") % 100) / 100.0 * (sp["s"][1] - sp["s"][0])
    val = sp["v"][0] + (h("val") % 100) / 100.0 * (sp["v"][1] - sp["v"][0])
    family = (hue, sat, val, dark)
    motifs = tuple(dom.get("motif") or _DEFAULT_MOTIFS)
    motif = motifs[(h("motif") + 3 * index) % len(motifs)]
    transes = tuple(dom.get("transition") or TRANSITIONS)
    trans = transes[(h("transition") + 5 * index) % len(transes)]
    ease = EASES[(h("ease") + index) % len(EASES)]
    number = NUMBERS[(h("number") + 3 * index) % len(NUMBERS)]
    moods = tuple(dom.get("mood") or MOODS)
    mood = moods[(h("mood") + 3 * index) % len(moods)]
    pool = _voices_for(dom.get("voice"))
    voice_index = (h("voice") + index) % len(pool)
    scale = SCALES[(h("scale") + index) % len(SCALES)]
    layout = LAYOUTS[(h("layout") + index) % len(LAYOUTS)]
    # 默认不用 HUD：chip-left 是"顶部 6px 通栏进度条 + 1px 胶囊描边"，
    # corner-line 更是一组细线 —— 那正是用户说的"默认带着一些细线"。
    # HUD 仍然可以用：手改 design.json 的 visual.hud 即可（schema 与绘制都支持）。
    hud = "none"
    align = ALIGNS[(h("align") + index) % len(ALIGNS)]
    font_family = FONT_FAMILIES[(h("family") + index) % len(FONT_FAMILIES)]

    # ---- 片内版式轮换（原则 5）----
    # visual.layout 是单值 → grammar 会把它当"全片默认"，
    # 于是 8 段片子 8 屏都是同一个骨架（左标题右卡片 / 居中大字 / 右标题左卡片 里挑一个），
    # 观众看到的是"长时间重复"。所以默认派生 2–3 种版式的轮换序列，
    # 由 grammar 按**章节**轮换（同一章内稳定，换章才换版式）。
    # 用户仍可在 design.visual.layout_cycle 里显式指定（会覆盖这里）。
    _others = [x for x in LAYOUTS if x != layout]
    _rot = (h("layout_rot") + index * 3) % len(_others)
    _others = _others[_rot:] + _others[:_rot]
    cycle_len = 2 if h("layout_n") % 2 == 0 else 3
    layout_cycle = [layout] + _others[:max(0, cycle_len - 1)]

    # ---- 字幕样式（原则 2/5）----
    # 字幕字号/位置/背板/圆角若是渲染器里的模块常量，任何两条片子字幕都一模一样，
    # 是"跨片同质化"里最稳定的一条重复。所以进设计（可选字段，写进 design.json 可改）。
    # 取值范围都留在"好看"的区间内：字号 40–56、背板透明度 96–208、圆角 2–24。
    sub_size = 40 + (h("subsize") % 5) * 4                    # 40..56
    sub_align = "center" if h("subalign") % 4 else "left"     # 多数居中，偶尔左对齐
    sub_y = None if h("suby") % 3 else (560 + (h("suby2") % 9) * 40)  # 1/3 概率抬高
    subtitle = {
        "size": int(sub_size),
        "align": sub_align,
        "plate_alpha": int(96 + (h("subalpha") % 8) * 14),    # 96..194
        "radius": int(2 + (h("subr") % 4) * 6),               # 2..20
        "max_lines": 2 if h("sublines") % 4 else 1,
    }
    if sub_y is not None:
        subtitle["y"] = int(sub_y)
    _sub_fam = h("subfam") % 3
    if _sub_fam == 0:
        subtitle["family"] = font_family                     # 与大标题同族
    elif _sub_fam == 1:
        subtitle["family"] = "sans"                          # 字幕用黑体（可读性最好）

    hue_shift = ((h("hue") % 161) - 80) / 10.0          # -8.0 .. +8.0 度
    target_shift = ((h("sv") % 9) - 4) / 100.0          # -0.04 .. +0.04
    # 求解失败会横向让开色相重试；返回值带着**实际用到**的色相与调色板，
    # 标签必须用实际色相，否则卡片上印的 h 与画面颜色对不上。
    _pg = _solve_palette_retry(family, hue_shift, target_shift)
    palette, hue = _pg["palette"], _pg["family"][0]

    density = _DENSITIES[(h("density") + index) % len(_DENSITIES)]
    push = _PUSHES[(h("push") + index) % len(_PUSHES)]
    breathe = _BREATHES[(h("breathe") + index) % len(_BREATHES)]
    enter = list(_ENTERS[(h("enter") + index) % len(_ENTERS)])
    x = _XS[(h("bx") + index) % len(_XS)]
    y = _YS[(h("by") + index) % len(_YS)]
    inset = _INSETS[(h("inset") + index) % len(_INSETS)]

    ratio = round(1.9 + (h("ratio") % 18) * 0.1, 1)     # 1.9 .. 3.6
    tracking = round((h("tracking") % 17) * 0.5, 1)     # 0 .. 8
    line_gap = round(1.15 + (h("gap") % 9) * 0.05, 2)   # 1.15 .. 1.55

    segs = 4 + h("segments") % 5                        # 4 .. 8
    target_sec = None
    target_mode = None
    dt = brief.get("duration_target")
    if dt is not None:
        # 段数按「每段约 7 秒」估：下限模式下这是**最少**段数，不是精确目标。
        # 成片时长仍由台词量（TTS 实测）决定，这里只保证结构不会被压成 4 段。
        segs = max(2, min(40, int(round(float(dt) / 7.0))))
        target_sec = float(dt)
        target_mode = brief.get("duration_mode") or "exact"
    hold_policy = HOLD_POLICIES[h("hold") % len(HOLD_POLICIES)]

    # 竖屏的停留/最短段要略长：竖屏一屏信息更少，同样 0.45s 的句前留白在 9:16
    # 里显得紧。系数按安全区面积比开方得到（竖屏 950x1490 / 横屏 1792x916 = 1.16），
    # sqrt(1.16) ≈ 1.08 —— 是**可复算的推导值**，不是拍脑袋的经验数。
    # 想要更慢/更快就在 film.json 里显式覆盖（film.json 永远优先）。
    pace = _pace_factor()
    lead = round((0.45 + (h("lead") % 9) * 0.05) * pace, 2)
    tail = round((0.75 + (h("tail") % 9) * 0.05) * pace, 2)
    min_dur = round((2.6 + (h("min") % 11) * 0.1) * pace, 2)
    hold = round((h("hold_sec") % 5) * 0.1 * pace, 2)
    stagger = round(0.24 + (h("stagger") % 15) * 0.02, 2)

    bpm = _MOOD_BPM[mood] + (h("bpm") % 9) - 4
    keys = ST.MINOR_KEYS if scale == "minor" else ST.MAJOR_KEYS
    key = keys[h("key") % len(keys)]
    music_seed = h("mseed") % 100000

    # 默认**单声线**：整片一个旁白。多声线只在用户点名要（对话/多人/广播剧…）时才采，
    # 因为「默认每片都换两三把声音」会让观众以为是广播剧而不是旁白（用户实测反馈）。
    # 真要多声线，也可以手改 design.audio.voice.map（voicekit 按 kind 取，逐句 voice 优先）。
    vcount = (2 + h("vcount") % 2) if _wants_multi_voice(brief) else 1
    picks = [pool[(voice_index + j) % len(pool)] for j in range(vcount)]
    vmap = {kind: picks[j % vcount] for j, kind in enumerate(VOICE_KINDS)}
    jitter = round(0.10 + (h("jitter") % 5) * 0.05, 2)

    did = "design-" + hashlib.sha1(
        ("%s|%s|%s|%s" % (ctext, tag, index, attempt)).encode("utf-8")
    ).hexdigest()[:8]
    # 标签描述"这配色长什么样"，而不是"它属于哪一套"（没有套了）
    fam_label = ("深" if dark else ("明" if vivid else "浅")) + "色 h%03d" % int(round(hue) % 360)
    label = "%s·%s" % (fam_label, _MOTIF_WORD[motif])
    pitch = "%s×%s：%s，%s转场，%s声线%s，%d BPM %s。" % (
        fam_label, _MOTIF_WORD[motif], _MOOD_WORD[mood], _TRANS_WORD[trans],
        _voice_gender(picks[0]),
        ("（单声线）" if vcount == 1 else "（%d 种声音）" % vcount),
        bpm, "小调" if scale == "minor" else "大调")

    return {
        "schema": SCHEMA,
        "id": did,
        "label": label,
        "pitch": pitch,
        "structure": {
            "segments": int(segs),
            "beats": _beats_for(segs),
            "hold_policy": hold_policy,
            "target_sec": target_sec,
            # exact = 精确目标（双向对账）；at_least = 下限（只有不足才告警）
            "target_mode": target_mode,
        },
        "visual": {
            "palette": {k: list(palette[k]) for k in PALETTE_KEYS},
            "family": font_family,
            "typography": {"tracking": tracking, "line_gap": line_gap,
                           "number": number, "ratio": ratio, "align": align},
            "motif": {"kind": motif, "density": density, "detail": {}},
            "transition": {"kind": trans,
                           "dur": 0.0 if trans == "cut" else round(0.20 + (h("dur") % 5) * 0.05, 2)},
            "motion": {"ease": ease, "enter": enter,
                       "push": push, "breathe_hz": breathe},
            "bias": {"x": x, "y": y, "inset": inset},
            "layout": layout,
            "layout_cycle": list(layout_cycle),
            "hud": hud,
            "size": _canvas_size(),
            "fps": 30,
            "subtitle": subtitle,
        },
        "audio": {
            "voice": {"mode": "edge_id", "id": picks[0], "map": vmap,
                      "jitter": jitter, "max_distinct": vcount},
            "music": {"mode": "params", "scale": scale,
                      "bpm": int(bpm), "key": key, "mood": mood, "seed": int(music_seed)},
            "sfx": {"enabled": True,
                    "density": SFX_DENSITIES[1 + h("sfx") % 3],
                    "gain_db": -9 + h("sfxgain") % 7},
        },
        "pacing": {"lead": lead, "tail": tail, "min_dur": min_dur,
                   "hold": hold, "item_stagger": stagger},
        # 认不出来的要求：随候选一起走，顶层（propose/summary）才能提示"这条没生效"。
        "_unmatched_constraints": list(unmatched),
    }


def sample_designs(brief, n=3, seed=None):
    """现场采样 n 个两两可区分的 design（brief 的 must_have/avoid 在这里硬过滤）。

    **默认"每次都不一样"**：
      - 不传 seed -> 每次调用都生成一个**新的 nonce**，同一 brief 跑两次得到不同设计；
      - 传 seed   -> 固定 nonce，逐字节可复现（调试、回归、写文档示例时用）。
    默认必须是"每次都不一样"：若默认"同一 brief 永远同一结果"，实测两次独立进程
    产出的指纹完全相同（31f64edd34e502f9 / 52d385953835379e）——那等于把"没有模板"
    做成了"模板藏在哈希里"，用户要新片子只能改 brief 措辞。这不满足"每条视频都不一样"。

    其余行为不变：
    - 所有选择由 sha1(非对称 nonce + brief 规范 JSON + index + seed + attempt) 派生，
      全程禁 random / time / hash()（str hash 每进程带随机盐）；
    - must_have/avoid 先解析成"每类允许的取值"，下标步进在**允许集**上轮转：
      约束于是不靠重采样碰运气（声线池只有 3 个女声时，"碰 3 次全中"的概率低到不可用）；
    - 每个候选先过 validate_design，再按 brief 复核一遍约束（不信"域已经限住"）；
    - 与已接受的候选至少 2 个维度不同（design_distance 自检），否则换 attempt；
    - 最多 50 次仍不行 -> raise，并点名是哪条约束把可选域收窄了（绝不静默放宽）。
    """
    _check_brief(brief)
    if not _is_int(n) or n < 1:
        raise ValueError("n 必须是正整数，收到 %r" % (n,))

    must, avoid, domain, unmatched = _resolve_constraints(brief)
    hint = _narrow_hint(must, avoid)
    # 显式 seed = 可复现（同 seed 同结果）；不给 seed = 每次新 nonce（同 brief 也不同片）。
    # 随机源用 os.urandom 而不是 random：这里要的是"不可预测"，不是"伪随机序列"，
    # 而且 nonce 只参与 sha1 输入，不参与任何需要序列性质的运算。
    if seed is None:
        brief = dict(brief)
        brief["_nonce"] = os.urandom(8).hex()
    elif "_nonce" not in brief:
        brief = dict(brief)
        brief["_nonce"] = ""

    out = []
    used_hues = []
    for i in range(n):
        chosen = None
        problems = None
        too_close = None
        violated = None
        why_not = []
        for attempt in range(_MAX_RESAMPLE):
            why_not = []
            cand = _make_design(brief, i, seed, attempt, domain, unmatched, used_hues)
            problems = validate_design(cand)
            if problems:
                why_not.append("没过校验：%s" % problems[0])
                continue
            violated = _constraint_problems(cand, must, avoid)
            if violated:
                why_not.append("违反 brief 约束：%s" % violated[0])
                continue
            too_close = [o["id"] for o in out
                         if design_distance(cand, o)["n"] < 2]
            if too_close:
                why_not.append("与前序候选不足 2 维差异：%s" % " / ".join(too_close))
                continue
            # 同批候选的**版式**不许撞车（n<=3 时）。
            #
            # 为什么单独管这一条：design_distance 在母题/转场/缓动/编号/配乐上很容易凑够
            # 2 维，但剪影门禁（G13）量的是**构图骨架**——两个候选都用 left-stack、
            # 只有母题与配色不同时，三值化后的前景占比/重心/边带基本同形，实测被判
            # "换风格仍只是换色"。版式在 n<=3 时互斥是最省力的结构分叉（3 个候选正好
            # 对应 3 种版式）。
            #
            # 但这是**优先级**不是硬约束：带"暖纸色 + 女声"这类窄约束时可选配色窗口只有
            # 52°，3 个候选可能只放得下 1–2 种版式；硬拦会让 n=3 直接失败（实测）。
            # 所以最后 8 次重采放宽这一条——宁可牺牲一次骨架分叉，也不让采样失败。
            if len(out) < 3 and attempt < _MAX_RESAMPLE - 8:
                clash_layout = [o["id"] for o in out
                                if _dig(o, ("visual", "layout")) == cand["visual"]["layout"]]
                if clash_layout:
                    why_not.append("版式与前序候选 %s 相同（剪影会像同一张骨架）"
                                   % " / ".join(clash_layout))
                    continue
            # 底色必须和已接受候选**拉开色相**。为什么单独管这一条：
            # design_distance 会在"母题/转场/缓动/编号"上找到足够的差异，
            # 于是两个候选可能共用同一片底色却仍被判为"可区分"。
            # 实测样例：A=近黑金·电影遮幅 / B=近黑金·细线页眉 —— 并排图里第一眼
            # 就是同一个片子的两种排法，用户不会认为那是两个方案。
            # 底色是整屏第一眼信息，必须先分叉。
            #
            # 判重直接比**底色色相距离**，不按 24° 分桶：有 must_have 配色约束时，
            # 可选色相被压到 ±26° 窗口里，24° 分桶只剩 2–3 档，n=3 要求 3 个不同族 →
            # 重采样 50 次成功率约一半；"暖纸色+女声"这类常见组合实测 20/20 全失败
            # （brief 本身完全合理）。比色相距离则窗口内只要彼此 ≥ _PALETTE_KEEP_GAP 度
            # 就算两片不同底，约束不与判重打架，候选数量也不受"分桶数"限制。
            if out:
                hue = _design_bg_hue(cand)
                clash = None
                min_gap = 360.0
                if hue is not None:
                    for o in out:
                        oh = _design_bg_hue(o)
                        if oh is None:
                            continue
                        gap = _hue_gap(hue, oh)
                        min_gap = min(min_gap, gap)
                        if gap < _PALETTE_KEEP_GAP:
                            clash = (o["id"], gap)
                            break
                # 分档判重：brief 把配色约束到窗口里时（如"深蓝"= 210°±26°），
                # 52° 的窗口放不下"3 个两两差 12°"的底色 —— 硬要求会让 n=3 有近一半概率
                # 采样失败（实测 19/40）。所以分两档：
                #   · 色相差 < 6°：无论别的维度多不同，并排看都是同一片底色 -> 必须换；
                #   · 色相差 < 12°：只有在**结构差异够强**（≥3 个维度不同）时才接受，
                #     这正是"第一眼是不同方案"的判据（底色接近但版式/母题/转场/字体全变）。
                if clash is not None and (min_gap < 6.0 or design_distance(cand, _by_id(out, clash[0]))["n"] < 3):
                    why_not.append("底色色相与前序候选 %s 只差 %.1f°（色相差太小，并排看是同一片）"
                                   % (clash[0], clash[1]))
                    continue
            chosen = cand
            break
        if chosen is None:
            detail = ("\n  最后一次被拒的原因：%s" % why_not[0]) if why_not else ""
            if violated:
                raise ValueError(
                    "第 %d 个候选重采样 %d 次仍不满足 brief 约束：\n  - %s%s\n"
                    "  怎么办：① 放宽 brief（去掉一条 must_have/avoid）；"
                    "② 减少候选数 --n 2；③ 用 --seed 固定后重试。"
                    % (i + 1, _MAX_RESAMPLE, "\n  - ".join(violated), hint))
            if problems:
                raise ValueError("第 %d 个候选重采样 %d 次仍没过校验：\n  - %s%s%s"
                                 % (i + 1, _MAX_RESAMPLE, "\n  - ".join(problems), hint, detail))
            raise ValueError(
                "第 %d 个候选重采样 %d 次仍与前序候选太像（最像 %s）%s%s\n"
                "  怎么办：① 减少候选数 --n 2；② 放宽 brief 里收窄可选域的约束；"
                "③ 用 --seed 固定后重试。"
                % (i + 1, _MAX_RESAMPLE, " / ".join(too_close or ["?"]), hint, detail))
        out.append(chosen)
        _h = _design_bg_hue(chosen)
        if _h is not None:
            used_hues.append(_h)
    return out


# ============================================================================
# 九、CLI
# ============================================================================


def _setup_stdio():
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                             # noqa: BLE001
            pass


def _palette_rows(n=8):
    """演示用：从连续色相空间均匀取 n 个点，各自的解出结果。

    配色是连续色相空间，没有"族"可列，但**还是得让人看见配色能长什么样**——
    所以均匀采样给你看，而不是给一份可选清单。这不是模板：打印出来的每个点
    都可以再调（h/s/v 都是连续量）。
    """
    rows = []
    for i in range(int(n)):
        hue = (i * 360.0 / max(1, int(n))) % 360.0
        dark = i % 3 != 2                      # 每三个里有一个浅色，便于对照
        sp = _PALETTE_SPACE[dark]
        sat = (sp["s"][0] + sp["s"][1]) / 2.0
        val = (sp["v"][0] + sp["v"][1]) / 2.0
        row = {"hue": round(hue, 1), "sat": round(sat, 3), "val": round(val, 3),
               "dark": dark, "label": ("深" if dark else "浅") + "色 h%03d" % int(hue)}
        try:
            pal = _solve_palette((hue, sat, val, dark))
            row["ink_hex"] = _hex(pal["ink"])
            row["accent_hex"] = _hex(pal["accent"])
            row["bg_hex"] = _hex(pal["bg"])
            row["surface_hex"] = _hex(pal["surface"])
        except Exception as e:                        # noqa: BLE001
            row["ink_hex"] = row["accent_hex"] = row["bg_hex"] = row["surface_hex"] = ""
            row["error"] = str(e)
        rows.append(row)
    return rows


def main(argv=None):
    _setup_stdio()
    ap = argparse.ArgumentParser(description="video-studio 设计 DSL：校验 / 采样 / 列配色族")
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--sample", metavar="BRIEF", default=None,
                     help="按题材采样 N 个设计")
    grp.add_argument("--lint", metavar="PATH", default=None,
                     help="校验一份 design.json")
    grp.add_argument("--list-palettes", action="store_true", help="列出配色族")
    ap.add_argument("--n", type=int, default=3, help="候选数（默认 3）")
    ap.add_argument("--seed", default=None, help="采样种子（同一 seed 结果可复现）")
    ap.add_argument("--json", action="store_true", help="只输出 JSON（stdout）")
    a = ap.parse_args(argv)

    if a.list_palettes:
        # 按 _palette_rows() 真实返回的键（hue/dark/ink_hex/...）打印，不要按老键名
        # （family/bg/accent_hue）取：键名对不上会 KeyError，而 --json 分支不读这些键、
        # 照样正常，自检容易漏掉。同时说明"这不是可选清单"。
        rows = _palette_rows()
        if a.json:
            print(json.dumps(rows, ensure_ascii=False, indent=1))
        else:
            print("配色采样示例（%d 个点，不是可选清单）：" % len(rows))
            print("  底色 = (色相 h°, 饱和 s, 明度 v) 连续空间里的一点；h 可在 0–360 任意取，"
                  "下面只是均匀散开的样点。")
            for r in rows:
                extra = ("  ERROR=%s" % r["error"]) if r.get("error") else ""
                print("  %-12s h=%6.1f s=%.2f v=%.2f  bg=%s surface=%s ink=%s accent=%s%s"
                      % (r["label"], r["hue"], r["sat"], r["val"],
                         r["bg_hex"] or "-", r["surface_hex"] or "-",
                         r["ink_hex"] or "-", r["accent_hex"] or "-", extra))
        return 0

    if a.lint:
        try:
            with open(a.lint, encoding="utf-8") as fh:
                design = json.load(fh)
        except (OSError, json.JSONDecodeError) as e:
            print("[designkit] 读不出 %s：%s" % (a.lint, e), file=sys.stderr)
            return 2
        bad = validate_design(design)
        if a.json:
            print(json.dumps({"path": a.lint, "ok": not bad, "problems": bad},
                             ensure_ascii=False, indent=1))
        elif bad:
            print("设计没过校验（%d 条）：" % len(bad))
            for s in bad:
                print("  -", s)
        else:
            print("设计 OK：%s" % design.get("id"))
        return 0 if not bad else 1

    if a.sample is not None:
        try:
            designs = sample_designs({"topic": a.sample}, n=a.n, seed=a.seed)
        except (TypeError, ValueError, RuntimeError) as e:
            print("[designkit] %s" % e, file=sys.stderr)
            return 2
        if a.json:
            print(json.dumps(designs, ensure_ascii=False, indent=1))
            return 0
        print("方案（brief=%r，n=%d）：" % (a.sample, len(designs)))
        for i, d in enumerate(designs):
            print("  [%s] %-14s %s" % ("ABCDEFGH"[i] if i < 8 else i + 1,
                                       d["label"], d["pitch"]))
        print("两两差异（n = 不同的设计维度数）：")
        for i in range(len(designs)):
            for j in range(i + 1, len(designs)):
                dist = design_distance(designs[i], designs[j])
                print("  %s-%s  n=%d  %s"
                      % ("ABCDEFGH"[i], "ABCDEFGH"[j], dist["n"],
                         " / ".join(dist["axes"])))
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
