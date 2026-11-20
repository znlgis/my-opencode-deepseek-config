# -*- coding: utf-8 -*-
"""webengine.py — 浏览器逐帧渲染引擎。

用无头浏览器（优先 Microsoft Edge，失败回落 Playwright Chromium）打开 Canvas 页面，
把时间精确跳到「帧号 / FPS」后经 CDP 截 PNG，喂给 ffmpeg 的 image2pipe 编成视频。
分块并行 + 断点续跑 + 显式缺口检测 + 崩溃重开；同一帧号渲染出的画面完全一致。

**跟随风格包**。公开入口都有一组可选参数（style / width / height / fps）：
    style       stylekit 的 style plan（dict，含 "theme"）或裸主题 dict；None = 不注入
    width/height 画布尺寸，只接受 16:9 横屏或 9:16 竖屏的偶数尺寸（4:3 / 其它比例 / 奇数直接 raise）
    fps         写进注入 payload 的帧率；None = 不约束（页面自己的 window.FPS 为准）
注入方式：add_init_script 在**页面加载前**写 window.__STYLE__（主题色板 / 版式 / 字体栈 /
画布 w·h·fps）。assets/web/engine.js 与 film.js 优先读它，取不到就退回深蓝橙常量。

三条硬约束（改这块必须先读）：
1. **style=None 时一个字节都不注入**：open_page 连 add_init_script 都不调，页面里不存在
   window.__STYLE__，渲染像素与默认外观逐字节一致（自检里有「无 style 页面无 __STYLE__」断言，
   外部还有逐帧 SHA-256 对照）。
2. 注入内容只来自入参：不读环境变量/时间/随机数，JSON 用 sort_keys 序列化，同输入同输出。
3. 尺寸与帧率 fail-closed：不符合 16:9 横屏 / 9:16 竖屏偶数就 raise，绝不静默缩放或改帧率
   （静默改帧率会让分块帧号整体错位，成片音画不同步且门禁看不出来）。

算法改编自 ai-history-film（MIT）video/render.py（逐帧 seek / 分块并行 / 崩溃重开 / 断点续跑），
代码按本项目契约重写；拼接复用 filmkit.concat_segments（必须传绝对路径，别改成相对路径）。

设计要点：
1. 逐帧渲染是无状态的：崩溃后重开浏览器从同一帧继续即可，不需要回滚。
2. 分块按「绝对起始帧 // 每块帧数」命名，重跑时按文件存在性跳过；拼接前显式查缺口，
   缺块直接 raise，不允许「少一段也出片」。
3. 先写 xxx.part.mp4，ffmpeg 正常退出后再 rename 成 xxx.mp4：中断只会留下 .part 垃圾，
   不会让半截文件被误认成已完成的块。
4. 失败要报错，不要静默降级：页面没 ready、DURATION 对不上、块时长异常都直接抛异常。

CLI：
    python webengine.py snap <html> --times 3,9.5 ...     # 截静帧 + 联系表
    python webengine.py frames <html> --frames 45,270 ... # 指定帧号截 PNG，打印 SHA-256
    python webengine.py cues <html> [--out x.json]        # 导出 cue / 视觉事件（§4.2 schema）
    python webengine.py full <html> --out x.mp4 [--workers 2] [--resume]
    python webengine.py textqa <html> [--from 0 --to 12 --step 0.2]
    python webengine.py selftest                          # 等价于 --selftest
    # 以上命令都可加风格参数（缺省 = 不注入）：
    #   --style work/style.json | --style '{"theme":{...}}'
    #   --width 1920 --height 1080 [--fps 30]
"""
from __future__ import annotations

__version__ = "1.1.0"

import argparse
import asyncio
import base64
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# ---------------------------------------------------------------- 常量（冻结契约 §5.3）
FPS = 30                  # 默认 30，可被页面 window.FPS 覆盖
W, H = 1920, 1080
FRAME_TIMEOUT = 90.0      # 单帧截图超时（秒），超时视同崩溃 → 重开浏览器
RETRY = 5                 # 崩溃重开重试次数
PART_SUFFIX = ".part.mp4"

_SCRIPT_DIR = Path(__file__).resolve().parent
_ASSET_DIR = _SCRIPT_DIR.parent / "assets" / "web"
DEFAULT_HTML = _ASSET_DIR / "index.html"

# 浏览器启动参数（同 ai-history-film；GPU/ANGLE、无滚动条、静音、禁后台节流、允许 file:// 读取）
BROWSER_ARGS = [
    "--use-angle=d3d11",
    "--enable-gpu",
    "--ignore-gpu-blocklist",
    "--enable-unsafe-swiftshader",
    "--allow-file-access-from-files",
    "--disable-web-security",
    "--force-color-profile=srgb",
    "--hide-scrollbars",
    "--mute-audio",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
]
# 确定性模式：关掉 GPU 光栅，2D canvas 全部走软件光栅。
# 为什么需要：默认参数下**同一页面会话内**截图 byte 完全一致，但**跨浏览器会话**
# （崩溃重开 / 多 worker / 续跑重渲）不完全一致——GPU 抗锯齿光栅化会有极小的非确定性。
# 实测（assets/web 模板，f=179/180，8 个独立会话）：
#   默认参数：8 个会话分成 2 簇，差异 4 个像素、每个通道 ±1 级（文字抗锯齿边缘），
#             成片经 H.264 后不可见，但"byte 完全一致"的说法不成立；
#   加 --disable-gpu：8/8 会话 byte 完全一致，代价是每帧 79.9 ms → 97.7 ms（约 +22%）。
# 所以做成显式开关（WEBENGINE_DETERMINISTIC=1 / --deterministic），默认仍走 GPU 求快；
# 要求"跨会话字节级可复现"（例如质检要拿两次渲染做 diff）时再打开。
DETERMINISTIC_ARGS = [
    "--disable-gpu",
]

_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_BROWSER_KIND = ""       # 最近一次 open_page 实际用上的浏览器（msedge / chromium），只用于日志与自检


def _setup_stdout() -> None:
    """管道输出时统一 UTF-8（控制台保持系统编码），避免中文写进日志/CI 后乱码。"""
    try:
        enc = (sys.stdout.encoding or "").lower()
        if not sys.stdout.isatty() and enc not in ("utf-8", "utf8", "cp65001"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _load_filmkit():
    """只读复用 filmkit：ffmpeg 定位 / 联系表 / 绝对路径 concat（绝不修改 filmkit）。"""
    try:
        import filmkit  # type: ignore
        return filmkit
    except Exception:
        pass
    here = str(_SCRIPT_DIR)
    if here not in sys.path:
        sys.path.insert(0, here)
    import filmkit  # type: ignore
    return filmkit


_K = _load_filmkit()
FFMPEG = _K.FFMPEG


# ---------------------------------------------------------------- 风格注入（契约）
# style 的两种合法写法：
#   ① stylekit 的 style plan（dict，至少含 "theme" 字典；render.py --stage style 落的 work/style.json 就是它）
#   ② 裸主题字典（扁平字段，颜色是 (r,g,b) 元组；设计的 theme 就是这种形状）
# 归一化后是**纯数据** payload，经 add_init_script 写成 window.__STYLE__。
# 这里不做任何"猜"：缺键 / 颜色非法 / 字体族未知一律 raise，不做静默回落（回落等于换个画风还不告诉人）。
STYLE_VERSION = 1

# family → 浏览器字体栈。键必须与 filmkit.FONT_FAMILIES 的四个族一致，否则网页与 PIL 两条
# 路线的画风会对不上（同一套 style 在 --engine pil / web 下字体要看起来是一家人）。
FONT_STACKS = {
    "sans": '"Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", sans-serif',
    "serif": '"Noto Serif SC", "Source Han Serif SC", SimSun, "Songti SC", serif',
    "kai": 'KaiTi, "Kaiti SC", STKaiti, "Microsoft YaHei", serif',
    "round": 'DengXian, "Microsoft YaHei", sans-serif',
}

# 主题里必须齐全的色板键（designkit 构造的主题都有；少一个就说明传错东西了）
THEME_COLOR_KEYS = ("bg", "bg2", "surface", "ink", "muted", "dim",
                    "accent", "accent2", "good", "bad", "rule")

# 画布只收 16:9 横屏或 9:16 竖屏（与 layoutkit 的两套 profile 一致）：宽高都必须偶数
# （H.264 yuv420p 要求），宽高比误差 ≤ 1e-3；其它比例（4:3 等）仍然 fail-closed。
_ASPECT_TOL = 1e-3
_MIN_CANVAS = (320, 180)


def _validate_canvas(width, height):
    """校验画布尺寸（16:9 横屏 / 9:16 竖屏 + 偶数），返回 (int, int)；不合格 raise ValueError。

    为什么 fail-closed：4:3 / 3:2 这些比例在网页模板里没有对应的安全区与版式，静默接受会渲出
    "文字贴边、构图塌掉"的片子；而 render.py 的 --engine pil 路线也只认这两套画幅（竖屏要配
    --layout portrait，见 SKILL.md §7.5.1），两边不一致时最容易出现"预览好好的、出片是竖的"。
    所以上游拒绝之外，这里再拦一道。
    """
    try:
        w = int(width)
        h = int(height)
    except (TypeError, ValueError):
        raise ValueError("画布尺寸必须是整数，收到 %r x %r" % (width, height)) from None
    if w <= 0 or h <= 0:
        raise ValueError("画布尺寸必须是正数，收到 %dx%d" % (w, h))
    if w < _MIN_CANVAS[0] or h < _MIN_CANVAS[1]:
        raise ValueError("画布太小：%dx%d（下限 %dx%d）" % (w, h, _MIN_CANVAS[0], _MIN_CANVAS[1]))
    # 画布比例：横屏 16:9 或竖屏 9:16（与 layoutkit 的两套 profile 一致）。
    # 其它比例仍然拒绝——web 页面由使用者自己写，但安全区/版式只做了这两套。
    ratio = w / float(h)
    ok_ratio = (abs(ratio - 16.0 / 9.0) <= _ASPECT_TOL
                or abs(ratio - 9.0 / 16.0) <= _ASPECT_TOL)
    if w % 2 or h % 2:
        raise ValueError(
            "画布宽高必须是偶数（H.264 yuv420p 的子采样要求），收到 %dx%d。"
            "改成 %dx%d 即可。" % (w, h, w + (w % 2), h + (h % 2)))
    if not ok_ratio:
        raise ValueError(
            "画布只支持 16:9 横屏或 9:16 竖屏；%dx%d 的宽高比是 %.4f:1。"
            "常见合法尺寸：横屏 1920x1080 / 1280x720，竖屏 1080x1920 / 720x1280。"
            % (w, h, ratio))
    return w, h


def _hex_color(value, where):
    """把颜色统一成 "#rrggbb"（小程序/网页都认）。支持 (r,g,b) 元组与 "#rrggbb"/"#rgb"。

    非法值直接 raise：颜色写错在画布上表现为"看不见字"，靠肉眼发现的成本远高于这里报错。
    """
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        try:
            rgb = [int(value[0]), int(value[1]), int(value[2])]
        except (TypeError, ValueError):
            raise ValueError("%s 的颜色不是三个整数：%r" % (where, value)) from None
        if min(rgb) < 0 or max(rgb) > 255:
            raise ValueError("%s 的颜色分量必须在 0–255：%r" % (where, value))
        return "#%02x%02x%02x" % (rgb[0], rgb[1], rgb[2])
    if isinstance(value, str):
        s = value.strip()
        if re.fullmatch(r"#[0-9a-fA-F]{6}", s):
            return s.lower()
        if re.fullmatch(r"#[0-9a-fA-F]{3}", s):
            return "#" + "".join(ch * 2 for ch in s[1:].lower())
    raise ValueError("%s 必须是 (r,g,b) 或 \"#rrggbb\"，收到 %r" % (where, value))


def _luma(color_hex):
    """相对亮度（0–1，sRGB 线性化前的近似即可）：只用来判断"深色/浅色主题"。"""
    s = str(color_hex).lstrip("#")
    r, g, b = (int(s[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _style_theme_dict(style):
    """从两种合法写法里取出「主题 dict」，并返回 (theme, plan_extra)。

    plan_extra 只在写法①里非空（style / style_name / legacy / layout_default / chapter_layout）。
    """
    if not isinstance(style, dict):
        raise TypeError("style 必须是 dict（stylekit 的 style plan 或裸主题 dict）或 None，收到 %r"
                        % (type(style).__name__,))
    theme = style.get("theme")
    if isinstance(theme, dict):
        extra = {
            "style": style.get("style"),
            "style_name": style.get("style_name"),
            "legacy": style.get("legacy"),
            "layout_default": style.get("layout_default"),
            "chapter_layout": style.get("chapter_layout"),
        }
        return theme, extra
    # 写法②：裸主题 dict（必须有 id/bg 之类的字段）
    if "bg" in style or "ink" in style:
        return style, {"style": style.get("id"), "style_name": style.get("label"),
                       "legacy": None, "layout_default": None, "chapter_layout": None}
    raise ValueError(
        "style 既不是 style plan（没有 \"theme\" 字典）也不是裸主题 dict（没有 bg / ink）。"
        "可用写法：已解析的计划（work/style.json）或裸主题 dict。收到键：%s"
        % (sorted(style)[:12],))


def normalize_style(style, *, width=W, height=H, fps=None):
    """把 style 归一成注入用 payload；style=None 返回 None（= 完全不注入）。

    payload 结构（window.__STYLE__，字段名即契约；改动要同步 assets/web/*.js）：
        version / style / style_name / theme_id / theme_label / legacy / dark
        canvas      {w, h, aspect, fps}      fps=None 时写 null（页面保留自己的 window.FPS）
        theme       {bg,bg2,surface,ink,muted,dim,accent,accent2,good,bad,rule}  # "#rrggbb"
        typography  {family, font_stack, scale, tracking, line_gap}
        layout      {default, chapter}
        card        {radius, fill_a, outline_a, pad, h, gap}
        material    {vignette, grain, grain_freq, texture, texture_alpha, vignette_tint}
        motion      {ease, entrance_dx, entrance_dy, push, item_stagger, transition}
        hud         "chip-left" | "corner-line" | "none"
        safe        {bias}
    """
    if style is None:
        return None
    w, h = _validate_canvas(width, height)
    theme, extra = _style_theme_dict(style)

    colors = {}
    for key in THEME_COLOR_KEYS:
        if key not in theme:
            raise ValueError("style 的主题缺少颜色 %r（需要齐全：%s）"
                             % (key, ", ".join(THEME_COLOR_KEYS)))
        colors[key] = _hex_color(theme[key], "theme.%s" % key)

    family = str(theme.get("family") or "sans")
    if family not in FONT_STACKS:
        raise ValueError("style 的字体族 %r 不认识；可用：%s（与 filmkit.FONT_FAMILIES 对齐）"
                         % (family, " / ".join(sorted(FONT_STACKS))))

    layout_default = extra.get("layout_default") or theme.get("layout") or "left-stack"
    chapter_layout = extra.get("chapter_layout") if isinstance(extra.get("chapter_layout"), dict) else {}

    def _num(v, default):
        try:
            return float(v) if v is not None else float(default)
        except (TypeError, ValueError):
            raise ValueError("style 字段 %r 不是数字：%r" % (v, v)) from None

    scale = {}
    raw_scale = theme.get("scale") if isinstance(theme.get("scale"), dict) else {}
    for k, v in raw_scale.items():
        if isinstance(v, (list, tuple)) and len(v) >= 2:
            scale[str(k)] = [int(v[0]), str(v[1])]
        elif isinstance(v, (int, float)):
            scale[str(k)] = [int(v), "reg"]
        else:
            raise ValueError("style.typography.scale.%s 必须是 (字号, 字重)，收到 %r" % (k, v))

    card = {}
    raw_card = theme.get("card") if isinstance(theme.get("card"), dict) else {}
    for k, v in raw_card.items():
        if isinstance(v, (int, float)):
            card[str(k)] = float(v) if isinstance(v, float) and not float(v).is_integer() else int(v)
        else:
            raise ValueError("style.layout.card.%s 必须是数字，收到 %r" % (k, v))

    qa = theme.get("qa") if isinstance(theme.get("qa"), dict) else {}
    if "dark" in qa:
        dark = bool(qa["dark"])
    else:
        dark = _luma(colors["bg"]) < 0.5

    fps_val = None
    if fps is not None:
        try:
            fps_val = float(fps)
        except (TypeError, ValueError):
            raise ValueError("fps 必须是数字，收到 %r" % (fps,)) from None
        if not math.isfinite(fps_val) or fps_val <= 0:
            raise ValueError("fps 必须是正数，收到 %r" % (fps,))

    hud = str(theme.get("hud") or "none")
    if hud not in ("chip-left", "corner-line", "none"):
        raise ValueError("style 的 hud 装帧 %r 不认识；可用：chip-left / corner-line / none" % (hud,))

    payload = {
        "version": STYLE_VERSION,
        "style": extra.get("style") or theme.get("id"),
        "style_name": extra.get("style_name") or theme.get("label") or "",
        "theme_id": theme.get("id") or extra.get("style") or "",
        "theme_label": theme.get("label") or extra.get("style_name") or "",
        "legacy": bool(extra.get("legacy")) if extra.get("legacy") is not None else False,
        "dark": bool(dark),
        # aspect 按真实画布写，别恒写 "16:9"：竖屏片（1080x1920）的载荷里写着 16:9
        # 会让下游按错比例算安全区，且这种不一致在 payload 里是"看得见的假话"。
        "canvas": {"w": w, "h": h, "aspect": "9:16" if h > w else "16:9", "fps": fps_val},
        "theme": colors,
        "typography": {
            "family": family,
            "font_stack": FONT_STACKS[family],
            "scale": scale,
            "tracking": _num(theme.get("tracking"), 0.0),
            "line_gap": _num(theme.get("line_gap"), 1.30),
        },
        "layout": {"default": str(layout_default),
                   "chapter": {str(k): str(v) for k, v in chapter_layout.items()}},
        "card": card,
        "material": {
            "vignette": _num(theme.get("vignette"), 0.0),
            "grain": _num(theme.get("grain"), 0.0),
            "grain_freq": _num(theme.get("grain_freq"), 0.0),
            "texture": str(theme.get("texture") or "none"),
            "texture_alpha": _num(theme.get("texture_alpha"), 0.0),
            "vignette_tint": _hex_color(theme.get("vignette_tint", (0, 0, 0)), "material.vignette_tint")
            if "vignette_tint" in theme else "#000000",
        },
        "motion": {
            "ease": str(theme.get("ease") or "out3"),
            "entrance_dx": int(_num(theme.get("entrance_dx"), 0)),
            "entrance_dy": int(_num(theme.get("entrance_dy"), 0)),
            "push": _num(theme.get("push"), 0.0),
            "item_stagger": _num(theme.get("item_stagger"), 0.0),
            "transition": str(theme.get("transition") or "black"),
        },
        "hud": hud,
        "safe": {"bias": int(_num(theme.get("safe_bias"), 0))},
    }
    return payload


def style_sha256(payload) -> str:
    """注入 payload 的稳定指纹（sort_keys + 紧凑分隔符）：同 style 同尺寸 → 同指纹。

    用途：① 日志里一眼看出"这次渲的是不是同一套画风"；② 分块目录名后缀——换了 style
    就不能复用旧块（旧块是旧画风，混进成片就是花片）。
    """
    blob = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _style_js(payload) -> str:
    """生成 add_init_script 的脚本体：**纯 JSON 赋值**，页面里不做任何派生计算。"""
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "window.__STYLE__ = %s;" % blob


# ---------------------------------------------------------------- 页面 / URL
def _to_url(html_path) -> str:
    """把本地 HTML 路径转成 file:// URI；已经是 URL 的原样返回。"""
    s = str(html_path)
    if _SCHEME_RE.match(s):
        return s
    p = Path(s).expanduser()
    if not p.is_absolute():
        p = Path.cwd() / p
    p = p.resolve()
    if not p.is_file():
        raise FileNotFoundError("页面不存在: %s" % p)
    return p.as_uri()


def _playwright():
    try:
        from playwright.async_api import async_playwright
    except Exception as exc:  # pragma: no cover - 环境缺依赖时给出可操作提示
        raise RuntimeError(
            "需要 playwright：pip install playwright 且 python -m playwright install chromium"
        ) from exc
    return async_playwright


def _console_log(msg) -> None:
    try:
        if msg.type in ("error", "warning"):
            print("[页面控制台] %s: %s" % (msg.type, msg.text), flush=True)
    except Exception:
        pass


def _use_deterministic(flag=None) -> bool:
    """确定性开关：显式参数优先，其次环境变量 WEBENGINE_DETERMINISTIC（1/true/yes/on）。

    注意 flag 的三态语义：None = 没表态（去读环境变量），True/False = 调用方明确要求。
    CLI 的 --deterministic 必须用 default=None（store_const）而不是 store_true：后者在
    "没传" 时给出 False，会把 WEBENGINE_DETERMINISTIC=1 当成"调用方明确要求关掉"，
    环境变量整条失效（命令行实测过，库调用路径却生效——两条路径行为不一致）。
    """
    if flag is not None:
        return bool(flag)
    return str(os.environ.get("WEBENGINE_DETERMINISTIC") or "").strip().lower() in (
        "1", "true", "yes", "on", "y", "t")


def browser_args(deterministic=None) -> list:
    """浏览器启动参数；deterministic=True 时追加软件光栅参数（跨会话字节可复现）。"""
    args = list(BROWSER_ARGS)
    if _use_deterministic(deterministic):
        args += DETERMINISTIC_ARGS
    return args


async def _page_contract(page) -> dict:
    """读页面的接口契约（都是 cheap 的同步求值），用于给出可操作的报错。"""
    try:
        return await page.evaluate(
            "() => ({ready: window.ready === true,"
            " renderFrame: typeof window.renderFrame,"
            " duration: (typeof window.DURATION === 'number' ? window.DURATION : null),"
            " fps: (typeof window.FPS === 'number' ? window.FPS : null),"
            " getCues: typeof window.getCues,"
            " getEvents: typeof window.getEvents,"
            " qaText: typeof window.qaText,"
            " hasFK: typeof window.FK === 'object' && window.FK !== null})")
    except Exception as exc:            # noqa: BLE001
        return {"_error": "%s: %s" % (type(exc).__name__, str(exc)[:200])}


def _format_contract(contract: dict) -> str:
    if contract.get("_error"):
        return "读页面契约失败（%s）" % contract["_error"]
    return ("renderFrame=%s, DURATION=%s, FPS=%s, getCues=%s, getEvents=%s, qaText=%s, FK=%s"
            % (contract.get("renderFrame"), contract.get("duration"), contract.get("fps"),
               contract.get("getCues"), contract.get("getEvents"), contract.get("qaText"),
               "有" if contract.get("hasFK") else "无"))


async def page_number(page, key: str, *, default=None) -> float:
    """读页面上的数值契约（window.DURATION / window.FPS），缺了或不是正数就给可操作报错。

    直接 float(await page.evaluate("window.DURATION")) 不够：页面忘了导出 DURATION
    时得到的是 None，报错只剩一句 "float() argument must be ... 'NoneType'"，看不出
    是页面契约缺字段（实测 page_meta / export_cues 都会这样）。
    default 传 None 表示必填（DURATION）；传数字表示缺省值（FPS 缺省口径是 30）。
    """
    raw = await page.evaluate("k => window[k]", key)
    if raw is None:
        if default is not None:
            return float(default)
        raise RuntimeError(
            "页面没有导出 window.%s（页面契约见 references/web-engine.md §3）。"
            "index.html 里在 film.js 之前写 window.WINDOW_DURATION = 秒数，"
            "或让模板按场景表自动推导（assets/web/film.js 的 resolveDuration）。" % key)
    try:
        val = float(raw)
    except (TypeError, ValueError):
        raise RuntimeError("页面 window.%s 不是数字：%r（必须是数字）" % (key, raw)) from None
    if not math.isfinite(val) or val <= 0:
        raise RuntimeError("页面 window.%s 非法：%r（必须是正数）" % (key, raw))
    return val


async def open_page(pw, url, width=W, height=H, log=False, deterministic=None,
                    ready_timeout=300_000, style=None, fps=None):
    """打开页面并等到 window.ready === true，返回 (browser, page, cdp)。

    浏览器优先 Microsoft Edge（channel="msedge"），启动失败回落 Playwright 自带的 Chromium；
    视口按 width x height（devicePixelRatio=1），截图与画布 1:1 像素。

    两道**可操作**的契约检查（否则缺 window.ready / renderFrame 时只会抛 Playwright 的
    裸超时异常，看不出是页面脚本挂了还是没调 FK.boot()）：
      1. 等到 window.ready === true 超时 → 报错里带上页面异常/控制台错误和契约快照；
      2. window.renderFrame 不是函数 → 直接报错（不要靠逐帧重试 5 次才发现）。
    确定性：deterministic=True（或环境变量 WEBENGINE_DETERMINISTIC=1）走软件光栅，
    跨浏览器会话 byte 级可复现；缺省走 GPU，快约 22%。

    风格注入：style 非 None 时在 page.goto **之前** add_init_script 注入 window.__STYLE__；
    style=None 时**不调用 add_init_script**，页面里不存在 __STYLE__（默认外观逐字节不变）。
    width/height 一律先过 _validate_canvas（16:9 横屏 / 9:16 竖屏且偶数，否则 raise）；
    fps 只用于写进
    payload（None = 不让 payload 约束帧率，页面 window.FPS 才是真值）。
    """
    global _BROWSER_KIND
    width, height = _validate_canvas(width, height)
    payload = normalize_style(style, width=width, height=height, fps=fps) if style is not None else None
    browser = None
    args = browser_args(deterministic)
    try:
        browser = await pw.chromium.launch(channel="msedge", headless=True, args=args)
        _BROWSER_KIND = "msedge"
        if log:
            print("[浏览器] Microsoft Edge（channel=msedge）", flush=True)
    except Exception as exc:
        if log:
            print("[浏览器] Edge 启动失败（%s），回落到 Playwright Chromium" % type(exc).__name__, flush=True)
        browser = await pw.chromium.launch(headless=True, args=args)
        _BROWSER_KIND = "chromium"
        if log:
            print("[浏览器] Playwright Chromium", flush=True)
    try:
        page = await browser.new_page(viewport={"width": int(width), "height": int(height)},
                                      device_scale_factor=1)
    except BaseException:
        # 浏览器已经启动成功、new_page 才失败（内存不足 / 浏览器刚崩 / 任务被取消）时，
        # 不关就会留下一个没有页面、也没人引用的 chromium 进程。render_full 的单帧重试最多
        # 5 次 × N 个 worker，密集失败时这些进程会一直堆着（任务管理器里一排 chromium），
        # 所以这里和下面的 goto/等 ready 一样，失败路径统一关浏览器。
        try:
            await browser.close()
        except Exception:
            pass
        raise
    page_errors = []

    def _on_pageerror(exc):
        page_errors.append(str(exc)[:200])
        print("[页面异常] %s" % exc, flush=True)

    def _on_console(msg):
        try:
            if msg.type in ("error", "warning"):
                page_errors.append("%s: %s" % (msg.type, msg.text[:200]))
        except Exception:               # noqa: BLE001
            pass
        if log:
            _console_log(msg)

    page.on("console", _on_console)
    page.on("pageerror", _on_pageerror)
    try:
        if payload is not None:
            # 必须在 goto 之前：engine.js/film.js 是同步 IIFE，加载时就要读到 __STYLE__
            await page.add_init_script(script=_style_js(payload))
            if log:
                print("[样式] %s / %s / %dx%d / 字体族=%s / sha=%s"
                      % (payload.get("style") or payload.get("theme_id"),
                         payload.get("style_name") or payload.get("theme_label"),
                         payload["canvas"]["w"], payload["canvas"]["h"],
                         payload["typography"]["family"], style_sha256(payload)[:12]),
                      flush=True)
        await page.goto(url, wait_until="domcontentloaded", timeout=180_000)
        try:
            await page.wait_for_function("window.ready === true", timeout=int(ready_timeout),
                                         polling=250)
        except Exception as exc:        # noqa: BLE001
            contract = await _page_contract(page)
            # 页面自己捕获的启动异常（示例页 index.html 的 boot().catch 会写到这个全局）：
            # 它比"window.ready 没变 true"更接近根因——异步 reject 不一定能进 pageerror。
            boot_err = ""
            try:
                boot_err = str(await page.evaluate(
                    "() => window.__BOOT_ERROR__ ? String(window.__BOOT_ERROR__) : ''") or "")
            except Exception:                                     # noqa: BLE001
                boot_err = ""
            detail = ("；页面异常/控制台错误：" + " | ".join(page_errors[:5])) if page_errors else ""
            if boot_err:
                detail += "；boot() 抛出的异常：%s" % boot_err.splitlines()[0][:300]
            raise RuntimeError(
                "页面 %s 在 %d s 内没有把 window.ready 置 true（%s）。契约快照：%s%s。"
                "排查：① 页面脚本是否报错（上面若有 [页面异常] 就是它）；"
                "② index.html 末尾是否调用了 window.FK.boot()；"
                "③ boot 里 document.fonts.ready 是否卡住。"
                % (url, int(ready_timeout) // 1000, type(exc).__name__,
                   _format_contract(contract), detail)
            ) from exc
        contract = await _page_contract(page)
        if contract.get("renderFrame") != "function":
            raise RuntimeError(
                "页面 %s 缺少 window.renderFrame(f)。契约快照：%s。"
                "逐帧渲染必须导出 renderFrame：engine.js 加载后应挂到 window 上，"
                "页面脚本顺序为 engine.js → film.js → qa_text.js。"
                % (url, _format_contract(contract))
            )
        if payload is not None:
            # 注入的契约自检：payload 说画幅（16:9 或 9:16）+ w/h，页面就必须真的按它建画布；
            # 页面没照做（老页面不认 __STYLE__）要当场报错，不能拿"截图能出图"当通过。
            got = await page.evaluate(
                "() => ({hasStyle: typeof window.__STYLE__ === 'object' && window.__STYLE__ !== null,"
                " applied: !!(window.FK && window.FK.style),"
                " w: window.FK ? window.FK.W : null, h: window.FK ? window.FK.H : null,"
                " fps: window.FPS})")
            want_w, want_h = payload["canvas"]["w"], payload["canvas"]["h"]
            # 两个条件缺一不可：__STYLE__ 在（注入成功）+ engine.js 真的读了它（FK.style 非空）。
            # 只查前者会漏掉"老 engine.js 静默忽略 __STYLE__、照样渲成深蓝橙"这种假通过。
            if not got.get("hasStyle") or not got.get("applied"):
                raise RuntimeError(
                    "页面 %s 的 engine.js 不认 window.__STYLE__（FK.style 为空；注入后画风不会变）。"
                    "风格注入要新版 engine.js 才认；把新版 assets/web/ 一起拷过去，"
                    "或确认页面脚本顺序是 engine.js → film.js → qa_text.js。" % url)
            if got.get("w") != want_w or got.get("h") != want_h:
                raise RuntimeError(
                    "页面画布 %sx%s 与注入的 %dx%d 不一致：页面没按 __STYLE__.canvas 建画布"
                    "（见 assets/web/engine.js 的 ST_CANVAS 段）。" % (got.get("w"), got.get("h"), want_w, want_h))
            if payload["canvas"]["fps"] is not None:
                page_fps = float(got.get("fps") or 0)
                if abs(page_fps - float(payload["canvas"]["fps"])) > 1e-6:
                    raise RuntimeError(
                        "页面 window.FPS=%s 与注入的 __STYLE__.canvas.fps=%s 不一致："
                        "帧率必须来自同一份 timeline，不能靠 style 猜。"
                        % (page_fps, payload["canvas"]["fps"]))
        # 截图 = 视口（device_scale_factor=1），所以页面画布的可视区域必须正好铺满视口。
        # 不查这一条的话，style=None + --width 1280 --height 720 会"成功"输出 1280x720：
        # 页面画布仍是 1920x1080（没有 __STYLE__ 就没人改它），截出来的是原画左上角一块
        # ——实测与 1920 帧左上角的 1280x720 裁切逐像素完全相同（422 KB PNG，0 像素差异），
        # 标题 / HUD / 时间码全被裁掉，而 CLI 照旧 exit 0。画布比视口小则是四边留黑边，
        # 同样"出片成功但画面是错的"，一并拦。
        box = await page.evaluate(
            "() => { var cs = document.querySelectorAll('canvas');"
            " if (!cs.length) return null;"
            " var l = 1e9, t = 1e9, r = -1e9, b = -1e9;"
            " for (var i = 0; i < cs.length; i++) {"
            "  var q = cs[i].getBoundingClientRect();"
            "  if (!(q.width > 0) || !(q.height > 0)) continue;"
            "  l = Math.min(l, q.left); t = Math.min(t, q.top);"
            "  r = Math.max(r, q.right); b = Math.max(b, q.bottom); }"
            " return (r > l) ? [l, t, r - l, b - t] : null; }")
        if box is None:
            raise ValueError(
                "页面 %s 里没有可截图的 <canvas>（页面契约要求 <canvas id='stage'>）。" % url)
        left, top, cw, ch = (float(v) for v in box)
        if (abs(left) > 1.5 or abs(top) > 1.5
                or abs(cw - float(width)) > 1.5 or abs(ch - float(height)) > 1.5):
            raise ValueError(
                "页面画布的可视区域是 x=%g y=%g w=%g h=%g（CSS 像素），与截图视口 %dx%d 不一致。"
                "截图尺寸就是视口（devicePixelRatio=1）：画布比视口大会被裁掉、比视口小会留黑边，"
                "两种都是「出片成功但画面是错的」。模板页面不注入 style 时画布固定 1920x1080 —— "
                "要换输出尺寸请用 --style 注入 __STYLE__.canvas（engine.js 会跟着改画布），"
                "或把页面画布与 CSS 尺寸一起改成目标尺寸；legacy 项目（不注入 style）"
                "请按 1920x1080 渲染，再由上游下采样到交付规格。"
                % (left, top, cw, ch, int(width), int(height)))
    except BaseException:
        # 用 BaseException 而不是 Exception：worker 被取消（task.cancel）抛的是
        # CancelledError，它不继承 Exception —— 用 Exception 会把刚启动的浏览器丢下不管。
        try:
            await browser.close()
        except Exception:
            pass
        raise
    try:
        cdp = await page.context.new_cdp_session(page)
    except BaseException:
        try:
            await browser.close()
        except Exception:
            pass
        raise
    return browser, page, cdp


async def _grab(page, cdp, f: int) -> bytes:
    """把时间轴精确跳到第 f 帧并截图（PNG）。"""
    await page.evaluate("f => window.renderFrame(f)", int(f))
    reply = await cdp.send("Page.captureScreenshot", {"format": "png", "optimizeForSpeed": True})
    return base64.b64decode(reply["data"])


async def _page_meta_async(html_path, deterministic=None, style=None, width=W, height=H, fps=None):
    url = _to_url(html_path)
    async with _playwright()() as pw:
        browser, page, _cdp = await open_page(pw, url, width, height, deterministic=deterministic,
                                             style=style, fps=fps)
        try:
            duration = await page_number(page, "DURATION")
            got_fps = await page_number(page, "FPS", default=FPS)
            return duration, got_fps
        finally:
            try:
                await browser.close()
            except Exception:
                pass


def page_meta(html_path, *, style=None, width=W, height=H, fps=None):
    """打开一次页面，读回 (DURATION, FPS)。

    style 非 None 时按注入后的页面读（注入可能改画布尺寸，帧率仍以页面为准，
    除非调用方显式传 fps 并在 payload 里断言）。
    """
    _setup_stdout()
    return asyncio.run(_page_meta_async(html_path, style=style, width=width, height=height, fps=fps))


# ---------------------------------------------------------------- snap
def snap(html_path, times, out_dir, tag="", deterministic=None, *, style=None,
         width=W, height=H, fps=None) -> list:
    """截取指定时刻的静帧（PNG）；多帧时额外拼一张联系表。返回帧 PNG 路径列表。

    style/width/height/fps 透传给 open_page（缺省 = 1920x1080、不注入）。
    """
    _setup_stdout()
    tlist = [float(t) for t in times]
    if not tlist:
        raise ValueError("snap 至少需要 1 个时刻（times 为空）")
    out = Path(out_dir).expanduser()
    if not out.is_absolute():
        out = (Path.cwd() / out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    return asyncio.run(_snap_async(html_path, tlist, out, str(tag), deterministic,
                                   style=style, width=width, height=height, fps=fps))


async def _snap_async(html_path, times, out: Path, tag: str, deterministic=None, *,
                      style=None, width=W, height=H, fps=None) -> list:
    url = _to_url(html_path)
    paths = []
    async with _playwright()() as pw:
        browser, page, cdp = await open_page(pw, url, width, height, deterministic=deterministic,
                                             style=style, fps=fps)
        try:
            fps = await page_number(page, "FPS", default=FPS)
            for t in times:
                png = await _grab(page, cdp, int(round(t * fps)))
                path = out / ("%st%07.2f.png" % (tag, t))
                path.write_bytes(png)
                paths.append(path)
                print("[snap] %6.2fs → %s" % (t, path), flush=True)
        finally:
            try:
                await browser.close()
            except Exception:
                pass
    if len(paths) >= 2:
        sheet = out / ("%ssheet_%.1f-%.1f.png" % (tag, times[0], times[-1]))
        _K.contact_sheet([str(p) for p in paths], str(sheet), cols=4, thumb_w=480)
        print("[联系表] %s" % sheet.resolve(), flush=True)
    return paths


# ---------------------------------------------------------------- cues / events 导出
def _finite_t(value, kind: str, label) -> float:
    """cue / event 的时间必须是有限秒数，返回 round(x, 3)。

    为什么单拎出来：NaN / Infinity 在 Python 的 json.dumps 里默认被写成字面量 NaN /
    Infinity，那不是合法 JSON（RFC 8259），而 Python 侧 json.load 又会照单全收 —— 于是
    坏时间一路流到配乐 / 音效 / 同步门禁，表现成"某条提示点没响""采样索引对不上"这种
    和根因隔了三层的现象。导出时就报错，带上 kind / type 便于定位是哪条。
    """
    try:
        t = float(value)
    except (TypeError, ValueError):
        raise ValueError("%s 的 t 不是数字：%r（%r）" % (kind, value, label)) from None
    if not math.isfinite(t):
        raise ValueError("%s 的 t 不是有限秒数：%r（%r）—— NaN / Infinity 不是合法 JSON，"
                         "写进 work/cues.json 后下游时间轴会静默错位" % (kind, value, label))
    return round(t, 3)


def _norm_cue_list(raw, src: str) -> list:
    out = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        cue = dict(item)
        if "t" not in cue:
            raise ValueError("cue 缺 t 字段: %r" % (item,))
        if not cue.get("type"):
            raise ValueError("cue 缺 type 字段: %r" % (item,))
        cue["t"] = _finite_t(cue["t"], "cue", cue.get("type"))
        cue.setdefault("src", src)
        out.append(cue)
    out.sort(key=lambda c: c["t"])
    return out


def _norm_events(raw) -> list:
    """规整页面导出的视觉事件；统一打 src="web" 标记。

    为什么打标记：render.py 在 engine=web 时会把 film.json / scenes.py 的 cue 与页面 cue
    **三路合并**（三路都是合法的登记方式），但 scenes.py 的 cue 是按 PIL 时间轴算的，
    与页面动画时间基准不同。qa_frames 的 sync_check 靠 src 判断"能不能配"，
    否则会拿 29s PIL 时间轴上的 ui_blip 去配 12s 页面上的 ui_blip，报出假 FAIL。
    """
    out = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        ev = dict(item)
        if "t" not in ev:
            continue
        ev["t"] = _finite_t(ev["t"], "event", ev.get("kind"))
        ev.setdefault("kind", "misc")
        ev.setdefault("src", "web")
        out.append(ev)
    out.sort(key=lambda e: e["t"])
    return out


def export_cues(html_path, deterministic=None, *, style=None, width=W, height=H, fps=None) -> dict:
    """导出页面登记的 cue / 视觉事件，返回可直接写 work/cues.json 的字典（契约 §4.2）。

    width/height 写进返回头（打完 style 注入后画布可能不是 1080p），其余照旧。
    """
    _setup_stdout()
    return asyncio.run(_export_cues_async(html_path, deterministic,
                                          style=style, width=width, height=height, fps=fps))


async def _export_cues_async(html_path, deterministic=None, *, style=None,
                             width=W, height=H, fps=None) -> dict:
    url = _to_url(html_path)
    width, height = _validate_canvas(width, height)
    async with _playwright()() as pw:
        browser, page, _cdp = await open_page(pw, url, width, height, deterministic=deterministic,
                                              style=style, fps=fps)
        try:
            duration = await page_number(page, "DURATION")
            fps = await page_number(page, "FPS", default=FPS)
            # 页面没导出 getCues/getEvents 时，`? : []` 兜底会静默返回空数组：cues.json 里
            # 0 条提示点，配乐/音效阶段照样跑完，只是整片悄悄变哑、音画同步也没有可配的事件
            # ——「静默丢一整个约定」比报错难查得多。engine.js 本身就导出这两个函数（没有提示
            # 点时返回空数组也算导出），所以 fail-closed 不会误伤正常页面。
            contract = await _page_contract(page)
            missing = [k for k in ("getCues", "getEvents") if contract.get(k) != "function"]
            if missing:
                raise RuntimeError(
                    "页面 %s 没有导出 window.%s（导出 cue / 视觉事件要求页面契约见 "
                    "references/web-engine.md §3）。assets/web/engine.js 自带这两个函数；"
                    "自定义页面请补上（本片确实没有提示点时也要显式导出空数组）："
                    "window.getCues = function () { return []; }; "
                    "window.getEvents = function () { return []; }; 契约快照：%s"
                    % (url, " / ".join(missing), _format_contract(contract)))
            cues = await page.evaluate("window.getCues()")
            events = await page.evaluate("window.getEvents()")
            # 返回的不是数组时 _norm_cue_list/_norm_events 会把它当"没有条目"逐项跳过 →
            # 依旧是静默 0 条，所以数组类型也要在这里拦下。
            for _name, _val in (("getCues", cues), ("getEvents", events)):
                if not isinstance(_val, list):
                    raise RuntimeError(
                        "页面 window.%s() 返回的不是数组（收到 %s）：cue / 事件必须是数组，"
                        "契约见 references/web-engine.md §3。"
                        % (_name, type(_val).__name__))
        finally:
            try:
                await browser.close()
            except Exception:
                pass
    return {
        "version": 1,
        "duration": round(duration, 3),
        "fps": fps,
        "width": width,
        "height": height,
        "cues": _norm_cue_list(cues, "web"),
        "events": _norm_events(events),
    }


# ---------------------------------------------------------------- 分块 / 编码
def _encoder_cmd(dst, fps: float, crf: int = 17, preset: str = "slow") -> list:
    """image2pipe(PNG) → libx264；显式 -r FPS、-g 2*FPS（关键帧密集，便于逐段检视）。"""
    return [
        str(FFMPEG), "-y", "-loglevel", "error",
        "-f", "image2pipe", "-c:v", "png", "-framerate", str(fps), "-i", "-",
        "-c:v", "libx264", "-preset", str(preset), "-crf", str(crf),
        "-pix_fmt", "yuv420p",
        "-r", str(fps),
        "-g", str(int(round(2 * fps))),
        "-bf", "2", "-threads", "2", "-movflags", "+faststart",
        str(dst),
    ]


def _chunk_dir(out_mp4, from_s: float, to_s: float, token: str = "") -> Path:
    """块目录：<stem>_<from>-<to>[_<token>]。

    token 只在"画布尺寸不是 1920x1080 或注入了 style"时非空——不同画风/尺寸的块不能混用，
    否则断点续跑会把旧画风的块拼进新片（花片）。token 为空时目录名保持不变，续跑才能命中。
    """
    out = Path(out_mp4)
    name = "%s_%g-%g" % (out.stem, float(from_s), float(to_s))
    if token:
        name += "_" + token
    return out.parent / "chunks" / name


def _style_token(payload, width: int, height: int) -> str:
    """分块目录的风格后缀；默认尺寸 + 无 style 时返回空串（保持既有目录名，续跑才能命中）。"""
    if payload is None and (int(width), int(height)) == (W, H):
        return ""
    parts = ["w%dh%d" % (int(width), int(height))]
    if payload is not None:
        parts.append("s" + style_sha256(payload)[:12])
    return "_".join(parts)


def _plan_chunks(chunk_dir, f0: int, f1: int, step: int) -> list:
    """按「绝对起始帧 // 每块帧数」命名块：c000.mp4 = 第 0 帧起、c001.mp4 = 第 step 帧起……"""
    step = max(1, int(step))
    chunks = []
    for a in range(int(f0), int(f1), step):
        b = min(int(f1), a + step)
        chunks.append((a, b, Path(chunk_dir) / ("c%03d.mp4" % (a // step))))
    return chunks


def _check_gaps(chunks) -> None:
    """拼接前显式缺口检测：缺块直接 raise，拒绝「少一段也出片」。"""
    missing = [Path(p).name for _a, _b, p in chunks if not Path(p).is_file()]
    if missing:
        shown = "、".join(missing[:8]) + ("…" if len(missing) > 8 else "")
        raise RuntimeError(
            "分块缺口：缺 %d 个块（%s）。拒绝拼出少一段的片子；重跑 --resume 补齐，"
            "或删掉块目录整段重渲。" % (len(missing), shown)
        )


def _probe_media(path):
    try:
        return _K.probe(str(path))
    except Exception as exc:
        return {"_error": "%s: %s" % (type(exc).__name__, exc)}


def _probe_duration(path):
    info = _probe_media(path)
    try:
        d = info.get("format", {}).get("duration")
        return float(d) if d is not None else None
    except Exception:
        return None


def _check_chunk_durations(chunks, fps: float) -> None:
    """块时长校验：块损坏 / 旧参数残留都会在拼接前被拦下。

    探测不出时长的块（空文件 / 非视频 / ffprobe 失败）不许 continue 静默放过——那要等到
    concat 才发现；一并算异常："读不出时长"本身就说明块是坏的。
    """
    bad = []
    for f0, f1, path in chunks:
        want = (f1 - f0) / float(fps)
        got = _probe_duration(path)
        if got is None:
            bad.append("%s 探测不到时长（块损坏 / 空文件 / 不是视频）" % Path(path).name)
            continue
        if abs(got - want) > max(0.15, 1.5 / float(fps)):
            bad.append("%s 实际 %.3fs / 期望 %.3fs" % (Path(path).name, got, want))
    if bad:
        raise RuntimeError("分块时长异常（块损坏或旧参数渲染）：" + "；".join(bad[:6]))


# ---------------------------------------------------------------- 浏览器会话（崩溃重开）
class _Browser:
    """一个无头浏览器会话；卡住/崩溃时自动关掉重开（逐帧无状态，从同一帧继续即可）。"""

    def __init__(self, pw, url, width=W, height=1080, k=0, log=False, deterministic=None,
                 style=None, fps=None):
        self.pw = pw
        self.url = url
        self.width = width
        self.height = height
        self.k = k
        self.log = log
        self.deterministic = deterministic
        self.style = style
        self.fps = fps
        self.browser = None
        self.page = None
        self.cdp = None

    async def open(self):
        await self.close()
        self.browser, self.page, self.cdp = await open_page(
            self.pw, self.url, self.width, self.height, log=self.log,
            deterministic=self.deterministic, style=self.style, fps=self.fps,
        )

    async def close(self):
        if self.browser is not None:
            try:
                await asyncio.wait_for(self.browser.close(), 20)
            except Exception:
                pass
        self.browser = None
        self.page = None
        self.cdp = None

    async def frame(self, f: int) -> bytes:
        last = None
        for attempt in range(RETRY):
            try:
                if self.browser is None:
                    await self.open()
                return await asyncio.wait_for(_grab(self.page, self.cdp, f), FRAME_TIMEOUT)
            except Exception as exc:
                last = exc
                print(
                    "  worker %d: 第 %d 帧失败（%s: %s），重开浏览器重试 %d/%d"
                    % (self.k, f, type(exc).__name__, str(exc)[:120], attempt + 1, RETRY),
                    flush=True,
                )
                await self.close()
        raise RuntimeError("第 %d 帧连续失败 %d 次，放弃：%s" % (f, RETRY, last))


async def _write_chunk(br: _Browser, f0: int, f1: int, dst: Path, fps: float,
                       crf: int, preset: str, k: int, state: dict) -> None:
    """渲染一块：逐帧截图 → ffmpeg image2pipe → xxx.part.mp4 → rename xxx.mp4。"""
    dst = Path(dst)
    part = dst.with_name(dst.name[: -len(".mp4")] + PART_SUFFIX)
    if part.exists():
        part.unlink()
    # stderr 落临时文件，不用 PIPE：编码器一旦刷出超过管道缓冲（Windows 64 KB）的 stderr，
    # 没人读就会写阻塞；而我们这边 proc.stdin.write 是**同步**阻塞调用（不交出事件循环），
    # 两边互等就是死锁 —— asyncio.wait_for 也救不了它。临时文件没有缓冲上限，也不会堵。
    errf = tempfile.TemporaryFile()
    try:
        proc = subprocess.Popen(
            _encoder_cmd(part, fps, crf, preset),
            stdin=subprocess.PIPE,
            stderr=errf,
        )
    except BaseException:
        errf.close()
        raise
    try:
        for f in range(f0, f1):
            proc.stdin.write(await br.frame(f))
        proc.stdin.close()
        rc = proc.wait(timeout=300)
        errf.seek(0)
        err = errf.read().decode("utf-8", "replace")
    except BaseException:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=10)
        except Exception:
            pass
        if part.exists():
            try:
                part.unlink()
            except Exception:
                pass
        raise
    finally:
        try:
            errf.close()
        except Exception:
            pass
    if rc != 0:
        if part.exists():
            part.unlink()
        raise RuntimeError("ffmpeg 编码失败（%s，rc=%d）：%s" % (dst.name, rc, err[:600]))
    part.replace(dst)
    state["done"] += f1 - f0
    elapsed = time.perf_counter() - state["t0"]
    ms = elapsed / max(state["done"], 1) * 1000.0
    print(
        "  worker %d: 完成 %s 总进度 %d/%d (%.0f ms/帧)"
        % (k, dst.name, state["done"], state["total"], ms),
        flush=True,
    )


async def _worker(k: int, queue, state: dict, url: str, fps: float, crf: int, preset: str,
            deterministic=None, style=None, width=W, height=H) -> None:
    async with _playwright()() as pw:
        br = _Browser(pw, url, width, height, k=k, deterministic=deterministic,
                      style=style, fps=fps)
        try:
            while True:
                try:
                    f0, f1, dst = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                await _write_chunk(br, f0, f1, dst, fps, crf, preset, k, state)
        finally:
            await br.close()


# ---------------------------------------------------------------- render_full
def render_full(html_path, out_mp4, duration, *, workers=2, chunk_s=30.0,
                resume=True, from_s=0.0, to_s=None, crf=17, preset="slow",
                deterministic=None, style=None, width=W, height=H, fps=None) -> Path:
    """并行渲染整片（或一段）→ mp4。返回成片绝对路径。

    duration 传 None 时读页面 window.DURATION；传数字时与页面对不上会直接 raise。
    deterministic=True（或 WEBENGINE_DETERMINISTIC=1）→ 软件光栅，跨会话 byte 可复现。

    style 非 None 时整片跟随风格包（每个 worker 都在页面加载前注入同一份
    __STYLE__），块目录名会带尺寸 + style 指纹后缀，**换风格不会复用旧块**；
    width/height 只收 16:9 横屏 / 9:16 竖屏的偶数尺寸（_validate_canvas），fps 非 None
    时要求页面帧率一致。
    """
    _setup_stdout()
    return asyncio.run(_render_full_async(
        html_path, out_mp4, duration, workers=workers, chunk_s=chunk_s, resume=resume,
        from_s=from_s, to_s=to_s, crf=crf, preset=preset, deterministic=deterministic,
        style=style, width=width, height=height, fps=fps,
    ))


async def _render_full_async(html_path, out_mp4, duration, *, workers=2, chunk_s=30.0,
                             resume=True, from_s=0.0, to_s=None, crf=17, preset="slow",
                             deterministic=None, style=None, width=W, height=H, fps=None) -> Path:
    url = _to_url(html_path)
    out = Path(out_mp4).expanduser()
    if not out.is_absolute():
        out = (Path.cwd() / out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    width, height = _validate_canvas(width, height)
    payload = normalize_style(style, width=width, height=height, fps=fps) if style is not None else None

    # chunk_s <= 0 会被后面的 max(1, ...) 静默夹成「1 帧/块」：360 帧的 12 s 片会拆出
    # 360 个块，每块单独起一个 ffmpeg 进程再拼接，慢到没法用，而且日志里只看得出"块很多"，
    # 看不出是自己传错了参数。放在开浏览器之前校验，参数错了不用白等一次页面加载。
    chunk_s = float(chunk_s)
    if not math.isfinite(chunk_s) or chunk_s <= 0:
        raise ValueError("chunk_s 必须是正数秒（收到 %r）；以前 <=0 会被静默当成 1 帧/块"
                         % (chunk_s,))

    # 先问页面要长度与帧率：这是块切分与成片校验的唯一真值来源
    page_dur, fps_page = await _page_meta_async(url, deterministic=deterministic,
                                                style=style, width=width, height=height, fps=fps)
    if duration is None:
        duration = page_dur
    duration = float(duration)
    if not math.isfinite(duration) or duration <= 0:
        # NaN 会穿过下面的 abs(diff) > 0.05（NaN 比较恒为 False），最后在 int(round(nan))
        # 处炸成一句 "cannot convert float NaN to integer"，看不出是参数问题。
        raise ValueError("duration 必须是正数秒（收到 %r）；传 None 表示读页面 window.DURATION"
                         % (duration,))
    if abs(duration - page_dur) > 0.05:
        raise ValueError(
            "页面 window.DURATION=%.3f 与传入 duration=%.3f 不一致（先对齐剧本时长）"
            % (page_dur, duration)
        )
    fps = fps_page
    from_s = float(from_s)
    to_s = duration if to_s is None else float(to_s)
    if not (math.isfinite(from_s) and math.isfinite(to_s)):
        raise ValueError("渲染区间必须是有限秒数：from_s=%r to_s=%r" % (from_s, to_s))
    if from_s < 0 or to_s <= from_s or to_s > duration + 1e-6:
        raise ValueError("渲染区间非法：from_s=%.3f to_s=%.3f duration=%.3f" % (from_s, to_s, duration))

    f0 = int(round(from_s * fps))
    f1 = int(round(to_s * fps))
    step = max(1, int(round(chunk_s * fps)))
    chunk_dir = _chunk_dir(out, from_s, to_s, _style_token(payload, width, height))
    chunk_dir.mkdir(parents=True, exist_ok=True)
    chunks = _plan_chunks(chunk_dir, f0, f1, step)
    if not resume:
        for _a, _b, path in chunks:
            for victim in (path, path.with_name(path.name[: -len(".mp4")] + PART_SUFFIX)):
                if victim.exists():
                    victim.unlink()

    todo = [c for c in chunks if not Path(c[2]).is_file()]
    style_note = ""
    if payload is not None:
        style_note = "，style=%s（sha=%s）" % (payload.get("style") or payload.get("theme_id"),
                                              style_sha256(payload)[:12])
    print(
        "[full] %s\n[full] %s fps，渲染 %.2f–%.2f s（%d 帧），块 %.1fs → %d 块%s，work=%s"
        % (url, ("%g" % fps), from_s, to_s, f1 - f0, float(chunk_s), len(chunks), style_note, chunk_dir),
        flush=True,
    )
    if todo:
        print("[full] 待渲染 %d 块（%.0f 帧），workers=%d" % (len(todo), sum(b - a for a, b, _p in todo), max(1, int(workers))), flush=True)
        queue: asyncio.Queue = asyncio.Queue()
        for item in todo:
            queue.put_nowait(item)
        state = {"done": 0, "total": sum(b - a for a, b, _p in todo), "t0": time.perf_counter()}
        nw = max(1, min(int(workers), len(todo)))
        tasks = [
            asyncio.create_task(_worker(k, queue, state, url, fps, int(crf), str(preset),
                                        deterministic=deterministic, style=style,
                                        width=width, height=height))
            for k in range(nw)
        ]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        elapsed = time.perf_counter() - state["t0"]
        print("[full] 全部块渲染完成，用时 %.1fs" % elapsed, flush=True)
    else:
        print("[full] 断点续跑：%d 个块已全部存在，跳过渲染" % len(chunks), flush=True)

    # 拼接前两道门：块必须齐、块时长必须对
    _check_gaps(chunks)
    _check_chunk_durations(chunks, fps)
    parts = [Path(p) for _a, _b, p in chunks]
    print("[full] 分块齐备（%d 块），开始无损拼接 → %s" % (len(parts), out), flush=True)
    _K.concat_segments([str(p) for p in parts], str(out), workdir=str(chunk_dir))

    info = _probe_media(out)
    got = info.get("format", {}).get("duration") if isinstance(info, dict) else None
    if got is not None:
        print("[full] 成片时长 %.3fs → %s" % (float(got), out), flush=True)
    return out


# ---------------------------------------------------------------- 浏览器端文字质检
# 「一闪而过」的判据是可见时长 < 0.6 s（qa_text.js 与 references/web-engine.md §9 同口径）。
# 采样步长必须小于它：步长 >= 0.6 s 时任何一段文字最多被采到 1 个点，寿命估计必然 >= 0.6 s，
# 这条判据就**永远不可能触发**——门禁是绿的，其实没在查。所以这里把常量拿出来，参数校验时
# 直接拒绝，而不是让一个"看起来跑过"的扫描去放行。
QA_BRIEF_S = 0.6


def _check_qa_window(t0, t1, step):
    """文字质检扫描参数校验；非法 raise ValueError（否则非法参数会被静默吞成空结果 + exit 0）。

    现状：assets/web/qa_text.js 的 qaText() 对四类非法输入**全部 throw**——非有限时间、
    t1 < t0、step <= 0、step >= 一闪而过的判据阈值（BRIEF_S = 0.6）。页面侧已经不会拿
    "空结果"兜底，空扫描 / 扫不到的扫描都不能算"没有缺陷"。

    那 Python 这道还留着做什么：它跑在**起浏览器之前**，CLI 能立刻给 exit 2 + 一行可读中文
    报错，而不是等 Playwright 起来、页面抛异常再翻译成 RuntimeError。两侧阈值必须同口径
    （QA_BRIEF_S == qa_text.js 的 BRIEF_S）：任何一侧放宽，step>=0.6 时「一闪而过」这条判据
    就永远不可能触发，等于没检查。
    """
    try:
        a = float(t0)
        b = None if t1 is None else float(t1)
        s = float(step)
    except (TypeError, ValueError):
        raise ValueError("文字质检参数必须是数字：t0=%r t1=%r step=%r" % (t0, t1, step)) from None
    if not math.isfinite(a) or (b is not None and not math.isfinite(b)):
        raise ValueError("文字质检时间必须是有限秒数：t0=%r t1=%r" % (t0, t1))
    if not math.isfinite(s) or s <= 0:
        raise ValueError(
            "文字质检 step 必须是正数秒（收到 %r）。以前 <=0 会被静默替换成默认 0.2，"
            "扫描窗口和调用方以为的对不上。" % (step,))
    if s >= QA_BRIEF_S:
        raise ValueError(
            "文字质检 step=%g s 必须小于 %g s：「一闪而过」判据是可见时长 < %g s，步长 >= %g s "
            "时每段文字最多被采到 1 个点，该判据永远不可能触发（等于没检查）。"
            "整片扫描请用 step<=0.5；长片可分段多次调用。"
            % (s, QA_BRIEF_S, QA_BRIEF_S, QA_BRIEF_S))
    if b is not None and b <= a:
        raise ValueError(
            "文字质检区间非法：t1=%.3f < t0=%.3f。以前这种空扫描会返回全 0 并让 CLI exit 0，"
            "看起来像「没有任何文字缺陷」。" % (b, a))
    return a, b, s


_QA_LIST_KEYS = ("overlaps", "offscreen", "brief", "never")


def _check_qa_result(result):
    """校验页面 qaText 的返回值形状；形状不对 raise RuntimeError（不能让缺字段=全 0=PASS）。

    只判 `isinstance(result, dict)` 不够：页面 `qaText=()=>({})` 时 CLI 会把四类缺陷
    都打印成 0 并 exit 0——**假通过**。契约要求四个数组 + frames（实际采样点数），
    这里连 frames<=0 也拦（扫描 0 帧等于没查）。
    """
    if not isinstance(result, dict):
        raise RuntimeError("页面 qaText 返回值不是对象：%r" % (result,))
    bad = [k for k in _QA_LIST_KEYS if not isinstance(result.get(k), list)]
    if bad:
        raise RuntimeError(
            "页面 qaText 返回值缺字段 / 类型不对：%s 必须是数组（页面契约见 "
            "references/web-engine.md §9；assets/web/qa_text.js 已按契约返回）。收到键：%s"
            % ("、".join(bad), sorted(result)[:12]))
    try:
        frames = int(result.get("frames"))
    except (TypeError, ValueError):
        frames = 0
    if frames <= 0:
        raise RuntimeError(
            "页面 qaText 返回 frames=%r（没有扫描任何采样点）。空扫描不能当「没有缺陷」通过；"
            "检查 qaText 的 t0/t1/step 参数与页面 DURATION 是否一致。" % (result.get("frames"),))
    return result


def qa_text(html_path, t0=0.0, t1=None, step=0.2, deterministic=None, *,
            style=None, width=W, height=H, fps=None) -> dict:
    """浏览器端文字质检：重叠 / 出画 / 一闪而过 / 从没露面（渲染期间会临时改页面状态）。

    跟着 style 一起检查——换了浅色主题/非 1080p 画布后，出画与重叠的判据必须用
    同一份画布尺寸（qa_text.js 读 FK.W / FK.H），否则"检查通过"只是没在检查新尺寸。

    参数与返回值 fail-closed（空扫描 / 扫不到的扫描 / 页面返回 {} 如果都 exit 0，
    门禁看着是绿的，其实什么都没查）。见 _check_qa_window / _check_qa_result。
    """
    _setup_stdout()
    t0, t1, step = _check_qa_window(t0, t1, step)
    return asyncio.run(_qa_text_async(html_path, t0, t1, step, deterministic,
                                      style=style, width=width, height=height, fps=fps))


async def _qa_text_async(html_path, t0, t1, step, deterministic=None, *,
                         style=None, width=W, height=H, fps=None) -> dict:
    url = _to_url(html_path)
    async with _playwright()() as pw:
        browser, page, _cdp = await open_page(pw, url, width, height, deterministic=deterministic,
                                              style=style, fps=fps)
        try:
            if t1 is None:
                t1 = await page_number(page, "DURATION")
            result = await page.evaluate(
                "([a, b, c]) => window.qaText(a, b, c)", [float(t0), float(t1), float(step)]
            )
        finally:
            try:
                await browser.close()
            except Exception:
                pass
    return _check_qa_result(result)


# ---------------------------------------------------------------- 指定帧号截 PNG
def render_frames(html_path, frames, out_dir=None, *, tag="", style=None, width=W, height=H,
                  fps=None, deterministic=None) -> list:
    """把**指定帧号**渲成 PNG，返回 [{"f","path","sha256","bytes","fps"}]（不编码视频）。

    这是给 render.py / 自检 / 人工验收用的最小确定性入口：
    帧号 → 时间 = f / FPS（与 render_full 同一口径），不存在"按播放时间截屏"。
    style=None 时不注入 __STYLE__（像素与默认外观一致）；传 style 时整页跟随风格包。
    out_dir 缺省 ./frames；文件名 <tag>f<帧号:05d>.png。
    """
    _setup_stdout()
    flist = [int(f) for f in frames]
    if not flist:
        raise ValueError("render_frames 至少需要 1 个帧号（frames 为空）")
    if any(f < 0 for f in flist):
        raise ValueError("帧号不能为负：%r" % (flist,))
    out = Path(out_dir).expanduser() if out_dir else (Path.cwd() / "frames")
    if not out.is_absolute():
        out = (Path.cwd() / out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    return asyncio.run(_render_frames_async(html_path, flist, out, str(tag), style,
                                            width, height, fps, deterministic))


async def _render_frames_async(html_path, frames, out: Path, tag: str, style=None,
                               width=W, height=H, fps=None, deterministic=None) -> list:
    url = _to_url(html_path)
    width, height = _validate_canvas(width, height)
    results = []
    async with _playwright()() as pw:
        browser, page, cdp = await open_page(pw, url, width, height, deterministic=deterministic,
                                             style=style, fps=fps)
        try:
            page_fps = await page_number(page, "FPS", default=FPS)
            for f in frames:
                png = await _grab(page, cdp, f)
                path = out / ("%sf%05d.png" % (tag, int(f)))
                path.write_bytes(png)
                results.append({
                    "f": int(f),
                    "path": str(path),
                    "sha256": hashlib.sha256(png).hexdigest(),
                    "bytes": len(png),
                    "fps": page_fps,
                })
        finally:
            try:
                await browser.close()
            except Exception:
                pass
    return results


# ---------------------------------------------------------------- 自检辅助
def _png_stats(data: bytes) -> dict:
    from io import BytesIO
    from PIL import Image

    im = Image.open(BytesIO(data)).convert("L")
    hist = im.histogram()
    total = float(sum(hist)) or 1.0
    mean = sum(i * count for i, count in enumerate(hist)) / total
    top = 0
    for i in range(255, -1, -1):
        if hist[i]:
            top = i
            break
    return {"mean": round(mean, 2), "max": top, "w": im.width, "h": im.height}


def _png_size(data):
    """PNG 的像素尺寸 (w, h)；不是 PNG / 空数据返回 None（自检断言用，不抛异常）。"""
    if not data:
        return None
    try:
        st = _png_stats(data)
        return (st["w"], st["h"])
    except Exception:                       # noqa: BLE001
        return None


def _encode_pngs(pngs, dst, fps: float, crf: int = 17, preset: str = "slow") -> Path:
    """把一组 PNG 字节直接喂 ffmpeg（自检里验证 image2pipe 链路）。"""
    proc = subprocess.Popen(
        _encoder_cmd(dst, fps, crf, preset), stdin=subprocess.PIPE, stderr=subprocess.PIPE
    )
    _out, err = proc.communicate(input=b"".join(pngs), timeout=300)
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg 编码失败：%s" % err.decode("utf-8", "replace")[:500])
    return Path(dst)


async def _selftest_probe() -> dict:
    """一次浏览器会话内采集自检需要的全部证据（页面只加载一遍 + 末尾重载验证零外链）。"""
    url = _to_url(DEFAULT_HTML)
    out = {"fps": FPS, "duration": 0.0, "browser": ""}
    async with _playwright()() as pw:
        browser, page, cdp = await open_page(pw, url, W, H, log=True)
        try:
            out["browser"] = _BROWSER_KIND
            out["duration"] = float(await page.evaluate("window.DURATION"))
            out["fps"] = float(await page.evaluate("window.FPS || 30"))
            out["ready"] = bool(await page.evaluate("window.ready === true"))

            first = await _grab(page, cdp, 45)
            second = await _grab(page, cdp, 45)
            out["same_bytes"] = first == second
            out["same_len"] = len(first)
            out["same_sha"] = hashlib.sha256(first).hexdigest()[:16]
            other = await _grab(page, cdp, 46)
            out["diff_frames"] = first != other

            pngs = {}
            stats = {}
            for f in (20, 160, 330):
                data = await _grab(page, cdp, f)
                pngs[f] = data
                stats[f] = _png_stats(data)
            out["pngs"] = pngs
            out["frame_stats"] = stats

            seq = []
            for i in range(12):
                seq.append(await _grab(page, cdp, 40 + i))
            out["seq"] = seq

            out["cues"] = await page.evaluate("window.getCues()")
            out["events"] = await page.evaluate("window.getEvents()")
            # step 用 0.2（文档口径）：12 s 页面 → 61 个采样点，自检据此校验 frames 字段
            out["qa"] = await page.evaluate(
                "([a, b, c]) => window.qaText(a, b, c)", [0.0, out["duration"], 0.2]
            )

            # 零外部网络：重载一次，记录页面发出的全部请求
            requests = []
            page.on("request", lambda req: requests.append(req.url))
            await page.reload(wait_until="domcontentloaded", timeout=120_000)
            await page.wait_for_function("window.ready === true", timeout=120_000, polling=100)
            out["requests"] = requests
        finally:
            try:
                await browser.close()
            except Exception:
                pass
    return out


# ---------------------------------------------------------------- 自检辅助
_NONDET_JS = re.compile(r"(Math\.random|Date\.now|performance\.now|new\s+Date\s*\()")


def _strip_js_comments(text: str) -> str:
    """去掉 JS 的 /* */ 与 // 注释，避免把文档注释里的"不许用 Math.random"当成违规。"""
    out = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    out = re.sub(r"(?m)^\s*//.*$", "", out)
    out = re.sub(r"(?<!:)//[^\n]*", "", out)
    return out


def _nondet_hits(text: str) -> list:
    """在（剔除注释后的）JS 里找非确定性时间/随机 API；返回 [(行号, 命中片段)]。"""
    cleaned = _strip_js_comments(text)
    hits = []
    for i, line in enumerate(cleaned.splitlines(), 1):
        for m in _NONDET_JS.finditer(line):
            hits.append((i, m.group(1)))
    return hits


async def _capture_frames(html_path, frames, deterministic=None):
    """开一个浏览器会话，连续截指定帧，返回 [(帧号, PNG bytes)]。"""
    url = _to_url(html_path)
    async with _playwright()() as pw:
        browser, page, cdp = await open_page(pw, url, W, H, deterministic=deterministic)
        try:
            return [(f, await _grab(page, cdp, f)) for f in frames]
        finally:
            try:
                await browser.close()
            except Exception:
                pass


async def _open_page_expect_error(html_path, ready_timeout=3000) -> str:
    """打开一个坏页面，返回它抛出的错误消息（用来断言报错可操作）。"""
    url = _to_url(html_path)
    async with _playwright()() as pw:
        try:
            await open_page(pw, url, W, H, ready_timeout=ready_timeout)
        except Exception as exc:        # noqa: BLE001
            return "%s: %s" % (type(exc).__name__, exc)
        raise RuntimeError("坏页面居然没有报错：%s" % html_path)


# ---------------------------------------------------------------- 自检：风格注入
# 自检用的主题故意选"浅色 + 衬线 + 右对齐 + 四角线框"：与模板默认的深蓝橙 + 居中 + 无框
# 在每一次可观测维度上都不同，任何一处没跟着 __STYLE__ 走都会暴露成"两张图一样"。
_SELFTEST_STYLE = {
    "id": "selftest-light", "label": "自检浅色",
    "bg": (250, 246, 238), "bg2": (236, 228, 214), "surface": (255, 252, 246),
    "ink": (28, 26, 24), "muted": (88, 82, 74), "dim": (112, 104, 94),
    "accent": (176, 48, 40), "accent2": (38, 86, 128),
    "good": (30, 110, 72), "bad": (160, 36, 32), "rule": (28, 26, 24),
    "vignette": 0.10, "vignette_tint": (214, 204, 186),
    "grain": 3.0, "grain_freq": 0.27, "texture": "paper", "texture_alpha": 0.03,
    "family": "serif",
    "scale": {"display": (92, "bold"), "heading": (60, "bold")},
    "line_gap": 1.34, "tracking": 2.0,
    "layout": "split-right",
    "card": {"radius": 8, "fill_a": 26, "outline_a": 40, "pad": 30, "h": 92, "gap": 112},
    "safe_bias": -8,
    "ease": "out5", "entrance_dx": 18, "entrance_dy": 20,
    "push": 0.06, "item_stagger": 0.38, "transition": "wash",
    "hud": "corner-line",
    "qa": {"luma_max": 0.045, "flicker_thresh": 6.0, "dark": False},
}


async def _selftest_style_probe() -> dict:
    """采集风格注入的三组证据（每个浏览器会话都独立，避免状态串味）。

    ① 无 style：页面里不应有 __STYLE__、画布 1920x1080、同帧两次 byte 相同；
    ② 有 style：__STYLE__ / 字体栈 / 画布生效、同帧两次 byte 相同、与 ① 同帧不同图；
    ③ 1280x720 + style：画布与截图像素尺寸跟着变（证明确实不是"写死 1080p 假装支持"）。
    """
    url = _to_url(DEFAULT_HTML)
    out = {}
    async with _playwright()() as pw:
        browser, page, cdp = await open_page(pw, url, W, H)
        try:
            out["plain_has_style"] = await page.evaluate(
                "() => typeof window.__STYLE__ !== 'undefined'")
            out["plain_canvas"] = await page.evaluate(
                "() => [document.getElementById('stage').width, document.getElementById('stage').height]")
            out["plain_a"] = await _grab(page, cdp, 45)
            out["plain_b"] = await _grab(page, cdp, 45)
        finally:
            try:
                await browser.close()
            except Exception:
                pass

        browser, page, cdp = await open_page(pw, url, W, H, style=_SELFTEST_STYLE)
        try:
            out["has_style"] = await page.evaluate(
                "() => typeof window.__STYLE__ === 'object' && window.__STYLE__ !== null")
            out["style_ink"] = await page.evaluate(
                "() => window.__STYLE__ && window.__STYLE__.theme ? window.__STYLE__.theme.ink : null")
            out["style_family"] = await page.evaluate(
                "() => window.FK ? window.FK.FAMILY : null")
            out["style_canvas"] = await page.evaluate(
                "() => [document.getElementById('stage').width, document.getElementById('stage').height]")
            out["style_a"] = await _grab(page, cdp, 45)
            out["style_b"] = await _grab(page, cdp, 45)
        finally:
            try:
                await browser.close()
            except Exception:
                pass

        browser, page, cdp = await open_page(pw, url, 1280, 720, style=_SELFTEST_STYLE)
        try:
            out["small_canvas"] = await page.evaluate(
                "() => [document.getElementById('stage').width, document.getElementById('stage').height]")
            out["small_png"] = await _grab(page, cdp, 45)
        finally:
            try:
                await browser.close()
            except Exception:
                pass

        # ④ 竖屏 1080x1920 + style：_validate_canvas 自 v1.8.5 起就放行 9:16，但 engine.js
        # 当时仍只认 16:9 —— 上游同意、页面抛错，竖屏片在浏览器路线必炸且报错指向"画布比例"，
        # 看起来像用户传错参数。这条用例把两边绑在一起：注入竖屏画布必须真的建出 1080x1920。
        browser, page, cdp = await open_page(pw, url, 1080, 1920, style=_SELFTEST_STYLE)
        try:
            out["portrait_canvas"] = await page.evaluate(
                "() => [document.getElementById('stage').width, document.getElementById('stage').height]")
            out["portrait_aspect"] = await page.evaluate(
                "() => window.__STYLE__ && window.__STYLE__.canvas ? window.__STYLE__.canvas.aspect : null")
            out["portrait_png"] = await _grab(page, cdp, 45)
        finally:
            try:
                await browser.close()
            except Exception:
                pass
    return out


# ---------------------------------------------------------------- 自检
def selftest() -> int:
    """不联网自检：页面契约、确定性（同帧截图 byte 相同）、cue 递增、文字质检、编码链路。"""
    _setup_stdout()
    t_start = time.perf_counter()
    results = []

    def check(title: str, ok: bool, detail: str = "") -> None:
        ok = bool(ok)
        results.append(ok)
        print(("[PASS] " if ok else "[FAIL] ") + title + (("  —— " + detail) if detail else ""), flush=True)

    print("=" * 78)
    print("webengine.py --selftest：无头浏览器逐帧渲染 / 确定性 / cue / 文字质检 / 风格注入")
    print("=" * 78)

    names = ("index.html", "engine.js", "film.js", "style.css", "qa_text.js")
    missing = [n for n in names if not (_ASSET_DIR / n).is_file()]
    check("assets/web/ 五个文件齐全", not missing,
          str(_ASSET_DIR) if not missing else "缺 " + "、".join(missing))

    net_pat = re.compile(r"https?://|//(?:cdn|unpkg|jsdelivr)|fonts\.googleapis")
    net_bad = []
    for name in names:
        path = _ASSET_DIR / name
        if not path.is_file():
            continue
        hit = net_pat.search(path.read_text(encoding="utf-8", errors="replace"))
        if hit:
            net_bad.append("%s 含 %s" % (name, hit.group(0)))
    check("页面零网络依赖（不连 CDN / 无外链）", not net_bad,
          "只引用同目录本地文件" if not net_bad else "；".join(net_bad[:4]))

    # 非确定性 API 扫描（剔除注释后）：Math.random / Date.now / performance.now / new Date()
    nondet_bad = []
    for name in names:
        path = _ASSET_DIR / name
        if not path.is_file() or not name.endswith(".js"):
            continue
        for ln, frag in _nondet_hits(path.read_text(encoding="utf-8", errors="replace")):
            nondet_bad.append("%s:%d %s" % (name, ln, frag))
    check("页面脚本零非确定性 API（Math.random / Date.now / performance.now）",
          not nondet_bad, "5 个 js 文件剔除注释后 0 命中" if not nondet_bad
          else "；".join(nondet_bad[:5]))

    # 纯函数级检查（不开浏览器）
    tmp_root = Path(tempfile.mkdtemp(prefix="webengine_self_"))
    try:
        chunk_dir = tmp_root / "chunks"
        chunks = _plan_chunks(chunk_dir, 0, 900, 900)
        names_ok = [Path(p).name for _a, _b, p in chunks] == ["c000.mp4"]
        check("分块按绝对起始帧命名（c{帧//step:03d}.mp4）", names_ok,
              "、".join(Path(p).name for _a, _b, p in chunks))

        gap_raised = False
        gap_detail = "缺块时抛 RuntimeError"
        try:
            _check_gaps(chunks)
            gap_detail = "缺块没有报错"
        except RuntimeError as exc:
            gap_raised = "缺口" in str(exc)
            gap_detail = "缺块时抛 RuntimeError" if gap_raised else str(exc)[:120]
        except Exception as exc:
            gap_detail = "%s: %s" % (type(exc).__name__, exc)
        check("拼接前显式缺口检测（缺块 raise）", gap_raised, gap_detail)

        chunk_dir.mkdir(parents=True, exist_ok=True)
        for _a, _b, path in chunks:
            path.write_bytes(b"")
        no_gap = True
        try:
            _check_gaps(chunks)
        except Exception:
            no_gap = False
        check("块齐备时不误报缺口", no_gap)

        cmd = _encoder_cmd("probe.mp4", 30, 17, "slow")
        enc_ok = "-r" in cmd and cmd[cmd.index("-r") + 1] == "30" \
            and "-g" in cmd and cmd[cmd.index("-g") + 1] == "60" \
            and "libx264" in cmd
        check("编码参数显式 -r FPS / -g 2*FPS / libx264", enc_ok,
              "fps=30 → -r 30 -g 60")

        # 崩溃重开：用桩替身模拟「前两帧失败、第三次成功」，验证重开重试逻辑（不依赖真实崩溃）
        reopen_ok = False
        reopen_detail = ""
        real_grab = globals()["_grab"]
        try:
            attempts = {"n": 0, "opens": 0, "closes": 0}

            class _StubBrowser(object):
                browser = None
                page = None
                cdp = None
                k = 0

                async def open(self):
                    attempts["opens"] += 1

                async def close(self):
                    attempts["closes"] += 1

            stub = _StubBrowser()
            stub.frame = _Browser.frame.__get__(stub, _StubBrowser)

            async def _flaky_grab(page, cdp, f):
                attempts["n"] += 1
                if attempts["n"] <= 2:
                    raise RuntimeError("模拟页面崩溃")
                return b"png-bytes"

            globals()["_grab"] = _flaky_grab
            got = asyncio.run(stub.frame(7))
            reopen_ok = (got == b"png-bytes") and attempts["n"] == 3 and attempts["closes"] == 2
            reopen_detail = "失败 2 次后第 3 次成功（重开浏览器 %d 次）" % attempts["opens"]
        except Exception as exc:
            reopen_detail = "%s: %s" % (type(exc).__name__, str(exc)[:200])
        finally:
            globals()["_grab"] = real_grab
        check("单帧失败自动重开浏览器重试（崩溃重开）", reopen_ok, reopen_detail)

        probe = {}
        try:
            probe = asyncio.run(_selftest_probe())
        except Exception as exc:
            probe = {"_error": "%s: %s" % (type(exc).__name__, str(exc)[:300])}
        perr = probe.get("_error")

        check("启动无头浏览器并等到 window.ready === true",
              bool(probe.get("ready")) and not perr,
              perr or "浏览器=%s，DURATION=%ss，FPS=%s"
              % (probe.get("browser", "?"), probe.get("duration"), probe.get("fps")))

        same = bool(probe.get("same_bytes"))
        check("同一帧两次截图 byte 完全相同", same,
              ("f=45 两次均 %d bytes，sha256=%s…" % (probe.get("same_len", 0), probe.get("same_sha", "")))
              if same else (perr or "两次截图字节不同（页面存在非确定性）"))

        # 跨浏览器会话确定性：确定性模式（软件光栅）下两次独立会话必须 byte 一致。
        # 默认 GPU 模式下实测会有 4 个像素 ±1 级的抗锯齿差异（见 DETERMINISTIC_ARGS 注释），
        # 所以这里测的是"确定性模式真的确定性"，而不是把 GPU 差异当失败。
        cross_ok = False
        cross_detail = perr or "无截图数据"
        if not perr:
            try:
                d1 = asyncio.run(_capture_frames(DEFAULT_HTML, (45, 180), deterministic=True))
                d2 = asyncio.run(_capture_frames(DEFAULT_HTML, (45, 180), deterministic=True))
                diff = [f for (f, a), (_f, b) in zip(d1, d2) if a != b]
                cross_ok = not diff
                cross_detail = ("两次独立会话 f=45/180 byte 完全一致（%d/%d bytes）"
                                % (len(d1[0][1]), len(d1[1][1])) if cross_ok
                                else "两次独立会话有差异帧：%s" % diff)
            except Exception as exc:    # noqa: BLE001
                cross_detail = "%s: %s" % (type(exc).__name__, str(exc)[:200])
        check("确定性模式（软件光栅）跨会话截图 byte 一致", cross_ok, cross_detail)

        # 坏页面 1：缺 window.renderFrame —— 报错要点名 renderFrame，不能只抛裸超时
        bad_html = tmp_root / "bad_no_render.html"
        bad_html.write_text(
            "<!doctype html><meta charset='utf-8'><canvas id='stage' width='960' height='540'></canvas>"
            "<script>window.DURATION=2.0;window.FPS=30;window.ready=true;</script>",
            encoding="utf-8")
        msg = ""
        try:
            msg = asyncio.run(_open_page_expect_error(bad_html))
        except Exception as exc:        # noqa: BLE001
            msg = "%s: %s" % (type(exc).__name__, exc)
        check("页面缺 renderFrame 时给出可操作报错", "renderFrame" in msg and "缺少" in msg,
              msg[:200])

        # 坏页面 2：ready 永远不置 true（且脚本报错）—— 报错要带页面异常与排查步骤
        bad_html2 = tmp_root / "bad_no_ready.html"
        bad_html2.write_text(
            "<!doctype html><meta charset='utf-8'><canvas id='stage' width='960' height='540'></canvas>"
            "<script>window.DURATION=2.0;window.FPS=30;"
            "window.renderFrame=async function(f){return true;};"
            "throw new Error('故意炸掉的场景脚本');</script>",
            encoding="utf-8")
        msg2 = ""
        try:
            msg2 = asyncio.run(_open_page_expect_error(bad_html2, ready_timeout=2500))
        except Exception as exc:        # noqa: BLE001
            msg2 = "%s: %s" % (type(exc).__name__, exc)
        check("页面 ready 不置 true 时给出可操作报错", "window.ready" in msg2 and "排查" in msg2,
              msg2[:200])

        stats = probe.get("frame_stats") or {}
        dark = []
        detail = []
        for f in sorted(stats):
            st = stats[f]
            if not (st["mean"] > 6.0 and st["max"] > 60):
                dark.append(str(f))
            detail.append("f=%d mean=%.1f max=%d" % (f, st["mean"], st["max"]))
        check("三帧非纯黑", (not dark) and len(stats) == 3,
              "；".join(detail) if not perr else perr)

        check("不同帧渲染结果不同", bool(probe.get("diff_frames")),
              "f=45 与 f=46 的 PNG 不同" if probe.get("diff_frames") else (perr or "相邻帧截图相同（时间码没变？）"))

        # ---- 画布尺寸 / 风格注入 ----
        # 合法：横屏 16:9 三档 + 竖屏 9:16 两档。这条用例以前断言"1080x1920 必须被拒"，
        # 而 _validate_canvas 从 v1.8.5 起就接受 9:16（竖屏是正式能力，见 SKILL.md §7.5.1）——
        # 断言没跟着改，CI 就一直红在这条上，红的不是能力而是过期的期望。
        # 现在的守则：**两套比例都收、其它比例仍然 fail-closed**，且竖屏报错文案要点名竖屏。
        good_sizes, bad_sizes = [], []
        for w_, h_ in ((1920, 1080), (1280, 720), (3840, 2160), (1080, 1920), (720, 1280)):
            try:
                _validate_canvas(w_, h_)
                good_sizes.append("%dx%d" % (w_, h_))
            except Exception as exc:            # noqa: BLE001
                good_sizes.append("意外拒绝 %dx%d（%s）" % (w_, h_, exc))
        for w_, h_ in ((1920, 1200), (1921, 1080), (1080, 1921), (0, 1080), (-1920, -1080)):
            try:
                _validate_canvas(w_, h_)
                bad_sizes.append("%dx%d 没被拒（%s 不在两套版式内）" % (w_, h_, "非正数"))
            except ValueError:
                pass
        # 4:3 的报错必须把两套合法画幅都说出来（只说"16:9 横屏"会让人以为竖屏不支持）
        four_three = ""
        try:
            _validate_canvas(1920, 1440)
        except ValueError as exc:
            four_three = str(exc)
        check("画布收 16:9 横屏与 9:16 竖屏偶数尺寸（4:3/奇数/非正数 fail-closed）",
              (not bad_sizes) and len(good_sizes) == 5
              and "9:16" in four_three and "16:9" in four_three,
              "；".join(bad_sizes) or ("合法：%s；4:3 报错点名两套画幅" % "、".join(good_sizes)))

        # ---- 深审：确定性开关三态 / 文字质检参数与返回值 fail-closed ----
        # 这三条都是纯函数校验（不开浏览器），专门守"门禁看着干净、其实没在查"的三条路径：
        #   · CLI --deterministic 用 store_true 时默认 False 会压掉 WEBENGINE_DETERMINISTIC；
        #   · textqa 空扫描（t1<t0）/ 步长 >= brief 阈值时返回全 0 并 exit 0；
        #   · 页面 qaText 返回 {} / frames=0 时同样被当成"零缺陷"。
        det_lines = []
        det_args = _build_parser().parse_args(["frames", "x.html", "--frames", "1"])
        if det_args.deterministic is not None:
            det_lines.append("未传 --deterministic 时默认值是 %r（应为 None，才能读环境变量）"
                             % (det_args.deterministic,))
        det_on = _build_parser().parse_args(["frames", "x.html", "--frames", "1", "--deterministic"])
        if det_on.deterministic is not True:
            det_lines.append("显式 --deterministic 没变成 True（%r）" % (det_on.deterministic,))
        old_env = os.environ.get("WEBENGINE_DETERMINISTIC")
        try:
            os.environ["WEBENGINE_DETERMINISTIC"] = "1"
            env_ok = bool(_use_deterministic(None))
            cli_env_ok = "--disable-gpu" in browser_args(det_args.deterministic)
            force_off = "--disable-gpu" in browser_args(False)
        finally:
            if old_env is None:
                os.environ.pop("WEBENGINE_DETERMINISTIC", None)
            else:
                os.environ["WEBENGINE_DETERMINISTIC"] = old_env
        if not env_ok:
            det_lines.append("WEBENGINE_DETERMINISTIC=1 时 _use_deterministic(None) 不是 True")
        if not cli_env_ok:
            det_lines.append("CLI 未传 --deterministic 时环境变量没生效")
        if force_off:
            det_lines.append("显式 deterministic=False 仍然加了 --disable-gpu（三态语义坏了）")
        check("确定性开关三态（CLI --deterministic 未传时尊重 WEBENGINE_DETERMINISTIC）",
              not det_lines, "；".join(det_lines) or "None=读环境变量 / True=软件光栅 / False=明确关")

        qa_lines = []
        for label, args_ in (("t1<t0", (2.0, 1.0, 0.2)),
                             ("step=0", (0.0, 2.0, 0.0)),
                             ("step<0", (0.0, 2.0, -0.1)),
                             ("step=brief 阈值 0.6", (0.0, 2.0, 0.6)),
                             ("step=1.0", (0.0, 2.0, 1.0))):
            try:
                _check_qa_window(*args_)
                qa_lines.append("%s 没被拒（会退化成空扫描/永远报不出 brief）" % label)
            except ValueError:
                pass
        ok_args = True
        try:
            a_, b_, s_ = _check_qa_window(0.0, 2.0, 0.2)
            ok_args = (a_, b_, s_) == (0.0, 2.0, 0.2)
            # t1=None 表示"到页面 DURATION"，必须放行（由 _qa_text_async 现读）
            _check_qa_window(0.0, None, 0.2)
        except Exception as exc:                # noqa: BLE001
            ok_args = False
            qa_lines.append("合法参数被拒：%s" % exc)
        check("文字质检参数 fail-closed（空扫描 / 无效步长不再 exit 0）",
              ok_args and not qa_lines,
              "；".join(qa_lines) or "t1<t0 / step<=0 / step>=0.6 全拒；t1=None、step=0.2 放行")

        res_lines = []
        for label, bad in (("{}", {}),
                           ("缺 never", {"overlaps": [], "offscreen": [], "brief": [], "frames": 3}),
                           ("frames=0", {"overlaps": [], "offscreen": [], "brief": [],
                                          "never": [], "frames": 0}),
                           ("overlaps 不是数组", {"overlaps": 0, "offscreen": [], "brief": [],
                                                  "never": [], "frames": 3})):
            try:
                _check_qa_result(bad)
                res_lines.append("%s 没被拒（会被当成零缺陷 PASS）" % label)
            except RuntimeError:
                pass
        try:
            good_res = _check_qa_result({"overlaps": [], "offscreen": [], "brief": [],
                                         "never": ["x"], "frames": 5})
            if good_res.get("frames") != 5:
                res_lines.append("合法返回值被改坏")
        except Exception as exc:                # noqa: BLE001
            res_lines.append("合法返回值被拒：%s" % exc)
        check("页面 qaText 返回值形状 fail-closed（缺字段 / frames=0 不再当 PASS）",
              not res_lines, "；".join(res_lines) or "{} / 缺字段 / frames=0 / 类型不对 全拒；正常返回值放行")

        # cue / event 时间必须是有限数：NaN / Infinity 会被 json.dumps 写成非法 JSON 字面量，
        # 再被下游 json.load 照单收下 —— 时间轴静默错位，且看不出是哪条提示点。
        t_lines = []
        try:
            ok_cues = _norm_cue_list([{"t": 1.0, "type": "ui_blip"}], "web")
            if ok_cues[0]["t"] != 1.0 or ok_cues[0]["src"] != "web":
                t_lines.append("合法 cue 被改坏：%r" % (ok_cues,))
            ok_ev = _norm_events([{"t": 1.0, "kind": "flash"}])
            if ok_ev[0]["t"] != 1.0:
                t_lines.append("合法 event 被改坏：%r" % (ok_ev,))
        except Exception as exc:            # noqa: BLE001
            t_lines.append("合法 cue/event 被拒：%s" % exc)
        for label, bad_cue in (("NaN", {"t": float("nan"), "type": "ui_blip"}),
                               ("Infinity", {"t": float("inf"), "type": "ui_blip"}),
                               ("字符串", {"t": "abc", "type": "ui_blip"})):
            try:
                _norm_cue_list([bad_cue], "web")
                t_lines.append("cue t=%s 没被拒（会写出非法 JSON）" % label)
            except ValueError:
                pass
        for label, bad_ev in (("NaN", {"t": float("nan"), "kind": "flash"}),
                              ("Infinity", {"t": float("inf"), "kind": "flash"})):
            try:
                _norm_events([bad_ev])
                t_lines.append("event t=%s 没被拒" % label)
            except ValueError:
                pass
        check("cue / 视觉事件时间只收有限数（NaN / Infinity 不再写进 cues.json）",
              not t_lines, "；".join(t_lines) or "1.0 放行（src 补齐）；NaN / Infinity / 字符串 全拒")

        style_norm, style_lines = {}, []
        try:
            style_norm["none"] = normalize_style(None)
        except Exception as exc:                # noqa: BLE001
            style_lines.append("None 报错：%s" % exc)
        try:
            style_norm["plan"] = normalize_style({
                "style": "selftest", "style_name": "自检",
                "theme": dict(_SELFTEST_STYLE), "layout_default": "split-right"})
        except Exception as exc:                # noqa: BLE001
            style_lines.append("style plan 报错：%s" % exc)
        try:
            style_norm["theme"] = normalize_style(dict(_SELFTEST_STYLE))
        except Exception as exc:                # noqa: BLE001
            style_lines.append("裸主题报错：%s" % exc)
        for label, bad in (("非 dict", "abc"),
                           ("缺色键", {"bg": (1, 2, 3), "ink": (255, 255, 255)}),
                           ("颜色越界", dict(_SELFTEST_STYLE, accent=(999, 0, 0))),
                           ("未知字体族", dict(_SELFTEST_STYLE, family="comic"))):
            try:
                normalize_style(bad)
                style_lines.append("%s 没被拒" % label)
            except (ValueError, TypeError):
                pass
        plan = style_norm.get("plan") or {}
        plan_ok = (plan.get("theme", {}).get("ink") == "#1c1a18"
                   and plan.get("typography", {}).get("family") == "serif"
                   and plan.get("layout", {}).get("default") == "split-right")
        same_sha = bool(plan) and style_sha256(plan) == style_sha256(normalize_style(
            {"style": "selftest", "style_name": "自检",
             "theme": dict(_SELFTEST_STYLE), "layout_default": "split-right"}))
        check("style=None 不注入；plan / 裸主题两种写法都归一；非法 style fail-closed",
              style_norm.get("none") is None and plan_ok and same_sha and not style_lines,
              "；".join(style_lines) or "None→None；ink=#1c1a18；family=serif；layout=split-right；非法 4 例全 raise")

        style_probe = {}
        try:
            style_probe = asyncio.run(_selftest_style_probe())
        except Exception as exc:                # noqa: BLE001
            style_probe = {"_error": "%s: %s" % (type(exc).__name__, str(exc)[:300])}
        serr = style_probe.get("_error")
        plain_a = style_probe.get("plain_a") or b""
        styled_a = style_probe.get("style_a") or b""
        check("style=None 时页面无 window.__STYLE__、画布 1920x1080（旧行为零注入）",
              (not serr) and style_probe.get("plain_has_style") is False
              and style_probe.get("plain_canvas") == [W, H],
              serr or "页面里没有 __STYLE__；画布 %s" % (style_probe.get("plain_canvas"),))
        check("注入 style 后 __STYLE__ / 字体栈 / 画布生效",
              (not serr) and bool(style_probe.get("has_style"))
              and style_probe.get("style_ink") == "#1c1a18"
              and style_probe.get("style_family") == FONT_STACKS["serif"]
              and style_probe.get("style_canvas") == [W, H],
              serr or ("theme.ink=%s；FK.FAMILY=衬线栈；画布 %s"
                       % (style_probe.get("style_ink"), style_probe.get("style_canvas"))))
        check("有 style 时同一帧两次截图 byte 相同（确定性不被风格破坏）",
              bool(styled_a) and styled_a == style_probe.get("style_b"),
              serr or ("两次均 %d bytes，sha256=%s…"
                       % (len(styled_a), hashlib.sha256(styled_a).hexdigest()[:16])))
        mean_plain = _png_stats(plain_a)["mean"] if plain_a else 0.0
        mean_style = _png_stats(styled_a)["mean"] if styled_a else 0.0
        check("有 style 与无 style 同一帧 PNG 不同（确实换了画风）",
              bool(plain_a) and bool(styled_a) and plain_a != styled_a
              and abs(mean_style - mean_plain) > 8.0,
              serr or ("f=45 灰度均值 %.1f → %.1f（深蓝橙 → 浅色纸感）" % (mean_plain, mean_style)))
        check("1280x720 + style 时画布与截图像素尺寸都跟着变",
              (not serr) and style_probe.get("small_canvas") == [1280, 720]
              and _png_size(style_probe.get("small_png")) == (1280, 720),
              serr or ("canvas=%s，PNG=%s"
                       % (style_probe.get("small_canvas"), _png_size(style_probe.get("small_png")))))
        check("1080x1920 + style 时竖屏画布能建出来（engine.js 与 _validate_canvas 同口径）",
              (not serr) and style_probe.get("portrait_canvas") == [1080, 1920]
              and _png_size(style_probe.get("portrait_png")) == (1080, 1920)
              and style_probe.get("portrait_aspect") == "9:16",
              serr or ("canvas=%s，PNG=%s，payload.aspect=%s"
                       % (style_probe.get("portrait_canvas"),
                          _png_size(style_probe.get("portrait_png")),
                          style_probe.get("portrait_aspect"))))


        cues = probe.get("cues") or []
        ts = [float(c.get("t", -1)) for c in cues]
        monotonic = len(ts) >= 2 and all(b > a for a, b in zip(ts, ts[1:]))
        types_ok = all(isinstance(c.get("type"), str) and c.get("type") for c in cues)
        check("getCues() 非空且时间严格递增", bool(cues) and monotonic and types_ok,
              "%d 个 cue，t = %s" % (len(cues), ", ".join("%g" % t for t in ts[:8])) if cues else (perr or "空列表"))

        events = probe.get("events") or []
        ev_mono = all(float(b.get("t", -1)) >= float(a.get("t", -1)) for a, b in zip(events, events[1:]))
        check("getEvents() 非空且时间递增", bool(events) and ev_mono,
              "%d 个视觉事件" % len(events) if events else (perr or "空列表"))

        qa = probe.get("qa") or {}
        qa_keys = all(k in qa for k in ("overlaps", "offscreen", "brief", "never", "frames"))
        qa_clean = qa_keys and not qa.get("overlaps") and not qa.get("offscreen")
        # frames 字段 = 采样帧数：12 s / step 0.2 = 60 步 → 61 个采样点
        want_frames = int(round(float(probe.get("duration") or 0) / 0.2)) + 1
        frames_ok = int(qa.get("frames") or 0) == want_frames
        check("qaText() 跑通、模板页无重叠/出画、frames=采样数", qa_clean and frames_ok,
              perr or ("重叠 %d、出画 %d、一闪而过 %d、从没露面 %d、frames=%s（期望 %d）"
                       % (len(qa.get("overlaps") or []), len(qa.get("offscreen") or []),
                          len(qa.get("brief") or []), len(qa.get("never") or []),
                          qa.get("frames"), want_frames)))

        # 坏页面 3：四类文字缺陷各造一个 —— 验证 qa_text.js 的阈值真的会失败，不是摆设。
        bad_page = tmp_root / "bad_text.html"
        eng = (_ASSET_DIR / "engine.js").as_uri()
        qat = (_ASSET_DIR / "qa_text.js").as_uri()
        bad_page.write_text(
            "<!doctype html><meta charset='utf-8'>"
            "<style>body{margin:0;background:#000}</style>"
            "<canvas id='stage' width='1920' height='1080'></canvas>"
            "<script src='%s'></script><script src='%s'></script>"
            "<script>"
            "window.DURATION=2.0;"
            "window.FK.watch('never.key','从没露面的文字');"
            "window.FK.scene('bad',0.0,2.0,function(S){"
            "  S.update(function(t){"
            "    window.FK.g.text('重叠甲',400,500,{size:90,key:'ov.a',group:'ov.a'});"
            "    window.FK.g.text('重叠乙',420,520,{size:90,key:'ov.b',group:'ov.b'});"
            "    window.FK.g.text('出画文字',1990,500,{size:60,key:'off.a',group:'off.a'});"
            "    if(t>0.45&&t<0.55) window.FK.g.text('一闪而过',800,800,{size:60,key:'brief.a',group:'brief.a'});"
            "  });"
            "});"
            "window.FK.boot();"
            "</script>" % (eng, qat),
            encoding="utf-8")
        bad_qa = {}
        try:
            bad_qa = qa_text(str(bad_page), 0.0, 2.0, 0.1)
        except Exception as exc:        # noqa: BLE001
            bad_qa = {"_error": "%s: %s" % (type(exc).__name__, str(exc)[:200])}
        err = bad_qa.get("_error")
        bad_ok = (not err and bad_qa.get("overlaps") and bad_qa.get("offscreen")
                  and bad_qa.get("brief") and bad_qa.get("never"))
        check("qaText 能报出重叠/出画/一闪而过/从没露面（故意做坏的页面）",
              bool(bad_ok),
              err or ("重叠 %d、出画 %d、一闪而过 %d、从没露面 %d"
                      % (len(bad_qa.get("overlaps") or []), len(bad_qa.get("offscreen") or []),
                         len(bad_qa.get("brief") or []), len(bad_qa.get("never") or []))))

        # 坏页面 3b：CLI textqa 遇到这四类必须 exit 1（只把重叠/出画算失败就漏了后两类）
        cli_bad = None
        try:
            cp = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "textqa", str(bad_page),
                 "--from", "0", "--to", "2", "--step", "0.1"],
                capture_output=True, timeout=180)
            cli_bad = (cp.returncode, (cp.stdout or b"").decode("utf-8", "replace"))
        except Exception as exc:        # noqa: BLE001
            cli_bad = (-1, "%s: %s" % (type(exc).__name__, exc))
        check("CLI textqa 对四类缺陷 exit 1", cli_bad[0] == 1,
              "exit=%s；%s" % (cli_bad[0], (cli_bad[1] or "").strip().splitlines()[0][:120]
                               if cli_bad[1] else "无输出"))

        reqs = probe.get("requests") or []
        external = [u for u in reqs if not u.startswith(("file:", "data:", "blob:", "about:"))]
        check("页面加载零外部网络请求", (not external) and not perr,
              perr or ("%d 个请求全部本地（file://）" % len(reqs) if not external else "外链：" + "、".join(external[:3])))

        enc_ok2 = False
        enc_detail = perr or "无截图数据"
        if probe.get("seq"):
            try:
                fps_now = float(probe.get("fps") or FPS)
                mp4 = tmp_root / "selftest_12f.mp4"
                _encode_pngs(probe["seq"], str(mp4), fps_now)
                info = _probe_media(mp4)
                streams = [s for s in (info.get("streams") or []) if s.get("codec_type") == "video"]
                vs = streams[0] if streams else {}
                nbf = int(vs.get("nb_frames") or 0)
                dur = float(info.get("format", {}).get("duration") or 0.0)
                enc_ok2 = (vs.get("width") == W and vs.get("height") == H and nbf == 12
                           and abs(dur - 12.0 / fps_now) < 0.2)
                enc_detail = "%dx%d、%d 帧、%.3fs、%s fps" % (
                    vs.get("width"), vs.get("height"), nbf, dur, vs.get("r_frame_rate"))
            except Exception as exc:
                enc_detail = "%s: %s" % (type(exc).__name__, str(exc)[:300])
        check("ffmpeg image2pipe(PNG) → libx264 编码烟测（12 帧）", enc_ok2, enc_detail)

        sheet_ok = False
        sheet_detail = perr or "无截图数据"
        if probe.get("pngs"):
            try:
                pngs = probe["pngs"]
                files = []
                for f in sorted(pngs):
                    p = tmp_root / ("f%03d.png" % f)
                    p.write_bytes(pngs[f])
                    files.append(str(p))
                sheet = tmp_root / "sheet.png"
                _K.contact_sheet(files, str(sheet), cols=2, thumb_w=480)
                sheet_ok = sheet.is_file() and sheet.stat().st_size > 1000
                sheet_detail = "%d bytes" % (sheet.stat().st_size if sheet.is_file() else 0)
            except Exception as exc:
                sheet_detail = "%s: %s" % (type(exc).__name__, str(exc)[:300])
        check("联系表（filmkit.contact_sheet）能生成", sheet_ok, sheet_detail)

        check("页面声明的 DURATION / FPS 有效",
              float(probe.get("duration") or 0) > 0 and float(probe.get("fps") or 0) > 0,
              "DURATION=%s，FPS=%s" % (probe.get("duration"), probe.get("fps")))
    finally:
        shutil.rmtree(str(tmp_root), ignore_errors=True)

    n_pass = sum(1 for ok in results if ok)
    n_fail = len(results) - n_pass
    elapsed = time.perf_counter() - t_start
    print("-" * 78)
    print("自检结果：%d 项，PASS %d，FAIL %d，耗时 %.1fs" % (len(results), n_pass, n_fail, elapsed))
    if n_fail == 0:
        print("结论：全部通过（同一帧两次截图 byte 级一致，浏览器逐帧渲染可复现）")
    else:
        print("结论：有失败项，见上方 [FAIL]")
    return 0 if n_fail == 0 else 1


# ---------------------------------------------------------------- CLI
def _parse_times(text: str) -> list:
    out = []
    for part in str(text or "").split(","):
        part = part.strip()
        if part:
            out.append(float(part))
    return out


def _parse_frames(text: str) -> list:
    """解析 --frames 45,270（帧号，整数，逗号分隔）；非法项给可操作报错。"""
    out = []
    for part in str(text or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError:
            raise ValueError("--frames 里有不是整数的项：%r（要逗号分隔的帧号，如 45,270）" % (part,)) from None
    return out


def _resolve_style_spec(text):
    """解析 --style 的**两种**写法：内联 JSON / JSON 文件路径。

    返回 normalize_style 能吃的 dict；空字符串返回 None（= 不注入）。
    解析失败一律 SystemExit（CLI 场景，带可操作提示），不做静默回落。

    不接受"风格 id"这种写法：没有内置外观表，`--style magazine` 没有对象可指。
    与其抛一个"没有风格 X"的错，不如直接把两种合法写法和替代方案说清楚。
    """
    s = str(text or "").strip()
    if not s:
        return None
    if s.startswith("{"):
        try:
            return json.loads(s)
        except json.JSONDecodeError as exc:
            raise SystemExit("[错误] --style 的内联 JSON 解析失败：%s" % exc) from exc
    p = Path(s).expanduser()
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SystemExit("[错误] --style 的 JSON 文件 %s 解析失败：%s" % (p, exc)) from exc
    raise SystemExit(
        "[错误] --style %r 不是 JSON、也不是存在的文件——而**按内置风格 id 取外观这条路已经没有了**。\n"
        "  本技能不提供内置风格/主题表。现在注入外观只有两种写法：\n"
        "    ① --style '{\"theme\": {...}, \"canvas\": {...}}'        # 内联 JSON\n"
        "    ② --style work/style.json                             # 由 render.py --stage style 落盘\n"
        "  想生成一套新外观：python propose.py --brief \"题材\" --script film.json\n"
        "  详细契约见 references/web-engine.md 的 __STYLE__ 段。" % (s,))


def _add_style_args(p, *, fps=True):
    """给子命令加风格参数（缺省 = 不注入、1920x1080）。"""
    p.add_argument("--style", default="",
                   help="外观载荷：work/style.json / 内联 JSON；缺省不注入"
                        "（不接受风格 id：内置风格表已删除）")
    p.add_argument("--width", type=int, default=W,
                   help="画布宽（16:9 横屏 / 9:16 竖屏且偶数，默认 %d）" % W)
    p.add_argument("--height", type=int, default=H,
                   help="画布高（与 --width 一起决定画幅；默认 %d）" % H)
    if fps:
        p.add_argument("--fps", type=float, default=None,
                       help="写进 __STYLE__.canvas.fps 的帧率（缺省不约束，页面 window.FPS 为准）")


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="webengine.py",
        description="浏览器逐帧渲染引擎：Canvas 页面 → CDP 截图 → ffmpeg → mp4（支持风格包注入）",
    )
    ap.add_argument("--selftest", action="store_true", help="跑自检（不联网，≤60 秒）")
    sub = ap.add_subparsers(dest="cmd")

    s = sub.add_parser("snap", help="截取指定时刻的静帧并拼联系表")
    s.add_argument("html", nargs="?", default=str(DEFAULT_HTML), help="页面路径（缺省用 assets/web/index.html）")
    s.add_argument("--times", default="", help="逗号分隔的秒数，如 3,9.5,13")
    s.add_argument("--every", type=float, help="等间隔采样（秒）")
    s.add_argument("--from", dest="t_from", type=float, default=0.0)
    s.add_argument("--to", dest="t_to", type=float)
    s.add_argument("--out", default="", help="输出目录（缺省 ./snaps）")
    s.add_argument("--tag", default="", help="文件名前缀")
    s.add_argument("--deterministic", action="store_const", const=True, default=None,
                   help="软件光栅（跨浏览器会话 byte 级可复现，慢约 20%%）")
    _add_style_args(s)

    fr = sub.add_parser("frames", help="指定帧号截 PNG（打印 SHA-256，用于像素回归对比）")
    fr.add_argument("html", nargs="?", default=str(DEFAULT_HTML))
    fr.add_argument("--frames", required=True, help="逗号分隔的帧号，如 45,270")
    fr.add_argument("--out", default="", help="输出目录（缺省 ./frames）")
    fr.add_argument("--tag", default="", help="文件名前缀")
    fr.add_argument("--deterministic", action="store_const", const=True, default=None)
    _add_style_args(fr)

    c = sub.add_parser("cues", help="导出 cue / 视觉事件（契约 §4.2 schema）")
    c.add_argument("html", nargs="?", default=str(DEFAULT_HTML))
    c.add_argument("--out", default="", help="写到 JSON 文件；缺省打到标准输出")
    c.add_argument("--deterministic", action="store_const", const=True, default=None)
    _add_style_args(c, fps=False)

    f = sub.add_parser("full", help="并行渲染整片（分块 + 断点续跑 + 缺口检测）")
    f.add_argument("html", nargs="?", default=str(DEFAULT_HTML))
    f.add_argument("--out", required=True, help="输出 mp4 路径")
    f.add_argument("--duration", type=float, default=None, help="成片秒数（缺省读页面 DURATION）")
    f.add_argument("--workers", type=int, default=2, help="并行浏览器数（默认 2）")
    f.add_argument("--chunk-s", type=float, default=30.0, help="每块秒数（默认 30）")
    f.add_argument("--resume", action=argparse.BooleanOptionalAction, default=True,
                   help="断点续跑：已存在的块跳过（--no-resume 强制重渲）")
    f.add_argument("--from", dest="t_from", type=float, default=0.0, help="起始秒（预览片段用）")
    f.add_argument("--to", dest="t_to", type=float, default=None, help="结束秒（缺省到片尾）")
    f.add_argument("--crf", type=int, default=17)
    f.add_argument("--preset", default="slow")
    f.add_argument("--deterministic", action="store_const", const=True, default=None,
                   help="软件光栅（跨浏览器会话 byte 级可复现，慢约 20%%）")
    _add_style_args(f)

    q = sub.add_parser("textqa", help="浏览器端文字质检（重叠 / 出画 / 一闪而过 / 从没露面）")
    q.add_argument("html", nargs="?", default=str(DEFAULT_HTML))
    q.add_argument("--from", dest="t0", type=float, default=0.0)
    q.add_argument("--to", dest="t1", type=float, default=None)
    q.add_argument("--step", type=float, default=0.2,
                   help="采样步长秒；必须 < 0.6（一闪而过的判据阈值），默认 0.2")
    q.add_argument("--out", default="", help="把完整结果写到 JSON")
    q.add_argument("--deterministic", action="store_const", const=True, default=None)
    _add_style_args(q)

    sub.add_parser("selftest", help="跑自检（等价于 --selftest）")
    return ap


def _json_dump(data, path: str) -> Path:
    dst = Path(path).expanduser()
    if not dst.is_absolute():
        dst = (Path.cwd() / dst).resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return dst


def main(argv=None) -> int:
    _setup_stdout()
    args = _build_parser().parse_args(argv)
    if args.selftest or args.cmd == "selftest":
        return selftest()
    if args.cmd is None:
        _build_parser().print_help()
        return 2

    # --style 统一在分支之前解析：内联 JSON / JSON 文件 / 风格 id 三种写法，解析失败算用法
    # 错误（exit 2）。每个分支各自解析时，坏 id 会抛 SystemExit 变成 exit 1，脚本里分不清
    # "参数写错"和"渲染失败"。
    try:
        style_spec = _resolve_style_spec(getattr(args, "style", ""))
    except SystemExit as exc:
        msg = exc.code if isinstance(exc.code, str) else ("[错误] %s" % (exc.code,))
        print(msg, flush=True)
        return 2

    if args.cmd == "snap":
        try:
            times = _parse_times(args.times)
        except ValueError:
            print("[错误] --times 里有不是数字的项：%r（要逗号分隔的秒数，如 3,9.5,13）"
                  % (args.times,), flush=True)
            return 2
        if args.every is not None and float(args.every) <= 0:
            # --every 0 会被当成"没写"、--every -1 则是死循环：先 page_meta 开一次浏览器，
            # 之后 while k <= end 永远成立，times 一路追加到内存爆掉（实测 40 s 都出不来）。
            print("[错误] --every 必须是正数秒（当前 %r）；不想等间隔采样就别传 --every" % (args.every,),
                  flush=True)
            return 2
        if args.every:
            end = args.t_to if args.t_to is not None else page_meta(
                args.html, style=style_spec, width=args.width, height=args.height,
                fps=args.fps)[0]
            k = float(args.t_from)
            while k <= end + 1e-9:
                times.append(round(k, 3))
                k += float(args.every)
        if not times:
            print("[错误] snap 需要 --times 3,9.5 或 --every 2 [--to 12]", flush=True)
            return 2
        out_dir = args.out or str(Path.cwd() / "snaps")
        try:
            snap(args.html, times, out_dir, tag=args.tag, deterministic=args.deterministic,
                 style=style_spec, width=args.width, height=args.height, fps=args.fps)
        except (ValueError, TypeError) as exc:
            print("[错误] %s" % exc, flush=True)
            return 2
        return 0

    if args.cmd == "frames":
        try:
            frames = _parse_frames(args.frames)
        except ValueError as exc:
            print("[错误] %s" % exc, flush=True)
            return 2
        if not frames:
            print("[错误] frames 需要 --frames 45,270（逗号分隔的帧号）", flush=True)
            return 2
        out_dir = args.out or str(Path.cwd() / "frames")
        try:
            results = render_frames(args.html, frames, out_dir, tag=args.tag,
                                    style=style_spec, width=args.width, height=args.height,
                                    fps=args.fps, deterministic=args.deterministic)
        except (ValueError, TypeError) as exc:
            print("[错误] %s" % exc, flush=True)
            return 2
        for r in results:
            print("[frames] f=%d %s  sha256=%s  %d bytes  → %s"
                  % (r["f"], "fps=%g" % r["fps"], r["sha256"], r["bytes"], r["path"]), flush=True)
        return 0

    if args.cmd == "cues":
        try:
            data = export_cues(args.html, deterministic=args.deterministic, style=style_spec,
                               width=args.width, height=args.height)
        except (ValueError, TypeError) as exc:
            print("[错误] %s" % exc, flush=True)
            return 2
        except RuntimeError as exc:
            # 页面契约 / 浏览器起不来这类错误（缺 getCues、ready 超时、缺 playwright…）：
            # 直接抛 traceback 会变成 exit 1，且报错淹没在栈里。这是"要改页面/环境"的用法
            # 错误，和 --style 写坏一样给 exit 2 + 一行可读原因。
            print("[错误] %s" % exc, flush=True)
            return 2
        if args.out:
            dst = _json_dump(data, args.out)
            print("[cues] %d 个 cue、%d 个视觉事件 → %s"
                  % (len(data["cues"]), len(data["events"]), dst), flush=True)
        else:
            print(json.dumps(data, ensure_ascii=False, indent=1), flush=True)
        return 0

    if args.cmd == "full":
        try:
            out = render_full(
                args.html, args.out, args.duration,
                workers=args.workers, chunk_s=args.chunk_s, resume=args.resume,
                from_s=args.t_from, to_s=args.t_to, crf=args.crf, preset=args.preset,
                deterministic=args.deterministic,
                style=style_spec, width=args.width, height=args.height, fps=args.fps,
            )
        except (ValueError, TypeError) as exc:
            print("[错误] %s" % exc, flush=True)
            return 2
        print("[done] %s" % out, flush=True)
        return 0

    if args.cmd == "textqa":
        try:
            result = qa_text(args.html, args.t0, args.t1, args.step,
                             deterministic=args.deterministic, style=style_spec,
                             width=args.width, height=args.height, fps=args.fps)
        except (ValueError, TypeError) as exc:
            print("[错误] %s" % exc, flush=True)
            return 2
        except RuntimeError as exc:
            # 同 cues：页面 qaText 返回值形状不对 / 契约缺失 / 浏览器起不来都属于"要改页面
            # 或环境"，给 exit 2 + 可读原因；别让一个 traceback 决定门禁红绿。
            print("[错误] %s" % exc, flush=True)
            return 2
        print("[textqa] 重叠 %d、出画 %d、一闪而过 %d、从没露面 %d"
              % (len(result.get("overlaps") or []), len(result.get("offscreen") or []),
                 len(result.get("brief") or []), len(result.get("never") or [])), flush=True)
        for item in (result.get("overlaps") or [])[:5]:
            print("  重叠 %.2f-%.2fs ratio=%.2f  %s" % (item["at"], item["to"], item["ratio"], item["pair"]), flush=True)
        for item in (result.get("offscreen") or [])[:5]:
            print("  出画 %.2f-%.2fs  %s  %s" % (item["at"], item["to"], item["el"], item["box"]), flush=True)
        for item in (result.get("never") or [])[:8]:
            print("  从没露面：%s" % item, flush=True)
        if args.out:
            print("[textqa] 详情 → %s" % _json_dump(result, args.out), flush=True)
        # 四类缺陷都算没通过：重叠/出画是布局错，一闪而过/从没露面同样会让成片看起来"漏字"。
        # 只看前两类的话，后两类会被打印出来却 exit 0 —— 门禁形同建议。
        bad = (bool(result.get("overlaps")) or bool(result.get("offscreen"))
               or bool(result.get("brief")) or bool(result.get("never")))
        return 1 if bad else 0

    _build_parser().print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
