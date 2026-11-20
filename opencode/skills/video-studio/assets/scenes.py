# -*- coding: utf-8 -*-
"""scenes.py — 技能内置的场景渲染器（默认渲染路径 + 自定义场景的范例）。

⚠ 这**不是**给用户拷走改的模板：render.py 不写 --scenes 时会自动回落到本文件，
用户工程目录里只需要一个 film.json。想改画面时，在剧本同目录放一个自己的 scenes.py
（或 --scenes 显式指定），它会被优先发现；本文件就是那份自写场景的**参考实现**：
10 类构图 / 9 种母题（默认 plain：不画装饰细线）/ 6 种转场 / 35 字段主题 / 版式轮换 / 字幕参数
全部从计划里读。

唯一约定：提供 draw_frame(scene, t_local, dur, t_global, script, frame) -> PIL.Image
  scene   : timeline.json 里的一段（含 id/text/sub/start/end/dur 与 grammar 注入的
            scene_type / scene_variant / scene_decor / scene_layout / scene_reason）
  t_local : 本段内已经过秒数
  dur     : 本段总秒数
  t_global: 全片秒数
  script  : 剧本 dict（含 _style 计划、_total 总时长）
  frame   : 全片帧号（确定性：同一帧号必须画出同一张图）

画风从哪来
----------
render.py 把 designkit.design_to_plan() 的计划塞进 script["_style"]（主题 / 视觉语言 /
版式轮换 / 配乐 / 声线 / 节奏 / 字幕样式）。本文件从这份计划里取配色、材质、字体族、
版式、缓动、HUD、镜头缓推——**没有任何内置风格表**：
同一份代码画出来是什么样，完全由现场采样并确认过的 design.json 决定。

铁律（决定画质与可复现性）：
  1. 不许用 random / time.time()；要随机用 K.hash2 / K.hash3 / vnoise(seed)
  2. 每帧从零画，不依赖上一帧的变量
  3. 静态背景预渲染一次并缓存（见 _bg()），不要每帧重算渐变 / 纹理 / 暗角
  4. 文字排版一律用本文件的 _wrap / _text_w（带缓存）：直接调 wrap_cn 会让单帧慢 2-3 倍
  5. 所有正文落在 K.SAFE 内（y <= 974），字幕由 render.py 统一画在 y >= 872
  6. 每段至少一个动态元素，静止不要超过 6 秒；镜头缓推 3-15%
  7. 缺字形会画成方块：用 K.glyph_missing_chars 自查，别写 ✓ × ↵ 这类符号（改用矢量线条）
  8. 性能：1080p 单帧预算 ~15ms。重活（渐变/暗角/纹理/精灵）全部预烘焙进 _CACHE；
     不许每帧全屏高斯模糊 / 全屏双线性 resize（实测 30ms/帧，直接吃掉预算）
"""
from __future__ import annotations

import hashlib
import math
import os
import sys
import warnings

from PIL import Image, ImageDraw, ImageFilter
import numpy as np

# stylekit 的单向依赖：stylekit 不 import 本文件（也不 import filmkit），不会成环。
# 用户会把 scenes.py 与 scripts/*.py 拷到同一目录，那种情况下直接 import 就行；
# 本仓库里是 assets/scenes.py 与 scripts/stylekit.py 两个目录，所以兜底加 ../scripts。
try:
    import stylekit as ST
except ImportError:
    _SCRIPTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "scripts")
    _SCRIPTS = os.path.normpath(_SCRIPTS)
    if _SCRIPTS not in sys.path:
        sys.path.insert(0, _SCRIPTS)
    import stylekit as ST

import filmkit as K
# 只 import 模板**当下真的用到**的名字（多余导入会被 pyflakes 记一笔，也容易让人以为哪都能用）。
# 写自己的场景时，缓动/噪声直接走模块：K.lerp / K.ease_in / K.ease_in_out / K.vnoise / K.hash2 …
from filmkit import SAFE, SUB_BAND, W, H, clamp, smooth, ease_out, rng

# ============================================================================
# 零、legacy 绘制器（历史基线，**当前不可达**；一个字符都不许改）
# ============================================================================
#
# legacy = 老项目（film.json 没有 style 字段）的绘制路径。这组函数是逐字节基线，
# 一个字符都不许改：老版本的金帧 sha256 是拿它的输出对拍的。
#
# 注意：**它不是活的绘制路径**。draw_frame 已删掉 legacy 分支（见文件末尾 draw_frame
# 的 docstring），stylekit 也不再产出 legacy 计划。保留它是为了能复核历史基线、
# 对比新旧几何差异；新主题化代码只在 _legacy_* 之外生长，互不影响。
# 读代码时别误以为"还支持 legacy"——要做老外观请走 propose.py 的采样链路。

# ---------------- 主题（legacy 专用；新代码走 stylekit.STYLES / THEMES） ----------------
THEME = dict(
    bg_top=(7, 14, 26), bg_bot=(17, 33, 54),
    ink=(240, 245, 252), muted=(150, 174, 200), dim=(96, 118, 144),
    accent=(243, 132, 60), accent2=(86, 170, 232),
    good=(108, 178, 110), bad=(226, 96, 92),
)

_CACHE = {}


def _vgrad(w, h, top, bot):
    """竖直渐变背景 + 极轻网点（确定性），缓存复用。"""
    key = ("bg", w, h, top, bot)
    if key in _CACHE:
        return _CACHE[key]
    a = np.linspace(0, 1, h, dtype=np.float32)[:, None]
    arr = np.array(top, dtype=np.float32)[None, :] * (1 - a[..., None]) \
        + np.array(bot, dtype=np.float32)[None, :] * a[..., None]
    arr = np.repeat(arr, w, axis=1)
    yy, xx = np.mgrid[0:h, 0:w]
    arr = np.clip(arr + (((np.sin(xx * 0.31) * np.sin(yy * 0.31)) > 0.955)
                         .astype(np.float32) * 5.0)[..., None], 0, 255)
    im = Image.fromarray(arr.astype(np.uint8), "RGB")
    _CACHE[key] = im
    return im


def _vignette_mask(w, h, strength=0.40):
    key = ("vig", w, h, strength)
    if key in _CACHE:
        return _CACHE[key]
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    r = np.sqrt(((xx - w / 2.0) / (w / 2.0)) ** 2 + ((yy - h / 2.0) / (h / 2.0)) ** 2) / 1.4142
    m = np.clip((r - 0.40) / 0.60, 0, 1) ** 1.6 * strength
    mask = Image.fromarray((m * 255).astype(np.uint8), "L")
    _CACHE[key] = mask
    return mask


def _soft_dot(img, x, y, r, color, alpha=1.0):
    """柔边圆点精灵（预渲染一次，之后只 paste）。"""
    if r < 0.6 or alpha <= 0.004:
        return img
    key = ("dot", int(round(r * 2)), color)
    if key not in _CACHE:
        s = int(r * 2 + 6)
        im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        ImageDraw.Draw(im).ellipse([3, 3, s - 3, s - 3], fill=color + (255,))
        _CACHE[key] = im.filter(ImageFilter.GaussianBlur(max(1.0, r * 0.22)))
    spr = _CACHE[key]
    if alpha < 0.985:
        spr = spr.copy()
        spr.putalpha(spr.split()[3].point(lambda v: int(v * alpha)))
    img.paste(spr, (int(x - spr.width / 2), int(y - spr.height / 2)), spr)
    return img


def _legacy_bg():
    """预烘焙背景：渐变 + 暗角一次算好，每帧只 copy（实测省 ~4.5ms/帧）。"""
    if "baked" not in _CACHE:
        base = _vgrad(W, H, THEME["bg_top"], THEME["bg_bot"])
        _CACHE["baked"] = Image.composite(Image.new("RGB", (W, H), (0, 0, 0)),
                                          base, _vignette_mask(W, H, 0.40))
    return _CACHE["baked"]


def _legacy_hud(img, chapter, progress, title):
    d = ImageDraw.Draw(img, "RGBA")
    d.rectangle([0, 0, int(W * clamp(progress)), 3], fill=THEME["accent"])
    d.rectangle([0, 0, W, 3], fill=(255, 255, 255, 11))
    f = K.font(27, "reg")
    tw = K.text_w_cached(chapter, 27, "reg")
    d.rounded_rectangle([64, 42, 64 + tw + 46, 96], radius=27,
                        fill=(4, 10, 20, 150), outline=(255, 255, 255, 40))
    d.ellipse([64 + 16, 62, 64 + 26, 72], fill=THEME["accent"])
    d.text((64 + 34, 56), chapter, font=f, fill=(228, 236, 246))
    f2 = K.font(23, "reg")
    tw2 = K.text_w_cached(title, 23, "reg")
    d.text((W - 64 - tw2, 58), title, font=f2, fill=(255, 255, 255, 96))
    return img


def _legacy_cover(img, t, dur, kicker, big, sub, progress, title):
    d = ImageDraw.Draw(img, "RGBA")
    p = ease_out(rng(t, 0.0, 0.9))
    p2 = ease_out(rng(t, 0.25, 1.3))
    d.text((150, 300 - 26 * (1 - p)), kicker, font=K.font(30, "bold"),
           fill=THEME["accent"] + (255,))
    y = 352
    for ln in K.wrap_cn_cached(big, 84, 1560, "bold"):
        d.text((150, y), ln, font=K.font(84, "bold"), fill=THEME["ink"] + (255,))
        y += 104
    if sub:
        for j, ln in enumerate(K.wrap_cn_cached(sub, 38, 1400, "light")):
            d.text((150, y + 26 + j * 56), ln, font=K.font(38, "light"),
                   fill=THEME["muted"] + (int(242 * p2),))
    return _legacy_hud(img, "01 · 开场", progress, title)


def _legacy_scene_hook(scene, t, dur, tg, script, frame):
    img = _legacy_bg().copy()
    d = ImageDraw.Draw(img, "RGBA")
    cx, cy = W // 2, H // 2 - 40
    p = smooth(rng(t, 0.15, 2.6))
    for i in range(18):
        a = (i / 18.0) * 2 * math.pi + 0.2
        rr = 60 + 460 * p * (0.55 + 0.45 * K.hash2(i, 11))
        x2, y2 = cx + math.cos(a) * rr, cy + math.sin(a) * rr * 0.62
        d.line([cx, cy, x2, y2], fill=(255, 255, 255, int(70 * p)), width=1)
        if K.hash2(i, 9) > 0.45:
            img = _soft_dot(img, x2, y2, 3 + 5 * K.hash2(i, 5), THEME["accent2"], 0.55 * p)
    img = _soft_dot(img, cx, cy, 26 + 6 * math.sin(t * 2.2), THEME["accent"], 0.9 * rng(t, 0.1, 0.7))
    return _legacy_cover(img, t, dur, "00 · 开场", script.get("title", ""),
                         scene.get("lead") or "零素材，全代码生成", tg / max(0.1, script.get("_total", 1.0)),
                         script.get("title", ""))


def _legacy_scene_body(scene, t, dur, tg, script, frame):
    img = _legacy_bg().copy()
    d = ImageDraw.Draw(img, "RGBA")
    p = ease_out(rng(t, 0.0, 0.8))
    d.text((150, 250 + int(14 * (1 - p))), scene.get("kicker", ""),
           font=K.font(28, "bold"), fill=THEME["accent"] + (int(255 * p),))
    for i, ln in enumerate(K.wrap_cn_cached(scene.get("big") or scene["text"][:18], 64, 820, "bold")):
        d.text((150, 300 + i * 78), ln, font=K.font(64, "bold"), fill=THEME["ink"])
    if scene.get("sub"):
        for i, ln in enumerate(K.wrap_cn_cached(scene["sub"], 34, 820)):
            d.text((150, 420 + i * 48), ln, font=K.font(34, "reg"), fill=THEME["muted"])

    for i, it in enumerate(scene.get("items") or []):
        p_i = ease_out(rng(t, 0.45 + 0.32 * i, 1.15 + 0.32 * i))
        if p_i <= 0:
            continue
        x, y = 1030 + int(46 * (1 - p_i)), 240 + i * 118
        d.rounded_rectangle([x, y, x + 740, y + 96], radius=14,
                            fill=(255, 255, 255, int(16 * p_i)),
                            outline=(255, 255, 255, int(28 * p_i)))
        col = THEME["good"] if it.get("ok", True) else THEME["bad"]
        img = _soft_dot(img, x + 34, y + 48, 11, col, p_i)
        d = ImageDraw.Draw(img, "RGBA")
        d.text((x + 60, y + 28), it["text"], font=K.font(32, "reg"),
               fill=THEME["ink"] + (int(255 * p_i),))
    return _legacy_hud(img, scene.get("chapter") or "02 · 正文",
                       tg / max(0.1, script.get("_total", 1.0)), script.get("title", ""))


def _legacy_scene_outro(scene, t, dur, tg, script, frame):
    img = _legacy_bg().copy()
    d = ImageDraw.Draw(img, "RGBA")
    p = ease_out(rng(t, 0.0, 1.0))
    cx, cy = W // 2, H // 2 - 30
    for i in range(26):
        a = (i / 26.0) * 2 * math.pi + t * 0.15
        rr = 500 * (1 - p) + 40 + 18 * K.hash2(i, 4)
        img = _soft_dot(img, cx + math.cos(a) * rr, cy + math.sin(a) * rr * 0.5,
                        3, THEME["accent2"], 0.5 * (1 - p * 0.7))
    d = ImageDraw.Draw(img, "RGBA")
    lines = K.wrap_cn_cached(scene.get("big") or script.get("end_text") or "完", 96, 1500, "bold")
    alpha = int(255 * ease_out(rng(t, 0.3, 1.5)))
    for i, ln in enumerate(lines):
        d.text(((W - K.text_w_cached(ln, 96, "bold")) / 2, 380 + i * 118), ln,
               font=K.font(96, "bold"), fill=THEME["ink"] + (alpha,))
    return _legacy_hud(img, "99 · 收尾", 1.0, script.get("title", ""))


_LEGACY_DRAWERS = {
    "hook": _legacy_scene_hook,
    "body": _legacy_scene_body,
    "outro": _legacy_scene_outro,
}


# ============================================================================
# 一、风格计划 / 主题
# ============================================================================

_PLAN_CACHE = {}
_THEME_WARNED = set()
# 默认不推镜。慢速推镜在整数光栅上必然抖：4% 的推镜摊到 8 秒 = 0.03px/帧，
# 取整后变成「每 5 帧跳 1px」，观众看到的是抖而不是推（见 _view 的注释）。
_DEFAULT_PUSH = 0.0


def _warn_once(key, msg):
    if key in _THEME_WARNED:
        return
    _THEME_WARNED.add(key)
    warnings.warn(msg, RuntimeWarning, stacklevel=3)


def _plan(script):
    """取整片的风格计划：render.py 会把解析好的计划放进 script["_style"]。

    没有就自己解一次（纯函数，同参数同结果）。每帧都会调用，所以只留一条缓存；
    键里带 id(script) 之外的稳定字段，避免脚本对象被回收后 id 复用串档。
    """
    p = script.get("_style")
    if isinstance(p, dict):
        return p
    key = (id(script), script.get("title"), len(script.get("lines") or []),
           script.get("style"), bool(script.get("pacing")))
    got = _PLAN_CACHE.get(key)
    if got is None:
        got = ST.resolve_script_style(script)
        _PLAN_CACHE.clear()
        _PLAN_CACHE[key] = got
    return got


# 主题里这些键的值是 RGB，统一要 (r,g,b) 元组。JSON 往返会把它变成 list，
# 而 list 不能当缓存键 —— _soft_dot / _bg 之类的 key 里都带着颜色，一旦错位就是
# "unhashable type: 'list'"。实测触发路径：--stage frames --jobs N 把 script 写成
# _script_snapshot.json，子进程读回来重建，于是 theme.bg 是 [8,8,10]。
# 修在**入口**（_scene_theme）而不是每个缓存点：缓存点会越加越多，入口只有一个。
_THEME_RGB_KEYS = ("bg", "bg2", "surface", "ink", "muted", "dim",
                   "accent", "accent2", "good", "bad", "rule", "vignette_tint")


def _freeze_theme(th):
    """把主题里的颜色值统一成元组（就地不重复建字面量的前提下返回新 dict）。"""
    out = dict(th)
    for k in _THEME_RGB_KEYS:
        v = out.get(k)
        if isinstance(v, (list, tuple)) and len(v) >= 3:
            out[k] = (int(v[0]), int(v[1]), int(v[2]))
    # scale / card / qa 是嵌套 dict，外面只读不建缓存键，原样保留
    return out


def _scene_theme(plan, scene):
    """本段用哪套主题：全片主题 + 可选的 scene["theme"] 覆盖（只许同明暗族）。"""
    base = plan.get("theme")
    if isinstance(base, dict):
        th = _freeze_theme(base)
    else:
        th = dict(ST.THEMES[plan["theme_id"]])
    sid = scene.get("theme")
    if not sid or str(sid) == str(th.get("id")):
        return th
    alt = ST.THEMES.get(str(sid))
    if alt is None:
        _warn_once(("unknown", str(sid)),
                   "场景 %r 指定了未知主题 %r，已忽略（可用：%s）"
                   % (scene.get("id"), sid, " / ".join(ST.STYLE_IDS)))
        return th
    if not ST.family_compatible(th, alt):
        # 跨明暗族 = 两部片，不是两段：段界会整屏换底，QA 的闪烁门禁必炸。
        _warn_once(("family", str(th.get("id")), str(sid)),
                   "场景 %r 想从主题 %s 换到 %s，两者明暗族不同（同片换族会触发闪烁门禁），已忽略"
                   % (scene.get("id"), th.get("id"), sid))
        return th
    return _freeze_theme(alt)


# ---------------- 包内小工具 ----------------

def _mix(a, b, p):
    """两色线性插值（p=0 -> a，p=1 -> b）。"""
    q = clamp(p)
    return (int(round(a[0] + (b[0] - a[0]) * q)),
            int(round(a[1] + (b[1] - a[1]) * q)),
            int(round(a[2] + (b[2] - a[2]) * q)))


def _rgba(c, alpha=255):
    """RGB + 0..255 透明度 -> 4 元组（PIL 的 RGBA 画笔要它）。"""
    return (int(c[0]), int(c[1]), int(c[2]),
            int(max(0, min(255, round(alpha)))))


def _ease(name):
    """风格包里的缓动名 -> 函数（四个都满足 f(0)=0 / f(1)=1）。"""
    got = _EASES.get(name)
    if got is not None:
        return got
    if name == "out5":
        got = lambda p: 1.0 - (1.0 - clamp(p)) ** 5
    elif name == "inOut3":
        got = K.ease_in_out
    elif name == "outBack":
        got = K.ease_out_back
    else:
        got = K.ease_out
    _EASES[name] = got
    return got


_EASES = {}


# ---------------- 字体：family 参数（旧 filmkit 没有就自动回退） ----------------

_FONT_CACHE = {}
_FAMILY_OK = None


def _family_ok():
    """filmkit.font 是否支持 family=...（老版本没有就回退旧签名）。"""
    global _FAMILY_OK
    if _FAMILY_OK is None:
        try:
            K.font(16, "reg", "sans")
            _FAMILY_OK = True
        except TypeError:
            _FAMILY_OK = False
        except Exception:                    # noqa: BLE001 - 字体族实现有 bug 时退回旧签名
            _FAMILY_OK = False
    return _FAMILY_OK


# 字号档：由 layoutkit 的 profile 给（横屏 1.0 = 原样；竖屏 0.68）。
# 为什么不再写 W/1920：竖屏不是"把横屏缩小"，而是换了一个观看距离——手机上正文
# 纯按 0.5625 缩完只剩 ~26px，小于舒适阅读尺寸。档位定义与理由都在 layoutkit 的 profile
# 注释里（单一真源），这里只读值；横屏 type_scale=1.0，因此对既有画面零影响。
_TYPE_SCALE = float(getattr(K, "TYPE_SCALE", 1.0))

# 每次"文字被钳进安全区"都记一笔（文本片段/字号/宽度）。竖屏下它是**诊断表**：
# 条目多说明该段版式在小画布上挤不下，该改的是版式，不是把字号再调小。
# 横屏永远不写这个列表（`_fit_px` 在 W==1920 时直接返回），所以它对既有行为零影响。
_CLAMPED = []

# 正文可用的**下边界**：字幕带顶再留 16px 间隙。
# 以前这里到处写 856（= 1080 版字幕带上沿 872 - 16），竖屏下它落在画面中间，
# 于是卡片区会被压到上半屏。改成从 layoutkit 的 SUB_BAND 派生，两个 profile 都成立。
_BODY_LIMIT = int(SUB_BAND[1]) - 16


def _font(th, size, weight="reg"):
    size = max(6, int(round(int(size) * _TYPE_SCALE)))
    fam = str(th.get("family") or "sans")
    key = (size, weight, fam)
    f = _FONT_CACHE.get(key)
    if f is None:
        if _family_ok():
            try:
                f = K.font(size, weight, fam)
            except Exception:                # noqa: BLE001 - 某族字体缺失时退回到默认字体
                f = K.font(size, weight)
        else:
            f = K.font(size, weight)
        _FONT_CACHE[key] = f
    return f


_TW_CACHE = {}
_WRAP_CACHE = {}


def _text_w(text, size, weight, th):
    """带缓存的行宽（按主题字体族；老 filmkit 回退到 K.text_w_cached）。"""
    if not text:
        return 0.0
    size = max(6, int(size))
    fam = str(th.get("family") or "sans")
    key = (text, size, weight, fam)
    got = _TW_CACHE.get(key)
    if got is None:
        if len(_TW_CACHE) > 40000:
            _TW_CACHE.clear()
        if _family_ok():
            got = K.text_w(text, _font(th, size, weight))
        else:
            got = K.text_w_cached(text, size, weight)
        _TW_CACHE[key] = got
    return got


def _wrap(text, size, max_w, weight, th):
    """带缓存的换行（同上）。"""
    if not text:
        return []
    size = max(6, int(size))
    max_w = max(80, int(max_w))
    fam = str(th.get("family") or "sans")
    key = (text, size, max_w, weight, fam)
    got = _WRAP_CACHE.get(key)
    if got is None:
        if len(_WRAP_CACHE) > 20000:
            _WRAP_CACHE.clear()
        if _family_ok():
            got = K.wrap_cn(text, _font(th, size, weight), max_w)
        else:
            got = K.wrap_cn_cached(text, size, max_w, weight)
        _WRAP_CACHE[key] = got
    return got


def _fit_size(text, max_w, size, weight, th, floor=16):
    """按卡片可用宽度缩字号（条目文字长短不一，不能撑破卡片）。"""
    s = int(size)
    while s > floor and _text_w(text, s, weight, th) > max_w:
        s -= 2
    return s


# ---------------- 镜头缓推（只推背景）+ 前景锁定 ----------------

class _View(object):
    """设计坐标 -> 帧坐标。

    为什么不是整帧重采样：实测 1080p 双线性整帧变换 29ms/帧（预算 15ms），
    而整帧按 push 放大又会把 150px 边距的内容推出 SAFE。所以分两层：
      背景层做**真缩放**（端点预烘 + blend，约 4.4ms，见 _plate_zoom/_bg_plate）；
      前景层**不进相机**（`_view` 恒 1.0）：整像素落位就不会被 1px 台阶抖碎，
      慢速亚像素位移在 1x 光栅上只能靠超采样解决，而超采样超单帧预算。
    """
    __slots__ = ("s", "cx", "cy")

    def __init__(self, s):
        self.s = float(s)
        self.cx = W * 0.5
        self.cy = H * 0.5

    def x(self, x):
        return int(round(self.cx + (float(x) - self.cx) * self.s))

    def y(self, y):
        return int(round(self.cy + (float(y) - self.cy) * self.s))

    def r(self, r):
        return max(0.0, float(r) * self.s)


def _push_scale(th, t_local, dur):
    """镜头缓推：t=0 -> 1.0，t=dur -> 1+push，单调平滑（端点必须收敛，不许抖）。"""
    u = clamp(t_local / max(0.05, float(dur)))
    return 1.0 + float(th.get("push") or _DEFAULT_PUSH) * smooth(u)


def _view(th, s_bg):
    """前景锁定：只有背景板随镜头缩放，前景永远按整像素落位。

    为什么前景不再跟镜头：`_View.x/y` 是 int(round())，而镜头推 4% 时前景总位移
    只有 7px，摊到 8 秒就是 0.03px/帧 —— 取整后是「每 5 帧整体跳 1px」，实测一次跳跃
    就有 5000+ 个像素差 >40 灰阶，观众看到的是抖动而不是推镜。纯慢速亚像素位移在
    1x 光栅上无法免费消除（要 4x 超采样，超单帧预算 15ms），所以本版把相机改成
    「只推背景」：静止的整像素文字永远不会抖，背景有端点预烘 + blend 兜底（0~1 灰阶）。
    要整幅推镜请在自己的 scenes.py 里做超采样，别把慢速位移塞回这条路径。
    """
    return _View(1.0)


_PLAIN_LINE = 6


def _plain(lang):
    """本片是否是「素净」设计（默认）：不画任何装饰细线。"""
    return str(((lang or {}).get("motif") or {}).get("kind") or "") == "plain"


def _lw(width, lang):
    """装饰描边的粗细：素净设计下一律抬到 _PLAIN_LINE，其余设计保持原值。"""
    w = int(width)
    return _PLAIN_LINE if _plain(lang) and w < _PLAIN_LINE else w


# ---------------- 背景：按主题烘焙一次（渐变 + 材质 + 颗粒 + 暗角） ----------------

def _grid_xy():
    yy = np.arange(H, dtype=np.float32)[:, None]
    xx = np.arange(W, dtype=np.float32)[None, :]
    return yy, xx


def _paper_noise(th):
    """纸纹：纤维条纹（vnoise）+ 多尺度斑块（hash2 小图上采样）。一次烘焙，不进逐帧路径。"""
    freq = float(th.get("grain_freq") or 0.27)
    xs = np.arange(W, dtype=np.float32) * freq * 0.35
    fiber = np.array([K.vnoise(float(v), 3) for v in xs], dtype=np.float32) - 0.5
    fine = np.array([K.vnoise(float(v) * 2.3, 9) for v in xs], dtype=np.float32) - 0.5
    rows, cols = 27, 48
    blobs = np.array([[K.hash2(r * 31 + 7, c * 17 + 3) for c in range(cols)]
                      for r in range(rows)], dtype=np.float32) - 0.5
    blob_img = Image.fromarray(((blobs + 0.5) * 255.0).astype(np.uint8), "L")
    blobs = np.asarray(blob_img.resize((W, H), Image.BICUBIC), dtype=np.float32) / 255.0 - 0.5
    return blobs * 0.55 + fiber[None, :] * 0.30 + fine[None, :] * 0.15


def _texture_layer(th, yy, xx):
    """背景材质层（灰度偏移；深色主题提亮、浅色主题压暗）。"""
    kind = str(th.get("texture") or "none")
    alpha = float(th.get("texture_alpha") or 0.0)
    if kind == "none" or alpha <= 0.0:
        return None
    sign = 1.0 if ST.is_dark(th) else -1.0
    amp = sign * 255.0 * alpha * 0.44
    if kind == "dots":
        f = float(th.get("grain_freq") or 0.31)
        layer = ((np.sin(xx * f) * np.sin(yy * f)) > 0.955).astype(np.float32)
    elif kind == "grid":
        step = 88.0
        layer = (((xx % step) < 1.0) | ((yy % step) < 1.0)).astype(np.float32)
    elif kind == "scanlines":
        layer = ((yy % 4.0) < 1.0).astype(np.float32)
    elif kind == "paper":
        layer = _paper_noise(th)
    else:
        return None
    return layer * amp


def _grain_layer(th, yy, xx):
    """确定性颗粒（坐标 hash，等价 vnoise 的 2D 版本；不用 random）。"""
    g = float(th.get("grain") or 0.0)
    if g <= 0.05:
        return None
    xi = np.arange(W, dtype=np.int64)[None, :]
    yi = np.arange(H, dtype=np.int64)[:, None]
    v = (xi * 374761393 + yi * 668265263) & 0xFFFFFFFF
    v = (v ^ (v >> 13)) & 0xFFFFFFFF
    v = (v * 1274126177) & 0xFFFFFFFF
    v = (v ^ (v >> 16)) & 0xFFFFFFFF
    noise = (v & 0xFFFF).astype(np.float32) / 65535.0 - 0.5
    flat = np.empty((H, W), dtype=np.float32)
    np.add(noise * (g * 1.2), 0.0, out=flat)
    return flat


def _vignette_layer(th, yy, xx):
    """暗角：浅色主题压向 vignette_tint（米灰），绝不压向纯黑。"""
    strength = float(th.get("vignette") or 0.0)
    if strength <= 0.0:
        return None
    r = np.sqrt(((xx - W * 0.5) / (W * 0.5)) ** 2 + ((yy - H * 0.5) / (H * 0.5)) ** 2) / 1.4142
    m = np.clip((r - 0.40) / 0.60, 0.0, 1.0) ** 1.6 * strength
    return m


def _hashable(v):
    """把 list 递归转成 tuple：主题从 JSON 读回来时元组会变成 list，而 list 不能当 dict 键。

    为什么需要它：--jobs >1 时 render.py 的 worker 从剧本快照读 script，主题色的
    (r,g,b) 元组变成 [r,g,b]，于是下面 _bg 的缓存键抛 unhashable type: "list"。
    render.py 已按剧本重解风格，但这层防御留着——scenes.py 是用户会拷走改的模板。
    """
    if isinstance(v, (list, tuple)):
        return tuple(_hashable(x) for x in v)
    if isinstance(v, dict):
        return tuple(sorted((k, _hashable(x)) for k, x in v.items()))
    return v


def _bg(th):
    """预烘焙背景：主题色渐变 + 材质 + 颗粒 + 暗角，一次算好，逐帧只 copy / 变换。"""
    key = _hashable(("baked", th.get("id"), th["bg"], th["bg2"], th["texture"], th["grain"],
                     th["vignette"], th["vignette_tint"], th["texture_alpha"],
                     th["grain_freq"]))
    got = _CACHE.get(key)
    if got is not None:
        return got
    yy, xx = _grid_xy()
    a = np.linspace(0.0, 1.0, H, dtype=np.float32)[:, None, None]
    top = np.array(th["bg"], dtype=np.float32)[None, None, :]
    bot = np.array(th["bg2"], dtype=np.float32)[None, None, :]
    arr = np.repeat(top * (1.0 - a) + bot * a, W, axis=1)
    tex = _texture_layer(th, yy, xx)
    if tex is not None:
        arr = arr + tex[:, :, None]
    grain = _grain_layer(th, yy, xx)
    if grain is not None:
        arr = arr + grain[:, :, None]
    np.clip(arr, 0.0, 255.0, out=arr)
    vig = _vignette_layer(th, yy, xx)
    if vig is not None:
        tint = np.array(th["vignette_tint"], dtype=np.float32)[None, None, :]
        arr = arr * (1.0 - vig)[:, :, None] + tint * vig[:, :, None]
        np.clip(arr, 0.0, 255.0, out=arr)
    im = Image.fromarray(arr.astype(np.uint8), "RGB")
    _CACHE[key] = im
    return im


_ZOOM_ORDER = []


def _zoom_tag(th, extra=None):
    """取景端点的缓存标签：主题决定底图，extra 用来区分装饰母题档位。"""
    return _hashable(("zoom", th.get("id"), th["bg"], th["bg2"], th["texture"],
                      th["grain"], th["grain_freq"], th["vignette"],
                      th["vignette_tint"], th["texture_alpha"], extra))


def _plate_zoom(src, s, th, tag):
    """按镜头缓推取景：端点预烘 + 逐帧 blend。

    为什么不再用 NEAREST 直采：底图把颗粒（`_grain_layer`）一起烘了进去，颗粒是
    逐像素的高频图案；NEAREST 在缓慢缩放时采样网格会整像素跳变，颗粒图案随之整体
    错位 1px，观感就是"满屏噪点在沸腾"——用户实测反馈是"明显的抖动感"，而背景-only
    的帧差只有 4~6 灰阶，单帧闪烁门禁根本抓不到。

    做法：s=1.0 与 s=1+push 两个端点各烘一张 BILINEAR 底版（每张约 60ms，只做一次），
    逐帧用 `Image.blend` 在两端点间线性插值（实测 4.4ms/帧）。端点内部是**平滑重采样**，
    没有 NEAREST 的网格跳变，帧差降到 0~1 灰阶；同时保留颗粒质感（不是把 grain 抹掉）。

    上限取 1+push：`_push_scale` 恒 <= 1+push，超出的 s 一律钳到端点上，不会外推。
    """
    if s <= 1.0008:
        return src.copy()
    cap = 1.0 + max(0.0, float(th.get("push") or _DEFAULT_PUSH))
    if cap <= 1.0008:
        return src.copy()
    s = min(float(s), cap)
    key = _hashable(("zpts", tag, round(cap, 6)))
    pair = _CACHE.get(key)
    if pair is None:
        lo = src.copy()
        bw, bh = W / cap, H / cap
        x0 = (W - bw) * 0.5
        y0 = (H - bh) * 0.5
        hi = src.transform((W, H), Image.EXTENT, (x0, y0, x0 + bw, y0 + bh), Image.BILINEAR)
        pair = (lo, hi)
        _CACHE[key] = pair
        _ZOOM_ORDER.append(key)
        while len(_ZOOM_ORDER) > 4:              # 一对端点约 12MB，限量防多片同进程膨胀
            old = _ZOOM_ORDER.pop(0)
            if old != key:
                _CACHE.pop(old, None)
    lo, hi = pair
    w = (s - 1.0) / (cap - 1.0)
    if w <= 0.0:
        return lo.copy()
    if w >= 1.0:
        return hi.copy()
    return Image.blend(lo, hi, w)


def _bg_plate(th, s):
    """逐帧背景：按镜头缩放取景（端点预烘 + blend，约 4.4ms/帧；见 _plate_zoom）。"""
    return _plate_zoom(_bg(th), s, th, _zoom_tag(th, "bg"))


# ---------------- 版式几何（全部从 ST.LAYOUTS 取） ----------------

def _title_anchor(lay):
    """标题锚点 + 对齐方式：left-stack 左对齐 / split-right 右对齐 / center-stack 居中。

    非横屏时把左对齐的锚点**钳进安全区**：`title_x=1030` 在 1920 宽里是"右栏起点"，
    折算到 1080 宽是 579，而左对齐的标题要往后排 400+px，直接从右边出画——实测竖屏
    的 split-right 会把标题切掉半个字。钳到 `SAFE[2] - title_w` 之后，右栏标题会往左让，
    与右栏卡片的宽度对齐（`panel_w` 已按同一比例折算）。

    横屏**逐字节不变**：W==1920 时直接走原路径（golden_frames 守这条）。
    """
    if lay.get("center"):
        return W * 0.5, "center"
    if lay.get("title_x") is None:
        return float(W - 150), "right"
    x = float(lay["title_x"])
    if W != 1920:
        try:
            tw = float(lay.get("title_w") or 0.0)
        except (TypeError, ValueError):
            tw = 0.0
        if tw > 0:
            x = max(float(SAFE[0]), min(x, float(SAFE[2]) - tw))
    return x, "left"


def _panel_x(lay):
    if lay.get("panel_x") is None:
        return (W - float(lay["panel_w"])) * 0.5
    return float(lay["panel_x"])


def _panel_boxes(lay, th, n, y_floor, v):
    """条目卡片布局：选列数 + 压缩卡片高度，保证①不压标题②不越字幕带（_BODY_LIMIT）。

    panel_gap 在 stylekit 里是**行距（pitch）**不是间隙：按 240 + i*118 排卡片时，
    所以 pitch 至少得比卡片高度多 6px，否则卡片会叠在一起。条目多时先加列，再压高度，
    最后才截断——静默重叠或画到字幕带下面比少画两条更糟。
    """
    if n <= 0:
        return []
    px = _panel_x(lay)
    pw = float(lay["panel_w"])
    base_pitch = max(float(lay["panel_gap"]), float(lay["panel_h"]) + 6.0)
    ch_base = float(lay["panel_h"])
    y0 = max(float(lay["panel_y"]), float(y_floor))
    ylim = v.cy + (float(_BODY_LIMIT) - v.cy) / max(0.2, v.s)
    avail = max(80.0, ylim - y0)
    if lay.get("center"):
        cand = (1, 2) if n <= 2 else ((2, 3) if n <= 6 else (3, 2))
    else:
        cand = (1, 2) if n <= 5 else ((2, 1) if n <= 10 else (2,))
    chosen = None
    for cols in cand:
        rows = int(math.ceil(n / float(cols)))
        for ch in (ch_base, ch_base * 0.82, 54.0):
            pitch = min(base_pitch, (avail - ch) / max(1, rows - 1)) if rows > 1 else base_pitch
            if pitch < ch + 6.0:
                continue
            if (rows - 1) * pitch + ch <= avail + 0.5:
                chosen = (cols, rows, ch, pitch)
                break
        if chosen:
            break
    if chosen is None:
        cols = cand[-1]
        ch, pitch = 54.0, 60.0
        rows = int(max(1, (avail + 6.0) // pitch))
        chosen = (cols, rows, ch, pitch)
    cols, rows, ch, pitch = chosen
    cap = rows * cols
    if n > cap:
        _warn_once(("panel", lay.get("layout"), n),
                   "本段有 %d 个条目，版式只放得下 %d 个（其余已省略）；"
                   "请拆成多段或减少 items" % (n, cap))
    gx = 40.0 if cols > 1 else 0.0
    cw = (pw - gx * (cols - 1)) / cols
    boxes = []
    for i in range(min(n, cap)):
        r, c = divmod(i, cols)
        boxes.append((px + c * (cw + gx), y0 + r * pitch, cw, ch))
    return boxes


# ---------------- 文字绘制（位置过视差，字号不重采样） ----------------

def _fit_px(px, w, text_snippet="", size=0):
    """把一段文字的左边界钳进安全区（只在非横屏生效），并记下每次钳位。

    为什么把这个兜底放在 `_draw_text` 而不是各构图的锚点函数里：竖屏下每个构图都会用自己的
    常量算 ax（`150+bdx+bins`、`W-150`、`W*0.5`…），逐个去改等于把"不出画"的责任散到 10 处，
    漏一处就是一段文字被切掉——实测 split-right 的标题与 kicker 都是从 930 起向右排、被右边缘
    切掉半个字。放在唯一的绘制入口，是**保证**而不是**猜测**。

    横屏（W==1920）直接原样返回：这条路径必须逐字节不变（golden_frames 守）。
    """
    if W == 1920:
        return px
    x0, x1 = float(SAFE[0]), float(SAFE[2])
    if w >= (x1 - x0):
        _CLAMPED.append((text_snippet, size, float(w)))
        return int(round(x0))
    fitted = max(x0, min(float(px), x1 - w))
    if abs(fitted - float(px)) > 0.5:
        _CLAMPED.append((text_snippet, size, float(w)))
    return int(round(fitted))


def _draw_text(img, th, v, text, x, y, size, weight, color, align="left",
               alpha=1.0, tracking=0.0):
    text = "" if text is None else str(text)
    if not text or alpha <= 0.004:
        return
    size = int(size)
    px = v.x(x)
    py = v.y(y)
    font = _font(th, size, weight)
    fill = _rgba(color, 255.0 * clamp(alpha))
    d = ImageDraw.Draw(img, "RGBA")
    tr = float(tracking or 0.0)
    if tr > 0.3 and len(text) > 1:
        total = _text_w(text, size, weight, th) + tr * (len(text) - 1)
        if align == "center":
            px -= int(total * 0.5)
        elif align == "right":
            px -= int(total)
        px = _fit_px(px, total, text[:16], size)
        for ch in text:
            d.text((px, py), ch, font=font, fill=fill)
            px += _text_w(ch, size, weight, th) + tr
        return
    w = _text_w(text, size, weight, th)
    if align == "center":
        px -= int(w * 0.5)
    elif align == "right":
        px -= int(w)
    px = _fit_px(px, w, text[:16], size)
    d.text((px, py), text, font=font, fill=fill)


def _entrance_x(th, p, align):
    dx = float(th.get("entrance_dx") or 0)
    if align == "right":
        return -dx * (1.0 - p)
    if align == "center":
        return 0.0
    return dx * (1.0 - p)


def _entrance_y(th, p):
    return float(th.get("entrance_dy") or 0) * (1.0 - p)


# ============================================================================
# 二、HUD（三档：chip-left / corner-line / none）
# ============================================================================

def _hud(img, th, chapter, progress, title, lang=None):
    style = str(th.get("hud") or "none")
    if style == "none":
        return img
    accent = th["accent"]
    rule = th["rule"]
    ink = th["ink"]
    prog = clamp(progress)
    # 遮幅设计里顶部 40px 是黑边：HUD 整体下移，别把胶囊/角线压在黑边上（实测会压）。
    mf = (lang or {}).get("motif") or {}
    oy = int((mf.get("detail") or {}).get("top") or 40) \
        if str(mf.get("kind") or "") == "letterbox" else 0
    d = ImageDraw.Draw(img, "RGBA")
    if style == "chip-left":
        # 通栏进度条按"块"画（6px），不按 1~3px 细线画：细线在压缩后最容易显脏。
        d.rectangle([0, 0, W, 6], fill=_rgba(rule, 14))
        d.rectangle([0, 0, int(W * prog), 6], fill=_rgba(accent, 255))
        cs = int(th["scale"]["chip"][0])
        tw = _text_w(chapter, cs, "reg", th)
        pad_l, pad_r = 16, 18
        h = int(cs * 1.95)
        x0, y0 = 64, 42 + oy
        x1 = x0 + pad_l + tw + pad_r
        fillc = _mix(th["surface"], th["bg"], 0.55)
        d.rounded_rectangle([x0, y0, x1, y0 + h], radius=h // 2,
                            fill=_rgba(fillc, 168), outline=_rgba(rule, 36))
        d.ellipse([x0 + 16, y0 + h // 2 - 5, x0 + 26, y0 + h // 2 + 5],
                  fill=_rgba(accent, 255))
        d.text((x0 + 34, y0 + (h - cs) // 2 - 3), chapter,
               font=_font(th, cs, "reg"), fill=_rgba(ink, 228))
        bs = int(th["scale"]["brand"][0])
        tw2 = _text_w(title, bs, "reg", th)
        d.text((W - 64 - tw2, 56 + oy), title, font=_font(th, bs, "reg"),
               fill=_rgba(_mix(ink, th["bg"], 0.62), 220))
        return img
    # corner-line：只有角标与细线，没有胶囊底（细线也要过 _lw：素净下不许出现 2px 线）
    d.rectangle([0, 0, W, 6], fill=_rgba(rule, 10))
    d.rectangle([0, 0, int(W * prog), 6], fill=_rgba(accent, 214))
    d.line([64, 44 + oy, 64, 96 + oy], fill=_rgba(accent, 190), width=_lw(2, lang))
    d.line([64, 44 + oy, 116, 44 + oy], fill=_rgba(accent, 190), width=_lw(2, lang))
    cs = int(th["scale"]["chip"][0])
    d.text((84, 54 + oy), chapter, font=_font(th, cs, "reg"), fill=_rgba(ink, 220))
    bs = int(th["scale"]["brand"][0])
    tw = _text_w(title, bs, "reg", th)
    d.text((W - 64 - tw, 56 + oy), title, font=_font(th, bs, "reg"), fill=_rgba(ink, 120))
    d.line([W - 64, 44 + oy, W - 64 - 96, 44 + oy], fill=_rgba(accent, 120), width=_lw(2, lang))
    d.line([W - 64, 44 + oy, W - 64, 96 + oy], fill=_rgba(accent, 120), width=_lw(2, lang))
    return img


# ============================================================================
# 三、场景语法（10 类构图 × 8 套视觉语言）
# ============================================================================
#
# 为什么重写这一节
# ---------------
# 旧的 _draw_hook / _draw_body / _draw_outro 是"一套骨架 + 换色"：背景板 → 眉标 →
# 大字 → 副标题 → 强调线 → HUD，三种 kind 只差字号与位置。于是 8 套风格换完只剩配色不同。
# 现在把"画什么"（scene type，由 grammar.py 按内容选）与"怎么画"（视觉语言，由 stylekit
# 的 VISUAL_LANGUAGES 给）拆开：
#   · 10 类构图各自有独立的视觉重心与留白（hero 偏左上 / statement 正中 / compare 强制对称 …）
#   · 每套风格有自己的装饰母题（括角 / 手绘内框 / letterbox / 信号条 …）与转场签名
#   · 片内还有 variant 轮换，避免"每屏同一个骨架"
#
# 性能纪律（这一节最容易把 25ms 预算吃光）
#   · 装饰母题全部**预烘焙**成一张 RGBA 图层，逐帧只 paste（~0.3ms）
#   · 文字用 _draw_text（字号不重采样，只过视差坐标）
#   · 禁每帧全屏 GaussianBlur（30–60ms）/ 全屏双线性 resize（29ms）/ np.array 往返（8–11ms）
#   · 段界 veil 只在段首 ≤0.45s 内生效，用 base plate 直接 paste/blend，不新增模糊

# ---------------- 视觉语言 / 场景字段 ----------------

_DEFAULT_LANG = {
    "motion": {"ease": "out3", "enter": (0.45, 0.85), "push": 0.0, "breathe_hz": 0.85},
    "motif": {"kind": "plain", "density": 0.0, "detail": {}},
    "transition": {"kind": "cut", "dur": 0.0},
    "typography": {"tracking": 0.0, "line_gap": 1.30, "align": "left",
                   "number": "dim", "ratio": 1.50},
    "layout_cycle": ("left-stack",),
}

_DECOR_LEVELS = (0.55, 0.75, 1.0)


def _lang(plan, th):
    """本片的视觉语言包。plan["language"] 由 stylekit 解析时塞进来；没有就按主题 id 查。"""
    got = (plan or {}).get("language")
    if isinstance(got, dict):
        return got
    try:
        return ST.visual_language(str((th or {}).get("id") or "tech-deep"))
    except Exception:                                    # noqa: BLE001 - 老 stylekit 没有这一层
        return _DEFAULT_LANG


def _scene_type(scene):
    """本段用哪个绘制器：优先 timeline 里 grammar 写好的 scene_type，其次按 kind 映射。"""
    t = str(scene.get("scene_type") or "").strip()
    if t:
        return t
    kind = str(scene.get("kind") or "body")
    return {"hook": "hero", "outro": "outro", "quote": "quote",
            "emphasis": "statement", "calm": "statement"}.get(kind, "list")


def _decor_level(scene):
    """本段的装饰密度档（0/1/2）。容错：脏数据当 0，绝不让 int() 把整帧渲染炸掉。"""
    try:
        lvl = int(scene.get("scene_decor") or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, min(2, lvl))


def _decor_scale(scene, lang):
    """装饰强度 = 片内密度档（0/1/2 -> 0.55/0.75/1.0）× 该风格母题密度。"""
    if _plain(lang):
        # 素净母题：装饰层整层不画（细线、网格、纹理都是装饰，默认设计里一个都不要）。
        return 0.0
    base = _DECOR_LEVELS[_decor_level(scene)]
    dens = float((lang.get("motif") or {}).get("density") or 1.0)
    return base * dens


def _progress(tg, script):
    """全片进度（0..1），HUD 进度条用。_total 由 render.py 注入（st_frames/st_stills）。"""
    return clamp(float(tg) / max(0.1, float(script.get("_total") or 1.0)))


def _phase(scene):
    """段相位（弧度）。NaN/Inf 一律当 0 —— 它们会让 int(round()) 直接抛
    ValueError: cannot convert float NaN to integer，而那是在渲染第 N 帧才炸。"""
    try:
        v = float(scene.get("scene_phase") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if v != v or v in (float("inf"), float("-inf")):        # NaN / ±Inf
        return 0.0
    return v


def _variant(scene):
    try:
        return int(scene.get("scene_variant") or 0) % 3
    except (TypeError, ValueError, OverflowError):
        return 0


def _scene_layout(scene, th):
    """本段版式：grammar 按 chapter 写好的 scene_layout 优先，其次主题默认。

    竖屏下把 ST.LAYOUTS 的**几何**按画布比例折算（见 `_fit_layout`）。这是"几何缩放"，
    不是"版式重设计"——`left-stack` 在竖屏仍然是左文右卡，只是两者都更窄、纵向更舒展。
    真要做竖屏原生版式（list 纵向堆叠、compare 上下对照），那是 portrait-and-motion.md
    的第三步，需要新录金帧、重算若干断言阈值，不能顺手塞在这里。
    """
    name = str(scene.get("scene_layout") or th.get("layout") or "left-stack")
    if H > W:
        # 竖屏：直接用**原生**几何，不再折算——ST.LAYOUTS_PORTRAIT 里的坐标已经是
        # 1080x1920 的像素，再乘一遍比例就是二次缩放（卡片会被压到上半屏）。
        return ST.layout_portrait(name)
    return _fit_layout(ST.LAYOUTS.get(name) or ST.LAYOUTS["left-stack"])


# 版式里 x 类字段（左上角坐标与宽度）按**画布宽比**折算；
# y 类字段（坐标）按**画布高比**折算；高度/行距属于"一个文字行有多高"，跟字号走（宽比）。
_LAY_X = ("title_x", "panel_x")
_LAY_Y = ("title_y", "kicker_y", "sub_y", "panel_y")
_LAY_W = ("title_w", "sub_w", "panel_w", "panel_h", "panel_gap")


def _fit_layout(lay):
    """把版式几何折算到当前画布。横屏时两个比例都是 1.0，**原样返回同一个对象**
    （不是副本）：横屏路径上不能多出任何一次 float 往返，否则 golden_frames 会因为
    浮点误差逐帧漂移——这条是像素级契约，不是洁癖。"""
    if W == 1920 and H == 1080:
        return lay
    sx = W / 1920.0
    sy = H / 1080.0
    out = dict(lay)
    for k in _LAY_X + _LAY_W:
        if out.get(k) is not None:
            try:
                out[k] = float(out[k]) * sx
            except (TypeError, ValueError):
                out[k] = lay.get(k)
    for k in _LAY_Y:
        if out.get(k) is not None:
            try:
                out[k] = float(out[k]) * sy
            except (TypeError, ValueError):
                out[k] = lay.get(k)
    return out


def _typo(lang):
    return lang.get("typography") or _DEFAULT_LANG["typography"]


def _bias(lang):
    """该风格的整体重心偏移 (dx, dy, inset)，单位 px。

    为什么需要它：剪影判据里 Cx/Cy 是"前景质心"，而"同样左对齐、同样字号"的两套浅色主题
    （paper / lo-fi）即使装饰不同，质心也几乎重合——实测这对 ΔCx 只有 2.3pp。
    把"这套风格的画面重心往哪偏"做成数据，是最便宜、也最像"导演习惯"的差异化手段。
    """
    b = (lang or {}).get("bias") or {}
    try:
        return (float(b.get("x") or 0.0), float(b.get("y") or 0.0), float(b.get("inset") or 0.0))
    except (TypeError, ValueError):
        return (0.0, 0.0, 0.0)


def _tracking(th, lang):
    return float(th.get("tracking") or 0.0) + float(_typo(lang).get("tracking") or 0.0)


def _gap(th, lang):
    return float(th.get("line_gap") or _typo(lang).get("line_gap") or 1.3)


# ---------------- 装饰母题（一次烘焙，逐帧只 paste） ----------------

def _motif_key(th, lang):
    """母题图层/底版的缓存键（主题 + 母题 + 密度 + 参与绘制的颜色）。

    单独抽出来是为了让 _motif_layer（RGBA 图层）与 _plate（合成后的底版）用同一把钥匙：
    两者必须一起失效，否则改了主题色会拿到"新图层 + 旧底版"的错配画面。
    """
    mf = lang.get("motif") or _DEFAULT_LANG["motif"]
    return _hashable(("motif", th.get("id"), mf.get("kind"),
                      tuple(sorted((str(k), _hashable(v)) for k, v in (mf.get("detail") or {}).items())),
                      round(float(mf.get("density") or 0.0), 3),
                      th["accent"], th["rule"], th["bg"], th["bg2"]))


def _motif_layer(th, lang):
    """把该风格独有的装饰图形烘焙成一张全屏 RGBA 图层（不含动态）。

    为什么烘焙：这些图形是静态的（括角、页眉线、letterbox、信号条），
    逐帧重画一次就是 3–8ms。注意：全屏 RGBA paste 实测是 **5.6ms**
    （Pillow 13 / 1080p），所以装饰直接烘焙进底版（见 _plate），
    逐帧只 copy 底版，不再单独 paste。
    """
    mf = lang.get("motif") or _DEFAULT_LANG["motif"]
    key = _motif_key(th, lang)
    got = _CACHE.get(key)
    if got is not None:
        return got
    kind = str(mf.get("kind") or "plain")
    det = mf.get("detail") or {}
    dens = float(mf.get("density") or 0.0)
    if kind == "plain":
        # 素净：装饰层就是细线的来源，整层透明（下游 paste 是空操作，不必特判）。
        lay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        _CACHE[key] = lay
        return lay
    lay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(lay, "RGBA")
    accent = th["accent"]
    rule = th["rule"]
    ink = th["ink"]

    def a(v):
        return int(max(0, min(255, round(v))))

    if kind == "brackets":
        # 四角 L 形括角 + 右侧数据刻度尺：科技风的"仪器感"
        c = int(det.get("corner") or 24)
        t = 2
        for (x, y, dx, dy) in ((48, 40, 1, 1), (W - 48, 40, -1, 1),
                               (48, H - 48, 1, -1), (W - 48, H - 48, -1, -1)):
            d.line([(x, y), (x + dx * c, y)], fill=_rgba(accent, a(150 * dens)), width=t)
            d.line([(x, y), (x, y + dy * c)], fill=_rgba(accent, a(150 * dens)), width=t)
        step = int(det.get("rule_step") or 120)
        for i, yy in enumerate(range(150, 860, step)):
            long = (i % 3 == 0)
            d.line([(W - 34, yy), (W - 34 + (18 if long else 10), yy)],
                   fill=_rgba(rule, a(70 * dens)), width=1)
        if det.get("scan"):
            d.rectangle([0, 120, W, 124], fill=_rgba(rule, a(18 * dens)))
    elif kind == "hairlines":
        # 杂志：双横线页眉 + 栏间细线 + 右上页码位
        d.rectangle([64, 112, W - 64, 114], fill=_rgba(rule, a(120 * dens)))
        d.rectangle([64, 118, W - 64, 119], fill=_rgba(rule, a(60 * dens)))
        if det.get("column_rule"):
            d.rectangle([W // 2, 160, W // 2 + 1, 820], fill=_rgba(rule, a(26 * dens)))
        d.rectangle([64, 838, W - 64, 839], fill=_rgba(rule, a(70 * dens)))
    elif kind == "handdrawn":
        # 纸感：不规则内框（vnoise 抖动）+ 四角胶带 + 方印
        wob = float(det.get("wobble") or 3.0)
        # 底边收在 1016：字幕带从 872 开始，外框若画到 1024 会被字幕底板压住一截，
        # 看起来像"框没画完"。
        x0, y0, x1, y1 = 56, 52, W - 56, 1016
        n = 10
        pts = []
        for i in range(n):                       # 上边
            pts.append((x0 + (x1 - x0) * i / (n - 1.0), y0 + wob * (K.vnoise(i * 0.7, 5) - 0.5)))
        for i in range(n):                       # 右边
            pts.append((x1 + wob * (K.vnoise(i * 0.9, 7) - 0.5), y0 + (y1 - y0) * i / (n - 1.0)))
        for i in range(n):                       # 下边
            pts.append((x1 - (x1 - x0) * i / (n - 1.0), y1 + wob * (K.vnoise(i * 0.6, 9) - 0.5)))
        for i in range(n):                       # 左边
            pts.append((x0 + wob * (K.vnoise(i * 0.8, 11) - 0.5), y1 - (y1 - y0) * i / (n - 1.0)))
        d.line(pts + [pts[0]], fill=_rgba(rule, a(120 * dens)), width=2, joint="curve")
        if det.get("tape"):
            for (tx, ty, ww, hh) in ((44, 40, 96, 26), (W - 140, 40, 96, 26)):
                d.polygon([(tx, ty + 4), (tx + ww, ty), (tx + ww, ty + hh), (tx, ty + hh + 4)],
                          fill=_rgba(_mix(ink, th["bg"], 0.72), a(70 * dens)))
        if det.get("seal"):
            sx, sy = W - 150, 128
            d.rectangle([sx, sy, sx + 58, sy + 58], outline=_rgba(th["accent2"], a(150 * dens)),
                        width=3)
    elif kind == "slabs":
        # 撞色：粗色条横贯 + 斜切块 + 圆形贴纸
        bh = int(det.get("bar_h") or 24)
        d.rectangle([0, 118, W, 118 + bh], fill=_rgba(accent, a(200 * dens)))
        sl = int(det.get("slant") or 18)
        d.polygon([(0, 118 + bh), (W, 118 + bh), (W, 118 + bh + sl), (0, 118 + bh + sl + 8)],
                  fill=_rgba(th["accent2"], a(120 * dens)))
        d.rectangle([0, 846, W, 852], fill=_rgba(accent, a(140 * dens)))
        if det.get("sticker"):
            for (cx, cy, r) in ((96, 196, 34), (W - 96, 196, 22)):
                d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=_rgba(accent, a(190 * dens)),
                          width=8)
    elif kind == "minimal":
        # 极简：只有一条 2px 横线（其余全是留白，这就是它的"装饰语言"）
        d.rectangle([W // 2 - 600, 700, W // 2 + 600, 702], fill=_rgba(rule, a(200 * dens)))
        d.rectangle([W // 2 - 120, 742, W // 2 + 120, 743], fill=_rgba(rule, a(90 * dens)))
    elif kind == "letterbox":
        # 电影遮幅：黑边由 _letterbox_overlay 在**缩放之后**贴（画幅不随镜头缩放）。
        # 这里不画任何东西——黑边若烘进底版会跟着 push 一起放大（实测 40px -> 0px）。
        pass
    elif kind == "signal":
        # lo-fi：底部 12 格信号条 + 时间戳位
        bars = int(det.get("bars") or 12)
        bw, gap = 10, 6
        total = bars * bw + (bars - 1) * gap
        x0 = W - 64 - total
        y0 = 806
        for i in range(bars):
            on = i < bars - 2
            d.rectangle([x0 + i * (bw + gap), y0, x0 + i * (bw + gap) + bw, y0 + 14],
                        fill=_rgba(accent if on else rule, a((170 if on else 40) * dens)))
    elif kind == "rays":
        # 史诗：上下双装饰线 + 18 条放射线 + 内细框
        d.rectangle([0, 104, W, 106], fill=_rgba(accent, a(150 * dens)))
        d.rectangle([0, 110, W, 111], fill=_rgba(accent, a(60 * dens)))
        d.rectangle([0, H - 112, W, H - 110], fill=_rgba(accent, a(150 * dens)))
        d.rectangle([0, H - 106, W, H - 105], fill=_rgba(accent, a(60 * dens)))
        cx, cy = W * 0.5, 470.0
        for i in range(int(det.get("rays") or 18)):
            ang = (i / 18.0) * 2.0 * math.pi
            r0, r1 = 250.0, 430.0 + 40.0 * K.hash2(i, 3)
            d.line([(cx + math.cos(ang) * r0, cy + math.sin(ang) * r0 * 0.62),
                    (cx + math.cos(ang) * r1, cy + math.sin(ang) * r1 * 0.62)],
                   fill=_rgba(accent, a(26 * dens)), width=1)
        if det.get("frame"):
            d.rectangle([52, 96, W - 52, 1016], outline=_rgba(accent, a(50 * dens)), width=1)
    _CACHE[key] = lay
    return lay


_PLATE_ORDER = []


def _plate(th, lang, scene, s_bg):
    """背景板 + 装饰母题合成后的整帧底版。

    这是单帧预算里最大的一处优化：装饰是静态的，逐帧单独 paste 一张全屏 RGBA 要 5.6ms，
    合成进底版后逐帧只剩 copy（1.3ms）或端点 blend 取景（约 4.4ms，见 _plate_zoom）。
    顺带一个好处：镜头缓推现在会把装饰一起推（**仅非素净母题**；素净下装饰层整层不画）——装饰本来就该跟着镜头走，
    以前"背景推、装饰不动"在强 push 的风格（cinema 0.12 / epic 0.14）里其实是穿帮。

    缓存：一个片子只用一套主题 + 一套母题，密度档最多 3 档 -> 最多 3 张底版（约 18MB）。
    用 LRU 限 4 张，防多片/多主题同进程时无限增长。
    """
    base = _bg(th)
    if _decor_scale(scene, lang) <= 0.02:
        out = base.copy() if s_bg <= 1.0008 else _bg_plate(th, s_bg)
        bar = _letterbox_overlay(th, lang, scene)   # 遮幅与装饰密度无关：画幅永远在
        if bar is not None:
            out.paste(bar, (0, 0), bar)
        return out
    key = _hashable(("plate", _motif_key(th, lang), _decor_level(scene)))
    got = _CACHE.get(key)
    if got is None:
        lay = _motif_layer(th, lang)
        got = base.copy()
        got.paste(lay, (0, 0), lay)
        _CACHE[key] = got
        _PLATE_ORDER.append(key)
        while len(_PLATE_ORDER) > 4:            # LRU：只留最近 4 张
            old = _PLATE_ORDER.pop(0)
            if old != key:
                _CACHE.pop(old, None)
    out = _plate_zoom(got, s_bg, th, _zoom_tag(th, key))
    # 遮幅（letterbox）的黑边是"画幅"，不随镜头缩放：缩放后再贴一层固定 overlay。
    # 否则 push=0.12 时 40px 黑边会被放大成 0px，画幅直接失效（实测）。
    bar = _letterbox_overlay(th, lang, scene)
    if bar is not None:
        out.paste(bar, (0, 0), bar)
    return out


def _letterbox_overlay(th, lang, scene):
    """letterbox 母题的固定黑边层（缓存）；非 letterbox 返回 None。"""
    mf = (lang or {}).get("motif") or {}
    if str(mf.get("kind") or "") != "letterbox":
        return None
    key = _hashable(("letterbox", _motif_key(th, lang), _decor_level(scene)))
    got = _CACHE.get(key)
    if got is not None:
        return got
    det = mf.get("detail") or {}
    top = int(det.get("top") or 40)
    bot = int(det.get("bottom") or 44)
    dens = float(mf.get("density") or 1.0) * _DECOR_LEVELS[_decor_level(scene)]
    lay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(lay, "RGBA")
    d.rectangle([0, 0, W, top], fill=(0, 0, 0, 255))
    d.rectangle([0, H - bot, W, H], fill=(0, 0, 0, 255))
    d.rectangle([0, top, W, top + 1], fill=_rgba(th["rule"], int(40 * dens)))
    d.rectangle([0, H - bot - 1, W, H - bot], fill=_rgba(th["rule"], int(40 * dens)))
    _CACHE[key] = lay
    return lay


def _apply_motif(img, th, lang, scene):
    """逐帧贴装饰层（0.3ms）。密度为 0 时直接跳过。"""
    if _decor_scale(scene, lang) <= 0.02:
        return img
    lay = _motif_layer(th, lang)
    img.paste(lay, (0, 0), lay)
    return img


# ---------------- 段界转场（veil：只在段首 ≤0.45s 内生效） ----------------

def _apply_veil(img, th, lang, scene, t_local, s_bg):
    """段首从背景"长出来"的过场。不做跨场景 cross-dissolve（draw_frame 拿不到上一段帧），
    只做"从本段背景板渐显 / 擦入"，观感是 dip 而不是 dissolve。

    为什么必须做：相邻两段可能是完全不同的构图（数字卡 → 左右对比），
    整屏瞬切会精确触发 qa_frames 的单帧闪烁门禁（8x8 分块灰度差 ≥6.0），
    观众看着也像"换了一部片"。
    """
    tr = lang.get("transition") or _DEFAULT_LANG["transition"]
    kind = str(tr.get("kind") or "cut")
    dur = float(tr.get("dur") or 0.0)
    if kind == "cut" or dur <= 0.01 or t_local >= dur:
        return img
    p = clamp(t_local / dur)
    base = _plate(th, lang, scene, s_bg)
    if kind in ("dip", "wash"):
        # dip/wash 的差别是"淡向哪一层"：wash 淡向主题底色（浅色主题不出现黑场），
        # dip 也淡向 bg2 —— 刻意不淡向纯黑，否则暗色主题会撞黑帧门禁（平均亮度 <0.045）。
        return Image.blend(base, img, float(p))
    # 擦入类：用背景板盖住"还没露出来"的部分（paste 比 mask 便宜）
    if kind == "wipe":                       # 自左向右
        cut = int(round(W * float(p)))
        if cut < W:
            img.paste(base.crop((cut, 0, W, H)), (cut, 0))
        return img
    if kind == "paper":                      # 自右向左（纸卷）
        cut = int(round(W * (1.0 - float(p))))
        if cut > 0:
            img.paste(base.crop((0, 0, cut, H)), (0, 0))
        return img
    if kind == "iris":                       # 中心向两侧展开
        half = int(round(W * 0.5 * float(p)))
        if half < W // 2:
            img.paste(base.crop((0, 0, W // 2 - half, H)), (0, 0))
            img.paste(base.crop((W // 2 + half, 0, W, H)), (W // 2 + half, 0))
        return img
    return img


# ---------------- 通用：数字（等宽）+ 编号写法 ----------------

_NUM_WEIGHT = "mono"


def _draw_num(img, th, v, text, x, y, size, color, align="center", alpha=1.0):
    """数字用等宽字体：滚动/跳变时字宽不抖（consola 只有 ASCII，中文标签另用 reg）。"""
    _draw_text(img, th, v, text, x, y, size, _NUM_WEIGHT, color, align, alpha)


def _chapter_index(chapter):
    """从 "03 · 画质关键" 里取 3；取不到返回 None（章节卡要用巨型编号）。"""
    s = str(chapter or "")
    for i in range(len(s) - 1):
        if s[i].isdigit() and s[i + 1].isdigit():
            return int(s[i:i + 2])
    for ch in s:
        if ch.isdigit():
            return int(ch)
    return None


_CN_NUM = "零一二三四五六七八九十"


def _number_text(style, n, fallback=""):
    """按视觉语言的编号写法生成编号文本（dim/serif/cn/block/roman/timecode/dot/gilt）。"""
    if n is None:
        return fallback
    if style in ("dim", "serif", "gilt"):
        return "%02d" % n
    if style == "cn":
        return _CN_NUM[n] + "、" if 0 < n < 11 else "%02d" % n
    if style == "block":
        return "%02d" % n
    if style == "roman":
        rom = ("I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X")
        return rom[n - 1] if 0 < n <= 10 else "%02d" % n
    if style == "timecode":
        return "00:%02d" % (n * 4 % 60)
    if style == "dot":
        return "· %02d" % n
    return "%02d" % n


# ---------------- 1) hero 封面 ----------------

def _draw_hero(scene, t, dur, tg, script, frame, plan, th):
    lang = _lang(plan, th)
    s_bg = _push_scale(th, t, dur)
    v = _view(th, s_bg)
    img = _plate(th, lang, scene, s_bg)
    lay = _scene_layout(scene, th)
    var = _variant(scene)
    ease = _ease((lang.get("motion") or {}).get("ease") or th.get("ease") or "out3")
    trk = _tracking(th, lang)
    gap = _gap(th, lang)
    ph = _phase(scene)

    # 重心：variant 决定标题落在左/中/右 —— 这是"换版式"最便宜也最明显的一种；
    # 再叠上该风格的 bias（比如 paper 整体压左下、lo-fi 整体偏右上）。
    bdx, bdy, bins = _bias(lang)
    if lay.get("center") or var == 2:
        ax, align = W * 0.5 + bdx, "center"
    elif var == 1:
        ax, align = float(W - 150) + bdx, "right"
    else:
        ax, align = 150.0 + bdx + bins, "left"


    # 持续动态：背景里缓慢扩散的同心弧（每套风格都在动，但颜色/透明度随主题）
    # 同心弧是细线装饰：素净设计里不画（「默认没有细线」是产品约束，不是审美建议）。
    if not _plain(lang):
        p_ring = smooth(rng(t, 0.15, 2.4))
        d = ImageDraw.Draw(img, "RGBA")
        cx, cy = (ax, 470.0) if align != "center" else (W * 0.5, 430.0)
        for i in range(3):
            rr = (120.0 + 260.0 * i) * (0.4 + 0.6 * p_ring)
            d.arc([v.x(cx - rr), v.y(cy - rr * 0.62), v.x(cx + rr), v.y(cy + rr * 0.62)],
                  start=int(200 + i * 40), end=int(340 + i * 40),
                  fill=_rgba(th["accent"], int(46 * p_ring)), width=2)

    size = int(th["scale"]["display"][0])
    big = scene.get("big") or script.get("title") or ""
    max_w = min(1560.0, float(W - 300))
    lines = _wrap(big, size, max_w, "bold", th)
    while len(lines) > 3 and size > 42:
        size = int(size * 0.88)
        lines = _wrap(big, size, max_w, "bold", th)
    lh = int(size * gap)
    ksize = int(th["scale"]["kicker"][0])
    kicker = scene.get("kicker") or "00 · 开场"
    y = float(lay["title_y"]) + bdy

    # kicker 的纵向位置必须**跟着标题字号走**，不能直接用布局表里的固定 y。
    # 为什么：布局表给的是 left-stack 的 title_y=300 / kicker_y=250，只差 50px；
    # 而 hero 的标题字号是 96px（display 档），字形往上顶穿 kicker —— 实测封面上
    # "00 · 开场"直接压在"价值与代价来自同一个选择"中间。
    # 按字号算间距后，换字体/换字号档（visual 语言包会改 ratio）都不会再压。
    _ky = min(float(lay["kicker_y"]), y - size * 1.05 - ksize * 0.35)
    # 竖屏：标题块整体下移到正文区垂直中线附近。
    # 横屏 hero 的 title_y=300 在 1080 高里是 28%，竖屏沿用同一数字会变成 15%（贴顶），
    # 下面留 1200px 空白——实测出图就是这样。这里按块高（kicker + 标题行 + 副标题）重算。
    if H > W:
        block_h = size * 1.05 + ksize * 0.35 + len(lines) * lh + (len(lines) - 1) * 0
        body_top, body_bot = float(SAFE[1]) + 260.0, float(SUB_BAND[1]) - 220.0
        y = (body_top + body_bot - block_h) * 0.5 + block_h - len(lines) * lh
        y = max(y, float(SAFE[1]) + 200.0)
        _ky = y - size * 1.05 - ksize * 0.35
    p0 = ease(rng(t, 0.05, 0.85))
    _draw_text(img, th, v, kicker, ax + _entrance_x(th, p0, align),
               _ky + _entrance_y(th, p0), ksize, "bold",
               th["accent"], align, p0, trk * 0.5)

    for i, ln in enumerate(lines):
        p = ease(rng(t, 0.12 + 0.13 * i, 0.88 + 0.13 * i))
        _draw_text(img, th, v, ln, ax + _entrance_x(th, p, align),
                   y + i * lh + _entrance_y(th, p), size, "bold",
                   th["ink"], align, p, trk)

    sub = scene.get("lead") or scene.get("sub") or ""
    # 副标题与大字重复时不要画第二遍：模板片里 big 与 sub 都是"零素材，全代码生成"，
    # 照抄会让画面出现两行一样的字（实测踩到过）。
    if sub and (str(sub).strip() == str(big).strip()
                or (len(str(sub)) >= 6 and str(sub).strip() in str(big).strip())):
        sub = ""
    sub_bottom = y + len(lines) * lh
    if sub:
        ssize = int(th["scale"]["sub"][0])
        sy = sub_bottom + int(size * 0.24)
        p2 = ease(rng(t, 0.30, 1.40))
        for j, ln in enumerate(_wrap(sub, ssize, float(lay["sub_w"]), "light", th)[:3]):
            _draw_text(img, th, v, ln, ax + _entrance_x(th, p2, align),
                       sy + j * int(ssize * gap), ssize, "light", th["muted"], align, p2)
        sub_bottom = sy + int(ssize * gap)

    # 持续动态：标题下呼吸强调线（相位随段偏移，避免每段同频"复印"）
    hz = float((lang.get("motion") or {}).get("breathe_hz") or 0.85)
    amp = 0.45 + 0.55 * (0.5 + 0.5 * math.sin(hz * t + ph))
    line_w = int(min(float(lay["title_w"]), 760) * 0.55 * amp)
    line_y = sub_bottom + int(size * 0.42)
    if line_y < _BODY_LIMIT:
        lx = ax if align == "left" else (ax - line_w if align == "right" else ax - line_w // 2)
        ImageDraw.Draw(img, "RGBA").line(
            [v.x(lx), v.y(line_y), v.x(lx + line_w), v.y(line_y)],
            fill=_rgba(th["accent"], 190), width=_lw(3, lang))

    img = _apply_veil(img, th, lang, scene, t, s_bg)
    return _hud(img, th, scene.get("chapter") or "01 · 开场", _progress(tg, script),
                script.get("title", ""), lang)


# ---------------- 2) statement 金句（居中大字，无卡片） ----------------

def _draw_statement(scene, t, dur, tg, script, frame, plan, th):
    lang = _lang(plan, th)
    s_bg = _push_scale(th, t, dur)
    v = _view(th, s_bg)
    img = _plate(th, lang, scene, s_bg)
    var = _variant(scene)
    ease = _ease((lang.get("motion") or {}).get("ease") or "out3")
    trk = _tracking(th, lang)
    gap = _gap(th, lang)
    ph = _phase(scene)

    big = str(scene.get("big") or scene.get("text") or "")[:24]
    size = int(th["scale"]["display"][0] * 1.10)
    max_w = 1400.0 if var != 1 else 1180.0
    lines = _wrap(big, size, max_w, "bold", th)
    while len(lines) > 2 and size > 52:
        size = int(size * 0.90)
        lines = _wrap(big, size, max_w, "bold", th)
    lh = int(size * gap)
    if H > W:
        # 竖屏：正文可视区是 SAFE[1]..SUB_BAND[1]（120..1450），把大字块放到它的垂直中线。
        # 横屏的 470 在 1080 高里是 43%，同一比例在 1920 高里也是 43% —— 但竖屏下方还要
        # 落副标题与下划线，固定 470 会让整块悬在画面上三分之一（实测出图就是这样）。
        body_top, body_bot = float(SAFE[1]) + 120.0, float(SUB_BAND[1]) - 60.0
        top = (body_top + body_bot - len(lines) * lh) * 0.5
    else:
        top = 470.0 - (len(lines) - 1) * lh * 0.5
    if var == 1:                                  # variant 1：整体上移，留出落款区
        top -= 40.0

    # 逐字显现：4 字一组（单字一次 text 太贵；整行一次又没"写出来"的感觉）
    reveal = clamp(t / max(0.35, min(1.6, dur * 0.45)))
    total_chars = sum(len(x) for x in lines)
    shown = int(total_chars * ease(reveal))
    walked = 0
    for i, ln in enumerate(lines):
        if walked >= shown:
            break
        take = max(0, min(len(ln), shown - walked))
        walked += len(ln)
        seg = ln[:take]
        if not seg:
            continue
        w = _text_w(seg, size, "bold", th)
        x0 = (W - w) * 0.5
        _draw_text(img, th, v, seg, x0, top + i * lh, size, "bold", th["ink"],
                   "left", 1.0, trk)

    # 持续动态：字距呼吸 + 下划线从左生长（保证"3–6 秒内有可见变化"）
    hz = float((lang.get("motion") or {}).get("breathe_hz") or 0.85)
    bre = 0.5 + 0.5 * math.sin(hz * 0.9 * t + ph)
    ul_w = int(280 * (0.7 + 0.3 * bre))
    ul_y = top + len(lines) * lh + int(size * 0.22)
    if ul_y < _BODY_LIMIT:
        d = ImageDraw.Draw(img, "RGBA")
        x0 = W * 0.5 - ul_w * 0.5
        d.line([v.x(x0), v.y(ul_y), v.x(x0 + ul_w), v.y(ul_y)],
               fill=_rgba(th["accent"], 170), width=_lw(3, lang))

    sub = scene.get("sub") or ""
    if sub and (str(sub).strip() == str(big).strip()
                or (len(str(sub)) >= 6 and str(sub).strip() in str(big).strip())):
        sub = ""                       # 与大字重复的落款不画（理由同 hero）
    if sub and var != 2:
        ssize = int(th["scale"]["sub"][0])
        p2 = ease(rng(t, 0.55, 1.55))
        for j, ln in enumerate(_wrap(sub, ssize, 1100, "light", th)[:2]):
            _draw_text(img, th, v, ln, W * 0.5,
                       top + len(lines) * lh + int(size * 0.55) + j * int(ssize * gap),
                       ssize, "light", th["muted"], "center", p2)

    img = _apply_veil(img, th, lang, scene, t, s_bg)
    return _hud(img, th, scene.get("chapter") or "02 · 正文", _progress(tg, script),
                script.get("title", ""), lang)


# ---------------- 3) list 要点（左标题 + 右条目 / 全宽序号列） ----------------

def _draw_list(scene, t, dur, tg, script, frame, plan, th):
    lang = _lang(plan, th)
    s_bg = _push_scale(th, t, dur)
    v = _view(th, s_bg)
    img = _plate(th, lang, scene, s_bg)
    lay = _scene_layout(scene, th)
    var = _variant(scene)
    ease = _ease((lang.get("motion") or {}).get("ease") or "out3")
    trk = _tracking(th, lang)
    gap = _gap(th, lang)
    ph = _phase(scene)

    items = list(scene.get("items") or [])
    stagger = float((plan.get("pacing") or {}).get("item_stagger") or 0.32)
    nstyle = str(_typo(lang).get("number") or "dim")

    # variant 0/2：左标题 + 右条目列；variant 1：全宽（标题在上，序号列在左）
    full = (var == 1) or lay.get("center")
    bdx, bdy, bins = _bias(lang)
    if full:
        ax, align = 150.0 + bdx + bins, "left"
        title_w = 1620.0
        panel_x, panel_w = 150.0 + bdx + bins, 1620.0
        panel_y = 400.0 + bdy
    else:
        ax, align = _title_anchor(lay)
        ax += bdx + (bins if align == "left" else 0.0)
        title_w = float(lay["title_w"])
        panel_x, panel_w = _panel_x(lay) + bdx, float(lay["panel_w"])
        panel_y = max(float(lay["panel_y"]) + bdy, 210.0)

    kicker = scene.get("kicker") or ""
    p0 = ease(rng(t, 0.0, 0.75))
    if kicker:
        _draw_text(img, th, v, kicker, ax + _entrance_x(th, p0, align),
                   float(lay["kicker_y"]) + _entrance_y(th, p0),
                   int(th["scale"]["kicker"][0]), "bold", th["accent"], align, p0, trk * 0.5)

    big = scene.get("big") or (scene.get("text") or "")[:18]
    bsize = int(th["scale"]["heading"][0])
    lines = _wrap(big, bsize, title_w, "bold", th)
    while len(lines) > 3 and bsize > 40:
        bsize = int(bsize * 0.9)
        lines = _wrap(big, bsize, title_w, "bold", th)
    lh = int(bsize * gap)
    p1 = ease(rng(t, 0.06, 0.80))
    y0 = float(lay["title_y"]) + bdy + _entrance_y(th, p1)
    for i, ln in enumerate(lines):
        _draw_text(img, th, v, ln, ax + _entrance_x(th, p1, align),
                   y0 + i * lh, bsize, "bold", th["ink"], align, p1, trk)
    sub_bottom = y0 + len(lines) * lh

    sub = scene.get("sub") or ""
    if sub:
        ssize = int(th["scale"]["body"][0])
        sy = max(sub_bottom + int(bsize * 0.30),
                 float(lay["sub_y"]) if not lay.get("center") else 0.0)
        p2 = ease(rng(t, 0.20, 0.95))
        for j, ln in enumerate(_wrap(sub, ssize, title_w, "reg", th)[:2]):
            _draw_text(img, th, v, ln, ax + _entrance_x(th, p2, align),
                       sy + j * int(ssize * gap), ssize, "reg", th["muted"], align, p2)
        sub_bottom = sy + int(ssize * gap)

    if not items:
        img = _apply_veil(img, th, lang, scene, t, s_bg)
        return _hud(img, th, scene.get("chapter") or "02 · 正文", _progress(tg, script),
                script.get("title", ""), lang)

    # 条目几何：全宽模式一行一条；双栏模式按行距排
    top = max(panel_y, sub_bottom + int(bsize * 0.45)) if full else panel_y
    top = max(top, 200.0)
    body = max(22, int(th["scale"]["body"][0]) - 1)
    # 行高：优先"每条 96–118px"，条目多时往下压，硬下限 56px（再低中文就糊成一片）。
    # 为什么不是固定行高：模板片 5 条要点在 9.3s 的段里，固定 118px 会溢出 _BODY_LIMIT 硬边界，
    # 于是第 5 条被静默截断（实测踩到过），观众看到"五步"只列出四步。
    avail = float(_BODY_LIMIT) - top
    row_h = avail / max(1, len(items)) if items else 118.0
    row_h = min(118.0, max(56.0, row_h - 6.0))
    shown = int(avail // row_h)
    if shown < len(items):
        _warn_once(("list", scene.get("id"), len(items)),
                   "本段 %d 个条目在版式里只放得下 %d 个（其余已省略）；请拆段或减条目"
                   % (len(items), max(0, shown)))
    for i, it in enumerate(items[:max(0, shown)]):
        p = ease(rng(t, 0.42 + stagger * i, 1.12 + stagger * i))
        if p <= 0.0:
            continue
        txt = str(it.get("text") if isinstance(it, dict) else it)
        ok = bool(it.get("ok", True)) if isinstance(it, dict) else True
        yy = top + i * row_h
        alpha = clamp(p)
        # 焦点行：每 2.2s 换一条高亮（左侧竖条 + 行底微亮），保证画面不静止
        focus = int((t / 2.2) + ph) % max(1, min(len(items), 6))
        is_focus = (i == focus)
        d = ImageDraw.Draw(img, "RGBA")
        if is_focus:
            d.rectangle([v.x(panel_x - 26), v.y(yy + 8), v.x(panel_x - 20), v.y(yy + row_h - 18)],
                        fill=_rgba(th["accent"], int(190 * alpha)))
            if not _plain(lang):
                d.rectangle([v.x(panel_x - 14), v.y(yy + row_h - 14), v.x(panel_x + panel_w),
                             v.y(yy + row_h - 12)],
                            fill=_rgba(th["rule"], int(26 * alpha)))
        col = th["good"] if ok else th["bad"]
        img = _soft_dot(img, v.x(panel_x + 12), v.y(yy + row_h * 0.5),
                        7.0 + 1.4 * math.sin(2.0 * t + i + ph), col, 0.95 * alpha)
        d = ImageDraw.Draw(img, "RGBA")
        num = _number_text(nstyle, i + 1)
        nsize = int(body * 0.78)
        nx = panel_x + 34
        # 编号字体：纯数字/符号走等宽（滚动不抖），带中文的写法（一、二、三）退回 reg
        # —— consola 没有中文字形，用 mono 画"一、"会得到两个方框。
        nweight = "reg" if any("\u4e00" <= ch <= "\u9fff" for ch in num) else _NUM_WEIGHT
        _draw_text(img, th, v, num, nx, yy + (row_h - nsize * 1.25) * 0.5,
                   nsize, nweight, th["dim"], "left", alpha * 0.9)
        tx = nx + max(70.0, _text_w(num, nsize, nweight, th) + 26.0)
        max_w = panel_x + panel_w - tx
        tsize = _fit_size(txt, max_w, body, "reg", th, 18)
        _draw_text(img, th, v, txt, tx, yy + (row_h - tsize * 1.2) * 0.5,
                   tsize, "reg", th["ink"], "left", alpha)

    img = _apply_veil(img, th, lang, scene, t, s_bg)
    return _hud(img, th, scene.get("chapter") or "02 · 正文", _progress(tg, script),
                script.get("title", ""), lang)


# ---------------- 4) stat 数字卡（巨数字 count-up） ----------------

def _draw_stat(scene, t, dur, tg, script, frame, plan, th):
    lang = _lang(plan, th)
    s_bg = _push_scale(th, t, dur)
    v = _view(th, s_bg)
    img = _plate(th, lang, scene, s_bg)
    var = _variant(scene)
    ease = _ease((lang.get("motion") or {}).get("ease") or "out3")
    ph = _phase(scene)

    stats = scene.get("_stats")
    if not stats:
        try:
            import grammar as _G
        except Exception:                                # noqa: BLE001
            _G = None
        stats = (_G.derive_stats(scene) if _G is not None else []) or []
    if not stats:
        return _draw_statement(scene, t, dur, tg, script, frame, plan, th)

    if H > W:
        # 竖屏：横向排布在 1080 宽里放不下四层信息，改**纵向分层**。
        return _draw_stat_portrait(scene, t, dur, tg, script, frame, plan, th, stats)

    main = stats[0]
    raw = str(main.get("value") if main.get("value") is not None else "")
    unit = str(main.get("unit") or "")
    label = str(main.get("label") or "")
    notes = [s for s in stats[1:3] if str(s.get("label") or "")]

    # count-up：0 -> target（整数位，小数位保留原样）；跳变 ≤2 位/帧，避免闪烁门禁
    target = None
    try:
        target = float(raw)
    except (TypeError, ValueError):
        target = None
    p = ease(clamp(t / max(0.5, min(1.2, dur * 0.42))))
    if target is None:
        shown_txt = raw
    elif "." in raw:
        dec = len(raw.split(".")[1])
        shown_txt = ("%." + str(dec) + "f") % (target * p)
    else:
        shown_txt = "%d" % int(round(target * p))

    size = int(240 * (1.0 if var != 1 else 0.86))
    max_w = 1500.0
    while size > 90 and _text_w(shown_txt + unit, size, _NUM_WEIGHT, th) > max_w:
        size = int(size * 0.9)
    cx = W * 0.5 if var != 1 else W * 0.5 - 160.0
    cy = 400.0
    total_w = _text_w(shown_txt, size, _NUM_WEIGHT, th) + _text_w(unit, int(size * 0.42), "reg", th)
    x0 = cx - total_w * 0.5
    _draw_num(img, th, v, shown_txt, x0, cy - size * 0.62, size, th["accent"], "left",
              0.25 + 0.75 * p)
    if unit:
        pu = ease(rng(t, 0.35, 0.95))
        _draw_text(img, th, v, unit, x0 + _text_w(shown_txt, size, _NUM_WEIGHT, th) + 10,
                   cy + size * 0.22, int(size * 0.42), "reg", th["muted"], "left", pu)

    if label:
        pl = ease(rng(t, 0.45, 1.10))
        for j, ln in enumerate(_wrap(label, int(th["scale"]["sub"][0]), 1200, "light", th)[:2]):
            _draw_text(img, th, v, ln, W * 0.5,
                       660.0 + j * int(th["scale"]["sub"][0] * 1.3),
                       int(th["scale"]["sub"][0]), "light", th["ink"], "center", pl)

    # 持续动态：底部 12 格刻度逐格点亮（0.55s/格，循环）
    bars = 12
    bw, gapb = 46, 14
    tot = bars * bw + (bars - 1) * gapb
    bx = W * 0.5 - tot * 0.5
    lit = int((t / 0.55 + ph) % bars)
    d = ImageDraw.Draw(img, "RGBA")
    for i in range(bars):
        on = i <= lit
        d.rectangle([v.x(bx + i * (bw + gapb)), v.y(760), v.x(bx + i * (bw + gapb) + bw),
                     v.y(768)], fill=_rgba(th["accent"] if on else th["rule"],
                                           int((150 if on else 34))))
    # 次要统计：**数值 + 单位 + 标签**都要画出来。
    # 这里必须把数值也画出来：只画 s["label"] 时 stats[] 里的第 2、3 项（例如
    # "338207 次 / 工具调用"、"22.62 B / Token 重放"）在画面上只剩两个光秃秃的词，
    # 并且行距 30px < 字号，两行直接叠在一起 —— 观众看到的是"工具调用/Token 重放"两行字
    # 糊在一块、数字完全消失。数字卡的价值就在数字，丢数字等于这段白做。
    nsize = int(th["scale"]["body"][0] - 6)
    step = int(nsize * 1.75)          # 行距必须大于字号，否则相邻两行会压在一起
    for j, s in enumerate(notes):
        pn = ease(rng(t, 0.6 + 0.15 * j, 1.3 + 0.15 * j))
        val = str(s.get("value") if s.get("value") is not None else "").strip()
        unit = str(s.get("unit") or "").strip()
        lbl = str(s.get("label") or "").strip()
        head = val + ((" " + unit) if unit else "")
        txt = (head + "  " + lbl) if head else lbl
        if not txt:
            continue
        _draw_text(img, th, v, txt, W * 0.5, 796.0 + j * step, nsize,
                   "reg", th["muted"], "center", pn)

    img = _apply_veil(img, th, lang, scene, t, s_bg)
    return _hud(img, th, scene.get("chapter") or "02 · 正文", _progress(tg, script),
                script.get("title", ""), lang)


# ---------------- 5) compare 左右对比（中缝 + 双栏） ----------------

def _draw_compare(scene, t, dur, tg, script, frame, plan, th):
    lang = _lang(plan, th)
    s_bg = _push_scale(th, t, dur)
    v = _view(th, s_bg)
    img = _plate(th, lang, scene, s_bg)
    ease = _ease((lang.get("motion") or {}).get("ease") or "out3")
    trk = _tracking(th, lang)
    ph = _phase(scene)

    sides = scene.get("_sides")
    if not sides:
        try:
            import grammar as _G
        except Exception:                                # noqa: BLE001
            _G = None
        sides = (_G.derive_sides(scene) if _G is not None else None)
    if not sides:
        return _draw_statement(scene, t, dur, tg, script, frame, plan, th)
    if H > W:
        # 竖屏：左右对照在 1080 宽里只剩 500px 一栏，字会被压到读不了——改**上下对照**。
        return _draw_compare_portrait(scene, t, dur, tg, script, frame, plan, th, sides)

    big = str(scene.get("big") or "")
    if big:
        pb = ease(rng(t, 0.0, 0.7))
        for _j, ln in enumerate(_wrap(big, int(th["scale"]["heading"][0] - 6), 1500, "bold", th)[:1]):
            _draw_text(img, th, v, ln, W * 0.5, 150.0 + _entrance_y(th, pb),
                       int(th["scale"]["heading"][0] - 6), "bold", th["ink"], "center", pb, trk)

    # 中缝：从中心向上下生长（0.35s），是"对比"这件事最直接的视觉符号
    pg = ease(rng(t, 0.05, 0.40))
    mid_x = W * 0.5
    y_top, y_bot = 240.0, 820.0
    cy = (y_top + y_bot) * 0.5
    half = (y_bot - y_top) * 0.5 * pg
    d = ImageDraw.Draw(img, "RGBA")
    d.line([v.x(mid_x), v.y(cy - half), v.x(mid_x), v.y(cy + half)],
           fill=_rgba(th["rule"], 90), width=_lw(2, lang))
    # 扫描高光是一条横细线：素净设计里不画（两栏交替高亮已经是持续动态了）。
    scan = cy - half + 2.0 * half * (0.5 + 0.5 * math.sin(0.6 * t + ph))
    if not _plain(lang) and abs(scan - cy) <= half:
        d.line([v.x(mid_x - 90), v.y(scan), v.x(mid_x + 90), v.y(scan)],
               fill=_rgba(th["accent"], 40), width=2)

    # 两栏内容垂直居中（上界 360，下界 800）：只有一两条时不会全挤在顶部留一大片空白。
    # 栏头：优先 sides 里作者写的 head；没有就**不要**写"左/右"这种零信息标签
    # （实测：观众看到"左 / 右"会以为画面坏了），改画一个 VS 徽标在中缝上方。
    cols = [("left", 150.0, 700.0, -1), ("right", W - 850.0, 700.0, 1)]
    max_rows = max(len((sides.get("left") or {}).get("items") or []),
                   len((sides.get("right") or {}).get("items") or []), 1)
    rows_h = min(3, max_rows) * 130
    y_start = max(380.0, 620.0 - rows_h * 0.5)
    heads = [str((sides.get(s) or {}).get("head") or "") for s, _, _, _ in cols]
    has_head = any(heads)
    for side, x0, ww, sgn in cols:
        blk = sides.get(side) or {}
        head = str(blk.get("head") or "")
        its = [str(x) for x in (blk.get("items") or [])]
        ph2 = ease(rng(t, 0.10, 0.70))
        if head:
            hx = x0 + 40 * (1 - ph2) * sgn
            _draw_text(img, th, v, head, hx, y_start - 92.0, int(th["scale"]["body"][0]),
                       "bold", th["accent"], "left", ph2)
            ImageDraw.Draw(img, "RGBA").line(
                [v.x(x0), v.y(y_start - 48.0), v.x(x0 + ww * ph2), v.y(y_start - 48.0)],
                fill=_rgba(th["accent"], 130), width=_lw(2, lang))
        for i, txt in enumerate(its[:3]):
            pi = ease(rng(t, 0.42 + 0.22 * i, 1.05 + 0.22 * i))
            if pi <= 0:
                continue
            yy = y_start + i * 130
            # 反相高亮：左右两栏轮流亮（周期 3.2s），让"对照"这件事一直在动
            hi = ((math.sin(2 * math.pi * t / 3.2 + ph + (0 if side == "left" else math.pi)) + 1) / 2)
            alpha = 0.55 + 0.45 * hi
            tsize = _fit_size(txt, ww - 40, int(th["scale"]["body"][0] - 2), "reg", th, 18)
            _draw_text(img, th, v, txt, x0 + 40 * (1 - pi) * sgn, yy, tsize, "reg",
                       th["ink"], "left", pi * alpha)
            img = _soft_dot(img, v.x(x0 - 8), v.y(yy + tsize * 0.6), 6.0,
                            th["accent"], 0.7 * pi)

    if not has_head:
        # 中缝 VS 徽标：对比这件事的"视觉锚点"，也让画面重心落在中轴上
        pv = ease(rng(t, 0.2, 0.8))
        vs_size = int(th["scale"]["body"][0] - 6)
        vw = _text_w("VS", vs_size, "bold", th)
        cyv = 250.0
        ImageDraw.Draw(img, "RGBA").rectangle(
            [v.x(mid_x - vw * 0.5 - 22), v.y(cyv - 8), v.x(mid_x + vw * 0.5 + 22), v.y(cyv + 44)],
            fill=_rgba(th["surface"], int(220 * pv)),
            outline=None if _plain(lang) else _rgba(th["accent"], int(180 * pv)))
        _draw_text(img, th, v, "VS", mid_x - vw * 0.5, cyv, vs_size, "bold",
                   th["accent"], "left", pv)

    img = _apply_veil(img, th, lang, scene, t, s_bg)
    return _hud(img, th, scene.get("chapter") or "02 · 正文", _progress(tg, script),
                script.get("title", ""), lang)


def _draw_compare_portrait(scene, t, dur, tg, script, frame, plan, th, sides):
    """竖屏对照：**上下两栏** + 横中缝（横屏版本是左右两栏 + 竖中缝）。

    为什么必须换方向而不是缩一缩：1080 宽减掉左右留白，一栏只剩 ~460px；
    中文正文一行放得下 7~8 字，三条要点要折 2~3 行，读起来是断的。竖屏的天然优势是纵向，
    上下两栏每栏能有 920px 宽、一整屏高的一半，字不用挤。

    结构对齐横屏版本：栏头（作者写了就画）+ 最多 3 条 + 反相高亮 + 无栏头时画 VS 徽标。
    """
    lang = _lang(plan, th)
    s_bg = _push_scale(th, t, dur)
    v = _view(th, s_bg)
    img = _plate(th, lang, scene, s_bg)
    ease = _ease((lang.get("motion") or {}).get("ease") or "out3")
    trk = _tracking(th, lang)
    ph = _phase(scene)

    big = str(scene.get("big") or "")
    pb = ease(rng(t, 0.0, 0.7))
    if big:
        for _j, ln in enumerate(_wrap(big, int(th["scale"]["heading"][0] - 10), 900, "bold", th)[:1]):
            _draw_text(img, th, v, ln, W * 0.5, 210.0 + _entrance_y(th, pb),
                       int(th["scale"]["heading"][0] - 10), "bold", th["ink"], "center", pb, trk)

    # 两个栏位的几何：上栏 / 下栏，各自一条横带。
    x0, ww = 80.0, float(W - 160)
    top_y0, bot_y0 = 470.0, 880.0
    row_pitch = 86.0
    max_rows = max(len((sides.get("left") or {}).get("items") or []),
                   len((sides.get("right") or {}).get("items") or []), 1)
    rows_h = min(3, max_rows) * row_pitch

    # 横中缝：从上栏底部向下栏顶部生长（0.35s），是「对照」最直接的符号。
    pg = ease(rng(t, 0.05, 0.40))
    mid_y = (top_y0 + rows_h + bot_y0) * 0.5 - 20.0
    d = ImageDraw.Draw(img, "RGBA")
    d.line([v.x(x0 + 60), v.y(mid_y), v.x(x0 + 60 + (ww - 120) * pg), v.y(mid_y)],
           fill=_rgba(th["rule"], 90), width=_lw(2, lang))

    blocks = (("left", top_y0, -1), ("right", bot_y0, 1))
    heads = [str((sides.get(s) or {}).get("head") or "") for s, _, _ in blocks]
    has_head = any(heads)
    for side, dy, sgn in blocks:
        blk = sides.get(side) or {}
        head = str(blk.get("head") or "")
        its = [str(x) for x in (blk.get("items") or [])]
        ph2 = ease(rng(t, 0.10, 0.70))
        if head:
            _draw_text(img, th, v, head, x0, dy - 74.0 + 18 * (1 - ph2) * sgn,
                       int(th["scale"]["body"][0]), "bold", th["accent"], "left", ph2)
            ImageDraw.Draw(img, "RGBA").line(
                [v.x(x0), v.y(dy - 34.0), v.x(x0 + 320 * ph2), v.y(dy - 34.0)],
                fill=_rgba(th["accent"], 130), width=_lw(2, lang))
        for i, txt in enumerate(its[:3]):
            pi = ease(rng(t, 0.42 + 0.22 * i, 1.05 + 0.22 * i))
            if pi <= 0:
                continue
            yy = dy + i * row_pitch
            # 反相高亮：上下两栏轮流亮（周期 3.2s），让「对照」一直在动
            hi = ((math.sin(2 * math.pi * t / 3.2 + ph + (0 if side == "left" else math.pi)) + 1) / 2)
            alpha = 0.55 + 0.45 * hi
            tsize = _fit_size(txt, ww - 90, int(th["scale"]["body"][0] - 2), "reg", th, 18)
            _draw_text(img, th, v, txt, x0 + 46, yy, tsize, "reg", th["ink"], "left", pi * alpha)
            img = _soft_dot(img, v.x(x0 + 16), v.y(yy + tsize * 0.6), 6.0,
                            th["accent"], 0.7 * pi)

    if not has_head:
        # 没有栏头时画 VS 徽标：放在横中缝左侧，充当两栏的「对撞点」。
        pv = ease(rng(t, 0.2, 0.8))
        vs_size = int(th["scale"]["body"][0] - 6)
        vw = _text_w("VS", vs_size, "bold", th)
        ImageDraw.Draw(img, "RGBA").rectangle(
            [v.x(x0 - 6), v.y(mid_y - vw * 0.58), v.x(x0 + 56), v.y(mid_y + vw * 0.58)],
            fill=_rgba(th["surface"], int(220 * pv)),
            outline=None if _plain(lang) else _rgba(th["accent"], int(180 * pv)))
        _draw_text(img, th, v, "VS", x0 + 8, mid_y - vs_size * 0.62, vs_size, "bold",
                   th["accent"], "left", pv)

    img = _apply_veil(img, th, lang, scene, t, s_bg)
    return _hud(img, th, scene.get("chapter") or "02 · 正文", _progress(tg, script),
                script.get("title", ""), lang)


# ---------------- 6) process 流程轴 ----------------

def _draw_process(scene, t, dur, tg, script, frame, plan, th):
    lang = _lang(plan, th)
    s_bg = _push_scale(th, t, dur)
    v = _view(th, s_bg)
    img = _plate(th, lang, scene, s_bg)
    ease = _ease((lang.get("motion") or {}).get("ease") or "out3")
    trk = _tracking(th, lang)
    # 这里**不**需要段相位：流程轴的持续运动来自 t/dur 的进度光点（见下面
    # "持续动态"那段），不需要再叠一层呼吸。其余构图多数用 ph 做呼吸/扫描，
    # 所以别照抄它们的开头——抄过来只会得到一个没人读的变量。

    steps = scene.get("_steps")
    if not steps:
        try:
            import grammar as _G
        except Exception:                                # noqa: BLE001
            _G = None
        steps = (_G.derive_steps(scene) if _G is not None else []) or []
    if len(steps) < 2:
        return _draw_list(scene, t, dur, tg, script, frame, plan, th)
    if H > W:
        # 竖屏：横向流程轴在 1080 宽里每个节点只有 ~200px，标签必然重叠——改**竖向流程轴**。
        return _draw_process_portrait(scene, t, dur, tg, script, frame, plan, th, steps)

    big = str(scene.get("big") or "")
    pb = ease(rng(t, 0.0, 0.7))
    if big:
        for _j, ln in enumerate(_wrap(big, int(th["scale"]["heading"][0] - 6), 1500, "bold", th)[:1]):
            _draw_text(img, th, v, ln, 150.0, 210.0 + _entrance_y(th, pb),
                       int(th["scale"]["heading"][0] - 6), "bold", th["ink"], "left", pb, trk)

    n = min(5, len(steps))
    x0, x1, ay = 240.0, float(W - 240), 560.0
    gapx = (x1 - x0) / max(1, n - 1)
    pg = ease(rng(t, 0.10, 0.90))
    d = ImageDraw.Draw(img, "RGBA")
    d.line([v.x(x0), v.y(ay), v.x(x0 + (x1 - x0) * pg), v.y(ay)],
           fill=_rgba(th["rule"], 70), width=_lw(2, lang))
    for i in range(n):
        st = steps[i]
        pn = ease(rng(t, 0.35 + 0.18 * i, 1.05 + 0.18 * i))
        if pn <= 0:
            continue
        cx = x0 + gapx * i
        r = 14.0 * pn
        if _plain(lang):
            # 素净下不画 3px 环（厚度 < 6 的描边都算细线），改实心圆点。
            d.ellipse([v.x(cx - r * 0.8), v.y(ay - r * 0.8),
                       v.x(cx + r * 0.8), v.y(ay + r * 0.8)],
                      fill=_rgba(th["accent"], int(230 * pn)))
        else:
            d.ellipse([v.x(cx - r), v.y(ay - r), v.x(cx + r), v.y(ay + r)],
                      outline=_rgba(th["accent"], int(230 * pn)), width=3)
        d.ellipse([v.x(cx - 4), v.y(ay - 4), v.x(cx + 4), v.y(ay + 4)],
                  fill=_rgba(th["accent"], int(200 * pn)))
        lbl = str(st.get("label") or "")
        lsize = int(th["scale"]["body"][0] - 4)
        lw = _text_w(lbl, lsize, "bold", th)
        _draw_text(img, th, v, lbl, cx - lw * 0.5, 470.0, lsize, "bold",
                   th["ink"], "left", pn)
        note = str(st.get("note") or "")
        if note:
            for j, ln in enumerate(_wrap(note, int(lsize * 0.82), 300, "reg", th)[:2]):
                nw = _text_w(ln, int(lsize * 0.82), "reg", th)
                _draw_text(img, th, v, ln, cx - nw * 0.5, 610.0 + j * int(lsize * 1.05),
                           int(lsize * 0.82), "reg", th["muted"], "left", pn * 0.95)
        num = _number_text(str(_typo(lang).get("number") or "dim"), i + 1)
        nw = "reg" if any("\u4e00" <= ch <= "\u9fff" for ch in num) else _NUM_WEIGHT
        _draw_text(img, th, v, num, cx - 26, ay - 66, int(lsize * 0.8), nw,
                   th["dim"], "left", pn * 0.9)

    # 持续动态：进度光点沿轴匀速（周期 = 段长），永远有运动
    if dur > 0.1:
        u = clamp((t / dur))
        px = x0 + (x1 - x0) * u
        img = _soft_dot(img, v.x(px), v.y(ay), 9.0, th["accent2"], 0.85)

    img = _apply_veil(img, th, lang, scene, t, s_bg)
    return _hud(img, th, scene.get("chapter") or "02 · 正文", _progress(tg, script),
                script.get("title", ""), lang)


def _draw_process_portrait(scene, t, dur, tg, script, frame, plan, th, steps):
    """竖屏流程轴：**竖向**（横屏版本是横向）。

    为什么必须换方向：横向轴在 1080 宽里减去两端留白只剩 600px，5 个节点每个 150px，
    而标签 4 个中文字就要 130px+，再加编号与注释必然重叠。竖屏纵向有 1400px 可用，
    每个节点一整行，标签、注释、编号都能并排摆开——这也是竖屏原生视频最常见的流程排版。
    """
    lang = _lang(plan, th)
    s_bg = _push_scale(th, t, dur)
    v = _view(th, s_bg)
    img = _plate(th, lang, scene, s_bg)
    ease = _ease((lang.get("motion") or {}).get("ease") or "out3")
    trk = _tracking(th, lang)

    big = str(scene.get("big") or "")
    pb = ease(rng(t, 0.0, 0.7))
    if big:
        for _j, ln in enumerate(_wrap(big, int(th["scale"]["heading"][0] - 8), 900, "bold", th)[:1]):
            _draw_text(img, th, v, ln, 80.0, 230.0 + _entrance_y(th, pb),
                       int(th["scale"]["heading"][0] - 8), "bold", th["ink"], "left", pb, trk)

    n = min(5, len(steps))
    ax = 150.0                       # 竖轴 x
    y0, y1 = 480.0, 1420.0
    gapy = (y1 - y0) / max(1, n - 1)
    lsize = int(th["scale"]["body"][0] - 4)
    pg = ease(rng(t, 0.10, 0.90))
    d = ImageDraw.Draw(img, "RGBA")
    d.line([v.x(ax), v.y(y0), v.x(ax), v.y(y0 + (y1 - y0) * pg)],
           fill=_rgba(th["rule"], 70), width=_lw(2, lang))

    for i in range(n):
        st = steps[i]
        pn = ease(rng(t, 0.35 + 0.18 * i, 1.05 + 0.18 * i))
        if pn <= 0:
            continue
        cy = y0 + gapy * i
        r = 14.0 * pn
        if _plain(lang):
            # 素净下不画 3px 环（厚度 < 6 的描边都算细线），改实心圆点。
            d.ellipse([v.x(ax - r * 0.8), v.y(cy - r * 0.8),
                       v.x(ax + r * 0.8), v.y(cy + r * 0.8)],
                      fill=_rgba(th["accent"], int(230 * pn)))
        else:
            d.ellipse([v.x(ax - r), v.y(cy - r), v.x(ax + r), v.y(cy + r)],
                      outline=_rgba(th["accent"], int(230 * pn)), width=3)
        d.ellipse([v.x(ax - 4), v.y(cy - 4), v.x(ax + 4), v.y(cy + 4)],
                  fill=_rgba(th["accent"], int(200 * pn)))
        lbl = str(st.get("label") or "")
        _draw_text(img, th, v, lbl, ax + 44, cy - lsize * 0.62, lsize, "bold",
                   th["ink"], "left", pn)
        note = str(st.get("note") or "")
        if note:
            ny = cy + lsize * 0.72
            for j, ln in enumerate(_wrap(note, int(lsize * 0.82), 700, "reg", th)[:2]):
                _draw_text(img, th, v, ln, ax + 44, ny + j * int(lsize * 1.05),
                           int(lsize * 0.82), "reg", th["muted"], "left", pn * 0.95)
        num = _number_text(str(_typo(lang).get("number") or "dim"), i + 1)
        nw = "reg" if any("\u4e00" <= ch <= "\u9fff" for ch in num) else _NUM_WEIGHT
        _draw_text(img, th, v, num, ax - 58, cy - lsize * 0.55, int(lsize * 0.8), nw,
                   th["dim"], "left", pn * 0.9)

    # 持续动态：进度光点沿竖轴匀速（周期 = 段长），永远有运动
    if dur > 0.1:
        u = clamp((t / dur))
        py = y0 + (y1 - y0) * u
        img = _soft_dot(img, v.x(ax), v.y(py), 9.0, th["accent2"], 0.85)

    img = _apply_veil(img, th, lang, scene, t, s_bg)
    return _hud(img, th, scene.get("chapter") or "02 · 正文", _progress(tg, script),
                script.get("title", ""), lang)


# ---------------- 7) quote 引语 ----------------

def _draw_quote(scene, t, dur, tg, script, frame, plan, th):
    lang = _lang(plan, th)
    s_bg = _push_scale(th, t, dur)
    v = _view(th, s_bg)
    img = _plate(th, lang, scene, s_bg)
    ease = _ease((lang.get("motion") or {}).get("ease") or "out3")
    trk = _tracking(th, lang)
    gap = _gap(th, lang)
    ph = _phase(scene)

    try:
        import grammar as _G
    except Exception:                                    # noqa: BLE001
        _G = None
    q, by = (scene.get("quote"), scene.get("by"))
    if not q and _G is not None:
        q, by = _G.derive_quote(scene)
    q = str(q or scene.get("text") or "")
    by = str(by or "")
    if H > W:
        # 竖屏：引文整幅宽铺开、引号在上，署名贴右下安全区。
        return _draw_quote_portrait(scene, t, dur, tg, script, frame, plan, th, q, by)

    # 巨型引号：**用字形**，不用矢量线。
    # 用两条短线拼「《」实测看起来像播放器的"快退"图标，
    # 观众会以为点错了什么。雅黑粗体的 U+201C 是有字形的（preflight 查过不缺口），
    # 直接放大 150px 就是最标准的引语符号。
    pq = ease(rng(t, 0.0, 0.45))
    qx, qy = 150.0, 150.0
    _draw_text(img, th, v, "\u201c", qx, qy, 150, "bold", th["accent"], "left", 0.55 * pq)

    qsize = 46
    max_w = 1240.0
    lines = _wrap(q, qsize, max_w, "light", th)
    while len(lines) > 4 and qsize > 30:
        qsize -= 2
        lines = _wrap(q, qsize, max_w, "light", th)
    lh = int(qsize * float(gap) * 1.16)
    top = 300.0
    reveal = clamp(t / max(0.4, min(2.2, dur * 0.55)))
    total = sum(len(x) for x in lines)
    shown = int(total * ease(reveal))
    walked = 0
    for i, ln in enumerate(lines):
        if walked >= shown:
            break
        take = max(0, min(len(ln), shown - walked))
        walked += len(ln)
        seg = ln[:take]
        if seg:
            _draw_text(img, th, v, seg, 340.0, top + i * lh, qsize, "light",
                       th["ink"], "left", 1.0, trk * 0.4)

    if by:
        pb = ease(rng(t, 0.9, 1.9))
        bw = _text_w("—— " + by, int(th["scale"]["body"][0]), "reg", th)
        _draw_text(img, th, v, "—— " + by, W - 300 - bw, top + len(lines) * lh + 46,
                   int(th["scale"]["body"][0]), "reg", th["muted"], "left", pb)

    # 持续动态：引号只做透明度呼吸，**不做位置漂移**。
    # 6px 的慢速上下浮动 = 0.1px/帧，取整后变成「每 10 帧跳 1px」——那不是呼吸，是抖动。
    bre = 0.5 + 0.5 * math.sin(0.5 * t + ph)
    _draw_text(img, th, v, "\u201c", qx, qy, 150, "bold",
               th["accent"], "left", 0.55 * (0.55 + 0.45 * bre))
    if int(t * 1.1) % 2 == 0:
        ly = top + len(lines) * lh - qsize
        lw = _text_w(lines[-1] if lines else "", qsize, "light", th)
        ImageDraw.Draw(img, "RGBA").rectangle(
            [v.x(340 + lw + 6), v.y(ly), v.x(340 + lw + 14), v.y(ly + qsize)],
            fill=_rgba(th["accent"], 150))

    img = _apply_veil(img, th, lang, scene, t, s_bg)
    return _hud(img, th, scene.get("chapter") or "02 · 正文", _progress(tg, script),
                script.get("title", ""), lang)


# ---------------- 8) chapter 章节卡 ----------------

def _draw_chapter(scene, t, dur, tg, script, frame, plan, th):
    lang = _lang(plan, th)
    s_bg = _push_scale(th, t, dur)
    v = _view(th, s_bg)
    img = _plate(th, lang, scene, s_bg)
    ease = _ease((lang.get("motion") or {}).get("ease") or "out3")
    trk = _tracking(th, lang)
    ph = _phase(scene)

    chapter = str(scene.get("chapter") or "")
    n = _chapter_index(chapter)
    nstyle = str(_typo(lang).get("number") or "dim")
    num = _number_text(nstyle, n, fallback=chapter[:2])
    if H > W:
        # 竖屏：编号/名称/横线纵向重排，横线缩到 SAFE 宽。
        name_p = str(scene.get("big") or "").strip() or chapter
        return _draw_chapter_portrait(scene, t, dur, tg, script, frame, plan, th,
                                     num, name_p, str(scene.get("kicker") or ""))

    # 巨型编号：用矩形"裁剪揭示"（先画字，再用两块背景板盖住未揭示部分），比 mask 便宜
    nsize = 240
    nx, ny = 96.0, 130.0
    _draw_num(img, th, v, num, nx, ny, nsize, th["accent"], "left", 0.30)
    p = ease(clamp(t / 0.7))
    cut = int(nx + _text_w(num, nsize, _NUM_WEIGHT, th) * p)
    if cut < nx + _text_w(num, nsize, _NUM_WEIGHT, th):
        base = _plate(th, lang, scene, s_bg)
        img.paste(base.crop((int(cut), int(ny - 10), W, int(ny + nsize * 1.3))),
                  (int(cut), int(ny - 10)))

    name = str(scene.get("big") or "").strip() or chapter
    pn = ease(rng(t, 0.25, 1.0))
    for j, ln in enumerate(_wrap(name, 68, 1300, "bold", th)[:2]):
        _draw_text(img, th, v, ln, 150.0, 470.0 + j * 86 + _entrance_y(th, pn),
                   68, "bold", th["ink"], "left", pn, trk)

    kicker = str(scene.get("kicker") or "")
    if kicker:
        pk = ease(rng(t, 0.15, 0.8))
        _draw_text(img, th, v, kicker, 150.0, 410.0, int(th["scale"]["kicker"][0]),
                   "bold", th["accent"], "left", pk, trk * 0.5)

    # 横线由中点向两侧生长 + 持续动态（端点微呼吸）
    pl = ease(rng(t, 0.35, 1.1))
    bre = 0.9 + 0.1 * math.sin(1.0 * t + ph)
    half = 800.0 * pl * bre
    d = ImageDraw.Draw(img, "RGBA")
    d.line([v.x(W * 0.5 - half), v.y(700), v.x(W * 0.5 + half), v.y(700)],
           fill=_rgba(th["rule"], 120), width=_lw(2, lang))

    # 持续动态：编号上方一条细光带横向扫（2.8s 周期）。
    # 为什么不做"整条竖光带"：竖着扫过编号会盖住笔画，看起来像渲染错误（实测第一版就是这样），
    # 而且一根 640px 高的半透明条会把剪影里的前景占比拉高，让章节卡和别的类型混淆。
    # 细光带只在编号上缘 6px 高，既有运动又不动主体。
    sx = 96.0 + (W - 400.0) * ((t / 2.8 + ph / 6.28) % 1.0)
    ImageDraw.Draw(img, "RGBA").rectangle(
        [v.x(sx), v.y(118), v.x(sx + 180), v.y(124)], fill=_rgba(th["accent"], 60))

    img = _apply_veil(img, th, lang, scene, t, s_bg)
    return _hud(img, th, chapter or "02 · 正文", _progress(tg, script),
                script.get("title", ""), lang)


# ---------------- 9) chart 数据条 ----------------

def _draw_chart(scene, t, dur, tg, script, frame, plan, th):
    lang = _lang(plan, th)
    s_bg = _push_scale(th, t, dur)
    v = _view(th, s_bg)
    img = _plate(th, lang, scene, s_bg)
    ease = _ease((lang.get("motion") or {}).get("ease") or "out3")
    trk = _tracking(th, lang)
    ph = _phase(scene)

    series = scene.get("_series")
    if not series:
        try:
            import grammar as _G
        except Exception:                                # noqa: BLE001
            _G = None
        series = (_G.derive_series(scene) if _G is not None else []) or []
    if len(series) < 2:
        return _draw_stat(scene, t, dur, tg, script, frame, plan, th)
    if H > W:
        # 竖屏：横屏版把标签画在 bar_x0=640 的**左边**，而竖屏整幅只有 1080 宽——
        # 标签会被钳到 0（_fit_px）与条压在一起，数值则直接画到画布外。
        # 改**标签在条上方、条从 120 起**，条形本身仍是横向（比较同类数值时横向最长条最好读）。
        return _draw_chart_portrait(scene, t, dur, tg, script, frame, plan, th, series)

    big = str(scene.get("big") or "")
    if big:
        pb = ease(rng(t, 0.0, 0.7))
        _draw_text(img, th, v, big, 150.0, 170.0 + _entrance_y(th, pb),
                   int(th["scale"]["heading"][0] - 20), "bold", th["ink"], "left", pb, trk)

    vals = []
    for s in series[:6]:
        try:
            vals.append(float(s.get("value")))
        except (TypeError, ValueError):
            vals.append(0.0)
    vmax = max(vals) if vals else 1.0
    if vmax <= 0:
        vmax = 1.0
    n = len(vals)
    bar_x0, bar_x1 = 640.0, 1600.0
    pitch = min(96.0, (760.0 - 250.0) / max(1, n))
    top = 260.0
    # 网格（一次画完，1px 低 alpha）
    d = ImageDraw.Draw(img, "RGBA")
    # 1px 网格线是细线装饰：素净设计里不画。
    if not _plain(lang):
        pg = ease(rng(t, 0.0, 0.35))
        for k in range(5):
            gx = bar_x0 + (bar_x1 - bar_x0) * k / 4.0
            d.line([v.x(gx), v.y(240), v.x(gx), v.y(240 + pitch * n * pg)],
                   fill=_rgba(th["rule"], 22), width=1)

    for i, s in enumerate(series[:n]):
        p = ease(rng(t, 0.20 + 0.15 * i, 0.95 + 0.15 * i))
        if p <= 0:
            continue
        yy = top + i * pitch
        frac = vals[i] / vmax
        wpx = (bar_x1 - bar_x0) * frac * p
        d = ImageDraw.Draw(img, "RGBA")
        d.rectangle([v.x(bar_x0), v.y(yy), v.x(bar_x0 + wpx), v.y(yy + 42)],
                    fill=_rgba(th["accent"] if i == 0 else th["accent2"], 190))
        lbl = str(s.get("label") or "")
        lsize = int(th["scale"]["body"][0] - 6)
        lw = _text_w(lbl, lsize, "reg", th)
        _draw_text(img, th, v, lbl, bar_x0 - 24 - lw, yy + 6, lsize, "reg",
                   th["ink"], "left", p)
        num = ("%g" % vals[i]) + str(s.get("unit") or "")
        _draw_num(img, th, v, num, bar_x0 + wpx + 16, yy + 4, int(lsize * 1.05),
                  th["ink"], "left", p)

    # 持续动态：最大条端点呼吸 + 一条网格高亮扫描
    if vals:
        i0 = vals.index(max(vals))
        yy = top + i0 * pitch
        r = 4.0 + 3.0 * (0.5 + 0.5 * math.sin(1.8 * t + ph))
        img = _soft_dot(img, v.x(bar_x1), v.y(yy + 21), r, th["accent"], 0.8)
    scan_y = 240.0 + (pitch * n) * ((t / 2.6 + ph / 6.28) % 1.0)
    ImageDraw.Draw(img, "RGBA").line(
        [v.x(bar_x0), v.y(scan_y), v.x(bar_x1), v.y(scan_y)],
        fill=_rgba(th["accent"], 26), width=_lw(2, lang))

    img = _apply_veil(img, th, lang, scene, t, s_bg)
    return _hud(img, th, scene.get("chapter") or "02 · 正文", _progress(tg, script),
                script.get("title", ""), lang)


def _draw_chart_portrait(scene, t, dur, tg, script, frame, plan, th, series):
    """竖屏数据条：**标签在条上方、数值在条内右端**，条形仍是横向。

    为什么保留横向条形：比较一组同类数值时，"最长条"比"最高柱"更好读，而且竖屏纵向高度
    足够放下 6 条（每条 一标签 + 一条 + 间距）。真正要改的是**标签的位置**——横屏版把它画在
    条形左侧（bar_x0=640 减去标签宽），在 1080 宽的画布里会把标签推到负坐标。

    数值的画法也换了：横屏版一律画在条的右端外侧（+16px），竖屏里那条位置会随最短条跑到画布外；
    改成"够长就放条内右端、太短就放条外右侧"。
    """
    lang = _lang(plan, th)
    s_bg = _push_scale(th, t, dur)
    v = _view(th, s_bg)
    img = _plate(th, lang, scene, s_bg)
    ease = _ease((lang.get("motion") or {}).get("ease") or "out3")
    trk = _tracking(th, lang)
    ph = _phase(scene)

    big = str(scene.get("big") or "")
    if big:
        pb = ease(rng(t, 0.0, 0.7))
        _draw_text(img, th, v, big, 100.0, 190.0 + _entrance_y(th, pb),
                   int(th["scale"]["heading"][0] - 26), "bold", th["ink"], "left", pb, trk)

    vals = []
    for s in series[:6]:
        try:
            vals.append(float(s.get("value")))
        except (TypeError, ValueError):
            vals.append(0.0)
    vmax = max(vals) if vals else 1.0
    if vmax <= 0:
        vmax = 1.0
    n = len(vals)

    x0, x1 = 100.0, float(W - 100)
    top = 330.0
    row_h = min(210.0, (1420.0 - top) / max(1, n))     # 每行 = 标签 + 条 + 间隙
    bar_h = 34.0
    lsize = int(th["scale"]["body"][0] - 6)

    d = ImageDraw.Draw(img, "RGBA")
    for i, s in enumerate(series[:n]):
        p = ease(rng(t, 0.20 + 0.15 * i, 0.95 + 0.15 * i))
        if p <= 0:
            continue
        ry = top + i * row_h
        lbl = str(s.get("label") or "")
        _draw_text(img, th, v, lbl, x0, ry, lsize, "reg", th["ink"], "left", p)
        by = ry + lsize * 1.42
        frac = vals[i] / vmax
        wpx = (x1 - x0) * frac * p
        d.rectangle([v.x(x0), v.y(by), v.x(x0 + wpx), v.y(by + bar_h)],
                    fill=_rgba(th["accent"] if i == 0 else th["accent2"], 190))
        num = ("%g" % vals[i]) + str(s.get("unit") or "")
        nsize = int(lsize * 1.02)
        nw = _text_w(num, nsize, _NUM_WEIGHT, th)
        # 条够长 -> 数字放条内右端（白字，靠对比读）；不够长 -> 放条外右侧（不与条重叠）
        if wpx > nw + 36:
            _draw_num(img, th, v, num, x0 + wpx - nw - 14, by + 4, nsize,
                      (255, 255, 255), "left", p)
        else:
            _draw_num(img, th, v, num, x0 + wpx + 14, by + 4, nsize,
                      th["ink"], "left", p)

    # 持续动态：最大条端点呼吸（与横屏一致的可读"最强项"提示）
    if vals:
        i0 = vals.index(max(vals))
        ry = top + i0 * row_h + lsize * 1.42
        r = 4.0 + 3.0 * (0.5 + 0.5 * math.sin(1.8 * t + ph))
        img = _soft_dot(img, v.x(x1), v.y(ry + bar_h * 0.5), r, th["accent"], 0.8)

    img = _apply_veil(img, th, lang, scene, t, s_bg)
    return _hud(img, th, scene.get("chapter") or "02 · 正文", _progress(tg, script),
                script.get("title", ""), lang)



def _draw_stat_portrait(scene, t, dur, tg, script, frame, plan, th, stats):
    """竖屏数字卡：数字在上、口径在中、刻度在下，纵向铺满一屏。

    横屏版把「大数字 + 口径 + 12 格刻度 + 2 条次要统计」压在 1080 高的横带里；
    竖屏（1080x1920）照搬会让上半屏空、下半屏挤。竖屏纵向空间足够把四层摊开：
    数字(y≈620) / 主口径(y≈980) / 刻度(y≈1160) / 次要统计(y≈1300 起)。
    """
    lang = _lang(plan, th)
    s_bg = _push_scale(th, t, dur)
    v = _view(th, s_bg)
    img = _plate(th, lang, scene, s_bg)
    ease = _ease((lang.get("motion") or {}).get("ease") or "out3")
    ph = _phase(scene)

    main = stats[0]
    raw = str(main.get("value") if main.get("value") is not None else "")
    unit = str(main.get("unit") or "")
    label = str(main.get("label") or "")
    notes = [s for s in stats[1:3] if str(s.get("label") or "")]

    target = None
    try:
        target = float(raw)
    except (TypeError, ValueError):
        target = None
    p = ease(clamp(t / max(0.5, min(1.2, dur * 0.42))))
    if target is None:
        shown_txt = raw
    elif "." in raw:
        dec = len(raw.split(".")[1])
        shown_txt = ("%." + str(dec) + "f") % (target * p)
    else:
        shown_txt = "%d" % int(round(target * p))

    # 数字：整屏最重的一层。max_w 用 SAFE 宽度（不是横屏常量 1500），
    # 长数字（103612P / 34.7）在 1080 宽里靠缩字号，而不是靠 _fit_px 钳位。
    size = 200
    max_w = float(SAFE[2] - SAFE[0])
    while size > 96 and _text_w(shown_txt + unit, size, _NUM_WEIGHT, th) > max_w:
        size = int(size * 0.9)
    cy = 700.0
    total_w = _text_w(shown_txt, size, _NUM_WEIGHT, th)
    if unit:
        total_w += _text_w(unit, int(size * 0.42), "reg", th)
    x0 = W * 0.5 - total_w * 0.5
    _draw_num(img, th, v, shown_txt, x0, cy - size * 0.62, size, th["accent"], "left",
              0.25 + 0.75 * p)
    if unit:
        pu = ease(rng(t, 0.35, 0.95))
        _draw_text(img, th, v, unit, x0 + _text_w(shown_txt, size, _NUM_WEIGHT, th) + 12,
                   cy + size * 0.22, int(size * 0.42), "reg", th["muted"], "left", pu)

    if label:
        pl = ease(rng(t, 0.45, 1.10))
        lsize = int(th["scale"]["sub"][0])
        for j, ln in enumerate(_wrap(label, lsize, max_w, "light", th)[:2]):
            _draw_text(img, th, v, ln, W * 0.5, 1060.0 + j * int(lsize * 1.3), lsize,
                       "light", th["ink"], "center", pl)

    # 持续动态：12 格刻度逐格点亮（横屏横排、竖屏竖排：与画面主要方向同向）
    bars = 12
    bh, gapb = 26, 8
    tot = bars * bh + (bars - 1) * gapb
    by = 1240.0
    bx = W * 0.5 - tot * 0.5
    lit = int((t / 0.55 + ph) % bars)
    d = ImageDraw.Draw(img, "RGBA")
    for i in range(bars):
        on = i <= lit
        d.rectangle([v.x(bx + i * (bh + gapb)), v.y(by),
                     v.x(bx + i * (bh + gapb) + bh), v.y(by + 8)],
                    fill=_rgba(th["accent"] if on else th["rule"],
                               int((150 if on else 34))))

    nsize = int(th["scale"]["body"][0] - 6)
    step = int(nsize * 1.75)
    for j, s in enumerate(notes):
        pn = ease(rng(t, 0.6 + 0.15 * j, 1.3 + 0.15 * j))
        val = str(s.get("value") if s.get("value") is not None else "").strip()
        un = str(s.get("unit") or "").strip()
        lbl = str(s.get("label") or "").strip()
        head = val + ((" " + un) if un else "")
        txt = (head + "  " + lbl) if head else lbl
        if not txt:
            continue
        txt = _fit_size(txt, max_w, nsize, "reg", th, 18)
        _draw_text(img, th, v, txt, W * 0.5, 1400.0 + j * step, nsize,
                   "reg", th["muted"], "center", pn)

    img = _apply_veil(img, th, lang, scene, t, s_bg)
    return _hud(img, th, scene.get("chapter") or "02 · 正文", _progress(tg, script),
                script.get("title", ""), lang)


def _draw_quote_portrait(scene, t, dur, tg, script, frame, plan, th, q, by):
    """竖屏引语：巨型引号在上、引文纵向铺开、署名右下。

    横屏版引文 max_w=1240、起始 x=340，放在 1080 宽里会被 _fit_px 钳成左边距 0；
    竖屏改成整幅宽（SAFE 宽 - 60），字号与折行数上限也随之调整（竖屏能放更多行）。
    """
    lang = _lang(plan, th)
    s_bg = _push_scale(th, t, dur)
    v = _view(th, s_bg)
    img = _plate(th, lang, scene, s_bg)
    ease = _ease((lang.get("motion") or {}).get("ease") or "out3")
    trk = _tracking(th, lang)
    gap = _gap(th, lang)
    ph = _phase(scene)

    max_w = float(SAFE[2] - SAFE[0] - 40)
    left = float(SAFE[0] + 20)

    pq = ease(rng(t, 0.0, 0.45))
    _draw_text(img, th, v, "\u201c", left, 300.0, 170, "bold", th["accent"], "left", 0.55 * pq)

    qsize = 54
    lines = _wrap(q, qsize, max_w, "light", th)
    while len(lines) > 7 and qsize > 32:
        qsize -= 2
        lines = _wrap(q, qsize, max_w, "light", th)
    lh = int(qsize * float(gap) * 1.20)
    block_h = max(1, len(lines)) * lh + (60.0 if by else 0.0)
    body_top, body_bot = float(SAFE[1]) + 240.0, float(SUB_BAND[1]) - 40.0
    top = max(body_top, (body_top + body_bot - block_h) * 0.5)
    reveal = clamp(t / max(0.4, min(2.2, dur * 0.55)))
    total = sum(len(x) for x in lines)
    shown = int(total * ease(reveal))
    walked = 0
    for i, ln in enumerate(lines):
        if walked >= shown:
            break
        take = max(0, min(len(ln), shown - walked))
        walked += len(ln)
        seg = ln[:take]
        if seg:
            _draw_text(img, th, v, seg, left, top + i * lh, qsize, "light",
                       th["ink"], "left", 1.0, trk * 0.4)

    if by:
        pb = ease(rng(t, 0.9, 1.9))
        bsize = int(th["scale"]["body"][0])
        bw = _text_w("—— " + by, bsize, "reg", th)
        bx = float(SAFE[2] - 20) - bw
        _draw_text(img, th, v, "—— " + by, bx, top + len(lines) * lh + 60,
                   bsize, "reg", th["muted"], "left", pb)

    # 持续动态：引号透明度呼吸（与横屏同口径，不做位置漂移）
    bre = 0.5 + 0.5 * math.sin(0.5 * t + ph)
    _draw_text(img, th, v, "\u201c", left, 300.0, 170, "bold",
               th["accent"], "left", 0.55 * (0.55 + 0.45 * bre))
    if int(t * 1.1) % 2 == 0:
        ly = top + max(0, len(lines) - 1) * lh + qsize * 0.92
        lw = _text_w(lines[-1] if lines else "", qsize, "light", th)
        rx = min(left + lw + 8, float(SAFE[2] - 12))
        ImageDraw.Draw(img, "RGBA").rectangle(
            [v.x(rx), v.y(ly), v.x(rx + 10), v.y(ly + qsize * 0.5)],
            fill=_rgba(th["accent"], 150))

    img = _apply_veil(img, th, lang, scene, t, s_bg)
    return _hud(img, th, scene.get("chapter") or "02 · 正文", _progress(tg, script),
                script.get("title", ""), lang)


def _draw_chapter_portrait(scene, t, dur, tg, script, frame, plan, th, num, name, kicker):
    """竖屏章节卡：编号在上、名称居中、横线在下。

    横屏版编号 240px 在 x=96、名称 68px 在 y=470、横线跨 1600px（y=700）；
    竖屏把横线缩到 SAFE 宽、整体纵向重新分布，否则下半屏会空掉 1000px。
    """
    lang = _lang(plan, th)
    s_bg = _push_scale(th, t, dur)
    v = _view(th, s_bg)
    img = _plate(th, lang, scene, s_bg)
    ease = _ease((lang.get("motion") or {}).get("ease") or "out3")
    trk = _tracking(th, lang)
    ph = _phase(scene)

    nsize = 300
    nx, ny = float(SAFE[0] + 20), 420.0
    _draw_num(img, th, v, num, nx, ny, nsize, th["accent"], "left", 0.30)
    p = ease(clamp(t / 0.7))
    nw_full = _text_w(num, nsize, _NUM_WEIGHT, th)
    cut = int(nx + nw_full * p)
    if cut < nx + nw_full:
        base = _plate(th, lang, scene, s_bg)
        top = int(ny - 12)
        bot = int(ny + nsize * 1.28)
        img.paste(base.crop((int(cut), top, W, bot)), (int(cut), top))

    pn = ease(rng(t, 0.2, 1.0))
    y = 940.0
    name_size = 76
    max_w = float(SAFE[2] - SAFE[0])
    for j, ln in enumerate(_wrap(name, name_size, max_w, "bold", th)[:3]):
        size = _fit_size(ln, max_w, name_size, "bold", th, 40)
        _draw_text(img, th, v, ln, W * 0.5, y + j * int(size * 1.25) + _entrance_y(th, pn),
                   size, "bold", th["ink"], "center", pn, trk)

    if kicker:
        pk = ease(rng(t, 0.15, 0.8))
        _draw_text(img, th, v, kicker, W * 0.5, 910.0, int(th["scale"]["kicker"][0]),
                   "bold", th["accent"], "center", pk, trk * 0.5)

    pl = ease(rng(t, 0.35, 1.1))
    bre = 0.9 + 0.1 * math.sin(1.0 * t + ph)
    half = (max_w * 0.5) * pl * bre
    # 横线贴着名称块下方（原来放 1450 会在名称与线之间留 400px 空洞）
    cy = 1290.0
    ImageDraw.Draw(img, "RGBA").line(
        [v.x(W * 0.5 - half), v.y(cy), v.x(W * 0.5 + half), v.y(cy)],
        fill=_rgba(th["rule"], 120), width=_lw(2, lang))

    # 持续动态：编号上缘细光带（与横屏同口径：只在 6px 高，既动又不盖笔画）
    span = float(SAFE[2] - SAFE[0] - 200)
    sx = float(SAFE[0]) + span * ((t / 2.8 + ph / 6.28) % 1.0)
    ImageDraw.Draw(img, "RGBA").rectangle(
        [v.x(sx), v.y(ny - 14), v.x(sx + 170), v.y(ny - 8)],
        fill=_rgba(th["accent"], 60))

    img = _apply_veil(img, th, lang, scene, t, s_bg)
    return _hud(img, th, scene.get("chapter") or "02 · 正文", _progress(tg, script),
                script.get("title", ""), lang)

# ---------------- 10) outro 收尾 ----------------

def _draw_outro(scene, t, dur, tg, script, frame, plan, th):
    lang = _lang(plan, th)
    s_bg = _push_scale(th, t, dur)
    v = _view(th, s_bg)
    img = _plate(th, lang, scene, s_bg)
    lay = _scene_layout(scene, th)
    ease = _ease((lang.get("motion") or {}).get("ease") or "out3")
    trk = _tracking(th, lang)
    gap = _gap(th, lang)
    ph = _phase(scene)

    # 光点环：数量随视觉语言母题走（epic 18 / cinema 12 / mono 0）
    mf = str((lang.get("motif") or {}).get("kind") or "minimal")
    ring_n = {"rays": 18, "letterbox": 12, "brackets": 10, "signal": 8,
              "handdrawn": 6, "slabs": 12, "hairlines": 6, "minimal": 0}.get(mf, 8)
    p = ease(rng(t, 0.0, 1.0))
    for i in range(ring_n):
        a = (i / float(max(1, ring_n))) * 2.0 * math.pi + 0.15 * t + ph
        rr = 460.0 * (1.0 - p) + 70.0 + 16.0 * K.hash2(i, 4)
        img = _soft_dot(img, v.x(W * 0.5 + math.cos(a) * rr),
                        v.y(520.0 + math.sin(a) * rr * 0.5), 3.0,
                        th["accent2"], 0.5 * (1.0 - p * 0.7))

    big = scene.get("big") or script.get("end_text") or "完"
    size = int(th["scale"]["display"][0])
    lines = _wrap(big, size, min(float(lay["title_w"]), 1520.0), "bold", th)
    while len(lines) > 3 and size > 42:
        size = int(size * 0.88)
        lines = _wrap(big, size, min(float(lay["title_w"]), 1520.0), "bold", th)
    lh = int(size * gap)
    pa = ease(rng(t, 0.30, 1.30))
    if H > W:
        # 竖屏：收尾块（大字 + 副标题 + 下划线）整体居中；横屏的 380 在竖屏里会贴到上方 1/5。
        body_top, body_bot = float(SAFE[1]) + 160.0, float(SUB_BAND[1]) - 80.0
        top = (body_top + body_bot - len(lines) * lh) * 0.5
    else:
        top = 380.0 - (len(lines) - 1) * lh * 0.5
    for i, ln in enumerate(lines):
        _draw_text(img, th, v, ln, W * 0.5, top + i * lh + _entrance_y(th, pa),
                   size, "bold", th["ink"], "center", 0.02 + 0.98 * pa, trk)
    bottom = top + len(lines) * lh + _entrance_y(th, pa)

    sub = scene.get("sub") or ""
    # 与大字重复的副标题不画（同 hero：模板片 sub 与 big 是同一句）
    if sub and (str(sub).strip() == str(big).strip()
                or (len(str(sub)) >= 6 and str(sub).strip() in str(big).strip())):
        sub = ""
    if sub:
        ssize = int(th["scale"]["sub"][0])
        p2 = ease(rng(t, 0.55, 1.55))
        for j, ln in enumerate(_wrap(sub, ssize, 1400, "light", th)[:2]):
            _draw_text(img, th, v, ln, W * 0.5,
                       bottom + int(size * 0.22) + j * int(ssize * gap),
                       ssize, "light", th["muted"], "center", p2)
        bottom = bottom + int(size * 0.22) + int(ssize * gap)

    grow = ease(rng(t, 0.45, 1.45))
    bre = 0.82 + 0.18 * math.sin(0.9 * t + ph)
    line_w = int(340 * grow * bre)
    ly = min(bottom + int(size * 0.30), 840)
    ImageDraw.Draw(img, "RGBA").line(
        [v.x(W * 0.5 - line_w * 0.5), v.y(ly), v.x(W * 0.5 + line_w * 0.5), v.y(ly)],
        fill=_rgba(th["accent"], 200), width=_lw(3, lang))

    img = _apply_veil(img, th, lang, scene, t, s_bg)
    return _hud(img, th, scene.get("chapter") or "99 · 收尾", 1.0, script.get("title", ""), lang)


_DRAWERS = {
    "hero": _draw_hero,
    "statement": _draw_statement,
    "list": _draw_list,
    "stat": _draw_stat,
    "compare": _draw_compare,
    "process": _draw_process,
    "quote": _draw_quote,
    "chapter": _draw_chapter,
    "chart": _draw_chart,
    "outro": _draw_outro,
}

# 老 kind -> 场景类型（没有 scene_type 的旧 timeline / 手工构造的 scene 走这里）
_KIND_TO_TYPE = {
    "hook": "hero", "body": "list", "quote": "quote", "outro": "outro",
    "emphasis": "statement", "calm": "statement",
}


# ============================================================================
# 四、对外契约
# ============================================================================

def draw_frame(scene, t_local, dur, t_global, script, frame):
    """按帧画一张 1920x1080 RGB 图。

    只有一条路：按 scene_type（grammar.py 选好、render.py 写进 timeline）
    派发到 10 类构图之一；没有 scene_type 的旧 timeline 按 kind 映射到兼容类型。

    原来的第 1 条分支（legacy —— film.json 不写 style 就走三个老绘制器）**已删除**：
    stylekit 不再产出 legacy 计划（不写设计会直接报错），那条分支永远走不到。
    留着死分支比删掉更危险——它会让"legacy 还能用"的错觉一直存在。

    未命中的类型回落到 list（"有条目就画条目"是最不会丢信息的兜底），
    并 warn 一次——静默画错构图比报错更糟。
    """
    plan = _plan(script)
    kind = scene.get("kind") or "body"
    th = _scene_theme(plan, scene)
    stype = str(scene.get("scene_type") or "").strip()
    if not stype:
        stype = _KIND_TO_TYPE.get(kind, "list")
    fn = _DRAWERS.get(stype)
    if fn is None:
        _warn_once(("scene_type", stype),
                   "场景 %r 的 scene_type=%r 不认识，已回落到 list 构图（可用：%s）"
                   % (scene.get("id"), stype, " / ".join(_DRAWERS.keys())))
        fn = _draw_list
    return fn(scene, t_local, dur, t_global, script, frame, plan, th)


# ---------------- 提示点（按 kind 与内容生成） ----------------
def _sha1_int(*parts):
    """把若干文本片段拼起来取 sha1 前 8 位（提示点"内容派生"的稳定来源）。"""
    raw = "|".join(str(p) for p in parts).encode("utf-8")
    return int(hashlib.sha1(raw).hexdigest()[:8], 16)


def _item_pitch(text, base=1760.0):
    """条目文本 -> ui_blip 音高（sha1 派生，不用序号）。

    契约 §3.4：body 条目的 pitch 必须由**条目文本**派生，不再按序号
    （1760+110*i 每片都一样）——同一句文案换到别的片子里仍是同一个音高，
    不同文案落到 -5~+7 半音的不同音级；万一两条碰撞，_dedupe_segment 会再升半音。
    """
    off = _sha1_int("pitch", text) % 13 - 5
    return round(float(base) * 2.0 ** (off / 12.0), 2)


def _cue_gain_decay(gain, repeat):
    """同段内同类型 cue 第 repeat 次重复（0 起）时的增益：每次再低 2 dB（-2/-4/-6/…）。

    契约 §3.4：不许出现"每段都一模一样的三连击"——同类型重复必须**严格递减**
    （第 2 次 -2、第 3 次 -4、第 4 次 -6……），且 pitch 必须不同。
    """
    return round(float(gain) - 2.0 * int(repeat), 2)


def _dedupe_segment(seg):
    """同一段内：同类型重复的 gain 递减、pitch 去重（保证"pitch 必须不同"）。

    正文条目多时 sha1 可能把两条吸附到同一个半音；碰撞就把后一个再升半音——
    仍然由文本派生，只是保证同段里不出现两个一模一样的高音（听着像卡带）。
    """
    seen = {}                                  # type -> {"n": 已出现次数, "pitch": 已用音高集}
    for c in seg:
        st = seen.setdefault(c["type"], {"n": 0, "pitch": set()})
        if st["n"]:
            c["gain"] = _cue_gain_decay(c.get("gain", 0.0), st["n"])
        st["n"] += 1
        p = c.get("pitch")
        if p is not None:
            p = float(p)
            guard = 0
            while round(p, 2) in st["pitch"] and guard < 12:
                p *= 2.0 ** (1.0 / 12.0)
                guard += 1
            c["pitch"] = round(p, 2)
            st["pitch"].add(round(p, 2))
    return seg


def cues(tl, script):
    """画面提示点钩子：render.py --stage cues 会调它（没有这个函数就不调）。

    好处是"声音跟着画面的像素走"：这里算的是卡片落位、数字滚完的时刻，
    画面一改，声音自动跟着改，不用回来手工对时间。

    条目落位步长与场景绘制共用 plan["pacing"]["item_stagger"]，
    两边读同一个数，否则音效会错位（写死 0.32 时踩过这个坑）。

    见 references/audio-variation.md §3.3：提示点按**场景 kind 与内容**生成，
    不是"每段 ui_blip 三连 + 结尾 answer_chime"的同质化固定套路（跨片音效雷同的
    来源之一）：
      hook  : 段首 impact（size 按字数 0.9~1.3）+ 落定 riser
      body  : 条目落位 ui_blip（pitch 由条目文本 sha1 派生）+ items>=3 补 answer_chime
              + 段尾 whoosh（转场到下一段）
      quote : reverse_swell + 落定轻 impact
      outro : answer_chime + 片尾 sub_drop
    同段内同类型 cue 重复时 gain 逐次再降 2 dB（-2/-4/-6）、pitch 必须不同。
    返回 schema 不变：[{"at": 时刻, "type": 配方名, ...参数}]；
      at 可以写 "abs:12.5" / 12.5 / "l03+0.35"（相对该段 start）/"l03"
      type 见 references/sfx-design.md 的配方表，或 sfxkit.list_recipes()
    """
    plan = _plan(script)
    stagger = 0.32
    if not plan.get("legacy"):
        stagger = float((plan.get("pacing") or {}).get("item_stagger") or 0.32)
    scenes = list(tl.get("scenes") or [])
    out = []
    for i, s in enumerate(scenes):
        sid = str(s.get("id") or ("l%02d" % (i + 1)))
        kind = str(s.get("kind") or "body")
        end = float(s.get("end") or 0.0)
        dur = float(s.get("dur") or max(0.0, end - float(s.get("start") or 0.0)))
        has_next = (i + 1) < len(scenes)
        seg = []

        def _at(off, sid=sid):
            """本段相对时刻 -> cue 的 at 写法（id+偏移，保持返回 schema 不变）。"""
            return "%s+%.2f" % (sid, max(0.0, float(off)))

        if kind == "hook":
            # 开场重击按文案字数给"体量"：字数越多 size 越大（0.9~1.3），避免每片同一记闷响
            text = str(s.get("text") or s.get("big") or "")
            n_char = len("".join(text.split()))
            size = round(0.9 + 0.4 * min(1.0, n_char / 40.0), 2)
            seg.append({"at": _at(0.10), "type": "impact", "size": size, "gain": -2})
            seg.append({"at": _at(min(1.2, max(0.35, dur * 0.28))),
                        "type": "riser", "dur": 1.2, "gain": -6})
        elif kind == "quote":
            # 引语段：先来一层反向渐强铺底，落定时轻一点的一击
            seg.append({"at": _at(0.05), "type": "reverse_swell", "dur": 1.2, "gain": -5})
            seg.append({"at": _at(min(1.4, max(0.5, dur * 0.30))),
                        "type": "impact", "size": 0.6, "gain": -8})
        elif kind == "outro":
            # 收尾：答对钟声 + 片尾 sub_drop（尾巴正好落在段尾，不留半截低频）
            seg.append({"at": _at(0.35), "type": "answer_chime", "gain": -4})
            seg.append({"at": _at(max(0.30, dur - 1.60)),
                        "type": "sub_drop", "dur": 1.6, "gain": -5})
        else:
            # body（以及 emphasis/calm 等未单独设计的 kind：按正文处理，向后兼容）
            items = list(s.get("items") or [])
            for j, it in enumerate(items):
                # 条目逐条落位：第一项在 0.48s 出现，之后每 stagger 秒一个（与 _draw_items 同一拍）
                at = 0.48 + stagger * j
                if at > dur - 0.12:            # 极短段：放不下的条目不再排提示点（避免响到下一段）
                    break
                txt = it.get("text") if isinstance(it, dict) else it
                seg.append({"at": _at(at), "type": "ui_blip",
                            "pitch": _item_pitch(txt), "gain": -6})
            blips = [c for c in seg if c["type"] == "ui_blip"]
            if len(items) >= 3 and blips and dur > 0.6:
                # 最后一个条目落定后的下一拍补一声"猜中了"，给列表收尾
                last_at = float(blips[-1]["at"].split("+", 1)[1])
                t_chime = min(last_at + stagger, max(last_at + 0.20, dur - 0.30))
                seg.append({"at": _at(t_chime), "type": "answer_chime", "gain": -6})
            if has_next and dur > 0.6:
                # 段尾 whoosh：从本段扫进下一段（C 路会在段界附近给转场类音效让位）
                seg.append({"at": _at(max(0.0, dur - 0.45)),
                            "type": "whoosh", "dur": 0.9, "gain": -6})
        out.extend(_dedupe_segment(seg))
    return out
