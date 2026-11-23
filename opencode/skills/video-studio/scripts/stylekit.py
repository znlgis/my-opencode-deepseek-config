# -*- coding: utf-8 -*-
"""stylekit.py — 视觉/音频原语库：颜色数学、版式几何、主题体检、视觉语言体检。

本模块是**纯数据 + 纯函数**的技术底座，不提供任何外观成品。
外观从哪来只有一条路：`work/design.json`——由 `propose.py` 按 brief 现场采样、
用户 `--confirm` 之后冻结，再由 `designkit.design_to_plan()` 转成同形的计划字典。

设计原则
--------
1. 只依赖标准库，不 import filmkit / scorekit / scenes（它们反过来 import 本模块，
   成环就炸）。因此可以单独测、结果可复现。
2. 阈值只有一份：对比度门槛（`LINT_LIMITS`）、视觉语言边界都在这里，
   `designkit` 直接复用，不抄第二份。
3. 确定性：只用 sha1 派生，禁 random / time / hash()（str hash 每次进程带随机盐）。
4. 模板不存在：`STYLES` / `THEMES` / `VISUAL_LANGUAGES` 恒为空表，
   写任何 style id 都会被明确拒绝并给出迁移指引（见 `resolve_script_style`）。

用法
----
    import stylekit as ST
    ST.theme_lint(theme)            # 对比度/明暗/结构体检，返回问题列表（空 = 过）
    ST.visual_lint_language(lang)   # 视觉语言包的数值边界体检
    ST.LAYOUTS["left-stack"]        # 版式几何（相对 W/H 与 SAFE，不写死魔法数）
    ST.is_dark(theme)               # 明暗族判定（同片不许跨族）

    python stylekit.py            # 打印本模块的职责说明
    python stylekit.py --list     # 已移除：没有可选外观清单，exit 2
"""
from __future__ import annotations

import argparse
import re
import sys

# ============================================================================
# 一、色彩：WCAG 2.1 对比度（门禁要用，所以放在本模块，纯标准库）
# ============================================================================


def hsv_to_rgb(h, s, v):
    """HSV(0..360, 0..1, 0..1) -> (r,g,b) 整数 0..255。

    技术原语：让"配色"可以是一个**连续空间**而不是一份命名清单。
    有了它，第 9 种外观 = 把色相转到 200°，不需要有人先写一个模板文件。
    """
    h = float(h) % 360.0
    s = max(0.0, min(1.0, float(s)))
    v = max(0.0, min(1.0, float(v)))
    c = v * s
    x = c * (1.0 - abs((h / 60.0) % 2.0 - 1.0))
    m = v - c
    if h < 60:
        rgb = (c, x, 0.0)
    elif h < 120:
        rgb = (x, c, 0.0)
    elif h < 180:
        rgb = (0.0, c, x)
    elif h < 240:
        rgb = (0.0, x, c)
    elif h < 300:
        rgb = (x, 0.0, c)
    else:
        rgb = (c, 0.0, x)
    return tuple(int(round(max(0.0, min(1.0, ch + m)) * 255.0)) for ch in rgb)


def rgb_to_hsv(rgb):
    """(r,g,b) 0..255 -> (h 0..360, s 0..1, v 0..1)。与 hsv_to_rgb 互逆。"""
    r, g, b = (max(0.0, min(1.0, float(c) / 255.0)) for c in tuple(rgb)[:3])
    mx, mn = max(r, g, b), min(r, g, b)
    d = mx - mn
    if d < 1e-12:
        return 0.0, 0.0, mx
    if mx == r:
        h = (60.0 * ((g - b) / d)) % 360.0
    elif mx == g:
        h = 60.0 * ((b - r) / d) + 120.0
    else:
        h = 60.0 * ((r - g) / d) + 240.0
    s = 0.0 if mx < 1e-12 else d / mx
    return h, s, mx


def _srgb_to_lin(v):
    """sRGB 通道（0..255）-> 线性光。"""
    c = float(v) / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def luminance(rgb):
    """相对亮度（WCAG 定义，0=黑 1=白）。"""
    r, g, b = rgb[0], rgb[1], rgb[2]
    return 0.2126 * _srgb_to_lin(r) + 0.7152 * _srgb_to_lin(g) + 0.0722 * _srgb_to_lin(b)


def contrast(c1, c2):
    """两色的对比度（1.0 ~ 21.0）。文字门禁用它，比「看截图觉得还行」可靠。"""
    l1, l2 = luminance(c1), luminance(c2)
    hi, lo = (l1, l2) if l1 >= l2 else (l2, l1)
    return (hi + 0.05) / (lo + 0.05)


def midpoint(c1, c2):
    """两色中点（渐变背景的中位亮度，作为文字对比度的参考底）。"""
    return (int(round((c1[0] + c2[0]) / 2.0)),
            int(round((c1[1] + c2[1]) / 2.0)),
            int(round((c1[2] + c2[2]) / 2.0)))


def _rgb(seq):
    t = tuple(int(v) for v in seq[:3])
    if len(t) != 3 or any(v < 0 or v > 255 for v in t):
        raise ValueError("颜色必须是 0..255 的 RGB 三元组，收到 %r" % (seq,))
    return t


_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6}|[0-9a-fA-F]{3})$")


def _color_of(v, key):
    """把 theme_overrides 里的颜色值统一成 (r,g,b)：支持 [r,g,b] 和 "#rrggbb" / "#rgb"。

    为什么必须支持 hex：schemas/film.schema.json 与 references/appearance.md 的示例
    就是 {"accent": "#ff8a3d"}，而 JSON 没有元组，照着文档写 hex 是必然的。
    字符串必须在**这里**转成元组：原样塞进主题的话，scenes.py 会在渲染期崩
    （实测：ink="#ffffff" -> ValueError: invalid literal for int() with base 10: '#'）。
    """
    if isinstance(v, str):
        m = _HEX_RE.match(v.strip())
        if not m:
            raise ValueError("theme_overrides.%s 的颜色写法不认识：%r（用 [r,g,b] 或 \"#rrggbb\"）"
                             % (key, v))
        h = m.group(1)
        if len(h) == 3:
            h = "".join(ch * 2 for ch in h)
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    if isinstance(v, (list, tuple)) and len(v) == 3:
        return _rgb(v)
    raise ValueError("theme_overrides.%s 必须是颜色（[r,g,b] 或 \"#rrggbb\"），收到 %r"
                     % (key, v))


# 主题里"颜色字段"的白名单（值是 0..255 的 RGB 元组）。不靠"长度 3 的元组"猜：
# keywords 也是元组（长度 10），将来若加一个正好 3 项的关键词元组就会被误当颜色。
_THEME_COLOR_KEYS = frozenset(("bg", "bg2", "surface", "ink", "muted", "dim",
                               "accent", "accent2", "good", "bad", "rule", "vignette_tint"))


def apply_theme_overrides(theme, overrides):
    """把 film.json 的 visual.theme_overrides 合并进主题**并做类型校验**。

    契约（schemas/film.schema.json：只接受主题里已存在的键；references/appearance.md §3）：
      - 未知键忽略：以后主题加字段时老剧本不因为它不认识就炸；
      - 颜色键（值本来是三色元组的那些）接受 [r,g,b] / "#rrggbb" / "#rgb"，统一成元组；
      - 数值键接受数字或数字字符串；字符串键必须是字符串；
      - 嵌套结构（card / scale / qa …）不支持覆盖，静默忽略（避免破坏已有配置）；
        真要改嵌套字段请在 designkit 里改生成逻辑。
    校验只在"值类型不对、会在渲染期崩"的场合抛 ValueError（提前到 --stage style）。
    返回新的 theme dict（不改入参）。
    """
    out = dict(theme or {})
    for k, v in (overrides or {}).items():
        if k not in out:
            continue
        base = out[k]
        if k in _THEME_COLOR_KEYS:
            out[k] = _color_of(v, k)
            continue
        if isinstance(base, bool):
            if not isinstance(v, bool):
                raise ValueError("theme_overrides.%s 必须是布尔，收到 %r" % (k, v))
            out[k] = bool(v)
            continue
        if isinstance(base, (int, float)):
            if isinstance(v, bool) or not isinstance(v, (int, float, str)):
                raise ValueError("theme_overrides.%s 必须是数字，收到 %r" % (k, v))
            try:
                num = float(v)
            except (TypeError, ValueError):
                raise ValueError("theme_overrides.%s 必须是数字，收到 %r" % (k, v)) from None
            out[k] = int(round(num)) if isinstance(base, int) else float(num)
            continue
        if isinstance(base, dict):
            # 嵌套结构（card / scale / qa）不支持覆盖：静默忽略而不是报错。
            # 配置里写了这种覆盖时应视为"没生效"而不是"配置坏了"，升级不该突然报错。
            # 想改嵌套字段要在 designkit 的 _theme_of_design 里改，见 references/appearance.md §7。
            continue
        if isinstance(base, str):
            if not isinstance(v, str):
                raise ValueError("theme_overrides.%s 必须是字符串，收到 %r" % (k, v))
            if k == "layout" and v not in LAYOUTS:
                raise ValueError("theme_overrides.layout 未知版式 %r；可用：%s"
                                 % (v, " / ".join(LAYOUT_IDS)))
            if k == "hud" and v not in ("chip-left", "corner-line", "none"):
                raise ValueError("theme_overrides.hud 未知取值 %r；可用：chip-left / corner-line / none"
                                 % (v,))
            out[k] = v
            continue
        # 其它类型（None 等）：保持原值，不折腾
    return out


def _theme(style_id, label, keywords, palette, material, typ, layout, motion, hud, qa):
    """把主题字段组装成一个扁平 dict（场景代码直接下标取，不嵌套，读起来快）。"""
    p = {k: _rgb(v) for k, v in palette.items()}
    return {
        "id": style_id,
        "label": label,
        "keywords": tuple(keywords),
        # T0 配色
        "bg": p["bg"], "bg2": p["bg2"], "surface": p["surface"],
        "ink": p["ink"], "muted": p["muted"], "dim": p["dim"],
        "accent": p["accent"], "accent2": p["accent2"],
        "good": p["good"], "bad": p["bad"], "rule": p["rule"],
        # T1 材质
        "vignette": float(material["vignette"]),
        "vignette_tint": _rgb(material["vignette_tint"]),
        "grain": float(material["grain"]),
        "grain_freq": float(material["grain_freq"]),
        "texture": str(material["texture"]),
        "texture_alpha": float(material["texture_alpha"]),
        # T2 排版
        "family": str(typ["family"]),
        "scale": dict(typ["scale"]),
        "line_gap": float(typ.get("line_gap", 1.30)),
        "tracking": float(typ.get("tracking", 0.0)),
        # T3 版式
        "layout": str(layout["layout"]),
        "card": dict(layout["card"]),
        "safe_bias": int(layout.get("safe_bias", 0)),
        # T4 动态
        "ease": str(motion["ease"]),
        "entrance_dx": int(motion["entrance_dx"]),
        "entrance_dy": int(motion["entrance_dy"]),
        "push": float(motion["push"]),
        "item_stagger": float(motion["item_stagger"]),
        "transition": str(motion["transition"]),
        # T5 HUD
        "hud": str(hud["style"]),
        # META
        "qa": dict(qa),
    }


# ============================================================================
# 二、主题：没有内置表，由 designkit 现场构造
# ============================================================================
#
# `THEMES` 恒为空：本技能不提供任何内置外观，写 style id 一律被拒绝（见 resolve_script_style）。
#
# 主题由 designkit 从连续空间现场构造：
#   · 底色 = (色相 h∈[0,360), 饱和 s, 明度 v) 连续取值，经 _palette_from_hue 算出三段底色；
#   · 其余 8 个语义色由对比度网格求解（每套都必须过 theme_lint）；
#   · 产出是同形的 35 字段主题字典，scenes.py 不需要知道它从哪来。
#
# 本模块提供的是**造东西的工具**，不是外观目录：
#   luminance / contrast / _rgb / _color_of / apply_theme_overrides /
#   _theme(构造器) / hsv_to_rgb / rgb_to_hsv / LINT_LIMITS / theme_lint /
#   bg_contrast / is_dark / family_compatible / LAYOUTS(几何参数，不是外观目录)
THEMES = {}
# 三、版式骨架（LAYOUTS）：3 种，scenes.py 按名字取几何参数
# ============================================================================
#
# 这是「每屏都长得一样」的解药：同一部片可以按章节在骨架之间轮换，但不换明暗族、不换 accent，
# 所以整体仍是一部片。几何全部相对于 W/H 与 SAFE，不写死 1920 里的魔法数。

LAYOUTS = {
    "left-stack": {
        "label": "左标题 / 右卡片",
        "title_x": 150, "title_y": 300, "title_w": 820,
        "kicker_y": 250, "sub_y": 420, "sub_w": 820,
        "panel_x": 1030, "panel_y": 240, "panel_w": 740, "panel_h": 96, "panel_gap": 118,
        "center": False,
    },
    "center-stack": {
        "label": "居中大字",
        "title_x": None, "title_y": 330, "title_w": 1560,
        "kicker_y": 268, "sub_y": 0, "sub_w": 1400,
        "panel_x": None, "panel_y": 650, "panel_w": 1320, "panel_h": 92, "panel_gap": 108,
        "center": True,
    },
    "split-right": {
        "label": "右标题 / 左卡片",
        "title_x": None, "title_y": 300, "title_w": 820,
        "kicker_y": 250, "sub_y": 420, "sub_w": 820,
        "panel_x": 150, "panel_y": 240, "panel_w": 740, "panel_h": 96, "panel_gap": 118,
        "center": False,
    },
}

LAYOUT_IDS = tuple(LAYOUTS.keys())

# ============================================================================
# 竖屏原生版式（同名的另一套几何，坐标直接是 1080x1920 的像素）
#
# 为什么不是把横屏的数乘个系数：竖屏的约束根本不是缩放——卡片该在标题下面纵向堆叠，
# 而不是在标题右侧；center-stack 的上下留白比例也完全不同。所以这里给的是另一套坐标，
# scenes._scene_layout 在竖屏下直接取用、不再折算（再折算一次就成了二次缩放）。
#
# 三套骨架在竖屏里靠**纵向节奏与密度**区分，不靠左右：竖屏没有真正的左右两栏。
# 注意 split-right 不能做成「卡片在上、标题在下」——正文段先出条目后出标题读起来是错的
# （第一版就是这么写的，出图后当场看出来）。
#
# 数值来源：SAFE=(65,120,1015,1610)、SUB_BAND=(80,1450,1000,1616)。
# 纵向分布：首版按横屏比例折过来，实测上半屏堆满、下半屏空 500px（1080x1920 的有效
# 可视高度是 1490，横屏只有 916，同一套 y 坐标在竖屏必然挤在上半）。所以竖屏
# 三套骨架的 y 一律下移并拉开：title_y 300->430 / panel_y 600->780（left-stack）等。
# 正文底边留到 1430，给字幕带留出 20px 间隙。
# ============================================================================
LAYOUTS_PORTRAIT = {
    "left-stack": {
        "label": "竖屏 标题在上 / 卡片在下",
        "title_x": 80, "title_y": 430, "title_w": 920,
        "kicker_y": 340, "sub_y": 600, "sub_w": 920,
        "panel_x": 80, "panel_y": 780, "panel_w": 920, "panel_h": 118, "panel_gap": 148,
        "center": False,
    },
    "center-stack": {
        "label": "竖屏 居中大字",
        "title_x": None, "title_y": 700, "title_w": 920,
        "kicker_y": 600, "sub_y": 0, "sub_w": 920,
        "panel_x": None, "panel_y": 1060, "panel_w": 880, "panel_h": 112, "panel_gap": 140,
        "center": True,
    },
    "split-right": {
        "label": "竖屏 紧凑标题 / 高密度卡片",
        "title_x": 80, "title_y": 380, "title_w": 920,
        "kicker_y": 290, "sub_y": 540, "sub_w": 920,
        "panel_x": 80, "panel_y": 720, "panel_w": 920, "panel_h": 104, "panel_gap": 132,
        "center": False,
    },
}


def layout_portrait(name):
    """取竖屏版式几何；名字与横屏一致（章节轮换用的还是那三个 key）。"""
    return LAYOUTS_PORTRAIT.get(str(name)) or LAYOUTS_PORTRAIT["left-stack"]


def layout(name):
    """取版式几何；未知名字报错并列出可用值（不静默回落）。"""
    if name not in LAYOUTS:
        raise KeyError("未知版式 %r；可用：%s" % (name, " / ".join(LAYOUT_IDS)))
    return LAYOUTS[name]


# ============================================================================
# 四、风格表：恒为空（本技能不提供模板）
# ============================================================================
#
# 一个设计从哪来只有一条路：work/design.json —— `propose.py` 按 brief 现场采样、
# 人 `--confirm` 之后冻结。表为空是**契约**，不是没填：selfcheck 断言它们必须为空，
# 有内容就意味着有人偷偷恢复了外观清单。
#
# `LEGACY_STYLE` 常量只服务于一件事：让 `style="legacy"` 这类写法被**明确拒绝**并
# 给出迁移指引，而不是抛 AttributeError。
STYLE_IDS = ()
DEFAULT_STYLE = None
LEGACY_STYLE = "legacy"
STYLES = {}

# ---- 调式池：技术原语，不是模板 ----
# 项目只支持中文口播常见的 4 个小调主音与 6 个大调主音。列出来是**约束**
# （musicgen 与 designkit 都按它校验"调性是否可实现"），不是"给你 10 套现成配色"。
MINOR_KEYS = ("Dm", "Am", "Em", "Cm")
MAJOR_KEYS = ("C", "G", "D", "F", "A", "Bb")


# "plain" 是默认母题：装饰层整层透明、背景无纹理 —— 默认设计不带任何细线。
# 其余 8 种是**显式选择**（brief 点名 / --revise / 手改 design.json），不是默认。
VISUAL_MOTIFS = ("plain", "brackets", "hairlines", "handdrawn", "slabs",
                 "minimal", "letterbox", "signal", "rays")
VISUAL_TRANSITIONS = ("dip", "wash", "paper", "wipe", "cut", "iris")
NUMBER_STYLES = ("dim", "serif", "cn", "block", "roman", "timecode", "dot", "gilt")

# ============================================================================
# 五、视觉语言包：名字是绘制原语，不是外观目录
# ============================================================================
#
# `VISUAL_LANGUAGES` 恒为空。视觉语言由 `designkit._language_of(design)` 从设计字段
# 直接拼出来（母题/转场/字距/缓动/重心/layout_cycle 五组），再交给
# `visual_lint_language` 做体检。
#
# 这里保留的是 VISUAL_MOTIFS / VISUAL_TRANSITIONS / NUMBER_STYLES —— **绘制原语的名字**：
# scenes.py 按名字分发到具体画法。这是"能画什么"，不是"长什么样"。
VISUAL_LANGUAGES = {}


def visual_language(lang=None):
    """返回一份可用的视觉语言包：传入字典就原样返回，否则给空壳。

    真正的语言包由 `designkit._language_of()` 生成并挂在 plan["language"] 上；
    这个函数只负责让"没有语言包"的调用点拿到一个合法空壳而不是 None。"""
    return lang if isinstance(lang, dict) else {}


def visual_lint_language(vl):
    """视觉语言体检：对**任意** language 字典，不要求它属于某个命名风格。

    没有这条检查，转场 1 秒、push 0.3 这类会让整片观感崩掉的错误要等渲完 300 帧
    才发现——所以它在 design 校验阶段就要跑。
    """
    bad = []
    if not isinstance(vl, dict) or not vl:
        return ["视觉语言包缺失或不是字典"]
    mo = vl.get("motion") or {}
    mf = vl.get("motif") or {}          # 注意：mo = motion，mf = motif，别混用
    tr = vl.get("transition") or {}
    ty = vl.get("typography") or {}
    if str(mf.get("kind")) not in VISUAL_MOTIFS:
        bad.append("motif.kind=%r 不在 %s 里" % (mf.get("kind"), " / ".join(VISUAL_MOTIFS)))
    if str(tr.get("kind")) not in VISUAL_TRANSITIONS:
        bad.append("transition.kind=%r 不在 %s 里"
                   % (tr.get("kind"), " / ".join(VISUAL_TRANSITIONS)))
    try:
        _d = float(tr.get("dur") or 0.0)
    except (TypeError, ValueError):
        _d = -1.0
    if not 0.0 <= _d <= 0.60:
        # 段界 veil 超过 0.6s 会吃掉 3–6s 节奏下限，也会把两段内容糊在一起
        bad.append("transition.dur=%r 必须在 0~0.60s（段界 veil 上限）" % tr.get("dur"))
    if str(ty.get("number")) not in NUMBER_STYLES:
        bad.append("typography.number=%r 不在 %s 里"
                   % (ty.get("number"), " / ".join(NUMBER_STYLES)))
    for key, lo, hi in (("ratio", 1.0, 3.6), ("tracking", 0.0, 8.0),
                        ("line_gap", 1.15, 1.55), ("push", 0.0, 0.16)):
        try:
            v = float((ty if key != "push" else mo).get(key) or 0.0)
        except (TypeError, ValueError):
            bad.append("typography/motion.%s=%r 不是数字" % (key, (ty if key != "push" else mo).get(key)))
            continue
        if key == "ratio" and not lo <= v <= hi:
            bad.append("typography.ratio=%r 必须在 1.0~3.6（display:body 字号比）" % v)
        elif key == "tracking" and not lo <= v <= hi:
            bad.append("typography.tracking=%r 必须在 0~8px" % v)
        elif key == "line_gap" and not lo <= v <= hi:
            bad.append("typography.line_gap=%r 必须在 1.15~1.55" % v)
        elif key == "push" and not lo <= v <= hi:
            # push > 0.16 时背景层放大后内容会被推出 SAFE（_bg_plate 只裁不缩）
            bad.append("motion.push=%r 必须在 0~0.16（超过会把内容推出安全区）" % v)
    try:
        dens = float(mf.get("density") or 0.0)
    except (TypeError, ValueError):
        dens = -1.0
    if not 0.0 <= dens <= 1.0:
        bad.append("motif.density=%r 必须在 0~1" % mf.get("density"))
    elif dens <= 0.0 and str(mf.get("kind")) not in ("plain", "minimal"):
        # plain 的零密度是**合法默认**（素净母题本来就不画装饰）；其余母题零密度
        # 等于「声明了装饰语言但一点不画」，那是配置错误，不是风格。
        bad.append("motif.density=0 只允许用于 plain / minimal（否则等于没有装饰语言）")
    enter = mo.get("enter") or (0.45, 0.85)
    try:
        ok_enter = (isinstance(enter, (list, tuple)) and len(enter) == 2
                    and 0.0 < float(enter[0]) <= float(enter[1]) <= 1.6)
    except (TypeError, ValueError):
        ok_enter = False
    if not ok_enter:
        bad.append("motion.enter=%r 必须是 (起, 止) 且 0 < 起 <= 止 <= 1.6 秒" % (enter,))
    return bad


def visual_lint(lang=None):
    """对**任意** language 字典做体检；传字符串（风格 id）时明确报错而不是静默通过。"""
    if isinstance(lang, str):
        return ["没有命名视觉语言 %r（视觉语言由 design 现场生成）" % lang]
    return visual_lint_language(lang or {})


# ============================================================================
# 六、解析：任何"按 style id 取外观"的调用都必须明确失败
# ============================================================================


def resolve_script_style(script):
    """没有设计时的**唯一失败出口**：明确报错并给两步迁移指引。

    本函数不再解析任何东西——本技能没有内置风格表，"按 style id 取外观"这件事不存在。
    但它必须**存在且报错**：`filmkit.style_plan` / `scenes.py` / `preflight` / `qavmaf`
    里都有「拿不到 plan 时回退到这里」的调用点。保留一个能给指引的入口，
    那些点会得到"先 propose 再 confirm"；删掉函数只会得到 AttributeError。

    真正的计划只有两个来源：`designkit.design_to_plan(work/design.json)`，
    或 `render.resolve_style()` 解析好挂在 `script["_style"]` 上的那一份。
    """
    raw = script.get("style") if isinstance(script, dict) else None
    extra = ("（收到 style=%r）" % (raw,)) if raw not in (None, "") else ""
    raise ValueError(
        "这个剧本没有可解析的设计%s：本技能不提供任何内置风格/模板，\n"
        "  「写 style 走内置外观」与「不写 style 走老外观」两条路都已删除。\n"
        "  正确做法（两步）：\n"
        "    1) python propose.py --brief \"题材/要求\" --script <film.json> --n 3\n"
        "    2) python propose.py --confirm --candidate A    # 冻结成 work/design.json\n"
        "  然后 python render.py --script <film.json> --stage all\n"
        "  （设计也可以手写 work/design.json，字段契约见 schemas/design.schema.json）"
        % extra)

# ============================================================================
# 七、门禁：主题体检（对比度 / 明暗 / 结构）
# ============================================================================
#
# 阈值来自 WCAG 2.1（inks 取 AAA 正文线，其余取 AA），不是拍脑袋定的。
# 量法：**按 bg/bg2 两端的最差值**，不看中点——中点会把"一端合格、一端不合格"平均过去。

LINT_LIMITS = {
    "ink": 7.0,          # 正文级（WCAG AAA 正文线）
    "muted": 4.5,        # 次级正文（WCAG AA）
    "dim": 3.0,          # 装饰级（HUD 小字、页脚）
    "accent": 4.5,       # 强调色常作文字
    "accent2": 4.5,      # 第二色相也常作文字
    "good": 3.0,         # 只承载图形（圆点靠位置+颜色双编码）
    "bad": 3.0,
    "lum_gap": 0.002,    # bg 与 bg2 的亮度差（防纯色底）
}


def bg_contrast(th, role):
    """文字色对背景的**最差**对比度：bg/bg2 是上下两端的主渐变（scenes._bg 按行插值），
    任何一行文字都可能落在两端附近，所以取二者较小值，而不是看中点。

    为什么不能只看中点：某次实测的 muted(86,100,96) 对 bg 是 5.28、对 bg2 只有 4.42，
    中点平均 4.84 把 AA 4.5 的门槛"平均"过去了。端点判据不会漏掉这种。
    """
    return min(contrast(th[role], th["bg"]), contrast(th[role], th["bg2"]))


def theme_lint(th):
    """主题体检，返回问题列表（空 = 过）。坏主题在 load 阶段就拦，别等渲完 300 帧。"""
    if not isinstance(th, dict):
        return ["主题不是 dict：%r" % (th,)]
    bad = []
    # 结构先自检：缺颜色键时下面按 th[role] 下标会抛 KeyError，而本函数的契约是
    # "返回问题列表（空 = 过）" —— 坏主题必须变成一条问题，不能变成崩溃。
    for key in ("bg", "bg2", "surface", "ink", "muted", "dim",
                "accent", "accent2", "good", "bad", "rule"):
        val = th.get(key)
        if not isinstance(val, (list, tuple)) or len(val) != 3:
            bad.append("主题缺颜色 %r（或不是 RGB 三元组）：%r" % (key, val))
        elif any(isinstance(c, bool) or not isinstance(c, (int, float))
                 or not 0 <= float(c) <= 255 for c in val):
            bad.append("主题颜色 %s=%r 不是 0..255 的 RGB" % (key, val))
    if bad:
        return bad
    for role, need in (("ink", LINT_LIMITS["ink"]), ("muted", LINT_LIMITS["muted"]),
                       ("dim", LINT_LIMITS["dim"]), ("accent", LINT_LIMITS["accent"]),
                       ("accent2", LINT_LIMITS["accent2"])):
        got = bg_contrast(th, role)
        if got < need:
            bad.append("%s 对背景对比度 %.2f < %.1f（bg/bg2 两端取最差）"
                       % (role, got, need))
    for role in ("good", "bad"):
        got = contrast(th[role], th["surface"])
        if got < LINT_LIMITS[role]:
            bad.append("%s 对卡片底色对比度 %.2f < %.1f" % (role, got, LINT_LIMITS[role]))
    gap = abs(luminance(th["bg"]) - luminance(th["bg2"]))
    if gap < LINT_LIMITS["lum_gap"]:
        bad.append("bg/bg2 亮度差 %.4f 太小（会像纯色底）" % gap)
    if luminance(th["bg"]) < 0.02 and float(th.get("qa", {}).get("luma_max", 0.045)) > 0.03:
        bad.append("暗色主题 bg 亮度 %.3f 贴空窗阈值，请把 qa.luma_max 降到 0.03 以下"
                   % luminance(th["bg"]))
    if th.get("layout") not in LAYOUTS:
        bad.append("layout=%r 不在 LAYOUTS 里" % th.get("layout"))
    if th.get("hud") not in ("chip-left", "corner-line", "none"):
        bad.append("hud=%r 不认识" % th.get("hud"))
    if not isinstance(th.get("item_stagger"), (int, float)) or th["item_stagger"] <= 0:
        bad.append("item_stagger 必须是正数")
    return bad


# ============================================================================
# 八、片内一致性：明暗族判定（段界不许跨族，否则必触发闪烁门禁）
# ============================================================================


def is_dark(th):
    return luminance(th["bg"]) < 0.35


def family_compatible(a, b):
    """两主题能否出现在同一部片里（同明暗族）。跨族=两部片，不是两段。"""
    return is_dark(a) == is_dark(b)


# ============================================================================
# 九、CLI：没有可列/可看的风格，只解释能力去了哪
# ============================================================================
#
# --list / --show / --lint 三个动作保留**壳**：说明"没有可选外观清单"并以 exit 2 退出。
# 不能返回 0 —— 在空表上 exit 0 是假通过，脚本会以为"列出来了"。


def _explain_removed(what):
    print("[stylekit] %s：本技能没有可选外观清单（不用内置风格表）。" % what,
          file=sys.stderr)
    print("  现在没有「可选外观清单」，每一版外观都是现场生成的：", file=sys.stderr)
    print("    python propose.py --brief \"题材/要求\" --script <film.json> --n 3", file=sys.stderr)
    print("    python propose.py --confirm --candidate A      # 冻结 work/design.json", file=sys.stderr)
    print("  想看某个设计长什么样 / 是否合规：", file=sys.stderr)
    print("    python designkit.py --lint <design.json>", file=sys.stderr)
    print("    python propose.py --show A", file=sys.stderr)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="视觉原语库（无内置风格：本命令只说明外观从哪来，不列清单）")
    ap.add_argument("--list", action="store_true", help="（没有此能力）列出外观")
    ap.add_argument("--show", metavar="ID", help="（没有此能力）查看某个外观")
    ap.add_argument("--lint", action="store_true", help="（没有此能力）批量体检内置主题")
    ap.add_argument("--json", action="store_true", help="配合 --show 输出 JSON")
    a = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                   # noqa: BLE001
        pass
    if a.show or a.list or a.lint:
        _explain_removed("--list / --show / --lint")
        # 退出码 2：与 render.py 的「未知取值」口径一致（不是 1 的「跑失败」）。
        return 2
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
