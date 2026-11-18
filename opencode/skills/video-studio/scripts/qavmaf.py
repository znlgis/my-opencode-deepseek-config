# -*- coding: utf-8 -*-
"""qavmaf.py — 全片 VMAF/SSIM 画质门禁。

为什么要有它
------------
qa_frames.py 只看"帧级异常"（白闪/空窗/静止/同步），看不出"整片画质掉了多少"：
CRF 调高、preset 降档、二次编码、缩放插值，都会让画面"糊一点"，但每帧都正常。
VMAF（Netflix 感知质量分，0–100）就是量化这件事的标准尺子。

历史坑（必须避开）
------------------
**参考帧必须由场景函数实时重渲染**（scenes.draw_frame），绝不能拿成片自己抽的帧
当参考——那是"拿成片和成片比"，VMAF 恒等于 100，门禁形同虚设。
所以本模块的参考帧 = render.py 的 frames 阶段同一套绘制管线：
    draw_frame -> draw_subtitle -> 片头/片尾淡入淡出（wash/black）
逐帧生成 rgb24，经 stdin 管道喂给 ffmpeg 的 rawvideo 输入，与成片解码流做
libvmaf / ssim 比较。

性能与流式
----------
1080p 每帧 1920x1080x3 = 6.22 MB。本模块**只渲一遍帧、边渲边喂**，内存恒定
（不缓存整片帧序列）；一次 ffmpeg 调用里用 filter_complex + split 同时算 VMAF
与 SSIM，不需要渲染第二遍。
实测（本机 12 核 / 24 线程，无 CUDA，仓库模板片 30.6 s / 918 帧 / 1080p，
2026-11-10 实测）：
- 全片逐帧（参考帧渲染 + VMAF+SSIM 联合比较）12.0 s，约 0.40x 实时；
  参考帧本身约 10 ms/帧（模板场景已缓存背景：绘制 6.0 ms + tobytes 4.2 ms），
  与 libvmaf 消费流水重叠；瓶颈在参考帧渲染侧（消费端约 76 帧/秒，
  渲染端约 99 帧/秒，实际总吞吐约 77 帧/秒 ≈ 两者中较慢的一段加上少量同步开销）。
- 按此量级，12 分钟片约 5 分钟（真实创意场景 draw_frame 更重，会线性变慢）。
- sample 模式（每 5 帧取 1 帧）2.95 s 跑完 184 帧，VMAF 与全片差 0.34 分。
- libvmaf threads=0 只有约 20 帧/秒；默认 min(8, CPU) 时约 124–160 帧/秒。
  分数对线程数完全不敏感：threads=0/1/4/8/16 与跨进程复跑，240 帧逐帧
  VMAF 分数 sha1 全部相同——多线程不影响确定性。
- 下采样交付（1280x720 / 960x540，参考流 lanczos 缩到成片尺寸）与 1080p
  同片比：VMAF 差 -0.22 / -0.03 分。

契约
----
report(film_mp4, tl, scenes_mod, script, *, mode="full", out_json=None,
       vmaf_pass=92.0, vmaf_warn=96.0, ssim_pass=0.95, ssim_warn=0.98,
       limit_seconds=None, sample_step=None, threads=DEFAULT_THREADS,
       log=print) -> dict

    film_mp4   : 成片路径（mp4；只比较视频流）
    tl         : timeline.json 的路径或已载入的 dict（需要 fps/total/frames/scenes）
    scenes_mod : 场景模块对象（含 draw_frame）或 scenes.py 路径
    script     : film.json 的路径或已载入的 dict（需要与 frames 阶段同一份；
                 含 style 解析结果时直接用，否则本模块按 stylekit 重新解析）
    mode       : "full" 全片逐帧 / "sample" 抽样（默认每 5 帧取 1 帧）
    vmaf_pass  : VMAF 均值低于此值 -> gate.status="fail"（默认 92.0）
    vmaf_warn  : VMAF 均值低于此值但 >= vmaf_pass -> pass + warning（默认 96.0）
    ssim_pass/warn : SSIM 阈值，**诊断项**（见"三档语义"第 3 条）
    limit_seconds  : 只比较前 N 秒（自测/冒烟用；会记 warning 说明非全片）
    sample_step    : 抽样步长（默认 SAMPLE_STEP=5；mode="full" 时给了它也按抽样算）
    threads        : libvmaf 线程数（默认 min(8, CPU)；实测多线程不影响分数）

返回 dict：
    judged          bool   是否真的比较了帧（False = 没查成，绝对不能当通过）
    mode            str    "full" / "sample"
    vmaf/ssim       dict   {score, pass, warn, status, detail, min, ...}
                           status ∈ pass / warn / fail / skipped
    frames_compared int    实际参与比较的帧数
    elapsed_s       float  总耗时（含参考帧渲染）
    warnings        list   给人看的中文说明（含"本次没查/只查了一部分"）
    errors          list   导致 fail 的硬错误
    gate            dict   {status: "pass"|"fail"|"skipped", detail}
    ref_scale       dict|None  参考流缩放映射（{"from":[1920,1080],"to":[1280,720]}；
                   成片与内部构图同尺寸时为 None）

三档语义（与 gates.py 的 OK/FAIL/SKIP 同义）
-------------------------------------------
1. **fail**：VMAF 均值 < vmaf_pass（默认 92）。这是唯一让质检阻断的情况。
2. **pass**：VMAF 均值 >= vmaf_pass；其中 [vmaf_pass, vmaf_warn) 区间记 warning
   （"通过了，但低于建议阈值，建议复核"）。
3. **skipped**：本次**没有查**，必须写明原因，**绝不冒充 pass**。触发条件：
   成片不存在 / 缺 timeline / 缺 scenes（或 draw_frame 抛错）/ 缺 filmkit /
   找不到 ffmpeg / ffmpeg 构建没有 libvmaf / 成片解码失败 / 比较 0 帧 /
   参考帧渲染中断（宁可 skipped 也不给"只查了一半"的 pass）。
   - 分辨率结构性不符 → **fail**（不是"没法查"）：
     · 成片分辨率 > 内部构图分辨率（1920x1080）：**上采样不是合法交付**，
       只会把 1080p 包装成"更高分辨率"；
     · 宽高比不是 16:9 横屏（本项目只做 16:9 缩放输出）；
     · 成片尺寸与 timeline.video 声明的交付尺寸不一致（顶层规格被改动）；
     · 读不出成片分辨率。
   - "下采样交付"是合法配置：内部构图恒 1920x1080，编码时 lanczos
     缩到 1280x720 / 960x540。此时本模块**在成片分辨率下比较**——参考流在
     ffmpeg 滤镜图里用 scale=w:h:flags=lanczos 缩到成片分辨率（与 FrameSink
     编码时同口径，不用 PIL resize）；返回 dict 的 ref_scale 记录映射关系。
     VMAF 阈值按 1080p 标定，下采样交付沿用同一套（实测 720p 与 1080p
     的 VMAF 差不到 3 分）。
   - SSIM 是诊断项不单独阻断（VMAF 才是感知门禁，且文本画面的 SSIM 对字幕
     边缘过于敏感）；SSIM 低于阈值只记 warning，但会写得足够醒目。

CLI
---
    python qavmaf.py --film out/film.mp4 --timeline work/timeline.json \
        --scenes scenes.py --script film.json [--mode full|sample] \
        [--limit-seconds 30] [--out work/qa/vmaf.json] [--json]

    退出码：0 = pass；1 = fail；2 = skipped（CI 必须把 2 也当"没通过"处理）
    --json  只把报告 JSON 打到 stdout（人读摘要走 stderr，便于管道消费）
    --selftest  跑内置自检（合成片，验证三档语义不许"没查写成通过"）

依赖：stdlib + numpy（分位数）+ Pillow（参考帧）+ 同目录 filmkit.py（W/H、字幕、
      ffmpeg/ffprobe 探测、探测函数）。ffmpeg 通过 FILMKIT_FFMPEG 覆盖，
      找不到可执行文件一律 fail-closed（skipped + 明确原因，不裸调用 "ffmpeg"）。
"""
from __future__ import annotations

__version__ = "1.1.0"

import argparse
import importlib.util
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

import numpy as np
from PIL import Image, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

try:                                    # 同目录 filmkit 提供 W/H、字幕、ffmpeg 探测
    import filmkit as _K
except Exception:                       # noqa: BLE001 —— 独立拷贝本文件时也不该在 import 阶段炸
    _K = None


# ---------------------------------------------------------------- 可调参数
# VMAF 阈值：本机实测 crf17 模板片 ~98+，crf30 明显掉档（见 .team 实测报告）。
# 92 是"明显劣化"的失败线，96 是"建议复核"线；两者都可用 CLI 覆盖。
VMAF_PASS = 92.0
VMAF_WARN = 96.0
# SSIM 诊断阈值（只记 warning，不单独阻断）
SSIM_PASS = 0.95
SSIM_WARN = 0.98
# 抽样模式默认步长：30 fps 下每 5 帧取 1 帧 = 6 fps，约 1/5 耗时
SAMPLE_STEP = 5
# 参考帧与成片同一时刻的像素差超过这个值就记 warning（0–1 归一化 MAE）。
# 正常同源时只有编码噪声（实测 << 0.01）；接近 0.05 说明渲染管线已漂移
# （例如 render.py 改了字幕/淡入淡出，而本模块还在复刻旧逻辑）。
SPOT_WARN_MAE = 0.02
# 抽帧对齐误差上限：成片帧数与时间轴帧数差超过这个比例，说明时间轴与成片不是同一次渲染
FRAME_COUNT_TOL = 0.02
# 单帧采样分辨率（spot check 用）
SPOT_W = 160
SPOT_H = 90

_FFMPEG_CACHE = ""
_FFPROBE_CACHE = ""
_CAP_CACHE = {}                         # {ffmpeg 路径: {"libvmaf": bool, "ssim": bool}}
# libvmaf 线程数默认值：实测 threads=0 时 libvmaf 只用 1–2 个线程（约 20 帧/秒），
# 8 线程约 124 帧/秒、16 线程约 142–164 帧/秒，而**分数逐位相同**（见 det 实测），
# 所以默认显式给线程数，别把 20 帧/秒的默认值留在这条门禁上。
DEFAULT_THREADS = min(8, os.cpu_count() or 4)


# ---------------------------------------------------------------- 小工具
def _finite(v) -> bool:
    try:
        return math.isfinite(float(v))
    except (TypeError, ValueError):
        return False


def _f(v, default=0.0) -> float:
    try:
        x = float(v)
        return x if math.isfinite(x) else float(default)
    except (TypeError, ValueError):
        return float(default)


def _tail(text, n=400) -> str:
    s = str(text or "").strip().replace("\r", "")
    return s[-n:] if len(s) > n else s


def _setup_stdio():
    """输出被重定向时切 UTF-8（Windows 控制台 GBK 会把中文日志打成乱码）。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:               # noqa: BLE001
            pass


def _write_json(data, path):
    path = str(path)
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    txt = json.dumps(data, ensure_ascii=False, indent=1, allow_nan=False)
    with open(path, "w", encoding="utf-8") as fp:
        fp.write(txt + "\n")


# ---------------------------------------------------------------- ffmpeg / ffprobe
def _probe_exe(cand):
    """把候选解析成真实可执行文件路径；解析不了返回 ""（不做"裸命令兜底"）。"""
    cand = str(cand or "").strip().strip('"')
    if not cand:
        return ""
    if os.path.isfile(cand):
        return os.path.abspath(cand)
    looks_pathy = os.path.isabs(cand) or os.sep in cand or bool(os.altsep and os.altsep in cand)
    if looks_pathy:
        return ""
    hit = shutil.which(cand)
    return os.path.abspath(hit) if hit else ""


def _ffmpeg() -> str:
    """解析 ffmpeg：FILMKIT_FFMPEG > filmkit.FFMPEG > PATH。找不到就抛 ValueError。

    与 qa_frames 的宽松版不同：这里**不**回退裸命令 "ffmpeg"——门禁必须
    fail-closed，让 report() 把"缺能力"写成 skipped，而不是等 subprocess
    在远处抛 FileNotFoundError 再被人当"通过"。
    """
    global _FFMPEG_CACHE
    if _FFMPEG_CACHE:
        return _FFMPEG_CACHE
    env = (os.environ.get("FILMKIT_FFMPEG") or "").strip()
    if env:
        exe = _probe_exe(env)
        if exe:
            _FFMPEG_CACHE = exe
            return exe
        raise ValueError("FILMKIT_FFMPEG=%r 指向的文件不存在或不可执行" % env)
    if _K is not None:
        exe = _probe_exe(getattr(_K, "FFMPEG", ""))
        if exe:
            _FFMPEG_CACHE = exe
            return exe
    exe = _probe_exe(shutil.which("ffmpeg") or "")
    if not exe:
        raise ValueError("找不到 ffmpeg 可执行文件：请设 FILMKIT_FFMPEG 或把 ffmpeg 放进 PATH")
    _FFMPEG_CACHE = exe
    return exe


def _ffprobe() -> str:
    """ffprobe 解析：优先 filmkit.ffprobe_path()，否则找 ffmpeg 同目录。"""
    global _FFPROBE_CACHE
    if _FFPROBE_CACHE:
        return _FFPROBE_CACHE
    if _K is not None:
        try:
            p = _probe_exe(_K.ffprobe_path())
            if p:
                _FFPROBE_CACHE = p
                return p
        except Exception:               # noqa: BLE001
            pass
    ff = _ffmpeg()
    d = os.path.dirname(ff)
    names = ("ffprobe.exe", "ffprobe") if os.name == "nt" else ("ffprobe", "ffprobe.exe")
    for name in names:
        p = os.path.join(d, name)
        if os.path.isfile(p):
            _FFPROBE_CACHE = p
            return p
    hit = _probe_exe(shutil.which("ffprobe") or "")
    if hit:
        _FFPROBE_CACHE = hit
        return hit
    _FFPROBE_CACHE = ""                 # 允许为空：_probe_film 会退回 ffmpeg -i 解析
    return ""


def _run(cmd, **kw):
    """跑一个短命令，返回 CompletedProcess（不抛异常）。"""
    return subprocess.run(cmd, capture_output=True, **kw)


def _has_filter(listing: str, name: str) -> bool:
    """在 ffmpeg -filters 输出里找滤镜名（形如 ".. libvmaf  VV->V  ..."）。"""
    for ln in str(listing or "").splitlines():
        parts = ln.split()
        for i, tok in enumerate(parts):
            if tok != name or i == 0:
                continue
            flags = parts[i - 1]
            if not flags or set(flags) - set("TSC."):
                continue
            if i + 1 < len(parts) and "->" in parts[i + 1]:
                return True
    return False


def _capabilities(ff: str) -> dict:
    """探测 ffmpeg 是否带 libvmaf / ssim（只探一次，按可执行路径缓存）。"""
    key = os.path.normcase(os.path.abspath(ff))
    hit = _CAP_CACHE.get(key)
    if hit is not None:
        return hit
    p = _run([ff, "-hide_banner", "-filters"])
    txt = (p.stdout + p.stderr).decode("utf-8", "replace")
    cap = {"libvmaf": _has_filter(txt, "libvmaf"), "ssim": _has_filter(txt, "ssim")}
    _CAP_CACHE[key] = cap
    return cap


def _parse_rate(txt):
    """把 ffprobe 的 "30000/1001" / "30" 解析成帧率；解析不了返回 None。"""
    txt = str(txt or "").strip()
    if not txt:
        return None
    try:
        if "/" in txt:
            num, _, den = txt.partition("/")
            num, den = float(num), float(den or 0)
            return num / den if num > 0 and den > 0 else None
        v = float(txt)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def _probe_film(film) -> dict:
    """探测成片视频流：宽高 / 帧率 / 时长 / 帧数 / 编码。失败时字段为 None/0。"""
    info = {"width": None, "height": None, "fps": None, "duration": 0.0,
            "nb_frames": None, "codec": "", "source": "none"}
    if _K is not None:
        try:
            raw = _K.probe(film) or {}
        except Exception:               # noqa: BLE001
            raw = {}
        for st in (raw.get("streams") or []):
            if str(st.get("codec_type") or "") != "video":
                continue
            try:
                info["width"] = int(st.get("width") or 0) or None
                info["height"] = int(st.get("height") or 0) or None
            except (TypeError, ValueError):
                pass
            info["fps"] = _parse_rate(st.get("r_frame_rate")) or _parse_rate(st.get("avg_frame_rate"))
            info["codec"] = str(st.get("codec_name") or "")
            try:
                info["nb_frames"] = int(st.get("nb_frames") or 0) or None
            except (TypeError, ValueError):
                info["nb_frames"] = None
            try:
                info["duration"] = float((raw.get("format") or {}).get("duration") or 0) or \
                    float(st.get("duration") or 0)
            except (TypeError, ValueError):
                info["duration"] = 0.0
            info["source"] = "filmkit.probe"
            break
    if info["width"] is None:
        pp = _ffprobe()
        if pp:
            p = _run([pp, "-v", "error", "-show_entries",
                      "stream=codec_type,codec_name,width,height,r_frame_rate,avg_frame_rate,"
                      "nb_frames,duration:format=duration",
                      "-of", "json", str(film)])
            if p.returncode == 0 and p.stdout:
                try:
                    data = json.loads(p.stdout.decode("utf-8", "replace"))
                except Exception:       # noqa: BLE001
                    data = {}
                for st in (data.get("streams") or []):
                    if str(st.get("codec_type") or "") != "video":
                        continue
                    info["width"] = int(st.get("width") or 0) or None
                    info["height"] = int(st.get("height") or 0) or None
                    info["fps"] = _parse_rate(st.get("r_frame_rate")) or \
                        _parse_rate(st.get("avg_frame_rate"))
                    info["codec"] = str(st.get("codec_name") or "")
                    try:
                        info["nb_frames"] = int(st.get("nb_frames") or 0) or None
                    except (TypeError, ValueError):
                        info["nb_frames"] = None
                    try:
                        info["duration"] = float((data.get("format") or {}).get("duration") or 0)
                    except (TypeError, ValueError):
                        info["duration"] = 0.0
                    info["source"] = "ffprobe"
                    break
    return info


# ---------------------------------------------------------------- 装载输入
def load_module(path, name="qavmaf_scenes"):
    """按独立模块名加载 scenes.py（避免跨片污染，与 render.py.load_module 同口径）。"""
    path = os.path.abspath(str(path))
    if not os.path.exists(path):
        raise ValueError("scenes.py 不存在：%s" % path)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError("scenes.py 无法作为模块加载：%s" % path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        # 失败也把半成品从 sys.modules 摘掉，避免下次加载命中坏模块
        if not hasattr(mod, "draw_frame"):
            sys.modules.pop(name, None)
    return mod


def _as_timeline(tl) -> dict:
    if isinstance(tl, dict):
        return tl
    path = os.path.abspath(str(tl))
    if not os.path.exists(path):
        raise ValueError("timeline 不存在：%s" % path)
    try:
        with open(path, encoding="utf-8") as fp:
            data = json.load(fp)
    except json.JSONDecodeError as e:
        raise ValueError("timeline 不是合法 JSON：%s（%s）" % (path, e)) from e
    if not isinstance(data, dict):
        raise ValueError("timeline 顶层必须是对象：%s" % path)
    return data


def _as_scenes(scenes_mod):
    if scenes_mod is None:
        raise ValueError("没有场景模块（scenes.py）：参考帧必须由场景函数实时渲染")
    if hasattr(scenes_mod, "draw_frame"):
        return scenes_mod
    return load_module(scenes_mod)


def _as_script(script) -> dict:
    if isinstance(script, dict):
        return dict(script)
    path = os.path.abspath(str(script))
    if not os.path.exists(path):
        raise ValueError("剧本不存在：%s" % path)
    try:
        with open(path, encoding="utf-8") as fp:
            data = json.load(fp)
    except json.JSONDecodeError as e:
        raise ValueError("剧本不是合法 JSON：%s（%s）" % (path, e)) from e
    if not isinstance(data, dict):
        raise ValueError("剧本顶层必须是对象：%s" % path)
    return data


# ---------------------------------------------------------------- 参考帧管线（复刻 render.py）
def _looks_legacy(script) -> bool:
    """有没有"风格"信息：没有就是 legacy 外观（与 stylekit 的判据一致）。"""
    vis = script.get("visual") if isinstance(script.get("visual"), dict) else {}
    mus = script.get("music") if isinstance(script.get("music"), dict) else {}
    return ("style" not in script) and not vis.get("theme") and not mus.get("style")


def _style_plan(script) -> dict:
    """解析剧本风格计划：**design 优先**，其次 script["_style"]，最后 stylekit。

    为什么必须先看 design："这一版长什么样"由 work/design.json 决定，
    而 qavmaf 是**独立进程**——它拿到的 script 里没有 render 塞进去的 _style。
    直接调 stylekit 会把 design 路径的片子解析成 tech-deep（默认风格），
    参考帧用一套主题、成片用另一套：实测 VMAF 1.50、同源抽查 MAE=0.0384。
    这不是"画质劣化"，是**参考帧根本不是同一部片**。
    """
    # ① design（与 render._plan_from_design 同一口径）
    design = script.get("design") if isinstance(script, dict) else None
    if isinstance(design, dict) and design:
        import designkit as DK
        bad = DK.validate_design(design)
        if bad:
            raise LookupError("design.json 没过校验，参考帧无法与成片同源：%s" % "；".join(bad[:3]))
        return DK.design_to_plan(design)
    # ② 调用方已经解析过（extract_frames 会塞进来）
    cached = script.get("_style") if isinstance(script, dict) else None
    if isinstance(cached, dict) and cached:
        return cached
    # ③ 老路径：stylekit
    try:
        import stylekit as ST
    except Exception as e:              # noqa: BLE001
        if _looks_legacy(script):
            return {"legacy": True, "style": "legacy", "theme_id": None, "theme": None}
        raise LookupError("缺 stylekit.py，无法解析剧本风格（%s）；"
                          "参考帧外观无法与成片同源" % e) from e
    return ST.resolve_script_style(script)


def _theme_of(plan):
    """复刻 render.py:_theme_for —— 非 legacy 返回 stylekit 的**基础主题**。

    注意：render.py 的 _theme_for 用的是 THEMES[plan["theme_id"]]（基础主题，
    不含 film.json 的 visual.theme_overrides），字幕颜色/字体族都从这里取。
    本函数必须与它逐个字段一致，否则参考帧的字幕与成片不同源、VMAF 会被误拉低。
    """
    tid = plan.get("theme_id")
    try:
        import stylekit as ST
        themes = getattr(ST, "THEMES", {}) or {}
        if tid in themes:
            return themes[tid]
    except Exception:                   # noqa: BLE001
        pass
    th = plan.get("theme")
    return th if isinstance(th, dict) else None


COMPARED_ASPECT = 16.0 / 9.0      # 缺省 16:9；实际比较时按当前画布取（见 canvas_aspect()）
ASPECT_TOL = 0.01                 # 宽高比容差（与 filmkit.video_plan 一致）


def canvas_aspect():
    """当前画布的宽高比（横屏 16:9 / 竖屏 9:16），以 layoutkit 为准。

    为什么不再写死 16:9：竖屏片（--layout portrait）成片是 1080x1920，写死 16:9 的话
    G11 会在"分辨率结构性不符"这一档直接 fail——而这不是画质问题，是版式问题。
    取不到画布时回落到 16:9（与历史行为一致，绝不因为读不到就判 fail）。
    """
    try:
        import layoutkit                                # noqa: PLC0415
        return float(layoutkit.W) / float(layoutkit.H)
    except Exception:                                   # noqa: BLE001
        return COMPARED_ASPECT



def _declared_out_size(tl, script):
    """交付规格：timeline.video（render.py 写出的快照）优先，其次剧本里的 video 块。

    返回 (out_w, out_h)；未声明时返回 None（老 timeline 没有 video 块时的行为）。
    """
    for src in ((tl or {}).get("video"), (script or {}).get("video")):
        if not isinstance(src, dict):
            continue
        try:
            ow = int(src.get("out_w") or src.get("width") or src.get("out_width") or 0)
            oh = int(src.get("out_h") or src.get("height") or src.get("out_height") or 0)
        except (TypeError, ValueError):
            continue
        if ow > 0 and oh > 0:
            return (ow, oh)
    return None


def _judge_sizes(info, tl, script, comp_w, comp_h):
    """判定"成片分辨率 vs 内部构图分辨率"，返回 (verdict, ref_scale, detail)。

    verdict:
      "same"   成片与构图同尺寸，直接逐帧比较
      "scale"  成片是 16:9 下采样交付，参考流在 ffmpeg 滤镜图里 lanczos 缩到成片尺寸
      "fail"   结构性不符：上采样 / 非 16:9 / 读不出尺寸 / 与声明交付尺寸不一致

    合法配置是"内部构图恒 1920x1080，编码时 lanczos 缩到 1280x720/960x540"。
    上采样不是合法交付（只会把 1080p 包装成更高分辨率），这里明确挡住。
    """
    fw, fh = info.get("width"), info.get("height")
    if not fw or not fh:
        return "fail", None, ("读不出成片分辨率（ffprobe/filmkit.probe 都没给出宽高）："
                              "无法确认成片与内部构图 %dx%d 的关系" % (comp_w, comp_h))
    fw, fh = int(fw), int(fh)
    # 先对"声明的交付规格"：成片尺寸必须与 timeline.video 一致，否则说明规格被改过
    # （拿错成片 / 手动替换过文件），此时后面任何比较都不可信。
    declared = _declared_out_size(tl, script)
    if declared and declared != (fw, fh):
        return "fail", None, (
            "成片分辨率 %dx%d 与 timeline.video 声明的交付尺寸 %dx%d 不一致："
            "规格被改动过（或是拿错了成片），不能按该时间轴验收"
            % (fw, fh, declared[0], declared[1]))
    if fw >= comp_w and fh >= comp_h:
        if (fw, fh) == (comp_w, comp_h):
            return "same", None, "%dx%d" % (comp_w, comp_h)
        return "fail", None, (
            "成片分辨率 %dx%d 大于内部构图 %dx%d：**上采样不是合法交付**——"
            "上采样只会把 1080p 的清晰度包装成更高分辨率（假 2K/4K）。"
            "要更高分辨率请改内部构图，不要把成片放大后交付" % (fw, fh, comp_w, comp_h))
    if fw > comp_w or fh > comp_h:
        return "fail", None, (
            "成片分辨率 %dx%d 与内部构图 %dx%d 交叉（一维大、一维小）："
            "没有任何合法的渲染路径会产生这个尺寸" % (fw, fh, comp_w, comp_h))
    want_aspect = canvas_aspect()
    if abs(fw / float(fh) - want_aspect) > ASPECT_TOL:
        return "fail", None, (
            "成片分辨率 %dx%d 宽高比 %.3f:1，与当前画布不符（期望 %.3f:1）："
            "横屏片只交付 16:9、竖屏片只交付 9:16 的缩放结果；比例不符说明成片被裁切/变形了"
            % (fw, fh, fw / float(fh), want_aspect))
    return ("scale", {"from": [comp_w, comp_h], "to": [fw, fh]},
            "参考流 %dx%d lanczos -> 成片 %dx%d" % (comp_w, comp_h, fw, fh))


def _prepare_script(script, tl):
    """返回 (script_for_draw, theme)：清理出与 render.py frames 阶段同一份输入。

    - script["_total"] = tl["total"]（场景里的进度条/HUD 依赖它）
    - script["_style"] = 风格计划（scenes.py 自己会读它决定配色）
    - theme：字幕主题（legacy = None）
    """
    sc = dict(script)                   # 浅拷贝：不改调用方的对象
    sc["_total"] = float(_f(tl.get("total"), 0.0))
    # design 必须**优先于**缓存的 _style：调用方（render.st_qa）可能已经用一个
    # 错误的 _style 污染过 script —— 它自己解析时没传 work、读不到 design.json，
    # 于是 _style 是 stylekit 的 auto 结果。看到 _style 就直接用，等于沿用那个错误。
    if isinstance(sc.get("design"), dict) and sc["design"]:
        plan = _style_plan(sc)
        sc["_style"] = plan
    else:
        plan = sc.get("_style")
        if not isinstance(plan, dict):
            plan = _style_plan(sc)
            sc["_style"] = plan
    legacy = bool((plan or {}).get("legacy"))
    if legacy:
        return sc, None
    theme = _theme_of(plan)
    if theme is None:
        raise LookupError("非 legacy 剧本解析不到主题（stylekit.THEMES 缺 %r）"
                          % ((plan or {}).get("theme_id"),))
    return sc, theme


def _sub_kw_for(script) -> dict:
    """从已解析的风格计划里取字幕样式参数（与 render.py:_sub_kw 同口径）。

    ⚠ 为什么参考帧必须传这些参数（实测的**误杀好片**缺陷）：
    render.py 的 frames / stills 阶段会传 design 的 visual.subtitle（size/align/
    plate_alpha/radius/max_lines/family/y）；参考帧不传就永远是 filmkit 的模块
    默认值（size=46 等）。同一部片（设计 size=52 / radius=14 / plate_alpha=180）
    实测 VMAF 91.33 —— 被阈值 92 判 fail，而两条路径用同一份参数时 ≥96。
    门禁误杀好片比漏查更有害，所以这里必须与主渲染路径吃同一份参数。

    实现在 filmkit.subtitle_kwargs（**不复制第二份**：两份迟早漂移）；
    老版 filmkit 没有这个函数时退回空 dict（= 旧行为，跨版本兼容）。
    """
    plan = script.get("_style") if isinstance(script, dict) else None
    fn = getattr(_K, "subtitle_kwargs", None)
    if fn is None:                      # 老 filmkit：还没有字幕样式参数
        return {}
    try:
        kw = fn(plan)
    except Exception:                   # noqa: BLE001 —— 样式解析失败不该挡住整片门禁
        return {}
    return kw if isinstance(kw, dict) else {}


def _draw_subtitle_like_render(frame, text, theme, words, kw=None):
    """烧字幕，与 render.py 同口径（带 words 词边界折行与设计字幕样式）。

    ⚠ 参考帧不传 words 也是**会误杀好片**的缺陷（实测）：
    render.py 的 frames / stills 阶段调 draw_subtitle 时总会传 words，
    字幕会按词边界折行（"…整整 / 一句旁白"）；参考帧不传就退回字符折行
    （"…整整一 / 句旁白"）。38 字字幕只差一个断点、约 2% 像素不同，
    但近静态画面的 VMAF 从 99.95 掉到 8.55 —— 门禁会把同源的好片判 fail。
    kw 是 design 的字幕样式（见 _sub_kw_for）；老版 filmkit 的 draw_subtitle
    没有 words 形参，TypeError 时退回旧调用（保留）。
    """
    kw = kw or {}
    if not words:
        return _K.draw_subtitle(frame, text, theme=theme, **kw)
    try:
        return _K.draw_subtitle(frame, text, theme=theme, words=words, **kw)
    except TypeError:                   # 老 filmkit：没有 words 形参（保持旧行为）
        return _K.draw_subtitle(frame, text, theme=theme, **kw)


def _draw_ref_frame(scenes, scenes_mod, script, theme, fi, fps, total):
    """渲染第 fi 帧参考图（RGB）。逐行复刻 render.py:_render_range 的 859–885 行。

    这一步是门禁的"参考系"：任何一行不一致（字幕、淡入淡出、wash 色）都会让
    VMAF 系统性偏低。改 render.py 的绘制顺序时必须同步改这里。
    """
    t = int(fi) / float(fps)
    idx = 0
    n = len(scenes)
    while idx + 1 < n and t >= scenes[idx + 1]["start"]:
        idx += 1
    s = scenes[idx]
    lt = t - s["start"]
    frame = scenes_mod.draw_frame(s, lt, s["dur"], t, script, int(fi))
    if frame is None:
        raise RuntimeError("场景 %s 在 t=%.2fs 返回 None" % (s.get("id"), t))
    if not isinstance(frame, Image.Image):
        raise RuntimeError("场景 %s 的 draw_frame 没有返回 PIL.Image（收到 %s）"
                           % (s.get("id"), type(frame).__name__))
    if frame.mode != "RGB":
        frame = frame.convert("RGB")
    hold = script.get("sub_max_hold")
    text = _K.sub_at(scenes, t, hold)
    if text:
        # 词表取法与 render.py 一致（老 filmkit 没有 sub_words_at 时返回 None）
        fn = getattr(_K, "sub_words_at", None)
        try:
            words = fn(scenes, t, hold) if fn is not None else None
        except Exception:               # noqa: BLE001 —— 词表异常不该让参考帧渲不出来
            words = None
        # 字幕样式也必须与主渲染路径同源（见 _sub_kw_for 的实测说明）
        frame = _draw_subtitle_like_render(frame, text, theme, words,
                                           kw=_sub_kw_for(script))
    fade = 1.0
    if t < 0.5:
        fade *= _K.ease_out(t / 0.5)
    if t > total - 0.8:
        fade *= max(0.0, (total - t) / 0.8)
    if fade < 0.999:
        wipe = (0, 0, 0)
        if theme is not None and str(theme.get("transition") or "black") == "wash":
            wipe = tuple(theme["bg2"])
        frame = Image.blend(Image.new("RGB", frame.size, wipe), frame, fade)
    return frame


# ---------------------------------------------------------------- 比较（一次 ffmpeg 调用）
def _build_filtergraph(fps, step, threads, has_ssim, vmaf_log, ssim_log, dist_fps=None,
                       ref_scale=None):
    """构造 filter_complex：参考流从 stdin 的 rawvideo 来，成片走解码流，秒对齐。

    关键点：
    - 两侧都 settb=AVTB + setpts=N/(fps*TB)：把时间戳重排成 0,1,2… 的等间隔序列，
      否则 rawvideo（1/30）与 h264 解码（1/15360）的 timebase 不一致，
      libvmaf/ssim 的 framesync 会按"最近的下界时间戳"凑对，容易错帧。
    - 抽样（step>1）只对成片做 select，参考侧本来只喂抽中的帧，两边帧数天然相等。
    - split=2：**只渲染一遍**参考帧，同时喂 VMAF 与 SSIM 两个滤镜。
    - ref_scale：下采样交付时，把参考流用 lanczos 缩到成片分辨率再比——
      与 filmkit.FrameSink 编码时的 -vf scale=out_w:out_h:flags=lanczos 同口径
      （不在 PIL 侧缩，避免两套插值口径混用）。
    """
    tb = "settb=expr=AVTB,setpts=N/(%.6g*TB)" % float(fps)
    ref = "[0:v]"
    if ref_scale:
        ref += "scale=%d:%d:flags=lanczos," % (int(ref_scale["to"][0]),
                                              int(ref_scale["to"][1]))
    ref += "format=yuv420p,%s,split=2[ra][rb]" % tb
    d = ""
    if dist_fps and abs(float(dist_fps) - float(fps)) > 1e-3:
        d += "fps=%.6g," % float(fps)          # 帧率不一致时先折算到时间轴帧率
    if int(step) > 1:
        d += "select='not(mod(n\\,%d))'," % int(step)
    d += "format=yuv420p,%s,split=2[da][db]" % tb
    dist = "[1:v]" + d
    vmaf = ("[da][ra]libvmaf=log_path=%s:log_fmt=json:n_threads=%d:"
            "shortest=1:eof_action=endall[vout]" % (vmaf_log, int(threads)))
    parts = [ref, dist, vmaf]
    if has_ssim:
        parts.append("[db][rb]ssim=stats_file=%s:shortest=1:eof_action=endall[sout]" % ssim_log)
    return ";".join(parts)


def _read_vmaf(path):
    """解析 libvmaf JSON：返回 {mean,min,max,harmonic_mean,frames,per_frame[],version}。"""
    try:
        with open(path, encoding="utf-8") as fp:
            data = json.load(fp)
    except (OSError, json.JSONDecodeError):
        return None
    pooled = ((data.get("pooled_metrics") or {}).get("vmaf") or {})
    if not pooled:
        return None
    per = []
    for fr in (data.get("frames") or []):
        v = ((fr.get("metrics") or {}).get("vmaf"))
        if _finite(v):
            per.append(float(v))
    if not per:
        return None
    return {"mean": _f(pooled.get("mean"), float(np.mean(per))),
            "min": _f(pooled.get("min"), float(np.min(per))),
            "max": _f(pooled.get("max"), float(np.max(per))),
            "harmonic_mean": _f(pooled.get("harmonic_mean"), float(np.mean(per))),
            "frames": len(per), "per_frame": per,
            "version": str(data.get("version") or "")}


_SSIM_RE = re.compile(r"All:([0-9.]+)")
_SSIM_Y_RE = re.compile(r"Y:([0-9.]+)")


def _read_ssim(path):
    """解析 ssim 的 stats_file：每行一帧 "n:1 Y:0.99 U:.. V:.. All:0.98 (13.7)"。"""
    try:
        with open(path, encoding="utf-8", errors="replace") as fp:
            lines = fp.read().splitlines()
    except OSError:
        return None
    alls, ys = [], []
    for ln in lines:
        m = _SSIM_RE.search(ln)
        if m:
            try:
                alls.append(float(m.group(1)))
            except ValueError:
                continue
        m2 = _SSIM_Y_RE.search(ln)
        if m2:
            try:
                ys.append(float(m2.group(1)))
            except ValueError:
                pass
    if not alls:
        return None
    return {"mean": float(np.mean(alls)), "min": float(np.min(alls)),
            "y": float(np.mean(ys)) if ys else None, "frames": len(alls)}


# ---------------------------------------------------------------- 同源抽查
def _decode_gray_frame(film, t):
    """精确解码成片 t 秒处的那一帧，缩到 SPOT_W x SPOT_H 灰度 float32(0-1)。"""
    cmd = [_ffmpeg(), "-v", "error", "-ss", "%.6f" % max(0.0, float(t)), "-i", str(film),
           "-frames:v", "1", "-vf", "scale=%d:%d:flags=area" % (SPOT_W, SPOT_H),
           "-an", "-sn", "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"]
    p = _run(cmd)
    need = SPOT_W * SPOT_H
    if p.returncode != 0 or len(p.stdout or b"") < need:
        return None
    arr = np.frombuffer(p.stdout[:need], dtype=np.uint8).reshape(SPOT_H, SPOT_W)
    return arr.astype(np.float32) * (1.0 / 255.0)


def _spot_check(film, ref_img, t, ref_scale=None):
    """参考帧 vs 同刻成片帧的 MAE：用来发现"参考帧和成片不是同源"的漂移。

    只是漂移探测（缩到 160x90 灰度），这里用 PIL 缩放无妨；VMAF/SSIM 的正式
    比较一定走 ffmpeg 滤镜图（见 _build_filtergraph 的 ref_scale 分支）。
    """
    got = _decode_gray_frame(film, t)
    if got is None:
        return None
    img = ref_img
    if ref_scale:
        img = img.resize((int(ref_scale["to"][0]), int(ref_scale["to"][1])), Image.LANCZOS)
    small = img.convert("L").resize((SPOT_W, SPOT_H), Image.BOX)
    ref = np.asarray(small, dtype=np.float32) * (1.0 / 255.0)
    return {"t": round(float(t), 3), "mae": round(float(np.mean(np.abs(ref - got))), 5)}


# ---------------------------------------------------------------- 报告
def _metric(pass_, warn_):
    return {"score": None, "pass": float(pass_), "warn": float(warn_),
            "status": "skipped", "detail": "未执行", "min": None}


def _grade(score, pass_, warn_) -> str:
    if score < float(pass_):
        return "fail"
    if score < float(warn_):
        return "warn"
    return "pass"


def report(film_mp4, tl, scenes_mod, script, *, mode="full", out_json=None,
           vmaf_pass=VMAF_PASS, vmaf_warn=VMAF_WARN,
           ssim_pass=SSIM_PASS, ssim_warn=SSIM_WARN,
           limit_seconds=None, sample_step=None, threads=DEFAULT_THREADS,
           log=print) -> dict:
    """全片 VMAF/SSIM 门禁（详见模块 docstring 的"契约"与"三档语义"）。"""
    t_start = time.perf_counter()
    log = log or (lambda *_a, **_k: None)
    mode = str(mode or "full").strip().lower()
    if mode not in ("full", "sample"):
        raise ValueError("mode 只能是 full / sample（收到 %r）" % (mode,))
    step = int(sample_step) if sample_step else (1 if mode == "full" else SAMPLE_STEP)
    step = max(1, int(step))

    rep = {"judged": False, "mode": mode,
           "vmaf": _metric(vmaf_pass, vmaf_warn),
           "ssim": _metric(ssim_pass, ssim_warn),
           "frames_compared": 0, "elapsed_s": 0.0,
           "warnings": [], "errors": [],
           "gate": {"status": "skipped", "detail": ""},
           "film": str(film_mp4 or ""), "sample_step": step,
           "limit_seconds": limit_seconds, "version": __version__}
    warnings = rep["warnings"]
    errors = rep["errors"]

    if float(vmaf_pass) > float(vmaf_warn):
        warnings.append("VMAF 阈值顺序异常：vmaf_pass=%.2f > vmaf_warn=%.2f，"
                        "warning 区间为空（只按 fail 线判定）" % (vmaf_pass, vmaf_warn))
    if float(ssim_pass) > float(ssim_warn):
        warnings.append("SSIM 阈值顺序异常：ssim_pass=%.3f > ssim_warn=%.3f（只按 fail 线判定）"
                        % (ssim_pass, ssim_warn))

    def _skip(reason):
        """没查成：skipped + 写明原因（绝不冒充 pass）。"""
        detail = "未执行：%s" % reason
        rep["gate"] = {"status": "skipped", "detail": detail}
        rep["vmaf"].update(status="skipped", detail=detail)
        rep["ssim"].update(status="skipped", detail=detail)
        warnings.append("VMAF/SSIM 门禁未执行（三档语义 = skipped，不是通过）：%s" % reason)
        rep["elapsed_s"] = round(time.perf_counter() - t_start, 2)
        _finish(rep, out_json)
        return rep

    # ---- 1. 输入 ----
    film = str(film_mp4 or "")
    if not film or not os.path.exists(film):
        return _skip("成片不存在：%s" % film)
    film = os.path.abspath(film)
    rep["film"] = film
    if _K is None:
        return _skip("缺同目录 filmkit.py（参考帧要它的 W/H、字幕绘制与 ffmpeg 探测），"
                     "无法复刻 frames 阶段")
    try:
        tl = _as_timeline(tl)
    except ValueError as e:
        return _skip(str(e))
    try:
        scenes_mod = _as_scenes(scenes_mod)
    except Exception as e:              # noqa: BLE001
        return _skip("场景模块加载失败：%s" % e)
    try:
        script = _as_script(script)
    except ValueError as e:
        return _skip(str(e))

    scenes = tl.get("scenes") or []
    if not scenes:
        return _skip("时间轴里没有 scenes（timeline.json 的 scenes 为空）")
    fps = _f(tl.get("fps"), 0.0) or _f(getattr(_K, "FPS", 30), 30.0)
    if not _finite(fps) or fps <= 0:
        return _skip("时间轴 fps 非法：%r" % (tl.get("fps"),))
    total = _f(tl.get("total"), 0.0)
    if total <= 0:
        return _skip("时间轴 total 非法：%r" % (tl.get("total"),))
    frames = int(round(_f(tl.get("frames"), 0.0) or total * fps))
    if frames <= 0:
        return _skip("时间轴 frames 非法：%r" % (tl.get("frames"),))

    # ---- 2. ffmpeg 能力（fail-closed：找不到 = skipped） ----
    try:
        ff = _ffmpeg()
    except ValueError as e:
        return _skip(str(e))
    caps = _capabilities(ff)
    rep["ffmpeg"] = ff
    # **顺序很重要**：libvmaf 能力门禁必须排在第 3 步"结构性校验"**之后**。
    # 上采样 / 尺寸与声明不符 / 非 16:9 这三条是**纯几何判定**（只看 ffprobe 拿到的
    # 宽高，连参考帧都不用画），跟有没有 libvmaf 毫无关系。以前能力门在它们前面，
    # 于是 apt 构建的 ffmpeg（Ubuntu 的 ffmpeg 不带 libvmaf）会让这三条一起被记成
    # "未执行：ffmpeg 构建不含 libvmaf"——**把能查的也当成查不了**，
    # CI 上这三条因此永远拿不到真实结论（qavmaf 自检里 3 项长期漂移）。

    # ---- 3. 成片探测 + 结构性校验（含下采样交付） ----
    info = _probe_film(film)
    rep["film_info"] = info
    W, H = int(getattr(_K, "W", 1920)), int(getattr(_K, "H", 1080))
    verdict, ref_scale, size_detail = _judge_sizes(info, tl, script, W, H)
    rep["ref_scale"] = ref_scale
    rep["size_verdict"] = verdict
    if verdict == "fail":
        rep["gate"] = {"status": "fail", "detail": size_detail}
        rep["vmaf"].update(status="fail", detail=size_detail)
        rep["ssim"].update(status="skipped", detail=size_detail)
        errors.append(size_detail)
        rep["elapsed_s"] = round(time.perf_counter() - t_start, 2)
        _finish(rep, out_json)
        return rep
    if ref_scale:
        warnings.append("成片 %dx%d 是下采样交付：参考流在 ffmpeg 滤镜图里 lanczos 缩到"
                        "成片分辨率后比较（与 FrameSink 编码口径一致）；VMAF 阈值仍按 "
                        "1080p 标定，分数与 1080p 交付不完全可比"
                        % (ref_scale["to"][0], ref_scale["to"][1]))

    # ---- 3.5 VMAF 能力门禁（fail-closed：没有 libvmaf 才算 skipped） ----
    # 放在结构性校验之后：几何问题该报 fail 就报 fail，不该被"没装 libvmaf"挡在前面。
    if not caps.get("libvmaf"):
        return _skip("ffmpeg 构建不含 libvmaf 滤镜（%s）：无法计算 VMAF，本次未检查画质"
                     % ff)
    has_ssim = bool(caps.get("ssim"))
    if not has_ssim:
        warnings.append("ffmpeg 构建不含 ssim 滤镜，本次只算 VMAF（SSIM 记为 skipped）")

    # ---- 4. 参考帧输入（含脚本/主题准备） ----
    try:
        script, theme = _prepare_script(script, tl)
    except Exception as e:              # noqa: BLE001 —— 风格解析失败也算"没法复刻参考帧"
        return _skip("参考帧准备失败（剧本风格/主题解析）：%s" % e)

    # ---- 5. 比较计划 ----
    n_expect = frames
    if limit_seconds is not None:
        lim = _f(limit_seconds, 0.0)
        if lim > 0:
            n_expect = min(n_expect, int(round(lim * fps)))
    n_film = None
    if info.get("nb_frames"):
        n_film = int(info["nb_frames"])
    elif info.get("duration"):
        n_film = int(round(float(info["duration"]) * fps))
    if n_film and n_film < n_expect:
        warnings.append("成片只有 %d 帧（时间轴 %d 帧）：只比较两者都能对上的前 %d 帧"
                        % (n_film, frames, n_film))
        n_expect = max(0, n_film)
    if n_expect <= 0:
        return _skip("没有可比较的帧（时间轴 %d 帧 / 成片 %r 帧）" % (frames, n_film))
    indices = list(range(0, n_expect, step))
    rep["frames_expected"] = len(indices)
    rep["frames_timeline"] = frames
    rep["film_fps"] = info.get("fps")

    dist_fps = None
    if info.get("fps"):
        dist_fps = float(info["fps"])
        if abs(dist_fps - fps) > 1e-3:
            warnings.append("成片帧率 %.6g 与时间轴 %.6g 不一致：成片侧用 fps 滤镜重采样后比较"
                            "（逐帧对齐已不可保证，分数仅供参考）" % (dist_fps, fps))
    if step > 1:
        warnings.append("sample 模式：每 %d 帧取 1 帧（共 %d 帧），结论不代表全片逐帧画质"
                        % (step, len(indices)))
    if limit_seconds is not None and _f(limit_seconds, 0) > 0:
        warnings.append("--limit-seconds=%.2fs：只比较了前 %.2fs，结论不代表全片"
                        % (_f(limit_seconds, 0), n_expect / float(fps)))

    # ---- 6. 一次 ffmpeg 调用：边渲参考帧边比较 ----
    tmpdir = tempfile.mkdtemp(prefix="qavmaf_")
    vmaf_log = "vmaf.json"
    ssim_log = "ssim.log"
    graph = _build_filtergraph(fps, step, threads, has_ssim, vmaf_log, ssim_log,
                               dist_fps=dist_fps, ref_scale=ref_scale)
    tlim = n_expect / float(fps) + (1.0 / float(fps))     # 多给一帧余量，避免舍入丢尾帧
    cmd = [ff, "-hide_banner", "-nostats", "-v", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "%dx%d" % (W, H),
           "-r", "%g" % float(fps), "-i", "pipe:0",
           "-t", "%.6f" % tlim, "-i", film,
           "-filter_complex", graph,
           "-map", "[vout]", "-f", "null", "-"]
    if has_ssim:
        cmd += ["-map", "[sout]", "-f", "null", "-"]

    spot_i = indices[len(indices) // 2]
    state = {"rendered": 0, "error": "", "spot": None}
    render_s = 0.0
    rc = -1
    err_txt = ""
    errf = tempfile.TemporaryFile()
    try:
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                    stderr=errf, cwd=tmpdir)
        except OSError as e:
            return _skip("启动 ffmpeg 失败：%s" % e)
        t_r0 = time.perf_counter()
        report_every = max(1, len(indices) // 10)
        try:
            for i, fi in enumerate(indices):
                frame = _draw_ref_frame(scenes, scenes_mod, script, theme, fi, fps, total)
                if fi == spot_i:
                    state["spot"] = frame.copy()
                try:
                    proc.stdin.write(frame.tobytes())
                except (BrokenPipeError, OSError):
                    state["error"] = "ffmpeg 提前退出（参考帧管道已关闭），见 stderr"
                    break
                state["rendered"] = i + 1
                if (i + 1) % report_every == 0 or (i + 1) == len(indices):
                    el = max(1e-6, time.perf_counter() - t_r0)
                    done = i + 1
                    eta = (len(indices) - done) / max(done / el, 1e-6)
                    log("   参考帧 %d/%d  %.1f 帧/秒  eta %.0fs"
                        % (done, len(indices), done / el, eta))
        except Exception as e:          # noqa: BLE001 —— 渲染/尺寸/字体错误都在这
            bad = indices[min(state["rendered"], len(indices) - 1)]
            state["error"] = "参考帧渲染失败（第 %d 帧 / t=%.3fs）：%s" % (
                bad, bad / float(fps), e)
        finally:
            render_s = time.perf_counter() - t_r0
            try:
                proc.stdin.close()
            except Exception:           # noqa: BLE001
                pass
            rc = proc.wait()
            try:
                errf.seek(0)
                err_txt = errf.read().decode("utf-8", "replace")
            except Exception:           # noqa: BLE001
                err_txt = ""
            errf.close()
    finally:
        # 双保险：异常路径也不能泄漏临时 stderr 文件（重复 close 是 no-op）
        try:
            errf.close()
        except Exception:               # noqa: BLE001
            pass

    try:
        vj = _read_vmaf(os.path.join(tmpdir, vmaf_log))
        ss = _read_ssim(os.path.join(tmpdir, ssim_log)) if has_ssim else None
        spot = None
        if state["spot"] is not None:
            try:
                spot = _spot_check(film, state["spot"], spot_i / float(fps),
                                   ref_scale=ref_scale)
            except Exception as e:      # noqa: BLE001
                spot = None
                warnings.append("同源抽查失败（不影响门禁）：%s" % e)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    rep["render_s"] = round(render_s, 2)
    rep["frames_rendered"] = int(state["rendered"])
    if spot:
        rep["spot_check"] = spot
    if state["error"]:
        # 参考帧没渲完 = 后半段"没查"：一律 skipped，不给"只查了一半"的 pass。
        return _skip("%s（已渲染 %d/%d 帧）" % (state["error"], state["rendered"],
                                              len(indices)))
    if rc != 0:
        return _skip("ffmpeg 比较失败（rc=%d）：%s" % (rc, _tail(err_txt, 500)))

    frames_compared = 0
    if vj:
        frames_compared = int(vj["frames"])
    elif ss:
        frames_compared = int(ss["frames"])
    rep["frames_compared"] = frames_compared
    if frames_compared <= 0:
        return _skip("比较了 0 帧（成片被 -t 截断？%s）" % (_tail(err_txt, 300) or "无 stderr"))

    # ---- 7. 判定 ----
    rep["coverage"] = round(frames_compared / float(len(indices)), 4)
    if frames_compared < len(indices):
        warnings.append("实际比较 %d 帧 < 计划 %d 帧（成片提前结束或帧率折算），"
                        "覆盖率 %.1f%%" % (frames_compared, len(indices),
                                         rep["coverage"] * 100.0))

    vscore = float(vj["mean"]) if vj else None
    vmin = float(vj["min"]) if vj else None
    if vj:
        per = np.asarray(vj["per_frame"], dtype=np.float64)
        p5 = float(np.percentile(per, 5)) if per.size else vmin
        status = _grade(vscore, vmaf_pass, vmaf_warn)
        rep["vmaf"].update(score=round(vscore, 3), min=round(vmin, 3),
                           p5=round(p5, 3), max=round(float(vj["max"]), 3),
                           harmonic_mean=round(float(vj["harmonic_mean"]), 3),
                           frames=frames_compared, status=status,
                           detail="VMAF 均值 %.2f（min %.2f / p5 %.2f），阈值 pass≥%.1f / warn≥%.1f"
                           % (vscore, vmin, p5, vmaf_pass, vmaf_warn),
                           model={"version": vj.get("version") or "",
                                  "note": "libvmaf 默认模型 vmaf_v0.6.1（ffmpeg 内置）"})
    else:
        return _skip("libvmaf 没有产出逐帧分数（log 文件缺失或为空）")

    sscore = float(ss["mean"]) if ss else None
    if ss:
        status_s = _grade(sscore, ssim_pass, ssim_warn)
        rep["ssim"].update(score=round(sscore, 5), min=round(float(ss["min"]), 5),
                           y=round(float(ss["y"]), 5) if ss.get("y") is not None else None,
                           frames=ss["frames"], status=status_s,
                           detail="SSIM 均值 %.4f（min %.4f / Y %.4f），诊断阈值 pass≥%.3f"
                           % (sscore, ss["min"], ss.get("y") or 0.0, ssim_pass))
    else:
        rep["ssim"].update(status="skipped",
                           detail="ssim 未产出统计（ffmpeg 构建缺 ssim 或统计文件为空）")

    # ---- 8. 汇总 ----
    gate_status = "fail" if vscore < float(vmaf_pass) else "pass"
    if gate_status == "fail":
        errors.append("VMAF 均值 %.2f < 失败阈值 %.1f（画面已明显劣化：检查 crf/preset/"
                      "是否二次编码/是否缩放插值）" % (vscore, vmaf_pass))
    elif vscore < float(vmaf_warn):
        warnings.append("VMAF 均值 %.2f 通过（≥%.1f），但低于建议阈值 %.1f："
                        "建议复核 crf/preset 与画面复杂度" % (vscore, vmaf_pass, vmaf_warn))
    if sscore is not None:
        if sscore < float(ssim_pass):
            warnings.append("SSIM 均值 %.4f < %.3f：结构相似度偏低（VMAF 为准，此项仅诊断）"
                            % (sscore, ssim_pass))
        elif sscore < float(ssim_warn):
            warnings.append("SSIM 均值 %.4f 低于建议值 %.3f（仅诊断，不阻断）"
                            % (sscore, ssim_warn))
    if spot:
        if _f(spot.get("mae"), 0.0) > SPOT_WARN_MAE:
            warnings.append("同源抽查异常：t=%.2fs 参考帧与成片帧 MAE=%.4f > %.3f —— "
                            "参考帧可能与成片不同源（render.py 的字幕/淡入淡出/主题改过？）"
                            "本次 VMAF 分数可能偏低"
                            % (spot["t"], spot["mae"], SPOT_WARN_MAE))

    rep["judged"] = True
    rep["elapsed_s"] = round(time.perf_counter() - t_start, 2)
    rep["gate"] = {"status": gate_status,
                   "detail": _gate_detail(rep, gate_status)}
    _finish(rep, out_json)
    return rep


def _gate_detail(rep, status) -> str:
    v, s = rep.get("vmaf") or {}, rep.get("ssim") or {}
    bits = []
    if status == "fail":
        bits.append("VMAF 均值 %.2f < 失败阈值 %.1f" % (_f(v.get("score")), _f(v.get("pass"))))
    else:
        bits.append("VMAF 均值 %.2f ≥ 失败阈值 %.1f（建议阈值 %.1f）"
                    % (_f(v.get("score")), _f(v.get("pass")), _f(v.get("warn"))))
    if s.get("score") is not None:
        bits.append("SSIM %.4f" % _f(s.get("score")))
    bits.append("比较 %d/%d 帧" % (int(rep.get("frames_compared") or 0),
                                  int(rep.get("frames_expected") or 0)))
    if int(rep.get("sample_step") or 1) > 1:
        bits.append("sample 每 %d 帧取 1 帧" % int(rep.get("sample_step") or 1))
        bits.append("非全片结论")
    elif rep.get("limit_seconds"):
        bits.append("仅前 %.1fs" % _f(rep.get("limit_seconds")))
    fi = rep.get("film_info") or {}
    if fi.get("width"):
        bits.append("%dx%d" % (int(fi["width"]), int(fi["height"])))
    rs = rep.get("ref_scale")
    if rs:
        bits.append("参考流 %dx%d→%dx%d" % (int(rs["from"][0]), int(rs["from"][1]),
                                           int(rs["to"][0]), int(rs["to"][1])))
    bits.append("耗时 %.1fs（参考帧渲染 %.1fs）"
                % (_f(rep.get("elapsed_s")), _f(rep.get("render_s"))))
    return "；".join(bits)


def _finish(rep, out_json=None):
    """收尾：算耗时、写 JSON（失败不掩盖主结论）。"""
    if not (rep.get("gate") or {}).get("status"):
        rep.setdefault("gate", {})["status"] = "skipped"
    if out_json:
        try:
            _write_json(rep, out_json)
            rep["out_json"] = os.path.abspath(str(out_json))
        except Exception as e:          # noqa: BLE001
            rep.setdefault("warnings", []).append("报告 JSON 落盘失败：%s" % e)


def _print_summary(rep, film, out_json=None):
    """人读摘要（与 qa_frames 的 _print_summary 同风格）。"""
    g = rep.get("gate") or {}
    st = str(g.get("status") or "skipped")
    print("== qavmaf 全片画质门禁（VMAF/SSIM）")
    print("   成片:", film)
    print("   模式: %s%s" % (rep.get("mode"), ("（每 %d 帧取 1 帧）" % rep["sample_step"])
                             if rep.get("mode") == "sample" else ""))
    v, s = rep.get("vmaf") or {}, rep.get("ssim") or {}
    if v.get("score") is not None:
        print("   VMAF: %.2f（min %.2f / p5 %.2f，%d 帧） 阈值 pass≥%.1f warn≥%.1f -> %s"
              % (_f(v["score"]), _f(v.get("min")), _f(v.get("p5")),
                 int(rep.get("frames_compared") or 0), _f(v.get("pass")),
                 _f(v.get("warn")), v.get("status")))
    else:
        print("   VMAF: 未计算")
    if s.get("score") is not None:
        print("   SSIM: %.4f（min %.4f，%d 帧，诊断项）-> %s"
              % (_f(s["score"]), _f(s.get("min")), int(s.get("frames") or 0), s.get("status")))
    else:
        print("   SSIM: 未计算（%s）" % (s.get("detail") or ""))
    rs = rep.get("ref_scale")
    if rs:
        print("   参考流缩放: %dx%d -> %dx%d（lanczos，与编码口径一致）"
              % (rs["from"][0], rs["from"][1], rs["to"][0], rs["to"][1]))
    print("   比较 %s/%s 帧  耗时 %.1fs（参考帧渲染 %.1fs）"
          % (rep.get("frames_compared"), rep.get("frames_expected"),
             _f(rep.get("elapsed_s")), _f(rep.get("render_s"))))
    for e in rep.get("errors") or []:
        print("   [FAIL]", e)
    for w in rep.get("warnings") or []:
        print("   [WARN]", w)
    tag = {"pass": "PASS", "fail": "FAIL", "skipped": "SKIPPED（未检查，不是通过）"}.get(st, st)
    print("   结论:", tag, "—", g.get("detail") or "")
    if out_json:
        print("   报告 JSON:", os.path.abspath(str(out_json)))


# ---------------------------------------------------------------- 自检
def selftest() -> int:
    """内置自检：合成片验证管线与三档语义（不联网，≤60 s）。

    注意：这里只测"管线 + 三档语义"——参考帧与 render.py 的同源性由真实片实验
    （.team/ 里的 crf17/crf30 对照）保证，合成片用它自己渲染的帧编码，
    因此不会出现"参考帧拿成片抽帧"那种恒等于 100 的假通过（那是被测模块的硬伤，
    不是自检的检查项）。
    """
    _setup_stdio()
    if _K is None:
        print("[FAIL] 自检需要同目录 filmkit.py")
        return 1
    results = []

    def check(name, fn):
        """跑一条检查。fn 返回 (True|False, detail) 或 ("skip", detail)。

        **三态**：环境不具备（比如这台机器的 ffmpeg 不带 libvmaf）记 SKIP，
        不能记 FAIL —— 那是"没查"而不是"查出来有问题"，混为一谈会让真正的
        代码回归被环境噪声淹没（本项目对 skipped != 通过 有明文约定）。
        """
        try:
            ok, detail = fn()
        except Exception as e:          # noqa: BLE001
            ok, detail = False, "异常：%s" % e
        if ok == "skip":
            results.append((name, "skip", detail))
            print("   [SKIP] %s%s" % (name, ("  —— " + detail) if detail else ""))
            return
        results.append((name, bool(ok), detail))
        print("   [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                                ("  —— " + detail) if detail else ""))


    def _no_libvmaf():
        """本机 ffmpeg 是否缺 libvmaf（缺则由依赖 VMAF 的检查统一记 SKIP）。"""
        try:
            return not _capabilities(_ffmpeg()).get("libvmaf")
        except Exception:               # noqa: BLE001 —— 探测失败也当"不具备"
            return True

    tmp = tempfile.mkdtemp(prefix="qavmaf_selftest_")
    try:
        W, H = int(_K.W), int(_K.H)
        n, fps = 24, 30
        scenes_src = '''# -*- coding: utf-8 -*-
import filmkit as K
from PIL import Image, ImageDraw


def draw_frame(scene, t_local, dur, t_global, script, frame):
    W, H = K.W, K.H
    im = Image.new("RGB", (W, H), (10, 16, 28))
    d = ImageDraw.Draw(im)
    p = min(1.0, max(0.0, t_local / max(dur, 1e-6)))
    x = int(120 + p * (W - 480))
    d.rectangle([x, 260, x + 360, 760], fill=(236, 168, 64))
    d.rectangle([180, 120, W - 180, 190], fill=(40, 90, 140))
    d.text((60, 40), "qavmaf selftest %d" % frame, fill=(240, 245, 250))
    return im
'''
        scenes_path = os.path.join(tmp, "scenes.py")
        with open(scenes_path, "w", encoding="utf-8") as fp:
            fp.write(scenes_src)
        script = {"title": "qavmaf selftest", "lines": [
            {"id": "l01", "text": "一", "sub": "自检"}]}
        # 没有内置风格表：合成剧本也必须自带一份**现场生成**的设计，
        # 否则 _style_plan() 解析不出主题（"不写 style 就走老外观"的兼容路径已移除），
        # 参考帧无法渲染。这里用 designkit 现场采样（seed 固定 = 可复现）、
        # design_to_plan 与 render 同一口径，**不引入任何写死主题/模板**。
        try:
            import designkit as _DK
            _design = _DK.sample_designs({"topic": "qavmaf selftest"}, n=1, seed=1)[0]
            script["_style"] = _DK.design_to_plan(_design)
        except Exception as e:          # noqa: BLE001 —— 缺 designkit 也要给出可读的 FAIL
            print("[FAIL] 自检需要 designkit.py 现场生成设计（无内置风格）：%s" % e)
            return 1
        scenes = [dict(script["lines"][0], start=0.0, end=n / float(fps),
                       dur=n / float(fps), voice_start=0.0, voice_end=n / float(fps))]
        tl = {"total": n / float(fps), "fps": fps, "frames": n, "scenes": scenes,
              "video": {"out_w": W, "out_h": H, "scale": False}}
        # 下采样交付：内部构图仍 1920x1080，交付 1280x720（编码时 lanczos 缩）
        tl720 = dict(tl, video={"out_w": 1280, "out_h": 720, "scale": True})
        # 老 timeline（没有 video 块）：只按"构图同尺寸"校验
        tl_legacy = {k: v for k, v in tl.items() if k != "video"}
        # 非法：声明 2560x1440 输出（内部构图只有 1080p，等于上采样）
        tl_up = dict(tl, video={"out_w": 2560, "out_h": 1440, "scale": True})
        mod = load_module(scenes_path, "qavmaf_selftest_scenes")
        script_p, theme = _prepare_script(script, tl)
        refs = [_draw_ref_frame(scenes, mod, script_p, theme, fi, fps, tl["total"])
                for fi in range(n)]

        def _encode(path, frames, crf, preset="veryfast", out_size=None):
            cmd = [_ffmpeg(), "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
                   "-s", "%dx%d" % (W, H), "-r", "%g" % fps, "-i", "-"]
            if out_size:
                # 与 filmkit.FrameSink 的 -vf scale=out_w:out_h:flags=lanczos 同口径
                cmd += ["-vf", "scale=%d:%d:flags=lanczos" % tuple(out_size)]
            cmd += ["-c:v", "libx264", "-preset", preset, "-crf", str(crf),
                    "-pix_fmt", "yuv420p", "-g", "60", path]
            p = subprocess.run(cmd, input=b"".join(f.tobytes() for f in frames),
                               capture_output=True)
            if p.returncode != 0:
                raise RuntimeError("编码失败：%s" % _tail(p.stderr.decode("utf-8", "replace"), 300))
            return path

        film_ok = _encode(os.path.join(tmp, "ok.mp4"), refs, 0)
        film_720 = _encode(os.path.join(tmp, "ok720.mp4"), refs, 0, out_size=(1280, 720))
        blurred = [f.filter(ImageFilter.GaussianBlur(4)) for f in refs]
        film_bad = _encode(os.path.join(tmp, "bad.mp4"), blurred, 34)

        base = dict(tl=tl, scenes_mod=mod, script=script)

        def c_pass():
            rep = report(film_ok, mode="full", log=None, **base)
            v = rep["vmaf"]["score"]
            ok = rep["judged"] and rep["gate"]["status"] == "pass"
            return ok and v is not None and v >= 90.0, "VMAF=%s gate=%s frames=%s" % (
                v, rep["gate"]["status"], rep["frames_compared"])

        def c_bad():
            rep = report(film_bad, mode="full", log=None, **base)
            v = rep["vmaf"]["score"]
            ok = rep["judged"] and rep["gate"]["status"] == "fail"
            return ok and v is not None and v < 92.0, "VMAF=%s gate=%s" % (
                v, rep["gate"]["status"])

        def c_discriminate():
            a = report(film_ok, mode="full", log=None, **base)["vmaf"]["score"]
            b = report(film_bad, mode="full", log=None, **base)["vmaf"]["score"]
            return (a - b) > 15.0, "lossless=%.2f bad=%.2f (差 %.2f)" % (a, b, a - b)

        def c_skip_no_scenes():
            rep = report(film_ok, tl, None, script, log=None)
            msg = " ".join(rep["warnings"])
            ok = (rep["gate"]["status"] == "skipped" and not rep["judged"]
                  and "场景" in msg)
            return ok, rep["gate"]["detail"]

        def c_skip_no_timeline():
            rep = report(film_ok, os.path.join(tmp, "nope.json"), mod, script, log=None)
            ok = rep["gate"]["status"] == "skipped" and not rep["judged"]
            return ok, rep["gate"]["detail"]

        def c_sample():
            rep = report(film_ok, mode="sample", sample_step=6, log=None, **base)
            exp = (n + 5) // 6
            ok = rep["judged"] and abs(int(rep["frames_compared"]) - exp) <= 1
            return ok, "frames_compared=%s 计划 %d" % (rep["frames_compared"], exp)

        def c_limit():
            rep = report(film_ok, mode="full", limit_seconds=n / float(fps) / 2,
                         log=None, **base)
            ok = rep["judged"] and int(rep["frames_compared"]) <= n // 2 + 1
            any_w = any("limit-seconds" in w for w in rep["warnings"])
            return ok and any_w, "frames_compared=%s" % rep["frames_compared"]

        def c_downscale_pass():
            # 合法配置：1920x1080 内部构图 -> 1280x720 交付，必须 pass；
            # 且与 1080p 同规格成片的 VMAF 差 < 3 分（阈值可比）
            rep = report(film_720, tl720, mod, script, mode="full", log=None)
            v1080 = report(film_ok, mode="full", log=None, **base)["vmaf"]["score"]
            rs = rep.get("ref_scale")
            ok = (rep["judged"] and rep["gate"]["status"] == "pass"
                  and rs == {"from": [W, H], "to": [1280, 720]})
            gap = abs(float(rep["vmaf"]["score"]) - float(v1080))
            return ok and gap < 3.0, ("720p VMAF=%.2f vs 1080p=%.2f 差=%.2f ref_scale=%s gate=%s"
                                      % (rep["vmaf"]["score"], v1080, gap, rs,
                                         rep["gate"]["status"]))

        def c_upscale_fail():
            # 2560x1440 成片 = 上采样，不是合法交付 -> fail（不是 skipped/pass）
            p = os.path.join(tmp, "up.mp4")
            cmd = [_ffmpeg(), "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
                   "-s", "%dx%d" % (W, H), "-r", "%g" % fps, "-i", "-",
                   "-vf", "scale=2560:1440:flags=lanczos",
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
                   "-pix_fmt", "yuv420p", p]
            data = b"".join(f.tobytes() for f in refs)
            q = subprocess.run(cmd, input=data, capture_output=True)
            if q.returncode != 0:
                return False, "编码失败：%s" % _tail(q.stderr.decode("utf-8", "replace"), 200)
            rep = report(p, tl_up, mod, script, mode="full", log=None)
            msg = " ".join(rep["errors"])
            ok = (rep["gate"]["status"] == "fail" and not rep["judged"]
                  and "上采样" in msg)
            return ok, rep["gate"]["detail"]

        def c_declared_fail():
            # 1080p 成片 + timeline 声明 720p：规格被改过/拿错片 -> fail
            rep = report(film_ok, tl720, mod, script, mode="full", log=None)
            msg = " ".join(rep["errors"])
            ok = (rep["gate"]["status"] == "fail" and not rep["judged"]
                  and "不一致" in msg)
            return ok, rep["gate"]["detail"]

        def c_aspect_fail():
            # 1280x960 = 4:3，不是 16:9：不能按"缩放交付"放行
            p = os.path.join(tmp, "4x3.mp4")
            cmd = [_ffmpeg(), "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
                   "-s", "%dx%d" % (W, H), "-r", "%g" % fps, "-i", "-",
                   "-vf", "scale=1280:960:flags=lanczos",
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
                   "-pix_fmt", "yuv420p", p]
            data = b"".join(f.tobytes() for f in refs)
            q = subprocess.run(cmd, input=data, capture_output=True)
            if q.returncode != 0:
                return False, "编码失败：%s" % _tail(q.stderr.decode("utf-8", "replace"), 200)
            rep = report(p, tl_legacy, mod, script, mode="full", log=None)
            msg = " ".join(rep["errors"])
            ok = (rep["gate"]["status"] == "fail" and "16:9" in msg)
            return ok, rep["gate"]["detail"]

        # 依赖 VMAF **分数**的检查：本机 ffmpeg 不带 libvmaf 时只能 SKIP
        # （没分数就没法判"高/低/区分度"）。它们被单独列出来，是为了让
        # "环境不具备"和"代码坏了"在输出上永远分得开。
        def need_vmaf(fn, what="这条检查要算 VMAF 分数"):
            def wrapped():
                if _no_libvmaf():
                    return "skip", "本机 ffmpeg 不带 libvmaf（%s），%s" % (_ffmpeg(), what)
                return fn()
            return wrapped

        check("正常片 VMAF 高、不误判 fail", need_vmaf(c_pass))
        check("劣化片 VMAF 低、判 fail", need_vmaf(c_bad))
        check("好片/劣化片分数区分度 > 15", need_vmaf(c_discriminate))
        check("缺 scenes -> skipped（不是 pass）", c_skip_no_scenes)
        check("缺 timeline -> skipped（不是 pass）", c_skip_no_timeline)
        check("sample 模式帧数按步长缩减", need_vmaf(c_sample, "抽样帧数要靠真跑一轮比较得出"))
        check("limit_seconds 生效并记 warning", need_vmaf(c_limit, "帧数统计要靠真跑一轮比较得出"))
        check("720p 下采样交付 -> pass 且与 1080p 差 < 3 分", need_vmaf(c_downscale_pass))
        # 下面三条是**纯几何判定**（只看 ffprobe 的宽高），与 libvmaf 无关，
        # 所以在任何机器上都必须给出真实结论——这正是 report() 里把能力门
        # 挪到结构性校验之后要保住的东西。
        check("上采样成片 -> fail（不是合法交付）", c_upscale_fail)
        check("成片尺寸与 timeline.video 声明不符 -> fail", c_declared_fail)
        check("非 16:9 成片 -> fail", c_aspect_fail)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    npass = sum(1 for _n, ok, _d in results if ok is True)
    nfail = sum(1 for _n, ok, _d in results if ok is False)
    nskip = sum(1 for _n, ok, _d in results if ok == "skip")
    print("== 结果：%d/%d PASS%s" % (
        npass, len(results),
        ("（FAIL %d / SKIP %d）" % (nfail, nskip)) if (nfail or nskip) else ""))
    if nskip:
        print("   SKIP = 本机环境不具备该能力（不是通过，也不是失败）；"
              "要覆盖请装带 libvmaf 的 ffmpeg，见 references/quality-gates.md")
    # 只有真实失败才返回非 0；SKIP 不影响退出码（否则装了 apt ffmpeg 的机器永远红）
    return 0 if nfail == 0 else 1


# ---------------------------------------------------------------- CLI
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="qavmaf.py",
        description="全片 VMAF/SSIM 画质门禁：参考帧由场景函数实时渲染")
    ap.add_argument("--film", default=None, help="成片路径（mp4）")
    ap.add_argument("--timeline", default=None, help="work/timeline.json")
    ap.add_argument("--scenes", default=None, help="scenes.py（含 draw_frame）")
    ap.add_argument("--script", default=None, help="film.json（与 frames 阶段同一份）")
    ap.add_argument("--mode", choices=("full", "sample"), default="full",
                    help="full=逐帧（默认）；sample=抽样（默认每 5 帧取 1 帧）")
    ap.add_argument("--sample-step", type=int, default=None, help="抽样步长（默认 5）")
    ap.add_argument("--limit-seconds", type=float, default=None,
                    help="只比较前 N 秒（自测/冒烟用；会记 warning）")
    ap.add_argument("--vmaf-pass", type=float, default=VMAF_PASS, help="VMAF 失败阈值")
    ap.add_argument("--vmaf-warn", type=float, default=VMAF_WARN, help="VMAF 建议阈值")
    ap.add_argument("--ssim-pass", type=float, default=SSIM_PASS, help="SSIM 诊断阈值")
    ap.add_argument("--ssim-warn", type=float, default=SSIM_WARN, help="SSIM 建议阈值")
    ap.add_argument("--threads", type=int, default=DEFAULT_THREADS,
                    help="libvmaf 线程数（默认 min(8, CPU)；0=由 libvmaf 自决）")
    ap.add_argument("--out", default=None, help="报告 JSON 输出路径（缺省不写）")
    ap.add_argument("--json", action="store_true", help="stdout 只输出报告 JSON")
    ap.add_argument("--selftest", action="store_true", help="跑内置自检（不联网）")
    a = ap.parse_args(argv)
    _setup_stdio()
    if a.selftest:
        return selftest()
    missing = [k for k in ("film", "timeline", "scenes", "script")
               if not getattr(a, k)]
    if missing:
        ap.error("缺少参数：--%s（或用 --selftest）" % " / --".join(missing))
    logf = (lambda msg: print(msg, file=sys.stderr)) if a.json else print
    try:
        rep = report(a.film, a.timeline, a.scenes, a.script, mode=a.mode,
                     out_json=a.out, vmaf_pass=a.vmaf_pass, vmaf_warn=a.vmaf_warn,
                     ssim_pass=a.ssim_pass, ssim_warn=a.ssim_warn,
                     limit_seconds=a.limit_seconds, sample_step=a.sample_step,
                     threads=a.threads, log=logf)
    except Exception as e:              # noqa: BLE001
        if a.json:
            print(json.dumps({"judged": False,
                              "gate": {"status": "skipped",
                                       "detail": "qavmaf 内部错误：%s" % e}},
                             ensure_ascii=False))
        print("[ERROR] qavmaf 内部错误：%s" % e, file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=1, allow_nan=False))
    else:
        _print_summary(rep, a.film, a.out)
    st = str((rep.get("gate") or {}).get("status") or "skipped")
    return 0 if st == "pass" else (1 if st == "fail" else 2)


if __name__ == "__main__":
    sys.exit(main())
