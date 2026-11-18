# -*- coding: utf-8 -*-
"""layoutkit.py — 画布与安全区的**单一真源**。

为什么单独一个模块
------------------
这些数字以前散落在 `filmkit` 顶部三个常量里（`W, H` / `SAFE` / `SUB_BAND`），
而 `assets/scenes.py`、`preflight`、`qa_frames` 各自 `from filmkit import ...` 复制一份值。
于是"改画布尺寸"不是改一个数，而是**找到所有复制过它的地方**；而"竖屏"恰好就是
改画布尺寸——所以它一直没被做，理由写成"安全区/版式按横屏像素写死"。

这里做的第一件事很小：**把这三个常量收成一份**，默认 profile 的数值与原来逐字节相同。
验收口径就是 `golden_frames --check` 三帧像素一致——不是"看起来一样"。

怎么选（**必须在任何 import scenes/filmkit 之前**）
------------------------------------------------
    VIDEO_STUDIO_LAYOUT=portrait-9x16    # render.py --layout portrait 会替你设这个变量

为什么是环境变量而不是函数参数：`scenes.py` 写的是 `from filmkit import W, H`，
值在**导入那一刻**就绑死了。想让它变，只能在导入之前定死——环境变量正好是这个时机的
自然载体，也不会让 10 个构图函数都多一个 profile 参数（那会改掉 draw_frame 的签名契约）。

竖屏的现状（务必读完再判断能不能用）
-----------------------------------
* 安全区、字幕带、画布尺寸**已经按竖屏算好**（1080x1920，字幕带上移避开系统 UI 区）。
* **版式骨架**与**重点构图内部**都已竖屏原生：`stylekit.LAYOUTS_PORTRAIT` 给三套骨架各一套
  1080x1920 坐标，`compare`（上下对照）/ `process`（竖向轴）/ `chart`（竖向条）各有专属
  绘制分支，竖屏不再靠横屏几何等比折算。
* 剩下的边界是**节奏**而不是方向：`hold` / 段落最小长度按横屏调过，`estimate.py` 的
  耗时基准也是 1080p 口径。所以竖屏当前仍是 **experimental**：能出片、能过 preflight 字形
  检测、10 类构图各有金帧像素基线，但**排版节奏未按竖屏复核**，不建议直接交付。
  `selfcheck` 第 75/76 条守住"竖屏下每类构图的内容不越出安全区"与"10 类构图各有像素基线"。

本模块只依赖标准库：任何脚本（含 preflight / qa）都可以安全 import 它。
"""
from __future__ import annotations

import os

# ---------------------------------------------------------------- 横屏 16:9（默认，唯一在用）
# 这些数值**不要单独改**：改画布要连 SAFE / SUB_BAND 一起重算，并且必须重录金帧。
LANDSCAPE = {
    "key": "landscape-16x9",
    "w": 1920,
    "h": 1080,
    # 正文安全区：左右各 64、上 58、下 106（留出字幕带）
    "safe": (64, 58, 1856, 974),
    # 字幕带：两行 + 距画面底部 50px，永不被裁
    "sub_band": (180, 872, 1740, 1030),
    # 单行字幕中文字数上限（超过就折行，折行会改变排版本，属于版式契约）
    "sub_max_chars": 24,
    # 字号档：横屏按 1920 宽标定，1.0 = 原样。
    "type_scale": 1.0,
}

# ---------------------------------------------------------------- 竖屏 9:16（experimental）
# 安全区按"短边 6%"给，字幕带整体上移：竖屏的底部 15% 通常被系统 UI / 进度条 / 评论区盖住，
# 而横屏的 SUB_BAND 是贴着画面底边的（1030/1080）。照抄横屏值 = 字幕被挡。
PORTRAIT = {
    "key": "portrait-9x16",
    "w": 1080,
    "h": 1920,
    "safe": (65, 120, 1015, 1610),
    "sub_band": (80, 1450, 1000, 1616),
    "sub_max_chars": 14,      # 竖屏一行装不下 24 个中文字，14 字接近满行
    # 字号档（竖屏）：判据是**可读性**，不是几何比例。
    # designkit 的字号盘以 body=34px 为基准（见 designkit._type_scale 的注释）；
    # 纯几何缩放（W/1920=0.5625）会让竖屏正文只有 ~19px，手机上必须凑近看。
    # MIN_BODY_PX=30 是下限，反推 type_scale >= 30/34 ≈ 0.882。
    # 0.89 把 34px 的 body 抬到 30.3px，标题 2 行、正文 1 行（实测最长句）；
    # 0.96 会让标题涨到 3 行——所以取 0.89，而不是取整到 0.9（0.9 也合格，但没余量）。
    "type_scale": 0.89,
}

_BY_KEY = {p["key"]: p for p in (LANDSCAPE, PORTRAIT)}
_ALIASES = {
    "landscape": "landscape-16x9", "16x9": "landscape-16x9", "horizontal": "landscape-16x9",
    "portrait": "portrait-9x16", "9x16": "portrait-9x16", "vertical": "portrait-9x16",
}


def _select():
    """按 VIDEO_STUDIO_LAYOUT 选 profile；未设置就是横屏（与历史行为一致）。"""
    raw = (os.environ.get("VIDEO_STUDIO_LAYOUT") or "").strip().lower()
    if not raw:
        return LANDSCAPE
    key = _ALIASES.get(raw, raw)
    if key not in _BY_KEY:
        raise RuntimeError("未知 VIDEO_STUDIO_LAYOUT=%r（可用：%s）"
                           % (raw, ", ".join(sorted(_BY_KEY))))
    return _BY_KEY[key]


# 当前生效的 profile。**导入期定死**：`scenes.py` 的 `from filmkit import W, H` 会把值
# 绑到模块上，运行期再改只会得到"用 A 尺寸排版、用 B 尺寸绘制"的半新半旧状态。
PROFILE = _select()

W = int(PROFILE["w"])
H = int(PROFILE["h"])
SAFE = tuple(PROFILE["safe"])
SUB_BAND = tuple(PROFILE["sub_band"])
SUB_MAX_CHARS = int(PROFILE["sub_max_chars"])
# 字号档：竖屏比几何缩放略大（见 profile 注释）。scenes 的 _font 读它。
TYPE_SCALE = float(PROFILE.get("type_scale", 1.0))

# 竖屏正文的最小可读字号（px）。这是竖屏字号档的**判据**，不是观感描述：
# 手机上正文小于 ~30px 就需要凑近看（1080 宽、6 英寸屏、约 30cm 视距的经验下限）。
# designkit 的 body 档在 1080p 横屏下约 46px，所以竖屏 type_scale 至少要 30/46≈0.65。
MIN_BODY_PX = 30.0


def body_px(base_px=34.0):
    """当前 profile 下正文字号的实际像素（base_px 是横屏 body 档的设计值）。"""
    return float(base_px) * float(TYPE_SCALE)


def profile(key=None):
    """取一份 profile 的**副本**。

    给副本而不是给本体：调用方改自己的局部变量不该污染全局（`PROFILE` 是指针，
    直接把它交出去等于把单一真源变成"谁都能改的共享状态"）。
    """
    src = PROFILE if key is None else _BY_KEY.get(str(key))
    if src is None:
        raise KeyError("未知 layout profile %r（可用：%s）" % (key, ", ".join(sorted(_BY_KEY))))
    return dict(src)




def verify_defaults():
    """自检用：把"本模块导出的值"与"历史常量"逐项比对。

    历史常量写死在这里而不是从 filmkit 读——如果从 filmkit 读，两边一起改就永远
    对得上，这条断言也就永远绿（这正是 selfcheck 第 25 条踩过的坑：拿同一个对象比自己）。
    """
    want = {
        "w": 1920, "h": 1080,
        "safe": (64, 58, 1856, 974),
        "sub_band": (180, 872, 1740, 1030),
        "sub_max_chars": 24,
    }
    got = {"w": W, "h": H, "safe": SAFE, "sub_band": SUB_BAND,
           "sub_max_chars": SUB_MAX_CHARS}
    bad = [k for k in want if got[k] != want[k]]
    if bad:
        raise AssertionError("; ".join("%s: %r != %r" % (k, got[k], want[k]) for k in bad))
    return "landscape-16x9: %dx%d / safe %s / band %s" % (W, H, SAFE, SUB_BAND)
