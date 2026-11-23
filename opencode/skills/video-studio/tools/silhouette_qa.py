# -*- coding: utf-8 -*-
"""silhouette_qa.py — 跨设计剪影可区分检查（S13）。

注：本标识是**离线检查编号**，不进 render.py 的 gate matrix（账本里的 G13 是
"版式文字冲突"，见 scripts/qatext.py）。本工具产出的是人工/AI 复核用的 JSON 与表格，
不被任何 stage 记进 work/gates.json；以前它自称 G13，导致 QC 报告里的 G13 被误读。

解决什么问题
------------
用户原则 #5："不同视频的整体风格不能大面积/长时间重复。" 颜色差异肉眼明显、结构差异却
从来没人量过：同一批绘制器与版式几何下，换一套皮肤（配色 / 字体 / 纹理 / 字距）之后，
灰度三值化后的**构图骨架**（前景占比、重心、边缘占用、左右对称、纵向明暗剖面）是否真的
变了？本工具把"骨架到底有没有变"变成可复核的数字，而不是审美口水。

输入从哪来
----------
技能里没有内置风格表（stylekit.STYLE_IDS == ()），"同一剧本换 style"这种输入不存在。
"多部片"来自 **designkit 现场采样**：同一 brief 不同 seed（或不同 brief）得到 N 个设计，
每个设计是一整条独立外观（配色/母题/版式/HUD/字体族/字号比例/字距/行距/重心偏移/转场/缓动/编号写法…）。
所以本工具比的是 **N 个设计之间两两比较**——这才对应"不同视频会不会长得一样"。

算法（验收口径；改阈值 / 改指标之前先读这一节）
----------------------------------------------
对每一对设计 (A, B)：
  1. 用同一份合成剧本（title="剪影校验"，固定 3 段内容，每段 dur=4.0s），只换设计；
     对每个场景类型（默认 hero/list/stat）各渲 3 帧：t_local = 0.05 / 0.5 / 0.9 × dur。
     frame = int(t_global * fps)，fps=30；渲染直接调 assets/scenes.py 的 draw_frame，
     **不叠字幕、不经过 ffmpeg** —— 本工具量的是"渲染器画出来的构图"，不是成片编码。
     合成剧本的 scene 同时写 kind / type / scene_type（新旧绘制器都能认出场景类型），
     并补上 stats / sides / steps / series / quote 等内容字段：同一段内容在不同版本的
     绘制器下都有素材可画，才有资格比较"构图是不是同一张骨架"。
     scene_layout 取该设计的 plan["layout_default"]：设计说"标题居中"就该渲成居中，
     否则版式差异会被强行抹平成 left-stack，等于把"设计意图"从判据里删掉。
  2. 灰度化（PIL 的 ITU-R 601 亮度）+ 降采样到 480×270（box）—— 低分辨率压掉字体、
     纹理、抗锯齿这些"皮肤级"差异，留下块状结构。
  3. 三值化：基准亮度 b = 本帧**四角各 40×40 块灰度中位数**。用四角而不是主题 bg 色，
     是因为深浅主题的 bg 亮度本来就差一个数量级；四角在本项目里永远是背景（正文与装饰
     都在安全区内），拿它当"本帧背景亮度"跨深浅主题才是同一把尺子。d = |gray - b|：
       d < 15        -> 0 背景
       15 <= d < 60  -> 1 中间（纹理、半透明卡片、渐变、抗锯齿边）
       d >= 60       -> 2 前景（文字、实心图形）
  4. 六项指标（全部在 480×270 上算，百分比相对画面）：
       F     前景档(2) 像素占比 × 100
       Cx/Cy 前景像素质心 x / y（0..100，按像素中心口径）
       B     四条边各 6% 带宽内"非背景档(1 或 2)"覆盖率的最大值 × 100
       S     左右半幅前景掩码的 IoU：**右半幅水平翻转后**再与左半幅比。不翻转就不是
             "对称度"，而是"左右两半同一相对位置都有前景"——那对居中标题恒为 0。
       P     垂直 9 带平均灰度剖面减自身中位数后，两帧逐带差的最大值。Why：F/Cx/Cy/B/S
             都是"按前景像素算"的指标，而前景档要求 |gray-b|>=60；letterbox 的上下黑边
             与近黑主题底色只差 ~9，**黑边被算成背景，cinema 最标志性的结构在判据里根本
             不存在**。P 量的是"画面纵向明暗分布"，不受字体影响。
  5. 判定"该场景可区分"：任一项达标即可 ——
       ΔF >= 6pp、|ΔCx| >= 8、|ΔCy| >= 8、ΔB >= 14、ΔS >= 0.20、ΔP >= 6.0。
     关键约束：**同一项指标必须在 3 帧上全部达标**。挑单帧最容易骗过门禁：入场瞬间
     两个设计的位移差本来就大，只看一帧会把"同一骨架 + 不同缓动"误判成结构不同。
  6. 一对设计在**任一场景类型**上可区分即算 PASS；所有场景都不达标才算 FAIL。

同族 vs 跨族（口径局限，别读错数）
----------------------------------
B 的基准是四角中位数：浅色主题整屏 bg→bg2 渐变本身就可能超过 15 的中间档门槛，整条边带
都被判"非背景"，于是浅 vs 深的 ΔB 往往只是背景亮度差，不是构图差。报告按**明暗族**分组
统计：同族通过数才是"骨架真的不同"的保守下界；跨族且只靠 ΔB 通过的对会被单独点名。
这是口径本身的局限，不是实现 bug —— 改判定线要单独评审，不能靠这里偷偷放宽/收紧。

注意：本工具**只量骨架，不量配色**。不同设计之间颜色必然不同（连续性色相空间），
但"颜色不同"不是原则 #5 的证据；所以判定完全由灰度结构指标给出，"颜色差异"不计分。

输出与退出码
------------
  * 人读表格：设计对 × 场景类型 × 六项差值（按帧列出）+ PASS/FAIL（靠哪项达标）
  * 汇总：N 对设计、通过 M 对，失败对按"最接近阈值"排序（便于优先修）
  * --json 输出机读结构（含每对、每场景、每帧的指标明细），供门禁消费
  * 退出码：全部通过 0；有失败 1；--strict 下存在跳过（skip）时 2。
    跳过 = 帧渲不出来 / 指标缺值，**skipped != 通过**：它只说明这次没证据，不说明
    骨架相同；--strict 用于 CI 里把"没跑成"也当红。

CLI（没有 --styles：STYLE_IDS 是空元组，按风格 ID 选已经无意义）
------------------------------------------------------------------------------------------
    python tools/silhouette_qa.py [--designs 3] [--seed 1] [--brief "题材/要求"]
                                  [--design-json PATH]
                                  [--scenes hero,list,stat]
                                  [--frames 0.05,0.5,0.9]
                                  [--json] [--strict] [--out PATH]
                                  [--scenes-py PATH] [--skill-root DIR] [--selftest]
    --designs        现场采样几个设计（>=2，缺省 3；两两比较）
    --seed           采样种子：固定值 = 可复现（缺省 1，CI 用它）；传 -1 / random = 每次新设计
    --brief          采样用的 brief 文本（缺省内置的一句话题材，不联网、不需要用户输入）
    --design-json    诊断用：不采样，直接读 design.json（单设计 / 数组 / {"designs":[...]}）；
                     给 1 个设计时只能做确定性自检，给 >=2 个才做两两比较
    --scenes         场景类型（逗号分隔，默认 hero,list,stat）
    --scenes-py      诊断用：换一份 scenes.py 渲染（默认 <skill-root>/assets/scenes.py）
    --skill-root     诊断用：改用含 scripts/ 与 assets/ 的另一份技能快照（如 git archive
                     出来的改造前版本），便于"改造前 vs 改造后"用同一个工具对比；
                     JSON 的 inputs 会记录两边的 sha1，避免比错文件
    --out            额外落一份 JSON（缺省只打印，不写文件）
    --strict         有跳过时退出码 2（缺省跳过不影响退出码，但报告里一定单独列出）
    --selftest       自检（离线，<=60s）：合成 2 组设计并渲染，断言
                     ① 不同设计之间可区分（PASS）
                     ② 同一设计重复渲染逐像素一致（PASS，确定性）
                     ③ 负控制：同一设计自己跟自己比必须 FAIL（否则门禁恒真，等于没门禁）
                     全过退出 0，任一不过退出 1。ci_check 会调用它。

依赖：stdlib + numpy + Pillow + 同仓库的 designkit / stylekit / filmkit / scenes
（项目已有依赖，无新增）；不依赖 TTS / ffmpeg / 网络，纯本地渲染。
"""
from __future__ import annotations

__version__ = "1.1.0"

import argparse
import copy
import hashlib
import importlib.util
import itertools
import json
import os
import sys
import time
import traceback

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SKILL = ROOT
SCRIPTS = os.path.join(SKILL, "scripts")
ASSETS = os.path.join(SKILL, "assets")
DEFAULT_SCENES_PY = os.path.join(ASSETS, "scenes.py")

RESULT_VERSION = 2
FPS = 30
SCENE_DUR = 4.0
DEFAULT_FRACS = (0.05, 0.5, 0.9)
DEFAULT_SCENE_TYPES = ("hero", "list", "stat")
SYNTH_TITLE = "剪影校验"
# 现场采样的默认数：3 个设计 = 3 对，足够回答"会不会长得一样"，又比 4 个少 1/3 渲染。
DEFAULT_DESIGNS = 3
# 默认固定种子：门禁的结论必须可复现（同一条命令两次跑出同一个 PASS/FAIL）。
# 想要"每次新设计"就传 --seed -1；CI 与 selfcheck 走缺省值。
DEFAULT_SEED = 1
# "每次新设计"的几种写法。只此一处真值：_seed_arg 直接用它，避免 help 文案、
# 解析器与常量三处各写一份（改一处就漂移）。
SEED_WORDS = ("-1", "random", "auto", "none")
# 采样 brief：一句话题材即可，采样器只把它当"给什么题材做方案"的背景信息。
DEFAULT_BRIEF = "剪影校验：同一份内容，多套现场采样的设计"

# 量化口径（480×270 是本工具定的"结构分辨率"：再低会把小字抹掉，再高会开始量字体）
QUAL_W, QUAL_H = 480, 270
CORNER = 40
TIER_BG_MAX = 15          # d < 15 -> 背景档 0
TIER_FG_MIN = 60          # d >= 60 -> 前景档 2
EDGE_BAND = 0.06          # 四边各 6% 带宽
METRIC_ORDER = ("F", "Cx", "Cy", "B", "S", "P")
# 达标线：F>=6pp / Cx,Cy>=8 / B>=14 / S>=0.20 / P>=6.0。
# 改这里等于改验收口径，需说明理由；v2.0.0 只换输入（设计而不是风格），不放宽任何一条。
THRESHOLDS = {"F": 6.0, "Cx": 8.0, "Cy": 8.0, "B": 14.0, "S": 0.20, "P": 6.0}
METRIC_LABELS = {
    "F": "前景像素占比(%)", "Cx": "前景质心x(占宽%)", "Cy": "前景质心y(占高%)",
    "B": "四边非背景覆盖率(%)", "S": "左右半幅前景 IoU",
    "P": "垂直明暗剖面最大偏差(灰度级)",
}

# ============================================================================
# 合成剧本：同一份内容，只有设计不同
# ============================================================================
# 每类场景给足该类型的"原料字段"：hero 要 big/sub/kicker，list 要 items，stat 要
# stats 或"数字+单位"，其余类型按 grammar.py 的契约补齐。给全的理由：本工具要能在
# 改造前的旧绘制器、改造后的 10 绘制器上都跑——旧版忽略多余字段，新版缺字段就画空。
# 文案固定不随设计变化，否则"构图差"里会混进"内容差"。
SCENE_CONTENT = {
    "hero": dict(
        kicker="00 · 开场", chapter="01 · 开场",
        big="结构决定第一印象",
        sub="同一份稿子，多套构图骨架",
        text="结构决定第一印象。同一份稿子，多套构图骨架。",
        items=[]),
    "statement": dict(
        kicker="01 · 金句", chapter="01 · 金句",
        big="骨架不同，观感才不同",
        sub="颜色只是皮肤",
        text="骨架不同，观感才不同。颜色只是皮肤。",
        items=[]),
    "list": dict(
        kicker="02 · 要点", chapter="02 · 要点",
        big="三条结构判据",
        sub="只比灰度三值化后的骨架",
        text="三条结构判据：前景占比、质心位移、边缘覆盖与对称度。",
        items=[{"text": "前景占比 F", "ok": True},
               {"text": "质心位移 Cx / Cy", "ok": True},
               {"text": "边缘覆盖 B 与对称度 S", "ok": True}]),
    "stat": dict(
        kicker="03 · 数据", chapter="03 · 数据",
        big="27%",
        sub="同一份稿子的构图差异",
        text="同一份稿子的构图差异 27%",
        items=[],
        stats=[{"value": 27, "unit": "%", "label": "构图差异", "note": "同一稿实测"}]),
    "compare": dict(
        kicker="04 · 对比", chapter="04 · 对比",
        big="改造前后",
        sub="同一段话，两种骨架",
        text="改造前后：同一段话，两种骨架。",
        items=[],
        sides={"left": {"head": "改造前",
                        "items": ["三段共用 body", "结构几乎不变", "只能靠换色区分"]},
               "right": {"head": "改造后",
                         "items": ["十类场景语法", "每类独立骨架", "结构指标可判定"]}}),
    "process": dict(
        kicker="05 · 流程", chapter="05 · 流程",
        big="四步出片",
        sub="从稿件到成片",
        text="四步出片：定稿、分镜、渲染、质检。",
        items=[],
        steps=[{"label": "定稿"}, {"label": "分镜"}, {"label": "渲染"}, {"label": "质检"}]),
    "quote": dict(
        kicker="06 · 引语", chapter="06 · 引语",
        big="颜色只是皮肤",
        sub="结构才是骨架",
        text="「颜色只是皮肤，结构才是骨架」——剪影校验",
        items=[],
        quote="颜色只是皮肤，结构才是骨架",
        by="剪影校验"),
    "chapter": dict(
        kicker="02", chapter="02 · 结构",
        big="第二章 结构",
        sub="",
        text="第二章 结构",
        items=[]),
    "chart": dict(
        kicker="07 · 数据条", chapter="07 · 数据条",
        big="三项结构指标",
        sub="同一份稿子的实测差值",
        text="三项结构指标：前景占比 42，边缘覆盖 31，对称度 0.18。",
        items=[],
        series=[{"label": "前景占比", "value": 42, "unit": "%"},
                {"label": "边缘覆盖", "value": 31, "unit": "%"},
                {"label": "对称度", "value": 0.18, "unit": ""}]),
    "outro": dict(
        kicker="99 · 收尾", chapter="99 · 收尾",
        big="完",
        sub="剪影校验",
        text="完。剪影校验。",
        items=[]),
}
SUPPORTED_SCENE_TYPES = tuple(SCENE_CONTENT)


# ---------------------------------------------------------------- 基础工具
def _setup_stdio():
    """输出被重定向时切 UTF-8（Windows 控制台默认 GBK，中文会乱码）。与 render.py 同口径。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                                   # noqa: BLE001
            pass


def _sha1_file(path):
    """文件内容 sha1（读不了返回 "missing"，不抛异常）。"""
    try:
        h = hashlib.sha1()
        with open(str(path), "rb") as fh:
            for blk in iter(lambda: fh.read(1 << 20), b""):
                h.update(blk)
        return h.hexdigest()
    except OSError:
        return "missing"


def _rel(path):
    """尽量把路径写成相对仓库根的 posix 形式（JSON 要跨机器可读）。"""
    try:
        rel = os.path.relpath(os.path.abspath(str(path)), ROOT)
    except ValueError:                                      # 跨盘符
        return os.path.basename(str(path))
    return rel.replace(os.sep, "/")


def _short_err(exc):
    """异常 -> 一行可读原因（traceback 只留末尾几行，别糊满报告）。"""
    return "%s: %s" % (type(exc).__name__, exc)


def _sha256_image(img):
    """一帧像素的 sha256：先写画布尺寸，再写 RGB 字节（尺寸变了哈希也一定变）。"""
    if img.mode != "RGB":
        img = img.convert("RGB")
    h = hashlib.sha256()
    h.update(("%dx%d|" % tuple(img.size)).encode("ascii"))
    h.update(img.tobytes())
    return h.hexdigest()


# ---------------------------------------------------------------- 依赖装载
def _modules(scripts_dir=SCRIPTS):
    """装载 filmkit / stylekit / designkit。tools/ 与 scripts/ 不同目录，先补 sys.path。

    支持换一个 scripts 目录（--skill-root 诊断用）：先清掉 sys.modules 里的同名条目，
    否则第二次调用拿到的还是上一份模块，"改造前 vs 改造后"会串档。
    """
    scripts_dir = os.path.abspath(str(scripts_dir))
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    for name in ("filmkit", "stylekit", "designkit"):
        if sys.modules.get(name) is not None:
            path = str(getattr(sys.modules[name], "__file__", "") or "")
            if os.path.dirname(os.path.abspath(path)) != scripts_dir:
                del sys.modules[name]
    try:
        import filmkit as K
        import stylekit as ST
        import designkit as DK
        return K, ST, DK
    except Exception as e:                                  # noqa: BLE001
        raise RuntimeError("装载 filmkit/stylekit/designkit 失败（%s）：%s"
                           % (scripts_dir, e)) from e


def _load_scenes(path):
    """按路径装载 scenes.py（独立模块名，避免与站点包里的同名模块串档）。

    scenes.py 自己会 import filmkit / stylekit：装载前把 scripts/ 与 assets/ 都补进
    sys.path，否则换一份 skill-root 时 ModuleNotFoundError（golden_frames 同处理）。
    """
    path = os.path.abspath(str(path))
    if not os.path.isfile(path):
        raise RuntimeError("scenes.py 不存在：%s" % path)
    for p in (os.path.dirname(path), SCRIPTS, ASSETS):
        if p and p not in sys.path:
            sys.path.insert(0, p)
    spec = importlib.util.spec_from_file_location("silhouette_scenes", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("scenes.py 无法作为模块加载：%s" % path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["silhouette_scenes"] = mod
    spec.loader.exec_module(mod)
    if not callable(getattr(mod, "draw_frame", None)):
        raise RuntimeError("scenes.py 缺少 draw_frame(scene, t_local, dur, t_global, "
                           "script, frame) 契约：%s" % path)
    return mod


# ---------------------------------------------------------------- 设计 -> 计划
# 注意：designkit 只经 _modules() 取（见上面那个三件套解包）。以前这里还有一个
# 只取 designkit 的包装 load_designkits()，全仓无人调用——删掉，避免"以为有独立入口"。


def sample_designs(DK, n, seed, brief=None):
    """现场采样 n 个设计（不同 brief 或同 brief 不同 seed，跨设计两两比较）。

    为什么要"现场采样"而不是读一份写死的清单：技能里没有任何内置外观表，
    多部片的差异全部来自 designkit 的连续采样；门禁必须量**采样器真的会产出的东西**。
    seed=None 表示每次新设计（采样器的默认语义）；本工具缺省固定 seed 以保结论可复现。
    """
    b = {"topic": str(brief or DEFAULT_BRIEF)}
    if seed is None:
        return list(DK.sample_designs(b, n=int(n)))
    return list(DK.sample_designs(b, n=int(n), seed=int(seed)))


def _normalize_designs(payload):
    """design.json 可以是单个设计 / 设计数组 / {"designs":[...]} / {"candidates":[...]}。"""
    if isinstance(payload, list):
        designs = payload
    elif isinstance(payload, dict):
        designs = None
        for key in ("designs", "candidates"):
            if isinstance(payload.get(key), list):
                designs = payload[key]
                break
        if designs is None:
            designs = [payload]
    else:
        raise ValueError("design.json 必须是设计对象或设计数组")
    bad = [i for i, d in enumerate(designs) if not isinstance(d, dict)]
    if bad:
        raise ValueError("design.json 里第 %s 项不是对象（设计必须是 dict）"
                         % ", ".join(str(i + 1) for i in bad[:4]))
    if not designs:
        raise ValueError("design.json 里没有任何设计")
    return designs


def _design_ids(designs):
    """设计对外的短 id：用设计自己的 id；重复时加 -2/-3 后缀（采样 id 是 8 位哈希，极少撞）。"""
    ids, used = [], set()
    for i, d in enumerate(designs):
        base = str(d.get("id") or ("design-%02d" % (i + 1)))
        did, k = base, 2
        while did in used:
            did = "%s-%d" % (base, k)
            k += 1
        used.add(did)
        ids.append(did)
    return ids


def design_plan(DK, design):
    """设计 -> 计划（designkit.design_to_plan 已含 validate + theme_lint，失败直接抛）。"""
    plan = DK.design_to_plan(design)
    if not isinstance(plan, dict):
        raise RuntimeError("design_to_plan 返回 %r，不是 dict" % type(plan).__name__)
    return plan


def _family_of(ST, plan):
    """明暗族：dark / light。只作诊断分组，不参与判定（见模块文档"口径局限"）。"""
    th = plan.get("theme")
    try:
        if isinstance(th, dict) and th:
            return "dark" if ST.is_dark(th) else "light"
    except Exception:                                       # noqa: BLE001 —— 分组失败不该毁门禁
        pass
    return "unknown"


# ---------------------------------------------------------------- 合成剧本 / 场景
def build_script(design_id, scene_types):
    """造一份"只有设计不同"的合成剧本（title/lines 固定，dur 固定 4.0s）。

    style 字段不写任何风格 id（没有内置风格表）；渲染用的计划由
    run_gate 塞进 script["_style"]，scenes.draw_frame 优先读它。
    """
    lines = []
    for i, stype in enumerate(scene_types):
        c = SCENE_CONTENT[stype]
        row = dict(c)
        row.update(id="qa-%02d-%s" % (i + 1, stype), kind=stype, type=stype,
                   dur=SCENE_DUR)
        lines.append(row)
    script = dict(title=SYNTH_TITLE, fps=FPS, lines=lines,
                  _total=round(SCENE_DUR * len(scene_types), 3),
                  _design=str(design_id))
    return script


def build_scenes(scene_types, layout_default="left-stack"):
    """造 scene dict：start/end 连续排，三条内容共用同一段长。

    kind / type / scene_type 都写同一个场景类型：旧绘制器只认 kind（认不出就回落
    body —— 那正是基线大量 FAIL 的机制），新绘制器认 type / scene_type；多写几个别名
    不改变任何绘制结果，只增加版本兼容面。scene_layout 从设计计划来：设计说居中就该
    居中，否则版式差异会被强行抹平。
    """
    rows = []
    for i, stype in enumerate(scene_types):
        content = copy.deepcopy(SCENE_CONTENT[stype])
        start = round(i * SCENE_DUR, 6)
        row = dict(content)
        row.setdefault("text", "")
        row.setdefault("big", "")
        row.setdefault("items", [])
        row.update(id="qa-%02d-%s" % (i + 1, stype), kind=stype, type=stype,
                   scene_type=stype, start=start, end=round(start + SCENE_DUR, 6),
                   dur=SCENE_DUR, scene_layout=str(layout_default or "left-stack"))
        rows.append(row)
    return rows


# ---------------------------------------------------------------- 渲染
def render_frame(scenes_mod, script, scene, frac, fps=FPS):
    """渲染一帧并返回 (img, frame)。frame 与真实管线同口径 = int(t_global * fps)。

    加 1e-6 是为了吸收浮点误差：0.9*4.0 + 8.0 在二进制里是 11.599999…，×30 后
    int() 会少一帧（347 而不是 348）。t_global*t 的理论值才是帧号口径，epsilon
    不改变 int 语义，只避免"同一时刻因浮点顺序不同差一帧"。
    """
    t_local = float(frac) * float(scene["dur"])
    t_global = float(scene["start"]) + t_local
    frame = int(t_global * float(fps) + 1e-6)
    img = scenes_mod.draw_frame(scene, t_local, float(scene["dur"]), t_global,
                                script, frame)
    if img is None:
        raise RuntimeError("draw_frame 返回 None（scene=%s, t=%.3fs）"
                           % (scene.get("id"), t_global))
    if not isinstance(img, Image.Image):
        raise RuntimeError("draw_frame 返回 %r，不是 PIL.Image（scene=%s）"
                           % (type(img).__name__, scene.get("id")))
    return img.convert("RGB"), frame


# ---------------------------------------------------------------- 量化 / 指标
def quantize(img):
    """灰度 + 降采样 + 三值化。返回 (gray, base, tier)。

    base 取四角 40×40 的中位数：本项目正文/装饰都在安全区内，四角一定是背景；而
    主题 bg 色在不同明暗族之间差一个数量级，直接拿"主题色"当基准会让深色与浅色
    设计的 |gray-b| 落在不同的分档分布里——那不是构图差，是配色差被误算成结构差。
    """
    gray = np.asarray(img.convert("L").resize((QUAL_W, QUAL_H), Image.BOX),
                      dtype=np.float32)
    corners = np.concatenate([
        gray[:CORNER, :CORNER].ravel(),
        gray[:CORNER, -CORNER:].ravel(),
        gray[-CORNER:, :CORNER].ravel(),
        gray[-CORNER:, -CORNER:].ravel(),
    ])
    base = float(np.median(corners))
    d = np.abs(gray - base)
    tier = np.zeros((QUAL_H, QUAL_W), dtype=np.uint8)
    tier[d >= TIER_BG_MAX] = 1
    tier[d >= TIER_FG_MIN] = 2
    return gray, base, tier


def measure(img):
    """一张图 -> (六项指标, 基准亮度)。数值全部是 Python float（JSON 可序列化）。

    基准亮度一并返回是为了**诊断**：它定义了"本帧背景有多亮"，据此可以把设计分成
    明暗族。量到的 B 在深浅主题之间天然不可比（浅色主题整屏 bg→bg2 的渐变就可能超过
    15 的中间档门槛，整条边带都算"非背景"），所以报告要按明暗族分组看。
    """
    _gray, base, tier = quantize(img)
    fg = tier == 2
    total = int(fg.size)
    F = float(fg.sum()) * 100.0 / float(total)
    ys, xs = np.nonzero(fg)
    if xs.size:
        # 像素中心口径：第 x 列的中心在 x+0.5，再折算成 0..100
        Cx = float(xs.mean() + 0.5) * 100.0 / float(QUAL_W)
        Cy = float(ys.mean() + 0.5) * 100.0 / float(QUAL_H)
    else:
        # 整帧没有前景就没有质心；这里返回 None（比较阶段按"缺值"处理），
        # 不拿 0 顶替——0 会被读成"重心在左上角"，把一个空帧说成极端构图。
        Cx = Cy = None
    nonbg = tier != 0
    band_x = max(1, int(round(QUAL_W * EDGE_BAND)))
    band_y = max(1, int(round(QUAL_H * EDGE_BAND)))
    # B = 四边覆盖率的**最大值**：只要有一条边被构图压住，两套"边缘语言"就不同。
    cover = (float(nonbg[:band_y, :].mean()), float(nonbg[-band_y:, :].mean()),
             float(nonbg[:, :band_x].mean()), float(nonbg[:, -band_x:].mean()))
    B = max(cover) * 100.0
    half = QUAL_W // 2
    left = fg[:, :half]
    right = fg[:, QUAL_W - half:][:, ::-1]          # 右半幅翻转：量的是对称性
    union = int(np.logical_or(left, right).sum())
    inter = int(np.logical_and(left, right).sum())
    # 两半都空 = 完美对称（空构图自己跟自己的 IoU 是 1）；只有一边空才是 0。
    S = 1.0 if union == 0 else float(inter) / float(union)
    # P：垂直 9 带的平均灰度剖面，减去自身中位数后再取"两套设计逐带差的最大值"。
    # 减中位数是为了抗整体亮度/主题差（否则跨明暗族会把配色差当成结构差）；
    # 用整行平均而不是逐像素，是为了抗字体（字形一变，逐像素全变，行均值几乎不动）。
    rows = np.array_split(np.arange(QUAL_H), 9)
    prof = np.array([_gray[r].mean() for r in rows], dtype=np.float32)
    prof = prof - float(np.median(prof))
    metrics = {"F": round(F, 3), "Cx": None if Cx is None else round(Cx, 3),
               "Cy": None if Cy is None else round(Cy, 3),
               "B": round(B, 3), "S": round(S, 4),
               "P": [round(float(x), 3) for x in prof]}
    return metrics, base


# ---------------------------------------------------------------- 逐设计渲染
def render_design(scenes_mod, ST, DK, design_id, design, scene_types, fracs, fps=FPS):
    """渲一套设计的全部帧。逐帧 try/except：一帧炸了记 skip，不让整个门禁崩。

    返回 {"meta": {...}, "scenes": {scene_type: [frame_record, ...]}}。
    """
    plan = design_plan(DK, design)
    layout_default = str(plan.get("layout_default") or "left-stack")
    script = build_script(design_id, scene_types)
    # 先解好计划塞进 _style：scenes._plan() 优先读它，避免每帧重复解析；
    # 也保证同一设计 9 帧用的是同一份计划（含 theme_overrides）。
    script["_style"] = plan
    out = {"meta": dict(design=str(design_id),
                        label=str(design.get("label") or ""),
                        theme_id=plan.get("theme_id"),
                        layout_default=layout_default,
                        family=_family_of(ST, plan),
                        design_fp=_design_fp(DK, design)),
           "scenes": {}}
    for scene in build_scenes(scene_types, layout_default):
        stype = str(scene.get("type"))
        recs = []
        for frac in fracs:
            rec = {"frac": round(float(frac), 4)}
            try:
                img, frame = render_frame(scenes_mod, script, scene, frac, fps)
                rec["t_local"] = round(float(frac) * SCENE_DUR, 4)
                rec["t_global"] = round(float(scene["start"]) + float(frac) * SCENE_DUR, 4)
                rec["frame"] = int(frame)
                rec["sha256"] = _sha256_image(img)
                rec["metrics"], base = measure(img)
                rec["base"] = round(float(base), 1)
                rec["error"] = None
            except Exception as e:                          # noqa: BLE001 —— 单帧失败降级为 skip
                rec["metrics"] = None
                rec["error"] = _short_err(e)
                rec["traceback"] = traceback.format_exc()[-800:]
            recs.append(rec)
        out["scenes"][stype] = recs
    # 明暗族：一帧四角基准亮度的中位数。只作诊断分组，不参与判定。
    bases = [rec["base"] for rows in out["scenes"].values() for rec in rows
             if rec.get("base") is not None]
    meta = out["meta"]
    meta["base_median"] = round(float(np.median(bases)), 1) if bases else None
    meta["family"] = ("unknown" if not bases else
                      ("light" if meta["base_median"] >= 128 else "dark"))
    return out


def _design_fp(DK, design):
    """设计指纹（诊断用）：优先 designkit.design_fingerprint，没有就按规范 JSON 取 sha1。

    指纹只用于"这份设计是不是同一版"的追溯，不参与任何判定（跨机器可复现同一指纹）。
    """
    fn = getattr(DK, "design_fingerprint", None)
    if callable(fn):
        try:
            return str(fn(design))
        except Exception:                                   # noqa: BLE001 —— 指纹坏了不毁门禁
            pass
    payload = json.dumps(design, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), default=str)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- 比较 / 判定
def _metric_of(rec, key):
    """取一帧的某项指标；渲染失败或质心缺值 -> None。

    P（垂直明暗剖面）是**向量**，原样返回列表；其余五项是标量，统一转 float。
    """
    md = rec.get("metrics")
    if not isinstance(md, dict):
        return None
    v = md.get(key)
    if v is None:
        return None
    if key == "P":
        return [float(x) for x in v] if isinstance(v, (list, tuple)) else None
    return float(v)


def compare_pair(render, design_a, design_b, scene_types):
    """比较一对设计。返回可 JSON 化的 pair 结果（含每场景 × 每指标 × 每帧的差值）。

    判定核心（与模块文档同口径）：
      * 场景可区分 = 存在一项指标，其**全部帧**的 |Δ| 都 >= 阈值；
      * 设计对通过 = 任一场景可区分。
    """
    pair = {"a": design_a, "b": design_b, "status": "fail",
            "passed": [], "closest": None, "scenes": []}
    best = None                     # (ratio, scene, metric, min_delta)，给失败对排序用
    for stype in scene_types:
        rows_a = render[design_a]["scenes"].get(stype) or []
        rows_b = render[design_b]["scenes"].get(stype) or []
        scene_row = {"scene": stype, "status": "fail",
                     "passed_metrics": [], "metrics": {}, "skip_reason": ""}
        n = min(len(rows_a), len(rows_b))
        errors = []
        for m in METRIC_ORDER:
            deltas, missing = [], False
            for i in range(n):
                va = _metric_of(rows_a[i], m)
                vb = _metric_of(rows_b[i], m)
                if va is None or vb is None:
                    deltas.append(None)
                    missing = True
                elif m == "P":
                    # 剖面是向量：逐带求差，取绝对值最大的一带作为这对设计的 P 差值。
                    pa, pb = list(va), list(vb)
                    if len(pa) != len(pb) or not pa:
                        deltas.append(None)
                        missing = True
                    else:
                        deltas.append(round(max(abs(float(x) - float(y))
                                                for x, y in zip(pa, pb)), 4))
                else:
                    deltas.append(round(abs(va - vb), 4))
            all_frames = (n > 0) and not missing
            min_delta = min(deltas) if all_frames else None
            passed = bool(all_frames and min_delta + 1e-9 >= THRESHOLDS[m])
            scene_row["metrics"][m] = {"deltas": deltas, "min": min_delta,
                                       "need": THRESHOLDS[m], "passed": passed}
            if passed:
                scene_row["passed_metrics"].append(m)
            elif min_delta is not None:
                ratio = min_delta / THRESHOLDS[m] if THRESHOLDS[m] else 0.0
                # 失败对里"最接近通过"的指标：完成度 = 3 帧里最差那帧 / 阈值
                if best is None or ratio > best[0]:
                    best = (round(ratio, 4), stype, m, min_delta)
        for side, rows in (("A", rows_a), ("B", rows_b)):
            for rec in rows[:n]:
                if rec.get("error"):
                    errors.append("%s@t=%s: %s" % (side, rec.get("frac"),
                                                   rec.get("error")))
        if scene_row["passed_metrics"]:
            scene_row["status"] = "pass"
        elif errors:
            # 有帧渲不出来 / 指标缺值 -> 这一场景没有证据，记 skip 而不是 fail：
            # "没量到"不能当成"结构相同"，也不能冤枉改动。
            scene_row["status"] = "skip"
            scene_row["skip_reason"] = "；".join(sorted(set(errors))[:3])
        pair["scenes"].append(scene_row)
    passed_rows = [r for r in pair["scenes"] if r["status"] == "pass"]
    if passed_rows:
        pair["status"] = "pass"
        pair["passed"] = [{"scene": r["scene"], "metrics": list(r["passed_metrics"])}
                          for r in passed_rows]
    elif all(r["status"] == "skip" for r in pair["scenes"]):
        pair["status"] = "skip"
    if pair["status"] == "fail" and best is not None:
        pair["closest"] = {"scene": best[1], "metric": best[2],
                           "delta": best[3], "need": THRESHOLDS[best[2]],
                           "ratio": best[0]}
    return pair


def run_gate(scenes_py, designs, scene_types, fracs, fps=FPS, scripts_dir=SCRIPTS,
             design_ids=None):
    """主流程：装载 -> 逐设计渲帧 -> 两两比较 -> 汇总。返回 (result, elapsed_s)。

    designs 是已经物化的设计 dict 列表（采样或 --design-json 读入）；design_ids 缺省
    用设计自带的 id。**同一设计之间不做比较**（自己跟自己必然 "不可区分"，没有意义）。
    """
    _K, ST, DK = _modules(scripts_dir)
    scenes_mod = _load_scenes(scenes_py)
    ids = list(design_ids or _design_ids(designs))
    if len(ids) != len(designs):
        raise RuntimeError("design_ids 数量（%d）与 designs（%d）不一致"
                           % (len(ids), len(designs)))
    t0 = time.perf_counter()
    render = {}
    for did, design in zip(ids, designs):
        render[did] = render_design(scenes_mod, ST, DK, did, design, scene_types, fracs, fps)
    pairs = [compare_pair(render, a, b, scene_types)
             for a, b in itertools.combinations(ids, 2)]
    elapsed = time.perf_counter() - t0
    designs_meta = [render[did]["meta"] for did in ids]
    summary = summarize(pairs, designs_meta)
    render_errors = []
    for did in ids:
        for stype in scene_types:
            for rec in render[did]["scenes"].get(stype) or []:
                if rec.get("error"):
                    render_errors.append({"design": did, "scene": stype,
                                          "frac": rec.get("frac"),
                                          "error": rec.get("error")})
    result = {
        "version": RESULT_VERSION,
        "generator": "silhouette_qa.py %s" % __version__,
        "comment": "S13 跨设计剪影可区分（离线检查，不进 render.py 的 gate matrix；"
               "账本里的 G13 是版式文字冲突）：同一份合成剧本只换设计（designkit 现场采样），"
                   "灰度三值化后比 F/Cx/Cy/B/S/P；同一场景 3 帧须同一项指标全部达标，"
                   "任一场景达标即通过。颜色差异不参与判定。",
        "params": {
            "designs": list(ids), "scenes": list(scene_types),
            "frames": [round(float(x), 4) for x in fracs],
            "fps": int(fps), "scene_dur": SCENE_DUR,
            "synth_title": SYNTH_TITLE,
            "quant": {"size": [QUAL_W, QUAL_H], "corner": CORNER,
                      "tier_bg_max": TIER_BG_MAX, "tier_fg_min": TIER_FG_MIN,
                      "edge_band": EDGE_BAND},
            "thresholds": dict(THRESHOLDS),
        },
        "inputs": {
            "skill_root": _rel(os.path.dirname(os.path.abspath(scripts_dir))),
            "scripts_dir": _rel(scripts_dir),
            "scenes_py": _rel(scenes_py),
            "scenes_sha1": _sha1_file(scenes_py),
            "stylekit_sha1": _sha1_file(os.path.join(scripts_dir, "stylekit.py")),
            "designkit_sha1": _sha1_file(os.path.join(scripts_dir, "designkit.py")),
            "filmkit_sha1": _sha1_file(os.path.join(scripts_dir, "filmkit.py")),
            "tool_sha1": _sha1_file(os.path.abspath(__file__)),
        },
        "designs": designs_meta,
        "render": {did: render[did]["scenes"] for did in ids},
        "pairs": pairs,
        "summary": summary,
        # 顶层结论：直接把三档写出来，消费方不必再从 pairs/summary 里推导。
        # 顺序与退出码一致：有 fail -> FAIL；全 skip -> SKIP；否则 PASS。
        "verdict": ("FAIL" if summary["fail"] else
                    ("SKIP" if summary["skip"] else "PASS")),
        "render_errors": render_errors,
    }
    return result, elapsed


def summarize(pairs, designs_meta):
    """汇总：通过 / 失败 / 跳过各几对，失败对按"最接近阈值"降序排。

    额外按**明暗族**分组统计。理由：B 的基准是四角中位数，浅色设计（bg 240 左右、
    bg2 220 左右）整屏渐变就可能超过 15 的中间档门槛，整条边带都被判"非背景"，
    于是浅 vs 深的 ΔB 往往只是背景亮度差，不是构图差。分组统计能把这种"跨族虚高"
    与"同族内真的画得不一样"分开——同族通过数才是骨架差异的保守下界。
    """
    passed = [{"a": p["a"], "b": p["b"], "passed": p["passed"]}
              for p in pairs if p["status"] == "pass"]
    failed = [p for p in pairs if p["status"] == "fail"]
    skipped = [{"a": p["a"], "b": p["b"]}
               for p in pairs if p["status"] == "skip"]
    fam = {m.get("design"): m.get("family") for m in designs_meta}
    by_family = {}
    for label, same in (("same", True), ("cross", False)):
        rows = [p for p in pairs
                if (fam.get(p["a"]) == fam.get(p["b"])) == same]
        by_family[label] = {"pairs": len(rows),
                            "pass": sum(1 for p in rows if p["status"] == "pass"),
                            "fail": sum(1 for p in rows if p["status"] == "fail"),
                            "skip": sum(1 for p in rows if p["status"] == "skip")}
    passed_by_metric = {m: 0 for m in METRIC_ORDER}
    for p in pairs:
        if p["status"] != "pass":
            continue
        ms = set()
        for row in p["passed"]:
            ms.update(row["metrics"])
        for m in ms:
            passed_by_metric[m] += 1
    # 跨族且只靠 ΔB 通过的对：B 由明暗族差贡献的可能性最大，报告里单独点名。
    cross_b_only = [{"a": p["a"], "b": p["b"]} for p in pairs
                    if p["status"] == "pass"
                    and fam.get(p["a"]) != fam.get(p["b"])
                    and {m for row in p["passed"] for m in row["metrics"]} == {"B"}]
    failed_sorted = sorted(
        failed,
        key=lambda p: (-(p["closest"]["ratio"] if p.get("closest") else -1.0),
                       p["a"], p["b"]))
    return {
        "pairs": len(pairs),
        "pass": len(passed),
        "fail": len(failed),
        "skip": len(skipped),
        "passed": passed,
        "failed": [{"a": p["a"], "b": p["b"], "closest": p.get("closest")}
                   for p in failed_sorted],
        "skipped": skipped,
        "by_family": by_family,
        "passed_by_metric": passed_by_metric,
        "cross_family_b_only": cross_b_only,
        "all_pass": len(failed) == 0 and len(skipped) == 0,
    }


# ---------------------------------------------------------------- 人读报告
def _fmt_metric(key, value):
    if value is None:
        return "-"
    return ("%.3f" % value) if key == "S" else ("%.1f" % value)


def _fmt_deltas(metric_data, key):
    d = metric_data.get("deltas") or []
    if not d or any(v is None for v in d):
        return "-".ljust(8 if key != "S" else 10)
    return "/".join(_fmt_metric(key, v) for v in d)


def _scene_line(row):
    if row["status"] == "pass":
        mark = "PASS(%s)" % "+".join(row["passed_metrics"])
    elif row["status"] == "skip":
        mark = "SKIP"
    else:
        mark = "FAIL"
    parts = []
    for k in METRIC_ORDER:
        md = row["metrics"][k]
        star = "*" if md["passed"] else " "
        parts.append("Δ%s %s%s" % (k, _fmt_deltas(md, k), star))
    return "   %-8s %s   %s" % (row["scene"], " | ".join(parts), mark)


def print_human(res):
    """打印人读报告：每对的每类场景一行，失败对按接近程度排序。"""
    p = res["params"]
    th = p["thresholds"]
    print("== silhouette_qa（S13 跨设计剪影可区分；离线检查，不进账本）")
    print("画布量化 %d×%d（四角 %d×%d 取基准），帧时刻 %s × %.1fs，fps=%d"
          % (p["quant"]["size"][0], p["quant"]["size"][1], p["quant"]["corner"],
             p["quant"]["corner"], "/".join("%.2f" % f for f in p["frames"]),
             p["scene_dur"], p["fps"]))
    print("设计 %d 套：%s" % (len(p["designs"]), ", ".join(p["designs"])))
    fam = {}
    for m in res.get("designs") or []:
        fam.setdefault(str(m.get("family")), []).append(str(m.get("design")))
    print("明暗族（四角基准亮度中位数）：%s"
          % "；".join("%s=[%s]" % (k, ", ".join(v)) for k, v in sorted(fam.items())))
    print("场景 %d 类：%s；合成剧本 title=%r"
          % (len(p["scenes"]), ", ".join(p["scenes"]), p["synth_title"]))
    print("达标线：ΔF>=%.1fpp |ΔCx|>=%.1f |ΔCy|>=%.1f ΔB>=%.1f ΔS>=%.2f"
          "（同一场景各帧须同一项全部达标，任一场景达标即 PASS）"
          % (th["F"], th["Cx"], th["Cy"], th["B"], th["S"]))
    print("指标：%s" % "；".join("%s=%s" % (k, METRIC_LABELS[k]) for k in METRIC_ORDER))
    print("法：右半幅翻转后与左半幅算 IoU；Δ 列 = 各帧 |差值|；* = 该项达标")
    print("注：只比灰度骨架，配色差异不计分（颜色不同 ≠ 结构不同）")
    print("")
    total = len(res["pairs"])
    b_only = {(x["a"], x["b"]) for x in
              (res["summary"].get("cross_family_b_only") or [])}
    for i, pair in enumerate(res["pairs"], 1):
        if pair["status"] == "pass":
            detail = "；".join("%s·%s" % (r["scene"], "+".join(r["metrics"]))
                              for r in pair["passed"])
            warn = "［跨族仅 ΔB，慎判］" if (pair["a"], pair["b"]) in b_only else ""
            head = "[%2d/%d] %-22s %-4s（%s）%s" % (
                i, total, "%s vs %s" % (pair["a"], pair["b"]), "PASS", detail, warn)
        elif pair["status"] == "skip":
            head = "[%2d/%d] %-22s SKIP（没有可比的帧）" % (
                i, total, "%s vs %s" % (pair["a"], pair["b"]))
        else:
            c = pair.get("closest") or {}
            head = "[%2d/%d] %-22s FAIL（最接近 %s：Δ%s=%.3f/%.3f，完成度 %.0f%%）" % (
                i, total, "%s vs %s" % (pair["a"], pair["b"]),
                c.get("scene", "?"), c.get("metric", "?"),
                c.get("delta", float("nan")), c.get("need", float("nan")),
                100.0 * c.get("ratio", 0.0))
        print(head)
        for row in pair["scenes"]:
            print(_scene_line(row))
            if row.get("skip_reason"):
                print("            ↳ 跳过原因：%s" % row["skip_reason"])
        print("")
    s = res["summary"]
    print("== 汇总")
    print("设计对 %d：通过 %d，失败 %d，跳过 %d%s"
          % (s["pairs"], s["pass"], s["fail"], s["skip"],
             "（全部通过）" if s["all_pass"] else ""))
    bf = s.get("by_family") or {}
    if bf:
        print("  同明暗族：%d 对（通过 %d / 失败 %d）——骨架差异的保守下界"
              % (bf["same"]["pairs"], bf["same"]["pass"], bf["same"]["fail"]))
        print("  跨明暗族：%d 对（通过 %d / 失败 %d）"
              % (bf["cross"]["pairs"], bf["cross"]["pass"], bf["cross"]["fail"]))
    pbm = s.get("passed_by_metric") or {}
    if pbm:
        print("  达标指标分布（一对可多项）：%s"
              % "，".join("%s %d 对" % (k, pbm[k]) for k in METRIC_ORDER if pbm.get(k)))
    if s.get("cross_family_b_only"):
        print("  ［口径提示］跨族且仅靠 ΔB 通过：%s"
              % "，".join("%s vs %s" % (x["a"], x["b"])
                         for x in s["cross_family_b_only"]))
        print("    B 的基准是四角中位数：浅色主题整屏 bg→bg2 渐变本身就可能让整条边带"
              "“非背景”，跨族 ΔB 常是背景亮度差而非构图差；判定照口径保留，复核时看同族分组。")
    if s["passed"]:
        print("通过的对：")
        for row in s["passed"]:
            print("  %-32s vs %-32s %s"
                  % (row["a"], row["b"],
                     "；".join("%s·%s" % (x["scene"], "+".join(x["metrics"]))
                              for x in row["passed"])))
    if s["failed"]:
        print("失败的对（按最接近阈值排序，越靠前越接近通过）：")
        for i, row in enumerate(s["failed"], 1):
            c = row.get("closest") or {}
            if c:
                print("  %2d. %-32s vs %-32s 最接近 %s·Δ%s=%.3f（阈值 %.3f，完成度 %.0f%%）"
                      % (i, row["a"], row["b"], c.get("scene"), c.get("metric"),
                         c.get("delta", 0.0), c.get("need", 0.0),
                         100.0 * c.get("ratio", 0.0)))
            else:
                print("  %2d. %-32s vs %-32s 无可比指标" % (i, row["a"], row["b"]))
    if s["skipped"]:
        print("跳过的对（skipped != 通过）：")
        for row in s["skipped"]:
            print("  %s vs %s" % (row["a"], row["b"]))
    if res.get("render_errors"):
        print("渲染失败帧：")
        for e in res["render_errors"]:
            print("  %s/%s/t=%s: %s" % (e["design"], e["scene"], e["frac"], e["error"]))


# ---------------------------------------------------------------- 参数解析
def _parse_scene_types(raw):
    """--scenes：场景类型列表；默认 hero,list,stat（3 类，够用且快）。"""
    if not str(raw or "").strip():
        return list(DEFAULT_SCENE_TYPES)
    out = []
    for part in str(raw).replace(" ", "").split(","):
        if not part:
            continue
        if part not in SUPPORTED_SCENE_TYPES:
            raise SystemExit("未知场景类型 %r；可用：%s"
                             % (part, " / ".join(SUPPORTED_SCENE_TYPES)))
        if part not in out:
            out.append(part)
    if not out:
        raise SystemExit("--scenes 里没有有效场景类型")
    return out


def _parse_frames(raw):
    """--frames：段内时刻（0..1 的比例）；默认 0.05/0.5/0.9。"""
    if not str(raw or "").strip():
        return list(DEFAULT_FRACS)
    out = []
    for part in str(raw).replace(" ", "").split(","):
        if not part:
            continue
        try:
            v = float(part)
        except ValueError:
            raise SystemExit("--frames 必须是逗号分隔的 0..1 比例，收到 %r" % part) from None
        if not (0.0 <= v <= 1.0):
            raise SystemExit("--frames 里 %r 超出 0..1（这是段内时刻占 dur 的比例）" % part)
        out.append(v)
    if not out:
        raise SystemExit("--frames 里没有有效值")
    return out


def _seed_arg(raw):
    """--seed：固定整数 = 可复现；-1 / random / auto = 每次新设计。"""
    s = str(raw).strip().lower()
    if s in SEED_WORDS:
        return None
    try:
        v = int(s)
    except ValueError:
        raise SystemExit("--seed 必须是整数，或 -1/random 表示每次新设计；收到 %r" % raw) from None
    if v < 0:
        return None
    return v


def _json_bytes(data):
    """确定性 JSON 字节：固定缩进、不带时间戳（基线可 diff、可提交）。"""
    return (json.dumps(data, ensure_ascii=False, indent=1) + "\n").encode("utf-8")


def _load_json(path):
    path = os.path.abspath(str(path))
    if not os.path.isfile(path):
        raise SystemExit("文件不存在：%s" % path)
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise SystemExit("%s 不是合法 JSON：%s" % (path, e)) from e


# ---------------------------------------------------------------- 自检
# 自检用的采样种子：
#   SELFTEST_SEED_GAP  第一个采样对的种子（确定性检查用这一个设计）；
#   SELFTEST_SEED_SCAN 正控制在 seed 1..N 里找第一对可区分的设计（不写死单个 seed）。
# 为什么不写死"seed=4 一定过"：designkit 仍在演进，实测同一个 seed 的可区分结论会在
# 改动后翻转；写死会让 selftest 变成"测试采样器哈希"，而不是"测试门禁能不能分辨"。
SELFTEST_SEED_GAP = 4
SELFTEST_SEED_SCAN = 8


def _selftest_render(DK, scenes_mod, scene_types, fracs, design, tag):
    """渲一组帧（selftest 用；返回 {scene: [rec, ...]}，失败直接抛）。"""
    plan = design_plan(DK, design)
    script = build_script(tag, scene_types)
    script["_style"] = plan
    layout_default = str(plan.get("layout_default") or "left-stack")
    out = {}
    for scene in build_scenes(scene_types, layout_default):
        recs = []
        for frac in fracs:
            img, frame = render_frame(scenes_mod, script, scene, frac)
            recs.append({"frac": round(float(frac), 4), "frame": int(frame),
                         "metrics": measure(img)[0],
                         "sha256": _sha256_image(img), "error": None})
        out[str(scene.get("type"))] = recs
    return out


def selftest(scripts_dir=SCRIPTS, scenes_py=DEFAULT_SCENES_PY):
    """自检：现场采样 2 组设计 + 渲染（离线、不联网、<=60s），断言三件事。

      1. **同一设计重复渲染逐像素一致**（确定性）：同进程渲两遍，9 帧 sha256 全等；
      2. **不同设计之间可区分**（正控制）：seed 1..SELFTEST_SEED_SCAN 里找第一对
         门禁判 PASS 的采样设计 —— 找得到才算"不同片画面结构可区分"；
      3. **同一设计自己跟自己比必须 FAIL**（负控制）：否则门禁恒真，等于没门禁。

    为什么用"同一 brief 的两个 seed"而不是两份不同 brief：门禁要回答的是"同一个采样器
    产出的两个设计像不像"——不同 brief 会混进题材差异，不是本门禁的判据。
    为什么正控制扫 seed 而不写死 seed：designkit 仍在演进，固定 seed 的结论会随采样器
    改动漂移；扫描找得到任意一对可区分即可证明"采样器产出的设计在骨架上确实有差异"，
    而全扫完都不行正该 FAIL（绝不靠放宽阈值让自检变绿）。

    返回 (ok, report)；report 是可直接 --json 的 dict。耗时实测 ~1.2s（首对命中即停），
    最坏（8 个 seed 全失败）约 10s，远低于 60s 上限。
    """
    t0 = time.perf_counter()
    _K, _ST, DK = _modules(scripts_dir)
    scenes_mod = _load_scenes(scenes_py)
    scene_types = list(DEFAULT_SCENE_TYPES)
    fracs = list(DEFAULT_FRACS)
    checks = []
    try:
        # 1) 确定性：同一设计渲两遍（同进程两次渲染，逐像素 sha256 必须相等）
        a = sample_designs(DK, 2, SELFTEST_SEED_GAP)[0]
        ra1 = _selftest_render(DK, scenes_mod, scene_types, fracs, a, "st-a-1")
        ra2 = _selftest_render(DK, scenes_mod, scene_types, fracs, a, "st-a-1")
        det_bad = []
        for stype in scene_types:
            for r1, r2 in zip(ra1[stype], ra2[stype]):
                if r1["sha256"] != r2["sha256"]:
                    det_bad.append("%s@%s(%s!=%s)" % (stype, r1["frac"],
                                                      r1["sha256"][:8], r2["sha256"][:8]))
                elif r1["metrics"] != r2["metrics"]:
                    det_bad.append("%s@%s 指标不一致" % (stype, r1["frac"]))
        checks.append({"name": "同一设计重复渲染逐像素一致（确定性）",
                       "ok": not det_bad,
                       "detail": ("%d 帧 sha256 全等" % sum(len(v) for v in ra1.values())
                                  if not det_bad else "；".join(det_bad[:4]))})
        # 2) 正控制：两个不同设计必须可区分。
        # 不写死"seed=N 一定过"：designkit 仍在演进，硬编码一个 seed 会让 selftest 随
        # 采样器改动漂移（例如 v2.0.0 开发期 seed=3/4 的结论在半天内就翻转了两次）。
        # 改为扫 seed 1..8：第一对可区分的就作为正控制证据；全扫完都不行 = 采样器产出的
        # 设计在灰度骨架上确实雷同 —— 那正该 FAIL，不许悄悄放宽。
        fwd = None
        for seed in range(1, SELFTEST_SEED_SCAN + 1):
            pair = sample_designs(DK, 2, seed)
            x, y = pair[0], pair[1]
            rx = _selftest_render(DK, scenes_mod, scene_types, fracs, x, "st-fwd-%d-a" % seed)
            ry = _selftest_render(DK, scenes_mod, scene_types, fracs, y, "st-fwd-%d-b" % seed)
            pair_xy = compare_pair({"A": {"scenes": rx}, "B": {"scenes": ry}},
                                   "A", "B", scene_types)
            if pair_xy["status"] == "pass":
                fwd = (seed, pair_xy)
                break
        checks.append({"name": "不同设计之间可区分（正控制）",
                       "ok": fwd is not None,
                       "detail": ("seed=%d PASS：%s" % (fwd[0], "；".join(
                           "%s·%s" % (r["scene"], "+".join(r["metrics"]))
                           for r in fwd[1]["passed"]))
                       if fwd is not None else
                       "seed 1..%d 全部不可区分（采样器入场设计骨架雷同）"
                       % SELFTEST_SEED_SCAN)})
        # 3) 负控制：同一设计自己跟自己比必须 FAIL（否则门禁恒真，等于没门禁）
        render_self = {"A": {"scenes": ra1}, "A2": {"scenes": ra2}}
        pair_aa = compare_pair(render_self, "A", "A2", scene_types)
        checks.append({"name": "同一设计自比必须不可区分（负控制）",
                       "ok": pair_aa["status"] == "fail",
                       "detail": ("FAIL（符合预期：Δ 全为 0）"
                                  if pair_aa["status"] == "fail" else
                                  "PASS（不符合预期：门禁把同一设计判成可区分，判据有 bug）")})
    except Exception as e:                                  # noqa: BLE001
        checks.append({"name": "自检执行", "ok": False,
                       "detail": "%s: %s" % (type(e).__name__, e)})
        traceback.print_exc()
    elapsed = round(time.perf_counter() - t0, 3)
    ok = bool(checks) and all(c["ok"] for c in checks)
    report = {"version": RESULT_VERSION, "generator": "silhouette_qa.py %s" % __version__,
              "selftest": True, "ok": ok, "elapsed_s": elapsed,
              "checks": checks,
              "inputs": {"scenes_py": _rel(scenes_py),
                         "scenes_sha1": _sha1_file(scenes_py),
                         "designkit_sha1": _sha1_file(os.path.join(scripts_dir, "designkit.py")),
                         "tool_sha1": _sha1_file(os.path.abspath(__file__))}}
    return ok, report


def print_selftest(report):
    print("== silhouette_qa --selftest（S13 自检：造 2 组设计现场验证判据）")
    for c in report["checks"]:
        print("  [%s] %s — %s" % ("PASS" if c["ok"] else "FAIL", c["name"], c["detail"]))
    print("自检 %s（用时 %.2fs）" % ("PASS" if report["ok"] else "FAIL",
                                    report.get("elapsed_s") or 0.0))


# ---------------------------------------------------------------- CLI
def main(argv=None):
    _setup_stdio()
    ap = argparse.ArgumentParser(
        description="跨设计剪影可区分检查 S13（离线检查，不进 render.py 的 gate matrix；"
                    "三值化后比结构指标。没有 --styles：技能不提供内置"
                    "风格表（stylekit.STYLE_IDS 为空），输入改为现场采样的设计。",
        epilog="例：python tools/silhouette_qa.py --designs 4 --json；"
               "python tools/silhouette_qa.py --selftest")
    ap.add_argument("--designs", type=int, default=DEFAULT_DESIGNS,
                    help="现场采样几个设计两两比较（>=2，缺省 %d）" % DEFAULT_DESIGNS)
    ap.add_argument("--seed", default=str(DEFAULT_SEED),
                    help="采样种子：固定整数 = 可复现（缺省 %d）；-1/random = 每次新设计"
                         % DEFAULT_SEED)
    ap.add_argument("--brief", default="",
                    help="采样用的 brief 文本（缺省内置一句话题材；不联网）")
    ap.add_argument("--design-json", default="",
                    help="诊断用：直接读设计文件（单设计/数组/{designs:[...]}），不再采样")
    ap.add_argument("--scenes", default="",
                    help="逗号分隔场景类型（缺省 hero,list,stat）")
    ap.add_argument("--frames", default="",
                    help="段内时刻比例，逗号分隔（缺省 0.05,0.5,0.9）")
    ap.add_argument("--scenes-py", default="",
                    help="诊断用：指定另一份 scenes.py 渲染（A/B 对比改造前后）")
    ap.add_argument("--skill-root", default="",
                    help="诊断用：改用含 scripts/ 与 assets/ 的另一份技能目录"
                         "（如 git archive 出来的改造前快照），缺省用当前仓库")
    ap.add_argument("--json", action="store_true", help="stdout 只输出 JSON（机读）")
    ap.add_argument("--strict", action="store_true",
                    help="存在跳过（渲不出/缺值）时退出码 2；跳过绝不写成通过")
    ap.add_argument("--out", default="", help="额外把 JSON 写到该路径（缺省不写文件）")
    ap.add_argument("--selftest", action="store_true",
                    help="自检：合成 2 组设计 + 渲染（离线 <=60s），验证判据可用且非恒真")
    a = ap.parse_args(argv)
    if a.selftest:
        try:
            skill_root = os.path.abspath(a.skill_root) if a.skill_root else SKILL
            scripts_dir = os.path.join(skill_root, "scripts")
            scenes_py = (os.path.abspath(a.scenes_py) if a.scenes_py
                         else os.path.join(skill_root, "assets", "scenes.py"))
            ok, report = selftest(scripts_dir, scenes_py)
        except SystemExit:
            raise
        except Exception as e:                              # noqa: BLE001
            sys.stderr.write("silhouette_qa --selftest 运行失败：%s: %s\n"
                             % (type(e).__name__, e))
            sys.stderr.write(traceback.format_exc()[-2000:])
            return 1
        if a.json:
            sys.stdout.write(_json_bytes(report).decode("utf-8"))
            sys.stdout.flush()
        else:
            print_selftest(report)
        return 0 if ok else 1
    try:
        skill_root = os.path.abspath(a.skill_root) if a.skill_root else SKILL
        scripts_dir = os.path.join(skill_root, "scripts")
        scenes_py = (os.path.abspath(a.scenes_py) if a.scenes_py
                     else os.path.join(skill_root, "assets", "scenes.py"))
        _K, _ST, DK = _modules(scripts_dir)
        scene_types = _parse_scene_types(a.scenes)
        fracs = _parse_frames(a.frames)
        if a.design_json:
            designs = _normalize_designs(_load_json(a.design_json))
            if len(designs) == 1:
                raise SystemExit("--design-json 只给了 1 个设计：两两比较至少需要 2 个设计"
                                 "（要验证确定性请用 --selftest）")
        else:
            if a.designs < 2:
                raise SystemExit("--designs 必须 >= 2（两两比较），收到 %d" % a.designs)
            seed = _seed_arg(a.seed)
            designs = sample_designs(DK, a.designs, seed, a.brief or DEFAULT_BRIEF)
        ids = _design_ids(designs)
        res, elapsed = run_gate(scenes_py, designs, scene_types, fracs, FPS,
                                scripts_dir, design_ids=ids)
    except SystemExit:
        raise
    except Exception as e:                                  # noqa: BLE001
        # 装载/全局性失败是**工具跑不成**，不是判 FAIL：明确报错并按失败退出，
        # 免得 CI 把"工具没跑起来"当成"设计不可区分"去修构图。
        sys.stderr.write("silhouette_qa 运行失败：%s: %s\n" % (type(e).__name__, e))
        sys.stderr.write(traceback.format_exc()[-2000:])
        return 1
    payload = _json_bytes(res)
    if a.out:
        out_path = os.path.abspath(a.out)
        d = os.path.dirname(out_path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(out_path, "wb") as fh:
            fh.write(payload)
    if a.json:
        sys.stdout.write(payload.decode("utf-8"))
        sys.stdout.flush()
    else:
        print_human(res)
        print("渲染 %d 设计 × %d 场景 × %d 帧，用时 %.2fs"
              % (len(ids), len(scene_types), len(fracs), elapsed))
        if a.out:
            print("JSON 已写入：%s" % os.path.abspath(a.out))
    s = res["summary"]
    if s["fail"] > 0:
        return 1
    if s["skip"] > 0 and a.strict:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
