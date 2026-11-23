# -*- coding: utf-8 -*-
"""previewkit.py — 离线方案预览：先看 3 个方案，再出片。

解决什么问题
-------------
用户要"先看 3 个方案再出片"，可现有的 ``render.py --stage stills`` 在缺 work/tts.json 时
会**自动联网合成旁白**（render.py:2529-2537）——"看一眼大概长什么样"这一步不该有网络、
不该有合成成本、也不该等几十秒。

本模块把预览做成**纯本地、纯确定性**的两件事：
  1. 用**合成时间轴**（每句旁白假装 ``fake_voice_dur`` 秒，默认 2.0）排 timeline，
     不读 tts.json、不合成语音。思路照抄 tools/golden_frames.py:32-35 与 build_timeline；
  2. 按 grammar.py 选出的真实构图（scene_type）渲染若干静帧 + 一张联系表。

因此本文件**不联网、不做旁白合成、不跑视频编码器**（不调用 filmkit.tts / filmkit 的
编码函数）。产出的 PNG 与真实成片的差别只在"哪一帧是哪一句"，不在"这一帧画成什么样"——
后者正是方案预览要回答的问题。

12 秒样音
-----------------
``render_audio_preview`` 是本文件唯一会"出声"的地方：按设计里的 bpm/key/scale/mood/seed
生成 12 秒配乐，回答"这一版的情绪对不对"。优先走 musicgen（参数化作曲器）；它没就绪时
退回 scorekit 的 legacy 路径（scorekit 没有风格表，那条路只保证"还能出声"，
音乐固定为 Dm/120，不能当作方案预览）。两条都不在就返回 ok=False。**样音失败绝不
打断提案**：提案是决策环节，不该因为配乐引擎没装好就整条断掉。

接口
----
    synthetic_timeline(script, fake_voice_dur=3.2, fps=30, plan=None) -> dict
    render_preview_frames(plan, script, times, outdir, theme=None) -> list[str]
    contact_sheet(paths, out_png, cols=None) -> str
    preview_candidate(design, script, outdir, n_frames=4, with_audio=False) -> dict
    render_audio_preview(design, out_dir, seconds=12.0) -> dict
    main(argv=None) -> int

times 的语义：**全片绝对秒**（0 <= t <= total）。也接受 ``[scene_id, 段内秒]`` 两元组
（与 render.py --stills 的 id:local 写法同义），但绝不把"绝对秒"当成"段内秒"。

三条硬规矩
----------
1. **scene_type 必须来自 grammar.plan_scenes**。scenes.draw_frame 在 scene_type 为空时会
   按 kind 回退（_KIND_TO_TYPE），那样看到的不是真实构图（hook->hero、body->list…），
   "预览"就成了另一种片子。本模块每次都显式调一次 plan_scenes 再画。
2. **每帧必须真的是当前画布尺寸**（横屏 1920x1080 / 竖屏 1080x1920，见 CANVAS）。
   draw_frame 与 draw_subtitle 之后各校验一次，不符直接 raise——静默输出一张尺寸不对的
   "预览"比报错更糟。
3. **designkit 由并行任务实现，本模块不自己实现它**。这里只依赖它的
   ``design_to_plan(design) -> plan`` 一个函数；designkit 还没就绪（import 失败或缺该函数）
   时，``design_to_plan`` 会用清晰的中文报错说明"缺什么、怎么绕过"，绝不静默回落到某一套
   内置风格（静默回落 = 所有候选又长得一样，正是这次改造要根除的毛病）。

性能实测（本机，Python 3.12 / Pillow 13 / numpy 2.5，模板片 4 段）
--------------------------------------------------------------
单候选 4 帧 1080p + 联系表：冷启动 ~1.2 s（其中 import 依赖 ~0.21 s，首帧烘焙背景 ~0.16 s），
同进程热缓存 ~0.9 s。瓶颈是 PNG 落盘：默认 compress_level=6 要 0.6 s/帧，预览图用
``compress_level=1``（0.10 s/帧，体积 +17%）换 5 倍速度——预览是**临时证据**，不是交付物。
"""
from __future__ import annotations

__version__ = "1.1.0"

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
import time
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.normpath(os.path.join(HERE, os.pardir, "assets"))


def _default_scenes():
    """默认 scenes.py：**先看同目录**，再回落到 <skill>/assets/。

    为什么不能只写死 assets/：用户可能把自己的 scenes.py 放在**剧本同目录**（myfilm/ 下只有
    film.json 与 scenes.py），此时按相对路径推出来的 <myfilm>/../assets/scenes.py 根本不存在——
    实测报 "scenes.py 不存在：<myfilm>/../assets/scenes.py"，提案直接跑不起来。
    脚本仍在 <skill>/scripts/ 时，同目录通常没有 scenes.py，于是回落到 assets/。
    """
    for cand in (os.path.join(HERE, "scenes.py"),
                 os.path.join(ASSETS, "scenes.py")):
        if os.path.isfile(cand):
            return cand
    return os.path.join(ASSETS, "scenes.py")


def _default_script():
    """默认 film.json：同目录优先（工作目录里就是剧本），否则回落 assets/。"""
    for cand in (os.path.join(HERE, "film.json"),
                 os.path.join(ASSETS, "film.json")):
        if os.path.isfile(cand):
            return cand
    return os.path.join(ASSETS, "film.json")


DEFAULT_SCENES = _default_scenes()
DEFAULT_SCRIPT = _default_script()
DEFAULT_OUTDIR = "preview"
# 样音的交付口径固定下来：48 kHz 立体声、12 秒。参数不一致的两份 WAV 没法并排比较。
AUDIO_PREVIEW_NAME = "audio_preview.wav"
AUDIO_PREVIEW_SECONDS = 12.0
AUDIO_SR = 48000

# 预览固定 1080p：方案比较的是"构图/配色/排版"，缩到 720p 会让字号与留白判断失真。
# 预览画布跟随当前 layout profile（横屏 1920x1080 / 竖屏 1080x1920）。
# 以前这里是常量 1920x1080：于是 `--layout portrait` 下 previewkit 的 _require_canvas
# 会把每一帧都判成"尺寸不符"，竖屏提案根本出不来（实测 career 一次：propose.py
# 不接受 --layout 就算绕过去了，出来的静帧仍是横屏——设计评审看的是错的东西）。
try:
    import layoutkit as _LK                        # noqa: PLC0415  # 导入期定死（延迟到模块半途会拿不到 profile）
    CANVAS = (int(_LK.W), int(_LK.H))
except Exception:                                  # noqa: BLE001 —— 只读旁路
    CANVAS = (1920, 1080)
# 假语音时长：**从 2.0 提到 2.8**。
# 为什么：段长 = lead + 假语音 + tail ≈ 2.0 + 1.4 = 3.4 s，而 chart 要求 ≥4.5 s、
# compare/process 要求 ≥4.0 s —— 于是预览里这些构图**必然被 grammar 降级成 list**。
# 实测后果：一份真片的 chart 段在预览里画成了要点列表（条形一根都没画），
# 用户看到的是"数据条没做出来"，而成片里其实是正常的。预览骗人比预览粗糙严重得多。
# 3.2 s 让段长约 4.6 s，能盖住 chart 的 4.5 s 门槛（2.8 s 时 4.2 s 仍差一点，
# 实测 chart 段还是被降级成 list）。假时长只是"排轴用的常数"，调大不会增加成本，
# 但它决定"预览能不能画出真实构图"，宁可偏大。
DEFAULT_FAKE_VOICE_DUR = 3.2
DEFAULT_FRAMES = 4
# 预览图落盘用快档（理由见模块文档的实测数据）；交付物仍由 render.py 走默认档。
PNG_COMPRESS_LEVEL = 1
CAND_IDS = "ABCDEFGH"

_MODS = None            # (filmkit, stylekit, grammar) 惰性装载缓存
_SCENES = None          # (path, module) 惰性装载缓存


# ---------------------------------------------------------------- 基础工具
def _setup_stdio():
    """输出被重定向时切 UTF-8（Windows 控制台默认 GBK，中文会乱码）。与 render.py 同口径。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                                   # noqa: BLE001
            pass


def _ensure_paths():
    """把 scripts/ 与 assets/ 补进 sys.path。

    scenes.py 自己会 ``import filmkit`` / ``import stylekit``，两个目录都得在路径上，
    否则按路径装载 scenes.py 时会 ModuleNotFoundError（tools/golden_frames.py 同处理）。
    """
    for p in (HERE, ASSETS):
        if p not in sys.path:
            sys.path.insert(0, p)


def _modules():
    """惰性装载 filmkit / stylekit / grammar（只装一次）。"""
    global _MODS
    if _MODS is not None:
        return _MODS
    _ensure_paths()
    try:
        import filmkit as K
        import stylekit as ST
        import grammar as G
    except Exception as e:                                  # noqa: BLE001
        raise RuntimeError("装载 filmkit/stylekit/grammar 失败（%s）：%s" % (HERE, e)) from e
    _MODS = (K, ST, G)
    return _MODS


def _scenes_module(path=None):
    """按路径装载 scenes.py（独立模块名，避免与站点包里的同名模块串档）。"""
    global _SCENES
    path = os.path.abspath(str(path or DEFAULT_SCENES))
    if _SCENES is not None and _SCENES[0] == path:
        return _SCENES[1]
    if not os.path.isfile(path):
        raise RuntimeError("scenes.py 不存在：%s" % path)
    _ensure_paths()
    spec = importlib.util.spec_from_file_location("preview_scenes", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("scenes.py 无法作为模块加载：%s" % path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["preview_scenes"] = mod
    spec.loader.exec_module(mod)
    if not callable(getattr(mod, "draw_frame", None)):
        raise RuntimeError("scenes.py 缺少 draw_frame(scene, t_local, dur, t_global, "
                           "script, frame) 契约：%s" % path)
    _SCENES = (path, mod)
    return mod


def _designkit():
    """惰性 import 并行开发的 designkit；不可用返回 None（由调用方给出可照做的报错）。

    designkit.py 可能还没写完（本轮由另一人并行实现），所以这里**只 import**，
    绝不在这里复制一份设计 DSL——那会导致两个"真源"。
    """
    _ensure_paths()
    try:
        import designkit
        return designkit
    except Exception:                                       # noqa: BLE001 —— 未就绪不炸 import 期
        return None


# ---------------------------------------------------------------- 参数校验
def _check_plan(plan):
    """校验这是一份"能喂给 scenes.draw_frame"的风格计划（fail-closed）。"""
    if not isinstance(plan, dict):
        raise TypeError("plan 必须是 designkit.design_to_plan(design) 返回的 dict，收到 %s"
                        % type(plan).__name__)
    if plan.get("legacy"):
        return plan
    if not isinstance(plan.get("theme"), dict) and not plan.get("theme_id"):
        raise ValueError("plan 不是 stylekit 形状的风格计划：缺 theme 与 theme_id"
                         "（现有键：%s）" % ", ".join(sorted(plan)[:12]))
    return plan


def _fps_of(script, plan=None):
    """画幅帧率：优先设计里的 video.fps / fps，否则跟随剧本（filmkit.video_plan）。"""
    K, _ST, _G = _modules()
    plan = plan if isinstance(plan, dict) else {}
    got = None
    video = plan.get("video")
    if isinstance(video, dict) and video.get("fps"):
        got = video.get("fps")
    elif plan.get("fps"):
        got = plan.get("fps")
    if got is not None:
        fps = int(got)
        allowed = tuple(getattr(K, "VIDEO_FPS_ALLOWED", (24, 25, 30, 50, 60)))
        if fps not in allowed:
            raise ValueError("plan 里的 fps=%r 不支持（可用：%s）"
                             % (got, "/".join(str(x) for x in allowed)))
        return fps
    return int(K.video_plan(script)["fps"])


def _scene_at(scenes, t):
    """全片时刻 -> (scene, 段内时刻)，与 render._render_range 的查找方式一致。"""
    idx = 0
    while idx + 1 < len(scenes) and t >= scenes[idx + 1]["start"]:
        idx += 1
    s = scenes[idx]
    return s, float(t) - float(s["start"])


def _require_canvas(img, scene, t, stage):
    """尺寸硬校验：方案预览必须等于当前 layout profile 的画布（不缩、不裁、不拉伸）。"""
    if img is None:
        raise RuntimeError("%s：场景 %s 在 t=%.3fs 返回 None（必须返回 PIL.Image）"
                           % (stage, scene.get("id"), t))
    size = tuple(int(v) for v in getattr(img, "size", (0, 0)))
    if size != CANVAS:
        raise RuntimeError("%s：场景 %s 在 t=%.3fs 渲染出 %dx%d，期望 %dx%d"
                           "（方案预览画布跟随 layout profile，尺寸不符说明绘制器/主题有问题）"
                           % (stage, scene.get("id"), t, size[0], size[1],
                              CANVAS[0], CANVAS[1]))


# ---------------------------------------------------------------- 合成时间轴
def synthetic_timeline(script, fake_voice_dur=DEFAULT_FAKE_VOICE_DUR, fps=30, plan=None):
    """用"假语音时长"排一条确定性时间轴（不联网、不读 TTS 产物）。

    每句 dur 固定为 ``fake_voice_dur``，其余（lead/tail/min_dur/hold）照常走
    filmkit.build_timeline。lead/tail/min_dur/hold 的取值顺序与 render.st_layout 完全一致：
    script 里显式写了就优先，否则用**设计计划的 pacing**，最后才是缺省值
    （0.55 / 0.85 / 3.0 / 0.0）。

    为什么必须读 plan["pacing"]：设计会改节奏（lead 0.45~0.85 / tail 0.75~1.1 /
    min_dur 2.6~3.5 / hold 0~0.4）。只按缺省值排轴，预览的段长就与成片不同——
    段长又决定 grammar 能不能画出 chart/compare/process（它们有最短段长门槛），
    于是预览里看到的构图可能成片里根本没有。这正是预览最不该犯的错。

    返回 filmkit.build_timeline 的**原样结构**：{total, scenes, fps, frames}
    （每段带 id/kind/start/end/dur/voice_start/voice_end 与剧本原字段）。

    注意：这不是成片时间轴。真实旁白时长被替换成了 fake_voice_dur，绝对时间必然不同，
    所以本函数只用于"看一眼构图与配色"，不用它反推成片时长。
    """
    K, _ST, _G = _modules()
    if not isinstance(script, dict):
        raise TypeError("script 必须是 dict（film.json 的内容），收到 %s" % type(script).__name__)
    pace = plan.get("pacing") if isinstance(plan, dict) else None
    pace = pace if isinstance(pace, dict) else {}
    try:
        fake = float(fake_voice_dur)
    except (TypeError, ValueError):
        raise ValueError("fake_voice_dur 必须是数字，收到 %r" % (fake_voice_dur,)) from None
    if fake <= 0:
        raise ValueError("fake_voice_dur 必须 > 0，收到 %r" % (fake_voice_dur,))

    lines = []
    for i, ln in enumerate(script.get("lines") or []):
        if not isinstance(ln, dict):
            continue
        row = dict(ln)
        row.setdefault("id", "l%02d" % (i + 1))
        row["dur"] = fake
        lines.append(row)
    if not lines:
        raise ValueError("剧本 lines 为空，无法排时间轴（预览至少需要一段内容）")

    return K.build_timeline(
        lines,
        lead=float(script.get("lead", pace.get("lead", 0.55))),
        tail=float(script.get("tail", pace.get("tail", 0.85))),
        min_dur=float(script.get("min_dur", pace.get("min_dur", 3.0))),
        tail_map=script.get("tail_map"),
        lead_map=script.get("lead_map"),
        hold=float(script.get("hold", pace.get("hold", 0.0))),
        max_dur=script.get("max_dur"),
        fps=int(fps),
    )


def default_times(tl, n_frames=DEFAULT_FRAMES):
    """默认取样时刻：在全部段上均匀取 n 段，各取段内中点（段少时就是每段一张）。

    为什么不取"前 4 段"：钩子/正文/收尾的构图差得最远，只取前 4 段会漏掉 outro，
    用户看不到"结尾长什么样"。均匀取点让 4 张图覆盖全片节奏。
    段内偏移用 golden_frames 的同一条钳制（>=0.55s 且 <= dur-0.8s），避开片头淡入淡出。
    """
    scenes = list(tl.get("scenes") or [])
    if not scenes:
        raise ValueError("时间轴里没有段，无法取样")
    total = float(tl.get("total") or 0.0)
    n = len(scenes)
    want = max(1, min(int(n_frames), n))
    if want == 1:
        idxs = [n // 2]
    else:
        idxs = sorted({int(round(i * (n - 1) / float(want - 1))) for i in range(want)})
    out = []
    for i in idxs:
        s = scenes[i]
        dur = float(s.get("dur") or 0.0)
        local = max(0.55, min(dur * 0.5, max(0.56, dur - 0.8)))
        t = float(s.get("start") or 0.0) + local
        out.append(round(min(max(t, 0.0), max(0.0, total - 0.001)), 3))
    return out


# ---------------------------------------------------------------- 渲染
def _resolve_time(tl, spec, by_id=None):
    """取样规格 -> (scene, 段内秒)。支持 全片绝对秒 / [id, 段内秒] / "id:段内秒"。"""
    scenes = tl["scenes"]
    if isinstance(spec, str):
        sid, _, local = spec.partition(":")
        spec = [sid, float(local or 0.0)]
    if isinstance(spec, (list, tuple)) and len(spec) == 2:
        by_id = by_id or {s.get("id"): s for s in scenes}
        s = by_id.get(str(spec[0]))
        if s is None:
            raise KeyError("时间轴里没有 id=%r 的段（有：%s）"
                           % (spec[0], " / ".join(str(x.get("id")) for x in scenes)))
        local = float(spec[1])
    else:
        t = float(spec)
        s, local = _scene_at(scenes, min(max(t, 0.0), max(0.0, float(tl["total"]) - 0.001)))
    local = max(0.0, min(float(local), max(0.0, float(s["dur"]) - 0.05)))
    return s, local


def render_preview_frames(plan, script, times, outdir, theme=None):
    """对给定 plan 渲染指定时刻的静帧，返回落盘的 PNG 路径列表（绝对路径）。

    - scene_type 由 **grammar.plan_scenes** 就地写进时间轴（不从 kind 猜）；
    - 每帧 = scenes.draw_frame(...) + filmkit.draw_subtitle(...)（与 render.py 同序）；
    - draw_frame 之后与 draw_subtitle 之后各校验一次尺寸，不是 1920x1080 就 raise。

    plan / script 不会被修改：内部用一份浅拷贝承载 ``_style`` / ``_total`` 等渲染期私有字段。
    """
    K, _ST, G = _modules()
    plan = _check_plan(plan)
    if not isinstance(script, dict):
        raise TypeError("script 必须是 dict（film.json 的内容），收到 %s" % type(script).__name__)
    if (int(K.W), int(K.H)) != CANVAS:
        raise RuntimeError("filmkit 画布 %dx%d 与本模块期望的 %dx%d 不一致：方案预览固定 1080p"
                           % (K.W, K.H, CANVAS[0], CANVAS[1]))
    specs = list(times or [])
    if not specs:
        raise ValueError("times 为空：至少要给一个取样时刻（全片绝对秒，或 [段id, 段内秒]）")

    outdir = os.path.abspath(str(outdir))
    os.makedirs(outdir, exist_ok=True)

    # plan 一并传进去：lead/tail/min_dur/hold 必须与 render.st_layout 同源，否则预览段长
    # 与成片不同，grammar 的最短段长门槛会把 chart/compare/process 判成别的构图。
    tl = synthetic_timeline(script, fps=_fps_of(script, plan), plan=plan)
    # 字幕样式：render.py 的 frames/stills 与 qavmaf 参考帧都传 design 的 visual.subtitle
    # （见 filmkit.subtitle_kwargs 与 qavmaf._sub_kw_for 的实测说明）。预览不传就永远是
    # filmkit 模块默认值（46px / plate_alpha=150），用户看到的是"另一套字幕"。
    sub_kw = K.subtitle_kwargs(plan)
    # 唯一 scene_type 来源。注意 scenes.draw_frame 已**没有** legacy 分支（stylekit 也不再
    # 产出 legacy 计划），所以这里补的 scene_type 一定会被真正用到。
    # 预览绝不允许 scene_type 为空：为空时 scenes 会静默落到 statement，看起来"能出图"，
    # 但和全片渲染的分镜不是同一份。
    G.plan_scenes(tl["scenes"], script, plan)
    missing = [s.get("id") for s in tl["scenes"] if not s.get("scene_type")]
    if missing:
        raise RuntimeError("grammar.plan_scenes 没有给这些段补 scene_type：%s"
                           % " / ".join(str(x) for x in missing[:6]))
    mod = _scenes_module(script.get("_scenes"))

    draw_script = dict(script)
    draw_script["_style"] = plan
    draw_script["_total"] = tl["total"]
    th = theme if isinstance(theme, dict) else (None if plan.get("legacy") else plan.get("theme"))
    hold = script.get("sub_max_hold")
    by_id = {s.get("id"): s for s in tl["scenes"]}

    made = []
    for i, spec in enumerate(specs):
        scene, local = _resolve_time(tl, spec, by_id)
        t = float(scene["start"]) + float(local)
        frame_no = int(round(t * float(tl["fps"])))
        frame_no = max(0, min(int(tl["frames"]) - 1, frame_no))
        img = mod.draw_frame(scene, local, float(scene["dur"]), t, draw_script, frame_no)
        _require_canvas(img, scene, t, "draw_frame")
        img = K.draw_subtitle(img, K.sub_at(tl["scenes"], t, hold), theme=th,
                              words=K.sub_words_at(tl["scenes"], t, hold), **sub_kw)
        _require_canvas(img, scene, t, "draw_subtitle")
        path = os.path.join(outdir, "%02d_%s_%05.2f.png" % (i, scene.get("id") or "s", t))
        img.save(path, compress_level=PNG_COMPRESS_LEVEL)
        made.append(path)
    return made


def contact_sheet(paths, out_png, cols=None):
    """把多张静帧并排拼成一张联系表（复用 filmkit.contact_sheet），返回输出路径。"""
    K, _ST, _G = _modules()
    paths = [str(p) for p in (paths or [])]
    if not paths:
        raise ValueError("contact_sheet: paths 为空，没有可拼的帧")
    missing = [p for p in paths if not os.path.isfile(p)]
    if missing:
        raise FileNotFoundError("contact_sheet: 这些帧不存在：%s" % "、".join(missing[:4]))
    n = len(paths)
    c = int(cols) if cols else min(4, n)
    if c <= 0:
        raise ValueError("contact_sheet: cols 必须 >= 1，收到 %r" % (cols,))
    out = K.contact_sheet(paths, os.path.abspath(str(out_png)), cols=c, thumb_w=480)
    if not out or not os.path.isfile(out):
        raise RuntimeError("contact_sheet: 联系表没有落盘（%s）" % out_png)
    return out


# ---------------------------------------------------------------- 设计 -> 计划
def design_to_plan(design):
    """把 design dict 交给 designkit.design_to_plan 解析（本模块**不实现**设计 DSL）。

    designkit.py 由并行任务实现，可能还没就绪：import 失败、或文件在但没有
    ``design_to_plan`` 时，这里抛一条能照着做的中文错误，并指明绕过方式
    （直接给 render_preview_frames 传一份 stylekit 形状的 plan）。
    """
    DK = _designkit()
    fn = getattr(DK, "design_to_plan", None) if DK is not None else None
    if not callable(fn):
        raise RuntimeError(
            "designkit.design_to_plan(design) 不可用：designkit.py 尚未就绪（缺该函数）。"
            "previewkit 不自己实现设计 DSL（会造出第二份真源）；"
            "要立刻看画面，可先调 render_preview_frames(plan, ...) 直接喂一份 "
            "stylekit.resolve_script_style(script) 形状的 plan。")
    return _check_plan(fn(design))


def design_fingerprint(design):
    """设计指纹：优先 designkit.design_fingerprint；没有就按规范化 JSON 取 sha1 前 16 位。

    只用于"这份候选是不是同一版"，不参与像素/门禁判断。
    """
    DK = _designkit()
    fn = getattr(DK, "design_fingerprint", None) if DK is not None else None
    if callable(fn):
        try:
            return str(fn(design))
        except Exception:                                   # noqa: BLE001 —— 指纹实现坏了不毁预览
            pass
    payload = json.dumps(design, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), default=str)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- 12 秒样音
def _musicgen():
    """惰性 import 并行开发的 musicgen；没就绪就让异常往上走，由调用方降级。"""
    _ensure_paths()
    import musicgen
    for fn in ("render", "save"):
        if not callable(getattr(musicgen, fn, None)):
            raise RuntimeError("musicgen 缺 %s()（接口：render(duration, bpm=, key=, "
                               "scale=, mood=, seed=) -> (samples, report)；save(path, samples)）"
                               % fn)
    return musicgen


def _scorekit():
    """惰性 import scorekit：它拉起 numpy，import 期就要几百毫秒，默认不该付这笔钱。

    scorekit 的配乐风格表已经没有了（零模板），它只剩 legacy 逐样本兼容
    路径（固定 Dm/120、无 style/mood 参数）。这里保留它只是"兜底还能出声"：
    musicgen 没就绪时不至于整条样音链路断掉。
    """
    _ensure_paths()
    import scorekit
    return scorekit


def _audio_params(design):
    """设计里的配乐参数 -> 样音参数（缺什么就用模块里唯一的那份缺省值）。"""
    mu = {}
    au = design.get("audio") if isinstance(design, dict) else None
    if isinstance(au, dict) and isinstance(au.get("music"), dict):
        mu = au["music"]
    try:
        bpm = float(mu.get("bpm"))
    except (TypeError, ValueError):
        bpm = 104.0
    try:
        seed = int(mu.get("seed"))
    except (TypeError, ValueError):
        seed = 7
    return {
        "bpm": bpm,
        "key": str(mu.get("key") or "Dm"),
        "scale": str(mu.get("scale") or "minor"),
        "mood": str(mu.get("mood") or "steady"),
        "seed": seed,
    }


def _render_musicgen(params, path, secs):
    MG = _musicgen()
    samples, report = MG.render(secs, bpm=params["bpm"], key=params["key"],
                                scale=params["scale"], mood=params["mood"],
                                seed=params["seed"])
    MG.save(path, samples)
    return report


def _render_scorekit(params, path, secs):
    """兜底：scorekit 的 legacy 路径（没有 style/mood 参数，别传）。"""
    K = _scorekit()
    samples, report = K.render(secs, bpm=params["bpm"], key=params["key"],
                               seed=params["seed"])
    K.save(path, samples)
    return report


_ENGINES = (("musicgen", _render_musicgen), ("scorekit", _render_scorekit))


def _probe_wav(path):
    """读 WAV 头：声道数 / 采样率 / 帧数 / 位宽（只读头，不读样本）。"""
    with wave.open(str(path), "rb") as w:
        return {"channels": int(w.getnchannels()), "rate": int(w.getframerate()),
                "frames": int(w.getnframes()), "width": int(w.getsampwidth())}


def _read_pcm(path):
    """WAV -> (numpy 数组 [n,2] 的立体声, 原采样率)。8/16/24/32 位都认。"""
    import numpy as np
    got = _probe_wav(path)
    with wave.open(str(path), "rb") as w:
        raw = w.readframes(got["frames"])
    width, ch = got["width"], got["channels"]
    if width == 1:
        a = (np.frombuffer(raw, np.uint8).astype(np.float64) - 128.0) / 128.0
    elif width == 2:
        a = np.frombuffer(raw, "<i2").astype(np.float64) / 32768.0
    elif width == 3:
        b = np.frombuffer(raw, np.uint8).reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        a = ((v ^ (1 << 23)) - (1 << 23)).astype(np.float64) / 8388608.0
    elif width == 4:
        a = np.frombuffer(raw, "<i4").astype(np.float64) / 2147483648.0
    else:
        raise ValueError("不支持的位深：%d bit" % (width * 8))
    a = a[: (len(a) // ch) * ch].reshape(-1, ch)
    if ch == 1:
        a = np.stack([a[:, 0], a[:, 0]], axis=1)
    elif ch > 2:
        a = a[:, :2]                                    # 多声道只取前两路（样音不做混音）
    return a, got["rate"]


def _rewrite_wav(path, secs):
    """把 WAV 改写成 48 kHz / 立体声 / 正好 secs 秒（16-bit 落盘）。"""
    import numpy as np
    a, rate = _read_pcm(path)
    n = int(round(float(secs) * AUDIO_SR))
    if rate != AUDIO_SR and len(a):
        src = np.linspace(0.0, 1.0, num=len(a), endpoint=False)
        dst = np.linspace(0.0, 1.0, num=n, endpoint=False)
        a = np.stack([np.interp(dst, src, a[:, c]) for c in range(2)], axis=1)
    if len(a) < n:
        a = np.vstack([a, np.zeros((n - len(a), 2))])
    pcm = np.rint(np.clip(a[:n], -1.0, 1.0) * 32767.0).astype("<i2")
    tmp = path + ".part"
    with wave.open(tmp, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(AUDIO_SR)
        w.writeframes(pcm.tobytes())
    os.replace(tmp, path)                                # 原子替换：读的人不会看到半个文件


def _ensure_wav_contract(path, secs):
    """核一遍"48 kHz / 立体声 / 正好 secs 秒"，不合就地重写。返回核对后的头信息。

    为什么要兜这一道：musicgen 是另一路并行开发的模块，它的输出口径本模块不能假设；
    而三件套不齐的 WAV 没法当作"同一份证据"去比较（时长差 0.3 秒就听不出接缝在哪）。
    """
    got = _probe_wav(path)
    if (got["channels"] == 2 and got["rate"] == AUDIO_SR
            and abs(got["frames"] / float(got["rate"]) - float(secs)) <= 0.05):
        return got
    _rewrite_wav(path, secs)
    return _probe_wav(path)


def render_audio_preview(design, out_dir, seconds=AUDIO_PREVIEW_SECONDS):
    """生成一个候选的 12 秒样音（48 kHz 立体声），返回结果 dict，**绝不抛异常**。

    为什么优先 musicgen：它直接吃设计里的 bpm/key/scale/mood/seed，12 秒一次合成；
    没就绪就退回 scorekit 的 legacy 路径（它没有风格表，出的是固定 Dm/120），
    engine 字段会写明是谁出的，调用方据此知道这条样音能不能代表方案。
    两条都不可用时返回 {"ok": False, "reason": ...}——提案是决策环节，配乐引擎缺位不该
    让整条提案断掉，但也不许假装成功（ok=True 却指着一个不存在的文件比报错更糟）。

    返回 {"ok", "path", "secs", "engine", "report"}（secs = 生成耗时）；
    降级时多一个 "reason" 说明为什么换了引擎。
    """
    t0 = time.perf_counter()
    try:
        secs = float(seconds)
        if not secs > 0:
            return {"ok": False, "reason": "seconds 必须 > 0，收到 %r" % (seconds,)}
        out_dir = os.path.abspath(str(out_dir))
        os.makedirs(out_dir, exist_ok=True)
    except Exception as e:                              # noqa: BLE001
        return {"ok": False, "reason": "参数不合法：%s: %s" % (type(e).__name__, e)}

    path = os.path.join(out_dir, AUDIO_PREVIEW_NAME)
    params = _audio_params(design)
    why, report, engine = "", None, ""
    for name, fn in _ENGINES:
        try:
            report = fn(params, path, secs)
            _ensure_wav_contract(path, secs)
        except Exception as e:                          # noqa: BLE001 —— 失败只降级，不外抛
            why += ("；" if why else "") + "%s 不可用（%s: %s）" % (name, type(e).__name__, e)
            continue
        engine = name
        break
    if not engine:
        return {"ok": False, "reason": why or "没有可用的配乐引擎"}

    out = {"ok": True, "path": path, "secs": round(time.perf_counter() - t0, 3),
           "engine": engine, "report": report, "audio_secs": secs}
    if engine != _ENGINES[0][0]:
        out["reason"] = why                             # 降级原因留痕，别让"为什么不是 musicgen"成谜
    return out


# ---------------------------------------------------------------- 单候选预览
def preview_candidate(design, script, outdir, n_frames=DEFAULT_FRAMES, with_audio=False):
    """单个候选：design -> plan -> 分镜 -> 取默认时刻 -> 出图 + 联系表。

    返回 {"id", "sheet", "frames", "secs", "design_fp"}：
      id        候选 id（design["id"]，缺省 "cand"）
      sheet     联系表 PNG（绝对路径）
      frames    静帧 PNG 列表（绝对路径，单张 1920x1080）
      secs      本次耗时（秒，含 design_to_plan、全部落盘；开了样音时也含样音）
      design_fp 设计指纹（designkit 有就用它的，没有就用规范化 JSON 的 sha1）

    with_audio=True 时多给 audio（样音 WAV 绝对路径，失败为 ""）、audio_engine、
    audio_secs（**生成耗时**，不是音频长度），失败时再加 audio_error。
    默认不开：既有调用方只要静帧，白等 1~2 秒没道理。

    全程不联网、不合成旁白：时间轴来自 synthetic_timeline。
    """
    t0 = time.perf_counter()
    if not isinstance(design, dict):
        raise TypeError("design 必须是 dict，收到 %s" % type(design).__name__)
    plan = design_to_plan(design)

    script = dict(script or {})
    tl = synthetic_timeline(script, fps=_fps_of(script, plan), plan=plan)
    # 显式调一次：要求"先定分镜再取样"（同时把 scene_type 写进 tl["scenes"]，
    # 下面的 render_preview_frames 会用同一条确定性规则再算一遍，代价是微秒级）。
    _G = _modules()[2]
    sb = _G.plan_scenes(tl["scenes"], script, plan)
    # **降级必须可见**：剧本写了 type=chart 却被降成 list，预览里画出来的就不是 chart。
    # 这是"预览与成片不一致"最难察觉的一种（不是时长差异，是构图整个换了），
    # 所以把降级信息放进返回值，让人读输出和 proposal.json 都能看到。
    downgrades = []
    req = {}
    for ln in (script.get("lines") or []):
        if isinstance(ln, dict) and ln.get("type"):
            req[str(ln.get("id") or "")] = str(ln["type"])
    for row in (sb.get("scenes") or []):
        want = req.get(str(row.get("id") or ""))
        if want and want != row.get("type"):
            downgrades.append({"id": row.get("id"), "want": want,
                               "got": row.get("type"),
                               "dur": row.get("dur"),
                               "reason": "；".join(str(x) for x in (row.get("reason") or [])[:2])})

    times = default_times(tl, n_frames)
    outdir = os.path.abspath(str(outdir))
    frames = render_preview_frames(plan, script, times, outdir)
    sheet = contact_sheet(frames, os.path.join(outdir, "_sheet.png"), cols=len(frames))
    row = {
        "id": str(design.get("id") or "cand"),
        "sheet": sheet,
        "frames": frames,
        "secs": round(time.perf_counter() - t0, 3),
        "design_fp": design_fingerprint(design),
        # 被降级的段（剧本写了 type 但段太短画不出来）：预览必须自曝这一点，
        # 否则用户会以为"数据条没做出来"，而成片里是有的。
        "downgrades": downgrades,
    }
    if with_audio:
        au = render_audio_preview(design, outdir)
        row["audio"] = str(au.get("path") or "") if au.get("ok") else ""
        row["audio_engine"] = str(au.get("engine") or "")
        row["audio_secs"] = au.get("secs")
        if not au.get("ok"):
            # 失败原因必须留在行里：上层要能说"这一版的样音没出来，因为……"，
            # 而不是只看到一个空字符串去猜。
            row["audio_error"] = str(au.get("reason") or "未知原因")
        row["secs"] = round(time.perf_counter() - t0, 3)
    return row


# ---------------------------------------------------------------- CLI
def _load_json(path):
    path = os.path.abspath(str(path))
    if not os.path.isfile(path):
        raise SystemExit("[preview] 文件不存在：%s" % path)
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise SystemExit("[preview] %s 不是合法 JSON：%s" % (path, e)) from e


def _load_script(path):
    script = _load_json(path)
    if not isinstance(script, dict) or not isinstance(script.get("lines"), list):
        raise SystemExit("[preview] 剧本结构不对（需要 dict 且 lines 是数组）：%s" % path)
    if not script["lines"]:
        raise SystemExit("[preview] 剧本 lines 为空，预览没有内容可画：%s" % path)
    script["_path"] = os.path.abspath(str(path))
    script["_dir"] = os.path.dirname(script["_path"])
    return script


def _designs_from(payload, path):
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
        raise SystemExit("[preview] %s 必须是设计对象或设计数组" % path)
    bad = [i for i, d in enumerate(designs) if not isinstance(d, dict)]
    if bad:
        raise SystemExit("[preview] %s 里第 %s 项不是对象（设计必须是 dict）"
                         % (path, ", ".join(str(i + 1) for i in bad[:4])))
    if not designs:
        raise SystemExit("[preview] %s 里没有任何设计" % path)
    return designs


def _candidate_ids(designs):
    """候选 id：优先设计自带的 id，否则按 A/B/C 编号；同时做成文件名安全的唯一串。"""
    ids, used = [], set()
    for i, d in enumerate(designs):
        raw = d.get("id")
        if not raw:
            raw = CAND_IDS[i] if i < len(CAND_IDS) else "C%d" % (i + 1)
        # 保留 Unicode 字母（中文设计 id 仍能当目录名）；只把路径分隔符/标点换成 "-"
        base = re.sub(r"[^\w.-]+", "-", str(raw), flags=re.UNICODE).strip("-._") or "cand"
        cid, k = base, 2
        while cid in used:
            cid = "%s-%d" % (base, k)
            k += 1
        used.add(cid)
        ids.append(cid)
    return ids


def _human(rows, wall, outdir):
    print("== previewkit（离线预览：合成时间轴，不合成旁白、不联网）")
    for r in rows:
        print("   候选 %-10s %2d 帧  %6.2fs  %s"
              % (r["id"], len(r["frames"]), r["secs"], r["sheet"]))
        for d in (r.get("downgrades") or []):
            print("     [降级] %s：剧本要求 %s，但因段长 %.1fs 不足画成了 %s"
                  % (d.get("id"), d.get("want"), float(d.get("dur") or 0), d.get("got")))
    print("   合计 %.2fs -> %s（%d 个候选）" % (wall, outdir, len(rows)))
    if any(r.get("downgrades") for r in rows):
        print("   注意：上面标了 [降级] 的段**预览与成片不一致**（成片段长由真实 TTS 决定，"
              "通常更长、不会被降级）。看那几段时请以 --stage stills 或成片为准。")


def main(argv=None):
    _setup_stdio()
    ap = argparse.ArgumentParser(
        description="离线方案预览：设计 -> 合成时间轴 -> 静帧 + 联系表（不联网）")
    ap.add_argument("--design", required=True,
                    help="design.json：单个设计 / 设计数组 / {\"designs\":[...]}")
    ap.add_argument("--script", default=DEFAULT_SCRIPT, help="film.json（默认模板片）")
    ap.add_argument("--outdir", default=DEFAULT_OUTDIR, help="输出目录（默认 ./preview）")
    ap.add_argument("--n-frames", type=int, default=DEFAULT_FRAMES,
                    help="每个候选取几帧（默认 4）")
    ap.add_argument("--json", action="store_true", help="stdout 只输出 JSON（机读）")
    a = ap.parse_args(argv)

    rows, err = [], None
    t_all = time.perf_counter()
    designs = []
    try:
        if a.n_frames < 1:
            raise SystemExit("[preview] --n-frames 必须 >= 1，收到 %d" % a.n_frames)
        designs = _designs_from(_load_json(a.design), a.design)
        script = _load_script(a.script)
        ids = _candidate_ids(designs)
        outdir = os.path.abspath(str(a.outdir))
        os.makedirs(outdir, exist_ok=True)
        for cid, design in zip(ids, designs):
            cdir = outdir if len(designs) == 1 else os.path.join(outdir, cid)
            row = preview_candidate(design, script, cdir, n_frames=a.n_frames)
            row["id"] = cid
            rows.append(row)
    except SystemExit as e:                                 # noqa: BLE001 —— --json 也必须只吐 JSON
        err = str(e.code) if e.code not in (None, 0) else "已中止"
    except Exception as e:                                  # noqa: BLE001
        err = "%s: %s" % (type(e).__name__, e)
    wall = round(time.perf_counter() - t_all, 3)
    ok = err is None and bool(rows) and len(rows) == len(designs)

    if a.json:
        out = {"ok": ok, "candidates": rows, "wall_s": wall}
        if err is not None:
            out["error"] = err
        json.dump(out, sys.stdout, ensure_ascii=False, indent=1)
        sys.stdout.write("\n")
    else:
        if err is not None:
            print("[preview] 失败：%s" % err, file=sys.stderr)
        else:
            _human(rows, wall, os.path.abspath(str(a.outdir)))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
